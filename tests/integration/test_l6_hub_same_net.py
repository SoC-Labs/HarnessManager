"""Lane L6: the Name dialog's proposal for a hub board has no same-/24 note."""

from __future__ import annotations

from tests.integration.test_identity_name import lab  # noqa: F401  (the fixture)


def test_proposal_behind_a_hub_has_no_same_net_note(lab):  # noqa: F811
    fake, session, svc = lab(hub=True)
    svc.status(session, refresh=True)
    p = svc.propose(session, mac="keep", ip="keep")
    assert p["address"]["same_net"] == "" and p["address"]["pc_example"] == ""


def test_twin_a_board_with_no_hub_keeps_the_note(lab):  # noqa: F811
    fake, session, svc = lab()
    svc.status(session, refresh=True)
    p = svc.propose(session, label="lab-07")
    assert "same /24" in p["address"]["same_net"]
