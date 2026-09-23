"""L1: the ``tcp://`` serial scheme (a SerialPort over an fpgahub TTY share). Each check has a twin."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from harness_manager.core.errors import ExitCode, UnavailableError, UnreachableError, UsageError
from harness_manager.core.transport import open_serial
from harness_manager.transports import tcp_serial
from harness_manager.transports.tcp_serial import TcpSerialPort, baud_info, mark_share


class Echo:
    """A one-client TCP server that records what arrives, with arrival times."""

    def __init__(self, greeting: bytes = b"") -> None:
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.got: list[tuple[float, bytes]] = []
        self.conn: socket.socket | None = None
        self._greeting = greeting
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self.conn, _ = self.srv.accept()
        if self._greeting:
            self.conn.sendall(self._greeting)
        while True:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.got.append((time.monotonic(), data))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.srv.close()


def test_open_serial_reaches_a_tcp_stream_and_reads_like_pyserial():
    srv = Echo(greeting=b"Cmd> ")
    try:
        port = open_serial(f"tcp://127.0.0.1:{srv.port}")
        deadline = time.monotonic() + 2
        while port.in_waiting < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert port.in_waiting == 5
        assert port.read(5) == b"Cmd> "
        assert port.read(1) == b""                     # the read timeout, not a hang
        port.close()
        port.close()                                    # idempotent
    finally:
        srv.close()


def test_negative_twin_nothing_listening_is_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    with pytest.raises(UnreachableError) as exc:
        open_serial(f"tcp://127.0.0.1:{dead}")
    assert exc.value.code == ExitCode.UNREACHABLE and "share" in exc.value.hint


def test_a_malformed_address_is_a_usage_error():
    with pytest.raises(UsageError):
        open_serial("tcp://127.0.0.1")
    with pytest.raises(UsageError):
        open_serial("tcp://127.0.0.1:99999")


def test_each_write_leaves_at_once_so_paced_characters_stay_apart():
    """The MCC drops characters closer than ~50 ms: one write must be one segment, now."""
    srv = Echo()
    try:
        port = TcpSerialPort("127.0.0.1", srv.port)
        for ch in b"REBOOT":
            port.write(bytes([ch]))
            time.sleep(0.06)
        deadline = time.monotonic() + 2
        while sum(len(d) for _, d in srv.got) < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        chunks = [d for _, d in srv.got]
        assert b"".join(chunks) == b"REBOOT"
        assert all(len(c) == 1 for c in chunks)         # never merged (TCP_NODELAY)
        gaps = [b - a for (a, _), (b, _) in zip(srv.got, srv.got[1:], strict=False)]
        assert min(gaps) > 0.03
        port.close()
    finally:
        srv.close()


def test_the_far_end_closing_is_an_oserror_after_the_buffered_bytes():
    srv = Echo(greeting=b"bye")
    try:
        port = TcpSerialPort("127.0.0.1", srv.port)
        deadline = time.monotonic() + 2
        while port.in_waiting < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        srv.conn.shutdown(socket.SHUT_RDWR)             # type: ignore[union-attr]
        time.sleep(0.05)
        assert port.read(3) == b"bye"                   # what arrived is still delivered
        with pytest.raises(OSError):
            _ = port.in_waiting                          # then it is a drop, like an unplugged port
    finally:
        srv.close()


def test_read_until_stops_at_the_prompt():
    srv = Echo(greeting=b"line one\r\nCmd> tail")
    try:
        port = TcpSerialPort("127.0.0.1", srv.port, read_timeout=1.0)
        assert port.read_until(b"Cmd> ") == b"line one\r\nCmd> "
        port.reset_input_buffer()
        assert port.read(4) == b""                       # the tail was discarded
        port.close()
    finally:
        srv.close()


def test_the_baud_is_the_shares_and_cannot_be_changed():
    srv = Echo()
    try:
        port = TcpSerialPort("127.0.0.1", srv.port, baud=115200)
        assert port.baudrate == 115200 and port.baud_settable is False
        assert "set by the hub share (115200)" in port.baud_reason
        port.baudrate = 115200                          # the same rate is fine
        with pytest.raises(UnavailableError) as exc:
            port.baudrate = 9600
        assert "set by the hub share (115200)" in exc.value.message
        port.close()
    finally:
        srv.close()


def test_baud_info_knows_a_share_and_not_a_shell_console():
    mark_share("tcp://127.0.0.1:12000", 115200)
    info = baud_info("tcp://127.0.0.1:12000")
    assert info is not None and info.baud == 115200 and not info.settable
    assert info.reason.startswith("set by the hub share (115200)")
    # The twin: a shell Ethernet UART console is tcp:// too, and its rate is the design's.
    assert baud_info("tcp://127.0.0.1:6930") is None
    # hub:// is always a share.
    hub = baud_info("hub://mapstone-dev/mps3_01_pl/dev/mps3_01_pl/tty_09")
    assert hub is not None and hub.baud == tcp_serial.DEFAULT_SHARE_BAUD


def test_opening_a_known_share_at_another_rate_is_refused():
    srv = Echo()
    try:
        mark_share(f"tcp://127.0.0.1:{srv.port}", 115200)
        with pytest.raises(UnavailableError):
            open_serial(f"tcp://127.0.0.1:{srv.port}", 9600)
        port = open_serial(f"tcp://127.0.0.1:{srv.port}", 115200)   # the twin
        port.close()
    finally:
        srv.close()


def test_a_read_only_port_refuses_writes_instead_of_losing_them():
    srv = Echo()
    try:
        port = TcpSerialPort("127.0.0.1", srv.port, read_only=True,
                             read_only_reason="another client (10.0.0.7:5) holds the write slot")
        with pytest.raises(OSError) as exc:
            port.write(b"R")
        assert "10.0.0.7:5" in str(exc.value)
        port.close()
        rw = TcpSerialPort("127.0.0.1", Echo().port)      # the twin writes
        assert rw.write(b"R") == 1
        rw.close()
    finally:
        srv.close()
