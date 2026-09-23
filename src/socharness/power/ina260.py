"""An INA260 on the board's 12 V input, read from the host through an MCP2221A USB-I2C bridge.

This is the cheap standalone option (research agent I, §4): DC volts, amps and
watts at the board side of the power brick, from the user's own PC.

INA260 registers (TI SBOS656, all 16-bit big-endian): 0x01 current (signed,
1.25 mA/LSB), 0x02 bus voltage (1.25 mV/LSB), 0x03 power (10 mW/LSB),
0xFE manufacturer ID (0x5449, "TI"). The driver checks the manufacturer ID
once before trusting any value.

The MCP2221A is driven with the optional ``EasyMCP2221`` package
(``pip install EasyMCP2221``). Without it, readings are unavailable with that
exact instruction. The I2C access sits behind ``I2cBus`` so tests (and other
bridges) can supply their own.

An INA260 measures; it cannot switch anything, so ``power_cycle`` is unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from socharness.core.errors import UnavailableError
from socharness.core.model import Reading
from socharness.core.pack import Progress

from .base import CURRENT, POWER, VOLTAGE, unavailable_readings
from .config import PowerConfig

REG_CURRENT = 0x01
REG_BUS_VOLTAGE = 0x02
REG_POWER = 0x03
REG_MANUFACTURER = 0xFE
TI_MANUFACTURER_ID = 0x5449
CURRENT_LSB_A = 1.25e-3
VOLTAGE_LSB_V = 1.25e-3
POWER_LSB_W = 10e-3
DC_CAVEAT = "DC at the board's 12 V input (INA260)"
NEEDS_PACKAGE = "ina260_mcp2221 needs the EasyMCP2221 package: pip install EasyMCP2221"


class I2cBus(Protocol):
    def read_register(self, address: int, register: int, length: int) -> bytes: ...


class Mcp2221Bus:
    """``I2cBus`` over an MCP2221A, via EasyMCP2221 (imported only when used)."""

    def __init__(self, device: int = 0) -> None:
        try:
            import EasyMCP2221  # optional dependency
        except ModuleNotFoundError as exc:
            raise UnavailableError("ina260_mcp2221", NEEDS_PACKAGE) from exc
        try:
            self._dev = EasyMCP2221.Device(devnum=device)
        except Exception as exc:  # noqa: BLE001 - the library raises plain exceptions
            raise UnavailableError(
                "ina260_mcp2221", f"no MCP2221A #{device} found ({exc}); check the USB cable") from exc

    def read_register(self, address: int, register: int, length: int) -> bytes:
        self._dev.I2C_write(address, bytes([register]), kind="nonstop")
        return bytes(self._dev.I2C_read(address, length, kind="restart"))


class Ina260Mcp2221:
    kind = "ina260_mcp2221"

    def __init__(self, cfg: PowerConfig, *,
                 bus_factory: Callable[[], I2cBus] | None = None) -> None:
        self.cfg = cfg
        self.label = cfg.label
        self._factory = bus_factory or (lambda: Mcp2221Bus(cfg.device))
        self._bus: I2cBus | None = None
        self._checked = False

    @property
    def cycle_reason(self) -> str:
        return f"{self.label} only measures; it cannot switch the board's power"

    def power_cycle(self, off_s: float = 0.0, *, wait: bool = True,
                    progress: Progress | None = None) -> dict:
        raise UnavailableError("power_cycle", self.cycle_reason)

    def _u16(self, register: int) -> int:
        assert self._bus is not None
        data = self._bus.read_register(self.cfg.i2c_address, register, 2)
        if len(data) != 2:
            raise OSError(f"short I2C read of register 0x{register:02x} ({len(data)} bytes)")
        return int.from_bytes(data, "big")

    def read(self) -> list[Reading]:
        addr = self.cfg.i2c_address
        try:
            if self._bus is None:
                self._bus = self._factory()
            if not self._checked:
                mfg = self._u16(REG_MANUFACTURER)
                if mfg != TI_MANUFACTURER_ID:
                    return unavailable_readings(
                        f"the device at 0x{addr:02x} is not an INA260 (manufacturer id "
                        f"0x{mfg:04x}, expected 0x{TI_MANUFACTURER_ID:04x})", self.label)
                self._checked = True
            raw_i = self._u16(REG_CURRENT)
            raw_v = self._u16(REG_BUS_VOLTAGE)
            raw_p = self._u16(REG_POWER)
        except UnavailableError as exc:
            return unavailable_readings(exc.reason, self.label)
        except Exception as exc:  # noqa: BLE001 - any bus fault is a reason, never a 0
            self._bus = None      # reopen next time (the bridge may have been unplugged)
            return unavailable_readings(f"I2C read from 0x{addr:02x} failed: {exc}", self.label)
        amps = (raw_i - 0x10000 if raw_i & 0x8000 else raw_i) * CURRENT_LSB_A
        caveat = DC_CAVEAT if amps >= 0 else \
            f"{DC_CAVEAT}; current reads negative: the shunt is wired the other way round"
        return [
            Reading(POWER[0], round(raw_p * POWER_LSB_W, 3), POWER[1], self.label, reason=caveat),
            Reading(VOLTAGE[0], round(raw_v * VOLTAGE_LSB_V, 5), VOLTAGE[1], self.label, reason=caveat),
            Reading(CURRENT[0], round(amps, 5), CURRENT[1], self.label, reason=caveat),
        ]
