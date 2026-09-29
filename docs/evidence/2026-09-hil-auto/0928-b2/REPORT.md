# HIL-AUTO: linux-nocard on 192.168.11.101, 1 of 30 iterations

- **STOPPED for safety: A1: HELD (exit 4): mps3@192.168.11.101:6900 is in use — held by dam1n19 on srv03335 (pid 3115305, 22707s): harness-manager-daemon: harness-manager-ui. Another holder, or the reset guard's card job: the runner never forces** (exit 2)
- plan `linux-nocard` (docs/HIL_LINUX.md), `--writes safe`, every 1800 s
- ran 2026-09-28T21:08:21+01:00 → 2026-09-28T21:09:27+01:00
- partition: not needed: the runner did not swap the partition
- SSH claim: None at the start, None at the end (the runner never runs `board claim`; Harness Manager has no verb to unclaim (mps3-unclaim on the board's serial console is the only way))

## Iterations

| # | result | pass | fail | skipped | first failure |
|---|---|---|---|---|---|
| [1](iter-001/REPORT.md) | STOPPED | 3 | 1 | 37 | A1: A1: HELD (exit 4): mps3@192.168.11.101:6900 is in use — held by dam1n19 on srv03335 (pid 3115305, 22707s): harness-manager-daemon: harness-manager-ui. Another holder, or the reset guard's card job: the runner never forces |
