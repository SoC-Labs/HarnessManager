"""The T14 mock harness-manager-daemon against docs/API.md, and the calls the UI makes through it.

The mock is what the browser tests run against, so it must be the contract, not an
approximation: its route table equals API.md's, errors carry the CLI's JSON shape and
the HTTP status API.md assigns, long operations answer 202 and finish through job.*
events, and consoles and events stream over WebSockets. Each behaviour has a negative
twin (no token, a mismatching overlay, a held board, an unknown job).
"""

from __future__ import annotations

import json
import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_HELD, BOARD_USB, DemoEngine
from tests.fakes.l3_week_plan import EXTENSION_ROUTES
from tests.fakes.t14_api_contract import (
    api_md_sections,
    app_routes,
    daemon_routes,
    frozen_endpoints,
    lease_requests_md_endpoints,
    normalise,
    parse_lease_requests_md,
    ui_endpoints,
)
from tests.fakes.t14_lease_requests import LEASE_REQUEST_ROUTES
from tests.fakes.t14_mock_api import ADDITIVE_ROUTES, HTTP_STATUS, ROUTES, create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "t14-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def enc(board_id: str) -> str:
    return quote(board_id, safe="")


@pytest.fixture
def engine():
    eng = DemoEngine(speed=0.02)
    yield eng
    eng.close_all()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, token=TOKEN), raise_server_exceptions=False) as c:
        yield c


def probe_and_open(client, board_id):
    cands = client.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
    cand = next(c for c in cands if c["board_id"] == board_id)
    r = client.post("/api/v1/boards", json={"candidate": cand, "note": "t14"}, headers=AUTH)
    return r


def wait_job(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running after {timeout}s")


# --- the route table -------------------------------------------------------------------------


def test_the_mock_route_table_is_api_md_plus_lease_requests_md_plus_any_additive_routes():
    mock = {(m, normalise(p)) for m, p in ROUTES}
    additive = {(m, normalise(p)) for m, p in ADDITIVE_ROUTES}
    assert mock - additive == frozen_endpoints()
    assert not additive & frozen_endpoints()


def test_the_mock_serves_exactly_the_four_routes_lease_requests_md_adds():
    four = {(m, normalise(p)) for m, p in LEASE_REQUEST_ROUTES}
    assert four == lease_requests_md_endpoints()
    assert len(four) == 4
    # the twin: a fifth route in the doc's API table would not match
    extra = "## API\n| `POST /boards/{bid}/lease/steal` | `{}` | 202 |\n"
    assert parse_lease_requests_md(extra) - four == {("POST", "/boards/{}/lease/steal")}
    # and a CLI row or an event row is not a route
    assert parse_lease_requests_md("## API\n| `lease.wanted` | `{id}` | holder |\n"
                                   "## CLI\n| `POST /x` | y |\n") == set()


def landed_extensions() -> set[str]:
    """The week-plan extension modules present in this tree (lanes L1, L2, L4 land them)."""
    import importlib.util

    return {m for m in EXTENSION_ROUTES
            if importlib.util.find_spec(f"harness_manager.daemon.{m}") is not None}


def test_the_mock_serves_each_extension_modules_routes_as_api_md_assigns_them():
    sections = api_md_sections()
    assert set(sections) == {"core", *EXTENSION_ROUTES}
    # LEASE_REQUESTS.md's routes belong to hub_api; they count on both sides, so this holds
    # before and after the lead folds them into API.md.
    four = lease_requests_md_endpoints()
    for module, routes in EXTENSION_ROUTES.items():
        mock = {(m, normalise(p)) for m, p in routes}
        doc = set(sections[module])
        if module == "hub_api":
            mock |= {(m, normalise(p)) for m, p in LEASE_REQUEST_ROUTES}
            doc |= four
        assert mock == doc, module


def test_the_real_daemon_serves_the_core_plus_exactly_the_extensions_that_landed():
    # T13's daemon is the product. The week-plan modules land separately (the lead wires
    # them at merge), so assert what exists and never fake what does not.
    sections = api_md_sections()
    landed = landed_extensions()
    expected = set(sections["core"]).union(*(sections[m] for m in landed))
    assert daemon_routes() == expected, f"landed extensions: {sorted(landed) or 'none'}"


def test_the_route_check_notices_a_route_the_daemon_lacks():
    sections = api_md_sections()
    pretend = set(sections["core"]) | sections["power_api"]      # as if power_api had landed
    assert daemon_routes() != pretend or "power_api" in landed_extensions()


def test_the_mock_app_serves_every_route_it_declares(engine):
    app = create_app(engine, token=TOKEN, serve_ui=False)
    assert app_routes(app) == {(m, normalise(p)) for m, p in ROUTES}


def test_every_ui_endpoint_is_served_by_the_mock():
    served = {(m, normalise(p)) for m, p in ROUTES}
    assert {n: ep for n, ep in ui_endpoints().items() if ep not in served} == {}


def test_http_statuses_follow_the_api_md_table():
    from harness_manager.core.errors import ExitCode

    assert HTTP_STATUS[ExitCode.USAGE] == 400
    assert HTTP_STATUS[ExitCode.ABSENT] == 404
    assert HTTP_STATUS[ExitCode.HELD] == HTTP_STATUS[ExitCode.ALREADY] == 409
    assert HTTP_STATUS[ExitCode.UNREACHABLE] == 502
    assert HTTP_STATUS[ExitCode.UNAVAILABLE] == HTTP_STATUS[ExitCode.NOTHING_ON_TARGET] == 422
    assert HTTP_STATUS[ExitCode.INCOMPATIBLE] == HTTP_STATUS[ExitCode.REFUSED] == 409


# --- auth --------------------------------------------------------------------------------------


def test_health_needs_no_token_and_everything_else_does(client):
    health = client.get("/api/v1/health").json()
    assert health["ok"] is True and {"version", "pid", "service"} <= set(health)
    r = client.get("/api/v1/boards")
    assert r.status_code == 401
    assert r.json()["ok"] is False and r.json()["error"]["name"] == "REFUSED"
    r = client.get("/api/v1/boards", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert client.get("/api/v1/boards", headers=AUTH).status_code == 200


def refused(client, url):
    """How a refused WebSocket ends: an HTTP denial, or a close with 4000 + the exit code."""
    from starlette.testclient import WebSocketDenialResponse
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(url) as ws:
            ws.receive()
    except WebSocketDenialResponse as denial:
        return ("denied", denial.status_code, denial.json()["error"]["name"])
    except WebSocketDisconnect as exc:
        return ("closed", exc.code, "")
    return ("open", 0, "")


def test_websockets_refuse_a_missing_token(client):
    assert refused(client, "/api/v1/events") in (("denied", 401, "REFUSED"), ("closed", 4015, ""))


def test_static_ui_is_served_at_root_with_the_csp(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Harness Manager" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    js = client.get("/js/app.js")
    assert js.headers["content-type"].startswith("text/javascript")
    assert client.get("/vendor/xterm/xterm.module.js").headers["content-type"].startswith(
        "text/javascript")
    assert client.get("/vendor/fonts/ibm-plex-sans-latin-400-normal.woff2").headers[
        "content-type"] == "font/woff2"


# --- boards ------------------------------------------------------------------------------------


def test_probe_returns_candidates_with_the_identity_they_read(client):
    cands = client.post("/api/v1/probe", json={}, headers=AUTH).json()["candidates"]
    by_id = {c["board_id"]: c for c in cands}
    assert set(by_id) == {BOARD_FIELDED, BOARD_USB, BOARD_HELD}
    assert by_id[BOARD_FIELDED]["identity"]["build_check"] == "unchecked"
    assert by_id[BOARD_FIELDED]["links"][0]["kind"] == "ethernet"


def test_boards_lists_holders_and_open_state(client):
    probe_and_open(client, BOARD_USB)
    rows = {b["board_id"]: b for b in client.get("/api/v1/boards", headers=AUTH).json()["boards"]}
    assert rows[BOARD_USB]["open"] is True
    assert rows[BOARD_HELD]["open"] is False
    assert rows[BOARD_HELD]["holder"]["user"] == "alice"
    assert "holder" not in rows[BOARD_FIELDED]


def test_open_then_info_serialises_like_the_cli(client):
    r = probe_and_open(client, BOARD_FIELDED)
    assert r.status_code == 200 and r.json()["board_id"] == BOARD_FIELDED
    info = client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}", headers=AUTH).json()
    assert info["identity"]["build_check"] == "unchecked"
    assert "reboot_board" in info["unavailable"]
    assert info["unavailable"]["reboot_board"].startswith("needs the Debug USB cable")
    assert isinstance(info["capabilities"], list) and info["capabilities"] == sorted(info["capabilities"])


def test_opening_a_held_board_is_409_and_names_the_holder(client):
    r = probe_and_open(client, BOARD_HELD)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["name"] == "HELD" and err["code"] == 4 and "alice" in err["holder"]


def test_info_of_a_board_not_open_is_404_absent(client):
    r = client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}", headers=AUTH)
    assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"


def test_close_releases_the_board(client):
    probe_and_open(client, BOARD_USB)
    assert client.delete(f"/api/v1/boards/{enc(BOARD_USB)}", headers=AUTH).json()["ok"]
    assert client.get(f"/api/v1/boards/{enc(BOARD_USB)}", headers=AUTH).status_code == 404


def test_telemetry_never_zero_fills(client):
    probe_and_open(client, BOARD_FIELDED)
    readings = client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/telemetry", headers=AUTH).json()["readings"]
    gone = [r for r in readings if not r["available"]]
    assert gone and all(r["value"] is None and r["reason"] for r in gone)


# --- deploy --------------------------------------------------------------------------------------


def test_overlays_and_a_mismatching_preflight_carry_the_refusal(client, engine):
    probe_and_open(client, BOARD_USB)
    bid = enc(BOARD_USB)
    ov = client.get(f"/api/v1/boards/{bid}/overlays", headers=AUTH).json()
    assert "nanosoc_multicore" in ov["blocked"]
    assert {o["name"] for o in ov["loadable"]} >= {"greybox", "nanosoc", "led"}
    bad = client.post(f"/api/v1/boards/{bid}/preflight", json={"overlay": "nanosoc_multicore"},
                      headers=AUTH).json()
    assert any(i["check"] == "mismatch" for i in bad["items"])
    assert bad["refusal"]["code"] in (14, 15)
    good = client.post(f"/api/v1/boards/{bid}/preflight", json={"overlay": "led"}, headers=AUTH).json()
    assert "refusal" not in good
    assert engine.called("deploy.deploy") == []


def test_deploy_is_a_job_that_finishes_with_the_result(client, engine):
    probe_and_open(client, BOARD_USB)
    r = client.post(f"/api/v1/boards/{enc(BOARD_USB)}/deploy", json={"overlay": "led"}, headers=AUTH)
    assert r.status_code == 202
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done"
    assert job["result"]["verified"] is True and job["result"]["rm_id"] == "0x0100001e"
    assert len(engine.called("deploy.deploy")) == 1


def test_a_refused_deploy_is_409_with_the_preflight_and_no_job(client, engine):
    probe_and_open(client, BOARD_USB)
    r = client.post(f"/api/v1/boards/{enc(BOARD_USB)}/deploy",
                    json={"overlay": "nanosoc_multicore"}, headers=AUTH)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] in (14, 15) and err["data"]["overlay"]["name"] == "nanosoc_multicore"
    assert any(i["check"] == "mismatch" for i in err["data"]["preflight"])
    assert client.get("/api/v1/jobs", headers=AUTH).json()["jobs"] == []
    assert engine.called("deploy.deploy") == []


@pytest.mark.parametrize("spec", ["led", "0x0100001e", {"name": "led", "rm_id": "0x0100001e"}])
def test_an_overlay_is_named_by_name_rm_id_or_object(client, spec):
    probe_and_open(client, BOARD_USB)
    r = client.post(f"/api/v1/boards/{enc(BOARD_USB)}/preflight", json={"overlay": spec}, headers=AUTH)
    assert r.json()["overlay"]["name"] == "led"


def test_while_a_job_runs_the_board_is_held_and_named(client, engine):
    engine.delays["deploy.deploy"] = 1.0
    probe_and_open(client, BOARD_USB)
    bid = enc(BOARD_USB)
    job = client.post(f"/api/v1/boards/{bid}/deploy", json={"overlay": "led"}, headers=AUTH).json()["job"]
    r = client.get(f"/api/v1/boards/{bid}", headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["name"] == "HELD"
    assert job in r.json()["error"]["holder"]
    assert client.get(f"/api/v1/boards/{bid}/session", headers=AUTH).json()["job"] == job
    rows = {b["board_id"]: b for b in client.get("/api/v1/boards", headers=AUTH).json()["boards"]}
    assert rows[BOARD_USB]["job"] == job and "job" not in rows[BOARD_FIELDED]
    wait_job(client, job)
    assert client.get(f"/api/v1/boards/{bid}", headers=AUTH).status_code == 200   # freed


def test_session_lists_adapters_and_reset_targets(client):
    probe_and_open(client, BOARD_USB)
    s = client.get(f"/api/v1/boards/{enc(BOARD_USB)}/session", headers=AUTH).json()
    assert s["adapters"]["controller"] is True and s["adapters"]["clocks"] is False
    assert s["reset_targets"] == ["dut"] and s["job"] is None


def test_opening_an_open_board_is_409_already(client):
    probe_and_open(client, BOARD_USB)
    r = probe_and_open(client, BOARD_USB)
    assert r.status_code == 409 and r.json()["error"]["name"] == "ALREADY"


def test_open_keeps_the_session_when_the_first_read_fails(client, engine):
    from harness_manager.core.errors import UnreachableError

    engine.failures["info"] = UnreachableError("shell did not reply", hint="check the board")
    r = probe_and_open(client, BOARD_USB)
    body = r.json()
    assert r.status_code == 200 and body["info"] is None
    assert body["info_error"]["name"] == "UNREACHABLE"
    assert BOARD_USB in engine.open_boards()


def test_an_unknown_job_is_404(client):
    r = client.get("/api/v1/jobs/nope", headers=AUTH)
    assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"


# --- events and consoles over WebSockets --------------------------------------------------------


def test_event_socket_streams_deploy_and_job_events_for_the_topics_asked(client):
    probe_and_open(client, BOARD_USB)
    topics = []
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=deploy.*,job.*") as ws:
        client.post(f"/api/v1/boards/{enc(BOARD_USB)}/deploy", json={"overlay": "led"}, headers=AUTH)
        while "job.done" not in topics:
            frame = json.loads(ws.receive_text())
            assert set(frame) == {"topic", "board_id", "data", "at"}
            topics.append(frame["topic"])
    assert topics[0] in ("job.started", "deploy.started")
    assert "deploy.done" in topics and "deploy.progress" in topics
    assert all(t.startswith(("deploy.", "job.")) for t in topics)


def test_console_socket_carries_bytes_both_ways(client, engine):
    probe_and_open(client, BOARD_USB)
    url = f"/api/v1/boards/{enc(BOARD_USB)}/consoles/uart0?token={TOKEN}"
    with client.websocket_connect(url) as ws:
        first = json.loads(ws.receive_text())
        assert first == {"state": "up", "name": "uart0", "detail": ""}
        deadline = time.monotonic() + 5
        while engine.inject_console(BOARD_USB, "uart0", "boot banner\r\n") == 0:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        got = b""
        while b"boot banner" not in got:
            msg = ws.receive()
            got += msg.get("bytes") or b""
        ws.send_bytes(b"help()\r\n")
        deadline = time.monotonic() + 5
        while (BOARD_USB, "uart0", b"help()\r\n") not in engine.consoles.writes:
            assert time.monotonic() < deadline
            time.sleep(0.02)


def test_console_socket_for_an_unknown_console_is_refused_with_absent(client):
    probe_and_open(client, BOARD_USB)
    got = refused(client, f"/api/v1/boards/{enc(BOARD_USB)}/consoles/nope?token={TOKEN}")
    assert got in (("denied", 404, "ABSENT"), ("closed", 4003, ""))


# --- debug, controller, storage ------------------------------------------------------------------


def test_debug_detect_up_down(client):
    probe_and_open(client, BOARD_USB)
    bid = enc(BOARD_USB)
    assert client.post(f"/api/v1/boards/{bid}/debug/detect", headers=AUTH).json()["idcode"] == "0x6ba00477"
    job = wait_job(client, client.post(f"/api/v1/boards/{bid}/debug/up", headers=AUTH).json()["job"])
    assert job["result"]["state"] == "up" and job["result"]["gdb_port"] > 0
    assert client.get(f"/api/v1/boards/{bid}/debug", headers=AUTH).json()["state"] == "up"
    assert client.post(f"/api/v1/boards/{bid}/debug/down", headers=AUTH).json()["state"] == "down"


def test_detect_on_a_design_without_a_debug_port_is_422(client):
    probe_and_open(client, BOARD_FIELDED)        # the greybox has no DAP
    r = client.post(f"/api/v1/boards/{enc(BOARD_FIELDED)}/debug/detect", headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["name"] == "NOTHING_ON_TARGET"


def test_reboot_without_the_usb_link_is_unavailable_with_its_reason(client, engine):
    probe_and_open(client, BOARD_FIELDED)
    r = client.post(f"/api/v1/boards/{enc(BOARD_FIELDED)}/controller/reboot", json={}, headers=AUTH)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["name"] == "UNAVAILABLE" and err["capability"] == "reboot_board"
    assert "Debug USB" in err["reason"]
    assert engine.called("controller.reboot") == []


def test_reboot_with_the_usb_link_is_a_job_with_phases(client):
    probe_and_open(client, BOARD_USB)
    r = client.post(f"/api/v1/boards/{enc(BOARD_USB)}/controller/reboot", json={"wait_s": 5}, headers=AUTH)
    assert r.status_code == 202
    assert wait_job(client, r.json()["job"])["state"] == "done"


def test_storage_pending_and_restore(client, engine, tmp_path):
    backup = tmp_path / "b.zip"
    backup.write_bytes(b"PK\x05\x06" + b"\x00" * 18)          # an empty zip
    engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                      "backup": {"path": str(backup)}})
    probe_and_open(client, BOARD_USB)
    bid = enc(BOARD_USB)
    assert client.get(f"/api/v1/boards/{bid}/storage/pending", headers=AUTH).json()["pending"]["op"] == "install"
    missing = client.post(f"/api/v1/boards/{bid}/storage/restore",
                          json={"backup_path": str(tmp_path / "gone.zip")}, headers=AUTH)
    assert missing.status_code == 404 and engine.called("storage.restore") == []
    r = client.post(f"/api/v1/boards/{bid}/storage/restore", json={"backup_path": str(backup)}, headers=AUTH)
    assert wait_job(client, r.json()["job"])["state"] == "done"
    assert client.get(f"/api/v1/boards/{bid}/storage/pending", headers=AUTH).json()["pending"] is None


def test_storage_pending_is_null_without_a_storage_link(client):
    probe_and_open(client, BOARD_FIELDED)
    assert client.get(f"/api/v1/boards/{enc(BOARD_FIELDED)}/storage/pending", headers=AUTH).json()["pending"] is None


def test_help_tabs_come_from_the_cli(client):
    tabs = client.get("/api/v1/help/tabs", headers=AUTH).json()["tabs"]
    assert tabs and all(set(t) == {"name", "text"} for t in tabs)


def test_daemon_shutdown_is_refused_while_a_job_runs(client, engine):
    engine.delays["deploy.deploy"] = 1.0
    probe_and_open(client, BOARD_USB)
    job = client.post(f"/api/v1/boards/{enc(BOARD_USB)}/deploy", json={"overlay": "led"},
                      headers=AUTH).json()["job"]
    r = client.post("/api/v1/daemon/shutdown", json={}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["name"] == "HELD"
    wait_job(client, job)
    # With no job, the mock says it cannot stop itself (it was not started by `harness-manager daemon`).
    assert client.post("/api/v1/daemon/shutdown", json={}, headers=AUTH).status_code == 422
