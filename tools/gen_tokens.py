"""Harness Manager's design tokens: one JSON file, three generated outputs (decision P4).

``design/tokens.json`` is the only place a colour, font or radius of the web UI is
written down. This script generates, from it:

- ``src/harness_manager/web/static/css/tokens.css``: the custom properties the web UI's
  ``app.css`` uses (light, dark by ``prefers-color-scheme``, dark by ``data-theme``);
- ``design/generated/palette.json``: every token resolved per theme, the status grammar,
  and the front panel's colour roles as RGB565;
- ``design/generated/clcd_palette.h``: the panel roles as RGB565 words for the Linux
  harness's panel renderer (decision P3: the panel palette changes there only). Its header
  carries the ``tokens.json`` sha256; the platform vendors it unchanged.

``--check`` is the drift gate (``make check``): it fails when an output is stale or
missing, or when ``app.css`` has colours of its own: a token it redefines, a colour
literal, or a ``var(--name)`` (also in the UI's JavaScript) that no stylesheet defines.

Usage::

    python3 tools/gen_tokens.py              # write the outputs (only those that changed)
    python3 tools/gen_tokens.py --check      # exit 1 and say why when anything drifted
    python3 tools/gen_tokens.py --root DIR   # another checkout (the tests' copies)

Stdlib only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOKENS = Path("design/tokens.json")
CSS_OUT = Path("src/harness_manager/web/static/css/tokens.css")
PALETTE_OUT = Path("design/generated/palette.json")
HEADER_OUT = Path("design/generated/clcd_palette.h")
APP_CSS = Path("src/harness_manager/web/static/css/app.css")
INDEX_HTML = Path("src/harness_manager/web/static/index.html")
JS_DIR = Path("src/harness_manager/web/static/js")

SCHEMA = "soclabs-harness-tokens/1"
PALETTE_SCHEMA = "soclabs-harness-palette/1"
THEMES = ("light", "dark")
#: Sections that become CSS custom properties: per theme, and the same in both.
THEMED = ("color", "effect")
STATIC = "static"
REGENERATE = "run: python3 tools/gen_tokens.py"

_NAME = re.compile(r"[a-z][a-z0-9-]*")
_HEX = re.compile(r"#[0-9a-f]{6}")
_RGBA = re.compile(r"rgba\(\s*\d{1,3},\s*\d{1,3},\s*\d{1,3},\s*(0|1|0?\.\d+)\s*\)")


# --- reading ---------------------------------------------------------------------------------


def text_of(data: bytes) -> str:
    """UTF-8, with CRLF read as LF (a Windows checkout converts line ends)."""
    return data.decode("utf-8").replace("\r\n", "\n")


def sha256_of(data: bytes) -> str:
    """The tokens' fingerprint: ``sha256sum design/tokens.json`` on an LF checkout."""
    return hashlib.sha256(text_of(data).encode("utf-8")).hexdigest()


def validate(tokens: object) -> list[str]:
    """What is wrong with a tokens document (empty when it is usable)."""
    if not isinstance(tokens, dict):
        return ["the document is not a JSON object"]
    if tokens.get("schema") != SCHEMA:
        return [f"schema is {tokens.get('schema')!r}, not {SCHEMA!r}"]
    problems: list[str] = []
    seen: dict[str, str] = {}
    for section in (STATIC, *THEMED):
        entries = tokens.get(section)
        if not isinstance(entries, dict) or not entries:
            problems.append(f"'{section}' must be a non-empty object")
            continue
        for name, value in entries.items():
            where = f"{section}.{name}"
            if not _NAME.fullmatch(name):
                problems.append(f"{where}: a token name is lower case letters, digits and '-'")
            if name in seen:
                problems.append(f"{where}: also defined in '{seen[name]}'")
            seen[name] = section
            if section == STATIC:
                if not isinstance(value, str) or not value.strip() or ";" in value:
                    problems.append(f"{where}: must be a CSS value (text, no ';')")
                continue
            if not isinstance(value, dict) or set(value) != set(THEMES):
                problems.append(f"{where}: must have exactly 'light' and 'dark'")
                continue
            for theme in THEMES:
                v = value[theme]
                if not isinstance(v, str) or not v.strip() or ";" in v:
                    problems.append(f"{where}.{theme}: must be a CSS value (text, no ';')")
                elif section == "color" and not (_HEX.fullmatch(v) or _RGBA.fullmatch(v)):
                    problems.append(f"{where}.{theme}: {v!r} is not #rrggbb (lower case) or "
                                    "rgba(r, g, b, a)")
    if problems:
        return problems
    colors = tokens["color"]
    for state, spec in (tokens.get("grammar") or {}).items():
        if not isinstance(spec, dict) or spec.get("color") not in colors:
            problems.append(f"grammar.{state}: its colour must name a 'color' token")
    panel = tokens.get("panel")
    if not isinstance(panel, dict) or not isinstance(panel.get("roles"), dict) or not panel["roles"]:
        return [*problems, "'panel.roles' must be a non-empty object"]
    if panel.get("theme") not in THEMES:
        problems.append(f"panel.theme must be light or dark, not {panel.get('theme')!r}")
        return problems
    for role, spec in panel["roles"].items():
        if not _NAME.fullmatch(role):
            problems.append(f"panel.roles.{role}: a role name is lower case letters, digits and '-'")
        for side in ("fg", "bg"):
            ref = spec.get(side) if isinstance(spec, dict) else None
            try:
                value = resolve(tokens, str(ref), panel["theme"])
            except (KeyError, ValueError) as exc:
                problems.append(f"panel.roles.{role}.{side}: {ref!r} does not resolve ({exc})")
                continue
            if not _HEX.fullmatch(value):
                problems.append(f"panel.roles.{role}.{side}: {ref!r} is {value!r}; the panel "
                                "needs an opaque #rrggbb")
    return problems


def load(root: Path = ROOT) -> tuple[dict, bytes]:
    """The tokens document and its raw bytes. Raises ValueError when it is unusable."""
    raw = (root / TOKENS).read_bytes()
    try:
        tokens = json.loads(text_of(raw))
    except ValueError as exc:
        raise ValueError(f"{TOKENS}: not JSON: {exc}") from None
    problems = validate(tokens)
    if problems:
        raise ValueError("\n".join(f"{TOKENS}: {p}" for p in problems))
    return tokens, raw


def load_tokens(path: Path | None = None) -> dict:
    """Just the document (``tools/clcd_mock.py``)."""
    root = ROOT if path is None else Path(path).resolve().parents[1]
    return load(root)[0]


# --- colours ---------------------------------------------------------------------------------


def _rgb(hexs: str) -> tuple[int, int, int]:
    h = hexs.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb565(hexs: str) -> int:
    """#rrggbb -> the panel's 16-bit word (rounded, not truncated)."""
    r, g, b = _rgb(hexs)
    return (round(r * 31 / 255) << 11) | (round(g * 63 / 255) << 5) | round(b * 31 / 255)


def rgb565_hex(v: int, *, bgr: bool = False) -> str:
    """The colour a 16-bit word shows (5/6-bit fields expanded by bit replication).
    ``bgr=True`` shows what an R<->B channel swap would put on the glass."""
    r5, g6, b5 = (v >> 11) & 31, (v >> 5) & 63, v & 31
    if bgr:
        r5, b5 = b5, r5
    r, g, b = (r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)
    return f"#{r:02x}{g:02x}{b:02x}"


def resolve(tokens: dict, ref: str, theme: str = "dark") -> str:
    """A panel role's colour reference -> its value. ``"#ffffff"`` is literal; ``"err"``
    is the theme's token; ``"err@light"`` names the theme; a bare ``"bg"`` honours the
    panel's ``bg_override`` (true black on a backlit TFT)."""
    if ref.startswith("#"):
        if not _HEX.fullmatch(ref):
            raise ValueError(f"{ref!r} is not #rrggbb")
        return ref
    name, _, want = ref.partition("@")
    if want and want not in THEMES:
        raise ValueError(f"theme {want!r} is not light or dark")
    if name == "bg" and not want and tokens.get("panel", {}).get("bg_override"):
        return tokens["panel"]["bg_override"]
    return tokens["color"][name][want or theme]


def panel_palette(tokens: dict) -> dict[str, tuple[int, int]]:
    """role -> (fg565, bg565), in the panel's theme."""
    theme = tokens["panel"]["theme"]
    return {role: (rgb565(resolve(tokens, spec["fg"], theme)), rgb565(resolve(tokens, spec["bg"], theme)))
            for role, spec in tokens["panel"]["roles"].items()}


# --- CSS -------------------------------------------------------------------------------------


def css_block(tokens: dict, theme: str) -> dict[str, str]:
    """The ``--name: value`` pairs a theme sets (colours, then effects)."""
    return {f"--{name}": vals[theme] for section in THEMED for name, vals in tokens[section].items()}


def static_block(tokens: dict) -> dict[str, str]:
    return {f"--{name}": value for name, value in tokens[STATIC].items()}


def token_properties(tokens: dict) -> set[str]:
    """Every custom property the tokens own."""
    return {f"--{name}" for section in (STATIC, *THEMED) for name in tokens[section]}


def _decls(pairs: dict[str, str], indent: str) -> list[str]:
    return [f"{indent}{k}: {v};" for k, v in pairs.items()]


def render_css(tokens: dict, sha: str) -> str:
    dark = css_block(tokens, "dark")
    lines = [
        f"/* GENERATED by tools/gen_tokens.py from {TOKENS.as_posix()}: do not edit this file.",
        f"   Edit {TOKENS.as_posix()}, then {REGENERATE}",
        f"   tokens.json sha256 {sha}",
        "",
        "   Harness Manager's design tokens. index.html loads this before app.css. Light is the",
        "   default; dark follows prefers-color-scheme, and <html data-theme=\"light|dark\">",
        "   overrides it (the two dark blocks are the same). */",
        "",
        ":root {",
        "  color-scheme: light;",
        *_decls(static_block(tokens), "  "),
        "",
        *_decls(css_block(tokens, "light"), "  "),
        "}",
        "",
        "@media (prefers-color-scheme: dark) {",
        "  :root:not([data-theme=\"light\"]) {",
        "    color-scheme: dark;",
        *_decls(dark, "    "),
        "  }",
        "}",
        "",
        ":root[data-theme=\"dark\"] {",
        "  color-scheme: dark;",
        *_decls(dark, "  "),
        "}",
    ]
    return "\n".join(lines) + "\n"


# --- palette.json ----------------------------------------------------------------------------


def _word(v: int) -> str:
    return f"0x{v:04X}"


def panel_roles(tokens: dict) -> dict[str, dict[str, str]]:
    """Each panel role: the token colours, the RGB565 words, and what the glass shows."""
    theme = tokens["panel"]["theme"]
    out: dict[str, dict[str, str]] = {}
    for role, spec in tokens["panel"]["roles"].items():
        fg, bg = resolve(tokens, spec["fg"], theme), resolve(tokens, spec["bg"], theme)
        f5, b5 = rgb565(fg), rgb565(bg)
        out[role] = {"fg_token": spec["fg"], "bg_token": spec["bg"], "fg": fg, "bg": bg,
                     "fg565": _word(f5), "bg565": _word(b5),
                     "fg_shown": rgb565_hex(f5), "bg_shown": rgb565_hex(b5)}
    return out


def render_palette(tokens: dict, sha: str) -> str:
    panel = tokens["panel"]
    doc = {
        "schema": PALETTE_SCHEMA,
        "generated_by": "tools/gen_tokens.py",
        "source": TOKENS.as_posix(),
        "tokens_sha256": sha,
        "web": {
            "static": {name: value for name, value in tokens[STATIC].items()},
            **{theme: {name: vals[theme] for section in THEMED for name, vals in tokens[section].items()}
               for theme in THEMES},
        },
        "grammar": {state: {"color": spec["color"],
                            **{theme: tokens["color"][spec["color"]][theme] for theme in THEMES},
                            **{k: spec[k] for k in ("icon", "glyph", "ascii", "means") if k in spec}}
                    for state, spec in (tokens.get("grammar") or {}).items()},
        "panel": {**{k: panel[k] for k in ("size_px", "grid", "depth", "channel_order", "theme",
                                           "bg_override") if k in panel},
                  "roles": panel_roles(tokens)},
    }
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


# --- clcd_palette.h --------------------------------------------------------------------------


def _c_name(role: str) -> str:
    return role.upper().replace("-", "_")


def render_header(tokens: dict, sha: str) -> str:
    panel = tokens["panel"]
    roles = panel_roles(tokens)
    width = max(len(_c_name(r)) for r in roles)
    size = "x".join(str(n) for n in panel.get("size_px", ()))
    order = str(panel.get("channel_order", "")).split(":", 1)[0].strip()
    lines = [
        "/* clcd_palette.h: the MPS3 front panel's colour roles as RGB565 words.",
        " *",
        f" * GENERATED by Harness Manager tools/gen_tokens.py from {TOKENS.as_posix()}: do not edit.",
        " * Vendor it unchanged; to change a colour, change the tokens in Harness Manager and",
        " * regenerate there (decision P4). Linux harness only (decision P3).",
        " *",
        f" * tokens.json sha256: {sha}",
        f" * schema:             {SCHEMA}",
        f" * panel:              {size}, RGB565 high byte first, channel order {order},",
        f" *                     theme {panel['theme']}, background {panel.get('bg_override') or 'from the theme'}",
        " *",
        " * CLCD_PALETTE_INIT initialises a table indexed by enum clcd_role, {fg, bg} per role:",
        " *     static const uint16_t pal[CLCD_ROLE_COUNT][2] = CLCD_PALETTE_INIT;",
        " */",
        "#ifndef CLCD_PALETTE_H",
        "#define CLCD_PALETTE_H",
        "",
        f"#define CLCD_TOKENS_SHA256 \"{sha}\"",
        "",
        "enum clcd_role {",
    ]
    for i, role in enumerate(roles):
        lines.append(f"    CLCD_ROLE_{_c_name(role)}{' = 0' if i == 0 else ''},")
    lines += ["    CLCD_ROLE_COUNT", "};", "",
              "/* role fg/bg: the RGB565 word, then the token it comes from and the colour the",
              " * word shows on the glass. */"]
    for role, r in roles.items():
        for side in ("fg", "bg"):
            name = f"CLCD_RGB565_{_c_name(role)}_{side.upper()}".ljust(width + 16)
            src = r[side + "_token"]
            src = "literal" if src.startswith("#") else src
            lines.append(f"#define {name} {r[side + '565']}u  /* {src} {r[side]} "
                         f"-> {r[side + '_shown']} */")
    lines += ["", "#define CLCD_PALETTE_INIT { \\"]
    for role in roles:
        c = _c_name(role)
        lines.append(f"    {{ CLCD_RGB565_{c}_FG, CLCD_RGB565_{c}_BG }}, \\")
    lines += ["}", "", "#endif /* CLCD_PALETTE_H */"]
    return "\n".join(lines) + "\n"


# --- generate and check ----------------------------------------------------------------------


def outputs(tokens: dict, raw: bytes) -> dict[Path, str]:
    sha = sha256_of(raw)
    return {CSS_OUT: render_css(tokens, sha), PALETTE_OUT: render_palette(tokens, sha),
            HEADER_OUT: render_header(tokens, sha)}


def write(root: Path = ROOT) -> list[Path]:
    """Write every output that differs from what is on disk; the paths written."""
    tokens, raw = load(root)
    written = []
    for rel, text in outputs(tokens, raw).items():
        path = root / rel
        if path.is_file() and text_of(path.read_bytes()) == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(rel)
    return written


def _strip_comments(css: str) -> str:
    """CSS without comments; each comment becomes as many newlines (line numbers hold)."""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


_DEFINE = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:")
_USE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)")
_TOKEN_CALL = re.compile(r"""\btoken\(\s*["'](--[a-zA-Z0-9_-]+)["']""")
_HEX_LITERAL = re.compile(r"(?<![\w-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])")
_COLOR_FN = re.compile(r"(?<![\w-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(", re.I)
_DECL = re.compile(r"(?<![\w-])([a-z-]+)\s*:\s*([^;{}]*)")
#: Properties whose value is (or holds) a colour, and the named colours a value must not use.
_COLOR_PROPS = re.compile(r"(?:.*-)?(?:color|background|border|outline|fill|stroke|shadow|"
                          r"column-rule|text-decoration)(?:-.*)?")
_NAMED = re.compile(r"(?<![\w-])(white|black|red|green|blue|gray|grey|silver|maroon|purple|"
                    r"fuchsia|lime|olive|yellow|navy|teal|aqua|orange|pink|brown)(?![\w-])", re.I)


def app_css_problems(css: str, tokens: dict, where: str = APP_CSS.as_posix()) -> list[str]:
    """``app.css`` must take every colour from the tokens: it redefines none of them and
    writes no colour of its own (a hex, rgb()/hsl()/..., or a named colour)."""
    text = _strip_comments(css)
    owned = token_properties(tokens)
    problems = []
    for m in _DEFINE.finditer(text):
        if m.group(1) in owned:
            problems.append(f"{where}:{_line(text, m.start())}: defines {m.group(1)}, which "
                            f"{TOKENS.as_posix()} owns: change it there, then {REGENERATE}")
    for m in _HEX_LITERAL.finditer(text):
        problems.append(f"{where}:{_line(text, m.start())}: colour literal {m.group(0)}: use a "
                        f"token (var(--name)), adding one to {TOKENS.as_posix()} if none fits")
    for m in _COLOR_FN.finditer(text):
        problems.append(f"{where}:{_line(text, m.start())}: colour literal {m.group(0)}...): use a "
                        f"token (var(--name)), adding one to {TOKENS.as_posix()} if none fits")
    for m in _DECL.finditer(text):
        prop, value = m.group(1), m.group(2)
        if prop.startswith("--") or not _COLOR_PROPS.fullmatch(prop):
            continue
        for n in _NAMED.finditer(value):
            problems.append(f"{where}:{_line(text, m.start(2) + n.start())}: named colour "
                            f"{n.group(0)!r} in {prop}: use a token (var(--name))")
    return problems


def reference_problems(root: Path, tokens: dict) -> list[str]:
    """Every ``var(--name)`` in app.css and the UI's JavaScript, and every ``token("--name")``
    (the terminal's colours), names a property some stylesheet defines."""
    css = _strip_comments(text_of((root / APP_CSS).read_bytes()))
    defined = token_properties(tokens) | {m.group(1) for m in _DEFINE.finditer(css)}
    problems = []
    sources = [(APP_CSS, css)]
    js_dir = root / JS_DIR
    if js_dir.is_dir():
        sources += [(p.relative_to(root), text_of(p.read_bytes())) for p in sorted(js_dir.rglob("*.js"))]
    for rel, text in sources:
        for pattern in (_USE, _TOKEN_CALL):
            for m in pattern.finditer(text):
                if m.group(1) not in defined:
                    problems.append(f"{rel.as_posix()}:{_line(text, m.start())}: {m.group(1)} is not "
                                    f"defined by {TOKENS.as_posix()} or app.css")
    return problems


def index_problems(root: Path) -> list[str]:
    html = text_of((root / INDEX_HTML).read_bytes())
    tokens_at, app_at = html.find('href="./css/tokens.css"'), html.find('href="./css/app.css"')
    if tokens_at < 0:
        return [f"{INDEX_HTML.as_posix()}: does not load ./css/tokens.css"]
    if app_at >= 0 and tokens_at > app_at:
        return [f"{INDEX_HTML.as_posix()}: loads ./css/tokens.css after ./css/app.css"]
    return []


def check(root: Path = ROOT) -> list[str]:
    """Everything that drifted from ``design/tokens.json`` (empty when nothing did)."""
    try:
        tokens, raw = load(root)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    problems = []
    for rel, text in outputs(tokens, raw).items():
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel.as_posix()} is missing: {REGENERATE}")
        elif text_of(path.read_bytes()) != text:
            problems.append(f"{rel.as_posix()} is stale (it does not match {TOKENS.as_posix()}): "
                            f"{REGENERATE}")
    problems += app_css_problems(text_of((root / APP_CSS).read_bytes()), tokens)
    problems += reference_problems(root, tokens)
    problems += index_problems(root)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 if an output is stale or app.css has its own colours")
    ap.add_argument("--root", type=Path, default=ROOT, help="the checkout (default: this one)")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    if a.check:
        problems = check(root)
        for p in problems:
            print(f"gen_tokens: {p}", file=sys.stderr)
        if problems:
            print(f"gen_tokens: FAIL ({len(problems)} problem{'s' if len(problems) != 1 else ''})",
                  file=sys.stderr)
            return 1
        print(f"gen_tokens: ok ({TOKENS.as_posix()} sha256 {sha256_of((root / TOKENS).read_bytes())[:12]})")
        return 0
    try:
        written = write(root)
    except (OSError, ValueError) as exc:
        print(f"gen_tokens: {exc}", file=sys.stderr)
        return 2
    for rel in written:
        print(f"wrote {rel.as_posix()}")
    if not written:
        print("gen_tokens: every output is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
