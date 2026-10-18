"""PINS-MINT4 (HM 1.2.1): the model describes the v2.2 / mint-4 static 0xA5856D30 (54 ports /
260 bits, NGPIO 48, the 16-bit ``hostio`` group) as the default shell, and every earlier
shell keeps its own 47 / 148 boundary. Reads the committed pin model; no platform repo."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import pytest

from harness_manager.cli import cmd_xdc
from harness_manager.cli.context import Ctx
from harness_manager.cli.output import report_error
from harness_manager.core.errors import HarnessError
from harness_manager.services.xdc.model import PinModel
from harness_manager_mps3 import pins

V22, V21_BARE, RC2 = "0xA5856D30", "0x72BB0A36", "0x44EE76D5"


@pytest.fixture(autouse=True)
def keep_tsv_columns(monkeypatch):
    from harness_manager.cli import output

    monkeypatch.setattr(output, "TSV_COLUMNS", dict(output.TSV_COLUMNS))
    monkeypatch.setattr(cmd_xdc, "TSV_COLUMNS", output.TSV_COLUMNS)


@pytest.fixture(scope="module")
def model() -> PinModel:
    return PinModel(pins.load_model(), pack="mps3")


def run(argv, capsys):
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


def test_v22_is_the_default_shell_with_54_ports_260_bits(model):
    assert model.default_shell == V22
    b = model.shell(V22)["rp_boundary"]
    assert b["totals"] == {"ports": 54, "bits": 260, "decoupler_intfs": 24}
    assert b["ngpio"] == 48
    sigs = model.boundary(V22)
    assert len(sigs) == 54 and sum(s.width for s in sigs) == 260
    assert model.boundary_groups(V22)[-1] == "hostio"
    hostio = [s for s in sigs if s.group == "hostio"]
    assert len(hostio) == 7 and sum(s.width for s in hostio) == 16
    assert {s.name: s.width for s in sigs if s.name.startswith("dut_gpio")} == {
        "dut_gpio_o": 48, "dut_gpio_oe": 48, "dut_gpio_i": 48}


def test_earlier_shells_keep_47_ports_148_bits(model):
    for sid in (V21_BARE, RC2):
        sigs = model.boundary(sid)
        assert len(sigs) == 47 and sum(s.width for s in sigs) == 148, sid
        assert model.shell(sid)["rp_boundary"]["ngpio"] == 16
        assert "hostio" not in model.boundary_groups(sid)


def test_every_v22_boundary_signal_has_connectivity(model):
    sh = model.shell(V22)
    names = {c["signal"] for c in sh["connectivity"]}
    assert {s.name for s in model.boundary(V22)} <= names
    assert sh["pblock"]["capacity"]["value"]["LUT"] == 214080 and "slice_range" not in sh["pblock"]


def test_rm_kit_for_v22_is_the_54_port_boundary(tmp_path: Path, capsys):
    code, out, _ = run(["xdc", "rm-kit", "--static-id", V22, "--out", str(tmp_path)], capsys)
    assert code == 0, out
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "manifest.json", "minimal_connectivity.csv", "minimal_connectivity.md",
        "minimal_ooc.xdc", "minimal_pblock.md", "minimal_wrapper_skeleton.sv"]
    assert "54 ports, 260 bits, 24 decoupler interfaces" in (tmp_path / "minimal_connectivity.md").read_text()
    wrap = (tmp_path / "minimal_wrapper_skeleton.sv").read_text()
    assert "NGPIO = 48" in wrap and "hostio_mode_i" in wrap and "hostio_iodata_oe_o" in wrap
    rows = list(csv.reader((tmp_path / "minimal_connectivity.csv").read_text().splitlines()))
    assert sum(1 for r in rows if any(c.startswith("hostio_") for c in r)) >= 7
    assert json.loads((tmp_path / "manifest.json").read_text())["facts"]["static_id"] == V22


def test_rm_kit_without_a_static_defaults_to_v22(tmp_path: Path, capsys):
    a, b = tmp_path / "default", tmp_path / "explicit"
    assert run(["xdc", "rm-kit", "--out", str(a)], capsys)[0] == 0
    assert run(["xdc", "rm-kit", "--static-id", V22, "--out", str(b)], capsys)[0] == 0
    assert {p.name: p.read_bytes() for p in a.iterdir()} == {p.name: p.read_bytes() for p in b.iterdir()}
    assert json.loads((a / "manifest.json").read_text())["facts"]["static_id"] == V22


@pytest.mark.parametrize("sid", [V21_BARE, RC2])
def test_rm_kit_for_an_older_static_keeps_its_own_counts(sid, tmp_path: Path, capsys):
    code, out, _ = run(["xdc", "rm-kit", "--static-id", sid, "--out", str(tmp_path)], capsys)
    assert code == 0, out
    assert "47 ports, 148 bits, 20 decoupler interfaces" in (tmp_path / "minimal_connectivity.md").read_text()
    wrap = (tmp_path / "minimal_wrapper_skeleton.sv").read_text()
    assert "NGPIO = 16" in wrap and "hostio" not in wrap
