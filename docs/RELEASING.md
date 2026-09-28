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
They use throwaway keys, fake build and lock tools, and a recording runner for `gh`, so
no test uses the network or a real key.
