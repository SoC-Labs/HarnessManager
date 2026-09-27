# Settings in Harness Manager: design

**Lane:** SETTINGS (design + one board-free spike). **Date:** 2026-09-25. **Base:** HM `main` c5e1f79.
**For:** david, who needs to decide §10. Nothing here is wired into the app.
**Spike code:** `tests/spikes/settings_*.py` (never collected). **Evidence:** `docs/assessment/settings_spike_2026-09-25/spike_output.txt` (44/44 PASS).
**On main (2026-09-26):** david decided §10 (D1-D4 here; the code cites them as S1-S4), and SET-CORE, SET-PACK, SET-HUBS and SET-API built it: `harness-manager config`, `harness-manager hub` and the `/settings` routes are its views; the Settings menu (SET-UI) is not on main yet. This file is the design as written on 2026-09-25, so "nothing here is wired" above is that day's state. The spike code stays on branch `team/settings-design`; only its evidence is on main.

Sources are cited as `path:line` in this repo, unless marked otherwise. `plat:` means the mps3-nanosoc-platform checkout, and `lx:` means its `feat/linux-harness` worktree.

david asked: *"Can you get agents to look at a settings menu for harness manager. Things like an FPGAhub server will need to be configured with it (optional)? What other mechanisms will need to be configured through a settings menu?"*

---

## 0. Recommendation

**Build one settings model, and let the Settings dialog, `harness-manager config` and the API all be views of it.**

- **One declaration per setting.** Each setting is declared once, as a schema row: its key, type, default, scope, whether it is a secret, and whether a change needs a board reopen or a service restart. Every setting also has one resolver with a fixed precedence: **admin lock > developer env > the user's file > the admin's machine default > pack default > built-in default**.
- **Pack settings.** A board pack declares its own rows (`BoardPack.settings()`), so KR260 and HAPS add rows without adding UI code.
- **Hubs become first-class.** A hub is its own `[hubs.<name>]` entry with a "Test connection" button. Boards refer to a hub by name, and no hub at all stays the zero-config default.
- **Secrets.** Secrets go to the OS keyring when the process can reach one, otherwise to a 0600 file. A small index records which of the two holds each secret, so a service started over ssh says "stored in your keyring, which this session cannot reach" instead of "not set". The API only ever says *set / not set / where*.
- **The UI.** The existing Settings dialog (UPDATE-UI's `SettingsModal`) grows a left-hand section list, like the Help modal. The Updates card moves into it unchanged.

**Size:** six lanes, about 55 h in total. The core and the hubs work come first and are board-free. §11 has the lane plan.

## 1. What exists today

Configuration is spread across nine places. None of them has a UI, except the update channel and mode.

| # | Where | What it holds | Written by | Cite |
|---|---|---|---|---|
| 1 | `~/.config/harness-manager/boards.toml` | Per board: `match`, `name`, `via`, `hub = {…}`, `power`, `sysmon`, `estimates`, `xvc` | hand only (no writer exists) | `src/harness_manager/power/config.py:1-30,157-185` |
| 2 | 31 `HARNESS_MANAGER_*` env vars read in `src/` (33 names; `SYSMON`/`SYSMON_ERR` are output markers), plus `FPGAHUB_*`, `XILINX_VIVADO` | tools, ports, sources, test seams | the shell | §2 |
| 3 | `<state>/update/settings.json` | `{channel, auto}` | `PUT /update/settings` | `src/harness_manager/services/update/selfupdate.py:115-155`, `daemon/update_api.py:377-396` |
| 4 | `/etc/harness-manager/policy.toml` (and the macOS/Windows paths) | admin: `self_update`, `channel`, `check_interval` | the admin | `src/harness_manager/services/update/policy.py:48-56,119-158` |
| 5 | `<install root>/install.json`, `install.conf` | venv, version, extras, uv | installer | `scripts/install.sh:62-65,550-559` |
| 6 | `~/.config/fpgahub/config.toml` | an fpgahub login (addr, token, tls_dir) | `fpgahub login` | `src/harness_manager/transports/hub_rest.py:388-394,429-449` |
| 7 | the browser's `localStorage` / `sessionStorage` | theme, dismissed notices, the daemon token (per tab) | the web UI | `web/static/js/theme.js:5,28-31`, `api.js:122-142` |
| 8 | CLI flags that act like settings | `--via`, `--holder`, `--ttl`, `--byo`, `--channel`, `--source`, `--state-dir`, `--port`, `--listen` | per command | `cli/cmd_hub.py:291`, `cli/cmd_daemon.py:62-64`, `daemon/server.py:414-429` |
| 9 | constants that a user would plausibly want to change | lease TTL 3600 s, console pace 20 ms, identify 10 s, cache caps (planned) | code | §2 |

**Facts that shape the design:**
- **Nothing writes `boards.toml`.** There is no TOML writer in the tree. `tomli_w` appears only in a test recorder, `tests/fakes/t8_record_fpgahub_golden.py:22`.
- **The file is re-read on every lookup** (`harness_manager_mps3/hub.py:170-175`), so a change reaches the next board open without a restart.
- **A board's name** in `boards.toml` already outranks the harness and the hub (`naming.py:11-18`). No route edits it, and the UI only displays it (`docs/API.md:370-378`).
- **The admin policy only tightens.** A channel pin is refused with 409 (`policy.py:80-88`), and a mode above the cap is stored but capped when read (`selfupdate.py:158-174`). No env var can move the policy file or turn it off (`policy.py:23-26`).
- **No event fires when a setting changes.** `PUT /update/settings` publishes nothing. Other tabs only see the change through the 60 s poll (`selfupdate.js:28,314-316`).
- **Secrets already have three conventions:**
  - the power plug: `password`, `password_env` or `password_file` (`power/config.py:271-302`);
  - the hub: `token_file`, then `$FPGAHUB_TOKEN`, then the fpgahub login store (`hub_rest.py:429-449`);
  - GitHub: `$HARNESS_MANAGER_GITHUB_TOKEN`, then `gh auth token` (`update/github.py:154-180`).

  None of the three uses a keyring. The daemon's own token is in `daemon.json` at 0600 (`daemon/state.py:103-109`).
- **The CLI and the service can see different env vars.** The service keeps the environment it was started with, and a CLI verb that goes through the service cannot pass its own (`cli/engine.py:14-18`). A setting that lives only in an env var can therefore differ between the CLI and the GUI.

## 2. Inventory

The full table is in **Appendix A**: **106 settings that exist today**, plus 12 that other lanes are planning. Every row cites its source. The counts by group:

| Group | Rows | In the menu | Env / dev only | Secret | Where it lives today |
|---|---:|---:|---:|---:|---|
| General (theme, app window) | 5 | 3 | 2 | 0 | localStorage, env, const |
| Hubs (fpgahub over SSH and REST, lease defaults) | 18 | 16 | 2 | 3 | boards.toml `hub`, env, fpgahub login, const |
| Boards (route, hub use, shares, power, XVC reach, telemetry tables, console rate) | 23 | 23 | 0 | 1 | boards.toml, runtime only |
| Tools (OpenOCD, Vivado, hw_server, uv, gh) | 7 | 6 | 1 | 0 | env, PATH |
| Updates (channel, mode, source, mirrors, token, install record) | 12 | 7 | 5 | 1 | settings.json, env, policy.toml |
| Harness versions and kits (channels, caches, sources, overlays, trust) | 9 | 9 | 0 | 0 | flags, env, const, none yet |
| Debug (port bases, hw_server mode) | 6 | 3 | 3 | 0 | env, flags, const |
| Consoles and pacing | 6 | 4 | 2 | 0 | const, React state |
| Presence and panel | 3 | 2 | 1 | 0 | const in three places |
| Advanced (service, state dir, logs) | 7 | 4 | 3 | 0 | env, flags, const |
| Developer and test seams | 10 | 0 | 10 | 0 | env, hidden flags |
| **Exists today** | **106** | **77** | **29** | **5** | |
| Planned by other lanes (Linux SSH claim, on-board OpenOCD, SD door, D13 persist, hold, hub data plane, U2) | 12 | 11 | 0 | 1 | none yet |
| **All** | **118** | **88** | **29** | **6** | |

**What users need in a menu (77 rows today).** Mostly hubs, boards, tools and updates, plus a few defaults that are constants today: lease TTL, console pacing, identify seconds, line ending. Five of the 77 are read-only (state dir, install record, harness pin, trust keys, `gh`).

**What stays env-only (29 rows).** These are developer and test seams: identify port and broadcast, push and TFTP ports, `TUNNELLED`, `CLI_ENGINE`, `NO_DAEMON`, `DEBUG`, `UPDATE_FIRST_CHECK_S`, `GITHUB_API`, `PTY_DIR`, `USE_INSTALLED`, `NO_SELF_UPDATE`, `--pack-overrides`, and similar. They are still declared in the schema (`ui = false`), so `harness-manager config list --all` shows them together with their current source.

**What is not an HM setting at all:**
- Board-side WireGuard keys (`MPS3_WG_*`, a build seam in the platform image: `plat:src/linux_harness/sw/br2_external/board/mps3_provision.sh:20-31`).
- The pinned release keys, which are compiled into the app and empty until U2 is decided (`services/update/trust.py:83`, `docs/KEYS.md`).
- The hub admin's own knobs (`gate_ethernet`, `share_tty`), which belong to fpgahub.

The menu shows the pinned keys **read-only**, with their fingerprints.

## 3. The model

### 3.1 A setting is a schema row

```python
Setting(key="hubs.*.url", type="url", default="", section="Hubs", scope="hub",
        env="", restart="reopen", secret=False, lockable=True, ui=True,
        help="The hub's API, https://HUB:7246", check=validate_hub_url)
```

| Field | Values | Why |
|---|---|---|
| `type` | `str int bool enum path url duration list secret ref` | The UI picks the control from it, and the CLI parses with it |
| `scope` | `user` · `machine` · `board` · `hub` · `pack` | Where the value lives, and what `*` expands over |
| `restart` | `live` · `reopen` · `daemon` | What a change needs. `live` means it is read at each use (most settings; `boards.toml` already is). `reopen` means the next board open picks it up (routes, hubs). `daemon` means a service restart (port bases, the state dir, listen, port). |
| `env` | the developer override | Always shown when it is in force (§4.2) |
| `lockable` | may the admin policy lock or preset it | Theme, and the state dir, are not lockable |
| `ui` | `false` = env-only (a dev or test seam) | Kept out of the menu; `config list --all` shows it |

The spike's `tests/spikes/settings_model.py` has 22 rows of the core schema and 2 pack rows. The lane writes out all of Appendix A.

### 3.2 Packs declare their own rows (KR260, HAPS)

`BoardPack.settings() -> Sequence[Setting]` is optional, and every key sits under the pack's prefix: `mps3.console.pace_ms`, `mps3.identify.port`. Per-board pack tables become board-scoped rows owned by the pack, for example `boards.*.xvc.reach` and `boards.*.power.kind`.

The pack still validates its own tables, as today (`hub.py:131-165`, `xvc.py:111-135`, `power/config.py:225-268`). The schema only lets the UI draw them and the resolver report where each value came from.

Today there is no such hook: `core/pack.py:377-395` has four methods, and the pack reads raw tables through `hub.board_tables()` (`hub.py:170`). This is **CCR SET-PACK-1** on `core/pack.py`.

## 4. Storage and precedence

### 4.1 Files

| File | Holds | Who writes it | Mode |
|---|---|---|---|
| `<config>/settings.toml` **(new)** | app-wide settings (`[general]`, `[tools]`, `[updates]`, `[kits]`, `[debug]`, `[consoles]`, `[advanced]`) and **`[hubs.<name>]`** | the menu, `config set`, you | 0600 |
| `<config>/boards.toml` (kept) | per board, as today, plus `hub.use = "<name>"` | the menu (round-trip, comments kept), you | 0600 when it holds an inline password (warned today, `power/config.py:305-316`) |
| `<config>/secrets/` **(new)** | `index.json` (which backend holds each secret; never a value) and the 0600 fallback files | the secret store | 0700 dir, 0600 files |
| `/etc/harness-manager/policy.toml` (kept) | the three U6 keys, plus new **`[lock]`**, **`[default]`** and **`[hubs.<name>]`** tables | the admin | root-owned, read-only to HM |
| `<state>/update/settings.json` | **migrated** into `settings.toml [updates]` on first write; read as a fallback for one release | | |

`<config>` is today's state directory: `$HARNESS_MANAGER_STATE_DIR`, else `~/.config/harness-manager` (`engine.py:79-86`). `settings.toml` follows the same rule as `boards.toml`, so every test that sets `HARNESS_MANAGER_STATE_DIR` stays hermetic.

Splitting config from state and cache under XDG (the store's blobs sit in `~/.config` today) is a separate question and is not needed here.

**Why keep `boards.toml` instead of merging it.** HM's own docs and the platform's Linux runbooks cite it (`docs/HIL_B0.md` §0, `docs/HUB_MODE.md`, `plat:docs/planning/B0_RUNBOOK_LINUX.md`, `plat:docs/planning/B1_RUNBOOK_LINUX.md`). Unknown pack tables pass through it untouched (`power/config.py:202`). Merging it in the week of the Linux cutover buys nothing.

**Why hubs move to `settings.toml`.** A hub is shared by boards. Today the lab board says `mapstone-dev.ecs.soton.ac.uk` twice, once in `via = "ssh:…"` and once in `hub.host` (`~/.config/harness-manager/boards.toml`, shape only).

**The writer.** Use `tomlkit`, a pure-Python round-trip editor that keeps comments and order. Hand-written comments are what makes `boards.toml` readable, and a writer that drops them would make users stop trusting the menu. Writes are atomic (tmp + fsync + `os.replace`, as `selfupdate.write_json` already does) and are serialised by a lock file in `<config>`. When the service is running, the CLI writes through the API instead of the file, so there is one writer and the event fires.

**Migration. Nothing moves until something is written.**
1. **Reading.** An inline `hub = { host = …, target = … }` keeps working forever as an anonymous hub. `via = "ssh:HOST"` keeps working.
2. **The Boards section** shows a board with an inline hub with a **"Make this a hub…"** button. The button writes `[hubs.<name>]` (default name: the host's first label, `mapstone-dev`) and rewrites the board's table as `hub = { use = "mapstone-dev", target = "mps3_01_pl", shares = {…} }`. It keeps every per-board key and every comment. It replaces `via = "ssh:<the same host>"` with `via = "hub"` only when the two hosts match.
3. **`settings.json`** is folded into `[updates]` on the first `PUT` and left in place, so a rollback to an older HM still reads it.

Before any rewrite, `boards.toml` is copied to `boards.toml.bak-<date>`.

**Which keys go where.** Per-board keys stay in the board's `hub` table: `target`, `board`, `shares`, `baud`, `start_shares`. Per-hub keys move to `[hubs.<name>]`: `host`, `url`, `group`, `jump`, the token, `ca_file`, `cert_file`, `key_file`, `insecure`, `events`, `direct`, `timeout_s`, `holder`, `lease_ttl`. A board table that has both `use` and a per-hub key is refused, naming the key. The alternative, a silent override, is how a lab ends up with two definitions of one hub.

### 4.2 Precedence

**The order, highest first:** policy `[lock]` > env > user file > policy `[default]` > pack default > schema default. After that, a policy **cap** can lower an ordered choice. Today `self_update` caps `updates.auto`.

- **The lock is above env.** This keeps the existing rule that a user cannot step around the admin (`policy.py:23`). An invalid lock **fails closed**: the setting stays locked at the default, and the problem is shown (spike S1.10b).
- **Env is above the user's file**, because tests and CI need a hermetic override that never edits a person's files. git, pip and uv all do the same, and every one of HM's 31 variables is used that way today. The cost is that a change in the GUI can be silently shadowed, so the resolver returns `shadowed = "$HARNESS_MANAGER_OPENOCD"` (S1.3). The menu shows the row as **"overridden by $X in the service's environment"** with the stored value greyed out, and `config get` says the same. A bad env value is skipped with a problem instead of failing the whole menu (S1.9).
- **The service and the shell can disagree.** The service's environment is the one that counts for the GUI. `harness-manager config get KEY`, run from a shell, prints both "the service uses …" and "this shell would use …" when they differ. That closes the gap `cli/engine.py:14-18` describes.
- **The policy `[default]` is below the user**, so an admin can pre-set a lab machine (its hub, its mirror, its Vivado) without taking the choice away (S1.6, S1.7).
- **Today's three policy keys keep their meaning:**
  - `channel` becomes a lock on `updates.channel`;
  - `check_interval` becomes a lock on `updates.check_interval`;
  - `self_update` becomes a cap on `updates.auto` (S1.8).

  An existing `policy.toml` needs no edit.

**Board-scoped values** resolve per board. For `boards.<key>.<field>` the order is: the board's own table, then a `[boards.defaults]` table (new, optional), then the pack default. Hub-scoped values resolve per hub, in the same way.

### 4.3 Admin locks in the UI

A locked row shows a lock icon, the value, and **"set by your administrator in /etc/harness-manager/policy.toml"**. This is the same wording UPDATE-UI uses (`selfupdate.js:29,564-567`), so one test style covers both. A `PUT` to a locked key is 409 REFUSED and names the file (S1.5). A policy `[hubs.lab]` table shows as a **machine hub**: locked, but the user still sets their own token for it. That is the lab-machine case: the admin names the hub, and each person brings their own credential.

## 5. Secrets

**Which settings are secrets:**
- `updates.github_token`;
- `hubs.<name>.token`;
- the passphrase of an mTLS key (only if the key is encrypted);
- `boards.<key>.power.auth.password`;
- the planned harness catalogue token, which is the same GitHub token (`docs/design/HARNESS_DISTRIBUTION.md:168,174`).

**Where a secret comes from, highest first:**
1. The env var (`$HARNESS_MANAGER_GITHUB_TOKEN`, `$FPGAHUB_TOKEN` with its `FPGAHUB_ADDR` rule).
2. **The store.**
3. A reference the user already has. These are kept, and never copied:
   - a `token_file`, or the power table's `password_file` / `password_env`;
   - `gh auth token`;
   - the fpgahub login store.

The menu shows which of these is in force, as in "from `gh auth token`".

**The store:**
- **The OS keyring when this process can reach one:** Secret Service (GNOME Keyring or KWallet) on Linux, the Keychain on macOS, the Credential Manager on Windows. Entries live under service `harness-manager`, with key = the setting key.
- **Otherwise a 0600 file** in `<config>/secrets/` (a 0700 directory). A file that group or others can read is **refused**, not just warned about: whatever read it may already hold the secret, so the fix is a new secret (S2.6).
- **`secrets/index.json`** records which backend holds each secret. It never holds a value (S2.3). Two things follow from it:
  - A service started over ssh or ThinLinc, with no session bus, reports **"stored in the Secret Service (your login keyring), which this process cannot reach"**, with the way out. It does not report "not set", and it never writes a second copy somewhere else (S2.8 to S2.10).
  - Setting the secret again from that session moves it to the file, deliberately.
- **What leaves the store:** only `get()`, called by the code that sends the credential. The API, events, logs, errors, `repr()` and the UI only ever see `{set, backend, where, reachable, why}`. There is no reveal button. "Replace" and "Remove" are the only actions.

**Measured on this box (spike S2):**
- An ssh session has no `DBUS_SESSION_BUS_ADDRESS`, so the keyring is unreachable and the fallback is used, with the reason stated.
- A throwaway GNOME Keyring in a private `dbus-run-session`, with `HOME` under `/tmp`, took the secret into the Secret Service.
- Back over ssh, that secret read as "stored, unreachable here".
- No process was left behind, and david's own keyrings (ThinLinc sessions since 09-16) were never touched.

**Library.** Use the `keyring` package, pure-Python wheels on all three OSes:
- Secret Service over `jeepney`, with no libsecret-tools needed (the spike used `secret-tool` only because `keyring` is not in the venv);
- `WinVaultKeyring` on Windows;
- the macOS Keychain.

It needs two guards:
- **Refuse `keyrings.alt`'s plaintext and "encrypted file" backends.** Accept only SecretService, KWallet, macOS and Windows.
- **Probe with a 3 s timeout.** A locked keyring on a desktop makes a lookup wait on an unlock prompt. The spike's probe handles this (`settings_secrets.py:77-96`).

This is decision **D2**.

**Existing files the lane must fix or flag:**
- `boards.toml` may hold an inline `power.auth.password`. The menu offers "move to the secret store", and the file is warned about at load, as today.
- `leases/` holds lease tokens at 0600 (`services/lease.py:494,548,626`). These are internal, not settings.
- `daemon.json` holds the service token at 0600. Also internal.
- The hub token hint still says "set hub.token_file in boards.toml" (`hub_rest.py:476`). Once the store exists it should also name `harness-manager config set-secret hubs.<name>.token` (**CCR SET-HUB-2**).

## 6. fpgahub hubs (optional)

### 6.1 What a hub is

```toml
# settings.toml
[hubs.lab]                                   # the name boards refer to
transport = "ssh"                            # ssh (a lab account) | rest (a token)
host      = "mapstone-dev.ecs.soton.ac.uk"   # ssh: the hub to ssh into ("local" when HM runs ON it)
group     = "fpga"                           # ssh: sg wrapper for the fpgahub socket ("" = none)
jump      = ""                               # ssh: ProxyJump on the way to the hub (optional)
holder    = ""                               # ssh: lease holder ("" = harness-manager-<user>@<host>)
lease_ttl = "1h"                             # the TTL asked for (hub may cap it)

[hubs.remote]
transport = "rest"
url       = "https://mapstone-dev.ecs.soton.ac.uk:7246"   # the web port takes a token; 7245 is mTLS
token     = "store"                          # store | file:PATH | env:VAR | fpgahub-login
ca_file   = ""                               # the hub's CA, when it is not in the system store
cert_file = ""; key_file = ""                # mTLS client pair, for 7245 only
events    = true; direct = "auto"; timeout_s = 30
host      = "mapstone-dev.ecs.soton.ac.uk"   # optional: the SSH fallback for the data plane (T8)
```

```toml
# boards.toml
[boards.lab]
match = ["192.168.10.101"]
name  = "mps3-01"
via   = "hub"                                # reach the board through its hub
hub   = { use = "lab", target = "mps3_01_pl", shares = { mcc = "/dev/mps3_01_pl/tty_00" } }
```

**Per-hub fields.** The REST keys are exactly T8's (`hub_rest.py:115-116`) and the SSH keys are L1's (`hub.py:12-22`), with three changes:
- **`jump` is new.** The tunnel honours `ProxyJump` from `~/.ssh/config` today, and this lets the menu set it without editing that file.
- **`holder` moves here from `--holder`.** Over REST the holder is always the token's principal, so the field is hidden for REST hubs (`hub_rest.py:32-34`).
- **`lease_ttl` moves here.** It replaces the constants `DEFAULT_TTL_S = 3600` and `DEFAULT_REQUEST_TTL_S = 7200` (`services/lease.py:126-127`).

**Standalone, with no hubs, is the zero-config default.** The Hubs section then says: *"No hub. Harness Manager talks to boards on your desk or your network directly. Add a hub when your boards live in a lab."* Nothing else changes, and no probe or test ever contacts a hub that is not configured.

### 6.2 Test connection (spike S3, S4)

The test proves each of these in order and stops at the first failure: **config → reach → auth → group → targets → target**. It **never takes, joins or releases a lease**, and it never starts a share. The REST path makes exactly three `GET`s (S3.2).

| Step | REST (T8's `RestHubClient`) | SSH (pyverify's `SshHubRunner`, so the quoting is the real one) |
|---|---|---|
| config | `parse_rest_table`. Plain http off-box is refused, because the token would cross the network readable (S3.8). | a host name, no spaces, no leading `-` |
| reach | `GET /health` (anonymous) reports the fpgahub version. A dead port fails in under 3 s (S3.7). | **One** ssh round trip with `BatchMode`, `ConnectTimeout=10` and `-J jump`. The markers `echo HM-TEST:login; id -Gn; echo HM-TEST:ids; sg fpga -c 'fpgahub board list --json'` show how far it got. "Could not resolve" or "timed out" means reach. |
| auth | `GET /whoami` reports the holder and role. No token or a 401 fails here, with the fix (S3.4, S3.5). | "Permission denied (publickey)" or "Host key verification failed" fails here, with the fix (S4 auth/hostkey). |
| group | not applicable (the token has a role) | login worked but `fpga` is missing from `id -Gn`, or the socket said EACCES: "ask the hub admin: usermod -aG fpga you; log in again" (S4 nogroup/socket) |
| targets | `GET /groups` lists what the hub offers: 3 targets on 2 boards in the fake | `fpgahub board list --json`; "command not found" means this is not the hub (S4 nofpgahub) |
| target | the board's configured target is in that list (S3.6) | same |

The result drives the UI: a green tick per step, or the failed step with its hint. The spike also found that `SshHubRunner`'s options have **no `ConnectTimeout`** (`pyverify/lease.py:94-95`), so a real hub call to a dead host waits for the caller's 60 s subprocess timeout. The test adds `ConnectTimeout=10`. Whether pyverify should add one too is a platform-side question, and the note is in §12.

**Add board from hub.** After a successful test, the targets list gets an **Add** button per target. It writes a `boards.toml` entry with the following, then probes:

| Field | Value |
|---|---|
| `hub.use` | the hub |
| `target` | the target |
| `name` | the hub's description |
| `match` | the target's `board_ip`. REST's `GET /targets/{t}` carries it (`tests/fakes/t8_hub_rest.py:335-355`). The SSH path needs `fpgahub target show --json`, which is not checked yet. |
| `shares` | `{mcc = "/dev/<target>/tty_00"}` (fpgahub's udev naming, `hub.py:101-102`) |

## 7. The Settings UI

**Build on UPDATE-UI's dialog; don't add a second one.** `SettingsModal` (`selfupdate.js:622-636`) becomes a two-column modal with a section list on the left, the layout `HelpModal` already uses (`.modal-body`, `.modal-nav`, `app.css:551-557`). The rail button (`SettingsButton`, `selfupdate.js:638-643`) and the board's "Open settings" link (`sections/update.js:181`) stay as they are, and a link can deep-link to a section (`openSettings("hubs")`). `UpdatesCard` moves into the Updates section unchanged, so its tests keep passing.

```
┌ Settings ─────────────────────────────────── this machine's Harness Manager ─ ✕ ┐
│ General      │ Hubs                                              [+ Add a hub] │
│ Hubs       2 │ ┌ lab ─────────── SSH · mapstone-dev.ecs.soton.ac.uk ──── ✓ 09:02 ┐│
│ Boards     1 │ │ Host      mapstone-dev.ecs.soton.ac.uk                 yours    ││
│ Tools      ! │ │ Group     fpga                                         default  ││
│ Updates    • │ │ Jump      —                                                     ││
│ Harness+kits │ │ Lease     1 h   Holder harness-manager-dam1n19@srv03335         ││
│ Debug        │ │ Boards    mps3-01 (mps3_01_pl)                                  ││
│ Consoles     │ │ [Test connection]  ✓ reach  ✓ auth  ✓ group  ✓ 3 targets        ││
│ Advanced     │ │   mps3_01_pl  mps3-01   in use        kr260_01_ps  [Add]       ││
│              │ └─────────────────────────────────────────────────────────────────┘│
│              │ ┌ remote ─────── REST · https://mapstone-dev…:7246 ──── ✗ auth ──┐│
│              │ │ Token    ●●●●  in a private file (no keyring in this session)   ││
│              │ │          [Replace] [Remove]                                     ││
│              │ │ ✗ auth: the hub did not accept the token (HTTP 401)            ││
│              │ │   Paste the token an fpgahub admin made (fpgahub token create)  ││
│              │ └─────────────────────────────────────────────────────────────────┘│
│              │ 🔒 "lab" is set by your administrator in /etc/harness-manager/…    │
│              │ ⟳ 1 change needs the service restarted            [Restart now]   │
└──────────────┴─────────────────────────────────────────────────────────────────┘
```

| Section | Rows (Appendix A) | Test / Detect |
|---|---|---|
| **General** | theme (stays in `localStorage`, read before first paint by `theme-boot.js`; moving it to the service would flash the page on load), the app window's browser, the identify default (10 s) | none |
| **Hubs** | §6: add, edit, remove, test, discover | Test connection per hub |
| **Boards** | per board: name (the first write N1 lacks), address and `match`, route (`via`), hub and target, shares, power meter, XVC reach, telemetry tables. A pack's own rows render under its heading. | Probe (existing `POST /boards`), Test power (a read, never a cycle) |
| **Tools** | OpenOCD, OpenOCD cfg dir, Vivado, hw_server, uv, `gh` | **Detect** fills the path from PATH and `$XILINX_VIVADO` and runs `--version`, showing "OpenOCD 0.12.0 at /usr/bin/openocd". A path that does not run is refused. OpenOCD also lists its adapters and passes only with remote_bitbang (DEBUG-OCD). |
| **Updates** | UPDATE-UI's card (channel, mode, status), plus source, mirrors, GitHub token, and the policy note | Check now (existing `POST /update/check`) |
| **Harness + kits** | channels shown, per-board pins (read-only; pin from the board's Update page), harness cache cap (planned 2 GB), kit cache cap (planned 1 GiB), kit sources, trust keys (read-only fingerprints) | Test source (reads `channel.json`, verifies it) |
| **Debug** | OpenOCD debug port base, XVC port base, hw_server mode (own / `--byo` default) | Detect hw_server |
| **Consoles** | DUT pacing (20 ms), MCC pacing over a share (100 ms), share baud, default line ending (now in React state only, `consoles.js:14`), scrollback, font size | none |
| **Advanced** | state dir (read-only, "set `HARNESS_MANAGER_STATE_DIR`"), service port and listen, log level, the log file (open), the files in use (`config path`), dev-only rows behind "Show developer settings" | none |

**Row behaviour** (one component, `SettingRow`):
- **Controls.** The type picks the control, reusing `Choice` (`selfupdate.js:505-515`) for enums, `.input` for text and paths, and a secret widget.
- **Saving.** A change saves on blur or click with one `PUT`, as UPDATE-UI does, and there is no Save button. The reply is the resolved row.
- **The source chip.** Each row has one: `default`, `yours`, `lab default` (policy `[default]`), `🔒 admin`, `from $VAR` (shadowed), `from the pack`.
- **Restart badges.** A `reopen` row shows "applies next time the board opens" with a **Reopen** button. A `daemon` row adds to the restart banner, which reuses the `RestartOverlay` from UPDATE-UI (`selfupdate.js:465`).
- **Problems.** A row's problems (a bad env value, an unknown key in the file) show under it as a `Reason`.

**Tokens and look:** only `design/tokens.json` values, per P4. There is no new colour. The lock and chip styles reuse `.badge-dot`, `Chip` and `.policy-note`.

## 8. CLI, API, events

**CLI** (`cli/cmd_config.py`, plus `cli/cmd_hub.py`'s new `hub` verb; neither name is taken today):

```
harness-manager config list [--section S] [--board B] [--all]   # --all: dev-only rows too
harness-manager config get KEY [--board B]                      # value, source, lock, shadow; both envs
harness-manager config set KEY VALUE [--board B]
harness-manager config unset KEY [--board B]
harness-manager config set-secret KEY [--stdin | --from-file F] # prompts (getpass); never from argv
harness-manager config clear-secret KEY
harness-manager config test hub NAME | tools | updates          # the same reports as the menu
harness-manager config path                                     # every file in use, and the keyring backend
harness-manager hub list | add NAME (--ssh HOST [--group G] [--jump J] | --url URL) | edit | remove | test NAME | targets NAME
```

`--json` works on every verb and gives the API's own shapes. Exit codes are the existing ones:
- 2 USAGE: a bad value;
- 15 REFUSED: locked;
- 7 UNREACHABLE: a test that cannot reach, or a secret in an unreachable keyring;
- 12 UNAVAILABLE.

**API** (`daemon/settings_api.py`; every route is behind the existing Bearer token):

| Route | Body / reply |
|---|---|
| `GET /settings[?section=&board=&dev=1]` | `{schema_version, files:{settings, boards, policy, secrets_backend}, policy:{path, problems}, rows:[{key, type, section, scope, value, default, source, where, locked, shadowed, capped, restart, choices, help, problems, secret?:{set, backend, where, reachable, why}}]}` |
| `GET /settings/schema` | the rows without values, including every pack's rows (the UI and `--help` read it) |
| `PUT /settings` | `{KEY: value, …}`, all or nothing. Returns the resolved rows plus `restart:{reopen:[…], daemon:[…]}`. A locked key gives 409; a bad value gives 400 naming the key. |
| `DELETE /settings/{key}` | unset (back to the next layer) |
| `PUT /settings/secrets/{key}` · `DELETE …` | `{value}`, write-only. The reply is the status, never the value. |
| `POST /settings/test` | `{kind:"hub", name}` or `{kind:"hub", table:{…}}` (test before saving) · `{kind:"tools"}` · `{kind:"updates"}`. SSH takes up to 20 s, so it runs as a job (`daemon/jobs.py`) and returns `{job}`; REST and tools answer inline. |
| `GET/PUT/DELETE /hubs[/{name}]` · `GET /hubs/{name}/targets` | sugar over `hubs.*` rows, plus discovery |

`GET/PUT /update/settings` stays, as a view over `updates.channel` and `updates.auto`, so UPDATE-UI's tests and older CLIs keep working. `docs/API.md` gains a Settings section, and `api.js` `ENDPOINTS` gains the routes, because the contract guard requires both (`tests/web/test_t14_static.py:271-296`).

**Events:**
- `settings.changed {keys, source: "api" | "file", restart: {reopen, daemon}}`. It never carries a value for a secret key, only the key. `source: "file"` comes from an mtime check each time a setting is resolved (at most one `stat` per second), so a hand edit reaches the open tabs.
- `hubs.changed {name}`.
- `settings.test {job, step, ok}` while an SSH test runs.

The update checker subscribes to `settings.changed`, so a new channel takes effect at once instead of at the next tick (`daemon/update_checker.py:151`).

## 9. Spike

`PYTHONPATH=src:. .venv/bin/python -m tests.spikes.settings_spike`, board-free, about 40 s: **44/44 PASS** (`docs/assessment/settings_spike_2026-09-25/spike_output.txt`).

| Group | What it shows |
|---|---|
| **S1 precedence, 16 probes** | lock > env > user > machine > pack > default. The env value shadows the user's and says so. A lock beats env, and `set` on it is REFUSED, naming the file. The three U6 keys keep their meaning (cap `notify`, pin `stable`, 12 h). A bad env value is skipped with the reason. A bad lock fails closed. Pack rows are declared by the pack. Dev rows stay out of the menu. A secret resolves to `{set}` only. The user file round-trips at 0600. |
| **S2 secrets, 11 probes** | Real on this box. An ssh session with no bus falls back to a 0600 file in a 0700 dir, with the reason. The index holds no value. Env beats the store. A 0644 file is refused. A throwaway GNOME Keyring in a private D-Bus session takes a secret into the Secret Service. Back over ssh, that secret reads "stored, unreachable here" (7 UNREACHABLE, with the way out). No file copy was made. Nothing was left running. |
| **S3 REST test, 8 probes** | Against T8's fake fpgahub 0.3.0 (`tests/fakes/t8_hub_rest.py`). The happy path lists 3 targets on 2 boards with only `GET /health`, `/whoami` and `/groups`: no lease. No token, a bad token, an unknown target, a dead port and plain http off-box each fail at the right step with a hint. The token never appears in the report. |
| **S4 SSH test, 9 probes** | Real subprocesses through fake `ssh`, `sg`, `id` and `fpgahub` on PATH. The quoting crosses two real shells, and `COLUMNS=400` arrives. dns and timeout give reach. hostkey and publickey give auth. nogroup and socket give group. nofpgahub gives targets. ok gives 3 targets. |
| **Negative twin** (run by hand) | With env swapped below the user file, S1.3, S1.4, S1.8, S1.9 and S1.10 fail, so the probes bite. |

**Spike files:**
- `settings_model.py`: schema, resolver, policy;
- `settings_secrets.py`: store, backends, index;
- `settings_hubtest.py`: the two testers;
- `settings_fake_bin.py`: the shims;
- `settings_spike.py`: the runner.

The lane moves the model and the store into `src/harness_manager/settings/` and promotes the fake binaries into `tests/fakes/`.

## 10. Decisions for david

**D1. Where settings live.**
- **(a) Recommended.** A new `settings.toml` for the app and the hubs. Keep `boards.toml` for boards, which gains `hub.use = "<name>"`. Edit both in place with `tomlkit`, which keeps comments.
- (b) One `config.toml` that absorbs `boards.toml`. Cleaner in a year, but it moves a file the HIL and Linux runbooks cite, in the cutover week.
- (c) No new file: the menu covers only updates and hubs, and everything else stays in env and hand-edited TOML.

**D2. Where secrets go.**
- **(a) Recommended.** The OS keyring through the `keyring` package, which adds a dependency (pure wheels), with the 0600 file fallback and the index (the spike's model).
- (b) File-only at 0600. The simplest, matches `token_file` today, and has no keyring UX. On a shared lab machine, though, root and backups see the token.
- (c) Hand-rolled per-OS helpers (`secret-tool`, `security`, ctypes `CredRead`). This avoids the dependency, but `secret-tool` is not installed by default on Ubuntu.

**D3. How far the admin policy reaches.**
- **(a) Recommended.** A generic `[lock]` and `[default]` over any lockable key, plus `[hubs.<name>]` for machine hubs. Today's three keys keep their meaning.
- (b) Keep the policy to self-update only (U6 as built), and add keys one at a time when an admin asks.

**D4. When.**
- **(a) Recommended.** SET-CORE and SET-HUBS now: board-free, no static and no firmware. The UI follows after the Linux cutover (10-12), so the Linux SSH rows (§A.11) land in the schema, not in more env vars.
- (b) All six lanes after the cutover.
- (c) Only the Hubs section now, as a stopgap on today's `boards.toml` (the menu writes the inline table), and the rest later.

The precedence order (§4.2) is not listed as a decision: env above the user's file is what every current test relies on. If david would rather have the GUI win over his shell, that is a one-line change to the resolver.

## 11. Lane plan

**Order: SET-CORE, then SET-HUBS, SET-API and SET-PACK in parallel, then SET-UI, then SET-WIRE.** No lane needs a board or a mint.

### SET-CORE (10 h)

- **Scope (new):** `src/harness_manager/settings/{schema,resolve,files,secrets,policy}.py`.
- **CCRs:**
  - **SET-CORE-1:** `pyproject.toml`, `constants.txt`, the wheelhouse (`keyring`, `tomlkit`).
  - **SET-CORE-2:** `services/update/policy.py` (parse the new `[lock]`/`[default]` tables and keep `Policy` as-is for its callers).
  - **SET-CORE-3:** `services/update/selfupdate.py` (read/write `updates.*` through the resolver; `settings.json` fallback).
- **Tests:** `tests/unit/test_settings_{resolve,files,secrets,policy}.py`. Every spike probe becomes a unit test, each with its negative twin. The keyring runs against an in-memory backend; the file backend is real in `tmp_path`.

### SET-HUBS (10 h)

- **Scope (new):** `src/harness_manager/settings/hubs.py` (the hub model, test connection, discovery, "make this a hub"), `tests/fakes/settings_fake_bin.py`.
- **CCRs:**
  - **SET-HUB-1:** `harness_manager_mps3/hub.py:131-190` (resolve `hub.use`; refuse `use` together with per-hub keys; `via = "hub"`).
  - **SET-HUB-2:** `transports/hub_rest.py:429-449,476` (a `store` credential source; hints name `config set-secret`).
  - **SET-HUB-3:** `services/lease.py:126-127,186-193` (TTL and holder from the hub row).
  - **SET-HUB-4:** `harness_manager_mps3/tunnel.py` (the `jump` → `-J`).
- **Tests:** unit, plus integration over `t8_hub_rest` and the fake binaries. The existing L1/T8/LR suites must stay green, which proves an inline `hub` still works.

### SET-API (8 h)

- **Scope (new):** `daemon/settings_api.py`, `cli/cmd_config.py`, the `hub` verb in `cli/cmd_hub.py` (owned by the L1 lane: **CCR SET-API-3**).
- **CCRs:**
  - **SET-API-1:** `daemon/app.py` (routes).
  - **SET-API-2:** `cli/main.py` (register).
  - **SET-API-4:** `docs/API.md`, `web/static/js/api.js` `ENDPOINTS`.
  - **SET-API-5:** `daemon/update_checker.py` (subscribe to `settings.changed`).
- **Tests:** `tests/integration/test_settings_api.py` over the real daemon (locked gives 409; a secret is never in any reply or event, checked by grepping the whole response and event log for the value; `PUT` is all or nothing), and `tests/unit/test_cli_config.py`.

### SET-PACK (5 h)

- **CCRs:**
  - **SET-PACK-1:** `core/pack.py` (optional `settings()`).
  - **SET-PACK-2:** `harness_manager_mps3/pack.py` + `constants.py` (declare the `mps3.*` rows and the board tables `xvc`/`power`/`sysmon`/`estimates`/`hub` as rows; the pack still validates).
- **Tests:** a fake pack with its own rows appears in `GET /settings/schema`; the MPS3 rows match what the pack validates, generated from one list.

### SET-UI (12 h)

- **Scope (new):** `web/static/js/settings/*.js` (`SettingsModal` sections, `SettingRow`, `SecretField`, `HubCard`, `TestSteps`).
- **CCRs:**
  - **SET-UI-1:** `selfupdate.js` (move `UpdatesCard` into a section; `openSettings(section)`).
  - **SET-UI-2:** `app.css` (the section layout, from tokens).
  - **SET-UI-3:** `tests/fakes/l3_week_plan.py` (the mock's `/settings` routes).
- **Tests:** `tests/web/test_settings_browser.py` over the mock and the real daemon, with a negative twin each (a lock shows as locked; a shadowed row names the variable; a secret field never renders a value; Test connection shows the failing step), plus screenshots, light and dark, for the review.

### SET-WIRE (10 h)

- **Scope:** switch each reader from `os.environ` to `settings.get()`, keeping the variable as the env layer.
- **Files:**
  - `services/debug.py:116-139,748`
  - `services/xvc.py:120-124,761-790,1219`
  - `services/kit/vivado.py:39`
  - `services/kit/service.py:80,247`
  - `services/update/{github,download,channel,app}.py`
  - `harness_manager_mps3/{openocd,overlays,xvc}.py`
  - `web/window.py:38-39`
- **CCRs:** one per owning lane. These are mechanical and can batch.
- **Tests:** every existing test must pass unchanged. That is the proof that env still wins.

**Total ≈ 55 h.** Docs (USER_GUIDE "Settings", INSTALL "policy [lock]/[default]", HUB_MODE "hubs") ride with each lane.

## 12. Found on the way

1. **No `ConnectTimeout` on the hub's ssh runner.** `pyverify.lease.SSH_OPTS` has none (`pyverify/lease.py:94-95`), so a dead or firewalled hub stalls a hub verb until the caller's own subprocess timeout (`HUB_TIMEOUT_S = 60`, `harness_manager_mps3/hub.py:91`), not the ~10 s a connect timeout would give. The HM tunnel has `ConnectTimeout=15` (`harness_manager_mps3/tunnel.py:97`). This is a platform-side fix, noted here for the lead.
2. **The update setting has no change event.** `PUT /update/settings` publishes nothing, so a second tab only sees the change after the 60 s poll, and the checker only after its next tick (§8 fixes both).
3. **The session-storage key names are inconsistent.** One uses a hyphen: `harness-manager.details` (`sections/overview.js:294`). All the others use `harness_manager.*`. This is harmless, but it is a key to migrate when prefs move.
4. **Several constants are really user preferences:**
   - the identify seconds (10, in React state, `sections/panel.js:28-29`);
   - the console line ending (`consoles.js:14`);
   - scrollback and font size (`consoles.js:44-53`).

   Each resets on reload.
5. **The fpgahub token hint names the file, not the tool.** It says "set hub.token_file in boards.toml" (`hub_rest.py:476`). That stays right for a hand-edited file, but once the menu exists it should name the menu and `config set-secret`.
6. **The state-dir rule is copied five times, and a `--state-dir` or `--demo` service still reads the real `boards.toml`.**
   - The copies: `engine.py:79`, `daemon/state.py:36`, `core/session.py:28`, `services/debug.py:447`, `services/xvc.py:1094`.
   - `boards.toml`, the tunnel dir, statics and the CLI's leases all call `resolve_state_dir()` with no config (`power/config.py:151-154`, `harness_manager_mps3/tunnel.py:282-285`, `statics.py:51-53`, `cli/cmd_hub.py:390`).

   SET-CORE should give the resolver one `config_dir()` that honours the service's own state dir. Otherwise a demo service shows your real hubs.
7. **`daemon start` drops two of the service's flags.** It does not forward `--log-level` or `--pack-overrides` (`daemon/control.py:161-162`), so today the two Advanced rows V4 and D4 cannot be set through the normal start path.
8. **The service checks only the Bearer token.** There is no Origin or Host check (`daemon/app.py:571-609`), and a non-loopback `--listen` only warns. The token travels in a header, not a cookie, so a hostile page can neither forge a request nor read a reply through DNS rebinding. Even so, `PUT /settings/secrets` makes the token worth more, and a Host allow-list (loopback names only) is cheap defence in depth. SET-API adds it together with the settings routes.
9. **The OpenOCD cfg dir is read at import as well as at call time** (`harness_manager_mps3/constants.py:113`; `openocd.py:99-105`). SET-WIRE must drop the import-time copy, or the T2 row (`mps3.openocd_cfg_dir`) is live for one path and restart-only for the other.

---

## Appendix A. Inventory

The columns:
- **Source:** where the value comes from today.
- **Scope:** U = per user, M = per machine, B = per board, H = per hub, P = per pack.
- **Chg** (what a change needs): L = live, R = board reopen, D = service restart.
- **Owner:** U = user, A = admin-lockable, Dev = developer.
- **Menu:** ✓ = shown; env = env-only (`ui = false`).

### A.1 General

| # | Key (proposed) | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| G1 | `general.theme` | light / dark / follow the system | `localStorage harness_manager.theme` (`web/static/js/theme.js:5,28-31`; read pre-paint `theme-boot.js:4`) | auto | U (browser) | | L | U | ✓ (stays in the browser) |
| G2 | `general.app_browser` | the browser the app window uses | env `HARNESS_MANAGER_APP_BROWSER` (`web/window.py:38,69,136`) | pywebview, else Chrome/Edge/Chromium | M | | L | U | ✓ |
| G3 | `general.window_size` | app window size | const `WINDOW_SIZE` (`web/window.py:41`) | 1440×900 | U | | L | U | ✓ (remember last) |
| G4 | `general.app_keep_dbus` | keep the desktop bus for the app window | env `HARNESS_MANAGER_APP_KEEP_DBUS` (`web/window.py:39,90-93`) | off | M | | L | Dev | env |
| G5 | `general.open_browser` | whether `ui` opens a browser | env `BROWSER`/`DISPLAY`/`WAYLAND_DISPLAY` (`cli/cmd_daemon.py:189-193`) | auto | U | | L | Dev | env |

### A.2 Hubs (`[hubs.<name>]`)

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| H1 | `hubs.*.transport` | SSH (lab account) or REST (token) | implied: `url` present means REST (`transports/hub_rest.py:351-357`) | ssh | H | | R | A | ✓ |
| H2 | `hubs.*.host` | SSH hub (`local` on the hub itself) | boards.toml `hub.host` (`harness_manager_mps3/hub.py:111,141-144`) | none | H | | R | A | ✓ |
| H3 | `hubs.*.group` | `sg` group for the fpgahub socket | `hub.group` (`hub.py:90,116,159`) | fpga | H | | R | A | ✓ |
| H4 | `hubs.*.jump` | ProxyJump on the way to the hub | none: only `~/.ssh/config` (`harness_manager_mps3/tunnel.py:36-45`) | none | H | | R | A | ✓ (new) |
| H5 | `hubs.*.url` | fpgahub REST API (7246 token, 7245 mTLS) | `hub.url` (`hub_rest.py:242,277-302`) | none | H | | R | A | ✓ |
| H6 | `hubs.*.token` | Bearer token | `hub.token_file`, then `$FPGAHUB_TOKEN` (with `$FPGAHUB_ADDR`), then the fpgahub login store (`hub_rest.py:429-449`) | none | H (per user) | **Y** | R | U | ✓ |
| H7 | `hubs.*.ca_file` | the hub's CA | `hub.ca_file` (`hub_rest.py:245`) | system store | H | | R | A | ✓ |
| H8 | `hubs.*.cert_file` / `key_file` | mTLS client pair (7245) | `hub_rest.py:246-247,340-342` | none | H | key **Y** (path) | R | U | ✓ |
| H9 | `hubs.*.insecure` | skip TLS verification | `hub_rest.py:248,319-321` | false | H | | R | A | ✓ (advanced, warned) |
| H10 | `hubs.*.events` | use the SSE stream | `hub_rest.py:249,323-325` | true | H | | R | A | ✓ (advanced) |
| H11 | `hubs.*.direct` | route vs tunnel for the data plane | `hub_rest.py:112,250,326-328` | auto | H | | R | A | ✓ (advanced) |
| H12 | `hubs.*.timeout_s` | REST call timeout | `hub_rest.py:105,252,332-334` | 30 s | H | | L | A | ✓ (advanced) |
| H13 | `hubs.*.holder` | lease holder (SSH; REST uses the token's principal) | `lease acquire --holder` (`cli/cmd_hub.py:291,828`); `default_holder()` (`services/lease.py:186-193`) | `harness-manager-<user>@<host>` | H | | L | U | ✓ (SSH only) |
| H14 | `hubs.*.lease_ttl` | lease length asked for | `--ttl`, `DEFAULT_TTL_S` (`services/lease.py:126`; `cli/cmd_hub.py:288`; `daemon/hub_api.py:96-102`) | 3600 s | H | | L | A | ✓ |
| H15 | `hubs.*.request_ttl` | TTL asked for with a lease request | `DEFAULT_REQUEST_TTL_S` (`services/lease.py:127`) | 7200 s | H | | L | A | ✓ (advanced) |
| H16 | `hubs.*.queue_timeout` | how long an acquire waits in the queue | `--timeout`, `ACQUIRE_TIMEOUT_S` (`services/lease.py:132`; `cli/cmd_hub.py:293`) | 3600 s | H | | L | U | ✓ (advanced) |
| H17 | `FPGAHUB_TOKEN` / `FPGAHUB_ADDR` | env override of the token | `hub_rest.py:435-438` | unset | U | **Y** | L | Dev | env |
| H18 | `FPGAHUB_CLIENT_CONFIG` | where the fpgahub login store is | `hub_rest.py:388-394` | `~/.config/fpgahub/config.toml` | U | | L | Dev | env |

### A.3 Boards (`boards.toml [boards.<key>]`)

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| B1 | `boards.*.match` | other ids/addresses for this board | `power/config.py:189-193` | [] | B | | L | U | ✓ ("Addresses") |
| B2 | `boards.*.name` | display name (outranks harness/hub) | `power/config.py:201,207-222`; `naming.py:11-18` | none | B | | L | U | ✓ (first write) |
| B3 | `boards.*.via` | route: direct, `ssh:HOST`, or `hub` | `hub.py:190-196`; `--via` (`cli/main.py:67,124`) | direct | B | | R | U | ✓ |
| B4 | `boards.*.hub.use` | which hub (new reference) | none (inline `hub` table today) | none | B | | R | U | ✓ (new) |
| B5 | `boards.*.hub.target` | fpgahub target | `hub.py:112,145` | `mps3_01_pl` (a lab name as default) | B | | R | U | ✓ |
| B6 | `boards.*.hub.board` | physical board for revoke | `hub.py:117,162`; `hub_rest.py:251` | "" (ask the hub) | B | | R | U | ✓ (advanced) |
| B7 | `boards.*.hub.shares` | name → `/dev` TTY | `hub.py:113,148` | {} | B | | R | U | ✓ |
| B8 | `boards.*.hub.baud` | share line rate | `hub.py:114,153`; `transports/tcp_serial.py:50` | 115200 | B | | R | U | ✓ (advanced) |
| B9 | `boards.*.hub.start_shares` | start a missing share | `hub.py:115,156` | false | B | | R | U | ✓ |
| B10 | `boards.*.power.kind` | shelly_gen2, tasmota, netio or ina260_mcp2221 | `power/config.py:229-235` | none | B | | L | U | ✓ |
| B11 | `boards.*.power.url` | plug URL (credentials in it are refused) | `power/config.py:236-246` | none | B | | L | U | ✓ |
| B12 | `boards.*.power.outlet` | outlet | `power/config.py:247-251` | 0 (shelly), 1 | B | | L | U | ✓ |
| B13 | `boards.*.power.auth.user` | plug user | `power/config.py:277-279` | admin | B | | L | U | ✓ |
| B14 | `boards.*.power.auth.password` | plug password (`password`, `_env` or `_file`) | `power/config.py:280-302`, readable warning `:305-316` | none | B | **Y** | L | U | ✓ (to the store) |
| B15 | `boards.*.power.timeout_s`, `cycle` | device timeout; allow a power cycle | `power/config.py:252-257` | 3 s; true (false for INA260) | B | | L | U | ✓ |
| B16 | `boards.*.power.i2c_address`, `device` | INA260 address; MCP2221 index | `power/config.py:258-264` | 0x40; 0 | B | | L | U | ✓ |
| B17 | `boards.*.xvc.reach` | auto, hub, board-ssh or direct | `harness_manager_mps3/xvc.py:13-16,110-137` | auto | B | | R | U | ✓ (pack row) |
| B18 | `boards.*.xvc.user`, `host` | board-SSH user and host | `xvc.py:123,130-136` | root; derived | B | | R | U | ✓ (pack row) |
| B19 | `boards.*.sysmon.*` | backend xsdb or openocd, with its binary, hw_server, device, adapter, speed, timeout | `harness_manager_mps3/sysmon.py:76-77,415-436` | xsdb, `tcp:127.0.0.1:3121` | B | | L | U | ✓ (pack row) |
| B20 | `boards.*.sysmon.min_interval_s` | SYSMON read interval | `harness_manager_mps3/telemetry.py:81,397` | 10 s | B | | L | U | ✓ (advanced) |
| B21 | `boards.*.estimates.vivado_reports` | directory of Vivado power reports | `harness_manager_mps3/telemetry.py:403-413` | unset | B | | L | U | ✓ (pack row) |
| B22 | `boards.*.consoles.<name>.baud` | a console's rate (runtime only, lost on restart) | `POST …/consoles/{name}/baud` (`services/console.py:1031`; `daemon/consoles_api.py:9`) | 115200; 76800 DUT | B | | L | U | ✓ (persist) |
| B23 | harness pin | the board's pinned harness release | `<state>/update/pins.json` (`services/update/state.py:112`; `docs/API.md:350`) | none | B | | L | U | ✓ (read-only; pin from the board's Update page) |

### A.4 Tools

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| T1 | `tools.openocd` | OpenOCD binary | env `HARNESS_MANAGER_OPENOCD` (`services/debug.py:116,131-143`) | `openocd` on PATH | M | | L | A | ✓ Detect |
| T2 | `mps3.openocd_cfg_dir` | OpenOCD target configs | env `HARNESS_MANAGER_MPS3_OPENOCD_DIR` (`harness_manager_mps3/openocd.py:58,99-105`; also frozen at import, `constants.py:111-118`) | the sibling platform checkout, else packaged | M (pack) | | L | A | ✓ (advanced) |
| T3 | `tools.vivado` | Vivado (or `off`) | env `HARNESS_MANAGER_VIVADO`; roots `/tools`, `/opt`, `/apps` (`services/kit/vivado.py:39-43,138,161`) | auto | M | | L | A | ✓ Detect |
| T4 | `tools.hw_server` | hw_server binary | env `HARNESS_MANAGER_HW_SERVER`, else Vivado's, then `$XILINX_VIVADO` (`services/xvc.py:120,760-786`) | auto | M | | L | A | ✓ Detect |
| T5 | `tools.uv` | uv for self-update | env `HARNESS_MANAGER_UV` (`services/update/app.py:83,259,300`); `install.json` `uv` | uv on PATH | M | | L | A | ✓ (advanced) |
| T6 | `tools.gh` | the `gh` CLI (token fallback) | `gh auth token` (`services/update/github.py:148,154-180`) | gh on PATH | U | | L | U | ✓ (read-only "found") |
| T7 | `XILINX_VIVADO` | Xilinx's own variable | `services/xvc.py:779` | unset | M | | L | Dev | env |

### A.5 Updates

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| U1 | `updates.channel` | stable, beta or dev | `update/settings.json`; env `HARNESS_MANAGER_UPDATE_CHANNEL`; policy `channel` (`selfupdate.py:116`; `channel.py:54,105`; `policy.py:63`) | stable | U | | L | A | ✓ (exists) |
| U2 | `updates.auto` | off, notify or stage | `settings.json`; policy `self_update` cap (`selfupdate.py:117,158-174`; `policy.py:62`) | stage | U | | L | A | ✓ (exists) |
| U3 | `updates.check_interval` | background check period | policy only (`policy.py:16,142-151`) | 6 h | M | | L | A | ✓ |
| U4 | `updates.source` | the release source (a GitHub repo, URL or directory) | env `HARNESS_MANAGER_UPDATE_SOURCE`; `--source` (`channel.py:50,80`; `cli/cmd_update.py:81`) | `github:SoC-Labs/HarnessManager` | U | | L | A | ✓ |
| U5 | `updates.mirrors` | mirrors tried by sha first | env `HARNESS_MANAGER_UPDATE_MIRRORS` (`download.py:78,114-118`) | [] | M | | L | A | ✓ |
| U6 | `updates.github_token` | token for the private (Arm-IP) parts | env `HARNESS_MANAGER_GITHUB_TOKEN`, then `gh auth token` (`github.py:48,171-180`; `download.py:110`) | gh | U | **Y** | L | U | ✓ |
| U7 | install record | the venv, version, extras, uv | `install.json` (`scripts/install.sh:553-556`; `_launch.py:54`) | none | M | | none | installer | ✓ (read-only, Advanced) |
| U8 | `HARNESS_MANAGER_GITHUB_API` | GitHub API host | `github.py:47,61` | api.github.com | M | | L | Dev | env |
| U9 | `HARNESS_MANAGER_UPDATE_FIRST_CHECK_S` | first check delay | `daemon/update_checker.py:48,58` | default | M | | D | Dev | env |
| U10 | `HARNESS_MANAGER_NO_SELF_UPDATE` | launcher ignores the pointer | `_launch.py:57` | off | M | | none | Dev | env |
| U11 | `HARNESS_MANAGER_USE_INSTALLED` | run the installer's version | `_launch.py:56` | off | M | | none | Dev | env |
| U12 | apply budgets `health_s`, `stable_s` | post-apply health wait | API params (`docs/API.md:325`) | 30 s; 10 s | M | | none | Dev | env |

### A.6 Harness versions and kits

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| K1 | `harness.channels` | the channels the Harness versions list shows | `--channel` (`cli/cmd_harness.py:69`); remembered per board in memory (`daemon/harness_api.py:108`) | stable | U | | L | U | ✓ |
| K2 | `harness.source` | the harness catalogue source | `--source` (`cmd_harness.py:73`) | the update source | U | | L | A | ✓ |
| K3 | `harness.cache_max` | the harness blob cache cap | none (planned: `HARNESS_DISTRIBUTION.md:288-292`) | 2 GB | M | | L | A | ✓ |
| K4 | `kits.cache_max` | the kit cache cap | none; a TODO (`services/kit/service.py:34-35`); planned (`DUT_BUILD_KIT_STORAGE.md:222`) | unbounded (planned: 1 GiB) | M | | L | A | ✓ |
| K5 | `kits.sources` | search order: cache, channel, hub | `SOURCES` (`kit/service.py:83-86`); `--source` (`cmd_kit.py:94`) | all | U | | L | U | ✓ |
| K6 | `kits.hub_dir` | the hub's mint archive path | env `HARNESS_MANAGER_KIT_HUB_DIR` (`kit/service.py:80,247`) | unset | M | | L | A | ✓ |
| K7 | `overlays.dirs` | extra overlay directories | env `HARNESS_MANAGER_MPS3_OVERLAY_DIRS`; `--overlay-dir` (`harness_manager_mps3/overlays.py:71,312-314`; `cli/main.py:156`) | none | U | | L | U | ✓ |
| K8 | trust keys | pinned release keys and rotation | `PINNED_KEYS` (`services/update/trust.py:83`), `<state>/update/keys.json` | empty (U2) | build | | none | build | ✓ (read-only fingerprints) |
| K9 | `kits.jobs` | Vivado threads for a kit build | `--jobs` (`cli/cmd_kit.py:121`) | 2 | U | | L | U | ✓ (advanced) |

### A.7 Debug

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| D1 | `debug.port_base` | pin the gdb/telnet/tcl block | env `HARNESS_MANAGER_DEBUG_PORT_BASE` (`services/debug.py:117,122-124,744-748`) | a hashed slot from 23300 | M | | D | A | ✓ (advanced) |
| D2 | `debug.xvc_port_base` | pin the relay and hw_server pair | env `HARNESS_MANAGER_XVC_PORT_BASE` (`services/xvc.py:121,124-128,1215-1219`) | a hashed slot from 23600 | M | | D | A | ✓ (advanced) |
| D3 | `debug.hw_server_mode` | HM's own hw_server, or `--byo` | `--byo` (`cli/cmd_xvc.py:97,109`; `daemon/xvc_api.py:82,107`) | own (X2) | U | | L | U | ✓ |
| D4 | `mps3.rbb_port` | the board's remote_bitbang port | pack kwarg via `--pack-overrides` (`constants.py:16`; `pack.py:296`) | 6921 | P | | D | Dev | env |
| D5 | `HARNESS_MANAGER_MPS3_XVC_PORT` | XVC port on a direct board | `harness_manager_mps3/xvc.py:58,286-288` | 2542 | B | | L | Dev | env |
| D6 | service timeouts | XVC start/stop/acquire, OpenOCD start/detect/stop | kwargs, never exposed (`services/xvc.py:1064-1065`; `debug.py:422-424`) | 60/5/8 s; 30/30/3 s | M | | D | Dev | env (not settable) |

### A.8 Consoles and pacing

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| C1 | `mps3.console.pace_ms` | per-character delay into the DUT UART | `DUT_CONSOLE_PACE_S`; pack kwarg `console_pace_s` (`harness_manager_mps3/constants.py:31`; `pack.py:298`) | 20 ms | P | | R | U | ✓ |
| C2 | `mps3.mcc.pace_ms` | MCC pace over USB and over a hub share | `MccTiming.pace_s` and `SHARE_PACE_S` (`harness_manager_mps3/mcc.py:122,139,142-147`) | 60 ms; 100 ms | P | | R | U | ✓ (advanced) |
| C3 | `consoles.line_ending` | Enter sends CR, LF or CRLF | React state per pane (`web/static/js/sections/consoles.js:14,122,192-194`) | CRLF | U | | L | U | ✓ |
| C4 | `consoles.scrollback`, `font_size` | the browser terminal | xterm consts (`consoles.js:44-53`) | 5000 lines; 13 px | U | | L | U | ✓ |
| C5 | `consoles.history_bytes` | replay for a new subscriber | `ConsoleBroker` kwarg (`services/console.py:733`) | 64 KiB | M | | D | Dev | env |
| C6 | `HARNESS_MANAGER_PTY_DIR` | the PTY link directory | `services/pty.py:107,186-189` | `/tmp/harness-manager-$USER` | U | | D | Dev | env |

### A.9 Presence and panel

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| P1 | `panel.identify_s` | identify blink default | `IDENTIFY_DEFAULT_S` in three places (`services/presence.py:89-90`; `cli/cmd_panel.py:40`; `web/static/js/sections/panel.js:29`) | 10 s (1..30) | U | | L | U | ✓ |
| P2 | `panel.presence_who` | the name the board's panel shows for you | `default_who()` (`services/presence.py:98-108,214`); no opt-out (`docs/design/CLCD_ALIGNMENT.md:28,153`) | user@host | U | | L | A | ✓ |
| P3 | `panel.beat_s` | presence hello interval | `BEAT_S`, `FAST_BEAT_S` (`core/panel.py:48,51`) | 30 s; 10 s | M | | D | Dev | env |

### A.10 Advanced (service, state, logs)

| # | Key | What | Source today | Default | Scope | Sec | Chg | Owner | Menu |
|---|---|---|---|---|---|---|---|---|---|
| V1 | `advanced.state_dir` | where settings and state live | env `HARNESS_MANAGER_STATE_DIR`; daemon `--state-dir` (`engine.py:58,79-86`; `daemon/server.py:414`) | `~/.config/harness-manager` | U | | D | U | ✓ (read-only: "set the variable") |
| V2 | `advanced.port` | the service's TCP port | `--port` (`daemon/server.py:416`; `cli/cmd_daemon.py:62`) | 0 (any free port) | M | | D | U | ✓ |
| V3 | `advanced.listen` | bind address (warns off loopback) | `--listen` (`server.py:417`; `daemon/state.py:50-56`) | 127.0.0.1 | M | | D | A | ✓ |
| V4 | `advanced.log_level` | log verbosity (`daemon start` does not forward it) | `--log-level` (`server.py:419`) | info | M | | D | U | ✓ |
| V5 | `advanced.log_rotation` | log size and number of backups | `LOG_MAX_BYTES`, `LOG_BACKUPS` (`daemon/logfile.py:25-26`) | 8 MiB × 3 | M | | D | Dev | env |
| V6 | `HARNESS_MANAGER_HOME`, `_BIN_DIR` | install root and bin dir | `scripts/install.sh:39-41`; `_launch.py:9` | `~/.local/share/harness-manager` | M | | none | installer | env |
| V7 | CLI→service timeout | the HTTP client's timeout | const (`client/http.py:22`) | 300 s | U | | L | Dev | env |

### A.11 Developer and test seams (env only; `config list --all`)

| # | Variable | What | Source | Default |
|---|---|---|---|---|
| X1 | `HARNESS_MANAGER_NO_DAEMON` | the CLI never uses the service | `cli/engine.py:41,77-78` | unset |
| X2 | `HARNESS_MANAGER_CLI_ENGINE` | engine factory override | `cli/engine.py:40,109,124` | unset |
| X3 | `HARNESS_MANAGER_DEBUG` | tracebacks on internal errors | `cli/main.py:426` | unset |
| X4 | `HARNESS_MANAGER_MPS3_IDENTIFY_PORT` | identify UDP port | `harness_manager_mps3/identify.py:61,76-88` | 6899 |
| X5 | `HARNESS_MANAGER_MPS3_IDENTIFY_BROADCAST` | discovery targets. Note: replaces the default, which includes the lab's 192.168.10.101. A user on another subnet may want "extra discovery addresses" as a Boards row later. | `identify.py:62,91-104,460-463` | 255.255.255.255 + 192.168.10.101 |
| X6 | `HARNESS_MANAGER_MPS3_PUSH_PORT` | bitstream push port | `harness_manager_mps3/deploy.py:116,276-277` | 6910 |
| X7 | `HARNESS_MANAGER_MPS3_TFTP_PORT` | TFTP port | `deploy.py:117,280-281` | 69 |
| X8 | `HARNESS_MANAGER_MPS3_TUNNELLED` | force the tunnelled flag | `deploy.py:120,151-175` | auto |
| X9 | `--pack-overrides` | pack constructor kwargs (hidden) | `daemon/server.py:429,446-453` | {} |
| X10 | `--demo` | scripted boards | `server.py:421`; `cli/cmd_daemon.py:56` | off |

### A.12 Planned by other lanes

| # | Key | What | From | Default | Scope | Sec | Chg | Menu |
|---|---|---|---|---|---|---|---|---|
| L1 | `boards.*.ssh.user` | Linux harness login (generalises `xvc.user`) | `plat:docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md:122` (DL5); `xvc.py:13-16` | root | B | | R | ✓ |
| L2 | `boards.*.ssh.identity` | the key HM's ssh uses for the board | `plat:docs/planning/B0_RUNBOOK_LINUX.md:56-57`; `B1_RUNBOOK_LINUX.md:78-89` | agent / ssh default | U | path to a secret (never read by HM) | R | ✓ |
| L3 | `boards.*.ssh.host_key` | pinned host key (from identify `ssh.host_key_sha256`) | `docs/TEAM_PLAN.md:383`; `lx:docs/planning/linux_lanes/IMAGE_CONTRACT.md:121-133` | TOFU at claim | B | | R | ✓ (show; "forget") |
| L4 | `boards.*.ssh.claim_key` | public key a claim writes | `B1_RUNBOOK_LINUX.md:194-211` | `~/.ssh/id_ed25519.pub` | U | | L | ✓ |
| L5 | `boards.*.ssh.jump` | ProxyJump via the hub (derived today) | `harness_manager_mps3/xvc.py:151-160`; `docs/design/XVC_DEBUG.md:350` | from the hub | B | | R | ✓ (override) |
| L6 | `debug.on_board` | `debug up` via on-board OpenOCD over an SSH forward | `lx:docs/planning/linux_lanes/GDB_SERVER_PROPOSAL.md:3,35,51-53` | on when the harness has `gdb_server` | B | | R | ✓ |
| L7 | `boards.*.install_door` | usb, hub or ethernet | `docs/design/HARNESS_DISTRIBUTION.md:437` | auto | B | | L | ✓ |
| L8 | `install.auto_revert` | revert a remote install that leaves the board dark (D6a) | `HARNESS_DISTRIBUTION.md:404,438,534-536` | on for remote doors | U | | L | ✓ (lockable) |
| L9 | `deploy.persist` | keep a deployed overlay on the card (D13) | `plat:docs/planning/HANDOVER_USD_OVERLAY_STORE.md:90,198-201` | persist (pyverify D1=B) | U/B | | L | ✓ |
| L10 | `updates.hold` | hold back a version (user and policy) | `docs/design/HM_SELF_UPDATE.md:429,459-466,580,593` | none | U + A | | L | ✓ (lockable) |
| L11 | `hubs.*.data_plane` | token-only users' route to the board | `docs/HUB_MODE.md:129-149` (undecided) | the SSH tunnel | H | | R | ✓ (when decided) |
| L12 | signing keys (U2) | who holds the release key | `docs/KEYS.md:3,14-22,67-69` | undecided | build | | none | read-only (K8) |

**Not HM settings, deliberately:**
- the board image's WireGuard (`MPS3_WG_*`), provisioning (`MPS3_IMAGE_KIND`, `MPS3_HOST_KEY_FILE`, …) and network (`MPS3_NET_MODE`) seams: `plat:src/linux_harness/sw/br2_external/board/mps3_provision.sh:12-69`;
- µSD card-detect polarity: a board strap;
- fpgahub's own `gate_ethernet`, `share_tty` and port-22 gate;
- a board's A/B slot choice: an operation, not a preference.

If HM ever provisions WireGuard, it is a hub-scoped row (L11).
