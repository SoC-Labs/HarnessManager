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
| `harness_manager.core.panel` (P1, additive) | The front panel: `Hello` (+ `HelloLease`, `HelloJob`) and `hello_message`/`encode_hello` with the field caps (printable ASCII, 256 B worst case, relative seconds); `PanelState`, `PanelSession`, `PanelEvent`, `TouchHealth`, `PanelFrame`, `PanelSupport`; the optional adapter `PanelAdapter` (`session.panel`, read with `getattr`: `support`, `state`, `frame`, `hello`, `locate`; optional `offer`/`withdraw` to ride a hello on the next connection). Capabilities `front_panel`, `locate`, `presence`. |
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
| `update.applying` `{id, phase: checking\|draining\|restarting\|cancelled\|failed, from, to, waiting_on?, eta_s?, reason?}`, `update.applied` `{id, from, to, seconds}`, `update.rolled_back` `{id, from, to, phase, reason}` | the app's own apply: drain, restart on the same port and token, health check, automatic rollback (OTA-D). `update.available` from the daemon's checker adds `notes`, `notes_url`, `staged`, `source: "checker"`; `update.app.staged` `{version, channel, notes}` when OTA-C's `stage_app` staged one |
| `power.cycle` `{phase: off\|on, off_s, device}` | a cold power cycle through a metered outlet (T9, L4) |
| `console.pty` `{name, path, device, clients, open}` | a console's PTY for `screen` opened, closed, or a client attached or left (L2) |
| `lease.state` `{target, state, holder, expires_at}` | a hub lease was held, queued, released, expired or lost (L1) |
| `tunnel.state` `{via, host, state: up\|down\|starting, ports, forwards, restarts, pid, detail}` | the SSH tunnel to a board behind a hub changed state (L1) |
| `lease.wanted` `{id, by, user, host, message, deadline_at}` | someone asked for the lease this session holds (LR-B, LR-C) |
| `lease.answered` `{id, answer, minutes, message}` | the holder answered this session's request, `release` or `keep`; a keep does not end the request (LR-B, LR-C) |
| `lease.force_available` `{id}` | this session's request is unanswered past its deadline (or its keep ran out), at the head of the queue (LR-B, LR-C) |
| `lease.taken` `{by, reason, at}` | this session's lease was force-released by someone else (LR-B, LR-C) |
| `lease.left` `{}` | this session left the queue and withdrew its request (LR-B, LR-C) |
| `lease.tapped` `{id, by, at}` | someone at the board tapped the front panel's banner for the open lease request (the oldest unanswered one); `GET /lease` then has `tapped_at` on it (`incoming[]` for the holder, `request` for the requester). A notice: nothing is released, answered, forced or left (CCR PANEL-1, `LeaseService.notify_holder`) |
| `hub.event` `{type, ts, target, board, data}` | fpgahub pushed a lease or share event about the board's target or its physical board (T8; `data` is fpgahub's own payload, never a token) |
| `hub.stream` `{state: connecting\|up\|down\|refused\|closed, detail, url, reconnects}` | the board's fpgahub event stream changed state (T8) |
| `job.started`, `job.progress`, `job.done`, `job.failed`, `events.dropped` | harness-manager-daemon jobs and back-pressure (docs/API.md) |
| `panel.state` `{page, owner, pending, banner, card, count, seq, source, touch, sessions}` | what the board's front panel shows changed (P1; `source`: panel \| rebuilt) |
| `panel.tap` `{seq, kind, on, ms_ago, at, notify, request?}` | someone touched the panel; once per `seq`. `on: "request"` in the lease holder's Harness Manager carries `notify: "holder"` and the open request: a notice, never a release (P1, decision P2) |
| `panel.locate` `{state: on\|off, until, seconds, who}` | Identify started or stopped on a board (P1) |
| `kit.progress` `{static_id, phase, bytes, total}` | a DUT build kit is being fetched (KIT-CORE; `POST /kits/fetch`) |
| `kit.stored` `{static_id, source, kit_id}` | a build kit entered the kit cache: fetched or imported (KIT-CORE) |
| `xvc.state` `{state: down\|starting\|ready\|attached\|held\|swapping\|failed, open, mode, relay_port, hw_server_port, hw_server_pid, url, attached, board_slot, reach, ltx, warnings, scope, rm_id, rm_name, detail}` | a board's fabric-debug (XVC) session changed: opened, attached, dropped for a swap and re-attached, reconnected, or closed (XVC-CORE; `scope`: the reconfigurable partition's debug chain, never whole-device JTAG) |

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
- **Hub mode (T8).**
  - boards.toml `hub.url` (with `token_file`, `ca_file`, ...) reaches fpgahub's REST API instead of ssh; with `host` too, REST wins and `host` is the data plane's SSH fallback. `via = "hub"` routes through the hub when the lease gate allows, else tunnels.
  - `session.hub.client.transport` is `"ssh"` or `"rest"`. A REST client has `notes_supported = False` (+ `notes_reason`), `can_revoke() -> (bool, reason)` (force-release needs an admin token), `observe(event)` and `relevant(event)`.
  - `session.reach` may be a `DirectReach` (`status()['mode'] == 'direct'`, with the plan).
- **Readings are never zero-filled.** A missing value is `Reading.unavailable(...)` with a reason and a source.
- **DUT build kits (KIT-CORE, additive to `core.pack`).**
  - `BuildProfile` (the KIT-GUIDE pack hook, CCR KG-1): part, partition (`rp_inst`, `rp_pblock`, `boundary_ports`, `boundary_bits`), `clr_max`, the Vivado release, `static_usercode`, `kit_id`, the user design-id range; `source` is `kit` or `pack`.
  - `KitCheck(name, state, detail, identity)` with `state` `ok | mismatch | warning | unchecked`, and `kit_refusal(items, what)`: any `mismatch` refuses (identity: `IncompatibleError` 14, else `RefusedError` 15); `warning` and `unchecked` never do. A Vivado release mismatch is a `warning` in HM; the generated `build_rm.tcl` refuses another major.minor (david K4).
  - `KitAdapter` (`build_profile`, `check_kit`, `kit_from_dir`) is found by module convention, like the pin model: `<pack package>.kit:make_kit_adapter()` (`harness_manager.services.kit.kit_adapter(pack)`), so a pack opts in without an edit to its `pack.py`. The MPS3 one also offers `taken_designs`, `propose_rm_id`, `rm_id_checks`, `check_pair`, `pack_receipt` and `import_overlay`. A pack without the module: `UnavailableError("build_kit", ...)`.
  - Kits live in the engine's `ContentStore`: files as kind `rm_kit_file` (meta `static_id, path, role`), then `kit.json` as kind `rm_kit` (meta `static_id, board_type, vivado, usercode, impl, source, imported_at`).

## Changing a contract

1. Write a **contract change request** in your hand-back:
   - the module;
   - the change as a minimal diff;
   - why it is needed;
   - who else it affects.
2. The lead applies agreed changes to `main` at the next merge window, then updates this file.
3. A change that blocks a team is applied mid-wave. Every team is told, and rebases.
4. Append-only lists (exit codes, event topics, capability names, TSV columns) may only grow.
