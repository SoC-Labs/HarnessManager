"""FIX-PACK-4 in the browser: the UI review's bugs in today's app (not the redesign).

A headless system Chrome over the real harness-manager-daemon on DemoEngine, or (the hub
tests, ``HUB``) the T14 mock with its week-plan sim (``behind_hub(bid, lease=...)``: mine |
elsewhere | other | none). Each behaviour has its negative twin. Nothing here reaches a hub or
a board.
"""

from __future__ import annotations

import getpass
import time
from typing import Any

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from tests.web import nav, wb

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 900}


# --- helpers ---------------------------------------------------------------------------------------


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def rail(page: Any, bid: str) -> Any:
    return page.locator(f'.board-item[data-board="{bid}"]')


def open_board(page: Any, bid: str = BOARD_USB) -> None:
    nav.open_board(page, bid)


def section(page: Any, key: str) -> None:
    """0.1.0's tab ``key`` where UI v2 put it (tests/web/nav.py)."""
    nav.section(page, key)


def wait_until(fn: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


# --- 5: the tabs never hide off-screen -------------------------------------------------------------

# The tabs a user cannot see: outside the tab row's box, or past the window's edge.
HIDDEN_TABS = """() => {
  const nav = document.querySelector('nav.sections');
  const n = nav.getBoundingClientRect();
  return [...nav.querySelectorAll('.section-tab')].filter((t) => {
    const r = t.getBoundingClientRect();
    return r.width === 0 || r.left < n.left - 1 || r.right > n.right + 1 || r.right > innerWidth;
  }).map((t) => t.dataset.section);
}"""


def test_every_tab_is_on_screen_at_1024_px(page_factory):
    # UI v2: five tabs; this USB board has no hub, so no Checks
    page = page_factory(width=1024, height=768)
    open_board(page)
    expect(page.locator(".section-tab")).to_have_count(4)
    assert page.evaluate(HIDDEN_TABS) == []
    for key in ("overview", "workbench", "build", "board"):
        nav.tab(page, key)
        expect(page.locator(f'.section-tab[data-section="{key}"]')).to_have_attribute("aria-selected", "true")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.errors == []


def test_negative_twin_a_scrolling_row_too_narrow_hides_tabs_and_the_check_sees_it(page_factory):
    page = page_factory(width=1024, height=768)
    open_board(page)
    page.add_style_tag(content="nav.sections { flex-wrap: nowrap !important; overflow-x: auto; width: 240px; }")
    hidden = page.evaluate(HIDDEN_TABS)
    assert "board" in hidden and "overview" not in hidden, hidden


# --- 8: the header's refresh re-reads the Card line and the SD journal ----------------------------


def give_card(engine: Any, card: str | None = "empty") -> None:
    feats = tuple(f for f in engine._board(BOARD_USB).identity.features if f != "usd")
    engine.set_features(BOARD_USB, (*feats, "usd"))
    engine.set_card(BOARD_USB, card)


def test_the_header_refresh_rereads_the_card_line_and_the_sd_journal(page_factory, engine, tmp_path):
    give_card(engine, "empty")
    page = page_factory(**APP)
    open_board(page)
    tile = by_id(page, "tile-card")
    expect(tile).to_have_text("empty", timeout=T)
    expect(by_id(page, "sd-banner")).to_have_count(0)
    # the world moves: the card is taken out, and an SD install is found interrupted
    engine.set_card(BOARD_USB, None)
    engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                      "backup": {"path": str(tmp_path / "b.zip")}})
    cards, journals = len(engine.called("deploy.card_status")), len(engine.called("storage.pending"))
    page.locator('[data-action="refresh-board"]').click()
    expect(by_id(page, "sd-banner")).to_be_visible(timeout=T)
    section(page, "overview")                    # a journal found first opens the SD tab
    expect(tile).to_have_text("none (boots as always)", timeout=T)
    assert len(engine.called("deploy.card_status")) > cards
    assert len(engine.called("storage.pending")) > journals
    assert page.errors == []


def test_negative_twin_without_a_card_store_the_refresh_reads_no_card(page_factory, engine):
    page = page_factory(**APP)
    open_board(page)
    expect(by_id(page, "tile-card")).to_have_text("no card store on this harness", timeout=T)
    infos, journals = len(engine.called("info")), len(engine.called("storage.pending"))
    cards = len(engine.called("deploy.card_status"))
    page.locator('[data-action="refresh-board"]').click()
    assert wait_until(lambda: len(engine.called("info")) > infos
                      and len(engine.called("storage.pending")) > journals)
    time.sleep(0.5)
    assert len(engine.called("deploy.card_status")) == cards         # nothing to read
    expect(by_id(page, "tile-card")).to_have_text("no card store on this harness")


# --- 9: Settings opens on General, then where you left it --------------------------------------------


def gear(page: Any) -> Any:
    page.locator('[data-action="settings"]').click()
    expect(by_id(page, "settings")).to_be_visible(timeout=T)
    return by_id(page, "settings-pane")


def test_settings_opens_on_general_then_on_the_last_section_used(page_factory):
    page = page_factory(**APP)
    page.wait_for_selector(".board-item", timeout=T)
    expect(gear(page)).to_have_attribute("data-settings-section", "general", timeout=T)
    page.locator('[data-testid="settings-nav"] [data-settings-section="tools"]').click()
    page.locator('[data-action="settings-close"]').click()
    expect(by_id(page, "settings")).to_have_count(0)
    expect(gear(page)).to_have_attribute("data-settings-section", "tools", timeout=T)
    assert page.errors == []


def test_negative_twin_the_update_tabs_settings_link_still_opens_updates(page_factory):
    page = page_factory(**APP)
    open_board(page)
    section(page, "update")
    by_id(page, "update-app").locator('[data-action="open-settings"]').click()
    expect(by_id(page, "settings-pane")).to_have_attribute("data-settings-section", "updates", timeout=T)
    expect(by_id(page, "update-settings")).to_be_visible(timeout=T)


# --- 1: one lease rule: held HERE, never `mine` -----------------------------------------------------

ME = f"{getpass.getuser()}@harness-manager"        # the mock's principal (this and other sessions)
HUB = pytest.mark.week_plan("hub_api", sim=True)


def sim(daemon: Any) -> Any:
    return daemon.app.state.sim


def xvc_page(page_factory: Any, daemon: Any, engine: Any, lease: str) -> Any:
    from tests.web.test_xvc_card_browser import xvc_board

    xvc_board(engine, BOARD_FIELDED)
    sim(daemon).behind_hub(BOARD_FIELDED, lease=lease)
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    section(page, "debug")
    page.wait_for_selector('[data-testid="xvc-card"]', timeout=T)
    return page


@HUB
def test_xvc_is_off_for_a_lease_your_other_session_holds(page_factory, daemon, engine):
    page = xvc_page(page_factory, daemon, engine, "elsewhere")
    why = f"XVC is for the lease holder only: {ME} holds this board in another session, not this Harness Manager"
    expect(by_id(page, "reason-xvc_open")).to_have_text(why, timeout=T)
    button = page.locator('[data-testid="xvc-card"] [data-action="xvc_open"]')
    expect(button).to_have_attribute("aria-disabled", "true")
    button.click(force=True)                          # an interlock: nothing is sent
    expect(by_id(page, "xvc-result")).to_contain_text("Nothing was run.")
    assert daemon.app.state.xvc.sessions == {}


@HUB
def test_negative_twin_xvc_opens_for_the_lease_held_here(page_factory, daemon, engine):
    page = xvc_page(page_factory, daemon, engine, "mine")
    button = page.locator('[data-testid="xvc-card"] [data-action="xvc_open"]')
    expect(button).not_to_have_attribute("aria-disabled", "true", timeout=T)
    expect(by_id(page, "reason-xvc_open")).to_have_count(0)


def harness_lease_page(page_factory: Any, daemon: Any, lease: str) -> tuple[Any, Any]:
    from tests.web.test_updui_browser import harness_page

    sim(daemon).behind_hub(BOARD_USB, lease=lease)
    return harness_page(page_factory)


@pytest.mark.week_plan("harness_api", sim=True)
def test_the_update_lease_line_is_not_yours_for_your_other_session(page_factory, daemon):
    from tests.web.test_updui_browser import install_rekey

    page, card = harness_lease_page(page_factory, daemon, "elsewhere")
    line = by_id(page, "harness-lease").first
    expect(line).to_contain_text(f"{ME} holds the lease on mps3_01_pl in another session, "
                                 "not this Harness Manager", timeout=T)
    expect(line).not_to_contain_text("You hold")
    panel = install_rekey(page, card)
    result = panel.locator('[data-testid="harness-result"]')
    expect(result).to_contain_text("HELD", timeout=T)                    # the daemon refuses too
    assert daemon.app.state.harness.running == {}


@pytest.mark.week_plan("harness_api", sim=True)
def test_negative_twin_the_update_lease_line_is_yours_when_held_here(page_factory, daemon):
    page, _card = harness_lease_page(page_factory, daemon, "mine")
    expect(by_id(page, "harness-lease").first).to_contain_text("You hold this board's hub lease",
                                                               timeout=T)


# --- 10: who may drive the board: the same rule on every tab ---------------------------------------


def hub_board_page(page_factory: Any, daemon: Any, lease: str) -> Any:
    # BOARD_USB: the demo board that can do everything (Debug USB: reboot, SD, resets)
    sim(daemon).behind_hub(BOARD_USB, lease=lease)
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    return page


def classes(page: Any, action: str, within: str = "") -> str:
    return page.locator(f'{within} [data-action="{action}"]'.strip()).first.get_attribute("class") or ""


@HUB
def test_on_a_board_someone_else_leases_program_and_friends_are_off_and_not_primary(page_factory, daemon):
    page = hub_board_page(page_factory, daemon, "other")
    held = "is for the lease holder only: alice@lab-pc-07 holds this board"
    tile = by_id(page, "tile-board")
    expect(tile.locator('[data-testid="reason-reset_dut"]')).to_have_text(f"Reset DUT {held}", timeout=T)
    expect(tile.locator('[data-testid="reason-reboot"]')).to_have_text(f"Reboot {held}")
    assert "danger" not in classes(page, "reboot", '[data-testid="tile-board"]')
    expect(by_id(page, "tile-debug").locator('[data-testid="reason-up"]')).to_have_text(f"Debug {held}")
    assert "primary" not in classes(page, "up", '[data-testid="tile-debug"]')
    section(page, "program")
    expect(by_id(page, "reason-program")).to_have_text(f"Program {held}", timeout=T)
    expect(page.locator('[data-action="program"]')).to_have_attribute("aria-disabled", "true")
    assert "primary" not in classes(page, "program")
    expect(by_id(page, "reason-restore")).to_have_text(f"Restore baseline {held}")
    section(page, "debug")
    expect(by_id(page, "debug-card").locator('[data-testid="reason-up"]')).to_have_text(f"Debug {held}")
    expect(by_id(page, "debug-card").locator('[data-testid="reason-detect"]')).to_have_text(f"Debug {held}")
    assert "primary" not in classes(page, "xvc_open")
    section(page, "power")
    expect(by_id(page, "reboot-card").locator('[data-testid="reason-reboot"]')).to_have_text(
        f"Reboot {held}")
    assert "danger" not in classes(page, "reboot", '[data-testid="reboot-card"]')
    expect(by_id(page, "reset-dut").locator('[data-testid="reason-reset_dut"]')).to_have_text(
        f"Reset DUT {held}")
    assert page.errors == []


@HUB
def test_negative_twin_the_lease_holder_gets_the_primary_buttons(page_factory, daemon):
    page = hub_board_page(page_factory, daemon, "mine")
    tile = by_id(page, "tile-board")
    expect(tile.locator('[data-action="reboot"]')).to_be_visible(timeout=T)
    expect(tile.locator('[data-testid="reason-reboot"]')).not_to_contain_text("lease holder")
    assert "danger" in classes(page, "reboot", '[data-testid="tile-board"]')
    assert "primary" in classes(page, "up", '[data-testid="tile-debug"]')
    section(page, "program")
    expect(page.locator('[data-action="program"]')).to_be_visible(timeout=T)
    assert "primary" in classes(page, "program")
    expect(by_id(page, "reason-program")).not_to_contain_text("lease holder")


@HUB
def test_your_other_sessions_lease_does_not_let_you_program_here(page_factory, daemon):
    page = hub_board_page(page_factory, daemon, "elsewhere")
    section(page, "program")
    expect(by_id(page, "reason-program")).to_have_text(
        f"Program is for the lease holder only: {ME} holds this board in another session, "
        "not this Harness Manager", timeout=T)
    assert "primary" not in classes(page, "program")


def test_negative_twin_a_board_with_no_hub_has_no_lease_rule(page_factory):
    page = page_factory(**APP)
    open_board(page)
    section(page, "program")
    expect(page.locator('[data-action="program"]')).to_be_visible(timeout=T)
    expect(by_id(page, "reason-program")).not_to_contain_text("lease", timeout=T)
    assert "primary" in classes(page, "program")


# --- 2: the preview says whose the hub lease is, apart from this app's lock -----------------------


@HUB
def test_the_preview_shows_the_last_known_hub_lease_apart_from_this_apps_lock(page_factory, daemon):
    sim(daemon).behind_hub(BOARD_FIELDED, lease="other")
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    expect(by_id(page, "lease-chip")).to_contain_text("alice@lab-pc-07", timeout=T)
    page.locator('[data-action="close-board"]').click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)     # the preview again
    expect(by_id(page, "preview-lock")).to_have_text("free")
    expect(page.locator(".preview dt", has_text="This app's lock")).to_have_count(1)
    lease = by_id(page, "preview-lease")
    expect(lease).to_have_attribute("data-lease", "other", timeout=T)
    expect(lease).to_contain_text("held by alice@lab-pc-07")
    expect(by_id(page, "preview-lease-at")).to_contain_text("as of ")
    assert page.locator(".preview dt", has_text="Lock").filter(has_not_text="app").count() == 0
    assert page.errors == []


@HUB
def test_negative_twin_a_board_never_read_shows_no_lease_it_does_not_know(page_factory, daemon):
    sim(daemon).behind_hub(BOARD_FIELDED, lease="other")
    page = page_factory(**APP)
    rail(page, BOARD_FIELDED).click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    expect(by_id(page, "preview-lock")).to_have_text("free")
    expect(by_id(page, "preview-lease")).to_have_count(0)            # nothing read: never "free"
    assert "held by" not in page.locator(".preview").inner_text()


# --- 3: the header's Harness says which version it is ----------------------------------------------


@pytest.mark.week_plan("harness_api", sim=True)
def test_the_header_says_release_or_firmware_and_never_disagrees_silently(page_factory, daemon):
    from tests.web.test_updui_browser import harness_page, install_rekey

    page, card = harness_page(page_factory)
    value = by_id(page, "fact-harness-value")
    expect(value).to_have_attribute("data-source", "release", timeout=T)   # the list is read now
    expect(value).to_have_text("release 1.0.0")
    expect(by_id(page, "fact-harness-fw")).to_have_count(0)               # the firmware agrees
    panel = install_rekey(page, card)
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text("installed: harness 1.1.1",
                                                                            timeout=T)
    # the mock's board now runs release 1.1.1 while its firmware still says 1.0.0: both are said
    expect(value).to_have_text("release 1.1.1", timeout=T)
    expect(by_id(page, "fact-harness-fw")).to_have_text(" · firmware 1.0.0")
    expect(by_id(page, "harness-running")).to_contain_text("1.1.1")
    expect(by_id(page, "harness-running-fw")).to_have_text("firmware reports 1.0.0")
    assert page.errors == []


def test_negative_twin_before_the_list_is_read_the_header_says_firmware(page_factory):
    page = page_factory(**APP)
    open_board(page)
    value = by_id(page, "fact-harness-value")
    expect(value).to_have_text("firmware 1.0.0", timeout=T)
    expect(value).to_have_attribute("data-source", "firmware")
    assert "version verb" in (value.get_attribute("title") or "")
    expect(by_id(page, "fact-harness-fw")).to_have_count(0)


# --- 4: the settings the page reads, and the one it cannot ------------------------------------------


def put_settings(daemon: Any, values: dict[str, Any]) -> None:
    import json
    import urllib.request

    req = urllib.request.Request(f"{daemon.url}/api/v1/settings", method="PUT",
                                 data=json.dumps(values).encode(),
                                 headers={"Authorization": f"Bearer {daemon.token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:            # noqa: S310 - loopback
        assert resp.status == 200


def panel_page(page_factory: Any, daemon: Any, engine: Any) -> Any:
    """BOARD_FIELDED with the Linux harness's panel verbs: the Front panel card's Identify
    (tests/web/test_p3_panel_ui.py's PanelSim, attached to the real daemon's demo sessions)."""
    from tests.fakes.clcd_panel_shell import PANEL_FEATURES
    from tests.fakes.p1_mock_panel import PanelSim

    if getattr(daemon.app.state, "panel", None) is None:
        PanelSim(engine).attach(engine)
    feats = [f for f in engine._board(BOARD_FIELDED).identity.features if f not in PANEL_FEATURES]
    engine.set_features(BOARD_FIELDED, [*feats, *PANEL_FEATURES])
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    toggle = page.locator('[data-action="details"]')
    toggle.wait_for(timeout=T)
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    page.wait_for_selector('[data-testid="panel-identify"]', timeout=T)
    return page.locator('[data-testid="panel-identify"] [data-testid="identify-seconds"]')


def test_the_front_panels_identify_starts_at_the_setting(page_factory, daemon, engine):
    put_settings(daemon, {"panel.identify_s": 20})
    expect(panel_page(page_factory, daemon, engine)).to_have_value("20", timeout=T)


def test_negative_twin_with_nothing_set_identify_starts_at_5_s(page_factory, daemon, engine):
    expect(panel_page(page_factory, daemon, engine)).to_have_value("5", timeout=T)   # LOCATE's 5 s


def test_the_send_line_and_the_byo_box_start_at_the_settings(page_factory, daemon):
    put_settings(daemon, {"consoles.line_ending": "lf", "debug.hw_server_mode": "byo"})
    page = page_factory(**APP)
    open_board(page)
    section(page, "consoles")
    expect(page.locator('[data-testid="send-ending"]').first).to_have_value("LF", timeout=T)
    section(page, "debug")
    expect(page.locator('[data-testid="xvc-byo"] input')).to_be_checked(timeout=T)
    # a change made while the page is open (the dialog, another tab, the CLI) follows
    put_settings(daemon, {"debug.hw_server_mode": "own"})
    expect(page.locator('[data-testid="xvc-byo"] input')).not_to_be_checked(timeout=T)
    assert page.errors == []


def test_negative_twin_with_nothing_set_the_send_line_is_crlf_and_byo_off(page_factory):
    page = page_factory(**APP)
    open_board(page)
    section(page, "consoles")
    expect(page.locator('[data-testid="send-ending"]').first).to_have_value("CRLF", timeout=T)
    section(page, "debug")
    expect(page.locator('[data-testid="xvc-byo"] input')).not_to_be_checked(timeout=T)


def test_settings_hides_the_row_nothing_reads_and_keeps_the_ones_that_work(page_factory):
    page = page_factory(**APP)
    page.wait_for_selector(".board-item", timeout=T)
    expect(gear(page)).to_have_attribute("data-settings-section", "general", timeout=T)
    row = page.locator('[data-testid="setting-row"][data-key="panel.identify_s"]')
    expect(row).to_be_visible(timeout=T)                                  # wired: shown
    expect(page.locator('[data-testid="setting-row"][data-key="panel.presence_who"]')).to_have_count(0)


# --- 6: one failed job is one Activity row, and a refused click is an error -------------------------


def errors_shown(page: Any) -> Any:
    section(page, "activity")
    page.locator('[data-testid="activity"]').get_by_role("button", name="Errors", exact=True).click()
    return page.locator('[data-testid="activity-table"] tbody tr')


def pick_led(page: Any) -> None:
    section(page, "program")
    wb.pick(page, "led")
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)


def test_a_failed_program_is_one_activity_row_with_its_reason(page_factory, engine):
    from harness_manager.core.errors import UnreachableError

    engine.failures["deploy.push"] = UnreachableError("the push socket closed mid-transfer",
                                                      hint="check the board's Ethernet")
    page = page_factory(**APP)
    open_board(page)
    pick_led(page)
    page.locator('[data-testid="arm-program"] input').check()
    page.locator('[data-action="program"]').click()
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "failed", timeout=T)
    expect(by_id(page, "program-result")).to_contain_text("UNREACHABLE", timeout=T)
    time.sleep(0.5)                                       # the daemon's own failure events land
    rows = errors_shown(page)
    expect(rows).to_have_count(1, timeout=T)
    expect(rows.first).to_contain_text("$ program led")
    expect(rows.first).to_contain_text("the push socket closed mid-transfer")
    assert page.errors == []


def test_negative_twin_a_job_another_client_ran_is_one_row_too(page_factory, engine, daemon):
    import json
    import urllib.parse
    import urllib.request

    from harness_manager.core.errors import UnreachableError

    engine.failures["deploy.push"] = UnreachableError("the push socket closed mid-transfer")
    page = page_factory(**APP)
    open_board(page)
    pick_led(page)                                        # the overlay list, as the CLI would read it
    url = f"{daemon.url}/api/v1/boards/{urllib.parse.quote(BOARD_USB, safe='')}/deploy"
    req = urllib.request.Request(url, method="POST", data=json.dumps({"overlay": "led"}).encode(),
                                 headers={"Authorization": f"Bearer {daemon.token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:            # noqa: S310 - loopback
        assert resp.status == 202
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "failed", timeout=T)
    time.sleep(0.5)
    rows = errors_shown(page)
    expect(rows).to_have_count(1, timeout=T)                          # deploy.failed; job.failed folded
    expect(rows.first).to_contain_text("the push socket closed mid-transfer")


def test_a_refused_program_shows_in_activity_errors(page_factory, engine):
    page = page_factory(**APP)
    open_board(page)
    pick_led(page)
    expect(by_id(page, "reason-program")).to_contain_text("not armed", timeout=T)
    page.locator('[data-action="program"]').click(force=True)         # the interlock answers
    expect(by_id(page, "program-result")).to_contain_text("Nothing was run.")
    rows = errors_shown(page)
    expect(rows).to_have_count(1, timeout=T)
    expect(rows.first).to_contain_text("$ program led  (refused, not run): not armed")
    assert engine.called("deploy.deploy") == []


def test_negative_twin_a_program_that_runs_logs_no_error(page_factory, engine):
    page = page_factory(**APP)
    open_board(page)
    pick_led(page)
    page.locator('[data-testid="arm-program"] input').check()
    page.locator('[data-action="program"]').click()
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "done", timeout=T)
    expect(errors_shown(page)).to_have_count(0)
    expect(page.locator('[data-testid="activity"]')).to_contain_text("0 of")
