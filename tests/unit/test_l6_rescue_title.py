"""Lane L6: the bring-up wizard's last step names the real next step in rescue."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src/harness_manager/web/static/js/bringup.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_next_step_title_in_rescue_and_otherwise():
    text = SRC.read_text()
    fn = re.search(r"export function nextTitle.*?\n}\n", text, re.S).group(0).replace("export ", "")
    out = subprocess.run(["node", "-e", fn + """
for (const a of [{rescue: true}, {rescue: false, linuxUp: true}, {}]) console.log(nextTitle(a));"""],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert out == ["Next: power-cycle it, then name it",
                   "Next: claim it, then name it", "Next: name it"]       # twins unchanged
