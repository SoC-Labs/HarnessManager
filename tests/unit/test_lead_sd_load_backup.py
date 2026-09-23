"""Lead-added after the T3 merge: StorageAdapter.load_backup (contract CCR from T5)."""

from __future__ import annotations

import zipfile

import pytest

from socharness.core.errors import RefusedError
from tests.unit.test_t3_sd import sd, storage  # noqa: F401 - reuse T3's fixtures


def test_load_backup_round_trips_a_real_backup(tmp_path, sd, storage):  # noqa: F811
    rec = storage.backup(tmp_path / "bk")
    loaded = storage.load_backup(rec.path)
    assert (loaded.sha256, loaded.files, loaded.volume_label) == (rec.sha256, rec.files, rec.volume_label)


def test_load_backup_refuses_a_tampered_archive(tmp_path, sd, storage):  # noqa: F811
    rec = storage.backup(tmp_path / "bk")
    with zipfile.ZipFile(rec.path, "a") as zf:
        zf.writestr("volume/extra.txt", b"sneaky")
    with pytest.raises(RefusedError):
        storage.load_backup(rec.path)


def test_load_backup_without_sidecar_is_verified_but_flagged(tmp_path, sd, storage):  # noqa: F811
    rec = storage.backup(tmp_path / "bk")
    from pathlib import Path
    Path(rec.path + ".sha256").unlink()
    loaded = storage.load_backup(rec.path)
    assert "no .sha256 sidecar" in loaded.volume_label


def test_load_backup_missing_file(tmp_path, storage):  # noqa: F811
    with pytest.raises(RefusedError):
        storage.load_backup(tmp_path / "nope.zip")
