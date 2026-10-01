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
import { holderOnly } from "../week.js";
import { Card, Icon, Reason, Spinner } from "../ui.js";
import { ModalShell, openModal, registerModal } from "../modal.js";

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
  const text = bad.map((f) => f.text).join("; ");
  // V7-ALIGN: the finding already says "identity not set (default label, ...)": not twice.
  const lead = head.slice(0, -2).toLowerCase();
  if (text.toLowerCase().startsWith(lead)) return text.charAt(0).toUpperCase() + text.slice(1) + ".";
  return head + text + ".";
}

function FixDialog({ bid, st, onClose }) {
  const fix = st.fix || {};
  const hub = st.hub || {};
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const ref = fix.refusal;
  // R3: the fix restarts the harness, so it is the lease holder's (the service refuses too).
  const leaseWhy = holderOnly(bid, "Fix identity");
  const apply = async () => {
    if (leaseWhy) return;
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
      ${ref || !leaseWhy ? null : html`<${Reason} level="held" icon="lock" testid="identity-fix-lease" text=${leaseWhy} />`}
      ${ref ? null : html`<button type="button" class="btn sm" data-action="identity-fix-confirm"
        disabled=${busy || typed.trim() !== fix.phrase || !!leaseWhy} onClick=${apply}>
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

// --- Board › Access (UI v2, lane UI2-BOARD): the identity card and the Fix identity dialog ----------
//
// What the board says it is (label, IP, MAC: net_identity when the harness reports one), the
// clashes G10 found across every board this Harness Manager has seen, and Fix identity… as a
// dialog (M4: the same changes, typed phrase and job as the tile's). Not the same as Identify
// (the blink).

function FixIdentityModal({ bid, close }) {
  const b = boardState(bid);
  const st = identityOf(b);
  return html`<${ModalShell} title="Fix identity" icon="tag" testid="fixid-modal">
    ${st ? html`<${FixDialog} bid=${bid} st=${st} onClose=${close} />`
      : html`<${Reason} text="The board's identity is not read yet." />`}
  <//>`;
}

registerModal("fixid", FixIdentityModal);

function ethAddress(b) {
  const links = ((b.info && b.info.candidate) || {}).links || [];
  const l = links.find((x) => x.kind === "ethernet");
  return l ? String(l.address).split(":")[0] : "";
}

export function IdentityCard({ bid, clashes = [], goTo = null }) {
  const b = boardState(bid);
  const st = identityOf(b);
  const has = !!(b.info && b.info.net_identity);
  useEffect(() => {
    if (has && !b.netIdentity && !b.netIdentityLoading && !b.netIdentityError) loadIdentity(bid);
  }, [bid, has]);
  const r = (st && st.reported) || {};
  const bare = !!(b.info && (b.info.identity || {}).harness_impl && b.info.identity.harness_impl !== "linux");
  const nothing = bare ? "not reported by the bare-metal harness" : "not reported";
  const problems = st ? problemText(st) : "";
  const changes = (st && st.fix && st.fix.changes) || [];
  const level = st && st.level === "err" ? "err" : st && st.level === "warn" ? "warn" : "";
  return html`<${Card} title="Identity" icon="tag" testid="access-identity"
      sub="Who this board says it is: its label, address and MAC. Not the same as Identify (the blink).">
    <div class="stack gap-8" data-status=${(st && st.status) || "none"}>
      <dl class="kv">
        <dt>Label</dt><dd>${r.label || html`<span class="muted">${nothing}</span>`}${r.source === "default" ? html` <span class="sub">image default</span>` : null}</dd>
        <dt>IP</dt><dd class="mono">${r.ip || ethAddress(b) || html`<span class="muted">${nothing}</span>`}</dd>
        <dt>MAC</dt><dd class="mono">${r.mac || html`<span class="muted" style="font-family:var(--font-sans)">${nothing}</span>`}</dd>
        ${st && st.hub && st.hub.target ? html`<dt>Hub entry</dt><dd class="small"><span class="mono">${st.hub.target}</span>${st.hub.label ? ` · ${st.hub.label}` : ""}${st.hub.board_ip ? ` · ${st.hub.board_ip}` : ""}</dd>` : null}
        ${st && !st.live && st.checked_at ? html`<dt>Checked</dt><dd class="small muted">${st.checked_at} (the last check)</dd>` : null}
      </dl>
      ${clashes.map((c) => {
        const others = (c.boards || []).filter((x) => x.board_id !== bid);
        const name = others.map((x) => x.name || x.target || x.board_id).join(", ");
        const go = others.find((x) => x.board_id && x.kind === "board");
        return html`<div class="stack gap-8" key=${`${c.field}:${c.value}`} data-testid="access-clash" data-field=${c.field}>
          <${Reason} level="err" text=${`Identity clash with ${name}: the same ${c.field === "mac" ? "MAC" : c.field === "ip" ? "IP" : "label"} ${c.value}.`} />
          ${!changes.length && go && goTo ? html`<div><button type="button" class="btn sm" data-action="access-go-clash"
            onClick=${() => goTo(go.board_id)}>Go to ${go.name || go.board_id}</button></div>` : null}</div>`;
      })}
      ${problems ? html`<${Reason} level=${level} testid="access-identity-warning" text=${problems} />` : null}
      ${changes.length ? html`<div><button type="button" class="btn sm primary" data-action="access-identity-fix"
        onClick=${() => openModal("fixid", { bid })}><${Icon} name="tag" /> Fix identity…</button></div>` : null}
      ${b.netIdentityError ? html`<${Reason} level="err" text=${`${b.netIdentityError.errName}: ${b.netIdentityError.message}`} />` : null}
      ${!has && b.info && !bare ? html`<p class="small muted">This harness image does not report its identity (net-protocol v0.16 adds it).</p>` : null}
    </div><//>`;
}
