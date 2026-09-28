"""BOARD-ID in the browser: the Board tile's Identity row and the "Fix identity" dialog.

Over the T14 mock (``tests/fakes/idn_mock_identity.py``: the identity as a test scripts it,
built with the service's own ``compare``/``plan_fix``). Board 2 reporting board 1's label, IP
and MAC is an error on the tile; the dialog lists the changes from the hub entry and asks for
the typed phrase before it posts. The twins: a board that matches its hub entry shows no
warning and no button; a netbooted board's dialog shows the refusal and offers nothing to press.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_FIELDED

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("identity_api", sim=True)]
T = 10_000


def open_board(page):
    page.locator(f'.board-item[data-board="{BOARD_FIELDED}"]').click()
    page.wait_for_selector(f'main[data-board="{BOARD_FIELDED}"], [data-action="open"]', timeout=T)
    if page.locator(f'main[data-board="{BOARD_FIELDED}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="tile-identity"]', timeout=T)


def test_a_clash_warns_on_the_tile_and_the_dialog_fixes_it_with_the_typed_phrase(page_factory,
                                                                                  daemon):
    sim = daemon.app.state.identity
    sim.board2_as_board1(BOARD_FIELDED)
    page = page_factory()
    open_board(page)
    row = page.locator('[data-testid="tile-identity"]')
    expect(row).to_have_attribute("data-status", "clash", timeout=T)
    expect(row).to_contain_text("MPS3-01 · 192.168.10.101/24 · 02:00:00:4d:50:53")
    warn = page.locator('[data-testid="tile-identity-warning"]')
    expect(warn).to_contain_text("Identity clash:")
    expect(warn).to_contain_text("the same MAC as mps3-01")
    assert "err" in warn.get_attribute("class")
    page.locator('[data-action="identity-fix"]').click()
    dialog = page.locator('[data-testid="identity-dialog"]')
    expect(dialog).to_contain_text("mps3_02_pl")
    changes = page.locator('[data-testid="identity-changes"] li')
    expect(changes).to_have_count(3)
    expect(page.locator('[data-testid="identity-changes"] li[data-field="label"]')).to_contain_text(
        "MPS3-01 → MPS3-02")
    expect(page.locator('[data-testid="identity-changes"] li[data-field="mac"]')).to_contain_text(
        "02:00:00:00:02:fe")
    expect(dialog).to_contain_text("never an MCC REBOOT")
    confirm = page.locator('[data-action="identity-fix-confirm"]')
    expect(confirm).to_be_disabled()
    page.locator('[data-testid="identity-phrase"]').fill("MPS3-2")
    expect(confirm).to_be_disabled()                 # a near miss is not the phrase
    assert sim.posts == []
    page.locator('[data-testid="identity-phrase"]').fill("MPS3-02")
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(row).to_have_attribute("data-status", "ok", timeout=T)
    expect(page.locator('[data-testid="tile-identity-warning"]')).to_have_count(0)
    expect(page.locator('[data-action="identity-fix"]')).to_have_count(0)
    assert sim.posts == [{"confirm": "MPS3-02", "from_hub": True}]
    assert not page.errors, page.errors


def test_twin_a_board_matching_its_hub_entry_has_no_warning_and_no_button(page_factory, daemon):
    daemon.app.state.identity.matching(BOARD_FIELDED)
    page = page_factory()
    open_board(page)
    row = page.locator('[data-testid="tile-identity"]')
    expect(row).to_have_attribute("data-status", "ok", timeout=T)
    expect(row).to_contain_text("MPS3-02 · 192.168.11.101/24 · 02:00:00:00:02:fe")
    expect(page.locator('[data-testid="tile-identity-warning"]')).to_have_count(0)
    expect(page.locator('[data-action="identity-fix"]')).to_have_count(0)
    assert not page.errors, page.errors


def test_twin_a_netbooted_board_shows_the_refusal_and_nothing_to_press(page_factory, daemon):
    sim = daemon.app.state.identity
    sim.board2_as_board1(BOARD_FIELDED, refusal={
        "name": "REFUSED", "message": "this board has no persistent store for an identity (a "
        "netboot, or no user microSD): its identity comes from the stage0 bake",
        "hint": "re-bake stage0 for this board"})
    page = page_factory()
    open_board(page)
    page.locator('[data-action="identity-fix"]').click()
    ref = page.locator('[data-testid="identity-refusal"]')
    expect(ref).to_contain_text("stage0 bake", timeout=T)
    expect(ref).to_contain_text("re-bake stage0")
    expect(page.locator('[data-action="identity-fix-confirm"]')).to_have_count(0)
    expect(page.locator('[data-testid="identity-phrase"]')).to_have_count(0)
    page.locator('[data-action="identity-fix-cancel"]').click()
    expect(page.locator('[data-testid="identity-dialog"]')).to_have_count(0)
    assert sim.posts == []
    assert not page.errors, page.errors
