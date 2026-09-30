"""FIX-PACK-3: findings from the P8 clean-account run of the platform guide (HM 506a5ef).

Item 1 (the installer's PATH advice) is in ``test_q3_installer.py``, beside the installer's
other tests. Each check here has a negative twin.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.cli.cmd_daemon import ENV_NO_ANSWER, ENV_NOT_RUNNING, _env_lines, _env_note
from harness_manager.daemon.app import create_app
from harness_manager.demo import DemoEngine
from tests.fakes.t13_daemon import run_cli

ROOT = Path(__file__).resolve().parents[2]

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


# --- item 3: GET /boards lists what the service knows; discovery is POST /probe (docs/API.md) ---

TOKEN = "fp3-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def demo_client():
    eng = DemoEngine(speed=0)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            yield c
    finally:
        eng.close_all()


def test_boards_on_a_fresh_demo_service_is_empty_until_a_probe(demo_client):
    """The behaviour docs/API.md now states (unchanged by FIX-PACK-3)."""
    c = demo_client
    assert c.get("/api/v1/boards", headers=AUTH).json()["boards"] == []
    found = c.post("/api/v1/probe", headers=AUTH, json={}).json()["candidates"]
    assert found
    rows = c.get("/api/v1/boards", headers=AUTH).json()["boards"]
    assert {r["source"] for r in rows} == {"probe"} and not any(r["open"] for r in rows)
    assert len(rows) == len(found)


def test_negative_twin_the_docs_say_boards_is_not_discovery():
    api = (ROOT / "docs" / "API.md").read_text()
    row = next(ln for ln in api.splitlines() if ln.startswith("| `GET /boards` |"))
    assert "discovers nothing" in row and "`POST /probe`" in row and "`{boards: []}`" in row
    assert "**`GET /boards` is not discovery.**" in api
    old_row = ("| `GET /boards` | open boards + lock owners | `{boards: [{board_id, open: bool, "
               "holder?: LockOwner, candidate, source, configured?}]}`.")
    assert "POST /probe" not in old_row                   # what the P8 reader had
