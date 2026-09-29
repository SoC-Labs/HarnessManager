# CI-PLAN lane MUTANT: would our tests catch real bugs?

## Decision

**Recommendation: two kinds of mutant, each on its own job.**

1. **Code mutants.** A tool changes our source and a test must go red. Tools: mutmut 3 (Python), Mull (firmware C), and Certitude (if licensed) or MCY (shell RTL). Scope: the ~15 files where a false pass ships a broken board. Nightly and incremental. Report-only at first, then a score ratchet.
2. **Fault mutants.** We break the *world*: a bad overlay, a harness that lies, a broken fake board. The product must refuse it, or a check must fail. This is a "mutant zoo" built once per static, plus mutated fakes. **Hard release gate from day one.** A mutant that is not caught, or is caught with the wrong error, blocks a harness or HM release.

Why both: code mutation cannot find a check nobody wrote. Three missing checks today (read, not run; confirm board-free first):
- `mactest` passes a MAC that drops frames. It only asks that tx and rx *advance* (`host/pyverify/pyverify/mactest.py:116`), and no test uses a lossy fake (`host/pyverify/tests/test_mactest.py`).
- HM pairs a clearing with its partial **by file name** (`harness-manager/src/harness_manager_mps3/overlays.py:281-300`).
- Every automatic HIL check of "the RM runs" reads the harness's own answer (`result.verified`, `result.rm_id`: `src/harness_manager/checks/plans.py:149-158`). The console checks are MANUAL (`plans.py:488,539`). A harness that lies passes.

| Alternative | Trade-off |
|---|---|
| A. Code mutation only | Cheap and board-free. Misses all three holes above. |
| B. Zoo only | Catches missing boundary checks. Says nothing about the 6000 HM tests. |
| C. Certitude for everything (RTL + C) | Best fit with VCS. Licence unconfirmed. No Python, no hardware. |

## 1. Python (HM, pyverify)

**Tool: mutmut 3** (3.8.0, Sep 2026). One forked child per mutant; runs only the tests that reach the mutated function; re-tests only changed functions (incremental cache); POSIX-only (fine). Alternatives: cosmic-ray (per-PR `cr-filter-git`, but whole test command per mutant: slower); mutatest (unmaintained since 2022: skip).

| Scope | Files | Why |
|---|---|---|
| P0 runner verdict | `checks/run.py` (`evaluate` :292, verdict, allow-list), `checks/plans.py` | A runner that passes a broken board is the worst bug. |
| P0 refusal rules | `core/pack.py` `preflight_refusal`/`kit_refusal` (:76,:134); `services/deploy.py` `mark_identity` (:97-114); `harness_manager_mps3/deploy.py` `_check_*` (:753-869); `overlays.py`; `bitcheck.py`; `services/kit/build.py` `receipt_checks` (:51); `services/update/trust.py` | These decide exit 14 vs 15 (`core/errors.py:17-31`). |
| P0 pyverify | `overlay.py`, `pusher.py` (header), `swap.py`, `mactest.py`, `rm_id.py`, `slot.py` | The codec and the refusals. |
| P1 | the rest of HM | Weekend runs, sharded. |

**Estimate (unverified):** P0 ≈ 6k LOC → 3-5k mutants × 2-10 s → first run 4-8 CPU-h, nightly incremental ≤0.5 CPU-h. Full HM (86k LOC, ~50k mutants): weekends only.

**Traps:**
- Tests that spawn `python -m harness_manager.cli.main` must import the mutated tree. Either point the children's `PYTHONPATH` at mutmut's `mutants/`, or use `InProcessHm` (`tests/fakes/hil_auto.py`).
- HM uses pyverify as a vendored wheel (`vendor/mps3_pyverify-0.1.0-*.whl`). Mutate pyverify in the platform repo against its own tests. Then do a second run with pyverify installed editable in HM's venv, so HM's tests also judge pyverify mutants.
- `pytest-timeout` of 60 s (`pyproject.toml:80`) kills looping mutants. They count as killed.

**The runner's own logic:** `evaluate()` passes a *missing* field when `optional=True` (`run.py:295`), so a harness that stops sending a field still passes. Give every optional expectation a board-fault mutant (section 5c).

## 2. Firmware C

**Tool: Mull** (LLVM plugin). Release 0.34.1 (Sep 2026) says "RHEL now targets LLVM 21", and srv03335 has clang 21.1.8.

1. Build the existing host tests with `CC=clang` and `-fpass-plugin=mull-ir-frontend-21 -g -grecord-command-line`. `CFLAGS` is `?=` (`firmware/test/Makefile:37`). Drop `-Werror` for clang-only warnings.
2. Run `mull-runner-21 bin/test_*` for each binary.
3. Configure `mull.yml`: `includePaths` = config_agent, coordinator, common, overlay_store, fw_stage0, harnessd; `excludePaths` = tests and fakes; `gitDiffRef: origin/master` for PR mode.

**Targets:** swap path (~10k LOC), stage0 (2.9k, `test_stage0_*.c`), harnessd (9.5k; its Python e2e tests need Mull's `--test-program`, unverified: start with the C tests). **Cost:** test binaries run in ms, so the swap path is ≲30 CPU-min (estimate).

**Alternatives:** dextool (heavier install); universalmutator (regex, recompiles per mutant: one-offs only). Pair with `firmware/test/coverage.sh`: a survivor on an unhit line is a coverage gap, not a weak test.

## 3. RTL

**Keep the hand-written named mutants** (the must-kill set):
- `tests/dfx_ctl` `make control-wdt-unclamp` ("MUST FAIL")
- `tests/usd_spi/mutate_cd_gate_ignored.py`
- `tests/dut_egress/mutate.py` (`MUTATION-POINT` tags; exits non-zero if a substitution does not apply)
- `tests/rmii_conformance/neg_control/`

| | MCY (Yosys) | Certitude (Synopsys) |
|---|---|---|
| On srv03335 | No (yosys/mcy not on PATH; the OSS CAD Suite tarball can go in $HOME) | Installed: `/eda/synopsys/2022-23/RHELx86/CERTITUDE_2022.06-SP2`. **Licence not confirmed.** |
| SystemVerilog | Native Yosys SV support is weak; needs yosys-slang or sv2v | Full SV, mutated at source level |
| Fit with the benches | Exports a netlist `mutated.v`. One `mutsel` input covers many mutants, so one compile. Cocotb tests that read internal signal names will break. | Instruments the source and keeps the hierarchy. Drives the existing VCS scripts. Reports activation → propagation → detection. |
| Equivalent mutants | Formal miter tags them `NOCHANGE` | Built in |

39 of 41 benches are `SIM ?= vcs`, so each mutant run takes a VCS licence. **Decide:** run `lmstat` for a Certitude feature (5 min, david or an admin). Licensed: Certitude + VCS on the shell IPs. Not: MCY + yosys-slang with a `mutsel` wrapper, Icarus-capable benches first.

**Where to mutate:**
1. **Shell IPs** where a silent change ships a broken board: dfx_ctl (decouple, gate, `rm_id_valid`, watchdog), usd_spi (card-detect gate), dut_egress, uart_bridge, CSR decode, RMII paths.
2. **Control points**: FSM next-state, enables, comparators, reset values, address slices. Use MCY `select`, with `size` 100-300 per module.
3. **Spec mutants** (custom, cheap): transpose two fields or change a reset value in a single source of truth (RDL, regmap, boundary), regenerate, run the gates. All green = no gate checks an independent witness: the MINFL/MAXFL "reference shares the bug" class, and the equivalence gate that could not fail.
4. **Parameter mutants**: bench at the width the block design instantiates vs the RTL default. The CSR decode regression (the width-12 hole) came from this difference.

**Cost:** 5 modules × 200 mutants × 10-60 s ≈ 3-15 CPU-h: **weekly**.

## 4. Hardware mutant zoo

The zoo lives in `zoo/<static_id>/` and stays **private** (eth_ss contains Arm CMSDK). Mutants come two ways:
- **Derived:** a script edits bytes or manifests of the fielded overlays. No Vivado.
- **Built:** via the kit flow, 30-60 min each. About 5 RMs ≈ 3-5 h, once per mint. They use rm_id in the user range `0x8000-0xFFFF` (`core/pack.py:119`) and are named `zoo_*`.

Each entry declares:
- the stage that must catch it;
- the exit code or net-protocol `code`;
- a no-side-effect witness: `stats.icap` and `swap_n` must not change after a host refusal.

**A mutant caught with the wrong error counts as a FAIL** (the ethmac lesson: the right answer for the wrong reason).

**Tiers:**
- **Z0:** HM refuses it; the board is never touched. Nightly on board 2.
- **Z1:** reaches harnessd and is refused there. A test-only pusher bypasses the host checks. Attended until board 2's stage0 bake is fielded.
- **Z2:** the mutant actually loads. Attended the first time, then weekly.

| # | Mutant | Must be caught at | Expected (real code) | Tier | Predicted |
|---|---|---|---|---|---|
| M1 | manifest `static_id` ≠ board | HM preflight `shell_id matches` | `IncompatibleError`, exit 14 (`pack.py:76`, `services/deploy.py:97`); pyverify `SwapError` "static_id mismatch" (`overlay.py:229`) | Z0 | caught |
| M1b | same, raw push | harnessd header check | `CFG_AGENT_ERR_STATIC_ID` (`config_agent.h:76`) | Z1 | caught |
| M1c | partial built on another static, manifest re-stamped | nothing. `static_binding` is always UNCHECKED (`bitcheck.py:398`), USERCODE is UNCHECKED over Ethernet (`mps3/deploy.py:848`), and "UNCHECKED never blocks" | **Never load** (can corrupt the static) | host only | **survives**. Ticket: require a receipt binding the static for non-fielded overlays |
| M2 | payload bit flipped, manifest stale | HM `crc and length` | `RefusedError`, exit 15 | Z0 | caught |
| M2b | good header CRC, 1 bit flipped in frame data only | harnessd's running CRC. Large partials are "detect-after-ICAP + park-safe" (`config_agent.c:554-560`) | `CFG_AGENT_ERR_CRC`; swap `ok:false`; `restore` → rm 0x0. Never flip FAR/header words. | Z1 attended | caught |
| M3 | manifest rm_id ≠ netlist | kit `receipt_checks` `rm_id`; otherwise the `SWAP_VERIFY` compare of `DFXCTL.RM_ID` (`swap_fsm.c:834`) | `stats.swap_err` = `verify` | Z1 | caught |
| M4 | clearing from another RM, renamed, manifest regenerated | name-only pair check; the bitcheck frames-inside test likely sees the same box (unverified) | the effect on silicon at the next swap-away is unknown | host; Z2 attended | **survives host-side** |
| M5 | DUT holds the bus / IRQ storm (`irq_out`=1, `uart_rx_tready`=0, `dut_lockup`=1) | the harness keeps serving; `restore` finishes within T | `svc_max_us` / `svc_skipped` / `lock` in `stats`; restore exits 0 | Z2 | unknown |
| M6 | stuck UART (uart_echo that never echoes) | only MANUAL console checks today | needs an automatic echo check | Z2 | **survives** |
| M7 | decoupler-unsafe outputs (toggles while in reset, `mdio_oe`, `tx_en` jabber). **Never drive `qspi_*`** (DUT SST26 rule). | the decoupler during the swap, then harness survival | restore exits 0; link stays up | Z2 attended | unknown |
| M8 | eth_ss drops 1 frame in 8, no FCS error | `mactest` (`mactest.py:116`) | should fail | Z2 | **survives**. Ticket: rx Δ == tx Δ after a drain |
| M9 | slow card | harnessd host build + fake_usd latency. On silicon: board 1 only, with a known-slow card, attended | HM shows the job writing and never retries mid-write | fake nightly | unknown |
| M10 | harness lies in `stats` (frozen `up_ms`, stale `rm`, `verified:true` on a failed swap) | harnessd host (hal_mock) mutant. On silicon: a netboot mutant rootfs on board 2, only after its stage0 bake is fielded | cross-witnesses:<br>• `up_ms` vs the host clock<br>• `swap_n` +1 per swap<br>• `icap` grows by the pushed bytes<br>• `sid` = `ping` = stage0 identity<br>• `rm` matches DUT behaviour | fake nightly | **survives today** |

## 5. Fakes vs silicon

**Already there:** FakeShell ↔ firmware `ctrl_echo` shape conformance (`tests/firmware_logic/test_fakeshell_conformance.py`, closed I31); FakeShell linux profile ↔ harnessd host build (`src/linux_harness/sw/harnessd/tests/test_fakeshell_parity.py`); fake hub ↔ real fpgahub golden (HM `tests/fakes/t8_record_fpgahub_golden.py` + `tests/unit/test_t8_contract.py`).

**Missing: fake ↔ silicon *behaviour*.** The push-before-swap bug lived in that gap: "green against a fake more permissive than the firmware" (`pyverify/swap.py:46-49`).

1. **Twin run, nightly.** Run the same HIL-AUTO plan (`linux-nocard`, `--writes safe`) on board 2 and on `VirtualMps3` (`tests/integration/test_hil_auto_virtual.py` already does the fake half).
   - Normalise the evidence JSON: drop times and counters. Keep verdict, exit code, key sets, types, and the `stats.swap` state sequence.
   - Any difference means a fake that lies or a board bug. File a ticket.
2. **Harness golden (the t8 pattern).** Record board 2's read-tier answers once per harness release. Replay them against the FakeShell linux profile in a unit test.
3. **Mutate the fakes** (nightly, 1-2 CPU-h). Survivors in each case are:
   - a. mutmut on `pyverify/testing/fakeshell.py`, judged by the conformance, parity and golden suites. Survivors = fake behaviour that no contract pins.
   - b. "Permissive fake": remove one refusal branch at a time, then run HM's suite. Survivors = harness refusals whose handling HM never tests.
   - c. Board-fault mutants, judged by `test_hil_auto_virtual`. Survivors = broken boards that the HIL plan passes.

## 6. First steps, cost, tickets

**First 3 steps:**
1. **Control registry** (~1 day). List every `check`/`check-ci` gate and its negative control. A meta-gate fails if a control passes or a gate has no control. About 129 files already mention "negative control" or "MUST FAIL". This step goes straight at "a gate that couldn't fail".
2. **Board-free zoo** (2-3 days). Build M1-M4, M8 and M10 as FakeShell/VirtualMps3 knobs, judged by `test_hil_auto_virtual`. This confirms or clears the four predicted survivors with no board.
3. **mutmut on the P0 Python scope** (1 day of setup + one overnight run). Report-only.

After that: Z0/Z1 on board 2, Mull on the swap path, the Certitude `lmstat` check, and the built RMs at the next mint.

| Job | Cadence | srv03335 | Board 2 |
|---|---|---|---|
| mutmut P0 (incremental) | nightly | ≤0.5 CPU-h (first run 4-8) | 0 |
| Mull, swap path + stage0 | nightly | ≤0.5 CPU-h | 0 |
| Fake mutation (5a-c) | nightly | 1-2 CPU-h | 0 |
| RTL, Certitude or MCY | weekly | 3-15 CPU-h + VCS licences | 0 |
| Zoo Z0 | nightly | minutes | ~5 min |
| Zoo Z1 + twin run | nightly, once the stage0 bake is fielded | minutes | ~15 min |
| Zoo Z2 | weekly (attended first) | none | ~25 min |
| Zoo build | per mint | 3-5 h Vivado | 0 |

All jobs run at `nice -n 19` with ≤4 workers (srv03335 load is 20-60).

**Survivors → tickets:**
1. Write `survivors.json`, keyed by (file, function, operator, before→after), not by line number.
2. Diff it against the stored baseline.
3. Open one GitHub issue per function (label `mutant-survivor`), at most 5 new issues per night. Issues work even while Actions is billing-blocked.
4. Close each issue one of three ways: a new test that kills the mutant, "equivalent" (a pragma with a reviewed reason), or a real bug.

The code-mutation score starts report-only, then ratchets per module like `coverage.sh` `COV_FLOOR`/`COV_STRICT`. **The zoo is a hard release gate.**

**Unsure:** mutmut 3 per-PR diff mode; Mull `--test-program`; yosys-slang in OSS CAD Suite; the Certitude licence; MCY netlists vs cocotb internal-signal reads; whether gen_checker tx/rx match exactly; a wrong clearing's effect on silicon; every runtime figure (estimates).

## Sources
- Repo: HM `ab311b4`; platform `origin/master 3f7cea2` (via `git show`); `-lx e7dd3c7`. Paths as cited. `docs/contracts/net-protocol.md:389-390` (pair coherence), `:1030-1070` (stats), `:1568-1590` (slot codes).
- mutmut: https://mutmut.readthedocs.io/en/latest/ ; releases https://pypi.org/project/mutmut/
- cosmic-ray filters: https://cosmic-ray.readthedocs.io/en/latest/how-tos/filters.html
- mutatest status: https://github.com/EvanKepner/mutatest
- Mull: https://mull.readthedocs.io/en/latest/Features.html , https://mull.readthedocs.io/en/latest/MullConfig.html , https://github.com/mull-project/mull/releases
- MCY: https://yosyshq.readthedocs.io/projects/mcy/en/latest/methodology.html , …/mutate.html , …/config.html
- yosys-slang: https://github.com/povik/yosys-slang
- Certitude: https://www.synopsys.com/verification/simulation/certitude.html
