"""FIX-PACK-1 item 2: a card whose background read is held back never spins for ever.

The first real-board session (2026-09-28) found the Front panel card on "Reading the
panel..." for ever with background reads off (``general.background_poll = off``, or a
board's ``poll = off``): every panel read is a background read, so each came back quiet and
the card waited for an answer that could not come. The Board tile's panel, card,
temperature and clock lines ("reading...") and the Telemetry card did the same.

Now each says why (the quiet reason: background reads off, the lease elsewhere, busy) and
offers **Read now**, one explicit read. A headless system Chrome on the REAL daemon over
DemoEngine, the fielded board on the Linux harness with the simulated panel
(``tests/fakes/p1_mock_panel.PanelSim``). Each check has its negative twin: with background
reads on, the same cards read and show the panel, and nothing says "Read now".
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_FIELDED
from tests.fakes.clcd_panel_shell import LINUX_STATUS_ROWS, PANEL_FEATURES
from tests.fakes.p1_mock_panel import PanelSim
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]
T = 10_000
APP = {"width": 1440, "height": 900}
BOARD = BOARD_FIELDED


@pytest.fixture
def psim(daemon, engine):
    display = getattr(daemon.app.state, "display", None)
    if display is not None:
        display.no_display(BOARD)                     # the text mirror (P3), not the Live display
    sim = getattr(daemon.app.state, "panel", None)
    if sim is None:
        sim = PanelSim(engine)
        sim.attach(engine)
    feats = [f for f in engine._board(BOARD).identity.features if f not in PANEL_FEATURES]
    engine.set_features(BOARD, [*feats, *PANEL_FEATURES])
    return sim


def gate(daemon, policy: str = "off", holder: str = ""):
    """The real daemon's background gate, as for a real board (the demo's says yes)."""
    q = daemon.app.state.daemon.quiet
    q.enabled = True
    q.policy_of = lambda _bid: policy
    q.lease_of = lambda _bid: holder
    return q


def open_board(page) -> None:
    page.locator(f'.board-item[data-board="{BOARD}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    nav.land(page)                       # UI v2: a board opens on the Workbench; these read the Overview
    # Background reads off: the Overview's first read was held back too ("Read now" there).
    page.wait_for_selector('[data-testid="overview"], [data-testid="info-quiet"]', timeout=T)
    if page.locator('[data-testid="info-quiet"]').count():
        page.locator('[data-testid="info-quiet"] button').click()
    page.wait_for_selector('[data-testid="panel-card"]', timeout=T)


def details(page) -> None:
    if page.locator('[data-action="details"][aria-expanded="false"]').count():
        page.locator('[data-action="details"]').click()
    page.wait_for_selector('[data-testid="panel-card"]', timeout=T)


def test_background_reads_off_the_panel_card_says_so_and_read_now_reads_once(
        page_factory, daemon, engine, psim, screenshots):
    gate(daemon, "off")
    page = page_factory(**APP)
    open_board(page)
    details(page)
    card = page.locator('[data-testid="panel-card"]')
    note = card.locator('[data-testid="panel-quiet"]')
    expect(note).to_contain_text("Background reads are off", timeout=T)
    assert card.locator('text="Reading the panel..."').count() == 0, "never the endless spinner"
    # The Overview (UI v2): "not read" on each held-back reading, the reason once, with Read now.
    expect(page.locator('[data-testid="ov-kpi-temp"]')).to_contain_text("not read")
    expect(page.locator('[data-testid="tile-board-quiet"]')).to_contain_text(
        "Background reads are off")
    page.screenshot(path=str(screenshots / "light-fixpack1-panel-background-off.png"))
    # Read now: one explicit read (no X-HM-Background), and the card shows the panel.
    asked: list = []
    page.on("request", lambda r: asked.append((r.url, r.headers)) if "/panel" in r.url else None)
    note.locator('[data-action="panel-read-now"]').click()
    mirror = card.locator('[data-testid="panel-mirror"]')
    expect(mirror.locator('[data-row="0"]')).to_have_text(LINUX_STATUS_ROWS[0], timeout=T)
    expect(card.locator('[data-testid="panel-read-age"]')).to_contain_text("Read from the panel")
    assert asked and all("x-hm-background" not in h for _u, h in asked), asked
    assert card.locator('[data-testid="panel-quiet"]').count() == 0
    # The Overview's Read now reads the rest (the telemetry), explicitly, too.
    page.locator('[data-action="tile-read-now"]').click()
    expect(page.locator('[data-testid="ov-kpi-temp"]')).not_to_contain_text("not read", timeout=T)
    expect(page.locator('[data-testid="tile-board-quiet"]')).to_have_count(0, timeout=T)
    assert page.errors == []


def test_the_board_tiles_read_now_reads_every_held_back_line(page_factory, daemon, engine, psim):
    gate(daemon, "off")
    page = page_factory(**APP)
    open_board(page)
    tile = page.locator('[data-testid="tile-board-quiet"]')
    expect(tile).to_contain_text("Background reads are off", timeout=T)
    tile.locator('[data-action="tile-read-now"]').click()
    expect(page.locator('[data-testid="panel-line"]')).to_contain_text("harness owns it",
                                                                      timeout=T)
    expect(page.locator('[data-testid="ov-kpi-temp"]')).not_to_contain_text("not read", timeout=T)
    expect(tile).to_have_count(0, timeout=T)
    # UI v2: the Front panel is on screen, so the same click read its text mirror too; a mirror
    # read the gate still held back says so, with its own Read now (never a spinner)
    details(page)
    note = page.locator('[data-testid="panel-mirror-quiet"]')
    if note.count():
        expect(note).to_contain_text("Background reads are off")
        note.locator('[data-action="panel-mirror-read-now"]').click()
    expect(page.locator('[data-testid="panel-mirror"] [data-row="0"]')).to_have_text(
        LINUX_STATUS_ROWS[0], timeout=T)
    assert page.errors == []


def test_a_lease_held_elsewhere_names_the_holder_on_the_panel_card(page_factory, daemon,
                                                                   engine, psim):
    gate(daemon, "on-view", holder="alice@lab-pc")
    page = page_factory(**APP)
    open_board(page)
    details(page)
    expect(page.locator('[data-testid="panel-quiet"]')).to_contain_text(
        "the hub lease is held by alice@lab-pc", timeout=T)
    assert page.errors == []


def test_twin_background_reads_on_the_cards_read_and_offer_no_read_now(page_factory, daemon,
                                                                       engine, psim):
    gate(daemon, "on-view")
    page = page_factory(**APP)
    open_board(page)
    details(page)
    card = page.locator('[data-testid="panel-card"]')
    expect(card.locator('[data-testid="panel-mirror"] [data-row="0"]')).to_have_text(
        LINUX_STATUS_ROWS[0], timeout=T)
    expect(page.locator('[data-testid="panel-line"]')).to_contain_text("harness owns it")
    for testid in ("panel-quiet", "tile-board-quiet"):
        assert page.locator(f'[data-testid="{testid}"]').count() == 0, testid
    assert page.errors == []
