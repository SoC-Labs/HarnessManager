"""The showcase's Live display: an in-memory lcd_mirror board for ``app --demo`` (lane LM4).

``docs/design/LCD_MIRROR.md`` is the design. The Linux showcase board (mps3-lx) mirrors its
panel: the harness's status page (the rows the text mirror shows, ``SWAP_ROWS_LX``) in the
firmware's own 8x16 font and the panel palette the harness renders with
(``design/generated/palette.json``, generated from ``design/tokens.json``), with the uptime
ticking every second and the heartbeat turning. It comes as the interim software tap sends
it (``mode: sw``: exact for the harness's own screen). The two bare-metal boards say why
they have no live display, in the MPS3 adapter's words (lane LM2, ``NEEDS_LINUX``); the
daemon's routes turn that into 422, or 409 HELD naming alice on the board behind the hub.

Nothing here opens a socket, starts a thread or reaches a board. ``display_connect()``
returns an in-memory byte stream that plays the board's end of the wire through
``core.display_wire``: HELLO first, nothing until KEY, UPDATEs at the asked RATE (the
clamp echoed), at most two unacknowledged (H1), PING answered by PONG, and no UPDATE when
nothing changed (the board's §6.1 correction 4).
"""

from __future__ import annotations

import base64
import threading
import time
import zlib
from typing import Any

from harness_manager.core import display_wire as w
from harness_manager.core.display import DisplayUnavailable

#: The panel font (``firmware/clcd/font8x16.h`` via ``docs/design/clcd/source/font8x16.json``,
#: sha256 83dff3df...): 95 glyphs (0x20-0x7E) x 16 scanlines, bit 7 the leftmost pixel.
#: ``tests/unit/test_lm4_demo_display.py`` checks it against that file.
FONT_Z = (
    "eNp1U7FqG0EQXZZDXBHCRrg4THAOcYQrQhAmxcURYlkWcUUKY1ykSGFMCClcmFSCGGdZhCOMIeYQIbgKKlz4C0xI"
    "IYTRB6RwkxQCQ9QEo9KFuWR2du8sOeQV0r2d2Zl5M7OE3EKCYABCWMK2tqIonbVXq9X3kUGeAsBlPft0uJosVL9n"
    "vTZjdLPX631+w9ij5bW1tQNF6uC1Gj5eX+91D94SMh6H4WAmHPV8nzn4vkcVD8OChyFXxFSRnSfnWcLcFWPKbYFz"
    "mA2dk3/Bx2NOKPUgo2+jc6VIEEXCIYoCiL7NSuzArSASm1hkWBO7pj9Sa+olHtVaJsD9II7TtNvNIWoD+LVS6vRM"
    "Uzq09oZo4YF2/rnWHpbgWwkmHlhkYccv3Vqh1HI7k9m/4iNJAlZHOUYMts+jpXrXAcW5tTnBJv7QXHLzhfr00XG/"
    "3z8+UsLUz+I4loAdqGIE/EpCZ2Tb/Ipr4CstoRBaoP+VlK59Ul6ZvELwNG2nKZzks5xzPgW++FBA2z9CdPF6EfjE"
    "XN21ESbG/6b9zNx/QC2K/vySMk23V9MU8l4An3IHl0+PTk5OsizTrv4PQxAHfH9/f29Y9leX80B9IJC7+hLpHL5K"
    "mfiVQj+ks/mePO8otRF44HF2aurNsrl9mYgbmP6MMBMgjnFjDYdyoEg4A36JFmw61jvSztfFyztdu61CaNC3xOaw"
    "RFQ5YZg+JdN5+5TFUpP/I6/X4fLt04agTaH13jv4HkBf7PShC86+0tK4AEZfBba5ITsGslnsp9R5Yb97z71Wp8fg"
    "GUhMeLuj9Q7Gf7lZzj+AJ0javsUfCF8B3ixWACZm/IVMX7yCmi5mfJ0/Yiuz+OH4z5v4RX3l/MncDkyRN1GPbHje"
    "EvLfy7XQ4LrUL3iD6uE3Q8w5Wmu1+84+sulaTwuu3VAdz3AB7PwJ7gCugLx0fCJq0QI82cMvHPmuNO+3Zva7gkJZ"
    "yKziCruFAZbCwAMx2MjuzI32LxWAQG4="
)
FONT_FIRST, FONT_COUNT = 0x20, 95

#: The panel roles the page uses, as RGB565 (fg, bg): ``design/generated/palette.json``
#: ``panel.roles`` (the test holds them to that file).
ROLES = {
    "title": (0xE73D, 0x1905),
    "rule": (0x31E9, 0x0000),
    "label": (0x7C32, 0x0000),
    "value": (0xE73D, 0x0000),
    "ok": (0x5E31, 0x0000),
    "chrome": (0xAD97, 0x1905),
}

COLS, ROWS = 40, 15
#: The uptime the page starts at (the text mirror's ``UP  : 001:04:12:48``), in seconds.
UPTIME0_S = 1 * 86400 + 4 * 3600 + 12 * 60 + 48
SPINNER = "|/-\\"
STATIC_ID = "0x4c1a0003"


def font() -> list[bytes]:
    raw = zlib.decompress(base64.b64decode(FONT_Z))
    return [raw[i * 16:(i + 1) * 16] for i in range(FONT_COUNT)]


class DemoLcdPanel:
    """The Linux showcase board's panel: its status page, re-rendered once a second."""

    def __init__(self, rows: tuple[str, ...], clock: Any = time.monotonic) -> None:
        self.rows = rows
        self.clock = clock
        self.t0 = clock()
        self.frozen = False               # tests: the picture stops changing
        self.version = 0
        self.frames = 0
        self._lock = threading.Lock()
        self._font = font()
        self._strips: dict[tuple[int, int], list[bytes]] = {}
        self._tick: int | None = None
        self._frame = b""

    def text(self, tick: int) -> list[tuple[str, list[str]]]:
        """The page at ``tick`` seconds: [(text, per-column role)] per row."""
        out = []
        for r, row in enumerate(self.rows):
            text = row.ljust(COLS)[:COLS]
            if text.startswith("UP  :"):
                up = UPTIME0_S + tick
                text = (f"UP  : {up // 86400:03d}:{up // 3600 % 24:02d}:{up // 60 % 60:02d}:"
                        f"{up % 60:02d}").ljust(COLS)
            if text.rstrip().endswith("hb \\"):
                i = text.rindex("hb \\") + 3
                text = text[:i] + SPINNER[tick % len(SPINNER)] + text[i + 1:]
            if r == 0:
                roles = ["title"] * COLS
            elif set(text.strip()) == {"-"}:
                roles = ["rule"] * COLS
            elif r == len(self.rows) - 1:
                roles = ["chrome"] * COLS
            else:
                roles = ["label"] * 5 + ["value"] * (COLS - 5)
                for word in ("UP 100/FD", "VERIFIED", "LOADED"):
                    at = text.find(word)
                    if at >= 0:
                        roles[at:at + len(word)] = ["ok"] * len(word)
            out.append((text, roles))
        return out

    def _strip(self, role: str) -> list[bytes]:
        fg, bg = ROLES[role]
        key = (fg, bg)
        strips = self._strips.get(key)
        if strips is None:
            f, b = fg.to_bytes(2, "little"), bg.to_bytes(2, "little")
            strips = [b"".join(f if bits & (0x80 >> x) else b for x in range(8)) for bits in range(256)]
            self._strips[key] = strips
        return strips

    def render(self, tick: int) -> bytes:
        """The page as a row-major 320x240 RGB565 LE frame (the wire's order)."""
        frame = bytearray(w.FRAME_BYTES)
        stride = w.W * 2
        for r, (text, roles) in enumerate(self.text(tick)):
            cells = []
            for ch, role in zip(text, roles, strict=True):
                code = ord(ch) - FONT_FIRST
                glyph = self._font[code if 0 <= code < FONT_COUNT else 0]
                cells.append((glyph, self._strip(role)))
            for s in range(16):
                o = (r * 16 + s) * stride
                frame[o:o + stride] = b"".join(strips[glyph[s]] for glyph, strips in cells)
        return bytes(frame)

    def frame(self) -> tuple[bytes, int, int]:
        """(the picture now, its version, its frame count): a new version once a second."""
        with self._lock:
            tick = int(self.clock() - self.t0)
            if self._tick is None or (tick != self._tick and not self.frozen):
                self._tick = tick
                self._frame = self.render(tick)
                self.version += 1
                self.frames += 1
            return self._frame, self.version, self.frames


def baseline_regs() -> bytes:
    """REGS as the harness init table leaves them (hx8347_init.c): the calibrated anchor."""
    r = bytearray(w.REGS_SIZE)
    for k, v in {0x01: 0x00, 0x16: 0x20, 0x17: 0x05, 0x1F: 0x90, 0x28: 0x3C, 0x36: 0x09,
                 0x04: 0x01, 0x05: 0x3F, 0x08: 0x00, 0x09: 0xEF}.items():
        r[k] = v
    return bytes(r)


class DemoLcdStream:
    """One in-memory connection to the board's lcd_mirror (``core.display.ByteStream``)."""

    RATE_DEFAULT, RATE_MAX, CLIENTS_MAX = 5, 30, 2

    def __init__(self, panel: DemoLcdPanel, *, boot_id: str = "demo-boot-1") -> None:
        self.panel = panel
        self._cv = threading.Condition()
        info = w.DisplayInfo(mode="sw", static_id=STATIC_ID, max_msg=w.MAX_MSG_DEFAULT,
                             extra={"boot_id": boot_id, "rate": self.RATE_DEFAULT,
                                    "rate_max": self.RATE_MAX, "clients_max": self.CLIENTS_MAX})
        self._out = bytearray(w.hello_msg(info))
        self._in = bytearray()
        self._timeout: float | None = None
        self._closed = False
        self.started = self.key_req = False
        self.rate = self.RATE_DEFAULT
        self.next_snap = 0.0
        self.seq = self.acked = 0          # the first UPDATE is seq 1 (correction 2)
        self.parts: list[tuple[list[bytes], int]] = []
        self.hdr: tuple[int, int, int] = (0, 0, 0)
        self.held: bytes | None = None
        self.held_version = -1
        self.regs = baseline_regs()
        self.status = w.ST_RST_N | w.ST_BL | w.ST_DISPLAY_ON | w.ST_FMT_OK | w.S_TEXT_ONLY

    # -- the socket-like side the compositor uses ----------------------------------------------

    def settimeout(self, t: float | None) -> None:
        self._timeout = t

    def sendall(self, data: bytes) -> None:
        with self._cv:
            if self._closed:
                raise OSError("the demo lcd_mirror stream is closed")
            self._in += data
            self._control()
            self._cv.notify_all()

    def recv(self, n: int) -> bytes:
        deadline = time.monotonic() + (self._timeout if self._timeout is not None else 3600.0)
        with self._cv:
            while True:
                if self._closed:
                    return b""
                self._pump()
                if self._out:
                    chunk = bytes(self._out[:n])
                    del self._out[:n]
                    return chunk
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("timed out")
                if self.started and self.rate and self._room():
                    left = min(left, max(0.005, self.next_snap - time.monotonic()))
                self._cv.wait(left)

    def shutdown(self, _how: int) -> None:
        self.close()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # -- the board's end of the wire -----------------------------------------------------------

    def _room(self) -> bool:
        return ((self.seq - self.acked) & 0xFFFFFFFF) < w.ACK_WINDOW          # H1

    def _control(self) -> None:
        buf = self._in
        while len(buf) >= w.HEADER_SIZE:
            _magic, typ, _r, ln = w.HEADER.unpack_from(buf)
            if len(buf) < w.HEADER_SIZE + ln:
                return
            body = bytes(buf[w.HEADER_SIZE:w.HEADER_SIZE + ln])
            del buf[:w.HEADER_SIZE + ln]
            if typ == w.T_KEY:
                self.key_req = self.started = True
            elif typ == w.T_RATE and ln >= 1:
                self.rate = min(self.RATE_MAX, body[0])
                self.next_snap = 0.0
                self._out += w.message(w.T_RATE, bytes([self.rate]))           # the clamp, at once
            elif typ == w.T_PING and ln >= 4:
                self._out += w.pong_msg(w.U32.unpack_from(body)[0])
            elif typ == w.T_ACK and ln >= 4:
                a = w.U32.unpack_from(body)[0]
                if 0 < ((a - self.acked) & 0xFFFFFFFF) < 0x80000000 and \
                        ((self.seq - a) & 0xFFFFFFFF) < 0x80000000:
                    self.acked = a

    def _pump(self) -> None:
        if not self._room():
            return
        if self.parts:
            self._next_part()
            return
        if not self.started or self.rate == 0:
            return                                              # not asked yet, or paused
        now = time.monotonic()
        if not self.key_req and now < self.next_snap:
            return
        self.next_snap = now + 1.0 / self.rate
        self._snap(now)
        if self.parts:
            self._next_part()

    def _snap(self, now: float) -> None:
        frame, version, frames = self.panel.frame()
        key = self.key_req
        if key:
            tiles = set(range(w.NTILES))
        elif version == self.held_version or self.held is None:
            return                                              # nothing changed: no UPDATE
        else:
            tiles = {t for t in range(w.NTILES)
                     if w.tile_of_frame(frame, t) != w.tile_of_frame(self.held, t)}
            if not tiles:
                self.held_version = version
                return
        recs = w.encode_records(frame, tiles)
        self.held, self.held_version = frame, version
        chunks = w.split_records(recs, w.part_budget(w.MAX_MSG_DEFAULT, key))
        self.parts = []
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            flags = w.S_SNAP_LAST if last else 0
            if key:
                flags |= w.S_KEY | (w.S_KEY_FIRST if i == 0 else 0) | (w.S_KEY_LAST if last else 0)
            self.parts.append((chunk, flags))
        self.hdr = (int(now * 1000) & 0xFFFFFFFF, frames, 0)
        self.key_req = False

    def _next_part(self) -> None:
        chunk, flags = self.parts.pop(0)
        t_ms, frames, resets = self.hdr
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        self._out += w.update_msg(self.seq, t_ms, frames, resets, self.status | flags,
                                  w.OWNER_HARNESS, w.ALL_VALID, chunk, regs=self.regs)


class DemoDisplay:
    """``session.display`` for a showcase board (``core.display.DisplayAdapter``)."""

    def __init__(self, engine: Any, board_id: str) -> None:
        self._e, self._bid = engine, board_id
        self._panel: DemoLcdPanel | None = None
        self._mu = threading.Lock()
        self.connects = 0

    def display_reason(self) -> str:
        """The MPS3 adapter's gate (LM2), in its words: the Linux harness, then the engine."""
        from harness_manager_mps3.display import (
            IMPL_BARE_METAL,
            IMPL_LINUX,
            LCD_MIRROR_FEATURE,
            NEEDS_LINUX,
            NO_ENGINE,
        )

        ident = self._e._board(self._bid).identity
        if ident.harness_impl != IMPL_LINUX:
            return NEEDS_LINUX.format(impl=ident.harness_impl or IMPL_BARE_METAL)
        if LCD_MIRROR_FEATURE not in (ident.features or ()):
            return NO_ENGINE
        return ""

    @property
    def panel(self) -> DemoLcdPanel:
        with self._mu:
            if self._panel is None:
                from harness_manager.demo_showcase import SWAP_ROWS_LX, _who

                who = _who()[:15]
                self._panel = DemoLcdPanel(tuple(r.format(who=who) for r in SWAP_ROWS_LX))
            return self._panel

    def display_connect(self) -> DemoLcdStream:
        why = self.display_reason()
        if why:
            raise DisplayUnavailable(why, retry_s=None)
        self.connects += 1
        return DemoLcdStream(self.panel, boot_id=f"demo-boot-{self.connects}")

    def display_release(self) -> None:
        """Nothing to drop: no forward was opened."""


__all__ = ["DemoDisplay", "DemoLcdPanel", "DemoLcdStream", "FONT_Z", "ROLES", "font"]
