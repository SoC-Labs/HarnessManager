// The Workbench's Program strip (UI v2, docs/design/ui-v2/prototype-b-round3.html "ProgramStrip";
// plan §1.3 W1-W7): the design picker (a combo as wide as its field, with a filter and tags),
// the inline preflight, Arm -> Program and Restore baseline, the Card line (only where there
// is a card), and ONE outcome box that is the download bar while a deploy runs.
//
// Everything shown comes from the daemon:
// - the designs: GET /overlays (loadable here, and the ones keyed to another shell, which the
//   preflight refuses); tags from each OverlayRef: "loaded now" (the board's rm_id), "ILAs" (an
//   .ltx travels with it, `ltx_sha256`; no count: the names need the .ltx read), "kit-built"
//   (a build receipt, `receipt_sha256`), "imported" (in the content store, `source` "store:..."),
//   "baseline" (rm_id 0);
// - the preflight: POST /preflight's items (Program is refused while any is MISMATCH: the deploy
//   service's own rule, core.pack.preflight_refusal); the lease chip is the page's lease state;
// - the bar: the deploy.* events (store.js onDeployEvent: phase, bytes of total, the rate and
//   time left from the events' own `at`), and each phase's time from the same events (below).
//
// Keep on the card (L1, decided 2026-09-25: off by default). The tick box shows only when the
// harness reports "usd" and GET /card says a card is in the slot. Ticked, Program sends
// keep_on_card: true and the outcome says whether the design was kept (and in which slot).
//
// Lease gating (R3): Program and Restore baseline go through gateReason with `holder`
// (actions.js), so on a hub board only this Harness Manager holding the lease drives them.
//
// FIX-PACK-7 (DEBUG-DOWN-FIRST): before every swap the daemon asks OpenOCD on a claimed Linux
// board down; when it cannot, the job is REFUSED (15) with error.data.debug_down. The strip then
// offers "Program anyway" (or "Restore anyway"): armed like Program (tick Arm, then click), the
// same gateReason (lease holder, preflight), and it sends `force: true`. A swap that went on
// despite a failed down says so (deploy.warning) in the outcome.
//
// FIX-PACK-8 (david: "warn + typed OK"): a design whose board pack declares that its boot code
// writes the DUT's flash (the OverlayRef's `writes_dut_flash`: {why, word}; on the MPS3,
// nanosoc_multicore until Linux v2.1) shows the pack's warning above Program on EVERY program,
// with a text field: Program (and Program anyway) stays off until the word is typed exactly
// (programGuard, so still behind the lease holder rule and Arm), then sends
// `allow_dut_flash_write: true`. Each program is a fresh choice: the field empties on a new pick
// and once Program runs. Core knows no design names: the words are the pack's.

import { gateReason, interlock, isArmed, panelState, runAction, runJob, setArmed } from "../actions.js";
import { bytesText, capState, elapsedSince, hexId, kib } from "../format.js";
import { html, useEffect, useRef } from "../lib.js";
import { openModal } from "../modal.js";
import {
  boardState, changed, hasCardStore, loadCard, loadOverlays, onBoardEvent, runPreflight, S, toast,
} from "../store.js";
import { Icon, ResultBlock, Spinner, useReveal } from "../ui.js";
import { holderOnly, leaseWho } from "../week.js";

const ARM = "program";
const PANEL = "program";
export const PHASES = ["guard", "swap", "push", "verify"];
const CARD_PHASE = "card";           // the card write, only when the deploy keeps the design
const WEIGHT = { guard: 1, swap: 1, push: 2.2, verify: 1, card: 1.6 };
const BYTE_PHASES = new Set(["push", "card"]);
// What each phase does (harness_manager_mps3/deploy.py: guard = the preflight assessment, swap =
// the shell parks the swap, push = clearing + partial bytes, verify = the swap's reply read
// back, card = the D13 commit to the card's free slot).
const SAYS = {
  guard: "checking the shell, rm_id, size and lease",
  swap: "the shell parks the swap: partition decoupled, DUT held",
  verify: "reading rm_id back from the board",
  card: "writing the pair to the card's free slot",
};
// Bare metal's Ethernet push, measured on silicon (the over-the-wire reconfiguration: ~570 KB/s).
// Only an estimate in the preflight ("push ~0.7 s"); the bar shows the real rate. No Linux rate
// is measured, so a Linux board quotes none.
const BM_PUSH_RATE = 570e3;

// --- the report of a deploy (the outcome's command lines) -------------------------------------

// The report line for a deploy's `card` ({kept, slot, why}); "" when it was not asked to keep.
export function cardText(card) {
  if (!card) return "";
  if (card.kept) return card.slot ? `Kept on the card (slot ${card.slot})` : "Kept on the card";
  return `Not kept: ${card.why || "no reason given"}`;
}

function renderDeploy(result) {
  if (!result || typeof result !== "object") return [{ kind: "out", text: String(result) }];
  const verdict = result.verified ? "verified by the board" : "WRITTEN, NOT VERIFIED";
  const lines = [
    { kind: result.verified ? "ok" : "warnline", text: `rm_id ${result.rm_id}: ${verdict}` },
    { kind: "out", text: `transport ${result.transport || "?"}, ${Number(result.seconds || 0).toFixed(1)} s` },
  ];
  if (result.card) lines.push({ kind: result.card.kept ? "ok" : "warnline", text: cardText(result.card) });
  return lines;
}

// --- the phases' own times, from the deploy events -------------------------------------------------
//
// store.js keeps the running phase, its bytes and the rate; the strip also says how long each
// phase took ("push 0.7 s") and the push's own rate once it is over. Both come from the events'
// `at` (the daemon's clock), never from this page's.

const timing = {};                   // bid -> {startedAt, order: [phase], at: {phase: [t0, t1]}, bytes, totals: {phase: n}}

function phaseTimes(bid) {
  if (!timing[bid]) timing[bid] = { startedAt: 0, order: [], at: {}, bytes: {}, totals: {} };
  return timing[bid];
}

onBoardEvent((ev) => {
  if (!ev.board_id || !ev.topic.startsWith("deploy.")) return;
  const t = phaseTimes(ev.board_id);
  const at = Number(ev.at) || Date.now() / 1000;
  const d = ev.data || {};
  const close = () => {
    const last = t.order[t.order.length - 1];
    if (last && t.at[last]) t.at[last][1] = at;
  };
  if (ev.topic === "deploy.started") {
    timing[ev.board_id] = { startedAt: at, order: [], at: {}, bytes: {}, totals: {} };
    if (uiOf(ev.board_id).anyway) { uiOf(ev.board_id).anyway = null; changed(); }   // swapping now
  } else if (ev.topic === "deploy.progress") {
    const ph = d.phase || "?";
    if (!t.startedAt) t.startedAt = at;
    if (!t.at[ph]) {
      close();
      t.order.push(ph);
      t.at[ph] = [at, at];
    } else {
      t.at[ph][1] = at;
    }
    t.bytes[ph] = Number(d.bytes || 0);
    t.totals[ph] = Number(d.total || 0);
  } else if (ev.topic === "deploy.done" || ev.topic === "deploy.failed") {
    close();
    t.doneAt = at;
  }
});

function phaseSecs(t, ph) {
  const span = t && t.at[ph];
  return span ? Math.max(0, span[1] - span[0]) : null;
}

const fmtSecs = (s) => (s === null || s === undefined ? "" : s < 9.95 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`);
const fmtRate = (bps) => `${Math.round(bps / 1000).toLocaleString("en-GB")} KB/s`;

// --- the picker's rows -------------------------------------------------------------------------------

function loadedId(b) {
  const id = b.info && b.info.identity && b.info.identity.rm_id;
  return id ? String(id).toLowerCase() : "";
}

function sameRm(a, c) { return !!a && !!c && String(a).toLowerCase() === String(c).toLowerCase(); }

// [{name, ov, blocked}]: the designs that load on this shell, then the ones keyed to another
// (still pickable: the preflight says why Program refuses them).
export function designRows(b) {
  const o = b.overlays;
  if (!o) return [];
  const out = (o.loadable || []).map((ov) => ({ name: ov.name, ov, blocked: "" }));
  for (const [name, reason] of Object.entries(o.blocked || {})) {
    const full = (o.all || []).find((x) => x.name === name) || null;
    out.push({ name, ov: full, blocked: reason });
  }
  return out;
}

// The tags a design carries, from its OverlayRef only.
export function designTags(b, ov, { full = true } = {}) {
  if (!ov) return [];
  const tags = [];
  if (full && sameRm(ov.rm_id, loadedId(b))) tags.push({ k: "now", text: "loaded now", title: "The design in the partition now" });
  if (ov.ltx_sha256) tags.push({ k: "ila", text: "ILAs", title: "Its ILA probes file (.ltx) travels with it: Logic analysers can open it" });
  if (ov.receipt_sha256) tags.push({ k: "kit", text: "kit-built", title: "Built with the DUT kit: its build receipt travels with it" });
  if (String(ov.source || "").startsWith("store:")) tags.push({ k: "imp", text: "imported", title: "Imported into this Harness Manager's content store" });
  if (full && /^0x0+$/i.test(String(ov.rm_id || ""))) tags.push({ k: "base", text: "baseline", title: "The board's safe design (Restore baseline loads it)" });
  // FIX-PACK-8: the pack says its boot code writes the DUT's flash (Program asks for a typed word)
  if (dutFlashOf(ov)) tags.push({ k: "dutflash", text: "writes DUT flash", title: dutFlashOf(ov).why });
  return tags;
}

function Tags({ tags }) {
  return tags.map((t) => html`<span key=${t.k} class=${`tag ${t.k}`} title=${t.title}>${t.text}</span>`);
}

// What to send as `overlay`: the OverlayRef itself when the list gave one (exact even when
// two overlays share a name across shells), else the name (harness-manager-daemon resolves both).
function overlaySpec(b, name) {
  const o = b.overlays;
  const ref = o && ((o.loadable || []).find((x) => x.name === name) || (o.all || []).find((x) => x.name === name));
  return ref || name;
}

function refOf(b, name) {
  const o = b.overlays;
  if (!o || !name) return null;
  return (o.loadable || []).find((x) => x.name === name) || (o.all || []).find((x) => x.name === name) || null;
}

// --- picking --------------------------------------------------------------------------------------------

const ui = {};                       // bid -> {open, q, dismissed, flash}

function uiOf(bid) {
  if (!ui[bid]) ui[bid] = { open: false, q: "", dismissed: "", flash: 0, armPulse: 0, anyway: null, word: "" };
  return ui[bid];
}

// FIX-PACK-8: the pack's declaration for a design whose boot code writes the DUT's flash
// ({why, word}), or null.
export function dutFlashOf(ov) {
  const w = ov && ov.writes_dut_flash;
  return w && w.word ? w : null;
}

// The warning, in the deploy service's words (core.pack.dut_flash_text).
export function dutFlashText(w) {
  return `${w.why} Type ${w.word} to program it anyway.`;
}

// The word was typed exactly (the CLI's rule: surrounding spaces do not count).
function wordTyped(bid, w) {
  return !!w && String(uiOf(bid).word || "").trim() === w.word;
}

// FIX-PACK-7: a deploy the daemon refused because OpenOCD on the board could not be stopped
// first (error.data.debug_down): offer to swap anyway. kind: "program" | "restore".
function offerAnyway(bid, kind, ok, value, overlay = "") {
  const why = !ok && value && value.data && value.data.debug_down;
  uiOf(bid).anyway = why ? { kind, overlay, reason: why.reason || "", launcher: why.launcher || "mps3-debug" } : null;
}

export function pickDesign(bid, name, { pulse = false } = {}) {
  const b = boardState(bid);
  const u = uiOf(bid);
  b.selectedOverlay = name;
  u.open = false;
  u.q = "";
  u.word = "";                          // FIX-PACK-8: a new pick types the word afresh
  if (pulse) { u.flash = Date.now(); u.armPulse = Date.now(); }
  if (u.anyway && u.anyway.kind === "program" && u.anyway.overlay !== name) u.anyway = null;
  setArmed(bid, ARM, false);            // a new pick is armed afresh
  changed();
  runPreflight(bid, name);
}

// "Program nanosoc_ila..." (Logic analysers) and Build's "Add to the Workbench": the design is
// picked, the strip flashes and Arm pulses; nothing is programmed until Arm, then Program.
export function pickForProgram(bid, name) {
  pickDesign(bid, name, { pulse: true });
  toast(`${name} picked: tick Arm, then Program`, { icon: "upload" });
}

function openImport(bid) {
  const u = uiOf(bid);
  u.open = false;
  changed();
  try {
    openModal("import", { bid });
  } catch (e) {
    // The Import dialog is the Build lane's (registerModal("import")); until it is in this
    // build, say so rather than fail.
    toast("Import a design is not in this build yet: use Build > Add, or `harness-manager kit pack --import`",
      { icon: "info", level: "err" });
  }
}

function DesignPicker({ bid }) {
  const b = boardState(bid);
  const u = uiOf(bid);
  const wrap = useRef(null);
  const rows = designRows(b);
  const loadable = rows.filter((r) => !r.blocked).length;
  const pick = b.selectedOverlay ? rows.find((r) => r.name === b.selectedOverlay) || { name: b.selectedOverlay, ov: refOf(b, b.selectedOverlay) } : null;
  const q = u.q.trim().toLowerCase();
  const shown = rows.filter((r) => !q || r.name.toLowerCase().includes(q)
    || String((r.ov && r.ov.rm_id) || "").toLowerCase().includes(q));
  // A click outside closes the list; so does Escape.
  useEffect(() => {
    if (!u.open) return undefined;
    const onDown = (e) => { if (wrap.current && !wrap.current.contains(e.target)) { u.open = false; changed(); } };
    const onKey = (e) => { if (e.key === "Escape") { u.open = false; changed(); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [u.open, bid]);
  const toggle = () => {
    u.open = !u.open;
    u.q = "";
    // Opening reads the list when there is none, or when the last read failed (a good old list
    // stays on show meanwhile).
    if (u.open && (!b.overlays || b.overlaysError) && !b.overlaysLoading) loadOverlays(bid);
    changed();
  };
  const placeholder = b.overlaysLoading && !b.overlays ? "Reading the designs..."
    : b.overlaysError && !b.overlays ? "The design list could not be read"
    : `Pick a design (${loadable} load on this shell)`;
  const opt = (r) => {
    const sel = b.selectedOverlay === r.name;
    return html`<button type="button" key=${r.name} class=${`opt ${r.blocked ? "blocked" : ""}`} role="option"
        aria-selected=${sel ? "true" : "false"} data-overlay=${r.name}
        title=${r.blocked ? `Keyed to another shell: ${r.blocked}` : r.ov ? `${r.ov.ip_class} IP · ${r.ov.source || ""}` : ""}
        onClick=${() => pickDesign(bid, r.name)}>
      <span class="n"><b>${r.name}</b><${Tags} tags=${designTags(b, r.ov)} />
        ${r.blocked ? html`<span class="tag other" data-testid=${`blocked-${r.name}`}>other shell</span>` : null}</span>
      <span class="rm">${r.ov ? hexId(r.ov.rm_id) : ""}</span>
      <span class="sz">${r.ov ? kib(r.ov.size_bytes) : ""}</span></button>`;
  };
  const ok = shown.filter((r) => !r.blocked);
  const other = shown.filter((r) => r.blocked);
  return html`<div class="combo-row" data-testid="overlays-card">
    <div class="combo-wrap" ref=${wrap}>
      <button type="button" class="combo" data-testid="design-picker" aria-haspopup="listbox"
          aria-expanded=${u.open ? "true" : "false"} onClick=${toggle}
          title=${pick ? `${pick.name}${pick.ov ? ` ${hexId(pick.ov.rm_id)}, ${kib(pick.ov.size_bytes)}` : ""}` : "Pick the design to program"}>
        <${Icon} name="layers" />
        ${pick ? html`<span class="name" data-testid="selected-overlay">${pick.name}</span>
            ${pick.ov ? html`<span class="rm">${hexId(pick.ov.rm_id)}</span>` : null}
            <span class="combo-tags"><${Tags} tags=${designTags(b, pick.ov, { full: false })} /></span>`
          : html`<span class="ph">${placeholder}</span>`}
        <span class="chev-d"><${Icon} name="chevron-down" /></span></button>
      ${u.open ? html`<div class="combo-pop" role="listbox" aria-label="Designs" data-testid="design-list">
        <input class="input sm" placeholder="Filter by name or rm_id" value=${u.q} aria-label="Filter the designs"
          data-testid="design-filter" ref=${(el) => { if (el && !el.dataset.f) { el.dataset.f = "1"; el.focus(); } }}
          onInput=${(e) => { u.q = e.target.value; changed(); }} />
        ${b.overlays && b.overlaysError ? html`<div class="small muted combo-stale" data-testid="design-list-stale">
          The last read failed: ${b.overlaysError.errName}: ${b.overlaysError.message}.
          <button type="button" class="link" data-action="design-reread" disabled=${b.overlaysLoading}
            onClick=${() => loadOverlays(bid)}>Read again</button></div>` : null}
        <div class="combo-list">
          ${ok.map(opt)}
          ${other.length ? html`<div class="combo-sep" title="The preflight refuses a design keyed to another shell">Keyed to another shell</div>` : null}
          ${other.map(opt)}
          ${shown.length ? null : html`<div class="small muted combo-none">${rows.length ? "No design matches." : b.overlaysError ? `${b.overlaysError.errName}: ${b.overlaysError.message}` : "No designs known for this board."}</div>`}
        </div>
        <div class="combo-foot"><button type="button" data-action="import-design" onClick=${() => openImport(bid)}>
          <${Icon} name="folder-input" />Import a design...<span class="muted">overlay folder, zip or build receipt</span></button></div>
      </div>` : null}
    </div>
    <button type="button" class="btn imp-btn" data-action="import" onClick=${() => openImport(bid)}
      title="Import a design: a kit-built overlay (folder or zip), a build receipt, or one checked in the Build tab">
      <${Icon} name="folder-input" /><span class="imp-lbl">Import...</span></button>
  </div>`;
}

// --- the inline preflight -------------------------------------------------------------------------------

const CHECK_ICON = { ok: "circle-check", mismatch: "circle-x", unchecked: "circle-help" };
const CHECK_LEVEL = { ok: "ok", mismatch: "err", unchecked: "unk" };

// The page's own chip for the lease (the daemon's preflight has no lease item): "" on a board
// with no hub.
function leaseChip(bid) {
  const who = leaseWho(bid);
  switch (who.state) {
    case "none": return null;
    case "here": return { lvl: "ok", icon: "circle-check", text: "lease yours" };
    case "unread": return { lvl: "unk", icon: "circle-help", text: "lease: reading" };
    case "unknown": return { lvl: "err", icon: "circle-x", text: "lease not known" };
    case "free": return { lvl: "err", icon: "circle-x", text: "no lease" };
    case "elsewhere": return { lvl: "err", icon: "circle-x", text: "lease: your other session" };
    default: return { lvl: "err", icon: "lock", text: `lease held by ${who.holder || "someone else"}` };
  }
}

function Preflight({ bid }) {
  const b = boardState(bid);
  const name = b.selectedOverlay;
  if (!name) {
    return html`<div class="small muted pf-empty">Preflight runs as soon as you pick: same shell, rm_id, size, lease.</div>`;
  }
  const items = b.preflight && b.preflightFor === name ? b.preflight.items : [];
  const ref = refOf(b, name);
  const extra = [];
  const lease = leaseChip(bid);
  if (lease) extra.push({ k: "lease", ...lease });
  if (ref && sameRm(ref.rm_id, loadedId(b))) extra.push({ k: "loaded", lvl: "warn", icon: "info", text: "loaded now: Program reloads it" });
  if (ref && ref.ltx_sha256) extra.push({ k: "ila", lvl: "info", icon: "scan-search", text: "ILAs" });
  if (ref && ref.receipt_sha256) extra.push({ k: "kit", lvl: "info", icon: "file-cog", text: "kit-built" });
  if (dutFlashOf(ref)) {
    extra.push({ k: "dutflash", lvl: "warn", icon: "triangle-alert", title: dutFlashOf(ref).why,
      text: "writes the DUT's flash" });
  }
  const impl = b.info && b.info.identity && b.info.identity.harness_impl;
  if (ref && ref.size_bytes && impl === "bare-metal") {
    extra.push({ k: "push", lvl: "info", icon: "upload", title: "An estimate from the bare-metal push rate measured on silicon; the bar shows the real rate",
      text: `push ~${fmtSecs(ref.size_bytes / BM_PUSH_RATE)} at ~570 KB/s` });
  }
  const bad = items.filter((i) => i.check === "mismatch").length;
  const unchecked = items.filter((i) => i.check === "unchecked").length;
  // The checks that passed fold into one chip ("5 checks OK", each on hover); a click lists them.
  const oks = items.filter((i) => i.check === "ok");
  const u = uiOf(bid);
  let summary = null;
  if (b.preflight && b.preflightFor === name && !b.preflightLoading) {
    const level = bad ? "err" : unchecked ? "unk" : "ok";
    const text = bad ? `${bad} mismatch${bad > 1 ? "es" : ""}: Program is refused`
      : `no mismatch${unchecked ? `, ${unchecked} unchecked (not a pass, does not block)` : ""}`;
    summary = html`<li class=${`pf-item pf-sum ${level}`} data-testid="preflight-summary"
      title=${b.preflightLine || ""}><${Icon} name=${bad ? "circle-x" : unchecked ? "circle-help" : "shield-check"} />${text}</li>`;
  }
  return html`<ul class="pf" aria-label="Preflight" data-testid="preflight-list">
    ${b.preflightLoading ? html`<li class="pf-item busy"><${Spinner} />preflight of ${name}...</li>` : null}
    ${b.preflightError ? html`<li class="pf-item err" data-testid="preflight-error"><${Icon} name="circle-x" />preflight failed to run: ${b.preflightError.errName}: ${b.preflightError.message}</li>` : null}
    ${oks.length && !u.pfAll ? html`<li class="pf-item ok pf-oks"><button type="button" class="pf-toggle" data-action="preflight-all"
        aria-expanded="false" title=${oks.map((i) => `${i.name}: OK${i.detail ? ` (${i.detail})` : ""}`).join("\n")}
        onClick=${() => { u.pfAll = true; changed(); }}><${Icon} name="circle-check" />${oks.length} check${oks.length > 1 ? "s" : ""} OK</button></li>` : null}
    ${items.map((i) => html`<li key=${i.name} class=${`pf-item ${CHECK_LEVEL[i.check] || "unk"} ${i.check === "ok" && !u.pfAll ? "sr-only" : ""}`} data-check=${i.check}
        title=${`${i.name}: ${i.check.toUpperCase()}${i.detail ? `\n${i.detail}` : ""}${i.check === "unchecked" ? "\nNot a pass: it could not be compared" : ""}`}>
      <${Icon} name=${CHECK_ICON[i.check] || "circle-help"} />${i.name}</li>`)}
    ${oks.length && u.pfAll ? html`<li class="pf-item"><button type="button" class="pf-toggle" data-action="preflight-all" aria-expanded="true"
        onClick=${() => { u.pfAll = false; changed(); }}>fewer</button></li>` : null}
    ${extra.map((c) => html`<li key=${`x-${c.k}`} class=${`pf-item ${c.lvl}`} data-pf=${c.k} title=${c.title || ""}>
      <${Icon} name=${c.icon} />${c.text}</li>`)}
    ${summary}
  </ul>`;
}

// --- Program / Restore baseline ------------------------------------------------------------------------

// Why Program cannot run yet (the guard of gateReason), or "".
function programGuard(bid) {
  const b = boardState(bid);
  if (!b.selectedOverlay) return "pick a design first";
  if (b.preflightLoading || b.preflightFor !== b.selectedOverlay || !b.preflight) {
    return b.preflightError ? `the preflight of ${b.selectedOverlay} failed to run`
      : `waiting for the preflight of ${b.selectedOverlay}`;
  }
  const bad = b.preflight.items.filter((i) => i.check === "mismatch").map((i) => i.name);
  if (bad.length) return `preflight MISMATCH (${bad.join(", ")}): Program is refused`;
  const w = dutFlashOf(refOf(b, b.selectedOverlay));
  if (w && !wordTyped(bid, w)) {
    return `type ${w.word} to program ${b.selectedOverlay}: its boot code writes the DUT's flash`;
  }
  return "";
}

// The Card line shows only for a harness with a card store and a card in the slot.
function cardShown(b) {
  return hasCardStore(b) && !!b.card && !!b.card.store && !!b.card.present;
}

// Keep only when the box is shown, usable and ticked: a stale tick never keeps.
function keeping(b) {
  return cardShown(b) && !b.card.reason && !!b.keepOnCard;
}

export const PROGRAM_GATE = { capability: "deploy_partial", arm: ARM, holder: "Program" };
export const RESTORE_GATE = { capability: "deploy_partial", arm: ARM, holder: "Restore baseline" };

function progress(ctx) {
  return (d) => ctx.progress(
    d.total > 1 ? `${d.phase}: ${bytesText(d.done)} of ${bytesText(d.total)}` : `${d.phase || "?"}: done`,
    d.phase || "?");
}

export function programSpecs(bid) {
  const b = boardState(bid);
  const name = b.selectedOverlay;
  // FIX-PACK-8: the typed word gives the consent (the CLI's --allow-dut-flash-write)
  const dut = dutFlashOf(refOf(b, name));
  const allow = !!dut && wordTyped(bid, dut);
  const flags = `${keeping(b) ? " --keep-on-card" : ""}${allow ? " --allow-dut-flash-write" : ""}`;
  // force: FIX-PACK-7's "Program anyway" (the CLI's --force); only when asked
  const deployRun = (force) => (ctx) => {
    // keep_on_card only when asked: the default never writes the card.
    const body = keeping(b) ? { overlay: overlaySpec(b, name), keep_on_card: true }
      : { overlay: overlaySpec(b, name) };
    if (force) body.force = true;
    if (allow) body.allow_dut_flash_write = true;
    b.keepOnCard = false;              // each keep is a fresh choice
    uiOf(bid).word = "";               // and so is the typed word (FIX-PACK-8)
    b.lastKind = "program";
    return runJob("deploy", { bid }, body, progress(ctx), "deploy");
  };
  const program = {
    key: "program", label: "Program", busyLabel: "Programming...", budgetS: 120,
    command: `program ${name || "?"}${flags}`,
    run: deployRun(false),
    render: renderDeploy,
    onDone: (ok, value) => {
      // harness-manager-daemon runs the preflight before it takes the job, and a refusal carries
      // the items it refused on: show them where the preflight lives.
      const items = !ok && value && value.data && value.data.preflight;
      if (Array.isArray(items)) {
        b.preflight = { items, refusal: null };
        b.preflightFor = name;
        b.preflightLine = `$ preflight ${name}  (from the refused deploy)`;
        changed();
      }
      offerAnyway(bid, "program", ok, value, name);
    },
  };
  const restoreRun = (force) => (ctx) => {
    b.lastKind = "restore";
    return runJob("restore", { bid }, force ? { force: true } : undefined, progress(ctx), "restore");
  };
  const restore = {
    key: "restore", label: "Restore baseline", busyLabel: "Restoring...", budgetS: 120,
    command: "restore",
    run: restoreRun(false),
    render: renderDeploy,
    onDone: (ok, value) => offerAnyway(bid, "restore", ok, value),
  };
  const programAnyway = {
    ...program, key: "program_anyway", label: "Program anyway",
    command: `program ${name || "?"}${flags} --force`,
    run: deployRun(true),
    onDone: (ok, value) => { program.onDone(ok, value); uiOf(bid).anyway = null; },
  };
  const restoreAnyway = {
    ...restore, key: "restore_anyway", label: "Restore anyway", command: "restore --force",
    run: restoreRun(true),
    onDone: () => { uiOf(bid).anyway = null; },
  };
  return { program, restore, programAnyway, restoreAnyway };
}

// FIX-PACK-7: "Program anyway" / "Restore anyway" after a refused swap (offerAnyway): the same
// arm and gate as the button it repeats, and why it is offered.
function Anyway({ bid, specs, flashReason }) {
  const offer = uiOf(bid).anyway;
  if (!offer) return null;
  const restore = offer.kind === "restore";
  const spec = restore ? specs.restoreAnyway : specs.programAnyway;
  const gate = restore ? RESTORE_GATE : { ...PROGRAM_GATE, guard: () => programGuard(bid) };
  const verb = restore ? "Restore anyway" : "Program anyway";
  const why = `OpenOCD on the board could not be stopped before the swap (${offer.launcher} down). `
    + `${verb} swaps all the same: on Linux v2.0.0 OpenOCD on the board may still drive JTAG during `
    + `the reconfiguration. Tick Arm, then ${verb}.`;
  // A refused click says so in the outcome box (the click is newer than the refused deploy).
  const refused = (reason) => { panelState(bid, PANEL).startedAt = Date.now(); flashReason(reason); };
  return html`<div class="prog-row" data-testid="anyway">
      <${DriveButton} bid=${bid} spec=${spec} variant="danger" icon="triangle-alert" gate=${gate} onRefused=${refused} />
      <span class="sr-only" id=${`reason-${spec.key}-${bid}`}>${gateReason(bid, PANEL, spec.key, gate)}</span>
    </div>
    <p class="reason warn" data-testid="anyway-why"><${Icon} name="triangle-alert" /><span>${why}</span></p>`;
}

// One button: the gate decides disabled and why; a refused click is the interlock (logged to
// Activity, "Nothing was run." in the outcome box), never a silent grey.
function DriveButton({ bid, spec, gate, variant = "", icon, onRefused }) {
  const p = panelState(bid, PANEL);
  const why = gateReason(bid, PANEL, spec.key, gate);
  const running = p.running === spec.key;
  const blocked = !!why && !running;
  // FIX-PACK-4: not the lease holder: never the blue button that says "click me" (any other
  // reason, "not armed" say, keeps it: that is the next thing to do)
  const notYours = blocked && !!holderOnly(bid, gate.holder || "Program");
  const look = notYours && variant === "primary" ? "" : variant;
  const onClick = () => {
    if (running) return;
    if (why) {
      interlock(bid, PANEL, spec.command, why);
      uiOf(bid).dismissed = "";
      if (onRefused) onRefused(why);
      return;
    }
    uiOf(bid).dismissed = "";
    runAction(bid, PANEL, { ...spec, arm: gate.arm });
  };
  return html`<button type="button" class=${`btn ${look}`} data-action=${spec.key}
      aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined}
      aria-describedby=${`reason-${spec.key}-${bid}`} title=${blocked ? why : undefined} onClick=${onClick}>
    ${running ? html`<${Spinner} />` : html`<${Icon} name=${icon} />`}${running ? spec.busyLabel : spec.label}
    ${running ? html`<span class="elapsed">${Math.floor(elapsedSince(p.startedAt))} s</span>` : null}</button>`;
}

function ArmProgram({ bid }) {
  const on = isArmed(bid, ARM);
  const p = panelState(bid, PANEL);
  const b = boardState(bid);
  const u = uiOf(bid);
  const pulse = !on && Date.now() - (u.armPulse || 0) < 3500;
  const dis = !!p.running || !!b.job;
  return html`<label class=${`arm-inline big ${on ? "armed" : ""} ${dis ? "dis" : ""} ${pulse ? "pulse" : ""}`}
      data-testid="arm-program" title="Arm: I understand this reconfigures the partition and resets the DUT">
    <input type="checkbox" checked=${on} disabled=${dis} aria-label="Arm: I understand this reconfigures the partition and resets the DUT"
      onChange=${(e) => setArmed(bid, ARM, e.target.checked)} />
    <${Icon} name=${on ? "lock-open" : "lock"} />Arm</label>`;
}

// FIX-PACK-8: the pack's warning for a design whose boot code writes the DUT's flash, and the
// field the word is typed into (Program stays off until it matches: programGuard).
function DutFlash({ bid }) {
  const b = boardState(bid);
  const name = b.selectedOverlay;
  const w = dutFlashOf(refOf(b, name));
  if (!w) return null;
  const u = uiOf(bid);
  const p = panelState(bid, PANEL);
  const ok = wordTyped(bid, w);
  const id = `dut-flash-word-${bid}`;
  return html`<div class="dut-flash" data-testid="dut-flash">
    <p class="reason warn" data-testid="dut-flash-warning"><${Icon} name="triangle-alert" /><span>${dutFlashText(w)}</span></p>
    <div class="field rekey"><label for=${id}>${ok ? html`<${Icon} name="lock-open" cls="sm" />` : html`<${Icon} name="lock" cls="sm" />`}Type ${w.word}</label>
      <input class="input sm mono grow" id=${id} data-testid="dut-flash-word" placeholder=${`type ${w.word}`}
        autocomplete="off" spellcheck="false" value=${u.word || ""} disabled=${!!p.running || !!b.job}
        aria-label=${`Type ${w.word} to program ${name} anyway`}
        onInput=${(e) => { u.word = e.target.value; changed(); }} /></div>
  </div>`;
}

function CardLine({ bid }) {
  const b = boardState(bid);
  if (!cardShown(b)) return null;
  const reason = b.card.reason || "";
  const on = !reason && !!b.keepOnCard;
  const def = b.card.default;
  const boots = def && def.rm_name ? `${def.rm_name}${def.slot ? ` [${def.slot}]` : ""}` : "greybox (nothing kept)";
  const what = b.selectedOverlay || "it";
  return html`<div class="card-line" data-testid="keep-card" title="The user microSD's overlay store: the design the board loads at power-on">
    <${Icon} name="memory-stick" />Boots next: <b data-testid="card-boots">${boots}</b> ·
    <label class=${`check ${reason ? "off" : ""}`}>
      <input type="checkbox" data-testid="keep-on-card" checked=${on} disabled=${!!reason}
        onChange=${(e) => { b.keepOnCard = e.target.checked; changed(); }} />keep ${what} on the card</label>
    <span class="sr-only">The board boots into this design next time.</span>
    ${reason ? html`<span class="reason keep-reason" data-testid="keep-reason"><${Icon} name="circle-slash" /><span>Cannot keep on this card: ${reason}</span></span>` : null}
  </div>`;
}

// --- the download bar -------------------------------------------------------------------------------------

export function deployPhases(dep) {
  const phases = [...PHASES, ...(dep.keep ? [CARD_PHASE] : [])];
  for (const p of dep.phases || []) if (!phases.includes(p)) phases.push(p);
  return phases;
}

function DownloadBar({ bid }) {
  const b = boardState(bid);
  const dep = b.deploy;
  const t = phaseTimes(bid);
  const phases = deployPhases(dep);
  const at = phases.indexOf(dep.phase);
  const cur = at >= 0 ? dep.phase : "";
  const bytes = at >= 0 && BYTE_PHASES.has(cur) && dep.total > 0;
  const frac = bytes ? Math.min(1, dep.bytes / dep.total) : 0;
  // FIX-PACK-6: an estimate (a frame in flight) is drawn hatched, its numbers with "~"
  const est = bytes && !!dep.estimated;
  const approx = est ? "~" : "";
  // What push sends: the push events' total, else the overlay's size (clearing + partial).
  const ref = refOf(b, dep.overlay);
  const total = (t && t.totals.push) || (cur === "push" ? dep.total : 0) || (ref && ref.size_bytes) || 0;
  let line;
  if (cur === "push" && dep.rate) {
    line = `push · ${approx}${bytesText(dep.bytes)} of ${bytesText(dep.total)} · ${approx}${fmtRate(dep.rate)}`
      + `${dep.left !== null && dep.left !== undefined ? ` · ${approx}${Math.max(1, Math.ceil(dep.left))} s left` : ""}`
      + `${est ? " (estimated)" : ""}`;
  } else if (cur === "push") {
    line = `push · starting · ${bytesText(dep.total)} to send (clearing + partial)`;
  } else if (cur === "card") {
    line = `card · ${approx}${bytesText(dep.bytes)} of ${bytesText(dep.total)} · ${SAYS.card}${est ? " (estimated)" : ""}`;
  } else if (cur) {
    const pushed = at > phases.indexOf("push") && total;
    line = `${cur} · ${SAYS[cur] || cur}${pushed ? ` · ${bytesText(total)} pushed` : ""}`;
  } else {
    line = `starting · ${dep.overlay || "the design"}`;
  }
  const since = dep.startedAt || (t && t.startedAt) || 0;
  const el = since ? Math.max(0, Date.now() / 1000 - since) : 0;
  const what = b.lastKind === "restore" || dep.overlay === "greybox" ? "Restoring the baseline" : `Programming ${dep.overlay || "the design"}`;
  return html`<div class="progress-box pgm" role="status" data-testid="deploy-progress" data-phase=${cur || "starting"}
      data-estimated=${est ? "1" : null}
      title=${`${what} on ${bid}: the phases are the deploy service's own (deploy.progress); bytes, rate and time left from its events' times`
        + `${est ? "; ~ and the hatched fill: an estimate while one frame is in flight (the push reports per frame), snapped to the real bytes when it completes" : ""}`}>
    <div class="pgm-steps">${phases.map((ph, i) => {
      const st = i < at ? "done" : i === at ? "active" : "";
      const fill = i < at ? 1 : i === at && bytes ? frac : 0;
      const secsTaken = phaseSecs(t, ph);
      const note = i < at ? fmtSecs(secsTaken) : i === at ? (bytes ? `${approx}${Math.floor(frac * 100)}%` : "")
        : ph === "push" && total ? bytesText(total) : "";
      return html`<div key=${ph} class=${`pgm-step ${st} ${BYTE_PHASES.has(ph) ? "bytes" : ""}`} style=${{ flexGrow: WEIGHT[ph] || 1 }} data-step=${ph}>
        <div class="pgm-lbl"><${Icon} name=${i < at ? "circle-check" : i === at ? "loader-circle" : "circle-dashed"} /><b>${ph}</b><span>${note}</span></div>
        <div class="pgm-track"><i class=${i === at && est ? "est" : ""} data-estimated=${i === at && est ? "1" : null}
          style=${{ width: `${Math.round(fill * 1000) / 10}%` }}></i></div></div>`;
    })}</div>
    <div class="pgm-line"><span class="pgm-now" data-testid="deploy-phase">${line}</span>
      <span class="num" title=${`${what}: since the deploy started (its first event)`}>${Math.floor(el)} s</span></div>
  </div>`;
}

// --- the outcome box ---------------------------------------------------------------------------------------

function outcomeOf(bid) {
  const b = boardState(bid);
  const dep = b.deploy;
  const p = panelState(bid, PANEL);
  const t = phaseTimes(bid);
  const notRun = p.lines.length && p.lines[0].kind === "rc" && p.lines[0].notRun;
  // The newer of: this page's last click (its command lines), the last deploy's end (events).
  const clickAt = p.startedAt ? p.startedAt / 1000 : 0;
  const depAt = dep.doneAt || 0;
  const lines = p.lines.filter((l) => l.kind !== "progress");
  const key = `${depAt}|${p.startedAt}|${p.lines.length}|${notRun ? 1 : 0}`;
  if (notRun && clickAt >= depAt) {
    const why = (p.lines[1] && p.lines[1].text) || "";
    return { key, state: "refused", lvl: "warn", icon: "circle-slash", text: `Not run: ${why.replace(/\s*Nothing was run\.$/, "")}`, lines };
  }
  // This page's click came after the last deploy ended and started none: the daemon refused it
  // before any job (409 HELD, a preflight refusal, no answer): its lines say why.
  if (lines.length && !p.running && (clickAt > depAt || (dep.state !== "done" && dep.state !== "failed"))) {
    const head = lines[0];
    const err = lines.find((l) => l.kind === "err");
    const failed = head.level === "err" || !!err;
    const what = p.command.startsWith("restore") ? "Restore baseline" : "Program";
    return { key, state: failed ? "failed" : "result", lvl: failed ? "err" : "", icon: failed ? "circle-x" : "info",
      text: failed ? `${what} was refused${err ? `: ${err.name}: ${err.text}` : ""}` : "", lines };
  }
  if (dep.state !== "done" && dep.state !== "failed") return null;
  const restore = dep.overlay === "greybox" || /^0x0+$/i.test(String(dep.rm_id || ""));
  const verb = restore ? "Restored baseline: greybox" : `Programmed ${dep.overlay || "the design"}`;
  const secs = dep.seconds || (t && t.doneAt && t.startedAt ? t.doneAt - t.startedAt : 0);
  const push = phaseSecs(t, "push");
  const total = (t && t.totals.push) || 0;
  const mine = lines.length && clickAt <= depAt + 1 ? lines : [];
  if (dep.state === "failed") {
    return { key, state: "failed", lvl: "err", icon: "circle-x",
      text: `${restore ? "Restore" : `Programming ${dep.overlay || "the design"}`} failed${dep.stage ? ` at ${dep.stage}` : ""}: ${dep.reason}`,
      lines: mine };
  }
  const verified = !!dep.verified;
  const at = new Date((dep.doneAt || Date.now() / 1000) * 1000).toTimeString().slice(0, 5);
  const pushed = total ? `pushed ${bytesText(total)}${push ? ` in ${fmtSecs(push)} · ${fmtRate(total / Math.max(push, 0.001))}` : ""}` : "";
  const phaseTitle = (t ? t.order : []).map((ph) => `${ph} ${fmtSecs(phaseSecs(t, ph))}`).join(" · ");
  return { key, state: verified ? "done" : "unverified", lvl: verified ? "ok" : "warn", warning: dep.warning || "",
    icon: verified ? "circle-check" : "triangle-alert",
    text: verified ? `${verb} ${hexId(dep.rm_id)}${secs ? ` · ${fmtSecs(secs)} in all` : ""}`
      : `Written, not verified: ${dep.overlay || "the design"} ${hexId(dep.rm_id)}`,
    push: [pushed, verified ? `verified ${at}` : ""].filter(Boolean).join(" · "),
    title: phaseTitle, card: dep.card, lines: mine };
}

// "$ program led  (rc 0, 1.2 s)": the command line's head, as the result box starts.
function rcText(l) {
  if (!l || l.kind !== "rc") return "command";
  if (l.notRun) return `$ ${l.command}  (not run)`;
  if (l.running) return `$ ${l.command}  (running)`;
  const rc = l.rc === null || l.rc === undefined ? "no answer" : `rc ${l.rc}`;
  return `$ ${l.command}  (${rc}, ${Number.isFinite(l.secs) ? l.secs.toFixed(1) : "?"} s)`;
}

function Outcome({ bid }) {
  const u = uiOf(bid);
  const oc = outcomeOf(bid);
  if (!oc || u.dismissed === oc.key) return null;
  return html`<div class=${`outcome pgm-out ${oc.lvl}`} role="status" data-testid="deploy-outcome" data-state=${oc.state}
      title=${oc.title || ""}>
    <${Icon} name=${oc.icon} />
    <span class="pgm-out-body">
      ${oc.text ? html`<span class="pgm-out1">${oc.text}</span>` : null}
      ${oc.push ? html`<span class="pgm-out2" data-testid="deploy-pushed">${oc.push}</span>` : null}
      ${oc.warning ? html`<span class="pgm-out2" data-testid="deploy-warning"><${Icon} name="triangle-alert" cls="sm" /> ${oc.warning}</span>` : null}
      ${oc.card ? html`<span class=${`chip ${oc.card.kept ? "ok" : "warn"} pgm-card`} data-testid="deploy-card"
          data-kept=${oc.card.kept ? "true" : "false"}><${Icon} name=${oc.card.kept ? "memory-stick" : "triangle-alert"} />
          ${cardText(oc.card)}${oc.card.kept ? ": boots next" : ""}</span>` : null}
      ${oc.lines && oc.lines.length ? html`<details class="pgm-cmd" open=${oc.lvl === "err" || oc.state === "refused"}>
          <summary title="The command this page ran, and what it said"><${Icon} name="chevron-right" cls="sm chev" /><span class="mono">${rcText(oc.lines[0])}</span></summary>
          <${ResultBlock} lines=${oc.lines} panel=${panelState(bid, PANEL)} testid="program-result" /></details>` : null}
    </span>
    <button type="button" class="btn ghost icon-only sm" title="Dismiss" aria-label="Dismiss the outcome"
      onClick=${() => { u.dismissed = oc.key; changed(); }}><${Icon} name="x" /></button>
  </div>`;
}

// --- the strip ---------------------------------------------------------------------------------------------

export function ProgramStrip({ bid }) {
  const b = boardState(bid);
  const u = uiOf(bid);
  const ref = useRef(null);
  // The list is read again whenever it was dropped: Build's Add and the Import dialog land here
  // with `overlays = null` and the design in `selectedOverlay` (import.js pickOnWorkbench).
  const listed = !!b.overlays;
  useEffect(() => {
    if (!b.overlays && !b.overlaysLoading) loadOverlays(bid);
  }, [bid, listed]);
  const store = hasCardStore(b);
  useEffect(() => {
    if (store && !b.card && !b.cardLoading) loadCard(bid);
  }, [bid, store]);
  // 0.1.0's "program" key (and Build's "Add to Program") lands here with the picker open.
  const revealed = useReveal(bid, "program", ref);
  const revealAt = revealed && S.ui.reveal ? S.ui.reveal.at : 0;
  useEffect(() => {
    if (revealAt && !b.selectedOverlay) { u.open = true; changed(); }
  }, [revealAt]);
  const cap = capState(b.info, "deploy_partial");
  const p = panelState(bid, PANEL);
  const specs = programSpecs(bid);
  const { program, restore } = specs;
  const flashReason = () => { u.flash = Date.now(); changed(); };
  const programWhy = gateReason(bid, PANEL, "program", { ...PROGRAM_GATE, guard: () => programGuard(bid) });
  const restoreWhy = gateReason(bid, PANEL, "restore", RESTORE_GATE);
  const running = b.deploy.state === "running";
  const flashing = Date.now() - (u.flash || 0) < 1600;
  const pick = b.selectedOverlay;
  let reason;
  if (running || p.running) {
    reason = html`<p class="reason busy" data-testid="reason-program"><${Spinner} /><span>${
      p.running === "restore" || b.lastKind === "restore" ? "restoring the baseline..." : `programming ${b.deploy.overlay || pick || ""}...`}</span></p>`;
  } else if (programWhy && programWhy !== "running") {
    const held = /lease holder only/.test(programWhy);
    reason = html`<p class=${`reason ${held ? "held" : /MISMATCH|failed/.test(programWhy) ? "err" : ""} ${flashing ? "flash" : ""}`}
        data-testid="reason-program" id=${`reason-program-${bid}`}>
      <${Icon} name=${held ? "lock" : programWhy.startsWith("Cannot:") ? "circle-slash" : programWhy.startsWith("not armed") ? "lock" : "info"} />
      <span>${programWhy}</span></p>`;
  } else {
    reason = html`<p class="reason ok" data-testid="reason-program" id=${`reason-program-${bid}`}><${Icon} name="circle-check" />
      <span>ready: Program swaps the partition to ${pick}, then reads it back</span></p>`;
  }
  return html`<section class=${`card wb-strip ${flashing && u.armPulse === u.flash ? "strip-flash" : ""}`} ref=${ref}
      aria-label="Program the partition" data-testid="program-card">
    ${cap && !cap.available ? html`<p class="reason strip-cap" data-testid="program-unavailable"><${Icon} name="circle-slash" />
      <span>Programming is not available on this board: ${cap.reason}</span></p>` : null}
    <div class="strip-grid">
      <div class="strip-col">
        <div class="strip-label">Design to program</div>
        <${DesignPicker} bid=${bid} />
        <${Preflight} bid=${bid} />
      </div>
      <div class="strip-col">
        <div class="strip-label">Program the partition</div>
        <${DutFlash} bid=${bid} />
        <div class="prog-row">
          <${ArmProgram} bid=${bid} />
          <${DriveButton} bid=${bid} spec=${program} variant="primary" icon="upload"
            gate=${{ ...PROGRAM_GATE, guard: () => programGuard(bid) }} onRefused=${flashReason} />
          <${DriveButton} bid=${bid} spec=${restore} icon="undo-2" gate=${RESTORE_GATE} onRefused=${flashReason} />
        </div>
        ${reason}
        <span class="sr-only" data-testid="reason-restore" id=${`reason-restore-${bid}`}>${restoreWhy === "running" ? "" : restoreWhy}</span>
        <${Anyway} bid=${bid} specs=${specs} flashReason=${flashReason} />
        <${CardLine} bid=${bid} />
      </div>
    </div>
    ${running ? html`<div class="strip-out"><${DownloadBar} bid=${bid} /></div>`
      : html`<${OutcomeWrap} bid=${bid} />`}
  </section>`;
}

function OutcomeWrap({ bid }) {
  const oc = outcomeOf(bid);
  if (!oc || uiOf(bid).dismissed === oc.key) return null;
  return html`<div class="strip-out"><${Outcome} bid=${bid} /></div>`;
}
