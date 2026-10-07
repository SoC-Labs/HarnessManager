"""SoC Labs Harness Manager.

A board manager for SoC prototyping harnesses. The Arm MPS3 is the pilot board.

Layers (see docs/ARCHITECTURE.md):

- ``harness_manager.core``: board-agnostic contracts, including models, errors and
  exit codes, capabilities, events, the board-pack interface, the registry and
  the session lock. Every team codes against these.
- ``harness_manager.transports``: how the engine reaches a board. There are two:
  ``direct`` (sockets, serial ports, mounted volumes, local processes) and
  ``hub`` (the fpgahub API).
- ``harness_manager.services``: board-agnostic managers (console, debug, deploy,
  telemetry, update) built on board-pack adapters.
- ``harness_manager.cli`` / ``harness_manager.web`` (served by ``harness_manager.daemon``):
  front-ends. They render what the engine reports and never talk to a board
  directly. ``harness_manager.demo`` is a scripted engine for demos and UI tests.
- ``harness_manager_mps3``: the MPS3 board pack.
"""

__version__ = "1.0.1"
