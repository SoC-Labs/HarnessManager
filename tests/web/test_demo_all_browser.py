"""DEMO-ALL in the browser: ``harness-manager app --demo`` shows every feature.

The REAL daemon app over ``DemoEngine(showcase=True)`` (what ``--demo`` serves), in the
system Chrome. Each board's Overview lines are checked against what the board is for, with
the board that must NOT show them as the negative twin; the app-update banner stays hidden
unless the demo knob stages an update. ``test_demo_all_screenshots.py`` photographs the
same demo for the review.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager import demo_catalog as cat
from harness_manager.demo import DemoEngine
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_V011
from tests.fakes.t14_mock_api import real_daemon
from tests.web import nav
from tests.web.conftest import dump_failed_pages

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 1000}
TOKEN = "demo-all"


class Showcase:
    """The demo daemon and its engine, and pages on it."""

    def __init__(self, browser: Any, daemon: Any, engine: DemoEngine) -> None:
        self.browser, self.daemon, self.engine = browser, daemon, engine
        self.contexts: list[Any] = []
        self.pages: list[Any] = []

    def page(self, scheme: str = "light", *, width: int = APP["width"],
             height: int = APP["height"]) -> Any:
        ctx = self.browser.new_context(viewport={"width": width, "height": height},
                                       color_scheme=scheme, reduced_motion="reduce")
        self.contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error"
                and "status of 4" not in m.text and "status of 5" not in m.text else None)
        page.goto(self.daemon.ui_url)
        page.wait_for_selector(".board-item", timeout=T)
        self.pages.append(page)
        return page

    def close(self) -> None:
        for ctx in self.contexts:
            ctx.close()


def make_showcase(browser: Any, tmp_path: Any, monkeypatch: Any, request: Any, *,
                  app_update: str = "", chatter: bool = False) -> Iterator[Showcase]:
    monkeypatch.delenv(cat.UPDATE_ENV, raising=False)
    sdir = tmp_path / "demo"
    engine = DemoEngine(speed=0.25, showcase=True, state_dir=sdir, app_update=app_update,
                        console_chatter=chatter)
    try:
        with real_daemon(engine, token=TOKEN, state_dir=sdir) as d:
            show = Showcase(browser, d, engine)
            try:
                yield show
            finally:
                dump_failed_pages(request, show.pages)
                show.close()
    finally:
        engine.close_all()


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


@pytest.fixture
def staged_showcase(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request, app_update="staged")


# --- helpers (test_demo_all_screenshots.py uses them too) ---------------------------------------


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def open_board(page: Any, bid: str) -> None:
    """Select ``bid`` in the rail, open it if it is not open here, wait for its header
    (tests/web/nav.py, shared by the files that import this one)."""
    nav.open_board(page, bid)


def section(page: Any, key: str) -> None:
    """0.1.0's tab ``key`` where UI v2 put it (tests/web/nav.py)."""
    nav.section(page, key)


# --- each board's lines ---------------------------------------------------------------------------


def test_the_linux_board_shows_card_claim_panel_identify_and_xvc(showcase):
    page = showcase.page()
    open_board(page, BOARD_LINUX)
    # UI v2 round 3: the Overview's Design card (Boots next, the OS slots), the Front panel, and
    # its Consoles and debug card; the SSH claim is on Board > Access (not claimed: attention)
    expect(by_id(page, "tile-card")).to_contain_text("nanosoc", timeout=T)
    expect(by_id(page, "tile-card")).to_contain_text("kept on the card [A]")
    expect(by_id(page, "ov-os-slots").locator('[data-slot="A"]')).to_contain_text("booted")
    expect(page.locator('[data-attention="claim"]')).to_have_count(0)           # claimed by you
    expect(by_id(page, "panel-line")).to_contain_text("harness owns it", timeout=T)
    expect(by_id(page, "panel-card")).not_to_have_attribute("data-view", "text")
    expect(by_id(page, "panel-identify").locator('[data-action="identify"]')).to_be_visible()
    expect(by_id(page, "tile-xvc")).to_have_attribute("data-state", "down", timeout=T)
    section(page, "program")
    expect(by_id(page, "keep-on-card")).to_be_enabled(timeout=T)
    section(page, "debug")
    page.locator('[data-action="xvc_open"]').click()
    expect(by_id(page, "xvc-state")).to_contain_text("ready", timeout=T)
    assert not page.errors, page.errors


def test_negative_twin_the_bare_metal_board_shows_the_rebuilt_panel_and_no_card(showcase):
    page = showcase.page()
    open_board(page, BOARD_V011)
    expect(by_id(page, "tile-card")).to_contain_text("no card store on this harness", timeout=T)
    expect(page.locator('[data-attention="claim"]')).to_have_count(0)   # no SSH on bare metal
    expect(by_id(page, "panel-headline")).to_have_attribute("data-state", "rebuilt", timeout=T)
    expect(by_id(page, "panel-identify").locator('[data-action="identify"]')).to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "reason-identify")).to_contain_text("harness feature 'locate'")
    section(page, "program")
    expect(by_id(page, "keep-card")).to_have_count(0)
    section(page, "debug")
    expect(by_id(page, "xvc-card")).to_contain_text("unauthenticated", timeout=T)
    assert not page.errors, page.errors


def test_the_leased_board_offers_the_queue_and_force_release(showcase):
    page = showcase.page()
    open_board(page, BOARD_LEASED)
    expect(by_id(page, "lease-request")).to_contain_text("held by alice@lab-pc-07", timeout=T)
    expect(by_id(page, "req-position")).to_contain_text("position 1")
    # UI v2: the Overview's Lease card (not an attention row): held by alice, and the queue
    lease = by_id(page, "ov-lease").locator('[data-testid="tile-lease"]')
    expect(lease).to_have_attribute("data-lease", "other", timeout=T)
    expect(by_id(page, "ov-lease-chip")).to_contain_text("Held by alice")
    page.locator('[data-action="lease_force_open"]').click()
    expect(by_id(page, "force-confirm")).to_be_visible(timeout=T)
    by_id(page, "force-board-name").fill("mps3-02")
    page.locator('[data-action="force_confirm"]').click()
    expect(lease).not_to_have_attribute("data-lease", "other", timeout=T)
    assert not page.errors, page.errors


def test_negative_twin_the_desk_boards_have_no_hub_or_lease(showcase):
    page = showcase.page()
    for bid in (BOARD_LINUX, BOARD_V011):
        open_board(page, bid)
        expect(by_id(page, "ov-access")).to_contain_text("No hub lease", timeout=T)
        expect(by_id(page, "ov-lease")).to_have_count(0)
        expect(by_id(page, "lease-request")).to_have_count(0)
    assert not page.errors, page.errors


# --- the app-update banner: off by default --------------------------------------------------------


def test_the_update_banner_stays_hidden_by_default(showcase):
    page = showcase.page()
    open_board(page, BOARD_V011)
    page.wait_for_timeout(1500)                        # the page's first GET /update/app landed
    expect(by_id(page, "app-update-banner")).to_have_count(0)
    section(page, "update")
    expect(nav.panel(page, "update")).to_contain_text("never updates itself", timeout=T)


def test_negative_twin_the_demo_knob_shows_the_staged_banner(staged_showcase):
    page = staged_showcase.page()
    expect(by_id(page, "app-update-banner")).to_have_attribute("data-kind", "staged", timeout=T)
    expect(by_id(page, "app-update-banner")).to_contain_text(cat.staged_version())
