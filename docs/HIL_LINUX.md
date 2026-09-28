# HIL: Harness Manager on the Linux harness (after cutover, ~70 min)

> **Unattended:** [HIL_AUTO.md](HIL_AUTO.md) runs this runbook's automatic checks overnight
> (`python -m tools.hil run --plan linux-netboot …`; on board 2, which has no user microSD,
> `--plan linux-nocard`), saves the evidence and writes `REPORT.md`; it lists the checks that stay
> manual and why. `tools/hil/plans.py` holds this runbook as data: a test fails when a check with
> an **Expect** here has no plan entry, or when a plan's skips differ from its preface below.

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

## Netboot mode (card unusable): read this first

The lab board's user microSD is **intermittent**. On 2026-09-27 the Linux lead (with david's
approval) zeroed both OS slot headers on it (LBA 67584 and 198656; 4 KB backups of each on the
hub in `/home/david/pv_soak/card_backup/`). The board now netboots stage0 → Linux, and the soak
image mounts `/persist` on tmpfs (`mps3.persist=off`). Until the Linux lead says the card is back:

1. **Skip everything that reads or writes the card, its slots or the config SD A/B:**
   - §C2 (the card);
   - §D2, D3 and D5 (keep on the card);
   - §G (config SD A/B);
   - §Z1, and Z2's `card status`.

   Never run `slot push|commit|rollback`, `card clear` or `program --keep-on-card` in this mode.
2. **§C1 is the netboot check.** `slot status` answers, and both slots read
   `empty (no S0LB header)`. That is the zeroed headers, not a fault.
3. **The SSH host key changes.** The harness keeps its host key in `/persist`, so the key on
   tmpfs is not the one the card held (and it changes back when the card returns). Harness
   Manager says so: `host key changed back to one seen on <date> (…); on the Linux harness this
   is usually /persist (the user microSD) mounting or not; re-pin with harness-manager board
   claim TARGET --adopt if you trust it`. SSH, and everything over it, is refused until you
   re-pin:
   - check the fingerprint against B1's `host key SHA256:…` line;
   - then `harness-manager board claim $B --adopt --key ~/.ssh/id_ed25519.pub`.

   A key never pinned here shows the loud `HOST KEY CHANGED` instead: ask the Linux lead before
   any `--replace-host-key`. B3's `persist.state` names tmpfs, not the card: expected here.
4. **The hub MCC read and REBOOT refuse while any process on the hub names `tty_00` on its
   command line, and the soak does.** The words are `another process on the hub names the MCC
   console /dev/mps3_01_pl/tty_00 on its command line, so it may open it at any moment (pid N:
   …)`. Nothing is sent. Do not ask for the soak to be stopped for this: skip D4 while it runs.
   When nothing names `tty_00`, D4 may run; expect the greybox back (netbooted), not nanosoc.

## Card-less mode (no user microSD: board 2): read this first

Board 2 (`mps3_02_pl`, `192.168.11.101`, boards.toml `lab2`) is Harness Manager's own board. It has
**no user microSD** and no JTAG cable. It netboots stage0 → Linux (the Linux lead pushes the image;
`/persist` is tmpfs), and a failed cold boot needs a person to press PB0. Netboot mode items 3
and 4 hold here too. Start every terminal with
`source ~/SoCLabs/harness-manager/tools/hil/env_b2.sh` (it sets `B`, `T`, `MCC_TTY`, `EV`, `RUN`
and F3's `BAKE`), and run HIL-AUTO with `--plan linux-nocard`.

1. **Skip everything that needs the user microSD, and every reset:**
   - §C2 (the card);
   - §D2, D3, D4 and D5 (keep on the card, then the REBOOT);
   - §F6 and §G (the config SD, then an MCC REBOOT);
   - §Z1, and Z2's `card status`.

   Never run `slot push|commit|rollback`, `card clear`, `program --keep-on-card`, `mcc reboot`
   or `harness install` on this board.
2. **§C1 is the card-less check.** The harness answers `slot status` with `card: false` and no
   slots; Harness Manager says so as exit 12: `OS slot update is unavailable: no user microSD
   card in the slot (the OS slots live on it)`. That is the empty slot, not a fault. Two
   `empty (no S0LB header)` slots mean a blank card is in: use Netboot mode instead.
3. **§F3 stages board 2's OWN bake** (its row in F3's table), never board 1's.
4. **Where a step names board 1** (`192.168.10.101`, `mps3_01_pl`, `/dev/mps3_01_pl/tty_00`), use
   board 2's: `$B`, `$T`, `$MCC_TTY`.

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
  sha256 `286ae54d2a2b…` (full hash in §F3), stage0 build `0xC457D656`.
  It is the 2026-09-27 re-bake of RC2: same static and UserID, new stage0 in BRAM.
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
- **Board 2** (`mps3_02_pl`, `192.168.11.101`, boards.toml `lab2`): the same static, UserID and
  overlays, but its own stage0 bake (build `0x6FAE6A0B`, sha256 `f206f788…`, rescue IP
  `192.168.11.101`) and no user microSD: read Card-less mode.

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
# board 1's hub target and its own base image (F3's table); board 2: tools/hil/env_b2.sh
export T=mps3_01_pl
export BAKE=/home/david/pv_rb/config_rm_greybox_stage0.bit
export BAKE_SHA=286ae54d2a2b8c15e8b610df8088d37e5c3b3c706aa9206aceade2b503f081b4
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
  `via = "ssh:mapstone-dev…"`, and a hub table with `target = "mps3_01_pl"` (`use = "lab"`
  instead of `host` is fine too). No `shares` are needed: an old `shares = { mcc = … }` entry is
  ignored, and Harness Manager never starts a share on `tty_00`.
  - Note any `ssh` sub-table with a `host_key`: it predates the cutover card (see §B).
- `SSH-OK`.

**In every new terminal:** `source ~/SoCLabs/harness-manager/docs/evidence/2026-09-hil-linux/env.sh`.

### 0.2 Announce, then check the board is free (2 min)

1. Paste this into the **Linux lead's session** and wait for the answer:
   > HM-HIL-LX: I want mps3_01_pl for ~70 min from HH:MM, lease holder `david-hm`, through Harness
   > Manager. Is the board free: no soak, runner or MCC tool on it? Harness Manager's MCC REBOOTs
   > run pyverify's tools on the hub (no share on `tty_00`). Please reply "free".
2. When it says "free", check (read-only):
   ```bash
   ssh $H 'pgrep -af "soak_linux.py (accel|tail|boots)"; ps -eo pid,user,args | grep -F tty_00 | grep -v grep; echo END' | tee $EV/0_hub_idle.txt
   harness-manager lease show $B | tee $EV/0_lease_before.txt
   harness-manager share list $B | tee $EV/0_shares_before.txt
   ```
   **Expect:**
   - only `END`: no soak runs and nothing reads `tty_00`;
   - `mps3_01_pl on mapstone-dev…: not leased`;
   - `share list` shows no `/dev/mps3_01_pl/tty_00`. If it does, someone else's share holds the
     MCC: every REBOOT below refuses (a second reader). Ask the Linux lead before going on.

   **The MCC path:** Harness Manager runs every MCC operation ON the hub through pyverify (its
   paced REBOOT writer, or `sd field --already-written` after an SD write). There is no share
   choice any more: a share on `tty_00` is a second reader that only `share stop` removes, and
   while one exists the platform's REBOOT refuses (rc 3/4).

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
- reach `board-ssh` when this Harness Manager already holds the claim (B1 would say
  `claimed by you`), with no lock note (the harness reports `xvc_lock`); before §B's adopt,
  reach `hub-tunnel` with the note `XVC goes the unauthenticated way … Claimed from here …`
  (an `xvc open` then would meet the board's lock: exit 15, the adopt hint). §E runs after B2;
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
- **Card-less (board 2):** exit 12, `OS slot update is unavailable: no user microSD card in
  the slot (the OS slots live on it)`: the harness said `card: false`, no slots.

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
SD.
```bash
harness-manager mcc $B reboot | tee $EV/d4_mcc_reboot.txt
```
1. It asks `reboot <board>? The board reloads from its SD and the running design is lost`.
   Answer `y`.
2. **Expect** `rebooted <board> (seen: sent, down, up)`, then `MCC loaded MB/HBI0309C/Nanosoc/nanosoc.bit`.
3. What it does, on the hub (`ssh $H 'sg fpga -c …'`, the hub's `python3.11`): pyverify's writer
   checks nothing else reads `tty_00`, types a CR and checks for `Cmd>`, sends REBOOT at 100 ms a
   character, waits for `Rebooting`, and captures the MCC boot log to `FPGA configuration
   complete`; then the shell must answer again. No share is started.
4. The wait is the Linux budget, 300 s, with no `--wait` (MCC-FIX; FIX-PACK-2 raised it from
   180 s: stage0's 10 s DDR settle puts a cold boot at ~190 s to 6900, 205 s to SSH). Add
   `--wait` only if the Linux lead says today's boot is slower still.
5. It refuses while the user microSD's card job writes or reads back (SLOT-TIMING's guard).

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

Needs §B's adopt. On a claimed Linux board, XVC goes over the board's own SSH: the
session's one claim forward (`ssh -J hub -l root board -L …:127.0.0.1:2542`, plus 6900,
6910 and 6921 for slots, the card and `debug up`), with the pinned host key. Through the
hub the claimed board would refuse it with `xvc locked: board claimed (use ssh)`.

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
2. It sends **one** `fpgahub target program <target> <that path> --method sd --force`
   (`$T`: `mps3_01_pl` for board 1, `mps3_02_pl` for board 2).
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
ssh $H "sg fpga -c 'fpgahub target program $T --list'" | tee $EV/f2_program_list.txt
```
**Expect:**
- a row `sd │ sd_install │ … │ yes │ …`;
- a `last programmed fingerprint: …` line. Record it.

`sd … no (<reason>)` means the door is unavailable. Record the reason and skip F6 and §G.

**F3. Stage the board's OWN base image the way Harness Manager does.**
- Writes only the hub user's cache: 13 MB, and the SD is not touched.
- Each board has its own stage0 bake: same static and UserID, but stage0's rescue IP and build id
  are baked into BRAM. **Never stage one board's bake for the other**: board 1's bit on board 2
  would put board 2's rescue on `192.168.10.101`, a subnet the hub cannot reach from board 2's NIC,
  and board 2 has no JTAG to recover.
- Both bakes exist only on the hub. The RC2 build dir on srv03335 (`…/build_mint3_rc2_linux/prod/`)
  still holds the original bake, so the first command copies the board's bake here (a read on the
  hub). `env.sh` (board 1) and `tools/hil/env_b2.sh` (board 2) set `T`, `BAKE` and `BAKE_SHA` from
  this table:

| Board | Hub target (`$T`) | Rescue IP | stage0 build | The bake on the hub (`$BAKE`) | sha256 (`$BAKE_SHA`) |
|---|---|---|---|---|---|
| 1 (`lab`) | `mps3_01_pl` | `192.168.10.101` | `0xC457D656` | `/home/david/pv_rb/config_rm_greybox_stage0.bit` (the 2026-09-27 re-bake) | `286ae54d2a2b8c15e8b610df8088d37e5c3b3c706aa9206aceade2b503f081b4` |
| 2 (`lab2`) | `mps3_02_pl` | `192.168.11.101` | `0x6FAE6A0B` | `/home/david/mps3_02_pack/sd_tree/MB/HBI0309C/Nanosoc/nanosoc.bit` (board 2's config-SD bake, 2026-09-28) | `f206f788f7497b650b6f0408ebb2fbdb795edb749784a3ec42e6caaaa3df5058` |

```bash
echo "board $B target $T bake $BAKE want $BAKE_SHA" | tee $EV/f3_stage.txt
BIT=$HOME/${T}_stage0_bake.bit; scp -q $H:$BAKE $BIT
SHA=$(sha256sum $BIT | cut -c1-64); echo "$SHA" | tee -a $EV/f3_stage.txt
if [ "$SHA" = "$BAKE_SHA" ]; then
  ssh -o ControlPath=none -o BatchMode=yes -o ConnectTimeout=15 $H "mkdir -p .cache/harness-manager/hub-sd && cat > .cache/harness-manager/hub-sd/$SHA.bit.part && mv -f .cache/harness-manager/hub-sd/$SHA.bit.part .cache/harness-manager/hub-sd/$SHA.bit" < $BIT
  ssh $H "sha256sum .cache/harness-manager/hub-sd/$SHA.bit; namei -l \$HOME/.cache/harness-manager/hub-sd/$SHA.bit" | tee -a $EV/f3_stage.txt
else
  echo "STOP: $BIT is $SHA, not $T's bake $BAKE_SHA: nothing staged" | tee -a $EV/f3_stage.txt
fi
```
**Expect:**
- the first line names the board you mean (`$T` = the board's row);
- `SHA` = the row's sha256, and no `STOP:` line. `STOP:` means the wrong bit or the wrong env
  file: nothing was staged. Skip F4 and F6;
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
ssh $H "sg fpga -c 'fpgahub target program $T \$HOME/.cache/harness-manager/hub-sd/no-such-file.bit --method hm-read-probe'" 2>&1 | tee $EV/f4_probe_control.txt
ssh $H "sg fpga -c 'fpgahub target program $T \$HOME/.cache/harness-manager/hub-sd/$SHA.bit --method hm-read-probe'" 2>&1 | tee $EV/f4_probe.txt
```
**Expect:**
- **Control:** an error naming `bitstream not found: /home/david/.cache/harness-manager/hub-sd/no-such-file.bit`.
- **Probe:** an error naming `board '<$T>' has no program method 'hm-read-probe' (available: […])`.
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

**F6 (opt-in, +10 min).** **WRITES THE CONFIG SD, then REBOOTs.** Never on a card-less board
(Card-less mode 1). Runs only if all of these hold:
- F4 passed;
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
4. It REBOOTs ON the hub with pyverify's `sd field --already-written` (the journal witness of
   this sha, one reader on `tty_00`, an intact `Cmd>`, then the paced REBOOT). If the MCC answers
   the first CR with only `\r\n` (it does for a few seconds after an SD write), the REBOOT is
   tried again, up to 3 times 5 s apart. The write is never retried.
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
- `nanosoc.bit` hashes to `286ae54d2a2b…` (the RC2 re-bake: the right card), and `nanosoca.bit` hashes the same;
- `diff` shows **only** the `F0FILE` line; `F0FILE: nanosoca.bit`;
- the unmount leaves `0`.

If anything differs before the `WRITES` line: `sudo umount $M`, `exit`, skip §G.

**G3. REBOOT** (strong proof: the MCC's own boot log names the file):
```bash
harness-manager --json mcc $B reboot --yes | tee $EV/g3_mcc_reboot.json
```
**Expect:** `"fpga_file": "MB/HBI0309C/Nanosoc/nanosoca.bit"` → **PASS**. The field is the MCC
banner's `Configuring FPGA from file` line, captured on the hub; `nanosoc.bit` there means the MCC
fell back or ignored the pointer: **FAIL**, go to G4.

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
> - No fpgahub share on tty_00 was started (Harness Manager never starts one).
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
| D4/G3: "another process on the hub has the MCC console /dev/mps3_01_pl/tty_00 open (pid N: …)" | a second reader: a `cat`, a console, an fpgahub share on `tty_00` | nothing was sent. `ssh $H 'ps -eo pid,user,args \| grep -F tty_00'`; have it closed (a share: ask david, `share stop` stops them all); repeat |
| D4/G3: "another process on the hub names the MCC console /dev/mps3_01_pl/tty_00 on its command line, so it may open it at any moment (pid N: …)" | a process that takes `tty_00` as an argument (the soak does). The scan cannot see a root process's open files, so it counts every process that names the tty | nothing was sent. Ask whoever runs it; repeat once it has stopped |
| D4/G3: "refusing the MCC REBOOT: no intact Cmd>" | the MCC is not at `Cmd>` (a `Debug>` left open, or a reader this account cannot see) | nothing was sent. After an SD write this was already retried 3 times 5 s apart; wait 30 s and repeat once |
| D4/G3: "the hub has no Python 3.10+ for pyverify's MCC tools" | no `python3.11` on the hub (its `python3` is 3.6) | the hub admin installs one; fpgahub's `/opt/fpgahub/bin/python3.11` counts |
| D4/G3: "MCC REBOOT refused: slot B is being written …" | the user microSD's card job is running (SLOT-TIMING) | wait for `slot status` to show it done; never force it during the soak |
| D4: `REBOOT sent … but no restart observed` | the REBOOT was not acknowledged | check `info` in 2 min before anything else; never send a second one on top |
| D4: "went down … but did not come back" | a slow or failed Linux boot | wait 2 min, then `info`. Still dark: the Linux lead's ROLLBACK runbook. If the MCC stops answering, david power-cycles |
| E2: "the SSH tunnel to 192.168.10.101 did not come up" (or, older, "the board-SSH forward for XVC did not come up") | the key does not log in | redo B2; check `ssh mps3-b2 true` |
| E2 (exit 15): "XVC (2542) needs the claiming key over the board's own SSH" | no pinned claim here: nothing was sent | redo B2's `--adopt` |
| E2 (exit 15): "XVC … was refused: the board is claimed" | the claim was not known here, so HM went through the hub and the board sent its lock line | `board claim-status $B`, then E2 again (or B2's `--adopt`) |
| E4: `connect_hw_server` answers a different version | a lingering auto-launched hw_server | `pkill -u $USER hw_server`; redo E2 and E3 |
| E4: a Digilent or USB target is listed | the partition-only filter failed | **STOP XVC** (E5); report it as a Harness Manager bug (`hw_server_argv`) |
| F2: `sd … no (…no USB mass-storage device…)` | the hub-path pin is back | record it; skip F6 and §G (the fix is the Linux lead's `mps3_fix_hub_path.sh`) |
| F4: the probe says `bitstream not found` | fpgahubd cannot see `~/.cache` (ProtectHome) | FAIL: report it with F1's output |
| F6: `POST /targets/mps3_01_pl/program: timed out` | the normal client timeout; the hub is still writing | **do nothing** for 5 min; then `ssh $H 'journalctl -u fpgahubd --since -10min --no-pager \| grep "program dispatched"'` must show `ok=True` with this sha. Never retry, never reset |
| G3: dark after the pointer flip | the MCC rejected `nanosoca.bit` | G4 now, then G3's REBOOT again |
| `ssh … -N -T` processes left after the app was killed | a hard kill skips the tunnel's close | start the app again (the service reaps a dead owner's tunnels); on an old build `pkill -f -- '-N -T .*mapstone-dev'` |

**Send back:** the whole `$EV` folder. The HM lead forwards the result to the guide session.
