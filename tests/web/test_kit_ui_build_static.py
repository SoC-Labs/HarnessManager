"""KIT-UI and UI v2 (lane UI2-BUILD): the Build tab and the Import dialog, statically: their
wiring, their imports, and the routes they call.

``sections/build.js`` and ``import.js`` call the kit routes by their ``api.js`` ENDPOINTS names
(the KIT-UI and ui2 api-build blocks). This holds them to the table: every name they call is
an ENDPOINTS entry the real daemon serves and docs/API.md lists under ``kit_api``, and every
name they import is one its module exports. Each check has a twin that shows it can fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness_manager.daemon import app as daemon_app
from tests.fakes.t14_api_contract import api_md_sections, daemon_routes, ui_endpoints
from tests.web.test_t10_xdc_static import exports

JS = Path(daemon_app.__file__).resolve().parents[1] / "web" / "static" / "js"
BUILD_JS = JS / "sections" / "build.js"
IMPORT_JS = JS / "import.js"
# the Build tab reads the check from the guide's own check step (POST /kits/check is the CLI's)
KIT_ROUTES = {
    "boardKit": ("GET", "/boards/{}/kit"), "boardGuide": ("GET", "/boards/{}/guide"),
    "kitGet": ("GET", "/kits/{}"), "kitFetch": ("POST", "/kits/fetch"),
    "kitZip": ("GET", "/kits/{}/zip"), "guideScript": ("POST", "/guide/script"),
    "kitPack": ("POST", "/kits/pack"), "designScan": ("POST", "/kits/design/scan"),
}
IMPORT_ROUTES = {"overlayImport": ("POST", "/overlays/import"), "overlayUpload": ("POST", "/overlays/upload")}


def called_names(js: str) -> set[str]:
    return set(re.findall(r'\b(?:call|callBlob|callUpload)\(\s*"(\w+)"', js))


def test_the_nav_puts_build_between_the_workbench_and_the_board():
    # UI v2: five tabs (route.js TABS); XDC is the Build tab's fold, Program is the Workbench's
    app = (JS / "app.js").read_text()
    route = (JS / "route.js").read_text()
    assert 'import { BuildSection } from "./sections/build.js";' in app
    keys = re.findall(r'\{ key: "(\w+)"', route.split("export const TABS", 1)[1].split("];", 1)[0])
    assert keys == ["overview", "workbench", "build", "board", "checks"], keys
    assert 'registerTab("build", BuildTab, { fallback: true });' in app
    assert "<${BuildSection} bid=${bid} />" in app and "<${BoardXdcSection} bid=${bid} />" in app
    assert re.search(r'xdc: \{ tab: "build", sub: "design", part: "xdc" \}', route)
    assert "export function BuildSection" in BUILD_JS.read_text()
    # UI v2: the lane's own body replaces app.js's fallback, and the Import dialog registers
    # itself; build.js imports import.js, so the dialog exists once the app has loaded
    assert 'registerTab("build", BuildSection);' in BUILD_JS.read_text()
    assert 'registerModal("import", ImportDialog);' in IMPORT_JS.read_text()
    assert 'from "../import.js";' in BUILD_JS.read_text()


def test_every_name_build_js_and_import_js_import_is_exported_by_its_module():
    for js in (BUILD_JS, IMPORT_JS):
        text = js.read_text()
        imports = re.findall(r"import\s*\{([^}]*)\}\s*from\s*\"([^\"]+)\"", text)
        assert imports
        for names, rel in imports:
            have = exports((js.parent / rel).resolve())
            for n in (x.strip().split(" as ")[0] for x in names.split(",") if x.strip()):
                assert n in have, f"{n} is not exported by {rel} ({js.name})"


def test_every_kit_route_build_js_calls_is_one_the_daemon_serves_and_api_md_lists():
    names = called_names(BUILD_JS.read_text())
    table = ui_endpoints()
    kit = {n for n in names if n in KIT_ROUTES}
    assert kit == set(KIT_ROUTES)                           # it uses every one it declares
    assert {n: table[n] for n in kit} == KIT_ROUTES         # KeyError = not in ENDPOINTS
    routes = set(KIT_ROUTES.values())
    assert routes <= daemon_routes()
    assert routes <= api_md_sections()["kit_api"]
    # the others it calls are the job record, the XDC catalogue (the design picker) and the
    # XDC export (Design's "Download the RM kit")
    assert names - kit == {"job", "boardXdc", "boardXdcExport"}


def test_every_route_the_import_dialog_calls_is_one_the_daemon_serves_and_api_md_lists():
    names = called_names(IMPORT_JS.read_text())
    assert names == set(IMPORT_ROUTES)
    table = ui_endpoints()
    assert {n: table[n] for n in names} == IMPORT_ROUTES
    routes = set(IMPORT_ROUTES.values())
    assert routes <= daemon_routes()
    assert routes <= api_md_sections()["kit_api"]


def test_the_upload_helper_sits_in_the_lanes_fenced_block_at_the_end_of_api_js():
    api = (JS / "api.js").read_text()
    block = api.split("// --- ui2 build ---", 1)[1]
    assert block.rstrip().endswith("// --- end ui2 build ---")        # the end of the file
    assert "export async function callUpload(" in block
    assert "JSON.stringify" not in block                              # the body is the bytes
    assert "callUpload" not in api.split("// --- ui2 build ---", 1)[0]  # nothing else touched


def test_the_route_check_catches_a_name_the_table_lacks():
    assert called_names('await call("noSuchKitRoute", {})') == {"noSuchKitRoute"}
    assert "noSuchKitRoute" not in ui_endpoints()


def test_the_kit_ui_block_in_api_js_is_separate_and_the_query_is_additive():
    api = (JS / "api.js").read_text()
    block = api.split("// --- KIT-UI:", 1)[1].split("// --- end KIT-UI ---", 1)[0]
    # the KIT-UI block keeps its eight names (kitCheck stays for the CLI-shaped callers);
    # designScan is the ui2 api-build block's
    assert set(re.findall(r"^\s+(\w+): \[", block, re.M)) == (set(KIT_ROUTES) - {"designScan"}) | {"kitCheck"}
    # call() keeps its three-argument form; the query is a fourth, optional argument (and
    # QUIET-POLL's request options a fifth: the background marker)
    assert "export async function call(name, params = {}, body = undefined, query = null, " \
        "opts = {})" in api


def test_the_section_colours_come_from_tokens_only():
    for js in (BUILD_JS, IMPORT_JS):
        text = js.read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", text.replace("#token", "")), js.name
        assert "rgb(" not in text and "hsl(" not in text
