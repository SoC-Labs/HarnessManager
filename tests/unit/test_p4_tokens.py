"""P4 TOKENS: ``design/tokens.json`` is the one source of the web UI's colours, and the front
panel's palette is frozen apart from it in ``design/panel/tokens.json`` (UI v2 risk R2);
``tools/gen_tokens.py`` generates from them, and its ``--check`` (in ``make check``) is the
drift gate. Each gate test has a negative twin: a hand edit that the gate must catch.

Board-free and hermetic: the drift tests work on a copy of the files under ``tmp_path``.
``tools/gen_tokens.py`` is a script, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("gen_tokens", REPO / "tools" / "gen_tokens.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_tokens"] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of every file the generator reads or writes."""
    for rel in (G.TOKENS, G.PANEL_TOKENS, G.CSS_OUT, G.PALETTE_OUT, G.HEADER_OUT, G.GLYPHS_OUT,
                G.INDEX_HTML):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, dst)
    for rel in G.stylesheets(REPO):
        shutil.copyfile(REPO / rel, tmp_path / rel)
    shutil.copytree(REPO / G.JS_DIR, tmp_path / G.JS_DIR)
    return tmp_path


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --- the committed files are fresh -----------------------------------------------------------


def test_the_committed_outputs_match_the_tokens_and_app_css_has_no_colours_of_its_own():
    assert G.check(REPO) == []


def test_a_copy_passes_and_writing_it_again_changes_nothing(tree):
    assert G.check(tree) == []
    assert G.write(tree) == []


# --- negative twins: a hand edit is caught ----------------------------------------------------


def test_a_hand_edited_colour_in_tokens_css_is_stale(tree):
    _edit(tree / G.CSS_OUT, "--err: #b1261d;", "--err: #b1261e;")
    problems = G.check(tree)
    assert problems == [f"{G.CSS_OUT.as_posix()} is stale (it does not match design/tokens.json): "
                        "run: python3 tools/gen_tokens.py"]


def test_a_colour_changed_in_tokens_json_moves_the_web_only_never_the_panel(tree):
    """R2: the web's theme changes in design/tokens.json; the panel's generated files (the
    vendored clcd_palette.h, palette.json) stay byte-identical."""
    panel = {rel: (tree / rel).read_bytes() for rel in (G.PALETTE_OUT, G.HEADER_OUT)}
    _edit(tree / G.TOKENS, '"held":           {"light": "#6b3fc4"', '"held":           {"light": "#6b3fc5"')
    doc = json.loads((tree / G.TOKENS).read_text())
    doc["color"]["err"]["dark"] = "#ff0000"                  # a dark value the panel uses (err)
    (tree / G.TOKENS).write_text(json.dumps(doc, indent=2))
    stale = G.check(tree)
    assert len(stale) == 1 and stale[0].startswith(f"{G.CSS_OUT.as_posix()} is stale"), stale
    assert G.write(tree) == [G.CSS_OUT]
    assert G.check(tree) == []
    assert "--held: #6b3fc5;" in (tree / G.CSS_OUT).read_text()
    assert "--err: #ff0000;" in (tree / G.CSS_OUT).read_text()
    assert {rel: (tree / rel).read_bytes() for rel in panel} == panel
    assert f'CLCD_TOKENS_SHA256 "{G.PANEL_SHA256}"' in (tree / G.HEADER_OUT).read_text()


def test_twin_a_colour_changed_in_the_frozen_panel_file_moves_the_panel_and_fails_the_pin(tree):
    _edit(tree / G.PANEL_TOKENS, '"held":           {"light": "#6b3fc4", "dark": "#b69cf5"}',
          '"held":           {"light": "#6b3fc4", "dark": "#b69cf6"}')
    problems = G.check(tree)
    assert len(problems) == 3, problems
    assert "is the front panel's frozen palette (UI v2 risk R2) and it is not the pinned one" in problems[0]
    assert all("is stale (it does not match design/panel/tokens.json)" in p for p in problems[1:])
    assert sorted(G.write(tree)) == sorted([G.PALETTE_OUT, G.HEADER_OUT])
    new_sha = G.sha256_of((tree / G.PANEL_TOKENS).read_bytes())
    assert f'CLCD_TOKENS_SHA256 "{new_sha}"' in (tree / G.HEADER_OUT).read_text()
    assert len(G.check(tree)) == 1                      # regenerated, still not the pinned one


def test_the_panels_generated_files_come_from_the_pinned_frozen_file():
    """R2 proof: the frozen panel file is the pinned one, and the committed panel outputs are
    exactly what it generates (so they are what design/tokens.json at 59011d72 generated)."""
    tokens, _raw = G.load(REPO)
    assert tokens["panel"]["frozen"] == G.PANEL_TOKENS.as_posix()
    panel, praw = G.load_panel(REPO)
    assert G.sha256_of(praw) == G.PANEL_SHA256 == \
        "59011d724e13ec07902e37ffdd3f6a480394cc0995d42649b3af320a73c4a4bb"
    assert "roles" in panel["panel"] and "roles" not in tokens["panel"]
    out = G.outputs(tokens, _raw, panel=(panel, praw))
    for rel in (G.PALETTE_OUT, G.HEADER_OUT):
        assert (REPO / rel).read_text() == out[rel]
    # external callers (tools/clcd_mock.py) resolve the panel in the frozen file too
    assert G.panel_palette(tokens) == G.panel_palette(panel)


def test_a_missing_output_is_reported(tree):
    (tree / G.HEADER_OUT).unlink()
    assert G.check(tree) == ["design/generated/clcd_palette.h is missing: "
                             "run: python3 tools/gen_tokens.py"]


def test_app_css_redefining_a_token_fails(tree):
    _edit(tree / G.APP_CSS, "  --rail-w: 272px;\n", "  --rail-w: 272px;\n  --ok: var(--warn);\n")
    problems = G.check(tree)
    assert len(problems) == 1 and "defines --ok, which design/tokens.json owns" in problems[0]
    line = (tree / G.APP_CSS).read_text().split("\n").index("  --ok: var(--warn);") + 1
    assert problems[0].startswith(f"{G.APP_CSS.as_posix()}:{line}:")


def test_a_per_tab_stylesheet_is_held_to_the_same_rules(tree):
    """UI v2: each tab's lane writes css/<tab>.css; the colour rules apply there too, and
    index.html must load it, after tokens.css."""
    sheet = tree / G.CSS_DIR / "wbtest.css"
    sheet.write_text(".wb-x { color: #123456; background: var(--no-such); }\n")
    problems = G.check(tree)
    assert f"{G.INDEX_HTML.as_posix()}: does not load ./css/wbtest.css" in problems, problems
    assert any(p.startswith(f"{G.CSS_DIR.as_posix()}/wbtest.css:1: colour literal #123456") for p in problems), problems
    assert any("wbtest.css:1: --no-such is not defined" in p for p in problems), problems
    _edit(tree / G.INDEX_HTML, '  <link rel="stylesheet" href="./css/app.css">\n',
          '  <link rel="stylesheet" href="./css/app.css">\n  <link rel="stylesheet" href="./css/wbtest.css">\n')
    sheet.write_text(".wb-x { color: var(--text); }\n")
    assert G.check(tree) == []
    # the twin: loaded before tokens.css
    _edit(tree / G.INDEX_HTML, '  <link rel="stylesheet" href="./css/wbtest.css">\n', "")
    _edit(tree / G.INDEX_HTML, '  <link rel="stylesheet" href="./css/tokens.css">\n',
          '  <link rel="stylesheet" href="./css/wbtest.css">\n  <link rel="stylesheet" href="./css/tokens.css">\n')
    assert G.check(tree) == [f"{G.INDEX_HTML.as_posix()}: loads ./css/tokens.css after ./css/wbtest.css"]


def test_app_css_defining_its_own_layout_property_is_fine(tree):
    # the twin: --rail-w is layout, not a token, and app.css tunes it at >= 1600 px
    assert "--rail-w: 300px" in (tree / G.APP_CSS).read_text()
    assert G.check(tree) == []


@pytest.mark.parametrize("literal", ["#b1261d", "#fff", "rgba(10, 12, 16, 0.45)", "hsl(4 72% 40%)"])
def test_a_colour_literal_in_app_css_fails(tree, literal):
    _edit(tree / G.APP_CSS, "color: var(--on-badge);", f"color: {literal};")
    problems = G.check(tree)
    assert len(problems) == 1 and "colour literal" in problems[0], problems


def test_a_named_colour_in_app_css_fails_but_white_space_does_not(tree):
    assert "white-space: nowrap" in (tree / G.APP_CSS).read_text()     # the twin: not a colour
    _edit(tree / G.APP_CSS, "color: var(--on-badge);", "color: white;")
    problems = G.check(tree)
    assert len(problems) == 1 and "named colour 'white' in color" in problems[0], problems


def test_a_var_that_no_stylesheet_defines_fails(tree):
    _edit(tree / G.APP_CSS, "color: var(--on-badge);", "color: var(--on-badg);")
    problems = G.check(tree)
    assert len(problems) == 1 and "--on-badg is not defined" in problems[0], problems


def test_a_terminal_colour_the_tokens_do_not_define_fails(tree):
    js = tree / G.JS_DIR / "consoles.js"
    _edit(js, 'token("--term-fg")', 'token("--term-fgg")')
    problems = G.check(tree)
    assert len(problems) == 1 and problems[0].startswith(f"{G.JS_DIR.as_posix()}/consoles.js:")
    assert "--term-fgg is not defined" in problems[0]


def test_index_html_must_load_tokens_css_before_app_css(tree):
    index = tree / G.INDEX_HTML
    _edit(index, '  <link rel="stylesheet" href="./css/tokens.css">\n', "")
    assert G.check(tree) == [f"{G.INDEX_HTML.as_posix()}: does not load ./css/tokens.css"]
    _edit(index, '  <link rel="stylesheet" href="./css/app.css">\n',
          '  <link rel="stylesheet" href="./css/app.css">\n  <link rel="stylesheet" href="./css/tokens.css">\n')
    assert G.check(tree) == [f"{G.INDEX_HTML.as_posix()}: loads ./css/tokens.css after ./css/app.css"]


def test_a_windows_checkout_with_crlf_line_ends_is_fresh(tree):
    for rel in (G.TOKENS, G.PANEL_TOKENS, G.CSS_OUT, G.PALETTE_OUT, G.HEADER_OUT, G.GLYPHS_OUT,
                G.APP_CSS, G.INDEX_HTML):
        p = tree / rel
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert G.check(tree) == []
    assert G.sha256_of((tree / G.TOKENS).read_bytes()) == G.sha256_of((REPO / G.TOKENS).read_bytes())


def test_the_command_line_gate_exits_1_on_drift_and_0_when_fresh(tree):
    tool = str(REPO / "tools" / "gen_tokens.py")
    ok = subprocess.run([sys.executable, tool, "--check", "--root", str(tree)],
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    _edit(tree / G.CSS_OUT, "--ok: #1b7a48;", "--ok: #1b7a49;")
    bad = subprocess.run([sys.executable, tool, "--check", "--root", str(tree)],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "tokens.css is stale" in bad.stderr and "FAIL (1 problem)" in bad.stderr


# --- the tokens themselves -------------------------------------------------------------------


def test_the_held_family_is_violet_in_both_themes():
    tokens = json.loads((REPO / G.TOKENS).read_text())
    assert {n: tokens["color"][n] for n in ("held", "held-soft", "held-border")} == {
        "held": {"light": "#6b3fc4", "dark": "#b69cf5"},
        "held-soft": {"light": "#f1ebfb", "dark": "#1f1830"},
        "held-border": {"light": "#d4c3f2", "dark": "#45376a"}}
    assert tokens["grammar"]["held"]["color"] == "held"


@pytest.mark.parametrize(("path", "value", "says"), [
    (("color", "ok", "dark"), "green", "is not #rrggbb"),
    (("color", "ok", "dark"), "#5CC68C", "is not #rrggbb"),
    (("color", "ok"), {"light": "#1b7a48"}, "exactly 'light' and 'dark'"),
    (("panel", "roles", "ok", "fg"), "term-selection", "needs an opaque #rrggbb"),
    (("panel", "roles", "ok", "fg"), "no-such-token", "does not resolve"),
    (("grammar", "ok", "color"), "no-such-token", "must name a 'color' token"),
])
def test_an_invalid_tokens_file_is_refused_with_the_reason(tree, path, value, says):
    rel = G.PANEL_TOKENS if path[0] == "panel" else G.TOKENS    # R2: the roles are the panel file's
    tokens = json.loads((tree / rel).read_text())
    node = tokens
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    (tree / rel).write_text(json.dumps(tokens))
    problems = G.check(tree)
    assert len(problems) == 1 and says in problems[0], problems
    assert G.main(["--root", str(tree)]) == 2                  # write refuses too


# --- the generated outputs -------------------------------------------------------------------


def _header_words(text: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"#define (CLCD_RGB565_\w+)\s+0x([0-9A-F]{4})u", text)}


def test_the_c_header_holds_the_panel_palette_and_the_tokens_sha256():
    tokens, raw = G.load_panel(REPO)
    text = (REPO / G.HEADER_OUT).read_text()
    sha = G.sha256_of(raw)
    assert f"tokens.json sha256: {sha}" in text and f'#define CLCD_TOKENS_SHA256 "{sha}"' in text
    words = _header_words(text)
    pal = G.panel_palette(tokens)
    assert len(words) == 2 * len(pal)
    for role, (fg, bg) in pal.items():
        c = role.upper().replace("-", "_")
        assert (words[f"CLCD_RGB565_{c}_FG"], words[f"CLCD_RGB565_{c}_BG"]) == (fg, bg)
    # docs/design/CLCD_ALIGNMENT.md section 4.2's words
    assert words["CLCD_RGB565_OK_FG"] == 0x5E31 and words["CLCD_RGB565_HELD_FG"] == 0xB4FE
    assert words["CLCD_RGB565_BANNER_ERR_BG"] == 0xB124 and words["CLCD_RGB565_TEXT_BG"] == 0x0000


def test_the_c_header_compiles_as_c99_and_its_table_is_the_palette(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler on PATH")
    src = tmp_path / "t.c"
    src.write_text('#include <stdint.h>\n#include <stdio.h>\n#include "clcd_palette.h"\n'
                   "static const uint16_t pal[CLCD_ROLE_COUNT][2] = CLCD_PALETTE_INIT;\n"
                   "int main(void) {\n  for (int i = 0; i < CLCD_ROLE_COUNT; i++)\n"
                   '    printf("%04X %04X\\n", pal[i][0], pal[i][1]);\n  return 0;\n}\n')
    exe = tmp_path / "t"
    subprocess.run([cc, "-std=c99", "-Wall", "-Wextra", "-Werror", "-pedantic",
                    "-I", str(REPO / G.HEADER_OUT.parent), str(src), "-o", str(exe)],
                   check=True, capture_output=True, timeout=60)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True, timeout=30).stdout
    pal = G.panel_palette(G.load_panel(REPO)[0])
    assert out.split("\n")[:-1] == [f"{fg:04X} {bg:04X}" for fg, bg in pal.values()]


def test_palette_json_resolves_every_token_per_theme_and_the_panel_roles():
    tokens, raw = G.load_panel(REPO)           # R2: the frozen panel file's tokens, as they were
    doc = json.loads((REPO / G.PALETTE_OUT).read_text())
    assert doc["tokens_sha256"] == G.sha256_of(raw) and doc["schema"] == "soclabs-harness-palette/1"
    for theme in ("light", "dark"):
        assert doc["web"][theme] == {k[2:]: v for k, v in G.css_block(tokens, theme).items()}
    assert doc["grammar"]["held"]["dark"] == "#b69cf5"
    assert doc["panel"]["roles"]["held"]["fg565"] == "0xB4FE"
    assert doc["panel"]["roles"]["text"]["bg"] == "#000000", "the panel background is true black"


def test_tokens_css_has_the_same_two_dark_blocks():
    css = (REPO / G.CSS_OUT).read_text()
    media = css[css.index(':root:not([data-theme="light"]) {'):css.index(":root[data-theme=\"dark\"] {")]
    forced = css[css.index(':root[data-theme="dark"] {'):]
    body = re.compile(r"--[a-z0-9-]+: [^;]+;")
    assert body.findall(media) == body.findall(forced) != []


# --- clcd_glyphs.h: the panel's extension glyphs (FIX-PACK-2 item 8) ------------------------------


def _glyph_bitmaps(text: str) -> list[list[int]]:
    body = text.split("font8x16_ext[CLCD_FONT_EXT_COUNT][16] = {", 1)[1].split("};", 1)[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return [[int(v, 16) for v in re.findall(r"0x([0-9A-F]{2})", blk)]
            for blk in re.findall(r"\{([^{}]*)\}", body)]


def _load_mock():
    spec = importlib.util.spec_from_file_location("clcd_mock_fp2", REPO / "tools" / "clcd_mock.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clcd_mock_fp2"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_glyph_header_is_the_one_table_the_mock_renders_with():
    mock = _load_mock()
    table = G.CLCD_GLYPHS
    assert {n: (ch, rows) for n, (ch, rows) in mock.GLYPHS.items()} == table   # one table
    assert [ord(ch) for ch, _ in table.values()] == list(range(0x80, 0x87))
    text = (REPO / G.GLYPHS_OUT).read_text()
    assert _glyph_bitmaps(text) == [rows for _ch, rows in table.values()]
    assert f'#define CLCD_GLYPHS_SHA256 "{G.glyphs_sha256()}"' in text
    assert "#define CLCD_FONT_EXT_FIRST 0x80" in text and "#define CLCD_FONT_EXT_COUNT 7" in text
    assert "#define CLCD_GLYPH_HELD 0x83" in text
    # the header comment: the source commit, the generator command, who vendors it
    assert re.search(r"^ \* source commit:  [0-9a-f]{12}", text, re.M)
    assert "Regenerate in Harness Manager: python3 tools/gen_tokens.py" in text
    assert "vendored by the Linux harness (firmware/clcd/HM_VENDORED.md)" in text


def test_a_bitmap_edited_in_the_generated_glyph_header_fails_the_gate(tree):
    _edit(tree / G.GLYPHS_OUT, "        0x8C, /* #...##.. */", "        0x8D, /* #...##.# */")
    assert G.check(tree) == ["design/generated/clcd_glyphs.h is stale (it does not match "
                             "tools/gen_tokens.py CLCD_GLYPHS): run: python3 tools/gen_tokens.py"]
    tool = str(REPO / "tools" / "gen_tokens.py")
    bad = subprocess.run([sys.executable, tool, "--check", "--root", str(tree)],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "clcd_glyphs.h is stale" in bad.stderr
    assert G.write(tree) == [G.GLYPHS_OUT] and G.check(tree) == []


def test_twin_the_source_commit_line_alone_is_not_drift_and_is_not_rewritten(tree):
    path = tree / G.GLYPHS_OUT
    text = path.read_text()
    moved = re.sub(r"^( \* source commit:  ).*$", r"\g<1>0123456789ab", text, flags=re.M)
    assert moved != text
    path.write_text(moved)
    assert G.check(tree) == [] and G.write(tree) == []
    assert path.read_text() == moved                        # a new HEAD alone rewrites nothing


def test_the_glyph_header_compiles_as_c99_and_holds_the_table(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler on PATH")
    src = tmp_path / "g.c"
    src.write_text('#include <stdio.h>\n#include "clcd_glyphs.h"\n#include "clcd_palette.h"\n'
                   "int main(void) {\n  for (int g = 0; g < CLCD_FONT_EXT_COUNT; g++) {\n"
                   '    for (int y = 0; y < 16; y++) printf("%02X", font8x16_ext[g][y]);\n'
                   '    printf(" %02X\\n", CLCD_FONT_EXT_FIRST + g);\n  }\n  return 0;\n}\n')
    exe = tmp_path / "g"
    subprocess.run([cc, "-std=c99", "-Wall", "-Wextra", "-Werror", "-pedantic",
                    "-I", str(REPO / G.GLYPHS_OUT.parent), str(src), "-o", str(exe)],
                   check=True, capture_output=True, timeout=60)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True, timeout=30).stdout
    want = ["".join(f"{b:02X}" for b in rows) + f" {ord(ch):02X}"
            for ch, rows in G.CLCD_GLYPHS.values()]
    assert out.split("\n")[:-1] == want
