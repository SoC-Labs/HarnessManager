"""``harness-manager app``'s window launcher (web/window.py): which window it opens, and the fallbacks.

No real browser or window starts here: ``popen``, ``which`` and ``browser_open``
are injected, and pywebview is replaced in ``sys.modules``.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from harness_manager.web import window

URL = "http://127.0.0.1:41411/#token=abc"


@pytest.fixture(autouse=True)
def _no_env_and_no_pywebview(monkeypatch):
    monkeypatch.delenv(window.ENV_APP_BROWSER, raising=False)
    monkeypatch.setitem(sys.modules, "webview", None)       # import webview -> ImportError


def which_of(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.envs.append(kwargs.get("env") or {})
        return types.SimpleNamespace(pid=4242)


def test_app_mode_uses_the_first_chromium_on_path_with_its_own_profile(tmp_path: Path):
    rec = Recorder()
    got = window.open_window(URL, tmp_path / "prof", popen=rec,
                             which=which_of("chromium-browser", "google-chrome"))
    assert (got.how, got.detail, got.pid) == ("app-mode", "/usr/bin/google-chrome", 4242)
    (args,) = rec.calls
    assert args[0] == "/usr/bin/google-chrome" and f"--app={URL}" in args
    assert f"--user-data-dir={tmp_path / 'prof'}" in args and (tmp_path / "prof").is_dir()


def test_the_environment_names_the_browser(tmp_path: Path, monkeypatch):
    exe = tmp_path / "my-chrome"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setenv(window.ENV_APP_BROWSER, str(exe))
    rec = Recorder()
    got = window.open_window(URL, tmp_path / "prof", popen=rec, which=which_of("google-chrome"))
    assert got.how == "app-mode" and rec.calls[0][0] == str(exe)


def test_negative_twin_a_bad_env_browser_falls_back_to_a_tab_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv(window.ENV_APP_BROWSER, str(tmp_path / "missing"))
    opened: list[str] = []
    got = window.open_window(URL, tmp_path / "prof", popen=Recorder(), which=which_of(),
                             browser_open=lambda u: opened.append(u) or True)
    assert got.how == "browser-tab" and opened == [URL]
    assert "is not an executable" in got.detail and "opened a browser tab" in got.detail


def test_no_chromium_opens_a_tab(tmp_path: Path):
    opened: list[str] = []
    got = window.open_window(URL, tmp_path / "prof", popen=Recorder(), which=which_of(),
                             browser_open=lambda u: opened.append(u) or True)
    assert got.how == "browser-tab" and opened == [URL]


def test_nothing_at_all_is_reported_not_hidden(tmp_path: Path):
    got = window.open_window(URL, tmp_path / "prof", popen=Recorder(), which=which_of(),
                             browser_open=lambda u: False)
    assert got.how == "none" and "no browser could be opened" in got.detail


def _fake_webview(monkeypatch, *, fail: bool) -> list:
    seen: list = []
    mod = types.ModuleType("webview")
    mod.create_window = lambda title, url, **kw: seen.append((title, url))

    def start():
        if fail:
            raise RuntimeError("no GUI backend")
        seen.append("started")

    mod.start = start
    monkeypatch.setitem(sys.modules, "webview", mod)
    return seen


def test_pywebview_gives_a_native_window_when_installed(tmp_path: Path, monkeypatch):
    seen = _fake_webview(monkeypatch, fail=False)
    rec = Recorder()
    got = window.open_window(URL, tmp_path / "prof", popen=rec, which=which_of("google-chrome"))
    assert got.how == "pywebview" and seen == [(window.TITLE, URL), "started"]
    assert rec.calls == []


def test_negative_twin_pywebview_without_a_backend_falls_back_to_app_mode(tmp_path, monkeypatch):
    _fake_webview(monkeypatch, fail=True)
    got = window.open_window(URL, tmp_path / "prof", popen=Recorder(),
                             which=which_of("google-chrome"))
    assert got.how == "app-mode"


def test_no_native_skips_pywebview(tmp_path: Path, monkeypatch):
    seen = _fake_webview(monkeypatch, fail=False)
    got = window.open_window(URL, tmp_path / "prof", native=False, popen=Recorder(),
                             which=which_of("google-chrome"))
    assert got.how == "app-mode" and seen == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the D-Bus isolation is Linux-only")
def test_the_app_window_runs_without_the_desktop_session_bus(tmp_path: Path, monkeypatch):
    # A ThinLinc desktop's session bus left Chrome's app window blank (2026-09-23).
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:abstract=/tmp/dbus-x")
    monkeypatch.delenv(window.ENV_KEEP_DBUS, raising=False)
    rec = Recorder()
    window.open_window(URL, tmp_path / "prof", popen=rec, which=which_of("google-chrome"))
    assert rec.envs[0]["DBUS_SESSION_BUS_ADDRESS"] == "disabled:"
    assert "--password-store=basic" in rec.calls[0]
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:abstract=/tmp/dbus-x"   # ours untouched


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the D-Bus isolation is Linux-only")
def test_negative_twin_keep_dbus_keeps_the_bus(monkeypatch):
    env = window.app_mode_env({"DBUS_SESSION_BUS_ADDRESS": "unix:x", window.ENV_KEEP_DBUS: "1"})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:x"
