"""LCD-MIRROR spike 1: an HX8347-D 8080 write-stream decoder that rebuilds the MPS3 panel's
picture, checked pixel for pixel against independent renderings.

Board-free and stdlib-only. NOT a test (``tests/spikes`` is never collected, see
``tests/conftest.py``); run it by hand::

    python3 tests/spikes/lcd_mirror_decoder.py                    # all checks, a summary table
    python3 tests/spikes/lcd_mirror_decoder.py --png /tmp/out     # also write the rebuilt frames

What it proves
--------------
The design (``docs/design/LCD_MIRROR.md``) needs ONE model of "what does the panel show,
given the bytes on its 8080 bus". The same model is (a) the interim software shadow in
harnessd (fed the harness's own CMD/DATA writes), (c) the reference the static-shell bus
snooper's RTL is verified against, and (d) the render step that turns the snooper's
physical-GRAM copy + register shadow into the viewer's 320x240 picture. This spike is
that model, run against three byte streams nobody wrote for it:

1. **The real harness renderer.** ``lcd_mirror_data/*.stream.z`` is the exact {RS,byte}
   stream ``firmware/clcd/clcd.c`` + ``hx8347_init.c`` (platform ``feat/linux-harness``
   4956d88, the code harnessd runs) pushes into clcd_0's CMD/DATA registers, captured
   with the mock HAL by ``lcd_mirror_data/fwstream.c``: boot (panel reset, the whole init
   table, first paint), an incremental repaint (link down: red rows), the DUT-OSD banner,
   and a KVM regain (reset, re-init, full repaint). The reference picture is rendered from
   the renderer's own 40x15 grid by HM's ``tools/clcd_mock.py`` (its font JSON and
   rasteriser; a separate code path from ``clcd.c``'s ``build_cell``).
2. **The clcd_demo DUT RM** (the byte sequence ``tests/clcd_demo/card_model.py`` in the
   platform repo says the RM emits for frame N), after a KVM handover reset, against that
   model's own ``card_pixel()``.
3. **The nanosoc ``ahb_clcd`` demo** (``fpga/rp/nanosoc_exp/sw/ahb_clcd.c``: the same init
   table, then four ``fill_rect``s), re-stated here, against the rectangles.

Plus two negative controls (a mirror that cannot fail proves nothing): a single flipped
pixel byte must be caught, and the as-vendored MADCTL 0xE0 must come out rotated 180
degrees (CLCD_PANEL_FACTS.md 7.4: that is what 0xE0 does to this upside-down-mounted
glass).

What is modelled, and what is ASSUMED (datasheet/board to confirm; see the design doc)
---------------------------------------------------------------------------------------
* Bus: a byte is latched on WR rising edge with CS low; RS=0 index, RS=1 data. PROVEN.
* RGB565 over 8 bits, high byte first, COLMOD 0x17=0x05. PROVEN (CLCD_PANEL_FACTS.md 2-3).
* Registers are 8-bit, one datum per index; 0x22 is the GRAM port. PROVEN for every
  writer in the tree (all three use the one firmware table and the same window protocol).
* A command byte discards a half-received pixel. PROVEN by construction: the init table
  ends ``0x22, 0x00`` (one byte) and the glyph stream that follows is legible on glass.
* The address counter restarts at (SC, SP) on 0x22 AND on a write to a start register.
  ASSUMED; every in-tree writer rewrites the whole window and then 0x22, so both
  readings give the same picture for them.
* Logical -> GRAM mapping from MADCTL MY/MX/MV. Only the RELATIVE transform to the
  board-proven anchor (MADCTL 0x20, PANEL 0x36 = 0x09 reads right way up) is claimed;
  the MX-vs-MY convention under MV is ASSUMED and flagged ``uncalibrated``. The run checks
  that it equals the snooper spec's direct viewer mapping (platform ``feat/linux-harness``
  5607f11 ``docs/planning/linux_lanes/LCD_MIRROR_FPGA.md`` 2.4, ``flip_conv=0``) on every
  pixel of all eight geometries, so this model can serve as that doc's 8.1 golden model.
* Anything off the calibrated set (MADCTL not 0x20/0xE0, PANEL 0x36 != 0x09, display mode
  0x01 != 0, COLMOD != 0x05, any scroll/partial register written) raises the
  ``uncalibrated`` flag rather than pretending to be exact.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import struct
import sys
import time
import zlib
from array import array
from pathlib import Path

sys.dont_write_bytecode = True           # never drop __pycache__ into a read-only tree

HERE = Path(__file__).resolve().parent
DATA = HERE / "lcd_mirror_data"
HM_ROOT = HERE.parents[1]
PLATFORM = Path(os.environ.get("LCDM_PLATFORM", "/home/dam1n19/SoCLabs/mps3-nanosoc-platform"))

W, H = 320, 240                  # viewer: the landscape picture a person at the bench reads
GW, GH = 240, 320                # HX8347-D native GRAM: 240 source columns x 320 gate lines
MADCTL_ANCHOR = 0x20             # board-PROVEN: reads right way up (hx8347_init.h CLCD_ROTATE_180=1)
PANEL_ANCHOR = 0x09              # board-PROVEN companion value of reg 0x36
CALIBRATED_MADCTL = {0x20, 0xE0}
SCROLL_PARTIAL_REGS = set(range(0x0A, 0x16))   # partial area + vertical scroll registers
RS_CMD, RS_DATA, RS_CTRL = 0, 1, 2              # RS_CTRL: clcd_0 CTRL write (bit2 RESET_N, bit1 BL)
TILE = 16


def _defaults() -> bytearray:
    """Register file after a panel reset. Only the registers the mirror interprets matter,
    and every in-tree writer rewrites all of them after each reset; the values here are
    ASSUMED (native portrait full window, display off) until read off the datasheet."""
    r = bytearray(256)
    r[0x04], r[0x05] = 0x00, 0xEF          # column end 239 (native portrait)
    r[0x08], r[0x09] = 0x01, 0x3F          # row end 319
    r[0x17] = 0x06                         # COLMOD: assume the 18-bit reset default
    r[0x28] = 0x00                         # display off
    return r


class Hx8347dShadow:
    """The panel, as far as the 8080 bus can tell. Feed it (rs, byte) records."""

    def __init__(self) -> None:
        self.gram = array("H", bytes(GW * GH * 2))
        self.known = bytearray(GW * GH)          # 0 = never written since power-on
        self.dirty: set[int] = set()             # GRAM tiles WRITTEN (physical, 16x16)
        self.changed: set[int] = set()           # GRAM tiles where a write CHANGED a pixel
        self.rst_n = False
        self.bl = False
        self.anomalies: dict[str, int] = {}
        self.written_regs: set[int] = set()
        self.pixels = 0
        self._reset_regs()

    # --- state -------------------------------------------------------------------------
    def _reset_regs(self) -> None:
        self.regs = _defaults()
        self.index = None
        self.pend: list[int] = []
        self.written_regs.clear()
        self._load_ac()

    def _note(self, what: str) -> None:
        self.anomalies[what] = self.anomalies.get(what, 0) + 1

    def _w16(self, hi: int, lo: int) -> int:
        return (self.regs[hi] << 8) | self.regs[lo]

    def window(self) -> tuple[int, int, int, int]:
        return self._w16(2, 3), self._w16(4, 5), self._w16(6, 7), self._w16(8, 9)

    def _load_ac(self) -> None:
        sc, _, sp, _ = self.window()
        self.col, self.page = sc, sp

    # --- bus ---------------------------------------------------------------------------
    def ctrl(self, byte: int) -> None:
        rst_n = bool(byte & 0x04)
        if not rst_n:
            self._reset_regs()                   # held in reset: registers to defaults
        self.rst_n = rst_n
        self.bl = bool(byte & 0x02)

    def cmd(self, b: int) -> None:
        if not self.rst_n:
            return self._note("write_in_reset")
        if self.pend:
            self._note("half_pixel_discarded")
        self.pend = []
        self.index = b
        if b == 0x22:
            self._load_ac()

    def data(self, b: int) -> None:
        if not self.rst_n:
            return self._note("write_in_reset")
        if self.index is None:
            return self._note("data_before_index")
        if self.index != 0x22:
            if self.index in self.written_regs and self.index not in (2, 3, 4, 5, 6, 7, 8, 9):
                pass                             # rewrites are normal (0x28 twice in init)
            self.regs[self.index] = b
            self.written_regs.add(self.index)
            if self.index in (2, 3, 6, 7):
                self._load_ac()
            return
        self.pend.append(b)
        colmod = self.regs[0x17]
        if colmod == 0x05:
            if len(self.pend) < 2:
                return
            px = (self.pend[0] << 8) | self.pend[1]
        elif colmod == 0x06:
            if len(self.pend) < 3:
                return
            r, g, bb = self.pend
            px = ((r >> 3) << 11) | ((g >> 2) << 5) | (bb >> 3)   # 18 bpp truncated to 565
            self._note("colmod_18bpp_truncated")
        else:
            self.pend = []
            return self._note(f"colmod_0x{colmod:02X}_unsupported")
        self.pend = []
        self._put(px)

    def _put(self, px: int) -> None:
        madctl = self.regs[0x16]
        g = self.phys(self.col, self.page, madctl)
        if g is None:
            self._note("pixel_out_of_gram")
        else:
            tile = (g // GW // TILE) * (GW // TILE) + (g % GW) // TILE
            if self.gram[g] != px:
                self.changed.add(tile)
            self.gram[g] = px
            self.known[g] = 1
            self.dirty.add(tile)
        self.pixels += 1
        sc, ec, sp, ep = self.window()
        self.col += 1
        if self.col > ec:
            self.col = sc
            self.page += 1
            if self.page > ep:
                self.page = sp

    @staticmethod
    def phys(col: int, page: int, madctl: int) -> int | None:
        """Logical (column counter, page counter) -> GRAM index. MV exchanges the counters;
        MX/MY then mirror the physical axes (the convention is ASSUMED; see module doc)."""
        if madctl & 0x20:
            gx, gy = page, col
        else:
            gx, gy = col, page
        if not (0 <= gx < GW and 0 <= gy < GH):
            return None
        if madctl & 0x40:
            gx = GW - 1 - gx
        if madctl & 0x80:
            gy = GH - 1 - gy
        return gy * GW + gx

    def feed(self, records) -> None:
        cmd, data, ctrl = self.cmd, self.data, self.ctrl
        for rs, b in records:
            if rs == RS_DATA:
                data(b)
            elif rs == RS_CMD:
                cmd(b)
            else:
                ctrl(b)

    # --- what a person sees ------------------------------------------------------------
    _VIEW = None

    @classmethod
    def _view_map(cls) -> array:
        """viewer (x, y) -> GRAM index, fixed by the board-proven anchor: under MADCTL 0x20 the
        renderer's logical (col, row) is exactly where a person reads it."""
        if cls._VIEW is None:
            cls._VIEW = array("I", (cls.phys(x, y, MADCTL_ANCHOR) for y in range(H) for x in range(W)))
        return cls._VIEW

    def viewer(self) -> array:
        g = self.gram
        return array("H", (g[i] for i in self._view_map()))

    def unknown_in_view(self) -> int:
        k = self.known
        return sum(1 for i in self._view_map() if not k[i])

    def flags(self) -> dict:
        r = self.regs
        unc = []
        if r[0x16] not in CALIBRATED_MADCTL:
            unc.append(f"MADCTL=0x{r[0x16]:02X}")
        if r[0x36] != PANEL_ANCHOR:
            unc.append(f"PANEL=0x{r[0x36]:02X}")
        if r[0x01]:
            unc.append(f"DISPMODE=0x{r[0x01]:02X}")
        if r[0x17] != 0x05:
            unc.append(f"COLMOD=0x{r[0x17]:02X}")
        if self.written_regs & SCROLL_PARTIAL_REGS:
            unc.append("scroll/partial")
        return {"rst_n": self.rst_n, "bl": self.bl,
                "display_on": (r[0x28] & 0x0C) == 0x0C,
                "standby": bool(r[0x1F] & 0x01),
                "uncalibrated": unc}


# --- stream sources ---------------------------------------------------------------------


def load_stream(name: str) -> list[tuple[int, int]]:
    raw = zlib.decompress((DATA / f"{name}.stream.z").read_bytes())
    return list(zip(raw[0::2], raw[1::2], strict=True))


def ctrl_reset_pulse() -> list[tuple[int, int]]:
    """The KVM's S_RST as the snooper sees it on the pads: CLCD_RST low (BL forced off), then
    released with the backlight on (clcd_kvm.sv:838-839)."""
    return [(RS_CTRL, 0x01), (RS_CTRL, 0x07)]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def clcd_demo_frame(frame: int):
    """(stream, expected viewer picture) for clcd_demo frame N, from the platform's card model."""
    sys.path.insert(0, str(PLATFORM / "tests" / "clcd_demo"))
    sys.path.insert(0, str(PLATFORM / "fpga" / "rp" / "clcd_demo"))
    cm = _load_module("card_model", PLATFORM / "tests" / "clcd_demo" / "card_model.py")
    table, defines = cm.load_firmware_table(PLATFORM)
    stream = cm.frame_sequence(table, defines, W, H, frame)
    lay = cm.Layout(W, H)
    want = array("H", (cm.card_pixel(x, y, lay, frame) for y in range(H) for x in range(W)))
    return stream, want


def nanosoc_demo():
    """ahb_clcd.c's panel_init() + draw_demo_frame(), re-stated (fpga/rp/nanosoc_exp/sw/ahb_clcd.c)."""
    sys.path.insert(0, str(PLATFORM / "fpga" / "rp" / "clcd_demo"))
    fw = _load_module("hx8347_table", PLATFORM / "fpga" / "rp" / "clcd_demo" / "hx8347_table.py")
    table, d = fw.load(PLATFORM)
    s = [((RS_CMD if e.op == d["HX_CMD"] else RS_DATA), e.val) for e in table if e.op != d["HX_DLY"]]

    def rgb(r, g, b):
        return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

    rects = [(0, 0, W, H, rgb(0, 0, 40)), (40, 60, 60, 120, rgb(255, 0, 0)),
             (130, 60, 60, 120, rgb(0, 255, 0)), (220, 60, 60, 120, rgb(0, 0, 255))]
    want = array("H", bytes(W * H * 2))
    for x, y, w, h, c in rects:
        x1, y1 = x + w - 1, y + h - 1
        for reg, val in ((2, x >> 8), (3, x & 0xFF), (4, x1 >> 8), (5, x1 & 0xFF),
                         (6, y >> 8), (7, y & 0xFF), (8, y1 >> 8), (9, y1 & 0xFF)):
            s += [(RS_CMD, reg), (RS_DATA, val)]
        s.append((RS_CMD, 0x22))
        s += [(RS_DATA, c >> 8), (RS_DATA, c & 0xFF)] * (w * h)
        for yy in range(y, y + h):
            want[yy * W + x:yy * W + x + w] = array("H", [c]) * w
    return s, want


def grid_reference(name: str) -> array:
    """The renderer's 40x15 grid, rasterised by HM's tools/clcd_mock.py, back to RGB565."""
    mock = _load_module("clcd_mock", HM_ROOT / "tools" / "clcd_mock.py")
    frame = mock.frame_from_preview((DATA / f"{name}.grid").read_text().splitlines())
    lines = mock.rasterise(frame, mock.load_font(), mock.TODAY)
    out = array("H")
    for line in lines:
        for i in range(0, len(line), 3):
            r, g, b = line[i], line[i + 1], line[i + 2]
            out.append(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return out


# --- measuring --------------------------------------------------------------------------


def diff(got: array, want: array) -> tuple[int, str]:
    bad = [i for i in range(W * H) if got[i] != want[i]]
    if not bad:
        return 0, ""
    i = bad[0]
    return len(bad), f"first at ({i % W},{i // W}) got 0x{got[i]:04X} want 0x{want[i]:04X}"


def rle16(px: array) -> bytes:
    out = bytearray()
    i, n = 0, len(px)
    while i < n:
        j = i + 1
        while j < n and px[j] == px[i] and j - i < 0xFFFF:
            j += 1
        out += struct.pack("<HH", j - i, px[i])
        i = j
    return bytes(out)


def dirty_payload(shadow: Hx8347dShadow, view: array, prev: array | None) -> dict:
    """Size of the update since the last call, in viewer-space 16x16 tiles (they map to GRAM
    tiles 1:1 because 240 and 320 are multiples of 16). ``wr`` = tiles WRITTEN (what a
    dirty-on-write snooper flags); ``chg`` = tiles whose pixels CHANGED (what a
    compare-on-write snooper flags, and all the wire needs to carry)."""
    vm = Hx8347dShadow._view_map()
    written = set()
    for vi, gi in enumerate(vm):
        t = (gi // GW // TILE) * (GW // TILE) + (gi % GW) // TILE
        if t in shadow.dirty:
            written.add(((vi // W) // TILE, (vi % W) // TILE))
    shadow.dirty.clear()
    raw = rle = 0
    blob = bytearray()
    changed = 0
    for ty, tx in sorted(written):
        tile = array("H")
        old = array("H")
        for y in range(ty * TILE, ty * TILE + TILE):
            tile.extend(view[y * W + tx * TILE:y * W + tx * TILE + TILE])
            if prev is not None:
                old.extend(prev[y * W + tx * TILE:y * W + tx * TILE + TILE])
        if prev is not None and tile == old:
            continue
        changed += 1
        raw += len(tile) * 2
        rle += len(rle16(tile))
        blob += tile.tobytes()
    return {"wr": len(written), "chg": changed, "raw": raw, "rle16": rle,
            "zlib1": len(zlib.compress(bytes(blob), 1)) if blob else 0}


def write_png(path: Path, view: array, scale: int = 2) -> None:
    rows = bytearray()
    for y in range(H):
        line = bytearray()
        for x in range(W):
            v = view[y * W + x]
            line += bytes((((v >> 11) & 0x1F) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63, (v & 0x1F) * 255 // 31)) * scale
        rows += (b"\x00" + line) * scale

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W * scale, H * scale, 8, 2, 0, 0, 0))
    path.write_bytes(png + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


# --- the run ----------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--png", type=Path, help="write the rebuilt viewer frames here")
    args = ap.parse_args(argv)
    if args.png:
        args.png.mkdir(parents=True, exist_ok=True)

    rows, fails = [], 0
    panel = Hx8347dShadow()
    last = {"view": None}

    def check(label, stream, want, *, expect_equal=True):
        nonlocal fails
        t0 = time.perf_counter()
        panel.feed(stream)
        dt = time.perf_counter() - t0
        view = panel.viewer()
        nbad, where = diff(view, want)
        ok = (nbad == 0) == expect_equal
        fails += not ok
        size = dirty_payload(panel, view, last["view"])
        last["view"] = view
        rows.append((label, len(stream), panel.unknown_in_view(), nbad, "PASS" if ok else "FAIL",
                     size, dt, panel.flags(), where))
        if args.png:
            write_png(args.png / f"{label}.png", view)
        return view

    # 1. the real harness renderer: boot -> link down -> DUT-OSD banner
    for name in ("boot", "link_down", "banner"):
        check(f"harness:{name}", load_stream(name), grid_reference(name))
    # 2. KVM handover to the DUT: reset pulse, then clcd_demo frame 5 (init + window + card)
    have_platform = (PLATFORM / "tests" / "clcd_demo" / "card_model.py").exists()
    if have_platform:
        stream, want = clcd_demo_frame(5)
        check("dut:clcd_demo#5", ctrl_reset_pulse() + stream, want)
        stream, want = clcd_demo_frame(6)
        check("dut:clcd_demo#6", stream, want)           # the RM's next pass: re-init + repaint
        stream, want = nanosoc_demo()
        check("dut:nanosoc_ahb_clcd", ctrl_reset_pulse() + stream, want)
    # 3. back to the harness: KVM reset, re-init, full repaint (clcd_regain)
    check("harness:regain", ctrl_reset_pulse() + load_stream("regain"), grid_reference("regain"))

    # negative controls
    boot = load_stream("boot")
    ref = grid_reference("boot")
    k = next(i for i in range(len(boot) - 1, 0, -1) if boot[i][0] == RS_DATA)
    flipped = boot[:k] + [(RS_DATA, boot[k][1] ^ 0x01)] + boot[k + 1:]
    panel = Hx8347dShadow()
    last["view"] = None
    check("neg:one-bit-flip", flipped, ref, expect_equal=False)
    m = next(i for i in range(len(boot) - 1) if boot[i] == (RS_CMD, 0x16))
    vendored = boot[:m + 1] + [(RS_DATA, 0xE0)] + boot[m + 2:]
    rot = array("H", (ref[(H - 1 - y) * W + (W - 1 - x)] for y in range(H) for x in range(W)))
    panel = Hx8347dShadow()
    last["view"] = None
    check("neg:MADCTL-0xE0=rot180", vendored, rot)

    # the Linux lead's snooper spec maps MADCTL to viewer coordinates directly
    # (LCD_MIRROR_FPGA.md 2.4, flip_conv=0); this model maps through physical GRAM and the
    # 0x20 anchor. They must agree on every pixel of all eight geometries.
    inv = {g: i for i, g in enumerate(Hx8347dShadow._view_map())}
    geo_bad = 0
    for mad in range(0x00, 0x100, 0x20):
        lw, lh = (W, H) if mad & 0x20 else (H, W)
        for y in range(lh):
            for x in range(lw):
                g0, s0 = (x, y) if mad & 0x20 else (y, x)
                want = ((W - 1 - g0) if mad & 0x80 else g0, (H - 1 - s0) if mad & 0x40 else s0)
                vi = inv[Hx8347dShadow.phys(x, y, mad)]
                geo_bad += (vi % W, vi // W) != want
    fails += geo_bad != 0
    print(f"spec: MADCTL x8 geometries vs LCD_MIRROR_FPGA.md 2.4: {geo_bad} pixel mismatches "
          f"({'PASS' if not geo_bad else 'FAIL'})")

    print(f"{'stream':24} {'bytes':>7} {'unknown':>7} {'badpx':>6} {'':4}  {'wr/chg':>7} {'raw B':>7} "
          f"{'rle16 B':>7} {'zlib1 B':>7} {'py ms':>6}  flags")
    for label, n, unk, nbad, verdict, size, dt, fl, where in rows:
        flag = ",".join(k for k in ("bl", "display_on") if fl[k]) + (" UNCAL:" + "/".join(fl["uncalibrated"]) if fl["uncalibrated"] else "")
        print(f"{label:24} {n:7d} {unk:7d} {nbad:6d} {verdict:4}  {size['wr']:3d}/{size['chg']:<3d} {size['raw']:7d} "
              f"{size['rle16']:7d} {size['zlib1']:7d} {dt * 1e3:6.0f}  {flag} {where}")
    if not have_platform:
        print(f"(platform repo not at {PLATFORM}: the DUT streams were skipped; set LCDM_PLATFORM)")
    print("RESULT:", "PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
