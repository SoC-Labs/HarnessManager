"""BRINGUP-USB in the browser: Add > Over USB and the bring-up wizard, end to end on the demo.

The REAL daemon over ``DemoEngine(showcase=True)`` (what ``app --demo`` serves) in the system
Chrome. The demo's new board (``mps3@usb:/dev/ttyUSB20``, demo_showcase's bringup-usb block)
is found by the scan, added, backed up, written, rebooted and witnessed; its Ethernet twin at
192.168.10.101 comes up bare-metal, or in stage0 RESCUE for the Linux bundle. Every
behaviour has its negative twin. Nothing here touches a device: the demo's writes are in
memory and its drive is a folder in the test's tmp dir.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from harness_manager import demo_catalog as cat
from harness_manager.demo import DemoEngine
from harness_manager.demo_showcase import BOARD_NEW_ETH, BOARD_NEW_USB
from harness_manager.services import bringup
from tests.fakes.t14_mock_api import real_daemon
from tests.web.conftest import dump_failed_pages

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 20_000
TOKEN = "bringup-usb"


class Demo:
    def __init__(self, browser: Any, daemon: Any, engine: DemoEngine) -> None:
        self.browser, self.daemon, self.engine = browser, daemon, engine
        self.contexts: list[Any] = []
        self.pages: list[Any] = []

    def page(self, scheme: str = "light") -> Any:
        ctx = self.browser.new_context(viewport={"width": 1440, "height": 900},
                                       color_scheme=scheme, reduced_motion="reduce")
        self.contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error"
                and "status of 4" not in m.text and "status of 5" not in m.text else None)
        page.goto(self.daemon.ui_url)
        page.wait_for_selector(".board-item", timeout=T)
        self.pages.append(page)
        return page

    def example(self, n: int) -> str:
        return self.engine.bringup_examples[n]["path"]


@pytest.fixture
def demo(browser, tmp_path, monkeypatch, request) -> Iterator[Demo]:
    monkeypatch.delenv(cat.UPDATE_ENV, raising=False)
    monkeypatch.delenv(bringup.SD_FLASH_ENV, raising=False)
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_OVERLAY_DIRS", raising=False)
    sdir = tmp_path / "demo"
    engine = DemoEngine(speed=0.25, showcase=True, state_dir=sdir)
    try:
        with real_daemon(engine, token=TOKEN, state_dir=sdir) as d:
            show = Demo(browser, d, engine)
            show.state_dir = sdir
            try:
                yield show
            finally:
                dump_failed_pages(request, show.pages)
                for ctx in show.contexts:
                    ctx.close()
    finally:
        engine.close_all()


def by(page: Any, testid: str) -> Any:
    return page.locator(f'[data-testid="{testid}"]')


def over_usb(page: Any) -> None:
    page.evaluate("import('./js/modal.js').then(m => m.openModal('add', {mode: 'usb'}))")
    expect(by(page, "add-over-usb")).to_be_visible(timeout=T)
    page.locator('[data-action="usb-scan"]').click()
    page.wait_for_function("() => !document.querySelector('[data-action=\"usb-scan\"]')"
                           ".getAttribute('aria-busy')", timeout=T)


def add_and_open(page: Any) -> None:
    over_usb(page)
    expect(by(page, "usb-board")).to_have_count(1, timeout=T)
    page.locator('[data-action="usb-add"]').click()
    expect(by(page, "bringup")).to_be_visible(timeout=T)


def check(page: Any, path: str, *, sign: bool = True) -> None:
    """Check the bundle; ``sign``: then type its INSTALL UNSIGNED <sha8> (a refused bundle has
    no phrase to type)."""
    by(page, "bundle-path").fill(path)
    page.locator('[data-action="bundle-check"]').click()
    expect(by(page, "bundle-check")).to_be_visible(timeout=T)
    if sign and by(page, "bundle-check").get_attribute("data-refused") == "no":
        by(page, "bundle-unsigned-phrase").fill(by(page, "bundle-unsigned-want").inner_text())


def back_up(page: Any) -> None:
    by(page, "bu-step-backup").locator('[data-action="sd_backup"]').click()
    expect(by(page, "bu-backup-path")).to_be_visible(timeout=T)


def write_usb(page: Any) -> None:
    page.locator('[data-testid="arm-bu-write"] input').check()
    page.locator('[data-action="bu_write"]').click()
    expect(by(page, "bu-step-write")).to_have_attribute("data-state", "done", timeout=T)


def reboot_and_witness(page: Any) -> None:
    step = by(page, "bu-step-reboot")
    step.locator("label.arm input").check()
    step.locator('[data-action="reboot"]').click()
    expect(by(page, "bu-reboot-result").locator(".rc.ok")).to_be_visible(timeout=T)
    page.locator('[data-action="bu_witness"]').click()


def bring_up(page: Any, path: str) -> None:
    add_and_open(page)
    check(page, path)
    back_up(page)
    write_usb(page)
    reboot_and_witness(page)


# --- Add > Over USB -----------------------------------------------------------------------------


def test_over_usb_lists_the_debug_usb_with_its_mcc_drive_and_ethernet_then_opens_the_wizard(demo):
    page = demo.page()
    over_usb(page)
    row = by(page, "usb-board")
    expect(row).to_have_count(1, timeout=T)
    expect(by(page, "usb-mcc")).to_contain_text("/dev/ttyUSB20")
    expect(by(page, "usb-mcc-answer")).to_have_text("answers")
    expect(by(page, "usb-drive")).to_contain_text("loads MB/HBI0309C/AN536/an536.bit")
    expect(by(page, "usb-drive")).to_contain_text("MB BIOS mbb_v141.ebf (never written)")
    expect(by(page, "usb-eth")).to_have_text("nothing answers")
    page.locator('[data-action="usb-add"]').click()
    expect(by(page, "bringup")).to_be_visible(timeout=T)
    expect(page.locator(f'.board-item[data-board="{BOARD_NEW_USB}"]')).to_be_visible()
    assert BOARD_NEW_USB in demo.engine.open_boards()
    assert not page.errors, page.errors


def test_twin_none_found_says_what_to_check_and_offers_nothing_to_add(demo):
    demo.engine._board(BOARD_NEW_USB).reachable = False          # unplugged
    page = demo.page()
    over_usb(page)
    none = by(page, "usb-none")
    expect(none).to_contain_text("No MPS3 Debug USB found on this PC.", timeout=T)
    for words in ("Debug USB cable", "power", "drive: it must be mounted"):
        expect(none).to_contain_text(words)
    expect(page.locator('[data-action="usb-add"]')).to_have_count(0)
    assert not page.errors, page.errors


def test_two_found_are_both_listed_and_the_ethernet_answer_is_not_guessed(demo):
    eng = demo.engine
    first = eng._board(BOARD_NEW_USB)
    second = replace(first, candidate=replace(first.candidate,
                                              board_id="mps3@usb:/dev/ttyUSB30"))
    eng._boards[second.candidate.board_id] = second
    page = demo.page()
    over_usb(page)
    expect(by(page, "usb-board")).to_have_count(2, timeout=T)
    expect(by(page, "usb-note")).to_contain_text("2 Debug USBs")
    expect(page.locator('[data-testid="usb-eth"]')).to_have_count(0)    # not paired
    assert not page.errors, page.errors


def test_a_drive_without_its_port_and_a_port_without_its_drive_say_what_is_lost(demo):
    eng = demo.engine
    b = eng._board(BOARD_NEW_USB)
    links = b.candidate.links
    b.candidate = replace(b.candidate, links=tuple(lk for lk in links if lk.kind.value == "usb_msd"))
    page = demo.page()
    over_usb(page)
    expect(by(page, "usb-problem")).to_contain_text("no MCC serial port with it", timeout=T)
    expect(by(page, "usb-mcc")).to_contain_text("none: no MCC serial port")
    b.candidate = replace(b.candidate, links=tuple(lk for lk in links if lk.kind.value != "usb_msd"))
    page.locator('[data-action="usb-scan"]').click()
    expect(by(page, "usb-problem")).to_contain_text("no V2M-MPS3 drive with it", timeout=T)
    expect(by(page, "usb-drive")).to_contain_text("none: no V2M-MPS3 drive")
    assert not page.errors, page.errors


# --- the source --------------------------------------------------------------------------------


def test_a_bundle_shows_its_base_bit_and_what_it_will_write(demo):
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(0))
    chk = by(page, "bundle-check")
    expect(chk).to_have_attribute("data-refused", "no")
    expect(by(page, "bundle-bit")).to_contain_text("MB/HBI0309C/Nanosoc/nanosoc.bit")
    expect(by(page, "bundle-bit")).to_contain_text("sha256")
    expect(by(page, "bundle-bit")).to_contain_text("USERID 0xc8551081")
    expect(by(page, "bundle-files")).to_contain_text("4 files")
    expect(by(page, "bundle-overlays")).to_contain_text("mps3.overlay_dirs")
    expect(by(page, "bu-step-source")).to_have_attribute("data-state", "done")
    assert not page.errors, page.errors


def test_a_bundle_is_unsigned_red_banner_its_sha256_and_the_typed_phrase(demo):
    page = demo.page()
    add_and_open(page)
    banner = by(page, "bu-unsigned-banner")
    expect(banner).to_have_text("Unsigned: Harness Manager cannot check where this came from; "
                                "only install a bundle you built or got from SoC Labs directly.")
    expect(banner).to_have_class("outcome err bu-unsigned")
    check(page, demo.example(0), sign=False)
    sha = bringup.check_bundle(demo.example(0), demo.state_dir / "t-work").sha256
    expect(by(page, "bundle-sha")).to_contain_text(sha)
    expect(by(page, "bundle-sha")).to_contain_text("of the folder's manifest")
    expect(by(page, "bundle-sha")).to_contain_text("LC_ALL=C sort -z")
    expect(by(page, "bundle-unsigned-want")).to_have_text(f"INSTALL UNSIGNED {sha[:8]}")
    expect(by(page, "bu-step-source")).to_have_attribute("data-state", "todo")
    expect(by(page, "reason-sd_backup")).to_contain_text(
        f"type INSTALL UNSIGNED {sha[:8]} in step 1 to install this unsigned bundle")
    by(page, "bundle-unsigned-phrase").fill(f"INSTALL UNSIGNED {sha[:8].upper()}")
    expect(by(page, "bu-step-source")).to_have_attribute("data-state", "done")
    assert not page.errors, page.errors


def test_twin_a_wrong_sha8_keeps_every_write_closed(demo):
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(1), sign=False)                     # the Linux zip
    expect(by(page, "bundle-sha")).to_contain_text("of the zip file itself")
    want = by(page, "bundle-unsigned-want").inner_text()
    wrong = want[:-8] + ("0" * 8 if not want.endswith("0" * 8) else "1" * 8)
    by(page, "bundle-unsigned-phrase").fill(wrong)
    expect(by(page, "bu-step-source")).to_have_attribute("data-state", "todo")
    page.locator('[data-action="sd_backup"]').click(force=True)     # the interlock answers
    expect(by(page, "bu-backup-result")).to_contain_text("Nothing was run.")
    page.locator('[data-testid="arm-bu-write"] input').check()
    expect(by(page, "reason-bu_write")).to_contain_text(f"type {want} in step 1")
    assert demo.engine.called("storage.install") == []
    assert not page.errors, page.errors


def test_twin_a_signed_release_shows_no_unsigned_banner(demo):
    page = demo.page()
    add_and_open(page)
    page.get_by_role("button", name="A signed harness release").click()
    expect(by(page, "bu-unsigned-banner")).to_have_count(0)
    expect(by(page, "bundle-unsigned-phrase")).to_have_count(0)
    assert not page.errors, page.errors


def test_twin_an_ebf_bundle_is_refused_and_nothing_can_be_written(demo):
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(2))
    expect(by(page, "bundle-check")).to_have_attribute("data-refused", "yes")
    expect(by(page, "bundle-unsigned-phrase")).to_have_count(0)   # nothing to confirm
    expect(by(page, "bundle-problem")).to_contain_text(".ebf (board-controller firmware) is "
                                                       "never written")
    expect(by(page, "bundle-refused")).to_contain_text("Nothing was written.")
    expect(by(page, "reason-bu_write")).to_contain_text("the bundle is refused (step 1)")
    assert not page.errors, page.errors


def test_releases_are_refused_plainly_while_no_signing_key_exists(demo):
    from harness_manager.services.update.trust import TrustStore

    demo.engine.update.trust = TrustStore(pinned=())
    demo.engine.update.channels.trust = demo.engine.update.trust
    page = demo.page()
    add_and_open(page)
    page.get_by_role("button", name="A signed harness release").click()
    refused = by(page, "release-refused")
    expect(refused).to_contain_text("Releases are refused here until signing keys exist", timeout=T)
    expect(refused).to_contain_text("docs/KEYS.md")
    expect(refused).to_contain_text("REFUSED: cannot verify channel.json: this build has no "
                                    "pinned update-signing keys")
    page.locator('[data-action="release-read"]').click()
    expect(by(page, "release-error")).to_contain_text("REFUSED", timeout=T)
    expect(by(page, "release-rows")).to_have_count(0)                # never a fake success
    assert not page.errors, page.errors


def test_twin_with_a_trusted_key_the_releases_are_listed(demo):
    page = demo.page()
    add_and_open(page)
    page.get_by_role("button", name="A signed harness release").click()
    expect(by(page, "release-refused")).to_have_count(0)
    page.locator('[data-action="release-read"]').click()
    expect(by(page, "release-rows").locator("li")).not_to_have_count(0, timeout=T)
    assert not page.errors, page.errors


# --- the backup gate and the write method switch ------------------------------------------------


def test_no_backup_refuses_the_write_and_says_so(demo):
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(0))
    page.locator('[data-testid="arm-bu-write"] input').check()
    expect(by(page, "reason-bu_write")).to_contain_text("back up the SD first (step 2)")
    page.locator('[data-action="bu_write"]').click(force=True)      # the interlock answers
    expect(by(page, "bu-write-result")).to_contain_text("Nothing was run.")
    assert demo.engine.called("storage.install") == []
    assert not page.errors, page.errors


def test_twin_after_the_backup_the_usb_write_runs_with_the_slow_write_warning(demo):
    page = demo.page()
    add_and_open(page)
    expect(by(page, "bu-usb-warning")).to_contain_text(
        "A USB write can take 5 minutes: do not unplug, power off or start a second write.")
    check(page, demo.example(0))
    back_up(page)
    write_usb(page)
    expect(by(page, "bu-write-result")).to_contain_text("added to mps3.overlay_dirs")
    assert len(demo.engine.called("storage.install")) == 1
    assert not page.errors, page.errors


def test_the_card_reader_is_disabled_with_the_reason_while_the_switch_is_off(demo):
    page = demo.page()
    add_and_open(page)
    expect(by(page, "bu-reader-off-note")).to_contain_text("bringup.sd_flash", timeout=T)
    page.get_by_role("button", name="SD card in this PC's card reader").click()
    expect(by(page, "bu-reader-disabled")).to_contain_text("bringup.sd_flash", timeout=T)
    expect(by(page, "bu-reader-disabled")).to_contain_text("HARNESS_MANAGER_BRINGUP_SD_FLASH=on")
    assert not page.errors, page.errors


def test_twin_switch_on_but_no_cardwriter_routes_is_a_placeholder_not_an_error(demo, monkeypatch):
    monkeypatch.setenv(bringup.SD_FLASH_ENV, "on")
    page = demo.page()
    # integ: SD-FLASH's routes are merged; a build without them answers 404
    page.route("**/api/v1/cardwriter/**", lambda r: r.fulfill(
        status=404, content_type="application/json", body='{"error":"not found"}'))
    add_and_open(page)
    page.get_by_role("button", name="SD card in this PC's card reader").click()
    expect(by(page, "bu-reader-disabled")).to_contain_text("no card-reader writer", timeout=T)
    expect(page.locator('[data-testid="reader-device-files"]')).to_have_count(0)
    assert not page.errors, page.errors


# --- reboot, witness, the OS step, next ----------------------------------------------------------


def test_bare_metal_comes_up_and_next_opens_it_on_access(demo):
    page = demo.page()
    bring_up(page, demo.example(0))
    expect(by(page, "bu-step-reboot")).to_have_attribute("data-state", "done", timeout=T)
    expect(by(page, "bu-witness-result")).to_contain_text("a harness answers at 192.168.10.101")
    expect(by(page, "bu-step-os")).to_have_count(0)                   # bare metal: no OS step
    expect(by(page, "bu-pc-hint")).to_contain_text("192.168.10.0/24")
    page.locator('[data-action="bu-next"]').click()
    expect(page.locator(f'main[data-board="{BOARD_NEW_ETH}"]')).to_be_visible(timeout=T)
    expect(page.locator('[data-board-page="access"], [data-testid="board-page-access"]')
           .first).to_be_visible(timeout=T)
    assert BOARD_NEW_ETH in demo.engine.open_boards()
    assert not page.errors, page.errors


def test_twin_a_dark_board_times_out_and_offers_the_restore(demo, monkeypatch):
    monkeypatch.setattr(bringup, "DEFAULT_WITNESS_S", 2.0)
    demo.engine.bringup_dark = True
    page = demo.page()
    bring_up(page, demo.example(0))
    timeout = by(page, "bu-timeout")
    expect(timeout).to_contain_text("Nothing answered at 192.168.10.101 within", timeout=T)
    expect(page.locator('[data-action="bu-next"]')).to_be_disabled()
    page.locator('[data-testid="arm-bu-restore"] input').check()
    page.locator('[data-action="bu_restore"]').click()
    expect(by(page, "bu-restore-result")).to_contain_text("restored the configuration SD",
                                                          timeout=T)
    assert len(demo.engine.called("storage.restore")) == 1
    assert not page.errors, page.errors


def test_linux_comes_up_in_rescue_and_the_os_step_offers_the_whole_card_image(demo):
    page = demo.page()
    bring_up(page, demo.example(1))
    expect(by(page, "bu-witness-result")).to_contain_text("stage0 RESCUE answers", timeout=T)
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible()
    expect(by(page, "bu-os-why-network")).to_contain_text("comes with Linux v2.1")
    expect(by(page, "bu-os-why-network")).to_contain_text("it does not write the card")
    expect(os_step.locator('[data-option="network"] input')).to_be_disabled()
    expect(by(page, "bu-os-why-reader")).to_contain_text("bringup.sd_flash")    # the switch is off
    expect(os_step.locator('[data-option="skip"] input')).to_be_enabled()
    expect(page.locator('[data-action="bu-next"]')).to_be_disabled()
    expect(by(page, "bu-next-why")).to_contain_text("stage0 RESCUE")
    os_step.locator('[data-option="skip"] input').check()
    expect(os_step).to_have_attribute("data-state", "done")
    assert not page.errors, page.errors


def test_twin_the_slot_image_is_never_offered_as_the_card(demo, monkeypatch):
    monkeypatch.setenv(bringup.SD_FLASH_ENV, "on")
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(1))
    expect(by(page, "bundle-check")).to_contain_text("never written to a card")
    # the page's own rule for the card image path (bringup.js cardImageWhy)
    why = page.evaluate("p => import('./js/bringup.js').then(m => m.cardImageWhy(p))",
                        "/tmp/x/mps3-harness-2.0.0/linux_slot.img")
    assert "SLOT image" in why and "never boots" in why
    assert page.evaluate("p => import('./js/bringup.js').then(m => m.cardImageWhy(p))",
                         "/tmp/x/card.img") == ""
    assert not page.errors, page.errors


def test_an_interrupted_install_shows_the_restore_card_first(demo):
    demo.engine.set_sd_journal(BOARD_NEW_USB, {"op": "install", "state": "interrupted",
                                               "current": "MB/HBI0309C/Nanosoc/nanosoc.bit",
                                               "backup": {"path": "/tmp/b.zip"}})
    page = demo.page()
    add_and_open(page)
    rec = by(page, "bringup").locator('[data-testid="sd-recovery"]')
    expect(rec).to_be_visible(timeout=T)
    first = page.locator('[data-testid="bringup"] .modal-pad > *').first
    expect(first).to_have_attribute("data-testid", "sd-recovery")
    assert not page.errors, page.errors


def test_twin_a_clean_card_shows_no_restore_card(demo):
    page = demo.page()
    add_and_open(page)
    expect(by(page, "bu-step-source")).to_be_visible()
    expect(by(page, "bringup").locator('[data-testid="sd-recovery"]')).to_have_count(0)
    assert not page.errors, page.errors


def test_the_wizard_reopens_where_it_was_from_board_versions(demo):
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(0))
    page.locator('[data-action="bu-close"]').click()
    expect(by(page, "bringup")).to_have_count(0)
    page.evaluate(f"import('./js/store.js').then(m => m.navigate({BOARD_NEW_USB!r}, 'board/versions'))")
    page.locator('[data-testid="config-sd-bringup"] [data-action="bringup-open"]').click()
    expect(by(page, "bundle-check")).to_be_visible(timeout=T)        # kept
    assert not page.errors, page.errors


# --- the card reader (SD-FLASH's routes, faked to the contract) --------------------------------


def reader_on(demo: Demo, monkeypatch, **kw: Any) -> Any:
    from tests.fakes.bringup_cardwriter import FakeCardwriter

    monkeypatch.setenv(bringup.SD_FLASH_ENV, "on")
    return FakeCardwriter(**kw).attach(demo.daemon.app)


def check_image(page: Any, path: str, *, sign: bool = True) -> None:
    """The OS step: the whole-card image, Check (POST /cardwriter/check), then its INSTALL
    UNSIGNED <sha8>."""
    by(page, "bu-card-image").fill(path)
    page.locator('[data-action="bu-card-check"]').click()
    expect(by(page, "bu-card-check")).to_be_visible(timeout=T)
    if sign:
        by(page, "bu-os-unsigned-phrase").fill(by(page, "bu-os-unsigned-want").inner_text())


def pick_card(page: Any, kind: str, phrase: str) -> None:
    from tests.fakes.bringup_cardwriter import DEVICE

    page.locator(f'[data-testid="reader-device-{kind}"]').select_option(DEVICE["id"])
    page.locator(f'[data-testid="reader-confirm-{kind}"]').fill(phrase)


def demo_reader(demo: Demo, monkeypatch) -> Any:
    """bringup.sd_flash on, with --demo's simulated card readers (cardwriter.demo_writer: temp
    files and a V2M-MPS3 folder in the demo's state dir; never a device)."""
    monkeypatch.setenv(bringup.SD_FLASH_ENV, "on")
    return demo.state_dir / "cardwriter-demo" / "V2M-MPS3"


def test_the_reader_door_writes_the_files_then_asks_for_the_card_back_no_mcc_reboot(demo,
                                                                                     monkeypatch):
    card = demo_reader(demo, monkeypatch)
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(0))
    back_up(page)
    expect(by(page, "bu-reader-off-note")).to_have_count(0)       # the twin: the door is open
    page.get_by_role("button", name="SD card in this PC's card reader").click()
    expect(by(page, "bu-reader-disabled")).to_have_count(0, timeout=T)
    picker = page.locator('[data-testid="reader-device-files"]')
    expect(picker.locator("option", has_text="/dev/sdb")).to_be_enabled()
    expect(picker.locator("option", has_text="/dev/sdc")).to_be_disabled()   # not mounted
    expect(picker.locator("option", has_text="/dev/sdc")).to_contain_text("is not mounted")
    picker.select_option(label=picker.locator("option", has_text="/dev/sdb").inner_text())
    phrase = "WRITE SD/MMC 31.9 GB"                       # the writer's own, as it lists it
    page.locator('[data-testid="reader-confirm-files"]').fill("WRITE SD/MMC 31914983424")
    expect(by(page, "reason-bu_reader")).to_contain_text(f"type {phrase} to confirm")
    page.locator('[data-testid="reader-confirm-files"]').fill(phrase)
    page.locator('[data-testid="arm-bu-reader"] input').check()
    page.locator('[data-action="bu_reader"]').click()
    expect(by(page, "bu-reader-result")).to_contain_text("files written and verified", timeout=T)
    assert (card / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.bit").is_file()
    expect(by(page, "bu-put-back")).to_contain_text("put the card back in the board's "
                                                    "configuration SD slot and power the board on")
    expect(by(page, "bu-step-reboot").locator('[data-action="reboot"]')).to_have_count(0)
    expect(by(page, "reason-bu_witness")).to_contain_text("put the card back")
    demo.engine.bringup_dark = False
    from harness_manager.demo_showcase import _bringup_come_up

    _bringup_come_up(demo.engine, {"impl": "bare-metal"})          # the card is back, the board on
    by(page, "bu-replaced").check()
    page.locator('[data-action="bu_witness"]').click()
    expect(by(page, "bu-witness-result")).to_contain_text("a harness answers", timeout=T)
    assert demo.engine.called("storage.install") == [] and demo.engine.called("controller.reboot") == []
    assert not page.errors, page.errors


def test_twin_the_reader_door_with_the_unsigned_phrase_untyped_writes_nothing(demo, monkeypatch):
    card = demo_reader(demo, monkeypatch)
    page = demo.page()
    add_and_open(page)
    check(page, demo.example(0), sign=False)
    page.get_by_role("button", name="SD card in this PC's card reader").click()
    picker = page.locator('[data-testid="reader-device-files"]')
    expect(picker).to_be_visible(timeout=T)
    picker.select_option(label=picker.locator("option", has_text="/dev/sdb").inner_text())
    page.locator('[data-testid="reader-confirm-files"]').fill("WRITE SD/MMC 31.9 GB")
    page.locator('[data-testid="arm-bu-reader"] input').check()
    expect(by(page, "reason-bu_reader")).to_contain_text("INSTALL UNSIGNED")
    page.locator('[data-action="bu_reader"]').click(force=True)      # the interlock answers
    expect(by(page, "bu-reader-result")).to_contain_text("Nothing was run.")
    assert not (card / "MB" / "HBI0309C" / "Nanosoc").exists()
    # and the service itself refuses a write without the phrase (never only the page)
    r = page.evaluate("""p => import('./js/api.js').then(m => m.bringupCall('bringupCardReader', {},
        {bundle: p, device_id: 'x', confirm: 'x'}).then(() => 'ok', e => e.errName + ': ' + e.message))""",
                      demo.example(0))
    assert r.startswith("REFUSED: not confirmed: this bundle is unsigned"), r
    assert not page.errors, page.errors


def test_the_os_step_shows_a_reader_without_privilege_the_command_and_never_escalates(
        demo, monkeypatch):
    from tests.fakes.bringup_cardwriter import DEVICE

    reader_on(demo, monkeypatch, privilege=True)
    page = demo.page()
    bring_up(page, demo.example(1))
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible(timeout=T)
    os_step.locator('[data-option="reader"] input').check()
    check_image(page, demo.engine.bringup_card_image)
    pick_card(page, "card", f"WRITE {DEVICE['model']} {DEVICE['size_bytes']}")
    page.locator('[data-testid="arm-bu-os"] input').check()
    page.locator('[data-action="bu_os"]').click()
    res = by(page, "bu-os-result")
    expect(res).to_contain_text("Harness Manager never escalates", timeout=T)
    expect(res).to_contain_text("sudo dd if=")
    expect(os_step).to_have_attribute("data-state", "todo")              # not written
    assert not page.errors, page.errors


def test_the_os_step_writes_a_whole_card_image_as_kind_card(demo, monkeypatch):
    from tests.fakes.bringup_cardwriter import DEVICE

    fake = reader_on(demo, monkeypatch)
    page = demo.page()
    bring_up(page, demo.example(1))
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible(timeout=T)
    os_step.locator('[data-option="reader"] input').check()
    card = demo.engine.bringup_card_image
    check_image(page, card)
    expect(by(page, "bu-card-sha")).to_contain_text(cw_sha(card))
    pick_card(page, "card", f"WRITE {DEVICE['model']} {DEVICE['size_bytes']}")
    page.locator('[data-testid="arm-bu-os"] input').check()
    page.locator('[data-action="bu_os"]').click()
    expect(by(page, "bu-os-result")).to_contain_text("whole-card image written and verified",
                                                     timeout=T)
    (w,) = fake.writes
    assert w["kind"] == "card" and w["source"] == card
    assert w["confirm_unsigned"] == f"INSTALL UNSIGNED {cw_sha(card)[:8]}"
    expect(by(page, "bu-os-back")).to_contain_text("power-cycle the board")
    assert not page.errors, page.errors


def test_twin_the_os_step_refuses_a_slot_image_as_the_card(demo, monkeypatch):
    from tests.fakes.bringup_cardwriter import DEVICE

    fake = reader_on(demo, monkeypatch)
    page = demo.page()
    bring_up(page, demo.example(1))
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible(timeout=T)
    os_step.locator('[data-option="reader"] input').check()
    by(page, "bu-card-image").fill("/home/me/mps3-harness-2.0.0/linux_slot.img")
    expect(by(page, "bu-card-why")).to_contain_text("SLOT image")
    pick_card(page, "card", f"WRITE {DEVICE['model']} {DEVICE['size_bytes']}")
    page.locator('[data-testid="arm-bu-os"] input').check()
    expect(by(page, "reason-bu_os")).to_contain_text("SLOT image")
    page.locator('[data-action="bu_os"]').click(force=True)
    expect(by(page, "bu-os-result")).to_contain_text("Nothing was run.")
    assert fake.writes == []
    assert not page.errors, page.errors


# --- a signed release over the Debug USB (the demo's catalogue trusts its throwaway key) --------


def pick(page: Any, version: str) -> None:
    page.get_by_role("button", name="A signed harness release").click()
    page.locator('[data-action="release-read"]').click()
    row = by(page, "release-rows").locator("li", has_text=version).first
    expect(row).to_be_visible(timeout=T)
    row.locator("input").check()
    expect(by(page, "release-plan")).to_be_visible(timeout=T)


def written_not_running(demo: Demo, monkeypatch) -> None:
    """RELEASE-PIPE's proven outcome of a USB-only install of a bare-metal release: the SD is
    written and the board rebooted, and nothing can confirm the harness without Ethernet, so
    the install ends written-not-running (exit 6) BY DESIGN. The demo catalogue's parts are
    placeholders its own checks refuse, so the outcome is the executor's, scripted here."""
    from harness_manager.demo_showcase import _bringup_come_up
    from harness_manager.services import harness_catalog
    from harness_manager.services.update.executor import RESULT_WRITTEN, UpdateOutcome

    def install(self, session, plan, approval, verified, *, by="user"):
        _bringup_come_up(demo.engine, {"impl": "bare-metal"})     # it comes up on Ethernet
        return UpdateOutcome(session.candidate.board_id, plan.version, RESULT_WRITTEN,
                             "written, not running: after the reboot the board does not report "
                             f"harness {plan.version} (shell_id: nothing to compare)",
                             evidence={"summary": "REBOOT witnessed (MCC banner)"},
                             backup={"path": "/tmp/bk.zip", "sha256": "0" * 64},
                             restore_hint="harness rollback TARGET --backup /tmp/bk.zip")

    monkeypatch.setattr(harness_catalog.HarnessCatalog, "install", install)


def test_a_bare_metal_release_over_usb_is_written_then_witnessed_never_a_failure(demo,
                                                                                 monkeypatch):
    written_not_running(demo, monkeypatch)
    page = demo.page()
    add_and_open(page)
    pick(page, "1.1.0")
    phrase = by(page, "release-phrase")
    expect(phrase).to_be_visible()           # the static is unknown over USB: a typed re-key
    expect(by(page, "reason-bu_write")).to_contain_text("to confirm the re-key")
    phrase.fill(by(page, "release-plan").locator("label code").inner_text())
    back_up(page)
    write_usb(page)
    res = by(page, "bu-write-result")
    expect(res).to_contain_text("written to the configuration SD, and the board rebooted")
    expect(res).to_contain_text("step 4 waits for it on Ethernet")
    expect(res.locator(".rc.ok")).to_be_visible()                   # rc 0, never a failure
    expect(by(page, "bu-release-rebooted")).to_be_visible()
    page.locator('[data-action="bu_witness"]').click()
    expect(by(page, "bu-witness-result")).to_contain_text("a harness answers", timeout=T)
    assert not page.errors, page.errors


def test_twin_a_linux_release_over_usb_shows_the_planners_refusal(demo):
    page = demo.page()
    add_and_open(page)
    page.get_by_role("button", name="A signed harness release").click()
    by(page, "release-all").check()                       # 2.0.0, the Linux harness, is beta
    page.locator('[data-action="release-read"]').click()
    rows = by(page, "release-rows")
    linux = rows.locator("li", has_text="linux")
    expect(linux.first).to_be_visible(timeout=T)
    linux.first.locator("input").check()
    expect(by(page, "release-blocker").first).to_contain_text("The planner refuses this release "
                                                              "here:", timeout=T)
    expect(by(page, "release-linux-usb")).to_contain_text("whole-card image in step 5")
    expect(by(page, "reason-bu_write")).to_contain_text("the planner refuses this release here")
    assert not page.errors, page.errors


def test_twin_written_not_running_on_a_board_with_ethernet_stays_a_failure(demo, monkeypatch):
    from harness_manager.demo_showcase import BOARD_V011
    from tests.web import nav

    written_not_running(demo, monkeypatch)
    page = demo.page()
    nav.open_board(page, BOARD_V011)
    page.evaluate(f"import('./js/bringup.js').then(m => m.openBringup({BOARD_V011!r}))")
    expect(by(page, "bringup")).to_be_visible(timeout=T)
    pick(page, "1.1.1")
    back_up(page)
    page.locator('[data-testid="arm-bu-write"] input').check()
    page.locator('[data-action="bu_write"]').click()
    res = by(page, "bu-write-result")
    expect(res).to_contain_text("ACTION_FAILED", timeout=T)
    expect(res).to_contain_text("written, not running")
    expect(by(page, "bu-step-write")).to_have_attribute("data-state", "todo")
    assert not page.errors, page.errors


def test_nothing_is_scanned_until_scan_is_clicked(demo):
    page = demo.page()
    page.evaluate("import('./js/modal.js').then(m => m.openModal('add', {mode: 'usb'}))")
    expect(by(page, "add-over-usb")).to_be_visible(timeout=T)
    expect(by(page, "usb-board")).to_have_count(0)
    assert demo.engine.called("controller.command") == []        # no MCC was typed at
    page.locator('[data-action="usb-scan"]').click()
    expect(by(page, "usb-board")).to_have_count(1, timeout=T)
    assert len(demo.engine.called("controller.command")) == 1     # one "?" at its prompt
    assert not page.errors, page.errors


# --- the proposed identity (david 2 Oct, D4a), handed to the identity writer --------------------


def test_next_proposes_a_name_from_the_mcc_usb_serial_a_random_mac_and_a_pool_ip(demo):
    """Lane IDENTITY (david 2 Oct): the name from the serial, a RANDOM MAC (never the
    serial's), the pool's next free IP; "Open it on Ethernet and name it…" opens the ONE "Name
    this board" dialog on the Ethernet board, pre-filled with them."""
    page = demo.page()
    bring_up(page, demo.example(0))
    expect(by(page, "bu-witness-result")).to_contain_text("a harness answers", timeout=T)
    expect(by(page, "bu-id-serial")).to_have_text("DEMO20")
    expect(by(page, "bu-id-label")).to_have_text("MPS3-MO20")
    expect(by(page, "bu-id-ip")).to_have_text("192.168.10.110")         # the pool's first
    mac = by(page, "bu-id-mac").inner_text()
    assert mac.startswith("02:") and not mac.startswith("02:00:00:")       # random, not derived
    expect(by(page, "bu-id-same-net")).to_have_text(
        "This PC must be on the same /24 (e.g. 192.168.10.1/24).")
    page.locator('[data-action="bu-next"]').click()
    modal = by(page, "name-board-modal")
    expect(modal).to_be_visible(timeout=T)
    expect(page.locator(f'main[data-board="{BOARD_NEW_ETH}"]')).to_be_visible(timeout=T)
    # the twin: the demo's new board runs the bare-metal harness, which has no identity store
    expect(by(page, "identity-refusal")).to_contain_text(
        "This board cannot take a name: the bare-metal harness has no identity store")
    expect(page.locator('[data-action="identity-fix-confirm"]')).to_have_count(0)
    assert not page.errors, page.errors


IDENTITY_URL = re.compile(r".*/api/v1/boards/[^/]+/identity(\?.*)?$")


def linux_identity(*, refusal: dict | None = None, reported: dict | None = None) -> dict:
    rep = {"label": "MPS3", "hostname": "mps3", "ip": "192.168.10.101/24",
           "mac": "02:00:00:4d:50:53", "impl": "linux", "feature": True, "feature_known": True,
           "source": {"label": "default", "ip": "default", "mac": "default"}, "persist": True,
           **(reported or {})}
    return {"status": "unset", "level": "warn", "reported": rep, "hub": None, "findings": [],
            "fix": {"changes": [], "phrase": "MPS3", "notes": [], "ready": False,
                    "refusal": refusal}, "notes": [], "live": True, "checked_at": "now"}


def serve_identity(page: Any, status: dict | None, posts: list[dict]) -> None:
    import json as _json

    def identity(route: Any) -> None:
        if route.request.method == "POST":
            posts.append(_json.loads(route.request.post_data or "{}"))
            route.fulfill(status=202, content_type="application/json",
                          body=_json.dumps({"ok": True, "job": "bu2-identity-job"}))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"ok": True, "board_id": "x", "identity": status}))

    page.route(IDENTITY_URL, identity)


def open_identity(page: Any, prefill: dict, impl: str = "linux") -> Any:
    from harness_manager.demo_showcase import BOARD_V011
    from tests.web import nav

    nav.open_board(page, BOARD_V011)
    page.evaluate("a => import('./js/modal.js').then(m => m.openModal('name-board', a))",
                  {"bid": BOARD_V011, "prefill": prefill, "impl": impl})
    modal = by(page, "name-board-modal")
    expect(modal).to_be_visible(timeout=T)
    return modal


PREFILL = {"label": "MPS3-MO20", "ip": "192.168.10.110/24", "mac": "02:13:8c:d4:4d:9c"}


def test_twin_a_harness_that_predates_identity_set_is_told_so_and_nothing_is_sent(demo):
    posts: list[dict] = []
    page = demo.page()
    serve_identity(page, linux_identity(refusal={
        "name": "UNAVAILABLE", "hint": "",
        "message": "pending the Linux lead's interface: this harness image has no identity verbs "
                   "(net-protocol v0.16 `identity`/`identity_set`, images rc2_v7 and later)"},
        reported={"feature": False}), posts)
    open_identity(page, PREFILL)
    expect(by(page, "identity-refusal")).to_contain_text(
        "This board's harness predates identity_set: pending the Linux lead's interface",
        timeout=T)
    expect(page.locator('[data-action="identity-fix-confirm"]')).to_have_count(0)
    assert posts == []
    assert not page.errors, page.errors


def test_twin_a_harness_that_reports_no_identity_at_all_predates_it_too(demo):
    posts: list[dict] = []
    page = demo.page()
    serve_identity(page, None, posts)
    open_identity(page, PREFILL)
    expect(by(page, "identity-refusal")).to_contain_text(
        "does not report its identity: it predates net-protocol v0.16 (identity_set)", timeout=T)
    expect(page.locator('[data-action="identity-fix-confirm"]')).to_have_count(0)
    assert posts == []
    assert not page.errors, page.errors


def test_over_usb_is_shown_by_default_in_the_add_dialog_no_setting_hides_it(demo):
    """david 2 Oct (decision 5): v0.1.1 ships "Over USB" un-hidden."""
    from harness_manager.settings import rows

    page = demo.page()
    page.evaluate("import('./js/modal.js').then(m => m.openModal('add'))")
    usb = page.get_by_role("button", name="Over USB (a new board plugged into this PC)")
    expect(usb).to_be_visible(timeout=T)
    expect(usb).to_be_enabled()
    usb.click()
    expect(by(page, "add-over-usb")).to_be_visible(timeout=T)
    # the twin: no settings row turns the door off (bringup.sd_flash is the card reader's only)
    names = {r.key for r in rows.CORE_ROWS}
    assert {n for n in names if n.startswith("bringup.")} == {"bringup.sd_flash",
                                                               "bringup.sd_flash_max"}
    assert not page.errors, page.errors



def cw_sha(path: str) -> str:
    from harness_manager.services import unsigned

    return unsigned.file_sha256(Path(path))


def test_twin_the_os_step_needs_the_images_unsigned_phrase_and_a_wrong_sha8_writes_nothing(
        demo, monkeypatch):
    from tests.fakes.bringup_cardwriter import DEVICE

    fake = reader_on(demo, monkeypatch)
    page = demo.page()
    bring_up(page, demo.example(1))
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible(timeout=T)
    os_step.locator('[data-option="reader"] input').check()
    card = demo.engine.bringup_card_image
    check_image(page, card, sign=False)
    expect(os_step.locator('[data-testid="bu-unsigned-banner"]')).to_contain_text(
        "Unsigned: Harness Manager cannot check where this came from")
    want = f"INSTALL UNSIGNED {cw_sha(card)[:8]}"
    expect(by(page, "bu-os-unsigned-want")).to_have_text(want)
    expect(by(page, "bu-card-sha")).to_contain_text("sha256sum CARD.img")
    wrong = want[:-8] + ("0" * 8 if not want.endswith("0" * 8) else "1" * 8)
    by(page, "bu-os-unsigned-phrase").fill(wrong)
    pick_card(page, "card", f"WRITE {DEVICE['model']} {DEVICE['size_bytes']}")
    page.locator('[data-testid="arm-bu-os"] input').check()
    expect(by(page, "reason-bu_os")).to_contain_text(f"type {want} to install this unsigned "
                                                     "card image")
    page.locator('[data-action="bu_os"]').click(force=True)        # the interlock answers
    expect(by(page, "bu-os-result")).to_contain_text("Nothing was run.")
    assert fake.writes == []
    # and the service refuses a write without it (never only the page)
    r = page.evaluate("""a => import('./js/api.js').then(m => m.bringupCall('cardwriterWrite', {},
        {device_id: a[1], kind: 'card', source: a[0], confirm: a[2]}).then(() => 'ok',
        e => e.errName + ': ' + e.message))""",
                      [card, DEVICE["id"], f"WRITE {DEVICE['model']} {DEVICE['size_bytes']}"])
    assert r.startswith("REFUSED: not confirmed: this card image is unsigned"), r
    assert not page.errors, page.errors
