"""Readers for the two sides of the web contract (Team T14 test helpers).

- ``api_md_endpoints()``: the endpoint set docs/API.md defines, as (METHOD, template).
- ``ui_endpoints()``: the endpoint set the web UI calls (``ENDPOINTS`` in js/api.js).

Templates are compared with their parameter names blanked (``/boards/{}``), so a
rename of a path parameter is not a contract change but a new path is.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API_MD = REPO / "docs" / "API.md"
STATIC = REPO / "src" / "socharness" / "web" / "static"
API_JS = STATIC / "js" / "api.js"

METHODS = ("GET", "POST", "DELETE", "PUT", "PATCH", "WS")
_SEG = re.compile(r"`([^`]+)`")
_CALL = re.compile(r"^(" + "|".join(METHODS) + r")\s+(\S+)")


def normalise(template: str) -> str:
    path = template.split("?", 1)[0]
    if path.startswith("/api/v1"):
        path = path[len("/api/v1"):]
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip("/") or "/")


def _resolve(prev: str, path: str) -> str:
    """``.../debug/detect`` after ``/boards/{bid}/debug`` -> ``/boards/{bid}/debug/detect``."""
    if path.startswith("..."):
        rest = path[3:].lstrip("/")
        first = rest.split("/", 1)[0]
        parts = prev.split("/")
        if first in parts:
            base = "/".join(parts[:parts.index(first)])
        else:
            base = "/".join(parts[:-1])
        return f"{base}/{rest}"
    return path


def parse_api_md(text: str) -> set[tuple[str, str]]:
    """Every (method, normalised path) in API.md's endpoint table and its Events section."""
    out: set[tuple[str, str]] = set()
    in_table = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_table = line.strip() == "## Endpoints"
        if line.startswith("- **Endpoint:**"):
            for seg in _SEG.findall(line):
                m = _CALL.match(seg)
                if m:
                    out.add((m.group(1), normalise(m.group(2))))
        if not in_table or not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        method, prev = "", ""
        for seg in _SEG.findall(first_cell):
            m = _CALL.match(seg)
            if m:
                method = m.group(1)
                path = _resolve(prev, m.group(2))
            elif seg.startswith("/") and method:
                # "`GET /x/temps` · `/osc`": same method, sibling path.
                path = prev.rsplit("/", 1)[0] + seg
            else:
                continue            # a body sketch such as `{overlay}`
            out.add((method, normalise(path)))
            prev = path
    return out


def api_md_endpoints() -> set[tuple[str, str]]:
    return parse_api_md(API_MD.read_text(encoding="utf-8"))


_ENTRY = re.compile(r'^\s*(\w+):\s*\[\s*"([A-Z]+)"\s*,\s*"([^"]+)"\s*\]', re.M)


def parse_ui_endpoints(js: str) -> dict[str, tuple[str, str]]:
    """``{name: (method, normalised path)}`` from the ENDPOINTS table of js/api.js."""
    block = js.split("export const ENDPOINTS", 1)[1].split("});", 1)[0]
    return {name: (method, normalise(path)) for name, method, path in _ENTRY.findall(block)}


def ui_endpoints() -> dict[str, tuple[str, str]]:
    return parse_ui_endpoints(API_JS.read_text(encoding="utf-8"))


def ui_additive() -> set[str]:
    """Names in ENDPOINTS the UI marks ADDITIVE: served by socharnessd beyond API.md v1."""
    js = API_JS.read_text(encoding="utf-8")
    block = js.split("export const ADDITIVE", 1)[1].split(";", 1)[0]
    return set(re.findall(r'"(\w+)"', block))


def app_routes(app) -> set[tuple[str, str]]:
    """(METHOD, normalised path) for every /api/v1 route of a FastAPI app (WS as "WS")."""
    def walk(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)     # FastAPI >= 0.140 includes lazily
            if inner is not None:
                yield from walk(inner.routes)
            else:
                yield r

    out = set()
    for r in walk(app.routes):
        path = getattr(r, "path", "")
        if not path.startswith("/api/v1"):
            continue
        path = path.replace(":path}", "}")
        for m in getattr(r, "methods", None) or {"WS"}:
            if m != "HEAD":
                out.add((m, normalise(path)))
    return out


def daemon_routes() -> set[tuple[str, str]]:
    """The real socharnessd's route table (Team T13), minus its catch-all 404 route."""
    from socharness.daemon.app import create_app
    from socharness.demo import DemoEngine

    engine = DemoEngine(speed=0)
    try:
        app = create_app(engine, token="route-table-only", static_dir=None)
        return {(m, p) for m, p in app_routes(app) if p != "/{}"}
    finally:
        engine.close_all()
