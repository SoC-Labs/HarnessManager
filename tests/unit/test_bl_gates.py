"""Board lease, seam + gates (HM-B): LeaseService and every lease gate serve hub leases AND
board leases through ``lease_backend.lease_of``. A minimal fake board adapter stands in for
``harness_manager_mps3.board_lease`` (HM-A builds the real one); no board, hub or SSH."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from harness_manager.core.errors import HeldError, RefusedError, UnavailableError
from harness_manager.daemon import drive_gate
from harness_manager.services import lease_backend as lb
from harness_manager.services.lease import LeaseService
from harness_manager.services.update.lease_gate import lease_state, require_lease

EXPIRES = "2099-01-01T12:34:00+00:00"


class FakeClient:
    """The ``HubClientLike`` verbs over one in-memory lease; the same shape for both kinds."""

    notes_supported = False
    notes_reason = "no notes in this fake"

    def __init__(self, principal: str = "hm") -> None:
        self._principal = principal
        self.holder = ""
        self.token = ""
        self.history: list[dict] = []
        self.down = False

    def principal(self) -> str:
        return self._principal

    def lease_show(self):
        if self.down:
            from harness_manager.core.errors import UnreachableError
            raise UnreachableError("no answer")
        return NS(held=bool(self.holder), holder=self.holder, user="", expires_at=EXPIRES,
                  queue=(), background_known=False, background_queue=(), tier="")

    def lease_acquire(self, holder, *, ttl, **_kw):
        self.holder, self.token = holder, "tok-1"
        return NS(token=self.token), EXPIRES

    def lease_heartbeat(self, token, holder):
        return EXPIRES

    def lease_release(self, token, holder):
        self.holder = self.token = ""

    def lease_cancel(self, holder):
        return False

    def lease_history(self):
        return list(self.history)


class Adapter:
    def __init__(self, kind: str, client: FakeClient, single_user: bool = True) -> None:
        self.kind, self.client = kind, client
        self.host = "hub.lab" if kind == "hub" else "192.168.10.101:6900"
        self.target = "mps3_01_pl"
        self.name = "mapstone" if kind == "hub" else "board-1"
        self.words = lb.WORDS[kind]
        self.single_user = single_user
        self.unknown_mode = False

    def mode(self):
        if self.kind == "hub" or self.unknown_mode:
            return None
        return lb.LeaseMode(single_user=self.single_user)


def make(kind: str, **kw) -> Adapter:
    return Adapter(kind, FakeClient(), **kw)


@pytest.fixture(params=["hub", "board"])
def adapter(request):
    return make(request.param)


def session(adapter):
    return NS(lease=adapter, hub=adapter if adapter.kind == "hub" else None)


# --- parametrised over a hub and a board: one behaviour ---------------------------------------


def test_acquire_view_release_are_the_same_for_hub_and_board(tmp_path, adapter):
    svc = LeaseService(tmp_path)
    assert svc.view(adapter)["lease"] is None
    out = svc.acquire(adapter, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    assert out["lease"]["holder"] == "hm"
    view = svc.view(adapter)
    assert view["lease"]["here"] is True and view["lease"]["mine"] is True
    svc.release(adapter, board_id="b1")
    assert svc.view(adapter)["lease"] is None


def test_the_gate_requires_the_lease_for_both_kinds(tmp_path, adapter):
    svc = LeaseService(tmp_path)
    st = lease_state(session(adapter), svc)
    assert st["required"] is True and st["here"] is False and st["holder"] == "nobody"
    with pytest.raises(HeldError):
        require_lease(session(adapter), svc, "program a design")
    svc.acquire(adapter, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    assert lease_state(session(adapter), svc)["here"] is True
    assert require_lease(session(adapter), svc, "program a design")["here"] is True


def test_someone_else_holds_it_refuses_with_their_name(tmp_path, adapter):
    adapter.client.holder = "alice@lab"
    svc = LeaseService(tmp_path)
    with pytest.raises(HeldError) as ei:
        require_lease(session(adapter), svc, "install")
    assert "alice@lab holds" in str(ei.value.message) and ei.value.holder == "alice@lab"


def test_a_server_that_does_not_answer_is_not_free(tmp_path, adapter):
    adapter.client.down = True
    st = lease_state(session(adapter), LeaseService(tmp_path))
    word = lb.server_word(adapter)
    assert st["holder"] == f"unknown ({word} did not answer)" and st["reason"]
    assert st["here"] is False


# --- board mode ---------------------------------------------------------------------------------


def test_board_mode_off_has_no_gate(tmp_path):
    b = make("board", single_user=False)
    st = lease_state(session(b), LeaseService(tmp_path))
    assert st == {"required": False, "mine": False, "here": False, "holder": "", "target": "",
                  "reason": ""}
    assert require_lease(session(b), LeaseService(tmp_path), "install")["required"] is False


def test_board_mode_unknown_is_not_required(tmp_path):
    b = make("board")
    b.unknown_mode = True
    assert lease_state(session(b), LeaseService(tmp_path))["required"] is False


def test_board_mode_on_gates_in_the_boards_words(tmp_path):
    b = make("board")
    b.client.down = True
    st = lease_state(session(b), LeaseService(tmp_path))
    assert st["required"] is True and "the board did not answer" in st["holder"]
    with pytest.raises(HeldError) as ei:
        require_lease(session(b), LeaseService(tmp_path), "install")
    assert "retry when the board answers" in ei.value.hint


def test_no_lease_server_at_all_is_open(tmp_path):
    assert lease_state(NS(hub=None), LeaseService(tmp_path))["required"] is False
    assert lease_state(None, None)["required"] is False


def test_view_carries_server_and_mode(tmp_path):
    svc = LeaseService(tmp_path)
    hub, board = make("hub"), make("board")
    hv, bv = svc.view(hub), svc.view(board)
    assert hv["server"] == {"kind": "hub", "host": "hub.lab", "name": "mapstone"}
    assert hv["mode"] is None
    assert bv["server"] == {"kind": "board", "host": "192.168.10.101:6900", "name": "board-1"}
    assert bv["mode"]["single_user"] is True and bv["mode"]["can_change"] is False
    assert svc.view(None)["server"] is None and svc.view(None)["mode"] is None


def test_board_mode_off_view_is_empty_with_the_boards_words(tmp_path):
    svc = LeaseService(tmp_path)
    v = svc.view(make("board", single_user=False))
    off = lb.WORDS["board"]["mode_off"]
    assert v["lease"] is None and v["notes_reason"] == off and v["revoke_reason"] == off
    assert v["background_reason"] == off and v["mode"]["single_user"] is False
    assert v["server"]["kind"] == "board"


def test_acquire_on_a_shared_board_says_why(tmp_path):
    with pytest.raises(UnavailableError) as ei:
        LeaseService(tmp_path).acquire(make("board", single_user=False), board_id="b1")
    assert lb.WORDS["board"]["mode_off"] in ei.value.message


def test_require_server_and_the_old_name(tmp_path):
    assert LeaseService.require_hub is LeaseService.require_server or \
        LeaseService.require_hub(1) == 1
    with pytest.raises(UnavailableError) as ei:
        LeaseService.require_server(None, "b1")
    assert "no hub, and single-user mode is off" in ei.value.message
    assert "not behind a hub" in ei.value.message


def test_keys_do_not_collide_between_a_hub_and_a_board(tmp_path):
    svc = LeaseService(tmp_path)
    hub, board = make("hub"), make("board")
    svc.acquire(hub, board_id="b1", ttl_s=60, holder="hm", heartbeat=False)
    assert svc.store.get(board.host, board.target) is None
    assert svc.store.get(hub.host, hub.target) is not None


def test_a_board_taken_entry_gives_lease_taken_by_someone_at_the_board(tmp_path):
    from harness_manager.core.events import EventBus

    bus, seen = EventBus(), []
    bus.subscribe("lease.taken", lambda ev: seen.append(ev.data))
    svc = LeaseService(tmp_path, bus)
    b = make("board")
    svc.acquire(b, board_id="b1", ttl_s=60, holder="hm", heartbeat=False)
    # the board ends the lease at the touchscreen; its history says so
    b.client.holder = b.client.token = ""
    b.client.history = [{"event": "lease.taken", "reason": "switched to shared on the touchscreen",
                         "ts": "2099-01-01T12:00:00+00:00"}]
    v = svc.view(b)                 # nobody holds it now, and our stored lease is gone
    assert v["lease"] is None
    b.client.holder = "someone"     # the board shows a new holder: ours is lost
    svc.acquire(b, board_id="b1", ttl_s=60, holder="hm", heartbeat=False)
    b.client.holder = "other@pc"
    b.client.history = [{"event": "lease.taken", "reason": "switched to shared on the touchscreen",
                         "ts": "2099-01-01T12:00:00+00:00"}]
    v = svc.view(b)
    assert v["taken"]["by"] == "someone at the board"
    assert "touchscreen" in v["taken"]["reason"]
    assert seen and seen[-1]["by"] == "someone at the board"


def test_negative_twin_a_hub_ignores_a_taken_entry(tmp_path):
    svc = LeaseService(tmp_path)
    h = make("hub")
    svc.acquire(h, board_id="b1", ttl_s=60, holder="hm", heartbeat=False)
    h.client.holder = "other@pc"
    h.client.history = [{"event": "lease.taken", "reason": "x", "ts": "2099-01-01T12:00:00+00:00"}]
    assert svc.view(h)["taken"] is None


def test_hub_overview_is_hub_only(tmp_path):
    with pytest.raises(UnavailableError):
        LeaseService(tmp_path).hub_overview("b", make("board"))


# --- drive gate ---------------------------------------------------------------------------------


def daemon(tmp_path):
    return NS(leases=LeaseService(tmp_path))


def test_drive_gate_board_words_and_no_reset_escape(tmp_path):
    b = make("board")
    b.client.holder = "alice@lab"
    d = daemon(tmp_path)
    with pytest.raises(HeldError) as ei:
        drive_gate.require_holder(d, "bid", session(b), "deploy")
    msg = ei.value.message
    assert msg.startswith("cannot program a design: board-1 is set to one person at a time "
                          "and alice@lab has it until ")
    assert "force" not in ei.value.hint
    # the hub's RESET phrase does not unlock a board: the board enforces its own lease
    with pytest.raises(HeldError):
        drive_gate.require_holder(d, "bid", session(b), "deploy",
                                  {"force": True, "consent": "RESET bid"})


def test_drive_gate_hub_keeps_the_reset_escape(tmp_path):
    h = make("hub")
    h.client.holder = "alice@lab"
    d = daemon(tmp_path)
    with pytest.raises(HeldError) as ei:
        drive_gate.require_holder(d, "bid", session(h), "deploy")
    assert "behind a hub" in ei.value.message and "RESET bid" in ei.value.hint
    assert drive_gate.require_holder(d, "bid", session(h), "deploy",
                                     {"force": True, "consent": "RESET bid"})["required"]
    with pytest.raises(RefusedError):
        drive_gate.require_holder(d, "bid", session(h), "deploy",
                                  {"force": True, "consent": "nope"})


def test_drive_gate_open_when_shared_or_ours(tmp_path):
    d = daemon(tmp_path)
    assert drive_gate.require_holder(d, "bid", session(make("board", single_user=False)),
                                     "deploy")["required"] is False
    b = make("board")
    d.leases.acquire(b, board_id="b1", ttl_s=60, holder="hm", heartbeat=False)
    assert drive_gate.require_holder(d, "bid", session(b), "deploy")["here"] is True
    assert drive_gate.require_holder(d, "bid", NS(), "deploy") is None


# --- XVC ---------------------------------------------------------------------------------------


def test_xvc_check_lease_goes_through_the_one_rule(tmp_path):
    from harness_manager.services.xvc import XvcService

    class Stub:
        _lease_service = XvcService._lease_service
        check_lease = XvcService.check_lease

        def __init__(self, leases):
            self.leases = leases

    b = make("board")
    stub = Stub(LeaseService(tmp_path))
    with pytest.raises(HeldError) as ei:
        stub.check_lease(session(b))
    assert "nobody holds" in ei.value.message
    stub.leases.acquire(b, board_id="b1", ttl_s=60, holder="hm", heartbeat=False)
    assert stub.check_lease(session(b)) == "hm"
    assert stub.check_lease(session(make("board", single_user=False))) == ""
    assert stub.check_lease(NS(hub=None)) == ""
