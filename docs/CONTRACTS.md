# Contracts

These are the interfaces every team codes against. They are owned by the lead and frozen for the length of a wave.

## Frozen now (Wave 0)

| Module | What it fixes |
|---|---|
| `socharness.core.errors` | The `ExitCode` table (append-only; the HAPS helper codes) and the `HarnessError` hierarchy. Every failure uses one of these. |
| `socharness.core.model` | `LinkKind`, `Link`, `Check` (three-state), `Reading` (value or `unavailable(reason)`, with a source), `BoardIdentity`, `Health`, `Candidate`, `BoardInfo`. |
| `socharness.core.capabilities` | The shared capability names; `Route`/`via` (links + harness features); `CapabilitySpec`; `negotiate()`. |
| `socharness.core.events` | `Event(topic, board_id, data)` and `EventBus` (`*` and `prefix.*` subscriptions; handler failures are isolated). |
| `socharness.core.pack` | `BoardPack` (`capability_specs`, `probe`, `open`); `BoardSession` (`identity`, `health`, optional adapters); the adapter protocols (`Deploy`, `Console`, `Debug`, `Reset`, `Clock`, `Telemetry`); `ProbeHints`. |
| `socharness.core.registry` | Packs are loaded from the `socharness.boards` entry-point group. |
| `socharness.core.session` | `SessionLock`: one process owns a board; the holder is named; stale locks are taken over; only your own lock is released. |
| `socharness.core.pack` (adapters) | `DeployAdapter`, `ConsoleAdapter`, `DebugAdapter`, `ResetAdapter`, `ClockAdapter`, `TelemetryAdapter`, `ControllerAdapter`, `StorageAdapter`; value types `OverlayRef`, `PreflightItem`, `DeployResult`, `BackupRecord`, `Progress`. |
| `socharness.core.services` | The service protocols the front-ends consume: `Engine`, `ContentStore`, `DeployService`, `ConsoleBroker`/`ConsoleStream`, `DebugService`/`DebugStatus`, `TelemetryService`, `EngineConfig`. |
| `socharness.core.transport` | `SerialPort` (the subset of `serial.Serial` used); `open_serial(url)` with the `serial://` (T3 registers it) and `fake://` (tests) schemes. |
| `socharness_board_mps3.pack` (lead-owned) | Adapter **hooks**: teams implement factories in their own modules (`deploy.make_deploy_adapter`, `mcc.make_controller_adapter`, `sd.make_storage_adapter`, `usb.probe_usb`, `usb.serial_console_endpoints`, `openocd.make_debug_adapter`, `telemetry.make_telemetry_adapter`), and `pack.py` wires them in when they exist. |

### Event topics (append-only)

| Topic | Meaning |
|---|---|
| `board.found`, `board.lost`, `board.identity` | discovery and identity changes |
| `session.opened`, `session.closed` | session lifecycle |
| `deploy.started`, `deploy.progress` `{phase, bytes, total}`, `deploy.done` `{rm_id, verified}`, `deploy.failed` `{reason}` | partition programming |
| `console.line` `{name, text}`, `console.state` `{name, state}` | consoles |
| `debug.state` `{state, ports}` | debug sessions |
| `controller.reboot` `{phase: sent\|down\|up}` | board reboot |
| `storage.progress` `{op, bytes, total}` | SD backup/install/restore |
| `update.available`, `update.progress`, `update.done` | updates |

## Wave 1 implementations (who implements which frozen protocol)

| Protocol | Owner | Module |
|---|---|---|
| `Engine`, `ContentStore`, `TelemetryService` | T1 | `socharness/engine.py`, `socharness/services/{store,telemetry}.py` |
| `DeployService` + the MPS3 `DeployAdapter` | T2 | `socharness/services/deploy.py`, `socharness_board_mps3/{deploy,overlays}.py` |
| MPS3 `ControllerAdapter`, `StorageAdapter`, USB probe, `serial://` opener | T3 | `socharness_board_mps3/{mcc,sd,usb}.py`, `socharness/transports/direct.py` |
| `ConsoleBroker`, `DebugService` + the MPS3 `DebugAdapter` | T4 | `socharness/services/{console,debug}.py`, `socharness_board_mps3/openocd.py` |
| CLI over `Engine` | T5 | `socharness/cli/**` |
| GUI over `Engine` | T6 | `socharness/gui/**` |

## Changing a contract

1. Write a **contract change request** in your hand-back:
   - the module;
   - the change as a minimal diff;
   - why it is needed;
   - who else it affects.
2. The lead applies agreed changes to `main` at the next merge window, then updates this file.
3. A change that blocks a team is applied mid-wave. Every team is told, and rebases.
4. Append-only lists (exit codes, event topics, capability names, TSV columns) may only grow.
