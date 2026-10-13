"""L5a SENSORS: the mint-4 `sensors` verb in Readings (net-protocol v0.20, feature `sysmon`) on the
virtual mint-4 board: the daemon body, the validated claim-locked set and clear, the converted
rows in telemetry, and the fallback on a board without the name. Loopback only."""

from __future__ import annotations

import json
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3 import telemetry as tm
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t13_daemon import TOKEN, bid_path, headers
from tests.fakes.virtual_board import LINUX_HARNESSD, LINUX_MINT4, VirtualMps3

H = headers()
ROOT = Path(__file__).resolve().parents[2]
CLOCKS_JS = ROOT / "src/harness_manager/web/static/js/sections/clocks.js"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def with_sensors(vb, **cfg):
    """The SYSMON model on the virtual board. The virtual board's LINUX_MINT4 advertises `sysmon`
    but its FakeShell is built without the model (self.sensors is None), so install it here."""
    from pyverify.testing.mint4_models import SensorsModel

    if vb.shell.sensors is None:
        vb.shell.sensors = SensorsModel(vb.shell, cfg)
    return vb


def open_board(vb, state):
    eng = Engine(EngineConfig(state_dir=state), packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
    session = eng.open(eng.candidate_for(vb.shell_endpoint))
    return eng, session


@pytest.fixture
def mint4(tmp_path, state):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        with_sensors(vb)
        eng, session = open_board(vb, state)
        try:
            yield vb, eng, session
        finally:
            eng.close_all()


def by_name(rows):
    return {r.name: r for r in rows}


# --- the body -----------------------------------------------------------------------------------


def test_get_returns_the_converted_body_with_alarm_words(mint4):
    vb, _eng, session = mint4
    body = session.telemetry.sensors_get()
    assert body["temp_c"] == 41.2 and body["vccint_v"] == 0.951 and body["alm"] == 0
    assert body["thr"]["temp_hi_c"] == 80.0 and body["alarms"] == [] and body["calibrated"] is False
    vb.shell.sensors.set_values(temp_c=101.0, vccaux_v=1.2)
    body = session.telemetry.sensors_get()
    assert body["alarms"] == ["Over temperature", "Temperature above its alarm level",
                              "VCCAUX outside its window"]


def test_telemetry_rows_use_the_converted_values_and_say_no_power_sensor(mint4):
    _vb, eng, session = mint4
    rows = by_name(eng.telemetry.readings(session))
    assert rows["fpga_die_temp"].value == 41.2 and rows["fpga_die_temp"].source == tm.SENSORS_SOURCE
    assert rows["vccaux"].value == pytest.approx(1.801) and "not calibrated" in rows["vccaux"].reason
    assert rows["fpga_die_temp_max"].value == 41.2
    assert rows["board_power"].value is None and rows["board_power"].reason == tm.NO_POWER_SENSOR


# --- set / clear ---------------------------------------------------------------------------------


def test_set_applies_and_reports_persisted_and_clear_alm_clears(mint4):
    vb, _eng, session = mint4
    out = session.telemetry.sensors_act("set", {"temp_hi_c": 85, "k": -1.5})
    assert out["thr"]["temp_hi_c"] == 85.0 and out["k"] == -1.5 and out["cal"] == "set"
    assert out["persisted"] is True and out["calibrated"] is True
    vb.shell.sensors.set_values(vccbram_v=0.5)
    assert session.telemetry.sensors_get()["alm"] == 16
    vb.shell.sensors.set_values(vccbram_v=0.952)                       # back inside its window
    cleared = session.telemetry.sensors_act("clear_alm")
    assert cleared["alm"] == 0 and cleared["alarms"] == []


@pytest.mark.parametrize("fields, words", [
    ({"temp_hi_c": 130}, "at most 125"),
    ({"temp_hi_c": 0}, "above 0"),
    ({"temp_hi_c": 90, "temp_ot_c": 80}, "below the over-temperature"),
    ({"temp_hi_c": 120}, "below the over-temperature"),          # against the 100 degC in force
    ({"vccint_lo_v": 0.4}, "between 0.5 and 1.2"),
    ({"vccint_hi_v": 0.9}, "lower limit must be below"),         # lo is 0.922 in force
    ({"k": 21}, "between -20 and 20"),
    ({"k": "hot"}, "must be a number"),
    ({}, "nothing to set"),
    ({"bogus": 1}, "cannot set"),
])
def test_set_refuses_out_of_range_and_applies_nothing(mint4, fields, words):
    vb, _eng, session = mint4
    before = dict(vb.shell.sensors.thr), vb.shell.sensors.k
    with pytest.raises(UsageError) as exc:
        session.telemetry.sensors_act("set", fields)
    assert words in exc.value.message
    assert (dict(vb.shell.sensors.thr), vb.shell.sensors.k) == before


def test_a_mixed_request_with_one_bad_value_applies_none(mint4):
    vb, _eng, session = mint4
    with pytest.raises(UsageError):
        session.telemetry.sensors_act("set", {"temp_hi_c": 85, "vccint_lo_v": 2.0})
    assert vb.shell.sensors.thr["temp_hi_c"] == 80.0


def test_external_reference_k_is_a_scale(tmp_path, state):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        with_sensors(vb, ref="external")
        eng, session = open_board(vb, state)
        try:
            with pytest.raises(UsageError) as exc:
                session.telemetry.sensors_act("set", {"k": 5})
            assert "scale" in exc.value.message and "0.5 and 2" in exc.value.message
            assert session.telemetry.sensors_act("set", {"k": 1.04})["k"] == 1.04
        finally:
            eng.close_all()


def test_claim_locked_set_is_refused_in_plain_words_and_changes_nothing(mint4):
    vb, _eng, session = mint4
    vb.shell.ssh_claimed, vb.shell.trusted_peer = True, "127.0.0.3"
    assert session.telemetry.sensors_get()["ref"] == "internal"        # get stays open
    vb.shell.sensors.set_values(vccaux_v=1.2)
    assert session.telemetry.sensors_get()["alm"] == 8
    for act, fields in (("set", {"temp_hi_c": 85}), ("clear_alm", None)):
        with pytest.raises(RefusedError) as exc:
            session.telemetry.sensors_act(act, fields)
        assert "claim" in exc.value.message and "nothing was changed" in exc.value.message
    assert vb.shell.sensors.thr["temp_hi_c"] == 80.0 and vb.shell.sensors.alm == 8


def test_sensors_error_maps_each_code_to_a_sentence():
    for code, want in (("not_supported", "does not have sensors"), ("no_hw", "did not answer"),
                       ("busy", "busy"), ("timeout", "did not answer in time"),
                       ("denied", "does not allow")):
        err = tm.sensors_error({"ok": False, "err": f"x {code}", "code": code}, "reading")
        assert want in err.message, (code, err.message)
    locked = tm.sensors_error({"ok": False, "err": "sensors locked: board claimed (use ssh)",
                               "code": "locked"}, "changing the alarm thresholds")
    assert "board is claimed" in locked.message and isinstance(locked, RefusedError)
    assert "nothing was changed" in tm.sensors_error(
        {"ok": False, "err": "invalid temp_hi_c: x", "code": "invalid"}, "s").message


# --- old boards ----------------------------------------------------------------------------------


def test_a_board_without_sysmon_falls_back_and_refuses_sensors(tmp_path, state):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        eng, session = open_board(vb, state)
        try:
            rows = by_name(eng.telemetry.readings(session))
            assert rows["fpga_die_temp"].value is None and rows["fpga_die_temp"].source != tm.SENSORS_SOURCE
            with pytest.raises(UnavailableError) as exc:
                session.telemetry.sensors_get()
            assert "mint-4" in exc.value.message
        finally:
            eng.close_all()


# --- the daemon routes ---------------------------------------------------------------------------


def test_daemon_routes_get_set_clear_and_refusals(tmp_path, state):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        with_sensors(vb)
        eng = Engine(EngineConfig(state_dir=state), packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
                cand = eng.candidate_for(vb.shell_endpoint)
                from harness_manager.cli.output import jsonable
                assert c.post("/api/v1/boards", json={"candidate": jsonable(cand)},
                              headers=H).status_code == 200
                url = bid_path(cand.board_id) + "/sensors"
                got = c.get(url, headers=H).json()
                assert got["ok"] and got["sensors"]["temp_c"] == 41.2
                bad = c.post(url, json={"act": "set", "temp_hi_c": 500}, headers=H)
                assert bad.status_code == 400 and vb.shell.sensors.thr["temp_hi_c"] == 80.0
                assert c.post(url, json={"act": "nope"}, headers=H).status_code == 400
                good = c.post(url, json={"act": "set", "temp_hi_c": 85}, headers=H)
                assert good.status_code == 200 and vb.shell.sensors.thr["temp_hi_c"] == 85.0
                assert c.post(url, json={"act": "clear_alm"}, headers=H).status_code == 200
        finally:
            eng.close_all()


# --- the page's validator (the contract ranges, client-side) -----------------------------------------


def _run_validator(form, ref, thr):
    src = CLOCKS_JS.read_text()
    block = src.split("// SENSORS-VALIDATE-BEGIN")[1].split("// SENSORS-VALIDATE-END")[0]
    js = block + f"\nconsole.log(JSON.stringify(checkSensorForm({json.dumps(form)}, " \
                 f"{json.dumps(ref)}, {json.dumps(thr)})));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_validator_matches_the_contract_ranges():
    thr = {"temp_hi_c": 80, "temp_ot_c": 100, "vccint_lo_v": 0.922, "vccint_hi_v": 0.978}
    ok = _run_validator({"temp_hi_c": "85", "k": "-1.5"}, "internal", thr)
    assert ok == {"values": {"temp_hi_c": 85, "k": -1.5}, "errors": []}
    assert _run_validator({}, "internal", thr)["errors"] == ["Fill in at least one value."]
    assert _run_validator({"temp_hi_c": "126"}, "internal", thr)["errors"]
    assert _run_validator({"temp_hi_c": "110"}, "internal", thr)["errors"]      # >= ot in force
    assert _run_validator({"vccint_lo_v": "0.4"}, "internal", thr)["errors"]
    assert _run_validator({"k": "3"}, "external", thr)["errors"]
    assert _run_validator({"k": "3"}, "internal", thr)["errors"] == []
    assert _run_validator({"k": "abc"}, "internal", thr)["errors"]


def test_the_page_has_the_sensors_card_and_words():
    src = CLOCKS_JS.read_text()
    for needle in ("LCD module ambient", "Set alarm thresholds / calibration", "Clear alarms",
                   "Over temperature", "VCCINT outside its window", "Uncalibrated", "Calibrated"):
        assert needle in src, needle


def test_merge_keeps_the_raw_rail_min_max_and_lets_converted_values_win():
    from harness_manager_mps3.sysmon import sysmon_readings
    from tests.fakes.t9_shell import STATS_SYSMON

    raw = sysmon_readings(tm.stats_sysmon_sample({"ok": True, "sysmon": STATS_SYSMON}))
    conv = tm.sensors_reply_readings({"cal": "set", "temp_c": 50.0, "temp_max_c": 51.0,
                                      "temp_min_c": 30.0, "vccint_v": 0.95, "vccaux_v": 1.8,
                                      "vccbram_v": 0.95})
    merged = by_name(tm.merge_sensors(raw, conv))
    assert merged["fpga_die_temp"].value == 50.0 and merged["fpga_die_temp"].source == tm.SENSORS_SOURCE
    assert "calibrated by the harness" in merged["fpga_die_temp"].reason
    assert merged["vccint_max"].source == "sysmon (harness stats)"       # raw-only rows are kept
    assert len(merged) == len(raw)
