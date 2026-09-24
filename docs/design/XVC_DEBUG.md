# XVC in Harness Manager: design

**Lane:** XVC (design + one board-free spike). **Date:** 2026-09-24. **Base:** HM `main` 80bb849.
**For:** david, to decide §5. Nothing here is wired into the app.
**Spike code:** `tests/spikes/` (the host-side pieces are in `tests/spikes/xvc_spike_lib.py`, kept out of the shipped package). **Evidence:** `docs/assessment/xvc_spike_2026-09-24/`.

Sources are cited as `repo:path:line`. The prefixes mean:
- `plat` = mps3-nanosoc-platform;
- `ila` = platform branch `feat/rm-ila-mint`, which is what the fielded 0x72BB0A36 runs;
- `lx` = platform branch `feat/linux-harness` at 17c7ea1;
- `hm` = this repo at 80bb849.

---

## 0. Recommendation

**Treat XVC as a brokered, lease-gated debug session.** Model it on the OpenOCD session. Do not make it a raw port.

**What HM does:**
- **Reach.** HM reaches the harness's XVC server through the SSH path it already has. The hub tunnel already forwards 2542 today, and nothing uses it (`hm:src/harness_manager_mps3/tunnel.py:14,110-113`).
- **Relay.** A small one-client relay sits in front of that path. It lets HM name who is attached, and it frees the board's single XVC slot before every partition swap.
- **hw_server.** HM runs its own hw_server by default:
  - on a fixed port per board;
  - with `-p0`;
  - with no `-d` and no `-I`;
  - with the XVC cable only (lane XVC-UI): `auto-open-servers` names the one xilinx-xvc server (the default `*` opens every local USB cable type) and `jtag-port-filter Xilinx/XVC/127.0.0.1:<R>` hides any other cable a client opens later. Evidence: `docs/assessment/xvc_ui_2026-09-25/hw_server_cable_filter.txt`; unverified against a physical USB cable until the board window.

  After every swap HM restarts it. This replaces the 20 s linger wait (up to 40 s) with a restart that takes a few seconds.
- **Probes files.** HM serves the matching `.ltx` for the RM that is loaded (and, on Linux, the static MIG file). It also serves a ready-made Vivado Tcl snippet.

**What the Linux harness should do:** give XVC the same trust rule david chose for slots (S12).
- Once the board is claimed, harnessd accepts XVC from loopback peers only.
- HM then reaches it with `ssh -J hub root@board -L 127.0.0.1:<p>:127.0.0.1:2542`.

That makes the board key the authentication, and the hub's port-22 lease gate the lease. The bare-metal harness cannot authenticate XVC at all. HM keeps using the hub tunnel for it and says so in the UI.

**Board-free spike results:**
- HM's own `SshTunnel` carries XVC through a two-hop ProxyJump. On loopback that adds about 0.2 ms per round trip.
- A real Vivado 2024.1 hw_server that HM owns identifies the fake exactly as silicon did: `debug_bridge`, idcode `0a003093`, irlen 6.
- It releases the board's slot within about 1–5 s of its last client leaving, and within 50 ms of SIGTERM.
- It survives a 12 s swap-gate stall.

## 1. What the harness serves: exists, planned, missing

### 1.1 Bare-metal (fielded 0x72BB0A36, firmware v0.11 on the card)

| Question | Answer | Status | Source |
|---|---|---|---|
| Process, port | `xvc_server.c`, a superloop service, TCP 2542 | exists | `ila:firmware/xvc_server/xvc_server.c:154`, `ila:firmware/common/net_proto.h:26` |
| Target | the Debug Bridge (`debug_bridge_0` @0x44A8, XAPP1251 layout) by default; `XVC_TARGET=jtagbb` images drive `jtag_bb` instead. Reported as feature `xvc_dbgbr` or `xvc_jtagbb` | exists | `ila:xvc_server.c:1-35`, `ila:net_proto.h:154-156` |
| Bind address | `IP_ADDR_ANY` (lwIP) | exists | `ila:firmware/platform/src/net_if_lwip.c:329` |
| Auth | **none**: anyone who can reach 192.168.10.101 (the hub, anyone on the hub) can attach | exists (gap) | same |
| Clients | **one**. A second is accepted and closed at once, and the first is not disturbed | exists | `ila:xvc_server.c:516-527` |
| getinfo | `xvcServer_v1.0:2048\n`; a shift of up to 4×2048 bits is accepted | exists | `ila:xvc_server.c:456`, `xvc_server.h:270,305-309` |
| What hw_server sees | `debug_bridge_0`, PART `debug_bridge`, IDCODE 0x0A003093 (silicon, 09-23). Behind it, through `m0_bscan`, the RM's own hub when the loaded RM carries one: `dbg_demo` and `nanosoc_ila` on 0x72BB0A36 (B1–B5 PASS) | exists | `plat:docs/evidence/2026-09-w2/xvc_smoke_20260923.txt`; `ila:docs/evidence/2026-09-w3/README.md:28-32` |
| MIG hub | none: the bare-metal BD keeps one BSCAN master | exists | `lx:docs/planning/linux_lanes/SHELL_CONTRACT.md:340-342` |
| Across a swap | the firmware gate: `getinfo`/`settck` still answer, and `shift:` **stalls** until the clearing is DONE. The TCP connection survives, and the stalled shift then runs on the new RM | exists | `plat:docs/planning/HANDOVER_RM_ILA_OVER_XVC.md` F10; `ila:xvc_server.c:495-497` |
| Host side of a swap | close the target before the swap and reopen after on a **fresh** hw_server. Reusing the auto-launched hw_server (which lingers 20 s) gave "No devices detected" (B3 1/3 → 3/3) | pyverify on `ila`/`lx` only; the HM-vendored pyverify (b2b83d3) has no `XvcSession` | `lx:host/pyverify/pyverify/debug.py:271-431`, `lx:host/pyverify/pyverify/hwserver.py`; `ila:docs/evidence/2026-09-w3/README.md:119-123`; `hm:vendor/README.md` |
| Across a warm restart (`reboot` verb, WDOG) | lwIP state is gone, so the client's socket dies (reset or EOF through the tunnel). After a restart the RP stays parked in reset until the first swap (`rm_ok:false`), so an RM hub is not reachable until one swap | exists (inferred for XVC from the RP rule) | `lx:docs/planning/linux_lanes/FINDINGS_TRIAGE.md:23` (#11) |
| Reconnect race | the server accepts BEFORE it notices a close (`accept` is at the top of `xvc_server_poll`, the close is found in `recv`). A client that reconnects within one poll of closing is refused. The spike measured 15–17 of 20 immediate reconnects refused on its model (§8) | exists (**new finding**) | `ila:xvc_server.c:513-590` |

### 1.2 Linux harness (`mps3-harnessd`, cutover about 10-12)

| Question | Answer | Status | Source |
|---|---|---|---|
| Process, port | the same `xvc_server.c`, compiled unmodified into `mps3-harnessd` (service row 7), TCP 2542, UIO `dbgbr` @0x44A8. **No** kernel XVC driver, no `/dev/xvc*`. The legacy `mps3-xvcd` is off in the image | exists | `lx:docs/planning/linux_lanes/HARNESSD_CONTRACT.md:21-24,116`; `lx:src/linux_harness/sw/harnessd/main_linux.c:359`; `lx:src/linux_harness/sw/configs/mbv_harness_defconfig:121` |
| Bind address | `--bind-any` (default) or `--loopback`, **global to every port**. The inittab passes no flag, so 2542 is on 0.0.0.0 | exists | `lx:main_linux.c:547,619-652,731`; `lx:firmware/test/posix_net_if.c:136`; `lx:src/linux_harness/sw/br2_external/package/mps3-harnessd/50-mps3-harnessd.inittab:1` |
| Auth | none on 2542. Precedent for a fix: the **slot lock** (S12). Once the board is claimed, slot mutations from a non-127/8 peer are refused, through `mps3_net_conn_peer()` / `mps3_net_addr_is_local()` | exists (for slots only) | `lx:HARNESSD_CONTRACT.md:392-398`; `lx:docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md:394` |
| SSH | dropbear, key-only (`-s`), local port forwarding allowed (no `-j`). Access is `ssh -J mapstone-dev` (DL5) | exists | `lx:src/linux_harness/sw/br2_external/board/mps3_provision.sh:205`; `lx:LINUX_HARNESS_PLAN:122` |
| Discovery | `version.features` has `xvc_dbgbr` (coordinator.c is shared), and `identify.ports.xvc` = 2542 | exists | `lx:firmware/coordinator/coordinator.c:936-941`; `lx:firmware/identify/identify.c:205-209` |
| MIG hub (SEAM-8) | `debug_bridge_0` gets `C_NUM_BS_MASTER 2`, and `m1_bscan` feeds a static `mig_dbg_hub` on `shell_clk`. It is reachable **only over XVC, only while Linux + harnessd run**. A board whose DDR fails calibration never shows it | exists (BD); silicon unproven (B1 step h) | `lx:SHELL_CONTRACT.md:327-365`; commit d8b29e8 |
| Static probes file | `config_rm_greybox_static.ltx` + a `.ltx.json` sidecar, gated for mbv (exactly one hub + one DDR4 slave) and bound to the flashable base. Load it for the MIG view; load the RM's `.ltx` for ILAs; loading both at once needs a **full-design** `.ltx` of that config | exists (P-mint copy; gate in force for mint 3) | `lx:docs/planning/linux_lanes/FLOW_CONTRACT.md:459-533` |
| Across a swap | the same firmware gate (same code) | exists | `lx:FINDINGS_TRIAGE.md:33` (#21) |
| Across a harnessd respawn | the kernel closes the sockets, so the client sees EOF. inittab respawns harnessd within about 1 s. A WDOG reset is a full OS boot (budget 180 s) | exists | `lx:HARNESSD_CONTRACT.md` §5.3; `hm:src/harness_manager_mps3/constants.py:57-58` |
| Who is attached (query) | **missing**. No verb reports the XVC peer; `stats`/`diag` do not carry it | missing | — |
| Host side | `XvcSession` (close before a swap, reopen on a fresh hw_server) and `wait_stale_hw_server` (20 × 2 s) | exists | `lx:docs/planning/linux_lanes/HOST_CONTRACT.md:97,110-117` |
| Forward hygiene | no forward ever rides a ControlMaster: a lingering `-L` to the board's loopback would pass the claim lock (#20). HM's `SshTunnel` already passes `ControlPath=none`/`ControlMaster=no`/`ExitOnForwardFailure=yes` | exists | `lx:HOST_CONTRACT.md:104-109`; `hm:tunnel.py:90-100` |

### 1.3 Harness Manager today

| Piece | State | Source |
|---|---|---|
| Hub tunnel forwards 2542 to a random local port (never local 2542: another session on srv03335 holds it) | **exists, unused** | `hm:src/harness_manager_mps3/tunnel.py:14,88,110-113`; `hm:src/harness_manager_mps3/pack.py:331-334` |
| `GET /boards/{bid}/tunnel` shows the `xvc` forward | exists | `hm:docs/API.md` (L1 hub table) |
| Capability name `debug_fabric` | exists in core and in the UI's title table; **the MPS3 pack declares no route for it** | `hm:src/harness_manager/core/capabilities.py:28`; `hm:src/harness_manager/web/static/js/format.js:14`; `hm:src/harness_manager_mps3/capabilities.py` |
| Swap hooks (the model to copy) | `DebugService` closes OpenOCD on `deploy.started`, and reopens on `deploy.done{verified}` | `hm:src/harness_manager/services/debug.py:439-441,873-912` |
| `.ltx` | directory overlays reach it through pyverify's `Overlay.ltx_path()`. **Content-store overlays drop it** (`_StoredOverlay.ltx_path()` returns None), and `import_overlay` never stores it | `hm:src/harness_manager_mps3/overlays.py:103-106,355-379` |
| Static artefacts (mint dir, static `.ltx`) | **missing**: HM has no notion of a mint directory | — |
| hw_server management | **missing** (only `sysmon.py` talks to an existing hw_server, via xsdb) | `hm:src/harness_manager_mps3/sysmon.py:244-312` |
| XVC routes, CLI verbs, events, UI | **missing** | — |

## 2. Reach and security

XVC has no authentication. Whoever holds the TCP connection holds the JTAG chain, and with it the RM's ILAs and (on Linux) the MIG calibration core. With one client per board, a squatter also blocks everyone else.

| Path | Who can attach | Lease enforced? | Works on |
|---|---|---|---|
| **A. Direct LAN** 192.168.10.101:2542 | anything on the hub's board NIC | no (`gate_ethernet=false`, and the OUTPUT chain gates only port 22 even with the patch) | on the hub only |
| **B. Hub tunnel** `ssh -L …:192.168.10.101:2542 hub` (HM today) | any hub SSH user, and any local user of a laptop with a lingering forward (the srv03335 2542 forward is exactly this) | no | bare-metal and Linux |
| **C. Board SSH** `ssh -J hub root@board -L 127.0.0.1:p:127.0.0.1:2542` | holders of a key in the board's `authorized_keys` **and** a hub account | **yes**, once the fpgahub port-22 uid gate lands | Linux only |
| D. fpgahub share | — | — | not applicable: shares are TTYs, open on 0.0.0.0 unauthenticated (`hm:docs/HUB_MODE.md:141`) |

**Recommendation.**
1. **Linux:** path C, plus a board-side **XVC lock**: when claimed, 2542 refuses non-loopback peers, the same rule and helpers as S12 (a request to HARNESSD, §7).
   - The SSH key is the authentication.
   - The hub's `holders_uid_<board>` rule on TCP 22 makes the lease the gate (`lx:docs/planning/linux_lanes/FPGAHUB_PATCH_NOTE.md:140-205`).
   - The fpgahub repo has no MPS3 XVC user. Its one XVC plugin, `xvc_jtag_openocd`, targets a hub-side hw_server on the ZC702 (`fpgahub:src/fpgahub/debug_plugins/xvc_jtag_openocd.py:44-50`), and no config in the repo points it at an MPS3.
   - The patch note does list 2542 among the ports fpgahub uses (`lx:FPGAHUB_PATCH_NOTE.md:200`). Check the live hub config before turning the lock on.
   - An unclaimed board behaves as today.
2. **Bare-metal:** path B, as HM already does it. The UI states the exposure in one line: "XVC on this harness is unauthenticated: anyone who can reach the board network can attach."
3. **Every forward:** 127.0.0.1 only, no ControlMaster, with the port checked before and after (HM's `SshTunnel` already does this; finding #20).

**What the lease means for XVC:**
- For a board behind a hub, HM opens the XVC relay only while `lease.mine` is true.
- On `lease.state` `released|expired|lost`, HM kicks the attachment and closes the relay before anything else.
- A direct board (no hub) has no lease. The session lock (one HM per board) is the gate.
- HM never probes or attaches for a user who does not hold the lease: the probe itself takes the board's slot for a moment.

## 3. What Harness Manager does

### 3.1 Capability and discovery

The MPS3 pack declares `debug_fabric` ("Debug the fabric (Vivado ILAs over XVC)"):
- `via(ETHERNET, features=("xvc_dbgbr",))` and `via(HUB, features=("xvc_dbgbr",))` for bare-metal, today's shape;
- `via(SSH, features=("xvc_dbgbr",))` for the Linux shape.

**Reasons HM shows instead:**
- An image reporting `xvc_jtagbb` gets: "2542 on this image drives jtag_bb (the Identify path), not the Debug Bridge: no ILAs."
- A harness older than v0.11 (no feature bits) gets: "needs harness firmware with 'xvc_dbgbr'".
- The MIG view is a sub-feature. It needs `impl == "linux"` and a static probes file. Otherwise the reason is "the bare-metal static has no MIG debug hub", or "no static probes file for 0x…; import the mint's".

### 3.2 The session: relay + hw_server

```
Vivado HW Manager ─► hw_server (HM-owned, 127.0.0.1:H, -p0, no -d/-I)          mode M1 (default)
                         │  xilinx-xvc:127.0.0.1:R
Vivado / openocd ───────►│                                                     mode M2 (bring your own)
                         ▼
                  XvcRelay 127.0.0.1:R   one client; names the attached pid/command; kick; hold
                         │
                  reach: hub tunnel local port (bare-metal)  |  board-SSH forward (Linux, lock on)
                         ▼
                  harness xvc_server :2542 ─► debug_bridge_0 ─► RM hub / MIG hub
```

- **Ports.** Use a stable block per board, like `DebugService`'s crc32 slot, so a saved Vivado URL keeps working. Suggestion: base 23600, two ports per slot (R, H), 64 slots. That keeps clear of 2542, 3121, 3000–3005 (hw_server's GDB ports) and OpenOCD's 23300 block.
- **The relay** exists so HM can:
  - show **who** holds the session (the spike reads the local peer's pid and command line from /proc);
  - free the board's one slot on demand (`kick`: the fake saw the disconnect 2–15 ms later, through two hops);
  - refuse re-attach during a swap (`hold`).

  It costs about 25 µs a round trip on loopback (§8).
- **M1: an HM-owned hw_server** (`xvc_spike_lib.hw_server_argv`). The spike showed:
  - it opens XVC lazily, only when a client opens the cable;
  - it releases the slot about 1–5 s after its last client leaves, so it does not squat while idle;
  - it frees the slot within 50 ms of SIGTERM.

  HM picks the hw_server that belongs to the user's Vivado. Order: `HARNESS_MANAGER_HW_SERVER`, else the `hw_server` next to `vivado` on PATH, else `$XILINX_VIVADO/bin`. **Never** the hub's shared `mapstone-dev:3121`, which is 2025.2 (`lx:host/pyverify/pyverify/debug.py:52-56`).
- **M2: bring your own.** HM gives `127.0.0.1:R` for `open_hw_target -xvc_url` (Vivado) or `xvc host/port` (Xilinx OpenOCD). HM still kicks at a swap. The user owns the 20 s linger: HM prints the rule, and `pyverify.hwserver.wait_stale_hw_server` is the helper.
- **Holding the slot between clients** (optional, §5 D-X4): the relay keeps its upstream connection open and hands it to successive local clients. That removes the reconnect race (§1.1) and stops another hub user from taking the slot between swaps. The cost: the board shows the slot held while HM's XVC session is "open".

### 3.3 Swaps, restarts and the tunnel

| Event | HM action (relay + M1) |
|---|---|
| `deploy.started` | `relay.hold("partition swap")`, which kicks the client; then stop HM's hw_server; publish `xvc.state swapping`. This runs synchronously, before the swap RPC, as `DebugService` does. It closes the target before the firmware gate can stall a shift into the new RM (handover trap 3) |
| `deploy.done {verified: true}` | start a **fresh** hw_server on the same port H; `relay.release()`; publish `xvc.state ready` with the new RM's `.ltx` path and Tcl. The user's Vivado runs `refresh_hw_device` or re-opens the target (the snippet, §3.5). No 20–40 s wait |
| `deploy.done {verified: false}` or `deploy.failed` | release with a warning: "XVC not reopened: the swap failed at <stage>; check what is loaded" (the same words as debug) |
| tunnel `down` (ssh died) | the client sees reset or EOF. The supervisor restarts on the **same** local port (spike: 1.8–2.2 s). `xvc.state` goes `down`, then `ready` |
| harness restart (`reboot`, WDOG, harnessd respawn) | the client sees EOF; `xvc.state down: harness restarted`. After it comes back with `rm_ok:false`, HM warns: "the RP is parked; swap once to bring the RM's ILAs back" |
| `lease.state` lost/expired/released | kick, close the relay, stop hw_server; `xvc.state down: lease <state>` |

**Default policy: auto-refresh** (close, then reopen after the swap). D-X3 offers "block the swap while attached". The spike found that hw_server survives a 12 s gate stall and re-detects a changed IDCODE on its own. But silicon's IDCODE never changes across a swap: only the debug hub behind the BSCAN master does, and the fake does not model that layer. The stale view on silicon is real (B3), so the fresh hw_server stays.

### 3.4 Arbitration

- **OpenOCD on 6921 vs XVC on 2542.**
  - On an `xvc_dbgbr` image they use different hardware (`jtag_bb` and DBGBR) and can run together.
  - On an `xvc_jtagbb` image both servers write the same `jtag_bb` DRIVE register (`ila:xvc_server.c:61-72`). HM then refuses to open one while the other is up, naming the other.
- **Another HM client.** The relay refuses a second local client (accept, then close, as the firmware does) and names the holder.
- **Another user on the board.** A probe (`getinfo`) through the path shows `free`, `held` (accept, then EOF or reset) or `refused` (not served).
  - Through `ssh -L`, a far end that refuses also looks like accept-then-EOF. The tunnel's `open_failures_since` separates "held" from "not served".
  - The probe takes the slot for one round trip, so HM never probes right before its own attach (the reconnect race). It probes only on demand, and only for the lease holder.

### 3.5 Probes files and the Tcl snippet

- **The RM's `.ltx`.**
  - For a directory overlay, use `Overlay.ltx_path()`, verified against the manifest's `ltx_crc32` (`ila:fpga/dfx/overlay/nanosoc_ila/manifest.json`).
  - For a content-store overlay, `import_overlay` must also store the `.ltx`, and `_StoredOverlay.ltx_path()` must return it (CCR X-2).
  - The loaded RM comes from `identity.rm_id`.
- **The static `.ltx`** (Linux, the MIG view) comes from a new per-static store: `<state>/statics/<static_id>/config_rm_greybox_static.ltx` + `.json`. It is imported from:
  - the mint's hub directory (`/home/david/mints/<static_id>/`);
  - or T7's two-target bundle (`targets.mcc_sd.static_ltx`, `lx:FLOW_CONTRACT.md:509`).

  The sidecar's `static_id` must match the board, or HM refuses.
- **Which file to load.** A device takes one `PROBES.FILE`:
  - RM ILAs only: the RM file;
  - MIG only: the static file;
  - both: a full-design `.ltx` of that config, which FLOW does not stage today (a request, §7). Until it does, the UI offers the two separately.
- **Version.** HM warns when the manifest's `vivado` is newer than the user's hw_server (mint 3 is built with 2026.1).
- **The Tcl** (`xvc_spike_lib.vivado_tcl`, M1):

  ```tcl
  open_hw_manager
  connect_hw_server -url localhost:<H>
  open_hw_target
  current_hw_device [lindex [get_hw_devices] 0]
  set_property PROBES.FILE {<ltx>} [current_hw_device]
  set_property FULL_PROBES.FILE {<ltx>} [current_hw_device]
  refresh_hw_device [current_hw_device]
  ```

  For M2, replace the second and third lines with `connect_hw_server -url localhost:3121` and `open_hw_target -xvc_url 127.0.0.1:<R>`. After a swap only the `set_property` and refresh lines are re-run.

## 4. Product surface

### 4.1 Web UI: an XVC card in the Debug section

```
┌ Fabric debug (XVC) ──────────────────────────────── [ready] ┐
│ Loaded: nanosoc_ila (0x0100000A) on 0x72BB0A36               │
│ Vivado: localhost:23601   [copy Tcl]  [copy URL]             │
│ Probes: nanosoc_ila.ltx ✓ crc   [download]                   │
│         MIG calibration (static) — Linux only  [download]    │
│ Attached: hw_server pid 4123 (you) since 14:02   [disconnect]│
│ ⚠ Unauthenticated on this harness (bare-metal)               │
│ ⚠ A swap closes this session; it reopens on the new RM       │
│ [Start]  [Stop]                                              │
└──────────────────────────────────────────────────────────────┘
```

- **States:**
  - `down`;
  - `starting`;
  - `ready` (nothing attached);
  - `attached` (with who);
  - `held` (someone else holds the board's slot);
  - `swapping`;
  - `failed` (with the reason).
- The card is greyed with the capability reason (§3.1) when `debug_fabric` is unavailable. The Overview's Debug tile gains one line.

### 4.2 CLI

All verbs go through the daemon (held board = the daemon's session), with the same JSON and TSV rules as `debug`:

```
harness-manager xvc status TARGET          state, endpoints, attached, probes files
harness-manager xvc up TARGET [--byo]      start relay (+ hw_server unless --byo); prints URL + Tcl
harness-manager xvc down TARGET            kick, stop hw_server, close relay
harness-manager xvc tcl TARGET [--byo]     print the snippet for what is loaded
harness-manager xvc ltx TARGET [--static|--rm] [-o FILE]   write the probes file
harness-manager xvc probe TARGET           free | held | refused (takes the slot for one getinfo)
```

### 4.3 Daemon routes and events (extension `daemon/xvc_api.py`)

| Method and path | Returns |
|---|---|
| `GET /boards/{bid}/xvc` | `{state, relay: {port}, hw_server: {port, pid, binary, version} or null, attached: {pid, command, since, bytes} or null, board_slot: free\|held\|unknown, ltx: {rm: {name, path, crc_ok}, static: {...} or null}, warnings: [..], mode: m1\|byo}` |
| `POST /boards/{bid}/xvc/up` `{byo?}` | 202 job `xvc_up` (hw_server start is seconds under load) |
| `POST /boards/{bid}/xvc/down` | `{ok}` |
| `GET /boards/{bid}/xvc/tcl?byo=` | `{tcl}` |
| `GET /boards/{bid}/xvc/ltx?which=rm\|static` | the file (`application/json`) |
| `POST /boards/{bid}/xvc/probe` | `{state, rtt_ms}`; 409 HELD while a job runs |

The event topic (append-only) is `xvc.state {state, relay_port, hw_server_port, attached, board_slot, ltx, detail}`. The rules are the usual ones:
- bearer auth;
- 409 HELD during a job;
- `deploy` jobs drive `swapping`.

### 4.4 Contracts: core vs the pack

- **Core (board-agnostic).** `core.pack` gains an optional `XvcAdapter` on `BoardSession.xvc`:
  - `xvc_endpoint() -> (host, port)`: where the harness's XVC server is reachable from this host, already tunnelled;
  - `xvc_reason() -> str`: `""` when usable;
  - `xvc_probes(rm_id) -> {rm: Path|None, static: Path|None, vivado: str}`;
  - `xvc_facts() -> {devices, hubs, notes}` for the UI.

  `services/xvc.py` holds the `XvcService`:
  - the relay;
  - the hw_server manager;
  - the port block;
  - the swap and lease hooks;
  - the events.

  It is modelled on `DebugService` and owns no board knowledge.
- **The MPS3 pack.**
  - `xvc_endpoint`: the reach's `xvc` port (bare-metal, today's tunnel), or a board-SSH forward (Linux with the lock: a second `SshTunnel` with `-J`, CCR X-1).
  - `xvc_probes`: the overlay catalogue plus the per-static store.
  - The capability routes.
- **Other boards fit:**
  - KR260 would return an endpoint on its own PS-side XVC server;
  - HAPS would return its `axi_jtag` bridge's.

  Neither needs a core change.

### 4.5 Before and after cutover

| | Bare-metal (today) | Linux (from mint 3) |
|---|---|---|
| Route | hub tunnel (exists) | board SSH via `-J` (new), once the lock is on; hub tunnel until then |
| Auth | none (UI warning) | board key + hub account; lease via the port-22 gate |
| Hubs | RM hub (ILA RMs) | RM hub + MIG hub |
| Restart | WDOG about 3 s | respawn about 1 s; WDOG = OS boot (budget 180 s) |
| Same HM code | yes; only `xvc_endpoint` and the MIG sub-feature differ | yes |

## 5. Decisions for david

**D-X1. How HM reaches XVC on the Linux harness.**
1. **(recommended)** An XVC lock on claimed boards (loopback peers only; S12's rule) + HM uses `ssh -J hub root@board -L`. The key is the auth; the lease gates port 22.
2. Keep 2542 on 0.0.0.0 and reach it through the hub tunnel, as on bare-metal. There is no auth, and any hub user can attach.
3. Bind 2542 to loopback always (per-port bind in `posix_net_if`), claimed or not. This is simpler to reason about, but an unclaimed lab board then has no XVC from the hub at all.

**D-X2. Who runs hw_server.**
1. **(recommended)** HM owns one per board (fixed port, killed and restarted around every swap), with "bring your own" as an option (`--byo`).
2. The user's Vivado auto-launches it; HM only waits out the linger (`wait_stale_hw_server`, up to 40 s per swap).
3. Print a command only; HM runs nothing.

**D-X3. A swap while XVC is attached.**
1. **(recommended)** Auto-refresh: close before, then reopen on a fresh hw_server after, with a notice and the new `.ltx`.
2. Block the deploy with 409 HELD naming the XVC holder, unless `force`.
3. Warn only; leave the session to the stall.

**D-X4. Should HM hold the board's slot while the XVC session is open?**
1. **(recommended)** Yes, but only while the user has pressed Start. The relay keeps one upstream connection and serves local clients in turn. This avoids the reconnect race and stops squatters between swaps.
2. No: connect upstream per client, and retry on "held" with back-off.

**D-X5. Probes for "ILAs + MIG at once".**
1. **(recommended)** Ask FLOW to stage a full-design `.ltx` per debug config for mbv. HM offers RM, static or full.
2. HM offers RM or static only; the user switches between them.
3. HM merges the two `.ltx` JSON files itself. **Not recommended**: nothing proves Vivado accepts a merged file.

**D-X6. Bare-metal XVC exposure.**
1. **(recommended)** Accept it until cutover; the UI warns, and HM opens the relay only for the lease holder.
2. A firmware change: a loopback/allow-list for 2542 on bare-metal. It needs a re-bake, and there is no loopback peer on bare-metal, so it would be a hub-IP allow-list. This is weak.

## 6. Implementation plan

The lanes can run in parallel. Each owns the files listed. Shared files change only through the lead's CCRs.

| Lane | Scope | Owns | Est. |
|---|---|---|---|
| **X1 Core service** | `XvcService`: relay (from the spike), port block, hw_server manager (argv, start, stop, version probe), swap and lease hooks, `xvc.state` events; the `XvcAdapter` protocol | new `services/xvc.py`, `tests/unit/test_xvc_service.py`, `tests/fakes/xvc_fake.py` (from `tests/spikes/xvc_fake_server.py`), `tests/fakes/fake_hw_server.py` | 6 h |
| **X2 MPS3 pack** | `xvc_endpoint` (the tunnel's `xvc` port; a board-SSH `-J` tunnel for Linux behind a boards.toml `xvc = "board-ssh"`); the capability routes; `xvc_probes` (catalogue + per-static store); the `.ltx` in the content store | new `harness_manager_mps3/xvc.py`, new `harness_manager_mps3/statics.py`, `tests/unit/test_xvc_mps3.py` | 5 h |
| **X3 Daemon + CLI** | `daemon/xvc_api.py` (routes, job `xvc_up`, HELD rules); `cli/cmd_xvc.py`; client parts; docs/API.md section | those files + `tests/integration/test_xvc_api.py` | 4 h |
| **X4 Web UI** | the XVC card, Overview tile line, events, the mock API | `web/static/js/sections/debug.js` (XVC part as `sections/xvc.js`), `tests/web/test_xvc_card.py`, `tests/fakes/t14_mock_api.py` additions | 4 h |
| **X5 HIL** (board window) | on 0x72BB0A36 through the hub: probe → up → Vivado attaches → `nanosoc_ila` capture → swap → reopen → capture; lease loss kicks. Linux B1 (h): the MIG view | `tests/hil/test_xvc_hil.py`, `docs/HIL_B0.md` section | 2 h + a 45 min window |

**CCRs (the lead applies them at the merge window):**
- **X-1 `tunnel.py`:** `SshTunnel(..., jump: str = "", user: str = "")`, which adds `-J`/`-l` to `build_argv`. `_runs_recorded_ssh` keeps matching. The spike used a private config's `ProxyJump`, which needs no code change for a user who writes the stanza themselves.
- **X-2 `overlays.py`:** `import_overlay` stores `manifest.ltx` as role `ltx`, and `_StoredOverlay.ltx_path()` returns it.
- **X-3 `core/pack.py`:** the optional `XvcAdapter` protocol and `BoardSession.xvc`.
- **X-4 `core/events` topic list** (docs/CONTRACTS.md): `xvc.state`.
- **X-5 `daemon/app.py`:** add `"xvc_api"` to `EXTENSIONS`.
- **X-6 `engine.py`:** resolve the `xvc` service lazily like `debug` (with the stub `reason`).
- **X-7 `harness_manager_mps3/capabilities.py`:** the `debug_fabric` spec (§3.1).
- **X-8 `pack.py`:** wire `xvc.make_xvc_adapter`.
- **X-9:** re-vendor pyverify after `feat/linux-harness` lands, so HM can import `pyverify.hwserver` instead of re-implementing it.

**Test strategy:**
- The fake XVC server (this spike's, promoted) speaks `getinfo:`/`settck:`/`shift:` with the firmware's rules: one client, the gate stall, fail-closed. Unit tests drive the relay and service against it.
- A `fake_hw_server` (a script that opens the XVC target when a client connects) keeps unit tests free of Vivado.
- An opt-in integration test (`HM_XVC_HW_SERVER=/path`) runs the real hw_server spike, and is skipped by default.
- The tunnel shape is tested with `tests.fakes.l1_fake_ssh` as today. The private-sshd spike stays a manual script: it needs `/usr/sbin/sshd`.

**Total:** about 21 h of lane work plus a 45 min board window. X1 and X2 gate X3 and X4 only through the frozen contract, so all four can start together.

## 7. Requests to the Linux harness lanes (not edited here)

1. **HARNESSD: the XVC lock** (if D-X1.1). When `harnessd_ssh_claimed()`, refuse 2542 connections from non-127/8 peers (accept, log the peer, close). Use a weak seam in `xvc_server.c`'s accept, `mps3_xvc_refuse_peer()`, defaulting to "accept", so bare metal is unchanged. Reuse `mps3_net_conn_peer()`/`mps3_net_addr_is_local()` and `--mock-trusted-peer` for tests. **Consider the same lock for 6921**: it is the same class of exposure.
2. **HARNESSD: who is attached.** `stats` (additive) gains `xvc: {peer, since_ms, shifts, stalled}` and `jtag: {peer, since_ms}`, so HM can name a remote holder it did not start.
3. **HARNESSD/firmware: the reconnect race.** In `xvc_server_poll`, when an incoming connection arrives while `s_conn` is set, first poll `s_conn` for a pending close (a zero-length `recv`) before refusing the new one. It is shared code, so it goes behind a flag if bare metal stays frozen. Test: close then reconnect at once, 20 times; expect 0 refusals (the spike's model refused 15–17).
4. **FLOW:** stage a full-design `.ltx` (static cores + RM hub + ILAs) beside each mbv debug RM's partial `.ltx`, with its own sidecar (D-X5.1). List the static `.ltx` + sidecar in the hub copy (`mint-hub-copy` already does this for mbv, `lx:FLOW_CONTRACT.md:505`).
5. **HOST:** keep `pyverify.hwserver` and `XvcSession` public. HM will import them after re-vendoring (X-9) rather than fork them.
6. **B1 step (h):** record whether the MIG core and an RM hub appear together behind `debug_bridge_0` (two hubs, one device?). This settles whether "both at once" needs a full `.ltx` or two devices.

## 8. The spike

**What it retires:** "Harness Manager's own SSH tunnel code can carry XVC to a loopback-bound harness through the hub (a two-hop ProxyJump). HM can own the hw_server, name the attachment, and free the board's slot around a swap, all without the board."

**Setup:**
- A fake XVC server with the firmware's semantics and a real IEEE 1149.1 TAP.
- A private user-mode OpenSSH 8.0 sshd on 127.0.0.1, with throwaway keys and its own config, so the user's `~/.ssh` is never read.
- HM's real `SshTunnel` (real ssh processes), in two shapes:
  - `xvc-hub` (1 hop, today's);
  - `xvc-board` (ProxyJump through `xvc-hub`, the Linux shape).
- The spike's `XvcRelay`.
- A real Vivado 2024.1 hw_server + xsdb, against the fake only.

The machine was loaded (load average 67–84 on 16 cores), so p90s are scheduler noise. The medians are over 320 samples, interleaved across paths.

| Path (127.0.0.1) | shift 64 b median | shift 2048 b median | overhead vs direct |
|---|---|---|---|
| direct to the fake | 32 µs | 33 µs | — |
| HM `SshTunnel`, 1 hop (hub shape) | 283 µs | 171 µs | +0.14 to +0.25 ms |
| HM `SshTunnel`, ProxyJump 2 hops (Linux shape) | 231 µs | 252 µs | +0.20 to +0.22 ms |
| relay + 2 hops | 258 µs | 275 µs | relay itself about +25 µs |

**Behaviour, all through the two-hop tunnel** (`tunnel_run.json`):

| Check | Result |
|---|---|
| Second client | seen as `held` (accept-then-EOF); the first client is undisturbed; the board logged 1 refusal; after release the probe reads `free` |
| Swap gate | `getinfo` answered while gated; `shift` stalled; the reply arrived 1 ms after ungate |
| Relay | named the attached pid and command (this process); refused a second client; `hold` kicked the client and the fake saw the slot free **1.9 ms** later; attach refused while held, allowed after release |
| Tunnel drop (SIGKILL ssh) | the client got a reset; the board slot freed in 32 ms; the supervisor restarted on the **same local port** in **1.8 s**; the probe after it read `free` |
| Reconnect race | an immediate reconnect after a close was refused 15–17 times in 20 on every path, direct included (8–18 across four runs). It is the server's accept-before-recv order (§1.1, §7.3) |
| Leftover processes | none (checked by scratch path) |

**Real hw_server** (`hw_server_run.txt`; argv from `xvc_spike_lib.hw_server_argv`: `-q -p0 -s TCP:127.0.0.1:<H> -e "set auto-open-servers xilinx-xvc:127.0.0.1:<fake>"`):

| Check | Result |
|---|---|
| Listening | after 2.4–4.5 s |
| With no client | it does **not** open XVC: the slot stays free |
| First client (xsdb `jtag targets -open 1`) | 700–950 shifts to scan; lists `debug_bridge (idcode 0a003093 irlen 6 fpga)`, as silicon did |
| While a cable is open | it keeps shifting in the background, about 30–250 shifts/s |
| A 12 s gate stall with the IDCODE changed underneath | no drop, no error. It re-detected the new device, and the log shows `xvc: disposed JTAG-jsn-XVC-…-0a003093-0`. hw_server keys a device by XVC URL + IDCODE, so on silicon, where the IDCODE never changes across a swap, the device context (and its cached debug-hub view) would **not** be disposed. That is a plausible mechanism for B3's stale view, **not proven** |
| Same hw_server, new client after the swap | sees the new chain |
| Fresh hw_server | the same |
| After the last client leaves | releases the XVC connection in about 1 s (about 5 s in an earlier run) |
| SIGTERM | slot free in 0.02–0.05 s |

**What the spike does not prove:**
- the real RTT through mapstone-dev, which was not measured: no ssh to the hub was allowed. The loopback overhead is a floor. Budget roughly (scan ≈ 700–950 round trips) × (hub RTT), for example about 3.5–5 s at 5 ms;
- hw_server's debug-core layer (hubs, ILAs) against a swap;
- Vivado GUI behaviour;
- dropbear as the board's sshd;
- the fpgahub port-22 gate.

**Re-run:**

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. nice -n 10 python -m tests.spikes.xvc_tunnel_spike \
    --rounds 8 --per-round 40 --connects 20 --json out.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. nice -n 10 python -m tests.spikes.xvc_hw_server_spike
```

## 9. Open questions

- **U-X1:** does `ssh -J` through the hub add a noticeable ILA upload time for a 16 K-sample capture? Measure it in X5.
- **U-X2:** with a mode-2 bridge carrying two BSCAN masters (Linux), does Vivado show one device with two hubs, or two devices? This decides D-X5 (B1 step h).
- **U-X3:** is the silicon stale view after a swap in hw_server's debug-core cache (the inference above) or elsewhere? M1's restart covers it either way; a cheaper `refresh_hw_device` would be nicer. Test in X5: the same hw_server, a swap, then `refresh_hw_device` only.
- **U-X4:** Windows/macOS. hw_server exists for Windows and Linux only. On macOS only M2 applies, with Vivado on another machine. The relay itself is portable Python, but its "who is attached" lookup reads /proc, so it is Linux-only.
