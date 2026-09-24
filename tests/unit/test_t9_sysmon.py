"""T9: SYSMON over JTAG. Conversions, the REF-bit caveat, read-only commands, and the tool
failures, with faked subprocesses (plus a real tclsh running our xsdb script against a Tcl
model of xsdb). Each check has a negative twin."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness_manager.core.errors import UnavailableError, UsageError
from harness_manager.core.model import LinkKind
from harness_manager_mps3.sysmon import (
    FLAG_JTGD,
    FLAG_OT,
    FLAG_REF,
    IR_SYSMON_DRP,
    READ_REGS,
    OpenOcdSysmon,
    SysmonSample,
    XsdbSysmon,
    make_sysmon_reader,
    parse_output,
    read_command,
    supply_v,
    sysmon_link,
    sysmon_readings,
    temp_c,
)
from tests.fakes.t9_sysmon import (
    GOOD_REGS,
    ScriptedRunner,
    code_for_v,
    logged_commands,
    make_fake_xsdb,
    marker_output,
)

TCLSH = shutil.which("tclsh")


def by_name(rows):
    return {r.name: r for r in rows}


# --- the register interface ------------------------------------------------------------


def test_read_commands_are_reads_only():
    assert read_command(0x00) == 0x04000000
    assert read_command(0x01) == 0x04010000
    assert read_command(0x3F) == 0x043F0000
    for a in READ_REGS:
        word = read_command(a)
        assert (word >> 26) & 0xF == 0b0001 and (word >> 16) & 0x3FF == a and word & 0xFFFF == 0
    with pytest.raises(ValueError):
        read_command(0x400)


def test_conversions_against_known_codes():
    # UG580 Eq. 2-7 / 2-5 and the supply transfer function, at hand-computed points.
    assert temp_c(0x0000) == pytest.approx(-273.6777)
    assert temp_c(0x8000) == pytest.approx(501.3743 / 2 - 273.6777)          # -22.99
    assert temp_c(0x8000, external_ref=True) == pytest.approx(502.9098 / 2 - 273.8195)
    assert temp_c(0xA235) == pytest.approx(44.0, abs=0.01)
    assert supply_v(0x8000) == 1.5 and supply_v(0x999A) == pytest.approx(1.8, abs=1e-4)
    assert supply_v(0x5111) == pytest.approx(0.95, abs=1e-4)


def test_readings_from_a_good_sample():
    rows = by_name(sysmon_readings(SysmonSample(dict(GOOD_REGS), source="sysmon-jtag (xsdb x)")))
    assert rows["fpga_die_temp"].value == 44.0 and rows["fpga_die_temp"].unit == "degC"
    assert rows["fpga_die_temp_max"].value == 51.5 and rows["fpga_die_temp_min"].value == 30.25
    assert rows["vccint"].value == pytest.approx(0.951, abs=1e-4)
    assert rows["vccaux"].value == pytest.approx(1.800, abs=1e-4)
    assert rows["vccbram_min"].value == pytest.approx(0.941, abs=1e-4)
    assert len(rows) == 12 and all(r.source == "sysmon-jtag (xsdb x)" for r in rows.values())
    assert rows["fpga_die_temp"].reason == ""          # internal reference: no caveat
    assert "since power-up" in rows["vccaux_max"].reason


def test_external_reference_flags_every_reading():
    regs = {**GOOD_REGS, 0x3F: 0x0000, 0x02: code_for_v(1.875)}      # 1.2 V ref: ~4.2 % high
    rows = sysmon_readings(SysmonSample(regs))
    assert all("external reference (flag 3Fh REF=0)" in r.reason for r in rows)
    assert "VCCAUX reads 1.875 V against 1.800 V nominal (+4.2 %)" in rows[0].reason
    # The external-reference transfer function is used for temperature.
    assert by_name(rows)["fpga_die_temp"].value == round(temp_c(GOOD_REGS[0], external_ref=True), 2)
    # Negative twin: REF=1 (internal) carries no reference caveat.
    internal = sysmon_readings(SysmonSample({**GOOD_REGS, 0x3F: FLAG_REF}))
    assert not any("reference" in r.reason for r in internal)


def test_unknown_reference_when_the_flag_was_not_read():
    regs = {k: v for k, v in GOOD_REGS.items() if k != 0x3F}
    rows = sysmon_readings(SysmonSample(regs, errors={0x3F: "read failed: chain broken"}))
    assert all("reference unknown: flag register 3Fh read failed: chain broken" in r.reason
               for r in rows)


def test_jtag_disabled_by_the_bitstream():
    rows = sysmon_readings(SysmonSample({**GOOD_REGS, 0x3F: FLAG_REF | FLAG_JTGD}))
    assert [r.name for r in rows] == ["fpga_die_temp", "vccint", "vccaux", "vccbram"]
    assert all(r.value is None and "JTGD" in r.reason for r in rows)


def test_empty_codes_are_unavailable_never_a_value():
    rows = by_name(sysmon_readings(SysmonSample({**GOOD_REGS, 0x00: 0x0000, 0x20: 0xFFFF})))
    assert rows["fpga_die_temp"].value is None and "0x0000" in rows["fpga_die_temp"].reason
    assert rows["fpga_die_temp_max"].value is None and "0xFFFF" in rows["fpga_die_temp_max"].reason
    assert rows["fpga_die_temp_min"].value == 30.25                   # twin: the others survive


def test_implausible_value_keeps_its_value_with_a_warning():
    rows = by_name(sysmon_readings(SysmonSample({**GOOD_REGS, 0x02: code_for_v(0.9)})))
    assert rows["vccaux"].value == pytest.approx(0.9, abs=1e-4)
    assert "outside the plausible 1.62..1.98 V" in rows["vccaux"].reason
    assert "plausible" not in rows["vccint"].reason


def test_over_temperature_flag_is_reported():
    rows = by_name(sysmon_readings(SysmonSample({**GOOD_REGS, 0x3F: FLAG_REF | FLAG_OT})))
    assert "over-temperature" in rows["fpga_die_temp"].reason
    assert "over-temperature" not in rows["vccint"].reason


def test_missing_register_is_unavailable_with_why():
    regs = {k: v for k, v in GOOD_REGS.items() if k != 0x06}
    rows = by_name(sysmon_readings(SysmonSample(regs, errors={0x06: "read failed: busy"})))
    assert rows["vccbram"].value is None and rows["vccbram"].reason == "SYSMON register 06h read failed: busy"


# --- tool output ------------------------------------------------------------------------


def test_parse_output_values_errors_and_fatal():
    text = ("noise\nHARNESS_MANAGER_SYSMON 00 0000a235\nHARNESS_MANAGER_SYSMON 3f 003f0200\n"
            "HARNESS_MANAGER_SYSMON_ERR read 06 JTAG busy\n"
            'in procedure: echo "HARNESS_MANAGER_SYSMON 01 [format %08x 0x$v]"\n')   # echoed template
    out = parse_output(text)
    assert out.codes == {0x00: 0xA235, 0x3F: 0x0200}
    assert out.errors == {0x06: "read failed: JTAG busy"} and out.fatal is None
    assert parse_output("HARNESS_MANAGER_SYSMON_ERR connect Connection refused").fatal == (
        "connect", "Connection refused")


# --- backend (a): xsdb, faked subprocess -------------------------------------------------


@pytest.fixture
def fake_tool(tmp_path: Path) -> str:
    exe = tmp_path / "tool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return str(exe)


def test_xsdb_script_is_read_only_and_parsed(fake_tool):
    runner = ScriptedRunner(marker_output(GOOD_REGS))
    sample = XsdbSysmon(xsdb=fake_tool, hw_server="tcp:hub.lab:3121", runner=runner,
                        clock=lambda: 42.0).read()
    assert sample.codes == GOOD_REGS and sample.observed_at == 42.0
    assert sample.source == "sysmon-jtag (xsdb tcp:hub.lab:3121)"
    (script,) = runner.scripts
    assert "connect -url {tcp:hub.lab:3121}" in script
    assert f"irshift -state IDLE -integer 12 {IR_SYSMON_DRP}" in script
    assert "-capture -integer 32 0" in script and "$s run -integer" in script
    words = [int(w) for w in script.split("foreach {addr cmd} {")[1].split("}")[0].split()[1::2]]
    assert words == [read_command(a) for a in READ_REGS]
    assert all((w >> 26) & 0xF == 1 for w in words)                     # reads only
    assert runner.kwargs[0]["timeout"] == 60.0


@pytest.mark.parametrize("stdout,expect", [
    ("HARNESS_MANAGER_SYSMON_ERR connect Connection refused",
     "cannot reach hw_server at tcp:127.0.0.1:3121: Connection refused"),
    ("HARNESS_MANAGER_SYSMON_ERR target no targets found",
     "no xcku115* on the JTAG chain behind tcp:127.0.0.1:3121: no targets found (is the JTAG cable on J17"),
    ("xsdb% something odd", "xsdb printed no SYSMON values: xsdb% something odd"),
])
def test_xsdb_failures_are_unavailable_with_the_reason(fake_tool, stdout, expect):
    with pytest.raises(UnavailableError) as err:
        XsdbSysmon(xsdb=fake_tool, runner=ScriptedRunner(stdout)).read()
    assert expect in err.value.reason


def test_xsdb_missing_tool_and_timeout(fake_tool):
    runner = ScriptedRunner()
    with pytest.raises(UnavailableError, match="xsdb not found"):
        XsdbSysmon(xsdb="definitely-not-xsdb-t9", runner=runner).read()
    assert runner.calls == []                                           # nothing was run
    with pytest.raises(UnavailableError, match="did not finish within 5 s"):
        XsdbSysmon(xsdb=fake_tool, timeout_s=5, runner=ScriptedRunner(
            raises=subprocess.TimeoutExpired("xsdb", 5))).read()
    with pytest.raises(UnavailableError, match="xsdb not found"):
        XsdbSysmon(xsdb=fake_tool, runner=ScriptedRunner(raises=FileNotFoundError())).read()
    # Found but not runnable (Windows: an extension-less script) is a reason, not a crash.
    with pytest.raises(UnavailableError, match=r"cannot run xsdb .*xsdb\.bat"):
        XsdbSysmon(xsdb=fake_tool, runner=ScriptedRunner(
            raises=OSError(8, "Exec format error"))).read()


@pytest.mark.parametrize("kw", [
    {"hw_server": "tcp:1.2.3.4:3121} ; exec rm -rf ~ ; {"},
    {"hw_server": "http://hub:3121"},
    {"device": 'xcku115" || 1 || "'},
])
def test_values_pasted_into_tcl_are_validated(kw):
    with pytest.raises(UsageError):
        XsdbSysmon(**kw)
    XsdbSysmon(hw_server="tcp:[::1]:3121", device="xcku115")          # twin: valid ones pass


# --- backend (a) through a real Tcl interpreter ----------------------------------------------


@pytest.mark.skipif(TCLSH is None, reason="no tclsh")
@pytest.mark.parametrize("mode,expect", [
    ("ok", None),
    ("refuse", "cannot reach hw_server"),
    ("notarget", "no xcku115* on the JTAG chain"),
])
def test_xsdb_script_runs_in_tcl(tmp_path: Path, mode, expect):
    exe, log = make_fake_xsdb(tmp_path, mode=mode, tclsh=TCLSH)
    reader = XsdbSysmon(xsdb=str(exe), timeout_s=30)
    if expect:
        with pytest.raises(UnavailableError, match=re.escape(expect)):
            reader.read()
        return
    sample = reader.read()
    assert sample.codes == GOOD_REGS and sample.errors == {}
    cmds = logged_commands(log)
    # Each register: a read command, then the no-op shift that returns its data.
    assert cmds == [c for a in READ_REGS for c in ((1, a), (0, 0))]
    assert all(c in (0, 1) for c, _a in cmds)                              # never a write


@pytest.mark.skipif(TCLSH is None, reason="no tclsh")
def test_xsdb_script_reports_a_failed_register(tmp_path: Path):
    exe, _log = make_fake_xsdb(tmp_path, mode="readfail", tclsh=TCLSH)
    sample = XsdbSysmon(xsdb=str(exe), timeout_s=30).read()
    assert 0x3F not in sample.codes and "JTAG chain broken" in sample.errors[0x3F]
    assert sample.codes[0x00] == GOOD_REGS[0x00]


# --- backend (b): OpenOCD, faked subprocess -------------------------------------------------


ADAPTER = ["source [find interface/ftdi/digilent-hs3.cfg]", "adapter serial 210299ABCDEF"]


def test_openocd_argv_order_and_read_only(fake_tool):
    r = OpenOcdSysmon(openocd=fake_tool, adapter=ADAPTER, search=("/cfg",))
    argv = r.argv(fake_tool)
    cmds = argv[4::2]
    assert argv[:3] == [fake_tool, "-s", "/cfg"] and set(argv[3::2]) == {"-c"}
    assert cmds[:2] == ADAPTER and cmds[2] == "transport select jtag"
    assert cmds[3] == "adapter speed 1000"
    assert cmds[4] == "jtag newtap ku115 tap -irlen 12 -expected-id 0x0390d093 -ignore-version"
    assert cmds[5] == "init" and cmds[-1] == "shutdown"
    joined = " ".join(cmds)
    assert "irscan ku115.tap 0xde4" in joined and "irscan ku115.tap 0x249" in joined
    for a in READ_REGS:
        assert f"harness_manager_rd {read_command(a)}" in joined
    assert "drscan ku115.tap 32 0]" in joined


def test_openocd_success_and_failures(fake_tool):
    ok = OpenOcdSysmon(openocd=fake_tool, adapter=ADAPTER,
                       runner=ScriptedRunner("", stderr=marker_output(GOOD_REGS))).read()
    assert ok.codes == GOOD_REGS and ok.source == "sysmon-jtag (openocd)"
    cases = [
        ("Warn : JTAG tap: ku115.tap UNEXPECTED: 0x6ba00477 (mfg...)", "not a KU115 (IDCODE 0x6ba00477)"),
        ("Error: JTAG scan chain interrogation failed: all ones", "scan chain all ones"),
        ("Error: unable to open ftdi device with vid 0403", "no FTDI JTAG adapter found"),
        ("HARNESS_MANAGER_SYSMON_ERR target IDCODE 0x13631093 is not a KU115", "openocd: IDCODE 0x13631093"),
        ("Error: something else broke\n", "Error: something else broke"),
    ]
    for stderr, expect in cases:
        with pytest.raises(UnavailableError) as err:
            OpenOcdSysmon(openocd=fake_tool, adapter=ADAPTER, runner=ScriptedRunner("", stderr)).read()
        assert expect in err.value.reason


# --- configuration --------------------------------------------------------------------------


def test_make_sysmon_reader_from_tables():
    x = make_sysmon_reader({"backend": "xsdb", "xsdb": "/opt/xsdb", "hw_server": "tcp:hub:3121",
                            "timeout_s": 30, "min_interval_s": 5})
    assert isinstance(x, XsdbSysmon) and x.timeout_s == 30.0 and x.hw_server == "tcp:hub:3121"
    assert sysmon_link(x).kind == LinkKind.JTAG and sysmon_link(x).address == "xsdb tcp:hub:3121"
    o = make_sysmon_reader({"backend": "openocd", "adapter": "source [find x.cfg]", "speed_khz": 500})
    assert isinstance(o, OpenOcdSysmon) and o.adapter == ("source [find x.cfg]",)
    assert sysmon_link(o).address == "openocd"
    for bad, msg in (({"backend": "jlink"}, "must be 'xsdb' or 'openocd'"),
                     ({"backend": "openocd"}, "adapter is required"),
                     ({"backend": "xsdb", "adapter": []}, "unknown keys"),
                     ({"backend": "openocd", "adapter": [1]}, "list of OpenOCD commands")):
        with pytest.raises(UsageError, match=msg):
            make_sysmon_reader(bad)
