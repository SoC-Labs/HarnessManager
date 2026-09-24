"""Small HDL and XDC text helpers: bus names, ANSI port lists, XDC pin assignments.

Deliberately tiny and dependency-free. They read only the regular subset the board
collateral uses (one ``set_property`` per line; ANSI-style module headers). Anything
they cannot read is reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- bus names ------------------------------------------------------------------------------

_BIT = re.compile(r"^(?P<base>[A-Za-z_][A-Za-z0-9_$]*)\[(?P<idx>\d+)\]$")
_RANGE = re.compile(r"^(?P<base>[A-Za-z_][A-Za-z0-9_$]*)\[(?P<msb>\d+):(?P<lsb>\d+)\]$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def split_bit(name: str) -> tuple[str, int | None]:
    """``"SH0_IO[3]"`` -> ``("SH0_IO", 3)``; ``"OSCCLK1"`` -> ``("OSCCLK1", None)``."""
    m = _BIT.match(name)
    if m:
        return m.group("base"), int(m.group("idx"))
    return name, None


def expand(name: str) -> list[str]:
    """``"USER_SW[3:0]"`` -> the four bit names, MSB first; a bit or scalar -> itself.

    Raises ``ValueError`` for a name that is neither (a typo must not become a pin).
    """
    name = name.strip()
    m = _RANGE.match(name)
    if m:
        base, msb, lsb = m.group("base"), int(m.group("msb")), int(m.group("lsb"))
        step = -1 if msb >= lsb else 1
        return [f"{base}[{i}]" for i in range(msb, lsb + step, step)]
    if _BIT.match(name) or _NAME.match(name):
        return [name]
    raise ValueError(f"not a port or net name: {name!r}")


def bit_sort_key(name: str) -> tuple[str, int]:
    base, idx = split_bit(name)
    return base, -1 if idx is None else idx


# --- ANSI port lists --------------------------------------------------------------------------


@dataclass(frozen=True)
class HdlPort:
    name: str
    direction: str          # "in" | "out" | "inout"
    msb: int | None = None  # None = scalar
    lsb: int | None = None
    line: int = 0

    @property
    def width(self) -> int:
        if self.msb is None or self.lsb is None:
            return 1
        return abs(self.msb - self.lsb) + 1

    def bits(self) -> list[str]:
        if self.msb is None or self.lsb is None:
            return [self.name]
        return expand(f"{self.name}[{self.msb}:{self.lsb}]")


_DIRS = {"input": "in", "output": "out", "inout": "inout"}
_ITEM = re.compile(
    r"^(?:(?P<dir>input|output|inout)\b)?"
    r"(?:\s*\b(?:wire|logic|reg|var|signed|unsigned|tri|wand|wor)\b)*"
    r"\s*(?:\[\s*(?P<msb>[^:\]]+?)\s*:\s*(?P<lsb>[^\]]+?)\s*\])?"
    r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*$", re.S)


def _blank_comments(text: str) -> str:
    """Comments and preprocessor lines become spaces: offsets (and so line numbers) stay put."""
    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r"//[^\n]*", blank, text)
    return re.sub(r"^[ \t]*`[^\n]*", blank, text, flags=re.M)


def _eval_bound(expr: str, params: dict[str, int]) -> int:
    expr = expr.strip()
    if re.fullmatch(r"\d+", expr):
        return int(expr)
    # PARAM-1, PARAM - 1, PARAM
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:-\s*(\d+))?", expr)
    if m and m.group(1) in params:
        return params[m.group(1)] - int(m.group(2) or 0)
    raise ValueError(f"cannot evaluate the range bound {expr!r}")


def _matching(text: str, i: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``i``."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced parentheses in the module header")


def _split_top(text: str, base: int) -> list[tuple[int, str]]:
    """Split at top-level commas: ``[(offset in the whole text, item)]``."""
    items, depth, cur = [], 0, 0
    for j, ch in enumerate(text):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append((base + cur, text[cur:j]))
            cur = j + 1
    items.append((base + cur, text[cur:]))
    return items


def parse_ansi_ports(text: str, module: str | None = None,
                     params: dict[str, int] | None = None) -> list[HdlPort]:
    """The ports of an ANSI-style (System)Verilog module header.

    ``module`` picks one module when the text has several; ``params`` resolves
    symbolic ranges such as ``[NGPIO-1:0]`` (a ``#(parameter N = 16)`` block adds its
    own). Raises ``ValueError`` when there is no such module or a declaration cannot
    be read. `ifdef'd ports are all read (the caller decides which defines apply).
    """
    params = dict(params or {})
    clean = _blank_comments(text)
    rx = rf"\bmodule\s+({re.escape(module) if module else '[A-Za-z_][A-Za-z0-9_$]*'})\b"
    m = re.search(rx, clean)
    if not m:
        raise ValueError(f"no module {module!r} in the text" if module else "no module in the text")
    i = m.end()
    while i < len(clean) and clean[i].isspace():
        i += 1
    if clean.startswith("#", i):
        po = clean.index("(", i)
        pc = _matching(clean, po)
        for pm in re.finditer(r"\bparameter\s+(?:int\s+|integer\s+)?([A-Za-z_][A-Za-z0-9_]*)"
                              r"\s*=\s*(\d+)", clean[po:pc]):
            params.setdefault(pm.group(1), int(pm.group(2)))
        i = pc + 1
    while i < len(clean) and clean[i].isspace():
        i += 1
    if not clean.startswith("(", i):
        raise ValueError(f"module {m.group(1)}: no port list")
    close = _matching(clean, i)
    ports: list[HdlPort] = []
    direction = None
    for pos, item in _split_top(clean[i + 1:close], i + 1):
        body = item.strip()
        if not body:
            continue
        line = clean.count("\n", 0, pos + (len(item) - len(item.lstrip()))) + 1
        im = _ITEM.match(re.sub(r"\s+", " ", body))
        if not im:
            raise ValueError(f"line {line}: cannot read the port declaration {body!r}")
        if im.group("dir"):
            direction = _DIRS[im.group("dir")]
        if direction is None:
            raise ValueError(f"line {line}: {body!r} has no direction (not an ANSI header)")
        msb = lsb = None
        if im.group("msb") is not None:
            msb = _eval_bound(im.group("msb"), params)
            lsb = _eval_bound(im.group("lsb"), params)
        ports.append(HdlPort(im.group("name"), direction, msb, lsb, line))
    return ports


# --- XDC pin assignments ------------------------------------------------------------------------


@dataclass
class XdcPort:
    name: str                    # a bit name: "USER_nLED[3]" or a scalar "OSCCLK1"
    pin: str = ""
    iostandard: str = ""
    props: dict[str, str] | None = None
    pin_line: int = 0
    group: str = ""


_SETPROP = re.compile(r"^\s*set_property\s+(?P<prop>[A-Z_.]+)\s+(?P<value>\S+)\s+"
                      r"\[get_ports\s+(?:\{(?P<b>[^}]+)\}|(?P<p>[^\]\s]+))\s*\]\s*$")
_CLOCK = re.compile(r"^\s*create_clock\b(?P<args>.*)\[get_ports\s+(?:\{(?P<b>[^}]+)\}|(?P<p>[^\]\s]+))"
                    r"\s*\]\s*$")


@dataclass
class XdcClock:
    port: str
    name: str
    period_ns: float
    line: int


def parse_xdc_pins(text: str) -> tuple[dict[str, XdcPort], list[XdcClock]]:
    """Every port a pin XDC places, with its IOSTANDARD and extra properties.

    Wildcard property lines (``{USER_nLED[*]}``) apply to every bit of that bus the
    file places. The group of a port is the first comment line of the banner block
    above it (``####`` or ``#----`` rules).
    """
    ports: dict[str, XdcPort] = {}
    wild: list[tuple[str, str, str, int]] = []
    clocks: list[XdcClock] = []
    group = ""
    prev_rule = False
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if ";#" in s and not s.startswith("#"):
            s = s.split(";#", 1)[0].rstrip()
        if re.match(r"^#\s*[#=\-]{8,}", s) or re.match(r"^#{8,}", s):
            prev_rule = True
            continue
        if prev_rule and s.startswith("#"):
            title = s.lstrip("#").strip()
            if title:
                group = re.split(r"\s+--\s+|\s+—\s+", title)[0].strip()
            prev_rule = False
            continue
        prev_rule = False
        cm = _CLOCK.match(s)
        if cm:
            args = cm.group("args")
            port = (cm.group("b") or cm.group("p")).strip()
            pm = re.search(r"-period\s+([0-9.]+)", args)
            nm = re.search(r"-name\s+(\{[^}]*\}|\S+)", args)
            if pm:
                clocks.append(XdcClock(port, (nm.group(1).strip("{}") if nm else port),
                                       float(pm.group(1)), n))
            continue
        m = _SETPROP.match(s)
        if not m:
            continue
        prop, value = m.group("prop"), m.group("value")
        target = (m.group("b") or m.group("p")).strip()
        if "*" in target:
            wild.append((target, prop, value, n))
            continue
        port = ports.setdefault(target, XdcPort(target, props={}, group=group))
        if prop == "PACKAGE_PIN":
            port.pin, port.pin_line = value, n
            port.group = port.group or group
        elif prop == "IOSTANDARD":
            port.iostandard = value
        else:
            assert port.props is not None
            port.props[prop] = value
    for target, prop, value, _n in wild:
        rx = re.compile("^" + re.escape(target).replace(r"\*", r"\d+") + "$")
        for port in ports.values():
            if rx.match(port.name):
                if prop == "IOSTANDARD":
                    port.iostandard = value
                elif prop != "PACKAGE_PIN":
                    assert port.props is not None
                    port.props[prop] = value
    return ports, clocks
