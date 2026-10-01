"""UI2-POLISH item 3 (david): the Front panel is the glass, black in light mode and dark mode.

"The front panel drawing inverts the colour when in light mode - it should be an accurate
representation of what is showing on the display where it has a black background in light and
dark mode." The causes, both fixed:

- the Live picture (``js/display.js``): its backing store started (and restarted, after a refusal)
  TRANSPARENT, and the frame behind the canvas was the theme's ``--term-bg`` (near white in light
  mode), so an undrawn tile, or a greyed or dimmed picture (``opacity``), showed the page's white;
  now the bare glass is opaque black and the frame is the panel palette's black;
- the Text view (``sections/panel.js``): the mirror's rows were the theme's ``--term-fg`` on
  ``--term-bg`` (dark text on white in light mode); now they are the panel palette's colours
  (``js/panel_codes.js`` ROLE_COLOURS) on black, in both themes.

The test renders the demo's panel in light and dark, takes a screenshot of the picture and reads
the pixels at the same points: identical, and the background #000000. The twin: the card around
the picture does follow the theme. The REAL daemon over ``DemoEngine(showcase=True)``.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
from tests.web import nav
from tests.web.test_demo_all_browser import showcase  # noqa: F401 - the fixture
from tests.web.test_lm4_display_browser import is_live, show_display

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}
BLACK = [0, 0, 0]
TEXT_FG = "rgb(231, 231, 239)"            # ROLE_COLOURS.text[0], #e7e7ef
# Points on the picture (fractions of its width and height): the last column of rows 3, 5, 7, 9
# and 12 of the status page, blank cells where nothing is drawn but the glass (the title and the
# chrome rows have the palette's #182029 behind them), plus a few points on the text.
GLASS_POINTS = [(0.997, 0.22), (0.997, 0.35), (0.997, 0.5), (0.997, 0.65), (0.997, 0.82)]
TEXT_POINTS = [(0.05, 0.05), (0.2, 0.2), (0.4, 0.33), (0.6, 0.5), (0.3, 0.75), (0.7, 0.9)]

SAMPLE = """async ([b64, pts]) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  const x = c.getContext('2d');
  x.drawImage(img, 0, 0);
  return pts.map(([fx, fy]) => Array.from(x.getImageData(
    Math.min(c.width - 1, Math.floor(fx * c.width)), Math.min(c.height - 1, Math.floor(fy * c.height)), 1, 1).data).slice(0, 3));
}"""


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def pixels(page: Any, locator: Any, points: list[tuple[float, float]]) -> list[list[int]]:
    """What the screen shows at ``points`` of ``locator``: a screenshot, read back in the page."""
    png = locator.screenshot(animations="disabled")
    return page.evaluate(SAMPLE, [base64.b64encode(png).decode(), points])


def frozen_panel(show: Any) -> None:
    show.engine.session(BOARD_LINUX).display.panel.frozen = True     # the uptime stops ticking


def live_page(show: Any, scheme: str) -> Any:
    page = show.page(scheme, **APP)
    nav.open_board(page, BOARD_LINUX)
    nav.tab(page, "overview")
    show_display(page)
    is_live(page)
    return page


def css(page: Any, testid: str, prop: str) -> str:
    return page.evaluate("([t, p]) => getComputedStyle(document.querySelector(`[data-testid=\"${t}\"]`))[p]",
                         [testid, prop])


def test_the_live_picture_is_the_same_black_glass_in_light_and_dark(showcase):  # noqa: F811
    nav_open = live_page(showcase, "light")                 # the panel exists once a viewer opened it
    frozen_panel(showcase)
    seen: dict[str, Any] = {}
    for scheme in ("light", "dark"):
        page = nav_open if scheme == "light" else live_page(showcase, scheme)
        seq = by_id(page, "live-display").get_attribute("data-seq")
        page.wait_for_timeout(600)                            # one more frame at most: frozen
        expect(by_id(page, "live-display")).to_have_attribute("data-seq", seq or "", timeout=T)
        frame = by_id(page, "live-frame")
        seen[scheme] = (pixels(page, frame, GLASS_POINTS), pixels(page, frame, TEXT_POINTS))
        # the frame behind the canvas is the glass's black, not the theme's --term-bg
        assert css(page, "live-frame", "backgroundColor") == "rgb(0, 0, 0)", scheme
        # a picture no tile has reached yet is the bare glass: opaque black, never see-through
        bare = page.evaluate("""() => import('./js/display.js').then((m) => {
          const c = new m.DisplayClient('no-such-board');
          const px = new Uint8Array(c.px.buffer, 0, 4);
          return Array.from(px);
        })""")
        assert bare == [0, 0, 0, 255], (scheme, bare)
        assert page.errors == [], page.errors
    glass_light, text_light = seen["light"]
    glass_dark, text_dark = seen["dark"]
    assert glass_light == [BLACK] * len(GLASS_POINTS), glass_light
    assert glass_dark == glass_light
    assert text_dark == text_light                            # the same pixels, whatever the theme


def test_twin_the_card_around_the_live_picture_follows_the_theme(showcase):  # noqa: F811
    light = live_page(showcase, "light")
    dark = live_page(showcase, "dark")
    card_l = css(light, "panel-card", "backgroundColor")
    card_d = css(dark, "panel-card", "backgroundColor")
    assert card_l != card_d, (card_l, card_d)                 # the page's theme: it does change
    edge_l = pixels(light, by_id(light, "panel-card"), [(0.5, 0.995)])
    edge_d = pixels(dark, by_id(dark, "panel-card"), [(0.5, 0.995)])
    assert edge_l != edge_d
    assert css(light, "live-frame", "backgroundColor") == css(dark, "live-frame", "backgroundColor")


@pytest.mark.parametrize("bid", [BOARD_LINUX, BOARD_V011], ids=["read-from-the-panel", "rebuilt"])
def test_the_text_view_is_the_panel_colours_on_black_in_both_themes(showcase, bid):  # noqa: F811
    got: dict[str, Any] = {}
    for scheme in ("light", "dark"):
        page = showcase.page(scheme, **APP)
        nav.open_board(page, bid)
        if bid == BOARD_LINUX:
            frozen_panel(showcase)
        nav.tab(page, "overview")
        view_text = page.locator('[data-testid="panel-card"] [data-action="panel-view-text"]')
        if view_text.is_enabled() and view_text.get_attribute("aria-pressed") != "true":
            view_text.click()
        mirror = by_id(page, "panel-mirror")
        expect(mirror).to_be_visible(timeout=T)
        expect(mirror.locator(".pm-row").first).to_be_visible(timeout=T)
        assert css(page, "panel-mirror", "backgroundColor") == "rgb(0, 0, 0)", scheme
        assert css(page, "panel-mirror", "color") == TEXT_FG, scheme
        plain = mirror.locator('[data-role="text"], [data-role="value"]').first
        if plain.count():
            assert plain.evaluate("(el) => getComputedStyle(el).color") == TEXT_FG
        got[scheme] = pixels(page, mirror, [(0.5, 0.5), (0.98, 0.5), (0.5, 0.02)])
        assert page.errors == [], page.errors
    assert got["light"] == got["dark"], got
    assert BLACK in got["light"]


def test_twin_the_text_views_card_follows_the_theme(showcase):  # noqa: F811
    cards = []
    for scheme in ("light", "dark"):
        page = showcase.page(scheme, **APP)
        nav.open_board(page, BOARD_V011)                      # bare metal: the Text view, rebuilt
        nav.tab(page, "overview")
        expect(by_id(page, "panel-mirror")).to_be_visible(timeout=T)
        cards.append(css(page, "panel-card", "backgroundColor"))
    assert cards[0] != cards[1], cards
