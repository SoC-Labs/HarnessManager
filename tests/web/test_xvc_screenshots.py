"""Lane XVC-UI review screenshots: the Debug section's XVC card in light and dark, over the
T14 mock (tests/fakes/x3_mock_xvc.py).

They land in tests/web/screenshots/review/ (gitignored); the curated copies for david are
committed as docs/review/2026-09-25/xvc-*.png. Each test asserts the state it photographs,
so a picture never shows a broken page.
"""

from __future__ import annotations

import pytest

from harness_manager.core.events import Event
from tests.web.test_xvc_card_browser import (
    BOARD,
    SCOPE,
    T,
    button,
    by_id,
    debug_page,
    sim,
    state_is,
    xvc_board,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("xvc_api", sim=True)]
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def shoot(page, review, name, *, whole=False):
    page.wait_for_timeout(400)
    assert not page.errors, page.errors
    expect(page.locator('[data-testid="xvc-card"] .card-sub')).to_have_attribute("title", SCOPE)
    if whole:
        page.screenshot(path=str(review / name), full_page=True)
    else:
        page.locator('[data-testid="xvc"]').screenshot(path=str(review / name))


@SCHEMES
def test_review_xvc_attached(page_factory, daemon, engine, review, scheme):
    xvc_board(engine)
    page = debug_page(page_factory, scheme)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    sim(daemon).attach(BOARD, command="hw_server -q -p0 -s TCP:127.0.0.1:23601 "
                                      "-e set auto-open-servers xilinx-xvc:127.0.0.1:23600 "
                                      "-e set jtag-port-filter Xilinx/XVC/127.0.0.1:23600")
    state_is(page, "attached")
    expect(by_id(page, "xvc-unauth")).to_be_visible()
    expect(by_id(page, "xvc-ltx")).to_contain_text("nanosoc_ila.ltx")
    shoot(page, review, f"xvc-attached-{scheme}.png", whole=True)


@SCHEMES
def test_review_xvc_held(page_factory, daemon, engine, review, scheme):
    xvc_board(engine)
    sim(daemon).hold_slot(BOARD, who="alice's Vivado on the hub")
    page = debug_page(page_factory, scheme)
    button(page, "xvc_open").click()
    state_is(page, "held")
    expect(by_id(page, "xvc-held")).to_contain_text("alice's Vivado on the hub")
    shoot(page, review, f"xvc-held-{scheme}.png")


@SCHEMES
def test_review_xvc_swapping_then_reattached(page_factory, daemon, engine, review, scheme):
    xvc_board(engine)
    page = debug_page(page_factory, scheme)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    engine.bus.publish(Event("deploy.started", BOARD, {"overlay": "nanosoc_ila_b"}))
    state_is(page, "swapping")
    expect(by_id(page, "xvc-swapping")).to_be_visible()
    shoot(page, review, f"xvc-swapping-{scheme}.png")
    engine._set_identity(BOARD, rm_id="0x0100000B", rm_name="nanosoc_ila_b")
    engine.bus.publish(Event("deploy.done", BOARD, {"verified": True, "rm_id": "0x0100000B"}))
    state_is(page, "ready")
    expect(by_id(page, "xvc-reattached")).to_contain_text("nanosoc_ila_b")
    expect(by_id(page, "xvc-tcl")).to_contain_text("nanosoc_ila_b.ltx")
    shoot(page, review, f"xvc-reattached-{scheme}.png")


@SCHEMES
def test_review_xvc_not_the_lease_holder(page_factory, daemon, engine, review, scheme):
    xvc_board(engine)
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder="alice@lab-pc-07")
    page = debug_page(page_factory, scheme)
    expect(by_id(page, "reason-xvc_open")).to_contain_text("lease holder only", timeout=T)
    shoot(page, review, f"xvc-not-lease-holder-{scheme}.png")


@SCHEMES
def test_review_xvc_linux_locked(page_factory, daemon, engine, review, scheme):
    xvc_board(engine, linux=True, lock=True)
    sim(daemon).stage_full(BOARD)
    page = debug_page(page_factory, scheme)
    page.locator('[data-testid="xvc-byo"] input').check()
    button(page, "xvc_open").click()
    state_is(page, "ready")
    expect(by_id(page, "xvc-unauth")).to_have_count(0)
    expect(by_id(page, "xvc-ltx-which")).to_contain_text("full design")
    expect(by_id(page, "xvc-static")).to_contain_text("config_rm_greybox_static.ltx")
    shoot(page, review, f"xvc-linux-byo-{scheme}.png")
