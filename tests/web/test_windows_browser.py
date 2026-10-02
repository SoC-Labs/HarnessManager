"""Lane WINDOWS in the browser: what the bring-up wizard shows a Windows laptop's user, on the
REAL daemon over the demo (``app --demo``) in the system Chrome. Windows is pretended where the
service decides it: this PC's network check (``bringup.network_check``, answered from the
lane's fixture) and the card writer's routes (``tests/fakes/win_cardwriter.py``, the real
Windows steps). Each behaviour has its negative twin. Nothing touches a device or PowerShell."""

from __future__ import annotations

import pytest

from harness_manager.services import bringup
from harness_manager.services import netcheck as NC
from tests.fakes import win_net
from tests.fakes.win_cardwriter import DEVICE, WinCardwriter
from tests.web.test_bringup_browser import (  # noqa: F401 - the demo fixture
    T,
    bring_up,
    by,
    check_image,
    demo,
    expect,
    over_usb,
)

pytestmark = pytest.mark.browser


def windows_network(monkeypatch, doc=None, **kw):
    """``bringup.network_check`` as on a Windows laptop whose board adapter is ``doc``."""
    adapters, blocks = NC.parse(doc or win_net.laptop())
    monkeypatch.setattr(bringup, "network_check",
                        lambda host: NC.diagnose(host, adapters, blocks, **kw))


def test_the_scan_shows_the_windows_network_fix_under_nothing_answers(demo, monkeypatch):  # noqa: F811
    windows_network(monkeypatch)
    page = demo.page()
    over_usb(page)
    expect(by(page, "usb-eth")).to_have_text("nothing answers", timeout=T)
    box = by(page, "usb-netcheck")
    expect(box).to_contain_text("This Windows PC's network")
    expect(box.locator('li[data-code="no_address"]')).to_contain_text(
        "This PC has no address on 192.168.10.0/24")
    expect(box).to_contain_text('Set-NetIPInterface -InterfaceAlias "Ethernet 2" -Dhcp Disabled')
    expect(box).to_contain_text('New-NetIPAddress -InterfaceAlias "Ethernet 2" -IPAddress '
                                "192.168.10.1 -PrefixLength 24")
    expect(box.locator("button[aria-label^='Copy']")).to_have_count(2)
    expect(box).to_contain_text("Harness Manager never runs these itself.")
    assert not page.errors, page.errors


def test_twin_off_windows_the_scan_shows_no_network_block(demo):  # noqa: F811
    page = demo.page()
    over_usb(page)
    expect(by(page, "usb-eth")).to_have_text("nothing answers", timeout=T)
    expect(by(page, "usb-netcheck")).to_have_count(0)
    assert not page.errors, page.errors


def test_a_witness_timeout_on_a_public_network_says_make_it_private(demo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bringup, "DEFAULT_WITNESS_S", 2.0)
    windows_network(monkeypatch, win_net.laptop(board_ip="192.168.10.1", category="Public"))
    demo.engine.bringup_dark = True
    page = demo.page()
    bring_up(page, demo.example(0))
    expect(by(page, "bu-timeout")).to_contain_text("Nothing answered at 192.168.10.101",
                                                   timeout=T)
    box = by(page, "bu-netcheck")
    expect(box.locator('li[data-code="public_profile"]')).to_contain_text(
        "The board's network is Public.")
    expect(box).to_contain_text('Set-NetConnectionProfile -InterfaceAlias "Ethernet 2" '
                                "-NetworkCategory Private")
    expect(box).to_contain_text("Or instead:")
    expect(box).to_contain_text('New-NetFirewallRule -DisplayName "Harness Manager board UDP" '
                                "-Direction Inbound -Protocol UDP -RemoteAddress "
                                "192.168.10.0/24 -Action Allow")
    assert not page.errors, page.errors


def test_twin_a_witness_timeout_on_a_private_network_has_no_block(demo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bringup, "DEFAULT_WITNESS_S", 2.0)
    windows_network(monkeypatch, win_net.laptop(board_ip="192.168.10.1", category="Private"))
    demo.engine.bringup_dark = True
    page = demo.page()
    bring_up(page, demo.example(0))
    expect(by(page, "bu-timeout")).to_contain_text("Nothing answered", timeout=T)
    expect(by(page, "bu-netcheck")).to_have_count(0)
    assert not page.errors, page.errors


def test_the_os_step_on_windows_gives_the_admin_steps_with_copy_buttons(demo, monkeypatch):  # noqa: F811
    monkeypatch.setenv(bringup.SD_FLASH_ENV, "on")
    fake = WinCardwriter().attach(demo.daemon.app)
    page = demo.page()
    bring_up(page, demo.example(1))
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible(timeout=T)
    os_step.locator('[data-option="reader"] input').check()
    card = demo.engine.bringup_card_image
    check_image(page, card)
    page.locator('[data-testid="reader-device-card"]').select_option(DEVICE["id"])
    expect(by(page, "bu-os-windows-note")).to_contain_text(
        "On Windows, Write gives you the steps instead of writing")
    page.locator('[data-testid="reader-confirm-card"]').fill(DEVICE["confirm"])
    page.locator('[data-testid="arm-bu-os"] input').check()
    page.locator('[data-action="bu_os"]').click()
    expect(by(page, "bu-os-result")).to_contain_text(
        "Harness Manager never writes a whole card on Windows", timeout=T)
    steps = by(page, "bu-os-privileged")
    expect(steps.locator("li[data-step]")).to_have_count(5)
    expect(steps.locator('li[data-step="1"]')).to_contain_text("$d = Get-Disk -Number 2")
    expect(steps.locator('li[data-step="2"]')).to_contain_text("diskpart /s")
    expect(steps.locator('li[data-step="4"]')).to_contain_text(f"OpenRead('{card}')")
    expect(steps.locator('li[data-step="5"]')).to_have_text("Update-Disk -Number 2")
    expect(steps.locator("button[aria-label^='Copy step']")).to_have_count(5)
    expect(by(page, "bu-os-verify")).to_contain_text("[Security.Cryptography.SHA256]::Create()")
    expect(by(page, "bu-os-imager")).to_contain_text("Or with Raspberry Pi Imager")
    expect(steps).to_contain_text("Choose OS: Use custom")
    expect(os_step).to_have_attribute("data-state", "todo")          # nothing written
    (w,) = fake.writes
    assert w["kind"] == "card" and w["source"] == card
    assert not page.errors, page.errors


def test_twin_the_os_step_on_linux_keeps_the_one_sudo_command(demo, monkeypatch):  # noqa: F811
    from tests.fakes.bringup_cardwriter import DEVICE as LINUX_DEVICE
    from tests.fakes.bringup_cardwriter import FakeCardwriter

    monkeypatch.setenv(bringup.SD_FLASH_ENV, "on")
    FakeCardwriter(privilege=True).attach(demo.daemon.app)
    page = demo.page()
    bring_up(page, demo.example(1))
    os_step = by(page, "bu-step-os")
    expect(os_step).to_be_visible(timeout=T)
    os_step.locator('[data-option="reader"] input').check()
    check_image(page, demo.engine.bringup_card_image)
    page.locator('[data-testid="reader-device-card"]').select_option(LINUX_DEVICE["id"])
    expect(by(page, "bu-os-windows-note")).to_have_count(0)
    page.locator('[data-testid="reader-confirm-card"]').fill(
        f"WRITE {LINUX_DEVICE['model']} {LINUX_DEVICE['size_bytes']}")
    page.locator('[data-testid="arm-bu-os"] input').check()
    page.locator('[data-action="bu_os"]').click()
    expect(by(page, "bu-os-result")).to_contain_text("sudo dd if=", timeout=T)
    expect(by(page, "bu-os-privileged")).to_have_count(0)
    assert not page.errors, page.errors
