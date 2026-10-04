"""QUICKWINS G3: "A folder of designs" in the Import dialog, and the Designs item in Settings →
Harness & kits. Fake overlay folders (kit pack of the fixture builds) over the real daemon."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from harness_manager.demo import BOARD_USB
from tests.fakes import kit_fakes as kf
from tests.web import nav
from tests.web.test_kit_ui_build_browser import by_id, fielded, import_kit
from tests.web.test_ui2_import_browser import open_import

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000


def pack(daemon: Any, base: Path, name: str, **build: str) -> Path:
    receipt = kf.passed_build(base / f"_b_{name}", name=name, **build)
    r = httpx.post(f"{daemon.url}/api/v1/kits/pack", json={"path": str(receipt)},
                   headers={"Authorization": f"Bearer {daemon.token}"}, timeout=30)
    assert r.status_code == 200, r.text
    dest = base / "overlays" / "open" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    Path(r.json()["overlay_dir"]).rename(dest)
    return dest


def ready(page_factory: Any, daemon: Any, engine: Any) -> Any:
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light", width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    return page


def test_a_folder_of_designs_lists_each_one_imported_skipped_or_refused(
        page_factory, daemon, engine, tmp_path):
    pack(daemon, tmp_path, "alpha", rm_id="0x010080F1")
    pack(daemon, tmp_path, "gamma", rm_id="0x010080F3", static_id="0x3F1A560F")
    bad = pack(daemon, tmp_path, "delta", rm_id="0x010080F4")
    m = json.loads((bad / "manifest.json").read_text())
    (bad / m["clearing"]["file"]).write_bytes(b"short")
    page = ready(page_factory, daemon, engine)
    open_import(page, way="folder")
    by_id(page, "import-folder-path").fill(str(tmp_path / "overlays"))
    by_id(page, "import-go").click()
    rows = by_id(page, "import-folder-list").locator("li")
    expect(rows).to_have_count(3, timeout=T)
    expect(page.locator('li[data-name="alpha"]')).to_have_attribute("data-state", "imported")
    expect(page.locator('li[data-name="gamma"]')).to_have_attribute("data-state", "skipped")
    expect(page.locator('li[data-name="gamma"] [data-testid="import-folder-text"]')
           ).to_contain_text("skipped: built for another static")
    expect(page.locator('li[data-name="delta"]')).to_have_attribute("data-state", "refused")
    expect(page.locator('li[data-name="delta"] [data-testid="import-folder-text"]')
           ).to_contain_text("refused: ")
    expect(by_id(page, "import-folder-ok")).to_contain_text(
        "1 imported into the overlay store, 1 skipped (another static), 1 refused.")
    assert not page.errors, page.errors


def test_twin_read_it_imports_nothing_and_an_empty_folder_says_so(
        page_factory, daemon, engine, tmp_path):
    pack(daemon, tmp_path, "alpha", rm_id="0x010080F1")
    (tmp_path / "empty").mkdir()
    page = ready(page_factory, daemon, engine)
    open_import(page, way="folder")
    by_id(page, "import-folder-path").fill(str(tmp_path / "overlays"))
    by_id(page, "import-folder-read").click()
    expect(page.locator('li[data-name="alpha"]')).to_have_attribute("data-state", "ready", timeout=T)
    assert by_id(page, "import-folder-ok").count() == 0
    by_id(page, "import-folder-path").fill(str(tmp_path / "empty"))
    by_id(page, "import-folder-read").click()
    expect(by_id(page, "import-refused")).to_contain_text("holds no design", timeout=T)
    assert not page.errors, page.errors


def test_settings_harness_and_kits_has_a_designs_item_that_opens_it(
        page_factory, daemon, engine):
    page = ready(page_factory, daemon, engine)
    page.locator('[data-action="settings"]').click()
    page.locator('[data-testid="settings-nav"] [data-settings-section="harness-kits"]').click()
    card = by_id(page, "settings-designs")
    expect(card).to_contain_text("Designs", timeout=T)
    expect(card).to_contain_text("Import every design of a harness release in one go")
    page.locator('[data-action="settings-import-designs"]').click()
    expect(by_id(page, "import-dialog")).to_be_visible(timeout=T)
    expect(by_id(page, "import-folder-path")).to_be_visible()
    assert not page.errors, page.errors


def test_twin_with_no_board_open_the_designs_button_says_why(page_factory, daemon, engine):
    page = page_factory("light", width=1440, height=900)
    page.locator('[data-action="settings"]').click()
    page.locator('[data-testid="settings-nav"] [data-settings-section="harness-kits"]').click()
    expect(by_id(page, "settings-designs-why")).to_contain_text("Open a board first", timeout=T)
    expect(page.locator('[data-action="settings-import-designs"]')).to_be_disabled()
