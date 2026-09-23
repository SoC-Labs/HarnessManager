"""Web UI test fixtures (Team T14): the daemon under the page, and a headless system Chrome.

- ``daemon``: the REAL socharnessd app (Team T13, ``socharness.daemon.app``) over
  ``DemoEngine``. A test marked ``@pytest.mark.mock_too`` also runs over the T14 mock
  (``tests/fakes/t14_mock_api.py``), which keeps the mock honest in the browser too.
- ``browser``: the SYSTEM Chrome through Playwright. Skipped, with the reason, when
  Playwright is not installed or no Chrome/Chromium binary is found; nothing is ever
  downloaded. ``SOCHARNESS_TEST_CHROME`` picks a binary.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from socharness.gui.demo_engine import DemoEngine
from tests.fakes.t14_mock_api import MockDaemon, real_daemon

SCREENSHOTS = Path(__file__).resolve().parent / "screenshots"
CHROME_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                     "/usr/bin/chromium-browser", "/usr/bin/chromium")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "browser: drives a headless system Chrome through Playwright (T14 web UI)")
    config.addinivalue_line(
        "markers", "mock_too: also run this browser test over the T14 mock socharnessd")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "daemon" in metafunc.fixturenames:
        servers = ["socharnessd"]
        if metafunc.definition.get_closest_marker("mock_too"):
            servers.append("mock")
        metafunc.parametrize("daemon", servers, indirect=True)


def chrome_binary() -> str | None:
    given = os.environ.get("SOCHARNESS_TEST_CHROME")
    if given:
        return given if Path(given).exists() else None
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return shutil.which("google-chrome") or shutil.which("chromium")


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    binary = chrome_binary()
    if binary is None:
        pytest.skip("no system Chrome/Chromium found (set SOCHARNESS_TEST_CHROME)")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=binary, headless=True,
                                  args=["--no-first-run", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001 - a host that cannot run Chrome skips
            pytest.skip(f"cannot launch {binary}: {exc}")
        yield b
        b.close()


@pytest.fixture
def engine() -> Iterator[DemoEngine]:
    eng = DemoEngine(speed=0.25)
    yield eng
    eng.close_all()


@pytest.fixture
def daemon(request, engine, tmp_path):
    kind = getattr(request, "param", "socharnessd")
    if kind == "mock":
        server = MockDaemon(engine, token="t14-browser")
    else:
        server = real_daemon(engine, token="t14-browser", state_dir=tmp_path / "socharnessd")
    with server as d:
        yield d


@pytest.fixture
def page_factory(browser, daemon):
    """``page_factory(scheme="light")`` -> a 1280x800 page on the UI, with errors collected."""
    contexts = []

    def make(scheme: str = "light", *, url: str | None = None, width: int = 1280,
             height: int = 800):
        ctx = browser.new_context(viewport={"width": width, "height": height},
                                  color_scheme=scheme, reduced_motion="reduce")
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error"
                and "status of 4" not in m.text and "status of 5" not in m.text else None)
        page.goto(url or daemon.ui_url)
        return page

    yield make
    for ctx in contexts:
        ctx.close()


@pytest.fixture(scope="session")
def screenshots() -> Path:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    return SCREENSHOTS
