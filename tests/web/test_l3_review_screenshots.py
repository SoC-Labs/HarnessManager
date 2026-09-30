"""The morning-review screenshots (lane L3): each page in a state worth looking at, at the
app window's 1440x900, over the T14 mock (which plays the week-plan routes).

They land in tests/web/screenshots/review/ (gitignored); the curated copies for david are
committed under docs/review/2026-09-24/. Each test also asserts the state it photographs,
so a picture never shows a broken page.
"""

from __future__ import annotations

import zipfile

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

# sim=True: these are scripted scenes (a meter, a hub, an update channel) only the mock's
# WeekPlanSim can play; the real-daemon behaviour is covered by the other l3 tests.
pytestmark = [pytest.mark.browser, pytest.mark.week_plan("consoles_api", "hub_api", "power_api",
                                                          "update_api", sim=True)]
T = 10_000
APP = {"width": 1440, "height": 900}


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def open_board(page, board_id):
    page.locator(f'.board-item[data-board="{board_id}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)


def section(page, key):
    """0.1.0's tab ``key`` where UI v2 put it (tests/web/nav.py)."""
    nav.section(page, key)


def settle(page, ms=350):
    page.wait_for_timeout(ms)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_overview_demo(page_factory, daemon, engine, review, scheme):
    sim = daemon.app.state.sim
    page = page_factory(scheme, **APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="tiles"]', timeout=T)
    page.locator('[data-action="attach-uart0"]').click()
    page.wait_for_selector('li[data-console="uart0"] code', timeout=T)
    sim.attach_screen(BOARD_USB, "uart0", 1)
    page.locator('[data-testid="tile-debug"] [data-action="up"]').click()
    expect(page.locator('[data-testid="tile-debug-state"]')).to_have_text("up", timeout=T)
    expect(page.locator('li[data-console="uart0"]')).to_contain_text("1 attached")
    settle(page)
    page.screenshot(path=str(review / f"overview-demo-{scheme}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_overview_ethernet_only(page_factory, review, scheme):
    page = page_factory(scheme, **APP)
    open_board(page, BOARD_FIELDED)
    expect(page.locator('[data-attention="build"]')).to_be_visible(timeout=T)
    expect(page.locator('[data-testid="tile-temp"]')).to_contain_text("unavailable")
    settle(page)
    page.screenshot(path=str(review / f"overview-ethernet-only-{scheme}.png"))


def test_review_console_screen_and_fixed_baud(page_factory, daemon, engine, review):
    sim = daemon.app.state.sim
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.wait_for_selector('[data-testid="console-state"]:has-text("up")', timeout=T)
    pane = page.locator('[data-testid="console-uart0"]')
    pane.locator('[data-action="attach-screen"]').click()
    pane.locator('[data-testid="screen-command"]').wait_for(timeout=T)
    sim.attach_screen(BOARD_USB, "uart0", 1)
    engine.inject_console(BOARD_USB, "uart0",
                          "\r\nMicroPython v1.24.0 on 2026-09-23; nanosoc with Cortex-M0\r\n"
                          "Type \"help()\" for more information.\r\n>>> print(1+1)\r\n2\r\n>>> ")
    expect(pane.locator('[data-testid="screen-clients"]')).to_have_text("1 attached")
    expect(pane.locator('[data-testid="baud"]')).to_contain_text("fixed by the nanosoc design")
    settle(page, 600)
    page.screenshot(path=str(review / "console-screen-fixed-baud-light.png"))


def test_review_console_serial_baud_selector(page_factory, review):
    page = page_factory("dark", **APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.locator('[data-console-tab="mcc"]').click()
    pane = page.locator('[data-testid="console-mcc"]')
    pane.locator('[data-testid="baud"] select').select_option("9600")
    expect(pane.locator('[data-testid="console-result"]')).to_contain_text("now 9600 baud")
    settle(page, 500)
    page.screenshot(path=str(review / "console-serial-baud-dark.png"))


def test_review_power(page_factory, daemon, review):
    daemon.app.state.sim.set_power(BOARD_USB)
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    card.locator('[data-testid="arm-power"] input').check()
    card.locator('[data-action="power_cycle"]').click()
    expect(card.locator('[data-testid="power-result"]')).to_contain_text("rc 0", timeout=T)
    settle(page)
    page.screenshot(path=str(review / "power-light.png"))


def test_review_update(page_factory, review):
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    section(page, "update")
    page.locator('[data-action="update_check"]').click()
    plan = page.locator('[data-testid="update-plan"]')
    expect(plan.locator('[data-testid="rekey-chip"]')).to_be_visible(timeout=T)
    plan.locator('[data-testid="rekey-phrase"]').fill("REKEY 0x72bb0a36")
    plan.locator('[data-testid="arm-update"] input').check()
    settle(page)
    page.screenshot(path=str(review / "update-plan-light.png"))
    plan.locator('[data-action="update_harness"]').click()
    expect(plan.locator('[data-testid="rollback-hint"]')).to_be_visible(timeout=T)
    page.emulate_media(color_scheme="dark")
    settle(page)
    page.screenshot(path=str(review / "update-done-dark.png"))


def test_review_clocks(page_factory, review):
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    section(page, "clocks")
    card = page.locator('[data-testid="clock-card"]')
    card.locator('.seg button:has-text("100 MHz")').click()
    card.locator('[data-testid="arm-clock"] input').check()
    card.locator('[data-action="clock"]').click()
    expect(card.locator('[data-reading="dut"]')).to_contain_text("100 MHz", timeout=T)
    settle(page)
    page.screenshot(path=str(review / "clocks-light.png"))


def test_review_sd(page_factory, review, tmp_path):
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    section(page, "sd")
    flow = page.locator('[data-testid="sd-flow"]')
    flow.locator('[data-action="sd_backup"]').click()
    expect(flow.locator('[data-testid="sd-backup-result"]')).to_contain_text("rc 0", timeout=T)
    backup = tmp_path / "sd-backup-20260924.zip"
    with zipfile.ZipFile(backup, "w") as zf:
        zf.writestr("config.txt", "TITLE: V2M-MPS3\n")
    flow.locator('[data-testid="sd-dest-0"]').fill("MB/HBI0309C/AN536/images.txt")
    flow.locator('[data-testid="sd-src-0"]').fill("/home/me/harness-1.1.0/images.txt")
    settle(page)
    page.screenshot(path=str(review / "sd-light.png"))


def test_review_hub_lease(page_factory, daemon, review):
    daemon.app.state.sim.behind_hub(BOARD_USB, lease="mine", expires_in_s=1620)
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    settle(page)
    page.screenshot(path=str(review / "hub-lease-light.png"))


def test_review_hub_attention(page_factory, daemon, review):
    daemon.app.state.sim.behind_hub(BOARD_FIELDED, lease="other", tunnel="down",
                                    detail="the ssh process exited (255): Connection refused")
    page = page_factory("dark", **APP)
    open_board(page, BOARD_FIELDED)
    expect(page.locator('[data-attention="tunnel"]')).to_be_visible(timeout=T)
    settle(page)
    page.screenshot(path=str(review / "hub-attention-dark.png"))
