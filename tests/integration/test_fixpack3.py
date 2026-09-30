"""FIX-PACK-3: findings from the P8 clean-account run of the platform guide (HM 506a5ef).

Item 1 (the installer's PATH advice) is in ``test_q3_installer.py``, beside the installer's
other tests. Each check here has a negative twin.
"""

from __future__ import annotations

import json

from harness_manager.cli.cmd_daemon import ENV_NO_ANSWER, ENV_NOT_RUNNING, _env_lines, _env_note
from tests.fakes.t13_daemon import run_cli

# --- item 2: `daemon status` on a stopped service says where the env line went ------------------


def test_a_stopped_service_says_its_environment_is_shown_while_it_runs(capsys):
    rc, out, err = run_cli(capsys, "daemon", "status")
    assert rc == 0, err
    assert "state      stopped" in out
    assert f"env        ({ENV_NOT_RUNNING})" in out.splitlines()
    rc, out, _ = run_cli(capsys, "--json", "daemon", "status")
    st = json.loads(out)
    assert st["state"] == "stopped" and st["env_note"] == ENV_NOT_RUNNING
    assert "env" not in st                                  # no environment is invented
    rc, out, _ = run_cli(capsys, "--tsv", "daemon", "status")
    cols = out.rstrip("\n").split("\t")
    assert cols[0] == "stopped" and cols[5] == "-" and cols[6] == ENV_NOT_RUNNING  # appended


def test_stale_and_unresponsive_say_why_too():
    assert _env_lines({"state": "stale"}) == [f"env        ({ENV_NOT_RUNNING})"]
    assert _env_lines({"state": "unresponsive"}) == [f"env        ({ENV_NO_ANSWER})"]


def test_negative_twin_a_running_service_never_gets_the_note():
    # a running service with its variables, with none, and an older one with no /daemon/env
    assert _env_note({"state": "running", "env": {"HARNESS_MANAGER_OPENOCD": "/x"}}) == ""
    assert _env_lines({"state": "running", "env": {}}) == [
        "env        no HARNESS_MANAGER_* or tool variables in the service's environment"]
    assert _env_lines({"state": "running"}) == []
    assert _env_lines({}) == []
