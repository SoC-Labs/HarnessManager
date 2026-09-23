"""``socharness app``: the web UI in its own window, like a desktop application.

The page is the same one ``socharness ui`` opens in a browser tab. Only the
window differs. The launcher tries, in order:

1. ``$SOCHARNESS_APP_BROWSER``: a Chromium-family executable the user names;
2. pywebview (the optional ``app`` extra): a native window, using WebView2 on
   Windows, WKWebView on macOS, and GTK/Qt WebKit on Linux. It runs in this
   process and returns when the window closes;
3. a Chromium-family browser in app mode (``--app=URL``): Chrome, Edge,
   Chromium or Brave. That gives a window with no tabs, no address bar, and its
   own profile under ``<state_dir>/app-window``, so it is its own taskbar entry.
   Edge ships with Windows, so this needs nothing extra there;
4. the default browser, in a tab (Firefox has no app mode).

Closing the window leaves socharnessd running, because the CLI shares it.
``socharness daemon stop`` stops it.
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

ENV_APP_BROWSER = "SOCHARNESS_APP_BROWSER"
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


def find_app_browser(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The Chromium-family executable to use for app mode, or None."""
    named = os.environ.get(ENV_APP_BROWSER, "").strip()
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
            "--no-first-run", "--no-default-browser-check", "--class=socharness"]


def _try_pywebview(url: str) -> Launched | None:
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return None
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
    named = os.environ.get(ENV_APP_BROWSER, "").strip()
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
        child = popen(app_mode_args(exe, url, profile_dir), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=(os.name != "nt"))
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


__all__: Sequence[str] = ("ENV_APP_BROWSER", "Launched", "app_mode_args", "describe",
                          "find_app_browser", "open_window")
