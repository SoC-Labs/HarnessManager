"""A board's network identity (label, IP, MAC): find clashes, and make a board match its hub
entry (lane BOARD-ID; docs/design/BOARD_IDENTITY.md).

Board-agnostic. The pack's ``session.net_identity`` adapter does the board's side (for the
MPS3, ``harness_manager_mps3.net_identity``: the harness's ``identity`` verb, identify, the
hub's target records, ``identity_set`` over the claim forward and the warm ``reboot`` verb).
The adapter answers:

- ``read(refresh=False, cheap=False) -> dict | None``: what the board says it is,
  ``{label, hostname, ip, mac, source, stage0, override, pending, persist, via, feature,
  impl, at}`` (``mac`` as ``aa:bb:..``; ``""`` for a field it does not report). ``cheap``:
  no control-port connection (``info``);
- ``hub_record(refresh=False) -> dict | None``: the board's own hub target
  (``{target, board, label, board_ip, prefix, board_mac, hostname, discovered_mac,
  mac_suspect}``), None with no hub;
- ``hub_others(refresh=False) -> list[dict]``: the hub's other targets, the same shape;
- ``fix_reason(reported) -> (name, message, hint)``: why this board cannot take a fix now
  (bare metal, an image without the identity verbs, a netbooted board, a claim that is not
  this Harness Manager's), ``("", "", "")`` when it can. No I/O beyond what ``read`` did;
- ``set_identity(want) -> dict``: THE SEAM (the harness's ``identity_set`` or the board's
  ``mps3-identity set``), ``{persisted, pending, applies, route}``;
- ``warm_reboot(progress, wait_s) -> dict``: the harness's own ``reboot`` verb, witnessed.
  Never an MCC REBOOT, never a power cycle (a cold start hits the stage0 DDR bug);
- ``address() -> str``: where this session reaches the board.

This service adds the rules every front end shares:

- **Detection** (``compare``): a clash (another board reports the same MAC, IP or label, or
  this board reports another hub target's IP or label) is an error; a field left at the
  image default, or a difference from the board's own hub record, is a warning; a hub record
  whose ``board_mac`` looks like the hub's own adapter is a note, and that MAC is never
  proposed.
- **The boards seen** (``SeenIdentities``: ``<state>/identity/seen.json``): the last identity
  each board reported, so a clash with a board that is not open now is still found.
- **The fix** (``fix``): never automatic. The typed phrase (the new label, or ``IDENTITY
  <board_id>``) must be given; the lease must be ours (behind a hub); the board's claim must
  be this Harness Manager's (the change is claim-locked); never during a card job (the reset
  guard); refused on bare metal and on a netbooted board with the reason. Then: set, the
  WARM reboot, and a verify that reads the identity again.
- A change publishes ``board.net_identity`` ``{status, reported, hub}`` on the bus.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus

log = logging.getLogger(__name__)

CAPABILITY = "board identity"
NO_ADAPTER = "this board's pack has no network identity (no Ethernet harness to ask)"
FIELDS = ("label", "hostname", "ip", "mac")
#: What the Linux image gives a board whose identity was never set (net-protocol v0.16:
#: ``source`` "default"), and what every older image gives every board.
DEFAULT_MAC = "02:00:00:4d:50:53"
DEFAULT_IP = "192.168.10.101"
#: V7-ALIGN: the SHIPPED image default label (identity_core.h ``MPS3_ID_DEFAULT_LABEL``): no
#: number, the same on every board, so it never makes a label clash on its own.
DEFAULT_LABEL = "MPS3"
#: ``MPS3`` and the pre-v0.16 LCD's compiled ``MPS3-01`` (never on the wire; kept for callers).
DEFAULT_LABELS = (DEFAULT_LABEL, "MPS3-01")
#: V7-ALIGN (net-protocol v0.16 as shipped): ``identity_set`` takes a label of 1-19 of
#: ``[A-Z0-9-]`` (the CLCD row-0 field); a stage0 bake (``S0_LABEL``) holds at most 8.
LABEL_MAX = 19
LABEL_BAKE_MAX = 8
#: The host name the board takes (RFC 1123 labels, <= 63 in all).
HOSTNAME_MAX = 63
#: A board seen longer ago than this no longer counts for a clash.
SEEN_MAX_AGE_S = 14 * 24 * 3600.0
#: A report that did not change is written to seen.json at most this often (info polls).
SEEN_REFRESH_S = 300.0
#: The reboot the fix waits for (the Linux harness's own budget).
REBOOT_WAIT_S = 180.0

STATUS_OK = "ok"
STATUS_UNSET = "unset"
STATUS_DIFFERS = "differs"
STATUS_CLASH = "clash"
STATUS_UNKNOWN = "unknown"
_LABEL_RE = re.compile(r"^[A-Z0-9-]{1," + str(LABEL_MAX) + r"}$")
#: One dot-separated label of a host name (identity_core.c ``id_check_hostname``).
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
#: V7-ALIGN: what an empty string in ``identity_set`` means on the board: that key is DROPPED
#: from the override (the field then comes from the stage0 bake, else the image default).
DROP = ""


# --- normalising ------------------------------------------------------------------------------


def norm_mac(value: Any) -> str:
    """``aa:bb:cc:dd:ee:ff`` (lowercase) from 12 hex digits or a ``:``/``-`` form; else ``""``."""
    if not isinstance(value, str):
        return ""
    digits = re.sub(r"[:\-.]", "", value.strip()).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", digits):
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def wire_mac(mac: str) -> str:
    """The harness's wire form: 12 lowercase hex digits."""
    return norm_mac(mac).replace(":", "")


def ip_addr(value: Any) -> str:
    """The address part of ``a.b.c.d[/n]``; ``""`` when it is not an IPv4 address."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return str(ipaddress.IPv4Interface(value.strip()).ip)
    except ValueError:
        return ""


def ip_prefix(value: Any) -> int | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        return ipaddress.IPv4Interface(value.strip()).network.prefixlen
    except ValueError:
        return None


def mac_is_local(mac: str) -> bool:
    """Locally administered (bit 1 of the first octet): every harness MAC is."""
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 0x02)


def mac_is_unicast_nonzero(mac: str) -> bool:
    m = norm_mac(mac)
    return bool(m) and not int(m[:2], 16) & 0x01 and m != "00:00:00:00:00:00"


def label_for_board(board: str) -> str:
    """The LCD label a hub board id stands for: ``mps3_02`` -> ``MPS3-02``."""
    return (board or "").strip().upper().replace("_", "-")


def hostname_base(hostname: str) -> str:
    """The hub's dnsmasq name less its ``-pl`` target suffix: ``mps3-02-pl`` -> ``mps3-02``."""
    h = (hostname or "").strip().lower()
    for suffix in ("-pl", "-ps", "-mcc"):
        if h.endswith(suffix):
            return h[: -len(suffix)]
    return h


def same(field_name: str, a: Any, b: Any) -> bool:
    """One field compared the way the board would: the IP by address, the MAC by value, the
    label and hostname without case. Empty never matches anything."""
    if field_name == "ip":
        x, y = ip_addr(a), ip_addr(b)
    elif field_name == "mac":
        x, y = norm_mac(a), norm_mac(b)
    else:
        x, y = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(x) and x == y


def hub_mac_suspect(record: Mapping[str, Any]) -> str:
    """Why the hub record's ``board_mac`` is probably not the board's own MAC, or ``""``.

    fpgahub's ``discovered_mac`` is a netdev it found on the HUB's own USB bus (its adapter),
    so a ``board_mac`` equal to it names the hub's side of the cable. An MPS3 harness MAC is
    locally administered; a vendor (universally administered) one is most likely the hub's
    USB adapter too (``mps3_01_pl`` 00:e0:4c:46:dc:f8 is, 2026-09-28)."""
    mac = norm_mac(record.get("board_mac"))
    if not mac:
        return ""
    disc = norm_mac(record.get("discovered_mac"))
    if disc and disc == mac:
        return (f"the hub's board_mac {mac} is the netdev fpgahub found on the hub's own USB "
                "bus (discovered_mac): the hub's adapter, not the board")
    if not mac_is_local(mac):
        return (f"the hub's board_mac {mac} is a vendor (universally administered) address; a "
                "harness MAC is locally administered, so it is probably the hub's USB adapter")
    return ""


def hostname_problem(text: str) -> str:
    """Why the board would refuse this host name (RFC 1123: dot-separated labels of
    ``[A-Za-z0-9-]``, none starting or ending with ``-``, at most 63 in all), or ``""``."""
    if not text:
        return "empty"
    if len(text) > HOSTNAME_MAX:
        return f"longer than {HOSTNAME_MAX} characters"
    if not all(_HOST_LABEL_RE.match(part) for part in text.split(".")):
        return "not an RFC 1123 host name"
    return ""


def ip_problem(text: str) -> tuple[str, str]:
    """``(why, "")`` when the board would refuse this address, else ``("", "a.b.c.d/nn")``
    (no prefix = /24). The board's rules (net-protocol v0.16): the prefix 8-30, a usable host
    address: not 0/8, 127/8 or >= 224, nor the network or broadcast address of its prefix."""
    m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:/(\d{1,2}))?", text or "")
    if not m:
        return ("empty" if not text else "not a dotted quad"), ""
    octets = [int(x) for x in m.groups()[:4]]
    prefix = int(m.group(5)) if m.group(5) is not None else 24
    if any(o > 255 for o in octets):
        return "not a dotted quad", ""
    if not 8 <= prefix <= 30:
        return "prefix not in 8..30", ""
    if octets[0] in (0, 127) or octets[0] >= 224:
        return "not a usable host address", ""
    value = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    host = 0xFFFFFFFF >> prefix
    if value & host == 0:
        return "the network address of its prefix", ""
    if value & host == host:
        return "the broadcast address of its prefix", ""
    return "", "{}.{}.{}.{}/{}".format(*octets, prefix)


def validate_want(want: Mapping[str, Any]) -> dict[str, str]:
    """The fields a person asked for, checked as the board checks them (``invalid`` there).
    ``UsageError`` naming the field; the IP gains /24 when it has no prefix.

    V7-ALIGN: ``""`` is kept (``DROP``): the board takes it as "drop this key from the
    override" (net-protocol v0.16 ``identity_set``). ``None`` means "not given"."""
    out: dict[str, str] = {}
    for key, value in want.items():
        if key not in FIELDS:
            raise UsageError(f"{key!r} is not an identity field", hint="label, hostname, ip, mac")
        if value is None:
            continue
        text = str(value).strip()
        if text == DROP:
            out[key] = DROP
            continue
        if key == "label":
            if not _LABEL_RE.match(text):
                raise UsageError(f"label {text!r} does not fit the LCD row",
                                 hint=f"1-{LABEL_MAX} of A-Z, 0-9 and -, e.g. MPS3-02 (a stage0 "
                                      f"bake holds at most {LABEL_BAKE_MAX})")
            out[key] = text
        elif key == "hostname":
            why = hostname_problem(text)
            if why:
                raise UsageError(f"hostname {text!r} is not a host name: {why}",
                                 hint="letters, digits and -, dot-separated, e.g. mps3-02")
            out[key] = text
        elif key == "ip":
            why, canon = ip_problem(text)
            if why:
                raise UsageError(f"ip {text!r} is not a usable IPv4 address: {why}",
                                 hint="a.b.c.d or a.b.c.d/nn (nn 8-30), e.g. 192.168.11.101/24")
            out[key] = canon
        else:
            mac = norm_mac(text)
            if not mac_is_unicast_nonzero(mac):
                raise UsageError(f"mac {text!r} is not a unicast, non-zero MAC",
                                 hint="12 hex digits, e.g. 02:00:00:00:02:fe")
            out[key] = mac
    return out


def _iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t,
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- detection ---------------------------------------------------------------------------------


def _finding(kind: str, level: str, text: str, *, field_name: str = "",
             other: str = "") -> dict[str, Any]:
    return {"kind": kind, "level": level, "field": field_name, "other": other, "text": text}


def _shown(field_name: str, value: Any) -> str:
    if field_name == "mac":
        return norm_mac(value) or str(value or "")
    if field_name == "ip":
        return ip_addr(value) or str(value or "")
    return str(value or "")


def label_is_default(label: Any, source: Any) -> bool:
    """V7-ALIGN: the label is the image default every board gets (``source`` ``default``;
    with no source, the shipped default ``MPS3``). Such a label is "identity not set", never a
    clash on its own: board 2 keeps ``MPS3`` until its stage0 identity bake is fielded."""
    if source:
        return source == "default"
    return str(label or "").strip().upper() == DEFAULT_LABEL


_NAMES = {"label": "label", "hostname": "hostname", "ip": "IP", "mac": "MAC"}


def compare(reported: Mapping[str, Any] | None, hub: Mapping[str, Any] | None,
            others: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The findings, worst first (docs/design/BOARD_IDENTITY.md §4).

    ``others``: ``{who, kind: "board"|"hub", label, ip, mac, hostname, label_source?}`` for
    every OTHER board this Harness Manager has seen and every other target the hub lists.

    V7-ALIGN: a label that is the image default on EITHER side is not a clash (it is "identity
    not set"), nor a difference from the hub record; a duplicate MAC or IP is a clash whatever
    its source (two boards on one network), and an IP or MAC unlike the hub's still differs."""
    out: list[dict[str, Any]] = []
    if not reported:
        return out
    source = reported.get("source") if isinstance(reported.get("source"), Mapping) else {}
    my_label_default = label_is_default(reported.get("label"), source.get("label"))
    # 1. clashes: the strongest signal
    for o in others:
        who = str(o.get("who") or "another board")
        for f in ("mac", "ip", "label"):
            mine, theirs = reported.get(f), o.get(f)
            if f == "mac" and o.get("kind") == "hub" and o.get("mac_suspect"):
                continue
            if f == "label" and (my_label_default or (
                    o.get("kind") != "hub" and label_is_default(theirs, o.get("label_source")))):
                continue
            if same(f, mine, theirs):
                what = {"mac": "MAC", "ip": "IP", "label": "label"}[f]
                if o.get("kind") == "hub":
                    text = (f"this board reports {who}'s {what} {_shown(f, mine)} (the hub "
                            f"lists it for {who})")
                else:
                    text = f"this board reports the same {what} as {who}: {_shown(f, mine)}"
                out.append(_finding("clash", "err", text, field_name=f, other=who))
    # 2. the image default: every board gets the same one
    defaults = [f for f in FIELDS if source.get(f) == "default"]
    if not source:
        if reported.get("mac") and norm_mac(reported.get("mac")) == DEFAULT_MAC:
            defaults.append("mac")
        if reported.get("label") and my_label_default:
            defaults.insert(0, "label")
    if defaults:
        vals = ", ".join(f"{f} {_shown(f, reported.get(f))}" for f in defaults)
        names = ", ".join(_NAMES[f] for f in defaults)
        out.append(_finding("unset", "warn",
                            f"identity not set (default {names}): {vals} "
                            f"{'is' if len(defaults) == 1 else 'are'} the image default, which "
                            "every board gets", field_name=",".join(defaults)))
    # 3. the board's own hub record
    if hub:
        target = str(hub.get("target") or "its hub target")
        want = {"label": hub.get("label"), "ip": hub.get("board_ip"), "mac": hub.get("board_mac")}
        for f in ("label", "ip", "mac"):
            mine, theirs = reported.get(f), want.get(f)
            if not theirs or not mine or same(f, mine, theirs):
                continue
            if f == "mac" and hub.get("mac_suspect"):
                continue
            if f == "label" and my_label_default:
                continue            # "identity not set (default label)" already says it
            out.append(_finding("differs", "warn",
                                f"{f} {_shown(f, mine)} differs from the hub's {target}: "
                                f"{_shown(f, theirs)}", field_name=f, other=target))
        if hub.get("mac_suspect"):
            out.append(_finding("hub_suspect", "note",
                                f"hub record {target}: {hub['mac_suspect']}; the board says "
                                f"{_shown('mac', reported.get('mac')) or '?'}. Not compared, "
                                "and never proposed in a fix", field_name="mac", other=target))
    return out


def summarise(findings: Sequence[Mapping[str, Any]], reported: Mapping[str, Any] | None) -> str:
    if not reported:
        return STATUS_UNKNOWN
    kinds = {f["kind"] for f in findings}
    if "clash" in kinds:
        return STATUS_CLASH
    if "unset" in kinds:
        return STATUS_UNSET
    if "differs" in kinds:
        return STATUS_DIFFERS
    return STATUS_OK


def level_of(status: str) -> str:
    return {STATUS_CLASH: "err", STATUS_UNSET: "warn", STATUS_DIFFERS: "warn"}.get(status, "ok")


# --- the fix plan --------------------------------------------------------------------------------


def want_from_hub(hub: Mapping[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """What the hub record says this board should be, and the notes on what it left out."""
    if not hub:
        return {}, ["no hub record to match (the board has no hub, or the hub did not say)"]
    want: dict[str, str] = {}
    notes: list[str] = []
    if hub.get("label"):
        want["label"] = str(hub["label"])
    addr = ip_addr(hub.get("board_ip"))
    if addr:
        want["ip"] = f"{addr}/{hub.get('prefix') or 24}"
    mac = norm_mac(hub.get("board_mac"))
    if mac and not hub.get("mac_suspect") and mac_is_unicast_nonzero(mac):
        want["mac"] = mac
    elif mac:
        notes.append(f"the hub's board_mac {mac} is not used ({hub.get('mac_suspect') or 'not a unicast MAC'}); "
                     "give --mac to set one")
    return want, notes


def phrase_for(board_id: str, want: Mapping[str, Any], reported: Mapping[str, Any] | None) -> str:
    """What a person types to go ahead: the board's label after the change, else
    ``IDENTITY <board_id>``."""
    if want.get("label") == DROP and "label" in want:
        return f"IDENTITY {board_id}"           # the label goes back to the bake: not known here
    label = str(want.get("label") or (reported or {}).get("label") or "").strip()
    return label or f"IDENTITY {board_id}"


def _override_has(reported: Mapping[str, Any] | None, field_name: str) -> bool:
    ovr = (reported or {}).get("override")
    return isinstance(ovr, Mapping) and field_name in ovr


def plan_fix(board_id: str, reported: Mapping[str, Any] | None, hub: Mapping[str, Any] | None, *,
             want: Mapping[str, Any] | None = None, from_hub: bool = False,
             clear: bool = False) -> dict[str, Any]:
    """``{want, changes: [{field, from, to}], phrase, notes, clear}``. Pure."""
    notes: list[str] = []
    if clear:
        return {"want": {}, "clear": True, "phrase": f"IDENTITY {board_id}", "notes": notes,
                "changes": [{"field": "override", "from": "set" if (reported or {}).get("override")
                             else "none", "to": "cleared (the stage0 bake or the image default)"}]}
    if from_hub or not want:
        base, notes = want_from_hub(hub)
    else:
        base = {}
    asked = validate_want(dict(want or {}))
    full = {**base, **asked}
    changes = []
    for f in FIELDS:
        if f not in full:
            continue
        cur = (reported or {}).get(f)
        if full[f] == DROP:
            # V7-ALIGN: "" drops the key from the board's override; nothing to drop = no change
            if _override_has(reported, f):
                changes.append({"field": f, "from": _shown(f, cur) if f != "ip" else str(cur or ""),
                                "to": DROP, "drop": True})
            continue
        if same(f, cur, full[f]) and (f != "ip" or ip_prefix(cur) in (None, ip_prefix(full[f]))):
            continue
        changes.append({"field": f, "from": _shown(f, cur) if f != "ip" else str(cur or ""),
                        "to": full[f]})
    return {"want": {c["field"]: full[c["field"]] for c in changes}, "clear": False,
            "changes": changes, "phrase": phrase_for(board_id, full, reported), "notes": notes}


# --- the boards seen -----------------------------------------------------------------------------


class SeenIdentities:
    """``<state>/identity/seen.json``: ``{board_id: {label, ip, mac, hostname, target, address,
    at}}``, the last identity each board reported, and its last hub record."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self._mu = threading.Lock()

    @property
    def path(self) -> Path:
        return self.dir / "seen.json"

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, board_id: str) -> dict[str, Any]:
        with self._mu:
            rec = self._read().get(board_id)
        return dict(rec) if isinstance(rec, dict) else {}

    def all(self) -> dict[str, dict[str, Any]]:
        with self._mu:
            return {k: dict(v) for k, v in self._read().items() if isinstance(v, dict)}

    def update(self, board_id: str, **fields: Any) -> None:
        with self._mu:
            data = self._read()
            rec = data.get(board_id) if isinstance(data.get(board_id), dict) else {}
            rec.update(fields)
            data[board_id] = rec
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("could not record the identity of %s: %s", board_id, exc)

    def others(self, board_id: str, *, target: str = "", address: str = "",
               now: float | None = None) -> list[dict[str, Any]]:
        """Every OTHER board seen recently, as ``compare``'s ``others``. The same board under
        another id (the same hub target, or the same address) is not another board."""
        now = time.time() if now is None else now
        out = []
        for bid, rec in self.all().items():
            if bid == board_id:
                continue
            if target and rec.get("target") == target:
                continue
            if address and rec.get("address") == address:
                continue
            if now - float(rec.get("at") or 0) > SEEN_MAX_AGE_S:
                continue
            out.append({"who": rec.get("name") or bid, "kind": "board", "board_id": bid,
                        **{f: rec.get(f, "") for f in FIELDS},
                        "label_source": rec.get("label_source", "")})
        return out


# --- the service ---------------------------------------------------------------------------------


def _bus_of(engine: Any) -> EventBus | None:
    if isinstance(engine, EventBus):
        return engine
    return getattr(engine, "bus", None)


def _bid(session: Any) -> str:
    return str(getattr(getattr(session, "candidate", None), "board_id", "") or "")


_ERRORS: dict[str, type[HarnessError]] = {
    "UNAVAILABLE": UnavailableError, "REFUSED": RefusedError, "HELD": HeldError,
    "INCOMPATIBLE": IncompatibleError,
}


def refusal_error(name: str, message: str, hint: str = "") -> HarnessError:
    """``fix_reason``'s triple as the error every front end shows."""
    if name == "UNAVAILABLE":
        return UnavailableError(CAPABILITY, message)
    cls = _ERRORS.get(name, RefusedError)
    if cls is HeldError:
        return HeldError(message, hint=hint)
    return cls(message, hint=hint)


class IdentityService:
    """``engine.board_identity``. ``leases``: an object with ``view(hub) -> {lease: {mine,
    holder}}`` (the daemon gives its ``LeaseService``); by default one on the state dir."""

    def __init__(self, engine: Any = None, *, leases: Any = None,
                 seen: SeenIdentities | None = None) -> None:
        self.engine = None if isinstance(engine, EventBus) else engine
        self.bus = _bus_of(engine)
        self.leases = leases
        self._seen = seen
        self._last: dict[str, dict[str, Any]] = {}
        self._mu = threading.Lock()

    @property
    def state_dir(self) -> Path:
        sd = getattr(self.engine, "state_dir", None)
        if sd is not None:
            return Path(sd)
        from harness_manager.settings.files import config_dir

        return config_dir()

    @property
    def seen(self) -> SeenIdentities:
        if self._seen is None:
            self._seen = SeenIdentities(self.state_dir / "identity")
        return self._seen

    @staticmethod
    def adapter(session: Any) -> Any:
        ad = getattr(session, "net_identity", None)
        if ad is None:
            raise UnavailableError(CAPABILITY, NO_ADAPTER)
        return ad

    # -- reading --------------------------------------------------------------------------------

    def status(self, session: Any, *, refresh: bool = False,
               cheap: bool = False) -> dict[str, Any] | None:
        """What ``info`` (``cheap``: no control-port connection, no hub call) and
        ``board identity`` (``refresh``: the hub too) show; None with no adapter."""
        ad = getattr(session, "net_identity", None)
        if ad is None:
            return None
        bid = _bid(session)
        reported = ad.read(refresh=refresh, cheap=cheap)
        prev = self.seen.get(bid)
        if reported is None and cheap and any(prev.get(f) for f in FIELDS):
            # through a hub, info never asks: the last identity this board reported
            reported = {**{f: prev.get(f, "") for f in FIELDS}, "source": {}, "via": "last check",
                        "feature": False, "impl": "", "at": _iso(float(prev.get("at") or 0)),
                        "last_check": True}
        try:
            hub = ad.hub_record(refresh=refresh, cheap=cheap)
        except HarnessError as exc:
            hub = None
            hub_err = exc.message
        else:
            hub_err = ""
        if hub is None and not hub_err and isinstance(prev.get("hub"), dict) and cheap:
            hub = dict(prev["hub"], last_check=True)
        others: list[dict[str, Any]] = []
        if refresh:
            try:
                others += [{**o, "kind": "hub", "who": o.get("target") or "another target"}
                           for o in ad.hub_others(refresh=True)]
            except HarnessError as exc:
                hub_err = hub_err or exc.message
        elif isinstance(prev.get("hub_others"), list):
            others += [dict(o) for o in prev["hub_others"] if isinstance(o, dict)]
        target = str((hub or {}).get("target") or "")
        address = ad.address() if callable(getattr(ad, "address", None)) else ""
        others += self.seen.others(bid, target=target, address=address)
        findings = compare(reported, hub, others)
        status = summarise(findings, reported)
        fix = None
        if reported:
            plan = plan_fix(bid, reported, hub, from_hub=True)
            name, message, hint = ad.fix_reason(reported)
            fix = {**plan, "ready": not name and bool(plan["changes"]),
                   "refusal": {"name": name, "message": message, "hint": hint} if name else None}
        notes = [f["text"] for f in findings]
        if hub_err:
            notes.append(f"the hub record could not be read: {hub_err}")
        out = {"status": status, "level": level_of(status), "reported": reported, "hub": hub,
               "findings": findings, "fix": fix, "notes": notes,
               "checked_at": (reported or {}).get("at") or _iso(),
               "live": bool(reported) and not (reported or {}).get("last_check")}
        if reported and not reported.get("last_check"):
            rec: dict[str, Any] = {f: reported.get(f, "") for f in FIELDS}
            src = reported.get("source") if isinstance(reported.get("source"), Mapping) else {}
            rec["label_source"] = str(src.get("label") or "")     # V7-ALIGN: default = no clash
            rec.update(target=target, address=address, at=time.time(),
                       name=getattr(getattr(session, "candidate", None), "name", "") or "")
            if hub and not hub.get("last_check"):
                rec["hub"] = dict(hub)
            if refresh:
                rec["hub_others"] = [o for o in others if o.get("kind") == "hub"]
            same_report = all(prev.get(k) == rec.get(k)
                              for k in (*FIELDS, "label_source", "target", "address"))
            if not same_report or refresh or "hub" in rec and prev.get("hub") != rec["hub"] \
                    or time.time() - float(prev.get("at") or 0) > SEEN_REFRESH_S:
                self.seen.update(bid, **rec)
        self._publish(session, out)
        return out

    # -- the checks -----------------------------------------------------------------------------

    def _lease_service(self) -> Any:
        if self.leases is None:
            from harness_manager.services.lease import LeaseService

            self.leases = LeaseService(self.state_dir, self.bus)
        return self.leases

    def check_lease(self, session: Any) -> str:
        """``""`` with no hub; the holder when the lease is ours; else ``HeldError``."""
        from harness_manager.services.claim import ClaimService

        return ClaimService(self.bus, leases=self._lease_service()).check_lease(
            session, what="changing the board's identity")

    def check_ready(self, session: Any, reported: Mapping[str, Any] | None) -> None:
        """The board can take a fix now (bare metal, the image, the card, the claim)."""
        ad = self.adapter(session)
        if not reported:
            raise UnavailableError(CAPABILITY, "the board did not say what it is")
        name, message, hint = ad.fix_reason(reported)
        if name:
            raise refusal_error(name, message, hint)

    def refusal_first(self, session: Any, reported: Mapping[str, Any] | None) -> None:
        """V7-ALIGN: the board's order of refusals (net-protocol v0.16 ``identity_set``):
        ``locked``, then ``no_persist``, then ``invalid``. A value it would refuse is only
        reported once the board could take a change at all: call this before raising the
        ``UsageError`` for a bad value. No I/O beyond what ``read`` did."""
        ad = getattr(session, "net_identity", None)
        if ad is None or not reported:
            return
        name, message, hint = ad.fix_reason(reported)
        if name:
            raise refusal_error(name, message, hint)

    @staticmethod
    def check_reset(session: Any) -> None:
        """Never while the card is being written or read back: the fix ends in a reboot."""
        from harness_manager.services import reset_guard

        reset_guard.check(session, reset_guard.ACTION_HARNESS_REBOOT)

    # -- the fix --------------------------------------------------------------------------------

    def fix(self, session: Any, *, confirm: str, want: Mapping[str, Any] | None = None,
            from_hub: bool = False, clear: bool = False, wait_s: float | None = None,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Set the board's identity (``from_hub``: its hub record; ``want``: the fields given;
        ``clear``: drop the override), reboot it WARM and verify. ``confirm`` is the typed
        phrase (``plan.phrase``). Never automatic."""
        say = progress or (lambda text: None)
        ad = self.adapter(session)
        bid = _bid(session)
        say("reading the board's identity and its hub record")
        before = self.status(session, refresh=True) or {}
        reported = before.get("reported")
        try:
            plan = plan_fix(bid, reported, before.get("hub"), want=want, from_hub=from_hub,
                            clear=clear)
        except UsageError:
            self.refusal_first(session, reported)
            raise
        if not plan["changes"] or (not clear and not plan["want"]):
            return {"board_id": bid, "action": "none", "changes": [], "verified": True,
                    "identity": before, "notes": ["the board already matches: nothing to do"]
                    + plan["notes"]}
        self.check_ready(session, reported)
        self.check_lease(session)
        self.check_reset(session)
        if str(confirm or "").strip() != plan["phrase"]:
            raise RefusedError("changing a board's identity needs the typed phrase: nothing was "
                               "changed", hint=f"type exactly: {plan['phrase']}")
        body = {"clear": True} if clear else dict(plan["want"])
        say("setting " + (", ".join(change_text(c) for c in plan["changes"])
                          if not clear else "the identity back to the stage0 bake"))
        set_reply = ad.set_identity(body)
        say("rebooting the harness (warm: the reboot verb; never an MCC REBOOT)")
        witness = ad.warm_reboot(lambda p, d, t: say(f"reboot {d}/{t} s"),
                                 REBOOT_WAIT_S if wait_s is None else wait_s)
        say("verifying")
        after_reported = None
        verify_err = ""
        try:
            after_reported = ad.read(refresh=True)
        except HarnessError as exc:
            verify_err = exc.message
        after = self.status(session, refresh=False) if after_reported else None
        verified, mismatched = _verified(plan, after_reported, clear=clear)
        notes = list(plan["notes"])
        if verify_err:
            moved = plan["want"].get("ip")
            notes.append(f"the board did not answer after the reboot ({verify_err})"
                         + (f"; its IP is now {moved}: open it there" if moved else ""))
        elif mismatched:
            notes.append("after the reboot the board still reports "
                         + ", ".join(f"{f} {_shown(f, (after_reported or {}).get(f))}"
                                     for f in mismatched))
        out = {"board_id": bid, "action": "cleared" if clear else "set",
               "changes": plan["changes"], "set": set_reply, "reboot": witness,
               "verified": verified, "identity": after or before, "notes": notes}
        if not verified:
            why = notes[-1] if notes else "it was not read back"
            raise ActionFailedError(
                f"the identity was set and the board rebooted, but it does not report the new "
                f"identity: {why}",
                hint="`harness-manager board identity TARGET` shows what it reports now")
        return out

    def _publish(self, session: Any, status: Mapping[str, Any]) -> None:
        bid = _bid(session)
        key = {k: status.get(k) for k in ("status", "reported", "hub")}
        with self._mu:
            changed = self._last.get(bid) != key
            self._last[bid] = key
        if not changed or self.bus is None:
            return
        rep = status.get("reported") or {}
        self.bus.publish(Event("board.net_identity", bid, {
            "status": status.get("status"),
            "reported": {f: rep.get(f, "") for f in FIELDS},
            "hub": {k: (status.get("hub") or {}).get(k) for k in ("target", "label", "board_ip",
                                                                   "board_mac")}}))


def change_text(change: Mapping[str, Any]) -> str:
    """One planned change in words: ``label MPS3-02``, or ``hostname dropped (...)``."""
    if change.get("drop"):
        return f"{change['field']} dropped from the board's own setting (back to the stage0 bake, " \
               "else the image default)"
    return f"{change['field']} {change['to']}"


def _verified(plan: Mapping[str, Any], after: Mapping[str, Any] | None, *,
              clear: bool) -> tuple[bool, list[str]]:
    if not after:
        return False, []
    if clear:
        return not after.get("override") and not after.get("pending"), []
    src = after.get("source") if isinstance(after.get("source"), Mapping) else {}
    bad = [f for f, v in plan["want"].items()
           if (src.get(f) == "override" or _override_has(after, f) if v == DROP
               else not same(f, after.get(f), v))]
    if after.get("pending"):
        bad.append("pending")
    return not bad, bad
