"""The SSH claim routes (docs/API.md "SSH claim of a Linux harness", ``claim_api.py``) in the
T14 mock daemon (lane LINUX-CLAIM).

The mock's boards are ``DemoEngine`` bare-metal boards, so none has a claim until a test
says so: ``ClaimSim.linux(bid, state="unclaimed")`` makes the board a Linux harness whose
``info`` carries ``claim``. ``POST /claim`` is a 202 job, refused without ``confirm`` (409
REFUSED) and on a board with no claim (422), exactly as the real routes are.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import Body, FastAPI

from harness_manager.core.errors import AlreadyError, RefusedError, UnavailableError

API = "/api/v1"
HOST_KEY = "SHA256:mockmockmockmockmockmockmockmockmockmockmoc"
KEY_FP = "SHA256:yourkeyyourkeyyourkeyyourkeyyourkeyyourkey"


class ClaimSim:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self.claims: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []

    def linux(self, bid: str, state: str = "unclaimed") -> None:
        with self._mu:
            self.claims[bid] = self._status(state)

    @staticmethod
    def _status(state: str, action: str = "") -> dict[str, Any]:
        claimed = None
        if state == "mine":
            claimed = {"by": "you@mock", "key_fp": KEY_FP, "at": "2026-09-25T12:00:00Z",
                       "mine": True}
        elif state == "other":
            claimed = {"by": "another key", "key_fp": None, "at": None, "mine": False}
        pinned = HOST_KEY if state == "mine" else None
        out = {"state": state, "claimed": claimed,
               "host_key": {"reported": HOST_KEY, "pinned": pinned,
                            "match": True if pinned else None, "seen_before": None},
               "route": "lan", "user": "root", "source": "identify (mock)",
               "checked_at": "2026-09-25T12:00:00Z", "live": True, "notes": []}
        if action:
            out["action"] = action
        return out

    def get(self, bid: str) -> dict[str, Any] | None:
        with self._mu:
            return self.claims.get(bid)


def register(app: FastAPI, state: Any, ok: Any, accepted: Any) -> ClaimSim:
    sim = ClaimSim()

    @app.get(f"{API}/boards/{{bid:path}}/claim")
    def claim_status(bid: str, refresh: str | None = None) -> dict[str, Any]:
        state.session(bid)
        return ok(board_id=bid, claim=sim.get(bid))

    @app.post(f"{API}/boards/{{bid:path}}/claim", status_code=202)
    def claim(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> Any:  # noqa: B008
        state.session(bid)
        b = body or {}
        sim.posts.append(dict(b))
        if b.get("confirm") is not True:
            raise RefusedError("a claim needs a confirmation", hint='send {"confirm": true}')
        cur = sim.get(bid)
        if cur is None:
            raise UnavailableError("ssh_claim", "no SSH to claim (bare metal)")

        def run(progress: Any) -> dict[str, Any]:
            if cur["state"] != "unclaimed" and not b.get("adopt"):
                raise AlreadyError(f"{bid} is already claimed")
            new = sim._status("mine", "adopted" if b.get("adopt") else "claimed")
            with sim._mu:
                sim.claims[bid] = {k: v for k, v in new.items() if k != "action"}
            return {"board_id": bid, "claim": new}

        return accepted(state.jobs.start(bid, "claim", run))

    @app.post(f"{API}/boards/{{bid:path}}/repin", status_code=202)
    def repin(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> Any:  # noqa: B008
        state.session(bid)
        b = body or {}
        sim.posts.append({"repin": dict(b)})
        if b.get("confirm") is not True or not b.get("fingerprint"):
            raise RefusedError("a re-pin needs a confirmation and the exact fingerprint")
        cur = sim.get(bid)
        if cur is None:
            raise UnavailableError("ssh_claim", "no SSH to claim (bare metal)")

        def run(progress: Any) -> dict[str, Any]:
            new = sim._status("mine", "repinned")
            new["host_key"] = {"reported": b["fingerprint"], "pinned": b["fingerprint"],
                               "match": True, "seen_before": None}
            with sim._mu:
                sim.claims[bid] = {k: v for k, v in new.items() if k != "action"}
            return {"board_id": bid, "claim": new}

        return accepted(state.jobs.start(bid, "repin", run))

    @app.get(f"{API}/boards/{{bid:path}}/ssh")
    def ssh_argv(bid: str, command: str | None = None) -> dict[str, Any]:
        state.session(bid)
        if sim.get(bid) is None:
            raise UnavailableError("ssh_claim", "no SSH to claim (bare metal)")
        return ok(board_id=bid, argv=["ssh", "-l", "root", "192.168.10.101"]
                  + ([command] if command else []))

    return sim
