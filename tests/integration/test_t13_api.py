"""Team T13: the harness-manager-daemon API on the virtual MPS3, through FastAPI's TestClient.

The app wraps T1's real Engine with the MPS3 pack pointed at ``VirtualMps3``
(FakeShell pinned to the fielded firmware) by ``EngineConfig.pack_overrides``.
Every check has a negative twin. Only 127.0.0.1 is reached.
"""

from __future__ import annotations

import json
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest

with warnings.catch_warnings():
    # starlette 1.x: "Using `httpx` with `starlette.testclient` is deprecated" (a UserWarning).
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketDenialResponse

from harness_manager.cli.output import jsonable
from harness_manager.core.errors import ExitCode
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.daemon.app import create_app
from harness_manager_mps3 import mcc as mccmod
from tests.fakes.t2_overlays import OTHER_STATIC_ID, SYNTH2_RM_ID, make_overlay, use_overlay_dirs
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3

H = headers()


@pytest.fixture
def overlays(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "ov"
    make_overlay(root, "synth")
    make_overlay(root, "alien", rm_id=SYNTH2_RM_ID, static_id=OTHER_STATIC_ID)
    use_overlay_dirs(monkeypatch, root)
    return root


@pytest.fixture
def engine(vboard: VirtualMps3):
    eng = engine_for(vboard)
    yield eng
    eng.close_all()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine, token=TOKEN, static_dir=None)) as c:
        yield c


def open_board(client: TestClient, vb: VirtualMps3, note: str = "t13") -> str:
    r = client.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": note}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def events_until(ws, topic: str, job: str | None = None) -> list[dict]:
    frames = []
    while True:
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if frame["topic"] == topic and (job is None or frame["data"].get("job") == job):
            return frames


# -- auth ------------------------------------------------------------------------------------------


def test_health_needs_no_token(client):
    for path in ("/api/v1/health", "/health"):
        r = client.get(path)
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["version"]


@pytest.mark.parametrize("auth", [None, "Bearer wrong-token", f"Basic {TOKEN}", "Bearer "])
def test_negative_twin_the_api_refuses_a_missing_or_wrong_token(client, auth):
    r = client.get("/api/v1/packs", headers={"Authorization": auth} if auth else {})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    err = r.json()["error"]
    assert r.json()["ok"] is False and err["code"] == ExitCode.REFUSED and "token" in err["message"]


def test_the_right_token_is_accepted(client):
    r = client.get("/api/v1/packs", headers=H)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["packs"] == {"mps3": "Arm MPS3 (V2M-MPS3, HBI0309C)"}
    # T14-1: the pack's capability titles and hints, so front-ends never mirror them.
    caps = {c["name"]: c for c in body["capabilities"]["mps3"]}
    assert caps["power_cycle"]["title"] == "Power-cycle the board (cold)"
    assert caps["power_cycle"]["needs_hint"].startswith("needs ")
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("query", ["", "?token=wrong"])
def test_websockets_refuse_a_missing_or_wrong_token(client, query):
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(f"/api/v1/events{query}"):
            pass
    assert denied.value.status_code == 401
    assert denied.value.json()["error"]["code"] == ExitCode.REFUSED


def test_negative_twin_a_websocket_with_the_token_is_accepted(client):
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*"):
        pass


# -- the full flow -----------------------------------------------------------------------------------


def test_probe_open_info_overlays_preflight_deploy_reset_close(client, vboard, overlays):
    # probe
    r = client.post("/api/v1/probe", json={"hosts": [vboard.shell_endpoint], "scan_usb": False},
                    headers=H)
    (cand,) = r.json()["candidates"]
    assert cand["identity"]["rm_name"] == "greybox"
    # open, from the probed candidate
    r = client.post("/api/v1/boards", json={"candidate": cand, "note": "flow"}, headers=H)
    body = r.json()
    bid = body["board_id"]
    assert r.status_code == 200 and body["info"]["identity"]["shell_id"] == "0x3f1a560f"
    B = bid_path(bid)
    # info is BoardInfo, as `harness-manager --json info` prints it
    info = client.get(B, headers=H).json()
    assert set(info) == {"ok", "candidate", "identity", "health", "capabilities", "unavailable"}
    assert "reboot_board" in info["unavailable"]
    # the lock is held by this process, once, and the note is the daemon's
    holder = client.get(f"{B}/lock", headers=H).json()["holder"]
    assert holder["note"] == "harness-manager-daemon: flow"
    listed = client.get("/api/v1/boards", headers=H).json()["boards"]
    assert [(b["board_id"], b["open"]) for b in listed] == [(bid, True)]
    # overlays: one loads, one is blocked with its reason
    ov = client.get(f"{B}/overlays", headers=H).json()
    assert [o["name"] for o in ov["loadable"]] == ["synth"]
    assert "shell_id matches" in ov["blocked"]["alien"]
    assert {o["name"] for o in ov["overlays"]} == {"synth", "alien"}
    # preflight of the wrong-shell overlay: the refusal, INCOMPATIBLE (14)
    pf = client.post(f"{B}/preflight", json={"overlay": "alien"}, headers=H).json()
    assert pf["refusal"]["code"] == ExitCode.INCOMPATIBLE
    assert any(i["check"] == "mismatch" and i["identity"] for i in pf["items"])
    ok_pf = client.post(f"{B}/preflight", json={"overlay": "synth"}, headers=H).json()
    assert "refusal" not in ok_pf
    # deploying it is refused BEFORE any job: 409 with the envelope, board untouched
    r = client.post(f"{B}/deploy", json={"overlay": "alien"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == ExitCode.INCOMPATIBLE
    assert r.json()["error"]["data"]["overlay"]["name"] == "alien"
    assert vboard.shell.accepted_pushes == [] and vboard.shell.current_rm_id == 0
    # deploy the right one: a job, whose progress arrives as events
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*,deploy.*") as ws:
        r = client.post(f"{B}/deploy", json={"overlay": "synth"}, headers=H)
        assert r.status_code == 202
        job = r.json()["job"]
        frames = events_until(ws, "job.done", job)
    topics = [f["topic"] for f in frames]
    assert topics[0] == "job.started" and "job.progress" in topics and "deploy.done" in topics
    assert topics.index("deploy.done") < topics.index("job.done")
    progress = [f["data"] for f in frames if f["topic"] == "job.progress"]
    assert {p["phase"] for p in progress} >= {"push", "verify"}
    assert frames[-1]["data"]["result"]["verified"] is True
    state = client.get(f"/api/v1/jobs/{job}", headers=H).json()
    assert state["state"] == "done" and state["result"]["rm_id"] == "0x01007a57"
    assert client.get(B, headers=H).json()["identity"]["rm_id"] == "0x01007a57"
    # reset the DUT; a target the board does not have is a usage error
    assert client.post(f"{B}/reset", json={"target": "dut"}, headers=H).json()["result"] == "done"
    bad = client.post(f"{B}/reset", json={"target": "board"}, headers=H)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == ExitCode.USAGE
    # close
    assert client.delete(B, headers=H).json() == {"ok": True, "board_id": bid, "closed": True}
    assert client.get(f"{B}/lock", headers=H).json()["holder"] is None
    gone = client.get(B, headers=H)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == ExitCode.ABSENT


def test_a_failing_job_reports_its_error(client, vboard, overlays):
    bid = open_board(client, vboard)
    B = bid_path(bid)
    with client.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*") as ws:
        r = client.post(f"{B}/restore", headers=H)      # no greybox overlay: ABSENT in the job
        job = r.json()["job"]
        frames = events_until(ws, "job.failed", job)
    assert frames[-1]["data"]["error"]["code"] == ExitCode.ABSENT
    state = client.get(f"/api/v1/jobs/{job}", headers=H).json()
    assert state["state"] == "failed" and "result" not in state
    listed = client.get("/api/v1/jobs", headers=H).json()["jobs"]
    assert [j["job"] for j in listed] == [job]
    missing = client.get("/api/v1/jobs/nope", headers=H)
    assert missing.status_code == 404


# -- statuses ------------------------------------------------------------------------------------------


def test_error_statuses_follow_the_table(client, vboard, engine):
    B = bid_path(f"mps3@{vboard.shell_endpoint}")
    r = client.get(B, headers=H)                           # not open: ABSENT
    assert (r.status_code, r.json()["error"]["name"]) == (404, "ABSENT")
    bid = open_board(client, vboard)
    r = client.post("/api/v1/boards", json={"target": vboard.shell_endpoint}, headers=H)
    assert (r.status_code, r.json()["error"]["name"]) == (409, "ALREADY")
    r = client.get(f"{bid_path(bid)}/controller/temps", headers=H)     # no Debug USB
    assert (r.status_code, r.json()["error"]["name"]) == (422, "UNAVAILABLE")
    assert r.json()["error"]["capability"] == "console_controller"
    assert "USB" in r.json()["error"]["reason"]
    r = client.post("/api/v1/boards", json={}, headers=H)
    assert (r.status_code, r.json()["error"]["name"]) == (400, "USAGE")
    r = client.post("/api/v1/boards", content=b"[1, 2]", headers={**H, "content-type":
                                                                   "application/json"})
    assert (r.status_code, r.json()["error"]["name"]) == (400, "USAGE")
    r = client.post("/api/v1/boards", content=b"{not json", headers={**H, "content-type":
                                                                      "application/json"})
    assert r.status_code == 400 and r.json()["ok"] is False
    r = client.get("/api/v1/no/such/thing", headers=H)
    assert (r.status_code, r.json()["error"]["name"]) == (404, "ABSENT")
    # the board held by ANOTHER engine (another process, in real life): HELD, holder named
    engine.close(bid)
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine

    other = Engine(EngineConfig(state_dir=engine.state_dir))
    other.open(other.candidate_for(vboard.shell_endpoint), note="someone else")
    try:
        r = client.post("/api/v1/boards", json={"target": vboard.shell_endpoint}, headers=H)
        assert (r.status_code, r.json()["error"]["name"]) == (409, "HELD")
        assert "someone else" in r.json()["error"]["holder"]
    finally:
        other.close_all()


def test_the_cli_hold_tag_never_reaches_the_daemons_lock(client, vboard):
    bid = open_board(client, vboard, note="[cli-hold] console uart0")
    note = client.get(f"{bid_path(bid)}/lock", headers=H).json()["holder"]["note"]
    assert note == "harness-manager-daemon: console uart0"          # `detach` must never signal the daemon


# -- the board gate ---------------------------------------------------------------------------------------


def test_a_board_with_a_running_job_refuses_other_requests(client, vboard):
    bid = open_board(client, vboard)
    daemon = client.app.state.daemon
    release = threading.Event()
    job = daemon.jobs.submit("deploy", bid, lambda progress: release.wait(10))
    try:
        r = client.get(bid_path(bid), headers=H)
        assert r.status_code == 409 and job.id in r.json()["error"]["message"]
        r = client.delete(bid_path(bid), headers=H)
        assert r.status_code == 409
        r = client.post("/api/v1/probe", json={"hosts": [vboard.shell_endpoint]}, headers=H)
        assert r.status_code == 409
        # requests that do not touch the board still answer
        assert client.get(f"{bid_path(bid)}/lock", headers=H).status_code == 200
        assert client.get(f"/api/v1/jobs/{job.id}", headers=H).json()["state"] == "running"
    finally:
        release.set()
    assert job.finished.wait(10)
    # negative twin: once the job ends, the same request is served
    assert client.get(bid_path(bid), headers=H).status_code == 200


# -- lab verbs --------------------------------------------------------------------------------------------


def test_lab_verbs_return_the_clis_json(client, vboard):
    B = bid_path(open_board(client, vboard))
    r = client.post(f"{B}/lab/link", json={"event": "pulse"}, headers=H).json()
    assert r["event"] == "pulse" and r["result"] == "done"
    r = client.post(f"{B}/lab/display", json={}, headers=H).json()
    assert r["requested"] == "query" and r["owner"] == "harness"
    r = client.post(f"{B}/lab/macgen", json={"gen": True, "chk": True}, headers=H).json()
    assert {"tx", "rx", "err"} <= set(r)


@pytest.mark.parametrize("verb, body", [
    ("link", {"event": "sideways"}), ("link", {}), ("display", {"owner": "cat"}),
    ("display", {"timeout": -1}), ("macgen", {"gen": "yes"}), ("dutrx", {"frames": "two"}),
    ("teleport", {}),
])
def test_negative_twin_bad_lab_requests_are_usage_errors(client, vboard, verb, body):
    B = bid_path(open_board(client, vboard))
    r = client.post(f"{B}/lab/{verb}", json=body, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE


# -- consoles ---------------------------------------------------------------------------------------------


def test_the_console_websocket_carries_bytes_both_ways(client, vboard):
    bid = open_board(client, vboard)
    assert client.get(f"{bid_path(bid)}/consoles", headers=H).json()["names"] == [
        "swo", "uart0", "uart1"]
    with client.websocket_connect(f"/api/v1/boards/{quote(bid, safe='')}/consoles/uart0"
                                  f"?token={TOKEN}") as ws:
        assert json.loads(ws.receive_text())["state"] in ("up", "connecting")
        got = b""
        while b"nanosoc boot\n" not in got:
            message = ws.receive()
            got += message.get("bytes") or b""
        ws.send_bytes(b"echo me\n")
        while b"echo me\n" not in got:
            message = ws.receive()
            got += message.get("bytes") or b""


def test_negative_twin_a_console_the_board_lacks_is_refused(client, vboard):
    bid = open_board(client, vboard)
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(f"/api/v1/boards/{quote(bid, safe='')}/consoles/nope"
                                      f"?token={TOKEN}"):
            pass
    assert denied.value.status_code == 404 and denied.value.json()["error"]["name"] == "ABSENT"


def test_a_console_export_answers_with_its_port(client, vboard):
    bid = open_board(client, vboard)
    r = client.post(f"{bid_path(bid)}/consoles/uart0/export", json={}, headers=H).json()
    assert r["host"] == "127.0.0.1" and r["port"] > 0
    bad = client.post(f"{bid_path(bid)}/consoles/uart0/export", json={"port": 99999}, headers=H)
    assert bad.status_code == 400


# -- the web UI ---------------------------------------------------------------------------------------------


def test_without_the_ui_package_slash_says_so(client):
    r = client.get("/")
    assert r.status_code == 200 and "not installed" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_negative_twin_an_installed_ui_is_served_at_slash(engine, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>the T14 ui</html>")
    (static / "app.js").write_text("console.log(1)")
    with TestClient(create_app(engine, token=TOKEN, static_dir=static)) as c:
        assert "the T14 ui" in c.get("/").text and "not installed" not in c.get("/").text
        assert c.get("/app.js").status_code == 200
        assert c.get("/api/v1/packs").status_code == 401        # the API is still guarded


# -- a USB board: ids with '/', the controller and the SD as jobs ------------------------------------------


@pytest.fixture
def usb_board(tmp_path: Path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    with VirtualMps3(tmp_path / "usb", usb=True) as vb:
        vb.mcc.clock = clock
        vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        counted = vb.mcc.on_reboot
        vb.mcc.on_reboot = lambda: (counted(), vb.shell.stop())
        vb.mcc.on_boot = vb.shell.start
        yield vb


def usb_candidate(vb: VirtualMps3) -> Candidate:
    base = vb.candidate(ethernet=True, usb=True)
    return Candidate(pack="mps3", board_id=f"mps3@usb:{vb.mcc_url}/x", links=base.links,
                     label="virtual MPS3 over USB", evidence="test")


def test_a_usb_board_id_with_slashes_routes_and_its_jobs_run(usb_board, tmp_path):
    eng = engine_for(usb_board)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            cand = usb_candidate(usb_board)
            assert "/" in cand.board_id
            r = c.post("/api/v1/boards", json={"candidate": jsonable(cand)}, headers=H)
            assert r.status_code == 200, r.text
            B = bid_path(cand.board_id)
            session = c.get(f"{B}/session", headers=H).json()
            assert session["adapters"]["controller"] and session["adapters"]["storage"]
            osc = c.get(f"{B}/controller/osc", headers=H).json()["readings"]
            assert [x["value"] for x in osc] == [25.0, 50.0, 50.0, 50.0, 24.576, 23.75]
            assert c.get(f"{B}/storage/pending", headers=H).json()["pending"] is None
            with c.websocket_connect(f"/api/v1/boards/{quote(cand.board_id, safe='')}"
                                     f"/consoles/uart0?token={TOKEN}") as ws:
                assert json.loads(ws.receive_text())["name"] == "uart0"
            # SD backup is a job; its result is the BackupRecord
            dest = tmp_path / "backups"
            with c.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*") as ws:
                job = c.post(f"{B}/storage/backup", json={"dest_dir": str(dest)},
                             headers=H).json()["job"]
                frames = events_until(ws, "job.done", job)
            record = frames[-1]["data"]["result"]
            assert Path(record["path"]).parent == dest and len(record["sha256"]) == 64
            # restore from it: "/storage/restore" must not be routed as "/restore"
            with c.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*") as ws:
                r = c.post(f"{B}/storage/restore", json={"backup_path": record["path"]},
                           headers=H)
                assert r.status_code == 202, r.text
                frames = events_until(ws, "job.done", r.json()["job"])
            assert frames[-1]["data"]["result"]["backup"]["sha256"] == record["sha256"]
            # a reboot is a job whose progress is the witness phases; with no wait_s the
            # pack picks the wait from the harness (T12: 120 s bare-metal, 300 s Linux)
            with c.websocket_connect(f"/api/v1/events?token={TOKEN}&topics=job.*") as ws:
                job = c.post(f"{B}/controller/reboot", json={}, headers=H).json()["job"]
                frames = events_until(ws, "job.done", job)
            phases = [f["data"]["phase"] for f in frames if f["topic"] == "job.progress"]
            assert phases == ["sent", "down", "up"] and usb_board.reboots == 1
            assert c.get(f"/api/v1/jobs/{job}", headers=H).json()["phases"] == phases
    finally:
        eng.close_all()


def test_negative_twin_sd_requests_need_absolute_existing_paths(usb_board):
    eng = engine_for(usb_board)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            cand = usb_candidate(usb_board)
            c.post("/api/v1/boards", json={"candidate": jsonable(cand)}, headers=H)
            B = bid_path(cand.board_id)
            r = c.post(f"{B}/storage/backup", json={"dest_dir": "relative/dir"}, headers=H)
            assert r.status_code == 400 and "absolute" in r.json()["error"]["message"]
            r = c.post(f"{B}/storage/install", json={"files": {"MB/x.txt": "/no/such/file"},
                                                     "backup_path": "/no/such.zip"}, headers=H)
            assert r.status_code == 404
            r = c.post(f"{B}/storage/install", json={"files": {"MB/fw.ebf": "/etc/hostname"},
                                                     "backup_path": "/no/such.zip"}, headers=H)
            assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
            for wait in (-1, 0, "soon"):
                r = c.post(f"{B}/controller/reboot", json={"wait_s": wait}, headers=H)
                assert r.status_code == 400, wait
            assert usb_board.reboots == 0
    finally:
        eng.close_all()


def test_an_ethernet_only_candidate_lacks_the_usb_adapters(client, vboard):
    bid = open_board(client, vboard)
    adapters = client.get(f"{bid_path(bid)}/session", headers=H).json()["adapters"]
    assert adapters["deploy"] and adapters["shell"] and not adapters["controller"]
    assert not adapters["storage"]


def test_links_given_by_the_client_reach_the_board(client, vboard):
    # target + serial/volume builds the same candidate the CLI builds with --serial/--volume
    r = client.post("/api/v1/boards", json={"target": vboard.shell_endpoint,
                                            "serial": ["fake://nothing-here"]}, headers=H)
    links = r.json()["info"]["candidate"]["links"]
    assert [lk["kind"] for lk in links] == [LinkKind.ETHERNET.value, LinkKind.USB_SERIAL.value]
    assert Link(LinkKind.USB_SERIAL, "fake://nothing-here", "given with serial").address == \
        links[1]["address"]
