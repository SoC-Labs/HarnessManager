"""QUICKWINS G2: revision, MCC firmware and user microSD, read only, on real-format samples."""
from __future__ import annotations

from pathlib import Path

from harness_manager.services import board_facts as bf
from harness_manager.services import bringup
from harness_manager_mps3 import sd

# What the MCC writes to LOG.TXT: CRLF, rewritten at each boot.
LOG = ("ARM V2M-MPS3 Firmware v1.3.2\r\nBuild Date: Apr 20 2018\r\n"
       "MotherBoard Revision C Variant A\r\nSetting up oscillators...\r\n")
LOG_B = LOG.replace("v1.3.2", "v1.4.1").replace("Revision C", "Revision B")


def test_firmware_from_a_crlf_log_and_the_last_line_wins():
    assert sd.firmware_of_log(LOG) == "v1.3.2"
    assert sd.firmware_of_log(LOG + LOG_B) == "v1.4.1"
    assert sd.firmware_of_log("  ARM V2M-MPS3 Firmware 1.3.2  \r\n") == "v1.3.2"
    assert sd.revision_of_log(LOG) == "HBI0309C"


def test_negative_twin_no_firmware_line_is_no_answer():
    assert sd.firmware_of_log("") == ""
    assert sd.firmware_of_log("ARM V2M-MPS3 Bootloader v1.0.0\r\nFirmware: v9\r\n") == ""
    assert sd.firmware_of_log("not ARM V2M-MPS3 Firmware v1.3.2 at the start") == ""


def card(tmp_path: Path, text: str | None, mb: tuple[str, ...] = ("HBI0309C",)) -> Path:
    for rev in mb:
        (tmp_path / "MB" / rev).mkdir(parents=True)
    if text is not None:
        (tmp_path / "LOG.txt").write_bytes(text.encode())     # the name's case is the card's
    return tmp_path


def test_mcc_firmware_of_a_card_prefers_the_boot_witness(tmp_path: Path):
    root = card(tmp_path, LOG)
    assert sd.mcc_firmware_of(root) == ("v1.3.2", "LOG.txt on its config SD")
    assert sd.mcc_firmware_of(root, boot_fw="v1.4.1") == ("v1.4.1", "the MCC boot log")
    assert sd.mcc_firmware_of(root.parent / "none") == ("", "")


def test_twin_a_card_without_a_log_says_nothing(tmp_path: Path):
    assert sd.mcc_firmware_of(card(tmp_path, None)) == ("", "")


def test_tested_values_and_the_untested_flags():
    ok = bf.build("HBI0309C", "LOG.TXT", "v1.3.2", "LOG.TXT", True)
    assert (bf.revision_chip(ok), bf.mcc_chip(ok), bf.sd_chip(ok)) == (
        "Rev C", "MCC v1.3.2", "microSD: yes")
    odd = bf.build("HBI0309B", "", "1.4.1", "", False)
    assert odd["revision_tested"] is False and odd["mcc_fw_tested"] is False
    assert (bf.revision_chip(odd), bf.mcc_chip(odd), bf.sd_chip(odd)) == (
        "Rev B: untested", "MCC v1.4.1: untested", "microSD: no")


def test_twin_unknown_facts_show_no_chip_and_no_dict():
    assert bf.build() is None
    f = bf.build("", "", "", "", None)
    assert f is None
    only = bf.build(user_sd=None, mcc_fw="v1.3.2")
    assert bf.revision_chip(only) == "" and bf.sd_chip(only) == ""


def test_drive_board_facts_for_the_scan_rows(tmp_path: Path):
    f = bringup.drive_board_facts(str(card(tmp_path, LOG)))
    assert f["revision"] == "C" and f["mcc_fw"] == "v1.3.2" and f["mcc_fw_tested"] is True
    assert bringup.drive_board_facts(str(tmp_path / "gone")) is None


class _Storage:
    def board_revision(self, *, boot_board=""):
        return "HBI0309B", "LOG.TXT"

    def mcc_firmware(self, *, boot_fw=""):
        raise OSError("drive gone")


class _Session:
    storage = _Storage()


def test_a_failing_seam_is_an_unknown_fact_never_an_error():
    f = bf.facts_of_session(_Session())
    assert f["revision"] == "B" and f["mcc_fw"] == "" and f["user_sd"] is None
    assert bf.facts_of_session(object()) is None


def test_user_sd_only_from_a_linux_harness_card_status():
    class Slots:
        last_card = False

    class Ident:
        harness_impl = "linux"

    s = type("S", (), {"os_slots": Slots()})()
    assert bf.facts_of_session(s, Ident())["user_sd"] is False
    Ident.harness_impl = "bare-metal"
    assert bf.facts_of_session(s, Ident()) is None


# -- `harness-manager info` -------------------------------------------------------------------

def _info_with(facts):
    import contextlib
    from types import SimpleNamespace as NS

    from harness_manager.cli import cmd_system
    from harness_manager.core.model import BoardIdentity, Health

    ident = BoardIdentity(board_type="mps3", shell_id="0x1", rm_id="0x2", rm_name="x")
    cand = NS(board_id="b", label="MPS3", name="", links=[], evidence="")
    info = NS(identity=ident, candidate=cand, health=Health(reachable=True, control_channel="up", notes=()),
              capabilities=frozenset(), unavailable={}, facts=facts)
    out = []

    @contextlib.contextmanager
    def board():
        yield cand, None

    ctx = NS(board=board, engine=NS(info=lambda bid: info), emit=out.append)
    cmd_system.cmd_info(ctx)
    return out[0]


def test_info_prints_and_returns_the_facts():
    res = _info_with(bf.build("HBI0309B", "", "v1.4.1", "", True))
    assert any("Rev B: untested   MCC v1.4.1: untested   microSD: yes" in line
               for line in res.human)
    assert res.data["facts"]["mcc_fw"] == "v1.4.1"


def test_twin_info_without_facts_has_no_facts_line_or_key():
    res = _info_with(None)
    assert not any(line.startswith("facts") for line in res.human)
    assert "facts" not in res.data
