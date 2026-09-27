# A pixel-exact, live mirror of the MPS3 LCD in Harness Manager

> **Status:** design, lane LCD-MIRROR, 2026-09-26. Built so far: LM1 (the core:
> `core/display_wire.py`, `core/display.py`, `services/display.py`) and LM2 (the MPS3 adapter
> and its lease hooks, §7.1-§7.2). The daemon API, UI and CLI (LM3-LM5) are not wired yet.
>
> **Decides:** david (§8).
>
> **Builds:**
> - HM (§7);
> - the Linux harness lead's FPGA and Linux lanes, per their own design
>   **`docs/planning/linux_lanes/LCD_MIRROR_FPGA.md`** (platform `feat/linux-harness`
>   5607f11, "LCDMIRROR-FPGA"). This doc adopts it and does not redesign it (§4-§6).
>
> **The wire:** LCDMIRROR-FPGA §6.2 plus the HM lead's six amendments is the agreed
> contract (§6).
>
> **Trees read:** HM `main` 2c55567; platform `master` b8c8c46; `-lx` `feat/linux-harness`
> 4956d88 / 5607f11. Cites are `path:line`.
>
> **Spikes:** `tests/spikes/lcd_mirror_decoder.py`, `lcd_mirror_rtl.py` and
> `lcd_mirror_transport.py`, all board-free (§10).

## 0. Recommendation

Yes, HM can show a pixel-exact live copy of the panel, whoever owns it.

**How**
- A small, input-only snooper in the static shell listens to the panel bus **behind the
  CLCD KVM**.
- It keeps a copy of the frame buffer in BRAM.
- A loopback daemon on the Linux harness sends changed 16x16 tiles through the SSH forward
  HM already uses for XVC.

**When**
- The snooper changes the static shell, so it rides **mint 4**.
- Before that, an interim software mode (no mint) mirrors the harness's own screen exactly
  and goes honestly blind while the DUT owns the panel.
- Both modes speak one wire, so HM is built once, now, against a fake board.

**What the spikes show**
- A model of the controller, fed the real harness firmware's byte stream, rebuilds every
  harness screen pixel for pixel.
- The same model rebuilds the clcd_demo and nanosoc DUT pictures across KVM handovers.
- A snooper RTL sketch agrees with the model bit for bit in Icarus over 1.06 M bus bytes.

**The ask of david** (§8): take (d), the hybrid. It costs:
- about 5 HM days, which can start now against the fake board;
- about 5 Linux days after cutover for the interim mirror (no mint);
- about 6.5 board-free FPGA days, riding mint 4 (§9).

## 1. What exists

| Piece | Where | What it means here |
|---|---|---|
| The panel | HX8347-D, 320x240, 8080 8-bit, RGB565 high byte first | `docs/CLCD_PANEL_FACTS.md` §1-§3 (platform). Colour order and orientation are now **proven on glass** with 0x36=0x09, 0x16=0x20 (`docs/evidence/2026-09-w2/p5_clcd_demo_20260923.txt:23-26`; LCDMIRROR-FPGA §1.4) |
| Harness renderer | `firmware/clcd/clcd.c`, compiled unmodified into harnessd (`-lx` `harnessd/main_linux.c:64,244-250,367`: service row 9, 30 ms budget) | Every byte passes `try_push()` (`-lx` `clcd.c:577-589`), which reads STATUS first and never writes into a full FIFO, so a tee on the register write is exact |
| KVM | `fpga/shell/ip/clcd_kvm/clcd_kvm.sv`; `shell_linux_bd.tcl:780,911-918` | Pads are muxed in `s_axi_aclk` (`clcd_kvm.sv:820-823`), with the DUT tunnel already synchronised and filtered (`:344-347`). Every handover pulses `CLCD_RST`, and the new owner re-inits and repaints |
| DUT writers | clcd_demo RM (ROM generated from the firmware table); nanosoc `ahb_clcd` demo (`fpga/rp/nanosoc_exp/sw/ahb_clcd.c:101-145`) | Same init table and the same window-then-0x22 protocol as the harness. clcd_demo repaints the **whole frame about 3 times a second** (evidence file above, finding 1) |
| HM today | Front panel design `docs/design/CLCD_ALIGNMENT.md`; `core/panel.py`, `services/presence.py`, `harness_manager_mps3/panel.py`, `web/static/js/sections/panel.js`; `tools/clcd_mock.py` | A 15x40 text-grid mirror. It runs against fakes: the board-side `panel` verb is HM request R2 (`CLCD_ALIGNMENT.md:419`) and is not on any image yet. It is the fallback (§7.5) |

## 2. Source of truth, ranked

| Rank | Option | Sees the DUT? | Exactness | Cost | Mint | Frame rate | CPU on the 100 MHz hart |
|---|---|---|---|---|---|---|---|
| **1** | **(d) hybrid: (a) now, (c) at mint 4, one wire** | from mint 4 | as (a), then as (c) | (a) + (c) | mint 4 | as (c) | as (c) |
| 2 | (c) static bus snooper after the KVM mux (LCDMIRROR-FPGA §2) | **yes**, both owners, across handovers | GRAM-exact for any window, wrap or partial write. MADCTL is applied at write time. Display on and standby come from the registers; BL and RST from the pads. Scroll, partial, SS/GS, invert and 18 bpp are recorded, and HM applies or badges them. `VIOL` catches a DUT below the tunnel floor | 38 RAMB36 (1.8 %), about 1.2 k LUT and 1.5 k FF, one clock, **no CDC**, no pins, no RP-boundary change | **mint 4** | set by the link, not the source: text ≥ 5 fps; clcd_demo about 3 fps (its own repaint rate); noise 1-2 fps | < 0.5 % for the harness screen; about 5 % for clcd_demo (LCDMIRROR-FPGA §6.3); **< 0.5 % with compare-on-write** (§4.2) |
| 3 | (a) software shadow in harnessd (LCDMIRROR-FPGA §7) | **no**: blind while the DUT owns the panel | exact for the harness (proven, §10.1) | a `hal_front.c` tap and a C port of the model; no RTL | none | the renderer's own pace (4 Hz refresh) | < 0.1 ms per clcd pass |
| 4 | (b) read GRAM back over the bus | would | would be exact, **but** | `READ_PATH=1` (a mint); unproven bidirectional buffers | mint | < 1 fps | high |

Why (b) is last, not just slow:
- A GRAM read reprograms the window and moves the controller's address counter, so it
  corrupts the owner's next write.
- The KVM can only change owners through a panel **hard reset**, which destroys the state
  we would be reading.

Two more options were considered and rejected:
- **Streaming raw `{RS,byte}` to the hart and decoding in software.** The bus peaks at
  9.1 MB/s. At about 60-140 cycles per byte, that is more than the whole hart.
- **Sampling the DUT tunnel through `board_gpio`.** AXI polling is orders of magnitude
  slower than the strobes.

## 3. What "pixel-exact" means

The mirror shows **what the controller scans out**: the GRAM, read through the panel's
display state. One executable model defines it: `Hx8347dShadow`
(`tests/spikes/lcd_mirror_decoder.py`). It is offered as LCDMIRROR-FPGA §8.1's golden
model, and its MADCTL mapping already equals that doc's §2.4 on every pixel of all eight
geometries.

| Aspect | Status | Evidence / handling |
|---|---|---|
| Byte latch on WR↑ with CS low; RS=0 index, RS=1 data; RGB565 high byte first; one datum per register; 0x22 = GRAM | PROVEN | PANEL_FACTS §1-§3 |
| An index byte discards a half pixel | PROVEN by construction | init ends `0x22, 0x00` and the glyphs that follow are legible |
| Orientation anchor: 0x16=0x20, 0x36=0x09 read right way up; 0xE0 is the 180° image | PROVEN | PANEL_FACTS §7.4; the spike's negative control |
| Colour order | PROVEN | clcd_demo bars in order on glass (§1) |
| Address-counter reload (start-register write vs 0x22) | ASSUMED (LCDMIRROR O1, `CTRL.ac_load`) | all in-tree writers rewrite the whole window, then 0x22, so both readings give the same picture |
| MX/MY convention under MV, single flips only | ASSUMED (O2, `CTRL.flip_conv`) | HM badges any MADCTL other than 0x20/0xE0 as `uncalibrated` |
| Register defaults after reset; GRAM kept through RST | ASSUMED (O3, O4) | VALID map: HM hatches tiles not written since the last reset |
| Display on/off, standby | recorded (0x28, 0x1F) | HM renders them (§7.4) |
| Gamma, VCOM, oscillator, power | out of scope, on purpose | they change how the glass looks, not what pixel it holds |

**Motion.** A mirror samples a moving picture, so "exact" is stated precisely:
- a picture that has settled is bit-exact;
- a sample taken mid-repaint is a real mix of consecutive GRAM states, just as a photo of
  the glass would be (the clcd_demo evidence caught exactly that);
- any later write re-dirties the tile, so the next update corrects it.

**Commands the in-tree writers use.** Every writer streams the same firmware table
(`firmware/clcd/hx8347_init.c`), then only the window, 0x22 and pixels. The table uses:
- drive: 0xEA-0xED, 0xE8, 0xE9 and 0x27;
- gamma: 0x40-0x4C and 0x50-0x5D;
- power: 0x1A-0x1D, 0x1F and 0x23-0x26;
- oscillator: 0x18 and 0x19;
- 0x17 (COLMOD), 0x36 (PANEL), 0x28 (display, written twice) and 0x01 (DISPMODE);
- 0x02-0x09 (window), 0x16 (MADCTL) and 0x22.

LCDMIRROR-FPGA §1.4 reaches the same list independently.

## 4. FPGA / static shell: adopted from LCDMIRROR-FPGA §1-§5

We adopt LCDMIRROR-FPGA §1-§5 whole. In brief:

**Tap and clocking**
- Input-only tap on the nine `clcd_kvm_0` pad-side nets plus `owner_o`.
- All in the 100 MHz `s_axi_aclk` domain: **no CDC**, no change to `clcd`/`clcd_kvm`, the RP
  boundary or the pads.

**Decode**
- Registers 0x02-0x09, 0x16, 0x17 and 0x22.
- All 256 registers are recorded.

**Frame buffer and tiles**
- The frame buffer is stored in **viewer order**: 38 RAMB36.
- 20x15 dirty and VALID tile maps, with an atomic `SNAP`.
- Counters: `SEQ`, `FRAMES`, `RAMWR`, `RESETS`, `BYTES`, `VIOL`, `OOB`, `RDS`.

**Registers, memory and mint**
- Registers: `LCDMIR` at `0x44B8_0000`, 256 KiB.
- BRAM, not DDR: the MBV D-cache covers all of DDR, and without Zicbom or PBMT Linux cannot
  map a carve-out uncached.
- Mint 4, riding with D11/D6/D8/D10.

### 4.1 What HM's spikes add to it
1. **Feasibility, in simulation.** `tests/spikes/lcd_mirror_rtl/lcdm_snoop.sv` is a
   ~200-line sketch of the same decoder. A bus-functional model drives it through one
   continuous history, including KVM reset pulses:
   - harness boot, a link-down repaint and the DUT-OSD;
   - clcd_demo frames 5 and 6;
   - the nanosoc demo;
   - harness regain.

   Under both harness and DUT strobe timing, it matches the Python model bit for bit: GRAM,
   registers, every dirty snapshot, and the counters (§10.3). The sketch stores physical
   GRAM order; LCDMIRROR-FPGA stores viewer order. That is a fixed permutation, and the
   model checks both.
2. **LCDMIRROR-FPGA §8's first two deliverables already exist**, board-free:
   - the golden model (§8.1);
   - the exact harness stream, from a host tool linking `clcd.c` with mock registers (§8.3,
     first bullet): `tests/spikes/lcd_mirror_data/fwstream.c` and four recorded streams
     (29 KB zlib).

   Take them as fixtures. This saves about 1 of LCDMIRROR-FPGA's 7.5 days (§11, H2).

### 4.2 One optional FPGA point: compare-on-write dirty tracking
LCDMIRROR-FPGA sets a tile dirty on any write. harnessd then reads each dirty tile (128
uncached words) to find out whether it really changed (§6.1 step 3).

clcd_demo repaints all 300 tiles about 3 times a second while the picture changes in about
12 (its frame counter). With the dirty bit set **only when a write changes a pixel**, the
spike measured **300 → 12** dirty tiles per repaint. That takes about 38 k uncached reads
per repaint down to about 1.5 k, and clcd_demo's cost from about 5 % CPU to under 0.5 %.

It costs a read-before-write on the snooper's BRAM port: the RAMB36 goes true-dual-port at
x36. Events are ≥ 4 cycles apart and the sketch needs 3. There is no extra BRAM.

**This is the FPGA lane's call. HM works either way.**

## 5. Linux harness: adopted from LCDMIRROR-FPGA §6-§7

**Daemon**
- `mps3-lcdmirror`, a child of harnessd (the GDB-proposal pattern), on `127.0.0.1:6940`,
  at most 2 clients.
- It does no MMIO while no client is connected.
- It sends dirty tiles only, in FILL/PAL1/PAL2/RLE16/RAW encodings.

**Interim mode**
- A tap in harnessd's `hal_front.c` `mps3_reg_write32` (`-lx` `:23-35`) feeds a C port of
  the golden model.
- The model writes the hardware aperture's layout into `/dev/shm/mps3-lcdmirror`.
- The owner comes from CLCDKVM `STATUS`. While the DUT owns the panel, VALID is all zero and
  HM greys the view.

**Why not inside harnessd**, confirmed by our LX sub-lane:
- the service table is full (13 of `MPS3_SVC_MAX` 13, `firmware/common/service.h:113`);
- a keyframe encode would overrun row 9's 30 ms budget;
- every harnessd listener binds 0.0.0.0 (`main_linux.c:737,881`);
- a mirror bug must not take down the watchdog kicker.

**Access**
- Loopback bind, so the port is reachable only via SSH with a claimed key (the S12 rule).
- Unclaim reboots the board.
- Lease end is invisible to the board, so HM closes its forward itself (as XVC does).

## 6. The wire: LCDMIRROR-FPGA §6.2 + six amendments (agreed)

**The contract**
- **§6.2:**
  - TCP `127.0.0.1:6940` through `ssh -J hub -L 127.0.0.1:<local>:127.0.0.1:6940
    root@board`;
  - `HELLO` {proto 1, W, H, fmt, tile, mode `hw`|`sw`, static_id};
  - `UPDATE` {seq, frames, resets, status, owner, valid map; REGS (256 B) on keyframes,
    else MODE; tile records `{idx u16, enc u8, len u16, payload}`};
  - client `KEY` / `RATE` / `PING`;
  - encodings FILL (2 B), PAL1 (36 B), PAL2 (72 B), RLE16, RAW (512 B);
  - `version.features += "lcd_mirror"`, `version.lcd_mirror = {"port":6940,"mode":…,"proto":1}`,
    `stats.lcd_mirror = {"peer","since","fps","bytes"}`.
- **The HM lead's amendments:**
  1. RGB565 little-endian in the stream.
  2. `t_ms` on every UPDATE.
  3. A refusal is one JSON line `{"ok":false,"err":"lcd_mirror: …"}`, then close.
  4. Status bits `exact` / `text_only` / `blind`.
  5. `max_msg` (≤ 64 KiB) in HELLO; keyframes are split across UPDATEs.
  6. KEY after a `seq` gap or on start gets a full keyframe with REGS at the next UPDATE;
     RATE is clamped and the clamp is echoed.

### 6.1 HM's byte-level reading (confirmed by the board side, with six corrections)

The Linux lead built `mps3-lcdmirror` to this reading (platform `feat/lcd-mirror` d86ce81;
`docs/contracts/net-protocol.md` v0.15 "LCD mirror (TCP 6940)") and checked in wire vectors,
which HM parses (`tests/fixtures/lcdmirror_wire/`, `tests/unit/test_lm1_display_wire.py`). HM's
one module for the wire is `core/display_wire.py`. **The six board-confirmed corrections**, all
applied there and in the text below:

1. **RATE's clamp** is echoed in the board's own `0x11` reply, at once. Nothing about the rate
   rides an UPDATE.
2. **`seq`** is per connection and **1 for the first UPDATE** (+1 each, wraps at 2^32), so an
   `ACK 0` acknowledges nothing.
3. **owner 3 (unknown) is reserved**; sw mode sends only 0 and 1.
4. **There are no heartbeat UPDATEs.** Nothing changed means no UPDATE, so PING is the liveness
   probe (HM keys `stale` on PONG alone, §7.2).
5. **`max_msg` (4096..65536) bounds the WHOLE message**, the 8-byte header included; a longer
   one is corrupt.
6. **REGS is the snooper's raw register log, never reset.** After `resets` changes, R16, R17,
   R36 and R01 come from MODE, not REGS (`core/display.py` `DisplayFrame.mode_regs`).

**Framing**
- `'L' 'M' u8 type, u8 rsvd=0, u32 len` (LE), then `len` bytes.
- A first byte `{` is the refusal line.

**0x01 HELLO**
- JSON `{"proto":1,"w":320,"h":240,"fmt":"rgb565le","tile":16,"mode":"hw"|"sw","static_id":"0x…","max_msg":65536}`,
  plus H3's `boot_id`, `rate`, `rate_max` (30) and `clients_max` (2). `max_msg` counts the
  8-byte header (correction 5).

**0x02 UPDATE**
- Fields in order:
  - `u32 seq` (1 for a connection's first UPDATE: correction 2), `u32 t_ms`, `u32 frames`,
    `u32 resets`, `u32 status`;
  - `u8 owner` (0 harness, 1 DUT; 3 unknown is reserved: correction 3);
  - `u8 valid[38]` (bit t = tile t, LSB first);
  - `u8 regs[256]` if `key_first` (a raw log, never reset: correction 6), else `u32 mode`
    (`{R01,R36,R17,R16}`, the panel's decoded state);
  - `u16 ntiles`, then the tiles.
- `status` bits:
  - [10:0]: the CSR `STATUS`;
  - [16] `exact`, [17] `text_only`, [18] `blind`;
  - [24] `key`, [25] `key_first`, [26] `key_last`, [27] `snap_last` (H1).
- Nothing changed: no UPDATE at all (correction 4).
- Tile `idx = ty*20 + tx`, pixels row-major.

| Encoding | Payload |
|---|---|
| FILL | u16 |
| PAL1 | 2 x u16 colours, then 16 x u16 rows; bit x selects colour 1 |
| PAL2 | 4 x u16 colours, then 16 x u32 rows; pixel x = bits [2x+1:2x] |
| RLE16 | PackBits over u16: `0x80\|(n-1)` then one u16 is a run of n; `n-1` then n u16 are literals; n ≤ 128 |
| RAW | 256 x u16 |

**Client messages**
- `0x10` KEY.
- `0x11` RATE: `u8` hz; the board echoes the clamped value in a `0x11` of its own, at once
  (correction 1).
- `0x12` PING `u32`, answered by `0x13` PONG `u32` at any time: the liveness probe.
- `0x14` ACK `u32 seq` (H1): cumulative; the board keeps at most 2 UPDATEs unacknowledged.

## 7. Harness Manager

HM's side was designed by sub-lane LCDM-HM and proven by the transport spike (§10.2). Cites
are HM `main` 2c55567.

### 7.1 The capability and where the code lives

**Name.** The core capability is `DISPLAY_MIRROR = "display_mirror"` ("Live display").
Plain `display` is taken: it collides with `lab display` (`cli/cmd_lab.py:82-104`) and with
`mps3.display_flip`. Any board with a mirrorable screen can implement it; MPS3 is first.

| Layer | File (new unless noted) | What |
|---|---|---|
| Model + protocol | `core/display.py` | `DisplayInfo` (w, h, fmt, tile, mode, static_id, max_msg). `DisplayUpdate` (seq, t_ms, frames, resets, status, owner, valid, regs\|mode, tiles). `DisplayAdapter`: `display_reason()`, `display_info()`, `open_stream(rate)`, `display_release()`. `DisplayStream`: `read()`, `key()`, `rate()`, `ping()`, `close()`. Also the board-agnostic 5-encoding tile decoder and `badges()` |
| Session | `core/pack.py` (edit) | `BoardSession.display: DisplayAdapter \| None`, beside `panel`; the pack hook `BoardPack.display_adapter(session) -> DisplayAdapter \| None` (default: `session.display`); a CONTRACTS row |
| Service | `services/display.py` | `DisplayService`: one upstream per board; the compositor (the latest record per tile plus the decoded frame); per-viewer dirty sets; open, grace-close and reconnect; lease/claim hooks; the `display.state` event |
| Daemon | `daemon/display_api.py` (in `EXTENSIONS`) | the routes in §7.3 |
| MPS3 | `harness_manager_mps3/display.py` (`Mps3Display`), hooked in `pack.py` (`session.display`; `Mps3Pack.display_adapter`) | Gate on the ENGINE name `lcd_mirror` in `version.features` (no bit: a bit number never counts), Linux only, with the port from `version.lcd_mirror.port` (default 6940). Reach the board through `claim.open_forward({"lcd_mirror": port})`, the XVC/GDB path: one forward per session, a new socket per connect. Reasons in the order a user fixes them: bare metal, no engine, the lease (D3, naming the holder), the claim |
| Capability | `harness_manager_mps3/capabilities.py` (edit) | `CapabilitySpec(C.DISPLAY_MIRROR, "Live display", via(L.ETHERNET\|L.HUB\|L.SSH, features=("lcd_mirror",)))` (a real MPS3 candidate carries Ethernet or hub links, not SSH), with the hint "needs the Linux harness with lcd_mirror and a claimed board"; the adapter's `display_reason()` says which |
| Web | `web/static/js/display.js`; `sections/panel.js` (edit) | the Live display canvas at the top of the Front panel card; today's text mirror becomes its fallback |
| CLI | `cli/cmd_display.py` | `display snapshot TARGET --out x.png [--scale 2] [--raw]`; `display show TARGET` |

### 7.2 Transport and lifecycle

**The upstream.** `ssh -J HUB -l root <pinned> -N -T -L 127.0.0.1:<p>:127.0.0.1:6940 BOARD`.
- It is built by the real `SshTunnel`, so it gets `ControlPath=none`, `BatchMode`,
  `ExitOnForwardFailure`, `StrictHostKeyChecking=yes`, and the supervisor that restarts on
  the **same** local port. The spike checks the argv.
- It runs in its own ssh process: a running ssh cannot gain a forward without a
  ControlMaster, and a ControlMaster is banned.
- There is one upstream per board, shared by every browser tab.

| Event | What HM does | Spike |
|---|---|---|
| First viewer (WS, PNG or CLI) | Opens the tunnel, reads HELLO, sends `RATE max(viewers)` and `KEY`. Presents nothing until `key_last` | every run |
| Last viewer leaves | Closes after 30 s; a returning viewer reuses the upstream | — |
| `seq` gap | Stops presenting, sends `KEY`, stages the keyframe, presents it on `key_last` | `seq_gap_key_recovery` PASS |
| Tunnel drop or board reboot | Same local port; the reader reconnects, sends KEY and shows "reconnecting" over the last picture | `drop_claim_loss_recover` PASS, 2.5 s |
| Claim lost, key refused, host key changed | State `down` with ssh's reason (`map_ssh_failure`). A changed host key stops at once; a forward that does not come up 3 times in a row stops (`Mps3Display`); falls back to the text mirror | same |
| Image without the service | Gated on the feature. As a backup, ssh's "open failed … Connection refused" gives "no lcd_mirror service", with a retry after 60 s | `no_service` PASS, 0.03 s |
| Third client | The board's JSON refusal line: state `refused`, back off. Each HM daemon uses one of the board's two slots | `refusal_3rd_client` PASS |
| Lease released, expired or lost | Closes at once, as XVC does: `DisplayService` hears `lease.state` on the bus and closes that board's upstream ("closed: the lease was lost"), which drops the forward. `session.closed` does the same. The next connect asks the lease again, fresh | — |
| KVM handover | `resets+1` and VALID=0 give hatching until the new owner paints | `handover_hatch` PASS: 300, then 0 |
| Liveness | `PING` every 1 s is the probe: the board sends no heartbeat UPDATEs (correction 4), so `stale` after 3 s with no **PONG** (an UPDATE does not count); reconnect after 10 s | — |

### 7.3 Daemon API

| Route | Returns |
|---|---|
| `WS /api/v1/boards/{bid}/display/ws?ack=1` | A text frame first: `{state, hello, reason}`. Then **binary** frames, each one UPDATE in the board's own layout with the tile records forwarded **as the board encoded them**. The first is a full keyframe built from the latest record per tile. Later text frames carry `{state, detail, rtt_ms, rate, badges}`. The client sends `{"ack": seq}` after drawing, and optionally `{"rate": hz}` |
| `GET /api/v1/boards/{bid}/display` | state, mode, owner, decoded status, badges, hatched count, seq, t_ms, rtt, rate, fps, bytes/s, viewers, reason |
| `GET /api/v1/boards/{bid}/display.png?scale=1\|2&hatch=1` | The presented picture: 8 ms to encode. It opens the upstream if needed and waits ≤10 s for a keyframe. `?format=raw` gives RGB565 LE for tests |
| event `display.state` | `{state, mode, owner, badges, reason}`, so the Board tile can say "Live display: DUT owns it" |

**Backpressure is per viewer, drop-to-latest.**
- A viewer is a set of ≤300 dirty tiles plus one message in flight until it is acked.
- The events WebSocket's `Outbox` drops the **oldest** frames (`daemon/outbox.py:1-13`). That
  is right for events and **wrong for a picture**: a dropped tile stays stale for ever. The
  display must not reuse it.
- On loopback, uvicorn's `send` does not push back until megabytes are queued. The spike
  measured a slow tab without the ack at 7.0 s behind, and with the ack at 61 ms.

**Turn off `ws_per_message_deflate`** (uvicorn's default, `daemon/server.py:390`).
Every browser offers it, and on the noise pattern it costs 6.3 ms against about 0.7 ms of
daemon CPU per message, for nothing on localhost.

### 7.4 The Live display canvas

**Decoding**
- **Decode in the browser.** The daemon forwards the encoded tiles: 7.3 KB for a harness
  keyframe, against 307 KB as RGBA.
- The JS decoder in the spike is bit-exact with the Python one. In node v10 it takes 1.2 ms
  for a harness keyframe and 4.1 ms for noise.
- A 64 K-entry LUT turns RGB565 into RGBA in 0.2-0.5 ms per full frame.

**The canvas**
- `<canvas width=320 height=240>`, so the backing store is always exact.
- One `ImageData` over a `Uint32Array`; `putImageData` on the dirty bounding box; ack after
  the draw in `requestAnimationFrame`.
- RGB565 → RGB888 by bit replication (`tools/gen_tokens.py:167-174`), the same as
  `tools/clcd_mock.py`.
- Tests read the canvas back with `getImageData` and compare it with `display.png?format=raw`.

**1x and 2x are integer device pixels**
- `k = max(1, round(devicePixelRatio × zoom))` device pixels per panel pixel.
- The CSS width is `320·k/dpr`, with `image-rendering: pixelated`.
- "1x" is one CSS pixel per panel pixel; a "device pixels" option forces k = zoom.
- A `matchMedia` resolution change re-evaluates it.

**Overlays**, drawn over the canvas and never into it:

| Condition | Shown as |
|---|---|
| VALID=0 tile | diagonal hatch on that tile |
| `mode=sw` and owner=DUT (`blind`) | greyed, with the `held` badge "DUT owns the panel: this harness image cannot see it (live view needs mint 4)" |
| owner=DUT, `mode=hw` | the `held` badge "DUT owns the panel". The picture is exact |
| backlight off / display off / standby (`!bl`, `!display_on`, `standby`) | dimmed, with a badge. A state must persist for 1 s before it shows: clcd_demo re-runs its init about 3 times a second, and 0x28 passes through "display off" each time |
| `viol` | warning badge "bus timing violations: the glass may differ" |
| `approx` | warning badge "18-bit colour shown as 16-bit" |
| `!fmt_ok` | warning badge "unknown pixel format" |
| MADCTL other than 0x20/0xE0; R36 ≠ 0x09; R01 ≠ 0; any scroll register written | warning badge "not mirrored exactly (R36=0x..)" |

**Freshness**
- Under the canvas: "updated 0.3 s ago · 12 fps · 9 kB/s · RTT 20 ms".
- After 3 s of silence: `stale` (dashed border, with the age).
- After 10 s, or on socket loss: `reconnecting` (a scrim over the last picture). The browser
  socket reconnects with a 0.5-8 s back-off, like `EventSocket`.

### 7.5 Fallback

When `display_mirror` is unavailable, the Front panel card shows today's **text mirror**
(`panel.js`, unchanged) under the capability's reason line. That covers:
- bare metal v0.11;
- a Linux image without `lcd_mirror`;
- an unclaimed board;
- a user without the lease;
- no SSH.

On bare metal, the text mirror is itself rebuilt from `clcd_kvm` state (`harness_manager_mps3/panel.py:273-347`).

### 7.6 Touch pass-through: not in v1 (D4)

**Why not.**
- There is no path to inject a touch into the STMPE811. A click could only become a fake event
  in harnessd's touch dispatch, which reaches the harness UI and **never the DUT**.
- It would forge the "someone is at the board" signal that the lease-request tap stands for
  (CLCD_ALIGNMENT D2).
- It lands on the flaky touch path: the bus-loss latch, volatile calibration with the axes
  swapped and mirrored, and a held touch that once starved the loop.

**If it is ever wanted:** a claim-gated `panel {"tap":{"row":r}}` through the page-aware hit
test. It must be:
- rate-limited to 1/s;
- refused while the DUT owns the panel, and on the lease-request banner;
- logged as virtual.

Page-next and Identify's "found it" are the only useful targets, and HM already has buttons for
both.

## 8. Decisions for david (recommendation first)

**D1: what the mirror is built on.**
1. **(d) the hybrid (recommended).** The interim software mirror ships about 5 working days after
   cutover, with no mint. The snooper makes it exact for the DUT at mint 4. HM is built once.
2. **(c) the snooper only.** It saves the 1.5-day interim, but there is no mirror at all
   until mint 4 is fielded.
3. **(a) software only.** It needs no mint, but it is permanently blind while the DUT owns
   the panel, and that is the case david asked about.
4. **(b) GRAM read-back.** Not recommended: it cannot coexist with a DUT that is drawing
   (§2).

**D2: put the snooper in mint 4.**
1. **Yes (recommended).** It rides with D11/D6/D8/D10. It adds no boundary change and no
   extra re-key, costs 38 RAMB36, and the RC2 floorplan has room (LCDMIRROR-FPGA §3).
2. Defer it to a later mint. Only the interim mirror exists until then.

**D3: who may see the live picture.**
1. **Only the lease holder (recommended),** exactly as XVC: HM never opens the forward for a
   user who does not hold the lease. Everyone else keeps today's text panel card.
2. Let the holder's HM relay the picture to other HM users watching the board. That needs
   a sharing hop HM does not have yet; revisit after v1.

**D4: clicking the mirror to touch the panel.**
1. **No, not in v1 (recommended).**
   - The STMPE811 is read by the harness over I2C, and there is no path to inject a touch
     into it.
   - Touch is still flaky: a bus-loss latch, and calibration that is volatile.
   - A held touch once starved the bare-metal loop.
2. Later, a harness-UI-only "virtual tap" verb in harnessd: claim-gated, rate-limited, and
   refused while the DUT owns the panel. It never reaches the DUT.
3. Real injection into the touch path. That needs static RTL on the I2C path; reject it.

## 9. Plan

**Interim** = no mint; the board side lands after the Linux cutover (Sun 27 Sep, 20:00+).
**Full** = mint 4. Board-side hours are LCDMIRROR-FPGA §10's (days × 8).

| Lane | Owner | Work | Hours | Needs | Phase |
|---|---|---|---|---|---|
| LM1 DISPLAY-CORE | HM | `core/display.py` (model, 5-encoding decoder, `badges()`); `services/display.py` (compositor, per-viewer dirty sets with ack, open, grace and reconnect, `display.state`); a `FakeLcdMirror` lifted from the transport spike | 12 | — (start now) | interim |
| LM2 DISPLAY-MPS3 | HM | the pack adapter, the feature gate, `claim.open_forward`, lease hooks, the capability spec; FakeSsh tests | 6 | LM1 | interim |
| LM3 DISPLAY-API | HM | WS (ack), status, `.png`/raw, the event; `ws_per_message_deflate` off; `API.md`, `CONTRACTS.md` | 6 | LM1 | interim |
| LM4 DISPLAY-UI | HM | canvas, `display.js`, overlays, freshness, 1x/2x, fallback to the text mirror; a browser pixel test against `.png?format=raw` | 10 | LM3 (mock API first) | interim |
| LM5 DISPLAY-CLI | HM | `display snapshot` / `show` | 3 | LM3 | interim |
| L-SVC | Linux | `mps3-lcdmirror` service, the wire, `version`/`stats`, packaging, tests; **plus ACK and `snap_last` (§11 H1): +4** | 24 + 4 | H1 answered | interim |
| L-SW | Linux | the interim tap: C model port, shm, owner blinding; gated on H2's golden streams | 12 | L-SVC | interim |
| L-BOARD-1 | Linux (board) | a 30 min sw-mode check, plus O6's measurements | 1 | cutover, L-SW | interim |
| F-SNOOP | FPGA | RTL + IP, the golden model (**-8 with H2**), cocotb incl. e2e and mutations, BD/regmap/DTS, OOC utilisation and timing | 60 - 8 | — (board-free, start any time) | mint 4 |
| F-MINT | FPGA | ride mint 4 with D11/D6/D8/D10 (one BD delta) | in the mint | mint 4 scheduled | mint 4 |
| L-HW | Linux | switch the service to the `hw` source | 4 | F-MINT | mint 4 |
| L-BOARD-2 | Linux (board) | mint-4 proof: mirror vs photo, handover, clcd_demo; the O1/O2/O4 photos | 4 | F-MINT | mint 4 |

**What each side costs**
- **HM: 37 h, about 5 days for one engineer, or about 2 days of wall time as 3 parallel lanes.**
  - HM needs no board. It merges dark: the capability says "needs the Linux harness with
    lcd_mirror" until a board reports the feature.
- **Linux harness, interim: about 41 h.** A live harness-screen mirror about 5 working days
  after cutover.
- **Mint 4: about 52 h of FPGA work** (all board-free until the mint), plus 8 h for the hw
  switch and the board proof.

**Order**
1. H1 is answered (a day).
2. LM1-LM3 and L-SVC run in parallel.
3. Then L-SW and LM4.
4. The sw-mode board check.
5. F-SNOOP runs whenever the FPGA lane has room. It rides mint 4, and L-HW flips the source.
6. HM changes nothing between interim and full.

## 10. Spikes (board-free)

### 10.1 The bus model rebuilds the real pictures: `tests/spikes/lcd_mirror_decoder.py` (PASS)

**Inputs.** Four recorded streams: the exact `{RS,byte}` bytes that `firmware/clcd/clcd.c`
and `hx8347_init.c` (`-lx` 4956d88, the code harnessd runs) push into clcd_0.
- They were captured by `lcd_mirror_data/fwstream.c`, which links the real renderer with the
  mock HAL.
- They cover boot, link-down, the DUT-OSD and a KVM regain: 0.55 M bytes in total.

**References.** The harness screens are compared with HM's `tools/clcd_mock.py`, which
rasterises the renderer's own 40x15 grid with HM's font JSON. That is a different code path
from `build_cell`. The DUT pictures come from the platform's `card_model.card_pixel()` and
from the nanosoc demo's rectangles.

```
spec: MADCTL x8 geometries vs LCD_MIRROR_FPGA.md 2.4: 0 pixel mismatches (PASS)
stream                     bytes unknown  badpx        wr/chg   raw B rle16 B zlib1 B
harness:boot              187946       0      0 PASS  300/300  153600   36024    6515
harness:link_down          37674       0      0 PASS   67/67    34304    4740     810
harness:banner            138957       0      0 PASS  269/269  137728    8892    2143
dut:clcd_demo#5           153739       0      0 PASS  300/291  148992   17104    2506   (after a KVM reset)
dut:clcd_demo#6           153737       0      0 PASS  300/12     6144    1628     228   (next repaint)
dut:nanosoc_ahb_clcd      196990       0      0 PASS  300/279  142848    6748    1492   (after a KVM reset)
harness:regain            187400       0      0 PASS  300/297  152064   38620    6799   (after a KVM reset)
neg:one-bit-flip          187946       0      1 PASS  (the flip is caught)
neg:MADCTL-0xE0=rot180    187946       0      0 PASS  (as-vendored MADCTL = the 180-degree image)
RESULT: PASS
```

Rebuilt frames are in `docs/design/lcd_mirror/`:
- `rebuilt_harness_link_down.png`;
- `rebuilt_harness_dut_osd.png`;
- `rebuilt_clcd_demo_f5.png`;
- `rebuilt_nanosoc_ahb_clcd.png`.

**Two findings for the design:**
- **Written is not changed.** clcd_demo frame 6 writes all 300 tiles and changes 12 (§4.2).
- **The renderer's first paint is 188 KB on the bus.** That means about 40 s at its
  256 B-per-pass pace, so the interim mirror fills progressively after boot or regain, just
  as the glass does.

**Run it:** `python3 tests/spikes/lcd_mirror_decoder.py [--png DIR]` (4 s).

### 10.2 Transport through the SSH forward: `tests/spikes/lcd_mirror_transport.py`

**The setup.** One process, all on loopback, with the agreed wire as read in §6.1:
- a fake `mps3-lcdmirror`, whose C and Python encoders are byte-identical;
- the **real** `SshTunnel` argv, driven through the tunnel tests' `FakeSsh`;
- HM's upstream reader and compositor;
- a real uvicorn WebSocket;
- browser stand-ins that CRC-check the picture after **every** message: 0 mismatches in every
  run.

The board-side CPU figures are host C `-O2`. Full output:
`docs/design/lcd_mirror/transport_spike_output.txt`.

| Pattern (board RATE 20 Hz) | Link | Flow | B/UPDATE | Keyframe | fps | p50 / p95 ms |
|---|---|---|---|---|---|---|
| (a) harness screen | none | — | ~145 | 7,302 B | 4.0 (source rate) | 2 / 21 |
| (b) clcd_demo card, 13 Hz repaints | none | — | ~670 | 7,934 B | 12.0 | 4 / 16 |
| (c) full-frame noise | none | — | 51,773 (3 per frame) | 155,571 B (3 UPDATEs) | 11.9 | 19 / 47 |
| (a) / (b) | 1.25 MB/s | — | 133 / 653 | | 4.0 / 12.8 | ≤ 41 |
| (c) | 1.25 MB/s | none | | | 12.4 | **832 / 1,532** (2.0 MB buffered) |
| (c) | 1.25 MB/s | **ACK ≤2** | | | 6.4 | **116 / 163** |
| (a) / (b) | 250 kB/s | — | 133 / 693 | | 4.0 / 12.7 | ≤ 58 |
| (c) | 250 kB/s | none | | | 7.6 | **5,401 / 9,587** |
| (c) | 250 kB/s | **ACK ≤2** | | | 2.0 | **579 / 700** |

The numbers are from the committed run. A second run, by the sub-lane, agreed to within
about 10 %.

**Real harness screens.** The keyframes, from §10.1's rebuilt pictures, are well under
LCDMIRROR-FPGA's "~11 KB" estimate:

| Screen | Keyframe | Delta from the previous screen |
|---|---|---|
| boot | 7,710 B | — |
| link-down | 7,914 B | 984 B |
| DUT-OSD | 3,883 B | — |
| regain | 7,914 B | — |

**Behaviour checks, all PASS:**
- refusal of a third client;
- a seq gap followed by KEY and an exact recovery;
- an image without the service;
- a tunnel drop plus a refused key, recovered on the same port;
- sw-mode blind (greyed, 300 tiles hatched);
- a handover hatch clearing on the first repaint;
- the PNG snapshot;
- RATE 50 clamped to 20 and echoed.

**Fan-out, 4 tabs on one upstream:**

| Tab | p50 latency |
|---|---|
| fast | 30 ms |
| slow, with ack | 61 ms |
| slow, **without** ack | 7.0 s |

**Not proven here:**
- dropbear and MBV throughput (LCDMIRROR O6);
- the real hub RTT;
- Chrome itself (the JS was timed in node v10).

**Run it:** `python3 tests/spikes/lcd_mirror_transport.py [--quick]` (82 s, or 50 s with
`--quick`).

### 10.3 Snooper RTL sketch vs the model, in Icarus: `tests/spikes/lcd_mirror_rtl.py` (PASS)

**The history.** `lcd_mirror_rtl/lcdm_snoop.sv` (a sketch, not a deliverable) is driven by an
8080 bus-functional model (`tb_lcdm_snoop.sv`) through one continuous history:

> harness boot → link-down → DUT-OSD → [KVM reset] → clcd_demo #5 → #6 → [KVM reset] →
> nanosoc demo → [KVM reset] → harness regain

The harness parts use clcd_0's real strobe timing (2/4/4/1 cycles); the DUT parts use a
slower, uneven one.

```
  harness:boot         dirty tiles rtl 179 == model-changed 179
  harness:link_down    dirty tiles rtl  67 == model-changed  67
  harness:banner       dirty tiles rtl 269 == model-changed 269
  dut:clcd_demo#5      dirty tiles rtl 291 == model-changed 291
  dut:clcd_demo#6      dirty tiles rtl  12 == model-changed  12
  dut:nanosoc          dirty tiles rtl 300 == model-changed 300
  harness:regain       dirty tiles rtl 297 == model-changed 297
  counters: records 1056450 bytes 1056435 pixels 510688 changed 296446 ramwr 2032 resets 3 anom 5
  simulated 166656.2 ms of bus in 117 s wall
RESULT: PASS
```

**What must match.** All 76,800 GRAM pixels, the 14 interpreted registers, every dirty
snapshot and the counters must equal the model.

**Mutation checks.** Three scripted mutants each make it fail, as they must:

| Mutant | Caught by |
|---|---|
| `--mutant byte-order` | 14,864 GRAM pixels differ, e.g. `0x00F8` where the model has `0xF800` |
| `--mutant wrap-off-by-one` | GRAM and dirty-map mismatches |
| `--mutant snap-no-clear` | stale dirty tiles in 4 of the 7 snapshots |

**Storage.** 1,228,800 bits = 38 RAMB36, a 2 Kb register shadow, and 2 x 300 dirty flops.
LUT and FF counts need synthesis; LCDMIRROR-FPGA estimates about 1.2 k LUT and 1.5 k FF.

**Run it:** `python3 tests/spikes/lcd_mirror_rtl.py` (about 2 min; needs `iverilog`).

## 11. Requests to the Linux harness lead (beyond the six amendments)

The list is kept short on purpose: LCDMIRROR-FPGA already covers the board side. Each item stands
alone; H1 is the only one HM cannot do without.

| # | Request | Why (evidence) | Hours (theirs) |
|---|---|---|---|
| **H1** | **Flow control and whole snapshots in the wire.**<br>• `0x14 ACK u32 seq` (HM → board): the board keeps ≤ 2 UPDATEs (≤ 128 KiB) unacknowledged and SNAPs again only when there is room (drop-to-latest at the source).<br>• A `snap_last` status bit ([27]) on the last UPDATE of one SNAP, and one `t_ms`/`frames` across its parts.<br>• Confirm or correct HM's byte-level reading (§6.1), and check in a few wire vectors: HELLO, a split keyframe, one UPDATE per encoding, the refusal line. | Without the ACK, the stacked SSH windows hold the backlog: full-frame motion lags **0.8-1.5 s at 1.25 MB/s and 5.4-9.6 s at 250 kB/s**. With it: **0.12-0.16 s and 0.58-0.70 s** (§10.2). Without `snap_last`, a SNAP split across UPDATEs (noise: 3) can only be shown part by part, which tears | 4 |
| **H2** | Take HM's golden model and recorded streams as LCDMIRROR §8.1 and §8.3: `tests/spikes/lcd_mirror_decoder.py` (`Hx8347dShadow`), `lcd_mirror_data/*.stream.z` and `fwstream.c`. Gate the §7 C port on the same pictures | One model, so the mirror, the snooper bench and HM's tests cannot drift. Its MADCTL map already equals §2.4 on all 8 geometries | saves ~8 |
| **H3** | Small wire rules, all confirmable in one reply:<br>• `seq` is per connection, +1 per UPDATE, wraps at 2^32.<br>• Key parts are consecutive and carry only VALID tiles.<br>• PONG is answered at any time.<br>• `RATE 0` = pause (no MMIO).<br>• `exact` = `hw` ∧ ¬`viol` ∧ `fmt_ok` ∧ ¬`approx`.<br>• `owner u8` wins over `status[2]`.<br>• HELLO adds `boot_id`, `rate_max` and `clients_max`.<br>• `max_msg` counts the 8-byte header. | So HM's decoder and the board's encoder cannot disagree at the edges | 1 |
| **H4** | *(optional, the FPGA lane's call)* Compare-on-write dirty tracking (§4.2) | clcd_demo drops from about 5 % CPU to under 0.5 %. Not needed for correctness | +2-4 |

Everything else HM needs is already in LCDMIRROR-FPGA or the amendments:
- the port and loopback bind;
- `version.features` / `version.lcd_mirror` / `stats.lcd_mirror`;
- VALID, `blind`, `text_only`, `exact`, `viol`, `approx`, `fmt_ok`, MODE and REGS;
- `max_msg`, split keyframes, KEY, RATE and PING.

HM's answers to LCDMIRROR-FPGA's own "Requests to HM":
- **All agreed:** render in viewer order as RGB565; hatch VALID=0; grey `sw` + DUT; badge
  `viol`/`approx`/`!fmt_ok`/non-baseline registers; use the golden model and streams as
  fixtures.
- **Implemented in the transport spike and designed in §7.4.**
