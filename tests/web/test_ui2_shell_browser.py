"""UI v2 Phase 1 (lane UI2-SHELL): the five tabs, the routes and the old keys, the Activity
drawer and the last-error chip, the sidebar cards, the header, and the lease gate on every
drive button the new tabs show (risk R3). Over the REAL daemon and ``DemoEngine`` (and the
showcase's hub boards). Each behaviour has its twin.
"""

from __future__ import annotations

import re
import urllib.parse

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from tests.web import nav
from tests.web.test_demo_all_browser import Showcase, make_showcase

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = nav.T


@pytest.fixture
def show(browser, tmp_path, monkeypatch, request):
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def store(page, js: str, *args):
    """Run ``js`` (an async function body with ``m`` = js/store.js) in the page."""
    return page.evaluate(f"""async (args) => {{
        const m = await import(new URL("js/store.js", document.baseURI).href);
        {js}
    }}""", list(args))


def hash_of(page) -> str:
    return urllib.parse.unquote(page.evaluate("location.hash"))


# --- the tabs and the routes ------------------------------------------------------------------------


def test_five_tabs_and_each_old_key_lands_where_route_js_says(page_factory):
    page = page_factory(width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    tabs = page.evaluate("() => [...document.querySelectorAll('.section-tab')].map((t) => t.dataset.section)")
    assert tabs == ["overview", "workbench", "build", "board"], tabs      # no hub: no Checks
    # the page's map (route.js OLD_KEYS) is the tests' map (nav.OLD_KEYS)
    got = page.evaluate("""async (keys) => {
        const r = await import(new URL("js/route.js", document.baseURI).href);
        return Object.fromEntries(keys.map((k) => { const x = r.resolveRoute(k);
          return [k, [x.tab, x.sub || "", x.part || "", x.activity || ""]]; }));
    }""", list(nav.OLD_KEYS))
    for key, (tab, sub, part) in nav.OLD_KEYS.items():
        want_sub = sub or ("design" if key == "xdc" else "")
        assert got[key][:3] == [tab, want_sub, part], (key, got[key])
    assert got["activity"][3] == "all" and got["checks"][0] == "checks"
    # each old key through setSection: the tab, the sub-page in the address bar
    for key, want in (("sd", "board/versions"), ("power", "board/recover"), ("clocks", "board/readings"),
                      ("program", "workbench"), ("xdc", "build/design")):
        store(page, "m.setSection(args[0], args[1]);", BOARD_USB, key)
        expect(page.locator(f'[data-testid="section-{want.split("/")[0]}"]')).to_be_visible(timeout=T)
        assert hash_of(page) == f"#/{BOARD_USB}/{want}", (key, hash_of(page))
    # the twin: Checks on a board with no hub lands on the Overview
    store(page, "m.setSection(args[0], 'checks');", BOARD_USB)
    expect(nav.by_id(page, "section-overview")).to_be_visible(timeout=T)
    assert page.errors == []


def test_a_deep_link_opens_its_board_page_and_a_reload_stays_there(page_factory, daemon):
    page = page_factory(width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    url = f"{daemon.ui_url.split('#')[0]}#/{urllib.parse.quote(BOARD_USB, safe='')}/board/readings"
    page.goto(url)
    expect(nav.by_id(page, "board-page-readings")).to_be_visible(timeout=T)
    expect(page.locator('[data-board-page="readings"]')).to_have_attribute("aria-current", "page")
    nav.board_page(page, "about")
    page.reload()
    expect(nav.by_id(page, "board-page-about")).to_be_visible(timeout=T)
    assert hash_of(page).endswith("/board/about")
    # the twin: a link to a page that does not exist keeps the tab and the page it was on
    nav.tab(page, "overview")
    page.goto(f"{daemon.ui_url.split('#')[0]}#/{urllib.parse.quote(BOARD_USB, safe='')}/board/nope")
    expect(nav.by_id(page, "board-page-about")).to_be_visible(timeout=T)
    assert page.errors == []


def test_a_saved_0_1_0_tab_map_is_migrated(page_factory):
    page = page_factory(width=1440, height=900)
    page.wait_for_selector(".board-item", timeout=T)
    page.evaluate("""([bid]) => {
        sessionStorage.setItem("harness_manager.sections", JSON.stringify({[bid]: "clocks"}));
        sessionStorage.setItem("harness_manager.selected", bid);
        sessionStorage.removeItem("harness_manager.subs");
    }""", [BOARD_USB])
    page.goto(page.url.split("#")[0])                  # no route in the address bar
    nav.open_board(page, BOARD_USB)
    expect(nav.by_id(page, "board-page-readings")).to_be_visible(timeout=T)
    saved = page.evaluate("JSON.parse(sessionStorage.getItem('harness_manager.sections'))")
    assert saved[BOARD_USB] == "board"
    assert page.errors == []


def test_checks_is_a_hub_boards_tab(show: Showcase):
    page = show.page()
    nav.open_board(page, BOARD_LEASED)
    expect(page.locator('.section-tab[data-section="checks"]')).to_be_visible(timeout=T)
    nav.open_board(page, BOARD_LINUX)                   # the twin: a desk board
    expect(page.locator('.section-tab[data-section="board"]')).to_be_visible(timeout=T)
    expect(page.locator('.section-tab[data-section="checks"]')).to_have_count(0)
    assert not page.errors, page.errors


# --- the Activity drawer and the last-error chip -------------------------------------------------------


def test_a_refused_click_raises_the_chip_and_the_drawer_shows_it_and_links_back(page_factory):
    page = page_factory(width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    expect(nav.by_id(page, "last-problem-chip")).to_have_count(0)
    nav.section(page, "program")
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    page.locator('[data-action="program"]').click(force=True)          # not armed: refused
    chip = nav.by_id(page, "last-problem-chip")
    expect(chip).to_have_attribute("data-level", "error", timeout=T)
    expect(nav.by_id(page, "activity-badge")).to_have_text("1")
    chip.click()
    drawer = nav.by_id(page, "activity")
    expect(drawer.get_by_role("button", name="Errors", exact=True)).to_have_attribute("aria-pressed", "true")
    rows = drawer.locator('[data-testid="activity-table"] tbody tr')
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("not armed")
    expect(chip).to_have_count(0)                                       # seen: the chip goes
    expect(nav.by_id(page, "activity-badge")).to_have_count(0)
    rows.first.locator('[data-action="activity-go"]').click()           # back to where it happened
    expect(nav.by_id(page, "activity-drawer")).to_have_count(0)
    expect(nav.by_id(page, "section-workbench")).to_be_visible(timeout=T)
    # the twin: Escape closes it, and All shows the info rows too
    nav.activity(page, "all")
    assert drawer.locator('[data-testid="activity-table"] tbody tr').count() > 1
    page.keyboard.press("Escape")
    expect(nav.by_id(page, "activity-drawer")).to_have_count(0)
    assert page.errors == []


def test_a_toast_shows_and_goes(page_factory):
    page = page_factory(width=1440, height=900)
    page.wait_for_selector(".board-item", timeout=T)
    store(page, "m.toast('Copied', {ms: 600});")
    expect(nav.by_id(page, "toast")).to_have_text("Copied", timeout=T)
    expect(nav.by_id(page, "toast")).to_have_count(0, timeout=T)


# --- the sidebar cards and the header -------------------------------------------------------------------


def test_every_hub_board_has_a_lease_badge_open_or_not(show: Showcase):
    page = show.page()
    page.wait_for_selector(".board-item", timeout=T)
    # not opened yet: the showcase's hub boards say so (nothing read, nothing made up)
    spare = nav.rail(page, BOARD_SPARE).locator('[data-testid="rail-lease-row"]')
    expect(spare).to_have_attribute("data-lease", re.compile(r"unread|free"), timeout=T)
    nav.open_board(page, BOARD_SPARE)
    expect(spare.locator('[data-testid="rail-lease-badge"]')).to_have_text("Free", timeout=T)
    nav.open_board(page, BOARD_LEASED)
    held = nav.rail(page, BOARD_LEASED)
    expect(held.locator('[data-testid="rail-lease-badge"]')).to_contain_text("Held by alice", timeout=T)
    expect(held.locator('[data-testid="rail-lease-waiting"]')).to_contain_text("waiting")
    # the USB tag from the links: the hub's MCC; the twin, a desk board with no USB link
    expect(held.locator('[data-testid="rail-usb"]')).to_have_attribute("data-usb", "hub")
    expect(nav.rail(page, BOARD_LINUX).locator('[data-testid="rail-usb"]')).to_have_attribute("data-usb", "none")
    expect(nav.rail(page, BOARD_V011).locator('[data-testid="rail-usb"]')).to_have_attribute("data-usb", "pc")
    # desk boards: no lease badge
    for bid in (BOARD_LINUX, BOARD_V011):
        assert nav.rail(page, bid).locator('[data-testid="rail-lease-row"]').count() == 0
    assert not page.errors, page.errors


def test_the_header_keeps_its_ids_and_says_the_facts(page_factory):
    page = page_factory(width=1440, height=900)
    nav.open_board(page, BOARD_FIELDED)
    expect(nav.by_id(page, "fact-shell")).to_have_text(re.compile(r"0x[0-9A-F]{8}"))
    expect(nav.by_id(page, "fact-design")).to_have_attribute("data-check", "unchecked")
    expect(nav.by_id(page, "build-chip")).to_have_attribute("data-level", "unk")   # not OK: kept
    expect(nav.by_id(page, "fact-usb")).to_have_attribute("data-usb", "none")
    expect(nav.by_id(page, "health-chip")).to_be_visible()
    assert page.errors == []


def test_programming_shows_the_mini_bars_then_verified(page_factory, engine):
    engine.speed = 2.0                     # a deploy long enough to see
    page = page_factory(width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    nav.section(page, "program")
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    page.locator('[data-testid="arm-program"] input').check()
    page.locator('[data-action="program"]').click()
    expect(nav.by_id(page, "design-progress")).to_be_visible(timeout=T)
    expect(nav.rail(page, BOARD_USB).locator('[data-testid="rail-progress"]')).to_be_visible()
    expect(nav.by_id(page, "workbench-tab-badge")).to_be_visible()
    expect(nav.by_id(page, "design-verified")).to_contain_text(re.compile(r"verified \d\d:\d\d"), timeout=T)
    expect(nav.by_id(page, "design-progress")).to_have_count(0)
    expect(nav.rail(page, BOARD_USB).locator('[data-testid="rail-progress"]')).to_have_count(0)
    assert page.errors == []


# --- R3: every drive button the new tabs show keeps its lease gate ------------------------------------


DRIVES = {
    "program": ["program", "restore"],
    "power": ["reset_dut", "reboot"],
}


def test_a_watcher_cannot_drive_from_the_workbench_or_board_recover(show: Showcase):
    page = show.page()
    nav.open_board(page, BOARD_LEASED)                  # alice holds the lease
    nav.section(page, "program")
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    for key in DRIVES["program"]:
        expect(nav.by_id(page, f"reason-{key}")).to_contain_text("for the lease holder only", timeout=T)
        expect(page.locator(f'[data-action="{key}"]')).to_have_attribute("aria-disabled", "true")
    nav.section(page, "debug")
    expect(nav.panel(page, "debug").locator('[data-action="up"]').first).to_have_attribute("aria-disabled", "true")
    nav.section(page, "power")
    for key in DRIVES["power"]:
        btn = nav.panel(page, "power").locator(f'[data-action="{key}"]').first
        expect(btn).to_have_attribute("aria-disabled", "true", timeout=T)
    assert any("for the lease holder only" in t for t in
               nav.panel(page, "power").locator(".reason").all_inner_texts())
    # the twin: the free board, once its lease is ours, drives
    nav.open_board(page, BOARD_SPARE)
    page.locator('[data-attention="lease"] [data-action="lease_acquire"]').click()
    expect(nav.rail(page, BOARD_SPARE).locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)
    nav.section(page, "program")
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    expect(nav.by_id(page, "reason-program")).to_contain_text("not armed", timeout=T)
    assert not page.errors, page.errors
