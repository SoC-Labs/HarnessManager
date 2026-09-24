"""KIT-UI: review screenshots of the Build section, light and dark, over the real daemon.

Three scenes, each asserted before it is photographed: the journey half-way (kit fetched,
Vivado matching, the script generated into a build directory), a build that failed with a
Vivado of another release on the machine, and every step done after "Add to Program".

They land in tests/web/screenshots/review/ (gitignored); the curated copies are committed
under docs/review/2026-09-25/ as build-*.png.
"""

from __future__ import annotations

import pytest

from tests.fakes import kit_fakes as kf
from tests.web.test_kit_ui_build_browser import expect_state, fielded, import_kit, open_build

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 900}


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def full_shot(page, path) -> None:
    """The whole section: the window grows to the section's height (it scrolls inside)."""
    h = page.evaluate("""() => { const b = document.querySelector('.section-body');
      return Math.ceil(b.getBoundingClientRect().top + b.scrollHeight) + 4; }""")
    page.set_viewport_size({"width": APP["width"], "height": max(APP["height"], h)})
    page.evaluate("() => document.querySelector('.section-body').scrollTo(0, 0)")
    page.wait_for_timeout(300)
    page.screenshot(path=str(path))


def set_build_dir(page, path) -> None:
    page.locator('[data-testid="build-dir"]').fill(str(path))
    page.locator('[data-testid="build-dir"]').blur()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_build_journey(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    fielded(engine)
    import_kit(daemon)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(kf.fake_vivado_script(tmp_path / "v")))
    page = page_factory(scheme, **APP)
    open_build(page)
    set_build_dir(page, tmp_path / "build" / "minimal")
    expect_state(page, "tools", "done")
    page.locator('[data-testid="script-generate"]').click()
    expect(page.locator('[data-testid="script-files"]')).to_be_visible(timeout=T)
    expect_state(page, "build", "next")
    expect(page.locator('[data-testid="build-next"]')).to_contain_text("5 Build")
    full_shot(page, review / f"build-journey-{scheme}.png")
    assert page.errors == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_build_failed(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    fielded(engine)
    import_kit(daemon)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO",
                       str(kf.fake_vivado_script(tmp_path / "v", release="2023.2", build=4029153)))
    bdir = tmp_path / "build" / "spike_rm"
    kf.passed_build(bdir, state="failed", stage="impl", gates=[
        {"gate": "static_id", "verdict": "PASS", "detail": "CRC-32 is 0x72BB0A36"},
        {"gate": "no_black_boxes", "verdict": "PASS", "detail": "0 black boxes"},
        {"gate": "rm_timing", "verdict": "FAIL",
         "detail": "setup WNS -0.412 ns over the RM's 34 registers"}])
    page = page_factory(scheme, **APP)
    open_build(page)
    set_build_dir(page, bdir)
    expect_state(page, "build", "failed")
    expect(page.locator('[data-testid="vivado-mismatch"]')).to_be_visible()
    expect(page.locator('[data-testid="trouble"][data-failing="true"]')).to_have_count(2)
    full_shot(page, review / f"build-failed-{scheme}.png")
    assert page.errors == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_review_build_added(page_factory, daemon, engine, tmp_path, monkeypatch, review, scheme):
    fielded(engine)
    import_kit(daemon)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(kf.fake_vivado_script(tmp_path / "v")))
    bdir = tmp_path / "build" / "spike_rm"
    kf.passed_build(bdir)
    page = page_factory(scheme, **APP)
    open_build(page)
    set_build_dir(page, bdir)
    expect_state(page, "check", "next")
    page.locator('[data-action="kit_check"]').click()
    expect(page.locator('[data-testid="check-result"]')).to_contain_text("passed:", timeout=T)
    page.locator('[data-action="kit_pack"]').click()
    expect(page.locator('[data-testid="pack-result"]')).to_contain_text("is in Program", timeout=T)
    expect_state(page, "check", "done")
    expect(page.locator('[data-testid="build-next"]')).to_contain_text("Every step is done")
    full_shot(page, review / f"build-added-{scheme}.png")
    assert page.errors == []
