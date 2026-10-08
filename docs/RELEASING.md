# Releasing Harness Manager and harness bundles

The release tool is `tools/release/` (`python -m tools.release`, or the `make release*`
targets). The lead runs it on a workstation: CI is not needed and not used. It builds a
release, signs a `channel.json` for one (catalogue, channel), reads the result back with
Harness Manager's own client code, and writes the `gh` steps that publish it.

**Every command is a dry run** unless you pass `--publish` (`PUBLISH=1`). A dry run writes
the whole signed release into `dist/release/`, verifies it, and writes the `gh` commands to
`dist/release/plans/<catalogue>-<channel>/publish-plan.txt` without running them.

**What blocks the first real release:**

- **The keys (david's U2).** No signing key is pinned in `trust.PINNED_KEYS`, so no client
  would trust a channel yet, and `--publish` refuses. See [KEYS.md](KEYS.md).

Private hosting (U1) is not a blocker any more. OTA-C (commit `4e1d394`) taught the client
to fetch `channel.json` with the GitHub token and to accept a private app wheel. The tool
now emits the real `access: github-token` (the default), and `--publish` of a private-repo
app release is allowed. A client that reads from GitHub needs a token
(`$HARNESS_MANAGER_GITHUB_TOKEN`, else `gh auth token`); a mirror needs none.

The design is `docs/design/HM_SELF_UPDATE.md` §4 (app) and
`docs/design/HARNESS_DISTRIBUTION.md` §4 (harness).

## One-time setup (about 10 minutes)

1. Make a tools venv with uv and build:
   `python3.11 -m venv ~/hm-release-tools && ~/hm-release-tools/bin/pip install uv build`.
   Without uv the tool falls back to pip-tools (`pip install pip-tools`). A pip-tools lock
   is for this machine's OS and Python only, and `--publish` refuses it (see below).
2. Install the `minisign` CLI (https://jedisct1.github.io/minisign/). It signs, so the
   secret key never enters Python.
3. `gh auth login`, with write access to `SoC-Labs/HarnessManager` (and to
   `SoC-Labs/mps3-harness-aaa` for harness releases).
4. Point the tool at the key files, once per shell:

   ```
   export HM_RELEASE_SECRET_KEY=/path/to/release.key   # passphrase-protected; minisign asks
   export HM_RELEASE_PUBLIC_KEY=/path/to/release.pub
   export HARNESS_MANAGER_UV=~/hm-release-tools/bin/uv
   ```

## An app release, step by step (about 8 minutes)

| # | Step | Time |
|---|---|---|
| 1 | `nice -n 10 make check` must end `CHECK PASS` | 3 min |
| 2 | Bump `__version__` in `src/harness_manager/__init__.py`: it is the one version source (`pyproject.toml` reads it). Add `## 0.2.0 (2026-10-02)` at the top of `CHANGELOG.md`: its text becomes the signed release notes. Commit | 1 min |
| 3 | Dry run: `make release RELEASE_ARGS="--smoke install"`. Read every `WARNING:` line | 1 min |
| 4 | Tag the commit: `git tag -a v0.2.0 -m v0.2.0` | 10 s |
| 5 | Publish to beta: `make release PUBLISH=1 RELEASE_ARGS="--smoke install"`. minisign asks for the passphrase | 2 min |
| 6 | Optional hub mirror: add `MIRROR=/path/to/mirror`, then copy that directory to the hub | 1 min |

`make release` first checks the venv (`tools/venv_guard.py`): it must load this checkout's source once, and its pyverify must be the vendored wheel. If it says REFUSED, run `make clean venv`; `ALLOW_DEV_PYVERIFY=1` allows a dry run on a development pyverify.

After a soak on beta, promote it (step 7). A stable release is never published directly:
`--channel stable` is refused.

| # | Step | Time |
|---|---|---|
| 7 | `make release-promote VERSION=0.2.0 PUBLISH=1` with the stable key (david signs) | 1 min |

**What step 3 does:**

1. **Preconditions.** Refuses a dirty tree (any change or untracked file), a
   `pyproject.toml` with a version of its own (it must read `harness_manager.__version__`:
   `dynamic = ["version"]`), a newest `CHANGELOG.md` heading that differs from
   `__version__`, a tag `v0.2.0` that points at another commit, and a version that is not
   newer than every release already on the channel.
2. **Wheel.** `python -m build --wheel` on `git archive HEAD` (committed files only), with
   `SOURCE_DATE_EPOCH` set to the commit time.
3. **Lock.** `uv pip compile --universal --generate-hashes --python-version 3.10
   -c constraints.txt --extra serial --extra ina260`. Without uv: `pip-compile
   --generate-hashes` (pip-tools), recorded as `universal: false`.
4. **pyverify.** The vendored wheel ships as a `dep` artifact, and the lock pins it as
   `mps3-pyverify==X --hash=sha256:<the vendored wheel>`. The client installs it from the
   verified download (no index serves it).
5. **Channel.** Adds the release to `hm-app/beta` (serial + 1, expiry in 180 days),
   validates it with the app's own `parse_channel`, and signs it.
6. **Smoke.** HM's `ChannelClient` fetches and verifies the channel over `file://`. HM's
   `Downloader` fetches every asset and checks size and sha256. The wheel's METADATA must
   name the version, and the lock must pin the dep by its hash. With `--smoke install`,
   the release also installs into a throwaway venv (`pip install --require-hashes`) and the
   installed `harness-manager version` must print the version. That step needs PyPI.
7. **Plan.** Writes the `gh` steps. `--publish` runs them in order and stops at the first
   failure:
   - `git push origin refs/tags/v0.2.0`;
   - `gh release create v0.2.0 --verify-tag --prerelease <wheel> <lock> <dep>`;
   - create the rolling release `channel-hm-app-beta` if it is missing;
   - `gh release upload channel-hm-app-beta channel.json channel.json.minisig --clobber`.

**What `--publish` adds:**

- the base is the live channel, read with `gh release download` (read-only);
- the signer must be the minisign CLI, and its key must be pinned in `trust.PINNED_KEYS`;
- the lock must be universal (uv), unless `--allow-non-universal-lock`;
- `--smoke install` must have passed, unless `--skip-install-smoke REASON`;
- the tag must exist at HEAD, and `CHANGELOG.md` must not say "unreleased".

## A harness release (about 8 minutes)

To build one straight from the platform's artifacts, use `make harness-release` instead
(next section): it assembles the bundle directory for you. These steps are for a bundle
directory you already have.

1. Assemble the bundle directory. Until the platform writes one (request R1), build it by
   hand from the mint. The layout is in the `tools/release/harness.py` module doc:
   - `mint.json` (bare metal) or `linux_bundle.json` (Linux, FLOW_CONTRACT v1.6);
   - `firmware.json` (what the firmware reports: version, sha, dirty, proto, features);
   - `sd/` (the config-SD tree with the static `.bit`);
   - `overlays/open/` and `overlays/aaa/`;
   - Linux: `linux_slot.img` and `linux_legal_info.tar`;
   - optional `kit/` and `notes.md`.
2. Check it: `python -m tools.release harness --bundle DIR --version 1.2.0 --check-only`.
   It prints the catalogue entry and writes nothing.
3. Dry run: `make release-harness BUNDLE=DIR VERSION=1.2.0`.
4. Publish to beta: add `PUBLISH=1`.
5. After a soak: `make release-promote CATALOG=mps3-harness VERSION=1.2.0 PUBLISH=1`.

**The harness front-end refuses, and writes nothing, when:**

| Refusal | Code | Why |
|---|---|---|
| a dirty image (a mint source `dirty`, dirty firmware, a Linux `image_kind` other than `release`, a dirty kit) | `DIRTY` | `--allow-dirty REASON` puts the reason in the signed entry, for beta or dev only. `promote` never takes such a release to stable |
| an unstamped `.bit`, or a header USERID that is not the mint's `static_usercode` | `UNSTAMPED`, `USERID` | the USERID names the implementation run |
| Arm IP in an open component: an overlay under `overlays/open` declared `arm-aaa`, or a known Arm-IP RM (`nanosoc*`, `eth_ss`); an open file with a path into the Arm IP library | `AAA_OPEN` | Arm IP goes only to the private AAA repo |
| any `.ebf` | `EBF` | it reflashes the MCC |
| a Linux bundle that is not fieldable (`fieldable: false`, `mint_kind: prototype`) or has no legal-info | `NOT_FIELDABLE` | a P-mint is never released (FLOW §5) |
| an overlay, slot image or flashable `.bit` keyed to another static | `STATIC` | an overlay must fit the static |

Identity-only refusals (`USERID`, `STATIC`) exit 14; the rest exit 15.

**What it records.** `identity.harness` is what the firmware **reports** (`1.0.0` today), never
the tag. `identity.fw_sha` is how HM tells two releases on one static apart. The release
also carries `firmware.{version, sha, stamped}`. Once firmware stamps VERSION with the
tag (U7), `stamped` becomes true.

Each component names its FLOW door in `door`: `mcc_sd` (the config SD and an MCC REBOOT)
or `ethernet` (the slot image and the overlays). The tool writes the schema `target` as
`mcc-sd`, `user-usd`, `host-store` or, for the kit, `host-kit`. `mcc-sd` and `user-usd` are
HM's older spellings: apps from before OTA-C parse them too, and today's client reads them
as `mcc_sd` and `ethernet`.

## Publishing a harness release

This is how a harness release gets from the platform's build outputs to a GitHub repo
that Harness Manager's catalogue reads. After it, a user picks the release in HM (or runs
`harness-manager harness install`), and HM downloads, verifies and installs it.

Two tools do the work:

- **`make harness-release`** builds the release. It assembles it from the platform's
  artifacts, signs it with a key you pass by path, and reads it back with HM's own client
  code. It never publishes.
- **`scripts/publish_harness_release.sh`** uploads a built release. `--dry-run` is the
  default: it prints every `gh` command and runs none. A real upload needs `--publish` and
  a typed confirmation at the terminal.

### What david decides before the first one

1. **The keys (U2).** Run the ceremony in [KEYS.md](KEYS.md), pin the public halves in
   `trust.PINNED_KEYS`, and ship an app release that carries them. Until then, every client
   refuses every channel, and the publish script refuses to upload.
2. **The repo.** The default is `SoC-Labs/HarnessManager` (private, david's U1), with the
   Arm-IP parts in `SoC-Labs/mps3-harness-aaa`. Three questions follow:
   - **Private or public?** Everything uploaded to a public repo is public. The channel and
     the assets go to the same repo.
   - **Who gets read access?** In a private repo, each HM user needs a GitHub account with
     read access, for example through an organisation team.
   - **How does HM authenticate?** In this order:
     - `HARNESS_MANAGER_GITHUB_TOKEN` (a fine-grained token with read-only "Contents" on
       the repo);
     - the token stored with `harness-manager config set-secret updates.github_token`;
     - the GitHub CLI's login (`gh auth login`; HM reads `gh auth token`).

     A mirror directory needs no token.
3. **The licence calls: what may be published, and to whom.**
   - **Arm IP.** The RMs in `AAA_RMS` are `nanosoc`, `nanosoc_multicore`, `nanosoc_upy`,
     `nanosoc_ila` and `eth_ss`. They are LEFT OUT by default. `--include-aaa` publishes
     them, but only to the private AAA repo. Who may read that repo is an Arm Academic
     Access question.
   - **AMD IP in the RM kit.** The kit holds the locked static DCP (MicroBlaze V, DDR4 MIG,
     AXI IP), and its `kit.json` says INTERNAL-ONLY (D2). Pass `KIT=` only once that is
     decided. It always goes up private.
   - **AMD IP in the bitstreams.** The config-SD `.bit` and the overlays are bitstreams
     built from that IP. Confirm they may be shared with the repo's readers.
   - **GPL.** The Linux image's `linux_legal_info.tar` (sources and licences) always goes
     in the same release. The tool refuses a Linux release without it.

### The steps (about 20 minutes once the keys exist)

| # | Step | Time |
|---|---|---|
| 1 | Once: the key ceremony and the keys PR ([KEYS.md](KEYS.md)) | 30 min |
| 2 | Find the inputs (the table below) | 5 min |
| 3 | Check them: `make harness-release … RELEASE_ARGS=--check-only`. It assembles and validates, and signs nothing | 1 min |
| 4 | A TEST dry run: add `TEST_KEY=1 OUT=/tmp/hr-test`, then `scripts/publish_harness_release.sh --repo OWNER/REPO /tmp/hr-test`. Read every `WARNING:` line | 2 min |
| 5 | Build it for real: `make harness-release … KEY=/path/to/release.key REPO=OWNER/REPO OUT=dist/harness-2.0.0 RELEASE_ARGS=--live-base`. minisign asks for the passphrase | 2 min |
| 6 | Dry-run the upload: `scripts/publish_harness_release.sh --repo OWNER/REPO dist/harness-2.0.0` | 1 min |
| 7 | Upload: the same command with `--publish`, at a terminal. Type the phrase it asks for: `PUBLISH mps3-harness 2.0.0 TO OWNER/REPO` | 5 min (about 300 MB) |
| 8 | Check it from any machine with a token: `harness-manager harness list --source github:OWNER/REPO --channel beta`, then `harness show 2.0.0 --source github:OWNER/REPO --channel beta` | 1 min |
| 9 | After a soak: `make release-promote CATALOG=mps3-harness VERSION=2.0.0 PUBLISH=1` (stable) | 1 min |

**The inputs for v2.0.0** (Linux, RC2 `0x44EE76D5`):

| Make variable | What | Where (1 Oct) |
|---|---|---|
| `FROM` | the mint's prod dir: `linux_bundle.json`, `linux_slot.img`, `linux_legal_info.tar` | the clean rc2_v7 build's `prod/` (slot image `05c83617…`) |
| `BIT` + `STAGE0_BAKE` | the public stage0 bake (label `MPS3`, 192.168.10.101) and its `mps3-stage0-bake` record | `config_rm_greybox_stage0_generic_b1.bit` (`554ce7ae…`) + `stage0_bake_generic_b1.json` |
| `SD_TEMPLATES` | `fpga/mps3_sd/templates` of the platform release commit | default: `$(PLATFORM)/fpga/mps3_sd/templates` |
| `OVERLAYS` | the mint's `overlay_mbv/` (13 RMs; 8 are published, the 5 Arm-IP RMs are left out) | `fpga/dfx/build_mint3_rc2_linux/overlay_mbv` |
| `KIT` | the RM kit zip (only after the D2 call) | `mps3_rc2_0x44EE76D5_kit.zip` (`95768b64…`) |
| `RELEASE_ARGS=--notes FILE` | the signed release notes; `«…»` placeholders are refused | `RELEASE_NOTES_v2.0.0.md`, filled in |

**Why `STAGE0_BAKE`.** The rc2_v7 `linux_bundle.json` names board 1's bake (`f876e73e…`,
label MPS3-01) as the config-SD `.bit`. A public release carries the generic bake.
`--stage0-bake` lets the SD carry a re-bake only when its record names the same static,
the same UserID and a mint, with a stage0 compiled for that static. The bundle's copy of
`linux_bundle.json` records the swap. The platform can drop this step by re-packing the
bundle with the generic bake (request R1).

**What `make harness-release` does:**

1. **Assembles** the bundle directory (`tools/release/assemble.py`):
   - stamps the config-SD tree from the templates, as `assemble_sd.sh` does: `config.txt`;
     for each board revision, `MB/HBI0309<rev>/board.txt` (every `@BOARD@` token stamped,
     comments included, as its `sed .../g` does); `Nanosoc/nanosoc.txt`, `images.txt` and the
     `.bit` under the name `F0FILE` gives;
   - **both revisions, B and C, by default** (FIX-PACK-9, david 2 Oct: Rev C supported, Rev B
     "boots, untested", no Rev A). The MCC reads only `MB/HBI0309<its revision>/`, so a C-only
     card leaves a Rev B board unprogrammed. `BOARD_REVS=HBI0309C` (`--board-rev C`) makes a
     C-only release. The SD part is then `sd-HBI0309BC`; it lists every file of both trees, and
     `compat.board_revs` and `board.revisions` are `["HBI0309B", "HBI0309C"]`. The finding
     `REVS` refuses trees that differ beyond each board.txt's revision token and its `;`
     comments. A ready `--sd` tree is taken as it is: it must serve exactly the `--board-rev`
     given, and without one, a tree that is not B+C is a WARNING;
   - declares `compat.min_app` **1.0.0** unless `--min-app` says otherwise: a B+C config SD
     needs Harness Manager 1.0.0's per-revision MBBIOS rule (an older one keeps only
     `MB/HBI0309C`'s line), so an older client's plan is blocked ("harness <v> needs
     harness-manager >= 1.0.0 (this is <its>); run `harness-manager update app` first");
   - copies the Linux parts;
   - splits the overlays by `AAA_RMS`;
   - takes the kit with its `kit.json`.

   It refuses:
   - an overlay that is not the one `linux_bundle.json` was packed with (rm_name and both
     CRC-32s);
   - a `.bit` that is not the bundle's flashable one, unless a matching re-bake record
     comes with it.
2. **Validates** it with the harness front-end. That means every refusal in the table
   above.
3. **Signs** `channel.json`. The base is the channel already in `OUT`, the live one with
   `--live-base` (read with `gh release download`, read-only), or none. The serial goes up
   by one.
4. **Reads it back** with HM's channel client, downloader and bundle checks.
5. **Prints** each asset's size and sha256. The full record is in
   `OUT/plans/mps3-harness-beta/report.json`.

**The signing key** goes by path: `KEY=FILE`, with its public half beside it (`release.key`
→ `release.pub`) or in `--public-key`. The minisign CLI signs, and a passphrase prompts on
the terminal. Nothing generates, prints or stores a real key.

**`TEST_KEY=1`** is for tests and dry runs:
- it makes a throwaway key in a temp dir, whose id starts with `7E57C0DE`, and deletes the
  secret half after the run;
- it marks the release TEST in four places: a top-level `test` in `channel.json`, the
  signature's trusted comment, the release notes, and `TEST-BUILD.txt` in `OUT`;
- no client trusts it: `trust.pinned()`, `trust.load_trust()` and `trust.apply_keys_json()`
  each refuse it with "a TEST key (7E57C0DE…) is never trusted", so it can be neither
  pinned nor rotated in;
- `--publish` and the publish script refuse it;
- tests hand it to the client only through the test seam: the CLI's engine factory
  building `UpdateService(trust=…)`.

**What the publish script checks before it uploads** (`python -m tools.release
publish-check`, the client's own code). Each failure stops it, and nothing is uploaded:

1. The tree was built for `--repo`. Rebuild with `REPO=` rather than upload elsewhere.
2. The channel is signed, and not by a TEST key.
3. With `--publish`, the key is pinned for the channel.
4. The client verifies the channel and every asset (size, sha256, the bundle checks), and
   each asset is on disk as signed, with no extra file beside it.
5. With `--publish`, the live channel (read-only) has a lower serial and every one of its
   releases is still in ours.

Then it uploads in this order:
1. the per-version release `mps3-harness-v<V>` with its assets;
2. the Arm-IP assets in the AAA repo, if any;
3. the rolling release `channel-mps3-harness-<channel>`, `channel.json` and its `.minisig`
   (`--clobber`). The channel goes last, so no client sees a release before its assets.

The repo needs at least one commit: `gh release create` tags its default branch.

**The v2.0.0-rc1 dry run with B and C** (2 Oct, FIX-PACK-9, `TEST_KEY=1
BOARD_REVS=HBI0309B,HBI0309C`, the inputs below, into `/tmpdir/claude-74755/release-pipe-dryrun-bc/`)
took 9 s. The SD part is `mps3-harness-2.0.0-rc1-sd-HBI0309BC.zip`, **4.3 MiB** (9 files: the
13.9 MB `.bit` twice, 2.2 MiB each compressed; a card carries 13.9 MB more than a C-only one).
Everything else is as below; the total is **267.7 MiB**. B's and C's board.txt differ in line
1 (`BOARD:`) and, until the platform rewords its template comment (CCR-3), in line 4.

**The v2.0.0-rc1 dry run** (1 Oct, C only, `TEST_KEY=1`, the inputs above, AAA and the kit as
listed) took 10 s:

| Asset | Size |
|---|---|
| `mps3-harness-2.0.0-rc1-sd-HBI0309C.zip` (the 13.9 MB `.bit` compresses) | 2.2 MiB |
| `mps3-harness-2.0.0-rc1-linux_slot.img` | 28.1 MiB |
| `mps3-harness-2.0.0-rc1-overlays-open.zip` (8 RMs) | 1.0 MiB |
| `mps3-kit-0x44EE76D5.zip` (private) | 36.0 MiB |
| `mps3-harness-2.0.0-rc1-linux_legal_info.tar` (GPL) | 198.3 MiB |
| `mps3-harness-2.0.0-rc1-linux_bundle.json` (the manifest, with the re-bake recorded) | 8.0 KiB |
| total | 265.6 MiB |

The 5 Arm-IP RMs it left out are about 12.8 MB unzipped.

**Known gaps:**
- **A USB-only board takes a Linux release's config SD, but not its OS image.** The
  image goes through the running harness, so `harness install -` on such a board is
  refused. Provisioning the image from stage0 rescue is HARNESS-DIST L3, not built yet.
- **A bare-metal release on a USB-only board** writes the card and reboots it. It then
  says "written, not running", because nothing can confirm the new harness without
  Ethernet.
- **The firmware still reports `harness 1.0.0`.** HM matches releases by `fw_sha` until
  U7 stamps the version.

## Withdraw a release

`python -m tools.release withdraw --catalog hm-app --channel beta --version 0.2.0 --reason "…"`

A release is never deleted. Its status becomes `withdrawn` and the reason is signed in. If
it was current, the newest older release becomes current (or `--current V`).

## The release tree

`dist/release/` (and a `--mirror` directory) use GitHub's own URL layout, so one signed
channel works on every host. The asset URLs in `channel.json` are relative:
`../v0.2.0/harness_manager-0.2.0-py3-none-any.whl`.

```
dist/release/
  SoC-Labs/HarnessManager/releases/download/
    channel-hm-app-beta/channel.json(.minisig)       the rolling channel release
    channel-mps3-harness-beta/channel.json(.minisig)
    v0.2.0/<wheel> <lock> <pyverify dep>              one release per app version
    mps3-harness-v1.2.0/<sd zip> <overlays-open zip> …
  SoC-Labs/mps3-harness-aaa/releases/download/
    mps3-harness-v1.2.0/<overlays-aaa zip>            Arm IP: the private AAA repo
  plans/<catalogue>-<channel>/publish-plan.txt, publish-plan.json, report.json
```

A mirror adds `blobs/<sha256>` for a lookup by hash (HARNESS-DIST H5).

- Clients read the channel from GitHub with
  `--source https://github.com/SoC-Labs/HarnessManager/releases/download/channel-hm-app-{channel}/channel.json`.
- The dry run reads from the local path of the same file.

`make dist` deletes `dist/`, including `dist/release/`. That is harmless: a published
channel's base is always the live one.

## Fields the client parses (OTA-C)

OTA-C (commit `4e1d394`) landed, so the client's parser now reads every field the tool
emits except a few informational ones:

| Field | What the client does with it |
|---|---|
| `catalog` (top level) | keys the anti-rollback serials by (catalogue, channel); a caller that names a catalogue refuses a document of another one |
| `artifacts[].kind: dep` | downloads and sha-checks the pyverify wheel; the stage step pins the lock to it |
| `notes` (per release) | shows them as the signed release notes (`harness show`, the harness panel, the update checker) |
| `access: github-token` | fetches the asset with the token (U1). The tool emits it for app assets too |
| `target: host-kit`, `kind: rm-kit` | parses the kit, and the kit service fetches it on demand by static. A harness install never downloads it (KIT-STORE K4) |
| `vivado` (release, kit) | records the Vivado release a kit needs |
| `door`, `firmware`, `source`, `lock_info`, `legal_info`, `tag` | kept in `extra`: informational |

The tool still probes its own tree's schema at run time (`schema_allows_private_app`,
`schema_has_host_kit`), so it never emits a field that parser would refuse. On this tree
both probes pass.

**Still pending:** no client trusts a channel until the release keys are pinned (david's
U2, [KEYS.md](KEYS.md)). Until then nothing is published, so no kit or private asset is
fetched in the field.

## Tests

`tests/unit/test_otar_release.py` and `tests/unit/test_otar_harness.py` cover the tool.
`tests/unit/test_release_pipe.py`, `tests/unit/test_release_publish_script.py` and
`tests/integration/test_release_e2e.py` cover `harness-release`, `publish-check`, the
publish script, and the round trip through a fake GitHub to a virtual board's config SD.
They use throwaway keys, fake build and lock tools, and a recording runner for `gh`, so
no test uses the network or a real key.
