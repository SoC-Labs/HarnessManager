"""T10: the Board & XDC section, statically: wiring, imports, and its routes exist.

``sections/xdc.js`` calls the XDC routes through its own small table (they are not in
docs/API.md yet, CCR T10-2), so this test is that table's contract: every route it calls
is one ``daemon/xdc_api.py`` serves, and every name it imports is one its module exports.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness_manager.daemon import app as daemon_app
from tests.fakes.t14_api_contract import daemon_routes

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


def test_every_route_xdc_js_calls_is_one_xdc_api_serves(monkeypatch):
    monkeypatch.setattr(daemon_app, "EXTENSIONS", (*daemon_app.EXTENSIONS, "xdc_api"))
    served = daemon_routes()                     # T14's reader of the real route table
    called = {(m, re.sub(r"\{\w+\}", "{}", p))
              for m, p in re.findall(r'\["(GET|POST)",\s*"([^"]+)"\]', XDC_JS.read_text())}
    assert called == {("GET", "/boards/{}/xdc"), ("POST", "/boards/{}/xdc/export")}
    assert called <= served, called - served


def test_without_the_ccr_the_daemon_does_not_serve_them_and_the_section_says_so():
    served = daemon_routes()
    assert ("GET", "/boards/{}/xdc") not in served or "xdc_api" in daemon_app.EXTENSIONS
    assert "has no XDC routes yet" in XDC_JS.read_text()
