"""Lane L6: claim times show in the viewer's local time."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "src/harness_manager/web/static/js"


def _node(src: str, tz: str) -> str:
    return subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True,
                          env={"TZ": tz, "PATH": "/usr/bin:/bin:/usr/local/bin"}).stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_claim_times_show_in_local_time():
    text = (JS / "sections/claim.js").read_text()
    fn = re.search(r"export function localStamp.*?\n}\n", text, re.S).group(0).replace("export ", "")
    out = _node(fn + 'console.log(localStamp("2026-10-13T23:30:00Z"))', "Asia/Kolkata")
    assert out == "2026-10-14 05:00"                        # UTC+5:30, next day
    # twin: a stamp that is not a date is shown as given, trimmed, never "NaN"
    assert _node(fn + 'console.log(localStamp("on an unknown day"))', "UTC") == "on an unknown da"
    # and the page no longer slices the UTC text itself
    assert "slice(0, 16)" not in text.replace("String(iso).slice(0, 16)", "")
