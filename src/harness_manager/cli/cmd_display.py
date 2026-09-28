"""``harness-manager display TARGET snapshot|status|show``: the board's LCD, pixel for pixel.

The Live display mirrors the board's 320x240 CLCD (docs/design/LCD_MIRROR.md §7). Verbs::

    harness-manager display TARGET snapshot [-o FILE] [--scale 1..4] [--hatch] [--raw]
    harness-manager display TARGET status
    harness-manager display TARGET show [--rate HZ] [--for SECONDS]

``snapshot`` writes the current picture as a PNG (``--raw``: the panel's own 153,600 bytes
of RGB565 little-endian) and prints the path and one status line. ``status`` prints the
mirror's state; ``--json`` gives the whole status object (the daemon's ``GET .../display``).
``show`` is a terminal view: half-block truecolor at a reduced scale, redrawn in place,
when stdout is a terminal that says it has truecolor (``COLORTERM=truecolor``); otherwise
one status line per refresh. Ctrl-] exits, as in ``console``.

**Who sees it.** The lease holder only, on a board behind a hub (exit 4 names the holder);
a board without the Linux harness's ``lcd_mirror`` exits 12 with the reason. Nothing
touches the panel: the view is read-only (no touch).

**Where it runs.** Through harness-manager-daemon when one runs (its routes: the one
upstream per board the web UI shares); otherwise in this process, with a compositor of its
own over the board pack's ``display_adapter`` hook, closed when the verb ends. The refusal
rules are the daemon's own (``daemon/display_api.refusal``), so both paths refuse alike.

``display`` is not ``lab TARGET display``: that one says (or flips) who drives the panel,
the harness or the DUT.

Exit codes: 0 done; 2 bad arguments; 3 the board is not open or not there; 4 someone else
holds the board's lease; 7 the board or the daemon did not answer; 12 no live display here
(the reason says why, e.g. "needs the Linux harness with lcd_mirror").
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import math
import os
import re
import shutil
import sys
import threading
import time
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from harness_manager.client.display import PictureModel
from harness_manager.core.display import (
    DISPLAY_MIRROR,
    FRAME_BYTES,
    HATCH_RGB565,
    TILE,
    TILES_X,
    H,
    W,
    owner_name,
    rgb888,
)
from harness_manager.core.errors import (
    ExitCode,
    HarnessError,
    HeldError,
    UnavailableError,
    UsageError,
)

from .context import VIA_HELP, VIA_METAVAR, Ctx, Stopper
from .output import Result, tsv_line

DEFAULT_RATE_HZ = 4.0
RATE_MIN_HZ, RATE_MAX_HZ = 0.2, 30.0
#: How long an in-process snapshot waits for the first whole picture (the daemon's PNG route
#: waits as long). FIX-PACK-1: 30 s, not 10: a cold start over ``ssh -J hub root@board``
#: spends most of 10 s bringing the SSH forward up, and the first try timed out on silicon.
PICTURE_WAIT_S = 30.0
#: Human mode: a snapshot still waiting after this long says what it waits for, and again
#: every ``PROGRESS_EVERY_S`` (stderr; never with --json or --tsv).
PROGRESS_AFTER_S = 0.5
PROGRESS_EVERY_S = 10.0
OPENING = ("opening the SSH forward to the board's lcd_mirror and waiting for the first "
           "picture (up to {wait:g} s)...")
#: The compositor clocks of an in-process view (None: the service's own). A test seam.
TIMINGS: Any = None
TARGET_HELP = "shell address host[:port] (the board's harness)"
WHAT = ("the board's LCD, pixel for pixel: snapshot, status, show (read-only; on a board "
        "behind a hub, the lease holder only)")

#: Half-block rendering: the upper half is the foreground colour, the lower the background.
HALF = "▀"
ALT_SCREEN_ON, ALT_SCREEN_OFF = "\x1b[?1049h\x1b[?25l", "\x1b[0m\x1b[?25h\x1b[?1049l"
CTRL_C = b"\x03"


# --- the parser -----------------------------------------------------------------------------------


def _fmt() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return p


def _via() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--via", metavar=VIA_METAVAR, default=argparse.SUPPRESS, help=VIA_HELP)
    return p


def _rate(text: str) -> float:
    try:
        hz = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not (RATE_MIN_HZ <= hz <= RATE_MAX_HZ) or math.isnan(hz):
        raise argparse.ArgumentTypeError(f"{text} is outside {RATE_MIN_HZ:g}-{RATE_MAX_HZ:g} Hz")
    return hz


def _seconds(text: str) -> float:
    try:
        s = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if s < 0 or math.isnan(s):
        raise argparse.ArgumentTypeError("must be 0 or more")
    return s


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``display`` to the top-level subparsers. Returns its parser."""
    fmt, via = _fmt(), _via()
    epilog = ("Not `lab TARGET display`, which says or flips who drives the panel (the "
              "harness or the DUT).")
    vp = subparsers.add_parser(
        "display", help=WHAT,
        description="The Live display: the board's 320x240 LCD mirrored pixel for pixel "
                    "(the Linux harness's lcd_mirror). Read-only: nothing is sent to the "
                    "panel. On a board behind a hub only the lease holder sees it.",
        epilog=epilog, parents=[fmt, via])
    vp.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    sub = vp.add_subparsers(dest="display_cmd", required=True, metavar="ACTION")

    def cols(layout: str) -> str:
        from .output import TSV_COLUMNS

        return f"--tsv columns: {' '.join(TSV_COLUMNS[layout])}"

    ap = sub.add_parser("snapshot", help="write the current picture to a PNG (or raw RGB565)",
                        description="Write the current picture to a file and print its path "
                                    "and the mirror's state: mode, owner, badges, and whether "
                                    "the picture is exact.",
                        parents=[fmt, via], epilog=cols("display snapshot"))
    ap.add_argument("-o", "--out", default=None, metavar="FILE",
                    help="where to write it (a directory: a new file in it); default "
                         "display-<board>-<time>.png (or .rgb565) here")
    ap.add_argument("--scale", type=int, choices=(1, 2, 3, 4), default=1,
                    help="panel pixels per PNG pixel, each way (2 gives 640x480)")
    ap.add_argument("--hatch", action="store_true",
                    help="hatch the tiles the mirror does not know yet, in grey")
    ap.add_argument("--raw", action="store_true",
                    help="the panel's own pixels: 153,600 bytes of RGB565 little-endian, "
                         "row-major (no --scale, no --hatch)")
    sub.add_parser("status", help="the mirror's state, mode, owner, badges, rate",
                   description="The Live display's state (--json: every field), and whether "
                               "it can open here, with the reason when not.",
                   parents=[fmt, via], epilog=cols("display status"))
    ap = sub.add_parser("show", help="a live view in this terminal (Ctrl-] exits)",
                        description="A live view in this terminal: half-block truecolor at a "
                                    "reduced scale, redrawn in place, when the terminal has "
                                    "truecolor; otherwise one status line per refresh. "
                                    "Ctrl-] exits (Ctrl-C too).",
                        parents=[fmt, via], epilog=cols("display show"))
    ap.add_argument("--rate", type=_rate, default=DEFAULT_RATE_HZ, metavar="HZ",
                    help=f"refreshes a second, {RATE_MIN_HZ:g}-{RATE_MAX_HZ:g} "
                         f"(default {DEFAULT_RATE_HZ:g})")
    ap.add_argument("--for", dest="for_s", type=_seconds, default=None, metavar="SECONDS",
                    help="show it this long, then stop (default: until Ctrl-])")
    vp.set_defaults(fn=cmd_display)
    return vp


# --- the two ways to the picture ------------------------------------------------------------------


def _explain(exc: HarnessError) -> HarnessError:
    """A HELD refusal always names the holder in its message."""
    if isinstance(exc, HeldError) and exc.holder and exc.holder not in exc.message:
        exc.message = f"{exc.message} (the lease holder: {exc.holder})"
    return exc


def _lease_service(engine: Any, session: Any) -> Any | None:
    """The hub API's lease view, for a board behind a hub (as the daemon shares its own)."""
    if getattr(session, "hub", None) is None:
        return None
    from harness_manager.services.lease import LeaseService

    state = getattr(engine, "state_dir", None)
    if state is None:
        from harness_manager.settings.files import config_dir

        state = config_dir()
    return LeaseService(Path(state))


class _LocalView:
    """A view on this process's compositor: a viewer that acks after drawing."""

    def __init__(self, svc: Any, board_id: str, adapter: Any, rate: int) -> None:
        self._news = threading.Event()
        self.model = PictureModel()
        self.viewer = svc.attach(board_id, adapter, ack=True, rate=rate, wake=self._news.set)

    def status(self) -> dict[str, Any]:
        return self.viewer.status()

    def _drain(self) -> bool:
        changed = False
        while True:
            msg = self.viewer.next_message()
            if msg is None:
                return changed
            self.viewer.ack(self.model.apply(msg))
            changed = True

    def pump(self, timeout: float) -> bool:
        self._news.clear()
        changed = self._drain()
        if not changed and timeout > 0:
            self._news.wait(timeout)
            self._news.clear()
            changed = self._drain()
        if self.viewer.ended:
            err = self.viewer.end_error
            raise err if err is not None else UnavailableError(
                DISPLAY_MIRROR, self.viewer.status().get("reason") or "the live display closed")
        return changed

    def close(self) -> None:
        self.viewer.close()


class _Local:
    """In-process: a compositor of this process's own over the pack's adapter."""

    def __init__(self, engine: Any, session: Any) -> None:
        from harness_manager.daemon.display_api import display_source

        self.engine, self.session = engine, session
        self.board_id = str(session.candidate.board_id)
        self.leases = _lease_service(engine, session)
        self.adapter = display_source(engine, session)
        shared = getattr(engine, "display", None)
        if shared is not None and callable(getattr(shared, "attach", None)):
            self.svc, self._owned = shared, False
        else:
            from harness_manager.services.display import DisplayService

            kw = {} if TIMINGS is None else {"timings": TIMINGS}
            self.svc, self._owned = DisplayService(engine, leases=self.leases, **kw), True

    def _refusal(self) -> HarnessError | None:
        from harness_manager.daemon.display_api import refusal

        return refusal(self.adapter, self.session, self.leases)

    def _check(self) -> None:
        err = self._refusal()
        if err is not None:
            raise err

    def status(self) -> dict[str, Any]:
        from harness_manager.daemon.display_api import STATUS_DEFAULTS

        try:
            err = self._refusal()
        except HarnessError as exc:                   # the lease could not be read: say so
            err = exc
        why = "" if err is None else getattr(err, "reason", "") or err.message
        return {"board_id": self.board_id, "available": err is None, "unavailable": why,
                **STATUS_DEFAULTS, **self.svc.status(self.board_id)}

    def still(self, *, fmt: str, scale: int, hatch: bool) -> tuple[bytes, dict[str, str]]:
        self._check()
        pic = self.svc.picture(self.board_id, self.adapter, wait_s=PICTURE_WAIT_S)
        data = pic.rgb565 if fmt == "raw" else pic.png(scale=scale, hatch=hatch)
        return data, {"x-display-seq": str(pic.seq), "x-display-hatched": str(len(pic.hatched)),
                      "x-display-owner": owner_name(pic.owner)}

    def view(self, *, rate: int) -> _LocalView:
        self._check()
        return _LocalView(self.svc, self.board_id, self.adapter, rate)

    def close(self) -> None:
        if self._owned:
            self.svc.shutdown()                      # the upstream closes, the forward drops


class _Remote:
    """Through harness-manager-daemon: its routes (``client/display.py``)."""

    def __init__(self, client: Any, session: Any) -> None:
        self.client, self.session = client, session

    def status(self) -> dict[str, Any]:
        return self.client.status(self.session)

    def still(self, *, fmt: str, scale: int, hatch: bool) -> tuple[bytes, dict[str, str]]:
        return self.client.still(self.session, fmt=fmt, scale=scale, hatch=hatch)

    def view(self, *, rate: int) -> Any:
        return self.client.view(self.session, rate=rate)

    def close(self) -> None:
        return None


def _display(ctx: Ctx, session: Any) -> Any:
    client = getattr(ctx.engine, "display", None)
    if client is not None and callable(getattr(client, "still", None)):
        return _Remote(client, session)
    return _Local(ctx.engine, session)


# --- the status, as words -------------------------------------------------------------------------


def _badge_keys(st: dict[str, Any]) -> list[str]:
    return [str(b.get("key", "")) for b in st.get("badges") or () if isinstance(b, dict)]


def is_exact(st: dict[str, Any]) -> bool:
    """The picture is the panel's, pixel for pixel: the board says ``exact`` (the snooper,
    no bus violation, a known format, no 18-bit colour) and no register is off the anchor."""
    flags = st.get("flags") or {}
    return bool(st.get("presented")) and bool(flags.get("exact")) and \
        "inexact" not in _badge_keys(st)


def exact_text(st: dict[str, Any]) -> str:
    if not st.get("presented"):
        return "no picture yet"
    flags = st.get("flags") or {}
    if is_exact(st):
        text = "exact"
    elif flags.get("blind"):
        text = "not exact (the DUT owns the panel and this image cannot see it)"
    elif flags.get("text_only"):
        text = "not exact (software tap: exact for the harness's own text only)"
    else:
        text = "not exact"
    hatched = int(st.get("hatched") or 0)
    if hatched:
        text += f", {hatched} tile{'s' if hatched != 1 else ''} unknown"
    return text


def badges_text(st: dict[str, Any]) -> str:
    return "; ".join(str(b.get("text", "")) for b in st.get("badges") or ()
                     if isinstance(b, dict)) or "-"


def status_line(st: dict[str, Any]) -> str:
    """One line: state (reason), mode, owner, exact or not, badges."""
    state = str(st.get("state") or "down")
    reason = str(st.get("reason") or "")
    head = f"{state}: {reason}" if reason else state
    return (f"{head}  mode {st.get('mode') or '-'}  owner {st.get('owner') or 'unknown'}  "
            f"{exact_text(st)}  badges {badges_text(st)}")


def _summary(st: dict[str, Any]) -> dict[str, Any]:
    return {"state": st.get("state") or "down", "reason": st.get("reason") or "",
            "mode": st.get("mode") or "", "owner": st.get("owner") or "unknown",
            "exact": is_exact(st), "badges": list(st.get("badges") or ())}


# --- snapshot -------------------------------------------------------------------------------------


def _slug(board_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", board_id).strip("_") or "board"


def _out_path(out: str | None, board_id: str, raw: bool) -> Path:
    name = (f"display-{_slug(board_id)}-{time.strftime('%Y%m%d-%H%M%S')}"
            f"{'.rgb565' if raw else '.png'}")
    if not out:
        return Path.cwd() / name
    path = Path(out).expanduser()
    if path.is_dir():
        return path / name
    if not path.parent.exists():
        raise UsageError(f"no directory {str(path.parent)!r} for {out!r}",
                         hint="make it first, or give -o a path in an existing directory")
    return path


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _waiting_words(ctx: Ctx, wait_s: float) -> Any:
    """FIX-PACK-1, human mode: while the first picture has not come after
    ``PROGRESS_AFTER_S``, say so on stderr ("opening the SSH forward..."), then every
    ``PROGRESS_EVERY_S``. A picture that comes at once (an upstream already live) says
    nothing."""
    if ctx.fmt != "human":
        yield
        return
    done = threading.Event()

    def speak() -> None:
        t0 = time.monotonic()
        if done.wait(PROGRESS_AFTER_S):
            return
        ctx.note(OPENING.format(wait=wait_s))
        while not done.wait(PROGRESS_EVERY_S):
            ctx.note(f"still waiting for the first picture ({time.monotonic() - t0:.0f} s of "
                     f"{wait_s:g} s)...")

    worker = threading.Thread(target=speak, daemon=True, name="display-snapshot-words")
    worker.start()
    try:
        yield
    finally:
        done.set()
        worker.join(timeout=1.0)


def _snapshot(ctx: Ctx, board_id: str, disp: Any) -> int:
    a = ctx.args
    raw = bool(a.raw)
    with _waiting_words(ctx, PICTURE_WAIT_S):
        data, meta = disp.still(fmt="raw" if raw else "png", scale=a.scale,
                                hatch=bool(a.hatch))
    try:
        st = disp.status()
    except HarnessError as exc:                        # the picture stands without it
        st = {"state": "unknown", "reason": exc.message}
    path = _out_path(a.out, board_id, raw)
    _write(path, data)
    width, height = (W, H) if raw else (W * a.scale, H * a.scale)
    hatched = int(meta.get("x-display-hatched") or 0)
    seq = int(meta.get("x-display-seq") or 0)
    summary = _summary(st)
    body = {"board_id": board_id, "path": str(path.resolve()),
            "format": "rgb565le" if raw else "png", "width": width, "height": height,
            "scale": 1 if raw else a.scale, "hatch": bool(a.hatch) and not raw,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "seq": seq,
            "hatched": hatched, **summary, "status": st}
    human = [str(path.resolve()),
             f"{board_id}  {status_line(st)}  ({width}x{height}"
             f"{', raw RGB565 LE' if raw else ''}, seq {seq})"]
    ctx.emit(Result("display snapshot", body,
                    rows=[[board_id, body["path"], body["format"], width, height, len(data),
                           summary["state"], summary["mode"], summary["owner"],
                           summary["exact"], badges_text(st), hatched, seq]],
                    human=human))
    return ExitCode.OK


# --- status ---------------------------------------------------------------------------------------


def _status(ctx: Ctx, board_id: str, disp: Any) -> int:
    st = disp.status()
    st.setdefault("board_id", board_id)
    available = bool(st.get("available"))
    lines = [f"display    {board_id}: {status_line(st)}",
             "available  " + ("yes" if available else f"no: {st.get('unavailable') or '?'}")]
    hello = st.get("hello") or {}
    if hello:
        lines.append(f"board      mode {hello.get('mode', '-')}, proto {hello.get('proto', '-')}, "
                     f"static_id {hello.get('static_id') or '-'}")
    if st.get("presented"):
        lines.append(f"picture    seq {st.get('seq')}, {st.get('hatched') or 0} tiles unknown, "
                     f"{st.get('resets') or 0} panel resets")
    elif available and not st.get("viewers"):
        lines.append("picture    none: nothing is viewing it now (`snapshot` or `show` opens it)")
    rate = st.get("rate")
    lines.append(f"rate       {'-' if rate is None else f'{rate} Hz'} (asked "
                 f"{st.get('rate_asked') if st.get('rate_asked') is not None else '-'}), "
                 f"{st.get('fps') or 0:g} fps, {st.get('bytes_per_s') or 0} B/s, "
                 f"{st.get('viewers') or 0} viewer{'s' if st.get('viewers') != 1 else ''}")
    summary = _summary(st)
    ctx.emit(Result("display status", st,
                    rows=[[board_id, available, summary["state"], summary["mode"],
                           summary["owner"], summary["exact"], badges_text(st),
                           st.get("hatched"), st.get("viewers"), st.get("fps"), rate,
                           st.get("reason") or st.get("unavailable") or ""]],
                    human=lines))
    return ExitCode.OK


# --- show -----------------------------------------------------------------------------------------


def truecolor(stream: Any, env: Mapping[str, str]) -> bool:
    """This stream is a terminal that says it draws 24-bit colour."""
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if env.get("NO_COLOR") or env.get("TERM", "") == "dumb":
        return False
    return env.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit") or \
        bool(env.get("WT_SESSION"))


def _stdin_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _key_reader() -> Any:
    from .cmd_io import _key_reader as reader

    return reader()


def fit_scale(cols: int, rows: int) -> int:
    """Panel pixels per terminal column: the picture (two pixel rows per text row) and one
    status line fit ``cols`` x ``rows``."""
    rows = max(1, rows - 1)
    return max(1, -(-W // max(1, cols)), -(-H // (2 * rows)))


def halfblock(frame: bytes | bytearray, valid: frozenset[int] | set[int], k: int) -> list[str]:
    """The picture at 1/``k`` scale as truecolor half-block rows (``HALF``: foreground = the
    upper pixel, background = the lower). A tile the mirror does not know is grey."""
    px = array("H")
    px.frombytes(bytes(frame[:FRAME_BYTES]))
    if sys.byteorder == "big":
        px.byteswap()
    ow, oh = W // k, H // k
    off = k // 2

    def at(x: int, y: int) -> int:
        return px[y * W + x] if (y // TILE) * TILES_X + x // TILE in valid else HATCH_RGB565

    lines = []
    for cy in range((oh + 1) // 2):
        y0 = 2 * cy * k + off
        y1 = (2 * cy + 1) * k + off
        parts, last = [], None
        for cx in range(ow):
            x = cx * k + off
            pair = (at(x, y0), at(x, y1) if y1 < H else 0)
            if pair != last:
                (r, g, b), (r2, g2, b2) = rgb888(pair[0]), rgb888(pair[1])
                parts.append(f"\x1b[38;2;{r};{g};{b};48;2;{r2};{g2};{b2}m")
                last = pair
            parts.append(HALF)
        parts.append("\x1b[0m")
        lines.append("".join(parts))
    return lines


def _write_out(out: TextIO, text: str) -> None:
    buf = getattr(out, "buffer", None)
    if buf is not None:
        out.flush()
        buf.write(text.encode("utf-8", "replace"))
        buf.flush()
    else:
        out.write(text)
        out.flush()


class _Screen:
    """The truecolor view: the alternate screen, redrawn in place; restored on exit."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.size: tuple[int, int] | None = None
        self.drawn = 0

    def __enter__(self) -> _Screen:
        _write_out(self.out, ALT_SCREEN_ON + "\x1b[2J")
        return self

    def __exit__(self, *exc: object) -> None:
        _write_out(self.out, ALT_SCREEN_OFF)

    def draw(self, model: PictureModel, status: str) -> None:
        size = shutil.get_terminal_size((80, 24))
        cols, rows = size.columns, size.lines
        buf = []
        if (cols, rows) != self.size:
            buf.append("\x1b[2J")
            self.size = (cols, rows)
        lines = halfblock(model.frame, model.valid, fit_scale(cols, rows)) \
            if model.presented else []
        for i, line in enumerate(lines, 1):
            buf.append(f"\x1b[{i};1H{line}\x1b[K")
        buf.append(f"\x1b[{len(lines) + 1};1H\x1b[0m{status[:max(1, cols - 1)]}\x1b[K")
        _write_out(self.out, "".join(buf))
        self.drawn += 1


def _live_status(view: Any) -> dict[str, Any]:
    st = dict(view.status())
    model = view.model
    st["presented"] = model.presented       # what THIS view draws, not the upstream's news
    st["seq"] = model.seq
    if model.presented:
        st["hatched"] = len(model.hatched)
    st["fps"] = model.fps()
    return st


def _tick_line(board_id: str, st: dict[str, Any]) -> str:
    seq = st.get("seq")
    return (f"{time.strftime('%H:%M:%S')}  {board_id}  {status_line(st)}  "
            f"seq {'-' if seq is None else seq}  {st.get('fps') or 0:g} fps")


def _show(ctx: Ctx, board_id: str, disp: Any) -> int:
    a = ctx.args
    if ctx.fmt == "json" and a.for_s is None:
        raise UsageError("--json collects the view into one object, so it needs --for SECONDS",
                         hint="e.g. harness-manager --json display TARGET show --for 5")
    period = 1.0 / a.rate
    ticks = None if a.for_s is None else max(1, math.ceil(a.for_s * a.rate - 1e-9))
    out = sys.stdout
    picture = ctx.fmt == "human" and truecolor(out, os.environ)
    keyed = ctx.fmt == "human" and _stdin_tty() and _stdout_tty()
    view = disp.view(rate=max(1, min(int(RATE_MAX_HZ), math.ceil(a.rate))))
    done = threading.Event()
    refreshes = 0
    last: dict[str, Any] = {}
    try:
        with contextlib.ExitStack() as stack:
            stopper = stack.enter_context(Stopper(None))
            if keyed:
                keys = stack.enter_context(_key_reader())
                threading.Thread(target=_watch_keys, args=(keys, done), daemon=True,
                                 name="display-show-keys").start()
                ctx.note(f"display of {board_id}: Ctrl-] exits")
            screen = stack.enter_context(_Screen(out)) if picture else None
            t0 = time.monotonic()
            while not done.is_set() and not stopper.done:
                if ticks is not None and refreshes >= ticks:
                    break
                _pump_until(view, t0 + refreshes * period, done, stopper)
                if done.is_set() or stopper.done:
                    break
                view.pump(0)
                last = _live_status(view)
                if screen is not None:
                    screen.draw(view.model, f"{board_id}  {status_line(last)}  "
                                            f"{last['fps']:g} fps  (Ctrl-] exits)")
                elif ctx.fmt == "tsv":
                    sys.stdout.write(tsv_line("display show", _show_row(board_id, last)) + "\n")
                    sys.stdout.flush()
                elif ctx.fmt == "human":
                    _write_out(out, _tick_line(board_id, last) + ("\r\n" if keyed else "\n"))
                refreshes += 1
            if a.for_s is not None and not done.is_set() and not stopper.done:
                _pump_until(view, t0 + a.for_s, done, stopper)   # the last picture's time
    finally:
        done.set()
        view.close()
    last = last or _live_status(view)
    if ctx.fmt == "json":
        ctx.emit(Result("display show", {
            "board_id": board_id, "refreshes": refreshes, "rate": a.rate, "picture": picture,
            "updates": view.model.messages, "seq": view.model.seq, **_summary(last),
            "status": last}))
    elif picture:
        _write_out(out, f"{board_id}  {status_line(last)}  ({refreshes} refreshes)\n")
    return ExitCode.OK


def _stdout_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _show_row(board_id: str, st: dict[str, Any]) -> list[Any]:
    s = _summary(st)
    return [time.strftime("%Y-%m-%dT%H:%M:%S"), board_id, s["state"], s["mode"], s["owner"],
            s["exact"], st.get("seq"), st.get("hatched"), st.get("fps"), badges_text(st)]


def _pump_until(view: Any, due: float, done: threading.Event, stopper: Stopper) -> None:
    """Draw what arrives until ``due`` (acking it), or until a stop."""
    while not done.is_set() and not stopper.done:
        left = due - time.monotonic()
        if left <= 0:
            return
        view.pump(min(left, 0.1))


def _watch_keys(keys: Any, done: threading.Event) -> None:
    """Ctrl-] (or Ctrl-C, which raw mode delivers as a byte) ends the view."""
    from .cmd_io import ESCAPE

    while not done.is_set():
        try:
            data = keys.read(0.2)
        except OSError:
            break
        if ESCAPE in data or CTRL_C in data:
            break
    done.set()


# --- the verb -------------------------------------------------------------------------------------


def cmd_display(ctx: Ctx) -> int:
    a = ctx.args
    action = a.display_cmd
    if action == "snapshot" and a.raw and (a.scale != 1 or a.hatch):
        raise UsageError("--raw is the panel's own pixels: no --scale, no --hatch",
                         hint="leave --scale and --hatch out, or drop --raw for a PNG")
    if action == "snapshot" and a.out:
        _out_path(a.out, "board", bool(a.raw))      # a bad -o fails before the board opens
    with ctx.board(note=f"display {action}") as (cand, session):
        disp = _display(ctx, session)
        try:
            return {"snapshot": _snapshot, "status": _status, "show": _show}[action](
                ctx, cand.board_id, disp)
        except HarnessError as exc:
            raise _explain(exc) from None
        finally:
            disp.close()
