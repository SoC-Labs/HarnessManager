"""LEASE-UI review screenshots, light and dark: the lease badges in the side list (free, yours,
held by alice, requested), Release and its confirm, Close board asking about the lease, over
the REAL daemon and the demo (``app --demo``'s mps3-02 and mps3-03); and a lease held by the
same principal in another session, over the T14 mock (the demo's hub cannot make one).

They land in tests/web/screenshots/review/lease-*.png (gitignored); the curated copies for
david are committed as docs/review/2026-09-28/lease-*.png. Each shot asserts the state it
photographs first.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_SPARE
from tests.web.test_demo_all_browser import Showcase, make_showcase
from tests.web.test_lease_ui_browser import (
    ALICE,
    APP,
    BOARD,
    ME,
    T,
    by_id,
    close_board,
    demo_open,
    open_board,
    rail,
    sim,
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
def showcase(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def shoot(page: Any, review: Any, name: str, scheme: str) -> None:
    page.wait_for_timeout(400)
    assert not page.errors, page.errors
    page.mouse.move(1, 1)                        # no hover tooltip in the picture
    page.screenshot(path=str(review / f"lease-{name}-{scheme}.png"))


@SCHEMES
def test_lease_review_screenshots_demo(showcase, review, scheme):
    page = showcase.page(scheme, width=1440, height=900)
    # free and held by alice (with your request in her queue), side by side in the rail
    demo_open(page, BOARD_SPARE)
    spare = rail(page, BOARD_SPARE).locator('[data-testid="rail-lease-badge"]')
    expect(spare).to_have_text("Free", timeout=T)
    demo_open(page, BOARD_LEASED)
    held = rail(page, BOARD_LEASED)
    expect(held.locator('[data-testid="rail-lease-badge"]')).to_have_text(f"Held by {ALICE}", timeout=T)
    expect(held.locator('[data-testid="rail-lease-queued"]')).to_have_text("Requested · #1")
    shoot(page, review, "held-and-free", scheme)

    # yours: Acquire on mps3-03; the header's Release, the Board tile's line, the rail
    demo_open(page, BOARD_SPARE)
    page.locator('[data-testid="fact-hub"] [data-action="lease_acquire"]').click()
    expect(spare).to_have_text("Yours", timeout=T)
    expect(by_id(page, "lease-chip")).to_contain_text("lease yours")
    expect(by_id(page, "tile-lease")).to_have_attribute("data-lease", "here")
    shoot(page, review, "yours", scheme)

    page.locator('[data-testid="fact-hub"] [data-action="lease_release_open"]').click()
    expect(by_id(page, "release-title")).to_have_text("Release mps3_03?")      # LEASE-BOARD
    shoot(page, review, "release-confirm", scheme)
    page.locator('[data-action="release_cancel"]').click()

    close_board(page)
    expect(by_id(page, "close-title")).to_have_text("Also release the lease on mps3_03?")
    shoot(page, review, "close-confirm", scheme)
    page.locator('[data-action="close_cancel"]').click()


@pytest.mark.week_plan("hub_api", sim=True)
@SCHEMES
def test_lease_review_screenshot_another_session(page_factory, daemon, review, scheme):
    sim(daemon).behind_hub(BOARD, lease="elsewhere")
    page = page_factory(scheme, **APP)
    open_board(page)
    expect(rail(page).locator('[data-testid="rail-lease-badge"]')).to_have_text(
        f"Held by {ME} (another session)", timeout=T)
    expect(by_id(page, "ov-lease-chip")).to_contain_text("in another session")
    shoot(page, review, "another-session", scheme)
