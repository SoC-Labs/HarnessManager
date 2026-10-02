"""Lane SD-FLASH: ``services/cardwriter.py``, each behaviour with its negative twin.

Discovery runs on FIXTURES (lsblk and diskutil answers); every "device" is a temp file behind
the ``FileAccess`` seam; ``guard`` fails any test that runs a real listing command or opens a
real device. Nothing here touches a block device.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.services import cardwriter as cw
from harness_manager.services.update import s0lb
from tests.fakes.cardwriter_fakes import (
    BLANK_SIZE,
    BUNDLE_BOARD,
    CARD_BOARD,
    CARD_SIZE,
    MCC,
    NO_LINE_BOARD,
    Rig,
    diskutil_info,
    diskutil_list,
    guard,  # noqa: F401 - the fixture
    lsblk_doc,
    old_lsblk,
    slot_image,
    sysfs,
)

pytestmark = pytest.mark.usefixtures("guard")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def names(listing: cw.Listing) -> tuple[set[str], dict[str, str]]:
    return ({c.disk.name for c in listing.devices}, {e.name: e.why for e in listing.excluded})


# --- discovery ---------------------------------------------------------------------------------


def test_only_card_readers_are_listed_and_every_other_disk_says_why(rig: Rig):
    listed, why = names(rig.writer.listing())
    assert listed == {"sdb", "sdc", "mmcblk0"}
    assert "the system disk" in why["nvme0n1"] and "/boot/efi" in why["nvme0n1"]
    assert why["sda"] == "on sata, not a USB or SD card reader"
    assert "loop device" in why["loop0"] and "zram" in why["zram0"]
    assert "optical" in why["sr0"]
    assert "MCC drive" in why["sdd"] and "never raw" in why["sdd"]
    assert "DAPLink" in why["sde"]
    assert "above the 256.0 GB cap" in why["sdf"]
    assert why["sdg"] == "no card in this reader"
    assert "eMMC (MMC)" in why["mmcblk1"] or "system disk" in why["mmcblk1"]
    assert "eMMC boot" in why["mmcblk1boot0"]


def test_the_system_disk_is_excluded_by_any_system_mount(rig: Rig):
    for mount in ("/", "/boot", "/home", "/usr", "/var", "[SWAP]"):
        rig.node("sdc")["children"][0]["mountpoints"] = [mount]
        _, why = names(rig.writer.listing())
        assert "the system disk" in why["sdc"], mount


def test_twin_a_card_mounted_elsewhere_is_still_listed(rig: Rig):
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    listed, _ = names(rig.writer.listing())
    assert "sdc" in listed


def test_the_boards_mcc_drive_is_excluded_but_its_card_in_a_reader_is_listed_for_files_only(rig):
    listing = rig.writer.listing()
    _, why = names(listing)
    assert "MCC drive" in why["sdd"]
    card = listing.find(rig.card("sdb").id)
    assert card is not None and MCC in card.disk.labels
    assert card.kinds["files"] == ""
    assert "would erase the board's configuration" in card.kinds["card"]


def test_twin_a_disk_with_an_arm_vendor_but_no_mcc_label_is_still_not_a_card(rig: Rig):
    node = rig.node("sdc")
    node["vendor"] = "ARM"
    _, why = names(rig.writer.listing())
    assert "board controller" in why["sdc"]


def test_a_non_removable_disk_is_excluded(rig: Rig):
    node = rig.node("sdc")
    node["rm"] = node["hotplug"] = False
    _, why = names(rig.writer.listing())
    assert why["sdc"] == "not removable (a fixed disk)"


def test_twin_hotplug_alone_makes_a_usb_disk_removable(rig: Rig):
    node = rig.node("sdc")
    node["rm"], node["hotplug"] = False, True
    listed, _ = names(rig.writer.listing())
    assert "sdc" in listed


def test_an_oversize_disk_is_excluded_by_the_cap(rig: Rig):
    rig.cap = 20_000_000_000
    _, why = names(rig.writer.listing())
    assert "31.9 GB is above the 20.0 GB cap (bringup.sd_flash_max)" in why["sdb"]


def test_twin_raising_the_cap_lists_the_big_usb_disk(rig: Rig):
    rig.cap = 3_000_000_000_000
    listed, _ = names(rig.writer.listing())
    assert "sdf" in listed


def test_an_sd_card_in_a_built_in_reader_is_listed_and_an_emmc_is_not(rig: Rig):
    rig.node("mmcblk1")["children"][0]["mountpoints"] = ["/mnt/emmc"]   # not a system mount
    listed, why = names(rig.writer.listing())
    assert "mmcblk0" in listed
    assert why["mmcblk1"] == "an internal eMMC (MMC), not an SD card"


def test_old_util_linux_answers_parse_the_same(tmp_path: Path):
    new = Rig(tmp_path / "a")
    old = Rig(tmp_path / "b", old=True)
    old.doc = new.doc
    a, b = new.writer.listing(), old.writer.listing()
    assert [c.id for c in a.devices] == [c.id for c in b.devices]
    assert names(a)[1] == names(b)[1]


def test_lsblk_without_the_new_columns_retries_with_the_old_ones():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if "MOUNTPOINTS" in argv[-1]:
            return 1, b"", b"lsblk: unknown column: PATH,MOUNTPOINTS"
        import json
        return 0, json.dumps(old_lsblk(lsblk_doc())).encode(), b""

    disks = cw.linux_disks(run=run, sysfs=sysfs)
    assert len(calls) == 2 and "MOUNTPOINT," in calls[1][-1]
    assert {d.name for d in disks} >= {"sdb", "sdc"}


def test_twin_a_failing_lsblk_is_unavailable_with_its_words():
    with pytest.raises(UnavailableError, match="lsblk failed: boom"):
        cw.linux_disks(run=lambda argv: (1, b"", b"boom"))


def test_macos_diskutil_plists_parse_into_card_readers():
    disks = cw.parse_diskutil(diskutil_list(), diskutil_info)
    by = {d.name: d for d in disks}
    sd = by["disk4"]
    assert (sd.path, sd.raw_path, sd.transport, sd.size) == ("/dev/disk4", "/dev/rdisk4", "sd",
                                                             CARD_SIZE)
    assert sd.volumes[0].fstype == "vfat" and sd.volumes[0].label == MCC
    assert sd.volumes[0].mountpoints == ("/Volumes/V2M-MPS3",)
    assert cw.exclusion(sd, cw.DEFAULT_MAX) == ""
    assert cw.exclusion(by["disk6"], cw.DEFAULT_MAX) == ""


def test_twin_macos_apfs_and_internal_disks_are_excluded():
    by = {d.name: d for d in cw.parse_diskutil(diskutil_list(), diskutil_info)}
    assert "APFS" in cw.exclusion(by["disk5"], cw.DEFAULT_MAX)
    internal = cw.Disk("disk0", "/dev/disk0", 500_000_000_000, transport="sd", removable=True,
                       internal=True, platform="darwin")
    assert cw.exclusion(internal, cw.DEFAULT_MAX) == "an internal disk"


def test_a_listed_card_carries_the_contract_fields(rig: Rig):
    doc = rig.card("sdb").to_json()
    assert {"id", "path", "model", "size_bytes", "removable", "mounted", "writable"} <= set(doc)
    assert doc["model"] == "SD/MMC" and doc["size"] == "31.9 GB" and doc["fs"] == "vfat"
    assert doc["confirm"] == "WRITE SD/MMC 31.9 GB"
    assert doc["mounted"] == [str(rig.root)] and doc["removable"] is True
    blank = rig.card("sdc").to_json()
    assert blank["kinds"]["card"]["ok"] and not blank["kinds"]["files"]["ok"]
    assert "not mounted" in blank["kinds"]["files"]["why_not"]


# --- the setting -------------------------------------------------------------------------------


def test_setting_off_lists_nothing_runs_nothing_and_refuses_a_write(rig: Rig):
    rig.enabled = False
    doc = rig.writer.devices_json()
    assert doc["enabled"] is False and doc["devices"] == []
    assert doc["reason"] == "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"
    assert rig.listed == 0
    with pytest.raises(UnavailableError, match="turned off"):
        rig.writer.prepare("sdc-x", "card", rig.card_image(), "WRITE x")


def test_twin_setting_on_lists(rig: Rig):
    doc = rig.writer.devices_json()
    assert doc["enabled"] is True and len(doc["devices"]) == 3 and rig.listed == 1
    assert "reason" not in doc


def test_the_setting_resolves_from_its_variable(monkeypatch: pytest.MonkeyPatch):
    assert cw.is_enabled() is False and cw.size_cap() == cw.DEFAULT_MAX
    monkeypatch.setenv(cw.ENABLE_ENV, "on")
    monkeypatch.setenv(cw.MAX_ENV, "64G")
    assert cw.is_enabled() is True and cw.size_cap() == 64_000_000_000


def test_a_failing_lister_is_a_reason_not_an_error(tmp_path: Path):
    def broken() -> list:
        raise UnavailableError(cw.CAPABILITY, "lsblk failed: no such file")

    w = cw.CardWriter(state_dir=tmp_path, platform="linux", enabled=lambda: True, lister=broken)
    doc = w.devices_json()
    assert doc["enabled"] is True and doc["devices"] == []
    assert doc["reason"] == "lsblk failed: no such file"


def test_windows_is_supported_now(tmp_path: Path):
    # lane WINDOWS: Get-Disk lists the cards (tests/unit/test_windows_cardwriter.py)
    w = cw.CardWriter(state_dir=tmp_path, platform="win32", enabled=lambda: True,
                      lister=lambda: [])
    doc = w.devices_json()
    assert doc["enabled"] is True and doc["supported"] is True and doc["devices"] == []


# --- the typed phrase and the device's identity -----------------------------------------------


def test_a_wrong_phrase_is_refused_and_names_the_right_one(rig: Rig):
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="type exactly 'WRITE MicroSD/M2 15.9 GB'") as err:
        rig.writer.prepare(card.id, "card", rig.card_image(), "WRITE MicroSD/M2 16 GB")
    assert err.value.data["confirm"] == "WRITE MicroSD/M2 15.9 GB"


def test_twin_the_right_phrase_passes(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "card", rig.card_image(), "WRITE MicroSD/M2 15.9 GB")
    assert plan.card is not None and plan.card.slots == ("A", "B")


def test_a_card_swapped_between_listing_and_write_is_refused(rig: Rig):
    card = rig.card("sdc")
    rig.node("sdc")["size"] = BLANK_SIZE * 2                       # another card
    with pytest.raises(RefusedError, match="changed since it was listed"):
        rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)
    rig.node("sdc")["size"] = BLANK_SIZE
    rig.node("sdc")["model"] = "Other Reader"
    with pytest.raises(RefusedError, match="changed since it was listed"):
        rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)


def test_a_same_size_card_swapped_is_refused_by_its_volumes(rig: Rig):
    card = rig.card("sdc")
    rig.node("sdc")["children"][0]["uuid"] = "9999-9999"
    with pytest.raises(RefusedError, match="changed since it was listed"):
        rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)


def test_twin_an_unchanged_card_and_a_new_mount_keep_their_id(rig: Rig):
    card = rig.card("sdc")
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    assert rig.writer.find(card.id).id == card.id


def test_a_card_swapped_inside_the_job_is_refused_before_any_byte(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)
    rig.node("sdc")["serial"] = "OTHER"
    before = rig.devices["/dev/sdc"].read_bytes()
    with pytest.raises(RefusedError, match="changed"):
        rig.writer.run(plan)
    assert rig.devices["/dev/sdc"].read_bytes() == before


def test_an_unplugged_reader_is_absent(rig: Rig):
    card = rig.card("sdc")
    rig.doc["blockdevices"] = [n for n in rig.doc["blockdevices"] if n["name"] != "sdc"]
    with pytest.raises(AbsentError):
        rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)


# --- card images -------------------------------------------------------------------------------


def test_a_single_os_slot_image_is_refused_with_how_to_build_a_card(rig: Rig):
    card = rig.card("sdc")
    with pytest.raises(RefusedError) as err:
        rig.writer.prepare(card.id, "card", rig.slot(), card.confirm)
    assert err.value.message == (
        "linux_slot.img: that is a single OS slot (linux_slot.img), not a whole-card image: "
        "build one with stage0_mkcard.py card --card-img")
    assert "rescue" in err.value.hint


def test_twin_a_card_image_with_an_mbr_is_accepted_written_and_read_back(rig: Rig):
    card = rig.card("sdc")
    src = rig.card_image()
    plan = rig.writer.prepare(card.id, "card", src, card.confirm)
    out = rig.writer.run(plan)
    assert out["outcome"] == "written" and out["verified"] is True
    assert out["bytes"] == src.stat().st_size and out["sha256"] == cw.file_sha256(src)
    assert rig.devices["/dev/sdc"].read_bytes()[:src.stat().st_size] == src.read_bytes()
    assert out["card"]["slots"] == ["A", "B"]


def test_a_file_without_an_mbr_is_refused(rig: Rig):
    card = rig.card("sdc")
    junk = rig.slot(b"\x7fELF" + b"\0" * 4092, "vmlinux")
    with pytest.raises(RefusedError, match=r"vmlinux is not a whole-card image: no MBR \(0x55AA"):
        rig.writer.prepare(card.id, "card", junk, card.confirm)


def test_twin_stage0_mkcards_own_card_layout_is_accepted(rig: Rig):
    src = rig.card_image("canon.img", canonical=True)          # 512 MiB, sparse
    image = cw.inspect_card(src)
    assert image.slots == ("A", "B") and image.size == 512 * cw.MIB
    assert image.hdr_crc == f"0x{s0lb.parse(slot_image()).header_crc32:08x}"


def test_an_mbr_without_a_bootable_slot_is_refused(rig: Rig):
    bad = bytearray(slot_image())
    bad[-1] ^= 0xFF                                     # the region's CRC now fails
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="no bootable stage0 slot.*region 0 fails its CRC"):
        rig.writer.prepare(card.id, "card", rig.card_image(slot=bytes(bad)), card.confirm)
    foreign = rig.tmp / "rpi.img"                       # an MBR, but a FAT partition, no slots
    from tests.fakes.cardwriter_fakes import mbr
    foreign.write_bytes(mbr([(0x0C, 8192, 100000)]) + b"\0" * 4096)
    with pytest.raises(RefusedError, match="not a type-0x7F slot"):
        rig.writer.prepare(card.id, "card", foreign, card.confirm)


def test_twin_one_bootable_slot_is_enough(rig: Rig):
    image = cw.inspect_card(rig.card_image(slot_b=False))
    assert image.slots == ("A",) and "slot B" in image.notes[0]


def test_a_card_image_too_big_for_the_card_is_refused(rig: Rig):
    src = rig.card_image()
    rig.node("sdc")["size"] = src.stat().st_size - 1
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="the card image is .* holds"):
        rig.writer.prepare(card.id, "card", src, card.confirm)


def test_twin_a_card_image_exactly_the_cards_size_fits(rig: Rig):
    src = rig.card_image()
    rig.node("sdc")["size"] = src.stat().st_size
    card = rig.card("sdc")
    assert rig.writer.prepare(card.id, "card", src, card.confirm).card is not None


def test_the_mcc_config_card_never_takes_a_card_image(rig: Rig):
    card = rig.card("sdb")
    with pytest.raises(RefusedError, match="would erase the board's configuration"):
        rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)


def test_a_corrupted_read_back_fails_the_verify(tmp_path: Path):
    def flip(path: Path) -> None:
        with path.open("r+b") as f:
            f.seek(8 * 512 + 100)
            b = f.read(1)
            f.seek(8 * 512 + 100)
            f.write(bytes([b[0] ^ 0xFF]))

    rig = Rig(tmp_path)
    rig.access.after_write = flip
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)
    with pytest.raises(ActionFailedError, match="read-back mismatch on /dev/sdc"):
        rig.writer.run(plan)
    done = rig.topics("cardwriter.done")
    assert done and done[-1]["verified"] is False and done[-1]["outcome"] == "verify_failed"


def test_twin_an_intact_read_back_verifies_and_says_so(rig: Rig):
    card = rig.card("sdc")
    src = rig.card_image()
    out = rig.writer.run(rig.writer.prepare(card.id, "card", src, card.confirm))
    done = rig.topics("cardwriter.done")[-1]
    assert done["verified"] is True and done["sha256"] == out["sha256"]
    phases = [d["phase"] for d in rig.topics("cardwriter.progress")]
    assert phases[0] == "unmount" and "write" in phases and phases[-1] == "verify"
    last = rig.topics("cardwriter.progress")[-1]
    assert last["bytes"] == last["total"] == src.stat().st_size


def test_a_mounted_card_is_unmounted_before_the_write(rig: Rig):
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    card = rig.card("sdc")
    rig.writer.run(rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm))
    assert rig.access.unmounted == ["/dev/sdc"]


def test_twin_a_card_still_mounted_after_the_unmount_is_refused(rig: Rig):
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    rig.access.on_unmount = None                         # the unmount does nothing
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)
    before = rig.devices["/dev/sdc"].read_bytes()
    with pytest.raises(RefusedError, match="still mounted"):
        rig.writer.run(plan)
    assert rig.devices["/dev/sdc"].read_bytes() == before


def test_a_card_image_changed_after_the_check_is_checked_again(rig: Rig):
    card = rig.card("sdc")
    src = rig.card_image()
    plan = rig.writer.prepare(card.id, "card", src, card.confirm)
    src.write_bytes(slot_image())                        # replaced by a slot image meanwhile
    with pytest.raises(RefusedError, match="single OS slot"):
        rig.writer.run(plan)


# --- privileges --------------------------------------------------------------------------------


def test_needs_privilege_ends_the_job_with_the_exact_commands(tmp_path: Path):
    rig = Rig(tmp_path, writable=False)
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    card = rig.card("sdc")
    assert card.needs_privilege is True and card.to_json()["needs_privilege"] is True
    before = rig.devices["/dev/sdc"].read_bytes()
    src = rig.card_image()
    out = rig.writer.run(rig.writer.prepare(card.id, "card", src, card.confirm))
    assert out["outcome"] == "needs_privilege" and out["verified"] is False
    n = src.stat().st_size
    assert out["privileged_command"] == (
        f"sudo umount /dev/sdc1 && sudo dd if={src} of=/dev/sdc bs=4M conv=fsync "
        f"status=progress")
    assert out["verify_command"] == f"sudo cmp -n {n} {src} /dev/sdc && echo verified"
    assert out["sha256"] == cw.file_sha256(src) and out["image"] == str(src)
    assert rig.devices["/dev/sdc"].read_bytes() == before and rig.access.unmounted == []
    done = rig.topics("cardwriter.done")[-1]
    assert done["outcome"] == "needs_privilege" and done["privileged_command"]


def test_twin_a_writable_device_needs_no_command(rig: Rig):
    card = rig.card("sdc")
    out = rig.writer.run(rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm))
    assert out["outcome"] == "written" and "privileged_command" not in out


def test_the_macos_command_is_unmountdisk_and_the_raw_disk():
    disk = cw.Disk("disk4", "/dev/disk4", CARD_SIZE, raw_path="/dev/rdisk4", platform="darwin")
    cmds = cw.privileged_commands(disk, "/tmp/x y.img", 5 * cw.MIB + 1, "ab" * 32)
    assert cmds["privileged_command"] == ("diskutil unmountDisk /dev/disk4 && sudo dd "
                                          "if='/tmp/x y.img' of=/dev/rdisk4 bs=4m && sync")
    assert cmds["verify_command"] == ("sudo dd if=/dev/rdisk4 bs=1m count=6 2>/dev/null | "
                                      "head -c 5242881 | shasum -a 256")
    assert cmds["verify_expect"] == f"prints {'ab' * 32}"


# --- the seam ----------------------------------------------------------------------------------


def test_real_access_refuses_a_regular_file_as_a_device(tmp_path: Path):
    f = tmp_path / "pretend-sdz"
    f.write_bytes(b"\0" * 4096)
    access = cw.RealAccess(platform="linux")
    with pytest.raises(RefusedError, match="is not under /dev"):
        access.check_device(str(f))
    with pytest.raises(RefusedError, match="is not a block device"):
        access.check_device("/dev/null")                 # a character device, not a disk
    with pytest.raises(AbsentError):
        access.check_device("/dev/hm-cardwriter-test-no-such-device")


def test_twin_only_the_seam_takes_a_file(tmp_path: Path):
    f = tmp_path / "pretend-sdz"
    seam = cw.FileAccess({"/dev/sdz": f})
    seam.check_device("/dev/sdz")
    assert seam.simulated is True and cw.RealAccess().simulated is False
    with pytest.raises(AbsentError):
        seam.check_device(str(f))                      # only the paths it was given


def test_one_write_per_device(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "card", rig.card_image(), card.confirm)
    started, release = threading.Event(), threading.Event()

    def slow(path: Path) -> None:
        started.set()
        release.wait(5)

    rig.access.after_write = slow
    t = threading.Thread(target=rig.writer.run, args=(plan,))
    t.start()
    assert started.wait(10)
    try:
        with pytest.raises(HeldError, match="already running"):
            rig.writer.run(plan)
    finally:
        release.set()
        t.join(10)


# --- files: the configuration SD in a reader -----------------------------------------------------


def test_files_without_a_backup_are_refused(rig: Rig):
    card = rig.card("sdb")
    with pytest.raises(RefusedError, match="needs a backup of it first"):
        rig.writer.prepare(card.id, "files", rig.bundle(), card.confirm)


def test_twin_files_back_the_card_up_then_write_and_read_back(rig: Rig):
    card = rig.card("sdb")
    plan = rig.writer.prepare(card.id, "files", rig.bundle(), card.confirm,
                              backup_dir=rig.tmp / "backups")
    out = rig.writer.run(plan)
    assert out["outcome"] == "written" and out["verified"] is True
    assert out["files"] == ["MB/HBI0309C/images.txt", "MB/HBI0309C/shell.bit"]
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "new harness\n"
    assert out["backup"]["taken"] is True and Path(out["backup"]["path"]).is_file()
    phases = [d["phase"] for d in rig.topics("cardwriter.progress")]
    assert phases[0] == "backup" and "write" in phases and "verify" in phases
    # and that backup is a backup of this card as it was: it restores the old file
    storage = rig.storage_for(str(rig.root))
    storage.restore(storage.load_backup(Path(out["backup"]["path"])))
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "old\n"


def test_a_given_backup_of_the_card_as_it_is_now_is_used(rig: Rig):
    storage = rig.storage_for(str(rig.root))
    rec = storage.backup(rig.tmp / "mine")
    card = rig.card("sdb")
    out = rig.writer.run(rig.writer.prepare(card.id, "files", rig.bundle(), card.confirm,
                                            backup_path=rec.path))
    assert out["backup"]["taken"] is False and out["backup"]["path"] == rec.path


def test_twin_a_stale_backup_is_refused_by_the_packs_rules(rig: Rig):
    storage = rig.storage_for(str(rig.root))
    rec = storage.backup(rig.tmp / "mine")
    (rig.root / "config.txt").write_text("changed since the backup\n", encoding="utf-8")
    card = rig.card("sdb")
    plan = rig.writer.prepare(card.id, "files", rig.bundle(), card.confirm, backup_path=rec.path)
    with pytest.raises(RefusedError, match="changed since backup"):
        rig.writer.run(plan)


def test_an_ebf_in_the_bundle_is_refused(rig: Rig):
    card = rig.card("sdb")
    with pytest.raises(RefusedError, match="board-controller firmware.*mbb_v141.ebf"):
        rig.writer.prepare(card.id, "files", rig.bundle(ebf=True), card.confirm,
                           backup_dir=rig.tmp / "backups")
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "old\n"


def test_twin_files_need_a_mounted_config_volume(rig: Rig):
    card = rig.card("sdc")                               # FAT, but not mounted
    with pytest.raises(RefusedError, match="not mounted"):
        rig.writer.prepare(card.id, "files", rig.bundle(), card.confirm,
                           backup_dir=rig.tmp / "backups")
    rig.node("sdc")["children"][0]["mountpoints"] = [str(rig.tmp)]   # mounted, not a config SD
    card = rig.card("sdc")
    assert "neither config.txt nor MB/" in card.kinds["files"] or "not V2M-MPS3" in \
        card.kinds["files"]


def test_bad_arguments_are_usage_errors(rig: Rig):
    card = rig.card("sdc")
    with pytest.raises(UsageError):
        rig.writer.prepare(card.id, "image", rig.card_image(), card.confirm)  # renamed: card
    with pytest.raises(UsageError):
        rig.writer.prepare("", "card", rig.card_image(), card.confirm)


# --- files: the card's MCC firmware selection (MBBIOS) is never changed ---------------------------

def written_board(rig: Rig) -> bytes:
    return (rig.root / "MB" / "HBI0309C" / "board.txt").read_bytes()


def files_write(rig: Rig, bundle: Path, **kw):
    card = rig.card("sdb")
    plan = rig.writer.prepare(card.id, "files", bundle, card.confirm,
                              backup_dir=rig.tmp / "backups", **kw)
    return plan, rig.writer.run(plan)


def test_mbbios_the_cards_line_is_kept_and_the_bundles_never_written(rig: Rig):
    rig.card_board_txt(CARD_BOARD)
    plan, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD))
    got = written_board(rig)
    assert b"MBBIOS: mbb_v141.ebf           ;MB BIOS image \xe2\x80\x94 stock\r\n" in got
    assert b"mbb_v999" not in got
    assert b"APPFILE: Nanosoc\\nanosoc.txt\r\n" in got and b"TITLE: nanoSoC" in got
    assert out["mbbios"] == [{"file": "MB/HBI0309C/board.txt", "action": "kept",
                              "value": "mbb_v141.ebf", "note": "MBBIOS kept: mbb_v141.ebf"}]
    assert plan.mbbios[0]["note"] == "MBBIOS kept: mbb_v141.ebf"


def test_mbbios_the_cards_line_is_kept_when_the_bundle_has_none(rig: Rig):
    rig.card_board_txt(CARD_BOARD)
    _, out = files_write(rig, rig.bundle(board_txt=NO_LINE_BOARD))
    got = written_board(rig)
    assert got.startswith(b"BOARD: HBI0309C\r\n[MCCS]\r\nMBBIOS: mbb_v141.ebf ")
    assert out["mbbios"][0]["action"] == "kept"


def test_mbbios_no_line_on_the_card_and_its_ebf_absent_writes_the_bundles_line_unchanged(rig):
    rig.card_board_txt(NO_LINE_BOARD)
    _, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD))
    assert written_board(rig) == BUNDLE_BOARD                     # never removed
    d = out["mbbios"][0]
    assert d["action"] == "bundle" and d["value"] == "mbb_v999.ebf"
    assert d["note"] == ("MBBIOS: mbb_v999.ebf from the bundle (the card has no mbb_v999.ebf, so "
                         "the MCC will not update)")


def test_mbbios_no_board_txt_on_the_card_counts_as_no_line(rig: Rig):
    rig.card_board_txt(None)
    _, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD))
    assert written_board(rig) == BUNDLE_BOARD and out["mbbios"][0]["action"] == "bundle"


def test_mbbios_no_line_and_the_named_ebf_on_the_card_is_refused(rig: Rig):
    rig.card_board_txt(NO_LINE_BOARD, ebf="MBB_V999.EBF")           # FAT: any case
    card = rig.card("sdb")
    with pytest.raises(RefusedError) as err:
        rig.writer.prepare(card.id, "files", rig.bundle(board_txt=BUNDLE_BOARD), card.confirm,
                           backup_dir=rig.tmp / "backups")
    assert err.value.message == ("this card would make the MCC update itself to mbb_v999.ebf: "
                                 "remove mbb_v999.ebf from the card, or add --allow-mcc-update")
    assert written_board(rig) == NO_LINE_BOARD and not (rig.tmp / "backups").exists()


def test_twin_allow_mcc_update_writes_the_bundles_line_and_says_so(rig: Rig):
    rig.card_board_txt(NO_LINE_BOARD, ebf="mbb_v999.ebf")
    _, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD), allow_mcc_update=True)
    assert written_board(rig) == BUNDLE_BOARD
    d = out["mbbios"][0]
    assert d["action"] == "allowed" and d["note"] == (
        "MBBIOS: mbb_v999.ebf from the bundle, allowed by --allow-mcc-update: the card has "
        "mbb_v999.ebf, so the MCC may update itself to it at its next boot")


def test_mbbios_is_decided_again_inside_the_job(rig: Rig):
    rig.card_board_txt(NO_LINE_BOARD)
    card = rig.card("sdb")
    plan = rig.writer.prepare(card.id, "files", rig.bundle(board_txt=BUNDLE_BOARD), card.confirm,
                              backup_dir=rig.tmp / "backups")
    (rig.root / "MB" / "HBI0309C" / "mbb_v999.ebf").write_bytes(b"fw")   # copied on meanwhile
    with pytest.raises(RefusedError, match="update itself to mbb_v999.ebf"):
        rig.writer.run(plan)
    assert written_board(rig) == NO_LINE_BOARD


# The rule itself is the pack's (harness_manager_mps3.mbbios, the one the hook resolves to):
# these pin the bytes it gives the card writer; tests/unit/test_fp7_mbbios.py has the rest.


def pack_decide():
    from harness_manager_mps3 import mbbios
    return mbbios.decide


def test_mbbios_decide_keeps_every_other_byte_and_line_ending():
    d = pack_decide()(BUNDLE_BOARD, card_board_txt=CARD_BOARD, card_files=[])
    want = BUNDLE_BOARD.replace(b"MBBIOS: mbb_v999.ebf ;the bundle's",
                                b"MBBIOS: mbb_v141.ebf           ;MB BIOS image \xe2\x80\x94 stock")
    assert d.content == want and d.action == "kept" and d.value == "mbb_v141.ebf"


def test_twin_mbbios_with_no_line_anywhere_changes_nothing():
    d = pack_decide()(NO_LINE_BOARD, card_board_txt=None,
                      card_files=["MB/HBI0309C/mbb_v141.ebf"])
    assert d.content == NO_LINE_BOARD and d.action == "none" and d.note == ""


@pytest.mark.parametrize("nl", [b"\n", b"\r\n"])
def test_mbbios_reads_crlf_and_lf_alike(nl: bytes):
    card = nl.join([b"BOARD: X", b"[MCCS]", b"MBBIOS: mbb_v141.ebf ;stock", b""])
    bundle = nl.join([b"BOARD: Y", b"[MCCS]", b"", b"[APP]", b"A: b", b""])
    d = pack_decide()(bundle, card_board_txt=card, card_files=[])
    assert d.action == "kept"
    assert d.content == nl.join([b"BOARD: Y", b"[MCCS]", b"MBBIOS: mbb_v141.ebf ;stock", b"",
                                 b"[APP]", b"A: b", b""])


def test_twin_an_ebf_anywhere_on_the_card_counts_any_case():
    decide = pack_decide()
    with pytest.raises(RefusedError) as err:
        decide(BUNDLE_BOARD, card_board_txt=NO_LINE_BOARD, card_files=["SOFTWARE/MBB_V999.EBF"])
    assert err.value.data == {"mcc_update": {"file": "mbb_v999.ebf", "value": "mbb_v999.ebf"}}
    assert decide(BUNDLE_BOARD, card_board_txt=NO_LINE_BOARD,
                  card_files=["MB/other.ebf"]).action == "bundle"


def test_the_card_writer_has_no_mbbios_rule_of_its_own():
    """The local copy is gone: the rule lives in the pack (one rule, one set of words)."""
    for name in ("decide", "keep_mbbios", "MbbiosDecision", "ALLOW_FLAG", "MBBIOS_DATA_KEY"):
        assert not hasattr(cw, name), name


# --- the pack hook (harness_manager_mps3.mbbios.keep_mbbios) ------------------------------------


def test_the_packs_keep_mbbios_is_used_when_a_pack_has_one(rig: Rig):
    calls = []

    def keep(files, *, card_board_txt, card_files, workdir, allow_mcc_update=False):
        calls.append((card_board_txt, sorted(card_files), allow_mcc_update))
        out = dict(files)
        swapped = Path(workdir) / "board.txt"
        swapped.write_bytes(b"BOARD: from the pack\n")
        out["MB/HBI0309C/board.txt"] = swapped
        from types import SimpleNamespace
        return out, SimpleNamespace(action="kept", value="pack.ebf", note="MBBIOS kept: pack.ebf")

    real = rig.storage_for

    class Plain:                    # a storage that does not apply the rule itself: the hook does
        def __init__(self, root):
            self.inner = real(root)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def install(self, files, *, backup, progress=None):
            return self.inner.install(files, backup=backup, progress=progress)

    rig.writer.storage_for = Plain
    rig.card_board_txt(CARD_BOARD)
    rig.writer.mbbios_for = lambda: keep
    _, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD))
    assert out["mbbios"][0]["note"] == "MBBIOS kept: pack.ebf"
    # integ/bringup: the MPS3 storage re-applies FIX-PACK-7's rule underneath (the card's line
    # goes in), so the hook's board.txt leads and nothing of the bundle's own line survives.
    assert written_board(rig).startswith(b"BOARD: from the pack")
    assert len(calls) == 2 and calls[0][0] == CARD_BOARD                # plan, then the job
    assert "MB/HBI0309C/images.txt" in calls[0][1]


class PlainStorage:
    """A pack's storage that does not apply the MBBIOS rule itself (no ``allow_mcc_update``):
    it records the board.txt the card writer hands it, then writes it."""

    def __init__(self, inner, seen: dict):
        self.inner = inner
        self.seen = seen

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def install(self, files, *, backup, progress=None):
        self.seen["board"] = Path(files["MB/HBI0309C/board.txt"]).read_bytes()
        return self.inner.install(files, backup=backup, progress=progress)


def test_twin_a_pack_without_an_mbbios_rule_writes_the_files_as_given(rig: Rig):
    seen: dict = {}
    real = rig.storage_for
    rig.writer.storage_for = lambda root: PlainStorage(real(root), seen)
    rig.writer.mbbios_for = lambda: None                 # the pack has no mbbios module
    rig.card_board_txt(CARD_BOARD)
    plan, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD))
    assert out["mbbios"] == [] and plan.mbbios == []    # no rule: no decision, no note
    assert seen["board"] == BUNDLE_BOARD                 # handed over byte for byte


def test_with_the_packs_rule_the_storage_is_handed_the_cards_line(rig: Rig):
    seen: dict = {}
    real = rig.storage_for
    rig.writer.storage_for = lambda root: PlainStorage(real(root), seen)
    rig.card_board_txt(CARD_BOARD)                       # mbbios_for: the real pack hook
    _, out = files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD))
    assert out["mbbios"][0]["note"] == "MBBIOS kept: mbb_v141.ebf"
    assert b"MBBIOS: mbb_v141.ebf " in seen["board"] and b"mbb_v999" not in seen["board"]


def test_a_storage_that_applies_the_rule_itself_gets_the_bundle_and_the_flag(rig: Rig):
    seen = {}
    real = rig.storage_for

    class Applies:                                       # Mps3Storage after FIX-PACK-7
        def __init__(self, root):
            self.inner = real(root)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def install(self, files, *, backup, progress=None, allow_mcc_update=False):
            seen["board"] = Path(files["MB/HBI0309C/board.txt"]).read_bytes()
            seen["allow"] = allow_mcc_update
            return self.inner.install(files, backup=backup, progress=progress,
                                      allow_mcc_update=allow_mcc_update)

    rig.writer.storage_for = Applies
    rig.card_board_txt(NO_LINE_BOARD, ebf="mbb_v999.ebf")
    files_write(rig, rig.bundle(board_txt=BUNDLE_BOARD), allow_mcc_update=True)
    assert seen == {"board": BUNDLE_BOARD, "allow": True}


# --- --demo ------------------------------------------------------------------------------------


def test_the_demo_lists_simulated_readers_and_writes_only_temp_files(tmp_path: Path):
    from tests.fakes.cardwriter_fakes import card_image

    w = cw.demo_writer(tmp_path, publish=lambda t, d: None)
    w._enabled = lambda: True
    doc = w.devices_json()
    assert doc["simulated"] is True
    assert [d["path"] for d in doc["devices"]] == ["/dev/sdb", "/dev/sdc"]
    assert {e["path"] for e in doc["excluded"]} == {"/dev/nvme0n1", "/dev/sdd"}
    blank = next(d for d in doc["devices"] if d["path"] == "/dev/sdc")
    src = card_image(tmp_path / "card.img")
    out = w.run(w.prepare(blank["id"], "card", src, blank["confirm"],
                          confirm_unsigned=cw.CardWriter.unsigned_of(src).phrase))
    assert out["verified"]
    assert (tmp_path / "cardwriter-demo" / "sdc.img").read_bytes()[:512] == src.read_bytes()[:512]


def test_integration_the_mps3_pack_supplies_the_mbbios_rule():
    """The pack hook finds the MPS3 pack's rule (with the CRLF and second-line fixes): the
    card writer has none of its own."""
    from harness_manager.services import cardwriter
    from harness_manager_mps3 import mbbios
    assert cardwriter.pack_mbbios() is mbbios.keep_mbbios


# --- the unsigned phrase: every unsigned write (david 2 Oct, "same rule") -------------------------


def test_a_card_image_needs_its_install_unsigned_phrase_before_the_write_phrase(rig: Rig):
    rig.writer.auto_unsigned = False
    card = rig.card("sdc")
    src = rig.card_image()
    sha = cw.file_sha256(src)
    with pytest.raises(RefusedError) as e:
        rig.writer.prepare(card.id, "card", src, "WRITE wrong too")
    assert e.value.message == (f"not confirmed: this card image is unsigned; type exactly "
                               f"'INSTALL UNSIGNED {sha[:8]}' to install it")
    assert e.value.data["unsigned"]["of"] == "file" and e.value.data["unsigned"]["sha256"] == sha
    plan = rig.writer.prepare(card.id, "card", src, card.confirm,
                              confirm_unsigned=f" install unsigned {sha[:8]} ".upper())
    assert plan.unsigned is not None and plan.summary()["unsigned"]["phrase"] == \
        f"INSTALL UNSIGNED {sha[:8]}"
    assert rig.writer.run(plan)["verified"] is True


def test_twin_a_wrong_sha8_is_refused_and_nothing_is_written(rig: Rig):
    card = rig.card("sdc")
    src = rig.card_image()
    right = cw.CardWriter.unsigned_of(src).phrase
    wrong = right[:-8] + ("0" * 8 if not right.endswith("0" * 8) else "1" * 8)
    with pytest.raises(RefusedError, match="does not name this card image"):
        rig.writer.prepare(card.id, "card", src, card.confirm, confirm_unsigned=wrong)
    assert rig.devices["/dev/sdc"].read_bytes() == b"\xee" * 4096


def test_run_never_writes_a_plan_whose_phrase_was_not_typed(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.plan(card.id, "card", rig.card_image())        # plan(): no phrases yet
    rig.writer.check_confirm(plan, card.confirm)
    with pytest.raises(RefusedError, match="this card image is unsigned"):
        rig.writer.run(plan)
    assert rig.devices["/dev/sdc"].read_bytes() == b"\xee" * 4096


def test_a_card_image_changed_after_its_phrase_is_refused_before_the_first_byte(rig: Rig):
    card = rig.card("sdc")
    src = rig.card_image()
    plan = rig.writer.prepare(card.id, "card", src, card.confirm)
    data = bytearray(src.read_bytes())
    data[-1] ^= 0xFF                                       # still a valid card, another sha256
    src.write_bytes(bytes(data))
    with pytest.raises(RefusedError, match="changed since its phrase was typed"):
        rig.writer.run(plan)
    assert rig.devices["/dev/sdc"].read_bytes() == b"\xee" * 4096


def test_a_bundle_changed_after_its_phrase_is_refused_too(rig: Rig):
    card = rig.card("sdb")
    bundle = rig.bundle()
    plan = rig.writer.prepare(card.id, "files", bundle, card.confirm,
                              backup_dir=rig.tmp / "backups")
    (bundle / "MB" / "HBI0309C" / "images.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(RefusedError, match="changed since its phrase was typed"):
        rig.writer.run(plan)
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "old\n"


def test_a_bundle_zip_is_unpacked_and_named_by_the_zips_own_sha256(rig: Rig):
    from tests.unit.test_bringup_service import zip_dir

    card = rig.card("sdb")
    z = zip_dir(rig.bundle(), rig.tmp / "bundle.zip", top="mps3-harness/")
    assert cw.CardWriter.unsigned_of(z).sha256 == cw.file_sha256(z)
    plan = rig.writer.prepare(card.id, "files", z, card.confirm, backup_dir=rig.tmp / "bk")
    assert sorted(plan.files) == ["MB/HBI0309C/images.txt", "MB/HBI0309C/shell.bit"]
    rig.writer.run(plan)
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "new harness\n"
    assert not list(rig.root.rglob("*.complete"))           # the unzip marker never on the card


def test_twin_a_release_bundle_folder_writes_its_sd_tree(rig: Rig):
    card = rig.card("sdb")
    rel = rig.tmp / "rel"
    rig.bundle(name="rel/sd")
    (rel / "mint.json").write_text("{}")
    plan = rig.writer.prepare(card.id, "files", rel, card.confirm, backup_dir=rig.tmp / "bk")
    assert sorted(plan.files) == ["MB/HBI0309C/images.txt", "MB/HBI0309C/shell.bit"]
    assert plan.unsigned is not None and plan.unsigned.of == "manifest" and \
        plan.unsigned.files == 3                            # the whole folder, mint.json too


def test_check_names_the_phrase_and_twin_a_symlinked_folder_is_refused(rig: Rig):
    src = rig.card_image()
    doc = rig.writer.check("card", src)
    assert doc["unsigned"]["phrase"] == cw.CardWriter.unsigned_of(src).phrase
    assert doc["card"]["bytes"] == src.stat().st_size
    bundle = rig.bundle()
    assert rig.writer.check("files", bundle)["count"] == 2
    (bundle / "linked").symlink_to(rig.tmp)
    with pytest.raises(RefusedError, match="linked: a symbolic link"):
        rig.writer.check("files", bundle)
    with pytest.raises(RefusedError, match="single OS slot"):
        rig.writer.check("card", rig.slot())
