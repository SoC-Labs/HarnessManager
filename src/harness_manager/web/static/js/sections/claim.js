// The Board tile's SSH claim line (lane LINUX-CLAIM; docs/API.md "SSH claim of a Linux
// harness"). Only a Linux harness has one: info.claim is absent on bare metal, so nothing
// shows. "Claim this board" asks once more before it posts {confirm: true}: a claim gives
// your key root on the board, and the board refuses every later one.

import { runJob } from "../actions.js";
import { toApiError } from "../api.js";
import { html, useState } from "../lib.js";
import { boardState, refreshInfo } from "../store.js";
import { Icon, Reason, Spinner } from "../ui.js";

export function claimText(c) {
  if (!c) return "";
  const who = c.claimed || {};
  if (c.state === "mine") return `claimed by you${who.key_fp ? ` (${who.key_fp})` : ""}`;
  if (c.state === "other") return "claimed by another key";
  if (c.state === "unclaimed") return "unclaimed";
  return "unknown";
}

export function ClaimTileRow({ bid }) {
  const b = boardState(bid);
  const c = b.info && b.info.claim;
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  if (!c) return null;
  const changedKey = c.host_key && c.host_key.match === false;
  const claim = async () => {
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
  return html`<span class="k">SSH</span>
    <span class="v" data-testid="tile-claim" data-claim=${c.state}>
      <span class="mono">${claimText(c)}</span>
      ${!c.live && c.checked_at ? html` <span class="muted small">(last check ${c.checked_at})</span>` : null}
      ${changedKey ? html`<div><${Reason} level="err" testid="tile-claim-hostkey"
        text=${`Host key changed: pinned ${c.host_key.pinned}, the board reports ${c.host_key.reported}. SSH is refused.`} /></div>` : null}
      ${c.state === "unclaimed" && !armed ? html` <button type="button" class="btn ghost sm" data-action="claim"
        onClick=${() => setArmed(true)}><${Icon} name="lock" /> Claim this board</button>` : null}
      ${c.state === "unclaimed" && armed ? html`<div class="mt-8"><${Reason} text="Your SSH key gets root on this board; the board then refuses every other key's claim and takes slot changes only over that key's SSH." />
        <button type="button" class="btn sm" data-action="claim-confirm" disabled=${busy} onClick=${claim}>
          ${busy ? html`<${Spinner} />` : null} Claim with my key</button>
        <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => setArmed(false)}>Cancel</button></div>` : null}
      ${err ? html`<div class="mt-8"><${Reason} level="err" testid="tile-claim-error"
        text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}`} /></div>` : null}
    </span>`;
}
