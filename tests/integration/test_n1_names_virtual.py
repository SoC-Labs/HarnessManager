"""N1: the lab board shows its name ("mps3-01") in probe, info and --json, end to end.

The rig is lane L1's lab (tests/fakes/l1_rig.py): boards.toml routes the board
through the fake hub's SSH tunnel, and the fake hub answers ``fpgahub board list
--json`` the way fpgahub 0.3.0 does. Each check has a negative twin.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.core.pack import ProbeHints
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3 import naming as mnaming
from tests.fakes.l1_rig import BOARD_IP, LAB_TOML, lab, write_boards_toml
from tests.fakes.virtual_board import FIELDED_3F1A560F, VirtualMps3


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture(autouse=True)
def _fresh_cache():
    mnaming.clear_cache()
    yield
    mnaming.clear_cache()


@pytest.fixture
def engine():
    eng = Engine(EngineConfig(state_dir=state_dir()))
    yield eng
    eng.close_all()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_the_lab_board_is_mps3_01_before_and_after_it_is_opened(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        found = engine.probe(ProbeHints(hosts=(BOARD_IP,), scan_usb=False, scan_network=False))
        assert [(c.name, c.name_source) for c in found] == [("mps3-01", "hub-target")]
        assert ["fpgahub", "board", "list", "--json"] not in rig.hub.calls   # probing never asks

        engine.open(found[0], note="n1")
        info = engine.info(found[0].board_id)
        assert (info.candidate.name, info.candidate.name_source) == ("mps3-01", "hub")
        assert engine.session(found[0].board_id).candidate.name_source == "hub"
        engine.info(found[0].board_id)
        assert rig.hub.calls.count(["fpgahub", "board", "list", "--json"]) == 1   # asked once


def test_negative_twin_a_board_with_no_hub_and_no_name_is_its_address(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        write_boards_toml(state_dir(), '[boards.lab]\nmatch = ["192.168.10.199"]\n')
        cand = engine.candidate_for(vb.shell_endpoint)
        engine.open(cand, note="n1")
        info = engine.info(cand.board_id)
        assert info.candidate.name == "" and info.candidate.name_source == ""


def test_boards_toml_name_wins_over_the_hub(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=LAB_TOML + 'name = "bench-a"\n'):
        cand = engine.candidate_for(BOARD_IP)
        assert (cand.name, cand.name_source) == ("bench-a", "config")
        engine.open(cand, note="n1")
        assert engine.info(cand.board_id).candidate.name == "bench-a"


def test_a_harness_that_reports_a_name_outranks_the_hub(tmp_path, monkeypatch, engine):
    profile = replace(FIELDED_3F1A560F, version_extra={"name": "unit-7"})
    with VirtualMps3(tmp_path, profile) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        found = engine.probe(ProbeHints(hosts=(BOARD_IP,), scan_usb=False, scan_network=False))
        assert (found[0].name, found[0].name_source) == ("unit-7", "harness")
        assert found[0].identity is not None and found[0].identity.name == "unit-7"
        engine.open(found[0], note="n1")
        info = engine.info(found[0].board_id)
        assert (info.candidate.name, info.candidate.name_source) == ("unit-7", "harness")
        assert info.identity.name == "unit-7"


def test_cli_info_and_probe_show_the_name(tmp_path, monkeypatch, capsys):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        rc, out, err = run(capsys, "info", BOARD_IP)
        assert rc == ExitCode.OK, err
        assert out.splitlines()[0] == "name       mps3-01 (from the hub)"
        rc, out, err = run(capsys, "--json", "info", BOARD_IP)
        assert rc == ExitCode.OK, err
        cand = json.loads(out)["candidate"]
        assert (cand["name"], cand["name_source"]) == ("mps3-01", "hub")
        assert json.loads(out)["identity"]["name"] == ""          # the harness says nothing yet
        rc, out, err = run(capsys, "--tsv", "info", BOARD_IP)
        assert TSV_COLUMNS["info"][-1] == "NAME" and out.rstrip("\n").split("\t")[-1] == "mps3-01"
        rc, out, err = run(capsys, "probe", "--host", BOARD_IP, "--no-scan")
        assert rc == ExitCode.OK, err
        assert out.startswith(f"mps3-01\tmps3@{BOARD_IP}:6900\t")


def test_negative_twin_cli_info_without_a_name_has_no_name_line(tmp_path, monkeypatch, capsys):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        write_boards_toml(state_dir(), '[boards.lab]\nmatch = ["192.168.10.199"]\n')
        rc, out, err = run(capsys, "info", vb.shell_endpoint)
        assert rc == ExitCode.OK, err
        assert out.splitlines()[0].startswith("board      ") and "name       " not in out
        rc, out, _ = run(capsys, "--json", "info", vb.shell_endpoint)
        assert json.loads(out)["candidate"]["name"] == ""
