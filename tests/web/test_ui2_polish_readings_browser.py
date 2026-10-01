"""UI2-POLISH items 5 and 6 (david's first real-board look): the Overview's readings row.

5. On a Linux board the DUT clock tile said "unavailable · the shell cannot read the DUT clock" in
   the warning tint. The Linux harness's DUT clock is FIXED by the shell: the MPS3 pack now reports
   the pin model's dut_clk rate with the reason "fixed by the shell" (harness_manager_mps3/clock.py),
   and the tile shows "50 MHz · fixed by the shell", plain. With no rate known the tile is left out
   and the row reflows.
6. No "?" in the readings: a counter the harness does not send is "not reported" with a short reason
   in neutral grey, and a value that DOES exist is the number (Partition swaps: the ICAP bytes when
   there is no swap count; Network: tx when there is no rx).

The REAL daemon over ``DemoEngine(showcase=True)``; the board's answers a real board gave (no
dut_clk in its telemetry, an old service's counters) are routed in the page (``page.route``) from
the daemon's own answer. Each behaviour has its twin.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import pytest

from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
from tests.web import nav
from tests.web.test_demo_all_browser import showcase  # noqa: F401 - the fixture

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}
FIXED = {"name": "dut", "value": 50.0, "unit": "MHz", "source": "the pin model, shell 0x44ee76d5",
         "reason": "fixed by the shell", "available": True}
NO_RATE = {"name": "dut", "value": None, "unit": "MHz", "source": "the pin model", "available": False,
           "reason": "fixed by the shell; the pin model has no rate for shell 0x4c1a0003"}


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def api_path(bid: str, tail: str = "") -> str:
    return f"/api/v1/boards/{quote(bid, safe='')}{tail}"


def route_json(page: Any, bid: str, tail: str, edit) -> None:
    """The daemon's own answer for ``GET /boards/{bid}{tail}``, edited by ``edit(data)``."""
    path = api_path(bid, tail)

    def handle(route: Any) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        resp = route.fetch()
        body = resp.json()
        if body.get("ok"):
            edit(body)
        route.fulfill(status=resp.status, content_type="application/json", body=json.dumps(body))

    page.route(lambda url: url.split("?", 1)[0].endswith(path), handle)


def route_answer(page: Any, bid: str, tail: str, readings: list[dict]) -> None:
    """``GET /boards/{bid}{tail}`` answers these readings (the demo has no clocks adapter: what
    the MPS3 pack answers on a real board)."""
    path = api_path(bid, tail)
    body = json.dumps({"ok": True, "board_id": bid, "readings": readings})
    page.route(lambda url: url.split("?", 1)[0].endswith(path),
               lambda route: route.fulfill(status=200, content_type="application/json", body=body)
               if route.request.method == "GET" else route.continue_())


def no_dut_clk(body: dict) -> None:
    body["readings"] = [r for r in body.get("readings", []) if r.get("name") != "dut_clk"]


def overview(show: Any, bid: str, scheme: str = "light", setup=None) -> Any:
    ctx = show.browser.new_context(viewport=APP, color_scheme=scheme, reduced_motion="reduce")
    show.contexts.append(ctx)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    if setup:
        setup(page)
    page.goto(show.daemon.ui_url)
    page.wait_for_selector(".board-item", timeout=T)
    show.pages.append(page)
    nav.open_board(page, bid)
    nav.tab(page, "overview")
    by_id(page, "ov-readings").wait_for(timeout=T)
    return page


def css(locator: Any, prop: str) -> str:
    return locator.evaluate(f"(el) => getComputedStyle(el).{prop}")


def token(page: Any, name: str) -> str:
    return page.evaluate(f"""() => {{ const d = document.createElement('i'); d.style.color = 'var({name})';
      document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; }}""")


# --- 5. the DUT clock on a Linux board ----------------------------------------------------------------


def test_a_linux_boards_dut_clock_is_the_shells_fixed_rate_plain_not_a_warning(showcase):  # noqa: F811
    def setup(page):
        route_json(page, BOARD_LINUX, "/telemetry", no_dut_clk)          # a real board: no dut_clk
        route_answer(page, BOARD_LINUX, "/clocks", [FIXED])               # the pack's answer
    page = overview(showcase, BOARD_LINUX, setup=setup)
    tile = by_id(page, "ov-kpi-clock")
    expect(tile).to_contain_text("50", timeout=T)
    expect(tile).to_contain_text("MHz")
    expect(tile).to_contain_text("fixed by the shell")
    expect(tile).to_have_attribute("data-level", "plain")
    assert css(tile, "backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")       # no tint
    for word in ("unavailable", "cannot read", "?"):
        assert word not in tile.inner_text(), tile.inner_text()
    expect(by_id(page, "ov-readings")).to_have_attribute("data-n", "6")
    assert page.errors == []


def test_twin_a_linux_board_with_no_rate_known_has_no_clock_tile_and_the_row_reflows(showcase):  # noqa: F811
    def setup(page):
        route_json(page, BOARD_LINUX, "/telemetry", no_dut_clk)
        route_answer(page, BOARD_LINUX, "/clocks", [NO_RATE])
    page = overview(showcase, BOARD_LINUX, setup=setup)
    row = by_id(page, "ov-readings")
    expect(row).to_have_attribute("data-n", "5", timeout=T)
    page.wait_for_timeout(800)                                           # the clocks read has landed
    expect(by_id(page, "ov-kpi-clock")).to_have_count(0)
    cols = css(row, "gridTemplateColumns").split()
    assert len(cols) == 5, cols                                          # five tiles fill the row
    assert "DUT clock" not in row.inner_text()
    assert page.errors == []


def test_twin_a_bare_metal_clock_it_cannot_read_back_is_not_reported_in_grey(showcase):  # noqa: F811
    unread = {"name": "dut", "value": None, "unit": "MHz", "source": "shell set_clk", "available": False,
              "reason": "the shell cannot read the DUT clock back; set it to know it"}

    def setup(page):
        route_json(page, BOARD_V011, "/telemetry", no_dut_clk)
        route_answer(page, BOARD_V011, "/clocks", [unread])
    page = overview(showcase, BOARD_V011, setup=setup)
    tile = by_id(page, "ov-kpi-clock")
    expect(tile).to_contain_text("not reported", timeout=T)
    expect(tile).to_contain_text("set it to know it")
    expect(tile).to_have_attribute("data-level", "unk")
    assert css(tile.locator(".ov-unav"), "color") == token(page, "--text-3")    # neutral grey
    assert css(tile, "backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")
    assert page.errors == []


# --- 6. no "?" in the readings -------------------------------------------------------------------------


def old_counters(body: dict) -> None:
    """An old service's answer: tx but no rx, ICAP bytes but no swap count, no svc_* tallies."""
    info = body.get("info", body)
    health = info.get("health") or {}
    health["counters"] = {"tx_frames": 1234, "icap": 2_516_582, "svc_max_us": 120_000}
    info["health"] = health
    info["stats"] = {}


def test_no_question_mark_in_the_readings_and_what_exists_is_the_number(showcase):  # noqa: F811
    page = overview(showcase, BOARD_LINUX, setup=lambda p: route_json(p, BOARD_LINUX, "", old_counters))
    row = by_id(page, "ov-readings")
    swaps = by_id(page, "ov-kpi-swaps")
    expect(swaps).to_contain_text("2.4 MiB", timeout=T)
    expect(swaps).to_contain_text("ICAP written")
    expect(swaps).to_contain_text("swap count: not reported")
    net = by_id(page, "ov-kpi-net")
    expect(net).to_contain_text("1,234")
    expect(net).to_contain_text("tx")
    expect(net).to_contain_text("rx: not reported")
    assert "this harness doesn't report rx counts" in (net.get_attribute("title") or "")
    loop = by_id(page, "ov-kpi-loop")
    expect(loop).to_contain_text("0.12")
    expect(loop).to_contain_text("overruns, skips: not reported")
    assert "?" not in row.inner_text(), row.inner_text()
    tips = row.evaluate("(el) => [...el.querySelectorAll('[title]')].map((e) => e.title).join(' | ')")
    assert "?" not in tips, tips
    assert page.errors == []


def test_twin_a_board_reporting_every_counter_shows_its_numbers(showcase):  # noqa: F811
    def full(body: dict) -> None:
        info = body.get("info", body)
        health = info.get("health") or {}
        health["counters"] = {"rx_frames": 18342, "tx_frames": 18011, "swaps": 7, "icap": 2_516_582,
                              "svc_max_us": 120_000, "svc_overruns": 0, "svc_skips": 2}
        info["health"] = health
        info["stats"] = {}
    page = overview(showcase, BOARD_LINUX, setup=lambda p: route_json(p, BOARD_LINUX, "", full))
    swaps = by_id(page, "ov-kpi-swaps")
    expect(swaps.locator(".ov-kpi-v")).to_have_text("7", timeout=T)
    expect(swaps).to_contain_text("ICAP 2.4 MiB written")
    net = by_id(page, "ov-kpi-net")
    expect(net.locator(".ov-kpi-v")).to_contain_text("18,342")
    expect(net).to_contain_text("rx 18,342 · tx 18,011")
    expect(by_id(page, "ov-kpi-loop")).to_contain_text("0 overruns · 2 skips")
    assert "?" not in by_id(page, "ov-readings").inner_text()
