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


# --- LeaseService.last_known: the preview's hub lease, no hub call --------------------------------


class _Hub:
    host = "mapstone-dev"
    target = "mps3_01_pl"
    client = None


def test_last_known_is_what_the_service_last_confirmed(tmp_path):
    from harness_manager.services.lease import LeaseService

    svc = LeaseService(tmp_path)
    hub = _Hub()
    svc._board_for(hub, "mps3@192.168.10.101:6900")               # track()'s record of the board
    svc._know(hub, "acquire", "david@mapstone-dev", "2026-10-01T12:00:00+00:00")
    known = svc.last_known("mps3@192.168.10.101:6900")
    assert known is not None
    assert (known["state"], known["holder"], known["here"], known["source"]) == \
        ("held", "david@mapstone-dev", True, "acquire")
    assert known["hub"] == "mapstone-dev" and known["expires_at"] == "2026-10-01T12:00:00+00:00"
    svc._know(hub, "release", None)
    assert svc.last_known("mps3@192.168.10.101:6900")["state"] == "free"


def test_negative_twin_a_board_never_read_or_whose_lease_ended_is_not_known(tmp_path):
    from harness_manager.services.lease import LeaseService

    svc = LeaseService(tmp_path)
    hub = _Hub()
    assert svc.last_known("mps3@192.168.10.101:6900") is None           # never seen
    svc._board_for(hub, "mps3@192.168.10.101:6900")
    assert svc.last_known("mps3@192.168.10.101:6900") is None           # tracked, never read
    svc._know(hub, "acquire", "david@mapstone-dev")
    with svc._mu:
        svc._known.clear()                                               # expired or lost here
    assert svc.last_known("mps3@192.168.10.101:6900") is None
