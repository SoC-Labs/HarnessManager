# Harness versions from the web: publish, catalogue, verify, install, roll back

**Lane HARNESS-DIST, 2026-09-24.** Board-free design plus one spike.

**Sister lanes:**
- **OTA** (HM self-update). Its design is `docs/design/HM_SELF_UPDATE.md` on `team/ota`, 74a022b. **§4 below is one shared model with OTA's §4.6**: one trust store, one schema, one downloader, one release tool, and the same hosting. The one deliberate difference is the Arm-IP access list (§4.1).
- **KIT-STORE** (the DUT build kit, `docs/design/DUT_BUILD_KIT_STORAGE.md` on `team/kit-store`). Kits ride the harness catalogue as a `host-kit` component.

This page owns how a *harness* version is published, found, fetched, checked, installed and undone. A harness version is what runs on the MPS3's FPGA:
- the static shell with its baked firmware;
- its overlays;
- the config-SD files;
- for the Linux harness, the OS image.

david's question: *"Can you get an agent to look at a way of implementing a methodology of being able to download different harness versions from the web? Can we load these from harness manager?"*

## 1. The answer

**Yes. Most of the loading half already exists.** Lane T7 built a signed channel, a verified download, a planner, and an installer with a backup and a witnessed reboot.

The spike published four fake releases in the real formats and served them on 127.0.0.1. T7's own code then did the following on the virtual MPS3:
1. listed the releases;
2. planned each version;
3. installed a firmware-only update;
4. downgraded across statics, with the typed re-key consent;
5. rolled back from the SD backup.

**What is missing is the other half and the edges:**
1. **Nobody publishes.**
   - There is no release command.
   - Lane G is only a plan (platform `docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md:415-510`).
   - The default source repo does not exist.
   - The pinned keys are empty.
2. **The board cannot say which release it runs.**
   - Every firmware since v0.8 answers `version.harness = "1.0.0"`.
   - T7 matches releases by that field, so it names the wrong release (spike P2).
   - A release that carries its tag can never be confirmed (P6).
3. **There is no catalogue.**
   - The API and the UI can only install the channel's *current* release.
   - They cannot pick a version or roll back to one.
4. **Remote installs have no door.**
   - Through the hub, fpgahub can write `nanosoc.bit` and HM can pace the REBOOT.
   - HM has no adapter joining the two (P14).
5. **A few rails break at scale:**
   - two catalogues collide in one serial store (P8);
   - a private index cannot be fetched (P9);
   - an offline mirror cannot serve absolute-URL assets (P10);
   - the schema refuses KIT-STORE's kit (P7);
   - there is no retention rule (P11) and no cache policy (P13).

**The recommendation:**
- **Publishing:** the platform's mint writes a release bundle. The **same lead-run release tool as OTA** signs it with the **same offline keys** and uploads it to the **same host** (private GitHub Releases now, with a token and a hub mirror). The Arm-IP overlays go to a separate private repo with its own access list. Nothing depends on CI.
- **The catalogue:** in HM, add a **Harness versions** catalogue on top of T7's planner and executor: a CLI noun, API routes and a UI card.
- **Two new install doors:**
  - **hub**: fpgahub's SD write plus the paced REBOOT HM already has;
  - **Linux OS slot**: T7-2.
- **The config SD becomes A/B by pointer.** An install then never rewrites the running bitstream, and a rollback is a one-line text write instead of a 12 MB one.

**Cost:** about 53 HM hours of harness-specific work (§11). The shared client and release-tool changes are OTA-C and OTA-R. The platform side is about 2–3 days (R1–R8, L1–L5).

## 2. What T7 already covers, and what is missing

| Need | T7 today | Missing |
|---|---|---|
| A signed index | `channel.json` v1 with a strict schema (`services/update/schema.py`); minisign verify with pinned keys, per-channel roles and root-signed rotation (`trust.py`); anti-rollback serial; expiry only warns (`channel.py:163-193`) | **No keys:** `PINNED_KEYS = ()` (`trust.py:83`), so every channel is refused. **No publisher, and no source:** the default `SoC-Labs/mps3-platform-dist` (`channel.py:85`) does not resolve on GitHub |
| Multi-version listing | `releases_summary` (`service.py:52-62`); the UI table is read-only (`web/static/js/sections/update.js:195-200`) | Per-release verdicts for the selected board, a "what changes" diff, notes, pins, history (spike P3) |
| Choose a version | CLI `--version` (`cli/cmd_update.py:101`); `make_plan(version=)` marks a downgrade "ROLLBACK … from the signed release history" (`planner.py:240-250`) | The daemon recomputes the plan **without** a version (`daemon/update_api.py:222`), and `update_check` takes none, so the UI can install only `plan.version` = current (`update.js:79-81`) |
| Which release the board runs | `match_release` by (static_id, harness, usercode) (`planner.py:165-177`) | It ignores `fw_sha`. Since the firmware always says `1.0.0`, it names the **wrong** release (P2). `confirm_identity` treats `harness version` as essential (`executor.py:146`), so a tag in `identity.harness` never confirms (P6) |
| Download | resumable, sha256-named cache, token sent only to GitHub hosts and unredirected (`download.py:155-243`) | The index fetch sends no token (`download.py:108-143`, P9). No mirror lookup by sha (P10). No eviction or pinning (P13) |
| Bundle checks | no `.ebf`; no MCC command files; the SD tree; board rev; `.bit` part + USERID; overlays keyed + CRC; Arm IP never public (`bundle.py:285-363`) | The OS-slot frame check is UNCHECKED "until FLOW_CONTRACT lands" (`bundle.py:406-411`). It has landed (`-lx` `FLOW_CONTRACT.md` §0.1–0.2) |
| Local install (Debug USB) | backup gate → journaled write, no client timeout, never retried (`harness_manager_mps3/sd.py` module doc) → paced, witnessed REBOOT (`mcc.py`) → confirm identity → `installed` or `written-not-running` (`executor.py:305-481`) | nothing (P4 and P5 PASS) |
| Rollback | restore the SD backup zip, reboot, confirm (`executor.py:549-599`) | Only the **last** install is remembered (`InstallRecords`, `state.py:144`). A rollback rewrites the whole SD (~12 MB). There is no "re-install the previous release", and a release dropped from the channel is gone (P11) |
| Remote install via the hub | the MCC share pace, 100 ms/char (`mcc.py:136-146`) | No SD door over the hub. A base update is blocked with "needs the MPS3 Debug USB" (`planner.py:298-303`, P14) |
| Linux OS slot | the `OsSlotAdapter` protocol and the executor's try-once/confirm (`os_slots.py:66-87`, `executor.py:483-524`) | The adapter itself (CCR T7-2). The first bare-metal → Linux install must provision a slot from stage0 **rescue**, and nothing models that |
| Several boards | one `board` spec per channel; the planner blocks another pack (`planner.py:216-217`) | Serials are keyed by channel name only (`state.py:107-140`, P8). Keys may sign only `stable`, `beta` and `dev` (`trust.py:42-49`) |
| Kits | — | the schema refuses `host-kit` / `rm-kit` (`schema.py:53-65`, P7) |
| Lease and consent | a re-key needs the typed `REKEY <static_id>` (`planner.py:98-109`); the daemon's board gates | No lease check anywhere in `services/update/`. The consent does not name the board or who is interrupted |

## 3. The version model

### 3.1 What a harness release is

A **harness release** is one signed entry in the harness catalogue: `version` plus `identity` plus `components`. Each component goes through one **door** (T7's `target`):

| Door | Goes through it | Bare metal | Linux (mint 3) |
|---|---|---|---|
| `mcc-sd`: the config SD, which the MCC loads at power-on or REBOOT | the static and its baked boot code | `config_rm_greybox_fw.bit` (the static + MicroBlaze firmware in BRAM), installed as `nanosoc.bit`. `config.txt`, `board.txt` and `nanosoc.txt` do not change per static | `config_rm_greybox_stage0.bit` (the static + stage0, baked) |
| `user-usd`: the board writes its own µSD | the OS | — | `linux_slot.img` (S0LB v2, ≤ 64 MiB) into slot A or B |
| `host-store`: HM's content store; the parts reach the board later over 6910 | overlays, openocd cfg, identity files, DUT firmware | overlays keyed to the static | the same |
| `host-kit` (new, from KIT-STORE) | the DUT build kit, fetched on demand and never deployed | one per static | one per static |

**Measured sizes:**
- The fw-baked `.bit` is 12,433,250 B (`fielded/0x72BB0A36/mint.json`). It is already compressed, so zip gains little.
- Partials are 1.25–2.9 MB and clearings 62–227 KB.
- A Linux slot image is about 24 MB.
- A kit is 10 MB for bare metal and 38 MB for Linux (KIT-STORE §11).
- **A release totals about 15–35 MB bare metal and 40–75 MB Linux.** GitHub Release assets allow 2 GiB each.

### 3.2 Identity on the wire: the "1.0.0" problem

**What the board answers:**
- `ping.shell_id`, the static_id.
- `version` on 6900, which gives `{harness, ver32, sha, dirty, features, usr_access, skew, impl?, usercode?}` (`net-protocol.md:739-823`; Linux NP:816-887).

**Why that does not name a release:**
- **`harness` is `1.0.0` on every image since v0.8.** That includes the fielded 0x72BB0A36 (released as tag v1.1.0), the ILA mint bake and the Linux harnessd (`/VERSION` = `1.0.0`; `tests/fakes/virtual_board.py:91-93`).
- **"v0.11" is the net-protocol version**, not the harness version.
- `mint.json` records `static_usercode.harness_version: "1.0.0"` and has no firmware-version field.
- The fielded v0.11 image (`config_rm_greybox_fw_v011.bit`, reply sha `987cf264`) appears only in `MANIFEST.md5` and the README.

**So the model separates two things:**
- **The release version** is `releases[].version`: a platform tag such as `v1.0.0` (@0130a3a) or `v1.1.0` (@cbf96fa). It is for humans, and it orders the catalogue.
- **The wire identity** (`identity`) is what the board will report. HM matches and confirms against it:
  - `static_id`: the CRC-32 of the locked static DCP; it decides whether an overlay fits;
  - `usercode`: the `.bit` UserID, i.e. which implementation run;
  - `fw_sha`: the firmware git sha (bare metal) or the harnessd/image sha (Linux);
  - `ver32` / `usr_access`: the fabric's HARNESS_VER32;
  - `impl`, `proto`, `features`.

**Rules. H2 builds rules 1–3 in HM; rule 4 is a platform request:**
1. The publisher writes `identity.harness` as what the firmware **reports**, never the tag. Today that is `1.0.0`. With the tag, every install ends `written-not-running` (P6).
2. `match_release` also compares `fw_sha` (8-hex prefix) when both sides have one. Two releases on one static then stay distinct (P2).
3. `confirm_identity` treats `shell_id` plus (`harness` **or** `fw_sha`) as essential. A firmware-only update is then confirmed by its sha.
4. **Platform request R4:** stamp firmware `VERSION` = the release tag at bake time (Lane G's G7 USR_ACCESS re-stamp policy). Once that lands, `harness` names the release, and rule 1 becomes "harness = tag".

### 3.3 Compatibility rules

| Between | Rule | Enforced by | New |
|---|---|---|---|
| release ↔ board type | the catalogue's `board.pack` is the board's pack, and the `.bit` part is the board's part | T7 planner + `bundle.check_sd_component` | one harness catalogue per pack (§4.0) |
| release ↔ board revision | `compat.board_revs` includes the SD's `MB/HBI0309x/`. The board is hard-coded HBI0309C; the MCC reports "rev C, var A" | T7 planner and bundle checks | — |
| release ↔ MCC firmware | `compat.mcc_fw_tested` (1.3.2) | a warning | — |
| harness ↔ overlays | the overlay's `static_id` **and** `static_usercode` equal the release's. A foreign-implementation partial with the right static_id wiped the FPGA twice in July | T7 bundle checks, the deploy preflight, and the firmware (it refuses a foreign static_id before ICAP) | — |
| harness ↔ HM | `compat.min_app` ≤ the HM version (blocker); `app.min_harness` | T7 | HM declares `SUPPORTED_PROTO = (min, max_tested)`. Below min: blocker. Above max_tested: warning ("HM may not know its verbs"). Verbs stay feature-gated. Matches OTA §8 |
| harness ↔ install door | a base change needs the `mcc-sd` door (local USB or the hub); an OS change needs the `user-usd` door (Linux + a card); host parts need no door | T7 blockers (USB only) | a per-board `doors` list in the catalogue (§5) |
| harness ↔ harness | a different `static_id` is a **re-key**: typed consent, and every overlay or RM keyed to the old static stops loading. A change of `impl` (bare metal ↔ Linux) is a re-key plus a change of door | T7 | the publisher derives `rekey` and `replaces_static_ids` (the spike publisher does) |
| harness ↔ DUT kit | the kit's `static_id` = the CRC-32 of its DCP = the board's `shell_id`. `kit.vivado.release` must match the user's Vivado: 2024.1 for bare metal, **2026.1 for Linux mint 3** | KIT-STORE K2/K3 | a re-key plan names the new kit and any Vivado change ("your DUT RMs must be re-linked with Vivado 2026.1") |
| Linux image ↔ static | the image's `provisioned.static_id` = the fabric's static_id. The board refuses a mismatch anyway | FLOW §0.2 + the board | HM checks it first, so no push leaves HM that the board would refuse |

### 3.4 How HM learns what a board runs

1. `ping` + `version` on 6900 (`harness_manager_mps3/shell.py:419-440`).
2. UDP 6899 `identify`: `shell_id, harness, proto, impl, mode:"rescue"`. It answers while 6900 is held, and from stage0 rescue.
3. On Linux, `slot status`: `running`, `default`, and per slot `hdr_crc`, `len`, `sid`.
4. With the Debug USB: the SD's `nanosoc.bit` header (UserID) and sha. This names the base even on a dark board.

## 4. Publishing: one shared model with OTA

### 4.0 What HARNESS-DIST and OTA agree

This is the agreed shared model (OTA §4.6):

| Part | Shared model | The harness side |
|---|---|---|
| Trust | **One trust store**: the same pinned root and release keys, `keys.json` rotation and revocation, minisign | nothing extra |
| Schema | **One schema**, `harness-manager-channel` v1, with a top-level **`catalog`** field (OTA-C adds it) | Harness entries add `identity.ver32`, `vivado`, and the `host-kit`/`rm-kit` component (= KIT-STORE K4). `notes` is shared |
| Catalogues | **An app catalogue** (`hm-app`, `app` section only) and **one harness catalogue per board pack** (`mps3-harness`, `harness` section only, `board.pack` set) | the pack supplies its catalogue id and default source. KR260 or HAPS add their own catalogue; none of them copies the app releases |
| Serials | keyed by **(catalog, channel)**, not channel name (OTA-C, `state.py:107-140`) | fixes P8 |
| Channels | `stable`, `beta`, `dev`, the names the trust roles know (`trust.py:42-49`). No per-branch or per-board channel names | "testing" is `beta`. "Pinned" is per-board HM state, not a channel (§5) |
| Download | **One `Downloader`**, cache and token handling. OTA-C adds the token on the channel fetch (fixes P9) and a `github-release` source (asset-id lookup) | adds a sha-addressed mirror lookup (H5, fixes P10), plus the cache retention policy (§5) |
| Release tool | **One lead-run tool**, `scripts/release/hm_release.py` (OTA-R). It signs with the **`minisign` CLI**, so the secret key never enters Python | a `harness` front-end (H13) that ingests the platform's release bundle and runs the harness checks |

### 4.1 Where versions live

**Recommended now (D1a, the same as OTA's D1a): private GitHub Releases, with a token and a hub mirror.**

**The harness catalogue:**
- It sits on **the same host as the app catalogue**: the private `SoC-Labs/HarnessManager` repo's Releases. One collaborator list and one token (`gh auth token`, else `HARNESS_MANAGER_GITHUB_TOKEN`, else a 0600 file) then cover both.
- **One rolling release per (catalogue, channel)**, for example tag `channel-mps3-harness-stable`, holds `channel.json` and its `.minisig`. The app's is OTA's `channel-<channel>`; OTA-R can spell it `channel-hm-app-<channel>` so both read the same.
- **One release per harness version**, tag `mps3-harness-v<version>` (e.g. `mps3-harness-v1.1.0`), holds the assets. The platform's git tags stay `v1.1.0`.

**One deliberate difference from OTA: the Arm-IP access list.**
- The overlays that contain Arm Academic Access IP are nanosoc, multicore, upy and eth_ss.
- They go to a **separate private repo, `SoC-Labs/mps3-harness-aaa`**, with `access: github-token` and `repo` set per component. T7 already supports that (`schema.py` `_component`), and the planner skips such a component without a token (spike P4).
- **Why:** the people allowed to receive AAA netlists are a narrower list than HM's users. They must stay separable on the day HM or the open harness parts go public.
- This is Lane G's Arm-IP split, which david agreed on 09-23.

**When the licence is decided (OTA D1b / Lane G's public dist):**
- The open harness parts and the index move to a public repo. Clients change only their default source.
- The AAA repo stays private.

**Why GitHub Releases, and not the alternatives:**
- Releases need no CI (Actions is blocked by billing), carry no bandwidth charge, and allow 2 GiB per asset.
- Pages is out: it was never enabled, and a private repo needs a paid plan.
- An object store is out: another account and more credentials.
- The hub alone is out: standalone users cannot reach it.

**The hub:**
- It stays the **archive of record** (`/home/david/mints/<sid>/`).
- It also becomes a **lab mirror**: the channel files plus `blobs/<sha256>`, reached with `--source` / `HARNESS_MANAGER_UPDATE_MIRRORS`. HM looks an asset up there **by hash** before any URL (H5).
- A mirror copy never has to rewrite signed URLs (P10).

### 4.2 Channels

| Want | How |
|---|---|
| stable / testing | `stable` / `beta`. `dev` is the lead's scratch channel |
| per board type | one harness catalogue per pack, keyed by `catalog`; the pack supplies the default source |
| pinned | not a channel: a per-board pin in HM state. `check` never proposes a release past the pin |
| an old release | stays listed until retired (§4.5); rollback-by-version needs it |

### 4.3 Signing and the key ceremony

The keys are **OTA's §4.4 ceremony, done once for both catalogues**:
- **root** is cold, on two encrypted USB sticks in two places, and signs only `keys.json`.
- **harness-release** is passphrase-protected on david's machine and signs `stable`, `beta` and `dev` for both catalogues.
- **app-ci** is for `dev`, once CI is back.

**What harness releases add to it:**
- **stable is signed by david.** The lead's run of the release tool stops and asks for the passphrase.
- **The Linux lane's later board-side check** ("minisign may be layered later, aligned with the update channel", SLOT_VERB_DRAFT §5.7) uses **the same release key**. The board pins its public half in the image (§9).

### 4.4 Who publishes: the lead-run release tool (no CI)

```
mint (platform FLOW, R1)                          release (OTA-R's tool + H13's harness front-end; lead-run)
────────────────────────                          ──────────────────────────────────────────────────────────
make -C fpga/dfx release-bundle   ──►  prod/release/                  hm_release.py harness stage --bundle DIR --version 1.2.0
  bundle.json (identity + files)        bundle.json                      │ re-verify with HM's own bundle checks against a
  mps3-harness-<v>-sd-HBI0309C.zip      *-sd-*.zip                       │ file:// channel (the publisher dogfoods the
  *-overlays-open.zip / -aaa.zip        *-overlays-{open,aaa}.zip        │ consumer: spike P1, P7)
  mps3-kit-<sid>.zip (KIT F1)           kit zip                          ▼
  Linux: linux_bundle.json +            linux_slot.img               hm_release.py harness sign --channel beta
         linux_slot.img                                                  │ minisign CLI, passphrase; serial+1;
  → also MINT_HUB/<sid>/release/                                         │ status/rekey/replaces derived
                                                                         ▼
                                                                 hm_release.py harness publish
                                                                   gh release create mps3-harness-v1.2.0 (open assets)
                                                                   gh release create … -R SoC-Labs/mps3-harness-aaa (AAA)
                                                                   gh release upload channel-mps3-harness-beta --clobber
                                                                   rsync to the hub mirror; verify from a clean state
                                                                 hm_release.py harness promote 1.2.0 beta→stable (re-sign only)
                                                                 hm_release.py harness withdraw 1.1.1 --reason …   (serial+1)
```

**The rules the tool enforces.** The spike's publisher already enforces the first three.
- Validate with the app's own `parse_channel` **before** signing.
- **Deterministic zips:** sorted, fixed dates and modes. Re-packing gives identical sha256.
- A published asset is never rewritten with different bytes.
- The serial only goes up.
- **Refuse:**
  - a dirty or unstamped image;
  - a USERID that is not `static_usercode`;
  - an Arm-IP part in an open component;
  - any `.ebf`;
  - a Linux bundle with `fieldable:false`.
- **Publish to `beta` first.** `promote` re-signs the same entries into `stable`, with no re-upload.
- **The smoke test**, OTA's step 3 for harnesses: serve the new channel on 127.0.0.1 with a throwaway key, then plan and install on a VirtualMps3. This is exactly the spike, and it stops the release on failure.
- `publish` ends by fetching from GitHub with a clean state.

### 4.5 Retention and withdrawal

- **Keep a release listed while its static is still fielded anywhere we know of**, plus the previous release of each `impl`.
- **Withdraw** with `status: withdrawn`. T7 then refuses to install it and flags boards that still run it.
- **Never delete.** A release dropped from the channel cannot be rolled back to by version (P11). Its assets stay on GitHub, so an archive channel via `--source` can still reach them.

## 5. The catalogue in HM

A new core service, `services/harness_catalog.py` (H6). It builds on `UpdateService` rather than replacing it.

**list.** Every release on the chosen channels (stable, plus beta if the user opts in), newest first. Each row carries a **verdict for the selected board**:
- `running`, `current`, `pinned`;
- one of `compatible`, `re-key`, `needs Debug USB`, `needs hub lease`, `needs Linux + card`, `withdrawn`, `needs newer HM`;
- the doors the install would use.

Each row is one `make_plan(version=v)`, as spike P3 shows.

**show.** For one release:
- the identity, the components with sizes, and whether each is cached;
- the notes;
- the **diff against the running identity**:
  - the static (a re-key ⇒ "every overlay and DUT RM keyed to 0x72BB0A36 stops loading; kit and Vivado change");
  - the fw sha;
  - features added or removed;
  - the protocol;
  - the overlay set: added, removed, re-keyed;
  - which SD files change;
  - whether there is an OS image.

**notes.** The shared, signed `notes` field (OTA-C). `notes_url` is only a link: HM never fetches HTML to decide anything.

**fetch.** Download and verify into the cache now, so a later install works offline.

**pin / unpin.** Per board.

**history.** An append-only `update/history/<board>.jsonl`. Each line records the version, static, fw_sha, door, backup or SD slot, and result. "Roll back to previous" reads it. Today `InstallRecords` keeps only the last install.

**cache policy.** Never evict:
- the blobs of the running, previous and pinned releases of any registered board;
- the kit for a registered board's static.

Everything else is LRU above a cap, default 2 GB. OTA's app venvs are separate, and their prune is OTA's.

**offline.** `harness mirror --to DIR [--channel …] [--versions …]` writes the channel files plus `blobs/<sha256>`. The downloader tries each directory in `HARNESS_MANAGER_UPDATE_MIRRORS` by hash before any URL.

## 6. Install paths

### (a) The Debug USB is plugged into this machine

**Door and steps:**
- T7 today: back up the SD, journaled write, read back, paced `REBOOT` on tty_00, confirm identity.
- With SD A/B (§6.1): write the inactive image, read it back, flip `F0FILE`, then REBOOT.

**Known traps:**
- 12 MB over USB-MSC takes about 68 s. There is no client timeout, and the write is never retried (sd.py).
- The DAPLink volume looks like an SD card.
- An `.ebf` on the card reflashes the MCC.
- MCC command files act as soon as they land.

**Guard:**
- sd.py already refuses all four traps.
- New: the confirm step names the board and every open session or debugger, e.g. "this reprograms mps3-01; 2 console sessions and a debug session will drop".

### (b) Remote, through the hub (the hub holds the Debug USB)

**Door and steps:** a new MPS3 door, `hub_sd` (H10).
1. Check that the lease is **ours**.
2. Upload the verified `nanosoc.bit`.
3. Run `fpgahub target program <tgt> <bit> --method sd --force`, as one request only.
4. Wait for **completion**, never the HTTP reply. Completion is the `board.program_completed` event, or the journal line `ok=True … sha256=<12hex>`.
5. Paced REBOOT over the MCC share: 100 ms per character, one reader, CR first (`mcc.py:136-146`).
6. Confirm over the tunnel.

**Known traps:**
- The client times out at 30 s while the daemon keeps writing. **That timeout is not a failure.** A retry plus a reset mid-write once left the board dark.
- Without `--force`, the write is silently skipped when that sha is already loaded.
- A human mounting the SD on the hub starves the MCC.
- `reconcile-macs --apply` breaks SD discovery while `--list` still says "Available: yes".
- A second reader on tty_00 splits the REBOOT.

**Guard:**
- One install at a time per board. An HTTP timeout is shown as "writing: waiting for completion", and completion means the event or journal sha equals the release's `.bit` sha. `--force` is always passed.
- **Allowed only when the SD delta is exactly `MB/HBI0309C/Nanosoc/nanosoc.bit`.** That holds for every static so far: A/B/C differ only in `BOARD:`, and the Linux `mcc_sd` door is one `.bit`.
- Blocked when someone else holds the lease. The confirm says "interrupts <holder>, <n> queued".
- The "backup" is the previous release's `.bit` from the cache, sha-verified, because fpgahub cannot back up the SD.

### (c) Standalone, Ethernet only, Linux harness

**Door and steps:** the `user-usd` door (H12, T7-2).
1. `slot status`.
2. 6910 kind-2 push, with static_id = `provisioned.static_id`.
3. Poll `job`.
4. `commit`.
5. `reboot`.
6. `identify` / `version`.

Once the board is **claimed**, the same steps run over an SSH tunnel to the board's 127.0.0.1. Overlays go over 6910, and D13 `commit` persists them.

**Known traps:**
- **The static cannot change from here.** The FPGA cannot reach the MCC SD, and the MPS3 has no FPGA configuration flash.
- A second push after `commit` needs a rollback first.
- **The card is slow.** Silicon B2 (2026-09-26) measured about 70 KB/s written and 14–135 KB/s read back; the Linux lead budgets about 30 s/MB (a 29 MB slot: minutes to write, more to verify). The numbers HM uses live in ONE place, lane SLOT-TIMING's constants (`harness_manager_mps3/os_slots.py`: the card rates and the budgets derived from them, settings rows `mps3.slot.*`); do not copy them here. pyverify's `wait_job` default (180 s) is too short.
- `verify` is a read, but it holds the board's one card job (other card jobs get `EBUSY`) for minutes: HM starts one only when a person asks (`slot verify`, `slot rollback`), never from a status read.
- stage0 catches only unhealthy images: a confirmed-but-wrong image stays.
- **A fallback leaves the default on the bad slot.** stage0 never writes the card; when the default does not come up healthy it boots the other slot, and `slot status` reads `running A, default B, target null, staged null` (between a commit and its reboot, `staged == default` instead). A push is then refused (`no free slot … rollback first`) until `rollback` makes A the default; until then every power cycle or MCC REBOOT tries B twice more (about 2 × 43 s of watchdog timeouts).
- **`verified: boot` is not a confirm.** It means only that the slot runs and its table CRC is stage0's `image_hdr_crc`. harnessd confirms a healthy boot later (≥ 2 s after start), and only the proposed `confirmed` field says so.
- **A slot written outside harnessd has no record** (`stage0_mkcard.py` + `dd`, `mps3-slot write`, the factory): its `verify` fails with `no slot record: static_id unknown`, so a rollback to it after a reboot is refused. Push it again from HM (the board fix that stamps booted slots is the Linux lead's change 1).
- **Two locks.** The claim lock (`slot locked: board claimed (use ssh)`) refuses slot changes from any peer but the board itself; the identity lock (`identity lock: <reason>`) refuses swap/commit and a flip to a boot-only-verified slot when the fabric and the image disagree. Both are fixed over Ethernet (the board's SSH; a push of the right image + commit + reboot), except a fabric whose static_id stage0 cannot give.

**Guard:**
- A release with a different `static_id` is shown as "needs Debug USB or hub" and is not planned here.
- Check `provisioned.static_id` == `shell_id` before pushing.
- After the reboot, running == default == the new slot, and `target` is the OTHER slot (where the next push goes). The new slot shows `verified: boot`; HM calls it confirmed only when `confirmed` says so, and "booted (not yet confirmed)" until then.
- After a fallback (`running != default`, `staged` null), status says "slot B failed to boot; A is running; roll back to make A the default", and the planner puts a rollback first.
- A wrong image is undone with `verify` + `rollback` + `reboot`.

### (d) Standalone, Ethernet only, bare metal

**Door:** overlays only: host store → 6910 deploy → D13 µSD `commit`.

**Known trap:** the firmware is baked into BRAM, so any firmware or static change means a new `.bit` on the MCC SD, and there is no standalone JTAG.

**Guard:** releases that change the base are shown as "needs Debug USB or hub", and the user is offered `--overlays-only` (P14).

### The first bare-metal → Linux install

**This is path (a) or (b) plus a provisioning step:**
1. After the base REBOOT, stage0 finds no valid slot and starts in **rescue** (`identify mode:"rescue"`, TFTP 69, no 6900).
2. HM pushes the slot image over the rescue TFTP path (as `stage0_push.py` does).
3. stage0 boots it, and harnessd confirms.

T7's executor would call step 1 `written-not-running` today. The Linux door must own the rescue step; the `os_slots.py` module doc already puts rescue behind the adapter. **Prove this at B2 before mint 3 is published.**

### 6.1 The config SD as A/B by pointer (D3a)

**How the MCC finds the bitstream:** `board.txt` → `APPFILE: Nanosoc\nanosoc.txt` → `F0FILE: nanosoc.bit`.

**The proposal:**
- **Two images.** Keep `nanosoca.bit` and `nanosocb.bit` on the card (8.3, lowercase; fpgahub `mps3_msd.py` requires it).
- **Install.** Write the image `F0FILE` does *not* name, read it back, then rewrite the ~200-byte `nanosoc.txt` so `F0FILE` names the new image. The running image is never touched, so an interrupted 12 MB write cannot darken the board. The only risky write lasts milliseconds.
- **Rollback.** Flip `F0FILE` back and REBOOT: no backup restore and no 12 MB write. Today's rollback rewrites the whole SD (P5).

**What it costs:**
- One board check that the MCC accepts another 8.3 `F0FILE` name, about 10 min.
- The new install mode in the pack's `sd.py` (H11).
- For the hub path, one fpgahub action that writes `nanosocX.bit` **and** patches `nanosoc.txt` (FH-b). fpgahub's `sd_install` manifests already allow files + patches (MANIFESTS.md §4.2).

## 7. Rollback and safety

### Keeping the previous version

Three copies, all named in the history:
- the cache pins the previous release's blobs;
- the SD keeps the previous image (A/B);
- the local path also keeps T7's full SD backup zip.

### A failed boot

**Bare metal.** The MCC has no fallback of its own, so HM decides from the witness and the identity:
- **Dark** (no ping, no identify) after the budget: with A/B, HM **auto-reverts** the pointer and REBOOTs, **if it was armed at approval** (D6a; on by default for remote doors). A dark remote board helps nobody, and the revert is a pointer flip.
- **Wrong identity:** `written-not-running`, with a one-click revert. It is not automatic, because a human should look at what is running.

**Linux OS.** stage0 try-once:
- an unconfirmed boot counts a `fail`;
- at 2 fails, stage0 boots the other slot;
- when both are exhausted, stage0 goes to rescue.

HM confirms only when the identity and `slot status` agree (T7's `_confirm_os`).

**Linux static.** The same as bare metal. ROLLBACK_RUNBOOK_LINUX R2/R3 is exactly door (b).

### Verification after an install

T7's `confirm_identity` checks shell_id, harness or fw_sha, usercode, impl, features and skew. Add:
- `usr_access` = the release's `ver32`, with `skew:false`;
- on Linux, default == running, and the slot is confirmed.

### DUT RMs built for the old static

- The plan lists what stops loading (T7 `_unusable`).
- The old kit stays cached for a rollback, and the new kit is offered, with any Vivado change shown.
- A D13 µSD default keyed to the old static is skipped at power-on (static_id mismatch), which is safe. HM offers to re-commit a matching default.
- The user's own RM directories are never touched.

## 8. Product surface

### 8.1 CLI (new `cli/cmd_harness.py`; D7a)

```
harness-manager harness list     [TARGET] [--channel stable|beta] [--all]       # the catalogue + verdicts
harness-manager harness show     VERSION [TARGET]                                # identity, parts, notes, diff vs running
harness-manager harness fetch    VERSION [--kit]                                 # into the cache (offline later)
harness-manager harness install  TARGET [VERSION] [--via usb|hub|ethernet] [--overlays-only]
                                  [--consent "REKEY 0x…"] [--yes] [--no-auto-revert]
harness-manager harness rollback TARGET [--to previous|VERSION|--backup ZIP]    # pointer flip, re-install, or restore
harness-manager harness pin      TARGET VERSION | --clear
harness-manager harness history  TARGET
harness-manager harness mirror   --to DIR [--channel …] [--versions …]
```

- `update check|harness|rollback` stay as T7's aliases.
- `update app` stays with OTA.
- The exit codes are T7's: 8 nothing to do; 6 written, not running; 14 identity mismatch; 15 a rail; 4 held.

### 8.2 API

These live in `daemon/harness_api.py` (H8) and follow docs/API.md's conventions. They are additive next to OTA's routes.

| Route | Does |
|---|---|
| `GET /harness/catalog?board_id=&channel=` | The cached catalogue with verdicts. Fast, no job. With no cache: 409 "refresh first" |
| `POST /harness/catalog/refresh` `{channel?, source?}` | 202 job `harness_refresh`; emits a `harness.catalog` event |
| `GET /harness/releases/{v}?board_id=` | The details, the diff, and the plan with its `fingerprint` |
| `POST /harness/releases/{v}/fetch` | 202 job `harness_fetch`, with `update.progress` |
| `POST /boards/{bid}/update/check` `{version?, via?}` | **CCR:** plan a chosen version |
| `POST /boards/{bid}/update/harness` `{fingerprint, version?, via?, rekey_phrase?, board_phrase?, auto_revert?}` | **CCR:** recompute the plan *with* the version and door. Today `update_api.py:222` drops them |
| `PUT /boards/{bid}/harness/pin` · `GET /boards/{bid}/harness/history` | pin; history |

### 8.3 UI: a Harness versions card

A new `sections/harness.js` (H9), next to OTA's app Settings card, holds:
- **Running:** the version (or "unrecorded"), static, fw sha, impl, since when, and a pinned chip.
- **Available:** one row per release, with:
  - verdict chips, size, and a cached tick;
  - a "What changes" expander;
  - an **Install** or **Roll back to** button. It opens T7's plan card and ArmBox, which asks for the typed re-key phrase **and**, on remote doors, the board name ("type mps3-01; this interrupts alice (lease), 1 queued").
- **Progress:** the existing job and step display. The hub door adds a step: `sd:writing (hub; a reply timeout is expected)`.
- **History:** each past install has a **Roll back to this** button.

### 8.4 Core vs pack

**Core (board-agnostic):**
- catalogue, schema, trust, download and mirror (shared with OTA);
- planner, executor, history, pins;
- the release-tool library.

**Pack (the MPS3 today; KR260 and HAPS later):**
- the doors:
  - `storage` (local USB SD);
  - **`hub_sd`**;
  - `controller` (MCC REBOOT);
  - **`os_slots`** (T7-2);
  - the overlay handler (T7-5);
- identity mapping;
- the harness catalogue id and default source;
- the board spec and the revision probe.

A KR260 pack brings its own doors. The core never learns what an MCC is. The planner's "Debug USB" blocker texts become the pack's door descriptions (H6).

## 9. Answers for the Linux lane (SLOT_VERB_DRAFT §5, HM's side)

| # | Answer |
|---|---|
| 1 `verify` act | **Keep.** HM needs it to roll back after rebooting into the new image |
| 2 nested objects | **Fine.** Keep `a`, `b` and `job` |
| 3 async + poll | **Poll**: every 0.5–1 s, which keeps 6900 free. The budget is NOT 180 s: the card runs at tens of KB/s (B2), so it comes from the image's size and the card's rates, lane SLOT-TIMING's constants (`harness_manager_mps3/os_slots.py`, rows `mps3.slot.*`) |
| 4 no card | **Agree** |
| 5 feature bit | HM gates on feature **names**. Add a named feature `slot`; its bit number does not matter to HM |
| 6 slot records | **Yes.** HM shows each slot's version and can flip to a confirmed slot |
| 7 trust | Decided: lock on claim. HM tunnels over SSH when `identify.ssh.claimed`. Later, the board verifies a signed release sha with the **same release key** (§4.3) |
| 8 rule 1 | **OK.** HM rolls back before a second push |
| 9 texts vs codes | **Add a stable `code`** beside `err`. HM matches the code and shows `err` |

**Built (lane LINUX-SLOTS, 2026-09-26), on these answers.** `harness_manager_mps3/os_slots.py`
(`session.os_slots`, CCR T7-2: the protocol moved to `core.pack`), `card.py` (D13,
`session.card`), `services/slots.py`, `harness-manager slot|card`, `GET /boards/{bid}/card`
and `/slots`, and the OS frame check in `bundle.py`. Until the Linux lane freezes the verb:
item 5 is detected by `version.impl == "linux"` (a `slot` feature is honoured if reported);
item 6's slot records are assumed (a slot without one cannot be rolled back to once another
runs: the board refuses, HM says why); item 7's claim is read from `identify.ssh.claimed`,
or, through the hub (no UDP), from a no-op guarded `rollback` whose lock refusal is the
answer; item 9 matches the `err` texts (no `code` yet). Rescue provisioning (L3) is not built.

**The Linux lead's answers (2026-09-26, `HM_ANSWERS_2026-09-26.md` on feat/linux-harness),
taken by lane LINUX-ANSWERS.** Item 6: harnessd writes a slot record only after its own push's
read-back, never at commit or at boot, so a slot written by mkcard/dd/`mps3-slot write` has
none (the stamp-on-boot fix is their change 1); HM explains the `no slot record` verify
failure. Items 5, 7, 9: HM reads `slot` (a feature NAME), `claimed` and `confirmed` in `slot
status`, `code` beside `err`, `job.code` and identify's `ssh.key_sha256` when present, and
never requires them (`harness_manager_mps3/slot_words.py`, `services/slot_health.py`). The
two locks are modelled separately, a fallback is detected (`running != default`, nothing
staged), and `verified: boot` is never called confirmed.

## 10. Decisions for david

Recommendation first in each. D1 and D5 are **the same decisions as OTA's D1 and D2**; take them once for both.

**D1. Hosting (= OTA D1).**
- **(a) Recommended:** private GitHub Releases with a token and a hub mirror. The harness catalogue goes on the same host as the app. The Arm-IP overlays go in a separate private `mps3-harness-aaa` repo.
- (b) Public now (Lane G's `mps3-platform-dist`). This needs the licence and G6 notices decided first. The AAA repo stays private either way.
- (c) Hub only. Standalone users get nothing.

**D2. What names a release on the board.**
- **(a) Recommended:** firmware `VERSION` = the release tag, stamped at bake (R4 = G7). Until then, HM matches by (static_id, fw_sha).
- (b) Never stamp. The tag exists only in the channel, and HM always matches by sha.

**D3. How the config SD is installed.**
- **(a) Recommended:** A/B by pointer (`nanosoca.bit`/`nanosocb.bit` plus an `F0FILE` flip). Needs one 10-minute board check.
- (b) In place, with a full backup (T7 today).

**D4. Remote installs through the hub.**
- **(a) Recommended:** HM's `hub_sd` door over `fpgahub … --method sd`, for `.bit`-only deltas, plus fpgahub requests FH-a–d.
- (b) The HM engine runs hub-side (T8's plugin) with its own SD access. Heavier, and needs disk access on the hub.
- (c) Local Debug USB only.

**D5. Key custody (= OTA D2).**
- **(a) Recommended:** offline minisign keys: root cold on two USB sticks; the release key passphrase-protected on your machine; you sign `stable`.
- (b) The lead also holds the release key.

**D6. Auto-revert when a remote install leaves the board dark.**
- **(a) Recommended:** yes, armed at approval and on by default for remote doors. With A/B it is only a pointer flip.
- (b) Never automatic.

**D7. CLI shape.**
- **(a) Recommended:** a new `harness` noun, with `update …` kept as T7 aliases.
- (b) Extend `update` only.

## 11. Lane plan (HM; after the decisions)

**Shared work that is not counted here:**
- **OTA-C:** `catalog` field, serials keyed by (catalog, channel), token on the channel fetch, `github-release` source, `notes`. It fixes P8 and P9 for both.
- **OTA-R:** `scripts/release/` core, `minisign` CLI signing, `docs/RELEASING.md`, `docs/KEYS.md`.
- **Keys:** david's ceremony and the `PINNED_KEYS` PR.

**Rules:**
- `services/update/**` is T7's merged area, and `daemon/update_api.py` is L4's. Changes there are **CCRs**, each assigned to exactly one lane.
- **H1 includes KIT-STORE's K4.** Do it once, inside OTA-C's schema pass.

| # | Work | Files | Hours | Needs |
|---|---|---|---|---|
| H1 | Harness schema additions: `identity.ver32`, `vivado`, target `host-kit` / kind `rm-kit` (= KIT K4). The planner ignores `host-kit` | CCR in OTA-C's pass: `schema.py`, `planner.py:267` | 1 | OTA-C |
| H2 | Identity matching: `fw_sha` in `match_release` and `base_differs`; confirm = shell_id + (harness **or** fw_sha) | CCR: `planner.py:165-190`, `executor.py:112-149` | 2 | — |
| H5 | Mirrors: look up by sha in `HARNESS_MANAGER_UPDATE_MIRRORS`, plus `harness mirror` | CCR: `download.py:145-189` (after OTA-C); new CLI verb | 3 | OTA-C |
| H6 | `services/harness_catalog.py`: verdicts, diff, pins, history, retention, door-neutral texts | new; CCR `executor.py` (append to history) | 7 | H1, H2 |
| H7 | `cli/cmd_harness.py` + TSV columns | new; CCR `cli/main.py` (one line), `cli/output.py` | 4 | H6 |
| H8 | `daemon/harness_api.py`, plus `version` and `via` on the update routes | new; CCR `daemon/update_api.py:175-230`, `docs/API.md` | 4 | H6 |
| H9 | UI: the Harness versions card | new `web/static/js/sections/harness.js`; CCR `update.js`, `api.js` (coordinate with OTA-U) | 5 | H8 |
| H10 | MPS3 `hub_sd` door: lease-held check, upload, `program --method sd --force`, completion by sha, single-flight, backup = previous `.bit`, board + holder consent | new `harness_manager_mps3/hub_sd.py`; CCR `pack.py`, `planner.py` (`BoardView.lease`, `via`) | 8 | D4a, FH-a |
| H11 | SD A/B by pointer in the local SD door (+ revert), and D6 auto-revert in the executor | CCR: `harness_manager_mps3/sd.py` (T3), `executor.py` | 5 | D3a, 1 board check |
| H12 | Linux `os_slots` adapter over `pyverify.slot` (status, push, commit, verify, rollback; SSH tunnel when claimed), the **rescue provisioning** of a first install, and the OS-slot frame check. **Built by LINUX-SLOTS except rescue provisioning (L3)** | new `harness_manager_mps3/os_slots.py`; CCR `core/pack.py` (T7-2), `bundle.py:406-411` | 8 | L2, L3 |
| H13 | The release tool's `harness` front-end: ingest the platform bundle; the harness refusals; the AAA split; smoke test = the spike on a VirtualMps3; promote and withdraw | `scripts/release/harness.py` (inside OTA-R's tree), from `tests/spikes/harness_dist_publish.py` | 6 | OTA-R, R1 |
| | **Total** | | **≈ 53 h (≈ 7 days)** | board checks: A/B pointer 10 min, hub door 20 min, Linux rescue at B2 |

**Order:**
1. H2 now; it needs nothing.
2. OTA-C, then H1 and H5.
3. H6, then H7 and H8, then H9.
4. H10, H11 and H12 run in parallel once their decisions and inputs land.
5. OTA-R, H13 and the keys are what make a release real. The first **beta** of the fielded 0x72BB0A36 (v1.1.0) can ship as soon as R1 and R2 exist.

### 11.1 Requests to the platform lanes (we ask; they edit)

**FLOW (bare metal):**

| # | Request | Why |
|---|---|---|
| R1 | `make -C fpga/dfx release-bundle` (Lane G G1–G3). It writes `prod/release/` with `bundle.json`, the SD zip, one overlay zip per `ip_class` and the identity zip, and stage 8 copies it to `MINT_HUB/<sid>/release/` | one artefact for the release tool to consume |
| R2 | `mint.json` records the **fielded bake**: `firmware.{version, proto, sha, dirty, flags, features, elf_sha256, bit_name, bit_sha256}` | the v0.11 bake exists only in `MANIFEST.md5` and the README |
| R3 | Copy all 13 partials and clearings to `MINT_HUB/<sid>/overlays/`. Today only the ILA RMs go there | the release step and the mirror need them all |
| R4 | Stamp firmware `VERSION` = the release tag at bake (G7), without changing USERID | then `version.harness` names the release |
| R5 | `ip_class` per RM in `rm_list.tcl`, plus a gate (G5) | decides open vs AAA per component |
| R6 | Make `MINT_HUB/<sid>/` write-once (KIT F6) | the hub is the archive of record |
| R7 | G8: correct the `mbb_v141.ebf` comment | the installer never writes an `.ebf` |
| R8 | The flow fills in `nanosoc.txt`'s `F0FILE` | needed for D3a |

**Linux:**

| # | Request | Why |
|---|---|---|
| L1 | `linux_bundle.json` gains a `harness` semver and `release_notes` | it maps 1:1 onto the mcc-sd, user-usd and host-store components |
| L2 | Freeze the `slot` verb with the §9 answers: a named `slot` feature, slot records, a `code` field | H12 needs it |
| L3 | A **rescue provisioning** contract a host can drive: rescue identify → TFTP push → boot → confirm | the first bare-metal → Linux install |
| L4 | `/etc/mps3/version harness` = the release tag | the same reason as R4 |
| L5 | Later: the board checks a signed release sha before `commit` | §9 item 7 |

**fpgahub (david's repo, for D4a):**

| # | Request |
|---|---|
| FH-a | `program --method sd` returns a job id at once, and `GET /jobs/{id}` gives `{state, sha256, dur}`. This replaces reading journalctl |
| FH-b | One locked `sd_install` action that writes a `.bit` to a chosen `dest` **and** patches `nanosoc.txt` (for A/B) |
| FH-c | Report the sha of the current `dest` before writing |
| FH-d | A preflight that the `sd` method can reach the card. `--list` "Available: yes" reports configuration, not reachability |

## 12. The spike

**The choice: (a), an end-to-end fake release.** It is the only option that exercises the *producer* half, and that is where most gaps turned out to be. Option (b), measuring T7 with a multi-version catalogue and a downgrade, is folded in as P2–P5 and P11.

**Files** (new files only; not collected by pytest):
- `tests/spikes/harness_dist_publish.py`: a prototype release command, including adapters for `mint.json` and `mps3-linux-bundle` v1;
- `tests/spikes/harness_dist_spike.py`: the probes.

**Run it:** `PYTHONPATH=src:. python -m tests.spikes.harness_dist_spike`. It uses only 127.0.0.1 and `/tmp/hdist-*`, which it deletes. Its keys are generated in memory.

**Setup:**
- **Four fake mints in real formats:**
  - an SD tree whose `nanosoc.bit` has a real Xilinx header (part and UserID);
  - T2 overlay triples keyed to each static and usercode;
  - a kit zip;
  - a Linux slot image, from the `mps3-linux-bundle` v1 shape.
- **Two channels:**
  - `stable`: 1.0.0 @0x3F1A560F; 1.1.0 @0x72BB0A36 (fw d68dd0ed); 1.1.1 @0x72BB0A36 (fw re-bake 0e12a0b0);
  - `beta`: the same plus 2.0.0, Linux @0x4C1A0003 (a placeholder static).
- **The board:** a VirtualMps3 on 0x72BB0A36 with the v0.11 profile and the Debug USB, driven through the real MPS3 pack, with FakeMcc on a fake clock.

**Results:** 16 probes, 4.1 s.

| # | Probe | Verdict | Evidence |
|---|---|---|---|
| P1 | publish + list | **PASS** | publisher → `parse_channel` → minisign → 127.0.0.1. T7 lists 1.1.1, 1.1.0 and 1.0.0. Re-packing gives identical assets |
| P2 | which release the board runs | **GAP** | the board reports 0x72bb0a36, `1.0.0`, sha d68dd0ed, which is 1.1.0. T7 says **1.1.1** |
| P3 | catalogue (one plan per version) | NOTE | 1.1.1 `full` (wrongly marked running), 1.1.0 `overlays`, 1.0.0 `full` re-key, 2.0.0 re-key + blocker "no OS slot update" |
| P4 | fw-only 1.1.1, local USB | **PASS** | `installed`; the sha is now 0e12a0b0; open overlays stored; AAA overlays skipped (no token); 1 reboot |
| P5 | downgrade across statics, then roll back | **PASS** | refused without `REKEY 0x3f1a560f`, installed with it; 3 unusable items listed; rollback `restored` to 1.1.1. The rollback rewrote the whole SD |
| P6 | the tag in `identity.harness` | **GAP** | the board booted the new SD but still says `1.0.0`, so the result is `written-not-running` |
| P7 | KIT-STORE's kit component | **GAP** | the schema refuses `host-kit`/`rm-kit`. The publisher caught it before signing |
| P8 | two catalogues on one host | **GAP** | mps3 `stable` #5, then kr260 `stable` #2, is refused as a rollback |
| P8b | channel names `testing`, `mps3-stable` | NOTE | keys may sign only stable, beta and dev |
| P9 | private index | **GAP** | HTTP 401; no Authorization header on the index request |
| P10 | offline mirror of absolute URLs | **GAP** | the index verified from the mirror, but the assets went to the dead origin |
| P11 | retention | NOTE | with 1.0.0 dropped: "no harness release 1.0.0" |
| P12 | Linux OS-slot-only update | **PASS** (planner only) | `os_slot=True`, `base=False`, no blockers. The adapter is T7-2 |
| P13 | cache | NOTE | 6 blobs. No eviction or pinning |
| P14 | bare metal, Ethernet only | NOTE | the base update is blocked "needs the Debug USB"; overlays-only works |

**What it retires:**
- T7's consumer works end to end on a real-shaped, multi-version channel, including a downgrade across statics and a rollback.
- A publisher that validates with the app's own parser cannot sign a channel the app would refuse.
- Deterministic packing makes re-publishing idempotent.

**What it found:** every GAP is listed in §2, and each one is assigned to OTA-C or to an H lane in §11.
