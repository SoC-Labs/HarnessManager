"""FIX-PACK-8 (N1 kept as a guard after N2) in the browser: the Build tab's Check step says
"boundary not timed (known issue 11)" when the build's check_timing says so: a warning, the
step still passes and Add stays open.

The REAL daemon over ``DemoEngine(showcase=True)``, its Linux board on the RC2 static, clicks
only (the walk of ``test_ui2_polish_build_browser``). The receipt is the build fakes' and the
timing report beside it a real Vivado 2026.1 report of ``minimal`` on RC2: before N2 (27,984
unclocked pins) it warns; the twin, N2's (0, and the 423 constant-clock endpoints), is clean.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import kit_fakes as kf
from tests.web import test_ui2_polish_build_browser as walk
from tests.web.test_demo_all_browser import showcase  # noqa: F401 - the fixture

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
ROOT = Path(__file__).resolve().parents[2]
NOCLOCK = ROOT / "tests/fixtures/kit/minimal_timing_noclock.rpt"
N2 = ROOT / "tests/fixtures/kit/minimal_timing_n2.rpt"
by_id = walk.by_id


def built(page: Any, tmp_path: Path, report: Path) -> None:
    """Design -> Build on the demo's Linux board, then a finished run's receipt and timing
    report land in the build directory, as Vivado's would."""
    walk.open_build(page)
    walk.expect_panel(page, "design", "current")
    by_id(page, "bd-continue").click()
    walk.expect_panel(page, "build", "current")
    bdir = tmp_path / "builds" / "minimal"
    by_id(page, "build-dir").fill(str(bdir))
    with page.expect_response(lambda r: r.url.endswith("/guide/script")) as resp:
        by_id(page, "script-write").click()
    assert resp.value.status == 200
    kf.passed_build(bdir, name="minimal", rm_id=walk.MINIMAL_RM_ID, static_id=walk.RC2,
                    usercode=walk.RC2_USERCODE, kit_id="mps3/0x44EE76D5/vivado-2026.1",
                    vivado="2026.1")
    shutil.copy(report, bdir / "out" / "minimal_timing.rpt")
    walk.expect_node(page, "build", "done")


def test_check_warns_boundary_not_timed_and_add_stays_open(showcase, tmp_path, monkeypatch):  # noqa: F811
    walk.vivado_2026(tmp_path, monkeypatch)
    page = showcase.page(**walk.APP)
    built(page, tmp_path, NOCLOCK)
    walk.expect_node(page, "check", "warn")                    # done, with a warning
    walk.node(page, "check").locator("button").click()
    walk.expect_panel(page, "check")
    expect(by_id(page, "check-passed")).to_contain_text(f"minimal {walk.MINIMAL_RM_ID}")
    warn = by_id(page, "check-boundary")
    expect(warn).to_contain_text(
        "Warning: boundary not timed (known issue 11): Vivado's check_timing in "
        "minimal_timing.rpt found 27,984 register/latch pins with no clock (27,446 on OSCCLK1;")
    expect(warn).to_contain_text("Your RM's own paths were still timed.")
    expect(by_id(page, "check-boundary-fix")).to_contain_text(
        "Fix: rebuild with a build_rm.tcl written by this Harness Manager")
    timing = page.locator('[data-group="timing"]')
    expect(timing).to_have_attribute("data-state", "warn")
    expect(timing).to_contain_text("boundary not timed (known issue 11)")
    walk.expect_node(page, "add", "current")                   # a warning never blocks Add
    assert page.errors == []


def test_twin_an_n2_build_checks_clean(showcase, tmp_path, monkeypatch):  # noqa: F811
    walk.vivado_2026(tmp_path, monkeypatch)
    page = showcase.page(**walk.APP)
    built(page, tmp_path, N2)
    walk.expect_node(page, "check", "done")
    walk.node(page, "check").locator("button").click()
    walk.expect_panel(page, "check")
    expect(by_id(page, "check-passed")).to_be_visible()
    expect(by_id(page, "check-boundary")).to_have_count(0)
    expect(page.locator('[data-group="timing"]')).to_have_attribute("data-state", "ok")
    walk.expect_node(page, "add", "current")
    assert page.errors == []
