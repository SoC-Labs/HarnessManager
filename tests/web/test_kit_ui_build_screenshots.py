"""UI v2 Build tab (lane UI2-BUILD): review screenshots, light and dark, over the real daemon.

Four scenes, each asserted before it is photographed: Design with My RTL (the constraints and
the pblock), the Vivado wait (Run it your way, a build running at impl), a build refused at
Check (rm_timing, the one gate and its fix), and Add done with the pblock's utilisation. Plus
the Import dialog after a refusal (exit 14).

They land in tests/web/screenshots/review/ (gitignored); curated copies go under docs/review/.
"""

from __future__ import annotations

import time

import pytest

from tests.fakes import kit_fakes as kf
from tests.fakes import ui2_build_fakes as uf
from tests.web.test_kit_ui_build_browser import (
    OTHER,
    by_id,
    choose_design,
    expect_node,
    expect_panel,
    fielded,
    import_kit,
    look,
    open_build,
    watch,
)
from tests.web.test_ui2_import_browser import open_import

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 900}
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def full_shot(page, path) -> None:
    """The whole tab: the window grows to the section's height (it scrolls inside)."""
    h = page.evaluate("""() => { const b = document.querySelector('.section-body');
      return Math.ceil(b.getBoundingClientRect().top + b.scrollHeight) + 4; }""")
    page.set_viewport_size({"width": APP["width"], "height": max(APP["height"], h)})
    page.evaluate("() => document.querySelector('.section-body').scrollTo(0, 0)")
    page.wait_for_timeout(300)
    page.screenshot(path=str(path))
    page.set_viewport_size(APP)


def ready(page_factory, daemon, engine, tmp_path, monkeypatch, scheme):
    fielded(engine)
    import_kit(daemon)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(kf.fake_vivado_script(tmp_path / "v")))
    page = page_factory(scheme, **APP)
    open_build(page)
    expect_node(page, "setup", "done")
    return page


@SCHEMES
def test_review_build_design_my_rtl(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    page = ready(page_factory, daemon, engine, tmp_path, monkeypatch, scheme)
    rtl = uf.rtl(tmp_path / "rtl")
    page.locator('[data-testid="bd-design-source"] [data-src="rtl"]').click()
    by_id(page, "bd-rtl-path").fill(str(rtl))
    by_id(page, "bd-rtl-read").click()
    expect(by_id(page, "bd-scan")).to_contain_text("top rm_demo", timeout=T)
    by_id(page, "bd-rm-xdc").fill(str(rtl / "demo_rm.xdc"))
    expect(by_id(page, "bd-pblock")).to_contain_text("SLICE_X48Y0:SLICE_X95Y119")
    full_shot(page, review / f"build-design-rtl-{scheme}.png")
    assert page.errors == []


@SCHEMES
def test_review_build_running(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    page = ready(page_factory, daemon, engine, tmp_path, monkeypatch, scheme)
    choose_design(page)
    bdir = tmp_path / "build" / "minimal"
    by_id(page, "build-dir").fill(str(bdir))
    by_id(page, "script-write").click()
    expect(by_id(page, "bd-way")).to_be_visible(timeout=T)
    page.screenshot(path=str(review / f"build-run-your-way-{scheme}.png"))
    uf.running_log(bdir, "impl", started=time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(time.time() - 1500)),
                   stage_at=time.time() - 400)
    by_id(page, "build-reload").click()
    expect_node(page, "build", "running")
    expect(by_id(page, "bd-running")).to_have_attribute("data-stage", "impl")
    page.wait_for_timeout(300)
    page.screenshot(path=str(review / f"build-running-{scheme}.png"))
    assert page.errors == []


@SCHEMES
def test_review_build_refused_at_check(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    page = ready(page_factory, daemon, engine, tmp_path, monkeypatch, scheme)
    bdir = tmp_path / "build" / "spike_rm"
    kf.passed_build(bdir, state="failed", stage="impl", gates=[
        {"gate": "static_id", "verdict": "PASS", "detail": "CRC-32 is 0x72BB0A36"},
        {"gate": "no_black_boxes", "verdict": "PASS", "detail": "0 black boxes"},
        {"gate": "rm_timing", "verdict": "FAIL", "detail": "setup WNS -0.412 ns over the RM's 34 registers"}])
    choose_design(page)
    watch(page, bdir)
    expect_node(page, "check", "failed")
    expect(by_id(page, "check-refused")).to_have_attribute("data-gate", "rm_timing")
    full_shot(page, review / f"build-refused-{scheme}.png")
    assert page.errors == []


@SCHEMES
def test_review_build_added_with_utilisation(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    page = ready(page_factory, daemon, engine, tmp_path, monkeypatch, scheme)
    bdir = tmp_path / "build" / "spike_rm"
    kf.passed_build(bdir)
    uf.util_report(bdir / "out", "spike_rm")
    choose_design(page)
    watch(page, bdir)
    expect_panel(page, "add", "current")
    look(page, "check")
    expect(by_id(page, "bd-util").locator('[data-meter="LUT"]')).to_contain_text("11,051 / 42,824")
    full_shot(page, review / f"build-check-utilisation-{scheme}.png")
    look(page, "add")
    page.locator('[data-action="kit_pack"]').click()
    page.wait_for_selector('[data-testid="section-workbench"]', timeout=T)
    page.locator('.section-tab[data-section="build"]').click()
    expect_node(page, "add", "done")
    page.screenshot(path=str(review / f"build-added-{scheme}.png"))
    assert page.errors == []


@SCHEMES
def test_review_import_refused(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    page = ready(page_factory, daemon, engine, tmp_path, monkeypatch, scheme)
    bdir = tmp_path / "other"
    kf.passed_build(bdir, name="uart_loop", static_id=OTHER)
    open_import(page, way="path")
    by_id(page, "import-path").fill(str(bdir))
    by_id(page, "import-go").click()
    expect(by_id(page, "import-exit")).to_have_text("exit 14 INCOMPATIBLE", timeout=T)
    page.screenshot(path=str(review / f"import-refused-{scheme}.png"))
    assert page.errors == []
