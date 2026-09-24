"""H2 (HARNESS-DIST §3.2): which release a board runs, and whether an install took, by the
wire identity with the firmware sha decisive. Pure: no board, no network.

The fielded lineage in one line: every firmware since v0.8 reports ``harness=1.0.0``, so
v1.1.0 (fw d68dd0ed) and a v1.1.1 re-bake (fw 0e12a0b0) on the SAME static and usercode
differ only in the firmware sha. Every test here has a negative twin.
"""

from __future__ import annotations

import pytest

from harness_manager.core.model import BoardIdentity, Check
from harness_manager.services.update.executor import (
    confirm_identity,
    confirm_wire_identity,
    journaled_release_runs,
)
from harness_manager.services.update.planner import (
    BoardView,
    base_differs,
    fw_sha_match,
    make_plan,
    match_release,
)
from harness_manager.services.update.schema import (
    BoardSpec,
    Channel,
    Compat,
    HarnessIdentity,
    HarnessRelease,
    parse_channel,
)
from tests.fakes.fake_channel import ChannelBuilder, TestKeys
from tests.fakes.t7_bundles import Release

KEYS = TestKeys()
S_ILA, U_ILA = "0x72bb0a36", "0xc8551081"
S_OLD = "0x3f1a560f"
FW_110, FW_111 = "d68dd0ed", "0e12a0b0"


def rel(version: str, fw_sha: str = "", *, harness: str = "1.0.0", static: str = S_ILA,
        usercode: str = U_ILA, impl: str = "bare-metal", status: str = "superseded"
        ) -> HarnessRelease:
    return HarnessRelease(version=version, status=status,
                          identity=HarnessIdentity(static_id=static, usercode=usercode,
                                                   harness=harness, impl=impl, fw_sha=fw_sha),
                          compat=Compat(), components=())


def chan(*releases: HarnessRelease) -> Channel:
    return Channel(channel="stable", serial=1, issued_at="2026-09-24T00:00:00Z",
                   signing_key_id="0" * 16, board=BoardSpec(pack="mps3"),
                   harness_current=releases[-1].version, harness=releases, app_current="", app=())


def board(sha: str = "", *, harness: str = "1.0.0", static: str = S_ILA, usercode: str = "",
          impl: str = "") -> BoardIdentity:
    return BoardIdentity(board_type="mps3", shell_id=static, harness_version=harness,
                         firmware_sha=sha, usercode=usercode, harness_impl=impl)


LINEAGE = chan(rel("1.0.0", "cb31b0f2", static=S_OLD, usercode="0xd46fcdcb"),
               rel("1.1.0", FW_110), rel("1.1.1", FW_111))


# --- fw_sha_match -----------------------------------------------------------------------


@pytest.mark.parametrize("want,have,expect", [
    ("d68dd0ed", "d68dd0ed", True),
    ("d68dd0ed4c1f00aa11223344556677889900aabb", "d68dd0ed", True),   # full sha vs 8 hex
    ("D68DD0ED", "d68dd0ed", True),                                     # case-blind
    ("d68dd0ed", "0e12a0b0", False),
    ("d68dd0ed4c1f", "d68dd0ed4c2f", False),                            # the common prefix, all of it
    ("abc", "abcdef01", False),                                         # under 7 hex: must be equal
    ("", "d68dd0ed", None), ("d68dd0ed", "", None),                     # nothing to decide with
])
def test_fw_sha_match(want, have, expect):
    assert fw_sha_match(want, have) is expect


# --- which release the board runs (spike P2) ---------------------------------------------


def test_two_releases_that_both_say_1_0_0_are_told_apart_by_the_firmware_sha():
    assert match_release(LINEAGE, board(FW_110)).version == "1.1.0"
    # twin: the other sha names the other release, whatever the harness string says
    assert match_release(LINEAGE, board(FW_111)).version == "1.1.1"


def test_a_wrong_firmware_sha_matches_no_release():
    assert match_release(LINEAGE, board("deadbeef")) is None
    assert base_differs(LINEAGE.harness[1], board("deadbeef"))
    # twin: the right sha is the same base, so there is nothing to write
    assert not base_differs(LINEAGE.harness[1], board(FW_110))


def test_a_matching_sha_on_another_static_is_no_match():
    assert match_release(LINEAGE, board(FW_110, static=S_OLD)) is None
    assert match_release(LINEAGE, board(FW_110, static=S_ILA)).version == "1.1.0"


def test_a_tag_in_identity_harness_does_not_stop_the_sha_from_matching():
    tagged = chan(rel("1.1.0", FW_110, harness="1.1.0"), rel("1.1.1", FW_111, harness="1.1.1"))
    assert match_release(tagged, board(FW_110)).version == "1.1.0"
    assert not base_differs(tagged.harness[0], board(FW_110))
    # twin: a matching version string never rescues a differing sha
    assert match_release(tagged, board("deadbeef", harness="1.1.0")) is None
    assert base_differs(tagged.harness[0], board("deadbeef", harness="1.1.0"))


def test_older_records_without_fw_sha_fall_back_to_the_harness_string():
    old = chan(rel("1.0.0", harness="1.0.0"), rel("1.1.0", harness="1.1.0"))
    assert match_release(old, board(FW_110, harness="1.1.0")).version == "1.1.0"
    assert not base_differs(old.harness[1], board(FW_110, harness="1.1.0"))
    # twin: without a sha the version string must agree
    assert match_release(old, board(FW_110, harness="1.2.0")) is None
    assert base_differs(old.harness[1], board(FW_110, harness="1.2.0"))


def test_a_board_that_reports_no_sha_falls_back_to_the_harness_string():
    assert match_release(chan(rel("1.0.0", FW_110, harness="1.0.0"),
                              rel("1.1.0", FW_111, harness="1.1.0")),
                         board("", harness="1.1.0")).version == "1.1.0"
    # twin: the fielded lineage says 1.0.0 for both, so it cannot be told: unrecorded
    assert match_release(LINEAGE, board("")) is None


def test_the_sha_outranks_a_release_without_one():
    mixed = chan(rel("1.0.9", harness="1.0.0"), rel("1.1.0", FW_110))
    assert match_release(mixed, board(FW_110)).version == "1.1.0"
    # twin: with a sha that names neither, only the sha-less record can still fit
    assert match_release(mixed, board("deadbeef")).version == "1.0.9"


def test_two_releases_of_the_same_firmware_are_ambiguous_unless_the_version_decides():
    same = chan(rel("1.1.0", FW_110), rel("1.1.2", FW_110))            # an overlays-only release
    assert match_release(same, board(FW_110)) is None
    # twin: when the harness strings differ, the one the board reports wins the tie
    told = chan(rel("1.1.0", FW_110, harness="1.1.0"), rel("1.1.2", FW_110, harness="1.1.2"))
    assert match_release(told, board(FW_110, harness="1.1.2")).version == "1.1.2"


@pytest.mark.parametrize("field,good,bad", [
    ("usercode", {"usercode": U_ILA}, {"usercode": "0x0badc0de"}),
    ("impl", {"impl": "bare-metal"}, {"impl": "linux"}),
])
def test_usercode_and_impl_must_agree_when_both_sides_say(field, good, bad):
    assert match_release(LINEAGE, board(FW_110, **good)).version == "1.1.0"
    assert match_release(LINEAGE, board(FW_110, **bad)) is None
    assert base_differs(LINEAGE.harness[1], board(FW_110, **bad))


# --- confirm after an install (spike P6) --------------------------------------------------


def test_a_1_1_x_release_confirms_by_its_sha_although_the_firmware_says_1_0_0():
    want = rel("1.1.1", FW_111, harness="1.1.1")                      # the tag in identity.harness
    ok, items = confirm_identity(want, board(FW_111))
    checks = {i.name: i.check for i in items}
    assert ok and checks["firmware sha"] == Check.OK
    assert checks["harness version"] == Check.UNCHECKED               # shown, not decisive
    # twin: the old firmware came back (the SD was not reread): never confirmed
    ok, items = confirm_identity(want, board(FW_110))
    checks = {i.name: i.check for i in items}
    assert not ok and checks["firmware sha"] == Check.MISMATCH
    assert checks["harness version"] == Check.MISMATCH


def test_a_release_that_records_the_wire_string_confirms_by_both():
    ok, _ = confirm_identity(rel("1.1.1", FW_111), board(FW_111))
    assert ok
    ok, _ = confirm_identity(rel("1.1.1", FW_111), board(FW_111, static=S_OLD))
    assert not ok                                                     # twin: wrong static


def test_without_a_sha_the_harness_string_is_still_essential():
    want = rel("1.1.0", "", harness="1.1.0")
    assert confirm_identity(want, board("", harness="1.1.0"))[0]
    assert not confirm_identity(want, board("", harness="1.0.0"))[0]
    assert not confirm_identity(want, board("", harness=""))[0]       # nothing proves it


def test_a_sha_only_release_confirms_by_its_sha():
    want = rel("1.1.1", FW_111, harness="")
    assert confirm_identity(want, board(FW_111, harness=""))[0]
    assert not confirm_identity(want, board("", harness=""))[0]       # twin: nothing reported


def test_a_skewed_build_is_never_confirmed_even_with_the_right_sha():
    skewed = BoardIdentity(board_type="mps3", shell_id=S_ILA, harness_version="1.0.0",
                           firmware_sha=FW_111, build_check=Check.MISMATCH)
    assert not confirm_wire_identity(rel("1.1.1", FW_111).identity, skewed)[0]
    assert confirm_wire_identity(rel("1.1.1", FW_111).identity, board(FW_111))[0]


# --- an interrupted update's journal -------------------------------------------------------


def test_a_journal_recognises_the_release_by_its_recorded_sha():
    j = {"version": "1.1.1", "static_id": S_ILA,
         "identity": {"harness": "1.1.1", "fw_sha": FW_111, "usercode": U_ILA,
                      "impl": "bare-metal"}}
    assert journaled_release_runs(j, board(FW_111))
    assert not journaled_release_runs(j, board(FW_110))                # twin: the old firmware
    assert not journaled_release_runs(j, None)


def test_a_journal_from_before_the_identity_was_recorded_compares_the_version():
    j = {"version": "1.1.0", "static_id": S_ILA}
    assert journaled_release_runs(j, board(FW_110, harness="1.1.0"))
    assert not journaled_release_runs(j, board(FW_110, harness="1.0.0"))


# --- the plan: which release runs, and what "newer" means ------------------------------------


def _channel(tmp_path, *releases: Release):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    for i, r in enumerate(releases):
        r.add_to(b, tmp_path / "art", current=(i == len(releases) - 1))
    return parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))


def _view(sha: str) -> BoardView:
    ident = BoardIdentity(board_type="mps3", shell_id="0x3f1a560f", harness_version="1.0.0",
                          firmware_sha=sha)
    return BoardView(board_id="mps3@test", pack="mps3", identity=ident, has_storage=True,
                     has_controller=True, sd_revisions=("HBI0309C",))


def _lineage(tmp_path, current_last: str = "1.1.1"):
    r110 = Release("1.1.0", sha=FW_110, identity_harness="1.0.0", wire_harness="1.0.0")
    r111 = Release("1.1.1", sha=FW_111, identity_harness="1.0.0", wire_harness="1.0.0")
    return _channel(tmp_path, *((r111, r110) if current_last == "1.1.0" else (r110, r111)))


def test_the_plan_names_the_running_release_and_installs_the_newer_firmware(tmp_path):
    ch = _lineage(tmp_path)
    plan = make_plan(ch, _view(FW_110), app_version="0.1.0")
    assert plan.running_release == "1.1.0" and plan.version == "1.1.1" and plan.base
    step = next(s for s in plan.steps if s.action == "confirm-identity")
    assert FW_111 in step.detail
    # twin: a board already on 1.1.1's firmware has no base to write
    same = make_plan(ch, _view(FW_111), app_version="0.1.0")
    assert same.running_release == "1.1.1" and not same.base


def test_a_board_on_a_newer_release_is_not_downgraded_although_both_say_1_0_0(tmp_path):
    ch = _lineage(tmp_path, current_last="1.1.0")                     # current is 1.1.0
    plan = make_plan(ch, _view(FW_111), app_version="0.1.0")
    assert plan.running_release == "1.1.1" and not plan.base
    assert any("runs harness 1.1.1, newer than" in w for w in plan.warnings)
    # twin: asking for 1.1.0 by name is a rollback from the signed history
    back = make_plan(ch, _view(FW_111), app_version="0.1.0", version="1.1.0")
    assert back.base and any("ROLLBACK from harness 1.1.1 to 1.1.0" in w for w in back.warnings)


def test_a_withdrawn_newer_release_is_replaced_by_the_current_one(tmp_path):
    ch = _lineage(tmp_path, current_last="1.1.0")
    ch.harness_release("1.1.1").__dict__["status"] = "withdrawn"      # frozen: test only
    plan = make_plan(ch, _view(FW_111), app_version="0.1.0")
    assert plan.base and any("WITHDRAWN" in w for w in plan.warnings)
    assert not any("nothing to do" in w for w in plan.warnings)
