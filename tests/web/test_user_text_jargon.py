"""No internal lane, wave or ticket names in what a user reads (T1).

Scans the string literals of every JS file under web/static/js and every non-docstring
string constant of the Python packages (comments and docstrings are for developers).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
JARGON = re.compile(
    r"HARNESS-DIST|\bmint \d\b|FIX-PACK|\bCCR\b|\bBRINGUP-|\bIDENTITY-|KIT-CORE|KIT-LIC|SET-UI"
    r"|\b[Ll]ane [A-Z][\w-]*|\(\s*[DTLHPUSQ]\d+\s*\)")
STR = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`")


def js_strings(text: str):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        yield from (m.group(0) for m in STR.finditer(line))


def py_strings(text: str):
    tree = ast.parse(text)
    docs = set()
    for n in ast.walk(tree):
        if (isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)):
            docs.add(id(n.body[0].value))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.value


def offenders():
    out = []
    for p in sorted(SRC.rglob("*")):
        if "checks" in p.parts:     # the HIL check plans are the developers' (D4, F6 are ids)
            continue
        if p.suffix == ".js":
            strings = js_strings(p.read_text())
        elif p.suffix == ".py":
            strings = py_strings(p.read_text())
        else:
            continue
        out += [f"{p.relative_to(SRC)}: {s[:90]!r}" for s in strings if JARGON.search(s)]
    return out


def test_no_internal_names_in_user_visible_strings():
    assert offenders() == []


def test_negative_twin_the_scanner_catches_the_old_texts():
    for old in ("comes with Linux v2.1 (HARNESS-DIST L3)",
                "the build kit arrived with lane KIT-CORE",
                "Until mint 4, stage0 rescue",
                "it predates FIX-PACK-9", "harness Lane C adds it", "(CCR T7-2)"):
        assert JARGON.search(old), old
    assert not JARGON.search("FPGA UART lane 2 (hard-wired)")
    assert list(js_strings('// a HARNESS-DIST comment\nconst a = "fine";')) == ['"fine"']
    assert js_strings and list(js_strings('x = "lane KIT-CORE"')) != []
