"""HIL-GUI review screenshots, light and dark: the Checks section's Start form (the lease free,
taken for the run, the announcement written), Start off under someone else's lease, a run in
progress, and a stopped run's REPORT.md.

They land in tests/web/screenshots/review/checks-*.png (gitignored); the curated copies for
david are committed as docs/review/2026-09-29/checks-*.png. Each shot asserts the state it
shows first, so a picture never shows the wrong thing.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE
from tests.web.test_demo_all_browser import Showcase, by_id, make_showcase
from tests.web.test_hil_gui_browser import T, checks_of, open_checks, scripted

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def show(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def shoot(page: Any, review: Any, name: str, scheme: str) -> None:
    page.wait_for_timeout(250)                   # the last render and fonts
    page.screenshot(path=str(review / f"checks-{name}-{scheme}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshot_the_start_form_and_the_announcement(show, review, scheme):
    page = show.page(scheme)
    open_checks(page, BOARD_SPARE)
    by_id(page, "checks-take-lease").locator("input").check()
    by_id(page, "checks-preview").click()
    expect(by_id(page, "checks-announce")).not_to_have_value("", timeout=T)
    expect(by_id(page, "checks-start")).to_be_enabled()
    shoot(page, review, "start", scheme)


def test_screenshot_start_off_under_someone_elses_lease(show, review):
    page = show.page("light")
    open_checks(page, BOARD_LEASED)
    expect(by_id(page, "checks-lease")).to_contain_text("alice", timeout=T)
    shoot(page, review, "held", "light")


def test_screenshot_the_linux_boards_auto_plan(show, review):
    page = show.page("light")
    open_checks(page, BOARD_LINUX)
    expect(by_id(page, "checks-auto-why")).to_contain_text("valid", timeout=T)
    shoot(page, review, "linux-auto", "light")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshot_a_run_in_progress_then_its_report(show, review, scheme):
    hm = scripted(show)
    hm.delay = 0.35
    page = show.page(scheme)
    open_checks(page, BOARD_SPARE)
    by_id(page, "checks-take-lease").locator("input").check()
    by_id(page, "checks-start").click()
    page.wait_for_function("() => /Iteration [2-9]/.test((document.querySelector("
                           "'[data-testid=checks-iteration]') || {}).textContent || '')",
                           timeout=T)
    expect(by_id(page, "checks-now")).to_contain_text(re.compile("Checking|Last check"), timeout=T)
    shoot(page, review, "running", scheme)
    by_id(page, "checks-stop").click()
    expect(by_id(page, "checks-last")).to_contain_text("STOPPED", timeout=T)
    last = checks_of(show).status(BOARD_SPARE)["last"]
    page.locator(f'[data-testid="checks-runs-table"] tr[data-run="{last["id"]}"]').click()
    expect(by_id(page, "checks-report-md").locator("h3")).to_contain_text("HIL-AUTO", timeout=T)
    shoot(page, review, "report", scheme)
