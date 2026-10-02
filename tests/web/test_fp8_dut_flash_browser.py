"""FIX-PACK-8 in the browser: Program warns before a design whose boot code writes the DUT's
flash (the MPS3 pack declares nanosoc_multicore: one byte at 0x20000 onward on every boot until
Linux v2.1) and needs the word, typed: MULTICORE.

The REAL daemon app over ``DemoEngine(showcase=True)`` in the system Chrome; the showcase's Linux
board lists nanosoc_multicore for its own shell. Each behaviour has a negative twin: another
design shows no warning and needs no word; a word typed wrong runs nothing; a board someone else
holds is still refused by the lease rule, the word typed or not.
"""

from __future__ import annotations

from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX
from tests.web import nav, wb
from tests.web import test_demo_all_browser as _demo

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 1000}
TEXT = ("nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte at 0x20000 onward) "
        "on every boot, until Linux v2.1: it damages a MicroPython image there (nanosoc_upy). "
        "Type MULTICORE to program it anyway.")
NEEDS_WORD = ("type MULTICORE to program nanosoc_multicore: its boot code writes the DUT's flash")
HELD = "Program is for the lease holder only: alice@lab-pc-07 holds this board"

showcase = _demo.showcase          # the fixture: the demo over the real daemon app
by_id = nav.by_id


def arm(page: Any) -> None:
    page.locator('[data-testid="arm-program"] input').check()


def test_multicore_warns_and_program_needs_the_typed_word(showcase):
    eng = showcase.engine
    page = showcase.page(**APP)
    nav.open_board(page, BOARD_LINUX)
    nav.section(page, "program")
    wb.open_picker(page)
    expect(page.locator('[data-overlay="nanosoc_multicore"] .tag.dutflash')).to_have_text(
        "writes DUT flash")
    expect(page.locator('[data-overlay="nanosoc_upy"] .tag.dutflash')).to_have_count(0)
    page.keyboard.press("Escape")
    wb.pick(page, "nanosoc_multicore", wait=True)
    expect(by_id(page, "dut-flash-warning")).to_have_text(TEXT)
    expect(page.locator('[data-pf="dutflash"]')).to_have_text("writes the DUT's flash")
    program = page.locator('[data-action="program"]')
    arm(page)
    # armed, but no word: Program stays off and says why; a forced click runs nothing
    expect(by_id(page, "reason-program")).to_have_text(NEEDS_WORD)
    expect(program).to_have_attribute("aria-disabled", "true")
    program.click(force=True)
    out = by_id(page, "deploy-outcome")
    expect(out).to_have_attribute("data-state", "refused", timeout=T)
    expect(out).to_contain_text(NEEDS_WORD)
    assert eng.called("deploy.deploy") == []
    # twin: the word typed wrong (case counts) is still no
    word = by_id(page, "dut-flash-word")
    word.fill("multicore")
    expect(program).to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "reason-program")).to_have_text(NEEDS_WORD)
    word.fill(" MULTICORE ")                     # surrounding spaces do not count (the CLI's rule)
    expect(program).not_to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "reason-program")).to_contain_text("ready: Program swaps the partition "
                                                          "to nanosoc_multicore")
    program.click()
    expect(out).to_have_attribute("data-state", "done", timeout=T)
    expect(out).to_contain_text("Programmed nanosoc_multicore")
    assert eng.called("deploy.allow_dut_flash_write") == [(BOARD_LINUX, "nanosoc_multicore")]
    assert [c[1] for c in eng.called("deploy.deploy")] == ["nanosoc_multicore"]
    # each program is a fresh choice: the field is empty again, and Program waits for it
    expect(word).to_have_value("")
    arm(page)
    expect(by_id(page, "reason-program")).to_have_text(NEEDS_WORD)
    assert not page.errors, page.errors


def test_twin_another_design_shows_no_warning_and_needs_no_word(showcase):
    eng = showcase.engine
    page = showcase.page(**APP)
    nav.open_board(page, BOARD_LINUX)
    nav.section(page, "program")
    wb.pick(page, "nanosoc_multicore", wait=True)
    expect(by_id(page, "dut-flash-warning")).to_be_visible()
    by_id(page, "dut-flash-word").fill("MULTICORE")
    wb.pick(page, "nanosoc_upy", wait=True)       # a new pick: the warning and the word go
    expect(by_id(page, "dut-flash")).to_have_count(0)
    expect(page.locator('[data-pf="dutflash"]')).to_have_count(0)
    arm(page)
    expect(by_id(page, "reason-program")).to_contain_text("ready: Program swaps the partition "
                                                          "to nanosoc_upy")
    page.locator('[data-action="program"]').click()
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "done", timeout=T)
    assert eng.called("deploy.allow_dut_flash_write") == []           # never sent for upy
    # and back on multicore the word is asked again (the earlier one was not kept)
    wb.pick(page, "nanosoc_multicore", wait=True)
    expect(by_id(page, "dut-flash-word")).to_have_value("")
    arm(page)
    expect(by_id(page, "reason-program")).to_have_text(NEEDS_WORD)
    assert not page.errors, page.errors


def test_twin_the_lease_rule_still_refuses_a_board_someone_else_holds(showcase):
    eng = showcase.engine
    eng._board(BOARD_LEASED).multicore_here = True     # multicore loads on alice's board too
    page = showcase.page(**APP)
    nav.open_board(page, BOARD_LEASED)
    nav.section(page, "program")
    wb.pick(page, "nanosoc_multicore", wait=True)
    expect(by_id(page, "dut-flash-warning")).to_have_text(TEXT)
    by_id(page, "dut-flash-word").fill("MULTICORE")
    arm(page)
    program = page.locator('[data-action="program"]')
    expect(by_id(page, "reason-program")).to_have_text(HELD, timeout=T)
    expect(program).to_have_attribute("aria-disabled", "true")
    program.click(force=True)
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "refused", timeout=T)
    assert eng.called("deploy.deploy") == [] and eng.called("deploy.allow_dut_flash_write") == []
    assert not page.errors, page.errors
