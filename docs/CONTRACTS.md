# Contracts

These are the interfaces every team codes against. They are owned by the lead and frozen for the length of a wave.

## Frozen now (Wave 0)

| Module | What it fixes |
|---|---|
| `harness_manager.core.errors` | The `ExitCode` table (append-only; the HAPS helper codes) and the `HarnessError` hierarchy. Every failure uses one of these. |
| `harness_manager.core.model` | `LinkKind`, `Link`, `Check` (three-state), `Reading` (value or `unavailable(reason)`, with a source), `BoardIdentity`, `Health`, `Candidate`, `BoardInfo`. |
| `harness_manager.core.capabilities` | The shared capability names; `Route`/`via` (links + harness features); `CapabilitySpec`; `negotiate()`. |
| `harness_manager.core.events` | `Event(topic, board_id, data)` and `EventBus` (`*` and `prefix.*` subscriptions; handler failures are isolated). |
| `harness_manager.core.pack` | `BoardPack` (`capability_specs`, `probe`, `open`); `BoardSession` (`identity`, `health`, optional adapters); the adapter protocols (`Deploy`, `Console`, `Debug`, `Reset`, `Clock`, `Telemetry`); `ProbeHints`. |
| `harness_manager.core.registry` | Packs are loaded from the `harness_manager.boards` entry-point group. |
| `harness_manager.core.session` | `SessionLock`: one process owns a board; the holder is named; stale locks are taken over; only your own lock is released. |
| `harness_manager.core.pack` (adapters) | `DeployAdapter`, `ConsoleAdapter`, `DebugAdapter`, `ResetAdapter`, `ClockAdapter`, `TelemetryAdapter`, `ControllerAdapter`, `StorageAdapter`, `PowerAdapter` (T9: `read`, `cycle_reason`, `power_cycle`); value types `OverlayRef`, `PreflightItem`, `DeployResult`, `BackupRecord`, `Progress`. |
| `harness_manager.core.services` | The service protocols the front-ends consume: `Engine`, `ContentStore`, `DeployService`, `ConsoleBroker`/`ConsoleStream`, `DebugService`/`DebugStatus`, `TelemetryService`, `EngineConfig`. |
| `harness_manager.core.transport` | `SerialPort` (the subset of `serial.Serial` used); `open_serial(url)` with the `serial://` (T3 registers it) and `fake://` (tests) schemes. |
| `harness_manager_mps3.pack` (lead-owned) | Adapter **hooks**: teams implement factories in their own modules (`deploy.make_deploy_adapter`, `mcc.make_controller_adapter`, `sd.make_storage_adapter`, `usb.probe_usb`, `usb.serial_console_endpoints`, `openocd.make_debug_adapter`, `telemetry.make_telemetry_adapter`, `telemetry.make_power_adapter`, `telemetry.with_config_links`), and `pack.py` wires them in when they exist. |

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
| `power.cycle` `{phase: off\|on, off_s, device}` | a cold power cycle through a metered outlet (T9, L4) |
| `console.pty` `{name, path, device, clients, open}` | a console's PTY for `screen` opened, closed, or a client attached or left (L2) |
| `lease.state` `{target, state, holder, expires_at}` | a hub lease was held, queued, released, expired or lost (L1) |
| `tunnel.state` `{via, host, state: up\|down\|starting, ports, forwards, restarts, pid, detail}` | the SSH tunnel to a board behind a hub changed state (L1) |
| `job.started`, `job.progress`, `job.done`, `job.failed`, `events.dropped` | harness-manager-daemon jobs and back-pressure (docs/API.md) |

## Wave 1 implementations (who implements which frozen protocol)

| Protocol | Owner | Module |
|---|---|---|
| `Engine`, `ContentStore`, `TelemetryService` | T1 | `harness_manager/engine.py`, `harness_manager/services/{store,telemetry}.py` |
| `DeployService` + the MPS3 `DeployAdapter` | T2 | `harness_manager/services/deploy.py`, `harness_manager_mps3/{deploy,overlays}.py` |
| MPS3 `ControllerAdapter`, `StorageAdapter`, USB probe, `serial://` opener | T3 | `harness_manager_mps3/{mcc,sd,usb}.py`, `harness_manager/transports/direct.py` |
| `ConsoleBroker`, `DebugService` + the MPS3 `DebugAdapter` | T4 | `harness_manager/services/{console,debug}.py`, `harness_manager_mps3/openocd.py` |
| CLI over `Engine` | T5 | `harness_manager/cli/**` |
| GUI over `Engine` | T6 | `harness-manager/gui/**` (retired at the T14 merge; the web UI replaced it, `DemoEngine` moved to `harness_manager/demo.py`) |

## Conventions the front-ends rely on

- **Service stubs carry `reason`.** When the engine cannot provide a service (the module is not installed, or failed to load), the service attribute is a stub whose every call raises `UnavailableError`, and it has a `reason: str` attribute. **A real service never defines `reason`.** Front-ends read `getattr(engine.<service>, "reason", None)` to grey a panel with its reason without calling the stub.
- **`Candidate.identity`** is what the board said while it was being probed, or `None`. It lets the selection list show shell, design and harness before a board is opened. `Engine.info()` still needs an open board.
- **`power_cycle` is narrowed by the adapter.** The capability needs a `SMART_POWER` link, and `Engine.info()` also moves it to `unavailable` with `session.power.cycle_reason` when the device cannot cycle (a meter-only INA260).
- **Telemetry reads can block.** A SYSMON read over JTAG takes about 3 s (xsdb). harness-manager-daemon and the web UI poll telemetry off the request thread, never on it.
- **`engine.update` is optional.** The in-process `Engine` has it (T7's `UpdateService`); a `RemoteEngine` does not. Read it with `getattr(engine, "update", None)`. The CLI's `update` verb always runs in-process, so a daemon that holds the board refuses it by name (HELD).
- **`ControllerAdapter.reboot` returns a dict**: `summary`, `down_after_s`, `up_after_s`, `down_evidence`, `up_evidence`, `shell_id_before`, `shell_id_after`, `fpga_configured`. It returns only once the controller console is back at its prompt.
- **`ConsoleAdapter.console_write_pace_s()` is optional** and returns `{console name: seconds per byte}`. The console broker then sends that console's input one byte at a time from a writer thread, so `write` never blocks. The MPS3 pack paces `uart0`/`uart1` at 20 ms (the nanoSoC UART has no receive FIFO); `Mps3Pack(console_pace_s=0)` turns it off.
- **Console rates (L2).**
  - `ConsoleAdapter.console_baud_info() -> {name: {kind, baud, source, settable, reason, choices, mode?, share?}}` and `console_set_baud(name, baud)` are optional. Host `serial://` ports may be left out: the broker owns their rate.
  - `ConsoleBroker` gains the optional `pty`, `pty_info`, `close_pty`, `baud`, `set_baud` and `consoles`. A broker without them gets the daemon's fallback PTYs.
  - `console.state` carries `baud` when the rate changes.
- **Reaching a board through a hub (L1).**
  - `ProbeHints.via` and `candidate_for_host(spec, via="")` take `"ssh:HOST"`. A pack that cannot tunnel raises `UsageError` for a non-empty `via`.
  - Sessions behind a hub may carry `session.hub` (`host`, `target`, `client`) and `session.reach` (`status()`, `close()`). A pack may offer `hub_for(candidate)`.
  - Engines that predate `via` still work: callers pass it only when it is set.
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
