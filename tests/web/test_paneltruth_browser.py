"""PANEL-TRUTH in the browser: the Front panel card says what the image reports, never a
harness type guessed from a missing feature, and the Live display rides out one hub hiccup.

What david saw on board 1 (``mps3_01_pl``, Linux harness rc2_v6 from its card, 2026-09-28):
"This harness (bare metal) reports only who owns the panel" on a Linux harness; "Sessions:
not reported: needs harness feature 'presence' (Linux ..."; "Live display: cannot confirm you
hold the lease ... kex_exchange_identification: read: Connection reset by peer" while he held
it; and a rebuilt text mirror with ``NET : 127.0.0.1`` and ``?`` for every unknown.

Over the T14 mock (its PanelSim, its DisplaySim with the ``checking`` knob) and over the demo
daemon's showcase (the in-memory lcd_mirror board). Each check has its negative twin.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_FIELDED, FIELDED_FEATURES
from harness_manager.demo_showcase import (
    BOARD_LINUX,
    BOARD_V011,
    LINUX_DEMO_FEATURES,
    PANEL_FEATURES,
)
from harness_manager_mps3.capabilities import NEEDS_LOCATE
from harness_manager_mps3.display import CHECKING
from tests.web.test_demo_all_browser import make_showcase
from tests.web.test_demo_all_browser import open_board as open_showcase_board
from tests.web.test_lm4_display_browser import HOLDER, by_id, is_live, live_page, show_display
from tests.web.test_p3_panel_ui import details, open_board

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]
T = 10_000
APP = {"width": 1440, "height": 900}
BOARD = BOARD_FIELDED
#: Words that name a harness TYPE. The card may say one only from the harness's own impl.
BARE = ("bare metal", "bare-metal")
DASH = "—"


def linux_without_panel(engine, bid=BOARD):
    """The harness says it is Linux (``version.impl``) and has 'clcd_kvm' but not 'panel',
    'presence' or 'locate': the rc2_v6 image on david's board."""
    engine._set_identity(bid, harness_impl="linux",
                         features=tuple(f for f in FIELDED_FEATURES if f not in PANEL_FEATURES))


def card(page):
    return page.locator('[data-testid="panel-card"]')


def no_display(daemon):
    display = getattr(daemon.app.state, "display", None)
    if display is not None:
        display.no_display(BOARD)


# --- 1. the card names no type it guessed ------------------------------------------------------------


@pytest.mark.mock_too
def test_a_linux_image_without_panel_features_is_never_called_bare_metal(page_factory, daemon,
                                                                         engine, psim):
    no_display(daemon)                                  # the rebuilt text is what is shown
    linux_without_panel(engine)
    page = page_factory(**APP)
    open_board(page)
    details(page)
    c = card(page)
    head = c.locator('[data-testid="panel-headline"]')
    expect(head).to_have_attribute("data-state", "rebuilt", timeout=T)
    expect(head.locator('[data-testid="panel-headline-chip"]')).to_have_text("Rebuilt")
    expect(c.locator('[data-testid="panel-rebuilt"]')).to_have_text(
        "Rebuilt from what Harness Manager read, not read from the panel: this Linux harness "
        "image does not send its panel's text.")
    missing = c.locator('[data-testid="panel-not-reported"]')
    expect(missing).to_contain_text(
        "Not reported by this image: page, touch health, who is connected, recent taps")
    expect(missing).to_have_attribute("data-missing", "page touch sessions taps")
    missing.locator("summary").click()                  # the features, behind the disclosure
    expect(c.locator('[data-testid="panel-not-reported-details"]')).to_contain_text(
        "who is connected: needs harness feature 'presence'")
    expect(c.locator('[data-testid="panel-impl"]')).to_have_text(
        'the harness says it is the Linux harness (version.impl "linux")')
    expect(c.locator('[data-testid="reason-identify"]')).to_have_text(f"Cannot: {NEEDS_LOCATE}")
    # only the rows it reports: no "not reported" rows, no Page, no Sessions
    for gone in ("panel-page", "panel-sessions-none", "panel-taps-none", "panel-touch"):
        assert c.locator(f'[data-testid="{gone}"]').count() == 0, gone
    expect(c.locator('[data-testid="panel-line"] [data-part="owner"]')).to_have_text("harness owns it")
    text = c.inner_text()
    assert not any(w in text for w in BARE), text
    assert "Linux harness)" not in text                 # no "(Linux harness)" as a missing thing
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_harness_that_says_bare_metal_is_named_so(page_factory, daemon, engine, psim):
    no_display(daemon)
    engine._set_identity(BOARD, harness_impl="bare-metal")
    page = page_factory(**APP)
    open_board(page)
    details(page)
    c = card(page)
    expect(c.locator('[data-testid="panel-rebuilt"]')).to_contain_text(
        "this bare-metal harness image does not send its panel's text", timeout=T)
    c.locator('[data-testid="panel-not-reported"] summary').click()
    expect(c.locator('[data-testid="panel-impl"]')).to_contain_text("the bare-metal harness")


@pytest.mark.mock_too
def test_twin_a_harness_that_does_not_say_is_named_neither_way(page_factory, daemon, engine,
                                                              psim):
    no_display(daemon)                                  # the demo's fielded board: no impl
    page = page_factory(**APP)
    open_board(page)
    details(page)
    c = card(page)
    expect(c.locator('[data-testid="panel-rebuilt"]')).to_contain_text(
        "this harness image does not send", timeout=T)
    c.locator('[data-testid="panel-not-reported"] summary').click()
    expect(c.locator('[data-testid="panel-impl"]')).to_have_text(
        "the harness did not say which harness it is")
    text = c.inner_text()
    assert not any(w in text for w in BARE) and "Linux" not in text, text


@pytest.mark.mock_too
def test_the_rebuilt_text_shows_unknowns_as_a_dash_with_a_legend(page_factory, daemon, engine,
                                                                 psim):
    no_display(daemon)
    linux_without_panel(engine)
    page = page_factory(**APP)
    open_board(page)
    details(page)
    mirror = card(page).locator('[data-testid="panel-mirror"]')
    expect(mirror).to_have_attribute("data-source", "rebuilt", timeout=T)
    expect(mirror.locator('[data-row="5"]')).to_have_text("NET : 192.168.10.101".ljust(40))
    expect(mirror.locator('[data-row="3"]')).to_have_text(f"SWAP: {DASH}".ljust(40))
    rows = mirror.inner_text()
    assert "?" not in rows and "127.0.0.1" not in rows
    expect(card(page).locator('[data-testid="panel-mirror-legend"]')).to_have_text(
        f"{DASH} not reported by this image")


@pytest.mark.mock_too
def test_twin_a_linux_image_with_the_panel_features_reports_every_row(page_factory, daemon,
                                                                      engine, psim):
    no_display(daemon)
    engine._set_identity(BOARD, harness_impl="linux", features=(*FIELDED_FEATURES, *PANEL_FEATURES))
    page = page_factory(**APP)
    open_board(page)
    details(page)
    psim.set_touch(BOARD, True)
    c = card(page)
    expect(c.locator('[data-testid="panel-headline"]')).to_have_attribute("data-state", "read",
                                                                           timeout=T)
    expect(c.locator('[data-testid="panel-line"] [data-part="page"]')).to_have_text("status page")
    expect(c.locator('[data-testid="panel-line"] [data-part="touch"]')).to_have_text("touch ok", timeout=T)
    expect(page.locator('[data-testid="ov-watching"]')).to_have_attribute("data-source", "presence")
    assert c.locator('[data-testid="panel-not-reported"]').count() == 0
    assert c.locator('[data-testid="panel-mirror-legend"]').count() == 0


@pytest.fixture
def psim(daemon, engine):
    """The front panel both servers answer from (as ``test_p3_panel_ui.psim``)."""
    from tests.fakes.p1_mock_panel import PanelSim

    sim = getattr(daemon.app.state, "panel", None)
    if sim is None:
        sim = PanelSim(engine)
        sim.attach(engine)
    return sim


# --- 2. the Live display: "checking", then live; the headline follows ---------------------------------


@pytest.mark.week_plan("display_api", sim=True)
def test_a_hub_that_does_not_answer_shows_checking_then_the_picture(page_factory, daemon,
                                                                   engine):
    linux_without_panel(engine)
    daemon.app.state.display.checking(BOARD, times=3)
    page = live_page(page_factory)
    root = by_id(page, "live-display")
    reason = by_id(page, "live-reason")
    expect(root).to_have_attribute("data-state", "checking", timeout=T)
    expect(reason).to_have_text(f"Live display: {CHECKING}")
    assert "kex" not in reason.inner_text()             # ssh's words are never the headline
    by_id(page, "live-detail").locator("summary").click()
    expect(by_id(page, "live-detail")).to_contain_text("kex_exchange_identification")
    assert root.get_attribute("data-refused") == ""     # not a refusal: it asks again itself
    expect(by_id(page, "panel-mirror")).to_be_visible()  # the rebuilt text meanwhile
    # ... and opens on the retry, with no click: the card's headline says Live
    is_live(page)
    head = card(page).locator('[data-testid="panel-headline"]')
    expect(head).to_have_attribute("data-state", "live", timeout=T)
    expect(head.locator('[data-testid="panel-headline-chip"]')).to_have_text("Live")
    expect(by_id(page, "panel-mirror")).to_have_count(0)
    expect(by_id(page, "live-detail")).to_have_count(0)
    assert not page.errors, page.errors


@pytest.mark.week_plan("display_api", sim=True)
def test_twin_the_hub_says_someone_else_holds_it_refused_by_name(page_factory, daemon, engine):
    linux_without_panel(engine)
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder=HOLDER)
    page = live_page(page_factory)
    expect(by_id(page, "live-display")).to_have_attribute("data-refused", "HELD", timeout=T)
    expect(by_id(page, "live-reason")).to_contain_text(f"{HOLDER} holds")
    assert CHECKING not in by_id(page, "live-reason").inner_text()
    head = card(page).locator('[data-testid="panel-headline"]')
    expect(head).to_have_attribute("data-state", "rebuilt")          # not Live
    assert not page.errors, page.errors


# --- 3. the demo daemon: david's board as the showcase's Linux board ----------------------------------


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request):
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def test_demo_a_linux_board_without_panel_features_is_live_and_says_what_it_lacks(showcase):
    showcase.engine._set_identity(BOARD_LINUX, features=tuple(
        f for f in LINUX_DEMO_FEATURES if f not in PANEL_FEATURES))
    page = showcase.page()
    open_showcase_board(page, BOARD_LINUX)
    show_display(page)
    is_live(page)
    c = card(page)
    expect(c.locator('[data-testid="panel-headline"]')).to_have_attribute("data-state", "live",
                                                                           timeout=T)
    expect(c.locator('[data-testid="panel-not-reported"]')).to_contain_text(
        "Not reported by this image: page, who is connected, recent taps")
    expect(c.locator('[data-testid="panel-line"] [data-part="touch"]')).to_have_text("touch ok")
    assert not any(w in c.inner_text() for w in BARE), c.inner_text()
    assert not page.errors, page.errors


def test_twin_demo_the_bare_metal_board_is_rebuilt_and_named_from_its_impl(showcase):
    page = showcase.page()
    open_showcase_board(page, BOARD_V011)
    c = card(page)
    expect(c.locator('[data-testid="panel-headline"]')).to_have_attribute("data-state", "rebuilt",
                                                                           timeout=T)
    expect(c.locator('[data-testid="panel-rebuilt"]')).to_contain_text(
        "this bare-metal harness image does not send its panel's text")
    # UI v2: the daemon's 422 (UNAVAILABLE) makes the Front panel Text only, and says why
    expect(c).to_have_attribute("data-view", "text", timeout=T)
    expect(c.locator('[data-action="panel-view-live"]')).to_be_disabled()
    assert not page.errors, page.errors
