"""SIDEBAR-UX: the sidebar's settings rows and the boards.toml listing. Each check has a twin.

- ``general.board_order`` / ``general.favourite_boards``: lists of board ids in the user's
  ``settings.toml`` (``PUT /settings``), applied live, never the admin's.
- ``daemon/configured.py``: the boards boards.toml configures, as rows of ``GET /boards``,
  built from the file with no contact, each saying how it is reached.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
import tomllib

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import UsageError
from harness_manager.core.services import EngineConfig
from harness_manager.daemon import configured
from harness_manager.daemon.app import create_app
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, DemoEngine
from harness_manager.engine import Engine
from harness_manager.power.config import BoardConfig
from harness_manager.settings import ops
from harness_manager.settings.rows import BOARD_LIST_MAX, CORE_ROWS

ORDER, FAVS = "general.board_order", "general.favourite_boards"
IDS = [BOARD_USB, BOARD_FIELDED, "mps3@192.168.10.121:6900"]
TOKEN = "sidebar-unit"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def ctx(tmp_path: Path) -> ops.SettingsContext:
    return ops.SettingsContext(state_dir=tmp_path / "state", env={"HARNESS_MANAGER_KEYRING": "off"},
                               policy_path=tmp_path / "policy.toml", keyrings=[])


# --- the rows -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [ORDER, FAVS])
def test_the_rows_are_the_users_lists_applied_live(key):
    spec = {s.key: s for s in CORE_ROWS}[key]
    assert (spec.type, spec.default, spec.section, spec.scope, spec.apply, spec.owner) == \
        ("list", [], "General", "user", "live", "user")
    assert spec.ui and spec.advanced and not spec.lockable and not spec.secret


def test_order_and_favourites_round_trip_through_settings_toml(ctx):
    got = ops.set_values(ctx, {ORDER: IDS, FAVS: [IDS[2]]})
    assert got["apply"] == "live" and sorted(got["keys"]) == sorted([ORDER, FAVS])
    data = tomllib.loads((ctx.state_dir / "settings.toml").read_text())
    assert data["general"]["board_order"] == IDS and data["general"]["favourite_boards"] == [IDS[2]]
    rows = {r["key"]: r for r in ops.listing(ctx, section="general")["rows"]}
    assert rows[ORDER]["value"] == IDS and rows[ORDER]["source"] == "user"
    assert rows[FAVS]["value"] == [IDS[2]]
    # unset: back to nothing ordered, nothing pinned
    ops.unset(ctx, FAVS)
    assert ops.listing(ctx, key=FAVS)["rows"][0]["value"] == []
    assert "board_order" in (ctx.state_dir / "settings.toml").read_text()


def test_negative_twin_unset_rows_read_as_empty_and_write_nothing(ctx):
    rows = {r["key"]: r for r in ops.listing(ctx, section="general")["rows"]}
    assert rows[ORDER]["value"] == [] and rows[ORDER]["source"] == "default"
    assert not (ctx.state_dir / "settings.toml").exists()


@pytest.mark.parametrize("bad, why", [
    ([BOARD_USB, BOARD_USB], "more than once"),
    ([""], "not board ids"),
    ([" mps3@x"], "not board ids"),
    (["mps3@\x07bell"], "not board ids"),
    (["x" * 257], "not board ids"),
    ([f"mps3@10.0.{i // 250}.{i % 250}:6900" for i in range(BOARD_LIST_MAX + 1)], "at most"),
    ([3], "list of strings"),
])
def test_negative_twin_a_list_that_is_not_board_ids_is_refused_and_nothing_written(ctx, bad, why):
    with pytest.raises(UsageError, match=why):
        ops.set_values(ctx, {ORDER: bad})
    assert not (ctx.state_dir / "settings.toml").exists()


def test_negative_twin_the_admin_policy_cannot_lock_the_users_order(ctx):
    ctx.policy_path.write_text(f'[lock]\n{ORDER} = ["{BOARD_USB}"]\n')
    ops.set_values(ctx, {ORDER: IDS})                              # still the user's to set
    row = ops.listing(ctx, key=ORDER)["rows"][0]
    assert row["value"] == IDS and row["source"] == "user"


# --- the boards.toml listing ------------------------------------------------------------------------------


def _board(key: str, match: tuple[str, ...] = (), **tables) -> BoardConfig:
    return BoardConfig(key=key, match=match, tables=tables)


@pytest.mark.parametrize("key, match, want", [
    ("mps3@192.168.10.101:6900", (), ("mps3", "192.168.10.101:6900")),
    ("lab", ("192.168.10.101",), ("mps3", "192.168.10.101")),
    ("lab", ("mps3@192.168.10.102:6900", "192.168.10.101"), ("mps3", "192.168.10.102:6900")),
])
def test_a_table_names_its_board_by_its_key_or_its_match(key, match, want):
    assert configured.target_of(_board(key, match), {"mps3"}) == want


@pytest.mark.parametrize("key, match", [("lab", ()), ("lab", ("has space",)),
                                        ("kr260@10.0.0.1:1", ())])
def test_negative_twin_a_table_with_no_address_or_an_unknown_pack_is_not_listed(key, match):
    assert configured.target_of(_board(key, match), {"mps3"}) is None


def test_the_summary_says_how_the_board_is_reached_and_never_a_secret():
    s = configured.summary(_board("lab", ("192.168.10.101",), via="hub",
                                  hub={"url": "https://tok:SECRET@hub.example:7246", "target": "t"}))
    assert s == {"key": "lab", "via": "hub", "hub": "https://hub.example:7246", "target": "t",
                 "match": ["192.168.10.101"], "name": ""}
    assert "SECRET" not in json.dumps(s)
    assert configured.summary(_board("x", hub={"use": "lab", "host": "h"}))["hub"] == "lab"


LAB = """\
[boards.lab]
match = ["192.168.10.121"]
name = "lab-mps3"
via = "hub"
hub = { use = "mapstone-dev", target = "mps3_01_pl" }

[boards.nowhere]
via = "hub"
hub = { use = "mapstone-dev", target = "mps3_09_pl" }

[boards.broken]
match = ["192.168.10.122"]
via = "tunnel-please"
"""


def _rows(engine, state: Path) -> dict[str, dict]:
    with TestClient(create_app(engine, token=TOKEN, state_dir=state, static_dir=None)) as c:
        return {b["board_id"]: b for b in c.get("/api/v1/boards", headers=H).json()["boards"]}


def test_get_boards_lists_a_boards_toml_board_without_contacting_it(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "boards.toml").write_text(LAB)
    (state / "settings.toml").write_text('[hubs.mapstone-dev]\nhost = "hub.example"\n')
    eng = Engine(EngineConfig(state_dir=state))
    try:
        rows = _rows(eng, state)
    finally:
        eng.close_all()
    # listed: the lab board, not open, routed through its hub, named; the table with no
    # address and the one whose via the pack refuses are not
    assert list(rows) == ["mps3@192.168.10.121:6900"]
    row = rows["mps3@192.168.10.121:6900"]
    assert row["open"] is False and row["source"] == "config" and "holder" not in row
    assert row["configured"] == {"key": "lab", "via": "hub", "hub": "mapstone-dev",
                                 "target": "mps3_01_pl", "match": ["192.168.10.121"],
                                 "name": "lab-mps3"}
    cand = row["candidate"]
    assert cand["name"] == "lab-mps3" and cand["identity"] is None
    assert cand["evidence"] == configured.EVIDENCE
    eth = next(lk for lk in cand["links"] if lk["kind"] == "ethernet")
    assert eth["via"] == "hub"                         # Open goes through the hub
    assert eng.open_boards() == []


def test_the_demo_engine_is_never_asked_about_a_configured_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "env"))
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "boards.toml").write_text(LAB)
    eng = DemoEngine(speed=0)
    try:
        rows = _rows(eng, tmp_path / "daemon")
        assert rows["mps3@192.168.10.121:6900"]["source"] == "config"
        asked = {n for n, _a in eng.calls}
        assert asked <= {"candidate_for"}, asked      # no probe, open, info or read
    finally:
        eng.close_all()


def test_negative_twin_without_boards_toml_a_fresh_service_lists_nothing(tmp_path):
    state = tmp_path / "state"
    eng = Engine(EngineConfig(state_dir=state))
    try:
        assert _rows(eng, state) == {}
    finally:
        eng.close_all()


def test_a_board_the_service_knows_keeps_its_source_and_gains_its_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "env"))
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "boards.toml").write_text(
        f'[boards."{BOARD_FIELDED}"]\nname = "desk"\n')
    eng = DemoEngine(speed=0)
    try:
        with TestClient(create_app(eng, token=TOKEN, state_dir=tmp_path / "d", static_dir=None)) as c:
            c.post("/api/v1/probe", json={}, headers=H)
            c.post("/api/v1/boards", json={"target": "192.168.10.102"}, headers=H)
            rows = {b["board_id"]: b for b in c.get("/api/v1/boards", headers=H).json()["boards"]}
    finally:
        eng.close_all()
    assert rows[BOARD_FIELDED]["source"] == "probe"
    assert rows[BOARD_FIELDED]["configured"]["key"] == BOARD_FIELDED
    assert rows[BOARD_USB]["source"] == "open" and "configured" not in rows[BOARD_USB]


def test_negative_twin_a_broken_boards_toml_lists_no_configured_board(tmp_path, caplog):
    state = tmp_path / "state"
    state.mkdir()
    (state / "boards.toml").write_text("[boards.lab\nmatch = [")
    eng = Engine(EngineConfig(state_dir=state))
    try:
        assert _rows(eng, state) == {}
    finally:
        eng.close_all()
    assert any("not valid TOML" in r.getMessage() for r in caplog.records)


def test_opening_a_listed_board_goes_through_its_candidate_and_it_stops_saying_not_contacted(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "env"))
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "boards.toml").write_text(
        f'[boards."{BOARD_FIELDED}"]\nname = "desk"\n')
    eng = DemoEngine(speed=0)
    try:
        with TestClient(create_app(eng, token=TOKEN, state_dir=tmp_path / "d", static_dir=None)) as c:
            def rows() -> dict[str, dict]:
                return {b["board_id"]: b for b in c.get("/api/v1/boards", headers=H).json()["boards"]}

            listed = rows()[BOARD_FIELDED]
            assert listed["source"] == "config" and eng.called("open") == []
            assert listed["candidate"]["evidence"] == configured.EVIDENCE
            r = c.post("/api/v1/boards", json={"candidate": listed["candidate"], "note": "t"}, headers=H)
            assert r.status_code == 200, r.text
            assert [a[0] for a in eng.called("open")] == [BOARD_FIELDED]
            opened = rows()[BOARD_FIELDED]
            # the daemon opened it as "opened from boards.toml" (the demo's session keeps its
            # scripted candidate; the real engine keeps the one it was given)
            assert opened["source"] == "open"
            assert opened["candidate"]["evidence"] != configured.EVIDENCE
            c.delete(f"/api/v1/boards/{BOARD_FIELDED.replace('@', '%40')}", headers=H)
            closed = rows()[BOARD_FIELDED]
            # twin: once contacted it is a board the service knows, not a config-only one
            assert closed["source"] == "probe" and closed["open"] is False
            assert closed["candidate"]["evidence"] in (configured.OPENED, "answered ping")
            assert closed["configured"]["name"] == "desk"
    finally:
        eng.close_all()
