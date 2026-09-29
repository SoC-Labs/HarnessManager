"""KIT-NIGHT: what the first end-to-end run of the DUT build flow on the RC2 kit v2 (real
Vivado 2026.1, 29 Sep 2026; docs/evidence/2026-09-29-kit-night/) found, fixed board-free.

- ``kit verify`` takes the kit zip a user was handed (it said exit 15 "cannot read
  <zip>/kit.json: Not a directory"): checked as it is, the checks ``import`` makes, nothing
  cached, the extraction removed;
- the ``vivado`` found on PATH is normalised: AMD's ``settings64.sh`` puts
  ``/research/CAD/Xilinx/Vivado//2026.1/Vivado/bin`` on PATH, and the ``//`` reached the
  printed build command, ``README.txt`` and the guide.

Every check has a negative twin. Vivado is never run.
"""

from __future__ import annotations

import shutil
import subprocess
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


# --- the PATH vivado, normalised --------------------------------------------------------------


class ByPath:
    """Answers ``vivado -version`` with the release in the executable's path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, argv, **_):
        self.calls.append(argv[0])
        rel = vivado.release_of_path(argv[0]) or "2099.9"
        return subprocess.CompletedProcess(argv, 0, f"vivado v{rel} (64-bit)\nSW Build 1 on x\n",
                                           "")


def install(root: Path, rel: str) -> Path:
    exe = root / rel / "Vivado" / "bin" / "vivado"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_the_path_vivado_from_settings64_is_printed_without_a_double_slash(tmp_path):
    root = tmp_path / "research" / "CAD" / "Xilinx" / "Vivado"
    exe = install(root, "2026.1")
    doubled = f"{root}//2026.1/Vivado/bin/vivado"            # what settings64.sh gives
    assert Path(doubled).is_file() and "//" in doubled
    run = ByPath()
    f = vivado.discover(runner=run, env={}, which=lambda _: doubled, roots=(), want="2026.1")
    assert f.install.path == str(exe) and "//" not in f.install.path
    assert f.install.how == "path" and f.install.version == "2026.1"
    assert f.on_path.path == str(exe)
    assert vivado.command_vivado(f, "2026.1") == str(exe)
    assert run.calls == [str(exe)]
    # twin: a clean PATH spelling is kept exactly as it was
    f = vivado.discover(runner=ByPath(), env={}, which=lambda _: str(exe), roots=(),
                        want="2026.1")
    assert f.install.path == str(exe)
    # and no vivado on PATH at all is still None, not "."
    f = vivado.discover(runner=ByPath(), env={}, which=lambda _: None, roots=(),
                        want="2026.1")
    assert f.install is None and f.on_path is None


# --- the build dir's README names the design the user built -----------------------------------


def _spike_design(tmp_path: Path) -> Path:
    import json

    p = tmp_path / "spike_rm.json"
    p.write_text(json.dumps({"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
                             "use": {"clkrst": {}, "status": {}, "gpio": {"timed": True}},
                             "build": {"sources": [str(kf.SPIKE_RM)], "top": "rm_spike_rm"}}))
    return p


def test_the_readme_names_the_design_it_was_written_for(kits, tmp_path, monkeypatch):
    from harness_manager.services.kit import script

    monkeypatch.setenv(vivado.ENV, "off")
    kits.import_(kf.FIXTURE)
    # the runbook's built-in minimal: `--design minimal`, not a placeholder
    s = script.make_script(kits, pack="mps3", static_id=kf.STATIC_ID, design="minimal")
    readme = s.files["README.txt"]
    assert "--design minimal --build-dir ." in readme
    assert "<your design .json>" not in readme
    assert "up to an hour on a loaded one" in readme              # the measured time
    # twin: a design file is named by its path
    p = _spike_design(tmp_path)
    s = script.make_script(kits, pack="mps3", static_id=kf.STATIC_ID, design=str(p))
    assert f"--design {p.resolve()} --build-dir ." in s.files["README.txt"]
    # twin: an inline design (the API's) has no name to give: the placeholder stays
    import json

    doc = json.loads(p.read_text())
    s = script.make_script(kits, pack="mps3", static_id=kf.STATIC_ID, design=doc)
    assert "--design <your design .json> --build-dir ." in s.files["README.txt"]
