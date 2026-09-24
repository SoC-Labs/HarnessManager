"""``harness-manager kit`` end to end through ``cli/main.py`` (KIT-CORE K5 / KG-C).

The fixture kit (a fake DCP whose CRC-32 is 0x72BB0A36), a state dir per test (conftest),
Vivado discovery pointed at a dummy file whose version read is faked (nothing is run), and
for TARGET the virtual MPS3: the ILA-mint profile runs 0x72BB0A36, the default one the
previous static 0x3F1A560F. Every success has a failing twin; every TSV layout is pinned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.cli import main as cli_main
from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.services.kit import vivado
from tests.fakes import kit_fakes as kf
from tests.fakes.t13_daemon import engine_for
from tests.fakes.virtual_board import VirtualMps3, ila_mint_bake_profile


@pytest.fixture(autouse=True)
def fake_vivado(tmp_path, monkeypatch):
    exe = tmp_path / "Vivado" / "2024.1" / "bin" / "vivado"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv(vivado.ENV, str(exe))
    monkeypatch.setattr(vivado, "read_version", lambda path, runner=None, timeout_s=0:
                        ("2024.1", 5076996, ""))
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    return exe


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli_main.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def js(capsys, *argv: str) -> tuple[int, dict]:
    rc, out, _ = run(capsys, *argv, "--json")
    return rc, json.loads(out)


def design(tmp_path: Path, **over) -> Path:
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}, "gpio": {"timed": True}},
         "build": {"sources": [str(kf.SPIKE_RM)]}}
    d.update(over)
    p = tmp_path / "spike_rm.json"
    p.write_text(json.dumps(d))
    return p


def tsv_ok(out: str, layout: str) -> None:
    rows = [line.split("\t") for line in out.splitlines() if line]
    assert rows, out
    assert all(len(r) == len(TSV_COLUMNS[layout]) for r in rows), (layout, rows)


# --- the journey, transcript style ---------------------------------------------------------------


def test_the_journey_from_kit_to_program(tmp_path, capsys):
    # import the fixture kit (a lab user's fielded/<sid>/ goes the same way)
    rc, out, _ = run(capsys, "kit", "import", str(kf.FIXTURE))
    assert rc == 0 and "cached: mps3/0x72BB0A36/vivado-2024.1" in out
    rc, out, _ = run(capsys, "kit", "list")
    assert rc == 0 and "0x72BB0A36  mps3/0x72BB0A36/vivado-2024.1" in out
    rc, out, _ = run(capsys, "kit", "info", "--static-id", "0x72bb0a36")
    assert rc == 0 and "static     0x72BB0A36" in out
    assert "needs 2024.1; found 2024.1" in out and "u_rp_dut / pblock_rp_dut, 47 ports / 148 bits" in out
    # fetch (from the cache) into a plain dir, then verify that dir
    kit_dir = tmp_path / "kit"
    rc, out, _ = run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36", "--out", str(kit_dir))
    assert rc == 0 and "from cache" in out and (kit_dir / "kit.json").is_file()
    rc, out, _ = run(capsys, "kit", "verify", str(kit_dir))
    assert rc == 0 and "CRC-32 of static_routed_locked.dcp is 0x72BB0A36" in out
    # the guide before the build: the build step is next
    dfile = design(tmp_path)
    bdir = tmp_path / "build" / "spike_rm"
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", "0x72BB0A36", "--design", str(dfile),
                     "--build-dir", str(bdir))
    assert rc == 0 and "NEXT   5 Build" in out and "done   3 Kit" in out
    # write the build dir
    rc, out, _ = run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design",
                     str(dfile), "--out", str(bdir))
    assert rc == 0 and (bdir / "build_rm.tcl").is_file() and "next: vivado -mode batch" in out
    rc, out, _ = run(capsys, "kit", "build", str(bdir))
    assert rc == 0 and "does not run Vivado yet" in out and "-source" in out
    # "Vivado ran": the receipt and the pair it names
    receipt = kf.passed_build(bdir)
    rc, out, _ = run(capsys, "kit", "check", str(receipt))
    assert rc == 0 and "passed" in out and "partial: frame_box" in out
    rc, out, _ = run(capsys, "kit", "pack", str(receipt), "--import")
    assert rc == 0 and "imported into the store" in out
    assert (bdir / "overlay" / "spike_rm" / "manifest.json").is_file()
    rc, g = js(capsys, "kit", "guide", "--static-id", "0x72BB0A36", "--design", str(dfile),
               "--build-dir", str(bdir))
    assert [s["state"] for s in g["steps"]] == ["done"] * 6 and g["next"] is None


# --- the failing twins ---------------------------------------------------------------------------


def test_no_static_is_a_usage_error(capsys):
    rc, _, err = run(capsys, "kit", "info")
    assert rc == ExitCode.USAGE and "which static" in err


def test_fetch_with_no_source_is_absent_and_names_what_it_tried(capsys):
    rc, _, err = run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36")
    # OTA-C wired the signed channel: it is tried, and says why it had no kit (the test
    # suite's update source is an absent local dir, never GitHub)
    assert rc == ExitCode.ABSENT and "channel: channel.json not found" in err
    assert "cache: not cached" in err and "hub:" in err


def test_fetch_from_the_hub_archive_path(tmp_path, capsys, monkeypatch):
    hub = tmp_path / "mints"
    kf.fielded_dir(hub / "0x72BB0A36")
    monkeypatch.setenv("HARNESS_MANAGER_KIT_HUB_DIR", str(hub))
    rc, out, _ = run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36")
    assert rc == 0 and "from hub" in out


def test_a_tampered_kit_dir_fails_verify(tmp_path, capsys):
    run(capsys, "kit", "import", str(kf.FIXTURE))
    out_dir = tmp_path / "k"
    run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36", "--out", str(out_dir))
    (out_dir / "static" / "static_stamp.json").write_text("{}")
    rc, out = js(capsys, "kit", "verify", str(out_dir))
    assert rc == ExitCode.REFUSED
    assert {c["name"]: c["state"] for c in out["error"]["data"]["checks"]}["files"] == "mismatch"


def test_a_corrupt_kit_is_refused_on_import(tmp_path, capsys):
    import shutil

    bad = shutil.copytree(kf.FIXTURE, tmp_path / "bad")
    (bad / "static" / "static_routed_locked.dcp").write_bytes(kf.fake_dcp("0x3F1A560F"))
    rc, _, err = run(capsys, "kit", "import", str(bad))
    assert rc == ExitCode.REFUSED and "sha256" in err


def test_a_failed_build_is_refused_by_check_and_pack(tmp_path, capsys):
    receipt = kf.passed_build(tmp_path, state="failed", gates=[
        {"gate": "rm_id_match", "verdict": "FAIL", "detail": "the netlist drives 0x010080F0"}])
    rc, _, err = run(capsys, "kit", "check", str(receipt))
    assert rc == ExitCode.REFUSED and "rm_id_match" in err
    rc, _, err = run(capsys, "kit", "pack", str(receipt), "--import")
    assert rc == ExitCode.REFUSED
    assert not (tmp_path / "overlay").exists()


def test_check_a_bare_partial_and_its_swapped_twin(tmp_path, capsys):
    p = tmp_path / "p.bin"
    c = tmp_path / "c.bin"
    p.write_bytes(kf.stream())
    c.write_bytes(kf.clearing_stream())
    rc, out, _ = run(capsys, "kit", "check", str(p), "--clearing", str(c))
    assert rc == 0 and "static_binding" in out
    rc, _, err = run(capsys, "kit", "check", str(c), "--clearing", str(p))
    assert rc == ExitCode.REFUSED and "swapped" in err


def test_script_refuses_a_design_that_fails_its_xdc_checks(tmp_path, capsys):
    run(capsys, "kit", "import", str(kf.FIXTURE))
    bad = design(tmp_path, ports=[{"name": "dut_clk", "dir": "in"}])
    rc, out = js(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design", str(bad),
                 "--out", str(tmp_path / "b"))
    assert rc == ExitCode.REFUSED and not (tmp_path / "b").exists()
    assert any(c["name"].startswith("xdc:") for c in out["error"]["data"]["checks"])


def test_guide_why_names_the_fix(capsys):
    rc, out, _ = run(capsys, "kit", "guide", "--why", "rm_timing")
    assert rc == 0 and "RM_XDC" in out
    rc, _, _ = run(capsys, "kit", "guide", "--why", "nonesuch")
    assert rc == ExitCode.USAGE


def test_print_tcl_for_a_vivado_session(tmp_path, capsys):
    run(capsys, "kit", "import", str(kf.FIXTURE))
    rc, out, _ = run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36", "--out",
                     str(tmp_path / "k"), "--print-tcl")
    assert rc == 0
    assert out.splitlines()[1:] == ["set HM_STATIC_ID 0x72BB0A36", "set HM_VIVADO 2024.1"]
    assert out.startswith("set HM_KIT_DIR {")


# --- TARGET: the board's live static ---------------------------------------------------------------


@pytest.fixture
def board(tmp_path):
    with VirtualMps3(tmp_path / "ila", profile=ila_mint_bake_profile()) as vb:
        previous = set_engine_factory(lambda args: engine_for(vb))
        try:
            yield vb
        finally:
            set_engine_factory(previous)


@pytest.fixture
def old_board(vboard):
    previous = set_engine_factory(lambda args: engine_for(vboard))
    try:
        yield vboard
    finally:
        set_engine_factory(previous)


def test_a_board_on_the_kits_static_passes_and_one_on_another_is_incompatible(
        board, tmp_path, capsys):
    run(capsys, "kit", "import", str(kf.FIXTURE))
    kit_dir = tmp_path / "k"
    rc, out, _ = run(capsys, "kit", "fetch", board.shell_endpoint, "--out", str(kit_dir))
    assert rc == 0, out
    rc, out, _ = run(capsys, "kit", "verify", str(kit_dir), board.shell_endpoint)
    assert rc == 0 and "the board runs 0x72bb0a36" in out.lower()
    rc, g = js(capsys, "kit", "guide", board.shell_endpoint)
    assert rc == 0 and g["static_id"] == "0x72BB0A36" and g["steps"][0]["state"] == "done"


def test_a_kit_for_another_static_than_the_board_exits_14(old_board, tmp_path, capsys):
    run(capsys, "kit", "import", str(kf.FIXTURE))
    kit_dir = tmp_path / "k"
    run(capsys, "kit", "fetch", "--static-id", "0x72BB0A36", "--out", str(kit_dir))
    rc, _, err = run(capsys, "kit", "verify", str(kit_dir), old_board.shell_endpoint)
    assert rc == ExitCode.INCOMPATIBLE and "0x3f1a560f" in err.lower()


# --- TSV layouts (the T5 census pins them here) -------------------------------------------------


def test_every_kit_tsv_layout(tmp_path, capsys):
    rc, out, _ = run(capsys, "kit", "import", str(kf.FIXTURE), "--tsv")
    assert rc == 0
    tsv_ok(out, "kit")
    rc, out, _ = run(capsys, "kit", "list", "--tsv")
    tsv_ok(out, "kit list")
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", "0x72BB0A36", "--tsv")
    tsv_ok(out, "kit guide")
    assert out.splitlines()[0].split("\t")[:3] == ["1", "target", "done"]
    rc, out, _ = run(capsys, "kit", "info", "--static-id", "0x72BB0A36", "--tsv")
    tsv_ok(out, "kit")
