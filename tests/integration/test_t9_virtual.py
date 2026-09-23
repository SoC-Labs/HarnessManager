"""T9 end to end on the virtual MPS3: the pack's telemetry hook -> Mps3Telemetry -> the T1
Engine/TelemetryService, with and without a configured meter, and with a harness that
reports ``sysmon``/``touch_temp`` (a future profile). Loopback only. Each check has a twin."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness_manager.core.model import LinkKind
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.telemetry import (
    NO_POWER_SENSOR,
    NO_STATS_CODEC,
    Mps3Telemetry,
    make_power_adapter,
    with_config_links,
)
from tests.fakes.t9_fixtures import power_report
from tests.fakes.t9_plugs import FakeClock, FakeShelly
from tests.fakes.t9_shell import MISSING, install_v011_stats, t9_virtual_board
from tests.fakes.t9_sysmon import make_fake_xsdb
from tests.fakes.virtual_board import VirtualMps3

TCLSH = shutil.which("tclsh")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    # conftest's autouse fixture already points HARNESS_MANAGER_STATE_DIR here, so the engine and
    # the pack (which reads boards.toml by the state-dir rule) agree.
    return tmp_path / "state"


def engine_for(vb: VirtualMps3, state: Path) -> Engine:
    return Engine(EngineConfig(state_dir=state), packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})


def write_boards(state: Path, text: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    path = state / "boards.toml"
    path.write_text(text)
    path.chmod(0o600)


def by_name(rows):
    return {r.name: r for r in rows}


# --- the fielded board ----------------------------------------------------------------------


def test_fielded_board_without_a_meter_explains_every_row(vboard: VirtualMps3, state: Path):
    eng = engine_for(vboard, state)
    try:
        session = eng.open(eng.candidate_for(vboard.shell_endpoint))
        assert isinstance(session.telemetry, Mps3Telemetry)
        rows = eng.telemetry.readings(session)
        assert all(r.source for r in rows)
        assert all(r.available or r.reason for r in rows)            # never a silent 0
        power = by_name(rows)["board_power"]
        assert power.value is None and power.reason == NO_POWER_SENSOR and power.source == "power-meter"
        info = eng.info(session.candidate.board_id)
        assert "telemetry_power" not in info.capabilities
    finally:
        eng.close_all()


def test_fielded_harness_is_asked_nothing_beyond_its_identity(tmp_path: Path, state: Path):
    with t9_virtual_board(tmp_path, features=()) as vb:
        eng = engine_for(vb, state)
        try:
            session = eng.open(eng.candidate_for(vb.shell_endpoint))
            before = len(vb.shell.ops)
            for _ in range(3):
                eng.telemetry.readings(session)
            assert vb.shell.ops[before:] == ["ping", "version"]      # one identity read, once
        finally:
            eng.close_all()


# --- a configured meter -------------------------------------------------------------------------


def test_a_configured_shelly_is_read_and_labelled(vboard: VirtualMps3, state: Path):
    board_id = f"mps3@{vboard.shell_endpoint}"
    with FakeShelly() as plug:
        write_boards(state, f'[boards."{board_id}".power]\nkind = "shelly_gen2"\nurl = "{plug.url}"\n')
        eng = engine_for(vboard, state)
        try:
            session = eng.open(eng.candidate_for(vboard.shell_endpoint))
            rows = by_name(eng.telemetry.readings(session))
            assert rows["board_power"].value == 11.4 and rows["supply_voltage"].value == 239.1
            assert rows["board_power"].source == f"shelly_gen2 {plug.url} outlet 0"
            # Negative twin: the same plug hanging is unavailable, never 0.
            plug.hang = True
            session.telemetry._power.driver.http.timeout_s = 0.3
            rows = by_name(eng.telemetry.readings(session))
            assert rows["board_power"].value is None and "did not answer" in rows["board_power"].reason
        finally:
            eng.close_all()


def test_config_links_light_up_the_power_capability(vboard: VirtualMps3, state: Path):
    with FakeShelly() as plug:
        write_boards(state, f'[boards.lab]\nmatch = ["127.0.0.1"]\n'
                            f'power = {{ kind = "shelly_gen2", url = "{plug.url}" }}\n')
        eng = engine_for(vboard, state)
        try:
            # The pack adds the boards.toml links itself (T9 CCR 2, applied by the lead).
            cand = eng.candidate_for(vboard.shell_endpoint)
            assert any(lk.kind == LinkKind.SMART_POWER for lk in cand.links)
            assert with_config_links(cand) == cand
            eng.open(cand)
            info = eng.info(cand.board_id)
            assert {"telemetry_power", "reboot_board", "power_cycle"} <= info.capabilities
            eng.close(cand.board_id)
            # Negative twin: the same board once boards.toml has no power table.
            write_boards(state, '[boards.lab]\nmatch = ["127.0.0.1"]\n')
            cand = eng.candidate_for(vboard.shell_endpoint)
            assert not any(lk.kind == LinkKind.SMART_POWER for lk in cand.links)
            eng.open(cand)
            info = eng.info(cand.board_id)
            assert "no power sensor" in info.unavailable["telemetry_power"]
            assert "power_cycle" in info.unavailable
        finally:
            eng.close_all()


def test_a_meter_only_ina260_measures_but_cannot_power_cycle(vboard: VirtualMps3, state: Path):
    write_boards(state, '[boards.lab]\nmatch = ["127.0.0.1"]\n'
                        'power = { kind = "ina260_mcp2221", i2c_address = 0x41, device = 1 }\n')
    eng = engine_for(vboard, state)
    try:
        cand = eng.candidate_for(vboard.shell_endpoint)
        eng.open(cand)
        info = eng.info(cand.board_id)
        assert "telemetry_power" in info.capabilities
        assert "power_cycle" not in info.capabilities
        assert "only measures" in info.unavailable["power_cycle"]
    finally:
        eng.close_all()


def test_power_cycle_through_the_session_adapter(vboard: VirtualMps3, state: Path):
    board_id = f"mps3@{vboard.shell_endpoint}"
    with FakeShelly() as plug:
        write_boards(state, f'[boards."{board_id}".power]\nkind = "shelly_gen2"\nurl = "{plug.url}"\n')
        session = Mps3Pack(console_ports=vboard.console_ports).open(
            Mps3Pack().candidate_for_host(vboard.shell_endpoint))
        try:
            ev = make_power_adapter(session).power_cycle(5.0, wait=False)
        finally:
            session.close()
    assert ev["board_id"] == board_id and ev["confirmed_off"] is True and ev["confirmed_on"] is None
    assert plug.sets == [{"id": "0", "on": "false", "toggle_after": "5"}] and plug.output is False


# --- a harness that reports sysmon / touch_temp (future profile) ---------------------------------


@pytest.fixture
def v011_codec(monkeypatch) -> None:
    """pyverify with the v0.11 ``stats()`` codec (a stand-in when the installed one predates it)."""
    install_v011_stats(monkeypatch)


def test_harness_sysmon_and_touch_temp_through_the_engine(tmp_path: Path, state: Path, v011_codec):
    with t9_virtual_board(tmp_path) as vb:
        eng = engine_for(vb, state)
        try:
            session = eng.open(eng.candidate_for(vb.shell_endpoint))
            rows = by_name(eng.telemetry.readings(session))
            assert rows["fpga_die_temp"].value == 44.0
            assert rows["fpga_die_temp"].source == "sysmon (harness stats)"
            assert rows["vccaux"].value == pytest.approx(1.8, abs=1e-4)
            assert rows["lcd_ambient_temp"].value == 23.9
            assert rows["lcd_ambient_temp"].source == "stmpe811 (harness telemetry)"
            n = vb.shell.ops.count("stats")
            eng.telemetry.readings(session)                   # rate-limited: no second ask
            assert vb.shell.ops.count("stats") == n == 1
            info = eng.info(session.candidate.board_id)
            assert "telemetry_temp" in info.capabilities
        finally:
            eng.close_all()


@pytest.mark.parametrize("change,row,why", [
    ({"stats_supported": False}, "fpga_die_temp", "unknown op 'stats'"),
    ({"sysmon": MISSING}, "fpga_die_temp", "no sysmon object"),
    ({"touch_temp_c": None}, "lcd_ambient_temp", "touch_temp_c is null"),
    ({"touch_temp_c": MISSING}, "lcd_ambient_temp", "has no touch_temp_c"),
])
def test_harness_that_claims_a_feature_but_does_not_deliver(tmp_path: Path, state: Path, v011_codec,
                                                           change, row, why):
    with t9_virtual_board(tmp_path) as vb:
        for k, v in change.items():
            setattr(vb.shell, k, v)
        session = Mps3Pack(console_ports=vb.console_ports).open(
            Mps3Pack().candidate_for_host(vb.shell_endpoint))
        try:
            rows = by_name(session.telemetry.readings())
        finally:
            session.close()
    assert rows[row].value is None and why in rows[row].reason


def test_a_pyverify_without_stats_is_a_reason_not_a_hand_rolled_request(tmp_path: Path, state: Path,
                                                                        monkeypatch):
    from pyverify.client import ShellClient

    monkeypatch.delattr(ShellClient, "stats", raising=False)     # the pre-v0.11 codec
    with t9_virtual_board(tmp_path) as vb:
        session = Mps3Pack(console_ports=vb.console_ports).open(
            Mps3Pack().candidate_for_host(vb.shell_endpoint))
        try:
            rows = by_name(session.telemetry.readings())
        finally:
            session.close()
        ops = list(vb.shell.ops)
    assert rows["fpga_die_temp"].value is None and rows["fpga_die_temp"].reason == NO_STATS_CODEC
    assert "stats" not in ops and "telemetry" in ops
    assert rows["lcd_ambient_temp"].value == 23.9                # touch_temp still works


# --- USB only: the T3 MCC rows and the T9 rows side by side ---------------------------------------


def test_usb_only_session_aggregates_mcc_and_t9_rows(tmp_path: Path, state: Path, monkeypatch):
    with VirtualMps3(tmp_path, usb=True) as vb:
        clock = FakeClock()
        monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
        monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
        vb.mcc.clock = clock
        eng = engine_for(vb, state)
        try:
            session = eng.open(vb.candidate(ethernet=False, usb=True))
            rows = by_name(eng.telemetry.readings(session))
        finally:
            eng.close_all()
    assert rows["mcc_temp"].value == 35.5 and rows["mcc_temp"].source == "mcc-console"   # T3, via T1
    assert rows["lcd_ambient_temp"].reason.startswith("needs the Ethernet link")          # T9
    assert rows["board_power"].reason == NO_POWER_SENSOR
    assert all(r.source for r in rows.values())


# --- JTAG SYSMON and the estimate, configured in boards.toml -------------------------------------


@pytest.mark.skipif(TCLSH is None, reason="no tclsh for the Tcl model of xsdb")
def test_jtag_sysmon_and_estimate_from_boards_toml(vboard: VirtualMps3, state: Path, tmp_path: Path):
    exe, _log = make_fake_xsdb(tmp_path / "xsdb", tclsh=TCLSH)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "power_rm_greybox.rpt").write_text(power_report(total="1.650"))
    board_id = f"mps3@{vboard.shell_endpoint}"
    write_boards(state, f'[boards."{board_id}".sysmon]\nbackend = "xsdb"\nxsdb = "{exe}"\n'
                        f'[boards."{board_id}".estimates]\nvivado_reports = "{reports}"\n')
    eng = engine_for(vboard, state)
    try:
        cand = eng.candidate_for(vboard.shell_endpoint)
        session = eng.open(with_config_links(cand))
        rows = by_name(eng.telemetry.readings(session))
        assert rows["fpga_die_temp"].value == 44.0
        assert rows["fpga_die_temp"].source == "sysmon-jtag (xsdb tcp:127.0.0.1:3121)"
        est = rows["onchip_power_estimate"]
        assert est.value == 1.65 and est.source == "estimate:vivado greybox"
        assert est.reason.startswith("ESTIMATE")
        assert "telemetry_temp" in eng.info(cand.board_id).capabilities     # via the JTAG link
    finally:
        eng.close_all()
