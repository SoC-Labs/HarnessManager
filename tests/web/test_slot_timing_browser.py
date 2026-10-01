"""SLOT-TIMING in the browser: the Board tile's Card line follows a running card job.

A headless system Chrome on the real harness-manager-daemon over DemoEngine. The demo board
gets the ``usd`` store and a card whose OS slots (as the MPS3 card adapter annotates them)
carry a card job; the Card line must say "writing slot B: 12.3 MB / 29 MB, ~N min left"
while it runs, and go back to the slots once it ends. Twin: an idle card shows the slots.
"""

from __future__ import annotations

import dataclasses

import pytest

from harness_manager.core.pack import CardStatus, SlotInfo, SlotJob, SlotStatus
from harness_manager.demo import BOARD_USB

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser

T = 15_000
CARD = '[data-testid="tile-card"]'
WRITING = SlotJob(act="push", slot="B", state="writing", got=12_300_000, length=29_000_000,
                  rate_bps=70_000.0, eta_s=2280.0)


def with_os_slots(engine, job: list[SlotJob]) -> None:
    """The demo card, annotated with OS slots whose job is ``job[0]`` (mutable)."""
    feats = tuple(f for f in engine._board(BOARD_USB).identity.features if f != "usd")
    engine.set_features(BOARD_USB, (*feats, "usd"))
    engine.set_card(BOARD_USB, "valid")
    plain = engine.deploy.card_status

    def card_status(session) -> CardStatus:
        st = SlotStatus(running="A", default="A", target="B", fabric_sid="0x72bb0a36",
                        slots={"A": SlotInfo("A", state="valid", verified="boot"),
                               "B": SlotInfo("B")}, job=job[0])
        return dataclasses.replace(plain(session), os_slots=st)

    engine.deploy.card_status = card_status


def open_board(page) -> None:
    page.locator(f'.board-item[data-board="{BOARD_USB}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector(CARD, timeout=T)             # UI v2: the Design card's Boots next


def test_the_card_line_says_the_running_job_then_the_slots_again(page_factory, engine):
    job = [WRITING]
    with_os_slots(engine, job)
    page = page_factory()
    open_board(page)
    line = page.locator(CARD)
    expect(line).to_have_text("writing slot B: 12.3 MB / 29 MB, ~38 min left", timeout=T)
    assert "wedge the card" in (line.get_attribute("title") or "")
    job[0] = SlotJob(act="push", slot="B", state="ok", got=29_000_000, length=29_000_000)
    expect(line).to_contain_text("nothing kept on the card", timeout=T)   # it re-reads (5 s)
    expect(page.locator('[data-testid="ov-os-slots"] [data-slot="B"]')).to_have_text("B empty")
    assert page.errors == []


def test_twin_an_idle_card_shows_its_slots(page_factory, engine):
    with_os_slots(engine, [SlotJob()])
    page = page_factory()
    open_board(page)
    expect(page.locator(CARD)).to_contain_text("nothing kept on the card", timeout=T)
    expect(page.locator('[data-testid="ov-os-slots"] [data-slot="A"]')).to_contain_text("booted")
    expect(page.locator('[data-testid="ov-os-slots"] [data-slot="B"]')).to_have_text("B empty")
    assert page.errors == []
