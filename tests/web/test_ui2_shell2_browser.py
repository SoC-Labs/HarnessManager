"""UI v2 Phase 2, lane UI2-SHELL2: the preview of a board that is not open (Open and take the
lease, Request board without opening it, the queue), the header's "N waiting" chip and its
popover, the Close / Request / Add / Settings / Help dialogs, over the REAL daemon and
``DemoEngine(showcase=True)`` (``app --demo``): mps3-02 behind the hub, held by alice, your request
#1 and bob's #2 queued, an automation run in the background tier; mps3-03 behind the same hub,
free; mps3-lx on this network. Each behaviour has its negative twin.

The preview is app.js's mount (CCR SHELL2-1); the dialogs and the chip are this lane's own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE
from tests.web import nav
from tests.web.test_demo_all_browser import Showcase, make_showcase

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = nav.T
APP = {"width": 1440, "height": 900}
ALICE = "alice@lab-pc-07"


@pytest.fixture
def show(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def preview(page: Any, bid: str) -> Any:
    """Select a board that is not open: its preview."""
    nav.rail(page, bid).click()
    card = by_id(page, "preview")
    expect(card).to_be_visible(timeout=T)
    return card


def open_here(page: Any, bid: str) -> None:
    """Open ``bid`` with the plain Open (no lease) and stay on the tab it lands on."""
    nav.close_activity(page)
    nav.rail(page, bid).click()
    page.wait_for_selector(f'main[data-board="{bid}"], [data-action="open"]', timeout=T)
    if page.locator(f'main[data-board="{bid}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    page.wait_for_selector(f'main[data-board="{bid}"] [data-testid="fact-shell"]'
                           ':not(:has-text("unknown"))', timeout=T)


def errors(page: Any) -> list[str]:
    """The page's errors, but a console socket of a board just closed retrying once: the
    Workbench (where a board opens) had it open; its last reconnect meets the closed session's 404
    (consoles.js, the WORKBENCH lane's; reported)."""
    return [e for e in page.errors if not ("WebSocket connection" in e and "/consoles/" in e)]


def opened(show: Showcase) -> list[str]:
    return list(show.engine.open_boards())


def calls(page: Any, sink: list[tuple[str, str, str]]) -> None:
    """Record every API call the page makes: (method, path, body)."""
    page.on("request", lambda r: sink.append((r.method, r.url.split("/api/v1")[-1], r.post_data or ""))
            if "/api/v1/" in r.url else None)


# --- the rail: one group per hub, then This network (S13) ---------------------------------------------


def test_the_rail_groups_hub_boards_under_their_hub_then_this_network(show: Showcase):
    page = show.page(**APP)
    hub = page.locator('[data-testid="rail-group-hub:mapstone-dev.ecs.soton.ac.uk"]')
    expect(hub.locator(".rail-card")).to_have_count(2, timeout=T)
    assert sorted(hub.locator(".rail-card").evaluate_all("els => els.map(e => e.dataset.board)")) == sorted(
        [BOARD_LEASED, BOARD_SPARE])
    expect(by_id(page, "rail-group-title-hub")).to_have_text("mapstone-dev")
    expect(by_id(page, "rail-group-title-net")).to_have_text("This network")
    net = by_id(page, "rail-group-rest").locator(".rail-card")
    expect(net).to_have_count(2)
    # Alt+Down moves a board inside its own group only, and says where
    first = hub.locator(".board-item").first
    moved = first.get_attribute("data-board")
    first.focus()
    page.keyboard.press("Alt+ArrowDown")
    expect(hub.locator(".rail-card").last).to_have_attribute("data-board", moved, timeout=T)
    expect(by_id(page, "rail-announce")).to_contain_text("position 2 of 2 in mapstone-dev", timeout=T)
    expect(net).to_have_count(2)
    assert not errors(page), page.errors


def test_negative_twin_boards_with_no_hub_have_one_group_and_no_titles(page_factory):
    page = page_factory(**APP)
    page.wait_for_selector(".board-item", timeout=T)
    expect(by_id(page, "rail-group-rest").locator(".rail-card")).to_have_count(3, timeout=T)
    assert by_id(page, "rail-group-title-hub").count() == 0
    assert by_id(page, "rail-group-title-net").count() == 0
    assert not errors(page), page.errors


# --- the preview ------------------------------------------------------------------------------------


def test_the_preview_of_a_held_board_shows_the_queue_and_offers_request_not_take(show: Showcase):
    page = show.page(**APP)
    card = preview(page, BOARD_LEASED)
    lease = by_id(page, "preview-lease")
    expect(lease).to_have_attribute("data-lease", "other", timeout=T)
    expect(by_id(page, "preview-lease-chip")).to_contain_text(f"Held by {ALICE}")
    # the showcase's own request is in the queue: #1, marked as yours; bob after; automation apart
    expect(by_id(page, "preview-requested")).to_have_text("Requested · #1", timeout=T)
    q = by_id(page, "preview-queue")
    expect(q.locator('.lq-row[data-you="true"]')).to_contain_text("You")
    expect(q.locator('.lq-row[data-tier="interactive"]')).to_have_count(2)
    expect(q.locator('.lq-row[data-tier="interactive"]').nth(1)).to_contain_text("bob@lab-pc-03")
    expect(q.locator('.lq-row[data-tier="interactive"]').nth(1)).to_contain_text("wants 30 min")
    expect(by_id(page, "preview-queue-bg").locator(".lq-row")).to_have_count(1)
    expect(by_id(page, "preview-waiting")).to_have_text("3 waiting")
    acts = by_id(page, "preview-actions")
    expect(acts.locator('[data-action="preview-cancel-request"]')).to_be_visible()
    expect(acts.locator('[data-action="open"]')).to_have_text("Open to watch")
    assert acts.locator('[data-action="open-take"]').count() == 0      # held: never "take"
    expect(by_id(page, "preview-note")).to_contain_text("You are #1 in the hub's queue")
    assert opened(show) == [] and not errors(page), page.errors
    # the twin: the free board offers Open and take the lease, and no request
    preview(page, BOARD_SPARE)
    expect(by_id(page, "preview-lease")).to_have_attribute("data-lease", "free", timeout=T)
    expect(by_id(page, "preview-waiting")).to_have_text("nobody waiting")
    expect(acts.locator('[data-action="open-take"]')).to_have_text("Open and take the lease")
    assert acts.locator('[data-action="preview-request"]').count() == 0
    # and a board with no hub has no lease row: Open board
    preview(page, BOARD_LINUX)
    expect(acts.locator('[data-action="open"]')).to_have_text("Open board")
    assert by_id(page, "preview-lease").count() == 0
    assert card.locator('[data-action="open-take"]').count() == 0
    assert opened(show) == [] and not errors(page), page.errors


def test_request_board_without_opening_it_sends_how_long_and_the_message(show: Showcase):
    page = show.page(**APP)
    seen: list[tuple[str, str, str]] = []
    calls(page, seen)
    preview(page, BOARD_LEASED)
    acts = by_id(page, "preview-actions")
    # leave the queue first (the showcase queued you): DELETE .../lease/queue, board not opened
    acts.locator('[data-action="preview-cancel-request"]').click()
    expect(acts.locator('[data-action="preview-request"]')).to_be_visible(timeout=T)
    assert any(m == "DELETE" and p.endswith("/lease/queue") for m, p, _b in seen), seen
    expect(by_id(page, "preview-waiting")).to_have_text("2 waiting", timeout=T)
    # Request board: the dialog says where you join (after bob) and that automation waits behind
    acts.locator('[data-action="preview-request"]').click()
    form = by_id(page, "lease-request-form")
    expect(form).to_be_visible()
    expect(page.locator("#lease-message")).to_be_focused()
    expect(by_id(page, "request-position")).to_have_text("#2", timeout=T)
    expect(by_id(page, "request-what")).to_contain_text("after bob (30 min)")
    expect(by_id(page, "request-what")).to_contain_text("One automation run waits behind you")
    expect(by_id(page, "request-queue").locator(".lq-row")).to_have_count(2)
    page.locator("#lease-message").fill("uart bring-up")
    by_id(page, "request-want").locator("button", has_text="2 h").click()
    with page.expect_request(lambda r: r.url.endswith("/lease/request") and r.method == "POST", timeout=T) as req:
        form.locator('[data-action="lease_request"]').click()
    assert json.loads(req.value.post_data) == {"message": "uart bring-up", "want_s": 7200}
    expect(form).to_have_count(0)
    expect(by_id(page, "preview-requested")).to_have_text("Requested · #2", timeout=T)
    expect(acts.locator('[data-action="preview-cancel-request"]')).to_be_visible()
    assert opened(show) == []                                    # requesting opens nothing
    # the twin: Cancel (and Escape) in the dialog send nothing
    n = len([1 for m, p, _b in seen if p.endswith("/lease/request")])
    preview(page, BOARD_LEASED)
    acts.locator('[data-action="preview-cancel-request"]').click()
    expect(acts.locator('[data-action="preview-request"]')).to_be_visible(timeout=T)
    acts.locator('[data-action="preview-request"]').click()
    page.locator('[data-action="lease_request_cancel"]').click()
    expect(form).to_have_count(0)
    acts.locator('[data-action="preview-request"]').click()
    page.keyboard.press("Escape")
    expect(form).to_have_count(0)
    page.wait_for_timeout(300)
    assert len([1 for m, p, _b in seen if p.endswith("/lease/request")]) == n
    assert not errors(page), page.errors


def test_open_and_take_the_lease_lands_on_the_workbench_holding_it(show: Showcase):
    page = show.page(**APP)
    preview(page, BOARD_SPARE)
    by_id(page, "preview-actions").locator('[data-action="open-take"]').click()
    page.wait_for_selector(f'main[data-board="{BOARD_SPARE}"] [data-testid="fact-shell"]', timeout=T)
    expect(by_id(page, "section-workbench")).to_be_visible(timeout=T)      # general.open_on
    expect(by_id(page, "lease-chip")).to_contain_text("lease yours", timeout=T)
    assert not errors(page), page.errors


def test_negative_twin_open_to_watch_takes_no_lease_and_open_on_overview_lands_there(show: Showcase):
    page = show.page(**APP)
    # Settings: Open a board on -> Overview
    page.locator('[data-action="settings"]').click()
    row = page.locator('[data-testid="setting-row"][data-key="general.open_on"]')
    expect(row).to_be_visible(timeout=T)
    row.locator('button[data-value="overview"]').click()
    expect(row.locator('button[data-value="overview"]')).to_have_attribute("aria-pressed", "true", timeout=T)
    page.locator('[data-action="settings-close"]').click()
    preview(page, BOARD_SPARE)
    by_id(page, "preview-actions").locator('[data-action="open"]').click()
    page.wait_for_selector(f'main[data-board="{BOARD_SPARE}"] [data-testid="fact-shell"]', timeout=T)
    expect(by_id(page, "section-overview")).to_be_visible(timeout=T)
    expect(by_id(page, "lease-chip")).to_have_text("no lease", timeout=T)
    assert not errors(page), page.errors


def test_a_service_without_open_on_leaves_the_tab_as_before(show: Showcase):
    # G12 may be missing (an older service): no "Open a board on" row, and a board opens where
    # the page would show it anyway (the Overview), never an error
    page = show.page(**APP)
    page.route(re.compile(r"/api/v1/settings\?key=general\.open_on$"),
               lambda route: route.fulfill(status=200, json={"ok": True, "rows": []}))
    page.reload()
    page.wait_for_selector(".board-item", timeout=T)
    preview(page, BOARD_LINUX)
    by_id(page, "preview-actions").locator('[data-action="open"]').click()
    page.wait_for_selector(f'main[data-board="{BOARD_LINUX}"] [data-testid="fact-shell"]', timeout=T)
    expect(by_id(page, "section-overview")).to_be_visible(timeout=T)
    assert not errors(page), page.errors


# --- the header's "N waiting" and the queue popover ------------------------------------------------------


def test_n_waiting_opens_the_hub_queue_and_escape_closes_it(show: Showcase):
    page = show.page(**APP)
    open_here(page, BOARD_LEASED)
    chip = by_id(page, "lease-queue-chip")
    expect(chip).to_have_text("3 waiting", timeout=T)                # you, bob, the automation run
    chip.click()
    pop = by_id(page, "lease-queue-pop")
    expect(pop).to_be_visible()
    expect(chip).to_have_attribute("aria-expanded", "true")
    expect(by_id(page, "lease-queue-holder")).to_contain_text(f"{ALICE} holds it")
    expect(pop.locator('.lq-row[data-you="true"]')).to_contain_text("wants 1 h")
    expect(pop.locator(".lq-row").nth(1)).to_contain_text("“a quick uart_echo check”")
    expect(pop.locator(".lq-bg-head")).to_contain_text("Automation")
    expect(pop.locator(".lq-foot")).to_contain_text("You are #1: alice sees your request.")
    expect(pop.locator('[data-action="lease_leave_pop"]')).to_be_visible()
    page.keyboard.press("Escape")
    expect(pop).to_have_count(0)
    expect(chip).to_be_focused()
    chip.click()
    page.locator('[data-testid="board-header"] h1').click()          # a click outside closes it
    expect(pop).to_have_count(0)
    # the twin: a board nobody waits for has no chip
    open_here(page, BOARD_SPARE)
    expect(by_id(page, "lease-chip")).to_be_visible(timeout=T)
    page.wait_for_timeout(500)
    assert by_id(page, "lease-queue-chip").count() == 0
    assert not errors(page), page.errors


# --- Close board -------------------------------------------------------------------------------------------


def take_spare(page: Any) -> None:
    preview(page, BOARD_SPARE)
    by_id(page, "preview-actions").locator('[data-action="open-take"]').click()
    expect(by_id(page, "lease-chip")).to_contain_text("lease yours", timeout=T)


def close_dialog(page: Any) -> Any:
    page.locator('[data-testid="board-header"] [data-action="close-board"]').click()
    d = by_id(page, "close-confirm")
    expect(d).to_be_visible()
    return d


def test_close_defaults_to_restore_release_and_close_when_yours_and_loaded(show: Showcase):
    page = show.page(**APP)
    seen: list[tuple[str, str, str]] = []
    calls(page, seen)
    take_spare(page)
    d = close_dialog(page)
    expect(by_id(page, "close-title")).to_have_text("Close mps3-03")
    expect(d).to_have_attribute("data-default", "restore")
    expect(d.locator('[data-choice="restore"] input')).to_be_checked()
    expect(d.locator('[data-choice="restore"]')).to_contain_text("Loads greybox in place of nanosoc")
    expect(d.locator('[data-choice="release"]')).to_contain_text("Leaves nanosoc loaded")
    expect(by_id(page, "close-keep-what")).to_contain_text("nothing renews it while the board is closed")
    expect(d.locator('[data-action="close_confirm"]')).to_have_text("Restore baseline, release and close")
    expect(d.locator('[data-action="close_cancel"]')).to_be_focused()
    d.locator('[data-action="close_confirm"]').click()
    expect(page.locator('[data-action="open-take"]')).to_be_visible(timeout=30_000)   # the preview
    order = [(m, p) for m, p, _b in seen if (m == "POST" and p.endswith("/restore")) or m == "DELETE"]
    assert [m for m, _p in order][:2] == ["POST", "DELETE"], order
    assert order[1][1].endswith("?release=true"), order
    expect(by_id(page, "preview-lease")).to_have_attribute("data-lease", "free", timeout=T)
    assert opened(show) == [] and not errors(page), page.errors


def test_negative_twin_release_and_close_leaves_the_design_and_cancel_closes_nothing(show: Showcase):
    page = show.page(**APP)
    seen: list[tuple[str, str, str]] = []
    calls(page, seen)
    take_spare(page)
    d = close_dialog(page)
    d.locator('[data-action="close_cancel"]').click()
    expect(d).to_have_count(0)
    d = close_dialog(page)
    page.keyboard.press("Escape")
    expect(d).to_have_count(0)
    assert opened(show) == [BOARD_SPARE]
    d = close_dialog(page)
    d.locator('[data-choice="release"]').click()
    expect(d.locator('[data-action="close_confirm"]')).to_have_text("Release and close")
    d.locator('[data-action="close_confirm"]').click()
    expect(page.locator('[data-action="open-take"]')).to_be_visible(timeout=T)
    assert not any(m == "POST" and p.endswith("/restore") for m, p, _b in seen)      # no restore
    assert any(m == "DELETE" and p.endswith("?release=true") for m, p, _b in seen)
    assert not errors(page), page.errors


def test_restore_on_a_board_someone_else_holds_is_refused_with_the_reason(show: Showcase):
    # R3: restoring drives the board; alice holds mps3-02, so Close offers it with why, and
    # closing sends no restore
    page = show.page(**APP)
    seen: list[tuple[str, str, str]] = []
    calls(page, seen)
    open_here(page, BOARD_LEASED)
    d = close_dialog(page)
    expect(d).to_have_attribute("data-default", "close")
    expect(d.locator('[data-choice="close"]')).to_contain_text(f"The lease stays with {ALICE}")
    restore = d.locator('[data-choice="restoreclose"]')
    expect(restore.locator("input")).to_be_disabled()
    expect(restore).to_contain_text(f"Not now: Restore baseline is for the lease holder only: {ALICE} holds this board")
    d.locator('[data-action="close_confirm"]').click()
    expect(page.locator('[data-action="preview-cancel-request"], [data-action="preview-request"]')).to_be_visible(timeout=T)
    assert not any(p.endswith("/restore") for _m, p, _b in seen), seen
    assert not errors(page), page.errors


def test_a_board_with_no_hub_closes_by_default_and_can_restore_first(show: Showcase):
    page = show.page(**APP)
    open_here(page, BOARD_LINUX)
    d = close_dialog(page)
    expect(d).to_have_attribute("data-default", "close")
    expect(d.locator('[data-choice="close"]')).to_contain_text("This board has no hub lease")
    expect(d.locator('[data-choice="restoreclose"] input')).to_be_enabled()
    d.locator('[data-action="close_confirm"]').click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    assert opened(show) == [] and not errors(page), page.errors


# --- Add a board -------------------------------------------------------------------------------------------


def test_add_by_address_tests_first_then_adds_to_the_rail(show: Showcase):
    page = show.page(**APP)
    page.locator('[aria-label="Add a board by address"]').click()
    dlg = by_id(page, "add-board")
    expect(dlg).to_be_visible()
    expect(page.locator('[aria-label="Board address"]')).to_be_focused(timeout=T)
    page.locator('[aria-label="Board address"]').fill("192.168.10.104")
    dlg.locator('[data-action="add-test"]').click()
    result = by_id(page, "add-test-result")
    expect(result).to_contain_text("Answered in", timeout=T)
    expect(result).to_contain_text("mps3-lx")
    with page.expect_request(lambda r: r.url.endswith("/api/v1/probe") and r.method == "POST", timeout=T) as req:
        dlg.locator('.rail-add button[type="submit"]').click()
    assert json.loads(req.value.post_data) == {"hosts": ["192.168.10.104"], "scan_usb": False}
    expect(dlg).to_have_count(0)
    expect(nav.rail(page, BOARD_LINUX)).to_have_attribute("aria-current", "true", timeout=T)
    assert not errors(page), page.errors


def test_negative_twin_an_address_nothing_answers_at_says_so_and_adds_nothing(show: Showcase):
    page = show.page(**APP)
    before = page.locator(".board-item").count()
    page.locator('[aria-label="Add a board by address"]').click()
    dlg = by_id(page, "add-board")
    page.locator('[aria-label="Board address"]').fill("192.168.99.99")
    dlg.locator('[data-action="add-test"]').click()
    expect(by_id(page, "add-test-result")).to_contain_text("Nothing answered at 192.168.99.99", timeout=T)
    dlg.locator('[data-action="add-cancel"]').click()
    expect(dlg).to_have_count(0)
    assert page.locator(".board-item").count() == before
    assert not errors(page), page.errors


def test_add_from_a_hub_lists_its_targets_with_their_leases(show: Showcase):
    page = show.page(**APP)
    page.locator('[aria-label="Add a board by address"]').click()
    dlg = by_id(page, "add-board")
    dlg.locator(".seg button", has_text="From a hub").click()
    expect(by_id(page, "add-hub-read")).to_contain_text("2 targets", timeout=T)
    rows = by_id(page, "add-hub-targets").locator("tr[data-target]")
    expect(rows).to_have_count(2)
    expect(dlg.locator('tr[data-target="mps3_02_pl"]')).to_contain_text(f"Held by {ALICE}")
    expect(dlg.locator('tr[data-target="mps3_03_pl"]')).to_contain_text("Free")
    # both are listed already: the row says so and selects it, never adds it twice
    expect(dlg.locator('tr[data-target="mps3_03_pl"] [data-action="add-target-select"]')).to_contain_text("in the list as mps3-03")
    assert dlg.locator('[data-action="add-target"]').count() == 0
    dlg.locator('tr[data-target="mps3_03_pl"] [data-action="add-target-select"]').click()
    expect(dlg).to_have_count(0)
    expect(nav.rail(page, BOARD_SPARE)).to_have_attribute("aria-current", "true", timeout=T)
    assert not errors(page), page.errors


# --- Settings and Help -------------------------------------------------------------------------------------


def test_settings_opens_on_general_with_open_a_board_on_first_and_hides_unread_rows(show: Showcase):
    page = show.page(**APP)
    page.locator('[data-action="settings"]').click()
    pane = by_id(page, "settings-pane")
    expect(pane).to_have_attribute("data-settings-section", "general", timeout=T)
    first = pane.locator('[data-testid="setting-row"]').first
    expect(first).to_have_attribute("data-key", "general.open_on", timeout=T)
    expect(first).to_contain_text("Open a board on")
    expect(first.locator("button")).to_have_text(["Workbench", "Overview"])
    expect(by_id(page, "theme-note")).to_contain_text("Theme is in the sidebar's foot")
    assert pane.locator('[data-key="general.theme"]').count() == 0
    assert pane.locator('[data-key="general.window_size"]').count() == 0          # nothing reads it
    page.locator('[data-testid="settings-nav"] [data-settings-section="consoles"]').click()
    expect(page.locator('[data-testid="setting-row"][data-key="consoles.line_ending"]')).to_be_visible(timeout=T)
    assert page.locator('[data-testid="setting-row"][data-key="consoles.scrollback"]').count() == 0
    # the twin: Show developer settings shows them (Advanced)
    page.locator('[data-testid="settings-nav"] [data-settings-section="advanced"]').click()
    page.locator('[data-action="show-dev"]').check()
    page.locator('[data-testid="settings-nav"] [data-settings-section="consoles"]').click()
    expect(page.locator('[data-testid="setting-row"][data-key="consoles.scrollback"]')).to_be_visible(timeout=T)
    # it opens on General again, whatever section was last used
    page.locator('[data-action="settings-close"]').click()
    page.locator('[data-action="settings"]').click()
    expect(pane).to_have_attribute("data-settings-section", "general", timeout=T)
    # Boards > Add a board… is the one Add dialog (Settings closes: one dialog at a time)
    page.locator('[data-testid="settings-nav"] [data-settings-section="boards"]').click()
    page.locator('[data-action="board-add-dialog"]').click()
    expect(by_id(page, "add-board")).to_be_visible(timeout=T)
    expect(by_id(page, "settings")).to_have_count(0)
    assert not errors(page), page.errors


def test_help_opens_on_the_page_you_are_on_and_names_the_cli_topics(show: Showcase):
    page = show.page(**APP)
    page.locator('[data-action="help"]').click()
    help_ = by_id(page, "help")
    expect(by_id(page, "help-pane")).to_have_attribute("data-help", "start", timeout=T)   # no board open
    page.keyboard.press("Escape")
    expect(help_).to_have_count(0)
    open_here(page, BOARD_LINUX)
    expect(by_id(page, "section-workbench")).to_be_visible(timeout=T)
    page.locator('[data-action="help"]').click()
    expect(by_id(page, "help-pane")).to_have_attribute("data-help", "workbench", timeout=T)
    expect(by_id(page, "help-text")).to_contain_text("Reset DUT is in its toolbar")
    topic = help_.locator('[data-help-cli="Program"]')
    expect(topic).to_be_visible(timeout=T)
    topic.click()
    expect(by_id(page, "help-pane")).to_have_attribute("data-help", "cli:Program")
    expect(help_.locator(".modal-text")).to_contain_text(re.compile(r"program", re.I))
    # every CLI topic is listed too, under Command line
    expect(help_.locator('.help-nav [data-help^="cli:"]')).to_have_count(19)
    assert not errors(page), page.errors
