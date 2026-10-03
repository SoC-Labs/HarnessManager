"""QUICKWINS G3: ``POST /overlays/import-folder``: a release's overlays/ folder, one design per
sub-folder, each imported / skipped (another static) / refused with its reason. Fake overlay
folders (``kit pack`` of the fixture builds); every refusal is a twin that imports nothing."""

from __future__ import annotations

import json
from pathlib import Path

from tests.integration import test_ui2_import_api as _api

H, packed = _api.H, _api.packed
board = _api.board                    # the fixtures
_no_vivado = _api._no_vivado


def make(c, base: Path, name: str, **build) -> Path:
    """A packed overlay folder named ``name`` under ``base`` (a release's overlays/)."""
    od = packed(c, base / f"_b_{name}", name=name, **build)
    dest = base / "overlays" / "open" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    od.rename(dest)
    return dest


def folder(c, path: Path, **kw):
    return c.post("/api/v1/overlays/import-folder", json={"path": str(path), **kw}, headers=H)


def states(body: dict) -> dict[str, str]:
    return {r["name"]: r["state"] for r in body["results"]}


def test_a_release_folder_imports_each_design_and_lists_every_one(board, tmp_path):
    c, bid, events = board
    make(c, tmp_path, "alpha", rm_id="0x010080F1")
    make(c, tmp_path, "beta", rm_id="0x010080F2")
    other = make(c, tmp_path, "gamma", rm_id="0x010080F3", static_id="0x3F1A560F")
    bad = make(c, tmp_path, "delta", rm_id="0x010080F4")
    m = json.loads((bad / "manifest.json").read_text())
    (bad / m["clearing"]["file"]).write_bytes(b"short")          # a bad CRC and length
    (tmp_path / "overlays" / "open" / "notes").mkdir()           # not a design: not listed
    r = folder(c, tmp_path / "overlays", board_id=bid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert states(body) == {"alpha": "imported", "beta": "imported", "gamma": "skipped",
                            "delta": "refused"}
    rows = {x["name"]: x for x in body["results"]}
    assert "another static" in rows["gamma"]["reason"] and "0x3f1a560f" in rows["gamma"]["reason"]
    assert rows["delta"]["reason"] and rows["alpha"]["reason"] == ""
    assert body["counts"] == {"imported": 2, "skipped": 1, "refused": 1}
    assert [e.topic for e in events].count("kit.imported") == 2
    assert other.is_dir()


def test_twin_check_only_writes_nothing_and_says_ready(board, tmp_path):
    c, bid, events = board
    make(c, tmp_path, "alpha", rm_id="0x010080F1")
    body = folder(c, tmp_path, board_id=bid, check_only=True).json()
    assert states(body) == {"alpha": "ready"} and body["check_only"] is True
    assert "kit.imported" not in [e.topic for e in events]
    assert folder(c, tmp_path, check_only="yes").status_code == 400


def test_twin_a_folder_with_no_design_or_no_folder_is_absent(board, tmp_path):
    c, bid, _ = board
    (tmp_path / "empty" / "sub").mkdir(parents=True)
    r = folder(c, tmp_path / "empty", board_id=bid)
    assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"
    assert "holds no design" in r.json()["error"]["message"]
    assert folder(c, tmp_path / "nope", board_id=bid).status_code == 404
    assert folder(c, "relative/path", board_id=bid).status_code == 400


def test_one_design_folder_itself_and_a_second_run_do_not_break(board, tmp_path):
    c, bid, _ = board
    one = make(c, tmp_path, "solo", rm_id="0x010080F5")
    assert states(folder(c, one, board_id=bid).json()) == {"solo": "imported"}
    again = folder(c, tmp_path, board_id=bid).json()
    assert states(again) == {"solo": "imported"}        # the store takes the same design again
