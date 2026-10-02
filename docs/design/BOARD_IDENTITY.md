# Board identity: detect a clash, and make a board match its hub entry

Lane BOARD-ID, 2026-09-28. Board-side contract: the Linux lead's answer of 2026-09-28
(net-protocol v0.16, images rc2_v7/v7n, due Tue 29 Sep evening). HM ships the code now,
feature-gated, and it lights up when those images land.

**Why.** The lab has two MPS3 boards behind the hub `mapstone-dev`. The hub's records are
right: `mps3_01_pl` is 192.168.10.101 and `mps3_02_pl` is 192.168.11.101 (hostname
`mps3-02-pl`). But board 2's Linux harness (rc2_v6n, netbooted, no card) shows board 1's
identity on its LCD: `MPS3-01`, `NET : 192.168.10.101`, `MAC 02:00:00:4D:50:53`. HM reaches
board 2 fine through the hub's address. david wants HM to find such clashes and fix them.

Source prefixes: `lx:` is `mps3-nanosoc-platform-lx` at 6beea09 (= `origin/feat/linux-harness`,
read only; not fetched: the fetch is over SSH), `hm:` is this repo.

## 1. Where the identity comes from today

Every image gives every board the same identity. Nothing on the board is per-board except
stage0's rescue address.

| What | Where it is set | Card boot (`/persist` on the card) | Netboot (no card) |
|---|---|---|---|
| **LCD label** (row 0) | `lx:firmware/clcd/clcd.h:97-98` `MPS3_BOARD_NAME "MPS3-01"`, set at `clcd.c:1327`, drawn at `clcd.c:826`. harnessd drives the panel (`harnessd/Makefile:92` `-DMPS3_HAS_CLCD`). The seam `clcd_set_board_name()` (`clcd.c:1269`) is not wired to anything | `MPS3-01` | `MPS3-01` |
| **LCD `NET :` line** (row 5) | `clcd.c:886-887` -> `clcd_fmt_net` -> `clcd_fmt_ip` (`clcd.c:414-421`): the compile-time `MPS3_DEFAULT_IP_A..D` (`firmware/common/net_proto.h:35-38`, 192.168.10.101). **Not the interface's address.** That is why both LCDs said .10.101 | 192.168.10.101 | 192.168.10.101 |
| **Real IP** | `lx:.../rootfs_overlay/etc/init.d/S41mps3net`: DHCP first (`:101`), then `MPS3_NET_STATIC` (`etc/mps3/net.conf:12`, 192.168.10.101/24) as a permanent secondary if `arping -D` finds it free (`:64-65`). The hub's dnsmasq gives board 2 192.168.11.101 (evidence `docs/evidence/2026-09-linux-b2/board2_commission/netboot.txt:7-9`, keyed on MAC 02:00:00:4d:50:53); board 2's link is its own, so DAD finds .10.101 free and board 2 holds BOTH (`claim_ssh.txt:3`: `lease=192.168.11.101 static101=added`) | net.conf may be edited: `/etc/mps3` is an overlay on `/persist/etc-mps3/upper` (`S12mps3persist:194-205`) | image default only (`/persist` is tmpfs) |
| **identify / version `ip`** | `lx:src/linux_harness/sw/harnessd/platform_linux.c:165-182`: `SIOCGIFADDR` on eth0 = the first address added (the lease if it came in the foreground attempt, else .10.101). identify's `board` is the constant `"mps3"` (`firmware/identify/identify.c:163`) | as left | as left |
| **MAC** | the DT: `lx:src/linux_harness/shell_linux.dts:341` `local-mac-address = [02 00 00 4D 50 53]`; eth0 reads it; harnessd reports sysfs (`platform_linux.c:117-131`, fallback the same constant `:120`); the LCD MAC row reads that (`clcd.c:966-970`) | image DTB | image DTB |
| **Hostname** | `lx:.../configs/mbv_harness_defconfig:78` `mps3-harness`; `net.conf:24` `MPS3_HOSTNAME=` empty (v0.16: the label lower-cased, §2.1) | `mps3-harness` unless net.conf is edited | `mps3-harness` |
| **stage0 rescue** | `lx:src/linux_soc/hw/fw_stage0/stage0.c:78-80` `S0_IP` (#ifndef, default .10.101), `:81-98` `MPS3_MAC0..5` (default 02:00:00:4D:50:53), both via `S0_EXTRA_DEFS` (`Makefile:51,69`). Board 2's bake (build 0x6FAE6A0B) changed only the rescue address and build id (platform `6850cda`); Linux never read it | per-board | per-board |
| **Kernel cmdline** | `shell_linux.dts:34`: console + uio only. `mps3.net=` and `mps3.persist=` are honoured but nothing sets them | - | - |

Bare metal is the same (`firmware/platform/src/net_if_lwip.c:776` weak MAC with per-octet
`-DMPS3_MACn`, `net_proto.h:35-38` IP).

## 2. The board-side contract (Linux lead, net-protocol v0.16)

At boot each field resolves: (1) the override in `/persist/etc/mps3/identity` (card boards
only); (2) stage0's status block (the per-board bake gains `S0_LABEL` and publishes
ip/mac/label); (3) the image default: label `MPS3` (no number), 192.168.10.101/24,
02:00:00:4d:50:53, `source:"default"`, which HM shows as "identity not set".

- **`identity`** (6900, a read, any peer): `{"ok":true,"op":"identity","label":"MPS3-02",
  "hostname":"mps3-02","ip":"192.168.11.101/24","mac":"0200000002fe","source":{"label":
  "stage0","hostname":"label","ip":"stage0","mac":"stage0"},"stage0":{...},"override":null,
  "pending":null,"persist":true}`. `pending`: what applies after the next reboot, when it
  differs from the running values. The MAC is 12 lowercase hex digits.
- **`identity_set`** (6900, CLAIM-LOCKED like `slot`/`usd`): any subset of `{label, hostname,
  ip, mac}` or `{"clear":true}` -> `{"ok":true,"persisted":true,"pending":{...},"applies":
  "reboot"}`. Codes: `locked`, `no_persist` (netboot or no card: the stage0 bake IS the
  identity), `invalid` (names the field; the MAC must be unicast and non-zero; the label
  must fit the LCD row). Applied by the WARM `reboot` verb only.
- `mps3-identity get|set k=v…|clear` on the board edits the same file (for SSH users).
- `identify` gains `label`; `version.features` gains `identity`.

### 2.1 As shipped (V7-ALIGN, platform `feat/linux-harness` 18622e5, images rc2_v7/v7n)

The shipped contract is additive over the draft above; where it differs, HM follows it:

| Point | Shipped | HM |
|---|---|---|
| `identity_set` refusals | in this order: `locked` (first, whatever the request holds), `no_persist` ("identity: no persistent /persist (use the card)"), `invalid` ("invalid <field>: <why>"); `io` for a failed write | `fix_reason` checks the claim before the card; a bad value is reported only after both (service `fix`, CLI, API: 409 before 400) |
| `""` for a field | DROPS that key from the override | `validate_want` keeps `""` (`DROP`); CLI `--unset FIELD`, API `unset: [..]`; a field sent as `""` to the API is still "not given" |
| label | 1-19 of `[A-Z0-9-]` (a stage0 bake: <= 8) | `LABEL_MAX` 19, the same charset |
| hostname | RFC 1123, dot-separated, <= 63; default = the label lower-cased (`mps3-01`; `mps3` with no bake), was `mps3-harness` | the same check. HM files SSH keys by board id, never by host name |
| ip | `a.b.c.d/nn`, nn 8-30, a usable host (not 0/8, 127/8, >= 224, network, broadcast) | the same check |
| replies | `identity`, `identity_set`, `locate` carry `"op"` | taken with or without |
| bare metal | `identity not supported`, code `not_supported` | UNAVAILABLE (bare metal named); `unknown op` is an image older than v0.16 |
| identify | `label` after `ssh`, before `ports`; `ip` is the DHCP lease while `dhcp:true` | a lease is kept as `lease`, never compared as the board's IP |
| default label | `MPS3` (no number) | never a label clash on either side ("identity not set (default label)"), nor a label "differs" from the hub record; a duplicate MAC or IP is still a clash, and an IP or MAC unlike the hub's still differs |

Board 2 before its identity bake reports `MPS3`, the old MAC 02:00:00:4d:50:53 and its own
IP (192.168.11.101, from stage0): HM shows "identity not set (default label, MAC)", and a
clash only on the MAC while board 1 still has that MAC too.

## 3. What HM reads

| Source | What | When |
|---|---|---|
| 6900 `identity` (feature `identity`) | label, hostname, ip, mac, source per field, stage0, override, pending, persist | `board identity`, `GET /identity`, the Board tile's own load. Never inside `info` |
| UDP identify (LAN only) | mac, ip, `label` once v0.16 | `info` (cached 30 s; never a control-port connection) and older images |
| 6900 `stats` | mac | older images behind a hub (UDP does not cross the tunnel) |
| the hub record: `fpgahub target show T` (SSH runner) or `GET /targets/{t}` (REST) | `network.{board_ip, board_mac, hostname, host_ip}`, `discovered_mac`, the owning board (`mps3_02`) | `board identity`, `GET /identity?refresh=true`; cached for the session |
| the other hub targets | the same, for every member `fpgahub board list --json` names | `board identity` and `refresh=true` only |
| the boards this HM has seen | `<state>/identity/seen.json`: the last identity each board reported (board_id -> label, ip, mac, at) | every read |

## 4. Clash detection (`services/board_identity.py`, board-agnostic)

Each finding has a `kind`, a `level` and one line of words.

1. **clash (error)**: another board reports the same MAC, IP or label; this board reports
   another hub target's `board_ip` or `hostname`. The strongest signal.
2. **unset (warning)**: a field comes from the image default (`source` `default`), or an
   older image reports the image's MAC 02:00:00:4d:50:53 (every board has it).
3. **differs (warning)**: the board's label/IP/MAC differ from its own hub record. The
   label is compared with the hub's owning board (`mps3_02` -> `MPS3-02`), the IP with
   `board_ip`, the hostname with `hostname` less a `-pl` suffix.
4. **hub record suspect (note)**: the hub's `board_mac` equals `discovered_mac` (a netdev
   the hub found on its own USB bus: the hub's adapter), or it is a universally
   administered address while the harness MACs are locally administered. Today
   `mps3_01_pl`'s `board_mac` 00:e0:4c:46:dc:f8 is the hub adapter's. HM then says which
   side says what, and never proposes that MAC in a fix.

`status` is the worst finding: `clash` > `unset`/`differs` > `ok`; `unknown` when nothing
was read. `info` carries it as `BoardInfo.net_identity` (absent on a board with no
Ethernet shell), and a clash also adds a `Health` note.

## 5. The fix: "Make this board match its hub entry"

`board identity TARGET --from-hub` (or `--label/--ip/--mac/--hostname`, or `--clear`), the
Board tile's **Fix identity**, `POST /boards/{bid}/identity`:

1. **Plan**: want = the hub record (label from the owning board, `board_ip` + the prefix of
   `host_ip`, `board_mac` unless suspect). Changes = the fields that differ from the
   board's running values. No changes: nothing is done.
2. **Refuse before the question** (each with its reason, nothing sent):
   - bare metal: "the bare-metal harness has no identity store; its identity is compiled
     in" (12 UNAVAILABLE);
   - no `identity` feature: "this harness image predates the identity verbs (rc2_v7)" (12);
   - `persist:false` (netboot, no card): "the identity comes from the stage0 bake: re-bake
     stage0 for this board (S0_IP, S0_LABEL, MPS3_MACn) or give it a card" (15 REFUSED);
   - the lease: behind a hub, only the lease holder (4 HELD, the holder named);
   - the claim: the board must be claimed by THIS HM (`claim.lock_plan` = board-ssh):
     unclaimed or another key's claim is 15 REFUSED with the claim hint;
   - the reset guard: a card job writing or reading back refuses (4 HELD, the job named).
3. **Typed confirm**: the new label (e.g. `MPS3-02`); `IDENTITY <board_id>` when there is
   no label. `--yes` never implies it (`--consent PHRASE`); the API needs
   `{"confirm": "<phrase>"}`.
4. **Set**: `set_identity(session, want)` (the seam, §6), over the claim forward.
5. **Warm reboot**: the harness `reboot` verb, witnessed (`session.os_slots.reboot`: up_ms
   restarts). **Never an MCC REBOOT or a power cycle**: a cold start hits the stage0 DDR
   bug right now.
6. **Verify**: read `identity` again; every changed field must now run with `pending` null.
   If the board's IP changed and the old address stops answering, the result says so
   (applied, not verified: reopen it at the new address).

## 6. The seam

```
set_identity(session, want: dict) -> {"persisted", "pending", "applies", "route"}
```

- **`HarnessdSetter`** (the default when `version.features` has `identity`): 6900
  `identity_set` through the claim forward (`claim.lock_route`, CLAIMED-LOCK), then the
  refusal codes above mapped to HM errors.
- **`SshCommandSetter`**: `mps3-identity set k=v…` over the pinned `board ssh` argv. Same
  checks on the board; kept for a harness that has the command but not the verb.
- Without either: `UnavailableError("board identity", "pending the Linux lead's interface
  ...")`. Tests use a fake setter.

## 7. Safety summary

Lease held here; the board claimed by this HM; a typed confirm; never during a card job;
refused on bare metal and on a netbooted board with the reason; only the warm `reboot`
verb; the MAC the hub has wrong is never proposed.

## 8. As built (lane BOARD-ID, 2026-09-28)

| Piece | Where |
|---|---|
| Rules, plan, the boards seen, the service | `src/harness_manager/services/board_identity.py` (`engine.board_identity`) |
| MPS3 adapter, the seam (`HarnessdSetter`, `SshCommandSetter`, `PendingSetter`) | `src/harness_manager_mps3/net_identity.py` (`session.net_identity`; one hook line in `pack.py`) |
| Hub records | `HubClient.target_info(name)`/`groups()` (SSH), `RestHubClient.target_info(name)` |
| `info` | `BoardInfo.net_identity`; a clash adds a `Health` note |
| CLI | `board identity TARGET [--from-hub | --label/--ip/--mac/--hostname | --clear] [--consent] [--wait]` (`cli/cmd_identity.py`) |
| API | `GET/POST /boards/{bid}/identity` (`daemon/identity_api.py`), `RemoteIdentity` |
| Web | the Board tile's Identity row and Fix identity dialog (`web/static/js/sections/identity.js`) |
| Fakes | `tests/fakes/idn_board.py` (the v0.16 verbs on the slot board), `tests/fakes/idn_mock_identity.py` (T14 mock) |

Not wired yet: nothing reads `identify.label` beyond the cheap read (v0.16 adds it); the SSH
setter is selectable (`Mps3NetIdentity(setter=SshCommandSetter())`) but never the default.


## 9. Name this board (lane IDENTITY, HM v0.1.1, 2026-10-02)

david's decisions of 2 Oct (relayed by the Linux lead) supersede BRINGUP-2's MAC from the MCC
serial: a name of 1-16 of A-Z, 0-9 and - (HM upper-cases; the aligned panel shows 16), boards
named both ways (staff cards, and on the spot in HM), the identity following card and board
later (a card sector in v2.1, MCCIF IDENT in mint 4: not here), and **a unique IP per board
with a RANDOM MAC**.

| Piece | Where |
|---|---|
| The rules (name, MAC, IP), `random_mac`, `allocate_ip`, the notes, `move_board_table`: board-agnostic, and the seams a later card-sector writer reuses | `services/identity_assign.py` |
| The pack's policy (`IdentityPolicy`): byte 0 0x02, reserved 02:00:00:*, the pool `mps3.identity.ip_pool` 192.168.10.110-199, never 192.168.10.101, `answering` = identify, the rescue note, the hub records known wrong | `harness_manager_mps3/net_identity.py` `identity_policy()`, `Mps3Pack.identity_policy()` |
| The registry: every MAC and IP assigned or seen, per board, first/last date (`history`) | `SeenIdentities.record/taken/move` (`<state>/identity/seen.json`) |
| The proposal, the guards (hub: `hub_fixed` names the hub; subnet: `other_subnet`), the move | `IdentityService.propose/guards/precheck/fix` |
| A board that moves: the reboot verb without the old-address witness, identify at the new IP, the pinned host key, the records moved (boards.toml, claims.json, known_hosts) | `Mps3NetIdentity.relocate/host_key/adopt_move`, `ClaimRecords.move` |
| CLI | `board identity TARGET --label N --mac random|MAC --ip auto|A.B.C.D [--hub-fixed HUB] [--other-subnet]` |
| API | `GET /boards/{bid}/identity/proposal`; POST `hub_fixed`, `other_subnet`, result `moved`, `address` |
| Web | ONE dialog, `name-board` (`sections/identity.js`, `css/identity.css`): from Board > Access (Name this board…, Fix identity… = its hub-entry mode) and from the bring-up wizard (pre-filled) |

**A move.** The board drops its old address at the restart (S41mps3net: DHCP first, then the
new static IP as a permanent secondary, skipped if `arping -D` finds it taken; the old .101 is
not kept). When the session reaches the board at the address that changes (its identity IP or
its DHCP lease; never through a hub or a tunnel, whose address does not change), the job reads
the pinned host key, sends the warm reboot verb, and asks identify at the new IP (every 3 s, up
to 240 s). The board's host key lives in /persist, so it is the same after the restart; another
key is another board (refused, nothing adopted). Found, the board's records move from
`mps3@old:6900` to `mps3@new:6900`: boards.toml (a table keyed by the old id is renamed; one
found by `match` gets the new address), the claim record, the known_hosts file written under
the new id's alias, the registry. The session at the old address is closed by the app when it
opens the new one (**Open it at …**).

**Not here:** the card identity sector (v2.1), MCCIF IDENT and the SYSCON writer (mint 4), the
DNA MAC. `identify.label` as HM's display name (the platform sends `label`, HM reads `name`)
is a separate change.
