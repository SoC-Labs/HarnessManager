"""Golden pictures for the live display tests (lane LM1).

The ONE panel model is ``Hx8347dShadow`` in ``tests/spikes/lcd_mirror_decoder.py`` (offered
to the Linux lead as LCDMIRROR §8.1's golden model, H2). This module loads it (the spike
sets ``sys.dont_write_bytecode``; that is restored), and builds the pictures the tests
compare against, all from in-tree data:

- the four recorded harness streams (``lcd_mirror_data/*.stream.z``: the exact bytes
  ``clcd.c`` pushes), fed cumulatively into one shadow as the spike does, and their
  independent references: the renderer's 40x15 grid rasterised by ``tools/clcd_mock.py``;
- a DUT picture after a KVM handover: the reset pulse, the harness's own init table (the
  boot stream up to its first ``0x22``: every in-tree writer streams that one table) and
  the nanosoc ``ahb_clcd`` demo's four rectangles, against the rectangles;
- the clcd_demo test card (bars, grey separator, a 16-cell binary counter), re-derived as
  the transport spike's ``Card`` does, as a picture and as its bus stream.

Pictures are row-major 320x240 RGB565 little-endian bytes (the wire's order).
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from array import array
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SPIKE = HERE.parent / "spikes" / "lcd_mirror_decoder.py"
W, H = 320, 240
RS_CMD, RS_DATA, RS_CTRL = 0, 1, 2
HARNESS_STREAMS = ("boot", "link_down", "banner", "regain")


@functools.lru_cache(maxsize=1)
def spike() -> Any:
    """The decoder spike as a module (``Hx8347dShadow``, ``load_stream``, ``grid_reference``)."""
    keep = sys.dont_write_bytecode
    try:
        spec = importlib.util.spec_from_file_location("lm1_lcd_mirror_decoder", SPIKE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["lm1_lcd_mirror_decoder"] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
    return mod


def le_bytes(a: array) -> bytes:
    """An ``array('H')`` of RGB565 values as little-endian bytes (the wire's order)."""
    if sys.byteorder == "big":
        a = array("H", a)
        a.byteswap()
    return a.tobytes()


def shadow() -> Any:
    return spike().Hx8347dShadow()


def viewer_bytes(sh: Any) -> bytes:
    return le_bytes(sh.viewer())


def valid_tiles_of(sh: Any) -> set[int]:
    """The viewer tiles every pixel of which was written since power-on (the model's
    ``known`` map; the recorded streams write every tile of the first paint)."""
    vm = sh._view_map()
    known = sh.known
    bad = {((vi // W) // 16) * 20 + (vi % W) // 16 for vi, gi in enumerate(vm) if not known[gi]}
    return set(range(300)) - bad


@functools.lru_cache(maxsize=1)
def harness_pictures() -> dict[str, tuple[bytes, bytes, bytes]]:
    """``{name: (model picture, grid reference, the model's 256 registers)}`` for the four
    recorded harness streams, fed cumulatively (boot -> link_down -> banner, then a KVM
    reset and the regain) as the decoder spike feeds them."""
    sp = spike()
    sh = sp.Hx8347dShadow()
    out: dict[str, tuple[bytes, bytes, bytes]] = {}
    for name in HARNESS_STREAMS:
        stream = sp.load_stream(name)
        if name == "regain":
            stream = sp.ctrl_reset_pulse() + stream
        sh.feed(stream)
        out[name] = (viewer_bytes(sh), le_bytes(sp.grid_reference(name)), bytes(sh.regs))
    return out


@functools.lru_cache(maxsize=1)
def boot_stream() -> list[tuple[int, int]]:
    return spike().load_stream("boot")


def init_table() -> list[tuple[int, int]]:
    """The harness's init table as it goes on the bus: the boot stream after its reset
    CTRL records, up to (not including) the first ``0x22``."""
    s = boot_stream()
    start = next(i for i, (rs, _b) in enumerate(s) if rs != RS_CTRL)
    end = next(i for i, rec in enumerate(s) if rec == (RS_CMD, 0x22))
    return list(s[start:end])


def rgb(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


NANOSOC_RECTS = ((0, 0, W, H, rgb(0, 0, 40)), (40, 60, 60, 120, rgb(255, 0, 0)),
                 (130, 60, 60, 120, rgb(0, 255, 0)), (220, 60, 60, 120, rgb(0, 0, 255)))


def rect_stream(rects: Any) -> list[tuple[int, int]]:
    """``fill_rect`` as ``ahb_clcd.c`` does it: the window (0x02-0x09), 0x22, pixels high byte first."""
    s: list[tuple[int, int]] = []
    for x, y, w, h, c in rects:
        x1, y1 = x + w - 1, y + h - 1
        for reg, val in ((2, x >> 8), (3, x & 0xFF), (4, x1 >> 8), (5, x1 & 0xFF),
                         (6, y >> 8), (7, y & 0xFF), (8, y1 >> 8), (9, y1 & 0xFF)):
            s += [(RS_CMD, reg), (RS_DATA, val)]
        s.append((RS_CMD, 0x22))
        s += [(RS_DATA, c >> 8), (RS_DATA, c & 0xFF)] * (w * h)
    return s


def rect_picture(rects: Any) -> bytes:
    a = array("H", bytes(W * H * 2))
    for x, y, w, h, c in rects:
        for yy in range(y, y + h):
            a[yy * W + x:yy * W + x + w] = array("H", [c]) * w
    return le_bytes(a)


@functools.lru_cache(maxsize=1)
def nanosoc_after_handover() -> tuple[bytes, bytes, bytes]:
    """(model picture, the rectangles, registers): a KVM reset, the init table, the demo."""
    sp = spike()
    sh = sp.Hx8347dShadow()
    sh.feed(boot_stream())                       # the harness had the panel
    sh.feed(sp.ctrl_reset_pulse() + init_table() + rect_stream(NANOSOC_RECTS))
    return viewer_bytes(sh), rect_picture(NANOSOC_RECTS), bytes(sh.regs)


# --- the clcd_demo test card (tests/clcd_demo/card_model.py, re-derived as the spike's Card) ---

CARD_BARS = (0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000)


def card_pixel(x: int, y: int, counter: int) -> int:
    b, strip, gut = 4, (H * 3) // 4, 2
    sep = strip - 4
    if x < b or x >= W - b or y < b or y >= H - b:
        return 0xFFFF
    if y >= strip:
        k = min(15, (x * 16) // W)
        lo, hi = (W * k) // 16, (W * (k + 1)) // 16
        if x < lo + gut or x >= hi - gut:
            return 0x4208
        return 0xFFFF if (counter >> (15 - k)) & 1 else 0x0000
    if y >= sep:
        return 0x4208
    return CARD_BARS[min(7, (x * 8) // W)]


@functools.lru_cache(maxsize=64)
def card_picture(counter: int) -> bytes:
    return le_bytes(array("H", (card_pixel(x, y, counter) for y in range(H) for x in range(W))))
