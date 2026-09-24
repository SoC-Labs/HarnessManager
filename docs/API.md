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
- An OverlayRef also carries `ltx_sha256` and `receipt_sha256`: the sha256 of the ILA probes file and of the build receipt that travel with the pair, or `""` when the overlay has none (additive; the content store keeps both, `harness_manager_mps3/overlays.py`).
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
- **Timestamps:** `lease.expires_at` is an ISO 8601 string as fpgahub reports it. Every other timestamp is epoch seconds.

### Lease requests, force release and leaving the queue (LR-C, `hub_api.py`)

docs/LEASE_REQUESTS.md is the design, with the lead decisions D1–D8.

| Method and path | Returns |
|---|---|
| `POST /boards/{bid}/lease/request` `{message?, ttl_s?}` | 202 job `lease_request`. Phases: `queued`, `notified`, `answered`, `force-available`, `held`. It ends with `{lease}` when the lease is ours, or `{left: true}` when we leave the queue. |
| `POST /boards/{bid}/lease/respond` `{id, answer: "release"\|"keep", minutes?, message?}` | `{ok}` |
| `POST /boards/{bid}/lease/force` `{confirm: true, confirm_board?}` | 202 job `lease_force`; the result is `{lease}`. Refused before any revoke (below). `confirm_board` is the board's name, needed when `lease.holder_kind` is not `"hm"` (D12). |
| `DELETE /boards/{bid}/lease/queue` | `{left: bool}`: leaves the queue and withdraws the request. |
| `DELETE /boards/{bid}/lease/taken` | `{dismissed: bool}`: forgets the last forced release of our lease (D11). `GET /lease` then returns `taken: null` until the next one. |

`GET /boards/{bid}/lease` adds `queue`, `request`, `incoming` (each with its `answer` or `null`), `taken` and `board` (the physical board a force revokes, `mps3_01`). It also says what this hub connection can do, before any request: `notes_supported` and `notes_reason` (messages and Keep; false over fpgahub's REST API), and `can_revoke` and `revoke_reason` (force-release; over REST only with an admin token). `request.reasked` and `request.reasked_at` say that the request was re-sent to a new holder (D9). The fields are in docs/LEASE_REQUESTS.md "API". Times are ISO 8601 UTC with `+00:00`.

Events: `lease.wanted`, `lease.answered`, `lease.force_available`, `lease.taken` and `lease.left` (docs/CONTRACTS.md). The lease service publishes them; the events WebSocket forwards them unchanged.

**As built by LR-C:**
- **A "keep" answer does not end the request job** (D1): the phase becomes `answered` and `lease.answered` is emitted. The job ends when the lease is held, when we leave (`{left: true}`, a success, D7), or after a successful force.
- **Holds:** a running `lease_request` job holds the board (409 HELD with `error.data.kind == "lease_request"`), like L1's queued `lease` job. `GET /lease`, `respond`, `force` and `DELETE /lease/queue` still work while it runs.
- **`request` refusal:** 409 ALREADY, with no job, when the lease is already this principal's, in this session or another.
- **`force` refusals** come before any revoke (D3):
  - 400 USAGE without `confirm: true`.
  - D12: when no Harness Manager session is known to hold the lease (`GET /lease` has `lease.holder_kind: "unknown"` and `lease.holder_kind_reason`; it may be a script), 400 USAGE without `confirm_board` (`error.data.{holder_kind, holder_kind_reason, confirm_board}` names the board to type), and 409 REFUSED when it is not the board's name (the N1 name, the hub's board id, the address or the target; any case).
  - 422 UNAVAILABLE while the holder still has time to answer, with `error.data.{request_id, deadline_at, time_left_s}`.
  - 409 REFUSED with the reason: no request, not at the head of the queue (`error.data.position`), or answered (`release`, or a `keep` whose minutes have not run out, with `error.data.time_left_s`).
  - 409 ALREADY when the lease is already yours.
- **`lease_force` runs beside the board's own `lease_request` job** (D2): the revoke is what ends that job, so force does not wait for its gate. With no request job running, force is an ordinary board job. Any other running job refuses it with 409 HELD. Its phases are `revoke` and `held`. `GET /jobs` and `GET /jobs/{id}` include it.
- **Validation (400 USAGE):** `id` is `[A-Za-z0-9_.-]{1,64}`; `minutes` is 5, 15, 30 or 60, and only with `keep`; `message` is at most 500 characters (control characters become one space); `ttl_s` is 60–86400.
- **`respond`:** a `release` is 409 HELD while a non-lease job (a deploy) runs on the board. A `keep` never is.
- **Dismissing (D11):** `DELETE /lease/taken` is local (the service's own record, no hub call) and takes no gate, so it works while a job runs. `dismissed` is false when there was nothing to dismiss; running it twice is fine. No event is sent: a front-end that dismisses clears its own banner, and others see `taken: null` on their next `GET /lease`.
- **Leaving:** `DELETE /lease/queue` stops the request job, which ends with `{left: true}`. `left` is true when a queue entry was removed or a request job was running. `DELETE /lease` does the same for a queued request and returns `{cancelled: true, left: true}`. Closing the board, or stopping the daemon, also leaves the queue.

### XDC export (T10, `xdc_api.py`)

docs/XDC_EXPORT.md is the reference: the pin model, the design format, the kits and the checks.

| Method and path | Returns |
|---|---|
| `GET /xdc?pack=mps3` | the catalogue: `model` (board, status, shells, sources), `kits`, `default_design`, `designs`, `checks` (the check codes). No board needed. |
| `POST /xdc/export` `{pack?, kit, design?, static_id?, preview?, format?}` | the kit: `{kind, design, passed, files: {name: text}, checks, facts}`. |
| `GET /boards/{bid}/xdc` | the catalogue for the board's pack, plus `board: {static_id, model_static_id, matches, reason}` and `board_id`. |
| `POST /boards/{bid}/xdc/export` `{kit, design?, preview?, format?}` | the same kit, checked against the static the board runs. |

- **`kit`** is `rm-kit` or `board`. **`design`** is a built-in design's name or an inline design object. A file path is 400 USAGE: the daemon's filesystem is not the caller's.
- **`passed`** is the kit's verdict; `ok` stays the envelope's. A failed check is 409 REFUSED with `error.data.checks` (every check, errors and notes), unless `preview: true`: then 200 with `passed: false` and the files, so a front end shows them next to the failures.
- **`format: "zip"`** answers `application/zip` (the files and `manifest.json` under `<design>/`), with `X-Xdc-Ok: 1|0`.
- **The board.** The kits come from the pin model, never from the board. The board routes need one fact from it, the static it runs: the identity it reported when probed, else one identity read under the board gate (409 HELD while a job runs). An RM kit for a board on another static fails the `static_id` check; with no static known it is a note.
- **Errors:** 400 USAGE for a bad `kit`, `format`, `design` or `static_id` type, or a design of the other kind; 404 ABSENT for an unknown design, pack or shell, or a board that is not open; 422 UNAVAILABLE for a pack with no pin model.

### Front panel: what it shows, presence and Identify (P1, `panel_api.py`)

docs/design/CLCD_ALIGNMENT.md is the design (§2, §5). The Linux harness's verbs `hello`, `panel` and `locate` (lanes R1-R3) are not built yet; until they are, the MPS3 pack follows the design's wire and its FakeShell profile answers it.

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/panel` | `{panel: PanelState or null, reason, identify: {available, reason, until}, support: {front_panel, presence, locate, source}, presence: {active, reason, sid, last_hello_at, sent, ridden, skipped, last_error, interval_s}}` |
| `GET /boards/{bid}/panel/frame` | `{rows: [15 strings of 40], roles, source, observed_at, note}` |
| `POST /boards/{bid}/identify` `{seconds?}` | `{until, seconds}`: the board blinks its panel until `until` (epoch seconds). `seconds` is 0-30 (default 10); 0 stops a blink. |

- **`PanelState`:** `{page, owner, pending, banner, card, touch: {present, cal, ok, bus_lost, recoveries, reason}, sessions: [{sid, who, role, age_s, mine}], count, seq, events: [{seq, kind, on, ms_ago, at}], source, observed_at, note}`.
  - `source` is `panel` (read from the panel: the Linux harness) or `rebuilt` (bare metal: the owner from `display`, everything else unknown; `note` says it was rebuilt from what Harness Manager read).
  - `page` and `owner` are `""` when not known. `sessions` are ordered holder > owner > watch, most recent first; `mine` marks this Harness Manager's own.
  - `touch.ok` is `false` with `reason` "touch unavailable (...)" when the harness's `stats` says `touch_ok: false`; every touch field is `null` when the harness did not say.
- **`panel` is null** with `reason` when the board has no front panel Harness Manager can reach (a USB-only board, or a pack with no panel adapter).
- **Identify on bare metal:** `identify.available` is false with the reason ("needs harness feature 'locate' (Linux harness)"); `POST /identify` is 422 UNAVAILABLE with the same reason, and nothing is sent to the board. A bad `seconds` is 400 USAGE, before the board.
- **Gates:** all three go through the board gate: 409 HELD naming the job while one runs. `POST /identify` is short, not a job.
- **Rate:** the board rate-limits reads, so `GET /panel` reuses an answer up to 1 s old and `GET /panel/frame` one up to 3 s old.
- **The mirror:** `rows` are 15 strings of 40 characters; `roles` is 600 per-cell role codes (`t` text, `i` inverted in a rebuilt frame; the Linux renderer's codes otherwise), or `""` when not known.

**Presence (no route).** For each board the daemon has open, it sends the board a `hello` every 30 s (every 10 s while a lease request or an Identify is open), first offered to the next control connection the daemon opens anyway. It is skipped while a job holds the board, except a lease job, which waits on the hub. The board lists a session for 90 s after its last hello, and keeps at most 4. A bare-metal board is never sent one; `presence.reason` says why. Closing the board stops the hellos.

Events (docs/CONTRACTS.md):
- `panel.state` when what the panel shows changes;
- `panel.tap` once per tap on the glass (de-duplicated by `seq`). A tap `on: "request"` (the lease-request banner) carries `notify: "holder"` and `request: {id, by}` in the lease holder's own Harness Manager: it notifies the holder and never releases;
- `panel.locate` when Identify starts or stops.

### DUT build kits and the build guide (KIT-CORE, `kit_api.py`)

docs/design/DUT_BUILD_KIT_STORAGE.md and docs/design/DUT_BUILD_GUIDE.md are the designs; david's decisions K1-K9 (2026-09-24) apply. A **kit** is one static's build inputs (the locked static DCP, whose CRC-32 is the static_id, plus `kit.json`), cached in the engine's content store.

| Method and path | Returns |
|---|---|
| `GET /kits` | `{kits: [summary], sources: [{name, available, reason}]}`. A summary is `{static_id, kit_id, board_type, part, vivado: {release, build, checkpoint_version}, static_usercode, harness_impl, access, ip_class, licence_note, size, files, sha256, source, imported_at}`. |
| `GET /kits/{static_id}` | `{kit: summary, manifest: kit.json, checks}`: the blobs re-hashed and the DCP's CRC-32 recomputed. 404 when not cached. |
| `POST /kits/fetch` `{static_id?, board_id?, source?}` | 202 job `kit_fetch` (engine-wide). `source`: `cache`, `channel`, `hub` or an absolute path; default, in that order. The result is `{kit, source, checks}`; a kit for another static than the board fails the job 14. |
| `POST /kits/import` `{path}` | `{kit, already, checks}`: a kit directory, a kit zip, or loose mint files (`fielded/<sid>/`). |
| `POST /kits/{static_id}/export` `{out_dir}` | `{out_dir, written}`: a plain directory Vivado opens. |
| `GET /kits/{static_id}/zip` | `application/zip`: `<static_id>/kit.json` and every kit file, for a browser that is not on the daemon's host. |
| `GET /boards/{bid}/kit` | `{board_id, static_id, cached, kit, profile, checks, sources, vivado}` for the static the board runs: its kit, the partition facts (`BuildProfile`), the kit against the live `shell_id` and `usercode`, and the Vivado found against the kit's release. |
| `GET /guide?pack=&static_id=&design=&build_dir=` | the guide: `{pack, static_id, board_id, kit_id, profile, vivado, design, build_dir, rm_id, steps, next}`. Each step is `{id, n, title, state, detail, reason, actions: [{kind: "copy", text}], checks}`; `state` is `done`, `next`, `blocked`, `failed` or `unchecked`; ids are `target`, `tools`, `kit`, `wrapper`, `build`, `check`. |
| `GET /boards/{bid}/guide?design=&build_dir=` | the same guide for the board's live static and identity. |
| `POST /guide/script` `{static_id, design, pack?, out_dir?, kit_dir?, jobs?, stop_after?, format?}` | `{design, rm_id, rm_id_proposed, static_id, kit_id, files: {name: text}, params, checks, command, receipt, out_dir, written}`. With `out_dir` it writes the build directory. `format: "zip"` answers `application/zip` (the files plus the kit). 409 REFUSED with `error.data.checks` when the design fails an XDC check. |
| `POST /kits/check` `{path, clearing?, static_id?, board_id?}` | `{passed, static_id, checks, facts}`, 200 whether or not it passed: a receipt (or a build directory) with its files and pair, or a bare partial. |
| `POST /kits/pack` `{path, out_dir?, import?}` | `{overlay_dir, manifest, imported, checks}`: the overlay triple written from a passed receipt (`build_receipt` names it), and with `import: true` put in the content store, so it shows in Program. 409 REFUSED with `error.data.checks` for a build that did not pass. |

- **Paths** (`path`, `out_dir`, `kit_dir`, `build_dir`, `source`, a `design` file) are absolute paths on the daemon's host (400 USAGE otherwise). The build runs on that host (david K6), and the daemon listens on loopback. `design` may also be a built-in design's name or an inline design object (docs/XDC_EXPORT.md), plus the optional `rm_id` and `build: {top, sources, include_dirs, defines, synth_hook, synth_dcp, rm_xdc}`.
- **Checks** are `{name, state, detail, identity}` with `state` `ok`, `mismatch`, `warning` or `unchecked`. Any `mismatch` refuses: 409 with name INCOMPATIBLE (14) when it is an identity check (the board's static or usercode), else REFUSED (15). `warning` and `unchecked` never refuse. A Vivado release that differs from the kit's is a `warning` here; the generated `build_rm.tcl` refuses another major.minor itself (david K4).
- **User rm_ids** (david K8): a design with no `rm_id` gets a proposal (design id `0x8000`-`0xFFFF`, stable per name, `rm_id_proposed: true`); a clash with the overlay catalogue is a `warning`.
- **Events:** `kit.progress {static_id, phase, bytes, total}` during a fetch; `kit.stored {static_id, source, kit_id}` when a kit enters the cache (docs/CONTRACTS.md).
- **Not yet:** the `channel` source reports itself unavailable until lane OTA-C adds the `rm-kit` channel kind; HM does not run Vivado (`kit build` prints the command).
- **Errors:** 400 USAGE for a bad id, path, `format`, `jobs` or `stop_after`; 404 ABSENT for a kit not in the cache, a missing receipt or file, or a board that is not open; 422 UNAVAILABLE for a pack with no build kit.

### Fabric debug over XVC (XVC-CORE, `xvc_api.py`)

docs/design/XVC_DEBUG.md is the design. **Scope:** XVC here is the harness's own XVC server, scoped to the reconfigurable partition's debug chain: the Debug Bridge and the debug hub and ILAs of the design loaded in the partition (on the Linux harness also the static MIG calibration hub behind the same bridge). It is never whole-device JTAG. Every status carries this as `scope`.

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/xvc` | `XvcStatus`: `{state, open, mode, relay_port, hw_server_port, hw_server_pid, hw_server, url, attached, board_slot, reach, ltx, warnings, scope, rm_id, rm_name, reason, detail}` |
| `POST /boards/{bid}/xvc/open` `{byo?}` | 202 job `xvc_open`; the result is the `XvcStatus`. Refused before any job (below). |
| `POST /boards/{bid}/xvc/close` | the `XvcStatus` (`down`): the client kicked, HM's hw_server stopped, the board's slot freed |
| `GET /boards/{bid}/xvc/tcl?byo=` | `{tcl, url, ltx, which, mode, scope, open}`: the Vivado Tcl for what is loaded |
| `GET /boards/{bid}/xvc/ltx?which=&format=` | the probes file as a download (`application/octet-stream`, `Content-Disposition`, `X-Ltx-Which`); with `format=json`, `{which, name, path, crc_ok, source, vivado}` |

- **States:** `down`, `starting`, `ready` (nothing attached), `attached` (`attached` names the local peer: `{peer, pid, command, since, bytes_up, bytes_down, shifts}`), `held` (another client holds the board's one XVC slot), `swapping`, `failed`. `open` says whether a session exists (it may be reconnecting or swapping). `board_slot` is `ours`, `held`, `refused`, `down`, `released` or `unknown`.
- **The relay holds the board's slot while a session is open** (D-X4): one upstream connection, whole XVC commands only, local clients in turn; a second local client is accepted and closed at once.
- **`mode`:** `m1` (HM's own hw_server: `-p0`, no `-d`/`-I`, one fixed port per board; Vivado connects to `url`, `localhost:H`) or `byo` (your own hw_server: `open_hw_target -xvc_url 127.0.0.1:R`, R being `relay_port`).
- **`reach`:** `hub-tunnel` (bare-metal behind a hub: the hub tunnel's 2542 forward), `board-ssh` (the Linux harness: `ssh -J HUB root@BOARD -L 127.0.0.1:p:127.0.0.1:2542`) or `direct`. boards.toml `xvc = { reach = "auto" }` chooses (`auto`, `hub`, `board-ssh`, `direct`).
- **`warnings`:** the bare-metal harness's XVC is open on every interface, so its status carries "XVC on this harness is unauthenticated: ..." until the Linux cutover (X6). A Linux harness carries the same warning until it reports the XVC lock (`xvc_lock`). Also: a swap closes the session and reopens it; `byo` reminds that your hw_server lingers 20 s.
- **`ltx`:** `{rm, static, full, preferred, note}`, each `{path, name, crc_ok, source, vivado}` or null. `preferred` is `full` when the mint staged a full-design file for the loaded RM (X5), else `rm`. `static` (the MIG view) is Linux only. `which` is `auto` (the preferred one), `rm`, `static` or `full`.
- **Refusals before any job:** 409 HELD while another job runs on the board; 409 HELD naming the holder when the board is behind a hub and the lease is not this client's (the lease holder only, and a hub that cannot be asked also refuses); 409 ALREADY when a session is open; 422 UNAVAILABLE when the image's XVC does not drive the Debug Bridge (`xvc_jtagbb`), the harness lacks `xvc_dbgbr`, or no hw_server is installed (use `byo`).
- **Gates:** `close` takes the board gate. `GET` routes never touch the board while a session is open; with none open, the first `GET /xvc` (and `tcl`, `ltx`) reads the board's identity once under the gate.
- **Swaps and leases:** `deploy.started` drops the board's slot and stops HM's hw_server before the swap; `deploy.done` (verified) re-attaches with a fresh hw_server on the same port and the new design's probes file; an unverified or failed swap closes the session with the reason. `lease.state` released, expired or lost closes it. Closing the board closes it.
- **Errors:** 400 USAGE for a bad `which`, `format` or `byo`; 404 ABSENT for no such probes file, or a board that is not open.

## Board names (lane N1, additive; CCR N1-1 to N1-4)
- **`Candidate` adds `name` and `name_source`.** They appear wherever a candidate does: `POST /probe`, `GET /boards` rows, `POST /boards` and `GET /boards/{bid}` (`info.candidate`), and the CLI's `probe --json` and `info --json`. `name` is the display name (`"mps3-01"`), and `""` means the board has none, so show the address. `name_source` is `config` (boards.toml `name`), `harness` (the board reports it), `hub` (the fpgahub board that owns the hub target, as the hub reports it or boards.toml `hub.board` states it) or `hub-target` (the same, derived from boards.toml `hub.target` by fpgahub's suffix rule with no hub call). The first of these that gives a name wins, in that order; `harness_manager.naming` holds the rule.
- **A name is display only.** It never keys a board: `board_id` does, and so do boards.toml tables, session locks and leases. A hub id is shown with `_` as `-` (`mps3_01` becomes `mps3-01`).
- **`BoardIdentity` adds `name`:** what the harness reports, `""` today (no fielded firmware sends it).
- **An open board can be renamed by `GET /boards/{bid}`.** When the harness or the hub gives a better name than the probe had, `info.candidate` carries it, and `GET /boards` shows it from then on. The first `info` of a board behind a hub asks the hub once (`fpgahub board list --json`, read-only) and caches the answer for an hour in that process. Set boards.toml `hub.board` to skip that call.
- **Events:** `board.found` and `board.identity` add `name` and `name_source`.
- **TSV:** `probe` and `info` append a `NAME` column.
- **The web UI** shows the name in the rail, in the header (with the design and shell under it), on the preview card and in the window title (`mps3-01 · Harness Manager`). A board with no name shows its address, as before.
