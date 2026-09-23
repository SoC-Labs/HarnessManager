"""Board power: external meters and outlets, and Vivado power estimates (Team T9).

The MPS3 has no current or power sensor and no PMBus (research agent I, 2026-09-23),
so real board power comes from an add-on configured per board in ``boards.toml``:

- ``shelly_gen2``, ``tasmota``, ``netio``: networked metered outlets (W, V, A, and a
  device-timed cold power cycle);
- ``ina260_mcp2221``: an INA260 on the 12 V input through a USB-I2C bridge (DC W, V, A).

``vivado`` turns routed ``report_power`` reports into readings labelled as ESTIMATES.
Everything here is board-agnostic: a board pack decides where it applies.
"""

from .adapter import DRIVERS, PowerAdapter, make_driver, make_power_adapter
from .base import DEFAULT_OFF_S, READINGS
from .config import (
    BoardConfig,
    BoardsConfig,
    ConfigError,
    PowerConfig,
    Secret,
    load_boards,
    power_link,
    with_links,
)

__all__ = [
    "DEFAULT_OFF_S",
    "DRIVERS",
    "READINGS",
    "BoardConfig",
    "BoardsConfig",
    "ConfigError",
    "PowerAdapter",
    "PowerConfig",
    "Secret",
    "load_boards",
    "make_driver",
    "make_power_adapter",
    "power_link",
    "with_links",
]
