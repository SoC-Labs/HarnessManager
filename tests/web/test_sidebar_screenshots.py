"""SIDEBAR-UX in the demo (``harness-manager app --demo``), and its review screenshots.

The demo's order and favourites are the demo's own settings (its state dir's settings.toml),
never the real ``~/.config``. The pictures land in tests/web/screenshots/review/sidebar-*.png
(gitignored); the curated copies for david are docs/review/2026-09-28/sidebar-*.png. Each
shot asserts the state it photographs first.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import tomllib

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from tests.web.test_demo_all_browser import Showcase, T, by_id, make_showcase
from tests.web.test_sidebar_browser import card, drag, rail_ids

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])
LAB = "mps3@192.168.10.121:6900"
LAB_TOML = """\
[boards.lab]
match = ["192.168.10.121"]
name = "lab-mps3"
via = "hub"
hub = { use = "mapstone-dev", target = "mps3_01_pl" }
"""


@pytest.fixture
def review(screenshots) -> Path:
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def showcase(browser, tmp_path, monkeypatch, request) -> Iterator[Showcase]:
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def shoot(page: Any, review: Path, name: str, scheme: str) -> None:
    page.wait_for_timeout(300)
    assert not page.errors, page.errors
    page.screenshot(path=str(review / f"sidebar-{name}-{scheme}.png"))


def demo_dir(showcase: Showcase) -> Path:
    return Path(showcase.engine.state_dir)


def test_the_demo_keeps_its_order_and_favourites_in_its_own_settings(showcase):
    page = showcase.page()
    expect(page.locator(".rail-card")).to_have_count(4, timeout=T)
    page.locator(f'.rail-card[data-board="{BOARD_SPARE}"] [data-testid="rail-star"]').click()
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_count(1, timeout=T)
    rest = rail_ids(page)[1:]
    drag(page, rest[-1], rest[0])
    expect(page.locator('[data-testid="rail-group-rest"] .rail-card').first).to_have_attribute(
        "data-board", rest[-1], timeout=T)
    own = demo_dir(showcase) / "settings.toml"
    for _ in range(50):
        if own.exists() and "favourite_boards" in own.read_text():
            break
        page.wait_for_timeout(100)
    want = [rest[-1], *rest[:-1]]
    for _ in range(50):
        data = tomllib.loads(own.read_text())
        if [b for b in data["general"].get("board_order", []) if b != BOARD_SPARE] == want:
            break
        page.wait_for_timeout(100)
    assert data["general"]["favourite_boards"] == [BOARD_SPARE]
    assert [b for b in data["general"]["board_order"] if b != BOARD_SPARE] == want
    assert set(data["general"]["board_order"]) == {BOARD_LINUX, BOARD_V011, BOARD_LEASED, BOARD_SPARE}
    # twin: the test's own "user" config dir (what ~/.config is outside a --demo service)
    # holds no sidebar settings
    user = Path(os.environ["HARNESS_MANAGER_STATE_DIR"]) / "settings.toml"
    assert not user.exists() or "board_order" not in user.read_text()
    assert page.errors == []


@SCHEMES
def test_review_the_sidebar(showcase, review, scheme):
    (demo_dir(showcase) / "boards.toml").write_text(LAB_TOML)
    page = showcase.page(scheme)
    expect(page.locator(".rail-card")).to_have_count(1, timeout=T)          # boards.toml, not contacted
    page.locator('[data-action="rescan"]').click()
    expect(page.locator(".rail-card")).to_have_count(5, timeout=T)
    expect(by_id(page, "scan-offer").locator(f'[data-offer="{LAB}"]')).to_contain_text("lab-mps3")
    expect(by_id(page, "scan-line")).to_contain_text("4 boards; 1 more in boards.toml")
    card(page, BOARD_LINUX).click()
    shoot(page, review, "scan-offer", scheme)

    # favourites at the top, and a board mid-drag with the line where it will land
    for bid in (BOARD_LINUX, BOARD_V011):
        page.locator(f'.rail-card[data-board="{bid}"] [data-testid="rail-star"]').click()
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_count(2, timeout=T)
    rest = page.eval_on_selector_all('[data-testid="rail-group-rest"] .rail-card',
                                     "els => els.map(e => e.dataset.board)")
    src = card(page, rest[-1]).bounding_box()
    dst = card(page, rest[0]).bounding_box()
    page.mouse.move(src["x"] + 40, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + 60, dst["y"] + 4, steps=12)
    expect(page.locator(f'.rail-card.drop-before[data-board="{rest[0]}"]')).to_have_count(1, timeout=T)
    shoot(page, review, "drag", scheme)
    page.mouse.up()
    expect(page.locator('[data-testid="rail-group-rest"] .rail-card').first).to_have_attribute(
        "data-board", rest[-1], timeout=T)
    page.mouse.move(700, 400)
    card(page, LAB).hover()
    shoot(page, review, "favourites", scheme)

    # the boards.toml board: its preview says how Open reaches it
    card(page, LAB).click()
    expect(by_id(page, "preview-route")).to_contain_text("through the hub mapstone-dev")
    shoot(page, review, "boards-toml-preview", scheme)

    # Add by address: the matching entry's hub, shown and used
    page.locator('[aria-label="Add a board by address"]').click()
    page.locator('[aria-label="Board address"]').fill("192.168.10.121")
    expect(by_id(page, "add-via")).to_have_value("hub")
    expect(by_id(page, "add-route")).to_contain_text("boards.toml lab: through the hub mapstone-dev")
    shoot(page, review, "add-route", scheme)
