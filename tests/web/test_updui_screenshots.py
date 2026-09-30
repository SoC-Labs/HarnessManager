"""Lane UPDATE-UI review screenshots, light and dark, over the T14 mock: the app-update banner,
the apply confirm, the restart overlay, the rolled-back banner, Settings under an admin
policy, the Harness versions card (verdicts, what changes, a re-key install, a lease held by
someone else) and the Overview's XVC line.

They land in tests/web/screenshots/review/ (gitignored); the curated copies for david are
committed as docs/review/2026-09-25/update-*.png and harness-*.png. Each test asserts the
state it photographs, so a picture never shows a broken page.
"""

from __future__ import annotations

import time

import pytest

from harness_manager.core.events import Event
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, FIELDED_FEATURES
from tests.web.test_updui_browser import (
    APP,
    NEW,
    POLICY,
    T,
    banner,
    by_id,
    harness_page,
    harness_sim,
    install_rekey,
    open_board,
    open_settings,
    row,
    sim_of,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])
TALL = 1800                    # the harness card grows past the app window: photograph all of it
NOTES = ("Restart to update keeps the port and the token: open pages reconnect by themselves.\n"
         "Console PTYs keep their paths; re-run screen on them after the restart.")


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def shoot(page, review, name, locator=None):
    page.wait_for_timeout(400)
    assert not page.errors, page.errors
    if locator is None:
        page.screenshot(path=str(review / name))
    else:
        locator.screenshot(path=str(review / name))


def staged(daemon, engine):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    engine.bus.publish(Event("update.app.staged", "", {"version": NEW, "channel": "stable",
                                                       "notes": NOTES}))
    return sim


@SCHEMES
@pytest.mark.week_plan("update_api", sim=True)
def test_review_update_banner_staged(page_factory, daemon, engine, review, scheme):
    sim_of(daemon).staged = [NEW]
    page = page_factory(scheme, **APP)
    open_board(page)
    expect(banner(page)).to_be_visible(timeout=T)
    engine.bus.publish(Event("update.app.staged", "", {"version": NEW, "channel": "stable",
                                                       "notes": NOTES}))
    banner(page).locator('[data-action="app-notes"]').click(timeout=T)
    expect(by_id(page, "app-notes")).to_contain_text("keeps the port and the token")
    shoot(page, review, f"update-banner-staged-{scheme}.png")


@SCHEMES
@pytest.mark.week_plan("update_api", sim=True)
def test_review_update_confirm_soft_busy(page_factory, daemon, review, scheme):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.restart_hold.clear()
    sim.app_soft_busy = [
        {"kind": "gdb", "board_id": BOARD_USB, "detail": f"OpenOCD for GDB on {BOARD_USB} (port 3333)"},
        {"kind": "xvc", "board_id": BOARD_USB, "detail": f"the XVC session on {BOARD_USB}, hw_server attached"},
        {"kind": "screen", "board_id": BOARD_USB, "name": "uart0", "clients": 1,
         "path": "/tmp/harness-manager-dam1n19/mps3-01-uart0",
         "detail": "1 terminal(s) on /tmp/harness-manager-dam1n19/mps3-01-uart0 (re-attach after the restart)"}]
    page = page_factory(scheme, **APP)
    open_board(page)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    expect(by_id(page, "apply-confirm").locator('[data-kind="screen"]')).to_be_visible(timeout=T)
    shoot(page, review, f"update-confirm-{scheme}.png")
    sim.restart_hold.set()


@SCHEMES
@pytest.mark.week_plan("update_api", sim=True)
def test_review_update_restart_overlay(page_factory, daemon, review, scheme):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.restart_hold.clear()
    page = page_factory(scheme, **APP)
    open_board(page)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    expect(by_id(page, "restart-overlay")).to_contain_text("Reconnecting", timeout=T)
    shoot(page, review, f"update-overlay-{scheme}.png")
    sim.apply_outcome = "rolled-back"          # end it without a reload
    sim.restart_hold.set()
    expect(by_id(page, "app-rolled-back")).to_be_visible(timeout=T)


@SCHEMES
@pytest.mark.week_plan("update_api", sim=True)
def test_review_update_rolled_back(page_factory, daemon, review, scheme):
    sim = sim_of(daemon)
    why = f"/health did not report {NEW} within 30 s (the daemon exited while starting)"
    sim.bad_versions[NEW] = {"reason": why, "phase": "health", "at": time.time() - 40}
    sim.app_last_apply = {"id": "a1b2c3", "from": "0.0.1", "to": NEW, "result": "rolled-back",
                          "phase": "health", "reason": why, "at": time.time() - 40, "seconds": 31.2}
    page = page_factory(scheme, **APP)
    open_board(page)
    expect(by_id(page, "app-rolled-back")).to_contain_text("It won't be offered again.", timeout=T)
    shoot(page, review, f"update-rolled-back-{scheme}.png")


@SCHEMES
@pytest.mark.week_plan("update_api", sim=True)
def test_review_update_settings_admin_policy(page_factory, daemon, review, scheme):
    sim = sim_of(daemon)
    sim.app_policy = dict(POLICY)
    sim.bad_versions["0.1.1"] = {"reason": "/health did not report 0.1.1 within 30 s", "phase": "health",
                                 "at": time.time() - 86400}
    sim.app_last_check = {"at": time.time() - 900, "interval_s": 43200, "channel": "stable",
                          "serial": 9, "available": "", "error": "", "mode": "notify"}
    page = page_factory(scheme, **APP)
    open_board(page)
    card = open_settings(page)
    expect(card.locator('[data-testid="policy-note"]')).to_be_visible(timeout=T)
    expect(card.locator('[data-testid="bad-versions"]')).to_contain_text("0.1.1")
    shoot(page, review, f"update-settings-policy-{scheme}.png", by_id(page, "settings"))


@SCHEMES
@pytest.mark.week_plan("xvc_api", sim=True)
def test_review_update_overview_xvc_line(page_factory, daemon, engine, review, scheme):
    engine._set_identity(BOARD_FIELDED, features=(*FIELDED_FEATURES, "xvc_dbgbr"),
                         rm_id="0x0100000A", rm_name="nanosoc_ila")
    page = page_factory(scheme, **APP)
    open_board(page, BOARD_FIELDED)
    daemon.app.state.xvc.open(BOARD_FIELDED, byo=False)
    daemon.app.state.xvc.attach(BOARD_FIELDED)
    expect(by_id(page, "tile-xvc")).to_have_attribute("data-state", "attached", timeout=T)
    shoot(page, review, f"update-overview-xvc-line-{scheme}.png", by_id(page, "tile-consoles"))


@SCHEMES
@pytest.mark.week_plan("harness_api", sim=True)
def test_review_harness_versions(page_factory, daemon, review, scheme):
    harness_sim(daemon).withdrawn = {"1.1.0"}
    page, card = harness_page(page_factory, scheme=scheme, all_channels=True, height=TALL)
    row(card, "1.1.1").locator('[data-action="changes"]').click()
    expect(row(card, "1.1.1").locator('[data-testid="changes"]')).to_contain_text("re-key")
    expect(row(card, "2.0.0")).to_have_attribute("data-verdict", "needs-door")
    shoot(page, review, f"harness-versions-{scheme}.png", card)


@SCHEMES
@pytest.mark.week_plan("harness_api", sim=True)
def test_review_harness_install_rekey(page_factory, daemon, review, scheme):
    page, card = harness_page(page_factory, scheme=scheme, height=TALL)
    panel = install_rekey(page, card)
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text("installed: harness 1.1.1",
                                                                            timeout=T)
    expect(row(card, "1.1.1").locator('[data-testid="marks"]')).to_contain_text("running", timeout=T)
    shoot(page, review, f"harness-install-rekey-{scheme}.png", card)


@SCHEMES
@pytest.mark.week_plan("harness_api", sim=True)
def test_review_harness_lease_held(page_factory, daemon, review, scheme):
    sim_of(daemon).behind_hub(BOARD_USB, lease="other")
    page, card = harness_page(page_factory, scheme=scheme, height=TALL)
    panel = install_rekey(page, card)
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text("holder: alice@lab-pc-07",
                                                                            timeout=T)
    shoot(page, review, f"harness-lease-held-{scheme}.png", panel)


@SCHEMES
@pytest.mark.week_plan("harness_api", sim=True)
def test_review_harness_rollback(page_factory, daemon, review, scheme):
    page, card = harness_page(page_factory, scheme=scheme, height=TALL)
    install_rekey(page, card)
    expect(row(card, "1.1.1").locator('[data-testid="marks"]')).to_contain_text("running", timeout=T)
    card.locator('[data-action="harness-history"]').click()
    expect(by_id(page, "harness-history")).to_contain_text("1.1.1", timeout=T)
    card.locator('[data-action="harness-rollback"]').click()
    expect(by_id(page, "harness-rollback")).to_have_attribute("data-release", "1.0.0", timeout=T)
    shoot(page, review, f"harness-rollback-{scheme}.png", card)
