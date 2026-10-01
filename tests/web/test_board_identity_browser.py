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
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("identity_api", sim=True)]
T = 10_000


def open_board(page):
    page.locator(f'.board-item[data-board="{BOARD_FIELDED}"]').click()
    page.wait_for_selector(f'main[data-board="{BOARD_FIELDED}"], [data-action="open"]', timeout=T)
    if page.locator(f'main[data-board="{BOARD_FIELDED}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    nav.land(page, "board")                       # UI v2: a board opens on the Workbench
    nav.board_page(page, "access")                # UI v2: the Overview's Board tile is gone
    page.wait_for_selector(f'{CARD} [data-status]', timeout=T)


# UI v2 (BOARD): the identity is Board > Access's card (label, IP, MAC as rows); Fix identity…
# opens the same dialog in a modal
CARD = '[data-testid="access-identity"]'
ROW = f'{CARD} [data-status]'
WARN = '[data-testid="access-identity-warning"]'
FIX = '[data-action="access-identity-fix"]'


def expect_identity(row, label, ip, mac):
    for text in (label, ip, mac):
        expect(row).to_contain_text(text, timeout=T)


def test_a_clash_warns_on_the_tile_and_the_dialog_fixes_it_with_the_typed_phrase(page_factory,
                                                                                  daemon):
    sim = daemon.app.state.identity
    sim.board2_as_board1(BOARD_FIELDED)
    page = page_factory()
    open_board(page)
    row = page.locator(ROW)
    expect(row).to_have_attribute("data-status", "clash", timeout=T)
    expect_identity(row, "MPS3-01", "192.168.10.101/24", "02:00:00:4d:50:53")
    warn = page.locator(WARN)
    expect(warn).to_contain_text("Identity clash:")
    expect(warn).to_contain_text("the same MAC as mps3-01")
    assert "err" in warn.get_attribute("class")
    page.locator(FIX).click()
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
    expect(page.locator(WARN)).to_have_count(0)
    expect(page.locator(FIX)).to_have_count(0)
    expect(page.locator('[data-testid="identity-dialog"]')).to_have_count(0)      # the modal closed
    assert sim.posts == [{"confirm": "MPS3-02", "from_hub": True}]
    assert not page.errors, page.errors


def test_twin_a_board_matching_its_hub_entry_has_no_warning_and_no_button(page_factory, daemon):
    daemon.app.state.identity.matching(BOARD_FIELDED)
    page = page_factory()
    open_board(page)
    row = page.locator(ROW)
    expect(row).to_have_attribute("data-status", "ok", timeout=T)
    expect_identity(row, "MPS3-02", "192.168.11.101/24", "02:00:00:00:02:fe")
    expect(page.locator(WARN)).to_have_count(0)
    expect(page.locator(FIX)).to_have_count(0)
    assert not page.errors, page.errors


def test_twin_a_netbooted_board_shows_the_refusal_and_nothing_to_press(page_factory, daemon):
    sim = daemon.app.state.identity
    sim.board2_as_board1(BOARD_FIELDED, refusal={
        "name": "REFUSED", "message": "this board has no persistent store for an identity (a "
        "netboot, or no user microSD): its identity comes from the stage0 bake",
        "hint": "re-bake stage0 for this board"})
    page = page_factory()
    open_board(page)
    page.locator(FIX).click()
    ref = page.locator('[data-testid="identity-refusal"]')
    expect(ref).to_contain_text("stage0 bake", timeout=T)
    expect(ref).to_contain_text("re-bake stage0")
    expect(page.locator('[data-action="identity-fix-confirm"]')).to_have_count(0)
    expect(page.locator('[data-testid="identity-phrase"]')).to_have_count(0)
    page.locator('[data-action="identity-fix-cancel"]').click()
    expect(page.locator('[data-testid="identity-dialog"]')).to_have_count(0)
    assert sim.posts == []
    assert not page.errors, page.errors


# --- V7-ALIGN: board 2 before its identity bake (net-protocol v0.16 as shipped) -----------------


def test_v7_board2s_default_label_says_identity_not_set_not_a_clash(page_factory, daemon):
    daemon.app.state.identity.board2_tonight(BOARD_FIELDED, board1_mac="02:00:00:00:01:fe")
    page = page_factory()
    open_board(page)
    row = page.locator(ROW)
    expect(row).to_have_attribute("data-status", "unset", timeout=T)
    warn = page.locator(WARN)
    expect(warn).to_contain_text("Identity not set (default label, MAC)")
    assert "identity not set: identity not set" not in warn.inner_text().lower()
    assert "clash" not in warn.inner_text().lower()
    assert not page.errors, page.errors


def test_v7_twin_board2_sharing_board1s_old_mac_is_a_mac_clash_only(page_factory, daemon):
    daemon.app.state.identity.board2_tonight(BOARD_FIELDED)          # board 1: the same old MAC
    page = page_factory()
    open_board(page)
    row = page.locator(ROW)
    expect(row).to_have_attribute("data-status", "clash", timeout=T)
    warn = page.locator(WARN)
    expect(warn).to_contain_text("Identity clash: this board reports the same MAC as mps3-01")
    assert "same label" not in warn.inner_text()
    assert not page.errors, page.errors
