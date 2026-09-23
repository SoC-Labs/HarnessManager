"""T4 through the real stack (S4 + S5 shape): T1's Engine, T2's DeployService, the virtual MPS3.

The engine builds ``ConsoleBroker(engine)`` and ``DebugService(engine)`` itself.
A real (synthetic) overlay deploy on FakeShell publishes ``deploy.started`` and
``deploy.done``; the consoles and the OpenOCD session must close before the
swap and come back after it. OpenOCD is stub_openocd; uart0 sits behind the
single-client proxy so the board's connection count is observable.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from socharness.core.errors import IncompatibleError
from socharness.core.events import EventBus
from socharness.core.services import EngineConfig
from socharness.engine import Engine
from socharness.services.console import ConsoleBroker
from socharness.services.debug import DebugService, pid_alive
from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.t2_overlays import (
    OTHER_STATIC_ID,
    make_overlay,
    point_pushes_at,
    ref_named,
    use_overlay_dirs,
)
from tests.fakes.t4_console_rig import EventLog, SingleClientProxy, read_until
from tests.fakes.t4_debug_rig import use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.fakes.virtual_board import VirtualMps3

NANOSOC = 0x01000001
NANOSOC_V1_1 = 0x01010001        # same design, next version: still has the DAP
NO_DAP = 0x01007A57              # T2's synthetic design: no debug port
BANNER = b"nanosoc boot\n"


@pytest.fixture
def board(tmp_path) -> Iterator[VirtualMps3]:
    with VirtualMps3(tmp_path / "vb", boot_rm_id=NANOSOC) as vb:
        yield vb


@pytest.fixture
def jtag() -> Iterator[FakeJtagServer]:
    with FakeJtagServer() as srv:
        yield srv


@pytest.fixture
def proxy(board) -> Iterator[SingleClientProxy]:
    p = SingleClientProxy(board.console_ports["uart0"])
    yield p
    p.close()


@pytest.fixture
def engine(board, jtag, proxy, tmp_path, monkeypatch) -> Iterator[tuple[Engine, object, EventLog]]:
    use_stub(monkeypatch, tmp_path)
    point_pushes_at(monkeypatch, board)
    ovl = tmp_path / "ov"
    make_overlay(ovl, "nanosoc_v11", rm_id=NANOSOC_V1_1)
    make_overlay(ovl, "synth", rm_id=NO_DAP)
    make_overlay(ovl, "foreign", rm_id=NANOSOC_V1_1, static_id=OTHER_STATIC_ID)
    use_overlay_dirs(monkeypatch, ovl)
    bus = EventBus()
    log = EventLog(bus)
    eng = Engine(EngineConfig(state_dir=tmp_path / "state"), bus=bus,
                 packs={"mps3": Mps3Pack(console_ports=dict(board.console_ports,
                                                            uart0=proxy.port),
                                         rbb_port=jtag.port)})
    cand = eng.candidate_for(board.shell_endpoint)
    session = eng.open(cand, note="t4 swap")
    # Fast reconnects for the test; the engine built the broker lazily with defaults.
    eng.consoles.backoff = (0.05, 0.2)
    try:
        yield eng, session, log
    finally:
        eng.close_all()


def is_(topic: str, state: str):
    return lambda e: e.topic == topic and e.data.get("state") == state


def test_engine_builds_the_t4_services(engine):
    eng, _, _ = engine
    assert isinstance(eng.consoles, ConsoleBroker) and isinstance(eng.debug, DebugService)


def test_s5_debug_and_s4_console_survive_a_verified_swap(engine, proxy):
    eng, session, log = engine
    first = eng.debug.up(session)
    con = eng.consoles.subscribe(session, "uart0")
    read_until(con, BANNER)

    mark = log.mark()
    result = eng.deploy.deploy(session, ref_named(eng.deploy.overlays(session), "nanosoc_v11"))
    assert result.verified

    topics = [e.topic for e in log.events[mark:]]
    started = topics.index("deploy.started")
    # Both were closed BEFORE the swap pushed anything (synchronous handlers).
    closed = [e for e in log.events[mark:][started:] if is_("debug.state", "down")(e)]
    assert closed and "partition swap" in closed[0].data["detail"]
    assert not pid_alive(first.pid)

    log.wait_for(is_("debug.state", "up"), after=mark)
    log.wait_for(lambda e: is_("console.state", "up")(e) and e.data["name"] == "uart0",
                 after=mark + started)
    again = eng.debug.status(session)
    assert again.state == "up" and again.gdb_port == first.gdb_port and again.pid != first.pid
    assert read_until(con, BANNER) == BANNER            # the same subscription, re-dialled
    assert proxy.accepted == 2 and proxy.refused == 0


def test_negative_twin_a_refused_deploy_leaves_both_alone(engine, proxy):
    eng, session, log = engine
    first = eng.debug.up(session)
    con = eng.consoles.subscribe(session, "uart0")
    read_until(con, BANNER)
    mark = log.mark()
    with pytest.raises(IncompatibleError):
        eng.deploy.deploy(session, ref_named(eng.deploy.overlays(session), "foreign"))
    assert [e.topic for e in log.events[mark:]] == ["deploy.failed"]
    assert eng.debug.status(session).pid == first.pid and pid_alive(first.pid)
    assert proxy.accepted == 1 and eng.consoles.state(session.candidate.board_id, "uart0") == "up"


def test_a_swap_to_a_design_without_a_dap_leaves_debug_down_with_the_reason(engine):
    eng, session, log = engine
    eng.debug.up(session)
    mark = log.mark()
    eng.deploy.deploy(session, ref_named(eng.deploy.overlays(session), "synth"))
    ev = log.wait_for(is_("debug.state", "failed"), after=mark)
    assert "no debug port" in ev.data["detail"]
    assert eng.debug.status(session).state == "down"


def test_engine_close_stops_openocd_and_closes_consoles(engine):
    eng, session, _ = engine
    st = eng.debug.up(session)
    con = eng.consoles.subscribe(session, "uart0")
    read_until(con, BANNER)
    eng.close(session.candidate.board_id)
    assert not pid_alive(st.pid) and con.closed
    eng.close(session.candidate.board_id)                   # already closed: a no-op


def test_negative_twin_engine_close_with_nothing_open_is_safe(board, jtag, tmp_path: Path):
    eng = Engine(EngineConfig(state_dir=tmp_path / "state2"),
                 packs={"mps3": Mps3Pack(console_ports=board.console_ports, rbb_port=jtag.port)})
    eng.open(eng.candidate_for(board.shell_endpoint))
    _ = eng.debug, eng.consoles                               # instantiated, never used
    eng.close_all()
