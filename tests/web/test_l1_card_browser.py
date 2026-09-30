"""L1-CARD in the browser: the Program section's "Keep on the card" box and the report line.

A headless system Chrome on the real harness-manager-daemon (and, marked ``mock_too``, the
T14 mock) over DemoEngine, whose ``set_features``/``set_card`` knobs give a board the D13
store and a card. The box shows only when the harness reports ``usd`` AND a card is in the
slot; it starts unticked; ticked, Program keeps the design and the report says where.
Each test has its negative twin.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser

T = 10_000
BOX = '[data-testid="keep-on-card"]'


def give_card(engine, *, usd: bool = True, card: str | None = "empty") -> None:
    feats = tuple(f for f in engine._board(BOARD_USB).identity.features if f != "usd")
    engine.set_features(BOARD_USB, (*feats, "usd") if usd else feats)
    engine.set_card(BOARD_USB, card)


def program_section(page) -> None:
    page.locator(f'.board-item[data-board="{BOARD_USB}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
    nav.section(page, "program")                      # UI v2: the Workbench's Program part
    page.wait_for_selector('[data-overlay="led"]', timeout=T)


def program_led(page) -> None:
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    page.locator('[data-testid="arm-program"] input').check()
    expect(page.locator('[data-action="program"]')).not_to_have_attribute("aria-disabled", "true")
    page.locator('[data-action="program"]').click()
    outcome = page.locator('[data-testid="deploy-outcome"]')
    outcome.wait_for(timeout=T)
    assert outcome.get_attribute("data-state") == "done"
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"program-result\"]')?.innerText.includes('rc 0')",
        timeout=T)


@pytest.mark.mock_too
def test_the_box_shows_unticked_and_ticked_it_keeps_the_design(page_factory, engine,
                                                              screenshots):
    give_card(engine)
    page = page_factory()
    program_section(page)
    box = page.locator(BOX)
    box.wait_for(timeout=T)
    expect(box).not_to_be_checked()
    expect(box).to_be_enabled()
    assert "The board boots into this design next time." in \
        page.locator('[data-testid="keep-card"]').inner_text()
    box.check()
    page.screenshot(path=str(screenshots / "light-program-keep-on-card.png"))
    program_led(page)
    card = page.locator('[data-testid="deploy-card"]')
    card.wait_for(timeout=T)
    assert card.get_attribute("data-kept") == "true"
    assert "Kept on the card (slot A)" in card.inner_text()
    result = page.locator('[data-testid="program-result"]').inner_text()
    assert "Kept on the card (slot A)" in result and "--keep-on-card" in result
    assert engine.called("deploy.deploy")[-1][-1] is True
    expect(page.locator(BOX)).not_to_be_checked()          # each keep is a fresh choice
    page.screenshot(path=str(screenshots / "light-program-kept.png"))
    assert page.errors == []


def test_negative_twin_the_box_left_unticked_keeps_nothing(page_factory, engine):
    give_card(engine)
    page = page_factory()
    program_section(page)
    page.locator(BOX).wait_for(timeout=T)
    program_led(page)
    assert engine.called("deploy.deploy")[-1][-1] is False
    assert page.locator('[data-testid="deploy-card"]').count() == 0
    result = page.locator('[data-testid="program-result"]').inner_text()
    assert "Kept on the card" not in result and "--keep-on-card" not in result


@pytest.mark.mock_too
def test_negative_twin_no_usd_hides_the_box_and_never_reads_the_card(page_factory, engine):
    give_card(engine, usd=False)                            # a card, but no store
    page = page_factory()
    program_section(page)
    page.wait_for_selector('[data-testid="program-card"]', timeout=T)
    assert page.locator('[data-testid="keep-card"]').count() == 0
    assert engine.called("deploy.card_status") == []
    program_led(page)
    assert engine.called("deploy.deploy")[-1][-1] is False


def test_negative_twin_no_card_hides_the_box(page_factory, engine):
    give_card(engine, card=None)                            # the store, but no card
    page = page_factory()
    program_section(page)
    # the card was read (the harness has the store) and said: no card
    for _ in range(200):
        if engine.called("deploy.card_status"):
            break
        page.wait_for_timeout(50)
    assert engine.called("deploy.card_status")
    assert page.locator('[data-testid="keep-card"]').count() == 0


def test_a_card_that_cannot_take_a_design_shows_the_box_disabled_with_the_reason(page_factory,
                                                                                 engine):
    give_card(engine, card="foreign")
    page = page_factory()
    program_section(page)
    box = page.locator(BOX)
    box.wait_for(timeout=T)
    expect(box).to_be_disabled()
    expect(page.locator('[data-testid="keep-reason"]')).to_contain_text("state 'foreign'")
