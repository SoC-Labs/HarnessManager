// The SSH claim (lane LINUX-CLAIM; docs/API.md "SSH claim of a Linux harness"), on Board >
// Access. Only a Linux harness has one: info.claim is absent on bare metal, so nothing shows.
// "Claim with my key…" asks once more before it posts {confirm: true}: a claim gives your key
// root on the board, and the board refuses every later one.

import { gateReason, interlock, runJob } from "../actions.js";
import { toApiError } from "../api.js";
import { html, useState } from "../lib.js";
import { boardState, refreshInfo } from "../store.js";
import { Card, Chip, Icon, Reason, Spinner } from "../ui.js";

// --- Board › Access (UI v2, lane UI2-BOARD): the SSH claim as its own card -----------------------


export function claimOf(b) { return (b && b.info && b.info.claim) || null; }

// The board's SSH link ("root@192.168.10.104"), as its candidate names it.
function sshAddress(b) {
  const links = ((b.info && b.info.candidate) || {}).links || [];
  const l = links.find((x) => x.kind === "ssh");
  return l ? l.address : "";
}

// Only a Linux harness has a claim (info.claim is absent on bare metal): nothing shows then.
export function ClaimCard({ bid }) {
  const b = boardState(bid);
  const c = claimOf(b);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  if (!c) return null;
  const who = c.claimed || {};
  const changedKey = c.host_key && c.host_key.match === false;
  const seenBefore = changedKey && c.host_key.seen_before ? String(c.host_key.seen_before).slice(0, 10) : "";
  // R3: a claim needs this board's lease on a hub board (the service says 409 HELD too).
  const why = gateReason(bid, "claim", "claim", { holder: "SSH claim" });
  const claim = async () => {
    if (why) { interlock(bid, "claim", "board claim", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      await runJob("claim", { bid }, { confirm: true }, null, "claim");
      setArmed(false);
      refreshInfo(bid);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  const ssh = sshAddress(b);
  return html`<${Card} title="SSH claim" icon="lock" testid="access-claim"
      sub="Who may log in to the board's Linux over SSH. Once claimed, slot and card changes are accepted only through it.">
    <div class="stack gap-8" data-claim=${c.state}>
      <dl class="kv">
        <dt>State</dt><dd>${c.state === "mine" ? html`<${Chip} level="ok" icon="lock">Claimed by you<//>`
          : c.state === "other" ? html`<${Chip} level="held" icon="lock">Claimed by another key<//>`
          : c.state === "unclaimed" ? html`<${Chip} level="warn" icon="lock-open">Not claimed<//>`
          : html`<${Chip} level="unk" icon="circle-help">Unknown<//>`}
          ${!c.live && c.checked_at ? html` <span class="muted small">last check ${c.checked_at}</span>` : null}</dd>
        ${who.key_fp ? html`<dt>Key</dt><dd class="mono small">${who.key_fp}</dd>` : null}
        ${who.by ? html`<dt>Claimed by</dt><dd class="small">${who.by}${who.at ? ` · ${String(who.at).slice(0, 16).replace("T", " ")}` : ""}</dd>` : null}
        ${ssh ? html`<dt>Log in</dt><dd class="mono small">${ssh}</dd>` : null}
      </dl>
      ${changedKey && seenBefore ? html`<${Reason} level="warn" testid="claim-hostkey"
        text=${`Host key changed back to one seen on ${seenBefore} (${c.host_key.reported}); on the Linux harness this is usually /persist (the user microSD) mounting or not. SSH is refused until you re-pin it: ${c.state === "unclaimed" ? "board claim" : "board claim --adopt"}, if you trust it.`} />` : null}
      ${changedKey && !seenBefore ? html`<${Reason} level="err" testid="claim-hostkey"
        text=${`Host key changed: pinned ${c.host_key.pinned}, the board reports ${c.host_key.reported}. SSH is refused.`} />` : null}
      ${c.state === "unclaimed" ? html`<${Reason} level="warn" text="Not claimed: anyone with the image's default key can log in." />` : null}
      ${(c.notes || []).map((n) => html`<${Reason} key=${n} text=${n} />`)}
      ${c.state === "unclaimed" ? html`<div class="bt-foot">
        ${armed ? html`<${Reason} text="Your SSH key gets root on this board; the board then refuses every other key's claim and takes slot changes only over that key's SSH." />
            <button type="button" class="btn sm primary" data-action="access-claim-confirm" disabled=${busy} onClick=${claim}
              aria-disabled=${why ? "true" : undefined}>${busy ? html`<${Spinner} />` : null} Claim with my key</button>
            <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => setArmed(false)}>Cancel</button>`
          : html`<button type="button" class="btn sm" data-action="access-claim" aria-disabled=${why ? "true" : undefined}
              title=${why || "Claim the board's SSH with your key"}
              onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board claim", why); return; } setArmed(true); }}>
              <${Icon} name="lock" /> Claim with my key…</button>`}
        ${why ? html`<span class="small muted" data-testid="reason-access-claim">${why}</span>` : null}</div>` : null}
      ${err ? html`<${Reason} level="err" testid="access-claim-error"
        text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}${err.holder ? ` (holder: ${err.holder})` : ""}`} />` : null}
    </div><//>`;
}
