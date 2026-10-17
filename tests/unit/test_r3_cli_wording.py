"""Release 3 remote run on board 1 (16 Oct, transcript docs/evidence/2026-10-mint4/lease): four
CLI papercuts. Every case has its negative twin."""

from __future__ import annotations

import pytest

from harness_manager.cli.cmd_claim import describe
from harness_manager.cli.cmd_hub import _human_view
from harness_manager.cli.main import make_parser
from harness_manager.core.errors import UsageError


def test_board_keys_takes_its_target_once():
    a = make_parser().parse_args(["board", "keys", "192.0.2.1", "list"])
    assert a.keys_cmd == "list" and "192.0.2.1" in vars(a).values()


def test_twin_a_second_target_is_an_error_not_required():
    with pytest.raises(UsageError, match="unrecognized arguments: 192.0.2.1"):
        make_parser().parse_args(["board", "keys", "192.0.2.1", "list", "192.0.2.1"])


def test_lease_help_no_longer_says_it_needs_a_hub():
    sub = next(a for a in make_parser()._actions if getattr(a, "choices", None)
               and "lease" in a.choices)
    text = sub.choices["lease"].description
    assert "It needs a hub" not in text and "single-user mode" in text


MINE = {"state": "mine", "live": True,
        "claimed": {"key_fp": "SHA256:mine", "by": "dam1n19@srv03335", "at": "2026-10-01"}}


def test_claim_status_names_the_owner_when_the_first_key_is_someone_else_s():
    line = describe({**MINE, "claim_key": "SHA256:owner"})
    assert line.startswith("your key is on the board") and "not its owner" in line
    assert "SHA256:owner" in line and "claimed by you" not in line


def test_twin_when_the_first_key_is_yours_it_says_claimed_by_you():
    assert describe({**MINE, "claim_key": "SHA256:mine"}).startswith("claimed by you")
    assert describe(dict(MINE)).startswith("claimed by you")          # first key not published


def _view(mine: bool) -> dict:
    return {"lease": {"holder": "david@mapstone-dev", "user": "david",
                      "expires_at": "2026-10-16T20:51:39Z", "mine": mine},
            "queue": [], "requests": []}


def test_a_shared_hub_account_s_lease_says_whose_yours_means():
    line = _human_view("mps3-01", "mps3_01_pl", _view(True), 0.0)[0]
    assert "yours (this hub account" in line


def test_twin_someone_else_s_lease_is_not_yours():
    assert "yours" not in _human_view("mps3-01", "mps3_01_pl", _view(False), 0.0)[0]
