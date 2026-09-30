"""FIX-PACK-4: the one lease rule (``services.lease.held_here``) every gate uses.

``mine`` is by principal, so it is also true when ANOTHER session of your own hub name holds
the lease (every lab session is david@mapstone-dev); ``here`` is this Harness Manager holding
the token. The gates (XVC, harness installs) go by ``here``; a lease from before ``here``
reads ``mine``, as the app's ``leaseWho`` does. Pure functions: no hub, no board.
"""

from __future__ import annotations

from harness_manager.services.lease import elsewhere_text, held_here


def test_held_here_is_the_token_not_the_principal():
    assert held_here({"holder": "d@m", "mine": True, "here": True}) is True
    assert held_here({"holder": "d@m", "mine": True, "here": False}) is False
    assert elsewhere_text({"holder": "d@m", "mine": True, "here": False}, "mps3_01") == \
        "d@m holds mps3_01 in another session, not this Harness Manager"


def test_negative_twin_no_lease_someone_else_and_a_view_without_here():
    assert held_here(None) is False and held_here({}) is False
    assert held_here({"holder": "alice@lab", "mine": False, "here": False}) is False
    assert held_here({"holder": "d@m", "mine": True}) is True          # before `here`: mine
    assert held_here({"holder": "alice@lab", "mine": False}) is False
    assert elsewhere_text({"holder": "alice@lab", "mine": False}, "mps3_01") == "alice@lab holds mps3_01"
