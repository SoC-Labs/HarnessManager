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

## To be fixed at the start of Wave 1 (each owner proposes it in its first hand-back)

| Interface | Owner | Shape |
|---|---|---|
| `Engine` (T1) | T1 | `probe()`, `open(board_id)`, `info(board_id)`, `close()`, `bus`. Holds a `SessionLock` per open board. |
| `ContentStore` (T1) | T1 | `put(bytes\|path) -> sha256`, `get(sha)`, `index(kind, key)` |
| `DeployService` (T2) | T2 | `compatible(session)`, `preflight(session, overlay) -> list[Check]`, `deploy(session, overlay)` (emits `deploy.*`), `restore_greybox(session)` |
| `McCDriver` (T3) | T3 | `command(line, allow=...)`, `reboot(witness=...)`, `temps()`, `oscillators()` |
| `SdVolume` (T3) | T3 | `find()`, `backup()`, `install(bundle)`, `restore(backup)` |
| `ConsoleBroker` (T4) | T4 | `subscribe(board, name) -> stream`, `export_tcp(board, name) -> port` |
| `DebugService` (T4) | T4 | `up(session)`, `down(session)`, `status(session)`, `detect(session)` |

## Changing a contract

1. Write a **contract change request** in your hand-back:
   - the module;
   - the change as a minimal diff;
   - why it is needed;
   - who else it affects.
2. The lead applies agreed changes to `main` at the next merge window, then updates this file.
3. A change that blocks a team is applied mid-wave. Every team is told, and rebases.
4. Append-only lists (exit codes, event topics, capability names, TSV columns) may only grow.
