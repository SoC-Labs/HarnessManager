"""Lane LM4 review screenshots: the Live display in light and dark.

Over the T14 mock (its DisplaySim scripts each state) and over the demo daemon (``app
--demo``'s Linux board). They land in tests/web/screenshots/review/ (gitignored); the curated
copies for david are committed as docs/review/2026-09-27/display-*.png. Each test asserts the
state it photographs, so a picture never shows a broken page.
"""

from __future__ import annotations

import pytest

from harness_manager.core import display_wire as w
from harness_manager.demo_showcase import BOARD_LINUX
from tests.web.test_demo_all_browser import make_showcase
from tests.web.test_demo_all_browser import open_board as open_showcase_board
from tests.web.test_lm4_display_browser import (
    BOARD,
    HOLDER,
    T,
    by_id,
    is_live,
    live_page,
    show_display,
    sim,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]
MOCK = pytest.mark.week_plan("display_api", sim=True)
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def shoot(page, review, name, scheme, testid="panel-card"):
    page.wait_for_timeout(500)
    assert not page.errors, page.errors
    by_id(page, testid).screenshot(path=str(review / f"display-{name}-{scheme}.png"))


@MOCK
@SCHEMES
def test_review_live_1x(page_factory, daemon, review, scheme):
    sim(daemon).freeze(BOARD)
    page = live_page(page_factory, scheme)
    is_live(page)
    expect(by_id(page, "live-freshness")).to_contain_text("fps")
    shoot(page, review, "live-1x", scheme)


@MOCK
@SCHEMES
def test_review_live_2x(page_factory, daemon, review, scheme):
    sim(daemon).freeze(BOARD)
    page = live_page(page_factory, scheme, width=1920, height=1200)
    is_live(page)
    page.locator('[data-testid="live-display"] .seg button:has-text("2x")').click()
    expect(by_id(page, "live-display")).to_have_attribute("data-k", "2")
    shoot(page, review, "live-2x", scheme, "live-display")
    page.locator('[data-testid="live-display"] .seg button:has-text("1x")').click()


@MOCK
@SCHEMES
def test_review_handover_hatched_and_held(page_factory, daemon, review, scheme):
    s = sim(daemon)
    s.freeze(BOARD)
    page = live_page(page_factory, scheme)
    is_live(page)
    m = s.mirror(BOARD)
    m.handover(w.OWNER_DUT)
    with m.edit() as p:                                   # the DUT has painted the top half
        for t in range(150):
            p.fill_tile(t, (0x001F, 0x07E0, 0xF800)[(t // 20) % 3])
    expect(by_id(page, "live-display")).to_have_attribute("data-hatched", "150", timeout=T)
    expect(page.locator('[data-badge="held"]')).to_be_visible()
    shoot(page, review, "hatched-held", scheme)


@MOCK
@SCHEMES
def test_review_blind(page_factory, daemon, review, scheme):
    s = sim(daemon)
    s.freeze(BOARD)
    s.mirror(BOARD).mode = "sw"
    page = live_page(page_factory, scheme)
    is_live(page)
    m = s.mirror(BOARD)
    m.handover(w.OWNER_DUT)
    with m.edit() as p:
        p.valid = set(range(w.NTILES))
    expect(by_id(page, "live-display")).to_have_attribute("data-grey", "yes", timeout=T)
    shoot(page, review, "blind", scheme)


@MOCK
@SCHEMES
def test_review_warnings_and_dim(page_factory, daemon, review, scheme):
    s = sim(daemon)
    s.freeze(BOARD)
    page = live_page(page_factory, scheme)
    is_live(page)
    with s.mirror(BOARD).edit() as p:
        p.csr |= w.ST_VIOL
        p.csr &= ~w.ST_BL
        p.regs[0x36] = 0x08
    expect(by_id(page, "live-display")).to_have_attribute("data-dim", "yes", timeout=T)
    expect(page.locator('[data-badge="viol"]')).to_be_visible()
    expect(page.locator('[data-badge="inexact"]')).to_be_visible()
    shoot(page, review, "warn-dim", scheme)


@MOCK
@SCHEMES
def test_review_stale(page_factory, daemon, review, scheme):
    sim(daemon).freeze(BOARD)
    page = live_page(page_factory, scheme)
    is_live(page)
    sim(daemon).stall(BOARD)
    expect(by_id(page, "live-display")).to_have_attribute("data-stale", "yes", timeout=8000)
    shoot(page, review, "stale", scheme)


@MOCK
@SCHEMES
def test_review_refused_409_text_mirror(page_factory, daemon, review, scheme):
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder=HOLDER)
    page = live_page(page_factory, scheme)
    expect(by_id(page, "live-display")).to_have_attribute("data-refused", "HELD", timeout=T)
    expect(by_id(page, "panel-mirror")).to_be_visible()
    shoot(page, review, "refused-409", scheme)


@SCHEMES
def test_review_demo_linux_board(browser, tmp_path, monkeypatch, request, review, scheme):
    for show in make_showcase(browser, tmp_path, monkeypatch, request):
        page = show.page(scheme)
        open_showcase_board(page, BOARD_LINUX)
        show_display(page)
        is_live(page)
        expect(by_id(page, "live-mode")).to_have_text("software tap")
        shoot(page, review, "demo-linux", scheme)
