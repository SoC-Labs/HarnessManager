"""KIT-UI: the Build section, statically: its wiring, its imports, and the routes it calls.

``sections/build.js`` calls the kit routes by their ``api.js`` ENDPOINTS names (the KIT-UI
block). This holds the section to the table: every name it calls is an ENDPOINTS entry the
real daemon serves and docs/API.md lists under ``kit_api``, and every name it imports is
one its module exports. Each check has a twin that shows it can fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness_manager.daemon import app as daemon_app
from tests.fakes.t14_api_contract import api_md_sections, daemon_routes, ui_endpoints
from tests.web.test_t10_xdc_static import exports

JS = Path(daemon_app.__file__).resolve().parents[1] / "web" / "static" / "js"
BUILD_JS = JS / "sections" / "build.js"
KIT_ROUTES = {
    "boardKit": ("GET", "/boards/{}/kit"), "boardGuide": ("GET", "/boards/{}/guide"),
    "kitGet": ("GET", "/kits/{}"), "kitFetch": ("POST", "/kits/fetch"),
    "kitZip": ("GET", "/kits/{}/zip"), "guideScript": ("POST", "/guide/script"),
    "kitCheck": ("POST", "/kits/check"), "kitPack": ("POST", "/kits/pack"),
}


def called_names(js: str) -> set[str]:
    return set(re.findall(r'\b(?:call|callBlob)\(\s*"(\w+)"', js))


def test_the_nav_puts_build_between_xdc_and_program():
    app = (JS / "app.js").read_text()
    assert 'import { BuildSection } from "./sections/build.js";' in app
    keys = re.findall(r'\{ key: "(\w+)"', app.split("export const SECTIONS", 1)[1].split("];", 1)[0])
    i = keys.index("build")
    assert keys[i - 1:i + 2] == ["xdc", "build", "program"], keys
    assert "export function BuildSection" in BUILD_JS.read_text()


def test_every_name_build_js_imports_is_exported_by_its_module():
    text = BUILD_JS.read_text()
    imports = re.findall(r"import\s*\{([^}]*)\}\s*from\s*\"([^\"]+)\"", text)
    assert imports
    for names, rel in imports:
        have = exports((BUILD_JS.parent / rel).resolve())
        for n in (x.strip().split(" as ")[0] for x in names.split(",") if x.strip()):
            assert n in have, f"{n} is not exported by {rel}"


def test_every_kit_route_build_js_calls_is_one_the_daemon_serves_and_api_md_lists():
    names = called_names(BUILD_JS.read_text())
    table = ui_endpoints()
    kit = {n for n in names if n in KIT_ROUTES}
    assert kit == set(KIT_ROUTES)                           # it uses every one it declares
    assert {n: table[n] for n in kit} == KIT_ROUTES         # KeyError = not in ENDPOINTS
    routes = set(KIT_ROUTES.values())
    assert routes <= daemon_routes()
    assert routes <= api_md_sections()["kit_api"]
    # the others it calls are the job record and the XDC catalogue (the design picker)
    assert names - kit == {"job", "boardXdc"}


def test_the_route_check_catches_a_name_the_table_lacks():
    assert called_names('await call("noSuchKitRoute", {})') == {"noSuchKitRoute"}
    assert "noSuchKitRoute" not in ui_endpoints()


def test_the_kit_ui_block_in_api_js_is_separate_and_the_query_is_additive():
    api = (JS / "api.js").read_text()
    block = api.split("// --- KIT-UI:", 1)[1].split("// --- end KIT-UI ---", 1)[0]
    assert set(re.findall(r"^\s+(\w+): \[", block, re.M)) == set(KIT_ROUTES)
    # call() keeps its three-argument form; the query is a fourth, optional argument
    assert "export async function call(name, params = {}, body = undefined, query = null)" in api


def test_the_section_colours_come_from_tokens_only():
    text = BUILD_JS.read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", text.replace("#token", ""))
    assert "rgb(" not in text and "hsl(" not in text
