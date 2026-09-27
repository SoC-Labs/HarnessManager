"""QUIET-POLL: the background gate (``services/quiet.py``) and the presence beat behind it,
board-free, on a fake clock. Every check has a negative twin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness_manager.core.errors import ActionFailedError, HeldError, UnreachableError
from harness_manager.core.events import EventBus
from harness_manager.core.panel import Hello, PanelState, PanelSupport
from harness_manager.services import quiet as Q
from harness_manager.services.presence import RETRY_S, PresenceService
from harness_manager.services.quiet import BackgroundGate, lease_elsewhere, policy_for
from harness_manager.settings import runtime


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def gate(*, policy: str = "on-view", holder: str = "", **kw) -> tuple[BackgroundGate, Clock]:
    clock = Clock()
    g = BackgroundGate(policy_of=lambda _b: policy, lease_of=lambda _b: holder, clock=clock,
                       **kw)
    return g, clock


# --- the verdict, cheapest first ------------------------------------------------------------------


def test_nobody_viewing_means_no_background_contact_and_a_viewer_means_go():
    g, _ = gate()
    assert g.check("b1").kind == Q.KIND_NO_VIEWER
    g.view("b1", "page")
    assert g.check("b1") is None


def test_policy_off_wins_over_a_viewer_and_twin_on_view_does_not():
    g, _ = gate(policy="off")
    g.view("b1", "page")
    assert g.check("b1").kind == Q.KIND_OFF
    g.policy_of = lambda _b: "on-view"
    assert g.check("b1") is None


def test_the_lease_elsewhere_stops_a_viewed_board_and_names_the_holder():
    g, _ = gate(holder="alice@lab-pc")
    g.view("b1", "page")
    q = g.check("b1")
    assert q.kind == Q.KIND_LEASE and q.holder == "alice@lab-pc" and "alice@lab-pc" in q.text
    g.lease_of = lambda _b: ""                   # twin: our own lease (or none)
    assert g.check("b1") is None


def test_no_hub_call_for_a_board_nobody_views():
    asked: list[str] = []
    g, _ = gate()
    g.lease_of = lambda b: asked.append(b) or "alice"
    assert g.check("b1").kind == Q.KIND_NO_VIEWER and asked == []
    g.view("b1", "page")
    assert g.check("b1").kind == Q.KIND_LEASE and asked == ["b1"]


def test_state_always_names_the_holder_even_with_nobody_viewing():
    g, _ = gate(holder="alice@lab-pc")
    st = g.state("b1")
    assert st["allowed"] is False and st["kind"] == Q.KIND_LEASE and st["holder"] == "alice@lab-pc"
    g.lease_of = lambda _b: ""
    st = g.state("b1")
    assert st["kind"] == Q.KIND_NO_VIEWER and st["holder"] == ""


# --- viewers ---------------------------------------------------------------------------------------


def test_a_viewer_lapses_after_its_ttl_and_twin_a_refresh_keeps_it():
    g, clock = gate()
    g.view("b1", "page", ttl_s=20)
    clock.t += 19
    g.view("b1", "page", ttl_s=20)               # refreshed
    clock.t += 19
    assert g.viewers("b1") == 1
    clock.t += 2
    assert g.viewers("b1") == 0 and g.check("b1").kind == Q.KIND_NO_VIEWER


def test_unview_and_forget():
    g, _ = gate()
    g.view("b1", "a")
    g.view("b1", "b")
    assert g.unview("b1", "a") and not g.unview("b1", "a") and g.viewers("b1") == 1
    g.forget("b1")
    assert g.viewers("b1") == 0 and g.check("b1").kind == Q.KIND_NO_VIEWER


def test_a_viewer_ttl_is_clamped():
    g, clock = gate()
    g.view("b1", "page", ttl_s=10**9)
    clock.t += Q.VIEWER_TTL_MAX_S + 1
    assert g.viewers("b1") == 0


# --- the back-off ----------------------------------------------------------------------------------


def test_a_refusal_backs_off_30_s_then_doubles_to_the_10_min_cap():
    g, clock = gate()
    g.view("b1", "page", ttl_s=300)
    steps = []
    for _ in range(8):
        steps.append(g.note_busy("b1", "reset"))
        q = g.check("b1")
        assert q.kind == Q.KIND_BUSY and "busy (another client)" in q.text
        clock.t += steps[-1]
        g.view("b1", "page", ttl_s=300)
        assert g.check("b1") is None
    assert steps == [30, 60, 120, 240, 480, 600, 600, 600]


def test_refusals_inside_a_back_off_do_not_lengthen_it():
    g, clock = gate()
    g.view("b1", "page")
    assert g.note_busy("b1") == 30
    clock.t += 10
    assert g.note_busy("b1") == pytest.approx(20)        # a click met the same busy board
    assert g.backing_off("b1")[1] == 30


def test_an_answered_call_ends_the_back_off():
    g, _ = gate()
    g.view("b1", "page")
    g.note_busy("b1")
    g.note_busy("b1")
    g.note_ok("b1")
    assert g.check("b1") is None
    assert g.note_busy("b1") == 30, "the next refusal starts from the first step again"


@pytest.mark.parametrize("exc", [HeldError("held"), UnreachableError("refused"),
                                 ConnectionResetError("reset"), TimeoutError("timed out")])
def test_observe_backs_off_on_contention(exc):
    g, _ = gate()
    g.view("b1", "page")
    g.observe("b1", exc)
    assert g.check("b1").kind == Q.KIND_BUSY


@pytest.mark.parametrize("exc", [ActionFailedError("the board said no"), None])
def test_twin_observe_ignores_other_failures_and_success(exc):
    g, _ = gate()
    g.view("b1", "page")
    g.observe("b1", exc)
    assert g.check("b1") is None


def test_the_demo_gate_says_yes_to_everything():
    g, _ = gate(policy="off", holder="alice", enabled=False)
    g.note_busy("b1")
    assert g.check("b1") is None and g.state("b1")["allowed"] is True


# --- the lease service's word, and the policy's -----------------------------------------------------


def test_lease_elsewhere_uses_the_lease_services_mine():
    assert lease_elsewhere({"lease": {"holder": "alice@x", "mine": False}}) == "alice@x"
    assert lease_elsewhere({"lease": {"holder": "david@mapstone-dev", "mine": True}}) == ""
    assert lease_elsewhere({"lease": None}) == "" and lease_elsewhere(None) == ""


def write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    runtime.refresh()


def test_policy_for_reads_the_board_then_the_defaults_then_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path))
    boards = tmp_path / "boards.toml"
    assert policy_for("mps3@10.0.0.1:6900") == "on-view"
    write(tmp_path / "settings.toml", '[general]\nbackground_poll = "off"\n')
    assert policy_for("mps3@10.0.0.1:6900") == "off"
    write(boards, '[boards."mps3@10.0.0.1:6900"]\npoll = "on-view"\n')
    assert policy_for("mps3@10.0.0.1:6900") == "on-view"
    write(boards, '[boards.defaults]\npoll = "on-view"\n[boards.lab]\nmatch = ["x"]\n')
    write(tmp_path / "settings.toml", "")
    write(boards, '[boards.defaults]\npoll = "off"\n[boards.lab]\nmatch = ["mps3@10.0.0.1:6900"]\n')
    assert policy_for("mps3@10.0.0.1:6900") == "off", "[boards.defaults] reaches the board"
    assert policy_for("mps3@10.0.0.9:6900") == "on-view", "a board without a table: the setting"


def test_twin_a_bad_poll_value_is_skipped_not_obeyed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path))
    write(tmp_path / "boards.toml", '[boards."mps3@10.0.0.1:6900"]\npoll = "sometimes"\n')
    assert policy_for("mps3@10.0.0.1:6900") == "on-view"
    write(tmp_path / "boards.toml", "not toml [")
    assert policy_for("mps3@10.0.0.1:6900") == "on-view", "a broken file never stops a board"


def test_the_setting_rows_exist_with_the_policy_default():
    from harness_manager.settings.resolve import core_schema

    schema = core_schema()
    row = schema.spec("general.background_poll")
    assert row.default == "on-view" and tuple(row.choices) == ("on-view", "off")
    board = schema.spec("boards.lab.poll")
    assert board.scope == "board" and tuple(board.choices) == ("", "on-view", "off")


# --- the presence beat behind the gate ------------------------------------------------------------


class FakePanel:
    def __init__(self) -> None:
        self.sent: list[Hello] = []
        self.offered: list[Hello] = []
        self.armed = None
        self.fail: Exception | None = None

    def support(self) -> PanelSupport:
        return PanelSupport()

    def hello(self, hello: Hello) -> PanelState:
        if self.fail is not None:
            raise self.fail
        self.sent.append(hello)
        return PanelState(page="status", owner="harness", count=1)

    def offer(self, hello, on_reply) -> None:
        self.offered.append(hello)
        self.armed = (hello, on_reply)

    def withdraw(self) -> bool:
        armed, self.armed = self.armed, None
        return armed is not None


def presence(g: BackgroundGate, clock: Clock, *, ride_wait_s: float = 0.0, gate=None):
    panel = FakePanel()
    session = SimpleNamespace(candidate=SimpleNamespace(name="mps3-01"), panel=panel, hub=None)
    svc = PresenceService(EventBus(), clock=clock, wall=lambda: 1.79e9, ride_wait_s=ride_wait_s,
                          who="me@srv", app="hm/0", allow=g.check, noted=g.observe,
                          **({"gate": gate} if gate else {}))
    svc.track("b1", session)
    return svc, panel


def test_no_viewer_no_hello_and_nothing_offered_to_ride():
    g, clock = gate()
    svc, panel = presence(g, clock, ride_wait_s=5.0)
    for _ in range(5):
        svc.beat_due()
        clock.t += 30
    assert panel.sent == [] and panel.offered == [] and svc.presence("b1")["quieted"] == 5
    assert "nobody is viewing" in svc.presence("b1")["quiet"]
    g.view("b1", "page", ttl_s=300)                       # twin
    svc.beat_due()
    assert len(panel.offered) == 1


def test_an_offered_hello_is_withdrawn_when_the_gate_closes():
    g, clock = gate()
    g.view("b1", "page", ttl_s=300)
    svc, panel = presence(g, clock, ride_wait_s=5.0)
    svc.beat_due()
    assert panel.armed is not None
    g.lease_of = lambda _b: "alice@lab"
    clock.t += 6
    svc.beat_due()
    assert panel.armed is None and panel.sent == []


def test_a_beat_the_board_turns_away_backs_off_and_waits_a_whole_beat():
    g, clock = gate()
    g.view("b1", "page", ttl_s=3600)
    svc, panel = presence(g, clock)
    panel.fail = HeldError("shell reset the connection")
    svc.beat_due()
    assert g.check("b1").kind == Q.KIND_BUSY
    panel.fail = None
    for _ in range(int(29 // 1)):                         # every tick of the next 29 s
        clock.t += 1
        svc.beat_due()
    assert panel.sent == [], "never retried within a beat, nor within the back-off"
    clock.t += 2
    svc.beat_due()
    assert len(panel.sent) == 1


def test_twin_a_beat_our_own_job_holds_back_is_retried_sooner():
    import contextlib

    g, clock = gate()
    g.view("b1", "page", ttl_s=3600)
    held = {"on": True}

    @contextlib.contextmanager
    def job_gate(_bid):
        if held["on"]:
            raise HeldError("harness-manager-daemon deploy job j1 holds the board")
        yield

    svc, panel = presence(g, clock, gate=job_gate)
    svc.beat_due()
    assert svc.presence("b1")["skipped"] == 1 and g.check("b1") is None, "our job: no back-off"
    held["on"] = False
    clock.t += RETRY_S
    svc.beat_due()
    assert len(panel.sent) == 1
