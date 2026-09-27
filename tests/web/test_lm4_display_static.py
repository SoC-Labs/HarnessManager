"""Lane LM4, statically: the Live display's wiring, colours and D4 (view only).

``js/display.js`` calls the display routes by their ``api.js`` ENDPOINTS names (the LM4 block),
takes every colour from the tokens, and sends the daemon nothing but ``{"ack": seq}`` and
``{"rate": hz}``: no click, touch or key on the picture reaches the board (D4). Each check
has a twin that shows it can fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness_manager.daemon import app as daemon_app
from tests.fakes.t14_api_contract import api_md_sections, daemon_routes, ui_endpoints

STATIC = Path(daemon_app.__file__).resolve().parents[1] / "web" / "static"
DISPLAY_JS = STATIC / "js" / "display.js"
PANEL_JS = STATIC / "js" / "sections" / "panel.js"
APP_CSS = STATIC / "css" / "app.css"
ROUTES = {"displaySocket": ("WS", "/boards/{}/display/ws"), "displayPng": ("GET", "/boards/{}/display.png")}


def test_the_lm4_block_names_the_display_routes_the_daemon_serves_and_api_md_lists():
    api = (STATIC / "js" / "api.js").read_text()
    block = api.split("// --- LM4 DISPLAY-UI:", 1)[1].split("// --- end LM4 DISPLAY-UI ---", 1)[0]
    assert set(re.findall(r"^\s+(\w+): \[", block, re.M)) == set(ROUTES)
    table = ui_endpoints()
    assert {n: table[n] for n in ROUTES} == ROUTES
    routes = set(ROUTES.values())
    assert routes <= api_md_sections()["display_api"]
    assert {r for r in routes if r[0] != "WS"} <= daemon_routes()
    js = DISPLAY_JS.read_text()
    assert set(re.findall(r'(?:socketUrl|callBytes)\(\s*"(\w+)"', js)) == set(ROUTES)
    assert '"display.*"' in api                            # display.state: a refused view asks again


def test_the_route_check_catches_a_name_the_table_lacks():
    assert "displayTouch" not in ui_endpoints()


COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\(")


def colours(text: str) -> list[str]:
    return COLOUR.findall(text)


def test_the_display_takes_every_colour_from_the_tokens():
    assert colours(DISPLAY_JS.read_text()) == []
    css = APP_CSS.read_text()
    block = css.split("/* LM4: the Live display", 1)[1].split("\n.lease-tapped", 1)[0]
    assert colours(block) == []
    assert set(re.findall(r"var\((--[\w-]+)\)", block)) <= {
        "--text", "--text-3", "--border-strong", "--warn", "--term-bg", "--shadow", "--shadow-pop",
        "--scrim", "--on-badge", "--held", "--held-soft", "--held-border", "--radius-sm"}
    assert colours("color: #ffffff; background: rgb(0, 0, 0)") == ["#ffffff", "rgb("]   # the twin


SEND = re.compile(r"this\.send\(\{\s*(\w+)\s*:")


def test_d4_the_page_sends_the_board_nothing_but_ack_and_rate():
    js = DISPLAY_JS.read_text()
    assert set(SEND.findall(js)) == {"ack", "rate"}
    assert "ws.send(" in js and js.count("ws.send(") == 1   # one place sends, and it is send()
    # the picture (canvas and frame) carries no pointer, mouse, touch or key handler
    live = js.split('<div class="ld-scroll">', 1)[1].split('<div class="ld-foot">', 1)[0]
    assert not re.search(r"\bon[A-Z]\w*=", live), re.findall(r"\bon[A-Z]\w*=", live)
    assert "View only" in js
    # the twin: the checks see a tap and a handler
    assert SEND.findall("this.send({ tap: 1 })") == ["tap"]
    assert re.search(r"\bon[A-Z]\w*=", '<canvas onClick=${f}></canvas>')


def test_the_canvas_css_is_pixelated_view_only_and_overlays_never_take_a_click():
    css = APP_CSS.read_text()
    rule = re.search(r"\.ld-canvas \{([^}]*)\}", css).group(1)
    assert "image-rendering: pixelated" in rule and "cursor: default" in rule
    for sel in (".ld-hatches", ".ld-badges"):
        assert "pointer-events: none" in re.search(re.escape(sel) + r" \{([^}]*)\}", css).group(1)
    assert "pointer-events: none" in re.search(r"\.ld-note, \.ld-scrim \{([^}]*)\}", css).group(1)
    assert "cursor: pointer" not in css.split("/* LM4: the Live display", 1)[1].split("\n.lease-tapped", 1)[0]


def test_the_front_panel_card_hosts_the_live_display_over_the_text_mirror():
    panel = PANEL_JS.read_text()
    assert 'import { LiveDisplay } from "../display.js";' in panel
    assert "<${LiveDisplay} bid=${bid}><${Mirror} f=${f} /><//>" in panel
    assert panel.count("<${Mirror} f=${f} />") == 1        # the text mirror only as its fallback
    js = DISPLAY_JS.read_text()
    assert "export function LiveDisplay({ bid, children = null })" in js
    assert "${children}" in js
