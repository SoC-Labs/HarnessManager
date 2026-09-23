"""Team T13: harness-manager-daemon under a REAL uvicorn on 127.0.0.1, with the CLI going through it.

``LiveDaemon`` runs the app in a thread and writes ``daemon.json``, so
``cli.engine.get_engine()`` finds it and every CLI verb runs on a
``RemoteEngine``. The negative twins run the same verb on the in-process engine
(``HARNESS_MANAGER_NO_DAEMON=1``). Only 127.0.0.1 is reached.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from harness_manager.cli.engine import (
    ENV_NO_DAEMON,
    describe_engine,
    get_engine,
    set_engine_factory,
)
from harness_manager.client import RemoteEngine
from harness_manager.core.errors import ExitCode
from harness_manager.core.events import Event
from harness_manager.core.services import Engine as EngineProtocol
from tests.fakes.t2_overlays import OTHER_STATIC_ID, SYNTH2_RM_ID, make_overlay, use_overlay_dirs
from tests.fakes.t4_console_rig import SingleClientProxy
from tests.fakes.t13_daemon import (
    LiveDaemon,
    engine_for,
    recv_json_until,
    recv_until,
    run_cli,
    state_dir,
    ws_connect,
)
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture(autouse=True)
def _real_engine_selection(monkeypatch):
    from harness_manager.cli import engine as cli_engine

    monkeypatch.delenv(cli_engine.ENV_ENGINE, raising=False)
    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    yield
    set_engine_factory(previous)


@pytest.fixture
def overlays(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "ov"
    make_overlay(root, "synth")
    make_overlay(root, "alien", rm_id=SYNTH2_RM_ID, static_id=OTHER_STATIC_ID)
    use_overlay_dirs(monkeypatch, root)
    return root


@pytest.fixture
def daemon(vboard: VirtualMps3):
    eng = engine_for(vboard)
    try:
        with LiveDaemon(eng) as d:
            yield d
    finally:
        eng.close_all()


def in_process(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    with monkeypatch.context() as m:
        m.setenv(ENV_NO_DAEMON, "1")
        return run_cli(capsys, *argv)


# -- engine selection ---------------------------------------------------------------------------------


def test_a_running_daemon_is_the_clis_engine(daemon):
    eng = get_engine()
    try:
        assert isinstance(eng, RemoteEngine) and isinstance(eng, EngineProtocol)
        assert eng.base_url == daemon.base_url
    finally:
        eng.close_all()
    assert describe_engine().startswith(f"harness-manager-daemon at {daemon.base_url}")


def test_negative_twin_no_daemon_env_keeps_the_cli_in_process(daemon, monkeypatch):
    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    eng = get_engine()
    try:
        assert not isinstance(eng, RemoteEngine)
    finally:
        eng.close_all()
    assert describe_engine() == "harness_manager.engine.Engine"


# -- CLI verbs through the daemon print what they print in-process -------------------------------------


def test_info_through_the_daemon_matches_in_process(daemon, vboard, capsys, monkeypatch):
    rc, out, _ = run_cli(capsys, "--json", "info", vboard.shell_endpoint)
    assert rc == 0
    remote = json.loads(out)
    # the CLI opened the board in the daemon, so it closed it there too
    with daemon.client() as c:
        assert c.get("/api/v1/boards").json()["boards"][0]["open"] is False
    rc, out, _ = in_process(monkeypatch, capsys, "--json", "info", vboard.shell_endpoint)
    local = json.loads(out)
    assert rc == 0
    for key in ("ok", "candidate", "identity", "capabilities", "unavailable"):
        assert remote[key] == local[key], key


def test_program_through_the_daemon_streams_progress_and_verifies(daemon, vboard, capsys,
                                                                 overlays):
    rc, out, err = run_cli(capsys, "--json", "program", vboard.shell_endpoint, "synth", "--yes")
    assert rc == 0, err
    result = json.loads(out)
    assert result["result"]["verified"] is True and result["result"]["rm_id"] == "0x01007a57"
    assert "preflight:" in err and "deploy: push" in err and "deploy: done" in err
    assert vboard.shell.current_rm_id == 0x01007A57


def test_negative_twin_a_wrong_shell_overlay_is_refused_the_same_way(daemon, vboard, capsys,
                                                                    overlays, monkeypatch):
    rc, out, _ = run_cli(capsys, "--json", "program", vboard.shell_endpoint, "alien", "--yes")
    remote = json.loads(out)
    assert rc == ExitCode.INCOMPATIBLE and vboard.shell.accepted_pushes == []
    rc_local, out_local, _ = in_process(monkeypatch, capsys, "--json", "program",
                                        vboard.shell_endpoint, "alien", "--yes")
    assert rc_local == rc and json.loads(out_local) == remote


def test_reset_clock_lab_and_console_through_the_daemon(daemon, vboard, capsys, monkeypatch):
    ep = vboard.shell_endpoint
    for argv in (("reset", ep), ("clock", ep), ("lab", ep, "link", "pulse"),
                 ("lab", ep, "display", "query"), ("lab", ep, "macgen"),
                 ("lab", ep, "dutrx", "--frames", "2"), ("overlays", ep),
                 ("debug", "status", ep), ("telemetry", ep)):
        rc, out, err = run_cli(capsys, "--json", *argv)
        rc_local, out_local, _ = in_process(monkeypatch, capsys, "--json", *argv)
        assert rc == rc_local, (argv, err)
        remote, local = json.loads(out), json.loads(out_local)
        if argv[0] == "telemetry":        # readings carry their own age
            for r in remote["readings"] + local["readings"]:
                r.pop("age_s"), r.pop("observed_at")
        if argv[0] == "clock":
            for r in remote["readings"] + local["readings"]:
                r.pop("age_s"), r.pop("observed_at")
        if argv[:3] == ("lab", ep, "macgen"):   # the generator's counters keep counting
            for d in (remote, local):
                d.pop("tx"), d.pop("rx"), d.pop("err")
        assert remote == local, argv
    rc, out, _ = run_cli(capsys, "--json", "console", ep, "uart0", "--for", "1")
    assert rc == 0 and "nanosoc boot" in json.loads(out)["text"]


def test_negative_twin_a_missing_adapter_is_unavailable_with_the_same_reason(daemon, vboard,
                                                                          capsys, monkeypatch):
    rc, out, _ = run_cli(capsys, "--json", "mcc", vboard.shell_endpoint, "temp")
    rc_local, out_local, _ = in_process(monkeypatch, capsys, "--json", "mcc",
                                        vboard.shell_endpoint, "temp")
    assert rc == rc_local == ExitCode.UNAVAILABLE
    assert json.loads(out) == json.loads(out_local)


def test_probe_through_the_daemon(daemon, vboard, capsys):
    rc, out, _ = run_cli(capsys, "--json", "probe", "--host", vboard.shell_endpoint, "--no-scan")
    (cand,) = json.loads(out)["candidates"]
    assert rc == 0 and cand["identity"]["rm_name"] == "greybox"


# -- one board session, two clients ---------------------------------------------------------------------


def test_the_cli_and_a_websocket_client_share_one_board_session(vboard, capsys, monkeypatch):
    proxy = SingleClientProxy(vboard.console_ports["uart0"])
    eng = engine_for(vboard, console_ports=dict(vboard.console_ports, uart0=proxy.port))
    try:
        with LiveDaemon(eng) as d, d.client() as web:
            # the "web UI" opens the board and watches uart0
            bid = web.post("/api/v1/boards", json={"target": vboard.shell_endpoint,
                                                   "note": "web ui"}).json()["board_id"]
            ui = ws_connect(d.ws_url(f"/boards/{bid.replace('@', '%40')}/consoles/uart0"))
            try:
                recv_until(ui, b"nanosoc boot\n")
                # the CLI, through the same daemon, while the UI holds the board
                rc, out, err = run_cli(capsys, "--json", "info", vboard.shell_endpoint)
                assert rc == 0, err
                assert json.loads(out)["identity"]["rm_name"] == "greybox"
                # a RemoteEngine client shares the session instead of fighting for it
                remote = RemoteEngine.discover()
                try:
                    session = remote.open(remote.candidate_for(vboard.shell_endpoint))
                    assert session.owned is False
                    stream = remote.consoles.subscribe(session, "uart0")
                    ui.send(b"ping\n")
                    got = b""
                    deadline = time.monotonic() + 10
                    while b"ping\n" not in got and time.monotonic() < deadline:
                        got += stream.read(timeout=0.5)
                    assert b"ping\n" in got                       # the CLI side saw it
                    assert b"ping\n" in recv_until(ui, b"ping\n")  # and so did the UI
                finally:
                    remote.close_all()
                # ONE lock, held ONCE, by the daemon; the board saw ONE console connection
                locks = list((state_dir() / "locks").glob("*.lock"))
                assert len(locks) == 1
                owner = json.loads(locks[0].read_text())
                assert owner["pid"] == os.getpid() and owner["note"] == "harness-manager-daemon: web ui"
                assert (proxy.accepted, proxy.refused) == (1, 0)
                # the CLI's clients left; the UI's session is still open and live
                boards = web.get("/api/v1/boards").json()["boards"]
                assert [b["open"] for b in boards] == [True]
                ui.send(b"again\n")
                recv_until(ui, b"again\n")
                # negative twin: the in-process engine cannot take the board the daemon holds
                rc, out, err = in_process(monkeypatch, capsys, "--json", "info",
                                          vboard.shell_endpoint)
                assert rc == ExitCode.HELD and "harness-manager-daemon: web ui" in json.loads(out)[
                    "error"]["holder"]
            finally:
                ui.close()
    finally:
        eng.close_all()
        proxy.close()


# -- back-pressure: drop the oldest, report it, never block the engine ----------------------------------


def _stalled_socket(port: int) -> socket.socket:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", port))
    return sock


def test_a_stalled_events_client_loses_the_oldest_events_and_is_told(vboard):
    eng = engine_for(vboard)
    try:
        with LiveDaemon(eng, write_json=False, event_limits=(50, 1 << 20)) as d:
            ws = ws_connect(d.ws_url("/events", topics="t13.*"),
                            sock=_stalled_socket(d.port), max_queue=1)
            try:
                n = 20_000
                pad = "x" * 1000

                def flood() -> None:
                    for i in range(n):
                        eng.bus.publish(Event("t13.flood", "", {"seq": i, "pad": pad}))

                publisher = threading.Thread(target=flood)
                started = time.monotonic()
                publisher.start()
                publisher.join(timeout=60)
                assert not publisher.is_alive(), "publishing blocked on a stalled client"
                elapsed = time.monotonic() - started
                frames = recv_json_until(ws, lambda f: f["data"].get("seq") == n - 1, timeout=60)
            finally:
                ws.close()
    finally:
        eng.close_all()
    dropped = [f for f in frames if f["topic"] == "events.dropped"]
    seqs = [f["data"]["seq"] for f in frames if f["topic"] == "t13.flood"]
    assert dropped and sum(f["data"]["dropped"] for f in dropped) + len(seqs) == n
    assert seqs == sorted(seqs) and seqs[-1] == n - 1       # the newest survive, in order
    assert elapsed < 60


def test_negative_twin_a_reading_events_client_loses_nothing(vboard):
    eng = engine_for(vboard)
    try:
        with LiveDaemon(eng, write_json=False) as d:
            ws = ws_connect(d.ws_url("/events", topics="t13.*"))
            try:
                for i in range(200):
                    eng.bus.publish(Event("t13.flood", "b", {"seq": i}))
                frames = recv_json_until(ws, lambda f: f["data"].get("seq") == 199)
            finally:
                ws.close()
    finally:
        eng.close_all()
    assert [f["data"]["seq"] for f in frames] == list(range(200))
    assert all(f["topic"] == "t13.flood" and f["board_id"] == "b" for f in frames)


def test_a_stalled_console_client_is_told_what_it_lost(vboard):
    # 4 MiB through uart0: unpaced (at the DUT's 20 ms/byte it would take a day).
    eng = engine_for(vboard, console_pace_s=0.0)
    try:
        with LiveDaemon(eng, write_json=False, console_limits=(8, 4096)) as d, \
                d.client() as web:
            bid = web.post("/api/v1/boards", json={"target": vboard.shell_endpoint}).json()[
                "board_id"]
            ws = ws_connect(d.ws_url(f"/boards/{bid.replace('@', '%40')}/consoles/uart0"),
                            sock=_stalled_socket(d.port), max_queue=1)
            try:
                blob = b"0123456789abcdef" * 4096               # 64 KiB per write
                for _ in range(64):                             # 4 MiB echoed back
                    ws.send(blob)
                ws.send(b"\nEND-MARK\n")
                got, notes = bytearray(), []
                deadline = time.monotonic() + 60
                while b"END-MARK" not in got and time.monotonic() < deadline:
                    msg = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                    if isinstance(msg, str):
                        notes.append(json.loads(msg))
                    else:
                        got += msg
            finally:
                ws.close()
    finally:
        eng.close_all()
    assert b"END-MARK" in got
    lost = sum(n.get("dropped", 0) for n in notes)
    assert lost > 0 and len(got) + lost >= 64 * len(blob)


# -- stopping the daemon ------------------------------------------------------------------------------------


def test_the_daemon_will_not_stop_under_a_running_job_unless_forced(vboard):
    eng = engine_for(vboard)
    release = threading.Event()
    try:
        with LiveDaemon(eng, write_json=False) as d, d.client() as web:
            bid = web.post("/api/v1/boards", json={"target": vboard.shell_endpoint}).json()[
                "board_id"]
            job = d.app.state.daemon.jobs.submit("deploy", bid, lambda p: release.wait(20))
            r = web.post("/api/v1/daemon/shutdown", json={})
            assert r.status_code == 409 and job.id in r.json()["error"]["message"]
            assert not d.stopped.is_set()
            r = web.post("/api/v1/daemon/shutdown", json={"force": True})
            assert r.status_code == 200 and r.json()["stopping"] is True
            assert d.stopped.wait(15)
    finally:
        release.set()
        eng.close_all()


# -- debug: OpenOCD belongs to the daemon, so it outlives the CLI verb that asked for it -----------------


def test_debug_up_is_a_job_and_openocd_outlives_the_client(tmp_path, monkeypatch, capsys):
    from harness_manager.services.debug import pid_alive, port_in_use
    from tests.fakes.t4_debug_rig import use_stub
    from tests.fakes.t4_rbb_jtag import FakeJtagServer

    use_stub(monkeypatch, tmp_path)
    with VirtualMps3(tmp_path / "nanosoc", boot_rm_id=0x01000001) as vb, \
            FakeJtagServer() as jtag:
        eng = engine_for(vb, rbb_port=jtag.port)
        try:
            with LiveDaemon(eng) as d, d.client() as web:
                bid = web.post("/api/v1/boards", json={"target": vb.shell_endpoint}).json()[
                    "board_id"]
                # a client asks for the debug server, then goes away
                remote = RemoteEngine.discover()
                try:
                    status = remote.debug.up(remote.session(bid))
                finally:
                    remote.close_all()
                assert status.state == "up" and status.pid > 0 and jtag.accepted == 1
                assert pid_alive(status.pid) and port_in_use(status.gdb_port)
                # a later CLI verb sees it up, served by the daemon
                rc, out, _ = run_cli(capsys, "--json", "debug", "status", vb.shell_endpoint)
                assert rc == 0 and json.loads(out)["status"]["gdb_port"] == status.gdb_port
                # a second up is ALREADY (8), as in-process
                job = web.post(f"/api/v1/boards/{bid.replace('@', '%40')}/debug/up").json()["job"]
                state = recv_job(web, job)
                assert state["state"] == "failed" and state["error"]["code"] == ExitCode.ALREADY
                down = web.post(f"/api/v1/boards/{bid.replace('@', '%40')}/debug/down").json()
                assert down["state"] == "down" and not pid_alive(status.pid)
        finally:
            eng.close_all()


def test_negative_twin_debug_up_on_a_design_without_a_debug_port(daemon, vboard, capsys,
                                                                 monkeypatch, tmp_path):
    from tests.fakes.t4_debug_rig import use_stub

    use_stub(monkeypatch, tmp_path)
    rc, out, _ = run_cli(capsys, "--json", "debug", "detect", vboard.shell_endpoint)
    rc_local, out_local, _ = in_process(monkeypatch, capsys, "--json", "debug", "detect",
                                        vboard.shell_endpoint)
    assert rc == rc_local == ExitCode.NOTHING_ON_TARGET          # greybox has no DAP
    assert json.loads(out) == json.loads(out_local)


def recv_job(web, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = web.get(f"/api/v1/jobs/{job_id}").json()
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")
