"""The MPS3 board's network identity (label, IP, MAC) for Harness Manager (lane BOARD-ID).

``make_identity_adapter(session)`` is the ``pack.py`` hook (``session.net_identity``); the
rules every front end shares are ``harness_manager.services.board_identity``'s, and the
design is docs/design/BOARD_IDENTITY.md.

What the board says (net-protocol v0.16, the Linux lead's contract of 2026-09-28; images
rc2_v7/v7n):

- **6900 ``identity``** (a read, any peer), when ``version.features`` has ``identity``:
  ``{label, hostname, ip ("a.b.c.d/n"), mac (12 hex), source{field: override|stage0|default|
  label}, stage0{label, ip, mac}, override, pending, persist}``;
- **6900 ``identity_set``** (CLAIM-LOCKED like ``slot``/``usd``): any of ``{label, hostname, ip,
  mac}`` or ``{"clear": true}`` -> ``{persisted, pending, applies: "reboot"}``; codes
  ``locked``, ``no_persist`` (netboot or no card: the stage0 bake IS the identity),
  ``invalid`` (names the field). It applies at the next WARM ``reboot``;
- **identify** (UDP 6899, the board's own network only) has ``mac`` and ``ip`` on every image
  and ``label`` from v0.16. An image without ``identity`` is read from identify on the LAN
  and from ``stats.mac`` through a hub (UDP does not cross the tunnel).

What Harness Manager adds here:

- **The hub record** (``hub_record``): ``fpgahub target show T`` (the SSH runner) or REST's
  ``GET /targets/{t}``: ``network.board_ip``/``board_mac``/``hostname``/``host_ip`` and
  ``discovered_mac``; the label is the owning hub board's (``mps3_02`` -> ``MPS3-02``). Asked
  once per session (``refresh`` asks again); ``info`` never asks.
- **THE SEAM** (``set_identity``): ``HarnessdSetter`` (6900 ``identity_set`` through the
  session's claim forward, CLAIMED-LOCK's ``lock_route``; the default when the image has the
  ``identity`` feature), ``SshCommandSetter`` (``mps3-identity set k=v...`` over the pinned
  ``board ssh``), and without either ``PendingSetter``: ``UnavailableError``, "pending the Linux
  lead's interface".
- **The reboot** (``warm_reboot``): ``session.os_slots.reboot`` (the harness's own ``reboot``
  verb, witnessed by ``up_ms`` restarting; the reset guard is checked there). NEVER the
  controller's MCC REBOOT and never a power cycle: a cold start hits the stage0 DDR bug.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.services import board_identity as BI

from . import slot_words
from .constants import IMPL_LINUX

log = logging.getLogger(__name__)

FEATURE = "identity"
CAPABILITY = BI.CAPABILITY
#: How long a board read is reused by ``info`` (``cheap``).
READ_TTL_S = 30.0
IDENTIFY_TIMEOUT_S = 0.5
PENDING_WHY = ("pending the Linux lead's interface: this harness image has no identity verbs "
               "(net-protocol v0.16 `identity`/`identity_set`, images rc2_v7 and later)")
NETBOOT_WHY = ("this board has no persistent store for an identity (a netboot, or no user "
               "microSD): its identity comes from the stage0 bake")
NETBOOT_HINT = ("re-bake stage0 for this board (S0_IP, S0_LABEL, MPS3_MAC0..5 via S0_EXTRA_DEFS; "
                "an updatemem re-bake, not a re-mint), or give it a user microSD and fix it again")


def _iso(t: float) -> str:
    return BI._iso(t)


# --- the wire ----------------------------------------------------------------------------------


def parse_identity(raw: Mapping[str, Any], *, impl: str = "", via: str = "identity",
                   at: float | None = None) -> dict[str, Any]:
    """A reply of the ``identity`` verb (or identify's keys) as the service's shape."""
    at = time.time() if at is None else at
    src = raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
    persist = raw.get("persist")

    def sub(key: str) -> dict[str, Any] | None:
        v = raw.get(key)
        if not isinstance(v, Mapping):
            return None
        out = {k: v.get(k) for k in BI.FIELDS if k in v}
        if "mac" in out:
            out["mac"] = BI.norm_mac(out["mac"]) or out["mac"]
        return out

    label = raw.get("label")
    return {
        "label": label if isinstance(label, str) else "",
        "hostname": raw.get("hostname") if isinstance(raw.get("hostname"), str) else "",
        "ip": raw.get("ip") if isinstance(raw.get("ip"), str) else "",
        "mac": BI.norm_mac(raw.get("mac")),
        "source": {k: str(v) for k, v in src.items() if k in BI.FIELDS},
        "stage0": sub("stage0"), "override": sub("override"), "pending": sub("pending"),
        "persist": persist if isinstance(persist, bool) else None,
        "via": via, "feature": via == "identity", "impl": impl, "at": _iso(at),
    }


def request(client: Any, msg: dict[str, Any]) -> dict[str, Any]:
    """One request pyverify does not model (``identity``, ``identity_set``: v0.16), on the
    connection ``Mps3Shell.call_raw`` opened (``panel._request``'s way)."""
    return client._request(msg)


def set_error(reply: Mapping[str, Any], what: str = "identity_set") -> HarnessError:
    """A refused ``identity_set`` as the error Harness Manager shows (the reply's ``code``)."""
    code = slot_words.reply_code(dict(reply))
    err = str(reply.get("err") or "")
    if code == "no_persist":
        return RefusedError(f"{NETBOOT_WHY} [harness: {err or code}]; nothing was changed",
                            hint=NETBOOT_HINT)
    if code == "invalid":
        return UsageError(f"the board refused the identity: {err or 'invalid'}; nothing was "
                          "changed", hint="label: fits the LCD row; ip: a.b.c.d/n; mac: unicast, "
                                          "non-zero")
    if code == "locked" or slot_words.is_claim_lock(err, code):
        from harness_manager.services.claim import lock_error

        return lock_error(err, what)
    if "unknown op" in err:
        return UnavailableError(CAPABILITY, PENDING_WHY)
    return ActionFailedError(f"the board refused {what}: {err or code or 'no reason given'}")


# --- the seam ------------------------------------------------------------------------------------


class PendingSetter:
    """No board-side interface in this image: say so, and send nothing."""

    name = "pending"

    def __call__(self, session: Any, want: Mapping[str, Any]) -> dict[str, Any]:
        raise UnavailableError(CAPABILITY, PENDING_WHY)


class HarnessdSetter:
    """6900 ``identity_set``: claim-locked, so it goes through the session's claim forward (the
    board's own SSH to its 127.0.0.1:6900) whenever the claim is this Harness Manager's;
    otherwise directly, and a ``locked`` refusal met there is asked about again."""

    name = "harnessd"

    def __call__(self, session: Any, want: Mapping[str, Any]) -> dict[str, Any]:
        msg: dict[str, Any] = {"op": "identity_set"}
        for k, v in want.items():
            msg[k] = BI.wire_mac(v) if k == "mac" else v
        route = _route(session, "changing the board's identity")
        if route == "board-ssh":
            reply = self._via_board(session, msg)
        else:
            reply = _shell(session).call_raw(lambda c, _tap: request(c, msg))
            if not reply.get("ok") and slot_words.is_claim_lock(str(reply.get("err") or ""),
                                                                slot_words.reply_code(reply)):
                _route(session, "changing the board's identity", claimed=True)
                reply, route = self._via_board(session, msg), "board-ssh"
        if not reply.get("ok"):
            raise set_error(reply)
        return {"persisted": bool(reply.get("persisted")), "pending": reply.get("pending"),
                "applies": reply.get("applies") or "reboot", "route": route or "direct",
                "setter": self.name}

    @staticmethod
    def _via_board(session: Any, msg: dict[str, Any]) -> dict[str, Any]:
        from .card import _forwarded, _gate_key
        from .os_slots import claim_forward
        from .shell import Mps3Shell

        with claim_forward(session, "identity", "changing the board's identity") as (
                host, ctl, _push):
            shell = _forwarded(Mps3Shell(host, ctl, gate_key=_gate_key(_shell(session))))
            return shell.call_raw(lambda c, _tap: request(c, msg))


#: ``(argv, timeout) -> CompletedProcess``: a one-shot ssh (a test seam).
DEFAULT_RUN: Callable[..., Any] = lambda argv, timeout: subprocess.run(  # noqa: E731
    argv, capture_output=True, text=True, timeout=timeout, check=False)


class SshCommandSetter:
    """``mps3-identity set k=v...`` on the board over the pinned ``board ssh`` (the claim's
    key): the board-side command edits the same file with the same checks."""

    name = "ssh"

    def __init__(self, run: Callable[..., Any] | None = None, timeout_s: float = 30.0) -> None:
        self.run = run
        self.timeout_s = timeout_s

    def __call__(self, session: Any, want: Mapping[str, Any]) -> dict[str, Any]:
        claim = getattr(session, "claim", None)
        if claim is None:
            raise UnavailableError(CAPABILITY, "no SSH to this board (bare metal)")
        _route(session, "changing the board's identity", claimed=True)
        words = ["mps3-identity"] + (["clear"] if want.get("clear") else
                                     ["set"] + [f"{k}={v}" for k, v in want.items()])
        argv = claim.ssh_argv([shlex.join(words)])
        res = (self.run or DEFAULT_RUN)(argv, self.timeout_s)
        if res.returncode != 0:
            said = (res.stderr or res.stdout or "").strip().splitlines()
            text = said[-1] if said else f"exit {res.returncode}"
            if "no_persist" in text or "no persist" in text:
                raise RefusedError(f"{NETBOOT_WHY} [board: {text}]; nothing was changed",
                                   hint=NETBOOT_HINT)
            raise ActionFailedError(f"`{' '.join(words)}` on the board failed: {text}")
        return {"persisted": True, "pending": dict(want), "applies": "reboot",
                "route": "board-ssh", "setter": self.name}


def _shell(session: Any) -> Any:
    shell = getattr(session, "shell", None)
    if shell is None:
        raise UnavailableError(CAPABILITY, "no Ethernet link to the harness")
    return shell


def _route(session: Any, what: str, claimed: bool | None = None) -> str:
    claim = getattr(session, "claim", None)
    if claim is None or not callable(getattr(claim, "lock_route", None)):
        return "board-ssh" if claimed else ""
    return claim.lock_route(what, impl=IMPL_LINUX, claimed=claimed)


# --- the adapter ---------------------------------------------------------------------------------


class Mps3NetIdentity:
    """``session.net_identity`` (``services.board_identity`` says what each method answers).

    ``setter``: the seam; None picks ``HarnessdSetter`` when the image has the ``identity``
    feature, else ``PendingSetter``."""

    def __init__(self, session: Any, *, setter: Callable[..., dict[str, Any]] | None = None,
                 identify: Callable[..., Any] | None = None) -> None:
        self._session = session
        self.setter = setter
        self._identify = identify
        self._mu = threading.Lock()
        self._read: dict[str, Any] | None = None
        self._read_at = 0.0
        self._quick: dict[str, Any] | None = None
        self._quick_at = -READ_TTL_S
        self._hub_rec: dict[str, Any] | None = None
        self._live: Any = None
        #: How the last change reached the board, and the setter that made it (tests).
        self.last_set: dict[str, Any] | None = None

    # -- plumbing -------------------------------------------------------------------------------

    def _shell(self) -> Any:
        return _shell(self._session)

    def _tunnelled(self) -> bool:
        from .os_slots import _tunnelled

        return _tunnelled(self._session)

    def address(self) -> str:
        shell = getattr(self._session, "shell", None)
        return f"{shell.host}:{shell.port}" if shell is not None else ""

    def live(self) -> Any:
        live = self._shell().live()
        with self._mu:
            self._live = live
        return live

    # -- reading --------------------------------------------------------------------------------

    def read(self, *, refresh: bool = False, cheap: bool = False) -> dict[str, Any] | None:
        """A FULL read (``refresh``, or none cached within ``READ_TTL_S``): the ``identity``
        verb when the image has it, else identify (LAN) or ``stats.mac``; it knows the
        image's features (``feature_known``). ``cheap`` (``info``): the last full read (marked
        ``last_check`` when old), else identify on the LAN (cached apart: it never stands in
        for a full read), else nothing; never a control-port connection."""
        now = time.monotonic()
        with self._mu:
            full, full_at = self._read, self._read_at
            quick, quick_at = self._quick, self._quick_at
        if cheap:
            if full is not None:
                return full if now - full_at < READ_TTL_S else {**full, "last_check": True}
            if now - quick_at < READ_TTL_S:
                return quick                    # the last answer, or the last silence
            if self._tunnelled():
                return None                     # through a hub: never a call from info
            out = self._from_identify(impl="")
            if out is not None:
                out["feature_known"] = False
            with self._mu:                      # a silent board is not asked again for a while
                self._quick, self._quick_at = out, now
            return out
        if not refresh and full is not None and now - full_at < READ_TTL_S:
            return full
        live = self.live()
        if FEATURE in live.features:
            reply = self._shell().call_raw(lambda c, _tap: request(c, {"op": "identity"}))
            if not reply.get("ok"):
                raise set_error(reply, "identity")
            out = parse_identity(reply, impl=live.impl)
        else:
            out = None if self._tunnelled() else self._from_identify(impl=live.impl)
            if out is None:                     # through a hub, or no identify: stats.mac
                out = self._from_stats(impl=live.impl or "bare-metal")
        if out is not None:
            out["feature_known"] = True
            with self._mu:
                self._read, self._read_at = out, now
        return out

    def _from_identify(self, *, impl: str) -> dict[str, Any] | None:
        from . import identify as _identify

        ask = self._identify or _identify.identify
        try:
            reply = ask(self._shell().host, timeout=IDENTIFY_TIMEOUT_S, retries=0)
        except (UnreachableError, UsageError):
            return None
        raw = reply.raw if hasattr(reply, "raw") else dict(reply)
        return parse_identity(raw, impl=impl or (reply.impl if hasattr(reply, "impl") else ""),
                              via="identify")

    def _from_stats(self, *, impl: str) -> dict[str, Any] | None:
        try:
            stats = self._shell().call(lambda c: c.stats())
        except HarnessError as exc:
            log.info("stats unread on %s: %s", self.address(), exc)
            return None
        mac = getattr(stats, "mac", "") if getattr(stats, "ok", False) else ""
        return parse_identity({"mac": mac}, impl=impl, via="stats") if mac else None

    # -- the hub --------------------------------------------------------------------------------

    def _hub(self) -> Any:
        return getattr(self._session, "hub", None)

    def hub_record(self, *, refresh: bool = False, cheap: bool = False) -> dict[str, Any] | None:
        hub = self._hub()
        if hub is None:
            return None
        with self._mu:
            cached = self._hub_rec
        if cached is not None and not refresh:
            return cached
        if cheap:
            return None
        rec = self._record(hub, hub.target, self._board_of(hub))
        with self._mu:
            self._hub_rec = rec
        return rec

    def hub_others(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        hub = self._hub()
        if hub is None or not refresh:
            return []
        groups = getattr(hub.client, "groups", None)
        if not callable(groups):
            return []
        out = []
        for g in groups():
            board = str(g.get("board") or "")
            for m in g.get("members") or []:
                name = m.get("name") if isinstance(m, dict) else None
                if not isinstance(name, str) or name == hub.target:
                    continue
                if not name.endswith("_pl"):
                    continue                    # the harness targets (an MCC has no IP of its own)
                try:
                    out.append(self._record(hub, name, board))
                except HarnessError as exc:
                    log.info("hub record of %s unread: %s", name, exc)
        return out

    @staticmethod
    def _board_of(hub: Any) -> str:
        from .naming import derive_board_id

        board = getattr(hub.config, "board", "") if getattr(hub, "config", None) else ""
        if board:
            return board
        try:
            return str(hub.client.board_id())
        except HarnessError:
            return derive_board_id(hub.target)

    @staticmethod
    def _record(hub: Any, target: str, board: str) -> dict[str, Any]:
        info = hub.client.target_info(target) if target != hub.target else hub.client.target_info()
        net = info.get("network") if isinstance(info.get("network"), Mapping) else {}
        rec = {"target": target, "board": board, "label": BI.label_for_board(board),
               "board_ip": BI.ip_addr(str(net.get("board_ip") or "")),
               "prefix": BI.ip_prefix(str(net.get("host_ip") or "")) or 24,
               "board_mac": BI.norm_mac(net.get("board_mac")),
               "pl_mac": BI.norm_mac(net.get("pl_mac")),
               "hostname": str(net.get("hostname") or ""),
               "discovered_mac": BI.norm_mac(info.get("discovered_mac")),
               "hub": getattr(hub, "host", "")}
        rec["mac_suspect"] = BI.hub_mac_suspect(rec)
        # the service's ``others`` shape too
        rec.update(ip=rec["board_ip"], mac=rec["board_mac"])
        return rec

    # -- can it be fixed ------------------------------------------------------------------------

    def fix_reason(self, reported: Mapping[str, Any]) -> tuple[str, str, str]:
        impl = str(reported.get("impl") or "")
        if impl and impl != IMPL_LINUX:
            return ("UNAVAILABLE", "the bare-metal harness has no identity store: its label, IP "
                    "and MAC are compiled into the firmware (MPS3_MAC0..5, MPS3_DEFAULT_IP_*)", "")
        if not reported.get("feature") and reported.get("feature_known"):
            if self.setter is not None:
                return "", "", ""
            return "UNAVAILABLE", PENDING_WHY, ""
        if reported.get("persist") is False:
            return "REFUSED", NETBOOT_WHY + "; Harness Manager never sets it", NETBOOT_HINT
        claim = getattr(self._session, "claim", None)
        plan = getattr(claim, "lock_plan", None)
        if callable(plan):
            route, why = plan(impl=IMPL_LINUX)
            if route == "locked":
                return "REFUSED", f"changing the identity is claim-locked: {why}", \
                    "only the claiming key can change it (`harness-manager board claim-status TARGET`)"
            if route != "board-ssh":
                return ("REFUSED", "changing the identity needs the board claimed by this Harness "
                        "Manager (the change is claim-locked)",
                        "claim it first: `harness-manager board claim TARGET`")
        return "", "", ""

    # -- the change -----------------------------------------------------------------------------

    def _setter(self) -> Callable[..., dict[str, Any]]:
        if self.setter is not None:
            return self.setter
        with self._mu:
            live = self._live
        if live is None:
            live = self.live()
        return HarnessdSetter() if FEATURE in live.features else PendingSetter()

    def set_identity(self, want: Mapping[str, Any]) -> dict[str, Any]:
        """THE SEAM (docs/design/BOARD_IDENTITY.md §6)."""
        out = self._setter()(self._session, dict(want))
        self.last_set = dict(out)
        with self._mu:
            self._read = self._quick = None     # the next read asks the board
            self._quick_at = -READ_TTL_S
        return out

    def warm_reboot(self, progress: Any, wait_s: float) -> dict[str, Any]:
        """The harness's own ``reboot`` verb, witnessed. Never an MCC REBOOT (the controller)
        and never a power cycle: a cold start hits the stage0 DDR bug."""
        slots = getattr(self._session, "os_slots", None)
        if slots is None or not callable(getattr(slots, "reboot", None)):
            raise UnavailableError(CAPABILITY, "this session cannot restart the harness (no "
                                               "reboot verb): nothing was rebooted")
        witness = slots.reboot(progress=progress, wait_s=wait_s)
        with self._mu:
            self._read = self._quick = None
            self._quick_at = -READ_TTL_S
            self._live = None
        resets = getattr(self._session, "resets", None)
        if resets is not None and callable(getattr(resets, "refresh", None)):
            resets.refresh()
        return dict(witness or {})


def make_identity_adapter(session: Any) -> Mps3NetIdentity | None:
    """The pack hook: every session with an Ethernet shell (bare metal is read, never set)."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3NetIdentity(session)
