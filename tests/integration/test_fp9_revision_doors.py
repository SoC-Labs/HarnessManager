"""FIX-PACK-9: a B+C release (platform v2.0.0: ``MB/HBI0309B`` and ``MB/HBI0309C``, identical
apart from the ``BOARD:`` line) through the doors that write the config SD. Each has its twin.

- **The update's Debug USB door** on ``VirtualMps3(usb=True)`` (T7's world): onto a C card both
  folders are written, C keeps the card's MBBIOS line, B takes the bundle's, the board comes
  up on the new harness; onto a B-only card of a Rev B board (its MCC reads MB/HBI0309B) the
  mirror, with "Rev B: boots, untested"; a C-only release onto that B board is blocked with
  the reason, and nothing is written.
- **The hub door** (HUB-SD's world): fpgahub writes HBI0309C's nanosoc.bit only, so the B
  folder of a B+C release is left as it is and said; the install goes through.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services.update import RESULT_INSTALLED
from tests.fakes.fake_mcc import BOOT_BANNER
from tests.fakes.t7_bundles import Release
from tests.integration import test_hubsd_install as _hub
from tests.integration import test_t7_harness_update as _t7

vb, fake_time, server, world = _t7.vb, _t7.fake_time, _t7.server, _t7.world

B, C = "HBI0309B", "HBI0309C"
CARD_LINE = b"MBBIOS: mbb_v132.ebf  ;MB BIOS IMAGE\n"
APP = b"APPFILE: Nanosoc\\nanosoc.txt\n"
NOTE = b"F0FILE: nanosoc.bit\n[OSCCLKS]\nOSC0: 25.0\nOSC1: 50.0\n"


def board_txt(rev: str, mbbios: bytes = b"MBBIOS: mbb_v141.ebf\n") -> bytes:
    return b"BOARD: " + rev.encode() + b"\n[MCCS]\n" + mbbios + b"[FPGAS]\n" + APP


@dataclass
class BcRelease(Release):
    """A release whose config SD carries ``revs`` (the platform's B+C), declared in its compat."""

    revs: tuple[str, ...] = (B, C)

    def __post_init__(self) -> None:
        bit = self.bit()
        extra: dict[str, bytes] = {}
        for rev in self.revs:
            extra[f"MB/{rev}/board.txt"] = board_txt(rev)
            extra[f"MB/{rev}/Nanosoc/nanosoc.txt"] = NOTE
            extra[f"MB/{rev}/Nanosoc/nanosoc.bit"] = bit
        self.sd_extra = {**extra, **self.sd_extra}

    def add_to(self, builder, tmp: Path, **kw: Any) -> dict[str, Any]:
        compat = {"min_app": "0.0.1", "board_revs": list(self.revs), "mcc_fw_tested": ["1.3.2"]}
        return builder.add_harness(self.version, self.identity(), self.components(builder, tmp),
                                   compat=compat, **kw)


def rev_b_board(vb) -> None:
    """Make the virtual board a Rev B: its card serves HBI0309B only, its MCC says rev B and
    reads MB/HBI0309B, and the card's LOG.TXT says so."""
    root = vb.sd.root
    (root / "MB" / C).rename(root / "MB" / B)
    (root / "MB" / B / "board.txt").write_bytes(board_txt(B, CARD_LINE))
    (root / "LOG.TXT").write_text("MotherBoard Revision B Variant A\r\n")
    vb.mcc.boot_banner = tuple(line.replace("rev C", "rev B").replace(C, B)
                               for line in BOOT_BANNER)


def said(events) -> list[str]:
    return [e.data.get("text", "") for e in events
            if e.topic == "update.progress" and e.data.get("phase") == "sd:mbbios"]


# --- the Debug USB door -------------------------------------------------------------------------


def test_bc_release_onto_a_c_card_writes_both_folders_and_keeps_cs_mbbios(world):
    w, vb = world, world["vb"]
    _t7.bind_identity_to_sd(vb)
    (vb.sd.root / "MB" / C / "board.txt").write_bytes(board_txt(C, CARD_LINE))
    new = BcRelease("1.1.0")
    _t7.publish(w, Release.fielded(), new)
    p, verified = _t7.plan(w)
    assert not p.blockers and not any(x.startswith("Rev B") for x in p.warnings)
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    root = vb.sd.root
    assert (root / "MB" / C / "board.txt").read_bytes() == board_txt(C, CARD_LINE)
    assert (root / "MB" / B / "board.txt").read_bytes() == board_txt(B)
    assert (root / "MB" / B / "Nanosoc" / "nanosoc.bit").read_bytes() == new.bit()
    assert (root / "MB" / C / "Nanosoc" / "nanosoc.bit").read_bytes() == new.bit()
    note = ("MBBIOS kept: HBI0309C mbb_v132.ebf; MBBIOS: HBI0309B mbb_v141.ebf from the bundle "
            "(the card has no mbb_v141.ebf, so the MCC will not update)")
    assert out.notes == [note] and said(w["events"]) == [note]


def test_twin_bc_release_onto_a_rev_b_card_is_the_mirror_and_warns(world):
    w, vb = world, world["vb"]
    rev_b_board(vb)
    _t7.bind_identity_to_sd(vb, rev=B)
    new = BcRelease("1.1.0")
    _t7.publish(w, Release.fielded(), new)
    p, verified = _t7.plan(w)
    assert not p.blockers
    assert ("Rev B: boots, untested. This board is HBI0309B (LOG.TXT on its config SD); "
            "harness 1.1.0 is supported on Rev C") in p.warnings
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail          # the board read MB/HBI0309B
    root = vb.sd.root
    assert (root / "MB" / B / "board.txt").read_bytes() == board_txt(B, CARD_LINE)
    assert (root / "MB" / C / "board.txt").read_bytes() == board_txt(C)
    assert out.notes == ["MBBIOS: HBI0309C mbb_v141.ebf from the bundle (the card has no "
                         "mbb_v141.ebf, so the MCC will not update); MBBIOS kept: HBI0309B "
                         "mbb_v132.ebf"]


def test_a_c_only_release_onto_a_rev_b_board_is_blocked_and_nothing_is_written(world):
    w, vb = world, world["vb"]
    rev_b_board(vb)
    before = vb.sd.snapshot()
    _t7.publish(w, Release.fielded(), Release("1.1.0"))         # board_revs [HBI0309C]
    p, verified = _t7.plan(w)
    assert p.blockers == [
        "this board is HBI0309B (LOG.TXT on its config SD), and harness 1.1.0 carries "
        "MB/HBI0309C only: the MCC reads only MB/HBI0309B/, so the board would stay "
        "unprogrammed"]
    with pytest.raises(RefusedError, match="would stay unprogrammed"):
        w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert vb.sd.snapshot() == before and vb.reboots == 0


def test_twin_the_same_c_only_release_onto_a_c_board_installs(world):
    w, vb = world, world["vb"]
    _t7.bind_identity_to_sd(vb)
    _t7.publish(w, Release.fielded(), Release("1.1.0"))
    p, verified = _t7.plan(w)
    assert not p.blockers
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail


# --- the hub door ------------------------------------------------------------------------------


def test_the_hub_door_writes_cs_bit_and_leaves_the_b_folder(tmp_path, server):
    # the C folder as the running release has it (only nanosoc.bit differs); B is new
    new = BcRelease("1.1.0", with_overlays=False, sd_extra={f"MB/{C}/board.txt": APP})
    w = _hub.World(tmp_path, server, releases=(_hub.OLD, new))
    out, plan = w.install()
    assert out.result == RESULT_INSTALLED, out.detail
    assert not plan.blockers          # no signed file lists here: the door measures it after
    assert len(w.program_calls()) == 1                    # nanosoc.bit (HBI0309C's) only
    assert out.notes[-1] == ("MB/HBI0309B not written: the hub writes "
                             "MB/HBI0309C/Nanosoc/nanosoc.bit only, and this board reads "
                             "MB/HBI0309C")


def test_twin_the_hub_door_still_refuses_another_change_in_the_c_folder(tmp_path, server):
    new = BcRelease("1.1.0", with_overlays=False,
                    sd_extra={f"MB/{C}/board.txt": APP,
                              f"MB/{C}/Nanosoc/nanosoc.txt": b"F0FILE: other.bit\n"})
    w = _hub.World(tmp_path, server, releases=(_hub.OLD, new))
    with pytest.raises(Exception, match=r"changes more of the config SD"):
        w.install()
    assert w.program_calls() == []
