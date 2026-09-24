"""Q3 install: `harness-manager app --with-app` on Linux, where the venv has no GTK or Qt.

pywebview on Linux needs PyGObject (``gi``) or Qt (``qtpy``); the installer's venv has
neither. pywebview then printed two tracebacks before it failed. The launcher now skips
it quietly and opens the Chrome/Chromium app window.
"""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from harness_manager.web import window


class FakeWebview(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("webview")
        self.created: list[str] = []

    def create_window(self, title, url, **_kw):
        self.created.append(url)

    def start(self):
        return None


@pytest.fixture
def fake_webview(monkeypatch):
    mod = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", mod)
    return mod


def _find_spec(present: set[str]):
    real = importlib.util.find_spec

    def find_spec(name, *a, **kw):
        if name in ("gi", "qtpy"):
            return object() if name in present else None
        return real(name, *a, **kw)

    return find_spec


def test_linux_without_gtk_or_qt_skips_pywebview_quietly(monkeypatch, fake_webview):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec(set()))
    got = window._try_pywebview("http://127.0.0.1:1/")
    assert got is not None and got.how == "none"
    assert "GTK or Qt" in got.detail
    assert fake_webview.created == []          # never asked pywebview for a window


@pytest.mark.parametrize("binding", ["gi", "qtpy"])
def test_linux_with_a_binding_uses_pywebview(monkeypatch, fake_webview, binding):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec({binding}))
    got = window._try_pywebview("http://127.0.0.1:1/")
    assert got is not None and got.how == "pywebview"
    assert fake_webview.created == ["http://127.0.0.1:1/"]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_other_systems_do_not_need_the_bindings(monkeypatch, fake_webview, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec(set()))
    assert window.pywebview_backend_missing() is None
    assert window._try_pywebview("http://127.0.0.1:1/").how == "pywebview"


def test_open_window_falls_back_to_app_mode(monkeypatch, fake_webview, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec(set()))
    monkeypatch.delenv(window.ENV_APP_BROWSER, raising=False)
    started: list[list[str]] = []

    def popen(argv, **_kw):
        started.append(argv)
        return types.SimpleNamespace(pid=4242)

    got = window.open_window("http://127.0.0.1:1/", tmp_path / "profile", popen=popen,
                             which=lambda n: "/usr/bin/chromium" if n == "chromium" else None,
                             browser_open=lambda _u: pytest.fail("no tab: app mode exists"))
    assert got.how == "app-mode" and got.detail == "/usr/bin/chromium"
    assert started and started[0][0] == "/usr/bin/chromium"
    assert "--class=harness-manager" in started[0]   # the menu entry's StartupWMClass
