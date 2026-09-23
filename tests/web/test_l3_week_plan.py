"""Lane L3 (UI) in the browser: the simplified Overview, screen and baud on the consoles,
the Power, Update, Clocks and SD pages, and a board behind a hub.

These run over the T14 mock, which simulates the frozen week-plan routes (lanes L1, L2
and L4 build the real ones in parallel): tests/fakes/l3_week_plan.py. With
HARNESS_MANAGER_WEB_WEEK_REAL=1 they also run over the real daemon once those modules
have landed. Each behaviour has its negative twin.
"""

from __future__ import annotations

import time
import zipfile

import pytest

from harness_manager.core.model import Health
from harness_manager.demo import BOARD_FIELDED, BOARD_USB

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
    page.locator(f'[data-section="{key}"]').click()
    page.wait_for_selector(f'[data-testid="section-{key}"]', timeout=T)


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
def test_overview_is_four_tiles_that_fit_the_app_window_without_the_identity_card(page_factory, scheme):
    page = page_factory(scheme, **APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="tiles"]', timeout=T)
    for tile in ("tile-design", "tile-consoles", "tile-debug", "tile-board"):
        box = page.locator(f'[data-testid="{tile}"]').bounding_box()
        assert box and box["y"] + box["height"] <= APP["height"], (tile, box)   # no scrolling
    assert page.locator('[data-testid="identity-card"]').count() == 0          # no duplication
    expect(page.locator('[data-testid="tile-design-name"]')).to_have_text("nanosoc")
    expect(page.locator('[data-testid="tile-temp"]')).to_contain_text("38.5")
    expect(page.locator('[data-testid="tile-clock"]')).to_contain_text("50 MHz")
    assert page.locator('[data-testid="attention"]').count() == 0              # all is well
    page.locator('[data-action="details"]').click()
    expect(page.locator('[data-testid="identity-card"]')).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.week_plan()
def test_needs_attention_shows_only_what_is_wrong_with_its_fix(page_factory, engine):
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    strip = page.locator('[data-testid="attention"]')
    expect(strip.locator('[data-attention="build"]')).to_contain_text("Build check unchecked")
    assert strip.locator("li").count() == 1                                      # only that
    # The harness goes busy: one more line, with the note the engine gave.
    engine._board(BOARD_FIELDED).health = Health(
        reachable=True, control_channel="busy", notes=("another client holds the control channel",))
    page.locator('[data-action="refresh-board"]').click()
    expect(strip.locator('[data-attention="harness"]')).to_contain_text("another client holds")
    strip.locator('[data-attention="build"] button:has-text("Update")').click()
    page.wait_for_selector('[data-testid="section-update"]', timeout=T)


@pytest.mark.week_plan("consoles_api")
def test_the_consoles_tile_attaches_screen_and_opens_a_console(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    tile = page.locator('[data-testid="tile-consoles"]')
    row = tile.locator('li[data-console="uart0"]')
    expect(row).to_contain_text("76800")
    row.locator('[data-action="attach-uart0"]').click()
    expect(row.locator("code")).to_contain_text("screen …/mps3_192.168.10.102_6900/uart0")
    assert row.locator("code").get_attribute("title").startswith("screen /tmp/harness-manager-")
    sim_of(daemon).attach_screen(BOARD_USB, "uart0", 1)
    expect(row).to_contain_text("1 attached")
    # the twin: a console nobody attached shows the button, not a path
    assert tile.locator('li[data-console="uart1"] code').count() == 0
    row.locator('[data-action="open-uart0"]').click()
    page.wait_for_selector('[data-testid="console-uart0"]', timeout=T)


@pytest.mark.week_plan()
def test_the_board_tile_reboot_is_armed_and_says_why_it_cannot(page_factory, engine):
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)                     # Ethernet only: no board controller
    tile = page.locator('[data-testid="tile-board"]')
    expect(tile.locator('[data-testid="reason-reboot"]')).to_contain_text("Cannot: needs the Debug USB cable")
    tile.locator('[data-action="reboot"]').click(force=True)
    expect(tile.locator('[data-testid="tile-board-result"]')).to_contain_text("Nothing was run.")
    assert engine.called("controller.reboot") == []


@pytest.mark.week_plan()
def test_the_board_tile_resets_the_dut_once_armed(page_factory, engine):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    tile = page.locator('[data-testid="tile-board"]')
    expect(tile.locator('[data-testid="reason-reset_dut"]')).to_contain_text("not armed")
    tile.locator('label.arm-inline input').first.check()
    tile.locator('[data-action="reset_dut"]').click()
    expect(tile.locator('[data-testid="tile-board-result"]')).to_contain_text("rc 0")
    assert engine.called("resets.reset") == [(BOARD_USB, "dut")]
    expect(tile.locator('label.arm-inline input').first).not_to_be_checked()   # disarmed after


# --- consoles: baud and screen ------------------------------------------------------------------


@pytest.mark.week_plan("consoles_api")
def test_a_design_fixed_baud_shows_its_rate_and_reason_and_no_selector(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    baud = page.locator('[data-testid="console-uart0"] [data-testid="baud"]')
    expect(baud).to_have_attribute("data-settable", "no")
    expect(baud).to_contain_text("76800")
    expect(baud).to_contain_text("fixed by the nanosoc design (needs harness 'uart_baud')")
    assert baud.locator("select").count() == 0


@pytest.mark.week_plan("consoles_api")
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


@pytest.mark.week_plan("consoles_api", "hub_api")
def test_a_hub_share_fixes_the_serial_baud_and_says_so(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.locator('[data-console-tab="mcc"]').click()
    baud = page.locator('[data-testid="console-mcc"] [data-testid="baud"]')
    expect(baud).to_contain_text("115200")
    expect(baud).to_contain_text("set by the hub share")


@pytest.mark.week_plan("consoles_api")
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
    assert page.locator('[data-testid="terminal-command"]').count() == 0     # no CLI line


@pytest.mark.week_plan("consoles_api")
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


@pytest.mark.week_plan("power_api")
def test_power_page_reads_the_supply_and_cycles_it_once_armed(page_factory, daemon):
    sim_of(daemon).set_power(BOARD_USB)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    expect(card.locator('[data-reading="board_power"]')).to_contain_text("23.4 W")
    expect(card.locator('[data-testid="reason-power_cycle"]')).to_contain_text("not armed")
    card.locator('[data-testid="arm-power"] input').check()
    card.locator('[data-action="power_cycle"]').click()
    expect(card.locator('[data-testid="power-result"]')).to_contain_text("rc 0", timeout=T)
    expect(card.locator('[data-testid="power-result"]')).to_contain_text("power-cycled through Shelly")


@pytest.mark.week_plan("power_api")
def test_a_meter_that_cannot_cycle_shows_the_reason_and_no_button(page_factory, daemon):
    sim_of(daemon).set_power(BOARD_USB, device="INA260 on the 12 V input",
                             cycle_reason="the INA260 only measures: it cannot switch the supply")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    section(page, "power")
    card = page.locator('[data-testid="power-card"]')
    expect(card.locator('[data-testid="power-cycle-reason"]')).to_contain_text("only measures")
    assert card.locator('[data-action="power_cycle"]').count() == 0


# --- update --------------------------------------------------------------------------------------


@pytest.mark.week_plan("update_api")
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
    expect(result).to_contain_text("installed: the board reports shell 0x72bb0a36", timeout=T)
    expect(plan.locator('[data-testid="rollback-hint"]')).to_contain_text("update rollback")
    expect(page.locator('[data-testid="fact-shell"]')).to_contain_text("0x72bb0a36", timeout=T)


@pytest.mark.week_plan("update_api")
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


# --- clocks ----------------------------------------------------------------------------------------


@pytest.mark.week_plan()
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
    expect(flow.locator('[data-testid="sd-backup-path"]')).to_have_value("demo-backup.zip")
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


@pytest.mark.week_plan("hub_api")
def test_a_board_behind_a_hub_shows_its_tunnel_and_lease_and_releases_it(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB, lease="mine", expires_in_s=1620)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_text("tunnel up")
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours")
    assert page.locator('[data-testid="attention"]').count() == 0
    page.locator('[data-testid="fact-hub"] [data-action="lease_release"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_have_text("no lease", timeout=T)
    expect(page.locator('[data-attention="lease"]')).to_contain_text("Not leased on mapstone-dev")
    page.locator('[data-attention="lease"] [data-action="lease_acquire"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    expect(page.locator('[data-attention="lease"]')).to_have_count(0)


@pytest.mark.week_plan("hub_api")
def test_someone_elses_lease_and_a_dead_tunnel_need_attention(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB, lease="other", tunnel="down",
                              detail="the ssh process exited (255): Connection refused")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to alice@lab-pc-07")
    expect(page.locator('[data-testid="tunnel-chip"]')).to_have_attribute("data-level", "err")
    expect(page.locator('[data-attention="tunnel"]')).to_contain_text("ssh mapstone-dev")
    expect(page.locator('[data-attention="lease"]')).to_contain_text("Leased to alice@lab-pc-07")


@pytest.mark.week_plan()
def test_a_board_not_behind_a_hub_shows_no_hub(page_factory):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="tiles"]', timeout=T)
    assert page.locator('[data-testid="fact-hub"]').count() == 0
