"""LEASE-UI in the browser: whose lease it is, Release, and Close board asking about the lease.

david (2026-09-28): the lease Release button must be easy to find; the board card in the
side list must say whether a board is leased, and whether to you or to someone else; and
closing a board must ask whether to release its lease too.

"Yours" is `lease.here` (THIS Harness Manager holds the lease token), never `lease.mine`:
every lab session shares one fpgahub principal (david@mapstone-dev), so a lease another
session or a soak holds under it is "Held by ... (another session)". Over the T14 mock
(``WeekPlanSim.behind_hub``: mine | elsewhere | other | none) and the demo daemon (the
showcase's mps3-02, alice's, and mps3-03, free). Each behaviour has its negative twin.
Nothing here reaches a hub or a board.
"""

from __future__ import annotations

import getpass
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from tests.web.test_demo_all_browser import Showcase, make_showcase

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
HUB = pytest.mark.week_plan("hub_api", sim=True)
T = 10_000
APP = {"width": 1440, "height": 900}
BOARD = BOARD_FIELDED               # "mps3-01" (N1, from the hub), behind mapstone-dev
TARGET = "mps3_01_pl"
HUB_BOARD = "mps3_01"               # LEASE-BOARD: fpgahub's physical board, what the text says
ME = f"{getpass.getuser()}@harness-manager"        # the mock's principal (this and other sessions)
ALICE = "alice@lab-pc-07"


# --- helpers -------------------------------------------------------------------------------------


def sim(daemon: Any) -> Any:
    return daemon.app.state.sim


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def rail(page: Any, bid: str = BOARD) -> Any:
    return page.locator(f'.board-item[data-board="{bid}"]')


def open_board(page: Any, bid: str = BOARD, *, hub: bool = True) -> None:
    rail(page, bid).click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
    if hub:
        page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)


def hub_page(page_factory: Any, daemon: Any, lease: str, *, scheme: str = "light") -> Any:
    sim(daemon).behind_hub(BOARD, lease=lease)
    page = page_factory(scheme, **APP)
    open_board(page)
    return page


def lease_record(daemon: Any, bid: str = BOARD) -> Any:
    return sim(daemon).hubs[bid]["lease"]


def closes(page: Any) -> list[str]:
    """The DELETE /boards/{bid} calls the page makes, with their query."""
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if r.method == "DELETE"
            and re.search(r"/boards/[^/]+(\?.*)?$", r.url) else None)
    return seen


def wait_until(fn: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


# --- the side list's badge ------------------------------------------------------------------------


@HUB
def test_the_rail_says_yours_when_this_harness_manager_holds_the_lease(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row).to_have_attribute("data-lease", "here", timeout=T)
    badge = row.locator('[data-testid="rail-lease-badge"]')
    expect(badge).to_have_text("Yours")
    expect(badge.locator("svg")).to_have_count(1)                      # an icon, and words
    expect(badge).to_have_attribute("title", re.compile(
        rf"{HUB_BOARD} on mapstone-dev \(target {TARGET}\), held by this Harness Manager"))
    # the board lock is its own chip ("Open"), never a second "Yours"
    expect(rail(page).locator('[data-testid="rail-open"]')).to_have_text("Open")
    assert rail(page).get_by_text("Yours", exact=True).count() == 1
    assert page.errors == []


@HUB
def test_negative_twin_the_same_principal_in_another_session_is_held_not_yours(page_factory, daemon):
    # REVIEW-W5: `mine` (by principal) without `here` (the token): another lab session or a soak.
    page = hub_page(page_factory, daemon, "elsewhere")
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row).to_have_attribute("data-lease", "elsewhere", timeout=T)
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text(f"Held by {ME} (another session)")
    assert rail(page).get_by_text("Yours", exact=True).count() == 0
    expect(by_id(page, "lease-chip")).to_have_text(f"leased to {ME} (another session)")
    expect(by_id(page, "lease-chip")).to_have_attribute("data-level", "held")
    expect(page.locator('[data-attention="lease"]')).to_contain_text(f"Leased to {ME} in another session.")
    expect(by_id(page, "tile-lease")).to_have_attribute("data-lease", "elsewhere")
    assert page.errors == []


@HUB
def test_the_rail_names_someone_elses_holder_and_our_place_in_the_queue(page_factory, daemon):
    page = hub_page(page_factory, daemon, "other")
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text(f"Held by {ALICE}", timeout=T)
    assert row.locator('[data-testid="rail-lease-queued"]').count() == 0      # twin: not asked yet
    page.locator('[data-testid="fact-hub"] [data-action="lease_request_open"]').click()
    page.locator('[data-testid="lease-request-form"] [data-action="lease_request"]').click()
    expect(row.locator('[data-testid="rail-lease-queued"]')).to_have_text("Requested · #1", timeout=T)
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text(f"Held by {ALICE}")


@HUB
def test_a_lease_nobody_holds_is_free_in_the_rail(page_factory, daemon):
    page = hub_page(page_factory, daemon, "none")
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row).to_have_attribute("data-lease", "free", timeout=T)
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text("Free")
    # Acquire: the same badge turns to Yours
    page.locator('[data-attention="lease"] [data-action="lease_acquire"]').click()
    expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)


@HUB
def test_a_lease_that_cannot_be_read_is_unknown_never_free(page_factory, daemon):
    # REVIEW-W5 2: not known is not free. The tunnel answers; GET /lease does not.
    sim(daemon).behind_hub(BOARD, lease="none")
    page = page_factory(**APP)
    page.route(re.compile(r"/lease$"), lambda route: route.fulfill(status=503, json={
        "ok": False, "error": {"code": 7, "name": "UNREACHABLE",
                               "message": "the hub mapstone-dev did not answer"}})
        if route.request.method == "GET" else route.continue_())
    open_board(page)
    badge = rail(page).locator('[data-testid="rail-lease-badge"]')
    expect(badge).to_have_text("Lease unknown", timeout=T)
    expect(by_id(page, "lease-chip")).to_have_text("lease unknown")
    expect(page.locator('[data-attention="lease"]')).to_contain_text("could not be read")
    assert page.locator('[data-testid="fact-hub"] [data-action="lease_acquire"]').count() == 0
    # the twin: the hub answers again, and the same (free) lease reads Free
    page.unroute(re.compile(r"/lease$"))
    sim(daemon).publish("lease.state", BOARD, {"target": TARGET, "state": "released",
                                               "holder": "", "expires_at": ""})   # a re-read
    expect(badge).to_have_text("Free", timeout=T)


@HUB
def test_negative_twin_a_board_with_no_hub_has_no_lease_badge(page_factory, daemon):
    page = page_factory(**APP)
    open_board(page, BOARD_USB, hub=False)
    page.wait_for_timeout(800)                                   # GET /lease answered: no hub
    assert rail(page, BOARD_USB).locator('[data-testid="rail-lease-row"]').count() == 0
    assert by_id(page, "tile-lease").count() == 0
    assert by_id(page, "fact-hub").count() == 0
    expect(rail(page, BOARD_USB).locator('[data-testid="rail-open"]')).to_have_text("Open")


# --- Release: prominent, and it asks first ----------------------------------------------------------


@HUB
def test_release_is_prominent_where_the_lease_is_yours_and_asks_first(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    header = page.locator('[data-testid="fact-hub"] [data-action="lease_release_open"]')
    expect(header).to_have_text("Release lease")
    expect(header).to_have_class(re.compile(r"\brelease\b"))
    expect(header).not_to_have_class(re.compile(r"\bghost\b"))           # a real button
    tile = by_id(page, "tile-lease")
    expect(tile.locator('[data-action="lease_release_open"]')).to_be_visible()
    # Cancel (and Escape) release nothing, and focus goes back
    header.click()
    dialog = by_id(page, "release-confirm")
    expect(dialog).to_be_visible()
    assert dialog.get_attribute("role") == "alertdialog"
    expect(dialog.locator('[data-testid="release-title"]')).to_have_text(f"Release {HUB_BOARD}?")
    expect(dialog.locator('[data-testid="release-target"]')).to_have_text(
        f"The hub leases it as target {TARGET}.")
    expect(dialog.locator('[data-testid="release-what"]')).to_have_text(
        "Others can take it; background checks pause.")
    expect(dialog.locator('[data-action="release_cancel"]')).to_be_focused()
    dialog.locator('[data-action="release_cancel"]').click()
    expect(dialog).to_have_count(0)
    expect(header).to_be_focused()
    header.click()
    expect(dialog).to_be_visible()                               # rendered: Escape reaches it
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert lease_record(daemon) is not None                      # nothing was released
    # Release, from the Board tile this time
    tile.locator('[data-action="lease_release_open"]').click()
    dialog.locator('[data-action="release_confirm"]').click()
    expect(by_id(page, "lease-chip")).to_have_text("no lease", timeout=T)
    expect(rail(page).locator('[data-testid="rail-lease-badge"]')).to_have_text("Free")
    expect(by_id(page, "lease-result")).to_contain_text(f"lease on {HUB_BOARD} released")
    assert lease_record(daemon) is None


@HUB
@pytest.mark.parametrize("lease", ["other", "elsewhere", "none"])
def test_negative_twin_release_is_absent_when_the_lease_is_not_yours(page_factory, daemon, lease):
    page = hub_page(page_factory, daemon, lease)
    expect(by_id(page, "tile-lease")).to_be_visible(timeout=T)
    assert page.locator('[data-action="lease_release_open"]').count() == 0
    if lease == "elsewhere":                                     # only that session can
        expect(by_id(page, "lease-elsewhere-note")).to_have_text("only that session can release it")


@HUB
def test_a_release_blocked_by_a_running_job_says_so_and_releases_nothing(page_factory, daemon):
    # Releasing mid-job hands a half-driven board to the next person: the confirm runs nothing.
    page = hub_page(page_factory, daemon, "mine")
    page.locator('[data-testid="fact-hub"] [data-action="lease_release_open"]').click()
    dialog = by_id(page, "release-confirm")
    assert dialog.locator('[data-testid="release-why"]').count() == 0           # the twin
    # a job starts on the board while the confirm is open (a power cycle, from the mock)
    daemon.app.state.daemon.jobs.start(BOARD, "power_cycle", lambda progress: time.sleep(4) or {})
    expect(dialog.locator('[data-testid="release-why"]')).to_contain_text(
        "Not now: waiting for the power cycle job to finish", timeout=T)
    go = dialog.locator('[data-action="release_confirm"]')
    expect(go).to_have_attribute("aria-disabled", "true")
    go.click(force=True)
    expect(dialog).to_be_visible()                               # nothing ran
    page.wait_for_timeout(300)
    assert lease_record(daemon) is not None


# --- LEASE-BOARD: the physical board's name, the target as a detail ---------------------------------------


@HUB
def test_lease_text_names_the_board_with_the_target_as_a_detail(page_factory, daemon):
    """david: "why does the lease say mps3_01_pl; isn't that deprecated?" The lease is still
    taken on the target (pyverify's name; docs/HUB_MODE.md); the text names fpgahub's board."""
    page = hub_page(page_factory, daemon, "mine")
    where = f"{HUB_BOARD} on mapstone-dev (target {TARGET})"
    expect(by_id(page, "lease-chip")).to_have_attribute("title", re.compile(re.escape(where)))
    expect(rail(page).locator('[data-testid="rail-lease-badge"]')).to_have_attribute(
        "title", re.compile(re.escape(where)))
    expect(by_id(page, "tile-lease").locator('[data-testid="tile-lease-badge"]')).to_have_attribute(
        "title", re.compile(re.escape(where)))
    page.locator('[data-testid="fact-hub"] [data-action="lease_release_open"]').click()
    dialog = by_id(page, "release-confirm")
    expect(dialog.locator('[data-testid="release-title"]')).to_have_text(f"Release {HUB_BOARD}?")
    expect(dialog.locator("code")).to_have_text(TARGET)
    assert page.errors == []


@HUB
def test_negative_twin_a_target_with_no_board_of_its_own_reads_as_before(page_factory, daemon):
    # The hub maps the target to no separate board (fpgahub's standalone group: board = target).
    sim(daemon).behind_hub(BOARD, lease="mine")
    sim(daemon).requests.set_board(BOARD, TARGET)
    page = page_factory(**APP)
    open_board(page)
    expect(by_id(page, "lease-chip")).to_have_attribute(
        "title", re.compile(rf"^{TARGET} on mapstone-dev, held by "))
    badge = rail(page).locator('[data-testid="rail-lease-badge"]')
    expect(badge).to_have_attribute("title", re.compile(rf"{TARGET} on mapstone-dev, held by this"))
    assert "(target" not in (badge.get_attribute("title") or "")
    page.locator('[data-testid="fact-hub"] [data-action="lease_release_open"]').click()
    dialog = by_id(page, "release-confirm")
    expect(dialog.locator('[data-testid="release-title"]')).to_have_text(f"Release {TARGET}?")
    assert dialog.locator('[data-testid="release-target"]').count() == 0
    dialog.locator('[data-action="release_cancel"]').click()
    close_board(page)
    close = by_id(page, "close-confirm")
    expect(close.locator('[data-testid="close-title"]')).to_have_text(
        f"Also release the lease on {TARGET}?")
    assert close.locator('[data-testid="close-target"]').count() == 0
    assert page.errors == []


# --- Close board asks about the lease ------------------------------------------------------------------


def close_board(page: Any) -> None:
    page.locator('[data-testid="board-header"] [data-action="close-board"]').click()


@HUB
def test_close_asks_about_a_lease_you_hold_and_cancel_leaves_the_board_open(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    seen = closes(page)
    close_board(page)
    dialog = by_id(page, "close-confirm")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[data-testid="close-title"]')).to_have_text(
        f"Also release the lease on {HUB_BOARD}?")
    expect(dialog.locator('[data-testid="close-target"]')).to_have_text(f"(target {TARGET})")
    expect(dialog.locator('[data-testid="close-keep-what"]')).to_contain_text(
        re.compile(r"it stays yours until \d\d:\d\d:\d\d, but nothing renews it while the board is closed"))
    for action, text in (("close_release", "Release and close"), ("close_keep", "Keep the lease"),
                         ("close_cancel", "Cancel")):
        expect(dialog.locator(f'[data-action="{action}"]')).to_have_text(text)
    expect(dialog.locator('[data-action="close_cancel"]')).to_be_focused()
    dialog.locator('[data-action="close_cancel"]').click()
    expect(dialog).to_have_count(0)
    expect(by_id(page, "board-header")).to_be_visible()          # still open
    close_board(page)
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    page.wait_for_timeout(300)
    assert seen == [] and lease_record(daemon) is not None       # nothing closed or released
    assert daemon.app.state.daemon.engine.open_boards() == [BOARD]


@HUB
def test_release_and_close_releases_the_lease_then_closes(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    seen = closes(page)
    close_board(page)
    by_id(page, "close-confirm").locator('[data-action="close_release"]').click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)     # the preview
    expect(by_id(page, "close-confirm")).to_have_count(0)
    assert wait_until(lambda: seen) and seen[0].endswith("?release=true")
    assert lease_record(daemon) is None                                        # released
    assert daemon.app.state.daemon.engine.open_boards() == []
    # UI v2: every hub board keeps a badge; a closed one shows what the service last knew
    # (GET /boards lease_known, no hub call), whatever that is: the real daemon remembers the
    # release (Free); the T14 mock remembers only its last GET /lease (Yours). CCR: the mock's
    # lease_known should follow a release as the service's does.
    page.wait_for_timeout(500)
    known = page.evaluate(f"() => (window.__harness_managerState().boards[{BOARD!r}] || {{}}).lease_known || null")
    assert known, "a board this service leased is remembered"
    row = rail(page).locator('[data-testid="rail-lease-row"]')
    expect(row).to_have_attribute("data-source", "known", timeout=T)
    want = "Free" if known["state"] == "free" else "Yours" if known["here"] else None
    if want:
        expect(row.locator('[data-testid="rail-lease-badge"]')).to_have_text(want)
    assert page.errors == []


@HUB
def test_negative_twin_keep_the_lease_closes_and_the_lease_stays(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    seen = closes(page)
    close_board(page)
    by_id(page, "close-confirm").locator('[data-action="close_keep"]').click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    assert wait_until(lambda: seen) and "release" not in seen[0]
    held = lease_record(daemon)
    assert held is not None and held["here"]                                   # still ours
    # open it again: the rail says Yours at once (the token never left)
    page.locator('[data-action="open"]').click()
    expect(rail(page).locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)


@HUB
@pytest.mark.parametrize("lease", ["none", "other", "elsewhere"])
def test_negative_twin_a_board_whose_lease_is_not_held_here_closes_without_asking(
        page_factory, daemon, lease):
    page = hub_page(page_factory, daemon, lease)
    seen = closes(page)
    before = lease_record(daemon)
    close_board(page)
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    assert by_id(page, "close-confirm").count() == 0
    assert wait_until(lambda: seen) and "release" not in seen[0]
    assert lease_record(daemon) == before                                      # untouched


@HUB
def test_a_release_that_fails_keeps_the_board_open_and_says_why(page_factory, daemon):
    page = hub_page(page_factory, daemon, "mine")
    page.route(re.compile(r"/boards/[^/]+\?release=true$"), lambda route: route.fulfill(
        status=503, json={"ok": False, "error": {
            "code": 7, "name": "UNREACHABLE", "message": "the hub mapstone-dev did not answer",
            "hint": "check `ssh mapstone-dev`"}}))
    close_board(page)
    dialog = by_id(page, "close-confirm")
    dialog.locator('[data-action="close_release"]').click()
    expect(dialog.locator('[data-testid="close-error"]')).to_contain_text(
        "UNREACHABLE: the hub mapstone-dev did not answer. The board is still open.", timeout=T)
    expect(by_id(page, "board-header")).to_be_visible()
    expect(dialog.locator('[data-action="close_keep"]')).to_be_enabled()      # choose again
    dialog.locator('[data-action="close_keep"]').click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    assert lease_record(daemon) is not None


# --- the demo: all three states --------------------------------------------------------------------


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def demo_open(page: Any, bid: str) -> None:
    rail(page, bid).click()
    page.wait_for_selector(f'main[data-board="{bid}"], [data-action="open"]', timeout=15_000)
    if page.locator(f'main[data-board="{bid}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    page.wait_for_selector(f'main[data-board="{bid}"] [data-testid="fact-shell"]'
                           ':not(:has-text("unknown"))', timeout=15_000)


def test_the_demo_shows_free_yours_and_held_by_alice(showcase):
    page = showcase.page()
    demo_open(page, BOARD_LEASED)
    held = rail(page, BOARD_LEASED).locator('[data-testid="rail-lease-row"]')
    expect(held.locator('[data-testid="rail-lease-badge"]')).to_have_text(f"Held by {ALICE}", timeout=T)
    expect(held.locator('[data-testid="rail-lease-queued"]')).to_have_text("Requested · #1")
    demo_open(page, BOARD_SPARE)
    spare = rail(page, BOARD_SPARE).locator('[data-testid="rail-lease-row"]')
    expect(spare.locator('[data-testid="rail-lease-badge"]')).to_have_text("Free", timeout=T)
    page.locator('[data-attention="lease"] [data-action="lease_acquire"]').click()
    expect(spare.locator('[data-testid="rail-lease-badge"]')).to_have_text("Yours", timeout=T)
    expect(held.locator('[data-testid="rail-lease-badge"]')).to_have_text(f"Held by {ALICE}")
    # the real daemon's close?release=true gives it back
    close_board(page)
    by_id(page, "close-confirm").locator('[data-action="close_release"]').click()
    expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
    demo_open(page, BOARD_SPARE)
    expect(spare.locator('[data-testid="rail-lease-badge"]')).to_have_text("Free", timeout=T)
    assert not page.errors, page.errors


def test_the_demo_names_the_hub_board_in_the_lease_badge(showcase):
    # LEASE-BOARD over the real daemon: the demo hub's board is mps3_02 (target mps3_02_pl).
    page = showcase.page()
    demo_open(page, BOARD_LEASED)
    badge = rail(page, BOARD_LEASED).locator('[data-testid="rail-lease-badge"]')
    expect(badge).to_have_attribute("title", re.compile(
        r"Held by alice@lab-pc-07: mps3_02 on mapstone-dev\.ecs\.soton\.ac\.uk "
        r"\(target mps3_02_pl\)"), timeout=T)
    assert not page.errors, page.errors


def test_negative_twin_the_demos_desk_boards_have_no_lease_badge(showcase):
    page = showcase.page()
    for bid in (BOARD_LINUX, BOARD_V011):
        demo_open(page, bid)
    page.wait_for_timeout(800)
    for bid in (BOARD_LINUX, BOARD_V011):
        assert rail(page, bid).locator('[data-testid="rail-lease-row"]').count() == 0
    assert not page.errors, page.errors
