"""SET-API: ``harness-manager config`` while a service runs: every verb goes through its
``/settings`` API (one writer; the open windows hear ``settings.changed``), and ``config get``
shows the service's view next to this shell's. The service is the real daemon app under a
real uvicorn on an ephemeral loopback port (``LiveDaemon``), over the demo engine.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode
from harness_manager.demo import DemoEngine
from harness_manager.settings import testers
from tests.fakes.t13_daemon import LiveDaemon, state_dir

SECRET = "tok-VIA-SERVICE-NEVER-PRINTED-0b7e"
ENV = "HARNESS_MANAGER_OPENOCD"


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy = tmp_path / "policy.toml"
    monkeypatch.setattr("harness_manager.services.update.policy.policy_path",
                        lambda *a, **k: policy)
    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
    monkeypatch.delenv(ENV, raising=False)
    eng = DemoEngine(speed=0)
    try:
        with LiveDaemon(eng, static_dir=None) as live:
            d = live.app.state.daemon
            d.settings.env = {"HARNESS_MANAGER_KEYRING": "off"}      # the service's own env
            events: list = []
            d.bus.subscribe("settings.changed", events.append)
            yield live, d, events, policy
    finally:
        eng.close_all()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_set_goes_through_the_service_and_the_windows_hear_it(svc, capsys):
    _, _, events, _ = svc
    rc, out, _ = run(capsys, "config", "set", "general.theme", "dark", "advanced.port", "0")
    assert rc == 0, out
    assert [e.data["keys"] for e in events] == [["general.theme", "advanced.port"]]
    assert events[0].data["apply"] == "restart" and "service restarts" in out
    rc, out, _ = run(capsys, "config", "unset", "general.theme")
    assert rc == 0 and [e.data["keys"] for e in events][-1] == ["general.theme"]


def test_negative_twin_with_the_service_bypassed_the_file_changes_but_no_one_hears(
        svc, capsys, monkeypatch):
    _, _, events, _ = svc
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    rc, _, _ = run(capsys, "config", "set", "general.theme", "dark")
    assert rc == 0 and "dark" in (state_dir() / "settings.toml").read_text()
    assert events == []


def test_get_shows_the_services_value_and_this_shells_when_they_differ(svc, capsys,
                                                                     monkeypatch):
    run(capsys, "config", "set", "tools.openocd", "/mine/ocd")
    monkeypatch.setenv(ENV, "/shell/ocd")                  # this shell only
    rc, out, _ = run(capsys, "config", "get", "tools.openocd")
    assert rc == 0
    assert out.splitlines()[0] == "tools.openocd = /mine/ocd"
    assert "this shell would use: /shell/ocd" in out and f"${ENV}" in out
    rc, out, _ = run(capsys, "--json", "config", "get", "tools.openocd")
    got = json.loads(out)
    assert got["differs"] is True and got["service"]["source"] == "user"
    assert got["shell"]["source"] == "env" and got["shell"]["shadowed"] == f"${ENV}"
    rc, out, _ = run(capsys, "--tsv", "config", "get", "tools.openocd")
    assert [line.split("\t")[-1] for line in out.splitlines()] == ["service", "shell"]


def test_negative_twin_the_services_environment_shadows_and_the_shell_does_not(svc, capsys):
    _, d, _, _ = svc
    run(capsys, "config", "set", "tools.openocd", "/mine/ocd")
    rc, out, _ = run(capsys, "config", "get", "tools.openocd")
    assert "this shell sees the same" in out
    d.settings.env = {**d.settings.env, ENV: "/service/ocd"}
    rc, out, _ = run(capsys, "config", "get", "tools.openocd")
    assert out.splitlines()[0] == "tools.openocd = /service/ocd"
    assert "the service's environment" in out and "hides your own value" in out
    assert "this shell would use: /mine/ocd" in out


def test_set_secret_through_the_service_stores_it_there_and_prints_no_value(svc, capsys,
                                                                            monkeypatch):
    _, d, events, _ = svc
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    rc, out, err = run(capsys, "config", "set-secret", "hubs.lab.token")
    assert rc == 0 and SECRET not in out + err
    assert d.settings.store().get("hubs.lab.token") == SECRET
    assert [e.data["keys"] for e in events] == [["hubs.lab.token"]]
    assert SECRET not in json.dumps([e.data for e in events])
    run(capsys, "config", "set", "hubs.lab.host", "hub.example")      # a hub, so it lists
    rc, out, err = run(capsys, "--json", "config", "list", "hubs")
    assert rc == 0 and SECRET not in out + err
    row = next(r for r in json.loads(out)["rows"] if r["key"] == "hubs.lab.token")
    assert row["value"] == {"set": True} and row["secret"]["backend"] == "file"


def test_a_locked_key_through_the_service_exits_15_naming_the_file(svc, capsys):
    _, _, events, policy = svc
    policy.write_text('[lock]\ntools.vivado = "/tools/vivado"\n')
    rc, _, err = run(capsys, "config", "set", "tools.vivado", "/mine")
    assert rc == ExitCode.REFUSED and str(policy) in err and events == []


def test_list_and_path_say_they_are_the_services_view(svc, capsys):
    rc, out, _ = run(capsys, "config", "list", "tools")
    assert rc == 0 and "the service's view" in out
    rc, out, _ = run(capsys, "config", "path")
    assert rc == 0 and "from the service" in out and "from this shell" in out


def test_a_slow_test_runs_as_a_job_in_the_service_and_the_cli_waits(svc, capsys):
    testers.register("hubs", lambda req: {"ok": True, "steps": [
        {"step": "reach", "ok": True, "detail": f"{req.name}: fpgahub 0.3.0"}]},
        job=True, needs_name=True)
    try:
        rc, out, _ = run(capsys, "config", "test", "hubs", "lab")
        assert rc == 0 and "hubs lab: PASS" in out and "lab: fpgahub 0.3.0" in out
        rc, _, err = run(capsys, "config", "test", "hubs")
        assert rc == ExitCode.USAGE and "config test hubs NAME" in err
    finally:
        testers.unregister("hubs")
