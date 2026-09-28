# Harness Manager on silicon: 27–28 Sep 2026

The first board sessions of Harness Manager (HM) against the MPS3 Linux harness. Every result
below ran on a real board through the lab hub `mapstone-dev`, with david holding the lease through
HM. The IDs are those of [`docs/HIL_LINUX.md`](../../HIL_LINUX.md).

| Board | Image | Reached as | HM commit |
|---|---|---|---|
| board 1 `mps3_01_pl` | rc2_v4n (netboot, morning), rc2_v4 from card slot B (~09:50), rc2_v5n (netboot, 11:30 visit) | 192.168.10.101 via hub | main `36b12df` |
| board 2 `mps3_02_pl` | rc2_v6n (netboot, no card) | 192.168.11.101 via hub | main `623271b` |

Static on both: RC2 `0x44EE76D5`.

## Passed on the board

| ID | What | Board | Evidence |
|---|---|---|---|
| B1 | `board claim --adopt --replace-host-key`; claim-status "claimed by you", host key pinned, claim key from identify `ssh.key_sha256` | 1 | `b1_claim_status.txt` |
| B2 | Re-adopt after the card boot: the new permanent host key is pinned | 1 | `b2_claim_status.txt` |
| A1 | Identity: `linux`, shell `0x44ee76d5`, ver32 `0x01000000`; features include `usd`, `slot`, `xvc_lock` | 1, 2 | `a1_info_2.json`, `lm_info.json` |
| A2 | Front panel: the rebuilt mirror, touch ok (the panel wording is being corrected: lane PANEL-TRUTH) | 1 | `a2_panel.txt` |
| A3 | XVC status: down, scoped to the RP debug chain | 1 | `a3_xvc.txt` |
| D4a | MCC temperature read ON the hub (no fpgahub share on tty_00): 35.0 °C (board 1), 37.4 °C (board 2) | 1, 2 | `d4_mcc_temp.json` |
| — | The hub MCC one-reader refusal (27 Sep 22:03: another process named tty_00 on its command line; nothing typed) | 1 | `d4_mcc_temp_first_refused_2026-09-27.json` |
| — | MCC research: `HELP` lists READ_AXI/WRITE_AXI (run mode); `CFG R TEMP 1..7` → "ERROR: Undefined" (only TEMP 0 answers) | 1 | `mcc_*.txt` |
| C1 | Slot status: running B, default B, B valid "booted, confirmed healthy", A empty, `claimed`/`confirmed` fields present | 1 | `c1_slot_status.json` |
| C2 | Card status: D13 store present, empty, 15193 MB, committable | 1 | `c2_card_status.json` |
| LM | **Live LCD mirror first light**: live, mode sw, owner harness (board 1 11:34, board 2 14:36) | 1, 2 | `first_light_2x.png`, `board2_first_light_2x.png`, `lm_*.txt`, `b2_snap.txt` |
| D1 | Overlay list: 13 overlays load on `0x44ee76d5` (after SERIAL-6900) | 2 | `b2_overlays.txt` |
| E1 | `program nanosoc_ila`: 2.8 MB over TCP in 78.8 s, verified | 2 | `b2_e1_program_ila.txt` |
| — | `program nanosoc`: 73.2 s, verified; `program greybox` (restore): 38.7 s, verified | 2 | `b2_dbg_program_nanosoc.txt` |
| E2 | `xvc open` over the claim forward: ready in 22 s, board XVC slot ours (daemon log) | 2 | `b2_e2_xvc_open.txt`, `b2_e3_xvc_status.txt` |
| debug | `debug up` with xPack OpenOCD 0.12 over the claim forward: gdb 127.0.0.1:23344 | 2 | `b2_dbg_up2.txt` |

## Failed, then fixed on main

| ID | What happened | Fix |
|---|---|---|
| D1, D2 (board 1, 09:53–10:19) | HM's own back-to-back 6900 connections were refused: harnessd accepts before it reaps the old client. D2's reset guard then refused the deploy **safely** (nothing written) | SERIAL-6900 (main `5b2ed04`): per-board gate + ghost retry; proven on board 2 (D1, E1 above) |
| `debug up` (board 2, 14:43) | The service ran with a stale `HARNESS_MANAGER_OPENOCD` (a SoC Labs build without remote_bitbang); DEBUG-OCD refused it up front, as designed | environment corrected; passed at 14:48 |
| Live display (board 1, 11:33) | Refused once: the hub's sshd reset HM's `lease show` (MaxStartups); the first snapshot's 10 s wait was too short for a cold SSH forward | FIX-PACK-1 (main `623271b`): hub citizen (cap, single-flight, one retry), 30 s snapshot wait |

## Not run (and why)

- **D2–D5 keep-on-card + MCC REBOOT**: a stage0 cold-start bug (DDR calibration during MCC clock setup) forbids MCC REBOOT/power-on on both boards until the fix is fielded (board 1, 28 Sep evening).
- **E3–E5 (Vivado ILA view over XVC)**: manual Vivado step, not done yet.
- **A4 finger test, F (hub SD door), G (config SD A/B), Z close-out**: time; F and G write SDs.
- **Board 2 slot/card**: no user microSD on board 2.

## Found on the board (fixes in progress)

- Board 2 runs with board 1's identity (LCD `MPS3-01`, `NET 192.168.10.101`, MAC `02:00:00:4D:50:53`): the Linux image hard-codes them. HM detection and fix: lane BOARD-ID; board side: the Linux lead's `identity` verb (rc2_v7).
- The hub's `mps3_01_pl` `board_mac` is the hub adapter's MAC, not the board's.
