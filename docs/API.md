# socharnessd: the local engine service API (v1)

This is a lead-owned contract, frozen for Wave 2. Team T13 implements the server and a Python client. Team T14 builds the web UI against it. fpgahub (T8) will later serve the same API shape in hub mode.

## Why it exists
- A board's lock belongs to one process, and each board port accepts one client.
- So the CLI, the web UI and long-lived sessions (debug, consoles) must all go through **one engine process per user**.
- `socharnessd` is that process. It wraps `socharness.engine.Engine` unchanged.

## Process and security
- **Starting it:**
  - `socharness daemon start|stop|status`;
  - `socharness ui` starts the daemon if needed, then opens the browser;
  - `socharness ui --no-browser --port N` for use over `ssh -L`.
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
- **Serialisation:** dataclasses are serialised with `socharness.cli.output`'s rules (enums become values, frozensets become sorted lists), so the CLI and API emit the same JSON for the same object.

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
- A 401 carries code 15 (REFUSED). The UI should tell the user to run `socharness ui` again.
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
- The daemon's lock note is `socharnessd: <note>`.

## Events
- **Endpoint:** `WS /api/v1/events?token=…&topics=board.*,deploy.*`.
- **Each text frame:** `{"topic", "board_id", "data", "at"}`, one per `core.events.Event`, with the topics listed in docs/CONTRACTS.md.
- **Job events:** `job.started`, `job.progress {job, phase, done, total}`, `job.done {job, result}` and `job.failed {job, error}`.
- **Drops:** `events.dropped {dropped}` is sent when a slow client's bounded queue drops its oldest events.

## The Python client (T13)
`socharness.client.RemoteEngine` implements the `core.services.Engine` protocol over this API. `socharness.cli.engine.get_engine()` prefers a running daemon and falls back to the in-process engine, so the CLI and the web UI share one board session.

## Additions at the T14 merge (lead)
- **T14-1:** `GET /packs` also returns each pack's capability `title` and `needs_hint`. The web UI uses them in place of its mirrored titles.
- **T14-3:** while `health.control_channel` is `rescue`, `offline` or `wedged`, `BoardInfo` lists the capabilities that need the harness's Ethernet services as unavailable, with the reason `the harness is <state>: <note>`. Routes over other links (USB, SSH, a power plug) are unaffected.
- **T14-4:** `/session` `services` gives `null` when an engine service works, else its stub `reason` (docs/CONTRACTS.md convention).
- **T14-5:** a HELD caused by a daemon job carries `error.data.{job, kind, board_id}`; `/boards` rows and `/session` add `job_kind`. Front-ends read these, not the holder text.
- **Declined, T14-2:** a wedged harness still makes `GET /boards/{bid}` fail with its own error code. That code is the honest answer, and the CLI's exit codes depend on it. The UI keeps the last good read and labels it stale.
- **Static files:** socharnessd serves the UI with `socharness.web.mount_static`: CSP `script-src 'self'`, `nosniff`, `no-cache`, and fixed media types.
