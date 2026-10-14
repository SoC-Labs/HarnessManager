"""Single-user mode UI (the board's own lease), statically: the two api.js routes, the strings
in ONE exported object (lease_words.js), the "Who can use this board" card, the lease texts that
say "the board" instead of "the hub", the touchscreen notice and the Access card. Each check
has a negative twin fed a bad input."""

from __future__ import annotations

import re

from tests.fakes.t14_api_contract import STATIC, parse_ui_endpoints

JS = STATIC / "js"


def read(*parts: str) -> str:
    return JS.joinpath(*parts).read_text(encoding="utf-8")


def words_block(js: str) -> str:
    return js.split("export const LEASE_WORDS", 1)[1]


def has_string(block: str, text: str) -> bool:
    return text in block


def test_mode_endpoints_are_in_api_js():
    table = parse_ui_endpoints(read("api.js"))
    assert table["leaseMode"] == ("GET", "/boards/{}/lease/mode")
    assert table["leaseModeSet"] == ("POST", "/boards/{}/lease/mode")


def test_every_spec_string_is_in_the_one_words_object():
    block = words_block(read("lease_words.js"))
    for text in (
        "Who can use this board", "Shared", "One person at a time",
        "One person at a time: whoever holds the lease can program, reset and debug. Everyone else "
        "can watch and ask for it. Someone at the board can always switch it off on the touchscreen.",
        "Claim the board first (SSH claim, above): the lease needs to know whose key is whose.",
        "Behind a hub, the hub's lease already does this.",
        "This board's release can't do single-user mode.",
        "Switched off at the board's touchscreen; it can be switched on again in",
        "You take the lease now (Yours · ", "renewed while the board is open here). Others with a key "
        "can still watch it, see that you have it, and ask for it. Someone at the board can always "
        "switch it off on the touchscreen.",
        "Switch and take the lease", "Switch to shared",
        "Anyone with a key can drive it again, at the same time as others.",
        "was ended at the board", "someone switched off single-user mode on the touchscreen. "
        "The board is shared again.",
        "Source: the board's own lease (single-user mode)", "Shared", "a desk board on this network",
        "Courtesy lock: it keeps people out of each other's way",
    ):
        assert has_string(block, text), text


def test_words_check_has_a_negative_twin():
    assert not has_string(words_block(read("lease_words.js")), "Hub lease for all")


def test_courtesy_label_matches_the_backend():
    from harness_manager.services.lease_backend import COURTESY_LABEL

    assert COURTESY_LABEL in words_block(read("lease_words.js"))


def test_single_user_card_is_built_from_seg_and_the_modal():
    claim = read("sections", "claim.js")
    assert re.search(r"function SingleUserCard\(", claim)
    assert re.search(r"<\$\{Seg\}[^>]*options=", claim)
    assert 'registerModal("lease_mode"' in claim
    assert 'runJob("leaseModeSet"' in claim and "confirm_board" in claim
    assert 'loadLeaseMode' in claim
    # the card follows the SSH claim card
    assert re.search(r"SshClaimCard\} bid=\$\{bid\} />\s*<\$\{SingleUserCard\}", claim)


def test_card_disabled_states_use_the_words():
    claim = read("sections", "claim.js")
    for key in ("W.behindHub", "W.noFeature", "W.unclaimed", "W.localHold", "W.notOwner"):
        assert key in claim, key
    assert not re.search(r"W\.unclaimed[^;]*\.toUpperCase", claim)


def test_loadhub_treats_server_as_a_lease_and_serverword_exists():
    week = read("week.js")
    assert "export function serverWord" in week
    assert "lease.server" in week and "boardShared" in week
    assert "export async function loadLeaseMode" in week
    assert 'call("leaseMode"' in week


def test_lease_texts_use_serverword_not_a_literal_hub():
    lease = read("lease.js")
    assert "serverWord(" in lease
    assert "Give the hub lease back" not in lease
    assert "This board has no hub lease" not in lease
    assert "another hub user" not in lease


def test_taken_banner_has_the_touchscreen_text():
    lease = read("lease.js")
    assert "takenAtBoard" in lease and '"someone at the board"' in lease
    assert 'ev.topic === "lease.mode"' in lease


def test_overview_access_card_both_modes():
    ov = read("sections", "overview.js")
    assert "LEASE_WORDS.ovShared" in ov and "LEASE_WORDS.ovOneLink" in ov
    assert "LEASE_WORDS.ovSourceBoard" in ov
    assert 'navigate(bid, "board/access")' in ov


def test_only_vendored_icons_in_the_new_code():
    icons = (STATIC / "vendor" / "lucide" / "icons.js").read_text(encoding="utf-8")
    claim = read("sections", "claim.js").split("Who can use this board", 1)[1]
    for name in set(re.findall(r'icon="([a-z0-9-]+)"', claim)):
        assert f'"{name}"' in icons or f"{name}:" in icons or f"'{name}'" in icons, name
