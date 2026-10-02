"""FIX-PACK-9 (the coordinator, 2 Oct): the platform's rc2 identify sends the board's own name
as ``label`` (firmware/identify/identify.c, net-proto v0.16), not ``name``, so the sidebar never
showed "MPS3-02". ``IdentifyReply.name`` reads ``label`` first, falls back to ``name``, both
through ``clean_name``, and is "" in rescue. The name reaches the display name as the
``harness`` source; boards.toml's own ``name`` (``config``) still outranks it. Each with its twin.
"""

from __future__ import annotations

import pytest

from harness_manager import naming
from harness_manager.core.model import Candidate
from harness_manager_mps3 import identify as ident
from tests.fakes.fake_identify import canonical_reply


def reply(**kw) -> ident.IdentifyReply:
    return ident.IdentifyReply(canonical_reply("n", **kw), ("10.0.0.5", 6899))


@pytest.mark.parametrize(("keys", "want"), [
    ({"label": "MPS3-02"}, "MPS3-02"),                       # label only (rc2)
    ({"name": "mps3-lab"}, "mps3-lab"),                      # name only (the proposed key)
    ({"label": "MPS3-01", "name": "other"}, "MPS3-01"),      # both: label first
    ({"label": "bad\x1b[2Jlabel", "name": "mps3-lab"}, "mps3-lab"),   # a bad label falls back
    ({}, ""),                                                # neither
])
def test_the_name_is_the_label_else_the_name(keys, want):
    r = reply(**keys)
    assert r.name == want and r.identity().name == want


@pytest.mark.parametrize("keys", [{"label": "MPS3-02"}, {"name": "mps3-lab"},
                                  {"label": "MPS3-01", "name": "other"}])
def test_twin_rescue_has_no_name(keys):
    r = reply(mode="rescue", reason="no bootable slot", **keys)
    assert r.is_rescue and r.name == "" and r.identity().name == ""


def test_the_label_names_the_sidebar_candidate():
    cand = reply(label="MPS3-02").candidate()
    named = naming.with_identity(cand, cand.identity)
    assert (named.name, named.name_source) == ("MPS3-02", naming.HARNESS)
    assert naming.display_name(named) == "MPS3-02"


def test_twin_boards_toml_name_still_wins_over_the_label():
    cand = reply(label="MPS3-02").candidate()
    configured = naming.offer(cand, "lab-board-a", naming.CONFIG)
    named = naming.with_identity(configured, cand.identity)
    assert (named.name, named.name_source) == ("lab-board-a", naming.CONFIG)
    bare = Candidate(pack="mps3", board_id="mps3@10.0.0.5:6900", links=())
    assert naming.display_name(naming.with_identity(bare, reply().identity())) == "10.0.0.5:6900"


# --- integ v1.1: the image-default label "MPS3" is no name -----------------------------------


def hub_named(cand: Candidate) -> Candidate:
    """The candidate as name_candidate leaves it once the hub target names the slot."""
    return naming.offer(cand, "mps3-01", naming.HUB_TARGET)


@pytest.mark.parametrize("label", ["MPS3", "mps3", "Mps3", " MPS3 "])
def test_the_image_default_label_is_unnamed_so_the_hub_target_names_the_board(label):
    cand = reply(label=label).candidate()
    named = hub_named(naming.with_identity(cand, cand.identity))
    assert (named.name, named.name_source) == ("mps3-01", naming.HUB_TARGET)
    assert naming.is_unnamed_label(label)
    bare = naming.with_identity(cand, cand.identity)
    assert naming.display_name(bare) == "10.0.0.5:6900"          # no hub: the address


@pytest.mark.parametrize("label", ["MPS3-02", "MPS3X", "MPS", "mps3-lab"])
def test_twin_a_real_name_still_outranks_the_hub(label):
    cand = reply(label=label).candidate()
    named = hub_named(naming.with_identity(cand, cand.identity))
    assert (named.name, named.name_source) == (label, naming.HARNESS)
    assert not naming.is_unnamed_label(label)


def test_twin_boards_toml_may_name_a_board_mps3():
    cand = reply(label="MPS3").candidate()
    named = naming.offer(cand, "MPS3", naming.CONFIG)
    assert (named.name, named.name_source) == ("MPS3", naming.CONFIG)


def test_the_unnamed_label_is_the_identity_services_default():
    from harness_manager.services.board_identity import DEFAULT_LABEL

    assert naming.UNNAMED_LABELS == {DEFAULT_LABEL.casefold()}
