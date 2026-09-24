"""T10: the XDC routes on the real daemon (``daemon/xdc_api.py``), over ``VirtualMps3``.

``xdc_api`` is loaded through ``app.EXTENSIONS`` (CCR T10-1). The board routes read only
the static the board reported when probed: the default virtual board runs the previous
shell 0x3F1A560F, the ILA-mint profile runs the fielded 0x72BB0A36 the pin model describes.
"""

from __future__ import annotations

import io
import warnings
import zipfile
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.daemon import app as daemon_app
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3, ila_mint_bake_profile

H = headers()


def client_for(vb: VirtualMps3) -> Iterator[tuple[TestClient, str]]:
    eng = engine_for(vb)
    try:
        with TestClient(daemon_app.create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "t10"}, headers=H)
            assert r.status_code == 200, r.text
            yield c, r.json()["board_id"]
    finally:
        eng.close_all()


@pytest.fixture
def old_board(vboard) -> Iterator[tuple[TestClient, str]]:
    yield from client_for(vboard)


@pytest.fixture
def fielded_board(tmp_path) -> Iterator[tuple[TestClient, str]]:
    with VirtualMps3(tmp_path / "ila", profile=ila_mint_bake_profile()) as vb:
        yield from client_for(vb)


def test_the_catalogue_needs_no_board(old_board):
    c, _ = old_board
    r = c.get("/api/v1/xdc", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["model"]["default_shell"] == "0x72BB0A36"
    assert {d["name"] for d in body["designs"]} >= {"nanosoc", "blinky"}
    assert c.get("/api/v1/xdc").status_code == 401            # the bearer rule holds


def test_the_board_catalogue_says_which_static_the_board_runs(old_board, fielded_board):
    c, bid = old_board
    b = c.get(bid_path(bid) + "/xdc", headers=H).json()["board"]
    assert b["static_id"].lower() == "0x3f1a560f" and b["matches"] is False
    assert "0x72BB0A36" in b["reason"]
    c2, bid2 = fielded_board
    b2 = c2.get(bid_path(bid2) + "/xdc", headers=H).json()["board"]
    assert b2["matches"] is True and b2["reason"] == ""


def test_preview_returns_the_files_and_the_checks(fielded_board):
    c, bid = fielded_board
    r = c.post(bid_path(bid) + "/xdc/export", json={"kit": "rm-kit", "design": "nanosoc",
                                                    "preview": True}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["passed"] is True and "nanosoc_ooc.xdc" in body["files"]
    assert "create_clock -name dut_clk" in body["files"]["nanosoc_ooc.xdc"]
    assert body["design"]["static_id"] == "0x72BB0A36"


def test_an_rm_kit_for_a_board_on_another_static_is_refused_with_the_reason(old_board):
    c, bid = old_board
    r = c.post(bid_path(bid) + "/xdc/export", json={"kit": "rm-kit", "design": "nanosoc"}, headers=H)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["name"] == "REFUSED"
    assert [x["code"] for x in err["data"]["checks"] if x["severity"] == "error"] == ["static_id"]
    # twin: the board-free export for the model's static is fine
    ok = c.post("/api/v1/xdc/export", json={"kit": "rm-kit", "design": "nanosoc"}, headers=H)
    assert ok.status_code == 200 and ok.json()["passed"] is True


def test_a_failed_check_is_409_with_every_check_and_preview_still_shows_them(fielded_board):
    c, bid = fielded_board
    design = {"kind": "board", "name": "bad", "ports": [
        {"port": "sw", "net": "USER_SW[0]", "dir": "out"},
        {"port": "clk", "net": "USER_SW[1]", "dir": "in", "clock_mhz": 10}]}
    r = c.post(bid_path(bid) + "/xdc/export", json={"kit": "board", "design": design}, headers=H)
    assert r.status_code == 409
    codes = [x["code"] for x in r.json()["error"]["data"]["checks"]]
    assert codes == ["direction", "clock_capable"]
    p = c.post(bid_path(bid) + "/xdc/export", json={"kit": "board", "design": design,
                                                    "preview": True}, headers=H)
    assert p.status_code == 200 and p.json()["ok"] is True   # the envelope: the request worked
    assert p.json()["passed"] is False                     # the kit: its checks did not
    assert "bad_pins.xdc" in p.json()["files"]


def test_zip_download(fielded_board):
    c, bid = fielded_board
    r = c.post(bid_path(bid) + "/xdc/export", json={"kit": "board", "design": "blinky",
                                                    "format": "zip"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["blinky/blinky_io.xdc", "blinky/blinky_pins.xdc",
                             "blinky/blinky_timing.xdc", "blinky/manifest.json"]


@pytest.mark.parametrize("body, status", [
    ({"kit": "bogus"}, 400),
    ({"kit": "board", "design": "/etc/passwd"}, 400),
    ({"kit": "board", "design": "blinky", "format": "tar"}, 400),
    ({"kit": "board", "design": "no_such"}, 404),
    ({"kit": "board", "design": "nanosoc"}, 400),
])
def test_bad_requests(fielded_board, body, status):
    c, bid = fielded_board
    r = c.post(bid_path(bid) + "/xdc/export", json=body, headers=H)
    assert r.status_code == status, r.text
    assert r.json()["ok"] is False


def test_the_board_routes_need_an_open_board(fielded_board):
    c, _ = fielded_board
    r = c.get(bid_path("mps3@203.0.113.9:6900") + "/xdc", headers=H)
    assert r.status_code == 404
