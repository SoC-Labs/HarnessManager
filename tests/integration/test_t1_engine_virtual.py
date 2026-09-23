"""End to end (Team T1): Engine -> MPS3 pack -> pyverify -> FakeShell (fielded 0x3F1A560F).

Also proves the session lock across two real processes: a child Python process
opens the board through its own Engine, and this process is turned away.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode, HeldError, UnreachableError
from harness_manager.core.events import EventBus
from harness_manager.core.model import LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.telemetry import NO_SOURCE_REASON
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.virtual_board import VirtualMps3

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def engine_for(vb: VirtualMps3, state: Path, bus: EventBus | None = None) -> Engine:
    return Engine(EngineConfig(state_dir=state),
                  packs={"mps3": Mps3Pack(console_ports=vb.console_ports)}, bus=bus)


def test_open_info_close_on_the_fielded_board(vboard: VirtualMps3, state: Path):
    bus = EventBus()
    seen: list = []
    bus.subscribe("*", seen.append)
    eng = engine_for(vboard, state, bus)
    cand = eng.candidate_for(vboard.shell_endpoint)
    eng.open(cand, note="t1 integration")
    info = eng.info(cand.board_id)

    ident = info.identity
    assert ident.shell_id.lower() == "0x3f1a560f"
    assert ident.harness_version == "1.0.0" and ident.rm_name == "greybox"
    assert ident.build_check.value == "unchecked"          # USR_ACCESS unreadable
    assert info.health.reachable and info.health.control_channel == "idle"
    assert "deploy_partial" in info.capabilities and "identify" in info.capabilities
    # Ethernet only: the reason for each missing capability is carried, not guessed.
    assert "reboot_board" not in info.capabilities
    assert info.unavailable["reboot_board"].startswith("needs the Debug USB cable")
    assert info.unavailable["reset_shell"] == "needs harness firmware with 'reboot'"

    lock = eng.lock_owner(cand.board_id)
    assert lock is not None and lock.pid == os.getpid() and lock.note == "t1 integration"
    eng.close(cand.board_id)
    assert eng.lock_owner(cand.board_id) is None
    assert [e.topic for e in seen] == ["session.opened", "board.identity", "session.closed"]
    assert seen[1].data["shell_id"].lower() == "0x3f1a560f"


def test_usb_links_light_up_controller_capabilities(tmp_path: Path, state: Path):
    # Twin of the Ethernet-only view: the same board with the Debug USB links.
    with VirtualMps3(tmp_path / "usb", usb=True) as vb:
        eng = engine_for(vb, state)
        cand = vb.candidate(usb=True)
        assert {lk.kind for lk in cand.links} >= {LinkKind.USB_SERIAL, LinkKind.USB_MSD}
        eng.open(cand)
        info = eng.info(cand.board_id)
        assert {"reboot_board", "console_controller", "storage_backup"} <= info.capabilities
        assert "reboot_board" not in info.unavailable
        eng.close_all()


def test_info_on_an_unreachable_board_is_exit_7(state: Path):
    eng = Engine(EngineConfig(state_dir=state), packs={"mps3": Mps3Pack()})
    cand = eng.candidate_for("127.0.0.1:1")
    eng.open(cand)                          # opening needs no traffic; asking does
    with pytest.raises(UnreachableError) as exc:
        eng.info(cand.board_id)
    assert exc.value.code == ExitCode.UNREACHABLE
    eng.close(cand.board_id)
    assert eng.lock_owner(cand.board_id) is None


def test_probe_finds_the_virtual_board_once(vboard: VirtualMps3, state: Path):
    bus = EventBus()
    seen: list = []
    bus.subscribe("board.*", seen.append)
    eng = engine_for(vboard, state, bus)
    hints = ProbeHints(hosts=(vboard.shell_endpoint, vboard.shell_endpoint), scan_usb=False,
                       timeout_s=1.0)
    found = eng.probe(hints)
    assert [c.board_id for c in found] == [f"mps3@{vboard.shell_endpoint}"]
    assert [e.topic for e in seen] == ["board.found"] and seen[0].data["pack"] == "mps3"


def test_probe_of_a_dead_address_finds_nothing(state: Path):
    bus = EventBus()
    seen: list = []
    bus.subscribe("*", seen.append)
    eng = Engine(EngineConfig(state_dir=state), packs={"mps3": Mps3Pack()}, bus=bus)
    assert eng.probe(ProbeHints(hosts=("127.0.0.1:1",), scan_usb=False, timeout_s=0.5)) == []
    assert seen == []


def test_telemetry_on_the_ethernet_only_board_is_explicitly_unavailable(vboard, state: Path):
    eng = engine_for(vboard, state)
    cand = eng.candidate_for(vboard.shell_endpoint)
    session = eng.open(cand)
    readings = eng.telemetry.readings(session)
    assert readings and all(r.available or r.reason for r in readings)   # never a silent 0
    if session.telemetry is None and session.controller is None:
        assert [(r.name, r.value, r.reason) for r in readings] == [
            ("temperature", None, NO_SOURCE_REASON)]
    eng.close_all()


# -- two real processes -------------------------------------------------------------------

CHILD = textwrap.dedent("""
    import sys
    from pathlib import Path
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    eng = Engine(EngineConfig(state_dir=Path(sys.argv[1])))
    eng.open(eng.candidate_for(sys.argv[2]), note="child process holding the board")
    print("HELD", flush=True)
    if sys.stdin.readline().strip() == "release":
        eng.close_all()
        print("RELEASED", flush=True)
""")


def spawn_holder(state: Path, endpoint: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(state), endpoint],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=REPO)
    line = proc.stdout.readline().strip()
    if line != "HELD":
        proc.kill()
        pytest.fail(f"child did not take the lock: {line!r} {proc.stderr.read()}")
    return proc


def test_a_second_process_is_turned_away_then_admitted(vboard: VirtualMps3, state: Path):
    child = spawn_holder(state, vboard.shell_endpoint)
    try:
        eng = engine_for(vboard, state)
        cand = eng.candidate_for(vboard.shell_endpoint)
        with pytest.raises(HeldError) as exc:
            eng.open(cand)
        assert exc.value.code == ExitCode.HELD
        assert f"pid {child.pid}" in exc.value.holder
        assert "child process holding the board" in exc.value.holder
        # Negative twin: once the holder releases, this process gets the board.
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "RELEASED"
        assert child.wait(timeout=20) == 0
        eng.open(cand)
        assert eng.lock_owner(cand.board_id).pid == os.getpid()
        eng.close_all()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=20)


def test_a_killed_holder_leaves_a_stale_lock_that_is_taken_over(vboard: VirtualMps3,
                                                                 state: Path):
    child = spawn_holder(state, vboard.shell_endpoint)
    child.kill()                                   # no release: the lock file stays behind
    child.wait(timeout=20)
    eng = engine_for(vboard, state)
    cand = eng.candidate_for(vboard.shell_endpoint)
    assert eng.lock_owner(cand.board_id).pid == child.pid
    eng.open(cand, note="after the crash")
    assert eng.lock_owner(cand.board_id).pid == os.getpid()
    eng.close_all()
