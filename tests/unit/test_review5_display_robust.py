"""REVIEW-W5 items 6-9: the live display's compositor and wire, against untrusted bytes and a
lease lost mid-connect. Board-free: a scripted in-memory board (no sockets but a
``socketpair`` for the forward), the REAL ``DisplayService`` and ``core.display_wire``.
Every check has a negative twin.

6. a forward opened by a connect that finishes AFTER ``close`` gave up waiting is released;
7. a keyframe (or a SNAP) that never ends is a ``WireError`` (the reconnect path), never a
   stage that grows without bound;
8. HELLO / refusal JSON: deep nesting and ``Infinity`` are ``WireError``s, only ``WireError``
   escapes ``MessageReader.feed``, ``max_msg`` 0/null/"" is invalid (not the default), and
   ``supported`` names a newer proto before its ``max_msg``;
9. the display socket's error-frame queue keeps the last ``NOTES_MAX``.
"""

from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from harness_manager.core import display_wire as w
from harness_manager.services import display as svcmod
from harness_manager.services.display import DisplayService, DisplayTimings


def wait_for(pred: Callable[[], Any], timeout: float = 10.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or pred}")
        time.sleep(0.01)


# --- 6. a forward opened after close() gave up waiting is released -----------------------------


class SlowForward:
    """Mirrors ``Mps3Display``: the connect opens a forward (a slow ``ssh -J``) kept on the
    adapter; ``display_release`` drops it (idempotent)."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.connecting = threading.Event()
        self.forward_open = False
        self.releases = 0
        self.peers: list[socket.socket] = []

    def display_reason(self) -> str:
        return ""

    def display_connect(self) -> socket.socket:
        self.connecting.set()
        self.go.wait(10)                     # ssh -J HUB ... BOARD, slower than close()'s wait
        self.forward_open = True
        a, b = socket.socketpair()
        self.peers.append(b)
        return a

    def display_release(self) -> None:
        self.releases += 1
        self.forward_open = False


def test_a_forward_opened_after_close_gave_up_waiting_is_released() -> None:
    """The lease is lost while the upstream thread is still inside ``display_connect``.
    ``close`` waits ``close_join_s``, releases (nothing open yet) and returns; the connect
    then opens the forward. Before the fix the once-only ``_released`` flag skipped the
    second release and the forward (a way to the board's loopback) outlived the lease."""
    src = SlowForward()
    svc = DisplayService(None, timings=DisplayTimings(grace_s=60, close_join_s=0.1))
    svc.attach("b1", src, ack=False)
    assert src.connecting.wait(5)
    svc.close("b1", "closed: the lease was lost")
    assert svc.status("b1")["state"] == "down"
    src.go.set()                             # the connect finishes after close() returned
    wait_for(lambda: src.releases >= 2 and not src.forward_open, what="the late release")
    assert not src.forward_open
    for p in src.peers:
        assert p.recv(1) == b"", "the late socket was closed"
        p.close()


def test_twin_a_connect_that_finishes_inside_the_wait_is_released_once_it_ends() -> None:
    src = SlowForward()
    svc = DisplayService(None, timings=DisplayTimings(grace_s=60, close_join_s=5.0))
    svc.attach("b1", src, ack=False)
    assert src.connecting.wait(5)
    threading.Timer(0.2, src.go.set).start()
    svc.close("b1", "closed: the lease was lost")
    assert not src.forward_open and src.releases >= 1
    for p in src.peers:
        p.close()


# --- 7. a SNAP that never ends ------------------------------------------------------------------


class FakeStream:
    """A connected stream to a scripted board: HELLO first, PONG for every PING, and
    ``on_key`` (a board thread) for every KEY."""

    def __init__(self, on_key: Callable[[FakeStream], None] | None = None,
                 hello: bytes | None = None) -> None:
        self.q: queue.Queue[bytes] = queue.Queue()
        self.t: float | None = None
        self.sent: list[bytes] = []
        self.on_key = on_key
        self.closed = False
        self.q.put(hello if hello is not None else
                   w.hello_msg(w.DisplayInfo(mode="hw", static_id="0x1")))

    def settimeout(self, t: float | None) -> None:
        self.t = t

    def recv(self, _n: int) -> bytes:
        if self.closed:
            return b""
        try:
            return self.q.get(timeout=self.t)
        except queue.Empty:
            raise TimeoutError from None

    def sendall(self, d: bytes) -> None:
        self.sent.append(d)
        typ = d[2]
        if typ == w.T_PING:
            self.q.put(w.pong_msg(struct.unpack_from("<I", d, 8)[0]))
        elif typ == w.T_KEY and self.on_key is not None:
            threading.Thread(target=self.on_key, args=(self,), daemon=True).start()

    def close(self) -> None:
        self.closed = True
        self.q.put(b"")


T = DisplayTimings(tick_s=0.02, ping_s=0.1, stale_s=5, dead_s=10, key_retry_s=5,
                   backoff_s=(0.1,), hello_timeout_s=2, grace_s=60)
FL = w.ST_BL | w.ST_DISPLAY_ON | w.ST_FMT_OK
RAW = [w.record(t, w.E_RAW, bytes([t & 0xFF]) * 512) for t in range(120)]


def endless_keyframe(parts: int) -> Callable[[FakeStream], None]:
    def board(s: FakeStream) -> None:
        for i in range(parts):                         # never key_last
            st = w.S_KEY | (w.S_KEY_FIRST if i == 0 else 0) | FL
            s.q.put(w.update_msg(i + 1, 0, 0, 0, st, 0, w.ALL_VALID, RAW, regs=bytes(256)))
    return board


def test_a_keyframe_that_never_ends_is_a_wire_error_not_a_growing_stage() -> None:
    """Before the fix: 50 parts of 120 tiles were all staged (3 MB and growing); now the part
    that takes it past 300 records is a protocol error and the upstream reconnects."""
    svc = DisplayService(timings=T)
    streams: list[FakeStream] = []

    def connect() -> FakeStream:
        s = FakeStream(on_key=endless_keyframe(50))
        streams.append(s)
        return s

    svc.attach("b1", connect)
    try:
        wait_for(lambda: svc.status("b1")["counters"]["protocol_errors"] >= 1,
                 what="a protocol error")
        wait_for(lambda: len(streams) >= 2, what="the reconnect")
    finally:
        svc.close_all()
    assert svc.status("b1")["counters"]["keys_presented"] == 0


def test_a_snap_that_never_ends_is_a_wire_error_too() -> None:
    """After one ``snap_last``, delta parts that never carry it again were all kept."""
    def board(s: FakeStream) -> None:
        s.q.put(w.update_msg(1, 0, 0, 0, w.S_KEY_BITS | w.S_SNAP_LAST | FL, 0, w.ALL_VALID,
                             [w.record(0, w.E_FILL, b"\x1f\x00")], regs=bytes(256)))
        for seq in range(2, 12):
            s.q.put(w.update_msg(seq, 0, 0, 0, FL, 0, w.ALL_VALID, RAW, mode=0x00090520))

    svc = DisplayService(timings=T)
    svc.attach("b2", lambda: FakeStream(on_key=board))
    try:
        wait_for(lambda: svc.status("b2")["counters"]["protocol_errors"] >= 1,
                 what="a protocol error")
    finally:
        svc.close_all()


def test_twin_a_keyframe_in_three_parts_of_100_tiles_is_shown() -> None:
    tiles = [w.record(t, w.E_RAW, bytes([t & 0xFF]) * 512) for t in range(w.NTILES)]

    def board(s: FakeStream) -> None:
        for i in range(3):
            st = w.S_KEY | (w.S_KEY_FIRST if i == 0 else 0) | (w.S_KEY_LAST | w.S_SNAP_LAST
                                                               if i == 2 else 0) | FL
            regs = bytes(256) if i == 0 else None
            kw: dict[str, Any] = {"regs": regs} if regs is not None else {"mode": 0x00090520}
            s.q.put(w.update_msg(i + 1, 0, 0, 0, st, 0, w.ALL_VALID,
                                 tiles[i * 100:(i + 1) * 100], **kw))

    svc = DisplayService(timings=T)
    svc.attach("b3", lambda: FakeStream(on_key=board))
    try:
        wait_for(lambda: svc.status("b3")["counters"]["keys_presented"] == 1, what="the key")
        assert svc.status("b3")["counters"]["protocol_errors"] == 0
    finally:
        svc.close_all()
    assert svcmod.MAX_SNAP_RECORDS == w.NTILES


# --- 8. HELLO and the refusal line --------------------------------------------------------------

BASE = {"proto": 1, "w": 320, "h": 240, "fmt": "rgb565le", "tile": 16, "mode": "hw",
        "static_id": "0x1", "max_msg": 65536}


def hello(**over: Any) -> bytes:
    return w.message(w.T_HELLO, json.dumps({**BASE, **over}).encode())


def hello_raw(key: str, literal: str) -> bytes:
    body = json.dumps({**BASE, key: "__X__"}).replace('"__X__"', literal)
    return w.message(w.T_HELLO, body.encode())


@pytest.mark.parametrize("data", [
    hello_raw("max_msg", "Infinity"), hello_raw("max_msg", "-Infinity"),
    hello_raw("proto", "Infinity"), hello_raw("w", "1e400"),
    w.message(w.T_HELLO, ('{"a":' + "[" * 20000 + "]" * 20000 + "}").encode()),
    hello_raw("x", "[" * 5000 + "]" * 5000),
], ids=["max_msg-inf", "max_msg--inf", "proto-inf", "w-1e400", "nested-20000", "nested-key"])
def test_a_hello_the_json_decoder_chokes_on_is_a_wire_error(data: bytes) -> None:
    """Before the fix these escaped as OverflowError / RecursionError: the upstream thread
    died as an "internal error" instead of taking the reconnect path."""
    with pytest.raises(w.WireError):
        w.MessageReader().feed(data)


def test_twin_a_nan_max_msg_was_already_a_wire_error_and_a_good_hello_reads() -> None:
    with pytest.raises(w.WireError):
        w.MessageReader().feed(hello_raw("max_msg", "NaN"))
    (info,) = w.MessageReader().feed(hello())
    assert isinstance(info, w.DisplayInfo) and info.supported() == ""


def test_only_wire_error_escapes_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_typ: int, _body: bytes) -> Any:
        raise struct.error("unpack requires a buffer of 4 bytes")

    monkeypatch.setattr(w, "decode_message", boom)
    with pytest.raises(w.WireError, match="could not be read"):
        w.MessageReader().feed(w.pong_msg(1))


def test_a_deeply_nested_refusal_line_is_a_refusal_with_its_text() -> None:
    line = b'{"err":' + b"[" * 3000 + b"\n"
    (ref,) = w.MessageReader().feed(line)
    assert isinstance(ref, w.Refusal) and ref.line.startswith('{"err":[[')
    r = w.MessageReader()
    assert r.feed(b'{"ok":false,"err":' + b"[" * 3000) == []
    cut = r.eof()
    assert isinstance(cut, w.Refusal)
    # twin: an ordinary refusal still names the board's reason
    (ok,) = w.MessageReader().feed(w.refusal_line("lcd_mirror: busy (2 clients)"))
    assert ok.err == "lcd_mirror: busy (2 clients)"


@pytest.mark.parametrize("literal", ["0", "null", '""', "false"])
def test_a_max_msg_of_nothing_is_invalid_not_the_default(literal: str) -> None:
    (info,) = w.MessageReader().feed(hello_raw("max_msg", literal))
    assert info.max_msg != w.MAX_MSG_DEFAULT
    assert info.supported().startswith("max_msg 0 ")


def test_twin_an_absent_max_msg_is_the_default() -> None:
    body = {k: v for k, v in BASE.items() if k != "max_msg"}
    (info,) = w.MessageReader().feed(w.message(w.T_HELLO, json.dumps(body).encode()))
    assert info.max_msg == w.MAX_MSG_DEFAULT and info.supported() == ""


def test_a_newer_proto_is_named_before_its_max_msg() -> None:
    info = w.parse_hello(json.dumps({**BASE, "proto": 2, "max_msg": 131072}).encode())
    assert info.supported().startswith("lcd_mirror proto 2")
    # twin: proto 1 with an oversized max_msg still says max_msg
    info = w.parse_hello(json.dumps({**BASE, "max_msg": 131072}).encode())
    assert info.supported().startswith("max_msg 131072")


def test_a_hello_with_infinity_takes_the_reconnect_path_in_the_compositor() -> None:
    svc = DisplayService(timings=T)
    n = {"connects": 0}

    def connect() -> FakeStream:
        n["connects"] += 1
        return FakeStream(hello=hello_raw("max_msg", "Infinity"))

    v = svc.attach("b4", connect)
    try:
        wait_for(lambda: n["connects"] >= 2, what="a reconnect")
        assert not v.ended and "internal error" not in svc.status("b4")["reason"]
    finally:
        svc.close_all()


# --- 9. the display socket's error frames --------------------------------------------------------


def test_the_display_socket_keeps_the_last_8_error_frames() -> None:
    """A client that floods bad text frames while the sender cannot send: the queue keeps the
    newest ``NOTES_MAX`` (8). Before the fix it was a list with no bound."""
    from harness_manager.core.errors import UsageError
    from harness_manager.daemon import display_api

    notes = display_api.Notes()
    for i in range(100):
        notes.add(UsageError(f"bad frame {i}"))
    assert len(notes) == display_api.NOTES_MAX == 8
    got = [json.loads(t)["error"]["message"] for t in notes.take()]
    assert got == [f"bad frame {i}" for i in range(92, 100)]
    # twin: fewer than the cap are all kept, in order, and taking empties it
    for i in range(3):
        notes.add(UsageError(f"late {i}"))
    assert [json.loads(t)["error"]["message"] for t in notes.take()] == \
        ["late 0", "late 1", "late 2"]
    assert notes.take() == []
