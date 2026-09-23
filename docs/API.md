# harness-manager-daemon: the local engine service API (v1)

This is a lead-owned contract, frozen for Wave 2. Team T13 implements the server and a Python client. Team T14 builds the web UI against it. fpgahub (T8) will later serve the same API shape in hub mode.

## Why it exists
- A board's lock belongs to one process, and each board port accepts one client.
- So the CLI, the web UI and long-lived sessions (debug, consoles) must all go through **one engine process per user**.
- `harness-manager-daemon` is that process. It wraps `harness_manager.engine.Engine` unchanged.

## Process and security
- **Starting it:**
  - `harness-manager daemon start|stop|status`;
  - `harness-manager ui` starts the daemon if needed, then opens the browser;
  - `harness-manager ui --no-browser --port N` for use over `ssh -L`.
- **Binding:** `127.0.0.1` only, by default. A non-loopback bind needs `--listen ADDR` and prints a warning.
- **State:** `<state_dir>/daemon.json` (mode 0600) holds `{pid, port, token, started_at, version}`. Only one daemon runs per state dir.
- **Auth:** every request carries `Authorization: Bearer <token>`.
  - The browser is opened at `http://127.0.0.1:N/#token=<token>`. The page moves the token into `sessionStorage` and removes it from the URL.
  - WebSockets pass `?token=`.
  - A missing or wrong token gets 401. There are no cookies, so a cross-site page cannot ride a session.
- **Static files:** the web UI is served from `/` (the wheel's package data). There is no CDN and no network fetch; everything is vendored.

## Conventions
- **Base path:** `/api/v1`. Bodies are JSON.
- **Success:** `{"ok": true, ...}`.
- **Failure:** `{"ok": false, "error": {"code": <ExitCode>, "name": "HELD", "message": "...", "hint": "...", "holder"?, "capability"?, "reason"?}}`. This is the same shape as the CLI's `--json` errors.
- **HTTP status by error:**

  | Error | Code | HTTP status |
  |---|---|---|
  | USAGE | 2 | 400 |
  | ABSENT | 3 | 404 |
  | HELD, ALREADY | 4, 8 | 409 |
  | UNREACHABLE | 7 | 502 |
  | UNAVAILABLE, NOTHING_ON_TARGET | 12, 13 | 422 |
  | INCOMPATIBLE, REFUSED | 14, 15 | 409 |
  | anything else | — | 500 |
- **Board ids** are URL-encoded (`mps3%40192.168.10.101%3A6900`).
- **Long operations** return `202 {"ok": true, "job": "<id>"}`. Progress and completion arrive as events. `GET /jobs/{id}` returns `{state: running|done|failed, result?, error?, progress: {phase, done, total}}`.
  - Long operations: deploy, restore, reboot, sd backup/install/restore, and debug up.
- **Serialisation:** dataclasses are serialised with `harness_manager.cli.output`'s rules (enums become values, frozensets become sorted lists), so the CLI and API emit the same JSON for the same object.

## Endpoints

| Method and path | Engine call | Returns |
|---|---|---|
| `GET /health` (no auth) | — | `{ok, version}` |
| `GET /packs` | `engine.packs()` | `{packs: {name: title}, capabilities: {pack: [{name, title, needs_hint}]}}` |
| `POST /probe` `{hosts?, serial_ports?, volumes?, scan_usb?, scan_network?, timeout_s?}` | `engine.probe(ProbeHints)` | `{candidates: [Candidate]}` (includes `identity` when probed) |
| `GET /boards` | open boards + lock owners | `{boards: [{board_id, open: bool, holder?: LockOwner, candidate}]}` |
| `POST /boards` `{target?, candidate?, note?}` | `engine.open(...)` | `{board_id, info: BoardInfo}` |
| `DELETE /boards/{bid}` | `engine.close(bid)` | `{ok}` |
| `GET /boards/{bid}` | `engine.info(bid)` | `BoardInfo` |
| `GET /boards/{bid}/lock` | `engine.lock_owner(bid)` | `{holder: LockOwner or null}` |
| `GET /boards/{bid}/telemetry` | `engine.telemetry.readings(session)` | `{readings: [Reading]}` |
| `GET /boards/{bid}/overlays` | `deploy.compatible(session)` | `{loadable: [OverlayRef], blocked: {name: reason}}` |
| `POST /boards/{bid}/preflight` `{overlay}` | `deploy.preflight` + `core.pack.preflight_refusal` | `{items: [PreflightItem], refusal?: error}` |
| `POST /boards/{bid}/deploy` `{overlay}` | `deploy.deploy` | 202 job |
| `POST /boards/{bid}/restore` | `deploy.restore_baseline` | 202 job |
| `POST /boards/{bid}/reset` `{target}` | `session.resets.reset` | `{ok}` |
| `GET /boards/{bid}/clocks` · `POST .../clocks` `{name, mhz}` | `session.clocks` | `{readings}` / `{reading}` |
| `GET /boards/{bid}/consoles` | `consoles.names` | `{names}` |
| `WS /boards/{bid}/consoles/{name}` | `consoles.subscribe` | binary frames both ways (bytes from and to the board); text frame `{"state":...}` on a state change |
| `POST /boards/{bid}/consoles/{name}/export` `{port?}` | `consoles.export_tcp` | `{host, port}` |
| `GET /boards/{bid}/debug` · `POST .../debug/detect` · `POST .../debug/up` · `POST .../debug/down` | `debug.status`/`detect`/`up`/`down` | `DebugStatus` / `{idcode}` / 202 job / `DebugStatus` |
| `GET /boards/{bid}/controller/temps` · `/osc` | `session.controller.temperatures`/`oscillators` | `{readings}` |
| `POST /boards/{bid}/controller/reboot` `{wait_s?}` | `session.controller.reboot` | 202 job; the result is the evidence |
| `POST /boards/{bid}/controller/command` `{line, arm?}` | `session.controller.command` | `{reply}` (allowlist enforced by the adapter) |
| `GET /boards/{bid}/storage/pending` | `session.storage.pending` | `{pending: obj or null}` |
| `POST /boards/{bid}/storage/backup` `{dest_dir?}` | `session.storage.backup` | 202 job; the result is a BackupRecord |
| `POST /boards/{bid}/storage/install` `{files: {dest: path}, backup_path}` | `load_backup` + `install` | 202 job |
| `POST /boards/{bid}/storage/restore` `{backup_path}` | `load_backup` + `restore` | 202 job |
| `POST /boards/{bid}/lab/{verb}` `{...}` | the CLI's lab verbs (`link`/`display`/`macgen`/`dutrx`) | verb result |
| `GET /help/tabs` | the CLI's `help --tabs` | `{tabs: [{name, text}]}` |
| `GET /jobs/{id}` | — | job state |
| `GET /jobs` | — | recent jobs; each record has `job, kind, board_id, state, phases, started_at, ended_at` |
| `GET /boards/{bid}/session` | session adapters | `{candidate, adapters: {deploy, consoles, debug, resets, clocks, telemetry, controller, storage, shell}: bool, reset_targets, job, job_kind, services: {deploy, consoles, debug, telemetry}: null or reason}` |
| `POST /daemon/shutdown` `{force?}` | — | `{ok}`; 409 while a job runs unless `force` |

## Behaviour clarified by the implementation (T13)
- `/health` is also at `/api/v1/health` and returns `{ok, version, pid, service}`.
- A 401 carries code 15 (REFUSED). The UI should tell the user to run `harness-manager ui` again.
- `POST /boards` returns `{board_id, info}`. If `info` is null, `info_error` explains why, but the session IS open. A 409 with name ALREADY means the board is already open in the daemon, so the UI should just use it.
- While a job runs on a board, every request that touches the board returns 409 HELD naming the job.
- `POST /deploy` runs the preflight synchronously. A mismatch returns 409 (code 14 or 15) with `error.data.{overlay, preflight}`, and no job is created. `POST /preflight` returns 200 and includes `refusal` only when it refuses.
- `overlay` in a request body may be a name, an `rm_id`, or the OverlayRef object itself.
- `GET /boards/{bid}/overlays` also returns `overlays` (all of them, including blocked ones).
- Board ids are percent-encoded, including `/`.
- **Console WebSocket frames:**
  - The first frame is text: `{"state","name","detail"}`.
  - Later text frames are `{"state":...}`, `{"dropped","dropped_frames"}` or `{"error"}`.
  - The client sends keystrokes as BINARY frames.
  - When the console ends, the server sends `{"state":"closed"}` and closes with code 1000.
- A refused WebSocket gets an HTTP denial carrying the error envelope, or a close with code 4000+exit code.
- The daemon's lock note is `harness-manager-daemon: <note>`.

## Events
- **Endpoint:** `WS /api/v1/events?token=…&topics=board.*,deploy.*`.
- **Each text frame:** `{"topic", "board_id", "data", "at"}`, one per `core.events.Event`, with the topics listed in docs/CONTRACTS.md.
- **Job events:** `job.started`, `job.progress {job, phase, done, total}`, `job.done {job, result}` and `job.failed {job, error}`.
- **Drops:** `events.dropped {dropped}` is sent when a slow client's bounded queue drops its oldest events.

## The Python client (T13)
`harness_manager.client.RemoteEngine` implements the `core.services.Engine` protocol over this API. `harness_manager.cli.engine.get_engine()` prefers a running daemon and falls back to the in-process engine, so the CLI and the web UI share one board session.

## Additions at the T14 merge (lead)
- **T14-1:** `GET /packs` also returns each pack's capability `title` and `needs_hint`. The web UI uses them in place of its mirrored titles.
- **T14-3:** while `health.control_channel` is `rescue`, `offline` or `wedged`, `BoardInfo` lists the capabilities that need the harness's Ethernet services as unavailable, with the short reason `the harness is <state> (see Health)`; `health.notes` carries the full explanation. Routes over other links (USB, SSH, a power plug) are unaffected.
- **T14-4:** `/session` `services` gives `null` when an engine service works, else its stub `reason` (docs/CONTRACTS.md convention).
- **T14-5:** a HELD caused by a daemon job carries `error.data.{job, kind, board_id}`; `/boards` rows and `/session` add `job_kind`. Front-ends read these, not the holder text.
- **Declined, T14-2:** a wedged harness still makes `GET /boards/{bid}` fail with its own error code. That code is the honest answer, and the CLI's exit codes depend on it. The UI keeps the last good read and labels it stale.
- **Static files:** harness-manager-daemon serves the UI with `harness_manager.web.mount_static`: CSP `script-src 'self'`, `nosniff`, `no-cache`, and fixed media types.

## Week-plan additions (frozen 2026-09-23, night; lanes L1, L2, L4)

The routes come from extension modules: `harness_manager.daemon.<consoles_api|hub_api|power_api|update_api>`, each with `register(ctx: RouteContext)`. `create_app` loads them before the core `/boards/{bid:path}` routes, because that path converter is greedy. Every rule above applies: bearer auth, the error envelope, 409 HELD while a job runs, 202 plus `job.*` events for long work.

### Consoles: PTYs for `screen`, and baud (L2, `consoles_api.py`)

| Method and path | Returns |
|---|---|
| `POST /boards/{bid}/consoles/{name}/pty` | `{path, device, command}`. Idempotent: it creates the console's PTY if needed. `path` is stable while the board is open: `/tmp/harness-manager-$USER/<board-slug>/<name>`, a symlink to `device` (`/dev/pts/N`). `command` is `screen <path>`. On Windows: 422 UNAVAILABLE, with the TCP export as the hint. |
| `GET /boards/{bid}/consoles/{name}/pty` | `{pty: {path, device, command, clients} or null}` |
| `DELETE /boards/{bid}/consoles/{name}/pty` | `{ok}`. Also closed when the board closes. |
| `GET /boards/{bid}/consoles/{name}/baud` | `{baud, settable, reason?, choices: [int], source}`. `source` is `serial` (the rate the host port is set to), `design` (the loaded design's fixed rate, e.g. 76800 for nanosoc), `harness` (the harness reports it) or `unknown`. `settable: false` carries `reason`. |
| `POST /boards/{bid}/consoles/{name}/baud` `{baud}` | `{baud, source}`. A serial console reopens at the new rate. An Ethernet console needs the harness feature `uart_baud`; without it, 422 UNAVAILABLE with the reason. |
| `GET /boards/{bid}/consoles` | Also returns `consoles: [{name, kind: "ethernet"|"serial", baud, settable, pty: path or null}]` next to `names`. |

Events: `console.state` gains `{baud}` when the rate changes, and `console.pty {name, path, clients}` fires when screen attaches or detaches.

### The hub: tunnel and leases (L1, `hub_api.py`)

| Method and path | Returns |
|---|---|
| `POST /probe` `{..., via?}` and `POST /boards` `{target?, via?}` | `via: "ssh:HOST"` reaches a board through an SSH tunnel (the lab hub). The candidate's Ethernet link is then `via="ssh"`. A `via` in boards.toml does the same without the field. |
| `GET /boards/{bid}/tunnel` | `{tunnel: {via, host, state: up|down|starting, ports: {remote: local}, detail} or null}` |
| `GET /boards/{bid}/lease` | `{lease: {target, holder, expires_at, mine} or null, hub: HOST or null}`. null when the board is not behind a hub. |
| `POST /boards/{bid}/lease` `{ttl_s?}` | 202 job `lease`. It completes when the lease is HELD (it may queue); the result is `{lease}`. The daemon heartbeats it while the board is open. |
| `DELETE /boards/{bid}/lease` | `{ok}` releases this client's lease. |

Events: `lease.state {target, state: held|queued|released|expired|lost, holder, expires_at}`.

### Power and update (L4, `power_api.py`, `update_api.py`)

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/power` | `{readings: [Reading], cycle_reason, device}`. `cycle_reason` is `""` when the board can be cycled. |
| `POST /boards/{bid}/power/cycle` `{off_s?}` | 202 job `power_cycle`; the result is the device's evidence. |
| `POST /update/check` `{board_id?, source?, channel?}` | 202 job `update_check`. The result is the check: the channel, the releases, the app update, and the board's plan with `fingerprint`, `mode`, `rekey`, `blockers`, `warnings`, `steps`. Read-only. |
| `POST /boards/{bid}/update/harness` `{fingerprint, rekey_phrase?}` | 202 job `update_harness`. The plan is recomputed and must match `fingerprint`. A re-key needs `rekey_phrase == "REKEY <static_id>"`. The result is the outcome. |
| `POST /boards/{bid}/update/rollback` | 202 job `update_rollback` |
| `POST /update/app` `{version?}` · `POST /update/app/rollback` | 202 jobs `update_app` / `update_app_rollback`. Refused (409) while any board job runs. |

Events: the `update.*` topics from docs/CONTRACTS.md are forwarded as they are.

**As built by L4 (additive):**
- `GET /power` also returns `board_id`.
- The `power_cycle` job result is `{board_id, meter, off_s, was_on, confirmed_off, confirmed_on, seconds}`. `power.cycle` phases are `off` and `on` only: there is no board-agnostic witness that the board came back, so refresh on `job.done`.
- `off_s` must be 2–300 s.
- Engine-wide jobs have `board_id: ""`, and `/probe` is not refused for them.
- `update/harness` takes optional `channel` and `source`. Without them, the plan comes from the check that issued the fingerprint, else the board's last check.
- `update/rollback` takes optional `{backup_path, wait_s}`. `/update/app` takes optional `{channel, source}`.
- Refusals carry `error.data.plan` (with the new `fingerprint`). Failed outcomes carry `error.data.outcome`.
- The update check result has `releases: {harness: [...], app: [...]}`, and `plan.fingerprint`.
- An app switch fails with HELD while this process holds any board: close the boards first.

**As built by L2 (consoles):**
- **The screen command.** `command` is `screen <path> <baud>` for a serial console whose rate is a standard termios speed, because screen with no rate sets 9600 on the line. Otherwise it is `screen <path>`.
- **Additive fields:**
  - the pty and baud replies carry `name`;
  - `DELETE .../pty` returns `closed`;
  - `GET baud` adds `kind`, `mode`, `design` and `cite`;
  - console rows add `source`, `reason`, `state` and `alias_of`;
  - `GET /consoles?rates=0` skips the rate lookup.
- **Holds and gates:** `POST baud` is 409 HELD while a job runs. `GET baud` and `GET /consoles` are never HELD: they return the last report, or `baud: null` with the reason. The PTY routes take no gate, like the console WebSocket.
- **`clients`** counts open file descriptions, found through inotify on the PTY device. One terminal per PTY: screen opens it exclusively, and the daemon clears the exclusive flag when screen leaves, so a re-attach works.
- **The rates on today's fielded shell:**
  - `uart0` is 76800, fixed by the nanosoc design;
  - `uart1` has no DUT side;
  - `swo` is 2000000 (firmware divisor);
  - FPGA UART lanes are settable;
  - hub shares are fixed by the share.

**As built by L1 (hub):**
- **Additive keys:** the tunnel object adds `forwards`, `restarts`, `pid` and `shares`; the lease object adds `user`.
- **`DELETE /lease`** returns `{released}`, or `{cancelled: true}` for a queued acquire, and 404 when this client holds no lease.
- **Limits:** `ttl_s` must be 60–86400, and `GET /lease` is cached for 10 s.
- **Holds:** a queued lease job holds the board (409 with `error.data.kind == "lease"`). Cancel it with `DELETE /lease`.
- **Over a hub share:** MCC reads are slow (about 2 s for temperatures, about 6 s for oscillators), and SD storage is unavailable because there is no `USB_MSD` link.
