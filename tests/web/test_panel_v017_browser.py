"""PANEL-V017 in the browser: the Front panel card's text mirror draws a frame's role codes by
design/tokens.json's order (net-protocol v0.17: ``a`` text ... ``i`` ok ... ``q``
banner-err ... ``u`` banner-held) and the status glyphs 0x80-0x86 as the panel draws them
(their 8x16 bitmaps), from ``js/panel_codes.js`` (GENERATED with ``core/panel_codes.py``).

The panel is ``PanelSim`` (tests/fakes/p1_mock_panel.py) over both servers. Each check has
its negative twin: before PANEL-V017 the mirror inverted every ``i`` cell (``i`` is ok on
the wire) and drew a glyph byte as an invisible control character.
"""

from __future__ import annotations

import pytest

from harness_manager.core import panel_codes
from harness_manager.core.panel import COLS, ROLE_TEXT, role_code
from tests.fakes.clcd_panel_shell import LINUX_STATUS_ROWS
from tests.web.test_p3_panel_ui import APP, BOARD, T, details, linux, open_board, text_mirror_only

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser]
__all__ = ["text_mirror_only"]      # the autouse fixture: the text mirror, not the Live display


@pytest.fixture
def psim(daemon, engine):
    from tests.fakes.p1_mock_panel import PanelSim

    sim = getattr(daemon.app.state, "panel", None)
    if sim is None:
        sim = PanelSim(engine)
        sim.attach(engine)
    return sim


def _rgb(hexs: str) -> str:
    h = hexs.lstrip("#")
    return f"rgb({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)})"


#: The board's aligned status row 2 (its own render, clcd_preview --aligned), in wire form.
ROW2 = "design nanosoc v1.0           \x80verified "
ROLES2 = "bbbbbbacccccccabbbbaaaaaaaaaaaiiiiiiiiia"


def _frame(banner_row: int = 11, banner_role: str = "banner-err"):
    rows = list(LINUX_STATUS_ROWS)
    roles = [ROLE_TEXT * COLS for _ in rows]
    rows[2], roles[2] = ROW2, ROLES2
    rows[banner_row] = "NETWORK LINK DOWN".center(COLS)
    roles[banner_row] = role_code(banner_role) * COLS
    return tuple(rows), "".join(roles)


def _show(page, psim, engine, theme: str, **kw):
    linux(engine)
    rows, roles = _frame(**kw)
    psim.set_rows(BOARD, rows, roles=roles, theme=theme)
    open_board(page)
    details(page)
    mirror = page.locator('[data-testid="panel-mirror"]')
    expect(mirror).to_have_attribute("data-theme", theme, timeout=T)
    return mirror


@pytest.mark.mock_too
def test_the_today_theme_inverts_the_banner_roles_only(page_factory, daemon, engine, psim):
    page = page_factory(**APP)
    mirror = _show(page, psim, engine, "today")
    ok = mirror.locator('[data-row="2"] [data-role="ok"]')
    expect(ok.first).to_be_visible(timeout=T)
    # "i" is ok on the wire: plain text in today's theme, never today's red
    assert ok.count() >= 1 and "pm-inv" not in (ok.last.get_attribute("class") or "")
    banner = mirror.locator('[data-row="11"] [data-role="banner-err"]')
    expect(banner).to_have_class("pm-inv")
    assert mirror.locator('[data-row="2"] [data-role="label"]').first.inner_text() == "design"
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_every_banner_role_is_todays_red_not_just_one_letter(
        page_factory, daemon, engine, psim):
    page = page_factory(**APP)
    mirror = _show(page, psim, engine, "today", banner_role="banner-held")    # "u"
    expect(mirror.locator('[data-row="11"] [data-role="banner-held"]')).to_have_class("pm-inv")
    assert mirror.locator('[data-row="11"] [data-role="banner-err"]').count() == 0
    assert mirror.locator(".pm-inv").count() == 1
    assert page.errors == []


@pytest.mark.mock_too
def test_the_aligned_theme_draws_the_glass_colours_and_the_glyph_bitmap(
        page_factory, daemon, engine, psim):
    page = page_factory(**APP)
    mirror = _show(page, psim, engine, "aligned")
    ok_fg, _ok_bg = panel_codes.ROLE_COLOURS["ok"]
    word = mirror.locator('[data-row="2"] span[data-role="ok"]:not(.pm-glyph)')
    expect(word).to_have_text("verified ", timeout=T)
    assert word.evaluate("el => getComputedStyle(el).color") == _rgb(ok_fg)
    banner_fg, banner_bg = panel_codes.ROLE_COLOURS["banner-err"]
    banner = mirror.locator('[data-row="11"] [data-role="banner-err"]')
    assert banner.evaluate("el => getComputedStyle(el).backgroundColor") == _rgb(banner_bg)
    assert mirror.evaluate("el => getComputedStyle(el).backgroundColor") == \
        _rgb(panel_codes.ROLE_COLOURS["text"][1])
    # the glyph: one cell, drawn from its bitmap (as many pixels as the table sets)
    glyph = mirror.locator('[data-row="2"] [data-glyph="ok"]')
    expect(glyph).to_have_count(1)
    assert glyph.get_attribute("data-role") == "ok"
    d = glyph.locator("path").get_attribute("d")
    lit = sum(bin(b).count("1") for b in panel_codes.GLYPHS[0x80][1])
    assert d.count("M") == lit > 0
    assert page.errors == []


@pytest.mark.mock_too
def test_negative_twin_the_today_theme_uses_the_uis_own_colours_not_the_glass(
        page_factory, daemon, engine, psim):
    page = page_factory(**APP)
    mirror = _show(page, psim, engine, "today")
    word = mirror.locator('[data-row="2"] span[data-role="ok"]:not(.pm-glyph)')
    expect(word).to_have_text("verified ", timeout=T)
    assert word.get_attribute("style") in (None, "")
    ok_fg, _ = panel_codes.ROLE_COLOURS["ok"]
    assert word.evaluate("el => getComputedStyle(el).color") != _rgb(ok_fg)
    # the glyph is still drawn from its bitmap (it is a cell of the grid in either theme)
    expect(mirror.locator('[data-row="2"] [data-glyph="ok"] path')).to_have_count(1)
    assert page.errors == []
