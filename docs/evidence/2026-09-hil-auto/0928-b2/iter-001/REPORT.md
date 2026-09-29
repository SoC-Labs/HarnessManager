# HIL-AUTO: linux-nocard on 192.168.11.101

- **STOPPED for safety: A1: HELD (exit 4): mps3@192.168.11.101:6900 is in use — held by dam1n19 on srv03335 (pid 3115305, 22707s): harness-manager-daemon: harness-manager-ui. Another holder, or the reset guard's card job: the runner never forces** (exit 2)
- plan `linux-nocard` (docs/HIL_LINUX.md), `--writes safe`, static `0x44ee76d5`, iteration 1 of 30
- ran 2026-09-28T21:08:23+01:00 → 2026-09-28T21:09:18+01:00

## First failure

**A1** Identity: HELD (exit 4): mps3@192.168.11.101:6900 is in use — held by dam1n19 on srv03335 (pid 3115305, 22707s): harness-manager-daemon: harness-manager-ui. Another holder, or the reset guard's card job: the runner never forces

Hint: `harness_impl` not linux or shell_id not the static: the board is not on RC2 (rolled back?): STOP, ask the Linux lead. `offline`: wait 60 s, repeat; then ask the Linux lead

Evidence: `a1_info.json`

## Sections

| § | section | pass | fail | stopped | manual | skipped |
|---|---|---|---|---|---|---|
| 0 | Setup | 3 | 0 | 0 | 1 | 0 |
| A | The Linux harness through Harness Manager | 0 | 0 | 1 | 0 | 3 |
| B | The SSH claim: check it, adopt it, never re-claim | 0 | 0 | 0 | 0 | 3 |
| C | OS slots and the user microSD (read) | 0 | 0 | 0 | 0 | 2 |
| D | Keep on the card, then an MCC REBOOT | 0 | 0 | 0 | 0 | 6 |
| E | XVC W5 on nanosoc_ila | 0 | 0 | 0 | 0 | 6 |
| F | The hub SD door | 0 | 0 | 0 | 0 | 5 |
| G | Config SD A/B by pointer | 0 | 0 | 0 | 0 | 4 |
| Z | Close-out | 0 | 0 | 0 | 0 | 8 |

## Checks

| id | verdict | s | why |
|---|---|---|---|
| 0.2 | pass | 1.244 |  |
| 0.3 | pass | 3.021 |  |
| 0.4 | pass | 0.313 |  |
| A1 | stopped | 0.584 | A1: HELD (exit 4): mps3@192.168.11.101:6900 is in use — held by dam1n19 on srv03335 (pid 3115305, 22707s): harness-manager-daemon: harness-manager-ui. Another holder, or the reset guard's card job: the runner never forces |
| A2 | skipped | 0 | the run stopped at A1 |
| A3 | skipped | 0 | the run stopped at A1 |
| A4 | skipped | 0 | the run stopped at A1 |
| B1 | skipped | 0 | the run stopped at A1 |
| B2 | skipped | 0 | the run stopped at A1 |
| B3 | skipped | 0 | the run stopped at A1 |
| C1 | skipped | 0 | the run stopped at A1 |
| C2 | skipped | 0 | the run stopped at A1 |
| D1 | skipped | 0 | the run stopped at A1 |
| D2 | skipped | 0 | the run stopped at A1 |
| D3 | skipped | 0 | the run stopped at A1 |
| D4a | skipped | 0 | the run stopped at A1 |
| D4 | skipped | 0 | the run stopped at A1 |
| D5 | skipped | 0 | the run stopped at A1 |
| E1 | skipped | 0 | the run stopped at A1 |
| E1b | skipped | 0 | the run stopped at A1 |
| E2 | skipped | 0 | the run stopped at A1 |
| E3 | skipped | 0 | the run stopped at A1 |
| E4 | skipped | 0 | the run stopped at A1 |
| E5 | skipped | 0 | the run stopped at A1 |
| F1 | skipped | 0 | the run stopped at A1 |
| F2 | skipped | 0 | the run stopped at A1 |
| F3 | skipped | 0 | the run stopped at A1 |
| F4 | skipped | 0 | the run stopped at A1 |
| F6 | skipped | 0 | the run stopped at A1 |
| G1 | skipped | 0 | the run stopped at A1 |
| G2 | skipped | 0 | the run stopped at A1 |
| G3 | skipped | 0 | the run stopped at A1 |
| G4 | skipped | 0 | the run stopped at A1 |
| Z1 | skipped | 0 | the run stopped at A1 |
| Z2 | skipped | 0 | the run stopped at A1 |
| Z2b | skipped | 0 | the run stopped at A1 |
| Z2c | skipped | 0 | the run stopped at A1 |
| Z3 | skipped | 0 | the run stopped at A1 |
| Z4 | skipped | 0 | the run stopped at A1 |
| Z5 | skipped | 0 | the run stopped at A1 |
| Z6 | skipped | 0 | the run stopped at A1 |

## Manual (never unattended)

- **0.1** The evidence folder and the environment: david's setup before the run: env.sh (overlay dirs, hw_server), boards.toml, SSH-OK to the hub. The runner reads what it gives
