"""FIX-PACK-6 in the browser: the estimated download bar (item 4) and the unverified design
(item 3), on the real daemon over the demo engine, driven by the service's own events on its
bus (as the MPS3 pack's deploy and the reboot job publish them). Each has its negative twin.
"""

from __future__ import annotations

import pytest

from harness_manager.core.events import Event
from harness_manager.demo import BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}
CLEARING, TOTAL = 169_000, 2_467_736


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def push(engine, done: int, *, estimated: bool = False) -> None:
    data = {"phase": "push", "bytes": done, "total": TOTAL}
    if estimated:
        data["estimated"] = True
    engine.bus.publish(Event("deploy.progress", BOARD_USB, data))


def test_an_estimate_is_hatched_with_a_tilde_and_snaps_to_the_real_bytes(page_factory, engine):
    page = page_factory(**APP)
    nav.open_board(page, BOARD_USB)
    nav.tab(page, "workbench")
    by_id(page, "program-card").wait_for(timeout=T)
    engine.bus.publish(Event("deploy.started", BOARD_USB, {"overlay": "nanosoc",
                                                           "rm_id": "0x01000001",
                                                           "keep_on_card": False}))
    push(engine, 0)
    push(engine, CLEARING)                                   # the clearing: real, 6 %
    bar = by_id(page, "deploy-progress")
    expect(bar).to_be_visible(timeout=T)
    step = bar.locator('[data-step="push"]')
    expect(step).to_contain_text("6%", timeout=T)
    expect(bar).not_to_have_attribute("data-estimated", "1")
    push(engine, 1_234_000, estimated=True)                  # the partial in flight
    expect(step).to_contain_text("~50%", timeout=T)
    expect(step.locator(".pgm-track > i")).to_have_class("est", timeout=T)
    expect(bar).to_have_attribute("data-estimated", "1")
    expect(by_id(page, "deploy-phase")).to_contain_text("~", timeout=T)
    # Twin: an older estimate never moves the bar back
    push(engine, 900_000, estimated=True)
    page.wait_for_timeout(300)
    expect(step).to_contain_text("~50%")
    # The frame completes: the real bytes, solid
    push(engine, TOTAL)
    expect(step).to_contain_text("100%", timeout=T)
    expect(step).not_to_contain_text("~")
    expect(step.locator(".pgm-track > i")).not_to_have_class("est")
    engine.bus.publish(Event("deploy.done", BOARD_USB, {"rm_id": "0x01000001",
                                                        "verified": True, "overlay": "nanosoc",
                                                        "seconds": 70.0, "card": None}))
    assert page.errors == []


def test_an_unverified_design_is_marked_until_a_deploy_proves_it(page_factory, engine):
    page = page_factory(**APP)
    nav.open_board(page, BOARD_USB)                          # nanosoc 0x01000001 is loaded
    fact = by_id(page, "fact-design")
    expect(fact).to_contain_text("nanosoc", timeout=T)
    expect(by_id(page, "design-unverified")).to_have_count(0)
    text = ("the board reports nanosoc (0x01000001) but no debug port answers: greybox is "
            "probably resident (known issue, Linux v2.0.0)")
    engine.bus.publish(Event("design.check", BOARD_USB, {
        "state": "unverified", "rm_id": "0x01000001", "rm_name": "nanosoc", "idcode": "",
        "expected_idcode": "0x6ba00477", "text": text, "reason": "all zeroes",
        "after": "mcc reboot", "at": 1_790_000_000.0}))
    chip = by_id(page, "design-unverified")
    expect(chip).to_be_visible(timeout=T)
    expect(chip).to_have_text("unverified")
    assert "greybox is probably resident" in (chip.get_attribute("title") or "")
    # the Activity row says it, as a warning
    page.locator('[data-action="activity"]').click()
    expect(by_id(page, "activity-table")).to_contain_text("design UNVERIFIED after mcc reboot",
                                                          timeout=T)
    nav.close_activity(page)
    # Twin: a verified check, or a deploy that proves the design, shows no such chip
    engine.bus.publish(Event("design.check", BOARD_USB, {
        "state": "verified", "rm_id": "0x01000001", "rm_name": "nanosoc",
        "idcode": "0x6ba00477", "text": "the board reports nanosoc (0x01000001) and its debug "
        "port answers (IDCODE 0x6ba00477)", "after": "mcc reboot"}))
    expect(chip).to_have_count(0, timeout=T)
    engine.bus.publish(Event("design.check", BOARD_USB, {
        "state": "unverified", "rm_id": "0x01000001", "rm_name": "nanosoc", "text": text,
        "after": "mcc reboot"}))
    expect(chip).to_be_visible(timeout=T)
    engine.bus.publish(Event("deploy.done", BOARD_USB, {"rm_id": "0x01000001",
                                                        "verified": True, "overlay": "nanosoc",
                                                        "seconds": 1.0, "card": None}))
    expect(chip).to_have_count(0, timeout=T)
    assert page.errors == []
