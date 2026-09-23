"""T4 console broker, board-free: serial (``fake://``) endpoints, line events, back-pressure."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from socharness.core.errors import AbsentError, ExitCode, UnavailableError
from socharness.core.events import EventBus
from socharness.core.transport import register_fake_serial, unregister_fake_serial
from socharness.services.console import ConsoleBroker
from tests.fakes.t4_console_rig import BareSession, EventLog, FakeUart, read_until


@pytest.fixture
def uart() -> Iterator[tuple[FakeUart, str]]:
    port = FakeUart()
    url = register_fake_serial("t4-uart", port)
    yield port, url
    unregister_fake_serial("t4-uart")


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def broker(bus: EventBus) -> Iterator[ConsoleBroker]:
    b = ConsoleBroker(bus, backoff=(0.05, 0.2), line_idle_s=0.2)
    yield b
    b.shutdown()


def test_serial_endpoint_via_fake_url(broker, uart):
    port, url = uart
    session = BareSession(consoles={"fpga0": url})
    s = broker.subscribe(session, "fpga0")
    port.feed(b"Hello world\r\n")
    assert read_until(s, b"\n") == b"Hello world\r\n"
    s.write(b"x")
    assert bytes(port.written) == b"x"


def test_negative_twin_serial_endpoint_that_does_not_exist_reports_down(broker, bus):
    log = EventLog(bus, "console.state")
    session = BareSession(consoles={"fpga0": "fake://no-such-port"})
    s = broker.subscribe(session, "fpga0")
    ev = log.wait_for(lambda e: e.data["state"] == "down")
    assert "cannot open fake://no-such-port" in ev.data["detail"]
    assert s.read(0.1) == b""


def test_serial_unplug_is_a_drop_and_it_reconnects(broker, bus, uart):
    port, url = uart
    log = EventLog(bus, "console.state")
    s = broker.subscribe(BareSession(consoles={"fpga0": url}), "fpga0")
    log.wait_for(lambda e: e.data["state"] == "up")
    mark = log.mark()
    port.unplug()
    log.wait_for(lambda e: e.data["state"] == "down", after=mark)
    port.broken = False                                  # plugged back in
    log.wait_for(lambda e: e.data["state"] == "up", after=mark)
    port.feed(b"again\n")
    assert read_until(s, b"again\n").endswith(b"again\n")


def test_console_line_events_split_lines_and_flush_an_idle_prompt(broker, bus, uart):
    port, url = uart
    lines = EventLog(bus, "console.line")
    broker.subscribe(BareSession(consoles={"fpga0": url}), "fpga0")
    port.feed(b"MicroPython v1.22\r\nType help\r\n>>> ")
    prompt = lines.wait_for(lambda e: e.data.get("partial"))
    assert prompt.data == {"name": "fpga0", "text": ">>> ", "partial": True}
    texts = [e.data["text"] for e in lines.events]
    assert texts == ["MicroPython v1.22", "Type help", ">>> "]   # CR stripped, no repeats
    # Negative twin: a complete line is never marked partial.
    assert not any(e.data.get("partial") for e in lines.events[:2])


def test_a_stalled_reader_loses_its_oldest_bytes_and_no_one_else_does(bus, uart):
    port, url = uart
    broker = ConsoleBroker(bus, backoff=(0.05, 0.2), buffer_bytes=64)
    try:
        session = BareSession(consoles={"fpga0": url})
        slow = broker.subscribe(session, "fpga0")
        fast = broker.subscribe(session, "fpga0")
        data = bytes(range(200))
        got = b""
        for i in range(0, len(data), 16):                     # the board trickles; fast keeps up
            piece = data[i:i + 16]
            port.feed(piece)
            got += read_until(fast, piece[-1:])
        assert got == data                                    # nothing lost for a live reader
        tail = read_until(slow, data[-1:])
        assert len(tail) <= 64 and data.endswith(tail)        # the slow one kept the newest
        assert slow.dropped == len(data) - len(tail)
        assert fast.dropped == 0
    finally:
        broker.shutdown()


def test_no_console_adapter_is_unavailable(broker):
    with pytest.raises(UnavailableError) as exc:
        broker.subscribe(BareSession(consoles=None), "uart0")
    assert exc.value.code == ExitCode.UNAVAILABLE
    assert broker.names(BareSession(consoles=None)) == []


def test_broker_without_a_bus_publishes_nothing_and_still_works(uart):
    port, url = uart
    broker = ConsoleBroker(None, backoff=(0.05, 0.2))
    try:
        s = broker.subscribe(BareSession(consoles={"fpga0": url}), "fpga0")
        port.feed(b"ok\n")
        assert read_until(s, b"ok\n") == b"ok\n"
    finally:
        broker.shutdown()


def test_engine_like_object_supplies_the_bus(uart):
    port, url = uart

    class Eng:
        bus = EventBus()

    log = EventLog(Eng.bus, "console.state")
    broker = ConsoleBroker(Eng())
    try:
        broker.subscribe(BareSession(consoles={"fpga0": url}), "fpga0")
        log.wait_for(lambda e: e.data["state"] == "up")
    finally:
        broker.shutdown()


# -- names: the "shell" alias and one opener per port -------------------------------------


def test_shell_is_an_alias_for_lane_2_and_shares_its_one_connection(broker, bus, uart):
    port, url = uart
    log = EventLog(bus, "console.state")
    session = BareSession(consoles={"fpga_uart2": url, "fpga_uart3": "fake://t4-other"})
    assert broker.names(session) == ["fpga_uart2", "fpga_uart3", "shell"]
    via_alias = broker.subscribe(session, "shell")
    direct = broker.subscribe(session, "fpga_uart2")
    port.feed(b"shell> \n")
    assert read_until(via_alias, b"\n") == read_until(direct, b"\n") == b"shell> \n"
    ups = [e for e in log.events if e.data["state"] == "up"]
    assert [e.data["name"] for e in ups] == ["fpga_uart2"]         # ONE opener of the port
    assert broker.state(session.candidate.board_id, "shell") == "up"


def test_negative_twin_no_lane_2_means_no_shell_alias(broker):
    session = BareSession(consoles={"uart0": "tcp://127.0.0.1:1"})
    assert "shell" not in broker.names(session)
    with pytest.raises(AbsentError):
        broker.subscribe(session, "shell")


def test_a_board_serving_its_own_shell_console_is_not_redirected(broker):
    session = BareSession(consoles={"shell": "fake://own-shell", "fpga_uart2": "fake://lane2"})
    assert broker.resolve(session, "shell") == ("shell", "fake://own-shell")


def test_two_names_for_one_url_collapse_to_one_opener(broker):
    session = BareSession(consoles={"b": "fake://same", "a": "fake://same"})
    assert broker.resolve(session, "b") == ("a", "fake://same")
