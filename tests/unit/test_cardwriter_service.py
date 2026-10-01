"""Lane SD-FLASH: ``services/cardwriter.py``, each behaviour with its negative twin.

Discovery runs on FIXTURES (lsblk and diskutil answers); every "device" is a temp file behind
the ``FileAccess`` seam; ``guard`` fails any test that runs a real listing command or opens a
real device. Nothing here touches a block device.
"""

from __future__ import annotations

import os
import struct
import threading
import zlib
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
    CARD_SIZE,
    MCC,
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
    assert "would erase the board's configuration" in card.kinds["image"]


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
    assert blank["kinds"]["image"]["ok"] and not blank["kinds"]["files"]["ok"]
    assert "not mounted" in blank["kinds"]["files"]["why_not"]


# --- the setting -------------------------------------------------------------------------------


def test_setting_off_lists_nothing_runs_nothing_and_refuses_a_write(rig: Rig):
    rig.enabled = False
    doc = rig.writer.devices_json()
    assert doc["enabled"] is False and doc["devices"] == []
    assert doc["reason"] == "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"
    assert rig.listed == 0
    with pytest.raises(UnavailableError, match="turned off"):
        rig.writer.prepare("sdc-x", "image", rig.image(), "WRITE x")


def test_twin_setting_on_lists(rig: Rig):
    doc = rig.writer.devices_json()
    assert doc["enabled"] is True and len(doc["devices"]) == 3 and rig.listed == 1
    assert "reason" not in doc


def test_the_setting_resolves_from_its_variable(monkeypatch: pytest.MonkeyPatch):
    assert cw.is_enabled() is False and cw.size_cap() == cw.DEFAULT_MAX
    monkeypatch.setenv(cw.ENABLE_ENV, "on")
    monkeypatch.setenv(cw.MAX_ENV, "64G")
    assert cw.is_enabled() is True and cw.size_cap() == 64_000_000_000


def test_windows_is_not_supported_yet(tmp_path: Path):
    w = cw.CardWriter(state_dir=tmp_path, platform="win32", enabled=lambda: True,
                      lister=lambda: [])
    doc = w.devices_json()
    assert doc["enabled"] is False and doc["supported"] is False
    assert "not supported on Windows yet" in doc["reason"]


# --- the typed phrase and the device's identity -----------------------------------------------


def test_a_wrong_phrase_is_refused_and_names_the_right_one(rig: Rig):
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="type exactly 'WRITE MicroSD/M2 15.9 GB'") as err:
        rig.writer.prepare(card.id, "image", rig.image(), "WRITE MicroSD/M2 16 GB")
    assert err.value.data["confirm"] == "WRITE MicroSD/M2 15.9 GB"


def test_twin_the_right_phrase_passes(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "image", rig.image(), "WRITE MicroSD/M2 15.9 GB")
    assert plan.image is not None and plan.image.kind == "slot"


def test_a_card_swapped_between_listing_and_write_is_refused(rig: Rig):
    card = rig.card("sdc")
    rig.node("sdc")["size"] = BLANK_SIZE * 2                       # another card
    with pytest.raises(RefusedError, match="changed since it was listed"):
        rig.writer.prepare(card.id, "image", rig.image(), card.confirm)
    rig.node("sdc")["size"] = BLANK_SIZE
    rig.node("sdc")["model"] = "Other Reader"
    with pytest.raises(RefusedError, match="changed since it was listed"):
        rig.writer.prepare(card.id, "image", rig.image(), card.confirm)


def test_a_same_size_card_swapped_is_refused_by_its_volumes(rig: Rig):
    card = rig.card("sdc")
    rig.node("sdc")["children"][0]["uuid"] = "9999-9999"
    with pytest.raises(RefusedError, match="changed since it was listed"):
        rig.writer.prepare(card.id, "image", rig.image(), card.confirm)


def test_twin_an_unchanged_card_and_a_new_mount_keep_their_id(rig: Rig):
    card = rig.card("sdc")
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    assert rig.writer.find(card.id).id == card.id


def test_a_card_swapped_inside_the_job_is_refused_before_any_byte(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "image", rig.image(), card.confirm)
    rig.node("sdc")["serial"] = "OTHER"
    before = rig.devices["/dev/sdc"].read_bytes()
    with pytest.raises(RefusedError, match="changed"):
        rig.writer.run(plan)
    assert rig.devices["/dev/sdc"].read_bytes() == before


def test_an_unplugged_reader_is_absent(rig: Rig):
    card = rig.card("sdc")
    rig.doc["blockdevices"] = [n for n in rig.doc["blockdevices"] if n["name"] != "sdc"]
    with pytest.raises(AbsentError):
        rig.writer.prepare(card.id, "image", rig.image(), card.confirm)


# --- images ------------------------------------------------------------------------------------


def test_not_an_s0lb_image_is_refused(rig: Rig):
    card = rig.card("sdc")
    junk = rig.image(b"\x7fELF" + b"\0" * 4092, "vmlinux")
    with pytest.raises(RefusedError, match="not a harness image") as err:
        rig.writer.prepare(card.id, "image", junk, card.confirm)
    assert "--any-image" in err.value.hint


def test_twin_any_image_writes_it_raw_from_byte_0(rig: Rig):
    card = rig.card("sdc")
    data = b"\x7fELF" + os.urandom(8188)
    plan = rig.writer.prepare(card.id, "image", rig.image(data, "vmlinux"), card.confirm,
                              any_image=True)
    assert plan.image.kind == "raw"
    out = rig.writer.run(plan)
    assert out["outcome"] == "written" and out["verified"] is True
    assert rig.devices["/dev/sdc"].read_bytes()[:len(data)] == data


def test_a_corrupt_boot_table_is_refused_too(rig: Rig):
    bad = bytearray(slot_image())
    bad[-1] ^= 0xFF                                   # the region's CRC now fails
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="region 0 fails its CRC"):
        rig.writer.prepare(card.id, "image", rig.image(bytes(bad)), card.confirm)


def test_an_image_too_big_for_the_card_is_refused(rig: Rig):
    rig.node("sdc")["size"] = 100 * cw.MIB                # a 100 MB card < the 163 MiB layout
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="the image needs 170.9 MB"):
        rig.writer.prepare(card.id, "image", rig.image(), card.confirm)


def test_twin_a_raw_image_that_fits_is_planned_and_one_byte_more_is_not(rig: Rig):
    rig.node("sdc")["size"] = 8192
    card = rig.card("sdc")
    ok = rig.image(b"x" * 8192, "fits.bin")
    assert rig.writer.prepare(card.id, "image", ok, card.confirm, any_image=True).image
    big = rig.image(b"x" * 8193, "big.bin")
    with pytest.raises(RefusedError, match="the image needs"):
        rig.writer.prepare(card.id, "image", big, card.confirm, any_image=True)


def test_the_mcc_config_card_never_takes_an_image(rig: Rig):
    card = rig.card("sdb")
    with pytest.raises(RefusedError, match="would erase the board's configuration"):
        rig.writer.prepare(card.id, "image", rig.image(), card.confirm, any_image=True)


def test_a_slot_image_is_composed_into_stage0s_card_layout_and_read_back(rig: Rig):
    card = rig.card("sdc")
    img = slot_image()
    plan = rig.writer.prepare(card.id, "image", rig.image(img), card.confirm)
    out = rig.writer.run(plan)
    assert out["outcome"] == "written" and out["verified"] is True
    assert out["bytes"] == cw.CARD_LAYOUT_BYTES == 169_869_312
    dev = rig.devices["/dev/sdc"].read_bytes()
    assert dev[510:512] == b"\x55\xaa"
    entries = [(dev[446 + 16 * i + 4], *struct.unpack_from("<II", dev, 446 + 16 * i + 8))
               for i in range(4)]
    persist = BLANK_SIZE // 512 - cw.LBA_PERSIST
    assert entries == [(0x7F, 67584, 131072), (0x7F, 198656, 131072),
                       (0x83, 329728, persist), (0xDA, 2048, 65536)]
    for lba in (1, 2):                                  # the boot-select sector, twice
        sec = dev[lba * 512:(lba + 1) * 512]
        assert struct.unpack_from("<IIII", sec) == (0x43423053, 1, 1, 1)
        assert struct.unpack_from("<I", sec, 0x1FC)[0] == zlib.crc32(sec[:0x1FC]) & 0xFFFFFFFF
    for lba in (67584, 198656):                         # the image in both slots
        assert dev[lba * 512:lba * 512 + len(img)] == img
    p4, p3 = 2048 * 512, 329728 * 512
    assert dev[p4:p4 + 65536 * 512].count(0) == 65536 * 512        # the store is blank
    assert dev[p3:p3 + cw.MIB].count(0) == cw.MIB                  # /persist's start is blank
    # the card as the writer's own check reads it: a card image, slot A's header CRC
    composed = rig.tmp / "card.img"
    composed.write_bytes(dev[:cw.CARD_LAYOUT_BYTES])
    again = cw.inspect_image(composed)
    assert again.kind == "card" and again.hdr_crc == f"0x{s0lb.parse(img).header_crc32:08x}"


def test_twin_a_card_image_is_written_as_it_is(rig: Rig, tmp_path: Path):
    card = rig.card("sdc")
    src = tmp_path / "card-src.img"
    cw.compose_card(rig.image(), 512 * cw.MIB, src)
    plan = rig.writer.prepare(card.id, "image", src, card.confirm)
    assert plan.image.kind == "card"
    out = rig.writer.run(plan)
    assert out["verified"] and out["bytes"] == src.stat().st_size
    assert rig.devices["/dev/sdc"].read_bytes()[:src.stat().st_size] == src.read_bytes()


def test_a_card_image_with_no_bootable_slot_is_refused(rig: Rig, tmp_path: Path):
    src = tmp_path / "card.img"
    cw.compose_card(rig.image(), 512 * cw.MIB, src)
    with src.open("r+b") as f:                         # break slot A and slot B
        for lba in (cw.LBA_A, cw.LBA_B):
            f.seek(lba * 512)
            f.write(b"\0" * 16)
    card = rig.card("sdc")
    with pytest.raises(RefusedError, match="without a bootable stage0 slot"):
        rig.writer.prepare(card.id, "image", src, card.confirm)


def test_a_corrupted_read_back_fails_the_verify(tmp_path: Path):
    def flip(path: Path) -> None:
        with path.open("r+b") as f:
            f.seek(cw.LBA_A * 512 + 100)
            b = f.read(1)
            f.seek(cw.LBA_A * 512 + 100)
            f.write(bytes([b[0] ^ 0xFF]))

    rig = Rig(tmp_path)
    rig.access.after_write = flip
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "image", rig.image(), card.confirm)
    with pytest.raises(ActionFailedError, match="read-back mismatch on /dev/sdc"):
        rig.writer.run(plan)
    done = rig.topics("cardwriter.done")
    assert done and done[-1]["verified"] is False and done[-1]["outcome"] == "verify_failed"


def test_twin_an_intact_read_back_verifies_and_says_so(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "image", rig.image(), card.confirm)
    out = rig.writer.run(plan)
    done = rig.topics("cardwriter.done")[-1]
    assert done["verified"] is True and done["sha256"] == out["sha256"]
    phases = [d["phase"] for d in rig.topics("cardwriter.progress")]
    assert phases[0] == "unmount" and "write" in phases and phases[-1] == "verify"
    last = rig.topics("cardwriter.progress")[-1]
    assert last["bytes"] == last["total"] == cw.CARD_LAYOUT_BYTES


def test_a_mounted_card_is_unmounted_before_the_write(rig: Rig):
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    card = rig.card("sdc")
    rig.writer.run(rig.writer.prepare(card.id, "image", rig.image(), card.confirm))
    assert rig.access.unmounted == ["/dev/sdc"]


def test_twin_a_card_still_mounted_after_the_unmount_is_refused(rig: Rig):
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    rig.access.on_unmount = None                         # the unmount does nothing
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "image", rig.image(), card.confirm)
    before = rig.devices["/dev/sdc"].read_bytes()
    with pytest.raises(RefusedError, match="still mounted"):
        rig.writer.run(plan)
    assert rig.devices["/dev/sdc"].read_bytes() == before


# --- privileges --------------------------------------------------------------------------------


def test_needs_privilege_ends_the_job_with_the_exact_commands(tmp_path: Path):
    rig = Rig(tmp_path, writable=False)
    rig.node("sdc")["children"][0]["mountpoints"] = ["/media/u/NO NAME"]
    card = rig.card("sdc")
    assert card.needs_privilege is True and card.to_json()["needs_privilege"] is True
    before = rig.devices["/dev/sdc"].read_bytes()
    out = rig.writer.run(rig.writer.prepare(card.id, "image", rig.image(), card.confirm))
    assert out["outcome"] == "needs_privilege" and out["verified"] is False
    img = out["image"]
    assert Path(img).stat().st_size == cw.CARD_LAYOUT_BYTES        # the composed card, kept
    assert out["privileged_command"] == (
        f"sudo umount /dev/sdc1 && sudo dd if={img} of=/dev/sdc bs=4M conv=fsync "
        f"status=progress")
    assert out["verify_command"] == f"sudo cmp -n 169869312 {img} /dev/sdc && echo verified"
    assert cw.file_sha256(Path(img)) == out["sha256"]
    assert rig.devices["/dev/sdc"].read_bytes() == before and rig.access.unmounted == []
    done = rig.topics("cardwriter.done")[-1]
    assert done["outcome"] == "needs_privilege" and done["privileged_command"]


def test_twin_a_writable_device_needs_no_command(rig: Rig):
    card = rig.card("sdc")
    out = rig.writer.run(rig.writer.prepare(card.id, "image", rig.image(), card.confirm))
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
    with pytest.raises(RefusedError, match="is not a block device"):
        access.check_device(str(f))
    with pytest.raises(AbsentError):
        access.check_device(str(tmp_path / "gone"))


def test_twin_only_the_seam_takes_a_file(tmp_path: Path):
    f = tmp_path / "pretend-sdz"
    seam = cw.FileAccess({"/dev/sdz": f})
    seam.check_device("/dev/sdz")
    assert seam.simulated is True and cw.RealAccess().simulated is False
    with pytest.raises(AbsentError):
        seam.check_device(str(f))                      # only the paths it was given


def test_one_write_per_device(rig: Rig):
    card = rig.card("sdc")
    plan = rig.writer.prepare(card.id, "image", rig.image(), card.confirm)
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
        rig.writer.prepare(card.id, "bits", rig.image(), card.confirm)
    with pytest.raises(UsageError):
        rig.writer.prepare("", "image", rig.image(), card.confirm)


# --- --demo ------------------------------------------------------------------------------------


def test_the_demo_lists_simulated_readers_and_writes_only_temp_files(tmp_path: Path):
    w = cw.demo_writer(tmp_path, publish=lambda t, d: None)
    w._enabled = lambda: True
    doc = w.devices_json()
    assert doc["simulated"] is True
    assert [d["path"] for d in doc["devices"]] == ["/dev/sdb", "/dev/sdc"]
    assert {e["path"] for e in doc["excluded"]} == {"/dev/nvme0n1", "/dev/sdd"}
    blank = next(d for d in doc["devices"] if d["path"] == "/dev/sdc")
    img = tmp_path / "linux_slot.img"
    img.write_bytes(slot_image())
    out = w.run(w.prepare(blank["id"], "image", img, blank["confirm"]))
    assert out["verified"] and (tmp_path / "cardwriter-demo" / "sdc.img").stat().st_size >= \
        cw.CARD_LAYOUT_BYTES
