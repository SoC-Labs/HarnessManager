"""T10: ``harness-manager xdc`` through ``cmd_xdc.register`` (main.py wiring is CCR T10-1).

The parser here is built the way ``cli/main.py`` builds it (a top-level ``--pack`` and
``--json``/``--tsv``, then ``register(sub)``); the verb needs no engine.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from harness_manager.cli import cmd_xdc
from harness_manager.cli.context import Ctx
from harness_manager.cli.output import report_error
from harness_manager.core.errors import ExitCode, HarnessError


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    p = argparse.ArgumentParser(prog="harness-manager")
    p.add_argument("--pack", default=None)
    p.add_argument("--json", action="store_true", default=False)
    p.add_argument("--tsv", action="store_true", default=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    cmd_xdc.register(sub)
    args = p.parse_args(argv)
    fmt = "json" if args.json else "tsv" if args.tsv else "human"
    try:
        code = int(args.fn(Ctx(args, None, fmt)))
    except HarnessError as exc:
        code = report_error(fmt, exc)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_info_names_the_model_the_shell_and_the_sources(capsys):
    code, out, _ = run(["xdc", "info"], capsys)
    assert code == 0
    assert "DERIVED" in out and "0x72BB0A36 (fielded): 47 ports, 148 bits, 20 decoupler" in out
    assert "nanosoc" in out and "fpga/shell/boundary.yaml" in out
    code, out, _ = run(["xdc", "info", "--json"], capsys)
    assert json.loads(out)["model"]["default_shell"] == "0x72BB0A36"


def test_rm_kit_writes_the_files_and_the_manifest(tmp_path: Path, capsys):
    code, out, _ = run(["xdc", "rm-kit", "--design", "nanosoc", "--out", str(tmp_path)], capsys)
    assert code == 0, out
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["manifest.json", "nanosoc_connectivity.csv", "nanosoc_connectivity.md",
                     "nanosoc_ooc.xdc", "nanosoc_pblock.md", "nanosoc_wrapper_skeleton.sv"]
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["ok"] and man["facts"]["static_id"] == "0x72BB0A36"
    assert man["facts"]["model"]["derived"] is True


def test_without_out_it_previews_and_writes_nothing(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(["xdc", "board", "--design", "blinky"], capsys)
    assert code == 0 and "not written; add --out DIR" in out and "blinky_pins.xdc" in out
    assert list(tmp_path.iterdir()) == []
    code, out, _ = run(["xdc", "board", "--json"], capsys)       # the default design
    body = json.loads(out)
    assert body["ok"] and "set_property PACKAGE_PIN AK16" in body["files"]["blinky_pins.xdc"]


def test_a_failed_check_exits_15_and_lists_every_check(tmp_path: Path, capsys):
    design = tmp_path / "bad.json"
    design.write_text(json.dumps({"kind": "board", "name": "bad", "ports": [
        {"port": "sw", "net": "USER_SW[0]", "dir": "out"},
        {"port": "g", "net": "SH0_IO[0]", "dir": "out", "iostandard": "LVCMOS18"}]}))
    out_dir = tmp_path / "out"
    code, out, err = run(["xdc", "board", "--design", str(design), "--out", str(out_dir), "--json"],
                         capsys)
    assert code == ExitCode.REFUSED
    assert "failed 2 checks" in err
    checks = json.loads(out)["error"]["data"]["checks"]
    assert [c["code"] for c in checks] == ["direction", "bank_voltage"]
    assert not out_dir.exists()


def test_tsv_rows_follow_the_registered_columns(capsys):
    code, out, _ = run(["xdc", "rm-kit", "--design", "nanosoc", "--tsv"], capsys)
    assert code == 0
    rows = [line.split("\t") for line in out.splitlines()]
    assert rows and all(len(r) == len(cmd_xdc.XDC_TSV["xdc"]) for r in rows)
    assert ["rm-kit", "nanosoc", "file", "nanosoc_ooc.xdc", "-"] in rows


@pytest.mark.parametrize("argv, code", [
    (["xdc", "rm-kit", "--design", "blinky"], ExitCode.USAGE),
    (["xdc", "board", "--design", "no_such_design"], ExitCode.ABSENT),
    (["xdc", "rm-kit", "--static-id", "0x3F1A560F"], ExitCode.REFUSED),
    (["--pack", "haps", "xdc", "info"], ExitCode.ABSENT),
])
def test_errors_carry_their_exit_codes(argv, code, capsys):
    got, _, err = run(argv, capsys)
    assert got == code, err
