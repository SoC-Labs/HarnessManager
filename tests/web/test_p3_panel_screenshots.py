"""Lane P3's review screenshots: the front panel in the Overview's Board tile and in Details,
at the app window's 1440x900, over the T14 mock (its PanelSim and its lease requests).

They land in tests/web/screenshots/review/ (gitignored); the curated copies are committed
under docs/review/2026-09-25/. Each test asserts the state it photographs, so a picture never
shows a broken page.
"""

from __future__ import annotations

import re

import pytest

from tests.web.test_p3_panel_ui import BOARD, details, linux, open_board

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("hub_api", sim=True)]
T = 10_000
APP = {"width": 1440, "height": 900}


@pytest.fixture(autouse=True)
def text_mirror_only(daemon):
    """These tests are about the text mirror (P3). Since LM4 the Live display replaces it while
    it is live, and the T14 mock gives every demo board a live display: not this board here."""
    display = getattr(daemon.app.state, "display", None)
    if display is not None:
        display.no_display(BOARD)


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def scroll_to(page, testid):
    page.locator(f'[data-testid="{testid}"]').scroll_into_view_if_needed()
    page.wait_for_timeout(300)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_panel_linux_overview_and_details(page_factory, daemon, engine, review, scheme):
    panel = daemon.app.state.panel
    linux(engine)
    panel.watcher(BOARD, "bob@srv03340")
    page = page_factory(scheme, **APP)
    open_board(page)
    expect(page.locator('[data-testid="tile-panel-line"]')).to_have_text(
        "status page · harness owns it · touch unknown", timeout=T)
    panel.set_touch(BOARD, True)
    expect(page.locator('[data-testid="tile-panel-line"]')).to_contain_text("touch ok", timeout=T)
    page.locator('[data-testid="tile-identify"] [data-action="identify"]').click()
    expect(page.locator('[data-testid="tile-identify"] [data-testid="identify-until"]')).to_contain_text(
        re.compile(r"blinking until"), timeout=T)
    page.wait_for_timeout(300)
    page.screenshot(path=str(review / f"panel-overview-linux-{scheme}.png"))
    details(page)
    panel.tap(BOARD, "identify")
    expect(page.locator('[data-testid="panel-taps"] li')).to_have_count(1, timeout=T)
    scroll_to(page, "panel-card")
    page.screenshot(path=str(review / f"panel-details-linux-{scheme}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_panel_dut_held_and_touch_lost(page_factory, daemon, engine, review, scheme):
    panel = daemon.app.state.panel
    linux(engine)
    page = page_factory(scheme, **APP)
    open_board(page)
    details(page)
    panel.set_touch(BOARD, False, bus_lost=3, recoveries=1)
    panel.set_owner(BOARD, "dut")
    expect(page.locator('[data-testid="panel-owner-chip"]')).to_have_attribute("data-level", "held", timeout=T)
    expect(page.locator('[data-part="touch"]').first).to_contain_text("touch unavailable", timeout=T)
    scroll_to(page, "panel-card")
    page.screenshot(path=str(review / f"panel-details-dut-held-{scheme}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_panel_bare_metal_rebuilt(page_factory, review, scheme):
    page = page_factory(scheme, **APP)
    open_board(page)
    expect(page.locator('[data-testid="tile-identify"] [data-testid="reason-identify"]')).to_contain_text(
        "needs harness feature 'locate'", timeout=T)
    page.screenshot(path=str(review / f"panel-overview-bare-metal-{scheme}.png"))
    details(page)
    expect(page.locator('[data-testid="panel-mirror"]')).to_have_attribute("data-source", "rebuilt")
    scroll_to(page, "panel-card")
    page.screenshot(path=str(review / f"panel-details-bare-metal-{scheme}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_panel_request_tap_on_the_holders_prompt(page_factory, daemon, engine, review, scheme):
    linux(engine)
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")
    page = page_factory(scheme, **APP)
    open_board(page)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    rid = daemon.app.state.sim.requests.incoming(BOARD, by="bob@lab-pc-02", message="demo at 3")
    prompt = page.locator(f'[data-testid="lease-wanted"][data-request="{rid}"]')
    expect(prompt).to_be_visible(timeout=T)
    daemon.app.state.panel.tap(BOARD, "request")
    expect(prompt.locator('[data-testid="wanted-tapped"]')).to_be_visible(timeout=T)
    page.wait_for_timeout(300)
    page.screenshot(path=str(review / f"panel-request-tapped-{scheme}.png"))
