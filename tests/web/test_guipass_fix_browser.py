"""Four small page defects found in the first Harness Manager pass on real hardware
(board 2, 2026-10-04): the picker's stale error, the DUT console's stale route text, the
silent DUT uart1, the header's missing uptime. Each fix has a twin that proves the
neighbouring behaviour did not move. A headless system Chrome on the real daemon over the
demo engines.
"""

from __future__ import annotations

import re

import pytest

from harness_manager.demo import BOARD_USB
from tests.web import nav, wb

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}
OVERLAYS = re.compile(r"/boards/[^/]+/overlays")


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def workbench(page, bid):
    nav.open_board(page, bid)
    nav.tab(page, "workbench")
    by_id(page, "program-card").wait_for(timeout=T)


def fail_overlays(page) -> None:
    page.route(OVERLAYS, lambda route: route.fulfill(status=503, json={
        "ok": False, "error": {"code": 7, "name": "UNREACHABLE",
                               "message": "the board did not answer"}}))


def reread(page, bid) -> None:
    page.evaluate("(bid) => import('./js/store.js').then((m) => m.loadOverlays(bid))", bid)


# --- 1. the picker's stale error ---------------------------------------------------------------


def test_a_failed_re_read_keeps_the_list_and_says_so_beside_it(page_factory):
    page = page_factory(**APP)
    workbench(page, BOARD_USB)
    expect(by_id(page, "design-picker")).to_contain_text("Pick a design (5 load on this shell)")
    fail_overlays(page)
    reread(page, BOARD_USB)
    expect(by_id(page, "design-picker")).to_contain_text("Pick a design (5 load on this shell)")
    assert "could not be read" not in by_id(page, "design-picker").inner_text()
    wb.open_picker(page)
    expect(by_id(page, "design-list-stale")).to_contain_text("The last read failed: UNREACHABLE")
    expect(page.locator('[data-testid="design-list"] [data-overlay]')).to_have_count(6)
    # Read again, with the board answering: the line goes, the list stays
    page.unroute(OVERLAYS)
    page.locator('[data-action="design-reread"]').click()
    expect(by_id(page, "design-list-stale")).to_have_count(0)
    expect(page.locator('[data-testid="design-list"] [data-overlay]')).to_have_count(6)
    assert page.errors == []


def test_opening_the_picker_re_reads_after_a_failure_and_not_otherwise(page_factory):
    page = page_factory(**APP)
    hits: list[str] = []
    page.on("request", lambda r: hits.append(r.url) if OVERLAYS.search(r.url) else None)
    workbench(page, BOARD_USB)
    expect(by_id(page, "design-picker")).to_contain_text("Pick a design")
    wb.open_picker(page)
    by_id(page, "design-picker").click()                 # close
    before = len(hits)
    by_id(page, "design-picker").click()                 # open again: the list is good
    page.wait_for_timeout(400)
    assert len(hits) == before                           # twin: no re-read of a good list
    by_id(page, "design-picker").click()                 # close
    fail_overlays(page)
    reread(page, BOARD_USB)
    expect(by_id(page, "design-picker")).to_contain_text("Pick a design")
    page.unroute(OVERLAYS)
    before = len(hits)
    by_id(page, "design-picker").click()                 # open with an error set
    page.wait_for_function("() => true")
    expect(by_id(page, "design-list-stale")).to_have_count(0, timeout=T)
    assert len(hits) > before


def test_negative_twin_with_no_list_the_picker_still_says_it_could_not_read(page_factory):
    page = page_factory(**APP)
    fail_overlays(page)
    nav.open_board(page, BOARD_USB)
    nav.tab(page, "workbench")
    by_id(page, "program-card").wait_for(timeout=T)
    expect(by_id(page, "design-picker")).to_contain_text("The design list could not be read")
    wb.open_picker(page)
    expect(by_id(page, "design-list")).to_contain_text("UNREACHABLE: the board did not answer")
    expect(by_id(page, "design-list-stale")).to_have_count(0)
