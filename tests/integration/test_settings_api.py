"""SET-API: the /settings routes on the real daemon app (docs/API.md "Settings").

The app is ``daemon.app.create_app`` over the demo engine, under FastAPI's TestClient, with
its settings in the test's own directory and its policy file pointed there too. Every
check has a negative twin.
"""

from __future__ import annotations

import json
import time
import warnings
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.events import Event
from harness_manager.daemon.app import create_app
from harness_manager.daemon.hosts import allowed_hosts
from harness_manager.demo import DemoEngine
from harness_manager.services.update.policy import Policy
from harness_manager.settings import testers

TOKEN = "set-api-test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "tok-NEVER-IN-A-REPLY-9d2e"
ENV = "HARNESS_MANAGER_OPENOCD"


class World:
    def __init__(self, client: TestClient, tmp: Path) -> None:
        self.c = client
        self.d = client.app.state.daemon
        self.tmp = tmp
        self.state = tmp / "state"
        self.policy = tmp / "policy.toml"
        self.d.settings.policy_path = self.policy
        self.events: list[Event] = []
        self.replies: list[str] = []
        self.d.bus.subscribe("settings.*", self.events.append)

    def call(self, method: str, path: str, **kw):
        r = self.c.request(method, f"/api/v1{path}", headers=H, **kw)
        self.replies.append(r.text)
        return r

    def changed(self) -> list[dict]:
        return [e.data for e in self.events if e.topic == "settings.changed"]


@pytest.fixture
def w(tmp_path: Path):
    eng = DemoEngine(speed=0)
    try:
        app = create_app(eng, token=TOKEN, state_dir=tmp_path / "state", static_dir=None)
        with TestClient(app) as client:
            yield World(client, tmp_path)
    finally:
        eng.close_all()


def test_get_settings_lists_every_row_with_its_source_and_needs_the_token(w):
    r = w.call("GET", "/settings")
    assert r.status_code == 200
    body = r.json()
    assert {"schema_version", "files", "policy", "problems", "sections", "instances",
            "rows"} <= set(body)
    assert body["files"]["config_dir"] == str(w.state)
    assert body["policy"] == {"path": str(w.policy), "exists": False, "problems": []}
    row = next(x for x in body["rows"] if x["key"] == "tools.openocd")
    assert (row["source"], row["value"], row["section_id"]) == ("default", "", "tools")
    assert "dev.no_daemon" not in {x["key"] for x in body["rows"]}
    every = w.call("GET", "/settings?all=1").json()["rows"]
    assert "dev.no_daemon" in {x["key"] for x in every}
    tools = w.call("GET", "/settings?section=tools").json()["rows"]
    assert tools and {x["section_id"] for x in tools} == {"tools"}


def test_negative_twin_no_token_is_401_and_a_bad_section_is_400(w):
    r = w.c.get("/api/v1/settings")
    assert r.status_code == 401 and r.json()["error"]["name"] == "REFUSED"
    r = w.call("GET", "/settings?section=nonesuch")
    assert r.status_code == 400 and "sections:" in r.json()["error"]["hint"]


def test_the_schema_has_every_row_without_a_value(w):
    body = w.call("GET", "/settings/schema").json()
    rows = {x["key"]: x for x in body["rows"]}
    assert {"tools.openocd", "hubs.*.url", "advanced.allowed_hosts", "dev.keyring"} <= set(rows)
    assert rows["hubs.*.token"]["secret"] is True and rows["hubs.*.token"]["default"] is None
    assert rows["dev.keyring"]["ui"] is False and "value" not in rows["tools.openocd"]


# --- set, get, unset -----------------------------------------------------------------------------


def test_put_get_delete_round_trip_and_each_change_is_an_event(w):
    r = w.call("PUT", "/settings", json={"tools.openocd": "/opt/openocd/bin/openocd",
                                         "consoles.scrollback": "8000"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["keys"] == ["tools.openocd", "consoles.scrollback"] and body["apply"] == "live"
    assert {x["key"]: x["value"] for x in body["rows"]} == {
        "tools.openocd": "/opt/openocd/bin/openocd", "consoles.scrollback": 8000}
    got = w.call("GET", "/settings?key=tools.openocd").json()["rows"]
    assert [(x["value"], x["source"]) for x in got] == [("/opt/openocd/bin/openocd", "user")]
    assert "openocd" in (w.state / "settings.toml").read_text()
    r = w.call("DELETE", "/settings/tools.openocd")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert r.json()["rows"][0]["source"] == "default"
    assert w.changed() == [
        {"keys": ["tools.openocd", "consoles.scrollback"], "apply": "live",
         "applies": {"live": ["tools.openocd", "consoles.scrollback"], "reopen": [],
                     "restart": []}, "source": "api"},
        {"keys": ["tools.openocd"], "apply": "live",
         "applies": {"live": ["tools.openocd"], "reopen": [], "restart": []}, "source": "api"}]


def test_negative_twin_deleting_what_was_never_set_writes_nothing_and_says_nothing(w):
    r = w.call("DELETE", "/settings/tools.openocd")
    assert r.status_code == 200 and r.json()["changed"] is False
    assert w.changed() == [] and not (w.state / "settings.toml").exists()


def test_a_board_key_with_dots_and_slashes_round_trips_through_the_path(w):
    key = 'boards."mps3@usb:/dev/ttyUSB0".name'
    r = w.call("PUT", "/settings", json={key: "mps3-bench"})
    assert r.status_code == 200, r.text
    assert "mps3-bench" in (w.state / "boards.toml").read_text()
    r = w.call("DELETE", f"/settings/{quote(key, safe='')}")
    assert r.status_code == 200 and r.json()["changed"] is True, r.text


def test_the_apply_class_comes_back(w):
    r = w.call("PUT", "/settings", json={"general.theme": "dark", "advanced.port": 0,
                                         "hubs.lab.host": "hub.example"})
    body = r.json()
    assert body["apply"] == "restart"
    assert body["applies"] == {"live": ["general.theme"], "reopen": ["hubs.lab.host"],
                               "restart": ["advanced.port"]}
    assert w.changed()[-1]["apply"] == "restart"


def test_negative_twin_a_live_change_alone_is_live(w):
    assert w.call("PUT", "/settings", json={"general.theme": "dark"}).json()["apply"] == "live"


# --- refusals ------------------------------------------------------------------------------------


def test_a_locked_key_is_409_naming_the_policy_file_and_nothing_is_written(w):
    w.policy.write_text('[lock]\ntools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"\n')
    r = w.call("PUT", "/settings", json={"general.theme": "dark", "tools.vivado": "/mine"})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["name"] == "REFUSED" and str(w.policy) in err["message"]
    assert not (w.state / "settings.toml").exists() and w.changed() == []
    row = w.call("GET", "/settings?key=tools.vivado").json()["rows"][0]
    assert row["locked"] is True and row["where"] == str(w.policy)


def test_negative_twin_the_same_put_without_the_lock_is_200(w):
    r = w.call("PUT", "/settings", json={"general.theme": "dark", "tools.vivado": "/mine"})
    assert r.status_code == 200 and len(w.changed()) == 1


def test_one_bad_key_is_400_naming_it_and_nothing_is_written(w):
    w.call("PUT", "/settings", json={"general.theme": "light"})
    before = (w.state / "settings.toml").read_text()
    r = w.call("PUT", "/settings", json={"general.theme": "dark", "tools.openocd": "/x",
                                         "consoles.scrollback": "lots"})
    assert r.status_code == 400 and "consoles.scrollback" in r.json()["error"]["message"]
    assert (w.state / "settings.toml").read_text() == before and len(w.changed()) == 1
    for bad in ([], {}, "x", {"no.such.key": 1}):
        assert w.call("PUT", "/settings", json=bad).status_code == 400, bad


def test_env_shadowing_is_reported_from_the_services_environment(w, monkeypatch):
    w.call("PUT", "/settings", json={"tools.openocd": "/mine/openocd"})
    monkeypatch.setenv(ENV, "/env/openocd")
    row = w.call("GET", "/settings?key=tools.openocd").json()["rows"][0]
    assert (row["value"], row["source"], row["shadowed"]) == ("/env/openocd", "env", f"${ENV}")


def test_negative_twin_without_the_variable_nothing_is_shadowed(w):
    w.call("PUT", "/settings", json={"tools.openocd": "/mine/openocd"})
    row = w.call("GET", "/settings?key=tools.openocd").json()["rows"][0]
    assert (row["source"], row["shadowed"]) == ("user", "")


# --- secrets -------------------------------------------------------------------------------------


def test_a_secret_goes_in_and_never_comes_out(w):
    frames = []
    with w.c.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=settings.*") as ws:
        r = w.call("PUT", "/settings/secrets/hubs.lab.token", json={"value": SECRET})
        assert r.status_code == 200, r.text
        assert r.json()["secret"]["set"] is True and r.json()["secret"]["backend"] == "file"
        frames.append(ws.receive_text())
        w.call("GET", "/settings")
        w.call("GET", "/settings?key=hubs.lab.token&all=1")
        w.call("GET", "/settings/schema")
        r = w.call("DELETE", "/settings/secrets/hubs.lab.token")
        assert r.json()["secret"]["set"] is False and r.json()["changed"] is True
        frames.append(ws.receive_text())
    assert [json.loads(f)["data"]["keys"] for f in frames] == [["hubs.lab.token"]] * 2
    everything = "\n".join(w.replies + frames + [json.dumps(e.data) for e in w.events])
    assert SECRET not in everything
    assert SECRET not in (w.state / "secrets" / "index.json").read_text()


def test_negative_twin_the_secret_really_was_stored(w):
    w.call("PUT", "/settings/secrets/hubs.lab.token", json={"value": SECRET})
    assert w.d.settings.store().get("hubs.lab.token") == SECRET


def test_a_secret_sent_the_wrong_way_is_refused_without_repeating_it(w):
    for method, path, body in (("PUT", "/settings", {"hubs.lab.token": SECRET}),
                               ("PUT", "/settings/secrets/tools.openocd", {"value": SECRET}),
                               ("PUT", "/settings/secrets/hubs.lab.token",
                                {"value": SECRET, "echo": SECRET}),
                               ("PUT", "/settings/secrets/hubs.lab.token",
                                {"value": f"{SECRET}\nline two"}),
                               ("PUT", "/settings/secrets/hubs.*.token", {"value": SECRET})):
        r = w.call(method, path, json=body)
        assert r.status_code == 400, (path, r.text)
        assert SECRET not in r.text
    assert w.changed() == [] and not (w.state / "secrets").exists()


# --- testing -------------------------------------------------------------------------------------


def test_a_section_without_a_tester_answers_not_testable_yet(w):
    r = w.call("POST", "/settings/test", json={"section": "tools"})
    assert r.status_code == 200
    assert r.json()["testable"] is False and "not testable yet" in r.json()["why"]
    assert w.call("POST", "/settings/test", json={"section": "nope"}).status_code == 400
    assert w.call("POST", "/settings/test", json={}).status_code == 400


def test_negative_twin_a_slow_tester_runs_as_a_job_and_a_fast_one_inline(w):
    calls = []

    def hub(req):
        calls.append((req.section, req.name, req.table))
        req.progress("reach", 1, 3)
        return {"ok": True, "steps": [{"step": "reach", "ok": True, "detail": "fpgahub 0.3.0"}],
                "targets": ["mps3_01_pl"]}

    testers.register("hubs", hub, job=True, needs_name=True)
    testers.register("tools", lambda req: {"ok": True, "steps": [{"step": "openocd",
                                                                   "ok": True}]})
    try:
        r = w.call("POST", "/settings/test", json={"section": "hubs"})
        assert r.status_code == 400 and "config test hubs NAME" in r.json()["error"]["message"]
        r = w.call("POST", "/settings/test", json={"section": "hubs", "name": "lab"})
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        deadline = time.monotonic() + 10
        while (state := w.call("GET", f"/jobs/{job}").json())["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert state["state"] == "done" and state["result"]["passed"] is True
        assert state["result"]["targets"] == ["mps3_01_pl"] and calls == [("hubs", "lab", None)]
        inline = w.call("POST", "/settings/test", json={"kind": "tools"}).json()
        assert inline["testable"] is True and inline["passed"] is True
    finally:
        testers.unregister("hubs")
        testers.unregister("tools")


# --- /update/settings stays as it was, and now says so ------------------------------------------


def _update_service(w: World) -> None:
    w.d.engine.update = SimpleNamespace(policy=Policy(),
                                        app=lambda: SimpleNamespace(dev_install=False))


def test_update_settings_keeps_its_shape_and_now_sends_settings_changed(w):
    _update_service(w)
    before = w.call("GET", "/update/settings").json()
    r = w.call("PUT", "/update/settings", json={"channel": "beta", "auto": "notify"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == set(before) == {"ok", "settings", "policy", "effective"}
    assert body["settings"] == {"channel": "beta", "auto": "notify"}
    assert set(body["effective"]) == set(before["effective"])
    assert w.changed() == [{"keys": ["updates.channel", "updates.auto"], "apply": "live",
                            "applies": {"live": ["updates.channel", "updates.auto"],
                                        "reopen": [], "restart": []}, "source": "api"}]
    row = w.call("GET", "/settings?key=updates.channel").json()["rows"][0]
    assert (row["value"], row["source"]) == ("beta", "user")         # one store, two views


def test_negative_twin_a_refused_update_settings_put_sends_nothing(w):
    _update_service(w)
    assert w.call("PUT", "/update/settings", json={"auto": "always"}).status_code == 400
    assert w.call("PUT", "/update/settings", json={}).status_code == 200
    assert w.changed() == []


# --- the Host allow-list ---------------------------------------------------------------------------


@pytest.fixture
def guarded(tmp_path: Path):
    eng = DemoEngine(speed=0)
    try:
        app = create_app(eng, token=TOKEN, state_dir=tmp_path / "state", static_dir=None,
                         allowed_hosts=allowed_hosts("127.0.0.1", ["hm.lab.example"]))
        with TestClient(app, base_url="http://127.0.0.1:40123") as client:
            yield client
    finally:
        eng.close_all()


def test_a_foreign_host_is_refused_before_any_route(guarded):
    for host in ("evil.example", "evil.example:40123", "127.0.0.1.evil.example", ""):
        r = guarded.get("/api/v1/settings", headers={**H, "host": host})
        assert r.status_code == 403, host
        assert r.json()["error"]["name"] == "REFUSED" and "host name" in r.json()["error"]["message"]
    r = guarded.get("/health", headers={"host": "evil.example"})
    assert r.status_code == 403
    from starlette.testclient import WebSocketDenialResponse
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with guarded.websocket_connect(f"/api/v1/events?token={TOKEN}",
                                       headers={"host": "evil.example"}) as ws:
            ws.receive_text()


def test_negative_twin_loopback_and_the_configured_name_pass(guarded):
    for host in ("127.0.0.1:40123", "localhost:40123", "[::1]:40123", "127.0.0.2",
                 "LOCALHOST", "hm.lab.example:8080"):
        r = guarded.get("/api/v1/settings/schema", headers={**H, "host": host})
        assert r.status_code == 200, host
    # (the test client's WebSocket says Host: testserver unless told)
    with guarded.websocket_connect(f"/api/v1/events?token={TOKEN}",
                                   headers={"host": "127.0.0.1:40123"}) as ws:
        ws.close()


def test_negative_twin_an_app_built_without_the_list_answers_any_host(w):
    assert w.c.get("/api/v1/settings/schema", headers={**H, "host": "evil.example"}
                   ).status_code == 200
