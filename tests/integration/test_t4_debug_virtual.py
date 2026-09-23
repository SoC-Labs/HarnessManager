"""T4 debug sessions on the virtual MPS3, with stub_openocd standing in for OpenOCD.

The design is chosen by the virtual board's rm_id (nanosoc = 0x01000001). The
board's jtag_server (6921) is ``FakeJtagServer``, which counts connections, so
a test can prove the probe overrides really reached the adapter. Every check
has a negative twin.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

import socharness.services.debug as dbg
from socharness.core.errors import (
    ExitCode,
    HeldError,
    NothingOnTargetError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
)
from socharness.core.events import Event, EventBus
from socharness.services.debug import (
    DEFAULT_PORT_BASE,
    PORT_BLOCK,
    PORT_SLOTS,
    DebugService,
    find_openocd,
    pid_alive,
    port_in_use,
    tcl_rpc,
)
from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.stub_openocd import read_log
from tests.fakes.t4_console_rig import EventLog, free_port
from tests.fakes.t4_debug_rig import StubRig, argv_positions, run_stub, use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.fakes.virtual_board import VirtualMps3

NANOSOC, MULTICORE, IICE = 0x01000001, 0x01000003, 0x01000008


@pytest.fixture
def rig(monkeypatch, tmp_path) -> StubRig:
    return use_stub(monkeypatch, tmp_path)


@pytest.fixture
def jtag() -> Iterator[FakeJtagServer]:
    with FakeJtagServer() as srv:
        yield srv


@pytest.fixture
def nanosoc(tmp_path) -> Iterator[VirtualMps3]:
    with VirtualMps3(tmp_path / "nanosoc", boot_rm_id=NANOSOC) as vb:
        yield vb


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def debug(bus) -> Iterator[DebugService]:
    svc = DebugService(bus, start_timeout=15)
    yield svc
    svc.close()


def open_session(vb: VirtualMps3, rbb_port: int):
    pack = Mps3Pack(console_ports=vb.console_ports, rbb_port=rbb_port)
    return pack.open(pack.candidate_for_host(vb.shell_endpoint))


def records(svc: DebugService) -> list[dict]:
    d = svc.registry_dir
    return [json.loads(p.read_text()) for p in d.glob("*.json")] if d.is_dir() else []


def default_block(board_id: str) -> int:
    return DEFAULT_PORT_BASE + PORT_BLOCK * (zlib.crc32(board_id.encode()) % PORT_SLOTS)


# -- up / status / down ----------------------------------------------------------------------


def test_up_orders_argv_serves_ports_records_the_session_and_down_cleans(rig, jtag, nanosoc,
                                                                          debug):
    session = open_session(nanosoc, jtag.port)
    st = debug.up(session)
    assert st.state == "up" and st.pid > 0
    assert st.config == ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl")

    argv = rig.runs()[-1]
    pos = argv_positions(argv)
    assert pos["probe"] < pos["first_f"] < pos["ports"] == len(argv) - 1
    assert argv[argv.index("-s") + 1] == str(rig.cfg_dir)
    assert f"set RBB_PORT {jtag.port}" in argv[:pos["first_f"]]
    assert "set TRANSPORT_MODE rbb" in argv[:pos["first_f"]]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-f"] == list(st.config)
    assert "nanosoc.cpu0 configure -event gdb-attach nanosoc_halt_examine" in argv
    assert argv[-1] == (f"gdb_port {st.gdb_port}; telnet_port {st.telnet_port}; "
                        f"tcl_port {st.tcl_port}; bindto 127.0.0.1")

    assert all(port_in_use(p) for p in (st.gdb_port, st.telnet_port, st.tcl_port))
    assert tcl_rpc(st.tcl_port, "version").startswith("Open On-Chip Debugger")
    assert jtag.accepted == 1          # it dialled OUR fake 6921: the override took effect

    (rec,) = records(debug)
    assert rec["pid"] == st.pid and rec["state"] == "up"
    assert rec["argv"] == [str(rig.binary), *argv]                # the stub logs argv[1:]
    assert rec["ports"] == {"gdb": st.gdb_port, "telnet": st.telnet_port, "tcl": st.tcl_port}
    assert rec["owner"]["pid"] == os.getpid()
    assert debug.status(session).state == "up"

    down = debug.down(session)
    assert down.state == "down" and not pid_alive(st.pid)
    assert not any(port_in_use(p) for p in (st.gdb_port, st.telnet_port, st.tcl_port))
    assert records(debug) == [] and debug.status(session).state == "down"
    assert debug.down(session).state == "down"          # nothing up: safe, idempotent


def test_a_second_up_is_already(rig, jtag, nanosoc, debug):
    session = open_session(nanosoc, jtag.port)
    debug.up(session)
    with pytest.raises(Exception) as exc:
        debug.up(session)
    assert exc.value.code == ExitCode.ALREADY and len(rig.runs()) == 1


def test_gdb_attach_runs_the_halt_examine_hook(rig, jtag, nanosoc, debug):
    session = open_session(nanosoc, jtag.port)
    st = debug.up(session)
    assert tcl_rpc(st.tcl_port, "nanosoc.cpu0 cget -event gdb-attach") == "nanosoc_halt_examine"
    socket.create_connection(("127.0.0.1", st.gdb_port), timeout=2).close()
    deadline = time.monotonic() + 5
    while not rig.events("gdb-attach") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert rig.events("gdb-attach")[0]["hook"] == "nanosoc_halt_examine"


def test_negative_twin_the_stub_rejects_the_orderings_openocd_would_get_wrong(rig):
    # A probe override after -f: the real configs would silently ignore it.
    late = run_stub(["-s", str(rig.cfg_dir), "-f", "nanosoc_mps3_jtag.cfg",
                     "-c", "set RBB_PORT 1", "-c", "shutdown"])
    assert late.returncode == 2 and "ORDER" in late.stderr
    # A port command after init: OpenOCD's own refusal.
    after = run_stub(["-c", "set RBB_HOST 127.0.0.1", "-c", "noinit", "-c", "init",
                      "-c", "gdb_port 1234", "-c", "shutdown"],
                     env=dict(os.environ, STUB_OPENOCD_NO_ADAPTER="1"))
    assert after.returncode == 1 and "must be used before 'init'" in after.stderr
    # No override at all: it would dial the lab board. The stub refuses to leave loopback.
    lab = run_stub(["-f", str(rig.cfg_dir / "nanosoc_mps3_jtag.cfg"), "-c", "init",
                    "-c", "shutdown"])
    assert lab.returncode == 3 and "192.168.10.101:6921" in lab.stderr


# -- which designs have a debug port ---------------------------------------------------------


def test_greybox_has_nothing_on_target(rig, jtag, vboard, debug):
    session = open_session(vboard, jtag.port)                # greybox, rm_id 0
    for call in (debug.up, debug.detect):
        with pytest.raises(NothingOnTargetError) as exc:
            call(session)
        assert exc.value.code == ExitCode.NOTHING_ON_TARGET == 13
    assert rig.runs() == [] and jtag.accepted == 0           # never spawned, never dialled


def test_multicore_says_its_config_does_not_exist_yet(rig, jtag, tmp_path, debug):
    with VirtualMps3(tmp_path / "mc", boot_rm_id=MULTICORE) as vb:
        with pytest.raises(NothingOnTargetError, match="two-AP config not available yet"):
            debug.up(open_session(vb, jtag.port))


def test_iice_uses_the_two_tap_chain_config(rig, jtag, tmp_path, debug):
    with VirtualMps3(tmp_path / "iice", boot_rm_id=IICE) as vb:
        st = debug.up(open_session(vb, jtag.port))
        argv = rig.runs()[-1]
        assert argv[argv.index("-f") + 1] == "nanosoc_iice_chain.cfg"
        assert st.config[0] == "nanosoc_iice_chain.cfg"


def test_missing_configs_are_unavailable(rig, jtag, nanosoc, debug, monkeypatch, tmp_path):
    monkeypatch.setenv("SOCHARNESS_MPS3_OPENOCD_DIR", str(tmp_path / "empty"))
    with pytest.raises(UnavailableError, match="SOCHARNESS_MPS3_OPENOCD_DIR") as exc:
        debug.up(open_session(nanosoc, jtag.port))
    assert exc.value.code == ExitCode.UNAVAILABLE and rig.runs() == []


# -- the binary --------------------------------------------------------------------------------


def test_no_openocd_binary_is_unavailable(rig, jtag, nanosoc, debug, monkeypatch, tmp_path):
    assert find_openocd() == str(rig.binary)                    # positive twin
    session = open_session(nanosoc, jtag.port)
    monkeypatch.setenv("SOCHARNESS_OPENOCD", str(tmp_path / "no-such-openocd"))
    with pytest.raises(UnavailableError) as exc:
        debug.up(session)
    assert exc.value.code == ExitCode.UNAVAILABLE == 12
    monkeypatch.delenv("SOCHARNESS_OPENOCD")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    with pytest.raises(UnavailableError) as exc:
        debug.detect(session)
    assert exc.value.reason == "OpenOCD not found — install it or set SOCHARNESS_OPENOCD"


# -- ports ---------------------------------------------------------------------------------------


def test_a_taken_block_is_skipped_reallocation(rig, jtag, nanosoc, debug):
    session = open_session(nanosoc, jtag.port)
    home = default_block(session.candidate.board_id)
    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", home))
        blocker.listen(1)
    except OSError:
        pytest.skip(f"port {home} is busy on this host already")
    try:
        st = debug.up(session)
        assert st.gdb_port != home and st.gdb_port >= DEFAULT_PORT_BASE
        assert all(str(home) not in a for a in rig.runs()[-1])
    finally:
        blocker.close()


def test_without_a_taken_port_the_board_gets_its_own_block(rig, jtag, nanosoc, debug):
    session = open_session(nanosoc, jtag.port)
    home = default_block(session.candidate.board_id)
    if any(port_in_use(home + i) for i in range(PORT_BLOCK)):
        pytest.skip(f"block {home} is busy on this host already")
    assert debug.up(session).gdb_port == home                  # stable across restarts


def test_a_pinned_base_that_is_taken_is_port_bound(rig, jtag, nanosoc, bus):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    base = blocker.getsockname()[1]
    svc = DebugService(bus, port_base=base)
    try:
        with pytest.raises(PortBoundError) as exc:
            svc.up(open_session(nanosoc, jtag.port))
        assert exc.value.code == ExitCode.PORT_BOUND == 5
        assert rig.runs() == []                                 # refused before spawning
    finally:
        blocker.close()
        svc.close()


def test_openocd_that_cannot_bind_gdb_is_killed_not_left_holding_the_board(rig, jtag, nanosoc,
                                                                           bus, monkeypatch):
    # Real OpenOCD 0.12 keeps running when only the gdb port is taken (measured).
    # Blind the pre-check (the race it cannot close) and make the stub hit it.
    monkeypatch.setattr(dbg, "port_in_use", lambda port: False)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    base = blocker.getsockname()[1]
    svc = DebugService(bus, port_base=base, start_timeout=10)
    try:
        with pytest.raises(PortBoundError, match=f"gdb port {base}"):
            svc.up(open_session(nanosoc, jtag.port))
        (run,) = [e for e in read_log(rig.log) if "argv" in e]
        assert not pid_alive(run["pid"])                        # killed, not left running
        assert records(svc) == []
    finally:
        blocker.close()
        svc.close()


# -- board-side failures -------------------------------------------------------------------------


def test_detect_returns_the_idcode_without_a_session(rig, jtag, nanosoc, debug):
    assert debug.detect(open_session(nanosoc, jtag.port)) == "0x6ba00477"
    argv = rig.runs()[-1]
    assert argv[-8:] == ["-c", "gdb_port disabled; telnet_port disabled; tcl_port disabled",
                         "-c", "init", "-c", "scan_chain", "-c", "shutdown"]
    assert records(debug) == []


def test_detect_during_a_session_asks_that_openocd_and_never_dials_twice(rig, jtag, nanosoc,
                                                                         debug):
    session = open_session(nanosoc, jtag.port)
    debug.up(session)
    assert debug.detect(session) == "0x6ba00477"
    assert len(rig.runs()) == 1 and jtag.accepted == 1 and jtag.refused == 0


@pytest.mark.parametrize("mode, error, code", [
    ("none", NothingOnTargetError, ExitCode.NOTHING_ON_TARGET),
    ("slam", HeldError, ExitCode.HELD),
    ("closed", UnreachableError, ExitCode.UNREACHABLE),
])
def test_negative_twins_board_side_failures(rig, nanosoc, debug, monkeypatch, mode, error, code):
    if mode == "none":
        monkeypatch.setenv("STUB_OPENOCD_IDCODE", "none")
    srv = FakeJtagServer(mode="slam" if mode == "slam" else "tap")
    port = free_port() if mode == "closed" else srv.port
    with srv:
        session = open_session(nanosoc, port)
        for call in (debug.detect, debug.up):
            with pytest.raises(error) as exc:
                call(session)
            assert exc.value.code == code
    assert records(debug) == []
    assert all(not pid_alive(e["pid"]) for e in read_log(rig.log) if "pid" in e)


# -- swaps -------------------------------------------------------------------------------------


def test_swap_closes_the_session_and_a_verified_swap_reopens_it_on_the_same_ports(
        rig, jtag, nanosoc, debug, bus):
    session = open_session(nanosoc, jtag.port)
    board = session.candidate.board_id
    log = EventLog(bus, "debug.state")
    first = debug.up(session)
    bus.publish(Event("deploy.started", board, {"overlay": "nanosoc", "rm_id": "0x01000001"}))
    # Synchronous: by the time the swap proceeds, OpenOCD is gone.
    assert not pid_alive(first.pid) and records(debug) == []
    assert log.events[-1].data["state"] == "down"
    assert "partition swap" in log.events[-1].data["detail"]
    mark = log.mark()
    bus.publish(Event("deploy.done", board, {"rm_id": "0x01000001", "verified": True}))
    log.wait_for(lambda e: e.data["state"] == "up", after=mark)
    again = debug.status(session)
    assert again.state == "up" and again.gdb_port == first.gdb_port and again.pid != first.pid
    assert len(rig.runs()) == 2


@pytest.mark.parametrize("ending", ["failed-deploy", "failed-confirm", "unverified"])
def test_negative_twin_a_swap_that_does_not_verify_leaves_it_down(rig, jtag, nanosoc, debug, bus,
                                                                 ending):
    session = open_session(nanosoc, jtag.port)
    board = session.candidate.board_id
    log = EventLog(bus, "debug.state")
    debug.up(session)
    bus.publish(Event("deploy.started", board, {"overlay": "nanosoc"}))
    if ending == "unverified":
        bus.publish(Event("deploy.done", board, {"rm_id": "0x01000001", "verified": False}))
        want = "not verified"
    else:
        stage = ending.split("-")[1]
        bus.publish(Event("deploy.failed", board, {"reason": "boom", "stage": stage}))
        want = f"swap failed at {stage}"
    assert want in log.events[-1].data["detail"]
    for t in debug.threads:
        t.join(5)
    assert debug.status(session).state == "down" and len(rig.runs()) == 1


def test_negative_twin_a_preflight_refusal_leaves_the_session_alone(rig, jtag, nanosoc, debug,
                                                                   bus):
    session = open_session(nanosoc, jtag.port)
    st = debug.up(session)
    bus.publish(Event("deploy.failed", session.candidate.board_id,
                      {"reason": "static_id", "stage": "preflight"}))
    assert debug.status(session).pid == st.pid and pid_alive(st.pid)


def test_a_swap_to_a_design_without_a_dap_stays_down_and_says_why(rig, jtag, nanosoc, debug, bus):
    session = open_session(nanosoc, jtag.port)
    board = session.candidate.board_id
    log = EventLog(bus, "debug.state")
    debug.up(session)
    bus.publish(Event("deploy.started", board, {"overlay": "greybox"}))
    nanosoc.shell.current_rm_id = 0                               # the swap loaded greybox
    mark = log.mark()
    bus.publish(Event("deploy.done", board, {"rm_id": "0x00000000", "verified": True}))
    ev = log.wait_for(lambda e: e.data["state"] == "failed", after=mark)
    assert "no debug port" in ev.data["detail"]
    assert debug.status(session).state == "down"


# -- orphans -----------------------------------------------------------------------------------


REPO = Path(__file__).resolve().parents[2]


def _owner_env(rig: StubRig) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    return env


def test_an_orphan_is_reaped(rig, jtag, debug):
    from tests.fakes.t4_console_rig import BareSession
    from tests.fakes.t4_debug_rig import StaticDebugAdapter, start_owner_process

    board = "mps3@orphan-test"
    owner, st = start_owner_process(REPO, rig.cfg_dir, jtag.port, board, _owner_env(rig))
    owner.kill()                                   # the engine dies; OpenOCD does not
    owner.wait(5)
    assert pid_alive(st["pid"]) and port_in_use(st["gdb"])        # the HAPS orphan, reproduced
    session = BareSession(board, debug=StaticDebugAdapter(rig.cfg_dir, jtag.port))
    status = debug.status(session)
    assert status.state == "down" and "reaped" in status.detail
    deadline = time.monotonic() + 5
    while pid_alive(st["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_alive(st["pid"]) and records(debug) == []
    assert debug.up(session).state == "up"        # and the board is usable again


def test_negative_twin_a_live_owners_session_is_held_not_reaped(rig, jtag, debug):
    from tests.fakes.t4_console_rig import BareSession
    from tests.fakes.t4_debug_rig import StaticDebugAdapter, start_owner_process

    board = "mps3@held-test"
    owner, st = start_owner_process(REPO, rig.cfg_dir, jtag.port, board, _owner_env(rig))
    try:
        session = BareSession(board, debug=StaticDebugAdapter(rig.cfg_dir, jtag.port))
        status = debug.status(session)
        assert status.state == "up" and "held by" in status.detail and status.pid == st["pid"]
        for call in (debug.up, debug.down):
            with pytest.raises(HeldError) as exc:
                call(session)
            assert exc.value.code == ExitCode.HELD
        assert pid_alive(st["pid"])
        assert debug.down(session, force=True).state == "down"
        assert not pid_alive(st["pid"])
    finally:
        owner.kill()
        owner.wait(5)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pid identity needs /proc")
def test_negative_twin_a_reused_pid_is_never_killed(rig, debug, tmp_path):
    import subprocess

    from tests.fakes.t4_console_rig import BareSession

    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        debug.registry_dir.mkdir(parents=True, exist_ok=True)
        board = "mps3@reuse-test"
        (debug.registry_dir / "mps3_reuse-test.json").write_text(json.dumps({
            "board_id": board, "pid": bystander.pid, "state": "up",
            "ports": {"gdb": free_port(), "telnet": free_port(), "tcl": free_port()},
            "ports_command": "gdb_port 1; telnet_port 2; tcl_port 3; bindto 127.0.0.1",
            "owner": {"user": "x", "host": socket.gethostname(), "pid": 999_999_999}}))
        status = debug.status(BareSession(board))
        assert status.state == "down" and "another program" in status.detail
        assert bystander.poll() is None                          # untouched
        assert records(debug) == []
    finally:
        bystander.kill()
        bystander.wait()


def test_a_session_whose_ports_are_all_gone_is_reaped(rig, debug):
    import subprocess

    from tests.fakes.t4_console_rig import BareSession

    silent = "gdb_port disabled; telnet_port disabled; tcl_port disabled"
    proc = subprocess.Popen([str(rig.binary), "-c", silent],
                            env=dict(os.environ, STUB_OPENOCD_NO_ADAPTER="1"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        debug.registry_dir.mkdir(parents=True, exist_ok=True)
        board = "mps3@ports-gone"
        (debug.registry_dir / "mps3_ports-gone.json").write_text(json.dumps({
            "board_id": board, "pid": proc.pid, "state": "up",
            "ports": {"gdb": free_port(), "telnet": free_port(), "tcl": free_port()},
            "ports_command": silent,
            "owner": {"user": "x", "host": socket.gethostname(), "pid": os.getppid()}}))
        status = debug.status(BareSession(board))
        assert status.state == "down" and "none of its ports" in status.detail
        assert proc.wait(5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_reap_orphans_sweeps_every_board(rig, jtag, debug):
    from tests.fakes.t4_debug_rig import start_owner_process

    owner, st = start_owner_process(REPO, rig.cfg_dir, jtag.port, "mps3@sweep", _owner_env(rig))
    owner.kill()
    owner.wait(5)
    assert debug.reap_orphans() == ["mps3@sweep"]
    assert debug.reap_orphans() == []                            # negative twin: nothing left
