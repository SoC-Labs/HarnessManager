# PERF lane: performance benchmarking of the harness and Harness Manager

Research only. The numbers come from committed evidence. Two were measured locally tonight without a board: HM import time and cachegrind instruction counts.

## Decisions

**Recommendation:** use one in-house JSON-lines schema, `perf/v1`, fed by three collectors:
- a nightly **board perf round**;
- an **HM bench** on srv03335;
- a **free ingest of HIL-AUTO's `seconds`**.

A rolling-median comparator turns them into a static trend page and a nightly delta table.

**How to time:**
- **Board metrics use wall-clock.** In soak run 4, each RM swapped 25 times over 17 h. The per-RM coefficient of variation was 1.1–2.0 %. 916 SSH logins all fell between 12.7 and 13.7 s.
- **srv03335 metrics use instruction counts or CPU time.** At load 34–38, the HM import wall time spread ±10 % (0.14–0.17 s), while cachegrind counted 399.8–400.2 M instructions (±0.05 %). The same Vivado build took 28 min at load 33 and 56 min at load 50–59.

| Alternative | Trade-off |
|---|---|
| asv | Good step detection. It is built for per-commit benchmarks of a Python repo, but board metrics key on (board, image). |
| Bencher (self-hosted) | Best statistics (t-test and change-point detection). It needs a server, a container and an open port. Revisit if the in-house comparator proves too weak. |
| github-action-benchmark | Easy alerts. GitHub Actions is billing-blocked, and hosted runners can't reach a board. |

**Why now:** the Linux cutover shipped two large slowdowns and nothing flagged them.
- **Swaps got about 10× slower.** Bare metal swapped in 3–7 s; Linux takes 36–84 s. The Linux 6910 push runs at about 40 KB/s on a 10 Mb/s link that carried TFTP at 0.63 MB/s, so there is about 15× headroom.
- **jump→6900 went from 18 s to 58 s.**

A third change is still unexplained. On the HIL-AUTO night of 28–29 Sep, `program nanosoc_ila` stepped from 68.8 s to 80.3 s (+17 %) after round 1. `restore` stepped from 40.3 s to 46.8 s. Both stayed there for 19 rounds, and every round said PASS.

## 1. Harness metrics (Linux, RC2 0x44EE76D5)

| Metric | Today | Silent regression? / matters? |
|---|---|---|
| Swap time per RM (end to end) | p50: led 36.6 s, greybox 40.1, nanosoc 64.8, ila 74.9, multicore 83.9 s. All-RM p99 is 84.6 s (board 1). | yes / **high** |
| Push throughput | ≈40 KB/s (2.58 MB partial + 193 KB clearing in 68.8 s). Bare metal, windowed: ≈0.5 MB/s. | yes / high |
| 6900 poll round trip, from the hub | p50 52 ms, p99 147 ms (n=952) | yes / medium |
| harnessd loop worst case (`svc_max_us`) | p50 147 ms, **p99 3.07 s** | yes / **high**: the WDOG stage is 21.5 s |
| Busy resets on single-client ports | 1023 in 17.4 h | yes / medium: this is HM's reconnect race; re-measure after 9c9e387 |
| SSH login | p50 13.2 s, p99 13.6 s. HM `board ssh` takes 22.5 s. | yes / high: the gate is p99 ≤ 20 s |
| Boot to 6900 / SSH | Card cold boot: 186.5 / ≤ 204.5 s. Netboot power-on: 140 / 158 s. Warm: 156–181 s. | yes / **high**: HM's 180 s wait was already outgrown (a90e374) |
| FIRST KICK vs the 42.9 s WDOG | `os_up_ms` 35.2 s, a margin of only 7.7 s | yes / **critical**: a larger initramfs would boot-loop the board |
| Card slot push / read-back | ~70 / ~14 KB/s: 37–50 min per 29 MB slot. SLOT-SPEED models 9.3 min. | yes / high: HM timeouts are derived from these rates |
| LCD repaint | 527 ns/byte in the harnessd log. 61 / 121 ms is modelled only. | yes / medium |
| Console | The DUT has no RX FIFO; 20 ms/char is the safe pace. | measure bytes lost at that pace |
| XVC ILA upload; debug-up → first GDB reply | **Not measured.** JTAG runs at ≈0.8 KB/s; the tunnel adds +0.2 ms per round trip. | unknown: take a baseline |
| MCC read (`mcc temp`) | 7.2–7.4 s (n=20) | low: the hub's 3.5 s silence sets the floor |
| harnessd memory | RSS 1464 → 1960 KB over 17 h; least-squares growth +4.9 KiB | yes / high over days |

## 2. Harness Manager metrics

| Metric | Today | How to measure |
|---|---|---|
| CLI import | 122 ms (`-X importtime`), 400 M instructions | Gate on cachegrind instruction count; use importtime to find the culprit |
| CLI verbs | `version` 0.25–0.39 s; `info` 1.65–1.97 s; `overlays` 3.1–3.5 s | hyperfine `--warmup 3 --runs 20 --export-json` for board-free verbs; HIL-AUTO for board verbs |
| Service start; API latency | not measured | Start the daemon and poll `/health`. Hit `/boards`, `/boards/{bid}`, `/telemetry`, `/overlays` and `/jobs` on the Q2 VirtualMps3 rig with httpx; record p50/p95 |
| Web UI | Frame decode 0.6–0.7 ms, against an 83 ms frame. First paint not measured. | Playwright: paint entries, LCP, and time until Program is clickable |
| HM overhead vs raw pyverify | not separable (boards differ) | Paired on one board: `pyverify deploy` then `harness-manager program`, alternating, ×3 |
| Hub calls | 83 calls per 10 min, now 1 connection per 10 min with SSH-MUX (f384949) | Count ssh execs against the fake hub |
| Service memory | 52 → 65 MB RSS in 40 min; +2.5–2.9 MB/h | The Q2 soak's /proc sampler, run for 8 h |
| Kit | `kit guide` 23–57 s at load ~50 (probe 17.4 s) | CPU time per verb. For Vivado, record CPU-seconds, not wall time |
| Test suites | `make check` ~32 min; web ~30 min | Per-test times from junit XML; flag any test that grows more than 2× |

## 3. Method

**Record format:** one `perf/v1` record per value.

```
{ts, run_id, source, metric, subject, value, unit, stat, better,
 board, static, image_sha, hm_commit, platform_commit, host, load1, n}
```

- Records hold allowlisted numeric fields only, never log text. Lease tokens already sit in evidence files (`2026-09-linux-b1-v4/summary.md:4`, w3 `sweep_20260924.txt:13`).
- Storage is gzip JSONL on a `perf-data` branch, about 30 KB a night.

**Collectors:**
1. **Board round.** pyverify runs on the hub, as the soak does. It covers:
   - a 13-RM sweep;
   - 10 SSH logins;
   - 200 6900 polls;
   - uart_echo at 20 ms/char;
   - ILA upload ×3;
   - debug-up → halt;
   - `mcc temp` ×5;
   - a clcd_demo repaint;
   - one warm reboot (FIRST KICK margin);
   - a /proc sample;
   - hub RTT as a control.
2. **HM bench:**
   - cachegrind (CodSpeed's approach);
   - hyperfine;
   - httpx on VirtualMps3;
   - Playwright;
   - the Q2 soak.
3. **HIL-AUTO ingest (about 100 lines).**
   - `summary.json` already has `board`, `static`, `plan` and `hm_commit` (`checks/run.py:967`), plus `checks[].seconds` (`:775`).
   - The evidence files give the push bytes.
   - That is n≈20 per check per night.
4. **Soak summaries.** `soak_linux.py` already computes SSH p99, the swap-p99 drift and the leak slopes (`:370-374,1936-1951`).

**Thresholds:**
- Each series is keyed by (metric, subject, board, image family).
- Baseline = the median of the last 10 accepted nights; σ̂ = 1.4826 × MAD.
- A regression flags when the value exceeds baseline + max(4σ̂, r·baseline), with r set by the source:
  - 10 % on board metrics (≈5σ);
  - 5 % on instruction counts;
  - 25 % on srv03335 wall time.
- A soft regression needs **2 nights in a row**. Breaking a hard budget fails the same night.
- A new image starts a new segment. The release report compares across images with Mann-Whitney for times and Fisher for rates, as `bootrate.py:1784-1850` already does.

**Noise:**
- **srv03335.** Nights are not quiet: load was 34–38 at 00:26 and 48–59 at 21:00. Measure counts or CPU time. Where wall time is unavoidable:
  - pin with `taskset`;
  - take the median of ≥20 runs;
  - log `load1`, and mark the run "not judged" when load1 > 48.
- **Board.**
  - The CI's own lease makes the board exclusive.
  - Use one ssh multiplex, because MaxStartups throttles bursts.
  - Keep board 1 and board 2 as separate series.
  - Drop a round if the hub RTT is more than 2× its baseline.

**Reporting:**
- **Nightly delta table:** only the series that moved.
- **Timeout headroom:** each timeout ÷ its measured p99. The timeouts are the 300 s reboot wait, 900 s card stall, 3729 s slot job, 42.9 s WDOG and 60 s kit launch. Alarm below 1.5×; two of these timeouts have already broken.
- **Trend page:** static HTML with one sparkline per series, built from the JSONL. No server.

## 4. Proposed release budgets

| Budget | Today | Gate |
|---|---|---|
| Swap p95 per RM | 37–86 s | ≤ 1.15 × this release's p50, and ≤ 100 s. Ratchet down once the push is fixed |
| FIRST KICK `os_up_ms` | 35.2 s | ≤ 37 s |
| Cold boot to SSH | ≤ 204.5 s | ≤ 240 s |
| SSH p99 | 13.6 s | ≤ 20 s; alarm above 15 s |
| 6900 poll p99 / `svc_max` p99 | 147 ms / 3.07 s | ≤ 250 ms / ≤ 5 s |
| harnessd growth over 12 h | +4.9 KiB | the soak's limits (1 MiB RSS, 2 fds, 8 MiB SUnreclaim) |
| Slot push + verify | 37–50 min | ≤ 55 min; ≤ 15 min after SLOT-SPEED |
| HM import / `version` | 400 M instructions / 0.3 s | ≤ 440 M instructions / ≤ 0.5 s |
| HM overhead over pyverify | unknown | ≤ max(5 s, 10 %) |
| HM hub connections, idle | 1 per 10 min | ≤ 2 per 10 min |
| HM service RSS, 8 h | +2.5–2.9 MB/h | ≤ 3 MB/h, flat after 4 h |
| `kit guide` | 23–57 s | ≤ 60 s |

## 5. First 3 steps after 6 Oct, and cost

1. **HIL-AUTO ingest, comparator and delta table** (~1 day, HM). Backfilling 28–29 Sep flags the +17 % step. Costs 0 board-minutes.
2. **HM bench** (~2 days, HM). About 15 CPU-minutes a night, plus one core at under 10 % for the 8 h soak.
3. **Board round** as `soak_linux.py --profile perf` (~2 days, Linux lead).
   - **Board 2, nightly after HIL-AUTO:** about 30–35 board-minutes (the sweep is 11.3 min, the reboot 3 min).
   - **Board 1, weekly:** add a cold boot and a slot push, about +50 min.

**Unsure:**
- the cause of the +17 % step;
- whether the round must pause HIL-AUTO;
- the XVC, GDB and first-paint baselines (5 nights will set them);
- whether Bencher could be hosted later.

## Sources

**Repos:**
- lx `docs/evidence/2026-09-linux-b2/soak_netboot/run4_20260927_151014/*`
- lx `scripts/soak_linux/soak_linux.py:370-374,1936-1951,3008`
- lx `docs/planning/linux_lanes/HARNESSD_CONTRACT.md:166-177,256-259`
- lx `docs/planning/linux_lanes/LCD_MIRROR_FPGA.md:353`
- lx `docs/planning/linux_lanes/GDB_SERVER_PROPOSAL.md:10`
- lx `docs/planning/linux_lanes/SLOT_VERB_DRAFT.md:86`
- lx `host/pyverify/pyverify/bootrate.py:1784-1850`
- main `docs/evidence/2026-09-linux-b1-v4/summary.md`
- HM `docs/evidence/2026-09-hil-auto/0928-b2-run2/iter-*/*.json`
- HM `docs/evidence/2026-09-28-hil/README.md:30-43`
- HM `src/harness_manager/checks/run.py:775,967`
- HM `docs/assessment/2026-09-24/Q2_ROBUSTNESS.md:15,88`
- HM `docs/design/XVC_DEBUG.md:390-396`
- HM `docs/design/LCD_MIRROR.md:410`
- HM integ/0930 `docs/evidence/2026-09-29-kit-night/README.md:84,168,176`
- HM `docs/HUB_MODE.md:354-358`

**Web:**
- [Bencher thresholds](https://bencher.dev/docs/explanation/thresholds/)
- [asv step detection](https://asv.readthedocs.io/en/latest/step_detection.html)
- [pytest-benchmark comparing](https://pytest-benchmark.readthedocs.io/en/latest/comparing.html)
- [hyperfine](https://github.com/sharkdp/hyperfine)
- [CodSpeed CPU simulation](https://codspeed.io/docs/instruments/cpu/overview)
- [pyperf system tuning](https://pyperf.readthedocs.io/en/latest/system.html)
- [github-action-benchmark](https://github.com/benchmark-action/github-action-benchmark)
- [Playwright performance measurement (Checkly)](https://www.checklyhq.com/docs/learn/playwright/performance/)
