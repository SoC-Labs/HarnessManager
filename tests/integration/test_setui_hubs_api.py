"""SET-UI: the /hubs routes (docs/API.md "Hubs in the Settings dialog") on the real daemon app,
and Tools Detect through POST /settings/test.

The app is ``daemon.app.create_app`` over the demo engine, under FastAPI's TestClient, with
its settings in the test's own directory and its policy file pointed there. REST hubs are the
T8 fake fpgahub on 127.0.0.1; nothing reaches a real hub, and no ssh runs. Every check has a
negative twin.
"""

from __future__ import annotations

import json
import os
import stat
import time
import warnings
from pathlib import Path

import pytest
import tomllib

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.events import Event
from harness_manager.daemon.app import create_app
from harness_manager.demo import DemoEngine
from tests.fakes.t8_hub_rest import FakeFpgahub

TOKEN = "set-ui-hubs-token"
H = {"Authorization": f"Bearer {TOKEN}"}


class World:
    def __init__(self, client: TestClient, tmp: Path) -> None:
        self.c = client
        self.d = client.app.state.daemon
        self.state = tmp / "state"
        self.policy = tmp / "policy.toml"
        self.d.settings.policy_path = self.policy
        self.d.settings.env = {"HARNESS_MANAGER_KEYRING": "off", "PATH": "/usr/bin:/bin"}
        self.d.settings.keyrings = []
        self.events: list[Event] = []
        self.replies: list[str] = []
        self.d.bus.subscribe("settings.*", self.events.append)

    def call(self, method: str, path: str, **kw):
        r = self.c.request(method, f"/api/v1{path}", headers=H, **kw)
        self.replies.append(r.text)
        return r

    def job(self, r) -> dict:
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        deadline = time.monotonic() + 15
        while (st := self.call("GET", f"/jobs/{job}").json())["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return st

    def file(self, name: str) -> dict:
        p = self.state / name
        return tomllib.loads(p.read_text()) if p.exists() else {}

    def changed(self) -> list[dict]:
        return [e.data for e in self.events if e.topic == "settings.changed"]


@pytest.fixture
def w(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-login.toml"))
    eng = DemoEngine(speed=0)
    try:
        app = create_app(eng, token=TOKEN, state_dir=tmp_path / "state", static_dir=None)
        with TestClient(app) as client:
            yield World(client, tmp_path)
    finally:
        eng.close_all()


@pytest.fixture
def fpgahub():
    with FakeFpgahub() as hub:
        yield hub


# --- list, add, change, remove ------------------------------------------------------------------


def test_no_hub_is_an_empty_list_and_adding_one_writes_it_and_says_so(w):
    assert w.call("GET", "/hubs").json()["hubs"] == []
    r = w.call("PUT", "/hubs/lab", json={"transport": "ssh", "host": "hub.invalid", "jump": "gw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["hub"]["host"] == "hub.invalid" and body["apply"] == "reopen"
    assert w.file("settings.toml")["hubs"]["lab"] == {"transport": "ssh", "host": "hub.invalid",
                                                      "jump": "gw"}
    assert w.changed()[-1]["keys"] == ["hubs.lab.host", "hubs.lab.jump", "hubs.lab.transport"]
    hubs = w.call("GET", "/hubs").json()["hubs"]
    assert [h["name"] for h in hubs] == ["lab"] and hubs[0]["boards"] == []
    r = w.call("PUT", "/hubs/lab", json={"group": ""})              # a change, not a new one
    assert r.status_code == 200 and r.json()["created"] is False
    assert w.file("settings.toml")["hubs"]["lab"]["group"] == ""


def test_negative_twin_a_hub_that_could_not_be_used_is_refused_and_nothing_is_written(w):
    r = w.call("PUT", "/hubs/lab", json={"transport": "rest"})       # REST with no url
    assert r.status_code == 400 and "url is not set" in r.json()["error"]["message"]
    r = w.call("PUT", "/hubs/bad name", json={"host": "x"})
    assert r.status_code == 400
    r = w.call("PUT", "/hubs/lab", json={"transport": "rest", "url": "http://hub.example:7246"})
    assert r.status_code == 400 and "plain http" in r.json()["error"]["message"]
    r = w.call("PUT", "/hubs/lab", json={"host": "h", "token": "x"})
    assert r.status_code == 400 and "secret" in r.json()["error"]["message"]
    assert not (w.state / "settings.toml").exists() and w.changed() == []


def test_a_machine_hub_is_listed_locked_and_refuses_changes_but_not_its_token(w, fpgahub):
    w.policy.write_text(f'[hubs.lab]\ntransport = "rest"\nurl = "{fpgahub.url}"\n')
    hub = w.call("GET", "/hubs").json()["hubs"][0]
    assert hub["machine"] is True and hub["policy"] == str(w.policy)
    assert set(hub["locked"]) >= {"transport", "url"}
    r = w.call("PUT", "/hubs/lab", json={"url": "https://elsewhere:7246"})
    assert r.status_code == 409 and str(w.policy) in r.json()["error"]["message"]
    assert w.call("DELETE", "/hubs/lab").status_code == 409
    r = w.call("PUT", "/settings/secrets/hubs.lab.token", json={"value": "mine-7a"})
    assert r.status_code == 200 and r.json()["secret"]["set"] is True          # the twin


def test_remove_is_refused_while_a_board_uses_the_hub_and_force_removes_it_and_its_token(w):
    (w.state).mkdir(parents=True, exist_ok=True)
    (w.state / "settings.toml").write_text('[hubs.lab]\nhost = "hub.invalid"\n')
    (w.state / "boards.toml").write_text('[boards.b1]\nhub = { use = "lab", target = "t1" }\n')
    w.call("PUT", "/settings/secrets/hubs.lab.token", json={"value": "tok-rm-1"})
    hub = w.call("GET", "/hubs").json()["hubs"][0]
    assert hub["boards"] == ["b1"] and hub["targets_used"] == {"b1": "t1"}
    r = w.call("DELETE", "/hubs/lab")
    assert r.status_code == 400 and "b1" in r.json()["error"]["message"]
    assert "lab" in w.file("settings.toml")["hubs"]                        # the twin: kept
    r = w.call("DELETE", "/hubs/lab?force=1")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": "lab", "boards": ["b1"]}
    assert "lab" not in w.file("settings.toml").get("hubs", {})
    assert w.d.settings.store().get("hubs.lab.token") is None
    assert w.call("DELETE", "/hubs/lab").status_code == 404


# --- adopt: "Make this a hub" -----------------------------------------------------------------------


def test_adopt_makes_an_inline_hub_table_a_named_hub_and_keeps_the_comments(w):
    w.state.mkdir(parents=True, exist_ok=True)
    (w.state / "boards.toml").write_text(
        '# my lab board\n[boards.lab]\nmatch = ["192.168.10.101"]\n'
        'via = "ssh:mapstone-dev.ecs.soton.ac.uk"\n'
        'hub = { host = "mapstone-dev.ecs.soton.ac.uk", target = "mps3_01_pl" }\n')
    inline = w.call("GET", "/hubs").json()["inline"]
    assert inline == [{"board": "lab", "host": "mapstone-dev.ecs.soton.ac.uk", "url": "",
                       "via": "ssh:mapstone-dev.ecs.soton.ac.uk", "name": "mapstone-dev"}]
    r = w.call("POST", "/hubs/adopt", json={"board": "lab"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["hub"] == "mapstone-dev" and out["created"] is True and out["via"] == "hub"
    text = (w.state / "boards.toml").read_text()
    assert "# my lab board" in text and 'use = "mapstone-dev"' in text and 'via = "hub"' in text
    assert Path(out["backup"]).exists()
    assert "hubs.mapstone-dev.host" in w.changed()[-1]["keys"]
    assert w.call("GET", "/hubs").json()["inline"] == []
    again = w.call("POST", "/hubs/adopt", json={"board": "lab"}).json()     # the twin: idempotent
    assert again["changed"] is False and again["keys"] == []


def test_negative_twin_adopt_refuses_a_board_with_no_hub_table(w):
    w.state.mkdir(parents=True, exist_ok=True)
    (w.state / "boards.toml").write_text('[boards.lab]\nmatch = ["10.0.0.1"]\n')
    before = (w.state / "boards.toml").read_text()
    assert w.call("POST", "/hubs/adopt", json={"board": "lab"}).status_code == 400
    assert w.call("POST", "/hubs/adopt", json={"board": "nope"}).status_code == 404
    assert w.call("POST", "/hubs/adopt", json={}).status_code == 400
    assert (w.state / "boards.toml").read_text() == before and w.changed() == []


# --- add a board from a hub's target ----------------------------------------------------------------


def test_add_this_board_reads_the_target_and_writes_boards_toml_without_a_lease(w, fpgahub):
    w.call("PUT", "/hubs/remote", json={"transport": "rest", "url": fpgahub.url})
    tok = fpgahub.add_token("alice", "write")
    w.call("PUT", "/settings/secrets/hubs.remote.token", json={"value": tok})
    st = w.job(w.call("POST", "/hubs/remote/boards", json={"target": "mps3_01_pl"}))
    assert st["state"] == "done", st
    res = st["result"]
    assert res["board"] == "mps3_01_pl" and res["hub"] == "remote"
    b = w.file("boards.toml")["boards"]["mps3_01_pl"]
    assert b["via"] == "hub" and b["hub"] == {"use": "remote", "target": "mps3_01_pl",
                                              "shares": {"mcc": "/dev/mps3_01_pl/tty_00"}}
    assert "boards.mps3_01_pl.via" in w.changed()[-1]["keys"]
    assert not fpgahub.leases and not fpgahub.emitted("lease.")
    assert all(tok not in t for t in w.replies)
    hub = w.call("GET", "/hubs").json()["hubs"][0]
    assert hub["boards"] == ["mps3_01_pl"]
    # the twin: the same target again is refused, and nothing more is written
    st = w.job(w.call("POST", "/hubs/remote/boards", json={"target": "mps3_01_pl"}))
    assert st["state"] == "failed" and "already" in st["error"]["message"]


def test_negative_twin_add_board_names_a_hub_that_exists_and_a_target(w):
    assert w.call("POST", "/hubs/nope/boards", json={"target": "t"}).status_code == 400
    w.call("PUT", "/hubs/lab", json={"host": "hub.invalid"})
    assert w.call("POST", "/hubs/lab/boards", json={}).status_code == 400
    assert not (w.state / "boards.toml").exists()


# --- hub Test connection reports progress per step --------------------------------------------------


def test_hub_test_connection_is_a_job_whose_progress_names_each_step(w, fpgahub):
    w.call("PUT", "/hubs/remote", json={"transport": "rest", "url": fpgahub.url})
    w.call("PUT", "/settings/secrets/hubs.remote.token", json={"value": fpgahub.add_token("a")})
    seen: list[str] = []
    w.d.bus.subscribe("job.progress", lambda e: seen.append(e.data.get("phase", "")))
    st = w.job(w.call("POST", "/settings/test", json={"section": "hubs", "name": "remote"}))
    assert st["state"] == "done" and st["result"]["passed"] is True
    assert {"config", "reach", "auth", "targets"} <= set(seen)
    assert len(st["result"]["targets"]) == 3


# --- Tools Detect -------------------------------------------------------------------------------------


def fake(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$@" >> "{path}.argv"\n{body}')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.skipif(os.name != "posix", reason="/bin/sh scripts stand in for the tools")
def test_tools_detect_runs_only_version_probes_and_never_hw_server(w, tmp_path):
    bindir = tmp_path / "bin"
    ocd = fake(bindir / "openocd", 'echo "Open On-Chip Debugger 0.12.0+dev-01234" >&2\n')
    uv = fake(bindir / "uv", 'echo "uv 0.4.30"\n')
    viv = fake(tmp_path / "Vivado" / "2024.1" / "bin" / "vivado", 'echo "vivado v2024.1 (64-bit)"\n')
    hw = fake(tmp_path / "Vivado" / "2024.1" / "bin" / "hw_server", 'echo RAN > "$0.ran"\n')
    w.d.settings.env = {**w.d.settings.env, "PATH": f"{bindir}:/usr/bin:/bin"}
    w.call("PUT", "/settings", json={"tools.vivado": str(viv), "tools.hw_server": str(hw)})
    st = w.job(w.call("POST", "/settings/test", json={"section": "tools"}))
    res = st["result"]
    assert res["passed"] is True, res
    details = {s["step"]: s["detail"] for s in res["steps"]}
    assert details["openocd"] == f"OpenOCD 0.12.0+dev-01234 at {ocd}"
    assert details["uv"] == f"uv 0.4.30 at {uv}"
    assert details["vivado"] == f"Vivado 2024.1 at {viv}"
    assert details["hw_server"].startswith(f"hw_server 2024.1 at {hw}")
    assert res["tools"]["openocd"] == {"path": str(ocd), "version": "0.12.0+dev-01234",
                                       "how": "PATH", "key": "tools.openocd"}
    assert Path(f"{ocd}.argv").read_text().split() == ["--version"]
    assert Path(f"{uv}.argv").read_text().split() == ["--version"]
    assert Path(f"{viv}.argv").read_text().split() == ["-version"]
    assert not Path(f"{hw}.argv").exists() and not Path(f"{hw}.ran").exists()
    assert not (w.state / "settings.toml").read_text().count("openocd")    # nothing written


@pytest.mark.skipif(os.name != "posix", reason="/bin/sh scripts stand in for the tools")
def test_negative_twin_a_tool_that_does_not_run_or_is_missing_fails_its_step(w, tmp_path):
    bad = fake(tmp_path / "bin" / "openocd", 'echo "cannot open shared object" >&2\nexit 127\n')
    w.call("PUT", "/settings", json={"tools.openocd": str(bad), "tools.uv": str(tmp_path / "nope")})
    res = w.job(w.call("POST", "/settings/test", json={"section": "tools", "name": "openocd"}))["result"]
    assert res["passed"] is False and [s["step"] for s in res["steps"]] == ["openocd"]
    assert "does not run" in res["steps"][0]["detail"] and "exited 127" in res["steps"][0]["detail"]
    res = w.job(w.call("POST", "/settings/test", json={"section": "tools", "name": "uv"}))["result"]
    assert res["passed"] is False and "not a file" in res["steps"][0]["detail"]
    r = w.call("POST", "/settings/test", json={"section": "tools", "name": "gcc"})
    st = w.job(r)
    assert st["state"] == "failed" and "no tool 'gcc'" in st["error"]["message"]
    assert json.dumps(st)                                                   # a plain report
