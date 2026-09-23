"""Lane L4: the update routes on the real daemon (check, harness, rollback, app, app rollback).

The board is ``VirtualMps3(usb=True)`` opened through the API, so the update service
drives T3's real ``Mps3Storage`` (backup gate, journal) and ``Mps3Controller`` (paced,
witnessed REBOOT) on the fake MCC clock; ``bind_identity_to_sd`` makes the shell come
back as whatever the SD now holds. The channel is T7's ``FakeChannelServer`` on
127.0.0.1, signed with the test keys; the app updater runs a ``FakeUv``. Every check
has a negative twin.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.cli.output import jsonable
from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager.services._unavailable import UnavailableService
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer
from tests.fakes.l4_service import KEYS, H, events_until, hold_board, wait_job, with_update
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_board import FakeUv, bind_identity_to_sd
from tests.fakes.t7_bundles import FIELDED_STATIC, NEW_STATIC, NEW_USERCODE, Release
from tests.fakes.t13_daemon import TOKEN, bid_path, state_dir
from tests.fakes.virtual_board import VirtualMps3

EVENTS = f"/api/v1/events?token={TOKEN}&topics=job.*,update.*"


@pytest.fixture
def vb(tmp_path):
    with VirtualMps3(tmp_path / "board", usb=True) as board:
        yield board


@pytest.fixture
def fake_time(vb, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    vb.mcc.clock = clock
    vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
    return clock


@pytest.fixture
def server(tmp_path) -> Iterator[FakeChannelServer]:
    with FakeChannelServer(tmp_path / "www") as srv:
        yield srv


@pytest.fixture
def world(vb, fake_time, server, tmp_path, monkeypatch) -> Iterator[dict]:
    # A plan made with no channel source falls back to $HARNESS_MANAGER_UPDATE_SOURCE, then
    # GitHub: point the fallback at a local path that does not exist, so no test goes out.
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", str(tmp_path / "no-channel-here"))
    eng = Engine(EngineConfig(state_dir=state_dir()),
                 packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
    uv = FakeUv()
    svc = with_update(eng, uv=uv)
    bus_events: list = []
    eng.bus.subscribe("update.*", bus_events.append)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as client:
            yield {"eng": eng, "svc": svc, "client": client, "vb": vb, "server": server,
                   "builder": ChannelBuilder(server.root, KEYS), "art": tmp_path / "art",
                   "uv": uv, "bus_events": bus_events}
    finally:
        eng.close_all()


def open_usb_board(w, *, usb: bool = True) -> str:
    cand = w["vb"].candidate(usb=usb)
    r = w["client"].post("/api/v1/boards", json={"candidate": jsonable(cand), "note": "l4"},
                         headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def publish(w, *releases: Release, serial: int = 1, **kw) -> None:
    for i, r in enumerate(releases):
        r.add_to(w["builder"], w["art"], current=(i == len(releases) - 1), **kw)
    w["builder"].publish(serial=serial)


def check(w, bid: str | None = None) -> dict:
    """POST /update/check, wait for the job; its result."""
    body = {"source": w["server"].source()} | ({"board_id": bid} if bid else {})
    r = w["client"].post("/api/v1/update/check", json=body, headers=H)
    assert r.status_code == 202, r.text
    state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "done", state
    return state["result"]


def post(w, path: str, body: dict | None = None):
    return w["client"].post(path, json=body or {}, headers=H)


# --- check ---------------------------------------------------------------------------------------


def test_check_returns_the_channel_the_releases_and_the_boards_plan_with_its_fingerprint(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    before = vb.sd.snapshot()
    with w["client"].websocket_connect(EVENTS) as ws:
        r = post(w, "/api/v1/update/check", {"board_id": bid, "source": w["server"].source()})
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        frames = events_until(ws, "job.done", job)
    assert frames[0]["data"] == {"job": job, "kind": "update_check"} and frames[0]["board_id"] == bid
    report = frames[-1]["data"]["result"]
    assert (report["channel"], report["serial"], report["harness_current"]) == ("stable", 1, "1.1.0")
    assert report["signed_by"] == KEYS.release.public.id_hex and report["available"] is True
    rels = report["releases"]["harness"]
    assert [(x["version"], x["current"]) for x in rels] == [("1.1.0", True), ("1.0.0", False)]
    assert rels[0]["static_id"] == FIELDED_STATIC and report["releases"]["app"] == []
    plan = report["plan"]
    assert len(plan["fingerprint"]) == 64 and plan["board_id"] == bid
    assert (plan["mode"], plan["rekey"], plan["blockers"]) == ("full", False, [])
    assert plan["running_release"] == "1.0.0" and isinstance(plan["warnings"], list)
    assert [s["action"] for s in plan["steps"]] == ["download", "verify", "store-overlays",
                                                    "backup-sd", "install-sd", "reboot",
                                                    "confirm-identity"]
    # the service's update.* events reach the socket as they are
    forwarded = [f for f in frames if f["topic"].startswith("update.")]
    assert [(f["topic"], f["board_id"], f["data"]) for f in forwarded] == [
        (e.topic, e.board_id, jsonable(e.data)) for e in w["bus_events"]]
    assert [f["topic"] for f in forwarded] == ["update.available"]
    # read-only: the board is untouched
    assert vb.sd.snapshot() == before and vb.reboots == 0


def test_negative_twin_a_check_without_a_board_has_no_plan_and_runs_in_the_service(world):
    publish(world, Release.fielded(), Release("1.1.0"))
    r = post(world, "/api/v1/update/check", {"source": world["server"].source()})
    state = wait_job(world["client"], r.json()["job"])
    assert state["state"] == "done" and state["board_id"] == "" and state["kind"] == "update_check"
    assert "plan" not in state["result"] and state["result"]["harness_current"] == "1.1.0"
    assert [x["version"] for x in state["result"]["releases"]["harness"]] == ["1.1.0", "1.0.0"]


def test_a_check_that_cannot_verify_the_channel_fails_its_job(world):
    publish(world, Release("1.1.0"))
    world["builder"].publish(serial=1, key=KEYS.rogue)          # signed by a key we do not trust
    r = post(world, "/api/v1/update/check", {"source": world["server"].source()})
    state = wait_job(world["client"], r.json()["job"])
    assert state["state"] == "failed" and state["error"]["code"] == ExitCode.REFUSED


# --- harness ------------------------------------------------------------------------------------


def test_harness_install_runs_the_checked_plan_and_reports_the_boards_word(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    plan = check(w, bid)["plan"]
    with w["client"].websocket_connect(EVENTS) as ws:
        r = post(w, f"{bid_path(bid)}/update/harness", {"fingerprint": plan["fingerprint"]})
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        frames = events_until(ws, "job.done", job)
    out = frames[-1]["data"]["result"]
    assert (out["result"], out["ok"], out["version"]) == ("installed", True, "1.1.0")
    assert out["identity_after"]["harness"] == "1.1.0" and Path(out["backup"]["path"]).is_file()
    topics = [f["topic"] for f in frames]
    assert topics.index("update.started") < topics.index("update.done") < topics.index("job.done")
    done = next(f for f in frames if f["topic"] == "update.done")
    assert done["board_id"] == bid and done["data"]["result"] == "installed"
    # update.progress drives the job's progress: backup, SD write, the witnessed reboot
    phases = [f["data"]["phase"] for f in frames if f["topic"] == "job.progress"]
    assert any(p.startswith("backup:") for p in phases) and any(p.startswith("sd:") for p in phases)
    assert any(p.startswith("reboot:") for p in phases)
    assert vb.reboots == 1
    assert w["client"].get(bid_path(bid), headers=H).json()["identity"]["harness_version"] == "1.1.0"


def test_negative_twin_a_stale_fingerprint_is_refused_before_any_job(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    plan = check(w, bid)["plan"]
    publish(w, Release("1.2.0"), serial=2)                  # the channel moved on since the check
    before = vb.sd.snapshot()
    r = post(w, f"{bid_path(bid)}/update/harness", {"fingerprint": plan["fingerprint"]})
    err = r.json()["error"]
    assert (r.status_code, err["name"]) == (409, "REFUSED") and "changed since" in err["message"]
    assert err["data"]["plan"]["version"] == "1.2.0"
    assert err["data"]["plan"]["fingerprint"] != plan["fingerprint"]
    assert vb.sd.snapshot() == before and vb.reboots == 0
    assert [j["kind"] for j in w["client"].get("/api/v1/jobs", headers=H).json()["jobs"]] == [
        "update_check"]


def test_a_fingerprint_the_daemon_never_issued_is_planned_from_the_boards_last_check(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    B = bid_path(bid)
    # no check yet: the plan comes from the default source (here: a local path, not GitHub)
    r = post(w, f"{B}/update/harness", {"fingerprint": "0" * 64})
    assert r.status_code == 502 and "no-channel-here" in r.json()["error"]["message"]
    plan = check(w, bid)["plan"]
    # after a check, an unknown fingerprint is compared with THAT channel: "changed since"
    r = post(w, f"{B}/update/harness", {"fingerprint": "0" * 64})
    err = r.json()["error"]
    assert (r.status_code, err["name"]) == (409, "REFUSED") and "changed since" in err["message"]
    assert err["data"]["plan"]["fingerprint"] == plan["fingerprint"] and vb.reboots == 0


def test_negative_twin_a_client_that_names_the_source_needs_no_check(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    p, _ = w["svc"].plan_harness(w["eng"].session(bid), source=w["server"].source())
    r = post(w, f"{bid_path(bid)}/update/harness", {"fingerprint": p.fingerprint(),
                                                    "source": w["server"].source()})
    assert r.status_code == 202, r.text
    state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "done" and state["result"]["result"] == "installed", state


def test_a_rekey_needs_the_exact_typed_phrase(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE), rekey=True)
    bid = open_usb_board(w)
    plan = check(w, bid)["plan"]
    assert plan["rekey"] is True and plan["consent_phrase"] == f"REKEY {NEW_STATIC}"
    B = bid_path(bid)
    for phrase in (None, "", "yes", f"REKEY {FIELDED_STATIC}", f"rekey {NEW_STATIC}x"):
        body = {"fingerprint": plan["fingerprint"]} | ({"rekey_phrase": phrase} if phrase is not None
                                                        else {})
        r = post(w, f"{B}/update/harness", body)
        err = r.json()["error"]
        assert (r.status_code, err["name"]) == (409, "REFUSED") and "RE-KEYS" in err["message"]
        assert f"REKEY {NEW_STATIC}" in err["hint"] and err["data"]["plan"]["rekey"] is True
    assert vb.reboots == 0
    # Negative twin: the exact phrase re-keys the board.
    r = post(w, f"{B}/update/harness", {"fingerprint": plan["fingerprint"],
                                        "rekey_phrase": f"REKEY {NEW_STATIC}"})
    assert r.status_code == 202, r.text
    state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "done" and state["result"]["result"] == "installed", state
    assert w["client"].get(B, headers=H).json()["identity"]["shell_id"] == NEW_STATIC


def test_a_plan_with_blockers_is_refused_with_the_plan(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"),
            compat={"min_app": "9.9.9", "board_revs": ["HBI0309C"]})
    bid = open_usb_board(w)
    plan = check(w, bid)["plan"]
    assert plan["blockers"] and "9.9.9" in plan["blockers"][0]
    r = post(w, f"{bid_path(bid)}/update/harness", {"fingerprint": plan["fingerprint"]})
    err = r.json()["error"]
    assert (r.status_code, err["name"]) == (409, "REFUSED") and "cannot update" in err["message"]
    assert err["data"]["plan"]["blockers"] == plan["blockers"] and vb.reboots == 0


def test_written_not_running_fails_the_job_with_the_outcome_and_the_rollback_restores(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb, stale=True)           # the board keeps booting the old image
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    before = vb.sd.snapshot()
    plan = check(w, bid)["plan"]
    r = post(w, f"{bid_path(bid)}/update/harness", {"fingerprint": plan["fingerprint"]})
    state = wait_job(w["client"], r.json()["job"])
    err = state["error"]
    assert state["state"] == "failed" and err["code"] == ExitCode.ACTION_FAILED
    assert "written, not running" in err["message"]
    outcome = err["data"]["outcome"]
    assert outcome["result"] == "written-not-running" and outcome["ok"] is False
    assert outcome["backup"]["path"] in err["hint"] and bid in err["hint"]
    # Negative twin: the rollback restores the SD and the board confirms the old harness.
    with w["client"].websocket_connect(EVENTS) as ws:
        r = post(w, f"{bid_path(bid)}/update/rollback")
        assert r.status_code == 202, r.text
        frames = events_until(ws, "job.done", r.json()["job"])
    back = frames[-1]["data"]["result"]
    assert back["result"] == "restored" and back["identity_after"]["harness"] == "1.0.0"
    assert any(f["data"].get("phase", "").startswith("restore:") for f in frames
               if f["topic"] == "job.progress")
    assert vb.sd.snapshot() == before and vb.reboots == 2


def test_rollback_refusals_are_the_envelope(world):
    w = world
    bid = open_usb_board(w)
    B = bid_path(bid)
    r = post(w, f"{B}/update/rollback", {"backup_path": "relative/backup.zip"})
    assert (r.status_code, r.json()["error"]["name"]) == (400, "USAGE")
    r = post(w, f"{B}/update/rollback", {"backup_path": "/nonexistent/backup.zip"})
    assert (r.status_code, r.json()["error"]["name"]) == (404, "ABSENT")
    r = post(w, f"{B}/update/rollback", {"wait_s": 0})
    assert (r.status_code, r.json()["error"]["name"]) == (400, "USAGE")
    # nothing recorded to roll back to: the job fails REFUSED, and the SD is untouched
    r = post(w, f"{B}/update/rollback")
    state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "failed" and state["error"]["code"] == ExitCode.REFUSED
    assert "no backup" in state["error"]["message"] and w["vb"].reboots == 0


def test_negative_twin_an_ethernet_only_board_cannot_roll_back(world):
    w = world
    bid = open_usb_board(w, usb=False)
    r = post(w, f"{bid_path(bid)}/update/rollback")
    err = r.json()["error"]
    assert (r.status_code, err["name"], err["capability"]) == (422, "UNAVAILABLE", "storage_install")
    assert w["client"].get("/api/v1/jobs", headers=H).json()["jobs"] == []


# --- the board gate --------------------------------------------------------------------------------


def test_update_routes_are_held_while_a_board_job_runs(world):
    w = world
    publish(w, Release.fielded(), Release("1.1.0"))
    bid = open_usb_board(w)
    plan = check(w, bid)["plan"]
    job, release = hold_board(w["client"].app.state.daemon, bid)
    B = bid_path(bid)
    try:
        for r in (post(w, "/api/v1/update/check", {"board_id": bid, "source": w["server"].source()}),
                  post(w, f"{B}/update/harness", {"fingerprint": plan["fingerprint"]}),
                  post(w, f"{B}/update/rollback"),
                  post(w, "/api/v1/update/app"),
                  post(w, "/api/v1/update/app/rollback")):
            err = r.json()["error"]
            assert (r.status_code, err["name"]) == (409, "HELD"), r.text
            assert (err["data"]["job"], err["data"]["kind"]) == (job.id, "deploy")
            assert job.id in err["message"]
        # a check that names no board does not need this board
        r = post(w, "/api/v1/update/check", {"source": w["server"].source()})
        assert r.status_code == 202
        assert wait_job(w["client"], r.json()["job"])["state"] == "done"
    finally:
        release.set()
    assert job.finished.wait(10)
    # Negative twin: the board's check is served once the job ends.
    assert check(w, bid)["plan"]["fingerprint"] == plan["fingerprint"]


# --- the app ---------------------------------------------------------------------------------------


def add_app(w, version: str, serial: int) -> None:
    w["builder"].add_app(version, AssetFile(f"harness_manager-{version}-py3-none-any.whl",
                                            b"PK-wheel-" + version.encode()))
    w["builder"].publish(serial=serial)


def test_app_update_stages_then_switches_only_when_no_board_is_held(world):
    w = world
    add_app(w, "0.2.0", serial=1)
    src = {"source": w["server"].source()}
    bid = open_usb_board(w)
    # T7's switch rail: this service holds the board, so the job stages but does not switch
    state = wait_job(w["client"], post(w, "/api/v1/update/app", src).json()["job"])
    assert state["state"] == "failed" and state["error"]["code"] == ExitCode.HELD
    assert "this process holds" in state["error"]["message"] and state["board_id"] == ""
    assert w["svc"].app().state()["versions"]["0.2.0"]["state"] == "staged"
    assert w["svc"].app().state()["current"] == ""
    # Negative twin: with the board closed, the same request switches
    assert w["client"].delete(bid_path(bid), headers=H).json()["closed"] is True
    state = wait_job(w["client"], post(w, "/api/v1/update/app", src).json()["job"])
    assert state["state"] == "done", state
    out = state["result"]
    assert (out["version"], out["switched"], out["pointer"]["current"]) == ("0.2.0", True, "0.2.0")
    # a second release, then the app rollback puts the first back
    add_app(w, "0.3.0", serial=2)
    state = wait_job(w["client"], post(w, "/api/v1/update/app", src).json()["job"])
    assert state["state"] == "done" and state["result"]["pointer"]["previous"] == "0.2.0"
    with w["client"].websocket_connect(EVENTS) as ws:
        r = post(w, "/api/v1/update/app/rollback")
        assert r.status_code == 202, r.text
        frames = events_until(ws, "job.done", r.json()["job"])
    assert frames[0]["data"]["kind"] == "update_app_rollback" and frames[0]["board_id"] == ""
    back = frames[-1]["data"]["result"]
    assert (back["target"], back["result"], back["state"]["current"]) == ("app", "switched", "0.2.0")


def test_app_rollback_with_nothing_to_go_back_to_fails_its_job(world):
    state = wait_job(world["client"], post(world, "/api/v1/update/app/rollback").json()["job"])
    assert state["state"] == "failed" and state["error"]["code"] == ExitCode.REFUSED
    assert "no previous version" in state["error"]["message"]


def test_an_app_job_in_flight_refuses_a_second_one(world):
    daemon = world["client"].app.state.daemon
    job, release = hold_board(daemon, "", kind="update_app")
    try:
        for path in ("/api/v1/update/app", "/api/v1/update/app/rollback", "/api/v1/update/check"):
            r = post(world, path, {"source": world["server"].source()} if "check" in path else {})
            err = r.json()["error"]
            assert (r.status_code, err["name"], err["data"]["job"]) == (409, "HELD", job.id)
    finally:
        release.set()
    assert job.finished.wait(10)
    r = post(world, "/api/v1/update/app/rollback")
    assert r.status_code == 202                          # the twin: free again


# --- the service, and the envelope -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/update/check", "/api/v1/update/app",
                                  "/api/v1/update/app/rollback", "harness", "rollback"])
def test_an_unavailable_update_service_is_422_with_its_reason(world, path):
    w = world
    bid = open_usb_board(w)
    w["eng"]._services["update"] = UnavailableService("update", "failed to load: boom")
    if not path.startswith("/"):
        path = f"{bid_path(bid)}/update/{path}"
    r = post(w, path, {"fingerprint": "f" * 64})
    err = r.json()["error"]
    assert (r.status_code, err["name"], err["capability"]) == (422, "UNAVAILABLE", "update")
    assert err["reason"] == "failed to load: boom"
    assert w["client"].get("/api/v1/jobs", headers=H).json()["jobs"] == []


def test_negative_twin_an_engine_with_no_update_service_says_so(vb, fake_time):
    class NoUpdate(Engine):
        update = None

    eng = NoUpdate(EngineConfig(state_dir=state_dir()),
                   packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/update/check", json={}, headers=H)
            assert r.status_code == 422 and "no update service" in r.json()["error"]["reason"]
    finally:
        eng.close_all()


@pytest.mark.parametrize("path,body,status,name", [
    ("harness", {}, 400, "USAGE"),                                    # no fingerprint
    ("harness", {"fingerprint": ""}, 400, "USAGE"),
    ("harness", {"fingerprint": "f" * 64, "rekey_phrase": 5}, 400, "USAGE"),
    ("harness", [1], 400, "USAGE"),
    ("/api/v1/update/check", {"board_id": "mps3@nowhere:6900"}, 404, "ABSENT"),
    ("/api/v1/update/check", {"channel": 7}, 400, "USAGE"),
    ("/api/v1/update/app", {"version": 3}, 400, "USAGE"),
    ("/api/v1/update/app", "not an object", 400, "USAGE"),
])
def test_bad_update_requests_are_the_envelope_and_create_no_job(world, path, body, status, name):
    w = world
    bid = open_usb_board(w)
    if not path.startswith("/"):
        path = f"{bid_path(bid)}/update/{path}"
    r = w["client"].post(path, json=body, headers=H)
    assert (r.status_code, r.json()["ok"], r.json()["error"]["name"]) == (status, False, name)
    assert w["client"].get("/api/v1/jobs", headers=H).json()["jobs"] == []


def test_update_routes_need_the_token(world):
    r = world["client"].post("/api/v1/update/check", json={})
    assert r.status_code == 401 and r.json()["error"]["code"] == ExitCode.REFUSED
