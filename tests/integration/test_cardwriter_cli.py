"""Lane SD-FLASH: ``harness-manager flash devices|write`` (cli/cmd_flash.py), in-process over the
rig's ``CardWriter`` (fixture lsblk, temp-file devices). Each behaviour has its negative twin;
nothing touches a block device (``guard``).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from harness_manager.cli import cmd_flash
from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode
from tests.fakes.cardwriter_fakes import (
    BUNDLE_BOARD,
    CARD_BOARD,
    NO_LINE_BOARD,
    Rig,
    guard,  # noqa: F401 - the fixture
    phrase,
)

pytestmark = pytest.mark.usefixtures("guard")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    r = Rig(tmp_path)
    monkeypatch.setattr(cmd_flash, "WRITER", r.writer)
    return r


def run(capsys, *argv: str, stdin: str | None = None,
        monkeypatch: pytest.MonkeyPatch | None = None) -> tuple[int, str, str]:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def sdc(rig: Rig):
    return rig.card("sdc")


def test_devices_off_says_why_and_how_to_turn_it_on(rig, capsys):
    rig.enabled = False
    rc, out, _ = run(capsys, "flash", "devices")
    assert rc == 0 and out.splitlines() == [
        "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)",
        "turn it on: harness-manager config set bringup.sd_flash on"]
    rc, out, _ = run(capsys, "--json", "flash", "devices")
    assert json.loads(out)["enabled"] is False and rig.listed == 0


def test_twin_devices_on_lists_the_cards_and_their_phrases(rig, capsys):
    rc, out, _ = run(capsys, "flash", "devices")
    assert rc == 0
    assert "type   WRITE MicroSD/M2 15.9 GB" in out and "type   WRITE SD/MMC 31.9 GB" in out
    assert "/dev/nvme0n1" not in out
    rc, out, _ = run(capsys, "flash", "devices", "--all")
    assert "not offered:" in out and "/dev/nvme0n1" in out and "the system disk" in out
    rc, out, _ = run(capsys, "--tsv", "flash", "devices")
    rows = [ln.split("\t") for ln in out.splitlines()]
    assert len(rows) == 3 and all(len(r) == len(cmd_flash.DEVICE_COLUMNS) for r in rows)
    row = next(r for r in rows if r[1] == "/dev/sdb")
    assert row[7:9] == ["yes", "no"] and row[9] == "WRITE SD/MMC 31.9 GB"


def test_write_with_the_phrase_given_writes_and_verifies(rig, capsys):
    src = rig.card_image()
    rc, out, err = run(capsys, "--json", "flash", "write", sdc(rig).id, str(src), "--kind",
                       "card", "--confirm", "WRITE MicroSD/M2 15.9 GB", "--confirm-unsigned",
                       phrase(src))
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["ok"] and doc["outcome"] == "written" and doc["verified"] is True
    assert "flash card: verify" in err
    assert rig.devices["/dev/sdc"].read_bytes()[:512] == src.read_bytes()[:512]


def test_twin_a_wrongly_typed_phrase_at_the_prompt_writes_nothing(rig, capsys, monkeypatch):
    before = rig.devices["/dev/sdc"].read_bytes()
    src = rig.card_image()
    rc, _, err = run(capsys, "flash", "write", sdc(rig).id, str(src), "--kind",
                     "card", stdin=f"{phrase(src)}\nWRITE MicroSD 16 GB\n",
                     monkeypatch=monkeypatch)
    assert rc == ExitCode.REFUSED and "type exactly 'WRITE MicroSD/M2 15.9 GB'" in err
    assert "To write it, type exactly: WRITE MicroSD/M2 15.9 GB" in err
    assert rig.devices["/dev/sdc"].read_bytes() == before


def test_the_right_phrase_typed_at_the_prompt_writes(rig, capsys, monkeypatch):
    src = rig.card_image()
    rc, out, err = run(capsys, "flash", "write", sdc(rig).id, str(src), "--kind",
                       "card", stdin=f"{phrase(src)}\nWRITE MicroSD/M2 15.9 GB\n",
                       monkeypatch=monkeypatch)
    assert rc == 0 and out.startswith("written  /dev/sdc: written, read back and verified")
    assert f"To install this unsigned card image, type exactly: {phrase(src)}" in err


def test_yes_never_asks_and_names_the_phrase(rig, capsys):
    src = rig.card_image()
    rc, out, err = run(capsys, "--json", "flash", "write", sdc(rig).id, str(src),
                       "--kind", "card", "--yes", "--confirm-unsigned", phrase(src))
    assert rc == ExitCode.REFUSED
    assert json.loads(out)["error"]["data"]["confirm"] == "WRITE MicroSD/M2 15.9 GB"


def test_a_slot_image_is_refused_before_the_question(rig, capsys):
    rc, _, err = run(capsys, "flash", "write", sdc(rig).id, str(rig.slot()), "--kind", "card",
                     "--yes")
    assert rc == ExitCode.REFUSED and "single OS slot (linux_slot.img)" in err


def test_needs_privilege_prints_the_sudo_commands_and_exits_12(rig, capsys):
    rig.access.writable = False
    src = rig.card_image()
    rc, out, err = run(capsys, "--json", "flash", "write", sdc(rig).id, str(src), "--kind",
                       "card", "--confirm", "WRITE MicroSD/M2 15.9 GB", "--confirm-unsigned",
                       phrase(src))
    assert rc == ExitCode.UNAVAILABLE
    cmd = f"sudo dd if={src} of=/dev/sdc bs=4M conv=fsync status=progress"
    assert f"    {cmd}" in err and "it never asks for root" in err
    data = json.loads(out)["error"]["data"]
    assert data["privileged_command"] == cmd and data["outcome"] == "needs_privilege"
    assert data["verify_command"].startswith(f"sudo cmp -n {src.stat().st_size} ")


def test_files_print_the_mbbios_kept_line(rig, capsys):
    rig.card_board_txt(CARD_BOARD)
    card = rig.card("sdb")
    bundle = rig.bundle(board_txt=BUNDLE_BOARD)
    rc, out, err = run(capsys, "flash", "write", card.id, str(bundle),
                       "--kind", "files", "--confirm", card.confirm, "--confirm-unsigned",
                       phrase(bundle))
    assert rc == 0, err
    assert "MBBIOS kept: mbb_v141.ebf" in err and "MBBIOS kept: mbb_v141.ebf" in out
    assert "backup   " in out and Path(rig.tmp / "state").exists()


def test_twin_files_that_would_update_the_mcc_need_allow_mcc_update(rig, capsys):
    rig.card_board_txt(NO_LINE_BOARD, ebf="mbb_v999.ebf")
    card = rig.card("sdb")
    bundle = rig.bundle(board_txt=BUNDLE_BOARD)
    rc, _, err = run(capsys, "flash", "write", card.id, str(bundle), "--kind", "files",
                     "--confirm", card.confirm)
    assert rc == ExitCode.REFUSED and "or add --allow-mcc-update" in err
    rc, out, err = run(capsys, "flash", "write", card.id, str(bundle), "--kind", "files",
                       "--confirm", card.confirm, "--allow-mcc-update", "--confirm-unsigned",
                       phrase(bundle))
    assert rc == 0 and "allowed by --allow-mcc-update: the card has mbb_v999.ebf" in out


def test_backup_options_are_for_files_only(rig, capsys):
    rc, _, err = run(capsys, "flash", "write", sdc(rig).id, str(rig.card_image()), "--kind",
                     "card", "--backup-dir", "/tmp/x", "--yes")
    assert rc == ExitCode.USAGE and "are for --kind files" in err


def test_help_names_the_verb(capsys):
    rc, out, _ = run(capsys, "help", "flash")
    assert rc == 0 and "devices" in out and "write" in out
    rc, out, _ = run(capsys, "help", "--tabs", "Board controller")
    assert "flash write DEVICE_ID SOURCE --kind files|card" in out


# --- the unsigned phrase: INSTALL UNSIGNED <sha8> as well as WRITE <model> <size> ---------------


def test_a_card_image_needs_both_phrases_and_says_its_sha256(rig, capsys):
    src = rig.card_image()
    sha = cw_sha(src)
    rc, out, err = run(capsys, "--json", "flash", "write", sdc(rig).id, str(src), "--kind",
                       "card", "--confirm", "WRITE MicroSD/M2 15.9 GB", "--yes")
    assert rc == ExitCode.REFUSED
    e = json.loads(out)["error"]
    assert e["message"] == ("not confirmed: --yes never types the phrase; give it with "
                            f"--confirm-unsigned 'INSTALL UNSIGNED {sha[:8]}'")
    assert e["data"]["unsigned"]["of"] == "file"
    assert "Unsigned: Harness Manager cannot check where this came from" in err
    assert f"sha256   {sha}  (the image: sha256sum CARD.img)" in err
    assert rig.devices["/dev/sdc"].read_bytes() == b"\xee" * 4096


def test_twin_a_wrong_sha8_is_refused_and_nothing_is_written(rig, capsys):
    src = rig.card_image()
    wrong = "INSTALL UNSIGNED " + ("0" * 8 if not phrase(src).endswith("0" * 8) else "1" * 8)
    rc, _, err = run(capsys, "flash", "write", sdc(rig).id, str(src), "--kind", "card",
                     "--confirm", "WRITE MicroSD/M2 15.9 GB", "--confirm-unsigned", wrong)
    assert rc == ExitCode.REFUSED and "does not name this card image" in err
    assert rig.devices["/dev/sdc"].read_bytes() == b"\xee" * 4096


def test_a_bundle_zip_is_named_by_its_own_sha256_and_written(rig, capsys, tmp_path):
    from tests.unit.test_bringup_service import zip_dir

    card = rig.card("sdb")
    z = zip_dir(rig.bundle(), tmp_path / "b.zip")
    rc, _, err = run(capsys, "flash", "write", card.id, str(z), "--kind", "files",
                     "--confirm", card.confirm, "--yes")
    assert rc == ExitCode.REFUSED and f"'INSTALL UNSIGNED {cw_sha(z)[:8]}'" in err
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "old\n"
    rc, out, err = run(capsys, "flash", "write", card.id, str(z), "--kind", "files",
                       "--confirm", card.confirm, "--confirm-unsigned", phrase(z))
    assert rc == 0, err
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "new harness\n"


def cw_sha(path: Path) -> str:
    from harness_manager.services import cardwriter as cw

    return cw.file_sha256(path)
