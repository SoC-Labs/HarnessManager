"""LINUX-SLOTS on the update side: the OS frame check, the static door rule, rule 1 in the
executor, the schema's os-slot declarations and the release tool's linux_bundle.json v1.

Every case has its negative twin beside it."""

from __future__ import annotations

import dataclasses
import json
import struct
import zlib
from pathlib import Path

import pytest

from harness_manager.core.model import BoardIdentity, Check
from harness_manager.services.harness_catalog import VERDICT_DOOR, VERDICT_FITS, verdict_of
from harness_manager.services.update import RESULT_INSTALLED, s0lb
from harness_manager.services.update.bundle import check_os_component
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.executor import HarnessInstaller
from harness_manager.services.update.os_slots import SLOT_VALID, VERIFIED_BOOT, SlotInfo
from harness_manager.services.update.planner import BoardView, make_plan
from harness_manager.services.update.schema import (
    ChannelFormatError,
    os_header_crc,
    os_provisioned_static,
    parse_channel,
)
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.s0lb_image import header_crc, linux_bundle_s0lb, make_s0lb
from tests.fakes.t7_board import FakeOsSlots, StubSession
from tests.fakes.t7_bundles import FIELDED_STATIC, NEW_STATIC, Release

KEYS = TestKeys()
IMAGE = make_s0lb(b"\x5a" * 4096, b"\xa5" * 1024)


def os_channel(tmp_path, image: bytes = IMAGE, *, static: str = FIELDED_STATIC,
               declare: bool = True, **fields):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    rel = Release("1.2.0", impl="linux", with_sd=False, with_overlays=False)
    extra = {"provisioned": {"static_id": static}}
    if declare:
        extra["s0lb"] = linux_bundle_s0lb(image)
        extra["crc32"] = f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}"
        extra["bytes"] = len(image)
    extra.update(fields)
    comps = [b.component("os-slot", "user-usd", AssetFile("linux_slot.img", image),
                         kind="os-slot", door="ethernet", **extra)]
    b.add_harness(rel.version, rel.identity(), comps)
    return b, rel


def parsed(tmp_path, **kw):
    b, _ = os_channel(tmp_path, **kw)
    ch = parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))
    return ch, ch.harness_release()


def checks(tmp_path, blob: bytes, **kw):
    _, rel = parsed(tmp_path, **kw)
    comp = rel.component("os-slot")
    p = tmp_path / "blob"
    p.write_bytes(blob)
    return {i.name.split(": ", 1)[1]: i for i in check_os_component(comp, p, rel)}


# --- 5. the frame check -------------------------------------------------------------------------


def test_the_frame_check_passes_the_image_the_bundle_declares(tmp_path):
    items = checks(tmp_path, IMAGE)
    assert {k: v.check for k, v in items.items()} == {
        "image hash": Check.OK, "stage0 frames": Check.OK, "provisioned static": Check.OK}
    assert header_crc(IMAGE) in items["stage0 frames"].detail


def test_twin_a_frame_the_bundle_did_not_declare_is_caught(tmp_path):
    frames = linux_bundle_s0lb(IMAGE)
    frames["regions"][1]["crc32"] = "0xDEADBEEF"           # the bundle said another region 1
    items = checks(tmp_path, IMAGE, s0lb=frames)
    assert items["stage0 frames"].check == Check.MISMATCH
    assert "region 1 crc32" in items["stage0 frames"].detail


def test_twin_a_corrupt_region_is_caught_even_when_the_table_is_fine(tmp_path):
    bad = bytearray(IMAGE)
    bad[-1] ^= 0xFF                                         # the last region's payload
    items = checks(tmp_path, bytes(bad), declare=False)
    assert items["stage0 frames"].check == Check.MISMATCH
    assert "region 1 fails its CRC" in items["stage0 frames"].detail


def test_twin_a_file_that_is_not_s0lb_v2_is_caught(tmp_path):
    v1 = bytearray(IMAGE)
    v1[4:8] = struct.pack("<I", 1)
    items = checks(tmp_path, bytes(v1), declare=False)
    assert items["stage0 frames"].check == Check.MISMATCH and "version 1" in \
        items["stage0 frames"].detail


def test_twin_no_declared_frames_is_unchecked_never_a_pass(tmp_path):
    items = checks(tmp_path, IMAGE, declare=False)
    assert items["stage0 frames"].check == Check.UNCHECKED
    assert "not a pass" in items["stage0 frames"].detail


def test_twin_an_image_provisioned_for_another_static_is_an_identity_mismatch(tmp_path):
    items = checks(tmp_path, IMAGE, static=NEW_STATIC)
    assert items["provisioned static"].check == Check.MISMATCH
    assert items["provisioned static"].identity


def test_twin_a_size_or_crc_the_release_did_not_sign_is_caught(tmp_path):
    items = checks(tmp_path, IMAGE, bytes=len(IMAGE) + 4)
    assert items["image hash"].check == Check.MISMATCH


def test_s0lb_parse_and_compare_are_stages0s_rules():
    t = s0lb.parse(IMAGE)
    assert t.ok and t.num_entries == 2 and f"0x{t.header_crc32:08x}" == header_crc(IMAGE)
    assert s0lb.compare(t, linux_bundle_s0lb(IMAGE)) == []
    with pytest.raises(s0lb.S0lbError, match="no boot table"):
        s0lb.parse(b"\0" * 64)
    torn = bytearray(IMAGE)
    torn[40] ^= 1                                           # an entry byte: the table CRC
    with pytest.raises(s0lb.S0lbError, match="table CRC"):
        s0lb.parse(bytes(torn))


# --- the schema's os-slot declarations ------------------------------------------------------------


def test_the_schema_keeps_the_os_slot_declarations(tmp_path):
    _, rel = parsed(tmp_path)
    comp = rel.component("os-slot")
    assert comp.target == "ethernet" and comp.kind == "os-slot"
    assert os_provisioned_static(comp, rel) == FIELDED_STATIC
    assert os_header_crc(comp) == header_crc(IMAGE)


def test_twin_a_malformed_declaration_refuses_the_channel(tmp_path):
    b, _ = os_channel(tmp_path, provisioned={"static_id": "not-hex"})
    with pytest.raises(ChannelFormatError, match="provisioned.static_id"):
        parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))
    b, _ = os_channel(tmp_path, s0lb={"regions": [{"dst": "0x80000000", "len": -1,
                                                   "crc32": "0x0"}]})
    with pytest.raises(ChannelFormatError):
        parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))


# --- 4. the static door rule: Ethernet only for the running static ---------------------------------


def view(shell: str, *, crc: str = "", storage: bool = False) -> BoardView:
    ident = BoardIdentity(board_type="mps3", shell_id=shell, harness_version="1.1.0",
                          harness_impl="linux")
    return BoardView(board_id="mps3@test", pack="mps3", identity=ident, has_os_slots=True,
                     has_storage=storage, has_controller=storage, os_active_crc=crc)


def test_an_os_image_for_another_static_needs_debug_usb_or_hub(tmp_path):
    ch, rel = parsed(tmp_path)
    plan = make_plan(ch, view(NEW_STATIC), app_version="0.1.0")
    assert plan.os_slot and plan.needs_door and plan.blockers
    assert plan.needs_door[0].startswith("needs Debug USB or hub")
    v = verdict_of(plan, rel, view(NEW_STATIC), app_version="0.1.0")
    assert v["verdict"] == VERDICT_DOOR and v["verdict_text"] == "needs Debug USB or hub"
    assert "debug-usb-or-hub" in v["needs"]


def test_twin_an_os_image_for_the_running_static_goes_over_ethernet(tmp_path):
    ch, rel = parsed(tmp_path)
    plan = make_plan(ch, view(FIELDED_STATIC), app_version="0.1.0")
    assert plan.os_slot and not plan.needs_door and not plan.blockers and not plan.base
    assert verdict_of(plan, rel, view(FIELDED_STATIC), app_version="0.1.0")["verdict"] == \
        VERDICT_FITS


def test_an_image_the_board_already_runs_is_known_by_its_table_crc(tmp_path):
    ch, _ = parsed(tmp_path)
    assert not make_plan(ch, view(FIELDED_STATIC, crc=header_crc(IMAGE)),
                         app_version="0.1.0").os_slot
    assert make_plan(ch, view(FIELDED_STATIC, crc="0x12345678"), app_version="0.1.0").os_slot


# --- rule 1 in the executor --------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    b, rel = os_channel(tmp_path)
    b.publish(serial=1)
    state = UpdateState(tmp_path / "state" / "update")
    dl = Downloader(state.cache)
    verified = ChannelClient(state, dl, KEYS.trust()).fetch("stable",
                                                            str(b.root / "channel" / "stable"))
    ident = BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_version="1.1.0",
                          harness_impl="linux", features=tuple(rel.features))
    slots = FakeOsSlots(slots={"A": SlotInfo("A", state=SLOT_VALID, hdr_crc="0xaaaaaaaa",
                                             verified=VERIFIED_BOOT, image_sha256="a" * 64,
                                             version="1.1.0"),
                               "B": SlotInfo("B", state=SLOT_VALID, hdr_crc="0xbbbbbbbb",
                                             verified="readback", version="1.1.9")})
    session = StubSession(ident, os_slots=slots)
    slots.on_boot = lambda info: setattr(session, "ident", dataclasses.replace(
        session.ident, harness_version=info.version))
    return verified, session, slots, HarnessInstaller(state=state, downloader=dl, store=None)


def plan_of(verified, session, slots):
    st = slots.status()
    return make_plan(verified.channel, BoardView(
        board_id=session.candidate.board_id, pack="mps3", identity=session.ident,
        has_os_slots=True, os_active_sha=st.active_info.image_sha256,
        os_active_crc=st.active_info.hdr_crc, os_pending=st.pending_commit), app_version="0.1.0")


def test_rule1_the_executor_rolls_a_pending_commit_back_before_pushing(world):
    verified, session, slots, installer = world
    slots.default = "B"                                   # someone committed B, no reboot yet
    plan = plan_of(verified, session, slots)
    assert "rollback-os-slot" in [s.action for s in plan.steps]
    out = installer.run(session, plan, plan.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert [c for c in slots.calls if c != "status"] == ["rollback:A", "push:B", "commit:B",
                                                         "reboot"]
    assert out.os_slot["rolled_back_first"] == "B"


def test_twin_rule1_without_a_pending_commit_nothing_is_rolled_back(world):
    verified, session, slots, installer = world
    plan = plan_of(verified, session, slots)
    assert "rollback-os-slot" not in [s.action for s in plan.steps]
    out = installer.run(session, plan, plan.approve(), verified)
    assert out.result == RESULT_INSTALLED
    assert [c for c in slots.calls if c != "status"] == ["push:B", "commit:B", "reboot"]
    assert out.os_slot["rolled_back_first"] == ""


def test_twin_a_healthy_but_wrong_image_is_left_for_a_person_to_roll_back(world):
    verified, session, slots, installer = world
    slots.on_boot = None                                  # the board keeps reporting 1.1.0
    plan = plan_of(verified, session, slots)
    out = installer.run(session, plan, plan.approve(), verified)
    assert out.result == "written-not-running" and slots.running == "B"
    assert "slot rollback" in out.restore_hint


# --- the release tool reads linux_bundle.json as linux_bundle.py writes it -----------------------------


@pytest.fixture
def release_key(tmp_path) -> Path:
    from tools.release import signer as signer_mod

    return signer_mod.keygen_throwaway(tmp_path / "keys")[0]


def test_the_release_tool_takes_schema_version_1_as_a_string_and_copies_the_frames(
        tmp_path, release_key):
    from tests.fakes.otar_release import write_bundle
    from tests.spikes.harness_dist_spike import linux_mint
    from tests.unit.test_otar_harness import CAT, harness
    from tools.release.common import Layout

    lnx = linux_mint(tmp_path / "m")
    si = {"s0lb": linux_bundle_s0lb(lnx.os_image)}
    bundle = write_bundle(tmp_path / "b", lnx, linux={
        "schema_version": "1", "harness": "2.0.0", "release_notes": "the Linux harness",
        "targets": {"ethernet": {"slot_image": si}}})
    (bundle / "notes.md").unlink()
    rc, text = harness(tmp_path / "dist", bundle, release_key, "2.0.0")
    assert rc == 0, text
    rel = json.loads(Layout(tmp_path / "dist").channel_file(CAT, "beta").read_bytes())[
        "harness"]["releases"][0]
    os_comp = next(c for c in rel["components"] if c["name"] == "os-slot")
    assert os_comp["s0lb"]["header_crc32"] == si["s0lb"]["header_crc32"]
    assert os_comp["provisioned"]["static_id"].lower() == rel["identity"]["static_id"].lower()
    assert os_comp["bytes"] == len(lnx.os_image) and os_comp["target"] == "user-usd"
    assert rel["identity"]["harness"] == "2.0.0" and rel["notes"] == "the Linux harness"
    # and the app reads it back, frames and all
    comp = parse_channel(json.loads(Layout(tmp_path / "dist").channel_file(CAT, "beta")
                                    .read_bytes())).harness_release("2.0.0").component("os-slot")
    assert os_header_crc(comp) == header_crc(lnx.os_image)


def test_twin_the_release_tool_refuses_an_image_whose_frames_are_not_the_bundles(
        tmp_path, release_key):
    from tests.fakes.otar_release import write_bundle
    from tests.spikes.harness_dist_spike import linux_mint
    from tests.unit.test_otar_harness import harness

    lnx = linux_mint(tmp_path / "m")
    frames = linux_bundle_s0lb(lnx.os_image)
    frames["entry_pc"] = "0x80200000"
    bundle = write_bundle(tmp_path / "b", lnx, linux={
        "targets": {"ethernet": {"slot_image": {"s0lb": frames}}}})
    rc, text = harness(tmp_path / "dist", bundle, release_key, "2.0.0")
    assert rc == 14 and "entry_pc is 0x80000000, the bundle declares 0x80200000" in text
    assert not any((tmp_path / "dist").rglob("*.img"))
