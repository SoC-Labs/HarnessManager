"""Name a board, and give it a MAC and an IP of its own (lane IDENTITY, Harness Manager v0.1.1).

david, 2 Oct 2026 (relayed by the Linux lead; they replace BRINGUP-2's MAC from the MCC serial):

1. **Board names:** A-Z, 0-9 and ``-``, 1-16 characters; Harness Manager upper-cases. The
   aligned panel shows 16 (the platform allows 19), so Harness Manager caps names at 16.
2. Workshop boards are named both ways: staff pre-name cards, and anyone renames on the spot.
3. The identity follows the card and the board in stages (a card sector in v2.1, MCCIF IDENT
   in mint 4). Not here; ``check_name``/``check_mac``/``check_ip`` are the seams a later
   card-sector writer reuses.
4. **A unique IP per board:** Harness Manager allocates an IP at install, and a RANDOM MAC.

Board-agnostic. What is the board pack's (``IdentityPolicy``): the MAC's first byte and the
ranges never handed out (the MPS3 image default 02:00:00:*), the address pool (MPS3:
192.168.10.110-199, the setting ``mps3.identity.ip_pool``), the addresses never handed out
(MPS3: 192.168.10.101, the image default and stage0 rescue's), and how to ask whether an
address answers now (MPS3: UDP identify). A pack gives its policy with ``identity_policy()``
(on the pack, or the engine for a demo); without one the core's generic policy applies.

What is the core's:

- the name rule (``check_name``), the MAC rule (``check_mac``: unicast, non-zero, outside the
  pack's reserved ranges) and the IP rule (``check_ip``: IPv4, a usable host of a /24);
- ``random_mac``: ``os.urandom(6)`` with byte 0 the pack's (MPS3 0x02: locally administered,
  unicast), re-rolled on a reserved range or a MAC in the registry;
- ``allocate_ip``: the first address of the pool that is not reserved, not in the registry and
  not answering now, searching from ``pool_start`` (the board's MAC: ``mac[5] mod`` the pool's
  size, so the lab's random MACs spread over the pool) and wrapping round; a clear refusal
  when none is left;
- **the registry** (``board_identity.SeenIdentities``, ``<state>/identity/seen.json``): every
  MAC and IP this Harness Manager assigned or saw, per board id, with the date;
- the notes every front end shows: the same-/24 rule, stage0 rescue's address, the MAC-only
  ARP trap, the hub guard;
- ``move_board_table``: a board whose address changed keeps its boards.toml table (its pinned
  SSH host key, its name, its power meter) under its new board id.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import RefusedError, UsageError

# --- names ---------------------------------------------------------------------------------------

#: david 2 Oct: the aligned panel shows 16 characters of row 0 (the platform takes 19).
NAME_MAX = 16
NAME_RULE = f"1-{NAME_MAX} characters of A-Z, 0-9 and - (lower case is upper-cased)"
NAME_EXAMPLE = "LAB-07"
_NAME_RE = re.compile(r"^[A-Z0-9-]{1," + str(NAME_MAX) + r"}$")

#: The words of the choices (CLI, API and the dialog).
MAC_RANDOM = "random"
IP_AUTO = "auto"
KEEP = "keep"


def normalize_name(value: Any) -> str:
    """The name as the board gets it: trimmed and upper-cased."""
    return str(value or "").strip().upper()


def name_problem(value: Any) -> str:
    """Why this is not a board name, or ``""``. Upper-cased first (``lab-07`` is fine)."""
    text = normalize_name(value)
    if not text:
        return "it is empty"
    bad = sorted({c for c in text if not ("A" <= c <= "Z" or "0" <= c <= "9" or c == "-")})
    if bad:
        shown = ", ".join("a space" if c == " " else repr(c) for c in bad)
        return f"it has {shown} (only A-Z, 0-9 and - are allowed)"
    if len(text) > NAME_MAX:
        return f"it is {len(text)} characters (at most {NAME_MAX}: the panel shows {NAME_MAX})"
    return ""


def check_name(value: Any) -> str:
    """The name upper-cased, or ``UsageError`` with the rule in the message."""
    why = name_problem(value)
    if why:
        raise UsageError(f"{str(value or '').strip()!r} is not a board name: {why}. A name is "
                         f"{NAME_RULE}", hint=f"e.g. {NAME_EXAMPLE}")
    text = normalize_name(value)
    assert _NAME_RE.match(text)
    return text


# --- the pack's policy ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityPolicy:
    """What a board pack decides about MACs and IPs (``identity_policy()``)."""

    pack: str = ""
    #: byte 0 of every random MAC: 0x02 = locally administered, unicast
    mac_first_byte: int = 0x02
    #: ``aa:bb:cc`` prefixes never handed out or accepted, and why
    reserved_mac_prefixes: tuple[str, ...] = ()
    reserved_mac_why: str = ""
    #: the pool ``--ip auto`` takes from (``a.b.c.X-Y``), and the setting that holds it
    ip_pool: str = ""
    ip_pool_setting: str = ""
    #: addresses never handed out, and why
    reserved_ips: tuple[str, ...] = ()
    reserved_ip_why: str = ""
    #: the rescue path's fixed address and MAC (said in the dialog until the board can learn
    #: its own: MPS3 mint 4)
    rescue_note: str = ""
    #: hub targets whose record is known to be wrong today: target -> why
    known_bad_hub_records: Mapping[str, str] = field(default_factory=dict)
    #: ``answering(ip) -> bool``: something answers at this address now (MPS3: identify)
    answering: Callable[[str], bool] | None = field(default=None, compare=False)


GENERIC_POLICY = IdentityPolicy()


def policy_for(engine: Any, pack: str = "mps3") -> IdentityPolicy:
    """The policy of ``pack``: the engine's own ``identity_policy()`` (a demo), else the
    pack's, else the generic one (no pool, no reserved ranges)."""
    own = getattr(engine, "identity_policy", None)
    if callable(own):
        got = own(pack)
        if isinstance(got, IdentityPolicy):
            return got
    packs = getattr(engine, "packs", None)
    try:
        found = packs().get(pack) if callable(packs) else None
    except Exception:  # noqa: BLE001 - a pack that cannot load has no policy
        found = None
    hook = getattr(found, "identity_policy", None)
    if callable(hook):
        got = hook()
        if isinstance(got, IdentityPolicy):
            return got
    return GENERIC_POLICY


# --- MACs ----------------------------------------------------------------------------------------


def _norm_mac(value: Any) -> str:
    from harness_manager.services.board_identity import norm_mac

    return norm_mac(value)


def mac_problem(value: Any, policy: IdentityPolicy = GENERIC_POLICY) -> str:
    """Why the board must not take this MAC, or ``""``."""
    mac = _norm_mac(value)
    if not mac:
        return "it is not a MAC (12 hex digits, e.g. 02:5e:3a:91:c0:17)"
    first = int(mac[:2], 16)
    if first & 0x01:
        return "it is a multicast MAC (bit 0 of the first byte is set)"
    if mac == "00:00:00:00:00:00":
        return "it is all zeros"
    for prefix in policy.reserved_mac_prefixes:
        if mac.startswith(prefix.lower()):
            return f"{prefix}:* is reserved" + (f" ({policy.reserved_mac_why})"
                                                if policy.reserved_mac_why else "")
    return ""


def check_mac(value: Any, policy: IdentityPolicy = GENERIC_POLICY) -> str:
    """``aa:bb:cc:dd:ee:ff`` or ``UsageError`` naming the rule."""
    why = mac_problem(value, policy)
    if why:
        raise UsageError(f"MAC {str(value or '').strip()!r} cannot be used: {why}",
                         hint="--mac random gives one that can (a locally administered "
                              "unicast MAC)")
    return _norm_mac(value)


def random_mac(policy: IdentityPolicy = GENERIC_POLICY, taken: Iterable[str] = (), *,
               urandom: Callable[[int], bytes] = os.urandom, tries: int = 64) -> str:
    """``urandom(6)`` with byte 0 the pack's (0x02: locally administered, unicast), re-rolled
    on a reserved range or a MAC already in ``taken`` (the registry)."""
    used = {_norm_mac(m) for m in taken if _norm_mac(m)}
    for _ in range(max(1, tries)):
        raw = bytearray(urandom(6))
        if len(raw) != 6:
            raise ValueError("urandom(6) did not give 6 bytes")
        raw[0] = policy.mac_first_byte & 0xFE          # never multicast
        mac = ":".join(f"{b:02x}" for b in raw)
        if not mac_problem(mac, policy) and mac not in used:
            return mac
    raise RefusedError(f"no random MAC that is free and allowed after {tries} tries: nothing "
                       "was changed", hint="give one: --mac 02:xx:xx:xx:xx:xx")


# --- IPs -----------------------------------------------------------------------------------------


def ip_only(value: Any) -> str:
    """The address part of ``a.b.c.d[/n]``; ``""`` when it is not IPv4."""
    from harness_manager.services.board_identity import ip_addr

    return ip_addr(value)


def ip_problem(value: Any) -> tuple[str, str]:
    """``(why, "")``, or ``("", "a.b.c.d/24")``: an IPv4 address, a usable host of a /24 (no
    prefix = /24). The board would take 8-30; a board Harness Manager names takes a /24."""
    from harness_manager.services.board_identity import ip_problem as board_rule

    text = str(value or "").strip()
    m = re.fullmatch(r"([0-9.]+)(?:/(\d{1,2}))?", text)
    if m and m.group(2) is not None and m.group(2) != "24":
        return f"its prefix is /{m.group(2)}; a board's address is in a /24", ""
    why, canon = board_rule(text)
    if why:
        return why, ""
    return "", canon


def check_ip(value: Any) -> str:
    """``a.b.c.d/24`` or ``UsageError`` naming the rule."""
    why, canon = ip_problem(value)
    if why:
        raise UsageError(f"IP {str(value or '').strip()!r} cannot be used: {why}",
                         hint="an IPv4 address in a /24, e.g. 192.168.10.117 (or --ip auto)")
    return canon


def parse_pool(text: Any) -> list[str]:
    """``a.b.c.X-Y`` or ``a.b.c.X-a.b.c.Y`` (one /24, X <= Y, host addresses only) -> the
    addresses in order. ``UsageError`` otherwise."""
    raw = str(text or "").strip()
    m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\s*-\s*"
                     r"(?:(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.)?(\d{1,3})", raw)
    if not m:
        raise UsageError(f"the address pool {raw!r} is not a.b.c.X-Y",
                         hint="e.g. 192.168.10.110-199")
    a, b, c, lo = (int(x) for x in m.group(1, 2, 3, 4))
    if m.group(5) is not None and (int(m.group(5)), int(m.group(6)), int(m.group(7))) != (a, b, c):
        raise UsageError(f"the address pool {raw!r} spans more than one /24",
                         hint="e.g. 192.168.10.110-199")
    hi = int(m.group(8))
    if any(x > 255 for x in (a, b, c)) or not 1 <= lo <= hi <= 254:
        raise UsageError(f"the address pool {raw!r} is not a range of host addresses",
                         hint="X and Y in 1..254, X <= Y, e.g. 192.168.10.110-199")
    return [f"{a}.{b}.{c}.{n}" for n in range(lo, hi + 1)]


def pool_problem(text: Any) -> str:
    """A settings check: ``""`` or why the pool is not one."""
    if text in ("", None):
        return ""
    try:
        parse_pool(text)
    except UsageError as exc:
        return exc.message
    return ""


def pool_start(addrs: list[str], mac: Any = None) -> int:
    """Where ``--ip auto`` starts in the pool (david 2 Oct, the lab's seat sheet): the index
    ``mac[5] mod len(pool)``, so boards with random MACs spread over the pool instead of all
    trying the first address (MPS3: 192.168.10.(110 + mac[5] mod 90)). 0 with no MAC."""
    m = _norm_mac(mac)
    if not m or not addrs:
        return 0
    return int(m.rsplit(":", 1)[1], 16) % len(addrs)


def allocate_ip(policy: IdentityPolicy, taken: Iterable[str] = (), *,
                answering: Callable[[str], bool] | None = None,
                pool: str | None = None, mac: Any = None) -> str:
    """The first address of the pool that is not reserved, not in ``taken`` (the registry)
    and not answering now, as ``a.b.c.d/24``. ``RefusedError`` when none is left. With
    ``mac`` (the MAC the board will have) the search starts at ``pool_start`` and wraps
    round to the pool's start; without one it starts at the pool's first address."""
    text = policy.ip_pool if pool is None else pool
    if not text:
        raise RefusedError("this board pack has no address pool, so Harness Manager cannot "
                           "pick an IP: nothing was changed", hint="give one: --ip A.B.C.D")
    addrs = parse_pool(text)
    first = pool_start(addrs, mac)
    addrs = addrs[first:] + addrs[:first]
    used = {ip_only(x) for x in taken if ip_only(x)}
    reserved = {ip_only(x) for x in policy.reserved_ips if ip_only(x)}
    ask = answering if answering is not None else policy.answering
    counts = {"reserved": 0, "registry": 0, "answering": 0}
    for ip in addrs:
        if ip in reserved:
            counts["reserved"] += 1
            continue
        if ip in used:
            counts["registry"] += 1
            continue
        if ask is not None and ask(ip):
            counts["answering"] += 1
            continue
        return f"{ip}/24"
    where = policy.ip_pool_setting or "the pool"
    raise RefusedError(
        f"no free address in the pool {text}: all {len(addrs)} are taken ({counts['registry']} "
        f"given to or seen on boards here, {counts['answering']} answering now, "
        f"{counts['reserved']} reserved); nothing was changed",
        hint=(f"widen it (harness-manager config set {where} 192.168.10.110-249) or give one: "
              "--ip A.B.C.D") if policy.ip_pool_setting else "give one: --ip A.B.C.D")


def pc_example(ip: Any) -> str:
    """An address for THIS PC in the board's /24: ``.1``, or ``.2`` when the board is ``.1``."""
    addr = ip_only(ip)
    if not addr:
        return ""
    head, _, last = addr.rpartition(".")
    return f"{head}.{2 if last == '1' else 1}/24"


def same_net_note(ip: Any) -> str:
    """The line printed beside every new address."""
    ex = pc_example(ip)
    return f"this PC must be on the same /24 (e.g. {ex})" if ex else ""


def network_of(ip: Any) -> str:
    addr = ip_only(ip)
    return str(ipaddress.IPv4Network(f"{addr}/24", strict=False)) if addr else ""


def local_address_toward(host: str) -> str:
    """This PC's address on the way to ``host`` (a UDP socket's connect sends nothing);
    ``""`` for a loopback or unroutable host, or when there is no route."""
    addr = ip_only(host)
    if not addr or ipaddress.IPv4Address(addr).is_loopback:
        return ""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((addr, 9))
        return str(s.getsockname()[0] or "")
    return ""


def subnet_check(new_ip: Any, local: str) -> dict[str, Any]:
    """``{local, network, same}`` for the new address against this PC's (``same`` None when
    this PC's address toward the board is not known: a hub, a tunnel)."""
    if not local or not ip_only(new_ip):
        return {"local": local, "network": network_of(local) if local else "", "same": None}
    return {"local": local, "network": network_of(local),
            "same": network_of(local) == network_of(new_ip)}


def other_subnet_text(new_ip: Any, check: Mapping[str, Any]) -> str:
    addr = ip_only(new_ip)
    return (f"{addr} is not on this PC's network (this PC is {check.get('local')} in "
            f"{check.get('network')}): after the restart this PC cannot reach the board until "
            f"it has an address in {network_of(addr)} (e.g. {pc_example(addr)})")


def pool_network(policy: IdentityPolicy) -> str:
    """The /24 of the policy's address pool (``192.168.10.0/24``), ``""`` with no pool or one
    that does not parse."""
    if not policy.ip_pool:
        return ""
    try:
        addrs = parse_pool(policy.ip_pool)
    except UsageError:
        return ""
    return network_of(addrs[0])


def outside_pool(policy: IdentityPolicy, board_ip: Any) -> bool:
    """The board's own address is in a /24 other than the pool's (a board behind the hub on
    192.168.11.0/24, pool 192.168.10.110-199). False when either is not known."""
    pool = pool_network(policy)
    addr = ip_only(board_ip)
    if not addr or ipaddress.IPv4Address(addr).is_loopback:
        return False                    # a loopback address is how a board is reached, not a network
    board = network_of(addr)
    return bool(pool and board and pool != board)


def outside_pool_text(policy: IdentityPolicy, board_ip: Any) -> str:
    """The dialog's warning for a board outside the pool's network."""
    addr = ip_only(board_ip)
    return (f"This board is on {network_of(addr)}, outside the address pool ({policy.ip_pool}): "
            f"its current address {addr} is kept. Choose another address only if the board's "
            "network will reach it.")


def confirm_subnet_text(policy: IdentityPolicy, board_ip: Any, new_ip: Any) -> str:
    """The refusal for an address in another /24 than a board that is outside the pool."""
    return (f"this board is on {network_of(board_ip)}, outside the address pool "
            f"({policy.ip_pool}), and {ip_only(new_ip)} is in {network_of(new_ip)}: the board "
            "may be unreachable after its next restart")


def arp_note(ip: Any) -> str:
    """The MAC-only trap: no gratuitous ARP after the change, so a PC keeps the old MAC for
    the same address (macOS up to ~20 min)."""
    addr = ip_only(ip) or "the board's address"
    return (f"the MAC changes but the IP stays {addr}: this PC may keep the old MAC for it in "
            f"its ARP cache (macOS up to ~20 min), so the board may not answer at first; clear "
            f"it with `sudo arp -d {addr}` (Harness Manager never runs sudo), or pair the new "
            f"MAC with a new IP (--ip auto)")


# --- choosing: random, auto, keep, or a value ---------------------------------------------------


def resolve(policy: IdentityPolicy, *, mac: Any = None, ip: Any = None,
            current: Mapping[str, Any] | None = None, taken_macs: Iterable[str] = (),
            taken_ips: Iterable[str] = (), answering: Callable[[str], bool] | None = None,
            urandom: Callable[[int], bytes] = os.urandom) -> dict[str, Any]:
    """``mac``: ``random``, ``keep``, a MAC or None; ``ip``: ``auto``, ``keep``, an address or
    None. Returns ``{mac, mac_how, ip, ip_how}`` (``how``: random/auto/keep/custom; a value
    ``None`` = not changed). ``UsageError``/``RefusedError`` with the rule."""
    cur = current or {}
    out: dict[str, Any] = {"mac": None, "mac_how": "", "ip": None, "ip_how": ""}
    m = str(mac).strip().lower() if mac is not None else ""
    if m == MAC_RANDOM:
        out.update(mac=random_mac(policy, taken_macs, urandom=urandom), mac_how=MAC_RANDOM)
    elif m == KEEP:
        out.update(mac=_norm_mac(cur.get("mac")) or None, mac_how=KEEP)
    elif m:
        out.update(mac=check_mac(m, policy), mac_how="custom")
    i = str(ip).strip().lower() if ip is not None else ""
    if i == IP_AUTO:
        # the first candidate comes from the MAC the board will have (the new one, else its
        # own): random MACs spread the lab's boards over the pool
        start_mac = out["mac"] or _norm_mac(cur.get("mac")) or None
        out.update(ip=allocate_ip(policy, taken_ips, answering=answering, mac=start_mac),
                   ip_how=IP_AUTO)
    elif i == KEEP:
        out.update(ip=(f"{ip_only(cur.get('ip'))}/24" if ip_only(cur.get("ip")) else None),
                   ip_how=KEEP)
    elif i:
        out.update(ip=check_ip(i), ip_how="custom")
    return out


# --- boards.toml: a board that moved keeps its table ---------------------------------------------


def _flatten(prefix: tuple[str, ...], table: Mapping[str, Any]) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for k, v in table.items():
        if isinstance(v, Mapping):
            out.update(_flatten((*prefix, k), v))
        else:
            out[(*prefix, k)] = v
    return out


def move_board_table(old_id: str, new_id: str, old_host: str, new_host: str, *,
                     state_dir: Any = None) -> str:
    """boards.toml after a board's address changed (``old_id`` -> ``new_id``): a table keyed
    by the old board id moves to the new id (with everything in it: the pinned SSH host key,
    the name, a power meter); a table found by ``match`` gets the new address there instead of
    the old one. Returns what was done (``""``: no table). The settings writer keeps a dated
    backup of the file."""
    from harness_manager.power.config import load_boards
    from harness_manager.settings.files import SettingsFiles
    from harness_manager.settings.schema import join_key

    files = SettingsFiles(state_dir)
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    path = files.boards_path
    if not path.is_file():
        return ""
    raw = tomllib.loads(path.read_text(encoding="utf-8")).get("boards", {})
    if not isinstance(raw, dict):
        return ""
    old_addr = old_id.split("@", 1)[1] if "@" in old_id else ""
    new_addr = new_id.split("@", 1)[1] if "@" in new_id else ""
    swap = {old_id: new_id, old_host: new_host}
    if old_addr and new_addr:
        swap[old_addr] = new_addr

    def moved(match: Iterable[Any]) -> list[str]:
        return [swap.get(m, m) for m in match if isinstance(m, str)]

    if isinstance(raw.get(old_id), dict):
        if new_id in raw:
            raise RefusedError(f"boards.toml already has a table for {new_id}: the table of "
                               f"{old_id} was not moved onto it",
                               hint=f"merge them by hand in {path}")
        table = dict(raw[old_id])
        if isinstance(table.get("match"), list):
            table["match"] = moved(table["match"])
        edits: dict[str, Any] = {join_key(("boards", new_id, *k)): v
                                 for k, v in _flatten((), table).items()}
        edits[join_key(("boards", old_id))] = None
        files.write(edits)
        return f"boards.toml [boards.\"{old_id}\"] moved to [boards.\"{new_id}\"]"
    names = {n for n in (old_id, old_addr, old_host) if n}
    board = next((b for b in load_boards(path).boards if names & set(b.match)), None)
    if board is None or board.key == new_id:
        return ""
    new_match = moved(board.match)
    files.write({join_key(("boards", board.key, "match")): new_match})
    return f"boards.toml [boards.{board.key}] match now has {new_host}"
