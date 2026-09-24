"""The kit routes on the real daemon (``daemon/kit_api.py``, loaded through
``app.EXTENSIONS``) over ``VirtualMps3``, and the same routes in the T14 mock.

The ILA-mint virtual board runs 0x72BB0A36 (the fixture kit's static); the default one
runs the previous static 0x3F1A560F. Vivado discovery is off: nothing is run. Every route
has a failing twin.
"""

from __future__ import annotations

import io
import json
import time
import warnings
import zipfile
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.daemon import app as daemon_app
from harness_manager.services.kit import vivado
from tests.fakes import kit_fakes as kf
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3, ila_mint_bake_profile

H = headers()
SID = "0x72BB0A36"


@pytest.fixture(autouse=True)
def _no_vivado(monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")


def client_for(vb: VirtualMps3, events: list | None = None) -> Iterator[tuple[TestClient, str]]:
    eng = engine_for(vb)
    if events is not None:
        eng.bus.subscribe("kit.*", events.append)
    try:
        with TestClient(daemon_app.create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "kit"},
                       headers=H)
            assert r.status_code == 200, r.text
            yield c, r.json()["board_id"]
    finally:
        eng.close_all()


@pytest.fixture
def events() -> list:
    return []


@pytest.fixture
def fielded(tmp_path, events) -> Iterator[tuple[TestClient, str]]:
    with VirtualMps3(tmp_path / "ila", profile=ila_mint_bake_profile()) as vb:
        yield from client_for(vb, events)


@pytest.fixture
def old(vboard) -> Iterator[tuple[TestClient, str]]:
    yield from client_for(vboard)


def wait(c: TestClient, job: str, timeout: float = 20.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = c.get(f"/api/v1/jobs/{job}", headers=H).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job} still running")


def imported(c: TestClient) -> dict:
    r = c.post("/api/v1/kits/import", json={"path": str(kf.FIXTURE)}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_import_list_get_export_zip(fielded, tmp_path, events):
    c, _ = fielded
    assert c.get("/api/v1/kits", headers=H).json()["kits"] == []
    body = imported(c)
    assert body["kit"]["kit_id"] == "mps3/0x72BB0A36/vivado-2024.1" and body["already"] is False
    assert [e.topic for e in events] == ["kit.stored"]
    kits = c.get("/api/v1/kits", headers=H).json()
    assert [k["static_id"] for k in kits["kits"]] == [SID]
    assert [s["name"] for s in kits["sources"]] == ["cache", "channel", "hub"]
    one = c.get("/api/v1/kits/0x72bb0a36", headers=H).json()
    assert one["manifest"]["rp"]["clr_max"] == 262144
    assert {x["name"]: x["state"] for x in one["checks"]} == {"files": "ok", "static_id": "ok"}
    out = tmp_path / "exported"
    r = c.post(f"/api/v1/kits/{SID}/export", json={"out_dir": str(out)}, headers=H)
    assert r.status_code == 200 and (out / "static" / "static_routed_locked.dcp").is_file()
    z = c.get(f"/api/v1/kits/{SID}/zip", headers=H)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert f"{SID}/kit.json" in zf.namelist()
    # the twins
    assert c.get("/api/v1/kits/0x3F1A560F", headers=H).status_code == 404
    assert c.get("/api/v1/kits/banana", headers=H).status_code == 400
    r = c.post(f"/api/v1/kits/{SID}/export", json={"out_dir": "relative/dir"}, headers=H)
    assert r.status_code == 400 and "absolute" in r.json()["error"]["message"]
    assert c.get("/api/v1/kits").status_code == 401


def test_fetch_is_a_job_with_kit_events(fielded, tmp_path, monkeypatch, events):
    c, _ = fielded
    r = c.post("/api/v1/kits/fetch", json={"static_id": SID}, headers=H)
    assert r.status_code == 202
    failed = wait(c, r.json()["job"])
    assert failed["state"] == "failed" and failed["error"]["name"] == "ABSENT"
    hub = tmp_path / "mints"
    kf.fielded_dir(hub / SID)
    r = c.post("/api/v1/kits/fetch", json={"static_id": SID, "source": str(hub / SID)},
               headers=H)
    done = wait(c, r.json()["job"])
    assert done["state"] == "done", done
    assert done["result"]["source"] == "path" and done["result"]["kit"]["static_id"] == SID
    topics = [e.topic for e in events]
    assert "kit.progress" in topics and topics[-1] == "kit.stored"


def test_the_board_kit_route_and_its_other_static_twin(fielded, old):
    c, bid = fielded
    imported(c)
    b = c.get(bid_path(bid) + "/kit", headers=H).json()
    assert b["static_id"] == SID and b["cached"] is True
    states = {x["name"]: x["state"] for x in b["checks"]}
    assert states["shell_id"] == "ok" and states["vivado"] == "warning"   # discovery is off
    c2, bid2 = old
    b2 = c2.get(bid_path(bid2) + "/kit", headers=H).json()
    assert b2["static_id"] == "0x3F1A560F" and b2["cached"] is False and b2["profile"] is None


def test_the_guide_with_and_without_a_board(fielded, old):
    c, bid = fielded
    g = c.get("/api/v1/guide", params={"static_id": SID}, headers=H).json()
    assert [s["id"] for s in g["steps"]] == ["target", "tools", "kit", "wrapper", "build",
                                             "check"]
    assert g["steps"][2]["state"] == "next"                  # not cached yet: fetch it
    imported(c)
    g = c.get(bid_path(bid) + "/guide", headers=H).json()
    assert g["board_id"] == bid and g["steps"][0]["state"] == "done"
    assert g["steps"][2]["state"] == "done"
    c2, bid2 = old
    g2 = c2.get(bid_path(bid2) + "/guide", headers=H).json()
    assert g2["steps"][0]["state"] == "failed"               # the pack knows nothing of it
    assert c.get("/api/v1/guide", params={"build_dir": "rel"}, headers=H).status_code == 400


def design_doc(tmp_path, **over) -> str:
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}, "gpio": {"timed": True}},
         "build": {"sources": [str(kf.SPIKE_RM)]}}
    d.update(over)
    p = tmp_path / "spike_rm.json"
    p.write_text(json.dumps(d))
    return str(p)


def test_script_json_zip_and_a_refused_design(fielded, tmp_path):
    c, _ = fielded
    imported(c)
    r = c.post("/api/v1/guide/script", json={"static_id": SID, "design": design_doc(tmp_path)},
               headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "build_rm.tcl" in body["files"] and body["rm_id"] == "0x010080F0"
    assert body["receipt"] == "out/spike_rm_build.json"
    z = c.post("/api/v1/guide/script", json={"static_id": SID, "design": design_doc(tmp_path),
                                             "format": "zip"}, headers=H)
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        names = zf.namelist()
    assert "spike_rm/build_rm.tcl" in names and "spike_rm/kit/static/static_routed_locked.dcp" in names
    out = tmp_path / "b"
    r = c.post("/api/v1/guide/script", json={"static_id": SID, "design": design_doc(tmp_path),
                                             "out_dir": str(out)}, headers=H)
    assert r.status_code == 200 and (out / "kit" / "kit.json").is_file()
    bad = c.post("/api/v1/guide/script", json={"static_id": SID, "design": design_doc(
        tmp_path, ports=[{"name": "dut_clk", "dir": "in"}])}, headers=H)
    assert bad.status_code == 409 and bad.json()["error"]["name"] == "REFUSED"
    assert bad.json()["error"]["data"]["checks"]
    assert c.post("/api/v1/guide/script", json={"static_id": SID}, headers=H).status_code == 400
    inline = c.post("/api/v1/guide/script", json={"static_id": SID, "design": "minimal"},
                    headers=H)
    assert inline.status_code == 200 and inline.json()["rm_id_proposed"] is True


def test_check_and_pack(fielded, tmp_path):
    c, bid = fielded
    imported(c)
    receipt = kf.passed_build(tmp_path / "b")
    r = c.post("/api/v1/kits/check", json={"path": str(receipt), "board_id": bid}, headers=H)
    body = r.json()
    assert r.status_code == 200 and body["passed"] is True
    assert {x["name"]: x["state"] for x in body["checks"]}["board_static"] == "ok"
    r = c.post("/api/v1/kits/pack", json={"path": str(receipt), "import": True}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["imported"]["rm_id"] == "0x010080f0"
    # the twins: a failed build checks as not passed (200) and packs as 409
    bad = kf.passed_build(tmp_path / "bad", state="failed")
    r = c.post("/api/v1/kits/check", json={"path": str(bad)}, headers=H)
    assert r.status_code == 200 and r.json()["passed"] is False
    r = c.post("/api/v1/kits/pack", json={"path": str(bad)}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["data"]["checks"]


# --- the T14 mock serves the same routes -------------------------------------------------------------


def test_the_mock_serves_the_kit_routes():
    from harness_manager.demo import DemoEngine
    from tests.fakes.t14_mock_api import create_app

    eng = DemoEngine(speed=0.02)
    try:
        with TestClient(create_app(eng, token="t14", serve_ui=False)) as c:
            h = {"Authorization": "Bearer t14"}
            assert c.get("/api/v1/kits", headers=h).json()["kits"] == []
            r = c.post("/api/v1/kits/import", json={"path": str(kf.FIXTURE)}, headers=h)
            assert r.status_code == 200, r.text
            g = c.get("/api/v1/guide", params={"static_id": SID}, headers=h).json()
            assert g["steps"][2]["state"] == "done"
            r = c.post("/api/v1/kits/fetch", json={"static_id": SID}, headers=h)
            assert r.status_code == 202
            assert c.get("/api/v1/kits/nope", headers=h).status_code == 400
    finally:
        eng.close_all()
