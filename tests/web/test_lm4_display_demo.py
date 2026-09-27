"""Lane LM4 over the demo daemon: ``harness-manager app --demo`` shows the Live display.

The REAL daemon app over ``DemoEngine(showcase=True)``: the Linux showcase board (mps3-lx)
mirrors its panel from the in-memory lcd_mirror board of ``harness_manager.demo_display``
(nothing is reached, no socket is opened for it); the bare-metal board is refused 422 with
the MPS3 adapter's words, and the board behind the hub 409 naming alice. Each has its twin.
"""

from __future__ import annotations

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_V011
from tests.web.test_demo_all_browser import make_showcase, open_board
from tests.web.test_lm4_display_browser import (
    T,
    by_id,
    canvas_pixels,
    display_sockets,
    is_live,
    png_size,
    raw_picture,
    show_display,
    viewers,
    wait_until,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
NEEDS_LINUX = "needs the Linux harness with lcd_mirror (this board runs the bare-metal harness)"


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request):
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def panel_of(show):
    return show.engine.session(BOARD_LINUX).display.panel


def test_the_demo_linux_board_shows_its_panel_live_byte_for_byte(showcase):
    page = showcase.page()
    socks = display_sockets(page)
    open_board(page, BOARD_LINUX)
    panel_of(showcase).frozen = True                      # the uptime stops ticking
    show_display(page)
    is_live(page)
    expect(by_id(page, "live-mode")).to_have_text("software tap")
    expect(by_id(page, "panel-mirror")).to_have_count(0)  # the text mirror is the fallback only
    seq, raw = raw_picture(showcase.daemon, BOARD_LINUX)
    assert wait_until(lambda: canvas_pixels(page)[0] == raw_picture(showcase.daemon, BOARD_LINUX)[0],
                      page=page)
    _cseq, canvas = canvas_pixels(page)
    assert canvas == raw_picture(showcase.daemon, BOARD_LINUX)[1]
    frame, _v, _n = panel_of(showcase).frame()
    assert canvas == frame                                # the panel as the board renders it
    # the twin: unfrozen, the uptime ticks and the picture moves on
    panel_of(showcase).frozen = False
    first = page.locator('[data-testid="live-display"]').get_attribute("data-seq")
    assert wait_until(lambda: by_id(page, "live-display").get_attribute("data-seq") != first,
                      timeout=5, page=page)
    assert canvas_pixels(page)[1] != canvas
    # the socket closes when the card is hidden (the demo holds nothing in the background)
    page.locator('[data-action="details"]').click()
    assert wait_until(socks[-1].is_closed, page=page)
    assert wait_until(lambda: viewers(showcase.daemon, BOARD_LINUX) == 0, page=page)
    assert not page.errors, page.errors


def test_the_demo_snapshot_is_a_png(showcase):
    page = showcase.page()
    open_board(page, BOARD_LINUX)
    show_display(page)
    is_live(page)
    with page.expect_download() as dl:
        page.locator('[data-action="live-snapshot"]').click()
    assert dl.value.suggested_filename.startswith("mps3-lx-live-display-")
    assert png_size(open(dl.value.path(), "rb").read()) == (320, 240)


def test_the_demo_bare_metal_boards_fall_back_to_the_text_mirror(showcase):
    page = showcase.page()
    open_board(page, BOARD_V011)
    show_display(page)
    expect(by_id(page, "live-display")).to_have_attribute("data-refused", "UNAVAILABLE", timeout=T)
    expect(by_id(page, "live-reason")).to_have_text(f"Live display: {NEEDS_LINUX}")
    expect(by_id(page, "panel-mirror")).to_have_attribute("data-source", "rebuilt")
    expect(by_id(page, "live-canvas")).to_have_count(0)
    # behind the hub, alice holds the lease: 409 names her (D3)
    open_board(page, BOARD_LEASED)
    show_display(page)
    expect(by_id(page, "live-display")).to_have_attribute("data-refused", "HELD", timeout=T)
    expect(by_id(page, "live-reason")).to_contain_text("the lease is held by alice@")
    expect(by_id(page, "panel-mirror")).to_be_visible()
    # the twin: the Linux board, in the same page, is live
    open_board(page, BOARD_LINUX)
    show_display(page)
    is_live(page)
    expect(by_id(page, "live-reason")).to_have_count(0)
    assert not page.errors, page.errors
