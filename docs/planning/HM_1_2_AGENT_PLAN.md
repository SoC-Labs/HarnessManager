# HM 1.2 agent plan: mint-4 harness support + priority fixes

Owner: HM lead. Written 13 Oct 2026. Base: `main` 8e7100f (HM 1.1.0).
Integration branch: `integ/v1.2` (worktree `../hm-integ-v12`). Every lane is a Sonnet agent
in its own worktree `../hm-12-<lane>` on branch `team/12-<lane>`, cut from `integ/v1.2`.

Inputs (all local, read-only for lanes):
- v0.20 contract: the platform repo's `docs/contracts/net-protocol.md` (mint-4 branch)
  ("Mint 4 — hostio streams, sensors, mcc, cosim, hostio", from line ~2060; `usd` status
  `slot_mb`/`store_ver`).
- pyverify + FakeShell `linux_mint4`: same checkout, `host/pyverify/` at lane/m4b-s2b 0554433d.
- Card layout source of truth: `src/linux_soc/hw/fw_stage0/stage0_mkcard.py` `layout(store_mib)`
  on origin/integ/mint4-shapeA 2d000082 (platform repo).

## Contract rules every lane follows

1. **Gate on feature NAMES, never on the protocol version.** The contract says so. Names:
   `hostio`, `cosim`, `sysmon`, `mccif`, `mcc_local`. Verb `mcc` exists when either MCC name does.
2. New verbs fail as `{"ok":false,"err":…,"code":…}`. Map `code` to a sentence:
   `not_supported`, `no_hw`, `locked`, `invalid`, `denied`, `no_route`, `busy`, `not_held`, `timeout`.
3. Claim-locked acts (`hostio set`, `sensors set/clear_alm`, `mcc osc_set/mbox w/reboot`,
   `cosim halt/step/run`, `reset mcc_warm/ddr`) go through HM's existing claim-forward route.
4. 6933 (hostio ADP) is claim-gated and is NOT in `identify.ports`. Never forward it from
   the ports list; only via `claim_ports` over the claim SSH.
5. `usd` `slot_mb`/`store_ver` may be absent (old shell, or store header not read). Absent
   means assume 8 MiB slots. `store_ver` 1 = 8 MiB, 2 = 32 MiB.

## Priority list

P0 = HM would write the wrong place, lose data, or mislabel mint-4 hardware.
P1 = a workshop attendee hits it (workshop 22-23 Oct). P2 = new capability. P3 = cleanup.

| Pri | Item | Lane |
|---|---|---|
| P0 | Card writer hard-codes v1 OS-slot LBAs; on a layout-v2 card it writes slot A over the store | L1 |
| P0 | No slot capacity: over-size keep-on-card commit returns "bad args" | L2 |
| P0 | Roll back waits 180 s, cold boot ~190 s: false failure | L2 |
| P0 | Card re-make to v2 wipes /persist (claim keys, keep-on-card); no backup | L3 |
| P0 | 6931 becomes hostio1 on mint 4; HM labels it uart1 and paces it at 20 ms/char | L4 |
| P0 | Tests model harness 1.0.0; no mint-4 fake board; vendored pyverify lacks v0.20 | W0-A |
| P0 | Design facts in four disagreeing tables; mint-4 sizes 3.45/7.0/3.97 MB need budgets | W0-B |
| P1 | Raw exceptions and JSON on screen | L6 |
| P1 | "Slot A is empty" when it isn't | L2 |
| P1 | Claim time in UTC · "/24" on hub boards · false identity clash · rescue "Next: name it" · hub label = description | L6 |
| P1 | MCC remote reboot and warm reset with no USB (`mcc reboot` two-phase, `reset mcc_warm`) | L5b |
| P1 | Sensors (die temp, rails, alarms) in Readings | L5a |
| P2 | Cosim: halt / step N cycles / run the DUT clock | L5c |
| P2 | Own icon and app on Linux/macOS/Windows taskbars | L7 |
| P2 | Multicore debug refused on the host path though the cfg ships | W0-B |
| P2 | Config SD through the hub | L8 (wave 2) |
| P2 | Adopt / add-key in the window | L9 (wave 2) |
| P2 | Per-slot version from slot status; dedupe release matching | L10 (wave 2) |
| P3 | One vocabulary, one place per action, hide jargon, cut settings | L11 (wave 3, alone) |
| P3 | Helper dedupe, dead code, JS unit tests, pyverify wheel, file splits | after 1.2 |

**Blocked, not planned:** a netboot-vs-card boot-source selector. v0.20 has no verb for it
(only the A/B boot-select sector via `slot commit`). Ask the Linux lead for a verb first.

## Waves

```
Wave 0  (2 lanes, ~3 h)      W0-A vendor+fakes   W0-B design manifest
            │ HM lead merges into integ/v1.2, runs make check
Wave 1  (8 lanes, ~4-8 h)    L1 card-mbr  L2 card-capacity  L3 persist  L4 hostio
                             L5a sensors  L5b mcc-reset  L5c cosim  L6 bug-pack  L7 icon*
            │ HM lead merges in order L1,L2,L3,L4,L5a,L5b,L5c,L6; make check after each
Wave 2  (3 lanes, ~4 h)      L8 hub-sd  L9 adopt-gui  L10 slot-version+match-dedupe
Wave 3  (1 lane)             L11 vocabulary + one-place-per-action + settings (touches all JS)
Gate    HM lead: make check + web tests; Thu: David runs the HIL sheet on board 2 (mint 4)
Ship    1.2.0 beta after re-vendoring pyverify from a TAGGED platform commit
```
*L7 owns only packaging files, so it can start in wave 0.

Calendar: wave 0 now, wave 1 tonight, wave 2 Wed morning, wave 3 Wed afternoon,
gate Wed evening, HIL Thu (bug flush). Board 2 is fielded Wed 14:00-16:00 without HM.

## Rules for every lane (paste into each brief)

- Work only in your worktree. Edit only the files your lane OWNS. If you must touch
  another file, stop and report the exact change you need; the HM lead makes it.
- Commit on your branch with explicit paths (`git add <file>…`). Never `git add -A`, never
  push, never merge, never rebase other branches.
- No boards, no leases, no hub, no SSH to real hosts, no ports 23300-23727 or 10000-19999.
  Tests use the fake boards (`pyverify.testing.fakeshell`, `tests/fakes/`).
- Never edit `/research/AAA/ip_library/**` or `/research/AAA/phys_ip_library/**`.
- Never weaken trust: `update/trust.py` PINNED_KEYS stay as they are.
- User-facing text: plain words, no internal hostnames, paths, shas or lane names.
- Done means: new tests for your change, `make lint` clean, the tests for your area pass,
  and one `make test` run whose result you report (known flaky: test_quiet_poll,
  test_lm5_display_cli, test_l5_install; report them, don't fix them).
- Hand back: branch, commit list, files changed, test counts, anything left undone and why.
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`

## Lane briefs

### W0-A VENDOR + FAKES (P0)
Goal: integ/v1.2 builds and tests against pyverify v0.20.
- Run `scripts/vendor_pyverify.sh <platform checkout> <sha>` against the mint-4 branch.
  Update the sha pin in `tests/unit/test_l5_release.py` and `vendor/README.md` provenance,
  marked "DEV ONLY: re-vendor from a tagged commit before 1.2.0".
- `tests/fakes/virtual_board.py`: bring `LINUX_HARNESSD` to v2.1 (current features, real
  static id); add `LINUX_MINT4` on FakeShell profile `linux_mint4` with knobs for an old card
  (`usd_slot_mib=8`) and an old shell (`usd_report_store_ver=False`).
- `harness_manager_mps3/capabilities.py`: declare CapabilitySpecs for `hostio`, `sysmon`
  (sensors verb), `mccif`, `mcc_local`, `cosim`, plus the user-card OS slots and store
  (today gated ad hoc). Declarations only; adapters come in wave 1.
- `harness_manager_mps3/selfcheck.py`: also scan function-level `from pyverify… import`.
OWNS: vendor/**, scripts/vendor_pyverify.sh, src/harness_manager_mps3/openocd_cfg/**,
tests/unit/test_l5_release.py, tests/fakes/**, capabilities.py, selfcheck.py.

### W0-B DESIGN MANIFEST (P0)
Goal: one table per design; budgets from it.
- New `src/harness_manager_mps3/designs.py`: one row per rm_id with name, UART map,
  DAP config, writes-DUT-flash reason, partial size, expected load time. Mint-4 sizes:
  greybox 3.45 MB, multicore 7.0 MB, eth_ss 3.97 MB; clear buffer 4 MiB; ~3 min multicore load.
- Replace `constants.py` KNOWN_DESIGNS / WRITES_DUT_FLASH / DAP_DESIGN_CONFIGS and
  `uart.py` DESIGN_UART with views of it (keep the old names as aliases this release).
  Include every design the UART map knows (eth_ss, uart_echo, clcd_demo, dbg_demo, nanosoc_ila).
- `openocd.py`: allow nanosoc_multicore on the host path with the shipped
  `nanosoc_mps3_multicore_jtag.cfg` (today refused at :118-123).
- Remove the time-dependent "until Linux v2.1" wording.
OWNS: designs.py (new), constants.py (design tables only), uart.py, openocd.py,
web/static/js/sections/program.js lines 25-40 only.

### L1 CARD-MBR (P0)
Goal: the card writer finds partitions the way stage0 does.
- New `services/card_layout.py`: parse the MBR; OS slot A/B = MBR entries 1/2, store =
  the entry with type 0xDA, persist = entry 3. Mirror `layout(store_mib)` and test it
  against v1 (store 2048+65536, A 67584, B 198656, persist 329728) and v2 (store
  2048+196608, A 198656, B 329728, persist 460800), sectors of 512 B.
- `services/cardwriter.py`: delete fixed LBA_A/LBA_B; read the card's MBR before any
  write; refuse a card with no valid MBR with a plain message. Docstrings in bringup.py
  that quote the old LBAs: report them, don't edit.
OWNS: services/card_layout.py (new), services/cardwriter.py, daemon/cardwriter_api.py,
tests for both.

### L2 CARD-CAPACITY + SLOT TEXT (P0/P1)
- `core/pack.py` CardStatus/SlotInfo: add `slot_mb`, `store_ver` (None when absent).
- `harness_manager_mps3/deploy.py` `_card_facts` / `card_status_from`: read them.
- `harness_manager_mps3/os_slots.py` and `card.py`: size check before push/commit using
  the design manifest size; map the engine's "bad args" on commit to "This card's slots
  are 8 MiB. Re-make the card in layout v2 (32 MiB slots) to keep this design on it."
- `services/slots.py` REBOOT_WAIT_S 180 → use `constants.REBOOT_WAIT_S_LINUX`;
  same in `daemon/card_api.py:141` and `update/executor.py:729`.
- `web/static/js/sections/update.js`: subtitle uses slot state, not version (:289-290);
  show the card's layout version and slot size. `sections/boardfacts.js` likewise.
OWNS: core/pack.py (CardStatus, SlotInfo only), deploy.py, os_slots.py (mps3), card.py,
services/slots.py, daemon/card_api.py, update/executor.py (that line only),
sections/update.js, sections/boardfacts.js.

### L3 PERSIST BACKUP (P0)
Goal: before a card re-make, save /persist to this PC; restore after.
- New `services/persist_backup.py`: over the claim SSH (reuse `harness_manager_mps3/claim.py`
  helpers, call them, don't edit them): `tar -C /persist -czf - .` to a local file under the
  user's data dir with a manifest (board id, time, sha256, file list); restore = upload and
  untar, then verify the file list.
- `daemon/persist_api.py`: backup, list, restore jobs with progress.
- New `web/static/js/sections/persist.js`: a "Card contents" card: Back up, Restore, and a
  rule that the re-make action stays disabled until a backup of this board exists. Export a
  mount function; the HM lead wires it into Versions at merge.
- CLI: `harness-manager card backup|restore` in a new `cli/cmd_persist.py`.
OWNS: the four new files + their tests. Registration lines in daemon/app.py and cli/main.py:
send them to the HM lead.

### L4 HOSTIO (P0)
- When `version.hostio` is present: label 6931 "hostio1" (not uart1), no 20 ms pacing;
  otherwise keep uart1. `identify.ports.hostio_usrt1` likewise.
- 6933 ADP: only via `claim_ports` over the claim SSH; never in the forwarded port list.
- `hostio` verb: show mode / pending / held and drops in the console panel; "Switch to
  hostio" is claim-locked and refused with `not_held` text unless greybox or held.
OWNS: harness_manager_mps3/constants.py (CONSOLE_PORTS only), tunnel.py, identify.py,
pack.py (console endpoints only), services/console.py, web/static/js/consoles.js,
sections/consoles.js.

### L5a SENSORS (P1)
- `sensors` get into Readings: die temp (cal/raw/min/max), VCCINT/AUX/BRAM, alarm bits as
  words, thresholds. `set`/`clear_alm` claim-locked, behind an arm step. `stats.sysmon`
  raw codes keep feeding the existing telemetry path.
OWNS: harness_manager_mps3/telemetry.py, sysmon.py, daemon/readings_api.py,
sections/clocks.js.

### L5b MCC + RESET (P1)
- `mcc` verb routes `scc` and `loopback` in `harness_mcc.py`; prefer it over USB/hub MCC
  when the board advertises it (selector in `mcc.py` and `daemon/mcc_route.py`, one place).
- Recover gains "Power-cycle the board (MCC reboot)": two-phase with a caller token;
  witness = board returns, same shell_id, `last_reboot` token state `done`; ETA 100-180 s.
  "Warm reset (like PBON)" = `reset mcc_warm`. Both claim-locked, both armed.
- Mirror the deny list from `pyverify.mint4` (MCC_DENY_ACTS, MCC_OSC_PROTECTED,
  MCC_PROBE_FUNCS). HM never offers a denied act. Clock set: devices 2, 4, 5, 7 only.
- `power.js:102`: no JSON.stringify of evidence.
OWNS: harness_mcc.py, mcc.py (selector only), daemon/mcc_route.py, daemon/power_api.py,
sections/power.js.

### L5c COSIM (P2)
- New `services/cosim.py`, `daemon/cosim_api.py`, `cli/cmd_cosim.py`
  (`cosim cycles|halt|step N|run`), new `web/static/js/sections/cosim.js` Workbench rail
  card: cycle counter, Halt / Step N / Run, the async-input limitation stated once.
OWNS: those new files + tests. Registration lines: send to the HM lead.

### L6 BUG PACK (P1)
- Raw errors: `daemon/app.py:833-835`, `daemon/jobs.py:296`, `services/hil_runs.py:697`
  return a sentence + a next step; raw text goes to Activity only. `overview.js:1037`.
  `bringup.js:521` no JSON.stringify.
- `sections/claim.js:134,137,146`: local time via `format.js` clock().
- Hub boards: no "/24" note (`identity_assign.py:322-325`, `board_identity.py:1000,1242`,
  `bringup.py:1001`, `identity.js:318,355`, `bringup.js:1056`).
- `daemon/identity_api.py:107-149`: dedupe a board and its own hub entry by MAC/board id.
- `bringup.js:1066`: rescue title names the real next step.
- `settings/hubs.py:522,529` and `daemon/hubs_api.py:197`: label = hub target name.
OWNS: the files named here only (L2 owns update.js; L5b owns power.js).

### L7 APP ICON + IDENTITY (P2, can start now)
- Design 3 icon candidates as SVG (MPS3 board outline + SoC Labs feel; legible at 16 px;
  light and dark tray variants) in `packaging/icon/`. Produce PNG 16-1024, `.ico`, `.icns`
  with a script (`packaging/icon/build_icons.py`, Pillow; no new runtime deps).
- macOS: `packaging/macos/` .app bundle template + `Info.plist`; installer step creates
  `~/Applications/Harness Manager.app` launching `harness-manager app`.
- Windows: Start Menu shortcut with the `.ico`; set AppUserModelID in `web/window.py`
  before the window opens.
- Linux: hicolor PNGs; StartupWMClass matches the pywebview window class.
- `web/window.py`: pass the icon to pywebview where the backend supports it.
OWNS: packaging/**, web/window.py, scripts/install.sh, scripts/install.ps1,
web/static/favicon.svg. David picks the icon before merge.

### Wave 2 (after wave 1 merges)
- **L8 HUB-SD:** config SD on hub boards via `hub_sd` as the storage adapter (sd.py,
  sd_ab.py, hub_sd.py, sections/sd.js, actions.js).
- **L9 ADOPT-GUI:** Access gains "Adopt this board" and "Add a key" with the CLI's arm
  steps (sections/claim.js, daemon/claim_api.py).
- **L10 SLOT-VERSION:** per-slot version/sha from slot status when present; the catalogue
  calls `planner.match_release` (delete the copy at harness_catalog.py:396-409, one
  running summary).

### Wave 3
- **L11 WORDS:** one vocabulary (board, release, take/give back; "slot" = OS slot only),
  one place per action, jargon to About, ~40 settings behind developer mode. Alone, last.

## HM lead's integration checklist
1. Create integ/v1.2 + worktrees; launch W0-A, W0-B, L7.
2. Merge W0 lanes; `make check`; then cut wave-1 worktrees from the new integ/v1.2.
3. Merge wave 1 in priority order; wire L3/L5c registration lines; `make check` + web tests.
4. Wave 2, wave 3 the same way.
5. Board 2 HIL sheet for David (Thu): card layout v2 read, size refusal on a v1 card,
   persist backup/restore round-trip, hostio1 console, sensors, MCC reboot witness, cosim step.
6. Re-vendor pyverify from a tagged platform commit; release 1.2.0 beta.
