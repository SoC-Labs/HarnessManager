"""Lane WINDOWS: the card writer on Windows, board-free. Discovery parses a FIXTURE of what
PowerShell answers (tests/fakes/win_disks.py); ``guard`` fails any test that runs a real
command or opens a device; a ``card`` write on Windows must end ``needs_privilege`` without
opening anything. Each behaviour has its negative twin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import RefusedError, UnavailableError
from harness_manager.services import cardwriter as cw
from tests.fakes import win_disks
from tests.fakes.cardwriter_fakes import MCC, card_image, guard  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures("guard")


class WinRig:
    """A Windows CardWriter: the fixture's disks, the real ``RealAccess`` (win32: it never
    opens a raw disk), the config card E:\\ standing in for a temp dir."""

    def __init__(self, tmp: Path, doc: dict | None = None) -> None:
        self.tmp = tmp
        self.root = tmp / "E-drive"
        (self.root / "MB" / "HBI0309C").mkdir(parents=True)
        (self.root / "config.txt").write_text("TITLE: config\n", encoding="utf-8")
        (self.root / "MB" / "HBI0309C" / "images.txt").write_text("old\n", encoding="utf-8")
        self.doc = doc or win_disks.laptop()
        self.roots: list[str] = []
        self.writer = cw.CardWriter(state_dir=tmp / "state", lister=self.lister,
                                    storage_for=self.storage_for, platform="win32",
                                    enabled=lambda: True)

    def lister(self) -> list[cw.Disk]:
        return cw.parse_windows_disks(self.doc)

    def storage_for(self, root: str) -> Any:
        from harness_manager_mps3.sd import Mps3Storage, SdEnv, VolumeInfo

        self.roots.append(root)
        if root != "E:\\":                 # F:\ is a blank card labelled NO NAME
            blank = self.tmp / "F-drive"
            blank.mkdir(exist_ok=True)
            env = SdEnv(list_volumes=lambda: [VolumeInfo("NO NAME", str(blank))])
            return Mps3Storage(str(blank), env=env)
        env = SdEnv(list_volumes=lambda: [VolumeInfo(MCC, str(self.root))])
        return Mps3Storage(str(self.root), env=env)

    def card(self, name: str) -> cw.CardDevice:
        return next(c for c in self.writer.listing().devices if c.disk.name == name)


@pytest.fixture
def rig(tmp_path: Path) -> WinRig:
    return WinRig(tmp_path)


def test_only_the_laptops_card_readers_are_listed_and_the_rest_say_why(rig: WinRig):
    listing = rig.writer.listing()
    listed = {c.disk.name for c in listing.devices}
    why = {e.name: e.why for e in listing.excluded}
    assert listed == {"PhysicalDrive1", "PhysicalDrive2", "PhysicalDrive6"}
    assert why["PhysicalDrive0"] == "holds Windows (this PC's system or boot disk)"
    assert "MCC drive" in why["PhysicalDrive3"] and "never raw" in why["PhysicalDrive3"]
    assert "DAPLink" in why["PhysicalDrive4"]
    assert why["PhysicalDrive5"] == "not removable (a fixed disk)"     # "External hard disk"
    assert "loop device" in why["PhysicalDrive7"]                      # a VHD
    assert why["PhysicalDrive8"] == "no card in this reader"


def test_twin_a_disk_with_the_system_drive_letter_is_excluded_without_the_flags(tmp_path):
    doc = win_disks.laptop()
    doc["disks"][0].update(IsSystem=False, IsBoot=False, BusType="USB")
    doc["drives"][0]["MediaType"] = "Removable Media"
    rig = WinRig(tmp_path, doc)
    why = {e.name: e.why for e in rig.writer.listing().excluded}
    assert why["PhysicalDrive0"] == "holds Windows (this PC's system or boot disk)"   # C:


def test_the_windows_fields_become_the_listing(rig: WinRig):
    card = rig.card("PhysicalDrive1")
    d = card.disk
    assert d.path == "\\\\.\\PhysicalDrive1" and d.number == 1 and d.platform == "win32"
    assert d.transport == "usb" and d.removable and d.mountpoints == ("E:\\",)
    assert d.volumes[0].fstype == "vfat" and d.labels == ("V2M-MPS3",)
    assert card.confirm == "WRITE Generic- SD/MMC USB Device 31.9 GB"
    js = card.to_json()
    assert js["needs_privilege"] is True and js["files_root"] == "E:\\"
    assert js["kinds"]["files"]["ok"] and not js["kinds"]["card"]["ok"]   # the config card
    blank = rig.card("PhysicalDrive2")
    assert blank.kinds["card"] == "" and blank.disk.transport == "usb"   # "7" = USB
    builtin = rig.card("PhysicalDrive6")
    assert builtin.disk.transport == "sd" and builtin.disk.mountpoints == ()  # "12" = SD


def test_twin_a_fat_volume_with_no_drive_letter_says_disk_management(rig: WinRig):
    why = rig.card("PhysicalDrive6").kinds["files"]
    assert why.startswith("its FAT volume has no drive letter: give it one in Disk Management")
    assert "udisksctl" not in why


def test_files_onto_the_drive_letter_backs_up_then_writes_with_no_admin(rig: WinRig, tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "MB" / "HBI0309C").mkdir(parents=True)
    (bundle / "config.txt").write_text("TITLE: new\n", encoding="utf-8")
    (bundle / "MB" / "HBI0309C" / "images.txt").write_text("new\n", encoding="utf-8")
    card = rig.card("PhysicalDrive1")
    info = rig.writer.unsigned_of(bundle)
    plan = rig.writer.prepare(card.id, "files", bundle, card.confirm,
                              backup_dir=tmp_path / "backups",
                              confirm_unsigned=info.phrase)
    out = rig.writer.run(plan)
    assert out["outcome"] == "written" and out["verified"] is True
    assert out["backup"]["taken"] is True and Path(out["backup"]["path"]).is_file()
    assert (rig.root / "config.txt").read_text() == "TITLE: new\n"
    assert set(rig.roots) == {"E:\\", "F:\\"}          # drive letters only, nothing raw
    assert "not the MPS3 configuration SD" in rig.card("PhysicalDrive2").kinds["files"]


def test_twin_files_without_the_unsigned_phrase_writes_nothing(rig: WinRig, tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "MB").mkdir(parents=True)
    (bundle / "config.txt").write_text("TITLE: new\n", encoding="utf-8")
    card = rig.card("PhysicalDrive1")
    with pytest.raises(RefusedError, match="INSTALL UNSIGNED"):
        rig.writer.prepare(card.id, "files", bundle, card.confirm,
                           backup_dir=tmp_path / "backups", confirm_unsigned=None)
    assert (rig.root / "config.txt").read_text() == "TITLE: config\n"


def test_a_card_image_on_windows_ends_needs_privilege_with_admin_steps(rig: WinRig, tmp_path):
    img = card_image(tmp_path / "card.img")
    card = rig.card("PhysicalDrive2")
    info = rig.writer.unsigned_of(img)
    plan = rig.writer.prepare(card.id, "card", img, card.confirm,
                              confirm_unsigned=info.phrase)
    out = rig.writer.run(plan)                  # guard: nothing opened, nothing run
    assert out["outcome"] == "needs_privilege" and out["verified"] is False
    assert out["privileged_shell"] == "powershell_admin" and out["disk_number"] == 2
    steps = out["privileged_steps"]
    assert steps[0].startswith("$d = Get-Disk -Number 2; if ($d.Size -ne 15931539456 -or "
                               "$d.IsSystem -or $d.IsBoot) { throw 'Disk 2 is not the card")
    assert steps[1] == ("Set-Content -Path \"$env:TEMP\\hm-clean-disk2.txt\" -Value "
                        "'select disk 2', 'clean'; diskpart /s \"$env:TEMP\\hm-clean-disk2.txt\"")
    assert "[HmRawDisk]::CreateFile('\\\\.\\PhysicalDrive2', 3221225472" in steps[3]
    assert f"[IO.File]::OpenRead('{img}')" in steps[3]
    # every sector but the first, then the first: Windows mounts nothing mid-write
    assert steps[3].index("Seek(512, 'Begin')") < steps[3].index("[void]$dst.Seek(0, 'Begin')")
    assert steps[4] == "Update-Disk -Number 2"
    assert out["verify_expect"] == f"prints {out['sha256']}"
    assert f"$left = [long]{out['bytes']}" in out["verify_command"]
    assert out["imager"]["name"] == "Raspberry Pi Imager"
    assert out["imager"]["check_expect"] == f"its Hash is {out['sha256'].upper()}"
    assert "Choose Storage: Generic- MicroSD/M2 USB Device 15.9 GB" in out["imager"]["steps"][3]
    assert "Run as administrator" in out["privileged_how"]


def test_twin_the_config_card_is_never_a_card_image_target_on_windows(rig: WinRig, tmp_path):
    img = card_image(tmp_path / "card.img")
    card = rig.card("PhysicalDrive1")
    with pytest.raises(RefusedError, match="MPS3 configuration SD"):
        rig.writer.plan(card.id, "card", img)


def test_twin_windows_never_opens_a_raw_disk_even_elevated():
    acc = cw.RealAccess(platform="win32")
    assert acc.can_write("\\\\.\\PhysicalDrive2") is False
    with pytest.raises(RefusedError, match="never writes a raw disk on Windows"):
        acc.check_device("\\\\.\\PhysicalDrive2")


def test_a_quote_in_the_image_path_is_doubled_for_powershell():
    d = cw.Disk("PhysicalDrive3", cw.windows_disk_path(3), 100 * 512, platform="win32",
                number=3, model="SD")
    out = cw.privileged_commands(d, "C:\\Users\\Ann O'Neil\\card.img", 512, "ab" * 32)
    assert "[IO.File]::OpenRead('C:\\Users\\Ann O''Neil\\card.img')" in out["privileged_steps"][3]
    assert out["imager"]["check_command"] == \
        "Get-FileHash -Algorithm SHA256 'C:\\Users\\Ann O''Neil\\card.img'"


def test_windows_disks_asks_powershell_read_only(monkeypatch):
    seen: list[list[str]] = []

    def fake(argv, timeout=15.0):  # noqa: ANN001
        seen.append(list(argv))
        return 0, win_disks.answer(), b""

    monkeypatch.setattr(cw, "run_command", fake)
    disks = cw.windows_disks()
    assert len(disks) == 9 and disks[1].labels == ("V2M-MPS3",)
    (argv,) = seen
    assert argv[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-EncodedCommand"]
    from harness_manager.core import winps

    script = winps.script_of(argv)
    for verb in ("Get-Disk", "Get-Partition", "Get-Volume", "Win32_DiskDrive", "ConvertTo-Json"):
        assert verb in script
    for never in ("Set-", "Clear-", "Remove-", "Format-", "New-", "diskpart", "Initialize-"):
        assert never not in script, never


def test_twin_a_failing_powershell_is_a_reason_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "run_command",
                        lambda argv, timeout=15.0: (1, b"", b"Get-Disk : Access denied\r\n"))
    w = cw.CardWriter(state_dir=tmp_path, platform="win32", enabled=lambda: True)
    doc = w.devices_json()
    assert doc["enabled"] is True and doc["supported"] is True and doc["devices"] == []
    assert doc["reason"] == ("PowerShell could not list the disks (Get-Disk): Get-Disk : "
                             "Access denied")
    monkeypatch.setattr(cw, "run_command", lambda argv, timeout=15.0: (0, b"not json", b""))
    with pytest.raises(UnavailableError, match="not JSON"):
        cw.windows_disks()


def test_one_disk_comes_as_an_object_not_a_list():
    doc = win_disks.laptop()
    one = {"system": "C:", "disks": doc["disks"][1], "parts": doc["parts"][2],
           "drives": doc["drives"][1]}
    (disk,) = cw.parse_windows_disks(one)
    assert disk.name == "PhysicalDrive1" and disk.mountpoints == ("E:\\",)


def test_another_os_is_still_refused(tmp_path):
    w = cw.CardWriter(state_dir=tmp_path, platform="sunos5", enabled=lambda: True,
                      lister=lambda: [])
    doc = w.devices_json()
    assert doc["enabled"] is False and doc["supported"] is False
    assert "Linux, macOS and Windows only" in doc["reason"]


def test_the_cli_prints_each_admin_step_the_check_and_the_imager():
    from harness_manager.cli.cmd_flash import privileged_text

    d = cw.Disk("PhysicalDrive2", cw.windows_disk_path(2), 4096, platform="win32", number=2,
                model="SD")
    out = cw.privileged_commands(d, "C:\\img\\card.img", 1024, "cd" * 32)
    lines = privileged_text(out, d.path)
    assert lines[0].startswith("Harness Manager never writes a whole disk on Windows "
                               "(\\\\.\\PhysicalDrive2): open PowerShell as Administrator")
    assert lines[1].startswith("  1. $d = Get-Disk -Number 2") and lines[5] == \
        "  5. Update-Disk -Number 2"
    assert lines[6] == f"then check it (in the same Administrator PowerShell; it prints {'cd' * 32}):"
    assert lines[8] == "Or with Raspberry Pi Imager (https://www.raspberrypi.com/software/):"


def test_twin_the_cli_on_linux_still_prints_one_sudo_command():
    from harness_manager.cli.cmd_flash import privileged_text

    d = cw.Disk("sdc", "/dev/sdc", 4096, platform="linux")
    out = cw.privileged_commands(d, "/tmp/card.img", 1024, "cd" * 32)
    lines = privileged_text(out, d.path)
    assert lines[1] == "    sudo dd if=/tmp/card.img of=/dev/sdc bs=4M conv=fsync status=progress"
    assert not any("PowerShell" in ln for ln in lines)
