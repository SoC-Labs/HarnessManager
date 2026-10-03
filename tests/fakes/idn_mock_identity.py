"""The board identity routes (docs/API.md "Board identity", ``identity_api.py``) in the T14 mock
daemon (lane BOARD-ID).

The mock's boards are ``DemoEngine`` boards, so none has an identity until a test says so:
``IdentitySim.board2_as_board1(bid)`` makes the board the lab's board 2 reporting board 1's
label, IP and MAC (a clash, with the fix from the hub entry); ``IdentitySim.matching(bid)`` the
twin that matches. ``POST /identity`` is a 202 job, refused without the typed phrase (409
REFUSED) exactly as the real route is; the job applies the plan and records the post.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from fastapi import Body, FastAPI

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.services import board_identity as BI

API = "/api/v1"

HUB2 = {"target": "mps3_02_pl", "board": "mps3_02", "label": "MPS3-02",
        "board_ip": "192.168.11.101", "prefix": 24, "board_mac": "02:00:00:00:02:fe",
        "hostname": "mps3-02-pl", "discovered_mac": "", "mac_suspect": ""}
#: V7-ALIGN: a "default" label is always MPS3 on the shipped image, so a board with board 1's
#: label has it from a bake (board 1's stage0 bake here); the MAC is the image's.
AS_BOARD1 = {"label": "MPS3-01", "hostname": "mps3-01", "ip": "192.168.10.101/24",
             "mac": "02:00:00:4d:50:53",
             "source": {"label": "stage0", "ip": "stage0", "mac": "default",
                        "hostname": "label"},
             "stage0": None, "override": None, "pending": None, "persist": True,
             "via": "identity", "feature": True, "impl": "linux", "at": "2026-09-28T13:30:00Z"}
AS_BOARD2 = {**AS_BOARD1, "label": "MPS3-02", "hostname": "mps3-02", "ip": "192.168.11.101/24",
             "mac": "02:00:00:00:02:fe",
             "source": {"label": "override", "ip": "override", "mac": "override",
                        "hostname": "label"}}
BOARD1_SEEN = {"who": "mps3-01", "kind": "board", "label": "MPS3-01", "ip": "192.168.10.101/24",
               "mac": "02:00:00:4d:50:53", "hostname": "mps3-01"}
#: V7-ALIGN: board 2 on rc2_v7 before its identity bake is fielded: the generic label and the
#: old MAC, its IP already its own (stage0).
BOARD2_TONIGHT = {**AS_BOARD1, "label": "MPS3", "hostname": "mps3", "ip": "192.168.11.101/24",
                  "source": {"label": "default", "ip": "stage0", "mac": "default",
                             "hostname": "label"}}


def compose(bid: str, reported: dict[str, Any], hub: dict[str, Any] | None,
            others: list[dict[str, Any]], refusal: dict[str, Any] | None = None) -> dict[str, Any]:
    """The service's own ``status`` shape, from the service's own pure functions."""
    findings = BI.compare(reported, hub, others)
    status = BI.summarise(findings, reported)
    plan = BI.plan_fix(bid, reported, hub, from_hub=True)
    return {"status": status, "level": BI.level_of(status), "reported": reported, "hub": hub,
            "findings": findings,
            "fix": {**plan, "ready": refusal is None and bool(plan["changes"]),
                    "refusal": refusal},
            "notes": [f["text"] for f in findings], "checked_at": reported.get("at"),
            "live": True}


class IdentitySim:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self.boards: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []

    def set(self, bid: str, reported: dict[str, Any], *, hub: dict[str, Any] | None = HUB2,
            others: list[dict[str, Any]] | None = None,
            refusal: dict[str, Any] | None = None) -> None:
        with self._mu:
            self.boards[bid] = {"reported": copy.deepcopy(reported), "hub": hub,
                                "others": list(others if others is not None else [BOARD1_SEEN]),
                                "refusal": refusal}

    def board2_as_board1(self, bid: str, **kw: Any) -> None:
        self.set(bid, AS_BOARD1, **kw)

    def matching(self, bid: str) -> None:
        self.set(bid, AS_BOARD2)

    def board2_tonight(self, bid: str, *, board1_mac: str = "02:00:00:4d:50:53") -> None:
        """Board 2 with the generic label and the old MAC; board 1 seen with ``board1_mac``
        (its own after its bake, or still the old one: a real MAC clash)."""
        self.set(bid, BOARD2_TONIGHT, others=[{**BOARD1_SEEN, "mac": board1_mac,
                                               "label_source": "stage0"}])

    def get(self, bid: str) -> dict[str, Any] | None:
        with self._mu:
            b = self.boards.get(bid)
            if b is None:
                return None
            return compose(bid, b["reported"], b["hub"], b["others"], b["refusal"])

    def apply(self, bid: str, want: dict[str, Any]) -> None:
        with self._mu:
            rep = self.boards[bid]["reported"]
            for k, v in want.items():
                rep[k] = v
                rep["source"][k] = "override"


def register(app: FastAPI, state: Any, ok: Any, accepted: Any) -> IdentitySim:
    sim = IdentitySim()

    @app.get(f"{API}/boards/{{bid:path}}/identity")
    def identity_status(bid: str, refresh: str | None = None) -> dict[str, Any]:
        state.session(bid)
        return ok(board_id=bid, identity=sim.get(bid))

    @app.post(f"{API}/boards/{{bid:path}}/identity", status_code=202)
    def identity_fix(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> Any:  # noqa: B008
        state.session(bid)
        b = body or {}
        sim.posts.append(dict(b))
        confirm = b.get("confirm")
        if not isinstance(confirm, str) or not confirm.strip():
            raise RefusedError("changing a board's identity needs the typed phrase: nothing was "
                               "changed", hint='send {"confirm": "<identity.fix.phrase>"}')
        st = sim.get(bid)
        if st is None:
            raise UnavailableError(BI.CAPABILITY, BI.NO_ADAPTER)
        ref = st["fix"]["refusal"]
        if ref:
            raise BI.refusal_error(ref["name"], ref["message"], ref.get("hint") or "")
        want = {k: b[k] for k in BI.FIELDS if b.get(k)}
        if not (want or b.get("from_hub") or b.get("clear")):
            raise UsageError("nothing to change")

        def run(progress: Any) -> dict[str, Any]:
            cur = sim.get(bid)
            plan = BI.plan_fix(bid, cur["reported"], cur["hub"], want=want or None,
                               from_hub=bool(b.get("from_hub")))
            if confirm.strip() != plan["phrase"]:
                raise RefusedError("changing a board's identity needs the typed phrase: nothing "
                                   "was changed", hint=f"type exactly: {plan['phrase']}")
            sim.apply(bid, plan["want"])
            return {"board_id": bid, "action": "set", "changes": plan["changes"],
                    "set": {"persisted": True, "route": "board-ssh", "setter": "harnessd"},
                    "reboot": {"summary": "reboot witnessed: up again after 41.0 s"},
                    "verified": True, "identity": sim.get(bid), "notes": []}

        return accepted(state.jobs.start(bid, "identity", run))

    # --- lane IDENTITY: "Name this board" (docs/API.md "Board identity"), from the service's
    # own propose() over the sim's board (no network: nothing in the pool answers) ---
    @app.get(f"{API}/boards/{{bid:path}}/identity/proposal")
    def identity_proposal(bid: str, label: str | None = None, mac: str | None = None,
                          ip: str | None = None) -> dict[str, Any]:
        state.session(bid)
        if sim.get(bid) is None:
            raise UnavailableError(BI.CAPABILITY, BI.NO_ADAPTER)
        return ok(board_id=bid, proposal=sim.propose(bid, label=label, mac=mac, ip=ip))

    return sim


class _SimAdapter:
    """The sim's board as a ``session.net_identity`` adapter, for the service's propose()."""

    def __init__(self, sim: IdentitySim, bid: str) -> None:
        self._sim, self._bid = sim, bid

    def read(self, *, refresh: bool = False, cheap: bool = False) -> dict[str, Any]:
        return copy.deepcopy(self._sim.boards[self._bid]["reported"])

    def hub_record(self, *, refresh: bool = False, cheap: bool = False) -> Any:
        return self._sim.boards[self._bid]["hub"]

    def hub_others(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return []

    def fix_reason(self, reported: Any) -> tuple[str, str, str]:
        ref = self._sim.boards[self._bid]["refusal"]
        return (ref["name"], ref["message"], ref.get("hint") or "") if ref else ("", "", "")

    def address(self) -> str:
        return "mock:6900"

    @staticmethod
    def policy() -> Any:
        from harness_manager.services.identity_assign import IdentityPolicy

        return IdentityPolicy(pack="mps3", reserved_mac_prefixes=("02:00:00",),
                              ip_pool="192.168.10.110-199", ip_pool_setting="mps3.identity.ip_pool",
                              reserved_ips=("192.168.10.101",),
                              rescue_note="in recovery mode the board still answers on "
                                          "192.168.10.101 with its default MAC",
                              known_bad_hub_records={"mps3_01_pl": "its board_mac is the hub's "
                                                                   "own USB adapter"})


def _propose(self: IdentitySim, bid: str, **kw: Any) -> dict[str, Any]:
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    hub = self.boards[bid]["hub"]
    session = SimpleNamespace(candidate=SimpleNamespace(board_id=bid, name="", pack="mps3"),
                              net_identity=_SimAdapter(self, bid),
                              hub=SimpleNamespace(host="hub.mock", target=hub["target"])
                              if hub else None)
    if not hasattr(self, "_seen_dir"):
        self._seen_dir = tempfile.mkdtemp(prefix="idn-mock-seen-")
    svc = BI.IdentityService(None, seen=BI.SeenIdentities(Path(self._seen_dir)))
    return svc.propose(session, **kw)


IdentitySim.propose = _propose          # type: ignore[attr-defined]
# --- end lane IDENTITY ---
