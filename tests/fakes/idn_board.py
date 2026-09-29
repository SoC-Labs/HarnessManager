"""The Linux harness's identity verbs (net-protocol v0.16), for the BOARD-ID tests.

HM's OWN model of the Linux lead's contract, ALIGNED (lane V7-ALIGN) to what shipped in platform
``feat/linux-harness`` 18622e5 (images rc2_v7/v7n): the rules, the refusal texts and their
order are those of pyverify's own model there (``pyverify.testing.fakeshell._IdentityModel``,
``identity_check``), written into this file when HM's vendored pyverify predated it. The
vendored pyverify has it since platform 3f7cea2; tests/integration/test_pyverify_vendored.py holds
this model to it (the rules, the refusal texts):

- ``identity`` (a read, any peer): ``{label, hostname, ip "a.b.c.d/n", mac (12 hex), source,
  stage0, override, pending, persist}``. Each field resolves override > stage0 > the image
  default (label ``MPS3``, 192.168.10.101/24, 02:00:00:4d:50:53, source ``default``); the
  hostname follows the label (source ``label``) unless set;
- ``identity_set`` (CLAIM-LOCKED for any peer but the board itself, like ``slot``/``usd``):
  any of ``{label, hostname, ip, mac}`` (``""`` DROPS that key from the override) or
  ``{"clear": true}``; refusals in the board's order: ``locked``, ``no_persist`` (a board with
  no card: a netboot), ``invalid`` ("invalid <field>: <why>"); the values go PENDING and apply
  at the next ``reboot`` (the WARM verb: ``_simulate_restart``). The reply carries ``op``
  (``reply_op=False``: a draft's reply without it);
- ``version.features`` gains ``identity`` (``has_identity=False``: an image without the verbs,
  which answers ``unknown op``, or with ``decline="not_supported"`` the v0.16 bare-metal
  coordinator's ``identity not supported``); identify's ``mac``/``ip`` follow the running
  identity, and it carries ``label`` after ``ssh`` and before ``ports``.

``IdentityBoard`` is ``lxslots_board.SlotBoard`` (the slot verbs, the claim lock, the reboot
that restarts the harness) plus the above. ``FakeHub``/``FakeHubClient`` stand in for the
session's hub adapter (``fpgahub target show`` / ``board list``); ``FakeLeases`` for the lease
service; ``SpyController`` for the MCC, to prove it is never asked to reboot.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from typing import Any

from tests.fakes.lxslots_board import LINUX_SID, SlotBoard

DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": "0200004d5053"}
BOARD1_MAC = "0200004d5053"          # every image's MAC: board 1 runs it, so does board 2 today
BOARD2_MAC = "0200000002fe"
FIELDS = ("label", "hostname", "ip", "mac")
#: The refusals, byte for byte (identity_linux.c / coordinator.c at 18622e5).
LOCKED_ERR = "identity locked: board claimed (use ssh)"
NO_PERSIST_ERR = "identity: no persistent /persist (use the card)"
NOT_SUPPORTED_ERR = "identity not supported"

_LABEL_RE = re.compile(r"[A-Z0-9-]{1,19}")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")


def _hostname_of(label: str) -> str:
    """The resolver: the label lower-cased when that is a host name, else ``mps3``."""
    low = label.lower()
    return low if check("hostname", low)[0] is None else "mps3"


def check(field: str, value: str) -> tuple[str | None, str | None]:
    """identity_core.c's rules (pyverify ``identity_check`` at 18622e5): ``(why, None)`` or
    ``(None, canonical)``: a bare quad takes /24; the MAC is 12 lowercase hex digits."""
    if field == "label":
        if not value:
            return "empty", None
        if len(value) > 19:
            return "longer than 19 characters", None
        return (None, value) if _LABEL_RE.fullmatch(value) else ("not [A-Z0-9-]", None)
    if field == "hostname":
        if not value:
            return "empty", None
        if len(value) > 63:
            return "longer than 63 characters", None
        ok = all(_HOST_LABEL_RE.fullmatch(p) for p in value.split("."))
        return (None, value) if ok else ("not an RFC 1123 host name", None)
    if field == "ip":
        m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:/(\d{1,2}))?", value or "")
        if not m:
            return ("empty" if not value else "not a dotted quad"), None
        o = [int(x) for x in m.groups()[:4]]
        pfx = int(m.group(5)) if m.group(5) is not None else 24
        if any(x > 255 for x in o):
            return "not a dotted quad", None
        if not 8 <= pfx <= 30:
            return "prefix not in 8..30", None
        if o[0] in (0, 127) or o[0] >= 224:
            return "not a usable host address", None
        ip = (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]
        host = 0xFFFFFFFF >> pfx
        if ip & host == 0:
            return "the network address of its prefix", None
        if ip & host == host:
            return "the broadcast address of its prefix", None
        return None, "{}.{}.{}.{}/{}".format(*o, pfx)
    if field == "mac":
        v = value or ""
        if re.fullmatch(r"[0-9A-Fa-f]{12}", v):
            h = v
        elif re.fullmatch(r"[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(\1[0-9A-Fa-f]{2}){4}", v):
            h = re.sub(r"[:-]", "", v)
        else:
            return "not 12 hex digits", None
        b = bytes.fromhex(h)
        if b[0] & 1:
            return "multicast, not unicast", None
        if not any(b):
            return "all zero", None
        return None, h.lower()
    return "unknown key", None


class IdentityBoard(SlotBoard):
    def __init__(self, *args: Any, running: dict[str, Any] | None = None,
                 stage0: dict[str, Any] | None = None, persist: bool = True,
                 has_identity: bool = True, decline: str = "unknown op",
                 reply_op: bool = True, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.has_identity = has_identity
        self.decline = decline
        self.reply_op = reply_op
        if has_identity:
            if "identity" not in self.features:
                self.features = (*self.features, "identity")
        else:
            # the vendored linux profile (pyverify from platform 3f7cea2) is a v0.16 image, with
            # `identity` and `locate`: an image without the verbs (rc2_v6) has neither
            self.features = tuple(f for f in self.features if f not in ("identity", "locate"))
            self.identity = None                   # nor identify's `label`
        self.persist = persist
        self.stage0 = dict(stage0) if stage0 else None
        self.override: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None
        self.identity_sets: list[tuple[str | None, dict[str, Any]]] = []
        self.identity_reads = 0
        self.running = self._resolve()
        if running:
            self.running.update(running)
            self.running.setdefault("source", {})
        self._sync_identify()

    # -- the model --------------------------------------------------------------------------

    def _resolve(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        override = self.override if override is None else override
        out: dict[str, Any] = {"source": {}}
        for f in ("label", "ip", "mac"):
            if override and override.get(f):
                out[f], out["source"][f] = override[f], "override"
            elif self.stage0 and self.stage0.get(f):
                out[f], out["source"][f] = self.stage0[f], "stage0"
            else:
                out[f], out["source"][f] = DEFAULT[f], "default"
        if override and override.get("hostname"):
            out["hostname"], out["source"]["hostname"] = override["hostname"], "override"
        else:
            host = _hostname_of(out["label"])
            out["hostname"] = host
            out["source"]["hostname"] = "label" if host == out["label"].lower() else "default"
        return out

    def _sync_identify(self) -> None:
        self.mac = self.running["mac"]
        self.board_ip = str(ipaddress.IPv4Interface(self.running["ip"]).ip)

    def identity_reply(self) -> dict[str, Any]:
        r = self.running
        return {"ok": True, "op": "identity", "label": r["label"], "hostname": r["hostname"],
                "ip": r["ip"], "mac": r["mac"], "source": dict(r["source"]),
                "stage0": dict(self.stage0) if self.stage0 else None,
                "override": dict(self.override) if self.override else None,
                "pending": dict(self.pending) if self.pending else None,
                "persist": self.persist}

    def _op_identity_set(self, req: dict[str, Any], peer: str | None) -> dict[str, Any]:
        """The board's order: the claim lock FIRST (whatever the request holds), then no card,
        then the request's shape, then every value; nothing is written if one is refused."""
        def refuse(err: str, code: str) -> dict[str, Any]:
            return {"ok": False, "err": err, "code": code}

        if self.ssh_claimed and not self._store_local(peer):
            return refuse(LOCKED_ERR, "locked")
        if not self.persist:
            return refuse(NO_PERSIST_ERR, "no_persist")
        clear = req.get("clear", False)
        if not isinstance(clear, bool):
            return refuse("invalid clear: not a bool", "invalid")
        edits: dict[str, str] = {}
        for f in FIELDS:
            if f in req:
                v = req[f]
                if not isinstance(v, str):
                    return refuse(f"invalid {f}: not a string", "invalid")
                if len(v) > 79:
                    return refuse(f"invalid {f}: too long", "invalid")
                edits[f] = v
        if clear and edits:
            return refuse("invalid request: clear takes no other field", "invalid")
        if not clear and not edits:
            return refuse("invalid request: nothing to set (label/hostname/ip/mac)", "invalid")
        new = dict(self.override or {})
        for f, v in edits.items():
            if v == "":
                new.pop(f, None)              # "" drops the key from the override
                continue
            why, canon = check(f, v)
            if why:
                return refuse(f"invalid {f}: {why}", "invalid")
            new[f] = canon
        with self._lock:
            self.identity_sets.append((peer, {**({"clear": True} if clear else {}), **edits}))
            self.override = None if (clear or not new) else new
            nxt = self._resolve(self.override or {})
            self._next = nxt
            self.pending = {f: nxt[f] for f in FIELDS if nxt[f] != self.running.get(f)} or None
        reply: dict[str, Any] = {"ok": True, "op": "identity_set", "persisted": True,
                                 "pending": dict(self.pending) if self.pending else None,
                                 "applies": "reboot"}
        if not self.reply_op:
            reply.pop("op")
        return reply

    def identify_reply(self, nonce: str) -> dict[str, Any]:
        """v0.16: identify carries the resolved ``label``, after ``ssh`` and before ``ports``."""
        r = super().identify_reply(nonce)
        if self.has_identity and r.get("mode") != "rescue" and "ports" in r:
            ports = r.pop("ports")
            r["label"] = self.running["label"]
            r["ports"] = ports
        return r

    def _identify_serve(self) -> None:
        """The vendored FakeShell's UDP 6899 loop (one datagram in, one out; malformed =
        silent; rate-limited; silent while hung), answering with ``identify_reply`` above: the
        vendored loop calls its module's reply builder, whose ``label`` (from platform 3f7cea2)
        is the vendored model's, not this board's."""
        sock = self._identify_sock
        tokens = float(self.identify_rate_per_s)
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                break
            req: dict[str, Any] | None = None
            try:
                obj = json.loads(data.decode("utf-8"))
                nonce = obj.get("nonce") if isinstance(obj, dict) else None
                if (isinstance(obj, dict) and obj.get("op") == "identify" and obj.get("v") == 1
                        and isinstance(nonce, str) and 8 <= len(nonce) <= 32
                        and set(nonce) <= set("0123456789abcdef")):
                    req = obj
            except (ValueError, UnicodeDecodeError):
                req = None
            now = time.monotonic()
            tokens = min(self.identify_rate_per_s, tokens + (now - last) * self.identify_rate_per_s)
            last = now
            replied = False
            if req is not None and tokens >= 1.0 and not self.hung:
                tokens -= 1.0
                payload = json.dumps(self.identify_reply(req["nonce"]),
                                     separators=(",", ":")).encode("ascii")
                try:
                    sock.sendto(payload[:1200], addr)
                    replied = True
                except OSError:
                    pass
            with self._lock:
                self.identify_requests.append((addr, req, replied))

    def handle_control(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        op = request.get("op")
        if op in ("identity", "identity_set") and not self.has_identity:
            if self.decline == "not_supported":
                return {"ok": False, "err": NOT_SUPPORTED_ERR, "code": "not_supported"}
            return {"ok": False, "err": f"unknown op {op!r}"}
        if op == "identity":
            self.identity_reads += 1
            with self._lock:
                return self.identity_reply()
        if op == "identity_set":
            return self._op_identity_set(request, peer)
        return super().handle_control(request, peer=peer)

    def _simulate_restart(self) -> None:
        super()._simulate_restart()
        with self._lock:
            nxt = getattr(self, "_next", None)
            if nxt is not None:
                self.running = nxt
                self._next = None
            self.pending = None
            self._sync_identify()


def identity_board(**kw: Any) -> IdentityBoard:
    """A started IdentityBoard on ephemeral ports (``lxslots_board.slot_board``'s defaults)."""
    kw.setdefault("profile", "linux")
    kw.setdefault("reboot_in_ms", 150)
    kw.setdefault("slots", {})
    kw.setdefault("static_id", LINUX_SID)
    kw.setdefault("harness_version", "1.0.0")
    fake = IdentityBoard.ephemeral(**kw)
    fake.start()
    return fake


# --- the hub, the lease, the MCC -----------------------------------------------------------------


def target_record(name: str, board_ip: str, board_mac: str, hostname: str, *,
                  host_ip: str = "", discovered_mac: str | None = None) -> dict[str, Any]:
    """fpgahub's ``BoardResponse`` (``target show`` / ``GET /targets/{t}``), trimmed."""
    net = {"host_ip": host_ip or board_ip.rsplit(".", 1)[0] + ".1/24", "board_ip": board_ip,
           "board_mac": board_mac, "pl_mac": None, "hostname": hostname, "dns_search": "fpga"}
    return {"name": name, "server": "mapstone-dev", "hub_path": "1-1", "description": "",
            "effective_hub_path": None, "discovered_mac": discovered_mac,
            "naming": {"net_name": f"{name}_eth", "tty_symlink_dir": name}, "network": net}


#: The lab on 2026-09-28: board 1's board_mac is the hub adapter's (Realtek), board 2's is
#: the MAC it should have.
LAB_TARGETS = {
    "mps3_01_pl": target_record("mps3_01_pl", "192.168.10.101", "00:e0:4c:46:dc:f8",
                                "mps3-01-pl", discovered_mac="00:e0:4c:46:dc:f8"),
    "mps3_02_pl": target_record("mps3_02_pl", "192.168.11.101", "02:00:00:00:02:fe",
                                "mps3-02-pl"),
}
LAB_GROUPS = [{"board": "mps3_01", "members": [{"name": "mps3_01_pl"}, {"name": "mps3_01_mcc"}]},
              {"board": "mps3_02", "members": [{"name": "mps3_02_pl"}, {"name": "mps3_02_mcc"}]}]


class FakeHubClient:
    transport = "ssh"

    def __init__(self, target: str, targets: dict[str, Any] | None = None,
                 groups: list[dict[str, Any]] | None = None) -> None:
        self.target = target
        self.targets = dict(LAB_TARGETS if targets is None else targets)
        self._groups = list(LAB_GROUPS if groups is None else groups)
        self.calls: list[tuple[str, str]] = []

    def target_info(self, name: str = "") -> dict[str, Any]:
        name = name or self.target
        self.calls.append(("target show", name))
        return dict(self.targets[name])

    def groups(self) -> list[dict[str, Any]]:
        self.calls.append(("board list", ""))
        return list(self._groups)

    def board_id(self) -> str:
        for g in self._groups:
            if any(m["name"] == self.target for m in g["members"]):
                return g["board"]
        return ""


class FakeHubConfig:
    board = ""


class FakeHub:
    """``session.hub`` (``Mps3Hub``'s shape: host, target, config, client)."""

    def __init__(self, target: str = "mps3_02_pl", **kw: Any) -> None:
        self.host = "hub.board-id.test"
        self.target = target
        self.config = FakeHubConfig()
        self.client = FakeHubClient(target, **kw)

    def close(self) -> None:
        pass


class FakeLeases:
    """The lease service's ``view(hub)``: ``mine`` True/False, or None for nobody."""

    def __init__(self, mine: bool | None = True, holder: str = "you@test") -> None:
        self.mine = mine
        self.holder = holder
        self.asked = 0

    def forget(self, hub: Any) -> None:
        pass

    def view(self, hub: Any) -> dict[str, Any]:
        self.asked += 1
        if self.mine is None:
            return {"lease": None}
        return {"lease": {"mine": self.mine, "holder": self.holder if self.mine else "someone@else"}}


class SpyController:
    """``session.controller``: an MCC that records every call. The fix must never touch it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def reboot(self, *a: Any, **kw: Any) -> Any:
        self.calls.append("reboot")
        raise AssertionError("an MCC REBOOT was asked for: the identity fix must use the "
                             "harness's reboot verb")

    def command(self, line: str) -> str:
        self.calls.append(f"command {line}")
        raise AssertionError(f"an MCC command was sent: {line}")
