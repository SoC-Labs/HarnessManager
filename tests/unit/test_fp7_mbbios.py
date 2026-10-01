"""FIX-PACK-7 (G8): MBBIOS is never changed by Harness Manager. Each check has its twin.

``mbbios.keep_mbbios`` is the one substitution every config-SD writer calls; here it is
checked on its own, then through ``Mps3Storage.install`` (the Debug USB door and ``sd
install``) on FakeSdVolume, then the A/B view. The hub door and the update executor are in
``tests/integration/test_fp7_mbbios_doors.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.core.errors import ExitCode, RefusedError
from harness_manager.services.update.planner import mcc_fw_tested, mcc_version
from harness_manager_mps3 import mbbios as M
from harness_manager_mps3.sd import Mps3Storage, SdEnv
from tests.fakes.fake_sd import FakeSdVolume

CARD = b"BOARD: HBI0309C\n[MCCS]\nMBBIOS: mbb_v132.ebf  ;MB BIOS IMAGE\n[FPGAS]\nAPPFILE: Nanosoc\\nanosoc.txt\n"
BUNDLE = b"BOARD: HBI0309C\n[MCCS]\nMBBIOS: mbb_v141.ebf  ;MB BIOS IMAGE\n[FPGAS]\nAPPFILE: Nanosoc\\nanosoc.txt\n"
NO_LINE = b"BOARD: HBI0309C\n[MCCS]\n[FPGAS]\nAPPFILE: Nanosoc\\nanosoc.txt\n"
EBF_ON_CARD = ["config.txt", "MB/HBI0309C/board.txt", "MB/HBI0309C/mbb_v141.ebf"]
NO_EBF = ["config.txt", "MB/HBI0309C/board.txt", "MB/HBI0309C/mbb_v132.ebf"]


def decide(bundle: bytes = BUNDLE, card: bytes | None = CARD, files=NO_EBF, **kw):
    return M.decide(bundle, card_board_txt=card, card_files=files, **kw)


# --- the rule ------------------------------------------------------------------------------------


def test_the_cards_line_is_kept_in_place_of_the_bundles():
    got = decide()
    assert (got.action, got.value, got.note) == (M.KEPT, "mbb_v132.ebf",
                                                  "MBBIOS kept: mbb_v132.ebf")
    assert got.content == CARD                                  # verbatim, comment and all


def test_twin_the_bundles_line_never_reaches_the_card():
    for card, files in ((CARD, NO_EBF), (CARD, EBF_ON_CARD)):
        assert b"mbb_v141" not in decide(card=card, files=files).content


def test_a_bundle_with_no_line_gets_the_cards_under_mccs():
    got = decide(bundle=NO_LINE)
    assert got.action == M.KEPT and got.content == \
        b"BOARD: HBI0309C\n[MCCS]\nMBBIOS: mbb_v132.ebf  ;MB BIOS IMAGE\n[FPGAS]\nAPPFILE: Nanosoc\\nanosoc.txt\n"


def test_twin_without_an_mccs_section_it_goes_before_the_first_section_or_at_the_end():
    got = decide(bundle=b"BOARD: X\n[FPGAS]\nAPPFILE: a\n")
    assert got.content == b"BOARD: X\nMBBIOS: mbb_v132.ebf  ;MB BIOS IMAGE\n[FPGAS]\nAPPFILE: a\n"
    got = decide(bundle=b"APPFILE: Nanosoc\\nanosoc.txt\n")
    assert got.content == b"APPFILE: Nanosoc\\nanosoc.txt\nMBBIOS: mbb_v132.ebf  ;MB BIOS IMAGE\n"


@pytest.mark.parametrize("card", [NO_LINE, None], ids=["no-line", "no-board-txt"])
def test_no_line_on_the_card_and_no_ebf_writes_the_bundles_line_unchanged(card):
    got = decide(card=card, files=NO_EBF)
    assert got.action == M.FROM_BUNDLE and got.content == BUNDLE
    assert got.note == ("MBBIOS: mbb_v141.ebf from the bundle (the card has no mbb_v141.ebf, "
                        "so the MCC will not update)")


@pytest.mark.parametrize("card", [NO_LINE, None], ids=["no-line", "no-board-txt"])
def test_twin_no_line_and_the_ebf_on_the_card_is_refused_15(card):
    with pytest.raises(RefusedError) as exc:
        decide(card=card, files=EBF_ON_CARD)
    assert exc.value.code == ExitCode.REFUSED
    assert exc.value.message == ("this card would make the MCC update itself to mbb_v141.ebf: "
                                 "remove mbb_v141.ebf from the card, or add --allow-mcc-update")
    assert exc.value.data["mcc_update"]["file"] == "mbb_v141.ebf"


def test_the_ebf_is_found_case_blind():
    with pytest.raises(RefusedError):
        decide(card=None, files=["MB/hbi0309c/MBB_V141.EBF"])


def test_allow_mcc_update_writes_it_with_a_warning():
    got = decide(card=None, files=EBF_ON_CARD, allow_mcc_update=True)
    assert got.action == M.ALLOWED and got.content == BUNDLE
    assert got.note == ("MBBIOS: mbb_v141.ebf from the bundle, allowed by --allow-mcc-update: "
                        "the card has mbb_v141.ebf, so the MCC may update itself to it at its "
                        "next boot")


def test_twin_allow_never_overrides_a_line_the_card_has():
    assert decide(files=EBF_ON_CARD, allow_mcc_update=True).content == CARD


def test_a_board_txt_without_the_line_is_never_written():
    """Every branch: a bundle line is kept, or the card's goes in; never removed."""
    for card in (CARD, NO_LINE, None):
        for files in (NO_EBF, EBF_ON_CARD):
            try:
                got = decide(card=card, files=files, allow_mcc_update=True)
            except RefusedError:
                continue
            assert M.mbbios_line(got.content.decode()) is not None


def test_neither_side_has_a_line_leaves_the_file_alone():
    got = decide(bundle=NO_LINE, card=NO_LINE)
    assert got.action == M.NO_LINE and got.note == "" and got.content == NO_LINE


def test_keep_mbbios_swaps_only_board_txt_and_twin_files_without_it(tmp_path):
    bundle = tmp_path / "b.txt"
    bundle.write_bytes(BUNDLE)
    bit = tmp_path / "n.bit"
    bit.write_bytes(b"BIT")
    files = {"mb/hbi0309c/BOARD.TXT": bundle, "MB/HBI0309C/Nanosoc/nanosoc.bit": bit}
    out, got = M.keep_mbbios(files, card_board_txt=CARD, card_files=NO_EBF, workdir=tmp_path)
    assert got.action == M.KEPT and out["MB/HBI0309C/Nanosoc/nanosoc.bit"] == bit
    assert out["mb/hbi0309c/BOARD.TXT"] != bundle
    assert out["mb/hbi0309c/BOARD.TXT"].read_bytes() == CARD and bundle.read_bytes() == BUNDLE
    out, got = M.keep_mbbios({"MB/HBI0309C/Nanosoc/nanosoc.bit": bit}, card_board_txt=CARD,
                             card_files=NO_EBF, workdir=tmp_path)
    assert got is None and out == {"MB/HBI0309C/Nanosoc/nanosoc.bit": bit}


# --- through Mps3Storage.install (sd install, the update's Debug USB door) -------------------------


def _env() -> SdEnv:
    return SdEnv(list_volumes=lambda: [], pid_alive=lambda pid: False,
                 hostname=lambda: "testhost", now=lambda: 1_790_000_000.0)


@pytest.fixture
def card(tmp_path) -> FakeSdVolume:
    return FakeSdVolume(tmp_path / "sd")


def _install(tmp_path, card, *, card_txt: bytes | None, bundle: bytes, ebf: str = "", **kw):
    board = card.root / "MB" / "HBI0309C" / "board.txt"
    if card_txt is None:
        board.unlink()
    else:
        board.write_bytes(card_txt)
    if ebf:
        (card.root / "MB" / "HBI0309C" / ebf).write_bytes(b"BIOS")
    storage = Mps3Storage(str(card.root), env=_env())
    rec = storage.backup(tmp_path / "bk")
    src = tmp_path / "bundle" / "board.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(bundle)
    said: list = []

    def progress(phase, done, total, detail=None):
        said.append((phase, (detail or {}).get("text", "")))

    progress.takes_detail = True                              # type: ignore[attr-defined]
    storage.install({"MB/HBI0309C/board.txt": src}, backup=rec, progress=progress, **kw)
    return storage, board, said


def test_install_keeps_the_cards_line_and_says_so(tmp_path, card):
    storage, board, said = _install(tmp_path, card, card_txt=CARD, bundle=BUNDLE)
    assert board.read_bytes() == CARD
    assert storage.install_notes == ["MBBIOS kept: mbb_v132.ebf"]
    assert ("mbbios", "MBBIOS kept: mbb_v132.ebf") in said


def test_twin_install_with_no_line_writes_the_bundles_and_says_where_it_came_from(tmp_path, card):
    storage, board, _ = _install(tmp_path, card, card_txt=None, bundle=BUNDLE)
    assert board.read_bytes() == BUNDLE                        # no mbb_v141.ebf on this card
    assert storage.install_notes[0].startswith("MBBIOS: mbb_v141.ebf from the bundle")


def test_install_refuses_the_mcc_update_before_writing_and_allow_overrides(tmp_path, card):
    with pytest.raises(RefusedError, match="update itself to mbb_v141.ebf"):
        _install(tmp_path, card, card_txt=NO_LINE, bundle=BUNDLE, ebf="mbb_v141.ebf")
    assert (card.root / "MB" / "HBI0309C" / "board.txt").read_bytes() == NO_LINE  # untouched
    assert not (card.root / ".harness-manager-journal.json").exists()           # never started
    storage, board, _ = _install(tmp_path / "again", FakeSdVolume(tmp_path / "sd2"),
                                 card_txt=NO_LINE, bundle=BUNDLE, ebf="mbb_v141.ebf",
                                 allow_mcc_update=True)
    assert board.read_bytes() == BUNDLE and "allowed by --allow-mcc-update" in \
        storage.install_notes[0]


# --- the A/B view never writes board.txt, and an MBBIOS-only difference no longer blocks it ------


def test_the_ab_view_measures_board_txt_after_the_substitution(tmp_path, card):
    from harness_manager_mps3.sd_ab import AbStorage

    (card.root / "MB" / "HBI0309C" / "board.txt").write_bytes(CARD)
    ab = AbStorage(Mps3Storage(str(card.root), env=_env()))
    bundle = tmp_path / "board.txt"
    bundle.write_bytes(BUNDLE)                                 # differs from the card by MBBIOS
    seen: dict = {}
    ab._install = lambda files, **kw: seen.update(           # the A/B write itself
        board=Path(files["MB/HBI0309C/board.txt"]).read_bytes())
    ab.install({"MB/HBI0309C/board.txt": bundle}, backup=SimpleNamespace(path="x"))
    assert seen["board"] == CARD
    assert ab.install_notes == ["MBBIOS kept: mbb_v132.ebf"]


def test_twin_the_ab_view_refuses_the_mcc_update_too(tmp_path, card):
    from harness_manager_mps3.sd_ab import AbStorage

    (card.root / "MB" / "HBI0309C" / "board.txt").write_bytes(NO_LINE)
    (card.root / "MB" / "HBI0309C" / "mbb_v141.ebf").write_bytes(b"BIOS")
    ab = AbStorage(Mps3Storage(str(card.root), env=_env()))
    bundle = tmp_path / "board.txt"
    bundle.write_bytes(BUNDLE)
    ab._install = lambda files, **kw: pytest.fail("nothing may be written")
    with pytest.raises(RefusedError, match="update itself"):
        ab.install({"MB/HBI0309C/board.txt": bundle}, backup=SimpleNamespace(path="x"))


# --- item 2: the MCC firmware version's leading v ----------------------------------------------------


def test_the_boards_v132_is_the_releases_132():
    assert mcc_version("v1.3.2") == mcc_version("1.3.2") == mcc_version(" V1.3.2 ") == "1.3.2"
    assert mcc_fw_tested("v1.3.2", ("1.3.2",)) and mcc_fw_tested("1.3.2", ("v1.3.2",))


def test_twin_a_really_different_version_still_differs():
    assert not mcc_fw_tested("v1.4.1", ("1.3.2",)) and not mcc_fw_tested("1.3.20", ("1.3.2",))
    assert mcc_version("vv1.3.2") != mcc_version("1.3.2")      # one leading v only
