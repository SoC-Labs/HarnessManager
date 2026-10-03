"""QUICKWINS G2: the board's revision, MCC firmware and user microSD, as chips on the Overview's
identity strip and on the bring-up scan rows. A value other than Rev C / MCC v1.3.2 is
"untested". Each behaviour has its twin.
"""
from __future__ import annotations

import pytest


from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
from tests.web import nav
from tests.web.test_bringup_browser import T, by, demo, over_usb  # noqa: F401  (the fixture)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser


def overview(page, bid):
    nav.open_board(page, bid)
    nav.tab(page, "overview")
    page.wait_for_selector('[data-testid="ov-identity"]', timeout=T)


def test_overview_chips_for_a_tested_board_and_a_linux_card(demo):
    page = demo.page()
    overview(page, BOARD_LINUX)
    expect(by(page, "ov-fact-rev")).to_have_text("Rev C", timeout=T)
    expect(by(page, "ov-fact-mcc")).to_have_text("MCC v1.3.2")
    expect(by(page, "ov-fact-sd")).to_have_text("microSD: yes")
    assert "untested" not in by(page, "ov-facts").inner_text()
    assert not page.errors, page.errors


def test_twin_an_untested_revision_and_firmware_say_so_and_no_card_says_no(demo):
    demo.engine.set_facts(BOARD_LINUX, revision="HBI0309B", mcc_fw="v1.4.1", user_sd=False)
    page = demo.page()
    overview(page, BOARD_LINUX)
    expect(by(page, "ov-fact-rev")).to_have_text("Rev B: untested", timeout=T)
    expect(by(page, "ov-fact-mcc")).to_have_text("MCC v1.4.1: untested")
    expect(by(page, "ov-fact-sd")).to_have_text("microSD: no")
    # checked, never changed: nothing here offers an MCC update
    assert "update" not in by(page, "ov-facts").inner_text().lower()
    assert not page.errors, page.errors


def test_twin_a_bare_metal_board_shows_no_microsd_chip(demo):
    page = demo.page()
    overview(page, BOARD_V011)
    expect(by(page, "ov-fact-rev")).to_have_text("Rev C", timeout=T)
    expect(by(page, "ov-fact-sd")).to_have_count(0)


def test_the_scan_row_shows_the_revision_and_firmware_from_log_txt(demo):
    page = demo.page()
    over_usb(page)
    expect(by(page, "usb-fact-rev")).to_have_text("Rev C", timeout=T)
    expect(by(page, "usb-fact-mcc")).to_have_text("MCC v1.3.2")


def test_twin_an_untested_log_is_flagged_on_the_scan_row(demo):
    drive = demo.state_dir / "demo-fixtures" / "bringup" / "V2M-MPS3"
    page = demo.page()
    drive.mkdir(parents=True, exist_ok=True)
    (drive / "LOG.TXT").write_bytes(b"ARM V2M-MPS3 Firmware v1.4.1\r\nMotherBoard Revision B Variant A\r\n")
    over_usb(page)
    expect(by(page, "usb-fact-mcc")).to_have_text("MCC v1.4.1: untested", timeout=T)
    expect(by(page, "usb-fact-rev")).to_have_text("Rev B: untested")
