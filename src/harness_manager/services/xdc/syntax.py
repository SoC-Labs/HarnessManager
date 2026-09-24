"""A light XDC (Tcl subset) syntax check, in Python.

It is not Vivado. It catches what a generator bug produces: unbalanced braces,
brackets or quotes; a command Vivado's constraint mode does not know (read_xdc
rejects Tcl control flow such as ``if``/``foreach``/``puts``); a nested command that is
not an object query; a malformed ``-period``/``-waveform``; a ``set_property`` without
its three arguments; a dangling line continuation. ``check_xdc(text)`` returns the
problems as ``"line N: message"`` strings; an empty list means the text passed.
"""

from __future__ import annotations

import re

COMMANDS = {
    "create_clock", "create_generated_clock", "set_clock_groups", "set_false_path",
    "set_max_delay", "set_min_delay", "set_multicycle_path", "set_input_delay",
    "set_output_delay", "set_property", "set_clock_uncertainty", "set_input_jitter",
    "set_system_jitter", "set_disable_timing", "set_case_analysis", "create_pblock",
    "add_cells_to_pblock", "resize_pblock", "set_load", "group_path", "set_bus_skew",
    "set_max_skew", "set_clock_sense", "set_data_check", "set",
}
QUERIES = {
    "get_ports", "get_clocks", "get_pins", "get_cells", "get_nets", "get_pblocks",
    "get_iobanks", "get_sites", "get_package_pins", "current_design", "get_clock_regions",
    "all_inputs", "all_outputs", "all_clocks", "get_generated_clocks", "expr", "list",
    "concat", "llength", "lindex",
}
FORBIDDEN = {"if", "foreach", "for", "while", "puts", "proc", "source", "return",
             "error", "catch", "switch", "eval", "exec"}


def _logical_lines(text: str) -> tuple[list[tuple[int, str]], list[str]]:
    out: list[tuple[int, str]] = []
    problems: list[str] = []
    buf, start = "", 0
    lines = text.splitlines()
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if not buf and line.lstrip().startswith("#"):
            continue
        if not buf:
            start = n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            if n == len(lines):
                problems.append(f"line {n}: the file ends inside a line continuation")
            continue
        buf += line
        if buf.strip():
            out.append((start, buf))
        buf = ""
    return out, problems


def _strip_inline_comment(cmd: str) -> str:
    # `;#` starts a comment when it is outside braces/quotes.
    depth, quote = 0, False
    for i, ch in enumerate(cmd):
        if ch == '"' and (i == 0 or cmd[i - 1] != "\\"):
            quote = not quote
        elif not quote and ch == "{":
            depth += 1
        elif not quote and ch == "}":
            depth -= 1
        elif not quote and depth == 0 and ch == ";" and cmd[i + 1:].lstrip().startswith("#"):
            return cmd[:i]
    return cmd


def _balanced(cmd: str) -> str:
    stack: list[str] = []
    quote = False
    pairs = {"}": "{", "]": "["}
    for i, ch in enumerate(cmd):
        if ch == "\\":
            continue
        if ch == '"' and (i == 0 or cmd[i - 1] != "\\") and not (stack and stack[-1] == "{"):
            quote = not quote
            continue
        if quote and ch not in "[]":
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or stack[-1] != pairs[ch]:
                return f"unbalanced {ch!r}"
            stack.pop()
    if quote:
        return "unterminated quote"
    if stack:
        return f"unclosed {stack[-1]!r}"
    return ""


def _words(cmd: str) -> list[str]:
    words, cur, depth = [], "", 0
    for ch in cmd:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                words.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        words.append(cur)
    return words


def check_xdc(text: str) -> list[str]:
    logical, problems = _logical_lines(text)
    for n, line in logical:
        cmd = _strip_inline_comment(line).strip()
        if not cmd:
            continue
        why = _balanced(cmd)
        if why:
            problems.append(f"line {n}: {why}")
            continue
        words = _words(cmd)
        head = words[0]
        if head in FORBIDDEN:
            problems.append(f"line {n}: {head!r} is Tcl control flow; read_xdc rejects it")
            continue
        if head not in COMMANDS:
            problems.append(f"line {n}: unknown XDC command {head!r}")
            continue
        for q in re.findall(r"\[\s*([A-Za-z_]+)", cmd):
            if q not in QUERIES:
                problems.append(f"line {n}: {q!r} is not an object query")
        if head == "set_property" and len(words) < 4 and "-dict" not in words:
            problems.append(f"line {n}: set_property needs a property, a value and objects")
        if head in ("create_clock", "create_generated_clock"):
            if head == "create_clock":
                m = re.search(r"-period\s+(\S+)", cmd)
                if not m or not re.fullmatch(r"\d+(\.\d+)?", m.group(1)):
                    problems.append(f"line {n}: create_clock needs a numeric -period")
            w = re.search(r"-waveform\s+\{([^}]*)\}", cmd)
            if w and not all(re.fullmatch(r"\d+(\.\d+)?", x) for x in w.group(1).split()):
                problems.append(f"line {n}: -waveform takes numbers")
            if not re.search(r"\[\s*get_(ports|pins|nets)\b", cmd):
                problems.append(f"line {n}: {head} names no source object")
        if head in ("set_false_path", "set_max_delay", "set_min_delay", "set_multicycle_path") \
                and not re.search(r"-(from|to|through)\b", cmd):
            problems.append(f"line {n}: {head} needs -from, -to or -through")
        if head == "set_clock_groups" and cmd.count("-group") < 2:
            problems.append(f"line {n}: set_clock_groups needs at least two -group")
    return problems
