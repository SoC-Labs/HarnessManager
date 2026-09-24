# Harness Manager and the MPS3 front panel (CLCD): presence, interaction, one design language

Lane CLCD-HM, 2026-09-24. Design plus one board-free spike; no firmware or HM product code changed.

david asked:
- Can Harness Manager work more closely with the MPS3's LCD?
- Can we tell whether it is attached?
- Is there a more aligned design language?

**Where the sources are.** Line numbers are pinned to these trees:

| Prefix | Tree | Pinned at |
|---|---|---|
| `fw:` | platform `mps3-nanosoc-platform`, branch `feat/rm-ila-mint` (the fielded v0.11 firmware) | e543630 |
| `lx:` | the `mps3-nanosoc-platform-lx` worktree, branch `feat/linux-harness` | 17c7ea1 |
| `hm:` | this repo | `main` 80bb849 |

The platform trees were only read (`git show`, `git archive` into /tmp).

**Spike picture:** `docs/design/clcd/mockup.png`. Rebuild it with `python3 tools/clcd_mock.py --shot`.

---

## 0. Recommendation

**Make the panel a second, read-mostly face of Harness Manager, and build it only on the Linux harness.**

**Presence.** Every Harness Manager session sends a small `hello` on the control port it already uses. It goes out once every 30 s, piggy-backed on connections HM makes anyway, and lives 90 s on the board.
- The Linux harness keeps a table of up to 4 sessions.
- The panel shows who is connected, and the hub lease that HM relays: the board cannot see the hub.
- Every `hello` reply returns the panel's state: page, KVM owner, card, and a small ring of tap events. So HM learns what the LCD shows with no extra round trip.

**Interactions.** Only those that cost no pixels or touch budget:
- **Identify** blinks the backlight. That is one register bit, and it works even while the DUT owns the panel.
- A lease request appears as a banner, and a tap there notifies the holder.
- Board-side programming progress.

The QR code is not worth building now.

**Design language.** Take HM's existing tokens and grammar, plus one new colour, "held" (violet, "someone else has it"), as the single source. It would live in `design/tokens.json` in this repo, generating both HM's CSS and a C palette header for the panel renderer.
- The panel keeps today's layout and facts.
- It gains HM's words (design, prog, shell, card), muted labels like HM's key/value tiles, and status colours.

**Bare metal (v0.11).** It stays as it is, and HM degrades by feature bit:
- it reads the KVM owner with `display query`;
- it draws a mirror of the panel rebuilt from its own reads;
- it greys out Identify and presence, with a reason.

**Size.** About 25 h of HM work in 4 parallel lanes, plus about 29 h of requests to the Linux harness lanes, plus a 10-minute bench check.

---

## 1. What exists and what is missing

### 1.1 What is on the LCD today (fielded v0.11)

| Fact | Evidence |
|---|---|
| **Panel.** Himax HX8347-D on the MCBQVGA-TS module, 320x240 landscape, RGB565 sent high byte first, over an 8080 byte streamer at `0x44AC_0000` with no framebuffer | `fw:firmware/clcd/clcd.h:2-5`, `fw:firmware/clcd/hx8347_init.c:131`, `fw:firmware/clcd/clcd.c:941-942`, `lx:docs/contracts/shell-regmap.md:419-428` |
| **Text mode only.** An 8x16 X11 "fixed" font, ASCII 0x20-0x7E, gives a 40x15 grid; any other character draws as a space | `fw:firmware/clcd/clcd.h:54-57`, `fw:firmware/clcd/font8x16.h:2-30`, `fw:firmware/clcd/clcd.c:906-908` |
| **Three colours in all:** white text, black background, and white-on-red for an "inverted" **row** (per row, not per cell) | `fw:firmware/clcd/clcd.c:65-67`, `:911-913` |
| **Status page** rows: 0 `MPS3-01` + `nanoSoC harness`; 2 `DUT :`; 3 `SWAP:`; 4 `SID :`; 5 `NET :`; 6 `UP  :`; 7 `DUT :` (reset/clock/MMCM); 8 `ICAP:`; 9 `CFG :`; 10-12 the fault banner (row 10 `DIP :` when the design has Ethernet); 13 a rule; 14 `MAC ...  hb /` | `fw:firmware/clcd/clcd.c:741-880`; real output: `docs/design/clcd/source/preview_v011_feat-rm-ila-mint.txt` |
| **The board name is a build constant,** `MPS3-01`, not the N1 name. `clcd_set_board_name()` is a seam "deliberately NOT wired to any control verb" | `fw:firmware/clcd/clcd.h:97-99`, `:115-124` |
| **Apps & Ports page:** one service per row with its command. The footer `PB1 tap:next page hold:give DUT` is drawn **inverted, in the fault red** | `fw:firmware/clcd/clcd.c:599-646` (`:644-645`) |
| **Apps page stale row:** it still lists SWD 6920, which is dormant since the JTAG cutover | `fw:firmware/clcd/clcd.c:584-594`; `main.c:59,423-445` (research lane) |
| **KVM notice:** "DUT HAS THE DISPLAY / PRESS PB1 TO RETURN", rows 6-8 inverted | `fw:firmware/clcd/clcd.c:543-553` |
| **Refresh:** re-formatted every 250 ms; only changed cells are pushed, at most 256 bytes per pass; each cell costs 273 bytes | `fw:firmware/clcd/clcd.h:62-74`, `fw:firmware/clcd/clcd.c:82-84`, `:474-493` |
| **Touch targets:** one, row 14 full width, meaning "next page". The hit test ignores the current page, so a tap on the status page's MAC row also turns the page | `fw:firmware/clcd/clcd.c:648-654`, `:1206-1218` |
| **Buttons:** PB1 tap (<800 ms) turns the page; a hold hands the panel to the DUT; any press while the DUT has it asks for it back | `fw:firmware/clcd/clcd.c:1050-1078` |
| **The held-touch hazard, and its v0.11 fix.** On 2026-09-22 a held finger drove clcd to 65-150 ms per pass and RST every 6900 connect. The fix: touch sampled at most every 10 ms, I2C waits capped at 3 ms, clcd budget 30 ms. **It is untested with a finger** | `fw:firmware/clcd/clcd.h:76-84`; commit `d68dd0e`; research lane: `touch.h:411-412`, `main.c:433-464`, `v011_volatile_20260924.txt:28` |
| **KVM ownership is a register,** CLCDKVM `STATUS[0]`. HM reads it with `display query`, and the reply may lag a flip by 7-9 ms | `lx:docs/contracts/shell-regmap.md:556`; net-protocol "display"; `hm:src/harness_manager/cli/cmd_lab.py:82-104` |
| **Backlight is on/off only** (no PWM). With `bl_rst_src=1` the **KVM** owns `CLCD_BL`, so the harness can blink it even while the DUT owns the pixels | `lx:docs/CLCD_PANEL_FACTS.md` §7.5; `lx:firmware/clcd_kvm/clcd_kvm.h:57-67`, `:108-109` |
| **RGB vs BGR is effectively settled:** the clcd_demo bars appeared in the right order on silicon. That demo's init table is generated from the harness's own table. "White looks pale blue" remains a white-point question | `docs/evidence/2026-09-w2/p5_clcd_demo_20260923.txt:23-25,39-43` (platform); `fw:fpga/rp/clcd_demo/hx8347_table.py:7` |

**What the LCD knows about who is connected: nothing.**
- Row 5 is the board's own compile-time IP (`fw:firmware/common/net_proto.h:35-38`).
- The 6900 server adopts one client and closes the extras (`fw:firmware/coordinator/coordinator_net.c:96-109`).
- `net_if.h` has no TCP peer address; only UDP has one (`fw:firmware/common/net_if.h:56-63`).
- No verb writes LCD text.

### 1.2 What the Linux harness already adds (feat/linux-harness)

| Fact | Evidence |
|---|---|
| **harnessd runs the firmware's own `clcd.c`, `clcd_kvm.c` and `touch.c` unmodified,** in userspace over UIO, as one poll-loop process. The clcd service has a 30 ms budget. There is no kernel framebuffer or `stmpe-ts` | `lx:src/linux_harness/sw/harnessd/main_linux.c:355-368`; `lx:.../harnessd/Makefile:97-108`; research lane: `shell_linux.dts:280-299`, `kernel_fragment_harness_slim.config:30,34` |
| **Row 4, D13:** `SID : 0x14E1A2D8  USD : nanosoc [A]` (card state, 16 chars from col 24; no USD state ever raises the banner) | `lx:firmware/clcd/clcd.h:352-370`; commit `85ba16f`; real output in `docs/design/clcd/source/preview_lx_feat-linux-harness.txt` |
| **Row 12 is the "engine row"** (`SYS : linux  ssh claimed SHA256:…`) behind a **weak seam**, so a bare-metal frame stays byte-identical. Rows 0-9 are full, 10 is the DUT IP, 10-12 are the banner, so **row 11 is the last free row** | `lx:firmware/clcd/clcd.h:440-474`; `lx:firmware/clcd/clcd.c:451-460,950-961` |
| **`version.impl`** is `"linux"` from harnessd and **absent** on bare metal | `lx:docs/contracts/net-protocol.md:47-52` |
| **Linux-only verbs already exist** (`slot`), so a Linux-only `hello`/`panel` has a precedent | `lx:docs/contracts/net-protocol.md:373` |
| **The request line limit is 256 B** (`MPS3_NET_LINE_MAX`); the reply limit is 1280 B | `lx:firmware/common/net_if.h:177`; `lx:firmware/common/net_proto.h:591` |
| **DL4** freezes bare-metal *platform* code (lwIP/`main.c`: fixes only). "New service features land in the shared firmware service modules, so both engines get them" | `lx:docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md:121,349` |
| **No presence, hello, heartbeat or session concept** exists anywhere. The peer address is known internally only, for the slot lock | research lane: `lx:firmware/common/net_if.h:156`, `posix_net_if.c:617,633` |

### 1.3 What Harness Manager has today

| Fact | Evidence |
|---|---|
| **The CLI can flip and query the panel owner** (`lab TARGET display harness\|dut\|toggle\|query`) | `hm:src/harness_manager/cli/cmd_lab.py:82-104`; capability `mps3.display_flip` at `hm:src/harness_manager_mps3/capabilities.py:66` |
| **The web UI has no panel control or view** ("lab verbs" is a listed gap) | `hm:docs/TEAM_PLAN.md:22`; only a title at `hm:src/harness_manager/web/static/js/format.js:26` |
| **An STMPE811 ambient reading** is shown when the harness reports `touch_temp`. The adapter polls no faster than 5 s *because* a held touch starves the loop | `hm:src/harness_manager_mps3/telemetry.py:21-24,80,136` |
| **6900 is open, ask, close, every call** ("Never hold 6900 open"). An EBUSY reply may name a `holder`/`peer`, but no firmware sends one | `hm:src/harness_manager_mps3/shell.py:5`, `:375` |
| **Lease state HM can relay:** holder principal, expiry, queue, incoming requests with a 2-minute deadline; polled every 10 s over ssh | `hm:docs/LEASE_REQUESTS.md:37`; `hm:src/harness_manager_mps3/hub.py:1119` |
| **N1 names:** `Candidate.name` (`mps3-01`); the harness `name` key is proposed but not fielded | `hm:src/harness_manager/core/model.py:87,112`; `hm:docs/assessment/2026-09-24/N1_BOARD_NAMES.md` §4 |
| **Tokens:** one accent colour, and status colours ok/warn/err/unk in light and dark, set as CSS custom properties. Icons are lucide | `hm:src/harness_manager/web/static/css/app.css:1-127`; `.../vendor/lucide/icons.js` |

### 1.4 What is missing

| Missing | Where it has to come from |
|---|---|
| A way for HM to tell the board who it is, or the hub lease | new Linux verb `hello` |
| A way for HM to read what the panel shows (page, banner, taps) beyond the owner | new Linux verb `panel` |
| Identify / locate | new Linux verb `locate` |
| Colour beyond white/black/red; per-cell colour; status glyphs | a clcd.c renderer seam |
| A shared vocabulary: the panel says `SID`, `SWAP`, `DUT`, `USD` and `MPS3-01`; HM says shell, program, design, card and `mps3-01` | this design (§4.4) |
| A host-side pixel renderer | **the spike provides one** (`tools/clcd_mock.py`). The platform has only a text-grid preview, `clcd_preview.c` |

**Small stale items found on the way** (for the platform owners, §7.3):
- `clcd_preview --json` does not escape the `\` of the heartbeat spinner, so its JSON is invalid on those frames.
- `CLCD_PANEL_FACTS.md` §7.1 and §9 still say RGB/BGR is unproven.
- The clcd_demo README still says "Never on silicon".
- `B1_RUNBOOK_LINUX.md:279-280` says clcd.c has "no impl field", but the engine row exists.
- The apps page lists SWD 6920.

---

## 2. "Do we know it's attached?": the presence handshake

### 2.1 The shape

```
 HM daemon (per open board)                      Linux harness (harnessd)
   presence thread, every 30 s (5 s while a       coordinator: `hello` -> session table (<=4, TTL)
   request/identify is open), or as the first     clcd.c:  row 0 right = lease badge (seam)
   line of any 6900 connection HM opens anyway              row 11     = hm row        (seam)
        |  {"op":"hello",...}  (<= 256 B)                    banners 10-12 = identify / request
        |------------------------------------------------>   touch hits -> event ring (8)
        |  {"ok":true,"sessions":2,"panel":{page,owner,
        |   banner,card,seq},"events":[{seq,k,on,ms_ago}]}
        |<------------------------------------------------
   emits panel.state / panel.event on the EventBus
```

**Why the control port, and not UDP 6899 or SSH.**
- It crosses the hub's SSH tunnel; UDP identify does not (`hm:src/harness_manager_mps3/identify.py` docstring).
- It needs no extra login.
- HM already opens it.

**The cost:** one extra request/reply at most every 25-30 s per session, on a single-client port. If 6900 is busy (EBUSY, or a swap parks it), that beat is skipped: a 90 s TTL survives two misses.

### 2.2 The wire (proposed; the spike's `hello_line` and its tests pin the budget)

```
-> {"op":"hello","v":1,"sid":"a1b2c3d4","who":"david@srv03335","app":"hm/0.1.0","name":"mps3-01",
    "role":"holder","lease":{"by":"david","left":4332,"q":1,"req":"bob","rl":103},
    "job":{"k":"program","p":42},"ttl":90}
<- {"ok":true,"op":"hello","sessions":2,
    "panel":{"page":"status","owner":"harness","pending":false,"banner":"","card":"nanosoc [A]","seq":17},
    "events":[{"seq":17,"k":"tap","on":"request","ms_ago":2300}]}
```

**The request fields:**

| Field | Rule |
|---|---|
| `sid` | 8 hex chars, random per (HM daemon, board open) |
| `who` | `user@host` |
| `app` | `hm/<version>` |
| `name` | the N1 name (the panel shows it on row 0, so the glass and the rail say the same `mps3-01`) |
| `role` | `holder` (this session holds the hub lease), `owner` (standalone: this daemon has the board open and ran the last write job) or `watch` |
| `lease.by`, `lease.req` | **user part only** |
| `lease.left`, `lease.rl` | **relative seconds**. The board has no wall clock it can trust (bare metal has none; the Linux image may lack NTP), so it ages them on its own monotonic clock from the moment the hello arrived |
| `job` | a running HM job, `k` + percent |

- **Budget:** printable ASCII only, clipped per field (`who` 20, `name` 16, users 12, `app` 12). The worst case is **251 B** against the 256 B line buffer (`tests/unit/test_clcd_mock.py::test_the_worst_case_hello_fits_the_harness_line_buffer`).
- If the Linux lanes raise `MPS3_NET_LINE_MAX` for harnessd, the caps can relax.
- The reply is at most about 400 B, well inside 1280.

### 2.3 Multiple sessions

Several HMs can watch one board. Only one holds the hub lease, and the control socket is per-call, so "who holds 6900" is never a lasting fact to show.

**The board keeps at most 4 sessions,** oldest dropped. It lists them live within their TTL, ordered holder > owner > watcher, most recent first.

**The panel shows the first session and a count:**
- `hm  david@srv03335  +1 watching`.
- With none live: `hm  none connected`.
- After a TTL expiry: `hm  david@srv03335  left 5m ago`. This is the "is it attached?" answer at the bench.

**The lease badge on row 0** is the freshest lease a live session reported, and the holder's own report wins. Examples: `david 1h12m, 1 waiting`, or `not leased` in warn when HM says the board is behind a hub and nobody holds it. With no live session there is no badge: **the panel never keeps showing a lease nobody has confirmed.**

**HM shows the other sessions** from the `panel.sessions` read ("bob@srv03340 watching").

**Trust: presence is display only.**
- Anything that reaches 6900 can send a hello.
- It never authorises anything. The hub lease and the Linux slot claim (TOFU) stay the controls.
- HM cross-checks: if a session on the board claims `holder` but the hub names someone else, HM says so.

### 2.4 Hub mode

The board cannot see fpgahub, so the holder's HM relays what `lease_status()` already gives it. It uses the cached `GET /lease` view (10 s) and makes **no extra ssh calls**.

A watcher's HM also knows the holder from `lease show`, so the badge survives while the holder's HM is closed but a watcher is open.

**Optional later:** fpgahub's own poller could send `hello` with `app:"fpgahub"`, the authoritative lease with no HM open (decision D1).

### 2.5 Board to HM: what the LCD shows

**The `hello` reply** carries `panel` and the event ring. For a board HM is not heartbeating (for example, a Details view), there is a separate read:

```
-> {"op":"panel"}                 <- {"ok":true,"page":"status","owner":"harness","pending":false,"banner":"",
                                      "card":"nanosoc [A]","touch":{"present":true,"cal":true},
                                      "sessions":[{"sid":"a1b2c3d4","who":"david@srv03335","role":"holder","age_s":12}],
                                      "seq":17,"events":[...]}
-> {"op":"panel","frame":true}    <- ... + "rows":[15 x 40-char strings],"roles":"<per-cell role codes, 600 chars>"
-> {"op":"panel","page":"apps"}   <- {"ok":true,"page":"apps"}   (only while the harness owns the panel)
```

- `frame:true` is about 1.25 KB, near the 1280 limit. So `roles` is sent run-length encoded, or split: `frame:"a"` gives rows 0-7 and `frame:"b"` rows 8-14. The Linux lane picks one and measures it.
- **Events are a ring of 8 with a rising `seq`.** Each HM remembers the last `seq` it saw. Nothing is acknowledged or deleted, so two HMs both see a tap.

**HM shows:**
- "LCD: status page · harness owns it";
- "DUT owns the panel";
- "Someone tapped the lease request on mps3-01 12 s ago".

### 2.6 Bare metal (v0.11): graceful degradation

**The feature bits** `presence`, `panel` and `locate` are absent on v0.11, so HM never sends `hello`.

**HM's Front panel card on bare metal:**
- the owner from `display query` (feature `clcd_kvm`);
- a mirror **rebuilt by HM** from `BoardInfo`, labelled "rebuilt from what Harness Manager read, not read from the panel";
- Identify disabled: "needs harness feature 'locate' (Linux harness)".

---

## 3. Interactions

| Interaction | Value | Cost (HM + harness) | Risk | Linux-only? |
|---|---|---|---|---|
| **Identify board** (ConfPro locate): HM → `locate {s:10}`. The backlight blinks at 2 Hz. An accent banner "IDENTIFY: david@srv03335" shows while the harness owns the panel. A tap = "found it" | **High**: it answers "which of these boards is mps3-01?" in a lab | 2 h + 4 h | **None to the touch budget or the loop.** A blink is one KVM `CTRL[5]` write per 250 ms and **zero pixel bytes**. While the DUT owns the panel, only the blink shows (the DUT's picture blinks too; HM says so first) | Yes. Bare metal has no verb, and a `display toggle` fallback would disturb a DUT |
| **Presence row + lease badge** (§2) | **High**: the direct answer to "do we know it's attached?", readable at the bench | 6 h + 6 h (with R2's seams) | A repaint of 1-2 rows on change (≤ 80 cells, spread at 256 B/pass). Spoofable, so display only | Yes |
| **Lease request on the LCD:** banner "bob@srv03340 wants this board / held by david 1:43 to answer / tap: tell david you are here" (held colour). The countdown comes from the request note, relayed as `rl` | **Medium-high**: the person at the bench sees why; the holder gets a physical-presence signal | 3 h + 3 h | **A tap only notifies** (an event the holder's HM turns into "someone at the board tapped"). It never releases, because the tapper may be the requester (decision D2). The banner yields to fault banners | Yes |
| **HM deploy/program progress on the panel:** row 3 `prog pushing nanosoc 42%` plus a bar on row 10 | **Medium**: bench confidence during a 3-7 s swap; shows who programmed it | 1 h + 3 h | The board computes it **itself** from `swap_fsm_icap_bytes()` against the length, because HM cannot talk while a swap parks 6900. HM adds only "by whom" (the last hello's `job`) | Yes |
| **Touch to acknowledge** (identify "found it", request "I'm here") | **Medium**: closes the loop without a phone call | inside the two rows above | **The held-touch hazard.** No new sampling: it reuses the 10 ms-limited `touch_poll`, one event per contact, an O(1) ring write, and no I/O on the touch path. A tap target exists only while its banner shows. Fixing the page-blind hit test is part of R2. **Needs a finger-on-glass regression in B1** (the v0.11 fix is unproven with a finger) | Yes |
| **Remote page switch** (`panel page:"apps"`) and **Hand to DUT / Take back** in the web UI (the existing `display` verb) | **Medium**: the lab verbs are a known web UI gap | 2 h + 1 h | Flipping ownership resets the panel (7-9 ms plus a repaint). The UI asks first, and never flips automatically for a banner | Page switch: yes. Owner flip: **no**, it works on v0.11 today |
| **QR code / URL** to connect HM | **Low**: HM runs on the PC next to the board, and row 5 already shows the IP. There is no `harness-manager://` handler to open | 1 h + **8-10 h** (a QR encoder in harnessd, and a pixel path in a text-cell renderer) | None, but it costs a whole page | Yes. **Recommend: not now.** Put `ssh root@<ip>` on the apps page instead (in the mock-up) |

---

## 4. One design language

### 4.1 One source of truth

`design/tokens.json`. The spike's draft is `docs/design/clcd/tokens.json`. It holds:

| Section | What it holds |
|---|---|
| `color` | Every colour custom property HM's `app.css` defines today, light and dark, **unchanged**. A test asserts equality: `test_tokens_equal_the_web_ui_css_today`. Plus one new family: `held`, `held-soft`, `held-border` |
| `grammar` | The states, each with a colour, a lucide icon, a panel glyph and an ASCII fallback |
| `names` | The shared vocabulary |
| `panel` | The theme (dark, with background forced to true black), the channel order, and the roles, each a fg/bg pair of token references |

**Generated from it:**

| Output | Consumer |
|---|---|
| `web/static/css/tokens.css` | the `:root` blocks now at `app.css:8-127` move there; CSP-safe, same origin |
| `clcd_palette.h` | a role table of RGB565 words plus the extended glyphs, handed to the platform with the tokens' sha256 in its header |
| `web/static/panel/palette.json` + `font8x16.json` | HM's pixel-true mirror |

**The owner is this repo (decision D4).**
- The web UI is the richest consumer and changes most often.
- The panel consumes about 20 words that rarely change.
- The platform keeps its "generated file + freshness" pattern for the header.

### 4.2 Status grammar (web and panel)

| State | Means | Web | Panel (RGB565 as the glass shows it) | Glyph (lucide → 8x16) | ASCII fallback |
|---|---|---|---|---|---|
| ok | working and proven | `--ok` chip | `#5cc68c` → `0x5E31` text | circle-check → ✓ | `OK` |
| warn | works, needs attention soon | `--warn` | `#e8ad58` → `0xE56B` | triangle-alert → ⚠ | `!` |
| err | broken now | `--err`; banner `err@light` | text `0xEC50`; banner white on `0xB124` | circle-x → ✕ | `X` |
| busy | running (programming, reboot, identify) | spinner + accent | `0x7CFE`; banner white on `0x32DA` | loader-circle → `\|/-\` | `\|/-\` |
| unk | unchecked or unknown; never drawn like ok | `--unk`, dashed | `0xCE13` | circle-help → ? | `?` |
| **held (new)** | **someone else holds it**: the lease, the panel (DUT), the control port | new `--held` violet chip | `0xB4FE`; banner white on `0x6A18` | lock → 🔒 | `#` |
| mine | you hold it (only HM knows who "you" are) | accent chip "you" | the panel never says "you" | user | `@` |

- **Today's collision:** the apps page's hint bar is drawn in the fault red. In the proposal it is neutral chrome, like HM's header.
- **The DUT-owns-the-panel notice** becomes **held**, not error.
- **Glyphs:** the Linux font gains 0x80-0x86 (✓ ✕ ⚠ 🔒 👤 ? ●), drawn as 8x16 stand-ins for the lucide icons HM uses (mock-up §4). Bare metal keeps ASCII.

### 4.3 Colour on the panel

- **RGB order:** proven by the clcd_demo bar order (§1.1). `aligned_status_if_bgr.png` shows what an R/B swap would do: every red turns blue.
- **White point:** the pale-blue white is still open. Proposed text white is HM's `#e4e7ec` (`0xE73D`), so the bench check is one red fill plus one text-white patch (R6).
- **Background:** true black (`bg_override`). HM's dark `#0f1216` would quantise to a visible not-quite-black on a backlit TFT.

### 4.4 The same names for things

| Concept | HM says | Panel today | Panel proposed |
|---|---|---|---|
| the board | `mps3-01` (N1) | `MPS3-01` (build constant) | `mps3-01`, from `hello.name`, then the N1 `name` key (via the `clcd_set_board_name` seam) |
| the static | shell `0x72bb0a36` | `SID :` | `shell 0x72BB0A36` |
| what the partition holds | Design | `DUT :` (row 2) | `design nanosoc v1.0 ✓verified` |
| loading it | Program | `SWAP:` | `prog #001 loaded … last ok` |
| user microSD | card (D13) | `USD :` | `card nanosoc [A]` |
| hub lease | Lease chip | none | row 0 badge `🔒 david 1h12m, 1 waiting` |
| a Harness Manager | the app | none | `hm david@srv03335 +1 watching` |
| the engine | harness (bare-metal / linux) | `nanoSoC harness`, `SYS : linux` | `sys linux ssh claimed …` |

**Hex case:** HM prints lower case (`hm:src/harness_manager_mps3/shell.py:97-107`), the panel upper case. Leave each as it is; hex is not prose.

### 4.5 Layout rhythm

The panel's title bar is HM's header:
- the name on the left;
- ownership (the lease chip) on the right;
- on a filled `surface-2` bar.

The key/value rows are HM's `.tile-kv`:
- a muted key (`text-3`);
- a normal value;
- the status word in its state colour, right-aligned.

The banners (rows 10-12, unchanged) are HM's "Needs attention" strip: one problem, its level's colour.

The footer is chrome (the rail).

HM's **Front panel** card shows the pixel mirror with the same facts as chips underneath: page, sessions, lease, card. Mock-up §1, right-hand column.

---

## 5. Product surface

### 5.1 Harness Manager core (board-agnostic; any board with no LCD simply lacks the capability)

| Piece | Proposal |
|---|---|
| Capabilities (append-only, `core/capabilities.py`) | `FRONT_PANEL = "front_panel"` (read the panel's state and mirror), `LOCATE = "locate"` ("Show which board this is"), `PRESENCE = "presence"` (tell the board who is connected). **Not `identify`:** that name is taken by "Identify the harness" (`hm:src/harness_manager/core/capabilities.py:21`) |
| Model (`core/model.py`) | `PanelState{page, owner, pending, banner, card, touch_present, sessions: tuple[PanelSession], events: tuple[PanelEvent], seq, source: "panel"\|"rebuilt", observed_at}` and `PanelFrame{rows, roles, source}` |
| Optional session adapter (`core/pack.py`, read through `getattr` like N1-3) | `session.panel`: `state()`, `frame()`, `hello(Hello) -> PanelState`, `locate(seconds, who)`, `set_page(page)`, `set_owner(owner)` |
| Service (`services/presence.py`) | One thread per open board. It builds `Hello` from the engine: `getpass.getuser()@socket.gethostname()`, `__version__`, `candidate.name`, the role from `LeaseService.view` (cached, so no extra ssh) or the session lock, and `job` from the JobManager. It sends at 30 s, 5 s while `lease.wanted` or a locate is open, skips on HELD, and emits events |
| Events (append) | `panel.state {page, owner, banner, card, sessions, seq, source}`, `panel.event {seq, kind:"tap", on:"identify"\|"request"\|"nav", ms_ago}`, `panel.locate {state:"on"\|"off", until}` |
| Daemon routes (`daemon/panel_api.py`) | `GET /boards/{bid}/panel` → `{panel: PanelState \| null, reason?}` · `GET /boards/{bid}/panel/frame` → `{rows, roles, source}` · `POST /boards/{bid}/identify {seconds?: 1-30}` → `{until}` (short, not a job; 409 HELD while a job runs, 422 UNAVAILABLE with the reason on bare metal) · `POST /boards/{bid}/panel/page {page}` · `POST /boards/{bid}/panel/owner {owner}` (wraps the existing `display`; answers once the flip lands, ≤ 2 s) |
| CLI | `harness-manager panel TARGET [--frame]` · `harness-manager identify TARGET [--seconds N]` (the UI word is **Identify**, the capability is `locate`) |
| Web UI | The **Board tile** gets one line ("Front panel: status page · harness owns it") and an **Identify** button. **Details** gets the **Front panel card**: a canvas mirror at 1x from `palette.json` + `font8x16.json`, chips, Identify, Hand to DUT / Take back, Show apps page. The lease chip (LR-D) gains "also watching: bob@srv03340" from `panel.sessions` |

### 5.2 MPS3 pack

`harness_manager_mps3/panel.py` builds `session.panel` over pyverify (the one-codec rule). It gates on `version.features` (`presence`, `panel`, `locate`) and falls back to:
- `display query` for the owner;
- a **rebuilt** frame from `BoardInfo`, using the same layout code the Linux page uses. `tools/clcd_mock.py`'s `aligned_status` is the seed.

Capability routes: `front_panel` via Ethernet/hub with feature `panel`, falling back to `clcd_kvm` for the owner only. `locate` and `presence` go via Ethernet/hub with their features.

### 5.3 The harness-side contract (for the Linux lanes)

| Verb | Request | Reply | Rate limits |
|---|---|---|---|
| `hello` | §2.2; ≤ 256 B | `{ok, sessions, panel, events}` | host: ≤ 1 per 10 s per `sid` (30 s normally). Board: repaint rows 0/11 only when their text changes; hellos from one `sid` less than 2 s apart get a reply but cause no repaint |
| `panel` | `{}` \| `{"frame":true}` \| `{"page":"status"\|"apps"}` | §2.5 | ≤ 1/s per client; `frame` ≤ 1 per 3 s. A page change only while the harness owns the panel, else `{"ok":false,"err":"dut owns the panel"}` |
| `locate` | `{"s":1-30,"who":"…"}`; `{"s":0}` stops | `{ok, until_ms}` | one at a time (a new one replaces it); blink period 500 ms, from the clcd service tick; the backlight is restored to on when it ends, or if harnessd restarts (clcd init) |

**Feature bits** in `version.features`: `presence`, `panel`, `locate`.

**Panel seams in `clcd.c`,** following the engine-row pattern (weak default, bare-metal byte-identical):

| Seam | Default | harnessd supplies |
|---|---|---|
| `mps3_clcd_title_right()` | row 0 | lease badge |
| `mps3_clcd_session_row()` | row 11, yields to banners | the hm row |
| `mps3_clcd_overlay()` | none | identify/request banner rows 10-12, ranked **below** the fault banners |
| `mps3_clcd_palette()` + a per-cell role plane (600 B) | today's white/black/red | `clcd_palette.h` |
| page-aware `clcd_hittest` | — | returns banner targets; hits go to an 8-entry event ring |

**Budget rules:**
- No new I/O on the touch path.
- The clcd service stays inside its 30 ms harnessd budget, with at most 256 B per pass.
- A presence change costs ≤ 2 rows (≤ 22 KB, spread over passes).
- Identify costs 0 pixel bytes.

---

## 6. Decisions for david (recommendation first)

| # | Decision | Options |
|---|---|---|
| **D1** | Where presence goes on the wire | **(a) a 6900 `hello`, piggy-backed, 30 s beat, 90 s TTL (rec.)**: crosses the hub tunnel, no new port · (b) UDP 6899: no 6900 contention, but it does not cross the hub tunnel, so hub users are invisible · (c) (a), plus fpgahub sends `hello` with the authoritative lease: best truth, needs an fpgahub patch (later, additive) |
| **D2** | What a tap on a lease-request banner does | **(a) it notifies the holder only (rec.)**: the tapper may be the requester · (b) it releases, if the holder opted in to "a person at the board may release" (physical access as authority, ConfPro-like) · (c) display only, no touch |
| **D3** | How far the panel moves (Linux only) | **(a) colour + HM's words + hm row + lease badge, behind seams so bare-metal frames stay byte-identical (rec.)**: the mock-up's "proposed" · (b) colour + hm row only, today's labels: the smallest clcd.c diff · (c) leave the panel alone; HM mirror and Identify only |
| **D4** | Who owns the tokens | **(a) this repo, `design/tokens.json` → `tokens.css` + a generated `clcd_palette.h` handed to the platform (rec.)** · (b) the platform repo, HM vendors them like pyverify · (c) a new small repo (also serves fpgahub's hard-coded web colours later) |
| **D5** | Where the Front panel lives in the web UI | **(a) a line + Identify in the Board tile, the full mirror card in Details (rec.)**: keeps the 2x2 overview · (b) a 5th overview tile · (c) its own section in the rail |
| **D6** | The new "held" colour | **(a) violet `#6b3fc4`/`#b69cf5` (rec.)**: distinct from accent, ok, warn and err · (b) reuse `unk` (khaki), which muddles "unchecked" with "someone else's" · (c) reuse `err` (today's "Leased to someone else" attention line is `err`), which reads as broken |

---

## 7. Lane plan

### 7.1 Harness Manager lanes (parallel worktrees from `main`; each owns only its files)

| Lane | Scope | Owns | CCRs (shared files) | Tests | Est. |
|---|---|---|---|---|---|
| **P1 PANEL-CORE** | The model, capabilities, presence service, daemon routes, events, CLI `panel`/`identify` | `services/presence.py`, `daemon/panel_api.py`, `cli/cmd_panel.py`, `tests/unit/test_p1_*`, `tests/integration/test_p1_*` | `core/model.py`, `core/capabilities.py`, `core/pack.py` (doc of the optional adapter), `docs/CONTRACTS.md`, `docs/API.md`, `daemon/app.py` + `engine.py` wiring (lead), `cli/main.py` | A fake session adapter: cadence (30 s, and 5 s while pending), skip on HELD, TTL rendering, event de-dup by `seq`, 422 on bare metal, the job/HELD rules | 8 h |
| **P2 PANEL-MPS3** | The pack adapter, feature gates, bare-metal owner + rebuilt frame, the `hello` builder (from the spike) | `harness_manager_mps3/panel.py`, `tests/fakes/clcd_panel_shell.py`, `tests/unit/test_p2_*` | `harness_manager_mps3/pack.py` (hook), `capabilities.py` (3 specs), `tests/fakes/virtual_board.py` (a `LINUX_PANEL` profile) | A FakeShell profile answering `hello`/`panel`/`locate` (until pyverify's lands); v0.11 profile → graceful; the 256 B budget; ASCII clipping | 6 h |
| **P3 PANEL-UI** | The Front panel card, canvas mirror, Identify button, Board-tile line, sessions on the lease chip | `web/static/js/sections/panel.js`, `web/static/panel/*`, `tests/web/test_p3_*` | `sections/overview.js` (Board tile), `sections/details.js`, `tests/fakes/t14_mock_api.py`, **LR-D's** lease chip, `app.css` (`.chip.held` only, after P4) | Browser: mirror pixels equal `tools/clcd_mock.py` for one frame; Identify disabled with its reason on bare metal; events update the card | 7 h |
| **P4 TOKENS** | `design/tokens.json` + `tools/gen_tokens.py` → `tokens.css`, `palette.json`, `clcd_palette.h`; the `held` family; the drift gate | `design/**`, `tools/gen_tokens.py`, `web/static/css/tokens.css`, `tests/unit/test_p4_*` | `app.css` (remove lines 8-127, `@import` or link `tokens.css`), `index.html` (the link) | The generated output equals the committed files; tokens equal today's CSS values (the spike's test, moved) | 4 h |

- **Order:** P4 merges first, because it touches `app.css`. P1, P2 and P3 then run in parallel, with P3 against the mock API.
- **Total:** about 25 h, about 1.5 days of wall time.
- **Sister lanes:** LR-D owns the lease UI (P3 sends it a CCR, not an edit); T10 and XVC do not overlap.

### 7.2 Requests to the Linux harness lanes (platform repo; their owners, their files)

| # | Request | Where | Est. |
|---|---|---|---|
| **R1** | `hello` verb + session table (≤ 4, TTL, relative-time ageing), feature `presence`; net-protocol.md; **pyverify `ShellClient.hello` + FakeShell `_op_hello`** (the one-codec rule, TEAM_PLAN §4) | coordinator service module | 6 h |
| **R2** | `panel` verb (state, frame, page); clcd.c seams `mps3_clcd_title_right`/`_session_row`/`_overlay`; page-aware `clcd_hittest`; the 8-entry event ring; feature `panel`; pyverify + FakeShell | `clcd.c` (weak defaults) + `harnessd` (strong providers) | 8 h |
| **R3** | `locate` verb: `clcd_kvm_set_backlight` blink from the clcd tick, an identify banner, a tap = event; feature `locate` | `clcd.c`/`clcd_kvm.c` + harnessd | 4 h |
| **R4** | The colour-role renderer: a per-cell role plane + the `mps3_clcd_palette()` seam (default = today) + glyphs 0x80-0x86 from the generated `clcd_palette.h`; D3's label changes behind the same seam; `clcd_preview` learns roles | `clcd.c`, `font8x16.h` extension, `tools/clcd_preview.c` | 8 h |
| **R5** | Board-side program progress (row 3 + bar) from `swap_fsm_icap_bytes()`/length | `clcd.c` + swap_fsm accessor | 3 h |
| **R6** | **Bench, B1 window, about 10 min:** one red fill + one `#e4e7ec` patch (white point); a locate blink seen with the DUT owning the panel; **a finger held on a banner while pinging 6900 at 10 Hz** (the v0.11 touch fix has never had a finger) | B1 runbook item 5 | 10 min |
| **R7** | Doc and bug fixes found here: `clcd_preview --json` escaping; `CLCD_PANEL_FACTS.md` §7.1/§9; the clcd_demo README; `B1_RUNBOOK_LINUX.md:279-280`; the SWD 6920 row on the apps page | various | 1 h |

- **Total:** about 30 h, plus the bench check.
- **R1-R3 are enough for HM's presence, panel and Identify.** R4 and R5 give the aligned look.
- **HM can merge P1-P3 before R1** (against the fakes); the features light up when a board reports them.

---

## 8. The spike (option (a): a side-by-side mock-up at true panel resolution)

**Why (a):** it answers the design-language question in one picture, and the presence model it needed came with tests, so it covers part of (b) too.

**How it is built:**
- The **real** firmware renderer (`clcd_preview.c` + `clcd.c` from `feat/rm-ila-mint`) was built with host gcc in `/tmp` (deleted afterwards). Its output was saved as the "today" fixture.
- The panel font was extracted from `font8x16.h` into JSON, with its source sha256.
- `tools/clcd_mock.py` (stdlib only) draws every frame pixel by pixel, quantises to RGB565, writes PNGs, and composes `mockup.html`. The HM card on that page uses HM's **real** `app.css` and lucide icons.
- Headless Chrome (HM's Playwright) took the screenshot.

**Files** (`docs/design/clcd/`):

| File | What it shows |
|---|---|
| `mockup.png` (and `mockup.html`) | everything, side by side |
| `today_status@2x.png` / `aligned_status@2x.png` | the status page, before and after |
| `aligned_identify.png`, `aligned_request.png`, `aligned_program.png`, `aligned_link_down.png`, `aligned_apps.png`, `aligned_dut.png` | the proposed states |
| `today_apps.png`, `today_link_down.png`, `today_dut.png` | today's equivalents |
| `aligned_status_if_bgr.png` | what an R/B swap would look like |
| `glyph_*.png` | the proposed 8x16 status glyphs |
| `tokens.json`, `source/` | the draft single source, and the fixtures with their provenance |

**Tests:** `tests/unit/test_clcd_mock.py`, 23 tests, all passing. They cover:
- the tokens equal `app.css` in both themes, and the new family is exactly `held*`;
- RGB565 matches the firmware and clcd_demo constants, and an R/B swap turns red blue;
- the font is the firmware's table;
- the "today" frames come from the real renderer (the red hint bar included);
- the worst-case hello is 251 B, within 256;
- ASCII-only hellos that send only the user part of a principal;
- holder-first ordering, TTL expiry and the "left N ago" text, the table cap of 4, and relative-time lease ageing;
- the lease badge (warn under 5 min; none when standalone; "not leased" behind a hub);
- the hm row fits 40 columns.
