"""HIL-GUI in the browser: the Checks section over the REAL daemon and ``DemoEngine(showcase=True)``.

The run's commands are a scripted Harness Manager CLI (``tests/fakes/hil_auto.ScriptedHm``: a
bare-metal board on the runbook's static, a little slow so the progress shows) on a clock
that skips the waits; the lease is the demo hub's (mps3-03 free, mps3-02 alice's). Every
behaviour has its twin:

- the plan is picked from the board: the Linux showcase board gets ``linux`` (a valid OS
  slot), the bare-metal one ``bare-metal``; the page's rule is the service's (one table);
- Start: someone else's lease disables it and names them; a free lease needs "Take the
  lease for the run" ticked;
- a run: progress, Stop (greybox back), the report and each iteration's, the lease taken for
  the run and given back; the announcement and its Copy.

``test_hil_gui_screenshots`` below photographs the same for the review
(``docs/review/2026-09-29/checks-*.png``).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from tests.fakes.hil_auto import GREYBOX, ScriptedHm
from tests.web.test_demo_all_browser import Showcase, by_id, make_showcase, open_board, section

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 20_000
CASES = Path(__file__).resolve().parent / "hil_gui_auto_cases.json"


class FastTime:
    """The runs' clock and sleep: real time plus every wait skipped (a 30-min interval passes
    at once), so an overnight run's iterations follow each other in the browser."""

    def __init__(self) -> None:
        self.offset = 0.0
        self._mu = threading.Lock()

    def clock(self) -> float:
        with self._mu:
            return time.time() + self.offset

    def sleep(self, seconds: float) -> None:
        with self._mu:
            self.offset += max(seconds, 0.0)
        time.sleep(0.01)


class SlowHm(ScriptedHm):
    """The bare-metal board as the CLI shows it, each command taking a moment."""

    delay = 0.12

    def __call__(self, argv, timeout):
        time.sleep(self.delay)
        return super().__call__(argv, timeout)


@pytest.fixture
def show(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def checks_of(show: Showcase) -> Any:
    return show.daemon.app.state.daemon.checks


def scripted(show: Showcase) -> SlowHm:
    hm = SlowHm(static="0x72bb0a36", impl="bare-metal")
    ft = FastTime()
    runs = checks_of(show)
    runs.invoker_factory = lambda _run: hm
    runs.clock, runs.sleep = ft.clock, ft.sleep
    return hm


def open_checks(page: Any, bid: str) -> None:
    open_board(page, bid)
    section(page, "checks")
    expect(by_id(page, "checks-card")).to_be_visible(timeout=T)


# --- the plan a board gets ------------------------------------------------------------------------


def test_the_plan_is_picked_from_the_board_and_can_be_overridden(show):
    # UI v2: Checks lives on hub boards only (a run takes the hub lease); the showcase's hub
    # boards are bare metal, so the Linux plan is the page's rule, checked against the
    # service's table below (test_the_pages_auto_rule_is_the_services)
    page = show.page()
    open_checks(page, BOARD_SPARE)
    expect(by_id(page, "checks-plan").locator("option").first).to_have_text("Auto: bare-metal",
                                                                             timeout=T)
    by_id(page, "checks-plan").select_option("linux-netboot")
    expect(by_id(page, "checks-auto-why")).to_contain_text("Chosen by hand. Auto would pick bare-metal")
    assert not page.errors, page.errors


def test_twin_a_board_with_no_hub_has_no_checks_tab_and_its_old_key_lands_on_the_overview(show):
    page = show.page()
    for bid in (BOARD_LINUX, BOARD_V011):
        open_board(page, bid)
        expect(page.locator('.section-tab[data-section="board"]')).to_be_visible(timeout=T)
        expect(page.locator('.section-tab[data-section="checks"]')).to_have_count(0)
    page.evaluate("""async (bid) => {
        const m = await import(new URL("js/store.js", document.baseURI).href);
        m.setSection(bid, "checks");
    }""", BOARD_V011)
    expect(by_id(page, "section-overview")).to_be_visible(timeout=T)
    assert not page.errors, page.errors


def test_the_pages_auto_rule_is_the_services(show):
    """checks.js ``autoPlan`` against ``checks.plans.auto_plan``'s table (test_hil_gui_units)."""
    page = show.page()
    cases = json.loads(CASES.read_text())
    got = page.evaluate("""async (cases) => {
        const m = await import(new URL("js/sections/checks.js", document.baseURI).href);
        return cases.map(([ident, card]) => m.autoPlan(ident, card).plan);
    }""", cases)
    assert got == [plan for _i, _c, plan in cases]


# --- the lease decides Start -------------------------------------------------------------------------


def test_a_lease_someone_else_holds_disables_start_and_names_them(show):
    page = show.page()
    open_checks(page, BOARD_LEASED)
    expect(by_id(page, "checks-start")).to_be_disabled(timeout=T)
    expect(by_id(page, "checks-lease")).to_contain_text("alice", timeout=T)
    expect(by_id(page, "checks-start-why")).to_have_count(0)            # said once, by the lease line
    expect(by_id(page, "checks-take-lease")).to_have_count(0)


def test_twin_a_free_lease_starts_once_take_the_lease_is_ticked(show):
    page = show.page()
    open_checks(page, BOARD_SPARE)
    expect(by_id(page, "checks-start")).to_be_disabled(timeout=T)
    expect(by_id(page, "checks-start-why")).to_contain_text("Take the lease for the run")
    by_id(page, "checks-take-lease").locator("input").check()
    expect(by_id(page, "checks-start")).to_be_enabled()
    expect(by_id(page, "checks-start-why")).to_have_count(0)


# --- a run: progress, stop, the report ------------------------------------------------------------------


def pin_until(page: Any) -> None:
    """Run until 12 h from now, not the form's 08:30: a run started 08:20-08:30 (inside the
    10-min margin) would end at once, before its first iteration."""
    until = (datetime.now() + timedelta(hours=12)).strftime("%H:%M")
    by_id(page, "checks-until").fill(until)
    expect(by_id(page, "checks-until")).to_have_value(until)


def start_run(page: Any, *, writes: str = "safe") -> None:
    pin_until(page)
    by_id(page, "checks-take-lease").locator("input").check()
    by_id(page, f"checks-writes-{writes}").click()
    by_id(page, "checks-start").click()
    expect(by_id(page, "checks-run")).to_be_visible(timeout=T)


def test_start_progress_stop_and_the_report(show):
    hm = scripted(show)
    page = show.page()
    open_checks(page, BOARD_SPARE)
    start_run(page)
    # live progress: an iteration, a check, counts; the tab and the banner say a run is on
    expect(by_id(page, "checks-iteration")).not_to_have_text("Iteration 0 of", timeout=T)
    expect(by_id(page, "checks-totals")).to_contain_text("pass", timeout=T)
    expect(by_id(page, "checks-tab-badge")).to_be_visible()
    expect(by_id(page, "checks-banner")).to_contain_text("Checks are running")
    page.wait_for_function("() => /Iteration [2-9]/.test(document.querySelector("
                           "'[data-testid=checks-iteration]').textContent)", timeout=T)
    by_id(page, "checks-stop").click()
    expect(by_id(page, "checks-last")).to_contain_text("STOPPED", timeout=T)
    expect(by_id(page, "checks-run")).to_have_count(0)
    expect(by_id(page, "checks-banner")).to_have_count(0)
    assert hm.rm == GREYBOX, "Stop put greybox back"
    last = checks_of(show).status(BOARD_SPARE)["last"]
    assert last["lease"]["taken"] and last["lease"]["released"], last["lease"]
    # the past runs list it; its REPORT.md renders, and each iteration's
    row = page.locator(f'[data-testid="checks-runs-table"] tr[data-run="{last["id"]}"]')
    expect(row).to_contain_text("STOPPED", timeout=T)
    row.click()
    md = by_id(page, "checks-report-md")
    expect(md.locator("h3")).to_contain_text("HIL-AUTO: bare-metal", timeout=T)
    expect(md).to_contain_text("STOPPED")
    expect(md.locator("table").first).to_be_visible()
    page.locator('[data-testid="checks-iterations"] button[data-iteration="1"]').click()
    expect(md.locator("h3")).to_contain_text("HIL-AUTO: bare-metal", timeout=T)
    expect(md).to_contain_text("iteration 1 of")
    assert "<script" not in md.inner_html()
    assert not page.errors, page.errors


def test_the_announcement_and_its_copy(show):
    ctx = show.browser.new_context(viewport={"width": 1440, "height": 1000})
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=show.daemon.url)
    show.contexts.append(ctx)
    page = ctx.new_page()
    page.goto(show.daemon.ui_url)
    page.wait_for_selector(".board-item", timeout=T)
    open_checks(page, BOARD_SPARE)
    by_id(page, "checks-take-lease").locator("input").check()
    expect(by_id(page, "checks-copy")).to_be_disabled()             # nothing written yet
    by_id(page, "checks-preview").click()
    text = by_id(page, "checks-announce")
    expect(text).to_have_value(re.compile(r"^HM HIL-AUTO on 192\.168\.10\.107"),
                               timeout=T)
    value = text.input_value()
    assert "writes safe" in value and "never " in value and "the app's Checks section: Stop" in value
    assert "free at Start: the Harness Manager service takes it" in value
    by_id(page, "checks-copy").click()
    expect(by_id(page, "checks-copy")).to_contain_text("Copied", timeout=T)
    assert page.evaluate("() => navigator.clipboard.readText()") == value
    assert checks_of(show).active(BOARD_SPARE) is None                # a preview starts nothing
