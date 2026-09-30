"""FIX-PACK-4 in the browser: the UI review's bugs in today's app (not the redesign).

A headless system Chrome over the real harness-manager-daemon on DemoEngine, or (the hub
tests, ``HUB``) the T14 mock with its week-plan sim (``behind_hub(bid, lease=...)``: mine |
elsewhere | other | none). Each behaviour has its negative twin. Nothing here reaches a hub or
a board.
"""

from __future__ import annotations

import getpass
import time
from typing import Any

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB

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
    expect(by_id(page, "sd-banner")).to_be_visible(timeout=T)
    section(page, "overview")                    # a journal found first opens the SD tab
    expect(tile).to_have_text("none (boots as always)", timeout=T)
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


# --- 1: one lease rule: held HERE, never `mine` -----------------------------------------------------

ME = f"{getpass.getuser()}@harness-manager"        # the mock's principal (this and other sessions)
HUB = pytest.mark.week_plan("hub_api", sim=True)


def sim(daemon: Any) -> Any:
    return daemon.app.state.sim


def xvc_page(page_factory: Any, daemon: Any, engine: Any, lease: str) -> Any:
    from tests.web.test_xvc_card_browser import xvc_board

    xvc_board(engine, BOARD_FIELDED)
    sim(daemon).behind_hub(BOARD_FIELDED, lease=lease)
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    section(page, "debug")
    page.wait_for_selector('[data-testid="xvc-card"]', timeout=T)
    return page


@HUB
def test_xvc_is_off_for_a_lease_your_other_session_holds(page_factory, daemon, engine):
    page = xvc_page(page_factory, daemon, engine, "elsewhere")
    why = f"XVC is for the lease holder only: {ME} holds this board in another session, not this Harness Manager"
    expect(by_id(page, "reason-xvc_open")).to_have_text(why, timeout=T)
    button = page.locator('[data-testid="xvc-card"] [data-action="xvc_open"]')
    expect(button).to_have_attribute("aria-disabled", "true")
    button.click(force=True)                          # an interlock: nothing is sent
    expect(by_id(page, "xvc-result")).to_contain_text("Nothing was run.")
    assert daemon.app.state.xvc.sessions == {}


@HUB
def test_negative_twin_xvc_opens_for_the_lease_held_here(page_factory, daemon, engine):
    page = xvc_page(page_factory, daemon, engine, "mine")
    button = page.locator('[data-testid="xvc-card"] [data-action="xvc_open"]')
    expect(button).not_to_have_attribute("aria-disabled", "true", timeout=T)
    expect(by_id(page, "reason-xvc_open")).to_have_count(0)


def harness_lease_page(page_factory: Any, daemon: Any, lease: str) -> tuple[Any, Any]:
    from tests.web.test_updui_browser import harness_page

    sim(daemon).behind_hub(BOARD_USB, lease=lease)
    return harness_page(page_factory)


@pytest.mark.week_plan("harness_api", sim=True)
def test_the_update_lease_line_is_not_yours_for_your_other_session(page_factory, daemon):
    from tests.web.test_updui_browser import install_rekey

    page, card = harness_lease_page(page_factory, daemon, "elsewhere")
    line = by_id(page, "harness-lease").first
    expect(line).to_contain_text(f"{ME} holds the lease on mps3_01_pl in another session, "
                                 "not this Harness Manager", timeout=T)
    expect(line).not_to_contain_text("You hold")
    panel = install_rekey(page, card)
    result = panel.locator('[data-testid="harness-result"]')
    expect(result).to_contain_text("HELD", timeout=T)                    # the daemon refuses too
    assert daemon.app.state.harness.running == {}


@pytest.mark.week_plan("harness_api", sim=True)
def test_negative_twin_the_update_lease_line_is_yours_when_held_here(page_factory, daemon):
    page, _card = harness_lease_page(page_factory, daemon, "mine")
    expect(by_id(page, "harness-lease").first).to_contain_text("You hold this board's hub lease",
                                                               timeout=T)


# --- 10: who may drive the board: the same rule on every tab ---------------------------------------


def hub_board_page(page_factory: Any, daemon: Any, lease: str) -> Any:
    # BOARD_USB: the demo board that can do everything (Debug USB: reboot, SD, resets)
    sim(daemon).behind_hub(BOARD_USB, lease=lease)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    return page


def classes(page: Any, action: str, within: str = "") -> str:
    return page.locator(f'{within} [data-action="{action}"]'.strip()).first.get_attribute("class") or ""


@HUB
def test_on_a_board_someone_else_leases_program_and_friends_are_off_and_not_primary(page_factory, daemon):
    page = hub_board_page(page_factory, daemon, "other")
    held = "is for the lease holder only: alice@lab-pc-07 holds this board"
    tile = by_id(page, "tile-board")
    expect(tile.locator('[data-testid="reason-reset_dut"]')).to_have_text(f"Reset DUT {held}", timeout=T)
    expect(tile.locator('[data-testid="reason-reboot"]')).to_have_text(f"Reboot {held}")
    assert "danger" not in classes(page, "reboot", '[data-testid="tile-board"]')
    expect(by_id(page, "tile-debug").locator('[data-testid="reason-up"]')).to_have_text(f"Debug {held}")
    assert "primary" not in classes(page, "up", '[data-testid="tile-debug"]')
    section(page, "program")
    expect(by_id(page, "reason-program")).to_have_text(f"Program {held}", timeout=T)
    expect(page.locator('[data-action="program"]')).to_have_attribute("aria-disabled", "true")
    assert "primary" not in classes(page, "program")
    expect(by_id(page, "reason-restore")).to_have_text(f"Restore baseline {held}")
    section(page, "debug")
    expect(by_id(page, "debug-card").locator('[data-testid="reason-up"]')).to_have_text(f"Debug {held}")
    expect(by_id(page, "debug-card").locator('[data-testid="reason-detect"]')).to_have_text(f"Debug {held}")
    assert "primary" not in classes(page, "xvc_open")
    section(page, "power")
    expect(by_id(page, "reboot-card").locator('[data-testid="reason-reboot"]')).to_have_text(
        f"Reboot {held}")
    assert "danger" not in classes(page, "reboot", '[data-testid="reboot-card"]')
    expect(by_id(page, "reset-dut").locator('[data-testid="reason-reset_dut"]')).to_have_text(
        f"Reset DUT {held}")
    assert page.errors == []


@HUB
def test_negative_twin_the_lease_holder_gets_the_primary_buttons(page_factory, daemon):
    page = hub_board_page(page_factory, daemon, "mine")
    tile = by_id(page, "tile-board")
    expect(tile.locator('[data-action="reboot"]')).to_be_visible(timeout=T)
    expect(tile.locator('[data-testid="reason-reboot"]')).not_to_contain_text("lease holder")
    assert "danger" in classes(page, "reboot", '[data-testid="tile-board"]')
    assert "primary" in classes(page, "up", '[data-testid="tile-debug"]')
    section(page, "program")
    expect(page.locator('[data-action="program"]')).to_be_visible(timeout=T)
    assert "primary" in classes(page, "program")
    expect(by_id(page, "reason-program")).not_to_contain_text("lease holder")


@HUB
def test_your_other_sessions_lease_does_not_let_you_program_here(page_factory, daemon):
    page = hub_board_page(page_factory, daemon, "elsewhere")
    section(page, "program")
    expect(by_id(page, "reason-program")).to_have_text(
        f"Program is for the lease holder only: {ME} holds this board in another session, "
        "not this Harness Manager", timeout=T)
    assert "primary" not in classes(page, "program")


def test_negative_twin_a_board_with_no_hub_has_no_lease_rule(page_factory):
    page = page_factory(**APP)
    open_board(page)
    section(page, "program")
    expect(page.locator('[data-action="program"]')).to_be_visible(timeout=T)
    expect(by_id(page, "reason-program")).not_to_contain_text("lease", timeout=T)
    assert "primary" in classes(page, "program")
