"""Who may type on a board's consoles (lane UI2-API-HUB, UI v2 gap G1b).

The harness console (the FPGA UART lane 2, tty_02: ``fpga_uart2``, alias ``shell``) is a ROOT
shell on the Linux harness. UI v2 shows it as "Harness console", and only the hub lease holder
types there. Every console row (``GET /boards/{bid}/consoles``) says so:

- ``role``: ``linux-root`` (lane 2 on a Linux harness), ``shell`` (lane 2 on bare metal: the
  shell's own console), ``dut`` (the design's UARTs over Ethernet: ``uart0``, ``uart1``,
  ``swo``), ``lane`` (another FPGA UART lane: what drives it depends on the design), ``mcc``
  (the board controller's console: only a scripted board lists one; Harness Manager never
  opens tty_00 as a console, MCC-FIX) or ``""`` (a name this service does not know);
- ``writable``: THIS Harness Manager's keystrokes reach the board;
- ``read_only_reason``: why not (``""`` when writable).

The rule, on a board behind a hub (a board without one has no lease: every console is
writable):

- ``linux-root``: only when the lease is held HERE (``services.lease.held_here``: this Harness
  Manager has the token; the page's ``holderOnly``). Nobody holding it, another session of your
  hub name, someone else, or a lease that could not be read: read-only;
- every other role: read-only while the lease is held and not here (someone else's, or your
  hub name's in another session); writable when it is free or yours (as today).

The console WebSocket drops what a read-only client types (``app.console``), says so once as
an ``{"error": HELD}`` frame, and sends ``{"input": {role, writable, read_only_reason}}``
when the socket opens read-only and whenever that changes. The lease is read from the lease
service's own view (no hub call when it is under a minute old; else one read, cached 10 s).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import HarnessError, HeldError
from harness_manager.services.lease import held_here

log = logging.getLogger(__name__)

ROLE_LINUX_ROOT, ROLE_SHELL, ROLE_DUT, ROLE_LANE, ROLE_MCC = (
    "linux-root", "shell", "dut", "lane", "mcc")
ROLES = (ROLE_LINUX_ROOT, ROLE_SHELL, ROLE_DUT, ROLE_LANE, ROLE_MCC, "")
#: The harness console's lane (tty_02) and its alias (``services.console.ALIASES``).
HARNESS_LANE, HARNESS_ALIAS = "fpga_uart2", "shell"
DUT_CONSOLES = ("uart0", "uart1", "swo")
#: How old a lease view may be before a console asks the hub again.
VIEW_MAX_AGE_S = 60.0


def role_of(key: str, name: str, impl: str) -> str:
    """A console's role from its broker key, the name asked and the harness implementation."""
    names = {key, name}
    if names & {HARNESS_LANE, HARNESS_ALIAS}:
        return ROLE_LINUX_ROOT if impl == "linux" else ROLE_SHELL
    if names & set(DUT_CONSOLES):
        return ROLE_DUT
    if any(n.startswith("fpga_uart") for n in names):
        return ROLE_LANE
    if "mcc" in names:
        return ROLE_MCC
    return ""


def harness_impl(d: Any, bid: str) -> str:
    """``linux`` / ``bare-metal`` / ``""``: the identity last read (never a board contact)."""
    ident = None
    last = getattr(d.engine, "last_identity", None)
    if callable(last):
        try:
            ident = last(bid)
        except HarnessError:
            ident = None
    if ident is None:
        try:
            session = d.engine.session(bid)
        except HarnessError:
            return ""
        ident = getattr(session.candidate, "identity", None)
        if ident is None:
            fn = getattr(session, "identity", None)
            try:
                ident = fn() if callable(fn) and getattr(d.engine, "fake_boards", False) else None
            except HarnessError:
                ident = None
    return str(getattr(ident, "harness_impl", "") or "")


def lease_of(d: Any, bid: str) -> tuple[bool, dict[str, Any] | None, str]:
    """``(behind_a_hub, lease or None, error)`` for the board's hub lease, from the lease
    service's view (cached; at most one hub read, itself cached 10 s)."""
    leases = getattr(d, "leases", None)
    try:
        hub = getattr(d.engine.session(bid), "hub", None)
    except HarnessError:
        hub = None
    if hub is None or leases is None:
        return False, None, ""
    try:
        view = leases.view(hub, cached_only=True, max_age_s=VIEW_MAX_AGE_S)
        if view is None:
            view = leases.view(hub)
    except HarnessError as exc:
        return True, None, exc.message or type(exc).__name__
    return True, (view or {}).get("lease") or None, ""


def rule(role: str, behind: bool, lease: dict[str, Any] | None, error: str) -> tuple[bool, str]:
    """``(writable, read_only_reason)`` for a console of ``role`` under this lease."""
    if not behind:
        return True, ""
    here = held_here(lease or {})
    holder = str((lease or {}).get("holder") or "someone else")
    other = f"{holder} holds this board's hub lease"
    if lease and lease.get("mine") and not here:
        other = f"{holder} holds this board's hub lease in another session, not this Harness Manager"
    if role == ROLE_LINUX_ROOT:
        if here:
            return True, ""
        if error:
            return False, ("read-only: only the lease holder types on the Linux console, and the "
                           f"hub lease could not be read ({error}; not known is not free)")
        if not lease:
            return False, ("read-only: only the lease holder types on the Linux console, and "
                           "nobody holds this board's lease: take it first")
        return False, f"read-only: only the lease holder types on the Linux console; {other}"
    if lease and not here:
        return False, f"read-only: {other}, and only the lease holder types on its consoles"
    return True, ""


def access(d: Any, bid: str, key: str, name: str) -> dict[str, Any]:
    """``{role, writable, read_only_reason}`` for one console of an open board."""
    row = with_access(d, bid, [{"name": name, "alias_of": key}])[0]
    return {k: row[k] for k in ("role", "writable", "read_only_reason")}


def with_access(d: Any, bid: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Console rows (``{name, alias_of?, ...}``) plus ``role``, ``writable`` and
    ``read_only_reason``: one lease read for them all."""
    impl = harness_impl(d, bid)
    behind, lease, error = lease_of(d, bid)
    out = []
    for row in rows:
        name = str(row.get("name") or "")
        role = role_of(str(row.get("alias_of") or name), name, impl)
        writable, why = rule(role, behind, lease, error)
        out.append({**{k: v for k, v in row.items() if k != "alias_of" or v != name},
                    "role": role, "writable": writable, "read_only_reason": why})
    return out


def held_error(bid: str, name: str, why: str) -> HeldError:
    """What a read-only client's keystrokes get (once per change), on the console socket."""
    return HeldError(f"console {name} of {bid}: your input was not sent ({why})",
                     hint="take the lease (or ask for it) to type here")


class InputGate:
    """The console WebSocket's input gate (G1b): ``allows()`` before each write.

    The access is worked out when the socket opens (``start``: one lease read at most, cached
    by the lease service), again at most every ``RECHECK_S`` while someone types, and at once
    when the board's lease changes (``lease.state``, ``hub.event``: on a thread of its own, so
    the publisher never waits for a hub). A read-only socket gets ``{"input": {...}}`` when it
    opens and whenever the access changes, and ``{"error": HELD}`` once per reason when its
    bytes are dropped. ``put`` is the socket's outbox (thread-safe)."""

    RECHECK_S = 10.0

    def __init__(self, d: Any, bid: str, key: str, name: str, put: Callable[[Any], None]) -> None:
        self.d, self.bid, self.key, self.name, self._put = d, bid, key, name, put
        self._mu = threading.Lock()
        self._state: dict[str, Any] | None = None
        self._at = 0.0
        self._warned = ""
        self._unsubs = [d.bus.subscribe(t, self._changed) for t in ("lease.state", "hub.event")]

    def _compute(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        try:
            acc = access(self.d, self.bid, self.key, self.name)
        except Exception as exc:  # noqa: BLE001 - never block a console on a bug here
            log.warning("console %s of %s: access unknown (%s); input allowed", self.name,
                        self.bid, exc)
            acc = {"role": "", "writable": True, "read_only_reason": ""}
        with self._mu:
            prev, self._state, self._at = self._state, acc, time.monotonic()
            if acc["writable"]:
                self._warned = ""
        return prev, acc

    def _say(self, prev: dict[str, Any] | None, acc: dict[str, Any]) -> None:
        key = (acc["writable"], acc["read_only_reason"])
        if (prev is None and not acc["writable"]) or \
                (prev is not None and (prev["writable"], prev["read_only_reason"]) != key):
            self._put(json.dumps({"input": acc}))

    def start(self) -> None:
        prev, acc = self._compute()
        self._say(prev, acc)

    def allows(self) -> bool:
        with self._mu:
            fresh = self._state is not None and time.monotonic() - self._at < self.RECHECK_S
        if not fresh:
            prev, _acc = self._compute()
            self._say(prev, _acc)
        with self._mu:
            acc = dict(self._state or {"writable": True, "read_only_reason": ""})
            warn = not acc["writable"] and self._warned != acc["read_only_reason"]
            if warn:
                self._warned = acc["read_only_reason"]
        if warn:
            from .wire import error_object

            self._put(json.dumps({"error": error_object(
                held_error(self.bid, self.name, acc["read_only_reason"]))}))
        return bool(acc["writable"])

    def _changed(self, ev: Any) -> None:
        if ev.board_id != self.bid:
            return

        def refresh() -> None:
            prev, acc = self._compute()
            self._say(prev, acc)

        threading.Thread(target=refresh, daemon=True,
                         name=f"console-access-{self.name}").start()

    def close(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
