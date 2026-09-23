"""T7: app self-update: side-by-side venvs via (a fake) uv, a switch pointer, rollback, busy refusal.

No real ``uv`` runs and nothing is installed: ``FakeUv`` records each argv and
imitates the three steps. The end-to-end test downloads a signed wheel from the
fake channel server (127.0.0.1 only).
"""

from __future__ import annotations

import json
import os

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.session import SessionLock
from harness_manager.services.update.app import (
    AppLayout,
    AppUpdater,
    LocalBusyProbe,
    python_satisfies,
)
from harness_manager.services.update.schema import AppRelease, Asset
from harness_manager.services.update.service import UpdateService
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_board import FakeUv

KEYS = TestKeys()
SHA = "cd" * 32


def release(version: str, *, name: str | None = None, requires_python: str = "") -> AppRelease:
    wheel = Asset(name=name or f"harness_manager-{version}-py3-none-any.whl",
                  url=f"https://x/harness_manager-{version}-py3-none-any.whl", sha256=SHA, size=10)
    return AppRelease(version=version, status="current", wheel=wheel,
                      requires_python=requires_python)


@pytest.fixture
def env(tmp_path):
    state_dir = tmp_path / "state"
    uv = FakeUv()
    extra: list[str] = []
    busy = LocalBusyProbe(state_dir, extra=(lambda: list(extra),))
    up = AppUpdater(AppLayout(state_dir / "update" / "app"), busy, uv="/opt/uv", runner=uv,
                    python_version="3.11", running_version="0.1.0", windows=False)
    wheel = tmp_path / "harness_manager-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    return {"up": up, "uv": uv, "state_dir": state_dir, "extra": extra, "wheel": wheel,
            "tmp": tmp_path}


def stage(env, version="0.2.0", **kw):
    return env["up"].stage(release(version, **kw), env["wheel"])


def test_stage_builds_a_side_by_side_venv_with_hash_pinned_requirements(env):
    up, uv = env["up"], env["uv"]
    info = stage(env)
    assert info["state"] == "staged"
    venv = up.layout.venv("0.2.0")
    py = str(venv / "bin" / "python")
    assert uv.calls[0] == ["/opt/uv", "venv", "--python", "3.11", str(venv)]
    assert uv.calls[1][:5] == ["/opt/uv", "pip", "install", "--python", py]
    assert "--require-hashes" in uv.calls[1]
    assert uv.calls[2][0] == py and uv.calls[2][1] == "-c"
    assert f"--hash=sha256:{SHA}" in uv.reqs_seen[0] and "harness-manager @ file://" in uv.reqs_seen[0]
    # pip needs the PEP 427 name, not the cache's hash name
    assert "/wheels/harness_manager-0.2.0-py3-none-any.whl --hash" in uv.reqs_seen[0]
    assert up.state()["current"] == ""                                # staging never switches


def test_a_lock_file_is_carried_into_the_requirements(env):
    lock = env["tmp"] / "lock.txt"
    lock.write_text("pyserial==3.5 --hash=sha256:" + "ef" * 32 + "\n")
    env["up"].stage(release("0.2.0"), env["wheel"], lock)
    assert "pyserial==3.5" in env["uv"].reqs_seen[0]


def test_switch_then_rollback(env):
    up = env["up"]
    stage(env, "0.2.0")
    env["wheel"] = env["wheel"].with_name("harness_manager-0.3.0-py3-none-any.whl")
    env["wheel"].write_bytes(b"wheel3")
    stage(env, "0.3.0")
    up.switch("0.2.0")
    st = up.switch("0.3.0")
    assert (st["current"], st["previous"]) == ("0.3.0", "0.2.0")
    pointer = json.loads(up.layout.pointer.read_text())
    assert pointer["current"] == "0.3.0"
    st = up.rollback()
    assert (st["current"], st["previous"]) == ("0.2.0", "0.3.0")
    assert up.layout.venv("0.3.0").is_dir()                          # kept for roll-forward


def test_switch_is_refused_while_a_live_process_holds_a_board(env):
    up = env["up"]
    stage(env)
    lock = SessionLock("mps3@192.168.10.101:6900", lock_dir=env["state_dir"] / "locks")
    lock.acquire()
    try:
        with pytest.raises(HeldError, match="cannot switch"):
            up.switch("0.2.0")
        assert up.state()["current"] == ""
    finally:
        lock.release()
    assert up.switch("0.2.0")["current"] == "0.2.0"                  # twin: free again


def test_a_stale_lock_does_not_block(env):
    locks = env["state_dir"] / "locks"
    locks.mkdir(parents=True)
    import socket

    (locks / "mps3_x.lock").write_text(json.dumps({"user": "u", "host": socket.gethostname(),
                                                   "pid": 2 ** 22 + 12345, "since": 0.0}))
    stage(env)
    assert env["up"].switch("0.2.0")["current"] == "0.2.0"


def test_switch_is_refused_while_a_harness_update_is_unfinished(env):
    stage(env)
    journal = env["state_dir"] / "update" / "journal"
    journal.mkdir(parents=True)
    (journal / "mps3_board.json").write_text("{}")
    with pytest.raises(HeldError, match="unfinished"):
        env["up"].switch("0.2.0")


def test_switch_and_rollback_are_refused_while_a_job_or_lease_is_active(env):
    up = env["up"]
    stage(env, "0.2.0")
    env["wheel"] = env["wheel"].with_name("harness_manager-0.3.0-py3-none-any.whl")
    env["wheel"].write_bytes(b"wheel3")
    stage(env, "0.3.0")
    up.switch("0.2.0")
    env["extra"].append("harness-manager-daemon job 7 (deploy nanosoc) is running")
    with pytest.raises(HeldError, match="job 7"):
        up.switch("0.3.0")
    up.__dict__["busy"] = LocalBusyProbe(env["state_dir"])       # job done
    up.switch("0.3.0")
    up.__dict__["busy"] = LocalBusyProbe(env["state_dir"], extra=(lambda: ["hub lease held"],))
    with pytest.raises(HeldError, match="hub lease"):
        up.rollback()
    assert up.state()["current"] == "0.3.0"


def test_a_failed_install_leaves_the_running_version_alone(env):
    env["uv"].fail_install = True
    with pytest.raises(ActionFailedError, match="hash mismatch"):
        stage(env)
    up = env["up"]
    assert up.state()["versions"]["0.2.0"]["state"] == "failed"
    assert not up.layout.venv("0.2.0").exists() and up.state()["current"] == ""
    with pytest.raises(RefusedError, match="not staged"):
        up.switch("0.2.0")


def test_a_venv_reporting_another_version_is_refused(env):
    env["uv"].report_version = "0.1.9"
    with pytest.raises(ActionFailedError, match="reports version 0.1.9"):
        stage(env)


def test_a_wheel_with_another_version_is_refused_before_uv_runs(env):
    with pytest.raises(IncompatibleError, match="version 0.9.9"):
        stage(env, name="harness_manager-0.9.9-py3-none-any.whl")
    assert env["uv"].calls == []


def test_a_wheel_of_another_package_is_refused(env):
    with pytest.raises(IncompatibleError, match="not harness-manager"):
        stage(env, name="evil-0.2.0-py3-none-any.whl")


def test_requires_python(env):
    with pytest.raises(IncompatibleError, match="needs Python"):
        stage(env, requires_python=">=3.12")
    assert python_satisfies(">=3.10,<4", (3, 11)) and not python_satisfies(">=3.12", (3, 11))


def test_no_uv_is_unavailable(env, monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_UV", raising=False)
    monkeypatch.setenv("PATH", str(env["tmp"]))
    up = AppUpdater(env["up"].layout, env["up"].busy, runner=env["uv"])
    with pytest.raises(UnavailableError, match="uv"):
        up.stage(release("0.2.0"), env["wheel"])


def test_switch_edge_cases(env):
    up = env["up"]
    with pytest.raises(RefusedError, match="no previous"):
        up.rollback()
    stage(env)
    up.switch("0.2.0")
    with pytest.raises(AlreadyError):
        up.switch("0.2.0")


def test_windows_venv_python_path(env):
    layout = env["up"].layout
    assert layout.python("0.2.0", windows=True).as_posix().endswith("Scripts/python.exe")
    assert layout.python("0.2.0", windows=False).as_posix().endswith("bin/python")


def test_prune_keeps_current_and_previous(env):
    up = env["up"]
    for v in ("0.2.0", "0.3.0", "0.4.0", "0.5.0"):
        wheel = env["tmp"] / f"harness_manager-{v}-py3-none-any.whl"
        wheel.write_bytes(v.encode())
        up.stage(release(v), wheel)
    up.switch("0.4.0")
    up.switch("0.5.0")
    removed = up.prune(keep=3)
    assert set(up.state()["versions"]) >= {"0.4.0", "0.5.0"}
    assert removed and "0.4.0" not in removed and "0.5.0" not in removed


# --- end to end through UpdateService, against the fake channel ------------------------


@pytest.fixture
def channel_app(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        b = ChannelBuilder(srv.root, KEYS)
        b.add_app("0.2.0", AssetFile("harness_manager-0.2.0-py3-none-any.whl", b"PK-fake-wheel"),
                  lock=AssetFile("harness_manager-0.2.0-requirements.lock",
                                 b"fastapi==0.110 --hash=sha256:" + b"ab" * 32 + b"\n"),
                  requires_python=">=3.10")
        yield srv, b


def service(tmp_path, uv: FakeUv) -> UpdateService:
    state_dir = tmp_path / "state"
    app = AppUpdater(AppLayout(state_dir / "update" / "app"), LocalBusyProbe(state_dir),
                     uv="/opt/uv", runner=uv, python_version="3.11", running_version="0.1.0",
                     windows=os.name == "nt")
    return UpdateService(state_dir=state_dir, trust=KEYS.trust(), app_version="0.1.0",
                         app_updater=app, token="")


def test_update_app_downloads_verifies_stages_and_switches(channel_app, tmp_path):
    srv, b = channel_app
    b.publish(serial=1)
    uv = FakeUv()
    out = service(tmp_path, uv).update_app(source=srv.source())
    assert out["switched"] and out["version"] == "0.2.0" and out["locked"]
    assert out["pointer"]["current"] == "0.2.0"
    assert "fastapi==0.110" in uv.reqs_seen[0]


def test_update_app_refuses_a_tampered_wheel_before_building_anything(channel_app, tmp_path):
    srv, b = channel_app
    b.publish(serial=1)
    (srv.root / "assets" / "harness_manager-0.2.0-py3-none-any.whl").write_bytes(b"PK-evil-wheel")
    uv = FakeUv()
    with pytest.raises(RefusedError, match="sha256"):
        service(tmp_path, uv).update_app(source=srv.source())
    assert uv.calls == []


def test_update_app_when_current_is_already_running(channel_app, tmp_path):
    srv, b = channel_app
    b.publish(serial=1)
    svc = service(tmp_path, FakeUv())
    svc.app_version = "0.2.0"
    with pytest.raises(AlreadyError):
        svc.update_app(source=srv.source())


def test_check_reports_an_app_update_without_installing(channel_app, tmp_path):
    srv, b = channel_app
    b.publish(serial=1)
    uv = FakeUv()
    report = service(tmp_path, uv).check(source=srv.source())
    assert report["app_update"] == "0.2.0" and report["available"] and uv.calls == []
