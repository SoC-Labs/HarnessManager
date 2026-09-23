// A board behind a hub (lane L1): the SSH tunnel's state and the lease, as chips in the
// header, with acquire and release. Nothing shows for a board that is not behind a hub.

import { runJob } from "./actions.js";
import { call } from "./api.js";
import { clock } from "./format.js";
import { html } from "./lib.js";
import { durationText, leaseLeft, loadHub, week } from "./week.js";
import { ActionRow, Chip, Icon } from "./ui.js";

export function leaseSpecs(bid) {
  const hub = week(bid).hub;
  const lease = hub && hub.lease;
  const label = !lease ? "Acquire lease" : lease.mine ? "Renew" : "Queue for it";
  return {
    acquire: {
      key: "lease_acquire", label, busyLabel: lease && !lease.mine ? "Queued..." : "Leasing...",
      budgetS: 120, command: `lease acquire${hub ? ` ${hub.host}` : ""}`,
      run: (ctx) => runJob("leaseTake", { bid }, {}, (d) => ctx.progress(d.phase || "waiting", d.phase), "lease"),
      render: (r) => [{ kind: "ok", text: r && r.lease
        ? `lease held on ${r.lease.target} until ${clock(r.lease.expires_at)}` : "lease held" }],
      onDone: () => loadHub(bid),
    },
    release: {
      key: "lease_release", label: "Release", busyLabel: "Releasing...", budgetS: 30,
      command: "lease release",
      run: async () => (await call("leaseRelease", { bid })).data,
      render: () => [{ kind: "ok", text: "lease released: other hub users may take the board" }],
      onDone: () => loadHub(bid),
    },
  };
}

const TUNNEL_LEVEL = { up: "ok", starting: "unk", down: "err" };

// The header's "Hub" fact: tunnel and lease chips, and the lease action that fits.
export function HubFact({ bid }) {
  const w = week(bid);
  const hub = w.hub;
  if (!hub) return null;
  const t = hub.tunnel;
  const lease = hub.lease;
  const left = leaseLeft(lease);
  const specs = leaseSpecs(bid);
  let leaseChip;
  if (!lease) {
    leaseChip = html`<${Chip} level="warn" icon="lock-open" testid="lease-chip" title=${`${hub.host}: nobody holds this board's lease`}>no lease<//>`;
  } else if (lease.mine) {
    leaseChip = html`<${Chip} level=${left !== null && left < 300 ? "warn" : "accent"} icon="lock" testid="lease-chip"
      title=${`${lease.target} on ${hub.host}, held by ${lease.holder}; the daemon renews it while the board is open`}>
      lease yours${left !== null ? ` · ${durationText(left)}` : ""}<//>`;
  } else {
    leaseChip = html`<${Chip} level="err" icon="lock" testid="lease-chip"
      title=${`${lease.target} on ${hub.host}, held by ${lease.holder}`}>leased to ${lease.holder || "someone else"}<//>`;
  }
  return html`<div class="fact hub-fact" data-testid="fact-hub">
    <span class="fact-label">Hub</span>
    <span class="fact-value row">
      <span class="mono secondary">${hub.host}</span>
      ${t ? html`<${Chip} level=${TUNNEL_LEVEL[t.state] || "unk"} icon=${t.state === "up" ? "cable" : "unplug"} testid="tunnel-chip"
        title=${`${t.via || "ssh"} to ${t.host || hub.host}${t.detail ? `: ${t.detail}` : ""}`}>tunnel ${t.state}<//>` : null}
      ${leaseChip}
      ${w.leaseQueued ? html`<span class="muted small"><${Icon} name="clock" cls="sm" /> queued</span>` : null}
      ${lease && lease.mine
        ? html`<${ActionRow} bid=${bid} panel="lease" spec=${specs.release} variant="ghost" compact=${true} showReason=${false} gate=${{}} />`
        : html`<${ActionRow} bid=${bid} panel="lease" spec=${specs.acquire} compact=${true} showReason=${false} gate=${{}} />`}
    </span>
  </div>`;
}
