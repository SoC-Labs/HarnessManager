"""UI2-API-BUILD G8 on the kit routes (``daemon/kit_api.py``) of the real daemon over
``VirtualMps3`` (the ILA-mint board runs 0x72BB0A36, the fixture kit's static): the guide's
``running``, ``pblock`` and ``utilisation``, ``kit check``'s facts, ``guide/script``'s ``run``,
and ``POST /kits/design/scan``; the mock serves the same (the real kit_api). Vivado discovery
is off: nothing runs Vivado. Each has a negative twin."""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.daemon import app as daemon_app
from harness_manager.services.kit import vivado
from tests.fakes import kit_fakes as kf
from tests.fakes import ui2_build_fakes as bf
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.ui2_build_fakes import rtl
from tests.fakes.virtual_board import VirtualMps3, ila_mint_bake_profile

H = headers()
SID = "0x72BB0A36"


@pytest.fixture(autouse=True)
def _no_vivado(monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")


@pytest.fixture
def board(tmp_path) -> Iterator[tuple[TestClient, str]]:
    with VirtualMps3(tmp_path / "ila", profile=ila_mint_bake_profile()) as vb:
        eng = engine_for(vb)
        try:
            with TestClient(daemon_app.create_app(eng, token=TOKEN, static_dir=None)) as c:
                r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "g8"},
                           headers=H)
                assert r.status_code == 200, r.text
                assert c.post("/api/v1/kits/import", json={"path": str(kf.FIXTURE)},
                              headers=H).status_code == 200
                yield c, r.json()["board_id"]
        finally:
            eng.close_all()


def guide(c: TestClient, bid: str, build_dir: Path) -> dict:
    r = c.get(bid_path(bid) + "/guide", params={"build_dir": str(build_dir)}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_guide_says_the_running_stage_and_the_pblock(board, tmp_path):
    c, bid = board
    at = time.time() - 60
    bf.running_log(tmp_path / "b", "impl", stage_at=at)
    g = guide(c, bid, tmp_path / "b")
    run = g["running"]
    assert run["stage"] == "impl" and run["stage_index"] == 4 and run["fresh"] is True
    assert run["started_at"] == bf.session_epoch() and run["stage_started_at"] == int(at)
    assert run["stages"] == ["preflight", "synth", "link", "impl", "verify", "bitstream"]
    build_step = next(s for s in g["steps"] if s["id"] == "build")
    assert "a build is running here: stage impl" in build_step["detail"]
    pb = g["pblock"]
    assert pb["name"] == "pblock_rp_dut" and pb["capacity"]["LUT"] == 42824 and pb["fixed"]
    assert g["utilisation"] is None                                  # no passed build yet
    # twins: an empty build dir has no running build; the board-less guide for an unknown
    # static has no pblock
    assert guide(c, bid, tmp_path / "empty")["running"] is None
    other = c.get("/api/v1/guide", params={"static_id": "0x12345678"}, headers=H).json()
    assert other["pblock"] is None and other["running"] is None


def test_a_passed_build_shows_its_utilisation_in_the_guide_and_in_kit_check(board, tmp_path):
    c, bid = board
    receipt = kf.passed_build(tmp_path / "b")
    g = guide(c, bid, tmp_path / "b")
    assert g["utilisation"] is None                                  # no report beside it
    bf.util_report(receipt.parent, "spike_rm", lut_used=32000)
    g = guide(c, bid, tmp_path / "b")
    rows = {r["key"]: r for r in g["utilisation"]["rows"]}
    assert rows["LUT"]["used"] == 32000 and rows["LUT"]["level"] == "warn"
    assert g["utilisation"]["worst"]["key"] == "LUT" and g["running"] is None
    body = c.post("/api/v1/kits/check", json={"path": str(receipt), "board_id": bid},
                  headers=H).json()
    assert body["facts"]["utilisation"]["pblock"] == "pblock_rp_dut"
    assert body["facts"]["pblock"]["slr"] == "SLR0"
    # twin: a failed build is not measured
    bad = kf.passed_build(tmp_path / "f", state="failed")
    bf.util_report(bad.parent, "spike_rm")
    assert guide(c, bid, tmp_path / "f")["utilisation"] is None


def test_the_script_answers_every_way_to_run_it(board, tmp_path):
    c, bid = board
    out = tmp_path / "build"
    r = c.post("/api/v1/guide/script", json={"static_id": SID, "design": "minimal",
                                             "out_dir": str(out), "stop_after": "link"},
               headers=H)
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert run["stop_after"] == "link" and run["receipt"] == "out/minimal_build.json"
    assert run["gui"]["argv"][1:3] == ["-mode", "gui"] and "STOP_AFTER=link" in run["gui"]["argv"]
    assert run["batch"]["argv"][1:3] == ["-mode", "batch"]
    assert run["session"]["lines"][-1] == "source build_rm.tcl"
    assert run["session"]["lines"][0] == f"cd {out.resolve()}"
    default = c.post("/api/v1/guide/script", json={"static_id": SID, "design": "minimal"},
                     headers=H).json()["run"]
    assert default["stop_after"] == "bitstream" and "-tclargs" not in default["gui"]["argv"]
    # twin: a stage the script does not have
    bad = c.post("/api/v1/guide/script", json={"static_id": SID, "design": "minimal",
                                               "stop_after": "route"}, headers=H)
    assert bad.status_code == 400


def test_design_scan_proposes_and_writes_a_design(board, tmp_path):
    c, bid = board
    src = rtl(tmp_path)
    r = c.post("/api/v1/kits/design/scan", json={"path": str(src), "board_id": bid}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["static_id"] == SID and body["top"] == "rm_demo" and body["use"] == {"clkrst": {}}
    assert body["rm_id_proposed"] is True and body["written"] is None
    assert body["design"]["build"]["top"] == "rm_demo"
    out = tmp_path / "designs"
    w = c.post("/api/v1/kits/design/scan", json={"path": str(src), "out": str(out),
                                                 "static_id": SID, "rm_id": "0x01008123"},
               headers=H).json()
    assert w["written"] == str(out / "demo.json") and w["rm_id"] == "0x01008123"
    doc = json.loads((out / "demo.json").read_text())
    assert doc["rm_id"] == "0x01008123" and doc["kind"] == "rm"
    # the written design goes straight into the script
    s = c.post("/api/v1/guide/script", json={"static_id": SID, "design": str(out / "demo.json")},
               headers=H)
    assert s.status_code == 200, s.text
    assert "a_top.sv" in s.json()["params"]["RM_SOURCES"]


def test_twins_design_scan_refuses(board, tmp_path):
    c, _ = board
    scan = "/api/v1/kits/design/scan"
    assert c.post(scan, json={"path": "rel/rtl"}, headers=H).status_code == 400
    assert c.post(scan, json={"path": str(tmp_path / "none")}, headers=H).status_code == 404
    (tmp_path / "empty").mkdir()
    assert c.post(scan, json={"path": str(tmp_path / "empty")}, headers=H).status_code == 404
    src = rtl(tmp_path / "r")
    assert c.post(scan, json={"path": str(src), "top": "nope"}, headers=H).status_code == 400
    assert c.post(scan, json={"path": str(src), "name": ""}, headers=H).status_code == 400
    assert c.post(scan, json={"path": str(src), "static_id": "banana"},
                  headers=H).status_code == 400


def test_the_mock_serves_the_build_additions():
    from harness_manager.demo import DemoEngine
    from tests.fakes.t14_mock_api import create_app

    eng = DemoEngine(speed=0.02)
    try:
        with TestClient(create_app(eng, token="t14", serve_ui=False)) as c:
            h = {"Authorization": "Bearer t14"}
            c.post("/api/v1/kits/import", json={"path": str(kf.FIXTURE)}, headers=h)
            g = c.get("/api/v1/guide", params={"static_id": SID}, headers=h).json()
            assert g["pblock"]["name"] == "pblock_rp_dut" and "running" in g
            r = c.post("/api/v1/kits/design/scan", json={"path": "/no/such/rtl"}, headers=h)
            assert r.status_code == 404
    finally:
        eng.close_all()
