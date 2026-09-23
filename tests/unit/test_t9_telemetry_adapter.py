"""T9: the MPS3 telemetry adapter with fake sessions (no network). Rows always carry a
source; an unavailable row always says why; each check has a negative twin."""

from __future__ import annotations

from pathlib import Path

import pytest

from socharness.core.errors import HeldError, UnavailableError
from socharness.core.model import BoardIdentity, Candidate, Link, LinkKind, Reading
from socharness.power.adapter import PowerAdapter
from socharness_board_mps3 import telemetry as tm
from socharness_board_mps3.sysmon import SysmonSample
from socharness_board_mps3.telemetry import (
    NO_ESTIMATES,
    NO_POWER_SENSOR,
    Mps3Telemetry,
    config_links,
    make_power_adapter,
    make_telemetry_adapter,
    stats_sysmon_sample,
    touch_readings,
    with_config_links,
)
from tests.fakes.t9_fixtures import power_report
from tests.fakes.t9_plugs import FakeClock
from tests.fakes.t9_shell import STATS_SYSMON
from tests.fakes.t9_sysmon import GOOD_REGS

BOARD = "mps3@192.168.10.101:6900"
ETH = Link(LinkKind.ETHERNET, "192.168.10.101:6900")


class FakeTap:
    """``Mps3Shell.call_raw``'s tap: the reply line pyverify parsed last."""

    last: dict = {}


class FakeResponse:
    def __init__(self, raw: dict) -> None:
        self.ok = bool(raw.get("ok"))
        self.raw = raw


class FakeClient:
    """pyverify's ShellClient verbs T9 uses. ``stats`` only when the codec has it (v0.11)."""

    def __init__(self, replies: dict[str, dict], tap: FakeTap, *, has_stats: bool = True) -> None:
        self.replies = replies
        self.tap = tap
        self.ops: list[str] = []
        if not has_stats:
            self.stats = None

    def _reply(self, op: str) -> dict:
        self.ops.append(op)
        self.tap.last = dict(self.replies[op])
        return self.tap.last

    def telemetry(self):
        return FakeResponse(self._reply("telemetry"))

    def stats(self):
        return FakeResponse(self._reply("stats"))


class FakeShell:
    def __init__(self, replies: dict[str, dict] | None = None, *, error: Exception | None = None,
                 has_stats: bool = True):
        self.tap = FakeTap()
        self.client = FakeClient(replies or {}, self.tap, has_stats=has_stats)
        self.error = error
        self.calls = 0

    def call_raw(self, fn):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return fn(self.client, self.tap)


class FakeSession:
    def __init__(self, *, shell=None, features=(), rm_name="nanosoc", probe_identity=False,
                 ident_error: Exception | None = None, links=(ETH,)) -> None:
        self.ident = BoardIdentity("mps3", shell_id="0x3f1a560f", rm_id="0x01000001",
                                   rm_name=rm_name, features=tuple(features))
        self.candidate = Candidate("mps3", BOARD, tuple(links),
                                   identity=self.ident if probe_identity else None)
        self.shell = shell
        self.ident_error = ident_error
        self.identity_calls = 0

    def identity(self) -> BoardIdentity:
        self.identity_calls += 1
        if self.ident_error is not None:
            raise self.ident_error
        return self.ident


def names(rows: list[Reading]) -> list[str]:
    return [r.name for r in rows]


# --- pure parsers --------------------------------------------------------------------------


def test_touch_readings_every_shape():
    ok = touch_readings({"ok": False, "err": "no power sensor", "touch_temp_c": 24.25})
    assert (ok[0].name, ok[0].value, ok[0].unit, ok[0].source) == (
        "lcd_ambient_temp", 24.25, "degC", "stmpe811 (harness telemetry)")
    assert "not the FPGA die" in ok[0].reason
    for reply, why in (({"ok": False}, "has no touch_temp_c"),
                       ({"touch_temp_c": None}, "touch_temp_c is null"),
                       ({"touch_temp_c": "hot"}, "not a number"),
                       ({"touch_temp_c": True}, "not a number"),
                       (None, "no telemetry reply")):
        (r,) = touch_readings(reply)
        assert r.value is None and why in r.reason
    (r,) = touch_readings(None, "cannot ask the harness: busy")
    assert r.reason == "cannot ask the harness: busy"


def test_stats_sysmon_sample_shapes():
    s = stats_sysmon_sample({"ok": True, "sysmon": STATS_SYSMON}, observed_at=5.0)
    assert isinstance(s, SysmonSample) and s.codes == GOOD_REGS and s.errors == {}
    assert s.observed_at == 5.0 and s.source == "sysmon (harness stats)"
    partial = stats_sysmon_sample({"ok": True, "sysmon": {"temp": 0xA235, "flag": "x"}})
    assert partial.codes == {0x00: 0xA235}
    assert partial.errors[0x3F] == "reported as 'x'" and partial.errors[0x01] == "not reported by the harness"
    assert "unknown op" in stats_sysmon_sample({"ok": False, "err": "unknown op 'stats'"})
    assert "no sysmon object" in stats_sysmon_sample({"ok": True})
    assert "none of the expected raw-code keys" in stats_sysmon_sample({"ok": True, "sysmon": {"t": 1}})
    assert stats_sysmon_sample(tm.NO_STATS_CODEC) == tm.NO_STATS_CODEC     # a reason passes through


# --- the adapter ---------------------------------------------------------------------------


def test_nothing_configured_nothing_reported_gives_four_honest_rows():
    session = FakeSession(shell=FakeShell(), probe_identity=True)
    rows = Mps3Telemetry(session).readings()
    assert names(rows) == ["fpga_die_temp", "lcd_ambient_temp", "board_power", "onchip_power_estimate"]
    assert all(r.value is None and r.reason and r.source for r in rows)
    assert "harness firmware with 'sysmon'" in rows[0].reason
    assert rows[1].reason == "needs harness firmware with 'touch_temp'"
    assert rows[2].reason == NO_POWER_SENSOR and rows[3].reason == NO_ESTIMATES
    # No network: the probe-time identity answered the feature question.
    assert session.identity_calls == 0 and session.shell.calls == 0


def test_without_a_probe_identity_it_asks_once():
    session = FakeSession(shell=FakeShell())
    adapter = Mps3Telemetry(session)
    adapter.readings()
    adapter.readings()
    assert session.identity_calls == 1


def test_no_ethernet_link_says_so():
    rows = Mps3Telemetry(FakeSession(shell=None, links=())).readings()
    assert "or the Ethernet link and harness firmware with 'sysmon'" in rows[0].reason
    assert rows[1].reason.startswith("needs the Ethernet link")
    assert "unknown (no Ethernet link" not in rows[3].reason          # estimates not configured


def test_identity_failure_is_the_reason_and_is_retried_soon():
    clock = FakeClock()
    session = FakeSession(shell=FakeShell(), ident_error=HeldError("6900 busy"))
    adapter = Mps3Telemetry(session, clock=clock)
    rows = adapter.readings()
    assert "cannot ask the harness: 6900 busy" in rows[0].reason
    assert rows[1].reason == "cannot ask the harness: 6900 busy"
    # The board comes back: after the short retry interval the real answer is used.
    session.ident_error = None
    clock.sleep(tm.SHELL_MIN_INTERVAL_S + 0.1)
    assert adapter.readings()[1].reason == "needs harness firmware with 'touch_temp'"
    calls = session.identity_calls
    adapter.readings()                                   # a good identity is kept longer
    assert session.identity_calls == calls


def test_a_meter_that_raises_does_not_hide_the_other_rows():
    class Broken:
        label = "broken meter"

        def read(self):
            raise OSError("gone")

    session = FakeSession(shell=None, links=())
    session.power = Broken()
    rows = {r.name: r for r in Mps3Telemetry(session).readings()}
    assert rows["board_power"].value is None and rows["board_power"].reason == "OSError: gone"
    assert rows["board_power"].source == "broken meter" and "fpga_die_temp" in rows


def test_harness_features_read_in_one_rate_limited_connection():
    clock = FakeClock()
    shell = FakeShell({"telemetry": {"ok": False, "err": "no power sensor", "touch_temp_c": 22.5},
                       "stats": {"ok": True, "sysmon": STATS_SYSMON}})
    session = FakeSession(shell=shell, features=("sysmon", "touch_temp"), probe_identity=True)
    adapter = Mps3Telemetry(session, clock=clock)
    rows = {r.name: r for r in adapter.readings()}
    assert rows["fpga_die_temp"].value == 44.0 and rows["fpga_die_temp"].source == "sysmon (harness stats)"
    assert rows["lcd_ambient_temp"].value == 22.5
    assert shell.calls == 1 and shell.client.ops == ["telemetry", "stats"]
    adapter.readings()                                   # within SHELL_MIN_INTERVAL_S: cached
    assert shell.calls == 1
    clock.sleep(tm.SHELL_MIN_INTERVAL_S)
    adapter.readings()
    assert shell.calls == 2


def test_a_pyverify_without_stats_says_so_and_sends_nothing_hand_rolled():
    shell = FakeShell({"telemetry": {"ok": False, "touch_temp_c": 22.5}}, has_stats=False)
    session = FakeSession(shell=shell, features=("sysmon", "touch_temp"), probe_identity=True)
    rows = Mps3Telemetry(session).readings()
    sysmon = [r for r in rows if r.source == "sysmon (harness stats)"]
    assert sysmon and all(r.value is None and r.reason == tm.NO_STATS_CODEC for r in sysmon)
    assert shell.client.ops == ["telemetry"]                   # stats was never sent
    assert next(r for r in rows if r.name == "lcd_ambient_temp").value == 22.5


def test_only_the_reported_feature_is_asked_for():
    shell = FakeShell({"telemetry": {"ok": False, "touch_temp_c": 22.5}})
    session = FakeSession(shell=shell, features=("touch_temp",), probe_identity=True)
    rows = Mps3Telemetry(session).readings()
    assert shell.client.ops == ["telemetry"]
    assert rows[0].name == "fpga_die_temp" and rows[0].value is None


def test_a_busy_shell_is_a_reason_on_both_rows():
    shell = FakeShell(error=HeldError("shell closed the connection", hint="one client"))
    session = FakeSession(shell=shell, features=("sysmon", "touch_temp"), probe_identity=True)
    rows = Mps3Telemetry(session).readings()
    sysmon = [r for r in rows if r.source == "sysmon (harness stats)"]
    assert names(sysmon) == ["fpga_die_temp", "vccint", "vccaux", "vccbram"]
    assert all("cannot ask the harness: shell closed the connection" in r.reason for r in sysmon)
    touch = next(r for r in rows if r.name == "lcd_ambient_temp")
    assert touch.value is None and "shell closed the connection" in touch.reason


class FakeReader:
    source = "sysmon-jtag (fake)"

    def __init__(self, sample: SysmonSample | None = None, error: Exception | None = None):
        self.sample, self.error, self.calls = sample, error, 0

    def read(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.sample


def test_jtag_sysmon_rows_and_its_rate_limit():
    clock = FakeClock()
    reader = FakeReader(SysmonSample(dict(GOOD_REGS), source="sysmon-jtag (fake)"))
    adapter = Mps3Telemetry(FakeSession(shell=None, links=()), sysmon=reader,
                            sysmon_interval_s=10, clock=clock)
    rows = adapter.readings()
    assert names(rows)[:3] == ["fpga_die_temp", "fpga_die_temp_max", "fpga_die_temp_min"]
    assert rows[0].value == 44.0 and rows[0].source == "sysmon-jtag (fake)"
    adapter.readings()
    assert reader.calls == 1
    clock.sleep(10)
    adapter.readings()
    assert reader.calls == 2


def test_jtag_failure_is_four_rows_with_the_reason_and_is_also_rate_limited():
    reader = FakeReader(error=UnavailableError("sysmon", "cannot reach hw_server at tcp:x:1: refused"))
    adapter = Mps3Telemetry(FakeSession(shell=None, links=()), sysmon=reader, clock=FakeClock())
    rows = adapter.readings()[:4]
    assert names(rows) == ["fpga_die_temp", "vccint", "vccaux", "vccbram"]
    assert all(r.value is None and "cannot reach hw_server" in r.reason for r in rows)
    adapter.readings()
    assert reader.calls == 1


def test_power_rows_prefer_the_sessions_power_adapter():
    class Meter:
        kind, label, cycle_reason = "fake", "fake meter", ""

        def __init__(self, w):
            self.w = w

        def read(self):
            return [Reading("board_power", self.w, "W", self.label)]

    own = PowerAdapter(Meter(10.0))
    session = FakeSession(shell=None, links=())
    assert Mps3Telemetry(session, power=own).readings()[2].value == 10.0
    session.power = PowerAdapter(Meter(12.5))
    assert Mps3Telemetry(session, power=own).readings()[2].value == 12.5


def test_estimate_for_the_loaded_design(tmp_path: Path):
    (tmp_path / "power_rm_nanosoc.rpt").write_text(power_report(total="2.345"))
    (tmp_path / "power_rm_led.rpt").write_text(power_report(total="1.800"))
    session = FakeSession(shell=FakeShell(), probe_identity=True, rm_name="nanosoc")
    rows = Mps3Telemetry(session, estimates_dir=tmp_path).readings()
    est = [r for r in rows if r.source == "estimate:vivado nanosoc"]
    assert names(est) == ["onchip_power_estimate", "onchip_dynamic_estimate",
                          "onchip_static_estimate", "junction_temp_estimate"]
    assert est[0].value == 2.345 and est[0].reason.startswith("ESTIMATE")
    # Negative twin: a design with no report says which ones exist.
    other = FakeSession(shell=FakeShell(), probe_identity=True, rm_name="nanosoc_upy")
    (r,) = [r for r in Mps3Telemetry(other, estimates_dir=tmp_path).readings()
            if r.name == "onchip_power_estimate"]
    assert r.value is None and "no Vivado power report for 'nanosoc_upy'" in r.reason
    assert "(found: led, nanosoc)" in r.reason


# --- the hook, with boards.toml in the state dir --------------------------------------------------


@pytest.fixture
def boards_toml(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SOCHARNESS_STATE_DIR", str(state))
    return state / "boards.toml"


def test_hook_with_no_file_still_explains(boards_toml: Path):
    adapter = make_telemetry_adapter(FakeSession(shell=None, links=()))
    assert adapter.board is None and adapter.config_error == ""
    assert make_power_adapter(FakeSession(shell=None, links=())) is None
    assert config_links(Candidate("mps3", BOARD, (ETH,))) == ()


def test_hook_reads_the_board_table(boards_toml: Path, tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    boards_toml.write_text(f"""
[boards.lab]
match = ["192.168.10.101"]
power = {{ kind = "netio", url = "http://pdu.lab", outlet = 2, auth = {{ password = "pw-t9-x" }} }}
sysmon = {{ backend = "xsdb", hw_server = "tcp:hub.lab:3121", min_interval_s = 30 }}
estimates = {{ vivado_reports = "{reports}" }}
""")
    boards_toml.chmod(0o600)
    session = FakeSession(shell=None)
    adapter = make_telemetry_adapter(session)
    assert adapter.board.key == "lab" and adapter.sysmon is not None
    assert adapter.sysmon_interval_s == 30.0 and adapter.estimates_dir == reports
    assert isinstance(make_power_adapter(session), PowerAdapter)
    links = config_links(session.candidate)
    assert [(lk.kind, lk.address) for lk in links] == [
        (LinkKind.SMART_POWER, "http://pdu.lab"), (LinkKind.JTAG, "xsdb tcp:hub.lab:3121")]
    assert all("pw-t9-x" not in repr(lk) for lk in links)
    cand = with_config_links(session.candidate)
    assert [lk.kind for lk in cand.links] == [LinkKind.ETHERNET, LinkKind.SMART_POWER, LinkKind.JTAG]


def test_hook_with_a_broken_file_puts_the_error_on_the_rows(boards_toml: Path):
    boards_toml.write_text("[boards\n")
    rows = make_telemetry_adapter(FakeSession(shell=None, links=())).readings()
    by = {r.name: r for r in rows}
    for name in ("fpga_die_temp", "board_power", "onchip_power_estimate"):
        assert "not valid TOML" in by[name].reason and by[name].value is None
    # Negative twin: the harness-only row does not depend on boards.toml.
    assert "not valid TOML" not in by["lcd_ambient_temp"].reason


def test_hook_with_bad_sysmon_and_estimates_tables(boards_toml: Path, tmp_path: Path):
    boards_toml.write_text(f"""
[boards."{BOARD}"]
sysmon = {{ backend = "jlink" }}
estimates = {{ vivado_reports = "{tmp_path / 'missing'}" }}
""")
    rows = make_telemetry_adapter(FakeSession(shell=None, links=())).readings()
    by = {r.name: r for r in rows}
    assert "boards.toml sysmon: sysmon.backend must be 'xsdb' or 'openocd'" in by["fpga_die_temp"].reason
    assert "is not a directory" in by["onchip_power_estimate"].reason
    assert config_links(Candidate("mps3", BOARD, ())) == ()            # a bad table adds no link
