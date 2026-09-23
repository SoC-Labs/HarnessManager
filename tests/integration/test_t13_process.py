"""Team T13: real ``harness-manager-daemon`` processes. Single instance, stale takeover, and the CLI verbs.

These spawn ``python -m harness_manager.daemon`` (directly, or detached through
``harness-manager daemon start`` / ``harness-manager ui``), always on 127.0.0.1 and an
ephemeral port, and always stop them. ``webbrowser.open`` is replaced, so no
browser ever starts. Every check has a negative twin.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from harness_manager.cli.engine import ENV_NO_DAEMON, set_engine_factory
from harness_manager.core.errors import ExitCode
from harness_manager.daemon.state import daemon_json_path, read_info
from tests.fakes.t13_daemon import (
    kill,
    pack_overrides,
    run_cli,
    spawn,
    state_dir,
    stop_state_dir,
    wait_info,
)
from tests.fakes.virtual_board import VirtualMps3

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch):
    from harness_manager.cli import engine as cli_engine

    monkeypatch.delenv(cli_engine.ENV_ENGINE, raising=False)
    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    yield
    set_engine_factory(previous)
    stop_state_dir(state_dir())


@pytest.fixture
def no_browser(monkeypatch) -> list[str]:
    """``webbrowser.open`` replaced by a recorder, on a machine that has a display."""
    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("BROWSER", raising=False)
    return opened


def token_of(url: str) -> str:
    fragment = urlsplit(url).fragment
    assert fragment.startswith("token=")
    return fragment[len("token="):]


def get(url: str, path: str, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    with httpx.Client(trust_env=False, timeout=10) as c:
        return c.get(url.split("#")[0].rstrip("/") + path, headers=headers)


# -- `harness-manager ui` ------------------------------------------------------------------------------------


def test_ui_no_browser_prints_a_url_whose_token_works(capsys, no_browser):
    rc, out, err = run_cli(capsys, "--json", "ui", "--no-browser")
    assert rc == 0, err
    shown = json.loads(out)
    url = shown["url"]
    assert shown["started"] is True and url.startswith("http://127.0.0.1:") and "#token=" in url
    assert no_browser == []                                 # --no-browser: nothing opened
    token = token_of(url)
    assert get(url, "/api/v1/packs", token).json()["packs"]["mps3"]
    # negative twins: no token, a wrong token
    assert get(url, "/api/v1/packs", None).status_code == 401
    assert get(url, "/api/v1/packs", token[::-1]).status_code == 401
    # the page itself is served (the placeholder until T14's UI is installed)
    assert get(url, "/", None).status_code == 200
    # daemon.json is private and names the same process, port and token
    info = read_info(state_dir())
    assert (info.pid, info.port, info.token) == (shown["pid"], shown["port"], token)
    if os.name != "nt":
        assert daemon_json_path(state_dir()).stat().st_mode & 0o777 == 0o600
    # the daemon's log never shows the token, even for a WebSocket URL that carried it
    from tests.fakes.t13_daemon import ws_connect

    ws_connect(f"ws://127.0.0.1:{info.port}/api/v1/events?token={token}").close()
    time.sleep(0.2)
    log = (state_dir() / "daemon.log").read_text()
    assert "harness-manager daemon start" in log and token not in log
    # a second `ui` reuses the running daemon and opens the browser this time
    rc, out, _ = run_cli(capsys, "--json", "ui")
    again = json.loads(out)
    assert rc == 0 and again["started"] is False and again["url"] == url
    assert no_browser == [url] and again["browser"] is True


def test_negative_twin_with_no_display_ui_prints_the_url_and_opens_nothing(capsys, no_browser,
                                                                          monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if sys.platform in ("win32", "darwin"):
        pytest.skip("Windows and macOS always have a browser to hand the URL to")
    rc, out, err = run_cli(capsys, "ui")
    assert rc == 0 and out.startswith("http://127.0.0.1:") and "#token=" in out
    assert no_browser == [] and "no display here" in err and "ssh -L" in err


def test_ui_on_a_different_port_than_the_running_daemon_is_refused(capsys, no_browser):
    rc, out, _ = run_cli(capsys, "--json", "ui", "--no-browser")
    port = json.loads(out)["port"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        other = s.getsockname()[1]
    rc, out, _ = run_cli(capsys, "--json", "ui", "--no-browser", "--port", str(other))
    err = json.loads(out)["error"]
    assert rc == ExitCode.USAGE and str(port) in err["message"]


# -- `harness-manager daemon start|stop|status` --------------------------------------------------------------


def test_daemon_start_status_stop(capsys):
    rc, out, _ = run_cli(capsys, "--json", "daemon", "status")
    assert rc == 0 and json.loads(out)["state"] == "stopped"
    rc, out, err = run_cli(capsys, "--json", "daemon", "start")
    started = json.loads(out)
    assert rc == 0, err
    assert started["state"] == "running"
    rc, out, _ = run_cli(capsys, "--tsv", "daemon", "status")
    cols = out.rstrip("\n").split("\t")
    assert rc == 0 and cols[0] == "running" and cols[1] == str(started["pid"])
    # starting twice is ALREADY (8)
    rc, out, _ = run_cli(capsys, "--json", "daemon", "start")
    assert rc == ExitCode.ALREADY and str(started["pid"]) in json.loads(out)["error"]["message"]
    rc, out, _ = run_cli(capsys, "--json", "daemon", "stop")
    assert rc == 0 and json.loads(out)["state"] == "stopped"
    assert not daemon_json_path(state_dir()).exists()
    # stopping twice is ALREADY too
    rc, out, _ = run_cli(capsys, "--json", "daemon", "stop")
    assert rc == ExitCode.ALREADY
    rc, out, _ = run_cli(capsys, "--json", "daemon", "status")
    assert json.loads(out)["state"] == "stopped"


def test_a_daemon_that_crashed_leaves_nothing_that_blocks_the_next(capsys):
    proc = spawn(state_dir())
    info = wait_info(state_dir(), proc.pid)
    proc.kill()                                  # no clean shutdown: files left behind
    proc.wait()
    assert read_info(state_dir()).pid == info.pid
    rc, out, _ = run_cli(capsys, "--json", "daemon", "status")
    assert json.loads(out)["state"] == "stale"
    # the stale lock and daemon.json are taken over by the next daemon
    rc, out, err = run_cli(capsys, "--json", "daemon", "start")
    assert rc == 0, err
    assert json.loads(out)["pid"] != info.pid and read_info(state_dir()).pid != info.pid


# -- one daemon per state dir ------------------------------------------------------------------------------


def test_a_second_daemon_on_the_same_state_dir_refuses_to_start(tmp_path):
    first = spawn(state_dir())
    try:
        info = wait_info(state_dir(), first.pid)
        second = spawn(state_dir())
        out, _ = second.communicate(timeout=60)
        assert second.returncode == ExitCode.HELD
        assert b"already running" in out and str(info.pid).encode() in out
        assert read_info(state_dir()).pid == first.pid          # the first one is untouched
        # negative twin: another state dir is independent
        other = spawn(tmp_path / "other-state")
        try:
            assert wait_info(tmp_path / "other-state", other.pid).pid == other.pid
        finally:
            kill(other)
    finally:
        kill(first)
    # a clean stop removes daemon.json and releases the lock
    assert not daemon_json_path(state_dir()).exists()
    assert not (state_dir() / "harness-manager-daemon.lock").exists()


def test_a_taken_port_is_port_bound(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        proc = subprocess.run([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                               str(state_dir()), "--port", str(port)],
                              capture_output=True, timeout=60)
    assert proc.returncode == ExitCode.PORT_BOUND and b"already in use" in proc.stdout + proc.stderr
    assert not daemon_json_path(state_dir()).exists()


# -- the whole path: CLI -> daemon process -> virtual board ------------------------------------------------


def test_the_cli_reaches_a_board_through_a_daemon_process(tmp_path, capsys):
    with VirtualMps3(tmp_path / "vb") as vb:
        proc = spawn(state_dir(), "--pack-overrides", json.dumps(pack_overrides(vb)))
        try:
            wait_info(state_dir(), proc.pid)
            rc, out, err = run_cli(capsys, "--json", "info", vb.shell_endpoint)
            assert rc == 0, err
            assert json.loads(out)["identity"]["shell_id"] == "0x3f1a560f"
            rc, out, _ = run_cli(capsys, "--json", "console", vb.shell_endpoint, "uart0",
                                 "--for", "1")
            assert rc == 0 and "nanosoc boot" in json.loads(out)["text"]
            # negative twin: a board the daemon cannot reach is UNREACHABLE, as in-process
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                dead = f"127.0.0.1:{s.getsockname()[1]}"
            rc, out, _ = run_cli(capsys, "--json", "info", dead)
            assert rc == ExitCode.UNREACHABLE
        finally:
            kill(proc)


def test_bad_pack_overrides_stop_the_daemon_at_start(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                           str(state_dir()), "--pack-overrides", '{"mps3": {"nope": 1}}'],
                          capture_output=True, timeout=60)
    assert proc.returncode == ExitCode.USAGE and not daemon_json_path(state_dir()).exists()
    assert not Path(state_dir() / "harness-manager-daemon.lock").exists()
