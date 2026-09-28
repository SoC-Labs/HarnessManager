# Board locate: blink the panel so you can find the board

Lane LOCATE, 2026-09-28. What can blink on an MPS3, the harnessd `locate` verb as the Linux
lead confirmed it, what Harness Manager (HM) builds on it, and the requests left for later.
Nothing in the platform repos changed.

**Why.** There are now two MPS3s in the lab: `mps3_01_pl` at 192.168.10.101 and `mps3_02_pl`
at 192.168.11.101. On 2026-09-28 board 2 showed board 1's identity on its LCD. The cause:
the panel's name is a compile-time constant (`MPS3_BOARD_NAME "MPS3-01"`, `lx:firmware/clcd/clcd.h:86-100`),
and so is the default IP (`lx:firmware/common/net_proto.h:33-38`). david asked for a button
that makes the physical board blink for 5 seconds.

**Status.** The Linux lead confirmed the board side on 2026-09-28. It is `locate`, to HM's own
R3 (`CLCD_ALIGNMENT.md:242,364,420`), and it ships in images **rc2_v7/v7n (Tuesday evening)**.
HM is built against exactly that wire, modelled in its fake.

Source prefixes: `lx:` is `mps3-nanosoc-platform-lx` (`feat/linux-harness`, 6beea09);
`fw:` is `mps3-nanosoc-platform` (the fielded tree, b2b83d3). Both were only read.

---

## 1. What can blink on an MPS3 today

| What | Who drives it | Can the harness blink it? | Evidence |
|---|---|---|---|
| **CLCD backlight** (on/off, no PWM) | the CLCD KVM, because the harness sets `bl_rst_src=1` at init | **Yes, even while the DUT owns the panel.** `clcd_kvm_set_backlight()` is a safe read-modify-write of `CTRL[5]`. It costs 0 pixel bytes. **This is what `locate` blinks** | `lx:firmware/clcd_kvm/clcd_kvm.h:55-67,108-109`; `lx:firmware/common/platform_regs.h:765,768`; `CLCD_ALIGNMENT.md` §1.1 |
| **CLCD text** (a banner) | `clcd.c`: 40x15 text; rows 10-12 are the banner rows | **Only while the harness owns the panel.** The fault banners use rows 10-12 today | `lx:firmware/clcd/clcd.c:918-935` |
| **User LEDs `USER_nLED[7:0]`** (8 of the board's 10; active low) | **the static shell** owns the pads, through `board_gpio_0` at `0x44AA_0000`. `OWN` is a per-bit mux: 0 = the DUT's `dut_gpio_o/oe` drive the pad, 1 = the harness's `OUT/OE` do. `OWN` resets to 0, so **the DUT owns every LED by default**, through the RP boundary. Both engines take **LED0** (`OWN \|= 1`) for the 1 Hz heartbeat | **Possible, but not in `locate`** (the Linux lead: out of scope). A later request, §4.1 | `lx:fpga/shell/shell_top.sv:54-56,595-608`; `lx:fpga/shell/ip/board_gpio/board_gpio.sv:8-16,205,339-349`; `lx:fpga/shell/constraints/mps3_harness.xdc:71-82`; `lx:docs/contracts/shell-regmap.md:875,956-959`; `lx:docs/contracts/partition-pins.md:151-153`; heartbeat: `lx:firmware/platform/src/main.c:116-119,261-265`, `lx:src/linux_harness/sw/harnessd/main_linux.c:252-270` |
| `USER_nLED[9:8]` | nobody under the shell | **No.** Only the monolithic build pins them. Pinning them changes the static, which means a re-key and a mint | `fw:fpga/monolithic/nanosoc_mps3.xdc:189-211` vs `lx:fpga/shell/constraints/mps3_harness.xdc:74-82` |
| **MCC LEDs** | the MCC's own firmware | **No.** The FPGA cannot command the MCC. Its command set (REBOOT/RESET/SHUTDOWN, as console words or MSD command files) has no LED verb. HM does not touch the MCC for this | `fw:docs/internal/HANDOVER_ETH_MCC_CONTROL.md:15-17,82-86` |

**The Linux harness today:**
- harnessd reaches `board_gpio_0` through UIO. The device tree binds it as `generic-uio`
  ("gpio", harnessd-owned), so there is no sysfs `gpio` or `leds` node
  (`lx:src/linux_harness/shell_linux.dts:266-271`).
- The CLCD, the KVM and touch run inside harnessd (`CLCD_ALIGNMENT.md` §1.2).

**Bare metal (v0.11, fielded 0x72BB0A36)** has no `locate`. UDP 6899 `identify` answers
"what is at this address?" and blinks nothing (`lx:docs/contracts/net-protocol.md:1136-1180`).
DL4 freezes bare-metal platform code, so HM treats `locate` as Linux-only. On bare metal
the button is disabled, with the reason "needs harness feature 'locate' (Linux harness)".

## 2. The verb (confirmed by the Linux lead, images rc2_v7/v7n)

```
-> {"op":"locate","s":5,"who":"dam1n19@srv03335 via HM"}      (6900; s 1-30)
<- {"ok":true,"op":"locate","until_ms":5000}                   (until_ms: ms from now)

-> {"op":"locate","s":0}                                         (stop)
<- {"ok":true,"op":"locate","until_ms":0}
```

| Board behaviour | As confirmed |
|---|---|
| Blink | the **backlight** at 2 Hz, from the CLCD tick |
| Banner | "IDENTIFY: \<who\>", only while the harness owns the panel. While the DUT owns it: the backlight only |
| Stop | `s:0`, or **a tap on the panel** |
| Restore | the backlight goes back on at the end, and on harnessd start |
| Feature | `locate` in `version.features` |
| Access | **no claim lock**: any peer, so HM goes over the normal hub tunnel |
| Not in this image | `hello` and `panel` (R1/R2), user LEDs, a board-side rate limit, a ring entry |

**As shipped (V7-ALIGN, net-protocol v0.16, platform `feat/linux-harness` 18622e5,
`locate_linux.c`).** The answer is `{"ok":true,"op":"locate","until_ms":N}`: it carries `op`
(HM takes it with or without). `until_ms` is RELATIVE, ms from the answer to the end (0 =
stopped), never more than `s * 1000`; HM does not believe a larger value (an absolute epoch,
say) and counts the asked seconds (`harness_manager_mps3.panel.locate_ms`, `locate.js`
`countdownMs`). `s` is 0-30; `who` is at most **32** printable ASCII characters. Anything else
is `{"ok":false,"err":"invalid s: …"|"invalid who: …","code":"invalid"}`, which HM shows as
USAGE (it sent something wrong) without forgetting the image's features. A build without the
panel, and bare metal, answer `locate not supported`, code `not_supported` (UNAVAILABLE).

**What HM sends.**
- `s:5`. `who` is `"<user>@<host> via Harness Manager"`, shortened to `"... via HM"` when that
  does not fit 30 characters (the 40-column banner row, less "IDENTIFY: ": what the glass
  shows). See `core.panel.locate_who`. The adapter clips any `who` to the board's 32
  (`core.panel.LOCATE_WHO_WIRE_MAX`), so the board never refuses it.
- Stop sends `s:0`.
- Nothing else: no `leds`, and no `hello` or `panel`, which this image does not have.

## 3. What Harness Manager does with it (built, against the fake)

| Where | What |
|---|---|
| Sidebar board card, Board tile | an **Identify** icon button. It is enabled when the board reports `locate`; otherwise it is disabled, with the reason as its tooltip. One click sends `locate` for 5 s and counts down from the answer's `until_ms`. A small **Stop** square next to the count sends `s:0` |
| A second press while it runs | **ignored**: nothing is sent, and the blink is neither extended nor restarted. Stop is the only way to end it early from HM |
| Details > Front panel | the existing Identify control (5-30 s, and Stop) |
| CLI | `harness-manager identify TARGET [--seconds N]` (default 5; 0 stops) |
| Daemon `POST /boards/{bid}/identify` | at most one start per board every 10 s, whoever asks through this daemon: 409 ALREADY with `retry_after_s`. A stop is never limited, and a start that failed does not count. The answer carries `until_ms` (the board's) and `next_at`. It works on a board the daemon knows but has not open: the daemon opens it for the one request and closes it again, and never tracks it for presence |
| Lease | **not required**, because the board has no claim lock. When the hub lease is someone else's, the answer names the holder, and the board's banner shows them who asked |
| Never | automatic or background. Only a click or a command sends it (QUIET-POLL) |

**A tap on the glass stops the blink early.** This image has no panel read (R1/R2), so
HM cannot see that happen. Its countdown runs to the end, and the tooltip says so.

**Why no lease.** Identify changes nothing a user relies on: no bitstream, console, reset or
pixels, and at most 30 s of backlight. Its main use is telling boards apart *before* you
lease one. Three limits bound the risk: HM's 10 s limit, the 30 s cap, and the banner naming
who asked. One limit is not HM's to lift: a hub with `gate_ethernet` drops a non-holder's
traffic to the board NIC (`docs/HUB_MODE.md:223-225`). There, Identify fails UNREACHABLE with
the reason.

## 4. Later board-side requests (not in rc2_v7/v7n)

1. **User LEDs.** The static shell owns the LED pads (§1), so harnessd could blink them too.
   Add `"leds":"all"|"hb"` to `locate`:
   - **`all`**: snapshot `OWN`/`OE`/`OUT`, set `OWN|=0xFF` and `OE|=0xFF`, alternate
     `OUT[7:0]` between `0x55` and `0xAA` every 250 ms, then write the snapshot back.
   - **`hb`**: toggle LED0 only, which the harness already owns.
   - HM would send `hb` when the hub lease is someone else's: while LEDs 1-7 are borrowed,
     a DUT reading back its LED pins sees the chase (`shell_top.sv:606-608`).
   - At start, harnessd sets `OWN` back to the heartbeat bit.
   - Cost: one register write per tick.
2. **The holder hears of it.** Once `hello`/`panel` (R1/R2) exist, put a
   `{"k":"locate","who":…}` entry in the panel's event ring. Then the lease holder's HM can
   say "Identified by …" even if the 5 s fell between two 30 s beats.
3. **A tap-stop HM can see.** With R2, `panel` carries `locate: {who, until_ms}` while it
   runs, so a stop from the glass ends HM's countdown too.
4. **The `who` cap.** ANSWERED by the shipped image (18622e5): the wire takes at most 32; the
   banner draws "IDENTIFY: " plus `who` clipped at the row's 40th column, so 30 show. HM
   composes to 30 and clips anything else to 32.
