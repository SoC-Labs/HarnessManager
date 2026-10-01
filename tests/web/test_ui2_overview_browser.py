"""UI v2 round 3, lane UI2-OVERVIEW: the Overview (the Front panel as the largest element, the
identity strip with the Debug USB route, the readings, the full lease queue, "X waits for this
board", Recent activity) and the restyled Checks, over the REAL daemon and the showcase
(``DemoEngine(showcase=True)``). Each behaviour has its negative twin. Real data only: every
value asserted here is one the showcase serves.
"""

from __future__ import annotations

import time
import urllib.parse

import pytest

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


def by_id(page, name):
    return nav.by_id(page, name)


def hash_of(page) -> str:
    return urllib.parse.unquote(page.evaluate("location.hash"))


def overview(page, bid):
    nav.open_board(page, bid)
    nav.tab(page, "overview")
    expect(by_id(page, "overview")).to_be_visible(timeout=T)
    expect(by_id(page, "panel-card")).to_be_visible(timeout=T)


def area(page, selector):
    return page.eval_on_selector(selector, "el => { const r = el.getBoundingClientRect(); return r.width * r.height; }")


# --- one screen, the Front panel the largest and the only one -------------------------------------


@pytest.mark.parametrize("bid", [BOARD_LINUX, BOARD_V011, BOARD_SPARE])
def test_the_overview_fits_one_1440x900_screen_and_the_front_panel_is_the_largest(show: Showcase, bid):
    page = show.page(width=1440, height=900)
    overview(page, bid)
    expect(by_id(page, "ov-readings").locator(".ov-kpi")).to_have_count(6, timeout=T)
    page.wait_for_timeout(800)                                   # the reads that follow the open
    sizes = page.evaluate("""() => { const s = document.querySelector('.section-body');
        return [s.scrollHeight, s.clientHeight]; }""")
    assert sizes[0] <= sizes[1] + 1, f"the Overview scrolls at 1440x900: {sizes}"
    panel = area(page, '[data-testid="panel-card"]')
    others = page.evaluate("""() => [...document.querySelectorAll('.ov-grid > .ov-card:not(.ov-panel), .ov-sum, .ov-att')]
        .map((el) => { const r = el.getBoundingClientRect(); return r.width * r.height; })""")
    assert others and panel > max(others), (panel, others)
    assert page.locator('[data-testid="panel-card"]').count() == 1
    assert not page.errors, page.errors


def test_twin_below_1180_px_the_front_panel_goes_first_and_the_page_may_scroll(show: Showcase):
    page = show.page(width=1024, height=768)
    overview(page, BOARD_V011)
    top = lambda sel: page.eval_on_selector(sel, "el => el.getBoundingClientRect().top")  # noqa: E731
    assert top('[data-testid="panel-card"]') < top('[data-testid="tile-design"]')
    expect(page.locator('[data-testid="panel-card"] [data-testid="panel-mirror"]')).to_be_visible(timeout=T)
    width = page.eval_on_selector('[data-testid="panel-card"] [data-testid="panel-mirror"]', "el => el.getBoundingClientRect().width")
    assert width <= 562, width                                  # capped, not the page's width
    assert not page.errors, page.errors


def test_the_front_panel_lives_only_on_the_overview(show: Showcase):
    page = show.page()
    overview(page, BOARD_LINUX)
    expect(by_id(page, "panel-card")).to_have_count(1)
    for key in ("workbench", "build", "board"):
        nav.tab(page, key)
        page.wait_for_timeout(300)
        assert page.locator('[data-testid="panel-card"]').count() == 0, key
        assert page.locator('[data-testid="live-display"]').count() == 0, key
    nav.tab(page, "overview")                                    # the twin: back, it is there
    expect(by_id(page, "panel-card")).to_have_count(1, timeout=T)
    assert not page.errors, page.errors


# --- the Front panel: Live / Text, Identify in its head ---------------------------------------------


def test_live_is_the_boards_own_picture_fitted_to_the_card_and_text_is_one_click(show: Showcase):
    page = show.page()
    overview(page, BOARD_LINUX)
    card = by_id(page, "panel-card")
    expect(card).to_have_attribute("data-view", "live", timeout=T)
    live = card.locator('[data-testid="live-display"]')
    expect(live).to_have_attribute("data-fit", "yes")
    expect(live).to_have_attribute("data-live", "yes", timeout=T)
    frame = card.locator('[data-testid="live-frame"]').bounding_box()
    assert frame and frame["width"] > 380, frame                   # fitted, not 320 px
    assert abs(frame["width"] * 0.75 - frame["height"]) < 3, frame  # 4:3
    expect(card.locator('[data-testid="panel-identify"] [data-action="identify"]')).to_be_visible()
    card.locator('[data-action="panel-view-text"]').click()
    expect(card).to_have_attribute("data-view", "text")
    expect(card.locator('[data-testid="panel-mirror"]')).to_be_visible(timeout=T)
    expect(card.locator('[data-testid="live-display"]')).to_have_count(0)
    expect(card.locator('[data-testid="panel-headline"]')).to_have_attribute("data-state", "read")
    card.locator('[data-action="panel-view-live"]').click()
    expect(card.locator('[data-testid="live-display"]')).to_have_count(1, timeout=T)
    assert not page.errors, page.errors


def test_twin_bare_metal_is_text_only_and_says_why(show: Showcase):
    page = show.page()
    overview(page, BOARD_V011)
    card = by_id(page, "panel-card")
    expect(card).to_have_attribute("data-view", "text", timeout=T)
    live = card.locator('[data-action="panel-view-live"]')
    expect(live).to_be_disabled()
    assert "lcd_mirror" in (live.get_attribute("title") or "")
    expect(card.locator('[data-testid="panel-mirror"]')).to_be_visible(timeout=T)
    expect(card.locator('[data-testid="panel-headline"]')).to_have_attribute("data-state", "rebuilt", timeout=T)
    expect(card.locator('[data-testid="reason-identify"]')).to_contain_text("harness feature 'locate'")
    assert card.locator('[data-testid="live-display"]').count() == 0
    assert not page.errors, page.errors


# --- the identity strip: the Debug USB route (G2) and the uptime (G4) --------------------------------


def test_the_debug_usb_chip_says_the_services_route_and_links_to_connections(show: Showcase):
    page = show.page()
    overview(page, BOARD_LEASED)
    chip = by_id(page, "ov-usb")
    expect(chip).to_have_attribute("data-usb", "hub", timeout=T)
    expect(chip).to_have_text("to the hub")
    assert "mapstone-dev" in (chip.get_attribute("title") or "")   # mcc_route_reason
    chip.click()
    expect(by_id(page, "section-board")).to_be_visible(timeout=T)
    assert hash_of(page).endswith("/board/connections"), hash_of(page)
    assert not page.errors, page.errors


def test_twin_a_board_with_no_debug_usb_and_one_on_this_pc(show: Showcase):
    page = show.page()
    overview(page, BOARD_LINUX)
    expect(by_id(page, "ov-usb")).to_have_attribute("data-usb", "none", timeout=T)
    expect(by_id(page, "ov-usb")).to_have_text("none (Ethernet only)")       # UI2-POLISH: one vocabulary
    overview(page, BOARD_V011)
    expect(by_id(page, "ov-usb")).to_have_attribute("data-usb", "pc", timeout=T)
    expect(by_id(page, "ov-usb")).to_have_text("to this PC")
    assert not page.errors, page.errors


def test_the_readings_come_from_the_board_read_and_say_what_is_missing(show: Showcase):
    page = show.page()
    overview(page, BOARD_LINUX)
    expect(by_id(page, "ov-uptime")).to_contain_text("1 d", timeout=T)          # uptime_s
    loop = by_id(page, "ov-kpi-loop")
    expect(loop).to_contain_text("not reported")
    expect(loop).to_contain_text("harnessd has no superloop")
    expect(loop).to_contain_text("answers in")                                  # answer_ms
    temp = by_id(page, "ov-kpi-temp")
    expect(temp).to_contain_text("unavailable", timeout=T)
    expect(temp).to_contain_text("needs the Debug USB cable")
    expect(by_id(page, "ov-kpi-net")).to_contain_text("100/FD")                  # stats link/spd
    expect(by_id(page, "ov-kpi-swaps")).to_contain_text("7")                     # stats swap_n
    assert temp.locator('[data-testid="ov-spark"]').count() == 0
    assert not page.errors, page.errors


def test_twin_bare_metal_reports_its_loop_and_a_temperature_trend(show: Showcase):
    page = show.page()
    overview(page, BOARD_V011)
    temp = by_id(page, "ov-kpi-temp")
    expect(temp).to_contain_text("41.0", timeout=T)
    expect(temp.locator('[data-testid="ov-spark"]')).to_be_visible(timeout=T)   # G4 history
    assert int(temp.locator('[data-testid="ov-spark"]').get_attribute("data-points")) >= 2
    expect(by_id(page, "ov-kpi-loop")).to_contain_text("s worst")               # svc_max_us
    assert not page.errors, page.errors


# --- the Lease card: the full queue (G11), "X waits for this board" ------------------------------------


def test_the_lease_card_lists_the_full_queue_with_automation_apart(show: Showcase):
    page = show.page()
    overview(page, BOARD_LEASED)
    card = by_id(page, "ov-lease")
    expect(card.locator('[data-testid="tile-lease"]')).to_have_attribute("data-lease", "other", timeout=T)
    expect(card.locator('[data-testid="ov-lease-chip"]')).to_contain_text("Held by alice")
    q = card.locator('[data-testid="ov-queue"]')
    expect(q).to_have_attribute("data-people", "2", timeout=T)
    expect(q).to_have_attribute("data-bots", "1", timeout=T)
    mine = q.locator('.ov-q-row[data-tier="interactive"][data-position="1"]')
    expect(mine).to_have_attribute("data-mine", "yes")
    expect(mine).to_have_class("ov-q-row you")
    expect(mine).to_contain_text("1 h")                                          # want_s 3600
    bob = q.locator('.ov-q-row[data-tier="interactive"][data-position="2"]')
    expect(bob).to_contain_text("bob")
    expect(bob).to_contain_text("30 min")                                        # want_s 1800
    assert "a quick uart_echo check" in (bob.get_attribute("title") or "")       # the message
    bot = q.locator('.ov-q-row[data-tier="background"]')
    expect(bot).to_contain_text("hil-runner")
    expect(bot).to_contain_text("automation")
    expect(card.locator('[data-testid="ov-wait"]')).to_contain_text("You wait, #1 (1 h): you get it when alice releases")
    assert not page.errors, page.errors


def test_twin_a_free_board_has_nobody_waiting_and_a_desk_board_has_no_lease(show: Showcase):
    page = show.page()
    overview(page, BOARD_SPARE)
    card = by_id(page, "ov-lease")
    expect(card.locator('[data-testid="tile-lease"]')).to_have_attribute("data-lease", "free", timeout=T)
    expect(card.locator('[data-testid="ov-queue"]')).to_contain_text("nobody waiting")
    expect(card.locator('[data-testid="ov-queue"]')).to_have_attribute("data-bots", "0")
    assert card.locator('[data-testid="ov-wait"]').count() == 0
    overview(page, BOARD_LINUX)
    expect(by_id(page, "ov-access")).to_contain_text("No hub lease", timeout=T)
    expect(by_id(page, "ov-access")).to_contain_text("need a hub")
    assert page.locator('[data-testid="ov-lease"]').count() == 0
    assert not page.errors, page.errors


def _carol_waits(show: Showcase) -> None:
    """carol asks for the spare board (its hub, in memory): a queue entry and her note."""
    from harness_manager.demo_showcase import _iso
    from harness_manager.services.lease import RequestNote

    hub = show.engine._hub_spare
    now = time.time()
    with hub.mu:
        hub.queue.append(("carol@lab-pc-11", "carol"))
        hub.notes["r-carol"] = RequestNote(id="r-carol", by="carol@lab-pc-11", user="carol", host="lab-pc-11",
                                           message="the nightly bring-up", created_at=_iso(now - 30),
                                           deadline_at=_iso(now + 90), want_s=1800)


def _read_lease_until(page, bid, locator, seconds=30):
    """Read the lease again (the page's own week read) until ``locator`` shows: the service
    reuses a hub read for 10 s."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.evaluate("""async (bid) => { const w = await import(new URL("js/week.js", document.baseURI).href);
            await w.loadHub(bid); }""", bid)
        if locator.count():
            return
        page.wait_for_timeout(2000)


def test_someone_waiting_for_your_board_says_so_twice(show: Showcase):
    page = show.page()
    overview(page, BOARD_SPARE)
    page.locator('[data-testid="fact-hub"] [data-action="lease_acquire"]').click()
    expect(by_id(page, "ov-lease").locator('[data-testid="tile-lease"]')).to_have_attribute("data-lease", "here", timeout=T)
    # the twin first: yours, nobody waiting: no wait box, no attention row
    assert by_id(page, "ov-wait").count() == 0
    assert page.locator('[data-attention="waiting"]').count() == 0
    _carol_waits(show)
    wait = by_id(page, "ov-wait")
    _read_lease_until(page, BOARD_SPARE, wait)
    expect(wait).to_contain_text("carol waits for this board (30 min)", timeout=T)
    expect(wait.locator('[data-action="ov-release"]')).to_be_visible()
    row = page.locator('[data-attention="waiting"]')
    expect(row).to_contain_text("carol waits for this board (30 min)")
    expect(row.locator('[data-action="attention-waiting"]')).to_have_text("Close board…")
    assert "the nightly bring-up" in (row.get_attribute("title") or "")
    assert not page.errors, page.errors


# --- needs attention, recent activity -----------------------------------------------------------------


def test_an_identity_clash_needs_attention_and_its_fix_is_on_board_access(show: Showcase):
    page = show.page()
    overview(page, BOARD_LEASED)
    row = page.locator('[data-attention="identity"]')
    expect(row).to_contain_text("Identity clash with mps3-03", timeout=T)       # G10
    expect(by_id(page, "ov-mac")).to_have_text("02:00:00:4d:50:53")
    row.locator('[data-action="attention-identity"]').click()
    expect(by_id(page, "section-board")).to_be_visible(timeout=T)
    assert hash_of(page).endswith("/board/access"), hash_of(page)
    overview(page, BOARD_V011)                                                   # the twin
    page.wait_for_timeout(500)
    assert page.locator('[data-attention="identity"]').count() == 0
    assert not page.errors, page.errors


def test_recent_activity_lists_this_boards_rows_and_opens_the_drawer(show: Showcase):
    page = show.page()
    overview(page, BOARD_V011)
    recent = by_id(page, "ov-recent")
    page.evaluate("""async ([bid, other]) => { const m = await import(new URL("js/store.js", document.baseURI).href);
        m.log("warning", "lease", "a warning on this board", bid);
        m.log("info", "session", "a row on another board", other); }""", [BOARD_V011, BOARD_LINUX])
    row = recent.locator(".ov-log-row").first                        # the newest first
    expect(row).to_contain_text("a warning on this board", timeout=T)
    expect(row).to_have_attribute("data-level", "warning")
    assert recent.locator(".ov-log-row").count() <= 4
    expect(recent).not_to_contain_text("a row on another board")      # the twin: not the other board's
    row.click()
    drawer = by_id(page, "activity")
    expect(drawer).to_be_visible(timeout=T)
    expect(drawer).to_contain_text("a warning on this board")
    assert not page.errors, page.errors


def test_twin_a_board_this_page_never_logged_says_so(show: Showcase):
    page = show.page()
    overview(page, BOARD_V011)
    page.evaluate("""async () => { const m = await import(new URL("js/store.js", document.baseURI).href);
        m.S.log.length = 0; m.changed(); }""")
    expect(by_id(page, "ov-recent")).to_contain_text("Nothing on this board since this page opened", timeout=T)
    assert not page.errors, page.errors


# --- Checks: hub boards only, the lease rule (R3) --------------------------------------------------------


def test_checks_start_is_refused_with_the_reason_on_a_board_someone_else_holds(show: Showcase):
    page = show.page()
    nav.open_board(page, BOARD_LEASED)
    nav.tab(page, "checks")
    start = by_id(page, "checks-start")
    expect(start).to_be_disabled(timeout=T)
    expect(by_id(page, "checks-lease")).to_contain_text("Leased to alice@lab-pc-07")
    start.click(force=True)                                                # nothing starts
    page.wait_for_timeout(500)
    assert by_id(page, "checks-run").count() == 0
    expect(by_id(page, "checks-announce-card")).to_be_visible()             # folded into the card
    # the twin: the free board, the lease taken for the run: Start is on
    nav.open_board(page, BOARD_SPARE)
    nav.tab(page, "checks")
    by_id(page, "checks-take-lease").locator("input").check()
    expect(by_id(page, "checks-start")).to_be_enabled(timeout=T)
    assert not page.errors, page.errors
