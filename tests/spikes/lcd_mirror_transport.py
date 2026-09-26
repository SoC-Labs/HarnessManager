"""LCD-mirror transport spike (team LCD-MIRROR, lane LCDM-HM, 2026-09-26). Board-free, hub-free.

SPIKE ONLY: it lives under ``tests/spikes/`` (as ``lcd_mirror_transport.py``), whose conftest keeps
pytest out (``collect_ignore_glob = ["*"]``; the name also misses ``python_files``), so it never
runs in ``make check`` and never ships in the wheel.

The wire is the Linux lead's ``docs/planning/linux_lanes/LCD_MIRROR_FPGA.md`` §6.2 (lx 5607f11) with
the HM lead's six amendments, in the byte-level reading the LCD-MIRROR lead gave (little-endian):

    frame   'L' 'M' u8 type, u8 rsvd=0, u32 len, then len bytes. A first byte '{' = a refusal:
            one JSON line {"ok":false,"err":"lcd_mirror: ..."}\\n, then close.
    0x01 HELLO   board->HM, first: JSON {proto:1, w, h, fmt:"rgb565le", tile:16, mode, static_id, max_msg}
    0x02 UPDATE  board->HM: u32 seq, u32 t_ms, u32 frames, u32 resets, u32 status, u8 owner,
                 u8 valid[38]; status.key_first ? u8 regs[256] : u32 mode; u16 ntiles;
                 ntiles x {u16 idx, u8 enc, u16 len, payload}. enc 0 FILL 2 B, 1 PAL1 36 B,
                 2 PAL2 72 B, 3 RLE16 (PackBits over u16), 4 RAW 512 B; the board picks the smallest.
    0x10 KEY  0x11 RATE u8 (echoed clamped)  0x12 PING u32 / 0x13 PONG u32      (HM->board, echo)
    0x14 ACK u32 seq   PROPOSAL (not in §6.2): HM->board; the board keeps <= N UPDATEs unacked.

Everything runs on 127.0.0.1:

    fake board (mps3-lcdmirror as §6.1 describes it: SNAP -> dirty tiles -> skip unchanged ->
        |  encode -> UPDATEs <= max_msg; <= 2 clients, the 3rd refused; KEY/RATE/PING; a seq gap
        |  can be injected) over three content patterns at 320x240 RGB565
        ^  127.0.0.1:<board>        (optional) Throttle: a link cap + ONE SSH channel window (2 MiB)
    tests.fakes.l1_fake_ssh.FakeSsh  (the tunnel tests' fake ssh: real local forwarders)
        ^  argv built by the REAL harness_manager_mps3.tunnel.SshTunnel in the claim.open_forward
        |  shape: ssh -J HUB -l root <pinned> -L 127.0.0.1:p:127.0.0.1:6940 BOARD
    HM: Upstream reader (decodes all 5 encodings; keyframes staged and presented atomically on
        |  key_last; KEY on start and after a seq gap; PING/PONG) -> Compositor (one framebuffer +
        |  the latest encoded record per tile; per-viewer dirty tiles, drop-to-latest)
        v  FastAPI/uvicorn WS /api/v1/boards/{bid}/display/ws (the same UPDATE layout, tile records
           forwarded as the board encoded them) and GET .../display.png
    browser stand-in: decodes, applies, and after EVERY message checks CRC(its picture) == the
        board's CRC at that seq; plus (node, when present) the same decoder in JavaScript.

Run (from anywhere; it finds the HM tree next to it, or $HM_ROOT):

    nice -n 10 <hm>/.venv/bin/python tests/spikes/lcd_mirror_transport.py [--quick] [--json out.json]

Scratch: $LCDM_SPIKE_SCRATCH or /tmp/lcdmirror-spike-<pid>, removed at the end (--keep keeps it).
HARNESS_MANAGER_STATE_DIR points into it, so the tunnel's orphan records never touch the user's.
Never touches ~/.ssh, ~/.config/harness-manager, a hub or a board; binds only ephemeral
127.0.0.1 ports and refuses any in 23300-23727 or 10000-19999.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True           # never write __pycache__ into the HM tree

import argparse  # noqa: E402
import array  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import ctypes  # noqa: E402
import functools  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import select  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import zlib  # noqa: E402
from collections import deque  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

HERE = Path(__file__).resolve()


def _hm_root() -> Path:
    env = os.environ.get("HM_ROOT")
    if env:
        return Path(env)
    if HERE.parent.name == "spikes" and HERE.parent.parent.name == "tests":
        return HERE.parents[2]
    return Path("/home/dam1n19/SoCLabs/hm-lcd-mirror")


HM_ROOT = _hm_root()
for _p in (HM_ROOT / "src", HM_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCRATCH = Path(os.environ.get("LCDM_SPIKE_SCRATCH") or f"/tmp/lcdmirror-spike-{os.getpid()}")
os.environ["HARNESS_MANAGER_STATE_DIR"] = str(SCRATCH / "hm-state")     # never the user's

import uvicorn  # noqa: E402
from fastapi import (  # noqa: E402  (module level: FastAPI resolves string annotations here)
    FastAPI,
    WebSocket,
)
from fastapi.responses import Response  # noqa: E402

from harness_manager_mps3 import tunnel as T  # noqa: E402
from tests.fakes.l1_fake_ssh import FakeSsh  # noqa: E402

W, H, TILE = 320, 240, 16
NPIX, FB_BYTES = W * H, W * H * 2
NTX, NTY = W // TILE, H // TILE           # 20 x 15
NT = NTX * NTY                            # 300 tiles
ALL_TILES = frozenset(range(NT))
LCDM_BOARD_PORT = 6940                    # §6.2: the board's loopback
BOARD, HUB = "mps3-01-board.spike", "hub.spike"
FORBIDDEN = ((23300, 23727), (10000, 19999))

# --- the wire (§6.2 + amendments, byte-level reading) -----------------------------------------

HDR = struct.Struct("<2sBBI")             # 'L','M', type, rsvd, len
UPD = struct.Struct("<IIIIIB38s")         # seq, t_ms, frames, resets, status, owner, valid[38] (59 B)
TREC = struct.Struct("<HBH")              # idx, enc, len                                     (5 B)
T_HELLO, T_UPDATE, T_KEY, T_RATE, T_PING, T_PONG, T_ACK = 0x01, 0x02, 0x10, 0x11, 0x12, 0x13, 0x14
E_FILL, E_PAL1, E_PAL2, E_RLE16, E_RAW = 0, 1, 2, 3, 4
ENC_NAMES = ("FILL", "PAL1", "PAL2", "RLE16", "RAW")
S_RST_N, S_BL, S_OWNER, S_DISP, S_STANDBY, S_IN_GRAM, S_FMT_OK, S_APPROX = (1 << i for i in range(8))
S_VIOL, S_OOB, S_RD = 1 << 8, 1 << 9, 1 << 10
S_EXACT, S_TEXT_ONLY, S_BLIND = 1 << 16, 1 << 17, 1 << 18
S_KEY, S_KEY_FIRST, S_KEY_LAST = 1 << 24, 1 << 25, 1 << 26
KEY_BITS = S_KEY | S_KEY_FIRST | S_KEY_LAST
OWNER_HARNESS, OWNER_DUT, OWNER_UNKNOWN = 0, 1, 3
MAX_MSG = 65536                           # amendment 5: max_msg <= 64 KiB (the whole message here)
RATE_MAX, RATE_DEFAULT = 20, 5            # the panel's own ceiling (clcd.h: 20 fps); §6.1 default 5 Hz


def msg(typ: int, body: bytes = b"") -> bytes:
    return HDR.pack(b"LM", typ, 0, len(body)) + body


def valid_bytes(tiles: set[int] | frozenset[int]) -> bytes:
    b = bytearray(38)
    for t in tiles:
        b[t >> 3] |= 1 << (t & 7)
    return bytes(b)


def valid_set(b: bytes) -> set[int]:
    return {t for t in range(NT) if b[t >> 3] >> (t & 7) & 1}


def baseline_regs() -> bytes:
    """REGS as the harness init table leaves them (hx8347_init.c; LCD_MIRROR_FPGA.md §1.4)."""
    r = bytearray(256)
    for k, v in {0x01: 0x00, 0x16: 0x20, 0x17: 0x05, 0x1F: 0x90, 0x28: 0x3C, 0x36: 0x09,
                 0x04: 0x01, 0x05: 0x3F, 0x08: 0x00, 0x09: 0xEF}.items():
        r[k] = v
    return bytes(r)


def mode_word(regs: bytes) -> int:
    return (regs[0x01] << 24) | (regs[0x36] << 16) | (regs[0x17] << 8) | regs[0x16]


def check_port(port: int, what: str) -> int:
    for lo, hi in FORBIDDEN:
        if lo <= port <= hi:
            raise SystemExit(f"REFUSED: {what} port {port} is in the forbidden range {lo}-{hi}")
    return port


BOUND: list[tuple[str, int]] = []


def bind_ephemeral(what: str) -> socket.socket:
    """127.0.0.1:0, re-picked while the kernel's choice is in a forbidden range, then asserted."""
    for _ in range(32):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        if not any(lo <= port <= hi for lo, hi in FORBIDDEN):
            BOUND.append((what, check_port(port, what)))
            return s
        s.close()
    raise SystemExit("no ephemeral port outside the forbidden ranges")


def pick_port(what: str) -> int:
    s = bind_ephemeral(what)
    port = s.getsockname()[1]
    s.close()
    return port


# --- pixels ----------------------------------------------------------------------------------

_PIX2 = [struct.pack("<H", v) for v in range(65536)]


def _rgb8(v: int) -> tuple[int, int, int]:
    r5, g6, b5 = (v >> 11) & 31, (v >> 5) & 63, v & 31    # bit replication (tools/gen_tokens.py)
    return (r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)


_RGB3 = [bytes(_rgb8(v)) for v in range(65536)]


def png_of(fb565: bytes) -> bytes:
    a = array.array("H")
    a.frombytes(fb565)
    rows = [b"\x00" + b"".join(map(_RGB3.__getitem__, a[y * W:(y + 1) * W])) for y in range(H)]

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def png_pixels(png: bytes) -> bytes:
    i, idat = 8, b""
    while i < len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        if png[i + 4:i + 8] == b"IDAT":
            idat += png[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    stride = 1 + W * 3
    return b"".join(raw[y * stride + 1:(y + 1) * stride] for y in range(H))


def tile_px(fb: bytes | bytearray, t: int) -> bytes:
    x0, y0 = (t % NTX) * TILE, (t // NTX) * TILE
    return b"".join(fb[((y0 + r) * W + x0) * 2:((y0 + r) * W + x0 + TILE) * 2] for r in range(TILE))


def put_tile(fb: bytearray, t: int, px: bytes) -> None:
    x0, y0 = (t % NTX) * TILE, (t // NTX) * TILE
    for r in range(TILE):
        o = ((y0 + r) * W + x0) * 2
        fb[o:o + 32] = px[r * 32:(r + 1) * 32]


def crc_valid(fb: bytes | bytearray, valid: set[int] | frozenset[int]) -> int:
    """CRC over the valid tiles only (an invalid tile's pixels are unknown by definition)."""
    if len(valid) == NT:
        return zlib.crc32(fb)
    c = 0
    for t in sorted(valid):
        c = zlib.crc32(tile_px(fb, t), c)
    return c


# --- tile encoders: C (as harnessd would run them) with a Python twin ------------------------------

C_SRC = r"""
#include <stdint.h>
#include <string.h>
static int rle_tile(const uint16_t *p, int n, uint8_t *o) {
  int i = 0, len = 0;
  while (i < n) {
    int j = i + 1;
    while (j < n && p[j] == p[i] && j - i < 128) j++;
    if (j - i >= 2) { o[len++] = (uint8_t)(0x80 | (j - i - 1)); memcpy(o + len, &p[i], 2); len += 2; i = j; }
    else {
      int s = i, k = i;
      while (k < n && k - s < 128) { if (k + 1 < n && p[k + 1] == p[k]) break; k++; }
      if (k == s) k = s + 1;
      o[len++] = (uint8_t)(k - s - 1); memcpy(o + len, &p[s], 2 * (k - s)); len += 2 * (k - s); i = k;
    }
  }
  return len;
}
int enc_tile(const uint16_t *p, uint8_t *out, int *enc) {
  uint16_t pal[4]; int np = 0;
  for (int i = 0; i < 256; i++) {
    uint16_t v = p[i]; int k = 0;
    while (k < np && pal[k] != v) k++;
    if (k == np) { if (np == 4) { np = 5; break; } pal[np++] = v; }
  }
  if (np == 1) { *enc = 0; memcpy(out, pal, 2); return 2; }
  uint8_t tmp[600]; int best = 512; *enc = 4; memcpy(out, p, 512);
  int rl = rle_tile(p, 256, tmp);
  if (rl < best) { best = rl; *enc = 3; memcpy(out, tmp, rl); }
  if (np == 2 && 36 < best) {
    *enc = 1; memcpy(out, pal, 4);
    for (int y = 0; y < 16; y++) { uint16_t row = 0;
      for (int x = 0; x < 16; x++) if (p[y * 16 + x] == pal[1]) row |= (uint16_t)(1u << x);
      memcpy(out + 4 + 2 * y, &row, 2); }
    best = 36;
  } else if (np >= 3 && np <= 4 && 72 < best) {
    while (np < 4) pal[np++] = pal[0];
    *enc = 2; memcpy(out, pal, 8);
    for (int y = 0; y < 16; y++) { uint32_t row = 0;
      for (int x = 0; x < 16; x++) { uint16_t v = p[y * 16 + x]; uint32_t k = 0; while (pal[k] != v) k++; row |= k << (2 * x); }
      memcpy(out + 8 + 4 * y, &row, 4); }
    best = 72;
  }
  return best;
}
int enc_frame(const uint8_t *fb, const uint8_t *mask, uint8_t *out) {
  uint16_t tile[256]; int o = 0;
  for (int t = 0; t < 300; t++) if (mask[t]) {
    int tx = t % 20, ty = t / 20, enc;
    for (int r = 0; r < 16; r++) memcpy(tile + r * 16, fb + (((ty * 16 + r) * 320) + tx * 16) * 2, 32);
    int n = enc_tile(tile, out + o + 5, &enc);
    out[o] = t & 0xff; out[o + 1] = t >> 8; out[o + 2] = (uint8_t)enc; out[o + 3] = n & 0xff; out[o + 4] = n >> 8;
    o += 5 + n;
  }
  return o;
}
int tile_diff(const uint8_t *a, const uint8_t *b, uint8_t *out) {
  int n = 0;
  for (int t = 0; t < 300; t++) { int tx = t % 20, ty = t / 20, d = 0;
    for (int y = ty * 16; y < ty * 16 + 16 && !d; y++)
      d = memcmp(a + ((y * 320) + tx * 16) * 2, b + ((y * 320) + tx * 16) * 2, 32) != 0;
    out[t] = (uint8_t)d; n += d; }
  return n;
}
"""


def _rle_py(a: array.array) -> bytes:
    out, i, n = bytearray(), 0, len(a)
    while i < n:
        j = i + 1
        while j < n and a[j] == a[i] and j - i < 128:
            j += 1
        if j - i >= 2:
            out.append(0x80 | (j - i - 1))
            out += _PIX2[a[i]]
            i = j
        else:
            s = k = i
            while k < n and k - s < 128:
                if k + 1 < n and a[k + 1] == a[k]:
                    break
                k += 1
            if k == s:
                k = s + 1
            out.append(k - s - 1)
            out += a[s:k].tobytes()
            i = k
    return bytes(out)


def enc_tile_py(px: bytes) -> tuple[int, bytes]:
    a = array.array("H")
    a.frombytes(px)
    pal: list[int] = []
    for v in a:
        if v not in pal:
            pal.append(v)
            if len(pal) > 4:
                break
    if len(pal) == 1:
        return E_FILL, _PIX2[pal[0]]
    best: tuple[int, bytes] = (E_RAW, px)
    r = _rle_py(a)
    if len(r) < 512:
        best = (E_RLE16, r)
    if len(pal) == 2 and 36 < len(best[1]):
        rows = [sum(1 << x for x in range(16) if a[y * 16 + x] == pal[1]) for y in range(16)]
        best = (E_PAL1, struct.pack("<2H16H", *pal, *rows))
    elif 3 <= len(pal) <= 4 and 72 < len(best[1]):
        pal4 = pal + [pal[0]] * (4 - len(pal))
        rows = [sum(pal4.index(a[y * 16 + x]) << (2 * x) for x in range(16)) for y in range(16)]
        best = (E_PAL2, struct.pack("<4H16I", *pal4, *rows))
    return best


class Native:
    """The board's encoder and tile diff in C (host gcc -O2), else the Python twin (flagged)."""

    def __init__(self, scratch: Path) -> None:
        self.lib, self.why = None, ""
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            self.why = "no C compiler: Python encoder (host numbers NOT representative of C)"
            return
        scratch.mkdir(parents=True, exist_ok=True)
        src, so = scratch / "lcdm_native.c", scratch / "lcdm_native.so"
        src.write_text(C_SRC)
        r = subprocess.run([cc, "-O2", "-shared", "-fPIC", "-o", str(so), str(src)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.why = f"cc failed ({r.stderr.strip()[:160]}): Python encoder"
            return
        lib = ctypes.CDLL(str(so))
        lib.enc_frame.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.enc_frame.restype = ctypes.c_int
        lib.tile_diff.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.tile_diff.restype = ctypes.c_int
        self.lib = lib
        self._out = ctypes.create_string_buffer(NT * 517 + 64)
        self._bm = ctypes.create_string_buffer(NT)
        self._mu = threading.Lock()            # one output buffer: two board connections share it

    @property
    def kind(self) -> str:
        return "C -O2 (host)" if self.lib else "Python (host)"

    def enc_frame(self, fb: bytes, tiles: set[int] | frozenset[int]) -> bytes:
        """Tile records {u16 idx, u8 enc, u16 len, payload} for ``tiles``, in index order."""
        if self.lib is None:
            return enc_frame_py(fb, tiles)
        mask = bytes(1 if t in tiles else 0 for t in range(NT))
        with self._mu:
            n = self.lib.enc_frame(fb, mask, self._out)
            return self._out.raw[:n]

    def tile_diff(self, a: bytes, b: bytes) -> set[int]:
        if self.lib is None:
            return {t for t in range(NT) if tile_px(a, t) != tile_px(b, t)}
        with self._mu:
            self.lib.tile_diff(a, b, self._bm)
            bm = self._bm.raw
        return {i for i in range(NT) if bm[i]}


def enc_frame_py(fb: bytes, tiles: set[int] | frozenset[int]) -> bytes:
    out = []
    for t in sorted(tiles):
        e, p = enc_tile_py(tile_px(fb, t))
        out.append(TREC.pack(t, e, len(p)) + p)
    return b"".join(out)


def split_records(recs: bytes, budget: int) -> list[tuple[int, bytes]]:
    """Cut a record stream into chunks of at most ``budget`` bytes: [(ntiles, bytes)]."""
    out, i, start, n = [], 0, 0, 0
    while i < len(recs):
        _t, _e, ln = TREC.unpack_from(recs, i)
        size = TREC.size + ln
        if i + size - start > budget and n:
            out.append((n, recs[start:i]))
            start, n = i, 0
        i += size
        n += 1
    if n or not out:
        out.append((n, recs[start:i]))
    return out


# --- HM's decoder (Python, as the daemon would run it) -----------------------------------------


@functools.lru_cache(maxsize=8192)
def _pal1_row(c0: bytes, c1: bytes, bits: int) -> bytes:
    return b"".join(c1 if bits >> x & 1 else c0 for x in range(16))


@functools.lru_cache(maxsize=8192)
def _pal2_row(pal: bytes, bits: int) -> bytes:
    return b"".join(pal[2 * (bits >> (2 * x) & 3):2 * (bits >> (2 * x) & 3) + 2] for x in range(16))


_ROWS16 = struct.Struct("<16H")
_ROWS32 = struct.Struct("<16I")


def decode_tile(enc: int, p: bytes) -> bytes:
    if enc == E_FILL:
        if len(p) != 2:
            raise ValueError("FILL is 2 bytes")
        return p * 256
    if enc == E_PAL1:
        c0, c1 = p[0:2], p[2:4]
        return b"".join(_pal1_row(c0, c1, r) for r in _ROWS16.unpack_from(p, 4))
    if enc == E_PAL2:
        pal = p[0:8]
        return b"".join(_pal2_row(pal, r) for r in _ROWS32.unpack_from(p, 8))
    if enc == E_RLE16:
        out, i = bytearray(), 0
        while i < len(p):
            t = p[i]
            i += 1
            if t & 0x80:
                out += p[i:i + 2] * ((t & 0x7F) + 1)
                i += 2
            else:
                out += p[i:i + 2 * (t + 1)]
                i += 2 * (t + 1)
        if len(out) != 512:
            raise ValueError(f"RLE16 gave {len(out)} bytes, not 512")
        return bytes(out)
    if enc == E_RAW:
        if len(p) != 512:
            raise ValueError("RAW is 512 bytes")
        return p
    raise ValueError(f"unknown tile encoding {enc}")


def parse_update(body: bytes) -> dict[str, Any]:
    seq, t_ms, frames, resets, status, owner, valid = UPD.unpack_from(body)
    off = UPD.size
    regs, mode = None, None
    if status & S_KEY_FIRST:
        regs = body[off:off + 256]
        off += 256
    else:
        mode = struct.unpack_from("<I", body, off)[0]
        off += 4
    (ntiles,) = struct.unpack_from("<H", body, off)
    off += 2
    tiles = []
    for _ in range(ntiles):
        t, e, ln = TREC.unpack_from(body, off)
        off += TREC.size
        if t >= NT:
            raise ValueError(f"tile index {t} out of range")
        tiles.append((t, e, body[off:off + ln]))
        off += ln
    if off != len(body):
        raise ValueError(f"UPDATE has {len(body) - off} trailing bytes")
    return {"seq": seq, "t_ms": t_ms, "frames": frames, "resets": resets, "status": status,
            "owner": owner, "valid": valid, "regs": regs, "mode": mode, "tiles": tiles}


def build_update(seq: int, t_ms: int, frames: int, resets: int, status: int, owner: int,
                 valid: bytes, regs: bytes, ntiles: int, recs: bytes) -> bytes:
    """REGS ride on key_first; every other UPDATE carries MODE."""
    head = UPD.pack(seq & 0xFFFFFFFF, t_ms & 0xFFFFFFFF, frames, resets, status, owner, valid)
    mid = regs if status & S_KEY_FIRST else struct.pack("<I", mode_word(regs))
    return msg(T_UPDATE, head + mid + struct.pack("<H", ntiles) + recs)


def badges(status: int, owner: int, mode: str, regs: bytes | None, mword: int | None) -> list[str]:
    """What the Front panel card says about a picture (LCD_MIRROR_FPGA.md §10 "Requests to HM")."""
    out = []
    if owner == OWNER_DUT:
        out.append("DUT owns the panel")
    if status & S_BLIND or (mode == "sw" and owner == OWNER_DUT):
        out.append("grey: this image cannot see the DUT's picture")
    if status & S_VIOL:
        out.append("bus timing violations: the glass may differ")
    if status & S_APPROX:
        out.append("18-bit colour, shown as 16-bit")
    if not status & S_FMT_OK:
        out.append("unknown pixel format")
    if not status & S_BL:
        out.append("backlight off")
    if not status & S_DISP:
        out.append("display off")
    if status & S_STANDBY:
        out.append("panel in standby")
    if mword is not None:
        r01, r36, r16 = (mword >> 24) & 255, (mword >> 16) & 255, mword & 255
    else:
        r01, r36, r16 = (regs[0x01], regs[0x36], regs[0x16]) if regs else (0, 0x09, 0x20)
    if r36 != 0x09 or r01 != 0x00 or r16 not in (0x20, 0xE0):
        out.append(f"not mirrored exactly: R36=0x{r36:02X} R01=0x{r01:02X} R16=0x{r16:02X}")
    if regs is not None and any(regs[0x0A:0x16]):
        out.append("scroll/partial registers set")
    if not status & S_EXACT:
        out.append("not exact")
    return out


# --- the three content patterns (the board's picture, viewer orientation) ----------------------


class StatusScreen:
    """(a) the harness status screen: real clcd_preview frames (tools/clcd_mock.py), a 250 ms
    reformat (firmware/clcd/clcd.h CLCD_REFRESH_MS: uptime + heartbeat cells in steady state),
    a page change at 0.5 s then every 5 s, cells drawn at a bounded rate (256 B/pass; ~470 cells/s)."""

    name, owner, mode = "a_status", OWNER_HARNESS, "sw"
    TICK_S, PAGE_S, CELLS_PER_S = 0.25, 5.0, 470.0
    SPIN = "/-\\|"
    _fixtures: tuple[list[Any], dict[str, list[int]]] | None = None

    @classmethod
    def fixtures(cls) -> tuple[list[Any], dict[str, list[int]]]:
        if cls._fixtures is None:
            spec = importlib.util.spec_from_file_location("clcd_mock_lcdm", HM_ROOT / "tools" / "clcd_mock.py")
            mock = importlib.util.module_from_spec(spec)
            sys.modules["clcd_mock_lcdm"] = mock
            spec.loader.exec_module(mock)
            text = (HM_ROOT / "docs/design/clcd/source/preview_lx_feat-linux-harness.txt").read_text()
            pages = [(f.text(), [("inv" in row) for row in f.roles])
                     for f in mock.preview_scenarios(text).values()]
            cls._fixtures = (pages, mock.load_font())
        return cls._fixtures

    def __init__(self) -> None:
        self.pages, self.font = self.fixtures()
        self.gram = bytearray(FB_BYTES)
        self.shown = [[" "] * 40 for _ in range(15)]
        self.shown_inv = [[False] * 40 for _ in range(15)]
        self.glyphs: dict[tuple[str, bool], list[bytes]] = {}
        self.page, self.ticks, self.t0, self.last = 0, 0, None, None
        self.queue: deque[tuple[int, int]] = deque()
        self.budget, self.frozen, self.frames, self.resets = 0.0, False, 0, 0
        self.valid: set[int] = set(ALL_TILES)
        self.want = self._compose()                     # the first paint, at once
        self._enqueue()
        while self.queue:
            r, c = self.queue.popleft()
            self._draw(r, c, *self._cell(r, c))

    def _compose(self) -> tuple[list[str], list[bool]]:
        rows, inv = self.pages[self.page % len(self.pages)]
        rows = [list(r.ljust(40)[:40]) for r in rows]
        up = self.ticks // 4
        s = f"{up // 86400:03d}:{up // 3600 % 24:02d}:{up // 60 % 60:02d}:{up % 60:02d}"
        rows[6][6:6 + len(s)] = s
        k = "".join(rows[14]).find("hb ")
        if k >= 0:
            rows[14][k + 3] = self.SPIN[self.ticks % 4]
        return ["".join(r) for r in rows], [bool(x) for x in inv]

    def _cell(self, r: int, c: int) -> tuple[str, bool]:
        rows, inv = self.want
        return rows[r][c], inv[r]

    def _enqueue(self) -> None:
        rows, inv = self.want
        queued = set(self.queue)
        for r in range(15):
            for c in range(40):
                if (self.shown[r][c], self.shown_inv[r][c]) != (rows[r][c], inv[r]) and (r, c) not in queued:
                    self.queue.append((r, c))

    def _draw(self, r: int, c: int, ch: str, inv: bool) -> None:
        g = self.glyphs.get((ch, inv))
        if g is None:
            fg, bg = _PIX2[0xFFFF], _PIX2[0xF800 if inv else 0x0000]
            bits = self.font.get(ch, self.font[" "])
            g = [b"".join(fg if bits[y] & (0x80 >> x) else bg for x in range(8)) for y in range(16)]
            self.glyphs[(ch, inv)] = g
        for y in range(16):
            o = ((r * 16 + y) * W + c * 8) * 2
            self.gram[o:o + 16] = g[y]
        self.shown[r][c], self.shown_inv[r][c] = ch, inv

    def advance(self, now: float) -> None:
        if self.t0 is None:
            self.t0 = self.last = now
        if self.frozen:
            return
        while self.t0 + (self.ticks + 1) * self.TICK_S <= now:
            self.ticks += 1
            if self.ticks == 2 or self.ticks % int(self.PAGE_S / self.TICK_S) == 0:
                self.page += 1                          # a page change (a redraw burst)
            self.want = self._compose()
            self._enqueue()
        self.budget = min(self.budget + (now - self.last) * self.CELLS_PER_S, 600.0)
        self.last = now
        while self.queue and self.budget >= 1.0:
            r, c = self.queue.popleft()
            self._draw(r, c, *self._cell(r, c))
            self.budget -= 1.0


class Card:
    """(b) the clcd_demo test card (tests/clcd_demo/card_model.py, re-derived): white frame, 8 bars,
    grey separator, a 16-cell binary counter; a FULL repaint every 75 ms (the tunnel's ~480 ns/byte:
    153,600 B in ~75 ms, docs/contracts/dut-display-tunnel.md:185; silicon did ~3/s because the demo
    re-inits every frame). It starts just after a KVM handover: VALID all 0 until the first repaint."""

    name, owner, mode = "b_card", OWNER_DUT, "hw"
    PERIOD_S = 0.075
    BARS = (0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000)
    _base: bytes | None = None

    def __init__(self, *, handover: bool = True) -> None:
        self.b, self.strip, self.sep, self.gut = 4, (H * 3) // 4, (H * 3) // 4 - 4, 2
        self.counter = 0
        if Card._base is None:
            Card._base = b"".join(_PIX2[self._px(x, y, 0)] for y in range(H) for x in range(W))
        self.gram = bytearray(Card._base)
        self.next, self.frozen, self.frames, self.resets = None, False, 0, 1 if handover else 0
        self.valid: set[int] = set() if handover else set(ALL_TILES)

    def _px(self, x: int, y: int, frame: int) -> int:
        b = self.b
        if x < b or x >= W - b or y < b or y >= H - b:
            return 0xFFFF
        if y >= self.strip:
            k = min(15, (x * 16) // W)
            lo, hi = (W * k) // 16, (W * (k + 1)) // 16
            if x < lo + self.gut or x >= hi - self.gut:
                return 0x4208
            return 0xFFFF if (frame >> (15 - k)) & 1 else 0x0000
        if y >= self.sep:
            return 0x4208
        return self.BARS[min(7, (x * 8) // W)]

    def _set_counter(self, value: int) -> None:
        value &= 0xFFFF
        changed = value ^ self.counter
        for k in range(16):
            if changed >> (15 - k) & 1:
                lo, hi = (W * k) // 16, (W * (k + 1)) // 16
                px = _PIX2[0xFFFF if value >> (15 - k) & 1 else 0x0000] * (hi - lo - 2 * self.gut)
                for y in range(self.strip, H - self.b):
                    o = (y * W + lo + self.gut) * 2
                    self.gram[o:o + len(px)] = px
        self.counter = value

    def advance(self, now: float) -> None:
        if self.next is None:
            self.next = now + self.PERIOD_S
        if self.frozen or now < self.next:
            return
        while self.next <= now:
            self.next += self.PERIOD_S
            self._set_counter(self.counter + 1)       # a full repaint: every tile written
            self.frames += 1
        self.valid = set(ALL_TILES)


class Noise:
    """(c) worst case: a full frame of fresh random pixels every 75 ms (incompressible)."""

    name, owner, mode = "c_noise", OWNER_DUT, "hw"
    PERIOD_S = 0.075

    def __init__(self) -> None:
        self.gram = bytearray(os.urandom(FB_BYTES))
        self.next, self.frozen, self.frames, self.resets = None, False, 0, 0
        self.valid: set[int] = set(ALL_TILES)

    def advance(self, now: float) -> None:
        if self.next is None:
            self.next = now + self.PERIOD_S
        if self.frozen or now < self.next:
            return
        while self.next <= now:
            self.next += self.PERIOD_S
            self.frames += 1
        self.gram[:] = os.urandom(FB_BYTES)


PATTERNS = {"a_status": StatusScreen, "b_card": lambda: Card(handover=False), "c_noise": Noise}


# --- the fake board: mps3-lcdmirror as §6.1/§6.2 describe it ----------------------------------------


class FakeBoard:
    """At most 2 clients (the 3rd gets the refusal line). Per client: HELLO; nothing until KEY
    (amendment 6: HM sends KEY on start); every 1/rate s: dirty tiles vs what this client holds
    (SNAP + "dirty does not mean changed"), encoded smallest-per-tile, cut into UPDATEs of at most
    ``max_msg``; a keyframe (all VALID tiles, REGS on key_first) on KEY. ``flow="ack"`` is the
    PROPOSAL: at most ``window`` UPDATEs unacknowledged. ``gap_at``: that UPDATE is dropped on the
    floor (seq still advances) - HM must notice, send KEY and recover."""

    CLIENTS = 2

    def __init__(self, pattern: Any, native: Native, *, flow: str = "none", window: int = 2,
                 max_msg: int = MAX_MSG, gap_at: int = 0, mode: str | None = None,
                 owner: int | None = None) -> None:
        self.p, self.native, self.flow, self.window, self.max_msg = pattern, native, flow, window, max_msg
        self.mode = mode or pattern.mode
        self.owner = pattern.owner if owner is None else owner
        self.gap_at = gap_at
        self.regs = baseline_regs()
        self.srv = bind_ephemeral("fake board LCDM")
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.plock = threading.Lock()
        self.history: dict[int, int] = {}      # seq -> CRC of the valid tiles the client holds after it
        self.sample_us: dict[int, int] = {}    # seq -> the SNAP time (µs, this process's clock)
        self.stop = threading.Event()
        self.clients = 0
        self.conn_no = 0
        self.last_seq = 0
        self.st: dict[str, Any] = {
            "updates": 0, "bytes": 0, "key_bytes": 0, "keys": 0, "key_updates": 0, "tiles": 0,
            "enc_cpu": 0.0, "diff_cpu": 0.0, "snap_cpu": 0.0, "blocked_s": 0.0, "window_waits": 0,
            "refused": 0, "gaps": 0, "pongs": 0, "rate": RATE_DEFAULT, "rate_asked": None,
            "enc_hist": [0] * 5, "samples_with_change": 0}
        self.thread = threading.Thread(target=self._accept, name="lcdm-board", daemon=True)
        self.thread.start()

    def freeze(self) -> None:
        self.p.frozen = True

    def close(self) -> None:
        self.stop.set()
        with contextlib.suppress(OSError):
            self.srv.shutdown(socket.SHUT_RDWR)          # close() alone never wakes accept()
        with contextlib.suppress(OSError):
            self.srv.close()
        self.thread.join(timeout=5)

    def _accept(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            if self.clients >= self.CLIENTS:
                self.st["refused"] += 1
                with contextlib.suppress(OSError):
                    conn.sendall(b'{"ok":false,"err":"lcd_mirror: busy (2 clients)"}\n')
                conn.close()
                continue
            self.clients += 1
            self.conn_no += 1
            threading.Thread(target=self._serve_counted, args=(conn, self.conn_no), daemon=True,
                             name=f"lcdm-board-conn{self.conn_no}").start()

    def _serve_counted(self, conn: socket.socket, no: int) -> None:
        try:
            self._serve(conn, no)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            self.clients -= 1

    def _status(self, key: int) -> int:
        s = S_RST_N | S_BL | S_DISP | S_FMT_OK | key
        if self.owner == OWNER_DUT:
            s |= S_OWNER
        if self.mode == "hw":
            s |= S_EXACT
        else:
            s |= S_TEXT_ONLY
            s |= S_BLIND if self.owner == OWNER_DUT else S_EXACT
        return s

    def _serve(self, conn: socket.socket, no: int) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)   # no hidden MB of buffer
        hello = {"proto": 1, "w": W, "h": H, "fmt": "rgb565le", "tile": TILE, "mode": self.mode,
                 "static_id": "0x44EE76D5", "max_msg": self.max_msg}
        conn.sendall(msg(T_HELLO, json.dumps(hello).encode()))
        seq = no * 1_000_000                  # per connection, +1 per UPDATE (a unique base per conn)
        held = bytearray(FB_BYTES)            # what this client holds
        held_valid: set[int] = set()
        state = {"started": False, "want_key": False, "period": 1.0 / RATE_DEFAULT, "acked": seq}
        rbuf = bytearray()
        next_t = time.monotonic()
        conn.setblocking(False)
        made = 0

        def send(m: bytes) -> None:
            conn.setblocking(True)
            t0 = time.monotonic()
            conn.sendall(m)
            self.st["blocked_s"] += time.monotonic() - t0
            conn.setblocking(False)

        def ctrl(timeout: float) -> bool:
            r, _, _ = select.select([conn], [], [], max(0.0, timeout))
            if not r:
                return True
            try:
                data = conn.recv(65536)
            except BlockingIOError:
                return True
            if not data:
                return False
            rbuf.extend(data)
            while len(rbuf) >= 8:
                _m, typ, _r, ln = HDR.unpack_from(rbuf)
                if len(rbuf) < 8 + ln:
                    break
                body = bytes(rbuf[8:8 + ln])
                del rbuf[:8 + ln]
                if typ == T_KEY:
                    state["want_key"] = state["started"] = True
                elif typ == T_RATE:
                    self.st["rate_asked"] = body[0]
                    hz = max(1, min(RATE_MAX, body[0]))
                    self.st["rate"] = hz
                    state["period"] = 1.0 / hz
                    send(msg(T_RATE, bytes([hz])))
                elif typ == T_PING:
                    send(msg(T_PONG, body[:4]))       # queued behind UPDATEs: a queueing-delay probe
                    self.st["pongs"] += 1
                elif typ == T_ACK:
                    state["acked"] = max(state["acked"], struct.unpack("<I", body[:4])[0])
            return True

        while not self.stop.is_set():
            if not ctrl(next_t - time.monotonic()):
                return
            now = time.monotonic()
            if now < next_t:
                continue
            next_t = max(next_t + state["period"], now)
            if not state["started"]:
                continue
            if self.flow == "ack" and seq - state["acked"] >= self.window:
                self.st["window_waits"] += 1
                continue                              # drop-to-latest at the source
            t_us = time.monotonic_ns() // 1000
            c0 = time.thread_time()
            with self.plock:                          # SNAP: one consistent picture
                self.p.advance(now)
                gram = bytes(self.p.gram)
                valid = set(self.p.valid)
                frames, resets = self.p.frames, self.p.resets
            self.st["snap_cpu"] += time.thread_time() - c0
            key = state["want_key"]
            c0 = time.thread_time()
            if key:
                tiles = set(valid)
            else:
                tiles = self.native.tile_diff(gram, bytes(held)) & valid
                tiles |= valid - held_valid           # newly valid tiles (after a handover)
            self.st["diff_cpu"] += time.thread_time() - c0
            if not tiles and not key and valid == held_valid:
                continue                              # nothing changed: no UPDATE (PING covers liveness)
            self.st["samples_with_change"] += 1
            c0 = time.thread_time()
            recs = self.native.enc_frame(gram, tiles)
            self.st["enc_cpu"] += time.thread_time() - c0
            budget = self.max_msg - (8 + UPD.size + (256 if key else 4) + 2)
            parts = split_records(recs, budget)
            vbytes = valid_bytes(valid)
            for i, (n, chunk) in enumerate(parts):
                kbits = 0
                if key:
                    kbits = S_KEY | (S_KEY_FIRST if i == 0 else 0) | (S_KEY_LAST if i == len(parts) - 1 else 0)
                seq += 1
                m = build_update(seq, t_us // 1000, frames, resets, self._status(kbits), self.owner,
                                 vbytes, self.regs, n, chunk)
                j = 0
                while j < len(chunk):                 # the client's copy after this part
                    t, e, ln = TREC.unpack_from(chunk, j)
                    put_tile(held, t, tile_px(gram, t))
                    self.st["enc_hist"][e] += 1
                    j += TREC.size + ln
                held_valid = set(valid)
                self.history[seq] = crc_valid(held, held_valid)
                self.sample_us[seq] = t_us
                self.last_seq = seq
                made += 1
                if self.gap_at and made == self.gap_at and not key:
                    self.st["gaps"] += 1              # this UPDATE is lost: the client never sees it
                    continue
                send(m)
                self.st["updates"] += 1
                self.st["bytes"] += len(m)
                self.st["tiles"] += n
                if key:
                    self.st["key_updates"] += 1
                    self.st["key_bytes"] += len(m)
            if key:
                self.st["keys"] += 1
                state["want_key"] = False


# --- a link cap with one SSH channel window of buffering ---------------------------------------------


class Throttle:
    """board -> HM at ``rate`` B/s with at most ``window`` bytes buffered (OpenSSH's default channel
    window is 2 MiB, and ``ssh -J`` stacks two); HM -> board passes unthrottled."""

    def __init__(self, upstream: tuple[str, int], rate: float, window: int = 2 << 20) -> None:
        self.upstream, self.rate, self.window = upstream, rate, window
        self.srv = bind_ephemeral("throttle")
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        self.max_buffered = 0
        self.socks: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True, name="throttle").start()

    def close(self) -> None:
        self.stop.set()
        for s in [self.srv, *self.socks]:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                s.close()

    def _accept(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                up = socket.socket()
                up.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
                up.connect(self.upstream)
            except OSError:
                conn.close()
                continue
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            self.socks += [conn, up]
            self._pipe(conn, up)

    def _pipe(self, conn: socket.socket, up: socket.socket) -> None:
        """One connection's three pumps. Their own scope, so a later accept cannot rebind
        the sockets and buffer these closures use."""
        if True:
            buf, cond, state = bytearray(), threading.Condition(), {"eof": False}

            def down_reader() -> None:
                try:
                    while not self.stop.is_set():
                        with cond:
                            while len(buf) >= self.window and not self.stop.is_set():
                                cond.wait(0.1)
                        data = up.recv(65536)
                        if not data:
                            break
                        with cond:
                            buf.extend(data)
                            self.max_buffered = max(self.max_buffered, len(buf))
                            cond.notify_all()
                except OSError:
                    pass
                with cond:
                    state["eof"] = True
                    cond.notify_all()

            def down_writer() -> None:
                tokens, last = 0.0, time.monotonic()
                try:
                    while not self.stop.is_set():
                        with cond:
                            while not buf and not state["eof"] and not self.stop.is_set():
                                cond.wait(0.1)
                            if not buf and state["eof"]:
                                break
                        now = time.monotonic()
                        tokens = min(tokens + (now - last) * self.rate, 32 * 1024)
                        last = now
                        if tokens < 1024:
                            time.sleep((1024 - tokens) / self.rate)
                            continue
                        with cond:
                            n = min(len(buf), int(tokens))
                            chunk = bytes(buf[:n])
                            del buf[:n]
                            cond.notify_all()
                        tokens -= n
                        conn.sendall(chunk)
                except OSError:
                    pass
                with contextlib.suppress(OSError):
                    conn.shutdown(socket.SHUT_WR)

            def up_pass() -> None:
                try:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            break
                        up.sendall(data)
                except OSError:
                    pass
                with contextlib.suppress(OSError):
                    up.shutdown(socket.SHUT_WR)

            for fn in (down_reader, down_writer, up_pass):
                threading.Thread(target=fn, daemon=True, name=f"throttle-{fn.__name__}").start()


# --- HM: the compositor and the upstream reader (the DisplayService prototype) -----------------------


class Viewer:
    def __init__(self, wake: Any) -> None:
        self.dirty: set[int] = set(ALL_TILES)           # a keyframe on join
        self.wake = wake
        self.sent = 0


class Compositor:
    """One presented picture per board, shared by N viewers: the decoded framebuffer (PNG, checks)
    and the latest encoded record per tile (forwarded to browsers as the board encoded it). A viewer
    holds only a dirty-tile set (<= 300), so a slow one costs no memory and slows nobody else."""

    def __init__(self) -> None:
        self.fb = bytearray(FB_BYTES)
        self.rec: list[bytes | None] = [None] * NT   # TREC + payload, as received
        self.mu = threading.Lock()
        self.seq = self.t_ms = self.frames = self.resets = self.status = 0
        self.owner = OWNER_UNKNOWN
        self.valid = bytes(38)
        self.regs = baseline_regs()
        self.mword: int | None = None
        self.hello: dict[str, Any] | None = None
        self.viewers: list[Viewer] = []
        self.state, self.detail = "down", ""
        self.presented = False
        self.max_dirty = 0

    @staticmethod
    def _wake(viewers: list[Viewer]) -> None:
        for v in viewers:
            with contextlib.suppress(RuntimeError):     # the viewer's event loop has gone
                v.wake()

    def add(self, wake: Any) -> Viewer:
        v = Viewer(wake)
        with self.mu:
            self.viewers.append(v)
            ready = self.presented
        if ready:
            self._wake([v])
        return v

    def remove(self, v: Viewer) -> None:
        with self.mu:
            with contextlib.suppress(ValueError):
                self.viewers.remove(v)

    def hatched(self) -> int:
        return NT - len(valid_set(self.valid))

    def present(self, fb: bytearray | None, recs: dict[int, bytes], hdr: dict[str, Any]) -> None:
        """Make an UPDATE (or a whole staged keyframe) visible, atomically."""
        with self.mu:
            if fb is not None:
                self.fb = fb
            for t, r in recs.items():
                self.rec[t] = r
            changed = set(recs)
            if hdr["valid"] != self.valid or hdr["owner"] != self.owner or \
                    (hdr["status"] & 0xFFFFFF) != (self.status & 0xFFFFFF):
                changed.add(0)                         # a header-only change still reaches viewers
            self.seq, self.t_ms = hdr["seq"], hdr["t_ms"]
            self.frames, self.resets, self.status = hdr["frames"], hdr["resets"], hdr["status"]
            self.owner, self.valid = hdr["owner"], hdr["valid"]
            if hdr["regs"] is not None:
                self.regs, self.mword = hdr["regs"], None
            elif hdr["mode"] is not None:
                self.mword = hdr["mode"]
            self.presented = True
            for v in self.viewers:
                v.dirty |= changed
                self.max_dirty = max(self.max_dirty, len(v.dirty))
            viewers = list(self.viewers)
        self._wake(viewers)

    def snapshot(self, v: Viewer) -> bytes | None:
        """What this viewer lacks: ONE UPDATE (no max_msg on localhost), consistent at ``seq``."""
        with self.mu:
            if not v.dirty or not self.presented:
                return None
            tiles, v.dirty = v.dirty, set()
            key = v.sent == 0
            if key:
                tiles = set(ALL_TILES)
            recs = [self.rec[t] for t in sorted(tiles) if self.rec[t] is not None]
            status = (self.status & ~KEY_BITS) | (KEY_BITS if key else 0)
            m = build_update(self.seq, self.t_ms, self.frames, self.resets, status, self.owner,
                             self.valid, self.regs, len(recs), b"".join(recs))
            v.sent += 1
        return m


class Refused(ConnectionError):
    pass


class Upstream(threading.Thread):
    """One connection to the board's lcd_mirror service through the tunnel; reconnects with back-off.
    KEY on start and after a seq gap; keyframe parts are staged and presented on key_last; PING
    every 0.5 s (a PONG queued behind UPDATEs measures the backlog); RATE asked once."""

    BACKOFF = (0.1, 0.2, 0.5, 1.0)

    def __init__(self, port: int, comp: Compositor, *, rate: int = RATE_MAX, flow: str = "none",
                 tunnel: Any = None, sample_us: dict[int, int] | None = None) -> None:
        super().__init__(daemon=True, name="lcdm-upstream")
        self.port, self.comp, self.rate, self.flow, self.tunnel = port, comp, rate, flow, tunnel
        self.sample_us = sample_us if sample_us is not None else {}
        self.stop = threading.Event()
        self.sock: socket.socket | None = None
        self.smu = threading.Lock()
        self.pings: dict[int, float] = {}
        self.st: dict[str, Any] = {"updates": 0, "bytes": 0, "lat_us": [], "connects": 0,
                                   "no_service": 0, "refusals": [], "errors": [], "keys_presented": 0,
                                   "gaps": 0, "keys_sent": 0, "rtt_ms": [], "rate_echo": None,
                                   "decode_cpu": 0.0, "hatched_max": 0}
        self.hello_evt, self.key_evt = threading.Event(), threading.Event()
        self.max_msg = MAX_MSG

    def close(self) -> None:
        self.stop.set()
        s = self.sock
        if s is not None:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
        self.join(timeout=5)

    def _send(self, m: bytes) -> None:
        s = self.sock
        if s is not None:
            with self.smu:
                s.sendall(m)

    def run(self) -> None:
        attempt = 0
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                self._session()
                attempt = 0
            except Refused as exc:
                self.st["refusals"].append(str(exc))
                self.comp.state, self.comp.detail = "refused", str(exc)
                attempt = len(self.BACKOFF) - 1
            except (OSError, EOFError, ValueError) as exc:
                if self.stop.is_set():
                    return
                why = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, EOFError) and self.tunnel is not None:
                    fails = self.tunnel.open_failures_since(t0, wait_s=0.5)  # accept-then-close via ssh -L
                    if fails:
                        self.st["no_service"] += 1
                        why = "no lcd_mirror service on the board (" + fails[-1] + ")"
                self.comp.state, self.comp.detail = "reconnecting", why
                self.st["errors"].append(why)
            if self.stop.wait(self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]):
                return
            attempt += 1

    @staticmethod
    def _exact(f: Any, n: int) -> bytes:
        b = f.read(n)
        if b is None or len(b) < n:
            raise EOFError(f"closed after {0 if not b else len(b)} of {n} bytes")
        return b

    def _read(self, f: Any) -> tuple[int, bytes]:
        head = f.read(1)
        if head == b"{":                              # amendment 3: the refusal line
            raise Refused((head + f.readline(4096)).decode("utf-8", "replace").strip())
        if not head:
            raise EOFError("closed before a message")
        head += self._exact(f, 7)
        m, typ, _r, ln = HDR.unpack(head)
        if m != b"LM" or 8 + ln > self.max_msg:
            raise ValueError(f"not an lcd_mirror message (magic {m!r}, len {ln})")
        return typ, self._exact(f, ln)

    def _pinger(self, sock: socket.socket) -> None:
        token = 0
        while not self.stop.is_set() and self.sock is sock:
            token += 1
            self.pings[token] = time.monotonic()
            with contextlib.suppress(OSError):
                self._send(msg(T_PING, struct.pack("<I", token)))
            if self.stop.wait(0.5):
                return

    def _session(self) -> None:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.settimeout(None)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.st["connects"] += 1
        self.pings = {}
        try:
            f = s.makefile("rb", buffering=256 * 1024)
            typ, body = self._read(f)
            if typ != T_HELLO:
                raise ValueError(f"first message is type {typ}, not HELLO")
            hello = json.loads(body)
            if (hello.get("proto"), hello.get("w"), hello.get("h"), hello.get("fmt"), hello.get("tile")) != \
                    (1, W, H, "rgb565le", TILE):
                raise ValueError(f"unsupported HELLO {hello}")
            self.max_msg = int(hello.get("max_msg") or MAX_MSG)
            self.comp.hello = hello
            self.comp.state, self.comp.detail = "up", ""
            self.hello_evt.set()
            self._send(msg(T_RATE, bytes([self.rate])))
            self._send(msg(T_KEY))                    # amendment 6: KEY on start
            self.st["keys_sent"] += 1
            threading.Thread(target=self._pinger, args=(s,), daemon=True, name="lcdm-ping").start()
            last_seq: int | None = None
            resync = True                             # nothing is presented before a whole keyframe
            staging: tuple[bytearray, dict[int, bytes]] | None = None
            staging_regs: bytes | None = None
            while not self.stop.is_set():
                typ, body = self._read(f)
                self.st["bytes"] += 8 + len(body)
                if typ == T_PONG:
                    (tok,) = struct.unpack("<I", body)
                    sent = self.pings.pop(tok, None)
                    if sent is not None:
                        self.st["rtt_ms"].append((time.monotonic(), (time.monotonic() - sent) * 1e3))
                    continue
                if typ == T_RATE:
                    self.st["rate_echo"] = body[0]
                    continue
                if typ != T_UPDATE:
                    continue                          # unknown types are skipped (forward compatible)
                c0 = time.thread_time()
                u = parse_update(body)
                self.st["updates"] += 1
                if last_seq is not None and u["seq"] != (last_seq + 1) & 0xFFFFFFFF:
                    self.st["gaps"] += 1
                    resync, staging = True, None
                    self._send(msg(T_KEY))            # amendment 6: KEY after a gap
                    self.st["keys_sent"] += 1
                last_seq = u["seq"]
                if self.flow == "ack":
                    self._send(msg(T_ACK, struct.pack("<I", u["seq"])))
                st = u["status"]
                if st & S_KEY:
                    if st & S_KEY_FIRST:
                        staging, staging_regs = (bytearray(self.comp.fb), {}), u["regs"]
                    if staging is not None:
                        fb, recs = staging
                        for t, e, p in u["tiles"]:
                            put_tile(fb, t, decode_tile(e, p))
                            recs[t] = TREC.pack(t, e, len(p)) + p
                        if st & S_KEY_LAST:
                            u["regs"] = staging_regs
                            self.comp.present(fb, recs, u)
                            staging, resync = None, False
                            self.st["keys_presented"] += 1
                            self.key_evt.set()
                elif not resync:
                    recs = {}
                    with self.comp.mu:
                        fb = self.comp.fb                # deltas go straight into the picture
                        for t, e, p in u["tiles"]:
                            put_tile(fb, t, decode_tile(e, p))
                            recs[t] = TREC.pack(t, e, len(p)) + p
                    self.comp.present(None, recs, u)
                self.st["decode_cpu"] += time.thread_time() - c0
                self.st["hatched_max"] = max(self.st["hatched_max"], NT - len(valid_set(u["valid"])))
                t_snap = self.sample_us.get(u["seq"])
                if t_snap is not None:
                    self.st["lat_us"].append((t_snap, time.monotonic_ns() // 1000 - t_snap))
        finally:
            self.sock = None
            with contextlib.suppress(OSError):
                s.close()


# --- the daemon's WebSocket (FastAPI + uvicorn, as harness-manager-daemon serves) ---------------------


class Daemon:
    def __init__(self) -> None:
        self.boards: dict[str, Compositor] = {}
        app = FastAPI()

        @app.websocket("/api/v1/boards/{bid}/display/ws")
        async def display_ws(ws: WebSocket, bid: str) -> None:
            comp = self.boards.get(bid)
            if comp is None:
                await ws.close(code=4012, reason="no display for this board")
                return
            await ws.accept()
            loop = asyncio.get_running_loop()
            ev, acked = asyncio.Event(), asyncio.Event()
            acked.set()
            ack_mode = ws.query_params.get("ack") == "1"
            v = comp.add(lambda: loop.call_soon_threadsafe(ev.set))
            await ws.send_text(json.dumps({"state": comp.state, "hello": comp.hello}))

            async def sender() -> None:
                while True:
                    await ev.wait()
                    if ack_mode:
                        await acked.wait()          # <= 1 message in flight: the kernel never queues
                        acked.clear()
                    ev.clear()
                    m = comp.snapshot(v)            # everything since the last send: drop-to-latest
                    if m is None:
                        acked.set()
                        continue
                    await ws.send_bytes(m)

            async def receiver() -> None:           # {"ack": seq}; a browser's KEY/RATE would arrive here
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if m.get("text") and '"ack"' in m["text"]:
                        acked.set()

            tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                comp.remove(v)

        @app.get("/api/v1/boards/{bid}/display.png")
        def display_png(bid: str) -> Response:
            comp = self.boards[bid]
            with comp.mu:
                fb, seq = bytes(comp.fb), comp.seq
            return Response(png_of(fb), media_type="image/png", headers={"X-Lcdm-Seq": str(seq)})

        self.sock = bind_ephemeral("daemon (uvicorn)")
        self.port = self.sock.getsockname()[1]
        cfg = uvicorn.Config(app, log_level="error", lifespan="off", access_log=False,
                             ws_per_message_deflate=True,       # HM's daemon default (server.py:390)
                             timeout_graceful_shutdown=2)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]},
                                       daemon=True, name="uvicorn")
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise SystemExit("uvicorn did not start")

    def url(self, bid: str, ack: bool = True) -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/boards/{bid}/display/ws" + ("?ack=1" if ack else "")

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class BrowserStandIn(threading.Thread):
    """The browser: decodes each binary UPDATE (all 5 encodings), applies it, and after EVERY
    message checks CRC(its valid tiles) == the board's CRC at that seq."""

    def __init__(self, url: str, history: dict[int, int], sample_us: dict[int, int], *,
                 slow_s: float = 0.0, deflate: bool = False, ack: bool = True, name: str = "viewer") -> None:
        super().__init__(daemon=True, name=name)
        self.url, self.history, self.sample_us = url, history, sample_us
        self.slow_s, self.deflate, self.ack = slow_s, deflate, ack
        self.fb = bytearray(FB_BYTES)
        self.valid: set[int] = set()
        self.stop = threading.Event()
        self.st: dict[str, Any] = {"msgs": 0, "bytes": 0, "lat_us": [], "crc_ok": 0, "crc_bad": 0,
                                   "crc_unknown": 0, "texts": [], "keys": 0, "negotiated": "",
                                   "first_bytes": 0, "error": ""}
        self.last_seq = -1
        self.ready = threading.Event()
        self.owner = self.status = 0

    def run(self) -> None:
        from websockets.sync.client import connect

        try:
            with connect(self.url, max_size=8 << 20, compression="deflate" if self.deflate else None,
                         open_timeout=10) as ws:
                self.st["negotiated"] = ws.response.headers.get("Sec-WebSocket-Extensions", "") or "none"
                self.ready.set()
                while not self.stop.is_set():
                    try:
                        m = ws.recv(timeout=0.1)
                    except TimeoutError:
                        continue
                    except Exception:  # noqa: BLE001 - closed
                        return
                    if isinstance(m, str):
                        self.st["texts"].append(json.loads(m))
                        continue
                    seq = self._apply(m)
                    if self.slow_s:
                        time.sleep(self.slow_s)        # a slow tab: busy before it can ack
                    if self.ack:
                        ws.send(json.dumps({"ack": seq}))
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            self.st["error"] = f"{type(exc).__name__}: {exc}"
            self.ready.set()

    def _apply(self, m: bytes) -> int:
        _mm, typ, _r, ln = HDR.unpack_from(m)
        assert typ == T_UPDATE and ln == len(m) - 8
        u = parse_update(m[8:])
        for t, e, p in u["tiles"]:
            put_tile(self.fb, t, decode_tile(e, p))
        self.valid = valid_set(u["valid"])
        now = time.monotonic_ns() // 1000
        self.st["msgs"] += 1
        self.st["bytes"] += len(m)
        if not self.st["first_bytes"]:
            self.st["first_bytes"] = len(m)
        t_snap = self.sample_us.get(u["seq"])
        if t_snap is not None:
            self.st["lat_us"].append((t_snap, now - t_snap))
        if u["status"] & S_KEY:
            self.st["keys"] += 1
        want = self.history.get(u["seq"])
        if want is None:
            self.st["crc_unknown"] += 1
        elif crc_valid(self.fb, self.valid) == want:
            self.st["crc_ok"] += 1
        else:
            self.st["crc_bad"] += 1
        self.last_seq, self.owner, self.status = u["seq"], u["owner"], u["status"]
        return u["seq"]


# --- one measured run ------------------------------------------------------------------------------------


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def cpu_of(th: threading.Thread) -> float:
    try:
        return time.clock_gettime(time.pthread_getcpuclockid(th.ident))
    except (OSError, TypeError, AttributeError):
        return float("nan")


class Ctx:
    def __init__(self, native: Native) -> None:
        self.native = native
        self.fake = FakeSsh(hub_loopback=False)
        self.local = pick_port("tunnel local (lcdm)")
        # The claim.open_forward shape (claim.py:962-979): ssh -J HUB -l root <pinned> -L ... BOARD
        pinned = ["-o", "HostKeyAlias=harness-manager-mps3-01", "-o", "StrictHostKeyChecking=yes",
                  "-o", "PreferredAuthentications=publickey"]
        self.tunnel = T.SshTunnel(BOARD, [T.Forward("lcdm", "127.0.0.1", LCDM_BOARD_PORT, self.local)],
                                  launcher=self.fake, ssh_g=self.fake.ssh_g, jump=HUB, user="root",
                                  options=pinned, backoff_s=(0.2, 0.5), label="mps3-01 lcdm (spike)")
        self.tunnel.start()
        check_port(self.tunnel.local_port("lcdm"), "tunnel local")
        self.daemon = Daemon()
        self.runs = 0

    def route(self, port: int) -> None:
        self.fake.routes[("127.0.0.1", LCDM_BOARD_PORT)] = ("127.0.0.1", port)

    def close(self) -> None:
        self.daemon.close()
        self.tunnel.close()
        self.fake.close()


def run_case(ctx: Ctx, pattern: str, *, duration: float, rate: float | None = None, hz: int = RATE_MAX,
             flow: str = "none", window: int = 2, viewers: tuple[dict[str, Any], ...] = ({},),
             drain_s: float = 8.0) -> dict[str, Any]:
    ctx.runs += 1
    bid = f"mps3-01-run{ctx.runs}"
    board = FakeBoard(PATTERNS[pattern](), ctx.native, flow=flow, window=window)
    proxy = Throttle(("127.0.0.1", board.port), rate) if rate else None
    ctx.route(proxy.port if proxy else board.port)
    comp = Compositor()
    ctx.daemon.boards[bid] = comp
    up = Upstream(ctx.tunnel.local_port("lcdm"), comp, rate=hz, flow=flow, tunnel=ctx.tunnel,
                  sample_us=board.sample_us)
    up.start()
    if not up.hello_evt.wait(10):
        raise RuntimeError(f"no HELLO through the tunnel: {up.st['errors'][-1:]}")
    clients = [BrowserStandIn(ctx.daemon.url(bid, v.get("ack", True)), board.history, board.sample_us,
                              name=f"viewer{i}", **v) for i, v in enumerate(viewers)]
    for c in clients:
        c.start()
        c.ready.wait(10)
    uv0, up0 = cpu_of(ctx.daemon.thread), cpu_of(up)
    t0 = time.monotonic_ns() // 1000
    hb0 = up.st["bytes"]
    time.sleep(duration)
    t1 = time.monotonic_ns() // 1000
    hb1 = up.st["bytes"]
    with board.plock:
        board.freeze()
    updates_at_freeze = board.st["updates"]
    deadline = time.monotonic() + drain_s
    drained = False
    while time.monotonic() < deadline:
        if board.last_seq and comp.seq == board.last_seq and all(c.last_seq == board.last_seq for c in clients):
            time.sleep(0.3)                           # a straggler would move last_seq on
            if comp.seq == board.last_seq and all(c.last_seq == board.last_seq for c in clients):
                drained = True
                break
        time.sleep(0.02)
    uv1, up1 = cpu_of(ctx.daemon.thread), cpu_of(up)
    want = crc_valid(board.p.gram, set(board.p.valid))
    exact = drained and all(crc_valid(c.fb, c.valid) == want for c in clients) and \
        crc_valid(comp.fb, valid_set(comp.valid)) == want
    for c in clients:
        c.stop.set()
    for c in clients:
        c.join(timeout=5)
    up.close()
    board.close()
    if proxy:
        proxy.close()
    ctx.daemon.boards.pop(bid, None)

    st, win_s = board.st, (t1 - t0) / 1e6
    n_steady = max(0, st["updates"] - st["key_updates"])
    v0 = clients[0]
    lat_v = [lat / 1e3 for (tu, lat) in v0.st["lat_us"] if t0 <= tu <= t1]
    snaps_in = {tu for (tu, _l) in v0.st["lat_us"] if t0 <= tu <= t1}
    rtt = [r for (_tm, r) in up.st["rtt_ms"]]
    ws_msgs = sum(c.st["msgs"] for c in clients)
    return {
        "pattern": pattern, "flow": flow if flow == "none" else f"ack{window}", "hz": st["rate"],
        "hz_asked": st["rate_asked"], "hz_echo": up.st["rate_echo"], "rate": rate, "duration_s": round(win_s, 2),
        "updates": st["updates"], "keyframe_B": st["key_bytes"] // max(1, st["keys"]),
        "keyframe_updates": st["key_updates"] // max(1, st["keys"]),
        "B_per_update": round((st["bytes"] - st["key_bytes"]) / n_steady) if n_steady else 0,
        "tiles_per_update": round(st["tiles"] / max(1, st["updates"]), 1),
        "enc_hist": dict(zip(ENC_NAMES, st["enc_hist"], strict=False)),
        "kBps": round((hb1 - hb0) / win_s / 1e3, 1),
        "fps_viewer": round(len(snaps_in) / win_s, 1),
        "ups_board": round(updates_at_freeze / win_s, 1),
        "lat_ms_p50": round(pct(lat_v, 0.5), 1), "lat_ms_p95": round(pct(lat_v, 0.95), 1),
        "rtt_ms_p50": round(pct(rtt, 0.5), 1), "rtt_ms_p95": round(pct(rtt, 0.95), 1),
        "board_snap_us": round(1e6 * st["snap_cpu"] / max(1, st["samples_with_change"]), 1),
        "board_diff_us": round(1e6 * st["diff_cpu"] / max(1, st["samples_with_change"]), 1),
        "board_enc_us": round(1e6 * st["enc_cpu"] / max(1, st["samples_with_change"]), 1),
        "hm_decode_us_per_update": round(1e6 * up.st["decode_cpu"] / max(1, up.st["updates"]), 1),
        "hm_reader_us_per_update": round(1e6 * (up1 - up0) / max(1, up.st["updates"]), 1),
        "hm_ws_us_per_msg": round(1e6 * (uv1 - uv0) / max(1, ws_msgs), 1),
        "board_blocked_s": round(st["blocked_s"], 2), "window_waits": st["window_waits"],
        "max_buffered_B": proxy.max_buffered if proxy else None,
        "crc_ok": sum(c.st["crc_ok"] for c in clients), "crc_bad": sum(c.st["crc_bad"] for c in clients),
        "exact": exact, "drained": drained, "gaps_seen": up.st["gaps"], "keys_sent": up.st["keys_sent"],
        "keys_presented": up.st["keys_presented"], "hatched_max": up.st["hatched_max"],
        "viewer_first_msg_B": v0.st["first_bytes"],
        "viewers": [{"msgs": c.st["msgs"], "keys": c.st["keys"], "slow_s": c.slow_s, "ack": c.ack,
                     "deflate": c.st["negotiated"], "crc_bad": c.st["crc_bad"], "error": c.st["error"],
                     "lat_ms_p50": round(pct([lat / 1e3 for (tu, lat) in c.st["lat_us"] if t0 <= tu <= t1], .5), 1)}
                    for c in clients],
        "viewer_max_dirty_tiles": comp.max_dirty,
    }


# --- checks: the behaviours the design depends on ----------------------------------------------------


def check_argv(ctx: Ctx) -> dict[str, Any]:
    argv = ctx.fake.launches[0]
    spec = argv[argv.index("-L") + 1]
    ok = (argv[-1] == BOARD and argv[argv.index("-J") + 1] == HUB and argv[argv.index("-l") + 1] == "root"
          and spec == f"127.0.0.1:{ctx.tunnel.local_port('lcdm')}:127.0.0.1:{LCDM_BOARD_PORT}"
          and all(o in argv for o in ("ControlPath=none", "ControlMaster=no", "ExitOnForwardFailure=yes",
                                      "BatchMode=yes", "StrictHostKeyChecking=yes")))
    return {"ok": ok, "argv": " ".join(argv[1:])}


def _stream(ctx: Ctx, board: FakeBoard, bid: str, **kw: Any) -> tuple[Compositor, Upstream, BrowserStandIn]:
    ctx.route(board.port)
    comp = Compositor()
    ctx.daemon.boards[bid] = comp
    up = Upstream(ctx.tunnel.local_port("lcdm"), comp, tunnel=ctx.tunnel, sample_us=board.sample_us, **kw)
    up.start()
    up.hello_evt.wait(10)
    viewer = BrowserStandIn(ctx.daemon.url(bid), board.history, board.sample_us)
    viewer.start()
    viewer.ready.wait(10)
    return comp, up, viewer


def _settle(board: FakeBoard, comp: Compositor, viewer: BrowserStandIn, timeout: float = 6) -> bool:
    with board.plock:
        board.freeze()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if board.last_seq and comp.seq == board.last_seq == viewer.last_seq:
            time.sleep(0.3)
            if comp.seq == board.last_seq == viewer.last_seq:
                return True
        time.sleep(0.05)
    return False


def _end(ctx: Ctx, bid: str, board: FakeBoard, up: Upstream, *viewers: BrowserStandIn) -> None:
    for v in viewers:
        v.stop.set()
        v.join(5)
    up.close()
    board.close()
    ctx.daemon.boards.pop(bid, None)


def check_no_service(ctx: Ctx) -> dict[str, Any]:
    ctx.route(pick_port("dead board port"))           # nothing listens: an image without the service
    comp = Compositor()
    up = Upstream(ctx.tunnel.local_port("lcdm"), comp, tunnel=ctx.tunnel)
    t0 = time.monotonic()
    up.start()
    while time.monotonic() - t0 < 3 and not up.st["no_service"]:
        time.sleep(0.05)
    up.close()
    return {"ok": up.st["no_service"] > 0 and comp.state == "reconnecting",
            "detected_in_s": round(time.monotonic() - t0, 2), "detail": comp.detail}


def check_refusal(ctx: Ctx) -> dict[str, Any]:
    """Two HM clients are served; a third gets the one-line JSON refusal, then close."""
    board = FakeBoard(StatusScreen(), ctx.native)
    ctx.route(board.port)
    ups = [Upstream(ctx.tunnel.local_port("lcdm"), Compositor(), tunnel=ctx.tunnel) for _ in range(3)]
    for u in ups[:2]:
        u.start()
        u.key_evt.wait(10)
    ups[2].start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3 and not ups[2].st["refusals"]:
        time.sleep(0.05)
    out = {"ok": all(u.st["keys_presented"] for u in ups[:2]) and bool(ups[2].st["refusals"])
                 and ups[2].comp.state == "refused" and board.st["refused"] >= 1,
           "served": [u.st["keys_presented"] for u in ups[:2]],
           "third": (ups[2].st["refusals"] or [""])[0]}
    for u in ups:
        u.close()
    board.close()
    return out


def check_gap(ctx: Ctx) -> dict[str, Any]:
    """One UPDATE is lost (seq still advances): HM sees the gap, sends KEY, the board sends a whole
    keyframe WITH REGS (split: noise is 3 UPDATEs), HM presents it on key_last, the viewer ends exact."""
    board = FakeBoard(Noise(), ctx.native, gap_at=12)
    comp, up, viewer = _stream(ctx, board, "mps3-01-gap")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 6 and up.st["keys_presented"] < 2:
        time.sleep(0.02)
    t_rec = time.monotonic() - t0
    settled = _settle(board, comp, viewer)
    exact = crc_valid(viewer.fb, viewer.valid) == crc_valid(board.p.gram, board.p.valid)
    out = {"ok": board.st["gaps"] == 1 and up.st["gaps"] >= 1 and up.st["keys_presented"] >= 2
                 and settled and exact and viewer.st["crc_bad"] == 0,
           "gaps_injected": board.st["gaps"], "gaps_seen": up.st["gaps"], "keys_sent": up.st["keys_sent"],
           "keys_presented": up.st["keys_presented"], "key_updates_total": board.st["key_updates"],
           "to_second_key_s": round(t_rec, 2), "viewer_crc_bad": viewer.st["crc_bad"], "viewer_exact": exact}
    _end(ctx, "mps3-01-gap", board, up, viewer)
    return out


def check_drop_and_claim_loss(ctx: Ctx) -> dict[str, Any]:
    """Tunnel drop (ServerAlive gave up / board reboot), then a refused key (claim lost) for ~1.2 s,
    then the key works again: the supervisor restarts on the SAME local port, the reader
    reconnects, sends KEY, and the viewer ends bit-exact."""
    board = FakeBoard(StatusScreen(), ctx.native)
    comp, up, viewer = _stream(ctx, board, "mps3-01-drop")
    time.sleep(0.8)
    local_before = ctx.tunnel.local_port("lcdm")
    up.key_evt.clear()
    ctx.fake.fail = "auth"                          # the next ssh start is refused (claim lost)
    t_drop = time.monotonic()
    ctx.fake.current.drop()
    time.sleep(1.2)
    tunnel_detail, state_during = ctx.tunnel.detail, comp.state
    ctx.fake.fail = ""                               # the key is accepted again
    recovered = up.key_evt.wait(10)
    t_rec = time.monotonic() - t_drop
    time.sleep(0.6)
    settled = _settle(board, comp, viewer)
    exact = crc_valid(viewer.fb, viewer.valid) == crc_valid(board.p.gram, board.p.valid)
    out = {"ok": recovered and settled and exact and "Permission denied" in tunnel_detail
                 and ctx.tunnel.local_port("lcdm") == local_before,
           "recovered_s": round(t_rec, 2), "same_local_port": ctx.tunnel.local_port("lcdm") == local_before,
           "tunnel_detail_while_refused": tunnel_detail, "hm_state_while_down": state_during,
           "reconnects": up.st["connects"], "tunnel_restarts": ctx.tunnel.restarts,
           "viewer_exact_after": exact, "viewer_crc_bad": viewer.st["crc_bad"]}
    _end(ctx, "mps3-01-drop", board, up, viewer)
    return out


def check_blind(ctx: Ctx) -> dict[str, Any]:
    """sw mode (pre-mint-4) while the DUT owns the panel: VALID all 0, status blind: grey + hatch 300."""
    board = FakeBoard(StatusScreen(), ctx.native, mode="sw", owner=OWNER_DUT)
    board.p.valid = set()
    comp, up, viewer = _stream(ctx, board, "mps3-01-blind")
    up.key_evt.wait(5)
    time.sleep(0.4)
    b = badges(comp.status, comp.owner, (comp.hello or {}).get("mode", ""), comp.regs, comp.mword)
    out = {"ok": bool(comp.status & S_BLIND) and comp.owner == OWNER_DUT and comp.hatched() == NT
                 and any(x.startswith("grey") for x in b) and viewer.owner == OWNER_DUT,
           "hello_mode": (comp.hello or {}).get("mode"), "status": f"0x{comp.status:08x}",
           "hatched": comp.hatched(), "badges": b}
    _end(ctx, "mps3-01-blind", board, up, viewer)
    return out


def check_handover(ctx: Ctx) -> dict[str, Any]:
    """Just after a KVM handover (RESETS+1, VALID all 0): the keyframe presents 300 hatched tiles;
    the DUT's first repaint validates them and the viewer ends bit-exact."""
    board = FakeBoard(Card(handover=True), ctx.native)
    comp, up, viewer = _stream(ctx, board, "mps3-01-handover")
    up.key_evt.wait(5)
    hatched_at_key = up.st["hatched_max"]
    time.sleep(0.5)
    settled = _settle(board, comp, viewer)
    exact = crc_valid(viewer.fb, viewer.valid) == crc_valid(board.p.gram, board.p.valid)
    out = {"ok": hatched_at_key == NT and comp.hatched() == 0 and settled and exact and viewer.st["crc_bad"] == 0,
           "hatched_at_key": hatched_at_key, "hatched_after_repaint": comp.hatched(), "resets": comp.resets,
           "viewer_exact": exact}
    _end(ctx, "mps3-01-handover", board, up, viewer)
    return out


def check_png(ctx: Ctx) -> dict[str, Any]:
    board = FakeBoard(Card(handover=False), ctx.native)
    board.freeze()
    comp, up, viewer = _stream(ctx, board, "mps3-01-png")
    up.key_evt.wait(10)
    t0 = time.perf_counter()
    with urllib.request.urlopen(f"http://127.0.0.1:{ctx.daemon.port}/api/v1/boards/mps3-01-png/display.png",
                                timeout=10) as r:
        png = r.read()
    ms = (time.perf_counter() - t0) * 1e3
    a = array.array("H")
    a.frombytes(bytes(board.p.gram))
    want = b"".join(map(_RGB3.__getitem__, a))
    out = {"ok": png_pixels(png) == want, "png_B": len(png), "get_ms": round(ms, 1)}
    _end(ctx, "mps3-01-png", board, up, viewer)
    return out


# --- keyframe sizes of the real harness screens, the encoders' agreement, decode costs ---------------


def keyframe_bytes(native: Native, fb: bytes, valid: set[int] | frozenset[int] = ALL_TILES) -> dict[str, Any]:
    recs = native.enc_frame(fb, valid)
    parts = split_records(recs, MAX_MSG - (8 + UPD.size + 256 + 2))
    hist = [0] * 5
    i = 0
    while i < len(recs):
        _t, e, ln = TREC.unpack_from(recs, i)
        hist[e] += 1
        i += TREC.size + ln
    total = sum(8 + UPD.size + (256 if k == 0 else 4) + 2 + len(c) for k, (_n, c) in enumerate(parts))
    return {"B": total, "updates": len(parts), "enc": {k: v for k, v in zip(ENC_NAMES, hist, strict=False) if v}}


def real_screens(native: Native) -> dict[str, Any]:
    """The Linux lead's decoder spike (tests/spikes/lcd_mirror_decoder.py): the REAL harness screens
    rebuilt from clcd.c's byte stream (boot, link_down, banner, regain), fed into one panel shadow."""
    path = HM_ROOT / "tests" / "spikes" / "lcd_mirror_decoder.py"
    if not path.is_file():
        return {"skipped": f"{path} not present"}
    spec = importlib.util.spec_from_file_location("lcdm_decoder_spike", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lcdm_decoder_spike"] = mod
    try:
        spec.loader.exec_module(mod)
        shadow = mod.Hx8347dShadow()
        out: dict[str, Any] = {}
        prev = None
        for name in ("boot", "link_down", "banner", "regain"):
            shadow.feed(mod.load_stream(name))
            view = shadow.viewer().tobytes()
            row: dict[str, Any] = {"keyframe": keyframe_bytes(native, view)}
            if prev is not None:
                d = native.tile_diff(view, prev)
                row["delta_tiles"] = len(d)
                row["delta_B"] = (len(native.enc_frame(view, d)) + 8 + UPD.size + 4 + 2) if d else 0
            out[name] = row
            prev = view
        return out
    except Exception as exc:  # noqa: BLE001 - optional input
        return {"skipped": f"{type(exc).__name__}: {exc}"}


JS_DECODER = r"""
const fs = require('fs');
const dir = process.argv[2];
const LUT = new Uint32Array(65536);
for (let v = 0; v < 65536; v++) { const r5 = v >> 11, g6 = (v >> 5) & 63, b5 = v & 31;
  LUT[v] = ((255 << 24) | (((b5 << 3) | (b5 >> 2)) << 16) | (((g6 << 2) | (g6 >> 4)) << 8) | ((r5 << 3) | (r5 >> 2))) >>> 0; }
// The browser's decoder for one lcd_mirror UPDATE (the WS leg): tiles into a 320x240 RGB565 buffer.
function apply(buf, fb) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  if (buf[0] !== 0x4C || buf[1] !== 0x4D || buf[2] !== 2) throw new Error('not an UPDATE');
  let o = 8; const status = dv.getUint32(o + 16, true);
  o += 59; o += (status & (1 << 25)) ? 256 : 4;
  const n = dv.getUint16(o, true); o += 2;
  const t16 = new Uint16Array(256);
  for (let k = 0; k < n; k++) {
    const idx = dv.getUint16(o, true), enc = buf[o + 2], len = dv.getUint16(o + 3, true); o += 5;
    const p = o; o += len;
    if (enc === 0) { t16.fill(dv.getUint16(p, true)); }
    else if (enc === 1) { const c0 = dv.getUint16(p, true), c1 = dv.getUint16(p + 2, true);
      for (let y = 0; y < 16; y++) { const r = dv.getUint16(p + 4 + 2 * y, true);
        for (let x = 0; x < 16; x++) t16[y * 16 + x] = (r >> x) & 1 ? c1 : c0; } }
    else if (enc === 2) { const pal = [0, 1, 2, 3].map(i => dv.getUint16(p + 2 * i, true));
      for (let y = 0; y < 16; y++) { const r = dv.getUint32(p + 8 + 4 * y, true);
        for (let x = 0; x < 16; x++) t16[y * 16 + x] = pal[(r >>> (2 * x)) & 3]; } }
    else if (enc === 3) { let i = p, j = 0;
      while (i < p + len) { const t = buf[i++];
        if (t & 0x80) { const c = dv.getUint16(i, true); i += 2; for (let q = 0; q <= (t & 0x7f); q++) t16[j++] = c; }
        else { for (let q = 0; q <= t; q++) { t16[j++] = dv.getUint16(i, true); i += 2; } } }
      if (j !== 256) throw new Error('RLE16 length'); }
    else if (enc === 4) { for (let i = 0; i < 256; i++) t16[i] = dv.getUint16(p + 2 * i, true); }
    else throw new Error('enc ' + enc);
    const x0 = (idx % 20) * 16, y0 = Math.floor(idx / 20) * 16;
    for (let y = 0; y < 16; y++) fb.set(t16.subarray(y * 16, y * 16 + 16), (y0 + y) * 320 + x0);
  }
}
const out = {node: process.version};
for (const name of fs.readdirSync(dir).filter(f => f.endsWith('.upd')).sort()) {
  const buf = new Uint8Array(fs.readFileSync(dir + '/' + name));
  const want = new Uint16Array(new Uint8Array(fs.readFileSync(dir + '/' + name.replace('.upd', '.fb'))).buffer);
  const fb = new Uint16Array(76800), rgba = new Uint32Array(76800);
  apply(buf, fb);
  let same = fb.length === want.length; for (let i = 0; same && i < fb.length; i++) same = fb[i] === want[i];
  const K = 200; for (let k = 0; k < 20; k++) apply(buf, fb);
  let a = process.hrtime.bigint(); for (let k = 0; k < K; k++) apply(buf, fb);
  const dec = Number(process.hrtime.bigint() - a) / 1e3 / K;
  a = process.hrtime.bigint(); for (let k = 0; k < K; k++) for (let i = 0; i < 76800; i++) rgba[i] = LUT[fb[i]];
  const conv = Number(process.hrtime.bigint() - a) / 1e3 / K;
  out[name] = {bytes: buf.length, bit_exact_vs_python: same, decode_us: +dec.toFixed(1), rgba_full_frame_us: +conv.toFixed(1)};
}
console.log(JSON.stringify(out));
"""


def microbench(native: Native, scratch: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    frames = {"a_status": bytes(StatusScreen().gram), "b_card": bytes(Card(handover=False).gram),
              "c_noise": os.urandom(FB_BYTES)}
    out["c_and_python_encoders_agree"] = all(native.enc_frame(fb, ALL_TILES) == enc_frame_py(fb, ALL_TILES)
                                             for fb in frames.values())
    jsdir = scratch / "js"
    jsdir.mkdir(parents=True, exist_ok=True)
    for name, fb in frames.items():
        recs = native.enc_frame(fb, ALL_TILES)
        m = build_update(1, 0, 0, 0, KEY_BITS | S_FMT_OK, 0, valid_bytes(ALL_TILES), baseline_regs(), NT, recs)
        (jsdir / f"{name}.upd").write_bytes(m)
        (jsdir / f"{name}.fb").write_bytes(fb)
        t = time.thread_time()
        for _ in range(10):
            native.enc_frame(fb, ALL_TILES)
        out[f"board_host_encode_full_frame_ms[{name}]"] = round((time.thread_time() - t) / 10 * 1e3, 3)
        t = time.thread_time()
        for _ in range(5):
            u = parse_update(m[8:])
            f2 = bytearray(FB_BYTES)
            for tt, e, p in u["tiles"]:
                put_tile(f2, tt, decode_tile(e, p))
        out[f"hm_python_decode_keyframe_ms[{name}]"] = round((time.thread_time() - t) / 5 * 1e3, 2)
        if bytes(f2) != fb:
            out[f"hm_python_decode_exact[{name}]"] = False
        out[f"keyframe[{name}]"] = keyframe_bytes(native, fb)
    other = bytearray(frames["b_card"])
    other[-1] ^= 1
    t = time.thread_time()
    for _ in range(50):
        native.tile_diff(frames["b_card"], bytes(other))
    out["board_host_tile_diff_full_frame_ms"] = round((time.thread_time() - t) / 50 * 1e3, 3)
    t = time.perf_counter()
    png_of(frames["b_card"])
    out["hm_png_encode_ms"] = round((time.perf_counter() - t) * 1e3, 1)
    node = shutil.which("node")
    if node:
        js = scratch / "lcdm_decoder.js"
        js.write_text(JS_DECODER)
        try:
            r = subprocess.run([node, str(js), str(jsdir)], capture_output=True, text=True, timeout=90)
            line = (r.stdout.strip().splitlines() or [""])[-1]
            out["browser_js_decoder"] = json.loads(line) if r.returncode == 0 and line else r.stderr.strip()[:300]
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            out["browser_js_decoder"] = f"node failed: {exc}"
    else:
        out["browser_js_decoder"] = "node not found"
    return out


# --- main ----------------------------------------------------------------------------------------------

COLS = [("pattern", 8), ("flow", 5), ("hz", 3), ("cap", 6), ("B/upd", 6), ("key B", 6), ("kB/s", 6),
        ("ups", 5), ("fps_v", 5), ("p50ms", 7), ("p95ms", 7), ("rtt95", 7), ("enc_us", 7),
        ("diff_us", 7), ("hm_us", 7), ("ws_us", 6), ("exact", 5)]


def row(r: dict[str, Any]) -> str:
    cap = f"{r['rate'] / 1e3:.0f}k" if r["rate"] else "-"
    vals = [r["pattern"], r["flow"], r["hz"], cap, r["B_per_update"], r["keyframe_B"], r["kBps"],
            r["ups_board"], r["fps_viewer"], r["lat_ms_p50"], r["lat_ms_p95"], r["rtt_ms_p95"],
            r["board_enc_us"], r["board_diff_us"], r["hm_reader_us_per_update"], r["hm_ws_us_per_msg"],
            "yes" if r["exact"] and not r["crc_bad"] else "NO"]
    return " ".join(str(v).rjust(w) for v, (_n, w) in zip(vals, COLS, strict=False))


def header() -> str:
    return " ".join(n.rjust(w) for n, w in COLS)


def main() -> int:
    ap = argparse.ArgumentParser(description="LCD mirror transport spike (lcd_mirror §6.2 through HM's SshTunnel)")
    ap.add_argument("--quick", action="store_true", help="short runs (a smoke test of the spike)")
    ap.add_argument("--json", default="", help="write every result here")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ns = ap.parse_args()
    sys.setswitchinterval(0.001)          # one process, many threads: keep GIL hand-offs short
    wall0 = time.monotonic()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    d_unthr = 0.8 if ns.quick else 1.5
    d_thr = 1.2 if ns.quick else 2.5
    res: dict[str, Any] = {"hm_root": str(HM_ROOT), "scratch": str(SCRATCH)}
    native = Native(SCRATCH)
    res["native"] = native.kind + (f" ({native.why})" if native.why else "")
    ctx = Ctx(native)
    sect = [time.monotonic()]

    def lap(name: str) -> None:
        sect.append(time.monotonic())
        res.setdefault("section_s", {})[name] = round(sect[-1] - sect[-2], 1)
    try:
        print(f"LCD-mirror transport spike (lcd_mirror §6.2 + amendments)  HM={HM_ROOT}  board encoder={res['native']}")
        print(f"tunnel argv (fake ssh): {check_argv(ctx)['argv']}")
        print("\n== 1. unthrottled (loopback through SshTunnel + FakeSsh), per pattern and RATE ==")
        print("   B/upd: steady-state bytes per UPDATE; key B: keyframe bytes (all parts); ups: UPDATEs/s from the board;")
        print("   kB/s: received by HM; fps_v: distinct board SNAPs/s whose content reached the viewer;")
        print("   p50/p95: SNAP -> applied in the browser stand-in (ms); rtt95: PING->PONG p95 (ms, queued behind UPDATEs);")
        print("   enc_us/diff_us: board encode / tile-diff host CPU per SNAP with a change; hm_us: HM reader CPU per UPDATE;")
        print("   ws_us: daemon WS thread CPU per browser message")
        print(header())
        res["unthrottled"] = []
        for pat in ("a_status", "b_card", "c_noise"):
            for hz in (RATE_DEFAULT, RATE_MAX):
                r = run_case(ctx, pat, hz=hz, duration=d_unthr)
                res["unthrottled"].append(r)
                print(row(r), flush=True)
        lap("1_unthrottled")
        r = run_case(ctx, "a_status", hz=50, duration=0.5)        # RATE above the clamp: the echo
        res["rate_clamp"] = {"asked": r["hz_asked"], "echo": r["hz_echo"], "used": r["hz"]}
        print(f"  RATE 50 asked -> board clamps to {r['hz']} and echoes {r['hz_echo']}")
        print("\n== 2. link caps: 1.25 MB/s (10 Mb/s half duplex) and 250 kB/s; one 2 MiB SSH window modelled ==")
        print(header() + "   max buffered")
        res["throttled"] = []
        for rate in (1_250_000, 250_000):
            for pat, flow in (("a_status", "none"), ("b_card", "none"), ("c_noise", "none"), ("c_noise", "ack")):
                r = run_case(ctx, pat, hz=RATE_MAX, rate=rate, flow=flow, duration=d_thr, drain_s=14)
                res["throttled"].append(r)
                print(row(r) + f"   {r['max_buffered_B']}", flush=True)
        lap("2_throttled")
        print("\n== 3. fan-out: 4 viewers on one upstream: fast, slow (0.3 s/msg) with and without the WS ack, "
              "permessage-deflate ==")
        r = run_case(ctx, "c_noise", hz=RATE_MAX, duration=d_thr,
                     viewers=({}, {"slow_s": 0.3}, {"slow_s": 0.3, "ack": False}, {"deflate": True}),
                     drain_s=25)                       # the no-ack slow viewer has seconds of backlog to drain
        res["fanout"] = r
        for i, v in enumerate(r["viewers"]):
            print(f"  viewer{i}: ack={v['ack']} msgs={v['msgs']} keys={v['keys']} slow_s={v['slow_s']} ext={v['deflate']} "
                  f"crc_bad={v['crc_bad']} lat_p50={v['lat_ms_p50']} ms {v['error']}")
        print(f"  daemon WS cost {r['hm_ws_us_per_msg']} us/msg (all viewers); max dirty tiles held for a viewer "
              f"{r['viewer_max_dirty_tiles']}; exact={r['exact']} crc_ok={r['crc_ok']} crc_bad={r['crc_bad']}")
        a = run_case(ctx, "c_noise", hz=RATE_MAX, duration=d_unthr, viewers=({"deflate": True},))
        b = run_case(ctx, "c_noise", hz=RATE_MAX, duration=d_unthr, viewers=({},))
        res["deflate_cost"] = {"ws_us_per_msg_deflate": a["hm_ws_us_per_msg"], "ws_us_per_msg_plain": b["hm_ws_us_per_msg"],
                               "negotiated": a["viewers"][0]["deflate"]}
        print(f"  one viewer, noise: daemon WS cost {a['hm_ws_us_per_msg']} us/msg with permessage-deflate "
              f"({a['viewers'][0]['deflate']}) vs {b['hm_ws_us_per_msg']} us/msg without")
        lap("3_fanout")
        print("\n== 4. behaviours ==")
        res["checks"] = {"argv": check_argv(ctx), "refusal_3rd_client": check_refusal(ctx),
                         "seq_gap_key_recovery": check_gap(ctx), "no_service": check_no_service(ctx),
                         "drop_claim_loss_recover": check_drop_and_claim_loss(ctx),
                         "blind_sw_dut": check_blind(ctx), "handover_hatch": check_handover(ctx),
                         "png_snapshot": check_png(ctx)}
        for k, v in res["checks"].items():
            print(f"  {k:24s} {'PASS' if v['ok'] else 'FAIL'}  " +
                  json.dumps({kk: vv for kk, vv in v.items() if kk not in ('ok', 'argv')}, default=str))
        lap("4_checks")
        print("\n== 5. keyframes of the REAL harness screens (clcd.c byte stream, via lcd_mirror_decoder.py) ==")
        res["real_screens"] = real_screens(native)
        for k, v in res["real_screens"].items():
            print(f"  {k:10s} {json.dumps(v)}")
        lap("5_real")
        print("\n== 6. per-frame costs (host) and the browser decoder (node) ==")
        res["micro"] = microbench(native, SCRATCH)
        for k, v in res["micro"].items():
            print(f"  {k:44s} {json.dumps(v) if isinstance(v, (dict, list)) else v}")
        lap("6_micro")
        print(f"\nsection wall times (s): {res['section_s']}")
    finally:
        ctx.close()
        res["ports_bound"] = BOUND
        res["ports_ok"] = all(not any(lo <= p <= hi for lo, hi in FORBIDDEN) for _w, p in BOUND)
        res["leftover_ssh_procs"] = [p.pid for p in ctx.fake.live()]
        res["wall_s"] = round(time.monotonic() - wall0, 1)
        if not ns.keep:
            shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\nports bound: {len(BOUND)}, all outside 23300-23727/10000-19999: {res['ports_ok']}; "
          f"live fake-ssh procs after close: {res['leftover_ssh_procs']}; wall {res['wall_s']} s")
    if ns.json:
        Path(ns.json).write_text(json.dumps(res, indent=2, default=str) + "\n")
    bad = [r for r in res.get("unthrottled", []) + res.get("throttled", []) if not r["exact"] or r["crc_bad"]]
    ok = not bad and all(v["ok"] for v in res.get("checks", {}).values())
    print("RESULT:", "PASS" if ok else f"FAIL ({len(bad)} inexact runs)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
