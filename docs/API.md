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
- **Host allow-list (SET-API, `hosts.py`):** a request whose `Host` is not a name this service answers to gets **403** with the error envelope (REFUSED, 15), before any route runs; a WebSocket is refused before it is accepted. The names: `localhost`, `127.0.0.1` (any `127.x.y.z`) and `::1` on any port; the `--listen` address unless it is a wildcard; off loopback, this machine's own host name and FQDN, and any IP address (a rebinding page's `Host` is its own name, never an address); and the names in the setting `advanced.allowed_hosts` (read when the service starts; the admin policy may set or lock it). Defence in depth against DNS rebinding: the token already keeps a hostile page out, and `PUT /settings/secrets` makes it worth more. `harness-manager daemon` always turns it on; an app a test builds with `create_app` has it only when given `allowed_hosts`.
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
| `GET /boards/{bid}/card` | `deploy.card_status(session)` | `{card: CardStatus, line}` |
| `POST /boards/{bid}/deploy` `{overlay, keep_on_card?}` | `deploy.deploy` | 202 job |
| `POST /boards/{bid}/restore` | `deploy.restore_baseline` | 202 job |
| `POST /boards/{bid}/reset` `{target}` | `session.resets.reset` | `{ok}` |
| `GET /boards/{bid}/clocks` · `POST .../clocks` `{name, mhz}` | `session.clocks` | `{readings}` / `{reading}` |
| `GET /boards/{bid}/consoles` | `consoles.names` | `{names}` |
| `WS /boards/{bid}/consoles/{name}` | `consoles.subscribe` | binary frames both ways (bytes from and to the board); text frame `{"state":...}` on a state change |
| `POST /boards/{bid}/consoles/{name}/export` `{port?}` | `consoles.export_tcp` | `{host, port}` |
| `GET /boards/{bid}/debug` · `POST .../debug/detect` · `POST .../debug/up` · `POST .../debug/down` | `debug.status`/`detect`/`up`/`down` | `DebugStatus` (GET adds `openocd: {ok, path, need, adapters, detail, hint}`: the OpenOCD this service would run and whether it has remote_bitbang) / `{idcode}` / 202 job / `DebugStatus` |
| `GET /boards/{bid}/controller/temps` · `/osc` | `session.controller.temperatures`/`oscillators` | `{readings}` |
| `POST /boards/{bid}/controller/reboot` `{wait_s?, force?, consent?}` | `session.controller.reboot` | 202 job; the result is the evidence. SLOT-TIMING: the job fails HELD (naming the card job) while the board's OS-slot card job is `writing` or `verifying`; `force: true` with `consent: "RESET <bid>"` resets anyway (the recovery of a job that never ends), else REFUSED |
| `POST /boards/{bid}/controller/command` `{line, arm?}` | `session.controller.command` | `{reply}` (allowlist enforced by the adapter) |
| `GET /boards/{bid}/storage/pending` | `session.storage.pending` | `{pending: obj or null}` |
| `POST /boards/{bid}/storage/backup` `{dest_dir?}` | `session.storage.backup` | 202 job; the result is a BackupRecord |
| `POST /boards/{bid}/storage/install` `{files: {dest: path}, backup_path}` | `load_backup` + `install` | 202 job |
| `POST /boards/{bid}/storage/restore` `{backup_path}` | `load_backup` + `restore` | 202 job |
| `POST /boards/{bid}/lab/{verb}` `{...}` | the CLI's lab verbs (`link`/`display`/`macgen`/`dutrx`) | verb result |
| `GET /help/tabs` | the CLI's `help --tabs` | `{tabs: [{name, text}]}` |
| `GET /jobs/{id}` | — | job state |
| `GET /jobs` | — | recent jobs; each record has `job, kind, board_id, state, phases, started_at, ended_at` |
| `GET /boards/{bid}/session` | session adapters | `{candidate, adapters: {deploy, consoles, debug, resets, clocks, telemetry, controller, storage, power, panel, shell}: bool, reset_targets, job, job_kind, services: {deploy, consoles, debug, telemetry}: null or reason}` |
| `POST /daemon/shutdown` `{force?}` | — | `{ok}`; 409 while a job runs unless `force` |

## Behaviour clarified by the implementation (T13)
- `/health` is also at `/api/v1/health` and returns `{ok, version, pid, service}`.
- A 401 carries code 15 (REFUSED). The UI should tell the user to run `harness-manager ui` again.
- `POST /boards` returns `{board_id, info}`. If `info` is null, `info_error` explains why, but the session IS open. A 409 with name ALREADY means the board is already open in the daemon, so the UI should just use it.
- While a job runs on a board, every request that touches the board returns 409 HELD naming the job.
- `POST /deploy` runs the preflight synchronously. A mismatch returns 409 (code 14 or 15) with `error.data.{overlay, preflight}`, and no job is created. `POST /preflight` returns 200 and includes `refusal` only when it refuses.
- `overlay` in a request body may be a name, an `rm_id`, or the OverlayRef object itself.
- **Keep on the card** (L1, decided 2026-09-25: off by default). `POST /deploy` with `keep_on_card: true` also writes the design to the board's user microSD after the verified swap, so the board boots into it at its next power-on. Without it (or `false`) the card is never written. A non-boolean is 400.
  - `GET /boards/{bid}/card` reads the card and writes nothing: `CardStatus` is `{store, present, state, text, reason}`. `store`: the harness keeps designs on a card (the MPS3 harness reports `usd`). `present`: a card is in the slot. `state`/`text`: the store's own words (`empty`, `valid`, `foreign`...; the front panel's card row). `reason`: why a deploy cannot keep its design on the card now, `""` when it can. The web UI shows its "Keep on the card" box only when `store` and `present`.
    - LINUX-SLOTS adds to `CardStatus` (additive; null/empty where unknown): `card_mb`, `default` (`{rm_id, rm_name, static_id, slot}`: what the board loads at power-on), `boot` (the power-on decision), `committable`, `os_slots` (on the Linux harness: the `slots` object of `GET /boards/{bid}/slots`) and `notes`; and the reply's `line`, the Board tile's Card line (`n/a: <reason>`, `none (boots as always)`, or the store state, the default and the OS slots).
  - `keep_on_card: true` checks the card after the preflight and before any job: a `reason` refuses with 422 UNAVAILABLE (code 12, capability `keep_on_card`), `error.data.{overlay, card}`, and no job. The two plain reasons: `this harness has no microSD store` and `no card in the USER microSD slot`.
  - The job's result (a `DeployResult`) then carries `card: {kept, slot, why}`: `kept` true with the `slot` (`A`/`B`), or false with `why` (the card was pulled, a card write failed...). A failed card write never fails the deploy: the swap stands, and the card keeps the design it had. `card` is `null` when the deploy was not asked to keep. `deploy.done` carries the same `card`; `deploy.started` carries `keep_on_card`; the card write reports progress as phase `card`.
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
- **Job events:** `job.started`, `job.progress {job, phase, done, total}`, `job.done {job, result}` and `job.failed {job, error}`. SLOT-TIMING (additive): a long card job's `job.progress` (and `GET /jobs/{id}` `progress`) adds `slot`, `rate_bps`, `eta_s` and `text` ("writing slot B: 12.3 MB / 29 MB, ~6 min left").
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
| `POST /boards/{bid}/power/cycle` `{off_s?, force?, consent?}` | 202 job `power_cycle`; the result is the device's evidence. SLOT-TIMING: refused like `controller/reboot` while the card job runs (`force` + `consent: "RESET <bid>"`). |
| `POST /update/check` `{board_id?, source?, channel?, version?}` | 202 job `update_check`. The result is the check: the channel, the releases, the app update, and the board's plan with `fingerprint`, `mode`, `rekey`, `blockers`, `warnings`, `steps`. Read-only. |
| `POST /boards/{bid}/update/harness` `{fingerprint, rekey_phrase?, version?}` | 202 job `update_harness`. The plan is recomputed and must match `fingerprint`. A re-key needs `rekey_phrase == "REKEY <static_id>"`. The result is the outcome. |
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
| `GET /boards/{bid}/panel` | `{panel: PanelState or null, reason, identify: {available, reason, until}, support: {front_panel, presence, locate, source}, presence: {active, reason, sid, last_hello_at, sent, ridden, skipped, last_error, interval_s}}`. `?state=0`: the same without reading the board's panel (`panel` null; `reason` only when it cannot be read at all) |
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
| `GET /guide?pack=&static_id=&design=&build_dir=` | the guide: `{pack, static_id, board_id, kit_id, profile, vivado, design, build_dir, rm_id, steps, next, receipt, troubleshooting}`. Each step is `{id, n, title, state, detail, reason, actions: [{kind: "copy", text}], checks}`; `state` is `done`, `next`, `blocked`, `failed` or `unchecked`; ids are `target`, `tools`, `kit`, `wrapper`, `build`, `check`. `receipt` is the newest receipt in `build_dir` (`{path, state, stage, kit_id, ..., gates: [{gate, verdict, detail}]}`) or null. `troubleshooting` is `{gates: [{gate, fix}], checks: {name: fix}}`: a card for every gate of `build_rm.tcl`, in order, and for every check that can refuse (a pair check `partial: <name>` or `clearing: <name>` uses the card `<name>`; `xdc` covers every `xdc:<code>` without its own) (KIT-UI, additive). |
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
- **`mode`:** `m1` (HM's own hw_server: `-p0`, no `-d`/`-I`, one fixed port per board, and the XVC cable only: `auto-open-servers xilinx-xvc:127.0.0.1:R` and `jtag-port-filter Xilinx/XVC/127.0.0.1:R`, so no local USB JTAG cable is ever offered; Vivado connects to `url`, `localhost:H`) or `byo` (your own hw_server: `open_hw_target -xvc_url 127.0.0.1:R`, R being `relay_port`).
- **`reach`:** `hub-tunnel` (bare-metal behind a hub: the hub tunnel's 2542 forward), `board-ssh` (the Linux harness: `ssh -J HUB root@BOARD -L 127.0.0.1:p:127.0.0.1:2542`) or `direct`. boards.toml `xvc = { reach = "auto" }` chooses (`auto`, `hub`, `board-ssh`, `direct`).
- **`warnings`:** the bare-metal harness's XVC is open on every interface, so its status carries "XVC on this harness is unauthenticated: ..." until the Linux cutover (X6). A Linux harness carries the same warning until it reports the XVC lock (`xvc_lock`). Also: a swap closes the session and reopens it; `byo` reminds that your hw_server lingers 20 s.
- **`ltx`:** `{rm, static, full, preferred, note}`, each `{path, name, crc_ok, source, vivado}` or null. `preferred` is `full` when the mint staged a full-design file for the loaded RM (X5), else `rm`. `static` (the MIG view) is Linux only. `which` is `auto` (the preferred one), `rm`, `static` or `full`.
- **Refusals before any job:** 409 HELD while another job runs on the board; 409 HELD naming the holder when the board is behind a hub and the lease is not this client's (the lease holder only, and a hub that cannot be asked also refuses); 409 ALREADY when a session is open; 422 UNAVAILABLE when the image's XVC does not drive the Debug Bridge (`xvc_jtagbb`), the harness lacks `xvc_dbgbr`, or no hw_server is installed (use `byo`).
- **Gates:** `close` takes the board gate. `GET` routes never touch the board while a session is open; with none open, the first `GET /xvc` (and `tcl`, `ltx`) reads the board's identity once under the gate.
- **Swaps and leases:** `deploy.started` drops the board's slot and stops HM's hw_server before the swap; `deploy.done` (verified) re-attaches with a fresh hw_server on the same port and the new design's probes file; an unverified or failed swap closes the session with the reason. `lease.state` released, expired or lost closes it. Closing the board closes it.
- **Errors:** 400 USAGE for a bad `which`, `format` or `byo`; 404 ABSENT for no such probes file, or a board that is not open.

### SSH claim of a Linux harness (LINUX-CLAIM, `claim_api.py`)

The Linux harness ships **unclaimed**: key-only SSH with no key. The first `authorized_keys` it is sent (TFTP, net-protocol "TOFU first-key claim") claims it, and every later claim is refused. Once claimed, the harness refuses slot changes from anything but the board itself (S12), so Harness Manager reaches it over SSH, `ssh -J HUB root@BOARD`, with the host key pinned at claim time in boards.toml `boards.<b>.ssh.host_key`.

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/claim?refresh=` | `{board_id, claim}`: `claim` is `BoardInfo.claim` (null on bare metal). `refresh=true` asks the board now, through the hub when there is one |
| `POST /boards/{bid}/claim` `{confirm: true, key?, adopt?, replace_host_key?}` | 202 job `claim`; the result is `{board_id, claim}` with `claim.action` `claimed` or `adopted` |
| `GET /boards/{bid}/ssh?command=` | `{board_id, argv}`: the pinned `ssh [-J HUB] -l root BOARD [command]` command line. Nothing is run |

- **`BoardInfo.claim`** (also on `GET /boards/{bid}` and `info --json`, and only on a Linux harness: the key is absent on bare metal): `{state, claimed, host_key, route, user, source, checked_at, live, notes}`. `state` is `mine` (this Harness Manager claimed it, or adopted the claim, and the host key still matches the pin), `other` (claimed by a key this Harness Manager did not claim with: the board does not publish which), `unclaimed` or `unknown`. `claimed` is `{by, key_fp, at, mine}` or null. `host_key` is `{reported, pinned, match}` (`match` false = the board's key changed: SSH to it is refused). `claim_key` (additive, LINUX-ANSWERS) is identify's `ssh.key_sha256`, the claim's first key, when the image publishes it (null otherwise); for `other` it is also `claimed.key_fp`. `live` false means the state is the last check on record (a board behind a hub: identify is UDP and does not ride the SSH tunnel, so `info` never asks the hub; `refresh=true` does).
- **Never automatic.** `POST` without `"confirm": true` is 409 REFUSED before anything reaches the board. Nothing in Harness Manager claims a board by itself or re-claims one.
- **Refusals before the job:** 409 HELD while another job runs; 409 HELD naming the holder when the board is behind a hub and the lease is not this client's; 422 UNAVAILABLE on bare metal. In the job: 409 ALREADY when the board is already claimed (`adopt` pins a claim you made elsewhere), 409 REFUSED when the host key differs from the pin (`replace_host_key`, only for a re-provisioned board) or from the one the board publishes, or when your key does not log in.
- **Two locks, not one** (LINUX-ANSWERS). **The claim lock:** a harness refusal `slot locked: board claimed (use ssh)` is shown as REFUSED: "this board is claimed by <key fp>; this operation needs the claiming key (use `board claim` only if the board was re-provisioned)". **The fabric identity lock**, `identity lock: <reason>` (the card's image and the FPGA's static disagree, claimed or not), is a separate REFUSED (`IdentityLockError`), never INCOMPATIBLE: `mismatch` (`image … != fabric …`, `usr_access … != image …`) or `unknown` (no stage0 block, `fabric static_id unknown`, `image static_id not provisioned`). Its hint gives the Ethernet fix: push the image built for this static, `slot commit`, reboot. When the FABRIC is unknown the board refuses slot writes too, and the hint says to boot through stage0 again instead.
- **Events:** `board.claim` `{state, claimed, host_key, route, action?}` after a claim or a refresh.

### App self-update: apply with a restart, status and settings (OTA-D, `update_api.py`)

docs/design/HM_SELF_UPDATE.md §5 is the design; david's decisions U3 (notify, auto-stage, apply on a click), U4 (the same PTY paths and a re-attach notice, no fd handover) and U6 (the admin policy file) apply. Additive: the routes above are unchanged.

| Method and path | Returns |
|---|---|
| `GET /update/app` | `{running, pointer: {current, previous, installer, root}, versions, bad: {version: {reason, phase, at}}, staged: [versions], available, last_check, last_apply, policy, settings, effective: {auto, channel, check_interval_s, why}, dev_install, apply}`. `apply` is `{state: idle\|checking\|draining\|restarting, id, from, to, waiting_on: [{job, kind, board_id}], ...}`, with `last` for an apply that ended without a restart. Files only: no network. |
| `POST /update/app/apply` `{version?, confirm?, drain_timeout_s?, health_s?, stable_s?}` | 202 `{apply}`. `version` defaults to the newest staged version above the one that runs. The new version's `--self-test` runs, the daemon drains, then it restarts onto the new version on the **same port and token**. A helper checks `/health` (the new version within `health_s`, default 30 s, then the same pid for `stable_s`, default 10 s) and rolls back by itself otherwise. |
| `POST /update/app/cancel` | `{apply}`: ends a drain (jobs are accepted again). 409 HELD `error.data.reason: RESTARTING` once the restart began; 409 ALREADY when nothing is being applied. |
| `GET /update/settings` · `PUT /update/settings` `{channel?, auto?}` | `{settings: {channel, auto}, policy, effective}`. `auto` is `off`, `notify`, `stage` or `""` (the default, `stage`). The admin policy only tightens: `effective.auto` is the stricter of the two; a channel the policy does not pin is 409 REFUSED. |

- **`POST /update/app`** takes `stage_only: true` (and `catalog`, default `hm-app`): the job `update_app` runs OTA-C's `stage_app` (the click path) and never switches (the apply does that), so it is not refused while other jobs run. Its result is `{staged, version, channel, why?, skipped_bad?, notes?}`.
- **Refusals before anything happens:** 409 REFUSED for a version that is not staged, is marked bad, or a developer install or an admin `off`; 409 ALREADY for the version that runs; 409 HELD when another process holds a board or a harness update is unfinished (the pointer switch would be refused), or with `error.data.reason: APPLYING` for a second apply; 422 UNAVAILABLE when the server was not started by `harness-manager daemon`; 400 USAGE for `health_s` outside 5–600, `stable_s` outside 1–300.
- **Soft busy:** GDB on OpenOCD, an open XVC session, and a `screen` attached to a console PTY end with the restart. Without `confirm: true` the apply is 409 REFUSED with `error.data.reason: SOFT_BUSY` and `error.data.soft_busy: [{kind: gdb|xvc|screen, board_id, detail, name?, path?, clients?}]`.
- **Drain:** while an apply drains or restarts, every new job is 409 HELD with `error.data.reason: DRAINING` (and `version`, `from`, `apply`). Running jobs finish; short requests are served.
- **What survives the restart:** the port and token (an open app window reconnects by itself), the open boards (closed, then reopened with the same note), each console PTY that was open (the same path, a new `/dev/pts/N`; the daemon writes `[Harness Manager is restarting for an update to V; re-attach with `screen <path>` in a few seconds]` into it first), and hub leases (never released; the new daemon heartbeats them once the board reopens). GDB, XVC and the console WebSockets reconnect.
- **Bad versions:** a version whose apply failed its self-test or health check is marked bad in OTA-C's catalogue store (`<state_dir>/update/bad_versions.json`, catalogue `hm-app`) and in the pointer (`versions[V].state: "bad"`). It is never offered by a check (`app_skipped`), never staged or switched to again; a newer release is offered as usual.
- **The resume file** `<state_dir>/update/resume.json` (0600) carries the token to the next daemon (`python -m harness_manager.daemon --resume FILE`), never argv; it is deleted once read. `last_apply.json` beside it records `{id, from, to, result: applied|rolled-back|down|not-switched|refused, phase, reason, seconds}`.
- **The checker:** the daemon checks the `hm-app` catalogue 60 s after it starts, then every `check_interval` (policy; default 6 h, ±10 %), backing off from 5 min to 24 h while it fails; offline is quiet (`last_check.error_kind: offline`), a refused channel is logged every time (`refused`). It publishes `update.available` only when the offer changes, stages it with OTA-C's `stage_app` when the effective mode is `stage` (the job `update_stage`, which also sends `update.app.staged`; a busy service retries in 10 min), and never applies. `off` and developer installs: no check at all (no fetch, no event).
- **Events** (docs/CONTRACTS.md): `update.applying {id, phase: checking|draining|restarting|cancelled|failed, from, to, waiting_on?, eta_s?, reason?}`, `update.applied {id, from, to, seconds}` (from the new daemon, once the helper confirmed it), `update.rolled_back {id, from, to, phase, reason}` (from the restarted old daemon). `update.available` adds `notes`, `notes_url`, `staged` and `source: "checker"` when the checker sends it.

### Harness versions (HARNESS-CAT, `harness_api.py`)

docs/design/HARNESS_DISTRIBUTION.md is the design (§5, §8.2); david's decisions U1, U7, U11 (2026-09-24) apply. The catalogue is every signed release of the board pack's harness catalogue (`mps3-harness`) on the chosen channels, each with a verdict for the board. It is built on the update service: the same channel, planner, executor, consent and history as `update/*`.

| Method and path | Returns |
|---|---|
| `GET /harness/catalog?board_id=&channel=` | The catalogue the last refresh built for that board (`board_id` empty: the one built without a board). `{catalog, board_id, channels, board, offer, releases, rollback, warnings, at}`. `channel` keeps only that channel's rows. 409 REFUSED "refresh it first" when none is cached. |
| `POST /harness/catalog/refresh` `{board_id?, channel?, all?, source?}` | 202 job `harness_refresh` (a board job with `board_id`, else engine-wide). Fetches and verifies each channel (`all`: stable, beta and dev; one that cannot be read is a warning), plans every release for the board, caches the result and returns it. Emits `harness.catalog`. |
| `GET /harness/releases/{version}?board_id=` | One release from the cached catalogue: its row plus `identity`, `compat`, `component_list` (`{name, target, kind, size, sha256, ip_class, access, private, repo, cached, skipped, files}`), `board`, and, with `board_id`, `plan` (the plan summary with its `fingerprint`; this reads the board's identity). 409 without a cached catalogue; 404 for a version no cached channel lists. |
| `POST /harness/releases/{version}/fetch` `{channel?, source?, kit?}` | 202 job `harness_fetch` (engine-wide). Downloads and sha256-checks the release's parts into the update cache. The result is `{version, channel, components: [{name, target, kind, size, sha256, result, why, path}], fetched, cached, skipped}`; `result` is `fetched`, `cached` or `skipped` (a private part without a token; the kit without `kit`). |
| `POST /boards/{bid}/harness/install` `{fingerprint, version?, rekey_phrase?, channel?, source?, overlays_only?, via?, board_phrase?, auto_revert?}` | 202 job `harness_install`. The plan is recomputed for `version` (default: what the board is offered, its pin else the current release) and must match `fingerprint`. The result is the outcome. HUB-SD: `via` is `hub` or `usb` (default: the planner picks; a board with no Debug USB here goes via the hub when its pack offers the door). A plan via the hub needs `board_phrase` equal to the plan's (it names the board, the lease holder and the queue); `auto_revert` (default: the plan's, armed) writes the previous base back if the board stays dark. A `dark`, `auto-reverted` or `auto-revert-failed` result fails the job with `error.data.outcome`. |
| `PUT /boards/{bid}/harness/pin` `{version}` · `DELETE /boards/{bid}/harness/pin` | `{board_id, pinned, previous}` (`pinned: ""` after a DELETE). Emits `harness.pinned`. |
| `GET /boards/{bid}/harness/history?limit=` | `{board_id, history, pinned, rollback}`. `history` is newest first; `rollback` is the cached catalogue's candidates, or null before a refresh. |
| `POST /boards/{bid}/harness/rollback` `{to?, fingerprint?, rekey_phrase?, channel?, source?, via?, board_phrase?, auto_revert?}` or `{backup_path, wait_s?, via?}` | 202 job `harness_rollback`. `to` is `previous` (default) or a version: a re-install through the same plan, fingerprint and consent. `backup_path` restores that config-SD backup instead (T7's rollback). |

- **A row** (`releases[]`) is `{version, channels, channel, status, released_at, static_id, usercode, impl, fw_sha, harness, ver32, proto, vivado, rekey_in_channel, notes, notes_url, size, cached, components, marks, running, installed, pinned, verdict, verdict_text, why, needs, reasons, warnings, mode, rekey, consent_phrase, doors, touches_board, fingerprint, changes}`.
  - `verdict`: `fits`, `re-key`, `needs-door` (`verdict_text` "needs Debug USB or hub") or `incompatible`; `""` without a board. `why` is the reason in one sentence; `reasons` are the plan's blockers.
  - `needs`: any of `debug-usb`, `linux-slot`, `debug-usb-or-hub` (an OS image provisioned for another static: the Ethernet door carries only an image for the running static; LINUX-SLOTS), `newer-app`, `consent`, `hub-lease`.
  - `marks`: any of `running` (the board reports the release's wire identity, the firmware sha first), `installed` (this HM's last install on the board names it), `written` (written to the SD, not running), `pinned`, `current`, `offered` (what an install with no version gives), `past-pin`.
  - `changes`: what installing it changes: `static` (a re-key: every overlay and DUT RM keyed to the running static stops loading), `firmware`, `impl`, `proto`, `features`, `overlays`, `kit` (the kit's static and Vivado release), `sd_files`, `os_image`, and `summary` lines.
- **`board`**: `{board_id, pack, identity_known, running, running_release, installed, pinned, lease: {required, mine, holder, target, reason}, doors}`.
- **`rollback`** candidates, best first: `{version, source: history|channel, why, listed, status, static_id, installable, reason}`. `previous` is the release the last install replaced; a board with no history falls back to the channel's release before the running one. One the channel no longer lists is refused, never swapped for another.
- **Refusals before any job:**
  - 409 HELD naming the holder when the board is behind a hub and this client does not hold its lease (an install or rollback that writes or reboots the board; a board with no hub has no lease);
  - 409 REFUSED with `error.data.plan` when the fingerprint is missing (rollback: confirm this plan) or differs, on blockers, when there is nothing to do, and for a re-key without `rekey_phrase == "REKEY <static_id>"`;
  - 409 HELD while another job runs on the board.
- **Jobs:** a `written-not-running` outcome fails the job (exit 6) with `error.data.outcome`. `update.progress` for the board drives `job.progress`.
- **Events:** `harness.catalog`, `harness.installing`, `harness.installed`, `harness.pinned` (docs/CONTRACTS.md), and the `update.*` topics the executor publishes.
- **Also (additive to L4's routes):** `POST /update/check` and `POST /boards/{bid}/update/harness` take an optional `version`. A check with a version plans that release; the install recomputes the plan with the body's `version`, else the version of the check that issued the fingerprint. (Before, the install always planned the channel's current release.) A pin also applies to `update/check` with no version: a release past the pin is not offered.
- **Errors:** 400 USAGE for a bad version, `limit`, `backup_path`, `wait_s` or body; 404 ABSENT for a board that is not open, a version no channel lists or a missing backup; 422 UNAVAILABLE when the engine has no update service.

### Settings (SET-API, `settings_api.py`)

docs/design/SETTINGS.md is the design (§4, §5, §8, §12.8); david's decisions S1-S4 (2026-09-25) apply. One settings model (`harness_manager.settings`: SET-CORE's schema, resolver, files and secret store); these routes, `harness-manager config` and the Settings menu (SET-UI, after the cutover) are views of it. The routes and the CLI run the same code (`settings/ops.py`), so `config --json` prints these shapes. Additive: `GET`/`PUT /update/settings` are unchanged.

| Method and path | Returns |
|---|---|
| `GET /settings?section=&key=&all=` | `{schema_version, files: {config_dir, settings, boards, policy, secrets, secrets_backend: {backend, where, why}}, service: {demo}, policy: {path, exists, problems}, problems, sections: [{id, name}], instances: {hubs, boards}, rows}`. `section` is a section's id or name (`tools`, `harness-kits`, `Harness + kits`); `key` gives that one row (a hub or board that no file names yet resolves to its defaults); `all=1` adds the developer and test seams (`ui: false`). Hub and board rows are listed once per hub and board the files (and the policy's machine hubs) name, and a board's named tables once per name the file gives (`boards.<b>.hub.shares.<name>`). A pack row's default is the pack's (`source: pack`). |
| `GET /settings/schema` | `{schema_version, sections, rows}`: every declared row without a value (`hubs.*.url` stays a pattern), each loaded board pack's rows too (`BoardPack.settings()`, SET-PACK: `mps3.*` and `boards.*.<table>`; the engine's `settings_schema()`). A pack whose rows the schema refuses is logged and left out. A row: `{key, type, default, section, section_id, doc, scope, secret, apply, owner, env, env_rank, choices, readonly, lockable, ui, ceiling, advanced, pack, bounds, min_exclusive}`; a secret's `default` is null; `bounds` is `[min, max]` for a number whose check has a range (the menu's input limits), else null: inclusive, and either end may be null (no limit); `min_exclusive: true` means "more than `min`" (a float that must be more than 0: `[0, null]`; an int's is `[1, null]`). The check stays the judge. |
| `PUT /settings` | Body `{KEY: value, ...}`. **All or nothing:** every key and value is checked, and every file it touches parsed, before anything is written. `{rows, keys, apply, applies: {live, reopen, restart}}`. |
| `DELETE /settings/{key}` | Removes your value (back to the admin's default, the pack's or the built-in one). `{rows, keys, apply, applies, changed}`; `changed: false` when your files did not set it (nothing is written, no event). |
| `PUT /settings/secrets/{key}` | Body `{value}` (nothing else). Stores it: the OS keyring when this service can reach one, else a 0600 file (`secrets.py`). `{key, secret: {set, backend, where, reachable, why}, rows, keys, apply, applies, changed}`. |
| `DELETE /settings/secrets/{key}` | Removes it from the store (and a stray file copy). The same reply, with `secret.set: false`. |
| `POST /settings/test` | Body `{section, name?, table?}` (`kind` is accepted for `section`). Runs the section's tester (`settings/testers.py`): `{section, name, testable: true, passed, steps: [{step, ok, detail, hint}], why, ...}`. A tester that can take seconds (the hub tester's ssh round trip) is the job `settings_test` (engine-wide, 202 `{job}`; the result is the same report). A section without a tester answers 200 `{testable: false, passed: null, steps: [], why: "the <section> settings are not testable yet"}`. |

- **A row** (`rows[]`) is `{key, value, source, where, locked, shadowed, capped, apply, problems, type, section, section_id, scope, doc, default, choices, readonly, owner, advanced, secret?}`.
  - `source`: `lock` (the admin policy's `[lock]`; `where` is the policy file), `env` (`where` is `$VAR`), `user` (`where` is `settings.toml` or `boards.toml`), `machine` (the policy's `[default]`), `pack` or `default`. The order is SETTINGS.md §4.2: lock > env > user > machine > pack > default; `updates.channel` puts the variable below your file.
  - `shadowed`: `"$VAR"` when a variable in **the service's** environment hides a value you set (the menu says "overridden by $VAR in the service's environment"). `config get` also shows what the shell it runs in would use.
  - `capped`: why the admin policy lowered the value (`updates.auto`).
  - `apply`: what a change needs. `live` (read at each use), `reopen` (the next board open picks it up) or `restart` (the service must restart). A reply's `apply` is the strongest of its keys'; `applies` lists the keys by class. The service's readers go through `settings/runtime.py` (SET-WIRE): a `live` row applies at its next use (the files are re-read when they change, and `settings.changed` drops the cache; the update checker checks again at once for an `updates.*` key), a `restart` row keeps the value the service started with. A `--state-dir`/`--demo` service reads its own files only.
  - `problems`: a value that was skipped (a bad variable, a bad file value) and why. A lock that is not valid fails closed: the row stays locked at its default.
- **Values.** A JSON string is parsed as the command line gives it (`"90m"`, `"true"`, `"0x40"`, `"a,b"`); any other JSON value must already have the type (`5` for an int, `true` for a bool).
- **Secrets never come back.** A secret row's `value` is `{set}` and its `secret` is `{set, backend, where, reachable, why}`; no reply, event, error or log line carries the value. `backend` is `secret-service`, `kwallet`, `macos-keychain`, `windows-credential`, `file`, `env` (the variable is set), `inline` (a plug password still in boards.toml) or a reference (`file`, `gh`, `fpgahub-login`). `reachable: false` means it is stored where this service cannot reach (a keyring, from a service started over ssh).
- **Refusals** (nothing is written): 400 USAGE for an unknown key, a bad value, a hub share on `tty_00` (`boards.*.hub.shares.<name>`, any `…/tty_00`: "tty_00 is the MCC console; Harness Manager never shares it; the MCC is reached on the hub"; only `shares.mcc` may name the MCC's path, which `hub_mcc` reads; a file value is skipped with that problem, and `POST /hubs/adopt` refuses a board that has one), a developer seam (`owner: dev`, `ui: false`: its variable only, SET-WIRE; a value in the file is ignored with a problem), a board's `match` or `name` in `[boards.defaults]`, a `*` in a key (name the hub or board), a secret through `PUT /settings` (use `/settings/secrets/{key}`), a non-secret through `/settings/secrets`, a secret that is not one line, or a file that does not parse (it is never overwritten); **409 REFUSED** for a key the admin policy locks, naming the policy file; 400 for a test with no section, or a hub test with no `name`.
- **Keys in the path** are URL-encoded; a board key keeps its TOML quotes (`boards."mps3@192.168.10.101:6900".name`).
- **Events** (docs/CONTRACTS.md): `settings.changed {keys, apply, applies, source: "api"}` after each change that wrote something, including `PUT /update/settings` (`keys: ["updates.channel"?, "updates.auto"?]`, its reply unchanged). It never carries a value. The daemon log records `settings.changed` with the keys, never the values.
- **The Host allow-list** (see "Process and security") applies to these routes as to every other.
- **The mock** (`tests/fakes/settings_mock.py`) serves these routes with this code, over a real resolver in a temporary directory.
- **The demo** (`daemon start --demo`, `app --demo`; SET-UI-MERGE): `service.demo` is true. Its settings are its own state dir's (`files.config_dir`); a secret goes to its own 0600 files, never the OS keyring (the keyring keys a secret by its name, so a demo secret would replace, and its Remove delete, your real one); and it reaches no hub: `POST /settings/test {section: "hubs"}` and `POST /hubs/{name}/boards` are 422 UNAVAILABLE ("the demo reaches no real hub") before any job. The demo pack declares the MPS3 rows, so the demo's schema has them.
- **Tools Detect (SET-UI):** `POST /settings/test {section: "tools", name?}` runs `settings/tooltest.py` as the job `settings_test`: per tool (`openocd`, `vivado`, `hw_server`, `uv`, or the one named) the executable the setting names (or the tool's own search), proven with a version probe only (`openocd --version`, `uv --version`, `vivado -version`; hw_server is never run, its release comes from its path). OpenOCD also lists its adapters (`openocd -c "adapter list" -c shutdown`, DEBUG-OCD) and passes only with remote_bitbang; with no setting, the first `openocd` on the service's PATH that has it is taken, as `debug up` does. The result adds `tools: {<tool>: {path, version, how, key}}` (`openocd` adds `adapters`); a path that does not run is a failed step. Nothing is written.

### Hubs in the Settings dialog (SET-UI, `hubs_api.py`)

SETTINGS.md §6 and §8: the hubs as the Settings dialog's Hubs section manages them. The work is SET-HUBS' `settings/hubs.py` and `hubtest.py` (what `harness-manager hub` runs); these routes only give it HTTP. Test connection is `POST /settings/test {section: "hubs", name}` (above). Nothing here takes, joins or releases a lease, and no reply carries a token.

| Method and path | Returns |
|---|---|
| `GET /hubs` | `{hubs: [{name, transport, machine, policy, locked, token: {set, backend, where, reachable, why}, token_ref, problems, host, group, jump, holder, url, ca_file, cert_file, key_file, insecure, events, direct, timeout_s, lease_ttl, request_ttl, queue_timeout, sources, boards, targets_used}], inline: [{board, host, url, via, name}], policy: {path, hubs}}`. `machine`: the admin policy defines it (every key it sets is locked; the token is still yours). `boards`: the `boards.toml` boards whose `hub.use` names it; `targets_used`: each one's target. `inline`: boards with an inline `hub` table, each a "Make this a hub" candidate (`name` is the name it would get). |
| `PUT /hubs/{name}` | Body: hub keys (`transport`, `host`, `url`, `group`, `jump`, `holder`, `lease_ttl`, ...; values as `PUT /settings` takes them). Adds the hub, or changes the keys given when it exists; all or nothing, and a hub that could not be used (SSH without a host, REST without a url, plain http off-box) is 400 with nothing written. `{hub, created, keys, apply}`. 409 REFUSED for a machine hub. The token is a secret: `PUT /settings/secrets/hubs.{name}.token`. |
| `DELETE /hubs/{name}?force=` | Removes `[hubs.<name>]` and its stored token: `{removed, boards}`. 400 while a board uses it (the message names them) unless `force=1`; 409 for a machine hub; 404 for no such hub. |
| `POST /hubs/{name}/boards` | Body `{target, board?, name?}`. The job `hub_add_board` (engine-wide, 202 `{job}`): reads the target's facts (`GET /targets/{t}` over REST, `fpgahub target show` over SSH; a read), then writes a `boards.toml` entry `{via: "hub", hub: {use, target}, name, match}`: no share (MCC-FIX: nothing ever shares the MCC's `tty_00`; the MCC of a hub board runs on the hub). The result is `{board, hub, target, match, name, path, notes, keys}`; a hub that will not say the target's address writes the board without it, with a note. 400 for a key or target that exists. |
| `POST /hubs/adopt` | Body `{board, name?}`. "Make this a hub": the board's inline `hub` table becomes `[hubs.<name>]` plus `hub.use` (`boards.toml` copied to `boards.toml.bak-<date>` first; comments kept). `{board, hub, created, reused, via, changed, backup, notes, keys, apply}`; `changed: false` when the board already names a hub. |

- **Events:** each change publishes `settings.changed {keys, apply, applies, source: "api"}` with the keys it wrote (never a value), as the settings routes do.
- **The mock** serves these routes with this code (`tests/fakes/settings_mock.py`).

### User microSD and OS slots (LINUX-SLOTS, `card_api.py`)

The Linux harness's OS slots A/B on the board's user microSD (net-protocol v0.14 "Slot images"). The card itself is `GET /boards/{bid}/card` (Keep on the card, above), which LINUX-SLOTS extends additively. Read-only here: the changes (`harness-manager slot push|commit|rollback`, `card commit|clear`) run in the CLI, where Harness Manager asks for the lease and a confirm. Those are Harness Manager's rules: the board has no lease and no confirm on slot acts (unclaimed, any peer may change the slots; claimed, only the board itself, through its SSH). This route only reads `slot status`: it never starts a `verify`, which holds the card job for minutes.

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/slots` | `{available, reason, slots}`. `slots` is `{card, running, default, target, staged, fabric_sid, seq, pending_commit, slots: {A, B: {state, hdr_crc, len, sid, verified, err, image_sha256, version, running, default}}, job: {act, slot, state, got, len, err, busy, rate_bps, eta_s, text}}`. SLOT-TIMING (additive): `busy` is `writing`/`verifying` (nothing may reset the board meanwhile), `rate_bps`/`eta_s` this service's estimate (the observed rate once the bytes move, else `mps3.slot.card_write_bps`/`card_read_bps`), `text` the one line. |

- **Not available is not an error:** a harness without OS slots (bare metal; a Linux harness with no card or in stage0 rescue) answers 200 with `available: false` and the `reason`. Nothing is sent to the board beyond `version` then.
- **What the boot means (LINUX-ANSWERS, additive keys in `slots`):** `fell_back` is the default slot that failed to boot (stage0 went back to the other one and the default stays on the bad slot until `slot rollback`; null otherwise); `committed_unbooted` is rule 1's slot, never a fallback; `confirmed` and `claimed` are the board's own fields when it sends them, else null; `notes` says it in words ("slot B failed to boot; A is running; roll back to make A the default"); each slot's `boot` is "read back this boot", "booted (not yet confirmed)" or "booted, confirmed healthy" (`verified: boot` alone is never called confirmed).
- **Errors:** 404 ABSENT for a board that is not open; 409 HELD while a job runs on the board (the board gate); 7 UNREACHABLE when the harness does not answer.
- **The reset guard (SLOT-TIMING, `services/reset_guard.py`).** While the card job is `writing` or `verifying` (B2 silicon: a reset mid-job wedged the card), every reset is refused HELD with the job named ("MCC REBOOT refused: slot B is being written (12.3/29 MB); a reset now can wedge the card. Wait ~6 min."): `controller/reboot`, `controller/command` REBOOT (409 at once), `power/cycle`, `deploy` and `restore` (the job fails), the harness's own reboot, and the update's reboot step (it waits for the job instead). Only the MCC REBOOT and the power cycle take `force` + `consent`. The card line (`GET /card` `line`) is the job's `text` while it runs.

### Live display: the LCD mirror (LM3, `display_api.py`)

docs/design/LCD_MIRROR.md is the design (§7.2-§7.4); david's decisions D3 (only the lease holder sees the live picture) and D4 (no touch pass-through) apply. The board side is the pack hook `BoardPack.display_adapter(session)` (lane LM2; default `session.display`): an adapter, or none when the board has no live display. `harness_manager.services.display.DisplayService` keeps one upstream per board, shared by every tab, the PNG and the CLI. The adapter and the compositor share the hub API's lease service, as XVC does.

| Method and path | Returns |
|---|---|
| `WS /boards/{bid}/display/ws?token=&ack=1&rate=` | text frames: the status (below), first and then whenever its state, reason, badges, owner, mode or rate change; binary frames: one UPDATE each, in the board's own layout (LCD_MIRROR §6.1, `core.display_wire`: the 8-byte header, then the tile records exactly as the board encoded them). The client sends `{"ack": seq}` after drawing, and `{"rate": hz}` (0-255) for another pace |
| `GET /boards/{bid}/display` | `{available, unavailable, state, reason, mode, hello, owner, flags, regs, badges, presented, hatched, seq, t_ms, frames, resets, rtt_ms, rate, rate_asked, fps, bytes_per_s, viewers, counters}` |
| `GET /boards/{bid}/display.png?scale=&hatch=&format=` | the presented picture: `image/png`, `scale` (1-4, default 1) x `scale` pixels per panel pixel, `hatch=1` (default 0) greys the tiles that are not VALID. `format=raw`: 153,600 bytes, 320x240 RGB565 little-endian, row-major (`X-Display-Width: 320`, `X-Display-Height: 240`, `X-Display-Format: rgb565le`) |

- **The status.** `state` is `down`, `connecting`, `syncing` (waiting for a whole keyframe), `live`, `stale` (no answer to PING for 3 s), `reconnecting` or `refused` (the board's two client slots are full); `reason` says why. `mode` is `sw` (the harness's software tap: blind while the DUT owns the panel) or `hw` (the static shell's snooper, mint 4), `""` before HELLO. `hello` is the board's HELLO (`{proto, w, h, fmt, tile, mode, static_id, max_msg, ...}`) or null. `owner` is `harness`, `dut` or `unknown`. `badges` are `[{key, level, text}]` (§7.4: `blind`, `held`, `backlight_off`, `display_off`, `standby`, `viol`, `approx`, `fmt`, `inexact`; `level` `grey`, `held`, `dim` or `warn`). `hatched` counts the tiles that are not VALID. `rate` is the board's own clamp of `rate_asked` (the highest any tab asked). `available` false with `unavailable` (the reason) when this client may not open the picture now; the route itself never opens anything, and answers 200 either way.
- **The socket.** `ack=1` (the default): each tab has ONE message in flight until it acks, and its next message holds the latest record of every tile it lacks. A tab that stops acking gets nothing more and is never fed stale tiles; the others never wait for it. `ack=0` has no flow control (tools and tests only). The first binary frame is a keyframe (every held tile, with REGS); every frame is one whole SNAP (`snap_last`). Text frames from the client that are not `{"ack": seq}` / `{"rate": hz}` JSON get `{"error": {...}}` (USAGE) and the socket stays. Binary frames from the client are ignored (D4: no touch pass-through).
- **The still.** `display.png` opens the upstream when none is open and waits up to 10 s for a keyframe: 422 UNAVAILABLE with the state and reason when none came. Both formats carry `X-Display-Seq`, `X-Display-Hatched` and `X-Display-Owner`; the PNG adds `X-Display-Scale`.
- **The upstream.** Opened by the first viewer (a socket or a PNG); closes 30 s after the last viewer leaves (a returning viewer reuses it), and at once when the board closes. When it ends for good (the board closed, the lease released, expired or lost, the claim lost, a source that refuses for good) each socket gets the final status (`state: down`, `reason`) and a close with the reason: 4000 + the exit code when the source's own error ended it (4004 when the lease turned out to be someone else's at the connect), else 1000. Reconnect to try again: the refusal is checked again. A `display.png` whose upstream ended that way answers with the same error (409 HELD naming the holder).
- **Refusals, before anything attaches or opens:** 401 with no or a wrong token (the socket's is an HTTP denial, as every socket's); 404 ABSENT for a board that is not open (socket: close 4003); 400 USAGE for a bad `scale`, `hatch`, `format`, `ack` or `rate`, and for `format=raw` with a scale or a hatch (socket: close 4002); 409 HELD naming the `holder` (`nobody` when no one holds it; `error.data.capability` is `display_mirror`) when the adapter says no and the board is behind a hub whose lease is not this client's (D3; the hub API's lease view); otherwise 422 UNAVAILABLE, capability `display_mirror`, when the pack has no live display for the board or its adapter says why not (the bare-metal harness, an image without `lcd_mirror`, an unclaimed board). The message is the adapter's first reason, in the order a user fixes them. The socket's refusal is a text frame `{"state": "refused", "reason", "error"}`, then a close with 4000 + the exit code (4004, 4012) and the reason. The Front panel card keeps its text mirror in every such case.
- **No board gate.** The mirror is its own connection (an SSH forward to the board's lcd_mirror service), so it runs through a job: a deploy's repaint shows live.
- **permessage-deflate is never negotiated** on any of the daemon's WebSockets (uvicorn `ws_per_message_deflate` off): on loopback it bought nothing and cost about 9x the daemon's CPU per display message.
- **Events:** `display.state` `{state, mode, owner, badges, reason}` whenever one of them changes (docs/CONTRACTS.md).

## Board names (lane N1, additive; CCR N1-1 to N1-4)
- **`Candidate` adds `name` and `name_source`.** They appear wherever a candidate does: `POST /probe`, `GET /boards` rows, `POST /boards` and `GET /boards/{bid}` (`info.candidate`), and the CLI's `probe --json` and `info --json`. `name` is the display name (`"mps3-01"`), and `""` means the board has none, so show the address. `name_source` is `config` (boards.toml `name`), `harness` (the board reports it), `hub` (the fpgahub board that owns the hub target, as the hub reports it or boards.toml `hub.board` states it) or `hub-target` (the same, derived from boards.toml `hub.target` by fpgahub's suffix rule with no hub call). The first of these that gives a name wins, in that order; `harness_manager.naming` holds the rule.
- **A name is display only.** It never keys a board: `board_id` does, and so do boards.toml tables, session locks and leases. A hub id is shown with `_` as `-` (`mps3_01` becomes `mps3-01`).
- **`BoardIdentity` adds `name`:** what the harness reports, `""` today (no fielded firmware sends it).
- **An open board can be renamed by `GET /boards/{bid}`.** When the harness or the hub gives a better name than the probe had, `info.candidate` carries it, and `GET /boards` shows it from then on. The first `info` of a board behind a hub asks the hub once (`fpgahub board list --json`, read-only) and caches the answer for an hour in that process. Set boards.toml `hub.board` to skip that call.
- **Events:** `board.found` and `board.identity` add `name` and `name_source`.
- **TSV:** `probe` and `info` append a `NAME` column.
- **The web UI** shows the name in the rail, in the header (with the design and shell under it), on the preview card and in the window title (`mps3-01 · Harness Manager`). A board with no name shows its address, as before.
