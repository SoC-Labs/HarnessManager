# Release signing keys

> **PENDING david's U2 decision.** No key exists yet, and `trust.PINNED_KEYS` is empty, so
> every channel is refused by every client. That is the safe default. This page is the plan
> for the ceremony (`docs/design/HM_SELF_UPDATE.md` §4.4, `HARNESS_DISTRIBUTION.md` §4.3).
> Its facts (key ids, dates, where the backups are) are filled in once the ceremony has
> happened. **The secret keys are never recorded here.**

## The roles

One trust store covers the app catalogue (`hm-app`) and every harness catalogue
(`mps3-harness`, …). The roles are T7's (`services/update/trust.py`):

| Role | Signs | Custody (recommended) |
|---|---|---|
| `root` | `keys.json` rotation statements only | cold: two encrypted USB sticks, in two places, never on a networked machine |
| `harness-release` | `channel.json` on stable, beta and dev, for both catalogues | passphrase-protected, on david's machine; david signs stable |
| `app-ci` | dev only | no passphrase, for CI once GitHub Actions is back (optional) |

Clients pin the public halves of root and release (and app-ci) in `PINNED_KEYS`. A new
release key arrives through a root-signed `keys.json` with a higher serial. A root key never
rotates over the air: a new root ships in a new app build.

## The ceremony (david, about 30 minutes, once)

1. On an offline or trusted laptop, make the two keys, each with a strong passphrase:
   - `minisign -G -p root.pub -s root.key`
   - `minisign -G -p release.pub -s release.key`
2. Optional: `minisign -G -W -p ci.pub -s ci.key` (no passphrase), for CI when it is back.
3. Copy `root.key` to two encrypted USB sticks and store them in two places. Delete it from
   the laptop.
4. Keep `release.key` on david's machine only.
5. Open a PR (the keys PR) that:
   - fills `PINNED_KEYS` in `src/harness_manager/services/update/trust.py`:
     `pinned("RWQ…", ROLE_ROOT, note="root 2026")`,
     `pinned("RWQ…", ROLE_RELEASE, note="harness-release 2026")`;
   - fills in the table below: key ids, dates, backup places. Never the keys.
6. The first release built from that PR is the **bootstrap**. Installs of 0.1.0 pin no keys,
   so they need one manual installer run to reach it.

| Key | Key id | Made | Backups |
|---|---|---|---|
| root | _pending U2_ | | |
| harness-release | _pending U2_ | | |
| app-ci | _pending U2_ | | |

## How the release tool uses a key

The key source is a parameter, so any of U2's options works without a code change:

| Setting | Argument | Environment |
|---|---|---|
| the signer: `minisign` (the CLI) or `python` (HM's own signer, throwaway keys only) | `--signer` | `HM_RELEASE_SIGNER` |
| the secret key file | `--secret-key` | `HM_RELEASE_SECRET_KEY` |
| its public key (needed first: the key id goes inside the signed channel) | `--public-key` | `HM_RELEASE_PUBLIC_KEY` |
| the minisign binary | `--minisign` | `HM_MINISIGN` |

- **The minisign CLI signs.** It is the default, and the only signer `--publish` accepts.
  It prompts for the passphrase on the terminal. The secret key never enters a Python
  process.
- **Every signature is checked after signing** against the public key. A secret key and a
  public key that are not a pair are refused.
- **`--publish` refuses a key the tree does not pin** (`trust.PINNED_KEYS`), because no
  client could verify its channel. It also refuses the app-ci key on any channel but dev.
- **HM's Python signer refuses a passphrase-protected key.** It never decrypts one.

If U2 puts the release key with the lead as well (option b), nothing changes but the path in
`HM_RELEASE_SECRET_KEY`. If david signs every stable release (option a), he runs
`make release-promote` himself, or the lead hands him the dry-run tree to sign.

## Throwaway keys (tests and dry runs)

`python -m tools.release keygen --throwaway /tmp/otar-keys` writes an **unencrypted**
pair for tests and dry runs. It refuses any directory outside the system temp dir. A
channel signed with such a key is trusted by no client build.
