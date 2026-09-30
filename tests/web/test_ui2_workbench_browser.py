"""UI v2 Phase 2, lane UI2-WORKBENCH: the Workbench tab in the browser (round 3,
docs/design/ui-v2/prototype-b-round3.html; docs/planning/UI_V2_PLAN.md §1.3 W1-W17).

Clicks only, on the real daemon over the demo engines (the classic one, and the showcase
``harness-manager app --demo`` serves), or over the T14 mock where a test scripts the world
outside the page (a hub lease, the XVC server). Each behaviour has its negative twin.

R3 (every drive button keeps its lease gate): ``test_a_watcher_cannot_drive_anything_on_the_workbench``
clicks every drive button the Workbench shows on a board someone else holds, and expects each
refused with the reason and nothing sent.
"""

from __future__ import annotations

import re

import pytest

from harness_manager.core.pack import OverlayRef
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, FIELDED_FEATURES
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from harness_manager.services.debug import gdb_command
from tests.web import nav, wb
from tests.web.test_demo_all_browser import showcase  # noqa: F401 - the fixture

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def workbench(page, bid):
    nav.open_board(page, bid)
    nav.tab(page, "workbench")
    by_id(page, "program-card").wait_for(timeout=T)


# --- the picker (W1, W2) ---------------------------------------------------------------------------


def test_the_picker_filters_and_tags_what_is_loaded_now(page_factory):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)                          # nanosoc 0x01000001 is loaded
    expect(by_id(page, "design-picker")).to_contain_text("Pick a design (5 load on this shell)")
    wb.open_picker(page)
    rows = page.locator('[data-testid="design-list"] [data-overlay]')
    expect(rows).to_have_count(6)                       # 5 here, 1 keyed to another shell
    expect(page.locator('[data-overlay="nanosoc"] .tag.now')).to_have_text("loaded now")
    expect(page.locator('[data-overlay="greybox"] .tag.base')).to_have_text("baseline")
    expect(page.locator('[data-overlay="led"] .tag')).to_have_count(0)    # nothing to say
    by_id(page, "design-filter").fill("0x0100001e")      # by rm_id
    expect(rows).to_have_count(1)
    expect(rows.first).to_have_attribute("data-overlay", "led")
    by_id(page, "design-filter").fill("led")             # by name
    expect(rows).to_have_count(1)
    assert page.errors == []


def test_negative_twin_a_filter_that_matches_nothing_says_so(page_factory):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    wb.open_picker(page)
    by_id(page, "design-filter").fill("zzz")
    expect(page.locator('[data-testid="design-list"] [data-overlay]')).to_have_count(0)
    expect(by_id(page, "design-list")).to_contain_text("No design matches.")
    page.keyboard.press("Escape")                        # Esc closes the list, nothing picked
    expect(by_id(page, "design-list")).to_have_count(0)
    expect(by_id(page, "selected-overlay")).to_have_count(0)


def test_a_design_keyed_to_another_shell_is_listed_apart_and_refused(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    wb.open_picker(page)
    expect(by_id(page, "blocked-nanosoc_multicore")).to_have_text("other shell")
    expect(by_id(page, "design-list")).to_contain_text("Keyed to another shell")
    wb.pick(page, "nanosoc_multicore")
    page.wait_for_selector('[data-testid="preflight-list"] li[data-check="mismatch"]', timeout=T)
    expect(by_id(page, "preflight-summary")).to_contain_text("Program is refused")
    page.locator('[data-testid="arm-program"] input').check()
    expect(by_id(page, "reason-program")).to_contain_text("preflight MISMATCH")
    expect(page.locator('[data-action="program"]')).to_have_attribute("aria-disabled", "true")
    page.locator('[data-action="program"]').click(force=True)
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "refused")
    assert engine.called("deploy.deploy") == []


def test_negative_twin_a_design_for_this_shell_passes_and_folds_its_checks(page_factory):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    wb.pick(page, "led", wait=True)
    expect(by_id(page, "blocked-led")).to_have_count(0)
    assert page.locator('[data-testid="preflight-list"] li[data-check="mismatch"]').count() == 0
    oks = page.locator('[data-action="preflight-all"]')
    expect(oks).to_contain_text("checks OK")             # the passing checks fold into one chip
    folded = page.locator('[data-testid="preflight-list"] li[data-check="ok"].sr-only')
    expect(folded).not_to_have_count(0)                  # each is still there for a screen reader
    oks.click()
    expect(folded).to_have_count(0)                      # listed, one chip each
    expect(page.locator('[data-testid="preflight-list"] li[data-check="ok"]').first).to_be_visible()
    expect(by_id(page, "preflight-summary")).to_contain_text("no mismatch")


# --- the download bar and the one outcome box (W4, W6) ----------------------------------------------


def test_programming_shows_the_download_bar_then_one_outcome(page_factory, engine):
    engine.speed = 2.0                                   # a deploy long enough to watch
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    wb.pick(page, "led", wait=True)
    page.locator('[data-testid="arm-program"] input').check()
    expect(by_id(page, "reason-program")).to_contain_text("ready: Program swaps the partition to led")
    page.locator('[data-action="program"]').click()
    bar = by_id(page, "deploy-progress")
    expect(bar).to_be_visible(timeout=T)
    for step in ("guard", "swap", "push", "verify"):
        expect(bar.locator(f'[data-step="{step}"]')).to_have_count(1)
    expect(bar.locator('[data-step="card"]')).to_have_count(0)          # not kept: no card step
    expect(by_id(page, "deploy-phase")).to_contain_text(re.compile(r"push · \d+ KiB of 100 KiB"), timeout=T)
    outcome = by_id(page, "deploy-outcome")
    expect(outcome).to_have_attribute("data-state", "done", timeout=T)
    expect(bar).to_have_count(0)                          # one box: the bar became the outcome
    expect(outcome).to_contain_text("Programmed led 0x0100001E")
    expect(by_id(page, "deploy-pushed")).to_contain_text(re.compile(r"pushed 100 KiB in [\d.]+ s · \d+ KB/s · verified \d\d:\d\d"))
    expect(by_id(page, "design-verified")).to_be_visible(timeout=T)    # the header agrees
    expect(page.locator('[data-testid="arm-program"] input')).not_to_be_checked()
    outcome.locator('button[aria-label="Dismiss the outcome"]').click()
    expect(outcome).to_have_count(0)
    assert page.errors == []


def test_negative_twin_a_failed_push_is_one_failed_outcome_with_its_reason(page_factory, engine):
    from harness_manager.core.errors import UnreachableError

    engine.failures["deploy.push"] = UnreachableError("the push socket closed mid-transfer")
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    wb.pick(page, "led", wait=True)
    page.locator('[data-testid="arm-program"] input').check()
    page.locator('[data-action="program"]').click()
    outcome = by_id(page, "deploy-outcome")
    expect(outcome).to_have_attribute("data-state", "failed", timeout=T)
    expect(outcome).to_contain_text("the push socket closed mid-transfer")
    expect(by_id(page, "deploy-pushed")).to_have_count(0)                # nothing was verified
    expect(by_id(page, "program-result")).to_contain_text("UNREACHABLE")  # its command, open
    expect(by_id(page, "design-verified")).to_have_count(0)


# --- Import a design (W7) -----------------------------------------------------------------------


def test_import_opens_the_import_dialog_from_the_button_and_the_pickers_foot(page_factory):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    for open_it in (lambda: page.locator('[data-action="import"]').click(),
                    lambda: (wb.open_picker(page), page.locator('[data-action="import-design"]').click())):
        open_it()
        # The Import dialog is the Build lane's (registerModal("import")); until it is in the
        # build the page says so, never an error.
        dialog = page.locator('[data-testid="modal-layer"][data-modal="import"]')
        toast = by_id(page, "toast")
        expect(dialog.or_(toast)).to_be_visible(timeout=T)
        if dialog.count():
            page.keyboard.press("Escape")
            expect(dialog).to_have_count(0)
        else:
            expect(toast).to_contain_text("Import a design")
        expect(by_id(page, "design-list")).to_have_count(0)             # the list closed
    assert page.errors == []


# --- the console switcher (W8, W9, W11-W13) ---------------------------------------------------------


def test_the_linux_board_lists_the_harness_console_and_you_can_type(showcase):  # noqa: F811
    page = showcase.page(**APP)
    workbench(page, BOARD_LINUX)
    tab = page.locator('[data-console-tab="shell"]')
    expect(tab).to_contain_text("Harness")
    for name in ("uart0", "uart1", "swo"):
        expect(page.locator(f'[data-console-tab="{name}"]')).to_have_count(1)
    expect(page.locator('[data-console-tab="uart0"]')).to_have_attribute("aria-selected", "true")
    tab.click()
    pane = by_id(page, "console-shell")
    expect(pane.locator(".cs-now")).to_have_text("Harness console")
    expect(by_id(page, "console-access")).to_contain_text("you can type")
    expect(by_id(page, "send-line")).to_be_enabled()
    expect(by_id(page, "send-line")).to_have_attribute("placeholder", re.compile("root shell"))
    assert not page.errors, page.errors


def test_negative_twin_bare_metal_has_a_shell_console_not_a_harness_console(showcase):  # noqa: F811
    page = showcase.page(**APP)
    workbench(page, BOARD_V011)
    expect(page.locator('[data-console-tab="shell"]')).to_contain_text("Shell")
    expect(page.locator(".cs-seg")).not_to_contain_text("Harness")
    page.locator('[data-console-tab="shell"]').click()
    expect(by_id(page, "console-shell").locator(".cs-now")).to_have_text("Shell console")
    expect(by_id(page, "console-access")).to_have_count(0)               # writable, not the root shell


def test_the_switcher_keeps_each_consoles_scrollback_and_counts_new_lines(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    expect(by_id(page, "console-state")).to_have_text("up", timeout=T)
    page.locator('[data-console-tab="uart1"]').click()
    expect(by_id(page, "console-uart1").locator('[data-testid="console-state"]')).to_have_text("up", timeout=T)
    page.locator('[data-console-tab="uart0"]').click()
    engine.inject_console(BOARD_USB, "uart1", "one\r\ntwo\r\nthree\r\n")
    expect(page.locator('[data-console-tab="uart1"] .cs-new')).to_have_text("3", timeout=T)
    page.locator('[data-console-tab="uart1"]').click()
    expect(page.locator('[data-console-tab="uart1"] .cs-new')).to_have_count(0)   # seen
    text = page.evaluate(f"window.__harness_managerConsoles.text({BOARD_USB!r}, 'uart1')")
    assert "three" in text


def test_negative_twin_a_console_never_opened_shows_no_count_and_no_connection(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    expect(by_id(page, "console-state")).to_have_text("up", timeout=T)
    engine.inject_console(BOARD_USB, "swo", "unseen\r\n")
    page.wait_for_timeout(700)
    expect(page.locator('[data-console-tab="swo"] .cs-new')).to_have_count(0)
    assert page.evaluate(f"window.__harness_managerConsoles.state({BOARD_USB!r}, 'swo')") is None


def test_reset_dut_is_armed_and_switches_to_the_dut_console(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    page.locator('[data-console-tab="shell"]').click()
    reset = page.locator('[data-action="reset_dut"]')
    expect(reset).to_have_attribute("aria-disabled", "true")            # not armed yet
    page.locator('[data-testid="arm-reset-dut"] input').check()
    expect(reset).not_to_have_attribute("aria-disabled", "true")
    reset.click()
    expect(by_id(page, "toast")).to_contain_text("DUT reset", timeout=T)
    assert engine.called("resets.reset") == [(BOARD_USB, "dut")]
    expect(page.locator('[data-console-tab="uart0"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator('[data-testid="arm-reset-dut"] input')).not_to_be_checked()   # disarmed


def test_negative_twin_reset_dut_unarmed_sends_nothing(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    page.locator('[data-action="reset_dut"]').click(force=True)
    expect(by_id(page, "reset-result")).to_contain_text("Nothing was run.")
    assert engine.called("resets.reset") == []


# --- R3: a watcher cannot drive anything on the Workbench ---------------------------------------------


def test_a_watcher_cannot_drive_anything_on_the_workbench(showcase):  # noqa: F811
    page = showcase.page(**APP)
    workbench(page, BOARD_LEASED)                         # alice@lab-pc-07 holds the lease
    held = "is for the lease holder only: alice@lab-pc-07 holds this board"
    wb.pick(page, "nanosoc", wait=True)
    expect(by_id(page, "reason-program")).to_have_text(f"Program {held}", timeout=T)
    expect(by_id(page, "reason-restore")).to_have_text(f"Restore baseline {held}")
    expect(by_id(page, "reason-reset_dut")).to_have_text(f"Reset DUT {held}")
    expect(by_id(page, "reason-up")).to_have_text(f"Debug {held}")
    expect(by_id(page, "reason-detect")).to_have_text(f"Debug {held}")
    expect(by_id(page, "reason-xvc_open")).to_have_text(f"XVC {held}", timeout=T)
    for action in ("program", "restore", "reset_dut", "up", "detect", "xvc_open"):
        button = page.locator(f'[data-action="{action}"]')
        expect(button).to_have_attribute("aria-disabled", "true")
        assert "primary" not in (button.get_attribute("class") or ""), action   # never "click me"
        button.click(force=True)                          # the interlock answers; nothing is sent
    # the consoles are read-only for a watcher, with the reason
    expect(by_id(page, "send-line")).to_be_disabled()
    expect(by_id(page, "console-readonly")).to_contain_text("alice@lab-pc-07 holds this board's hub lease")
    expect(page.locator('[data-testid="arm-reset-dut"] input')).to_be_disabled()
    eng = showcase.engine
    for call in ("deploy.deploy", "deploy.restore", "resets.reset", "debug.up", "debug.detect"):
        assert eng.called(call) == [], call
    rows = nav.activity(page, "err").locator('[data-testid="activity-table"] tbody tr')
    expect(rows.first).to_contain_text("refused, not run", timeout=T)
    assert not page.errors, page.errors


def test_negative_twin_the_lease_holder_drives_the_workbench(showcase):  # noqa: F811
    page = showcase.page(**APP)
    nav.open_board(page, BOARD_SPARE)
    page.locator('[data-attention="lease"] [data-action="lease_acquire"]').click()
    expect(nav.rail(page, BOARD_SPARE).locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)
    nav.tab(page, "workbench")
    wb.pick(page, "led", wait=True)
    expect(by_id(page, "reason-program")).to_contain_text("not armed", timeout=T)
    expect(by_id(page, "reason-reset_dut")).to_have_count(0)
    expect(by_id(page, "reason-up")).to_have_count(0)
    expect(page.locator('[data-action="up"]')).not_to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "send-line")).to_be_enabled()
    page.locator('[data-testid="arm-program"] input').check()
    page.locator('[data-action="program"]').click()
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "done", timeout=T)
    assert len(showcase.engine.called("deploy.deploy")) == 1


# --- the Harness console: only the lease holder types (G1b) -------------------------------------------


def _linux_behind_hub(engine, daemon, lease):
    engine._set_identity(BOARD_USB, harness_impl="linux")
    daemon.app.state.sim.behind_hub(BOARD_USB, lease=lease, holder="alice@lab-pc-07")


@pytest.mark.week_plan("consoles_api", sim=True)
def test_the_harness_console_is_read_only_unless_the_lease_is_yours(page_factory, engine, daemon):
    _linux_behind_hub(engine, daemon, "other")
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    tab = page.locator('[data-console-tab="shell"]')
    expect(tab).to_contain_text("Harness", timeout=T)
    tab.click()
    expect(by_id(page, "console-access")).to_contain_text("read-only")
    expect(by_id(page, "console-readonly")).to_contain_text("only the lease holder types on the Linux console")
    expect(by_id(page, "send-line")).to_be_disabled()
    before = list(engine.consoles.writes)
    page.locator('[data-action="send"]').click(force=True)
    expect(by_id(page, "console-result")).to_contain_text("Nothing was run.")
    assert engine.consoles.writes == before


@pytest.mark.week_plan("consoles_api", sim=True)
def test_negative_twin_the_lease_holder_types_on_the_harness_console(page_factory, engine, daemon):
    _linux_behind_hub(engine, daemon, "mine")
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    page.locator('[data-console-tab="shell"]').click()
    expect(by_id(page, "console-access")).to_contain_text("you can type", timeout=T)
    expect(by_id(page, "send-line")).to_be_enabled()
    expect(by_id(page, "console-readonly")).to_have_count(0)


# --- the rail: Debug (W14) ---------------------------------------------------------------------------


def test_debug_open_session_gives_the_gdb_line_and_where_openocd_runs(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    card = by_id(page, "debug-card")
    expect(card.locator('[data-testid="debug-where"]')).to_have_count(0)   # not said: not shown
    page.locator('[data-action="up"]').click()
    expect(by_id(page, "debug-state")).to_have_text("up", timeout=T)
    expect(by_id(page, "debug-attach")).to_contain_text(gdb_command(3343))
    expect(by_id(page, "debug-where")).to_have_attribute("data-where", "host")    # an older reply
    expect(by_id(page, "debug-where")).to_contain_text("on this PC")
    expect(page.locator('[data-testid^="debug-attach-"]')).to_have_count(0)      # one core
    page.locator('[data-action="down"]').click()
    expect(by_id(page, "debug-state")).to_have_text("down", timeout=T)
    expect(by_id(page, "debug-attach")).to_have_count(0)


def test_negative_twin_a_design_without_a_debug_port_says_so_and_opens_nothing(page_factory, engine):
    page = page_factory(**APP)
    workbench(page, BOARD_FIELDED)                        # greybox: no DAP
    page.locator('[data-action="up"]').click()
    expect(by_id(page, "debug-result")).to_contain_text("NOTHING_ON_TARGET", timeout=T)
    expect(by_id(page, "debug-state")).not_to_have_text("up")
    expect(by_id(page, "debug-attach")).to_have_count(0)


# --- the rail: Logic analysers, "Program nanosoc_ila..." (W15, W16) ------------------------------------


def _with_ila_design(monkeypatch):
    """The demo's overlay store plus nanosoc_ila, whose probes file travels with it."""
    from harness_manager import demo

    base = demo._overlays

    def overlays(static=demo.SHELL_FIELDED):
        out = base(static)
        return [*out, OverlayRef(name="nanosoc_ila", rm_id="0x0100000a", static_id=static,
                                 static_usercode="0x5f3a9c11", source="fielded/nanosoc_ila/manifest.json",
                                 size_bytes=498_304, ip_class="arm-aaa", ltx_sha256="ab" * 32)]

    monkeypatch.setattr(demo, "_overlays", overlays)


def _greybox_with_xvc(engine):
    engine._set_identity(BOARD_FIELDED, features=(*FIELDED_FEATURES, "xvc_dbgbr"),
                         rm_id="0x00000000", rm_name="greybox")


@pytest.mark.week_plan("xvc_api", sim=True)
def test_a_design_without_ilas_offers_to_program_nanosoc_ila(page_factory, engine, monkeypatch):
    _with_ila_design(monkeypatch)
    _greybox_with_xvc(engine)
    page = page_factory(**APP)
    workbench(page, BOARD_FIELDED)
    expect(by_id(page, "xvc-state")).to_have_text("no ILAs", timeout=T)
    offer = by_id(page, "xvc-no-ila")
    expect(offer).to_contain_text("greybox has no ILAs")
    button = page.locator('[data-action="program-ila"]')
    expect(button).to_have_text("Program nanosoc_ila...")
    button.click()
    expect(by_id(page, "selected-overlay")).to_have_text("nanosoc_ila")   # picked, not programmed
    expect(by_id(page, "toast")).to_contain_text("nanosoc_ila picked: tick Arm, then Program")
    expect(button).to_have_text("Picked: Arm, then Program")
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    expect(page.locator('[data-pf="ila"]')).to_contain_text("ILAs")
    assert engine.called("deploy.deploy") == []


@pytest.mark.week_plan("xvc_api", sim=True)
def test_negative_twin_no_ila_design_on_the_shell_offers_nothing(page_factory, engine):
    _greybox_with_xvc(engine)
    page = page_factory(**APP)
    workbench(page, BOARD_FIELDED)
    expect(by_id(page, "xvc-no-ila")).to_contain_text("no design on this shell carries a probes file", timeout=T)
    expect(page.locator('[data-action="program-ila"]')).to_have_count(0)


# --- the rail folds below 1280 px (W17) ----------------------------------------------------------------


def test_the_rail_folds_to_icons_below_1280_and_opens_a_flyout(page_factory):
    page = page_factory(width=1200, height=800)
    workbench(page, BOARD_USB)
    expect(by_id(page, "workbench")).to_have_attribute("data-folded", "true")
    expect(by_id(page, "debug-card")).to_have_count(0)
    page.locator('[data-fly="debug"]').click()
    expect(page.locator('.flyout [data-testid="debug-card"]')).to_be_visible()
    page.locator('[data-fly="ila"]').click()
    expect(page.locator('.flyout [data-testid="xvc-card"]')).to_be_visible()
    page.locator('[data-action="wb-unfold"]').click()
    expect(by_id(page, "workbench")).to_have_attribute("data-folded", "false")
    expect(page.locator('.wb-rail [data-testid="debug-card"]')).to_be_visible()


def test_negative_twin_at_1440_the_rail_is_open_and_fits_without_scrolling(page_factory):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    expect(by_id(page, "workbench")).to_have_attribute("data-folded", "false")
    expect(page.locator('.wb-rail [data-testid="debug-card"]')).to_be_visible()
    expect(page.locator('.wb-rail [data-testid="xvc-card"]')).to_be_visible()
    over = page.evaluate("""() => { const r = document.querySelector('.wb-rail');
      return r.scrollHeight - r.clientHeight; }""")
    assert over <= 1, f"the rail scrolls by {over} px at 1440x900"
    width = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert width <= 0, width


# --- Recent here ------------------------------------------------------------------------------------------


TALL = {"width": 1440, "height": 1100}      # Recent here shows in what the rail cards leave


def test_recent_here_lists_this_boards_events_and_opens_activity(page_factory):
    page = page_factory(**TALL)
    workbench(page, BOARD_USB)
    wb.pick(page, "led", wait=True)
    page.locator('[data-action="program"]').click(force=True)      # not armed: refused, logged
    recent = by_id(page, "wb-recent")
    expect(recent).to_contain_text("refused, not run", timeout=T)
    recent.locator('[data-action="wb-activity"]').click()
    expect(by_id(page, "activity-drawer")).to_be_visible(timeout=T)


def test_negative_twin_another_boards_events_are_not_recent_here(page_factory):
    page = page_factory(**TALL)
    workbench(page, BOARD_FIELDED)
    wb.pick(page, "led", wait=True)
    page.locator('[data-action="program"]').click(force=True)
    expect(by_id(page, "wb-recent")).to_contain_text("refused, not run", timeout=T)
    workbench(page, BOARD_USB)
    expect(by_id(page, "wb-recent")).not_to_contain_text("refused, not run")
