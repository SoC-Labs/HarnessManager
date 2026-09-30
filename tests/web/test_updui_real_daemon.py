"""Lane UPDATE-UI over the REAL daemon: the app-update half of the page (OTA-U) against
``daemon/update_api.py`` and ``update_apply.Applier``, not a simulation.

The daemon runs over ``DemoEngine`` boards with a real ``UpdateService`` whose app updater
points at an install root in ``tmp_path``: the installer's venv registered, and 0.9.0 staged
beside it (a pointer and a python file; nothing is built). The update source is never asked
(the checker is not started under the test server, and no route here fetches a channel), the
trust store is empty, and there is no key. The apply's three outside effects are recorders,
as in ``tests/integration/test_otad_apply_api.py``: the new version's ``--self-test``, the
detached helper, and the daemon's own shutdown, which here only makes ``/health`` report the
new version (the restart the helper would do). Every action on the page is a click; each
behaviour has its negative twin. Nothing reaches a board, a hub, GitHub or a real key.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from harness_manager import __version__, _launch
from harness_manager.demo import BOARD_USB, DemoEngine
from harness_manager.services.update import UpdateService
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.policy import Policy
from harness_manager.services.update.trust import TrustStore
from tests.fakes.t7_board import FakeUv
from tests.fakes.t14_mock_api import real_daemon
from tests.web.conftest import dump_failed_pages

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 1000}
TOKEN = "updui-real"
NEW = "0.9.0"
DEV = "this is a developer install (pip install -e)"


def install_root(tmp_path: Path) -> Path:
    """The installer's venv (pointer ``""``) and ``NEW`` staged beside it."""
    root = tmp_path / "root"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("installer python")
    _launch.register(root, root / "venv", __version__, extras=[], windows=False)
    (root / "versions" / NEW / "bin").mkdir(parents=True)
    (root / "versions" / NEW / "bin" / "python").write_text(f"{NEW} python")
    ptr = json.loads((root / "current.json").read_text())
    ptr["versions"] = {NEW: {"state": "staged", "at": time.time()}}
    (root / "current.json").write_text(json.dumps(ptr))
    return root


class Recorder:
    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.spawned: list[list[str]] = []
        self.shutdowns = 0

    def spawn(self, argv: list[str], env: dict[str, str], log: Path, cwd: Path) -> Any:
        self.spawned.append(argv)
        return type("Proc", (), {"pid": 424242})()

    def self_test(self, python: Path) -> str:
        return ""

    def shutdown(self) -> None:
        # The helper would start the new version on the same port and token: /health says so.
        self.shutdowns += 1
        self.monkeypatch.setattr("harness_manager.daemon.app.__version__", NEW)


class World:
    def __init__(self, tmp_path: Path, monkeypatch, *, dev: str = "", policy: Policy | None = None):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.root = install_root(tmp_path)
        self.rec = Recorder(monkeypatch)
        self.engine = DemoEngine(speed=0.25)
        app_up = AppUpdater(AppLayout(self.root), LocalBusyProbe(self.state), uv="/opt/uv",
                            runner=FakeUv(), python_version="3.11", running_version=__version__,
                            windows=False, state_dir=self.state, dev_install=dev)
        self.svc = UpdateService(self.engine, state_dir=self.state, trust=TrustStore(pinned=()),
                                 token="", app_version=__version__, app_updater=app_up,
                                 policy=policy or Policy())
        self.engine.update = self.svc
        self.server = real_daemon(self.engine, token=TOKEN, state_dir=self.state)

    def __enter__(self) -> World:
        self.server.__enter__()
        d = self.daemon
        d.runtime = {"port": self.server.port, "listen": "127.0.0.1", "log_level": "info",
                     "pack_overrides": {}, "demo": True}
        d.shutdown = self.rec.shutdown
        a = d.update_applier
        a.prefix = str(self.root / "venv")
        a.spawn = self.rec.spawn
        a.self_test = self.rec.self_test
        a.grace_s = 0.05
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.__exit__(*exc)
        self.engine.close_all()

    @property
    def daemon(self) -> Any:
        return self.server.app.state.daemon

    def write(self, name: str, data: dict[str, Any]) -> None:
        path = self.state / "update" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def mark_bad(self, version: str, reason: str) -> None:
        ptr = json.loads((self.root / "current.json").read_text())
        ptr["versions"][version] = {"state": "bad", "reason": reason, "phase": "health",
                                    "at": time.time()}
        (self.root / "current.json").write_text(json.dumps(ptr))


@pytest.fixture
def pages(browser, request):
    made: list[Any] = []
    contexts: list[Any] = []

    def make(world: World, scheme: str = "light"):
        ctx = browser.new_context(viewport=APP, color_scheme=scheme, reduced_motion="reduce")
        contexts.append(ctx)
        page = ctx.new_page()
        page.goto(world.server.ui_url)
        made.append(page)
        return page

    yield make
    dump_failed_pages(request, made)
    for ctx in contexts:
        ctx.close()


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def banner(page):
    return by_id(page, "app-update-banner")


def open_settings(page):
    # FIX-PACK-4: the gear opens General first (then the last section used): go to Updates.
    page.locator('[data-action="settings"]').click()
    page.locator('[data-testid="settings-nav"] [data-settings-section="updates"]').click()
    return by_id(page, "update-settings")


def wait_reloaded(page, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if page.evaluate("window.__updui_loaded !== true"):
                return True
        except Exception:  # noqa: BLE001, S110 - the context goes away during the reload
            pass
        time.sleep(0.1)
    return False


def gdb_busy() -> list[dict[str, Any]]:
    return [{"kind": "gdb", "board_id": BOARD_USB,
             "detail": f"OpenOCD for GDB on {BOARD_USB} (port 3333)"}]


# --- the banner ------------------------------------------------------------------------------------


def test_real_daemon_a_staged_version_raises_the_banner(tmp_path, monkeypatch, pages):
    with World(tmp_path, monkeypatch) as w:
        page = pages(w)
        expect(banner(page)).to_contain_text(f"Harness Manager {NEW} is ready: Restart to update",
                                             timeout=T)
        card = open_settings(page)
        expect(card.locator('[data-testid="settings-running"]')).to_have_text(__version__)
        expect(card.locator('[data-testid="settings-staged"]')).to_contain_text(NEW)
        assert card.locator('[data-testid="dev-install"]').count() == 0


def test_real_daemon_negative_twin_a_developer_install_has_no_banner(tmp_path, monkeypatch, pages):
    with World(tmp_path, monkeypatch, dev=DEV) as w:
        page = pages(w)
        card = open_settings(page)
        expect(card.locator('[data-testid="dev-install"]')).to_contain_text(
            f"Developer install: updates are off ({DEV})", timeout=T)
        assert banner(page).count() == 0


# --- apply: soft busy, the confirm, the overlay, the reload -------------------------------------------


def test_real_daemon_soft_busy_needs_a_confirm_then_the_page_reloads_on_the_new_version(
        tmp_path, monkeypatch, pages):
    with World(tmp_path, monkeypatch) as w:
        w.daemon.soft_busy_probes = [gdb_busy]
        page = pages(w)
        banner(page).locator('[data-action="app-apply"]').click(timeout=T)
        confirm = by_id(page, "apply-confirm")
        expect(confirm.locator('[data-kind="gdb"]')).to_contain_text("OpenOCD for GDB on", timeout=T)
        assert w.rec.spawned == [] and w.daemon.update_applier.state == "idle"
        page.evaluate("window.__updui_loaded = true")
        confirm.locator('[data-action="app-apply-confirm"]').click()
        assert wait_reloaded(page), "the page did not reload onto the new version"
        page.wait_for_selector('[data-testid="daemon-line"]', timeout=T)
        expect(by_id(page, "daemon-line")).to_contain_text(NEW, timeout=T)
        assert len(w.rec.spawned) == 1 and "harness_manager.daemon.update_apply" in " ".join(
            w.rec.spawned[0])
        assert w.rec.shutdowns == 1


def test_real_daemon_negative_twin_not_now_starts_nothing(tmp_path, monkeypatch, pages):
    with World(tmp_path, monkeypatch) as w:
        w.daemon.soft_busy_probes = [gdb_busy]
        page = pages(w)
        banner(page).locator('[data-action="app-apply"]').click(timeout=T)
        by_id(page, "apply-confirm").locator('[data-action="app-apply-cancel"]').click()
        expect(by_id(page, "apply-confirm")).to_have_count(0)
        time.sleep(0.5)
        assert w.rec.spawned == [] and w.rec.shutdowns == 0
        assert w.daemon.update_applier.state == "idle"
        expect(banner(page)).to_be_visible()                    # still offered


# --- how the last apply ended ---------------------------------------------------------------------------


def test_real_daemon_a_rolled_back_apply_shows_the_error_banner(tmp_path, monkeypatch, pages):
    why = f"/health did not report {NEW} within 30 s"
    with World(tmp_path, monkeypatch) as w:
        w.mark_bad(NEW, why)
        w.write("last_apply.json", {"id": "abc123", "from": __version__, "to": NEW,
                                    "result": "rolled-back", "phase": "health", "reason": why,
                                    "at": time.time() - 30, "seconds": 31.0})
        page = pages(w)
        err = by_id(page, "app-rolled-back")
        expect(err).to_contain_text(f"Harness Manager {NEW} failed its health check; back on "
                                    f"{__version__}. It won't be offered again.", timeout=T)
        assert banner(page).count() == 0                          # the bad version is not offered
        card = open_settings(page)
        expect(card.locator(f'[data-testid="bad-versions"] tr[data-version="{NEW}"]')).to_contain_text(why)


def test_real_daemon_negative_twin_an_applied_update_says_updated_not_failed(tmp_path, monkeypatch, pages):
    with World(tmp_path, monkeypatch) as w:
        w.write("last_apply.json", {"id": "def456", "from": "0.0.9", "to": __version__,
                                    "result": "applied", "phase": "", "reason": "",
                                    "at": time.time() - 20, "seconds": 4.0})
        page = pages(w)
        expect(by_id(page, "app-updated")).to_contain_text(f"Updated to Harness Manager {__version__}",
                                                           timeout=T)
        assert by_id(page, "app-rolled-back").count() == 0


# --- Settings and the admin policy ----------------------------------------------------------------------


def test_real_daemon_the_admin_policy_locks_its_settings(tmp_path, monkeypatch, pages):
    policy = Policy(path="/etc/harness-manager/policy.toml", self_update="notify", channel="stable")
    with World(tmp_path, monkeypatch, policy=policy) as w:
        page = pages(w)
        card = open_settings(page)
        expect(card.locator('[data-testid="policy-note"]')).to_contain_text(
            "/etc/harness-manager/policy.toml", timeout=T)
        for sel in ('[data-testid="set-channel"] button[data-value="beta"]',
                    '[data-testid="set-channel"] button[data-value="dev"]',
                    '[data-testid="set-auto"] button[data-value="stage"]'):
            expect(card.locator(sel)).to_be_disabled()
        expect(card.locator('[data-testid="effective-why"]')).to_contain_text("allows at most 'notify'")
        card.locator('[data-testid="set-auto"] button[data-value="off"]').click()
        expect(card.locator('[data-testid="set-auto"] button[data-value="off"]')).to_have_attribute(
            "aria-pressed", "true", timeout=T)
        saved = json.loads((w.state / "update" / "settings.json").read_text())
        assert saved == {"channel": "", "auto": "off"}


def test_real_daemon_negative_twin_without_a_policy_the_channel_is_the_users(tmp_path, monkeypatch, pages):
    with World(tmp_path, monkeypatch) as w:
        page = pages(w)
        card = open_settings(page)
        beta = card.locator('[data-testid="set-channel"] button[data-value="beta"]')
        expect(beta).to_be_enabled(timeout=T)
        assert card.locator('[data-testid="policy-note"]').count() == 0
        beta.click()
        expect(beta).to_have_attribute("aria-pressed", "true", timeout=T)
        saved = json.loads((w.state / "update" / "settings.json").read_text())
        assert saved["channel"] == "beta"


# --- SMALL-4: next_check and last_check.notes, from the real route ---------------------------------


def test_real_daemon_the_card_shows_the_checkers_next_check_and_the_notes(tmp_path, monkeypatch,
                                                                         pages):
    from tests.web.test_updui_browser import NOTES, next_check_text

    with World(tmp_path, monkeypatch) as w:
        at = time.time() + 3 * 3600
        w.daemon.update_checker.next_at = at                 # the timer (not started here)
        w.write("last_check.json", {"at": time.time() - 30, "mode": "stage", "interval_s": 21600,
                                    "error": "", "channel": "stable", "serial": 7,
                                    "available": NEW, "staged": True, "notes": NOTES})
        page = pages(w)
        card = open_settings(page)
        expect(card.locator('[data-testid="next-check"]')).to_have_text(next_check_text(at),
                                                                          timeout=T)
        text = card.locator('[data-testid="last-check-notes-text"]')
        expect(text).to_be_hidden()
        card.locator('[data-action="last-check-notes"]').click()
        expect(text).to_have_text(NOTES)
        # the twin: no timer runs: the card estimates, as before
        w.daemon.update_checker.next_at = None
        page.reload()
        card = open_settings(page)
        expect(card.locator('[data-testid="next-check"]')).not_to_contain_text("next check at",
                                                                               timeout=T)
        expect(card.locator('[data-testid="next-check"]')).to_contain_text("then every 6 h")
