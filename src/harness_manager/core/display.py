"""The live display mirror: the model, HM's end of the wire, and the badges (lane LM1).

``docs/design/LCD_MIRROR.md`` is the design (§3 what "pixel-exact" means, §6 the wire,
§7 Harness Manager). david's decisions of 2026-09-26: the hybrid (a harnessd software tap
now, the FPGA snooper in mint 4; Harness Manager is identical for both), only the lease
holder sees the live picture, no touch pass-through.

Board-agnostic and importable with no daemon:

- ``DisplayFrame``: the presented picture of one board. The pixels (kept tile-major, so a
  decoded tile is one slice), the latest tile record per tile exactly as the board encoded
  it (forwarded to browsers unchanged), VALID, the owner, the status word, the counters,
  REGS and MODE.
- The five-encoding tile decoder and every byte of the wire live in
  ``core.display_wire`` (one module, so the Linux lead's §6.1 corrections land in one
  file); this module re-exports what callers need.
- ``badges()``: what the Live display says about a picture (§7.4), with ``DimDebounce``
  for the states that must persist 1 s before they show.
- ``DisplayStream``: HM's end of one lcd_mirror connection over any socket-like byte
  stream (``read``/``key``/``rate``/``ping``/``ack``/``close``).
- ``DisplayAdapter``: what a board pack provides (``display_reason``, ``display_connect``,
  optionally ``display_gate``, ``display_diagnose`` and ``display_release``). A bare callable that returns a
  connected byte stream is accepted wherever an adapter is (``as_display_source``).
- ``DisplayPicture`` + ``png_rgb565``: a still of the presented picture (``display.png``,
  ``display snapshot``).

The compositor (per-viewer dirty sets, ack, open/grace/reconnect) is
``harness_manager.services.display``.
"""

from __future__ import annotations

import socket
import struct
import threading
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from harness_manager.core import display_wire as wire
from harness_manager.core.capabilities import DISPLAY_MIRROR  # noqa: F401 - re-exported
from harness_manager.core.display_wire import (  # noqa: F401 - re-exported for callers
    ALL_VALID,
    E_FILL,
    E_PAL1,
    E_PAL2,
    E_RAW,
    E_RLE16,
    ENCODING_NAMES,
    FRAME_BYTES,
    NONE_VALID,
    NTILES,
    OWNER_DUT,
    OWNER_HARNESS,
    OWNER_UNKNOWN,
    REGS_SIZE,
    S_FRAMING,
    S_KEY_BITS,
    S_SNAP_LAST,
    TILE,
    TILE_BYTES,
    TILES_X,
    TILES_Y,
    VALID_BYTES,
    DisplayInfo,
    DisplayUpdate,
    H,
    Message,
    MessageReader,
    ModeRegs,
    Pong,
    RateEcho,
    Refusal,
    StatusFlags,
    TileRecord,
    Unknown,
    W,
    WireError,
    decode_tile,
    encode_tile,
    owner_name,
    valid_bytes,
    valid_tiles,
)
from harness_manager.core.errors import UnavailableError

#: The capability (§7.1) is ``DISPLAY_MIRROR`` ("display_mirror"). Plain ``display`` is taken
#: (``lab display``, ``mps3.display_flip``). The name lives with the other shared capability
#: names (``core.capabilities``) and is re-exported here.
DISPLAY_MIRROR_TITLE = "Live display"

# --- the panel's calibrated anchor (§3): anything else is badged, never pretended -----------

CALIBRATED_MADCTL = frozenset({0x20, 0xE0})   # R16: board-proven right way up / 180 degrees
PANEL_ANCHOR = 0x09                           # R36: board-proven companion value
DISPMODE_NORMAL = 0x00                        # R01
COLMOD_565 = 0x05                             # R17
#: Partial area and vertical scroll (0x0A-0x15). ASSUMED: the register log holds 0 where
#: nothing was written. The log is never reset (the board's §6.1 correction), so "set" means
#: written since the mirror started, not since the last panel reset: conservative.
SCROLL_PARTIAL = range(0x0A, 0x16)
DIM_PERSIST_S = 1.0                           # §7.4: a dim state must persist 1 s to show


class DisplayUnavailable(UnavailableError):
    """The mirror cannot be reached now, with the reason (``no lcd_mirror service``, ``claim
    lost``). ``retry_s``: when the compositor tries again (None: not until a viewer asks
    again).

    PANEL-TRUTH (additive): ``state`` is what the compositor shows meanwhile: ``down`` (the
    default), or ``connecting`` for a step still under way (the lease being checked with a
    hub that did not answer this time: "checking your lease with the hub..."). ``detail`` is
    the raw cause (ssh's words), for a Details disclosure, never the headline."""

    def __init__(self, reason: str, *, retry_s: float | None = 60.0, state: str = "down",
                 detail: str = "") -> None:
        super().__init__(DISPLAY_MIRROR, reason)
        self.retry_s = retry_s
        self.state = state if state in ("down", "connecting") else "down"
        self.detail = detail


# --- badges (§7.4) ---------------------------------------------------------------------------

BLIND_TEXT = "DUT owns the panel: this harness image cannot see it (live view needs mint 4)"
HELD_TEXT = "DUT owns the panel"
VIOL_TEXT = "bus timing violations: the glass may differ"
APPROX_TEXT = "18-bit colour shown as 16-bit"
FMT_TEXT = "unknown pixel format"
DIM_TEXT = {"backlight_off": "backlight off", "display_off": "display off",
            "standby": "panel in standby"}
DIM_KEYS = tuple(DIM_TEXT)


@dataclass(frozen=True)
class Badge:
    """One overlay badge. ``level``: ``grey`` (grey the picture: it is not the panel),
    ``held`` (the DUT owns the panel; the picture is exact), ``dim`` (dim the picture),
    ``warn`` (the glass may differ)."""

    key: str
    level: str
    text: str

    def to_json(self) -> dict[str, str]:
        return {"key": self.key, "level": self.level, "text": self.text}


@dataclass(frozen=True)
class PanelRegs:
    """The registers the badges read: the mode word's four, and whether any scroll or
    partial register is set (None: no REGS seen yet)."""

    r01: int = DISPMODE_NORMAL
    r36: int = PANEL_ANCHOR
    r17: int = COLMOD_565
    r16: int = 0x20
    scroll: bool | None = None

    @classmethod
    def of(cls, mode: ModeRegs, regs: bytes | None = None) -> PanelRegs:
        scroll = None if regs is None else any(regs[i] for i in SCROLL_PARTIAL)
        return cls(mode.r01, mode.r36, mode.r17, mode.r16, scroll)

    def off_anchor(self) -> list[str]:
        out = []
        if self.r16 not in CALIBRATED_MADCTL:
            out.append(f"R16=0x{self.r16:02X}")
        if self.r36 != PANEL_ANCHOR:
            out.append(f"R36=0x{self.r36:02X}")
        if self.r01 != DISPMODE_NORMAL:
            out.append(f"R01=0x{self.r01:02X}")
        if self.scroll:
            out.append("scroll/partial set")
        return out

    def to_json(self) -> dict[str, Any]:
        return {"r01": self.r01, "r36": self.r36, "r17": self.r17, "r16": self.r16,
                "scroll": self.scroll}


def dim_conditions(flags: StatusFlags) -> frozenset[str]:
    """The dimming states in force now (before the 1 s debounce)."""
    out = set()
    if not flags.bl:
        out.add("backlight_off")
    if not flags.display_on:
        out.add("display_off")
    if flags.standby:
        out.add("standby")
    return frozenset(out)


def badges(flags: StatusFlags, owner: int, mode: str, regs: PanelRegs, *,
           dims: Iterable[str] | None = None) -> list[Badge]:
    """What the Live display says about a picture (§7.4), most important first.

    ``dims``: the dimming states that have persisted ``DIM_PERSIST_S`` (``DimDebounce``);
    None shows every one in force now.
    """
    out: list[Badge] = []
    if flags.blind or (mode == "sw" and owner == OWNER_DUT):
        out.append(Badge("blind", "grey", BLIND_TEXT))
    elif owner == OWNER_DUT:
        out.append(Badge("held", "held", HELD_TEXT))
    shown = dim_conditions(flags) if dims is None else frozenset(dims) & dim_conditions(flags)
    for key in DIM_KEYS:
        if key in shown:
            out.append(Badge(key, "dim", DIM_TEXT[key]))
    if flags.viol:
        out.append(Badge("viol", "warn", VIOL_TEXT))
    if flags.approx:
        out.append(Badge("approx", "warn", APPROX_TEXT))
    if not flags.fmt_ok:
        out.append(Badge("fmt", "warn", FMT_TEXT))
    off = regs.off_anchor()
    if off:
        out.append(Badge("inexact", "warn", f"not mirrored exactly ({', '.join(off)})"))
    return out


class DimDebounce:
    """§7.4: a dim state shows only once it has held for ``persist_s`` (clcd_demo re-runs its
    init about 3 times a second, and 0x28 passes through "display off" each time)."""

    def __init__(self, persist_s: float = DIM_PERSIST_S) -> None:
        self.persist_s = persist_s
        self.since: dict[str, float] = {}

    def update(self, flags: StatusFlags, now: float) -> frozenset[str]:
        active = dim_conditions(flags)
        for k in list(self.since):
            if k not in active:
                del self.since[k]
        for k in active:
            self.since.setdefault(k, now)
        return self.confirmed(now)

    def confirmed(self, now: float) -> frozenset[str]:
        return frozenset(k for k, t in self.since.items() if now - t >= self.persist_s)

    def pending(self, now: float) -> bool:
        return any(now - t < self.persist_s for t in self.since.values())

    def reset(self) -> None:
        self.since.clear()


# --- the model -------------------------------------------------------------------------------


def decode_parts(parts: Iterable[DisplayUpdate]) -> dict[int, tuple[bytes, bytes]]:
    """Decode every tile of one SNAP's parts: ``{tile: (pixels, record)}``, a later part
    winning. Raises ``WireError`` on a malformed payload (the stream is then untrusted)."""
    out: dict[int, tuple[bytes, bytes]] = {}
    for u in parts:
        for rec in u.tiles:
            out[rec.idx] = (decode_tile(rec.enc, rec.payload), rec.raw)
    return out


class DisplayFrame:
    """The presented picture of one board (§7.1 "the latest record per tile plus the decoded
    frame"). Not thread-safe: the compositor holds its lock around every call.

    ``commit`` applies one whole SNAP (a keyframe, or a delta's parts) at once: a picture
    is never shown half-updated.
    """

    def __init__(self) -> None:
        self.info: DisplayInfo | None = None
        self.px = bytearray(FRAME_BYTES)                  # tile-major: tile t at [512t, 512t+512)
        self.records: list[bytes | None] = [None] * NTILES
        self.valid = NONE_VALID
        self.valid_list = [False] * NTILES
        self.owner = OWNER_UNKNOWN
        self.status = 0                                   # without the framing bits
        self.seq = self.t_ms = self.frames = self.resets = 0
        self.regs: bytes | None = None                    # the last 256 REGS (keyframes): a raw log
        self.mode: int | None = None                      # the latest MODE word (never from REGS)
        self.mode_resets: int | None = None               # RESETS when that MODE word came
        self.presented = False
        self.version = 0                                  # +1 per commit

    # -- updating ----------------------------------------------------------------------------

    def commit(self, parts: Sequence[DisplayUpdate], decoded: Mapping[int, tuple[bytes, bytes]],
               *, key: bool) -> None:
        """Make one SNAP visible. A keyframe replaces every record (a tile it does not carry
        is not VALID, H3); a delta replaces the records it carries."""
        if not parts:
            return
        if key:
            self.records = [None] * NTILES
        px = self.px
        for t, (pixels, raw) in decoded.items():
            px[t * TILE_BYTES:(t + 1) * TILE_BYTES] = pixels
            self.records[t] = raw
        for u in parts:
            if u.regs is not None:
                self.regs = u.regs
            elif u.mode is not None:
                self.mode, self.mode_resets = u.mode, u.resets
        last = parts[-1]                                  # H1: one t_ms/frames across the parts
        self.seq, self.t_ms, self.frames, self.resets = last.seq, last.t_ms, last.frames, last.resets
        self.status = last.status & ~S_FRAMING
        self.owner = last.owner
        if last.valid != self.valid:
            self.valid = last.valid
            self.valid_list = wire.valid_list(last.valid)
        self.presented = True
        self.version += 1

    # -- reading -----------------------------------------------------------------------------

    def flags(self) -> StatusFlags:
        return StatusFlags(self.status)

    def mode_regs(self) -> ModeRegs:
        """R01, R36, R17, R16 as the panel holds them. REGS is the snooper's raw register
        log, never reset (the board's §6.1 correction): after RESETS changes, its four can
        be pre-reset values. So MODE (the decoded state) wins whenever one has come since
        the last reset; REGS stand in only until then (a keyframe in one part carries no
        MODE); then an older MODE; then the calibrated anchor."""
        if self.mode is not None and self.mode_resets == self.resets:
            return wire.mode_regs(self.mode)
        if self.regs is not None:
            return wire.mode_regs(wire.mode_word(self.regs))
        if self.mode is not None:
            return wire.mode_regs(self.mode)
        return ModeRegs(DISPMODE_NORMAL, PANEL_ANCHOR, COLMOD_565, 0x20)

    def new_connection(self) -> None:
        """A new upstream connection: a MODE word from the last one may be stale, so the
        next keyframe's REGS stand in until this connection sends MODE."""
        self.mode = self.mode_resets = None

    def panel_regs(self) -> PanelRegs:
        return PanelRegs.of(self.mode_regs(), self.regs)

    def regs_for_viewer(self) -> bytes:
        """REGS for a viewer's keyframe: the last full set, with the latest mode word's four
        registers laid over it (MODE rides every delta; REGS only keyframes)."""
        r = bytearray(self.regs or bytes(REGS_SIZE))
        m = self.mode_regs()
        r[0x01], r[0x36], r[0x17], r[0x16] = m.r01, m.r36, m.r17, m.r16
        return bytes(r)

    def held(self, t: int) -> bool:
        """Tile ``t`` shows a known picture: VALID, and a record is held."""
        return self.valid_list[t] and self.records[t] is not None

    def hatched_tiles(self) -> frozenset[int]:
        return frozenset(t for t in range(NTILES) if not self.held(t))

    def hatched(self) -> int:
        return sum(1 for t in range(NTILES) if not self.held(t))

    def tile_px(self, t: int) -> bytes:
        return bytes(self.px[t * TILE_BYTES:(t + 1) * TILE_BYTES])

    def rgb565(self) -> bytes:
        """The picture, row-major (viewer order), RGB565 LE: 153,600 bytes."""
        return tiles_to_rows(self.px)

    def header_sig(self) -> tuple[Any, ...]:
        """What a viewer must be told about even when no tile changed."""
        return (self.valid, self.owner, self.status, self.mode_regs(), self.resets)

    def viewer_update(self, tiles: Iterable[int], *, key: bool) -> bytes:
        """One UPDATE in the board's own layout (§7.3): the records forwarded as the board
        encoded them. A viewer's message is always one whole SNAP (``snap_last``)."""
        recs = [r for r in (self.records[t] for t in sorted(tiles)) if r is not None]
        status = self.status | S_SNAP_LAST | (S_KEY_BITS if key else 0)
        regs = self.regs_for_viewer()
        return wire.update_msg(self.seq, self.t_ms, self.frames, self.resets, status, self.owner,
                               self.valid, recs, regs=regs, mode=wire.mode_word(regs))

    def picture(self, *, badge_list: Sequence[Badge] = ()) -> DisplayPicture:
        return DisplayPicture(rgb565=self.rgb565(), valid=self.valid,
                              hatched=self.hatched_tiles(), seq=self.seq, t_ms=self.t_ms,
                              frames=self.frames, resets=self.resets, owner=self.owner,
                              status=self.status, regs=self.panel_regs(),
                              mode=self.info.mode if self.info else "",
                              badges=tuple(badge_list))


def tiles_to_rows(px: bytes | bytearray) -> bytes:
    """Tile-major pixels -> a row-major 320x240 frame."""
    out = bytearray(FRAME_BYTES)
    stride, row = W * 2, TILE * 2
    for t in range(NTILES):
        x0, y0 = wire.tile_origin(t)
        src = t * TILE_BYTES
        dst = y0 * stride + x0 * 2
        for r in range(TILE):
            out[dst + r * stride:dst + r * stride + row] = px[src + r * row:src + (r + 1) * row]
    return bytes(out)


def rows_to_tiles(frame: bytes | bytearray) -> bytes:
    """A row-major 320x240 frame -> tile-major pixels."""
    return b"".join(wire.tile_of_frame(frame, t) for t in range(NTILES))


# --- a still picture -------------------------------------------------------------------------

HATCH_RGB565 = 0x8410         # mid grey, drawn over the hatched tiles of a still


@dataclass(frozen=True)
class DisplayPicture:
    """A still of the presented picture (``display.png``, ``display snapshot``)."""

    rgb565: bytes                              # row-major, RGB565 LE, 320x240
    valid: bytes = ALL_VALID
    hatched: frozenset[int] = frozenset()
    seq: int = 0
    t_ms: int = 0
    frames: int = 0
    resets: int = 0
    owner: int = OWNER_UNKNOWN
    status: int = 0
    regs: PanelRegs = field(default_factory=PanelRegs)
    mode: str = ""
    badges: tuple[Badge, ...] = ()

    def pixel(self, x: int, y: int) -> int:
        return struct.unpack_from("<H", self.rgb565, (y * W + x) * 2)[0]

    def png(self, *, scale: int = 1, hatch: bool = False) -> bytes:
        return png_rgb565(self.rgb565, scale=scale, hatch_tiles=self.hatched if hatch else ())


_RGB888: list[bytes] | None = None


def rgb888(v: int) -> tuple[int, int, int]:
    """RGB565 -> RGB888 by bit replication (``tools/gen_tokens.py``, ``tools/clcd_mock.py``)."""
    r5, g6, b5 = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return (r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)


def _rgb888_table() -> list[bytes]:
    global _RGB888
    if _RGB888 is None:
        _RGB888 = [bytes(rgb888(v)) for v in range(65536)]
    return _RGB888


def png_rgb565(frame: bytes, *, w: int = W, h: int = H, scale: int = 1,
               hatch_tiles: Iterable[int] = ()) -> bytes:
    """A row-major RGB565 LE frame as a PNG (RGB, 8 bit), ``scale`` x ``scale`` pixels per panel
    pixel; ``hatch_tiles`` get a grey diagonal hatch (§7.4 VALID=0)."""
    if scale not in (1, 2, 3, 4):
        raise ValueError("scale is 1-4")
    hatch = set(hatch_tiles)
    if hatch:
        buf = bytearray(frame)
        grey = struct.pack("<H", HATCH_RGB565)
        for t in hatch:
            x0, y0 = wire.tile_origin(t)
            for r in range(TILE):
                for x in range(TILE):
                    if (x + r) % 8 < 2:
                        o = ((y0 + r) * w + x0 + x) * 2
                        buf[o:o + 2] = grey
        frame = bytes(buf)
    table = _rgb888_table()
    vals = struct.unpack(f"<{w * h}H", frame)
    rows = []
    for y in range(h):
        line = b"".join(map(table.__getitem__, vals[y * w:(y + 1) * w]))
        if scale > 1:
            line = b"".join(line[i:i + 3] * scale for i in range(0, len(line), 3))
        rows.append((b"\x00" + line) * scale)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w * scale, h * scale, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


# --- HM's end of one connection --------------------------------------------------------------


@runtime_checkable
class ByteStream(Protocol):
    """A connected, socket-like byte stream to the board's lcd_mirror service (a real
    socket to an SSH forward's local port, in the product)."""

    def recv(self, n: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def settimeout(self, t: float | None) -> None: ...

    def close(self) -> None: ...


class DisplayStream:
    """HM's end of one lcd_mirror connection (§7.1): ``read``, ``key``, ``rate``, ``ping``,
    ``ack`` (H1), ``close``. Sends are safe from any thread; ``read`` is for one reader."""

    RECV = 65536

    def __init__(self, stream: Any, *, max_msg: int = wire.MAX_MSG_DEFAULT) -> None:
        self.stream = stream
        self.reader = MessageReader(max_msg)
        self._send_lock = threading.Lock()
        self._closed = False
        self.bytes_out = 0

    @property
    def bytes_in(self) -> int:
        return self.reader.bytes

    def read(self, timeout: float) -> list[Message]:
        """The messages that arrived within ``timeout`` ([] if none). ``EOFError`` when the
        board closed the stream (a refusal line cut short by the close is returned)."""
        if self._closed:
            raise EOFError("the lcd_mirror stream is closed")
        try:
            self.stream.settimeout(timeout)
            data = self.stream.recv(self.RECV)
        except TimeoutError:
            return []
        if not data:
            refusal = self.reader.eof()
            if refusal is not None:
                return [refusal]
            raise EOFError("the board closed the lcd_mirror stream")
        return self.reader.feed(data)

    def send(self, msg: bytes) -> None:
        with self._send_lock:
            if self._closed:
                raise EOFError("the lcd_mirror stream is closed")
            self.stream.sendall(msg)
            self.bytes_out += len(msg)

    def key(self) -> None:
        self.send(wire.key_msg())

    def rate(self, hz: int) -> None:
        self.send(wire.rate_msg(hz))

    def ping(self, token: int) -> None:
        self.send(wire.ping_msg(token))

    def ack(self, seq: int) -> None:
        self.send(wire.ack_msg(seq))

    def close(self) -> None:
        """Idempotent, from any thread; a blocked ``read`` returns (EOF or an OSError)."""
        self._closed = True
        shutdown = getattr(self.stream, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.stream.close()
        except OSError:
            pass


# --- what a board pack provides --------------------------------------------------------------


@runtime_checkable
class DisplayAdapter(Protocol):
    """``session.display`` (lane LM2 wires the MPS3 one; None: the board has no mirror).

    ``display_reason()``: "" when the mirror can be opened now, else why not (the capability
    line: "needs the Linux harness with lcd_mirror and a claimed board").
    ``display_connect()``: a NEW connected byte stream to the board's lcd_mirror service
    (MPS3: a socket to ``claim.open_forward({"lcd_mirror": 6940})``'s local port). It may
    raise ``DisplayUnavailable`` (with ``retry_s``), another ``HarnessError`` (the compositor
    stops: down with that reason) or ``OSError`` (a transient: retried with back-off).

    Optional:
    ``display_gate()``: why the board can NEVER show the mirror as it is now (the feature
    gate: the bare-metal harness, an image without ``lcd_mirror``), else "" (also when it is
    not known now). The routes answer it 422 UNAVAILABLE before they look at the lease
    (``daemon.display_api.refusal``); ``display_reason()`` still gives it first too.
    ``display_diagnose(since)``: after a stream that closed before HELLO (an SSH ``-L``
    accepts, then closes, when the far end refuses), the reason if the transport knows one
    since monotonic time ``since`` ("no lcd_mirror service on the board (...)"), else "".
    ``display_release()``: the compositor closed this board's upstream for good (drop the
    forward).
    """

    def display_reason(self) -> str: ...

    def display_connect(self) -> Any: ...


class _CallableSource:
    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def display_reason(self) -> str:
        return ""

    def display_connect(self) -> Any:
        return self._connect()

    def __repr__(self) -> str:
        return f"<display source {self._connect!r}>"


def as_display_source(source: Any) -> Any:
    """An adapter (has ``display_connect``) as is; a bare connector callable, wrapped."""
    if hasattr(source, "display_connect"):
        return source
    if callable(source):
        return _CallableSource(source)
    raise TypeError(f"not a display source: {source!r}")
