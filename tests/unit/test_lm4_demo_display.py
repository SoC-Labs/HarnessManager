"""Lane LM4: the demo's Live display (``harness_manager.demo_display``), board-free.

The Linux showcase board mirrors its panel from an in-memory lcd_mirror board. It must speak
the agreed wire (docs/design/LCD_MIRROR.md §6.1, ``core.display_wire``), draw with the
firmware's own font and the panel palette generated from the tokens, and never open a
socket. The bare-metal boards give the MPS3 adapter's reasons. Each check has a twin.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from harness_manager.core import display_wire as w
from harness_manager.core.display import DisplayUnavailable
from harness_manager.demo import DemoEngine
from harness_manager.demo_display import (
    ROLES,
    DemoDisplay,
    DemoLcdPanel,
    DemoLcdStream,
    font,
)
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_V011, SWAP_ROWS_LX
from harness_manager.services.display import DisplayService, DisplayTimings
from harness_manager_mps3.display import NEEDS_LINUX, NO_ENGINE
from tests.fakes.lm1_fake_lcd_mirror import ViewerModel

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def panel(clock=None) -> DemoLcdPanel:
    return DemoLcdPanel(tuple(r.format(who="you@host") for r in SWAP_ROWS_LX),
                        clock=clock or time.monotonic)


def read_all(s: DemoLcdStream, reader: w.MessageReader, timeout: float = 0.3) -> list:
    """Every message the stream has now (reading until it would block ``timeout``)."""
    out = []
    s.settimeout(timeout)
    while True:
        try:
            data = s.recv(65536)
        except TimeoutError:
            return out
        if not data:
            return out
        out += reader.feed(data)


# --- the font and the palette: one source each ------------------------------------------------------


def test_the_font_is_the_firmwares_font8x16():
    src = json.loads((ROOT / "docs/design/clcd/source/font8x16.json").read_text())
    assert [list(g) for g in font()] == src["glyphs"]
    # the twin: one flipped scanline is seen
    other = [list(g) for g in font()]
    other[33][5] ^= 0x10
    assert other != src["glyphs"]


def test_the_panel_roles_are_the_generated_palette():
    roles = json.loads((ROOT / "design/generated/palette.json").read_text())["panel"]["roles"]
    for name, (fg, bg) in ROLES.items():
        assert (fg, bg) == (int(roles[name]["fg565"], 16), int(roles[name]["bg565"], 16)), name
    assert int(roles["err"]["fg565"], 16) not in {fg for fg, _ in ROLES.values()}   # the twin


def test_the_page_draws_its_rows_in_the_font_and_ticks():
    clock = Clock()
    p = panel(clock)
    f0, v0, n0 = p.frame()
    assert len(f0) == w.FRAME_BYTES
    glyphs = font()
    # "M" of "MPS3-LX" at row 0, column 0, in the title role: scanline by scanline
    fg, bg = (v.to_bytes(2, "little") for v in ROLES["title"])
    g = glyphs[ord("M") - 0x20]
    for s in range(16):
        row = f0[s * 640:s * 640 + 16]
        assert row == b"".join(fg if g[s] & (0x80 >> x) else bg for x in range(8))
    assert p.frame() == (f0, v0, n0)                      # the same second: the same picture
    clock.t += 1.0                                        # the uptime ticks, the heartbeat turns
    f1, v1, n1 = p.frame()
    assert (v1, n1) == (v0 + 1, n0 + 1) and f1 != f0
    changed = {t for t in range(w.NTILES) if w.tile_of_frame(f0, t) != w.tile_of_frame(f1, t)}
    assert 1 <= len(changed) <= 6
    p.frozen = True                                       # the twin: frozen, nothing moves
    clock.t += 5.0
    assert p.frame()[0] == f1


# --- the wire -------------------------------------------------------------------------------------


def test_the_stream_speaks_the_wire_hello_key_rate_ping_ack():
    s = DemoLcdStream(panel())
    r = w.MessageReader()
    msgs = read_all(s, r)
    assert len(msgs) == 1 and isinstance(msgs[0], w.DisplayInfo)          # HELLO first
    hello = msgs[0]
    assert hello.mode == "sw" and hello.supported() == "" and hello.extra["clients_max"] == 2
    assert read_all(s, r) == []                           # nothing until KEY (the twin)
    s.sendall(w.rate_msg(99))                             # RATE is clamped, the clamp echoed
    s.sendall(w.ping_msg(7))
    s.sendall(w.key_msg())
    msgs = read_all(s, r)
    assert w.RateEcho(30) in msgs and w.Pong(7) in msgs
    ups = [m for m in msgs if isinstance(m, w.DisplayUpdate)]
    assert ups and ups[0].seq == 1 and ups[0].key_first and ups[-1].key_last
    assert ups[0].regs is not None and ups[0].mode_regs() == w.ModeRegs(0x00, 0x09, 0x05, 0x20)
    assert len(ups) <= w.ACK_WINDOW                        # H1: at most two unacknowledged
    assert ups[0].flags.text_only and not ups[0].flags.blind and ups[0].owner == w.OWNER_HARNESS
    assert ups[-1].snap_last and ups[0].valid == w.ALL_VALID
    tiles = {t.idx for u in ups for t in u.tiles}
    assert tiles == set(range(w.NTILES))


def test_the_stream_sends_only_changes_and_respects_the_ack_window_and_rate_0():
    clock = Clock()
    p = panel(clock)
    s = DemoLcdStream(p)
    r = w.MessageReader()
    read_all(s, r)
    s.sendall(w.rate_msg(30) + w.key_msg())
    ups = [m for m in read_all(s, r) if isinstance(m, w.DisplayUpdate)]
    s.sendall(w.ack_msg(ups[-1].seq))
    time.sleep(0.1)
    assert [m for m in read_all(s, r) if isinstance(m, w.DisplayUpdate)] == []   # nothing changed
    clock.t += 1.0
    ups = [m for m in read_all(s, r) if isinstance(m, w.DisplayUpdate)]
    assert len(ups) == 1 and not ups[0].key and 1 <= len(ups[0].tiles) <= 6
    # without an ACK the board holds: two unacknowledged at most
    seq = ups[0].seq
    for _ in range(3):
        clock.t += 1.0
        read_all(s, r, 0.1)
    assert s.seq - seq <= 1                                # one more, then the window is full
    s.sendall(w.ack_msg(s.seq) + w.rate_msg(0))            # paused: nothing, even on a change
    read_all(s, r, 0.1)
    clock.t += 1.0
    assert [m for m in read_all(s, r) if isinstance(m, w.DisplayUpdate)] == []
    s.close()
    assert s.recv(10) == b""


def test_the_compositor_presents_the_panel_pixel_for_pixel_with_no_socket(monkeypatch):
    real = socket.socket

    def no_socket(*a, **kw):                              # the demo never opens one
        raise AssertionError("the demo display opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    p = panel()
    p.frozen = True
    svc = DisplayService(timings=DisplayTimings(grace_s=0.2))
    try:
        viewer = svc.attach("lx", lambda: DemoLcdStream(p))
        vm = ViewerModel()
        deadline = time.monotonic() + 5
        while vm.messages == 0 and time.monotonic() < deadline:
            vm.pump(viewer)
            time.sleep(0.02)
        frame = p.frame()[0]
        assert vm.keys == 1 and vm.matches(frame, set(range(w.NTILES)))
        assert svc.status("lx")["state"] == "live" and svc.status("lx")["mode"] == "sw"
        assert svc.status("lx")["badges"] == []            # the harness owns it: nothing to say
        viewer.close()
    finally:
        svc.shutdown()
        monkeypatch.setattr(socket, "socket", real)


# --- the showcase's boards -------------------------------------------------------------------------


@pytest.fixture
def showcase(tmp_path):
    eng = DemoEngine(speed=0.02, showcase=True, state_dir=tmp_path / "demo", app_update="")
    try:
        yield eng
    finally:
        eng.close_all()


def open_(eng, bid):
    cand = next(c for c in eng.probe() if c.board_id == bid)
    eng.open(cand)
    return eng.session(bid)


def test_the_linux_board_has_a_live_display_and_the_bare_metal_boards_say_why(showcase):
    lx = open_(showcase, BOARD_LINUX)
    assert isinstance(lx.display, DemoDisplay) and lx.display.display_reason() == ""
    assert "lcd_mirror" in lx.identity().features
    info = showcase.info(BOARD_LINUX)
    assert "display_mirror" in info.capabilities
    stream = lx.display.display_connect()
    assert isinstance(stream, DemoLcdStream)
    stream.close()
    for bid in (BOARD_V011, BOARD_LEASED):                # the twins
        s = open_(showcase, bid)
        assert s.display.display_reason() == NEEDS_LINUX.format(impl="bare-metal")
        with pytest.raises(DisplayUnavailable):
            s.display.display_connect()
        assert "display_mirror" not in showcase.info(bid).capabilities


def test_a_linux_image_without_the_engine_says_so(showcase):
    lx = open_(showcase, BOARD_LINUX)
    feats = [f for f in lx.identity().features if f != "lcd_mirror"]
    board = showcase._board(BOARD_LINUX)
    from dataclasses import replace

    board.identity = replace(board.identity, features=tuple(feats))
    assert lx.display.display_reason() == NO_ENGINE
