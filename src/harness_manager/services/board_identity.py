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
#: How long a board whose address changed is looked for at its new address (the Linux lead,
#: 2 Oct: it comes back on the new IP after ~2-3 min).
MOVE_WAIT_S = 240.0

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
            # lane IDENTITY (david 2 Oct): upper-cased, 1-16 (the aligned panel shows 16;
            # the board itself takes 19, LABEL_MAX)
            from harness_manager.services.identity_assign import check_name

            label = check_name(text)
            assert _LABEL_RE.match(label)
            out[key] = label
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
            self._write(data, board_id)

    # -- the registry (lane IDENTITY): every MAC and IP assigned or seen, per board ----------

    def record(self, board_id: str, *, mac: Any = "", ip: Any = "", how: str = "seen",
               at: float | None = None) -> None:
        """Add ``mac``/``ip`` to the board's history (``history: {mac|ip: {value: {how, first,
        last}}}``). ``how`` is ``assigned`` (Harness Manager gave it) or ``seen`` (the board
        reported it); ``assigned`` is never downgraded."""
        vals = {"mac": norm_mac(mac), "ip": ip_addr(ip)}
        if not any(vals.values()):
            return
        when = _iso(at)
        with self._mu:
            data = self._read()
            rec = data.get(board_id) if isinstance(data.get(board_id), dict) else {}
            hist = rec.get("history") if isinstance(rec.get("history"), dict) else {}
            changed = False
            for f, v in vals.items():
                if not v:
                    continue
                seen = hist.get(f) if isinstance(hist.get(f), dict) else {}
                prev = seen.get(v) if isinstance(seen.get(v), dict) else None
                keep = "assigned" if how == "assigned" or (prev or {}).get("how") == "assigned" \
                    else "seen"
                entry = {"how": keep, "first": (prev or {}).get("first") or when, "last": when}
                if prev != entry:
                    seen[v] = entry
                    hist[f] = seen
                    changed = True
            if not changed:
                return
            rec["history"] = hist
            data[board_id] = rec
            self._write(data, board_id)

    def taken(self, field_name: str) -> dict[str, list[str]]:
        """Every ``mac`` or ``ip`` in the registry (assigned or seen, any age), with the
        boards it belongs to: the current report and the history."""
        out: dict[str, list[str]] = {}
        norm = norm_mac if field_name == "mac" else ip_addr
        for bid, rec in self.all().items():
            vals = {norm(rec.get(field_name))}
            hist = rec.get("history") if isinstance(rec.get("history"), dict) else {}
            got = hist.get(field_name) if isinstance(hist.get(field_name), dict) else {}
            vals |= {norm(v) for v in got}
            for v in vals - {""}:
                out.setdefault(v, []).append(bid)
        return out

    def move(self, old_id: str, new_id: str) -> None:
        """The board's record under its new id (its address changed): the histories merge,
        the newer report wins."""
        if old_id == new_id:
            return
        with self._mu:
            data = self._read()
            old = data.pop(old_id, None)
            if not isinstance(old, dict):
                return
            new = data.get(new_id) if isinstance(data.get(new_id), dict) else {}
            merged = {**new, **old} if float(old.get("at") or 0) >= float(new.get("at") or 0) \
                else {**old, **new}
            hist: dict[str, Any] = {}
            for rec in (new, old):
                for f, vals in (rec.get("history") or {}).items():
                    if isinstance(vals, dict):
                        hist.setdefault(f, {}).update(vals)
            if hist:
                merged["history"] = hist
            merged["moved_from"] = old_id
            data[new_id] = merged
            self._write(data, new_id)

    def _write(self, data: dict[str, Any], board_id: str) -> None:
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
                self.seen.record(bid, mac=rec.get("mac"), ip=rec.get("ip"))   # the registry
        self._publish(session, out)
        return out

    # -- naming a board: the choices, the guards, the proposal (lane IDENTITY) -------------------

    def policy(self, session: Any = None) -> Any:
        """The board pack's ``IdentityPolicy`` (the adapter's, else the engine's pack's)."""
        from harness_manager.services import identity_assign as IA

        ad = getattr(session, "net_identity", None) if session is not None else None
        own = getattr(ad, "policy", None)
        if callable(own):
            got = own()
            if isinstance(got, IA.IdentityPolicy):
                return got
        pack = str(getattr(getattr(session, "candidate", None), "pack", "") or "mps3")
        return IA.policy_for(self.engine, pack)

    @staticmethod
    def _hub_name(session: Any, hub: Mapping[str, Any] | None) -> str:
        """The hub the board sits behind, as a person names it; ``""`` with no hub."""
        name = str((hub or {}).get("hub") or "")
        live = getattr(session, "hub", None)
        if not name and live is not None:
            name = str(getattr(live, "host", "") or "")
        if not name and (hub or live is not None):
            name = str((hub or {}).get("target") or getattr(live, "target", "") or "the hub")
        return name

    def choose(self, session: Any, st: Mapping[str, Any] | None, *, mac: Any = None,
               ip: Any = None) -> dict[str, Any]:
        """``mac`` random/keep/value and ``ip`` auto/keep/value as values, from the pack's
        policy and the registry (``seen.json``). A board behind a hub takes the hub's
        address, never one from this PC's pool."""
        from harness_manager.services import identity_assign as IA

        st = st or {}
        if str(ip or "").strip().lower() == IA.IP_AUTO and \
                self._hub_name(session, st.get("hub")):
            hub = self._hub_name(session, st.get("hub"))
            raise RefusedError(f"--ip auto picks from this PC's own network; this board is "
                               f"behind the hub {hub}, whose record gives it its address: "
                               "nothing was changed",
                               hint="keep its IP, or match the hub entry (--from-hub)")
        return IA.resolve(self.policy(session), mac=mac, ip=ip, current=st.get("reported"),
                          taken_macs=self.seen.taken("mac"), taken_ips=self.seen.taken("ip"))

    def check_values(self, session: Any, want: Mapping[str, Any]) -> dict[str, Any]:
        """A person's own ``mac``/``ip`` against the pack's rules (unicast, not the image's
        range; IPv4 in a /24): ``UsageError`` naming the rule. ``random``/``auto``, ``""``
        (drop) and None pass through, as does every other field (``validate_want`` checks
        those)."""
        from harness_manager.services import identity_assign as IA

        out = dict(want)
        policy = None
        for k in ("mac", "ip"):
            v = out.get(k)
            if v in (None, DROP) or str(v).strip().lower() in (IA.MAC_RANDOM, IA.IP_AUTO):
                continue
            if k == "mac":
                policy = policy or self.policy(session)
                out[k] = IA.check_mac(v, policy)
            else:
                out[k] = IA.check_ip(v)
        return out

    def precheck(self, session: Any, st: Mapping[str, Any] | None, want: Mapping[str, Any], *,
                 from_hub: bool = False, hub_fixed: str = "",
                 other_subnet: bool = False, confirm_subnet: bool = False) -> list[str]:
        """The API's refusals before its job (nothing is chosen, set or sent): the values
        (400), and the hub and subnet guards (409). ``random``/``auto`` count as a change; an
        auto IP's own subnet check waits for the job, which picks it."""
        from harness_manager.services import identity_assign as IA

        st = st or {}
        w = self.check_values(session, want)
        picks = {k for k in ("mac", "ip")
                 if str(w.get(k) or "").strip().lower() in (IA.MAC_RANDOM, IA.IP_AUTO)}
        if "ip" in picks and self._hub_name(session, st.get("hub")):
            self.choose(session, st, ip=IA.IP_AUTO)          # the hub's refusal
        plan = plan_fix(_bid(session), st.get("reported"), st.get("hub"),
                        want={k: v for k, v in w.items() if k not in picks} or None,
                        from_hub=from_hub)
        if "mac" in picks:
            plan = {**plan, "want": {**plan["want"], "mac": "02:ff:ff:ff:ff:fe"}}  # "changes"
        return self.guards(session, st, plan, from_hub=from_hub, hub_fixed=hub_fixed,
                           other_subnet=other_subnet, confirm_subnet=confirm_subnet)

    def _moving(self, session: Any, reported: Mapping[str, Any] | None, new_ip: Any) -> str:
        """The address this session reaches the board at, when that is the address about to
        change (the board is reached at its own identity IP, or its DHCP lease); else ``""``.
        A board reached through a hub or a tunnel (loopback) does not move for this PC."""
        addr = ip_addr(new_ip)
        ad = getattr(session, "net_identity", None)
        where = ad.address() if callable(getattr(ad, "address", None)) else ""
        host = where.rsplit(":", 1)[0].strip("[]") if where else ""
        cur = {ip_addr((reported or {}).get("ip")), ip_addr((reported or {}).get("lease"))}
        if not addr or not host or host not in cur - {""} or addr == host:
            return ""
        return host

    def guards(self, session: Any, st: Mapping[str, Any] | None, plan: Mapping[str, Any], *,
               from_hub: bool = False, hub_fixed: str = "", other_subnet: bool = False,
               confirm_subnet: bool = False) -> list[str]:
        """The refusals a name change meets BEFORE anything is sent (``RefusedError``), and the
        notes it carries. The hub guard: a board behind a hub is given its address by the hub's
        DHCP (dnsmasq), keyed on its MAC, so its MAC (or IP) changes only once the person names
        the hub (``hub_fixed``) to say its record was fixed first. The subnet guard: a new IP
        outside this PC's /24 needs ``other_subnet``."""
        from harness_manager.services import identity_assign as IA

        st = st or {}
        reported = st.get("reported") or {}
        want = plan.get("want") or {}
        hub = st.get("hub") if isinstance(st.get("hub"), Mapping) else None
        hub_name = self._hub_name(session, hub)
        policy = self.policy(session)
        notes: list[str] = []
        new_mac = want.get("mac") if want.get("mac") not in (None, DROP) else ""
        new_ip = want.get("ip") if want.get("ip") not in (None, DROP) else ""
        cur_ip = ip_addr(reported.get("ip"))
        if (new_ip and cur_ip and IA.outside_pool(policy, cur_ip)
                and IA.network_of(new_ip) != IA.network_of(cur_ip) and not confirm_subnet):
            raise RefusedError(
                IA.confirm_subnet_text(policy, cur_ip, new_ip) + "; nothing was changed",
                hint=f"keep the board's address, or give one in {IA.network_of(cur_ip)}, or "
                     "confirm that the board's network will reach the new one: "
                     "--allow-other-subnet (API: confirm_subnet: true)")
        if hub_name:
            target = str((hub or {}).get("target") or getattr(getattr(session, "hub", None),
                                                             "target", "") or "its hub target")
            bad = dict(policy.known_bad_hub_records)
            changes_mac = bool(new_mac) and not (
                from_hub and same("mac", new_mac, (hub or {}).get("board_mac")))
            changes_ip = bool(new_ip) and not same("ip", new_ip, (hub or {}).get("board_ip"))
            if (changes_mac or changes_ip) and str(hub_fixed or "").strip() != hub_name:
                what = " and ".join(w for w, on in (("MAC", changes_mac), ("IP", changes_ip))
                                    if on)
                known = "; ".join(f"{t}'s hub record is known to be wrong today ({why})"
                                  for t, why in bad.items())
                raise RefusedError(
                    f"this board is behind the hub {hub_name}, whose DHCP (dnsmasq) knows it by "
                    f"its MAC ({target}): changing its {what} before the hub's record is fixed "
                    "loses the board's address"
                    + (f". {known[0].upper()}{known[1:]}" if known else "")
                    + ". Nothing was changed",
                    hint=f"fix {target}'s record on {hub_name} first (fpgahub), then confirm by "
                         f"naming the hub: --hub-fixed {hub_name}")
        elif new_ip and not same("ip", new_ip, reported.get("ip")):
            ad = getattr(session, "net_identity", None)
            where = ad.address() if callable(getattr(ad, "address", None)) else ""
            local = IA.local_address_toward(where.rsplit(":", 1)[0].strip("[]")) if where else ""
            chk = IA.subnet_check(new_ip, local)
            if chk["same"] is False and not other_subnet:
                raise RefusedError(IA.other_subnet_text(new_ip, chk) + "; nothing was changed",
                                   hint=f"give an address in {chk['network']} (--ip auto picks "
                                        "one), or confirm the other network: --other-subnet")
            if chk["same"] is False:
                notes.append(IA.other_subnet_text(new_ip, chk))
        if new_mac and not new_ip and not hub_name and not same("mac", new_mac,
                                                                reported.get("mac")):
            notes.append(IA.arp_note(reported.get("ip")))
        for f, value in (("mac", new_mac), ("ip", new_ip)):
            holders = [b for b in self.seen.taken(f).get(
                norm_mac(value) if f == "mac" else ip_addr(value), []) if b != _bid(session)]
            if value and holders:
                notes.append(f"{_NAMES[f]} {_shown(f, value)} is in this Harness Manager's "
                             f"registry for {', '.join(holders)} (seen.json)")
        if policy.rescue_note:
            notes.append(policy.rescue_note)
        return notes

    def propose(self, session: Any, *, label: Any = None, mac: Any = None,
                ip: Any = None) -> dict[str, Any]:
        """What the "Name this board" dialog shows (``GET .../identity/proposal``): the name
        upper-cased and checked, a random MAC (or keep, or a value), an IP from the pool (or
        keep, or a value), the rules, the same-/24 line, the hub guard and the notes. Nothing
        is set, written or reserved; ``mac=random`` gives a new MAC each time."""
        from harness_manager.services import identity_assign as IA

        bid = _bid(session)
        st = self.status(session, cheap=True) or {}
        reported = st.get("reported") or {}
        policy = self.policy(session)
        hub = st.get("hub") if isinstance(st.get("hub"), Mapping) else None
        hub_name = self._hub_name(session, hub)
        cur_mac, cur_ip = norm_mac(reported.get("mac")), ip_addr(reported.get("ip"))
        default_mac = IA.KEEP if hub_name or (cur_mac and not IA.mac_problem(cur_mac, policy)
                                              and cur_mac != DEFAULT_MAC) else IA.MAC_RANDOM
        default_ip = IA.KEEP if hub_name or (
            cur_ip and cur_ip not in {ip_addr(x) for x in policy.reserved_ips}
            and cur_ip != DEFAULT_IP and not IA.ip_problem(cur_ip)[0]) else IA.IP_AUTO
        if not policy.ip_pool and default_ip == IA.IP_AUTO:
            default_ip = IA.KEEP
        mac = default_mac if mac in (None, "") else mac
        ip = default_ip if ip in (None, "") else ip
        # a board outside the pool's /24: a pool address would lose it, so its own is kept
        off_pool = bool(cur_ip) and IA.outside_pool(policy, cur_ip)
        usable_ip = bool(cur_ip) and not IA.ip_problem(cur_ip)[0]
        kept_for_subnet = off_pool and usable_ip and str(ip).strip().lower() == IA.IP_AUTO \
            and not hub_name
        if kept_for_subnet:
            ip = IA.KEEP
        # keep the current MAC only when the MPS3 rules would allow it (not the image's range)
        mac_reserved = IA.mac_problem(cur_mac, policy) if cur_mac else ""
        keep_allowed = bool(cur_mac) and (not mac_reserved or bool(hub_name))
        mac_why = ""
        if cur_mac and not keep_allowed:
            mac_why = (f"the current MAC {cur_mac} is the image's default range, which every "
                       "board starts with: a new random MAC is required")
            if str(mac).strip().lower() == IA.KEEP:
                mac = IA.MAC_RANDOM
        out: dict[str, Any] = {"board_id": bid, "current": {f: reported.get(f, "") for f in FIELDS},
                               "defaults": {"mac": default_mac, "ip": default_ip}}
        name = IA.normalize_name(label if label not in (None, "") else reported.get("label"))
        out["label"] = name
        out["label_problem"] = IA.name_problem(name) if name else "it is empty"
        errors: dict[str, str] = {}
        chosen: dict[str, Any] = {"mac": None, "mac_how": "", "ip": None, "ip_how": ""}
        for key, value in (("mac", mac), ("ip", ip)):
            try:
                got = self.choose(session, st, **{key: value})
            except HarnessError as exc:
                errors[key] = exc.message + (f" ({exc.hint})" if exc.hint else "")
                chosen[f"{key}_how"] = str(value)
                continue
            chosen[key], chosen[f"{key}_how"] = got[key], got[f"{key}_how"]
        out.update(chosen, errors=errors)
        out["mac_keep"] = {"allowed": keep_allowed, "why": mac_why}
        new_cmp = chosen["ip"] or ""
        out["subnet"] = {
            "outside_pool": off_pool, "board_network": IA.network_of(cur_ip) if cur_ip else "",
            "pool_network": IA.pool_network(policy), "kept": kept_for_subnet,
            "warning": IA.outside_pool_text(policy, cur_ip) if off_pool else "",
            "confirm_needed": bool(off_pool and new_cmp
                                   and IA.network_of(new_cmp) != IA.network_of(cur_ip)),
            "confirm_text": ("The board's network will reach this address: set it anyway"
                             if off_pool else "")}
        new_ip = chosen["ip"] or (f"{cur_ip}/24" if cur_ip else "")
        want = {k: v for k, v in (("label", name if not out["label_problem"] else None),
                                  ("mac", chosen["mac"]), ("ip", chosen["ip"])) if v}
        changes = [{"field": f, "from": _shown(f, reported.get(f)), "to": _shown(f, v)}
                   for f, v in want.items() if not same(f, reported.get(f), v)]
        out["changes"] = changes
        out["phrase"] = name if name and not out["label_problem"] else \
            (str(reported.get("label") or "") or f"IDENTITY {bid}")
        ad = getattr(session, "net_identity", None)
        where = ad.address() if callable(getattr(ad, "address", None)) else ""
        local = "" if hub_name else IA.local_address_toward(where.rsplit(":", 1)[0].strip("[]"))
        out["address"] = {"ip": ip_addr(new_ip), "same_net": IA.same_net_note(new_ip),
                          "pc_example": IA.pc_example(new_ip),
                          "subnet": IA.subnet_check(new_ip, local),
                          "moving": bool(self._moving(session, reported, chosen["ip"]))}
        out["pool"] = {"range": policy.ip_pool, "setting": policy.ip_pool_setting}
        out["rules"] = {"name_max": IA.NAME_MAX, "name": IA.NAME_RULE,
                        "mac": "unicast, not all zeros"
                               + "".join(f", not {p}:*" for p in policy.reserved_mac_prefixes),
                        "mac_reserved": list(policy.reserved_mac_prefixes),
                        "ip": "IPv4, a host address of a /24"}
        hub_out = None
        if hub_name:
            target = str((hub or {}).get("target") or "")
            hub_out = {"name": hub_name, "target": target, "record": dict(hub or {}),
                       "known_bad": dict(policy.known_bad_hub_records),
                       "guard": bool(chosen["mac"] and not same("mac", chosen["mac"], cur_mac))
                       or bool(chosen["ip"] and not same("ip", chosen["ip"], cur_ip))}
        out["hub"] = hub_out
        notes: list[str] = []
        if chosen["mac"] and not same("mac", chosen["mac"], cur_mac) and not hub_name and (
                not chosen["ip"] or same("ip", chosen["ip"], cur_ip)):
            notes.append(IA.arp_note(cur_ip))
        if policy.rescue_note:
            notes.append(policy.rescue_note)
        out["notes"] = notes
        out["rescue_note"] = policy.rescue_note
        fix = st.get("fix") or {}
        out["refusal"] = fix.get("refusal")
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

    def preflight(self, session: Any, *, want: Mapping[str, Any] | None = None,
                  from_hub: bool = False, clear: bool = False, hub_fixed: str = "",
                  other_subnet: bool = False, confirm_subnet: bool = False) -> dict[str, Any]:
        """Everything ``fix`` checks before it asks for the phrase, nothing sent or reserved:
        the board read again, the values (the board's order: its refusal, then a bad value),
        ``random``/``auto`` chosen, the plan, the hub and subnet guards. ``{want, plan, notes,
        identity, address}``; ``want`` has the chosen values, for ``fix`` (lane IDENTITY: what
        the CLI shows is what is set, also through the service: ``POST .../identity``
        ``dry_run``)."""
        from harness_manager.services import identity_assign as IA

        bid = _bid(session)
        self.adapter(session)
        before = self.status(session, refresh=True) or {}
        reported = before.get("reported")
        try:
            want = self.check_values(session, dict(want or {}))
        except UsageError:
            self.refusal_first(session, reported)
            raise
        picks = {k: want[k] for k in ("mac", "ip")
                 if str(want.get(k) or "").strip().lower() in (IA.MAC_RANDOM, IA.IP_AUTO)}
        if picks:
            self.refusal_first(session, reported)
            got = self.choose(session, before, **picks)
            want.update({k: got[k] for k in picks})
        try:
            plan = plan_fix(bid, reported, before.get("hub"), want=want or None,
                            from_hub=from_hub, clear=clear)
        except UsageError:
            self.refusal_first(session, reported)
            raise
        notes: list[str] = []
        if plan["changes"] and (clear or plan["want"]):
            self.check_ready(session, reported)
            if not clear:
                notes = self.guards(session, before, plan, from_hub=from_hub,
                                    hub_fixed=hub_fixed, other_subnet=other_subnet,
                                    confirm_subnet=confirm_subnet)
        new_ip = "" if clear else str(plan["want"].get("ip") or "")
        return {"want": want, "plan": plan, "notes": notes, "identity": before,
                "address": self._address_out(new_ip)}

    def fix(self, session: Any, *, confirm: str, want: Mapping[str, Any] | None = None,
            from_hub: bool = False, clear: bool = False, wait_s: float | None = None,
            progress: Callable[[str], None] | None = None, hub_fixed: str = "",
            other_subnet: bool = False, confirm_subnet: bool = False) -> dict[str, Any]:
        """Set the board's identity (``from_hub``: its hub record; ``want``: the fields given,
        ``mac: "random"`` and ``ip: "auto"`` chosen here; ``clear``: drop the override), reboot
        it WARM and verify. ``confirm`` is the typed phrase (``plan.phrase``); ``hub_fixed``
        names the hub when a hub board's MAC or IP changes; ``other_subnet`` lets the new IP
        leave this PC's /24. Never automatic.

        A board this session reaches at the address that changes MOVES (lane IDENTITY, the
        Linux lead 2 Oct): its SSH host key is read first, the warm reboot is not witnessed at
        the old address (the board drops it), the board is found at the new IP by identify and
        accepted only when its host key is the one pinned for it; then its records (boards.toml,
        the claim, the registry) follow it to its new board id (``moved``)."""
        from harness_manager.services import identity_assign as IA

        say = progress or (lambda text: None)
        ad = self.adapter(session)
        bid = _bid(session)
        say("reading the board's identity and its hub record")
        before = self.status(session, refresh=True) or {}
        reported = before.get("reported")
        try:
            want = self.check_values(session, dict(want or {}))
        except UsageError:
            self.refusal_first(session, reported)           # the board's order: invalid last
            raise
        picks = {k: want[k] for k in ("mac", "ip")
                 if str(want.get(k) or "").strip().lower() in (IA.MAC_RANDOM, IA.IP_AUTO)}
        if picks:
            self.refusal_first(session, reported)           # the board's order first
            got = self.choose(session, before, **picks)
            for k in picks:
                want[k] = got[k]
                say(f"{k} {got[k]} ({got[f'{k}_how']})")
        try:
            plan = plan_fix(bid, reported, before.get("hub"), want=want or None,
                            from_hub=from_hub, clear=clear)
        except UsageError:
            self.refusal_first(session, reported)
            raise
        if not plan["changes"] or (not clear and not plan["want"]):
            return {"board_id": bid, "action": "none", "changes": [], "verified": True,
                    "identity": before, "notes": ["the board already matches: nothing to do"]
                    + plan["notes"]}
        self.check_ready(session, reported)
        guard_notes = [] if clear else self.guards(session, before, plan, from_hub=from_hub,
                                                   hub_fixed=hub_fixed,
                                                   other_subnet=other_subnet,
                                                   confirm_subnet=confirm_subnet)
        self.check_lease(session)
        self.check_reset(session)
        if str(confirm or "").strip() != plan["phrase"]:
            raise RefusedError("changing a board's identity needs the typed phrase: nothing was "
                               "changed", hint=f"type exactly: {plan['phrase']}")
        new_ip = "" if clear else str(plan["want"].get("ip") or "")
        old_host = self._moving(session, reported, new_ip)
        host_key = ""
        if old_host:
            find = getattr(ad, "relocate", None)
            key_of = getattr(ad, "host_key", None)
            if not callable(find) or not callable(key_of):
                raise RefusedError(f"this session reaches the board at {old_host}, the address "
                                   "that changes, and its pack cannot follow it to the new one: "
                                   "nothing was changed",
                                   hint="keep its IP, or reach it another way first")
            host_key = str(key_of() or "")
            if not host_key:
                raise RefusedError("the board's SSH host key is not pinned here, so after the "
                                   f"restart Harness Manager could not tell it from another board "
                                   f"at {ip_addr(new_ip)}: nothing was changed",
                                   hint="claim it first: `harness-manager board claim TARGET`")
            say(f"host key {host_key}: the board must show it at {ip_addr(new_ip)}")
        body = {"clear": True} if clear else dict(plan["want"])
        say("setting " + (", ".join(change_text(c) for c in plan["changes"])
                          if not clear else "the identity back to the stage0 bake"))
        set_reply = ad.set_identity(body)
        if not clear:
            self.seen.record(bid, mac=plan["want"].get("mac") or "",
                             ip=plan["want"].get("ip") or "", how="assigned")
        notes = list(plan["notes"]) + guard_notes
        if old_host:
            return self._fix_moving(session, ad, plan, set_reply, notes, old_host=old_host,
                                    new_ip=new_ip, host_key=host_key, wait_s=wait_s, say=say)
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
               "verified": verified, "identity": after or before, "notes": notes,
               "moved": None, "address": self._address_out(new_ip)}
        if not verified:
            why = notes[-1] if notes else "it was not read back"
            raise ActionFailedError(
                f"the identity was set and the board rebooted, but it does not report the new "
                f"identity: {why}",
                hint="`harness-manager board identity TARGET` shows what it reports now")
        return out

    @staticmethod
    def _address_out(new_ip: str) -> dict[str, str] | None:
        from harness_manager.services import identity_assign as IA

        if not ip_addr(new_ip):
            return None
        return {"ip": ip_addr(new_ip), "same_net": IA.same_net_note(new_ip),
                "pc_example": IA.pc_example(new_ip)}

    def _fix_moving(self, session: Any, ad: Any, plan: Mapping[str, Any], set_reply: Any,
                    notes: list[str], *, old_host: str, new_ip: str, host_key: str,
                    wait_s: float | None, say: Callable[[str], None]) -> dict[str, Any]:
        """The warm reboot of a board whose address changes, and finding it again."""
        bid = _bid(session)
        addr = ip_addr(new_ip)
        say(f"rebooting the harness (warm: the reboot verb); it comes back at {addr}, not "
            f"{old_host}")
        found = ad.relocate(addr, host_key, wait_s=MOVE_WAIT_S if wait_s is None else wait_s,
                            progress=say)
        new_id = str(found.get("board_id") or "")
        after_reported = found.get("reported") or {}
        elsewhere = str(found.get("elsewhere") or "")      # found by a broadcast, not at addr
        if found.get("note"):
            notes.append(str(found["note"]))
        say(f"found at {found.get('address') or addr} with host key {host_key}")
        records: list[str] = []
        if new_id and new_id != bid:
            records = list(ad.adopt_move(bid, new_id, old_host=old_host,
                                         new_host=elsewhere or addr) or [])
            self.seen.move(bid, new_id)
            records.append(f"seen.json: {bid} -> {new_id}")
            for line in records:
                say(line)
        key = new_id or bid
        rec = {f: after_reported.get(f, "") for f in FIELDS}
        src = after_reported.get("source") if isinstance(after_reported.get("source"),
                                                         Mapping) else {}
        self.seen.update(key, **rec, label_source=str(src.get("label") or ""),
                         address=str(found.get("address") or ""), at=time.time())
        self.seen.record(key, mac=rec.get("mac"), ip=rec.get("ip"))
        checked = {**plan, "want": {f: v for f, v in plan["want"].items()
                                    if f in after_reported and after_reported.get(f) != ""
                                    and not (f == "ip" and elsewhere)}}
        verified, mismatched = _verified(checked, after_reported, clear=False)
        if mismatched:
            notes.append("at its new address the board reports "
                         + ", ".join(f"{f} {_shown(f, after_reported.get(f))}"
                                     for f in mismatched))
        findings = compare(after_reported, None, self.seen.others(key))
        status = summarise(findings, after_reported)
        identity = {"status": status, "level": level_of(status), "reported": after_reported,
                    "hub": None, "findings": findings, "fix": None,
                    "notes": [f["text"] for f in findings], "checked_at": _iso(), "live": True}
        moved = {"from": bid, "to": key, "address": str(found.get("address") or ""),
                 "host": elsewhere or addr, "old_host": old_host, "records": records,
                 "elsewhere": bool(elsewhere)}
        out = {"board_id": key, "action": "set", "changes": plan["changes"], "set": set_reply,
               "reboot": found.get("reboot") or {}, "verified": verified, "identity": identity,
               "notes": notes, "moved": moved,
               "address": self._address_out(elsewhere or new_ip)}
        if not verified:
            raise ActionFailedError(
                f"the board moved to {addr}, but it does not report the new identity: "
                f"{notes[-1]}", hint=f"`harness-manager board identity {addr}` shows what it "
                                     "reports now")
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
