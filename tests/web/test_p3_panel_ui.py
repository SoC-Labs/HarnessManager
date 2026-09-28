"""Lane P3 in the browser: the MPS3 front panel in the web UI (docs/design/CLCD_ALIGNMENT.md §5).

david's decisions: P5 a line + Identify in the Board tile, the mirror in Details; P6 "held"
(violet) is someone else having it (the DUT owning the panel); P2 a tap on the panel's
lease-request banner notifies the holder and never releases; P3 these are Linux-harness
features, and bare metal (v0.11) shows a mirror labelled as rebuilt, with Identify disabled
and the reason.

Clicks only. The panel is ``PanelSim`` (tests/fakes/p1_mock_panel.py): the T14 mock serves
it, and ``PanelSim.attach`` gives the REAL daemon's demo sessions the same adapter, so
every test but the lease one runs over both servers (the lease requests are the mock's
``LeaseRequestSim``, as for lane LR-D). Each behaviour has its negative twin.
"""

from __future__ import annotations

import re
import time

import pytest

from harness_manager.demo import BOARD_FIELDED
from tests.fakes.clcd_panel_shell import LINUX_STATUS_ROWS, PANEL_FEATURES
from tests.fakes.p1_mock_panel import PanelSim

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]
T = 10_000
APP = {"width": 1440, "height": 900}
BOARD = BOARD_FIELDED
LOCATE_WHY = "needs harness feature 'locate' (Linux harness)"


@pytest.fixture(autouse=True)
def text_mirror_only(daemon):
    """These tests are about the text mirror (P3). Since LM4 the Live display replaces it while
    it is live, and the T14 mock gives every demo board a live display: not this board here."""
    display = getattr(daemon.app.state, "display", None)
    if display is not None:
        display.no_display(BOARD)


# --- helpers -----------------------------------------------------------------------------------


@pytest.fixture
def psim(daemon, engine):
    """The front panel both servers answer from: the mock's own, or one attached to the real
    daemon's demo sessions (before the page opens the board)."""
    sim = getattr(daemon.app.state, "panel", None)
    if sim is None:
        sim = PanelSim(engine)
        sim.attach(engine)
    return sim


def linux(engine, bid=BOARD):
    """The board runs the Linux harness with the front-panel verbs (R1-R3)."""
    feats = [f for f in engine._board(bid).identity.features if f not in PANEL_FEATURES]
    engine.set_features(bid, [*feats, *PANEL_FEATURES])


def open_board(page, bid=BOARD):
    page.locator(f'.board-item[data-board="{bid}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
    page.wait_for_selector('[data-testid="tile-panel"]', timeout=T)


def details(page):
    if page.locator('[data-action="details"][aria-expanded="false"]').count():
        page.locator('[data-action="details"]').click()
    page.wait_for_selector('[data-testid="panel-mirror"]', timeout=T)


def colour_of(page, selector):
    return page.eval_on_selector(selector, "el => getComputedStyle(el).color")


def token(page, name):
    """A token's value as the page computes it (the CSS var through a probe element)."""
    return page.evaluate("""(name) => {
      const el = document.createElement('span');
      el.style.color = `var(${name})`;
      document.body.appendChild(el);
      const c = getComputedStyle(el).color;
      el.remove();
      return c;
    }""", name)


def wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def line(page):
    return page.locator('[data-testid="tile-panel-line"]')


# --- Identify ------------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_identify_on_linux_blinks_for_the_chosen_seconds_and_stops(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    expect(line(page)).to_have_text("status page · harness owns it · touch unknown", timeout=T)
    # LOCATE: the tile has the one-click 5 s button; the chosen-seconds control is in Details.
    details(page)
    tile = page.locator('[data-testid="panel-identify"]')
    select = tile.locator('[data-testid="identify-seconds"]')
    expect(select).to_have_value("5")                              # the default (LOCATE: 5 s)
    select.select_option("20")
    t0 = time.time()
    tile.locator('[data-action="identify"]').click()
    until = tile.locator('[data-testid="identify-until"]')
    expect(until).to_contain_text(re.compile(r"blinking until \d\d:\d\d:\d\d"), timeout=T)
    assert wait_until(lambda: psim.boards[BOARD]["locate_until"] > 0)
    assert 18 <= psim.boards[BOARD]["locate_until"] - t0 <= 23        # the 20 s asked for
    assert tile.locator('[data-testid="reason-identify"]').count() == 0
    # Stop: seconds 0, and the line goes
    tile.locator('[data-action="identify_stop"]').click()
    expect(until).to_have_count(0, timeout=T)
    assert wait_until(lambda: psim.boards[BOARD]["locate_until"] == 0)
    expect(tile.locator('[data-action="identify"]')).to_be_enabled()
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_identify_on_bare_metal_is_disabled_with_the_reason(page_factory, daemon, engine, psim):
    page = page_factory(**APP)
    open_board(page)                                            # v0.11: no "locate"
    # LOCATE: the tile's one-click button, disabled with the reason as its tooltip
    tile_button = page.locator('[data-testid="tile-locate"]')
    expect(tile_button).to_have_attribute("aria-disabled", "true", timeout=T)
    expect(tile_button).to_have_attribute("title", f"Cannot: {LOCATE_WHY}")
    expect(page.locator('[data-testid="tile-locate-line"]')).to_have_text(f"Cannot: {LOCATE_WHY}")
    tile_button.click(force=True)                # aria-disabled: nothing is sent
    # the line says it is rebuilt; the Details card too, with Identify disabled there as well
    expect(line(page)).to_contain_text("page not reported · harness owns it · touch unknown")
    expect(page.locator('[data-testid="tile-panel-rebuilt"]')).to_have_text("rebuilt")
    details(page)
    tile = page.locator('[data-testid="panel-identify"]')
    button = tile.locator('[data-action="identify"]')
    expect(button).to_have_attribute("aria-disabled", "true", timeout=T)
    expect(button).to_have_attribute("title", f"Cannot: {LOCATE_WHY}")
    expect(tile.locator('[data-testid="reason-identify"]')).to_have_text(f"Cannot: {LOCATE_WHY}")
    expect(tile.locator('[data-testid="identify-seconds"]')).to_be_disabled()
    button.click(force=True)                     # aria-disabled: an interlock, nothing is sent
    expect(tile.locator('[data-testid="identify-result"]')).to_contain_text("Nothing was run.")
    expect(tile.locator('[data-testid="identify-result"]')).to_contain_text(
        "$ identify 192.168.10.101:6900 --seconds 5  (not run)")
    assert psim.boards[BOARD]["locate_until"] == 0.0 and psim.locates == []
    assert tile.locator('[data-testid="identify-until"]').count() == 0
    card = page.locator('[data-testid="panel-card"]')
    expect(card.locator('[data-testid="panel-rebuilt"]')).to_contain_text(
        "Rebuilt from what Harness Manager read, not read from the panel")
    expect(card.locator('[data-testid="panel-mirror"]')).to_have_attribute("data-source", "rebuilt")
    expect(card.locator('[data-testid="panel-identify"] [data-testid="reason-identify"]')).to_have_text(
        f"Cannot: {LOCATE_WHY}")
    expect(card.locator('[data-testid="panel-sessions-none"]')).to_contain_text("not reported")


# --- the mirror ------------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_the_mirror_updates_on_a_panel_state_event(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    details(page)
    mirror = page.locator('[data-testid="panel-mirror"]')
    expect(mirror).to_have_attribute("data-source", "panel", timeout=T)
    expect(mirror.locator('[data-row="0"]')).to_have_text(LINUX_STATUS_ROWS[0])
    assert page.locator('[data-testid="panel-rebuilt"]').count() == 0     # read, not rebuilt
    expect(page.locator('[data-testid="panel-read-age"]')).to_contain_text("Read from the panel")
    # the sessions, with this Harness Manager's marked
    mine = page.locator('[data-testid="panel-sessions"] li[data-mine="yes"]')
    expect(mine).to_have_count(1)
    expect(mine.locator('[data-testid="panel-session-mine"]')).to_have_text("you")
    # the board raises a banner: panel.state, and the mirror follows (no reload, no click)
    rows = list(LINUX_STATUS_ROWS)
    rows[11] = "         NETWORK LINK DOWN".ljust(40)
    psim.set_rows(BOARD, tuple(rows), banner="NETWORK LINK DOWN")
    expect(mirror.locator('[data-row="11"]')).to_have_text(rows[11], timeout=T)
    expect(page.locator('[data-testid="panel-banner"]')).to_have_text("NETWORK LINK DOWN")
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_without_a_panel_state_event_the_mirror_stays(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    details(page)
    mirror = page.locator('[data-testid="panel-mirror"]')
    expect(mirror.locator('[data-row="0"]')).to_have_text(LINUX_STATUS_ROWS[0], timeout=T)
    # the glass changes, but nothing says so: the page shows what it last read
    psim.board(BOARD)["rows"] = tuple("CHANGED WITHOUT AN EVENT".ljust(40) for _ in range(15))
    page.wait_for_timeout(4000)                                 # past the 3 s frame reuse
    expect(mirror.locator('[data-row="0"]')).to_have_text(LINUX_STATUS_ROWS[0])
    assert page.locator('[data-testid="panel-banner"]').count() == 0


# --- held: the DUT owns the panel ---------------------------------------------------------------------


@pytest.mark.mock_too
def test_a_dut_owned_panel_shows_the_held_colour(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    details(page)
    psim.set_owner(BOARD, "dut")
    owner = '[data-testid="tile-panel-line"] [data-part="owner"]'
    expect(page.locator(owner)).to_have_text("DUT owns it", timeout=T)
    expect(page.locator(owner)).to_have_attribute("data-level", "held")
    assert colour_of(page, owner) == token(page, "--held")
    chip = page.locator('[data-testid="panel-owner-chip"]')
    expect(chip).to_have_attribute("data-level", "held")
    expect(chip).to_have_text("DUT owns it")
    assert colour_of(page, '[data-testid="panel-owner-chip"]') == token(page, "--held")
    # and in dark: the dark theme's held
    page.locator('.seg button[title="Dark"]').click()
    page.wait_for_timeout(200)
    assert colour_of(page, owner) == token(page, "--held")
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_a_harness_owned_panel_is_ok_not_held(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    owner = '[data-testid="tile-panel-line"] [data-part="owner"]'
    expect(page.locator(owner)).to_have_text("harness owns it", timeout=T)
    expect(page.locator(owner)).to_have_attribute("data-level", "ok")
    assert colour_of(page, owner) == token(page, "--ok")
    assert colour_of(page, owner) != token(page, "--held")
    # it went to the DUT and came back: ok again
    psim.set_owner(BOARD, "dut")
    expect(page.locator(owner)).to_have_attribute("data-level", "held", timeout=T)
    psim.set_owner(BOARD, "harness")
    expect(page.locator(owner)).to_have_attribute("data-level", "ok", timeout=T)


# --- touch health -----------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_touch_unavailable_is_shown_with_its_counts(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    details(page)
    psim.set_touch(BOARD, False, bus_lost=3, recoveries=1)
    touch = '[data-testid="tile-panel-line"] [data-part="touch"]'
    expect(page.locator(touch)).to_have_text("touch unavailable (bus lost 3×, 1 recovery)", timeout=T)
    expect(page.locator(touch)).to_have_attribute("data-level", "err")
    assert colour_of(page, touch) == token(page, "--err")
    expect(page.locator('[data-testid="panel-touch"]')).to_have_text("touch unavailable (bus lost 3×, 1 recovery)")
    psim.set_touch(BOARD, False, bus_lost=5, recoveries=2)
    expect(page.locator(touch)).to_have_text("touch unavailable (bus lost 5×, 2 recoveries)", timeout=T)


@pytest.mark.mock_too
def test_negative_twin_touch_ok_or_unreported_is_not_unavailable(page_factory, daemon, engine, psim):
    linux(engine)
    page = page_factory(**APP)
    open_board(page)
    touch = '[data-testid="tile-panel-line"] [data-part="touch"]'
    expect(page.locator(touch)).to_have_text("touch unknown", timeout=T)       # not reported
    expect(page.locator(touch)).to_have_attribute("data-level", "unk")
    psim.set_touch(BOARD, True)
    expect(page.locator(touch)).to_have_text("touch ok", timeout=T)
    expect(page.locator(touch)).to_have_attribute("data-level", "ok")


# --- a tap on the panel's lease-request banner (mock only: LR-D's lease requests) ----------------


def holder_page(page_factory, daemon, engine):
    linux(engine)
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")
    page = page_factory(**APP)
    open_board(page)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    return page


@pytest.mark.week_plan("hub_api", sim=True)
def test_a_request_tap_shows_tapped_on_the_panel_to_the_holder_and_never_releases(page_factory, daemon, engine):
    page = holder_page(page_factory, daemon, engine)
    reqs = daemon.app.state.sim.requests
    rid = reqs.incoming(BOARD, by="bob@lab-pc-02", message="demo at 3")
    prompt = page.locator(f'[data-testid="lease-wanted"][data-request="{rid}"]')
    expect(prompt).to_be_visible(timeout=T)
    assert prompt.locator('[data-testid="wanted-tapped"]').count() == 0
    details(page)
    daemon.app.state.panel.tap(BOARD, "request")               # someone at the board taps it
    tapped = prompt.locator('[data-testid="wanted-tapped"]')
    expect(tapped).to_contain_text(re.compile(r"Tapped on the panel at \d\d:\d\d:\d\d"), timeout=T)
    expect(tapped).to_contain_text("Nothing was released")
    expect(page.locator('[data-testid="panel-taps"] li[data-on="request"]')).to_contain_text(
        "tapped the lease request")
    # it never releases: the lease is still ours, the request still waits for our answer
    page.wait_for_timeout(500)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours")
    expect(prompt.locator('[data-action="respond_release"]')).to_be_visible()
    assert reqs.answers.get(BOARD, {}) == {} and reqs.revokes == []
    assert [n["id"] for n in reqs.inbox[BOARD]] == [rid]
    assert daemon.app.state.sim.hubs[BOARD]["lease"]["mine"] is True
    assert page.errors == []


@pytest.mark.week_plan("hub_api", sim=True)
def test_negative_twin_a_tap_elsewhere_or_with_no_request_marks_nothing(page_factory, daemon, engine):
    page = holder_page(page_factory, daemon, engine)
    reqs = daemon.app.state.sim.requests
    panel = daemon.app.state.panel
    details(page)
    panel.tap(BOARD, "request")                                # no open request: a stale banner
    expect(page.locator('[data-testid="panel-taps"] li[data-on="request"]')).to_have_count(1, timeout=T)
    assert reqs.taps.get(BOARD, {}) == {}
    rid = reqs.incoming(BOARD, by="bob@lab-pc-02", message="demo at 3")
    prompt = page.locator(f'[data-testid="lease-wanted"][data-request="{rid}"]')
    expect(prompt).to_be_visible(timeout=T)
    panel.tap(BOARD, "nav")                                    # a page turn is not a request tap
    expect(page.locator('[data-testid="panel-taps"] li[data-on="nav"]')).to_contain_text("tapped next page",
                                                                                         timeout=T)
    page.wait_for_timeout(1500)
    assert prompt.locator('[data-testid="wanted-tapped"]').count() == 0
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours")
