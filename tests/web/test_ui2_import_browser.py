"""UI v2 Import dialog (lane UI2-BUILD, UI_V2_PLAN.md M6; ``js/import.js``,
``registerModal("import")``) in a headless system Chrome, over the real harness-manager-daemon and
the T14 mock (both run the real ``kit_api`` routes: POST /overlays/import and /overlays/upload).

The Workbench opens it with ``openModal("import", {bid})``; these tests open it the same way
(the Workbench lane owns the button). The demo USB board is moved to 0x72BB0A36, the fixture
kit's static. Each behaviour has its negative twin: another shell refuses with exit 14
INCOMPATIBLE, a build that did not pass with exit 15 REFUSED, a path that is not there with
ABSENT; nothing is imported then.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from harness_manager.demo import BOARD_USB
from tests.fakes import kit_fakes as kf
from tests.web import nav
from tests.web.test_kit_ui_build_browser import (
    OTHER,
    by_id,
    choose_design,
    expect_node,
    fielded,
    import_kit,
    open_build,
    picked,
    watch,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000


def open_import(page: Any, bid: str = BOARD_USB, way: str = "") -> Any:
    page.evaluate("([bid, way]) => import('./js/modal.js').then((m) => m.openModal('import', way ? {bid, way} : {bid}))",
                  [bid, way])
    dlg = by_id(page, "import-dialog")
    expect(dlg).to_be_visible(timeout=T)
    return dlg


def group(page: Any, gid: str) -> Any:
    return by_id(page, "import-groups").locator(f'[data-group="{gid}"]')


def zip_out(build_dir: Path, dest: Path) -> Path:
    """A build's out/ as the zip a user would send (receipt + the pair it names)."""
    with zipfile.ZipFile(dest, "w") as z:
        for f in (build_dir / "out").iterdir():
            z.write(f, f"out/{f.name}")
    return dest


def ready_page(page_factory: Any, daemon: Any, engine: Any) -> Any:
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light", width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    return page


# --- a path on this machine ------------------------------------------------------------------------


@pytest.mark.mock_too
def test_a_path_is_read_then_imported_and_lands_picked_on_the_workbench(page_factory, daemon, engine, tmp_path):
    bdir = tmp_path / "b"
    kf.passed_build(bdir)
    page = ready_page(page_factory, daemon, engine)
    open_import(page)
    # before anything is read: the five rows say what is checked
    rows = by_id(page, "import-groups").locator("li")
    expect(rows).to_have_count(5)
    expect(group(page, "shell")).to_contain_text("0x72BB0A36")
    expect(by_id(page, "import-go")).to_be_disabled()
    by_id(page, "import-way-path").click()
    by_id(page, "import-path").fill(str(bdir))
    by_id(page, "import-read").click()                            # check_only: nothing written
    expect(by_id(page, "import-picked")).to_contain_text("spike_rm", timeout=T)
    expect(by_id(page, "import-picked")).to_contain_text("build receipt")
    for gid in ("files", "shell", "rm_id", "crc"):
        expect(group(page, gid)).to_have_attribute("data-state", "ok")
    expect(group(page, "pair")).to_have_attribute("data-state", "unchecked")   # static_binding
    expect(by_id(page, "import-checked")).to_contain_text("spike_rm passes")
    assert not (bdir / "overlay").exists()
    by_id(page, "import-go").click()
    ok = by_id(page, "import-ok")
    expect(ok).to_contain_text("Imported spike_rm 0x010080F0", timeout=T)
    assert (bdir / "overlay" / "spike_rm" / "manifest.json").is_file()
    by_id(page, "import-done").click()
    page.wait_for_selector('[data-testid="section-workbench"]', timeout=T)
    expect(by_id(page, "import-dialog")).to_have_count(0)
    assert picked(page) == "spike_rm"
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_build_for_another_shell_is_refused_exit_14_and_nothing_is_imported(
        page_factory, daemon, engine, tmp_path):
    bdir = tmp_path / "other"
    kf.passed_build(bdir, name="uart_loop", static_id=OTHER)
    page = ready_page(page_factory, daemon, engine)
    open_import(page, way="path")
    by_id(page, "import-path").fill(str(bdir))
    by_id(page, "import-go").click()
    refused = by_id(page, "import-refused")
    expect(refused).to_have_attribute("data-error", "INCOMPATIBLE", timeout=T)
    expect(by_id(page, "import-exit")).to_have_text("exit 14 INCOMPATIBLE")
    expect(refused).to_contain_text("Nothing was imported")
    expect(refused).to_contain_text(f"import it on a board that runs {OTHER}")
    expect(group(page, "shell")).to_have_attribute("data-state", "mismatch")
    expect(group(page, "shell")).to_contain_text(f"built for {OTHER}")
    expect(by_id(page, "import-done")).to_have_count(0)
    assert not (bdir / "overlay").exists()
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_build_that_did_not_pass_is_refused_exit_15_and_a_missing_path_is_absent(
        page_factory, daemon, engine, tmp_path):
    bdir = tmp_path / "failed"
    kf.passed_build(bdir, state="failed", stage="impl", gates=[
        {"gate": "rm_timing", "verdict": "FAIL", "detail": "setup WNS -0.412 ns"}])
    page = ready_page(page_factory, daemon, engine)
    open_import(page, way="path")
    by_id(page, "import-path").fill(str(bdir))
    by_id(page, "import-go").click()
    expect(by_id(page, "import-refused")).to_have_attribute("data-error", "REFUSED", timeout=T)
    expect(by_id(page, "import-exit")).to_have_text("exit 15 REFUSED")
    by_id(page, "import-path").fill(str(tmp_path / "nothing-here"))
    by_id(page, "import-go").click()
    expect(by_id(page, "import-refused")).to_have_attribute("data-error", "ABSENT", timeout=T)
    expect(by_id(page, "import-refused")).to_contain_text("Not imported")
    assert not (bdir / "overlay").exists()
    assert page.errors == []


# --- a zip, sent as the request body -----------------------------------------------------------------


@pytest.mark.mock_too
def test_a_zip_is_uploaded_and_imported_and_its_twin_for_another_shell_is_refused(
        page_factory, daemon, engine, tmp_path):
    good = tmp_path / "good"
    kf.passed_build(good, name="zip_rm", rm_id="0x0100A3C1")
    other = tmp_path / "other"
    kf.passed_build(other, name="uart_loop", static_id=OTHER)
    page = ready_page(page_factory, daemon, engine)
    # the twin first: another shell's build, zipped
    open_import(page)
    by_id(page, "import-file").set_input_files(str(zip_out(other, tmp_path / "uart_loop.zip")))
    expect(by_id(page, "import-picked")).to_contain_text("uart_loop.zip")
    with page.expect_response(lambda r: "/overlays/upload" in r.url) as resp:
        by_id(page, "import-go").click()
    assert resp.value.status == 409
    assert resp.value.request.headers.get("content-type") == "application/zip"
    expect(by_id(page, "import-exit")).to_have_text("exit 14 INCOMPATIBLE", timeout=T)
    expect(group(page, "shell")).to_have_attribute("data-state", "mismatch")
    # a receipt alone cannot travel: its pair is beside it on disk
    receipt = other / "out" / "uart_loop_build.json"
    by_id(page, "import-file").set_input_files(str(receipt))
    expect(by_id(page, "import-note")).to_contain_text("is a receipt")
    expect(by_id(page, "import-go")).to_be_disabled()
    # the good zip: imported into the store (the upload's own folder is removed)
    by_id(page, "import-file").set_input_files(str(zip_out(good, tmp_path / "zip_rm.zip")))
    by_id(page, "import-go").click()
    expect(by_id(page, "import-ok")).to_contain_text("Imported zip_rm 0x0100A3C1", timeout=T)
    assert page.errors == []


# --- From Build ------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_from_build_lists_what_the_build_tab_checked_and_its_twin_lists_nothing(
        page_factory, daemon, engine, tmp_path):
    bdir = tmp_path / "b"
    kf.passed_build(bdir)
    page = ready_page(page_factory, daemon, engine)
    # the twin first: nothing checked in this page yet
    open_import(page, way="build")
    expect(by_id(page, "import-built-none")).to_be_visible()
    page.keyboard.press("Escape")
    expect(by_id(page, "import-dialog")).to_have_count(0)
    # the Build tab checks a passed build: From Build lists it (not added yet)
    open_build(page)
    choose_design(page)
    watch(page, bdir)
    expect_node(page, "check", "done")
    dlg = open_import(page)
    expect(by_id(page, "import-way-build")).to_contain_text("From Build · 1")
    by_id(page, "import-way-build").click()
    row = by_id(page, "import-built").locator('li[data-name="spike_rm"]')
    expect(row).to_contain_text("0x010080F0")
    row.get_by_role("button", name="Select").click()
    by_id(page, "import-go").click()
    expect(by_id(page, "import-ok")).to_contain_text("Imported spike_rm", timeout=T)
    assert (bdir / "overlay" / "spike_rm" / "manifest.json").is_file()
    assert dlg.is_visible()
    assert page.errors == []
