"""BRINGUP-USB's command line (cli/cmd_bringup.py): the wizard's plan, end to end, over the real
engine and MPS3 pack on a virtual board's Debug USB (the FakeMcc on ``fake://``, the
FakeSdVolume as its drive), with its negative twins. The verb is built here with
``register`` (its wiring into cli/main.py is CCR BRINGUP-3)."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.cli import cmd_bringup, cmd_flash
from harness_manager.cli.context import Ctx
from harness_manager.cli.main import _fmt_parent, _usb_parent
from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager_mps3 import mcc as mccmod
from tests.fakes.cardwriter_fakes import (
    CARD_BOARD,
    NO_LINE_BOARD,
    Rig,
    guard,  # noqa: F401 - the fixture
)
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


@pytest.mark.usefixtures("guard")
def test_twin_the_card_reader_door_says_why_it_cannot_be_used(board, tmp_path, capsys):
    bundle = sd_tree(tmp_path / "good")                  # bringup.sd_flash off (the default)
    with pytest.raises(UnavailableError) as e:
        run(board, tmp_path, capsys, "--bundle", str(bundle), "--card-reader", "usb-x")
    assert "bringup.sd_flash" in e.value.reason and "config set bringup.sd_flash on" in \
        e.value.hint
    assert board.reboots == 0


# --- --card-reader: the card writer's `files` kind, no MCC reboot --------------------------------


@pytest.fixture
def rig(tmp_path: Path, monkeypatch, guard) -> Rig:  # noqa: F811 - guard is the fixture
    r = Rig(tmp_path / "reader")
    monkeypatch.setattr(cmd_flash, "WRITER", r.writer)
    return r


def run_reader(board: VirtualMps3, rig: Rig, tmp_path: Path, capsys, *extra: str,
               yes: bool = True) -> tuple[int, dict]:
    """``bringup - --bundle B --card-reader ID``: no --serial, no --volume (the card is out of
    the board, in the rig's reader as sdb, mounted at rig.root)."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    cmd_bringup.register(sub, parents=(_fmt_parent(), _usb_parent()))
    args = p.parse_args(["bringup", "-", "--card-reader", rig.card("sdb").id, "--host",
                         board.shell_endpoint, "--backup-dir", str(tmp_path / "bk"),
                         *(["--yes"] if yes else []), *extra])
    eng = engine_for(board)
    try:
        rc = args.fn(Ctx(args, eng, "json"))
    finally:
        eng.close_all()
    return rc, json.loads(capsys.readouterr().out)


PHRASE = "WRITE SD/MMC 31.9 GB"


def test_card_reader_writes_the_card_backs_it_up_and_witnesses_with_no_mcc_reboot(
        board, rig, tmp_path, capsys):
    bundle = release_bundle(tmp_path / "rel", linux=False)
    rc, out = run_reader(board, rig, tmp_path, capsys, "--bundle", str(bundle),
                         "--confirm", PHRASE)
    assert rc == 0
    steps = [(s["step"], s["result"]) for s in out["steps"]]
    assert steps == [("source", "checked"), ("backup", "taken"), ("write", "written"),
                     ("mbbios", "bundle"), ("overlays", "added"), ("reboot", "by-hand"),
                     ("witness", "running"), ("next", "access")]
    by = {s["step"]: s["detail"] for s in out["steps"]}
    assert by["reboot"] == ("no MCC reboot with the card reader: put the card back in the "
                            "board's configuration SD slot and power the board on")
    assert "/dev/sdb" in by["write"] and "read back" in by["write"]
    assert (rig.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes() == \
        (bundle / "sd/MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes()
    assert board.reboots == 0                            # never through the MCC
    assert Path(out["backup"]["path"]).parent == tmp_path / "bk"
    assert out["write"]["verified"] is True and out["card_reader"]["kind"] == "files"


def test_twin_a_wrong_phrase_writes_nothing(board, rig, tmp_path, capsys):
    before = sorted(p.relative_to(rig.root).as_posix() for p in rig.root.rglob("*"))
    with pytest.raises(RefusedError) as e:
        run_reader(board, rig, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")),
                   "--confirm", "WRITE SD/MMC 32 GB")
    assert "type exactly 'WRITE SD/MMC 31.9 GB'" in e.value.message
    assert sorted(p.relative_to(rig.root).as_posix() for p in rig.root.rglob("*")) == before
    assert not (tmp_path / "bk").exists() and board.reboots == 0


def test_twin_yes_never_types_the_phrase(board, rig, tmp_path, capsys):
    with pytest.raises(RefusedError) as e:
        run_reader(board, rig, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")))
    assert "--yes never types the phrase" in e.value.message
    assert e.value.data["confirm"] == PHRASE
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "old\n"


def test_card_reader_keeps_the_cards_mbbios_line(board, rig, tmp_path, capsys):
    rig.card_board_txt(CARD_BOARD)
    rc, out = run_reader(board, rig, tmp_path, capsys, "--bundle",
                         str(sd_tree(tmp_path / "good")), "--confirm", PHRASE)
    assert rc == 0
    mb = [s for s in out["steps"] if s["step"] == "mbbios"]
    assert mb == [{"step": "mbbios", "result": "kept", "detail": "MBBIOS kept: mbb_v141.ebf"}]
    got = (rig.root / "MB" / "HBI0309C" / "board.txt").read_bytes()
    assert b"MBBIOS: mbb_v141.ebf           ;MB BIOS image" in got and b";stock" not in got


def test_twin_a_card_that_would_update_the_mcc_is_refused_before_the_phrase(
        board, rig, tmp_path, capsys):
    rig.card_board_txt(NO_LINE_BOARD, ebf="mbb_v141.ebf")
    with pytest.raises(RefusedError) as e:
        run_reader(board, rig, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")),
                   "--confirm", PHRASE)
    assert "would make the MCC update itself to mbb_v141.ebf" in e.value.message
    assert (rig.root / "MB" / "HBI0309C" / "board.txt").read_bytes() == NO_LINE_BOARD
    assert not (tmp_path / "bk").exists()


def test_card_reader_asks_for_the_phrase_and_whether_the_card_is_back(
        board, rig, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{PHRASE}\nn\n"))
    with pytest.raises(RefusedError) as e:
        run_reader(board, rig, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")),
                   yes=False)
    assert e.value.message == "the card is written; nothing waited for the harness"
    assert f"harness-manager probe --host {board.shell_endpoint} --no-scan" in e.value.hint
    assert [s[1] for s in e.value.data["steps"]][-1] == "reboot"
    assert (rig.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").is_file()   # written all the same


def test_twin_card_reader_refuses_a_signed_release_and_confirm_needs_the_reader(
        board, rig, tmp_path, capsys):
    with pytest.raises(UsageError, match="a signed release goes through the Debug USB"):
        run_reader(board, rig, tmp_path, capsys, "--version", "1.1.0")
    with pytest.raises(UsageError, match="--confirm is the card reader's typed phrase"):
        run(board, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")),
            "--confirm", PHRASE)
    assert board.reboots == 0


def test_twin_a_board_that_stays_dark_names_the_backup_to_restore(board, tmp_path, capsys):
    from harness_manager.core.errors import ActionFailedError

    board.mcc.on_boot = lambda: None                   # the FPGA loads, the harness never answers
    with pytest.raises(ActionFailedError) as e:
        run(board, tmp_path, capsys, "--bundle", str(sd_tree(tmp_path / "good")), "--wait", "1")
    assert e.value.data["timeout"] is True and "restore" in e.value.hint
    assert str(tmp_path / "bk") in e.value.hint
