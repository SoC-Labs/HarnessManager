"""T10: the light XDC syntax check and the HDL/XDC readers, each with a negative twin."""

from __future__ import annotations

import pytest

from harness_manager.services import xdc
from harness_manager.services.xdc.hdl import expand, parse_ansi_ports, parse_xdc_pins, split_bit
from harness_manager.services.xdc.syntax import check_xdc
from harness_manager_mps3 import pins

GOOD = """\
# a comment
create_clock -name clk -period 20.000 -waveform {0.000 10.000} [get_ports clk]
set_property PACKAGE_PIN AK16 [get_ports clk] ;# trailing comment
set_property -dict {PACKAGE_PIN BB14 IOSTANDARD LVCMOS33} [get_ports x]
set_false_path -from [get_ports -quiet {a b[*]}]
set_clock_groups -name g -asynchronous \\
    -group [get_clocks a] \\
    -group [get_clocks -quiet {b c}]
set_property CONFIG_VOLTAGE 3.3 [current_design]
"""


def test_a_well_formed_xdc_passes():
    assert check_xdc(GOOD) == []


@pytest.mark.parametrize("bad, why", [
    ("set_false_path -from [get_ports {a b]\n", "unbalanced"),
    ("set_false_path -from [get_ports a\n", "unclosed"),
    ('set_property NOTE "open [get_ports a]\n', "unterminated quote"),
    ("if {1} { create_clock -period 1 [get_ports a] }\n", "control flow"),
    ("puts hello\n", "control flow"),
    ("create_clok -period 20 [get_ports a]\n", "unknown XDC command"),
    ("create_clock -name c [get_ports a]\n", "numeric -period"),
    ("create_clock -period 20 -waveform {0 x} [get_ports a]\n", "-waveform takes numbers"),
    ("create_clock -period 20\n", "names no source object"),
    ("set_false_path [get_ports a]\n", "needs -from"),
    ("set_clock_groups -asynchronous -group [get_clocks a]\n", "at least two"),
    ("set_property PACKAGE_PIN [get_ports a]\n", "needs a property"),
    ("set_false_path -from [get_portz a]\n", "not an object query"),
    ("set_false_path -from [get_ports a] \\\n", "line continuation"),
])
def test_each_syntax_problem_is_caught(bad, why):
    problems = check_xdc(bad)
    assert problems and why in " ".join(problems), problems


@pytest.mark.parametrize("name", sorted(pins.design_files()))
def test_every_generated_xdc_is_syntactically_valid(name):
    kind = "board" if name in ("blinky", "shield_gpio", "harness_shell") else "rm-kit"
    kit = xdc.export("mps3", kind, name)
    xdcs = {n: t for n, t in kit.files.items() if n.endswith(".xdc")}
    assert xdcs
    for fname, text in xdcs.items():
        assert check_xdc(text) == [], fname


# --- readers -------------------------------------------------------------------------------


def test_expand_and_split_bit():
    assert expand("USER_SW[2:0]") == ["USER_SW[2]", "USER_SW[1]", "USER_SW[0]"]
    assert expand("a[0:1]") == ["a[0]", "a[1]"]
    assert expand("OSCCLK1") == ["OSCCLK1"]
    assert split_bit("SH0_IO[3]") == ("SH0_IO", 3) and split_bit("x") == ("x", None)
    with pytest.raises(ValueError):
        expand("USER_SW[2:")
    with pytest.raises(ValueError):
        expand("1abc")


def test_parse_ansi_ports_reads_ranges_params_and_leading_commas():
    sv = """
module top #(parameter int NGPIO = 16) (
  input  wire        clk,   // the clock
  output logic [NGPIO-1:0] gpio_o,
  inout  wire [17:10] pd,
  input  wire a, b
`ifdef X
  , output wire extra
`endif
);
endmodule
"""
    ports = parse_ansi_ports(sv)
    assert [(p.name, p.direction, p.width) for p in ports] == [
        ("clk", "in", 1), ("gpio_o", "out", 16), ("pd", "inout", 8), ("a", "in", 1),
        ("b", "in", 1), ("extra", "out", 1)]
    assert ports[2].bits()[0] == "pd[17]"


def test_parse_ansi_ports_refuses_what_it_cannot_read():
    with pytest.raises(ValueError, match="no module"):
        parse_ansi_ports("wire x;")
    with pytest.raises(ValueError, match="range bound"):
        parse_ansi_ports("module m (input wire [W*2:0] a);\nendmodule\n")


def test_parse_xdc_pins_applies_wildcard_standards():
    text = ("set_property PACKAGE_PIN A1 [get_ports {led[0]}]\n"
            "set_property PACKAGE_PIN A2 [get_ports {led[1]}]\n"
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n"
            "set_property PULLUP true [get_ports {led[1]}]\n"
            "create_clock -period 20.000 -name clk [get_ports clk]\n")
    ports, clocks = parse_xdc_pins(text)
    assert ports["led[0]"].iostandard == ports["led[1]"].iostandard == "LVCMOS18"
    assert ports["led[1]"].props == {"PULLUP": "true"} and ports["led[0]"].props == {}
    assert clocks[0].port == "clk" and clocks[0].period_ns == 20.0
