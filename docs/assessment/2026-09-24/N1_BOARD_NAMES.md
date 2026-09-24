# N1: board names (2026-09-24)

**Answer.** With this branch, the lab board shows as **mps3-01** in the rail, the header, the
window title, `harness-manager probe` and `harness-manager info`. The name comes from the hub.
The boards.toml from docs/HIL_B0.md already names the hub target (`hub.target =
"mps3_01_pl"`), and fpgahub's board for that target is `mps3_01`:

- **Before the board is opened** (rail, `probe`), the name is derived with no hub call, by
  fpgahub's own rule. Source `hub-target`.
- **At the first `info`**, the hub is asked once with `fpgahub board list --json`, which is
  read-only. Source `hub`.

No fielded source gives a per-unit identity today. The name is display only and never keys
the board.

## 1. Where a name or a unit identity can come from

Each source is checked against the fielded board: shell `0x72BB0A36`, bare-metal firmware
v0.11, reached through the SSH tunnel and fpgahub shares.

| # | Source | What it holds | Today? | Evidence |
|---|---|---|---|---|
| 1 | **fpgahub target** | `mps3_01_pl`: the name for leases, shares and `target show` | **Yes**, in boards.toml `hub.target` | pyverify `lease.py` "THE NAME AUTHORITY" (measured 2026-09-11) |
| 1 | **fpgahub board** (the chassis) | `mps3_01`, which owns `mps3_01_pl`. `fpgahub board list --json` returns `{"groups":[{"board":"mps3_01","size":1,"is_paired":false,"members":[{"name":"mps3_01_pl","role":"pl"}]}]}` | **Yes**: one read-only ssh call, or derived offline | fpgahub 0.3.0 (`~/SoCLabs/fpgahub-v030-readonly`): `cli.py` `chassis_list` over `GET /groups` (`api/v1.py:393`), shape `api/schemas.py` `GroupsResponse`. `grouping.chassis_of` takes an explicit `Board.chassis`, else strips `_ps`/`_pl`/`_mcc` |
| 1 | fpgahub `description` | A free-text label: `Board.description` (`target show`, `PUT /targets/<n>/metadata`), and `[chassis.<id>] description` (`board status`) | Maybe: empty unless the hub admin set it; its content on mapstone-dev is unknown | `config.py` `Board.description`, `Chassis.description` |
| 1 | fpgahub `network.hostname` | The DHCP/DNS name the hub's dnsmasq gives the board | Unknown value (not read: no ssh); the MPS3 does no DHCP until firmware A0 | `config.py` `BoardNetwork.hostname` |
| 2 | harness `version` | `harness, ver32, sha, dirty, lmb_kb, features, usr_access, skew` | **No name, no unit** | `net_proto.c` `MPS3_OP_VERSION` on `feat/rm-ila-mint`; pyverify `VersionResponse`; FakeShell `_op_version` |
| 2 | harness `ping` | `shell_id, rm_id` | No name, no unit | FakeShell `_op_ping` |
| 2 | harness `stats` (v0.11) | 22 fpgahub keys plus extras, including `mac` | No name. `mac` is a compile-time constant | `net_proto.c` `MPS3_OP_STATS` |
| 2 | identify (UDP 6899) | Has a `unit` slot; there is no `name` | **Not on v0.11 bare-metal** (from v0.12). It does not cross the tunnel anyway | `identify.py` docstring; Linux harness plan §10 |
| 2 | Linux harness (`mps3-harnessd`, mint 3) | identify `impl`, `unit` once D6 lands, a Linux hostname | Not fielded | Linux harness plan §10; handover D6 |
| 3 | MCC banner | `USB Serial Number = 0000000000000` (all zeros), `HBI0309 build 567`, `rev C, var A` | **No unit id**: the serial is zeros on this board; the rest is the board revision | `docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt`; `mcc.py` `BootRecord` |
| 3 | MCC `CFG R` | `OSC n`, `TEMP 0`, `V n` (always ERROR), `SCC` | No board id | `mcc.py` facts 3 and 4 |
| 4 | config SD | Volume label `V2M-MPS3` (the same on every board); `config.txt`, `MB/HBI0309C/...` | No per-unit file. Only the host can read it (USB MSD, owned by the MCC), never the harness | `fpga/mps3_sd/` |
| 4 | FPGA DNA | 96-bit `DNA_PORTE2`, unique per die | **Not reachable by the app**: only a JTAG read on the hub, which stalls the MicroBlaze. The harness gets it with D6 (mint 3 or 4) | handover D6; FOLD plan |
| 4 | shell MAC | `02:00:00:4d:50:53` (C board), `...:42` (B board) | **Not per unit**: compile-time `MPS3_MAC0..5` | `net_if_lwip.c` `mps3_platform_mac` |
| 5 | boards.toml | `name = "mps3-01"`, new in this lane | **Yes**: always works, per user | `power/config.py` `BoardConfig.name` |

**Unit identity.** There is none today. The first real one is the D6 DNA, reported as identify
`unit`. T12 already keys a board by it: `board_id = mps3@<unit>`, else `mps3@<ip>:<port>`.

## 2. The design

### Resolution order (`harness_manager/naming.py`; the first source that gives a name wins)

| Rank | `name_source` | Where it comes from | Why it ranks here |
|---|---|---|---|
| 1 | `config` | boards.toml `name` in the board's table (matched by key or `match`) | The only name the user sets on purpose. If an automatic source could override it, setting it would do nothing |
| 2 | `harness` | `BoardIdentity.name`: the proposed `name` key of `version` and identify | It is stored on the board, so it travels with the board from hub to hub |
| 3 | `hub` | The fpgahub board that owns `hub.target`: boards.toml `hub.board` if set, else the hub's answer (`fpgahub board list --json`, cached for 1 h per process, a failure for 5 min) | It names the hub's slot. A board swapped into the slot inherits it, so it ranks below the harness |
| 4 | `hub-target` | The same id, derived offline from `hub.target` by fpgahub's suffix rule | Instant, and good for boards that are not open yet. The hub's answer replaces it at the first `info` |
| 5 | `""` | Nothing matched | Front-ends show the address, as before |

**Differs from the lead's sketch.** The lead suggested harness, then hub, then boards.toml, then
address. This design puts boards.toml **first**, for the reason in rank 1. The order is the
one tuple `naming.SOURCES`, so flipping it is a one-line change with the tests.

**Hub ids are shown with hyphens.** `mps3_01` becomes `mps3-01`. The raw id stays visible:
`hub.target` in boards.toml, and the rail tooltip ("name from the hub").

**A name is display only.** It never keys anything. `board_id`, boards.toml tables, session
locks and hub leases stay keyed as before. So a rename loses nothing, two boards may share a
name, and a TARGET argument is still an address. Accepting a name as a TARGET is a follow-up:
it needs `Mps3Pack.candidate_for_host`, which the lead owns.

**Validation (`naming.clean_name`).** A name must be a string of 1 to 64 printable characters
after trimming. Control and format characters are refused, because they could rewrite a
terminal line or a TSV row. When the harness or the hub sends a bad name, the next source is
used. A bad boards.toml `name` is logged, with its key, and ignored. It is not a
`ConfigError`: the same file routes the board through its hub (`via`, `hub`), and a typo in
a label must not cut the board off. Naming never raises.

### Where each source is applied

- **Candidate time, offline** (`harness_manager_mps3/naming.py` `name_candidate`, a pack
  hook). `Mps3Pack.candidate_for_host` and `probe` apply boards.toml, the probe identity's
  harness name, then the hub table (`hub.board`, the cache, or the derived id). This never
  calls the hub or the board.
- **Info time** (`Engine.info`, CCR N1-2). The live identity's harness name is offered first,
  then the optional session hook `board_name(identity)` (CCR N1-3). The MPS3 session answers
  with the hub's confirmed name, asking once per (hub, target) per process. When the name
  improves, the engine replaces the candidate in its entry and in `session.candidate`, so
  `GET /boards` and later reads keep it. Only the `name` fields change.
- **Merges keep the name.** `engine.probe`, `power.config.with_links`, the USB pairing and
  the daemon's `POST /boards` with extra links rebuilt `Candidate` field by field, which would
  drop any new field. They now use `dataclasses.replace`, and `engine.probe` keeps the
  better-ranked name.

### The hub lookup, and LR-A

LR-A owns `hub.py` and adds `HubClient.board_id()`. This lane does not edit `hub.py`:

- `naming.hub_board_id(host, target, group, client=...)` calls `client.board_id()` when the
  session's `HubClient` has it.
- Until then it runs its own `["fpgahub", "board", "list", "--json"]` through
  `hub.DEFAULT_RUNNER_FACTORY`. That is the same seam, and the same `sg fpga` ssh runner, as
  every other hub verb.
- It reads the boards.toml `hub` table raw, including `board`, the key LEASE_REQUESTS.md
  gives LR-A. So it does not depend on LR-A's parser.

**Cost.** One ssh call at the first `info` of a board behind a hub, per process. The daemon
makes it once an hour; each CLI `info` makes it once, about 1 to 2 s. boards.toml
`hub.board = "mps3_01"` skips the call.

### What shows it

- **CLI.**
  - `info`: the first line is `name       mps3-01 (from the hub)`, only when the board has a name.
  - `probe`: each line starts with the name when the board has one.
  - `--json`: the candidate gains `name` and `name_source`; `identity` gains `name`.
  - `--tsv`: `NAME` is appended to the `probe` and `info` columns.
- **Web UI** (`format.js` `boardName`/`nameSourceText`; `app.js`; one CSS rule).
  - Rail: the name in bold, with the board id and the name's source in the tooltip. Otherwise the address, as before.
  - Header: `h1` shows the name, a new `.header-sub` line shows "MPS3 greybox on shell 0x72bb0a36", then the board id.
  - Preview card: "mps3-01 · MPS3 greybox on shell …".
  - Window title: `mps3-01 · Harness Manager`.
  - Demo: the fielded demo board is named `mps3-01` (source `hub`).
- **Events.** `board.found` and `board.identity` add `name` and `name_source`.
- **docs/API.md.** "Board names (lane N1)". **docs/USER_GUIDE.md.** A short "Board names" paragraph.

## 3. Contract change requests (implemented behind them on `team/n1-naming`)

| CCR | File (owner) | Change |
|---|---|---|
| **N1-1** | `core/model.py` (lead) | `BoardIdentity.name: str = ""`; `Candidate.name: str = ""`, `Candidate.name_source: str = ""`. Additive with defaults; the codec ignores unknown keys, so old clients and daemons interoperate |
| **N1-2** | `engine.py` | `Engine.info` may rename the open board: only the name fields of `entry.candidate`/`session.candidate` change. `probe` merges keep the better name. `board.found`/`board.identity` add `name`, `name_source` (CONTRACTS topic payloads, additive) |
| **N1-3** | `core/pack.py` (lead; documentation only) | Optional `BoardSession.board_name(identity) -> (name, source)`. The engine calls it through `getattr`, so no protocol edit is needed; list it with the other optional hooks |
| **N1-4** | `harness_manager_mps3/hub.py` (LR-A) | When LR-A merges `HubClient.board_id()`: `naming.hub_board_id` already prefers it (tested with a stand-in client). Then delete `naming.query_board_id`, the private `board list` call. If `board_id()` caches, drop naming's cache too |
| **N1-5** | `harness_manager_mps3/pack.py` (lead) | Hooks `.naming:name_candidate` (candidate time) and `.naming:session_board_name` (via `Mps3Session.board_name`) |
| API | `docs/API.md` | The additive section above |

## 4. Harness-side proposals

### For the harness (bare-metal) lead: FOLD A-v0.12, a service module so both engines get it

| # | Proposal | Detail |
|---|---|---|
| H1 | **`name` in `version` and identify** | An additive string key; absent or `""` means unnamed. Recommend `[a-z0-9][a-z0-9-]{0,31}`, a DNS label, so it can double as the Linux hostname. Hosts accept any 1 to 64 printable characters and ignore the rest. No net-protocol bump beyond the v0.12 notes. pyverify: `VersionResponse.name`; FakeShell `harness_name=`. |
| H2 | **`unit` in `version` too** | Today it is only in identify, which does not cross the hub's tunnel. `unit` = the D6 `DNA_PORTE2` value, fixed-width lowercase hex. Through a tunnel, `version` is the only identity channel, so the host can key the board by unit there too. |
| H3 | **Where the name lives: the user microSD (D13 store)** | A small record, for example `MPS3/NAME.TXT`, one line, read once at boot by the `firmware/usd` driver. No card, or no record, means no name, and boot is unchanged (david's rule). Not the config SD: the MCC owns it and the FPGA cannot read it. Not the QSPI flash: the flash-boot regression's "writer unknown" (13 bytes zeroed at 0x20000) makes a second flash writer a bad idea until that is found. The name travels with the card; `unit` (H2) lets the host notice a card moved to another board. |
| H4 | **Setting it: `{"op":"set_name","name":"mps3-01"}`** | Writes H3's record and replies `{"ok":true,"name":…}`. `ok:false,"err":"no store"` without a card. Harness Manager would add `harness-manager name TARGET NEW` and a rename field in the header, behind a `set_name` feature bit. Until then, the host writes the file on the card. |
| H5 | **D6 derives the MAC from the DNA** | Already an option in handover D6. It removes the "two boards cannot share one L2 segment" limit and makes the MAC per-unit. |

### For the Linux harness lead (mint 3)

| # | Proposal | Detail |
|---|---|---|
| L1 | **Report `name`** in `version` and identify. The default is the OS hostname. | |
| L2 | **Store it on the persistent partition** (`/etc/mps3/name`) and set the hostname from it at boot. | `ssh mps3-01` and identify then agree. |
| L3 | **`set_name` updates both the file and the hostname.** | |
| L4 | **`unit` (DNA) in `version`**, as H2. | |

### For fpgahub (the hub owner): optional

The hub's name already works. A `[chassis.mps3_01] description` would give a subtitle. A later
lane could show it: `fpgahub board status mps3_01 --json` returns `description`.

## 5. Tests (all board-free; each has a negative twin)

- **`tests/unit/test_n1_naming.py`** (28 tests).
  - The order, and that a weaker source never renames.
  - Validation, including control, format and ANSI characters.
  - boards.toml `name`: matched, and not another board's; a bad value is logged and ignored, and the file still routes the board.
  - `with_links` keeps the name.
  - The name from `version` and from identify, and ignored in rescue.
  - `board list --json` parsing: a target no board owns gives no name; junk raises.
  - The suffix rule.
  - The fake hub's JSON is fpgahub's shape.
  - One hub call, then the cache; a failing hub gives no name and is not asked again.
  - LR-A's `board_id()` is preferred.
  - The hub table names a board before it is opened, with no hub call.
  - A cached hub answer beats the derived name; `hub.board`; config beats the hub; the session hook.
- **`tests/integration/test_n1_names_virtual.py`** (6 tests, L1's lab rig: fake ssh tunnel plus fake hub).
  - Probe gives `mps3-01`/`hub-target`; the first `info` gives `mps3-01`/`hub`, and the hub is asked exactly once.
  - An unnamed board is its address.
  - boards.toml beats the hub.
  - A harness `name` beats the hub, in probe and info.
  - CLI human, `--json` and `--tsv` output; with no name, there is no name line.
- **`tests/web/test_n1_names_browser.py`** (2 tests, headless Chrome).
  - The rail, preview, header and window title show `mps3-01` with the source tooltip.
  - The unnamed USB demo board shows its address, with no subtitle.
- **`tests/fakes/l1_fake_hub.py`.** `board list --json` answered in fpgahub 0.3.0's shape. `FakeHub.boards` sets an explicit chassis.

**Unproven.** The `board list --json` call has not run against the real hub: no ssh from this
lane. Its shape is from the 0.3.0 source and matches `pyverify`'s measured `mps3_01`/`mps3_01_pl`
pair. The first real `info` in the app will show it.
