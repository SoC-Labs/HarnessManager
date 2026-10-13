"""W0-A: the pyverify import scan also sees function-level imports."""

from __future__ import annotations

from harness_manager_mps3 import selfcheck


def test_function_level_import_is_found_and_checked(tmp_path):
    (tmp_path / "m.py").write_text(
        "from pyverify.client import ShellClient\n"
        "def f():\n    from pyverify.client import NoSuchNameZ\n"
        "    import pyverify.nosuchmodz\n")
    need = selfcheck.needed(tmp_path)
    assert need["pyverify.client"] == {"ShellClient", "NoSuchNameZ"}
    assert "pyverify.nosuchmodz" in need
    assert selfcheck.missing(tmp_path) == ["pyverify.client.NoSuchNameZ", "pyverify.nosuchmodz"]


def test_the_pack_itself_passes():
    assert selfcheck.missing() == []
