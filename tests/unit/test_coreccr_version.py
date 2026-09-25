"""CORE-CCR (CCR OTA-R): one version source, ``harness_manager.__version__``.

``pyproject.toml`` has a dynamic version that setuptools reads from that attribute;
``CHANGELOG.md`` is a checked mirror. The release tool's preconditions (``tools/release``)
run on the OTA-R fake checkout (``tests/fakes/otar_release.py``: real git, no network).
Every check has a negative twin. The wheel that setuptools really builds is checked in
``tests/integration/test_coreccr_wheel_version.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import harness_manager
from tests.fakes.otar_release import make_repo
from tools.release.app import (
    VERSION_ATTR,
    check_preconditions,
    read_facts,
    version_source_problem,
)
from tools.release.common import ReleaseError

ROOT = Path(__file__).resolve().parents[2]


def test_this_checkout_reads_its_version_from___version__():
    facts = read_facts(ROOT)
    assert VERSION_ATTR == "harness_manager.__version__"
    assert version_source_problem(facts) == ""
    assert facts.init_version == harness_manager.__version__ and facts.pyproject_version == ""
    assert facts.changelog_version == harness_manager.__version__, "CHANGELOG mirrors it"


def test_the_editable_install_reports___version__():
    from importlib.metadata import version

    assert version("harness-manager") == harness_manager.__version__


def test_the_release_tool_takes_the_version_from___version__(tmp_path):
    repo = make_repo(tmp_path / "hm", "0.2.0")
    pre = check_preconditions(repo, None, allow_dirty=False)
    assert pre.version == "0.2.0" and pre.facts.init_version == "0.2.0"
    assert pre.facts.pyproject_dynamic and pre.facts.pyproject_version == ""


def test_twin_the_release_tool_refuses_a_changelog_that_differs(tmp_path):
    repo = make_repo(tmp_path / "hm", "0.2.0", changelog_version="0.1.9")
    with pytest.raises(ReleaseError) as exc:
        check_preconditions(repo, None, allow_dirty=False)
    assert "CHANGELOG.md (newest heading) says 0.1.9" in exc.value.message
    assert "__version__" in exc.value.hint


def test_twin_the_release_tool_refuses_a_pyproject_with_a_version_of_its_own(tmp_path):
    repo = make_repo(tmp_path / "hm", "0.2.0", pyproject_version="0.2.0")
    with pytest.raises(ReleaseError) as exc:
        check_preconditions(repo, None, allow_dirty=False)
    assert "pyproject.toml has its own version (0.2.0)" in exc.value.message
    assert 'dynamic = ["version"]' in exc.value.hint


def test_twin_a_dynamic_version_read_from_elsewhere_is_refused(tmp_path):
    repo = make_repo(tmp_path / "hm", "0.2.0")
    pp = repo / "pyproject.toml"
    pp.write_text(pp.read_text().replace('"harness_manager.__version__"',
                                         '"harness_manager.VERSION"'))
    problem = version_source_problem(read_facts(repo))
    assert "does not read its version from harness_manager.__version__" in problem
    assert "harness_manager.VERSION" in problem


def test_the_installer_checks_read___version__():
    smoke = (ROOT / "scripts" / "smoke_install.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "src/harness_manager/__init__.py" in smoke and "^__version__" in smoke
    assert "__init__.py -Pattern '^__version__" in ci
    for text in (smoke, ci):
        assert "^version = " not in text, "a version read from pyproject.toml, which has none"
