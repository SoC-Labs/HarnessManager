# CI-PLAN lane MATRIX: what the CI tests, on the boards and off them

Lane MATRIX, 2026-09-30. Research only: nothing was run.
Timings are measured on silicon unless marked *est.* (my estimate) or *unsure*.

## Decisions first

**Recommendation:** turn board 2's nightly into a **full-coverage night** with a health gate.

- Board time finds what the simulations miss. Since 24 Sep, ~27 commits on the Linux branch cite a
  silicon or board finding (a `git log --grep` count, approximate).
- Today's HIL-AUTO uses ~5 min of board time per 30 min: 2 swaps and 13 reads.
- Add to the night: the 13-RM sweep with one functional probe per RM, port churn, a swap storm and
  warm-reset drills.
- Long soaks, cold boots and card writes move to a weekly/pre-release tier on board 1.
- If the health check fails, the board is taken offline for the night, as LAVA does
  ([LAVA health checks](https://docs.lavasoftware.org/lava/healthchecks.html)).

| Option | Trade-off |
|---|---|
| **A (rec.) Extend HIL-AUTO's plans on board 2 nightly; soak_linux weekly** | Reuses two proven runners, their allow-lists and their evidence. Cost: two report formats. |
| B. soak_linux `accel` every night | The most coverage already written. But it needs 24 h to decide, needs the hub's py3.11, and never exercises HM's CLI or service. |
| C. Keep today's HIL-AUTO | Zero work. It catches none of the swap, port, identity or card bugs in §4. |

## 1. What exists, and what it proves

| Tool | Proves | Runs |
|---|---|---|
| HM `make check` (~6000, ~32 min) ∥ `tests/web` (~700, ~30 min) | CLI, service and app against `VirtualMps3`/FakeShell/fake hub; smoke-install in a throwaway HOME | by hand (billing-blocked) |
| HM `tests/soak/q2_soak.py`, `q2_inject.py` | the daemon for 40 min on 3 virtual boards: leaks, the board vanishing mid-push, concurrent clients | **never** (not collected) |
| HIL-AUTO (`src/harness_manager/checks/plans.py:18-24`) | the runbooks as data (read/safe/manual); 2 swaps + reads | nightly, board 2: 20/20 on 28-29 Sep |
| Platform `check-ci`/`check` (`Makefile:72`, `:434`) | contracts, RM boundary, rm_id, pyverify, firmware/stage0 host-gcc, harnessd conformance, lint; benches only with `SIM=vcs` | by hand; `ci-full.yml` dormant |
| `harness-regression` (`docs/HARNESS_REGRESSION.md:98-167`) | one gate per past escape; tier 3 is xsdb/JTAG, **stale for Linux** (`pyverify csr-liveness` is its port) | by hand |
| harnessd `qemu_smoke.sh`, ICAP QEMU swaptest; soak/keeper self-tests | rv32 build under a real kernel; the tools' dry runs | **no gate** |
| `-lx scripts/soak_linux/soak_linux.py:12-70` | 24 h: sweep, W1 subset, set_clk, power-on boots, WDOG, churn, leaks | by hand, board 1 |
| `-lx scripts/linux_board/b2_keeper.py:1-50` | re-netboots and re-claims board 2 after any reset | hub service, no verdict |
| sweep/proof scripts, `pyverify boot-rate`, B0-B2/CUTOVER/ROLLBACK runbooks | bare-metal 13/13, DUTEGR, TAP IDCODE, cold-boot rate, rollback ≤ 15 min, parity sign-off (§C4.6) | once each |

## 2. Coverage matrix

**E** = automated; **M** = manual or one-off; **GAP** = none.

| Capability | Sim / virtual | Board read-only | Board write | Stress |
|---|---|---|---|---|
| Swap every overlay + verify | E: swap FSM, FakeShell, rm_id, CRC, image/overlay match | E: D1 | **GAP nightly** (2 of 13) | M: soak (one swap every 3 min); GAP: aborts |
| DUT console | E: `uart_echo_integration`, t4 | GAP | M: HIL_B0 check W2 (`screen`) | GAP: stale backlog |
| Debug up / gdb | E: t4 debug; `jtag_dap_bringup` SKIPs | E: R5 (bare metal) | M: soak TAP IDCODE hourly; gdb by hand | GAP |
| XVC / ILA | E: mock XVC, spikes | E: A3/R10 | E: E1; M: E2-E5 (Vivado) | GAP: hw_server 20 s linger (`-lx docs/planning/linux_lanes/FINDINGS_TRIAGE.md:31`) |
| Identity / locate | E: harnessd + HM tests | E: A1 (stop), A5 | E: A6 | GAP: after re-netboot |
| LCD mirror / panel | E: lm1-5, `test_lcdmirror_e2e` | E: A2 | GAP: mirror frame after `clcd_demo` | GAP |
| Card slots A/B push/commit/rollback | E: slot e2e, stage0 fallback | E: C1/C2 (board 1) | M: B2 + ROLLBACK runbooks | GAP |
| Keep-on-card | E: `test_keep_budget.py` | E: Z2c | M: D2-D5 | GAP |
| MCC temp / reboot | E: fake MCC | E: D4a/R4 | M: soak, boot-rate; **banned on board 2** | M: soak phase B |
| Netboot / stage0 rescue | E: stage0 tests incl. `test_stage0_cold.c` | E: identify 6899 | M: keeper, soak `--netboot` | M: 17 pushes (run 4) |
| DUT Ethernet ingress/egress/inject | E: eth_ss, dut_egress, gen_checker | GAP | M: soak mactest/dutrx; inject RTL only (*unsure*) | GAP: inject round skipped |
| Touch | M | M: T(a) diag; stats health (`d8786d6`) | M: A4 finger (a person) | GAP |
| Lease / claim / SSH | E: lease, claim, TOFU, fpgahub API | E: 0.3, B1, B3 | M: B2 adopt | GAP |
| HM install / update / self-update | E: smoke-install, otad/otar, t7 | n/a | GAP: a harness bundle update on a board | GAP: upgrade from every release |
| kit → build → deploy | E: `test_kit_*` (fake Vivado) | E: `kit check` | M: KIT-NIGHT (29 Sep, PASS) and the agent clean run (30 Sep) | GAP: all 13 RMs |
| Bare-metal vs Linux parity | E: conformance suite, `test_fakeshell_parity` | M: the `bare-metal` plan exists | GAP: a sweep on both images | GAP |

## 3. Tiers

**Per commit (board-free):** ~35 min on the critical path, 0 board minutes.

- HM `make check` ∥ `tests/web`.
- Platform `check-ci` + `lint` (*unsure*: 10-20 min).
- Add `qemu_smoke.sh` (*est.* 5 min).

**Nightly, off-board** (srv03335, `nice`; 0 board minutes):

- `make check SIM=vcs` (*unsure*: 1-2 h);
- `harness-regression` 0-2;
- q2 soak + inject (~50 min);
- the ICAP QEMU swaptest.

**Nightly on board 2** (18:00-08:30 = 870 min; ~5.5 h active; netboot, no card, warm resets only):

| Start | Block | Min |
|---|---|---|
| 18:00 | **N0 health gate.** CI lease (TTL 15 h), `tty_00` free, A1/A5 + static, `csr-liveness`. FAIL = offline tonight | 5 |
| 18:05 | **N1 sweep + probes.** 13 swaps (694 s summed, run 4) plus one probe each: uart_echo 6930 echo; nanosoc console banner + `debug up`/gdb batch; nanosoc_ila `xvc open` + Vivado 2026.1 batch `get_hw_ilas` (*unsure* it runs headless without the E4 person step); eth_ss mactest/dutrx; clcd_demo LCD-mirror frame hash | 30 |
| 18:35 | **N2 port churn.** 500 connects each on 6900/6910/2542/6921/6940; HM service + CLI during a swap; 20 parallel hub one-shots | 20 |
| 18:55 | **N3 swap storm.** ~120 random swaps; every 10th push killed at 10/50/90 % | 150 |
| 21:25 | **N4 warm-reset drill** (keeper `--expect-reset`). `reboot` ×2, WDOG trip ×1. Each must go rescue → push → claim → re-pin → identity MPS3-02/.11.101 → the first swap releases the RP. **Needs david's OK until board 2 has COLDFIX:** its old stage0 arms no WDOG at entry, so a hung warm boot needs PB0 (*unsure* how likely) | 20 |
| 21:45 | **N5 HIL-AUTO** `linux-nocard` every 30 min. Leak samples every 5 min; MCC temp and touch health every 30 min | 85 |
| 06:05 | **N6** sweep again → greybox → identity → report → release | 30 |

**Weekly / pre-release:**

| Test | Where | Time | When |
|---|---|---|---|
| W1 soak `accel` (SSH p99 ≤ 20 s) | board 2 at weekends (no power-on boots until COLDFIX); board 1 before a release | 24 h | weekly |
| W2 soak `tail` | board 1 | 72 h | release |
| W3 cold boots: 10 paced MCC REBOOTs + 1 no-card (+2 PB0, a person) | board 1; board 2 after COLDFIX | ~40 min | weekly |
| W4 card cycle: slot B push → verify → boot-once → rollback to A; keep → clear | board 1 or a spare card | ~2 h | weekly |
| W5 both images: ROLLBACK to 0x72BB0A36 → `bare-metal` plan + sweep → undo | board 1 | ~45 min | release |
| W6 `kit fetch --source hub` + CRC for every fielded static (would have caught the hub overwrite fixed in `987cf26`), then kit build of all 13 RMs (Vivado 2026.1) → `kit check` → deploy each | srv03335 + board 2 | *est.* 8-13 h CPU; 25 min board | weekend |
| W7 clean-account guide run (P8) | board 2, read-only | ~30 min | release |

**Release gates:**

| Product | Gate |
|---|---|
| **Harness** | check-ci + check-linux + `SIM=vcs` + regression 0-2 (`--with-vivado` for a new static); 13/13 on both boards; image/overlay match; 10/10 cold boots per board; W1 with 0 unexplained resets; W2; rollback ≤ 15 min; 3 green nights |
| **Designs** (RMs) | rm_id, pin-check, CRC, clearing-fit; its bench READY and green; `kit check` against the static; ≥ 20 storm swaps in and out with its probe PASS, on each image it ships for |
| **HM** | `make check` + `tests/web` on the tag; smoke-install per claimed OS; upgrade from the last release; OTA self-update + rollback; HIL-AUTO `linux-nocard`, `linux` and `bare-metal` PASS **from the installed wheel**; `harness install` + `harness rollback` once on board 1 (an SD write, so david is present); q2 soak flat; W7 |

## 4. Stress and fault injection, each against a real bug

Hashes: `P:` = platform origin/master, `HM:` = harness-manager.

| Stress | How | The real bug it would have caught |
|---|---|---|
| **Swap storm** | N3; also every RM × RM pair once a week | #7 a dead client wedged the shell; P:`304970e`/`15c05df` (Jul) a failed swap kept the push session and gated the channels; P:`d28c292` (17 Jul) no re-keyed shell could load an RM (HWICAP FIFO), though ping and the gate passed; P:`46f0a72` (Aug) swap away from a DAP RM always rejected; P:`3bfda65` (25 Sep) the same on Linux over TFTP; P:`72b574e` first swap away from greybox failed closed |
| **Back-to-back 6900/6921 clients** | N2 | harnessd accepted before it reaped (P:`9c9e387`, 28 Sep; soak run 4 logged 1023 busy retries); soak run 1 **crashed 17 min in** on a second 6900 client, an idle HM presence hello (`crash1_*`, HM:`c4f3fa9`); HM refused itself (HM:`71a5e0c`); 6921 accepted before it drained (HM:`cd9c859`); HIL-AUTO night 1 stopped HELD by HM's own UI (HM:`8ffb3f3`) |
| **Hub sshd bursts** | N2 | MaxStartups reset `lease show` and the watcher (28-29 Sep, 549 conn/h; HM:`76bec4c`, `1a0d499` on main; SSH-MUX `f384949` **not on main yet**) |
| **Lease contention / force-release** | a second CI principal queues, requests, force-releases on a spare target; fpgahub upgrade smoke | HM force-release would kick soak leases after 2 min (HM:`6ab3132`); fpgahub 0.3.0 broke every lease client: token wrap, socket group (P:`39a0326`, P:`ccc2fde`) |
| **Host-key churn after re-netboot** | N4 | board 2 has a new key and no claim after every reset; board 1's key flip-flopped with /persist (27-28 Sep; P:`bc969ec`, HM:`56b80a5`; no persistence fix) |
| **Card stalls > 30 s** | W4 + a stalling-SD fake | the 30 s default tore both 29 MB pushes at ~27 MB (26 and 29 Sep; P:`6e6a2a9`); keep-on-card inherited it (HM:`a7ab3a0`) |
| **Reset mid card write** | **sacrificial card + david's OK only**: `reboot` / MCC REBOOT at 50 % of a slot-B push; expect EBUSY or fallback to A | 26 Sep 18:50: a reboot during a slot job wedged the card, and the Linux WDOG looped (P:`53f49b4`: reboot now EBUSY during a card job). The old card died after 21:13 the same day and was replaced on 28 Sep |
| **Network loss mid-push** | kill the pusher or the forward at 10/50/90 % | #7; P:`6b6dca1` keepalive reap; HM q2_inject `deploy` (virtual only) |
| **24 h+ soak** | W1/W2 | SSH p99 13.6 s (run 4; fix P:`c1e716f` **not on master yet**); run 2 failed on the MAC inject round (P:`5a6dd65`); run 3: **the soak itself** unbound mmc_spi under a remounted card (P:`6fae6a0`); touch-bus latch at ~2.7 h (P:`d8786d6`); touch starved the loop (P:`d68dd0e`) |
| **Cold/warm boot loop** | W3 (cold), N4 (warm) | stage0 DDR calib in the MCC clock window (P:`cd767ae`); WDOG kick no-op (P:`cd11647`); WDOG before harnessd with /persist on the card (P:`4956d88`); frozen `time` (P:`b9e25db`); TFTP from ephemeral ports (P:`33d0cbd`, `4e7b0e1`); 5 "ok" REBOOTs that never reloaded, sent to tty_01 (Jul-Aug) |
| **Thermal** | MCC temp every 30 min; ALERT > 60 °C or +10 °C/night | none yet (35-37 °C): a trend, not a gate. Mint 4 adds the FPGA die temperature (SYSMON), and MCCIF adds a warm reset that needs no PB0 (`-lx docs/planning/linux_lanes/BACKLOG_2026-09-28.md`; freeze 21 Oct) |

Artefact identity checks, which belong in N0 and per commit:

- Partials were loaded onto another P&R run of the static, and that destroyed the whole config
  (24 Jul; P:`f03fb85`, `b95ea34`).
- A mint carried a stale ELF (P:`54a48b4`).
- The SD held a different shell from the one recorded as fielded, for 6 days (P:`1cbb6c6`).

## 5. Safety rules

HIL-AUTO already enforces these (`docs/HIL_AUTO.md:241-263`):

- the lease;
- an exact argv allow-list;
- stops on an identity surprise, HELD or exit 15;
- `tty_00` busy = skip;
- greybox in a `finally`.

The CI adds:

1. **Never the DUT SST26.** No flash write or erase verb is on the allow-list. The image has no MTD
   driver (P:`f4c0892`); a gate keeps it that way. A weekly **read-only**
   sector CRC is compared with a baseline, because the writer of the 13 zeroed bytes at 0x20000 is
   still unknown. *Unsure:* whether the M0 loader's read path is isolated from its writes. If it is
   not, drop this check.
2. **`tty_00` has one reader.** Check `share list` + a hub `pgrep` before each MCC contact.
   On 24 Sep a stale root `cat` split a REBOOT. A REBOOT is paced: a bare CR, then 100 ms per
   character.
3. **MCC REBOOT limits.** None on board 2 until its COLDFIX bake is fielded. Board 1 gets ≤ 12 per
   24 h, and never a second REBOOT before the first was seen down and back. A reload counts only
   when `boot_count` or USERCODE proves it: in Jul-Aug, 5 REBOOTs said "ok" and reloaded nothing.
4. **SD writes.** The config SD: ≤ 1 a week, `--expect-sha256`, never retried mid-write. The user
   card: slot B only; flip only after verify.
5. **End state.** greybox with `rm_id 0x00000000` read back; the card default unchanged; the claim
   as it started; the keeper unpaused; the lease released.
6. **The window.** Book 18:00-08:30 as an fpgahub reservation, so nobody overlaps it (`fpgahub board reservation`, `cli.py:1741`: "CI-style scheduling"). The daemon
   activates it at the start and force-releases it at the end (`fpgahub/src/fpgahub/daemon.py:340-365`).
   Hub-side runners also take a PID-bound lease, so a crashed runner frees the board at once
   (`daemon.py:206`). PID binding is hub-local only.

**An unattended write is safe when:**

- its undo is in the `finally`;
- a precondition gate runs first (identity, lease, no card job, keeper state);
- rate and budget caps hold;
- the blast radius is one slot or one partition;
- the ladder is automated: keeper → rescue push → greybox. Anything that needs PB0 stops the night
  and pages a person.

Also:

- `ANNOUNCE.txt` is written first.
- Evidence is never overwritten.
- A secret scan runs before commit: `sweep_20260924.txt:13` has a lease token.
- The test tools get their own tests, in a gate. The run 3 FAIL was the soak's own fault. `scripts/soak_linux/test_soak_linux.py` (42 tests) and `scripts/linux_board/test_b2_keeper.py` (15) are on master, but no `Makefile` target runs them.

## 6. First five to automate after 6 Oct (by past bugs caught per board-hour)

| # | Test | Board time | Past bugs it catches | Effort (*est.*) |
|---|---|---|---|---|
| 1 | **Off-board nightly**: q2 soak/inject, `SIM=vcs`, QEMU smoke, regression 0-2, the orphaned soak/keeper tests | **0** | 11+: q2's one run found 8 HM bugs (`Q2_ROBUSTNESS.md:22-34`); CSR width (P:`c89abce`); stale partial (P:`4d1c017`); image/overlay match (P:`f03fb85`) | ½ day |
| 2 | **Port churn + concurrent clients** (N2) | 20 min | 7: `9c9e387`, soak run 1 crash, `71a5e0c`, `cd9c859`, `37767db`, HIL night 1 HELD, MaxStartups | ½ day |
| 3 | **Warm-reset/netboot drill** (N4) | 20 min | 6: identity (`18622e5`), host-key churn, TFTP, WDOG kick no-op, frozen `time`, RP parked after a WDOG | 1 day, plus a CI re-pin rule (HM accepts no key automatically, `src/harness_manager_mps3/claim.py:1078`) and david's OK until COLDFIX |
| 4 | **13-RM sweep + per-RM probe** (N1/N6) | 60 min | 6: HWICAP FIFO, static mismatch, greybox clearing, DAP teardown, rm_id drift, RP parked after a restart | 1-2 days |
| 5 | **Swap storm with aborts** (N3) | 150 min | 6: #7, `304970e`, `15c05df`, `6b6dca1`, `46f0a72`, `3bfda65` | ½ day |

**Owners:**

- **HM lead:** the HIL plan sections for N0-N2, N4 and N5, and the CI re-pin rule.
- **Linux lead:** soak_linux as a library, the keeper `--expect-reset` hook, and W1-W5.
- **david:** the principal, the leases, SD writes, PB0 cover and the spare card.

**Cost:**

- Board 2: 14.5 h leased every night.
- Board 1: ~3 h a week (W3 + W4), plus ~4 days before each release (W1 + W2 + W5).
- srv03335: ~3 h of CPU a night, plus 8-13 h on a W6 weekend.

**First three steps:**

1. Run the off-board nightly on srv03335 from a systemd timer. This needs no board and no
   principal.
2. Give the CI its own fpgahub principal and a standing 18:00-08:30 reservation on board 2. It must
   not be `david@mapstone-dev`.
3. Add N2 and N1 to HIL-AUTO.
   - N1 is a plan section: 13 × `program` + a probe, tier `safe`, inside the existing allow-list.
   - N2 is a new check kind: raw connects, allow-listed by port.
