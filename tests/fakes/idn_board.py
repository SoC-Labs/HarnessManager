"""The Linux harness's identity verbs (net-protocol v0.16), for the BOARD-ID tests.

HM's OWN model of the Linux lead's contract of 2026-09-28 (pyverify's FakeShell gets it from
their lane; until then this is the double):

- ``identity`` (a read, any peer): ``{label, hostname, ip "a.b.c.d/n", mac (12 hex), source,
  stage0, override, pending, persist}``. Each field resolves override > stage0 > the image
  default (label ``MPS3``, 192.168.10.101/24, 02:00:00:4d:50:53, source ``default``); the
  hostname follows the label (source ``label``) unless set;
- ``identity_set`` (CLAIM-LOCKED for any peer but the board itself, like ``slot``/``usd``):
  any of ``{label, hostname, ip, mac}`` or ``{"clear": true}``; ``no_persist`` on a board
  with no card (a netboot), ``invalid`` naming the field; the values go PENDING and apply at
  the next ``reboot`` (the WARM verb: ``_simulate_restart``);
- ``version.features`` gains ``identity`` (``has_identity=False``: an older image, which
  answers ``unknown op``); identify's ``mac``/``ip`` follow the running identity.

``IdentityBoard`` is ``lxslots_board.SlotBoard`` (the slot verbs, the claim lock, the reboot
that restarts the harness) plus the above. ``FakeHub``/``FakeHubClient`` stand in for the
session's hub adapter (``fpgahub target show`` / ``board list``); ``FakeLeases`` for the lease
service; ``SpyController`` for the MCC, to prove it is never asked to reboot.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from tests.fakes.lxslots_board import LINUX_SID, SlotBoard

DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": "0200004d5053"}
BOARD1_MAC = "0200004d5053"          # every image's MAC: board 1 runs it, so does board 2 today
BOARD2_MAC = "0200000002fe"


def _hostname_of(label: str) -> str:
    return label.lower()


class IdentityBoard(SlotBoard):
    def __init__(self, *args: Any, running: dict[str, Any] | None = None,
                 stage0: dict[str, Any] | None = None, persist: bool = True,
                 has_identity: bool = True, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.has_identity = has_identity
        if has_identity:
            self.features = (*self.features, "identity")
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
            if override and f in override:
                out[f], out["source"][f] = override[f], "override"
            elif self.stage0 and self.stage0.get(f):
                out[f], out["source"][f] = self.stage0[f], "stage0"
            else:
                out[f], out["source"][f] = DEFAULT[f], "default"
        if override and "hostname" in override:
            out["hostname"], out["source"]["hostname"] = override["hostname"], "override"
        else:
            out["hostname"], out["source"]["hostname"] = _hostname_of(out["label"]), "label"
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

    @staticmethod
    def _invalid(req: dict[str, Any]) -> str:
        if "label" in req and not re.fullmatch(r"[\x20-\x7e]{1,23}", str(req["label"])):
            return "label"
        if "mac" in req:
            m = str(req["mac"])
            if not re.fullmatch(r"[0-9a-f]{12}", m) or int(m[:2], 16) & 1 or int(m, 16) == 0:
                return "mac"
        if "ip" in req:
            try:
                ipaddress.IPv4Interface(str(req["ip"]))
            except ValueError:
                return "ip"
        return ""

    def _op_identity_set(self, req: dict[str, Any], peer: str | None) -> dict[str, Any]:
        if self.ssh_claimed and not self._store_local(peer):
            return {"ok": False, "err": "identity locked: board claimed (use ssh)",
                    "code": "locked"}
        if not self.persist:
            return {"ok": False, "err": "no persist: the stage0 bake is the identity",
                    "code": "no_persist"}
        body = {k: v for k, v in req.items() if k in ("label", "hostname", "ip", "mac", "clear")}
        bad = self._invalid(body)
        if bad:
            return {"ok": False, "err": f"invalid {bad}", "code": "invalid"}
        with self._lock:
            self.identity_sets.append((peer, dict(body)))
            if body.get("clear"):
                self.override = None
                nxt = self._resolve({})
            else:
                self.override = {**(self.override or {}),
                                 **{k: v for k, v in body.items() if k != "clear"}}
                nxt = self._resolve()
            self._next = nxt
            self.pending = {f: nxt[f] for f in ("label", "hostname", "ip", "mac")
                            if nxt[f] != self.running.get(f)} or None
        return {"ok": True, "persisted": True, "pending": dict(self.pending or {}),
                "applies": "reboot"}

    def handle_control(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        op = request.get("op")
        if op in ("identity", "identity_set") and not self.has_identity:
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
