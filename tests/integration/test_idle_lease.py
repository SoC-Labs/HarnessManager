"""IDLE-LEASE: an open board nobody uses gives its hub lease back (daemon/idle_api.py).

Each check has a twin. The app runs a real Engine on the L1 lab rig (a virtual MPS3 behind a
fake hub and a fake ssh: only 127.0.0.1 is reached). Time is a fake clock: the idle round
(``IdleLeases.tick``) is called by the test, its thread stopped, so no test waits minutes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.daemon import idle_api as idle_lease
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager.services.lease import LeaseService
from tests.fakes.l1_rig import BOARD_IP, HUB, TARGET, lab
from tests.fakes.t13_daemon import TOKEN, bid_path, headers
from tests.fakes.virtual_board import VirtualMps3

H = headers()
PAGE = {**H, "X-HM-Client": "page"}
MIN = 60.0


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as r:
        yield r


@pytest.fixture
def client(rig) -> Iterator[TestClient]:
    eng = Engine(EngineConfig(state_dir=state_dir()))
    with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
        yield c
    eng.close_all()


@pytest.fixture
def idle(client) -> SimpleNamespace:
    """The daemon's idle watcher on a fake clock, its own thread stopped, the limit 30 min."""
    d = client.app.state.daemon
    w = d.idle
    w.close()                                  # the test runs the rounds
    clock = FakeClock()
    w.clock = clock
    w.wall = lambda: 1_800_000_000.0 + clock.t
    limit = {"min": 30}
    w._limit_min = lambda: limit["min"]
    events: list[dict] = []
    d.bus.subscribe("lease.idle", lambda ev: events.append({"board_id": ev.board_id, **ev.data}))
    return SimpleNamespace(w=w, d=d, clock=clock, limit=limit, events=events)


def open_lab(client: TestClient) -> str:
    r = client.post("/api/v1/boards", json={"target": BOARD_IP, "note": "idle"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def wait_job(client: TestClient, job: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/jobs/{job}", headers=H).json()
        if state["state"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError(f"job {job} still running")


def take_lease(client: TestClient, bid: str) -> None:
    """The app's own acquire (POST /lease): a lease THIS service took."""
    r = client.post(f"{bid_path(bid)}/lease", json={"ttl_s": 600}, headers=H)
    assert r.status_code == 202
    assert wait_job(client, r.json()["job"])["state"] == "done"


def opened_with_lease(client: TestClient, ix: SimpleNamespace) -> str:
    bid = open_lab(client)
    take_lease(client, bid)
    ix.w.touch(bid, "test start")              # the clock starts now (fake time)
    return bid


def run_for(ix: SimpleNamespace, minutes: float, step_s: float = 30.0) -> None:
    """Advance the fake clock ``minutes``, one idle round every ``step_s``."""
    end = ix.clock.t + minutes * MIN
    while ix.clock.t < end:
        ix.clock.advance(min(step_s, end - ix.clock.t))
        ix.w.tick()


def states(ix: SimpleNamespace) -> list[str]:
    return [e["state"] for e in ix.events]


def is_open(client: TestClient, bid: str) -> bool:
    return bid in client.app.state.daemon.engine.open_boards()


# --- idle past the limit: warned, then released and closed -----------------------------------------


def test_idle_past_the_limit_is_warned_then_released_and_the_board_closed(client, rig, idle, caplog):
    bid = opened_with_lease(client, idle)
    caplog.set_level(logging.INFO, logger="harness_manager.daemon.idle_api")
    run_for(idle, 28)
    assert states(idle) == ["warning"]
    warn = idle.events[0]
    assert warn["board_id"] == bid and warn["own"] is True and warn["limit_min"] == 30
    assert warn["text"].endswith("'s lease is released in 2 min: nothing has used it for "
                                 "28 min. Use the board to keep it.")
    assert warn["release_at"].endswith("Z")
    assert is_open(client, bid) and rig.hub.current is not None
    st = client.get(f"{bid_path(bid)}/lease/idle", headers=H).json()
    assert st["warning"] is True and st["own"] is True and st["limit_min"] == 30
    run_for(idle, 2)
    assert states(idle) == ["warning", "released"]
    assert "lease was released after 30 min with nothing using it" in idle.events[-1]["text"]
    assert rig.hub.current is None                              # given back to the hub
    assert not is_open(client, bid)                             # and the board is closed
    assert bid not in idle.d.leases.tracked()                   # no more heartbeats
    assert any("lease released after 30 min idle" in r.getMessage() for r in caplog.records)


def test_negative_twin_not_idle_long_enough_nothing_happens(client, rig, idle):
    bid = opened_with_lease(client, idle)
    run_for(idle, 27.5)
    assert states(idle) == []                                   # 2 min before the limit: not yet
    run_for(idle, 1)
    assert states(idle) == ["warning"]
    run_for(idle, 1.4)                                          # 29.9 min: not released yet
    assert states(idle) == ["warning"]
    assert is_open(client, bid) and rig.hub.current is not None


def test_a_late_round_still_warns_two_minutes_before_releasing(client, rig, idle):
    bid = opened_with_lease(client, idle)
    run_for(idle, 45, step_s=45 * MIN)                          # one round, long after the limit
    assert states(idle) == ["warning"]                          # the warning first, never a surprise
    run_for(idle, 1.9)
    assert states(idle) == ["warning"] and is_open(client, bid)
    run_for(idle, 0.2)
    assert states(idle) == ["warning", "released"] and not is_open(client, bid)


# --- activity keeps the lease -------------------------------------------------------------------------


def test_an_api_request_on_the_board_resets_the_idle_clock(client, rig, idle):
    bid = opened_with_lease(client, idle)
    run_for(idle, 28.5)
    assert states(idle) == ["warning"]
    # The CLI (or a script) asks something about the board: use, even a read.
    assert client.get(f"{bid_path(bid)}/tunnel", headers=H).status_code == 200
    assert states(idle) == ["warning", "kept"]
    run_for(idle, 27)
    assert states(idle) == ["warning", "kept"] and is_open(client, bid)
    run_for(idle, 3.5)
    assert states(idle)[-1] == "released"


def test_negative_twin_the_pages_own_reads_and_background_reads_are_not_use(client, rig, idle):
    bid = opened_with_lease(client, idle)
    run_for(idle, 20)
    # The page polls the lease and the tunnel of every open board, visible or not: not use.
    assert client.get(f"{bid_path(bid)}/tunnel", headers=PAGE).status_code == 200
    assert client.get(f"{bid_path(bid)}/lease", headers=PAGE).status_code == 200
    assert client.get(f"{bid_path(bid)}/tunnel",
                      headers={**H, "X-HM-Background": "1"}).status_code == 200
    assert client.get(f"{bid_path(bid)}/lease/idle", headers=H).status_code == 200
    run_for(idle, 10)
    assert states(idle) == ["warning", "released"] and not is_open(client, bid)


def test_a_page_showing_the_board_holds_it_and_one_that_left_does_not(client, rig, idle):
    bid = opened_with_lease(client, idle)
    r = client.put(f"{bid_path(bid)}/viewers/tab-1", json={"ttl_s": 300}, headers=PAGE)
    assert r.status_code == 200
    run_for(idle, 120)
    assert states(idle) == [] and is_open(client, bid)          # a window shows it: in use
    # Twin: the window closed (or looks at another board): the clock runs from then.
    assert client.delete(f"{bid_path(bid)}/viewers/tab-1", headers=PAGE).status_code == 200
    run_for(idle, 30.5)
    assert states(idle) == ["warning", "released"] and not is_open(client, bid)


def test_an_open_debug_session_holds_the_board(client, rig, idle):
    bid = opened_with_lease(client, idle)
    live = {"on": True}
    stub = SimpleNamespace(running_here=lambda b: live["on"] and b == bid)
    services = idle.d.engine._services
    real = services.get("debug")
    services["debug"] = stub
    try:
        run_for(idle, 90)
        assert states(idle) == [] and is_open(client, bid)
        live["on"] = False                                     # twin: the session ended
        run_for(idle, 30.5)
        assert states(idle) == ["warning", "released"]
    finally:
        if real is None:
            services.pop("debug", None)
        else:
            services["debug"] = real


def test_an_attached_console_holds_the_board(client, rig, idle):
    bid = opened_with_lease(client, idle)
    n = {"subs": 1}
    services = idle.d.engine._services
    real = services.get("consoles")
    services["consoles"] = SimpleNamespace(attached=lambda b: n["subs"] if b == bid else 0,
                                           close_all=lambda b: None)
    try:
        run_for(idle, 45)
        assert states(idle) == []
        n["subs"] = 0                                          # twin: detached
        run_for(idle, 30.5)
        assert states(idle) == ["warning", "released"]
    finally:
        if real is None:
            services.pop("consoles", None)
        else:
            services["consoles"] = real


def test_a_probe_that_fails_counts_as_use(client, rig, idle):
    bid = opened_with_lease(client, idle)

    def broken(_b: str) -> int:
        raise RuntimeError("no idea")

    services = idle.d.engine._services
    real = services.get("consoles")
    services["consoles"] = SimpleNamespace(attached=broken, close_all=lambda b: None)
    try:
        run_for(idle, 60)
        assert states(idle) == [] and is_open(client, bid)
    finally:
        if real is None:
            services.pop("consoles", None)
        else:
            services["consoles"] = real


# --- a running job (a slot write) is never interrupted --------------------------------------------------


def test_a_running_slot_write_is_never_interrupted(client, rig, idle):
    bid = opened_with_lease(client, idle)
    go = threading.Event()
    started = threading.Event()

    def slot_write(progress):
        started.set()
        assert go.wait(20)
        return {"written": True}

    job = idle.d.jobs.submit("slot_push", bid, slot_write)
    assert started.wait(5)
    run_for(idle, 120)                                         # two hours into the write
    assert states(idle) == [] and is_open(client, bid) and rig.hub.current is not None
    assert idle.d.jobs.get(job.id).state == "running"
    go.set()
    assert wait_job(client, job.id)["state"] == "done"
    # The clock starts when the write ends, never from before it.
    run_for(idle, 27.5)
    assert states(idle) == []
    run_for(idle, 3)
    assert states(idle) == ["warning", "released"]


def test_negative_twin_a_job_that_starts_after_the_check_still_refuses_the_close(client, rig, idle):
    """Belt and braces: the close itself goes through the user's Close, which a running job
    refuses (HELD), so even a job that slipped in after the round looked is never cut."""
    bid = opened_with_lease(client, idle)
    run_for(idle, 28)
    assert states(idle) == ["warning"]
    go = threading.Event()
    started = threading.Event()

    def write(progress):
        started.set()
        assert go.wait(20)

    job = idle.d.jobs.submit("sd_install", bid, write)
    assert started.wait(5)
    idle.w.holds = lambda b: []                                # the round missed it
    run_for(idle, 2.5)
    assert states(idle) == ["warning", "failed"]
    assert "was not released" in idle.events[-1]["text"]
    assert is_open(client, bid) and rig.hub.current is not None
    assert idle.d.jobs.get(job.id).state == "running"
    go.set()
    wait_job(client, job.id)


# --- the setting -----------------------------------------------------------------------------------------


def test_setting_zero_never_releases(client, rig, idle):
    bid = opened_with_lease(client, idle)
    idle.limit["min"] = 0
    run_for(idle, 600, step_s=5 * MIN)
    assert states(idle) == [] and is_open(client, bid) and rig.hub.current is not None
    st = client.get(f"{bid_path(bid)}/lease/idle", headers=H).json()
    assert st["watched"] is False and st["limit_min"] == 0
    # Twin: set it back to 30: the clock starts from then, not from when it was off.
    idle.limit["min"] = 30
    run_for(idle, 27.5)
    assert states(idle) == []
    run_for(idle, 3)
    assert states(idle) == ["warning", "released"]


def test_the_setting_row_is_declared_with_its_default_and_bounds():
    from harness_manager.settings.rows import CORE_ROWS

    row = next(r for r in CORE_ROWS if r.key == "lease.idle_release_min")
    assert row.type == "int" and row.default == 30 and row.apply == "live"
    assert "0: never" in row.doc
    assert row.check(0) == "" and row.check(30) == "" and row.check(1440) == ""
    assert row.check(-1) and row.check(1441)                  # twin: out of range is refused


def test_the_setting_is_read_from_the_settings_file(tmp_path):
    d = SimpleNamespace(state_dir=tmp_path)
    w = idle_lease.IdleLeases(d)
    assert w.limit_s() == 30 * MIN                             # the default
    (tmp_path / "settings.toml").write_text("[lease]\nidle_release_min = 0\n", encoding="utf-8")
    from harness_manager.settings import runtime

    runtime.refresh()
    assert w.limit_s() == 0                                    # twin: the user's 0 is read


# --- "Keep it" ------------------------------------------------------------------------------------------


def test_keep_it_resets_the_clock(client, rig, idle):
    bid = opened_with_lease(client, idle)
    run_for(idle, 28.5)
    assert states(idle) == ["warning"]
    r = client.post(f"{bid_path(bid)}/lease/keep", headers=PAGE)
    assert r.status_code == 200 and r.json()["warning"] is False
    assert states(idle) == ["warning", "kept"]
    run_for(idle, 5)
    assert states(idle) == ["warning", "kept"] and is_open(client, bid)


def test_negative_twin_keep_it_on_a_closed_board_is_404(client, rig, idle):
    r = client.post(f"{bid_path('mps3@nowhere:6900')}/lease/keep", headers=PAGE)
    assert r.status_code == 404 and r.json()["error"]["code"] == ExitCode.ABSENT


# --- only a lease this service took is released ------------------------------------------------------------


def cli_acquire(client: TestClient, bid: str) -> None:
    """``harness-manager lease acquire`` in a terminal: its own LeaseService, same store."""
    hub = client.app.state.daemon.engine.session(bid).hub
    out = LeaseService(state_dir()).acquire(hub, board_id=bid, ttl_s=600, heartbeat=False)
    assert out["lease"]["target"] == TARGET


def test_a_lease_taken_in_a_terminal_is_never_released_the_board_only_closes(client, rig, idle):
    bid = open_lab(client)
    cli_acquire(client, bid)
    idle.w.touch(bid)
    held = dict(rig.hub.current)
    assert not idle.d.leases.acquired_here(idle.d.engine.session(bid).hub)
    run_for(idle, 28)
    assert states(idle) == ["warning"] and idle.events[0]["own"] is False
    assert "is closed in 2 min" in idle.events[0]["text"]
    assert "not released here" in idle.events[0]["text"]
    run_for(idle, 2)
    assert states(idle) == ["warning", "closed"]
    assert "it was not released" in idle.events[-1]["text"]
    assert rig.hub.current == held                             # still the terminal's lease
    assert not is_open(client, bid)                            # the heartbeat stopped with the close
    assert idle.d.leases.store.get(HUB, TARGET) is not None    # its token stays for the terminal


def test_twin_a_lease_the_service_took_is_marked_and_released(client, rig, idle):
    bid = opened_with_lease(client, idle)
    hub = idle.d.engine.session(bid).hub
    assert idle.d.leases.acquired_here(hub)
    # A terminal that takes the board afterwards (a new token) makes it not ours again.
    stored = idle.d.leases.store.get(HUB, TARGET)
    LeaseService(state_dir())._store_grant(hub, stored.holder, SimpleNamespace(token="tok-new"),
                                           stored.expires_at, 600)
    assert not idle.d.leases.acquired_here(hub)


def test_a_board_with_no_lease_held_here_is_not_watched(client, rig, idle):
    bid = open_lab(client)
    idle.w.touch(bid)
    run_for(idle, 90, step_s=5 * MIN)
    assert states(idle) == [] and is_open(client, bid)        # nothing of ours to give back
    # Twin: a lease taken later starts the clock from then.
    take_lease(client, bid)
    run_for(idle, 27.5)
    assert states(idle) == []
    run_for(idle, 3)
    assert states(idle) == ["warning", "released"]


# --- the rule, as a table -------------------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,hdrs,use", [
    ("GET", "/api/v1/boards/b/tunnel", {}, True),                           # the CLI reads
    ("POST", "/api/v1/boards/b/deploy", {}, True),
    ("GET", "/api/v1/boards/b/tunnel", {"x-hm-client": "page"}, False),     # the page polls
    ("POST", "/api/v1/boards/b/deploy", {"x-hm-client": "page"}, True),     # the page acts
    ("GET", "/api/v1/boards/b", {"x-hm-background": "1"}, False),
    ("PUT", "/api/v1/boards/b/viewers/v1", {"x-hm-client": "page"}, False),  # counted as a viewer
    ("GET", "/api/v1/boards/b/lease/idle", {}, False),
])
def test_which_requests_are_use(method, path, hdrs, use):
    assert idle_lease.page_request_is_use(method, path, hdrs) is use
