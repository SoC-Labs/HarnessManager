"""SoC Labs Harness Manager.

A board manager for SoC prototyping harnesses. The Arm MPS3 is the pilot board.

Layers (see docs/ARCHITECTURE.md):

- ``socharness.core``: board-agnostic contracts, including models, errors and
  exit codes, capabilities, events, the board-pack interface, the registry and
  the session lock. Every team codes against these.
- ``socharness.transports``: how the engine reaches a board. There are two:
  ``direct`` (sockets, serial ports, mounted volumes, local processes) and
  ``hub`` (the fpgahub API).
- ``socharness.services``: board-agnostic managers (console, debug, deploy,
  telemetry, update) built on board-pack adapters.
- ``socharness.cli`` / ``socharness.gui``: front-ends. They render what the
  engine reports and never talk to a board directly.
- ``socharness_board_mps3``: the MPS3 board pack.
"""

__version__ = "0.0.1"
