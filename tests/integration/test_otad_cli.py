"""Lane OTA-D: ``harness-manager update app --apply`` and ``harness-manager update status``.

``--apply`` talks to a daemon: here the real app under a real uvicorn in a thread
(``LiveDaemon``, which writes ``daemon.json`` so the CLI finds it), over an engine whose
update service stages with a ``FakeUv`` from ``FakeChannelServer`` (the ``hm-app``
catalogue, the test keys). The apply's helper is a recorder that writes the verdict the
real helper would (``last_apply.json``); the restart itself is ``test_otad_restart_process``.
Every behaviour has a negative twin.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from harness_manager import __version__, _launch
from harness_manager.cli import main as climain
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.daemon import update_apply as ua
from harness_manager.services.update import UpdateService
from harness_manager.services.update import selfupdate as su
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.policy import Policy
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_board import FakeUv
from tests.fakes.t13_daemon import LiveDaemon, state_dir

KEYS = TestKeys()


def install_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("installer python")
    _launch.register(root, root / "venv", __version__, extras=[], windows=False)
    return root


def run(capsys, monkeypatch, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = climain.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def rig(tmp_path: Path, monkeypatch):
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine

    sd = state_dir()
    root = install_root(tmp_path)
    eng = Engine(EngineConfig(state_dir=sd), packs={})
    app = AppUpdater(AppLayout(root), LocalBusyProbe(sd), uv="/opt/uv", runner=FakeUv(),
                     python_version="3.11", running_version=__version__, windows=False,
                     state_dir=sd)
    svc = UpdateService(eng, state_dir=sd, trust=KEYS.trust(), token="", bus=eng.bus,
                        app_version=__version__, app_updater=app, policy=Policy())
    eng._services["update"] = svc
    with FakeChannelServer(tmp_path / "www") as srv:
        builder = ChannelBuilder(srv.root, KEYS)
        builder.add_app("1.1.0", AssetFile("harness_manager-1.1.0-py3-none-any.whl", b"PK-w"))
        builder.publish(serial=1)
        monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", srv.source())
        yield {"eng": eng, "svc": svc, "app": app, "root": root, "srv": srv, "sd": sd}
    eng.close_all()


def with_daemon(r: dict, verdict: str, reason: str = ""):
    """The daemon, with the helper replaced by one that writes ``verdict`` for the apply."""
    live = LiveDaemon(r["eng"], static_dir=None)
    d = live.app.state.daemon
    d.runtime = {"port": live.port, "listen": "127.0.0.1", "log_level": "info",
                 "pack_overrides": {}, "demo": False}
    ap = d.update_applier
    ap.prefix = str(r["root"] / "venv")
    ap.self_test = lambda py: ""
    ap.grace_s = 0.0

    def helper(argv: list[str], env: dict, log: Path, cwd: Path) -> Any:
        a = ua._parser().parse_args(argv[3:])
        rec = {"id": a.id, "from": a.from_version, "to": a.to, "result": verdict,
               "phase": "start" if verdict != "applied" else "", "reason": reason,
               "seconds": 3.2}
        threading.Timer(0.3, su.write_json, args=(su.last_apply_path(r["sd"]), rec)).start()
        return type("Proc", (), {"pid": 1})()

    ap.spawn = helper
    return live


def test_update_app_apply_stages_through_the_daemon_and_reports_the_restart(rig, capsys,
                                                                           monkeypatch):
    with with_daemon(rig, "applied"):
        rc, out, err = run(capsys, monkeypatch, "--json", "update", "app", "--apply", "--yes")
    assert rc == ExitCode.OK, err
    body = json.loads(out)
    assert (body["version"], body["result"], body["apply"]["to"]) == ("1.1.0", "applied", "1.1.0")
    assert rig["app"].state()["versions"]["1.1.0"]["state"] == "staged"       # staged by it
    assert "update: applying 1.1.0" in err


def test_negative_twin_a_rolled_back_apply_fails_with_the_reason(rig, capsys, monkeypatch):
    with with_daemon(rig, "rolled-back", "the daemon exited with code 1 while starting"):
        rc, out, err = run(capsys, monkeypatch, "--json", "update", "app", "--apply", "--yes")
    assert rc == ExitCode.ACTION_FAILED
    err_obj = json.loads(out)["error"]
    assert "did not come up" in err_obj["message"] and "exited with code 1" in err_obj["message"]
    assert err_obj["data"]["apply"]["result"] == "rolled-back"


def test_update_app_apply_tsv_has_the_documented_columns(rig, capsys, monkeypatch):
    with with_daemon(rig, "applied"):
        rc, out, err = run(capsys, monkeypatch, "--tsv", "update", "app", "--apply", "--yes")
    assert rc == ExitCode.OK, err
    row = out.rstrip("\n").split("\t")
    assert len(row) == len(TSV_COLUMNS["update app"]) and row[-1] == "applied"


def test_update_status_reads_the_files_with_no_daemon(rig, capsys, monkeypatch, tmp_path):
    from harness_manager.cli.engine import set_engine_factory

    previous = set_engine_factory(lambda _args: rig["eng"])
    try:
        rig["app"].stage(*_release(tmp_path, "1.2.0"))
        rig["app"].mark_bad("1.1.1", "exited 2 s after it answered", phase="stable")
        su.write_json(su.last_apply_path(rig["sd"]), {"id": "x", "from": __version__,
                                                      "to": "1.1.1", "result": "rolled-back",
                                                      "reason": "exited", "at": 1.0})
        rc, out, err = run(capsys, monkeypatch, "--json", "update", "status")
        assert rc == 0, err
        body = json.loads(out)
        assert body["staged"] == ["1.2.0"] and "1.1.1" in body["bad"]
        assert body["effective"]["auto"] == "stage" and body["daemon"]["state"] == "stopped"
        assert body["last_apply"]["result"] == "rolled-back"
        rc, out, _ = run(capsys, monkeypatch, "update", "status")
        assert "bad        1.1.1: exited 2 s after it answered" in out
        assert "last apply" in out and "rolled-back" in out
        rc, out, _ = run(capsys, monkeypatch, "--tsv", "update", "status")
        assert len(out.rstrip("\n").split("\t")) == len(TSV_COLUMNS["update status"])
    finally:
        set_engine_factory(previous)


def test_negative_twin_apply_with_no_daemon_stages_and_switches_here(rig, capsys, monkeypatch):
    from harness_manager.cli.engine import set_engine_factory

    previous = set_engine_factory(lambda _args: rig["eng"])
    try:
        rc, out, err = run(capsys, monkeypatch, "--json", "update", "app", "--apply", "--yes")
    finally:
        set_engine_factory(previous)
    assert rc == ExitCode.OK, err
    assert "nothing to restart" in err
    body = json.loads(out)
    assert body["switched"] is True and rig["app"].state()["current"] == "1.1.0"


def _release(tmp_path: Path, version: str):
    from harness_manager.services.update.schema import AppRelease, Asset

    wheel = tmp_path / f"w-{version}.whl"
    wheel.write_bytes(b"PK")
    rel = AppRelease(version=version, status="current",
                     wheel=Asset(name=f"harness_manager-{version}-py3-none-any.whl",
                                 url="https://x/w.whl", sha256="ab" * 32, size=2))
    return rel, wheel
