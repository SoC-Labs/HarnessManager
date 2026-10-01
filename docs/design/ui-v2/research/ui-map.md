# UI-MAP: what Harness Manager shows today, and where (2026-09-30)

Source: `/home/dam1n19/SoCLabs/hm-ui-review` @ 1a127de, `src/harness_manager/web/static/js/…`. Demo: the real
`harness-manager-daemon` app over `DemoEngine(showcase=True)`, private state, headless system Chrome, light, 1440x900.
Full inventory: `inventory.md` / `inventory.csv` (141 rows). Screenshots: `shots/` (90 PNGs). Drivers: `drive*.py`.
(Saved by the lead from the lane's hand-back; the lane could not write .md reports.)

## Summary

1. 1 rail + 12 board tabs + 1 hidden drawer (Overview > Details, 5 cards) + 8 modal dialogs + 9 Settings sections + 13 banner types = 141 places.
2. **Lease: 11 places, 4 vocabularies.** Free = "Free" (neutral) in the rail, "no lease" (amber) in the header, "Not leased… Fix: acquire" in Needs attention. Yours = two Release lease buttons on one screen (header + Board tile).
3. **Two lease rules in code.** Rail/header/Checks use `lease.here`; the XVC gate (`sections/xvc.js:157-166`) and Update lease line (`sections/harness.js:339-344`) use `lease.mine` (another of your sessions counts as yours there).
4. **Identity (shell/design/harness/build) repeated 6-12 times** (rail, header subtitle, header facts, Design tile, Details>Identity, Build, XDC, Update, Program, preview); casing drift `0x72bb0a36` vs `0x72BB0A36`.
5. **"Harness version" means two things that can disagree**: header 1.0.0 (version verb) vs Update>Harness versions "Running 1.1.0" (release catalogue).
6. **Overview Board tile is a catch-all**: temperature, DUT clock, Card, Panel+Identify, SSH claim, Identity, Hub lease, Reset DUT, Reboot.
7. **Live LCD buried**: Overview > Details (folded) > Front panel, ~1000 px down; no Panel tab; Identify = 3 controls, 2 states.
8. **Update tab: 2 install flows, 2 different "Roll back"s**; "re-key" marks different rows in its two tables; the app card only points to Settings.
9. **XDC and Build overlap**: same design catalogue, both paste-JSON/viewers/zips, separate state; Build step 4 "Wrapper and XDC" links back to XDC.
10. **Near-empty tabs**: Clocks (2 cards; demo "Cannot"); SD card on Linux/Ethernet boards (all 4 actions disabled); Power on Linux (2/4 usable, both also on Overview); Checks on desk boards (whole form disabled); Update before Refresh.
11. **"SD card" tab = config SD; "Card" elsewhere = the Linux user microSD.** Header refresh re-reads neither the Card line nor the SD journal.
12. **4 Settings rows stored but read by nothing** (`panel.identify_s` says 10 s, Identify blinks 5 s; `consoles.line_ending` vs the Send select; `debug.hw_server_mode` vs XVC BYO box; `panel.presence_who`). Settings > Debug shows 1 row.
13. **History split 4 ways**: Activity (this page's session only), Checks > Past runs, Update > History, Program > Progress.
14. **"Add a board" in 3 places** (rail +, Settings > Boards, Settings > Hubs > Add this board), different persistence. Theme in 2 places.
15. Proposed single homes: header = identity + lease; Consoles/Debug/Program/Power/Update own their actions; Overview = a summary that links, not repeats.

## Inventory (condensed)

| Area | Rows | What is there | Gates |
|---|---|---|---|
| Rail | 20 | brand; + Add by address; Scan + boards.toml offer; Favourites groups; per card: health dot, name, job spinner, Open chip (board lock), held-by chip, lease badge, pack·design·shell, links/route, star, Identify, drag; foot: Theme, service line, Settings (+update dot), Help | lease badge only for an open hub board; Identify only with feature `locate` |
| Workspace states | 4 | Connecting; No boards (Scan); Select a board; Board preview (not open) | |
| Banners | 13 | session expired; daemon down; service-env warning; last read failed; interrupted SD; restart needed; app update (staged/available/applying/outcome); lease taken; holder prompt; answered; request bar; checks running | lease banners for every open hub board on every tab (`lease.js:919-931`) |
| Header | 15 | name, subtitle, id; job chip; refresh; Open chip; Close board; facts Shell, Design, Harness, Build, Health, Hub (tunnel, lease chip, Acquire/Release/Request/Cancel), Background; 12 tabs + badges | Hub only behind a hub; Background only when paused |
| Overview | 16 | held-back/failed read cards; Needs attention (sd, read, harness, build, tunnel, lease); tiles Design, Consoles, Debug (+XVC), Board; Details toggle | Board tile: Temperature, DUT clock, Card, Panel+Identify, SSH, Identity, Hub lease, quiet note, Reset DUT, Reboot |
| Details (folded) | 5 | Identity; Telemetry; Front panel (Live display, mirror, owner/page/banner/card/touch/sessions/taps, Identify 5-30 s); Health (+counters); Capabilities | Live display = Linux + lcd_mirror + lease |
| XDC | 3 | Pin model; Export (RM kit / Full board, design or paste JSON, Preview, zip, checks); Files | |
| Build | 9 | Head; steps Target, Tools, Kit, Wrapper and XDC, Build, Check and add; Script files; When it goes wrong | head-only for ~2 s first visit |
| Program | 5 | not-available line; Overlays (+preflight on select); Preflight; Program (Keep on the card, Arm, Program, Restore baseline); Progress | keep = Linux + card |
| Consoles | 7 | sub-tab per console; bar (state, Save, Clear, Reconnect); baud; screen; Export to TCP; terminal; send line + ending | |
| Debug | 3 | OpenOCD (Detect, Open, Close); Connection (IDCODE, ports, gdb); Fabric debug XVC (BYO, Open/Close, URL, .ltx, MIG .ltx, Tcl) | XVC behind a hub: lease holder |
| Power | 4 | supply (readings, power-cycle); Board reboot; DUT reset; Restart the shell | capability-gated |
| Clocks | 2 | DUT clock presets; Board oscillators | |
| SD card | 2 | interrupted-install recovery; 4-step install | storage_* (Debug USB) |
| Update | 6 | Harness versions (list, Pin, Install…, History, Roll back) + inline panels; channel release (Check) + Plan (Install, Roll back); The app (pointer) | empty until Refresh |
| Checks | 5 | Start form / live run; last run; Announcement; Past runs + REPORT viewer | needs a hub lease held here |
| Activity | 1 | log, filters, this board / all | session only |
| Dialogs | 19 | Help (19 CLI topics); Settings (9); Request board; Force release; Release lease; Close board + release?; Restart to update?; restarting overlay; Fix identity; Claim confirm | |
| Dead code | 2 | `sections/placeholders.js` never imported; `AppVersionChip` (`selfupdate.js:695-700`) never rendered | |

## Duplication matrix (same = one source; words = same state, different wording; can disagree = different sources/rules)

| # | Fact / action | Where | Agree? | Proposed single home |
|---|---|---|---|---|
| 1 | Hub lease state | rail badge; header Hub chip; Board tile row; Needs attention; Background chip; request bar; Checks line; Update line; XVC gate; Live display refusal; Release/Close dialogs | words; XVC + Update use `lease.mine` (can disagree) | header Hub fact; rail uses the same words; drop tile row + attention item; one `here` predicate |
| 2 | Lease actions | header; Board tile (Release); attention; request bar | same | header only; request bar while active |
| 3 | Shell, design, rm_id | rail; header subtitle; header facts; Design tile; Details Identity; preview; Build; XDC (both casings); Program; XVC; Update | same, casing differs | header facts; Details keeps only extras |
| 4 | Harness version | header; Details; Update Running; Plan title | can disagree (1.0.0 vs 1.1.0) | Update tab; header shows the release or labels "firmware" |
| 5 | Build check | header "Build" chip; attention; Details; preview | same; "Build" also names the DUT Build tab | header chip renamed ("Firmware check" / "Fabric match") |
| 6 | Health | rail dot; header chip; attention; Details Health; stale banner + attention read + card | one failed read worded 3 ways | header chip → Health details |
| 7 | Identify / locate | rail icon (5 s); Board tile Panel row; Details Front panel IdentifyControl (5-30 s); Settings panel.identify_s = 10 (unused) | can disagree (2 impls, 2 states) | rail icon + one Identify in a Panel home |
| 8 | Front panel / LCD | Board tile Panel line; Details Front panel; Settings panel.presence_who (unused) | same | a Panel home (tab or first-class card), Live display first |
| 9 | User microSD (Card) | Board tile Card (GET /card); Program keep; Checks text; Front panel Card + mirror USD | can disagree (2 sources; header refresh skips it) | one Card/storage home with OS slots |
| 10 | Config SD vs user card | "SD card" tab vs "Card" | one word, two things | rename "Config SD", fold under Power or Update |
| 11 | SD install interrupted | banner; attention; tab "!"; recovery card | 2 button labels | banner + recovery card only |
| 12 | Consoles | Overview tile; Consoles tab; app-update confirm; Settings consoles.* (unused) | tile vs tab dots differ | Consoles tab; tile = one line + Open |
| 13 | OpenOCD debug | Debug tile; Debug tab | same | Debug tab; tile keeps state + gdb port |
| 14 | XVC | Debug tile line; Debug tab card; Settings debug.hw_server_mode (unused) | setting unused | Debug tab; wire or drop the setting |
| 15 | Reset DUT / Reboot | Board tile; Power; SD step 3; attention → Power | same | Power; tile keeps Reset DUT at most |
| 16 | DUT clock / temperature | Board tile; Details Telemetry; Clocks tab | can disagree (2 sources) | merge Clocks into Power & clocks; tile keeps temperature |
| 17 | Programming state | Design tile; Overlays Loaded now; Progress | same | Program |
| 18 | Harness install / rollback | Harness versions Install…/Roll back; channel Plan Install/Roll back | 2 flows, 2 meanings of Roll back | Harness versions only |
| 19 | App version / update | rail service line; Settings dot; banner; Settings > Updates; Update "The app" | same | banner + Settings > Updates |
| 20 | History / activity | Activity; Checks Past runs; Update History; Program Progress; Settings Last update | different logs | Activity = board history; others link |
| 21 | Design picker / XDC | XDC Export; Build head + step 4 + Script files | separate state | Build; XDC = step 4 detail; Full board = advanced |
| 22 | Tools (Vivado) | Settings > Tools; Build step 2 | same | Build step 2 shows it + link |
| 23 | Add a board | rail +; Settings > Boards; Settings > Hubs > Add this board | 3 adds | rail + with "save to boards.toml" |
| 24 | Theme | rail foot; Settings General | same | rail foot |
| 25 | Background polling | header chip; general.background_poll; per-board poll; quiet notes | same | header chip → setting |
| 26 | Capabilities / why not | Details Capabilities; inline reasons; attention | same | inline reasons; Capabilities to a board info page |
| 27 | Board lock vs lease | rail "Open" chip + header "Open" chip beside the lease chip | two lock concepts | Open in the rail only; header shows Close board |

## Counts
- Tabs 12 (+ Details drawer, Consoles sub-tabs); workspace states 4; modal dialogs 8 + Settings 9 + 9 inline; banner types 13; ~46 cards; 141 inventory rows.
- Clicks from app start: health/identity/lease/temperature/card/SSH 0; Acquire 1; Release 2; Request 2; Identify 5 s 1 (rail); Identify 10-30 s 3 + scroll; Live LCD 1 + ~1000 px scroll; console 1; OpenOCD + gdb port 1; XVC open 2; Program 4 (+1 keep); Restore 3; Reset/Reboot 2; Restart shell/Power-cycle 3; DUT clock 4; harness install 4-5; Build kit/script/check+add 2/2/4; Checks start 2-3; Fix identity 3; **OS slots / card commit/clear: no UI (CLI only)**.
- Near-empty: Clocks (all boards); SD card on Linux/hub (all disabled); Power on Linux (2/4, both on Overview); Checks on desk boards (dead form); Update before Refresh; Build head-only for ~2 s; Activity session-only.

## Friction (ranked by frequency)
1. Lease: 11 places, 4 vocabularies, 2 rules; two Release buttons.
2. Who may drive the board changes tab by tab (attention says "must not drive" yet Program/Reset/Reboot/Debug stay enabled; XVC/install/Checks gated).
3. Board tile catch-all (7-9 rows; Reset beside Reboot).
4. Identity repeated 6-12 times with casing drift; subtitle repeats the facts row.
5. "Harness" and "Build" each mean two things.
6. Live display/panel buried; Identify 3 controls/2 states; setting 10 s vs blink 5 s.
7. Bring-up loop needs 3 tabs (Program → Consoles → Debug) while tiles duplicate half of each.
8. Update tab confusing (2 flows, 2 Roll backs, re-key drift, empty until Refresh).
9. XDC → Build → Program split over 3 tabs, overlapping pickers, state not carried.
10. Near-empty tabs (see counts).
11. "SD card" vs "Card"; refresh skips both.
12. "What happened to this board?" needs 4 places.
13. Interrupted SD / failed reads shown 3-4 times with different button words.
14. OS slots and card commit/clear have no UI.
15. Settings rows that do nothing; Debug/Consoles settings near-empty.
16. Add a board in 3 places; Theme in 2.
17. Lease banners stack above the header on every tab (~95 px).
18. Help = the CLI's 19 topics, not organised by the app.
19. Build tab empty for ~2 s on first visit.
20. USER_GUIDE §2 lists 11 sections; Checks missing.

## Caveats
Demo differs from a real board: no clocks adapter; no net_identity; hub board offers SD storage; polling never paused. No holder prompt / lease-taken banner staged. Light theme 1440x900 only.
