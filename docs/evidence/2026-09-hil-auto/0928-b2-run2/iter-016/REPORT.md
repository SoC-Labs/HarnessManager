# HIL-AUTO: linux-nocard on 192.168.11.101

- **PASS** (exit 0)
- plan `linux-nocard` (docs/HIL_LINUX.md), `--writes safe`, static `0x44ee76d5`, iteration 16 of 30
- ran 2026-09-29T05:56:01+01:00 → 2026-09-29T06:00:54+01:00

## Sections

| § | section | pass | fail | stopped | manual | skipped |
|---|---|---|---|---|---|---|
| 0 | Setup | 3 | 0 | 0 | 1 | 0 |
| A | The Linux harness through Harness Manager | 3 | 0 | 0 | 1 | 0 |
| B | The SSH claim: check it, adopt it, never re-claim | 2 | 0 | 0 | 1 | 0 |
| C | OS slots and the user microSD (read) | 1 | 0 | 0 | 0 | 1 |
| D | Keep on the card, then an MCC REBOOT | 2 | 0 | 0 | 0 | 4 |
| E | XVC W5 on nanosoc_ila | 2 | 0 | 0 | 4 | 0 |
| F | The hub SD door | 0 | 0 | 0 | 4 | 1 |
| G | Config SD A/B by pointer | 0 | 0 | 0 | 0 | 4 |
| Z | Close-out | 2 | 0 | 0 | 4 | 2 |

## Checks

| id | verdict | s | why |
|---|---|---|---|
| 0.2 | pass | 1.122 |  |
| 0.3 | pass | 2.646 |  |
| 0.4 | pass | 0.296 |  |
| A1 | pass | 1.704 |  |
| A2 | pass | 1.05 |  |
| A3 | pass | 1.037 |  |
| B1 | pass | 1.464 |  |
| B3 | pass | 22.545 |  |
| C1 | pass | 1.074 |  |
| C2 | skipped | 0 | no user microSD (HIL_LINUX.md, Card-less mode 1) |
| D1 | pass | 3.14 |  |
| D2 | skipped | 0 | no user microSD (HIL_LINUX.md, Card-less mode 1) |
| D3 | skipped | 0 | no user microSD (HIL_LINUX.md, Card-less mode 1) |
| D4a | pass | 7.273 |  |
| D4 | skipped | 0 | no user microSD (Card-less mode 1): nothing to boot from the card, and no MCC REBOOT on a card-less board (a failed cold boot needs a person at PB0) |
| D5 | skipped | 0 | no user microSD (HIL_LINUX.md, Card-less mode 1) |
| E1 | pass | 80.575 |  |
| E1b | pass | 1.655 |  |
| F6 | skipped | 0 | no user microSD (Card-less mode 1): F6 writes the config SD, then REBOOTs: no reset of any kind on a card-less board |
| G1 | skipped | 0 | no user microSD (Card-less mode 1): §G writes the config SD and ends in an MCC REBOOT, and a card-less board gets no reset of any kind |
| G2 | skipped | 0 | no user microSD (Card-less mode 1): §G writes the config SD and ends in an MCC REBOOT, and a card-less board gets no reset of any kind |
| G3 | skipped | 0 | no user microSD (Card-less mode 1): §G writes the config SD and ends in an MCC REBOOT, and a card-less board gets no reset of any kind |
| G4 | skipped | 0 | no user microSD (Card-less mode 1): §G writes the config SD and ends in an MCC REBOOT, and a card-less board gets no reset of any kind |
| Z1 | skipped | 0 | no user microSD (HIL_LINUX.md, Card-less mode 1) |
| Z2 | pass | 46.972 |  |
| Z2b | pass | 1.687 |  |
| Z2c | skipped | 0 | no user microSD (HIL_LINUX.md, Card-less mode 1) |

## Manual (never unattended)

- **0.1** The evidence folder and the environment: david's setup before the run: env.sh (overlay dirs, hw_server), boards.toml, SSH-OK to the hub. The runner reads what it gives
- **A4** The finger test: a person holds a finger on the panel for 10 s
- **B2** Adopt the claim: a trust decision: it pins the board's host key in boards.toml and asks `y`
- **E2** Open XVC: the XVC session is for E4's Vivado; it needs B2's adopt
- **E3** The Tcl and the hw_server: follows E2
- **E4** Vivado 2026.1: a person reads get_hw_targets/get_hw_ilas in Vivado
- **E5** Close XVC: follows E2
- **F1** How the daemon is sandboxed: raw ssh on the hub, not an HM verb
- **F2** The hub offers an sd method: raw fpgahub on the hub, not an HM verb
- **F3** Stage the board's own base image: writes the hub user's cache over raw ssh, from the board's row of F3's table
- **F4** The read probe: raw fpgahub on the hub, not an HM verb
- **Z3** Close the board; no tunnel left: david closes the app; `pgrep -af -- '-N -T'` on srv03335
- **Z4** Release the lease: the runner never releases the lease: david does, after reading REPORT.md
- **Z5** Tell the Linux lead and the guide session: a message from david
- **Z6** Send the evidence: the runner writes summary.json and REPORT.md; david sends the folder
