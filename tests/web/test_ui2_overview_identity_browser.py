"""UI v2 round 3, lane UI2-OVERVIEW: the Overview's identity strip and its identity-clash row, from
the board's own identity check (BOARD-ID, ``BoardInfo.net_identity``), over the T14 mock and its
identity sim (``tests/fakes/idn_mock_identity.py``, built with the service's own rules). The
label, IP and MAC are what the board reports; a clash is one attention row with its one fix.
Each behaviour has its twin.
"""

from __future__ import annotations

import re

import pytest

from harness_manager.demo import BOARD_FIELDED
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("identity_api", sim=True)]
T = nav.T


def overview(page):
    nav.open_board(page, BOARD_FIELDED)
    nav.tab(page, "overview")
    page.wait_for_selector('[data-testid="ov-identity"]', timeout=T)


def test_a_board_reporting_board_1s_identity_shows_it_and_needs_its_fix(page_factory, daemon):
    daemon.app.state.identity.board2_as_board1(BOARD_FIELDED)
    page = page_factory()
    overview(page)
    strip = nav.by_id(page, "ov-identity")
    expect(strip.locator('[data-testid="ov-label"]')).to_have_text("MPS3-01", timeout=T)
    expect(strip).to_contain_text("192.168.10.101/24")
    mac = strip.locator('[data-testid="ov-mac"]')
    expect(mac).to_have_text("02:00:00:4d:50:53")
    expect(mac).to_have_class(re.compile(r"\bov-id-bad\b"))              # the clash mark
    assert "image default" not in strip.inner_text()                     # MPS3-01 is a bake
    row = page.locator('[data-attention="identity"]')
    expect(row).to_contain_text("Identity clash", timeout=T)
    row.locator('[data-action="attention-identity"]').click()             # Fix identity…
    expect(nav.by_id(page, "board-page-access")).to_be_visible(timeout=T)
    assert not page.errors, page.errors


def test_twin_a_board_that_matches_its_hub_entry_needs_nothing(page_factory, daemon):
    daemon.app.state.identity.matching(BOARD_FIELDED)
    page = page_factory()
    overview(page)
    strip = nav.by_id(page, "ov-identity")
    expect(strip.locator('[data-testid="ov-label"]')).to_have_text("MPS3-02", timeout=T)
    expect(strip.locator('[data-testid="ov-mac"]')).to_have_text("02:00:00:00:02:fe")
    assert "ov-id-bad" not in (strip.locator('[data-testid="ov-mac"]').get_attribute("class") or "")
    page.wait_for_timeout(500)
    assert page.locator('[data-attention="identity"]').count() == 0
    assert not page.errors, page.errors


def test_twin_the_image_default_label_says_so(page_factory, daemon):
    daemon.app.state.identity.board2_tonight(BOARD_FIELDED, board1_mac="02:00:00:00:01:fe")
    page = page_factory()
    overview(page)
    strip = nav.by_id(page, "ov-identity")
    expect(strip.locator('[data-testid="ov-label"]')).to_have_text("MPS3", timeout=T)
    expect(strip).to_contain_text("image default")
    assert not page.errors, page.errors
