"""T10: the Board & XDC section in a headless system Chrome, on the real daemon with
``xdc_api`` loaded (in ``EXTENSIONS`` since CCR T10-1).

Drives it with clicks only: preview the RM kit (the demo board runs the previous static,
so the one error is ``static_id``), switch to the full-board kit (no error: the twin),
then preview a bad pasted design and see its direction check.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

pytestmark = pytest.mark.browser
T = 10_000


def open_xdc(page):
    page.wait_for_selector(".board-item", timeout=T)
    page.locator(f'.board-item[data-board="{BOARD_USB}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    nav.section(page, "xdc")                           # UI v2: the Build tab's XDC fold
    page.wait_for_selector('[data-testid="xdc-model"]', timeout=T)


@pytest.mark.mock_too
def test_preview_the_rm_kit_then_the_board_kit_then_a_failing_design(page_factory, screenshots,
                                                                    daemon):
    page = page_factory("light")
    open_xdc(page)
    assert "derived" in page.locator('[data-testid="xdc-derived"]').inner_text()
    assert page.locator('[data-testid="xdc-board-static"]').inner_text().strip()

    page.locator('[data-testid="xdc-design"]').select_option("nanosoc")
    page.locator('[data-testid="xdc-preview"]').click()
    page.wait_for_selector('[data-testid="xdc-file-body"]', timeout=T)
    body = page.locator('[data-testid="xdc-file-body"]').inner_text()
    assert "create_clock -name dut_clk -period 20.000" in body
    assert page.locator('[data-check="tied_off"]').count() == 1
    # the demo board runs the previous static 0x3F1A560F: the preview says the RM kit is
    # for another static (the one error), and the Files card says it is a preview only
    errors = page.locator('[data-severity="error"]')
    assert errors.count() == 1 and errors.first.get_attribute("data-check") == "static_id"
    assert "Preview only" in page.locator('[data-testid="xdc-files"]').inner_text()
    page.screenshot(path=str(screenshots / f"light-xdc-rm-kit-{type(daemon).__name__}.png"))

    page.locator('button:has-text("Full board")').click()
    page.locator('[data-testid="xdc-design"]').select_option("blinky")
    page.locator('[data-testid="xdc-preview"]').click()
    page.wait_for_selector('[data-testid="xdc-checks-ok"]', timeout=T)     # the twin: no error
    assert page.locator('[data-severity="error"]').count() == 0
    page.locator('[data-testid="xdc-file-tabs"] [data-file="blinky_pins.xdc"]').click()
    assert "PACKAGE_PIN AK16" in page.locator('[data-testid="xdc-file-body"]').inner_text()
    page.screenshot(path=str(screenshots / f"light-xdc-board-{type(daemon).__name__}.png"))
    with page.expect_download(timeout=T) as dl:                    # api.js callBlob
        page.locator('[data-testid="xdc-download"]').click()
    assert dl.value.suggested_filename == "blinky_board.zip"
    page.wait_for_selector('[data-testid="xdc-downloaded"]', timeout=T)

    page.locator('[data-testid="xdc-use-custom"]').check()
    page.locator('[data-testid="xdc-custom"]').fill(
        '{"kind": "board", "name": "bad", "ports": ['
        '{"port": "sw", "net": "USER_SW[0]", "dir": "out"}]}')
    page.locator('[data-testid="xdc-preview"]').click()
    page.wait_for_selector('[data-severity="error"][data-check="direction"]', timeout=T)
    assert "fight the board's driver" in page.locator('[data-testid="xdc-checks"]').inner_text()
    overflow = page.evaluate("""() => {
      const w = document.documentElement.clientWidth;
      return [...document.querySelectorAll('.card')].filter(
        (el) => el.getBoundingClientRect().right > w + 1).map((el) => el.dataset.testid || '?');
    }""")
    assert overflow == []
    assert page.errors == []
