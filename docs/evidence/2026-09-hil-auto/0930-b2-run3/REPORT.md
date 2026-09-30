# HIL-AUTO: linux-nocard on 192.168.11.101:6900, 13 of 30 iterations

- **PASS** (exit 0)
- HM cda3667829c5 (the Harness Manager checkout that ran)
- plan `linux-nocard` (docs/HIL_LINUX.md), `--writes safe`, every 1800 s
- ran 2026-09-30T00:49:05+01:00 → 2026-09-30T08:05:16+01:00
- partition: on greybox at the end (read back)
- SSH claim: mine at the start, mine at the end (the runner never runs `board claim`; Harness Manager has no verb to unclaim (mps3-unclaim on the board's serial console is the only way))
- ended early: the deadline (2026-09-30T08:05:00+01:00)

## Iterations

| # | result | pass | fail | skipped | first failure |
|---|---|---|---|---|---|
| [1](iter-001/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [2](iter-002/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [3](iter-003/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [4](iter-004/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [5](iter-005/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [6](iter-006/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [7](iter-007/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [8](iter-008/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [9](iter-009/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [10](iter-010/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [11](iter-011/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [12](iter-012/REPORT.md) | PASS | 17 | 0 | 12 |  |
| [13](iter-013/REPORT.md) | PASS | 17 | 0 | 12 |  |
