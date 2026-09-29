# FRESH lane: does a new user succeed by following the guide, and are the docs true?

Research only, 30 Sep 2026. Nothing was run except read-only checks on srv03335 (tool presence, one unprivileged `bwrap` probe).

## Decision

**Recommendation: one in-house extractor (`docrun`) plus three layers and a drift gate.**

| Layer | What | Cadence |
|---|---|---|
| **L0 parse** | Every tagged command in the guide and HM docs is parsed, not run: `harness-manager …` lines through HM's own `make_parser()`, `pyverify.cli …` lines through pyverify's parser. Placeholders must come from a declared list | every commit, in `make check` |
| **L1 executable docs** | Tagged blocks run in order in a **bubblewrap sandbox on srv03335** (a throwaway home, the team's files hidden). Output is compared with the doc's own `<span class="o">` lines | nightly; weekly full Vivado build; board tier inside the HIL window |
| **L2 agent new user** | A pinned, context-free `claude -p --bare` in the same sandbox, given only the rendered guide and a fixed task list. It returns structured findings, which are scored and diffed against last week's | weekly, plus 2 runs per release candidate |
| **Drift gate** | `check_doc_claims.py`: the numbers the docs state (times, sizes, versions) are checked against the latest evidence JSON | nightly, and at release |

Alternatives:
- **Runme or pytest-markdown-docs off the shelf.** Rejected: the guide is HTML fragments (45 `<pre class="cmd">` blocks), not Markdown, and pytest-markdown-docs runs Python fences only. We keep **prysk** (the maintained cram fork) as the runner under our extractor.
- **Only the human second-user test.** It catches clarity problems but not drift between releases. It stays planned as a complement (runbook:252).
- **Containers as the main environment.** No podman or docker on srv03335, and a container gets no Vivado, licence or `/etc/profile.d` realism. Use them only for HM's distro install matrix, which already exists.

## 1. What exists

| Asset | Fact | Reuse |
|---|---|---|
| P8 clean-account run | The `cr` wrapper runs `env -i HOME=… bash -lc` and logs each command, its rc and its time (CLEAN_ACCOUNT_SYNTHESIS_RUN_2026-09-30.md:104). It keeps david's SSH keys and groups (:24) and has a "Known on `506a5ef`" list (:79) and an evidence template (:227) | Becomes the L1/L2 runner, and the known list becomes `docs-known.toml` |
| KIT-NIGHT | Found 12 findings (README.md:167-179). F1: `bash -lc` never reads `~/.bashrc`. F2: the build takes 28-56 min, not the documented 35 or 20. F3: 21 CRITICAL WARNINGs that are harmless. F9: no board-free overlay listing | The findings-table format (step, doc says, what happened, fix, owner) is the template |
| Platform guide | HTML in `docs/platform-guide/src/*.html`: 45 `pre.cmd` blocks with `.o` (output) and `.c` (comment) spans, 11 `<hub-host>` placeholders, about 17 "about N min/s" claims. The version is pinned in prose (02_front.html:45). Appendix D is a hand-kept "Documentation drift register" (40_appx.html:88) | Tag the blocks; make the pins machine-readable |
| HM install matrix | Rocky 8/9 (+uv), Ubuntu 22/24, Debian 12 and Fedora in containers, plus an offline wheelhouse job (install-matrix.yml:55-123), weekly. The Windows job runs `install.ps1` in PowerShell 5.1 and checks the demo UI (ci.yml:95). **Billing-blocked** | Keep. It already covers Python 3.10-3.13 and the OS matrix |
| `smoke_install.sh` | Installs into a throwaway HOME, checks `--version`, the demo UI with its CSP, an upgrade and an uninstall (scripts/smoke_install.sh:1-14) | The L1 "install" tier |
| Doc gates | Platform: `check_status_citations.py` (Makefile:175) and `check_fielded_shell_claims.py` (:96). HM: `test_cli_help_coverage.py` | The drift gate joins this family |
| DOCS-DRIFT-2 | Done by hand. It found "a push takes seconds" where the evidence says 38.7-80.6 s, and "about 25 iterations" where 20 ran (HM e032a9a) | Exactly what the drift gate automates |

## 2a. Executable docs (L0/L1)

**Tagging.** In HTML: `<pre class="cmd" data-id="s6.3-ping" data-run="free|vivado|hub|board|manual|illustrative" data-expect="glob|re|none" data-evidence="docs/evidence/…">`. In Markdown the same keys go in the info string: ```` ```bash run=free id=install-2 ````. GitHub ignores the extra words.

`docrun extract` is roughly 200 lines of stdlib `html.parser` plus a fence parser. It emits one prysk `.t` file per chapter, with `.o` lines as expected output. Volatile fields (ports, tokens, SHAs, times) carry `(glob)` or `(re)`. An untagged block, or a placeholder not declared in `docs-env/<site>.env`, is a **failure**, so new text cannot silently opt out.

**Keeping expected outputs honest.** `data-evidence` names the evidence file that output was pasted from. L0 checks the snippet still matches that file. L1 compares it with the live output. A mismatch is either a regression or drift, and the bot proposes the new output as a patch for a person to approve.

**Steps that need scarce resources:**

| Tag | Runs how |
|---|---|
| `free` | bwrap sandbox, nightly |
| `vivado` | srv03335 only (licence from `/etc/profile.d`, 2026.1 needs group `arm`). Nightly with `STOP_AFTER=link` (KIT-NIGHT used this for F3; ~5-10 min, estimate). Full build weekly and per RC: 28-56 min measured, 4 threads, 4.7 GiB, `nice 10` |
| `hub` | Read-only verbs only (`hub test`, `hub targets`, `fpgahub lease show`), from a CI hub account. Never a lease |
| `board` | Only inside the nightly board-2 HIL lease, under the CI principal, from an allowlist: `program minimal` / `info` / `restore` (~3-4 board-min; a push takes 38-80 s). The same commands also run against `--demo` or HM's `tests/fakes/virtual_board.py` to check their shape |
| `manual` | Destructive or person-needed steps (SD write, MCC REBOOT, claim, PB0): L0 parse only. **Never automated**, per the brief's hard rules |

## 2b. Agent-driven new-user runs (L2)

- **Isolation (verified feasible today).** Unprivileged bubblewrap 0.4.0 works on srv03335: `bwrap --ro-bind / / --tmpfs /home/dam1n19` hid the home while Vivado 2026.1 stayed readable.
  - Also mask `/tmpdir` (other sessions' notes, including this plan and KIT-NIGHT) and `/tmp`.
  - Bind in only: the guide at the pinned SHA, the kit zip, and a **read-only GitHub deploy key**.
  - `--bare` skips CLAUDE.md, auto-memory, hooks and plugins; a fresh HOME has no `~/.claude`.
  - Preflight canary: `ls ~/SoCLabs /tmpdir/claude-* ~/.claude` must fail, or the run aborts.
- **Repeatability.**
  - Pinned: `--model <exact id> --effort <level> --exclude-dynamic-system-prompt-sections --no-session-persistence --disallowedTools WebSearch,WebFetch --max-budget-usd 15`.
  - Versioned files: a system prompt (`newuser_brief.md`, modelled on runbook:255-269), a task list (`newuser_tasks.toml`) and `pins.toml`.
  - `--output-format stream-json` for the transcript; `--json-schema` for the findings `{task, step_id, class, doc_anchor, expected, observed, blocked, minutes}`.
- **Tasks (fixed).**
  - T1 install HM. T2 demo check. T3 kit import. T4 build `minimal` (link nightly, full weekly). T5 check and pack. T6 hub add and test (read-only).
  - T7 answer 6 comprehension questions against an answer key, e.g. "how long does the build take?", "what must you never write?". This measures clarity with no hardware.
  - Board tasks (program/info/restore) stay lead-run in a slot, as on 30 Sep.
- **Scoring rubric.**
  - Objective task pass/fail from rc and markers (`HM_RM_BUILD_COMPLETE`, `kit check` rc 0).
  - **Improvisation count**: commands the agent ran that match no guide block after placeholder normalisation. A capable agent routes around bad docs, and this metric exposes it.
  - Measured time against the doc's stated time.
  - Findings, classified by a separate judge call (also context-free) as doc-wrong, doc-unclear, tool-bug, env or agent-error.
- **Comparable night to night.** Diff the normalised command sequence and the findings, keyed by fingerprint `(step_id, class, anchor)`: new, resolved, still open. Raw transcripts vary, so don't diff them. Run twice per RC to see the variance.
- **Model choice.** Use a mid-tier model (e.g. Sonnet 5) as the "new user": it is less able to route around unclear text than Opus, which makes the test harsher and cheaper. Once per RC, add an Opus run.
- **Personas.** P-bash is the default. P-tcsh uses login shell tcsh; FIX-PACK-2 hit csh on the hub (hubtest.py:14-23). Likely first catch, not yet run: `install.sh`'s PATH advice for a non-bash/zsh/fish shell is "add `export PATH=…` to `~/.profile`" (scripts/install.sh:636-647), and tcsh reads neither. Unsure: `tcsh -l` is honoured only as the sole flag, so the wrapper must start it with argv[0] `-tcsh`. P-noarm (no `arm` group, decision A2) needs a real CI account.

## 3. Fresh environments

| Option | Verdict |
|---|---|
| **bwrap sandbox on srv03335** | **Primary.** Available now, no admin needed, real RHEL 8.10 + `/etc/profile.d` + licence + Vivado. Gap: david's uid and groups |
| Dedicated CI account `mps3ci` (srv03335 + hub) | Second step (admin). Tests SSH-key setup, group `arm` (A2), `fpga` via real sssd, and a tcsh variant. A fresh `usermod -aG fpga` reproduces the stale-cache case naturally |
| Rootless podman on srv03335 | Plausible but **needs admin**: podman is not installed, and the domain account has no `/etc/subuid` entry. `newuidmap` has `cap_setuid`, user namespaces are on, and home is local XFS. Worth it only if GitHub stays blocked (it would run `install-matrix.yml` locally) |
| VMs | Not on srv03335: it is a VMware guest with no `/dev/kvm` |
| Windows 11 | The `ci.yml` other-os job already covers `install.ps1` (needs GitHub billing or a self-hosted Windows runner). An agent persona on Windows needs a lab Windows VM with OpenSSH: later |
| Hub side | Users never install fpgahub, so test the **user's** hub path: `mps3ci` on mapstone-dev, nightly `hub test` (checks sg, csh and sshd MaxStartups). The server install (`packaging/install.sh --role server`) belongs to fpgahub's owner; a Rocky 8 VM or container job can cover it later |

Matrix that matters:
- Shells: bash login, tcsh login.
- Python: 3.9 must be refused with the right message; 3.10-3.13 are already in the matrix; srv03335 has 3.6/3.8/3.11/3.12.
- OS: RHEL 8 (srv03335, real), RHEL 9 / Ubuntu 22/24 (containers), Windows 11.

## 4. Doc-accuracy loop

1. **Finding to fix.** The nightly job files or updates one GitHub issue per fingerprint, labelled `docs-finding`, in the owning repo. A person triages weekly (~15 min) into owner + fix. Appendix D is generated from the open issues instead of kept by hand. Known items live in `docs-known.toml` with an owner and an expiry. A known item that stops reproducing is flagged "fixed? remove".
2. **Drift gate.** `docs/claims.toml` maps claim ids to a doc regex, an evidence source and a tolerance. Example: `push.linux.nanosoc`, source HIL `iter-*/e1_program_ila.json:seconds`, rule "the doc's range must cover p10-p90 of the last 14 nights". Sources are HIL-AUTO JSONs, kit build receipts plus `time -v`, the `smoke_install` "installed in N s" line, and `cr` `took=` lines. It is red when a range no longer covers the evidence, or when a version or id disagrees with its authority (HM `__version__`, `FIELDED_SHELL.md`, kit sha256).
3. **Pinning.** `docs/platform-guide/pins.toml` holds `{platform_sha, hm_tag, fpgahub, static_id, kit_sha256, vivado}`, and the front-matter sentence is generated from it (as `gen_pins.py` does for pins). Two nightly lanes:
   - **pinned** must stay green; red means the environment regressed.
   - **head** (guide vs HM `main` / platform `master`) warns of drift before a release.
4. **Release gate** (add to HM `docs/RELEASING.md` and the platform release). All five must hold:
   - L0 green;
   - L1 `free` + `vivado` green at the release SHAs;
   - the drift gate green;
   - one L2 run with **zero open doc-wrong or blocked findings** (doc-unclear allowed with an owner);
   - `pins.toml` equal to the release tags.
   The gate writes its record into `docs/evidence/`.

## 5. First 3 steps after 6 Oct, and ongoing cost

1. **Baseline (~1 h, guide lead).** File the 30 Sep P8 evidence in `docs/evidence/2026-09-clean-account/`, record that session's model and token cost (the only real cost datum), and seed `docs-known.toml` from runbook:79 plus KIT-NIGHT F1-F12.
2. **L0 (~1 day, estimate).** Tag the 53 guide `<pre>` blocks and the ~33 bash fences in HM README/INSTALL/USER_GUIDE. Write `docrun extract` and the parse-only test into HM `make check` and platform `make check` stage 2.
3. **L1 nightly (~1 day, estimate).** Wrap `cr` in bwrap (`nr`), cron on srv03335 at 01:00 (cron works; `systemd --user` does not: no bus). Tiers `free` + `vivado(link)` against `pins.toml`, a metrics JSONL for the drift gate, and a notice on red. L2 follows once L1 is stable.

| Layer | Wall | CPU (shared srv03335) | Board | Agent $ |
|---|---|---|---|---|
| L0 | <30 s per commit | nil | 0 | 0 |
| L1 nightly (free + link) | ~15 min | ~1 core-h | 0 | 0 |
| L1 weekly full build | 28-56 min | ~2-4 core-h, 4.7 GiB | 0 | 0 |
| L1 board tier | ~4 min in the HIL lease | nil | ~4 min/night | 0 |
| L2 per run | 1-2 h (waits on Vivado) | as L1 | 0 | **~$3-6 on Sonnet 5, ~$4-8 on Opus 5.5** (estimate: ~60 turns, ~3.5M mostly-cached input, ~60k output) |

Monthly: about $30-80 for weekly runs plus RC runs; nightly L2 would be about $100-250.

**Decisions for david:**
- **D1.** An Anthropic API key and workspace for CI with a spend cap: `--bare` accepts only `ANTHROPIC_API_KEY`. This is also the secrets-hygiene boundary.
- **D2.** `mps3ci` accounts on srv03335 and mapstone-dev (tcsh variant; with and without `arm`).
- **D3.** Admin install of podman and subuid (optional).
- **D4.** Windows host: GitHub billing or a lab VM.

## Sources

Repo:
- guide `docs/planning/CLEAN_ACCOUNT_SYNTHESIS_RUN_2026-09-30.md:24,79,104,145,227,252`
- `docs/platform-guide/src/02_front.html:45`, `40_appx.html:88`, `22_first.html:28-81`
- `hm-kit-night/docs/evidence/2026-09-29-kit-night/README.md:167-179`
- HM:
  - `.github/workflows/install-matrix.yml:25,55-123`
  - `.github/workflows/ci.yml:95-140`
  - `scripts/smoke_install.sh:1-14`
  - `scripts/install.sh:636-647`
  - `src/harness_manager/settings/hubtest.py:14-23`
  - `docs/INSTALL.md:18-33,397-428`
  - commit `e032a9a`
- platform `Makefile:96,175`

Web:
- [prysk](https://github.com/prysk/prysk)
- [cram](https://github.com/aiiie/cram)
- [Runme cell options](https://docs.runme.dev/configuration/cell-level/)
- [pytest-markdown-docs](https://github.com/modal-labs/pytest-markdown-docs)
- [byexample](https://byexamples.github.io/byexample/overview/usage.html)
- [rootless podman subuid](https://rootlesscontaine.rs/getting-started/common/subuid/)

Pricing is from the bundled claude-api reference (Opus 5.5 $4/$20 and Sonnet 5 $2/$10 per MTok; cache reads ~$0.20).
