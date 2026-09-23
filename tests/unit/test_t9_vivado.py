"""T9: Vivado report_power parsing. Every value is labelled an ESTIMATE; each check has a twin."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness_manager.power.vivado import (
    NotAPowerReport,
    find_reports,
    load_estimates,
    parse_report,
    rm_name_from_path,
)
from tests.fakes.t9_fixtures import TIMING_REPORT, power_report

REAL_REPORT = (Path(__file__).resolve().parents[3] / "mps3-nanosoc-platform" / "fpga" / "dfx"
               / "build_mint_2026_09" / "shell_proj" / "shell_proj.runs" / "impl_1"
               / "shell_top_power_routed.rpt")


def test_parses_the_summary_supplies_and_confidence():
    est = parse_report(power_report(), rm="shell_top")
    assert (est.total_w, est.dynamic_w, est.static_w, est.junction_c) == (1.711, 0.442, 1.270, 26.9)
    assert est.confidence == "Low" and est.design_state == "routed"
    assert est.device == "xcku115-flvb1760-1-c" and est.date == "Tue Sep 15 15:08:16 2026"
    assert est.rails["Vccint"] == (0.950, 0.678) and est.rails["Vccbram"] == (0.950, 0.045)


def test_a_timing_report_is_not_a_power_report():
    with pytest.raises(NotAPowerReport):
        parse_report(TIMING_REPORT)


def test_every_reading_is_labelled_an_estimate():
    rows = parse_report(power_report(), rm="nanosoc").readings()
    assert [r.name for r in rows] == ["onchip_power_estimate", "onchip_dynamic_estimate",
                                      "onchip_static_estimate", "junction_temp_estimate"]
    assert all(r.source == "estimate:vivado nanosoc" for r in rows)
    assert all(r.reason.startswith("ESTIMATE, not a measurement") for r in rows)
    assert all("vectorless" in r.reason and "confidence Low" in r.reason for r in rows)
    assert [r.unit for r in rows] == ["W", "W", "W", "degC"]


def test_a_missing_summary_value_is_unavailable_not_zero():
    rows = parse_report(power_report(dynamic="NA"), rm="x").readings()
    dyn = next(r for r in rows if r.name == "onchip_dynamic_estimate")
    assert dyn.value is None and "no Dynamic value" in dyn.reason
    # Negative twin: the other values are still there.
    assert next(r for r in rows if r.name == "onchip_power_estimate").value == 1.711


def test_activity_file_changes_the_caveat():
    text = power_report().replace("| Simulation Activity File | ---          |",
                                  "| Simulation Activity File | top.saif     |")
    assert "activity from top.saif" in parse_report(text, rm="x").caveat
    assert "vectorless" in parse_report(power_report(), rm="x").caveat


@pytest.mark.parametrize("name,rm", [
    ("power_rm_nanosoc.rpt", "nanosoc"),
    ("config_rm_led_power_routed.rpt", "led"),
    ("shell_top_power_routed.rpt", "shell_top"),
    ("power_rm_nanosoc_upy.rpt", "nanosoc_upy"),
])
def test_design_name_from_the_file_name(name, rm):
    assert rm_name_from_path(Path(name)) == rm


def test_find_reports_by_content_and_keeps_the_newest(tmp_path: Path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "power_rm_nanosoc.rpt").write_text(power_report(total="2.100"))
    (tmp_path / "a" / "b" / "config_rm_led_power_routed.rpt").write_text(power_report(total="1.800"))
    (tmp_path / "timing_rm_nanosoc.rpt").write_text(TIMING_REPORT)        # not power: ignored
    (tmp_path / "util_rm_power.txt").write_text(power_report())            # not *.rpt: ignored
    older = tmp_path / "a" / "power_rm_nanosoc.rpt"
    older.write_text(power_report(total="9.999"))
    os.utime(older, (1_000_000, 1_000_000))
    found = find_reports(tmp_path)
    assert sorted(found) == ["led", "nanosoc"]
    ests = load_estimates(tmp_path)
    assert ests["nanosoc"].total_w == 2.100          # the newer of the two nanosoc reports
    assert ests["led"].total_w == 1.800
    assert ests["nanosoc"].readings()[0].source == "estimate:vivado nanosoc"


def test_find_reports_in_a_missing_directory_is_empty(tmp_path: Path):
    assert find_reports(tmp_path / "nope") == {} and load_estimates(tmp_path / "nope") == {}


@pytest.mark.skipif(not REAL_REPORT.is_file(), reason="platform repo mint build not present")
def test_the_real_fielded_shell_report_parses():
    est = parse_report(REAL_REPORT.read_text(), rm=rm_name_from_path(REAL_REPORT), path=REAL_REPORT)
    assert est.rm == "shell_top" and est.total_w == 1.711 and est.confidence == "Low"
    assert len(est.rails) > 10
