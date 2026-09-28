"""Lane LOCATE in the browser: Identify on each sidebar board card and on the Board tile.

docs/design/BOARD_LOCATE.md §3 (the board side: the Linux lead's ``locate``, rc2_v7/v7n). Clicks only. The boards' panels are ``PanelSim``
(tests/fakes/p1_mock_panel.py): the T14 mock serves it, and ``PanelSim.attach`` gives the
REAL daemon's demo sessions the same adapter, so each test marked ``mock_too`` runs over both
servers. ``PanelSim.locates`` counts every locate a board was sent and ``calls`` every
front-panel request, whoever made it. The showcase demo (``app --demo``) has its own test at
the end. Each behaviour has its negative twin.
"""

from __future__ import annotations

import re
import time

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_HELD, BOARD_USB
from tests.fakes.clcd_panel_shell import PANEL_FEATURES
from tests.fakes.p1_mock_panel import PanelSim

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]
T = 10_000
APP = {"width": 1440, "height": 900}
BOARD = BOARD_FIELDED
LOCATE_WHY = "needs harness feature 'locate' (Linux harness)"


@pytest.fixture
def psim(daemon, engine):
    sim = getattr(daemon.app.state, "panel", None)
    if sim is None:
        sim = PanelSim(engine)
        sim.attach(engine)
    return sim


def linux(engine, bid=BOARD):
    """The board runs the Linux harness with ``locate`` (before the page probes it)."""
    feats = [f for f in engine._board(bid).identity.features if f not in PANEL_FEATURES]
    engine.set_features(bid, [*feats, *PANEL_FEATURES])


def rail(page, bid=BOARD):
    return page.locator(f'[data-testid="rail-locate"][data-board="{bid}"]')


def wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def loaded(page):
    page.wait_for_selector('[data-testid="rail-locate"]', timeout=T)


# --- enabled by the feature -------------------------------------------------------------------


@pytest.mark.mock_too
def test_the_rail_button_is_enabled_by_the_locate_feature(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    button = rail(page)
    expect(button).to_have_attribute("data-state", "idle", timeout=T)
    expect(button).not_to_have_attribute("aria-disabled", "true")
    expect(button).to_have_attribute("title", re.compile(r"blink this board's panel for 5 s"))
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_a_board_without_locate_is_disabled_with_the_reason(page_factory, daemon,
                                                                          engine, psim):
    linux(engine)                                  # BOARD has it; the others are v0.11
    page = page_factory(**APP)
    loaded(page)
    for bid in (BOARD_USB, BOARD_HELD):
        button = rail(page, bid)
        expect(button).to_have_attribute("aria-disabled", "true", timeout=T)
        expect(button).to_have_attribute("title", f"Cannot: {LOCATE_WHY}")
        button.click(force=True)                   # aria-disabled: nothing is sent
    page.wait_for_timeout(300)
    assert psim.locates == [] and psim.calls == []


# --- one click, one locate, a countdown -----------------------------------------------------------


@pytest.mark.mock_too
def test_one_click_sends_one_locate_for_5_s_and_counts_down(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    rail(page).click()
    count = page.locator(f'[data-testid="rail-locate-wrap"]:has([data-board="{BOARD}"]) '
                         '[data-testid="rail-locate-count"]')
    expect(count).to_have_text(re.compile(r"^[45]$"), timeout=T)
    assert wait_until(lambda: len(psim.locates) == 1)
    bid, seconds, who = psim.locates[0]
    assert (bid, seconds) == (BOARD, 5) and who.endswith((" via Harness Manager", " via HM"))
    expect(rail(page)).to_have_attribute("data-state", "blinking")
    expect(count).to_have_text(re.compile(r"^[123]$"), timeout=T)
    # after the 5 s: the daemon's 10 s window says when it may go again, then it is idle
    expect(rail(page)).to_have_attribute("data-state", "wait", timeout=T)
    expect(rail(page)).to_have_attribute("title", re.compile(r"Again in \d+ s"))
    expect(rail(page)).to_have_attribute("data-state", "idle", timeout=T)
    assert len(psim.locates) == 1
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_a_double_click_and_presses_while_it_runs_send_one(page_factory, daemon,
                                                                         engine, psim):
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST"
            and r.url.endswith("/identify") else None)
    rail(page).dblclick()
    expect(rail(page)).to_have_attribute("data-state", "blinking", timeout=T)
    for _ in range(3):
        rail(page).click(force=True)
    page.wait_for_timeout(500)
    assert [s for _b, s, _w in psim.locates] == [5]
    assert len(posts) == 1, posts


@pytest.mark.mock_too
def test_the_daemons_10_s_limit_shows_when_it_may_go_again(page_factory, daemon, engine, psim):
    """Another client (the CLI) identified the board 1 s long: the button is free again after
    1 s, but the daemon's window is not; the press is refused and says when."""
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    page.evaluate("""async (bid) => {
      const api = await import('./js/api.js');
      await api.call('identify', { bid }, { seconds: 1 });
    }""", BOARD)
    expect(rail(page)).to_have_attribute("data-state", "idle", timeout=T)
    rail(page).click()
    expect(rail(page)).to_have_attribute("data-state", "wait", timeout=T)
    expect(rail(page)).to_have_attribute("title", re.compile(r"once every 10 s per board\. Again in \d+ s"))
    assert [s for _b, s, _w in psim.locates] == [1], "the refused press reached no board"


@pytest.mark.mock_too
def test_negative_twin_after_the_window_the_press_goes(page_factory, daemon, engine, psim):
    linux(engine)
    limiter = psim.limiter if getattr(daemon.app.state, "panel", None) is not None \
        else daemon.app.state.daemon.presence.limiter
    limiter.every_s = 1.0
    page = page_factory(**APP)
    loaded(page)
    page.evaluate("""async (bid) => {
      const api = await import('./js/api.js');
      await api.call('identify', { bid }, { seconds: 1 });
    }""", BOARD)
    expect(rail(page)).to_have_attribute("data-state", "idle", timeout=T)
    page.wait_for_timeout(1200)
    rail(page).click()
    expect(rail(page)).to_have_attribute("data-state", "blinking", timeout=T)
    assert wait_until(lambda: [s for _b, s, _w in psim.locates] == [1, 5])


# --- never background ------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_the_buttons_make_no_request_on_their_own(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    expect(rail(page)).to_have_attribute("data-state", "idle", timeout=T)
    page.wait_for_timeout(3000)
    assert psim.locates == [] and psim.calls == [], "no locate, and no panel read for a button"


@pytest.mark.mock_too
def test_negative_twin_the_click_is_the_only_request(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    rail(page).click()
    assert wait_until(lambda: psim.calls == [("locate", BOARD)])
    page.wait_for_timeout(1000)
    assert psim.calls == [("locate", BOARD)]


# --- the Board tile, and the holder's note ---------------------------------------------------------


def open_board(page, bid=BOARD):
    page.locator(f'.board-item[data-board="{bid}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
    page.wait_for_selector('[data-testid="tile-locate"]', timeout=T)


@pytest.mark.mock_too
def test_the_board_tile_button_counts_down_and_stop_sends_seconds_0(page_factory, daemon,
                                                                    engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    page.locator('[data-testid="tile-locate"]').click()
    expect(page.locator('[data-testid="tile-locate-line"]')).to_contain_text(
        re.compile(r"blinking, \d s left"), timeout=T)
    expect(rail(page)).to_have_attribute("data-state", "blinking")      # one state, both places
    page.locator('[data-testid="tile-locate-stop"]').click()
    expect(page.locator('[data-testid="tile-locate-count"]')).to_have_count(0, timeout=T)
    assert wait_until(lambda: [s for _b, s, _w in psim.locates] == [5, 0])
    expect(page.locator('[data-testid="tile-locate-stop"]')).to_have_count(0)


@pytest.mark.mock_too
def test_negative_twin_no_stop_while_idle_and_a_second_press_does_not_stop(page_factory, daemon,
                                                                           engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    expect(page.locator('[data-testid="tile-locate-stop"]')).to_have_count(0)
    page.locator('[data-testid="tile-locate"]').click()
    expect(page.locator('[data-testid="tile-locate"]')).to_have_attribute("data-state", "blinking",
                                                                          timeout=T)
    page.locator('[data-testid="tile-locate"]').click(force=True)   # ignored, not a stop
    page.wait_for_timeout(500)
    assert [s for _b, s, _w in psim.locates] == [5]
    expect(page.locator('[data-testid="tile-locate"]')).to_have_attribute("data-state", "blinking")


@pytest.mark.mock_too
def test_the_countdown_follows_the_boards_until_ms(page_factory, daemon, engine, psim):
    """The board said 3 s (a board may answer less than asked): the count starts at 3."""
    linux(engine)
    page = page_factory(**APP)
    loaded(page)

    def three(route):
        response = route.fetch()
        body = response.json()
        body["until_ms"] = 3000
        route.fulfill(response=response, json=body)

    page.route("**/identify", three)
    rail(page).click()
    count = page.locator('[data-testid="rail-locate-count"]')
    expect(count).to_have_text(re.compile(r"^[23]$"), timeout=T)
    expect(count).to_have_count(0, timeout=4500)          # gone after about 3 s, not 5


@pytest.mark.mock_too
def test_negative_twin_the_default_answer_counts_from_5(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    loaded(page)
    rail(page).click()
    count = page.locator('[data-testid="rail-locate-count"]')
    expect(count).to_have_text(re.compile(r"^[45]$"), timeout=T)
    page.wait_for_timeout(3200)
    expect(count).to_have_text(re.compile(r"^[12]$"))      # still counting after 3.2 s


# --- the showcase demo (app --demo) ------------------------------------------------------------------


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request):
    from tests.web.test_demo_all_browser import make_showcase

    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def test_the_demo_linux_board_blinks_from_the_sidebar_without_being_opened(showcase):
    from harness_manager.demo_showcase import BOARD_LINUX

    page = showcase.page()
    loaded(page)
    button = rail(page, BOARD_LINUX)
    expect(button).to_have_attribute("data-state", "idle", timeout=T)
    button.click()
    expect(button).to_have_attribute("data-state", "blinking", timeout=T)
    glass = showcase.engine.__dict__["_demo_locate"][BOARD_LINUX]
    assert glass["until"] > time.time() and glass["who"].endswith((" via Harness Manager", " via HM"))
    assert BOARD_LINUX not in showcase.engine.open_boards(), "opened for the one request only"
    assert not page.errors, page.errors


def test_negative_twin_the_demo_bare_metal_and_leased_boards_say_why_not(showcase):
    from harness_manager.demo_showcase import BOARD_LEASED, BOARD_V011

    page = showcase.page()
    loaded(page)
    for bid in (BOARD_V011, BOARD_LEASED):
        button = rail(page, bid)
        expect(button).to_have_attribute("aria-disabled", "true", timeout=T)
        expect(button).to_have_attribute("title", f"Cannot: {LOCATE_WHY}")
        button.click(force=True)
    page.wait_for_timeout(300)
    assert "_demo_locate" not in showcase.engine.__dict__ or all(
        g["until"] == 0.0 for g in showcase.engine.__dict__["_demo_locate"].values())
