"""The web UI over the REAL stack: harness-manager-daemon (T13) -> Engine -> MPS3 pack -> pyverify ->
VirtualMps3, in every harness state Team T12 modelled (FIELDED_ILA_V011, LINUX_HARNESSD,
busy, wedged, rescue, a tunnelled link). No DemoEngine and no mock here: what the page
shows is what the product daemon and engine report.

Each state is photographed in light and dark under tests/web/screenshots/states-*.png.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from harness_manager.core.model import Link, LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t14_mock_api import real_daemon
from tests.fakes.virtual_board import (
    FIELDED_3F1A560F,
    FIELDED_ILA_V011,
    LINUX_HARNESSD,
    VirtualMps3,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000


@pytest.fixture
def stage(browser, tmp_path, monkeypatch, screenshots):
    """``stage(vb, candidate)`` -> a page with that board open, on the real engine."""
    daemons, contexts = [], []

    def make(vb: VirtualMps3, candidate=None, scheme: str = "light"):
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                        packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        daemon = real_daemon(engine, token="t14-states", state_dir=tmp_path / "harness-manager-daemon").start()
        daemons.append(daemon)
        cand = candidate or vb.candidate()
        daemon.seed(cand)                  # known to the daemon, so the page never scans
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme,
                                  reduced_motion="reduce")
        contexts.append(ctx)
        page = ctx.new_page()
        page.goto(daemon.ui_url)
        page.locator(f'.board-item[data-board="{cand.board_id}"]').click()
        page.locator('[data-action="open"]').click()
        page.wait_for_selector('[data-testid="board-header"]', timeout=T)
        page.locator('[data-action="details"]').click()        # the Details start collapsed
        page.wait_for_selector('[data-testid="health-card"]', timeout=T)
        return page, engine, cand.board_id

    yield make
    for ctx in contexts:
        ctx.close()
    for d in daemons:
        d.stop()


def shoot(page, screenshots, name):
    for theme in ("Light", "Dark"):
        page.locator(f'.rail .seg button:has-text("{theme}")').click()
        page.wait_for_timeout(120)
        page.screenshot(path=str(screenshots / f"states-{name}-{theme.lower()}.png"))
    page.locator('.rail .seg button:has-text("Auto")').click()


def health_chip(page):
    return page.locator('[data-testid="health-chip"]')


def refresh(page):
    page.locator('[data-action="refresh-board"]').click()


# --- identity by harness generation ------------------------------------------------------


def test_ila_v011_is_bare_metal_with_a_passing_build_check(stage, tmp_path, screenshots):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        page, _, _ = stage(vb)
        expect(page.locator('[data-testid="build-chip"]')).to_have_attribute("data-level", "ok")
        expect(page.locator('[data-testid="fact-harness"]')).to_contain_text("bare-metal")
        expect(page.locator('[data-testid="fact-shell"]')).to_contain_text("0x72bb0a36")
        expect(health_chip(page)).to_have_attribute("data-level", "ok")
        # v0.11 reports `reboot`, so the shell restart lights up (the fielded board lacks it).
        assert page.locator('[data-testid="capabilities-card"] .cap[data-capability="reset_shell"]').count() == 1
        shoot(page, screenshots, "ila-v011")


def test_negative_twin_the_fielded_board_stays_unchecked_and_cannot_restart(stage, tmp_path):
    with VirtualMps3(tmp_path, FIELDED_3F1A560F) as vb:
        page, _, _ = stage(vb)
        expect(page.locator('[data-testid="build-chip"]')).to_have_attribute("data-level", "unk")
        missing = page.locator('[data-testid="capabilities-card"] li[data-capability="reset_shell"]')
        expect(missing).to_contain_text("reboot")


def test_linux_harness_shows_linux_its_ssh_link_and_the_shell_console(stage, tmp_path, screenshots):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        page, _, _ = stage(vb, vb.candidate(ssh=True))
        expect(page.locator('[data-testid="fact-harness"]')).to_contain_text("linux")
        expect(page.locator('[data-testid="id-harness"]')).to_contain_text("linux")
        assert page.locator('[data-testid="identity-card"] [data-link="ssh"]').count() == 1
        assert page.locator('.cap[data-capability="console_shell"]').count() == 1
        shoot(page, screenshots, "linux")


def test_negative_twin_linux_without_ssh_says_the_shell_console_needs_it(stage, tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        page, _, _ = stage(vb)
        missing = page.locator('li[data-capability="console_shell"]')
        expect(missing).to_contain_text("SSH")


def test_a_tunnelled_link_is_labelled_as_one(stage, tmp_path, screenshots):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        cand = vb.candidate(ssh=True)
        eth = Link(LinkKind.ETHERNET, vb.shell_endpoint, "shell control channel", via="ssh")
        cand = replace(cand, links=(eth, *[lk for lk in cand.links if lk.kind != LinkKind.ETHERNET]))
        page, _, _ = stage(vb, cand)
        link = page.locator('[data-testid="identity-card"] [data-link="ethernet"]')
        expect(link).to_contain_text("via SSH tunnel")
        shoot(page, screenshots, "tunnel")


# --- harness states ------------------------------------------------------------------------


def test_busy_shows_a_warning_and_who_holds_the_channel(stage, tmp_path, screenshots):
    with VirtualMps3(tmp_path, LINUX_HARNESSD, unit="dna-0a0b0c0d") as vb:
        page, _, _ = stage(vb)
        expect(health_chip(page)).to_have_attribute("data-level", "ok")
        vb.set_busy()
        refresh(page)
        expect(health_chip(page)).to_have_attribute("data-level", "warn")
        expect(health_chip(page)).to_contain_text("Busy")
        expect(page.locator('[data-testid="health-note"]').first).to_contain_text("another client")
        shoot(page, screenshots, "busy")
        vb.set_busy(False)
        refresh(page)
        expect(health_chip(page)).to_have_attribute("data-level", "ok")    # the twin: it recovers


def test_a_hung_harness_shows_the_failed_read_and_keeps_the_last_good_one(stage, tmp_path, screenshots):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        page, engine, bid = stage(vb)
        engine.session(bid).shell.timeout = 0.3
        vb.hang()
        try:
            refresh(page)
            banner = page.locator('[data-testid="stale-banner"]')
            expect(banner).to_contain_text("did not reply", timeout=T)
            expect(banner).to_contain_text("Showing what it said at")
            expect(health_chip(page)).to_have_attribute("data-level", "err")
            expect(page.locator('[data-testid="fact-shell"]')).not_to_contain_text("unknown")
            shoot(page, screenshots, "wedged")
        finally:
            vb.unhang()
        page.locator('[data-testid="stale-banner"] button').click()
        expect(page.locator('[data-testid="stale-banner"]')).to_have_count(0, timeout=T)
        expect(health_chip(page)).to_have_attribute("data-level", "ok")


def test_a_board_reboot_shows_the_controllers_evidence(stage, tmp_path, screenshots, monkeypatch):
    # The page is under test here, not the MCC's timing (tests/unit/test_t3_mcc.py and
    # test_t3_hooks.py hold that). At the real 60 ms a character, the Details' MCC reads
    # hold the board and the reboot POST waits ~6.5 s for them (Q1 finding); a 5 ms pace,
    # a faster witness and a 1 s boot keep every phase the evidence reports (sent, down,
    # up, shell id) and cut the test from ~14 s.
    from harness_manager_mps3 import mcc

    monkeypatch.setattr(mcc, "DEFAULT_TIMING", replace(
        mcc.DEFAULT_TIMING, pace_s=0.005, ping_interval_s=0.25, ping_timeout_s=0.25,
        quiet_s=0.5, reopen_interval_s=0.1))
    with VirtualMps3(tmp_path, FIELDED_3F1A560F, usb=True) as vb:
        vb.mcc._pace = 0.002                  # the fake drops characters closer than this
        vb.mcc.boot_s = 1.0
        page, _, _ = stage(vb, vb.candidate(usb=True))
        page.locator('[data-section="power"]').click()
        page.locator('[data-testid="arm-reboot"] input').check()
        page.locator('[data-action="reboot"]').click()
        expect(page.locator('[data-testid="job-chip"]')).to_contain_text("board reboot", timeout=T)
        result = page.locator('[data-testid="reboot-result"]')
        expect(result).to_contain_text("rc 0", timeout=45_000)
        expect(result).to_contain_text("down after")
        expect(result).to_contain_text("shell 0x3f1a560f -> 0x3f1a560f (unchanged)")
        assert vb.reboots == 1
        shoot(page, screenshots, "reboot-evidence")


def test_a_rescue_board_is_found_opened_and_explained(stage, tmp_path, screenshots, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        endpoint = vb.shell_endpoint
        vb.enter_rescue("slot A and B failed CRC")
        pack = Mps3Pack(console_ports=vb.console_ports)
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        # scan_network stays on: with explicit hosts it only unicasts identify to them, and a
        # board in rescue answers identify, not 6900.
        (cand,) = pack.probe(ProbeHints(hosts=(endpoint,), scan_usb=False, timeout_s=0.5))
        page, _, _ = stage(vb, cand)
        expect(health_chip(page)).to_have_attribute("data-level", "warn")
        expect(health_chip(page)).to_contain_text("Rescue")
        # harness_impl is "" in rescue: shown as unknown, never as an error.
        expect(page.locator('[data-testid="fact-harness"]')).to_contain_text("unknown")
        notes = page.locator('[data-testid="health-note"]')
        expect(notes.first).to_contain_text("RESCUE")
        expect(notes.nth(1)).to_contain_text("slot A and B failed CRC")
        shoot(page, screenshots, "rescue")
