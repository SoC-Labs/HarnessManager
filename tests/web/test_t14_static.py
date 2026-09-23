"""The web UI's static files: every reference resolves locally, nothing loads from the
network, every vendored file is listed with its version, hash and licence, and the UI
calls only endpoints docs/API.md defines. Each check has a negative twin that feeds it
a bad input and expects it to fail."""

from __future__ import annotations

import hashlib
import importlib.resources
import re
from pathlib import Path

import pytest

from tests.fakes.t14_api_contract import (
    REPO,
    STATIC,
    api_md_endpoints,
    daemon_routes,
    normalise,
    parse_api_md,
    parse_ui_endpoints,
    ui_additive,
    ui_endpoints,
)

VENDOR = STATIC / "vendor"
FIRST_PARTY_SUFFIXES = (".html", ".js", ".css", ".svg")

# URLs that appear as text inside vendored files and are never fetched: XML namespace
# names, and the credits in xterm.js's licence header.
ALLOWED_VENDOR_URLS = {
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1998/Math/MathML",
    "http://bellard.org/jslinux/",
    "https://github.com/chjj/term.js",
}
ALLOWED_FIRST_PARTY_URLS = {"http://www.w3.org/2000/svg"}

_FROM = re.compile(r"""(?:^|[\s;})])(?:import|export)\b[^;"'`]*?\bfrom\s*(["'])([^"']+)\1""", re.M)
_BARE = re.compile(r"""(?:^|[\s;])import\s*(["'])([^"']+)\1""", re.M)
_DYN = re.compile(r"""\bimport\s*\(\s*(["'])([^"']+)\1\s*\)""")
_URL = re.compile(r"""https?://[^\s"'`)<>\\]+""")
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""")
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*["']([^"']+)["']""")
_HTML_REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""")


def served_files() -> list[Path]:
    return sorted(p for p in STATIC.rglob("*") if p.is_file())


def first_party(p: Path) -> bool:
    return VENDOR not in p.parents


# --- checkers (pure, so the negative twins can feed them bad input) ---------------------


def js_imports(text: str) -> list[str]:
    return [m.group(2) for rx in (_FROM, _BARE, _DYN) for m in rx.finditer(text)]


def import_problems(source: Path, text: str, root: Path = STATIC) -> list[str]:
    """Every import that is not a relative path to an existing file under ``root``."""
    problems = []
    for spec in js_imports(text):
        if not spec.startswith(("./", "../")):
            problems.append(f"{source.name}: {spec!r} is not a relative path (bare or remote)")
            continue
        target = (source.parent / spec).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            problems.append(f"{source.name}: {spec!r} does not resolve to a served file")
    return problems


def remote_urls(text: str, allowed: set[str]) -> list[str]:
    return [u for u in _URL.findall(text) if u not in allowed]


def local_ref_problems(source: Path, refs: list[str]) -> list[str]:
    problems = []
    for ref in refs:
        if ref.startswith(("data:", "#")):
            continue
        if "://" in ref or ref.startswith("//"):
            problems.append(f"{source.name}: {ref!r} is remote")
            continue
        target = (source.parent / ref.split("?")[0].split("#")[0]).resolve()
        if not target.is_file():
            problems.append(f"{source.name}: {ref!r} does not exist")
    return problems


# --- the served files -------------------------------------------------------------------------


def test_index_html_references_resolve_to_served_files():
    index = STATIC / "index.html"
    refs = _HTML_REF.findall(index.read_text())
    assert "./js/app.js" in refs and "./css/app.css" in refs
    assert local_ref_problems(index, refs) == []


def test_index_html_has_no_inline_script():
    # The UI runs under script-src 'self' (harness_manager.web.CONTENT_SECURITY_POLICY).
    text = (STATIC / "index.html").read_text()
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", text, re.S):
        assert "src=" in m.group(1), "inline <script> would be blocked by the CSP"
        assert m.group(2).strip() == ""


def test_every_js_import_resolves_to_a_vendored_or_first_party_file():
    problems = []
    for p in served_files():
        if p.suffix == ".js":
            problems += import_problems(p, p.read_text(encoding="utf-8"))
    assert problems == []


def test_the_import_checker_rejects_bare_remote_and_missing_imports():
    src = STATIC / "js" / "app.js"
    bad = ('import { h } from "preact";\n'
           'import x from "https://cdn.jsdelivr.net/npm/preact/+esm";\n'
           'import "./no-such-file.js";\n'
           'const m = await import("../../../../etc/passwd");\n')
    problems = import_problems(src, bad)
    assert len(problems) == 4, problems


def test_every_css_url_and_import_resolves():
    problems = []
    for p in served_files():
        if p.suffix == ".css":
            text = p.read_text(encoding="utf-8")
            problems += local_ref_problems(p, _CSS_URL.findall(text) + _CSS_IMPORT.findall(text))
    assert problems == []


def test_the_css_checker_rejects_a_remote_font():
    css = STATIC / "css" / "fonts.css"
    bad = '@import url("https://fonts.googleapis.com/css2?family=Inter");'
    assert local_ref_problems(css, _CSS_IMPORT.findall(bad)) != []


def test_no_http_urls_in_first_party_html_js_css():
    found = {}
    for p in served_files():
        if first_party(p) and p.suffix in FIRST_PARTY_SUFFIXES:
            urls = remote_urls(p.read_text(encoding="utf-8"), ALLOWED_FIRST_PARTY_URLS)
            if urls:
                found[str(p.relative_to(STATIC))] = urls
    assert found == {}


def test_vendored_files_contain_only_known_non_fetched_urls():
    found = {}
    for p in served_files():
        if not first_party(p) and p.suffix in (".js", ".css"):
            urls = remote_urls(p.read_text(encoding="utf-8"), ALLOWED_VENDOR_URLS)
            if urls:
                found[str(p.relative_to(STATIC))] = urls
    assert found == {}


def test_the_url_checker_catches_a_cdn_reference():
    assert remote_urls('<link href="https://unpkg.com/x.css">', ALLOWED_FIRST_PARTY_URLS) == [
        "https://unpkg.com/x.css"]


# --- vendoring: versions, hashes, licences ---------------------------------------------------


def vendor_manifest() -> dict[str, dict[str, str]]:
    rows = {}
    for line in (VENDOR / "VENDOR.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].startswith("`") and "/" in cells[0]:
            rows[cells[0].strip("`")] = {"package": cells[1].strip("`"), "sha256": cells[4].strip("`")}
    return rows


def vendor_present() -> set[str]:
    return {str(p.relative_to(VENDOR)) for p in VENDOR.rglob("*")
            if p.is_file() and p.name != "VENDOR.md"}


def manifest_problems(present: set[str], listed: dict[str, dict[str, str]],
                      read=lambda name: (VENDOR / name).read_bytes()) -> list[str]:
    problems = [f"{n}: on disk, not in VENDOR.md" for n in sorted(present - set(listed))]
    problems += [f"{n}: in VENDOR.md, not on disk" for n in sorted(set(listed) - present)]
    for name, row in listed.items():
        if not re.search(r"@\d+\.\d+\.\d+$", row["package"]):
            problems.append(f"{name}: version not pinned")
        if name in present and row["sha256"] != "generated":
            if hashlib.sha256(read(name)).hexdigest() != row["sha256"]:
                problems.append(f"{name}: changed without its VENDOR.md entry")
    return problems


def test_every_vendored_file_is_listed_pinned_and_hashed():
    assert manifest_problems(vendor_present(), vendor_manifest()) == []


def test_the_manifest_check_notices_an_unlisted_or_edited_file():
    present = vendor_present() | {"evil/tracker.js"}
    listed = vendor_manifest()
    assert manifest_problems(present, listed) == ["evil/tracker.js: on disk, not in VENDOR.md"]
    edited = manifest_problems(vendor_present(), listed,
                               read=lambda name: b"tampered" if name.endswith("htm.module.js")
                               else (VENDOR / name).read_bytes())
    assert edited == ["htm/htm.module.js: changed without its VENDOR.md entry"]


def test_each_vendored_library_ships_its_licence():
    for lib in sorted(p for p in VENDOR.iterdir() if p.is_dir()):
        licences = [p for p in lib.iterdir() if p.name.startswith("LICENSE")]
        assert licences, f"{lib.name} has no LICENSE file"
        for lic in licences:
            assert lic.stat().st_size > 500, f"{lic} looks empty"


def test_fonts_are_open_font_licence():
    for lic in (VENDOR / "fonts").glob("LICENSE*"):
        assert "SIL OPEN FONT LICENSE" in lic.read_text().upper()


# --- icons -------------------------------------------------------------------------------------


def test_every_literal_icon_name_exists_in_the_vendored_set():
    icons = set(re.findall(r'^\s+"([a-z0-9-]+)":', (VENDOR / "lucide" / "icons.js").read_text(), re.M))
    used = set()
    for p in (STATIC / "js").rglob("*.js"):
        text = p.read_text()
        used |= set(re.findall(r'(?<!\[)\b(?:name|icon)=\\?"([a-z0-9-]+)"', text))
        used |= set(re.findall(r'\bicon:\s*"([a-z0-9-]+)"', text))
        used |= set(re.findall(r'\bname=\$\{[^}]*?"([a-z0-9-]+)"', text))
    assert used, "no icon names found: the pattern is broken"
    assert used - icons == set()


# --- the package ---------------------------------------------------------------------------------


def test_static_files_ship_as_package_data():
    root = importlib.resources.files("harness_manager.web") / "static"
    assert (root / "index.html").is_file()
    assert (root / "vendor" / "VENDOR.md").is_file()
    text = (REPO / "pyproject.toml").read_text()
    assert '"harness_manager.web" = ["static/**/*", "static/*"]' in text


def test_web_package_exposes_the_static_dir():
    import harness_manager.web as web

    assert web.static_dir() == STATIC.resolve()
    assert "script-src 'self'" in web.CONTENT_SECURITY_POLICY
    assert web.MEDIA_TYPES[".js"].startswith("text/javascript")


# --- the endpoints the UI calls are the ones API.md defines -----------------------------------


def test_every_endpoint_the_ui_calls_is_in_api_md():
    defined = api_md_endpoints()
    called = {n: ep for n, ep in ui_endpoints().items() if n not in ui_additive()}
    assert len(called) >= 20
    assert {name: ep for name, ep in called.items() if ep not in defined} == {}


def test_the_ui_additive_endpoints_are_ones_harness_manager_daemon_serves_and_api_md_lacks():
    # The only way past API.md: a route the real daemon (T13) serves, named in ADDITIVE.
    additive = {n: ep for n, ep in ui_endpoints().items() if n in ui_additive()}
    assert set(additive) == ui_additive()
    served = daemon_routes()
    for name, ep in additive.items():
        assert ep in served, name
        assert ep not in api_md_endpoints(), f"{name} is in API.md now: drop it from ADDITIVE"


def test_the_ui_calls_the_session_route_api_md_now_lists():
    assert ui_endpoints()["session"] == ("GET", "/boards/{}/session")
    assert ("GET", "/boards/{}/session") in api_md_endpoints()


def test_the_endpoint_check_catches_an_endpoint_api_md_lacks():
    js = 'export const ENDPOINTS = Object.freeze({\n  sneaky: ["GET", "/boards/{bid}/secret"],\n});'
    assert parse_ui_endpoints(js)["sneaky"] not in api_md_endpoints()


def test_the_api_md_parser_reads_combined_rows():
    md = ("## Endpoints\n| a | b | c |\n|---|---|---|\n"
          "| `GET /boards/{bid}/debug` · `POST .../debug/up` | x | y |\n"
          "| `GET /boards/{bid}/controller/temps` · `/osc` | x | y |\n")
    assert parse_api_md(md) == {("GET", "/boards/{}/debug"), ("POST", "/boards/{}/debug/up"),
                               ("GET", "/boards/{}/controller/temps"),
                               ("GET", "/boards/{}/controller/osc")}
    assert normalise("/api/v1/events?token=x") == "/events"


@pytest.mark.parametrize("name", ["deploy", "restore", "debugUp", "reboot", "sdBackup", "sdRestore"])
def test_long_operations_are_called_as_posts(name):
    # API.md: these answer 202 {job}; the UI waits on job.* events for them.
    assert ui_endpoints()[name][0] == "POST"
