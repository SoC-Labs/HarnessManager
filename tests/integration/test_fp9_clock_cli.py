"""FIX-PACK-9 (the coordinator, 2 Oct): ``harness-manager clock <ip>`` on a Linux board said
"dut  unavailable: the shell cannot read the DUT clock back; set it to know it". The CLI's
candidate is only the address (no identity), so the MPS3 clock adapter read the board as bare
metal. It now asks the session (the shell's ``version``), and the Linux board's DUT clock is
50 MHz, from the board's stats (LINUX-CLOCK, HM 1.2: the Linux harness answers set_clk and
reports dut_mhz), in human, ``--json`` and ``--tsv``. Twin: a bare-metal board is unchanged.
"""

from __future__ import annotations

import json

import pytest

from harness_manager.cli import main as climain
from harness_manager.cli.engine import set_engine_factory
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.virtual_board import VirtualMps3, ila_v011_profile, linux_harnessd_profile

RC2 = 0x44EE76D5
WHY = "set with presets 25, 50 or 100 MHz; goes back to 50 MHz when the harness restarts"
SRC = "board stats (clk_wiz_dut read-back)"


@pytest.fixture(params=["linux", "bare-metal"])
def board(request, tmp_path):
    profile = (linux_harnessd_profile(RC2) if request.param == "linux"
               else ila_v011_profile())
    with VirtualMps3(tmp_path / "board", profile) as vb:
        state = tmp_path / "state"
        previous = set_engine_factory(lambda _a: Engine(
            EngineConfig(state_dir=state), packs={"mps3": Mps3Pack(console_ports=vb.console_ports)}))
        try:
            yield request.param, vb
        finally:
            set_engine_factory(previous)


def run(capsys, *argv: str) -> tuple[int, str]:
    rc = climain.main(list(argv))
    return rc, capsys.readouterr()[0]


def test_clock_on_a_linux_board_says_50_mhz_from_the_board_stats(board, capsys):
    impl, vb = board
    rc, human = run(capsys, "clock", vb.shell_endpoint)
    assert rc == 0, human
    rc, js = run(capsys, "--json", "clock", vb.shell_endpoint)
    (r,) = json.loads(js)["readings"]
    rc, tsv = run(capsys, "--tsv", "clock", vb.shell_endpoint)
    row = tsv.strip().splitlines()[-1].split("\t")
    if impl == "linux":
        assert human.strip() == f"dut              50 MHz  [{SRC}]  ({WHY})"
        assert (r["value"], r["unit"], r["source"], r["reason"], r["available"]) == (
            50.0, "MHz", SRC, WHY, True)
        assert row[1:5] == ["dut", "50", "MHz", SRC] and row[-1] == WHY
        assert "cannot read" not in human + js + tsv
    else:                                                   # the twin: bare metal unchanged
        assert human.strip() == ("dut              unavailable: the shell cannot read the DUT "
                                 "clock back; set it to know it")
        assert r["available"] is False and r["source"] == "shell set_clk"


def test_setting_it_works_on_a_linux_board_and_on_bare_metal(board, capsys):
    impl, vb = board
    rc, out = run(capsys, "--json", "clock", vb.shell_endpoint, "--dut-mhz", "25")
    assert rc == 0, out
    (r,) = json.loads(out)["readings"]
    assert r["value"] == 25.0
    assert r["source"] == (SRC if impl == "linux" else "shell set_clk (last set value)")
    rc, out = run(capsys, "--json", "clock", vb.shell_endpoint, "--dut-mhz", "33")   # the twin
    assert rc != 0 and "not a preset" in json.loads(out)["error"]["message"]
    if impl == "linux":                                     # read back from the board's stats
        rc, out = run(capsys, "--json", "clock", vb.shell_endpoint)
        assert json.loads(out)["readings"][0]["value"] == 25.0
