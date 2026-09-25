"""SET-API: ``harness-manager config`` with no service running (it reads and writes the files
itself, through ``settings.ops``). The service path is ``tests/integration/
test_cli_config_service.py``. Every behaviour has a negative twin.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.settings import testers

SECRET = "tok-CLI-NEVER-PRINTED-51aa"


@pytest.fixture(autouse=True)
def _policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "policy.toml"
    monkeypatch.setattr("harness_manager.services.update.policy.policy_path",
                        lambda *a, **k: path)
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    return path


def state() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def run(capsys, *argv: str, stdin: str | None = None, monkeypatch=None) -> tuple[int, str, str]:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_set_get_list_unset_round_trip(capsys):
    rc, out, _ = run(capsys, "--json", "config", "set", "tools.openocd", "/opt/ocd",
                     "consoles.scrollback", "8000")
    assert rc == 0, out
    body = json.loads(out)
    assert body["ok"] and body["keys"] == ["tools.openocd", "consoles.scrollback"]
    rc, out, _ = run(capsys, "--json", "config", "get", "tools.openocd")
    got = json.loads(out)
    assert rc == 0 and got["row"]["value"] == "/opt/ocd" and got["row"]["source"] == "user"
    assert got["service"] is None and got["differs"] is False
    rc, out, _ = run(capsys, "config", "list", "consoles")
    assert rc == 0 and "consoles.scrollback" in out and "8000" in out and "yours" in out
    rc, out, _ = run(capsys, "config", "unset", "tools.openocd")
    assert rc == 0 and "default" in out
    assert "openocd" not in (state() / "settings.toml").read_text()


def test_negative_twin_unset_of_nothing_says_nothing_was_written(capsys):
    rc, out, _ = run(capsys, "config", "unset", "tools.openocd")
    assert rc == 0 and "nothing was written" in out
    assert not (state() / "settings.toml").exists()


def test_a_bad_value_writes_nothing_and_exits_2(capsys):
    run(capsys, "config", "set", "general.theme", "light")
    before = (state() / "settings.toml").read_text()
    rc, _, err = run(capsys, "config", "set", "general.theme", "dark", "consoles.scrollback",
                     "lots")
    assert rc == ExitCode.USAGE and "consoles.scrollback" in err and "nothing was written" in err
    assert (state() / "settings.toml").read_text() == before


def test_negative_twin_the_same_pairs_all_good_exit_0(capsys):
    rc, _, _ = run(capsys, "config", "set", "general.theme", "dark", "consoles.scrollback",
                   "9000")
    assert rc == 0 and "9000" in (state() / "settings.toml").read_text()


def test_pairs_must_pair(capsys):
    rc, _, err = run(capsys, "config", "set", "updates.mirrors", "/a", "/b")
    assert rc == ExitCode.USAGE and "ONE argument" in err
    rc, _, _ = run(capsys, "config", "set", "updates.mirrors", "/a,/b")
    assert rc == 0


def test_a_locked_key_exits_15_naming_the_policy_file(capsys, _policy):
    _policy.write_text('[lock]\ntools.vivado = "/tools/vivado"\n')
    rc, _, err = run(capsys, "config", "set", "tools.vivado", "/mine")
    assert rc == ExitCode.REFUSED and str(_policy) in err
    assert not (state() / "settings.toml").exists()
    rc, out, _ = run(capsys, "config", "get", "tools.vivado")
    assert "locked by the administrator" in out and str(_policy) in out


def test_negative_twin_without_the_lock_it_is_set(capsys):
    rc, _, _ = run(capsys, "config", "set", "tools.vivado", "/mine")
    assert rc == 0


def test_get_reports_env_shadowing(capsys, monkeypatch):
    run(capsys, "config", "set", "tools.openocd", "/mine/ocd")
    monkeypatch.setenv("HARNESS_MANAGER_OPENOCD", "/env/ocd")
    rc, out, _ = run(capsys, "config", "get", "tools.openocd")
    assert rc == 0 and "/env/ocd" in out and "$HARNESS_MANAGER_OPENOCD" in out
    assert "hides your own value" in out and "this shell's environment" in out


def test_negative_twin_without_the_variable_nothing_is_hidden(capsys):
    run(capsys, "config", "set", "tools.openocd", "/mine/ocd")
    rc, out, _ = run(capsys, "config", "get", "tools.openocd")
    assert rc == 0 and "hides" not in out and "/mine/ocd" in out


# --- secrets -------------------------------------------------------------------------------------


def test_set_secret_reads_stdin_and_never_prints_it(capsys, monkeypatch):
    rc, out, err = run(capsys, "config", "set-secret", "hubs.lab.token", stdin=SECRET + "\n",
                       monkeypatch=monkeypatch)
    assert rc == 0 and "stored in a private file" in out
    assert SECRET not in out + err
    for argv in (("config", "get", "hubs.lab.token"), ("--json", "config", "list", "--all"),
                 ("--tsv", "config", "list", "hubs"), ("--json", "config", "path")):
        rc, out, err = run(capsys, *argv)
        assert rc == 0 and SECRET not in out + err, argv
    from harness_manager.settings import SecretStore

    assert SecretStore(state(), keyrings=[]).get("hubs.lab.token") == SECRET
    rc, out, _ = run(capsys, "config", "unset-secret", "hubs.lab.token")
    assert rc == 0 and "not set" in out


def test_negative_twin_set_secret_refuses_a_value_on_the_command_line(capsys, monkeypatch):
    rc, out, err = run(capsys, "config", "set-secret", "hubs.lab.token", SECRET,
                       stdin="", monkeypatch=monkeypatch)
    assert rc == ExitCode.USAGE and "never takes the secret on the command line" in err
    assert SECRET not in out + err
    assert not (state() / "secrets").exists()
    rc, _, err = run(capsys, "config", "set-secret", "hubs.lab.token", stdin="",
                     monkeypatch=monkeypatch)
    assert rc == ExitCode.USAGE and "no secret on stdin" in err
    rc, _, err = run(capsys, "config", "set", "hubs.lab.token", SECRET)
    assert rc == ExitCode.USAGE and "set-secret" in err and SECRET not in err


def test_clear_secret_is_unset_secret(capsys, monkeypatch):
    run(capsys, "config", "set-secret", "updates.github_token", stdin=SECRET,
        monkeypatch=monkeypatch)
    rc, out, _ = run(capsys, "--json", "config", "clear-secret", "updates.github_token")
    assert rc == 0 and json.loads(out)["secret"]["set"] is False


# --- path, test, formats ----------------------------------------------------------------------


def test_path_names_the_files_and_the_secret_backend(capsys, _policy):
    rc, out, _ = run(capsys, "config", "path")
    assert rc == 0 and str(state() / "settings.toml") in out and str(_policy) in out
    assert "new secrets go to a private file" in out


def test_test_without_a_tester_exits_12(capsys):
    rc, _, err = run(capsys, "config", "test", "tools")
    assert rc == ExitCode.UNAVAILABLE and "not testable yet" in err


def test_negative_twin_a_tester_that_fails_exits_6_and_one_that_passes_0(capsys):
    verdict = {"ok": False}
    testers.register("tools", lambda req: {"ok": verdict["ok"], "steps": [
        {"step": "openocd", "ok": verdict["ok"], "detail": "not found", "hint": "install it"}]})
    try:
        rc, out, _ = run(capsys, "config", "test", "tools")
        assert rc == ExitCode.ACTION_FAILED and "FAIL" in out and "install it" in out
        verdict["ok"] = True
        rc, out, _ = run(capsys, "config", "test", "tools")
        assert rc == 0 and "PASS" in out
    finally:
        testers.unregister("tools")


@pytest.mark.parametrize("argv, layout", [
    (("config", "list", "tools"), "config list"),
    (("config", "get", "tools.openocd"), "config get"),
    (("config", "set", "general.theme", "dark"), "config set|unset"),
    (("config", "unset", "general.theme"), "config set|unset"),
    (("config", "unset-secret", "hubs.lab.token"), "config set-secret|unset-secret"),
    (("config", "path"), "config path"),
])
def test_tsv_rows_have_the_layouts_columns(capsys, argv, layout):
    rc, out, _ = run(capsys, "--tsv", *argv)
    rows = [line.split("\t") for line in out.splitlines()]
    assert rc == 0 and rows
    assert {len(r) for r in rows} == {len(TSV_COLUMNS[layout])}


def test_config_is_in_the_help(capsys):
    rc, out, _ = run(capsys, "help", "config")
    assert rc == 0 and "set-secret" in out
    rc, out, _ = run(capsys, "help", "--tabs", "Output")
    assert "config set-secret|unset-secret" in out
