"""FIX-PACK-7 in the browser: a Program refused because OpenOCD on the board could not be
stopped first (DEBUG-DOWN-FIRST), then the ARMED "Program anyway".

The positive case is the REAL daemon app over ``DemoEngine(showcase=True)`` (its Linux board,
whose ``mps3-debug down`` the demo makes fail with the service's own words) in the system
Chrome. The negative twin is a board someone else holds by the time "Program anyway" is
clicked (the T14 mock's hub sim): the same gate as Program refuses it with the holder's reason,
and nothing is sent.
"""

from __future__ import annotations

from typing import Any

import pytest

from harness_manager.core.events import Event
from harness_manager.demo import BOARD_USB
from harness_manager.demo_showcase import BOARD_LINUX
from harness_manager.services import debug_onboard as OB
from tests.web import nav, wb
from tests.web import test_demo_all_browser as _demo

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 1000}
WHY = "the board's SSH failed: ssh: connect to host 192.168.10.104 port 22: Connection timed out"
HELD = "Program is for the lease holder only: alice@lab-pc-07 holds this board"

showcase = _demo.showcase          # the fixture: the demo over the real daemon app
by_id = nav.by_id


def arm(page: Any) -> None:
    page.locator('[data-testid="arm-program"] input').check()


def program_refused(page: Any, design: str) -> Any:
    """Pick ``design``, arm, Program: the daemon refuses (15); returns the outcome box."""
    nav.section(page, "program")
    wb.pick(page, design, wait=True)
    arm(page)
    page.locator('[data-action="program"]').click()
    out = by_id(page, "deploy-outcome")
    expect(out).to_have_attribute("data-state", "failed", timeout=T)
    return out


def test_a_refused_program_offers_program_anyway_armed_and_it_swaps_with_a_warning(showcase):
    eng = showcase.engine
    eng.set_debug_down(BOARD_LINUX, WHY)
    page = showcase.page(**APP)
    nav.open_board(page, BOARD_LINUX)
    out = program_refused(page, "nanosoc_upy")
    # the refusal, in the service's words, and its hint
    expect(out).to_contain_text(f"`mps3-debug down` failed before the swap ({WHY})")
    expect(out).to_contain_text(OB.DOWN_FIRST_HINT)
    expect(out).to_contain_text("REFUSED")
    assert eng.called("deploy.debug_down") == [(BOARD_LINUX, False)]
    anyway = page.locator('[data-action="program_anyway"]')
    expect(anyway).to_contain_text("Program anyway")
    expect(by_id(page, "anyway-why")).to_contain_text(
        "OpenOCD on the board could not be stopped before the swap (mps3-debug down). Program "
        "anyway swaps all the same: on Linux v2.0.0 OpenOCD on the board may still drive JTAG "
        "during the reconfiguration. Tick Arm, then Program anyway.")
    # two steps: the refused run cleared Arm, so a click alone runs nothing
    expect(anyway).to_have_attribute("aria-disabled", "true")
    anyway.click(force=True)
    expect(out).to_have_attribute("data-state", "refused", timeout=T)
    expect(out).to_contain_text("not armed")
    assert eng.called("deploy.debug_down") == [(BOARD_LINUX, False)]      # nothing was sent
    arm(page)
    expect(anyway).not_to_have_attribute("aria-disabled", "true")
    anyway.click()
    expect(out).to_have_attribute("data-state", "done", timeout=T)
    expect(by_id(page, "deploy-warning")).to_contain_text(
        f"swapping anyway (forced) although `mps3-debug down` failed ({WHY}): on Linux v2.0.0 "
        "OpenOCD on the board may still drive JTAG during the reconfiguration")
    assert eng.called("deploy.debug_down")[-1] == (BOARD_LINUX, True)     # force: true
    expect(anyway).to_have_count(0)                                       # the offer is spent
    assert not page.errors, page.errors


def test_twin_a_board_whose_down_works_programs_with_no_offer(showcase):
    page = showcase.page(**APP)
    nav.open_board(page, BOARD_LINUX)
    nav.section(page, "program")
    wb.pick(page, "nanosoc_upy", wait=True)
    arm(page)
    page.locator('[data-action="program"]').click()
    expect(by_id(page, "deploy-outcome")).to_have_attribute("data-state", "done", timeout=T)
    expect(page.locator('[data-action="program_anyway"]')).to_have_count(0)
    expect(by_id(page, "deploy-warning")).to_have_count(0)
    assert showcase.engine.called("deploy.debug_down") == [(BOARD_LINUX, False)]
    assert not page.errors, page.errors


@pytest.mark.week_plan("hub_api", sim=True)
def test_twin_program_anyway_on_a_board_someone_else_now_holds_is_refused_with_the_holder(
        page_factory, daemon, engine):
    sim = daemon.app.state.sim
    sim.behind_hub(BOARD_USB, lease="mine")
    engine.set_debug_down(BOARD_USB, WHY)
    page = page_factory(**APP)
    nav.open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    out = program_refused(page, "nanosoc")
    anyway = page.locator('[data-action="program_anyway"]')
    expect(anyway).to_be_visible()
    # alice takes the lease; the page hears it and reads the lease again
    sim.behind_hub(BOARD_USB, lease="other")
    engine.bus.publish(Event("lease.state", BOARD_USB, {"state": "held"}))
    expect(by_id(page, "reason-program")).to_have_text(HELD, timeout=T)
    arm(page)
    expect(anyway).to_have_attribute("aria-disabled", "true")
    assert "primary" not in (anyway.get_attribute("class") or "")
    anyway.click(force=True)                       # the interlock answers; nothing is sent
    expect(out).to_have_attribute("data-state", "refused", timeout=T)
    expect(out).to_contain_text(HELD)
    assert engine.called("deploy.debug_down") == [(BOARD_USB, False)]     # never forced
    assert len(engine.called("deploy.deploy")) == 1
    assert page.errors == []
