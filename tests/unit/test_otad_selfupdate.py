"""Lane OTA-D: the self-update records (settings, bad marks), the offer that skips a bad
version, prune that never deletes a venv in use, and the drain of the job manager.

The install root is a directory in ``tmp_path`` with the installer's venv registered;
``FakeUv`` builds nothing. The in-use check starts one real child process whose argv[0]
is inside a version's venv (Linux ``/proc``). Every behaviour has a negative twin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from harness_manager import _launch
from harness_manager.core.errors import HeldError, RefusedError, UsageError
from harness_manager.core.events import EventBus
from harness_manager.daemon.jobs import BoardGates, JobManager
from harness_manager.services.update import selfupdate as su
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.policy import Policy
from harness_manager.services.update.schema import AppRelease, Asset
from tests.fakes.t7_board import FakeUv

SHA = "ab" * 32


def release(version: str) -> AppRelease:
    return AppRelease(version=version, status="current",
                      wheel=Asset(name=f"harness_manager-{version}-py3-none-any.whl",
                                  url="https://x/w.whl", sha256=SHA, size=4))


def root_with(tmp_path: Path, *staged: str) -> Path:
    root = tmp_path / "root"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("installer python")
    _launch.register(root, root / "venv", "0.1.0", extras=[], windows=False)
    ptr = json.loads((root / "current.json").read_text())
    for v in staged:
        (root / "versions" / v / "bin").mkdir(parents=True)
        (root / "versions" / v / "bin" / "python").write_text(f"{v} python")
        ptr["versions"][v] = {"state": "staged", "at": 0.0}
    (root / "current.json").write_text(json.dumps(ptr))
    return root


def updater(root: Path, tmp_path: Path, running: str = "0.1.0") -> AppUpdater:
    return AppUpdater(AppLayout(root), LocalBusyProbe(tmp_path / "state"), uv="/opt/uv",
                      runner=FakeUv(), python_version="3.11", running_version=running,
                      windows=False, state_dir=tmp_path / "state")


def store_marks(tmp_path: Path) -> dict:
    """OTA-C's catalogue store: <state>/update/bad_versions.json, catalogue hm-app."""
    path = tmp_path / "state" / "update" / "bad_versions.json"
    return json.loads(path.read_text()).get("hm-app", {}) if path.exists() else {}


# --- settings and the effective mode ------------------------------------------------------------


def test_the_default_is_stage_and_the_stricter_of_policy_and_settings_wins(tmp_path):
    sd = tmp_path / "state"
    assert su.effective(Policy(), su.load_settings(sd))["auto"] == "stage"      # U3
    su.save_settings(sd, auto="stage")
    eff = su.effective(Policy(path="/etc/p.toml", self_update="notify"), su.load_settings(sd))
    assert eff["auto"] == "notify" and "allows at most 'notify'" in eff["why"]
    eff = su.effective(Policy(path="/etc/p.toml", self_update="off"), su.load_settings(sd))
    assert eff["auto"] == "off" and "turns self-update off" in eff["why"]
    # negative twins: the user may be stricter than the policy, and a dev install is off
    su.save_settings(sd, auto="notify")
    assert su.effective(Policy(), su.load_settings(sd))["auto"] == "notify"
    su.save_settings(sd, auto="off")
    assert su.effective(Policy(), su.load_settings(sd))["why"] == \
        "your settings turn self-update off"
    eff = su.effective(Policy(), su.Settings(), blocked="this is a developer install")
    assert eff == {"auto": "off", "channel": "", "check_interval_s": 21600,
                   "why": "this is a developer install"}


def test_settings_refuse_a_bad_mode_and_a_channel_the_policy_does_not_pin(tmp_path):
    sd = tmp_path / "state"
    with pytest.raises(UsageError, match="auto must be one of"):
        su.save_settings(sd, auto="always")
    with pytest.raises(UsageError, match="channel name"):
        su.save_settings(sd, channel="Beta!")
    pinned = Policy(path="/etc/p.toml", channel="stable")
    with pytest.raises(RefusedError, match="pins the 'stable' channel"):
        su.save_settings(sd, channel="beta", policy=pinned)
    assert not su.settings_path(sd).exists()                     # nothing written
    # the twin: the pinned channel, or none, is accepted
    assert su.save_settings(sd, channel="stable", policy=pinned).channel == "stable"
    assert su.save_settings(sd, channel="", policy=pinned).channel == ""


# --- bad versions ----------------------------------------------------------------------------------


def offered(up: AppUpdater, tmp_path: Path, *releases: AppRelease):
    """OTA-C's offer (``appstage.offer_app``) over this updater."""
    from harness_manager.services.update.appstage import offer_app
    from harness_manager.services.update.schema import BoardSpec, Channel
    from harness_manager.services.update.state import UpdateState

    ch = Channel(channel="stable", serial=1, issued_at="", signing_key_id="", board=BoardSpec(),
                 harness_current="", harness=(), app_current=releases[0].version,
                 app=tuple(releases))
    return offer_app(ch, "0.1.0", UpdateState.under(tmp_path / "state"), app=up)


def test_a_bad_version_is_never_offered_staged_or_switched_to_again(tmp_path):
    root = root_with(tmp_path, "0.2.0")
    up = updater(root, tmp_path)
    assert offered(up, tmp_path, release("0.2.0")).release.version == "0.2.0"
    up.mark_bad("0.2.0", "the daemon exited with code 1 while starting", phase="start")
    verdict = offered(up, tmp_path, release("0.2.0"))
    assert verdict.release is None and verdict.skipped_bad == "0.2.0"
    for call in (lambda: up.stage(release("0.2.0"), tmp_path / "w.whl"),
                 lambda: up.switch("0.2.0")):
        with pytest.raises(RefusedError, match="marked bad"):
            call()
    assert store_marks(tmp_path)["0.2.0"]["phase"] == "start"         # OTA-C's store
    assert up.state()["versions"]["0.2.0"]["state"] == "bad"          # and the pointer
    # OTA-C's own offer honours it too
    from harness_manager.services.update.appstage import bad_reason
    from harness_manager.services.update.state import UpdateState

    assert bad_reason(UpdateState.under(tmp_path / "state"), "0.2.0")["phase"] == "start"
    # negative twin: a NEWER release is offered as usual
    assert offered(up, tmp_path, release("0.3.0"), release("0.2.0")).release.version == "0.3.0"


def test_the_bad_mark_outlives_prune_of_its_venv(tmp_path):
    root = root_with(tmp_path, "0.2.0", "0.3.0", "0.4.0", "0.5.0")
    up = updater(root, tmp_path)
    st = up.state()
    st["current"], st["previous"] = "0.5.0", "0.4.0"
    up._save(st)
    up.mark_bad("0.2.0", "failed its health check")
    removed = up.prune(keep=3)
    assert "0.2.0" in removed and not (root / "versions" / "0.2.0").exists()
    assert "0.2.0" not in up.state()["versions"]
    assert up.bad("0.2.0")["reason"] == "failed its health check"          # still bad
    assert offered(up, tmp_path, release("0.2.0")).skipped_bad == "0.2.0"


def test_rollback_to_a_bad_previous_version_is_refused_with_its_reason(tmp_path):
    root = root_with(tmp_path, "0.2.0", "0.3.0")
    up = updater(root, tmp_path)
    up.switch("0.2.0")
    up.switch("0.3.0")
    up.mark_bad("0.2.0", "exited 2 s after it answered")
    with pytest.raises(RefusedError, match="exited 2 s after it answered"):
        up.rollback()
    from harness_manager.services.update.appstage import clear_bad
    from harness_manager.services.update.state import UpdateState

    assert clear_bad(UpdateState.under(tmp_path / "state"), "0.2.0")
    st = up.state()
    st["versions"]["0.2.0"]["state"] = "staged"
    up._save(st)
    assert up.rollback()["current"] == "0.2.0"                             # the twin


# --- prune never deletes a venv in use -------------------------------------------------------------


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="the /proc path of the check")
def test_prune_skips_a_venv_a_running_process_uses(tmp_path):
    root = root_with(tmp_path, "0.2.0", "0.3.0", "0.4.0", "0.5.0")
    up = updater(root, tmp_path)
    st = up.state()
    st["current"], st["previous"] = "0.5.0", "0.4.0"
    up._save(st)
    py = root / "versions" / "0.2.0" / "bin" / "python"
    py.unlink()
    py.symlink_to(sys.executable)                    # a real interpreter, argv[0] in the venv
    child = subprocess.Popen([str(py), "-c", "import time; time.sleep(60)"])
    try:
        deadline = time.monotonic() + 10
        while str(py) not in Path(f"/proc/{child.pid}/cmdline").read_bytes().decode():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        removed = up.prune(keep=2)
        assert removed == ["0.3.0"] and (root / "versions" / "0.2.0").exists()
        assert f"pid {child.pid}" in up.pruned_skipped["0.2.0"]
    finally:
        child.kill()
        child.wait(10)
    # the twin: once nothing runs from it, it goes
    assert up.prune(keep=2) == ["0.2.0"] and not (root / "versions" / "0.2.0").exists()


def test_without_proc_the_daemon_and_helper_records_decide(tmp_path):
    sd = tmp_path / "state"
    venv = tmp_path / "root" / "versions" / "0.2.0"
    su.write_json(sd / "daemon.json", {"pid": 4001, "version": "0.2.0"})
    su.write_json(su.apply_record_path(sd), {"pid": 4002, "python_version": "0.1.0"})
    alive = {4001, 4002}.__contains__
    # argv says where it runs from, when the system can tell
    why = su.venv_in_use(venv, "0.2.0", state_dir=sd, proc="/no-proc", alive=alive,
                         cmdline=lambda pid: [str(venv / "bin" / "python"), "-m", "x"]
                         if pid == 4001 else ["/usr/bin/python3"])
    assert why == "harness-manager-daemon (pid 4001) runs from it"
    # Windows cannot read argv: then the version each one runs decides
    assert "runs 0.2.0" in su.venv_in_use(venv, "0.2.0", state_dir=sd, proc="/no-proc",
                                          alive=alive, cmdline=lambda pid: None)
    # negative twins: another version, or a daemon that is gone
    assert su.venv_in_use(venv, "0.3.0", state_dir=sd, proc="/no-proc", alive=alive,
                          cmdline=lambda pid: None) == ""
    assert su.venv_in_use(venv, "0.2.0", state_dir=sd, proc="/no-proc",
                          alive=lambda pid: False, cmdline=lambda pid: None) == ""


# --- the job manager's drain -------------------------------------------------------------------------


def test_drain_refuses_new_jobs_lets_running_ones_finish_and_undrain_reopens():
    jobs = JobManager(EventBus(), BoardGates())
    release = threading.Event()
    running = jobs.submit("deploy", "b1", lambda progress: release.wait(10))
    jobs.drain(lambda: HeldError("draining", holder="x"))
    assert jobs.draining
    with pytest.raises(HeldError, match="draining"):
        jobs.submit("deploy", "b2", lambda progress: None)
    assert jobs.wait_idle(timeout=0.2) is False                 # still running
    release.set()
    assert jobs.wait_idle(timeout=10) is True and running.state == "done"
    # the twin: undrained, jobs are accepted again
    jobs.undrain()
    job = jobs.submit("deploy", "b2", lambda progress: "ok")
    assert job.finished.wait(10) and job.result == "ok"
    stop = threading.Event()
    stop.set()
    blocker = threading.Event()
    jobs.submit("deploy", "b3", lambda progress: blocker.wait(10))
    assert jobs.wait_idle(timeout=5, stop=stop) is False        # a cancel ends the wait
    blocker.set()
    jobs.shutdown()
