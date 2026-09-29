# HIL, unattended: the runner

> One runner for [HIL_LINUX.md](HIL_LINUX.md) and [HIL_B0.md](HIL_B0.md). It runs every check
> a machine can judge, through Harness Manager's own CLI (`harness-manager --json …`), saves
> each answer as evidence, and writes `REPORT.md`. The checks that need a person, a GUI,
> Vivado, or a write it must not make stay manual; the report lists them.
>
> **Who:** david starts it, on srv03335, with his lease. No agent runs it.
> **When:** the HM nights, Mon 28, Tue 29, Wed 30 Sep and Fri 2 Oct, 18:00 → 08:30.
>
> **Where:** in the app (the board's **Checks** section, below). The command line is the
> fallback, and when a Harness Manager service is running it hands the run to it.

## In the app

The run lives in the Harness Manager service (lane HIL-GUI, `services/hil_runs.py`). No
`daemon stop`, no `lease acquire --ttl`, no tmux, no `source`: the service keeps the board open
for the run and keeps the lease until the run ends.

1. **17:30: the announcement.** Open the board, then **Checks**.
   - **Plan** is picked from the board: bare metal → `bare-metal`; the Linux harness with no
     user microSD → `linux-nocard` (board 2); with a blank card (no valid OS slot) →
     `linux-netboot`; with a usable card → `linux`. The line under it says why. Pick another
     to override it.
   - **Writes:** Read only, or Safe (the swaps and the MCC read; greybox is put back).
   - **Run until** (default the next 08:30) **every** 30 min.
   - **Start:** Now, or **At** a time (18:00): the service begins the run then.
   - Press **Write the announcement**. The **Announcement** box shows `ANNOUNCE.txt` as the run
     would write it; **Copy** puts it on the clipboard for the 17:30 message. Nothing is sent.
2. **The lease.** The line above Start says what happens:
   - **yours** (this Harness Manager holds it): the service heartbeats it until the run ends,
     however long that is;
   - **free:** tick **Take the lease for the run**. The service takes it when the run starts,
     keeps it until the run ends, then releases it;
   - **someone else's** (or your hub name in another session or tool): Start is off, and the
     line names the holder. Ask for the board (Request board) or wait. Nothing is ever forced.
3. **Start** (or **Schedule for 18:00**). The panel shows the iteration, the check it is on,
   the pass/fail/skipped counts for this iteration and the whole run, the first failure with
   its hint and evidence file, when the next iteration starts, and the last lines of its log.
   The **Checks** tab has a badge, and a banner says a run is on while you look at the board.
   You can close the app: the run goes on in the service.
4. **Stop** finishes the check it is on, puts greybox back and writes `REPORT.md` (the same as
   Ctrl-C to the command line). A scheduled run that has not started is cancelled.
5. **08:30: Past runs** lists every run on the board, newest first. Open one for its
   `REPORT.md`; with more than one iteration, each iteration's own report is a button. The
   evidence folder is `<state dir>/checks/<board>/<YYYYMMDD-HHMMSS>-<plan>/` (the run's
   Evidence line), laid out as below.

**The service's settings apply, not your shell's.** The run's commands run in the service's
environment, so what `env.sh`/`env_b2.sh` export for the command line must be settings the
service reads. Once per machine (`config set` says when a change needs the board reopened or
the service restarted):

```bash
harness-manager config set mps3.overlay_dirs $HOME/SoCLabs/mps3-nanosoc-platform-lx/fpga/dfx/build_mint3_rc2_linux/overlay_mbv
harness-manager config set tools.hw_server /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/hw_server
```

(`D1`'s overlays check says so when they are missing: "the overlays folder is gone or keyed to
another static".) The board and its hub come from `boards.toml`, as for every other verb.

While a run is active the board cannot be closed, and `harness-manager daemon stop` refuses
(naming the run); `--force` stops each run first, and the service keeps serving while the
run restores greybox through it (up to 3 min), then stops. A service that is killed ends the run where it is. A run is an explicit action,
so QUIET-POLL's viewer rules do not gate it: it runs with no window open, paced by its own
rules (`--gap`, `--interval`).

**Our own service is not another holder.** The run's commands are Harness Manager's CLI
through this service (it shares the service's board session), so the app having the board
open no longer makes them refuse. A different process or client still stops the run: another
host's card job, another client on the control port, someone else's lease.

## From the command line (the fallback)

`python -m tools.hil run …` (or `make hil-auto`) with the same options as before. With a
Harness Manager service running for the state dir it does not run the checks itself: it hands
the run to the service (`POST /boards/{bid}/checks`, `--take-lease` when the lease is free),
prints its progress and exits with its code; Ctrl-C there is Stop. The app shows the same run.
With no service running (or `--in-process`) it runs here, every command on the CLI's
in-process engine, as below: then nothing heartbeats the lease, so take it with a `--ttl`
that outlasts the run, and a service holding the board makes every command refuse (exit 4).

## Run it overnight (command line, no service)

1. **17:30: write the announcement** (sends nothing to the board or the hub):
   ```bash
   source ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh
   export RUN=$HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil-auto/$(date +%m%d)
   cd ~/SoCLabs/harness-manager
   .venv/bin/python -m tools.hil run --plan linux-netboot --board $B --evidence $RUN \
     --writes safe --repeat 40 --interval 1200 --until 08:30 --announce-only
   ```
   It prints `$RUN/ANNOUNCE.txt`: start, planned end, plan, writes mode, what it changes and
   what it never does. Paste it into the 17:30 announcement. The run rewrites it at the start
   with its own start time and pid.
2. **18:00: free the board for the runner, then take the lease** (terminal B):
   ```bash
   harness-manager daemon stop
   harness-manager lease acquire $B --ttl 54000 --holder david-hm
   ```
   - `daemon stop`: with no service running, the runner's commands run on the CLI's
     in-process engine (with a service running, the run goes to it: "In the app"). A service
     holding the board while the runner is `--in-process` makes every command refuse
     (exit 4), and the runner stops.
   - `--ttl 54000` is 15 h, past 08:30. Nothing heartbeats the lease overnight. The runner
     stops `--margin` (10 min) before it expires, so a swap never outlives it.
3. **Start it in tmux.** A tmux window does not inherit this shell's variables: set them
   again inside it.
   ```bash
   tmux new -s hil
   source ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh
   export RUN=$HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil-auto/$(date +%m%d)
   cd ~/SoCLabs/harness-manager
   .venv/bin/python -m tools.hil run --plan linux-netboot --board $B --evidence $RUN \
     --writes safe --repeat 40 --interval 1200 --until 08:30
   ```
   Detach with `Ctrl-b d`. Without tmux: `nohup <the same command> > $RUN.log 2>&1 &`.
   From the checkout, `make hil-auto HIL_ARGS='--plan … --board … --evidence …'` is the same.
4. **08:30: read `$RUN/REPORT.md`.** Its first line says PASS, FAIL or STOPPED. Then
   `harness-manager lease release $B`.

## Board 2: the nightly run

Board 2 (`mps3_02_pl`, `192.168.11.101`, boards.toml `lab2`) is Harness Manager's own board: HIL-AUTO
runs on it every HM night. **In the app:** open board 2, **Checks**: the plan is picked as
`linux-nocard` (no user microSD), writes Safe, until 08:30 every 30 min, Start at 18:00. The
steps below are the command-line fallback. It has **no user microSD** and no JTAG, so the plan is `linux-nocard`
(HIL_LINUX.md "Card-less mode"): swaps and the MCC read only, **no reset of any kind**.
`tools/hil/env_b2.sh` sets `B`, `T`, `MCC_TTY`, `EV` and `RUN` (`…/2026-09-hil-auto/<MMDD>-b2`).

1. **17:30: the announcement** (sends nothing):
   ```bash
   source ~/SoCLabs/harness-manager/tools/hil/env_b2.sh
   cd ~/SoCLabs/harness-manager
   .venv/bin/python -m tools.hil run --plan linux-nocard --board $B --evidence $RUN \
     --writes safe --repeat 30 --interval 1800 --until 08:30 --announce-only
   ```
2. **18:00: free the board for the runner, then a 15 h lease:**
   ```bash
   harness-manager daemon stop
   harness-manager lease acquire 192.168.11.101 --ttl 54000 --holder david-hm
   ```
3. **Start it in tmux** (set the variables again inside it):
   ```bash
   tmux new -s hil-b2
   source ~/SoCLabs/harness-manager/tools/hil/env_b2.sh
   cd ~/SoCLabs/harness-manager
   .venv/bin/python -m tools.hil run --plan linux-nocard --board $B --evidence $RUN \
     --writes safe --repeat 30 --interval 1800 --until 08:30
   ```
   One iteration is about 5 min (two verified swaps of ~75 s each, the reads, the MCC read),
   then 30 min of rest: about 25 iterations by 08:20. `--until` ends it; `--repeat 30` is the cap.
4. **08:30:** read `$RUN/REPORT.md`, then `harness-manager lease release 192.168.11.101`.

After a warm reset board 2 sits in stage0 rescue until the Linux lead pushes an image; its claim
and host key change on every netboot. HIL-AUTO never resets it. A board that went to rescue is
`unreachable` (the run stops, exit 2, and REPORT.md says where); a changed host key refuses SSH
(exit 15, a stop): re-adopt it (HIL_LINUX.md Netboot mode 3) and ask the Linux lead.

**Stop it early:** `Ctrl-C` in its tmux window, or `kill -INT <pid>` (the pid is in
`ANNOUNCE.txt`). It finishes the check it is on, puts greybox back, writes the report, and
exits 2.

## The options

| Option | Default | What it does |
|---|---|---|
| `--plan` | (required) | `linux-netboot` (HIL_LINUX.md in Netboot mode: a blank card), `linux-nocard` (Card-less mode: board 2, no user microSD), `linux` (the card usable again), `bare-metal` (HIL_B0.md) |
| `--board` | (required) | the board: `192.168.10.101` (board 1), `192.168.11.101` (board 2) |
| `--evidence DIR` | (required) | a new folder; one holding evidence is refused (never overwritten) |
| `--writes` | `none` | `none`: read-only checks. `safe`: also the swap-and-restore checks, the MCC read and `identify` (Linux A6: a 5 s blink; bare metal R7b: refused) |
| `--repeat N`, `--interval S` | 1, 900 | N iterations, S seconds apart (at least 60) |
| `--until HH:MM` (or `--deadline`) | none | no check starts after `until − margin`; then it restores and exits. `HH:MM` is the next one; an ISO time also works |
| `--margin MIN` | 10 | the quiet minutes before `--until` and before the lease expires |
| `--stop-on-first-fail` | off | end at the first failed check (still restores) |
| `--expect-static HEX` | the runbook's | the static the board must run (`0x44ee76d5` Linux RC2, `0x72bb0a36` bare metal) |
| `--gap S` | 3 | seconds between two commands |
| `--max-unreachable N` | 3 | tries of a read check before an unreachable board stops the run |
| `--announce-only` | off | write `ANNOUNCE.txt` and exit |
| `--take-lease` | off | through a service: when the lease is free, the service takes it for the run and releases it at the end |
| `--in-process` | off | never hand the run to a running service: run it here on the CLI's in-process engine |

`python -m tools.hil plans [--plan P]` prints every check: id, section, tier, command.

## What it runs, and what stays manual

The plans are `src/harness_manager/checks/plans.py` (`tools/hil/plans.py` is the same module). Check ids are the runbook's; a lettered id (`E1b`, `R7b`)
is a second command of that check. A check may accept a second answer (A6: exit 12, the image
has no `locate`); its pass line then says which (`not on this image`). `tests/unit/test_hil_auto_plans.py` fails when a runbook
check with an **Expect** has no plan entry, or the other way round, and when the netboot or
nocard plan's skips differ from the runbook's Netboot mode or Card-less mode list.

**`linux-netboot`** (per iteration):

- **read:** 0.2 (no share on `tty_00`), 0.3 (lease held here), 0.4 (version), A1 identity
  (shell `0x44ee76d5`, `harness_impl linux`: anything else **stops**), A2 panel, A3 XVC status,
  A5 `board identity` (net-protocol v0.16: the verdict and what the board reports are
  recorded, a mismatch is never a failure; the pass line names the image, A1's
  `harness_version` and `features`), B1 claim status, B3 `persist.state` (only once B2's adopt pinned the claim here), C1 slots
  (both `empty`), D1 overlays;
- **safe** (`--writes safe`): A6 `identify --seconds 5` (a 5 s backlight blink with an
  IDENTIFY banner; nothing persistent, no claim lock. It passes as a blink on an image with the
  `locate` feature, or as exit 12 naming `harness feature 'locate'` on one without: "not on this
  image"; any other answer fails); D4a the MCC read on the hub; E1 program `nanosoc_ila` (not
  kept on the card), E1b identity; Z2 restore greybox, Z2b identity;
- **skipped (Netboot mode):** C2, D2, D3, D5, §G, Z1, Z2's `card status` (Z2c), and D4.

**`linux-nocard`** (board 2): the same as `linux-netboot`, except:

- C1 expects the card-less answer: `slot status` exits 12, `no user microSD card in the slot`
  (the harness said `card: false`, no slots). Harness Manager reports a card-less board as
  unavailable; it never prints `card: false` itself;
- D4, F6 and §G are skipped, not manual: no reset of any kind on a board with no card and no
  JTAG. Every skip reason starts "no user microSD";
- the safe checks are the same four: A6 (locate), D4a (MCC read), E1 (program `nanosoc_ila`),
  Z2 (restore).

**`linux`:** the same as `linux-netboot`, plus C2 and Z2c (the card's default line is C2's); C1
and B3 expect a card-backed board.

**`bare-metal`:** 1 (lease), 2 (no share on `tty_00`), R1, R5 (needs
`HARNESS_MANAGER_OPENOCD` from HIL_B0.md 0.3), R6, R7, R10, R11 read; R4 (MCC), R7b
(`identify`, refused on bare metal), W1/W1b, W4/W4b safe.

**Manual, and why:**

| Checks | Why never unattended |
|---|---|
| D2, D3, D5, Z1 | write the user microSD (keep on the card, card clear) |
| D4, G3 | an MCC REBOOT power-cycles the board |
| §G (G1, G2, G4), F6 | write the config SD (sudo mount on the hub; the SD door) |
| F1–F4 | raw `ssh`/`fpgahub` on the hub, not Harness Manager verbs. §F is manual in every plan (a test holds it), and F3 stages the board's OWN bake from HIL_LINUX.md F3's per-board table |
| B2 | a trust decision: it pins the board's host key and asks `y` |
| A4 | a finger on the panel |
| E2–E5, W5 | Vivado, read by a person |
| R2, R3, R8, R9, W2, W3 | the app, `screen`, gdb |
| 0.1, 3, Z3–Z6, 7 | setup and close-out: the lease is released by david, never by the runner |

## The safety rules (enforced in `harness_manager/checks/run.py`, on both routes)

1. **The lease.** Before anything touches the board, `lease show` must say `lease.here`: this
   Harness Manager holds the token. Asked again before every section, every safe check and the
   final restore; lost means stop. It never acquires, requests, forces or releases one.
2. **The allow-list.** Every command must match an exact argv shape for the `--writes` mode.
   Nothing else is ever sent: no `--keep-on-card`, no `--force`, no slot, card or SD verbs,
   no `mcc reboot`/`mcc cmd`, no `share start` (never a share on `tty_00`), no `board claim`.
3. **Stops (exit 2):** an unexpected identity; a refusal (exit 15, the claim lock); HELD
   (exit 4: another holder, a busy control channel, or the reset guard's card job, which it
   never forces); the lease lost; the board unreachable `--max-unreachable` times (reads back
   off 30 s, doubling to 10 min; a write is never retried). A HELD from this Harness
   Manager's own request on the control port (`error.data.reason: OWN_REQUEST`) is not
   another holder: a read is asked again after the back-off, a write fails that check and the
   run goes on.
4. **The MCC read:** another reader on `tty_00` (the soak names it) is "skipped (tty_00
   busy)", never retried, never a failure.
5. **The end state**, in a `finally`: if it swapped the partition, it restores greybox (while
   the lease is still here; a reset-guard refusal is asked again every 60 s for up to 40 min,
   never forced) and reads the identity back. **The SSH claim:** it never claims, so the claim
   ends as it started (B1's state, read again at the end). Harness Manager has no verb to
   unclaim: only `mps3-unclaim` on the board's serial console does that.
6. **Pace:** `--gap` between commands, `--interval` of at least 60 s, back-offs, no loops.

## The evidence

```
$RUN/
  ANNOUNCE.txt        what it will do (written first)
  0_lease_gate.json   the start's lease check
  summary.json        the run: exit, end state, every iteration's counts and first failure
  REPORT.md           the same, to read
  end_restore.json    the final restore, when one was needed
  iter-001/           one per iteration (with --repeat > 1; one iteration writes here directly)
    a1_info.json …    one per check, the runbook's file name: the command, exit, seconds,
                      verdict, why, the CLI's JSON, stderr
    summary.json  REPORT.md
```

**REPORT.md:** the result line (PASS exit 0, FAIL exit 1, STOPPED exit 2 with the reason); the
end state (partition, claim); the first failure with its runbook id, hint and evidence file;
a pass/fail/stopped/manual/skipped table per runbook section; every check; the manual list.
With `--repeat`, the top report lists each iteration and which checks failed in which.

## When it stops or refuses

| REPORT.md says | Do this |
|---|---|
| `refused to start: the board is not leased` / `held by …` | take the lease (step 2); `lease show $B` must say `yours` |
| `refused to start: the lease expires at …` | take it with a longer `--ttl` |
| `HELD (exit 4): … is in use` at the first board check | `--in-process` while a service holds the board: drop `--in-process` (the run goes to the service), or `harness-manager daemon stop` |
| `refused to start: the hub lease on … is held by …` (app or service) | someone else's lease: Start names them. Request the board, or wait |
| `refused to start: nobody holds the hub lease … take it for the run` | tick **Take the lease for the run** (`--take-lease`) |
| `HELD (exit 4): slot B is being written …` | a card job is running: someone else is using the board. Ask the Linux lead |
| `an unexpected identity` | the board is not on the expected static: HIL_LINUX.md's failure table (A1 row) |
| `refused (exit 15)` | a safety rail or the claim lock; the message says which |
| `the board was unreachable N times` | HIL_LINUX.md's A1 `offline` row |
| partition: `NOT attempted` / `not restored` | put greybox back by hand: `harness-manager restore $B` |
