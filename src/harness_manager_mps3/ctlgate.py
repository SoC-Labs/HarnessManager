"""One control connection per board at a time, in this process (lane SERIAL-6900).

**Why.** The MPS3 control port (6900) serves ONE client: while a connection is open, every
other connect is accepted and closed at once (harnessd, SERVICE_DISPOSITION §2A.1) or reset
(lwIP). On 2026-09-28 the lab MPS3 turned Harness Manager away from ITSELF: the service's
threads (the page's refresh burst: identity, diag, panel, telemetry, card, slots, console
rates; plus routes the daemon's per-board op gate does not cover, such as ``GET /session``,
``/claim`` and ``/display``, and the presence beat's feature read) each opened their own
connection at the same moment, refused each other, and QUIET-POLL read every refusal as
"another client" and backed off.

**The gate.** Every 6900 connection this process opens to a board goes through that board's
``ControlGate`` (``Mps3Shell.call_raw``, the deploy's parked swap connection, the OS-slot
requests pyverify sends, the card's parked commit, and the same through a claim forward):

- one connection at a time per board, keyed by the board's own control address
  (``key_for``), so the tunnel's local port, a probe's short tunnel and the claim's board-SSH
  forward to the board's 127.0.0.1 all share one gate;
- FIFO: waiters are served in arrival order, so a burst of background reads cannot starve a
  click;
- bounded: a waiter gives up after ``GATE_WAIT_S`` (10 s) with ``OwnRequestBusyError``, "busy
  with this Harness Manager's own request". That is never "another client":
  ``services.quiet.is_contention`` skips it, so our own queue never starts a back-off;
- re-entrant: a thread that holds the gate and asks again passes at once (no deadlock). The
  board would still turn that inner connection away, so it is counted (``stats.nested``) and
  logged: no path should hold one connection while opening another.

**One request per connection, closed at once** (QUIET-POLL) is unchanged: the gate is held
from the connect to the close, never between calls.

**Our own ghost** (the reap). The harness reaps a closed client only on its NEXT coordinator
pass, and each pass ACCEPTS FIRST (``firmware/coordinator/coordinator_net.c``, bare metal and
harnessd alike: ``if (s_conn == 0) adopt; else close(incoming)``, then it reads the old
client's EOF). So a connect that lands after our close but before that pass is turned away,
closed unanswered (or reset, once a line was sent), even from the same process: typically a
3-10 ms window on the MBV, longer during a swap or a CLCD repaint. Through an SSH forward (the
hub tunnel, a probe's tunnel, the claim's board-SSH forward) the window grows: ssh can
forward the next channel's open before the old channel's EOF. On silicon (2026-09-28) it
refused the deploy reset guard's read straight after the preflight, every time. Inside the
gate, and only right after OUR OWN previous connection to the board closed (within
``REAP_WINDOW_S``):

- pace: a new connection waits until ``PACE_S`` (``PACE_FORWARD_S`` through an SSH forward)
  has passed since that close, so the board has usually reaped it;
- retry: one turned away at the connect or first exchange (no reply line; not EBUSY, not a
  failed swap settling) is our own ghost and is tried again, ``REAP_RETRY_S`` (20 x 50 ms,
  about 1 s), as pyverify's ``_shell_call_retrying`` and ``slot._one_line`` do. Nothing that
  was answered is ever sent again: the harness closes a turned-away client unread.

Only a refusal that outlasts that, or one with no recent close of ours, counts as contention
(another client). Refused connections never count as a close of ours, so a real other client
holding the port costs one retry burst at most, then QUIET-POLL backs off as before.

``set_enabled(False)`` turns the gate, the pace and the retry off: the tests' negative twins.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from harness_manager.core.errors import HeldError

log = logging.getLogger(__name__)

#: How long a request waits for this process's own request in flight on the same board.
GATE_WAIT_S = 10.0
#: The least time between our own close and our next connect to the same board (direct).
PACE_S = 0.02
#: The same through an SSH forward (its close lags further).
PACE_FORWARD_S = 0.05
#: A connection turned away this soon after our own previous one closed may be that one,
#: not yet reaped by the board (module docstring): it is retried.
REAP_WINDOW_S = 2.0
#: The pauses before each retry: 20 x 50 ms (pyverify's ``_REFUSED_ATTEMPTS``/``_GAP_S``).
REAP_RETRY_S: tuple[float, ...] = (0.05,) * 20

OWN_REQUEST = "OWN_REQUEST"
OWN_HOLDER = "harness-manager (this process)"


class OwnRequestBusyError(HeldError):
    """The board's control port is busy with THIS Harness Manager's own request: the wait for
    it ran out. Never "another client" (``services.quiet.is_contention`` skips it)."""

    own_request = True

    def __init__(self, message: str, *, key: str, hint: str = "") -> None:
        super().__init__(message, holder=OWN_HOLDER, hint=hint)
        self.data = {"reason": OWN_REQUEST, "board": key}


def is_own_request(exc: BaseException | None) -> bool:
    """``exc`` is a wait on our own gate that ran out (not a board's refusal)."""
    return bool(getattr(exc, "own_request", False))


@dataclass
class GateStats:
    served: int = 0          # slots handed out (outermost only)
    waited: int = 0          # of those, how many had to queue
    max_wait_s: float = 0.0
    timeouts: int = 0        # waits that ran out (OwnRequestBusyError)
    nested: int = 0          # a holder asked again on the same thread
    reaped: int = 0          # retries of a connection turned away unanswered (our own ghost)


class _Waiter:
    __slots__ = ("thread", "event")

    def __init__(self, thread: int) -> None:
        self.thread = thread
        self.event = threading.Event()


class ControlGate:
    """One board's control port, in this process: FIFO, bounded, re-entrant (module docstring)."""

    def __init__(self, key: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.key = key
        self.clock = clock
        self.stats = GateStats()
        self._mu = threading.Lock()
        self._owner: int | None = None
        self._owner_name = ""
        self._depth = 0
        self._since = 0.0
        self._what = ""
        self._queue: deque[_Waiter] = deque()
        self._last_close: float | None = None

    # -- state ----------------------------------------------------------------------------

    @property
    def held(self) -> bool:
        with self._mu:
            return self._owner is not None

    def waiting(self) -> int:
        with self._mu:
            return len(self._queue)

    def holder_text(self) -> str:
        with self._mu:
            if self._owner is None:
                return ""
            age = max(0.0, self.clock() - self._since)
            what = f"{self._what} " if self._what else ""
            return f"{what}on thread {self._owner_name}, {age:.1f} s"

    # -- the slot ---------------------------------------------------------------------------

    def acquire(self, wait_s: float | None = None, what: str = "") -> None:
        """Take the gate (re-entrant). ``OwnRequestBusyError`` after ``wait_s`` (default
        ``GATE_WAIT_S``)."""
        me = threading.get_ident()
        limit = GATE_WAIT_S if wait_s is None else max(0.0, float(wait_s))
        with self._mu:
            if self._owner == me:
                self._depth += 1
                self.stats.nested += 1
                nested = True
            elif self._owner is None and not self._queue:
                self._take(me, what)
                return
            else:
                nested = False
                waiter = _Waiter(me)
                self._queue.append(waiter)
                started = self.clock()
        if nested:
            log.warning("a control connection to %s was opened while this thread already "
                        "holds one: the single-client port turns the inner one away", self.key)
            return
        waiter.event.wait(limit)
        with self._mu:
            if self._owner == me:                         # handed over (maybe as time ran out)
                waited = self.clock() - started
                self.stats.waited += 1
                self.stats.max_wait_s = max(self.stats.max_wait_s, waited)
                self._owner_name = threading.current_thread().name
                self._what = what
                return
            self._queue.remove(waiter)
            self.stats.timeouts += 1
            holder = self._holder_locked()
        raise OwnRequestBusyError(
            f"the control port of {self.key} is busy with this Harness Manager's own request "
            f"({holder or 'another request'}): it serves one connection at a time, and this "
            f"request waited {limit:.0f} s", key=self.key,
            hint="not another client: retry when that request ends (a deploy's swap or a card "
                 "commit holds the port for their whole run)")

    def _take(self, me: int, what: str) -> None:
        self._owner = me
        self._owner_name = threading.current_thread().name
        self._depth = 1
        self._since = self.clock()
        self._what = what
        self.stats.served += 1

    def _holder_locked(self) -> str:
        if self._owner is None:
            return ""
        age = max(0.0, self.clock() - self._since)
        what = f"{self._what}, " if self._what else ""
        return f"{what}{age:.1f} s so far"

    def release(self) -> None:
        with self._mu:
            if self._owner != threading.get_ident():
                raise RuntimeError(f"the control gate of {self.key} is not held by this thread")
            self._depth -= 1
            if self._depth > 0:
                return
            if self._queue:                               # FIFO: hand it to the first waiter
                nxt = self._queue.popleft()
                self._owner = nxt.thread
                self._owner_name = ""
                self._depth = 1
                self._since = self.clock()
                self._what = ""
                self.stats.served += 1
                nxt.event.set()
                return
            self._owner = None
            self._owner_name = ""
            self._what = ""

    @contextlib.contextmanager
    def slot(self, what: str = "", wait_s: float | None = None) -> Iterator[ControlGate]:
        """Hold the gate for one connection (``with gate.slot(): connect, ask, close``)."""
        if not _enabled:
            yield self
            return
        self.acquire(wait_s, what)
        try:
            yield self
        finally:
            self.release()

    # -- our own ghost (module docstring) ---------------------------------------------------

    def note_close(self) -> None:
        """A connection the board adopted (it answered) was just closed by us."""
        with self._mu:
            self._last_close = self.clock()

    def recently_closed(self, window_s: float | None = None) -> bool:
        """Our own previous connection closed less than ``window_s`` (``REAP_WINDOW_S``) ago
        (gate on only)."""
        if not _enabled:
            return False
        with self._mu:
            last = self._last_close
        window = REAP_WINDOW_S if window_s is None else window_s
        return last is not None and self.clock() - last < window

    def pace(self, lagging: bool = False, gap_s: float | None = None,
             sleep: Callable[[float], None] = time.sleep) -> float:
        """Wait until ``gap_s`` (``PACE_S``; ``PACE_FORWARD_S`` through an SSH forward,
        ``lagging``) has passed since our own previous close. Returns the time waited."""
        if not _enabled:
            return 0.0
        if gap_s is None:
            gap_s = PACE_FORWARD_S if lagging else PACE_S
        with self._mu:
            last = self._last_close
        if last is None:
            return 0.0
        left = gap_s - (self.clock() - last)
        if left <= 0.0:
            return 0.0
        sleep(left)
        return left

    def reap_delays(self) -> tuple[float, ...]:
        """Right after our own close (``REAP_WINDOW_S``): the pauses before each retry of a
        connection turned away unanswered (``REAP_RETRY_S``); else none."""
        return REAP_RETRY_S if self.recently_closed() else ()


# --- the registry -------------------------------------------------------------------------------

_gates: dict[str, ControlGate] = {}
_gates_mu = threading.Lock()
_enabled = True


def key_for(host: str, port: int | str) -> str:
    """A board's gate key: its own control address, ``host:port`` (brackets and case dropped)."""
    h = str(host or "").strip().strip("[]").lower()
    return f"{h}:{int(port)}"


def key_of_address(address: str, default_port: int) -> str:
    """``key_for`` a link address (``host``, ``host:port``, ``[v6]:port``)."""
    from .shell import parse_endpoint

    host, port = parse_endpoint(address, default_port)
    return key_for(host, port)


def gate_for(key: str) -> ControlGate:
    with _gates_mu:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates[key] = ControlGate(key)
        return gate


def enabled() -> bool:
    return _enabled


def set_enabled(on: bool) -> bool:
    """Turn every gate (and the reap) on or off; returns the previous setting. A test seam
    for the negative twins: production never turns it off."""
    global _enabled
    previous, _enabled = _enabled, bool(on)
    return previous


def forget(key: str | None = None) -> None:
    """Drop a gate that nobody holds (None: every idle gate). Tests use it between cases."""
    with _gates_mu:
        keys = [key] if key is not None else list(_gates)
        for k in keys:
            gate = _gates.get(k)
            if gate is not None and not gate.held and not gate.waiting():
                del _gates[k]


def gate_of(shell: object) -> ControlGate | None:
    """The gate of any object with a ``gate_key`` (an ``Mps3Shell``), else None."""
    key = getattr(shell, "gate_key", None)
    return gate_for(key) if isinstance(key, str) and key else None


@contextlib.contextmanager
def held(shell: object, what: str = "", wait_s: float | None = None, *,
         lagging: bool | None = None) -> Iterator[ControlGate | None]:
    """Hold ``shell``'s board gate for a connection this module does not open itself (the
    deploy's parked swap, pyverify's ``slot`` requests), paced when it goes through an SSH
    forward (``lagging``; default: the shell's own ``lagging_close``). A shell without a
    gate: nothing."""
    gate = gate_of(shell)
    if gate is None:
        yield None
        return
    if wait_s is None:
        wait_s = getattr(shell, "gate_wait_s", None)
    if lagging is None:
        lagging = bool(getattr(shell, "lagging_close", False))
    with gate.slot(what, wait_s=wait_s):
        gate.pace(lagging)
        yield gate
