"""DEMO-ALL review screenshots, light and dark: the demo (``harness-manager app --demo``), one
picture per section and per feature, over the REAL daemon and ``DemoEngine(showcase=True)``.

They land in tests/web/screenshots/review/demo-*.png (gitignored); the curated copies for
david are committed as docs/review/2026-09-26/demo-*.png. Each shot asserts the state it
photographs first, so a picture never shows a broken or half-loaded page.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager import demo_catalog as cat
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_V011
from tests.web.test_demo_all_browser import (
    Showcase,
    T,
    by_id,
    make_showcase,
    open_board,
    section,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def chatty(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    """The demo as ``--demo`` serves it (console chatter on)."""
    yield from make_showcase(browser, tmp_path, monkeypatch, request, chatter=True)


@pytest.fixture
def staged(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request, app_update="staged")


def shoot(page: Any, review: Any, name: str, scheme: str, *, full: bool = False) -> None:
    page.wait_for_timeout(500)
    assert not page.errors, page.errors
    page.screenshot(path=str(review / f"demo-{name}-{scheme}.png"), full_page=full)


def _linux(page: Any, review: Any, scheme: str) -> None:
    open_board(page, BOARD_LINUX)
    expect(by_id(page, "tile-card")).to_contain_text("OS A:valid* B:valid", timeout=T)
    expect(by_id(page, "tile-claim")).to_contain_text("claimed by you")
    expect(by_id(page, "tile-panel-line")).to_contain_text("harness owns it", timeout=T)
    expect(by_id(page, "tile-xvc")).to_have_attribute("data-state", "down", timeout=T)
    shoot(page, review, "overview-linux", scheme)

    page.locator('[data-action="details"]').click()
    expect(by_id(page, "panel-sessions")).to_contain_text("bob@lab-pc-03", timeout=T)
    expect(by_id(page, "panel-touch")).to_be_visible()
    by_id(page, "panel-card").scroll_into_view_if_needed()
    # LM4: the Linux board's panel is the Live display (the text mirror is its fallback)
    expect(by_id(page, "live-display")).to_have_attribute("data-live", "yes", timeout=T)
    shoot(page, review, "details-panel-linux", scheme, full=True)
    page.locator('[data-action="details"]').click()             # closed again for the rest

    section(page, "program")
    page.locator('[data-overlay="nanosoc_upy"]').click()
    expect(by_id(page, "preflight-summary")).to_be_visible(timeout=T)
    by_id(page, "keep-on-card").check()
    expect(by_id(page, "keep-on-card")).to_be_checked()
    shoot(page, review, "program-keep-on-card", scheme)

    section(page, "consoles")
    page.wait_for_timeout(2500)                                  # a heartbeat or two
    shoot(page, review, "consoles", scheme)

    section(page, "debug")
    page.locator('[data-action="xvc_open"]').click()
    expect(by_id(page, "xvc-state")).to_contain_text("ready", timeout=T)
    shoot(page, review, "debug-xvc-open-linux", scheme, full=True)


def _bare_metal(page: Any, review: Any, scheme: str) -> None:
    open_board(page, BOARD_V011)
    expect(by_id(page, "tile-panel-rebuilt")).to_be_visible(timeout=T)
    expect(by_id(page, "tile-locate")).to_have_attribute("aria-disabled", "true")   # LOCATE
    expect(by_id(page, "tile-temp")).to_contain_text("41", timeout=T)
    shoot(page, review, "overview-bare-metal", scheme)

    section(page, "debug")
    expect(by_id(page, "xvc-card")).to_contain_text("unauthenticated", timeout=T)
    shoot(page, review, "debug-xvc-warning-bare-metal", scheme, full=True)

    section(page, "build")
    # UI v2 Build (lane UI2-BUILD): target and kit done = Setup done, or warning for Vivado only
    expect(by_id(page, "bd-node-setup")).to_have_attribute("data-state", re.compile(r"^(done|warn)$"), timeout=T)
    expect(by_id(page, "bd-panel")).to_have_attribute("data-step", "design")
    shoot(page, review, "build", scheme, full=True)

    section(page, "update")
    by_id(page, "harness-all").check()
    page.locator('[data-action="harness_refresh"]').click()
    expect(page.locator('[data-testid="harness-rows"] > li')).to_have_count(4, timeout=T)
    verdicts = by_id(page, "harness-rows").locator('[data-testid="verdict"]')
    expect(verdicts).to_have_count(4)
    expect(by_id(page, "harness-rows")).to_contain_text("re-key")
    expect(by_id(page, "pinned-chip")).to_be_visible()
    page.locator('[data-action="harness-history"]').click()
    expect(by_id(page, "harness-history")).to_contain_text("1.0.0", timeout=T)
    shoot(page, review, "update-harness-versions", scheme, full=True)

    for key in ("xdc", "power", "clocks", "sd", "activity"):
        section(page, key)
        page.wait_for_timeout(800)
        shoot(page, review, key, scheme, full=True)


def _leased(page: Any, review: Any, scheme: str) -> None:
    open_board(page, BOARD_LEASED)
    expect(by_id(page, "lease-request")).to_contain_text("alice@lab-pc-07", timeout=T)
    expect(page.locator('[data-attention="lease"]')).to_be_visible()
    shoot(page, review, "overview-leased", scheme)
    page.locator('[data-action="lease_force_open"]').click()
    expect(by_id(page, "force-confirm")).to_be_visible(timeout=T)
    expect(by_id(page, "force-board")).to_be_visible()
    shoot(page, review, "lease-force-confirm", scheme)
    page.locator('[data-action="force_cancel"]').click()


@SCHEMES
def test_review_the_demo_section_by_section(chatty, review, scheme):
    page = chatty.page(scheme)
    _linux(page, review, scheme)
    _bare_metal(page, review, scheme)
    _leased(page, review, scheme)


@SCHEMES
def test_review_the_staged_update_banner(staged, review, scheme):
    page = staged.page(scheme)
    open_board(page, BOARD_V011)
    expect(by_id(page, "app-update-banner")).to_contain_text(cat.staged_version(), timeout=T)
    shoot(page, review, "update-banner-staged", scheme)
