"""FIX-PACK-4 in the browser: the UI review's bugs in today's app (not the redesign).

A headless system Chrome over the real harness-manager-daemon on DemoEngine, or (the hub
tests, ``HUB``) the T14 mock with its week-plan sim (``behind_hub(bid, lease=...)``: mine |
elsewhere | other | none). Each behaviour has its negative twin. Nothing here reaches a hub or
a board.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from harness_manager.demo import BOARD_USB

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 900}


# --- helpers ---------------------------------------------------------------------------------------


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def rail(page: Any, bid: str) -> Any:
    return page.locator(f'.board-item[data-board="{bid}"]')


def open_board(page: Any, bid: str = BOARD_USB) -> None:
    rail(page, bid).click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)


def section(page: Any, key: str) -> None:
    page.locator(f'.section-tab[data-section="{key}"]').click()
    page.wait_for_selector(f'[data-testid="section-{key}"]', timeout=T)


def wait_until(fn: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


# --- 5: the tabs never hide off-screen -------------------------------------------------------------

# The tabs a user cannot see: outside the tab row's box, or past the window's edge.
HIDDEN_TABS = """() => {
  const nav = document.querySelector('nav.sections');
  const n = nav.getBoundingClientRect();
  return [...nav.querySelectorAll('.section-tab')].filter((t) => {
    const r = t.getBoundingClientRect();
    return r.width === 0 || r.left < n.left - 1 || r.right > n.right + 1 || r.right > innerWidth;
  }).map((t) => t.dataset.section);
}"""


def test_every_tab_is_on_screen_at_1024_px(page_factory):
    page = page_factory(width=1024, height=768)
    open_board(page)
    expect(page.locator(".section-tab")).to_have_count(12)
    assert page.evaluate(HIDDEN_TABS) == []
    for key in ("update", "checks", "activity"):           # the three the review lost
        section(page, key)
        expect(page.locator(f'.section-tab[data-section="{key}"]')).to_have_attribute("aria-selected", "true")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.errors == []


def test_negative_twin_the_old_scrolling_row_hides_tabs_and_the_check_sees_it(page_factory):
    page = page_factory(width=1024, height=768)
    open_board(page)
    page.add_style_tag(content="nav.sections { flex-wrap: nowrap !important; overflow-x: auto; }")
    hidden = page.evaluate(HIDDEN_TABS)
    assert "activity" in hidden and "overview" not in hidden, hidden


# --- 8: the header's refresh re-reads the Card line and the SD journal ----------------------------


def give_card(engine: Any, card: str | None = "empty") -> None:
    feats = tuple(f for f in engine._board(BOARD_USB).identity.features if f != "usd")
    engine.set_features(BOARD_USB, (*feats, "usd"))
    engine.set_card(BOARD_USB, card)


def test_the_header_refresh_rereads_the_card_line_and_the_sd_journal(page_factory, engine, tmp_path):
    give_card(engine, "empty")
    page = page_factory(**APP)
    open_board(page)
    tile = by_id(page, "tile-card")
    expect(tile).to_have_text("empty", timeout=T)
    expect(by_id(page, "sd-banner")).to_have_count(0)
    # the world moves: the card is taken out, and an SD install is found interrupted
    engine.set_card(BOARD_USB, None)
    engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                      "backup": {"path": str(tmp_path / "b.zip")}})
    cards, journals = len(engine.called("deploy.card_status")), len(engine.called("storage.pending"))
    page.locator('[data-action="refresh-board"]').click()
    expect(tile).to_have_text("none (boots as always)", timeout=T)
    expect(by_id(page, "sd-banner")).to_be_visible(timeout=T)
    assert len(engine.called("deploy.card_status")) > cards
    assert len(engine.called("storage.pending")) > journals
    assert page.errors == []


def test_negative_twin_without_a_card_store_the_refresh_reads_no_card(page_factory, engine):
    page = page_factory(**APP)
    open_board(page)
    expect(by_id(page, "tile-card")).to_have_text("no card store on this harness", timeout=T)
    infos, journals = len(engine.called("info")), len(engine.called("storage.pending"))
    cards = len(engine.called("deploy.card_status"))
    page.locator('[data-action="refresh-board"]').click()
    assert wait_until(lambda: len(engine.called("info")) > infos
                      and len(engine.called("storage.pending")) > journals)
    time.sleep(0.5)
    assert len(engine.called("deploy.card_status")) == cards         # nothing to read
    expect(by_id(page, "tile-card")).to_have_text("no card store on this harness")


# --- 9: Settings opens on General, then where you left it --------------------------------------------


def gear(page: Any) -> Any:
    page.locator('[data-action="settings"]').click()
    expect(by_id(page, "settings")).to_be_visible(timeout=T)
    return by_id(page, "settings-pane")


def test_settings_opens_on_general_then_on_the_last_section_used(page_factory):
    page = page_factory(**APP)
    page.wait_for_selector(".board-item", timeout=T)
    expect(gear(page)).to_have_attribute("data-settings-section", "general", timeout=T)
    page.locator('[data-testid="settings-nav"] [data-settings-section="tools"]').click()
    page.locator('[data-action="settings-close"]').click()
    expect(by_id(page, "settings")).to_have_count(0)
    expect(gear(page)).to_have_attribute("data-settings-section", "tools", timeout=T)
    assert page.errors == []


def test_negative_twin_the_update_tabs_settings_link_still_opens_updates(page_factory):
    page = page_factory(**APP)
    open_board(page)
    section(page, "update")
    by_id(page, "update-app").locator('[data-action="open-settings"]').click()
    expect(by_id(page, "settings-pane")).to_have_attribute("data-settings-section", "updates", timeout=T)
    expect(by_id(page, "update-settings")).to_be_visible(timeout=T)
