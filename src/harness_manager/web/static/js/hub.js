// A board behind a hub (lane L1): the SSH tunnel's state and the lease, as chips in the
// header, with acquire and release. Nothing shows for a board that is not behind a hub.
// Someone else's lease offers "Request board" (lane LR-D, lease.js): the holder is asked,
// and the request's own bar (queue position, countdown, Leave queue, Force) sits above.

import { runJob } from "./actions.js";
import { openRequestForm, ReleaseButton, requestActive } from "./lease.js";
import { call } from "./api.js";
import { clock } from "./format.js";
import { html } from "./lib.js";
import { boardState } from "./store.js";
import {
  durationText, epochOf, leaseLeft, leaseName, leaseWhere, leaseWho, loadHub, staleNote, week,
} from "./week.js";
import { ActionRow, Chip, Icon } from "./ui.js";

// POST .../lease {ttl_s?} (60-86400; the daemon's default is 3600 and it heartbeats the lease
// while the board is open). The job may QUEUE behind another holder: it then holds the board,
// and DELETE .../lease cancels it ({cancelled: true}).
export function leaseSpecs(bid) {
  const hub = week(bid).hub;
  const lease = hub && hub.lease;
  const label = !lease ? "Acquire lease" : "Renew";
  return {
    acquire: {
      key: "lease_acquire", label, busyLabel: "Leasing...",
      budgetS: 600, command: `lease acquire${hub ? ` ${hub.host}` : ""}`,
      run: (ctx) => runJob("leaseTake", { bid }, {}, (d) => ctx.progress(
        d.phase === "queued" ? `queued${d.done ? ` (position ${d.done})` : ""}` : d.phase || "waiting", d.phase), "lease"),
      // LEASE-BOARD: name the physical board (mps3_01) when the daemon says it, else the target
      render: (r) => [{ kind: "ok", text: r && r.lease
        ? `lease ${r.already ? "already " : ""}held on ${leaseName(r.lease)}${epochOf(r.lease.expires_at) ? ` until ${clock(epochOf(r.lease.expires_at))}` : ""}`
        : "lease held" }],
      onDone: () => loadHub(bid),
    },
    release: {
      key: "lease_release", label: "Release", busyLabel: "Releasing...", budgetS: 30,
      command: "lease release",
      run: async () => (await call("leaseRelease", { bid })).data,
      render: (r) => [{ kind: "ok", text: r && r.cancelled
        ? "the queued lease request was cancelled; its queue entry is removed"
        : `lease${r && r.released && leaseName(r.released) ? ` on ${leaseName(r.released)}` : ""} released: other hub users may take the board` }],
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

// The header's "Hub" fact: tunnel and lease chips, and the lease action that fits. LEASE-UI:
// "yours" is THIS Harness Manager holding the token (`here`); the same hub principal in
// another session is someone else here, named as such, with no Release (only that session can).
export function HubFact({ bid }) {
  const w = week(bid);
  const hub = w.hub;
  if (!hub) return null;
  const t = hub.tunnel;
  const lease = hub.lease;
  const who = leaseWho(bid);
  const where = leaseWhere(who);        // LEASE-BOARD: "mps3_01 on HUB (target mps3_01_pl)"
  const left = leaseLeft(lease);
  const specs = leaseSpecs(bid);
  const job = boardState(bid).job;
  const acquiring = !!(job && job.kind === "lease");        // it may be queued: offer Cancel
  const requesting = requestActive(bid);
  const req = hub.request;
  let leaseChip;
  if (who.state === "unknown") {
    leaseChip = html`<${Chip} level="unk" icon="circle-help" testid="lease-chip" title=${`${hub.host}: the lease could not be read: ${who.error}`}>lease unknown<//>`;
  } else if (!lease) {
    leaseChip = html`<${Chip} level="warn" icon="lock-open" testid="lease-chip" title=${who.board ? `${hub.host}: nobody holds the lease on ${who.board}` : `${hub.host}: nobody holds this board's lease`}>no lease<//>`;
  } else if (who.state === "here") {
    leaseChip = html`<${Chip} level=${left !== null && left < 300 ? "warn" : "accent"} icon="lock" testid="lease-chip"
      title=${`${where}, held by ${lease.holder}${lease.user ? ` (user ${lease.user})` : ""}: this Harness Manager holds it and renews it while the board is open${who.stale ? `; ${staleNote(who.stale)}` : ""}`}>
      lease yours${left !== null ? ` · ${durationText(left)}` : ""}<//>`;
  } else if (who.state === "elsewhere") {
    leaseChip = html`<${Chip} level="held" icon="lock" testid="lease-chip"
      title=${`${where}, held under your hub name by another session or tool (a soak, a runner), not this Harness Manager${left !== null ? `; it ends in ${durationText(left)}` : ""}. Release it there.`}>
      leased to ${lease.holder} (another session)<//>`;
  } else {
    leaseChip = html`<${Chip} level="err" icon="lock" testid="lease-chip"
      title=${`${where}, held by ${lease.holder}${lease.user ? ` (user ${lease.user})` : ""}${left !== null ? `; theirs ends in ${durationText(left)}` : ""}`}>leased to ${lease.holder || "someone else"}<//>`;
  }
  const acquireRow = html`<${ActionRow} bid=${bid} panel="lease" spec=${specs.acquire} compact=${true} showReason=${false} gate=${{}} />`;
  let action;
  if (who.state === "here") action = acquiring ? acquireRow : html`<${ReleaseButton} bid=${bid} />`;
  else if (who.state === "elsewhere") {
    action = html`<span class="muted small" data-testid="lease-elsewhere-note">only that session can release it</span>`;
  } else if (lease) action = requesting ? null : html`<${RequestButton} bid=${bid} />`;
  else if (who.state === "unknown") action = null;      // read it again first: it may be held
  else action = acquireRow;
  return html`<div class="fact hub-fact" data-testid="fact-hub">
    <span class="fact-label">Hub</span>
    <span class="fact-value row">
      <span class="mono secondary">${hub.host}</span>
      ${t ? html`<${Chip} level=${TUNNEL_LEVEL[t.state] || "unk"} icon=${t.state === "up" ? "cable" : "unplug"} testid="tunnel-chip"
        title=${tunnelTitle(t, hub)}>tunnel ${t.state}<//>` : null}
      ${leaseChip}
      ${who.stale ? html`<span class="muted small" data-testid="lease-stale"
        title=${who.stale.error ? `The last read: ${who.stale.error}` : ""}>${staleNote(who.stale)}</span>` : null}
      ${requesting ? html`<span class="muted small" data-testid="lease-requested"><${Icon} name="send" cls="sm" /> requested${req && req.position ? ` · position ${req.position}` : ""}</span>`
        : w.leaseQueued ? html`<span class="muted small" data-testid="lease-queued"><${Icon} name="clock" cls="sm" /> queued</span>` : null}
      ${action}
      ${acquiring ? html`<${ActionRow} bid=${bid} panel="lease_cancel" spec=${specs.cancel} variant="ghost" compact=${true}
        showReason=${false} gate=${{ whileJob: true }} />` : null}
    </span>
  </div>`;
}

// "Request board": opens the small form (lease.js) that asks the holder to give it up.
export function RequestButton({ bid, compact = true }) {
  return html`<button type="button" class=${`btn ${compact ? "sm" : ""}`} data-action="lease_request_open"
    aria-haspopup="dialog" title="Join the queue and ask the holder to give the board up"
    onClick=${(e) => openRequestForm(bid, e.currentTarget)}><${Icon} name="send" /> Request board</button>`;
}
