"""SIDEBAR-UX in the browser: order, favourites, the boards.toml boards, Scan and Add.

david asked for draggable board cards and favourites, and hit three bugs in the same rail on
2026-09-28: after a service restart the boards.toml boards were gone until re-added; Scan
offered only what answered on this network; and Add by address ignored the matching
boards.toml entry's hub. Every behaviour below has its negative twin. The order and the
favourites are the user's settings (``general.board_order``, ``general.favourite_boards``),
in the service's own config dir here (the mock's temporary one, or the real daemon's
``tmp_path`` one), never ``~/.config``. No board, hub or ssh: the demo engine's boards only.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_HELD, BOARD_USB, DemoEngine
from tests.fakes.t14_mock_api import real_daemon
from tests.web.conftest import dump_failed_pages

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser

T = 10_000
ORDER = "general.board_order"
FAVS = "general.favourite_boards"
LAB_IP = "192.168.10.121"                 # nothing scripted answers here: the hub board
LAB = f"mps3@{LAB_IP}:6900"
LAB_TOML = f"""\
[boards.lab]
match = ["{LAB_IP}"]
name = "lab-mps3"
via = "hub"
hub = {{ use = "mapstone-dev", target = "mps3_01_pl" }}
"""


# --- helpers ---------------------------------------------------------------------------------------


def rail_ids(page) -> list[str]:
    return page.eval_on_selector_all(".rail-card", "els => els.map(e => e.dataset.board)")


def card(page, bid: str):
    return page.locator(f'.board-item[data-board="{bid}"]')


def wait_rail(page, n: int) -> list[str]:
    expect(page.locator(".rail-card")).to_have_count(n, timeout=T)
    return rail_ids(page)


def setting(daemon: Any, key: str) -> Any:
    req = urllib.request.Request(f"{daemon.url}/api/v1/settings?key={key}",
                                 headers={"Authorization": f"Bearer {daemon.token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:           # noqa: S310 - loopback
        return json.loads(resp.read())["rows"][0]["value"]


def wait_setting(page, daemon: Any, key: str, want: Any) -> None:
    got = None
    for _ in range(100):
        got = setting(daemon, key)
        if got == want:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"{key} is {got!r}, not {want!r}")


def drag(page, bid: str, onto: str, *, below: bool = False, check_line: bool = True) -> None:
    """Drag ``bid``'s card by the mouse to just above (or below) ``onto``'s."""
    src = card(page, bid).bounding_box()
    dst = card(page, onto).bounding_box()
    page.mouse.move(src["x"] + 40, src["y"] + src["height"] / 2)
    page.mouse.down()
    y = dst["y"] + (dst["height"] - 4 if below else 4)
    page.mouse.move(src["x"] + 40, y, steps=12)
    if check_line:
        where = "drop-after" if below else "drop-before"
        expect(page.locator(f'.rail-card.{where}[data-board="{onto}"]')).to_have_count(1, timeout=T)
        expect(page.locator(f'.rail-card.dragging[data-board="{bid}"]')).to_have_count(1)
    page.mouse.up()


# --- order: drag, and its twin -------------------------------------------------------------------


@pytest.mark.mock_too
def test_drag_reorders_the_rail_and_the_order_persists_across_a_reload(page_factory, daemon):
    page = page_factory()
    first, second, third = wait_rail(page, 3)
    expect(card(page, first)).to_have_attribute("aria-current", "true", timeout=T)
    drag(page, third, first)
    expect(page.locator(".rail-card")).to_have_count(3)
    assert rail_ids(page) == [third, first, second]
    # the drag opened nothing: the selection and the workspace are the first board's
    expect(card(page, third)).to_have_attribute("aria-current", "false")
    expect(card(page, first)).to_have_attribute("aria-current", "true")
    assert page.locator(".rail-card.drop-before, .rail-card.drop-after, .rail-card.dragging").count() == 0
    wait_setting(page, daemon, ORDER, [third, first, second])
    page.reload()
    assert wait_rail(page, 3) == [third, first, second]
    # and down again, below the last card
    drag(page, third, second, below=True)
    expect(page.locator(".rail-card").first).to_have_attribute("data-board", first)
    assert rail_ids(page) == [first, second, third]
    wait_setting(page, daemon, ORDER, [first, second, third])
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_a_click_without_a_drag_opens_the_board_and_keeps_the_order(page_factory, daemon):
    page = page_factory()
    ids = wait_rail(page, 3)
    target = ids[2]
    box = card(page, target).bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 42, box["y"] + box["height"] / 2 + 2)     # under the threshold
    page.mouse.up()
    expect(card(page, target)).to_have_attribute("aria-current", "true", timeout=T)
    expect(page.locator('[data-action="open"], [data-testid="board-header"]').first).to_be_visible(timeout=T)
    assert rail_ids(page) == ids
    page.wait_for_timeout(600)                    # longer than the save's debounce
    assert setting(daemon, ORDER) == []           # nothing moved, nothing written
    assert page.errors == []


# --- the keyboard -------------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_alt_arrows_move_the_focused_board_and_a_screen_reader_hears_where(page_factory, daemon):
    page = page_factory()
    first, second, third = wait_rail(page, 3)
    announce = page.locator('[data-testid="rail-announce"]')
    expect(announce).to_have_attribute("aria-live", "polite")
    card(page, third).focus()
    page.keyboard.press("Alt+ArrowUp")
    expect(page.locator(".rail-card").nth(1)).to_have_attribute("data-board", third, timeout=T)
    assert rail_ids(page) == [first, third, second]
    expect(announce).to_contain_text("moved to position 2 of 3", timeout=T)
    # the focus stays on the board that moved, so the next press moves it again
    page.wait_for_function(f"document.activeElement && document.activeElement.dataset.board === {json.dumps(third)}",
                           timeout=T)
    page.keyboard.press("Alt+ArrowUp")
    expect(page.locator(".rail-card").first).to_have_attribute("data-board", third, timeout=T)
    expect(announce).to_contain_text("moved to position 1 of 3", timeout=T)
    page.keyboard.press("Alt+ArrowUp")
    expect(announce).to_contain_text("already first", timeout=T)
    wait_setting(page, daemon, ORDER, [third, first, second])
    # twin: an arrow without Alt moves nothing (and a card's Enter still opens it)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(400)
    assert rail_ids(page) == [third, first, second]
    page.keyboard.press("Enter")
    expect(card(page, third)).to_have_attribute("aria-current", "true", timeout=T)
    assert rail_ids(page) == [third, first, second]
    assert page.errors == []


# --- favourites --------------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_a_star_pins_the_board_at_the_top_persists_and_unstarring_restores_the_order(page_factory, daemon):
    page = page_factory()
    ids = wait_rail(page, 3)
    last = ids[2]
    star = page.locator(f'.rail-card[data-board="{last}"] [data-testid="rail-star"]')
    expect(star).to_have_attribute("aria-pressed", "false")
    assert star.get_attribute("aria-label").startswith("Favourite ")
    star.click()
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_count(1, timeout=T)
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_attribute("data-board", last)
    expect(star).to_have_attribute("aria-pressed", "true")
    assert rail_ids(page) == [last, ids[0], ids[1]]
    expect(page.locator(".rail-group-title").first).to_contain_text("Favourites")
    expect(page.locator('[data-testid="rail-announce"]')).to_contain_text("added to Favourites", timeout=T)
    wait_setting(page, daemon, FAVS, [last])
    # the star opened nothing
    expect(card(page, last)).to_have_attribute("aria-current", "false")

    page.reload()
    wait_rail(page, 3)
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_attribute("data-board", last,
                                                                                       timeout=T)
    assert rail_ids(page) == [last, ids[0], ids[1]]
    # a new window (nothing selected in it yet) starts on the first board shown: the favourite
    fresh = page_factory()
    wait_rail(fresh, 3)
    expect(card(fresh, last)).to_have_attribute("aria-current", "true", timeout=T)
    assert rail_ids(fresh) == [last, ids[0], ids[1]]

    # twin: unstar it with the keyboard (Space on the star): it goes back to where it was
    star = page.locator(f'.rail-card[data-board="{last}"] [data-testid="rail-star"]')
    star.focus()
    page.keyboard.press("Space")
    expect(star).to_have_attribute("aria-pressed", "false", timeout=T)
    expect(page.locator('[data-testid="rail-group-fav"]')).to_have_count(0)
    assert rail_ids(page) == ids
    expect(page.locator('[data-testid="rail-announce"]')).to_contain_text("removed from Favourites", timeout=T)
    wait_setting(page, daemon, FAVS, [])
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_a_move_inside_the_favourites_keeps_the_other_boards_order(page_factory, daemon):
    page = page_factory()
    a, b, c = wait_rail(page, 3)
    for bid in (b, c):
        page.locator(f'.rail-card[data-board="{bid}"] [data-testid="rail-star"]').click()
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_count(2, timeout=T)
    assert rail_ids(page) == [b, c, a]
    card(page, c).focus()
    page.keyboard.press("Alt+ArrowUp")
    expect(page.locator('[data-testid="rail-announce"]')).to_contain_text("position 1 of 2 in Favourites", timeout=T)
    assert rail_ids(page) == [c, b, a]
    # a drag never crosses into the other group: past the last favourite it stays last there
    drag(page, c, a, below=True, check_line=False)
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card').last).to_have_attribute("data-board", c,
                                                                                            timeout=T)
    assert rail_ids(page) == [b, c, a]
    assert page.errors == []


# --- this browser keeps them only while the settings do not answer -----------------------------------------


def local_copy(page) -> Any:
    return page.evaluate("() => JSON.parse(localStorage.getItem('harness_manager.sidebar') || 'null')")


def test_while_the_settings_do_not_answer_this_browser_keeps_the_favourites_then_hands_them_over(
        page_factory, daemon):
    page = page_factory()
    ids = wait_rail(page, 3)
    page.route("**/api/v1/settings**", lambda route: route.abort())
    page.locator(f'.rail-card[data-board="{ids[2]}"] [data-testid="rail-star"]').click()
    page.wait_for_function("() => localStorage.getItem('harness_manager.sidebar') !== null", timeout=T)
    assert local_copy(page)["favs"] == [ids[2]] and local_copy(page)["pending"] is True
    assert setting(daemon, FAVS) == []                          # the settings never heard
    page.reload()                                              # still away: the browser's copy
    wait_rail(page, 3)
    expect(page.locator('[data-testid="rail-group-fav"] .rail-card')).to_have_attribute(
        "data-board", ids[2], timeout=T)
    page.unroute("**/api/v1/settings**")
    page.reload()                                              # back: the settings take it
    wait_setting(page, daemon, FAVS, [ids[2]])
    page.wait_for_function("() => localStorage.getItem('harness_manager.sidebar') === null", timeout=T)
    assert rail_ids(page)[0] == ids[2]
    assert [e for e in page.errors if "net::ERR_FAILED" not in e] == []


def test_negative_twin_while_the_settings_answer_this_browser_keeps_nothing(page_factory, daemon):
    page = page_factory()
    ids = wait_rail(page, 3)
    page.locator(f'.rail-card[data-board="{ids[1]}"] [data-testid="rail-star"]').click()
    wait_setting(page, daemon, FAVS, [ids[1]])
    assert local_copy(page) is None
    assert page.errors == []


# --- the boards.toml boards: listed after a restart, without contact -------------------------------------


class Served:
    """The real daemon over a fresh demo engine, as a restarted service is: it has probed and
    opened nothing. ``boards.toml`` is the test's (the classic demo engine reads the default
    one: ``$HARNESS_MANAGER_STATE_DIR``)."""

    def __init__(self, browser: Any, state: Path) -> None:
        self.browser = browser
        self.state = state
        self.engine: DemoEngine | None = None
        self.server: Any = None
        self.contexts: list[Any] = []
        self.pages: list[Any] = []

    def start(self) -> Any:
        self.engine = DemoEngine(speed=0.25)
        self.server = real_daemon(self.engine, token="sidebar", state_dir=self.state).start()
        return self.server

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
        if self.engine is not None:
            self.engine.close_all()
        self.server = self.engine = None

    def page(self, scheme: str = "light") -> Any:
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme,
                                       reduced_motion="reduce")
        self.contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(self.server.ui_url)
        self.pages.append(page)
        return page

    def contact(self) -> list[tuple[str, tuple]]:
        """Every engine call that would reach a board (a probe, an open, a read)."""
        assert self.engine is not None
        return [(n, a) for n, a in self.engine.calls
                if n in ("probe", "open", "info", "telemetry.readings")]


@pytest.fixture
def served(browser, tmp_path, request) -> Iterator[Served]:
    s = Served(browser, tmp_path / "service")
    try:
        yield s
    finally:
        dump_failed_pages(request, s.pages)
        for ctx in s.contexts:
            ctx.close()
        s.stop()


def boards_toml(text: str) -> Path:
    import os

    state = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    path = state / "boards.toml"
    path.write_text(text)
    return path


def test_boards_toml_boards_are_listed_after_a_restart_without_being_contacted(served):
    boards_toml(LAB_TOML + f'\n[boards."{BOARD_FIELDED}"]\nname = "desk-mps3"\n')
    served.start()
    page = served.page()
    ids = wait_rail(page, 2)
    assert sorted(ids) == sorted([LAB, BOARD_FIELDED])
    # before: scan and open a board that boards.toml does not name
    page.locator('[data-action="rescan"]').click()
    expect(page.locator(".rail-card")).to_have_count(4, timeout=T)
    card(page, BOARD_USB).click()
    page.locator('[data-action="open"]').click()
    expect(page.locator(f'main[data-board="{BOARD_USB}"]')).to_be_visible(timeout=T)

    served.stop()                                  # the service restarts
    served.start()
    page = served.page()
    ids = wait_rail(page, 2)
    assert sorted(ids) == sorted([LAB, BOARD_FIELDED])
    # twin: the boards it had found or opened but boards.toml does not name are not listed
    assert BOARD_USB not in rail_ids(page) and BOARD_HELD not in rail_ids(page)
    lab = page.locator(f'.rail-card[data-board="{LAB}"]')
    expect(lab.locator('[data-testid="rail-name"]')).to_have_text("lab-mps3")
    expect(lab.locator('[data-testid="rail-not-open"]')).to_have_text("not open")
    expect(lab.locator('[data-testid="rail-route"]')).to_have_text("through the hub mapstone-dev")
    expect(lab.locator(".dot")).to_have_class("dot unk")
    assert "not contacted" in lab.locator(".dot").get_attribute("title")
    page.wait_for_timeout(1500)                    # the page's own reads have had their go
    assert served.contact() == [], served.contact()
    # selecting it contacts nothing either: the preview says how Open will reach it
    card(page, LAB).click()
    expect(page.locator('[data-testid="preview-route"]')).to_contain_text("through the hub mapstone-dev")
    expect(page.locator('[data-testid="preview-route"]')).to_contain_text("lab")
    expect(page.locator(".preview .card-sub")).to_contain_text("not contacted until you open it")
    page.wait_for_timeout(500)
    assert served.contact() == []
    # Open is the action: the board is asked only now
    card(page, BOARD_FIELDED).click()
    page.locator('[data-action="open"]').click()
    expect(page.locator(f'main[data-board="{BOARD_FIELDED}"]')).to_be_visible(timeout=T)
    assert [a[0] for n, a in served.contact() if n == "open"] == [BOARD_FIELDED]
    expect(card(page, BOARD_FIELDED).locator('[data-testid="rail-open"]')).to_be_visible(timeout=T)
    assert page.errors == []


def test_negative_twin_without_boards_toml_a_restarted_service_lists_nothing_until_a_scan(served):
    served.start()
    page = served.page()
    # nothing configured: the page's first scan (as before) is what lists boards
    expect(page.locator(".rail-card")).to_have_count(3, timeout=T)
    assert page.locator('[data-testid="rail-not-open"]').count() == 0
    assert [n for n, _a in served.contact()] == ["probe"]
    assert page.errors == []


# --- Scan offers the boards.toml boards --------------------------------------------------------------------


def test_scan_offers_the_boards_toml_boards_when_nothing_answers(served):
    boards_toml(LAB_TOML)
    served.start()
    assert served.engine is not None
    for board in served.engine._boards.values():   # nothing answers on this network
        board.reachable = False
    page = served.page()
    wait_rail(page, 1)
    page.locator('[data-action="rescan"]').click()
    line = page.locator('[data-testid="scan-line"]')
    expect(line).to_contain_text("no boards answered on this network", timeout=T)
    expect(line).to_contain_text("1 more in boards.toml (not contacted)")
    offer = page.locator('[data-testid="scan-offer"]')
    expect(offer.locator(f'[data-offer="{LAB}"]')).to_contain_text("lab-mps3")
    expect(offer.locator(f'[data-offer="{LAB}"]')).to_contain_text("through the hub mapstone-dev")
    assert "err" not in (line.get_attribute("class") or "")
    offer.locator(f'[data-offer="{LAB}"]').click()
    expect(card(page, LAB)).to_have_attribute("aria-current", "true", timeout=T)
    expect(page.locator('[data-testid="preview-route"]')).to_contain_text("mapstone-dev")
    assert [n for n, _a in served.contact()] == ["probe"]          # the scan, nothing else
    assert page.errors == []


def test_negative_twin_without_boards_toml_scan_offers_nothing(served):
    served.start()
    assert served.engine is not None
    for board in served.engine._boards.values():
        board.reachable = False
    page = served.page()
    line = page.locator('[data-testid="scan-line"]')
    expect(line).to_contain_text("no boards answered. Check the Ethernet link", timeout=T)
    assert page.locator('[data-testid="scan-offer"]').count() == 0
    assert page.errors == []


# --- Add by address: the matching entry's route ---------------------------------------------------------------


def probe_body(page, address: str) -> dict:
    page.locator('[aria-label="Board address"]').fill(address)
    with page.expect_request(lambda r: r.url.endswith("/api/v1/probe") and r.method == "POST",
                             timeout=T) as req:
        page.locator(".rail-add button[type=submit]").click()
    return json.loads(req.value.post_data or "{}")


def test_add_by_an_address_boards_toml_names_goes_through_its_hub(served):
    boards_toml(LAB_TOML)
    served.start()
    page = served.page()
    wait_rail(page, 1)
    page.locator('[aria-label="Add a board by address"]').click()
    page.locator('[aria-label="Board address"]').fill(LAB_IP)
    via = page.locator('[data-testid="add-via"]')
    expect(via).to_have_value("hub", timeout=T)
    note = page.locator('[data-testid="add-route"]')
    expect(note).to_contain_text("boards.toml lab: through the hub mapstone-dev (mps3_01_pl)")
    body = probe_body(page, LAB_IP)
    assert body == {"hosts": [LAB_IP], "scan_usb": False, "via": "hub"}
    # with a port, or as its board id, it is the same entry
    page.locator('[aria-label="Add a board by address"]').click()
    page.locator('[aria-label="Board address"]').fill(f"{LAB_IP}:6900")
    expect(via).to_have_value("hub")
    # a route typed by hand wins, and the note says so
    via.fill("ssh:other-hub")
    expect(note).to_contain_text("the route typed here is used instead")
    assert probe_body(page, f"{LAB_IP}:6900")["via"] == "ssh:other-hub"
    assert page.errors == []


def test_negative_twin_an_address_boards_toml_does_not_name_has_no_route(served):
    boards_toml(LAB_TOML)
    served.start()
    page = served.page()
    wait_rail(page, 1)
    page.locator('[aria-label="Add a board by address"]').click()
    page.locator('[aria-label="Board address"]').fill("192.168.10.122")
    expect(page.locator('[data-testid="add-via"]')).to_have_value("")
    assert page.locator('[data-testid="add-route"]').count() == 0
    # the pack's rule: a match without a port names every port of its host, so :6901 is
    # the entry's too (the preview's route is what the service would use)
    page.locator('[aria-label="Board address"]').fill(f"{LAB_IP}:6901")
    expect(page.locator('[data-testid="add-via"]')).to_have_value("hub")
    page.locator('[aria-label="Board address"]').fill("192.168.10.12")       # a prefix is not it
    expect(page.locator('[data-testid="add-via"]')).to_have_value("")
    body = probe_body(page, "192.168.10.122")
    assert body == {"hosts": ["192.168.10.122"], "scan_usb": False}
    assert page.errors == []
