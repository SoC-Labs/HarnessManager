"""CARD-BUSY (15 Oct, silicon): a card commit started right after another one can come back
``bad args`` although the pair fits; a retry 1-2 min later works. ``card commit`` waits and
retries once, then says the card was busy; never "too big" (an over-size pair is refused
before it is sent). A deploy's Keep on card says the same words.

Every case has its negative twin."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import HeldError
from harness_manager.services.slots import SlotService
from harness_manager_mps3 import card as card_mod
from harness_manager_mps3.deploy import card_outcome
from tests.integration.test_linux_answers_board import with_card  # noqa: F401 - fixture


def busy_then(fake, *, clears: bool):
    """The board refuses commits with ``bad args``; the wait before the retry clears it
    (the card finished its previous commit) when ``clears``."""
    real = fake._op_commit
    state = {"busy": True, "refused": 0}

    def op(request):
        if state["busy"]:
            state["refused"] += 1
            return {"ok": False, "err": "bad args"}
        return real(request)

    fake._op_commit = op
    waits: list[float] = []

    def sleep(s: float) -> None:
        waits.append(s)
        if clears:
            state["busy"] = False

    return state, waits, sleep


def test_a_busy_card_is_retried_once_after_a_wait_and_the_commit_lands(with_card, monkeypatch):
    fake, session = with_card
    state, waits, sleep = busy_then(fake, clears=True)
    monkeypatch.setattr(card_mod.Mps3Card, "_sleep", staticmethod(sleep), raising=False)
    out = SlotService().card_commit(session)
    assert out["rm_name"] == "synth" and fake.commits == [("synth", out["slot"])]
    assert state["refused"] == 1 and len(waits) == 1


def test_twin_still_busy_after_the_retry_says_busy_never_too_big(with_card, monkeypatch):
    fake, session = with_card
    state, waits, sleep = busy_then(fake, clears=False)
    monkeypatch.setattr(card_mod.Mps3Card, "_sleep", staticmethod(sleep), raising=False)
    with pytest.raises(HeldError) as exc:
        SlotService().card_commit(session)
    assert card_mod.CARD_BUSY in exc.value.message
    assert card_mod.SLOT_TOO_SMALL not in exc.value.message
    assert state["refused"] == 2 and len(waits) == 1 and fake.commits == []


def test_twin_another_refusal_is_not_retried(with_card, monkeypatch):
    fake, session = with_card
    fake._op_commit = lambda request: {"ok": False, "err": "io"}
    waits: list[float] = []
    monkeypatch.setattr(card_mod.Mps3Card, "_sleep", staticmethod(waits.append), raising=False)
    with pytest.raises(Exception) as exc:
        SlotService().card_commit(session)
    assert waits == [] and card_mod.CARD_BUSY not in str(exc.value)


def test_keep_on_card_with_bad_args_says_the_card_was_busy():
    from pyverify.swap import PERSIST_FAILED, PersistResult

    out = card_outcome(PersistResult(status=PERSIST_FAILED, err="bad args"))
    assert out.kept is False and card_mod.CARD_BUSY in out.why and "card commit" in out.why


def test_twin_keep_on_card_with_another_error_keeps_the_old_words():
    from pyverify.swap import PERSIST_FAILED, PersistResult

    out = card_outcome(PersistResult(status=PERSIST_FAILED, err="io"))
    assert "the card write failed (io)" in out.why and card_mod.CARD_BUSY not in out.why
