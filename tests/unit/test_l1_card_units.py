"""L1-CARD units: the deploy service's "Keep on the card", and the MPS3 pack's readings of
pyverify's ``usd`` reply and persist result. No sockets; each check has a negative twin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyverify.client import UsdResponse
from pyverify.swap import PersistResult

from harness_manager.core.errors import ExitCode, IncompatibleError, UnavailableError
from harness_manager.core.events import EventBus
from harness_manager.core.model import Check
from harness_manager.core.pack import (
    CARD_NO_CARD,
    CARD_NO_STORE,
    CardOutcome,
    CardStatus,
    PreflightItem,
    card_status_of,
    keep_refusal,
)
from harness_manager.services.deploy import ITEM_SHELL_ID, DeployService
from harness_manager_mps3.deploy import card_outcome, card_status_from
from tests.unit.test_t2_deploy_service import NANOSOC, FakeSession, ScriptedAdapter

READY = CardStatus(store=True, present=True, state="empty", text="empty")
NO_CARD = CardStatus(store=True, present=False, state="none", text="none", reason=CARD_NO_CARD)


class CardAdapter(ScriptedAdapter):
    """A scripted adapter with the card support: records every deploy's keywords."""

    def __init__(self, card: CardStatus = READY, **kw) -> None:
        super().__init__(**kw)
        self.card = card
        self.kwargs: list[dict] = []
        self.card_reads = 0

    def card_status(self) -> CardStatus:
        self.card_reads += 1
        return self.card

    def deploy(self, overlay, progress=None, **kw):
        self.kwargs.append(kw)
        result = super().deploy(overlay, progress)
        if kw.get("keep_on_card"):
            from dataclasses import replace
            result = replace(result, card=CardOutcome(kept=True, slot="B"))
        return result


def service():
    bus = EventBus()
    seen = []
    bus.subscribe("deploy.*", seen.append)
    return DeployService(SimpleNamespace(bus=bus)), seen


# -- the default never asks the board to keep -----------------------------------------


def test_the_default_deploy_never_passes_keep_even_when_the_card_could_take_it():
    adapter = CardAdapter()
    svc, seen = service()
    result = svc.deploy(FakeSession(adapter), NANOSOC)
    assert adapter.kwargs == [{}] and result.card is None
    assert adapter.card_reads == 0                     # not even read
    assert next(e for e in seen if e.topic == "deploy.done").data["card"] is None


def test_negative_twin_keep_on_card_passes_it_and_reports_the_slot():
    adapter = CardAdapter()
    svc, seen = service()
    result = svc.deploy(FakeSession(adapter), NANOSOC, keep_on_card=True)
    assert adapter.kwargs == [{"keep_on_card": True}]
    assert result.card == CardOutcome(kept=True, slot="B")
    started = next(e for e in seen if e.topic == "deploy.started")
    done = next(e for e in seen if e.topic == "deploy.done")
    assert started.data["keep_on_card"] is True
    assert done.data["card"] == {"kept": True, "slot": "B", "why": ""}


# -- refusals: before anything is pushed ----------------------------------------------


def test_keep_on_a_board_without_card_support_is_refused_with_no_store():
    adapter = ScriptedAdapter()                       # no card_status: no store
    svc, seen = service()
    with pytest.raises(UnavailableError) as err:
        svc.deploy(FakeSession(adapter), NANOSOC, keep_on_card=True)
    assert err.value.code == ExitCode.UNAVAILABLE
    assert (err.value.capability, err.value.reason) == ("keep_on_card", CARD_NO_STORE)
    assert adapter.deployed == []
    assert [e.topic for e in seen] == ["deploy.failed"]
    assert seen[0].data["stage"] == "preflight" and CARD_NO_STORE in seen[0].data["reason"]


def test_negative_twin_the_same_board_deploys_without_keep():
    adapter = ScriptedAdapter()
    svc, _ = service()
    assert svc.deploy(FakeSession(adapter), NANOSOC).card is None
    assert adapter.deployed == ["nanosoc"]


def test_keep_with_no_card_is_refused_with_its_reason():
    adapter = CardAdapter(card=NO_CARD)
    svc, seen = service()
    with pytest.raises(UnavailableError, match="no card in the USER microSD slot"):
        svc.deploy(FakeSession(adapter), NANOSOC, keep_on_card=True)
    assert adapter.deployed == [] and [e.topic for e in seen] == ["deploy.failed"]


def test_negative_twin_no_card_still_deploys_without_keep():
    adapter = CardAdapter(card=NO_CARD)
    svc, _ = service()
    assert svc.deploy(FakeSession(adapter), NANOSOC).verified
    assert adapter.kwargs == [{}]


def test_a_preflight_mismatch_wins_over_the_card_and_the_card_is_not_read():
    adapter = CardAdapter(card=NO_CARD, items={
        "nanosoc": [PreflightItem(ITEM_SHELL_ID, Check.MISMATCH, "other shell")]})
    svc, _ = service()
    with pytest.raises(IncompatibleError):
        svc.deploy(FakeSession(adapter), NANOSOC, keep_on_card=True)
    assert adapter.card_reads == 0 and adapter.deployed == []


def test_an_adapter_that_ignores_keep_still_gets_a_card_line():
    class Deaf(CardAdapter):
        def deploy(self, overlay, progress=None, **kw):
            self.kwargs.append(kw)
            return ScriptedAdapter.deploy(self, overlay, progress)   # card=None

    svc, _ = service()
    result = svc.deploy(FakeSession(Deaf()), NANOSOC, keep_on_card=True)
    assert result.card == CardOutcome(kept=False,
                                      why="the board reported nothing about the card")


def test_card_status_of_and_keep_refusal():
    assert card_status_of(object()) == CardStatus(store=False, reason=CARD_NO_STORE)
    assert card_status_of(SimpleNamespace(card_status=lambda: READY)) is READY
    assert keep_refusal(READY) is None
    err = keep_refusal(NO_CARD)
    assert isinstance(err, UnavailableError) and err.reason == CARD_NO_CARD


def test_card_outcome_text():
    assert CardOutcome(kept=True, slot="B").text() == "Kept on the card (slot B)"
    assert CardOutcome(kept=False, why="no card").text() == "Not kept: no card"


# -- the MPS3 pack: pyverify's usd reply -> CardStatus ------------------------------------


@pytest.mark.parametrize("reply, store, present, reason", [
    (UsdResponse(ok=True, present=True, state="empty", text="empty"), True, True, ""),
    (UsdResponse(ok=True, present=True, state="valid", text="led [A]"), True, True, ""),
    (UsdResponse(ok=True, present=True, state="stale", text="stale key"), True, True, ""),
    (UsdResponse(ok=True, present=False, state="none", text="none"), True, False, CARD_NO_CARD),
    (UsdResponse(ok=True, present=False, state="no_hw", text=""), False, False, CARD_NO_STORE),
    (UsdResponse(ok=False, err="unavailable"), False, False, f"{CARD_NO_STORE} (usd: unavailable)"),
])
def test_card_status_from_a_usd_reply(reply, store, present, reason):
    got = card_status_from(reply)
    assert (got.store, got.present, got.reason) == (store, present, reason)


@pytest.mark.parametrize("state, words", [
    ("foreign", "holds no harness store"),
    ("init", "still starting"),
    ("unsupported", "not a kind the harness supports"),
    ("error", "reports an error (ERR 13)"),
])
def test_negative_twin_a_present_card_that_cannot_take_a_design(state, words):
    got = card_status_from(UsdResponse(ok=True, present=True, state=state, text="ERR 13"))
    assert got.store and got.present and words in got.reason


@pytest.mark.parametrize("persist, want", [
    (PersistResult(status="committed", slot="B"), CardOutcome(kept=True, slot="B")),
    (PersistResult(status="skipped", reason="no card in the user microSD slot"),
     CardOutcome(kept=False, why="no card in the user microSD slot")),
    (PersistResult(status="failed", err="io", warning=True),
     CardOutcome(kept=False, why="the card write failed (io); the card keeps the design it had")),
    (None, CardOutcome(kept=False, why="pyverify reported nothing about the card")),
])
def test_card_outcome_from_pyverify(persist, want):
    assert card_outcome(persist) == want
