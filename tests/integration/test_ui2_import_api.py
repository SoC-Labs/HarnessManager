"""UI2-API-BUILD G5: ``POST /overlays/import`` (the Import dialog) on the real daemon over
``VirtualMps3`` (the ILA-mint board runs 0x72BB0A36, the fixture kit's static), and the same
route in the T14 mock (the real kit_api). A packed overlay folder and a build receipt both
import; every refusal is a twin that imports nothing. Vivado discovery is off."""

from __future__ import annotations

import json
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
from tests.fakes.t13_daemon import TOKEN, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3, ila_mint_bake_profile

H = headers()
GROUP_IDS = ["files", "shell", "rm_id", "crc", "pair"]


@pytest.fixture(autouse=True)
def _no_vivado(monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")


@pytest.fixture
def board(tmp_path) -> Iterator[tuple[TestClient, str, list]]:
    events: list = []
    with VirtualMps3(tmp_path / "ila", profile=ila_mint_bake_profile()) as vb:
        eng = engine_for(vb)
        eng.bus.subscribe("kit.*", events.append)
        try:
            with TestClient(daemon_app.create_app(eng, token=TOKEN, static_dir=None)) as c:
                r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "g5"},
                           headers=H)
                assert r.status_code == 200, r.text
                assert c.post("/api/v1/kits/import", json={"path": str(kf.FIXTURE)},
                              headers=H).status_code == 200
                yield c, r.json()["board_id"], events
        finally:
            eng.close_all()


def imp(c: TestClient, path: Path | str, **kw) -> object:
    return c.post("/api/v1/overlays/import", json={"path": str(path), **kw}, headers=H)


def groups(body: dict) -> dict[str, str]:
    return {g["id"]: g["state"] for g in body["groups"]}


def packed(c: TestClient, tmp_path: Path, **build) -> Path:
    """A kit-built overlay folder (``kit pack`` without --import)."""
    receipt = kf.passed_build(tmp_path / "b", **build)
    r = c.post("/api/v1/kits/pack", json={"path": str(receipt)}, headers=H)
    assert r.status_code == 200, r.text
    return Path(r.json()["overlay_dir"])


def test_a_receipt_imports_through_kit_pack_with_the_five_groups(board, tmp_path):
    c, bid, events = board
    receipt = kf.passed_build(tmp_path / "b")
    r = imp(c, receipt, board_id=bid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "receipt" and body["passed"] is True and body["board_id"] == bid
    assert body["name"] == "spike_rm" and body["rm_id"] == "0x010080F0"
    assert [g["id"] for g in body["groups"]] == GROUP_IDS
    assert [g["title"] for g in body["groups"]] == [
        "Files", "Same shell", "rm_id in the user range", "CRC and sizes",
        "Clearing pairs with the partial"]
    assert groups(body)["shell"] == "ok" and groups(body)["crc"] == "ok"
    assert body["imported"]["rm_id"] == "0x010080f0"
    assert Path(body["overlay_dir"], "manifest.json").is_file()
    assert [e.topic for e in events][-1] == "kit.imported"
    # a build directory names the same receipt
    again = imp(c, tmp_path / "b", board_id=bid).json()
    assert again["kind"] == "receipt" and again["passed"]


def test_a_packed_overlay_folder_imports_as_it_is(board, tmp_path):
    c, bid, _ = board
    od = packed(c, tmp_path)
    body = imp(c, od, board_id=bid).json()
    assert body["kind"] == "overlay" and body["passed"] and body["overlay_dir"] == str(od)
    assert body["static_id"].lower() == "0x72bb0a36"
    assert set(groups(body).values()) <= {"ok", "warning", "unchecked"}
    names = {x["name"] for x in body["checks"]}
    assert {"manifest", "crc", "board_static", "rm_id_clash"} <= names
    assert "kit" not in names                     # the static's kit is cached: frames compared
    assert body["imported"]["name"] == "spike_rm"
    # manifest.json itself names the same folder
    assert imp(c, od / "manifest.json", board_id=bid).json()["kind"] == "overlay"


def test_check_only_writes_nothing(board, tmp_path):
    c, bid, events = board
    receipt = kf.passed_build(tmp_path / "b")
    body = imp(c, receipt, board_id=bid, check_only=True).json()
    assert body["passed"] and body["imported"] is None and body["overlay_dir"] is None
    assert not (tmp_path / "b" / "overlay").exists()
    assert "kit.imported" not in [e.topic for e in events]
    assert imp(c, receipt, check_only="yes").status_code == 400


def test_twin_another_shell_is_refused_14_incompatible_and_nothing_is_imported(board, tmp_path):
    c, bid, events = board
    receipt = kf.passed_build(tmp_path / "b", static_id="0x3F1A560F")
    r = imp(c, receipt, board_id=bid)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["name"] == "INCOMPATIBLE" and err["code"] == 14
    assert {g["id"]: g["state"] for g in err["data"]["groups"]}["shell"] == "mismatch"
    assert err["data"]["imported"] is None
    assert not (tmp_path / "b" / "overlay").exists()
    # the static asked for, with no board, is held the same way
    ok = kf.passed_build(tmp_path / "c")
    r = imp(c, ok, static_id="0x3F1A560F")
    assert r.status_code == 409 and r.json()["error"]["name"] == "INCOMPATIBLE"
    assert "kit.imported" not in [e.topic for e in events]


def test_twin_a_folder_missing_its_clearing_or_with_a_bad_crc_is_refused(board, tmp_path):
    c, bid, _ = board
    od = packed(c, tmp_path)
    manifest = json.loads((od / "manifest.json").read_text())
    clearing = od / manifest["clearing"]["file"]
    data = clearing.read_bytes()
    clearing.write_bytes(data[:-4] + b"\x00\x00\x00\x00")          # same length, other bytes
    r = imp(c, od, board_id=bid)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert {g["id"]: g["state"] for g in r.json()["error"]["data"]["groups"]}["crc"] == "mismatch"
    clearing.unlink()
    r = imp(c, od, board_id=bid)
    assert r.status_code == 409
    assert {g["id"]: g["state"] for g in r.json()["error"]["data"]["groups"]}["files"] == \
        "mismatch"


def test_twin_a_receipt_whose_files_are_not_beside_it_is_refused(board, tmp_path):
    c, bid, _ = board
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"\x00" * 64)
    receipt = kf.passed_build(tmp_path / "b", partial_bin=f"../../{outside.name}")
    r = imp(c, receipt, board_id=bid)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    bad = [x for x in r.json()["error"]["data"]["checks"] if x["name"] == "partial"]
    assert bad and "not a file name beside it" in bad[0]["detail"]


def test_twin_paths_that_are_not_importable(board, tmp_path):
    c, bid, _ = board
    assert imp(c, "relative/dir").status_code == 400
    z = tmp_path / "design.zip"
    z.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    r = imp(c, z)
    assert r.status_code == 400 and "/overlays/upload" in r.json()["error"]["hint"]
    (tmp_path / "empty").mkdir()
    assert imp(c, tmp_path / "empty").status_code == 404
    assert imp(c, tmp_path / "nothing-here").status_code == 404
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    assert imp(c, txt).status_code == 400
    assert imp(c, tmp_path / "b", board_id="mps3@10.9.9.9:6900").status_code == 404
    assert c.post("/api/v1/overlays/import", json={"path": str(tmp_path)}).status_code == 401


def test_the_mock_serves_the_import_route():
    from harness_manager.demo import DemoEngine
    from tests.fakes.t14_mock_api import create_app

    eng = DemoEngine(speed=0.02)
    try:
        with TestClient(create_app(eng, token="t14", serve_ui=False)) as c:
            h = {"Authorization": "Bearer t14"}
            r = c.post("/api/v1/overlays/import", json={"path": "/no/such/overlay"}, headers=h)
            assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"
            assert c.post("/api/v1/overlays/import", json={"path": "rel"},
                          headers=h).status_code == 400
    finally:
        eng.close_all()


# --- the zip upload (the dialog's "Choose a zip") --------------------------------------------------


def zipped(folder: Path, dest: Path, *, inside: str = "") -> bytes:
    """The folder as a zip; ``inside`` puts it one folder down (as a zipped folder is)."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                zf.write(p, str(Path(inside) / p.relative_to(folder)) if inside
                         else str(p.relative_to(folder)))
    dest.write_bytes(buf.getvalue())
    return buf.getvalue()


def upload(c: TestClient, data: bytes, **q) -> object:
    return c.post("/api/v1/overlays/upload", params=q, content=data,
                  headers={**H, "Content-Type": "application/zip"})


def test_an_uploaded_zip_of_the_folder_imports_and_leaves_nothing_behind(board, tmp_path):
    c, bid, events = board
    od = packed(c, tmp_path)
    data = zipped(od, tmp_path / "spike.zip", inside="spike_rm")
    work = Path(c.app.state.daemon.engine.state_dir) / "kits"
    r = upload(c, data, name="spike.zip", board_id=bid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "overlay" and body["passed"] and body["path"] == "spike.zip"
    assert body["upload"] == {"name": "spike.zip", "bytes": len(data)}
    assert body["overlay_dir"] is None and body["imported"]["name"] == "spike_rm"
    assert [e.topic for e in events][-1] == "kit.imported"
    assert not [p for p in work.glob(".upload-*")]                  # extraction and file gone
    # a zipped build directory (its receipt in out/) imports through kit pack
    receipt = kf.passed_build(tmp_path / "b2", name="other_rm", rm_id="0x010080F1")
    b2 = zipped(receipt.parent.parent, tmp_path / "b2.zip")
    got = upload(c, b2, name="b2.zip", board_id=bid, check_only="true").json()
    assert got["kind"] == "receipt" and got["passed"] and got["imported"] is None


def test_twins_uploads_that_are_refused(board, tmp_path):
    c, bid, _ = board
    assert upload(c, b"", name="x.zip").status_code == 400                   # empty
    assert upload(c, b"not a zip at all", name="x.zip").status_code == 409   # unreadable
    empty = tmp_path / "e"
    (empty / "docs").mkdir(parents=True)
    (empty / "docs" / "README.md").write_text("nothing to import")
    r = upload(c, zipped(empty, tmp_path / "e.zip"), name="e.zip")
    assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"
    other = kf.passed_build(tmp_path / "o", static_id="0x3F1A560F")
    r = upload(c, zipped(other.parent.parent, tmp_path / "o.zip"), name="o.zip", board_id=bid)
    assert r.status_code == 409 and r.json()["error"]["name"] == "INCOMPATIBLE"
    assert r.json()["error"]["data"]["path"] == "o.zip"
    r = c.post("/api/v1/overlays/upload", params={"name": "big.zip"}, content=b"x" * 16,
               headers={**H, "Content-Type": "application/zip",
                        "Content-Length": str(300 * 1024 * 1024)})
    assert r.status_code == 413 and "256 MB" in r.json()["error"]["message"]
    assert upload(c, b"PK", check_only="maybe").status_code == 400
    assert c.post("/api/v1/overlays/upload", content=b"PK").status_code == 401


def test_a_zip_that_escapes_its_folder_is_refused(board, tmp_path):
    import io
    import zipfile

    c, _, _ = board
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape/manifest.json", "{}")
    r = upload(c, buf.getvalue(), name="evil.zip")
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert not (tmp_path / "escape").exists()
