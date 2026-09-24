"""Lane LR-D review screenshots: lease requests in light and dark, at the app window's
1440x900, over the T14 mock (tests/fakes/t14_lease_requests.py).

They land in tests/web/screenshots/review/ (gitignored); the curated copies for david are
committed as docs/review/2026-09-24/lease-*.png. Each test asserts the state it
photographs, so a picture never shows a broken page.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_USB
from tests.web.test_lrd_browser import (
    APP,
    HOLDER,
    POLL,
    TARGET,
    T,
    force_button,
    open_board,
    reqs,
    section,
    send_request,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("hub_api", sim=True)]
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def settle(page, ms=400):
    page.wait_for_timeout(ms)


def requesting(page_factory, daemon, scheme):
    daemon.app.state.sim.behind_hub(BOARD_USB, lease="other")
    page = page_factory(scheme, **APP)
    open_board(page)
    return page, send_request(page)


@SCHEMES
def test_review_lease_request_waiting(page_factory, daemon, review, scheme):
    page, bar = requesting(page_factory, daemon, scheme)
    expect(bar.locator('[data-testid="req-countdown"]')).to_contain_text("1:5", timeout=T)
    expect(bar.locator('[data-testid="req-position"]')).to_have_text("position 1 in the queue")
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")
    settle(page)
    assert not page.errors, page.errors
    page.screenshot(path=str(review / f"lease-request-waiting-{scheme}.png"))


@SCHEMES
def test_review_lease_force_confirm(page_factory, daemon, review, scheme):
    page, bar = requesting(page_factory, daemon, scheme)
    reqs(daemon).advance(BOARD_USB, 121)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    force_button(page).click()
    modal = page.locator('[data-testid="force-confirm"]')
    expect(modal.locator('[data-testid="force-what"]')).to_contain_text(f"This kicks {HOLDER} off {TARGET} now")
    settle(page)
    assert not page.errors, page.errors
    page.screenshot(path=str(review / f"lease-force-confirm-{scheme}.png"))
    assert reqs(daemon).revokes == []                    # a picture, not a revoke


@SCHEMES
def test_review_lease_holder_prompt(page_factory, daemon, review, scheme):
    daemon.app.state.sim.behind_hub(BOARD_USB, lease="mine")
    page = page_factory(scheme, **APP)
    open_board(page)
    section(page, "consoles")                            # any section, not only Overview
    reqs(daemon).incoming(BOARD_USB, by="bob@lab-pc-02",
                          message="need it for the 15:00 demo, about an hour", age_s=41)
    prompt = page.locator('[data-testid="lease-wanted"]')
    expect(prompt.locator('[data-testid="wanted-countdown"]')).to_contain_text("1:1", timeout=T)
    prompt.locator('[data-testid="wanted-message"]').fill("finishing a run, ten more minutes")
    settle(page)
    assert not page.errors, page.errors
    page.screenshot(path=str(review / f"lease-holder-prompt-{scheme}.png"))


@SCHEMES
def test_review_lease_victim_banner(page_factory, daemon, review, scheme):
    daemon.app.state.sim.behind_hub(BOARD_USB, lease="mine")
    page = page_factory(scheme, **APP)
    open_board(page)
    section(page, "program")
    reqs(daemon).taken(BOARD_USB, by="bob@lab-pc-02")
    expect(page.locator('[data-testid="lease-taken"]')).to_contain_text(
        f"{TARGET} was force-released by bob@lab-pc-02", timeout=T)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to bob@lab-pc-02", timeout=T)
    settle(page)
    assert not page.errors, page.errors
    page.screenshot(path=str(review / f"lease-victim-banner-{scheme}.png"))
