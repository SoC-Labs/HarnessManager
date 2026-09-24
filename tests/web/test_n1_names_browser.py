"""N1 in the browser: a board's name in the rail, the header and the window title.

The demo's fielded board carries the lab board's hub name (``mps3-01``, source
``hub``); the USB demo board has none, so it is its address (the negative twin).
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser

T = 10_000


def rail(page, board_id):
    return page.locator(f'.board-item[data-board="{board_id}"]')


def test_a_named_board_shows_its_name_in_the_rail_header_and_title(page_factory, screenshots):
    page = page_factory()
    page.wait_for_selector(f'.board-item[data-board="{BOARD_FIELDED}"]', timeout=T)
    name = rail(page, BOARD_FIELDED).locator('[data-testid="rail-name"]')
    expect(name).to_have_text("mps3-01")
    assert "name from the hub" in name.get_attribute("title")
    assert BOARD_FIELDED in name.get_attribute("title")

    rail(page, BOARD_FIELDED).click()
    expect(page.locator('[data-testid="preview-name"]')).to_contain_text("mps3-01")
    expect(page).to_have_title("mps3-01 · Harness Manager")
    page.locator('[data-action="open"]').click()
    header = page.locator('[data-testid="header-name"]')
    expect(header).to_have_text("mps3-01", timeout=T)
    expect(page.locator('[data-testid="header-sub"]')).to_contain_text("on shell 0x3f1a560f")
    expect(page.locator(".header-id")).to_have_text(BOARD_FIELDED)
    expect(page).to_have_title("mps3-01 · Harness Manager")
    page.screenshot(path=str(screenshots / "n1-board-name-light.png"))
    assert page.errors == []


def test_negative_twin_an_unnamed_board_is_called_by_its_address(page_factory):
    page = page_factory()
    page.wait_for_selector(f'.board-item[data-board="{BOARD_USB}"]', timeout=T)
    address = BOARD_USB.split("@", 1)[1]
    name = rail(page, BOARD_USB).locator('[data-testid="rail-name"]')
    expect(name).to_have_text(address)
    assert name.get_attribute("title") == BOARD_USB

    rail(page, BOARD_USB).click()
    expect(page).to_have_title(f"{address} · Harness Manager")
    page.locator('[data-action="open"]').click()
    header = page.locator('[data-testid="header-name"]')
    expect(header).to_contain_text("on shell 0x3f1a560f", timeout=T)
    assert page.locator('[data-testid="header-sub"]').count() == 0
    assert page.errors == []
