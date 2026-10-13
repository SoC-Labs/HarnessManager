"""L10 SLOT-VERSION: per-slot version/sha from slot status, slot -> release matching, and the
catalogue calling the planner's one copy of the matching and the running summary."""

from __future__ import annotations

from pathlib import Path

from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import SlotInfo
from harness_manager.services.harness_catalog import HarnessCatalog
from harness_manager.services.update.planner import (
    BoardView,
    match_among,
    match_release,
    match_slot,
    running_summary,
)
from harness_manager.services.update.schema import parse_channel
from harness_manager.services.update.version import same_u32
from harness_manager_mps3 import os_slots
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.s0lb_image import header_crc, linux_bundle_s0lb, make_s0lb
from tests.fakes.t7_bundles import FIELDED_STATIC, Release

KEYS = TestKeys()
IMG_A = make_s0lb(b"\x5a" * 4096, b"\xa5" * 1024)
IMG_B = make_s0lb(b"\x11" * 4096, b"\x22" * 1024)


def channel(tmp_path: Path):
    """Two Linux releases on one static: 2.0.0 (sha aaaa1111) and 2.1.0 (sha bbbb2222)."""
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    for ver, sha, img in (("2.0.0", "aaaa1111", IMG_A), ("2.1.0", "bbbb2222", IMG_B)):
        rel = Release(ver, impl="linux", with_sd=False, with_overlays=False, sha=sha)
        comp = b.component("os-slot", "user-usd", AssetFile("linux_slot.img", img),
                           kind="os-slot", door="ethernet",
                           provisioned={"static_id": FIELDED_STATIC}, s0lb=linux_bundle_s0lb(img))
        b.add_harness(ver, rel.identity(), [comp])
    return parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))


def slot(name="A", *, crc="", sha=None, version="", img_sha="") -> SlotInfo:
    return SlotInfo(name, state="valid", hdr_crc=crc, version=version, sha=sha,
                    image_sha256=img_sha)


# --- 1. slot status carries version/sha only when the board says so -----------------------------


def raw(**b):
    return {"ok": True, "card": True, "running": "A", "default": "A", "seq": 1, "target": "B",
            "a": {"state": "valid", "hdr_crc": "0x3e5e9c2c", "len": 10, "verified": "boot"},
            "b": {"state": "valid", "hdr_crc": "0x0a4da20b", "len": 10, **b}}


def test_a_slot_with_version_and_sha_carries_both():
    st = os_slots.parse_status(raw(version="2.1.0-dev", sha="ad297bf9"))
    assert (st.slots["B"].version, st.slots["B"].sha) == ("2.1.0-dev", "ad297bf9")


def test_a_slot_without_them_is_unknown_not_empty_text():
    st = os_slots.parse_status(raw())
    assert st.slots["A"].sha is None and st.slots["A"].version == ""
    st2 = os_slots.parse_status(raw(version="", sha=None))
    assert st2.slots["B"].sha is None and st2.slots["B"].version == ""


def test_the_boards_version_outranks_what_this_host_recorded():
    class Recs:
        def get(self, crc):
            return {"sha256": "ab" * 32, "version": "1.9.9"}

    fake = type("A", (), {"records": Recs()})()
    st = os_slots.parse_status(raw(version="2.1.0-dev", sha="ad297bf9"))
    out = os_slots.Mps3OsSlots._annotate(fake, st)
    assert out.slots["B"].version == "2.1.0-dev" and out.slots["B"].sha == "ad297bf9"
    assert out.slots["B"].image_sha256 == "ab" * 32
    # no board version: the host's record still names it
    out2 = os_slots.Mps3OsSlots._annotate(fake, os_slots.parse_status(raw()))
    assert out2.slots["B"].version == "1.9.9" and out2.slots["B"].sha is None


# --- 2. matching: sha beats hdr_crc; a version string alone never matches ------------------------


def test_sha_names_the_release(tmp_path):
    ch = channel(tmp_path)
    rel, basis = match_slot(ch.harness, slot(sha="bbbb2222"), FIELDED_STATIC)
    assert (rel.version, basis) == ("2.1.0", "sha")


def test_sha_beats_a_hdr_crc_that_points_elsewhere(tmp_path):
    ch = channel(tmp_path)
    rel, basis = match_slot(ch.harness, slot(sha="aaaa1111", crc=header_crc(IMG_B)), FIELDED_STATIC)
    assert (rel.version, basis) == ("2.0.0", "sha")


def test_without_a_sha_the_hdr_crc_names_it(tmp_path):
    ch = channel(tmp_path)
    rel, basis = match_slot(ch.harness, slot(crc=header_crc(IMG_B)), FIELDED_STATIC)
    assert (rel.version, basis) == ("2.1.0", "hdr_crc")


def test_a_sha_no_release_has_falls_back_to_hdr_crc(tmp_path):
    ch = channel(tmp_path)
    rel, basis = match_slot(ch.harness, slot(sha="deadbeef", crc=header_crc(IMG_A)), FIELDED_STATIC)
    assert (rel.version, basis) == ("2.0.0", "hdr_crc")


def test_a_version_string_alone_never_matches(tmp_path):
    ch = channel(tmp_path)
    assert match_slot(ch.harness, slot(version="2.1.0"), FIELDED_STATIC) == (None, "")
    assert match_slot(ch.harness, slot(version="2.1.0", sha="deadbeef", crc="0x1"),
                      FIELDED_STATIC) == (None, "")


def test_another_static_or_an_invalid_slot_is_unknown(tmp_path):
    ch = channel(tmp_path)
    assert match_slot(ch.harness, slot(sha="bbbb2222"), "0x12345678") == (None, "")
    bad = SlotInfo("A", state="empty", sha="bbbb2222")
    assert match_slot(ch.harness, bad, FIELDED_STATIC) == (None, "")


def test_the_catalogue_names_both_slots(tmp_path):
    ch = channel(tmp_path)
    ident = BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_impl="linux")
    view = BoardView(board_id="b", pack="mps3", identity=ident, has_os_slots=True,
                     os_slot_infos={"A": slot("A", crc=header_crc(IMG_A)),
                                    "B": slot("B", sha="bbbb2222"), })
    got = HarnessCatalog.slot_releases(ch.harness, view)
    assert got == {"A": {"release": "2.0.0", "basis": "hdr_crc"},
                   "B": {"release": "2.1.0", "basis": "sha"}}
    assert HarnessCatalog.slot_releases(ch.harness, BoardView(board_id="b", pack="mps3",
                                                              identity=ident)) == {}


# --- 3. the dedupe keeps what both copies said ---------------------------------------------------


IDENT = BoardIdentity(board_type="mps3", shell_id="0X61BC6789", harness_version="2.1.0",
                      firmware_sha="bbbb2222", harness_impl="linux", usercode="0XABCD",
                      ver32="0X00020100", proto="0.10", features=("slot", "icap"))


def test_running_release_is_the_planners_match(tmp_path):
    ch = channel(tmp_path)
    ident = BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_version="2.1.0",
                          firmware_sha="bbbb2222", harness_impl="linux")
    # snapshot of the old catalogue copy: the same answer as match_release, None for no identity
    assert HarnessCatalog.running_release(ch.harness, ident).version == "2.1.0"
    assert match_among(ch.harness, ident).version == match_release(ch, ident).version
    assert HarnessCatalog.running_release(ch.harness, None) is None
    assert HarnessCatalog.running_release(
        ch.harness, BoardIdentity(board_type="mps3", shell_id="")) is None


def test_one_running_summary_with_every_key_either_copy_had():
    got = running_summary(IDENT)
    # old planner keys + old catalogue keys (proto was catalogue-only, features planner-only)
    assert got == {"shell_id": "0x61bc6789", "harness": "2.1.0", "firmware_sha": "bbbb2222",
                   "usercode": "0xabcd", "impl": "linux", "features": ["icap", "slot"],
                   "ver32": "0x00020100", "proto": "0.10"}
    assert running_summary(None) == {}
    assert not hasattr(HarnessCatalog, "_running_summary")


# --- 4. the one guarded same_u32 -------------------------------------------------------------------


def test_same_u32_is_guarded():
    assert same_u32("0x00ff", "0xFF") and not same_u32("0x1", "0x2")
    assert same_u32(None, None) is True      # two blanks: equal text, no exception
    assert same_u32(None, "0x1") is False and same_u32("0x1", None) is False
    assert same_u32("abc", "ABC") and not same_u32("abc", "abd")
    assert os_slots.same_u32(None, "0x1") is False and os_slots.same_u32(None, None) is False
    assert os_slots.same_u32("0x1", 1)


def test_the_slot_tile_names_the_release_and_never_calls_a_valid_image_empty():
    js = (Path(__file__).parents[2] / "src/harness_manager/web/static/js/sections/update.js").read_text()
    assert '"unknown release"' in js and "slotRelease(s)" in js
    assert 'return "empty"' in js
