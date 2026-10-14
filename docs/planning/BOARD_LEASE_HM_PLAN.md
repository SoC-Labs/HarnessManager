# Board lease (single-user mode) in Harness Manager — lane plan (Release 3, Mon 19 Oct)

Plan of record: https://claude.ai/artifact/R3wnzGRK1BvxfqmNNXyKvt · cross-project lane table:
/tmpdir/claude-74755/linux-lanes/RELEASE3_LANES.md · seam (frozen F0):
`src/harness_manager/services/lease_backend.py`.

## What it is
A desk board can be its own lease server (feature `board_lease`, net-protocol v0.21). The
hub lease model users know (Take lease / Give back / Request / queue / Keep / force after
2:00 with the board's name typed / idle give-back / the taken banner) is reused unchanged;
only the words change ("the board" instead of "the hub"). External users install only HM:
reads go over the control port (6900), writes over the claim SSH HM already uses.
Phase 1 is a **courtesy lock** and HM says so (`lease_backend.COURTESY_LABEL`).

## Board wire (pyverify `board_lease.py`, frozen F0 names; contract v0.21 text by -f9)
- verb `lease`, acts `show history acquire heartbeat release request respond cancel revoke mode`.
- relative seconds only; `acquire` → 32-hex token; every gated verb carries top-level
  `"lease":"<token>"` in single-user mode.
- codes: `leased lease_required lease_held bad_token queue_full local_hold not_holder busy_access`
  (+ the claim gate's `locked`).
- hello gains `board_lease {mode, held, who, expires_in_s, queue_len, you}`; identify gains
  `lease {mode, held, who}`; panel events `lease_break`, `access_arm`.
- defaults: TTL 3600, request window 120, reclaim after reboot 300, touchscreen local hold
  900, queue 4.

## UX
- Board → Access gets a card **"Who can use this board"** with a segmented control
  **Shared / One person at a time** (claim.js, under the SSH claim card). It is the board's
  state (every PC sees it), not a setting.
- Turning ON: owner only (the claim holder, `claim_status.claimed.mine`), board free; it
  **takes the lease in the same step**. Confirm dialog: "Switch <board> to one person at a
  time? You take the lease now… Someone at the board can always switch it off on the
  touchscreen." Button "Switch and take the lease". Shows the courtesy-lock label.
- Turning OFF: owner or current holder; owner while someone else holds = force with the
  board name typed (existing ForceConfirm).
- Unclaimed board: disabled, "Claim the board first (SSH claim, above): the lease needs to
  know whose key is whose." Behind a hub: read-only, "Behind a hub, the hub's lease already
  does this."
- Mode ON reuses every lease control (LeaseBadge, Take lease / Give back, RequestForm,
  RequestBar, HolderPrompts, QueueChip, ForceConfirm, TakenBanner, Close board, holderOnly
  greying). `serverWord(bid)` in week.js gives "the hub"/"the board".
- Broken at the touchscreen → TakenBanner: "<board>'s lease was ended at the board at
  14:32: someone switched off single-user mode on the touchscreen. The board is shared
  again." (`lease.taken {by:"someone at the board", reason, at}`).
- Board lease TTL default 15 min in HM settings `lease.board_ttl_min` (heartbeat every
  ttl/3), `lease.board_request_ttl_min` 60, idle give-back `lease.idle_release_min` shared.

## Architecture
- `session.lease` (pack hook row `("lease","board_lease","make_lease_adapter")`): the hub
  adapter when there is a hub, else `Mps3BoardLease` when the board reports `board_lease`;
  `Mps3Pack.lease_for(candidate)` beside `hub_for`.
- `harness_manager_mps3/board_lease.py`: `BoardLeaseClient` implementing `HubClientLike`
  (lease_show/acquire(poll queue)/heartbeat/release/cancel, principal, board_id, revoke,
  history, put_request/list_requests/delete_request/put_answer/get_answer) + `mode()` /
  `set_mode(on, take, confirm_board)`. Reads via 6900, writes via `Mps3Claim.ssh_argv`/the
  claim forward. Token preamble: `shell.py` preamble becomes a chain; the board-lease
  preamble adds `"lease": token` to gated requests when HM holds the board lease.
  Refusal `code` leased/lease_required/bad_token → `HeldError` with `data.reason="LEASE"`.
- `services/lease.py`: `require_hub` → `require_server` (keep alias); texts from
  `adapter.words`; `view()` adds `server {kind, host, name}` and `mode`; hub-only parts
  (hub_overview, on_hub_event) guarded by kind; LeaseStore keys (board address, board id).
- Gates: `update/lease_gate.lease_state` and `daemon/drive_gate.require_holder` use
  `lease_of` + `lease_required`; same for idle_api, quiet, xvc.check_lease, hil_runs;
  `adapter_for_job(..., drive=)` in app.py (lead wires); readings_api POST, consoles input,
  cosim gated.
- Daemon: `daemon/lease_mode_api.py` — `GET/POST /boards/{bid}/lease/mode`
  (`{single_user, confirm, confirm_board?}` → 202 job `lease_mode`); hub_api routes resolve
  via `lease_of`; events `lease.mode`, `lease.state.server`.
- CLI: existing `lease` verbs work on desk boards; new `harness-manager lease mode TARGET
  [on|off]`.

## Lanes (Sonnet; own files only; lead merges into integ/v1.2)
| Lane | Owns | Notes |
|---|---|---|
| HM-A BOARD-CLIENT | harness_manager_mps3/board_lease.py (new), pack.py (hook row + lease_for only), shell.py (preamble chain + refusal mapping only), tests/fakes/bl_board_lease.py (new in-memory board lease server + FakeShell shim), tests for these | builds against the pyverify F0 stubs' names; HM's vendored pyverify gets the real client later (P1) |
| HM-B SEAM+GATES | services/lease.py, services/update/lease_gate.py, daemon/drive_gate.py, daemon/idle_api.py, services/quiet.py, services/xvc.py (check_lease only), services/hil_runs.py (lease check only), tests | lease_backend.py is frozen: request changes from the lead |
| HM-C DAEMON+CLI | daemon/lease_mode_api.py (new), daemon/hub_api.py, cli/cmd_hub.py, cli/helptext.py (lease section), docs/API.md (lease sections), tests/fakes/l3_week_plan.py + t14_mock_api.py (lease rows only), tests | app.py EXTENSIONS line: send to the lead |
| HM-D UI | web/static/js/week.js, lease.js, sections/claim.js, sections/overview.js (Access/Lease card branch), sidebar.js (badge only), tests/web static tests | builds against t14_mock_api fields `server`, `mode`; strings frozen **Sat 12:00** for the guide |
| HM-E DOCS+HIL | docs/USER_GUIDE.md, CHANGELOG.md, docs/HIL_LINUX.md, docs/LEASE_REQUESTS.md (amendment) | after A-D merge |
