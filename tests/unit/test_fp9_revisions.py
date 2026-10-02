"""FIX-PACK-9: MPS3 board revisions B and C (david, 2 Oct). Each check has its twin.

Platform v2.0.0 ships config-SD folders for BOTH ``MB/HBI0309B`` and ``MB/HBI0309C``
(identical apart from the ``BOARD:`` line); the MCC reads only ``MB/HBI0309<detected rev>/``.

- ``mbbios.keep_mbbios``: the G8 MBBIOS rule on EVERY revision's board.txt, each against the
  card's board.txt of the same revision (B+C onto a C card, onto a B-only card, the ``.ebf``
  refusal per revision, CRLF, one line per file, a pre-FIX-PACK-9 caller, the "no B or C
  folder" warning);
- ``Mps3Storage.install``: both folders written, another revision's tree (Arm's HBI0309A)
  untouched, the notes;
- the board's revision (``board_revision_of``: the MCC boot witness, LOG.TXT, a card with one
  revision folder) and the planner's ``revision_check`` (B+C passes on both, warns on B; a
  C-only release on a B board is blocked with the reason);
- the bundle check (a declared revision has its board.txt), the hub door's other revisions.

The update's Debug USB door end to end is in ``tests/integration/test_fp9_revision_doors.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.core.errors import ExitCode, RefusedError
from harness_manager.core.pack import Check
from harness_manager.services.update import hub_door
from harness_manager.services.update.planner import (
    BoardView,
    make_plan,
    revision_check,
)
from harness_manager.services.update.schema import parse_channel
from harness_manager_mps3 import mbbios as M
from harness_manager_mps3.sd import Mps3Storage, SdEnv, board_revision_of, revision_of_log
from tests.fakes.fake_channel import ChannelBuilder, TestKeys
from tests.fakes.fake_sd import FakeSdVolume
from tests.fakes.t7_bundles import FIELDED_HARNESS, FIELDED_STATIC, Release

B, C = "HBI0309B", "HBI0309C"
BOARD_B, BOARD_C = "MB/HBI0309B/board.txt", "MB/HBI0309C/board.txt"


def board(rev: str, line: bytes = b"MBBIOS: mbb_v141.ebf  ;MB BIOS IMAGE\n", nl: bytes = b"\n"
          ) -> bytes:
    """A board.txt as the platform stamps it, for ``rev``."""
    text = (b"BOARD: " + rev.encode() + b"\n;comment\n[MCCS]\n" + line
            + b"[FPGAS]\nAPPFILE: Nanosoc\\nanosoc.txt\n")
    return text.replace(b"\n", nl)


CARD_LINE = b"MBBIOS: mbb_v132.ebf  ;card's own\n"


def bundle_files(tmp: Path, *, b: bytes | None = None, c: bytes | None = None,
                 revs=(B, C)) -> dict[str, Path]:
    """A config-SD bundle with one folder per revision in ``revs``."""
    out: dict[str, Path] = {}
    for rev in revs:
        d = tmp / "bundle" / "MB" / rev
        (d / "Nanosoc").mkdir(parents=True, exist_ok=True)
        (d / "board.txt").write_bytes((b if rev == B else c) or board(rev))
        (d / "Nanosoc" / "nanosoc.bit").write_bytes(b"BIT")
        out[f"MB/{rev}/board.txt"] = d / "board.txt"
        out[f"MB/{rev}/Nanosoc/nanosoc.bit"] = d / "Nanosoc" / "nanosoc.bit"
    return out


def keep(tmp: Path, files, *, card: dict[str, bytes], card_files, **kw):
    return M.keep_mbbios(files, card_boards=card, card_files=card_files, workdir=tmp, **kw)


C_CARD_FILES = ["config.txt", BOARD_C, "MB/HBI0309C/Nanosoc/nanosoc.bit",
                "MB/HBI0309C/mbb_v132.ebf"]
B_CARD_FILES = [f.replace(C, B) for f in C_CARD_FILES]


# --- the rule, per revision --------------------------------------------------------------------


def test_bc_bundle_onto_a_c_card_keeps_cs_line_from_the_card_and_bs_from_the_bundle(tmp_path):
    files = bundle_files(tmp_path)
    out, got = keep(tmp_path, files, card={BOARD_C: board(C, CARD_LINE)},
                    card_files=C_CARD_FILES)
    assert out[BOARD_C].read_bytes() == board(C, CARD_LINE)            # the card's line, verbatim
    assert out[BOARD_B].read_bytes() == board(B)                       # the bundle's, unchanged
    assert out[BOARD_B] == files[BOARD_B]                              # (not even copied)
    assert got.note == ("MBBIOS kept: HBI0309C mbb_v132.ebf; MBBIOS: HBI0309B mbb_v141.ebf from "
                        "the bundle (the card has no mbb_v141.ebf, so the MCC will not update)")
    assert [(d.rev, d.action) for d in got.revs] == [(C, M.KEPT), (B, M.FROM_BUNDLE)]
    assert got.action == M.FROM_BUNDLE and got.warnings == ()
    assert got.value == "HBI0309C mbb_v132.ebf, HBI0309B mbb_v141.ebf"
    assert [d.path for d in got.per_rev()] == [BOARD_C, BOARD_B]


def test_twin_bc_bundle_onto_a_b_only_card_is_the_mirror(tmp_path):
    files = bundle_files(tmp_path)
    out, got = keep(tmp_path, files, card={BOARD_B: board(B, CARD_LINE)},
                    card_files=B_CARD_FILES)
    assert out[BOARD_B].read_bytes() == board(B, CARD_LINE)
    assert out[BOARD_C].read_bytes() == board(C)
    assert [(d.rev, d.action) for d in got.revs] == [(C, M.FROM_BUNDLE), (B, M.KEPT)]
    assert got.note == ("MBBIOS: HBI0309C mbb_v141.ebf from the bundle (the card has no "
                        "mbb_v141.ebf, so the MCC will not update); MBBIOS kept: HBI0309B "
                        "mbb_v132.ebf")


def test_each_revision_keeps_its_own_cards_line(tmp_path):
    card = {BOARD_B: board(B, b"MBBIOS: mbb_v140.ebf\n"), BOARD_C: board(C, CARD_LINE)}
    out, got = keep(tmp_path, bundle_files(tmp_path), card=card,
                    card_files=[*C_CARD_FILES, BOARD_B])
    assert b"MBBIOS: mbb_v140.ebf\n" in out[BOARD_B].read_bytes()
    assert CARD_LINE in out[BOARD_C].read_bytes()
    assert got.action == M.KEPT and got.note == ("MBBIOS kept: HBI0309C mbb_v132.ebf; MBBIOS "
                                                 "kept: HBI0309B mbb_v140.ebf")


def test_the_ebf_refusal_is_per_revision_and_names_the_folder(tmp_path):
    """C keeps the card's line; B's folder is not on the card, and the .ebf the bundle's B line
    names IS on the card (in the C folder: the whole card counts): refused, nothing returned."""
    with pytest.raises(RefusedError) as exc:
        keep(tmp_path, bundle_files(tmp_path), card={BOARD_C: board(C, CARD_LINE)},
             card_files=[*C_CARD_FILES, "MB/HBI0309C/mbb_v141.ebf"])
    assert exc.value.code == ExitCode.REFUSED
    assert exc.value.message == (
        "this card would make the MCC update itself to mbb_v141.ebf through "
        "MB/HBI0309B/board.txt: remove mbb_v141.ebf from the card, or add --allow-mcc-update")
    assert exc.value.data["mcc_update"] == {"file": "mbb_v141.ebf", "value": "mbb_v141.ebf",
                                            "board_txt": BOARD_B}


def test_twin_allow_mcc_update_writes_bs_line_and_still_keeps_cs(tmp_path):
    out, got = keep(tmp_path, bundle_files(tmp_path), card={BOARD_C: board(C, CARD_LINE)},
                    card_files=[*C_CARD_FILES, "MB/HBI0309C/mbb_v141.ebf"],
                    allow_mcc_update=True)
    assert [(d.rev, d.action) for d in got.revs] == [(C, M.KEPT), (B, M.ALLOWED)]
    assert got.action == M.ALLOWED and "HBI0309B mbb_v141.ebf from the bundle, allowed by " \
        "--allow-mcc-update" in got.note
    assert CARD_LINE in out[BOARD_C].read_bytes()


def test_a_cards_line_in_one_folder_never_lands_in_another(tmp_path):
    """The rule never borrows C's card line for B: B has no card counterpart."""
    out, _ = keep(tmp_path, bundle_files(tmp_path), card={BOARD_C: board(C, CARD_LINE)},
                  card_files=C_CARD_FILES)
    assert b"mbb_v132" not in out[BOARD_B].read_bytes()


def test_crlf_files_per_revision_keep_their_line_endings(tmp_path):
    files = bundle_files(tmp_path, b=board(B, nl=b"\r\n"), c=board(C, nl=b"\r\n"))
    out, got = keep(tmp_path, files, card={BOARD_C: board(C, CARD_LINE, nl=b"\r\n")},
                    card_files=C_CARD_FILES)
    c_txt, b_txt = out[BOARD_C].read_bytes(), out[BOARD_B].read_bytes()
    assert c_txt == board(C, CARD_LINE, nl=b"\r\n") and b"\r\r" not in c_txt
    assert b_txt == board(B, nl=b"\r\n")
    assert [d.action for d in got.revs] == [M.KEPT, M.FROM_BUNDLE]


def test_twin_a_crlf_card_line_goes_into_an_lf_bundle_without_doubling(tmp_path):
    card = b"BOARD: HBI0309B\r\n[MCCS]\r\nMBBIOS: mbb_v132.ebf ;card\r\n"
    out, _ = keep(tmp_path, bundle_files(tmp_path), card={BOARD_B: card},
                  card_files=B_CARD_FILES)
    got = out[BOARD_B].read_bytes()
    assert b"MBBIOS: mbb_v132.ebf ;card" in got and b"\r\r" not in got and b"v141" not in got


def test_only_the_first_mbbios_line_per_file_reaches_the_card(tmp_path):
    two = board(B, b"MBBIOS: mbb_v141.ebf\nmbbios: mbb_v999.ebf ;dup\n")
    out, got = keep(tmp_path, bundle_files(tmp_path, b=two), card={BOARD_C: board(C, CARD_LINE)},
                    card_files=[*C_CARD_FILES, "MB/HBI0309B/mbb_v999.ebf"])   # the 2nd's .ebf
    assert out[BOARD_B].read_bytes() == board(B, b"MBBIOS: mbb_v141.ebf\n")
    assert got.revs[1].value == "mbb_v141.ebf"


def test_no_revision_is_ever_written_without_the_line(tmp_path):
    for card in ({}, {BOARD_C: board(C, CARD_LINE)}, {BOARD_B: board(B, b"")}):
        for extra in ([], ["MB/HBI0309C/mbb_v141.ebf"]):
            try:
                out, _ = keep(tmp_path, bundle_files(tmp_path), card=card,
                              card_files=[*C_CARD_FILES, *extra], allow_mcc_update=True)
            except RefusedError:
                continue
            for rel in (BOARD_B, BOARD_C):
                assert M.mbbios_line(out[rel].read_bytes().decode()) is not None, (card, rel)


def test_a_c_only_bundle_says_what_it_said_before(tmp_path):
    out, got = keep(tmp_path, bundle_files(tmp_path, revs=(C,)),
                    card={BOARD_C: board(C, CARD_LINE)}, card_files=C_CARD_FILES)
    assert (got.action, got.note, got.revs, got.rev) == (M.KEPT, "MBBIOS kept: mbb_v132.ebf",
                                                          (), C)


def test_a_pre_fix_pack_9_caller_cannot_have_a_b_card_line_guessed(tmp_path):
    """``card_board_txt`` is HBI0309C's; the card lists B's board.txt, which was not passed."""
    with pytest.raises(RefusedError, match="MB/HBI0309B/board.txt was not read"):
        M.keep_mbbios(bundle_files(tmp_path), card_board_txt=board(C, CARD_LINE),
                      card_files=[*C_CARD_FILES, BOARD_B], workdir=tmp_path)


def test_twin_a_pre_fix_pack_9_caller_on_a_card_without_b_works(tmp_path):
    out, got = M.keep_mbbios(bundle_files(tmp_path), card_board_txt=board(C, CARD_LINE),
                             card_files=C_CARD_FILES, workdir=tmp_path)
    assert [(d.rev, d.action) for d in got.revs] == [(C, M.KEPT), (B, M.FROM_BUNDLE)]


def test_a_card_with_neither_b_nor_c_is_written_with_a_warning(tmp_path):
    for card_files in (["config.txt"], ["config.txt", "MB/HBI0309A/board.txt"]):
        _, got = keep(tmp_path, bundle_files(tmp_path), card={}, card_files=card_files)
        assert got.warnings == ("this card had no HBI0309B or HBI0309C folder: is it an MPS3 "
                                "configuration SD? both were written",)
        assert M.notes_of(got)[-1] == ("WARNING: this card had no HBI0309B or HBI0309C folder: "
                                       "is it an MPS3 configuration SD? both were written")
    _, got = keep(tmp_path, bundle_files(tmp_path, revs=(C,)), card={}, card_files=["config.txt"])
    assert got.warnings[0].endswith("HBI0309C was written")


def test_twin_a_card_with_b_or_c_has_no_warning(tmp_path):
    for card_files in (C_CARD_FILES, B_CARD_FILES, ["MB/HBI0309A/x.txt", BOARD_C]):
        _, got = keep(tmp_path, bundle_files(tmp_path), card={}, card_files=card_files)
        assert got.warnings == ()


def test_card_boards_of_reads_every_revision_case_blind(tmp_path):
    for rev, txt in (("hbi0309b", b"b"), ("HBI0309C", b"c"), ("HBI0309A", b"a")):
        (tmp_path / "mb" / rev).mkdir(parents=True)
        (tmp_path / "mb" / rev / ("BOARD.TXT" if rev == "HBI0309C" else "board.txt")
         ).write_bytes(txt)
    (tmp_path / "mb" / "notarev").mkdir()
    (tmp_path / "mb" / "notarev" / "board.txt").write_bytes(b"x")
    got = M.card_boards_of(tmp_path)
    assert got == {"mb/HBI0309A/board.txt": b"a", "mb/HBI0309C/BOARD.TXT": b"c",
                   "mb/hbi0309b/board.txt": b"b"}
    assert {M.board_rev(k) for k in got} == {"HBI0309A", B, C}
    assert M.card_boards_of(tmp_path / "nothing") == {}


# --- through Mps3Storage.install (sd install, the update's Debug USB door) -----------------------


def _env() -> SdEnv:
    return SdEnv(list_volumes=lambda: [], pid_alive=lambda pid: False,
                 hostname=lambda: "testhost", now=lambda: 1_790_000_000.0)


def as_rev(card: FakeSdVolume, rev: str) -> FakeSdVolume:
    """FakeSdVolume serves HBI0309C; move its folder to ``rev`` (a card made for that board)."""
    (card.root / "MB" / C).rename(card.root / "MB" / rev)
    return card


def _install(tmp_path, card: FakeSdVolume, files, **kw):
    storage = Mps3Storage(str(card.root), env=_env())
    rec = storage.backup(tmp_path / "bk")
    said: list = []

    def progress(phase, done, total, detail=None):
        said.append((phase, (detail or {}).get("text", "")))

    progress.takes_detail = True                              # type: ignore[attr-defined]
    storage.install(files, backup=rec, progress=progress, **kw)
    return storage, said


def test_install_writes_both_folders_onto_a_c_card(tmp_path):
    card = FakeSdVolume(tmp_path / "sd")
    (card.root / "MB" / C / "board.txt").write_bytes(board(C, CARD_LINE))
    storage, said = _install(tmp_path, card, bundle_files(tmp_path))
    assert (card.root / BOARD_C).read_bytes() == board(C, CARD_LINE)
    assert (card.root / BOARD_B).read_bytes() == board(B)
    assert (card.root / "MB" / B / "Nanosoc" / "nanosoc.bit").read_bytes() == b"BIT"
    note = ("MBBIOS kept: HBI0309C mbb_v132.ebf; MBBIOS: HBI0309B mbb_v141.ebf from the bundle "
            "(the card has no mbb_v141.ebf, so the MCC will not update)")
    assert storage.install_notes == [note] and ("mbbios", note) in said


def test_twin_install_onto_a_b_only_card_is_the_mirror(tmp_path):
    card = as_rev(FakeSdVolume(tmp_path / "sd"), B)
    (card.root / BOARD_B).write_bytes(board(B, CARD_LINE))
    storage, _ = _install(tmp_path, card, bundle_files(tmp_path))
    assert (card.root / BOARD_B).read_bytes() == board(B, CARD_LINE)
    assert (card.root / BOARD_C).read_bytes() == board(C)
    assert storage.install_notes == [
        "MBBIOS: HBI0309C mbb_v141.ebf from the bundle (the card has no mbb_v141.ebf, so the "
        "MCC will not update); MBBIOS kept: HBI0309B mbb_v132.ebf"]


def test_install_refuses_the_mcc_update_for_one_revision_before_writing_anything(tmp_path):
    card = FakeSdVolume(tmp_path / "sd")
    (card.root / "MB" / C / "board.txt").write_bytes(board(C, CARD_LINE))
    (card.root / "MB" / C / "mbb_v141.ebf").write_bytes(b"BIOS")
    before = card.snapshot()
    with pytest.raises(RefusedError, match="through MB/HBI0309B/board.txt"):
        _install(tmp_path, card, bundle_files(tmp_path))
    after = card.snapshot()
    assert after == before                                  # neither folder written


def test_another_revision_tree_on_the_card_is_left_untouched(tmp_path):
    card = FakeSdVolume(tmp_path / "sd")
    a = card.root / "MB" / "HBI0309A"
    (a / "AN536").mkdir(parents=True)
    (a / "board.txt").write_bytes(b"BOARD: HBI0309A\r\n[MCCS]\r\nMBBIOS: mbb_v132.ebf\r\n")
    (a / "AN536" / "an536.bit").write_bytes(b"ARM")
    before = {k: v for k, v in card.snapshot().items() if k.startswith("MB/HBI0309A/")}
    storage, _ = _install(tmp_path, card, bundle_files(tmp_path))
    assert {k: v for k, v in card.snapshot().items() if k.startswith("MB/HBI0309A/")} == before
    assert not any("HBI0309A" in n for n in storage.install_notes)


def test_install_onto_a_card_with_neither_folder_warns(tmp_path):
    card = as_rev(FakeSdVolume(tmp_path / "sd"), "HBI0309A")
    storage, said = _install(tmp_path, card, bundle_files(tmp_path))
    warn = ("WARNING: this card had no HBI0309B or HBI0309C folder: is it an MPS3 configuration "
            "SD? both were written")
    assert storage.install_notes[-1] == warn and ("mbbios", warn) in said
    assert (card.root / BOARD_B).is_file() and (card.root / BOARD_C).is_file()


def test_twin_install_onto_a_c_card_does_not_warn(tmp_path):
    storage, _ = _install(tmp_path, FakeSdVolume(tmp_path / "sd"), bundle_files(tmp_path))
    assert not any(n.startswith("WARNING") for n in storage.install_notes)


# --- the board's revision ----------------------------------------------------------------------


def test_revision_of_the_mcc_log_and_the_console():
    assert revision_of_log("MotherBoard Revision C Variant A\r\n") == C
    assert revision_of_log("Configuring motherboard (rev B, var A)...") == B
    assert revision_of_log("motherboard (rev C, var A)\nMotherBoard Revision B Variant A") == B
    assert revision_of_log("Configuring FPGA from file \\MB\\HBI0309C\\x.bit") == ""


def test_board_revision_of_a_card(tmp_path):
    card = FakeSdVolume(tmp_path / "sd")
    assert board_revision_of(card.root) == (C, "the config SD's only revision folder")
    (card.root / "MB" / B).mkdir()
    assert board_revision_of(card.root) == ("", "")            # B and C: says nothing
    (card.root / "log.txt").write_text("x\nMotherBoard Revision B Variant A\n")
    assert board_revision_of(card.root) == (B, "log.txt on its config SD")
    assert board_revision_of(card.root, boot_board="rev C, var A") == (
        C, "the MCC boot log: rev C")
    storage = Mps3Storage(str(card.root), env=_env())
    assert storage.board_revision() == (B, "log.txt on its config SD")


# --- the planner -------------------------------------------------------------------------------

KEYS = TestKeys()


def channel(tmp_path, *, revs=(B, C), sd_files: dict[str, str] | None = None):
    """A channel with the fielded release and 1.1.0 for ``revs`` (``sd_files``: the SD
    part's signed file list)."""
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    Release.fielded().add_to(b, tmp_path / "art", current=False)
    new = Release("1.1.0")
    comps = new.components(b, tmp_path / "art")
    if sd_files is not None:
        comps[0] = {**comps[0], "files": sd_files}
    b.add_harness(new.version, new.identity(), comps,
                  compat={"min_app": "0.0.1", "board_revs": list(revs),
                          "mcc_fw_tested": ["1.3.2"]})
    return parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))


def view(rev: str = "", how: str = "LOG.TXT on its config SD", folders=(B, C)) -> BoardView:
    from harness_manager.core.model import BoardIdentity

    return BoardView(board_id="mps3@test", pack="mps3",
                     identity=BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC,
                                            harness_version=FIELDED_HARNESS),
                     has_storage=True, has_controller=True, sd_revisions=tuple(folders),
                     board_rev=rev, board_rev_from=how if rev else "")


REV_B = ("Rev B: boots, untested. This board is HBI0309B (LOG.TXT on its config SD); harness "
         "1.1.0 is supported on Rev C")


def test_bc_release_passes_on_a_b_board_with_the_rev_b_warning(tmp_path):
    plan = make_plan(channel(tmp_path), view(B), app_version="0.1.0")
    assert not plan.blockers and plan.base and REV_B in plan.warnings


def test_twin_bc_release_passes_on_a_c_board_without_it(tmp_path):
    plan = make_plan(channel(tmp_path), view(C), app_version="0.1.0")
    assert not plan.blockers and not any(w.startswith("Rev B") for w in plan.warnings)


def test_a_c_only_release_on_a_b_board_is_blocked_with_the_reason(tmp_path):
    plan = make_plan(channel(tmp_path, revs=(C,)), view(B), app_version="0.1.0")
    assert plan.blockers == [
        "this board is HBI0309B (LOG.TXT on its config SD), and harness 1.1.0 carries "
        "MB/HBI0309C only: the MCC reads only MB/HBI0309B/, so the board would stay "
        "unprogrammed"]
    assert not any(w.startswith("Rev B") for w in plan.warnings)   # blocked, not warned
    with pytest.raises(RefusedError, match="would stay unprogrammed"):
        plan.approve()


def test_twin_a_c_only_release_on_a_c_board_passes(tmp_path):
    plan = make_plan(channel(tmp_path, revs=(C,)), view(C), app_version="0.1.0")
    assert not plan.blockers


def test_a_b_only_card_is_a_b_board_when_nothing_else_says(tmp_path):
    """No witness, no LOG.TXT: a card with one revision folder serves that revision."""
    how = "the config SD's only revision folder"
    plan = make_plan(channel(tmp_path, revs=(C,)), view(B, how, folders=(B,)),
                     app_version="0.1.0")
    assert plan.blockers[0].startswith(f"this board is HBI0309B ({how}), and harness 1.1.0 "
                                       "carries MB/HBI0309C only")
    plan = make_plan(channel(tmp_path), view(B, how, folders=(B,)), app_version="0.1.0")
    assert not plan.blockers and plan.warnings[-1].startswith("Rev B: boots, untested")


def test_a_declared_revision_missing_from_the_signed_file_list_is_blocked(tmp_path):
    files = {BOARD_C: "1" * 64, "MB/HBI0309C/Nanosoc/nanosoc.bit": "2" * 64}
    plan = make_plan(channel(tmp_path, sd_files=files), view(B), app_version="0.1.0")
    assert plan.blockers == [
        "this board is HBI0309B (LOG.TXT on its config SD), and harness 1.1.0's config SD has "
        "no MB/HBI0309B/board.txt: the MCC reads only MB/HBI0309B/, so the board would stay "
        "unprogrammed"]


def test_twin_the_file_list_with_the_folder_passes(tmp_path):
    files = {BOARD_C: "1" * 64, BOARD_B: "3" * 64, "MB/HBI0309C/Nanosoc/nanosoc.bit": "2" * 64}
    plan = make_plan(channel(tmp_path, sd_files=files), view(B), app_version="0.1.0")
    assert not plan.blockers


def test_an_unknown_revision_keeps_the_folder_rule(tmp_path):
    ch = channel(tmp_path)
    assert revision_check(ch.harness_release(), view("", folders=("HBI0309A",)))[0] == (
        "the config SD is for HBI0309A; harness 1.1.0 supports HBI0309B, HBI0309C")
    assert revision_check(ch.harness_release(), view("", folders=("HBI0309A", C))) == ("", "")


def test_a_card_with_no_revision_folder_is_a_plan_warning(tmp_path):
    plan = make_plan(channel(tmp_path), view("", folders=()), app_version="0.1.0")
    assert not plan.blockers and (
        "the config SD has no revision folder (MB/HBI*): is it this board's configuration SD? "
        "harness 1.1.0 writes MB/HBI0309B, MB/HBI0309C") in plan.warnings
    plan = make_plan(channel(tmp_path), view("", folders=(C,)), app_version="0.1.0")
    assert not any("no revision folder" in w for w in plan.warnings)


def test_the_catalogue_verdict_says_why_a_c_only_release_does_not_fit_a_b_board(tmp_path):
    from harness_manager.services.harness_catalog import verdict_of

    ch = channel(tmp_path, revs=(C,))
    rel, b = ch.harness_release(), view(B)
    v = verdict_of(make_plan(ch, b, app_version="0.1.0"), rel, b, app_version="0.1.0")
    assert v["verdict"] == "incompatible" and "would stay unprogrammed" in v["why"]
    ch = channel(tmp_path)
    v = verdict_of(make_plan(ch, b, app_version="0.1.0"), ch.harness_release(), b,
                   app_version="0.1.0")
    assert v["verdict"] != "incompatible"


# --- the bundle check: a declared revision has its board.txt ----------------------------------


def _sd_check(tmp_path, tree: dict[str, bytes], revs):
    from harness_manager.services.update.bundle import check_sd_component

    root = tmp_path / "tree"
    files = {}
    for rel, data in tree.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        files[rel] = root / rel
    release = SimpleNamespace(compat=SimpleNamespace(board_revs=tuple(revs)),
                              identity=SimpleNamespace(usercode=""))
    comp = SimpleNamespace(name="sd")
    return next(i for i in check_sd_component(comp, files, release, "")
                if i.name == "sd: board revision")


def test_the_bundle_check_refuses_a_declared_revision_without_its_folder(tmp_path):
    item = _sd_check(tmp_path, {BOARD_C: b"x", "config.txt": b"c"}, (B, C))
    assert item.check == Check.MISMATCH and item.detail == ("declares HBI0309B but has no "
                                                "MB/HBI0309B/board.txt")


def test_twin_the_bundle_check_passes_both_folders(tmp_path):
    item = _sd_check(tmp_path, {BOARD_C: b"x", BOARD_B: b"y"}, (B, C))
    assert item.check == Check.OK and item.detail == "HBI0309B, HBI0309C"


# --- the hub door: another revision's folder is left, and said -------------------------------------


def test_the_hub_planner_leaves_another_revisions_folder_and_says_so():
    from tests.unit.test_hubsd_units import DOOR, NANOSOC_BIT, _plan, _rel

    old = _rel("1.0.0", {BOARD_C: "1" * 64, NANOSOC_BIT: "2" * 64})
    new = _rel("1.1.0", {BOARD_C: "1" * 64, NANOSOC_BIT: "3" * 64, BOARD_B: "4" * 64,
                         "MB/HBI0309B/Nanosoc/nanosoc.bit": "3" * 64})
    door = {**DOOR, "deferred_paths": [BOARD_C], "left_prefixes": ["MB/HBI0309A/",
                                                                  "MB/HBI0309B/"]}
    plan = _plan()
    hub_door.apply(plan, new, None, SimpleNamespace(hub_sd=door, has_storage=False,
                                                    has_controller=True),
                   via=None, running=old, have_token=False)
    assert not plan.blockers
    assert (f"the hub door leaves MB/HBI0309B on the card as it is: it writes {NANOSOC_BIT} "
            "only (the board behind the hub reads that folder)") in plan.warnings


def test_twin_without_left_prefixes_the_other_folder_blocks_as_before():
    from tests.unit.test_hubsd_units import DOOR, NANOSOC_BIT, _plan, _rel

    old = _rel("1.0.0", {BOARD_C: "1" * 64, NANOSOC_BIT: "2" * 64})
    new = _rel("1.1.0", {BOARD_C: "1" * 64, NANOSOC_BIT: "3" * 64, BOARD_B: "4" * 64})
    plan = _plan()
    hub_door.apply(plan, new, None, SimpleNamespace(hub_sd=DOOR, has_storage=False,
                                                    has_controller=True),
                   via=None, running=old, have_token=False)
    assert any("MB/HBI0309B/board.txt" in b for b in plan.blockers)


def test_the_ab_view_refuses_a_release_with_another_revision(tmp_path):
    from harness_manager_mps3.sd_ab import AbStorage

    card = FakeSdVolume(tmp_path / "sd")
    ab = AbStorage(Mps3Storage(str(card.root), env=_env()))
    ab._install = lambda files, **kw: pytest.fail("nothing may be written")
    with pytest.raises(RefusedError) as exc:
        ab.install(bundle_files(tmp_path), backup=SimpleNamespace(path="x"))
    assert exc.value.message == ("the A/B install (setting updates.sd_ab) writes MB/HBI0309C "
                                 "only, and this release also carries MB/HBI0309B: nothing was "
                                 "written")


def test_twin_the_ab_view_takes_a_c_only_release_as_before(tmp_path):
    from harness_manager_mps3.sd_ab import AbStorage

    card = FakeSdVolume(tmp_path / "sd")
    ab = AbStorage(Mps3Storage(str(card.root), env=_env()))
    seen: dict = {}
    ab._install = lambda files, **kw: seen.update(files=sorted(files))
    ab.install(bundle_files(tmp_path, revs=(C,)), backup=SimpleNamespace(path="x"))
    assert seen["files"] == ["MB/HBI0309C/Nanosoc/nanosoc.bit", BOARD_C]

