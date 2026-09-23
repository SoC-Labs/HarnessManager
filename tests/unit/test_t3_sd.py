"""T3: the config-SD storage adapter on FakeSdVolume. Every check has a negative twin.

Board-free: the "SD" is a temp directory laid out like the real one, and volume
discovery runs against fake /dev/disk/by-label, /proc/mounts, sysfs and Win32
views.
"""

from __future__ import annotations

import dataclasses
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from socharness.core.errors import (
    AbsentError,
    ActionFailedError,
    ExitCode,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from socharness.core.model import Candidate, Link, LinkKind
from socharness.core.pack import StorageAdapter
from socharness_board_mps3 import sd as sdmod
from socharness_board_mps3.sd import (
    JOURNAL_NAME,
    Mps3Storage,
    SdEnv,
    VolumeInfo,
    linux_volumes,
    windows_volumes,
)
from tests.fakes.fake_sd import FakeDapLinkVolume, FakeSdVolume
from tests.fakes.t3_usb import FakeWinVolumes

NOW = 1_790_000_000.0
BIT = "MB/HBI0309C/Nanosoc/nanosoc.bit"


def env(volumes: list[VolumeInfo] | None = None, **kw) -> SdEnv:
    base = dict(list_volumes=lambda: list(volumes or []), pid_alive=lambda pid: False,
                hostname=lambda: "testhost", now=lambda: NOW)
    base.update(kw)
    return SdEnv(**base)


@pytest.fixture
def sd(tmp_path: Path) -> FakeSdVolume:
    return FakeSdVolume(tmp_path / "sd")


@pytest.fixture
def storage(sd: FakeSdVolume) -> Mps3Storage:
    return Mps3Storage(str(sd.root), env=env())


def src(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_storage_satisfies_the_protocol(storage):
    assert isinstance(storage, StorageAdapter)


# --- backup -> install -> verify -> restore ---------------------------------------------


def test_round_trip(tmp_path, sd, storage):
    before = sd.snapshot()
    ebf_stat = sd.ebf.stat()
    phases: list[tuple[str, int, int]] = []
    rec = storage.backup(tmp_path / "backups", progress=lambda *a: phases.append(a))
    assert rec.files == len(before) and rec.volume_label == "V2M-MPS3"
    assert Path(rec.path + ".sha256").read_text().split()[0] == rec.sha256
    with zipfile.ZipFile(rec.path) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("MANIFEST.json"))
    assert {"volume/" + p for p in before} <= names
    assert {e["path"]: e["sha256"] for e in manifest["files"]} == before
    assert phases[-1][0] == "backup" and phases[-1][1] == phases[-1][2] > 0

    new_bit = src(tmp_path, "new.bit", b"\x01" * 4096)
    images = src(tmp_path, "images.txt", b"TOTALIMAGES: 0\n")
    phases.clear()
    storage.install({BIT: new_bit, "MB\\HBI0309C\\Nanosoc\\images.txt": images}, backup=rec,
                    progress=lambda *a: phases.append(a))
    assert sd.bit.read_bytes() == b"\x01" * 4096
    assert (sd.root / "MB/HBI0309C/Nanosoc/images.txt").read_bytes() == b"TOTALIMAGES: 0\n"
    assert storage.pending() is None                          # the journal is cleared
    assert storage.last_install.verified
    assert storage.last_install.created == ["MB/HBI0309C/Nanosoc/images.txt"]
    assert {p for p, _, _ in phases} == {"install", "verify"}
    assert phases[-1][1] == phases[-1][2]
    assert sd.ebf.read_bytes() == b"MCCBIOS" and sd.ebf.stat().st_mtime_ns == ebf_stat.st_mtime_ns

    storage.restore(rec)
    assert sd.snapshot() == before
    assert storage.last_restore.written == [BIT]
    assert storage.last_restore.removed == ["MB/HBI0309C/Nanosoc/images.txt"]
    assert storage.pending() is None


def test_install_without_backup_is_refused(tmp_path, sd, storage):
    before = sd.snapshot()
    with pytest.raises(RefusedError, match="backup") as info:
        storage.install({BIT: src(tmp_path, "n.bit", b"x")}, backup=None)
    assert info.value.code == ExitCode.REFUSED
    assert sd.snapshot() == before and not (sd.root / JOURNAL_NAME).exists()


def test_tampered_backup_is_refused(tmp_path, sd, storage):
    rec = storage.backup(tmp_path / "b")
    with open(rec.path, "ab") as f:
        f.write(b"\0")
    before = sd.snapshot()
    with pytest.raises(RefusedError, match="sha256"):
        storage.install({BIT: src(tmp_path, "n.bit", b"x")}, backup=rec)
    assert sd.snapshot() == before


def test_backup_record_that_lies_about_its_contents_is_refused(tmp_path, storage):
    rec = storage.backup(tmp_path / "b")
    with pytest.raises(RefusedError, match="lists"):
        storage.verify_backup(dataclasses.replace(rec, files=rec.files + 1))
    assert storage.verify_backup(rec)["label"] == "V2M-MPS3"          # twin


def test_backup_of_an_older_sd_state_is_refused(tmp_path, sd, storage):
    rec = storage.backup(tmp_path / "b")
    (sd.root / "config.txt").write_text("TITLE: changed behind our back\nUARTMODE: 1\n")
    with pytest.raises(RefusedError, match="changed since backup.*config.txt"):
        storage.install({BIT: src(tmp_path, "n.bit", b"x")}, backup=rec)
    fresh = storage.backup(tmp_path / "b")                           # twin: a fresh backup works
    storage.install({BIT: src(tmp_path, "n.bit", b"x")}, backup=fresh)
    assert sd.bit.read_bytes() == b"x"


# --- rails: .ebf, MCC command files, path escapes, space ---------------------------------


@pytest.mark.parametrize("dest", ["MB/HBI0309C/mbb_v141.ebf", "mb/hbi0309c/MBB_V132.EBF", "x.EBF"])
def test_ebf_is_never_written(tmp_path, sd, storage, dest):
    rec = storage.backup(tmp_path / "b")
    before = sd.snapshot()
    with pytest.raises(RefusedError, match=".ebf"):
        storage.install({dest: src(tmp_path, "fw.bin", b"EVIL")}, backup=rec)
    assert sd.snapshot() == before and not (sd.root / JOURNAL_NAME).exists()


def test_ebf_source_is_never_installed(tmp_path, sd, storage):
    rec = storage.backup(tmp_path / "b")
    with pytest.raises(RefusedError, match="MCC firmware"):
        storage.install({BIT: src(tmp_path, "mbb_v141.ebf", b"EVIL")}, backup=rec)
    storage.install({BIT: src(tmp_path, "ok.bit", b"GOOD")}, backup=rec)       # twin
    assert sd.bit.read_bytes() == b"GOOD"


@pytest.mark.parametrize("name", ["reboot.txt", "RESET.TXT", "shutdown.txt"])
def test_mcc_command_files_are_refused(tmp_path, sd, storage, name):
    rec = storage.backup(tmp_path / "b")
    with pytest.raises(RefusedError, match="MCC command file"):
        storage.install({name: src(tmp_path, "empty", b"")}, backup=rec)
    assert not (sd.root / name).exists()


@pytest.mark.parametrize("dest", ["../escape.txt", "/abs.txt", "C:\\x.txt", "MB/../x.txt", "\\x.txt",
                                  "MB//x.txt", JOURNAL_NAME])
def test_escaping_or_reserved_paths_are_refused(tmp_path, storage, dest):
    rec = storage.backup(tmp_path / "b")
    with pytest.raises(UsageError):
        storage.install({dest: src(tmp_path, "f", b"x")}, backup=rec)


def test_windows_style_and_case_folded_paths_hit_the_same_fat_file(tmp_path, sd, storage):
    rec = storage.backup(tmp_path / "b")
    storage.install({"mb\\hbi0309c\\NANOSOC\\NANOSOC.BIT": src(tmp_path, "n.bit", b"NEW")}, backup=rec)
    assert sd.bit.read_bytes() == b"NEW"
    assert storage.last_install.created == []                      # overwrote, did not add
    assert not any(p.name == "NANOSOC.BIT" for p in sd.root.rglob("*"))


def test_two_keys_for_one_fat_file_are_refused(tmp_path, storage):
    rec = storage.backup(tmp_path / "b")
    with pytest.raises(UsageError, match="same file"):
        storage.install({BIT: src(tmp_path, "a", b"a"), BIT.upper(): src(tmp_path, "b", b"b")},
                        backup=rec)


def test_install_refuses_when_the_sd_is_too_full(tmp_path, sd):
    st = Mps3Storage(str(sd.root), env=env(disk_free=lambda p: 10))
    rec = st.backup(tmp_path / "b")
    with pytest.raises(RefusedError, match="free"):
        st.install({"MB/big.bin": src(tmp_path, "big", b"x" * 100)}, backup=rec)
    assert not (sd.root / "MB/big.bin").exists()


# --- interruption and the journal -------------------------------------------------------


def test_interrupted_install_is_detected_and_restored(tmp_path, sd, storage):
    before = sd.snapshot()
    rec = storage.backup(tmp_path / "b")
    first = src(tmp_path, "n.bit", b"\x02" * 1000)
    second = src(tmp_path, "extra.bin", b"\x03" * 1000)

    def pull_the_cable(phase, done, total):
        if phase == "install" and done > 1000:
            raise RuntimeError("USB disconnected")

    with pytest.raises(ActionFailedError, match="interrupted while writing .*extra.bin"):
        storage.install({BIT: first, "MB/HBI0309C/Nanosoc/extra.bin": second}, backup=rec,
                        progress=pull_the_cable)
    journal = storage.pending()
    assert journal["state"] == "interrupted" and journal["done"] == [BIT]
    assert journal["current"] == "MB/HBI0309C/Nanosoc/extra.bin"
    assert journal["backup"]["path"] == rec.path
    assert sd.bit.read_bytes() == b"\x02" * 1000          # half the install landed
    with pytest.raises(RefusedError, match="interrupted install"):
        storage.install({BIT: first}, backup=rec)       # no second write on top
    with pytest.raises(RefusedError, match="interrupted install"):
        storage.backup(tmp_path / "b")                    # a backup of a half state is not one
    storage.restore(rec)
    assert sd.snapshot() == before and storage.pending() is None


def test_completed_install_leaves_no_journal(tmp_path, sd, storage):
    # Twin of the above.
    rec = storage.backup(tmp_path / "b")
    storage.install({BIT: src(tmp_path, "n.bit", b"ok")}, backup=rec)
    assert storage.pending() is None and not (sd.root / JOURNAL_NAME).exists()


def test_a_second_install_while_one_is_in_flight_is_held(tmp_path, storage):
    rec = storage.backup(tmp_path / "b")
    seen = []

    def meanwhile(phase, done, total):
        if phase == "install" and not seen:
            with pytest.raises(HeldError, match="in flight"):
                storage.install({BIT: src(tmp_path, "m.bit", b"m")}, backup=rec)
            with pytest.raises(HeldError, match="never interrupt"):
                storage.restore(rec)
            seen.append(1)

    storage.install({BIT: src(tmp_path, "n.bit", b"n")}, backup=rec, progress=meanwhile)
    assert seen == [1]


def _plant_killed_journal(sd: FakeSdVolume, rec, *, host="testhost", pid=999_999,
                          started_at=NOW) -> None:
    journal = {"op": "install", "state": "in-flight", "pid": pid, "host": host,
               "started_at": started_at, "planned": [BIT], "done": [], "created": [],
               "current": BIT, "backup": {"path": rec.path, "sha256": rec.sha256}}
    (sd.root / JOURNAL_NAME).write_text(json.dumps(journal))
    (sd.root / (BIT + ".socharness-tmp")).write_bytes(b"\x07" * 10)   # the half-written temp


def test_install_killed_mid_write_is_detected_and_restored(tmp_path, sd):
    live = {"alive": True}
    st = Mps3Storage(str(sd.root), env=env(pid_alive=lambda pid: live["alive"]))
    before = sd.snapshot()
    rec = st.backup(tmp_path / "b")
    _plant_killed_journal(sd, rec)
    with pytest.raises(HeldError, match="never retry"):          # its owner still runs
        st.install({BIT: src(tmp_path, "n.bit", b"n")}, backup=rec)
    with pytest.raises(HeldError):
        st.restore(rec)
    live["alive"] = False                                        # the owner is gone
    with pytest.raises(RefusedError, match="restore the backup"):
        st.install({BIT: src(tmp_path, "n.bit", b"n")}, backup=rec)
    st.restore(rec)
    assert sd.snapshot() == before and st.pending() is None


def test_foreign_host_journal_is_live_until_stale(tmp_path, sd, storage):
    rec = storage.backup(tmp_path / "b")
    _plant_killed_journal(sd, rec, host="hub", started_at=NOW - 60)
    with pytest.raises(HeldError):
        storage.install({BIT: src(tmp_path, "n.bit", b"n")}, backup=rec)
    _plant_killed_journal(sd, rec, host="hub", started_at=NOW - 3600)
    with pytest.raises(RefusedError, match="interrupted"):
        storage.install({BIT: src(tmp_path, "n.bit", b"n")}, backup=rec)


def test_read_back_mismatch_fails_and_leaves_the_journal(tmp_path, sd, storage, monkeypatch):
    rec = storage.backup(tmp_path / "b")
    real = sdmod._copy_stream

    def flaky(read, dest, tick):
        real(read, dest, tick)
        if dest.name == "nanosoc.bit":
            dest.write_bytes(b"CORRUPT")                  # what a bad card would hand back

    monkeypatch.setattr(sdmod, "_copy_stream", flaky)
    with pytest.raises(ActionFailedError, match="read-back mismatch"):
        storage.install({BIT: src(tmp_path, "n.bit", b"GOOD")}, backup=rec)
    assert storage.pending()["state"] == "verify-failed"
    monkeypatch.setattr(sdmod, "_copy_stream", real)
    storage.restore(rec)
    assert storage.pending() is None and sd.bit.read_bytes() == b"\x00" * 64


def test_restore_never_writes_or_deletes_ebf(tmp_path, sd, storage):
    rec = storage.backup(tmp_path / "b")
    sd.ebf.write_bytes(b"SOMEONE ELSE'S MCC FIRMWARE")          # changed outside the manager
    (sd.root / "MB/HBI0309C/mbb_v141.ebf").write_bytes(b"NEW BIOS")   # added outside it
    (sd.root / "added.txt").write_text("added after the backup")
    storage.restore(rec)
    assert sd.ebf.read_bytes() == b"SOMEONE ELSE'S MCC FIRMWARE"
    assert (sd.root / "MB/HBI0309C/mbb_v141.ebf").exists()
    assert not (sd.root / "added.txt").exists()                # a non-.ebf addition is removed
    assert sorted(storage.last_restore.ebf_left_alone) == ["MB/HBI0309C/mbb_v132.ebf",
                                                           "MB/HBI0309C/mbb_v141.ebf"]


# --- which volume: DAPLink refused -----------------------------------------------------


def test_daplink_drive_is_refused_by_content(tmp_path):
    dap = FakeDapLinkVolume(tmp_path / "dap")
    st = Mps3Storage(str(dap.root), env=env())
    with pytest.raises(RefusedError, match="DAPLink"):
        st.locate()
    with pytest.raises(RefusedError, match="DAPLink"):
        st.backup(tmp_path / "b")
    assert not (tmp_path / "b").exists()


def test_daplink_drive_is_refused_by_label(tmp_path, sd):
    # Even with config-SD content, a volume labelled "MBED MPS3" is the DAPLink drive.
    vols = [VolumeInfo(label="MBED MPS3", root=str(sd.root), device="/dev/sda")]
    with pytest.raises(RefusedError, match="DAPLink"):
        Mps3Storage(str(sd.root), env=env(vols)).locate()
    ok = [VolumeInfo(label="V2M-MPS3", root=str(sd.root), device="/dev/sdb1")]
    assert Mps3Storage(str(sd.root), env=env(ok)).locate() == str(sd.root)      # twin


def test_other_labels_and_plain_folders_are_refused(tmp_path, sd):
    vols = [VolumeInfo(label="USBSTICK", root=str(sd.root))]
    with pytest.raises(RefusedError, match="not the MPS3"):
        Mps3Storage(str(sd.root), env=env(vols)).locate()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RefusedError, match="does not look like"):
        Mps3Storage(str(empty), env=env()).locate()


# --- discovery by label: Linux and Windows views ----------------------------------------


def fake_linux(tmp_path: Path, sd_root: Path, *, mounted: bool = True) -> dict[str, Path]:
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "sdb1").write_bytes(b"")
    (dev / "sda").write_bytes(b"")
    by_label = tmp_path / "by-label"
    by_label.mkdir()
    os.symlink(dev / "sdb1", by_label / "V2M-MPS3")
    os.symlink(dev / "sda", by_label / "MBED\\x20MPS3")
    usbdev = (tmp_path / "sys/devices/pci0000:00/usb1/1-2/1-2.3/1-2.3.4/1-2.3.4.1/"
              "1-2.3.4.1:1.0/host0/target0:0:0/0:0:0:0/block/sdb/sdb1")
    usbdev.mkdir(parents=True)
    block = tmp_path / "sys/class/block"
    block.mkdir(parents=True)
    os.symlink(usbdev, block / "sdb1")
    mounts = tmp_path / "mounts"
    lines = [f"{dev / 'sda'} /media/u/MBED\\040MPS3 vfat rw 0 0"]
    if mounted:
        lines.append(f"{dev / 'sdb1'} {sd_root} vfat rw,nosuid 0 0")
    mounts.write_text("\n".join(lines) + "\n")
    return {"by_label": by_label, "mounts": mounts, "sys_block": block, "dev": dev}


@pytest.mark.skipif(os.name == "nt", reason="symlinks model the Linux /dev tree")
def test_linux_discovery_by_label(tmp_path, sd):
    paths = fake_linux(tmp_path, sd.root)
    vols = linux_volumes(by_label=paths["by_label"], mounts=paths["mounts"],
                         sys_block=paths["sys_block"])
    by = {v.label: v for v in vols}
    assert by["V2M-MPS3"].root == str(sd.root) and by["V2M-MPS3"].usb_path == "1-2.3.4.1"
    assert by["MBED MPS3"].root == "/media/u/MBED MPS3"          # \x20 and \040 unescaped
    st = Mps3Storage("", env=env(vols))
    assert st.locate() == str(sd.root)
    by_dev = Mps3Storage(str(paths["dev"] / "sdb1"), env=env(vols))   # a device path works too
    assert by_dev.locate() == str(sd.root)


@pytest.mark.skipif(os.name == "nt", reason="symlinks model the Linux /dev tree")
def test_linux_unmounted_sd_is_unavailable_with_a_hint(tmp_path, sd):
    paths = fake_linux(tmp_path, sd.root, mounted=False)
    vols = linux_volumes(by_label=paths["by_label"], mounts=paths["mounts"],
                         sys_block=paths["sys_block"])
    with pytest.raises(UnavailableError, match="not mounted") as info:
        Mps3Storage("", env=env(vols)).locate()
    assert info.value.code == ExitCode.UNAVAILABLE and "udisksctl" in str(info.value)


def test_no_sd_is_absent_and_two_are_ambiguous(tmp_path, sd):
    with pytest.raises(AbsentError, match="no volume labelled V2M-MPS3"):
        Mps3Storage("", env=env([VolumeInfo("MBED MPS3", "/x")])).locate()
    two = [VolumeInfo("V2M-MPS3", str(sd.root)), VolumeInfo("V2M-MPS3", "/media/other")]
    with pytest.raises(UsageError, match="2 volumes"):
        Mps3Storage("", env=env(two)).locate()
    assert Mps3Storage("label:V2M-MPS3", env=env(two[:1])).locate() == str(sd.root)   # twin


def test_windows_discovery_by_drive_letter():
    api = FakeWinVolumes({"C:\\": "OS", "E:\\": "V2M-MPS3", "F:\\": "MBED MPS3", "G:\\": None})
    vols = windows_volumes(api)
    assert [(v.label, v.root, v.device) for v in vols] == [
        ("OS", "C:\\", "C:"), ("V2M-MPS3", "E:\\", "E:"), ("MBED MPS3", "F:\\", "F:")]
    st = Mps3Storage("", env=env(vols))
    assert str(st._find_root()) == "E:\\"                        # the DAPLink F: is not taken
    assert sdmod.is_daplink_label("MBED MPS3") and not sdmod.is_daplink_label("V2M-MPS3")


# --- the pack hook -------------------------------------------------------------------


def test_hook_needs_a_usb_msd_link(sd):
    no = SimpleNamespace(candidate=Candidate("mps3", "b", (Link(LinkKind.ETHERNET, "h:1"),)))
    assert sdmod.make_storage_adapter(no) is None
    yes = SimpleNamespace(candidate=Candidate("mps3", "b", (Link(LinkKind.USB_MSD, str(sd.root)),)))
    adapter = sdmod.make_storage_adapter(yes)
    assert isinstance(adapter, Mps3Storage) and adapter.address == str(sd.root)
