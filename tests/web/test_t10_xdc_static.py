"""T10: the Board & XDC section, statically: wiring, imports, and its routes exist.

``sections/xdc.js`` calls the XDC routes by their ``api.js`` ENDPOINTS names (CCR T10-2
put them there and in docs/API.md, whose tests hold the table to the contract). This test
holds the section to the table: every endpoint name it calls is an ENDPOINTS entry the real
daemon serves, and every name it imports is one its module exports.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness_manager.daemon import app as daemon_app
from tests.fakes.t14_api_contract import api_md_sections, daemon_routes, ui_endpoints

JS = Path(daemon_app.__file__).resolve().parents[1] / "web" / "static" / "js"
XDC_JS = JS / "sections" / "xdc.js"


def exports(path: Path) -> set[str]:
    text = path.read_text()
    names = set(re.findall(r"export\s+(?:async\s+)?(?:function|class|const|let)\s+(\w+)", text))
    for block in re.findall(r"export\s*\{([^}]*)\}", text):
        names |= {n.strip().split(" as ")[-1] for n in block.split(",") if n.strip()}
    return names


def test_the_nav_renders_the_xdc_section_from_its_own_module():
    app = (JS / "app.js").read_text()
    assert 'import { BoardXdcSection } from "./sections/xdc.js";' in app
    assert "export function BoardXdcSection" in XDC_JS.read_text()


def test_every_name_xdc_js_imports_is_exported_by_its_module():
    text = XDC_JS.read_text()
    imports = re.findall(r"import\s*\{([^}]*)\}\s*from\s*\"([^\"]+)\"", text)
    assert imports
    for names, rel in imports:
        have = exports((XDC_JS.parent / rel).resolve())
        for n in (x.strip() for x in names.split(",") if x.strip()):
            assert n in have, f"{n} is not exported by {rel}"


def test_the_import_check_catches_a_missing_export():
    assert "noSuchHelper" not in exports(JS / "api.js")
    assert "socketUrl" in exports(JS / "api.js")


def called_names(js: str) -> set[str]:
    """The ENDPOINTS names a section passes to call()/callBlob() (directly or through a wrapper
    whose first argument is the name)."""
    return set(re.findall(r'\b(?:call|callBlob|xdcCall)\(\s*"(\w+)"', js))


def test_every_endpoint_xdc_js_calls_is_an_xdc_api_route_the_daemon_serves():
    names = called_names(XDC_JS.read_text())
    assert names == {"boardXdc", "boardXdcExport"}
    table = ui_endpoints()
    routes = {table[n] for n in names}                # KeyError = not in ENDPOINTS
    assert routes == {("GET", "/boards/{}/xdc"), ("POST", "/boards/{}/xdc/export")}
    assert routes <= daemon_routes()                  # T14's reader of the real route table
    assert routes <= api_md_sections()["xdc_api"]


def test_the_name_check_catches_a_name_the_table_lacks():
    assert called_names('await call("noSuchRoute", { bid })') == {"noSuchRoute"}
    assert "noSuchRoute" not in ui_endpoints()


def test_the_daemon_loads_xdc_api_and_the_section_still_covers_an_older_daemon():
    assert "xdc_api" in daemon_app.EXTENSIONS
    assert ("GET", "/boards/{}/xdc") in daemon_routes()
    assert "has no XDC routes yet" in XDC_JS.read_text()
