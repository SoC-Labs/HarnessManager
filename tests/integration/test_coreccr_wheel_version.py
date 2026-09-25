"""CORE-CCR (CCR OTA-R): the wheel setuptools really builds takes its version from
``harness_manager.__version__``.

A copy of the checkout (never the checkout itself: no ``build/`` or ``*.egg-info`` here) gets
a sentinel ``__version__``, and the release tool's own builder (``tools.release.app.
build_wheel``: ``python -m build --wheel``) builds it under pytest's temp dir. The wheel's
METADATA must say the sentinel: before OTA-R it said pyproject.toml's static version. The
negative twins (a CHANGELOG or a pyproject that disagrees is refused) are in
``tests/unit/test_coreccr_version.py``. The build needs the package index for setuptools, so
the test skips, with the reason, when it is unreachable (as the L5 packaging test does).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import harness_manager
from tests.integration.test_l5_packaging import IGNORE, ROOT, index_unreachable
from tools.release.app import build_wheel, read_facts, version_source_problem, wheel_version

pytestmark = [pytest.mark.slow, pytest.mark.packaging, pytest.mark.timeout(600)]

SENTINEL = "9.8.7"


def test_a_built_wheel_says_the_version___version___says(tmp_path: Path):
    pytest.importorskip("build", reason="the 'build' package is not installed (the dev extra)")
    reason = index_unreachable()
    if reason:
        pytest.skip(reason)
    assert harness_manager.__version__ != SENTINEL
    src = tmp_path / "src"
    shutil.copytree(ROOT, src, ignore=IGNORE)
    init = src / "src" / "harness_manager" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    line = f'__version__ = "{harness_manager.__version__}"'
    assert text.count(line) == 1
    init.write_text(text.replace(line, f'__version__ = "{SENTINEL}"'), encoding="utf-8")
    facts = read_facts(src)
    assert version_source_problem(facts) == "" and facts.init_version == SENTINEL

    wheel = build_wheel(python=sys.executable)(src, tmp_path / "dist", 1_700_000_000)
    assert wheel.name == f"harness_manager-{SENTINEL}-py3-none-any.whl"
    assert wheel_version(wheel) == SENTINEL, "the wheel's METADATA follows __version__"
