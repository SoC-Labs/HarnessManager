"""HM-INT: the board lease end to end, on the real daemon (``create_app``).

A real Mps3Pack session over the claimed-lock lab (the claim forward through the fake SSH, the
board enforcing the whole v2.2 claim lock) with HM-A's in-memory board lease server plugged
into the board. Everything HM-A..HM-D built is on the path: BoardLeaseClient, LeaseService
(store, heartbeat, view, events), the lease_mode job, the hub_api routes, the shell token
preamble. Loopback only: no board, hub or real ssh. The CLI half is at the end.
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import HeldError
from harness_manager.core.events import Event
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3 import shell as SH
from tests.fakes.bl_board_lease import BoardLeaseServer, attach
from tests.fakes.claimed_lock import (
    BOARD_IP,
    HUB,
    board_key_fp,
    observed_claimed,
    pin_claim,
)
from tests.fakes.t13_daemon import TOKEN, bid_path, headers
from tests.integration.test_claimed_lock import lab  # noqa: F401  (the fixture)

H = headers()
HUB_SIDE = "127.0.0.1"
TARGET = f"{BOARD_IP}:6900"
BOARD_NAME = "mps3-01"


@pytest.fixture(autouse=True)
def _no_stale_tokens():
    SH._lease_tokens.clear()              # process-wide, keyed by the lab's one board address
    yield
    SH._lease_tokens.clear()


@dataclass
class Daemon:
    """One Harness Manager: its state dir, engine and app. ``restart()`` is a new process:
    nothing in memory survives but what is in the state dir."""

    state: Path
    rig: Any
    events: list[Event] = field(default_factory=list)
    eng: Any = None
    client: Any = None
    bid: str = ""

    def start(self) -> Daemon:
        with self.env():
            self.eng = Engine(EngineConfig(state_dir=self.state))
            self.client = TestClient(create_app(self.eng, token=TOKEN, static_dir=None))
            self.client.__enter__()
            d = self.client.app.state.daemon
            for topic in ("lease.taken", "lease.state", "lease.mode"):
                d.bus.subscribe(topic, self.events.append)
            r = self.client.post("/api/v1/boards", json={"target": TARGET, "via": f"ssh:{HUB}"},
                                 headers=H)
            assert r.status_code == 200, r.text
            self.bid = r.json()["board_id"]
        return self

    @contextmanager
    def env(self) -> Iterator[None]:
        old = os.environ.get("HARNESS_MANAGER_STATE_DIR")
        os.environ["HARNESS_MANAGER_STATE_DIR"] = str(self.state)
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("HARNESS_MANAGER_STATE_DIR", None)
            else:
                os.environ["HARNESS_MANAGER_STATE_DIR"] = old

    def stop(self) -> None:
        with self.env():
            self.client.__exit__(None, None, None)
            self.eng.close_all()
        SH._lease_tokens.clear()

    def restart(self) -> Daemon:
        self.stop()
        self.events.clear()
        return self.start()

    @property
    def d(self) -> Any:
        return self.client.app.state.daemon

    @property
    def leases(self) -> Any:
        return self.d.leases

    def url(self, tail: str = "") -> str:
        return f"{bid_path(self.bid)}{tail}"

    def get(self, tail: str) -> dict[str, Any]:
        with self.env():
            r = self.client.get(self.url(tail), headers=H)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, tail: str, body: dict[str, Any] | None = None, *, ok: int = 200) -> Any:
        with self.env():
            r = self.client.post(self.url(tail), json=body or {}, headers=H)
        assert r.status_code == ok, r.text
        return r.json()

    def job(self, tail: str, body: dict[str, Any]) -> dict[str, Any]:
        out = self.post(tail, body, ok=202)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            j = self.client.get(f"/api/v1/jobs/{out['job']}", headers=H).json()
            if j["state"] in ("done", "failed"):
                return j
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    def mode_on(self) -> dict[str, Any]:
        return self.job("/lease/mode", {"single_user": True, "confirm": True})

    def beat(self) -> None:
        with self.env():
            self.leases.beat_due(force=True)


@dataclass
class World:
    rig: Any
    srv: BoardLeaseServer
    tmp: Path
    daemons: list[Daemon] = field(default_factory=list)

    def hm(self, name: str = "hm1", *, pin: bool = True) -> Daemon:
        state = self.tmp / name
        state.mkdir(parents=True, exist_ok=True)
        dm = Daemon(state, self.rig)
        if pin:                           # this HM holds a key the board trusts
            with dm.env():
                pin_claim(self.rig.session)
                observed_claimed(self.rig.board_id, board_key_fp(), claimed=True)
        dm.start()
        self.daemons.append(dm)
        return dm

    def carried(self, op: str) -> list[Any]:
        """The ``lease`` field of each ``op`` request the board saw (None: no token)."""
        return [r.get("lease") for r in self.srv.seen if r.get("op") == op]


@pytest.fixture
def world(lab, tmp_path) -> Iterator[World]:  # noqa: F811
    rig = lab(pinned=True, observed=True)
    rig.fake.claim_gate_all = True
    srv = attach(rig.fake, name=BOARD_NAME)
    w = World(rig, srv, tmp_path)
    yield w
    for dm in reversed(w.daemons):
        dm.stop()


def stored_token(hm: Daemon) -> str:
    """The board lease token in this HM's LeaseStore ("" when none)."""
    adapter = hm.eng.session(hm.bid).lease
    stored = hm.leases.store.get(adapter.host, adapter.target)
    return stored.token if stored else ""


def test_a_shared_board_then_single_user_mode_on_takes_the_lease(world):
    hm = world.hm()
    mode = hm.get("/lease/mode")
    assert mode["available"] is True and mode["single_user"] is False and mode["can_change"]
    shown = hm.get("/lease")
    assert shown["server"]["kind"] == "board" and shown["mode"]["single_user"] is False
    assert shown["lease"] is None

    job = hm.mode_on()
    assert job["state"] == "done", job
    assert job["result"]["mode"]["single_user"] is True
    assert job["result"]["lease"]["mine"] is True and job["result"]["lease"]["here"] is True
    token = world.srv.holder["token"]

    shown = hm.get("/lease")
    assert shown["server"]["kind"] == "board" and shown["server"]["name"]
    assert shown["server"]["host"] and shown["mode"]["single_user"] is True
    assert shown["lease"]["mine"] is True and shown["lease"]["here"] is True
    assert hm.get("/lease/mode")["single_user"] is True
    assert stored_token(hm) == token                      # kept as acquire would keep it
    assert hm.bid in hm.leases.tracked()                  # ... and heartbeated
    assert any(e.topic == "lease.mode" and e.data["single_user"] for e in hm.events)


def test_program_and_reset_from_this_hm_carry_the_token(world):
    hm = world.hm()
    hm.mode_on()
    token = world.srv.holder["token"]
    hm.post("/reset", {"target": "dut"})
    sent = world.carried("reset")
    assert sent and set(sent) == {token}
    assert world.rig.fake.gated == []                     # nothing was refused on the way
    # deploy rides the same preamble (a swap is a gated verb)
    assert SH.lease_token_for(world.rig.session.shell.gate_key) == token


def test_twin_the_gated_verb_without_a_token_is_the_boards_refusal(world):
    hm = world.hm()
    hm.mode_on()
    SH._lease_tokens.clear()                              # as if the preamble had no token
    hm.d.leases.store.drop(*_server_key(hm))
    with pytest.raises(HeldError) as exc, hm.env():
        hm.eng.session(hm.bid).resets.reset("dut")
    assert exc.value.data["reason"] == "LEASE"            # the drive gate's reason


def _server_key(hm: Daemon) -> tuple[str, str]:
    adapter = hm.eng.session(hm.bid).lease
    return adapter.host, adapter.target


def test_another_hm_is_refused_with_the_boards_words(world):
    one = world.hm("hm1")
    one.mode_on()
    two = world.hm("hm2")                                 # its own state dir: nothing stored
    shown = two.get("/lease")
    assert shown["lease"]["mine"] is False and shown["lease"]["here"] is False
    # the app's gate says so in plain words, before anything is sent
    with two.env():
        r = two.client.post(two.url("/reset"), json={"target": "dut"}, headers=H)
    assert r.status_code == 409, r.text
    assert "one person at a time" in r.json()["error"]["message"]
    assert world.carried("reset") == []
    # past the gate, the board itself refuses, and it is a LEASE refusal
    with pytest.raises(HeldError) as exc, two.env():
        two.eng.session(two.bid).resets.reset("dut")
    assert exc.value.data["reason"] == "LEASE" and exc.value.data["code"] == "leased"
    assert "has the board" in exc.value.message           # the board's own words
    assert world.carried("reset") == [None]


def test_the_heartbeat_renews_the_lease(world):
    hm = world.hm()
    hm.mode_on()
    before = world.srv.holder["expires"]
    world.srv.tick(600)
    hm.beat()
    after = world.srv.holder["expires"]
    assert after > before                                  # the board took the heartbeat
    assert [a for a, _ in world.srv.acts].count("heartbeat") == 1
    assert world.srv.holder["token"] == stored_token(hm)
    assert hm.get("/lease")["lease"]["here"] is True


def test_a_break_at_the_touchscreen_is_told_as_lease_taken(world):
    hm = world.hm()
    hm.mode_on()
    world.srv.local_break()                                # mode off + history lease_break
    hm.beat()
    taken = [e for e in hm.events if e.topic == "lease.taken"]
    assert len(taken) == 1, [(e.topic, e.data) for e in hm.events]
    assert taken[0].data["by"] == "someone at the board"
    assert "touchscreen" in taken[0].data["reason"]
    assert hm.get("/lease/mode")["single_user"] is False   # the board is shared again
    shown = hm.get("/lease")
    assert shown["lease"] is None and shown["taken"]["by"] == "someone at the board"
    assert stored_token(hm) == ""                          # the token is forgotten


def test_a_restart_reuses_the_stored_token(world):
    hm = world.hm()
    hm.mode_on()
    token = world.srv.holder["token"]
    hm = hm.restart()                                      # a new process: only the state dir
    acquires = [a for a, _ in world.srv.acts].count("acquire")
    shown = hm.get("/lease")
    assert shown["lease"]["mine"] is True and shown["lease"]["here"] is True
    hm.post("/reset", {"target": "dut"})
    assert world.carried("reset") == [token]               # the preamble read it from the store
    hm.beat()
    assert [a for a, _ in world.srv.acts].count("acquire") == acquires == 0
    assert world.srv.holder["token"] == token              # never re-acquired


def test_turning_it_off_ends_our_lease_and_forgets_the_token(world):
    hm = world.hm()
    hm.mode_on()
    job = hm.job("/lease/mode", {"single_user": False, "confirm": True})
    assert job["state"] == "done", job
    assert job["result"]["mode"]["single_user"] is False
    assert stored_token(hm) == ""
    assert hm.get("/lease")["lease"] is None
    hm.post("/reset", {"target": "dut"})                   # shared: no lease needed
    assert world.carried("reset") == [None]


# --- the CLI on a desk board (no daemon, no hub) --------------------------------------------------


def cli(capsys, *argv):
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_lease_mode_on_then_lease_show_on_a_desk_board(lab, capsys, monkeypatch):  # noqa: F811
    from harness_manager.cli import cmd_hub

    rig = lab(claimed=False)                               # reachable directly: a desk board
    srv = attach(rig.fake, BoardLeaseServer(name=BOARD_NAME, claimed=lambda: True))
    monkeypatch.setattr(cmd_hub, "_stdin_is_tty", lambda: False)
    target = f"127.0.0.1:{rig.fake.control_port}"
    rc, out, err = cli(capsys, "lease", "mode", target)
    assert rc == 0 and "shared" in out, (out, err)
    rc, out, err = cli(capsys, "lease", "mode", target, "on", "--yes")
    assert rc == 0 and "one person at a time" in out and "you hold the lease" in out, (out, err)
    assert srv.mode == "single_user" and srv.holder is not None
    token = srv.holder["token"]
    rc, out, err = cli(capsys, "lease", "show", target)
    assert rc == 0, (out, err)
    assert srv.holder["who"] in out                        # the board's holder is shown
    # the CLI keeps what it was given, as the daemon does: a later run can give it back
    from harness_manager.engine import resolve_state_dir

    kept = list((resolve_state_dir() / "leases").glob("*.json"))
    assert kept and token in kept[0].read_text(encoding="utf-8")
    rc, out, err = cli(capsys, "lease", "release", target)
    assert rc == 0 and srv.holder is None, (out, err)


# --- the CLI on a CLAIMED board (HM-FIX1) ---------------------------------------------------------


def _claimed_cli(world, capsys, monkeypatch, *argv):
    from harness_manager.cli import cmd_hub

    monkeypatch.setattr(cmd_hub, "_stdin_is_tty", lambda: False)
    real = Engine.candidate_for                          # as a boards.toml row for the hub path
    monkeypatch.setattr(Engine, "candidate_for", lambda self, target, pack="mps3", via="":
                        real(self, target, pack, via=via or f"ssh:{HUB}"))
    return cli(capsys, *argv)


def test_cli_lease_mode_on_a_board_we_claimed_rides_the_claim_forward(world, capsys, monkeypatch):
    hm = world.hm()                                        # the claim is pinned to this state
    hm.stop()
    with hm.env():
        rc, out, err = _claimed_cli(world, capsys, monkeypatch, "lease", "mode", TARGET,
                                    "on", "--yes")
    assert rc == 0 and "you hold the lease" in out, (out, err)
    assert world.srv.mode == "single_user" and world.srv.holder is not None
    # the write reached the board through the claim forward, not from this PC
    assert world.rig.ssh.board_launches()                  # the claim forward was launched
    assert not [g for g in world.rig.fake.gated if g[0] == "lease"]   # the gate refused nothing


def test_cli_lease_mode_on_a_board_claimed_by_another_is_refused_in_the_claims_words(
        world, capsys, monkeypatch):
    world.rig.fake.claim_gate_all = True
    hm = world.hm(pin=False)                               # no key of ours the board trusts
    hm.stop()
    with hm.env():
        observed_claimed(world.rig.board_id, board_key_fp(), claimed=True)   # known claimed
        sent_before = len(world.rig.mutations("lease"))
        rc, out, err = _claimed_cli(world, capsys, monkeypatch, "lease", "mode", TARGET,
                                    "on", "--yes")
    assert rc != 0 and world.srv.mode != "single_user" and world.srv.holder is None, (out, err)
    assert len(world.rig.mutations("lease")) == sent_before     # nothing was sent
    assert not world.rig.fake.gated and "claim" in err.lower(), err
