"""PANEL-TRUTH's review screenshots: the Front panel card as david's board 1 shows it now.

They land in tests/web/screenshots/review/paneltruth/ (gitignored); the curated copies are
committed under docs/review/2026-09-28/ as ``panel-*.png``. Each test asserts the state it
photographs, so a picture never shows a broken page.
"""

# ruff: noqa: F811 - the tests take the psim fixture imported below
from __future__ import annotations

import pytest

from harness_manager.demo_showcase import BOARD_LINUX, LINUX_DEMO_FEATURES, PANEL_FEATURES
from tests.web.test_demo_all_browser import make_showcase
from tests.web.test_demo_all_browser import open_board as open_showcase_board
from tests.web.test_lm4_display_browser import is_live, show_display
from tests.web.test_p3_panel_ui import details, open_board
from tests.web.test_paneltruth_browser import (
    APP,
    BOARD,
    T,
    card,
    linux_without_panel,
    no_display,
    psim,  # noqa: F401 - the fixture
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]


@pytest.fixture
def review(screenshots):
    out = screenshots / "review" / "paneltruth"
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request):
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def shoot(page, review, name):
    c = card(page)
    c.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    c.screenshot(path=str(review / f"{name}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_linux_without_panel_features_live(showcase, review, scheme):
    """david's board after the fix: the Linux harness without 'panel', its Live display on."""
    showcase.engine._set_identity(BOARD_LINUX, features=tuple(
        f for f in LINUX_DEMO_FEATURES if f not in PANEL_FEATURES))
    page = showcase.page(scheme)
    open_showcase_board(page, BOARD_LINUX)
    show_display(page)
    is_live(page)
    expect(card(page).locator('[data-testid="panel-headline"]')).to_have_attribute(
        "data-state", "live", timeout=T)
    card(page).locator('[data-testid="panel-not-reported"] summary').click()
    shoot(page, review, f"panel-linux-no-panel-live-{scheme}")


@pytest.mark.week_plan("display_api", sim=True)
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_checking_the_lease_with_the_hub(page_factory, daemon, engine, psim, review,
                                               scheme):
    linux_without_panel(engine)
    daemon.app.state.display.checking(BOARD, times=200)
    page = page_factory(scheme, **APP)
    open_board(page)
    details(page)
    show_display(page)                                   # it opens only while on screen
    expect(page.locator('[data-testid="live-display"]')).to_have_attribute(
        "data-state", "checking", timeout=T)
    page.locator('[data-testid="live-detail"] summary').click()
    shoot(page, review, f"panel-checking-lease-{scheme}")


@pytest.mark.mock_too
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_linux_without_panel_features_rebuilt(page_factory, daemon, engine, psim, review,
                                                    scheme):
    no_display(daemon)
    linux_without_panel(engine)
    page = page_factory(scheme, **APP)
    open_board(page)
    details(page)
    expect(card(page).locator('[data-testid="panel-mirror-legend"]')).to_be_visible(timeout=T)
    card(page).locator('[data-testid="panel-not-reported"] summary').click()
    shoot(page, review, f"panel-linux-no-panel-rebuilt-{scheme}")


@pytest.mark.mock_too
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_bare_metal_rebuilt(page_factory, daemon, engine, psim, review, scheme):
    no_display(daemon)
    engine._set_identity(BOARD, harness_impl="bare-metal")
    page = page_factory(scheme, **APP)
    open_board(page)
    expect(page.locator('[data-testid="panel-line"]')).to_contain_text("harness owns it",
                                                                       timeout=T)
    page.locator('[data-testid="panel-card"]').screenshot(
        path=str(review / f"panel-card-bare-metal-{scheme}.png"))
    details(page)
    expect(card(page).locator('[data-testid="panel-rebuilt"]')).to_contain_text(
        "bare-metal harness image", timeout=T)
    shoot(page, review, f"panel-bare-metal-rebuilt-{scheme}")
