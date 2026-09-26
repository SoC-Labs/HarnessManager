"""HUB-SD U8: the config SD A/B by pointer, on a fake SD tree. Each check has a twin.

The tree is T7's (``t7_bundles.sd_files``): ``board.txt`` -> ``APPFILE: Nanosoc\\nanosoc.txt``
-> ``F0FILE: nanosoc.bit``. Nothing here touches a real volume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager_mps3.sd import Mps3Storage, file_sha256
from harness_manager_mps3.sd_ab import AbStorage, patch_f0file, read_pointer
from tests.fakes.t7_bundles import Release, sd_files

OLD = Release.fielded(with_overlays=False)
NEW = Release("1.1.0", with_overlays=False)
NEWER = Release("1.1.1", sha="0e12a0b0", with_overlays=False)
NOTE = "MB/HBI0309C/Nanosoc/nanosoc.txt"


def _tree(root: Path, files: dict[str, bytes]) -> dict[str, Path]:
    out = {}
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out[rel] = p
    return out


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "sd"
    _tree(root, sd_files(OLD.bit()))
    return root


def release_files(tmp: Path, rel: Release, **kw) -> dict[str, Path]:
    return _tree(tmp / f"rel-{rel.version}", sd_files(rel.bit(), **kw))


def test_install_writes_the_inactive_image_then_flips_the_pointer(card, tmp_path):
    ab = AbStorage(Mps3Storage(str(card)))
    legacy = file_sha256(card / "MB/HBI0309C/Nanosoc/nanosoc.bit")
    rec = ab.backup(tmp_path / "backups")
    ab.install(release_files(tmp_path, NEW), backup=rec)
    ptr = read_pointer(card)
    assert ptr.image_name == "nanosoca.bit"
    assert (card / "MB/HBI0309C/Nanosoc/nanosoca.bit").read_bytes() == NEW.bit()
    assert file_sha256(card / "MB/HBI0309C/Nanosoc/nanosoc.bit") == legacy     # never touched
    assert ab.last_install.pointer_from == "nanosoc.bit" and ab.last_install.verified
    # the next install alternates, and leaves the running image alone
    rec2 = ab.backup(tmp_path / "backups")
    ab.install(release_files(tmp_path, NEWER), backup=rec2)
    assert read_pointer(card).image_name == "nanosocb.bit"
    assert (card / "MB/HBI0309C/Nanosoc/nanosoca.bit").read_bytes() == NEW.bit()
    assert ab.pending() is None


def test_twin_rollback_is_the_flip_back(card, tmp_path):
    ab = AbStorage(Mps3Storage(str(card)))
    before = (card / NOTE).read_bytes()
    rec = ab.backup(tmp_path / "backups")
    ab.install(release_files(tmp_path, NEW), backup=rec)
    assert (card / NOTE).read_bytes() != before
    ab.restore(ab.load_backup(Path(rec.path)))
    assert (card / NOTE).read_bytes() == before and read_pointer(card).image_name == "nanosoc.bit"
    # the flip back wrote no image: both are still there
    assert (card / "MB/HBI0309C/Nanosoc/nanosoca.bit").read_bytes() == NEW.bit()


def test_a_release_that_changes_more_than_the_image_is_refused(card, tmp_path):
    ab = AbStorage(Mps3Storage(str(card)))
    rec = ab.backup(tmp_path / "backups")
    files = release_files(tmp_path, NEW, extra={"config.txt": b"TITLE: other\n"})
    with pytest.raises(RefusedError) as err:
        ab.install(files, backup=rec)
    assert "config.txt" in err.value.message and "sd_ab off" in err.value.hint
    assert not (card / "MB/HBI0309C/Nanosoc/nanosoca.bit").exists()
    assert read_pointer(card).image_name == "nanosoc.bit"


def test_twin_the_pointer_line_alone_differing_is_not_a_change(card, tmp_path):
    ab = AbStorage(Mps3Storage(str(card)))
    ab.install(release_files(tmp_path, NEW), backup=ab.backup(tmp_path / "b"))
    # the release's nanosoc.txt still says nanosoc.bit; the card's says nanosoca.bit
    ab.install(release_files(tmp_path, NEWER), backup=ab.backup(tmp_path / "b"))
    assert read_pointer(card).image_name == "nanosocb.bit"


def test_a_flip_back_to_an_image_that_changed_is_refused(card, tmp_path):
    ab = AbStorage(Mps3Storage(str(card)))
    rec = ab.backup(tmp_path / "backups")
    ab.install(release_files(tmp_path, NEW), backup=rec)
    (card / "MB/HBI0309C/Nanosoc/nanosoc.bit").write_bytes(b"not what it was")
    with pytest.raises(RefusedError) as err:
        ab.restore(rec)
    assert "would boot something else" in err.value.message
    assert read_pointer(card).image_name == "nanosoca.bit"


def test_twin_a_backup_of_another_pointer_is_refused_before_any_write(card, tmp_path):
    ab = AbStorage(Mps3Storage(str(card)))
    stale = ab.backup(tmp_path / "backups")
    ab.install(release_files(tmp_path, NEW), backup=ab.backup(tmp_path / "backups"))
    with pytest.raises(RefusedError) as err:
        ab.install(release_files(tmp_path, NEWER), backup=stale)
    assert "pointer changed" in err.value.message
    assert not (card / "MB/HBI0309C/Nanosoc/nanosocb.bit").exists()


def test_patch_f0file_keeps_everything_else_byte_for_byte():
    text = "F0FILE: nanosoc.bit\r\n[OSCCLKS]\r\nOSC0: 25.0\r\n"
    assert patch_f0file(text, "nanosocb.bit") == "F0FILE: nanosocb.bit\r\n[OSCCLKS]\r\nOSC0: 25.0\r\n"


def test_twin_patch_f0file_refuses_a_name_the_mcc_cannot_read():
    with pytest.raises(RefusedError):
        patch_f0file("F0FILE: nanosoc.bit\n", "NanoSoC_image_b.bit")
    with pytest.raises(RefusedError):
        patch_f0file("TITLE: nothing to point\n", "nanosocb.bit")
