# HIL: the lab MPS3 through the hub (Thu 09-24 test, Fri 09-25 B0)

> **Friday 25 Sep, 09:00–10:00 is Harness Manager's slot** (bare-metal `0x72BB0A36`; agreed with the
> Linux lead at 03:05). B1 v4 starts at 10:00 on its own lease, so this slot must end with the board
> restored to greybox and the lease released by **09:55**.
>
> | Time | What |
> |---|---|
> | 09:00 | §0 checks, §1 lease (via Harness Manager, which uses `sg fpga`), §2 share, §3 app |
> | 09:10 | §4 read-only checks R1–R11 |
> | 09:35 | §5 write checks W1–W5 with david present, if R1–R11 passed |
> | 09:50 | §7 close-out: restore greybox, release the lease, send the evidence |
> | ~10:50 | §6 at the end of B1 v4, on the Linux lead's lease: Harness Manager against Linux (10 min) |

Copy-paste steps for running Harness Manager on srv03335 against the lab MPS3,
which only the hub `mapstone-dev.ecs.soton.ac.uk` can reach. Every check lists
the command, the expected answer and the evidence file to save.

**Rules for the whole window:**

1. The lease is **david's**. An agent never takes one.
2. **Never run `fpgahub share stop`.** It stops every share on the board,
   other people's consoles included. Harness Manager refuses to run it.
3. During B0 slot 1, **stay off `tty_02`**, because slot 2's console must be
   the first client there. The boards.toml below shares nothing.
4. **Never start an fpgahub share on `tty_00` (the MCC).** The paced REBOOT needs exactly
   one reader there, a share is a reader that only `share stop` removes (rule 2), and while
   one exists the platform's tools refuse to REBOOT. Harness Manager runs every MCC operation
   ON the hub through pyverify instead, and refuses `share start … mcc`.
5. UDP does not cross the SSH tunnel, so identify (6899) and TFTP (69) do not
   work from srv03335. Deploys push over 6910 TCP.

**The board as of 09-24** (from the ILA mint session's findings,
`FINDINGS_FOR_LINUX_HARNESS_2026-09-24.md` on the platform's `feat/rm-ila-mint`):

- **The fielded shell is `0x72BB0A36`** (boundary 47/148/20), running firmware
  v0.11 (`987cf264`) from the SD card. Expect `shell_id` `0x72bb0a36` in R1.
  `0x3f1a560f` is the previous board state, and its overlays do not load on this shell.
- **A new console client may first see stale output from the previous design.** The shell
  holds UART output while nobody reads 6930. Harness Manager drops its own scrollback
  on a verified swap, but it cannot drop what the shell still holds.
- **MCC commands run on the hub, paced at 100 ms a character**, the rate the remote REBOOT was
  proven at (pyverify's hub-side tools over `ssh mapstone-dev 'sg fpga -c …'`). Exactly one
  program may read `tty_00`, so close any other console on it: the tools refuse otherwise.
- **After a MicroBlaze warm restart** (watchdog, `reboot`), the partition stays in reset
  until the first swap, and `stats` may show `rm_ok: false`. That is a real state,
  not a Harness Manager fault.

How it works: `via = "ssh:mapstone-dev…"` in boards.toml makes the app run one
`ssh -N` to the hub, forwarding the shell's ports (6900, 6910, 6921,
6930–6932, 2542) to free local ports. It never binds local port 2542. The MCC
arrives as an fpgahub TTY share, reached through a second forward.

---

## 0. Once, before the window (10 min, no board)

1. Set the evidence folder:
   ```bash
   export EV=$HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil && mkdir -p $EV
   ```
2. Write `boards.toml`. The table key `lab` is a free name; `match` ties it to
   the board's address.
   ```bash
   mkdir -p ~/.config/harness-manager && cat > ~/.config/harness-manager/boards.toml <<'EOF'
   [boards.lab]
   match = ["192.168.10.101"]
   via = "ssh:mapstone-dev.ecs.soton.ac.uk"
   hub = { host = "mapstone-dev.ecs.soton.ac.uk", target = "mps3_01_pl" }
   EOF
   ```
3. Point the app at OpenOCD, which is not on PATH on srv03335:
   ```bash
   export HARNESS_MANAGER_OPENOCD=$HOME/SoCLabs/soclabs-openocd/install/bin/openocd
   export HARNESS_MANAGER_MPS3_OPENOCD_DIR=$HOME/SoCLabs/mps3-nanosoc-platform/host/openocd
   ```
4. Check that ssh to the hub works without a prompt. The tunnel uses
   `BatchMode`, so a prompt would fail it.
   ```bash
   ssh -o BatchMode=yes -o ControlPath=none -o ClearAllForwardings=yes mapstone-dev.ecs.soton.ac.uk true && echo SSH-OK
   ```
   Expected: `SSH-OK`.
   - Your `~/.ssh/config` gives mapstone-dev `LocalForward 18081/18082`, and
     your ControlMaster already holds those ports.
   - The tunnel therefore runs with a copy of your config that leaves them out:
     `~/.config/harness-manager/tunnel/ssh_config.*`, rewritten on every start.
   - `ClearAllForwardings` cannot do this job, because it also drops the
     tunnel's own `-L`.

---

## 1. Take the lease (david, ~1 min, may queue)

**Thursday:** take it through Harness Manager, so the app heartbeats it while
the board is open.
```bash
harness-manager lease acquire 192.168.10.101 --ttl 7200 --holder david-hm | tee $EV/t1_lease_acquire.txt
```
- **Expected:** `mps3_01_pl on mapstone-dev.ecs.soton.ac.uk: held by david-hm until <time>`.
- **If someone holds it**, stderr shows `queued at position N …` and the
  command waits. Ctrl-C leaves the queue cleanly.
- The token stays in `~/.config/harness-manager/leases/` (mode 0600). It is
  never printed.

**Friday 09:00:** the same command as Thursday, with `--ttl 3600`. Harness Manager
runs fpgahub under `sg fpga`, so the lease belongs to you, not to root.
- **Never take it with `sudo`.** A sudo lease belongs to `root@mapstone-dev`: agents
  cannot renew it, and Harness Manager treats it as someone else's lease (force-release
  would then ask for the board name, D12).
- If the Linux lead still holds it at 09:00, use the lease panel's **Request** (or
  `harness-manager lease request 192.168.10.101 --message "HM slot 09:00"`) rather than
  forcing it.

Check it either way:
```bash
harness-manager lease show 192.168.10.101 | tee $EV/t1_lease_show.txt
```

---

## 2. Check nothing reads the MCC console (`tty_00`)

Nothing to start: the MCC is reached ON the hub. Check it is free:
```bash
ssh mapstone-dev.ecs.soton.ac.uk 'ps -eo pid,user,args | grep -F tty_00 | grep -v grep; echo END' | tee $EV/t2_tty00.txt
```
- **Expected:** only `END`. Any line is another reader (a `cat`, a socat, an fpgahub share on
  `tty_00`): Harness Manager's MCC checks will refuse, naming it. Ask its owner to close it; a
  share goes only with `share stop`, which stops every share (rule 2), so that is david's call.

**Optional, Thursday only (never during B0 slot 1): the shell console.** Add
lane 2 to boards.toml's hub table:
```
shares = { fpga_uart2 = "/dev/mps3_01_pl/tty_02" }
```
then run `harness-manager share start 192.168.10.101 fpga_uart2`.
- It appears as console `fpga_uart2` (alias `shell`), at a rate the share sets.
- Whoever connects to `tty_02` first owns its write slot.

---

## 3. Start the app

Terminal A (leave it running):
```bash
harness-manager app
```
1. In the window, look for boards. The default probe tries `192.168.10.101`,
   and boards.toml routes it through the hub.
2. Open the board.

**Expected on the Overview:**
- the header shows the shell id and the design;
- the Ethernet link reads `… via ssh:mapstone-dev.ecs.soton.ac.uk`;
- the MCC link reads `the MCC console /dev/mps3_01_pl/tty_00, reached ON the hub …`.

Evidence of the tunnel (terminal B):
```bash
pgrep -af -- '-N -T' | grep mapstone | tee $EV/t3_tunnel_ps.txt
```
- **Expected:** one `ssh … -o ControlPath=none … -o ExitOnForwardFailure=yes … -L 127.0.0.1:<n>:192.168.10.101:6900 … mapstone-dev.ecs.soton.ac.uk` line.
- No forward uses local port `2542`.
- The MCC check (R4) adds no forward: it runs on the hub over a one-shot ssh.

From here on, CLI commands go through the app's service and share its session.
The service reads the step 0.3 and 5.1 variables when it starts: if it was
already running before you set them, run `harness-manager daemon stop` and
start the app again.

---

## 4. Read-only checks (Thursday; B0 slot 1)

Work down the list and save each file.

| # | Check | Command (terminal B) | Expected | Evidence |
|---|---|---|---|---|
| R1 | Identity and health | `harness-manager --json info 192.168.10.101 \| tee $EV/r1_info.json` | `shell_id` `0x72bb0a36`, `harness_version` `1.0.0` (every firmware since v0.8 says 1.0.0; Harness Manager names the release by its firmware sha instead), `health.reachable` true; `capabilities` include `console_dut`, `console_controller`, `telemetry_temp` | `r1_info.json` |
| R2 | A console in the GUI | Consoles → `uart0` → Open | state `up`. Silent on greybox (no DUT UART); nanosoc prints its banner after a DUT reset | screenshot `r2_console_gui.png` |
| R3 | The same console in `screen` | `harness-manager pty 192.168.10.101 uart0` (lane L2's verb) prints the path, then `screen <path>` | the same bytes as the GUI, at the same time; leave `screen` with `Ctrl-a k` | `r3_screen.txt` (paste) |
| R4 | MCC temperature, read on the hub | `harness-manager --json mcc 192.168.10.101 temp \| tee $EV/r4_mcc_temp.json` | `mcc_temp` about 35 degC, `source` `mcc-console (hub)`; takes ~5 s (paced at 100 ms/char) | `r4_mcc_temp.json` |
| R5 | Debug detect | `harness-manager --json debug detect 192.168.10.101 \| tee $EV/r5_debug_detect.json` | on greybox: exit 13, `the loaded design (greybox) has no debug port` (correct: there is nothing to detect); on nanosoc: `idcode` `0x6ba00477` over remote_bitbang through the tunnel | `r5_debug_detect.json` |
| R6 | The lease | `harness-manager --json lease show 192.168.10.101 \| tee $EV/r6_lease.json` | `holder` `david-hm`, `mine` true, `holder_kind` `hm`; `notes_supported` and `can_revoke` true (SSH hub) | `r6_lease.json` |
| R7 | Front panel on bare metal | `harness-manager panel show 192.168.10.101 \| tee $EV/r7_panel.txt`, then `harness-manager identify 192.168.10.101; echo rc=$?` | `source rebuilt from what Harness Manager read`; the owner (harness or DUT). Identify: `unavailable — needs harness feature 'locate' (Linux harness)`, rc=12, and the backlight does **not** blink. In the app: the Board tile's Panel line and the Details mirror say "rebuilt" | `r7_panel.txt` |
| R8 | XDC for the fielded static | Board & XDC section → "RM kit" → Preview; or `harness-manager xdc rm-kit --design nanosoc --out $EV/xdc` | the model card says the board **runs** the model's static `0x72BB0A36`; the preview has no failed check | `xdc/`, screenshot `r8_xdc.png` |
| R9 | Build guide against the board | Build section (between XDC and Program) | the Target step is **done** for `0x72BB0A36`; Kit is **next** (no kit cached yet) or done; Tools shows the Vivado needed (2024.1) | screenshot `r9_build.png` |
| R10 | XVC status (read-only) | `harness-manager xvc status 192.168.10.101 \| tee $EV/r10_xvc.txt` | `down`; the scope line (reconfigurable partition only, never whole-device JTAG); the bare-metal warning (unauthenticated until cutover) | `r10_xvc.txt` |
| R11 | Harness versions (no releases yet; skip if `harness` is not yet a command) | `harness-manager harness list 192.168.10.101 2>&1 \| tee $EV/r11_harness.txt; echo rc=$?` | the board line (`runs … shell 0x72bb0a36, fw <sha>`) and a clear "no channel/release published" message, not a traceback. Nothing is installed | `r11_harness.txt` |

**Exit condition:** nothing was written. R1 run again shows the same `rm_id`.

---

## 5. Write checks (only with david present; 09:35–09:50)

Stop at 09:50 whatever is left: W4 (restore greybox) must run before the close-out.

1. **Find the board's overlays.** Their `static_id` must match R1's `shell_id`.
   - `0x3f1a560f`:
     ```bash
     export HARNESS_MANAGER_MPS3_OVERLAY_DIRS=$HOME/SoCLabs/mps3-nanosoc-platform/fpga/dfx/overlay
     ```
   - `0x72bb0a36` (the ILA mint): copy the mint's re-keyed overlays first. They
     live on the hub (W1 runbook §0):
     ```bash
     scp -rq mapstone-dev.ecs.soton.ac.uk:/home/david/mint_72BB0A36/prod/overlay $HOME/mint_72BB0A36_overlay
     export HARNESS_MANAGER_MPS3_OVERLAY_DIRS=$HOME/mint_72BB0A36_overlay
     ```
   - Restart the app (step 3) so it sees the variable.
2. **Run the checks:**

| # | Check | Command | Expected | Evidence |
|---|---|---|---|---|
| W1 | Program nanosoc | `harness-manager program 192.168.10.101 nanosoc \| tee $EV/w1_program.txt` (it shows the preflight and asks to confirm) | every preflight item ok or unchecked; the result line ends `via tcp+windowed; verified` (6910 through the tunnel, never TFTP); `rm_id` `0x01000001`; about 5–15 s | `w1_program.txt` |
| W2 | A console | `screen <path from R3>`, then Reset DUT in the GUI | the nanosoc boot banner; typed characters reach the DUT (paced 20 ms/char) | `w2_console.txt` (paste) |
| W3 | Debug up + gdb | terminal B: `harness-manager debug up 192.168.10.101` (holds; Ctrl-C ends it). Terminal C, with the gdb port it printed (a block from 23300 up): `arm-none-eabi-gdb -batch -ex 'target extended-remote localhost:<gdb port>' -ex 'info registers pc' -ex detach \| tee $EV/w3_gdb.txt` | `debug up` shows state `up` and its ports; gdb prints a `pc` value and detaches | `w3_gdb.txt` |
| W5 | XVC session (optional, while nanosoc_ila is loaded) | `harness-manager program 192.168.10.101 nanosoc_ila`, then `harness-manager xvc open 192.168.10.101 --for 5 \| tee $EV/w5_xvc.txt`; in Vivado 2024.1 paste the Tcl it prints; then `harness-manager xvc close 192.168.10.101` | `ready`; Vivado's hardware manager shows `debug_bridge` (IDCODE `0x0a003093`) and the RM's ILA with the `.ltx` loaded. **Also check** hw_server lists no local USB cable (the partition-only rule) | `w5_xvc.txt`, screenshot `w5_vivado.png` |
| W4 | Restore greybox (always, last) | Ctrl-C the `debug up` / `xvc close` first, then `harness-manager restore 192.168.10.101 \| tee $EV/w4_restore.txt` | `verified`, `rm_id` `0x00000000` | `w4_restore.txt` |

---

## 6. At the end of B1 v4: Harness Manager on the Linux harness (~10 min, ~10:50)

After the cutover, the Linux harness has its own runbook: [HIL_LINUX.md](HIL_LINUX.md).

Only if B1 v4 reached step (e) (SSH to the board). This runs on the **Linux lead's lease**: do
not take or request a lease. The board runs the P-mint static `0x61BC6789` with harnessd.

| # | Check | Command | Expected | Evidence |
|---|---|---|---|---|
| S3.1 | Identity | `harness-manager --json info 192.168.10.101 \| tee $EV/s3_info.json` | exit 0; `shell_id` `0x61bc6789`; `impl` `linux`; `features` listed; `health.reachable` true | `s3_info.json` |
| S3.2 | Front panel | `harness-manager panel show 192.168.10.101 \| tee $EV/s3_panel.txt` | the harnessd build has no `hello`/`panel`/`locate` yet (requests R1–R3 come after cutover), so the rebuilt view with the reason; touch fields shown if `stats` carries `touch_ok` | `s3_panel.txt` |
| S3.3 | XVC status | `harness-manager xvc status 192.168.10.101 \| tee $EV/s3_xvc.txt` | `down`; the scope line; the unauthenticated warning (no `xvc_lock` feature yet) | `s3_xvc.txt` |
| S3.4 | CLCD finger test (CLCD-HM R6, with the Linux lead) | hold a finger on the panel for 10 s while `harness-manager info` runs twice | both `info` calls answer (a held touch must not starve the network); record `touch_ok`, `touch_bus_lost`, `touch_recoveries` from `stats` before and after | `s3_touch.txt` |

---

## 7. Close-out

1. Close the board in the app (or `harness-manager daemon stop`). The tunnel
   closes with it.
   ```bash
   pgrep -af -- '-N -T' | grep mapstone || echo NO-TUNNEL | tee $EV/c1_tunnel_closed.txt
   ```
   Expected: `NO-TUNNEL`.
2. **Release the lease by 09:55** (B1 v4 takes the board at 10:00).
   ```bash
   harness-manager lease release 192.168.10.101 | tee $EV/c2_release.txt
   harness-manager lease show 192.168.10.101 | tee -a $EV/c2_release.txt
   ```
   Expected: `released`, then `not leased`.
3. Leave any lane shares running. Never `share stop` (rule 2).

---

## If something fails

| What you see | Cause | Do this |
|---|---|---|
| `the SSH tunnel to mapstone-dev… did not come up: ssh exited with status 255 (…Permission denied (publickey))` | no usable key without a prompt | re-run step 0.4; load the key (`ssh-add`) |
| `your ssh config gives mapstone-dev… port forwards the tunnel cannot leave out` | a `LocalForward` in an `Include`d file | move it into `~/.ssh/config` itself (the tunnel comments it out there) |
| R4: `another process reads the MCC console /dev/mps3_01_pl/tty_00 … (pid N: …)` | another reader on `tty_00` (a `cat`, an fpgahub share) | step 2; have it closed, repeat R4 |
| an MCC REBOOT: `no Python 3.10+ for pyverify's MCC tools` | the hub has no `python3.11` (its `python3` is 3.6; reads run on 3.6, a REBOOT does not) | the hub admin installs one; fpgahub's `/opt/fpgahub/bin/python3.11` also counts |
| R1: `offline`, "the hub … could not reach the shell (channel N: open failed …)" | the hub reached no shell: board off, rebooting, or harness down | check from the hub: the B0 runbook's S2.3 ping block |
| R1: `busy`, "another client holds the control channel" | a real second client on 6900 (a pyverify run, the hub's poller) | wait, or close the other client |
| `lease show` exits 7, "your account on the hub needs the 'fpga' group" | fpgahub 0.3.0's socket group | `ssh mapstone-dev… 'id -nG'` must list `fpga` |
| a probe finds nothing | boards.toml missing or `match` wrong; UDP discovery never crosses the tunnel | check `~/.config/harness-manager/boards.toml`; open by address |
| `ssh … -N -T` processes left after the app was killed (not closed) | a hard kill skips the tunnel's close | start the app again: from Q2 (`team/q2-robust`) the service stops a killed owner's tunnels, OpenOCD and PTY links when it starts, and says so in `daemon.log`. On an older build: `pkill -f -- '-N -T .*mapstone-dev'` (only Harness Manager's tunnels run with `-N -T`) |

**Send back:** the whole `$EV` folder, plus any screenshots. The lead forwards the result to the
platform-guide session, which switches Harness Manager's write paths from "virtual board only" to
"proven on the board".
