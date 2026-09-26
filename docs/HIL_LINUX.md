# HIL: Harness Manager on the Linux harness (after cutover, ~70 min)

> **When:** after the Linux soak and the cutover, when the Linux lead says the board is free
> (about **Sun 27 Sep 20:00** or later). **Who:** david, at srv03335, alone. No agent takes the
> lease or touches the board.
>
> | Start | Section | Min | Writes? |
> |---|---|---|---|
> | T+0 | §0 setup: evidence folder, announce, lease, restart the app | 8 | no |
> | T+8 | §A the Linux harness through Harness Manager: info, panel, XVC status, finger test | 7 | no |
> | T+15 | §B the SSH claim: check it, adopt it (never re-claim) | 4 | boards.toml only |
> | T+19 | §C OS slots and the user microSD (read) | 2 | no |
> | T+21 | §D Keep on the card, then an MCC REBOOT | 12 | **user microSD** |
> | T+33 | §E XVC W5 on `nanosoc_ila` in Vivado 2026.1 | 10 | no |
> | T+43 | §F hub SD door: can fpgahubd read what Harness Manager stages? | 12 | hub cache only (F6 opt-in: **config SD**) |
> | T+55 | §G config SD A/B by pointer (opt-in, only if §F passed) | 10 | **config SD** |
> | T+65 | §Z close-out: card default back, greybox, lease, tell people, evidence | 5 | **user microSD** |
>
> Out of time? Skip §G, then F6. Never skip §Z.

Every step lists the command, the expected answer and the evidence file (under `$EV`).
Commands run in **terminal B** unless a step says otherwise.

**Rules for the whole window:**

1. **The lease is david's, taken through Harness Manager** (`sg fpga` on the hub). Never
   `sudo` for a lease, never `lease force`.
2. **Never `fpgahub share stop`.** It stops every share on the board. Harness Manager has no stop.
3. **`tty_00` (the MCC) has exactly one reader.** A second reader splits the REBOOT into a no-op.
   Never open `tty_02` (the Linux console).
4. **A hub SD write (`--method sd`) always hits the client timeout (~30 s; the 12 MB write takes
   ~68 s). That is not a failure.** Never retry it and never reset the board mid-write. Wait
   5 min, then read the hub journal (the failure table says how).
5. **Never write the DUT's SST26 flash.** Nothing below does. Since 09-24 it holds MicroPython, so
   nanosoc boots that, not hello.
6. **After a warm restart of the harness, the partition stays in reset until the first swap**
   (`stats` `rm_ok: false`). That is real, not a Harness Manager fault: program once.
7. Steps that write are marked **WRITES**, with what they write.

**The board (from the Linux runbooks, B2 §RC2, CUT C1):**

- **Static:** RC2 `0x44EE76D5`, UserID `0xFB1F8C76`, USR_ACCESS `0x01000000`, the Linux harness
  (harnessd, `impl: linux`). The base image on the config SD is `config_rm_greybox_stage0.bit`,
  sha256 `8a30ade887b1…` (full hash in §F3).
- **Address:** `192.168.10.101`. Only the hub `mapstone-dev.ecs.soton.ac.uk` reaches it (fpgahub
  target `mps3_01_pl`), so Harness Manager tunnels through the hub (boards.toml `via`).
- **Overlays:** keyed to `0x44EE76D5`, in the RC2 build `…/build_mint3_rc2_linux/overlay_mbv`:
  - `greybox` `0x00000000`;
  - `nanosoc` `0x01000001`;
  - `nanosoc_ila` `0x0100000A` (carries `nanosoc_ila.ltx`).
- **SSH:** the RC2 claim authorised two keys: your srv03335 `~/.ssh/id_ed25519.pub` and the hub's
  soak key (B2 P3). Your `~/.ssh/config` alias `mps3-b2` reaches the board.
- **The user microSD:** OS slots A/B (expect running A, default A) and the overlay store. B2
  cleared the store's power-on default after its test, so expect none unless the Linux lead set one.
- **Vivado:** the RC2 mint and its ILAs are 2026.1:
  `/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/`.

---

## 0. Setup (8 min, no board writes)

### 0.1 The evidence folder and the environment (2 min)

Paste in terminal B:
```bash
mkdir -p $HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux
cat > $HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh <<'EOF'
export EV=$HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux
export B=192.168.10.101
export H=mapstone-dev.ecs.soton.ac.uk
# main's code: the checkout's venv, not an installed release's launcher
export PATH=$HOME/SoCLabs/harness-manager/.venv/bin:$PATH
# RC2's overlays (static 0x44EE76D5)
export HARNESS_MANAGER_MPS3_OVERLAY_DIRS=$HOME/SoCLabs/mps3-nanosoc-platform-lx/fpga/dfx/build_mint3_rc2_linux/overlay_mbv
# the ILAs are Vivado 2026.1: Harness Manager's own hw_server must be 2026.1 too
export HARNESS_MANAGER_HW_SERVER=/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/hw_server
EOF
source $HOME/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh
command -v harness-manager; ls $HARNESS_MANAGER_MPS3_OVERLAY_DIRS | tee $EV/0_overlays_dir.txt
cat ~/.config/harness-manager/boards.toml | tee $EV/0_boards_toml.txt
ssh -o BatchMode=yes -o ControlPath=none -o ClearAllForwardings=yes $H true && echo SSH-OK
```

**Expect:**
- `command -v` prints `…/SoCLabs/harness-manager/.venv/bin/harness-manager`.
- The overlay list includes `greybox`, `nanosoc` and `nanosoc_ila`.
  - If the folder is gone, copy the hub's copy and point the variable at it:
    `scp -rq $H:/home/david/mints/0x44EE76D5/overlay_mbv $HOME/mint_44EE76D5_overlay_mbv`.
- boards.toml has the board table from the Thursday HIL: `match = ["192.168.10.101"]`,
  `via = "ssh:mapstone-dev…"`, and a hub table with `target = "mps3_01_pl"` and
  `shares = { mcc = "/dev/mps3_01_pl/tty_00" }` (`use = "lab"` instead of `host` is fine too).
  - Note any `ssh` sub-table with a `host_key`: it predates the cutover card (see §B).
- `SSH-OK`.

**In every new terminal:** `source ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh`.

### 0.2 Announce, then check the board is free (2 min)

1. Paste this into the **Linux lead's session** and wait for the answer:
   > HM-HIL-LX: I want mps3_01_pl for ~70 min from HH:MM, lease holder `david-hm`, through Harness
   > Manager. (1) Is the board free: no soak, runner or MCC tool on it? (2) May I start an fpgahub
   > share on `tty_00` for Harness Manager's MCC REBOOT? It stays running afterwards (fpgahub has
   > no single-share stop), and while it runs, `soak_linux.py mcc-reboot` refuses (rc 3/4).
   > Please reply "free" and yes/no to (2).
2. When it says "free", check (read-only):
   ```bash
   ssh $H 'pgrep -af "soak_linux.py (accel|tail|boots)"; ps -eo pid,user,args | grep -F tty_00 | grep -v grep; echo END' | tee $EV/0_hub_idle.txt
   harness-manager lease show $B | tee $EV/0_lease_before.txt
   harness-manager share list $B | tee $EV/0_shares_before.txt
   ```
   **Expect:**
   - only `END`: no soak runs and nothing reads `tty_00`;
   - `mps3_01_pl on mapstone-dev…: not leased`;
   - `share list` either shows `/dev/mps3_01_pl/tty_00` or not; note which.
3. **Choose the MCC path now** and write it down (§D and §G use it):
   - **`hm`** if `tty_00` is already shared, or the Linux lead said yes to (2): Harness Manager's
     own `mcc … reboot`.
   - **`hubtool`** otherwise: the platform's paced REBOOT tool on the hub. It opens no share and
     keeps the MCC boot log.
   ```bash
   echo "MCC=hm" > $EV/0_mcc_path.txt       # or: echo "MCC=hubtool" > $EV/0_mcc_path.txt
   ```

### 0.3 Take the lease through Harness Manager (1 min, may queue)

```bash
harness-manager lease acquire $B --ttl 7200 --holder david-hm | tee $EV/0_lease_acquire.txt
harness-manager lease show $B | tee $EV/0_lease_show.txt
```
**Expect:**
- acquire prints `mps3_01_pl on mapstone-dev.ecs.soton.ac.uk: held by david-hm until <time>`;
- show prints `… held by david-hm (user david, expires …) — yours`.

Harness Manager runs fpgahub under `sg fpga`, so the lease is yours, not root's, and the app
heartbeats it while the board is open.
- **If it queues** (`queued at position N`): someone still holds the board. Press Ctrl-C (it
  leaves the queue) and ask the Linux lead.

### 0.4 Restart Harness Manager on the latest code (2 min)

Terminal B:
```bash
git -C $HOME/SoCLabs/harness-manager status -sb | head -1 | tee $EV/0_hm_version.txt
git -C $HOME/SoCLabs/harness-manager log --oneline -1 | tee -a $EV/0_hm_version.txt
harness-manager version | tee -a $EV/0_hm_version.txt
harness-manager daemon stop
```
**Expect:**
- `## main`, and the head the HM lead named in the go message;
- the version;
- `daemon stop` either stops the service or says it was not running.

Stopping the service makes the next start read §0.1's variables.

Terminal A (leave it running):
```bash
source ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh
harness-manager app
```
In the window, open the board `192.168.10.101`. **Expect** on the Overview:
- shell `0x44ee76d5`;
- the Ethernet link `… via ssh:mapstone-dev.ecs.soton.ac.uk`.

---

## A. The Linux harness through Harness Manager (7 min, read only)

**A1. Identity**
```bash
harness-manager --json info $B | tee $EV/a1_info.json
```
**Expect** exit 0 and:
- `identity.harness_impl` `linux`;
- `identity.shell_id` `0x44ee76d5`;
- `identity.ver32` `0x01000000` and `identity.usercode` `0xfb1f8c76`, when the harness reports them
  (empty is not a failure);
- `identity.features` lists `usd`: §C and §D need it;
- `health.reachable` true;
- a `claim` block (its last check; §B asks the board now).

**A2. Front panel**
```bash
harness-manager panel show $B | tee $EV/a2_panel.txt
```
**Expect:**
- source `panel` if harnessd has the `panel` feature, else `rebuilt` with the reason;
- a `touch` line.

**A3. XVC status**
```bash
harness-manager xvc status $B | tee $EV/a3_xvc.txt
```
**Expect:**
- state `down`;
- the scope line ("scoped to the reconfigurable partition's debug chain … never whole-device JTAG");
- reach `board-ssh`;
- the note that this Linux harness does not report `xvc_lock` yet (no note if it does);
- the MIG note.

**A4. The finger test** (CLCD-HM R6): a held touch must not starve the network.
```bash
harness-manager --json panel show $B > $EV/a4_touch_before.json
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["panel"]["touch"])' $EV/a4_touch_before.json
# Hold a finger on the panel NOW, for 10 s, while this runs:
for i in 1 2; do date +%T; harness-manager info $B > $EV/a4_info_$i.txt; echo "info $i rc=$?"; done 2>&1 | tee $EV/a4_finger.txt
# Finger off.
harness-manager --json panel show $B > $EV/a4_touch_after.json
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["panel"]["touch"])' $EV/a4_touch_after.json
```
**Expect:**
- both `info` calls answer `rc=0` within a few seconds each;
- `ok` True before and after;
- write `bus_lost` and `recoveries` before and after into `a4_finger.txt`. A rise in `bus_lost` is
  the touch-bus wedge: tell the Linux lead.

---

## B. The SSH claim (4 min): check it, adopt it, never re-claim

**B1. Who claimed the board?** It asks the board now, through the hub.
```bash
harness-manager board claim-status $B | tee $EV/b1_claim_status.txt
```
**Expect:**
- `claim <board>: claimed by another key (not this Harness Manager's)`;
- the note `… If it is yours (pyverify claim, another machine): harness-manager board claim TARGET --adopt`;
- `host key SHA256:… (the board's; not pinned)`;
- `route hub mapstone-dev…  user root`.

The board does not publish which key claimed it. "Another key" is expected: the Linux lead's
runner claimed it, not this Harness Manager.

Other answers:
- `claimed by you`: already adopted. Skip B2.
- `unclaimed`: **STOP §B. Never claim.** Ask the Linux lead: a cutover board should be claimed,
  and a claim now would lock their keys out.
- a `HOST KEY CHANGED` note: see the failure table.

**B2. Adopt the claim** (only after B1 said "another key"). It asks first; answer `y`.
```bash
harness-manager board claim $B --adopt --key ~/.ssh/id_ed25519.pub | tee $EV/b2_adopt.txt
```
**Expect:**
- `host key: connecting over SSH to capture it`;
- `pinned SHA256:… in boards.toml boards.<table>.ssh.host_key`;
- `claim <board>: claimed by you (…)`.

What adopt does and does not do:
- It logs in once as root with that key (`true`), checks the host key against the board's identify,
  and pins it. It also writes `ssh.key` (your private key's path) into boards.toml.
- It adds no key to the board and never repeats the claim.

If adopt answers `the board's SSH refused your key (…): the board is claimed by another key`
(exit 15): the claim is not your key.
1. **STOP §B.** Tell the Linux lead.
2. Skip §E (XVC reaches the board over that SSH).
3. Carry on with §C, §D and §F.

**B3. The pinned SSH works**
```bash
harness-manager board ssh $B -c 'cat /run/mps3/persist.state' | tee $EV/b3_ssh.txt
```
**Expect:** `backing=card dev=/dev/mmcblk0p3 storage=ok`.

---

## C. OS slots and the user microSD (2 min, read only)

**C1. The OS slots**
```bash
harness-manager slot status $B | tee $EV/c1_slot_status.txt
```
**Expect:**
- `slots <board>: running A, default A, a push goes to B`;
- slots A and B both `valid`, with a `hdr_crc` and `verified`;
- a `job` line with no job running.

**C2. The card**
```bash
harness-manager card status $B | tee $EV/c2_card_status.txt
```
**Expect:**
- `card <board>: valid, <N> MB`;
- a `default …` line. Expect `default    none: the greybox loads at power-on`.
  **This line is what §Z puts back.** Circle it in the file.
- `power-on …`;
- the `os slots` lines, as in C1.

---

## D. Keep on the card, then an MCC REBOOT (12 min): WRITES THE USER MICROSD

**D1. What loads** (read)
```bash
harness-manager overlays $B | tee $EV/d1_overlays.txt
```
**Expect:** `nanosoc` and `nanosoc_ila` on `ok` lines, and nothing that should load under `cannot`.

**D2. Program nanosoc and keep it on the card.**
- **WRITES** the user microSD: the harness re-pushes the ~3 MB pair into the card store's inactive
  slot, reads it back, then flips the store's header.
- The partition swaps too.
```bash
harness-manager program $B nanosoc --keep-on-card | tee $EV/d2_program_keep.txt
```
It prints the preflight, then `card: …; the design will be kept on it`, then asks
`program nanosoc (0x01000001) into <board> and keep it on the card?`. Answer `y`.

**Expect** (1–2 min):
- **Preflight:** every item `ok`, except `static_usercode matches … (not a pass)`, which is
  unchecked (it needs JTAG).
- **Result:** `programmed nanosoc (0x01000001) into <board> in N s via tcp; verified`. It is TCP
  through the tunnel, never TFTP; the Linux harness has no windowed mode.
- **Card:** `kept on the card (slot A|B): the board boots into nanosoc next time`.

The same in the app: Program → `nanosoc` → tick **Keep on the card** (off by default) → Program.

**D3. The card took it**
```bash
harness-manager card status $B | tee $EV/d3_card_after_keep.txt
```
**Expect:** `default    nanosoc 0x01000001 for static 0x44ee76d5, store slot A|B`.

**D4. REBOOT the board** (2–4 min). The MCC power-cycles it and reloads the FPGA from the config
SD. Use the path from `0_mcc_path.txt`.

- **`MCC=hm`** (Harness Manager's own REBOOT):
  ```bash
  harness-manager share start $B mcc | tee $EV/d4_share.txt
  harness-manager mcc $B reboot --wait 240 | tee $EV/d4_mcc_reboot.txt
  ```
  1. `share start` prints `/dev/mps3_01_pl/tty_00 → 0.0.0.0:<port>  writer -  clients 0`.
     `clients 0` means nobody else reads `tty_00`. **Clients 1 or more: STOP**, find the reader.
  2. The reboot asks `reboot <board>? The board reloads from its SD and the running design is
     lost`. Answer `y`.
  3. **Expect** `rebooted <board> (seen: sent, down, up)`.
  4. What it does: it types a CR and checks for the `Cmd>` prompt, then sends REBOOT at 100 ms a
     character over the share, and proves the board went down and came back.
  5. **Always pass `--wait 240`.** The verb's default of 120 s is the bare-metal budget, and a
     Linux boot takes longer.
- **`MCC=hubtool`** (the platform's paced REBOOT, same rules; keeps the MCC boot log):
  ```bash
  ssh $H "sg fpga -c '/usr/bin/python3.11 /home/david/mps3_rollback/soak_linux.py mcc-reboot --log /home/david/mps3_rollback/hil_lx_d4_mcc.log'" | tee $EV/d4_mcc_reboot.txt
  ```
  **Expect** `{"tty": "/dev/mps3_01_pl/tty_00", …, "rc": 0, "ack": true, "configuring": true, "complete": true, "failed": false}`.

**D5. The board came back running nanosoc from the card**
```bash
harness-manager card status $B | tee $EV/d5_card_after_reboot.txt
harness-manager --json info $B | tee $EV/d5_info_after_reboot.json
harness-manager board claim-status $B | tee $EV/d5_claim_after_reboot.txt
```
**Expect:**
- `power-on    loaded`. If it says `pending`, wait 30 s and repeat: a swap during the power-on load
  is refused.
- `identity.rm_id` `0x01000001` and `identity.shell_id` `0x44ee76d5`.
- `claimed by you`: the claim and the host key live on the card.

A power-on configures the greybox base, so nanosoc running now can only be the card's power-on
load: **Keep on the card PASS.**

---

## E. XVC W5 on nanosoc_ila (10 min, read only on the board)

Needs §B's adopt. On the Linux harness, XVC goes over the board's own SSH
(`ssh -J hub root@board -L …:2542`), with the pinned host key.

**E1. Load the ILA design** (a swap; not kept on the card). It asks; answer `y`.
```bash
harness-manager program $B nanosoc_ila | tee $EV/e1_program_ila.txt
```
**Expect:** `programmed nanosoc_ila (0x0100000a) into <board> in N s via tcp; verified`.

**E2. Open XVC** (terminal C)
```bash
source ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh
harness-manager xvc open $B | tee $EV/e2_xvc_open.txt
```
**Expect:** state `ready`, reach `board-ssh`.
- With the app open, it returns with `the XVC session stays with harness-manager-daemon;
  harness-manager xvc close … ends it`.
- Otherwise it holds until Ctrl-C: leave it running.

**E3. The Tcl and the hw_server** (terminal B)
```bash
harness-manager xvc tcl $B | tee $EV/e3_xvc_tcl.txt
pgrep -af hw_server | tee $EV/e4_hw_server_ps.txt
```
**Expect:**
- The Tcl: `# XVC here is scoped …`, `open_hw_manager`, `connect_hw_server -url localhost:<H>`,
  `open_hw_target`, `current_hw_device [lindex [get_hw_devices] 0]`, two `set_property … {…nanosoc_ila.ltx}`
  lines, and `refresh_hw_device [current_hw_device]`.
- One hw_server from `/research/CAD/Xilinx/Vivado/2026.1/…`, with
  `-e set auto-open-servers xilinx-xvc:127.0.0.1:<R> -e set jtag-port-filter Xilinx/XVC/127.0.0.1:<R>`.
  Those flags open the XVC cable only, never a local USB one.

**E4. Vivado 2026.1** (terminal D)
```bash
/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode tcl -nojournal -nolog
```
1. Paste E3's Tcl.
2. Then:
   ```tcl
   get_hw_targets
   get_hw_devices
   get_property IDCODE_HEX [current_hw_device]
   get_hw_ilas
   ```
3. **Expect:**
   - **exactly one** target, whose name contains `Xilinx/XVC/127.0.0.1:`, and **no** Digilent or
     other USB target (the partition-only rule);
   - a `debug_bridge` device, IDCODE `0A003093` (the B1 XVC smoke read `0x0A003093`);
   - at least one `hw_ila_*`, with the probes file loaded.
4. Copy the Tcl console text into `$EV/e5_vivado.txt`. A screenshot `e5_vivado.png` is optional.
5. Type `exit` to leave Vivado.

**E5. Close XVC** (terminal B)
```bash
harness-manager xvc close $B | tee $EV/e6_xvc_close.txt
pgrep -af hw_server || echo NO-HW-SERVER
```
**Expect:** `down`, then `NO-HW-SERVER`. If terminal C still holds, it ends too.

---

## F. The hub SD door: can fpgahubd read what Harness Manager stages? (12 min)

**What the door does** (`hub_sd.py`, HARNESS-DIST §6 (b)):
1. It uploads the `.bit` over ssh into the hub user's `~/.cache/harness-manager/hub-sd/<sha256>.bit`.
2. It sends **one** `fpgahub target program mps3_01_pl <that path> --method sd --force`.
3. fpgahubd reads the file **by path**.

**Why it may fail:** the unit file in the fpgahub repo sets `ProtectHome=yes`, which hides `/home`
from the daemon. The platform's runbooks program from `/home/david/…` and that worked, so the
installed unit probably differs.

This section proves the point for Harness Manager's staging path **without writing the SD**:
- F1, F2 and F4 write nothing;
- F3 writes only the hub user's cache;
- F6 (opt-in) **writes the config SD**.

**F1. How the daemon is sandboxed** (read)
```bash
ssh $H 'systemctl show fpgahubd -p ProtectHome -p PrivateTmp -p User -p ReadOnlyPaths -p InaccessiblePaths; findmnt -no FSTYPE,SOURCE -T $HOME' | tee $EV/f1_unit.txt
```
**Expect:**
- `ProtectHome=no` or `ProtectHome=read-only`: either lets the daemon read `/home`.
  - `yes` or `tmpfs` means the daemon cannot see `/home`, and F4 will fail.
- `PrivateTmp=yes`: this is why nothing is staged in `/tmp`.
- the filesystem of your hub home.

**F2. The hub offers an `sd` method** (read). This is the query behind the door's "available".
```bash
ssh $H 'sg fpga -c "fpgahub target program mps3_01_pl --list"' | tee $EV/f2_program_list.txt
```
**Expect:**
- a row `sd │ sd_install │ … │ yes │ …`;
- a `last programmed fingerprint: …` line. Record it.

`sd … no (<reason>)` means the door is unavailable. Record the reason and skip F6 and §G.

**F3. Stage RC2's own base image the way Harness Manager does.**
- Writes only the hub user's cache: 13 MB, and the SD is not touched.
- These bytes are the image the board runs.
```bash
BIT=$HOME/SoCLabs/mps3-nanosoc-platform-lx/fpga/dfx/build_mint3_rc2_linux/prod/config_rm_greybox_stage0.bit
SHA=$(sha256sum $BIT | cut -c1-64); echo "$SHA" | tee $EV/f3_stage.txt
ssh -o ControlPath=none -o BatchMode=yes -o ConnectTimeout=15 $H "mkdir -p .cache/harness-manager/hub-sd && cat > .cache/harness-manager/hub-sd/$SHA.bit.part && mv -f .cache/harness-manager/hub-sd/$SHA.bit.part .cache/harness-manager/hub-sd/$SHA.bit" < $BIT
ssh $H "sha256sum .cache/harness-manager/hub-sd/$SHA.bit; namei -l \$HOME/.cache/harness-manager/hub-sd/$SHA.bit" | tee -a $EV/f3_stage.txt
```
**Expect:**
- `SHA` = `8a30ade887b12713065e1cd1b7411520fc56bb9ffcda5f5439be48d97cebc7c5`;
- the hub's `sha256sum` prints the same;
- `namei` shows each directory's owner and mode.

The upload command is the one `SshUploader` runs.

**F4. The read probe.** It writes nothing.

It asks fpgahubd to program the staged file with a **method that does not exist**. fpgahub
v0.3.0's `dispatch_program` works in this order:
1. it checks the lease;
2. it answers `bitstream not found` if it cannot see the file;
3. it parses the header and hashes the whole file;
4. only then does it look up the method.

So a "no such method" refusal proves the daemon read the file, and nothing was dispatched. The
first command is the control, with a file that does not exist.
```bash
ssh $H "sg fpga -c 'fpgahub target program mps3_01_pl \$HOME/.cache/harness-manager/hub-sd/no-such-file.bit --method hm-read-probe'" 2>&1 | tee $EV/f4_probe_control.txt
ssh $H "sg fpga -c 'fpgahub target program mps3_01_pl \$HOME/.cache/harness-manager/hub-sd/$SHA.bit --method hm-read-probe'" 2>&1 | tee $EV/f4_probe.txt
```
**Expect:**
- **Control:** an error naming `bitstream not found: /home/david/.cache/harness-manager/hub-sd/no-such-file.bit`.
- **Probe:** an error naming `board 'mps3_01_pl' has no program method 'hm-read-probe' (available: […])`.
  → **PASS:** the daemon found, read and hashed Harness Manager's staged file.
  - `skip: bitstream_loaded already at sha256=…` is a PASS too: the daemon read it, and a skip
    writes nothing.

**FAIL outcomes:**
- The probe also says `bitstream not found: …/<sha>.bit`: fpgahubd cannot see the hub user's home
  (F1).
- `bitstream header parse failed`, or an HTTP 500: the daemon saw the file but could not read it
  (check F3's `namei`).

For either FAIL: the SSH door cannot program from its staging dir. Skip F6 and §G, and report it.
The fix is on the hub side: a staging dir the daemon can read, or the REST door's `--from <id>`.
F1 `ProtectHome=yes` plus an F4 FAIL means: set `hubs.<name>.stage_dir` to a group-`fpga` directory
outside `/home` and `/tmp` that the hub admin creates (`docs/HUB_MODE.md`, "The hub SD door's
staging directory"); an F4 PASS means the default stays.

A lease answer (409/423) means the hub does not count `david-hm`'s lease as yours: redo §0.3,
then repeat F4.

Leave the staged file where it is. A real install overwrites it.

**F6 (opt-in, +10 min).** **WRITES THE CONFIG SD, then REBOOTs.** Runs only if all of these hold:
- F4 passed;
- `MCC=hm`;
- you opt in;
- `harness list` shows a published release for this static.
```bash
harness-manager harness list $B | tee $EV/f6_harness_list.txt
```
Go on only if the `board` line says `runs <version>` (not `unrecorded`) **and** a release for
static `0x44ee76d5` is listed. The door keeps the running release's `nanosoc.bit` from the signed
cache as its backup, so without both the planner refuses. If either is missing, **skip F6**: F4 is
today's check.
```bash
harness-manager harness install $B <VERSION> --door hub | tee $EV/f6_install.txt
```
**What happens:**
1. It prints the plan and asks you to type `INSTALL mps3_01_pl HELD BY david-hm 0 QUEUED`.
2. It uploads the `.bit`, then sends one `--method sd --force`. Expect the progress text
   `a client timeout here is expected: the hub keeps writing`.
3. It waits for the hub journal's `program dispatched: … ok=True … sha256=<12 hex>`.
4. It sends the paced REBOOT over the `tty_00` share.
5. **Expect** the board back on `0x44ee76d5`.

**Never re-run it after a timeout.**

---

## G. Config SD A/B by pointer (opt-in, 10 min): WRITES THE CONFIG SD

**Runs only if** F4 passed and you opt in.

**What it proves:** the MCC boots an `F0FILE` other than `nanosoc.bit`. With that proven,
`updates.sd_ab` can be switched on.

**Why it is by hand:** Harness Manager cannot do this through the hub yet.
- fpgahub's `sd` method writes only `nanosoc.bit` (FPGAHUB_REQUESTS FH-b).
- Harness Manager's A/B code (`sd_ab.py`) needs the config SD mounted on this machine.

So this is done once, on the hub, with `sudo`. That is only for the mount; never for the lease.

**Rules:**
- The running image `nanosoc.bit` is never touched. `nanosoca.bit` is a byte copy of it.
- Never REBOOT while the SD is mounted on the hub: that starves the MCC.
- **`F0FILE` goes back to `nanosoc.bit` before §Z.** Otherwise the Linux lead's rollback, which
  writes `nanosoc.bit`, would silently not boot.

**G1 + G2. On the hub** (one recorded session). The first four commands are read only; after
that, **WRITES**.
```bash
ssh -t $H script -q hil_lx_g.typescript
```
Then, on the hub:
```bash
ls -l /dev/disk/by-label/ | grep -i V2M-MPS3            # exactly one line
mount | grep -c fpgahub/sd                               # 0: no fpgahub SD write in progress
M=/mnt/mps3sd_hil; D=$M/MB/HBI0309C/Nanosoc
sudo mkdir -p $M && sudo mount /dev/disk/by-label/V2M-MPS3 $M
grep -i APPFILE $M/MB/HBI0309C/board.txt; grep -i F0FILE $D/nanosoc.txt; sha256sum $D/nanosoc.bit
# --- WRITES from here: a byte copy of the running image, then the one-line pointer ---
sudo cp $D/nanosoc.bit $D/nanosoca.bit && sync && sha256sum $D/nanosoca.bit
cp $D/nanosoc.txt ~/nanosoc.txt.hil_orig
sudo sed -i -E '/^[[:space:]]*F0FILE/I s/nanosoc\.bit/nanosoca.bit/I' $D/nanosoc.txt && sync
diff ~/nanosoc.txt.hil_orig $D/nanosoc.txt; grep -i F0FILE $D/nanosoc.txt
sudo umount $M && mount | grep -c mps3sd_hil
exit
```
**Expect:**
- one `V2M-MPS3` link, then `0`;
- `APPFILE: Nanosoc\nanosoc.txt` and `F0FILE: nanosoc.bit`;
- `nanosoc.bit` hashes to `8a30ade887b1…` (RC2: the right card), and `nanosoca.bit` hashes the same;
- `diff` shows **only** the `F0FILE` line; `F0FILE: nanosoca.bit`;
- the unmount leaves `0`.

If anything differs before the `WRITES` line: `sudo umount $M`, `exit`, skip §G.

**G3. REBOOT** with the path from `0_mcc_path.txt`:
- **`MCC=hubtool`** (strong proof: the MCC log names the file):
  ```bash
  ssh $H "sg fpga -c '/usr/bin/python3.11 /home/david/mps3_rollback/soak_linux.py mcc-reboot --log /home/david/mps3_rollback/hil_lx_g3_mcc.log'" | tee $EV/g3_mcc_reboot.txt
  ssh $H "grep -i 'Configuring FPGA from file' /home/david/mps3_rollback/hil_lx_g3_mcc.log" | tee -a $EV/g3_mcc_reboot.txt
  ```
  **Expect:** `rc 0`, and `Configuring FPGA from file \MB\HBI0309C\Nanosoc\nanosoca.bit` → **PASS**.
- **`MCC=hm`**:
  ```bash
  harness-manager mcc $B reboot --wait 240 | tee $EV/g3_mcc_reboot.txt
  ```
  **Expect:** `rebooted <board> (seen: sent, down, up)`.
  - This is **PASS (inferred)** only: Harness Manager's reboot output does not say which file the
    MCC loaded.
  - It proves the pointer only if the MCC does not fall back to `nanosoc.bit` on an F0FILE it
    rejects. That is not verified.

Then:
```bash
harness-manager --json info $B | tee $EV/g3_info.json
```
**Expect:** shell `0x44ee76d5`, `reachable` true.

**Dark board** (no `up` within 240 s, or `rc 4`): do G4 **now**, then repeat G3's REBOOT. The MCC
itself still answers: the SD is its drive, not the FPGA's.

**G4. The pointer back** (always). **WRITES** the config SD: one line.
```bash
ssh -t $H script -q -a hil_lx_g.typescript
```
Then, on the hub:
```bash
mount | grep -c fpgahub/sd                               # 0
M=/mnt/mps3sd_hil; D=$M/MB/HBI0309C/Nanosoc
sudo mount /dev/disk/by-label/V2M-MPS3 $M
sudo sed -i -E '/^[[:space:]]*F0FILE/I s/nanosoca\.bit/nanosoc.bit/I' $D/nanosoc.txt && sync
diff ~/nanosoc.txt.hil_orig $D/nanosoc.txt && echo POINTER-RESTORED
sudo umount $M && mount | grep -c mps3sd_hil
exit
```
Then, on srv03335:
```bash
scp -q $H:hil_lx_g.typescript $EV/g_hub_session.txt
```
**Expect:** `POINTER-RESTORED`, then `0`.
- No REBOOT is needed: `nanosoc.bit` is byte-identical to what runs.
- Leave `nanosoca.bit` on the card: the A/B mode uses it, and nothing reads it now.

**After a strong PASS**, the HM lead switches the mode on after reading the evidence:
`harness-manager config set updates.sd_ab true`. It takes effect only for installs with the config
SD mounted on this machine; the hub door needs FH-b first.

---

## Z. Close-out (5 min)

**Z1. Put the card's power-on default back to C2's line.** **WRITES** the user microSD. It asks;
answer `y`.
- If C2 said `default none`:
  ```bash
  harness-manager card clear $B | tee $EV/z1_card.txt
  ```
  **Expect:** `done …`, then `default    none: the greybox loads at power-on`.
- If C2 named an overlay X:
  ```bash
  harness-manager program $B X --keep-on-card | tee $EV/z1_card.txt
  ```
  **Expect:** `kept on the card …`.

**Z2. Greybox**
```bash
harness-manager restore $B | tee $EV/z2_restore.txt
harness-manager card status $B | tee $EV/z3_card_final.txt
```
**Expect:**
- `restored <board> to the baseline (0x00000000) in N s; verified`;
- the card's `default` line equals C2's.

**Z3. Close the board** in the app (or `harness-manager daemon stop`), then:
```bash
pgrep -af -- '-N -T' | grep mapstone || echo NO-TUNNEL | tee $EV/z4_tunnel_closed.txt
```
**Expect:** `NO-TUNNEL`.

**Z4. Release the lease**
```bash
harness-manager lease release $B | tee $EV/z5_release.txt
harness-manager lease show $B | tee -a $EV/z5_release.txt
```
**Expect:** released, then `mps3_01_pl on mapstone-dev…: not leased`.

**Z5. Tell the Linux lead and the guide session** (`mps3-nanosoc-platform-98`). Paste into both:
> HM-HIL-LX done HH:MM; mps3_01_pl is free (lease released).
> - The board is on RC2 0x44EE76D5 and runs the greybox. The card's power-on default is <none / X>,
>   as before.
> - The config SD `F0FILE` is nanosoc.bit. <§G ran: nanosoca.bit, a copy of the RC2 image, is left
>   on the card. / §G not run.>
> - The tty_00 fpgahub share is <running since §D / not started>.
> - Harness Manager adopted the SSH claim: it pinned the host key and added no key to the board.
> - Evidence: ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/

**Z6. Send the evidence**
```bash
ls -la $EV | tee $EV/z7_manifest.txt
```
Then tell the HM lead the folder is complete.

---

## If something fails

| What you see | Cause | Do this |
|---|---|---|
| `lease acquire`: `queued at position N` | someone still holds the board | Ctrl-C (it leaves the queue); ask the Linux lead. Never `lease force` |
| `lease show` exits 7, "your account on the hub needs the 'fpga' group" | fpgahub 0.3.0's socket group | `ssh $H id -nG` must list `fpga` |
| A1: `offline`, "the hub … could not reach the shell" | board rebooting, harnessd down, or a stale tunnel | wait 60 s and repeat A1; then `ssh mps3-b2 true`; then ask the Linux lead |
| A1: `harness_impl` not `linux`, or `shell_id` not `0x44ee76d5` | the board is not on RC2 (rolled back?) | **STOP**; ask the Linux lead |
| B1: `unclaimed` | the claim was lost (a RAM boot keeps it on tmpfs) or never made | **never claim**; ask the Linux lead |
| B2: `THE BOARD'S SSH HOST KEY CHANGED: pinned …, the board now reports …` | a pin in boards.toml from before the cutover card | ask the Linux lead whether the board was re-provisioned. If yes: `harness-manager board claim $B --adopt --key ~/.ssh/id_ed25519.pub --replace-host-key` (still checked against identify). If no: **STOP**, something else answers for the board |
| B2: `the SSH host key the board offered (…) is not the one its identify published` | something between you and the board | **STOP**; nothing was pinned; tell the Linux lead |
| B2: `the board's SSH refused your key` | the claim is not your srv03335 key | stop §B, skip §E, ask the Linux lead. `ssh -o BatchMode=yes mps3-b2 true` shows whether your alias's key logs in; if it does, pass that key's `.pub` to `--key` |
| D2: exit 12, "user microSD …" | no card, or the harness reports no `usd` | `card status` says which; skip §D |
| D2: `not kept on the card: <why>` | the card write failed; a card failure never fails the deploy, so the swap stands | record the reason; skip D4 and D5 |
| any `program`: refused because the power-on load is running | the card's power-on load is in progress | wait for `power-on loaded` (`card status`), then repeat |
| `stats` `rm_ok: false` after a restart | the partition stays in reset until the first swap | program once |
| D4 (`hm`): `share start` shows `clients 1+`, or "another client (…) holds the hub share's write slot" | someone else reads `tty_00` | `ssh $H 'ps -eo pid,user,args \| grep -F tty_00'`; have them close it; repeat |
| D4 (`hm`): `REBOOT sent but no restart observed` | the MCC dropped the REBOOT; nothing happened | safe to repeat **once** |
| D4 (`hm`): "went down … but did not come back" | a slow or failed Linux boot | wait 2 min, then `info`. Still dark: the Linux lead's ROLLBACK runbook. If the MCC stops answering, david power-cycles |
| D4/G3 (`hubtool`): `rc 2` / `rc 3` / `rc 4` / `rc 1` | rc 2: not in group `fpga`. rc 3/4: another reader, or an fpgahub share on `tty_00`. rc 1: no echo | nothing was sent for rc 2–4: fix, or switch to `MCC=hm` for rc 3/4. rc 1: repeat once |
| E2: "the board-SSH forward for XVC did not come up" | no pinned claim, or the key does not log in | redo B2; check `ssh mps3-b2 true` |
| E4: `connect_hw_server` answers a different version | a lingering auto-launched hw_server | `pkill -u $USER hw_server`; redo E2 and E3 |
| E4: a Digilent or USB target is listed | the partition-only filter failed | **STOP XVC** (E5); report it as a Harness Manager bug (`hw_server_argv`) |
| F2: `sd … no (…no USB mass-storage device…)` | the hub-path pin is back | record it; skip F6 and §G (the fix is the Linux lead's `mps3_fix_hub_path.sh`) |
| F4: the probe says `bitstream not found` | fpgahubd cannot see `~/.cache` (ProtectHome) | FAIL: report it with F1's output |
| F6: `POST /targets/mps3_01_pl/program: timed out` | the normal client timeout; the hub is still writing | **do nothing** for 5 min; then `ssh $H 'journalctl -u fpgahubd --since -10min --no-pager \| grep "program dispatched"'` must show `ok=True` with this sha. Never retry, never reset |
| G3: dark after the pointer flip | the MCC rejected `nanosoca.bit` | G4 now, then G3's REBOOT again |
| `ssh … -N -T` processes left after the app was killed | a hard kill skips the tunnel's close | start the app again (the service reaps a dead owner's tunnels); on an old build `pkill -f -- '-N -T .*mapstone-dev'` |

**Send back:** the whole `$EV` folder. The HM lead forwards the result to the guide session.
