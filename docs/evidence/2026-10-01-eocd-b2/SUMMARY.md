# E-OCD on board 2: OpenOCD on the board (HIL_LINUX §E-OCD), Thu 1 Oct 2026 16:28-16:49

- **Board:** mps3_02_pl, 192.168.11.101. Linux v2.0.0-rc1 netboot image rc2_v7n_rc (harness sha 1e504993), static 0x44EE76D5.
- **Harness Manager:** main f328ad7 (0.1.0). The service was restarted 16:21 onto this code.
- **Lease:** david@mapstone-dev, taken in the app.
- **Launcher:** mps3-debug/1 1.0.0, OpenOCD 0.12.0+mps3 (remote_bitbang to 6921).
- **Path:** debug.on_board = auto (default), which picked the board.
- **Evidence:** this folder.

| Step | Result | Notes |
|---|---|---|
| Re-pin | PASS | host key SHA256:DPgz3qD1Nn5… pinned and checked against the board (`0_claim_repin.txt`) |
| OCD0 | PASS | launcher version + status JSON (state down, 14 designs); `program nanosoc` 64 s, verified |
| OCD1 | PASS | `debug up`: where = the board, one gdb line, no telnet/tcl line; NO-LOCAL-OPENOCD |
| OCD2 | PASS | idcode 0x6ba00477 (13 s); board: state up, design nanosoc, tap 0x6ba00477, cpu0 on 3333; local forward on 127.0.0.1 only; no 4444/6666 here |
| OCD3 | PASS | halt + full register dump, 49 s. The DUT was in HardFault lockup (pc 0xfffffffe, IPSR 3): firmware state, not HM |
| OCD4 | PASS (1 KiB) | save → pattern → read back → restore → resume: RAM-OK, 213 s for 4 × 1 KiB; then DUT reset done. Sized 1 KiB for the slower on-board path |
| OCD5 | PASS | Ctrl-C closed it in 18 s; status down on both ends ("reason":"closed"); claim kept |
| OCD6 | SKIPPED | optional twin; needs a temporary change to david's settings file (not touched). Covered by tests |
| OCD7 | PASS | swap to nanosoc_multicore with debug up: 79.6 s verified; OpenOCD closed and reopened (pid 762 → 775); two gdb lines cpu0/cpu1; board shows design nanosoc_multicore, cores 3333 + 3334 |
| OCD8 | cfg UNPROVEN (Linux lead) | IDCODE OK. cpu1 halts, pc 0x08000528 sp 0x18003bc0 ("Cortex-M0+ r0p1"). cpu0: the board's OpenOCD refused it: `Cortex-M PARTNO 0x0 is unrecognized` on AP 0 in nanosoc_mps3_multicore_jtag.cfg:63 (`openocd.log`). HM's forward reached the board; not an HM fault. RAM round trip on core 0 not run |
| OCD9 | PASS | down in 18 s; `restore` → greybox 40.6 s, verified |

## Findings

**For the Linux lead:**
1. **The multicore cfg, AP 0:** cpu0 reads PARTNO 0x0. Check the AP index and the core 0 reset state in the 2-AP cfg.
2. **The board clock:** `started_at` reads 1970-01-01 (no RTC/NTP on the board).
3. **Plain nanosoc** sits in HardFault lockup after programming on board 2 (its SST26 holds MicroPython).

**For HM (small):**
1. A held `debug up` terminal doesn't print the new ports after a swap reopens the session; `debug status` does.
2. `debug status` with where = board still prints this PC's `openocd …` line, which is confusing.
3. `board ssh -c` splits the words: FIX-PACK-6 item 2, not on main yet.

## Timing on the board path (rc image)

- halt + registers: 49 s;
- 1 KiB × 4 transfers: 213 s (~19 B/s);
- swap with debug up: 101 s;
- close: 18 s.
