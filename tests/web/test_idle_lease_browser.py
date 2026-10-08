"""IDLE-LEASE in the browser: the warning banner, "Keep it", and the release notice.

On the real daemon over the demo engine, driven by the service's own ``lease.idle`` events on
its bus (as ``daemon/idle_api.py`` publishes them; the demo's scripted boards are never idle
by themselves). Each behaviour has its negative twin. Nothing here reaches a hub or a board.
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
WARN = ("mps3-usb's lease is released in 2 min: nothing has used it for 28 min. "
        "Use the board to keep it.")


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def warn(engine, bid: str = BOARD_USB, text: str = WARN) -> None:
    engine.bus.publish(Event("lease.idle", bid, {
        "state": "warning", "board": "mps3-usb", "idle_min": 28, "limit_min": 30, "own": True,
        "text": text, "release_at": "2099-01-01T00:00:00Z"}))


def test_the_warning_shows_a_banner_and_keep_it_asks_the_service(page_factory, engine):
    page = page_factory(**APP)
    nav.open_board(page, BOARD_USB)
    keeps: list[dict] = []
    page.on("request", lambda r: keeps.append(r.headers) if r.method == "POST"
            and r.url.endswith("/lease/keep") else None)
    banner = by_id(page, "lease-idle")
    expect(banner).to_have_count(0)
    warn(engine)
    expect(banner).to_be_visible(timeout=T)
    expect(by_id(page, "lease-idle-text")).to_have_text(WARN)
    expect(by_id(page, "lease-idle-countdown")).to_be_visible()
    banner.locator('[data-action="lease_idle_keep"]').click()
    expect(banner).to_have_count(0, timeout=T)              # the service answered: kept
    assert len(keeps) == 1 and keeps[0].get("x-hm-client") == "page"
    assert page.errors == []


def test_negative_twin_use_elsewhere_withdraws_the_banner_without_a_click(page_factory, engine):
    page = page_factory(**APP)
    nav.open_board(page, BOARD_USB)
    warn(engine)
    banner = by_id(page, "lease-idle")
    expect(banner).to_be_visible(timeout=T)
    engine.bus.publish(Event("lease.idle", BOARD_USB, {
        "state": "kept", "board": "mps3-usb", "idle_min": 0, "limit_min": 30, "own": True,
        "text": "mps3-usb is in use again: its lease is kept."}))
    expect(banner).to_have_count(0, timeout=T)
    assert page.errors == []


def test_the_release_is_told_and_logged_and_the_banner_goes(page_factory, engine):
    page = page_factory(**APP)
    nav.open_board(page, BOARD_USB)
    warn(engine)
    expect(by_id(page, "lease-idle")).to_be_visible(timeout=T)
    text = ("mps3-usb's lease was released after 30 min with nothing using it, and the board "
            "was closed. Open it again to take the lease again.")
    engine.bus.publish(Event("lease.idle", BOARD_USB, {
        "state": "released", "board": "mps3-usb", "idle_min": 30, "limit_min": 30,
        "own": True, "text": text}))
    expect(by_id(page, "lease-idle")).to_have_count(0, timeout=T)
    expect(page.get_by_text(text).first).to_be_visible(timeout=T)     # the toast
    assert page.errors == []


def test_the_pages_own_reads_say_they_are_the_page(page_factory, engine):
    page = page_factory(**APP)
    seen: list[dict] = []
    page.on("request", lambda r: seen.append(r.headers) if "/api/v1/" in r.url else None)
    nav.open_board(page, BOARD_USB)
    assert seen and all(h.get("x-hm-client") == "page" for h in seen)
    assert page.errors == []
