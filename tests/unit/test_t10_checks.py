"""T10: every XDC check fires on a crafted bad design, and each has a negative twin.

A twin is the smallest change that makes the same design legal: the check must stay
quiet on it, so a check that fires on everything cannot pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import ExitCode, RefusedError, UsageError
from harness_manager.services import xdc
from harness_manager.services.xdc.design import from_doc
from harness_manager_mps3 import pins

MODEL = xdc.PinModel(pins.load_model(), pack="mps3")


def board(*ports: dict[str, Any]) -> xdc.Kit:
    return xdc.board_kit(MODEL, from_doc({"kind": "board", "name": "t", "ports": list(ports)}))


def rm(**doc: Any) -> xdc.Kit:
    return xdc.rm_kit(MODEL, from_doc({"kind": "rm", "name": "t", **doc}))


def codes(kit: xdc.Kit) -> list[str]:
    return [f.code for f in kit.errors]


def boundary_ports() -> list[dict[str, Any]]:
    return [{"name": s.name, "dir": s.rm_dir, "width": s.width} for s in MODEL.boundary()]


# --- board kit ----------------------------------------------------------------------------------


def test_pin_conflict_two_ports_on_one_pin():
    bad = board({"port": "a", "net": "USER_nLED[0]", "dir": "out"},
                {"port": "b", "net": "USER_nLED[0]", "dir": "out"})
    assert codes(bad) == ["pin_conflict"] and "already a" in bad.errors[0].reason
    twin = board({"port": "a", "net": "USER_nLED[0]", "dir": "out"},
                 {"port": "b", "net": "USER_nLED[1]", "dir": "out"})
    assert twin.ok


def test_pin_conflict_a_raw_pin_that_is_already_a_named_net():
    bad = board({"port": "a", "net": "USER_nLED[0]", "dir": "out"},
                {"port": "b", "pin": "AU32", "iostandard": "LVCMOS18", "dir": "out"})
    assert codes(bad) == ["pin_conflict"]


def test_pin_conflict_a_reserved_net():
    bad = board({"port": "tx", "net": "UART_TX_F[0]", "dir": "out"})
    assert codes(bad) == ["pin_conflict"] and "MCC's own console" in bad.errors[0].reason
    twin = board({"port": "tx", "net": "UART_TX_F[3]", "dir": "out"})
    assert twin.ok


def test_bank_voltage_a_1v8_standard_in_a_3v3_bank():
    bad = board({"port": "g", "net": "SH0_IO[0]", "dir": "out", "iostandard": "LVCMOS18"})
    assert codes(bad) == ["bank_voltage"]
    assert "bank 84 is fixed at 3.3 V" in bad.errors[0].reason
    twin = board({"port": "g", "net": "SH0_IO[0]", "dir": "out", "iostandard": "LVCMOS33"})
    assert twin.ok


def test_bank_voltage_a_bank_whose_vcco_the_model_does_not_know_is_an_error_not_a_pass():
    pin45 = next(p for p, v in MODEL.package_pins.items() if v["bank"] == "45")
    bad = board({"port": "x", "pin": pin45, "dir": "out", "iostandard": "LVCMOS18"})
    assert codes(bad) == ["bank_voltage"] and "not in the model" in bad.errors[0].reason
    pin44 = next(p for p, v in MODEL.package_pins.items()
                 if v["bank"] == "44" and p not in MODEL.by_pin)
    twin = board({"port": "x", "pin": pin44, "dir": "out", "iostandard": "LVCMOS18"})
    assert twin.ok


def test_bank_voltage_an_unknown_standard_or_none_at_all():
    assert codes(board({"port": "g", "net": "SH0_IO[0]", "dir": "out",
                        "iostandard": "LVCMOS99"})) == ["bank_voltage"]
    none = board({"port": "irq", "net": "PB_IRQ", "dir": "in"})     # the pinmap gives it none
    assert codes(none) == ["bank_voltage"] and "no IO standard" in none.errors[0].reason
    assert board({"port": "irq", "net": "PB_IRQ", "dir": "in", "iostandard": "LVCMOS18"}).ok


def test_direction_driving_a_net_the_board_drives():
    bad = board({"port": "sw", "net": "USER_SW[0]", "dir": "out"})
    assert codes(bad) == ["direction"] and "fight" in bad.errors[0].reason
    assert board({"port": "sw", "net": "USER_SW[0]", "dir": "in"}).ok


def test_direction_reading_a_net_nothing_on_the_board_drives():
    bad = board({"port": "led", "net": "USER_nLED[0]", "dir": "in"})
    assert codes(bad) == ["direction"]
    assert board({"port": "led", "net": "USER_nLED[0]", "dir": "out"}).ok


def test_clock_capable_a_clock_on_a_non_gc_pin():
    bad = board({"port": "clk", "net": "USER_SW[0]", "dir": "in", "clock_mhz": 10})
    assert codes(bad) == ["clock_capable"] and "not a global-clock (GC) pin" in bad.errors[0].reason
    twin = board({"port": "clk", "net": "OSCCLK[1]", "dir": "in", "clock_mhz": 50})
    assert twin.ok


def test_clock_period_disagreeing_with_the_oscillator_the_mcc_programs():
    bad = board({"port": "clk", "net": "OSCCLK[1]", "dir": "in", "clock_mhz": 100})
    assert codes(bad) == ["clock_period"] and "OSC1" in bad.errors[0].reason
    assert board({"port": "clk", "net": "OSCCLK[1]", "dir": "in", "clock_mhz": 50}).ok


def test_missing_pin_a_net_the_board_does_not_have():
    bad = board({"port": "led", "net": "USER_LED[0]", "dir": "out"})
    assert codes(bad) == ["missing_pin"] and "USER_nLED[0]" in bad.errors[0].hint
    raw = board({"port": "x", "pin": "ZZ99", "dir": "out", "iostandard": "LVCMOS18"})
    assert codes(raw) == ["missing_pin"]
    nothing = board({"port": "x", "dir": "out"})
    assert codes(nothing) == ["missing_pin"]
    assert board({"port": "led", "net": "USER_nLED[0]", "dir": "out"}).ok


def test_width_a_bus_onto_a_different_width():
    bad = board({"port": "led[3:0]", "net": "USER_nLED[7:0]", "dir": "out"})
    assert codes(bad) == ["width"]
    assert board({"port": "led[7:0]", "net": "USER_nLED[7:0]", "dir": "out"}).ok


def test_a_board_caution_travels_as_a_note_not_an_error():
    k = board({"port": "g", "net": "SH0_IO[16]", "dir": "out"})
    assert k.ok and [f.code for f in k.findings] == ["caution"]


# --- RM kit --------------------------------------------------------------------------------------


def test_rm_missing_pin_a_boundary_port_the_rm_lacks():
    ports = [p for p in boundary_ports() if p["name"] != "jtag_tdo"]
    bad = rm(use={"clkrst": {}}, ports=ports)
    assert codes(bad) == ["missing_pin"] and bad.errors[0].subject == "jtag_tdo"
    assert rm(use={"clkrst": {}}, ports=boundary_ports()).ok


def test_rm_missing_pin_a_port_the_boundary_does_not_have():
    bad = rm(use={"clkrst": {}}, ports=boundary_ports() + [{"name": "swd_clk", "dir": "in"}])
    assert codes(bad) == ["missing_pin"] and "re-mint" in bad.errors[0].reason


def test_rm_missing_pin_an_unknown_group_or_clock_port():
    assert codes(rm(use={"clkrst": {}, "pcie": {}})) == ["missing_pin"]
    assert codes(rm(use={"clkrst": {}}, clocks=["core_clk"])) == ["missing_pin"]
    assert codes(rm(use={"eth": {"timed": ["phy_rmii_rxd", "tx_clk"]}})) == ["missing_pin"]


def test_rm_direction_mismatch():
    ports = [dict(p, dir="in") if p["name"] == "jtag_tdo" else p for p in boundary_ports()]
    bad = rm(use={"clkrst": {}}, ports=ports)
    assert codes(bad) == ["direction"] and "RM out" in bad.errors[0].reason


def test_rm_width_mismatch():
    ports = [dict(p, width=16) if p["name"] == "rm_id" else p for p in boundary_ports()]
    assert codes(rm(use={"clkrst": {}}, ports=ports)) == ["width"]


def test_rm_clock_capable_a_clock_on_a_data_signal():
    bad = rm(use={"clkrst": {}, "uart": {}}, clocks=["uart_rx_tvalid"])
    assert codes(bad) == ["clock_capable"] and "not a clock the shell drives" in bad.errors[0].reason
    assert rm(use={"clkrst": {}, "jtag": {}}, clocks=["jtag_tck"]).ok


def test_rm_clock_period_disagreeing_with_the_static():
    bad = rm(use={"clkrst": {}}, clocks=[{"port": "dut_clk", "period_ns": 10.0}])
    assert codes(bad) == ["clock_period"]
    assert rm(use={"clkrst": {}}, clocks=[{"port": "dut_clk", "period_ns": 20.0}]).ok


def test_rm_pin_conflict_the_partition_has_no_io_sites():
    bad = rm(use={"clkrst": {}}, pins=[{"port": "led0", "pin": "AU32"}])
    assert codes(bad) == ["pin_conflict"]
    assert "0 IO sites" in bad.errors[0].reason and "USER_nLED" in bad.errors[0].reason
    assert rm(use={"clkrst": {}}, pins=[]).ok


def test_rm_static_id_the_model_does_not_describe():
    bad = rm(use={"clkrst": {}}, static_id="0x3F1A560F")
    assert codes(bad) == ["static_id"]
    assert rm(use={"clkrst": {}}, static_id="0x72bb0a36").ok


def test_rm_the_kits_own_skeleton_passes_the_port_check(tmp_path: Path):
    kit = xdc.export("mps3", "rm-kit", "nanosoc")
    sv = tmp_path / "rm_nanosoc.sv"
    sv.write_text(kit.files["nanosoc_wrapper_skeleton.sv"])
    design = tmp_path / "d.json"
    design.write_text(json.dumps({"kind": "rm", "name": "mine", "wrapper": sv.name,
                                  "use": {"clkrst": {}}}))
    assert xdc.export("mps3", "rm-kit", str(design)).ok
    # twin: the same wrapper with one port removed
    sv.write_text(kit.files["nanosoc_wrapper_skeleton.sv"].replace(
        "  output logic              swo,\n", ""))
    bad = xdc.export("mps3", "rm-kit", str(design))
    assert codes(bad) == ["missing_pin"] and bad.errors[0].subject == "swo"


# --- refusal and the gate ------------------------------------------------------------------------


def test_a_failed_check_refuses_the_export_with_every_finding(tmp_path: Path):
    bad = board({"port": "sw", "net": "USER_SW[0]", "dir": "out"},
                {"port": "g", "net": "SH0_IO[0]", "dir": "out", "iostandard": "LVCMOS18"})
    with pytest.raises(RefusedError) as ei:
        xdc.write_kit(bad, tmp_path / "out")
    assert ei.value.code == ExitCode.REFUSED
    assert [c["code"] for c in ei.value.data["checks"]] == ["direction", "bank_voltage"]
    assert not (tmp_path / "out").exists()
    good = board({"port": "sw", "net": "USER_SW[0]", "dir": "in"})
    written = xdc.write_kit(good, tmp_path / "out")
    assert {p.name for p in written} == {"t_pins.xdc", "t_io.xdc", "t_timing.xdc", "manifest.json"}


@pytest.mark.parametrize("name", sorted(pins.design_files()))
def test_every_builtin_design_passes_every_check(name):
    kit_name = "board" if json.loads(pins.design_files()[name].read_text())["kind"] == "board" \
        else "rm-kit"
    kit = xdc.export("mps3", kit_name, name)
    assert kit.ok, [f.line() for f in kit.errors]
    assert not [f for f in kit.findings if f.code == "syntax"]


def test_a_design_of_the_wrong_kind_or_shape_is_a_usage_error(tmp_path: Path):
    with pytest.raises(UsageError):
        xdc.export("mps3", "board", "nanosoc")
    with pytest.raises(UsageError):
        xdc.export("mps3", "rm-kit", {"kind": "fpga", "name": "x"})
    with pytest.raises(UsageError):
        xdc.export("mps3", "nope", "blinky")
    with pytest.raises(Exception, match="no design named"):
        xdc.export("mps3", "board", "no_such_design")
