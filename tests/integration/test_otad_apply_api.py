"""Lane OTA-D: ``POST /update/app/apply`` and friends, in process (the restart itself is in
``test_otad_restart_process.py``, with real processes).

The daemon app runs under FastAPI's TestClient over the real Engine and a virtual MPS3 (a
console with a real PTY). The update service's app updater points at an install root in
``tmp_path`` with the installer's venv registered and 0.2.0 staged. The apply step's three
outside effects are replaced by recorders: the new version's ``--self-test``, the detached
helper, and the daemon's own shutdown. Every check has a negative twin.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager import __version__, _launch
from harness_manager.core.errors import ExitCode
from harness_manager.core.session import SessionLock
from harness_manager.daemon import update_apply as ua
from harness_manager.daemon.app import create_app
from harness_manager.services import pty as ptymod
from harness_manager.services.update import UpdateService
from harness_manager.services.update import selfupdate as su
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.policy import Policy
from tests.fakes.l2_rig import (
    PtyClient,
    apply_pack_ccr,
    fast_pty_options,
    install_uart_baud_codec,
    l2_virtual_board,
    pty_dir,
    wait_for,
)
from tests.fakes.l4_service import KEYS, hold_board, wait_job
from tests.fakes.t7_board import FakeUv
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers, state_dir

pytestmark = pytest.mark.skipif(not ptymod.supported(), reason="PTYs need a POSIX system")

H = headers()
NEW = "0.2.0"


@pytest.fixture(autouse=True)
def _wiring(tmp_path: Path, monkeypatch) -> None:
    apply_pack_ccr(monkeypatch)
    install_uart_baud_codec(monkeypatch)
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(pty_dir(tmp_path)))


def install_root(tmp_path: Path) -> Path:
    """The installer's venv (current ``""``) and 0.2.0 staged beside it."""
    root = tmp_path / "root"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("installer python")
    _launch.register(root, root / "venv", __version__, extras=[], windows=False)
    (root / "versions" / NEW / "bin").mkdir(parents=True)
    (root / "versions" / NEW / "bin" / "python").write_text("0.2.0 python")
    ptr = json.loads((root / "current.json").read_text())
    ptr["versions"] = {NEW: {"state": "staged", "at": 0.0}}
    (root / "current.json").write_text(json.dumps(ptr))
    return root


class Recorder:
    def __init__(self) -> None:
        self.spawned: list[tuple[list[str], dict[str, str]]] = []
        self.shutdowns = 0
        self.self_tests: list[str] = []
        self.self_test_says = ""

    def spawn(self, argv: list[str], env: dict[str, str], log: Path, cwd: Path) -> Any:
        self.spawned.append((argv, env))
        return type("Proc", (), {"pid": 424242})()

    def shutdown(self) -> None:
        self.shutdowns += 1

    def self_test(self, python: Path) -> str:
        self.self_tests.append(str(python))
        return self.self_test_says


@pytest.fixture
def world(tmp_path: Path):
    root = install_root(tmp_path)
    rec = Recorder()
    with l2_virtual_board(tmp_path / "vb") as vb:
        eng = engine_for(vb)
        eng.consoles._pty_options.update(fast_pty_options())
        app_up = AppUpdater(AppLayout(root), LocalBusyProbe(state_dir()), uv="/opt/uv",
                            runner=FakeUv(), python_version="3.11", running_version=__version__,
                            windows=False, state_dir=state_dir())
        svc = UpdateService(eng, trust=KEYS.trust(), token="", app_version=__version__,
                            app_updater=app_up, policy=Policy())
        eng._services["update"] = svc
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None,
                                       shutdown=rec.shutdown)) as client:
                d = client.app.state.daemon
                d.runtime = {"port": 4242, "listen": "127.0.0.1", "log_level": "info",
                             "pack_overrides": {"mps3": {"x": 1}}, "demo": False}
                applier = d.update_applier
                applier.prefix = str(root / "venv")
                applier.spawn = rec.spawn
                applier.self_test = rec.self_test
                applier.grace_s = 0.2
                yield {"client": client, "d": d, "rec": rec, "svc": svc, "root": root, "vb": vb,
                       "eng": eng, "applier": applier}
        finally:
            eng.close_all()


def open_board(w: dict) -> str:
    r = w["client"].post("/api/v1/boards", json={"target": w["vb"].shell_endpoint,
                                                 "note": "otad"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def apply(w: dict, **body: Any):
    return w["client"].post("/api/v1/update/app/apply", json=body, headers=H)


def apply_state(w: dict) -> dict:
    return w["client"].get("/api/v1/update/app", headers=H).json()["apply"]


def wait_idle(w: dict, timeout: float = 20.0) -> dict:
    return wait_for(lambda: (s := apply_state(w))["state"] == "idle" and s, timeout=timeout,
                    what="the apply to end")


def wait_restarted(w: dict, timeout: float = 20.0) -> None:
    wait_for(lambda: w["rec"].shutdowns > 0, timeout=timeout, what="the daemon's shutdown")


# --- refusals before anything happens --------------------------------------------------------


@pytest.mark.parametrize("case,status,name,words", [
    ("not staged", 409, "REFUSED", "is not staged"),
    ("marked bad", 409, "REFUSED", "marked bad"),
    ("another process holds a board", 409, "HELD", "in use by"),
    ("not started by harness-manager daemon", 422, "UNAVAILABLE", "cannot restart itself"),
    ("a developer install", 409, "REFUSED", "developer install"),
    ("health_s out of range", 400, "USAGE", "health_s"),
])
def test_apply_refuses_what_it_cannot_do_and_changes_nothing(world, case, status, name, words):
    w = world
    body: dict[str, Any] = {"version": NEW}
    if case == "not staged":
        body["version"] = "0.9.0"
    elif case == "marked bad":
        w["svc"].app().mark_bad(NEW, "it failed its health check", phase="health")
    elif case == "another process holds a board":
        lock = SessionLock("mps3@elsewhere", lock_dir=state_dir() / "locks")
        lock.acquire()
        data = json.loads(lock.path.read_text())
        data["pid"] = os.getppid() or 1                 # a live process that is not this one
        lock.path.write_text(json.dumps(data))
    elif case == "not started by harness-manager daemon":
        w["d"].runtime = None
    elif case == "a developer install":
        w["svc"].app().dev_install = "this is a developer install (pip install -e)"
    else:
        body["health_s"] = 1
    r = apply(w, **body)
    err = r.json()["error"]
    assert (r.status_code, err["name"]) == (status, name), r.text
    assert words in err["message"] or words in err.get("reason", "")
    assert apply_state(w)["state"] == "idle" and w["rec"].spawned == []
    assert not su.resume_path(state_dir()).exists()


def test_negative_twin_a_staged_version_with_nothing_in_the_way_is_applied(world):
    w = world
    r = apply(w, version=NEW, stable_s=2)
    assert r.status_code == 202, r.text
    assert r.json()["apply"]["to"] == NEW
    wait_restarted(w)
    argv, env = w["rec"].spawned[0]
    assert argv[:3] == [sys.executable, "-m", "harness_manager.daemon.update_apply"]
    a = ua._parser().parse_args(argv[3:])
    assert (a.to, a.from_version, a.from_pointer, a.old_pid) == (NEW, __version__, "", os.getpid())
    assert (a.stable_s, a.root) == (2.0, str(w["root"]))
    assert TOKEN not in " ".join(argv)                   # the token never travels in argv
    assert w["rec"].self_tests == [str(w["root"] / "versions" / NEW / "bin" / "python")]


# --- soft busy -------------------------------------------------------------------------------


def test_soft_busy_screen_needs_confirm_and_gets_the_notice(world):
    w = world
    bid = open_board(w)
    B = bid_path(bid)
    pty = w["client"].post(f"{B}/consoles/uart0/pty", headers=H).json()
    with PtyClient(pty["path"]) as term:
        wait_for(lambda: w["client"].get(f"{B}/consoles/uart0/pty", headers=H)
                 .json()["pty"]["clients"] == 1, what="screen attached")
        r = apply(w, version=NEW)
        err = r.json()["error"]
        assert (r.status_code, err["name"], err["data"]["reason"]) == (409, "REFUSED", "SOFT_BUSY")
        busy = err["data"]["soft_busy"]
        assert [(b["kind"], b["board_id"], b["path"]) for b in busy] == [("screen", bid, pty["path"])]
        assert w["rec"].spawned == [] and apply_state(w)["state"] == "idle"
        # the twin: confirmed, it goes ahead, and screen is told how to come back
        r = apply(w, version=NEW, confirm=True)
        assert r.status_code == 202, r.text
        got = term.read_until(b"re-attach with", timeout=20)
    wait_restarted(w)
    text = got.decode(errors="replace")
    assert f"restarting for an update to {NEW}" in text and f"screen {pty['path']}" in text
    resume = json.loads(su.resume_path(state_dir()).read_text())
    assert resume["token"] == TOKEN and resume["port"] == 4242 and resume["to"] == NEW
    assert resume["pack_overrides"] == {"mps3": {"x": 1}}
    board = resume["boards"][0]
    assert board["board_id"] == bid and board["note"] == "otad"
    assert board["consoles"] == [{"name": "uart0", "path": pty["path"]}]
    assert su.resume_path(state_dir()).stat().st_mode & 0o777 == 0o600


# --- drain --------------------------------------------------------------------------------------


def test_drain_refuses_new_jobs_and_waits_for_the_running_ones(world):
    w = world
    bid = open_board(w)
    job, release = hold_board(w["d"], bid)
    try:
        r = apply(w, version=NEW)
        assert r.status_code == 202, r.text
        st = wait_for(lambda: (s := apply_state(w))["state"] == "draining" and s,
                      what="draining")
        assert [j["job"] for j in st["waiting_on"]] == [job.id]
        # a new job is refused at once, and says why
        r = w["client"].post("/api/v1/update/check", json={}, headers=H)
        err = r.json()["error"]
        assert (r.status_code, err["name"], err["data"]["reason"]) == (409, "HELD", "DRAINING")
        assert err["data"]["version"] == NEW
        time.sleep(0.5)
        assert w["rec"].spawned == [] and w["rec"].shutdowns == 0     # still waiting
    finally:
        release.set()
    assert job.finished.wait(10) and job.state == "done"              # it finished, not killed
    wait_restarted(w)
    assert len(w["rec"].spawned) == 1


def test_negative_twin_cancel_ends_the_drain_and_jobs_are_accepted_again(world):
    w = world
    bid = open_board(w)
    job, release = hold_board(w["d"], bid)
    try:
        assert apply(w, version=NEW).status_code == 202
        wait_for(lambda: apply_state(w)["state"] == "draining", what="draining")
        r = w["client"].post("/api/v1/update/app/cancel", headers=H)
        assert r.status_code == 200 and r.json()["apply"]["state"] == "idle", r.text
        assert r.json()["apply"]["last"]["result"] == "cancelled"
        r = w["client"].post("/api/v1/update/check", json={}, headers=H)
        assert r.status_code == 202, r.text                            # accepted again
        wait_job(w["client"], r.json()["job"])
    finally:
        release.set()
    assert job.finished.wait(10)
    time.sleep(0.5)
    assert w["rec"].spawned == [] and w["rec"].shutdowns == 0
    assert not su.resume_path(state_dir()).exists()
    r = w["client"].post("/api/v1/update/app/cancel", headers=H)
    assert (r.status_code, r.json()["error"]["name"]) == (409, "ALREADY")


def test_a_second_apply_while_one_is_pending_is_held(world):
    w = world
    bid = open_board(w)
    job, release = hold_board(w["d"], bid)
    try:
        assert apply(w, version=NEW).status_code == 202
        r = apply(w, version=NEW)
        err = r.json()["error"]
        assert (r.status_code, err["name"], err["data"]["reason"]) == (409, "HELD", "APPLYING")
    finally:
        release.set()
    wait_restarted(w)
    assert len(w["rec"].spawned) == 1


# --- self-test --------------------------------------------------------------------------------


def test_a_version_that_fails_its_self_test_is_marked_bad_and_nothing_restarts(world):
    w = world
    w["rec"].self_test_says = "its self-test failed (exit 1): ImportError: boom"
    assert apply(w, version=NEW).status_code == 202
    st = wait_idle(w)
    assert st["last"]["result"] == "failed" and "self-test" in st["last"]["reason"]
    assert w["rec"].spawned == [] and w["rec"].shutdowns == 0
    assert w["svc"].app().bad(NEW)["phase"] == "self-test"
    from harness_manager.services.update.appstage import bad_reason

    assert bad_reason(w["svc"].state, NEW)["phase"] == "self-test"      # OTA-C's store too
    last = json.loads(su.last_apply_path(state_dir()).read_text())
    assert (last["result"], last["to"]) == ("refused", NEW)
    # jobs are accepted again, and the bad version is never applied again (the twin)
    assert w["client"].post("/api/v1/update/check", json={}, headers=H).status_code == 202
    r = apply(w, version=NEW)
    assert (r.status_code, r.json()["error"]["name"]) == (409, "REFUSED")
    assert "marked bad" in r.json()["error"]["message"]


# --- status, settings, stage_only ---------------------------------------------------------------


def test_status_shows_the_pointer_the_staged_version_and_the_effective_mode(world):
    w = world
    body = w["client"].get("/api/v1/update/app", headers=H).json()
    assert body["ok"] and body["running"] == __version__
    assert body["staged"] == [NEW] and body["bad"] == {} and body["pointer"]["current"] == ""
    assert body["effective"]["auto"] == "stage" and body["apply"]["state"] == "idle"
    w["svc"].app().mark_bad(NEW, "exited while starting", phase="start")
    body = w["client"].get("/api/v1/update/app", headers=H).json()
    assert body["staged"] == [] and body["bad"][NEW]["reason"] == "exited while starting"


def test_settings_are_capped_by_the_admin_policy(world):
    w = world
    c = w["client"]
    r = c.put("/api/v1/update/settings", json={"auto": "notify", "channel": "beta"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == {"channel": "beta", "auto": "notify"}
    assert r.json()["effective"]["auto"] == "notify"
    assert c.get("/api/v1/update/settings", headers=H).json()["settings"]["auto"] == "notify"
    # the administrator allows at most notify; a user's "stage" is capped, and says why
    w["svc"].policy = Policy(path="/etc/harness-manager/policy.toml", self_update="notify",
                             channel="stable")
    r = c.put("/api/v1/update/settings", json={"auto": "stage", "channel": ""}, headers=H)
    eff = r.json()["effective"]
    assert eff["auto"] == "notify" and "allows at most" in eff["why"] and eff["channel"] == "stable"
    # negative twins: a pinned channel, a bad mode, an unknown key
    for bad, status, name in (({"channel": "beta"}, 409, "REFUSED"),
                              ({"auto": "always"}, 400, "USAGE"),
                              ({"hold": "0.2"}, 400, "USAGE")):
        r = c.put("/api/v1/update/settings", json=bad, headers=H)
        assert (r.status_code, r.json()["error"]["name"]) == (status, name), bad


def test_stage_only_stages_and_never_switches_even_with_a_board_open(world):
    from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer

    w = world
    open_board(w)
    with FakeChannelServer(w["root"].parent / "www") as srv:
        builder = ChannelBuilder(srv.root, KEYS)
        builder.add_app("0.3.0", AssetFile("harness_manager-0.3.0-py3-none-any.whl", b"PK-w"))
        builder.publish(serial=1)
        r = w["client"].post("/api/v1/update/app", headers=H,
                             json={"source": srv.source(), "stage_only": True})
        state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "done", state
    assert (state["result"]["version"], state["result"]["staged"]) == ("0.3.0", True)
    ptr = w["svc"].app().state()
    assert ptr["current"] == "" and ptr["versions"]["0.3.0"]["state"] == "staged"


# --- the resumed daemon's side ----------------------------------------------------------------


def test_a_resumed_daemon_reopens_the_boards_and_the_ptys_at_the_same_paths(world, tmp_path):
    w = world
    bid = open_board(w)
    B = bid_path(bid)
    pty = w["client"].post(f"{B}/consoles/uart0/pty", headers=H).json()
    plan = {"id": "r1", "from": __version__, "from_pointer": "", "to": NEW}
    resume = ua.snapshot(w["d"], plan)
    # the old daemon goes: the board closes, the PTY link goes with it
    assert w["client"].delete(B, headers=H).json()["closed"] is True
    assert not os.path.lexists(pty["path"])
    report = ua.reopen_boards(w["d"], resume)
    assert report["opened"] == [bid] and report["failed"] == []
    assert report["ptys"] == [{"board_id": bid, "name": "uart0", "path": pty["path"],
                               "was": pty["path"]}]
    assert os.readlink(pty["path"]).startswith("/dev/pts/")
    owner = w["eng"].lock_owner(bid)
    assert owner is not None and owner.note == "harness-manager-daemon: otad"
    # the verdict: once the helper writes it for THIS resume, the event goes out
    seen: list = []
    w["d"].bus.subscribe("update.*", seen.append)
    su.write_json(su.last_apply_path(state_dir()), {"id": "other", "result": "applied"})
    stop = threading.Event()
    t = threading.Thread(target=ua.await_verdict, args=(w["d"], resume),
                         kwargs={"wait_s": 10, "stop": stop, "poll_s": 0.05})
    t.start()
    time.sleep(0.3)
    assert [e for e in seen if e.topic == "update.applied"] == []      # not its id: ignored
    su.write_json(su.last_apply_path(state_dir()), {"id": "r1", "result": "applied",
                                                   "from": __version__, "to": NEW,
                                                   "seconds": 7.5})
    t.join(10)
    ev = [e for e in seen if e.topic == "update.applied"]
    assert len(ev) == 1 and ev[0].data == {"id": "r1", "from": __version__, "to": NEW,
                                           "seconds": 7.5}


def test_negative_twin_what_cannot_be_reopened_is_reported_and_the_rest_goes_on(world):
    from harness_manager.cli.output import jsonable

    w = world
    bid = open_board(w)
    cand = jsonable(w["eng"].session(bid).candidate)
    w["client"].delete(bid_path(bid), headers=H)
    gone = {**cand, "pack": "nope", "board_id": "nope@x"}
    resume = {"boards": [
        {"board_id": "nope@x", "note": "", "candidate": gone, "consoles": []},
        {"board_id": bid, "note": "otad", "candidate": cand,
         "consoles": [{"name": "no-such-console", "path": "/tmp/x"}]}]}
    report = ua.reopen_boards(w["d"], resume)
    assert report["opened"] == [bid] and report["ptys"] == []
    assert [(f["board_id"], f["what"]) for f in report["failed"]] == [
        ("nope@x", "open"), (bid, "pty no-such-console")]


def test_read_resume_refuses_a_file_that_is_not_one(tmp_path):
    from harness_manager.core.errors import UsageError

    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"schema": 99, "token": "x", "port": 1}))
    with pytest.raises(UsageError, match="schema 99"):
        ua.read_resume(bad)
    with pytest.raises(UsageError, match="not a resume file"):
        ua.read_resume(tmp_path / "missing.json")
    good = tmp_path / "g.json"
    good.write_text(json.dumps({"schema": 1, "token": "t", "port": 5}))
    assert ua.read_resume(good)["port"] == 5
    assert ExitCode.USAGE == 2


def test_soft_busy_lists_gdb_and_xvc_sessions_and_confirm_goes_ahead(world):
    from harness_manager.core.services import DebugStatus

    w = world
    bid = open_board(w)

    class Debug:
        reason = None

        def status(self, session):
            return DebugStatus(state="up", gdb_port=3333, pid=99)

        def down(self, session):
            return DebugStatus(state="down")

    class Xvc:
        reason = None

        def status(self, session):
            return type("St", (), {"open": True,
                                   "attached": {"command": "vivado", "pid": 7}})()

        def close(self, session, reason=""):
            return None

    w["eng"]._services["debug"] = Debug()
    w["eng"]._services["xvc"] = Xvc()
    r = apply(w, version=NEW)
    err = r.json()["error"]
    assert (r.status_code, err["data"]["reason"]) == (409, "SOFT_BUSY"), r.text
    rows = {b["kind"]: b for b in err["data"]["soft_busy"]}
    assert set(rows) == {"gdb", "xvc"} and rows["gdb"]["board_id"] == bid
    assert "port 3333" in rows["gdb"]["detail"] and "vivado attached" in rows["xvc"]["detail"]
    assert "OpenOCD for GDB" in err["message"]
    assert apply(w, version=NEW, confirm=True).status_code == 202          # the twin
    wait_restarted(w)
