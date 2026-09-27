"""``harness-manager app``: the web UI in its own window, like a desktop application.

The page is the same one ``harness-manager ui`` opens in a browser tab. Only the
window differs. The launcher tries, in order:

1. the setting ``general.app_browser`` (``$HARNESS_MANAGER_APP_BROWSER``, then the Settings
   menu / ``settings.toml``): a Chromium-family executable the user names;
2. pywebview (the optional ``app`` extra): a native window, using WebView2 on
   Windows, WKWebView on macOS, and GTK/Qt WebKit on Linux. It runs in this
   process and returns when the window closes;
3. a Chromium-family browser in app mode (``--app=URL``): Chrome, Edge,
   Chromium or Brave. That gives a window with no tabs, no address bar, and its
   own profile under ``<state_dir>/app-window``, so it is its own taskbar entry.
   Edge ships with Windows, so this needs nothing extra there;
4. the default browser, in a tab (Firefox has no app mode).

Closing the window leaves harness-manager-daemon running, because the CLI shares it.
``harness-manager daemon stop`` stops it.

On Linux the app window starts WITHOUT the desktop's D-Bus session bus, and with
``--password-store=basic``. On a ThinLinc (Xvnc) desktop, Chrome attached to the
session bus mapped a window but never loaded the page: no request reached the
daemon, and the window stayed blank. With the bus disabled, the same command
rendered the page (reproduced on a private Xvnc, 2026-09-23). The app window uses
nothing on that bus. ``HARNESS_MANAGER_APP_KEEP_DBUS=1`` keeps the bus.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ENV_APP_BROWSER = "HARNESS_MANAGER_APP_BROWSER"
ENV_KEEP_DBUS = "HARNESS_MANAGER_APP_KEEP_DBUS"
TITLE = "Harness Manager"
WINDOW_SIZE = (1440, 900)

#: Chromium-family executables on PATH, most common first.
PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "microsoft-edge", "microsoft-edge-stable", "brave-browser", "msedge", "chrome")


def _platform_paths() -> list[Path]:
    if sys.platform == "darwin":
        apps = ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser")
        return [Path(f"/Applications/{a}.app/Contents/MacOS/{a}") for a in apps]
    if sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        tails = (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                 r"BraveSoftware\Brave-Browser\Application\brave.exe")
        return [Path(r) / t for r in roots if r for t in tails]
    return []


@dataclass(frozen=True)
class Launched:
    how: str            # "pywebview" | "app-mode" | "browser-tab" | "none"
    detail: str         # the executable, or why nothing opened
    pid: int | None = None


def named_app_browser() -> str:
    """The setting ``general.app_browser``: ``ENV_APP_BROWSER``, then the Settings menu /
    ``settings.toml`` (lane SET-WIRE); "" when neither names one. A settings file that
    cannot be read names none (the window still opens)."""
    try:
        from harness_manager.settings import runtime

        return str(runtime.value("general.app_browser") or "").strip()
    except Exception:  # noqa: BLE001 - never worth failing to open the window
        return os.environ.get(ENV_APP_BROWSER, "").strip()


def find_app_browser(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The Chromium-family executable to use for app mode, or None."""
    named = named_app_browser()
    if named:
        return named if (Path(named).is_file() or which(named)) else None
    for name in PATH_NAMES:
        found = which(name)
        if found:
            return found
    for path in _platform_paths():
        if path.is_file():
            return str(path)
    return None


def app_mode_args(exe: str, url: str, profile_dir: Path) -> list[str]:
    """The command line for a Chromium-family browser in app mode, with its own profile."""
    w, h = WINDOW_SIZE
    return [exe, f"--app={url}", f"--user-data-dir={profile_dir}", f"--window-size={w},{h}",
            "--no-first-run", "--no-default-browser-check", "--class=harness-manager",
            "--password-store=basic"]


def app_mode_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The app window's environment: on Linux, no desktop session bus (see the docstring)."""
    env = dict(os.environ if environ is None else environ)
    if sys.platform.startswith("linux") and env.get(ENV_KEEP_DBUS, "") in ("", "0"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"
    return env


def pywebview_backend_missing() -> str | None:
    """Why pywebview cannot open a window here, or None.

    Install lane Q3 (product code, for the install): on Linux pywebview needs GTK
    (``gi``, PyGObject) or Qt (``qtpy``) bindings, which the installer's venv does not
    have. pywebview then printed two tracebacks before failing, on every ``app``.
    Skip it quietly; the Chrome/Chromium app window is the Linux path.
    """
    if not sys.platform.startswith("linux"):
        return None
    import importlib.util

    if importlib.util.find_spec("gi") or importlib.util.find_spec("qtpy"):
        return None
    return "pywebview has no GTK or Qt bindings here"


def _try_pywebview(url: str) -> Launched | None:
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return None
    missing = pywebview_backend_missing()
    if missing:
        return Launched("none", missing)
    try:
        webview.create_window(TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1])
        webview.start()                      # blocks until the window closes
    except Exception as exc:  # noqa: BLE001 - no GUI backend: fall back to app mode
        return Launched("none", f"pywebview could not open a window: {exc}")
    return Launched("pywebview", "native window (closed)")


def open_window(url: str, profile_dir: Path, *, native: bool = True,
                popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                which: Callable[[str], str | None] = shutil.which,
                browser_open: Callable[[str], bool] = webbrowser.open) -> Launched:
    """Open ``url`` as an application window; say how (see the module docstring)."""
    named = named_app_browser()
    notes: list[str] = []
    if native and not named:
        got = _try_pywebview(url)
        if got is not None and got.how == "pywebview":
            return got
        if got is not None:
            notes.append(got.detail)
    exe = find_app_browser(which)
    if exe is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        child = popen(app_mode_args(exe, url, profile_dir), env=app_mode_env(),
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, start_new_session=(os.name != "nt"))
        return Launched("app-mode", exe, getattr(child, "pid", None))
    if named:
        notes.append(f"${ENV_APP_BROWSER}={named} is not an executable")
    try:
        if browser_open(url):
            notes.append("no Chrome, Edge, Chromium or Brave found: opened a browser tab")
            return Launched("browser-tab", "; ".join(notes))
    except webbrowser.Error:
        pass
    notes.append("no browser could be opened")
    return Launched("none", "; ".join(notes))


def describe(launched: Launched) -> str:
    return {"pywebview": "opened a native window",
            "app-mode": f"opened an app window ({Path(launched.detail).name})",
            "browser-tab": launched.detail,
            "none": launched.detail}[launched.how]


__all__: Sequence[str] = ("ENV_APP_BROWSER", "ENV_KEEP_DBUS", "Launched", "app_mode_args",
                          "app_mode_env", "describe",
                          "find_app_browser", "open_window")
