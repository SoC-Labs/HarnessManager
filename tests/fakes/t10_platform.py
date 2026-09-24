"""T10 test helpers: the platform repo (read-only, through git show) and XDC semantics.

The platform checkout is optional: tests that need it skip with the reason when it is
not next to this repo (CI) or ``$HM_PLATFORM_DIR`` does not point at one.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLATFORM = Path(os.environ.get("HM_PLATFORM_DIR", str(REPO.parent / "mps3-nanosoc-platform")))
GOLDEN = Path(__file__).resolve().parent / "t10_golden"


def platform_ref(model: dict) -> str:
    return os.environ.get("HM_PLATFORM_REF") or model["status"]["platform_ref"]


def git_show(ref: str, path: str) -> str:
    if not (PLATFORM / ".git").exists():
        pytest.skip(f"the platform repo is not at {PLATFORM} (set HM_PLATFORM_DIR)")
    r = subprocess.run(["git", "-C", str(PLATFORM), "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"{ref}:{path} is not in the platform repo: {r.stderr.strip()}")
    return r.stdout


def md_boundary(text: str) -> list[tuple[str, str, str, str]]:
    """partition-pins.md's generated tables -> [(group, signal, dir, width)], in order.

    Written independently of the generator's parser, so the drift test is a second reader.
    """
    out = []
    for m in re.finditer(r"BEGIN GENERATED\[boundary-(\w+)\].*?\n(.*?)<!-- END", text, re.S):
        for row in m.group(2).splitlines():
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) < 3 or not cells[0].startswith("`"):
                continue
            out.append((m.group(1), cells[0].strip("`"), cells[1], cells[2]))
    return out


def xdc_semantics(text: str) -> dict:
    """{clocks: {name: period}, from: {port}, to: {port}} with continuations joined."""
    text = re.sub(r"\\\n\s*", " ", text)
    clocks, frm, to = {}, set(), set()
    for line in text.splitlines():
        line = line.split(";#")[0].strip()
        if line.startswith("#"):
            continue
        m = re.match(r"create_clock\s+-name\s+(\S+)\s+-period\s+([0-9.]+)", line)
        if m:
            clocks[m.group(1)] = float(m.group(2))
            continue
        m = re.match(r"set_false_path\s+-(from|to)\s+\[get_ports\s+(?:-quiet\s+)?"
                     r"(?:\{([^}]*)\}|([^\s\]]+))\s*\]", line)
        if m:
            (frm if m.group(1) == "from" else to).update((m.group(2) or m.group(3)).split())
    return {"clocks": clocks, "from": frm, "to": to}
