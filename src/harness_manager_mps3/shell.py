"""The MPS3 shell over Ethernet, as seen through pyverify.

This module turns pyverify replies into board-agnostic models and pyverify or
socket failures into ``HarnessError``s with exit codes. The control port
accepts ONE client, so every call opens, asks and closes. Never hold 6900 open
(docs: harness handover §5; fpgahub's poller does the same).

Two harness engines answer the same wire (Linux harness plan §10: the same
firmware service modules run bare-metal and under ``mps3-harnessd``). What the
host sees, and what it means:

| On 6900 | Meaning | Error (exit) | ``Health.control_channel`` |
|---|---|---|---|
| connect refused (RST) | nothing listens: Linux kernel up and harnessd dead or respawning; or bare-metal lwIP up with no listener | ``ShellRefusedError`` (7) | ``offline``, notes from the diagnosis |
| connect times out | no board, a dead CPU, or stage0 rescue (no TCP) | ``ShellSilentError`` (7) | ``offline``, or ``rescue`` when identify says so |
| connected, never a reply | the service loop is hung (a hung harnessd's kernel still accepts) | ``ShellWedgedError`` (7) | ``wedged`` |
| accept then EOF, or RST after accept | another client holds 6900 (single client) | ``HeldError`` (4) | ``busy`` |
| ``{"ok":false,"err":"EBUSY"}`` (A3) | the same, said explicitly | ``HeldError`` (4) | ``busy`` |
| refused, or accept then EOF/RST, within 35 s of a push this process ran that failed; or EBUSY with a ``swap`` key | the harness is finishing a failed swap | ``SwapSettlingError`` (4) | ``busy`` |

After a failed push the harness keeps the swap parked until its idle timeout
(``MPS3_SWAP_AWAIT_IDLE_MS``, 30 s) fails it, and until then it turns every new
6900 client away (B1 v4, 2026-09-25). ``Mps3Deploy`` records each push that
failed with the swap still parked (``note_failed_push``); for
``FAILED_PUSH_WINDOW_S`` after it, a turned-away connect to that endpoint is
``SwapSettlingError``, never "offline" or "another client". Outside the window
nothing changes.

The diagnosis after a refused or silent connect asks, cheapest first, whether
anything else on the board answers: UDP identify (6899; ``mode:"rescue"`` means
stage0 rescue), then for non-loopback hosts TCP 22 (the Linux OS) and ICMP.

Reading keys pyverify does not model. pyverify's ``VersionResponse`` (the
installed one) drops ``impl``, ``proto``, ``usercode`` and ``clr_max``, and its
``DiagResponse`` turns an ABSENT key into 0. Every control connection here goes
through ``_TapTransport``, which keeps the last reply line pyverify parsed, so
those additive keys are read from the same line pyverify decoded, never from a
second hand-rolled request. PYVERIFY REQUEST (HOST lane, in progress):
``VersionResponse.impl``/``extra`` and ``DiagResponse.present``; once they land
the tap is only needed for the EBUSY line.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pyverify import client as _pv_client
from pyverify import rm_id as rmid
from pyverify.client import ShellClient, ShellProtocolError, SocketTransport

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity, Check, Health

from . import ctlgate
from . import identify as _identify
from .capabilities import HARNESS_STATES
from .constants import (
    IMPL_BARE_METAL,
    IMPL_LINUX,
    KNOWN_DESIGNS,
    SSH_PORT,
)

T = TypeVar("T")

#: pyverify's HOST lane adds ``ShellChannelClosed(ConnectionError)`` with the same
#: "closed by peer" text; use it when present, match the text otherwise.
_CHANNEL_CLOSED: type[BaseException] | None = getattr(_pv_client, "ShellChannelClosed", None)


def resolve_rm_name(rm_id: str) -> str:
    """Design name from the overlay manifests (T2), falling back to KNOWN_DESIGNS."""
    try:
        from .overlays import resolve_rm_name as from_manifests
    except ImportError:
        return KNOWN_DESIGNS.get(rmid.design_id(rm_id), "")
    return from_manifests(rm_id)


_resolve_rm_name = resolve_rm_name     # the Wave-1 private name; kept for importers


def parse_endpoint(spec: str, default_port: int) -> tuple[str, int]:
    """'host' | 'host:port' -> (host, port). IPv6 literals need brackets."""
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, int(port)
    return spec, default_port


def _hex_id(value: Any) -> str:
    """A u32 id as ``"0x"`` + 8 lowercase hex, whatever form it arrived in; else ``""``."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value & 0xFFFFFFFF:08x}"
    try:
        return f"0x{int(str(value), 0) & 0xFFFFFFFF:08x}"
    except ValueError:
        return str(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- errors --------------------------------------------------------------------------


class ShellRefusedError(UnreachableError):
    """The control port refused the connection: nothing listens on 6900."""


class ShellSilentError(UnreachableError):
    """The connect to 6900 timed out: nothing answered at all."""


class ShellWedgedError(UnreachableError):
    """6900 accepted the connection and never replied: the service loop is hung."""


class ShellRescueError(UnreachableError):
    """No control channel because stage0 is in RESCUE (it said so over identify).

    ``identity`` is what stage0 reported (fabric ``shell_id``, no harness), so a
    caller that wants to show the board anyway can; ``reply`` is the identify reply.
    """

    def __init__(self, message: str, *, hint: str = "", identity: BoardIdentity | None = None,
                 reply: Any = None) -> None:
        super().__init__(message, hint=hint)
        self.identity = identity
        self.reply = reply


class SwapSettlingError(HeldError):
    """6900 turned the connection away while the harness finishes a failed swap.

    ``remaining_s`` is how long, at most, until it takes clients again (0.0 when the
    harness said so itself and gave no bound).
    """

    def __init__(self, message: str, *, hint: str = "", holder: str = "",
                 remaining_s: float = 0.0) -> None:
        super().__init__(message, holder=holder, hint=hint)
        self.remaining_s = remaining_s


class _ShellBusy(Exception):
    """The shell answered ``{"ok":false,"err":"EBUSY",...}`` (harness handover A3)."""

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__(f"EBUSY: {reply}")
        self.reply = reply


# --- failed pushes ---------------------------------------------------------------------

#: The harness's swap idle timeout (swap_fsm.h ``MPS3_SWAP_AWAIT_IDLE_MS``): a parked
#: swap that sees no push for this long fails, and 6900 serves new clients again.
SWAP_AWAIT_IDLE_S = 30.0
#: How long after a failed push a turned-away 6900 connect means "settling": the idle
#: timeout plus a margin for the harness's poll loop and the host's own close.
FAILED_PUSH_WINDOW_S = 35.0

#: ``(host, port)`` of a control channel -> ``clock()`` when a push to it failed with
#: the swap still parked. Process-wide: the daemon opens more than one ``Mps3Shell``
#: for one board (a session, a probe), and each must see the same failure.
_failed_pushes: dict[tuple[str, int], float] = {}
_failed_pushes_lock = threading.Lock()
#: The clock the window runs on (tests replace it).
clock: Callable[[], float] = time.monotonic


def note_failed_push(host: str, port: int) -> None:
    """Record that a push to ``host:port``'s harness failed with its swap still parked."""
    with _failed_pushes_lock:
        _failed_pushes[(host, port)] = clock()


def clear_failed_push(host: str, port: int) -> None:
    """Forget it (the harness answered again, or a later swap settled)."""
    with _failed_pushes_lock:
        _failed_pushes.pop((host, port), None)


def settling_remaining_s(host: str, port: int) -> float:
    """Seconds left in the window after a failed push to ``host:port``; 0.0 outside it."""
    with _failed_pushes_lock:
        at = _failed_pushes.get((host, port))
        if at is None:
            return 0.0
        left = FAILED_PUSH_WINDOW_S - (clock() - at)
        if left <= 0.0:
            del _failed_pushes[(host, port)]
            return 0.0
        return left


def _is_ebusy(exc: HeldError) -> bool:
    """The harness said EBUSY itself (another client, named): never reclassified."""
    return bool(getattr(exc, "ebusy", False))


def _turned_away_at_once(exc: HeldError, taps: Sequence[Any]) -> bool:
    """The board accepted and closed (or reset) the connection before any reply line: the
    single-client refusal, not EBUSY, a settling swap, or our own gate's wait. A reset that
    lands during the connect itself (``at_connect``, no tap yet) is the same refusal."""
    if _is_ebusy(exc) or isinstance(exc, SwapSettlingError) or ctlgate.is_own_request(exc):
        return False
    if getattr(exc, "at_connect", False):
        return True
    return bool(taps) and not any(getattr(t, "replies", 0) for t in taps)


def _says_swap_settling(reply: dict[str, Any]) -> bool:
    """An EBUSY line that names a swap in progress (an additive ``swap`` key, the name
    ``stats`` uses, with any state but ``idle``). No harness sends it yet."""
    swap = reply.get("swap")
    return isinstance(swap, str) and swap.strip() not in ("", "idle")


# --- the tap -------------------------------------------------------------------------


def _json_obj(line: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


class _TapTransport:
    """A pyverify ``Transport`` that keeps each reply line pyverify is about to parse.

    pyverify still does all the decoding; this only lets the pack read keys the
    installed pyverify does not model (``impl``, ``proto``, ``usercode``,
    ``clr_max``, the diag keys actually present), and turns the A3 busy line into
    ``HeldError`` before pyverify reads it as a failed ``ping``.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last: dict[str, Any] = {}
        #: Reply lines read on this connection (SERIAL-6900: 0 = turned away before any reply).
        self.replies = 0

    def send_line(self, payload: bytes) -> None:
        self._inner.send_line(payload)

    def recv_line(self) -> bytes:
        line = self._inner.recv_line()
        self.replies += 1
        self.last = _json_obj(line)
        if self.last.get("ok") is False and str(self.last.get("err", "")).strip().upper() == "EBUSY":
            raise _ShellBusy(self.last)
        return line

    def close(self) -> None:
        self._inner.close()


# --- one live read -------------------------------------------------------------------


@dataclass(frozen=True)
class ShellLive:
    """``ping`` + ``version`` from ONE 6900 connection, with the raw ``version`` line."""

    shell_id: str
    rm_id: str
    version: Any = None                      # pyverify VersionResponse, or None
    raw_version: dict[str, Any] = field(default_factory=dict)
    version_busy: bool = False               # `version` answered EBUSY after `ping` worked

    @property
    def version_ok(self) -> bool:
        return self.version is not None and bool(self.version.ok)

    @property
    def features(self) -> tuple[str, ...]:
        return tuple(self.version.features) if self.version_ok else ()

    @property
    def impl(self) -> str:
        """``"linux"`` | ``"bare-metal"`` (``impl`` absent) | ``""`` (no ``version`` verb)."""
        if not self.version_ok:
            return ""
        impl = self.raw_version.get("impl", getattr(self.version, "impl", None))
        return impl if isinstance(impl, str) and impl else IMPL_BARE_METAL

    @property
    def proto(self) -> str:
        p = self.raw_version.get("proto") if self.version_ok else None
        return str(p) if p is not None and not isinstance(p, (bool, dict, list)) else ""

    @property
    def usercode(self) -> str:
        return _hex_id(self.raw_version.get("usercode")) if self.version_ok else ""

    @property
    def ver32(self) -> str:
        """``version.ver32`` (the packed HARNESS_VER32) as ``0x`` + 8 hex; "" when absent or 0
        (0 is "not stamped": no firmware with a ``version`` verb packs 0.0.0)."""
        v = _hex_id(self.raw_version.get("ver32")) if self.version_ok else ""
        return "" if not v.startswith("0x") or int(v, 16) == 0 else v

    @property
    def name(self) -> str:
        """The board's own name, when the harness sends the (proposed, N1) ``name`` key."""
        from harness_manager.naming import clean_name

        return clean_name(self.raw_version.get("name")) if self.version_ok else ""

    @property
    def lmb_kb(self) -> int:
        return int(self.version.lmb_kb) if self.version_ok else 0

    @property
    def clr_max(self) -> int | None:
        """The largest clearing this harness can hold (additive ``clr_max``), if it says."""
        v = self.raw_version.get("clr_max") if self.version_ok else None
        return v if _is_int(v) and v > 0 else None

    def identity(self) -> BoardIdentity:
        verdict = self.version.skew_verdict if self.version_ok else "unchecked"
        ver = self.version if self.version_ok else None
        return BoardIdentity(
            board_type="mps3",
            shell_id=self.shell_id,
            rm_id=self.rm_id,
            rm_name=resolve_rm_name(self.rm_id) if self.rm_id else "",
            harness_version=ver.harness if ver else "",
            firmware_sha=ver.sha if ver else "",
            firmware_dirty=bool(ver.dirty) if ver else False,
            features=self.features,
            build_check={"ok": Check.OK, "SKEW": Check.MISMATCH}.get(verdict, Check.UNCHECKED),
            harness_impl=self.impl,
            proto=self.proto,
            usercode=self.usercode,
            name=self.name,
            ver32=self.ver32,
        )


# --- evidence beyond 6900 ------------------------------------------------------------


def icmp_echo(host: str, timeout: float = 1.0) -> bool:
    """One ICMP echo through the system ``ping`` (no raw sockets needed). False when
    there is no ``ping`` binary or it fails in any way."""
    ping = shutil.which("ping")
    if ping is None:
        return False
    secs = str(max(1, round(timeout)))
    if sys.platform.startswith("win"):
        args = [ping, "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif sys.platform == "darwin":
        args = [ping, "-c", "1", "-t", secs, host]
    else:
        args = [ping, "-c", "1", "-W", secs, host]
    try:
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout + 2.0, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def identify_quietly(host: str, timeout: float = 0.5) -> _identify.IdentifyReply | None:
    try:
        reply = _identify.identify(host, timeout=timeout, retries=1)
    except HarnessError:
        return None
    return reply if reply.ok else None


@dataclass(frozen=True)
class ShellProbes:
    """How the diagnosis gathers evidence. Tests inject fakes; nothing here is required."""

    identify: Callable[[str, float], Any] = identify_quietly
    tcp: Callable[[str, int, float], bool] = tcp_open
    icmp: Callable[[str, float], bool] = icmp_echo


DEFAULT_PROBES = ShellProbes()


@dataclass(frozen=True)
class Diagnosis:
    """Why the control channel does not answer, from the evidence that was cheap to get."""

    state: str                       # "offline" | "service_down" | "rescue" | "wedged"
    notes: tuple[str, ...]
    reply: Any = None                # the IdentifyReply, when identify answered

    def health(self) -> Health:
        channel = {"service_down": "offline"}.get(self.state, self.state)
        # "reachable" = something on the board answered us (TCP accept, identify, SSH, ICMP).
        return Health(reachable=self.state != "offline", control_channel=channel,
                      notes=self.notes)

    @property
    def hint(self) -> str:
        return " ".join(self.notes)


# --- the shell -----------------------------------------------------------------------


#: ``Mps3Shell.preamble``: run on a connection before the caller's own requests.
Preamble = Callable[[ShellClient, _TapTransport], None]


class Mps3Shell:
    def __init__(self, host: str, port: int, *, timeout: float = 3.0,
                 probes: ShellProbes | None = None, gate_key: str = "") -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.probes = probes or DEFAULT_PROBES
        #: SERIAL-6900: the board's control-port gate (``ctlgate``): one connection at a time
        #: per board in this process. The key is the BOARD's own control address, so a shell
        #: on a tunnel's local port (or a claim forward) shares its board's gate; by default
        #: this shell's own address.
        self.gate_key = gate_key or ctlgate.key_for(host, port)
        #: How long a call waits for this process's own request in flight on the board
        #: (None: ``ctlgate.GATE_WAIT_S``).
        self.gate_wait_s: float | None = None
        #: This shell reaches the board through an SSH forward (the hub tunnel, a probe's
        #: tunnel, a claim forward): our close reaches the board later, so back-to-back
        #: calls are paced longer (``ctlgate.PACE_FORWARD_S``). The pack sets it.
        self.lagging_close = False
        #: CCR PANEL-3: when set, ``call_raw`` runs ``preamble(client, tap)`` on every
        #: connection it opens, before ``fn``, inside the same error mapping. The panel
        #: adapter sets it to send a pending ``hello`` first (``Mps3Panel.ride``); it sends
        #: nothing unless a hello is waiting. None: connections carry only ``fn``'s requests.
        self.preamble: Preamble | None = None
        #: QUIET-POLL: when set, ``observer(None)`` after each call the board answered and
        #: ``observer(exc)`` after each one that failed (refused, reset, timed out, held...),
        #: for every caller. The daemon sets it to its background gate
        #: (``services.quiet.BackgroundGate.observe``), so a refusal anyone meets backs off
        #: the background polls. It never changes the call's result or error.
        self.observer: Callable[[BaseException | None], None] | None = None

    # -- plumbing ---------------------------------------------------------------

    def _connect(self) -> _TapTransport:
        where = f"{self.host}:{self.port}"
        try:
            inner = SocketTransport(self.host, self.port, self.timeout)
        except ConnectionRefusedError as exc:
            raise ShellRefusedError(
                f"shell at {where} refused the connection",
                hint="nothing listens on the control port: is the board powered and the "
                     "harness running?") from exc
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            # FIX-PACK-1: the board ACCEPTED the connect and reset it before the connect
            # call returned (the RST beat the socket's own SO_ERROR check; Windows reports
            # WSAECONNABORTED). That is the single-client turn-away seen one step earlier,
            # never "unreachable": HELD, and our own ghost's retry covers it
            # (``_turned_away_at_once``). Before the fix it answered 502 UNREACHABLE about
            # one explicit read in three while another client held the port.
            err = HeldError(
                f"shell at {where} reset the connection",
                hint="another client probably holds the control port, or the board is restarting")
            err.at_connect = True                    # turned away before any exchange
            raise err from exc
        except TimeoutError as exc:
            raise ShellSilentError(
                f"shell at {where} did not answer within {self.timeout}s",
                hint="check the Ethernet link and the board's IP") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot reach {where}: {exc}") from exc
        return _TapTransport(inner)

    def call_raw(self, fn: Callable[[ShellClient, _TapTransport], T]) -> T:
        """Open 6900, run ``fn(client, tap)``, close. Maps failures to exit-coded errors.

        ``tap.last`` is the reply line pyverify parsed last, as a dict: read keys the
        installed pyverify does not model from it, never send a hand-rolled request.
        ``self.preamble``, when set, runs first on the same connection (CCR PANEL-3).
        ``self.observer``, when set, hears the outcome (QUIET-POLL).

        SERIAL-6900: the whole open/ask/close holds the board's control gate (``gate()``),
        so this process never races itself for the single-client port; a wait for our own
        request that runs out is ``ctlgate.OwnRequestBusyError`` (never "another client").
        """
        try:
            result = self._gated_call(fn)
        except BaseException as exc:
            self._observe(exc)
            raise
        self._observe(None)
        return result

    def gate(self) -> ctlgate.ControlGate:
        """This board's control-port gate (SERIAL-6900)."""
        return ctlgate.gate_for(self.gate_key)

    def _gated_call(self, fn: Callable[[ShellClient, _TapTransport], T]) -> T:
        gate = self.gate()
        with gate.slot(f"{self.host}:{self.port}", wait_s=self.gate_wait_s):
            gate.pace(self.lagging_close)
            delays = iter(gate.reap_delays())
            while True:
                taps: list[_TapTransport] = []
                try:
                    return self._settled_call(fn, taps)
                except HeldError as exc:
                    # Turned away before any reply, right after our own previous connection
                    # closed: the board has not reaped that one yet (ctlgate, "our own
                    # ghost"). Try again (20 x 50 ms); after that it is someone else.
                    if not _turned_away_at_once(exc, taps):
                        raise
                    delay = next(delays, None)
                    if delay is None:
                        raise
                    gate.stats.reaped += 1
                    time.sleep(delay)
                finally:
                    if any(t.replies for t in taps):
                        gate.note_close()

    def _observe(self, exc: BaseException | None) -> None:
        observer = self.observer
        if observer is None:
            return
        try:
            observer(exc)
        except Exception:  # noqa: BLE001 - the observer's bug is never the caller's error
            logging.getLogger(__name__).exception("the control-port observer failed")

    def _settled_call(self, fn: Callable[[ShellClient, _TapTransport], T],
                      taps: list[_TapTransport] | None = None) -> T:
        try:
            result = self._call_raw(fn, taps)
        except SwapSettlingError:
            raise
        except (ShellRefusedError, HeldError) as exc:
            left = settling_remaining_s(self.host, self.port)
            if left <= 0.0 or (isinstance(exc, HeldError) and _is_ebusy(exc)):
                raise
            raise SwapSettlingError(
                f"shell at {self.host}:{self.port} turned the connection away: the harness "
                "is finishing a failed swap", hint=HARNESS_STATES["harness.swap_settling"],
                remaining_s=left) from exc
        clear_failed_push(self.host, self.port)
        return result

    def _call_raw(self, fn: Callable[[ShellClient, _TapTransport], T],
                  taps: list[_TapTransport] | None = None) -> T:
        where = f"{self.host}:{self.port}"
        preamble = self.preamble
        tap = self._connect()
        if taps is not None:
            taps.append(tap)
        try:
            with ShellClient(self.host, self.port, timeout=self.timeout, transport=tap) as client:
                if preamble is not None:
                    preamble(client, tap)
                return fn(client, tap)
        except _ShellBusy as exc:
            holder = str(exc.reply.get("holder") or exc.reply.get("peer") or "")
            if _says_swap_settling(exc.reply):
                raise SwapSettlingError(
                    f"shell at {where} is busy (EBUSY): the harness is finishing a swap",
                    holder=holder, hint=HARNESS_STATES["harness.swap_settling"]) from exc
            err = HeldError(f"shell at {where} is busy (EBUSY): another client holds the "
                            "control port", holder=holder, hint=HARNESS_STATES["harness.busy"])
            err.ebusy = True                         # said explicitly: not a refusal
            raise err from exc
        except TimeoutError as exc:
            raise ShellWedgedError(
                f"shell at {where} accepted the connection but did not reply within "
                f"{self.timeout}s", hint=HARNESS_STATES["harness.wedged"]) from exc
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            # lwIP (bare-metal) or the kernel (Linux) may RST the extra client instead
            # of closing it; a board restarting mid-request looks the same. Windows
            # reports the same accept-then-close as WSAECONNABORTED (WinError 10053).
            raise HeldError(
                f"shell at {where} reset the connection",
                hint="another client probably holds the control port, or the board is restarting",
            ) from exc
        except ConnectionError as exc:
            # Accept-then-EOF is how the shell turns away a second client (one client at a
            # time). pyverify raises ConnectionError("... closed by peer"), or its
            # ShellChannelClosed subclass once the HOST lane lands.
            if (_CHANNEL_CLOSED is not None and isinstance(exc, _CHANNEL_CLOSED)) \
                    or "closed by peer" in str(exc):
                raise HeldError(
                    f"shell at {where} closed the connection",
                    hint="another client probably holds the control port (one client at a time)",
                ) from exc
            raise UnreachableError(f"cannot reach {where}: {exc}") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot reach {where}: {exc}") from exc
        except ShellProtocolError as exc:
            if "EOF" in str(exc) or "closed" in str(exc).lower():
                raise HeldError(
                    f"shell at {where} closed the connection",
                    hint="another client probably holds the control port (one client at a time)",
                ) from exc
            raise ActionFailedError(f"shell protocol error: {exc}") from exc

    def call(self, fn: Callable[[ShellClient], T]) -> T:
        """Open 6900, run ``fn``, close. Maps failures to exit-coded errors."""
        return self.call_raw(lambda client, _tap: fn(client))

    def note_failed_push(self) -> None:
        """A push this process ran failed with the swap still parked: for
        ``FAILED_PUSH_WINDOW_S`` a turned-away connect is ``SwapSettlingError``."""
        note_failed_push(self.host, self.port)

    def settling_remaining_s(self) -> float:
        """Seconds left in that window; 0.0 when there is none."""
        return settling_remaining_s(self.host, self.port)

    # -- identity -----------------------------------------------------------------

    def live(self) -> ShellLive:
        """``ping`` then ``version`` on one connection, keeping the raw ``version`` line.

        A harness without ``version`` (net-protocol < v0.8) answers "unknown op": that
        is an older harness, not a fault (``version_ok`` False, ``impl`` ""). A
        ``version`` that answers EBUSY after a good ``ping`` still yields the ping.
        """
        def ask(c: ShellClient, tap: _TapTransport) -> ShellLive:
            ping = c.ping()
            if not ping.ok:
                err = tap.last.get("err", "")
                raise ActionFailedError("shell answered ping with ok:false"
                                        + (f" ({err})" if err else ""))
            try:
                ver = c.version()
            except _ShellBusy:
                return ShellLive(ping.shell_id, ping.rm_id, version_busy=True)
            return ShellLive(ping.shell_id, ping.rm_id, version=ver, raw_version=dict(tap.last))

        return self.call_raw(ask)

    def identity(self) -> BoardIdentity:
        """What the harness says it is.

        - Held (another client, or EBUSY): the identity comes from UDP identify when
          the board answers it (identify is independent of 6900); else ``HeldError``.
        - Refused or silent: ``UnreachableError`` with the diagnosis as its hint; a
          board whose identify says ``mode:"rescue"`` raises ``ShellRescueError``
          carrying stage0's identity (``health()`` says "rescue"). It is an error, not
          an identity: there is no harness to identify, and a probe must not report
          a rescue board as having "answered ping".
        """
        try:
            return self.live().identity()
        except HeldError:
            reply = self.probes.identify(self.host, min(self.timeout, 0.5))
            if reply is not None and not reply.is_rescue:
                return reply.identity()
            raise
        except ShellWedgedError:
            raise
        except UnreachableError as exc:
            diagnosis = self.diagnose(exc)
            if diagnosis.reply is not None and diagnosis.reply.is_rescue:
                reason = diagnosis.reply.reason
                raise ShellRescueError(
                    f"the board at {self.host} is in stage0 RESCUE: no control channel"
                    + (f" ({reason})" if reason else ""),
                    hint=HARNESS_STATES["harness.rescue"],
                    identity=diagnosis.reply.identity(), reply=diagnosis.reply) from exc
            raise type(exc)(exc.message, hint=diagnosis.hint or exc.hint) from exc

    # -- health -------------------------------------------------------------------

    def diagnose(self, exc: UnreachableError) -> Diagnosis:
        """Why 6900 does not answer. Cheapest evidence first; never raises."""
        if isinstance(exc, ShellWedgedError):
            return Diagnosis("wedged", (HARNESS_STATES["harness.wedged"],))
        reply = self.probes.identify(self.host, min(self.timeout, 0.5))
        if reply is not None:
            if reply.is_rescue:
                notes = [HARNESS_STATES["harness.rescue"]]
                if reply.reason:
                    notes.append(f"stage0 says: {reply.reason}")
                return Diagnosis("rescue", tuple(notes), reply)
            return Diagnosis("service_down", (
                f"identify answers ({reply.impl}), the control port does not: "
                + HARNESS_STATES["harness.service_down"],), reply)
        # A loopback host is a test fake or a tunnel end: the local machine answering
        # TCP 22 or ICMP says nothing about the board.
        if not _identify.is_loopback(self.host):
            refused = isinstance(exc, ShellRefusedError)
            if refused and self.probes.tcp(self.host, SSH_PORT, 1.0):
                return Diagnosis("service_down", (
                    "the OS is up (SSH answers on 22) but nothing listens on the control "
                    "port: " + HARNESS_STATES["harness.service_down"],))
            if self.probes.icmp(self.host, 1.0):
                if refused:
                    return Diagnosis("service_down", (
                        "the board answers ICMP but refuses the control port: "
                        + HARNESS_STATES["harness.service_down"],))
                return Diagnosis("service_down", (
                    "the board answers ICMP but the control port does not accept: the "
                    "harness is starting, hung before its listener, or stage0 is in a "
                    "rescue that does not answer identify",))
        return Diagnosis("offline", (HARNESS_STATES["harness.offline"],))

    def health(self) -> Health:
        try:
            diag, raw = self.call_raw(lambda c, tap: (c.diag(), dict(tap.last)))
        except SwapSettlingError as exc:
            notes = [exc.hint]
            if exc.remaining_s > 0.0:
                notes.append(f"at most {exc.remaining_s:.0f} s more (the swap's idle timeout)")
            return Health(reachable=True, control_channel="busy", notes=tuple(notes))
        except HeldError as exc:
            if ctlgate.is_own_request(exc):
                # SERIAL-6900: our own request holds the port (a swap, a commit): busy, but
                # nobody else is on it.
                return Health(reachable=True, control_channel="busy", notes=(exc.message,))
            notes = [HARNESS_STATES["harness.busy"]]
            if exc.holder:
                notes.append(f"held by {exc.holder}")
            return Health(reachable=True, control_channel="busy", notes=tuple(notes))
        except UnreachableError as exc:
            return self.diagnose(exc).health()
        if not diag.ok:
            # A harness without `diag` (e.g. the July Linux v0.7 daemons) is a legitimate
            # older harness, not a fault: reachable, idle, just no counters.
            return Health(reachable=True, control_channel="idle",
                          notes=("this harness does not report diagnostic counters (no `diag` verb)",))
        # Only the keys the harness SENT. pyverify fills an absent key with 0, and
        # mps3-harnessd omits the lwIP/LAN9220 keys it has no source for: a 0 there
        # is not a measurement (TEAM_PLAN principle 7).
        counters = {k: int(v) for k, v in raw.items() if k != "ok" and _is_int(v)}
        notes = []
        if counters.get("svc_skipped"):
            notes.append("superloop skipped services (control channel may be starved)")
        return Health(reachable=True, control_channel="idle", counters=counters, notes=tuple(notes))

    # -- simple verbs -------------------------------------------------------------

    def reset(self, target: str) -> None:
        """``reset`` one target. A target this harness does not know is ``UsageError``."""
        ok, err = self.call_raw(lambda c, tap: (c.reset(target).ok, str(tap.last.get("err", ""))))
        if ok:
            return
        if any(e in err.lower() for e in _BAD_TARGET_ERRS):
            raise UsageError(f"this harness does not accept reset target {target!r} ({err})",
                             hint="list the targets with `reset --help`")
        raise ActionFailedError(f"shell refused reset target {target!r}"
                                + (f": {err}" if err else ""))

    def reset_dut(self) -> None:
        resp = self.call(lambda c: c.reset("dut"))
        if not resp.ok:
            raise ActionFailedError("shell refused reset target 'dut'")

    def restart_harness(self) -> int:
        """Restart a Linux harness WITHOUT reloading the FPGA: harnessd's ``reboot`` verb
        (net-protocol v0.11). Returns ``in_ms``, the upper bound on the restart; the
        connection drops, and ``stats().up_ms`` restarting is the witness (``os_slots``)."""
        resp = self.call(lambda c: c.reboot())
        if not resp.ok:
            raise ActionFailedError(f"the harness refused reboot: {resp.err or '?'}",
                                    hint="it needs the watchdog (the 'reboot' feature); the "
                                         "board REBOOT (MCC) reloads the FPGA instead")
        return int(resp.in_ms)


# --- reset targets, read from the harness ---------------------------------------------

#: Every reset target the MPS3 harness vocabulary defines (ARCHITECTURE_SPEC §5,
#: harness handover A6). v0.11 accepts only "dut" (coordinator.c
#: coordinator_handle_reset: anything else is "bad target").
RESET_VOCABULARY: tuple[str, ...] = ("dut", "rp", "dbg")

#: ``version.features`` names that would declare a target, should a harness report
#: them (A6 has not named them; these are the names harness-manager proposes).
RESET_TARGET_FEATURES: dict[str, str] = {"reset_rp": "rp", "reset_dbg": "dbg"}

#: How the two servers word "no such target": the firmware says "bad target",
#: pyverify's FakeShell "unknown reset target 'x'".
_BAD_TARGET_ERRS = ("bad target", "unknown reset target")


#: MCC-FIX: "Restart the shell" on a Linux harness is harnessd's ``reboot`` verb
#: (net-protocol v0.11; HARNESSD_CONTRACT "reboot": the watchdog, stage0, Linux again),
#: a restart with NO FPGA reload. Bare metal keeps its own answer to ``reset shell``.
SHELL_RESTART_TARGET = "shell"


def reset_targets_from(live: ShellLive) -> tuple[str, ...]:
    """The targets a harness declares: an additive ``version.reset_targets`` array
    when it sends one, else "dut" plus any target a feature name declares. A Linux
    harness adds "shell": its restart is the ``reboot`` verb (``ShellResets.reset``)."""
    declared = live.raw_version.get("reset_targets") if live.version_ok else None
    linux = [SHELL_RESTART_TARGET] if live.impl == IMPL_LINUX else []
    if isinstance(declared, list) and declared and all(isinstance(t, str) for t in declared):
        return tuple(dict.fromkeys([*declared, *linux]))
    targets = ["dut"]
    targets += [t for f, t in RESET_TARGET_FEATURES.items() if f in live.features]
    return tuple(dict.fromkeys([*targets, *linux]))


class ShellResets:
    """``ResetAdapter`` whose targets come from the harness, not a hard-coded ("dut",).

    No harness can be asked "which targets do you accept?" without resetting
    something (``reset`` has no dry run), so this reads what the harness DECLARES
    (``version.reset_targets`` or feature names) and LEARNS from the answers to
    real requests: a vocabulary target ("rp", "dbg") the harness did not declare
    is still sent when asked for; "ok" adds it, "bad target" removes it, and the
    refusal is a ``UsageError`` that lists what is accepted.
    """

    def __init__(self, shell: Mps3Shell, session: Any = None) -> None:
        self._shell = shell
        self._session = session              # for SLOT-TIMING's reset guard (MCC-FIX)
        self._impl = ""                      # the engine that declared the targets
        self._declared: tuple[str, ...] | None = None
        self._accepted: list[str] = []
        self._refused: set[str] = set()

    def refresh(self) -> None:
        """Forget what was read and learned (after a harness change or a reboot)."""
        self._declared = None
        self._impl = ""
        self._accepted.clear()
        self._refused.clear()

    def reset_targets(self) -> Sequence[str]:
        if self._declared is None:
            try:
                live = self._shell.live()
                self._declared = reset_targets_from(live)
                self._impl = live.impl
            except HarnessError:
                return tuple(dict.fromkeys(["dut", *self._accepted]))   # "dut" is always there
        return tuple(t for t in dict.fromkeys([*self._declared, *self._accepted])
                     if t not in self._refused)

    def reset(self, target: str) -> None:
        known = self.reset_targets()
        if target == SHELL_RESTART_TARGET and target in known and self._impl == IMPL_LINUX:
            from .mcc import guard_reset

            guard_reset(self._session, "ACTION_HARNESS_REBOOT")   # not mid card job (B2)
            self._shell.restart_harness()
            return
        if target not in known and (target not in RESET_VOCABULARY or target in self._refused):
            raise UsageError(f"reset target {target!r} is not supported by this harness",
                             hint="targets: " + ", ".join(known))
        try:
            self._shell.reset(target)
        except UsageError as exc:
            self._refused.add(target)
            if target in self._accepted:
                self._accepted.remove(target)
            raise UsageError(exc.message, hint="targets: " + ", ".join(self.reset_targets())) \
                from exc
        if target not in known:
            self._accepted.append(target)


def make_reset_adapter(session: Any) -> ShellResets | None:
    """The reset hook the lead can wire in ``pack.py`` (CCR T12-1). None without a shell."""
    shell = getattr(session, "shell", None)
    return ShellResets(shell, session) if shell is not None else None
