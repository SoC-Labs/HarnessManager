# UI v2 ("the Workbench"): from prototype B to the real app

**Status:** plan, 30 Sep 2026. Lane UI-V2-PLAN, branch `team/ui-v2-plan` (based on `team/fix-pack-4` 9abaa04).
No app code is written here.

**Target design:** `docs/design/ui-v2/prototype-b-round2.html` (Preact + htm, one fake store `S`), plus four
changes david asked for on round 2 (round 3 is being built; this plan already covers it):

1. The Board tab is **sub-pages**, each one screen: Recover · Versions · Connections · Readings · Access · About,
   with a deep-link key each (`#…/board/versions`).
2. A **download bar** for programming (guard → swap → push → verify → card), in the Program strip, with a mini
   bar in the header and on the sidebar card.
3. The **full lease queue**: who waits, their place, tier, how long they want, their message.
4. Build: **custom constraints and floorplan** (`build.rm_xdc`, the partition pblock), and utilisation against
   the pblock in Check.

Also from round 3: the **Front panel lives only on the Overview**, as its largest element (Live/Text, Identify);
the Workbench's right rail holds just Debug and Logic analysers.

**Inputs:** `docs/design/ui-v2/research/ui-map.md` (today's app), `ui-tasks.md` (tasks and clicks),
`lease-hubless-findings.txt`, the code at 9abaa04, and a run of the real daemon over `DemoEngine(showcase=True)`.

**The one fact that makes this cheap:** today's app is already Preact 10.29.8 + htm 3.1.1, vendored, no build
step (`web/static/js/lib.js`, `vendor/VENDOR.md`). The prototype uses the same library, so its components port
almost line for line; the work is wiring them to `store.js` instead of the fake `S`.

**Path shorthands:** `JS/` = `src/harness_manager/web/static/js/`, `SEC/` = `JS/sections/`,
`D/` = `src/harness_manager/daemon/`, `P/` = `src/harness_manager_mps3/`, `SVC/` = `src/harness_manager/services/`.
Every REST path is under `/api/v1`.

---

## Summary

| | |
|---|---|
| Elements mapped | **147 rows: 61 MOVE · 59 REWORK · 21 BACKEND · 6 FAKE** (12 fake items in all, section 1.8) |
| Backend gaps | **12 gaps, about 87 h** with the demo/mock/API.md work; v0.2.0 must-haves about 50 h |
| Test impact | 64 entries in `tests/web`: 26 survive, 29 small edit, 9 rewrite; **about 60 h** with one shared `tests/web/nav.py` |
| Phase 1 (shell, one lane) | about 35 h (1-2 days) |
| Phase 2 (5 UI lanes + 2 backend lanes + docs, in parallel) | about 220 h for v0.2.0 (3-4 days) + about 34 h of backend parts for v0.2.1 |
| Total | **about 255 h for v0.2.0** (290 h with the v0.2.1 parts); v0.2.0 on about **Thu 15 Oct** if Phase 1 starts Wed 7 Oct |

---

## 1. Element map

Status words:
- **MOVE**: the data and the UI exist; only the place changes.
- **REWORK**: the data exists; the UI is new.
- **BACKEND**: needs a new or changed endpoint or service (gap number in section 2).
- **FAKE**: prototype only; dropped or deferred.

"Today" is file:line at 9abaa04. "Feeds it" is METHOD path → daemon handler.

### 1.1 Shell: sidebar, preview, header, tabs, Activity drawer, theme

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| S1 | Board card: 1 px outline, selected = accent outline + 3 px edge (`RailCard`) | card `JS/sidebar.js:340-405`, no outline | `GET /boards` → `D/app.py:959` | REWORK |
| S2 | Health dot (warn on a stale read or a clash) | `JS/sidebar.js:348-359,377`; `healthOf` `JS/format.js:73-87` | `GET /boards/{bid}` → `D/app.py:1431` | MOVE |
| S3 | Board label (panel row 0, e.g. `MPS3-02`) | not in the rail; Board tile `SEC/identity.js:35-39,111-113` | `GET /boards/{bid}/identity` → `D/identity_api.py:82` | REWORK (open boards; last-known for closed ones rides G3) |
| S4 | Kind: Linux / bare-metal | header only, `JS/app.js:165-166` (`identity.harness_impl`) | `GET /boards/{bid}` | REWORK |
| S5 | Identity-clash mark on **both** cards | none (only the own board's tile, `SEC/identity.js:42-51`) | `GET /boards/{bid}/identity` | BACKEND (G10) |
| S6 | Open chip | `JS/sidebar.js:381-382` | `GET /boards` `open` | MOVE |
| S7 | Lease badge on **every** hub board, + "Requested · #n" | `JS/sidebar.js:386` shows it only when `row.open` (`:344`); `LeaseBadge` `JS/lease.js:730-756` | `GET /boards/{bid}/lease` → `D/hub_api.py:375` (needs an open session) | BACKEND (G3) |
| S8 | Design + job spinner + **mini download bar** | `JS/sidebar.js:342,380,387-392` | `GET /boards` `rm_name`, `job`; `deploy.progress` events | REWORK |
| S9 | Route line (hub target or IP) | `JS/sidebar.js:393-397`, `routeText` `:327-334` | `GET /boards` `configured` | MOVE |
| S10 | USB tag: USB · hub / USB · loop / USB · PC / no USB | link-kind icons only, `JS/sidebar.js:360,394-396`; `JS/format.js:89-109` | probe `candidate.links` | BACKEND (G2) |
| S11 | Favourite star | `JS/sidebar.js:399-402`, `toggleFavourite` `:209-216` | `PUT /settings` `general.favourite_boards` → `D/settings_api.py:153` | MOVE |
| S12 | Identify 5 s with a countdown | `LocateButton` `JS/locate.js:187-211` (from `JS/sidebar.js:403`) | `POST /boards/{bid}/identify` → `D/panel_api.py:256` | MOVE |
| S13 | Groups: one per hub ("mapstone-dev") + "This network" | `BoardList` `JS/sidebar.js:413-423` (Favourites / Other boards) | `GET /boards` `configured.via`, `configured.hub` | REWORK |
| S14 | Drag and Alt+Up/Down reorder (not in the prototype; kept) | `JS/sidebar.js:185-322` | `PUT /settings` `general.board_order` | MOVE |
| S15 | Scan ("probes every board and reads each hub's leases") | `JS/app.js:75-83`; `JS/store.js:396-439` | `POST /probe` → `D/app.py:934`; `GET /boards` | MOVE (+ G3) |
| S16 | Foot: Activity (error badge), Settings (update dot), Help | Activity is a tab (`JS/app.js:49`); `SettingsButton` `JS/selfupdate.js:681-686`; Help `JS/app.js:97-98` | `GET /update/app` → `D/update_api.py:368`; `GET /help/tabs` → `D/app.py:1044` | REWORK |
| S17 | Theme Auto / Light / Dark, in the foot only | `JS/app.js:87-92`; second copy `ThemeRow` `JS/settings/sections.js:119-129,408-410` | localStorage `harness_manager.theme` | MOVE (drop the Settings copy) |
| S18 | Service line | `JS/app.js:56-60,93-95` | `GET /health` → `D/app.py:855`; `WS /events` | MOVE |
| S19 | Narrow layout: board select + icons (`MobileTop`) | none | — | REWORK (optional) |
| S20 | Preview: Hub lease, Loaded, Health, Reached by | `BoardPreview` `JS/app.js:277-346`, `PreviewLease` `:257-275`; "This app's lock" `:325-326` | `GET /boards` `lease_known` → `D/app.py:980-985` | REWORK |
| S21 | Preview: **Open and take the lease** / Open to watch | Open board only, `JS/app.js:329-332` | `POST /boards` → `D/app.py:990`, then `POST /boards/{bid}/lease` → `D/hub_api.py:382` | REWORK |
| S22 | Preview: **Request board** / Cancel request, without opening | only on an open board: `JS/hub.js:107,129-133`, `SEC/overview.js:107-108` | `POST /boards/{bid}/lease/request` → `D/hub_api.py:274`; `DELETE …/lease/queue` → `:360` | BACKEND (G3) |
| S23 | Header title + Health chip (Healthy / Stale · read failed hh:mm / Reading…) | `JS/app.js:196-197,203,228-230` | `GET /boards/{bid}` | MOVE |
| S24 | Subtitle: kind · ip:6900 · hub target | `liveTitle`, `JS/app.js:205` | `GET /boards/{bid}` | MOVE |
| S25 | Last error / warning chip → Activity, filtered | none (the stale banner `JS/app.js:373-379` does part of it) | client log `S.log` | REWORK |
| S26 | Read again | `JS/app.js:211-214` → `rereadBoard` `JS/store.js:531-536` | `GET /boards/{bid}`, `/storage/pending`, `/card` | MOVE |
| S27 | Close board | `JS/app.js:217-218` → `requestClose` `JS/lease.js:836-841` | `DELETE /boards/{bid}?release=` → `D/app.py:1440` | MOVE (dialog: M1) |
| S28 | Shell fact, upper-case hex | `JS/app.js:223` | `GET /boards/{bid}` | MOVE |
| S29 | Design fact + "verified hh:mm" chip; % and mini bar while programming | `JS/app.js:224-225`; "verified" only in `SEC/program.js:143,242-246`, `SEC/overview.js:165` | `GET /boards/{bid}`; `deploy.*` events | REWORK |
| S30 | Harness fact | `JS/app.js:158-167,226` | `GET /harness/catalog` → `D/harness_api.py:226` | MOVE |
| S31 | Debug USB fact | none | — | BACKEND (G2) |
| S32 | Hub lease fact: one chip + Acquire / Release / Request… / Cancel | `HubFact` `JS/hub.js:71-126`; `ReleaseButton` `JS/lease.js:759-767` | `GET/POST/DELETE /boards/{bid}/lease` → `D/hub_api.py:375/382/392`; `/lease/request` `:274` | MOVE (one vocabulary) |
| S33 | Facts that leave the header: Build check, tunnel chip, Background chip, header Open chip | `JS/app.js:227`; `JS/hub.js:114-115`; `JS/app.js:115-134,232`; `JS/app.js:215-216` | — | MOVE (Build check → Design chip's tooltip; tunnel → Board › Connections; Background → Overview Health; Open → rail only) |
| S34 | Five tabs with badges; Checks only on hub boards | `SECTIONS` `JS/app.js:34-50`; strip `:234-243` | — | REWORK |
| S35 | The 13 banners (request bar, app update, lease taken, stale read, SD interrupted…) | `JS/app.js:355-388`; `JS/selfupdate.js:370-426`; `JS/lease.js:919-931`; `SEC/checks.js:568-574` | as today | MOVE |
| S36 | Activity **drawer**: this board / all, All / Errors, each row links back to its tab | tab `SEC/activity.js:16-47`; `S.log` `JS/store.js:14,262-267` (client side, 2000 rows, this page only) | `WS /events` | REWORK |
| S37 | Activity "kept 30 days, the CLI writes here too" | none | — | BACKEND (G9) |
| S38 | Toasts | none | — | REWORK |
| S39 | Dark theme, round-2 values | `design/tokens.json` → `css/tokens.css:59-149` via `tools/gen_tokens.py` | — | REWORK (pin the panel palette first, risk R2) |
| S40 | New tokens `--rc-*` (sidebar cards) | none | — | REWORK |
| S41 | Icons the prototype adds: chevron-down, eye, folder-input, panel-right, repeat, tag, wrench | `vendor/lucide/icons.js` has 75; a browser test fails on a missing name | — | MOVE (vendor 7 SVGs) |

### 1.2 Overview (round 3: the Front panel is the largest element; the other cards shrink)

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| O1 | Needs attention: **identity clash** + Fix identity… (or "Go to mps3-01") | only a Board-tile row, `SEC/identity.js:42-51` | `GET /boards/{bid}/identity` | REWORK (+ G10 for "Go to") |
| O2 | Needs attention: last read failed + Read again | `SEC/overview.js:50-54` | `GET /boards/{bid}` | MOVE |
| O3 | Needs attention: SSH not claimed → Board › Access | tile row `SEC/overview.js:358` → `SEC/claim.js:21-60` | `info.claim` | REWORK |
| O4 | Needs attention: your lease ends in ≤ 10 min → Close board… | lease items `SEC/overview.js:86-112` | `GET /boards/{bid}/lease` | REWORK |
| O5 | Attention items that leave: sd, harness, build, tunnel, "Not leased… acquire" | `SEC/overview.js:45-49,55-85,86-112` | — | MOVE (sd → banner + Board › Versions; tunnel → Connections; lease → header) |
| O6 | Front panel, **largest element**: Live (lcd_mirror) / Text, Identify 5-30 s, owner · page · touch | folded Details › Front panel: `PanelCard` `SEC/panel.js:608-661`, `LiveDisplay` `JS/display.js:730-819`, `Mirror` `SEC/panel.js:496-526`, `IdentifyControl` `SEC/panel.js:386-408` | `WS /boards/{bid}/display/ws` → `D/display_api.py:424`; `GET /panel/frame` → `D/panel_api.py:204`; `GET /panel` → `:212`; `POST /identify` → `:256` | MOVE (from the fold to the top; the size and zoom are REWORK inside the move) |
| O7 | Who is watching | `Sessions` `SEC/panel.js:532-546` (from `GET /panel`); viewer registry `D/quiet_api.py:55-67` never shown | `GET /boards/{bid}/panel`; `GET /boards/{bid}/background` → `D/quiet_api.py:67` | REWORK |
| O8 | Identity strip: label (+ "image default"), IP, MAC (clash mark), harness kind · version · image · protocol | Identity card `SEC/details.js:10-48`; `SEC/identity.js:35-39` | `GET /boards/{bid}`; `GET /boards/{bid}/identity` | REWORK |
| O9 | Uptime | none | — | BACKEND (G4) |
| O10 | Debug USB chip in the strip | none | — | BACKEND (G2) |
| O11 | Health KPI + read age ("read 4 s ago · every 10 s", "paused: alice holds it") | header chip; `BackgroundFact` `JS/app.js:115-134` | `GET /boards/{bid}`; `GET /boards/{bid}/background` → `D/quiet_api.py:67` (in ENDPOINTS, never called) | REWORK |
| O12 | Temperature KPI + 30-minute sparkline | latest value only: `SEC/overview.js:354`, `pickTemperature` `:265-268`; `SEC/details.js:81-113` | `GET /boards/{bid}/telemetry` → `D/app.py:1279` | BACKEND (G4, the history) |
| O13 | DUT clock KPI | `SEC/overview.js:355` reads telemetry `dut_clk`, which only the demo emits: **a real board shows "not reported"** | `GET /boards/{bid}/clocks` → `D/app.py:1397` | REWORK (read `/clocks`: fixes a real-board bug) |
| O14 | Harness loop KPI (bare metal: `svc_max_us`, `svc_max_ix`, overruns, skips; Linux: "not reported" + answer time) | generic counters `SEC/details.js:52-79` (from `diag`, `P/shell.py:758-768`) | `info.health.counters` | REWORK (bare metal); answer time BACKEND (G4) |
| O15 | Network KPI (link, rx/tx, drops, errors) | generic counters only; `diag` sends `tx_frames_sent`, `tx_errors`, `rx_drops`, `rx_recover` (platform `contracts/net-protocol.md:749-808`); `rx_frames` and `crc_errors` exist only in the demo (`demo.py:180-182`) | `info.health.counters`; link/speed only in `stats` | BACKEND (G4: `stats` pass-through) |
| O16 | Partition swaps KPI (count, ICAP bytes, last swap) | `icap_bytes` is a `diag` key shown as a generic counter; the swap count is `stats.swap_n`, never served | `info.health.counters`; `stats` | BACKEND (G4) |
| O17 | Design card: loaded + tags (ILAs, kit-built, baseline), verified when, Boots next, OS slots A/B, "Can load N designs" | Design tile `SEC/overview.js:151-168`; card row `:281-312` | `GET /overlays` (`OverlayRef.ltx_sha256`, `receipt_sha256`, `core/pack.py:64-65`); `GET /card` → `D/app.py:1296` | REWORK |
| O18 | Lease card (hub board): holder, until, taken at, meter, the **full queue**, running / last / next checks | lease tile row `SEC/overview.js:316-321`; queue = your own place only (`JS/lease.js`) | `GET /boards/{bid}/lease`; `GET /boards/{bid}/checks` → `D/hil_api.py:68` | BACKEND (G11) |
| O19 | Access card (desk board): no hub lease, who drives, who watches | none | viewers, `D/quiet_api.py:67` | REWORK |
| O20 | Consoles and debug card: each console's last line, Debug state + gdb port, ILAs state | Consoles tile `SEC/overview.js:172-209`; Debug tile `:239-263`; `XvcLine` `:217-237` | consoles, debug, xvc status | REWORK |
| O21 | Recent activity (5 rows) → drawer | Activity tab | client `S.log` | REWORK |
| O22 | Details fold goes (Identity, Telemetry, Health, Capabilities) | `SEC/details.js:10-131`; fold `SEC/overview.js:374-405` | as today | MOVE (→ Board › Readings and About) |
| O23 | Board tile's Reset DUT and Reboot go | `SEC/overview.js:365-368` | `POST /reset`, `POST /controller/reboot` | MOVE (→ Workbench toolbar, Board › Recover) |

### 1.3 Workbench (round 3: the right rail holds Debug and Logic analysers only)

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| W1 | Design picker: a combo with a filter | `OverlaysCard` `SEC/program.js:63-99`; list `JS/store.js:566-581` | `GET /boards/{bid}/overlays` → `D/app.py:1287` | REWORK |
| W2 | Tags: loaded now, ILAs, kit-built, imported, baseline | "Loaded now" only, `SEC/program.js:82,92` | `OverlayRef.ltx_sha256` / `receipt_sha256` (`core/pack.py:64-65`); greybox = rm_id 0 (`SVC/deploy.py:403`) | REWORK ("N ILAs" as a count needs the ILA names: G8, else "ILAs" without a count) |
| W3 | Inline preflight chips (same shell, rm_id, fits, lease, loaded now) | `PreflightCard` `SEC/program.js:101-127`; `runPreflight` `JS/store.js:631-654` | `POST /boards/{bid}/preflight` → `D/app.py:1321` (items `SVC/deploy.py:87-93`; no lease item) | REWORK (the lease chip comes from the page's lease state) |
| W4 | Arm → Program, Restore baseline, the reason line | `SEC/program.js:178-218`; gate `JS/actions.js:42-75` | `POST /boards/{bid}/deploy` → `D/app.py:1330`; `POST /boards/{bid}/restore` → `:1364` | MOVE |
| W5 | Card line: "Boots next …" + keep on the card | `KeepBox` `SEC/program.js:152-169`, show rule `:28-35` | `GET /boards/{bid}/card` → `D/app.py:1296` | MOVE |
| W6 | **Download bar**: guard → swap → push (bytes = clearing + partial) → verify (→ card), rate and ETA | `ProgressCard` `SEC/program.js:225-272`, `PHASES` `:17-18` | `deploy.progress {phase, bytes, total}` (`SVC/deploy.py:15,212`); every event frame carries `at` (`D/wire.py:80`), so rate and ETA are client-side | REWORK |
| W7 | Import… button + "Import a design…" at the foot of the picker | none (only Build step 6, `SEC/build.js:626-646`) | `POST /kits/pack {path, import:true}` → `D/kit_api.py:274` | REWORK (the dialog is M6) |
| W8 | Console switcher: DUT console, Shell console (bare metal), new-line counts | sub-tabs `SEC/consoles.js:234-242`; names `JS/store.js:695-707` | `GET /boards/{bid}/consoles` → `D/consoles_api.py:227`; `WS /boards/{bid}/consoles/{name}` → `D/app.py:1486` | MOVE |
| W9 | **Harness console** (tty_02, Linux; only the lease holder types) | half there: a hub board's `shares = { fpga_uart2 = "/dev/<target>/tty_02" }` already lists it as `fpga_uart2` / alias `shell` (`P/hub.py:9-20`, `SVC/console.py:115-119`) when a share runs; no label, no write gate | `GET /consoles`, console WebSocket | BACKEND (G1b) |
| W10 | **MCC console** (tty_00, read-only) | none: the MCC is never a console endpoint (`SVC/console.py:78-81`); transcripts are kept (`P/mcc.py:690,706`, `P/hub_mcc.py:430,595,740`) and never served | — | BACKEND (G1a: a transcript, not a live stream) |
| W11 | Console row: state, access (you can type / read-only), route + baud, Save, Clear, Attach with screen, Export to TCP | `SEC/consoles.js:42-205` (baud `:42-75`, screen `:93-123`, export `:150-162`, Save/Clear `:163-182`) | `/consoles/{name}/export` → `D/app.py:1092`; `/pty` → `D/consoles_api.py:182,187`; `/baud` → `:204,210` | MOVE |
| W12 | **Reset DUT** (armed) in the console toolbar; switches to the DUT console | Power `SEC/power.js:30-40,181-200`; tile `SEC/overview.js:365` | `POST /boards/{bid}/reset` → `D/app.py:1384` | MOVE |
| W13 | Send a line + line ending | `SEC/consoles.js:134-149,197-205` | console WebSocket | MOVE |
| W14 | Debug card: Open session / Close session, Detect, IDCODE, gdb line, ports | `SEC/debug.js:39-109` | `POST /debug/detect` → `D/app.py:1101`; `/debug/up` → `:1108`; `/debug/down` → `:1121`; `GET /debug` → `:1421` | MOVE (rename "Open session") |
| W15 | Logic analysers card: Open / Close, URL, probes, Copy Tcl (sets PROBES.FILE), BYO | `XvcCard` `SEC/xvc.js:293-394` | `POST /xvc/open` → `D/xvc_api.py:79`; `/close` → `:99`; `GET /xvc/tcl` → `:106`; `/xvc/ltx` → `:118`; `GET /xvc` → `:141` | MOVE |
| W16 | "No ILAs in this design" + **Program nanosoc_ila…** shortcut | none | overlays list | REWORK |
| W17 | Rail folds to icons below 1280 px, with flyouts | none | — | REWORK |

### 1.4 Build (five steps; XDC folds in)

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| B1 | Stepper Setup · Design · Build · Check · Add (done / now / running / failed / waits / warn) | six stacked steps, `SEC/build.js:317-667` | `GET /boards/{bid}/guide` → `D/kit_api.py:230` (`SVC/kit/guide.py`: `STEPS` `:42-43`, `STATES` `:44`, `guide()` `:236`) | REWORK |
| B2 | "Step 3 of 5 · Build · Vivado running · impl · 12 min · Next: Check" | guide `next` | same | REWORK |
| B3 | "Ready to build" line (target · tools · kit; licence warning in amber) | steps 1-3, `SEC/build.js:345-449` | guide `vivado`, `profile`; `GET /boards/{bid}/kit` → `D/kit_api.py:195` | REWORK |
| B4 | Setup panel: target, Vivado path, licence tier, kit id and version, Verify, Download kit zip, Fetch again | `SEC/build.js:345-449` | `GET /kits/{sid}` → `D/kit_api.py:124`; `/kits/{sid}/zip` → `:181`; `POST /kits/fetch` → `:130` | MOVE |
| B5 | Design panel: Example / My RTL / Paste .json | design picker `SEC/build.js:278-315`; paste `SEC/xdc.js:113-177` | `GET /boards/{bid}/xdc` → `D/xdc_api.py:141` | REWORK |
| B6 | My RTL: a folder or `.f` list → sources in compile order, top, a written `<name>.json` | none | — | BACKEND (G8) |
| B7 | rm_id proposed (user range, stable per name) | `SVC/kit/guide.py:518`, `SVC/kit/script.py:196` (`propose_rm_id`) | guide | MOVE |
| B8 | Generics editor (`build.generics`, a file generic checked for existence) | none (the script reads them, `SVC/kit/script.py:19`) | — | REWORK (written with B6's design writer) |
| B9 | **Constraints and floorplan** panel: an RM_XDC (`build.rm_xdc`, `read_xdc -cell u_rp_dut` after link), the partition pblock's facts | `rm_xdc` in `SVC/kit/script.py:119,288`, gate `rm_xdc_present` (`templates/build_rm.tcl.template:367,448`); pblock facts only inside the RM kit zip (`<name>_pblock.md`, `SVC/xdc/kits.py:167,288,428`) | pin model `P/pins/mps3_board_pins.json` `shells.<id>.pblock` | REWORK (+ G8: the pblock in the guide JSON) |
| B10 | Download the RM kit (wrapper + XDC); More exports › full-board XDC | XDC tab `SEC/xdc.js:40-43,113-177` | `POST /boards/{bid}/xdc/export` → `D/xdc_api.py:148` | MOVE |
| B11 | Write the build directory (folder, threads) + Download as a zip | step 5 `SEC/build.js:540-597` | `POST /guide/script` → `D/kit_api.py:235` | MOVE |
| B12 | Run Vivado: the command to copy, "watching <dir>" | `SEC/build.js:540-597,671-684` | guide `receipt` | MOVE |
| B12a | **Run it your way**: Batch (today) · Vivado GUI (`-mode gui -source build_rm.tcl -log build_rm.log`) · your open Vivado (`cd dir; set argv {…}; source build_rm.tcl`), + "Stop after link to floorplan" (`STOP_AFTER=link` keeps the linked design open for nested pblocks) | none; lane KIT-INTERACTIVE (`team/kit-interactive`) is adding `kit build --gui/--stop-after` tonight | `POST /guide/script` → `D/kit_api.py:235` (passes the lane's command strings through) | REWORK (on KIT-INTERACTIVE's CLI output; when the user's own session writes the log elsewhere, HM watches the receipt only and says so) |
| B13 | Vivado running: elapsed, stage (`HM_STAGE` in `build_rm.log`), no second Vivado | none | — | BACKEND (G8) |
| B14 | Finished: the verdict line (`HM_RM_BUILD_*`) | guide receipt (anchored verdict, FIX-PACK-3) | `GET /boards/{bid}/guide` | MOVE |
| B15 | Check: stages, four groups (identity, timing, pr_verify, files), the one failed gate, its fix and a button | step 6 `SEC/build.js:601-667`; "When it goes wrong" `:687-747` | `POST /kits/check` → `D/kit_api.py:264` | REWORK |
| B16 | **Utilisation against the pblock** in Check | none: `build_rm.tcl` writes `<name>_util.rpt` against the pblock (template `:473-477`) and nothing parses it | — | BACKEND (G8) |
| B17 | Add: "Add to the Workbench and program…" (kit pack --import, lands on the Workbench with it picked) | "Add to Program" `SEC/build.js:626-646` | `POST /kits/pack {import:true}` → `D/kit_api.py:274` | MOVE |
| B18 | Lease note: building needs no lease | none | page lease state | REWORK |
| B19 | Foot: the same on the command line; every build gate and its fix (21) | `SEC/build.js:687-747` | guide `troubleshooting` | MOVE |
| B20 | Demo menu, "Simulate: Vivado finished" | — | — | FAKE |

### 1.5 Board: sub-pages Recover · Versions · Connections · Readings · Access · About

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| BD1 | Sub-page nav + deep-link keys (`#…/board/recover` …) | none | — | REWORK |
| BD2 | **Recover** frame: 5 rungs gentle → heavy, severity bar, typical time, what each keeps (DUT, consoles, design, lease, FPGA config), can/cannot line, "wait while the card is written" guard | spread over Power, Program, SD card, Overview | `GET /card` (card job), `SVC/reset_guard.py` | REWORK |
| BD3 | Rung 1 Reset DUT | `SEC/power.js:30-40,181-200` | `POST /boards/{bid}/reset` → `D/app.py:1384` | MOVE |
| BD4 | Rung 2 Back to greybox (= Restore baseline) | `SEC/program.js:201-206` | `POST /boards/{bid}/restore` → `D/app.py:1364` | MOVE |
| BD5 | Rung 3 Restart the shell | `SEC/power.js:202-223` | `POST /reset {target:"shell"}` → `D/app.py:1384` | MOVE |
| BD6 | Rung 4 Reboot via the MCC | `SEC/power.js:42-53,225-240`; `SEC/sd.js:141-146` | `POST /boards/{bid}/controller/reboot` → `D/app.py:1144` | MOVE |
| BD7 | Rung 5 Power-cycle | `SEC/power.js:108-177` | `GET /power` → `D/power_api.py:68`; `POST /power/cycle` → `:87` | MOVE (shown only where `boards.toml` has a plug) |
| BD8 | **Versions**: one "Harness & OS versions" list; per row marks (running, slot A/B, netboot, offered, previous), verdict, facts | `HarnessVersionsCard` `SEC/harness.js:491-555`, rows `:217-255` | `GET /harness/catalog` → `D/harness_api.py:226`; `GET /harness/releases/{v}` → `:260` | REWORK |
| BD9 | Install… with its plan: steps, times, live step + ETA, "don't reboot" warning, lease-too-short warning | `InstallPanel` `SEC/harness.js:349-409` | `POST /boards/{bid}/harness/install` → `D/harness_api.py:294` (job); steps from `SVC/update/planner.py:555-609`, progress as `update.progress` → `job.progress` (`SVC/update/executor.py:355-793`, `D/update_api.py:185-193`) | REWORK (the phase map in `SEC/update.js:49-58` lacks `os:` → write-os-slot, rollback-first and confirm-os-slot: 0.5 h) |
| BD10 | OS slot tiles A / B (writing, reading back, torn, fell back, running · default, booting, fallback) | none; `GET /slots` (`D/card_api.py:38`) exists and nothing reads it | `GET /boards/{bid}/slots`; `GET /card` `os_slots` | REWORK (read) |
| BD11 | "Slot B did not boot: make A the default again" | none (CLI `slot rollback`) | — | BACKEND (G6) |
| BD12 | Roll back to X (boot the other slot, or write it again) | release rollback `SEC/harness.js:413-471`; channel rollback `SEC/update.js:114-120` | `POST /boards/{bid}/harness/rollback` → `D/harness_api.py:351` | REWORK (+ G6 for the slot path) |
| BD13 | Overlay store line: "boots nanosoc at power-on (kept)" + Clear | read-only card row `SEC/overview.js:281-312` | `GET /card`; `card commit` / `card clear` are CLI only | BACKEND (G6) |
| BD14 | Netboot board: "the hub serves rc2_v7n at every cold boot" + Ask for it… (copy a request) | none | `GET /card` (no card) + image name | REWORK |
| BD15 | History in Activity | `SEC/harness.js:473-487,540-542` | `GET /boards/{bid}/harness/history` → `D/harness_api.py:338` | MOVE |
| BD16 | The older channel checker and "The app" card go | `SEC/update.js:168-234` | `POST /update/check` → `D/update_api.py:197` | MOVE (removed; the app keeps its banner + Settings) |
| BD17 | Configuration SD (bare metal with a route): route, holds, last backup, Back up now; interrupted-install recovery | `SEC/sd.js:15-157`; banner `JS/app.js:380-386` | `/storage/pending` → `D/app.py:1182`; `/storage/backup` → `:1189`; `/storage/install` → `:1203`; `/storage/restore` → `:1234` | MOVE |
| BD18 | Releases 2.0.1 / rc2_v8 and 1.1.1, and the slot-state demo buttons | — | — | FAKE |
| BD19 | **Connections**: diagram; Ethernet row (tunnel, 6900) | tunnel chip `JS/hub.js:114-115`; Links `SEC/details.js:25-27` | `GET /boards/{bid}/tunnel` → `D/hub_api.py:262` | REWORK |
| BD20 | Connections: Debug USB row (to the hub / this PC / looped back / none; what it gives; the fix) | none | — | BACKEND (G2) |
| BD21 | Connections: SSH row, JTAG row | `SEC/claim.js`; none for JTAG | `info.claim` | REWORK |
| BD22 | mps3-03's USB loop (J2 → J8, ISP1763) | — | — | FAKE (the mint-4 plan) |
| BD23 | **Readings**: Health, Temperature (with its source), DUT clock | Telemetry card `SEC/details.js:81-113`; supply `SEC/power.js:127-163` | `GET /telemetry` → `D/app.py:1279`; `GET /power` → `D/power_api.py:68` | MOVE |
| BD24 | Readings: "Supply · not measured" | — | — | FAKE (drop the row) |
| BD25 | Readings: counters since the shell started | `SEC/details.js:52-79` | `info.health.counters` | MOVE |
| BD26 | Readings: DUT clock presets 25/50/100 MHz + board oscillators | `SEC/clocks.js:12,27-76` | `GET/POST /clocks` → `D/app.py:1397,1405`; `GET /controller/osc` → `:1136` | MOVE |
| BD27 | **Access**: SSH claim (claim, release) | `SEC/claim.js:21-60` | `POST /boards/{bid}/claim` → `D/claim_api.py:67` | MOVE |
| BD28 | Access: Identity + Fix identity… (or Go to the other board) | `SEC/identity.js:18-123` | `GET/POST /boards/{bid}/identity` → `D/identity_api.py:82,88` | MOVE |
| BD29 | Access: who may drive this board (lease rule, hub-less: the app's lock) | header lease; `lease-hubless-findings.txt` | `GET /boards/{bid}/lease`; `GET /boards/{bid}/lock` → `D/app.py:1259` | REWORK |
| BD30 | **About**: board id, hub target, image, feature tags, "Not available here" | Identity + Capabilities cards `SEC/details.js:10-48,115-131` | `GET /boards/{bid}`; `GET /packs` → `D/app.py:926`; `GET /session` → `:1263` | MOVE |

### 1.6 Checks (hub boards only)

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| C1 | Start a run: plan, writes, until / every, keep the lease, the announcement written for you, Start / Stop | `StartForm` `SEC/checks.js:225-274`; `RunPanel` `:324-357`; announcement `:377-397` | `POST/DELETE /boards/{bid}/checks` → `D/hil_api.py:74,85` | MOVE |
| C2 | Past runs + REPORT.md viewer | `PastRuns` `SEC/checks.js:421-469` | `GET /boards/{bid}/checks/{run}/report` → `D/hil_api.py:62` | MOVE |
| C3 | The tab only on hub boards | always shown, `JS/app.js:48` | — | REWORK (Phase 1) |
| C4 | "Running now": another machine's run (mps3-01's soak by david@mapstone-dev) | none | — | FAKE (HM sees only its own service's runs; there is no hub run registry) |

### 1.7 Dialogs

| # | Element (prototype) | Today | Feeds it | Status |
|---|---|---|---|---|
| M1 | Close: **Restore baseline, release and close** (default when yours and a design is loaded) / Release and close / Close and keep the lease | `CloseConfirm` `JS/lease.js:868-909` (no restore choice) | `POST /restore` → `D/app.py:1364`, then `DELETE /boards/{bid}?release=true` → `:1440` | REWORK |
| M2 | Release | `ReleaseConfirm` `JS/lease.js:800-831` | `DELETE /boards/{bid}/lease` → `D/hub_api.py:392` | MOVE |
| M3 | Request: message, how long, your place | `RequestForm` `JS/lease.js:635-663` | `POST /boards/{bid}/lease/request {message, ttl_s}` → `D/hub_api.py:274` | MOVE (+ G11 carries "how long") |
| M4 | Fix identity: now vs the hub entry, Arm, Set and restart the shell | inline `FixDialog` `SEC/identity.js:53-96` | `POST /boards/{bid}/identity` → `D/identity_api.py:88` | MOVE |
| M5 | Add a board: From a hub (its targets and their leases) / By address (Test, save to boards.toml) | three places: `JS/sidebar.js:490-516`; `JS/settings/sections.js:253-272`; `JS/settings/hubs.js:97-124` | `POST /probe`; `POST /settings/test` → `D/settings_api.py:119`; `POST /hubs/{name}/boards` → `D/hubs_api.py:145` | REWORK |
| M6 | **Import a design**: a folder / zip / receipt, From Build, a path; five checks; refused with exit 14 | none | `POST /kits/pack {path, import:true}` → `D/kit_api.py:274` (path only) | BACKEND (G5) |
| M7 | Settings: opens on General; "Open a board on" Workbench / Overview; rows nothing reads are hidden | `SettingsModal` `JS/selfupdate.js:656-679`; panes `JS/settings/sections.js:388-423`; last pane in sessionStorage | `D/settings_api.py:87-159` | REWORK (+ one settings row, `general.open_on`, in G12) |
| M8 | Help organised by the app's pages (+ the CLI's 19 topics under "CLI") | `HelpModal` `JS/app.js:405-430`; topics `cli/helptext.py:482-501` | `GET /help/tabs` → `D/app.py:1044` | REWORK |
| M9 | Force release (not in the prototype; kept) | `ForceConfirm` `JS/lease.js:665-715` | `POST /boards/{bid}/lease/force` → `D/hub_api.py:328` | MOVE |
| M10 | Restart to update (not in the prototype; kept) | `JS/selfupdate.js:442-489` | `POST /update/app/apply` → `D/update_api.py:376` | MOVE |
| M11 | "What changed" and the prototype bar | — | — | FAKE |

### 1.8 FAKE items, in one place

| # | What | Why it is fake | Decision |
|---|---|---|---|
| F1 | Release 2.0.1 / rc2_v8, bare-metal 1.1.1 (BD18) | not published | drop; the list shows the signed catalogue |
| F2 | mps3-03's USB loop, "USB · loop" (BD22) | the mint-4 plan (ISP1763 USB host) | keep `self` as a value of G2 so it lights up after mint 4; nothing shows it today |
| F3 | Networked power plug on every board (rung 5 "none is set") | no lab board has one | show rung 5 only where `boards.toml` has `power.*` |
| F4 | "Supply · not measured" (BD24) | no reading | drop |
| F5 | "Running now" by another host (C4) | no hub run registry | drop; HM's own runs stay |
| F6 | Build Demo menu, Simulate buttons (B20); slot demo buttons | prototype playback | drop |
| F7 | "What changed", the prototype bar (M11) | prototype | drop |
| F8 | "CPU" / "no CPU" tag on designs, ILA names ("u_ila_ahb (AHB bus)") | no manifest field says so | drop "CPU"; ILA names come with G8's `.ltx` read, else "ILAs" |
| F9 | Fixed numbers: temperatures, rx/tx, swaps, uptime seeds (`OV_SEED`) | fake data | replaced by G4 |
| F10 | The hub entry as the source of the corrected label and MAC in Fix identity ("Set to (hub entry)") | HM's fix dialog takes the values from the user | keep today's source; see G10 |
| F11 | Alice, the queue of one, the message in the queue over REST | REST hubs carry no request notes (`docs/HUB_MODE.md` "degraded mode") | real queue via G11; messages only over the SSH transport |
| F12 | "Harness Manager answers in 0.1 s" as a stored figure | not measured | G4 measures it |

### 1.9 Counts

| Area | MOVE | REWORK | BACKEND | FAKE | Rows |
|---|---|---|---|---|---|
| Shell | 19 | 16 | 6 | 0 | 41 |
| Overview | 5 | 12 | 6 | 0 | 23 |
| Workbench | 8 | 7 | 2 | 0 | 17 |
| Build | 8 | 9 | 3 | 1 | 21 |
| Board | 14 | 10 | 3 | 3 | 30 |
| Checks | 2 | 1 | 0 | 1 | 4 |
| Dialogs | 5 | 4 | 1 | 1 | 11 |
| **Total** | **61** | **59** | **21** | **6** | **147** |

The separate FAKE list (1.8) holds 12 items; 6 of them are rows above, the rest are data inside rows.

---

## 2. Backend gaps

Every new route also needs: a `DemoEngine` answer (`src/harness_manager/demo*.py`, the browser tests run on it), a
`MockDaemon` answer (`tests/fakes/t14_mock_api.py`; `test_t14_mock_contract.py` checks its routes equal API.md),
a row in `docs/API.md`, and a name in `JS/api.js` `ENDPOINTS` (`test_t14_static.py` checks every name against
API.md). Count about 1 h per route for that; it is included below.

| Gap | What is missing | Smallest design | Owning files | Hours | v0.2.0? |
|---|---|---|---|---|---|
| **G1a** MCC console, read-only | nothing serves tty_00; a live tail would be a **second reader** | a transcript, not a stream: `GET /boards/{bid}/controller/log` = a per-board ring of the last N MCC transcripts (each op's `last_transcript`) + the last boot log, with times and the op name; memory only, never touches the tty | `D/app.py` (or new `D/controller_api.py`), `SVC/telemetry.py`, `P/mcc.py`, `P/hub_mcc.py` | 4.5 | later |
| **G1b** Harness console (tty_02) | shown as `fpga_uart2`/`shell` when a hub share runs; no role, and **anyone may type** | console rows gain `role: "linux-root"`, `writable`, `read_only_reason`; the console WebSocket drops input when the lease is someone else's (`SVC/console.py:1211-1238`, `D/app.py:1486-1530`) | `SVC/console.py`, `D/consoles_api.py`, `D/app.py` | 4.5 | yes |
| **G2** Debug USB route | no field; the route is implicit in which controller is built (`P/mcc.py:1133-1161`) | `mcc_route: hub\|pc\|self\|none` + `mcc_route_reason` in `GET /boards/{bid}/session` and `info` (pc = `Mps3Controller`, hub = `HubMccController`, self = `harness_mcc.route()` loopback/scc, else none); for a closed board in `GET /boards`, derived from the candidate's links (`hub-mcc://` = hub, else "unknown until opened"); optional `debug_usb = "…"` in the board's `boards.toml` table as declared intent | `D/app.py:959-988,1263-1277`, `D/configured.py`, `P/mcc.py`, `P/harness_mcc.py` | 4.5 | yes |
| **G3** Lease on every hub board; request without opening | lease reads are per board and need an open session (`D/hub_api.py:375-380`) | `HubClient.lease_overview()`: SSH `fpgahub status --json`, REST `GET /api/v1/status` (fpgahub returns `lease_state`, `holder`, `user`, `expires_at`, `queue_length`, `in_use` for **every** target; `GET /api/v1/boards` has the first three, fpgahub `schemas.py:108-113`, v0.3.0 has them); `LeaseService.hub_overview(hub)` cached 15-30 s, seeding `lease_known` for every configured board; route `GET /hubs/{name}/leases`; the request / cancel / view routes also work for a configured board with no session. Closed boards' label and kind come from `SeenIdentities` (`<state>/identity/seen.json`) on the same `GET /boards` row | `SVC/lease.py`, `P/hub.py`, `transports/hub_rest.py`, `D/hubs_api.py`, `D/hub_api.py`, `D/app.py` | 10 | yes |
| **G4** Readings | no uptime, no answer time, no trend, `stats` never served whole | additive fields on `GET /boards/{bid}`: `answer_ms` (timed around `engine.info`, `D/app.py:1435`), `uptime_s` / `os_uptime_s` (from identify / `stats.up_ms`); whitelisted `stats` keys (`swap_n`, `icap`, `rxdrop`, `txerr`, `link`, `spd`, `svc_*`) from the read the adapter already makes (`P/telemetry.py:108-127`); `GET /boards/{bid}/readings/history` = a daemon ring (about 720 points per board) fed by every `/telemetry` and `info` result, no extra board contact | `D/app.py`, new `SVC/history.py`, `P/telemetry.py` | 8.5 (answer + uptime + stats: 3) | answer/uptime/stats yes; history later (the sparkline hides until then) |
| **G5** Import a design | `POST /kits/pack {path, import:true}` takes a receipt or build dir only; no overlay-folder import; **no upload anywhere** (JSON bodies only, `D/app.py:114`) | (1) `POST /overlays/import {path}` for a packed overlay folder (`manifest.json` + partial + clearing) → `adapter.import_overlay` (`P/kit.py:332-339`); (2) `POST /kits/pack/upload?import=1` with a raw `application/zip` body streamed to `kits.work_dir` (cap 256 MB → 413), safe extract (no absolute paths, `..`, symlinks), reuse `build.load` / `check_path` / `pack_receipt`, delete the temp dir; reject a receipt whose file fields are not basenames (`SVC/kit/build.py:91-98` joins them). A folder is zipped in the browser, or given as a path | `D/kit_api.py`, `SVC/kit/build.py`, `P/kit.py` | 9 (path only: 2.5) | path yes; zip later |
| **G6** Versions: OS slots and the card | the install steps and progress exist (`SVC/update/planner.py:555-609`, `update.progress` → `job.progress`), but slot rollback and card commit/clear are CLI only (`D/card_api.py:10-13` is read-only) | `POST /boards/{bid}/slots/rollback {confirm, reboot?, wait_s?}` (job around `SlotService.rollback`, `SVC/slots.py:224`, which checks the lease; claim-locked over board SSH); `POST /boards/{bid}/card/commit` and `/card/clear` (jobs around the CLI's card verbs); planner blocker text for a netbooted board ("the OS is the hub's TFTP image", `SVC/update/planner.py:509`) | `D/card_api.py`, `SVC/slots.py`, `SVC/update/planner.py` | 6 | yes |
| **G7** Recover: the lease is enforced in the page only | Program, Restore, Reset, Reboot, Restart shell, Power-cycle, Clock and Debug check the lease in the browser only; bare metal advertises `reset_shell` but has no `shell` target (`P/shell.py:822-831`) | reuse `lease_gate.lease_state` (`SVC/update/lease_gate.py`) in restore, reset, reboot, power-cycle, clock, deploy, debug up: 409 HELD when the lease is someone else's, keeping today's force/consent escape; map bare-metal `reboot` to the `shell` target | `D/app.py`, `D/power_api.py`, `P/shell.py` | 4 | yes |
| **G8** Build | the stage of a running Vivado is only inside a `detail` string; no start time; the six guide steps; nothing parses `<name>_util.rpt`; no RTL-folder scan; pblock facts only inside the kit zip | (a) the template prints `HM_STAGE <n> <clock seconds>` (the parser takes `split()[0]`, `SVC/kit/build.py:73-74`, so old logs still parse); `Guide.to_json` adds `running: {stage, stage_index, stages, started_at, stage_started_at, log_mtime, fresh}`; (b) parse `<name>_util.rpt` (LUT, FF, BRAM tiles, DSP used vs the pblock's) into the check result; (c) the pblock facts (`P/pins/mps3_board_pins.json` `shells.<id>.pblock`: `pblock_rp_dut`, X2Y0-X3Y1, `SLICE_X48Y0:SLICE_X95Y119`, 42,824 LUT, 144 BRAM tiles, no BUFG/BSCAN/IOB) in the guide JSON; (d) the KIT-INTERACTIVE command variants (GUI, open session, `STOP_AFTER=link`) in `POST /guide/script`'s answer; (e) `POST /kits/design/scan {path}`: an RTL folder or `.f` list → sources in compile order (packages first, `+incdir+`, `+define+`), top, and a written `<name>.json` with the proposed rm_id and the generics | `SVC/kit/guide.py`, `SVC/kit/build.py`, `SVC/kit/templates/build_rm.tcl.template`, `SVC/xdc/kits.py`, `D/kit_api.py` | 15 ((a)-(d): 9) | (a)-(d) yes; (e) later |
| **G9** Activity kept 30 days, the CLI too | the log is the page's memory (`JS/store.js:14,262-290`); refusals are never published (`D/app.py:737-749`) | `SVC/activity.py`: one JSONL file per day in `<state>/activity/` (O_APPEND, one row per line); a bus subscriber for a topic whitelist (`job.*`, `deploy.*`, `update.*`, `lease.state`, `lease.taken`, `power.cycle`, `controller.reboot`, `board.net_identity`, `kit.stored`) folding a failed job into one row; the error handler logs refused non-GET calls (client from an `X-HM-Client` header); prune after 30 days; `GET /activity?board=&level=&since=&limit=` and `POST /activity` for the page's own rows; the in-process CLI engine attaches the same writer | new `SVC/activity.py`, `D/app.py`, `cli/engine.py` | 11 | later (the drawer ships on the page log) |
| **G10** Identity clash across boards | computed only when that board is opened (`SVC/board_identity.py:474-520,603-663`) | `GET /identity/clashes` = pairwise over `SeenIdentities.all()` + cached hub records with the existing rules (`same`, `label_is_default`) → `[{field, value, boards}]`; no board contact | `D/identity_api.py`, `SVC/board_identity.py` | 3 | yes |
| **G11** The full lease queue | `GET /boards/{bid}/lease` already returns `queue: [{position, holder, user, mine}]` (`SVC/lease.py:1355-1359`), but the REST client drops `tier` and `background_queue` (`transports/hub_rest.py:972-979`) and a request note has no "how long" | add `tier` and `background_queue` to `QueueEntry` / `LeaseStatus` (both transports); add `want_s` to the request note (`LEASE_REQUESTS.md`, readers ignore unknown keys). Messages reach other people only over the SSH transport; over REST the hub has no notes (degraded mode, `docs/HUB_MODE.md:207-232`): the proposed fpgahub feature lifts that and is not HM's work | `transports/hub_rest.py`, `P/hub.py`, `SVC/lease.py`, `docs/LEASE_REQUESTS.md` | 3 | yes |
| **G12** "Open a board on" setting | no row | `general.open_on` (`workbench` \| `overview`) in `src/harness_manager/settings/rows.py` | `settings/rows.py` | 0.5 | yes |
| | | | **Total** | **about 84 h** (+ about 3 h of shared API.md/ENDPOINTS merging) | must-haves about 50 h |

**Answers to the specific questions**

- **Consoles HM can reach today:** `uart0`/`uart1`/`swo` over the shell's Ethernet (TCP 6930-6932, `P/constants.py:29`);
  `fpga_uart0..3` over a local Debug USB (`P/usb.py:387-419`, interface 00 = the MCC is left out on purpose);
  hub shares as `hub://` links relayed to TCP (`P/hub.py:305-326`). `start_shares` is false by default: HM uses a
  share someone started and never starts one (`P/hub.py:18-20`). The fpgahub web console can bridge any running share.
- **Is a read-only MCC console safe under "one tty_00 reader, never an fpgahub share on tty_00"? Not as a live
  stream.** A tail is a second reader: on a hub board it makes every paced REBOOT and every TEMP/OSC read refuse
  (the one-reader scan returns rc 3, `P/hub_mcc.py:461-479`); on a PC board it makes the per-operation open fail
  with EBUSY (`P/mcc.py:692-712`). HM cannot become the single owner on a hub board, because REBOOT runs as
  pyverify's script **on the hub**. So G1a serves the transcripts HM already captures, labelled "MCC (read-only)
  · last read hh:mm". Also: the 30 s telemetry poll already opens tty_00 twice per read (TEMP, then 6 × OSC, each
  with a 3.5 s listen floor, `P/hub_mcc.py:96-100,761-800`); read OSC once per session, as fpgahub does.
- **Debug USB route:** no `boards.toml` field and no hub target field today; G2 derives it from the controller HM
  builds, and `self` from the harness features `mccif`/`mcc_local` (`P/harness_mcc.py:1-63`, only `status` is
  implemented until net-protocol v0.18). A declared `debug_usb` key is optional.
- **Overview readings:** bare metal and Linux both answer `diag` (`svc_max_us`, `svc_max_ix`, `svc_overruns`,
  `svc_skips`, `icap_bytes`, `rx_drops`, `tx_frames_sent`, `tx_errors`…); Linux omits the keys it cannot fill, so
  harnessd has no `svc_*`. `stats` (`up_ms`, `swap_n`, `icap`, `rxdrop`, `txerr`, `link`, `spd`) is read in pieces
  and never served. The answer time is not measured server-side (only the page's `timed()`,
  `JS/store.js:314-327`); G4 adds `answer_ms`.
- **Import:** the daemon runs where the browser runs by default (loopback, `D/state.py:54-58`), but browsers never
  reveal a file's path, so "choose a zip" needs the upload route; "a path on this machine" does not.
- **Recover "Back to greybox":** it is `POST /boards/{bid}/restore` (restore baseline): MOVE, no backend work.
- **Build stepper:** `SVC/kit/guide.py` computes `target, tools, kit, wrapper, build, check` with states
  `done|next|blocked|failed|unchecked`. Mapping: Setup = target + tools + kit; Design = wrapper; Build = build (+
  G8 `running`); Check = check's checks; Add = check `done` + imported. The page polls every 10-15 s while
  `running` is set (today it refreshes only on a button, `SEC/build.js:322-324`).

**Separate item for david (not a lane):** HM's XDC checker accepts `create_pblock`, `add_cells_to_pblock` and
`resize_pblock` (`SVC/xdc/syntax.py:19-24`), but a nested pblock inside `pblock_rp_dut` has never been built. One
test build on srv03335 (about 1 h of Vivado, no board) should prove it links, passes `pr_verify` and routes,
ideally with KIT-INTERACTIVE's `STOP_AFTER=link`. Until it passes, the Constraints and floorplan panel shows the
partition's facts and takes an RM_XDC, and does not offer "add a pblock".

---

## 3. Test impact

**How tests pick a tab today:** each tab is `button[role=tab].section-tab[data-section=KEY]` and the open panel is
`div[role=tabpanel][data-testid="section-KEY"]` (`JS/app.js:233-242,465-467`). Every test clicks
`[data-section="KEY"]` and waits for `section-KEY`. No test uses a tab label or a URL. There is **no shared
helper**: about 15 files each define `open_board` / `section`, and 9 of them are imported by other files.
**Nothing compares pixels**: the 13 `*_screenshots.py` files only save PNGs (gitignored); the committed copies in
`docs/review/2026-09-2*` are curated by hand.

### 3.1 The classes (64 entries, 14,623 lines)

| Class | Entries | Hours | Files |
|---|---|---|---|
| **Survive** | 26 | 1.5 (contingency) | `conftest.py`, `__init__.py`, `hil_gui_auto_cases.json`; mocks and contracts: `test_hcat_mock`, `test_lm3_mock_display`, `test_lrd_mock`, `test_otad_mock_update`, `test_p1_mock_panel`, `test_settings_mock`, `test_t10_mock_xdc`, `test_x3_mock_xvc`, `test_t14_mock_contract`; static: `test_fixpack4_static`, `test_t14_static`, `test_lm4_display_static` (at risk: app.css block markers); Settings and sidebar: `test_setui_browser`, `test_setui_merge_browser`, `test_setui_screenshots`, `test_sidebar_browser`, `test_sidebar_screenshots`, `test_review5_sections`, `test_debugocd_browser`; real daemon/process: `test_updui_real_daemon`, `test_t14_real_process` (at risk: header), `test_n1_names_browser` (at risk: header), `test_quiet_poll_browser` (at risk: the Background chip) |
| **Small edit** | 29 | 33 | navigation or a few moved selectors: `test_board_identity_browser`, `test_hil_gui_browser`, `test_hil_gui_screenshots`, `test_kit_ui_build_browser`, `test_kit_ui_build_screenshots`, `test_kit_ui_build_static`, `test_l1_card_browser`, `test_l3_week_plan` (878 lines; about 10 of 39 tests need rewriting), `test_lease_fresh_browser`, `test_lm4_display_browser` (+ 1 rewritten test), `test_lm4_display_demo`, `test_lm4_display_screenshots`, `test_locate_browser`, `test_lrd_browser`, `test_lrd_screenshots`, `test_panel_v017_browser`, `test_paneltruth_browser`, `test_paneltruth_screenshots`, `test_slot_timing_browser`, `test_small4_claim_hostkey_browser`, `test_t10_xdc_browser`, `test_t10_xdc_static`, `test_t14_browser` (about 5 tests rewritten), `test_t14_harness_states`, `test_updui_browser`, `test_updui_screenshots`, `test_xvc_card_browser`, `test_xvc_card_real_daemon`, `test_xvc_screenshots` |
| **Rewrite** | 9 | 28 | layouts that are replaced: `test_demo_all_browser` (Overview tiles; hosts the shared `open_board`/`section`), `test_demo_all_screenshots`, `test_fixpack1_quiet_cards_browser` (tiles, Details), `test_fixpack4_browser` (tab count 12, hidden tabs, Activity tab), `test_l3_review_screenshots`, `test_lease_ui_browser` (the badge-only-when-open rule, `tile-lease`), `test_lease_ui_screenshots`, `test_p3_panel_screenshots`, `test_p3_panel_ui` (tile panel line, Details; hosts helpers for 4 files) |
| Shared `tests/web/nav.py` + re-curating `docs/review` images | | 5 | |
| Outside `tests/web` | | 3 | `tests/integration/test_lxslots_api.py:85-99` greps `SEC/overview.js` for `tile-card` / `CardRow` (**breaks**); `tests/integration/test_fixpack2_service_env.py:196-204` (`env-banner` in app.js; at risk); `tests/unit/test_review5_consistency.py:224-238` (a sentence in `SEC/harness.js`); `tests/unit/test_fixpack3_kit.py:146-156` ("about 30 minutes on a quiet machine" in `SEC/build.js`: keep the sentence); `tests/unit/test_p4_tokens.py` (text anchors in app.css such as `--rail-w: 272px` and the index.html link order) |
| **Total** | 64 | **about 70 h** measured file by file; **about 60 h** when `nav.py` lands first (it fixes most small edits by itself) | |

CI fails the run when **any** browser test is skipped (`.github/workflows/ci.yml:88-91`), and CI is billing-blocked,
so every merge runs the web tests locally.

### 3.2 Keep these test ids (they are the "board is open" waits)

- `data-testid="fact-shell"` (22 references in 17 files): the new header keeps the Shell fact and its id.
- `lease-chip` (46 references in 11 files) and `fact-hub` (17 in 5 files): the new lease fact keeps both ids.
- `data-section` / `section-KEY` on the new five tabs (with the new keys), so `nav.py` is a key map, not a rewrite.

### 3.3 Old tab keys and deep links that must keep working

**Today there is no URL routing.** The only URL form is `#token=…` (built by `D/state.py:77-79`, read and stripped
by `JS/api.js:172-187`). The selected tab lives in sessionStorage `harness_manager.sections` (a board → key map,
`JS/store.js:97-101,161-169`), the selected board in `harness_manager.selected`, the Details fold in
`harness-manager.details`, the last Settings pane in `harness_manager.settings.section`.

**New routes (Phase 1, `JS/route.js`):** `#/<board>/<tab>[/<sub>][?console=…&activity=err]`, the board id
URL-encoded (ids hold `@` and `:`). `#token=` is read first, as today, then the route. The router writes the route
with `history.replaceState` on every tab change and keeps the sessionStorage map, so a reload without a hash still
lands where you were. Keys: `overview`, `workbench`, `build[/setup|design|build|check|add]`,
`board[/recover|versions|connections|readings|access|about]`, `checks`.

| Old key (sessionStorage, `setSection`, tests) | New route | Note |
|---|---|---|
| `overview` | `overview` | |
| `xdc` | `build/design` | the RM kit is in Design; full-board XDC under More exports |
| `build` | `build` | lands on the current step |
| `program` | `workbench` | the picker has focus |
| `consoles` | `workbench?console=<name>` | the switcher selects that console |
| `debug` | `workbench` | the rail's Debug card; XVC is Logic analysers |
| `power` | `board/recover` | |
| `clocks` | `board/readings` | DUT clock presets and oscillators |
| `sd` | `board/versions` | Configuration SD card + the interrupted-install recovery (the banner links here) |
| `update` | `board/versions` | |
| `checks` | `checks` on a hub board; `overview` on a desk board | the tab is hidden without a hub |
| `activity` | `overview` + the Activity drawer open (this board) | |
| Details fold (`harness-manager.details`) | `board/about` (identity, capabilities), `board/readings` (telemetry, counters), `overview` (Front panel) | the key is ignored and removed |
| `openSettings("updates")` (`SEC/update.js:181`) | unchanged | Settings keeps its pane keys; it opens on General unless a key is given |

**Calls in the code that navigate by old key** (each lane replaces its own; `setSection` maps old keys until then):
`JS/app.js:384` `sd`; `JS/store.js:689` `sd` (auto-select on an interrupted SD install); `SEC/overview.js:48`
`sd`, `:61,66` `power`, `:72,76` `update`, `:159` `program`, `:178,183` `consoles`, `:224` `debug`;
`SEC/build.js:333,662` `program`, `:519` `xdc`.

**Words that name old tabs outside the JS** (the DOCS lane): `SVC/kit/guide.py:148,150` ("the XDC section"),
`src/harness_manager/checks/plans.py:518`, `src/harness_manager/checks/run.py:1198`,
`SVC/hil_runs.py:603`, about 50 mentions in `docs/USER_GUIDE.md` (for example "Power > Board reboot" in §12.3 and
"Power > Restart the shell" in §14.2), and `cli/helptext.py` where it points at the app.

---

## 4. Lane plan

Integration branch **`feat/ui-v2`**, cut from `main` at the v0.1.0 tag. Lanes branch from it as `team/ui2-<lane>`,
work in their own worktrees, never commit to `main`, never `git add -A`, and run `make check` plus the web tests
(with Playwright; a skip fails) before they hand back. The integrator (the lead) merges, in the order below.

### 4.1 Phase 1: the shell (one lane, `team/ui2-shell`, about 35 h, 1-2 days)

The rule for Phase 1: **every commit is shippable**. The five tabs appear at once, and until Phase 2 replaces them
their bodies are today's sections, composed: Workbench = Program + Consoles + Debug stacked; Board = the six
sub-pages rendering today's Power, Clocks, SD card, Update and Details cards; Build = Build with XDC as a fold;
Checks hidden on desk boards. If Phase 2 slips, `feat/ui-v2` after Phase 1 is still a release.

| Step | What | Hours |
|---|---|---|
| 1 | `JS/route.js` + tabs 12 → 5 (`SECTIONS`), the old-key map of 3.3 inside `setSection`, the sessionStorage migration, `#/<board>/<tab>/<sub>` routes, `data-section` / `section-KEY` ids on the new tabs | 7 |
| 2 | Extension points, so no Phase 2 lane edits `app.js` or `store.js`: `openModal(kind, props)` / `registerModal` (`JS/modal.js`), `navigate(bid, route)`, `openActivity(bid, filter)`, `toast(text)`, `boardState(bid).deploy` for the mini bars; empty `css/overview.css`, `workbench.css`, `build.css`, `board.css` linked from `index.html`; stub `SEC/workbench.js` and `SEC/board.js` composing today's sections | 3 |
| 3 | Activity **drawer** from the rail foot (this board / all, All / Errors, each row links to its route), the header's last-error chip, toasts; the log stays the page's until G9 | 4 |
| 4 | Sidebar cards: outline, selected edge, label, kind, one group per hub + "This network" (stars sort first inside a group), clash mark slot, lease badge on every hub board (from `lease_known` until G3), USB tag (from the link kinds until G2), the mini download bar | 6 |
| 5 | Header: Shell (id `fact-shell` kept), Design + "verified hh:mm" + the mini bar, Harness, Debug USB, Hub lease (ids `fact-hub`, `lease-chip` kept); Build check → the Design chip's tooltip; Background chip only while paused | 3 |
| 6 | Tokens: first pin the panel palette (risk R2), then the round-2 dark values, `--rc-*`, the 7 icons; `make tokens`; fix `tests/unit/test_p4_tokens.py` anchors | 4 |
| 7 | Tests: `tests/web/nav.py` (the map in 3.3), point the 9 shared helpers at it, fix the tab count / order / list tests (`test_fixpack4_browser.py:74`, `test_kit_ui_build_browser.py:120-122`, `test_kit_ui_build_static.py:32-38`, `test_t14_browser.py:25`), move the Checks tests onto hub boards | 8 |

### 4.2 Phase 2: parallel lanes (about 220 h for v0.2.0, 3-4 days)

**File ownership is strict.** A file has one owner per phase. Anything outside your files goes to the integrator as
a CCR message, never as an edit.

| File or area | Phase 1 owner | Phase 2 owner |
|---|---|---|
| `JS/app.js`, `JS/store.js`, `JS/route.js`, `JS/modal.js`, `index.html`, `css/app.css`, `JS/ui.js`, `JS/format.js`, `JS/actions.js`, `JS/week.js`, `JS/locate.js`, `JS/viewer.js`, `JS/prefs.js` | SHELL | **integrator only** (CCR) |
| `JS/api.js` (`ENDPOINTS`) and `docs/API.md` | SHELL (no new names) | **API lanes only**; UI lanes use the names they publish |
| `design/tokens.json`, `tools/gen_tokens.py`, the generated files, `vendor/lucide/icons.js` | SHELL | frozen (CCR) |
| `css/<tab>.css` | SHELL creates them empty | that tab's lane |
| `JS/sidebar.js`, `JS/hub.js`, `JS/lease.js`, `JS/settings/*`, `JS/selfupdate.js`, new `JS/add.js` | SHELL | **SHELL-2** |
| `SEC/overview.js`, `SEC/details.js` (deleted at the end), `SEC/panel.js`, `JS/display.js`, `SEC/checks.js` | — | **OVERVIEW** |
| `SEC/workbench.js`, `SEC/program.js`, `SEC/consoles.js`, `JS/consoles.js`, `SEC/debug.js`, `SEC/xvc.js` | SHELL (stub) | **WORKBENCH** |
| `SEC/build.js`, `SEC/xdc.js`, new `JS/import.js` (the Import dialog) | — | **BUILD** |
| `SEC/board.js`, `SEC/power.js`, `SEC/clocks.js`, `SEC/sd.js`, `SEC/update.js`, `SEC/harness.js`, `SEC/identity.js`, `SEC/claim.js` | SHELL (stub) | **BOARD** |
| `src/harness_manager/daemon/**`, `services/**`, `transports/**`, `harness_manager_mps3/**`, `demo*.py`, `tests/fakes/t14_mock_api.py`, `tests/integration/**`, `settings/rows.py` | — | **API-HUB** / **API-BUILD** (split below) |
| `tests/web/nav.py` | SHELL | integrator (CCR) |
| `tests/web/test_*.py` | SHELL (step 7) | the lane whose tab the file drives (3.1) |
| `docs/USER_GUIDE.md`, `CHANGELOG.md`, `cli/helptext.py`, the Python hint strings, `docs/review/` | — | **DOCS** |

| Lane | Scope | Backend it uses | UI h | Test h | Total |
|---|---|---|---|---|---|
| **API-HUB** | G1b, G2, G3, G7, G10, G11, G12 (G1a later); `SVC/lease.py`, `P/hub.py`, `transports/hub_rest.py`, `D/hub_api.py`, `D/hubs_api.py`, `D/identity_api.py`, `SVC/console.py`, `D/consoles_api.py`, `D/power_api.py` | — | — | incl. | 30 (+ 4.5 later) |
| **API-BUILD** | G4, G5, G6, G8, G9 (the "later" parts after v0.2.0); `SVC/kit/**`, `D/kit_api.py`, `D/card_api.py`, `SVC/slots.py`, `SVC/update/planner.py`, `SVC/history.py`, `SVC/activity.py`. Shares `D/app.py` with API-HUB: API-HUB owns it; API-BUILD sends CCRs or adds routes in its own `*_api.py` | — | — | incl. | 23 (+ 29 later) |
| **WORKBENCH** | Program strip (combo, filter, tags, inline preflight, **download bar** with rate/ETA from the events' `at`), console switcher (+ Harness console, + MCC transcript when G1a lands), Reset DUT in the toolbar, rail = Debug + Logic analysers, "Program nanosoc_ila…", fold below 1280 px | G1b | 22 | 8 | 30 |
| **BOARD** | sub-pages Recover (ladder, keeps matrix, card-busy guard), Versions (one list, slot tiles, plans with steps and ETA, roll back, netboot "Ask for it…", Configuration SD), Connections (diagram, Ethernet, Debug USB, SSH, JTAG), Readings, Access (claim, identity, who may drive), About | G2, G6, G7 | 26 | 10 | 36 |
| **BUILD** | stepper, Ready line, Design (Example / My RTL / Paste, generics, **Constraints and floorplan**), Build (**Run it your way**, watching, stage + elapsed), Check (groups, **utilisation vs the pblock**, the one failed gate), Add → Workbench, the Import dialog | G5, G8 (+ KIT-INTERACTIVE) | 24 | 4 | 28 |
| **OVERVIEW** (+ Checks) | Needs attention, the **Front panel as the largest element** (Live / Text, Identify, watchers; `SEC/panel.js` and `JS/display.js` move here), the identity strip, the KPI row (smaller), Design, Lease (full queue) / Access, Consoles and debug, Recent activity; Checks: hub-only, restyle | G3, G4, G10, G11 | 20 | 14 | 34 |
| **SHELL-2** | preview (Open and take the lease, Request without opening), Close dialog (restore, release, close by default), Request (how long), Add a board (one dialog), Settings (opens on General, `open_on`, hide unread rows), Help by page | G3, G11, G12 | 14 | 8 | 22 |
| **DOCS** | USER_GUIDE (§2, 4-10, 12, 13, 14.2), CHANGELOG 0.2.0, Help texts, Python hint strings, `docs/review` images, the guide handoff pack (R1) | — | — | — | 10 |
| Integration and gates | merges, `make check` + web tests per merge, one board window | — | — | — | 8 |

**Merge order into `feat/ui-v2`:**

1. **Phase 1 SHELL.**
2. **API drop A** from both API lanes (day 1 of Phase 2): API.md rows, `ENDPOINTS` names, and `DemoEngine` +
   `MockDaemon` answers for every must-have route, so UI lanes code and test against the shapes from the start.
3. **WORKBENCH** (the most-used page), then **BOARD**, then **SHELL-2**, then **BUILD**. Their files do not
   overlap, so the order only sets who rebases.
4. **API drop B** (the real services behind the same shapes) at any point after drop A.
5. **OVERVIEW** last among the UI lanes: it reads the most from the others and from G3/G4.
6. **DOCS**, then the gate on `feat/ui-v2`, then a board window.

### 4.3 Into `main` as v0.2.0

| When | What |
|---|---|
| Tue 6 Oct | v0.1.0 ships from `main`. Nothing from this plan is in it. |
| Wed 7 Oct | cut `feat/ui-v2` from the v0.1.0 tag; Phase 1 starts |
| Thu 8 Oct | platform guide 1.0 (shows v0.1.0, as it should); Phase 1 merges late Thu or Fri |
| Fri 9 - Tue 13 Oct | Phase 2: 5 UI lanes + 2 API lanes + DOCS in parallel |
| Wed 14 Oct | integration, full gate, one board window on B2 (about 30 min: programming bar, consoles incl. tty_02, lease queue, OS slots read, Recover rungs 1-3) |
| Thu 15 Oct | merge `feat/ui-v2` → `main`, tag **v0.2.0**; the "later" backend parts (about 34 h) go to v0.2.1 |

This lands a week before the mint-4 freeze (21 Oct), so the mint's board windows and v0.2.0 do not compete.
Fix-packs that land on `main` after the cut are forward-ported by the integrator (about 1-2 h each).

**If david wants it sooner:**

- **Start now, merge after v0.1.0** (recommended if agents are free): Phase 1 and API drop A start Thu 1 Oct on
  `feat/ui-v2` cut from `main` once FIX-PACK-4 merges. v0.2.0 then lands about **Mon 12 Oct**. The cost is
  forward-porting every v0.1.0 fix-pack that touches `web/static/js` into `feat/ui-v2`, and agent time taken from
  v0.1.0 hardening and the guide.
- **Put it in v0.1.0:** not recommended. About 290 h does not fit in six days next to the MVP; the Wed agent clean
  run on B2 and guide 1.0's four figures and §7.5 table would all describe a UI that changed two days earlier.

### 4.4 Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **The guide's screenshots.** Guide 1.0 (Thu 8 Oct) shows today's UI: four figures in the guide worktree (`docs/platform-guide/img/hm_overview.png`, `hm_xvc.png`, `hm_build.png`, `hm_lease_request.png`), the 12-section table in §7.5 (`src/23_hm.html:112-127`) and words like "Power > Board reboot" | v0.2.0 after Thu 8 Oct, and guide 1.0 names "Harness Manager 0.1.0". The DOCS lane hands the guide lead a pack for guide 1.1: four new screenshots at the same sizes from the `*_screenshots.py` tests, the new section table (5 tabs + the Board sub-pages), and a list of changed words; about 3 h of the guide lead's time |
| R2 | **The panel palette is generated from the web's dark tokens.** `tokens.json` `panel.theme = "dark"`, so `clcd_palette.h` (vendored by the Linux harness), `palette.json` and `panel_codes.py/js` (the text mirror's `ROLE_COLOURS`) change with the web's dark theme, and the header carries the sha256 of the whole `tokens.json` | Phase 1 step 6 first adds a frozen `panel.palette` block (today's dark values; the prototype's `--lcd-*` values are exactly these), resolves the panel roles against it, and hashes only the panel's inputs, so the vendored header stays byte-identical; then changes the web's dark values. `make tokens-check` proves it |
| R3 | **The lease is enforced in the page only** for Program, Restore, Reset, Reboot, Restart shell, Power-cycle, Clock and Debug. The new UI moves Reset into the console toolbar and the rungs into Board: a button that forgets the `holder` gate lets a watcher drive the board | every drive button goes through `gateReason` (`JS/actions.js:42-75`) with `holder`; each lane adds a test that clicks its drive buttons on a board held by someone else; G7 adds the daemon-side 409 |
| R4 | The "board is open" waits: `fact-shell` (17 files), `lease-chip` (11), `fact-hub` (5) | keep the ids (3.2) |
| R5 | CI is billing-blocked and fails on any skipped browser test | every merge runs `make check` + the web tests locally (Playwright + `/usr/bin/google-chrome`) |
| R6 | A live MCC console would be a second tty_00 reader | G1a is a transcript; no stream, no share on tty_00 |
| R7 | The prototype keeps moving (round 3 is being built now) | freeze the design at round 3 before Phase 2 starts; later rounds go to v0.2.x |
| R8 | Demo and mock parity: a route without a `DemoEngine` and `MockDaemon` answer cannot be browser-tested | API drop A ships the shapes in both before any UI lane needs them |
| R9 | Board time: the OS-slot install takes 41-50 min per slot and HM has never written a slot on silicon | v0.2.0 proves the slot **read**, the plan and roll back only; a real install stays a separate, scheduled test |
| R10 | Two moving lanes outside this plan: KIT-INTERACTIVE (`kit build --gui/--stop-after`) and the nested-pblock build | B12a and the floorplan panel plan on the CLI's output; if it slips, Build ships Batch only and the panel without "add a pblock" |
| R11 | `feat/ui-v2` lives about a week beside `main` | the integrator forward-ports fix-packs daily; lanes rebase before their merge |

---

## 5. Questions for david

Each has a default; silence means the default.

1. **Start before v0.1.0 ships?** Default: **no for the UI**. Phase 1 starts Wed 7 Oct from the v0.1.0 tag and
   v0.2.0 lands about Thu 15 Oct. The two API lanes may start their drop A now, because it touches no page.
   (Starting everything now moves v0.2.0 to about Mon 12 Oct, at the cost of forward-porting every v0.1.0 fix-pack.)
2. **The MCC console.** A live tty_00 console breaks the one-reader rule, so the plan offers a read-only
   **transcript** of what HM already reads (G1a). Default: the transcript in **v0.2.1**; no live MCC console.
3. **Enforce the hub lease in the daemon** (G7) for Program, Restore, Reset, Reboot, Restart shell, Power-cycle,
   Clock and Debug, so the CLI and other pages get 409 HELD too? Default: **yes, in v0.2.0**, keeping today's
   force/consent escape.
4. **The nested-pblock test build** (about 1 h of Vivado on srv03335, no board), in the KIT-INTERACTIVE lane with
   `STOP_AFTER=link`. Default: **yes**; until it passes, the Constraints and floorplan panel shows the partition's
   pblock and takes an RM_XDC, with no "add a pblock".
5. **Activity kept 30 days, with the CLI writing to it too** (G9, about 11 h). Default: **v0.2.1**; v0.2.0's drawer
   shows this page's log, as today, and says "since this page opened".
