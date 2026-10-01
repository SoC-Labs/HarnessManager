"""QUIET-POLL in the browser: the page views the board it shows, marks the reads nobody clicked
as background, and says calmly when background reads are paused.

A headless system Chrome on the real harness-manager-daemon over DemoEngine. The demo's
background gate allows everything (its boards are scripted), so the page's viewer is counted
by the real gate, and the "paused" test turns the gate on with a lease someone else holds.
Each test has its twin.
"""

from __future__ import annotations

import time
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_USB

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

pytestmark = pytest.mark.browser

T = 10_000
INFO = quote(BOARD_USB, safe="")


def open_board(page) -> None:
    page.locator(f'.board-item[data-board="{BOARD_USB}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)


def until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def info_reads(requests: list) -> list[dict]:
    return [h for m, u, h in requests if m == "GET" and u.split("?")[0].endswith(f"/boards/{INFO}")]


def test_the_page_views_the_board_it_shows_and_marks_its_own_reads(page_factory, daemon):
    gate = daemon.app.state.daemon.quiet
    page = page_factory()
    requests: list = []
    page.on("request", lambda r: requests.append((r.method, r.url, r.headers)))
    open_board(page)
    until(lambda: gate.viewers(BOARD_USB) == 1)
    assert any(h.get("x-hm-background") == "1" for h in until(lambda: info_reads(requests))), \
        "the read after the open is one nobody clicked"
    # the open's own background reads land first (UI v2's Overview reads more at open: the
    # lease queue, the clashes, the trend), so the window below holds the click's read only
    settled = -1
    while settled != len(info_reads(requests)):
        settled = len(info_reads(requests))
        page.wait_for_timeout(1000)
    requests.clear()
    page.locator('[data-action="refresh-board"]').click()          # twin: a click
    clicked = until(lambda: info_reads(requests))
    assert all("x-hm-background" not in h for h in clicked)
    page.locator('button:has-text("Close board")').click()           # closing stops the view
    until(lambda: gate.viewers(BOARD_USB) == 0)
    assert page.errors == []


def test_paused_by_someone_elses_lease_is_calm_and_a_click_still_reads(page_factory, daemon,
                                                                       engine, screenshots):
    d = daemon.app.state.daemon
    d.quiet.enabled = True                       # as for a real board
    d.quiet.lease_of = lambda _bid: "alice@lab-pc"
    page = page_factory()
    open_board(page)
    chip = page.locator('[data-testid="background-chip"]')
    chip.wait_for(timeout=T)
    assert "Paused: lease held by alice@lab-pc" in chip.inner_text()
    assert chip.get_attribute("data-level") == "held", "calm (held), never an error"
    assert page.locator('[data-testid="stale-banner"]').count() == 0
    page.screenshot(path=str(screenshots / "light-background-paused-lease.png"))
    before = len(engine.called("info"))
    page.locator('[data-action="refresh-board"]').click()          # explicit: it reads
    until(lambda: len(engine.called("info")) > before)
    d.quiet.lease_of = lambda _bid: ""                               # twin: the lease is ours
    page.locator('[data-action="refresh-board"]').click()
    page.wait_for_selector('[data-testid="background-chip"]', state="detached", timeout=T)
    assert page.errors == []
