"""Web UI test fixtures (Team T14): the daemon under the page, and a headless system Chrome.

- ``daemon``: the REAL harness-manager-daemon app (Team T13, ``harness_manager.daemon.app``) over
  ``DemoEngine``. A test marked ``@pytest.mark.mock_too`` also runs over the T14 mock
  (``tests/fakes/t14_mock_api.py``), which keeps the mock honest in the browser too.
- ``browser``: the SYSTEM Chrome through Playwright. Skipped, with the reason, when
  Playwright is not installed or no Chrome/Chromium binary is found; nothing is ever
  downloaded. ``HARNESS_MANAGER_TEST_CHROME`` picks a binary.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.demo import DemoEngine
from tests.fakes.t14_mock_api import MockDaemon, real_daemon

SCREENSHOTS = Path(__file__).resolve().parent / "screenshots"
CHROME_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                     "/usr/bin/chromium-browser", "/usr/bin/chromium")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "browser: drives a headless system Chrome through Playwright (T14 web UI)")
    config.addinivalue_line(
        "markers", "mock_too: also run this browser test over the T14 mock harness-manager-daemon")
    config.addinivalue_line(
        "markers", "week_plan(*modules, sim=False): needs the week-plan routes of those daemon "
                   "extension modules; runs over the mock, and over the real daemon too when "
                   "they have landed (HARNESS_MANAGER_WEB_WEEK_REAL=0: mock only). sim=True: the test "
                   "scripts its scenario through the mock's WeekPlanSim (a meter, a hub, an "
                   "update channel), so it runs over the mock only")


def landed(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(f"harness_manager.daemon.{module}") is not None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "daemon" in metafunc.fixturenames:
        week = metafunc.definition.get_closest_marker("week_plan")
        if week is not None:
            # L1/L2/L4 landed these routes separately: the mock always has them; the real
            # daemon joins once every module the test needs is in the tree. That was opt-in
            # (=1) while the lanes ran, so CI never ran these over the real daemon; all four
            # modules are on main now (Q1 2026-09-24), so it is on unless =0.
            servers = ["mock"]
            if (os.environ.get("HARNESS_MANAGER_WEB_WEEK_REAL", "1") != "0"
                    and not week.kwargs.get("sim") and all(landed(m) for m in week.args)):
                servers.append("harness-manager-daemon")
        else:
            servers = ["harness-manager-daemon"]
            if metafunc.definition.get_closest_marker("mock_too"):
                servers.append("mock")
        metafunc.parametrize("daemon", servers, indirect=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def dump_failed_pages(request, pages) -> None:
    """A failed browser test leaves each page's screenshot and state under screenshots/failures."""
    import json
    import re

    rep = getattr(request.node, "rep_call", None)
    if rep is None or not rep.failed:
        return
    out = SCREENSHOTS / "failures"
    out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)[-120:]
    for i, page in enumerate(pages):
        try:
            page.screenshot(path=str(out / f"{stem}-{i}.png"))
            state = page.evaluate("window.__harness_managerState ? window.__harness_managerState() : null")
            (out / f"{stem}-{i}.json").write_text(json.dumps(state, indent=1, default=str))
        except Exception:  # noqa: BLE001, S112 - a report must never mask the failure
            continue


def chrome_binary() -> str | None:
    given = os.environ.get("HARNESS_MANAGER_TEST_CHROME")
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
        pytest.skip("no system Chrome/Chromium found (set HARNESS_MANAGER_TEST_CHROME)")
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
    kind = getattr(request, "param", "harness-manager-daemon")
    if kind == "mock":
        server = MockDaemon(engine, token="t14-browser")
    else:
        server = real_daemon(engine, token="t14-browser", state_dir=tmp_path / "harness-manager-daemon")
    with server as d:
        yield d


@pytest.fixture
def page_factory(browser, daemon, request):
    """``page_factory(scheme="light")`` -> a 1280x800 page on the UI, with errors collected."""
    contexts, pages = [], []

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
        pages.append(page)
        return page

    yield make
    dump_failed_pages(request, pages)
    for ctx in contexts:
        ctx.close()


@pytest.fixture(scope="session")
def screenshots() -> Path:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    return SCREENSHOTS
