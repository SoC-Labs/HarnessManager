"""Lane SD-FLASH in a real browser: Settings shows the new "Bring-up" section (the section the
refusal names: "Settings → Bring-up, bringup.sd_flash"), SD flashing is off there, and turning
it on with a click makes ``GET /cardwriter/devices`` list the (simulated, ``--demo``) cards at
once. Over the real daemon and the T14 mock. Nothing touches a block device.
"""

from __future__ import annotations

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000


def devices(daemon) -> dict:
    r = httpx.get(f"{daemon.url}/api/v1/cardwriter/devices",
                  headers={"Authorization": f"Bearer {daemon.token}"}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.mock_too
def test_settings_bring_up_turns_sd_flashing_on_and_the_cards_appear(page_factory, daemon):
    assert devices(daemon)["enabled"] is False
    page = page_factory("light", width=1440, height=900)
    page.locator('[data-action="settings"]').click()
    nav = page.locator('[data-testid="settings-nav"] [data-settings-section="bring-up"]')
    expect(nav).to_have_text("Bring-up", timeout=T)
    nav.click()
    row = page.locator('[data-testid="setting-row"][data-key="bringup.sd_flash"]')
    expect(row).to_contain_text("Write SD cards in this PC's card reader", timeout=T)
    seg = row.locator('[data-testid="setting-seg"]')
    expect(seg.locator('[data-value="off"]')).to_have_attribute("aria-pressed", "true")
    seg.locator('[data-value="on"]').click()
    expect(seg.locator('[data-value="on"]')).to_have_attribute("aria-pressed", "true", timeout=T)
    expect(row.locator('[data-testid="source-chip"] [data-source]')).to_have_attribute(
        "data-source", "user", timeout=T)
    doc = devices(daemon)
    assert doc["enabled"] is True and doc["simulated"] is True
    assert [d["path"] for d in doc["devices"]] == ["/dev/sdb", "/dev/sdc"]
    assert not page.errors, page.errors


@pytest.mark.mock_too
def test_twin_left_off_the_section_says_off_and_nothing_is_listed(page_factory, daemon):
    page = page_factory("dark", width=1440, height=900)
    page.locator('[data-action="settings"]').click()
    page.locator('[data-testid="settings-nav"] [data-settings-section="bring-up"]').click()
    row = page.locator('[data-testid="setting-row"][data-key="bringup.sd_flash"]')
    expect(row.locator('[data-testid="setting-seg"] [data-value="off"]')).to_have_attribute(
        "aria-pressed", "true", timeout=T)
    doc = devices(daemon)
    assert doc["enabled"] is False and doc["devices"] == []
    assert doc["reason"] == "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"
    assert not page.errors, page.errors
