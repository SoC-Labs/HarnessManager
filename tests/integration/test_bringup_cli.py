"""BRINGUP-USB's command line (cli/cmd_bringup.py): the wizard's plan, end to end, over the real
engine and MPS3 pack on a virtual board's Debug USB (the FakeMcc on ``fake://``, the
FakeSdVolume as its drive), with its negative twins. The verb is built here with
``register`` (its wiring into cli/main.py is CCR BRINGUP-3)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.cli import cmd_bringup
from harness_manager.cli.context import Ctx
from harness_manager.cli.main import _fmt_parent, _usb_parent
from harness_manager.core.errors import RefusedError, UnavailableError
from harness_manager_mps3 import mcc as mccmod
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t13_daemon import engine_for
from tests.fakes.virtual_board import VirtualMps3
from tests.unit.test_bringup_service import release_bundle, sd_tree


@pytest.fixture
def board(tmp_path: Path, monkeypatch) -> Iterator[VirtualMps3]:
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_OVERLAY_DIRS", raising=False)
    monkeypatch.delenv("HARNESS_MANAGER_BRINGUP_SD_FLASH", raising=False)
    with VirtualMps3(tmp_path / "usb", usb=True) as vb:
        vb.mcc.clock = clock
        vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        counted = vb.mcc.on_reboot
        vb.mcc.on_reboot = lambda: (counted(), vb.shell.stop())
        vb.mcc.on_boot = vb.shell.start
        yield vb


def run(board: VirtualMps3, tmp_path: Path, capsys, *extra: str) -> tuple[int, dict]:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    cmd_bringup.register(sub, parents=(_fmt_parent(), _usb_parent()))
    args = p.parse_args(["bringup", "-", "--serial", board.mcc_url, "--volume",
                         str(board.sd.root), "--host", board.shell_endpoint, "--yes",
                         "--backup-dir", str(tmp_path / "bk"), *extra])
    eng = engine_for(board)
    try:
        rc = args.fn(Ctx(args, eng, "json"))
    finally:
        eng.close_all()
    return rc, json.loads(capsys.readouterr().out)


def test_bringup_backs_up_writes_reboots_and_witnesses_a_bundle(board, tmp_path, capsys):
    bundle = release_bundle(tmp_path / "rel", linux=False)
    ebf = board.sd.ebf.read_bytes()
    rc, out = run(board, tmp_path, capsys, "--bundle", str(bundle), "--wait", "30")
    assert rc == 0
    steps = [(s["step"], s["result"]) for s in out["steps"]]
    assert steps == [("source", "checked"), ("backup", "taken"), ("write", "written"),
                     ("overlays", "added"), ("reboot", "witnessed"), ("witness", "running"),
                     ("next", "access")]
    assert (board.sd.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes() == \
        (bundle / "sd/MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes()
    assert board.sd.ebf.read_bytes() == ebf and board.reboots == 1
    assert Path(out["backup"]["path"]).parent == tmp_path / "bk"


def test_twin_a_refused_bundle_writes_and_reboots_nothing(board, tmp_path, capsys):
    bad = sd_tree(tmp_path / "bad")
    (bad / "MB" / "HBI0309C" / "mbb_v141.ebf").write_bytes(b"MB BIOS")
    before = board.sd.snapshot()
    with pytest.raises(RefusedError) as e:
        run(board, tmp_path, capsys, "--bundle", str(bad))
    assert ".ebf" in e.value.message and e.value.data["check"]["refused"] is True
    assert board.sd.snapshot() == before and board.reboots == 0


def test_twin_the_card_reader_door_says_why_it_cannot_be_used(board, tmp_path, capsys,
                                                             monkeypatch):
    bundle = sd_tree(tmp_path / "good")
    with pytest.raises(UnavailableError) as e:
        run(board, tmp_path, capsys, "--bundle", str(bundle), "--card-reader", "usb-x")
    assert "bringup.sd_flash" in e.value.reason
    monkeypatch.setenv("HARNESS_MANAGER_BRINGUP_SD_FLASH", "on")
    with pytest.raises(UnavailableError) as e:
        run(board, tmp_path, capsys, "--bundle", str(bundle), "--card-reader", "usb-x")
    assert "flash write DEVICE_ID BUNDLE --kind files" in e.value.reason and board.reboots == 0


def test_twin_a_board_that_stays_dark_names_the_backup_to_restore(board, tmp_path, capsys):
    from harness_manager.core.errors import ActionFailedError

    board.mcc.on_boot = lambda: None                   # the FPGA loads, the harness never answers
    with pytest.raises(ActionFailedError) as e:
        run(board, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")), "--wait", "1")
    assert e.value.data["timeout"] is True and "restore" in e.value.hint
    assert str(tmp_path / "bk") in e.value.hint
