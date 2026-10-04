"""The MPS3 board's network identity (label, IP, MAC) for Harness Manager (lane BOARD-ID).

``make_identity_adapter(session)`` is the ``pack.py`` hook (``session.net_identity``); the
rules every front end shares are ``harness_manager.services.board_identity``'s, and the
design is docs/design/BOARD_IDENTITY.md.

What the board says (net-protocol v0.16 AS SHIPPED, platform ``feat/linux-harness`` 18622e5;
images rc2_v7/v7n; V7-ALIGN checked every point below against that commit):

- **6900 ``identity``** (a read, any peer), when ``version.features`` has ``identity``:
  ``{label, hostname, ip ("a.b.c.d/n"), mac (12 hex), source{field: override|stage0|default|
  label}, stage0{label, ip, mac}, override, pending, persist}``;
- **6900 ``identity_set``** (CLAIM-LOCKED like ``slot``/``usd``): any of ``{label, hostname, ip,
  mac}`` (``""`` DROPS that key from the override) or ``{"clear": true}`` -> ``{op, persisted,
  pending, applies: "reboot"}``; refusals IN THIS ORDER: ``locked`` (checked first, whatever
  the request holds), ``no_persist`` (netboot or no card: the stage0 bake IS the identity),
  ``invalid`` ("invalid <field>: <why>"); ``io`` for a write that failed. It applies at the
  next WARM ``reboot``. The label is 1-19 of ``[A-Z0-9-]`` (a stage0 bake holds 8);
- **the new replies carry ``"op"``** (``identity``, ``identity_set``, ``locate``), unlike the
  older verbs: tolerated, never required;
- **bare metal** (a v0.16 coordinator) answers both ``identity not supported``, code
  ``not_supported``; an image older than v0.16 answers ``unknown op``;
- **identify** (UDP 6899, the board's own network only) has ``mac`` and ``ip`` on every image
  and ``label`` from v0.16 (after ``ssh``, before ``ports``). Its ``ip`` is the resolved static
  address, EXCEPT while a DHCP lease is held (``dhcp: true``): then it is the lease, which is
  not the board's identity (HM keeps it as ``lease`` and leaves ``ip`` empty). An image
  without ``identity`` is read from identify on the LAN and from ``stats.mac`` through a hub
  (UDP does not cross the tunnel);
- **the default hostname** is the label in lower case (``mps3-01`` after board 1's bake;
  ``mps3`` without one); it was ``mps3-harness``. HM files the board's SSH key by board id
  (``claim.host_key_alias``), never by host name, so nothing here depends on it.

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
NETBOOT_WHY = ("this board has no persistent store for an identity (a netboot, no user "
               "microSD, or a card written on a PC whose /persist partition is still blank): "
               "its identity comes from the stage0 bake")
#: The Linux lead (2 Oct): a card written on a PC has a blank p3, so /persist is tmpfs and
#: identity_set answers no_persist until `mps3-persist format --erase` and a reboot. Said, never
#: done: Harness Manager does not format a card by itself.
PERSIST_FORMAT = "mps3-persist format --erase && mps3-reboot"
NETBOOT_HINT = ("a card written on a PC (the Linux lead, 2 Oct): with the board claimed (its claim "
                "is in the blank, in-memory /persist), `harness-manager board ssh TARGET -c "
                f"'{PERSIST_FORMAT}'` formats the card's partition 3 and restarts it; the board "
                "comes back UNCLAIMED with a NEW SSH host key, so claim it again: `harness-manager "
                "board claim TARGET --replace-host-key`; then set the identity again. Harness "
                "Manager never formats it for you. A netboot: re-bake stage0 for this board "
                "(S0_IP, S0_LABEL, MPS3_MAC0..5 via S0_EXTRA_DEFS; an updatemem re-bake, not a "
                "re-mint)")

# --- lane IDENTITY: the MPS3 pack's identity policy (david 2 Oct) ----------------------------------

#: Every random MAC starts 02 (locally administered, unicast); 02:00:00:* is the image's own
#: range (the default 02:00:00:4d:50:53 and the stage0 bakes' 02:00:00:00:0x:xx), never handed
#: out and never accepted from a person.
MAC_FIRST_BYTE = 0x02
RESERVED_MAC_PREFIXES = ("02:00:00",)
RESERVED_MAC_WHY = "the MPS3 image's own range: its default MAC and the stage0 bakes"
#: The pool `--ip auto` takes from (the setting mps3.identity.ip_pool).
IP_POOL_KEY = "mps3.identity.ip_pool"
IP_POOL_ENV = "HARNESS_MANAGER_MPS3_IP_POOL"
DEFAULT_IP_POOL = "192.168.10.110-199"
#: Never handed out: the generic image's address, which stage0 rescue answers on too.
RESERVED_IPS = (BI.DEFAULT_IP,)
RESERVED_IP_WHY = "the generic image's address and stage0 rescue's"
RESCUE_NOTE = ("in recovery mode the board still answers on 192.168.10.101 with its default "
               "MAC 02:00:00:4d:50:53, whatever this board is named: look for a board "
               "in recovery mode there")
#: Hub records known to be wrong today (the hub's board_mac is its own USB adapter).
KNOWN_BAD_HUB_RECORDS = {
    "mps3_01_pl": "its board_mac 00:e0:4c:46:dc:f8 is the hub's own USB adapter, not the board",
}
#: How long one "does anything answer at this address?" identify waits (the pool's check).
ANSWER_TIMEOUT_S = 0.3


def answering(ip: str) -> bool:
    """Something answers identify (UDP 6899) at ``ip`` now: a harness, or stage0 rescue."""
    from . import identify as _identify

    try:
        _identify.identify(ip, timeout=ANSWER_TIMEOUT_S, retries=0)
    except (UnreachableError, UsageError):
        return False
    return True


#: The pool's check (a test seam: tests never send a datagram to a real address).
DEFAULT_ANSWERING: Callable[[str], bool] = answering


def ip_pool() -> str:
    """mps3.identity.ip_pool (its variable, else the setting, else the default)."""
    from .settings import value

    try:
        got = value(IP_POOL_KEY)          # IP_POOL_ENV first: the resolver's env layer
    except HarnessError as exc:
        log.warning("%s unread (%s): using %s", IP_POOL_KEY, exc, DEFAULT_IP_POOL)
        return DEFAULT_IP_POOL
    return str(got or DEFAULT_IP_POOL)


def identity_policy() -> Any:
    """The MPS3 pack's ``IdentityPolicy`` (``Mps3Pack.identity_policy``)."""
    from harness_manager.services.identity_assign import IdentityPolicy

    return IdentityPolicy(
        pack="mps3", mac_first_byte=MAC_FIRST_BYTE, reserved_mac_prefixes=RESERVED_MAC_PREFIXES,
        reserved_mac_why=RESERVED_MAC_WHY, ip_pool=ip_pool(), ip_pool_setting=IP_POOL_KEY,
        reserved_ips=RESERVED_IPS, reserved_ip_why=RESERVED_IP_WHY, rescue_note=RESCUE_NOTE,
        known_bad_hub_records=dict(KNOWN_BAD_HUB_RECORDS),
        answering=lambda ip: DEFAULT_ANSWERING(ip))

#: A board whose address changed: the words when it is not found at the new one (the Linux
#: lead, 2 Oct, (d)).
MOVE_TIMEOUT = ("board not seen on {ip} after {minutes} min: it may be on DHCP or the address "
                "was taken (DAD); check the panel, which shows the IP on row 5")



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
    ip = raw.get("ip") if isinstance(raw.get("ip"), str) else ""
    lease = ""
    if via == "identify" and raw.get("dhcp") is True:
        # v0.16: identify's ip is the DHCP lease while one is held, not the board's identity
        # (its static address is then a secondary): never compared, kept as ``lease``
        ip, lease = "", ip
    out = {
        "label": label if isinstance(label, str) else "",
        "hostname": raw.get("hostname") if isinstance(raw.get("hostname"), str) else "",
        "ip": ip,
        "mac": BI.norm_mac(raw.get("mac")),
        "source": {k: str(v) for k, v in src.items() if k in BI.FIELDS},
        "stage0": sub("stage0"), "override": sub("override"), "pending": sub("pending"),
        "persist": persist if isinstance(persist, bool) else None,
        "via": via, "feature": via == "identity", "impl": impl, "at": _iso(at),
    }
    if lease:
        out["lease"] = lease
    return out


def request(client: Any, msg: dict[str, Any]) -> dict[str, Any]:
    """One ``identity``/``identity_set`` request (v0.16) through pyverify's framing, on the
    connection ``Mps3Shell.call_raw`` opened (``panel._request``'s way). The vendored pyverify
    (platform 3f7cea2) has ``ShellClient.identity()``/``identity_set()``; HM builds the same
    request itself, byte for byte (tests/integration/test_pyverify_vendored.py), so a pyverify
    older than 18622e5 (an editable checkout) still works."""
    return client._request(msg)


#: V7-ALIGN: a v0.16 bare-metal coordinator declines both verbs with this code.
NOT_SUPPORTED_WHY = ("the bare-metal harness has no identity store: its label, IP and MAC are "
                     "compiled into the firmware (MPS3_MAC0..5, MPS3_DEFAULT_IP_*)")


def set_error(reply: Mapping[str, Any], what: str = "identity_set") -> HarnessError:
    """A refused ``identity``/``identity_set`` as the error Harness Manager shows (the reply's
    ``code``: ``locked``, ``no_persist``, ``invalid``, ``not_supported``; ``unknown op`` from an
    image older than v0.16)."""
    code = slot_words.reply_code(dict(reply))
    err = str(reply.get("err") or "")
    if code == "not_supported":
        return UnavailableError(CAPABILITY, f"{NOT_SUPPORTED_WHY} [harness: {err or code}]")
    if code == "no_persist":
        return RefusedError(f"{NETBOOT_WHY} [harness: {err or code}]; nothing was changed",
                            hint=NETBOOT_HINT)
    if code == "invalid":
        return UsageError(f"the board refused the identity: {err or 'invalid'}; nothing was "
                          "changed", hint=f"label: 1-{BI.LABEL_MAX} of A-Z, 0-9 and -; ip: "
                                          "a.b.c.d/nn (nn 8-30); mac: unicast, non-zero")
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


def _run_one_shot(argv: Any, timeout: float) -> Any:
    from harness_manager.core.proc import no_window

    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL, **no_window())


#: ``(argv, timeout) -> CompletedProcess``: a one-shot ssh (a test seam).
DEFAULT_RUN: Callable[..., Any] = _run_one_shot


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
        self._facts: dict[str, Any] | None = None
        self._facts_at = -READ_TTL_S
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

    def readings_facts(self) -> dict[str, Any] | None:
        """The harness's uptime from UDP 6899 identify (``up_ms``, and the Linux OS's
        ``os_up_ms``), for a board whose telemetry reads no ``stats``. No control-port
        connection: one datagram on the board's own network, cached ``READ_TTL_S`` (a silent
        board too), never through a hub. None when identify does not answer or has no uptime."""
        now = time.monotonic()
        with self._mu:
            cached, cached_at = self._facts, self._facts_at
        if now - cached_at < READ_TTL_S:
            return cached
        out: dict[str, Any] | None = None
        if not self._tunnelled() and self._shell() is not None:
            from . import identify as _identify

            ask = self._identify or _identify.identify
            try:
                reply = ask(self._shell().host, timeout=IDENTIFY_TIMEOUT_S, retries=0)
                raw = reply.raw if hasattr(reply, "raw") else dict(reply)
            except (UnreachableError, UsageError):
                raw = {}
            if isinstance(raw, Mapping) and isinstance(raw.get("up_ms"), (int, float)):
                out = {"up_ms": raw["up_ms"], "at": time.time(), "source": "identify (UDP 6899)"}
                if raw.get("os_up_ms") is not None:
                    out["os_up_ms"] = raw["os_up_ms"]
        with self._mu:
            self._facts, self._facts_at = out, now
        return out

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
        """V7-ALIGN: in the board's order (net-protocol v0.16 ``identity_set``): no identity
        verbs at all (``not_supported`` / an older image), then the claim (``locked``), then no
        card (``no_persist``); a bad value (``invalid``) is the caller's, last."""
        impl = str(reported.get("impl") or "")
        if impl and impl != IMPL_LINUX:
            return "UNAVAILABLE", NOT_SUPPORTED_WHY, ""
        if not reported.get("feature") and reported.get("feature_known"):
            if self.setter is not None:
                return "", "", ""
            return "UNAVAILABLE", PENDING_WHY, ""
        claimed = self._claim_reason()
        if claimed[0]:
            return claimed
        if reported.get("persist") is False:
            return "REFUSED", NETBOOT_WHY + "; Harness Manager never sets it", NETBOOT_HINT
        return "", "", ""

    def _claim_reason(self) -> tuple[str, str, str]:
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

    # -- lane IDENTITY: the pack's policy, and a board whose address changes ----------------------

    #: How often the new address is asked while the board restarts (tests: less).
    move_poll_s = 3.0
    #: The Linux lead (2 Oct): when the new address does not answer (DAD refused it, the board
    #: is on DHCP), an identify BROADCAST from this long after the reboot, then this often;
    #: only an answer with the board's own host key is adopted, wherever it is.
    broadcast_after_s = 60.0
    broadcast_every_s = 20.0

    @staticmethod
    def policy() -> Any:
        return identity_policy()

    def host_key(self) -> str:
        """The SSH host key fingerprint pinned for this board (boards.toml ``ssh.host_key``),
        ``""`` when none is: what the board must show at its new address."""
        from .claim import pin_fingerprint, ssh_config

        try:
            pin = ssh_config(self._session.candidate)["host_key"]
        except HarnessError:
            return ""
        return pin_fingerprint(pin) if pin else ""

    def relocate(self, ip: str, host_key: str, *, wait_s: float,
                 progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """The harness's own ``reboot`` verb (the reset guard first; never an MCC REBOOT), NOT
        witnessed at the old address (the board drops it), then identify at ``ip`` until a
        board answers there. Accepted only with ``host_key`` (the board's pinned key; /persist
        keeps it): another key is another board, never adopted. Returns ``{board_id, address,
        reported, reboot, identify}``."""
        from harness_manager.core.errors import HeldError
        from harness_manager.services import reset_guard

        from . import identify as _identify
        from .os_slots import card_job_refusal

        say = progress or (lambda _t: None)
        reset_guard.check(self._session, reset_guard.ACTION_HARNESS_REBOOT)
        shell = self._shell()
        try:
            resp = shell.call(lambda c: c.reboot())
        except HeldError as exc:
            busy = card_job_refusal(self._session, exc)
            if busy is None:
                raise
            raise busy from exc
        if not resp.ok:
            raise ActionFailedError(f"the harness refused reboot: {resp.err or '?'}; the new "
                                    "identity waits for the next restart",
                                    hint="it needs the watchdog ('reboot' feature)")
        with self._mu:
            self._read = self._quick = None
            self._quick_at = -READ_TTL_S
            self._live = None
        sent = time.monotonic()
        ask = self._identify or _identify.identify
        last_broadcast = -1e9
        elsewhere = ""
        while True:
            elapsed = time.monotonic() - sent
            try:
                reply = ask(ip, timeout=1.0, retries=0)
            except (UnreachableError, UsageError):
                reply = None
            if reply is not None and getattr(reply, "is_rescue", False):
                reply = None                     # stage0 rescue: not the harness yet
            if reply is None and elapsed >= self.broadcast_after_s \
                    and elapsed - last_broadcast >= self.broadcast_every_s:
                last_broadcast = elapsed
                say(f"nothing at {ip} yet: asking the network (identify broadcast) for the "
                    f"board's host key {host_key}")
                reply = self._by_broadcast(host_key, old_host=shell.host, since_s=elapsed)
                if reply is not None:
                    elsewhere = str(getattr(reply, "address", "") or "")
            if reply is not None:
                ssh = reply.ssh if hasattr(reply, "ssh") else {}
                seen = str(ssh.get("host_key_sha256") or "")
                if not seen:
                    raise RefusedError(
                        f"a board answers at {ip} but publishes no SSH host key, so Harness "
                        "Manager cannot tell it is this board; it was not adopted",
                        hint=f"check the panel (row 5 shows its IP); `harness-manager board "
                             f"identity {ip}` reads it")
                if seen != host_key:
                    raise RefusedError(
                        f"a different board answers at {ip}: its SSH host key is {seen}, this "
                        f"board's is {host_key}. It was not adopted. This board took the new "
                        "identity at its restart but keeps the address only if it was free "
                        "(DAD), so it may be on DHCP; check its panel (row 5 shows its IP)",
                        hint="give this board another address once you find it")
                raw = reply.raw if hasattr(reply, "raw") else dict(reply)
                impl = reply.impl if hasattr(reply, "impl") else IMPL_LINUX
                reported = parse_identity(raw, impl=impl, via="identify")
                address = reply.control_endpoint if hasattr(reply, "control_endpoint") \
                    else f"{ip}:6900"
                board_id = reply.board_id if hasattr(reply, "board_id") else f"mps3@{address}"
                note = ""
                if elsewhere and elsewhere != ip:
                    note = (f"the board did not take {ip} (the address may have been taken: "
                            f"DAD, or it is on DHCP): it was found at {elsewhere} by an identify "
                            "broadcast with its own SSH host key, and is opened there; give it "
                            "another address once you know why")
                return {"board_id": board_id, "address": address, "reported": reported,
                        "identify": raw, "elsewhere": elsewhere if note else "", "note": note,
                        "reboot": {"up_after_s": round(elapsed, 3),
                                   "down_evidence": f"the reboot verb answered (in_ms="
                                                    f"{getattr(resp, 'in_ms', 0)})",
                                   "up_evidence": f"identify at {ip}: host key {seen}",
                                   "summary": f"restarted; found at {ip} after {elapsed:.0f} s "
                                              "(the same SSH host key)"}}
            if elapsed >= wait_s:
                minutes = max(1, round(wait_s / 60))
                raise ActionFailedError(
                    "the identity was set and the board restarted, but "
                    + MOVE_TIMEOUT.format(ip=ip, minutes=minutes),
                    hint=f"once it answers: `harness-manager board identity {ip}`")
            say(f"waiting for the board at {ip} ({elapsed:.0f}/{wait_s:.0f} s)")
            time.sleep(self.move_poll_s)

    @staticmethod
    def _by_broadcast(host_key: str, *, old_host: str = "", since_s: float = 0.0) -> Any:
        """The one identify answer on the network with ``host_key`` (never another board's)
        that is the board AFTER its restart: not at its old address, and its harness up for
        less time than since the reboot was sent; else None. ``identify.discover`` is looked
        up when called (a test seam)."""
        from . import identify as _identify

        try:
            replies = _identify.discover()
        except (HarnessError, OSError):
            return None
        for r in replies:
            ssh = r.ssh if hasattr(r, "ssh") else {}
            if getattr(r, "is_rescue", False) or ssh.get("host_key_sha256") != host_key:
                continue
            if old_host and getattr(r, "address", "") == old_host:
                continue                         # still the old harness, before its restart
            up = getattr(r, "up_ms", None)
            if up is not None and up > (since_s + 2.0) * 1000.0:
                continue                         # not restarted since the reboot was sent
            return r
        return None

    def adopt_move(self, old_id: str, new_id: str, *, old_host: str,
                   new_host: str) -> list[str]:
        """This Harness Manager's records of the board under its new board id: its boards.toml
        table (the pinned SSH host key goes with it), its claim record, and the pinned
        known_hosts entry written again under the new id's alias. The board's own keys did not
        change (/persist keeps them), so the claim is carried over, not repeated."""
        from harness_manager.services.identity_assign import move_board_table

        from .claim import (
            ClaimRecords,
            _atomic_write,
            _state_dir,
            host_key_alias,
            known_hosts_path,
            parse_key_line,
            ssh_config,
        )

        done: list[str] = []
        try:
            pin = ssh_config(self._session.candidate)["host_key"]
        except HarnessError:
            pin = ""
        what = move_board_table(old_id, new_id, old_host, new_host, state_dir=_state_dir())
        if what:
            done.append(what)
        if ClaimRecords().move(old_id, new_id):
            done.append(f"claims.json: the claim of {old_id} is now {new_id}'s")
        old_kh = known_hosts_path(old_id)
        if old_kh.exists():
            old_kh.unlink()
        if pin and not pin.startswith("SHA256:"):
            ktype, blob = parse_key_line(pin)
            _atomic_write(known_hosts_path(new_id), f"{host_key_alias(new_id)} {ktype} {blob}\n")
            done.append(f"known_hosts: the pinned key, filed under {new_id}")
        return done


def make_identity_adapter(session: Any) -> Mps3NetIdentity | None:
    """The pack hook: every session with an Ethernet shell (bare metal is read, never set)."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3NetIdentity(session)
