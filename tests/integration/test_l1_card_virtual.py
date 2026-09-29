"""L1-CARD: "Keep on the card" end to end against a virtual board (david, 2026-09-25: option a).

A deploy keeps the design on the board's user microSD ONLY when asked. The board is
pyverify's FakeShell (the Linux profile, which models the D13 store and ``commit``) with
the ``usd`` feature and a card put in the slot, so every commit crosses real sockets on
127.0.0.1. Each behaviour has its negative twin.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from harness_manager.core.errors import UnavailableError
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.deploy import NO_CARD, NO_STORE
from harness_manager_mps3 import deploy as dep
from harness_manager_mps3 import shell as sh
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t2_overlays import make_overlay, point_pushes_at, use_overlay_dirs
from tests.fakes.t13_daemon import pack_overrides
from tests.fakes.virtual_board import FIELDED_ILA_V011, LINUX_HARNESSD, VirtualMps3

#: mps3-harnessd with the D13 store: the Linux harness advertises "usd" (bare metal does not).
LINUX_USD = replace(LINUX_HARNESSD, name="linux-harnessd-usd",
                    features=(*LINUX_HARNESSD.features, "usd"))


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


@pytest.fixture
def overlays(tmp_path, monkeypatch):
    root = tmp_path / "overlays"
    for prof in (LINUX_USD, FIELDED_ILA_V011):
        make_overlay(root / prof.name, "synth", static_id=prof.static_id)
    use_overlay_dirs(monkeypatch, root / LINUX_USD.name, root / FIELDED_ILA_V011.name)
    return root


@pytest.fixture
def persists(monkeypatch):
    """What each deploy asked pyverify for: ``persist``, as ``SwapOrchestrator.deploy`` got it."""
    seen: list[bool] = []
    real = dep.SwapOrchestrator.deploy

    def spy(self, overlay, **kwargs):
        seen.append(kwargs.get("persist", True))
        return real(self, overlay, **kwargs)

    monkeypatch.setattr(dep.SwapOrchestrator, "deploy", spy)
    return seen


def open_and_find(vb: VirtualMps3, monkeypatch):
    point_pushes_at(monkeypatch, vb)
    session = Mps3Pack(console_ports=vb.console_ports).open(vb.candidate())
    ref = next(r for r in session.deploy.overlays()
               if r.name == "synth" and int(r.static_id, 16) == vb.profile.static_id)
    return session, ref


def board(tmp_path, profile=LINUX_USD, *, card: str | None = "da") -> VirtualMps3:
    vb = VirtualMps3(tmp_path, profile)
    if card is not None:
        vb.shell.usd_insert(card)
    return vb


# -- the default never writes the card ------------------------------------------------


def test_the_default_deploy_never_persists_even_on_a_linux_board_with_a_card(
        tmp_path, monkeypatch, overlays, persists):
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        assert session.deploy.card_status().reason == ""        # it COULD keep: a card is in
        result = session.deploy.deploy(ref)
        commits, slots = list(vb.shell.commits), dict(vb.shell.usd_slots)
    assert result.verified and persists == [False]
    assert result.card is None and session.deploy.last_commit_pusher is None
    assert commits == [] and slots == {"A": None, "B": None}


def test_negative_twin_keep_on_card_persists_and_reports_the_slot(
        tmp_path, monkeypatch, overlays, persists):
    phases: list[str] = []
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        result = session.deploy.deploy(ref, lambda ph, done, total: phases.append(ph),
                                       keep_on_card=True)
        commits, slot_a = list(vb.shell.commits), vb.shell.usd_slots["A"]
        status = session.deploy.card_status()
    assert result.verified and persists == [True]
    assert (result.card.kept, result.card.slot, result.card.why) == (True, "A", "")
    assert result.card.text() == "Kept on the card (slot A)"
    assert commits == [("synth", "A")] and slot_a.rm_id == int(ref.rm_id, 16)
    assert status.state == "valid"                               # the card now holds it
    # the card write is its own phase, after the swap is verified
    assert phases.index("card") > phases.index("verify")
    assert "card" not in phases[:phases.index("verify")]
    pusher = session.deploy.last_commit_pusher
    # KEEP-BUDGET: the card's stall row (900 s), as `card commit`; never the swap push's 30 s
    assert (pusher.transport, pusher.windowed, pusher.timeout_s) == ("tcp", False, 900.0)


def test_a_second_keep_goes_to_the_other_slot(tmp_path, monkeypatch, overlays):
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        first = session.deploy.deploy(ref, keep_on_card=True)
        second = session.deploy.deploy(ref, keep_on_card=True)
    assert (first.card.slot, second.card.slot) == ("A", "B")


def test_a_failed_card_write_never_fails_the_deploy(tmp_path, monkeypatch, overlays):
    with board(tmp_path) as vb:
        vb.shell.usd_commit_fail = "io"
        session, ref = open_and_find(vb, monkeypatch)
        result = session.deploy.deploy(ref, keep_on_card=True)
        running = vb.shell.current_rm_id
    assert result.verified and running == int(ref.rm_id, 16)     # the swap stands
    assert result.card.kept is False and "the card write failed (io)" in result.card.why


def test_negative_twin_the_same_board_writes_fine_without_the_fault(
        tmp_path, monkeypatch, overlays):
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        assert session.deploy.deploy(ref, keep_on_card=True).card.kept is True


# -- reading the card ---------------------------------------------------------------------


def test_card_status_reads_a_linux_board_with_a_card(tmp_path, monkeypatch, overlays):
    with board(tmp_path) as vb:
        session, _ = open_and_find(vb, monkeypatch)
        status = session.deploy.card_status()
        commits = list(vb.shell.commits)
    assert (status.store, status.present, status.state, status.reason) == (True, True, "empty", "")
    assert commits == []                                          # reading writes nothing


def test_negative_twin_no_card_in_the_slot(tmp_path, monkeypatch, overlays):
    with board(tmp_path, card=None) as vb:
        session, _ = open_and_find(vb, monkeypatch)
        status = session.deploy.card_status()
    assert (status.store, status.present, status.reason) == (True, False, NO_CARD)


def test_negative_twin_a_bare_metal_harness_has_no_store(tmp_path, monkeypatch, overlays):
    with board(tmp_path, FIELDED_ILA_V011, card=None) as vb:
        session, _ = open_and_find(vb, monkeypatch)
        status = session.deploy.card_status()
    assert (status.store, status.present, status.reason) == (False, False, NO_STORE)


def test_a_foreign_card_cannot_take_a_design(tmp_path, monkeypatch, overlays):
    with board(tmp_path, card="fs") as vb:
        session, _ = open_and_find(vb, monkeypatch)
        status = session.deploy.card_status()
    assert (status.store, status.present, status.state) == (True, True, "foreign")
    assert "holds no harness store" in status.reason


# -- the engine refuses before anything is pushed -----------------------------------------


def engine_for(vb: VirtualMps3, tmp_path) -> Engine:
    return Engine(EngineConfig(state_dir=tmp_path / "state", pack_overrides=pack_overrides(vb)))


@pytest.mark.parametrize("profile, card, reason", [
    (LINUX_USD, None, NO_CARD),
    (FIELDED_ILA_V011, "da", NO_STORE),
])
def test_the_engine_refuses_keep_without_a_card_or_a_store_and_pushes_nothing(
        tmp_path, monkeypatch, overlays, persists, profile, card, reason):
    with board(tmp_path, profile, card=card) as vb:
        point_pushes_at(monkeypatch, vb)
        eng = engine_for(vb, tmp_path)
        try:
            session = eng.open(vb.candidate())
            ref = next(r for r in eng.deploy.overlays(session)
                       if int(r.static_id, 16) == profile.static_id)
            with pytest.raises(UnavailableError) as err:
                eng.deploy.deploy(session, ref, keep_on_card=True)
            pushes = list(vb.shell.accepted_pushes)
        finally:
            eng.close_all()
    assert err.value.capability == "keep_on_card" and err.value.reason == reason
    assert str(err.value) == f"keep_on_card is unavailable: {reason}"
    assert pushes == [] and persists == []                       # nothing left the host


def test_negative_twin_the_engine_keeps_with_a_card(tmp_path, monkeypatch, overlays, persists):
    events = []
    with board(tmp_path) as vb:
        point_pushes_at(monkeypatch, vb)
        eng = engine_for(vb, tmp_path)
        eng.bus.subscribe("deploy.*", events.append)
        try:
            session = eng.open(vb.candidate())
            ref = next(r for r in eng.deploy.overlays(session)
                       if int(r.static_id, 16) == LINUX_USD.static_id)
            result = eng.deploy.deploy(session, ref, keep_on_card=True)
        finally:
            eng.close_all()
    assert result.card.kept and persists == [True]
    started = next(e for e in events if e.topic == "deploy.started")
    done = next(e for e in events if e.topic == "deploy.done")
    assert started.data["keep_on_card"] is True
    assert done.data["card"] == {"kept": True, "slot": "A", "why": ""}
