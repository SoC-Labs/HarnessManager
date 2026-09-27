"""The lcd_mirror wire (TCP 6940): every byte-level fact Harness Manager relies on, in ONE module.

Lane LM1 (DISPLAY-CORE), from ``docs/design/LCD_MIRROR.md`` §6. The contract is
LCDMIRROR-FPGA §6.2 plus the HM lead's six amendments; the byte layout is HM's reading of
it (§6.1), plus H1 (``ACK``, ``snap_last``) and the small rules of H3.

**One module on purpose.** Every place that depends on a byte-level fact is marked with a
``# §6.1`` comment (``# H1`` / ``# H3`` for the requests), so a correction lands here as a
one-file change. Nothing else in Harness Manager packs or unpacks these bytes:
``core.display`` (the model), ``services.display`` (the compositor), the fakes and the
tests all go through this module.

**Confirmed by the board side.** The Linux lead built ``mps3-lcdmirror`` to this reading
(platform ``feat/lcd-mirror`` d86ce81; ``docs/contracts/net-protocol.md`` v0.15 "LCD mirror
(TCP 6940)") and checked in wire vectors, copied to ``tests/fixtures/lcdmirror_wire/`` and
parsed by ``tests/unit/test_lm1_display_wire.py``. Their corrections, applied here:

1. RATE's clamp is echoed in the board's own ``0x11`` reply, at once; nothing about the
   rate rides an UPDATE.
2. ``seq`` is per connection and 1 for the first UPDATE (+1 each, wraps at 2^32), so an
   ``ACK 0`` acknowledges nothing.
3. owner 3 (unknown) is reserved; sw mode sends only 0 and 1.
4. There are no heartbeat UPDATEs: nothing changed means no UPDATE, and PING is the
   liveness probe (``services.display`` keys ``stale`` on PONG).
5. ``max_msg`` (4096..65536) bounds the WHOLE message, the 8-byte header included (H3);
   a longer one is corrupt.
6. REGS is the snooper's raw register log, never reset: after RESETS changes, R16, R17,
   R36 and R01 come from MODE (``core.display.DisplayFrame.mode_regs``).

Stdlib only; no daemon, no board.

The wire in brief (§6.1)::

    frame     'L' 'M' u8 type, u8 rsvd=0, u32 len (LE), then len bytes.
              A first byte '{' is instead the refusal: ONE JSON line, then close.
    0x01 HELLO   board -> HM, first: JSON {proto, w, h, fmt, tile, mode, static_id, max_msg, ...}
    0x02 UPDATE  board -> HM: u32 seq, u32 t_ms, u32 frames, u32 resets, u32 status, u8 owner,
                 u8 valid[38]; key_first ? u8 regs[256] : u32 mode; u16 ntiles;
                 ntiles x {u16 idx, u8 enc, u16 len, payload}
    0x10 KEY   0x11 RATE u8 (the board echoes the clamped value)   0x12 PING u32 / 0x13 PONG u32
    0x14 ACK u32 seq (H1: at most 2 UPDATEs unacknowledged)
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

# --- geometry ------------------------------------------------------------------------------

PROTO = 1                          # §6.1 HELLO "proto":1
W, H = 320, 240                    # §6.1 HELLO "w"/"h": the viewer (landscape) picture
TILE = 16                          # §6.1 HELLO "tile"
FMT = "rgb565le"                   # §6.1 amendment 1: RGB565, little-endian in the stream
TILES_X, TILES_Y = W // TILE, H // TILE           # 20 x 15
NTILES = TILES_X * TILES_Y                        # 300
TILE_PX = TILE * TILE                             # 256
TILE_BYTES = TILE_PX * 2                          # 512
FRAME_BYTES = W * H * 2                           # 153,600
VALID_BYTES = (NTILES + 7) // 8                   # 38: §6.1 u8 valid[38]
REGS_SIZE = 256                                   # §6.1 u8 regs[256]: byte i = last datum to index i
                                                  # (a raw log, never reset: correction 6)

# --- framing ---------------------------------------------------------------------------------

MAGIC = b"LM"                                     # §6.1
HEADER = struct.Struct("<2sBBI")                  # §6.1 'L' 'M' u8 type, u8 rsvd, u32 len (LE)
HEADER_SIZE = HEADER.size                         # 8
REFUSAL_BYTE = 0x7B                               # §6.1 a first byte '{' is the refusal line
REFUSAL_MAX = 4096                                # longest refusal line read before giving up
#: Amendment 5 + H3 (confirmed): ``max_msg`` in HELLO bounds a WHOLE board message, the
#: 8-byte header included; the board keeps it within 4096..65536.
MAX_MSG_DEFAULT = 65536                           # §6.1 / H3
MAX_MSG_MIN = 4096                                # §6.1 (LCDM_MIN_MSG)

T_HELLO = 0x01                                    # §6.1
T_UPDATE = 0x02                                   # §6.1
T_KEY = 0x10                                      # §6.1 client
T_RATE = 0x11                                     # §6.1 client u8 hz; the board's own 0x11 u8
                                                  # reply is the clamp (correction 1)
T_PING = 0x12                                     # §6.1 client u32
T_PONG = 0x13                                     # §6.1 board u32 (the PING's)
T_ACK = 0x14                                      # H1 client u32 seq (cumulative; seq starts
                                                  # at 1, so ACK 0 acknowledges nothing: correction 2)
TYPE_NAMES = {T_HELLO: "HELLO", T_UPDATE: "UPDATE", T_KEY: "KEY", T_RATE: "RATE",
              T_PING: "PING", T_PONG: "PONG", T_ACK: "ACK"}

#: H1: the board keeps at most this many UPDATEs unacknowledged.
ACK_WINDOW = 2                                    # H1
RATE_MAX = 255                                    # §6.1 RATE is a u8; the board clamps to
                                                  # 0..HELLO rate_max (30), 0 = pause

# --- UPDATE ----------------------------------------------------------------------------------

#: §6.1: u32 seq, u32 t_ms, u32 frames, u32 resets, u32 status, u8 owner, u8 valid[38] (59 B).
#: seq: per connection, 1 for the first UPDATE, +1 each, wraps at 2^32 (correction 2).
UPDATE_FIXED = struct.Struct(f"<IIIIIB{VALID_BYTES}s")
MODE_WORD = struct.Struct("<I")                   # §6.1 u32 mode, when not key_first
U16 = struct.Struct("<H")
U32 = struct.Struct("<I")
_PIXELS = struct.Struct(f"<{16 * 16}H")            # one tile's pixels, LE (§6.1 amendment 1)
TILE_RECORD = struct.Struct("<HBH")               # §6.1 {u16 idx, u8 enc, u16 len}, then payload

# status: [10:0] the snooper's CSR STATUS (LCDMIRROR-FPGA §4, 0x010)          # §6.1
ST_RST_N = 1 << 0          # panel RST released (live)
ST_BL = 1 << 1             # backlight on (live)
ST_OWNER = 1 << 2          # the KVM owner (1 = DUT); the u8 owner wins (H3)
ST_DISPLAY_ON = 1 << 3     # R28 GON.DTE.D = 11
ST_STANDBY = 1 << 4        # R1F STB
ST_IN_GRAM = 1 << 5        # index = 0x22
ST_FMT_OK = 1 << 6         # a pixel format the snooper renders
ST_APPROX = 1 << 7         # 18-bit colour, stored as 16-bit
ST_VIOL = 1 << 8           # bus timing violations (sticky)
ST_OOB = 1 << 9            # a pixel out of GRAM (sticky)
ST_RD_SEEN = 1 << 10       # a bus read was seen (sticky)
ST_CSR_MASK = 0x7FF
# the wire's own bits
S_EXACT = 1 << 16          # §6.1 amendment 4: hw and not viol, fmt_ok, not approx (H3)
S_TEXT_ONLY = 1 << 17      # §6.1 amendment 4: sw mode (exact only for the harness's text)
S_BLIND = 1 << 18          # §6.1 amendment 4: sw mode while the DUT owns the panel (or the
                           # KVM drives it mid-handover)
S_KEY = 1 << 24            # §6.1 a part of a keyframe
S_KEY_FIRST = 1 << 25      # §6.1 its first part: regs[256] in place of the mode word
S_KEY_LAST = 1 << 26       # §6.1 its last part
S_SNAP_LAST = 1 << 27      # H1: the last UPDATE of one SNAP (one t_ms/frames across its parts)
S_KEY_BITS = S_KEY | S_KEY_FIRST | S_KEY_LAST
S_FRAMING = S_KEY_BITS | S_SNAP_LAST

OWNER_HARNESS = 0          # §6.1 u8 owner
OWNER_DUT = 1              # §6.1
OWNER_UNKNOWN = 3          # §6.1 reserved; sw mode sends only 0/1 (correction 3). Any other
                           # value reads as unknown too
OWNER_NAMES = {OWNER_HARNESS: "harness", OWNER_DUT: "dut"}

MODES = ("hw", "sw")       # §6.1 HELLO "mode"

# tile encodings (§6.1 table)
E_FILL, E_PAL1, E_PAL2, E_RLE16, E_RAW = 0, 1, 2, 3, 4          # §6.1
ENCODING_NAMES = ("FILL", "PAL1", "PAL2", "RLE16", "RAW")
PAYLOAD_SIZE = {E_FILL: 2, E_PAL1: 36, E_PAL2: 72, E_RAW: TILE_BYTES}   # RLE16 varies
RLE_MAX_RUN = 128                                                # §6.1 n <= 128


class WireError(ValueError):
    """The bytes are not the lcd_mirror wire as this module reads it."""


# --- the MODE word ---------------------------------------------------------------------------


class ModeRegs(NamedTuple):
    """The four registers the mode word carries (§6.1: R16 | R17<<8 | R36<<16 | R01<<24)."""

    r01: int               # display mode (partial, idle, ...)
    r36: int               # panel characteristic (SS/GS/BGR...)
    r17: int               # COLMOD
    r16: int               # MADCTL


def mode_word(regs: bytes | bytearray) -> int:
    """The u32 mode word from a 256-byte register file: R01<<24 | R36<<16 | R17<<8 | R16."""
    return (regs[0x01] << 24) | (regs[0x36] << 16) | (regs[0x17] << 8) | regs[0x16]   # §6.1


def mode_regs(word: int) -> ModeRegs:
    return ModeRegs((word >> 24) & 0xFF, (word >> 16) & 0xFF, (word >> 8) & 0xFF, word & 0xFF)  # §6.1


# --- VALID -----------------------------------------------------------------------------------


def valid_bytes(tiles: Iterable[int]) -> bytes:
    """The 38-byte map: bit t = tile t, LSB first (§6.1)."""
    b = bytearray(VALID_BYTES)
    for t in tiles:
        if not 0 <= t < NTILES:
            raise WireError(f"tile {t} is out of range")
        b[t >> 3] |= 1 << (t & 7)                                  # §6.1 LSB first
    return bytes(b)


def valid_tiles(b: bytes | bytearray) -> frozenset[int]:
    return frozenset(t for t in range(NTILES) if b[t >> 3] >> (t & 7) & 1)   # §6.1


def valid_list(b: bytes | bytearray) -> list[bool]:
    """``[valid(t) for t in range(300)]``: the fast form the compositor scans."""
    return [bool(b[t >> 3] >> (t & 7) & 1) for t in range(NTILES)]


ALL_VALID = valid_bytes(range(NTILES))
NONE_VALID = bytes(VALID_BYTES)


def tile_origin(t: int) -> tuple[int, int]:
    """Tile ``t``'s top-left pixel: ``idx = ty*20 + tx`` (§6.1)."""
    return (t % TILES_X) * TILE, (t // TILES_X) * TILE


# --- status ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusFlags:
    """The UPDATE ``status`` word, named (§6.1)."""

    word: int = 0

    def _bit(self, mask: int) -> bool:
        return bool(self.word & mask)

    rst_n = property(lambda self: self._bit(ST_RST_N))
    bl = property(lambda self: self._bit(ST_BL))
    owner_dut = property(lambda self: self._bit(ST_OWNER))
    display_on = property(lambda self: self._bit(ST_DISPLAY_ON))
    standby = property(lambda self: self._bit(ST_STANDBY))
    in_gram = property(lambda self: self._bit(ST_IN_GRAM))
    fmt_ok = property(lambda self: self._bit(ST_FMT_OK))
    approx = property(lambda self: self._bit(ST_APPROX))
    viol = property(lambda self: self._bit(ST_VIOL))
    oob = property(lambda self: self._bit(ST_OOB))
    rd_seen = property(lambda self: self._bit(ST_RD_SEEN))
    exact = property(lambda self: self._bit(S_EXACT))
    text_only = property(lambda self: self._bit(S_TEXT_ONLY))
    blind = property(lambda self: self._bit(S_BLIND))
    key = property(lambda self: self._bit(S_KEY))
    key_first = property(lambda self: self._bit(S_KEY_FIRST))
    key_last = property(lambda self: self._bit(S_KEY_LAST))
    snap_last = property(lambda self: self._bit(S_SNAP_LAST))

    NAMES = ("rst_n", "bl", "owner_dut", "display_on", "standby", "in_gram", "fmt_ok", "approx",
             "viol", "oob", "rd_seen", "exact", "text_only", "blind")

    def to_json(self) -> dict[str, Any]:
        """The picture's flags (not the framing bits), plus the raw word in hex."""
        out: dict[str, Any] = {name: getattr(self, name) for name in self.NAMES}
        out["word"] = f"0x{self.word & ~S_FRAMING & 0xFFFFFFFF:08x}"
        return out


def owner_name(owner: int) -> str:
    return OWNER_NAMES.get(owner, "unknown")                       # §6.1 3 (reserved) = unknown


# --- messages --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DisplayInfo:
    """HELLO: what the board's lcd_mirror service serves (§6.1)."""

    proto: int = PROTO
    w: int = W
    h: int = H
    fmt: str = FMT
    tile: int = TILE
    mode: str = "hw"                   # hw (the snooper) | sw (the interim software shadow)
    static_id: str = ""
    max_msg: int = MAX_MSG_DEFAULT
    #: Keys this reading does not interpret (H3 adds boot_id, rate_max, clients_max).
    extra: Mapping[str, Any] = field(default_factory=dict)

    def supported(self) -> str:
        """"" when Harness Manager can render this stream, else why not. The proto first: a
        newer proto may well allow a bigger ``max_msg`` (REVIEW-W5 8)."""
        if self.proto != PROTO:
            return f"lcd_mirror proto {self.proto} (this Harness Manager speaks proto {PROTO})"
        if not MAX_MSG_MIN <= self.max_msg <= MAX_MSG_DEFAULT:                  # §6.1
            return f"max_msg {self.max_msg} (outside {MAX_MSG_MIN}..{MAX_MSG_DEFAULT})"
        if (self.w, self.h, self.tile) != (W, H, TILE):
            return f"a {self.w}x{self.h} panel in {self.tile}-pixel tiles (this Harness Manager renders {W}x{H}/{TILE})"
        if self.fmt != FMT:
            return f"pixel format {self.fmt!r} (this Harness Manager renders {FMT!r})"
        if self.mode not in MODES:
            return f"mirror mode {self.mode!r}"
        return ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"proto": self.proto, "w": self.w, "h": self.h, "fmt": self.fmt,
                               "tile": self.tile, "mode": self.mode, "static_id": self.static_id,
                               "max_msg": self.max_msg}
        out.update(self.extra)
        return out


_HELLO_KEYS = ("proto", "w", "h", "fmt", "tile", "mode", "static_id", "max_msg")


def _max_msg(obj: Mapping[str, Any]) -> int:
    """HELLO's ``max_msg``: absent is the default (§6.1); a value that is no number (0, null,
    "", a boolean) is 0, which ``supported`` refuses: never the default (REVIEW-W5 8)."""
    if "max_msg" not in obj:
        return MAX_MSG_DEFAULT
    v = obj["max_msg"]
    if v is None or isinstance(v, bool) or v == "":
        return 0
    return int(v)


def parse_hello(body: bytes) -> DisplayInfo:
    """HELLO's JSON (§6.1). Unknown keys are kept in ``extra``. Anything wrong with it is a
    ``WireError``: deep nesting (``RecursionError``) and ``Infinity`` (``OverflowError``)
    too."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise WireError(f"HELLO is not JSON: {type(exc).__name__}: {str(exc)[:80]}") from None
    if not isinstance(obj, dict):
        raise WireError("HELLO is not a JSON object")
    try:
        max_msg = _max_msg(obj)                                                   # §6.1
        info = DisplayInfo(proto=int(obj.get("proto", 0)), w=int(obj.get("w", 0)),
                           h=int(obj.get("h", 0)), fmt=str(obj.get("fmt", "")),
                           tile=int(obj.get("tile", 0)), mode=str(obj.get("mode", "")),
                           static_id=str(obj.get("static_id", "")), max_msg=max_msg,
                           extra={k: v for k, v in obj.items() if k not in _HELLO_KEYS})
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise WireError(f"HELLO has a malformed field: {type(exc).__name__}: "
                        f"{str(exc)[:80]}") from None
    return info


def hello_body(info: DisplayInfo) -> bytes:
    return json.dumps(info.to_json(), separators=(",", ":")).encode()


@dataclass(frozen=True)
class TileRecord:
    """One tile of an UPDATE. ``raw`` is the record exactly as the board encoded it (the
    5-byte header and the payload): the compositor forwards it to browsers unchanged."""

    idx: int
    enc: int
    payload: bytes
    raw: bytes


@dataclass(frozen=True)
class DisplayUpdate:
    """UPDATE (§6.1), parsed; the tile payloads are validated by ``decode_tile``, not here."""

    seq: int
    t_ms: int
    frames: int
    resets: int
    status: int
    owner: int
    valid: bytes
    regs: bytes | None                   # 256 bytes on key_first, else None
    mode: int | None                     # the mode word when not key_first, else None
    tiles: tuple[TileRecord, ...]

    @property
    def flags(self) -> StatusFlags:
        return StatusFlags(self.status)

    @property
    def key(self) -> bool:
        return bool(self.status & S_KEY)

    @property
    def key_first(self) -> bool:
        return bool(self.status & S_KEY_FIRST)

    @property
    def key_last(self) -> bool:
        return bool(self.status & S_KEY_LAST)

    @property
    def snap_last(self) -> bool:
        return bool(self.status & S_SNAP_LAST)                    # H1

    def mode_regs(self) -> ModeRegs:
        if self.regs is not None:
            return mode_regs(mode_word(self.regs))
        return mode_regs(self.mode or 0)


def parse_update(body: bytes) -> DisplayUpdate:
    """An UPDATE body (after the 8-byte header): §6.1's field order, strictly."""
    mv = memoryview(body)
    if len(body) < UPDATE_FIXED.size:
        raise WireError(f"UPDATE is {len(body)} B, shorter than its {UPDATE_FIXED.size} B header")
    seq, t_ms, frames, resets, status, owner, valid = UPDATE_FIXED.unpack_from(body)   # §6.1
    off = UPDATE_FIXED.size
    regs: bytes | None = None
    mode: int | None = None
    if status & S_KEY_FIRST:                                       # §6.1 regs[256] if key_first
        if len(body) < off + REGS_SIZE + U16.size:
            raise WireError("UPDATE key_first is too short for its 256 REGS")
        regs = bytes(mv[off:off + REGS_SIZE])
        off += REGS_SIZE
    else:                                                          # §6.1 else u32 mode
        if len(body) < off + MODE_WORD.size + U16.size:
            raise WireError("UPDATE is too short for its mode word")
        (mode,) = MODE_WORD.unpack_from(body, off)
        off += MODE_WORD.size
    (ntiles,) = U16.unpack_from(body, off)                         # §6.1 u16 ntiles
    off += U16.size
    if ntiles > NTILES:
        raise WireError(f"UPDATE claims {ntiles} tiles; the panel has {NTILES}")
    tiles = []
    for _ in range(ntiles):
        if off + TILE_RECORD.size > len(body):
            raise WireError("UPDATE ends inside a tile record")
        idx, enc, ln = TILE_RECORD.unpack_from(body, off)          # §6.1 {u16 idx, u8 enc, u16 len}
        end = off + TILE_RECORD.size + ln
        if end > len(body):
            raise WireError(f"tile {idx}'s payload runs {end - len(body)} B past the UPDATE")
        if idx >= NTILES:
            raise WireError(f"tile index {idx} is out of range")
        if enc > E_RAW:
            raise WireError(f"tile {idx} has unknown encoding {enc}")
        tiles.append(TileRecord(idx, enc, bytes(mv[off + TILE_RECORD.size:end]), bytes(mv[off:end])))
        off = end
    if off != len(body):
        raise WireError(f"UPDATE has {len(body) - off} trailing bytes")
    return DisplayUpdate(seq, t_ms, frames, resets, status, owner, bytes(valid), regs, mode,
                         tuple(tiles))


class Pong(NamedTuple):
    token: int


class RateEcho(NamedTuple):
    hz: int


class Refusal(NamedTuple):
    """Amendment 3: ``{"ok":false,"err":"lcd_mirror: ..."}``, then close."""

    err: str
    line: str


class Unknown(NamedTuple):
    type: int
    body: bytes


Message = DisplayInfo | DisplayUpdate | Pong | RateEcho | Refusal | Unknown


def decode_message(typ: int, body: bytes) -> Message:
    """A framed board -> HM message, typed. Unknown types are returned, not refused
    (forward compatible: a newer board may add messages HM does not read)."""
    if typ == T_HELLO:
        return parse_hello(body)
    if typ == T_UPDATE:
        return parse_update(body)
    if typ == T_PONG:
        if len(body) < 4:
            raise WireError("PONG is shorter than its u32")
        return Pong(U32.unpack_from(body)[0])                     # §6.1 u32
    if typ == T_RATE:
        if len(body) < 1:
            raise WireError("RATE echo is empty")
        return RateEcho(body[0])                                   # §6.1 u8
    return Unknown(typ, body)


def parse_refusal(line: bytes) -> Refusal:
    """The refusal line. Its JSON is the board's; when it will not parse (malformed, nested
    past the recursion limit) the line itself is the reason."""
    text = line.decode("utf-8", "replace").strip()
    err = text
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("err"):
            err = str(obj["err"])
    except (ValueError, RecursionError):
        pass
    return Refusal(err, text)


def _decoded(typ: int, body: bytes) -> Message:
    """``decode_message``, where only ``WireError`` escapes (REVIEW-W5 8): the stream is the
    board's, untrusted, and the compositor's reconnect path catches exactly that."""
    try:
        return decode_message(typ, body)
    except WireError:
        raise
    except Exception as exc:  # noqa: BLE001 - any other failure decoding is a bad message
        raise WireError(f"message type 0x{typ:02x} could not be read: {type(exc).__name__}: "
                        f"{str(exc)[:80]}") from None


class MessageReader:
    """An incremental reader of the board's byte stream: ``feed(bytes) -> [Message]``.
    Only ``WireError`` escapes ``feed``.

    Framing per §6.1. A ``{`` where a message would start is the refusal line (the board
    closes after it): the reader returns a ``Refusal`` and reads nothing more. ``max_msg``
    bounds a whole message, header included (set it from HELLO).
    """

    def __init__(self, max_msg: int = MAX_MSG_DEFAULT) -> None:
        self.max_msg = max_msg
        self._buf = bytearray()
        self.done = False                  # a refusal was read: the stream is over
        self.bytes = 0

    def feed(self, data: bytes) -> list[Message]:
        if self.done:
            return []
        self._buf += data
        self.bytes += len(data)
        out: list[Message] = []
        buf = self._buf
        while buf:
            if buf[0] == REFUSAL_BYTE:                             # §6.1 the refusal line
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > REFUSAL_MAX:
                        out.append(parse_refusal(bytes(buf[:REFUSAL_MAX])))
                        self.done = True
                        buf.clear()
                    break
                out.append(parse_refusal(bytes(buf[:nl])))
                self.done = True
                buf.clear()
                break
            if len(buf) < HEADER_SIZE:
                break
            magic, typ, _rsvd, ln = HEADER.unpack_from(buf)          # §6.1 rsvd is not checked
            if magic != MAGIC:
                raise WireError(f"not an lcd_mirror message (magic {bytes(magic)!r})")
            if HEADER_SIZE + ln > self.max_msg:                      # H3: the header counts
                raise WireError(f"message of {HEADER_SIZE + ln} B is over max_msg {self.max_msg}")
            if len(buf) < HEADER_SIZE + ln:
                break
            body = bytes(buf[HEADER_SIZE:HEADER_SIZE + ln])
            del buf[:HEADER_SIZE + ln]
            out.append(_decoded(typ, body))
        return out

    def eof(self) -> Refusal | None:
        """At end of stream: a refusal line cut short by the close, if that is what is left."""
        if not self.done and self._buf[:1] == bytes([REFUSAL_BYTE]):
            self.done = True
            return parse_refusal(bytes(self._buf))
        return None

    @property
    def pending(self) -> int:
        return len(self._buf)


# --- building messages -----------------------------------------------------------------------


def message(typ: int, body: bytes = b"") -> bytes:
    return HEADER.pack(MAGIC, typ, 0, len(body)) + body               # §6.1 rsvd = 0


def key_msg() -> bytes:
    return message(T_KEY)                                              # §6.1 empty body


def rate_msg(hz: int) -> bytes:
    return message(T_RATE, bytes([max(0, min(RATE_MAX, int(hz)))]))  # §6.1 u8 hz


def ping_msg(token: int) -> bytes:
    return message(T_PING, U32.pack(token & 0xFFFFFFFF))              # §6.1 u32


def pong_msg(token: int) -> bytes:
    return message(T_PONG, U32.pack(token & 0xFFFFFFFF))              # §6.1 u32


def ack_msg(seq: int) -> bytes:
    """Every UPDATE up to ``seq`` is consumed (H1, cumulative). ``seq`` 0 acknowledges
    nothing on a fresh connection: the first UPDATE is seq 1 (correction 2)."""
    return message(T_ACK, U32.pack(seq & 0xFFFFFFFF))                 # H1 u32 seq


def hello_msg(info: DisplayInfo) -> bytes:
    return message(T_HELLO, hello_body(info))


def refusal_line(err: str) -> bytes:
    """``{"ok":false,"err":"lcd_mirror: busy (2 clients)"}\\n`` (or ``not loopback (use an ssh
    port forward)``): the board's two refusals, byte for byte."""
    return (json.dumps({"ok": False, "err": err}, separators=(",", ":")) + "\n").encode()   # §6.1


def record(idx: int, enc: int, payload: bytes) -> bytes:
    return TILE_RECORD.pack(idx, enc, len(payload)) + payload          # §6.1


def update_body(seq: int, t_ms: int, frames: int, resets: int, status: int, owner: int,
                valid: bytes, records: Iterable[bytes], *, regs: bytes | None = None,
                mode: int | None = None) -> bytes:
    """An UPDATE body. ``regs`` (256 B) rides on ``key_first``; every other UPDATE carries
    the mode word (``mode``, or the one ``regs`` implies)."""
    recs = list(records)
    head = UPDATE_FIXED.pack(seq & 0xFFFFFFFF, t_ms & 0xFFFFFFFF, frames & 0xFFFFFFFF,
                             resets & 0xFFFFFFFF, status & 0xFFFFFFFF, owner & 0xFF, valid)
    if status & S_KEY_FIRST:                                           # §6.1
        if regs is None or len(regs) != REGS_SIZE:
            raise WireError("a key_first UPDATE carries the 256 REGS")
        mid = bytes(regs)
    else:
        word = mode if mode is not None else (mode_word(regs) if regs is not None else 0)
        mid = MODE_WORD.pack(word & 0xFFFFFFFF)
    return head + mid + U16.pack(len(recs)) + b"".join(recs)


def update_msg(*args: Any, **kw: Any) -> bytes:
    return message(T_UPDATE, update_body(*args, **kw))


def part_budget(max_msg: int, key: bool) -> int:
    """Record bytes one UPDATE of at most ``max_msg`` (header included, H3) can carry."""
    return max_msg - (HEADER_SIZE + UPDATE_FIXED.size + (REGS_SIZE if key else MODE_WORD.size)
                      + U16.size)                                      # H3


def split_records(records: list[bytes], budget: int) -> list[list[bytes]]:
    """Cut a SNAP's records into parts of at most ``budget`` bytes (at least one record
    each, and at least one part even when there are no records)."""
    parts: list[list[bytes]] = []
    cur: list[bytes] = []
    size = 0
    for r in records:
        if cur and size + len(r) > budget:
            parts.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += len(r)
    if cur or not parts:
        parts.append(cur)
    return parts


# --- the tile codec (§6.1 table) -------------------------------------------------------------

#: A byte of PAL1 row bits -> its 8 pixel indices, pixel x = bit x (LSB first)     # §6.1
_EXPAND1 = [bytes((b >> i) & 1 for i in range(8)) for b in range(256)]
#: A byte of PAL2 row bits -> its 4 pixel indices, pixel x = bits [2x+1:2x]        # §6.1
_EXPAND2 = [bytes((b >> (2 * i)) & 3 for i in range(4)) for b in range(256)]
_ZERO_TABLE = bytes(256)


def _paint(indices: bytes, colours: bytes) -> bytes:
    """256 palette indices + the palette (u16 LE each) -> 512 bytes of pixels, in C loops."""
    lo, hi = bytearray(_ZERO_TABLE), bytearray(_ZERO_TABLE)
    lo[0:len(colours) // 2] = colours[0::2]
    hi[0:len(colours) // 2] = colours[1::2]
    out = bytearray(TILE_BYTES)
    out[0::2] = indices.translate(lo)
    out[1::2] = indices.translate(hi)
    return bytes(out)


def decode_tile(enc: int, payload: bytes) -> bytes:
    """One tile record's payload -> 512 bytes of RGB565 LE pixels, row-major (§6.1)."""
    n = len(payload)
    if enc == E_FILL:                                                  # §6.1 u16
        if n != 2:
            raise WireError(f"FILL is 2 B, not {n}")
        return payload * TILE_PX
    if enc == E_RAW:                                                   # §6.1 256 x u16
        if n != TILE_BYTES:
            raise WireError(f"RAW is {TILE_BYTES} B, not {n}")
        return bytes(payload)
    if enc == E_PAL1:                                                  # §6.1 2 x u16, 16 x u16 rows
        if n != 36:
            raise WireError(f"PAL1 is 36 B, not {n}")
        return _paint(b"".join(map(_EXPAND1.__getitem__, payload[4:36])), payload[0:4])
    if enc == E_PAL2:                                                  # §6.1 4 x u16, 16 x u32 rows
        if n != 72:
            raise WireError(f"PAL2 is 72 B, not {n}")
        return _paint(b"".join(map(_EXPAND2.__getitem__, payload[8:72])), payload[0:8])
    if enc == E_RLE16:                                                 # §6.1 PackBits over u16
        out = bytearray()
        i = 0
        while i < n:
            t = payload[i]
            i += 1
            if t & 0x80:                                               # 0x80|(n-1), one u16: a run
                px = payload[i:i + 2]
                if len(px) != 2:
                    raise WireError("RLE16 run is cut short")
                out += px * ((t & 0x7F) + 1)
                i += 2
            else:                                                      # n-1, then n u16: literals
                k = 2 * (t + 1)
                lit = payload[i:i + k]
                if len(lit) != k:
                    raise WireError("RLE16 literal is cut short")
                out += lit
                i += k
            if len(out) > TILE_BYTES:
                raise WireError("RLE16 runs past the tile's 256 pixels")
        if len(out) != TILE_BYTES:
            raise WireError(f"RLE16 gave {len(out) // 2} pixels, not {TILE_PX}")
        return bytes(out)
    raise WireError(f"unknown tile encoding {enc}")


def _rle16(px: list[int], raw: bytes) -> bytes:
    """PackBits over u16 (§6.1), the board's choice of cuts (lcdmirror_enc.c / the spike)."""
    out = bytearray()
    i, n = 0, len(px)
    while i < n:
        j = i + 1
        while j < n and px[j] == px[i] and j - i < RLE_MAX_RUN:
            j += 1
        if j - i >= 2:
            out.append(0x80 | (j - i - 1))
            out += raw[2 * i:2 * i + 2]
            i = j
        else:
            s = k = i
            while k < n and k - s < RLE_MAX_RUN:
                if k + 1 < n and px[k + 1] == px[k]:
                    break
                k += 1
            if k == s:
                k = s + 1
            out.append(k - s - 1)
            out += raw[2 * s:2 * k]
            i = k
    return bytes(out)


def encode_tile(px: bytes) -> tuple[int, bytes]:
    """512 bytes of RGB565 LE pixels -> (encoding, payload), the smallest the board would
    pick: FILL for one colour; else RLE16 if under 512 B (else RAW); then PAL1 (2 colours)
    or PAL2 (3-4) only if STRICTLY smaller. Harness Manager only decodes on the product
    path: this is for the fake board, the tests and the wire vectors."""
    if len(px) != TILE_BYTES:
        raise WireError(f"a tile is {TILE_BYTES} B, not {len(px)}")
    vals = list(_PIXELS.unpack(px))
    pal: list[int] = []
    for v in vals:
        if v not in pal:
            pal.append(v)
            if len(pal) > 4:
                break
    if len(pal) == 1:
        return E_FILL, bytes(px[0:2])
    best: tuple[int, bytes] = (E_RAW, bytes(px))
    r = _rle16(vals, px)
    if len(r) < TILE_BYTES:
        best = (E_RLE16, r)
    if len(pal) == 2 and 36 < len(best[1]):
        rows = [sum(1 << x for x in range(TILE) if vals[y * TILE + x] == pal[1]) for y in range(TILE)]
        best = (E_PAL1, struct.pack("<2H16H", *pal, *rows))            # §6.1
    elif 3 <= len(pal) <= 4 and 72 < len(best[1]):
        pal4 = pal + [pal[0]] * (4 - len(pal))
        rows = [sum(pal4.index(vals[y * TILE + x]) << (2 * x) for x in range(TILE)) for y in range(TILE)]
        best = (E_PAL2, struct.pack("<4H16I", *pal4, *rows))           # §6.1
    return best


# --- frames: row-major viewer order <-> tiles --------------------------------------------------


def tile_of_frame(frame: bytes | bytearray | memoryview, t: int) -> bytes:
    """Tile ``t`` (512 B, row-major) out of a row-major 320x240 RGB565 LE frame."""
    x0, y0 = tile_origin(t)
    stride = W * 2
    o = y0 * stride + x0 * 2
    return b"".join(bytes(frame[o + r * stride:o + r * stride + TILE * 2]) for r in range(TILE))


def put_tile_in_frame(frame: bytearray, t: int, px: bytes) -> None:
    x0, y0 = tile_origin(t)
    stride = W * 2
    o = y0 * stride + x0 * 2
    for r in range(TILE):
        frame[o + r * stride:o + r * stride + TILE * 2] = px[r * TILE * 2:(r + 1) * TILE * 2]


def encode_records(frame: bytes | bytearray, tiles: Iterable[int]) -> list[bytes]:
    """Tile records for ``tiles`` (index order) out of a row-major frame."""
    out = []
    for t in sorted(tiles):
        enc, payload = encode_tile(tile_of_frame(frame, t))
        out.append(record(t, enc, payload))
    return out
