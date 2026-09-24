#!/usr/bin/env python3
"""Spike (lane KIT-GUIDE): render docs/design/build_rm.tcl.template with KEY=VALUE pairs.

Not wired into Harness Manager. The real renderer would take its values from the
kit (static_id, USERCODE, part, RP instance, boundary bits, Vivado version) and
from the user's design; this one takes them on the command line so the template
can be run by hand in Vivado.

    render_build_rm.py OUT.tcl KEY=VALUE ...

A placeholder left unset renders as an empty value, except the few below that
have defaults.
"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "design" / "build_rm.tcl.template"
DEFAULTS = {
    "JOBS": "2",
    "STOP_AFTER": "bitstream",
    "ALLOW_TIMING_FAIL": "0",
    "CLEARING_MAX": "262144",
    "HM_VERSION": "spike",
}


def render(values: dict[str, str]) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    vals = {**DEFAULTS, "GENERATED_AT": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"), **values}
    for k, v in vals.items():
        if "{" in v or "}" in v:
            raise SystemExit(f"{k}: braces are not allowed in a value ({v!r})")
    out = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: vals.get(m.group(1), ""), text)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    values = {}
    for arg in argv[2:]:
        k, _, v = arg.partition("=")
        values[k] = v
    Path(argv[1]).write_text(render(values), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
