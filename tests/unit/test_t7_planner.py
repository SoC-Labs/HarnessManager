"""T7: the planner compares the board with the channel. Pure: no board, no network."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.core.model import BoardIdentity
from harness_manager.services.update.planner import (
    MODE_FULL,
    MODE_NONE,
    MODE_OVERLAYS,
    BoardView,
    make_plan,
)
from harness_manager.services.update.schema import parse_channel
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.t7_bundles import (
    FIELDED_HARNESS,
    FIELDED_STATIC,
    NEW_STATIC,
    NEW_USERCODE,
    Release,
)

KEYS = TestKeys()


def channel(tmp_path, *releases: Release, **kw):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    for i, r in enumerate(releases):
        r.add_to(b, tmp_path / "art", current=(i == len(releases) - 1), **kw)
    return parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))


def board(*, shell=FIELDED_STATIC, harness=FIELDED_HARNESS, storage=True, controller=True,
          known=True, revs=("HBI0309C",), os_slots=False, pack="mps3") -> BoardView:
    ident = BoardIdentity(board_type="mps3", shell_id=shell, harness_version=harness) if known \
        else None
    return BoardView(board_id="mps3@test", pack=pack, identity=ident, identity_known=known,
                     has_storage=storage, has_controller=controller, has_os_slots=os_slots,
                     sd_revisions=revs)


def test_a_firmware_update_on_the_same_shell(tmp_path):
    plan = make_plan(channel(tmp_path, Release("1.0.0"), Release("1.1.0")), board(),
                     app_version="0.1.0")
    assert plan.mode == MODE_FULL and plan.base and not plan.rekey and not plan.blockers
    assert plan.running_release == "1.0.0" and plan.version == "1.1.0"
    assert [s.action for s in plan.steps] == ["download", "verify", "store-overlays", "backup-sd",
                                              "install-sd", "reboot", "confirm-identity"]


def test_an_up_to_date_board_with_its_overlays_stored_has_nothing_to_do(tmp_path):
    ch = channel(tmp_path, Release("1.1.0"))
    stored = [c.asset.sha256 for c in ch.harness_release().components if c.target == "host-store"]
    plan = make_plan(ch, board(harness="1.1.0"), app_version="0.1.0", stored_components=stored)
    assert plan.mode == MODE_NONE and plan.up_to_date and not plan.steps


def test_an_up_to_date_board_gets_new_overlays_only(tmp_path):
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(harness="1.1.0"),
                     app_version="0.1.0")
    assert plan.mode == MODE_OVERLAYS and not plan.base
    assert [s.action for s in plan.steps] == ["download", "verify", "store-overlays"]
    assert plan.components == ["overlays-open"]


def test_a_rekey_needs_typed_consent_and_lists_what_breaks(tmp_path):
    ch = channel(tmp_path, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE),
                 rekey=True)
    stored = [{"rm_name": "nanosoc", "rm_id": "0x01000001", "static_id": FIELDED_STATIC},
              {"rm_name": "other", "rm_id": "0x01000002", "static_id": "0x12345678"}]
    plan = make_plan(ch, board(), app_version="0.1.0", stored_overlays=stored)
    assert plan.rekey and plan.consent_phrase == f"REKEY {NEW_STATIC}"
    assert any("nanosoc" in u for u in plan.unusable)
    assert not any("other" in u for u in plan.unusable)
    with pytest.raises(RefusedError, match="RE-KEYS"):
        plan.approve()
    with pytest.raises(RefusedError, match="type exactly"):
        plan.approve(consent="yes")
    assert plan.approve(consent=f"REKEY {NEW_STATIC}").consent == f"REKEY {NEW_STATIC}"


def test_an_unflagged_static_change_is_still_a_rekey(tmp_path):
    ch = channel(tmp_path, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE))
    plan = make_plan(ch, board(), app_version="0.1.0")
    assert plan.rekey and any("does not mark" in w for w in plan.warnings)


def test_an_ordinary_update_needs_no_consent(tmp_path):
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(), app_version="0.1.0")
    assert plan.approve().fingerprint == plan.fingerprint()


@pytest.mark.parametrize("kw,match", [
    ({"storage": False}, "Debug USB"),
    ({"controller": False}, "REBOOT"),
    ({"revs": ("HBI0309B",)}, "config SD is for HBI0309B"),
    ({"pack": "kr260"}, "for mps3 boards"),
])
def test_blockers(tmp_path, kw, match):
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(**kw), app_version="0.1.0")
    assert any(match in b for b in plan.blockers), plan.blockers
    with pytest.raises(RefusedError, match="cannot run"):
        plan.approve()


def test_an_app_too_old_for_the_release_is_blocked(tmp_path):
    ch = channel(tmp_path, Release("1.1.0"), compat={"min_app": "9.0.0"})
    plan = make_plan(ch, board(), app_version="0.1.0")
    assert any("update app" in b for b in plan.blockers)


def test_a_withdrawn_release_is_blocked(tmp_path):
    ch = channel(tmp_path, Release("1.0.0"), Release("1.1.0"))
    ch.harness[1].__dict__["status"] = "withdrawn"      # 1.0.0 (frozen dataclass: test only)
    plan = make_plan(ch, board(harness="1.1.0"), app_version="0.1.0", version="1.0.0")
    assert any("withdrawn" in b for b in plan.blockers)


def test_a_board_running_a_withdrawn_release_is_warned(tmp_path):
    ch = channel(tmp_path, Release("1.0.0"), Release("1.1.0"))
    ch.harness[1].__dict__["status"] = "withdrawn"
    plan = make_plan(ch, board(), app_version="0.1.0")
    assert any("WITHDRAWN" in w for w in plan.warnings)


def test_a_newer_board_is_not_downgraded_by_default(tmp_path):
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(harness="1.2.0"),
                     app_version="0.1.0")
    assert not plan.base and any("newer than" in w for w in plan.warnings)


def test_an_explicit_older_version_is_a_rollback_from_signed_history(tmp_path):
    ch = channel(tmp_path, Release("1.0.0"), Release("1.1.0"))
    plan = make_plan(ch, board(harness="1.1.0"), app_version="0.1.0", version="1.0.0")
    assert plan.base and any("ROLLBACK" in w for w in plan.warnings)


def test_an_unreadable_board_needs_rekey_consent_because_it_might_be_one(tmp_path):
    # The running static_id is unknown, so the install MAY re-key: typed consent, always.
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(known=False), app_version="0.1.0")
    assert plan.base and plan.rekey and any("cannot be CONFIRMED" in w for w in plan.warnings)
    assert any("may re-key" in w for w in plan.warnings)
    with pytest.raises(RefusedError, match="type exactly"):
        plan.approve()
    plan.approve(consent=plan.consent_phrase)


def test_a_board_reporting_an_odd_version_string_does_not_crash_the_planner(tmp_path):
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(harness="1.0.0-dirty"),
                     app_version="0.1.0")
    assert plan.base and not plan.rekey


def test_overlays_only_on_a_rekeyed_release_is_blocked(tmp_path):
    ch = channel(tmp_path, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE))
    plan = make_plan(ch, board(), app_version="0.1.0", overlays_only=True)
    assert any("install the harness first" in b for b in plan.blockers) and not plan.rekey


def test_overlays_only_on_the_same_shell(tmp_path):
    plan = make_plan(channel(tmp_path, Release("1.1.0")), board(), app_version="0.1.0",
                     overlays_only=True)
    assert plan.mode == MODE_OVERLAYS and not plan.base and not plan.blockers


def test_private_components_need_a_token(tmp_path):
    ch = channel(tmp_path, Release("1.1.0", private_overlays=True))
    without = make_plan(ch, board(), app_version="0.1.0")
    assert "overlays-aaa" in without.skipped and "overlays-aaa" not in without.components
    with_token = make_plan(ch, board(), app_version="0.1.0", have_token=True)
    assert "overlays-aaa" in with_token.components


def test_an_os_image_needs_an_os_slot_capable_harness(tmp_path):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    rel = Release("1.2.0", impl="linux", with_sd=False, with_overlays=False)
    comps = [b.component("os", "user-usd", AssetFile("os.s0", b"S0LB" + b"\x00" * 60),
                         kind="os-slot")]
    b.add_harness(rel.version, rel.identity(), comps)
    ch = parse_channel(b.document(channel="stable", serial=1, key=KEYS.release))
    plan = make_plan(ch, board(), app_version="0.1.0")
    assert plan.os_slot and any("OS slot" in x for x in plan.blockers)
    ok = make_plan(ch, board(os_slots=True), app_version="0.1.0")
    assert ok.os_slot and not ok.blockers and ok.reboot_wait_s == 180.0
    assert make_plan(channel(tmp_path, Release("1.1.0")), board(), app_version="0.1.0").reboot_wait_s is None
    assert "write-os-slot" in [s.action for s in ok.steps]


def test_the_fingerprint_follows_the_plan(tmp_path):
    ch = channel(tmp_path, Release("1.0.0"), Release("1.1.0"))
    a = make_plan(ch, board(), app_version="0.1.0")
    b = make_plan(ch, board(harness="0.9.0"), app_version="0.1.0")
    assert a.fingerprint() != b.fingerprint()
