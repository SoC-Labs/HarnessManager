// Entry point: the rail (boards), the header (the selected board), the five tabs (UI v2).
//
// The tabs and their routes are route.js's; a tab's body is registered there (registerTab),
// so a lane replaces its tab from its own module. The extension points a lane uses instead of
// editing this file: navigate / openActivity / toast (store.js), registerModal / openModal
// (modal.js), registerTab (route.js), boardState(bid).deploy (the mini bars).

import { ApiError, call, hasToken, initToken } from "./api.js";
import { closeBoardConsoles } from "./consoles.js";
import {
  boardName, boardTitle, checkLabel, clock, deployBar, hexId, liveTitle, healthOf, holderAge, holderText,
  nameSourceText, usbRoute,
} from "./format.js";
import { html, render, useEffect, useRef, useState } from "./lib.js";
import { BoardSection } from "./sections/board.js";            // UI v2: Board's six pages
import { BoardXdcSection } from "./sections/xdc.js";
import { BuildSection } from "./sections/build.js";          // KIT-UI
import { attentionItems, OverviewSection } from "./sections/overview.js";
import { WorkbenchSection } from "./sections/workbench.js";    // UI v2: Program + Consoles + Debug
import { ChecksBanner, ChecksSection, checksRun } from "./sections/checks.js";   // HIL-GUI
import { HubFact } from "./hub.js";
import { ActivityDrawer, LastProblemChip } from "./drawer.js";   // UI v2: Activity is a drawer
import { ModalLayer, ModalShell, openModal, registerModal, ToastLayer } from "./modal.js";
import { epochOf } from "./week.js";
import { loadSettingValues } from "./prefs.js";          // FIX-PACK-4: the rows the page reads
import { LeaseBanners, requestClose } from "./lease.js";
import { registerTab, tabBody, tabsFor } from "./route.js";
import { AddByAddress, BoardList, P as SIDEBAR, routeText, ScanOffer, startSidebar } from "./sidebar.js";   // SIDEBAR-UX
import {
  boardState, changed, closeActivity, hubBoard, jobLabel, log, navigate, openActivity, openedBoard,
  openedOrClosedHere, probe, refreshInfo, rereadBoard, S, sectionOf, select, start, subscribe, timed,
  UI_NOTE, unseenErrors,
} from "./store.js";
import { applyTheme, initTheme } from "./theme.js";
import { AppUpdateBanners, AppUpdateLayer, SettingsButton, startSelfUpdate } from "./selfupdate.js";   // UPDATE-UI
import { CheckChip, Chip, Icon, LinkLine, MiniBar, Reason, Seg, Spinner, useReveal } from "./ui.js";

// --- the tabs' bodies: today's sections until each Phase 2 lane registers its own -----------

// Build: today's six steps, with the XDC page folded under them (0.1.0's "XDC" tab: its old
// key lands here with the fold open).
function BuildTab({ bid }) {
  const ref = useRef(null);
  const revealed = useReveal(bid, "xdc", ref);
  const [open, setOpen] = useState(revealed);
  useEffect(() => { if (revealed) setOpen(true); }, [revealed, S.ui.reveal && S.ui.reveal.at]);
  return html`<div class="stack">
    <${BuildSection} bid=${bid} />
    <section class="details" data-testid="xdc-fold" ref=${ref}>
      <button type="button" class="details-toggle" aria-expanded=${open ? "true" : "false"}
        data-action="xdc-fold" onClick=${() => setOpen(!open)}>
        <${Icon} name="chevron-right" cls=${`sm chev ${open ? "open" : ""}`} />Constraints (XDC)
        <span class="muted small">the RM kit for this shell's partition, the full-board export</span></button>
      ${open ? html`<div class="mt-14"><${BoardXdcSection} bid=${bid} /></div>` : null}
    </section>
  </div>`;
}

registerTab("overview", OverviewSection, { fallback: true });
registerTab("workbench", WorkbenchSection, { fallback: true });
registerTab("build", BuildTab, { fallback: true });
registerTab("board", BoardSection, { fallback: true });
registerTab("checks", ChecksSection, { fallback: true });

// --- the rail ------------------------------------------------------------------------------

function Rail() {
  const [adding, setAdding] = useState(false);
  const conn = S.connection;
  const daemon = conn === "ok" ? (S.eventsUp ? "ok" : "warn") : conn === "unknown" ? "unk" : "err";
  const daemonText = conn === "ok"
    ? `harness-manager-daemon ${S.version || ""}${S.eventsUp ? "" : " · events reconnecting"}`
    : conn === "auth" ? "session expired" : conn === "down" ? "harness-manager-daemon not answering" : "connecting...";
  const errors = unseenErrors();
  return html`<aside class="rail" aria-label="Boards">
    <div class="brand">
      <div class="brand-mark"><${Icon} name="circuit-board" /></div>
      <div>
        <div class="brand-name">Harness Manager</div>
        <div class="brand-sub">SoC Labs</div>
      </div>
    </div>
    <div class="rail-head">
      <span class="rail-title">Boards</span>
      <span class="rail-tools">
        <button type="button" class="btn ghost sm icon-only" title="Add a board by address"
          aria-label="Add a board by address" aria-expanded=${adding ? "true" : "false"}
          onClick=${() => setAdding(!adding)}><${Icon} name="plus" /></button>
        <button type="button" class="btn ghost sm icon-only" title="Scan for boards (and list the boards.toml ones)"
          aria-label="Scan for boards" data-action="rescan"
          aria-busy=${S.scan.running ? "true" : undefined} onClick=${() => { if (!S.scan.running) probe(); }}>
          ${S.scan.running ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`}</button>
      </span>
    </div>
    ${adding ? html`<${AddByAddress} onDone=${() => setAdding(false)} />` : null}
    ${S.scan.line ? html`<div class=${`rail-status ${S.scan.level === "err" ? "err" : ""}`}
      data-testid="scan-line">${S.scan.line}</div>` : null}
    <${ScanOffer} />
    <${BoardList} />
    <div class="rail-foot">
      <div class="foot-links">
        <button type="button" class="btn ghost sm" data-action="activity" aria-haspopup="dialog"
          aria-expanded=${S.ui.drawer ? "true" : "false"} title="Activity: what happened, newest first"
          onClick=${() => (S.ui.drawer ? closeActivity() : openActivity(S.selected))}><${Icon} name="history" />Activity
          ${errors ? html`<span class="foot-badge" data-testid="activity-badge"
            aria-label=${`${errors} new error${errors === 1 ? "" : "s"}`}>${errors}</span>` : null}</button>
        <${SettingsButton} />
        <button type="button" class="btn ghost sm" data-action="help" onClick=${openHelp}
          title="The command-line help, tab by tab"><${Icon} name="book-open" />Help</button>
      </div>
      <${Seg} label="Theme" value=${S.theme} onChange=${(v) => { applyTheme(v); changed(); }}
        options=${[
          { value: "system", label: "Auto", icon: "monitor", title: "Follow the system theme" },
          { value: "light", label: "Light", icon: "sun" },
          { value: "dark", label: "Dark", icon: "moon" },
        ]} />
      <div class="daemon-line" data-testid="daemon-line">
        <span class=${`dot ${daemon}`}></span><span class="grow"
          title=${S.daemon ? `${S.daemon.service || "harness-manager-daemon"} ${S.daemon.version || ""}, pid ${S.daemon.pid || "?"}` : ""}>${daemonText}</span>
      </div>
    </div>
  </aside>`;
}

// --- the header ------------------------------------------------------------------------------

function Fact({ label, children, testid = "" }) {
  return html`<div class="fact" data-testid=${testid || undefined}>
    <span class="fact-label">${label}</span><span class="fact-value">${children}</span></div>`;
}

// QUIET-POLL: background contact with this board is paused (the lease is someone else's, the
// board is busy with another client, or background reads are off). Calm, never red: only the
// reads nobody clicked wait; every button still works. Nothing shows while it may go ahead,
// or when the only reason is that no page views it (this one does, a moment later).
export function backgroundWords(st) {
  if (!st || st.allowed || !st.kind || st.kind === "no_viewer") return null;
  if (st.kind === "lease") {
    return { icon: "lock", level: "held", text: `Paused: lease held by ${st.holder || "someone else"}` };
  }
  if (st.kind === "lease_unknown") return { icon: "lock", level: "unk", text: "Paused: lease unknown" };
  if (st.kind === "busy") return { icon: "timer", level: "held", text: "Busy (another client)" };
  return { icon: "circle-pause", level: "unk", text: "Background reads off" };
}

function BackgroundFact({ bid }) {
  const b = boardState(bid);
  const st = (b.quiet && b.quiet.kind === "busy") ? { ...(b.background || {}), ...b.quiet, allowed: false }
    : b.background;
  const w = backgroundWords(st);
  if (!w) return null;
  return html`<${Fact} label="Background" testid="fact-background"><${Chip} level=${w.level}
    icon=${w.icon} testid="background-chip" title=${`${st.text || w.text}. Explicit actions still work.`}>
    ${w.text}<//><//>`;
}

// FIX-PACK-4: "Harness" meant two things that could disagree on one screen: the version the
// firmware reports (the version verb, 1.0.0) and the release of the signed catalogue the board
// runs (Update > Harness versions, 1.1.0). The header says which: the release when this page
// knows it (the Harness versions list was read), with the firmware's number beside it when that
// differs; otherwise "firmware 1.0.0". It never shows one number as if it were the other.
function harnessWords(ident, release) {
  const verb = (ident && ident.harness_version) || "";
  if (release) {
    return { source: "release", text: `release ${release}`, fw: verb && verb !== release ? `firmware ${verb}` : "",
      title: verb && verb !== release
        ? `Release ${release} of the signed harness catalogue: what Update > Harness versions matches this board to. `
          + `Its firmware reports version ${verb}: the firmware's own number, not the release.`
        : `Release ${release} of the signed harness catalogue (Update > Harness versions); the firmware reports ${verb || "no version"}.` };
  }
  if (verb) {
    return { source: "firmware", text: `firmware ${verb}`, fw: "",
      title: `What the harness firmware reports (its version verb). The catalogue release it belongs to shows here, `
        + "and in Update > Harness versions, once that list is read." };
  }
  return { source: "unknown", text: "unknown", fw: "", title: "" };
}

function HarnessFact({ bid, ident }) {
  const h = boardState(bid).harness;           // sections/harness.js: GET /harness/catalog, when read
  const board = (h && h.catalog && h.catalog.board) || null;
  const w = harnessWords(ident, board ? board.running_release : "");
  return html`<${Fact} label="Harness" testid="fact-harness">
    <span data-testid="fact-harness-value" data-source=${w.source} title=${w.title || undefined}>${w.text}</span>
    ${w.fw ? html`<span class="secondary" data-testid="fact-harness-fw" title=${w.title}>${` · ${w.fw}`}</span>` : null}
    ${ident.harness_version || ident.harness_impl
      ? html`<span class="secondary">${` · ${ident.harness_impl || "impl unknown"}`}</span>` : null}<//>`;
}

// UI v2: the design in the partition, and how this page knows it. While a deploy runs: its
// phase and the mini bar (the byte count and rate on hover). After one this page watched
// verify: "verified hh:mm". The harness's build check rides the fact's tooltip; only a check
// that is not OK (mismatch, unchecked) keeps its chip, since it asks for attention.
const BUILD_WORDS = {
  ok: "Build check OK: the harness firmware matches the fabric it runs on.",
  mismatch: "Build check MISMATCH: the harness firmware was built for different fabric.",
  unchecked: "Build check UNCHECKED: the harness could not compare its firmware with the fabric (not a pass).",
};

function sameId(a, c) { return !!a && !!c && String(a).toLowerCase() === String(c).toLowerCase(); }

function DesignFact({ bid, ident }) {
  const b = boardState(bid);
  const dep = b.deploy;
  const bar = deployBar(dep);
  const check = ident.build_check || "unchecked";
  const title = `The design in the partition${ident.rm_id ? ` (rm_id ${ident.rm_id})` : ""}. ${BUILD_WORDS[check] || `Build check ${checkLabel(check)}.`}`;
  const verified = !bar && dep.state === "done" && dep.verified && sameId(dep.rm_id, ident.rm_id) && dep.doneAt;
  return html`<div class="fact design-fact" data-testid="fact-design" data-check=${check} title=${title}>
    <span class="fact-label">Design</span>
    <span class="fact-value">
      ${bar ? html`<span class="pgm-fact" data-testid="design-progress" title=${`Programming ${bar.overlay}: ${bar.line}`}>
          <${Chip} level="accent" cls="busy"><${Spinner} />${bar.overlay} · ${bar.phase}${bar.pct ? ` ${bar.pct}` : ""}<//>
          <${MiniBar} bar=${bar} /></span>`
        : html`<span>${ident.rm_name || "unknown"}</span>${ident.rm_id ? html` <span class="mono">${hexId(ident.rm_id)}</span>` : null}`}
      ${verified ? html`<${Chip} level="ok" icon="circle-check" testid="design-verified"
          title=${`Read back from the board after programming, at ${clock(dep.doneAt)}. ${BUILD_WORDS[check] || ""}`}>verified ${clock(dep.doneAt).slice(0, 5)}<//>` : null}
      ${check !== "ok" ? html`<${CheckChip} check=${check} testid="build-chip" prefix="build " />` : null}
    </span>
  </div>`;
}

// UI v2: where the board's Debug USB (the MCC's cable) goes, from its links until the service
// serves the route (plan gap G2).
function UsbFact({ bid, cand }) {
  const u = usbRoute(cand, S.boards[bid]);
  return html`<div class=${`fact usb-fact ${u.to}`} data-testid="fact-usb" data-usb=${u.to} title=${`Debug USB: ${u.detail}`}>
    <span class="fact-label">Debug USB</span><span class="fact-value"><${Icon} name=${u.icon} cls="sm" />${u.fact}</span></div>`;
}

// DELETE /boards/{bid}; `release` (LEASE-UI) releases this Harness Manager's hub lease on it
// first (?release=true). Resolves to the timed() result; the board stays open on an error.
async function closeBoardNow(bid, { release = false } = {}) {
  const r = await timed(`close ${bid}${release ? " (and release its lease)" : ""}`,
    () => call("closeBoard", { bid }, undefined, release ? { release: "true" } : null));
  log(r.error ? "error" : "info", "session", r.error ? `${r.line}  ${r.error.message}` : r.line, bid);
  if (!r.error) {
    if (release) {
      const rel = r.data.data.released;
      log("info", "lease", rel ? `lease on ${rel.board || rel.target} released: other hub users may take the board`
        : "no hub lease was held here: nothing to release", bid);
    }
    openedOrClosedHere(bid, false);
    S.boards[bid].holder = null;
    closeBoardConsoles(bid);
    delete S.board[bid];
  }
  changed();
  return r;
}

function BoardHeader({ bid }) {
  const row = S.boards[bid] || {};
  const b = boardState(bid);
  const info = b.info;
  const cand = (info && info.candidate) || row.candidate || {};
  const ident = (info && info.identity) || cand.identity || {};
  const failed = b.infoError && b.infoError.errName !== "ABSENT";
  const health = failed ? { level: "err", text: "Not answering", detail: b.infoError.message } : healthOf(info);
  // LEASE-UI: a board whose hub lease THIS Harness Manager holds asks first (lease.js).
  const close = (e) => requestClose(bid, e.currentTarget, (o) => closeBoardNow(bid, o));
  const attention = attentionItems(bid);
  return html`<header class="board-header" data-testid="board-header">
    <div class="header-row1">
      <div class="header-titles">
        <div class="header-title-row">
          <h1 class="header-title" data-testid="header-name"
            title=${nameSourceText(cand) || undefined}>${cand.name || liveTitle(cand, ident, bid)}</h1>
          <${Chip} level=${health.level} testid="health-chip" title=${health.detail}
            icon=${health.level === "ok" ? "activity" : health.level === "err" ? "circle-x" : "circle-help"}>${health.text}<//>
        </div>
        ${cand.name ? html`<div class="header-sub" data-testid="header-sub">${liveTitle(cand, ident, bid)}</div>` : null}
        <div class="header-id">${bid}</div>
      </div>
      <div class="header-actions">
        <${LastProblemChip} bid=${bid} />
        ${b.job ? html`<${Chip} level="accent" testid="job-chip" title=${`harness-manager-daemon job ${b.job.id}: the board's other actions wait for it`}>
          <${Spinner} />${jobLabel(b.job.kind)} running · ${Math.floor((Date.now() - b.job.at) / 1000)} s<//>` : null}
        ${row.open ? html`<button type="button" class="btn ghost sm icon-only" data-action="refresh-board"
            aria-label="Read the board again" title="Read the board again (its info, the Card line and the SD journal)"
            onClick=${() => rereadBoard(bid)}
            aria-busy=${b.infoLoading ? "true" : undefined}>${b.infoLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`}</button>` : null}
        ${row.open ? html`<button type="button" class="btn sm" data-action="close-board" onClick=${close}
            title=${`Release the board's lock (${row.holder ? holderText(row.holder) : "this daemon"}'s) so other tools can use it; asks about the hub lease when it is yours`}>Close board</button>`
        : null}
      </div>
    </div>
    <div class="facts">
      <${Fact} label="Shell" testid="fact-shell"><span class="mono">${hexId(ident.shell_id) || "unknown"}</span><//>
      <${DesignFact} bid=${bid} ident=${ident} />
      <${HarnessFact} bid=${bid} ident=${ident} />
      <${UsbFact} bid=${bid} cand=${cand} />
      <${HubFact} bid=${bid} />
      <${BackgroundFact} bid=${bid} />
    </div>
    <nav class="sections" role="tablist" aria-label="Board sections">
      ${tabsFor(hubBoard(bid)).map((t) => {
        const badge = t.key === "overview" && attention.length ? html`<span class=${`badge ${attention.some((a) => a.level === "err") ? "" : "warn"}`}
              data-testid="overview-tab-badge" aria-label=${`${attention.length} to look at`}
              title=${attention.map((a) => a.title).join("\n")}>${attention.length}</span>`
          : t.key === "board" && b.pending ? html`<span class="badge" aria-label="needs attention"
              title="An SD install was interrupted: Board > Versions">!</span>`
          : t.key === "workbench" && b.deploy.state === "running" ? html`<span class="badge run" data-testid="workbench-tab-badge"
              aria-label="programming" title=${`Programming ${b.deploy.overlay || "a design"}`}><${Icon} name="loader-circle" cls="spin" /></span>`
          : t.key === "checks" && checksRun(bid) ? html`<span class="badge run" data-testid="checks-tab-badge"
              aria-label="a checks run is active" title="A checks run is active on this board"><${Icon} name="play" /></span>` : null;
        return html`<button type="button" role="tab" key=${t.key} class="section-tab"
          data-section=${t.key} aria-selected=${sectionOf(bid) === t.key ? "true" : "false"}
          onClick=${() => navigate(bid, t.key)}><${Icon} name=${t.icon} cls="sm" />${t.label}${badge}</button>`;
      })}
    </nav>
  </header>`;
}

// --- a board that is not open here: a preview and the Open button ----------------------------

// FIX-PACK-4: the preview's "Lock" was the service's own board lock, and read "free" for a board
// alice holds on the hub. It is named for what it is, and the hub lease has its own row.
const LOCK_TITLE = "Harness Manager's own lock on this board: free unless another Harness Manager "
  + "session or tool on this machine has it open. The hub lease is its own row.";

// The hub lease as the service last knew it (GET /boards lease_known: no hub call; it is read
// again when the board opens). A board the service never read shows "read when you open it"
// when boards.toml puts it behind a hub, and no row otherwise.
function PreviewLease({ row }) {
  const k = row.lease_known;
  const conf = row.configured || {};
  if (!k) {
    if (!conf.hub && conf.via !== "hub") return null;
    return html`<dt>Hub lease</dt><dd data-testid="preview-lease" data-lease="unread">
      <span class="muted">not read yet: read when you open the board</span></dd>`;
  }
  const at = epochOf(k.confirmed_at);
  const name = k.board || k.target || "the board";
  const title = `What this Harness Manager last read of the lease on ${name} (${k.hub || "the hub"})`
    + `${at === null ? "" : ` at ${clock(at)}`}; it is read again when you open the board.`;
  const state = k.state === "free" ? "free" : k.here ? "here" : k.mine ? "elsewhere" : "other";
  const chip = state === "free" ? html`<${Chip} icon="lock-open" title=${title}>free<//>`
    : state === "here" ? html`<${Chip} level="ok" icon="user" title=${title}>yours<//>`
    : html`<${Chip} level="held" icon="lock" title=${title}>held by ${k.holder || "someone else"}${state === "elsewhere" ? " (another session)" : ""}<//>`;
  return html`<dt>Hub lease</dt><dd data-testid="preview-lease" data-lease=${state}>${chip}
    ${at !== null ? html` <span class="muted small" data-testid="preview-lease-at">as of ${clock(at)}</span>` : null}</dd>`;
}

function BoardPreview({ bid }) {
  const row = S.boards[bid] || {};
  const cand = row.candidate || {};
  const ident = cand.identity || null;
  const [state, setState] = useState({ busy: false, line: "", error: null, t0: 0 });
  const open = async () => {
    setState({ busy: true, line: "", error: null, t0: Date.now() });
    const r = await timed(`open ${bid}`, () => call("openBoard", {}, { candidate: cand, note: UI_NOTE }));
    const already = r.error && r.error.errName === "ALREADY";   // open in this daemon: use it
    log(r.error && !already ? "error" : "info", "session",
      r.error ? `${r.line}  ${r.error.errName}: ${r.error.message}` : r.line, bid);
    if (r.error && !already) {
      if (r.error.holder) S.boards[bid] = { ...S.boards[bid], holder: { user: r.error.holder } };
      setState({ busy: false, line: r.line, error: r.error, t0: 0 });
      changed();
      return;
    }
    openedOrClosedHere(bid, true);
    const b = boardState(bid);
    const d = r.data ? r.data.data : {};
    if (d.info) {
      b.info = d.info;
      b.infoOkAt = Date.now() / 1000;
    }
    // The session is open even when the first read failed; the workspace says why.
    if (!d.info && d.info_error) b.infoError = new ApiError(d.info_error, 200);
    openedBoard(bid);
    setState({ busy: false, line: r.line, error: null, t0: 0 });
    changed();
  };
  const packTitle = S.packs[cand.pack] || cand.pack || "";
  const held = row.holder;
  const fromConfig = row.source === "config";       // SIDEBAR-UX: listed from boards.toml
  return html`<div class="section-body"><div class="preview stack">
    <section class="card" aria-label="Board">
      <div class="card-head"><h2 class="card-title" data-testid="preview-name"><${Icon} name="server" />${cand.name
        ? `${cand.name} · ${cand.label || boardTitle(cand, bid)}` : cand.label || boardTitle(cand, bid)}</h2></div>
      <p class="card-sub">${packTitle}${fromConfig ? " · in boards.toml: not contacted until you open it"
        : cand.evidence ? ` · found: ${cand.evidence}` : ""}</p>
      <div class="card-body">
        <dl class="kv">
          <dt>Board id</dt><dd class="mono">${bid}</dd>
          ${row.configured ? html`<dt>Route</dt><dd data-testid="preview-route">boards.toml <b>${row.configured.key}</b>: ${routeText(row.configured)}${row.configured.target ? html` <span class="mono sub">${row.configured.target}</span>` : null}</dd>` : null}
          <dt>Links</dt><dd>${(cand.links || []).map((l) => html`<${LinkLine} key=${l.kind + l.address} link=${l} />`)}</dd>
          <dt>Shell</dt><dd class="mono">${ident ? ident.shell_id || "unknown" : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Design</dt><dd>${ident ? html`${ident.rm_name || "unknown"} <span class="mono sub">${ident.rm_id}</span>` : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Harness firmware</dt><dd>${ident ? ident.harness_version || "unknown" : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Build check</dt><dd>${ident ? html`<${CheckChip} check=${ident.build_check} testid="preview-build" />` : html`<span class="muted">read when opened</span>`}</dd>
          <dt title=${LOCK_TITLE}>This app's lock</dt><dd data-testid="preview-lock">${held ? html`<${Chip} level="warn" icon="lock" title=${LOCK_TITLE}>held by ${holderText(held)}${held.since ? `, ${holderAge(held)}` : ""}<//>`
            : html`<${Chip} icon="lock-open" title=${LOCK_TITLE}>free<//>`}</dd>
          <${PreviewLease} row=${row} />
        </dl>
        <div class="open-row">
          <button type="button" class="btn primary" data-action="open" onClick=${open}
            aria-busy=${state.busy ? "true" : undefined}>
            ${state.busy ? html`<${Spinner} /> Opening...` : html`<${Icon} name="lock" /> Open board`}</button>
          <${Reason} text=${held
            ? "Open asks the daemon anyway: it refuses a live lock and takes over a stale one."
            : `Opening takes the board's lock for this daemon, so the CLI and this page share one session. ${boardName(cand, bid)} stays yours until you close it.`} />
        </div>
        ${state.line ? html`<div class="result mt-14" data-testid="open-result">
          <div><span class=${`rc ${state.error ? "err" : "ok"}`}>${state.line}</span></div>
          ${state.error ? html`<div><span class="errname">${state.error.errName}</span>  ${state.error.message}</div>
            ${state.error.hint ? html`<div class="hint">hint: ${state.error.hint}</div>` : null}
            <div class="hint">The board was not opened.</div>` : null}
        </div>` : null}
      </div>
    </section>
  </div></div>`;
}

// --- banners ------------------------------------------------------------------------------------

function sentence(text) {
  const t = String(text || "").trim();
  return t && !/[.!?]$/.test(t) ? `${t}.` : t;
}

function Banners({ bid }) {
  const out = [];
  if (S.connection === "auth") {
    out.push(html`<div class="banner err" role="alert" key="auth"><${Icon} name="lock" />
      <div class="grow"><strong>Session expired: run${" "}<code>harness-manager ui</code>${" "}again.</strong>${" "}
      harness-manager-daemon refused this page's token (it restarted, or the page came from an old link);
      ${" "}<code>harness-manager ui</code>${" "}opens the page with the current one.</div></div>`);
  } else if (S.connection === "down") {
    out.push(html`<div class="banner warn" role="status" key="down"><${Icon} name="unplug" />
      <div class="grow"><strong>harness-manager-daemon is not answering.</strong>${" "}The page keeps what it
      last read and retries. Check it with${" "}<code>harness-manager daemon status</code>.</div></div>`);
  }
  if (S.serviceEnv && S.serviceEnv.warning) {
    // FIX-PACK-2: a tool variable the service started with hides the user's own setting
    out.push(html`<div class="banner warn" role="status" key="env" data-testid="env-banner">
      <${Icon} name="triangle-alert" /><div class="grow">${sentence(S.serviceEnv.warning)}</div></div>`);
  }
  const b = bid && S.board[bid];
  if (b && b.infoError && b.infoError.errName !== "ABSENT") {
    out.push(html`<div class="banner warn" role="status" key="stale" data-testid="stale-banner">
      <${Icon} name="triangle-alert" /><div class="grow"><strong>The last read of this board failed.</strong>${" "}
      ${sentence(b.infoError.message)}${b.info && b.infoOkAt ? ` Showing what it said at ${clock(b.infoOkAt)}.` : ""}
      ${b.infoError.hint ? html`<div class="secondary small mt-8">${sentence(b.infoError.hint)}</div>` : null}</div>
      <button type="button" class="btn sm" onClick=${() => refreshInfo(bid)}>Read again</button></div>`);
  }
  if (b && b.pending) {
    out.push(html`<div class="banner err" role="alert" key="sd" data-testid="sd-banner">
      <${Icon} name="hard-drive" /><div class="grow"><strong>Interrupted SD install.</strong>${" "}
      Restore the configuration SD before anything else on this board.</div>
      <button type="button" class="btn sm danger" onClick=${() => navigate(bid, "board/versions")}>Go to recovery</button>
    </div>`);
  }
  return out;
}

// --- help: the CLI's own help text (GET /help/tabs), so the two never disagree -------------------

const help = { tabs: null, current: 0, line: "", error: null };

async function openHelp() {
  openModal("help");
  if (help.tabs) return;
  const r = await timed("help --tabs", () => call("helpTabs"));
  help.line = r.line;
  help.error = r.error;
  if (!r.error) help.tabs = r.data.data.tabs || [];
  changed();
}

function HelpDialog() {
  const tab = help.tabs && help.tabs[help.current];
  return html`<${ModalShell} title="Help" icon="book-open" testid="help" bodyCls="modal-body"
      note=${html`the same text as <code>harness-manager help --tabs</code>`}>
    ${help.error ? html`<div class="card-body"><${Reason} level="err" text=${`${help.line}: ${help.error.message}`} /></div>`
      : !help.tabs ? html`<div class="card-body muted"><${Spinner} /> Reading the help...</div>`
      : html`<nav class="modal-nav" aria-label="Help topics">${help.tabs.map((t, i) => html`<button type="button" key=${t.name}
          aria-current=${i === help.current ? "true" : "false"} onClick=${() => { help.current = i; changed(); }}>${t.name}</button>`)}</nav>
        <pre class="modal-text">${tab ? tab.text : ""}</pre>`}
  <//>`;
}

registerModal("help", HelpDialog);

// --- the app ------------------------------------------------------------------------------------

function Workspace() {
  const bid = S.selected;
  if (!bid || !S.boards[bid]) {
    if (S.connection === "unknown") {
      return html`<main class="workspace"><div class="section-body"><div class="empty-state"><div>
        <p class="muted"><${Spinner} /> Connecting to harness-manager-daemon...</p></div></div></div></main>`;
    }
    const noBoards = !S.order.length;
    return html`<main class="workspace">
      <${AppUpdateBanners} />
      <${Banners} bid=${null} />
      <div class="section-body">
        <div class="empty-state"><div>
          <div class="placeholder-art center"><${Icon} name="scan-search" cls="lg" /></div>
          <h2>${noBoards ? (S.scan.running ? "Looking for boards..." : "No boards yet") : "Select a board"}</h2>
          <p>${noBoards
            ? "Scan finds boards on the network and the USB links. A standalone MPS3 answers at 192.168.10.101 until it gets network discovery; add it by address with the + button."
            : "Pick a board in the list on the left."}</p>
          ${noBoards && !S.scan.running ? html`<p class="mt-14">
            <button type="button" class="btn primary" onClick=${() => probe()}><${Icon} name="refresh-cw" /> Scan for boards</button></p>` : null}
        </div></div>
      </div>
    </main>`;
  }
  const row = S.boards[bid];
  if (!row.open) {
    return html`<main class="workspace"><${AppUpdateBanners} /><${Banners} bid=${null} /><${LeaseBanners} bid=${null} />
      <${BoardPreview} key=${bid} bid=${bid} /></main>`;
  }
  const tab = sectionOf(bid);
  const body = tabBody(tab) || tabBody("overview");
  const Section = body.render;
  const label = (tabsFor(true).find((t) => t.key === tab) || {}).label || tab;
  return html`<main class="workspace" data-board=${bid}>
    <${AppUpdateBanners} />
    <${Banners} bid=${bid} />
    <${LeaseBanners} bid=${bid} />
    <${ChecksBanner} bid=${bid} />
    <${BoardHeader} bid=${bid} />
    <div class=${`section-body ${body.fill ? "fill" : ""}`} role="tabpanel"
      data-testid=${`section-${tab}`} data-tab=${tab} aria-label=${label}>
      <${Section} bid=${bid} />
    </div>
  </main>`;
}

function App() {
  const [, setN] = useState(0);
  useEffect(() => subscribe(() => setN((n) => n + 1)), []);
  const row = S.selected && S.boards[S.selected];
  const sel = row && S.board[S.selected];
  const named = (sel && sel.info && sel.info.candidate) || (row && row.candidate) || null;
  const title = row ? `${boardName(named, S.selected)} · Harness Manager` : "Harness Manager";
  if (document.title !== title) document.title = title;
  return html`<div class="app"><${Rail} /><${Workspace} /><${ActivityDrawer} />
    <${AppUpdateLayer} /><${ModalLayer} /><${ToastLayer} /></div>`;
}

// A read-only snapshot for the browser tests' failure reports and for the devtools console.
window.__harness_managerState = () => JSON.parse(JSON.stringify({
  connection: S.connection, eventsUp: S.eventsUp, selected: S.selected, boards: S.boards,
  sections: S.sections, subs: S.subs, route: window.location.hash, drawer: S.ui.drawer,
  jobs: Object.fromEntries(Object.entries(S.board).map(([bid, b]) => [bid, b.job])),
  sidebar: { order: SIDEBAR.order, favs: SIDEBAR.favs, where: SIDEBAR.where },
  log: S.log.slice(-80),
}));

initTheme();
initToken();
render(html`<${App} />`, document.getElementById("app"));
if (!hasToken()) S.connection = "auth";
startSidebar();
start();
startSelfUpdate();
loadSettingValues();
