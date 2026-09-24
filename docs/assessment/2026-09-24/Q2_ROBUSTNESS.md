# Q2 Robustness: Harness Manager under failure and over time (2026-09-24)

**Lane:** Q2 Robustness. **Branch:** `team/q2-robust`, rebased on `main` at `4f7ad91`.
**Method:** everything ran on srv03335 under `nice -n 10`. It used private state and PTY
directories and ephemeral ports. The fakes stood in for the hardware: VirtualMps3, the fake
`ssh` executable, the fake hub and the stub OpenOCD. No real board, hub, lease or process of
david's was touched.

## Bottom line

- **No leak in 40 minutes of churn.**
  - File descriptors, sockets, PTY masters, threads, child processes and inotify instances
    all stay flat.
  - The Python heap holds steady (tracemalloc: +0.09 MB over 4 minutes of heavy churn).
  - RSS creeps about 2.5–2.9 MB/h. That fits glibc per-thread arenas, not a Python leak.
- **Crashes and stops left debris; they no longer do.**
  - A `kill -9`'d daemon left its `ssh -N` tunnels running forever, an OpenOCD holding the
    board's JTAG port, and PTY links that could point at someone else's terminal. The next
    daemon start now cleans all three.
  - `daemon stop` falsely reported "did not stop" after 15 s whenever the daemon's parent
    was still alive. It now answers in about 1 s.
- **Eight user-facing bugs are fixed, each with a test and a negative twin.**
  - Three more problems sit in files the LR lanes own. They are reproduced here as xfail
    tests and handed over with a patch or a fix sketch.

## Top 5 findings (ranked by impact on a user)

| # | Finding | Evidence | Status |
|---|---|---|---|
| 1 | A killed daemon's `ssh -N` tunnels live forever. They hold the hub connection and forwards, and HIL_B0 told david to `pkill` them. Its OpenOCD is reaped only if that board is reopened, and until then it holds the board's single JTAG client slot. | `q2_inject`-style kill -9: two ssh and one OpenOCD survived a restart and a clean stop | **fixed** `eb40604` |
| 2 | `daemon stop` waits 15 s, SIGTERMs a zombie and says "did not stop — stop pid by hand" whenever the daemon's parent has not reaped it (the app window with pywebview; any long-lived parent). A new daemon then refuses to start (HELD). | the daemon was in state Z 0.75 s after the stop request; 15.1 s, exit 6 | **fixed** `efe94f4` |
| 3 | A close that races a deploy closes the board under the job. The deploy then fails UNREACHABLE mid-push, with the swap parked. | concurrency run: `job deploy failed UNREACHABLE` | **fixed** `5ae225f` |
| 4 | A board that is off behind the hub reads as HELD ("another client probably holds the control port") instead of UNREACHABLE ("the hub could not reach the shell"). ssh's "open failed" line arrives after the client has seen the close. | board stopped mid-deploy: `GET /boards` → 409 HELD | **fixed** `59d8280` |
| 5 | Reboot answers only after the UI's MCC reads finish, about 6.5 s on the real UI (Q1's finding). The route took the board's op gate just to fetch the adapter. | 1.72 s here with one 2 s read in flight | **fixed** `d4e6901` |

The other findings follow in "What I fixed" and "What remains".

## What I fixed

`git log --oneline main..HEAD` is in the hand-back. What each fix changes for a user:

| Commit | What now works | Test (twin) |
|---|---|---|
| `efe94f4` | A zombie counts as dead (`core.session.pid_alive`): `daemon stop` is quick and true, and a new daemon starts. `Engine.close_all` closes every board even when one fails. `Mps3Session.close` closes the tunnel even if the share close fails. A full or read-only state dir while taking a lock is a message (exit 6), and no empty lock file is left behind. | `test_an_exited_unreaped_child_is_not_alive` (twin: a running child), `test_daemon_stop_is_quick_and_true_when_its_parent_has_not_reaped_it`, `test_close_all_closes_every_board_when_one_fails_then_says_so` (twin), `test_a_full_disk_while_taking_a_lock_…` (twin inside), `test_negative_twin_the_lock_still_says_held_for_a_live_holder` |
| `eb40604` | **No debris.** Each ssh tunnel is recorded in `<state>/tunnel/procs/`. The daemon start, and every new tunnel, stops the ssh of a dead owner, but only if its argv still matches the record. The daemon start also reaps orphaned OpenOCDs and sweeps stale PTY links. SIGINT, SIGTERM or SIGHUP during the clean-up no longer kills it half way, and SIGHUP stops a foreground daemon cleanly. Start-up OSErrors are messages, not tracebacks. A tunnel whose local port was taken while starting picks new ports. daemon.log gets timestamps on uvicorn lines, job start/done/failed lines, and board event lines. | `test_kill_9_then_restart_cleans_up_…` (twin: a second daemon touches nothing), `test_a_signal_while_the_boards_close_…` ×2 (twin: without the fix it dies, `rc == -15`), SIGHUP pair, orphan-tunnel trio (twins: a live owner, a reused pid), port-taken pair, `test_a_failed_job_is_in_the_log_with_its_error` |
| `59d8280` | Board off behind the hub → UNREACHABLE with the hub's reason. The pack waits up to 0.5 s for ssh's line, and only on that failure path. | `test_a_board_that_is_off_through_the_tunnel_is_unreachable_…` (twin: a real HELD stays HELD within 2 s), `test_open_failures_since_waits_only_as_long_as_asked` |
| `5ae225f` | `BoardGates.op` re-checks for a job once it holds the lock. The deploy, restore and debug-up jobs first check that their session is still open ("closed before the deploy started; nothing was sent", ABSENT). | `_race` pair; `test_a_deploy_whose_board_closed_before_the_job_ran_sends_nothing[True/False]` |
| `91e48db` | `console --for` on a console that never connected fails with exit 7, and says "not connected yet" on stderr while it waits. It used to return `{"ok": true, "text": ""}`. | `test_a_console_that_never_connected_…` (twin: a connecting console returns its banner) |
| `3f65e18` | Paced input dropped at an ordinary close is logged as INFO "closed with N unsent". It was WARNING "link down", 268 times in the soak. | pair: close vs a real unplug |
| `d4e6901` | Reboot and SD backup are claimed at once (202). The job waits only for the request in flight and says so (first phase: `waiting for the request in flight`). `daemon/control.py` goes back to main, because Q3 owns it. | `test_reboot_is_accepted_at_once_while_an_mcc_read_is_in_flight[busy/idle]` |
| `2a6bbcc` | daemon.log drops uvicorn's bare "connection open"/"connection closed" lines, which were 40 % of the log. | `test_uvicorn_websocket_chatter_is_dropped_but_its_warnings_are_not` |
| `7e7d9da` | The rig: `tests/soak/q2_soak.py` (soak), `q2_inject.py` (failure injection), `q2_boards.py`, `q2_tables.py`. None of them is collected by pytest. | — |

Handed to other lanes (no product change on this branch):

- `f9d69cc` puts `hub.py` back to main, because the LR lanes own it. The relay fd leak's test is a **strict xfail**.
- `ae55388` and `268636a` add **xfail** reproductions for two more LR findings (below).

## Soak

**Rig.** 3 VirtualMps3 boards ran in their own process. The real `python -m
harness_manager.daemon` reached each board `via = "ssh:fakehub.invalid"`, through the fake
`ssh` executable, with stub OpenOCD. The churn ran for 40 minutes and was light, about 5
requests a second in total:

- one board closed and reopened every ~90 s;
- a console WebSocket per board every ~5 s: type, read the echo, leave;
- a uart0 PTY per board every ~10 s: open the path, write, read, close; the PTY itself is closed 30 % of the time;
- UI polling of info, telemetry, debug and consoles every ~2 s;
- an events WebSocket connecting and disconnecting every ~3 s;
- every ~4 min: deploy nanosoc, debug up, status, debug down, restore.

Samples were taken each minute.

**Two runs:** `before` is main at `64eb716`, and `after` is this branch before `2a6bbcc`.
Totals per run:

- before: 10 285 polls, 1 221 console echoes, 609 PTY attaches, 320 event sockets, 26 reopens, 10 × deploy/debug/restore;
- after: similar, 9 328 polls.

| Metric | before: first → last (max) | after: first → last (max) | Trend, minutes 20–40 | Verdict |
|---|---|---|---|---|
| fds | 7 → 17 (24) | 7 → 20 (23) | −8/h, +1/h | flat: follows the live consoles and PTYs |
| sockets | 3 → 5 (10) | 3 → 6 (10) | flat | flat |
| PTY masters | 0 → 2 (3) | 0 → 3 (3) | flat | one per open uart0 PTY, never more |
| inotify instances | 0 → 1 (1) | 0 → 1 (1) | flat | one per process, as designed |
| threads | 2 → 22 (25) | 2 → 26 (26) | −3/h, +3/h | flat: follows the live sessions |
| RSS (KiB) | 51 724 → 64 032 (65 548) | 52 392 → 64 884 (66 592) | +2 880/h, +2 477/h | slowing creep; see below |
| children (ssh + OpenOCD) | 0 → 3 (3) | 0 → 3 (4) | flat | one ssh per open board, plus OpenOCD while debug is up |
| state dir (bytes) | 809 → 300 094 | 908 → 423 240 | = daemon.log | only the log grows |
| daemon.log (bytes) | 284 → 298 805 | 385 → 419 550 | 460 KB/h, 598 KB/h | grows without a bound; see below |
| `daemon stop` at the end | **15.1 s, "did not stop"** | **1.2 s, stopped** | | fixed (#2) |
| debris after the stop | none | none | | |

**RSS.** tracemalloc on the same churn in-process, 10× faster, 4 minutes after a
1-minute warm-up: the traced heap went from 34.39 to 34.48 MB. The biggest growth was
10 KB in pyverify's FakeShell and 8 KB of date formatting. So there is no Python-level
leak. The RSS creep slows over time, 55 → 59 → 63 → 64 MB at 0/5/20/40 minutes. That
matches glibc's per-thread malloc arenas from the thread churn (a thread per console
WebSocket and PTY). **Next step:** a 4-hour run to see it plateau. `MALLOC_ARENA_MAX=2` in
the daemon's environment would cap it if it does not.

**daemon.log.** Under this churn it grew 0.46–0.60 MB/h (`after` is larger because uvicorn
lines now carry timestamps). 80 % of it was uvicorn's per-WebSocket lines, and `2a6bbcc`
removes half of those. A real UI keeps one events socket and one console socket open, so
it grows much slower. Nothing rotates it: the rotation at `daemon start` went to Q3 (below).

**Errors the churn saw:** 0.4 % of console sessions (5 of 1 221, and 6 of 1 171) did not
get their echo back within 10 s. Keystrokes typed while a console re-dials its
single-client port are dropped, and the log says so. The only other failures were the
churn's own races (a board closed between listing and calling it: 404 ABSENT, correct)
and one PORT_BOUND on the pinned debug block. The rig pins a block from the ephemeral
range; the product refused honestly.

The full per-minute tables, one per metric, are in the appendix.

## Failure injection: observed, then after the fixes

Run with `python -m tests.soak.q2_inject` against the real daemon.

| Scenario | Observed (main) | Now |
|---|---|---|
| **Board vanishes mid-console** (shell stopped, WS and PTY attached) | The console stayed "up". The fake keeps established connections open, so a dead link cannot be observed with the fakes. After the board came back, typed input echoed again, paced at 20 ms a character, and the PTY got it too. | Same. By analysis, through `ssh -L` on the real board the console stays "up" and silent until the hub's TCP gives up (see "What remains", #4). |
| **Board vanishes mid-deploy** (4 MiB push, board stopped at `push`) | Job failed honestly: `ACTION_FAILED … windowed raw-TCP push … timed out`, hint "the swap was parked … restore the baseline". `GET /boards` while it was down: **409 HELD "another client probably holds the control port"** (wrong). A later deploy was refused "a swap is already in flight" (the fake keeps its state; honest). | `GET /boards` while down: **502 UNREACHABLE "the hub … could not reach the shell (channel N: open failed …)"** (fix #4). |
| **OpenOCD dies under a session** (SIGKILL to the stub) | `GET /debug`: `failed`, "OpenOCD exited with code -9: <its last lines>", and a `debug.state failed` event. The next GET says `down` with no detail. `debug up` works again. | Unchanged (fine; the one-shot detail is under "What remains"). |
| **Tunnel ssh dies** | Supervisor restarts it on the same ports in about 1.5 s: down → starting → up, `restarts` 1. The board answers after. | Unchanged, good. |
| **…repeatedly** (killed 5× as soon as it was up) | Each restart took 1.4–1.8 s. The back-off stays at the 1 s floor, because every restart succeeded; it only grows (1, 2, 5, 10, 30 s) while restarts fail, which the L1 tests cover. `restarts` 6, one ssh per board, board fine. | Unchanged, good. |
| **Hub share drops** (MCC share stopped, then restarted on a new port) | While stopped: an honest reading, "no fpgahub share for … — start it: ssh … fpgahub share start …". After the restart: re-routed to the new port, temperature back. **But a read straight after another one failed with "another client (127.0.0.1:N) holds the hub share's write slot"**, and that client was our own connection that had just closed. | Reproduced as an xfail for the LR lanes (see "What remains", #2). |
| **Daemon `kill -9` mid-session, then restart** | Two ssh and one OpenOCD alive forever. daemon.json, the instance lock, 2 board locks, the debug record and the PTY link + owner file were left behind. The restart took over daemon.json and the lock, but not the rest; OpenOCD was reaped only when that board was reopened. | The restart reaps the ssh, OpenOCD and PTY link, and logs "clean-up after a daemon that did not stop cleanly". Stale board locks are still taken over on the next open (harmless). |
| **State dir not writable / disk full** | Daemon: **traceback**, exit 1 (mkdir, then the lock file). CLI: "internal error: PermissionError" (exit 1). A full disk mid-lock-write left an **empty lock file**. The disk-full case was simulated with a monkeypatch; no tmpfs without root. | `harness-manager-daemon: cannot create its state directory …: it is not writable — use a state directory you can write: --state-dir DIR …` (exit 6); the lock case says the same, and no empty lock is left. |
| **Two CLI commands and the UI on one board** (33 parallel requests during a 16 MiB deploy) | No 500s. 1×202, 10×200, 10×409 HELD (naming the job), 9×404 after the close, 1×422. **The DELETE won the op lock after the deploy's job had claimed the board** and closed it under the job: deploy failed UNREACHABLE. | Fixed (#3): a waiting request is refused once a job claims the board, and a job whose session was closed fails ABSENT before sending anything. |
| **Port already taken** | `--port N` taken: exit 5, "port N … is already in use — pick another --port, or 0". A pinned debug base taken: PORT_BOUND, not moved (seen in the soak). A tunnel's local port taken while starting: UnreachableError with a VPN hint (misleading). | Tunnel: picks new ports, up to 3 tries; if they are pinned, the hint names the local port. |
| **Clock jumps** (by reading the code) | Every timeout, back-off, pacing and lease-heartbeat interval uses `time.monotonic()`. Wall clock is used only for timestamps shown to people, telemetry staleness (a jump makes readings look fresher or staler, and they are labelled), the store's 24 h temp-file sweep, and the torn-lock grace (a backwards jump delays a torn lock's takeover). Nothing blocks or fires early. | No change needed. |
| **Malformed requests** (23 cases) | All were 4xx with the error envelope, a message and a hint. No 500, no traceback in daemon.log. | Unchanged. |

## Shutdown and cleanup

- **`daemon stop`, idle or with 3 boards open:** ~1 s. Clean every time: no children,
  daemon.json, instance lock, board locks, debug records or PTY links left. Before fix #2
  it took 15 s and reported failure whenever the parent had not reaped the daemon.
- **Ctrl-C of foreground verbs.** All exit 0 and leave nothing:
  - `debug up` (through the daemon): debug goes down and OpenOCD is gone.
  - `console`: exits cleanly.
  - `pty`: in-process, the PTY dir is removed. Through the daemon, the PTY stays, as its
    message says, because it belongs to the daemon.
- **Closing the app window** leaves the daemon running, by design (`daemon stop` stops it).
  With pywebview the `app` process is the daemon's parent, which is the zombie case fix #2
  covers.
- **A signal during the clean-up** (a second Ctrl-C, `daemon stop`'s SIGTERM fallback)
  used to kill it half way. It is now logged and the clean-up finishes (fix in `eb40604`).
- **Crash orphans are reaped on the next start** (fix #1). If only a CLI verb was killed,
  the next tunnel start reaps its tunnel too.

## Error honesty (sampled)

- **CLI against a refused port** (`127.0.0.1:1`, no daemon). Each gives a clear message
  and a hint:
  - `info` → 7;
  - `--json info` → 7, JSON on stdout;
  - `reset` → 7;
  - `program` with no overlays → 3;
  - `debug detect` without OpenOCD → 12;
  - `mcc temp` without USB → 12;
  - `lease show` without a hub → 3;
  - `daemon stop` when stopped → 8;
  - unknown verb or missing TARGET → 2.
- **One false "done", now fixed (#`91e48db`):** `console --for` on a console that never
  connected.
- **Cosmetic:**
  - argparse errors print `harness-manager: harness-manager: …` (the prefix twice);
  - generic API USAGE errors carry a CLI hint ("run `harness-manager help`").
- **The daemon's 500s:** none in 23 malformed cases, 33 concurrent requests, or ~10 000
  soak polls. The `_guard` middleware still turns any bug into the envelope.

## Logging

- **Enough to diagnose a field problem now?** Mostly. Before this work, a failed deploy or
  reboot job left **nothing** in daemon.log, and uvicorn's lines had no timestamps. Now
  daemon.log has:
  - every job's start, done (duration) and failure (error name and message);
  - session open and close, identity changes, deploy done or failed, debug state,
    reboot, power, lease and update events;
  - the tunnel's state changes (already there);
  - the clean-up at start;
  - "boards closed in N s" at stop;
  - timestamps on every line.
- **Secrets:** API tokens in WebSocket URLs are masked (`token=***`): 307 checked in the
  soak log, none leaked. Lease tokens are never logged.
- **One leak outside the log:** the lease token rides on the `ssh … fpgahub lease
  heartbeat --token T` command line, which is visible in `ps` to every user on srv03335
  and on the hub (see "What remains", #5).

## What remains, ranked by impact on a user

1. **[LR, services/lease.py] The lease heartbeat thread dies on the first non-HarnessError**
   (an OSError from `store.put` on a full disk, or anything unexpected from pyverify). It
   never beats again, and the lease lapses with **no `lease.state` event**.
   - Repro: `test_the_lease_heartbeat_survives_one_failed_round` (xfail, not strict so the
     fix does not break the merge).
   - Fix: in `beat_due`, catch `Exception` per board, `log.exception`, and retry next tick.
2. **[LR, hub.py] Back-to-back MCC reads over a hub share refuse themselves.**
   - Cause: `open_hub_share` sets read-only from `share list`'s reader count, and that
     count still includes our own connection that closed a few ms earlier.
   - Where it bites: telemetry reads temperatures then oscillators, and Reboot now follows
     a read immediately.
   - On the real hub, `share list` takes ~1 s over ssh, which mostly masks it.
   - Repro: `test_back_to_back_mcc_reads_over_a_hub_share_both_work` (xfail).
   - Fix sketch: when the only reader could be our own last connection (closed < 1 s ago),
     re-list after ~200 ms before going read-only.
3. **[LR, hub.py] `ShareRelay` leaks two fds per shared-console connection** until the
   board closes. It is the same kind of leak as the one fixed in the product elsewhere. A
   UI that re-opens a shared lane console for hours runs toward `ulimit -n`.
   - Repro: `test_the_share_relay_closes_both_sockets_of_each_connection` (strict xfail).
   - Patch (tested on this branch before it was handed back, `efe94f4`):

   ```diff
   @@ class ShareRelay: _serve
   -        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
   -        _pipe(upstream, client)
   +        def from_client() -> None:
   +            _pipe(client, upstream)
   +            with contextlib.suppress(OSError):
   +                upstream.shutdown(socket.SHUT_RDWR)   # the console left: end both ways
   +
   +        back = threading.Thread(target=from_client, daemon=True)
   +        back.start()
   +        try:
   +            _pipe(upstream, client)
   +            back.join(timeout=5.0)
   +        finally:
   +            with self._mu:
   +                self._conns = [c for c in self._conns if c is not client and c is not upstream]
   +            for c in (client, upstream):
   +                with contextlib.suppress(OSError):
   +                    c.shutdown(socket.SHUT_RDWR)
   +                c.close()
   ```
4. **A board that dies behind the hub leaves its consoles "up" and silent.** Through
   `ssh -L`, the hub's TCP to the board fails only on a retransmit timeout (~15 min, and
   only once something is sent). The GUI's console dot stays green while Health says
   offline. A design choice for the lead: mark consoles `down: the board is unreachable`
   when the health poll says offline.
5. **The lease token is on the ssh command line** (`fpgahub lease heartbeat --token T`),
   visible in `ps` on the multi-user srv03335 and on the hub. The fix needs fpgahub and
   pyverify: a `--token-stdin` or an environment variable.
6. **[Q3, daemon/control.py] daemon.log is never rotated.** Suggested: rotate to
   `daemon.log.1` in `_spawn` past 8 MiB, and turn an OSError there (a read-only state
   dir) into a message. `harness-manager daemon start` currently prints "internal error:
   PermissionError". The patch was tested on this branch and then removed:

   ```diff
   +LOG_ROTATE_BYTES = 8 * 1024 * 1024
   +def _rotate_log(log_path, limit=LOG_ROTATE_BYTES):
   +    try:
   +        if log_path.stat().st_size > limit:
   +            os.replace(log_path, log_path.with_name(log_path.name + ".1"))
   +    except OSError:
   +        pass
    def _spawn(...):
   -    state_dir.mkdir(parents=True, exist_ok=True)
   -    fd = os.open(log_path, ...)
   +    try:
   +        state_dir.mkdir(parents=True, exist_ok=True)
   +        _rotate_log(log_path)
   +        fd = os.open(log_path, ...)
   +    except OSError as exc:
   +        raise ActionFailedError(f"cannot start harness-manager-daemon: cannot write {log_path}: {exc.strerror or exc}",
   +                                hint="use a state directory you can write on a disk with free space (HARNESS_MANAGER_STATE_DIR)") from exc
   ```
7. **MCC reads still serialise every other request on the board.** Reset, info and the
   control port wait behind a 2–6 s MCC read. That is correct for the MCC console, but
   not needed for the control port. The follow-up is one gate per resource (control port,
   MCC console) instead of one per board, with jobs holding both. That is a design
   change, so I did not make it today.
8. **RSS creep of ~2.5 MB/h** (probably glibc arenas). Needs a 4-hour soak; if it does not
   plateau, set `MALLOC_ARENA_MAX=2` in the daemon's environment.
9. **Small items:**
   - `GET /debug`'s "failed" detail is one-shot;
   - keystrokes typed while a console re-dials are dropped (logged);
   - stale board lock files stay until the next open;
   - the CLI's in-process engine does not reap at start (the daemon and each new tunnel do);
   - the doubled `harness-manager:` prefix on argparse errors.
10. **For Q1, flaky tests seen under load** (load average ~29 from the parallel lanes):
    - `test_l2_pty.py::test_negative_twin_a_flushing_reset_loses_the_queued_output` fails
      about 1 in 3 alone on srv03335, on main too.
    - `test_t13_live.py::test_a_stalled_console_client_is_told_what_it_lost` failed once in
      a full run (0 bytes lost) and passes alone 3/3.
    - The T4 debug tests use the real default port block (23300+), so a test's stub can
      take the slot david's `debug up` prefers. His session would then move to another
      block.

## Contract change requests

- **CCR-Q2-1: an optional `BoardPack.reap_orphans() -> list[str]`** (`core.pack`).
  - The daemon calls it once at start, after it holds the instance lock. The pack stops
    what a dead owner left running and returns a description of each item.
  - Additive and optional: the daemon uses `getattr`.
  - Affects pack authors. `Mps3Pack.reap_orphans` is added in the **lead-owned**
    `harness_manager_mps3/pack.py`.
  - Also edited in `pack.py`: `Mps3Session.close` try/finally, and `OPEN_FAILURE_GRACE_S`
    for the tunnel refusal.
- **CCR-Q2-2: `SshTunnel.open_failures_since(t0, wait_s=0.0)`**. The new keyword is
  additive, and the default keeps today's behaviour.
- **CCR-Q2-3: job phases** (docs/API.md, additive). A job that waits for the request in
  flight on its board starts with the phase `waiting for the request in flight`. Board
  jobs (deploy, restore, debug_up) whose board was closed before they ran fail ABSENT.
- **CCR-Q2-4: CLI exit code.** `console … --for S` (and `--json`) on a console that never
  connected now exits 7 UNREACHABLE; it used to exit 0 with empty text.
- **CCR-Q2-5: behaviour, not shape:**
  - `core.session.pid_alive` treats a zombie as dead (Linux);
  - `Engine.close_all` closes all, then raises the first failure;
  - state-dir write failures are `ActionFailedError` (6);
  - new state files: `<state>/tunnel/procs/*.json`.

## For the lead to decide

1. **Route the three LR findings** (heartbeat, share write slot, relay fds). Their tests
   are xfail, and the relay one is strict.
2. **Hand the daemon.log rotation to Q3** (patch above).
3. **Consoles behind a dead hub link** (remaining #4): mark them down when Health says
   offline?
4. **Split the board gate by resource** (control port / MCC console)?
5. **The lease token on argv**: an fpgahub or pyverify change.
6. **Accept the edits to lead-owned `pack.py`** (CCR-Q2-1).

## Reproduce

```bash
nice -n 10 .venv/bin/python -m tests.soak.q2_soak --root /tmp/hm-q2-soak --minutes 40
nice -n 10 .venv/bin/python -m tests.soak.q2_inject --root /tmp/hm-q2-inject
.venv/bin/python -m tests.soak.q2_tables after=/tmp/hm-q2-soak/soak.json
nice -n 10 .venv/bin/pytest -q tests/unit/test_q2_robustness.py tests/integration/test_q2_daemon_debris.py
```

**Scratchpad note:** subagent lanes share one scratchpad directory. My `check1.log` was
overwritten by LR-C's `make check`. Name scratch files per lane.

## Appendix: soak tables, one per sampled metric

`before` is main at `64eb716`; `after` is this branch before `2a6bbcc`. Rows are every 5
minutes, then an idle sample after the churn stopped, with the boards still open.

**open file descriptors** (`fds`)

| minute | before | after |
|---:|---:|---:|
| 0 | 7 | 7 |
| 5 | 19 | 22 |
| 10 | 22 | 19 |
| 15 | 20 | 20 |
| 20 | 24 | 20 |
| 25 | 20 | 16 |
| 30 | 19 | 22 |
| 35 | 19 | 22 |
| 40 | 19 | 23 |
| 40 (idle) | 17 | 20 |

before: min 7, max 24, first 7, last 17

after: min 7, max 23, first 7, last 20

**of which sockets** (`sockets`)

| minute | before | after |
|---:|---:|---:|
| 0 | 3 | 3 |
| 5 | 7 | 8 |
| 10 | 8 | 7 |
| 15 | 8 | 8 |
| 20 | 10 | 8 |
| 25 | 8 | 6 |
| 30 | 7 | 8 |
| 35 | 7 | 8 |
| 40 | 7 | 9 |
| 40 (idle) | 5 | 6 |

before: min 3, max 10, first 3, last 5

after: min 3, max 10, first 3, last 6

**of which PTY masters** (`ptmx`)

| minute | before | after |
|---:|---:|---:|
| 0 | 0 | 0 |
| 5 | 2 | 3 |
| 10 | 3 | 2 |
| 15 | 2 | 2 |
| 20 | 3 | 2 |
| 25 | 2 | 1 |
| 30 | 2 | 3 |
| 35 | 2 | 3 |
| 40 | 2 | 3 |
| 40 (idle) | 2 | 3 |

before: min 0, max 3, first 0, last 2

after: min 0, max 3, first 0, last 3

**inotify instances** (`inotify`)

| minute | before | after |
|---:|---:|---:|
| 0 | 0 | 0 |
| 5 | 1 | 1 |
| 10 | 1 | 1 |
| 15 | 1 | 1 |
| 20 | 1 | 1 |
| 25 | 1 | 1 |
| 30 | 1 | 1 |
| 35 | 1 | 1 |
| 40 | 1 | 1 |
| 40 (idle) | 1 | 1 |

before: min 0, max 1, first 0, last 1

after: min 0, max 1, first 0, last 1

**threads** (`threads`)

| minute | before | after |
|---:|---:|---:|
| 0 | 2 | 2 |
| 5 | 20 | 25 |
| 10 | 25 | 20 |
| 15 | 20 | 21 |
| 20 | 24 | 24 |
| 25 | 21 | 16 |
| 30 | 22 | 24 |
| 35 | 22 | 26 |
| 40 | 22 | 26 |
| 40 (idle) | 22 | 26 |

before: min 2, max 25, first 2, last 22

after: min 2, max 26, first 2, last 26

**resident memory (KiB)** (`rss_kb`)

| minute | before | after |
|---:|---:|---:|
| 0 | 51724 | 52392 |
| 5 | 62172 | 61000 |
| 10 | 61068 | 61976 |
| 15 | 62360 | 63576 |
| 20 | 62856 | 63960 |
| 25 | 62864 | 64168 |
| 30 | 63436 | 66592 |
| 35 | 63884 | 64548 |
| 40 | 64032 | 64884 |
| 40 (idle) | 64032 | 64884 |

before: min 51724, max 65548, first 51724, last 64032

after: min 52392, max 66592, first 52392, last 64884

**child processes (ssh + OpenOCD)** (`children`)

| minute | before | after |
|---:|---:|---:|
| 0 | 0 | 0 |
| 5 | 3 | 3 |
| 10 | 3 | 3 |
| 15 | 3 | 3 |
| 20 | 3 | 3 |
| 25 | 3 | 3 |
| 30 | 3 | 3 |
| 35 | 3 | 3 |
| 40 | 3 | 3 |
| 40 (idle) | 3 | 3 |

before: min 0, max 3, first 0, last 3

after: min 0, max 4, first 0, last 3

**state dir size (bytes)** (`state_bytes`)

| minute | before | after |
|---:|---:|---:|
| 0 | 809 | 908 |
| 5 | 36538 | 59749 |
| 10 | 75159 | 116539 |
| 15 | 110886 | 169006 |
| 20 | 148283 | 224508 |
| 25 | 184547 | 272144 |
| 30 | 223045 | 321890 |
| 35 | 261608 | 375427 |
| 40 | 300094 | 423240 |
| 40 (idle) | 300094 | 423240 |

before: min 809, max 300094, first 809, last 300094

after: min 908, max 423240, first 908, last 423240

**daemon.log size (bytes)** (`log_bytes`)

| minute | before | after |
|---:|---:|---:|
| 0 | 284 | 385 |
| 5 | 35249 | 56059 |
| 10 | 73870 | 112847 |
| 15 | 109597 | 165315 |
| 20 | 146995 | 220816 |
| 25 | 183258 | 268452 |
| 30 | 221757 | 318198 |
| 35 | 260319 | 371736 |
| 40 | 298805 | 419550 |
| 40 (idle) | 298805 | 419550 |

before: min 284, max 298805, first 284, last 298805

after: min 385, max 419550, first 385, last 419550

