# Harness Manager: build plan for parallel agent teams

**Owner:** the lead agent (Claude), accountable to david.

**Status (2026-09-23, late): Wave 2 is merged** (approved by david). Worktrees `../harness-manager-t12|t13|t14|t7|t9`:

| Team | Scope | State |
|---|---|---|
| T12 | Harness evolution: ILA v0.11 + Linux, identify, wedged/rescue | merged (9fc2004, CCRs 64ae3be) |
| T13 | `harness-manager-daemon` local service + RemoteEngine, per docs/API.md | merged (752d047, CCRs fdebfef) |
| T14 | Clean web UI replacing Qt: no-build ES modules, served by harness-manager-daemon | merged (8f1a7ee, CCRs 1daa928); Qt retired |
| T7 | Signed GitHub update channel, two-target bundles, app self-update | merged (7750171, CCRs e60d7f0) |
| T9 | Telemetry sources and power | merged (764c472, CCRs 5ae5937) |

`make check` on main: 1539 passed, 7 skipped (the Qt tests left with Qt). Try the UI with no board: `harness-manager ui --demo`.

**Wave 2 follow-ups (not yet assigned):**
- **harness-manager-daemon endpoints** for `session.power` (read, cycle) and `engine.update` (check, harness, app, rollback as jobs). The web UI greys both out until then.
- **T7-2:** move `OsSlotAdapter` into `core.pack` as `BoardSession.os_slots`, with a T12 pack hook. This waits on the Linux lead's FLOW_CONTRACT.md.
- **T7-5:** a public overlay-import hook on the pack (today `PackOverlayHandler` imports `harness_manager_<pack>.overlays` by name).
- **Trust keys:** `update/trust.py` `PINNED_KEYS` is empty, so every channel is refused. david creates the minisign keys (harness-release, root, optional app-ci).
- **Web UI gaps** (Qt had none of these either): SD install page, Clocks, Board & XDC, lab verbs, an update page, a power-cycle button.
- **T14-2 (declined):** a wedged harness keeps failing `GET /boards/{bid}` with its code; revisit only if the stale view proves confusing on a real board.

T8 (hub mode) and T10 (XDC export) follow.

## Week plan: a working Harness Manager by Fri 09-25 (drawn up 2026-09-23 23:00; awaiting david's go)

**Friday acceptance ("working"):** on srv03335, `harness-manager app` shows the lab MPS3 through the hub, with:
1. the simplified Overview;
2. every console in the GUI AND attachable with `screen <path>` at the same time, with the baud set from the GUI;
3. MCC temperature and board-controller access over a hub share;
4. debug `detect`;
5. and, if david grants a write slot, programming a partition plus a debug session.

It installs from `git@github.com:SoC-Labs/HarnessManager.git` (private) with one command.

**Facts that shape it:**
- **Baud on UART0/1 over Ethernet is fixed in the loaded design.** `rp_nanosoc_wrapper.sv`: `UART_BAUD = 76800` sets the `uart_axis_shim` divider at build time. No register or verb changes it at run time, so a GUI setting needs a hardware change.
- **Consoles on real serial ports can change baud from the app today:** the FPGA UART lanes on the Debug USB, the MCC, and hub shares.
- **76800 is not a Linux termios speed,** so `screen`'s baud argument cannot carry it. For Ethernet consoles the GUI is the baud control, and the PTY just carries bytes.

### Step 0 (lead, first, about 1 h): rename, then freeze the new contracts

**Rename `harness-manager` to Harness Manager everywhere:**

| What | New name |
|---|---|
| Command | `harness-manager` |
| Distribution | `harness-manager` |
| Imports | `harness_manager`, `harness_manager_mps3` |
| Board-pack entry point | `harness_manager.boards` |
| Environment variables | `HARNESS_MANAGER_*` |
| State directory | `~/.config/harness-manager` |
| Daemon (in prose) | "the Harness Manager service" |
| API `service` field | `harness-manager` |
| Update-channel schema | `harness-manager-channel` |
| Pack distribution | `harness-manager-board-*` |

Nothing was published under the old name, so there are no compatibility shims.

**Then:**
- set `origin` to SoC-Labs/HarnessManager;
- freeze the API additions below in docs/API.md, so the UI lane can build against them while the backend lanes build them.

### Tonight: six lanes in parallel (worktrees from post-rename main)

| Lane | Scope | Owns | Est. |
|---|---|---|---|
| **L1 Reach** | SSH tunnel (`--via ssh:HOST`, boards.toml `via`); a `tcp://` serial scheme for fpgahub shares (the MCC on `tty_00`, FPGA UART lanes); leases through pyverify.lease (show, acquire, heartbeat, release) as a service; the July v0.7 FakeShell profile; docs/HIL_B0.md (slot commands, expected answers, evidence files). | `harness_manager_mps3/tunnel.py`, `transports/tcp_serial.py`, `services/lease.py`, `daemon/hub_api.py`, the fakes, the docs | 6 h |
| **L2 Consoles** | A PTY per console in the daemon, with a stable path `/tmp/harness-manager-$USER/<board>/<console>` for `screen <path>` (the GUI and screen share the broker); `ConsoleBroker.baud()/set_baud()`: serial backends reopen at the new rate, Ethernet consoles report the design's fixed rate and refuse changes with the reason unless the harness has the `uart_baud` feature; a standard baud set by screen is forwarded; `harness-manager pty TARGET NAME`. | `services/console.py`, `services/pty.py`, `daemon/consoles_api.py`, `client/` console parts, `cli/cmd_io.py` | 6 h |
| **L3 UI** (the T14 agent, resumed) | The simplified Overview (spec below); the console bar gets a baud selector and the screen command (the `console` CLI line goes); pages for update, power-cycle, SD install and Clocks; a lease chip. Builds against the mock and the frozen API; screenshots for david in the morning. | `web/**`, `tests/web/**`, `tests/fakes/t14_*` | 8 h |
| **L4 Service endpoints** | power (read, cycle as a job), update (check, harness, app, rollback as jobs), clocks (list, set), all behind the existing job/HELD rules. | `daemon/power_api.py`, `daemon/update_api.py`, `daemon/clocks_api.py` | 4 h |
| **L5 Release** | `pyproject` 0.1.0; an install script (uv/pipx plus the vendored pyverify wheel); GitHub Actions CI (lint, tests, headless browser tests); README quickstart for external MPS3 owners; the pywebview `app` extra; CHANGELOG. | `README.md`, `scripts/`, `.github/`, packaging | 4 h |
| **L6 HW baud** (optional, platform repo worktree, simulation only) | A runtime divisor in `uart_axis_shim`, set in-band from the shell over the existing host→DUT stream (no boundary change, so no re-mint; an overlay rebuild only), plus a `uart_baud` verb and feature, the pyverify codec, FakeShell, and a cocotb/VCS proof. Handover for the Linux lead's service modules. | platform `fpga/rp/nanosoc/uart_axis_shim.sv`, `firmware/uart_over_eth`, pyverify | overnight |

The lead wires each new router and hook into `app.py` and `pack.py` (lead-owned) at merge, so no two lanes edit the same file.

**Simplified Overview (L3 spec):**
1. **No duplication.** The page header already shows the board, shell, design, harness and health, so the Identity card goes.
2. **Four action tiles:**
   - **Design:** the name and rm_id, with "Program…";
   - **Consoles:** each with a state dot, "Open", and the screen path to copy;
   - **Debug:** state and gdb port, with "Start";
   - **Board:** temperature and DUT clock, with "Reset DUT" and "Reboot".
3. **A "Needs attention" strip,** shown only when something is wrong: build unchecked, harness not idle, the lease expiring. One line each.
4. **"Details", collapsed:** identity, counters, capabilities with their reasons, telemetry.

### Thursday 09-24
- **Morning:** david reviews the Overview screenshots and tries `screen` on the demo boards. The lead merges L1–L5 and runs the full suite.
- **Afternoon:**
  - a tunnel rehearsal against the virtual board;
  - install on srv03335 from the private repo;
  - fix-forward.
- **Board:** W1 (ILA mint fielding) runs that day, and Harness Manager stays off the board.

### Friday 09-25 (B0)
- **Slot 1:** read-only, 30 min, on whatever shell W1 leaves.
- **Slot 3:** July Linux, 10 min.
- **Optional slot 4:** the write proof, 15 min, if david grants it: program nanosoc, open uart0 via screen, debug detect, restore greybox.
- **Close:** fix-forward, tag v0.1.0, push.

### Decisions for david
1. **L6 hardware-baud lane tonight** (recommended). Simulation only; silicon needs an overlay rebuild and a board window next week.
2. **Slot 4 on Friday**, for the write proof.
3. **Push to SoC-Labs/HarnessManager** after the rename: the repo is private and empty.
4. **Licence:** can wait for the tag.

**Status (2026-09-23): Wave 1 is complete and merged.** `make check` gives 900 passed and 4 skipped (the real-OpenOCD tests; they pass with `HARNESS_MANAGER_TEST_REAL_OPENOCD` set). The GUI tests run offscreen.

| Team | Merged as | What landed |
|---|---|---|
| T1 | `1ebba72` | Engine, ContentStore, TelemetryService |
| T2 | `702dfb4` | Guarded deploy, MPS3 deploy adapter, overlay catalogue |
| T3 | `52b8528`, `693cc87` | MCC controller, config-SD storage, USB discovery, `serial://` |
| T4 | `c29aea6` | Console broker, OpenOCD session manager, MPS3 debug adapter |
| T5 | `7bd4163` | The full CLI |
| T6 | `2162eb2`, `c2bb67d` | GUI MVP (selection dialog + tabs) |

The lead's contract fixes after each merge are the `Apply T<n> contract change requests` commits.

**Background reading:**
- the 2026-09-23 feasibility report (its artifact was withdrawn; ask the lead for the local copy);
- the harness-side handover: `mps3-nanosoc-platform/docs/planning/BOARD_MANAGER_HARNESS_HANDOVER.md`.

**Next action:**
1. david opens a board window for **HIL R0/R1** (read-only: `harness-manager info`, the consoles, the MCC `HELP`/`CFG R`, and debug `detect`).
2. The lead opens the Wave 2 contract window (§4a).

---

## 1. What we can build on today's harness

The fielded shell is `0x3F1A560F` (harness 1.0.0). Every row marked "today" can be built **and proven on the real board with no firmware change or re-mint**. Rows marked "later" are built now against the virtual board, and switch on automatically when a board reports the feature (capability negotiation, §2).

| Feature | On today's harness? | Path | Team |
|---|---|---|---|
| Identify and health (`ping`, `version`, `diag`) | today | Ethernet 6900 | done (W0) |
| Program partitions (11 overlays, 3–7 s each) | today | 6900 `swap` + 6910 windowed push | T2 |
| DUT consoles UART0/UART1/SWO | today (6930 proven; 6931/6932 code only) | TCP 6930–6932 | T4 |
| DUT debug: OpenOCD, gdb, Arm DS "Generic GDB" | today | `remote_bitbang` → 6921 | T4 |
| DUT reset, DUT clock presets 25/50/100 MHz | today (reset `dut` only) | 6900 | T2/T5 |
| CLCD owner flip, vPHY link events, traffic generator, DUT frames | today | 6900 `display`/`link`/`macgen`/`dutrx` | T5 ("lab" verbs) |
| MCC console, board REBOOT (reload from SD) | today, with the Debug USB | FT4232H tty, ≥50 ms/char | T3 |
| MCC temperature, oscillator set-points | today, with the Debug USB | `CFG R TEMP/OSC` | T3/T9 |
| Config SD backup / install / restore | today, with the Debug USB | USB mass storage `V2M-MPS3` | T3/T7 |
| FPGA UART consoles (incl. the shell console, lane 2) | today, with the Debug USB | FT4232H | T4 |
| FPGA die temperature + VCCs (SYSMON) | today, **hub only** (JTAG cable) | `hw_server`/xsdb, IR 0xDE4 | T9 |
| Board power, cold power-cycle | with a purchase | networked metered plug (`SMART_POWER` link) | T9 |
| Network discovery | later: firmware A0 | UDP identify | T3 |
| Failed-swap visibility, uptime witness | later: firmware A1 `stats` | 6900 | T2 |
| Shell console over Ethernet | later: firmware A12 | TCP 6939 | T4 |
| Reboot / temperature / oscillators over Ethernet | later: J7 mod + firmware A13 `mcc` | 6900 | T3 |
| Persistent firmware/overlay updates over Ethernet | later: the Ethernet-only mint (D13) | 6900/6910 | T7 |
| Harness updates from GitHub | needs harness Lane G bundles | dist repo channel | T7 |
| XDC export | needs harness Lane C pin DB (fixture until then) | local | T10 |
| Hub mode | today, against fpgahub 0.3.0 | REST/SSE/WSS | T8 |

**Bottom line:** everything except network discovery can be built and demonstrated on the lab board today. Standalone users need the fixed IP `192.168.10.101` until firmware A0 lands.

## 2. Principles every team follows

1. **Contract first.**
   - Teams code against `harness_manager.core` (models, errors and exit codes, capabilities, events, the pack interface, the session lock).
   - Contracts change only through `docs/CONTRACTS.md` (§5).
2. **One codec.**
   - The MPS3 shell protocol is spoken only through `pyverify` (platform repo). If it lacks something, the fix goes to the harness agent, not into this repo.
3. **Virtual board first, hardware last.**
   - All development and CI run against `tests/fakes/VirtualMps3`: pyverify's `FakeShell` + `FakeMcc` + `FakeSdVolume`.
   - Each fake must model the real traps and cite where each came from:
     - the MCC's burst drop;
     - single-client ports;
     - 6900 parked during a swap;
     - the windowed-push deadlock;
     - `reset` accepting only `dut`.
   - HIL tests exist, but run only in a board window david opens.
4. **Firmware profiles, never assumptions.**
   - The virtual board pins behaviour to a real release: `FIELDED_3F1A560F`.
   - A new firmware feature gets a new profile plus tests for both profiles.
5. **Capability negotiation.**
   - The UI never guesses. A missing capability always carries its reason: "needs the Debug USB cable", or "needs harness firmware with 'stats'".
6. **CLI first; the GUI renders what the engine reports.**
   - Every GUI action is a CLI verb or an engine call.
   - The GUI never opens a socket to a board.
7. **Honest data.**
   - An unavailable reading is `Reading.unavailable(reason)`, never 0.
   - Three-state checks stay three-state: `unchecked` is not a pass.
8. **Safety rails are code, not docs.**
   - Destructive MCC commands are hard-denied.
   - The SD is always backed up before a write, and `.ebf` files are never written or deleted.
   - A re-key needs typed consent.
   - Nothing reports "installed" until the board itself confirms it.

## 3. Package map and ownership

| Path | Owner | Contents |
|---|---|---|
| `src/harness_manager/core/**` | **lead** | contracts (frozen per wave) |
| `tests/fakes/virtual_board.py`, `tests/conftest.py`, `pyproject.toml`, `Makefile`, `docs/CONTRACTS.md` | **lead** | shared fixtures and build |
| `src/harness_manager/engine.py`, `src/harness_manager/services/telemetry.py`, `src/harness_manager/services/store.py` | T1 | engine facade, telemetry aggregator, local content store |
| `src/harness_manager/services/deploy.py`, `src/harness_manager_mps3/deploy.py`, `src/harness_manager_mps3/overlays.py` | T2 | guarded deploy |
| `src/harness_manager/transports/direct.py`, `src/harness_manager_mps3/{mcc,sd,usb}.py`, `tests/fakes/fake_mcc.py`, `tests/fakes/fake_sd.py` | T3 | controller, storage, USB discovery |
| `src/harness_manager/services/{console,debug}.py`, `src/harness_manager_mps3/openocd.py`, `tests/fakes/stub_openocd.py` | T4 | consoles, debug sessions |
| `src/harness_manager/cli/**` | T5 | all verbs, output formats |
| ~~`src/harness-manager/gui/**`, `tests/gui/**`~~ | T6 | PySide6 app, retired at the T14 merge (`DemoEngine` is now `harness_manager/demo.py`) |
| `src/harness_manager/services/update/**`, `tests/fakes/fake_channel.py` | T7 | channel, verify, install, self-update |
| `src/harness_manager/transports/hub.py`, `src/harness_manager_fpgahub/**`, `tests/fakes/fake_hub.py` | T8 | hub mode |
| `src/harness_manager_mps3/telemetry.py`, `src/harness_manager/power/**` | T9 | telemetry sources, smart plugs |
| `src/harness_manager/services/xdc/**`, `src/harness_manager_mps3/pins/**` | T10 | XDC export |
| `packaging/**`, `.github/**`, `docs/user/**` | T11 | release, CI, user docs |

Each team's tests go in `tests/unit/test_<team>_*.py` and `tests/integration/test_<team>_*.py`, so pytest's default collection finds them. Every team may **read** everything; it **writes** only its own rows.

## 4. Waves and teams

### Wave 0: foundation (lead) — DONE

Delivered:
- the repo and pyproject;
- the `core` contracts;
- the MPS3 pack skeleton: identity, health, `reset dut`, console endpoints, and the debug config chosen by `rm_id`;
- `VirtualMps3` with the fielded profile;
- `FakeMcc`;
- the CLI `version`/`packs`/`info`/`probe`;
- the read-only HIL tier;
- `make check`.

### Wave 1: the engine, CLI and GUI MVP (6 teams in parallel, ~8–10 working days)

**T1 · Engine core services (4–6 d)**
- `harness_manager.engine.Engine`:
  - probe every pack;
  - open sessions under the `SessionLock`;
  - compute the capability view;
  - publish events.
- `services/store.py`: a content-addressed local store (sha256), with indexes keyed by `static_id`/`static_usercode` for overlays and by harness version for bundles.
- `services/telemetry.py`: aggregates readings with provenance and max age.
- Settings and config (`~/.config/harness-manager/`, `HARNESS_MANAGER_*` env).
- **Tests:**
  - lock contention between two engines;
  - store integrity (a corrupted blob is detected);
  - a stale reading is flagged.

**T2 · Guarded deploy (5–7 d)**
- Overlay import from `fpga/dfx/overlay/*/manifest.json`, the fielded dirs and, later, bundles.
- Pre-flight checks, all blocking:
  - session held;
  - `shell_id == static_id`;
  - CRC and length;
  - the clearing file matches its partial;
  - the clearing fits in 256 KiB;
  - transport auto-selected from `features` (windowed ⇒ tcp + windowed);
  - no other 6900 client.
- Deploy is `pyverify.swap.SwapOrchestrator` with progress events.
- One-step "restore greybox".
- `list_compatible()` returns each incompatible overlay with its reason.
- `rm_name` is resolved from manifests, replacing the scaffold's `KNOWN_DESIGNS`.
- **Tests** against `FakeShell`'s swap model:
  - success;
  - `static_id` mismatch ⇒ `INCOMPATIBLE` and the board untouched;
  - CRC failure;
  - the windowed-push deadlock avoided;
  - progress events are ordered.

**T3 · Board controller, storage and USB discovery (7–9 d)**
- `transports/direct.py`: sockets and pyserial, behind small interfaces so they can be faked.
- The MCC driver:
  - paced ≥50 ms per character;
  - an allowlist, with `FORMAT`/`DEL`/`EEPROM`/`USB_OFF`/`SHUTDOWN`/`REN`/`COPY`/`CAP`/`FILL` hard-denied;
  - menu handling;
  - parsers for `CFG R` and the boot log. Use `docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt` as a fixture.
- REBOOT with a "went down, came back" witness.
- USB discovery via pyserial `list_ports` (FT4232H `0403:6011`), then which interface is the MCC. The live log says tty_00 is MCC and tty_01 is FPGA lane 0.
- Pair USB boards with Ethernet shells.
- SD discovery by the `V2M-MPS3` label on Linux (udisks or mounts) and Windows (drive letters). Refuse the DAPLink volume.
- SD backup: a zip with sha256 manifest.
- SD install: journaled and atomic; never touches `.ebf`; a backup is a mandatory gate; the timeout is expected and never retried.
- SD restore.
- **Tests:** every trap in `FakeMcc`; install interrupted mid-write ⇒ recoverable; Windows path handling.

**T4 · Consoles and debug sessions (6–8 d)**
- `services/console.py`:
  - one owner per board port;
  - fan-out to N subscribers;
  - reconnect across swaps (6931/6932 drop on every swap);
  - re-export as a local TCP port or PTY, for external terminals.
- `services/debug.py`, an OpenOCD session manager:
  - probe half + target half (overrides before `-f`);
  - config chosen by `rm_id`;
  - gdb/telnet/tcl bound to localhost on a per-board port block;
  - up / down / status;
  - auto-close before a swap and reopen after `verified`;
  - orphan reaping;
  - holder attribution;
  - "Detect" = IDCODE only.
- A stub `openocd` for tests. Optionally, a real OpenOCD against `socket_harness`'s loopback rbb server.
- **Tests:** two subscribers see the same bytes; reconnect after a swap; the session closes on a swap event; port-in-use ⇒ `PORT_BOUND`.

**T5 · CLI (4–6 d)**
- The full verb set:
  - `probe`, `info`, `attach`/`detach`;
  - `overlays`, `program`, `restore`;
  - `console <name>`, `debug up|down|status|detect`;
  - `reset <target>`, `clock`;
  - `lab link|macgen|dutrx|display`;
  - `mcc temp|osc|reboot`, `sd backup|install|restore`;
  - `telemetry`;
  - `help --tabs`, which serves the GUI's help text.
- `--json`/`--tsv` with append-only columns; exit codes from `ExitCode` only.
- **Tests:** golden output (JSON schema, TSV column count, exit code) for every verb, success and failure. The CLI calls services, never pyverify directly.

**T6 · GUI MVP (8–12 d)**
- PySide6 app:
  - a System Selection dialog (board, shell, design, harness, holder, health, links);
  - a main window with tabs: System, Program, Consoles, Debug, Reset, Log. Clocks and Board&XDC are stubs.
- "Cannot: reason" rendering for unavailable capabilities.
- Every engine call runs on a worker thread; an event-bus → Qt-signal bridge.
- Panels described as data (ported from HAPS `PanelDesc`).
- **Tests:** pytest-qt offscreen (`QT_QPA_PLATFORM=offscreen`, `xvfb-run` fallback):
  - every button calls the right engine method (via a fake engine);
  - disabled states follow capabilities;
  - fits a 1280×800 window.
- Uses only T1's `Engine` interface. Until T1 lands, it codes against a fake engine built from the `core` contracts.


### 4a. Wave 1 → Wave 2 carry-over (the contract window at the start of Wave 2)

**Scenario coverage after Wave 1:**

| Scenario | Where it is covered |
|---|---|
| S1 | `tests/integration/test_info_virtual.py` |
| S2, S3 | `test_t2_deploy_virtual.py`, `test_t2_engine_deploy.py` |
| S4, S5 | `test_t4_console_virtual.py`, `test_t4_engine_swap.py` |
| S6 | `test_t3_hooks.py::test_s6_*` |
| S7 | `test_t3_sd.py` round trip |
| S8 | `scenario_s8_capabilities.py` |
| S9, S10 | Wave 2 (T7, T8) |

**Contract changes deferred from Wave 1, all agreed in principle:**
- `PreflightItem.identity` + a core `refusal()` rule, so front-ends stop importing the deploy service (T2 CCR 4, T5 CCR 3).
- `Mps3Pack(overlay_dirs=)` as a pack setting instead of an env var (T5 CCR 4).
- A `LabAdapter` protocol for `link`/`display`/`macgen`/`dutrx` (T5 CCR 5).
- Reboot evidence returned from `ControllerAdapter.reboot()` (T3 CCR 3; T7 needs it).
- Optional `DebugAdapter` extras (`openocd_post_config`, `describe`, `expected_idcode`) and the ConsoleBroker/DebugService extras T4 already implements (T4-2, T4-3).
- Links changing on an open board, i.e. hotplug (T6 CCR-3).

**Linux harness alignment (agreed with the MicroBlaze agent, 2026-09-23).** Linux becomes the product at mint 3, around 10-08 → 10-12.
- **The wire is unchanged:** the same firmware service modules run under `mps3-harnessd`.
- **New team T12 · Linux harness support**, Wave 2:
  - `BoardIdentity` carries `impl` (`version.impl`);
  - health states `wedged` (accepts, never replies) and `rescue` (pingable, TFTP only, no 6900);
  - a UDP identify (6899) probe;
  - an SSH link kind with the **claim** flow (TFTP `authorized_keys` while unclaimed, then pin the host key);
  - reboot-witness timeout 180 s on Linux;
  - a `linux` VirtualMps3 profile, once FakeShell grows it.
- **T7's harness bundle** follows the two-target manifest in the platform repo's `docs/planning/linux_lanes/FLOW_CONTRACT.md`. Base = MCC SD (bitstream + stage0). Ethernet-updatable = µSD slot image + overlays, written to the inactive slot, with rollback by stage0's boot counter.
- **Don't build:** bare-metal-only platform features, or a TCP shell console.

**New Wave 2 work these revealed:**
- **A local engine service (`harness-manager-daemon`, per user).** A board lock belongs to one process, so today:
  - `harness-manager attach` must stay in the foreground;
  - `debug up` cannot leave OpenOCD running after the command exits;
  - the GUI and the CLI cannot share one board.

  A small per-user daemon owning the engine fixes all three (T5 design question, T4-7). Its client API is the same shape as hub mode, so T8 builds it alongside.
- **A more realistic virtual board** (T4-5):
  - promote `SingleClientProxy` and `FakeJtagServer` into `VirtualMps3`;
  - model 6931/6932 dropping on a swap;
  - ask the harness agent to add one-client-per-port to FakeShell itself.
- **The demo engine** (`--fake`) shows a USB board with build check "OK". Real fielded boards report "unchecked", so make the demo match.
- **Unproven until real hardware or a real OS:**
  - Linux/Windows drive and port discovery;
  - the MCC's real REBOOT echo;
  - SD writes over the MCC's USB storage;
  - a real gdb attach through the board's 6921;
  - Windows as a whole.

### Wave 2: updates, hub, telemetry, XDC (~8–10 working days, after the Wave 1 merge)

**T7 · Update channel (8–10 d)**
- The `channel.json` v1 schema and minisign (Ed25519) verification, with pinned keys.
- A resumable downloader with sha256 checks.
- Bundle verification plus domain checks: part, `static_id`, USERID = `static_usercode`, CRC.
- The harness install flow: T3 SD install → REBOOT → confirm identity → mark installed or "written, not running".
- Overlay-only updates, which go to the store and never touch the SD.
- App self-update into side-by-side `uv` venvs.
- A fake channel server (local HTTP + test keys).

**T8 · Hub mode (6–8 d)**
- `transports/hub.py`: the fpgahub 0.3.0 REST client, SSE events, leases, and WSS TCP tunnels for local gdb/Vivado ports.
- `harness_manager_fpgahub`: the engine running hub-side as an fpgahub plugin.
- Contract tests against fpgahub's OpenAPI. Read the 0.3.0 tree, not the stale local checkout.
- A fake hub fixture.

**T9 · Telemetry sources (4–6 d)**
- MCC temperature and oscillators (via T3).
- SYSMON over JTAG via xsdb (IR `0xDE4`), with the VREFP/REF-bit caveat.
- STMPE811 temperature, once the firmware reports `touch_temp`.
- Smart-plug `SMART_POWER` drivers: Shelly Gen2, Tasmota, NETIO JSON.
- The INA260 via MCP2221A.
- Vivado power estimates, labelled as estimates.

**T10 · XDC export (8–12 d)**
- A generator over a board-pin model. Use a fixture until harness Lane C delivers `board_pins.yaml`.
- The RM kit: an OOC XDC by boundary group, a connectivity sheet, and pblock facts.
- The full-board three-file export, with conflict, bank, direction and clock-capable checks.

### Wave 3: release (~5–7 working days)

**T11 · Packaging, CI and user docs**
- Wheels.
- `uv` bootstrap installers for Linux and Windows.
- A GitHub Actions matrix (ubuntu + windows) running `make check`.
- Channel publishing, with harness Lane G.
- A user guide for external MPS3 owners: first install, Ethernet-only use, the recovery ladder.

**HIL campaign with david.** Tiers are in §6.

### Harness agent (parallel, platform repo)

The harness agent works through handover lanes B, A, G, C and D in its own repo. The app teams depend on:

| Harness item | Unblocks | Blocking? |
|---|---|---|
| B2 transport default | nothing (T2 auto-selects anyway) | no |
| B3/B4 JTAG retarget, config split, multicore config | T4 multicore debug | partly |
| A0 identify/DHCP, A1 `stats`, A12 shell console, A13 `mcc` | T3 discovery, T2 failed-swap view, T4 shell console over Ethernet, T3 Ethernet reboot | no: built against new FakeShell profiles |
| **FakeShell support for each new verb** | our tests for each "later" feature | **yes**. Rule: a new verb lands in firmware, pyverify **and FakeShell** together. |
| G1–G3 bundle contract + builder | T7 real bundles | T7 can start on a fixture |
| C1 `board_pins.yaml` | T10 real data | T10 can start on a fixture |

## 5. Parallelisation and integration

**Isolation.**
- Each team is one agent in its own **git worktree**, on branch `team/<id>-<slug>`, forked from `main` at the wave's start.
- Teams commit only on their branch and never push. The lead merges.

**Ownership.**
- A team writes only the paths it owns (§3).
- The lead checks every hand-back with `git diff --stat main...team/<id>`. A change outside the owned paths is rejected, not merged.

**Contracts.**
- `harness_manager.core` and the shared fakes are frozen for a wave.
- A team that needs a change writes a **contract change request** in its hand-back: what, why, and the smallest diff.
- The lead applies agreed requests to `main` at the next merge window. Blocking requests are applied mid-wave and all teams are told.

**Coding against a teammate's work.**
- Code against the interface in `docs/CONTRACTS.md`, and use a local fake in your own tests.
- Never import another team's private module.

**The gate.**
- A hand-back is accepted only if `make check` is green in the team's worktree, rebased on the latest `main`.
- It must include the new tests, each check with a negative twin.
- It must list anything left unproven.

**Merge order**, per wave, in dependency order:
- Wave 1: **T1 → T3 → T2 → T4 → T5 → T6**.
- Wave 2: **T9 → T7 → T8 → T10**.
- After each merge, the lead runs the full suite plus the scenario suite, and fixes integration breaks before the next merge.

**Integration scenarios** (`tests/integration/scenario_*.py`, owned by the lead, extended by teams):

| # | Scenario | Needs |
|---|---|---|
| S1 | probe + info | done |
| S2 | deploy an overlay; identity changes to the new `rm_id` | T1, T2 |
| S3 | deploy with the wrong `static_id` ⇒ `INCOMPATIBLE`, board untouched | T2 |
| S4 | two console subscribers; reconnect across a swap | T2, T4 |
| S5 | OpenOCD session up → swap → auto-close → reopen | T2, T4 |
| S6 | paced MCC REBOOT ⇒ board-down and board-up witness | T3 |
| S7 | SD backup → install → verify → rollback | T3 |
| S8 | Ethernet-only capability view; USB-only capability view | T1, T3 |
| S9 | channel check → verify → harness install via S7 | T7 |
| S10 | the same S2 via the fake hub | T8 |

**Checkpoints:**
- **I1** (end of Wave 1): S1–S8 green, GUI MVP on the virtual board, then **HIL R0/R1** with david.
- **I2** (end of Wave 2): S9–S10 green, then **HIL M1–M3**.
- **I3:** release candidate, then **HIL M4–M5**, pilot users.

## 6. Testing strategy

| Layer | Where | Runs | Rule |
|---|---|---|---|
| Unit | `tests/unit` | every `make check` | pure logic with fakes; every check has a negative twin |
| Contract | `tests/integration/test_*_contract.py` | every `make check` | the pack against `FakeShell` profiles; later, replays of recorded board transcripts |
| Scenario | `tests/integration/scenario_*.py` | every `make check` | end to end on `VirtualMps3`; event-driven waits, never `sleep` |
| CLI golden | `tests/integration/test_cli_*.py` | every `make check` | JSON schema, TSV columns, exit code |
| GUI | `tests/gui` | `make check` when PySide6 is present | offscreen; fake engine; fits-window check |
| HIL R0 | `tests/hil` | board window only | read-only: `info`, consoles read, MCC `HELP`/`CFG R` |
| HIL R1 | `tests/hil` | board window only | OpenOCD detect, IDCODE |
| HIL M1–M5 | `tests/hil` | board window + `HARNESS_MANAGER_HIL_MUTATE=1` | M1 deploy greybox↔led; M2 reset and clock; M3 MCC REBOOT; M4 SD backup; M5 install + rollback (david present) |

**Cross-cutting rules:**
- Tests pass on Linux and Windows. Use `pathlib` everywhere.
- Coverage target: 85% for `core` and `services`.
- No test touches `~/.config`; the `HARNESS_MANAGER_STATE_DIR` fixture handles that.
- No test reaches any host except `127.0.0.1`.

## 7. Milestones (indicative; board windows set the real pace)

| Milestone | Content | When |
|---|---|---|
| M0 | Scaffold | **done 2026-09-23** |
| M1 | CLI MVP on the virtual board (S1–S8) | end of Wave 1 (~2 weeks) |
| M2 | First real-board run, read-only | the first board window after M1 |
| M3 | GUI MVP + standalone installer (SD backup/install via USB) | end of Wave 1 / early Wave 2 |
| M4 | Updates from GitHub + hub mode | end of Wave 2 (~4 weeks) |
| M5 | v0.1 to pilot users (lab + one external MPS3 owner) | end of Wave 3 (~5–6 weeks) |

## 8. Team brief template (the lead fills one per team)

```
You are team T<n> (<name>) building SoC Labs Harness Manager.

Repo: /home/dam1n19/SoCLabs/harness-manager. You work in your own git worktree,
on branch team/t<n>-<slug>. Commit only there. Never push. Never touch hardware,
the hub, or the platform repo.

Read first: docs/TEAM_PLAN.md (§2 principles, §3 ownership, §6 testing),
docs/CONTRACTS.md, and src/harness_manager/core/**.

You own (write only these): <paths>
Implement: <deliverables from §4>
Consume (read-only interfaces): <contracts / other teams' interfaces>

Tests required: <list>, each check with a negative twin, all on the virtual board.

Done when:
- `make check` is green, rebased on main;
- your scenario(s) pass;
- the hand-back lists: files changed, tests added, anything unproven, and any
  contract change requests.
```

## 9. Risks

| Risk | Mitigation |
|---|---|
| Contract churn stalls parallel teams | Freeze per wave; change requests are batched; the lead owns `core`. |
| Fakes drift from the real firmware | Firmware profiles; the harness agent updates FakeShell with every verb; HIL checkpoints each wave; transcript replay tests. |
| GUI thread blocking (the HAPS lesson) | All engine calls on workers; a test fails if the UI thread blocks for more than 100 ms. |
| Windows differences (serial names, drive letters, no PTY) | CI runs on Windows from Wave 1. T4 re-exports consoles as TCP on Windows, not PTY. |
| The single-client board ports | Exactly one engine owns a board (session lock, or the hub lease); consoles are fanned out, never shared. |
| The pyverify distribution: the private repo, and the PyPI name clash | Ship pyverify as a wheel from the harness release (Lane G9); `mps3-pyverify` stays the dist name. |
| Board windows are scarce | Batch HIL tiers; everything else runs on the virtual board. |
