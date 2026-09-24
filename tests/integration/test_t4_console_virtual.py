"""T4 consoles on the virtual MPS3: FakeShell's console ports behind a single-client proxy.

FakeShell's console servers accept any number of clients. The real firmware
accepts ONE (uart_over_eth.c:188-198). ``SingleClientProxy`` restores that rule
and counts connections, so "the board saw one connection" is a real assertion.
Every check has a negative twin.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
from pyverify.console import ConsoleReader

from harness_manager.core.errors import AbsentError, ExitCode, PortBoundError, UnreachableError
from harness_manager.core.events import Event, EventBus
from harness_manager.services.console import ConsoleBroker
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t4_console_rig import (
    EventLog,
    SingleClientProxy,
    read_until,
    recv_until,
)
from tests.fakes.virtual_board import VirtualMps3

BANNER = b"nanosoc boot\n"          # FakeShell's default uart0 banner, sent on every connect


@pytest.fixture
def proxy(vboard: VirtualMps3) -> Iterator[SingleClientProxy]:
    p = SingleClientProxy(vboard.console_ports["uart0"])
    yield p
    p.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def broker(bus: EventBus) -> Iterator[ConsoleBroker]:
    b = ConsoleBroker(bus, backoff=(0.05, 0.2))
    yield b
    b.shutdown()


@pytest.fixture
def session(vboard: VirtualMps3, proxy: SingleClientProxy):
    # Unpaced: these tests push kilobytes through uart0. Pacing has its own tests below.
    ports = dict(vboard.console_ports, uart0=proxy.port)
    pack = Mps3Pack(console_ports=ports, console_pace_s=0.0)
    return pack.open(pack.candidate_for_host(vboard.shell_endpoint))


@pytest.fixture
def paced_session(vboard: VirtualMps3, proxy: SingleClientProxy):
    ports = dict(vboard.console_ports, uart0=proxy.port)
    pack = Mps3Pack(console_ports=ports)            # the real default: DUT UARTs paced
    return pack.open(pack.candidate_for_host(vboard.shell_endpoint))


def up_after(log: EventLog, mark: int, name: str = "uart0") -> Event:
    return log.wait_for(lambda e: e.topic == "console.state" and e.data["name"] == name
                        and e.data["state"] == "up", after=mark)


# -- one connection, many readers ------------------------------------------------------------


def test_two_subscribers_both_get_the_banner_over_one_board_connection(broker, session, proxy):
    a = broker.subscribe(session, "uart0")
    b = broker.subscribe(session, "uart0")
    assert read_until(a, BANNER) == BANNER
    assert read_until(b, BANNER) == BANNER
    assert (proxy.accepted, proxy.refused) == (1, 0)
    assert broker.names(session) == ["swo", "uart0", "uart1"]


def test_negative_twin_the_port_really_refuses_a_second_client(proxy):
    # Without this, "accepted == 1" above could just mean the proxy never counts.
    with ConsoleReader("127.0.0.1", proxy.port, timeout=2.0) as first:
        assert first.read_until(BANNER, timeout=2.0) == BANNER
        with ConsoleReader("127.0.0.1", proxy.port, timeout=2.0) as second:
            with pytest.raises(ConnectionError):
                second.read_until(BANNER, timeout=2.0)
    assert (proxy.accepted, proxy.refused) == (1, 1)


def test_a_late_subscriber_gets_the_banner_from_scrollback_not_a_new_connection(broker, session,
                                                                               proxy):
    first = broker.subscribe(session, "uart0")
    read_until(first, BANNER)
    late = broker.subscribe(session, "uart0")
    assert late.read(2.0) == BANNER
    # Negative twin: without replay, the late reader sees nothing (no second dial).
    blind = broker.subscribe(session, "uart0", replay=False)
    assert blind.read(0.3) == b""
    assert proxy.accepted == 1


def test_writes_from_two_subscribers_do_not_interleave(broker, session):
    a = broker.subscribe(session, "uart0")
    b = broker.subscribe(session, "uart0", replay=False)
    read_until(a, BANNER)
    lines = 40

    def writer(stream, ch: bytes) -> None:
        for _ in range(lines):
            stream.write(ch * 60 + b"\n")

    ts = [threading.Thread(target=writer, args=(a, b"A")),
          threading.Thread(target=writer, args=(b, b"B"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    echoed = b""
    while echoed.count(b"\n") < 2 * lines:           # FakeShell echoes every byte back
        chunk = b.read(5.0)
        assert chunk, f"echo stalled after {len(echoed.splitlines())} lines"
        echoed += chunk.replace(BANNER, b"")          # b may have caught the banner live
    for line in echoed.splitlines():
        assert line in (b"A" * 60, b"B" * 60), line
    assert echoed.count(b"A" * 60) == echoed.count(b"B" * 60) == lines


def test_negative_twin_writing_to_a_closed_subscription_is_refused(broker, session):
    s = broker.subscribe(session, "uart0")
    s.close()
    with pytest.raises(Exception, match="closed"):
        s.write(b"x")
    assert s.read(0.1) == b"" and s.closed


# -- reconnect ----------------------------------------------------------------------------------


def test_upstream_drop_reconnects_and_subscribers_keep_reading(broker, session, proxy, bus):
    log = EventLog(bus, "console.state")
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    mark = log.mark()
    proxy.drop()                                      # what a partition swap does to UART1/SWO
    log.wait_for(lambda e: e.data["state"] == "down", after=mark)
    up_after(log, mark)
    assert read_until(s, BANNER) == BANNER           # the same stream, after the re-dial
    assert proxy.accepted == 2
    states = [e.data["state"] for e in log.events[mark:]]
    assert "down" in states and states[-1] == "up"


def test_negative_twin_a_board_that_is_away_stays_down_until_it_returns(broker, session, proxy,
                                                                       bus):
    log = EventLog(bus, "console.state")
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    mark = log.mark()
    proxy.down()                                       # refused, not just dropped
    ev = log.wait_for(lambda e: e.data["state"] == "down" and "cannot open" in e.data["detail"],
                      after=mark)
    assert ev.data["name"] == "uart0"
    assert s.read(0.3) == b""                          # a timeout, not an error, not a hang
    with pytest.raises(UnreachableError):
        s.write(b"lost\n")                             # honest: nothing to write to
    mark = log.mark()
    proxy.up()
    up_after(log, mark)
    assert read_until(s, BANNER) == BANNER


def test_a_port_held_by_another_client_is_reported_then_taken_when_free(broker, session, proxy,
                                                                       bus):
    log = EventLog(bus, "console.state")
    other = ConsoleReader("127.0.0.1", proxy.port, timeout=2.0).connect()
    other.read_until(BANNER, timeout=2.0)
    s = broker.subscribe(session, "uart0")
    ev = log.wait_for(lambda e: e.data["state"] == "down" and "holds" in e.data["detail"])
    assert "one client per port" in ev.data["detail"]
    mark = log.mark()
    other.close()
    up_after(log, mark)
    assert read_until(s, BANNER) == BANNER


# -- release ------------------------------------------------------------------------------------


def test_the_last_subscriber_leaving_frees_the_board_port(broker, session, proxy, bus):
    log = EventLog(bus, "console.state")
    a = broker.subscribe(session, "uart0")
    b = broker.subscribe(session, "uart0")
    read_until(a, BANNER)
    a.close()
    # Negative twin: one reader left, so the port is still ours.
    with ConsoleReader("127.0.0.1", proxy.port, timeout=2.0) as other:
        with pytest.raises(ConnectionError):
            other.read_until(BANNER, timeout=2.0)
    b.close()
    log.wait_for(lambda e: e.data["state"] == "closed")
    with ConsoleReader("127.0.0.1", proxy.port, timeout=2.0) as other:
        assert other.read_until(BANNER, timeout=2.0) == BANNER
    assert broker.state(session.candidate.board_id, "uart0") == "closed"


def test_close_all_ends_every_stream_and_is_safe_twice(broker, session):
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    broker.close_all(session.candidate.board_id)
    assert s.read(1.0) == b"" and s.closed
    broker.close_all(session.candidate.board_id)          # nothing open: a no-op
    broker.close_all("mps3@nowhere")


# -- TCP re-export ------------------------------------------------------------------------------


def test_export_tcp_round_trip(broker, session, proxy):
    watcher = broker.subscribe(session, "uart0")
    port = broker.export_tcp(session, "uart0")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as term:
        assert recv_until(term, BANNER) == BANNER       # scrollback for the new terminal
        term.sendall(b"help\n")
        assert b"help\n" in recv_until(term, b"help\n")  # FakeShell echo, back to the terminal
    assert b"help\n" in read_until(watcher, b"help\n")   # ...and to the other reader
    assert proxy.accepted == 1                            # still one board connection
    broker.unexport(session.candidate.board_id, port)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_negative_twin_export_on_a_taken_port_is_port_bound(broker, session):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        with pytest.raises(PortBoundError) as exc:
            broker.export_tcp(session, "uart0", blocker.getsockname()[1])
        assert exc.value.code == ExitCode.PORT_BOUND
    finally:
        blocker.close()
    assert broker.state(session.candidate.board_id, "uart0") == "closed"   # nothing left open


def test_negative_twin_unknown_console_is_absent(broker, session):
    with pytest.raises(AbsentError) as exc:
        broker.subscribe(session, "uart7")
    assert exc.value.code == ExitCode.ABSENT and "uart0" in exc.value.hint


# -- swaps ---------------------------------------------------------------------------------------


def test_swap_closes_the_console_and_a_verified_swap_reopens_it(broker, session, proxy, bus):
    log = EventLog(bus, "console.state")
    board = session.candidate.board_id
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    mark = log.mark()
    bus.publish(Event("deploy.started", board, {"overlay": "nanosoc", "rm_id": "0x01000001"}))
    ev = log.wait_for(lambda e: e.data["state"] == "down", after=mark)
    assert "partition swap" in ev.data["detail"]
    # really closed, and not re-dialled: the proxy's relay thread notices the close a
    # moment after the broker publishes "down" (a race on slow CI runners)
    deadline = time.monotonic() + 5
    while proxy.busy and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not proxy.busy and proxy.accepted == 1
    mark = log.mark()
    bus.publish(Event("deploy.done", board, {"rm_id": "0x01000001", "verified": True}))
    up_after(log, mark)
    assert read_until(s, BANNER) == BANNER
    assert proxy.accepted == 2


def test_negative_twin_a_failed_swap_leaves_the_console_down(broker, session, proxy, bus):
    log = EventLog(bus, "console.state")
    board = session.candidate.board_id
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    bus.publish(Event("deploy.started", board, {"overlay": "nanosoc"}))
    mark = log.mark()
    bus.publish(Event("deploy.failed", board, {"reason": "rm_id mismatch", "stage": "confirm"}))
    ev = log.wait_for(lambda e: "swap failed at confirm" in e.data["detail"], after=mark)
    assert ev.data["state"] == "down"
    assert s.read(0.3) == b"" and proxy.accepted == 1
    mark = log.mark()
    broker.reconnect(board)                               # the operator's explicit retry
    up_after(log, mark)


def test_negative_twin_a_preflight_refusal_touches_nothing(broker, session, proxy, bus):
    log = EventLog(bus, "console.state")
    board = session.candidate.board_id
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    mark = log.mark()
    bus.publish(Event("deploy.failed", board, {"reason": "static_id", "stage": "preflight"}))
    s.write(b"still here\n")
    assert b"still here" in read_until(s, b"still here\n")
    assert log.events[mark:] == [] and proxy.accepted == 1


# -- the Debug USB lanes (T3's endpoints) -------------------------------------------------


def test_shell_alias_opens_lane_2_over_usb_and_never_the_mcc(tmp_path, broker):
    from harness_manager.core.model import Candidate, Link, LinkKind
    from harness_manager.core.transport import register_fake_serial, unregister_fake_serial
    from tests.fakes.t4_console_rig import FakeUart

    lane2 = FakeUart()
    lane_url = register_fake_serial("t4-lane2", lane2)
    try:
        with VirtualMps3(tmp_path / "usb", usb=True) as vb:
            cand = Candidate(pack="mps3", board_id=f"mps3@{vb.shell_endpoint}", links=(
                Link(LinkKind.ETHERNET, vb.shell_endpoint, "shell control channel"),
                Link(LinkKind.USB_SERIAL, vb.mcc_url, "FT4232H if00: MCC console"),
                Link(LinkKind.USB_SERIAL, lane_url, "FT4232H if02: FPGA UART lane 2"),
            ))
            session = Mps3Pack(console_ports=vb.console_ports).open(cand)
            endpoints = session.consoles.console_endpoints()
            assert endpoints["fpga_uart2"] == lane_url
            assert vb.mcc_url not in endpoints.values()          # T3 never lists the MCC
            assert "shell" in broker.names(session)
            s = broker.subscribe(session, "shell")
            lane2.feed(b"harness 1.0.0 shell\r\n")
            assert read_until(s, b"\n") == b"harness 1.0.0 shell\r\n"
            assert broker.resolve(session, "shell") == ("fpga_uart2", lane_url)
    finally:
        unregister_fake_serial("t4-lane2")


# -- paced input (lead): the DUT UART has no receive FIFO -----------------------------------


def test_dut_uart_input_is_paced_and_write_does_not_block(broker, paced_session):
    import time

    from harness_manager_mps3.constants import DUT_CONSOLE_PACE_S

    assert paced_session.consoles.console_write_pace_s() == {
        "uart0": DUT_CONSOLE_PACE_S, "uart1": DUT_CONSOLE_PACE_S}
    s = broker.subscribe(paced_session, "uart0")
    read_until(s, BANNER)
    text = b"print(1+1)\r"
    t0 = time.monotonic()
    s.write(text)
    assert time.monotonic() - t0 < 0.1                     # queued, not sent inline
    echoed = read_until(s, text)
    took = time.monotonic() - t0
    assert text in echoed
    assert took >= (len(text) - 1) * DUT_CONSOLE_PACE_S     # one byte per pace, at least


def test_negative_twin_swo_and_an_unpaced_pack_are_not_paced(broker, session, paced_session):
    assert "swo" not in paced_session.consoles.console_write_pace_s()
    assert session.consoles.console_write_pace_s() == {"uart0": 0.0, "uart1": 0.0}
    import time

    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    t0 = time.monotonic()
    s.write(b"x" * 200 + b"\n")
    read_until(s, b"x" * 200)
    assert time.monotonic() - t0 < 200 * 0.02 / 2                # far faster than paced


def test_after_a_verified_swap_a_new_subscriber_is_not_replayed_the_old_design(broker, session,
                                                                               bus):
    # ILA mint findings 2026-09-24 #4: output from before a swap belongs to the old design.
    board = session.candidate.board_id
    log = EventLog(bus, "console.state")
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    s.write(b"OLD-DESIGN\n")
    read_until(s, b"OLD-DESIGN")                          # FakeShell echoes: now in scrollback
    mark = log.mark()
    bus.publish(Event("deploy.started", board, {"overlay": "nanosoc", "rm_id": "0x01000001"}))
    log.wait_for(lambda e: e.data["state"] == "down", after=mark)
    mark = log.mark()
    bus.publish(Event("deploy.done", board, {"rm_id": "0x01000001", "verified": True}))
    up_after(log, mark)
    late = broker.subscribe(session, "uart0")             # replay=True by default
    got = read_until(late, BANNER)
    assert b"OLD-DESIGN" not in got


def test_negative_twin_without_a_swap_the_scrollback_is_replayed(broker, session):
    s = broker.subscribe(session, "uart0")
    read_until(s, BANNER)
    s.write(b"SAME-DESIGN\n")
    read_until(s, b"SAME-DESIGN")
    late = broker.subscribe(session, "uart0")
    assert b"SAME-DESIGN" in read_until(late, b"SAME-DESIGN")
