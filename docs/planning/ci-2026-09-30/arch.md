# ARCH: what runs the CI, and how it reaches the boards

## Decisions

| # | Decision | Recommendation | Alternatives (one-line trade-off) |
|---|---|---|---|
| A1 | Orchestrator | **GitHub Actions with self-hosted runners**, if a 15-min probe shows self-hosted jobs start while billing is blocked. Jobs are repo `make`/script targets, so the YAML stays thin. | **GitLab CI (git.soton.ac.uk)**: no billing, hub runner already installed; needs a mirror + status bridge. **Jenkins/Buildbot**: a server nobody owns. **LAVA/labgrid**: want to own the boards (§1). |
| A2 | Nightly clock | A **systemd timer** on the CI host calls `gh workflow run` (workflow_dispatch) at 17:30 and 18:00 | GitHub `schedule:` cron can be delayed, and under load jobs can be dropped |
| A3 | Machines | **New dedicated CI VM/box** for tests, wheels and board clients. **srv03335** for Vivado/VCS only (one job at a time). **mapstone-dev** only for hub-side tools. | Everything on srv03335: load 35 at 00:22 today, and the HM flakes are load flakes |
| A4 | Identity | fpgahub **write tokens** owned by `ci` (principal `ci@mapstone-dev`): `ci-night` (interactive tier) and `ci-bg-day` (background tier by `[policy]`) | CI as a unix user in group `fpga`: the socket makes it **admin** |
| A5 | Yielding to people | The CI holds a normal lease at night and **yields gracefully when anyone queues**. In the day it is background tier, so people preempt it at once. | Background tier all night: any acquire kills a run mid-swap and greybox is not restored |

## 1. The orchestrator

**Billing.** GitHub's docs say Actions usage "is free for … self-hosted runners"; the documented block is on *metered* quota. They do not say whether a **payment-failed lock** (our message: "job was not started … payments failed") also stops self-hosted jobs. **Unsure → probe** (step 1 below). The $0.002/min self-hosted fee is **postponed, not cancelled**, so the jobs must stay portable.

| System | How it fits "fpgahub is the only way to touch a board" | Verdict |
|---|---|---|
| GH Actions self-hosted | One outbound-HTTPS runner daemon; jobs call HM/pyverify, which lease through fpgahub. Native PR checks; `ci-full.yml` already targets `[self-hosted, mps3-lab]`. | **Pick** (if the probe passes) |
| GitLab CI | Prior art: TideLink's `fpga-pair` leases through fpgahub's background tier on a `tags:[fpga]` hub runner. The hub's runner is installed but **never registered** (16 Sep). The "soton VLAB" EDA runner is "not ours to load". | Fallback. Pull mirroring is Premium (instance tier unknown), so the mirror is a cron push. |
| Jenkins / Buildbot | Their resource locks would be a second board lock beside fpgahub | No: an extra server with no owner |
| LAVA | Its scheduler owns the device (device dictionary, power commands, health checks). MCC-SD / stage0-TFTP / DFX boots are not LAVA boot methods. | No |
| labgrid (tbot, r4d similar) | The coordinator locks "places" exclusively, duplicating leases; as a library it duplicates pyverify + HM checks | No |

## 2. Where each job runs

| Job | Runner label (concurrency) | Host | Why |
|---|---|---|---|
| HM `make check` (~32 min) + `tests/web` (~30 min), platform `check-ci` + lint | `ci-linux` (2) | CI box | Out of the load that caused HM's flakes. The toolchain is pinned in a podman image (gcc 11 / dtc 1.6.1), which ends the CI-vs-box drift. |
| HM wheels, `make dist`, install matrix | `ci-linux` | CI box (podman) | GitHub `container:` jobs need Docker; the hub has podman only, srv03335 has no container runtime |
| cocotb under VCS (`make check SIM=vcs`) | `eda` (1) | srv03335 | VCS licence + lab IP library |
| Vivado RM builds, kit flow, bare-metal/Linux harness images | `vivado` (1, `nice 19`) | srv03335 → CI box later | Needs `/research/CAD/Xilinx`, read-only `/research/AAA/ip_library`, and a FlexLM seat |
| Board HIL: HM service run, pyverify sweeps | `board-b1`, `board-b2` (1 each) | CI box, through the hub | The **user's own path** (SSH tunnel / REST token), so CI tests the product |
| Hub-side: soak, MCC read/REBOOT, recovery probes | `mps3-hub` (1) | mapstone-dev, shell runner, user `mps3ci` | UDP identify/TFTP do not tunnel. Use `/usr/bin/python3.11` in full (python3 is 3.6). |
| HM Windows unit + `install.ps1` | `windows` | Windows 11 VM on the CI box | Hosted `windows-latest` is billing-blocked (**licence source unsure**) |
| macOS | hosted, when billing returns | — | No lab Mac; manual check before release until then |

**Licences.** Vivado uses the university floating pool, `XILINXD_LICENSE_FILE=27070@xilinxlm{1,2,3}.soton.ac.uk` (`/etc/profile.d/xilinx.sh:2`). Runner services start non-login shells that **do not source `/etc/profile.d`**, and without the variable 2026.1 exits 42 (`services/kit/licence.py:9-15`). Set it and the 2026.1 path in the runner's env file; the profile puts **2024.1** first on PATH. KU115 needs Core (2026.1) or Enterprise (2024.1). **Unsure**: seat count, and whether a new box can mount `/research`.

## 3. Identity and secrets

- **Principal.** `fpgahub token create ci-night --role write --owner ci`, plus `ci-bg-day`. A `[policy]` rule gives `ci-bg-*` background tier, `max_ttl_s 54000`, `max_boards 1`. The holder is the credential's principal (`ci@mapstone-dev`), not `--holder`.
- **Leases.** Every unix-socket caller is **admin**; a Bearer token on the socket "is honoured instead". So:
  - leases go over REST 7246 (HM hub mode, `url` + `host`, `host` only for the SSH data-plane tunnel);
  - `mps3ci` stays out of `fpga`;
  - MCC access goes through a narrow wrapper (`mps3-mcc read|reboot <target>`), like `mps3-lease`.
- **Unattended lifecycle.** HM service `--take-lease` takes, heartbeats and releases. Backstops: TTL = window end + margin; the job's `timeout-minutes`; an 08:30 reservation end, which force-releases only its own holder.
- **Credentials** live on runner hosts only, as 0600 `file:` refs (HM refuses readable files). They are never GitHub secrets, since no hosted job touches a board. The hub key's `authorized_keys` has `from="<CI box>"`, `permitopen="192.168.1{0,1}.101:*"`, `no-pty`. Rotate quarterly, and at once after a leak.
- **Redaction** (a token was committed once): every job replaces its **own known token strings** in evidence before archiving; `gitleaks` with an fpgahub-token rule scans every push and every evidence tarball (GitHub push protection needs paid GHAS); the same scan runs as a pre-commit hook.

## 4. Board scheduling

| Board | Day (08:30-18:00) | Night (18:00-08:30) |
|---|---|---|
| B2 (HM) | Short read-only smokes as `ci-bg-day` (people preempt them); skip if held | CI owns it: HIL-AUTO `linux-nocard`, safe writes, every 30 min (~5 min per iteration). **No MCC REBOOT** until the B2 stage0 bake is fielded. |
| B1 (Linux lead) | Not CI | Only on nights the Linux lead releases in the calendar. **If a soak holds the lease, CI SKIPs and never queues.** |

- **17:30 announce job.** Read `board reservation list`; if a person booked tonight, stand down. Otherwise create `ci 18:00→08:30` (people get `reservation.starting_soon` 5 min before), write HM `--announce-only` ANNOUNCE.txt, and post it.
- **18:00.** If someone holds the board, wait up to 60 min, then SKIP (never a fail, as `ci-full.yml` does).
- **Yield rule** (small HM change, `--yield-on-queue`, ~1 day). The runner already re-reads `lease show` before every section and safe check. On a non-empty queue it finishes the check, restores greybox (40-47 s), writes `YIELDED to <principal>`, and releases: about 3 min worst case. That works for HM "Request board" (2-min deadline) and a raw `fpgahub lease acquire` alike.
- fpgahub plan D4 may demote reservations to advisory: use them as the calendar, not for force-release.

## 5. Artefacts and reporting

- **Store** (lab-internal, because RM partials carry Arm IP): `ci-artefacts/<repo>/<YYYYMMDD>-<sha12>/` on the CI box, backed up. Not GitHub artifacts: billed storage, and Arm IP on GitHub. fpgahub's bitstream repository (LRU, pins) suits full `.bit` files only.
- **Retention:**

  | What | Keep |
  |---|---|
  | Passing nightly evidence | 30 days |
  | Failing nightly evidence | 90 days |
  | Last 5 builds per branch | until replaced |
  | Anything tagged, released or fielded (static DCP + 13 overlays) | forever |

- **Reporting.** Every job writes `summary.json` + JUnit. A script renders a static nightly page (prior art: TideLink's `ci/generate_dashboard.py`) and posts **commit statuses** plus one comment on a pinned "Nightly" GitHub issue; `@dam1n19` on failure means email with no new server. A morning Claude session refreshes the artifact dashboard from the same `summary.json` (a job cannot publish one).
- **Flakes.** `ci/flakes.toml` holds test id, owner and first-seen. Listed tests get one automatic re-run and never gate. A per-test pass rate over 14 nights promotes or retires entries.

## 6. Recovery (bounded ladder; each rung logged)

| State (detected by) | Automatic action | Limit |
|---|---|---|
| B2 in stage0 rescue (identify UDP 6899; HM run stops "unreachable" / exit 15) | Wait for **b2_keeper** (hub systemd service: push, then claim) to reach `mode run`; re-adopt the new host key. Arm `--expect-reset` before planned resets. | 10 min; keeper's own caps (pushes/hour, GIVE UP on DDR calib) |
| B1 wedged (6900 refuses after retries) | One **paced MCC REBOOT on tty_00** (`soak_linux.py mcc-reboot`: refuses if another reader), then wait ~190 s for 6900 | 1 per night; never during an SD write |
| B2 wedged, dark, or keeper GIVE UP; any board silent | **Needs a person**: write `<board>.quarantine` on the hub, which later jobs SKIP. Issue comment + email, naming PB0 or a power cycle. | No retries |

Never: retry an SD write, write the DUT SST26, share tty_00, or force-release a person.

## Cost, owner, first steps

- **Cost.** CI box: 32 cores, 128 GB, ~2 TB (**unsure**, roughly £4-6k, or an iSolutions VM). A Windows licence. About 1 day of HM work (yield) and about 3 days of wiring.
- **Owner.** david (runner admin + hub sudo). The HM lead owns the B2 nightly job; the Linux lead owns the B1 calendar.
- **Catches.** Regressions across HM, the harness and the DUTs, every night, without anyone running gates by hand.

1. **Probe (15 min).** A self-hosted runner on srv03335, a `ci/probe` branch; does the job start? This decides A1.
2. **Hub identity (david, 30 min, sudo).** User `mps3ci`, the two tokens, the `[policy]` rule, `authorized_keys` restrictions.
3. **First nightly (1 day).** B2 `linux-nocard` through the HM service with `--take-lease`, a 17:30/18:00 timer, results in the artefact store and a GitHub issue comment. Add the yield rule after.

## Sources

**Repos:**
- HM `docs/HIL_AUTO.md:8-9,16-41,122-160,241-263`
- HM `docs/LEASE_REQUESTS.md:14-37,180`
- HM `docs/HUB_MODE.md:3,39,103,123-128,184-193`
- HM `src/harness_manager/services/lease.py:1-30`
- HM `src/harness_manager/services/kit/licence.py:1-17`
- HM `.github/workflows/ci.yml`, `install-matrix.yml:40-44`
- HM `docs/assessment/2026-09-24/Q1_TESTS.md:7-9`
- `/etc/profile.d/xilinx.sh:1-2`
- fpgahub v0.3.0:
  - `UPGRADING.md:13-62,110-113`
  - `docs/GUIDE.md:552-629`
  - `README.md:401-409`
  - `src/fpgahub/config.py:2893-2922`
  - `src/fpgahub/daemon.py:362-388`
  - `docs/BITSTREAM_REPOSITORY.md:149-166`
- fpgahub ac8b138:
  - `docs/IMPROVEMENT_PLAN_2026-09.md:105-108,449,680-689,887`
  - `.gitlab-ci.yml:10-18`
- tidelink `.gitlab-ci.yml:945-1006`
- Platform 3f7cea2:
  - `docs/CI.md:11-19`
  - `.github/workflows/ci-full.yml:8-13,47-73`
- lx:
  - `scripts/linux_board/b2_keeper.py:2-50`
  - `b2-keeper.service`
  - `mps3_lease.sh:1-20`
  - `scripts/soak_linux/soak_linux.py:1-60`
  - `docs/planning/linux_lanes/FINDINGS_TRIAGE.md:68`

**Web:**
- https://docs.github.com/en/actions/concepts/billing-and-usage
- https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions
- https://github.com/resources/insights/2026-pricing-changes-for-github-actions
- https://github.com/orgs/community/discussions/165506
- https://docs.github.com/en/actions/reference/limits
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.gitlab.com/user/project/repository/mirror/pull/
- https://labgrid.readthedocs.io/en/latest/usage.html
- https://docs.lavasoftware.org/lava/lava-scheduler-device-dictionary.html
- https://docs.lavasoftware.org/lava/healthchecks.html
