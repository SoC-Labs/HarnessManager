"""``socharness.gui`` must import, and ``socharness-gui`` must fail cleanly, without PySide6.

These run with or without PySide6 installed: the child process hides it.
"""

from __future__ import annotations

import subprocess
import sys

HIDE_QT = "import sys; sys.modules['PySide6'] = None; "


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", HIDE_QT + code], capture_output=True,
                          text=True, timeout=60)


def test_package_and_demo_engine_import_without_pyside6():
    proc = run("import socharness.gui, socharness.gui.demo_engine; print('ok')")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_gui_main_exits_12_without_pyside6():
    proc = run("import socharness.gui as g; sys.exit(g.main([]))")
    assert proc.returncode == 12
    assert "PySide6 is not installed" in proc.stderr


def test_negative_twin_the_qt_modules_really_are_hidden():
    # Proves the two tests above ran with PySide6 hidden: a Qt module cannot import.
    proc = run("import socharness.gui.app")
    assert proc.returncode != 0
    assert "PySide6" in proc.stderr
