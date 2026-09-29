"""KIT-NIGHT: what the first end-to-end run of the DUT build flow on the RC2 kit v2 (real
Vivado 2026.1, 29 Sep 2026; docs/evidence/2026-09-29-kit-night/) found, fixed board-free.

- ``kit verify`` takes the kit zip a user was handed (it said exit 15 "cannot read
  <zip>/kit.json: Not a directory"): checked as it is, the checks ``import`` makes, nothing
  cached, the extraction removed;

Every check has a negative twin. Vivado is never run.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from harness_manager.cli import main as cli_main
from harness_manager.core.errors import ExitCode, UsageError
from harness_manager.services.kit import vivado
from harness_manager.services.kit.schema import KitFormatError
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from tests.fakes import kit_fakes as kf


@pytest.fixture
def kits(tmp_path: Path) -> KitService:
    return KitService(ContentStore(tmp_path / "store"), tmp_path / "kits",
                      hub=HubSource(None))


def zip_dir(src: Path, dest: Path, top: str = "mps3_kit") -> Path:
    """A kit zip the way the Linux lead publishes one: everything one directory down."""
    with zipfile.ZipFile(dest, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, f"{top}/{p.relative_to(src).as_posix()}")
    return dest


def st(checks) -> dict[str, str]:
    return {c.name: c.state for c in checks}


# --- kit verify ZIP ---------------------------------------------------------------------------


def test_verify_takes_a_kit_zip_checks_it_and_caches_nothing(kits, tmp_path):
    z = zip_dir(kf.FIXTURE, tmp_path / "kit.zip")
    manifest, checks = kits.verify_dir(z)
    assert manifest.static_id == kf.STATIC_ID
    assert st(checks) == {"files": "ok", "static_id": "ok"}
    assert kits.list() == []                                  # verify never caches
    assert not any(kits.work_dir.glob(".unzip-*"))            # the extraction is gone
    # twin: the same zip with one byte of the DCP flipped fails the same checks
    bad = shutil.copytree(kf.FIXTURE, tmp_path / "bad")
    dcp = bad / "static" / "static_routed_locked.dcp"
    data = bytearray(dcp.read_bytes())
    data[10] ^= 0xFF
    dcp.write_bytes(bytes(data))
    _, checks = kits.verify_dir(zip_dir(bad, tmp_path / "bad.zip"))
    assert st(checks) == {"files": "mismatch", "static_id": "mismatch"}
    assert not any(kits.work_dir.glob(".unzip-*"))


def test_verify_refuses_a_file_that_is_no_zip_and_a_zip_with_no_kit(kits, tmp_path):
    note = tmp_path / "notes.txt"
    note.write_text("not a kit\n")
    with pytest.raises(UsageError, match="neither a kit directory nor a zip"):
        kits.verify_dir(note)
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("README.txt", "no kit.json here\n")
    with pytest.raises(KitFormatError, match="holds no kit.json"):
        kits.verify_dir(empty)
    assert not any(kits.work_dir.glob(".unzip-*"))
    # twin: a plain kit directory still verifies as before
    _, checks = kits.verify_dir(kf.FIXTURE)
    assert st(checks) == {"files": "ok", "static_id": "ok"}


def test_import_still_takes_the_zip_after_the_refactor(kits, tmp_path):
    res = kits.import_(zip_dir(kf.FIXTURE, tmp_path / "kit.zip"))
    assert st(res.checks) == {"files": "ok", "static_id": "ok"}
    assert [k.static_id for k in kits.list()] == [kf.STATIC_ID]
    assert not any(kits.work_dir.glob(".unzip-*"))
    with pytest.raises(KitFormatError, match="holds no kit.json"):   # twin
        empty = tmp_path / "empty.zip"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("x.txt", "x")
        kits.import_(empty)


def test_cli_kit_verify_zip_passes_and_a_tampered_zip_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    rc = cli_main.main(["kit", "verify", str(zip_dir(kf.FIXTURE, tmp_path / "kit.zip"))])
    out = capsys.readouterr().out
    assert rc == ExitCode.OK, out
    assert "kit mps3/0x72BB0A36/vivado-2024.1" in out and "static_id" in out
    rc = cli_main.main(["kit", "list"])
    assert "no kits cached" in capsys.readouterr().out            # verify cached nothing
    # twin: a tampered zip is refused (15), naming the file check
    bad = shutil.copytree(kf.FIXTURE, tmp_path / "bad")
    (bad / "static" / "static_stamp.json").write_text("{}")
    rc = cli_main.main(["kit", "verify", str(zip_dir(bad, tmp_path / "bad.zip"))])
    assert rc == ExitCode.REFUSED
    # twin: not a zip at all is a usage error (2), not a refused kit
    note = tmp_path / "notes.txt"
    note.write_text("x\n")
    rc = cli_main.main(["kit", "verify", str(note)])
    err = capsys.readouterr().err
    assert rc == ExitCode.USAGE and "neither a kit directory nor a zip" in err
