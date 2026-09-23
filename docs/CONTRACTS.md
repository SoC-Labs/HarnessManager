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
| `socharness.core.pack` (adapters) | `DeployAdapter`, `ConsoleAdapter`, `DebugAdapter`, `ResetAdapter`, `ClockAdapter`, `TelemetryAdapter`, `ControllerAdapter`, `StorageAdapter`, `PowerAdapter` (T9: `read`, `cycle_reason`, `power_cycle`); value types `OverlayRef`, `PreflightItem`, `DeployResult`, `BackupRecord`, `Progress`. |
| `socharness.core.services` | The service protocols the front-ends consume: `Engine`, `ContentStore`, `DeployService`, `ConsoleBroker`/`ConsoleStream`, `DebugService`/`DebugStatus`, `TelemetryService`, `EngineConfig`. |
| `socharness.core.transport` | `SerialPort` (the subset of `serial.Serial` used); `open_serial(url)` with the `serial://` (T3 registers it) and `fake://` (tests) schemes. |
| `socharness_board_mps3.pack` (lead-owned) | Adapter **hooks**: teams implement factories in their own modules (`deploy.make_deploy_adapter`, `mcc.make_controller_adapter`, `sd.make_storage_adapter`, `usb.probe_usb`, `usb.serial_console_endpoints`, `openocd.make_debug_adapter`, `telemetry.make_telemetry_adapter`, `telemetry.make_power_adapter`, `telemetry.with_config_links`), and `pack.py` wires them in when they exist. |

### Event topics (append-only)

| Topic | Meaning |
|---|---|
| `board.found`, `board.lost`, `board.identity` | discovery and identity changes |
| `session.opened`, `session.closed` | session lifecycle |
| `deploy.started`, `deploy.progress` `{phase, bytes, total}`, `deploy.done` `{rm_id, verified}`, `deploy.failed` `{reason}` | partition programming |
| `console.line` `{name, text, partial?}`, `console.state` `{name, state: connecting\|up\|down\|closed, detail, endpoint}` | consoles |
| `debug.state` `{state: down\|starting\|up\|failed, ports, pid, detail, config}` | debug sessions |
| `controller.reboot` `{phase: sent\|down\|up}` | board reboot |
| `storage.progress` `{op, bytes, total}` | SD backup/install/restore |
| `update.available`, `update.started` `{version, mode, rekey}`, `update.progress` `{phase, bytes, total}`, `update.done` `{version, result, detail}`, `update.failed` `{version, reason, phase}` | updates (T7). `result`: installed \| written-not-running \| stored \| up-to-date \| restored \| restored-not-confirmed |
| `power.cycle` `{phase: off\|on\|up, off_s, device}` | a cold power cycle through a metered outlet (T9) |
| `job.started`, `job.progress`, `job.done`, `job.failed`, `events.dropped` | socharnessd jobs and back-pressure (docs/API.md) |

## Wave 1 implementations (who implements which frozen protocol)

| Protocol | Owner | Module |
|---|---|---|
| `Engine`, `ContentStore`, `TelemetryService` | T1 | `socharness/engine.py`, `socharness/services/{store,telemetry}.py` |
| `DeployService` + the MPS3 `DeployAdapter` | T2 | `socharness/services/deploy.py`, `socharness_board_mps3/{deploy,overlays}.py` |
| MPS3 `ControllerAdapter`, `StorageAdapter`, USB probe, `serial://` opener | T3 | `socharness_board_mps3/{mcc,sd,usb}.py`, `socharness/transports/direct.py` |
| `ConsoleBroker`, `DebugService` + the MPS3 `DebugAdapter` | T4 | `socharness/services/{console,debug}.py`, `socharness_board_mps3/openocd.py` |
| CLI over `Engine` | T5 | `socharness/cli/**` |
| GUI over `Engine` | T6 | `socharness/gui/**` |

## Conventions the front-ends rely on

- **Service stubs carry `reason`.** When the engine cannot provide a service (the module is not installed, or failed to load), the service attribute is a stub whose every call raises `UnavailableError`, and it has a `reason: str` attribute. **A real service never defines `reason`.** Front-ends read `getattr(engine.<service>, "reason", None)` to grey a panel with its reason without calling the stub.
- **`Candidate.identity`** is what the board said while it was being probed, or `None`. It lets the selection list show shell, design and harness before a board is opened. `Engine.info()` still needs an open board.
- **`power_cycle` is narrowed by the adapter.** The capability needs a `SMART_POWER` link, and `Engine.info()` also moves it to `unavailable` with `session.power.cycle_reason` when the device cannot cycle (a meter-only INA260).
- **Telemetry reads can block.** A SYSMON read over JTAG takes about 3 s (xsdb). socharnessd and the web UI poll telemetry off the request thread, never on it.
- **`engine.update` is optional.** The in-process `Engine` has it (T7's `UpdateService`); a `RemoteEngine` does not. Read it with `getattr(engine, "update", None)`. The CLI's `update` verb always runs in-process, so a daemon that holds the board refuses it by name (HELD).
- **`ControllerAdapter.reboot` returns a dict**: `summary`, `down_after_s`, `up_after_s`, `down_evidence`, `up_evidence`, `shell_id_before`, `shell_id_after`, `fpga_configured`. It returns only once the controller console is back at its prompt.
- **Readings are never zero-filled.** A missing value is `Reading.unavailable(...)` with a reason and a source.

## Changing a contract

1. Write a **contract change request** in your hand-back:
   - the module;
   - the change as a minimal diff;
   - why it is needed;
   - who else it affects.
2. The lead applies agreed changes to `main` at the next merge window, then updates this file.
3. A change that blocks a team is applied mid-wave. Every team is told, and rebases.
4. Append-only lists (exit codes, event topics, capability names, TSV columns) may only grow.
