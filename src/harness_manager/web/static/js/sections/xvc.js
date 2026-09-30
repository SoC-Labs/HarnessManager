// The Workbench rail's Logic analysers card (UI v2, round 3 "IlaCard"; lane XVC-UI before it;
// docs/design/XVC_DEBUG.md 4.1, docs/API.md "Fabric debug over XVC"): Vivado's view of the
// partition's ILAs through the harness's own XVC server, behind Harness Manager's relay and
// (unless --byo) its own hw_server. When the loaded design has no ILAs it offers the design on
// this shell that has them ("Program nanosoc_ila..."), which picks it in the Program strip.
//
// david's decisions (2026-09-24) as the card shows them:
// - X1 scope: the line under the title is always there, whatever the state (round 3's short
//   words: "ILA over XVC: the partition's debug chain only"; the full sentence on hover and
//   under More). XVC reaches the reconfigurable partition's debug chain, never whole-device JTAG.
// - X2: Open starts HM's hw_server; "Bring your own hw_server" (--byo) starts none.
// - X3: a partition swap closes the session and re-attaches it: "swapping", then
//   "re-attached on <design>", live from xvc.state events.
// - X4: while a session is open HM holds the board's one XVC slot. "held" (violet, P6) is
//   another client holding it.
// - X5: the Download button gets the full-design .ltx when the mint staged one.
// - X6: the bare-metal harness's XVC is unauthenticated: its warning shows, and Open is
//   for the lease holder only (anyone else sees the buttons disabled, with the reason).

import { gateReason, interlock, panelState, runAction, runJob } from "../actions.js";
import { call, callBlob, heldByJob, routeMissing, toApiError } from "../api.js";
import { clock } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, onBoardEvent, onJobEnded, timed, toast } from "../store.js";
import { holderOnly } from "../week.js";
import { settingValue } from "../prefs.js";
import { Chip, CopyButton, Icon, Reason, ResultBlock, Spinner } from "../ui.js";
import { pickForProgram } from "./program.js";

export const XVC_SCOPE = "XVC reaches the reconfigurable partition's debug chain (Debug Bridge, "
  + "debug hub and ILAs of the loaded design). It never gives whole-device JTAG.";

const STATE_LOOK = {
  down: { level: "", icon: "unplug" },
  starting: { level: "unk", icon: "loader-circle" },
  ready: { level: "ok", icon: "cable" },
  attached: { level: "accent", icon: "plug-zap" },
  held: { level: "held", icon: "lock" },
  swapping: { level: "warn", icon: "arrow-right-left" },
  failed: { level: "err", icon: "circle-x" },
};

const WHICH_TEXT = { full: "full design", rm: "partition (RM)", static: "static (MIG)" };
const NO_MIG = "the bare-metal static has no MIG debug hub";

// --- per-board state ---------------------------------------------------------------------------

export function xvc(bid) {
  const b = boardState(bid);
  if (!b.xvc) {
    b.xvc = {
      st: null, error: null, at: 0, unsupported: "",   // unsupported: why there is no XVC here
      byo: null,               // the toggle (null: the setting debug.hw_server_mode, byoOf);
                               // an open session's own mode wins
      heldBy: "",              // the last open found the board's slot taken: by whom
      swap: "",                // non-empty while a partition swap holds the session
      reattached: "",          // the design it re-attached on after the last swap
      tcl: null, tclError: null, tclKey: "", tclGen: 0, copied: false,
      actionAt: 0,             // when this page's last Open/Close started
      ltxBusy: "", ltxError: null, downloaded: "",
    };
  }
  return b.xvc;
}

// FIX-PACK-4: "Bring your own hw_server" starts as Settings > Debug > debug.hw_server_mode
// says (prefs.js); a tick here wins for this page.
export function byoOf(x) {
  return x.byo === null || x.byo === undefined ? settingValue("debug.hw_server_mode") === "byo" : !!x.byo;
}

function slotHeldText(st) {
  if (!st) return "";
  if (st.state === "held") return st.detail || "another client holds the board's XVC slot";
  if (st.state === "failed" && /XVC slot is held/.test(st.detail || "")) return st.detail;
  return "";
}

function applyStatus(bid, data) {
  const x = xvc(bid);
  const d = data || {};
  if (d.state === "swapping") {
    x.swap = d.detail || "partition swap";
    x.reattached = "";
  } else if (x.swap && (d.state === "ready" || d.state === "attached")) {
    x.reattached = d.rm_name || d.rm_id || "the new design";
    x.swap = "";
  } else if (d.state === "down" || d.state === "failed") {
    x.swap = "";
    x.reattached = "";
  }
  if (d.state === "ready" || d.state === "attached") x.heldBy = "";
  x.st = d;
  x.error = null;
  x.at = performance.now();
  scheduleTcl(bid);
}

export async function loadXvc(bid) {
  const x = xvc(bid);
  const asked = performance.now();
  const r = await timed("xvc status", () => call("xvcStatus", { bid }));
  if (x.at > asked) return;              // an xvc.state event is newer than this read
  if (r.error) {
    if (heldByJob(r.error)) return;      // the job's end reads it again
    if (routeMissing(r.error) || r.error.errName === "UNAVAILABLE") {
      // A daemon without the XVC routes, or an engine without the service (the demo): a
      // reason, not an error.
      x.unsupported = r.error.reason || r.error.message || "this harness-manager-daemon has no XVC routes";
      x.st = { state: "down", open: false, reason: x.unsupported, warnings: [] };
      x.error = null;
    } else {
      x.error = r.error;
    }
  } else {
    applyStatus(bid, r.data.data);
  }
  changed();
}

// The Vivado Tcl for what is open (or would be): read again when what it names changes.
function tclKey(bid) {
  const x = xvc(bid);
  const st = x.st || {};
  const ltx = st.ltx || {};
  const pref = ltx.preferred && ltx[ltx.preferred];
  return [st.open ? "open" : (byoOf(x) ? "byo" : "m1"), st.mode, st.url, st.rm_id,
    pref ? pref.path : ""].join("|");
}

const tclTimers = {};
function scheduleTcl(bid, ms = 60) {
  const x = xvc(bid);
  if (tclKey(bid) === x.tclKey && (x.tcl || x.tclError)) return;
  clearTimeout(tclTimers[bid]);
  tclTimers[bid] = setTimeout(() => loadTcl(bid), ms);
}

export async function loadTcl(bid) {
  const x = xvc(bid);
  if (x.unsupported) return;
  const key = tclKey(bid);
  x.tclGen += 1;
  const gen = x.tclGen;
  const open = !!(x.st && x.st.open);
  const byo = open ? "" : (byoOf(x) ? "true" : "false");
  const r = await timed("xvc tcl", () => call("xvcTcl", { bid, byo }));
  if (gen !== x.tclGen) return;          // a newer read is on its way
  if (r.error && heldByJob(r.error)) return;
  x.tclKey = key;
  x.tcl = r.error ? null : r.data.data;
  x.tclError = r.error;
  changed();
}

onBoardEvent((ev) => {
  if (ev.topic !== "xvc.state" || !ev.board_id) return;
  applyStatus(ev.board_id, ev.data);
});

onJobEnded((bid, kind) => {
  if (kind === "xvc_open" || kind === "deploy" || kind === "restore") loadXvc(bid);
});

// --- why a button cannot run ---------------------------------------------------------------------

// X6: behind a hub, XVC is for the lease holder only (the daemon refuses anyone else with
// 409 HELD naming the holder). A board with no hub has no lease: its session lock is the gate.
// FIX-PACK-4: the one rule (week.js holderOnly): held HERE, never `mine` (another session of
// your hub name held it and this card enabled Open).
export function leaseReason(bid) {
  return holderOnly(bid, "XVC");
}

function statusLines(st) {
  if (!st || typeof st !== "object") return [{ kind: "out", text: String(st) }];
  const out = [{ kind: st.state === "ready" || st.state === "attached" ? "ok" : "out", text: `state     ${st.state}` }];
  if (st.url) out.push({ kind: "out", text: `vivado    ${st.url}${st.mode === "byo" ? " (open_hw_target -xvc_url)" : ""}` });
  if (st.relay_port) out.push({ kind: "out", text: `relay     127.0.0.1:${st.relay_port}` });
  if (st.hw_server_pid) out.push({ kind: "out", text: `hw_server pid ${st.hw_server_pid}${st.hw_server ? `  ${st.hw_server}` : ""}` });
  if (st.board_slot) out.push({ kind: "out", text: `slot      ${st.board_slot}` });
  if (st.detail) out.push({ kind: "out", text: `detail    ${st.detail}` });
  return out;
}

export function xvcSpecs(bid) {
  const x = xvc(bid);
  const target = bid.includes("@") ? bid.slice(bid.indexOf("@") + 1) : bid;
  // The action's answer, unless an xvc.state event came after the action started: events
  // are newer (a swap or an attach can follow the open job's "ready" before its answer lands).
  const began = () => { x.actionAt = performance.now(); };
  const keep = (ok, value) => {
    if (ok && value && typeof value === "object" && value.state) {
      if (x.at <= (x.actionAt || 0)) applyStatus(bid, value);
      changed();
    } else {
      const e = toApiError(value);
      // The job failed because another client holds the board's one XVC slot (not the lease).
      if (!ok && e.errName === "HELD" && /XVC slot is held/.test(e.message)) {
        x.heldBy = e.message;
        changed();
      }
      loadXvc(bid);
    }
  };
  const open = {
    key: "xvc_open", label: "Open", busyLabel: "Opening...", budgetS: 90,
    command: `xvc open ${target}${byoOf(x) ? " --byo" : ""}`,
    run: (ctx) => {
      began();
      return runJob("xvcOpen", { bid }, { byo: byoOf(x) },
        (d) => ctx.progress(d.phase || "starting", d.phase), "xvc_open");
    },
    render: statusLines, onDone: keep,
  };
  const close = {
    key: "xvc_close", label: "Close", busyLabel: "Closing...", budgetS: 30,
    command: `xvc close ${target}`,
    run: async () => { began(); return (await call("xvcClose", { bid })).data; },
    render: statusLines,
    onDone: (ok, value) => {
      if (ok) { x.heldBy = ""; x.reattached = ""; }
      keep(ok, value);
    },
  };
  return { open, close };
}

// --- the probes file -------------------------------------------------------------------------------

async function downloadLtx(bid, which, item) {
  const x = xvc(bid);
  x.ltxBusy = which;
  x.ltxError = null;
  changed();
  try {
    const blob = await callBlob("xvcLtx", { bid, which });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (item && item.name) || `${which}.ltx`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    x.downloaded = a.download;
  } catch (e) {
    x.ltxError = toApiError(e);
  }
  x.ltxBusy = "";
  changed();
}

function LtxRow({ bid, which, item, reason, testid }) {
  const x = xvc(bid);
  if (!item || !item.path) {
    return html`<span class="muted" data-testid=${testid} data-available="false">unavailable: ${reason}</span>`;
  }
  const busy = x.ltxBusy === which;
  return html`<span class="xvc-ltx" data-testid=${testid} data-available="true">
    <span class="mono small" title=${item.path}>${item.name || item.path}</span>
    ${item.crc_ok === true ? html`<${Chip} level="ok" icon="check" title="matches the overlay manifest's CRC">crc<//>`
      : item.crc_ok === false ? html`<${Chip} level="err" icon="circle-x" title="does not match the overlay manifest's CRC">crc<//>` : null}
    <button type="button" class="btn ghost sm icon-only" data-action=${`xvc-ltx-${which}`} aria-busy=${busy ? "true" : undefined}
      aria-label=${`Download the ${WHICH_TEXT[which] || which} probes file`}
      title=${`Download the ${WHICH_TEXT[which] || which} probes file (.ltx) for Vivado's PROBES.FILE`}
      onClick=${() => { if (!busy) downloadLtx(bid, which === "preferred" ? "auto" : which, item); }}>
      ${busy ? html`<${Spinner} />` : html`<${Icon} name="download" />`}</button>
  </span>`;
}

// --- the Tcl ---------------------------------------------------------------------------------------

function CopyTcl({ bid, text, primary = false }) {
  const x = xvc(bid);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      x.copied = true;
      toast("Vivado Tcl copied: it connects Vivado and sets PROBES.FILE", { icon: "copy" });
    } catch (e) {
      x.copied = false;
    }
    changed();
    setTimeout(() => { x.copied = false; changed(); }, 1500);
  };
  return html`<button type="button" class=${`btn sm ${primary ? "primary" : "ghost"}`} data-action="xvc-copy-tcl" disabled=${!text}
    onClick=${copy} title="Copy the Tcl for Vivado's Tcl console: it connects and loads the probes (sets PROBES.FILE)">
    <${Icon} name=${x.copied ? "check" : "copy"} />${x.copied ? "Copied" : "Copy Tcl"}</button>`;
}

// --- the card ----------------------------------------------------------------------------------------

export function viewState(bid) {
  const x = xvc(bid);
  const st = x.st;
  if (!st) return x.error ? "unknown" : "reading";
  if (slotHeldText(st) || (x.heldBy && !st.open)) return "held";
  return st.state || "unknown";
}

// The loaded design has no ILAs: the design on this shell that has them (its probes file travels
// with it, `ltx_sha256`; nanosoc_ila first, the platform's ILA build), or null.
export function ilaDesign(bid) {
  const b = boardState(bid);
  const list = (b.overlays && b.overlays.loadable) || [];
  const loaded = String((b.info && b.info.identity && b.info.identity.rm_id) || "").toLowerCase();
  const others = list.filter((o) => String(o.rm_id || "").toLowerCase() !== loaded);
  return others.find((o) => o.name === "nanosoc_ila") || others.find((o) => o.ltx_sha256) || null;
}

function ProgramIla({ bid, design }) {
  const b = boardState(bid);
  const ila = ilaDesign(bid);
  if (!ila) {
    return html`<p class="reason" data-testid="xvc-no-ila"><${Icon} name="info" /><span>${design || "The loaded design"} has no ILAs,
      and no design on this shell carries a probes file.</span></p>`;
  }
  const why = holderOnly(bid, "Program") || (b.job ? `waiting for the ${b.job.kind} job to finish` : "");
  const picked = b.selectedOverlay === ila.name;
  return html`<div class="xvc-no-ila" data-testid="xvc-no-ila">
    <div class="small secondary"><b>${design || "The loaded design"}</b> has no ILAs. <span class="mono">${ila.name}</span>
      ${ila.name === "nanosoc_ila" ? " is the same SoC with ILAs." : " carries a probes file."}</div>
    <div class="row"><button type="button" class=${`btn sm ${picked || why ? "" : "primary"}`} data-action="program-ila"
        aria-disabled=${why ? "true" : undefined} title=${why || `Pick ${ila.name} in the Program strip: then Arm, then Program`}
        onClick=${() => { if (!why) pickForProgram(bid, ila.name); }}>
      <${Icon} name="upload" />${picked ? "Picked: Arm, then Program" : `Program ${ila.name}...`}</button></div>
    ${why ? html`<p class="reason held"><${Icon} name="lock" /><span>${why}</span></p>` : null}
  </div>`;
}

const CHIP_TEXT = { down: "closed", starting: "opening", ready: "ready", attached: "attached", held: "held",
  swapping: "swapping", failed: "failed", reading: "reading", unknown: "unknown" };

// The rail's Logic analysers card (UI v2, prototype "IlaCard"; plan §1.3 W15-W16): Open / Close,
// the URL for Vivado, the probes file, Copy Tcl (sets PROBES.FILE), bring your own hw_server; the
// rest (the Tcl itself, the static MIG probes, the reach) folds under More.
export function XvcCard({ bid }) {
  const x = xvc(bid);
  useEffect(() => { loadXvc(bid); }, [bid]);
  const byo = byoOf(x);
  useEffect(() => { if (x.st) scheduleTcl(bid); }, [bid, byo]);     // the setting's answer came
  const st = x.st || {};
  const state = viewState(bid);
  const look = STATE_LOOK[state] || { level: "unk", icon: "circle-help" };
  const held = slotHeldText(st) || (state === "held" ? x.heldBy : "");
  const { open, close } = xvcSpecs(bid);
  const p = panelState(bid, "xvc");
  const warnings = st.warnings || [];
  const unauth = warnings.filter((w) => /unauthenticated/i.test(w));
  const notes = warnings.filter((w) => !/unauthenticated/i.test(w));
  const ltx = st.ltx || {};
  const info = boardState(bid).info;
  const ident = (info && info.identity) || {};
  const linux = ident.harness_impl === "linux";
  // With no session open the status names no probes file: the Tcl answer does (its path and
  // which one), so the file can be fetched before Open.
  const tclLtx = !st.open && x.tcl && x.tcl.ltx
    ? { path: x.tcl.ltx, name: String(x.tcl.ltx).split(/[\\/]/).pop() } : null;
  const prefKey = ltx.preferred || (tclLtx ? x.tcl.which : "");
  const pref = ltx.preferred ? ltx[ltx.preferred] : tclLtx;
  const design = { name: st.rm_name || ident.rm_name || "", id: st.rm_id || ident.rm_id || "" };
  const att = st.attached;
  // No ILAs in the loaded design: the status read says so (no probes file, not unsupported).
  const noIla = !!x.st && !x.unsupported && !st.reason && !pref && !st.open && !x.swap;
  // FIX-PACK-4: the lease rule is the gate's `holder` (actions.js), so Open is never the
  // primary button for someone who may not use it; Close stays for an open session.
  const openGuard = () => (st.reason ? `Cannot: ${st.reason}` : "")
    || (st.open ? "the session is already open" : "");
  const closeGuard = () => (st.open ? "" : holderOnly(bid, "XVC") || "no XVC session is open");
  const openGate = { guard: openGuard, holder: "XVC" };
  const closeGate = { guard: closeGuard };
  const byoLocked = !!st.open || !!p.running;
  const tclText = x.tcl ? x.tcl.tcl : "";
  const lines = p.lines.filter((l) => l.kind !== "progress");
  const chip = html`<${Chip} level=${look.level} icon=${look.icon} testid="xvc-state"
    title=${st.detail || ""}>${noIla && state === "down" ? "no ILAs" : CHIP_TEXT[state] || state}<//>`;
  const reasons = [["xvc_open", openGate], ["xvc_close", closeGate]].map(([k, g]) => {
    const why = gateReason(bid, "xvc", k, g);
    if (!why || why === "running") return null;
    const quiet = /no XVC session is open|already open/.test(why) || (k === "xvc_close" && why === gateReason(bid, "xvc", "xvc_open", openGate));
    if (quiet) return html`<span key=${k} class="sr-only" data-testid=${`reason-${k}`}>${why}</span>`;
    const lease = /lease holder only/.test(why);
    return html`<p key=${k} class=${`reason ${lease ? "held" : ""}`} data-testid=${`reason-${k}`}>
      <${Icon} name=${lease ? "lock" : why.startsWith("Cannot:") ? "circle-slash" : "info"} /><span>${why}</span></p>`;
  });
  const btn = (spec, gate, variant, icon) => {
    const why = gateReason(bid, "xvc", spec.key, gate);
    const running = p.running === spec.key;
    const blocked = !!why && !running;
    const look2 = blocked && variant === "primary" ? "" : variant;
    const onClick = () => {
      if (running) return;
      if (why) { interlock(bid, "xvc", spec.command, why); return; }
      runAction(bid, "xvc", spec);
    };
    return html`<button type="button" class=${`btn sm ${look2}`} data-action=${spec.key}
        aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined}
        title=${blocked ? why : undefined} onClick=${onClick}>
      ${running ? html`<${Spinner} />` : html`<${Icon} name=${icon} />`}${running ? spec.busyLabel : spec.label}</button>`;
  };
  return html`<section class="xvc" data-testid="xvc" data-state=${state}>
    <section class="card wb-ila" data-testid="xvc-card" aria-label="Logic analysers">
      <div class="card-head"><h2 class="card-title"><${Icon} name="scan-search" />Logic analysers</h2><span class="spacer"></span>${chip}</div>
      <p class="card-sub" title=${XVC_SCOPE}>ILA over XVC: the partition's debug chain only</p>
      <div class="card-body"><div class="stack tight">
        ${x.error ? html`<${Reason} level="err" testid="xvc-error" text=${`Cannot read the XVC state: ${x.error.errName}: ${x.error.message}`} />` : null}
        ${st.reason ? html`<${Reason} level="unk" icon="circle-slash" testid="xvc-reason" text=${`Not on this board: ${st.reason}`} />` : null}
        ${unauth.map((w) => html`<p key=${w} class="reason warn xvc-unauth" data-testid="xvc-unauth" title=${w}><${Icon} name="triangle-alert" />
          <span>${w}</span></p>`)}
        ${held ? html`<p class="reason held" data-testid="xvc-held"><${Icon} name="lock" />
          <span>Held by another client: ${held}. The harness serves one XVC client; close the other Vivado or hw_server first.</span></p>` : null}
        ${x.swap ? html`<${Reason} level="warn" icon="arrow-right-left" testid="xvc-swapping"
          text=${`Swapping... ${x.swap}.`} />` : null}
        ${x.reattached && !x.swap ? html`<${Reason} level="ok" icon="refresh-cw" testid="xvc-reattached"
          text=${`Re-attached on ${x.reattached} after the swap: re-run the probes lines of the Tcl (or refresh_hw_device).`} />` : null}
        ${noIla ? html`<${ProgramIla} bid=${bid} design=${design.name} />` : null}
        <dl class="kv" data-testid="xvc-facts">
          ${st.open ? html`<dt>Vivado</dt><dd data-testid="xvc-url">${st.url
            ? html`<span class="copy-row"><code title=${st.mode === "byo" ? "your hw_server: open_hw_target -xvc_url" : "Harness Manager's hw_server: connect_hw_server -url"}>${st.url}</code><${CopyButton} text=${st.url} /></span>`
            : html`<span class="muted">starting...</span>`}</dd>` : null}
          <dt>Probes</dt><dd><${LtxRow} bid=${bid} which="preferred" item=${pref} testid="xvc-ltx"
            reason=${ltx.note || (x.tcl || st.open ? `no probes file for the loaded design${design.name ? ` (${design.name})` : ""}` : "reading the board...")} />
            ${pref ? html`${" "}<span class="xvc-ltx-which" data-testid="xvc-ltx-which">${WHICH_TEXT[prefKey] || prefKey}${pref.vivado ? ` · Vivado ${pref.vivado}` : ""}</span>` : null}</dd>
          ${st.open ? html`<dt>Attached</dt><dd data-testid="xvc-attached">${att
            ? html`<div class="line"><${Chip} level="accent" icon="plug-zap">${att.pid ? `pid ${att.pid}` : att.peer}<//>
                ${att.pid && att.pid === st.hw_server_pid ? html`<span class="secondary small">Harness Manager's hw_server</span>` : null}</div>
              ${att.command ? html`<div class="mono sub" data-testid="xvc-attached-cmd">${att.command}</div>` : null}
              ${att.since ? html`<div class="sub">since ${clock(att.since)}${att.shifts ? ` · ${att.shifts} shifts` : ""}</div>` : null}`
            : html`<span class="muted">nobody yet</span>`}</dd>` : null}
        </dl>
        <div class="row">
          ${st.open ? html`<${CopyTcl} bid=${bid} text=${tclText} primary=${true} />${btn(close, closeGate, "", "square")}`
            : html`${btn(open, openGate, "primary", "play")}<${CopyTcl} bid=${bid} text=${tclText} />`}
          <span class="small muted">A swap closes it.</span>
        </div>
        ${st.open ? null : html`<label class=${`check xvc-byo ${byoOf(x) ? "on" : ""}`} data-testid="xvc-byo"
            title=${byoLocked ? "close the session to change it" : "Harness Manager starts no hw_server; your Vivado opens the relay itself (open_hw_target -xvc_url)"}>
          <input type="checkbox" checked=${byoOf(x)} disabled=${byoLocked}
            onChange=${(e) => { x.byo = e.target.checked; changed(); scheduleTcl(bid, 0); }} />
          Bring your own hw_server (<code>--byo</code>)</label>`}
        ${reasons}
        ${lines.length ? html`<${ResultBlock} lines=${lines} panel=${p} testid="xvc-result" />` : null}
        ${x.ltxError ? html`<${Reason} level="err" testid="xvc-ltx-error" text=${`${x.ltxError.errName}: ${x.ltxError.message}`} />` : null}
        ${x.downloaded ? html`<${Reason} level="ok" testid="xvc-downloaded" text=${`Saved ${x.downloaded}: set it as the device's PROBES.FILE (the Tcl does).`} />` : null}
        <details class="xvc-more" data-testid="xvc-more">
          <summary><${Icon} name="chevron-right" cls="sm chev" />More: the Tcl, the static probes, the reach</summary>
          <dl class="kv">
            <dt>Design</dt><dd data-testid="xvc-design">${design.name || design.id
              ? html`${design.name || "unnamed"} ${design.id ? html`<span class="mono sub">${design.id}</span>` : null}`
              : html`<span class="muted">-</span>`}</dd>
            ${st.detail && !held && !x.swap && !x.reattached ? html`<dt>Status</dt><dd class="small" data-testid="xvc-detail">${st.detail}</dd>` : null}
            <dt>Static (MIG)</dt><dd><${LtxRow} bid=${bid} which="static" item=${ltx.static} testid="xvc-static"
              reason=${ltx.static_note || (linux ? "no static probes file for this static yet: import the mint's" : NO_MIG)} /></dd>
            <dt>Reach</dt><dd class="small">${st.reach || html`<span class="muted">-</span>`}</dd>
          </dl>
          <div class="xvc-tcl-head"><span class="field-label">Vivado Tcl</span>
            <span class="secondary small">${x.tcl ? (x.tcl.open ? "for the open session" : "a preview: Open first, then paste it") : ""}</span></div>
          ${x.unsupported ? html`<${Reason} level="unk" testid="xvc-tcl-error" text=${`No Tcl: ${x.unsupported}`} />`
            : x.tclError ? html`<${Reason} level="err" testid="xvc-tcl-error" text=${`${x.tclError.errName}: ${x.tclError.message}`} />`
            : html`<pre class="result xvc-tcl" data-testid="xvc-tcl">${tclText || (x.tcl === null ? "reading..." : "")}</pre>`}
          ${notes.map((w) => html`<${Reason} key=${w} testid="xvc-note" text=${w} />`)}
          <p class="small muted xvc-scope">${XVC_SCOPE}</p>
        </details>
      </div></div>
    </section>
  </section>`;
}
