#!/usr/bin/env python3
"""Build the Harness Manager app icons from one master SVG.

    python packaging/icon/build_icons.py [MASTER.svg] [--out DIR] [--preview]

MASTER defaults to ``candidates/A-board.svg``. Outputs (default ``packaging/icon/build/``):

* ``hicolor/<N>x<N>/apps/harness-manager.png`` for N in 16 24 32 48 64 128 256 512 1024
* ``harness-manager.ico``  (16 24 32 48 64 128 256, PNG entries)
* ``harness-manager.icns`` (16 32 64 128 256 512 1024, PNG entries; no iconutil needed)

``--preview`` also writes ``candidates/preview.png``: every candidate at 16/32/64/256 on a
light and a dark tray (needs Pillow). This is a dev tool: nothing here is a runtime
dependency of Harness Manager. The SVG is rasterised by the first of cairosvg,
rsvg-convert, ImageMagick (``magick`` / ``convert``) or PySide6 QtSvg that is present.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MASTER = HERE / "candidates" / "A-board.svg"
DEFAULT_OUT = HERE / "build"
NAME = "harness-manager"

PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: ICNS element type for a PNG payload of that pixel size.
ICNS_TYPES = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08",
              512: b"ic09", 1024: b"ic10"}


class NoRenderer(RuntimeError):
    pass


def _render_cairosvg(svg: Path, size: int) -> bytes:
    import cairosvg  # type: ignore[import-not-found]

    return cairosvg.svg2png(url=str(svg), output_width=size, output_height=size)


def _render_cli(cmd: list[str], out: Path) -> bytes:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out.read_bytes()


def _render_qt(svg: Path, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # type: ignore[import-not-found]
    from PySide6.QtGui import QGuiApplication, QImage, QPainter  # type: ignore[import-not-found]
    from PySide6.QtSvg import QSvgRenderer  # type: ignore[import-not-found]

    app = QGuiApplication.instance() or QGuiApplication(["icons", "-platform", "offscreen"])
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    QSvgRenderer(str(svg)).render(painter)
    painter.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    del app
    return bytes(data.data())


def renderer_name() -> str:
    for mod in ("cairosvg",):
        try:
            __import__(mod)
            return mod
        except Exception:  # noqa: BLE001
            pass
    for exe in ("rsvg-convert", "magick", "convert"):
        if shutil.which(exe):
            return exe
    try:
        __import__("PySide6.QtSvg")
        return "qtsvg"
    except Exception:  # noqa: BLE001
        raise NoRenderer("no SVG renderer: install cairosvg into the venv "
                         "(.venv/bin/pip install cairosvg) or rsvg-convert / ImageMagick") from None


def rasterize(svg: Path, size: int, renderer: str | None = None) -> bytes:
    """``svg`` as a ``size`` x ``size`` PNG (transparent corners)."""
    r = renderer or renderer_name()
    if r == "cairosvg":
        return _render_cairosvg(svg, size)
    if r == "qtsvg":
        return _render_qt(svg, size)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.png"
        if r == "rsvg-convert":
            return _render_cli([r, "-w", str(size), "-h", str(size), "-o", str(out), str(svg)], out)
        # ImageMagick: rasterise at 4x the density, then resize, for clean small sizes.
        density = max(72, size * 96 * 4 // 256)
        cmd = [r] if r == "magick" else ["convert"]
        return _render_cli(cmd + ["-background", "none", "-density", str(density), str(svg), "-strip",
                                  "-resize", f"{size}x{size}", f"PNG32:{out}"], out)


def make_ico(pngs: dict[int, bytes]) -> bytes:
    """A multi-size .ico whose entries are PNGs (Vista and later)."""
    sizes = sorted(pngs)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, body = b"", b""
    for s in sizes:
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(pngs[s]), offset)
        offset += len(pngs[s])
        body += pngs[s]
    return head + entries + body


def make_icns(pngs: dict[int, bytes]) -> bytes:
    """An .icns container: 'icns', total length, then (type, length, PNG) elements."""
    body = b""
    for s in sorted(ICNS_TYPES):
        data = pngs[s]
        body += ICNS_TYPES[s] + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def build(master: Path, out: Path, renderer: str | None = None) -> list[Path]:
    """Write every output; return the paths."""
    master = Path(master)
    if not master.is_file():
        raise FileNotFoundError(master)
    r = renderer or renderer_name()
    sizes = sorted(set(PNG_SIZES) | set(ICO_SIZES) | set(ICNS_TYPES))
    pngs = {s: rasterize(master, s, r) for s in sizes}
    written: list[Path] = []
    for s in PNG_SIZES:
        p = out / "hicolor" / f"{s}x{s}" / "apps" / f"{NAME}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pngs[s])
        written.append(p)
    ico = out / f"{NAME}.ico"
    ico.write_bytes(make_ico({s: pngs[s] for s in ICO_SIZES}))
    icns = out / f"{NAME}.icns"
    icns.write_bytes(make_icns(pngs))
    shutil.copyfile(master, out / f"{NAME}.svg")
    return written + [ico, icns, out / f"{NAME}.svg"]


def contact_sheet(candidates: list[Path], dest: Path, renderer: str | None = None) -> None:
    """One PNG: a row per candidate, 16/32/64/256 px on a light then a dark tray."""
    import io

    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    r = renderer or renderer_name()
    sizes = (16, 32, 64, 256)
    pad = 16
    row_w = sum(sizes) + pad * (len(sizes) + 1)
    row_h = 256 + 2 * pad
    sheet = Image.new("RGB", (row_w * 2, row_h * len(candidates)))
    draw = ImageDraw.Draw(sheet)
    for i, svg in enumerate(candidates):
        for half, bg in enumerate(((236, 238, 242), (32, 34, 40))):
            draw.rectangle([half * row_w, i * row_h, (half + 1) * row_w, (i + 1) * row_h], fill=bg)
            x = half * row_w + pad
            for s in sizes:
                im = Image.open(io.BytesIO(rasterize(svg, s, r))).convert("RGBA")
                sheet.paste(im, (x, i * row_h + pad + (256 - s) // 2), im)
                x += s + pad
        draw.text((4, i * row_h + 2), svg.stem, fill=(120, 120, 130))
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("master", nargs="?", type=Path, default=DEFAULT_MASTER)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--preview", action="store_true",
                    help="also write candidates/preview.png (needs Pillow)")
    args = ap.parse_args(argv)
    try:
        files = build(args.master, args.out)
        if args.preview:
            contact_sheet(sorted((HERE / "candidates").glob("*.svg")),
                          HERE / "candidates" / "preview.png")
    except (NoRenderer, FileNotFoundError, ImportError) as exc:
        print(f"build_icons: {exc}", file=sys.stderr)
        return 1
    for f in files:
        print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
