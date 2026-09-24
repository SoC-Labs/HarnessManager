# T8 hub mode: contract change requests

The exact diffs are in `T8_CCRS.patch`, next to this file. It is made against `8085488`: from the repo root, run `git apply --check docs/assessment/2026-09-24/T8_CCRS.patch`, then `git apply` it.

Proof: with every CCR applied (in a copy of the tree), the new tests `tests/integration/test_t8_ccr_wiring.py` pass, 8 of 8. They skip until the CCRs land, and each skip names the missing hook. The full suite passes on that copy too: 1987 passed, 11 skipped. One earlier run hit the t13 events-flood test under host load; it passes when run alone, with or without the CCRs.

| CCR | File (owner) | Change | Why |
|---|---|---|---|
| T8-1 | `harness_manager_mps3/hub.py` (LR-A) | `HubConfig.host` defaults to `""`. New: `HubConfig.rest` (a `hub_rest.RestHubConfig`) and `HubConfig.transport`. `parse_hub_table` accepts `hub_rest.REST_KEYS` and needs `url` or `host`. `Mps3Hub.client = hub_rest.client_for(cfg, ssh_factory=…)`. The share routes keep the config: a REST client lists and starts shares, and a REST-only hub (no `host`) reaches a share directly on `HUB:<port>` (`_ShareRoute.host`). | `url` means REST, `host` means SSH, both means REST. The selection lives in `hub_rest`; `hub.py` only calls it. |
| T8-2 | `harness_manager/daemon/hub_api.py` (LR-C) | On `session.opened`, a board whose hub client is REST with `events` on gets `hub_events.attach_bus(d.bus, bid, client, on_resync=leases.forget)`. It closes on `session.closed` and at daemon close. `d.bus.subscribe("hub.event", leases.on_hub_event)`. `d.hub_streams` exposes them. | Lease and queue changes then arrive by push, not by the 10 s poll. |
| T8-3 | `docs/CONTRACTS.md` (lead) | Appends the event topics `hub.event {type, ts, target, board, data}` and `hub.stream {state, detail, url, reconnects}`, and a "Hub mode" convention: `client.transport`, `notes_supported`/`notes_reason`, `can_revoke()`, `DirectReach`. | Event topics are append-only and contract-owned. |
| T8-4 | `harness_manager/services/lease.py` (LR-B) | Adds `forget(hub)` (public) and `on_hub_event(ev)`. The handler drops the cached view. When `lease.revoked`, `lease.admin_revoked` or `lease.expired` names our lease (the stored holder, or the client's `principal()`), it heartbeats that board now, which emits `lost`/`expired` and drops the token. | A force-release reaches the victim in about 1 s, not at the next heartbeat (up to 20 minutes away). |
| T8-5 | `harness_manager_mps3/tunnel.py` (L1, now LR-A) | `VIA_HUB = "hub"`. `parse_via`, `with_via` and `candidate_via` understand `"hub"`. `open_reach` sends a `via="hub"` link to `hub_reach.open_hub_reach`, with the SSH tunnel to `hub.host` as the fallback. | `via = "hub"` is the SSH-free data plane (docs/HUB_MODE.md). |

## For LR-B and LR-D, beyond the diffs

These are behaviour asks, not diffs, because those lanes are rewriting the same code now:

1. **Holder.** Use `hub.client.principal()` as the holder. The REST client returns it from the first acquire too (`Lease.holder`). It fixes `mine` in both transports (LEASE_REQUESTS.md "Who am I").
2. **Force.** Before offering force, call `getattr(hub.client, "can_revoke", None)`. Over REST, `(False, reason)` for a write token means `force_available=false`, with that reason.
3. **Notes.** Check `getattr(hub.client, "notes_supported", True)`. When it is False:
   - hide the message field and the Keep buttons, and show `notes_reason`;
   - `respond(keep)` gets `UnavailableError`;
   - the requester's deadline runs from its own note.
4. **Who took the board.** Over REST the victim learns `{by, reason, at}` from `hub.event` `lease.admin_revoked`, or from `lease_history()` (the client merges what the stream saw). Over SSH, `fpgahub target lease-history` never returns `lease.admin_revoked` and drops `by`/`reason`. This was measured on the real v0.3.0 app (`tests/fakes/t8_fpgahub_v030_golden.json`). The SSH-side source is the hub's `/var/log/fpgahub/lease_events.jsonl`.
5. **A revoke with no waiter** reads as `expired` at the heartbeat. Only the stream or a `lease.revoked` history record says "revoked".
