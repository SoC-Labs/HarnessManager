# Board locate: blink the LEDs and the panel so you can find the board

Lane LOCATE, 2026-09-28. A request to the Linux lead for one harnessd verb, plus what
Harness Manager (HM) already does with it. Nothing in the platform repos changed.

**Why.** There are now two MPS3s in the lab: `mps3_01_pl` at 192.168.10.101 and `mps3_02_pl`
at 192.168.11.101. On 2026-09-28 board 2 showed board 1's identity on its LCD. The cause is
that the panel's name is a compile-time constant (`MPS3_BOARD_NAME "MPS3-01"`, `lx:firmware/clcd/clcd.h:86-100`),
and so is the default IP (`lx:firmware/common/net_proto.h:33-38`). david asked for a button
that makes the physical board blink for 5 seconds.

**The ask, in one line.** Add a `locate` verb that blinks the user LEDs and the CLCD
backlight for `s` seconds, shows an `IDENTIFY` banner with the requester and the board's
live IP while the harness owns the panel, and records who asked in the panel's event
ring. Report it as feature `locate`. This is R3 of `CLCD_ALIGNMENT.md` §5.3, with the LEDs
and the ring event added.

Source prefixes: `lx:` is `mps3-nanosoc-platform-lx` (`feat/linux-harness`, 6beea09);
`fw:` is `mps3-nanosoc-platform` (the fielded tree, b2b83d3). Both were only read.

---

## 1. What can blink on an MPS3 today

| What | Who drives it | Can the harness blink it? | Evidence |
|---|---|---|---|
| **User LEDs `USER_nLED[7:0]`** (8 of the board's 10; active low) | the static shell's `board_gpio_0` at `0x44AA_0000`. Pads `[7:0]` are the LEDs; a LED lights when its pad is driven high (`pad_o & pad_oe`) | **Yes.** `OWN` is a per-bit mux: 0 = the DUT's `dut_gpio_o/oe` drive the pad, 1 = the harness's `OUT/OE` do. `OWN` resets to 0, so **the DUT owns every LED by default**. Both engines already take **LED0** (`OWN |= 1`) for the 1 Hz heartbeat; LEDs 1-7 belong to the DUT through the RP boundary | `lx:fpga/shell/shell_top.sv:54-56,595-608`; `lx:fpga/shell/ip/board_gpio/board_gpio.sv:8-16,205,339-349`; `lx:fpga/shell/constraints/mps3_harness.xdc:71-82`; `lx:docs/contracts/shell-regmap.md:875,956-959`; `lx:docs/contracts/partition-pins.md:151-153`; heartbeat: `lx:firmware/platform/src/main.c:116-119,261-265`, `lx:src/linux_harness/sw/harnessd/main_linux.c:252-270` |
| `USER_nLED[9:8]` | nobody under the shell | **No.** Only the monolithic build pins them. Adding them changes the static's pins, so a re-key and a mint. Not worth it | `fw:fpga/monolithic/nanosoc_mps3.xdc:189-211` vs `lx:fpga/shell/constraints/mps3_harness.xdc:74-82` |
| **CLCD backlight** (on/off, no PWM) | the CLCD KVM, because the harness sets `bl_rst_src=1` at init | **Yes, even while the DUT owns the panel.** `clcd_kvm_set_backlight()` is a safe read-modify-write of `CTRL[5]`. It costs 0 pixel bytes | `lx:firmware/clcd_kvm/clcd_kvm.h:55-67,108-109`; `lx:firmware/common/platform_regs.h:765,768`; `docs/design/CLCD_ALIGNMENT.md` §1.1 (backlight row) |
| **CLCD text** (a banner) | `clcd.c`, 40x15 text, rows 10-12 are the banner rows | **Only while the harness owns the panel.** The fault banners use rows 10-12 today. An identify banner must rank below them | `lx:firmware/clcd/clcd.c:918-935`; `CLCD_ALIGNMENT.md` §5.3 (`mps3_clcd_overlay()`) |
| **MCC LEDs** | the MCC's own firmware | **No.** The FPGA cannot command the MCC, and its command set (REBOOT/RESET/SHUTDOWN, as console words or MSD command files) has no LED verb. HM does not touch the MCC for this | `fw:docs/internal/HANDOVER_ETH_MCC_CONTROL.md:15-17,82-86` |

**The Linux harness, today:**
- harnessd reaches `board_gpio_0` through UIO. The device tree binds it as `generic-uio`
  ("gpio", harnessd-owned), so there is no sysfs `gpio`/`leds` node for anyone else
  (`lx:src/linux_harness/shell_linux.dts:266-271`).
- The CLCD, the KVM and touch run inside harnessd too (`CLCD_ALIGNMENT.md` §1.2).
- There is no `locate`, `hello` or `panel` verb. `version.features` ends at bit 15
  (`xvc_lock`) (`lx:firmware/common/net_proto.h:154-181`).

**Bare metal (v0.11, fielded 0x72BB0A36)** has no `locate` verb either. UDP 6899 `identify`
answers "what is at this address?"; it blinks nothing (`lx:docs/contracts/net-protocol.md:1136-1180`).
DL4 freezes bare-metal platform code, so HM treats `locate` as Linux-only. Bare metal shows
the button disabled, with the reason "needs harness feature 'locate' (Linux harness)".

## 2. The verb

```
-> {"op":"locate","s":5,"who":"david@srv03335"}                      (<= 256 B)
<- {"ok":true,"until_ms":5000,"leds":"all","panel":"banner"}

-> {"op":"locate","s":5,"who":"bob@lab-pc-03","leds":"hb"}           (not the lease holder)
<- {"ok":true,"until_ms":5000,"leds":"hb","panel":"backlight"}       (the DUT owns the panel)

-> {"op":"locate","s":0}                                             (stop; always accepted)
<- {"ok":true,"until_ms":0}
```

| Key | Request | Notes |
|---|---|---|
| `s` | int 0-30 | seconds. 0 stops a running blink. HM sends 5 |
| `who` | optional, printable ASCII, at most 20 chars (HM's `WHO_MAX`) | shown on the banner and recorded in the ring |
| `leds` | optional: `"all"` (default) or `"hb"` | `all` borrows LEDs 0-7. `hb` blinks LED0 only, which the harness already owns, and never touches the DUT's bits. HM sends `hb` when the board's hub lease is someone else's |

| Key | Reply | Notes |
|---|---|---|
| `until_ms` | int | ms until it stops, on the board's clock (0 after a stop) |
| `leds` | `"all"` \| `"hb"` \| `"none"` | what actually blinks. `none` = the GPIO block is absent (a bitstream without it) |
| `panel` | `"banner"` \| `"backlight"` \| `"none"` | `banner` = banner plus backlight; `backlight` = the DUT owns the panel, so only the backlight blinks; `none` = no CLCD in this build |

### What the board does for `s` seconds

1. **LEDs** (`leds:"all"`): snapshot `OWN`, `OE` and `OUT`. Set `OWN |= 0xFF` and `OE |= 0xFF`.
   Alternate `OUT[7:0]` between `0x55` and `0xAA` every 250 ms (a 2 Hz chase, which the
   1 Hz heartbeat never looks like). At the end, write the snapshot back exactly.
   For `leds:"hb"`: toggle LED0 every 125 ms, then hand it back to the heartbeat.
2. **Backlight**: toggle `clcd_kvm_set_backlight()` every 250 ms. At the end it must be on.
3. **Banner** (only while the harness owns the panel), rows 10-12 inverted, through the
   `mps3_clcd_overlay()` seam, ranked below every fault banner:
   ```
   row 10   >>>>>>>>>>>>  IDENTIFY  <<<<<<<<<<<<
   row 11   asked by david@srv03335         5 s
   row 12   192.168.11.101  02:00:00:4d:50:53
   ```
   Row 12 is the board's **live** IP and MAC, not `MPS3_BOARD_NAME`, because the name is
   what went wrong today. Painting it costs the same as today's fault banner. Blinking
   costs nothing more: the blink is the backlight, not a repaint.
4. **Ring event**: append `{"seq":N,"k":"locate","on":"","who":"david@srv03335","ms_ago":0}`
   to the panel's event ring (the ring R1/R2 already specify). The ring is never
   acknowledged, so **every** HM watching the board sees it at its next `hello` or `panel`
   read, even if the 5 s blink fell between two 30 s beats. That is how the lease holder
   learns someone identified the board.
5. While it runs, the `panel` reply (and a `hello` reply's `panel`) carries
   `"locate":{"who":"…","until_ms":N}`. It is absent otherwise.

### Refusals and limits

| Case | Answer |
|---|---|
| a start within **10 s** of the last start, whoever asks | `{"ok":false,"err":"locate: rate limited, retry in 7 s","retry_ms":7000}` (a stop is never limited) |
| `s` not an int 0-30, `leds` not `all`/`hb` | `{"ok":false,"err":"locate: bad s"}` |
| `mode:"nohw"` (no fabric) | today's `no fabric: <why>` |
| a swap has 6900 parked | today's EBUSY; HM says "busy (a swap)" |
| the DUT owns the panel | **not a refusal**: backlight only, reply `panel:"backlight"` |
| the lease is someone else's | **not a refusal on the board** (it cannot see the hub). HM sends `leds:"hb"` so another person's DUT LEDs are never borrowed |

**Restore rules (harnessd crash or restart).** At start, harnessd writes `OWN` to the
heartbeat bit alone, so a blink cut short never leaves LEDs 1-7 away from the DUT. Only
harnessd writes `OWN` (`shell-regmap.md:875`). It also turns the backlight on (clcd init
already does this).

**Feature.** `locate`, **appended** as the next free `version.features` bit after the ones
already taken (`xvc_lock` = 15; the lcd_mirror lane's name follows its own seam). Announce
it only when the verb is linked. Bare metal never sets it.

**Cost to the loop.** 2 register writes per 250 ms tick from the clcd service slot (GPIO
`OUT` and KVM `CTRL`), plus one banner paint while the harness owns the panel. No touch I/O.

### What it disturbs, said plainly

- **A DUT that reads back its LED pins** sees the chase on `dut_gpio_i[7:1]` for `s`
  seconds: the pad readback loops the driven value (`shell_top.sv:606-608`). Its own
  `dut_gpio_o` is untouched. `leds:"hb"` avoids this, so HM uses it on a board someone
  else holds.
- **A DUT that owns the panel** sees its picture blink with the backlight. Its pixels are
  untouched.

## 3. What Harness Manager does with it (built, against a fake)

| Where | What |
|---|---|
| Sidebar board card and the Board tile | an **Identify** icon button. Enabled when the board reports `locate`; otherwise disabled, with the reason as its tooltip. One click sends `locate` for 5 s and shows a 5-4-3-2-1 countdown. A second press while it runs sends nothing |
| Details > Front panel | the existing Identify control (1-30 s, and Stop) |
| CLI | `harness-manager identify TARGET [--seconds N]` (default 5; 0 stops) |
| Daemon | `POST /boards/{bid}/identify`: at most one start per board per 10 s (409 ALREADY, with `retry_after_s`). It also works on a board that is known but not open here: the daemon opens it for the one request (never tracked for presence) and closes it again |
| Lease | **not required** (see below). When the hub lease is someone else's, HM sends `leds:"hb"` and the answer names the holder |
| Holder's note | a `locate` ring event from someone else becomes `panel.locate {source:"board", who, mine:false}`. The holder's app shows "Identified by bob@lab-pc-03 at 14:02:05" |
| Never | automatic or background. Identify is an explicit click or command; no poll, beat or page load sends it (QUIET-POLL) |

**Why no lease is needed.** Identify changes nothing a user relies on: no bitstream, no
console, no reset, and at most 30 s of LEDs and backlight. Its main use is telling boards
apart *before* you lease one. The risk is bounded four ways:
- a board-side rate limit and an HM-side one (1 per 10 s);
- the 30 s cap;
- `leds:"hb"` on a board someone else holds;
- the holder is told who did it.

One limit is not HM's to lift: a hub with `gate_ethernet` drops a non-holder's traffic to
the board NIC (`docs/HUB_MODE.md:223-225`). There, Identify fails UNREACHABLE with the
hub's reason.

## 4. Open questions for the Linux lead

1. Does the 250 ms tick come from the clcd service slot or its own slot? Either works; HM
   only reads `until_ms`.
2. Is `0x55`/`0xAA` distinct enough from the DUTs we ship? `rm_led` drives the low 4 bits
   from a counter (`fw:fpga/dfx/rms/rm_led/rm_led.sv:39,163-166`).
3. Should the `who` on row 11 be clipped to 16 characters (the panel's session width), as
   the `hello` row is?
