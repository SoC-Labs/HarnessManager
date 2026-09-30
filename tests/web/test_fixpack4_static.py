"""FIX-PACK-4, the UI review's static findings: no dead first-party code ships.

The review found ``sections/placeholders.js`` imported by nothing and ``AppVersionChip``
(selfupdate.js) rendered nowhere. Both are gone; these checks keep it so: every first-party
module is reachable from the page's entry points (index.html's scripts), and every exported
component (a PascalCase function) is used somewhere. Each check has a negative twin that
feeds it an orphan and expects it caught. Pure file reads: no browser, daemon or board.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.fakes.t14_api_contract import STATIC

_IMPORT = re.compile(r"""(?:^|[\s;})])(?:import|export)\b[^;"'`]*?\bfrom\s*(["'])([^"']+)\1"""
                     r"""|(?:^|[\s;])import\s*(["'])([^"']+)\3""", re.M)
_SCRIPT = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""")
_COMPONENT = re.compile(r"^export (?:async )?function ([A-Z]\w*)", re.M)


def first_party_js(root: Path = STATIC) -> dict[Path, str]:
    return {p.resolve(): p.read_text(encoding="utf-8") for p in root.rglob("*.js")
            if "vendor" not in p.relative_to(root).parts}


def orphans(modules: dict[Path, str], entries: list[Path]) -> list[str]:
    """The modules no entry point reaches through its imports."""
    seen: set[Path] = set()
    stack = [e.resolve() for e in entries]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        text = modules.get(p)
        if text is None:
            continue                               # vendored: its own imports are its own
        for m in _IMPORT.finditer(text):
            stack.append((p.parent / (m.group(2) or m.group(4))).resolve())
    return sorted(p.name for p in modules if p not in seen)


def unused_components(modules: dict[Path, str]) -> list[str]:
    """Exported components that no other module names and their own module never renders."""
    out = []
    for p, text in modules.items():
        for name in _COMPONENT.findall(text):
            word = re.compile(rf"\b{name}\b")
            own = len(word.findall(text)) > 1
            other = any(word.search(t) for q, t in modules.items() if q != p)
            if not own and not other:
                out.append(f"{p.name}: {name}")
    return sorted(out)


def entry_points() -> list[Path]:
    index = STATIC / "index.html"
    return [(index.parent / src).resolve() for src in _SCRIPT.findall(index.read_text())]


def test_every_first_party_module_is_reachable_from_the_page():
    entries = entry_points()
    assert {e.name for e in entries} == {"app.js", "theme-boot.js"}
    assert orphans(first_party_js(), entries) == []
    assert not (STATIC / "js" / "sections" / "placeholders.js").exists()


def test_negative_twin_an_orphan_module_is_caught(tmp_path):
    (tmp_path / "app.js").write_text('import { a } from "./used.js";\n')
    (tmp_path / "used.js").write_text("export const a = 1;\n")
    (tmp_path / "placeholders.js").write_text("export function ClocksSection() {}\n")
    assert orphans(first_party_js(tmp_path), [tmp_path / "app.js"]) == ["placeholders.js"]


def test_every_exported_component_is_used():
    assert unused_components(first_party_js()) == []
    assert "AppVersionChip" not in (STATIC / "js" / "selfupdate.js").read_text()


def test_negative_twin_a_component_nobody_renders_is_caught(tmp_path):
    (tmp_path / "app.js").write_text('import { Shown } from "./ui.js";\nrender(html`<${Shown} />`);\n')
    (tmp_path / "ui.js").write_text(
        "export function Shown() { return null; }\n"
        "export function Inner() { return null; }\nexport function Outer() { return html`<${Inner} />`; }\n"
        "export function AppVersionChip() { return null; }\n")
    assert unused_components(first_party_js(tmp_path)) == ["ui.js: AppVersionChip", "ui.js: Outer"]
