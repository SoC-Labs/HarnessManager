"""The board-agnostic power adapter: one configured meter, as a session sees it.

Proposed contract (T9 CCR 1): ``BoardSession.power: PowerAdapter | None`` with

- ``read() -> list[Reading]``: ``board_power`` (W), ``supply_voltage`` (V),
  ``supply_current`` (A). Never raises; a fault is ``Reading.unavailable``.
- ``cycle_reason: str``: why ``power_cycle`` cannot work ("" when it can).
- ``power_cycle(off_s=5.0, *, wait=True, progress=None) -> dict``: a COLD power
  cycle through the outlet, timed by the device itself; returns the evidence.
"""

from __future__ import annotations

from typing import Any, Protocol

from harness_manager.core.errors import UsageError
from harness_manager.core.model import Reading
from harness_manager.core.pack import Progress

from .base import DEFAULT_OFF_S, unavailable_readings
from .config import BoardConfig, PowerConfig
from .ina260 import Ina260Mcp2221
from .plugs import Netio, ShellyGen2, Tasmota

DRIVERS: dict[str, type] = {
    "shelly_gen2": ShellyGen2,
    "tasmota": Tasmota,
    "netio": Netio,
    "ina260_mcp2221": Ina260Mcp2221,
}
NO_METER = "the board has no power meter configured (add a power table in boards.toml)"


class PowerDriver(Protocol):
    kind: str
    label: str

    def read(self) -> list[Reading]: ...

    @property
    def cycle_reason(self) -> str: ...

    def power_cycle(self, off_s: float = DEFAULT_OFF_S, *, wait: bool = True,
                    progress: Progress | None = None) -> dict: ...


def make_driver(cfg: PowerConfig, **kwargs: Any) -> PowerDriver:
    try:
        cls = DRIVERS[cfg.kind]
    except KeyError:
        raise UsageError(f"no power driver for kind {cfg.kind!r}",
                         hint=f"kinds: {', '.join(DRIVERS)}") from None
    return cls(cfg, **kwargs)


class _Misconfigured:
    """Stands in for a driver when the power table cannot be used: says why, every time."""

    kind = "misconfigured"

    def __init__(self, reason: str) -> None:
        self.label = "power meter (boards.toml)"
        self.cycle_reason = reason
        self._reason = reason

    def read(self) -> list[Reading]:
        return unavailable_readings(self._reason, self.label)

    def power_cycle(self, off_s: float = DEFAULT_OFF_S, *, wait: bool = True,
                    progress: Progress | None = None) -> dict:
        raise UsageError(self._reason, hint="fix the power table in boards.toml")


class PowerAdapter:
    def __init__(self, driver: PowerDriver, *, board_id: str = "") -> None:
        self.driver = driver
        self.board_id = board_id

    @property
    def label(self) -> str:
        return self.driver.label

    def read(self) -> list[Reading]:
        try:
            return list(self.driver.read())
        except Exception as exc:  # noqa: BLE001 - a meter fault is a reading, never a crash
            return unavailable_readings(f"{type(exc).__name__}: {exc}", self.driver.label)

    @property
    def cycle_reason(self) -> str:
        return self.driver.cycle_reason

    def power_cycle(self, off_s: float = DEFAULT_OFF_S, *, wait: bool = True,
                    progress: Progress | None = None) -> dict:
        evidence = self.driver.power_cycle(off_s, wait=wait, progress=progress)
        return {"board_id": self.board_id, **evidence}


def make_power_adapter(board: BoardConfig | None, **driver_kwargs: Any) -> PowerAdapter | None:
    """The adapter for a board's ``power`` table; None when it has none."""
    if board is None or not board.has_power:
        return None
    if board.power is None:
        return PowerAdapter(_Misconfigured(board.power_error), board_id=board.key)
    return PowerAdapter(make_driver(board.power, **driver_kwargs), board_id=board.key)
