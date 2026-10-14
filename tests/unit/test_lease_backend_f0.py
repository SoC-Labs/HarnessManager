"""F0: the lease seam's frozen names and rules (HM lanes A-D build on them)."""
from types import SimpleNamespace as NS

from harness_manager.services import lease_backend as lb


def test_lease_of_prefers_session_lease_then_hub():
    hub, board = NS(kind="hub"), NS(kind="board")
    assert lb.lease_of(NS(lease=board, hub=hub)) is board
    assert lb.lease_of(NS(hub=hub)) is hub
    assert lb.lease_of(NS()) is None


def test_words_and_kind_default_to_hub():
    assert lb.server_word(NS()) == "the hub"
    assert lb.server_word(NS(kind="board")) == "the board"


def test_lease_required_rules():
    assert lb.lease_required(None) is False
    assert lb.lease_required(NS(kind="hub")) is True
    on = NS(kind="board", mode=lambda: lb.LeaseMode(single_user=True))
    off = NS(kind="board", mode=lambda: lb.LeaseMode(single_user=False))
    unknown = NS(kind="board", mode=lambda: None)
    assert lb.lease_required(on) is True
    assert lb.lease_required(off) is False and lb.lease_required(unknown) is False
