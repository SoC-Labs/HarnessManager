"""Team T13: daemon.json, discovery, the single-instance lock, and the CLI's engine choice.

Every check has a negative twin. The instance-lock checks use real child
processes for "another live daemon" and "a dead one", because a lock names a
pid and our own pid is always ours.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from socharness.cli import engine as cli_engine
from socharness.cli.engine import (
    ENV_NO_DAEMON,
    describe_engine,
    get_engine,
    set_engine_factory,
    wants_daemon,
)
from socharness.core.errors import HeldError
from socharness.daemon.state import (
    DaemonInfo,
    DaemonInstance,
    daemon_json_path,
    discover,
    read_info,
    remove_info,
    write_info,
)
from socharness.engine import Engine
from tests.fakes.t13_daemon import state_dir


@pytest.fixture(autouse=True)
def _real_selection(monkeypatch):
    monkeypatch.delenv(cli_engine.ENV_ENGINE, raising=False)
    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    yield
    set_engine_factory(previous)


def info(pid: int, **kw) -> DaemonInfo:
    return DaemonInfo(pid=pid, port=kw.pop("port", 1), token="tok", started_at=1.0,
                      version="0.0.1", **kw)


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@pytest.fixture
def live_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield child
    child.kill()
    child.wait()


# -- daemon.json -------------------------------------------------------------------------------


def test_daemon_json_round_trips_and_is_private(tmp_path):
    path = write_info(tmp_path, info(os.getpid(), hostname=socket.gethostname()))
    assert read_info(tmp_path) == info(os.getpid(), hostname=socket.gethostname())
    data = json.loads(path.read_text())
    assert {"pid", "port", "token", "started_at", "version"} <= set(data)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("text", ["", "{not json", '{"pid": 1}', "[]"])
def test_negative_twin_a_broken_daemon_json_names_no_daemon(tmp_path, text):
    daemon_json_path(tmp_path).write_text(text)
    assert read_info(tmp_path) is None and discover(tmp_path) is None


def test_the_ui_url_carries_the_token_in_the_fragment():
    i = info(1, port=5000)
    assert i.base_url == "http://127.0.0.1:5000" and i.ui_url == "http://127.0.0.1:5000/#token=tok"
    assert info(1, port=5, host="::1").base_url == "http://[::1]:5"


def test_discover_finds_a_live_daemon_on_this_machine(tmp_path):
    write_info(tmp_path, info(os.getpid(), hostname=socket.gethostname()))
    assert discover(tmp_path).pid == os.getpid()


@pytest.mark.parametrize("why", ["dead pid", "other machine"])
def test_negative_twin_discover_ignores_a_stale_or_foreign_daemon_json(tmp_path, why):
    pid, host = (dead_pid(), "") if why == "dead pid" else (os.getpid(), "some-other-host")
    write_info(tmp_path, info(pid, hostname=host))
    assert discover(tmp_path) is None


def test_remove_info_only_removes_its_own_file(tmp_path):
    write_info(tmp_path, info(12345))
    assert not remove_info(tmp_path, 54321) and read_info(tmp_path) is not None
    assert remove_info(tmp_path, 12345) and read_info(tmp_path) is None


# -- one daemon per state dir ------------------------------------------------------------------


def _foreign_lock(sdir: Path, pid: int) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "socharnessd.lock").write_text(json.dumps(
        {"user": "someone", "host": socket.gethostname(), "pid": pid, "since": time.time(),
         "note": "socharnessd"}))


def test_a_live_daemon_keeps_the_state_dir(tmp_path, live_child):
    _foreign_lock(tmp_path, live_child.pid)
    with pytest.raises(HeldError) as err:
        DaemonInstance(tmp_path).acquire()
    assert "already running" in err.value.message and str(live_child.pid) in err.value.holder


def test_negative_twin_a_dead_daemons_lock_is_taken_over(tmp_path):
    _foreign_lock(tmp_path, dead_pid())
    inst = DaemonInstance(tmp_path)
    inst.acquire()
    try:
        owner = json.loads((tmp_path / "socharnessd.lock").read_text())
        assert owner["pid"] == os.getpid()
    finally:
        inst.release()
    assert not (tmp_path / "socharnessd.lock").exists()


# -- the CLI's engine choice (no daemon running) -----------------------------------------------


def test_with_no_daemon_the_cli_uses_the_in_process_engine():
    assert not daemon_json_path(state_dir()).exists()
    eng = get_engine()
    try:
        assert isinstance(eng, Engine)
    finally:
        eng.close_all()
    assert describe_engine() == "socharness.engine.Engine"


def test_negative_twin_a_stale_daemon_json_is_not_used(tmp_path):
    write_info(state_dir(), info(dead_pid()))
    eng = get_engine()
    try:
        assert isinstance(eng, Engine)
    finally:
        eng.close_all()
    assert describe_engine() == "socharness.engine.Engine"


def test_a_recorded_daemon_that_does_not_answer_is_not_used():
    # A live pid (ours) but nothing on the port: health fails, so the in-process engine.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    write_info(state_dir(), info(os.getpid(), port=port))
    eng = get_engine()
    try:
        assert isinstance(eng, Engine)
    finally:
        eng.close_all()


@pytest.mark.parametrize("args, env, wanted", [
    (None, None, True),
    (argparse.Namespace(cmd="info"), None, True),
    (argparse.Namespace(cmd="program", overlay_dir=[]), None, True),
    (argparse.Namespace(cmd="attach"), None, False),
    (argparse.Namespace(cmd="detach"), None, False),
    (argparse.Namespace(cmd="daemon"), None, False),
    (argparse.Namespace(cmd="ui"), None, False),
    (argparse.Namespace(cmd="program", overlay_dir=["/x"]), None, False),
    (argparse.Namespace(cmd="info"), "1", False),
    (argparse.Namespace(cmd="info"), "0", True),
])
def test_which_verbs_may_use_the_daemon(monkeypatch, args, env, wanted):
    if env is not None:
        monkeypatch.setenv(ENV_NO_DAEMON, env)
    assert wants_daemon(args) is wanted
