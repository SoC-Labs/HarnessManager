"""Engine acquisition: the real Engine, the demo engine, or a clear exit 12."""

from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from socharness.gui import app as gui_app  # noqa: E402
from socharness.gui.demo_engine import DemoEngine  # noqa: E402

pytestmark = pytest.mark.gui


def test_fake_uses_the_demo_engine():
    engine, rc, msg = gui_app.acquire_engine(fake=True)
    try:
        assert isinstance(engine, DemoEngine) and rc == 0 and msg == ""
    finally:
        engine.close_all()


def test_without_fake_the_real_engine_is_used():
    from socharness.engine import Engine

    engine, rc, _ = gui_app.acquire_engine()
    assert isinstance(engine, Engine) and rc == 0
    engine.close_all()


def test_a_stand_in_engine_module_is_picked_up(monkeypatch):
    mod = types.ModuleType("socharness.engine")

    class Engine:
        pass

    mod.Engine = Engine
    monkeypatch.setitem(sys.modules, "socharness.engine", mod)
    engine, rc, _ = gui_app.acquire_engine()
    assert isinstance(engine, Engine) and rc == 0


def test_negative_twin_engine_not_installed_is_exit_12_with_a_dialog(qapp, monkeypatch):
    monkeypatch.setitem(sys.modules, "socharness.engine", None)
    engine, rc, msg = gui_app.acquire_engine()
    assert engine is None and rc == 12 and "not installed in this build" in msg

    shown: list[str] = []
    monkeypatch.setattr(gui_app.QMessageBox, "critical",
                        staticmethod(lambda parent, title, text: shown.append(text)))
    assert gui_app.main([]) == 12
    assert shown and "engine is not installed in this build" in shown[0]
