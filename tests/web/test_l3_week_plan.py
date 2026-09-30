"""Lane L3 (UI) in the browser: the simplified Overview, screen and baud on the consoles,
the Power, Update, Clocks and SD pages, and a board behind a hub.

These run over the T14 mock, which simulates the week-plan routes of lanes L1, L2 and L4:
tests/fakes/l3_week_plan.py. The ones without sim=True also run over the real daemon, now
that those modules are on main (HARNESS_MANAGER_WEB_WEEK_REAL=0 turns that off). Each
behaviour has its negative twin.
"""

from __future__ import annotations

import json
import re
import time
import zipfile

import pytest

from harness_manager.core.model import Health
from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 900}          # the `harness-manager app` window


def rail(page, board_id):
    return page.locator(f'.board-item[data-board="{board_id}"]')


def open_board(page, board_id):
    rail(page, board_id).click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)


def section(page, key):
    """0.1.0's tab ``key`` where UI v2 put it (tests/web/nav.py)."""
    nav.section(page, key)


def sim_of(daemon):
    return daemon.app.state.sim


def wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


# --- the Overview ----------------------------------------------------------------------------


@pytest.mark.week_plan("consoles_api")
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_overview_fits_the_app_window_without_the_identity_card(page_factory, scheme):
    # UI v2 round 3: one screen (Needs attention, the identity strip and readings, the cards
    # beside the Front panel); the Identity card is Board > About's, not a fold here
    page = page_factory(scheme, **APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="overview"]', timeout=T)
    for card in ("tile-design", "tile-consoles", "panel-card", "ov-readings"):
        box = page.locator(f'[data-testid="{card}"]').bounding_box()
        assert box and box["y"] + box["height"] <= APP["height"] + 1, (card, box)   # no scrolling
    assert page.locator('[data-testid="identity-card"]').count() == 0          # no duplication
    assert page.locator('[data-action="details"]').count() == 0                # no fold
    expect(page.locator('[data-testid="tile-design-name"]')).to_have_text("nanosoc")
    expect(page.locator('[data-testid="ov-kpi-temp"]')).to_contain_text("38.5", timeout=T)
    expect(page.locator('[data-testid="ov-kpi-clock"]')).to_contain_text("50")
    assert page.locator('[data-testid="attention"]').count() == 0              # all is well
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.week_plan()
def test_needs_attention_shows_only_what_is_wrong_with_its_fix(page_factory, engine):
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    # round 3: an unchecked build is the header's Design chip, not a row here; a busy harness is
    # the Health reading's (another client), not a problem to fix
    expect(page.locator('[data-testid="build-chip"]')).to_contain_text("Unchecked", timeout=T)
    engine._board(BOARD_FIELDED).health = Health(
        reachable=True, control_channel="busy", notes=("another client holds the control channel",))
    page.locator('[data-action="refresh-board"]').click()
    expect(page.locator('[data-testid="ov-kpi-health"]')).to_contain_text("Busy", timeout=T)
    assert page.locator('[data-testid="attention"]').count() == 0
    # the harness wedges: one row, with the note the engine gave and its one fix
    engine._board(BOARD_FIELDED).health = Health(
        reachable=True, control_channel="wedged", notes=("the control channel stopped answering",))
    page.locator('[data-action="refresh-board"]').click()
    strip = page.locator('[data-testid="attention"]')
    expect(strip.locator('[data-attention="harness"]')).to_contain_text("The harness is wedged", timeout=T)
    assert strip.locator("li").count() == 1                                      # only that
    strip.locator('[data-action="attention-harness"]').click()
    nav.panel(page, "power").wait_for(timeout=T)            # UI v2: Board > Recover


@pytest.mark.week_plan("consoles_api", sim=True)
def test_the_consoles_card_opens_a_console_on_the_workbench(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    card = page.locator('[data-testid="tile-consoles"]')
    row = card.locator('li[data-console="uart0"]')
    expect(row).to_contain_text("not open in this page", timeout=T)            # the twin first
    row.locator(".ov-con-name").click()
    page.wait_for_selector('[data-testid="console-uart0"]', timeout=T)
    assert "console=uart0" in page.evaluate("location.hash")


# --- consoles: baud and screen ------------------------------------------------------------------


@pytest.mark.week_plan("consoles_api", sim=True)
def test_a_design_fixed_baud_shows_its_rate_and_reason_and_no_selector(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    baud = page.locator('[data-testid="console-uart0"] [data-testid="baud"]')
    expect(baud).to_have_attribute("data-settable", "no")
    expect(baud).to_contain_text("76800")
    expect(baud).to_contain_text("fixed by the nanosoc design (needs harness 'uart_baud')")
    assert "rp_nanosoc_wrapper.sv:54" in baud.get_attribute("title")        # the cite, on hover
    assert baud.locator("select").count() == 0


@pytest.mark.week_plan("consoles_api", sim=True)
def test_swo_is_fixed_by_the_harness_and_uart1_has_no_rate(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.locator('[data-console-tab="swo"]').click()
    swo = page.locator('[data-testid="console-swo"] [data-testid="baud"]')
    expect(swo).to_contain_text("2000000")
    expect(swo).to_contain_text("fixed by the harness firmware")
    page.locator('[data-console-tab="uart1"]').click()
    uart1 = page.locator('[data-testid="console-uart1"] [data-testid="baud"]')
    expect(uart1).to_contain_text("no rate")                                  # never a 0
    expect(uart1).to_contain_text("nothing drives uart1")


@pytest.mark.week_plan("consoles_api", sim=True)
def test_a_serial_console_baud_is_set_from_the_gui_and_confirmed(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.locator('[data-console-tab="mcc"]').click()
    baud = page.locator('[data-testid="console-mcc"] [data-testid="baud"]')
    expect(baud).to_have_attribute("data-settable", "yes")
    baud.locator("select").select_option("9600")
    result = page.locator('[data-testid="console-mcc"] [data-testid="console-result"]')
    expect(result).to_contain_text("$ console mcc baud 9600  (rc 0")
    expect(result).to_contain_text("now 9600 baud")
    assert sim_of(daemon).bauds[(BOARD_USB, "mcc")] == 9600
    expect(baud.locator("select")).to_have_value("9600")


@pytest.mark.week_plan("consoles_api", "hub_api", sim=True)
def test_a_hub_share_fixes_the_serial_baud_and_says_so(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.locator('[data-console-tab="mcc"]').click()
    baud = page.locator('[data-testid="console-mcc"] [data-testid="baud"]')
    expect(baud).to_contain_text("115200")
    expect(baud).to_contain_text("set by the hub share")


@pytest.mark.week_plan("consoles_api", sim=True)
def test_attach_with_screen_shows_the_command_and_the_client_count(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    pane = page.locator('[data-testid="console-uart0"]')
    pane.locator('[data-action="attach-screen"]').click()
    expect(pane.locator('[data-testid="screen-command"] code')).to_contain_text("screen …/")
    expect(pane.locator('[data-testid="console-result"]')).to_contain_text("$ console uart0 pty  (rc 0")
    sim_of(daemon).attach_screen(BOARD_USB, "uart0", 2)
    expect(pane.locator('[data-testid="screen-clients"]')).to_have_text("2 attached")
    # No CLI line (Q1: the old check looked for a data-testid the page no longer has, so
    # it could not fail; the removed row read "harness-manager console <address> <name>").
    assert "harness-manager console" not in pane.inner_text()


@pytest.mark.week_plan("consoles_api", sim=True)
def test_a_serial_consoles_screen_command_carries_its_rate_verbatim(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.locator('[data-console-tab="mcc"]').click()
    pane = page.locator('[data-testid="console-mcc"]')
    pane.locator('[data-action="attach-screen"]').click()
    code = pane.locator('[data-testid="screen-command"] code')
    expect(code).to_contain_text("/mcc 115200")                   # screen would set 9600 without it
    assert code.get_attribute("title").endswith("/mcc 115200")
    expect(pane.locator('[data-testid="screen-exclusive"]')).to_contain_text("one terminal at a time")
    # screen leaves, then the PTY closes: the command goes, the attach button is back.
    sim_of(daemon).attach_screen(BOARD_USB, "mcc", 0)
    expect(pane.locator('[data-testid="screen-clients"]')).to_have_text("0 attached")
    assert sim_of(daemon).close_pty(BOARD_USB, "mcc")
    expect(pane.locator('[data-action="attach-screen"]')).to_be_visible(timeout=T)


@pytest.mark.week_plan("consoles_api")
def test_attach_with_screen_makes_a_pty_on_the_daemon(page_factory, daemon, tmp_path, monkeypatch):
    # Real-daemon capable: L2's fallback gives the demo boards real PTYs (under tmp_path here).
    from tests.fakes.l2_rig import pty_dir  # termios: never import it on Windows

    monkeypatch.setenv("HARNESS_MANAGER_PTY_DIR", str(pty_dir(tmp_path)))
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    pane = page.locator('[data-testid="console-uart0"]')
    pane.locator('[data-action="attach-screen"]').click()
    code = pane.locator('[data-testid="screen-command"] code')
    expect(code).to_contain_text("/uart0", timeout=T)
    assert code.get_attribute("title").startswith("screen /")
    expect(pane.locator('[data-testid="screen-exclusive"]')).to_be_visible()
    expect(pane.locator('[data-testid="console-result"]')).to_contain_text("$ console uart0 pty  (rc 0")


@pytest.mark.week_plan("consoles_api", sim=True)
def test_without_ptys_screen_says_why_and_offers_the_tcp_export(page_factory, daemon):
    sim_of(daemon).pty_unavailable = "PTYs need a POSIX host (this is Windows): export the console over TCP"
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    pane = page.locator('[data-testid="console-uart0"]')
    pane.locator('[data-action="attach-screen"]').click()
    unavailable = pane.locator('[data-testid="screen-unavailable"]')
    expect(unavailable).to_contain_text("this is Windows")
    unavailable.locator("button:has-text('Export to TCP instead')").click()
    expect(pane).to_contain_text("telnet 127.0.0.1 ")


# --- power --------------------------------------------------------------------------------------


@pytest.mark.week_plan("power_api", sim=True)
def test_power_page_reads_the_supply_and_cycles_it_once_armed(page_factory, daemon):
    sim_of(daemon).set_power(BOARD_USB)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    expect(card.locator('[data-reading="board_power"]')).to_contain_text("11.4 W")
    expect(card.locator('[data-reading="supply_current"]')).to_contain_text("0.071 A")
    expect(card.locator('[data-testid="reason-power_cycle"]')).to_contain_text("not armed")
    card.locator('[data-testid="arm-power"] input').check()
    card.locator('[data-action="power_cycle"]').click()
    expect(card.locator('[data-testid="power-result"]')).to_contain_text("rc 0", timeout=T)
    result = card.locator('[data-testid="power-result"]')
    expect(result).to_contain_text("off 5 s, OFF confirmed, ON confirmed")
    expect(result).to_contain_text("outlet on")                 # the phases: off, on; never "up"
    expect(result).not_to_contain_text("outlet up")


@pytest.mark.week_plan("power_api", sim=True)
def test_a_meter_that_cannot_cycle_shows_the_reason_and_no_button(page_factory, daemon):
    sim_of(daemon).set_power(BOARD_USB, device="INA260 on the 12 V input",
                             cycle_reason="the INA260 only measures: it cannot switch the supply")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    expect(card.locator('[data-testid="power-cycle-reason"]')).to_contain_text("only measures")
    assert card.locator('[data-action="power_cycle"]').count() == 0


@pytest.mark.week_plan("power_api")
def test_a_board_with_no_meter_says_why_once_and_offers_no_cycle(page_factory, daemon):
    # Real-daemon capable: a DemoEngine board has no power adapter, as on the lab MPS3 today.
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    rows = card.locator('[data-testid="power-readings"] tr')
    expect(rows).to_have_count(3, timeout=T)
    for name in ("board_power", "supply_voltage", "supply_current"):
        expect(card.locator(f'[data-reading="{name}"]')).to_contain_text("unavailable")   # no 0
    expect(card.locator('[data-testid="power-readings-reason"]')).to_be_visible()
    expect(card.locator('[data-testid="power-cycle-reason"]')).to_contain_text("Power cycle: cannot")
    assert card.locator('[data-action="power_cycle"]').count() == 0


@pytest.mark.week_plan("power_api", sim=True)
def test_an_off_time_out_of_range_is_interlocked_and_nothing_runs(page_factory, daemon):
    sim_of(daemon).set_power(BOARD_USB)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    card.locator('[data-testid="power-off-s"]').fill("1")
    card.locator('[data-testid="arm-power"] input').check()
    expect(card.locator('[data-testid="reason-power_cycle"]')).to_contain_text("2 to 300 seconds")
    card.locator('[data-action="power_cycle"]').click(force=True)
    expect(card.locator('[data-testid="power-result"]')).to_contain_text("Nothing was run.")
    card.locator('[data-testid="power-off-s"]').fill("3")
    card.locator('[data-action="power_cycle"]').click()
    expect(card.locator('[data-testid="power-result"]')).to_contain_text("off 3 s, OFF confirmed", timeout=T)


# --- update --------------------------------------------------------------------------------------


@pytest.mark.week_plan("update_api", sim=True)
def test_update_check_plan_rekey_consent_install_and_rollback_hint(page_factory, engine):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "update")
    page.locator('[data-action="update_check"]').click()
    plan = page.locator('[data-testid="update-plan"]')
    expect(plan.locator('[data-testid="rekey-chip"]')).to_be_visible(timeout=T)
    expect(plan.locator('[data-testid="update-steps"]')).to_contain_text("backup-sd")
    plan.locator('[data-testid="arm-update"] input').check()
    reason = plan.locator('[data-testid="reason-update_harness"]')
    expect(reason).to_contain_text("type exactly REKEY 0x72bb0a36")
    plan.locator('[data-testid="rekey-phrase"]').fill("rekey 0x72bb0a36")         # not exact
    plan.locator('[data-action="update_harness"]').click(force=True)
    expect(plan.locator('[data-testid="update-result"]')).to_contain_text("Nothing was run.")
    assert engine.info(BOARD_USB).identity.shell_id == "0x3f1a560f"
    plan.locator('[data-testid="rekey-phrase"]').fill("REKEY 0x72bb0a36")
    plan.locator('[data-action="update_harness"]').click()
    result = plan.locator('[data-testid="update-result"]')
    expect(result).to_contain_text("installed: harness 1.1.0 is running", timeout=T)
    expect(result).to_contain_text("usercode: UNCHECKED")           # never shown as ok
    expect(plan.locator('[data-testid="rollback-hint"]')).to_contain_text("restores the backup")
    expect(page.locator('[data-testid="fact-shell"]')).to_contain_text("0x72BB0A36", timeout=T)


@pytest.mark.week_plan("update_api", sim=True)
def test_an_update_the_board_cannot_take_lists_its_blockers_and_is_refused(page_factory, engine):
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)                      # no Debug USB: no SD, no reboot
    section(page, "update")
    page.locator('[data-action="update_check"]').click()
    plan = page.locator('[data-testid="update-plan"]')
    expect(plan.locator('[data-testid="update-blocker"]').first).to_contain_text("Debug USB", timeout=T)
    plan.locator('[data-testid="arm-update"] input').check()
    plan.locator('[data-testid="rekey-phrase"]').fill("REKEY 0x72bb0a36")
    expect(plan.locator('[data-testid="reason-update_harness"]')).to_contain_text("blocked:")
    assert engine.info(BOARD_FIELDED).identity.shell_id == "0x3f1a560f"


@pytest.mark.week_plan("update_api")
def test_an_engine_without_an_update_service_says_so_cleanly(page_factory, daemon):
    # Real-daemon capable: DemoEngine has no update service (422 UNAVAILABLE).
    sim = getattr(daemon.app.state, "sim", None)
    if sim is not None:
        sim.update_reason = "this engine has no update service"
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "update")
    page.locator('[data-action="update_check"]').click()
    expect(page.locator('[data-testid="update-unavailable"]')).to_contain_text(
        "Updates are unavailable here: this engine has no update service", timeout=T)
    assert page.locator('[data-testid="update-plan"]').count() == 0


@pytest.mark.week_plan("update_api", sim=True)
def test_a_plan_that_changed_since_the_check_is_shown_again_to_approve(page_factory, daemon, engine):
    sim = sim_of(daemon)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "update")
    page.locator('[data-action="update_check"]').click()
    plan = page.locator('[data-testid="update-plan"]')
    expect(plan).to_contain_text("release #14", timeout=T)
    sim.bump_channel()                                   # a new release lands after the check
    plan.locator('[data-testid="rekey-phrase"]').fill("REKEY 0x72bb0a36")
    plan.locator('[data-testid="arm-update"] input').check()
    plan.locator('[data-action="update_harness"]').click()
    result = plan.locator('[data-testid="update-result"]')
    expect(result).to_contain_text("changed since it was checked; nothing was installed", timeout=T)
    expect(plan.locator('[data-testid="plan-changed"]')).to_be_visible()
    expect(plan).to_contain_text("release #15")
    assert engine.info(BOARD_USB).identity.shell_id == "0x3f1a560f"
    expect(plan.locator('[data-testid="rekey-phrase"]')).to_have_value("")    # consent again
    plan.locator('[data-testid="rekey-phrase"]').fill("REKEY 0x72bb0a36")
    plan.locator('[data-testid="arm-update"] input').check()
    plan.locator('[data-action="update_harness"]').click()
    expect(result).to_contain_text("installed: harness 1.1.0 is running", timeout=T)


@pytest.mark.week_plan("update_api", sim=True)
def test_an_install_the_board_does_not_run_fails_with_its_outcome_and_the_rollback(page_factory, daemon, engine):
    sim = sim_of(daemon)
    sim.update_outcome = "written-not-running"
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "update")
    page.locator('[data-action="update_check"]').click()
    plan = page.locator('[data-testid="update-plan"]')
    plan.locator('[data-testid="rekey-phrase"]').fill("REKEY 0x72bb0a36", timeout=T)
    plan.locator('[data-testid="arm-update"] input').check()
    plan.locator('[data-action="update_harness"]').click()
    result = plan.locator('[data-testid="update-result"]')
    expect(result).to_contain_text("ACTION_FAILED", timeout=T)
    expect(result).to_contain_text("written-not-running: the SD holds the new base")
    expect(plan.locator('[data-testid="rollback-hint"]')).to_contain_text("restores the backup")
    assert plan.locator('[data-testid="plan-applied"]').count() == 0
    plan.locator('[data-testid="arm-rollback"] input').check()
    plan.locator('[data-action="update_rollback"]').click()
    expect(result).to_contain_text("restored: restored the backup", timeout=T)


@pytest.mark.week_plan("update_api", sim=True)
def test_the_app_update_is_not_switched_from_a_boards_page(page_factory, daemon):
    # UPDATE-UI (OTA-U) moved the app's own update out of the board's page: it is staged in
    # the background and applied with a restart from the banner and Settings, never switched
    # in place here (test_updui_browser.py covers the banner and Settings).
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "update")
    card = page.locator('[data-testid="update-app"]')
    expect(card).to_contain_text("You run Harness Manager 0.0.1", timeout=T)
    assert card.locator('[data-action="update_app"]').count() == 0
    expect(card.locator('[data-action="open-settings"]')).to_be_visible()
    assert sim_of(daemon).app_version == "0.0.1"


# --- clocks ----------------------------------------------------------------------------------------


@pytest.mark.week_plan(sim=True)
def test_clocks_set_a_dut_preset_and_read_the_oscillators(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "clocks")
    card = page.locator('[data-testid="clock-card"]')
    expect(card.locator('[data-reading="dut"]')).to_contain_text("set it to know it")    # never 0
    card.locator('.seg button:has-text("100 MHz")').click()
    card.locator('[data-testid="arm-clock"] input').check()
    card.locator('[data-action="clock"]').click()
    expect(card.locator('[data-testid="clock-result"]')).to_contain_text("$ clock dut 100  (rc 0")
    expect(card.locator('[data-reading="dut"]')).to_contain_text("100 MHz")
    expect(page.locator('[data-testid="osc-card"] [data-reading="osc0"]')).to_contain_text("50 MHz")


@pytest.mark.week_plan()
def test_without_the_debug_usb_the_oscillators_say_why(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    section(page, "clocks")
    expect(page.locator('[data-testid="osc-reason"]')).to_contain_text("needs the Debug USB cable")


# --- SD install flow ---------------------------------------------------------------------------------


@pytest.mark.week_plan()
def test_sd_flow_backup_then_install_needs_files_and_a_backup(page_factory, engine, tmp_path):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "sd")
    flow = page.locator('[data-testid="sd-flow"]')
    flow.locator('[data-action="sd_backup"]').click()
    expect(flow.locator('[data-testid="sd-backup-result"]')).to_contain_text("rc 0", timeout=T)
    # The backup's path (the mock's "demo-backup.zip", the daemon's state dir) fills the field.
    expect(flow.locator('[data-testid="sd-backup-path"]')).to_have_value(re.compile(r"demo-backup\.zip$"))
    flow.locator('[data-testid="arm-sd-install"] input').check()
    expect(flow.locator('[data-testid="reason-sd_install"]')).to_contain_text("add at least one file")
    flow.locator('[data-testid="sd-dest-0"]').fill("MB/HBI0309C/HARNESS.ebf")
    flow.locator('[data-testid="sd-src-0"]').fill(str(tmp_path / "x.ebf"))
    expect(flow.locator('[data-testid="reason-sd_install"]')).to_contain_text("never written")
    backup = tmp_path / "b.zip"
    with zipfile.ZipFile(backup, "w") as zf:
        zf.writestr("config.txt", "TITLE: V2M-MPS3\n")
    image = tmp_path / "images.txt"
    image.write_text("TITLE: nanosoc\n")
    flow.locator('[data-testid="sd-dest-0"]').fill("MB/HBI0309C/AN536/images.txt")
    flow.locator('[data-testid="sd-src-0"]').fill(str(image))
    flow.locator('[data-testid="sd-backup-path"]').fill(str(backup))
    flow.locator('[data-action="sd_install"]').click()
    # The demo engine never writes an SD: the job fails with its REFUSED, shown as such.
    expect(flow.locator('[data-testid="sd-install-result"]')).to_contain_text("REFUSED", timeout=T)
    assert engine.called("storage.install")
    flow.locator('[data-testid="sd-step-restore"] input[type="checkbox"]').check()
    flow.locator('[data-action="sd_restore_manual"]').click()
    expect(flow.locator('[data-testid="sd-restore-result"]')).to_contain_text("rc 0", timeout=T)


# --- a board behind a hub -------------------------------------------------------------------------------


@pytest.mark.week_plan("hub_api", sim=True)
def test_a_board_behind_a_hub_shows_its_tunnel_and_lease_and_releases_it(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB, lease="mine", expires_in_s=1620)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_text("tunnel up")
    # fpgahub's ISO expiry: 1620 s from now reads as 27 min, never NaN.
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text(re.compile(r"lease yours · 2[67] min"))
    assert page.locator('[data-testid="attention"]').count() == 0
    # LEASE-UI: Release asks first (tests/web/test_lease_ui_browser.py has the rest)
    page.locator('[data-testid="fact-hub"] [data-action="lease_release_open"]').click()
    page.locator('[data-testid="release-confirm"] [data-action="release_confirm"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_have_text("no lease", timeout=T)
    expect(page.locator('[data-testid="tile-lease"]')).to_have_attribute("data-lease", "free")   # the Lease card
    page.locator('[data-testid="fact-hub"] [data-action="lease_acquire"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    expect(page.locator('[data-testid="tile-lease"]')).to_have_attribute("data-lease", "here")


@pytest.mark.week_plan("hub_api", sim=True)
def test_someone_elses_lease_and_a_dead_tunnel_need_attention(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB, lease="other", tunnel="down",
                              detail="the ssh process exited (255): Connection refused")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to alice@lab-pc-07")
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_attribute("data-level", "err")
    expect(page.locator('[data-attention="tunnel"]')).to_contain_text("ssh mapstone-dev")
    expect(page.locator('[data-testid="tile-lease"]')).to_have_attribute("data-lease", "other")   # the Lease card


def queue_for_it(page):
    """Someone else holds the lease: "Request board" (lane LR-D, docs/LEASE_REQUESTS.md; it
    replaced "Queue for it") queues this client behind them as a lease_request job."""
    page.locator('[data-testid="fact-hub"] [data-action="lease_request_open"]').click()
    page.locator('[data-testid="lease-request-form"] [data-action="lease_request"]').click()
    expect(page.locator('[data-testid="lease-requested"]')).to_be_visible(timeout=T)


LEAVE_QUEUE = '[data-testid="lease-request"] [data-action="lease_leave"]'


@pytest.mark.week_plan("hub_api", sim=True)
def test_a_queued_lease_holds_the_board_and_can_be_cancelled(page_factory, daemon, engine):
    sim_of(daemon).behind_hub(BOARD_USB, lease="other")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    queue_for_it(page)
    # While it queues the board is held: a DUT reset waits and says why (Board > Recover).
    section(page, "power")
    tile = page.locator('[data-testid="board-page-recover"]')
    expect(tile.locator('[data-testid="reason-reset_dut"]')).to_contain_text("waiting for the hub lease")
    page.locator(LEAVE_QUEUE).click()
    expect(page.locator('[data-testid="lease-requested"]')).to_have_count(0, timeout=T)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to alice@lab-pc-07")
    # the job no longer holds the board; the lease rule (FIX-PACK-4: the holder only) does
    expect(tile.locator('[data-testid="reason-reset_dut"]')).to_contain_text(
        "Reset DUT is for the lease holder only: alice@lab-pc-07")
    assert not engine.called("resets.reset")


BOARDS_LIST = re.compile(r"/api/v1/boards(\?[^/]*)?$")


def page_state(page):
    return page.evaluate("window.__harness_managerState()")


@pytest.mark.week_plan("hub_api", sim=True)
def test_a_boards_list_asked_before_a_job_started_does_not_end_it(page_factory, daemon):
    # Q1 2026-09-24: the queued-lease test above failed 2 runs in 30 with the lease queued
    # and the reset saying "not armed". The GET /boards that follows session.opened was
    # asked before the lease job started and answered after it; the page read its "no job"
    # as a job.done it had missed, ended the live job, and never let it back.
    sim_of(daemon).behind_hub(BOARD_USB, lease="other")
    page = page_factory(**APP)
    rail(page, BOARD_USB).wait_for(timeout=T)            # the first list is in
    stale = []

    def hold(route):
        if route.request.method != "GET" or stale:
            route.continue_()
            return
        response = route.fetch()                          # answered now, before the job
        row = next((r for r in response.json()["boards"] if r["board_id"] == BOARD_USB), {})
        if row.get("open") and not row.get("job"):
            stale.append((route, response))               # held: it lands after the job
        else:
            route.fulfill(response=response)              # a list from before the open

    page.route(BOARDS_LIST, hold)
    open_board(page, BOARD_USB)                           # session.opened: the list again
    deadline = time.monotonic() + 10
    while not stale and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert stale, "the page did not ask for the boards list after the board opened"
    queue_for_it(page)
    section(page, "power")
    tile = page.locator('[data-testid="board-page-recover"]')
    reason = tile.locator('[data-testid="reason-reset_dut"]').first
    expect(reason).to_contain_text("waiting for the hub lease", timeout=T)
    route, response = stale[0]
    body = response.json()
    rows = [r for r in body["boards"] if r["board_id"] == BOARD_USB]
    assert rows and rows[0].get("open") and not rows[0].get("job")   # really the stale list
    rows[0]["q1_stale"] = True                            # a marker: this list has landed
    route.fulfill(response=response, json=body)
    page.unroute(BOARDS_LIST)
    page.wait_for_function(
        f"() => (window.__harness_managerState().boards[{BOARD_USB!r}] || {{}}).q1_stale",
        timeout=T)
    assert page_state(page)["jobs"][BOARD_USB] is not None
    expect(reason).to_contain_text("waiting for the hub lease")


@pytest.mark.week_plan("hub_api", sim=True)
def test_negative_twin_a_newer_list_ends_a_job_whose_end_the_page_missed(page_factory, daemon):
    # The recovery the guard above must keep: the job ends while the page hears nothing
    # (job.done dropped, GET /jobs/{id} unanswered); the next list ends it.
    sim_of(daemon).behind_hub(BOARD_USB, lease="other")
    page = page_factory(**APP)
    missed = {"on": False, "quiet": True, "dropped": []}
    sockets = []

    def relay(ws):
        server = ws.connect_to_server()

        def from_server(message):
            try:
                topic = json.loads(message).get("topic", "")
            except (TypeError, ValueError):
                topic = ""
            if missed["on"] and topic in ("job.done", "job.failed"):
                missed["dropped"].append(topic)
                return
            ws.send(message)

        server.on_message(from_server)
        sockets.append(ws)

    page.route_web_socket(re.compile(r"/api/v1/events"), relay)
    page.route(re.compile(r"/api/v1/jobs/"),
               lambda route: route.abort() if missed["on"] else route.continue_())
    # Nor a list until the test sends events.dropped: the page re-reads the list every 15 s
    # (and on a socket reconnect), and on a loaded machine that list landed between the
    # cancel and the check below and ended the job first (FLAKE 2026-09-24).
    page.route(BOARDS_LIST, lambda route: route.abort() if missed["on"] and missed["quiet"]
               and route.request.method == "GET" else route.continue_())
    page.reload()
    open_board(page, BOARD_USB)
    assert sockets, "the events socket did not go through the relay"
    queue_for_it(page)
    section(page, "power")
    reason = page.locator('[data-testid="board-page-recover"] [data-testid="reason-reset_dut"]').first
    expect(reason).to_contain_text("waiting for the hub lease", timeout=T)
    missed["on"] = True
    page.locator(LEAVE_QUEUE).click()
    deadline = time.monotonic() + 10
    while not missed["dropped"] and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert missed["dropped"], "the job's end never came, so there was nothing to miss"
    expect(reason).to_contain_text("waiting for the hub lease")    # the page missed it
    missed["quiet"] = False
    sockets[-1].send(json.dumps({"topic": "events.dropped", "board_id": "",
                                 "data": {"dropped": 1}, "at": time.time()}))
    # the list ended it: the lease rule (FIX-PACK-4, alice holds it) is what stops a reset now
    expect(reason).to_contain_text("Reset DUT is for the lease holder only", timeout=T)
    assert page_state(page)["jobs"][BOARD_USB] is None


DROPPED = json.dumps({"topic": "events.dropped", "board_id": "", "data": {"dropped": 1}})


def relay_events(page, drop=lambda topic: False):
    """The page's event socket through a relay: ``drop(topic)`` hides an event from it.
    Returns the relayed sockets (the last one is the page's live socket)."""
    sockets = []

    def relay(ws):
        server = ws.connect_to_server()

        def from_server(message):
            try:
                topic = json.loads(message).get("topic", "")
            except (TypeError, ValueError):
                topic = ""
            if not drop(topic):
                ws.send(message)

        server.on_message(from_server)
        sockets.append(ws)

    page.route_web_socket(re.compile(r"/api/v1/events"), relay)
    return sockets


@pytest.mark.week_plan()
def test_a_boards_list_asked_before_the_open_does_not_close_the_board(page_factory, daemon):
    # FLAKE 2026-09-24: the list-before-a-job test above timed out on CI waiting for the
    # Shell fact. A list in flight when Open was clicked (the start-up read, say) was
    # answered after the open: its "open: false" put the board back to its preview, and
    # the list session.opened asks for (the one that test holds) was all that would have
    # brought it back.
    page = page_factory(**APP)
    sockets = relay_events(page)
    page.reload()
    rail(page, BOARD_USB).wait_for(timeout=T)
    held, released = [], []

    def hold(route):
        if route.request.method != "GET" or released:
            route.continue_()
        elif not held:
            held.append((route, route.fetch()))         # answered now, before the open
        else:
            held.append((route, None))                  # newer lists wait until it landed

    page.route(BOARDS_LIST, hold)
    sockets[-1].send(DROPPED)                           # the page asks for the list now
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held, "the page did not ask for the boards list"
    route, response = held[0]
    body = response.json()
    rows = [r for r in body["boards"] if r["board_id"] == BOARD_USB]
    assert rows and not rows[0].get("open")             # really a list from before the open
    open_board(page, BOARD_USB)
    rows[0]["flake_stale"] = True                       # a marker: this list has landed
    route.fulfill(response=response, json=body)
    page.wait_for_function(
        f"() => (window.__harness_managerState().boards[{BOARD_USB!r}] || {{}}).flake_stale",
        timeout=T)
    assert page_state(page)["boards"][BOARD_USB]["open"]
    expect(page.locator('[data-testid="fact-shell"]')).to_contain_text(re.compile(r"0x[0-9A-F]{8}"))
    released.append(True)
    for later, _ in held[1:]:
        later.continue_()
    page.unroute(BOARDS_LIST)


@pytest.mark.week_plan()
def test_negative_twin_a_list_asked_after_the_board_closed_elsewhere_closes_it(
        page_factory, daemon, engine):
    # The guard above keeps only what this page did after the list was asked: a board the
    # CLI closed (the page heard nothing of it) is closed by the next list.
    page = page_factory(**APP)
    sockets = relay_events(page, drop=lambda topic: topic.startswith("session."))
    page.reload()
    open_board(page, BOARD_USB)
    info = re.compile(r"/api/v1/boards/[^/?]+$")        # only the list may tell the page
    page.route(info, lambda route: route.abort() if route.request.method == "GET"
               else route.continue_())
    engine.close(BOARD_USB)
    sockets[-1].send(DROPPED)
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    assert not page_state(page)["boards"][BOARD_USB]["open"]


@pytest.mark.week_plan("hub_api", sim=True)
def test_a_tunnel_that_drops_and_comes_back_is_followed_live(page_factory, daemon):
    sim = sim_of(daemon)
    sim.behind_hub(BOARD_USB, lease="mine")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_text("tunnel up")
    sim.set_tunnel(BOARD_USB, "starting", "ssh exited (255); restarting in 2 s")
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_text("tunnel starting", timeout=T)
    expect(page.locator('[data-attention="tunnel"]')).to_contain_text("restarting in 2 s")
    assert "restarted 1 time(s)" in page.locator('[data-testid="tunnel-chip"]').get_attribute("title")
    sim.set_tunnel(BOARD_USB, "up")
    expect(page.locator('[data-attention="tunnel"]')).to_have_count(0, timeout=T)


@pytest.mark.week_plan("hub_api", sim=True)
def test_the_sd_page_over_a_hub_says_why_the_sd_is_out_of_reach(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_FIELDED, lease="mine")         # Ethernet only: no USB_MSD
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    section(page, "sd")
    expect(page.locator('[data-testid="sd-unavailable"]')).to_contain_text("out of reach")
    expect(page.locator('[data-testid="sd-flow"] [data-testid="reason-sd_backup"]')).to_contain_text("Cannot:")


@pytest.mark.week_plan("hub_api", sim=True)
def test_a_board_added_through_a_hub_opens_with_its_tunnel_and_lease(page_factory, daemon):
    page = page_factory(**APP)
    page.locator('[aria-label="Add a board by address"]').click()
    page.locator('[aria-label="Board address"]').fill("192.168.10.102")
    page.locator('[data-testid="add-via"]').fill("mapstone-dev")
    page.locator('.rail-add button[type="submit"]').click()
    expect(page.locator(".rail-status")).to_contain_text("--via ssh:mapstone-dev", timeout=T)
    page.locator('[data-action="open"]').click()               # the candidate carries its route
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_text("tunnel up", timeout=T)
    expect(page.locator('[data-testid="lease-chip"]')).to_have_text("no lease")


PROBE = re.compile(r"/api/v1/probe$")


def add_by_address(page, address, via=""):
    page.locator('[aria-label="Add a board by address"]').click()
    page.locator('[aria-label="Board address"]').fill(address)
    page.locator('[data-testid="add-via"]').fill(via)
    page.locator('.rail-add button[type="submit"]').click()


@pytest.mark.week_plan("hub_api", sim=True)
def test_an_address_added_while_the_page_loads_is_not_followed_by_a_scan(page_factory, daemon):
    # FLAKE 2026-09-24 (CI run 35996099505): the test above added an address before the
    # page's first list was in. That list was empty, so the page chained its own first
    # scan behind the add, and the scan's "3 boards" replaced the add's answer.
    page = page_factory(url="about:blank", **APP)
    bodies, health, adds = [], [], []

    def hold_health(route):
        if not bodies:
            health.append(route)                  # start() waits: the add goes first
        else:
            route.continue_()

    def record(route):
        bodies.append(route.request.post_data_json)
        if len(bodies) == 1:
            adds.append(route)                    # held until start() has read its list
        else:
            route.continue_()

    page.route(re.compile(r"/api/v1/health$"), hold_health)
    page.route(PROBE, record)
    page.goto(daemon.ui_url)
    add_by_address(page, "192.168.10.102", "mapstone-dev")
    deadline = time.monotonic() + 10
    while not (adds and health) and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert adds and health, "the add or the page's start-up read did not happen"
    with page.expect_response(lambda r: BOARDS_LIST.search(r.url) and r.request.method == "GET",
                              timeout=T):
        for route in health:
            route.continue_()                     # start(): health, packs, the list
    page.evaluate("() => new Promise((done) => setTimeout(done, 100))")
    adds[0].continue_()
    status = page.locator(".rail-status")
    expect(status).to_contain_text("--via ssh:mapstone-dev", timeout=T)
    add_by_address(page, "192.168.10.101")        # anything chained runs before this one
    expect(status).to_contain_text("probe 192.168.10.101", timeout=T)
    assert [b.get("hosts") for b in bodies] == [["192.168.10.102"], ["192.168.10.101"]]


@pytest.mark.week_plan()
def test_negative_twin_a_page_that_opens_with_no_boards_scans_by_itself(page_factory, daemon):
    bodies = []
    page = page_factory(url="about:blank", **APP)
    page.route(PROBE, lambda route: (bodies.append(route.request.post_data_json),
                                     route.continue_()))
    page.goto(daemon.ui_url)
    expect(page.locator(".rail-status")).to_contain_text("board", timeout=T)
    assert bodies == [{}]


@pytest.mark.week_plan()
def test_a_board_not_behind_a_hub_shows_no_hub(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="overview"]', timeout=T)
    assert page.locator('[data-testid="fact-hub"]').count() == 0
