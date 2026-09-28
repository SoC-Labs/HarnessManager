"""FIX-PACK-2 item 6: the service shows the tool variables it STARTED with.

The finding: on POSIX ``control._spawn`` hands the service the whole environment of the process
that started it, and ``app``/``ui`` reuse a running service, so a ``HARNESS_MANAGER_OPENOCD``
exported in an old terminal outranks the user's ``settings.toml`` for the service's life,
even once the user's own shell no longer has it. The service now keeps what it started with
and says so: ``GET /api/v1/daemon/env``, ``harness-manager daemon status``, Settings >
Advanced, and a one-line warning in the app when a Tools variable hides the user's setting.

The process tests start a real service with the variable set in the STARTER's environment,
then drop it from this (the user's) environment before asking: the service still has it, and
says so. Each check has a negative twin.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.cli.cmd_daemon import _env_lines
from harness_manager.daemon import service_env as SE
from harness_manager.daemon.app import create_app
from harness_manager.demo import DemoEngine
from harness_manager.settings import runtime
from tests.fakes.t13_daemon import run_cli, state_dir, stop_state_dir

STALE = "/home/me/SoCLabs/soclabs-openocd/install/bin/openocd"     # no remote_bitbang
MINE = "/home/me/opt/xpack-openocd/bin/openocd"
TOKEN = "fp2-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def write_settings(text: str) -> None:
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.toml").write_text(text)
    runtime.refresh()


# --- what is captured, and what is never shown ---------------------------------------------------


def test_capture_takes_harness_manager_and_tool_variables_only():
    env = {"HARNESS_MANAGER_OPENOCD": STALE, "XILINX_VIVADO": "/opt/Xilinx/Vivado/2024.1",
           "PATH": "/usr/bin", "HOME": "/home/me", "SOCLABS_OPENOCD_ROOT": "/x"}
    got = SE.capture(env)
    assert got == {"HARNESS_MANAGER_OPENOCD": STALE, "XILINX_VIVADO": "/opt/Xilinx/Vivado/2024.1"}


def test_negative_twin_a_secret_is_never_shown():
    env = {"HARNESS_MANAGER_GITHUB_TOKEN": "ghp_secret", "FPGAHUB_TOKEN": "hub-secret",
           "HARNESS_MANAGER_SOME_PASSWORD": "pw", "HARNESS_MANAGER_OPENOCD": STALE}
    snap = SE.capture(env)
    assert set(snap) == set(env)
    out = SE.shown(snap)
    assert out["HARNESS_MANAGER_OPENOCD"] == STALE
    for k in ("HARNESS_MANAGER_GITHUB_TOKEN", "FPGAHUB_TOKEN", "HARNESS_MANAGER_SOME_PASSWORD"):
        assert out[k] == SE.HIDDEN
    described = json.dumps(SE.describe(snap))
    assert "ghp_secret" not in described and "hub-secret" not in described


# --- which variable overrides what ------------------------------------------------------------------


def test_a_tool_variable_that_hides_your_setting_is_flagged_and_warned():
    write_settings(f'[tools]\nopenocd = "{MINE}"\n')
    d = SE.describe({"HARNESS_MANAGER_OPENOCD": STALE})
    (o,) = d["overrides"]
    assert (o["var"], o["key"], o["tool"], o["in_effect"], o["hides_yours"]) == \
        ("HARNESS_MANAGER_OPENOCD", "tools.openocd", True, True, True)
    assert d["warning"].startswith("$HARNESS_MANAGER_OPENOCD in the service's environment "
                                   "overrides your tools.openocd")
    assert "harness-manager daemon stop" in d["warning"]


def test_negative_twin_no_warning_without_your_own_value_or_for_a_non_tool_row():
    d = SE.describe({"HARNESS_MANAGER_OPENOCD": STALE})         # nothing in settings.toml
    (o,) = d["overrides"]
    assert o["in_effect"] and not o["hides_yours"] and d["warning"] == ""
    write_settings('[general]\napp_browser = "firefox"\n')
    d = SE.describe({"HARNESS_MANAGER_APP_BROWSER": "chromium"})
    (o,) = d["overrides"]
    assert o["hides_yours"] and not o["tool"] and d["warning"] == ""   # not a tool variable


def test_twin_a_variable_your_file_outranks_is_not_in_effect():
    write_settings('[updates]\nchannel = "stable"\n')             # updates.channel: under-user
    d = SE.describe({"HARNESS_MANAGER_UPDATE_CHANNEL": "beta"})
    (o,) = d["overrides"]
    assert not o["in_effect"] and not o["hides_yours"]


# --- the route (what the app and `daemon status` read) --------------------------------------------


@pytest.fixture
def client():
    eng = DemoEngine(speed=0)
    try:
        app = create_app(eng, token=TOKEN, static_dir=None)
        with TestClient(app) as c:
            yield c, app
    finally:
        eng.close_all()


def test_the_route_answers_what_the_service_started_with(client):
    c, app = client
    write_settings(f'[tools]\nopenocd = "{MINE}"\n')
    app.state.daemon.env_at_start = {"HARNESS_MANAGER_OPENOCD": STALE}
    body = c.get("/api/v1/daemon/env", headers=AUTH).json()
    assert body["ok"] and body["env"] == {"HARNESS_MANAGER_OPENOCD": STALE}
    assert body["overrides"][0]["hides_yours"] and "tools.openocd" in body["warning"]


def test_negative_twin_the_route_needs_the_token_and_health_says_nothing(client):
    c, app = client
    app.state.daemon.env_at_start = {"HARNESS_MANAGER_OPENOCD": STALE}
    assert c.get("/api/v1/daemon/env").status_code == 401
    assert STALE not in c.get("/api/v1/health").text


# --- `daemon status` ------------------------------------------------------------------------------


def test_status_lines_name_the_variable_the_setting_and_the_warning():
    st = {"env": {"HARNESS_MANAGER_OPENOCD": STALE},
          "env_overrides": [{"var": "HARNESS_MANAGER_OPENOCD", "key": "tools.openocd",
                             "in_effect": True, "hides_yours": True, "tool": True}],
          "env_warning": "$HARNESS_MANAGER_OPENOCD ... overrides your tools.openocd"}
    lines = _env_lines(st)
    assert lines[0] == (f"env        HARNESS_MANAGER_OPENOCD={STALE}  (sets tools.openocd; "
                        "hides your own value)")
    assert lines[-1].startswith("warning    $HARNESS_MANAGER_OPENOCD")
    assert _env_lines({"env": {}}) == [
        "env        no HARNESS_MANAGER_* or tool variables in the service's environment"]
    assert _env_lines({}) == []                     # an older service: nothing to say


@pytest.fixture
def daemon_cleanup():
    yield
    stop_state_dir(state_dir())


@pytest.mark.timeout(120)
def test_a_service_keeps_the_starters_variable_after_your_shell_dropped_it(
        capsys, monkeypatch, daemon_cleanup):
    """The finding, end to end: the starter had it, the user's shell no longer does."""
    write_settings(f'[tools]\nopenocd = "{MINE}"\n')
    monkeypatch.setenv("HARNESS_MANAGER_OPENOCD", STALE)           # the old terminal
    rc, out, err = run_cli(capsys, "--json", "daemon", "start")
    assert rc == 0, err
    monkeypatch.delenv("HARNESS_MANAGER_OPENOCD")                   # the user's shell now
    rc, out, err = run_cli(capsys, "--json", "daemon", "status")
    st = json.loads(out)
    assert rc == 0 and st["state"] == "running", err
    assert st["env"]["HARNESS_MANAGER_OPENOCD"] == STALE
    assert "tools.openocd" in st["env_warning"]
    rc, out, _ = run_cli(capsys, "daemon", "status")
    assert f"HARNESS_MANAGER_OPENOCD={STALE}" in out and "warning" in out
    rc, out, _ = run_cli(capsys, "--tsv", "daemon", "status")
    assert out.rstrip("\n").split("\t")[5].startswith("$HARNESS_MANAGER_OPENOCD")
    log = (state_dir() / "daemon.log").read_text()
    assert "service environment: " in log and STALE in log


@pytest.mark.timeout(120)
def test_negative_twin_a_service_started_without_it_has_no_warning(
        capsys, monkeypatch, daemon_cleanup):
    write_settings(f'[tools]\nopenocd = "{MINE}"\n')
    monkeypatch.delenv("HARNESS_MANAGER_OPENOCD", raising=False)
    rc, out, err = run_cli(capsys, "--json", "daemon", "start")
    assert rc == 0, err
    monkeypatch.setenv("HARNESS_MANAGER_OPENOCD", STALE)           # set in YOUR shell only
    rc, out, _ = run_cli(capsys, "--json", "daemon", "status")
    st = json.loads(out)
    assert "HARNESS_MANAGER_OPENOCD" not in st["env"] and st["env_warning"] == ""


# --- the app (static: no browser on this box; the mock serves the route for the browser tests) --


JS = Path(__file__).resolve().parents[2] / "src/harness_manager/web/static/js"


def test_the_app_reads_the_route_and_shows_the_warning_and_the_advanced_card():
    api = (JS / "api.js").read_text()
    store = (JS / "store.js").read_text()
    app = (JS / "app.js").read_text()
    sections = (JS / "settings/sections.js").read_text()
    assert 'daemonEnv: ["GET", "/daemon/env"]' in api
    assert 'call("daemonEnv")' in store and '"settings.changed") loadServiceEnv()' in store
    assert 'data-testid="env-banner"' in app and "S.serviceEnv.warning" in app
    assert "<${ServiceEnvCard} />" in sections and 'data-testid="service-env"' in sections


def test_negative_twin_the_mock_answers_no_warning_until_a_test_scripts_one():
    from tests.fakes.t14_mock_api import create_app as mock_app

    eng = DemoEngine(speed=0)
    try:
        app = mock_app(eng, token=TOKEN, serve_ui=False)
        with TestClient(app) as c:
            body = c.get("/api/v1/daemon/env", headers=AUTH).json()
            assert body["ok"] and body["env"] == {} and body["warning"] == ""
            write_settings(f'[tools]\nopenocd = "{MINE}"\n')
            app.state.service_env = {"HARNESS_MANAGER_OPENOCD": STALE}
            assert "tools.openocd" in c.get("/api/v1/daemon/env", headers=AUTH).json()["warning"]
    finally:
        eng.close_all()
