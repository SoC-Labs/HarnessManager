"""T7: the OS-slot flow (Linux harness, user µSD A/B) through the interface, with a fake.

The mechanism is the board's slot contract (net-protocol v0.14; lane LINUX-SLOTS moved
the seam onto it): push into the free slot, read back, commit it as the default, reboot,
and call it installed only when the board runs the new slot as the default, verified by
its boot, and reports the new harness. ``FakeOsSlots`` models that contract; the MPS3
adapter itself is tested against pyverify's FakeShell (test_lxslots_*).
"""

from __future__ import annotations

import dataclasses

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.core.events import EventBus
from harness_manager.core.model import BoardIdentity
from harness_manager.services.update import RESULT_INSTALLED, RESULT_WRITTEN
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader, sha256_file
from harness_manager.services.update.executor import HarnessInstaller
from harness_manager.services.update.os_slots import SLOT_VALID, VERIFIED_BOOT, SlotInfo
from harness_manager.services.update.planner import BoardView, make_plan
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.s0lb_image import header_crc, make_s0lb
from tests.fakes.t7_board import FakeOsSlots, StubSession
from tests.fakes.t7_bundles import FIELDED_STATIC, Release

KEYS = TestKeys()
IMAGE = make_s0lb()


@pytest.fixture
def world(tmp_path):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    rel = Release("1.2.0", impl="linux", with_sd=False, with_overlays=False)
    comps = [b.component("os-1.2.0", "user-usd", AssetFile("mps3-os-1.2.0.s0", IMAGE),
                         kind="os-slot")]
    b.add_harness(rel.version, rel.identity(), comps)
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
                               "B": SlotInfo("B")})
    session = StubSession(ident, os_slots=slots)

    def booted(info):   # the board comes up running whatever image stage0 booted
        session.ident = dataclasses.replace(session.ident, harness_version=info.version)
    slots.on_boot = booted
    bus = EventBus()
    events = []
    bus.subscribe("update.*", events.append)
    installer = HarnessInstaller(state=state, downloader=dl, store=None, bus=bus)
    return {"verified": verified, "session": session, "slots": slots, "installer": installer,
            "events": events, "state": state}


def plan_for(w):
    s = w["session"]
    view = BoardView(board_id=s.candidate.board_id, pack="mps3", identity=s.ident,
                     has_os_slots=True, os_active_sha=w["slots"].status().active_info.image_sha256)
    return make_plan(w["verified"].channel, view, app_version="0.1.0")


def test_os_update_writes_the_inactive_slot_and_confirms(world):
    w = world
    plan = plan_for(w)
    assert plan.os_slot and not plan.base and plan.reboot_wait_s == 180.0
    out = w["installer"].run(w["session"], plan, plan.approve(), w["verified"])
    assert out.result == RESULT_INSTALLED, out.detail
    slots = w["slots"]
    assert slots.running == "B" and slots.default == "B"
    assert slots.slots["B"].verified == VERIFIED_BOOT
    assert slots.slots["A"].valid                                     # the old slot is kept
    assert [c for c in slots.calls if c != "status"] == ["push:B", "commit:B", "reboot"]
    assert out.os_slot["previous_slot"] == "A" and out.os_slot["hdr_crc"] == header_crc(IMAGE)
    assert [e.topic for e in w["events"]][0] == "update.started"
    assert w["events"][-1].topic == "update.done"


def test_a_new_image_that_does_not_confirm_is_rolled_back_by_stage0(world):
    w = world
    w["slots"].bad_images.add(sha256_file_bytes(IMAGE))
    plan = plan_for(w)
    out = w["installer"].run(w["session"], plan, plan.approve(), w["verified"])
    assert out.result == RESULT_WRITTEN
    assert out.os_slot["rolled_back"] and w["slots"].running == "A"
    assert w["session"].ident.harness_version == "1.1.0"            # still the old image


def test_an_unwitnessed_reboot_is_written_not_running(world):
    w = world
    w["slots"].reboot_fails = True
    plan = plan_for(w)
    out = w["installer"].run(w["session"], plan, plan.approve(), w["verified"])
    assert out.result == RESULT_WRITTEN and "not witnessed" in out.detail


def test_an_image_already_running_is_not_written_again(world):
    w = world
    slots = w["slots"]
    blob = w["verified"].channel.harness_release().components[0].asset.sha256
    slots.slots["A"] = dataclasses.replace(slots.slots["A"], image_sha256=blob)
    plan = plan_for(w)
    assert not plan.os_slot


def test_without_an_approval_nothing_is_written(world):
    w = world
    plan = plan_for(w)
    with pytest.raises(RefusedError, match="not approved"):
        w["installer"].run(w["session"], plan, None, w["verified"])
    assert w["slots"].calls == ["status"]                              # the planner's read only


def test_an_approval_for_another_plan_is_refused(world):
    w = world
    plan = plan_for(w)
    approval = plan.approve()
    plan.components.append("extra")
    with pytest.raises(RefusedError, match="plan changed"):
        w["installer"].run(w["session"], plan, approval, w["verified"])


def sha256_file_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_sha256_file_helper(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(IMAGE)
    assert sha256_file(p) == sha256_file_bytes(IMAGE)


# --- both targets in one release: OS slot + config-SD base, one reboot -----------------------


class _Log(list):
    pass


class StubStorage:
    """Enough of a StorageAdapter to order the calls (T3's real one is tested elsewhere)."""

    def __init__(self, log: _Log, tmp) -> None:
        self.log, self.tmp = log, tmp

    def locate(self) -> str:
        (self.tmp / "sd" / "MB" / "HBI0309C").mkdir(parents=True, exist_ok=True)
        return str(self.tmp / "sd")

    def pending(self):
        return None

    def backup(self, dest_dir, progress=None):
        from harness_manager.core.pack import BackupRecord

        self.log.append("sd:backup")
        return BackupRecord(path=str(dest_dir / "b.zip"), sha256="0" * 64, created_at=0.0,
                            files=1, volume_label="V2M-MPS3")

    def install(self, files, *, backup, progress=None):
        assert backup is not None                            # the gate: never without one
        self.log.append(f"sd:install:{len(files)}")


class StubController:
    def __init__(self, log: _Log, slots: FakeOsSlots) -> None:
        self.log, self.slots = log, slots

    def reboot(self, progress=None, wait_s=120.0):
        self.log.append("mcc:reboot")
        self.slots.reboot()          # the FPGA reloads, stage0 boots the armed slot
        return "REBOOT witnessed: down after 2.0s, up after 40.0s"


def test_a_two_target_release_writes_the_os_slot_then_the_sd_then_reboots_once(tmp_path):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    rel = Release("1.2.0", impl="linux", with_overlays=False)
    comps = rel.components(b, tmp_path / "art")
    comps.append(b.component("os-1.2.0", "user-usd", AssetFile("mps3-os-1.2.0.s0", IMAGE),
                             kind="os-slot"))
    b.add_harness(rel.version, rel.identity(), comps)
    b.publish(serial=1)
    state = UpdateState(tmp_path / "state" / "update")
    dl = Downloader(state.cache)
    verified = ChannelClient(state, dl, KEYS.trust()).fetch("stable",
                                                            str(b.root / "channel" / "stable"))
    log = _Log()
    slots = FakeOsSlots(slots={"A": SlotInfo("A", state=SLOT_VALID, hdr_crc="0xaaaaaaaa",
                                             verified=VERIFIED_BOOT, image_sha256="a" * 64,
                                             version="1.1.0"),
                               "B": SlotInfo("B")})
    # The board runs 1.1.0's firmware (another sha): H2 decides the base by the sha.
    ident = BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_version="1.1.0",
                          harness_impl="linux", features=tuple(rel.features),
                          firmware_sha="0110b0a7")
    session = StubSession(ident, storage=StubStorage(log, tmp_path),
                          controller=None, os_slots=slots)
    session.controller = StubController(log, slots)
    slots.on_boot = lambda info: setattr(session, "ident", dataclasses.replace(
        session.ident, harness_version=info.version, firmware_sha=rel.sha))
    view = BoardView(board_id=session.candidate.board_id, pack="mps3", identity=ident,
                     has_storage=True, has_controller=True, has_os_slots=True,
                     os_active_sha="a" * 64, sd_revisions=("HBI0309C",))
    plan = make_plan(verified.channel, view, app_version="0.1.0")
    assert plan.base and plan.os_slot and plan.reboot_wait_s == 180.0
    out = HarnessInstaller(state=state, downloader=dl, store=None).run(
        session, plan, plan.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    order = [c for c in slots.calls if c != "status"]
    assert order == ["push:B", "commit:B", "reboot"]
    assert log == ["sd:backup", "sd:install:4", "mcc:reboot"]
    assert "slots" not in out.evidence and out.evidence["summary"].startswith("REBOOT witnessed")


def test_an_os_only_journal_is_recovered_without_the_debug_usb(world):
    from harness_manager.services.update.state import InstallRecords, Journal

    w = world
    journal = Journal(w["state"], w["session"].candidate.board_id)
    # A previous run died after writing + booting slot B; the board runs 1.2.0 now.
    journal.write(phase="rebooting", version="1.2.0", static_id=FIELDED_STATIC, base=False,
                  os_slot=True)
    w["session"].ident = dataclasses.replace(w["session"].ident, harness_version="1.2.0")
    w["slots"].active = "B"
    w["slots"].slots["B"] = SlotInfo("B", state=SLOT_VALID, hdr_crc=header_crc(IMAGE),
                                     verified=VERIFIED_BOOT,
                                     image_sha256=sha256_file_bytes(IMAGE), version="1.2.0")
    plan = plan_for(w)
    out = w["installer"].run(w["session"], plan, plan.approve(), w["verified"])
    assert out.result == "up-to-date" and journal.read() is None
    rec = InstallRecords(w["state"]).get(w["session"].candidate.board_id)
    assert rec["detail"].startswith("recovered")


def test_an_os_only_journal_that_did_not_take_is_dropped_and_the_update_runs(world):
    from harness_manager.services.update.state import Journal

    w = world
    Journal(w["state"], w["session"].candidate.board_id).write(
        phase="os-written", version="1.2.0", static_id=FIELDED_STATIC, base=False, os_slot=True)
    plan = plan_for(w)
    out = w["installer"].run(w["session"], plan, plan.approve(), w["verified"])
    assert out.result == RESULT_INSTALLED
    assert any(e.data.get("phase") == "journal-dropped" for e in w["events"])
