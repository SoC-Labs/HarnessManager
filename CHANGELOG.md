# Changelog

Each release lists what a user of Harness Manager will notice. Versions follow
`MAJOR.MINOR.PATCH`; until 1.0.0 a minor version may change the command line or the
API (docs/API.md says what changed).

## 1.0.1 (2026-10-07)

- **A board running harness v2.0.0 is recognised.** v2.0.0 was published with a firmware
  identity the board does not report, so Versions said "Running unrecorded" and marked 2.0.0
  "incompatible" through the hub. Harness Manager now also recognises the running release by
  the OS image in the board's running slot: a v2.0.0 board shows "Running 2.0.0", and 2.0.0 has
  nothing to install.

## 1.0.0 (2026-10-07)

The first release for people outside the build team: SoC Labs staff and external MPS3
owners.

### MPS3 board revisions B and C (FIX-PACK-9, rc2)
- **A config-SD bundle for both revisions installs onto either.** Platform v2.0.0 ships
  `MB/HBI0309B` and `MB/HBI0309C`, identical apart from the `BOARD:` line (Rev C supported, Rev
  B "boots, untested"; david, 2 Oct). The MCC reads only the folder of the revision it detects,
  so a card is written with both folders, as Arm's own bundles are. A folder the bundle does
  not carry (an Arm `HBI0309A` tree) is never touched, and stays in the backup.
- **MBBIOS is kept in every revision's folder.** The FIX-PACK-7 rule now runs on each
  `MB/HBI0309<rev>/board.txt` against the card's board.txt of the SAME revision: the card's
  line is kept; a revision folder the card lacks gets the bundle's line unchanged, unless the
  `.ebf` it names is anywhere on the card (refused, 15: "this card would make the MCC update
  itself to mbb_v141.ebf through MB/HBI0309B/board.txt: remove mbb_v141.ebf from the card, or
  add --allow-mcc-update"; `error.data.mcc_update.board_txt` names the file). The note names
  each revision: "MBBIOS kept: HBI0309C mbb_v132.ebf; MBBIOS: HBI0309B mbb_v141.ebf from the
  bundle (the card has no mbb_v141.ebf, so the MCC will not update)". A C-only bundle says what
  it said before ("MBBIOS kept: mbb_v132.ebf").
- **A card with neither folder is written, with a warning:** "WARNING: this card had no
  HBI0309B or HBI0309C folder: is it an MPS3 configuration SD? both were written" (a note
  beside the MBBIOS one). The plan says it first: "the config SD has no revision folder
  (MB/HBI*): is it this board's configuration SD? harness 2.0.0 writes MB/HBI0309B,
  MB/HBI0309C".
- **The plan checks the board's revision.** Harness Manager reads it from the MCC's boot log
  (after a REBOOT it witnessed: "Configuring motherboard (rev C, var A)"), else the card's
  `LOG.TXT` ("MotherBoard Revision C Variant A"), else a card with only one `MB/HBI0309*`
  folder; a card with several says nothing (until FIX-PACK-9 the folders were all it read). A
  release that does not carry the board's revision is blocked: "this board is HBI0309B (LOG.TXT
  on its config SD), and harness 1.2.0 carries MB/HBI0309C only: the MCC reads only
  MB/HBI0309B/, so the board would stay unprogrammed". On a Rev B board a B+C release plans
  with the warning "Rev B: boots, untested. This board is HBI0309B (LOG.TXT on its config SD);
  harness 2.0.0 is supported on Rev C". The release list's verdict says the same.
- The bundle check refuses a release that declares a revision (`compat.board_revs`) whose
  `MB/<rev>/board.txt` is not in its SD tree.
- **The hub door** writes HBI0309C's `nanosoc.bit` only (fpgahub's one file), so it leaves a
  release's `MB/HBI0309B` folder on the card as it is: the plan warns "the hub door leaves
  MB/HBI0309B on the card as it is: it writes MB/HBI0309C/Nanosoc/nanosoc.bit only (the board
  behind the hub reads that folder)" and the install notes "MB/HBI0309B not written: the hub
  writes MB/HBI0309C/Nanosoc/nanosoc.bit only, and this board reads MB/HBI0309C".
- **The A/B view (`updates.sd_ab`, off by default) flips HBI0309C's pointer only**, so it
  refuses a B+C release before reading or writing: "the A/B install (setting updates.sd_ab)
  writes MB/HBI0309C only, and this release also carries MB/HBI0309B: nothing was written"
  (hint: turn `updates.sd_ab` off and install again).

### `clock` on a Linux board says the DUT clock is fixed (FIX-PACK-9, rc2)
- `harness-manager clock <ip>` on a Linux board said "dut  unavailable: the shell cannot read
  the DUT clock back; set it to know it": the CLI's board is only an address, so the MPS3 pack
  read it as bare metal. It now asks the board (the shell's `version`), once, and says what the
  app's tile says, in human, `--json` and `--tsv`: `dut  50 MHz  [pin-model]  (fixed by the
  shell: the Linux harness cannot change it)`. The reading (`GET /boards/{bid}/clocks` too) has
  source `pin-model` (was "the pin model, shell <id>") and that reason (was "fixed by the
  shell"; the tile still shows "fixed by the shell"). A bare-metal board is unchanged.

### The sidebar shows the board's own name from identify (FIX-PACK-9, rc2)
- The platform's rc2 identify sends the board's name as `label` ("MPS3-02"; net-proto v0.16,
  lane IDENT), and Harness Manager read only the proposed `name` key, so the sidebar showed the
  address. It now reads `label` first and falls back to `name` (each must be a clean name; a bad
  label falls back), and none in stage0 rescue. Precedence is unchanged: a boards.toml `name`
  still wins, then the board's own name, then the hub's.

### Boundary not timed: kept as a guard (FIX-PACK-8, rc2; N1 after N2)
- N2 (below) fixed the cause of Linux v2.0.0 known issue 11 in `build_rm.tcl`. A build from an
  older `build_rm.tcl`, or one from a synth checkpoint you bring (`build.synth_dcp`) that already
  carries a `create_clock`, still links with the static<->RM boundary untimed while Vivado's
  summary says every constraint is met. Harness Manager now reads check_timing from the build's
  `<name>_timing.rpt` and, when it counts more than 0 register/latch pins with no clock or more
  than 1,000 unconstrained endpoints (not counting those on a constant clock), says so as a
  WARNING, never a refusal: "boundary not timed (known issue 11): Vivado's check_timing in
  minimal_timing.rpt found 27,984 register/latch pins with no clock (27,446 on OSCCLK1; 538 on
  u_shell/…/tck_i_reg/Q) and 86,692 unconstrained endpoints (Harness Manager warns above 0 and
  1,000), so the static<->RM partition boundary was not timing-analysed, even where the summary
  says every constraint is met. Your RM's own paths were still timed." with "fix: rebuild with a
  build_rm.tcl written by this Harness Manager (`harness-manager kit script ... --out DIR`): it
  writes the RM checkpoint before it reads the OOC XDC, so the OOC create_clock no longer
  replaces the static's clocks at the link. A synth checkpoint you bring (build.synth_dcp) must
  be written before any create_clock is read into it."
- Why those limits: issue 11 measured 27,984 unclocked pins and 86,692 unconstrained endpoints on
  `minimal`; with N2 both are 0. The 423 endpoints "due to constant clock" are in every build of
  the static, N2's too (a clock tied off in the static): they are not timing paths and never
  count. 1,000 keeps a few unconstrained endpoints of an RM's own from being called issue 11.
- Where: `kit check` (a `WARNING:` line and its `fix:` under the verdict; the check
  `boundary_timing`, `ok` or `warning`; `--json` `facts.boundary`), `kit pack`'s checks, `POST
  /kits/check`, the guide (`boundary`; the Check step's reason) and the Build tab's Check step
  ("done, with a warning": the warning and its fix above the checks, Timing marked; Add stays
  open). `kit build DIR` warns when DIR's `build_rm.tcl` reads the OOC XDC before it writes the
  checkpoint (an older Harness Manager wrote it: write it again with `kit script`), and when the
  last build there was not timed. docs/API.md.
- The OOC XDC kit's header no longer claims "the static's propagated clocks supersede every
  create_clock here": it says to read the file only after the RM checkpoint is written, and why.

### nanosoc_multicore asks before it writes the DUT's flash (FIX-PACK-8, rc2)
- **Programming nanosoc_multicore needs a typed word.** Its CPU1 boot ROM writes one 0x00 byte
  at DUT flash 0x20000 onward on every boot, until the v2.1 ROM fix (Linux v2.0.0 known issue
  15), which damages a MicroPython image there. Harness Manager cannot see what the DUT's flash
  holds, so it warns before EVERY program of the design (rm_id `0x01000003`, by name or design
  id): "nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte at 0x20000 onward)
  on every boot, until Linux v2.1: it damages a MicroPython image there (nanosoc_upy). Type
  MULTICORE to program it anyway."
- **CLI:** `program TARGET nanosoc_multicore` prints the warning and asks for MULTICORE, typed
  (it answers the y/N question too). `--yes` never implies it: at a terminal the word is still
  asked for, and a run with no terminal refuses (exit 15, "… — nothing was programmed: run it
  in a terminal and type the word, or pass --allow-dut-flash-write (--yes never implies it)").
  The new `--allow-dut-flash-write` gives it for a script and prints "WARNING: <why>
  Programming it anyway (--allow-dut-flash-write).". A word typed wrong refuses (15, "not
  confirmed: nanosoc_multicore was not programmed (its boot code writes the DUT's flash)").
  `overlays` marks the design "writes the DUT's flash".
- **API:** `POST /boards/{bid}/deploy` needs `allow_dut_flash_write: true` for such a design;
  without it 409 REFUSED (15) before any job, with the warning as the message and
  `error.data.dut_flash_write: {design, why, word}`. The OverlayRef gains `writes_dut_flash`
  (`{why, word}` or `null`, additive). docs/API.md.
- **App:** the Workbench's Program strip shows the warning above Program, with a field to type
  MULTICORE in; Program (and Program anyway) stays off until it matches ("type MULTICORE to
  program nanosoc_multicore: its boot code writes the DUT's flash"), behind the lease holder
  rule and Arm as before. The picker tags the design "writes DUT flash", and the preflight adds
  a "writes the DUT's flash" chip. Each program is a fresh choice: the field empties on a new
  pick and once Program runs.
- Board-agnostic: the pack declares it (`WRITES_DUT_FLASH` in the MPS3 pack's design table;
  `core.pack.DutFlashWrite`), and core shows any pack's declaration. No HIL-AUTO plan programs
  nanosoc_multicore; docs/HIL_LINUX.md OCD8 (manual) passes `--allow-dut-flash-write`.

### OpenOCD on the board (DEBUG-ONBOARD)
- **On a claimed Linux board, OpenOCD runs on the board.** When the board's image has
  OpenOCD and its launcher (`mps3-debug`, v7 or later), `debug up` starts OpenOCD there over
  the claim's SSH, and gdb reaches it through the board's SSH: the gdb ports ride the one
  forward HM already keeps for the claimed board (local ends on 127.0.0.1; the board's
  telnet and Tcl stay on the board). No OpenOCD is needed on your PC. Every other board keeps
  this PC's OpenOCD over remote_bitbang, unchanged. HM asks the board once per session.
- **The setting `debug.on_board`**: `auto` (default: on the board when it can, else this PC),
  `true` (on the board, or exit 12 / 15 saying why), `false` (this PC only). Also
  `$HARNESS_MANAGER_DEBUG_ON_BOARD`. With `auto`, a PC without a suitable OpenOCD is no
  longer refused before the board opens: the board may run its own.
- **Two cores, two gdb ports.** nanosoc_multicore (on the board) gets one gdb port and one
  `attach` line per core. `debug status` says where OpenOCD runs; `--json` adds
  `gdb_commands` (one per core); `--tsv` appends `WHERE`, `GDB_PORTS`, `CORES`. The app's
  Debug card shows "OpenOCD: on the board" or "on this PC", and an Attach row per core.
- **Exit codes:** 4 when the board's JTAG is held (the message names who), when the lease is
  not yours, or when this PC's OpenOCD is turned away because the board's own holds JTAG
  ("use it (`debug status`), or stop it (`harness-manager debug down`)"); 12 no launcher
  with `debug.on_board = true`; 13 no debug port; 14 no config for the design; 15 not
  claimed here; 6 OpenOCD did not start (with the board's log tail); 8 already up.
- **Swaps:** a program stops the board's OpenOCD first and `debug status` says "closed for
  the swap" (also when the board's own watchdog stopped it); HM's session reopens after a
  verified swap. `debug down` also stops an on-board session HM did not start; closing the
  board stops only HM's own.
- **OpenOCD on the board stops before EVERY swap (FIX-PACK-7, DEBUG-DOWN-FIRST, agreed with the
  Linux lead).** Before any `program` or `restore` (the CLI, the app's Program and Restore
  baseline, Build's Add then Program) on a claimed Linux board this HM can enter, HM runs
  `mps3-debug down --json` over the claim's SSH first, whoever started that OpenOCD (another
  terminal, a restarted service, `mps3-debug up` by hand) and whatever `debug.on_board` says.
  Down (or already down) goes on; no launcher (exit 127) or no OpenOCD in the image (exit
  12) goes on. Anything else (the SSH did
  not answer, another exit, an answer HM cannot read) refuses the program with exit 15
  before anything touches the board: "`mps3-debug down` failed before the swap (…): OpenOCD
  on the board may still drive JTAG, so nothing was programmed", hint "retry, or add --force
  to swap anyway (on Linux v2.0.0 OpenOCD on the board may still drive JTAG during the
  reconfiguration)". `program --force` and `restore --force` (new; the API's `force: true`;
  the app's armed **Program anyway** / **Restore anyway**) swap with a warning instead. A
  board whose launcher lists the `harnessd-lock` capability (Linux v2.1) warns and goes on.
  Bare metal and unclaimed boards are unchanged.
- **The terminal holding `debug up` follows a swap:** when the swap reopens the session it
  prints the new gdb and `attach` lines (the ports change); when the swap was not verified it
  prints why the session was not reopened. `debug status` on the board path no longer prints
  this PC's `openocd … has remote_bitbang` line (it described this PC); `--json` keeps it.
- API (additive): `DebugStatus` adds `gdb_ports`, `cores`, `where`; `debug.state` adds
  `where` (docs/API.md "OpenOCD on the board"). docs/HIL_LINUX.md step E-OCD is the silicon
  proof (needs the launcher image).

### H1 findings (FIX-PACK-6)
From H1 on board 1 (MPS3 Linux v7, Thu 1 Oct, `docs/evidence/2026-10-01-h1/`) and the first look
at UI v2 on real boards.
- **`card` and `slot` changes work while the app has the board open.** `card clear` and `card
  commit` ran in the command's own process, so the service's board lock refused them ("… is in
  use — held by … harness-manager-daemon: harness-manager-ui", H1 Z1). With the service running,
  `card commit|clear` and `slot push|commit|verify|rollback` now go through it as its jobs, as
  `program`, `restore` and `mcc reboot` do; without one they run in-process as before. The
  command still asks first. New routes `POST /boards/{bid}/slots/{push,commit,verify}`
  (docs/API.md).
- **`board ssh TARGET -c CMD` sends CMD as one command**, as `ssh host 'CMD'` does, on Linux and
  Windows: a pipe, `;` or quotes inside it reach the board's shell intact. It was split into words
  (`'uptime;' '(logread' … '|' …`) and lost its quoting.
- **After a cold boot the reported design is checked against the DAP.** On Linux v2.0.0 harnessd
  reported nanosoc (`0x01000001`) after an MCC REBOOT while the greybox was resident (H1 r5-r8;
  fixed in Linux v2.1). After `mcc reboot` and `power cycle` on a Linux board whose reported
  design has a debug port, Harness Manager reads one IDCODE (as `debug detect`; nothing halts)
  and says `design verified` (0x6ba00477), `design UNVERIFIED: … greybox is probably resident
  (known issue, Linux v2.0.0)`, or `not cross-checked` with the reason (no OpenOCD here; after a
  power cycle the harness may not be up yet). It is in the result, `--json` (`design_check`), an
  Activity row, and the app marks the Design **unverified**; `GET /boards/{bid}` carries it
  until a deploy proves its design. It never fails the reboot and never changes the board.
- **The progress bar moves during a frame.** Push and card progress sat at 6 % for the whole
  2.3 MB partial, then jumped to 100 %: the push reports per frame. While a frame is in flight
  Harness Manager now estimates from the push's measured rate (at most every 0.5 s, never
  backwards, never past the frame's end) and snaps to the real bytes when the frame completes.
  Estimates are marked: `estimated: true` in `deploy.progress` and `job.progress`, `~` in the
  command's lines, a hatched fill with `~` in the app. Per-chunk reporting inside pyverify's
  pusher is the real fix (platform repo).
- **HIL D5 (docs/HIL_LINUX.md) for Linux v2.0.0:** after a cold boot with keep-on-card, expect
  `power-on failed:timeout` with the greybox resident (Linux KNOWN ISSUE 12: the power-on load
  cannot finish in 30 s at the card's ~14 KB/s; fixed in v2.1). The pass criterion is now the DAP
  cross-check (`design verified`, `debug detect` 0x6ba00477), not the reported rm_id.

### UI review fixes (FIX-PACK-4)
- **One lease rule.** On a board behind a hub, "yours" is this Harness Manager holding the
  lease (`here`), everywhere: the XVC card and the Update tab's lease line used `mine`, so a
  lease another session of your own hub name held (a soak, a runner) enabled XVC and said
  "installs are yours". The service's gates follow the same rule: XVC open and harness
  installs refuse a lease your hub name holds in another session, and say so. `GET
  /harness/catalog`'s `board.lease` gains `here` (additive).
- **Who may drive a hub board is the same on every tab.** Program, Restore baseline, Reset
  DUT, Reboot, Restart shell, Power-cycle, the DUT clock, OpenOCD Detect/Open session and
  XVC Open run only for the lease holder here; otherwise the button is off, no longer the
  blue (or red) one, and says why in one line, as Checks and XVC did. Program stayed blue
  while Needs attention said "must not drive the board".
- **The board preview says whose the hub lease is.** "Lock: free" was the service's own board
  lock and read "free" for a board alice held. It is now **This app's lock**, and a **Hub
  lease** row shows the lease as the service last knew it, with its time, and no hub read
  (`GET /boards` rows gain `lease_known`, additive).
- **The header's Harness says which version.** "release 1.1.0" (the catalogue's release the
  board runs, once Update > Harness versions has read its list), with "firmware 1.0.0" beside
  it when the firmware's number differs; before that, "firmware 1.0.0", with a tooltip.
  Update > Harness versions' Running line names the firmware's number when it differs, and
  Details and the preview say "Harness firmware".
- **Settings the app now reads:** `panel.identify_s` (the Front panel card's Identify time;
  its default is now 5 s, as every other Identify), `consoles.line_ending` (the send line's
  default ending) and `debug.hw_server_mode` (the XVC "Bring your own hw_server" default). A
  change applies in an open page at once. `panel.presence_who` is not read yet, so the dialog
  no longer offers it (`config` still lists it; docs/USER_GUIDE.md §11).
- **The tabs wrap** onto a second row in a narrow window: at 1024 px, Update, Checks and
  Activity were off-screen behind a hidden scrollbar.
- **Activity:** a failed job is one row with its reason (it was three: the action, the
  daemon's `job.failed`, and `update.failed` or `deploy.failed`); a job another client ran
  is one row too. A click the app refused (not armed, not the lease holder) is logged as an
  error, so a refused Program shows in **Errors**.
- **The header's refresh** re-reads the Card line and the SD journal too, not only the board.
- **Settings opens on General** (then on the section last used in that window); the Update
  tab's Settings button still opens Updates.
- Removed dead code: `sections/placeholders.js` and `AppVersionChip`.

### The partition boundary is timed (N2: Linux v2.0.0 known issue 11 fixed)
- `build_rm.tcl` writes the RM checkpoint straight after `synth_design`, before it reads the OOC
  XDC. Before, the checkpoint carried the OOC `create_clock -name dut_clk`, which at the link
  overwrote the static's clock of the same name on OSCCLK1 (`[Constraints 18-619]`): the shell's
  clk_wiz clocks lost their source and the static<->RM boundary was not timed (check_timing
  no_clock 27,984 for `minimal` on RC2, while the summary said every constraint was met).
- Measured on Vivado 2026.1 with the RC2 kit: no_clock 27,984 -> 0, unconstrained endpoints
  87,346 -> 423, the boundary paths timed against `clk_out1_shell_bd_clk_wiz_dut_0`, 18 fewer
  CRITICAL WARNINGs; every gate passes (docs/evidence/2026-09-30-kit-interactive §11). The
  `clocks_after_link` gate now counts 23 clocks (was 22). Rebuild an RM to get it.
- A synth checkpoint you bring (`build.synth_dcp`) must be written before any `create_clock`
  is read into it.

### Run the build in your own Vivado (KIT-INTERACTIVE)
- **`kit build DIR --gui`** prints the GUI command (`vivado -mode gui -source …/build_rm.tcl
  -log …/build_rm.log …`): the GUI stays open after the script, so `--stop-after link` leaves
  the linked design there to floorplan. `-mode tcl` does the same at a `Vivado%` prompt.
- `kit build` also prints the one line for a Vivado that is already open:
  `cd {DIR}; set argv {STOP_AFTER=link}; source build_rm.tcl`. It always sets `argv` (`{}`
  too): a session keeps the last one. `--json` gains `mode`, `commands` (`batch`, `gui`,
  `tcl`), `source_tcl` and `stays_open`; `command` is the one asked for (batch by default).
- **Floorplan with nested pblocks.** After `STOP_AFTER=link`, draw pblocks inside the
  partition's pblock, then type `hm_save_floorplan FILE` (a proc `build_rm.tcl` now defines)
  and give FILE as the design's `build.rm_xdc`: the next build reads it `-cell u_rp_dut`, as a
  child of `pblock_rp_dut`. `write_xdc -cell u_rp_dut` does not work for this: it writes the
  partition's own pblock too, which read back becomes a second top-level pblock that takes
  the RM's cells, and the build then fails at `place_design` (DRC PLDE-1). Nor do the lines
  the journal echoes: their full `u_rp_dut/…` names match nothing under `read_xdc -cell`, so
  the pblock stays empty. Proven on Vivado 2026.1 with the RC2 kit: a full build with a child
  pblock passed all 25 gates with every assigned cell placed inside it
  (docs/evidence/2026-09-30-kit-interactive).
- **`build.rm_xdc` crashed Vivado 2026.1.** `build_rm.tcl` read it with
  `read_xdc -cell $rp_cell`, a cell object taken before `read_checkpoint -cell`: Vivado
  segfaulted there (exit 139, `HASCUtils::getXDCName`; 2 of 2 batch links, with two
  different files). No build had set `rm_xdc` before. It now asks for the cell again
  (`read_xdc -cell [get_cells $rp]`).
- `build_rm.tcl` sourced into a session with no `argv` at all builds with its own values (it
  stopped with `can't read "argv"`). Batch prints the same markers as before.
- **A `STOP_AFTER` build is not a failure.** `kit check` of a `stopped` receipt says
  "stopped after link (STOP_AFTER=link), not a failure: the 14 gates up to there passed" and
  exits 0 (it said "failed 1 check", exit 15); `--json` has `state: stopped`,
  `stopped_after`, `passed: false`. `kit pack` still refuses it. The guide's Build step
  offers the command with `-tclargs STOP_AFTER=bitstream`, which runs a script written with
  `--stop-after link` to the end.
- `build_rm.tcl` prints `HM_STAGE <stage> <epoch seconds>` (was `HM_STAGE <stage>`), so a
  watcher can show how long the current stage has run. Every HM reader takes the stage from
  the first word; a log from before still reads. The only change to batch's markers.
- docs: USER_GUIDE 7.2 "Run it in your own Vivado" and "Floorplan"; DUT_BUILD_GUIDE §3.5-3.6.

### Findings from a clean-account run of the guide (FIX-PACK-3)
- **The installer's PATH advice reaches a login shell.** bash: add the line to `~/.bashrc`
  and to the file a login shell reads (`~/.bash_profile`, `~/.bash_login` or `~/.profile`,
  whichever exists), because ssh and `bash -l` never read `~/.bashrc`: a clean RHEL account
  followed the old advice and got `command not found`. tcsh/csh get
  `set path = ( $HOME/.local/bin $path )` in `~/.tcshrc` or `~/.cshrc` (the fallback,
  `~/.profile`, is one they never read); zsh names `~/.zprofile` too. docs/INSTALL.md
  "When harness-manager is not on PATH" lists every shell.
- `harness-manager daemon status` on a stopped service prints `env (the service is not
  running; its environment is shown while it runs)` instead of nothing (`--json`: `env_note`;
  `--tsv`: `ENV_NOTE` appended).
- docs/API.md: `GET /boards` lists the boards the service already knows (open, probed since
  it started, in `boards.toml`) and discovers nothing, so a fresh service answers `[]`;
  discovery is `POST /probe`. Behaviour unchanged.
- The build verdict: `build_rm.log` echoes the script, so `HM_RM_BUILD_FAILED gate=` stands
  in it after a real `HM_RM_BUILD_COMPLETE`. The README, `kit build`, the guide and the
  script now say the verdict is the receipt's state, or the last line that *starts* with
  `HM_RM_BUILD_` (`grep -E '^HM_RM_BUILD_' build_rm.log | tail -1`). HM's own reading was
  already anchored.
- `kit guide` and `kit fetch --out` suggest `--out ~/builds/<name>` (as the user guide), not
  the relative `build/<name>`; on Windows, the absolute path.
- Build times as measured: about 30 min for a small RM on a quiet machine, up to an hour when
  the machine is loaded, nanosoc about 50 min (was "about 20 min").
- A build with no timed path inside the partition (`minimal`) says so: the receipt gains
  `rm_timing_note` ("no timed path inside the partition; whole-design WNS … from
  <name>_timing.rpt"), `design_wns`, `design_whs` and `timing_rpt`, and the `rm_timing`
  gate's detail ends with the same words. `kit check` prints a `timing` line (for an older
  receipt it reads `<name>_timing.rpt` beside it). The gate itself is unchanged.

### Walkthrough findings (FIX-PACK-5)
From the guide's §6 walk on board 2 (Linux harness rc2_v7n, claimed, through the hub,
30 Sep): program nanosoc, console, debug up with a GDB halt, restore.
- **Reset the DUT from inside the console.** In `harness-manager console TARGET uart0`,
  Ctrl-] then r asks `reset the DUT of <board>? r or y resets it`; r or y resets it and the
  console stays open, so the boot shows; any other key cancels (nothing is sent to the
  board). After Ctrl-], any other key exits at once, and Ctrl-] alone exits after 2 s (a
  board with no reset adapter keeps the old instant exit). The banner says so. Why: a
  console holds the board's session lock for as long as it runs (one process owns a board),
  so `reset` from a second terminal exited 4, "your own `harness-manager console uart0` …
  holds it". 6900 was never the cause: HM opens it per call. With the service running
  before the console, both terminals share its session and `reset` works (now tested); the
  refusal's hint says both ways, and says Ctrl-] (not Ctrl-C) ends an interactive console.
- **`debug up` prints the gdb line** (`attach`, and `gdb_command` in `--json`, also on
  `debug status`): `arm-none-eabi-gdb -ex "set remotetimeout 60" -ex "target
  extended-remote 127.0.0.1:<port>"`. Through the claim's SSH forward gdb's 2 s default
  failed the attach; 60 worked. It is on the line for every route: it only bounds the wait
  for a reply. The app's Debug card copies the same line (Attach). `debug up` also says to
  run gdb in another terminal (it holds its own until Ctrl-C).
- **`restore` with no greybox says what to do.** The message names the shell, the overlay
  directories HM searched (or that none are set), the imported overlays for that shell, and
  a greybox it has for another shell. The hint: `restore TARGET --overlay-dir DIR` once, or
  `config set mps3.overlay_dirs DIR` for good, with the mint's overlay folder. A kit carries
  no overlays (the RC2 kit zip holds the static, boundary and XDC only; `kit pack` refuses
  rm_id 0), so `kit import` cannot register a greybox and does not try.
- Docs: docs/USER_GUIDE.md §5 (the console's reset), §6 (`program` asks [y/N] and a script
  needs `--yes`; `restore` needs the greybox), §8.1 (`debug up` holds its terminal, no
  `--background`, the app's Open session instead; the gdb line and why), §12.1 (`board
  claim`, `--adopt` too, asks: scripts add `--yes`); docs/HIL_LINUX.md B2, D2, E, E1, Z2.

### Checks: the HIL runbooks, unattended, from the app (HIL-GUI)
- A **Checks** section per board runs the lab runbooks' automatic checks overnight in the
  Harness Manager service: no `daemon stop`, no long `lease acquire --ttl`, no tmux. The
  plan is picked from the board (bare metal, the Linux harness with no card, a blank card, or
  a usable card) and can be overridden; writes read only or safe; run until a time (default
  the next 08:30) every 30 min; start now or at a time (18:00).
- The lease: yours is kept (heartbeated) until the run ends; a free one can be taken for the
  run and is given back at the end; someone else's turns Start off and names them. Nothing is
  ever forced.
- A live panel (iteration, check, counts, first failure, next start), **Stop** (finishes the
  check, puts greybox back, writes `REPORT.md`), past runs with each `REPORT.md`, and the
  17:30 announcement (`ANNOUNCE.txt`) with a Copy button.
- The run's commands go through the service, so the app having the board open is no longer
  "another holder"; a different process or client still stops the run. While a run is on,
  the board cannot be closed and `daemon stop` refuses (`--force` stops the run first).
- `python -m tools.hil run` hands the run to a running service and follows it (Ctrl-C is
  Stop); `--in-process` keeps the old way. The runner is now `harness_manager.checks` (in the
  wheel); `tools/hil` is its command line.
- API: `GET`/`POST`/`DELETE /boards/{bid}/checks`, `GET /boards/{bid}/checks/{run}/report`;
  events `checks.state`, `checks.progress` (docs/API.md, docs/CONTRACTS.md).

### Identify: which board is which (LOCATE)
- Every board card in the sidebar, and the Board tile, has an **Identify** icon. One click
  blinks the board's panel backlight for 5 seconds. While the harness owns the panel, the
  panel also shows "IDENTIFY: you@host via Harness Manager". The icon counts down, and a
  small square stops it early.
- It needs no hub lease, and the board does not have to be open. A second press while it
  runs sends nothing.
- The icon is disabled, with the reason as its tooltip, when the board does not report the
  harness feature `locate`. The Linux harness has it from images rc2_v7/v7n; bare metal
  (v0.11) does not (docs/design/BOARD_LOCATE.md).
- At most one Identify per board every 10 s (409 ALREADY / exit 8, saying when). When
  someone else holds the lease, the answer names them.
- `harness-manager identify TARGET` now blinks for 5 s by default (was 10).
- Aligned to the images as built (rc2_v7/v7n, net-protocol v0.16): the countdown follows the
  board's relative `until_ms` and ignores a value longer than the blink asked for; `who` may
  be up to 32 characters on the wire; a refusal of a bad request is a usage error, and a
  build without the panel says "locate not supported".

### Board identity: shipped contract (V7-ALIGN)
- `board identity --label` takes what the board takes: 1-19 of A-Z, 0-9 and - (was 23 of any
  printable). Host names may have dots; an IP needs a prefix of 8-30 and a usable address.
- `board identity TARGET --unset hostname` (API `unset: ["hostname"]`) drops one field of the
  board's own setting, so it follows the stage0 bake again.
- A refused change names the same reason the board would: the claim first, then no card,
  then a bad value.
- A board still on the image's default label `MPS3` shows "identity not set (default label)"
  and is never a clash with another board on the label alone; a shared MAC still is.

### pyverify from platform master 3f7cea2 (PYVERIFY-VENDOR)
- The vendored pyverify wheel (and the MPS3 OpenOCD configs) now come from platform master
  3f7cea2 (was 3bfda65): the board identity and locate codec (net-protocol v0.16), the slot
  codes, and a FakeShell that models them. Slot pushes and card commits keep Harness
  Manager's own budgets (the `mps3.slot.*` settings); the platform's new `slot push` defaults
  (600 s per chunk, 3600 s per job) change nothing here.
- A harness reboot (`slot rollback`, `reset shell`, the identity fix) refused by the board
  because a card job started after Harness Manager checked (another host's push) now says
  so, like the check itself ("harness reboot refused: slot B is being written ..."), not
  "another client holds the control port". Harness images from platform 53f49b4 on refuse
  their own reboot during a card job.
- A rollback refused with "no slot record" no longer says to wait for a board fix: images
  from platform 53f49b4 on stamp a slot's record once it boots and is confirmed.

### Front panel: the Linux harness's presence and panel as shipped (PANEL-V017)
- The Front panel card's text mirror reads the colour of every cell as the board sends it
  (net-protocol v0.17): one letter per role, in the order of `design/tokens.json`. It used
  to invert every `i` cell, and on the wire `i` is "ok". In today's look the banners are
  white on red; with the board's aligned look (`--panel-theme aligned`) the mirror shows the
  panel's own colours.
- The panel's status glyphs (tick, cross, warning, lock, person, question, dot) are drawn
  in the mirror as the panel draws them; `harness-manager panel mirror` prints a
  one-character stand-in instead of an invisible control character.
- A hello the board refuses is reported (presence's last error) and Harness Manager keeps
  the board's feature list; only a board that lacks the verb is read again.
- Row 0 of the panel keeps the board's own label (it tells boards apart); your Harness
  Manager shows in the `hm` row.

### Install
- `scripts/install.sh` (Linux, macOS) and `scripts/install.ps1` (Windows): one command
  makes a private venv, installs Harness Manager, and puts `harness-manager` on your PATH.
  It uses uv when uv is on PATH, else Python 3.10+ with venv and pip. Re-running it
  upgrades in place; `--uninstall` removes it and keeps your settings.
- Options: `--with-serial` (the Debug USB serial ports), `--with-app` (a native window
  through pywebview), `--from` a checkout, a wheel or a git URL.
- pyverify, the MPS3 shell codec, ships as a wheel in `vendor/`, with the platform commit
  it came from and its sha256 (`vendor/README.md`). It is not on PyPI.
- Tested dependency versions: `constraints.txt` pins every dependency for Python 3.10 and
  newer, and the installer uses it, so every install of a release gets the same
  packages. `--latest` takes the newest instead; `make lock` re-pins.
- Linux: **Harness Manager** in the desktop's application menu, with an
  **Open with Demo Boards** action (`--no-desktop` skips it). It is written on a server
  with no display too, for a later remote desktop such as ThinLinc.
- No network: `scripts/make_wheelhouse.sh` (or `make wheelhouse`) collects every wheel,
  and `install.sh --offline DIR` installs from it without the package index.
- The installer finds a Python 3.10+ beside an older `python3` (RHEL 8's 3.6, Rocky 9's
  3.9), and when there is none it lists what it found and prints the package to install
  for your distribution, or the uv one-liner. When a Python cannot make a venv, it
  shows the real cause, which venv hides, and names the fix: `python3.X-venv`, or on
  RHEL 8 `sudo dnf upgrade expat`, because python3.12 needs a newer expat than an
  un-updated system has.
- The installer is safe to run twice at once (the second stops and names the first),
  safe to interrupt (a re-run resumes), checks it can write before it starts, refuses
  `sudo`, keeps your extras and menu choice across upgrades, explains a missing network
  or proxy, and prints the full path of the command when `~/.local/bin` is not on PATH.
- `harness-manager daemon stop`, and so every upgrade, no longer fails inside a
  container. There, a stopped service stays a zombie, which still looked alive. It also
  never signals a process that has reused the service's old pid: it checks the pid is
  this state dir's harness-manager-daemon first.
- daemon.log is capped: over 8 MiB it moves to `daemon.log.1` (three kept), at
  `daemon start` and once a minute while the service runs.
- `daemon start` on a state directory it cannot write says so, with the next step
  (exit 6), instead of "internal error: PermissionError".
- `harness-manager app` with `--with-app` on Linux no longer prints two pywebview
  tracebacks: without GTK or Qt bindings it goes straight to the Chrome app window.
- CI installs from a checkout on Rocky Linux 8 and 9, Ubuntu 22.04 and 24.04, Debian 12
  and Fedora, with only each distribution's prerequisites, and once with no network.

### The app and the command
- `harness-manager app`: the web UI in its own window (pywebview, or a Chrome, Edge or
  Chromium app window). `harness-manager ui`: the same page in a browser tab.
  `--demo` on either shows scripted boards with no hardware.
- The demo shows every part of the app, offline: a Linux harness (the user microSD with
  its OS slots, "Keep on the card", the SSH claim, the front panel with its sessions and
  Identify, XVC), today's bare-metal v0.11 board (the rebuilt panel, XVC with its
  warning, the Debug USB pages), a board behind a hub whose lease someone else holds
  (the queue, your request, force-release) and a board on the same hub whose lease is
  free (take it, release it, close it). Harness versions lists a signed demo
  catalogue with every verdict, a history and a pin; the Build page has a kit for each
  demo static. `HARNESS_MANAGER_DEMO_UPDATE=staged` also shows the app-update banner (it
  is off by default, and the demo never applies an update).
- The app window starts without the desktop's D-Bus session bus, so it is no longer
  blank on ThinLinc. `HARNESS_MANAGER_APP_KEEP_DBUS=1` keeps the bus.
- With no display (an SSH session), `app` and `ui` print the URL and the `ssh -L`
  command to reach it.
- A per-user background service (harness-manager-daemon) owns the board sessions, so
  the command and the app share one board. It listens on 127.0.0.1 only, with a token.
- The command: `probe`, `info`, `attach`/`detach`, `telemetry`, `overlays`, `program`,
  `restore`, `console`, `debug`, `reset`, `clock`, `lab`, `mcc`, `sd`, `update`,
  `daemon`, `ui`, `app`, `help`. Every verb has `--json` and `--tsv` output and
  documented exit codes (`harness-manager help --tabs`).
- `harness-manager help --tabs`, and the app's Help dialog built from it, cover every
  verb: new sections for the front panel, hubs and leases, the Linux harness, building
  a DUT, updates, settings, and the app and service. Every option has help text, and
  `--via` says it takes `ssh:HOST` or `hub`.
- The settings (`harness-manager config`, the Settings menu) now control what
  environment variables alone did: OpenOCD, Vivado, hw_server and uv, the app window's
  browser, the update source and mirrors, the GitHub token (the secret store, between
  `$HARNESS_MANAGER_GITHUB_TOKEN` and `gh`), the kit hub archive, the debug and XVC port
  bases, and the MPS3 OpenOCD configs and overlay folders. A variable that is set still
  wins, as before. A change applies at the next use, except the port bases and the
  service's port, address and log level, which apply when the service restarts.
- `daemon start --log-level` reaches the service; without it, and without `--port` or
  `--listen`, the service uses its settings (`advanced.log_level`, `advanced.port`,
  `advanced.listen`). A `--state-dir` or `--demo` service reads its own `boards.toml` and
  settings, never yours.
- `boards.toml` takes a `[boards.defaults]` table: values every board gets unless its
  own table says otherwise (never a board's `name` or `match`, and never a meter, hub
  route or SYSMON table the board does not have).

### The MPS3 board pack
- Identify the harness and its health (idle, busy, wedged, offline, service down,
  rescue), with what to do for each.
- The capability view: every feature the board has, and for each one it lacks, what it
  needs ("needs the Debug USB cable", "needs harness firmware with 'stats'").
- Program the DUT partition with a preflight check against the shell, then confirm the
  load. Restore the baseline design.
- Programming a board that runs the Linux harness always uses TCP and waits up to 30 s
  for each part of the push. When you swap away from a design with a debug port
  (nanosoc), the harness takes the new design only after it has cleared the old one,
  and over TFTP it rejected the new design (found on the board, 25 Sep).
- After a failed push, the harness turns new connections away for up to 30 s while it
  finishes that swap. The board now shows as busy, with that reason, instead of offline.
- Keep on the card: `harness-manager program TARGET RM --keep-on-card`, or the
  **Keep on the card** box in the app's Program page, also writes the design to the
  board's user microSD, so the board boots into it next time. It is off by default: a
  plain program never writes the card. The box shows only when the harness has a
  microSD store (the Linux harness) and a card is in the slot. Without a store or a
  card, the program is refused before anything is written, with the reason. The
  report says "Kept on the card (slot B)" or why not; a card write that fails never
  fails the program.
- Keep on the card now waits as long as `card commit` does (KEEP-BUDGET): up to 900 s on a
  card that stops taking bytes (was 30 s) and the card's time for the pair's write and
  read-back (nanosoc: ~318 s; was 300 s). A card the board itself gave up on (its own 30 s
  idle limit) says so.
- DUT consoles (UART0, UART1, SWO) over Ethernet; the MCC and the FPGA UARTs over the
  Debug USB.
- Each console can also be a terminal device for `screen`
  (`/tmp/harness-manager-$USER/<board>/<console>`, `harness-manager pty`), shown in the
  app and in `screen` at the same time (Linux and macOS).
- Console rates: `harness-manager baud` and the app show each console's rate. Serial
  consoles change rate; Ethernet consoles report the loaded design's fixed rate (76800
  on nanosoc) and say why they cannot change it.
- A debug server for the DUT CPU (OpenOCD), for gdb and Arm DS. An OpenOCD built without
  the remote_bitbang adapter is refused before it starts, naming it and the adapters it
  has; with none configured, HM takes the first `openocd` on PATH that has remote_bitbang.
  `debug status` says which OpenOCD it would use.
- Reset the DUT, set the DUT clock, read temperatures and oscillators.
- The board controller (MCC) over the Debug USB: temperatures, oscillators, a reboot
  that proves the board came back, and allowlisted commands (destructive ones are
  refused).
- "Find boards on the network" is available on any Ethernet link where the board answers
  identify (UDP 6899). It needed a harness feature `identify` that no image lists, so it
  was unavailable everywhere. Through a hub it says "not through a hub" (UDP does not
  cross the tunnel).
- A hub's Test connection checks the group the way Harness Manager uses it, `sg fpga -c
  true`. On the lab hub `id -Gn` misses `fpga` (a stale group cache) while `sg fpga` works:
  that is now a pass with a note, not a failure.
- `xvc status` (and Debug > Fabric) says what the board reports now: a board found by UDP
  identify, or read while another client held its control port, no longer says "needs
  harness firmware with 'xvc_dbgbr'" while the board reports it.
- `harness-manager daemon status`, Settings → Advanced and a warning line in the app show
  the tool variables the service started with (`HARNESS_MANAGER_*`). The service keeps
  the environment of the shell that started it, so a `HARNESS_MANAGER_OPENOCD` from an old
  terminal could override your OpenOCD setting unseen; the app now says so, and names the
  way out (stop the service, start it from a clean shell).
- Ready for net-protocol v0.18 (mint 4): a harness that lists `mccif` (in-fabric SCC) or
  `mcc_local` (USB loopback) offers board reboot and oscillators without the Debug USB,
  gated on the route its `mcc status` reports. Rebooting and oscillators over the harness
  say "pending v0.18" until Harness Manager drives them; the Debug USB and the hub work as
  before.
- The MCC temperature says what it measures: "the SLR0 die diode via U53 (MPS3 schematic
  p4); not ambient" (was "sensor identity unverified").
- A reboot of a Linux board waits up to 300 s by default (was 180 s): with the Linux
  lead's stage0 DDR settle, a cold MCC boot answers after ~190 s. Bare metal keeps 120 s;
  `--wait` still overrides both.
- The configuration SD: backup, install and restore. A backup comes first, `.ebf` files
  are never written, and an interrupted install can be recovered.
- Board power from a networked plug (Shelly, Tasmota, NETIO) or an INA260, set in
  `boards.toml`: `harness-manager power show`, and a cold `power cycle`.
- Signed updates of the harness and the app (`harness-manager update`). The channel is
  not live yet: it refuses every release until the release keys are made.
- Settings > Updates says when the service checks next ("next check at 14:05, then every
  6 h") and, when a check found an update, its release notes (collapsed; a one-line
  summary when the release has none). `GET /update/app` gains `next_check` and
  `last_check.notes`.
- Harness versions: `harness-manager harness list|show|fetch|install|pin|unpin|history|
  rollback|mirror` (and the `/harness` API) lists every release of the board's harness
  catalogue with a verdict for the board (fits, re-key, needs Debug USB or hub,
  incompatible) and what it would change, installs a chosen version, pins a board to a
  release, keeps each board's last 20 installs, and rolls back to the release the last
  install replaced. A board behind a hub is installed on only by the lease holder.
  `update` still works as before.
- Boards behind a lab hub: `via = "ssh:HOST"` in `boards.toml`, or `--via ssh:HOST`,
  reaches the board through one supervised SSH tunnel (consoles, programming, debug).
  The MCC and FPGA UART lanes work over the hub's serial shares. `harness-manager lease`
  and `share` manage the hub lease and shares (there is no `share stop`: it would stop
  every share on the board). The lease is heartbeated while the board is open.
- Lease text names the hub's physical board (`mps3_01`), with the fpgahub target it is
  leased as (`mps3_01_pl`) as a detail. This covers the lease badge, the Release and Close
  dialogs, lease messages, and `lease show|acquire|release` ("mps3-01 (mps3_01 on HUB,
  target mps3_01_pl)"). `mps3_01_pl` is not deprecated. HM still leases on the target,
  the same name pyverify uses, so HM and the platform's scripts share one lease and one
  queue (docs/HUB_MODE.md, "Boards and targets"). JSON adds `board` beside `target` in
  `GET /lease`, the acquire, request and release results, and `lease.state`. The `lease`
  TSV appends a BOARD column. The board comes from `hub.board` in `boards.toml`, else the
  hub, asked once and kept. When the hub gives the target no board of its own, the text
  shows the target as before.
- DUT console input is paced (20 ms a byte on UART0/UART1), because the nanoSoC UART has
  no receive FIFO: a paste no longer arrives garbled.
- The Linux harness's OS slots: `harness-manager slot status|push|commit|rollback|verify`.
  A push goes into the slot that is neither running nor the default and is read back by
  the board; it boots only after `slot commit` and a reboot, and `slot rollback` puts the
  previous slot back. Once the board's SSH is claimed, the changes go through it. A harness
  update carrying an OS image installs over Ethernet only when the image is for the static
  the board runs; otherwise it needs the Debug USB or the hub. The downloaded image is
  checked against the frames its `linux_bundle.json` declared before it is sent.
- `slot status` says when the new slot failed to boot ("slot B failed to boot; A is
  running; roll back to make A the default"), and an update rolls back first. A booted
  slot reads "booted (not yet confirmed)" until the harness reports harnessd's confirm, and
  an update waits up to 30 s for that confirm when the harness reports it. A slot written
  outside Harness Manager (a card image, `dd`) is explained when its read-back finds no
  record.
- The board's user microSD: `harness-manager card status|commit|clear` shows what the board
  loads at power-on, makes the running overlay that default, or clears it. With no card the
  board boots exactly as it always has, and every change is refused. The Board tile shows a
  Card line.
- **MBBIOS is never changed by Harness Manager (FIX-PACK-7, platform item G8).** Every path
  that writes the config SD (`sd install`, `harness install` / `update harness` through the
  Debug USB, the A/B view or the hub door) keeps the target card's own `MBBIOS:` line in
  board.txt: "MBBIOS kept: <value>". A card with no MBBIOS line (or no board.txt) gets the
  bundle's line unchanged only when the `.ebf` it names is not on the card ("MBBIOS: <value>
  from the bundle (the card has no <file>, so the MCC will not update)"); with that `.ebf` on
  the card the write is refused (exit 15) before anything is written: "this card would make
  the MCC update itself to <file>: remove <file> from the card, or add --allow-mcc-update".
  `--allow-mcc-update` (`allow_mcc_update` in the API) writes it with a warning. A board.txt
  with the line removed is never written. Why: a bundle naming `mbb_v141.ebf` could make a
  third-party MCC that has that file update itself from 1.3.2 to 1.4.1, and Harness Manager is
  proven on 1.3.2 only. The note is printed, in `--json` (`notes`) and in the progress events.
- **No false "MCC firmware not tested" warning:** the board's `v1.3.2` now matches a release
  tested on `1.3.2`; a really different version still warns.

### The Live display (the LCD mirror)
- The service serves a live, pixel-exact copy of the board's 320x240 LCD to the lease
  holder only (`/api/v1/boards/{bid}/display`: a WebSocket, the status and a PNG or the raw
  RGB565 frame; docs/API.md "Live display"). Everyone else, a board without the Linux
  harness's `lcd_mirror`, and a bare-metal board get the reason, and the Front panel's text
  mirror as before. Nothing is shown before a whole picture has arrived, a browser tab that
  falls behind skips to the newest picture instead of queueing old ones, and touch is not
  passed through. The web page and the `display` command come next.
- The service no longer compresses its WebSocket messages (permessage-deflate): on the
  local machine it only cost CPU.
- The web page shows it: **Live display** at the top of the board's Front panel card
  (Details). The picture is exact, pixel for pixel, at 1x or 2x (whole screen pixels on any
  display), with Pause and a Snapshot (PNG). Tiles the panel has not drawn since a reset are
  hatched; the picture is greyed while the DUT owns the panel and the harness cannot see it,
  dimmed when the backlight or the display is off for a second or more, and badged when the
  glass may differ. "stale" and "reconnecting" show over the last picture. The picture is
  view only: a click does nothing to the board. It is open only while it is on screen: a
  hidden card or a background tab closes it. When it is refused, the text mirror stays, with
  the reason (and who holds the lease). `app --demo` shows it on the Linux demo board.
- The Live display follows the image the board runs now. On 2026-09-28 the board rebooted
  from an image with `lcd_mirror` into one without it, and the display kept what it knew
  and showed "connecting" for ever. Now a stream that closes before the board's first
  reply asks the board again, and an image without the engine ends the display with "the
  Live display needs a harness image with lcd_mirror (this image has none...)". A reboot,
  a power cycle, a new harness image or a changed SSH host key drops what the display knew
  and its SSH forward, and every identity read updates it. When the image names the engine
  but nothing listens on 6940, the display stops after 3 tries with ssh's reason.
- `display TARGET snapshot` waits up to 30 s for the first picture (it was 10 s), which
  covers a cold SSH forward through the hub: on silicon the first try timed out while the
  forward came up. In human mode it says "opening the SSH forward to the board's
  lcd_mirror..." while it waits, and a wait that runs out says the display is still
  opening and to try again. `GET .../display.png` (the page's Snapshot) waits as long.
- A board that can never show the Live display (the bare-metal harness, an image without
  `lcd_mirror`) now says so (422 UNAVAILABLE, exit 12) even when someone else holds its
  lease, instead of naming the holder (409 HELD, exit 4): taking the lease would not help.
  The lease comes next, then the claim. The web page and `display` answer alike.

- The Live display rides out one busy hub (PANEL-TRUTH, 2026-09-28). With the lease held
  and heartbeated, it said "cannot confirm you hold the lease on mps3_01_pl: lease show on
  mapstone-dev... failed: ... kex_exchange_identification: read: Connection reset by peer":
  the hub's ssh server had turned ONE connection away, and the display asked the hub afresh
  on every open. Now it takes the lease service's own view while it is under a minute old;
  a hub read that fails that way stands on the last time the hub said the lease is yours
  here (a view or a heartbeat, under 5 minutes old); else it says "checking your lease with
  the hub..." with a spinner, puts ssh's words behind **Details**, and asks again (2 s, 5 s,
  10 s, 20 s, then every 30 s). Someone else's lease is still refused, by name.

### The Front panel card says what the image reports (PANEL-TRUTH)
- The card named the Linux harness "bare metal" because its image lacked the optional
  features `panel`, `presence` and `locate`. Every word is now about what THIS image
  reports, by feature; the harness type comes only from the harness's own `version.impl`
  (`support.impl` in `GET .../panel`, additive), and only when it said.
- One headline: **Live**, **Read**, **Rebuilt** or **Not available**, with the reason in
  plain words. The Live display comes first, the rebuilt text is its fallback, labelled
  once. The card shows only the rows the image reports, and one line "Not reported by this
  image: page, touch health, who is connected, recent taps", with the features behind it.
  The Board tile's panel line leaves out a page the image does not report.
- The rebuilt text shows its `NET` as the board's own address, never `127.0.0.1` (the SSH
  tunnel's local end, on this computer), and a fact Harness Manager did not read as "—"
  with the legend "not reported by this image", never "?". Its `DUT` row follows a swap at
  once (every identity read updates it; it was up to 5 minutes behind).
- The reasons read "Identify isn't available on this harness image yet (harness feature
  'locate')" and "this harness image doesn't report who is connected (harness feature
  'presence')" in the app, `panel show`, `identify` and the API. `panel show` adds
  `harness` (from `version.impl`) and one `missing` line.
- `tools/gen_tokens.py` also generates `design/generated/clcd_glyphs.h`: the panel's
  extension glyphs 0x80-0x86 as 8x16 bitmaps, from the one glyph table the panel mock
  uses, for the Linux harness to vendor beside `clcd_palette.h`; `make check` fails when
  it drifts.

### The Linux harness's SSH claim
- `harness-manager board claim TARGET` claims an unclaimed Linux harness with your SSH key
  (the harness's TOFU claim), after asking, and only for the lease holder. It pins the
  board's host key in boards.toml `boards.<b>.ssh.host_key`; a changed key is refused.
  `--adopt` pins a claim made elsewhere; `--replace-host-key` is for a re-provisioned board.
- `board claim-status` and `board ssh` (root on the board, through the hub, with the pinned
  key). `info`, `GET /boards/{bid}` and the Board tile show the claim (`claim`); bare-metal
  boards are unchanged.
- The harness's claim lock (`slot locked: board claimed (use ssh)`) reads "this board is
  claimed by <key>; this operation needs the claiming key". The fabric identity lock
  (`identity lock: ...`) is a different lock: it now says whether the image and the FPGA's
  static disagree or one cannot be read, and how to fix it over Ethernet (push the right
  image, commit, reboot); it exits 15, no longer 14.
- A board whose host key changes BACK to one Harness Manager pinned for it before says so
  plainly: "host key changed back to one seen on <date>; on the Linux harness this is usually
  /persist (the user microSD) mounting or not; re-pin with `board claim --adopt` if you trust
  it" (claim status, `board claim-status`, the Board tile, SSH, forwards, the Live display).
  `board claim --adopt` re-pins such a key without `--replace-host-key`. A key never pinned
  here keeps the loud warning. Nothing is accepted automatically.
- The claim through a hub runs its helper with the hub's `python3.11` first and refuses
  clearly when the hub has no Python 3.8+. When SSH refuses the key right after a claim the
  board accepted, the hint says why (the board's key sync), not "wait".
- **A claimed board: debug, XVC, slots and the card go over SSH to the board.** A claimed
  Linux harness serves JTAG (6921), XVC (2542), the slot push and `slot commit`/`rollback`,
  and `card clear`/`card commit`/Keep on the card to the board itself only, so through the
  hub they were refused. On a board you claimed (or `--adopt`ed) from this Harness Manager
  they now all go through one SSH forward to the board's loopback (`ssh -J HUB root@BOARD
  -L …`, the pinned key), opened when one of them needs it, shared, and closed when the last
  is done and with the board. A board claimed by another key is refused before anything is
  sent (exit 15, with `board claim TARGET --adopt`). The board's one-line refusal (`… locked:
  board claimed (use ssh)`) reads as that claim error (exit 15), never "connection closed"
  or "held by another client". Bare metal and unclaimed boards are unchanged.
- XVC on an **unclaimed** Linux board goes through the hub tunnel (or the LAN), like bare
  metal: it has no lock, and its SSH has no key to log in with yet, so the board-SSH reach
  failed there. `xvc.reach = "auto"` takes board SSH only on a board claimed from here;
  `xvc.reach = "board-ssh"` still forces it. `xvc status` says which, and why.

### A good citizen on a shared board (QUIET-POLL)
- Harness Manager no longer contacts a board in the background unless a window shows it.
  On 2026-09-27 a background poll from an idle Harness Manager took the lab MPS3's
  single-client control port and reset a soak's control call. Presence hellos and the
  page's own refreshes (Overview, panel, telemetry, card, console rates) now run only
  while the app or a tab has the board selected and on screen. They stop when the tab
  closes or looks elsewhere (docs/USER_GUIDE.md "HM on a shared board").
- While the board's hub lease is someone else's, background contact stops entirely. The
  header shows "Paused: lease held by `<who>`", and an explicit read (`info`,
  **Read now**) still works and names the holder.
- A background connection the board refuses, resets or lets time out is taken to mean
  "another client is using it". Harness Manager backs off from 30 s up to 10 min and
  shows "Busy (another client)", never a red error. Your own clicks and commands keep
  today's behaviour.
- Consoles on a board whose lease is someone else's: a live console stays connected until
  it drops, then waits ("paused: lease held by `<who>`") and reconnects once the lease is
  yours or free. A new console there is refused, naming the holder. A board with no hub
  is unchanged.
- With background reads off (`general.background_poll = off`, or a board's `poll = off`)
  or paused (the lease is someone else's, the board is busy), the Front panel card no
  longer spins on "Reading the panel..." for ever, and neither do the Board tile's panel,
  card, temperature and clock lines or the Telemetry card. Each says why ("Background
  reads are off") and has **Read now**, which reads once, explicitly.
- Harness Manager is gentler on the hub's sshd. On 2026-09-28 the hub reset `lease show`
  and the request watcher mid-handshake ("kex_exchange_identification: read: Connection
  reset by peer"): its sshd throttles new connections (MaxStartups), and Harness Manager
  opened 3 to 12 one-shot ssh connections per hub, none reused. Now at most 4 one-shot hub
  commands run at once per hub (the rest wait their turn), views of one hub's lease that
  are in flight at the same time share one hub read, a tunnel's restart wait gets a random
  extra of up to half its step, and a connection the hub resets before authentication
  (nothing ran) is tried once more. A command that started on the hub is never run again.
  SSH connection sharing (ControlMaster) is not used: it needs a check on the hub first.
- Background telemetry that finds another reader on the MCC console backs off like a
  refused connection ("Busy (another client)").
- A hub MCC read or REBOOT refused for another reader of `tty_00` says what the hub saw: a
  process that "has the MCC console open", or one that "names the MCC console on its
  command line, so it may open it at any moment" (it is still refused: the hub cannot show
  another account's open files).
- New setting `general.background_poll` (`on-view`, the default, or `off`), and
  `poll = "off"` per board in `boards.toml`. With `off`, Harness Manager touches the board
  only when you ask.
- API (additive): `PUT`/`DELETE /boards/{bid}/viewers/{vid}`, `GET
  /boards/{bid}/background`, the `X-HM-Background` request header, and a health note on
  `GET /boards/{bid}` naming the lease holder (docs/API.md "Background reads").
- Harness Manager no longer turns itself away from a board (SERIAL-6900). On 2026-09-28 the
  lab MPS3 refused Harness Manager's own connections: the page's refresh and a CLI command
  opened the single-client control port at the same moment, and a connection opened right
  after one closed met the old one before the board had let it go (the board reaps a
  closed client only on its next pass). `program --keep-on-card` was refused at its reset
  guard every time, and each refusal counted as "another client". Now each board's control
  port is used by one request at a time in each Harness Manager process, in arrival order;
  a request turned away unanswered right after one of ours closed is tried again for up to
  1 s; waiting for our own request (a swap holds the port for its whole run) says so after
  10 s and never counts as another client. Only a refusal that outlasts that is someone
  else, and backs off as before.
- A read that meets another client's hold of the control port now always answers 409
  HELD ("another client probably holds the control port"). About one read in three
  answered 502 UNREACHABLE instead, when the board's reset landed while the connect itself
  was still finishing; a deploy's swap connection failed the same way. That reset is now
  a turn-away like any other, retried as our own ghost right after our own close. A
  connect nothing listens for is still UNREACHABLE.
- The service answers two identical board reads in flight with one read of the board.
- `slot status` and `card status` go through the running service like every other read
  (they were refused by its lock). `--overlay-dir` while the service holds the board says
  how to give it the directory instead, and `mps3.overlay_dirs` now applies to a board that
  is already open, at its next listing.

### The lease right after you take it (LEASE-FRESH)
- Acquire, Release and the heartbeat's own answers are the lease state: the rail card, the
  header chip, the Background line and the Overview show "Yours" (or "no lease") at once,
  without waiting for the next `lease show`.
- One hub read that fails (the lab hub's sshd resets connections under load) no longer turns
  a lease you just took into "Lease unknown" / "Needs attention". The page keeps the state
  it last knew, with a quiet note ("last confirmed 21:06:31; the hub didn't answer the last
  read, reading again"). It says "lease unknown" only after 3 failed reads in a row, once
  the known expiry has passed, or when nothing is known. A board last seen free is still
  treated as unknown for background reads.
- A hub command that ssh turned away before it started ("Connection reset by peer" on
  connect, or in the key exchange) is tried again, up to 3 attempts. A read-only command is
  also tried again when the connection dropped mid-way. Acquire, release and other writes
  that may have run are never repeated.
- The service shares one ssh connection per hub (OpenSSH multiplexing) instead of opening a
  new one, with a new key exchange, for every hub call: with one board open that was about
  8 connections a minute, which is what made the lab hub reset connections. Off on Windows
  and with `HARNESS_MANAGER_HUB_SSH_MUX=0`; the service closes its own shared connections
  when it stops (docs/HUB_MODE.md "SSH to the hub").

### The app's pages
- A simpler Overview: four tiles (Design, Consoles, Debug, Board), a "Needs attention"
  line only when something is wrong, and the details folded away.
- Consoles: "Attach with screen" gives the command to copy, the rate and why it is
  fixed, a rate selector where it can change, and Export to TCP.
- Power, Update, Clocks and SD card pages; a lease and tunnel chip for boards behind a
  hub.
- Settings (the gear in the rail): one dialog with sections for General, Hubs, Boards,
  Tools, Updates, Harness & kits, Debug, Consoles and Advanced. Each row says where its
  value comes from (default, yours, lab default, admin, or an environment variable that
  overrides it), saves when you change it, and has Reset. A setting the admin policy
  locks is disabled and names the policy file. Secrets (the GitHub token, a hub's token)
  show only whether they are set and where, never the value. Hubs: add an SSH or REST
  hub, Test connection step by step (config, reach, auth, group, targets) with the fix
  for the step that failed, Add this board from what the hub offers, and "Make this a
  hub" for a hub written inline in `boards.toml`. Tools: Detect finds OpenOCD, Vivado,
  hw_server and uv and runs only their version probe. A change that needs the service
  restarted shows a banner until it is; one that applies at the next board open offers
  Reopen board.
- Settings, on today's settings (SET-UI-MERGE): Reopen board reopens only the boards a
  change is about (a hub's row: the boards that use that hub). The restart banner names
  the `daemon start` flag (`--port`, `--listen`, `--log-level`) that would win over the
  setting you changed. "Show developer settings" lists the developer seams read-only, each
  with the variable that sets it. The OS-slot card timing (`mps3.slot.*`) shows under the
  MPS3 pack in Harness & kits, and an SSH hub has its SD stage directory. Numbers are
  checked against their range before they are sent ("must be more than 0"). An old
  `shares.mcc` entry in `boards.toml` shows as what it is, the MCC's path on the hub, never
  as a share; nothing offers a share on the MCC. A share on `tty_00` under any other name is
  refused, by `config set`, in the files and by "Make this a hub": tty_00 is the MCC
  console, which Harness Manager never shares.
- `harness-manager app --demo`: the Settings dialog shows the MPS3 pack's rows, writes the
  demo's own directory only, stores a secret in the demo's own files (never your keyring,
  so it cannot replace or remove your real token), and reaches no hub: Test connection
  and Add this board say so instead of running.
- Whose lease is it (LEASE-UI). Each open board behind a hub has a badge in the board
  list, an icon and words: **Yours** (this Harness Manager holds the lease), **Held by
  alice@lab-pc-07**, **Free**, and **Requested · #1** or **Queued** while you wait. A lease
  held under your own hub name by another session or a script (every lab session shares
  one fpgahub principal) says **Held by david@mapstone-dev (another session)**, never
  Yours, and offers no Release: only that session can. The board lock's chip (in the list
  and the header) now says **Open** instead of Yours. A board with no hub has no badge.
- **Release lease** is a real button wherever a lease you hold is shown: the header's Hub
  line, the Board tile's new "Hub lease" line, and the request bar once the board is
  yours. It always asks first ("Release mps3_01_pl? Others can take it; background checks
  pause."), naming who is next in the queue, and says why when a job on the board stops
  it. Force release stays a separate, red, typed confirm.
- **Close board** on a board whose lease this Harness Manager holds asks "Also release the
  lease on mps3_01_pl?": **Release and close**, **Keep the lease** (it stays yours until
  it expires, but nothing renews it while the board is closed) or **Cancel**. Any other
  board closes without asking. A release that fails leaves the board open and says why.
  The API: `DELETE /boards/{bid}?release=true` releases this Harness Manager's lease
  first and returns `released` (additive; without it, close is unchanged).
- The demo has a fourth board, mps3-03, behind the same hub with a free lease, so the
  badges show free, yours (Acquire it) and held by alice (mps3-02).
- **The board list, your way (SIDEBAR-UX).** Drag a board card to reorder the list (a
  line shows where it lands; on a touch screen, drag its grip), or focus it and press
  **Alt+Up** / **Alt+Down** (a screen reader hears the new place). A click without a drag
  still opens the board. The star on a card pins it in a **Favourites** group at the top;
  unstarring puts it back where it was. The order and the favourites are your settings
  (`general.board_order`, `general.favourite_boards`), shared by every window and the app,
  not kept per browser; the demo keeps its own. A new window starts on the first board.
- **Boards in `boards.toml` no longer vanish after the service restarts.** Every board
  your `boards.toml` configures is listed, as **not open** with its route ("through the
  hub mapstone-dev"), and nothing contacts it until you press **Open board**, which goes
  through its own `via` and hub. A board that is neither in `boards.toml` nor open is not
  listed after a restart, as before. The API: `GET /boards` rows add `source` (`open`,
  `probe` or `config`) and `configured` (the table's key, via, hub, target, match and
  name; additive).
- **Scan** also offers your `boards.toml` boards ("no boards answered on this network; 1
  more in boards.toml"), since a board behind a hub never answers a scan.
- **+ Add a board by address** uses the matching `boards.toml` entry's route: type
  `192.168.10.101` and the hub field fills with `hub`, with a line naming the entry
  (`boards.toml lab: through the hub mapstone-dev`). A route you type wins. Adding a board
  whose entry says `via = "hub"` now finds it: the probe goes through the hub's SSH host
  (it used to try a tunnel with no host and report "no boards answered").

### Building a DUT for the RC2 static (Vivado 2026.1)
- Vivado 2025.1 and later install as `<root>/<release>/Vivado/bin/vivado`; HM now finds that
  layout, searches `/research/CAD/Xilinx/Vivado` and the 2025.1 installer's `/tools/Xilinx`,
  and takes an install directory in `tools.vivado` / `HARNESS_MANAGER_VIVADO`. A path that
  names no vivado says which one you probably meant.
- With a kit that needs 2026.1, an installed 2026.1 wins over another release on PATH, and
  Settings → Tools → Detect prefers the cached kits' release and says when the `vivado` on
  PATH is another one.
- `kit build` and `kit script` print the full path of the Vivado of the kit's release (a bare
  `vivado` runs whatever your login profile put first); `kit build` fails (exit 12) when
  there is none. The guide's Tools step is not done while PATH's `vivado` is another release.
- `kit script --design minimal` builds its wrapper skeleton (no `-tclargs RM_SOURCES=` needed),
  and the skeleton ties `dut_lockup` and `irq_out` off. A design can name such outputs with
  `"use": {"status": {"tie": [...]}}`.
- A design's `build.generics` (`{NAME: value}`, or `{"path": FILE}` for a `$readmemh`
  image) passes top-level parameters to synthesis, one `-generic` each; a path is written
  absolute and a missing one stops the build at preflight (`generic_file_present`) instead
  of a blank ROM. `build.sources` refuses a `.hex`, `.xci`, `.xdc`, `.tcl` or `.dcp` and
  names the key that takes it. The `build` keys are in the user guide (§7).
- `kit script --design nanosoc` (the built-in, which names no RTL) no longer builds its
  skeleton: the skeleton leaves the used groups' outputs undriven, so the result would have
  been an empty RM under nanosoc's name and rm_id. RM_SOURCES stays empty and the warning
  names the undriven outputs; `minimal` still builds as its skeleton.
- `kit pack --import` says when Program will not list the import: an overlay of the same
  name, rm_id and static that comes first (an overlay dir, such as the fielded set, or an
  earlier import) wins, and `program TARGET NAME` loads that one. It names the other
  overlay, says whether its bits are identical, and gives the `--overlay-dir` to load this
  build. The web **Add to Program** result shows the same hint.
- The guide sees a build that is running: while `build_rm.log` has a stage and no verdict
  and was written in the last 30 minutes, the Build step says "a build is running here:
  stage link …" and offers no second Vivado (it would overwrite `out/`), and the last run's
  receipt is not shown as this build's. A log with no verdict that stopped long ago is
  named as a run that died.
- `kit check --static-id ID` on a receipt of another static refuses (exit 14) instead of
  ignoring the flag.
- The pin model describes more than one shell (`tools/gen_mps3_pins.py --shell`, `--all`),
  and now holds RC2 (`0x44EE76D5`, from its record at platform 6beea09) beside the fielded
  `0x72BB0A36`: `kit import`, `kit script` and the XDC kits work for RC2. RC2 is marked not
  fielded, and its RM kits name its own `dut_clk` buffer (`BUFGCE_X2Y47`).
- The `rm_timing` gate says why a slack is empty: an RM with no registers (such as
  `minimal`) has no path of its own, and a register with no timed path has no slack.
- The guide's Tools step names the licence the build needs, by the kit's release: "a device
  licence for xcku115 is needed from synthesis on: Vivado 2026.1 Core or higher (2024.1:
  Enterprise); the static's IP needs none". It stays *unchecked*: only synthesis can check
  it, and unchecked is never a pass.
- The guide launches the kit's Vivado once (`-mode batch` with a two-line Tcl, 60 s, no
  design). Vivado 2026.1 with no licence file exits 42, and the Tools step now fails with
  "Vivado 2026.1 did not start: no licence file (set XILINXD_LICENSE_FILE or
  LM_LICENSE_FILE)" and the `export` to copy, before you reach the build. Any other failed
  start shows its exit code. `vivado -version` could not catch this: it exits 0 with no
  licence. A start does not prove the KU115 licence, and the guide says so. The web page's
  Tools card has a **Starts** row.

### Unattended HIL on board 2 (`tools/hil`, HIL-B2)
- A `linux-nocard` plan for a board with no user microSD (board 2, `mps3_02_pl`,
  `192.168.11.101`): C1 expects the card-less answer (`slot status` exit 12, "no user
  microSD card in the slot"); C2, D2-D5, F6, §G, Z1 and Z2c are skipped ("no user
  microSD"); `--writes safe` keeps the two swaps and the MCC temperature read. No reset of
  any kind. `docs/HIL_LINUX.md` has a Card-less mode preface, and the drift test holds the
  plan's skips to it.
- `tools/hil/env_b2.sh` sets board 2's address, hub target, MCC tty and evidence folders;
  `docs/HIL_AUTO.md` has board 2's nightly recipe.
- HIL_LINUX.md F3 stages each board's OWN base image from a per-board table (board 1's
  re-bake `286ae54d…`, board 2's bake `f206f788…`) and refuses a bit whose sha256 is not
  the board's row. §F stays manual in every plan.

### Known limits
- Finding boards (was: "The board has a fixed address, 192.168.10.101, and there is no
  network discovery yet"; superseded by BOARD-ID and FIX-PACK-2). The lab has two boards, both
  through the hub: `mps3_01_pl` at 192.168.10.101 and `mps3_02_pl` at 192.168.11.101. Each
  board's label, IP and MAC are read and set with `board identity` (BOARD-ID; the board needs
  image rc2_v7/v7n), and two boards that share one are flagged. "Find boards on the network"
  needs an Ethernet link on the board's own network and an identify answer (UDP 6899). It
  never works through a hub or an SSH tunnel, so a lab board is added by its address or from
  the hub (`hub targets --add`).
- Windows and macOS run the unit tests and the installer in CI; they have not been used
  with a real board. `install.ps1` does not yet use `constraints.txt`, the wheelhouse or
  a Start-menu entry.
- nanosoc_multicore with OpenOCD on the board: only cpu1 can be debugged on Linux v2.0.0;
  the board's OpenOCD refuses cpu0 (core 0 is held in reset by the boot gate; fix in v2.1).
- nanosoc_multicore's boot ROM writes the DUT flash (one byte at 0x20000 onward, on every
  boot): don't load it on a board whose flash holds the MicroPython image (nanosoc_upy); fix
  in v2.1.
- MPS3 Rev B boards: a v2.0.0 config SD carries MB/HBI0309B and MB/HBI0309C, and Rev B
  "boots, untested" (only Rev C is tested). A Rev B board behind fpgahub cannot be installed
  through the hub door (the hub writes MB/HBI0309C's nanosoc.bit only): install it over its
  Debug USB. The A/B config-SD view (`updates.sd_ab`, off by default) is Rev C only and refuses
  a B+C release. Harness Manager warns and asks for MULTICORE, typed, before every program of it
  (FIX-PACK-8).
