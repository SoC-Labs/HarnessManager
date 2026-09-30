# HIL-AUTO: linux-nocard on 192.168.11.101:6900, 5 of 21 iterations

- **STOPPED: asked to stop** (Stop in the app, or a signal: it finished the check and put the end state back; exit 2)
- HM ab311b4c96cc (the Harness Manager checkout that ran)
- plan `linux-nocard` (docs/HIL_LINUX.md), `--writes safe`, every 1800 s
- ran 2026-09-29T22:16:27+01:00 → 2026-09-30T00:46:17+01:00
- partition: on greybox at the end (read back)
- SSH claim: mine at the start, mine at the end (the runner never runs `board claim`; Harness Manager has no verb to unclaim (mps3-unclaim on the board's serial console is the only way))
- ended early: interrupted by a signal (Stop in the app)

## Iterations

| # | result | pass | fail | skipped | first failure |
|---|---|---|---|---|---|
| [1](iter-001/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [2](iter-002/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [3](iter-003/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [4](iter-004/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [5](iter-005/REPORT.md) | PASS | 17 | 0 | 12 |  |
