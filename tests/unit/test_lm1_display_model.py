"""The display model, the golden pictures and the badges (docs/design/LCD_MIRROR.md §3, §7.4;
lane LM1).

Golden: the recorded harness byte streams (``tests/spikes/lcd_mirror_data``, the exact
bytes ``clcd.c`` pushes) go through the ONE panel model (``Hx8347dShadow``), are tiled,
encoded and cut into UPDATEs as the board does, parsed and decoded by Harness Manager,
and must come out equal to BOTH the model's picture and the independent reference
(``tools/clcd_mock.py`` rasterising the renderer's own grid). Negative twins: a flipped bit
and a wrong MADCTL geometry must be caught.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from array import array

import pytest

from harness_manager.core import display as D
from harness_manager.core import display_wire as w
from tests.fakes import lm1_golden as G
from tests.fakes.lm1_fake_lcd_mirror import baseline_regs

W, H = 320, 240


def through_the_wire(frame: bytes, regs: bytes, *, valid: set[int] | None = None,
                     max_msg: int = 65536, status: int = w.ST_FMT_OK | w.ST_BL | w.ST_DISPLAY_ON,
                     owner: int = w.OWNER_HARNESS) -> D.DisplayFrame:
    """A keyframe of ``frame`` as the board sends it, read back by Harness Manager."""
    valid = set(range(300)) if valid is None else valid
    recs = w.encode_records(frame, valid)
    parts = w.split_records(recs, w.part_budget(max_msg, True))
    reader = w.MessageReader(max_msg)
    updates = []
    for i, chunk in enumerate(parts):
        last = i == len(parts) - 1
        flags = w.S_KEY | (w.S_KEY_FIRST if i == 0 else 0) | ((w.S_KEY_LAST | w.S_SNAP_LAST) if last else 0)
        m = w.update_msg(100 + i, 5, 1, 0, status | flags, owner, w.valid_bytes(valid), chunk, regs=regs)
        assert len(m) <= max_msg
        updates += reader.feed(m)
    f = D.DisplayFrame()
    f.commit(updates, D.decode_parts(updates), key=True)
    return f


def diff_pixels(a: bytes, b: bytes) -> int:
    va, vb = struct.unpack(f"<{W * H}H", a), struct.unpack(f"<{W * H}H", b)
    return sum(1 for x, y in zip(va, vb, strict=True) if x != y)


def rot180(frame: bytes) -> bytes:
    a = array("H")
    a.frombytes(frame)
    return G.le_bytes(array("H", reversed(a)))


# --- golden pictures ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", G.HARNESS_STREAMS)
def test_recorded_harness_stream_through_the_wire_is_the_golden_picture(name: str) -> None:
    model, grid, regs = G.harness_pictures()[name]
    assert model == grid                                       # the model agrees with the renderer
    f = through_the_wire(model, regs)
    assert f.rgb565() == model
    assert f.hatched() == 0 and f.presented
    assert D.badges(f.flags(), f.owner, "sw", f.panel_regs()) == []   # the calibrated anchor


def test_split_keyframe_of_small_max_msg_is_the_same_picture() -> None:
    model, _grid, regs = G.harness_pictures()["boot"]
    f = through_the_wire(model, regs, max_msg=1024)
    assert f.rgb565() == model


def test_dut_picture_after_a_kvm_handover() -> None:
    model, rects, regs = G.nanosoc_after_handover()
    assert model == rects
    f = through_the_wire(model, regs, owner=w.OWNER_DUT)
    assert f.rgb565() == rects
    assert [b.key for b in D.badges(f.flags(), f.owner, "hw", f.panel_regs())] == ["held"]


@pytest.mark.parametrize("counter", [0, 1, 0x5A5A, 0xFFFF])
def test_clcd_demo_card(counter: int) -> None:
    card = G.card_picture(counter)
    assert through_the_wire(card, baseline_regs()).rgb565() == card


def test_card_picture_is_card_pixel() -> None:
    for counter in (0, 0xA5C3):
        slow = G.le_bytes(array("H", (G.card_pixel(x, y, counter) for y in range(H) for x in range(W))))
        assert G.card_picture(counter) == slow
        assert G.card_counter(slow) == counter


def test_noise_frame_every_encoding_path() -> None:
    frame = bytearray(os.urandom(w.FRAME_BYTES))
    for t, colour in ((0, 0x1234), (1, 0xFFFF)):
        w.put_tile_in_frame(frame, t, colour.to_bytes(2, "little") * 256)
    frame = bytes(frame)
    assert through_the_wire(frame, baseline_regs()).rgb565() == frame


# --- negative twins: a mirror that cannot fail proves nothing ------------------------------------


def test_negative_twin_one_flipped_bit_is_caught() -> None:
    boot = G.boot_stream()
    k = next(i for i in range(len(boot) - 1, 0, -1) if boot[i][0] == G.RS_DATA)
    flipped = boot[:k] + [(G.RS_DATA, boot[k][1] ^ 0x01)] + boot[k + 1:]
    sh = G.shadow()
    sh.feed(flipped)
    _model, grid, _regs = G.harness_pictures()["boot"]
    f = through_the_wire(G.viewer_bytes(sh), bytes(sh.regs))
    assert diff_pixels(f.rgb565(), grid) == 1


def _with_madctl(value: int) -> tuple[bytes, bytes]:
    boot = G.boot_stream()
    m = next(i for i in range(len(boot) - 1) if boot[i] == (G.RS_CMD, 0x16))
    sh = G.shadow()
    sh.feed(boot[:m + 1] + [(G.RS_DATA, value)] + boot[m + 2:])
    return G.viewer_bytes(sh), bytes(sh.regs)


def test_negative_twin_madctl_e0_is_the_180_degree_image() -> None:
    _model, grid, _regs = G.harness_pictures()["boot"]
    frame, regs = _with_madctl(0xE0)
    f = through_the_wire(frame, regs)
    assert f.rgb565() != grid
    assert f.rgb565() == rot180(grid)
    assert f.panel_regs().r16 == 0xE0 and "inexact" not in [b.key for b in
                                                            D.badges(f.flags(), 0, "hw", f.panel_regs())]


def test_negative_twin_wrong_madctl_geometry_is_caught_and_badged() -> None:
    _model, grid, _regs = G.harness_pictures()["boot"]
    frame, regs = _with_madctl(0x00)                         # no MV: the portrait geometry
    f = through_the_wire(frame, regs)
    assert diff_pixels(f.rgb565(), grid) > 10_000
    b = D.badges(f.flags(), 0, "hw", f.panel_regs())
    assert [x.key for x in b] == ["inexact"] and "R16=0x00" in b[0].text


# --- the model ----------------------------------------------------------------------------------


def _update(seq: int, tiles: dict[int, bytes], *, valid: set[int] | None = None, status: int = w.ST_FMT_OK,
            owner: int = 0, mode: int | None = 0x00090520, regs: bytes | None = None, key: bool = False) -> w.DisplayUpdate:
    recs = [w.record(t, *w.encode_tile(px)) for t, px in sorted(tiles.items())]
    flags = (w.S_KEY_BITS if key else 0) | w.S_SNAP_LAST
    body = w.update_body(seq, seq * 10, seq, 0, status | flags, owner,
                         w.valid_bytes(set(range(300)) if valid is None else valid), recs,
                         regs=regs if key else None, mode=mode)
    return w.parse_update(body)


def fill(c: int) -> bytes:
    return c.to_bytes(2, "little") * 256


def test_commit_key_then_delta() -> None:
    f = D.DisplayFrame()
    key = _update(1, {t: fill(t) for t in range(300)}, key=True, regs=baseline_regs())
    f.commit([key], D.decode_parts([key]), key=True)
    assert f.seq == 1 and f.hatched() == 0 and f.regs == baseline_regs()
    delta = _update(2, {5: fill(0xABCD)}, mode=0x00090560)
    f.commit([delta], D.decode_parts([delta]), key=False)
    assert f.tile_px(5) == fill(0xABCD) and f.tile_px(6) == fill(6)
    assert f.mode_regs().r16 == 0x60
    assert f.regs_for_viewer()[0x16] == 0x60 and f.regs_for_viewer()[0x1F] == baseline_regs()[0x1F]
    assert f.status & w.S_FRAMING == 0


def test_a_keyframe_drops_tiles_it_does_not_carry() -> None:
    f = D.DisplayFrame()
    full = _update(1, {t: fill(1) for t in range(300)}, key=True, regs=bytes(256))
    f.commit([full], D.decode_parts([full]), key=True)
    half = _update(2, {t: fill(2) for t in range(150)}, valid=set(range(150)), key=True, regs=bytes(256))
    f.commit([half], D.decode_parts([half]), key=True)
    assert f.hatched() == 150 and f.hatched_tiles() == frozenset(range(150, 300))


def test_viewer_update_round_trips_through_the_browser_reading() -> None:
    model, _grid, regs = G.harness_pictures()["banner"]
    f = through_the_wire(model, regs)
    for key in (True, False):
        m = f.viewer_update(range(300), key=key)
        u = w.parse_update(m[8:])
        assert u.snap_last and u.key == key and (u.regs is not None) == key
        out = bytearray(w.FRAME_BYTES)
        for rec in u.tiles:
            w.put_tile_in_frame(out, rec.idx, w.decode_tile(rec.enc, rec.payload))
        assert bytes(out) == model


def test_rows_and_tiles_invert() -> None:
    frame = os.urandom(w.FRAME_BYTES)
    assert D.tiles_to_rows(D.rows_to_tiles(frame)) == frame


# --- badges (§7.4) --------------------------------------------------------------------------------

OK = w.ST_RST_N | w.ST_BL | w.ST_DISPLAY_ON | w.ST_FMT_OK


def keys(status: int, owner: int = 0, mode: str = "hw", regs: D.PanelRegs | None = None, dims=None) -> list[str]:
    return [b.key for b in D.badges(w.StatusFlags(status), owner, mode, regs or D.PanelRegs(), dims=dims)]


def test_badges_table() -> None:
    assert keys(OK | w.S_EXACT) == []
    assert keys(OK | w.S_EXACT, owner=w.OWNER_DUT) == ["held"]
    assert keys(OK | w.S_TEXT_ONLY, owner=w.OWNER_DUT, mode="sw") == ["blind"]
    assert keys(OK | w.S_BLIND, owner=w.OWNER_UNKNOWN, mode="sw") == ["blind"]
    assert keys(OK | w.ST_VIOL) == ["viol"]
    assert keys(OK | w.ST_APPROX) == ["approx"]
    assert keys(OK & ~w.ST_FMT_OK) == ["fmt"]
    assert keys(OK & ~w.ST_BL) == ["backlight_off"]
    assert keys((OK & ~w.ST_DISPLAY_ON) | w.ST_STANDBY) == ["display_off", "standby"]
    assert keys(OK, regs=D.PanelRegs(r36=0x08)) == ["inexact"]
    assert keys(OK, regs=D.PanelRegs(r01=0x01, scroll=True)) == ["inexact"]


def test_badge_texts_are_the_designs() -> None:
    b = D.badges(w.StatusFlags(OK | w.S_TEXT_ONLY), w.OWNER_DUT, "sw", D.PanelRegs())
    assert b[0].to_json() == {"key": "blind", "level": "grey", "text": D.BLIND_TEXT}
    assert "mint 4" in b[0].text
    b = D.badges(w.StatusFlags(OK), 0, "hw", D.PanelRegs(r16=0x60, r36=0x08, scroll=True))
    assert b[0].text == "not mirrored exactly (R16=0x60, R36=0x08, scroll/partial set)"


def test_scroll_registers_from_regs() -> None:
    regs = bytearray(baseline_regs())
    assert D.PanelRegs.of(w.mode_regs(w.mode_word(regs)), bytes(regs)).scroll is False
    regs[0x0E] = 1
    assert D.PanelRegs.of(w.mode_regs(w.mode_word(regs)), bytes(regs)).off_anchor() == ["scroll/partial set"]


def test_dim_states_must_persist_one_second() -> None:
    deb = D.DimDebounce(1.0)
    off = w.StatusFlags(OK & ~w.ST_DISPLAY_ON)
    on = w.StatusFlags(OK)
    assert deb.update(off, 10.0) == frozenset()
    assert deb.update(on, 10.3) == frozenset()                # clcd_demo's re-init: a flicker
    assert deb.update(off, 10.6) == frozenset()
    assert deb.pending(10.7)
    assert deb.update(off, 11.5) == frozenset()
    assert deb.update(off, 11.6) == frozenset({"display_off"})
    assert keys(off.word, dims=deb.confirmed(11.6)) == ["display_off"]
    assert keys(off.word, dims=frozenset()) == []


# --- the still picture ----------------------------------------------------------------------------


def png_rgb(png: bytes) -> tuple[int, int, bytes]:
    i, idat, size = 8, b"", (0, 0)
    while i < len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        tag = png[i + 4:i + 8]
        if tag == b"IHDR":
            size = struct.unpack(">II", png[i + 8:i + 16])
        if tag == b"IDAT":
            idat += png[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    wd, ht = size
    stride = 1 + wd * 3
    return wd, ht, b"".join(raw[y * stride + 1:(y + 1) * stride] for y in range(ht))


def test_png_is_the_picture_with_bit_replication() -> None:
    card = G.card_picture(0x00FF)
    pic = through_the_wire(card, baseline_regs()).picture()
    wd, ht, rgb = png_rgb(pic.png())
    assert (wd, ht) == (320, 240)
    vals = struct.unpack(f"<{W * H}H", card)
    assert rgb == b"".join(bytes(D.rgb888(v)) for v in vals)
    assert D.rgb888(0xFFFF) == (255, 255, 255) and D.rgb888(0x4208) == (66, 65, 66)
    wd2, ht2, rgb2 = png_rgb(pic.png(scale=2))
    assert (wd2, ht2) == (640, 480) and rgb2[:6] == rgb[:3] * 2


def test_png_hatch_marks_only_hatched_tiles() -> None:
    card = G.card_picture(0)
    f = through_the_wire(card, baseline_regs(), valid=set(range(1, 300)))
    pic = f.picture()
    assert pic.hatched == frozenset({0})
    _w, _h, plain = png_rgb(pic.png())
    _w, _h, hatched = png_rgb(pic.png(hatch=True))
    differ = {((i // 3) % W // 16) + ((i // 3) // W // 16) * 20 for i in range(0, len(plain), 3)
              if plain[i:i + 3] != hatched[i:i + 3]}
    assert differ == {0}


# --- decode performance (lenient bounds; the numbers are reported by the lane) -------------------


def _best_ms(fn, n: int = 5) -> float:
    best = 1e9
    for _ in range(n):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best * 1e3


@pytest.mark.parametrize("what", ["card", "noise"])
def test_decode_keeps_up_with_12_fps_full_frames(what: str) -> None:
    """clcd_demo repaints the whole card at up to ~12 fps; the worst case is 12 fps of fresh
    noise (every tile RAW). One full-frame UPDATE must parse, decode and commit in well
    under 1/12 s on one core (the measured figure is a few ms)."""
    frame = G.card_picture(0x1234) if what == "card" else os.urandom(w.FRAME_BYTES)
    recs = w.encode_records(frame, range(300))
    body = w.update_body(1, 0, 0, 0, w.ST_FMT_OK, 0, w.ALL_VALID, recs, mode=0)

    def one() -> None:
        u = w.parse_update(body)
        f = D.DisplayFrame()
        f.commit([u], D.decode_parts([u]), key=False)

    assert _best_ms(one) < 1000 / 12 / 2


# --- HM's end of one connection ----------------------------------------------------------------


def test_display_stream_over_a_socket_pair() -> None:
    import socket

    a, b = socket.socketpair()
    ds = D.DisplayStream(a)
    assert ds.read(0.01) == []                                   # a timeout is not an error
    ds.key()
    ds.ack(7)
    assert b.recv(64) == w.key_msg() + w.ack_msg(7)
    b.sendall(w.pong_msg(3) + b'{"ok":false,"err":"lcd_mirror: bu')
    assert ds.read(1.0) == [w.Pong(3)]
    b.close()
    (ref,) = ds.read(1.0)                                        # the close cut the line short
    assert isinstance(ref, w.Refusal) and ref.err.startswith('{"ok":false')
    with pytest.raises(EOFError):
        ds.read(1.0)
    ds.close()
    ds.close()                                                   # idempotent
    with pytest.raises(EOFError):
        ds.key()


def test_a_source_is_an_adapter_or_a_callable() -> None:
    marker = object()
    src = D.as_display_source(lambda: marker)
    assert src.display_reason() == "" and src.display_connect() is marker
    adapter = type("A", (), {"display_reason": lambda self: "", "display_connect": lambda self: 1})()
    assert D.as_display_source(adapter) is adapter and isinstance(adapter, D.DisplayAdapter)
    with pytest.raises(TypeError):
        D.as_display_source(42)
    e = D.DisplayUnavailable("no lcd_mirror service", retry_s=None)
    assert e.capability == D.DISPLAY_MIRROR and e.retry_s is None and "no lcd_mirror service" in str(e)
