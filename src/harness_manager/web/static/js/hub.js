// A board behind a hub (lane L1): the SSH tunnel's state and the lease, as chips in the
// header, with acquire and release. Nothing shows for a board that is not behind a hub.

import { runJob } from "./actions.js";
import { call } from "./api.js";
import { clock } from "./format.js";
import { html } from "./lib.js";
import { boardState } from "./store.js";
import { durationText, epochOf, leaseLeft, loadHub, week } from "./week.js";
import { ActionRow, Chip, Icon } from "./ui.js";

// POST .../lease {ttl_s?} (60-86400; the daemon's default is 3600 and it heartbeats the lease
// while the board is open). The job may QUEUE behind another holder: it then holds the board,
// and DELETE .../lease cancels it ({cancelled: true}).
export function leaseSpecs(bid) {
  const hub = week(bid).hub;
  const lease = hub && hub.lease;
  const label = !lease ? "Acquire lease" : lease.mine ? "Renew" : "Queue for it";
  return {
    acquire: {
      key: "lease_acquire", label, busyLabel: lease && !lease.mine ? "Queued..." : "Leasing...",
      budgetS: 600, command: `lease acquire${hub ? ` ${hub.host}` : ""}`,
      run: (ctx) => runJob("leaseTake", { bid }, {}, (d) => ctx.progress(
        d.phase === "queued" ? `queued${d.done ? ` (position ${d.done})` : ""}` : d.phase || "waiting", d.phase), "lease"),
      render: (r) => [{ kind: "ok", text: r && r.lease
        ? `lease ${r.already ? "already " : ""}held on ${r.lease.target}${epochOf(r.lease.expires_at) ? ` until ${clock(epochOf(r.lease.expires_at))}` : ""}`
        : "lease held" }],
      onDone: () => loadHub(bid),
    },
    release: {
      key: "lease_release", label: "Release", busyLabel: "Releasing...", budgetS: 30,
      command: "lease release",
      run: async () => (await call("leaseRelease", { bid })).data,
      render: (r) => [{ kind: "ok", text: r && r.cancelled
        ? "the queued lease request was cancelled; its queue entry is removed"
        : `lease${r && r.released && r.released.target ? ` on ${r.released.target}` : ""} released: other hub users may take the board` }],
      onDone: () => loadHub(bid),
    },
    cancel: {
      key: "lease_cancel", label: "Cancel", busyLabel: "Cancelling...", budgetS: 30,
      command: "lease release (cancel the queued request)",
      run: async () => (await call("leaseRelease", { bid })).data,
      render: (r) => [{ kind: "ok", text: r && r.cancelled
        ? "the queued lease request was cancelled; the board is free again" : "nothing was queued" }],
      onDone: () => loadHub(bid),
    },
  };
}

const TUNNEL_LEVEL = { up: "ok", starting: "unk", down: "err" };

function tunnelTitle(t, hub) {
  const parts = [`${t.via || "ssh"} to ${t.host || hub.host}`];
  const fwd = t.forwards || t.ports;
  if (fwd && typeof fwd === "object") parts.push(`${Object.keys(fwd).length} port(s) forwarded`);
  if (t.restarts) parts.push(`restarted ${t.restarts} time(s)`);
  if (t.shares && typeof t.shares === "object") parts.push(`${Object.keys(t.shares).length} hub share(s)`);
  if (t.detail) parts.push(t.detail);
  return parts.join("; ");
}

// The header's "Hub" fact: tunnel and lease chips, and the lease action that fits.
export function HubFact({ bid }) {
  const w = week(bid);
  const hub = w.hub;
  if (!hub) return null;
  const t = hub.tunnel;
  const lease = hub.lease;
  const left = leaseLeft(lease);
  const specs = leaseSpecs(bid);
  const job = boardState(bid).job;
  const acquiring = !!(job && job.kind === "lease");        // it may be queued: offer Cancel
  let leaseChip;
  if (!lease) {
    leaseChip = html`<${Chip} level="warn" icon="lock-open" testid="lease-chip" title=${`${hub.host}: nobody holds this board's lease`}>no lease<//>`;
  } else if (lease.mine) {
    leaseChip = html`<${Chip} level=${left !== null && left < 300 ? "warn" : "accent"} icon="lock" testid="lease-chip"
      title=${`${lease.target} on ${hub.host}, held by ${lease.holder}${lease.user ? ` (user ${lease.user})` : ""}; the daemon renews it while the board is open`}>
      lease yours${left !== null ? ` · ${durationText(left)}` : ""}<//>`;
  } else {
    leaseChip = html`<${Chip} level="err" icon="lock" testid="lease-chip"
      title=${`${lease.target} on ${hub.host}, held by ${lease.holder}${lease.user ? ` (user ${lease.user})` : ""}${left !== null ? `; theirs ends in ${durationText(left)}` : ""}`}>leased to ${lease.holder || "someone else"}<//>`;
  }
  return html`<div class="fact hub-fact" data-testid="fact-hub">
    <span class="fact-label">Hub</span>
    <span class="fact-value row">
      <span class="mono secondary">${hub.host}</span>
      ${t ? html`<${Chip} level=${TUNNEL_LEVEL[t.state] || "unk"} icon=${t.state === "up" ? "cable" : "unplug"} testid="tunnel-chip"
        title=${tunnelTitle(t, hub)}>tunnel ${t.state}<//>` : null}
      ${leaseChip}
      ${w.leaseQueued ? html`<span class="muted small" data-testid="lease-queued"><${Icon} name="clock" cls="sm" /> queued</span>` : null}
      ${lease && lease.mine && !acquiring
        ? html`<${ActionRow} bid=${bid} panel="lease" spec=${specs.release} variant="ghost" compact=${true} showReason=${false} gate=${{}} />`
        : html`<${ActionRow} bid=${bid} panel="lease" spec=${specs.acquire} compact=${true} showReason=${false} gate=${{}} />`}
      ${acquiring ? html`<${ActionRow} bid=${bid} panel="lease_cancel" spec=${specs.cancel} variant="ghost" compact=${true}
        showReason=${false} gate=${{ whileJob: true }} />` : null}
    </span>
  </div>`;
}
