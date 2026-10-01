"""KIT-INTERACTIVE through ``harness-manager kit`` (cli/main.py): the build run in the
user's own Vivado (docs/evidence/2026-09-30-kit-interactive).

- ``kit build DIR`` prints, beside the batch command, the one Tcl line for a Vivado that is
  already open (``cd {DIR}; set argv {...}; source build_rm.tcl``); ``--gui`` prints the GUI
  command first; ``--json`` has all three. With ``--stop-after link`` it says how to keep a
  floorplan (``hm_save_floorplan FILE``, as build.rm_xdc; not ``write_xdc -cell``);
- ``kit check`` of a ``stopped`` receipt (a ``STOP_AFTER`` run) says "stopped after <stage>"
  and exits 0: it said "failed 1 check" and exited 15;
- ``kit guide`` of a stopped receipt offers the command that runs the build to the end.

The fixture kit and a faked Vivado discovery (``test_kit_cli.py``'s autouse fixture). Vivado
is never run. Every check has a negative twin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode
from harness_manager.services.kit import vivado
from tests.fakes import kit_fakes as kf
from tests.integration.test_kit_cli import design, fake_vivado, run  # noqa: F401 (fixture)


@pytest.fixture
def bdir(tmp_path, capsys) -> Path:
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    d = tmp_path / "builds" / "spike_rm"
    rc, _, err = run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design",
                     str(design(tmp_path)), "--out", str(d))
    assert rc == 0, err
    return d


def exe() -> str:
    """The Vivado the commands name: test_kit_cli's autouse fake_vivado sets it."""
    return os.environ[vivado.ENV]


def stopped(bdir: Path, stage: str = "link") -> Path:
    """A receipt as build_rm.tcl writes it after STOP_AFTER=<stage>: the gates up to there
    passed, no pair."""
    r = kf.passed_build(bdir, state="stopped", gates=[
        {"gate": "static_id", "verdict": "PASS", "detail": "CRC-32 is 0x72BB0A36"},
        {"gate": "rm_id_match", "verdict": "PASS", "detail": "0x010080F0"},
        {"gate": "drc_hdpr_link", "verdict": "PASS", "detail": "no HDPR-16/18/50"}])
    doc = json.loads(r.read_text())
    doc["stage"] = stage
    for k in ("partial_bin", "partial_len", "partial_crc32", "clearing_bin", "clearing_len",
              "clearing_crc32", "built", "rm_wns", "rm_whs"):
        doc.pop(k, None)
    r.write_text(json.dumps(doc))
    for f in r.parent.glob("*_partial*"):
        f.unlink()
    return r


# --- kit build: the three ways -------------------------------------------------------------------


def test_kit_build_prints_the_tcl_line_for_an_open_vivado(bdir, capsys):
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--stop-after", "link", "--jobs", "4")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].startswith(f"{exe()} -mode batch")               # batch is first
    assert lines[0].endswith("-tclargs STOP_AFTER=link JOBS=4")
    src = f"cd {{{bdir.resolve().as_posix()}}}; set argv {{STOP_AFTER=link JOBS=4}}; source build_rm.tcl"
    assert f"  {src}" in lines
    assert "a design already open stays open beside the build's" in out
    assert "`hm_save_floorplan FILE` (not write_xdc -cell)" in out          # the floorplan
    # twin: no --stop-after: the line still sets argv (to nothing), and no floorplan words
    rc, out, _ = run(capsys, "kit", "build", str(bdir))
    assert rc == 0
    assert f"  cd {{{bdir.resolve().as_posix()}}}; set argv {{}}; source build_rm.tcl" in out
    assert "-tclargs" not in out.splitlines()[0] and "hm_save_floorplan" not in out


def test_kit_build_gui_prints_the_gui_command_first(bdir, capsys):
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--gui", "--stop-after", "link")
    assert rc == 0
    first = out.splitlines()[0]
    assert first.startswith(f"{exe()} -mode gui -source ")
    assert first.endswith("-tclargs STOP_AFTER=link")
    assert f"-log {bdir.resolve().as_posix()}/build_rm.log" in first      # markers land there
    # twin: without --gui the first line is the batch command, as before
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--stop-after", "link")
    assert out.splitlines()[0].startswith(f"{exe()} -mode batch -source ")


def test_kit_build_json_has_every_way(bdir, capsys):
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--gui", "--stop-after", "synth",
                     "--json")
    assert rc == 0
    d = json.loads(out)
    assert d["mode"] == "gui" and d["command"] == d["commands"]["gui"]
    assert set(d["commands"]) == {"batch", "gui", "tcl"}
    for mode, argv in d["commands"].items():
        assert argv[:3] == [str(exe()), "-mode", mode]
        assert argv[-2:] == ["-tclargs", "STOP_AFTER=synth"]
    assert d["source_tcl"].endswith("set argv {STOP_AFTER=synth}; source build_rm.tcl")
    assert d["stays_open"] is None and d["ran"] is False          # synth: nothing to floorplan
    # twin: batch by default, and the link stop names the floorplan
    rc, out, _ = run(capsys, "kit", "build", str(bdir), "--stop-after", "link", "--json")
    d = json.loads(out)
    assert d["mode"] == "batch" and d["command"] == d["commands"]["batch"]
    assert "hm_save_floorplan FILE" in d["stays_open"] and "read_xdc -cell u_rp_dut" in d["stays_open"]


def _same_lines(d: Path, capsys, stop: str) -> None:
    """``kit build`` prints exactly the lines the Build tab's "Run it your way" shows
    (``render.run_commands``, the page's ``run`` from the daemon): batch, GUI, the open-session
    line and, after link, the floorplan loop. ONE source: ``vivado_command`` / ``source_tcl``."""
    from harness_manager.services.kit import render

    web = render.run_commands(d.resolve(), vivado=exe(), stop_after=stop)
    extra = ["--stop-after", stop] if stop else []
    rc, out, _ = run(capsys, "kit", "build", str(d), *extra)
    assert rc == 0
    assert out.splitlines()[0] == web["batch"]["text"]
    assert f"  {web['session']['text']}" in out.splitlines()
    rc, out, _ = run(capsys, "kit", "build", str(d), "--gui", *extra)
    assert rc == 0
    assert out.splitlines()[0] == web["gui"]["text"]
    rc, out, _ = run(capsys, "kit", "build", str(d), "--gui", *extra, "--json")
    j = json.loads(out)
    assert j["commands"]["batch"] == web["batch"]["argv"] and j["commands"]["gui"] == web["gui"]["argv"]
    assert j["source_tcl"] == web["session"]["text"] and j["stays_open"] == web["stays_open"]


def test_kit_build_and_the_build_tabs_run_it_your_way_print_the_same_lines(bdir, capsys):
    _same_lines(bdir, capsys, "link")
    _same_lines(bdir, capsys, "")


def test_twin_a_build_directory_with_a_space_is_quoted_the_same_way(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    d = tmp_path / "my builds" / "spike_rm"
    rc, _, err = run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design",
                     str(design(tmp_path)), "--out", str(d))
    assert rc == 0, err
    _same_lines(d, capsys, "link")
    rc, out, _ = run(capsys, "kit", "build", str(d))
    assert f"'{d.resolve().as_posix()}/build_rm.tcl'" in out.splitlines()[0]   # one shell word


def test_a_script_written_to_stop_after_link_names_the_floorplan(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    d = tmp_path / "b"
    assert run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design",
               str(design(tmp_path)), "--out", str(d), "--stop-after", "link")[0] == 0
    readme = (d / "README.txt").read_text()
    assert "set argv {STOP_AFTER=link}; source build_rm.tcl" in readme
    assert "hm_save_floorplan FILE" in readme and "-mode gui" in readme
    rc, out, _ = run(capsys, "kit", "build", str(d))          # the script's own STOP_AFTER
    assert rc == 0 and "hm_save_floorplan" in out and "read_xdc -cell u_rp_dut" in out
    rc, out, _ = run(capsys, "kit", "build", str(d), "--stop-after", "bitstream")   # twin
    assert rc == 0 and "hm_save_floorplan" not in out


# --- a stopped receipt is not a failure ---------------------------------------------------------------


def test_kit_check_reads_a_stopped_receipt_as_stopped(bdir, capsys):
    r = stopped(bdir, "link")
    rc, out, err = run(capsys, "kit", "check", str(r))
    assert rc == ExitCode.OK, err
    assert out.startswith("the build spike_rm: stopped after link (STOP_AFTER=link), not a "
                          "failure: the 3 gates up to there passed")
    assert f"next: harness-manager kit build {bdir} --stop-after bitstream" in out
    assert "failed" not in out
    rc, out, _ = run(capsys, "kit", "check", str(r), "--json")
    d = json.loads(out)
    assert d["passed"] is False and d["state"] == "stopped" and d["stopped_after"] == "link"
    # twin: a failed receipt is still refused (15), and a stopped one still is not packed
    rc, _, err = run(capsys, "kit", "pack", str(r), "--import")
    assert rc == ExitCode.REFUSED and "stopped after link" in err
    f = kf.passed_build(bdir / "f", state="failed", gates=[
        {"gate": "drc_hdpr_link", "verdict": "FAIL", "detail": "HDPR-18"}])
    rc, _, err = run(capsys, "kit", "check", str(f))
    assert rc == ExitCode.REFUSED and "drc_hdpr_link" in err


def test_a_stopped_receipt_for_another_static_is_still_refused(bdir, capsys):
    r = stopped(bdir, "synth")
    rc, _, err = run(capsys, "kit", "check", str(r), "--static-id", "0x44EE76D5")
    assert rc == ExitCode.INCOMPATIBLE and "expected_static" in err
    rc, out, _ = run(capsys, "kit", "check", str(r), "--static-id", "0x72BB0A36")   # twin
    assert rc == 0 and "stopped after synth" in out


def test_kit_guide_offers_the_command_that_finishes_a_stopped_build(tmp_path, capsys):
    assert run(capsys, "kit", "import", str(kf.FIXTURE))[0] == 0
    dfile = design(tmp_path)
    d = tmp_path / "b"
    assert run(capsys, "kit", "script", "--static-id", "0x72BB0A36", "--design", str(dfile),
               "--out", str(d), "--stop-after", "link")[0] == 0       # the script stops at link
    stopped(d, "link")
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", "0x72BB0A36", "--design",
                     str(dfile), "--build-dir", str(d))
    assert rc == 0 and "NEXT   5 Build" in out and "FAILED" not in out
    assert "spike_rm stopped after link (STOP_AFTER=link), not a failure" in out
    nxt = next(line for line in out.splitlines() if line.startswith("next: "))
    assert nxt.endswith("-tclargs STOP_AFTER=bitstream")        # runs it to the end
    # twin: a failed receipt reads as failed, with the plain command
    kf.passed_build(d, state="failed", gates=[
        {"gate": "drc_hdpr_link", "verdict": "FAIL", "detail": "HDPR-18"}])
    rc, out, _ = run(capsys, "kit", "guide", "--static-id", "0x72BB0A36", "--design",
                     str(dfile), "--build-dir", str(d))
    assert rc == 0 and "FAILED 5 Build" in out and "STOP_AFTER=bitstream" not in out
