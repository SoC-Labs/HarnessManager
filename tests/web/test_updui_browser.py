"""Lane UPDATE-UI in the browser, over the T14 mock: the app's own update (OTA-U: the banner,
the apply confirm, the restart overlay and reload, the rolled-back banner, Settings with the
admin policy), a board's Harness versions card (H9: verdicts, the re-key phrase, the lease,
pin and unpin, history and rollback) and the Overview's XVC line.

The mock's knobs (``tests/fakes/l3_week_plan.py`` for the app update, the real
``harness_api`` routes and catalogue over ``hcat_mock_harness.SimUpdate`` for the harness,
``x3_mock_xvc`` for XVC) stand in for the world outside the page: a version the checker
staged, an admin policy file, a GDB session a restart would end, a hub lease someone else
holds. Every action on the page is a click. Each behaviour has its negative twin.
``test_updui_real_daemon.py`` runs the app-update half over the real daemon. Nothing reaches
a board, a hub, GitHub or a real key.
"""

from __future__ import annotations

import re
import time

import pytest

from harness_manager.core.events import Event
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, FIELDED_FEATURES
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 1000}
NEW = "0.2.0"
OLD = "0.0.1"                       # what the mock "runs" (WeekPlanSim.app_version)
DOOR = "needs Debug USB here, or a hub that can write its SD"
POLICY = {"path": "/etc/harness-manager/policy.toml", "self_update": "notify", "channel": "stable",
          "check_interval_s": 43200, "problems": []}


# --- helpers (test_updui_screenshots.py uses them too) -----------------------------------------


def rail(page, bid):
    return page.locator(f'.board-item[data-board="{bid}"]')


def open_board(page, bid=BOARD_USB):
    nav.open_board(page, bid)                  # tests/web/nav.py (setui imports this one)


def section(page, key):
    """0.1.0's tab ``key`` where UI v2 put it (tests/web/nav.py)."""
    nav.section(page, key)


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def sim_of(daemon):
    return daemon.app.state.sim


def harness_sim(daemon):
    return daemon.app.state.harness


def wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def mark(page):
    """Tag this page load; a reload loses the tag."""
    page.evaluate("window.__updui_loaded = true")


def reloaded(page):
    return page.evaluate("window.__updui_loaded !== true")


def wait_reloaded(page, timeout=10.0):
    """The page's CSP (script-src 'self') refuses wait_for_function's eval: poll instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if reloaded(page):
                return True
        except Exception:  # noqa: BLE001, S110 - the context goes away during the reload
            pass
        time.sleep(0.1)
    return False


def banner(page):
    return by_id(page, "app-update-banner")


def open_settings(page):
    # FIX-PACK-4: the gear opens General first (then the last section used): go to Updates.
    page.locator('[data-action="settings"]').click()
    expect(by_id(page, "settings")).to_be_visible(timeout=T)
    page.locator('[data-testid="settings-nav"] [data-settings-section="updates"]').click()
    return by_id(page, "update-settings")


def harness_page(page_factory, bid=BOARD_USB, scheme="light", *, all_channels=False, height=None):
    page = page_factory(scheme, width=APP["width"], height=height or APP["height"])
    open_board(page, bid)
    section(page, "update")
    card = by_id(page, "harness-card")
    expect(by_id(page, "harness-empty")).to_be_visible(timeout=T)     # nothing cached yet
    if all_channels:
        by_id(page, "harness-all").check()
    card.locator('[data-action="harness_refresh"]').click()
    expect(card.locator('[data-release="1.0.0"]')).to_be_visible(timeout=T)
    return page, card


def row(card, version):
    return card.locator(f'.hrow[data-release="{version}"]')


def marks_of(card, version):
    return row(card, version).locator('[data-testid="marks"]')


def install_rekey(page, card, version="1.1.1", phrase="REKEY 0x72bb0a36", *, force=False):
    row(card, version).locator('[data-action="install"]').click()
    panel = by_id(page, "harness-install")
    expect(panel.locator('[data-testid="harness-steps"]')).to_be_visible(timeout=T)
    panel.locator('[data-testid="harness-rekey"]').fill(phrase)
    panel.locator('[data-testid="arm-harness"] input').check()
    panel.locator('[data-action="harness_install"]').click(force=force)   # force: a blocked click
    return panel


# --- the app: the banner ---------------------------------------------------------------------------


@pytest.mark.week_plan("update_api", sim=True)
def test_the_banner_offers_a_staged_version_with_its_notes(page_factory, daemon, engine):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    page = page_factory(**APP)
    expect(banner(page)).to_contain_text(f"Harness Manager {NEW} is ready: Restart to update",
                                         timeout=T)
    expect(banner(page)).to_have_attribute("data-kind", "staged")
    expect(banner(page).locator('[data-action="app-apply"]')).to_be_visible()
    expect(by_id(page, "settings-badge")).to_be_visible()
    # the checker stages the next one: its signed notes arrive with the event and show inline
    sim.staged = ["0.3.0", NEW]
    engine.bus.publish(Event("update.app.staged", "", {"version": "0.3.0", "channel": "stable",
                                                       "notes": "Adds the hub SD door."}))
    expect(banner(page)).to_contain_text("Harness Manager 0.3.0 is ready", timeout=T)
    banner(page).locator('[data-action="app-notes"]').click()
    expect(by_id(page, "app-notes")).to_have_text("Adds the hub SD door.")
    # Later hides this version only
    banner(page).locator('[data-action="app-later"]').click()
    expect(banner(page)).to_have_count(0)


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_a_developer_install_shows_no_banner_and_settings_says_why(page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.app_available = NEW
    sim.app_dev_install = "this is a developer install (pip install -e)"
    page = page_factory(**APP)
    card = open_settings(page)
    expect(card.locator('[data-testid="dev-install"]')).to_contain_text(
        "Developer install: updates are off (this is a developer install (pip install -e))")
    for choice in ("set-channel", "set-auto"):
        for b in card.locator(f'[data-testid="{choice}"] button').all():
            expect(b).to_be_disabled()
    expect(card.locator('[data-testid="next-check"]')).to_contain_text("never: this is a developer install")
    assert banner(page).count() == 0 and by_id(page, "settings-badge").count() == 0


@pytest.mark.week_plan("update_api", sim=True)
def test_under_notify_the_banner_says_available_and_downloads_on_a_click(page_factory, daemon):
    sim = sim_of(daemon)
    sim.update_settings["auto"] = "notify"
    sim.app_available = NEW
    page = page_factory(**APP)
    expect(banner(page)).to_contain_text(f"Harness Manager {NEW} is available", timeout=T)
    expect(banner(page)).to_have_attribute("data-kind", "available")
    assert sim.staged == []                                    # notify: nothing downloaded yet
    banner(page).locator('[data-action="app-stage"]').click()
    expect(banner(page)).to_contain_text(f"Harness Manager {NEW} is ready: Restart to update",
                                         timeout=T)
    assert sim.staged == [NEW] and sim.apply_state == "idle"  # staged, never applied


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_an_admin_off_policy_hides_the_offer(page_factory, daemon):
    sim = sim_of(daemon)
    sim.app_available = NEW
    sim.staged = [NEW]
    sim.app_policy = {**POLICY, "self_update": "off"}
    page = page_factory(**APP)
    card = open_settings(page)
    expect(card.locator('[data-testid="effective-why"]')).to_contain_text("turns self-update off")
    assert banner(page).count() == 0


# --- the app: apply, soft busy, the overlay, the reload, the rollback --------------------------------


@pytest.mark.week_plan("update_api", sim=True)
def test_apply_with_soft_busy_sessions_needs_a_confirm_that_lists_them(page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.restart_hold.clear()                                   # hold the restart on screen
    sim.app_soft_busy = [
        {"kind": "gdb", "board_id": BOARD_USB, "detail": f"OpenOCD for GDB on {BOARD_USB} (port 3333)"},
        {"kind": "screen", "board_id": BOARD_USB, "name": "uart0", "clients": 1,
         "path": "/tmp/harness-manager-u/mps3-uart0",
         "detail": "1 terminal(s) on /tmp/harness-manager-u/mps3-uart0 (re-attach after the restart)"}]
    page = page_factory(**APP)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    confirm = by_id(page, "apply-confirm")
    expect(confirm).to_be_visible(timeout=T)
    expect(confirm.locator('[data-kind="gdb"]')).to_contain_text("OpenOCD for GDB on")
    expect(confirm.locator('[data-kind="screen"]')).to_contain_text("mps3-uart0")
    expect(confirm).to_contain_text("screen /tmp/harness-manager-u/mps3-uart0")   # U4: same path
    assert sim.apply_state == "idle"                           # nothing started without the confirm
    confirm.locator('[data-action="app-apply-confirm"]').click()
    expect(by_id(page, "restart-overlay")).to_be_visible(timeout=T)
    assert sim.apply_plan and sim.apply_plan["to"] == NEW
    sim.restart_hold.set()


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_not_now_leaves_the_service_alone_and_no_soft_busy_needs_no_confirm(
        page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.restart_hold.clear()
    sim.app_soft_busy = [{"kind": "xvc", "board_id": BOARD_USB,
                          "detail": f"the XVC session on {BOARD_USB}, hw_server attached"}]
    page = page_factory(**APP)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    by_id(page, "apply-confirm").locator('[data-action="app-apply-cancel"]').click()
    expect(by_id(page, "apply-confirm")).to_have_count(0)
    assert sim.apply_state == "idle" and by_id(page, "restart-overlay").count() == 0
    sim.app_soft_busy = []                                     # the XVC session closed
    banner(page).locator('[data-action="app-apply"]').click()
    expect(by_id(page, "restart-overlay")).to_be_visible(timeout=T)
    assert by_id(page, "apply-confirm").count() == 0
    sim.restart_hold.set()


@pytest.mark.week_plan("update_api", sim=True)
def test_the_apply_shows_its_phases_and_cancel_ends_a_drain(page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.apply_hold.clear()                      # the mock stays in the drain
    page = page_factory(**APP)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    applying = by_id(page, "app-applying")
    expect(applying).to_contain_text(f"Updating Harness Manager to {NEW}", timeout=T)
    expect(applying.locator('[data-phase="draining"]')).to_have_class("active")
    expect(applying.locator('[data-phase="restarting"]')).not_to_have_class("active")
    applying.locator('[data-action="app-cancel"]').click()
    ended = by_id(page, "app-apply-ended")
    expect(ended).to_contain_text(f"The update to {NEW} was cancelled", timeout=T)
    expect(ended).to_contain_text("Nothing was restarted.")
    assert sim.apply_state == "idle" and by_id(page, "restart-overlay").count() == 0
    sim.apply_hold.set()
    time.sleep(0.3)
    assert sim.app_version == OLD               # the cancelled apply never restarted


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_a_drain_that_is_let_run_goes_on_to_the_restart(page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.apply_hold.clear()
    sim.restart_hold.clear()
    page = page_factory(**APP)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    expect(by_id(page, "app-applying").locator('[data-phase="draining"]')).to_have_class(
        "active", timeout=T)
    sim.apply_hold.set()                        # the running jobs finished
    expect(by_id(page, "restart-overlay")).to_be_visible(timeout=T)
    assert by_id(page, "app-apply-ended").count() == 0
    sim.apply_outcome = "rolled-back"           # end it without a reload
    sim.restart_hold.set()
    expect(by_id(page, "app-rolled-back")).to_be_visible(timeout=T)


@pytest.mark.week_plan("update_api", sim=True)
def test_the_overlay_counts_down_while_reconnecting_then_the_page_reloads_on_the_new_version(
        page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.restart_hold.clear()
    page = page_factory(**APP)
    open_board(page)                            # the reload comes back to the same board
    mark(page)
    banner(page).locator('[data-action="app-apply"]').click()
    overlay = by_id(page, "restart-overlay")
    expect(overlay).to_contain_text(f"Restarting Harness Manager to {NEW}", timeout=T)
    expect(overlay.locator('[data-testid="restart-state"]')).to_contain_text("Reconnecting")
    expect(overlay.locator('[data-testid="restart-countdown"]')).to_have_text(
        re.compile(r"^\d+ s$"))
    time.sleep(1.5)
    assert not reloaded(page)                   # /health still says the old version: no reload
    sim.restart_hold.set()                      # the new daemon answers
    assert wait_reloaded(page)
    page.wait_for_selector('[data-testid="daemon-line"]', timeout=T)
    expect(by_id(page, "app-updated")).to_contain_text(f"Updated to Harness Manager {NEW}", timeout=T)
    expect(by_id(page, "daemon-line")).to_contain_text(NEW)
    expect(by_id(page, "board-header")).to_be_visible(timeout=T)
    assert banner(page).count() == 0            # nothing newer is staged


@pytest.mark.week_plan("update_api", sim=True)
def test_a_version_that_fails_its_health_check_says_so_and_the_page_stays(page_factory, daemon):
    sim = sim_of(daemon)
    sim.staged = [NEW]
    sim.apply_outcome = "rolled-back"
    page = page_factory(**APP)
    mark(page)
    banner(page).locator('[data-action="app-apply"]').click(timeout=T)
    err = by_id(page, "app-rolled-back")
    expect(err).to_contain_text(f"Harness Manager {NEW} failed its health check; back on {OLD}. "
                                "It won't be offered again.", timeout=T)
    expect(err.locator('[data-testid="app-rolled-back-why"]')).to_contain_text("did not report")
    expect(by_id(page, "restart-overlay")).to_have_count(0)
    assert not reloaded(page)                   # the version did not change: no reload
    assert banner(page).count() == 0            # a bad version is never offered again
    card = open_settings(page)
    expect(card.locator(f'[data-testid="bad-versions"] tr[data-version="{NEW}"]')).to_contain_text(
        "did not report")


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_a_dismissed_rollback_banner_stays_dismissed_after_a_reload(page_factory, daemon):
    sim = sim_of(daemon)
    sim.app_last_apply = {"id": "a1b2c3", "from": OLD, "to": NEW, "result": "rolled-back",
                          "phase": "health", "reason": "the daemon exited while starting",
                          "at": time.time() - 60, "seconds": 12.0}
    page = page_factory(**APP)
    expect(by_id(page, "app-rolled-back")).to_be_visible(timeout=T)    # after a restart too
    by_id(page, "app-rolled-back").locator('[data-action="dismiss-outcome"]').click()
    expect(by_id(page, "app-rolled-back")).to_have_count(0)
    page.reload()
    page.wait_for_selector('[data-testid="daemon-line"]', timeout=T)
    time.sleep(0.8)
    assert by_id(page, "app-rolled-back").count() == 0


# --- Settings: the admin policy -------------------------------------------------------------------------


@pytest.mark.week_plan("update_api", sim=True)
def test_the_admin_policy_disables_what_it_limits_and_says_so(page_factory, daemon):
    sim = sim_of(daemon)
    sim.app_policy = dict(POLICY)
    sim.app_last_check = {"at": time.time() - 900, "interval_s": 43200, "channel": "stable",
                          "serial": 9, "available": "", "error": ""}
    page = page_factory(**APP)
    card = open_settings(page)
    expect(card.locator('[data-testid="policy-note"]')).to_contain_text("/etc/harness-manager/policy.toml")
    for ch in ("beta", "dev"):
        b = card.locator(f'[data-testid="set-channel"] button[data-value="{ch}"]')
        expect(b).to_be_disabled()
        expect(b).to_have_attribute("title", "set by your admin policy (stable only)")
    expect(card.locator('[data-testid="set-channel"] button[data-value="stable"]')).to_have_attribute(
        "aria-pressed", "true")
    stage = card.locator('[data-testid="set-auto"] button[data-value="stage"]')
    expect(stage).to_be_disabled()
    expect(stage).to_have_attribute("title", "set by your admin policy (at most notify)")
    expect(card.locator('[data-testid="set-auto"] button[data-value="notify"]')).to_have_attribute(
        "aria-pressed", "true")
    expect(card.locator('[data-testid="effective-why"]')).to_contain_text("allows at most 'notify'")
    expect(card.locator('[data-testid="policy-locked"]')).to_contain_text("set by your admin policy")
    expect(card.locator('[data-testid="last-check"]')).to_contain_text("stable #9, up to date")
    expect(card.locator('[data-testid="next-check"]')).to_contain_text("then every 12 h")
    assert sim.update_settings == {"channel": "", "auto": ""}
    # what the policy allows still works
    card.locator('[data-testid="set-auto"] button[data-value="off"]').click()
    expect(card.locator('[data-testid="set-auto"] button[data-value="off"]')).to_have_attribute(
        "aria-pressed", "true", timeout=T)
    assert sim.update_settings["auto"] == "off"


def next_check_text(at: float, every: str = "6 h") -> str:
    """What the card says for the daemon's ``next_check`` (local HH:MM; the date if not today)."""
    t = time.localtime(at)
    same_day = time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d", time.localtime())
    when = time.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M", t)
    return f"next check at {when}, then every {every}"


NOTES = "Faster consoles.\nThe touch bus no longer wedges."


@pytest.mark.week_plan("update_api", sim=True)
def test_settings_shows_the_next_check_time_and_the_notes_collapsed(page_factory, daemon):
    """SMALL-4: GET /update/app next_check and last_check.notes on the Updates card."""
    sim = sim_of(daemon)
    at = time.time() + 2 * 3600
    sim.app_next_check = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
    sim.app_last_check = {"at": time.time() - 60, "interval_s": 21600, "channel": "stable",
                          "serial": 4, "available": NEW, "staged": False, "error": "",
                          "notes": NOTES}
    page = page_factory(**APP)
    card = open_settings(page)
    expect(card.locator('[data-testid="next-check"]')).to_have_text(next_check_text(at), timeout=T)
    fold = card.locator('[data-testid="last-check-notes"]')
    expect(fold).to_contain_text(f"What's new in {NEW}")
    text = card.locator('[data-testid="last-check-notes-text"]')
    expect(text).to_be_hidden()                                        # collapsed
    card.locator('[data-action="last-check-notes"]').click()
    expect(text).to_be_visible()
    assert text.inner_text() == NOTES
    assert not page.errors, page.errors


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_without_a_schedule_or_notes_the_card_is_as_before(page_factory, daemon):
    sim = sim_of(daemon)
    sim.app_next_check = None                    # an older daemon, or no timer
    sim.app_last_check = {"at": time.time() - 900, "interval_s": 21600, "channel": "stable",
                          "serial": 4, "available": "", "error": ""}
    page = page_factory(**APP)
    card = open_settings(page)
    nxt = card.locator('[data-testid="next-check"]')
    expect(nxt).to_contain_text("about ", timeout=T)
    expect(nxt).not_to_contain_text("next check at")
    expect(card.locator('[data-testid="last-check"]')).to_contain_text("up to date")
    assert card.locator('[data-testid="last-check-notes"]').count() == 0
    assert not page.errors, page.errors


@pytest.mark.week_plan("update_api", sim=True)
def test_negative_twin_without_a_policy_every_setting_is_the_users(page_factory, daemon):
    sim = sim_of(daemon)
    page = page_factory(**APP)
    card = open_settings(page)
    assert card.locator('[data-testid="policy-note"]').count() == 0
    assert card.locator('[data-testid="policy-locked"]').count() == 0
    for b in card.locator('[data-testid="set-channel"] button, [data-testid="set-auto"] button').all():
        expect(b).to_be_enabled()
    card.locator('[data-testid="set-channel"] button[data-value="beta"]').click()
    expect(card.locator('[data-testid="set-channel"] button[data-value="beta"]')).to_have_attribute(
        "aria-pressed", "true", timeout=T)
    assert sim.update_settings["channel"] == "beta"
    expect(card.locator('[data-testid="bad-versions-none"]')).to_be_visible()


# --- the board's Update page: the app card points to Settings --------------------------------------


@pytest.mark.week_plan("harness_api", sim=True)
def test_the_harness_list_gives_each_release_a_verdict_with_its_reason(page_factory, daemon):
    harness_sim(daemon).withdrawn = {"1.1.0"}
    page, card = harness_page(page_factory, all_channels=True)
    expect(row(card, "2.0.0")).to_have_attribute("data-verdict", "needs-door", timeout=T)
    expected = {"2.0.0": "needs-door", "1.1.1": "re-key", "1.1.0": "incompatible", "1.0.0": "fits"}
    for version, verdict in expected.items():
        expect(row(card, version)).to_have_attribute("data-verdict", verdict)
    expect(row(card, "1.1.1").locator('[data-testid="why"]')).to_contain_text(
        "type 'REKEY 0x72bb0a36' to consent")
    expect(row(card, "1.1.0").locator('[data-testid="why"]')).to_contain_text("withdrawn")
    expect(row(card, "2.0.0").locator('[data-testid="why"]')).to_contain_text(DOOR)
    expect(marks_of(card, "1.0.0")).to_contain_text("running")
    expect(marks_of(card, "1.1.1")).to_contain_text("current")
    expect(marks_of(card, "1.1.1")).to_contain_text("offered")
    expect(by_id(page, "harness-running")).to_contain_text("1.0.0")
    row(card, "1.1.1").locator('[data-action="changes"]').click()
    expect(row(card, "1.1.1").locator('[data-testid="changes"]')).to_contain_text(
        "re-key: static 0x3f1a560f -> 0x72bb0a36")
    for version in ("2.0.0", "1.1.0", "1.0.0"):          # door, withdrawn, running: no install
        expect(row(card, version).locator('[data-action="install"]')).to_have_attribute(
            "aria-disabled", "true")
    expect(by_id(page, "harness-not-yet")).to_contain_text("Not yet")


@pytest.mark.week_plan("harness_api", sim=True)
def test_negative_twin_a_board_without_the_debug_usb_needs_a_door_for_a_new_base(page_factory, daemon):
    page, card = harness_page(page_factory, BOARD_FIELDED)
    expect(row(card, "1.1.1")).to_have_attribute("data-verdict", "needs-door")
    expect(row(card, "1.1.1").locator('[data-testid="verdict"]')).to_contain_text("needs Debug USB or hub")
    expect(row(card, "1.1.1").locator('[data-testid="why"]')).to_contain_text(DOOR)
    button = row(card, "1.1.1").locator('[data-action="install"]')
    expect(button).to_have_attribute("title", DOOR)
    button.click(force=True)
    assert by_id(page, "harness-install").count() == 0          # nothing was even planned
    expect(row(card, "1.0.0")).to_have_attribute("data-verdict", "fits")


@pytest.mark.week_plan("harness_api", sim=True)
def test_a_rekey_install_needs_the_exact_phrase(page_factory, daemon, engine):
    page, card = harness_page(page_factory)
    panel = install_rekey(page, card, phrase="rekey 0x72bb0a36", force=True)   # not exact
    expect(panel.locator('[data-testid="reason-harness_install"]')).to_contain_text(
        "a re-key: type exactly REKEY 0x72bb0a36")
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text("Nothing was run.")
    assert harness_sim(daemon).running == {}


@pytest.mark.week_plan("harness_api", sim=True)
def test_negative_twin_the_exact_phrase_installs_and_the_running_mark_moves(page_factory, daemon):
    page, card = harness_page(page_factory)
    panel = install_rekey(page, card)
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text(
        "installed: harness 1.1.1 is running (mock)", timeout=T)
    expect(marks_of(card, "1.1.1")).to_contain_text("running", timeout=T)
    expect(marks_of(card, "1.1.1")).to_contain_text("installed")
    assert "running" not in marks_of(card, "1.0.0").inner_text()
    assert harness_sim(daemon).running == {BOARD_USB: "1.1.1"}


@pytest.mark.week_plan("harness_api", sim=True)
def test_an_install_without_the_hub_lease_is_refused_naming_the_holder(page_factory, daemon):
    # UI v2 (R3): Install is a drive button: the page refuses it for a watcher, naming the
    # holder, and nothing reaches the service (which would say 409 HELD too).
    sim_of(daemon).behind_hub(BOARD_USB, lease="other")
    page, card = harness_page(page_factory)
    expect(by_id(page, "harness-lease").first).to_contain_text("alice@lab-pc-07")
    panel = install_rekey(page, card, force=True)
    expect(panel.locator('[data-testid="reason-harness_install"]')).to_contain_text(
        "Install is for the lease holder only: alice@lab-pc-07 holds this board", timeout=T)
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text("Nothing was run.")
    assert harness_sim(daemon).running == {}


@pytest.mark.week_plan("harness_api", sim=True)
def test_negative_twin_the_lease_holder_installs(page_factory, daemon):
    sim_of(daemon).behind_hub(BOARD_USB, lease="mine")
    page, card = harness_page(page_factory)
    expect(by_id(page, "harness-lease").first).to_contain_text("You hold this board's hub lease")
    panel = install_rekey(page, card)
    expect(panel.locator('[data-testid="harness-result"]')).to_contain_text(
        "installed: harness 1.1.1", timeout=T)


@pytest.mark.week_plan("harness_api", sim=True)
def test_pin_moves_the_offer_and_unpin_puts_it_back(page_factory, daemon):
    page, card = harness_page(page_factory)
    row(card, "1.1.0").locator('[data-action="pin"]').click()
    expect(marks_of(card, "1.1.0")).to_contain_text("pinned", timeout=T)
    expect(marks_of(card, "1.1.0")).to_contain_text("offered")
    expect(marks_of(card, "1.1.1")).to_contain_text("past-pin")
    expect(by_id(page, "pinned-chip")).to_contain_text("pinned 1.1.0")
    assert "offered" not in marks_of(card, "1.1.1").inner_text()
    row(card, "1.1.0").locator('[data-action="pin"]').click()        # now it says Unpin
    expect(marks_of(card, "1.1.1")).to_contain_text("offered", timeout=T)
    assert "pinned" not in marks_of(card, "1.1.0").inner_text()
    assert by_id(page, "pinned-chip").count() == 0


@pytest.mark.week_plan("harness_api", sim=True)
def test_negative_twin_with_no_install_there_is_no_history_and_nothing_to_roll_back_to(
        page_factory, daemon):
    page, card = harness_page(page_factory)
    back = page.locator('[data-action="harness-rollback"]')      # UI v2: the board's side of Versions
    expect(back).to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "rollback-why")).to_contain_text("nothing to roll back to")
    back.click(force=True)
    assert by_id(page, "harness-rollback").count() == 0
    card.locator('[data-action="harness-history"]').click()
    expect(by_id(page, "harness-history-none")).to_be_visible(timeout=T)


@pytest.mark.week_plan("harness_api", sim=True)
def test_history_then_rollback_to_the_previous_release(page_factory, daemon):
    page, card = harness_page(page_factory)
    install_rekey(page, card)
    expect(marks_of(card, "1.1.1")).to_contain_text("running", timeout=T)
    card.locator('[data-action="harness-history"]').click()
    expect(by_id(page, "harness-history").locator('tr[data-version="1.1.1"]')).to_contain_text(
        "1.0.0", timeout=T)
    back = page.locator('[data-action="harness-rollback"]')      # UI v2: the board's side of Versions
    expect(back).to_contain_text("Roll back to 1.0.0", timeout=T)
    back.click()
    panel = by_id(page, "harness-rollback")
    expect(panel).to_have_attribute("data-release", "1.0.0", timeout=T)
    panel.locator('[data-testid="rollback-rekey"]').fill("REKEY 0x3f1a560f")   # back across statics
    panel.locator('[data-testid="arm-harness-rollback"] input').check()
    panel.locator('[data-action="harness_rollback"]').click()
    expect(marks_of(card, "1.0.0")).to_contain_text("running", timeout=T)
    assert harness_sim(daemon).running == {BOARD_USB: "1.0.0"}


# --- the Overview's Debug tile: one line for XVC ---------------------------------------------------------


@pytest.mark.week_plan("xvc_api", sim=True)
def test_the_debug_tile_shows_the_xvc_session_in_one_line(page_factory, daemon, engine):
    engine._set_identity(BOARD_FIELDED, features=(*FIELDED_FEATURES, "xvc_dbgbr"),
                         rm_id="0x0100000A", rm_name="nanosoc_ila")
    page = page_factory(**APP)
    open_board(page, BOARD_FIELDED)
    line = by_id(page, "tile-xvc")
    expect(line).to_have_attribute("data-state", "down", timeout=T)
    expect(line).to_contain_text("closed")
    daemon.app.state.xvc.open(BOARD_FIELDED, byo=False)       # the Debug section opened it
    expect(line).to_have_attribute("data-state", "ready", timeout=T)
    expect(line).to_contain_text("localhost:")
    daemon.app.state.xvc.attach(BOARD_FIELDED)
    expect(line).to_have_attribute("data-state", "attached", timeout=T)
    expect(line).to_contain_text("attached")


@pytest.mark.week_plan("xvc_api", sim=True)
def test_negative_twin_a_harness_without_xvc_says_why_on_the_tile(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB)                                # v0.8 firmware: no xvc_dbgbr
    line = by_id(page, "tile-xvc")
    expect(line).to_have_attribute("data-state", "unsupported", timeout=T)
    expect(line).to_contain_text("not on this board: needs harness firmware with 'xvc_dbgbr'")
    assert line.locator('[data-testid="tile-xvc-state"]').count() == 0
