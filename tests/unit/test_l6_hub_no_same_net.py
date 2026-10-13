"""Lane L6: a board behind a hub gets no "this PC must be on the same /24" note."""

from __future__ import annotations

from harness_manager.services import identity_assign as IA
from harness_manager.services.board_identity import IdentityService
from harness_manager.services.bringup import propose_identity


def _policy():
    return IA.IdentityPolicy(pack="mps3", reserved_mac_prefixes=("02:00:00",),
                             ip_pool="192.168.10.110-199")


def test_same_net_note_is_for_direct_boards_only():
    assert "same /24" in IA.same_net_note("192.168.10.117/24")
    assert IA.same_net_note("192.168.10.117/24", hub=True) == ""          # twin


def test_address_out_omits_the_note_for_a_hub_board():
    direct = IdentityService._address_out("192.168.10.117/24")
    assert "same /24" in direct["same_net"] and direct["pc_example"]
    hubbed = IdentityService._address_out("192.168.10.117/24", hub=True)
    assert hubbed["same_net"] == "" and hubbed["pc_example"] == ""
    assert hubbed["ip"] == direct["ip"]


def test_a_proposal_for_a_hub_board_has_no_note():
    assert "same /24" in propose_identity("ABCD1234", policy=_policy())["same_net"]
    assert propose_identity("ABCD1234", policy=_policy(), hub=True)["same_net"] == ""


def test_the_pages_show_the_note_only_when_there_is_one():
    from pathlib import Path
    js = Path(__file__).resolve().parents[2] / "src/harness_manager/web/static/js"
    ident = (js / "sections/identity.js").read_text()
    assert ident.count("address.same_net ?") == 2
    assert "p.ip && p.same_net" in (js / "bringup.js").read_text()
