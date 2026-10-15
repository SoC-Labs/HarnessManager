"""REKEY-LOCKED: after a re-key upgrade (config SD written + REBOOT) the board runs the NEW
fabric under the OLD Linux image. The base is in place; only the OS image is older.

Every case has its negative twin beside it."""

from __future__ import annotations

import dataclasses
import zlib

import pytest

from harness_manager.core.model import BoardIdentity
from harness_manager.services.harness_catalog import VERDICT_DOOR, VERDICT_FITS, verdict_of
from harness_manager.services.update import RESULT_INSTALLED
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.executor import HarnessInstaller
from harness_manager.services.update.os_slots import SLOT_VALID, VERIFIED_BOOT, SlotInfo
from harness_manager.services.update.planner import BoardView, make_plan
from harness_manager.services.update.schema import parse_channel
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.s0lb_image import linux_bundle_s0lb, make_s0lb
from tests.fakes.t7_board import FakeOsSlots, StubSession
from tests.fakes.t7_bundles import FIELDED_STATIC, NEW_STATIC, USERCODE, Release

KEYS = TestKeys()
IMAGE = make_s0lb(b"\x5a" * 4096, b"\xa5" * 1024)
WARNING = (f"the board already runs harness 2.2.0's fabric (static {NEW_STATIC}); only its OS "
           "image is older: this installs the OS image over Ethernet")


def channel(tmp_path, *, impl="linux", os_slot=True):
    b = ChannelBuilder(tmp_path / "m", KEYS)
    rel = Release("2.2.0", static_id=NEW_STATIC, impl=impl, with_overlays=False)
    comps = rel.components(b, tmp_path)
    if os_slot:
        comps.append(b.component(
            "os-slot", "user-usd", AssetFile("linux_slot.img", IMAGE), kind="os-slot",
            door="ethernet", provisioned={"static_id": NEW_STATIC},
            s0lb=linux_bundle_s0lb(IMAGE), crc32=f"0x{zlib.crc32(IMAGE) & 0xFFFFFFFF:08X}",
            bytes=len(IMAGE)))
    b.add_harness("2.2.0", rel.identity(), comps, rekey=True)
    return b, rel


def parsed(tmp_path, **kw):
    b, _ = channel(tmp_path, **kw)
    return parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))


def locked(shell=NEW_STATIC, usercode=USERCODE, impl="linux") -> BoardIdentity:
    return BoardIdentity(board_type="mps3", shell_id=shell, usercode=usercode,
                         harness_version="2.1.0", harness_impl=impl,
                         firmware_sha="deadbeef", ver32="0x21000000")


def booted(sid=FIELDED_STATIC) -> dict:
    """slot status as board 1 answered it in the locked state (field_b1, 14 Oct): slot A runs
    v2.1's image, whose record binds the OLD static, on the re-keyed fabric."""
    return {"A": SlotInfo("A", state=SLOT_VALID, hdr_crc="0xd8abb834", sid=sid,
                          verified=VERIFIED_BOOT, version="2.1.0"),
            "B": SlotInfo("B", state=SLOT_VALID, hdr_crc="0x314076d0", sid=sid)}


def view(ident, usb=False, running_sid=FIELDED_STATIC) -> BoardView:
    return BoardView(board_id="mps3@t", pack="mps3", identity=ident, has_os_slots=True,
                     has_storage=usb, has_controller=usb, os_slot_infos=booted(running_sid))


def plan(ch, ident, usb=False, running_sid=FIELDED_STATIC):
    v = view(ident, usb, running_sid)
    p = make_plan(ch, v, app_version="1.2.0")
    return p, v


def test_a_locked_board_gets_the_os_image_only_over_ethernet(tmp_path):
    ch = parsed(tmp_path)
    p, v = plan(ch, locked())
    assert (p.base, p.os_slot, p.rekey) == (False, True, False)
    assert p.blockers == [] and p.needs_door == []
    assert WARNING in p.warnings
    assert verdict_of(p, ch.harness_release(), v, app_version="1.2.0")["verdict"] == VERDICT_FITS


def test_a_locked_board_with_debug_usb_does_not_rewrite_the_config_sd(tmp_path):
    p, _ = plan(parsed(tmp_path), locked(), usb=True)
    assert (p.base, p.os_slot, p.rekey) == (False, True, False)
    assert not any(c.startswith("sd-") for c in p.components)


def test_twin_another_usercode_keeps_the_base(tmp_path):
    ch = parsed(tmp_path)
    p, v = plan(ch, locked(usercode="0x12345678"))
    assert p.base is True and WARNING not in p.warnings
    assert any("config SD" in b for b in p.blockers)
    assert verdict_of(p, ch.harness_release(), v, app_version="1.2.0")["verdict"] == VERDICT_DOOR


def test_twin_a_release_without_an_os_slot_is_unchanged(tmp_path):
    p, _ = plan(parsed(tmp_path, os_slot=False), locked())
    assert p.base is True and p.os_slot is False and WARNING not in p.warnings


def test_twin_a_bare_metal_release_is_unchanged(tmp_path):
    p, _ = plan(parsed(tmp_path, impl="bare-metal", os_slot=False), locked(impl="bare-metal"))
    assert p.base is True and WARNING not in p.warnings


def test_twin_the_old_fabric_keeps_the_existing_blocker(tmp_path):
    p, _ = plan(parsed(tmp_path), locked(shell=FIELDED_STATIC, usercode=USERCODE))
    assert p.base is True and WARNING not in p.warnings
    assert p.blockers and any("not built yet" in b for b in p.blockers)


# --- the executor: push, commit, reboot; no storage, no controller ------------------------------------


@pytest.fixture
def world(tmp_path):
    b, rel = channel(tmp_path)
    b.publish(serial=1)
    state = UpdateState(tmp_path / "state" / "update")
    dl = Downloader(state.cache)
    verified = ChannelClient(state, dl, KEYS.trust()).fetch("stable",
                                                            str(b.root / "channel" / "stable"))
    slots = FakeOsSlots(fabric_sid=NEW_STATIC, slots={"A": SlotInfo("A", state=SLOT_VALID, hdr_crc="0xaaaaaaaa",
                                             verified=VERIFIED_BOOT, image_sha256="a" * 64,
                                             version="2.1.0", sid=FIELDED_STATIC),
                               "B": SlotInfo("B", state=SLOT_VALID, hdr_crc="0xbbbbbbbb",
                                             verified="readback", version="2.0.0")})
    session = StubSession(locked(), os_slots=slots)
    slots.on_boot = lambda info: setattr(session, "ident", dataclasses.replace(
        session.ident, harness_version="2.2.0", firmware_sha="c0ffee00", features=tuple(rel.features)))
    return verified, session, slots, HarnessInstaller(state=state, downloader=dl, store=None)


def test_the_os_only_install_pushes_commits_and_reboots_without_storage(world):
    verified, session, slots, installer = world
    st = slots.status()
    p = make_plan(verified.channel, BoardView(
        board_id=session.candidate.board_id, pack="mps3", identity=session.ident,
        has_os_slots=True, os_active_sha=st.active_info.image_sha256,
        os_active_crc=st.active_info.hdr_crc, os_slot_infos=dict(st.slots)), app_version="1.2.0")
    assert (p.base, p.os_slot, p.blockers) == (False, True, [])
    assert p.reboot_wait_s is not None
    out = installer.run(session, p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert [c for c in slots.calls if c != "status"] == ["push:B", "commit:B", "reboot"]


def test_twin_the_same_board_with_a_base_would_need_storage(tmp_path):
    p, _ = plan(parsed(tmp_path), locked(usercode="0x12345678"))
    assert p.base and any("Debug USB" in b for b in p.blockers)


def test_twin_a_running_image_for_the_fabric_s_own_static_still_writes_its_base(tmp_path):
    # The regression (15 Oct gate, test_t7_os_slots): a same-static release whose config SD
    # changed is NOT the locked state; only an image provisioned for ANOTHER static is.
    p, _ = plan(parsed(tmp_path), locked(), usb=True, running_sid=NEW_STATIC)
    assert p.base and p.os_slot and WARNING not in p.warnings


def test_twin_no_slot_record_is_not_taken_as_the_locked_state(tmp_path):
    ch = parsed(tmp_path)
    v = BoardView(board_id="mps3@t", pack="mps3", identity=locked(), has_os_slots=True,
                  has_storage=True, has_controller=True)
    p = make_plan(ch, v, app_version="1.2.0")
    assert p.base and WARNING not in p.warnings
