"""The lcd_mirror wire as Harness Manager reads it (docs/design/LCD_MIRROR.md §6.1; lane LM1).

Hand-assembled byte vectors pin every §6.1 assumption independently of HM's own encoder
(an encoder/decoder pair that shares a mistake would otherwise pass its own round trip).
When the Linux lead checks in wire vectors (H1), they replace the ``VECTOR_*`` literals.
Negative twins: the same checks with a deliberate mistake must fail.
"""

from __future__ import annotations

import os
import random
import struct

import pytest

from harness_manager.core import display_wire as w

# --- framing and client messages (literal bytes) -------------------------------------------------


def test_client_messages_are_the_literal_bytes() -> None:
    assert w.key_msg() == bytes.fromhex("4c4d1000" "00000000")
    assert w.rate_msg(20) == bytes.fromhex("4c4d1100" "01000000" "14")
    assert w.rate_msg(999) == bytes.fromhex("4c4d1100" "01000000" "ff")       # a u8
    assert w.ping_msg(7) == bytes.fromhex("4c4d1200" "04000000" "07000000")
    assert w.ack_msg(0x01020304) == bytes.fromhex("4c4d1400" "04000000" "04030201")   # H1


def test_valid_map_is_lsb_first() -> None:
    b = w.valid_bytes({0, 9, 299})
    assert len(b) == 38
    assert b[0] == 0x01 and b[1] == 0x02 and b[37] == 0x08
    assert w.valid_tiles(b) == {0, 9, 299}
    assert w.valid_tiles(w.ALL_VALID) == set(range(300))
    with pytest.raises(w.WireError):
        w.valid_bytes({300})


def test_mode_word_layout() -> None:
    regs = bytearray(256)
    regs[0x01], regs[0x36], regs[0x17], regs[0x16] = 0x11, 0x09, 0x05, 0x20
    assert w.mode_word(regs) == 0x11090520
    assert w.mode_regs(0x11090520) == w.ModeRegs(0x11, 0x09, 0x05, 0x20)


# One UPDATE, assembled by hand from §6.1: seq 1, t_ms 2, frames 3, resets 4, status fmt_ok,
# owner harness, tile 0 valid, mode {R01 0, R36 0x09, R17 0x05, R16 0x20}, one FILL tile.
VECTOR_UPDATE = bytes.fromhex(
    "4c4d0200" "48000000"                    # 'L' 'M' UPDATE rsvd, len 72 (59 + 4 + 2 + 7)
    "01000000" "02000000" "03000000" "04000000" "40000000"   # seq t_ms frames resets status
    "00"                                     # owner
    "01" + "00" * 37 +                       # valid[38]: tile 0
    "20050900"                               # mode word LE: R16 R17 R36 R01
    "0100"                                   # ntiles
    "0000" "00" "0200" "3412")               # idx 0, FILL, len 2, 0x1234 LE


def test_update_vector_parses_field_by_field() -> None:
    r = w.MessageReader()
    (u,) = r.feed(VECTOR_UPDATE)
    assert isinstance(u, w.DisplayUpdate)
    assert (u.seq, u.t_ms, u.frames, u.resets, u.status, u.owner) == (1, 2, 3, 4, 0x40, 0)
    assert w.valid_tiles(u.valid) == {0}
    assert u.regs is None and u.mode == 0x00090520
    assert u.mode_regs() == w.ModeRegs(0x00, 0x09, 0x05, 0x20)
    (rec,) = u.tiles
    assert (rec.idx, rec.enc, rec.payload) == (0, w.E_FILL, b"\x34\x12")
    assert rec.raw == bytes.fromhex("0000" "00" "0200" "3412")
    assert w.decode_tile(rec.enc, rec.payload) == b"\x34\x12" * 256
    assert u.flags.fmt_ok and not u.flags.exact and not u.key


def test_update_builder_reproduces_the_vector() -> None:
    body = w.update_body(1, 2, 3, 4, 0x40, 0, w.valid_bytes({0}), [w.record(0, w.E_FILL, b"\x34\x12")],
                         mode=0x00090520)
    assert w.message(w.T_UPDATE, body) == VECTOR_UPDATE


def test_key_first_carries_regs_not_the_mode_word() -> None:
    regs = bytes(range(256))
    status = w.S_KEY | w.S_KEY_FIRST | w.S_KEY_LAST | w.S_SNAP_LAST
    body = w.update_body(9, 0, 0, 0, status, 1, w.ALL_VALID, [], regs=regs)
    assert len(body) == 59 + 256 + 2
    u = w.parse_update(body)
    assert u.regs == regs and u.mode is None and u.key and u.key_first and u.key_last and u.snap_last
    assert u.owner == w.OWNER_DUT
    with pytest.raises(w.WireError):
        w.update_body(9, 0, 0, 0, status, 1, w.ALL_VALID, [])      # key_first without REGS


def test_status_bits() -> None:
    f = w.StatusFlags(w.ST_RST_N | w.ST_BL | w.ST_DISPLAY_ON | w.ST_FMT_OK | w.ST_VIOL | w.S_BLIND
                      | w.S_TEXT_ONLY | w.S_SNAP_LAST)
    assert f.rst_n and f.bl and f.display_on and f.fmt_ok and f.viol and f.blind and f.text_only
    assert not (f.standby or f.approx or f.exact or f.oob or f.rd_seen or f.owner_dut or f.key)
    assert f.snap_last
    j = f.to_json()
    assert j["viol"] is True and j["exact"] is False
    assert j["word"] == f"0x{(f.word & ~w.S_FRAMING):08x}"
    assert (w.ST_VIOL, w.S_EXACT, w.S_TEXT_ONLY, w.S_BLIND) == (1 << 8, 1 << 16, 1 << 17, 1 << 18)
    assert (w.S_KEY, w.S_KEY_FIRST, w.S_KEY_LAST, w.S_SNAP_LAST) == (1 << 24, 1 << 25, 1 << 26, 1 << 27)


def test_owner_names() -> None:
    assert [w.owner_name(o) for o in (0, 1, 2, 3, 200)] == ["harness", "dut", "unknown", "unknown", "unknown"]


# --- HELLO -----------------------------------------------------------------------------------------


def test_hello_parses_and_keeps_extras() -> None:
    body = (b'{"proto":1,"w":320,"h":240,"fmt":"rgb565le","tile":16,"mode":"sw",'
            b'"static_id":"0x44EE76D5","max_msg":65536,"boot_id":"b1","rate":5,"rate_max":30,'
            b'"clients_max":2}')
    (info,) = w.MessageReader().feed(w.message(w.T_HELLO, body))
    assert isinstance(info, w.DisplayInfo)
    assert (info.mode, info.static_id, info.max_msg) == ("sw", "0x44EE76D5", 65536)
    assert info.extra == {"boot_id": "b1", "rate": 5, "rate_max": 30, "clients_max": 2}
    assert info.supported() == ""
    assert w.parse_hello(w.hello_body(info)) == info


@pytest.mark.parametrize("patch, words", [
    ({"proto": 2}, "proto 2"), ({"w": 480}, "480x240"), ({"fmt": "rgb565be"}, "rgb565be"),
    ({"mode": "xx"}, "mode 'xx'")])
def test_hello_this_hm_cannot_render_says_why(patch: dict, words: str) -> None:
    info = w.DisplayInfo(**{**{"mode": "hw"}, **patch})
    assert words in info.supported()


def test_hello_that_is_not_json() -> None:
    with pytest.raises(w.WireError):
        w.parse_hello(b"not json")
    with pytest.raises(w.WireError):
        w.parse_hello(b"[1,2]")


# --- the reader ------------------------------------------------------------------------------------


def test_reader_takes_a_stream_byte_by_byte() -> None:
    stream = w.hello_msg(w.DisplayInfo()) + VECTOR_UPDATE + w.pong_msg(5) + w.message(w.T_RATE, b"\x14") \
        + w.message(0x7F, b"future")
    r = w.MessageReader()
    got = []
    for i in range(len(stream)):
        got += r.feed(stream[i:i + 1])
    assert [type(m).__name__ for m in got] == ["DisplayInfo", "DisplayUpdate", "Pong", "RateEcho", "Unknown"]
    assert got[2] == w.Pong(5) and got[3] == w.RateEcho(20) and got[4] == w.Unknown(0x7F, b"future")
    assert r.pending == 0 and r.bytes == len(stream)


def test_refusal_line_ends_the_stream() -> None:
    line = w.refusal_line("lcd_mirror: busy (2 clients)")
    assert line == b'{"ok":false,"err":"lcd_mirror: busy (2 clients)"}\n'
    r = w.MessageReader()
    assert r.feed(line[:10]) == []
    (ref,) = r.feed(line[10:] + VECTOR_UPDATE)
    assert isinstance(ref, w.Refusal) and ref.err == "lcd_mirror: busy (2 clients)"
    assert r.done and r.feed(VECTOR_UPDATE) == []


def test_refusal_cut_short_by_the_close() -> None:
    r = w.MessageReader()
    assert r.feed(b'{"ok":false,"err":"lcd_mirror: no') == []
    ref = r.eof()
    assert isinstance(ref, w.Refusal) and "lcd_mirror: no" in ref.err


def test_reader_refuses_what_is_not_the_wire() -> None:
    with pytest.raises(w.WireError):
        w.MessageReader().feed(b"SSH-2.0-dropbear\r\n")
    big = w.HEADER.pack(b"LM", w.T_UPDATE, 0, 70000)
    with pytest.raises(w.WireError):
        w.MessageReader(max_msg=65536).feed(big)


@pytest.mark.parametrize("cut", ["short", "trailing", "idx", "enc", "ntiles", "record"])
def test_malformed_updates_are_refused(cut: str) -> None:
    body = bytearray(VECTOR_UPDATE[8:])
    if cut == "short":
        body = body[:40]
    elif cut == "trailing":
        body += b"\x00"
    elif cut == "idx":
        body[-7:-5] = struct.pack("<H", 300)
    elif cut == "enc":
        body[-5] = 5
    elif cut == "ntiles":
        body[-9:-7] = struct.pack("<H", 301)
    elif cut == "record":
        body[-4:-2] = struct.pack("<H", 9)
    with pytest.raises(w.WireError):
        w.parse_update(bytes(body))


# --- the five encodings: hand-built payloads -----------------------------------------------------


def px(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}H", *values)


def test_pal1_bit_x_selects_colour_1_lsb_first() -> None:
    c0, c1 = 0x0000, 0xFFFF
    rows = [1 << y for y in range(16)]                     # the diagonal
    payload = struct.pack("<2H16H", c0, c1, *rows)
    assert payload[4:6] == b"\x01\x00"                     # row 0: only pixel 0 set
    tile = w.decode_tile(w.E_PAL1, payload)
    want = b"".join(px(*(c1 if x == y else c0 for x in range(16))) for y in range(16))
    assert tile == want


def test_pal1_negative_twin_msb_first_is_caught() -> None:
    """The same payload read MSB first (a plausible misreading) must NOT give the picture."""
    payload = struct.pack("<2H16H", 0, 0xFFFF, *[1 << y for y in range(16)])
    tile = w.decode_tile(w.E_PAL1, payload)
    msb = b"".join(px(*(0xFFFF if x == 15 - y else 0 for x in range(16))) for y in range(16))
    assert tile != msb


def test_pal2_pixel_x_is_bits_2x_plus_1_to_2x() -> None:
    pal = (0x0001, 0x0002, 0x0003, 0x0004)
    rows = [sum(((x + y) % 4) << (2 * x) for x in range(16)) for y in range(16)]
    payload = struct.pack("<4H16I", *pal, *rows)
    assert payload[8:12] == struct.pack("<I", 0xE4E4E4E4)      # row 0: 0,1,2,3,0,1,...
    tile = w.decode_tile(w.E_PAL2, payload)
    assert tile == b"".join(px(*(pal[(x + y) % 4] for x in range(16))) for y in range(16))


def test_rle16_runs_and_literals() -> None:
    payload = bytes([0x80 | 127]) + px(0xAAAA) + bytes([0x80 | 125]) + px(0xBBBB) \
        + bytes([0x02]) + px(1, 2, 3)                      # 128 + 126 + 3 = 257: one too many
    with pytest.raises(w.WireError):
        w.decode_tile(w.E_RLE16, payload)
    payload = bytes([0x80 | 127]) + px(0xAAAA) + bytes([0x80 | 124]) + px(0xBBBB) \
        + bytes([0x02]) + px(1, 2, 3)                      # 128 + 125 + 3 = 256
    assert w.decode_tile(w.E_RLE16, payload) == px(0xAAAA) * 128 + px(0xBBBB) * 125 + px(1, 2, 3)


@pytest.mark.parametrize("enc, payload", [
    (w.E_FILL, b"\x00\x00\x00"), (w.E_RAW, bytes(510)), (w.E_PAL1, bytes(35)), (w.E_PAL2, bytes(73)),
    (w.E_RLE16, bytes([0x80 | 127])), (w.E_RLE16, bytes([0x05]) + px(1, 2)), (w.E_RLE16, b""),
    (5, bytes(2))])
def test_malformed_tiles_are_refused(enc: int, payload: bytes) -> None:
    with pytest.raises(w.WireError):
        w.decode_tile(enc, payload)


def test_rgb565_is_little_endian_negative_twin() -> None:
    """Amendment 1: LE. A big-endian reading of the same FILL must not give the colour."""
    tile = w.decode_tile(w.E_FILL, b"\x00\xf8")             # 0xF800 = red, LE
    assert struct.unpack_from("<H", tile)[0] == 0xF800
    assert struct.unpack_from(">H", tile)[0] != 0xF800


# --- the encoder (the fake board's) against the decoder ------------------------------------------


def _tiles(seed: int) -> list[bytes]:
    rnd = random.Random(seed)
    out = [px(0x1234) * 256, os.urandom(512)]
    for ncol in (2, 3, 4, 5, 8):
        pal = [rnd.randrange(65536) for _ in range(ncol)]
        out.append(px(*(rnd.choice(pal) for _ in range(256))))
        out.append(px(*(pal[(i // 37) % ncol] for i in range(256))))      # runs
    out.append(px(*range(256)))                                          # a gradient
    return out


@pytest.mark.parametrize("seed", range(4))
def test_every_encoding_round_trips(seed: int) -> None:
    seen = set()
    for tile in _tiles(seed):
        enc, payload = w.encode_tile(tile)
        seen.add(enc)
        assert w.decode_tile(enc, payload) == tile
        size = {w.E_FILL: 2, w.E_PAL1: 36, w.E_PAL2: 72, w.E_RAW: 512}.get(enc)
        assert size is None or len(payload) == size
    assert seen >= {w.E_FILL, w.E_RAW, w.E_RLE16}


def test_encoder_picks_the_smallest() -> None:
    two = px(*((0xFFFF if (x + y) % 2 else 0) for y in range(16) for x in range(16)))
    assert w.encode_tile(two)[0] == w.E_PAL1                 # RLE would be 512+
    four = px(*((x % 4) * 0x1111 for y in range(16) for x in range(16)))
    assert w.encode_tile(four)[0] == w.E_PAL2
    runs = px(*([7] * 128 + [9] * 128))
    assert w.encode_tile(runs) == (w.E_RLE16, bytes([0xFF]) + px(7) + bytes([0xFF]) + px(9))


def test_split_records_respects_max_msg() -> None:
    frame = os.urandom(w.FRAME_BYTES)
    recs = w.encode_records(frame, range(300))
    for key in (True, False):
        parts = w.split_records(recs, w.part_budget(65536, key))
        assert len(parts) >= 3 and sum(len(p) for p in parts) == 300
        for i, p in enumerate(parts):
            status = (w.S_KEY_FIRST if key and i == 0 else 0)
            m = w.update_msg(i, 0, 0, 0, status, 0, w.ALL_VALID, p, regs=bytes(256), mode=0)
            assert len(m) <= 65536                            # H3: max_msg counts the header
    assert w.split_records([], 100) == [[]]


def test_frame_tile_helpers_invert() -> None:
    frame = bytearray(os.urandom(w.FRAME_BYTES))
    t = 21                                                   # tx 1, ty 1
    assert w.tile_origin(t) == (16, 16)
    tile = w.tile_of_frame(frame, t)
    assert tile[:32] == bytes(frame[(16 * 320 + 16) * 2:(16 * 320 + 32) * 2])
    other = bytearray(w.FRAME_BYTES)
    w.put_tile_in_frame(other, t, tile)
    assert w.tile_of_frame(other, t) == tile
