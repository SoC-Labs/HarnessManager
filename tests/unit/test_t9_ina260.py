"""T9: the INA260 over an MCP2221A, with a fake I2C bus. Each check has a negative twin."""

from __future__ import annotations

import sys

import pytest

from harness_manager.core.errors import UnavailableError
from harness_manager.power.adapter import make_driver
from harness_manager.power.config import PowerConfig
from harness_manager.power.ina260 import NEEDS_PACKAGE, Ina260Mcp2221

CFG = PowerConfig(kind="ina260_mcp2221", i2c_address=0x40, device=0, cycle=False)


class FakeBus:
    def __init__(self, regs: dict[int, int], *, fail: Exception | None = None) -> None:
        self.regs = regs
        self.fail = fail
        self.reads: list[tuple[int, int, int]] = []

    def read_register(self, address: int, register: int, length: int) -> bytes:
        self.reads.append((address, register, length))
        if self.fail is not None:
            raise self.fail
        return self.regs[register].to_bytes(2, "big")


GOOD = {0xFE: 0x5449, 0x01: 1600, 0x02: 9600, 0x03: 2400}   # 2.000 A, 12.000 V, 24.00 W


def test_reads_dc_watts_volts_amps():
    bus = FakeBus(dict(GOOD))
    rows = Ina260Mcp2221(CFG, bus_factory=lambda: bus).read()
    assert [(r.name, r.value, r.unit) for r in rows] == [
        ("board_power", 24.0, "W"), ("supply_voltage", 12.0, "V"), ("supply_current", 2.0, "A")]
    assert all("DC at the board's 12 V input" in r.reason for r in rows)
    assert all(r.source == "ina260 at 0x40 on MCP2221A #0" for r in rows)
    assert {reg for _a, reg, _n in bus.reads} == {0xFE, 0x01, 0x02, 0x03}


def test_manufacturer_id_is_checked_once():
    bus = FakeBus(dict(GOOD))
    d = Ina260Mcp2221(CFG, bus_factory=lambda: bus)
    d.read()
    d.read()
    assert [reg for _a, reg, _n in bus.reads].count(0xFE) == 1


def test_a_device_that_is_not_an_ina260_is_refused():
    rows = Ina260Mcp2221(CFG, bus_factory=lambda: FakeBus({**GOOD, 0xFE: 0x1234})).read()
    assert all(r.value is None for r in rows)
    assert "not an INA260 (manufacturer id 0x1234" in rows[0].reason


def test_negative_current_is_flagged():
    rows = Ina260Mcp2221(CFG, bus_factory=lambda: FakeBus({**GOOD, 0x01: 0x10000 - 800})).read()
    assert rows[2].value == -1.0 and "wired the other way round" in rows[2].reason
    assert "wired" not in Ina260Mcp2221(CFG, bus_factory=lambda: FakeBus(dict(GOOD))).read()[2].reason


def test_a_bus_fault_is_a_reason_and_the_bridge_is_reopened():
    opened = []

    def factory():
        opened.append(1)
        return FakeBus(dict(GOOD), fail=OSError("USB device gone") if len(opened) == 1 else None)

    d = Ina260Mcp2221(CFG, bus_factory=factory)
    bad = d.read()
    assert all(r.value is None and "USB device gone" in r.reason for r in bad)
    good = d.read()
    assert good[0].value == 24.0 and len(opened) == 2


def test_without_easymcp2221_the_reason_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "EasyMCP2221", None)     # import -> ModuleNotFoundError
    rows = make_driver(CFG).read()
    assert all(r.value is None and r.reason == NEEDS_PACKAGE for r in rows)


def test_an_ina260_cannot_power_cycle():
    d = Ina260Mcp2221(CFG, bus_factory=lambda: FakeBus(dict(GOOD)))
    assert "only measures" in d.cycle_reason
    with pytest.raises(UnavailableError, match="only measures"):
        d.power_cycle(5.0)
