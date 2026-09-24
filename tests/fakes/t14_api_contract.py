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
#: The frozen lease-requests design (lanes LR-A..D): its "API" table adds routes to hub_api.
LEASE_REQUESTS_MD = REPO / "docs" / "LEASE_REQUESTS.md"
STATIC = REPO / "src" / "harness_manager" / "web" / "static"
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


TABLE_SECTIONS = ("## Endpoints", "## Week-plan additions")
_MODULE = re.compile(r"`(\w+_api)\.py`")


def _row_routes(line: str) -> list[tuple[str, str]]:
    first_cell = line.split("|")[1]
    method, prev, out = "", "", []
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
        out.append((method, normalise(path)))
        prev = path
    return out


def parse_api_md_sections(text: str) -> dict[str, set[tuple[str, str]]]:
    """Routes by where API.md defines them: ``core`` (the Endpoints table and the Events
    section) or the daemon extension module named in a week-plan heading (``hub_api`` ...)."""
    out: dict[str, set[tuple[str, str]]] = {"core": set()}
    section, modules = "", ["core"]
    for line in text.splitlines():
        if line.startswith("## "):
            section = next((s for s in TABLE_SECTIONS if line.startswith(s)), line.strip())
        elif line.startswith("### ") and section == "## Week-plan additions":
            modules = _MODULE.findall(line) or ["core"]
        if line.startswith("## "):
            modules = ["core"]
        if line.startswith("- **Endpoint:**"):
            for seg in _SEG.findall(line):
                m = _CALL.match(seg)
                if m:
                    out["core"].add((m.group(1), normalise(m.group(2))))
        if section in TABLE_SECTIONS and line.startswith("| `"):
            for route in _row_routes(line):
                # A heading may name two modules ("power_api.py, update_api.py"): a route
                # goes to the one whose name its path carries, else the first.
                owner = next((m for m in modules if f"/{m[:-4]}" in route[1]), modules[0])
                out.setdefault(owner, set()).add(route)
    # A week-plan row may repeat a core route with a new field (`POST /probe {via?}`).
    for mod, routes in out.items():
        if mod != "core":
            routes -= out["core"]
    return out


def parse_api_md(text: str) -> set[tuple[str, str]]:
    """Every (method, normalised path) API.md defines: the core table, the Events section and
    the week-plan additions."""
    return set().union(*parse_api_md_sections(text).values())


def api_md_sections() -> dict[str, set[tuple[str, str]]]:
    return parse_api_md_sections(API_MD.read_text(encoding="utf-8"))


def api_md_endpoints() -> set[tuple[str, str]]:
    return parse_api_md(API_MD.read_text(encoding="utf-8"))


def parse_lease_requests_md(text: str) -> set[tuple[str, str]]:
    """The routes docs/LEASE_REQUESTS.md adds: its "## API" table (the four), and any full
    board route a row of its "## Decisions" table names (D11: ``DELETE
    /boards/{bid}/lease/taken``). Shorthand in the prose (``GET /lease``) is not a route."""
    out: set[tuple[str, str]] = set()
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = "api" if line.startswith("## API") else (
                "decisions" if line.startswith("## Decisions") else "")
        elif section == "api" and line.startswith("| `"):
            out.update(_row_routes(line))
        elif section == "decisions" and line.startswith("| D"):
            for seg in _SEG.findall(line):
                m = _CALL.match(seg)
                if m and m.group(2).startswith("/boards/{"):
                    out.add((m.group(1), normalise(m.group(2))))
    return out


def lease_requests_md_endpoints() -> set[tuple[str, str]]:
    return parse_lease_requests_md(LEASE_REQUESTS_MD.read_text(encoding="utf-8"))


def frozen_endpoints() -> set[tuple[str, str]]:
    """Every route the frozen docs define: API.md plus LEASE_REQUESTS.md (until the lead folds
    the latter into API.md; the union holds either way)."""
    return api_md_endpoints() | lease_requests_md_endpoints()


_ENTRY = re.compile(r'^\s*(\w+):\s*\[\s*"([A-Z]+)"\s*,\s*"([^"]+)"\s*\]', re.M)


def parse_ui_endpoints(js: str) -> dict[str, tuple[str, str]]:
    """``{name: (method, normalised path)}`` from the ENDPOINTS table of js/api.js."""
    block = js.split("export const ENDPOINTS", 1)[1].split("});", 1)[0]
    return {name: (method, normalise(path)) for name, method, path in _ENTRY.findall(block)}


def ui_endpoints() -> dict[str, tuple[str, str]]:
    return parse_ui_endpoints(API_JS.read_text(encoding="utf-8"))


def ui_additive() -> set[str]:
    """Names in ENDPOINTS the UI marks ADDITIVE: served by harness-manager-daemon beyond API.md v1."""
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
    """The real harness-manager-daemon's route table (Team T13), minus its catch-all 404 route."""
    from harness_manager.daemon.app import create_app
    from harness_manager.demo import DemoEngine

    engine = DemoEngine(speed=0)
    try:
        app = create_app(engine, token="route-table-only", static_dir=None)
        return {(m, p) for m, p in app_routes(app) if p != "/{}"}
    finally:
        engine.close_all()
