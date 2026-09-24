# Q1 Tests: the unit, integration and browser suite (2026-09-24)

Lane Q1 of the quality assessment. Branch `team/q1-tests` from `64eb716`. Host: srv03335 (16 cores, load average 10 to 30 throughout: other lanes were building and testing at the same time). Every run was under `nice -n 10`.

## The five findings that matter most

1. **Two real flakes, each 2 runs in 30 under load, both fixed.**
   - The debug gdb-bind test (`test_t4_debug_virtual`): its port block sat in the ephemeral range. 60/60 after the fix.
   - The queued-lease browser test (`test_l3_week_plan`): the cause was a **product bug** in `web/static/js/store.js`. A `GET /boards` answer that was older than a job ended the live job, for good. The board then showed as free while its lease was still queued. 30/30 after the fix, and a new deterministic test fails 3/3 without the fix.
2. **Isolation holds for state and PTYs, but only the state directory was isolated by construction.**
   - Four full runs and 13 flake loops ran with canary `HARNESS_MANAGER_STATE_DIR` and `HARNESS_MANAGER_PTY_DIR`. They left 0 entries in either canary, left `/tmp/harness-manager-dam1n19` unchanged, and left 0 processes behind.
   - The root fixture set only the state directory, so a test that forgot `l2_rig.pty_dir()` would have written beside david's live PTY links. Fixed: every test now gets a private PTY directory.
   - The suite does write to `$HOME` in other ways: Chrome profile side files, and the pip cache. See §5.
3. **Nine checks could never fail. Fixed.** One example: `terminal-command`, a `data-testid` that no longer exists anywhere, was asserted absent in two browser tests. Six weaker ones remain (§3.3).
4. **Coverage is good overall, with thin spots in risky code.** Overall 90.2 % of lines and 81.2 % of branches.
   - `services/pty.py` is at 76.6 % lines and 67.3 % branches: the `/proc` fallback scanner has never run.
   - `daemon/control.py` `stop()`: the SIGTERM escalation has never run, and it signals a pid read from `daemon.json` with no identity check. **Product risk, for Q2.**
   - `update/executor.py` rollback and journal branches: 74 % of branches covered.
   - The daemon's HELD gate between two short requests had no test. Now it has three.
5. **CI blind spots.**
   - Windows and macOS run `tests/unit` only: 961 and 971 of about 1880 tests.
   - The 11 week-plan browser tests never ran over the real daemon, because that was opt-in. Now it is the default, and all 11 pass.
   - Real OpenOCD and xsdb (8 tests) and the platform-repo tests (7) skip in CI.
   - Locally, `make venv` installs pyverify *editable from whatever branch `../mps3-nanosoc-platform` has checked out*. CI uses the vendored wheel.

## 1. Coverage

This is coverage 7.16 over run 1 (`coverage run -m pytest`, branch mode, `patch = subprocess`, so daemons started as processes are counted).

| Package | Statements | Lines | Branches | Branches covered |
|---|---:|---:|---:|---:|
| harness_manager (top level: engine, demo) | 791 | 92.3 % | 148 | 80.4 % |
| harness_manager/cli | 1976 | 92.9 % | 508 | 83.9 % |
| harness_manager/client | 799 | **78.0 %** | 176 | **61.9 %** |
| harness_manager/core | 555 | 93.3 % | 62 | 77.4 % |
| harness_manager/daemon | 2024 | 89.2 % | 414 | 76.1 % |
| harness_manager/power | 807 | 92.1 % | 228 | 85.1 % |
| harness_manager/services | 2712 | 87.9 % | 726 | 80.9 % |
| harness_manager/services/update | 2436 | 91.7 % | 706 | 79.2 % |
| harness_manager/transports | 250 | 85.6 % | 64 | 78.1 % |
| harness_manager/web | 113 | 89.4 % | 34 | 76.5 % |
| harness_manager_mps3 | 4558 | 91.2 % | 1312 | 85.4 % |
| **Total** | 17021 | **90.2 %** | 4378 | **81.2 %** |

The TEAM_PLAN target is 85 % for `core` and `services`. Both meet it for lines, but neither does for branches.

### The risky areas you named

| Area | Module | Lines | Branches | What is not run (by function) |
|---|---|---:|---:|---|
| Deploy | `services/deploy.py` | 98.0 % | 100 % | nothing of note |
| Deploy | `mps3/deploy.py` | 91.0 % | 88.2 % | `deploy` 7, `_check_shell_id` 3, `_check_usercode` 3, `_swap_error` 3: identity-guard refusals |
| Update | `update/executor.py` | 90.6 % | **74.1 %** | `rollback` 6, `run` 5, `_store_overlays` 5, `_check_journal` 3, `_write_os` 3, `confirm_identity` 3 |
| Update | `update/service.py` | 89.8 % | 76.7 % | |
| SD | `mps3/sd.py` | 86.6 % | 83.9 % | about 45 of 84 missed lines are Windows and macOS volume listing and Windows `pid_alive`. No CI job measures them. `backup`/`restore`/`verify_backup` 4 + 3 + 4 |
| MCC | `mps3/mcc.py` | 91.6 % | 87.6 % | `voltages` 8, `classify` 7, `_witness` 5, `_drain_to_prompt` 4 |
| Console broker | `services/console.py` | 93.1 % | 90.8 % | `_drop_queued` 8, `_write_loop` 5 |
| PTYs | `services/pty.py` | **76.6 %** | **67.3 %** | `scan_clients` 30 + `_scan` 21: the no-inotify `/proc` fallback has never run. `sweep_stale` 8 |
| Tunnel | `mps3/tunnel.py` | 89.6 % | 85.3 % | `_stop_proc` 5, `close` 4. The fake `ssh.wait()` never times out, so terminate, then wait, then kill never escalates (§3.3) |
| Hub | `mps3/hub.py` | 92.3 % | 79.1 % | parse and route edge cases |
| Jobs and HELD gates | `daemon/jobs.py` | 92.7 % | **71.4 %** | `op()` busy-with-another-request (**now tested**), pool-shutdown paths |
| Daemon control | `daemon/control.py` | **76.1 %** | 67.3 % | `stop()` 19 lines: the SIGTERM escalation. **Risk:** it SIGTERMs `info.pid` from `daemon.json` when the daemon does not answer. If the daemon crashed and the pid was reused, that is an unrelated process. `status` 8 |
| Session lock | `core/session.py` | 75.9 % | 58.3 % | 17 of 26 lines are the Windows `_pid_alive`. Also a torn lock that vanishes, and running out of retries |
| Client | `client/remote.py` | 75.5 % | 52.9 % | `run_job` 17, `ws_connect` 12, `write` 9, `_reader` 8 |

## 2. Flakiness

### Full-suite runs

These are baseline runs 2 to 4, on a snapshot of `64eb716` (`git archive`), so my edits could not change what they measured.

| Run | Order | Extra | Result | Wall |
|---|---|---|---|---|
| r1 | file order | coverage (+ subprocesses) | 1871 passed, 9 skipped, 1 deselected | 809 s |
| r2 | file order | `HOME` = a canary directory | 1871 passed, 9 skipped | 613 s |
| r3 | file order | | 1871 passed, 9 skipped | 623 s |
| r4 | **random** (pytest-randomly; it started at `test_t4_debug_units`) | | 1871 passed, 9 skipped | 595 s |
| `make check` on this branch (dbf79fe) | file order | lint + tests, canaries | **1890 passed, 9 skipped, 1 deselected; CHECK PASS**; canaries empty, 0 leftover processes | 572 s |

No full run failed. **No order dependence** was found in one random order. The seed was not logged, because `-q` hides the header.

### Loops (pytest-repeat, one process, under load)

| Test | Before | After |
|---|---|---|
| `test_l2_pty::test_reattach_after_an_exclusive_client_exits` (known) | 30/30 | (64eb716 already fixed it) |
| `test_t4_console_virtual::test_swap_closes…` (known) | 30/30 | |
| `test_t4_console_virtual` failed-swap twin and replay-after-swap | 60/60 | |
| `test_t4_debug_virtual::test_openocd_that_cannot_bind_gdb…` (known) | **28/30** | **60/60** |
| `test_l3_week_plan::test_a_queued_lease…` (known) | **28/30** | **30/30** |
| `test_l1_tcp_serial` paced characters; far-end close | 30/30; 30/30 | |
| `test_l1_lease` heartbeat | 30/30 | |
| `test_l2_consoles_virtual -k screen` (GNU screen, 2 tests) | 30/30 | |
| `test_t4_console_virtual -k paced` | 60/60 | |
| `test_t13_live` (whole file, 15 tests) | 150/150 | |

### Every test that failed, with its cause

1. **`test_openocd_that_cannot_bind_gdb_is_killed_not_left_holding_the_board`**, 2/30.
   - The failure: `local tcl port 36020 is already in use` instead of `gdb port 36017`.
   - The cause: the test held gdb with `bind(0)`, so `base+2` and `base+3` were in the ephemeral range (32768–65535 here). This host's outgoing connections took one of them between the check and the stub's bind.
   - The fix: `held_gdb_block()` picks a free block in 20000–23000. That is below every OS's ephemeral range and below the service's own 23300 range.
2. **`test_a_queued_lease_holds_the_board_and_can_be_cancelled`**, 2/30. **Product bug.** The failure: the reset reason reads "not armed" while the lease chip reads "queued". The page-state dump showed:
   - `session.opened` at .753, which schedules `loadBoards` for 300 ms later;
   - `job.started` (lease) at 1.112;
   - `jobs[board] = null`.

   The `GET /boards` was asked before the job started and answered after it. `loadBoards` took "no job" in an open row to mean a missed `job.done`. It called `jobEnded()`, which also adds the id to `endedJobs`, so `setJob` never brings it back. Fixed in `store.js`: only a job known before the list was asked for is ended that way.
3. **The new twin test, during development**, 3/20. **Product race.** `start()` ran `select(saved or first)` after its own `loadBoards()`. The event socket's own `loadBoards()` could draw the rail first, so a board the user had already clicked was switched back to the saved one (seen after `page.reload()`). Fixed: `start()` selects only when nothing is selected yet. 40/40 after.

## 3. Test quality

### 3.1 Vacuous checks: fixed (commit 757e5bc)

| Where | Why it could not fail |
|---|---|
| `test_t1_engine_virtual::test_telemetry_on_the_ethernet_only_board…` | The exact-readings assert sat under `if session.telemetry is None`, which never holds: the pack always wires an adapter. Now it pins the four readings and their reasons, and the no-adapter case is its own twin. |
| `test_t7_bundle::test_archive_escapes_are_refused` | `assert not out.exists()`: `safe_extract` renames its temp directory to `out` only on success. Now the check is that nothing at all was left next to the archive. |
| `test_t14_browser::test_console_output_appears…`, `test_l3_week_plan` (screen attach) | `[data-testid="terminal-command"]` count 0: that testid has been gone from the product since 56ea14a. |
| `test_t14_browser::test_a_slow_engine_call…` | `engine.called("debug.detect") == []` was true by construction (the demo records a call only after `hold()` releases it). `hold()` now records the call reaching the service. |
| `test_l1_reach_virtual::test_boards_toml_hub_table_errors_name_the_key` | `pytest.raises(Exception)` also matched `KeyError('host')`. Now it expects `UsageError`. |
| `test_l1_cli::test_lease_acquire_show_release` | `"token" not in out` checked the key, not the secret. Now it checks the value. |
| `test_t5_help`, `test_l4_cli_power`, `test_t9_sysmon` (2) | `all(...)`/`not any(...)` over a list that could be empty |
| `test_t13_process::test_ui_…` token redaction | `token not in daemon.log` after a fixed 0.2 s passed even if no line had been written yet. It now waits for the masked `token=***` line first. |

### 3.2 Checks without a negative twin

Added by this lane:
- the HELD gate between short requests (`BoardGates.op`), plus its twin;
- the PTY default directory, plus its twin;
- the stale-list test, whose twin is that a newer list still ends a missed job;
- the no-telemetry-adapter twin.

Still without a twin (a name heuristic, confirmed by reading):
- **`daemon/control.stop()`** has no test of "the daemon does not answer, so SIGTERM", and none of "the pid now belongs to someone else, so do not signal it". The second needs a product change first.
- `services/pty.py` `/proc` fallback (`scan_clients`/`_scan`): with inotify unavailable, a client count has neither a positive nor a negative test.
- `update/executor.py` `rollback` failure branches and `_check_journal`. The SD write is the most destructive path, and its branches are covered at 74 %.
- `core/session.py`: the torn lock that vanishes, and "lock could not be taken" after two tries.
- `scenario_s8_capabilities.py` (3 tests), `test_l5_release.py` (8), and the review-screenshot tests: positive only. Low risk.

### 3.3 Weaker checks still there (medium confidence; not fixed)

- `test_l1_hub_api.py:87` checks `"token" not in str(result)`, the key and not the value (as in `test_l1_cli`). **Not edited**, because lanes LR-A to LR-D own the hub tests now.
- `test_l1_lease.py:56`: `"token" not in out["lease"]` is redundant after an exact dict equality. It is harmless.
- `test_t14_browser.py:407-411` and `test_l3_week_plan.py:601`, `:74`: a count of 0 checked right after `open_board`. The banner and hub come from separate async reads, so these can pass before they arrive.
- `test_t14_browser` "every section fits": `wait_for_timeout(150)` and then "no horizontal overflow". Under load, that can check a section before it has drawn.
- `test_t13_live.py:140-160`: it compares the daemon with in-process for each verb but never asserts rc 0, so the same failure on both sides passes.
- `tests/fakes/l1_fake_ssh.py:118` `wait()` returns 0 on timeout instead of raising `TimeoutExpired`, so the tunnel's terminate, then wait, then kill escalation is never exercised.
- `test_l2_pty.py:480`, `:522`: `pytest.raises(AssertionError)` around a read. A dead client process also passes.

### 3.4 Fixed-time sleeps

There are 59 `sleep`/`wait_for_timeout` sites in `tests/`. Most are 10–50 ms pauses inside poll loops that have a deadline, which is fine.

| Where | Verdict |
|---|---|
| `test_t12_profiles.py` Linux reboot: slept 0.3 s, then one `health()` inside a 0.1–0.6 s outage | **Fixed:** it polls into the window |
| `test_t13_process.py` 0.2 s then the log | **Fixed:** it waits for the masked line |
| `test_l1_lease.py:150` (0.2 s), `test_l1_tunnel.py:199` (0.3 s) | These check that nothing happens, which needs a window. Acceptable |
| `test_l1_tcp_serial.py:114` (50 ms for the FIN) | A race in principle; 30/30 under load |
| `test_l2_consoles_virtual.py:316` (0.2 s between screen rounds) | After a condition wait; acceptable |
| `test_t14_browser.py:542-543` (150 to 200 ms per section, about 3.6 s in all) | A negative overflow check after a fixed settle (§3.3). Wait for a per-section drawn marker instead |
| `test_l3_review_screenshots.py` `settle(350)` ×12, `test_t14_harness_states.py` 120 ms ×2 per shot | Cosmetic (screenshots); about 4.5 s in all |
| Fakes (`l2_rig` 200 ms screen TCSAFLUSH model, `t12_harness_shell` reboot timings, `l3_week_plan` sim delays) | They model real timing on purpose |

### 3.5 Tests that depend on this host

| Depends on | Tests | Here | CI (Linux) |
|---|---|---|---|
| `/tmp`, not `$TMPDIR` | `l2_rig.pty_dir()`: on srv03335 an outside process opens new ttys under `/tmpdir`, which upsets client counts | used | used |
| The platform repo next door | `test_l2_uart` (5; 1 skips here, because the checkout's branch lacks `rp_nanosoc_ila_wrapper.sv`), `test_t9_vivado` real report (**runs here**), `test_t2_deploy_virtual` real overlays (**runs here**, reads the real `.bin`s) | mixed | all skip |
| pyverify source | `make venv` installs it editable from `../mps3-nanosoc-platform` (branch `feat/onboard-monolithic-to-fpga-toolkit` today) | platform checkout | vendored wheel |
| System Chrome | `tests/web` (155 tests) | `/usr/bin/google-chrome` | runner Chrome; the skip guard fails the job |
| Real tools | tclsh (3), GNU screen (2), shellcheck (1), ssh `-G` (2), xsdb (1: **runs here**, `/apps/Xilinx`), OpenOCD (7), uv (1) | OpenOCD and uv skip | OpenOCD and xsdb skip |
| The PyPI index (network) | `test_l5_packaging` (6), `test_l5_install` (2) | run (about 50 s) | run |
| `/proc`, Linux | PTY client counts, `/proc/net/tcp`, pid identity | run | run |

### 3.6 Skips, and whether CI runs them

- **Here:** 9 skipped:
  - real OpenOCD ×4 and ×3;
  - uv ×1;
  - `test_l2_uart:62` ×1.
- **CI Linux** (run 35988607483): 15 skipped:
  - `test_l2_uart` ×5;
  - `test_t9_vivado` ×1;
  - `test_t4_real_openocd` ×4;
  - `test_t9_real_tools` ×4 (OpenOCD ×3, xsdb ×1);
  - `test_t2_deploy_virtual` real overlays ×1.
  - **The real-OpenOCD and xsdb tests run nowhere automatically.** They are the only proof that the debug service's argv and IR/DR sequences work with the real tools.
- **CI Windows:** 961 passed, 22 skipped. **CI macOS:** 971 passed, 12 skipped. Both are **unit only**. 862 integration and web tests (707 + 155) have never run on Windows or macOS, including the whole daemon, deploy and console stack. The PTY tests skip off Linux, as intended.
- **Hidden by opt-in, not by a skip:** the week-plan browser tests ran over the mock only unless `HARNESS_MANAGER_WEB_WEEK_REAL=1`, and CI never set it. **Fixed:** 11 more tests, about 25 s.

## 4. Speed

Wall time was 595–623 s without coverage. The test time is concentrated:
- **80 tests ≥ 2 s take 47 % of the time.** 347 tests ≥ 0.5 s take 84 %.
- The median test takes 0.022 s.
- By tier: integration 324 s, web 251 s, unit 41 s.

Slowest tests (run 3, then run 4):

| Time | Test | Verdict |
|---|---|---|
| 27–29 s | `test_l5_install[pip]` | Network and a real venv. **Candidate:** keep it in CI and leave it out of local `make check` (`-m "not packaging"`). Decision for the lead |
| 15–22 s (setup) | `test_l5_packaging` (sdist, then wheel, then venv) | as above |
| **14.3 s → 6 s** | `test_t14_harness_states::test_a_board_reboot_shows_the_controllers_evidence` | **Fixed.** The reboot POST waited 6.5 s for MCC reads paced at 60 ms (see §6). The test now paces the fake MCC at 5 ms with a faster witness |
| 5.8–8.4 s ×2 | `test_every_section_fits_1280x800…` (light, dark) | 3.6 s of it is fixed settles; replace them with drawn markers |
| 4.1 s ×2 | `test_program_ok_path_shows_progress_to_done` (daemon, mock) | DemoEngine speed 0.25; it could run at 0.1 without losing the "running" screenshot |
| 4.3 s | `test_l2_cli::test_pty_in_process_holds_the_pty_until_it_ends` | `--for` seconds; could be shorter |
| 3–5 s ×12 | `test_l3_review_screenshots` | Screenshots for review. Could move behind a `review` marker that CI runs and local `make check` does not |
| 3–4 s | `test_t14_harness_states` (9) | Each starts a real daemon and Chrome context. Shared per module, this would save about 1 s each |
| about 3 s | `test_t13_process` (daemon processes), `test_l1_reach_virtual`, `test_t9_virtual` shelly | Real processes; keep |

Low-risk savings, before the packaging decision: about 15 s done, and about 20 s more available from the settles, demo speed and `--for`. Leaving packaging out of local `make check` saves about 50 s (8 %).

## 5. Isolation

- **Canaries.** Every run exported `HARNESS_MANAGER_STATE_DIR` and `HARNESS_MANAGER_PTY_DIR` pointing at empty canary directories, which the per-test fixture should override. Any file in a canary would be a test that escaped its private directory.
  - Result: **0 files**, over 4 full runs and 13 loops.
  - `/tmp/harness-manager-dam1n19` was byte-identical before and after (`ls -laR`).
  - Files under `~/.config/harness-manager` changed during runs, but none were ours:
    - Chrome cache files of david's app window;
    - from 12:13 on: `daemon.json`, `locks/mps3_192.168.10.101_6900.lock`, `leases/mapstone-dev…mps3_01_pl.json` and `tunnel/ssh_config.*`. These come from david's real daemon (pid 1292380, started 12:13:05 with `--state-dir ~/.config/harness-manager`).
    - The proof: in run 2, `HOME` and both state variables pointed at canaries, and the canary `HOME` got no `.config/harness-manager` at all.
- **Leftover processes:** every child carried `HM_Q1_RUN=<tag>` in its environment. After each run, a scan of `/proc/*/environ` found **0 survivors** in every run and loop.
- **Fixed ports:** none are bound. Two things are shared with a real daemon on the same host:
  - the stub-OpenOCD debug tests use the product's default range 23300–23555. The service skips busy blocks, but a test can take the home block of david's real board for a moment;
  - `held_gdb_block` now uses 20000–23000.
- **`HOME` (run 2, fake `HOME`):** the suite wrote there:
  - `.cache/pip` (576 files, from the packaging and install tests; this is by design, and `smoke_install.sh` shares the real cache on purpose);
  - the headless Chrome's `.config/google-chrome/Crash Reports`, `.local/share/pki/nssdb`, `.local/share/applications/mimeapps.list` (empty), `.cache/dconf` and `.cache/fontconfig`.

  In a normal run these land in the real `$HOME`, beside david's Chrome profile. Harmless so far, but the browser fixture could give Chrome its own `HOME`.
- **The source tree:** `tests/web/screenshots/` (gitignored) gets about 60 PNGs per run, plus `failures/` dumps.
- **`/tmp`:** `l2_rig.pty_dir()` makes `/tmp/hm-pty-*` directories and removes them at interpreter exit (`atexit`). A killed run leaves empty directories behind. A per-test finalizer would be tighter.
- **Root-fixture gap: fixed.** The root fixture now also sets `HARNESS_MANAGER_PTY_DIR`, per test (commit 7a9650c).

## 6. Product findings for the lead and Q2 (not fixed here)

1. **`daemon/control.stop()`** SIGTERMs the pid in `daemon.json` when the daemon does not answer, without checking that the pid is still a harness-manager daemon. A crashed daemon plus a reused pid means `harness-manager daemon stop` (and `install.sh`, which calls it) can signal an unrelated process of this user. This path has never run (coverage).
2. **A reboot from the UI waits behind telemetry.** On the virtual board, `POST /controller/reboot` returned 202 only after 6.5 s: `gates.op()` waits for the Details panel's MCC reads, paced at 60 ms a character. The user sees nothing for that time. It is the same mechanism as "a UI reboot stalled" in 64eb716 #2.

## 7. What I fixed

| Commit | Change | Product? |
|---|---|---|
| 6e70600 | The OpenOCD gdb-bind test holds a block below the ephemeral range (plus the real-OpenOCD twin) | no |
| 44b282f | A boards list older than a job no longer ends it; a board clicked during start-up stays selected. Plus a deterministic test and its twin | **yes: `src/harness_manager/web/static/js/store.js`** |
| 7a9650c | Every test gets a private `HARNESS_MANAGER_PTY_DIR`; the documented default is now tested | no |
| 757e5bc | Nine vacuous checks made real | no |
| b7f2f68 | The Linux reboot test polls into the outage window | no |
| 258cb9c | The week-plan browser tests run over the real daemon by default (11 tests) | no |
| d87e2c6 | `test_l1_hub_api.py` put back as it was on main (lanes LR-A to LR-D own it) | no |
| 22053c8 | The reboot-evidence browser test takes 6 s, down from 14 s | no |
| dbf79fe | The daemon's HELD gate between short requests: 3 tests | no |

The project's dependencies are unchanged. pytest-cov, pytest-randomly and pytest-repeat were installed into the lane's venv for the assessment only. pytest-randomly and pytest-repeat were uninstalled before `make check`.

## 8. What remains, ranked

1. **`control.stop()` pid identity** (product, Q2): add the check and its test.
2. **Windows and macOS integration coverage:** run at least `tests/integration -m "not packaging"` on one of them. The daemon, deploy and console stack has never run off Linux.
3. **Real OpenOCD in CI:** `apt-get install openocd` on the Linux job would un-skip 7 tests that prove the debug argv against the real tool.
4. **`update/executor.py` rollback and journal branches** and **`pty.py` `/proc` fallback:** tests with negative twins.
5. **The six medium checks in §3.3**, especially the async count-0 races in the browser tests and `test_t13_live` never asserting rc 0.
6. **Speed:** the packaging marker decision (about 50 s), fixed settles replaced by drawn markers (about 4 s, and it removes a vacuous-check risk), and a module-shared daemon for `test_t14_harness_states`.
7. **Isolation polish:** a private `HOME` for the headless Chrome, and a per-test cleanup of `/tmp/hm-pty-*`.
8. **Local and CI parity:** `make venv` uses the platform checkout's pyverify when one exists. Either say so in `make check`'s output, or add `make check-wheel` to reproduce CI.

## 9. How to reproduce

- Full-suite runs with canaries: `scripts` were ad hoc (lane scratchpad). What they did:
  1. Export `HARNESS_MANAGER_STATE_DIR`/`HARNESS_MANAGER_PTY_DIR` to empty `/tmp/hm-q1-canary-*` directories and set `HM_Q1_RUN=<tag>`.
  2. Run `nice -n 10 .venv/bin/pytest -q -rs --junitxml … --durations=40`.
  3. List the canaries, scan `/proc/*/environ` for the tag, and diff `ls -laR /tmp/harness-manager-$USER` before and after.
- Coverage: `coverage run --rcfile=<branch=True, parallel=True, patch=subprocess, source_pkgs=harness_manager,harness_manager_mps3> -m pytest`, then `coverage combine`.
- Loops: `pytest -p no:randomly --count=30 <nodeid>` (pytest-repeat).

## `make check` (this branch, under `nice -n 10`)

```
.venv/bin/ruff check src tests
All checks passed!
.venv/bin/pytest -q
1890 passed, 9 skipped, 1 deselected in 572.20s (0:09:32)
CHECK PASS
```

That is 19 more tests than main (1871): 11 week-plan tests over the real daemon, 3 HELD-gate tests, 2 stale-list browser tests, 2 PTY-directory tests and 1 no-telemetry twin.
