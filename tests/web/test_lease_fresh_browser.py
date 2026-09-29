"""LEASE-FRESH in the browser: after an acquire the page says "Yours" at once, and one failed
hub read right after it does not turn that into "Lease unknown" / "Needs attention".

david (2026-09-29, mps3-02): the acquire worked, the `lease show` after it met the hub sshd's
throttle ("Connection reset by peer"), and the rail said "Lease unknown", the header "lease
unknown", Background "Paused: lease unknown", and "Needs attention" said the lease could not
be read. Now the acquire's own answer (``lease.state`` with ``source: acquire``) is shown at
once, and the service carries it over a failed read (``stale``, with a quiet note); only
repeated failures (3 in a row) still say "unknown".

Over the T14 mock (``WeekPlanSim.lease_read_fails``: the service's two answers, scripted) and
the demo daemon (the real lease service over the showcase's in-memory hub, whose reads are
made to fail here). Each behaviour has its negative twin. Nothing reaches a hub or a board.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager.core.errors import UnreachableError
from harness_manager.demo_showcase import BOARD_SPARE, SPARE_TARGET, DemoHubClient
from harness_manager.services import lease as leasemod
from tests.web.test_demo_all_browser import Showcase, make_showcase
from tests.web.test_lease_ui_browser import BOARD, by_id, demo_open, hub_page, rail, sim

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
HUB = pytest.mark.week_plan("hub_api", sim=True)
T = 10_000
RESET = "Connection reset by peer"


def acquire_from_attention(page: Any) -> None:
    page.locator('[data-attention="lease"] [data-action="lease_acquire"]').click()


# --- over the mock --------------------------------------------------------------------------------


@HUB
def test_acquire_shows_yours_at_once_though_the_next_read_fails(page_factory, daemon):
    page = hub_page(page_factory, daemon, "none")
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row).to_have_attribute("data-lease", "free", timeout=T)
    sim(daemon).lease_read_fails(BOARD, "stale")          # every read after this one fails
    acquire_from_attention(page)
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)
    expect(by_id(page, "lease-chip")).to_contain_text("lease yours")
    # the read that followed failed: a quiet note, never "unknown" or "Needs attention"
    note = by_id(page, "lease-stale")
    expect(note).to_contain_text(re.compile(
        r"last confirmed \d\d:\d\d:\d\d; the hub didn't answer the last read, reading again"),
        timeout=T)
    expect(note).to_have_attribute("title", re.compile(RESET))
    expect(page.locator('[data-attention="lease"]')).to_have_count(0)
    assert page.get_by_text("Lease unknown").count() == 0
    assert page.get_by_text("could not be read").count() == 0
    assert page.errors == []


@HUB
def test_twin_repeated_failed_reads_still_say_unknown_and_need_attention(page_factory, daemon):
    page = hub_page(page_factory, daemon, "none")
    sim(daemon).lease_read_fails(BOARD, "unknown")        # the service has given up (3 in a row)
    acquire_from_attention(page)
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text("Lease unknown", timeout=T)
    expect(by_id(page, "lease-chip")).to_have_text("lease unknown")
    expect(page.locator('[data-attention="lease"]')).to_contain_text("could not be read")
    expect(page.locator('[data-attention="lease"]')).to_contain_text("did not answer 3 lease reads")
    assert by_id(page, "lease-stale").count() == 0


@HUB
def test_release_shows_not_leased_at_once(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row).to_have_attribute("data-lease", "here", timeout=T)
    by_id(page, "fact-hub").locator('[data-action="lease_release_open"]').click()
    by_id(page, "release-confirm").locator('[data-action="release_confirm"]').click()
    expect(row).to_have_attribute("data-lease", "free", timeout=T)
    expect(by_id(page, "lease-chip")).to_have_text("no lease")
    assert page.errors == []


# --- over the demo daemon (the real lease service) --------------------------------------------------


class FailingReads:
    """The showcase hub's ``lease status`` for the spare board, failing like the hub's sshd
    while ``on``; counts the reads it failed."""

    def __init__(self) -> None:
        self.on = False
        self.failed = 0
        self.mu = threading.Lock()


@pytest.fixture
def failing(monkeypatch: pytest.MonkeyPatch) -> FailingReads:
    reads = FailingReads()
    real = DemoHubClient.lease_status

    def lease_status(self: DemoHubClient) -> Any:
        if self.target == SPARE_TARGET and reads.on:
            with reads.mu:
                reads.failed += 1
            raise UnreachableError(f"lease show on {self.host} failed: lease show failed: ssh: "
                                   f"connect to host {self.host} port 22: {RESET}")
        return real(self)

    monkeypatch.setattr(DemoHubClient, "lease_status", lease_status)
    return reads


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def spare_row(page: Any) -> Any:
    return rail(page, BOARD_SPARE).locator('[data-testid="rail-lease-row"]')


def test_the_real_service_keeps_yours_through_a_failed_read_after_acquire(showcase, failing):
    page = showcase.page()
    demo_open(page, BOARD_SPARE)
    expect(spare_row(page).locator('[data-testid="rail-lease-badge"]')).to_have_text("Free", timeout=T)
    failing.on = True                                      # every read after the acquire fails
    acquire_from_attention(page)
    expect(spare_row(page).locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)
    expect(by_id(page, "lease-chip")).to_contain_text("lease yours")
    expect(by_id(page, "lease-stale")).to_contain_text("the hub didn't answer the last read",
                                                       timeout=T)
    assert failing.failed >= 1, "a read after the acquire was made, and failed"
    expect(page.locator('[data-attention="lease"]')).to_have_count(0)
    # Background is not paused for it (the viewer's next beat asks the gate again)
    bg = by_id(page, "fact-background")
    page.wait_for_timeout(500)
    if bg.count():
        expect(bg).not_to_contain_text("lease unknown")
    # a reload reads again: still yours, still calm
    page.reload()
    page.wait_for_selector(".board-item", timeout=T)
    demo_open(page, BOARD_SPARE)
    expect(spare_row(page).locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)
    expect(page.locator('[data-attention="lease"]')).to_have_count(0)
    assert not page.errors, page.errors


def test_twin_the_real_service_says_unknown_after_three_failed_reads(showcase, failing, monkeypatch):
    monkeypatch.setattr(leasemod, "READ_RETRY_S", 0.0)    # every failed read is a miss
    page = showcase.page()
    demo_open(page, BOARD_SPARE)
    expect(spare_row(page).locator('[data-testid="rail-lease-badge"]')).to_have_text("Free", timeout=T)
    failing.on = True
    acquire_from_attention(page)
    expect(spare_row(page).locator('[data-testid="rail-lease-badge"]')).to_have_text(
        re.compile("Yours|Lease unknown"), timeout=T)
    d = showcase.daemon.app.state.daemon
    hub = d.engine.session(BOARD_SPARE).hub
    for _ in range(leasemod.READ_MISSES_MAX + 1):          # the reads a few polls would make
        try:
            d.leases.view(hub)
        except UnreachableError:
            break
    else:
        pytest.fail("three failed reads in a row never said unknown")
    page.reload()
    page.wait_for_selector(".board-item", timeout=T)
    demo_open(page, BOARD_SPARE)
    expect(spare_row(page).locator('[data-testid="rail-lease-badge"]')).to_have_text(
        "Lease unknown", timeout=T)
    expect(page.locator('[data-attention="lease"]')).to_contain_text("could not be read")
    assert failing.failed >= leasemod.READ_MISSES_MAX
