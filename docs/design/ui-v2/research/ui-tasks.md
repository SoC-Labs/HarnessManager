# Harness Manager UI by task (lane UI-TASKS), 30 Sep 2026

Walked in the running showcase demo (`app --demo`, 4 boards) with Playwright at main 1a127de. Files: `WALKS.md`
(every step), `walks/*.json|txt`, `shots/` (100), `crawl/<board>/<nn>_<tab>.{png,txt}` (every tab of every demo
board), `walk.py`, `crawl.py`, `serve_demo.py`. (Saved by the lead from the lane's hand-back.)

## Summary
Most daily tasks take 3-7 clicks. Program, consoles and gdb are already short; the friction is the lease and facts on the wrong tab.
1. **You cannot see who holds a lab board until you open it.** Sidebar lease badges only for open boards (`sidebar.js:386`); the preview's "Lock: free" (`app.js:263`) is the service lock, not the hub lease. Once open, the lease shows in 5 places and "Acquire lease" in 2.
2. **12 tabs don't fit at 1024 px**: the row scrolls with a hidden scrollbar (`app.css:242-244`), so SD card, Update, Checks, Activity are off-screen. On a lab Linux board 4 tabs are mostly "Cannot" (Clocks, SD card, most of Power, Checks without a hub).
3. **Frequent things hidden, rare things get tabs**: Live display at the bottom of folded Details; identity clash only in one Board-tile row; XDC, Clocks, SD card, Update each a whole tab.
4. **Tasks cross tabs**: boot banner = 4 page visits (Reset DUT not on Consoles); recovery spread over 4 tabs; Build picks the design on XDC and again on Build; failures only beside the greyed button, Activity logs nothing for a refused Program and 3 rows for one failed job.
5. **CLI vs app naming**: `debug up` = "Start" (tile) / "Open session" (tab); front panel = 3 CLI verbs, 4 app names; `board identity` (label/IP/MAC) next to "Identify" (blink).

**Proposal: keep sidebar + header + tabs + Settings; 12 tabs → 6-7**: Overview · Program · Consoles · Debug · Build · Board (+ Checks on hub boards only). XDC → Build step 4; Clocks + Power + SD card + Update + Details reference cards → one Board tab; Front panel onto the Overview; Activity → a drawer from the sidebar footer.

| Task | Today | Proposed |
|---|---|---|
| See who holds a board | 2 (and you open it) | 0 |
| Take a free lease | 3 | 2 |
| Request a held board | 4 | 3 |
| Console + reset | 7 | 5 |
| Give the board back | 5 | 2 |
| First run | 10 | 5 |
| Build → Program | ~15 | ~10 |

## Top tasks (ranked)
1 lease / who has it / request · 2 DUT console (+ reset for the banner) · 3 program + confirm · 4 gdb · 5 give the board back (restore, release, close) · 6 why did it fail · 7 health + temperature · 8 front panel · 9 identify a physical board · 10 ILA over XVC · 11 build my own DUT · 12 overnight checks · 13 change a setting · 14 recover a stuck board · 15 update a board's harness · 16 update HM · 17 first run (hub + board) · 18 fix an identity clash.

## Proposed structure
| Today | Proposed |
|---|---|
| Overview: 4 tiles + folded Details | Overview: Design, Consoles, Debug, Board tiles + a **Front panel** tile (Live display); Needs attention stays; Details goes |
| XDC tab | Build step 4 exports the RM kit; Full-board export under "More exports" |
| Build tab | Build, one design picker for the whole journey |
| Program tab | Program: preflight inline under the picked overlay; one outcome box; a **Card** line under Program; Restore baseline |
| Consoles tab | Consoles + an armed **Reset DUT** in the console toolbar |
| Debug tab | Debug: OpenOCD + "Logic analysers (ILA over XVC)" |
| Power, Clocks, SD card, Update + Details reference cards | **Board** tab: Recover (ladder in order), Readings, DUT clock, Harness versions, Card & OS slots, SSH claim, Identity, Configuration SD (only with a route), About |
| Checks tab | Checks, hub boards only |
| Activity tab | **Activity drawer** from the sidebar footer, filtered to the selected board |
| Lease in 5 places | badge on **every** hub board in the sidebar, **one** lease control in the header, the request bar while waiting |
| 3 ways to add a board | one "Add a board" dialog from sidebar + (address, or hub + targets) |

## Task table
| Task | Today: path · clicks · pages | Proposed path | Clicks | What changes |
|---|---|---|---|---|
| t02a who holds a board | select → preview "Lock: free" (wrong question) → Open → read header/bar/strip/sidebar/tile · 2 · 2 | sidebar badge on every hub board; preview "Hub lease: held by alice, 47 min left" | 0 | `lease show` once per hub at Scan/selection (hub, not board); rename "Lock" |
| t02b take a free lease | select → Open → Acquire · 3 · 2 | select → **Open and take the lease** | 2 | drop Acquire from Needs attention |
| t02c request a held board | select → Open → Request → type → Send · 4 · 3 | select → **Request board** on the preview → type → Send | 3 | request without locking a board you can't use |
| t05 console + boot banner | Open → uart0 Open → Overview → Arm → Reset DUT → Consoles → Attach with screen · 7 · 4 | Open → uart0 Open → Arm → Reset DUT (console toolbar) → Attach | 5 · 1 | Reset DUT in the console toolbar |
| t04 program + confirm (hub) | select → Open → Acquire → Program → pick → preflight below fold → Arm → Program → "Done" in Progress · 7 · 3 | select → Open and take → Program → pick (inline preflight) → Arm → Program; header Design chip "verified 12:04" | 6 · 2 | inline preflight; one outcome box; Program not primary while someone else holds the lease |
| t04b keep on card | … tick Keep → Arm → Program; what boots next only on Overview · 6 | Card line under the button "Boots next: nanosoc [A] · ☐ keep on card" | 6 · 1 | merge keep tick + card status; hide SD card tab without a config-SD route |
| t06 gdb | Open → Start (tile) → Copy; command only on the tab · 3 | Open → Open session → Copy; tile shows full `target extended-remote 127.0.0.1:PORT` | 3 | one name everywhere |
| t16 give the board back | Program → Arm → Restore → Close → Release and close · 5 · 3 | Close board → **"Restore baseline, release and close"** (default when you hold the lease and a design is loaded) | 2 | close dialog carries the runbook order |
| t14 why it failed | refused Program: reason beside the button, Activity empty; failed job: 3 rows · 0-2 | Activity drawer from anywhere (1); header "last error" chip opens it filtered | 1 | one row per failure; refused actions logged; link back to the card |
| t09 health + temperature | header Health + tile Temperature; telemetry in Details; Power has no temp · 1 | same first look; one Readings card on Board | 1 | merge Details › Telemetry with Power › supply |
| t08 front panel | Open → Details → scroll · 2 + scroll | Front panel tile on the Overview | 1 | shows label + who's watching |
| t03 identify | sidebar Identify icon · 1 | unchanged + board's own label on the card | 1 | drop the Board tile copy |
| t07 ILA | (program nanosoc_ila) → Debug → XVC Open → Copy Tcl → .ltx · 4 / 8 | XVC card: "no ILAs in this design" + **Program nanosoc_ila** → Open → Copy Tcl (Tcl sets PROBES.FILE) | 3 / 5 | "ILA" appears nowhere today |
| t11 build my DUT | XDC → pick → zip → Build → pick again → dir → Generate → copy → Vivado → Refresh → Check → Add to Program → Program → pick → Arm → Program · ~15 · 4 tabs | Build → pick → dir → Generate → copy → Vivado → Check → **Add and program…** → Arm → Program | ~10 · 2 | one picker; XDC = step 4; Add preselects the overlay |
| t12 overnight checks | select → Open → Checks → tick take-lease → Write announcement → Copy → Start · 7 · 3 | same, announcement auto-written | 6 | hide Checks without a hub |
| t15 change a setting | Settings (opens on Updates) → Tools → type · 2 | same; errors link to the setting | 2 (1) | open on General/last used; one Theme; hide unread settings |
| t17 recover | Power → Arm → Restart shell; other steps on 3 other tabs · 3 · 4 tabs | Board › Recover (USER_GUIDE §14.2 order) → Arm → Restart shell | 3 · 1 | Reboot off the Overview |
| t10b harness update | Open → Update → Refresh → Install… → Arm → Install · 7 · 3 | Open → Board → Install… → Arm → Install (list loads on first visit) | 6 | drop the older channel checker and "The app" card |
| t10a HM update | banner "Restart to update" · 1 | unchanged | 1 | — |
| t01 first run | Settings → Hubs → Add a hub → host → Test → Add hub → Test connection → Add this board → close → select → Open · 10 · 5 | sidebar + → From a hub → host, Test → tick target → Add and open | 5 | one dialog |
| t13 identity clash | Open → find it in the Board tile → Fix identity → label → Set and restart; nothing else points to it · 3 | Needs attention "Identity clash with mps3-01" + **Fix identity…**; warning dot on both sidebar cards | 2 | Identity row → Board tab |

Remember-from-another-page: t05 Reset DUT; t04b what boots next; t07 which overlay has ILAs; t11 the design picked on XDC; t14 Activity has no link back; t15 walk to Settings › Tools; t17 recovery order; t02a free before opening.
Dead ends: preview "Lock: free" for a held board; Activity Errors empty after a refused Program; Checks "not behind a hub" after a full form; nothing points to an identity clash; Clocks "Cannot" vs tile "DUT clock 50 MHz"; SD card all "Cannot" on Linux; tabs off-screen at 1024 px.

## Duplicates
Lease state (bar, header, attention, sidebar, tile) → header + sidebar badge (+ bar while waiting) · Acquire (header, attention) → header · Identify (sidebar, tile, Front panel) → sidebar + Front panel · Reset DUT/Reboot (tile, Power) → Consoles (Reset) + Board › Recover · Theme (footer, Settings) → footer · Add a board (3) → one dialog · Harness update check (Refresh list, Check for updates) → one list · HM update (banner, Settings, Update "The app") → banner + Settings · Program outcome (result box, Progress) → one box · Card state (Keep box, tile Card line) → Card line under Program · Readings (tile, Details › Telemetry, Power) → Board › Readings · Design picker (XDC, Build) → Build · one job failure = 3 Activity rows → 1 · Debug action "Start"/"Open session" → one name.

## CLI vs app wording
attach/detach vs Open/Close board (add `open`/`close` CLI aliases) · board name `mps3_02 … target mps3_02_pl` vs "mps3-02" vs dialogs "mps3_03" (one UI name) · console/pty/--export vs Open/Attach with screen/Export to TCP · `debug up/down` vs Start/Stop vs Open/Close session ("Open session") · `xvc` vs "Fabric debug (XVC)" vs guides' "ILA" ("Logic analysers (ILA over XVC)") · panel show/mirror + display show/snapshot vs Panel line/Front panel/Live display/text mirror (one noun "Front panel", Live + Text views) · `board identity` vs Details "Identity" = shell/design ids (rename "About this board") · info/telemetry/mcc temp vs Health/Temperature/Telemetry ("Readings") · harness list/install vs Harness versions + old "Check for updates" (drop old) · `kit pack --import` vs "Add to Program" · exit 14 vs "preflight MISMATCH (shell_id matches)" (rename the check "same shell").

## Prominence
More: lease + request (badge on every hub board, request/take from preview, one header control) · Program + status ("verified" on the Design chip, inline preflight, Card line) · Consoles (Reset DUT in toolbar) · Identify (+ board label on the card) · Front panel onto the Overview · Errors (Activity drawer, "last error" chip, refusals logged) · Identity clash (attention + sidebar).
Less: XDC (Build step 4; Full board under More exports) · Clocks (one DUT-clock row; oscillators folded) · Config SD (Board card only with a route) · Power/Reboot/Power-cycle (Board › Recover; Reboot off the Overview) · Harness versions (Board card) · Checks (hub boards only) · Details (Board › About; Build-check chip into a tooltip) · raw MCC / lab tools (CLI only) · stored-but-unread settings (hide).

## Separate findings
`sections/placeholders.js` dead code · on mps3-02 "Program…" stays blue while attention says "must not drive the board" · Overview DUT clock vs Clocks "no adapter" read different sources.
