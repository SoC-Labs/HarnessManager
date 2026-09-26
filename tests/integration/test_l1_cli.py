"""L1: ``harness-manager lease`` and ``harness-manager share`` against the fake hub. Each check has a twin."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode
from tests.fakes.l1_rig import BOARD_IP, HUB, MCC_TTY, lab
from tests.fakes.virtual_board import VirtualMps3


@contextmanager
def hub_verbs() -> Iterator[None]:
    """``cli.main`` with ``lease``/``share`` registered, as the lead will wire them (CCR L1-2)."""
    from harness_manager.cli import main as cli_main
    from harness_manager.cli.cmd_hub import register

    original = cli_main.make_parser

    def make_parser() -> argparse.ArgumentParser:
        parser = original()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if "lease" not in sub.choices:
            register(sub)
        return parser

    cli_main.make_parser = make_parser
    try:
        yield
    finally:
        cli_main.make_parser = original


def run(capsys, *argv: str) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    with hub_verbs():
        rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def rig(tmp_path, monkeypatch):
    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd) as r:
        yield r


def test_lease_acquire_show_release(capsys, rig):
    rc, out, _ = run(capsys, "--json", "lease", "show", BOARD_IP)
    assert rc == ExitCode.OK and json.loads(out)["lease"] is None and json.loads(out)["hub"] == HUB
    rc, out, err = run(capsys, "--json", "lease", "acquire", BOARD_IP, "--ttl", "900",
                       "--holder", "david-b0")
    assert rc == ExitCode.OK, err
    lease = json.loads(out)["lease"]
    # The secret itself never leaves (the fake hub's tokens are "tok-NNNN-..."), under any key.
    assert lease["holder"] == "david-b0" and lease["mine"] and "tok-" not in out + err
    assert rig.hub.current["ttl"] == 900
    rc, out, _ = run(capsys, "lease", "show", BOARD_IP)
    assert rc == ExitCode.OK and "held by david-b0" in out and "yours" in out
    rc, out, _ = run(capsys, "--tsv", "lease", "show", BOARD_IP)
    assert out.split("\t")[:4] == ["mps3_01_pl", HUB, "held", "david-b0"]
    rc, out, _ = run(capsys, "lease", "release", BOARD_IP)
    assert rc == ExitCode.OK and "released" in out and rig.hub.current is None


def test_negative_twin_release_without_our_lease_is_absent(capsys, rig):
    rig.hub.steal("b0-linux")
    rc, _, err = run(capsys, "lease", "release", BOARD_IP)
    assert rc == ExitCode.ABSENT and "held by b0-linux" in err
    assert rig.hub.current["holder"] == "b0-linux"                   # untouched


def test_a_queued_acquire_says_so_on_stderr(capsys, rig, monkeypatch):
    from harness_manager.services import lease as leasemod

    rig.hub.queue_first = 1
    monkeypatch.setattr(leasemod, "POLL_S", 0.01)
    rc, out, err = run(capsys, "lease", "acquire", BOARD_IP, "--holder", "david-b0")
    assert rc == ExitCode.OK and "queued at position 1" in err and "held by david-b0" in out


LANE2 = "/dev/mps3_01_pl/tty_02"
SHARES_TOML = (f'[boards.lab]\nmatch = ["{BOARD_IP}"]\nvia = "ssh:{HUB}"\n'
               f'hub = {{ host = "{HUB}", target = "mps3_01_pl", shares = {{ mcc = "{MCC_TTY}", '
               f'fpga_uart2 = "{LANE2}" }} }}\n')


def test_share_list_and_start(capsys, tmp_path, monkeypatch):
    from tests.fakes.l1_fake_hub import FakeLane

    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd, toml=SHARES_TOML) as rig:
        rig.hub.add_tty(LANE2, FakeLane(), share=True)
        rc, out, _ = run(capsys, "--json", "share", "list", BOARD_IP)
        shares = json.loads(out)["shares"]
        assert rc == ExitCode.OK and [s["tty"] for s in shares] == [LANE2]
        rc, out, _ = run(capsys, "--json", "share", "start", BOARD_IP, "fpga_uart2")
        assert rc == ExitCode.OK and json.loads(out)["shares"][0]["port"] == shares[0]["port"]
        rc, _, err = run(capsys, "share", "start", BOARD_IP, "fpga_uart3")     # not configured
        assert rc == ExitCode.ABSENT and "fpga_uart2" in err


def test_negative_twin_share_start_refuses_the_mcc_by_name_or_path(capsys, tmp_path, monkeypatch):
    # MCC-FIX: Harness Manager never starts an fpgahub share on tty_00, even when boards.toml
    # still names one (`mcc`), and even by its /dev path. The hub is never asked.
    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd, toml=SHARES_TOML) as rig:
        for name in ("mcc", MCC_TTY):
            rc, _, err = run(capsys, "share", "start", BOARD_IP, name)
            assert rc == ExitCode.REFUSED and "never starts or uses an fpgahub share" in err
        assert rig.tool.share_starts() == [] and MCC_TTY not in rig.hub.shares


def test_negative_twin_there_is_no_share_stop(capsys, rig):
    rc, _, _ = run(capsys, "share", "stop", BOARD_IP)
    assert rc == ExitCode.USAGE and rig.hub.share_stops == []


def test_a_board_without_a_hub_table_says_how_to_add_one(capsys, tmp_path, monkeypatch):
    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd, toml=""):
        rc, _, err = run(capsys, "lease", "show", BOARD_IP)
    assert rc == ExitCode.ABSENT and "not behind a hub" in err and "boards.toml" in err
