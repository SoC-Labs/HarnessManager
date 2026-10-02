# Bringing up a new MPS3 board from a laptop with Harness Manager: scope, plan and decisions

**Lane BRINGUP-SCOPE, Thu 1 Oct 2026, night.** Written from code and docs only. No board, hub
or network was touched, and no platform file was changed.

**Who reads this:**
- david, to decide (section 6);
- the lanes building the first pieces tonight (BRINGUP-USB, SD-FLASH, RELEASE-PIPE): their
  work sits inside this plan;
- the Linux lead: section 3.3 holds the contract questions.

**Citations.** Every claim cites a file. **UNVERIFIED** marks a claim no file proves.

| Prefix | Tree | Commit |
|---|---|---|
| (none) | this Harness Manager tree, `hm-bringup-scope` | `a6dd60c` |
| `lx:` | `/home/dam1n19/SoCLabs/mps3-nanosoc-platform-lx` (`feat/linux-harness`) | `e7dd3c7` |
| `om:` | the platform's `origin/master`, read with `git show` (the v2.0.0 cutover) | `e3e2afd` |
| `pf:` | `/home/dam1n19/SoCLabs/mps3-nanosoc-platform` working tree (branch `feat/onboard-monolithic-to-fpga-toolkit`, not master) | `b2b83d3` |
| `ln:` | `/tmpdir/claude-74755/linux-lanes` (the Linux lead's scratch lanes, not in git) | n/a |
| `mem:` | the lead's notes, `~/.claude/projects/-home-dam1n19-SoCLabs-mps3-nanosoc-platform/memory/` (not in git; weaker evidence) | n/a |

---

## Read this first

1. **Bare metal from a laptop is built today, by CLI, in about 15 minutes.** Every step is
   tested against fakes. HM has **never** run it on a real board's Debug USB, because the lab's
   Debug USB cables are on the hub. The first real-board USB run gates everything else (P1).
2. **The Linux harness has a hole: a blank user microSD.**
   - The config SD half is the same as bare metal.
   - The OS half needs a card reader (SD-FLASH, tonight) or rescue provisioning over the
     network (HARNESS-DIST L3: not built, needs the Linux lead).
   - **Stage0's rescue never writes the card.** It boots one pushed image from RAM; Linux then
     writes the card (3.3).
3. **Tonight's SD-FLASH contract has a layout bug.** `kind: "image"` writes `linux_slot.img`
   to the whole device. That image is ONE slot. Stage0 needs an MBR plus a boot-select sector,
   and the slots at fixed LBAs (67584 and 198656). An image written at byte 0 never boots (3.3).
4. **Signed releases wait on david's key ceremony** (`docs/KEYS.md:3`). Until then the wizard
   can install only a bundle folder, unsigned, as `sd install` does today.
5. **Two new boards on one network clash.**
   - Every image has the same MAC.
   - Bare metal also has the same fixed IP.
   - Stage0 rescue answers only at its baked IP (3.4).
6. **Nine decisions for david** are in section 6. D1 (keys), D2 (the Linux blank-card door) and
   D8 (where the first real-board test runs) gate the most work.

---

## 1. The user journey

### 1.1 The starting state

| Thing | Out of the box | Source |
|---|---|---|
| MCC firmware | as Arm shipped it. Our boards report `v1.3.2`, built Apr 20 2018, HBI0309 build 567 | `lx:docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt:3-5` |
| Board revision | HBI0309C ("rev C, var A"). HM's planner checks it against the release | `pB_mcc_log_20260923.txt:12`; `src/harness_manager/services/update/planner.py:447-451` |
| Config SD (the MCC's microSD) | Arm's stock files, or a blank card. **No doc describes a factory card** | NOT FOUND in either repo (research note); **UNVERIFIED** |
| User microSD (Linux only) | none, or blank | — |
| Network | nothing set up | — |
| This laptop | HM installed (`docs/USER_GUIDE.md:42-79`). On Linux the user is in `dialout` (`:75-76`) | — |

### 1.2 Bare-metal harness: the config SD only

| # | Step | Time | Built today? |
|---|---|---|---|
| 1 | Plug in the Debug USB and Ethernet, power on. The config SD shows up as the drive `V2M-MPS3` | 1 min | n/a |
| 2 | `harness-manager probe` lists the four serial ports and the drive, and names the MCC port | 10 s | yes: `src/harness_manager_mps3/usb.py:1-45` |
| 3 | Pick a release: a signed catalogue release, or a bundle folder from SoC Labs | 1 min | folder: yes. Catalogue: refuses every channel until keys exist (`docs/USER_GUIDE.md:1229-1231`) |
| 4 | Back up the config SD to a zip | 1-5 min (**UNVERIFIED**: it reads the whole card, and a factory card's size is unknown) | yes: `src/harness_manager_mps3/sd.py:562` |
| 5 | Install the bundle: journaled, read back, never retried | 68-79 s measured for a 12-14 MB `.bit` over USB-MSC (`lx:host/pyverify/pyverify/fielding.py:113-115`; `lx:docs/evidence/2026-09-linux-b2/b2_sd_write.txt:15`). The guide warns "can take 5 minutes" | yes: `docs/USER_GUIDE.md:247-256` |
| 6 | Paced MCC REBOOT on `tty_00`, witnessed down and up | ~60 s to a bare-metal ping (the 26 Sep rollback: 127 s for write + reboot, `om:docs/planning/RELEASE_NOTES_v2.0.0.md:213`); HM waits up to 120 s (`src/harness_manager_mps3/constants.py:68`) | yes: `src/harness_manager_mps3/mcc.py:1-60` |
| 7 | Give the laptop's Ethernet `192.168.10.1/24`, then `harness-manager info 192.168.10.101` | 2 min | yes: `docs/USER_GUIDE.md:265-273` |
| 8 | Name it in `boards.toml` (`name = …`). This is an HM label only: bare metal has no identity store | 30 s | yes: `docs/USER_GUIDE.md:202-210`; `docs/design/BOARD_IDENTITY.md:116-117` |
| 9 | Program the first design | 1-2 min | yes, given the overlays (`docs/USER_GUIDE.md:649-688`). A bundle folder's overlays are not imported: the user needs `--overlay-dir BUNDLE/overlays/open` (gap G10) |

- **Total: about 15 minutes**, as `docs/USER_GUIDE.md:220` says.
- A bare-metal board is never claimed: the SSH claim is a Linux feature (`docs/USER_GUIDE.md:1491-1495`).
- A bare-metal board does not answer UDP identify (`lx:docs/contracts/net-protocol.md:416`), so
  a network scan cannot find it. HM reaches it by address.

### 1.3 Linux harness: config SD + stage0 + the OS image on the user microSD

Steps 1-6 are the same, but with a Linux release. Its config-SD `.bit` is the RC2 static with
**stage0** baked into BRAM (`lx:docs/planning/linux_lanes/FLOW_CONTRACT.md:38-57`). Then:

| # | Step | Time | Built today? |
|---|---|---|---|
| 6L | Stage0 finds no card, or a blank one, and comes up in **rescue**: ping, TFTP 69 and unicast identify `mode:"rescue"` at its baked IP; no 6900 | ~3 min from the REBOOT (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:199`) | stage0: yes (`lx:docs/planning/linux_lanes/STAGE0_CONTRACT.md:25-43`). HM reports rescue and refuses to go on (`src/harness_manager_mps3/os_slots.py:526-528`) |
| 7L | Put an OS image on the card, through **(a)** a card reader on the laptop or **(b)** rescue over the network | see below | **no** |
| 8L | REBOOT. Stage0 boots the default slot and harnessd confirms it | cold boot to 6900 ~190 s median, 188.6 s max, 205 s to SSH (`src/harness_manager_mps3/constants.py:62-67`) | yes |
| 9L | Claim it. The first SSH key sent claims it for good | 30 s | yes: `docs/USER_GUIDE.md:1491-1545` |
| 10L | Name it: `board identity TARGET --label MPS3-03 --ip … --mac …` (needs a card and the claim; applies after a warm restart) | 2 min | yes: `src/harness_manager/cli/cmd_identity.py:1-24` |
| 11L | Program a design; optionally **Keep on the card** | 2 min | yes |

**What has to land on the card** (`lx:docs/planning/linux_lanes/STAGE0_CONTRACT.md:377-400`;
`lx:src/linux_soc/hw/fw_stage0/stage0_mkcard.py:6-11,35-40`):

| Where | What | Size |
|---|---|---|
| LBA 0 | MBR | 512 B |
| LBA 1 and 2 | boot-select sector "S0BC", two copies | 512 B each |
| p4, LBA 2048 | D13 overlay store (type `0xDA`) | 32 MiB partition; empty until `usd format` |
| p1, LBA 67584 | slot A (type `0x7F`): one `linux_slot.img` from its first byte | 64 MiB partition |
| p2, LBA 198656 | slot B | 64 MiB partition |
| p3, LBA 329728 | `/persist` ext4 (type `0x83`) | the rest of the card |

**The OS image sizes and times:**

| What | Size | Written by the board | Written in a laptop card reader |
|---|---|---|---|
| `linux_slot.img` (S0LB v2; one slot) | 29.3-29.5 MB (`ln:cutover/wt/fielded/0x44EE76D5/linux_bundle.json:161-181`: 29,447,688 B); capped at 64 MiB (`src/harness_manager/services/update/s0lb.py:28`) | 41-50 min per slot today: ~12 min push + 30-35 min read-back (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:204`; `src/harness_manager_mps3/os_slots.py:62-64`). Board 1 measured 37 and 42 min. ~9 min modelled with the slot-speed fix, which is not on silicon or on `origin/master` | ~10 min "estimated" for both slots (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:206`). **Never done on silicon** (`lx:docs/planning/linux_lanes/BACKLOG_2026-09-28.md:216`) |
| Rescue push into RAM | the same image | 54.7 s for 29.3 MB, then 75 s to run mode (`lx:docs/evidence/2026-09-linux-b2/b2_usd_boot.txt:28-31`) | n/a |
| Config-SD `.bit` | 13,880,333 B, Linux static with stage0 (`ln:cutover/wt/fielded/0x44EE76D5/linux_bundle.json:192-195`) | 68-79 s over USB-MSC | seconds, card out of the board |
| `linux_legal_info.tar` (GPL sources) | 207,953,920 B. Travels with the image, never installed | n/a | n/a |

**Totals for a Linux bring-up:**
- **Card reader path:** about 25 minutes, once it exists.
- **Rescue path:** not possible in HM today. Once L3 exists: about 15 min + one slot write.
  That is ~60 min today, or ~25 min with the slot-speed fix.
- **The documented upgrade from bare metal** (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:181-210`)
  does it by hand with pyverify: a per-board stage0 bake (10 min, Vivado 2026.1), field, rescue
  push, claim, then the card (~1.5 h over the board, or ~10 min estimated on a PC).

---

## 2. What exists today

### 2.1 Harness Manager

| Piece | What it does | Where | On silicon? |
|---|---|---|---|
| `probe` with `scan_usb` | lists FT4232H ports and the `V2M-MPS3` volume; numbers the four interfaces (Linux: the sysfs location; Windows FTDI VCP: the A-D channel letter); pairs a USB board with an Ethernet board | `src/harness_manager_mps3/usb.py:1-45,132-184,321-333`; `scan_usb` defaults to true (`src/harness_manager/core/pack.py:43`) | **no** (the Debug USB is on the hub) |
| The app's **Add** dialog | probes one address only (`scan_usb: false`) | `src/harness_manager/web/static/js/add.js:211`; `store.js:564` | n/a |
| `sd - --volume V backup DIR`, `install BUNDLE --backup ZIP`, `restore ZIP` | the config SD over USB-MSC. Refuses the DAPLink drive, a volume without `config.txt` or `MB/`, any `.ebf` and the MCC command files. The journal marks an in-flight write. It never deletes stock files, has no client timeout and never retries | `src/harness_manager_mps3/sd.py:1-48,457-480`; CLI `src/harness_manager/cli/main.py:297-325` | **no**, not from HM. pyverify's equivalent writes the card on the hub (`lx:host/pyverify/pyverify/fielding.py:1-36`) |
| Config SD A/B by pointer | `nanosoca.bit`/`nanosocb.bit` plus an `F0FILE` flip, so an interrupted write cannot darken the board | `src/harness_manager_mps3/sd_ab.py:1-25`. Off (`updates.sd_ab`) until a 10-min board check | **no** |
| `mcc - --serial PORT reboot` | MCC = FT4232H interface 00; one character every ≥ 60 ms; refuses without a `Cmd>` prompt; never types in the 3 s auto-boot window; witnesses down, then up. FORMAT, DEL, EEPROM, COPY and others are denied | `src/harness_manager_mps3/mcc.py:1-60,105-118` | yes **through the hub** on 1 Oct ("rebooted … seen: sent, down, up", `docs/evidence/2026-10-01-h1/d4_mcc_reboot.txt:1-5`). Over a local serial port: **no** |
| Hub SD door | `fpgahub … --method sd --force`; completion proven by the hub's sha; never retried | `src/harness_manager_mps3/hub_sd.py:1-50` | **UNVERIFIED**: no HM evidence file found |
| Signed catalogue | `harness list/show/fetch/install/pin/history/rollback/mirror`; `--source URL\|DIR\|github:O/R`; `--door hub\|usb` | `src/harness_manager/services/harness_catalog.py`; `src/harness_manager/cli/cmd_harness.py:78-82` | **no**: `PINNED_KEYS = ()` (`src/harness_manager/services/update/trust.py:83`) |
| Verdict `needs-door` ("needs Debug USB or hub") | the release needs a config-SD write | `src/harness_manager/services/harness_catalog.py:91-94`; `src/harness_manager/web/static/js/sections/harness.js:25,45` | demo only |
| GUI **SD card** section | back up, install, reboot + witness, restore; a red recovery card when a journal is left behind | `src/harness_manager/web/static/js/sections/sd.js:1-40` | **no** |
| OS slots | `slot status/push/commit/rollback/verify`; rescue is reported and refused | `src/harness_manager_mps3/os_slots.py:1-80,526-528` | `slot status` yes. HM never wrote a slot on silicon: every slot write was pyverify's |
| S0LB frame check | refuses a bad `linux_slot.img` before it reaches a board | `src/harness_manager/services/update/s0lb.py:1-30` | n/a |
| Claim | never automatic; the first key wins; the host key is pinned | `src/harness_manager/services/claim.py:1-20`; `docs/USER_GUIDE.md:1491-1545` | yes (H1, 1 Oct) |
| Identity | `board identity`: label, IP, MAC; a typed phrase; needs the claim and a card. Refused on bare metal and on netboot | `src/harness_manager/cli/cmd_identity.py:1-24`; `docs/design/BOARD_IDENTITY.md:107-125` | the verb is in rc2_v7. An HM `identity_set` on silicon: **UNVERIFIED** |
| UDP identify scan | broadcast to 255.255.255.255 **plus a unicast to 192.168.10.101** | `src/harness_manager_mps3/identify.py:1-40` | yes (Linux) |
| Release tool | a bundle dir in, signed entries out. Refuses a dirty image, any `.ebf`, Arm IP in an open part, and a P-mint | `tools/release/harness.py:1-46`; `docs/RELEASING.md:99-126` | dry runs only |

### 2.2 The platform side (read only)

**The config-SD bundle** (`lx:fpga/mps3_sd/README.md:11-32,116-139`):

| File | What it does | Key lines |
|---|---|---|
| `config.txt` (root) | MCC settings | `AUTORUN TRUE`, `AUTORUNDELAY 3`, `UARTMODE 1`, `USB_REMOTE TRUE`, `WDTRESET NONE` (`lx:fpga/mps3_sd/templates/config.txt:16-17,30,41,43`) |
| `MB/HBI0309C/board.txt` | which app to load | `BOARD: @BOARD@` (A/B/C, the only per-revision difference); **`MBBIOS: mbb_v141.ebf`**; `APPFILE: Nanosoc\nanosoc.txt`, and the `\` is mandatory (`templates/board.txt:1,16-17,20-22`; `README.md:84-90,112-114`) |
| `MB/HBI0309C/Nanosoc/nanosoc.txt` | the FPGA image and clocks | `F0FILE: nanosoc.bit`, `OSC0: 25.0` (24.0 breaks Ethernet), `FPGA_DDR` (`templates/nanosoc.txt:18,23-26,54`) |
| `MB/HBI0309C/Nanosoc/nanosoc.bit` | the static + baked firmware (bare metal) or stage0 (Linux) | 12.1-12.6 MB bare metal; 13.9 MB Linux |

- `assemble_sd.sh` builds `bundle/` and never writes a card (`lx:fpga/mps3_sd/assemble_sd.sh:10-13`).
- **No platform tool writes `config.txt`, `board.txt` or `nanosoc.txt` to a card.** The hub's
  `sd_install` writes only `MB/HBI0309C/Nanosoc/nanosoc.bit`
  (`lx:host/fpgahub/mps3_01_pl.shell-update.toml:80`). The README's bundle copy is manual
  (`README.md:116-139`). **HM's `sd install` is the first tool that writes the whole tree.**
- The README says to merge, not wipe: "Do not delete the SD's stock Arm files — especially
  `mbb_v141.ebf` (the MB BIOS our `board.txt` references)" (`README.md:126-133`).
- **Doc drift:** the README spells the label `V2M_MPS3` (`README.md:118,121,125`). The scripts
  and HM use `V2M-MPS3` (`lx:scripts/mps3_sd_update.sh:39`; `src/harness_manager_mps3/constants.py:97`).
- **Doc drift:** the generated `pf:fpga/mps3_sd/bundle/HBI0309C/config.txt` is from Jul 30
  and still says `UARTMODE: 0`. The template says 1. Since the shell console moved to lane 2,
  UARTMODE no longer matters to it (`lx:docs/BOARD_BRINGUP.md:163-184`).

**The `.ebf` landmine** (`lx:docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md:465-468`, item G8):
- Our `board.txt` names `MBBIOS: mbb_v141.ebf`, but our live cards do not hold that file. The
  MCC logs `File not found` on every boot (`pB_mcc_log_20260923.txt:10`).
- "If that file is ever copied onto a card, the next boot silently reflashes the MCC from 1.3.2
  to 1.4.1." G8 asks to correct the comment. It is still open: `templates/board.txt:16` still
  says the file is "already present".
- **A factory card is exactly where that file may be present.** See risk R4.

**MCC REBOOT rules:**
- `tty_00` is the MCC console; `tty_01` is FPGA lane 0. The boot log says
  `UART0: MCC, UART1: FPGA0` (`pB_mcc_log_20260923.txt:33`; `lx:host/pyverify/pyverify/bootrate.py:50-58`).
- One reader only: a leftover `cat` on `tty_00` split the first remote REBOOT
  (`lx:docs/evidence/2026-09-w3/w1_field_remote_20260924.txt:12-16`).
- Send a bare CR first, check for `Cmd>`, then type `REBOOT` one character at a time, at least
  50 ms apart (`bootrate.py:225-231,640-650,749-752`).
- After a write, the MCC can answer a bare CR with an empty line. Re-run with
  `--already-written`; never write twice (`lx:docs/evidence/2026-09-linux-b2/b2_sd_write.txt:56,70,73-130`).
  HM retries the prompt read three times, 5 s apart (`src/harness_manager_mps3/mcc.py:171-177`).
- A REBOOT sent during an SD write is echoed and ignored (`lx:docs/evidence/2026-09-w3/v011_card_boot_20260924.txt:11-16`).
- `reboot.txt` (TRM §3.2) is **not proven** on this board (`lx:docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md:485`, W2).
- **Never leave the config SD mounted.** A mount starves the MCC, and the board goes dark
  (`pf:docs/internal/BOARD_HANDOFF_NOTES.md:75-78`; gitignored).

**Stage0 and rescue** (`lx:docs/planning/linux_lanes/STAGE0_CONTRACT.md`):
- **Boot order** (`:25-43`):
  1. Arm the watchdog.
  2. On a cold entry, settle 10 s.
  3. DDR calibration must hold 1 s.
  4. Card with a valid MBR.
  5. The default slot, CRC OK, hand off.
  6. The other slot.
  7. Otherwise rescue.
- **Try once, then confirm** (`:73-75,293-317`): 2 unconfirmed boots skip a slot; both
  exhausted → rescue.
- **Read-only on the card** (`:69-70`): "Stage0 never writes the card. The size gate proves it."
- **Rescue is a TFTP server** that takes ONE pushed image, verifies it in DDR and boots it once
  from RAM. It is not written to the card (`lx:docs/LINUX_HARNESS.md:170-180`;
  `STAGE0_CONTRACT.md:440-465`).
  - The final ACK comes only after verification. A bad image gets `ERROR 0 image rejected: <reason>`.
  - Limits: 64 MiB, `blksize` up to 1468, one session at a time.
- **Rescue network** (`:426-431`): the static baked `S0_IP`/24 and MAC. There is no DHCP.
  Identify is unicast only: "ask 192.168.10.101 itself" (`lx:docs/LINUX_HARNESS.md:226`).
- **Rescue reasons** (`:196`): ddr calib fail, no card, no usd_spi block, card unsupported, card
  error, no stage0 slots, no valid slot, slots exhausted, watchdog loop.
- **First install on a blank card** is a human runbook, rehearsed in QEMU only
  (`lx:docs/LINUX_HARNESS.md:259-315`):
  1. Rescue-push the image into RAM.
  2. Claim (on tmpfs).
  3. Over SSH: `dd` the MBR and boot-select made by `stage0_mkcard.py card`, `partprobe`,
     `mps3-persist format --erase`.
  4. `slot push`, `commit`, `reboot`.
  5. Claim again: the RAM boot's claim and host key were on tmpfs.
  6. `usd format`.
- **Cold-boot rules:**
  - MIG `sys_rst` is `~USER_nPB0` only, so a stuck DDR calibration needs a person at PB0
    (`lx:fpga/shell/bd/cpu_mbv.tcl:31-32`).
  - The mint's own bake has no cold-start fix: never field it
    (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:223-224`).

**`linux_bundle.json` and FLOW_CONTRACT** (`lx:docs/planning/linux_lanes/FLOW_CONTRACT.md:38-106`):
- Two doors on one static:
  - `mcc_sd`: `config_rm_greybox_stage0.bit` with stage0 baked in;
  - `ethernet`: `linux_slot.img`, the overlays and `linux_legal_info.tar`.
- One image serves both the rescue push and the slots (`:47`).
- **Only board 1's pair has a bundle.** Board 2's bakes are named by none
  (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:154-155`).

**Per-board identity** (`lx:docs/planning/linux_lanes/STAGE0_CONTRACT.md:96-103,272-291`):
- Stage0 build knobs: `S0_LABEL` (≤ 8 characters), `S0_IP`, `MPS3_MAC0..5`, or `S0_BOARD=mps3_0N`.
- Linux uses, in order: the `/persist/etc/mps3/identity` override, then the stage0 bake, then
  the image default (`lx:docs/contracts/net-protocol.md:13-20,1268-1288`).
- `identity_set` answers `no_persist` without a card (`net-protocol.md:1324`).
- **Today every new board gets its own config-SD bitstream** (step 1 of the upgrade:
  `om:docs/planning/RELEASE_NOTES_v2.0.0.md:186-191`). `updatemem` changes only BRAM, so the
  static and the overlays stay shared.
- **A generic bake exists:** label `MPS3`, 192.168.10.101, 02:00:00:4D:50:53, "for the public
  v2.0.0 bundle (david: YES, 30 Sep)". It is "NOT yet loaded on a board"
  (`ln:generic/out/README.txt:1-9`).
- **MAC allocation: no registry or scheme exists.** DNA-derived MACs are a mint-4 idea (D6)
  (`lx:docs/planning/linux_lanes/BACKLOG_2026-09-28.md:54,148`; `lx:firmware/platform/README.md:330-334`).

**What is proven on silicon, and what is not:**

| Proven | Not proven |
|---|---|
| A config-SD `.bit` write over USB-MSC on the hub, 68-79 s (fpgahub/pyverify) | HM's local `sd install` and `mcc reboot` over a laptop's Debug USB |
| Paced MCC REBOOT on `tty_00`, also through HM on the hub (1 Oct) | A full-tree install (`config.txt`, `board.txt`) onto any card, and onto a factory card |
| Cold boot from the card, 6/6 (board 1) | A Windows or macOS host on a real Debug USB |
| Rescue push into RAM: 54.7 s, then run mode 75 s later (board 1) | `stage0_mkcard.py --card-img` written on a PC (`BACKLOG_2026-09-28.md:216`) |
| The blank-card runbook, in QEMU (`LINUX_HARNESS.md:313-315`) | The blank-card runbook on silicon, by HM |
| | The generic stage0 bake on any board |
| | Config SD A/B by pointer (`F0FILE` ≠ `nanosoc.bit`) |
| | `reboot.txt` (USB-MSD reboot) |

---

## 3. The gaps

Each gap has an owner, a size and its dependencies. "HM" means a Harness Manager agent lane.
"Linux lead" means the platform's Linux harness owner. Hours are working hours, estimates.

### Summary

| # | Gap | Owner | Hours | Depends on |
|---|---|---|---|---|
| G1 | Signing keys | david (ceremony), HM (keys PR) | david 0.5; HM 1.5 | D1 |
| G2 | Publishing | lead (release), david (repos, access) | lead 3-4; david 0.5 | G1, D5, RELEASE-PIPE |
| G3a | Linux card, via a card reader | Linux lead + HM | Linux lead 4-8; HM 4-6 | D2; SD-FLASH; a contract fix (3.3) |
| G3b | Linux card, via rescue (L3) | Linux lead + HM | Linux lead 2 h (doc) + 1 d (`slot init`, if D2 says so); HM 10-12 | D2; Linux lead's answers |
| G4 | Per-board identity at install | HM + david | HM 3-4 | D4 |
| G5 | Licences | david | 1-2 for the decision; legal **UNVERIFIED** | — |
| G6 | Windows and macOS | HM | HM 6-8 + two test sessions of 2 h | a real board on each OS |
| G7 | MCC firmware versions | HM | 1 | — |
| G8 | Recovery from an interrupted write | HM | 2-3 | SD-FLASH, BRINGUP-USB |
| G9 | The network | HM + david | HM 2 | D4 |
| G10 | The bundle's overlays after a folder install | HM | 1-2 | BRINGUP-USB |

### 3.1 Signing keys (G1)

- **Today:** no key exists, `trust.PINNED_KEYS` is empty, and every channel is refused
  (`docs/KEYS.md:3-7`; `src/harness_manager/services/update/trust.py:83`). That is deliberate:
  never weaken it.
- **The ceremony** is david, about 30 minutes, once (`docs/KEYS.md:24-39`):
  - a root key, cold, on two USB sticks;
  - a release key, passphrase-protected, on david's machine;
  - a PR that fills `PINNED_KEYS`.
- **The bootstrap:** an install of 0.1.0 pins no keys. Each user needs one manual installer run
  to reach the first keyed build (`docs/KEYS.md:38-39`).
- **MVP:** david accepted "HM ships unsigned" for v0.1.0
  (`mem:mvp-plan-2026-09-27.md`, "Defaults david accepted"). So the keys are a P1 item, after Tue 6 Oct.
- **Owner and size:**
  - david: 0.5 h;
  - HM: the keys PR + tests, 1 h, then a bootstrap release, 0.5 h.
- **Depends on:** D1.

### 3.2 Publishing: where, who can read, tokens (G2)

**Where** (`docs/RELEASING.md:146-168`; `docs/design/HARNESS_DISTRIBUTION.md:163-191`):
- The app and the open harness parts go on **private GitHub Releases on `SoC-Labs/HarnessManager`**:
  one rolling `channel-<catalogue>-<channel>` release, plus one release per version.
- The Arm-IP overlays (nanosoc, multicore, upy, eth_ss) go on **private
  `SoC-Labs/mps3-harness-aaa`**.
- The hub is a mirror, looked up by sha256.

**Who can read:**
- The collaborators of each repo. Users already need GitHub access to install HM
  (`docs/USER_GUIDE.md:46-47`).
- The AAA list is narrower on purpose (`docs/design/HARNESS_DISTRIBUTION.md:172-176`).
- Whether `SoC-Labs/mps3-harness-aaa` exists yet: **UNVERIFIED** (no network here).

**Tokens:**
- `$HARNESS_MANAGER_GITHUB_TOKEN`, else `gh auth token` (`src/harness_manager/services/update/github.py:20-21,48`).
- Without a token that can read AAA, the Arm overlays are skipped (`docs/USER_GUIDE.md:1307-1308`).
- A mirror needs no token (`docs/RELEASING.md:21`).

**What is missing:**
1. The keys (G1). `--publish` refuses an unpinned key (`docs/KEYS.md:63-64`).
2. A platform bundle producer (R1, `make -C fpga/dfx release-bundle`) does not exist. Bundles
   are assembled by hand from the mint (`docs/RELEASING.md:101-108`). RELEASE-PIPE's script
   (tonight) wraps the tool, not the producer.
3. **Which Linux bundle to publish.**
   - Only board 1's pair has a `linux_bundle.json`, and its stage0 says `MPS3-01`.
   - The public bundle needs the **generic** bake, which has never booted a board
     (`ln:generic/out/README.txt:9`: "Proof opportunity: Fri rehearsal item 2").
4. The licence for anything public (G5).

**Owner and size:**
- lead: the first beta release (bare-metal v1.1.0 and Linux v2.0.0 generic), 3-4 h;
- david: create the AAA repo and its access list, 0.5 h.

**Depends on:** G1, D5, the generic-bake proof.

### 3.3 Linux OS provisioning on a blank card (G3)

**The fact that shapes both doors.** Stage0 never writes the card
(`lx:docs/planning/linux_lanes/STAGE0_CONTRACT.md:69-70`). So "rescue over TFTP writes the
card" is not a stage0 feature, and will not become one without a stage0 rebuild. The card
gets its layout and slots either from a PC (a) or from Linux running in RAM (b).

#### (a) A card reader on the laptop (SD-FLASH)

- **The contract bug.** Tonight's brief has `kind: "image"`: "a raw image (the Linux
  `linux_slot.img`) onto the whole device". That writes the slot image at byte 0. Stage0 reads
  the MBR at LBA 0 and the slots at LBA 67584 and 198656. **The result is rescue "no MBR"**
  (`STAGE0_CONTRACT.md:184,196,377-400`).
- **What a card-reader write must contain:**
  - the MBR (LBA 0);
  - the boot-select sectors (LBA 1-2);
  - slot A at LBA 67584 (and optionally slot B at 198656);
  - ideally a formatted `/persist` (p3).
- **The platform tool exists:** `stage0_mkcard.py card --slot-a IMG --card-img card.img`
  writes a **sparse** whole-card image (`lx:src/linux_soc/hw/fw_stage0/stage0_mkcard.py:15-18,143-161`).
  Its persist partition is 256 MiB by default, so a card image is ~417 MiB, but only ~59 MB
  of it is data.
  - **Not proven on silicon** (`BACKLOG_2026-09-28.md:216`).
  - **It leaves p3 all zeros**, so the first claim and host key live on tmpfs until
    `mps3-persist format --erase` and a reboot: two claims (`:216`).
  - The `--persist-fs` fix is a v2.1 backlog item (`:217`).
  - Windows and macOS cannot make ext4, so the format has to come inside the image.
- **Recommended contract change (a CCR for SD-FLASH and BRINGUP-USB):**
  - rename the raw kind `card`, sourced from a **card image the platform publishes**
    (`linux_card.img`, made by `stage0_mkcard.py card --card-img --persist-fs`, named with its
    sha256 in `linux_bundle.json`), never from `linux_slot.img`;
  - make the writer skip holes (a sparse image), and read back only the extents it wrote;
  - SD-FLASH refuses an S0LB file (magic `S0LB` at byte 0) offered as `card`, with the reason
    "this is one OS slot, not a card image".
- **Owner and size:**
  - Linux lead: `--persist-fs`, the card image in the bundle, one real card proven on a PC
    reader, 4-8 h;
  - HM: the `card` kind and the S0LB refusal, plus the wizard step, 4-6 h after the contract.
- **Time for the user:** ~1-2 min to write in a USB card reader (**UNVERIFIED**: no card
  write was measured), then the card goes into the board and the board REBOOTs (~3 min).

#### (b) Rescue over the network (HARNESS-DIST L3)

The sequence exists as a human runbook (`lx:docs/LINUX_HARNESS.md:228-315`), rehearsed in QEMU.
HM would drive it:

| # | Step | HM verb today | Time |
|---|---|---|---|
| 1 | unicast identify at the baked IP → `mode:"rescue"`, reason `no card` or `no valid slot` | identify: yes | 1 s |
| 2 | TFTP WRQ of `linux_slot.img`, `blksize` 1468, `tsize`; wait for the final ACK | **no** (pyverify `netboot` exists: `lx:docs/LINUX_HARNESS.md:170-172`) | 55 s |
| 3 | wait for identify `mode:"run"`; `slot status` `running:"rescue"` | partly | 75 s |
| 4 | claim (lands on tmpfs) | yes | 30 s |
| 5 | the layout: over SSH `dd` MBR + boot-select, `partprobe`, `mps3-persist format --erase`; **or** a new `slot init` act | **no** | 1 min |
| 6 | `slot push` → `commit` | yes | 12-50 min |
| 7 | MCC REBOOT; stage0 boots the slot | yes | ~3 min |
| 8 | claim again (new host key), `usd format` | yes (`--replace-host-key`) | 1 min |

**Traps a program must handle** (`lx:docs/planning/linux_lanes/HM_ANSWERS_2026-09-26.md:85-96`):
- never send the claim's TFTP while identify says rescue: stage0 takes any WRQ as a boot image;
- the RAM claim is on tmpfs;
- only one slot holds an image after the first card boot;
- the rescue image must include `cd11647` (the early watchdog kick).

**Owner and size:**
- Linux lead: an L3 section in the host contract, 2 h; a `slot init` act with a confirm token,
  ~1 d, if D2 says so (`HM_ANSWERS_2026-09-26.md:96,177`);
- HM: an L3 adapter (rescue push, run wait, layout or `slot init`, push, commit, reboot,
  re-claim), its fake, the wizard steps and tests, 10-12 h.

**Depends on:** D2, and the answers to the questions below.

#### The questions for the Linux lead (exact)

Rescue, as written today (`STAGE0_CONTRACT.md:426-465`), already answers the brief's first
four questions:
- **Can it accept a slot image over TFTP?** Yes.
- **Does it write it to the card?** No.
- **Size?** ≤ 64 MiB.
- **Verification?** CRC of the table, then every region, in DDR.
- **How is it confirmed?** The final ACK, then `booted_from=3`, then identify `mode:"run"`.

What HM still needs:

**Contract**
- **Q1. Rescue stays read-only.** Will stage0 stay read-only on the card for v2.x and mint 4?
  If yes, HM builds card provisioning only on Linux-in-RAM (door b) and on the PC (door a),
  never "stage0 writes the slot".
- **Q2. `slot init`.** For blank cards over Ethernet, will you build `slot init` (your change 11)
  for v2.1, or should HM run the SSH `dd` recipe (`LINUX_HARNESS.md` §6.4) as its L3 step 5?
  If you build it, HM needs:
  - the op name and the confirm token;
  - exactly what it writes (MBR, boot-select, `/persist` format, D13 format?);
  - its refusals (`code`s for a card that is not blank, a foreign card, no card);
  - and whether it is claim-locked.
- **Q3. The card image.** Will the release bundle carry a whole-card image
  (`stage0_mkcard.py card --card-img`, with `--persist-fs`) named in `linux_bundle.json` with
  its sha256? Or `mbr.bin` + `bootsel.bin` that HM places itself? Also:
  - What card sizes are supported?
  - With `--persist-fs` at a fixed size, is the rest of a large card left unused?
  - Is a sparse write (skip the zero extents) safe, or must p3 and D13 be zeroed?
- **Q4. One claim.** With `--persist-fs`, does the first card boot keep its host key and claim
  (a single claim)? Until v2.1, what exact text should HM show for the second claim?

**Which image, and how HM knows it is done**
- **Q5. The image to push into RAM.** For provisioning, should HM push `linux_slot.img`
  (persist on) or the `n` netboot build (persist off)? FLOW says one image serves both
  (`FLOW_CONTRACT.md:47`), but the fielded netboot image is a separate v7n build.
- **Q6. "Ready for card writes".** After an accepted push, which fields prove Linux-in-RAM is
  ready? Is it identify `mode:"run"` + `slot status` `running:"rescue"` + `confirmed`
  (`lx:docs/contracts/net-protocol.md:1503,1510`)? And after the first card boot, is
  `running:"B"`, `default:"B"`, `confirmed:true` the full proof?
- **Q7. Wrong static.** If HM pushes an image provisioned for another static, does stage0's
  rescue refuse it (which `image rejected` reason?), or does it boot and then hit the identity
  lock?

**Network and identity**
- **Q8. Finding a rescue board.** Rescue identify is unicast to the baked IP only
  (`lx:src/linux_soc/hw/fw_stage0/stage0_net.c:236`). Will a later bake also answer a broadcast
  identify, so HM's scan finds a rescue board whose IP it does not know? If not, HM needs the
  IP from the user, or from the bundle's `stage0_bake.json`.
- **Q9. The generic bake.** When is it proven on a board (Fri rehearsal item 2)? For a new
  board, do you recommend the generic bake plus `identity_set` over a per-board bake? With the
  generic bake, rescue always uses .10.101 and 02:00:00:4D:50:53, so two rescue boards cannot
  share a segment. Is that acceptable for v2.x?
- **Q10. Card detect.** Is `MPS3_USD_CD_POL=0` right on every HBI0309C board, or is an inverted
  detect a per-board risk HM should diagnose (rescue "no card" with a card in)?

**Speed and trust**
- **Q11. Slot write speed.** When will the slot-speed fix (189924d, modelled 9.3 min) be on
  silicon and on `master`? HM's budgets come from `mps3.slot.*`, and the wizard shows the time
  it expects.
- **Q12. Verification.** The board checks CRCs only, with no sha256 or signature
  (`ln:hmupdate/ASSESSMENT.md:46`; backlog B9). Is HM checking the download's signed sha256
  before the push enough for a first install, until L5?

### 3.4 Per-board identity at install, and MAC uniqueness (G4)

**Today:**
- **Bare metal:** the IP and MAC are compiled in, the same on every board (192.168.10.101,
  02:00:00:4D:50:53) (`lx:firmware/common/net_proto.h:35-38`; `docs/design/BOARD_IDENTITY.md:32-33`).
  A per-board MAC needs a firmware rebuild. HM has no path for it.
- **Linux:** two ways.
  - **(i) Per-board stage0 bake.** 10 min in Vivado 2026.1 by someone with the platform tree,
    giving a per-board config-SD `.bit` (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:186-191`).
    Rescue then has the right IP and MAC.
  - **(ii) The generic bake + `identity_set`.** The override lives in `/persist`, so it needs
    a card and the claim. Rescue always uses the default IP and MAC.
- **MAC uniqueness:** no registry (NOT FOUND in either repo). Board 2 ran with board 1's MAC
  until its identity bake (`lx:docs/contracts/net-protocol.md:16-18`). The real fix is D6
  (DNA-derived MACs, mint 4).

**What HM could do, board-free:**
- Derive a stable, locally administered MAC from the MCC's USB serial number. HM already reads
  that serial from the MCC banner (`src/harness_manager_mps3/mcc.py:384-386,399`).
- Then set it with `identity_set` in the wizard's "name this board" step.
- **UNVERIFIED:** that the MCC serial is unique per board. Two boards' banners would prove it.

**Owner and size:** HM, 3-4 h: the wizard's identity step, the derivation, a clash check
against known boards (`src/harness_manager/services/board_identity.py`), and tests.
**Depends on:** D4.

### 3.5 Licences: what can ship to whom (G5)

| Part | Contains | Can ship | Source |
|---|---|---|---|
| Project code (platform) | SoC Labs code | Apache-2.0 | `om:README.md:303-311`; `om:NOTICE:13-26` |
| nanoSoC, multicore, upy, eth_ss overlays | **Arm Academic Access IP** | registered users only, through the private AAA repo | `om:docs/contracts/overlay-manifest.md:7-9,93-110`; `lx:docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md:421-422` (david, 23 Sep) |
| The static `.bit` and the locked DCP (kit) | **AMD IP** (MicroBlaze V, MIG, …), encrypted in the DCP; no Arm IP | the kit is "INTERNAL-ONLY"; the bitstream's terms are **open** (G6 "confirm the AMD bitstream terms") | `docs/design/DUT_BUILD_KIT_STORAGE.md:41-43,72-87`; `lx:docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md:461` |
| The IP in the statics | no-charge "Included" IP; no IP licence to build | — | `docs/design/DUT_BUILD_GUIDE.md:45` |
| `linux_slot.img` | Buildroot Linux (GPL) | only together with `linux_legal_info.tar` (208 MB) | `lx:docs/planning/linux_lanes/FLOW_CONTRACT.md:73` |
| The MCC's `.ebf` | Arm MCC firmware | never: we do not ship it, and HM never writes it | `lx:fpga/mps3_sd/templates/board.txt:16-17` |

- AMD's actual terms for redistributing a bitstream that contains MicroBlaze V or the MIG: NOT
  FOUND. This is a question for AMD's licence text or the university's legal office.
- **Owner:** david, 1-2 h to decide. **Recommendation** (D5): registered users only for now.

### 3.6 Windows and macOS (G6)

| Need | Windows | macOS | Linux |
|---|---|---|---|
| Find the config SD | a drive letter with the volume label `V2M-MPS3` (`src/harness_manager_mps3/sd.py:165-199`) | `/Volumes/V2M-MPS3` (`sd.py:202-206`) | `/dev/disk/by-label` + `/proc/mounts`. A headless box has no automount: `udisksctl mount` (`docs/assessment/2026-09-24/Q3_INSTALL.md:214-216`) |
| Name the MCC port | usbser/WinUSB: `MI_00` in the location; FTDI VCP driver: channel letter `A` on the serial number (`src/harness_manager_mps3/usb.py:22-33,155-169`) | **no rule in `usb.py`**: an FT4232H without a location or letter falls to "interface numbers unknown", and no MCC link is offered (`usb.py:30-33,176-183`). **UNVERIFIED** what pyserial reports on macOS | sysfs `:1.0` |
| Driver for the FT4232H | the FTDI VCP driver or Windows' own; **UNVERIFIED** which a fresh Windows 11 picks | the built-in FTDI driver; **UNVERIFIED** | built in; the user needs `dialout` |
| Write files to the config SD | no privilege | no privilege | the mount needs udisks or root |
| Raw card write (Linux card image) | Administrator (`\\.\PhysicalDriveN`) | `sudo` for `/dev/rdiskN`, after `diskutil unmountDisk` | root, or a udisks policy |

- HM never escalates. SD-FLASH returns `needs_privilege` with the exact command (the brief's contract).
- Two MPS3s on one laptop give two `V2M-MPS3` drives. HM refuses and asks which
  (`src/harness_manager_mps3/sd.py:439-441`).
- No Windows or macOS note exists in the platform repos (NOT FOUND).
- **Owner and size:** HM, 6-8 h for fixes (the macOS port numbering is the likely one),
  plus two 2 h test sessions with a real board: one on Windows 11, one on macOS.
- **Depends on:** a board and a person with each OS (P3).

### 3.7 MCC firmware versions and other boards (G7)

- **Seen:** v1.3.2 only, on both lab boards (`pB_mcc_log_20260923.txt:4-5`).
- **Known differences** (`lx:docs/planning/linux_lanes/mint4/10_mccif.md:10,181-185,225-226`;
  untracked in `lx`):
  - `WDOG_CBRST` is v1.3.7+;
  - `WDTRESET` is not supported;
  - `ACLKREG`/`BREVREG`/`CPUWAITREG` are v1.3.6+, and how 1.3.2 treats them is untested;
  - upgrades need a person.
- **An HM bug, found here:**
  - the board reports `v1.3.2` (`src/harness_manager_mps3/mcc.py:396-397`; `tests/fakes/fake_mcc.py:65`);
  - the release tool writes `mcc_fw_tested: ["1.3.2"]` (`tools/release/harness.py:236`);
  - the planner compares the raw strings (`src/harness_manager/services/update/planner.py:452-455`).
  - **Every real board would get "MCC firmware v1.3.2 was not tested".**
  - Fix: normalise a leading `v` on both sides. HM, 0.5 h + a test and its twin.
- **Board revisions:**
  - a config SD for another `HBI0309x` is a planner blocker (`planner.py:447-451`);
  - the A/B door hard-codes `MB/HBI0309C` (`src/harness_manager_mps3/sd_ab.py:49`);
  - so does the hub's `sd_install` (`lx:host/fpgahub/mps3_01_pl.shell-update.toml:80`);
  - A/B/C bundles differ only in `BOARD:` (`lx:fpga/mps3_sd/README.md:84-90`).
- **A new board with a newer MCC:** HM warns, never updates the MCC, and parses the banner
  lines it knows. Whether a newer MCC's prompt or banner differs: **UNVERIFIED**. The first P1
  board shows it.
- **Owner and size:** HM, 1 h (the bug + a banner capture fixture from the first real board).

### 3.8 Recovery when a write is interrupted (G8)

| What was interrupted | What is left | The way back | Built? |
|---|---|---|---|
| Config SD over USB (power, cable, crash) | the journal on the SD; maybe a torn `nanosoc.bit` | the red "Interrupted SD install" card → **Restore the SD** from the backup zip, then REBOOT (`docs/USER_GUIDE.md:293-297`; `src/harness_manager/web/static/js/sections/sd.js:15-40`) | yes |
| The same, and the drive no longer appears | a dark board | take the config microSD out, unzip the backup to it in a PC reader (`docs/USER_GUIDE.md:1696-1698`). With SD-FLASH: write the backup's files in the reader | the manual step yes; HM-assisted: SD-FLASH `files` |
| Config SD with A/B by pointer | the running image is never touched | nothing: the pointer was not flipped (`src/harness_manager_mps3/sd_ab.py:12-15`) | built, off until the board check |
| Linux card in a reader | a torn MBR or slot | the board goes to rescue (no valid slot) and cannot brick. Write the card again | follows from stage0 (`STAGE0_CONTRACT.md:25-43,69-70`) |
| Linux slot push over Ethernet | a half slot; the other slot and the default are untouched | push again; the board never writes the running or the default slot (`src/harness_manager/services/update/os_slots.py:17-19`) | yes |
| A REBOOT during a card job | "uSD init error" on B2 | HM refuses a reboot while a card job runs (`src/harness_manager_mps3/os_slots.py:77-79`) | yes |

- **Gap:** the wizard has to resume. On reopening, it checks the SD journal, the card state and
  identify, and then offers the next step, never a repeat write.
- **Owner and size:** BRINGUP-USB and SD-FLASH, 2-3 h.

### 3.9 The network (G9)

- **This PC needs a NIC on the board's /24:**
  - the guide uses `192.168.10.1/24` (`docs/USER_GUIDE.md:265-267`);
  - the Linux doc uses `.10.2` (`lx:docs/LINUX_HARNESS.md:206-211`);
  - a laptop with no Ethernet port needs a USB Ethernet adapter.
- **What each image does on a new network:**
  - bare metal: static .10.101 only;
  - stage0 rescue: its baked static only, no DHCP;
  - Linux: DHCP first, then the static as a secondary after a duplicate-address check
    (`docs/design/BOARD_IDENTITY.md:25`).
- **Scan:** HM broadcasts identify and also asks 192.168.10.101 directly
  (`src/harness_manager_mps3/identify.py:34-39`). That finds a default board, a rescue board at
  the default IP, and a Linux board on the same segment. It does not find bare metal, or a
  rescue board at another baked IP.
- **Gap:**
  - the wizard checks "this PC has no address on 192.168.10.0/24";
  - it then shows the exact command for the OS (`ip addr add`, `networksetup`,
    `netsh interface ip set address`);
  - HM never changes the network itself.
- **Owner and size:** HM, 2 h.
- **The plan for new boards:** D9.

### 3.10 The bundle's overlays after a folder install (G10)

- `sd install` writes only the SD tree.
- The bundle's `overlays/open/` are not in HM's store, so **Program** and `restore` (which needs
  the greybox) find nothing (`docs/USER_GUIDE.md:686-688`).
- The catalogue path stores them (`host-store`).
- **Gap:** the wizard adds `BUNDLE/overlays/open` to the overlay dirs, or imports them.
- **Owner and size:** HM, 1-2 h.

---

## 4. Risks and safety rules

| # | Risk | What prevents it | Status |
|---|---|---|---|
| R1 | **The wrong disk.** A raw write to the laptop's own disk, or the board's DAPLink drive taken for the config SD | SD-FLASH lists removable card readers only, never the system disk or the `V2M-MPS3` drive, and asks for the typed `WRITE <model> <size>` (brief). `sd.py` refuses the DAPLink by label and by content (`src/harness_manager_mps3/sd.py:9-16,457-480`) | config SD: built. Card reader: tonight |
| R2 | **A second reader on the MCC console splits the REBOOT** | HM sends nothing without an intact `Cmd>`. On the hub, a `/proc` scan refuses while anything holds or names `tty_00` (`docs/USER_GUIDE.md:1734-1735`; `lx:host/pyverify/pyverify/bootrate.py:562-593`). On a laptop: **UNVERIFIED**. HM cannot see another app (PuTTY, screen) holding the COM port except by failing to open it | hub: built. Laptop: the open fails, and the wizard must say "close the other serial program" |
| R3 | **The hub's `--method sd` client timeout.** The client gives up at 30 s while fpgahubd writes for ~68 s. A retry plus a reset left the board dark once | one request, `--force`, completion proven by the hub's sha, never retried (`src/harness_manager_mps3/hub_sd.py:24-34`). A local USB write has no client timeout (`sd.py:22-30`) | built |
| R4 | **`.ebf`: an MCC reflash** | HM and the release tool never write or delete an `.ebf` (`sd.py:31-34`; `tools/release/harness.py:37`). **But our `board.txt` names `MBBIOS: mbb_v141.ebf`.** On a factory card that holds that file, the next boot may reflash the MCC 1.3.2 → 1.4.1 (`lx:docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md:465-468`). A power cut during that reflash is unrecoverable from HM (**UNVERIFIED** severity) | **OPEN.** HM preflight to add: refuse or warn when `board.txt`'s `MBBIOS` names an `.ebf` that is on the card, 1-2 h. Platform: G8 (drop or match the `MBBIOS` line). D9 |
| R5 | **The DUT's SST26 flash** | bring-up never writes it. The M0 flash loader is a separate, approved procedure (`mem:board-identity-per-board.md`). No bring-up step touches 6921 | rule |
| R6 | **No backup, or a lost one** | `install` refuses without a verified backup of the SD as it is now (`sd.py:17-21`). The wizard keeps the zip and its sha256 off the board, and shows the path | built |
| R7 | **Typing during the MCC's auto-boot window stops the FPGA load** | HM listens 3.5 s for silence before a read, and never types in a banner (`src/harness_manager_mps3/mcc.py:36-40,119-127`) | built |
| R8 | **Claiming while stage0 is in rescue.** Stage0 takes any TFTP write as a boot image | the L3 adapter waits for `mode:"run"` before it claims (`lx:docs/planning/linux_lanes/HM_ANSWERS_2026-09-26.md:91-92`) | L3: to build |
| R9 | **The wrong stage0 bake** | never field the mint's own bake: it has no cold-start fix (`om:docs/planning/RELEASE_NOTES_v2.0.0.md:223-224`). The release tool refuses a non-fieldable bundle (`docs/RELEASING.md:123`) | built |
| R10 | **A cold REBOOT that sticks DDR calibration** | the stage0 cold fix (10 s settle) proved 6/6 on board 1. A stuck calibration still needs PB0 at the board (`lx:fpga/shell/bd/cpu_mbv.tcl:31-32`). Board 2 has no MCC REBOOT without PB0 cover | rule; mint 4 fixes it |
| R11 | **Two boards with one MAC or IP on one segment** | HM's clash check (`docs/design/BOARD_IDENTITY.md:86-101`); one new board at a time on a direct cable (D9) | partly |
| R12 | **The config SD left mounted** starves the MCC, and the board goes dark | HM mounts nothing. SD-FLASH unmounts before a raw write, and only a card-reader device (`pf:docs/internal/BOARD_HANDOFF_NOTES.md:75-78`) | rule |

**Rules every bring-up step keeps:**
1. One write at a time. A slow write is not a failed one, and is never retried.
2. Back up before any config-SD write.
3. Never write or delete `.ebf`, and never write an MCC command file.
4. One reader on `tty_00`, CR first, paced.
5. Never touch the DUT flash.
6. HM never escalates privileges, and never changes the network.

---

## 5. A phased plan with dates

Today is Thu 1 Oct. The MVP ships **Tue 6 Oct** (platform v2.0.0, HM v0.1.0, guide 1.0), with
Thu 8 as a buffer (`mem:mvp-plan-2026-09-27.md`). The bring-up work must not disturb it.

### P0: tonight and Fri 2 Oct (the lanes, then integration)

| What | Who | Delivers | Hours |
|---|---|---|---|
| BRINGUP-USB | HM lane | the wizard over the Debug USB: probe, pick a release or folder, back up, install, reboot + witness, the "SD flashing" switch | tonight |
| SD-FLASH | HM lane | `cardwriter`: `files` onto a mounted FAT card, the raw kind, `needs_privilege` | tonight |
| RELEASE-PIPE | HM lane | `scripts/publish_harness_release.sh`, the release tool's checks | tonight |
| BRINGUP-SCOPE | HM lane | this document | tonight |
| Integration + the CCRs below | lead | fix the raw kind (3.3a); the `v1.3.2` bug (3.7) | Fri, 3-4 h |
| The generic stage0 bake on board 1 | Linux lead | the public Linux bundle's first proof (`ln:generic/out/README.txt:9`) | Fri rehearsal |

**From Sat 3 to Tue 6 (the MVP freeze):**
- the wizard ships behind a setting, off, or waits for 0.1.1 (D7);
- no bring-up step runs on a board.

### P1: Wed 7 - Fri 9 Oct (keys, a first release, a real board)

| What | Who | Hours | Needs |
|---|---|---|---|
| The key ceremony | david | 0.5 | D1 |
| The keys PR + a bootstrap build | HM | 1.5 | the ceremony |
| The first beta harness release: bare metal v1.1.0, and Linux v2.0.0 with the generic bake | lead (david signs) | 3-4 | G2, D5 |
| **Real-board USB test** (below) | david + HM agent on call | 1.5-2 | D8; a board's Debug USB on david's laptop |
| Fixes from that test | HM | 4-8 | the test |
| The `MBBIOS`/`.ebf` preflight (R4) | HM | 1-2 | D9 |

**The real-board USB test, in order.** About 90 minutes. Bare metal first, so a failure costs
one SD restore.

1. `probe`: four ports, the drive, the MCC named.
2. `mcc - --serial … temp` (a read only).
3. Back up the config SD.
4. Install the bare-metal bundle.
5. REBOOT, witnessed, then `info 192.168.10.101`.
6. Restore the backup and REBOOT.
7. If time remains: the A/B pointer check (`updates.sd_ab`, 10 min).

Capture the MCC banner, the backup size and every time taken.

### P2: Mon 12 - Fri 16 Oct (Linux on a blank card)

| What | Who | Hours | Needs |
|---|---|---|---|
| Answers to Q1-Q12 | Linux lead | 1 | — |
| Door (a): `--persist-fs`, a card image in the bundle, one card written on a PC and booted | Linux lead | 4-8 | Q3, Q4; a board with PB0 cover |
| Door (a) in HM: the `card` kind, the wizard step | HM | 4-6 | the card image |
| Door (b): the L3 host contract; `slot init` if D2 says so | Linux lead | 2 h + ~1 d | D2 |
| Door (b) in HM: the L3 adapter, fake, wizard steps | HM | 10-12 | the contract |
| Identity step + MAC derivation (G4), network check (G9), overlays (G10), resume (G8) | HM | 8-11 | D4, D9 |
| A Linux bring-up on a board from a blank card, through HM | david + HM | 1.5 | the above |

Mint 4 freezes on 21 Oct (`mem:mint4-ethernet-only-plan.md`). P2 board time must not
collide with its proofs.

### P3: Mon 19 - Fri 23 Oct (Windows and macOS)

| What | Who | Hours | Needs |
|---|---|---|---|
| A test session on Windows 11 with a real Debug USB | a person + HM | 2 | a Windows laptop, a board |
| The same on macOS | a person + HM | 2 | a Mac, a board |
| Fixes (macOS port numbering, drivers text, raw-write commands) | HM | 6-8 | the sessions |

### Totals (estimates)

| Who | P1 | P2 | P3 |
|---|---|---|---|
| HM agents | 7-12 h | 22-29 h | 6-8 h |
| Linux lead | 0 | 1-2 d | 0 |
| david | 2.5 h | 1.5 h + decisions | 0-4 h (if he runs the sessions) |

---

## 6. Decisions for david

The recommendation is first in each. The decisions are grouped by what they gate.

### Gate P1

**D1. Release keys: when, and who holds them** (= KEYS.md U2).
- **(a) Recommended:** the ceremony Wed 7 Oct, as `docs/KEYS.md:24-39` describes:
  - the root key cold, on two USB sticks;
  - the release key on your machine;
  - you sign stable.
- (b) The same, and the lead also holds the release key, so betas need no hand-off.
- (c) Stay unsigned through October. The wizard installs bundle folders only.

**D3. Unsigned bundle folders in the wizard.**
- **(a) Recommended:** allow a folder or zip, with a typed `INSTALL UNSIGNED <sha8>`, a red
  "unsigned" banner, and never as the default once keys exist.
  - This is today's `sd install`, with the same checks.
  - A signed offline mirror (`harness mirror DIR`) stays the preferred offline route.
- (b) Signed only: a mirror directory or the catalogue, so nothing works until D1.
- (c) The CLI only. The wizard offers the catalogue alone.

**D7. When the wizard ships.**
- **(a) Recommended:** in **0.1.1**, after the P1 real-board test. If it merges before Tue, it
  goes in 0.1.0 behind a setting, off.
- (b) In 0.1.0 on Tue, marked experimental, untested on a real Debug USB.
- (c) After P2, with Linux.

**D8. Where the first real-board USB test runs.**
- **(a) Recommended:** your laptop at the board, on board 2's Debug USB (it is HM's board), with
  PB0 cover, about 90 minutes.
- (b) HM on the hub machine, on its local Debug USB.
  - fpgahub must let go of `tty_00` and the card first.
  - The hub account cannot mount the SD without root (`mem:sd-install-timeout-trap.md`).
  - Riskier.
- (c) Wait for a third board.

### Gate P2

**D2. The Linux blank-card door.**
- **(a) Recommended:** the card reader first.
  - It is the shortest path: ~10 min, versus 60 min today over the board.
  - It needs no new board code beyond `--persist-fs` and a card image in the bundle.
  - Then (b) in v2.1 for remote boards.
- (b) Rescue over the network (L3) first, with a `slot init` act. It works with no card reader
  and no hands at the board after the first REBOOT. ~1 d for the Linux lead + 10-12 h for HM.
- (c) Rescue over the network with HM driving the SSH `dd` recipe. No new board verb, but HM
  runs root commands on the board.
- (d) SoC Labs ships pre-written cards. No tooling, but no self-service.

**D4. Identity for a new board.**
- **(a) Recommended:**
  - the generic stage0 bake for every board;
  - the wizard's "name this board" step sets the label, IP and MAC with `identity_set`;
  - the MAC is derived from the MCC's USB serial number (locally administered).
  - One bitstream for all. Rescue keeps the default IP, so bring up one board at a time.
- (b) A per-board stage0 bake made by SoC Labs, 10 min of Vivado per board.
  - Rescue gets the right IP.
  - A per-board file to keep, and a person with the platform tree for every new board.
- (c) Wait for mint 4's DNA-derived MAC.

**D9. Network plan, and the `MBBIOS` line.**
- **(a) Recommended:**
  - a direct cable with the laptop at `192.168.10.1/24`, one new board at a time;
  - then `identity_set` to the board's lab address;
  - ask the platform to drop the `MBBIOS: mbb_v141.ebf` line from our `board.txt` (G8),
    so a factory card's `.ebf` is never named.
- (b) The lab LAN with DHCP reservations by MAC. Needs unique MACs first (D4).
- (c) Keep `MBBIOS` and rely on HM's preflight warning alone.

### Policy

**D5. Who gets what (the licence).**
- **(a) Recommended:** registered users only.
  - Private GitHub repos.
  - Arm overlays in the AAA repo, as agreed on 23 Sep.
  - The Linux image always with its legal-info.
  - Decide "public" after the AMD bitstream terms (G6) are read.
- (b) The open parts public now: needs the AMD terms and the notices first.
- (c) The hub only. Standalone users get nothing.

**D6. Bare metal in the wizard.**
- **(a) Recommended:** both. Linux is the default. Bare metal is offered as "the harness without
  Linux", because it is the fully built path today.
- (b) Linux only: bare metal is being retired (the deletion PR B10, ~26 Oct,
  `lx:docs/planning/linux_lanes/BACKLOG_2026-09-28.md:84`).
- (c) Bare metal only until P2.

---

## Appendix: CCRs this plan raises

| To | Change | Why |
|---|---|---|
| SD-FLASH + BRINGUP-USB | rename the raw kind `card`; its source is a card image, never `linux_slot.img`; refuse an S0LB file at byte 0 | 3.3a: a slot image at byte 0 never boots |
| HM (`planner.py` or `tools/release/harness.py`) | normalise a leading `v` in MCC firmware versions | 3.7: a false warning on every board |
| HM (`sd.py` preflight) | warn or refuse when `board.txt`'s `MBBIOS` names an `.ebf` on the card | R4 |
| platform (G8) | drop or correct `MBBIOS: mbb_v141.ebf` in `templates/board.txt`; fix the README's `V2M_MPS3` spelling | R4; 2.2 |
| Linux lead | answer Q1-Q12; `--persist-fs`; a card image in `linux_bundle.json`; the L3 section | 3.3 |
