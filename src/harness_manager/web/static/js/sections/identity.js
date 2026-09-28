// The Board tile's Identity row (lane BOARD-ID; docs/API.md "Board identity",
// docs/design/BOARD_IDENTITY.md). What the board says it is (label, IP, MAC), a warning when
// it clashes with another board or is not what its hub entry says, and "Fix identity": a
// dialog listing the changes, the typed phrase (the new label), then a 202 job that sets
// it, restarts the harness WARM (its reboot verb, never an MCC REBOOT) and reads it back.
// A board with no Ethernet harness has no info.net_identity, so nothing shows.

import { runJob } from "../actions.js";
import { call, toApiError } from "../api.js";
import { html, useEffect, useState } from "../lib.js";
import { boardState, changed, refreshInfo } from "../store.js";
import { Icon, Reason, Spinner } from "../ui.js";

export function identityOf(b) {
  return (b && b.netIdentity) || (b && b.info && b.info.net_identity) || null;
}

export async function loadIdentity(bid, { refresh = false } = {}) {
  const b = boardState(bid);
  if (b.netIdentityLoading) return;
  b.netIdentityLoading = true;
  changed();
  try {
    const { data } = await call("netIdentity", { bid }, undefined, refresh ? { refresh: "true" } : null);
    b.netIdentity = data.identity || null;
    b.netIdentityError = null;
  } catch (e) {
    b.netIdentityError = toApiError(e);
  } finally {
    b.netIdentityLoading = false;
    changed();
  }
}

export function identityLine(st) {
  const r = (st && st.reported) || {};
  const bits = [r.label || "no label", r.ip || "no IP", r.mac || "no MAC"];
  return bits.join(" · ");
}

// The worst findings first, in one sentence each.
function problemText(st) {
  const bad = (st.findings || []).filter((f) => f.level === "err" || f.level === "warn");
  if (!bad.length) return "";
  const head = st.status === "clash" ? "Identity clash: " : st.status === "unset" ? "Identity not set: " : "Not its hub entry: ";
  return head + bad.map((f) => f.text).join("; ") + ".";
}

function FixDialog({ bid, st, onClose }) {
  const fix = st.fix || {};
  const hub = st.hub || {};
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const ref = fix.refusal;
  const apply = async () => {
    setBusy(true); setErr(null);
    try {
      await runJob("netIdentityFix", { bid }, { confirm: typed.trim(), from_hub: true }, null, "identity");
      onClose();
      loadIdentity(bid);
      refreshInfo(bid);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="mt-8 identity-dialog" role="dialog" aria-label="Fix identity" data-testid="identity-dialog">
    <p class="small">Make this board match its hub entry${hub.target ? html` <span class="mono">${hub.target}</span>` : null}:</p>
    <ul class="small" data-testid="identity-changes">
      ${(fix.changes || []).map((c) => html`<li data-field=${c.field}><span class="mono">${c.field}</span>
        ${" "}<span class="mono">${c.from || "-"}</span> → <b class="mono">${c.to}</b></li>`)}
    </ul>
    ${(fix.notes || []).map((n) => html`<${Reason} text=${n} />`)}
    ${ref ? html`<${Reason} level="err" testid="identity-refusal"
        text=${`${ref.message}${ref.hint ? ` (${ref.hint})` : ""}`} />`
      : html`<${Reason} text="The harness then restarts (its reboot verb, warm: the FPGA is not reloaded, never an MCC REBOOT) and the identity is read back. It needs your lease and your claim, and waits for no card job." />
        <label class="small mt-8">Type <b class="mono">${fix.phrase}</b> to confirm${" "}
          <input type="text" class="mono" data-testid="identity-phrase" value=${typed} autocomplete="off"
            onInput=${(e) => setTyped(e.target.value)} /></label>`}
    <div class="mt-8">
      ${ref ? null : html`<button type="button" class="btn sm" data-action="identity-fix-confirm"
        disabled=${busy || typed.trim() !== fix.phrase} onClick=${apply}>
        ${busy ? html`<${Spinner} />` : null} Set and restart</button>`}
      <button type="button" class="btn ghost sm" data-action="identity-fix-cancel" disabled=${busy}
        onClick=${onClose}>Cancel</button>
    </div>
    ${err ? html`<div class="mt-8"><${Reason} level="err" testid="identity-fix-error"
      text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}`} /></div>` : null}
  </div>`;
}

export function IdentityTileRow({ bid }) {
  const b = boardState(bid);
  const st = identityOf(b);
  const [open, setOpen] = useState(false);
  const has = !!(b.info && b.info.net_identity);
  // One read of the board when it opens (the tile's own; info never asks the board or hub).
  useEffect(() => {
    if (has && !b.netIdentity && !b.netIdentityLoading && !b.netIdentityError) loadIdentity(bid);
  }, [bid, has]);
  if (!st) return null;
  const level = st.level === "err" ? "err" : st.level === "warn" ? "warn" : "";
  const problems = problemText(st);
  const changes = (st.fix && st.fix.changes) || [];
  return html`<span class="k">Identity</span>
    <span class="v" data-testid="tile-identity" data-status=${st.status}>
      <span class="mono">${identityLine(st)}</span>
      ${!st.live && st.checked_at ? html` <span class="muted small">(last check ${st.checked_at})</span>` : null}
      ${b.netIdentityLoading ? html` <${Spinner} />` : null}
      ${problems ? html`<div><${Reason} level=${level} testid="tile-identity-warning" text=${problems} /></div>` : null}
      ${changes.length && !open ? html` <button type="button" class="btn ghost sm" data-action="identity-fix"
        onClick=${() => setOpen(true)}><${Icon} name="sliders-horizontal" /> Fix identity</button>` : null}
      ${open ? html`<${FixDialog} bid=${bid} st=${st} onClose=${() => setOpen(false)} />` : null}
      ${b.netIdentityError ? html`<div><${Reason} level="err" testid="tile-identity-error"
        text=${`${b.netIdentityError.errName}: ${b.netIdentityError.message}`} /></div>` : null}
    </span>`;
}
