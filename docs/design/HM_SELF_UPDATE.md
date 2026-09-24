# Harness Manager self-update (over the air)

**Lane:** OTA (design + one board-free spike). **Date:** 2026-09-24. **Base:** main 056f68f.
**Asked by david:** "Can you get a separate agent to look at doing over the air updates to
harness manager itself?"

**Scope:** how Harness Manager (HM) updates itself: check, notify, download, verify, stage,
switch, restart, roll back. Linux first, then Windows and macOS. This is a design, not an
implementation. The spike code is in `scripts/spikes/ota_*`.

---

## 1. Recommendation

Keep T7's model and finish it. T7 already has the parts: a signed channel, hash-pinned
downloads, side-by-side `uv` venvs, a `current.json` pointer, and a busy rail. Q3's Linux
and macOS launcher already follows that pointer. The spike ran a real signed update from
0.1.0 to 0.1.1 on this machine, installed by the real installer, and it worked. It also
showed that the path breaks in three places before any user can use it:

1. **No release can be staged without a hashed lock that pins pyverify by URL.** T7's
   "no lock: a warning" path always fails.
2. **Nothing restarts the daemon.** The switch only takes effect "from the next start". A
   restart also changes the token, so an open app window is locked out.
3. **Rollback cannot return to the installed version.** The installer's venv is not a
   version T7 knows.

**Build next:**

- **A lead-run `make release`.** It builds the wheel, a universal hashed lock and a signed
  `channel.json`, and runs the spike's smoke install before it publishes.
- **A small daemon "apply" step:**
  1. drain the jobs;
  2. write a resume file (port, token, open boards, PTYs);
  3. exit;
  4. a helper process running the old, known-good interpreter switches the pointer, starts
     the new daemon on the same port and token, and checks `/health`;
  5. if the new daemon is unhealthy within 30 s, the helper rolls back automatically.
- **A Python launcher.** It replaces the `sed` shell shim and the Windows `.exe` copy, so
  Windows gets self-update too.

**Hosting:** use GitHub Releases on the private repo with a token for now. Keep one catalog
and one trust model with HARNESS-DIST. Once the licence is decided, a public signed-wheel
repo is a one-line URL change.

**Bootstrap:** existing 0.1.0 installs pin no keys. They need one last manual installer
run to reach the first OTA-capable release.

---

## 2. What exists today

### 2.1 End to end, step by step

| Step | State | Where |
|---|---|---|
| **Check** | Works, on demand only. No periodic check. `update.available` is published only when someone runs a check. | `services/update/service.py:177-206` |
| **Notify** | Missing. No UI code subscribes to `update.available`, and there is no banner. The app card sits inside each board's Update page. | `web/static/js/sections/update.js:162-190` |
| **Download** | Works. Resumable, sha256-checked, content-addressed cache. The token goes only to GitHub API hosts. | `services/update/download.py:1-20, 51` |
| **Verify** | Works: minisign with pinned keys, then schema, channel name, anti-rollback serial, expiry as a warning. **But `PINNED_KEYS` is empty, so every channel is refused.** | `services/update/channel.py:116-146`, `trust.py:83, 114-118` |
| **Stage** | Works, but only with a hashed lock that pins pyverify by URL (spike). The venv is built by `uv` beside the running one, and a smoke check imports it and reads its version. | `services/update/app.py:232-269` |
| **Switch** | Works: an atomic `current.json` swap. It is refused while a live process holds a board lock, a harness update journal is open, or (daemon) any job runs. | `app.py:280-293, 86-116`; `daemon/update_api.py:139-160, 287-299` |
| **Restart** | **Missing.** The CLI says "it runs from the next start" (`cli/cmd_update.py:304`). The UI says "restart harness-manager to run it" (`update.js:177`). | none |
| **Health check / auto-rollback** | **Missing.** | none |
| **Rollback** | Swaps current and previous. **It refuses when previous is the installer's venv** (spike: exit 15). It does not mark a failed version as bad, so `check` offers it again (spike). | `app.py:295-308, 337-343` |
| **Release production** | **Missing.** No release tool, no lock, no keys, no releases (`gh release list`: empty). The default source repo `SoC-Labs/mps3-platform-dist` does not exist (`gh repo view`: not found). | `channel.py:38-39`; `Makefile:64-71` (`make dist` has no lock or signing) |
| **Dev-install guard** | **Missing.** Nothing detects `pip install -e`. A developer checkout shares `~/.config/harness-manager` with the installed command, so `update app` from a checkout would switch david's installed launcher. | `service.py:210-236` (no check); `engine.py:77-84` (the same default state dir) |

**Tests:** every self-update test uses a `FakeUv` and a fake wheel (`b"PK-wheel"`), in
`tests/unit/test_t7_app.py:52` and `tests/integration/test_l4_update_api.py:365-367`. No
test ran a real `uv` or a real wheel before this spike.

### 2.2 Does T7's layout fit Q3's installer?

**Mostly, on Linux and macOS.** The shim that `install.sh` writes reads
`$HARNESS_MANAGER_STATE_DIR` (or `~/.config/harness-manager`) at
`/update/app/current.json`. It execs `versions/<current>/bin/harness-manager` and otherwise
the installer's venv (`scripts/install.sh:521-539`, `docs/INSTALL.md:86-90`). The spike
confirmed it: after the switch, `harness-manager version` printed 0.1.1, and
`daemon start` ran `.../update/app/versions/0.1.1/bin/python -m harness_manager.daemon`.
The desktop entry points at the shim (`install.sh:574`), so it follows too.

Where they disagree, and which side should win:

| # | Mismatch | Evidence | Fix (which side wins) |
|---|---|---|---|
| M1 | **Windows ignores the pointer.** The command is a copy of the venv's `harness-manager.exe`, so self-update there does nothing. | `scripts/install.ps1:352`, `INSTALL.md:88-90` | A Python launcher entry point (§7). The installer side changes. |
| M2 | **The installer's venv is not a T7 version.** Rollback after the first self-update is refused. | `app.py:295-300`; spike rc=15 | The installer registers its venv in the pointer (`installer: {version, path}`), and rollback may target it. T7 changes. |
| M3 | **Re-running the installer does not win.** A pointer set by a self-update keeps running the older self-updated version after a newer manual install. | `install.sh:541-545` only warns | The installer clears the pointer when it installs a version at least as new, and keeps the self-updated venvs as rollback targets. The installer changes. |
| M4 | **Venvs live in the state dir.** They sit in `~/.config/harness-manager/update/app/versions/` (32 MB each in the spike). Users back up and sync that dir, and `--uninstall` leaves the venvs behind. | `services/update/state.py:13`, `install.sh:221` | Move `versions/`, `wheels/`, `reqs/` and `current.json` under the install root (`$HARNESS_MANAGER_HOME`, default `~/.local/share/harness-manager`). The installer writes `install.json` there. The installer layout wins; T7 changes, with a one-time migration. |
| M5 | **The self-updater needs `uv`, but the installer does not guarantee it.** The installer falls back to venv and pip when there is no uv. On this lab machine there is no uv, so a stock install gets "UNAVAILABLE: needs uv". | `app.py:202-207`, `install.sh:393` | The installer always puts uv in its venv (`pip install uv`; the spike did exactly that), and the updater looks there first. Fallback: `python -m venv` plus `pip install --require-hashes`. |
| M6 | **Extras are lost on update.** The installer remembers `--with-serial` and `--with-app`, but the updater installs the bare wheel. The spike only kept pyserial because the lock listed it. | `install.sh:136-151`, `app.py:211-218` | The lock carries the always-on small extras (serial, ina260). A second lock asset covers `app` (pywebview). The updater reads the recorded extras. |
| M7 | **The state-dir override mismatches.** The engine honours `config.state_dir`, but the shim only reads the env var or the default. | `engine.py:77-84`, `install.sh:528` | Fixed by M4: the pointer moves into the install root. |

### 2.3 Other defects the spike and reading found (small)

- **The staging error keeps only the last line of uv's message.** The user sees
  "unsatisfiable." and not "mps3-pyverify was not found in the package registry"
  (`app.py:274`).
- **A stale `error` field survives a successful stage.** `_mark` merges, so it never clears
  the field (`app.py:196-200`). Seen in the spike's `current.json`.
- **`min_harness` is parsed but never used** (`schema.py` `AppRelease`; no reader). No
  compatibility check guards an app update (§8).
- **The app wheel must be public** (`schema.py:484`), and the channel is fetched without a
  token (`download.py`, `fetch_bytes`). Both contradict a private repo (§4.2).
- **Anti-rollback serials are keyed by channel name only** (`state.py:113-137`). A separate
  app catalog and a harness catalog both named `stable` would refuse each other (§4.6).
- **A board the daemon holds blocks its own app switch** ("this process holds X",
  `app.py:105-106`). The UI therefore asks you to close your boards first (`update.js:180`),
  and the app card lives on a board's page.
- **The CLI and daemon run different versions after a switch until a restart** (spike: CLI
  0.1.1, daemon 0.1.0). Nothing warns about it.
- **Branch channels cannot be signed.** Pinned release keys may sign only
  `stable|beta|dev` (`trust.py:45-49`), so a `dev-feat-x` channel is refused.

---

## 3. The spike: a real self-update, end to end, in isolation

**Files (new):**

- `scripts/spikes/ota_selfupdate_spike.sh`: the driver.
- `ota_env.sh`: the isolation.
- `ota_release.py`: a prototype `make release` covering keygen, pin, bump, lock and publish.
- `ota_restart.py`: a prototype of the restart with health check and auto-rollback.
- `ota_hold_lock.py`: holds a board lock from a live process.
- `ota_slow_http.py`: a 25 s job for the daemon.

**Isolation:**

- HOME, the XDG dirs, `HARNESS_MANAGER_{HOME,BIN_DIR,STATE_DIR,PTY_DIR}` and uv's cache
  all live under `/tmp/ota-spike*`.
- Ports 47815 (channel), 47816 (slow channel) and 47817 (daemon) were free.
- The key was generated for the run and thrown away.
- No real key, no network write, no board.

**Re-run:** `scripts/spikes/ota_selfupdate_spike.sh /tmp/ota-spikeN`. It takes about 3.5
minutes on this loaded machine (load average about 60).

The run was done three times: one by hand, then two with the driver. All three gave the
same results. The last driver run's output is in
`docs/design/evidence/ota_spike_2026-09-24.log`.

| # | What | Result |
|---|---|---|
| 1 | Build three wheels from `git archive HEAD`, each pinning a throwaway key in `trust.py`. 0.1.2's daemon exits at start on purpose. | OK (`uv build`) |
| 2 | Install 0.1.0 with the real `scripts/install.sh --from <wheel> --with-serial`. No uv on PATH, so it used venv and pip with Python 3.12. Start its daemon. | **OK.** Takes 20 s. `/health` reports 0.1.0. `update check` verifies the signature and offers 0.1.1. |
| 3 | Serial 1: 0.1.1 **without a lock** | **BROKE** (exit 6). uv under `--require-hashes` cannot resolve `mps3-pyverify`, which is not on PyPI. T7's no-lock path is dead. |
| 4 | Serial 2: 0.1.1 with a universal hashed lock (`uv pip compile --universal --generate-hashes`, 22 packages) and pyverify pinned as `@ http://…/assets/mps3_pyverify-0.1.0-py3-none-any.whl --hash=…`. A live process holds a board lock. | **OK.** It stages in about 3 s (warm cache) beside the running venv. The venv holds `harness-manager 0.1.1`, `mps3-pyverify 0.1.0` and `pyserial 3.5`. The switch is **refused, HELD, exit 4**, naming the lock holder. |
| 5 | Daemon path: a 25 s `update_check` job runs, then `POST /api/v1/update/app` | **409 HELD** "cannot update the app while update_check job … runs". After the job, the same POST gives a job with `switched: true`. |
| 6 | Right after the switch | The daemon **still runs 0.1.0** (`/health`), while the command runs 0.1.1. Stop and start through the launcher: the daemon runs 0.1.1 from `versions/0.1.1`. |
| 7 | `update rollback --app` right after the first switch | **BROKE** (exit 15): "no previous version". The installer's 0.1.0 is not a T7 version (M2). |
| 8 | Serial 3: 0.1.2 is staged and switched, then `ota_restart.py` runs | 0.1.2's daemon fails at start. The prototype calls T7's own `AppUpdater.rollback()` and restarts 0.1.1 **on the same port**: "rolled-back" in 2.6 to 13.6 s, depending on load. **But `update check` offers 0.1.2 again** (no bad-version mark). |
| 9 | `update app --version 0.1.0` (signed history), then `rollback --app`, then clear the pointer | All three work. Each restart takes 2 to 10 s. With a cleared pointer, the launcher falls back to the installer's 0.1.0. |
| 10 | Token across a restart | **Changes on every start** (`daemon/server.py:277`). The old token then gets HTTP 401, so an open app window is locked out. |

**What it proves:**

- T7 and Q3 fit on Linux, apart from M2 to M6.
- The release format needs a lock that pins pyverify.
- Restart and health-check rollback work as a separate helper process using existing verbs.
- The missing pieces are all small.

---

## 4. Release pipeline

### 4.1 What a release contains

For app version `V`, all listed in the signed channel:

| Asset | Made by | Why |
|---|---|---|
| `harness_manager-V-py3-none-any.whl` | `uv build` from the tagged commit, with `SOURCE_DATE_EPOCH` = the commit time | the app |
| `harness_manager-V.lock.txt` | `uv pip compile --universal --generate-hashes -c constraints.txt --extra serial --extra ina260` | every dependency pinned by hash, for every OS (markers) |
| `harness_manager-V.app.lock.txt` (optional) | the same, `--extra app` | pywebview and its OS-specific dependencies |
| `mps3_pyverify-X-py3-none-any.whl` | copied from `vendor/` | not on PyPI; see below |
| `channel.json` + `channel.json.minisig` | the release tool; minisign | the signed index |

**pyverify:** add an artifact kind `dep` to the app release schema (`schema.py`
`_app_release`; unknown kinds are already ignored). T7's `Downloader` fetches the dep
wheels, with the token when access is `github-token`, and checks their hashes. The stage
step then rewrites the lock's `mps3-pyverify` line to the verified local file. The spike
used an absolute `@ http://` URL instead. That works only for a public host, and it ties
the lock to one host.

No release has ever shipped, so the schema can change now without a version bump.

### 4.2 Hosting (the repo is private)

A signed channel makes the host untrusted. Hosting is only "which URL" plus "does it need
a token".

| Option | How | For | Against |
|---|---|---|---|
| **A. GitHub Releases on the private HarnessManager repo (recommended now)** | One rolling release per channel (tag `channel-stable`, `channel-beta`, `channel-dev`) holds `channel.json(.minisig)`. Per-version releases (`v0.2.0`) hold the wheel, locks and pyverify. Fetch through `api.github.com/repos/…/releases/assets/<id>` with `Accept: application/octet-stream` and the token. | No new repo. Access follows the repo's collaborators, and users already need GitHub access to install. | **Every user needs a token.** Default to `gh auth token` when `gh` is logged in, else `HARNESS_MANAGER_GITHUB_TOKEN` or a 0600 token file. **T7 changes:** a channel fetch that can use the token, a `github-release` source type (asset-id lookup), and allowing `access: github-token` for the app wheel. |
| B. A public dist repo (`SoC-Labs/harness-manager-dist`) holding only the signed channel and wheels | Pages or raw URLs | No token, as T7 is written today (public channel and wheel). | A wheel is readable source, so this publishes HM. It needs david's licence decision first. |
| C. Hub or LAN mirror (a directory served by fpgahub, a lab share, or `file://`) | `--source` / `HARNESS_MANAGER_UPDATE_SOURCE` (exists) | Offline labs, no GitHub dependency. The Downloader already supports it. | Only on the lab network. Someone must sync it. |

**Recommendation:** A now, with C as a mirror (`make release MIRROR=hub:/srv/hm-dist`).
Move to B the day the licence is decided; clients only change their default source URL.

### 4.3 Channels and version scheme

**Version:** PEP 440, single-sourced. Today it is in two places: `pyproject.toml` and
`__init__.py`. Make `__version__` read `importlib.metadata.version()`, or use setuptools'
dynamic `attr`.

| Channel | Who publishes | Versions | Key |
|---|---|---|---|
| `stable` | the lead runs it; david approves the promotion | `0.2.0` | release key (offline) |
| `beta` | the lead | `0.3.0b1` | release key |
| `dev` | the lead, or CI once it is back | `0.3.0.dev<N>+g<sha>`, with N the commit count (monotonic, because T7 ignores the `+local` part when ordering) | `app-ci` key (dev only, `trust.py:45-49`) |

**Branches:** publish branch builds to `dev`, with the branch named in the notes. Do not
create per-branch channels: `trust.py` would refuse them.

**Staged rollout:** publishing a release does not make it current. `current` moves in a
second `make release PROMOTE=1` run, with a new serial.

### 4.4 Keys: custody, ceremony, rotation

T7 already implements the roles (`trust.py:1-20`):

- **root:** cold. It signs `keys.json` rotations only, and is pinned in the app.
- **harness-release:** signs stable, beta and dev.
- **app-ci:** signs dev only.

Revocation goes in `keys.json`. Use **one trust store for app and harness**; HARNESS-DIST
uses the same keys.

**Ceremony (david, about 30 minutes, once):**

1. On an offline or trusted laptop, run `minisign -G -p root.pub -s root.key` and
   `minisign -G -p release.pub -s release.key`, each with a strong passphrase.
2. Optionally generate `ci.pub`/`ci.key` without a passphrase, for when CI is back.
3. Put `root.key` on two encrypted USB sticks, stored in two places. Never on a networked
   machine.
4. Keep `release.key` (passphrase-protected) on david's machine only. The lead's
   `make release` asks david to sign, or runs on david's account.
5. Commit a PR that fills `PINNED_KEYS` with root and release (and ci) plus their key ids,
   and adds `docs/KEYS.md` with the ids, the dates, and where the backups are (not the keys).
6. The first release with pinned keys is the **bootstrap**: installs of 0.1.0 pin nothing,
   so they need one manual installer run.

**Rotation:**

- A new release key is added through a root-signed `keys.json` with a higher serial
  (exists: `trust.py` `apply_keys_json`).
- The old key is revoked in the same file.
- Root can only rotate by shipping a new app with a new pinned root, because roots never
  rotate over the air (`TrustStore.roots`). That is by design.

**Sign with the `minisign` CLI** so the secret key never enters a Python process. The
Python signer in `minisign.py` stays for tests and throwaway keys.

### 4.5 `make release`: local and lead-run, because CI is blocked

`scripts/release/hm_release.py` grows out of the spike's `ota_release.py`. A new
`Makefile` target (CCR) runs it:

```
make release VERSION=0.2.0 CHANNEL=beta [PROMOTE=1] [DRY=1] [MIRROR=DIR]
```

1. **Preconditions:**
   - a clean tree, HEAD tagged `v0.2.0` (not pushed yet);
   - the version is single-sourced and matches;
   - `make check` is green;
   - `CHANGELOG.md` has a 0.2.0 section, which becomes the signed `notes`.
2. **Build:** the wheel (reproducible), both locks, and the pyverify dep.
3. **Smoke (the spike, automated):**
   1. In a temp HOME, install the previous stable release with `install.sh`.
   2. Serve the new channel on 127.0.0.1, signed with a throwaway key (the throwaway build
      pins that key).
   3. Run `update app`, then apply.
   4. Check that `/health` reports V.
   5. Roll back.

   A failure stops the release. This is the only test that uses a real `uv`, real PyPI and
   a real wheel.
4. **Channel:**
   1. Fetch the live `channel.json` and verify it with the pinned keys.
   2. Add the release (`status: current` only with `PROMOTE=1`).
   3. Increase the serial by 1 and set `expires_at` to now + 180 days.
5. **Sign:** `minisign -S -s release.key -m channel.json -t "hm-channel beta serial 7"`,
   which prompts for the passphrase.
6. **Publish (lead-run):**
   - `gh release create v0.2.0` with the assets;
   - `gh release upload channel-beta channel.json channel.json.minisig --clobber`;
   - `git push origin v0.2.0`;
   - optionally rsync to the mirror.

   `DRY=1` stops before this step and serves the result locally.
7. **Verify:** from a clean state dir, `harness-manager update check --channel beta`
   against the published source must report the new serial.

**Time:** about 5 minutes per release, plus about 3 minutes for the smoke install.

### 4.6 One catalog and trust model with HARNESS-DIST

**Agree with HARNESS-DIST on:**

- **One trust store.** Same pinned roots, same `keys.json` rotation and revocation, same
  minisign.
- **One schema** (`harness-manager-channel` v1). It already allows app-only or
  harness-only documents (`schema.py` `parse_channel`).
- **Two catalogs.** An **app catalog** (board-agnostic, `app` section only), and **one
  harness catalog per board pack** (`harness` section only, `board.pack` set). T7's
  default today puts the app into the MPS3 platform channel. With a second pack (KR260,
  HAPS), each pack channel would need a copy of the app releases.
- **Serials keyed by `(catalog id, channel)`, not by channel name.** This is a one-line
  change in `SerialStore` plus a migration. Add a top-level `catalog` field: `hm-app`,
  `mps3-harness`.
- **One `Downloader`, cache and token handling** for both.

---

## 5. Client flow

### 5.1 Check and notify

- **Who checks:** the daemon runs a background checker.
  - First check 60 s after start, then every 6 h (±10 % jitter).
  - On failure, back off exponentially up to 24 h.
  - An offline or unreachable source is logged once and stays silent in the UI.
  - A bad signature or a serial rollback is loud: a red card that names the source.
- **The CLI checks only when asked.**
- **Result:** kept in `update/app/last_check.json`. `update.available {app, notes, channel}`
  is published when a new version appears, not on every check.
- **UI:** a top-bar badge plus a dismissible banner: "Harness Manager 0.2.0 is available.
  What's new · Update". The notes are the signed `notes` text from the channel, shown
  inline. Do not rely on `notes_url`, which a private repo cannot serve to a browser. Each
  version is dismissed separately.
- **Policy** (`update.auto` in settings): `off | notify | stage | apply-when-idle`. Default
  is **stage**: download and build the venv in the background once available (it never
  touches the running version), so "Update" is a restart of a few seconds.
  `apply-when-idle` exists for unattended lab hubs; it applies only on `stable`, and only
  from the policy file.

### 5.2 Stage (the current T7 code, fixed)

- **Download and verify** the wheel, the lock and the dep wheels (Downloader), then build
  `versions/V` with uv, hashes required (§4.1). Steps:
  1. rewrite the pyverify line to the verified local file;
  2. install the recorded extras (M6);
  3. smoke-test: `python -c "import harness_manager; print(__version__)"`, then
     `python -m harness_manager.daemon --self-test` (new; imports the daemon app and the
     board packs without binding anything).
- **Allowed while boards are open and jobs run.** Staging never touches the running venv.
  Only one stage runs at a time.
- **Refused for a developer install (§5.6)** and for a version marked `bad`.

### 5.3 Apply: drain, switch, restart, preserve state

"Apply" replaces T7's in-process switch. The daemon cannot switch safely by itself,
because its own boards count as busy (`app.py:105-106`), and something outside it must
start the new version.

```
UI "Update now"  ->  POST /update/app/apply {version}
daemon: DRAINING  (new jobs 409 HELD "restarting to 0.2.0"; running jobs finish; cancel = abort)
daemon: writes update/app/resume.json (0600): port, listen, token, open boards + consoles
        with PTYs, target version, from version, deadline
daemon: spawns the apply helper (detached, OLD interpreter = known good), then exits cleanly
        (closes boards: locks released, OpenOCD stopped; leases are NOT released)
helper: waits for the old pid to go -> switch (current.json; busy = other processes' locks)
        -> starts NEW daemon with --resume resume.json (same port, same token)
        -> /health must answer version == V within 30 s and stay up 10 s
        -> else: pointer back, start OLD daemon with the same resume, mark V "bad"
        -> writes update/app/last_apply.json {from, to, result, why, at}
new daemon: reads resume.json -> reopens the boards -> PTYs at the same paths -> publishes
        update.app.done (or update.app.failed from last_apply.json) -> deletes resume.json
UI: the event socket reconnects by itself (api.js:324-339, same port + token), sees
        /health version != the version it loaded -> location.reload()
```

What each kind of session gets:

| State | During apply | Why it is safe |
|---|---|---|
| Running jobs (deploy, swap, SD write, harness update, power cycle) | **Wait** (drain). No timeout by default; the user can cancel the apply. | Never interrupted. |
| Another process's board lock (a CLI deploy in another terminal) | **Wait.** The helper's switch waits for it, and the daemon's drain shows it. | T7's rail, kept (spike step 4). |
| Hub leases | **Kept.** The token file is in `<state>/leases/` and the TTL is 3600 s (`services/lease.py:18, 60`). The new daemon heartbeats once it reopens the board. | A restart of 3 to 15 s is far below the TTL / 3 heartbeat. |
| Open boards | Closed and **reopened** from `resume.json`. | Board locks go with the old pid and are re-taken by the new daemon. |
| Consoles (TCP to the shell) | Reconnected. The replay buffer covers the gap. | Board side unaffected. |
| **PTYs for `screen`** | **Same path, new `/dev/pts/N`.** `screen` sees its line hang up and must be re-run on the same path. Before closing, the daemon writes one line into each PTY: `[harness-manager restarting to 0.2.0: re-run screen <path>]`. | Paths are stable (`services/pty.py:1-20`); the device is not. Phase 2 (D4): hand the PTY master and slave fds to the new daemon with `SCM_RIGHTS`, so `screen` never notices. |
| GDB on OpenOCD, XVC/ILA clients, open console websockets | **Soft busy.** The apply dialog lists them ("1 GDB session will disconnect"). The user confirms or waits. | These are interactive; the user decides. |
| The app window | Stays open. Reconnects and reloads by itself. | Same port and token. |

**New daemon flags (CCR on `daemon/server.py`):**

- `--resume FILE`: reuse the port and token, and reopen the boards.
- `--self-test`.

The token then travels in a 0600 file, never in argv (visible in `ps`).

**CLI-only users (no daemon):** `update app` stages and switches when no board lock is held
(works today, spike step 4). The next command runs the new version. There is nothing to
restart.

### 5.4 Health check and automatic rollback

- **Budget:** `/health` must answer with `version == V` within 30 s (configurable) and stay
  up for 10 s. The spike's prototype restarted in 2 to 14 s on a machine with load about
  60.
- **On failure:**
  1. restore the pointer and start the old version with the same resume file;
  2. mark V `bad` in `current.json` (`versions[V].state = "bad"`, with the reason);
  3. leave `update.app.failed {version, phase: "health", reason}` in `last_apply.json`, so
     the UI shows it after the reload.

  `offer()` skips bad versions until a newer one appears. This fixes spike step 8.
- **Crash after the window:** if the new daemon crashes later, the next `daemon start` or
  `app` run notices that `last_apply.json` is less than 10 minutes old and that the daemon
  died. It offers a one-click rollback. It does not do it silently.
- **Escape hatch:** `HARNESS_MANAGER_USE_INSTALLED=1 harness-manager …` makes the launcher
  run the installer's venv. That version can run `update rollback --app` even when the new
  version's CLI cannot import.

### 5.5 Manual rollback, prune, hold back

- **`update rollback --app`** switches to the previous good version, and then applies it
  (with a restart) when a daemon runs. The previous version may be the installer's venv
  (M2).
- **Keep the current, the previous and one more**; `update prune` (exists, `app.py:316-335`).
  Never prune a version whose Python is running (Windows cannot delete it anyway).
- **`update app --version V`** installs an older signed release (works, spike step 9).
- **Hold back:** `update settings --hold 0.2` stops offers above 0.2.x until released. It
  is shown in the UI.

### 5.6 Developer installs never self-update

The updater refuses to run (REFUSED, and the checker is off) when any of these hold:

- the running `harness_manager` distribution has `direct_url.json` with
  `dir_info.editable: true`;
- `sys.prefix` is neither the installer's venv (from `install.json`) nor
  `versions/<v>`;
- `HARNESS_MANAGER_SELF_UPDATE=0`.

The message is: "this is a developer install (`pip install -e`); update it with git". The
UI shows "Developer install: updates are off".

This matters here: david's dev venv and his installed command share
`~/.config/harness-manager` by default. So a dev `update app` today would switch the
installed launcher. M4 (pointer in the install root) also removes that coupling.

---

## 6. Multi-user and shared installs

- **Today:** the installer is per-user only. It refuses to run under sudo
  (`install.sh:90-94`). The state dir, daemon, port and PTY dir are all per-user
  (`/tmp/harness-manager-$USER`).
  - Several users on one lab machine each have their own install and update on their own.
  - The cost is about 30 to 50 MB per staged version per user, which uv's cache can share
    only per user. That is fine.
- **Recommended:** keep per-user installs, and add an optional admin policy file
  `/etc/harness-manager/policy.toml` (on Windows, `%ProgramData%\harness-manager\policy.toml`):
  - `self_update = off|notify|stage`;
  - `channel = stable`;
  - `source = <mirror URL>`;
  - `hold = "0.2"`.

  The user's own settings cannot loosen it. This covers managed lab machines and hubs.
- **System-wide install (not recommended now):** an admin-owned
  `/opt/harness-manager/{versions,current.json}`. Only the admin (or a group) could switch;
  users would see "0.2.0 is available: ask your admin". Each user's daemon would pick it
  up at its next restart, and the UI would show "a newer version is installed: restart to
  use it". This is more machinery (a setgid group, a policy of who restarts whom) for a
  case nobody has asked for.

---

## 7. Per-OS details

**Launcher (all OSes).** Replace the `sed` shell shim and the Windows `.exe` copy with one
Python entry point in the installer's venv: `harness_manager._launch:main`, which becomes
the `harness-manager` console script (CCR on `pyproject.toml`). It:

1. reads `<install root>/current.json`;
2. if the pointer names another version, runs `versions/<v>/{bin/python,Scripts/python.exe}
   -m harness_manager.cli …`:
   - on POSIX, with `os.execv` (same pid, same terminal);
   - on Windows, with `subprocess` plus exit-code and Ctrl-C passthrough (Windows has no
     real exec);
3. otherwise imports the CLI directly.

It must stay tiny and stable, because it is updated only when the installer is re-run.

### Linux

- **Service:** there is no systemd unit today (no hits in the tree). The daemon is a
  detached process started by the CLI or app.
- **Not needed for OTA.** The apply helper uses `daemon stop` / `daemon start`.
- **If a user unit is added later** (`systemctl --user`, D-Bus-free on ThinLinc): its
  `ExecStart` must be the launcher, and apply becomes `systemctl --user restart`. The
  resume file stays the same.
- **The desktop entry** runs the launcher, so it follows the pointer with no change
  (`install.sh:574`).

### Windows

- **In-use files:** the side-by-side venv avoids locked files, because a new version is a
  new directory and the running one is never modified.
- **Prune:** remaining locks only matter to prune, which must skip a version whose
  `python.exe` is running.
- **Launcher:** the Python launcher fixes M1.
- **uv:** `install.ps1` installs uv in its venv.
- **Daemon start:** `daemon_python()` already starts the base interpreter with
  `__PYVENV_LAUNCHER__`, so the pid in `daemon.json` is the real one
  (`daemon/control.py:131-146`). The same works for `versions\V\Scripts\python.exe`.
- **Service:** no Windows service. It would run in session 0 with no desktop and no user
  profile tokens. Autostart, if wanted, is a per-user Task Scheduler "at logon" entry
  running `harness-manager daemon start`.
- **SmartScreen and Defender** do not apply to venv files built locally by uv. Only a
  downloaded `.exe` installer would be affected; we do not ship one.

### macOS

- **No `/proc`:**
  - `_cmdline` falls back to `ps` (`control.py:236-252`);
  - `install.sh`'s `service_gone` reads `/proc/$pid/stat` only when it exists
    (`install.sh:165-176`).
- **Zombies (Q3's finding):** a stopped daemon stays a zombie until its parent reaps it.
  The apply helper is a separate detached process, and the daemon it starts is re-parented
  to launchd, so neither leaves zombies. Wait for the old daemon by polling `/health` and
  the pid through `pid_alive` plus `ps -o stat`, not `kill -0` alone.
- **Quarantine and Gatekeeper:**
  - Files fetched by urllib and built by uv get no `com.apple.quarantine` attribute. Only
    LaunchServices-aware apps (browsers) set it.
  - Pure-Python and PyPI binary wheels load without notarisation.
  - Keep it that way: **do not ship a `.app` bundle.** A bundle would need signing and
    notarisation for every update.
  - If a Dock icon is wanted, the installer can write a small locally-generated `.app`
    that runs the launcher. A locally created bundle is not quarantined.
- **launchd:** optional LaunchAgent (`RunAtLoad`, no `KeepAlive`, so it does not fight
  apply). Not needed for OTA.

---

## 8. Compatibility: HM version, harness wire protocol, board packs

- **App needs a newer harness:** the app release declares
  `compat: {net_protocols: ["0.9", "0.10", "0.11"], packs: {"mps3": ">=1"}}`. It replaces
  the unused `min_harness`.
  - At offer time, the client compares this with the protocol each **known** board last
    reported: the boards store and last identities, plus `/boards` when the daemon runs.
  - If the new app would drop a board's protocol, the offer carries a warning: "0.3.0 no
    longer talks to harness 1.0 (mps3-02): update that board's harness first, or hold
    back".
  - It is a blocker only under the `apply-when-idle` policy.
- **Harness needs a newer app:** this exists. The harness planner blocks on
  `compat.min_app` (`planner.py:218-219`). Add a link from that blocker to the app update
  ("update Harness Manager to ≥ 0.3.0 first").
- **Old harness needs an old HM:** keep that version by holding back, or install it with
  `update app --version V` from signed history (works, spike step 9).
- **Board packs** are in-tree today, so they update with the app.
  - When `harness-manager-board-mps3` splits out, it becomes one more locked dependency in
    the app release.
  - Pack versions are pinned by the lock; there is no separate pack channel.
  - The pack's protocol range feeds the check above.
- **pyverify** (the protocol codec) is pinned exactly by the lock and shipped as a `dep`
  artifact, so an app release always carries the codec it was tested with.

---

## 9. Product surface

### CLI

| Command | Exists? | Change |
|---|---|---|
| `update check` | yes (`CHANNEL SERIAL HARNESS_CURRENT APP_CURRENT APP_UPDATE …`) | add columns `APP_STAGED`, `APP_POLICY` (append-only TSV) |
| `update app [--version V] [--stage-only] [--yes]` | yes | stages by default; switches only when no daemon runs, else points to `--apply` |
| `update app --apply` / `update apply [V]` | **new** | drain, switch and restart through the daemon (or stage and switch with no daemon); `--no-wait` |
| `update rollback --app` | yes | may target the installer's version; applies with a restart |
| `update status` | **new** | pointer, versions (state, bad), last check, last apply, policy, dev-install flag |
| `update settings [--channel C] [--auto P] [--hold V]` | **new** | writes `<state>/update/settings.json`; the policy file can override it |
| `update prune` | code exists (`app.py:316`), no verb | expose it |
| `daemon restart` | **new** | the generic resume restart; apply uses it |

### API (all additive; CCR on `docs/API.md`)

| Route | |
|---|---|
| `GET /update/app` | `{running, pointer, versions, available, staged, pending_apply, last_check, last_apply, policy, dev_install}` |
| `POST /update/app` `{version?, channel?, source?}` | stage (202 job `update_app`). It no longer switches inside the daemon. |
| `POST /update/app/apply` `{version, drain_timeout_s?}` | 202. The daemon drains, then restarts. `409 HELD` if another apply is pending. |
| `POST /update/app/cancel` | cancel a pending or draining apply |
| `POST /update/app/rollback` | exists; becomes a rollback plus apply |
| `PUT /update/settings` | channel, auto, hold |
| `GET /health` | add `api`, `static_hash` and `resumed_from` |

**Events:**

- `update.available` exists.
- New: `update.app.staged {version}`, `update.app.draining {version, waiting_on: [jobs]}`,
  `update.app.restarting {from, to, eta_s}`, `update.app.done {from, to, result: switched|rolled-back}`,
  `update.app.failed {version, phase, reason}`.

### UI (`web/static/js/sections/update.js`, `app.js`, `api.js`)

- **Banner and badge** from `update.available`. Show the notes inline, and a Dismiss
  button per version.
- **Settings → Updates card.** Move it out of the per-board Update page, because it is not
  about a board. It holds:
  - channel: stable / beta / dev;
  - policy: off / notify / stage;
  - "Check now" and the last check (time, error);
  - the versions on disk with Roll back;
  - hold back;
  - the dev-install notice.
- **Progress:**
  1. stage (download bytes, building the environment);
  2. "Restart now" or "When idle", listing the soft-busy sessions;
  3. drain ("waiting for deploy on mps3-01");
  4. a "Restarting… reconnecting" overlay with a countdown up to the health budget;
  5. an auto-reload when `/health.version` changes;
  6. a toast: "Updated to 0.2.0", or "0.2.0 did not start (reason); rolled back to 0.1.1".

---

## 10. Decisions for david

Recommendation first in each.

| # | Decision | Options (recommended first) |
|---|---|---|
| D1 | **Hosting** | **(a) Private GitHub Releases plus a token** (`gh auth token` or env), with a hub mirror. (b) A public signed-wheel repo: no token, but HM becomes readable, so decide the licence first. (c) Hub/LAN mirror only: offline-friendly, but external owners get no updates. |
| D2 | **Key custody** | **(a) Offline minisign keys. Release key passphrase-protected on david's machine; root cold on two encrypted USB sticks; lead-run `make release` asks david to sign stable.** An unattended `app-ci` key for dev when CI returns. (b) Release key with the lead too: faster beta and dev, weaker custody. (c) Everything in a GitHub Actions secret: no (CI is blocked, and it is the weakest custody). |
| D3 | **Default update policy** | **(a) Notify, auto-stage, apply on click (with drain).** (b) Notify only (a slower update: stage happens on click). (c) Auto-apply when idle, only through the lab policy file for unattended hubs. |
| D4 | **`screen` across a restart** | **(a) Phase 1: same PTY paths plus a re-attach notice; fd handover later if it annoys anyone.** (b) `SCM_RIGHTS` handover now (+~1.5 days, POSIX only). (c) Treat an attached `screen` as busy and wait until it detaches (it may never detach). |
| D5 | **Launcher / Windows** | **(a) A Python launcher entry point for all OSes (fixes Windows, replaces the `sed` shim).** (b) A `.cmd` shim on Windows only (quoting and "Terminate batch job?" problems). (c) No self-update on Windows for now; the installer re-run is the update. |
| D6 | **Shared machines** | **(a) Per-user installs plus an optional admin policy file.** (b) A system-wide install with admin-only switching (more machinery, no user asking for it). |

---

## 11. Lane plan

Order: R and C first (a real release becomes possible), then L (migration), then D, then U.
T runs throughout.

| Lane | Scope (files it owns) | CCRs on shared files | Hours |
|---|---|---|---|
| **OTA-R** release tool | `scripts/release/**` (from `scripts/spikes/ota_release.py`), `docs/RELEASING.md`, `docs/KEYS.md`, `CHANGELOG.md` notes format | `Makefile` (release target); `pyproject.toml` + `__init__.py` (single-source version) | 6 |
| **OTA-C** client core | `services/update/app.py`, `service.py`, `schema.py` (`dep` artifacts, `notes`, `compat`, `catalog`), `state.py` (serials keyed by catalog: **agree with HARNESS-DIST first**), `download.py` (token for channel, github-release source) | none expected | 9 |
| **OTA-L** launcher and installers | new `src/harness_manager/_launch.py`, `scripts/install.sh`, `scripts/install.ps1`, `scripts/smoke_install.sh`, the M2/M3/M4/M5/M6 migration, `HARNESS_MANAGER_USE_INSTALLED` | `pyproject.toml` (console-script entry), `docs/INSTALL.md` | 7 |
| **OTA-D** daemon apply and restart | new `daemon/update_apply.py` (helper), `daemon/update_api.py` (apply, cancel, status, settings routes), the periodic checker | `daemon/server.py` (`--resume`, `--self-test`, token reuse); `daemon/jobs.py` (drain mode); `services/console.py` + `services/pty.py` (reopen PTYs at the same path, re-attach notice); `daemon/control.py` (`restart`); `docs/API.md`, `docs/CONTRACTS.md` (events) | 12 |
| **OTA-U** UI | `web/static/js/sections/update.js` (Settings card, progress, overlay) | `web/static/js/app.js` (banner, version-change reload), `api.js` (routes) | 6 |
| **OTA-T** tests and CI | `tests/integration/test_ota_real_uv.py` (the spike as a `packaging`/`slow` test), fake-uv unit tests for the new pieces, macOS and Windows CI jobs once Actions is unblocked | `.github/workflows/*` | 5 |
| **Keys (david)** | the ceremony (§4.4) and the PR that pins the keys | `services/update/trust.py` | 0.5 (david) |

**Total:** about 45 agent-hours. With R, C and L in parallel, the first real OTA release
(0.2.0, the "last manual install") is about 2 working days away. The seamless restart
(D and U) follows in about 2 more.

**Blocked on david:** D1 (hosting) blocks C's `download.py` work and R's publish step.
D2 plus the ceremony block the first real release; everything else can proceed with
throwaway keys.

---

## Appendix: spike files

| File | What |
|---|---|
| `scripts/spikes/ota_selfupdate_spike.sh` | The driver; `bash scripts/spikes/ota_selfupdate_spike.sh /tmp/ota-spikeN`. It stops its own daemon and servers on exit. |
| `scripts/spikes/ota_env.sh` | Isolation: HOME, XDG, state, PTY, install root, bin dir, ports, uv. |
| `scripts/spikes/ota_release.py` | Prototype release tool: `keygen`, `pin`, `bump`, `lock`, `publish` (throwaway keys only). |
| `scripts/spikes/ota_restart.py` | Prototype apply helper: stop, start on the same port, health, and T7 `rollback()` on failure. |
| `scripts/spikes/ota_hold_lock.py` | Holds a board `SessionLock` from a live process. |
| `scripts/spikes/ota_slow_http.py` | A 25 s channel, which keeps a daemon job running. |
