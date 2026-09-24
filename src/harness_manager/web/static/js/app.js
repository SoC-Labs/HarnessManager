// Entry point: the rail (boards), the header (the selected board), the sections.

import { ApiError, call, hasToken, initToken } from "./api.js";
import { closeBoardConsoles } from "./consoles.js";
import {
  boardName, boardTitle, clock, designText, liveTitle, healthOf, holderAge, holderText, LINK_ICONS,
  linkName, nameSourceText,
} from "./format.js";
import { html, render, useEffect, useState } from "./lib.js";
import { ActivitySection } from "./sections/activity.js";
import { ConsolesSection } from "./sections/consoles.js";
import { DebugSection } from "./sections/debug.js";
import { OverviewSection } from "./sections/overview.js";
import { ClocksSection } from "./sections/clocks.js";
import { BoardXdcSection } from "./sections/xdc.js";
import { PowerSection } from "./sections/power.js";
import { ProgramSection } from "./sections/program.js";
import { SdSection } from "./sections/sd.js";
import { UpdateSection } from "./sections/update.js";
import { HubFact } from "./hub.js";
import { LeaseBanners } from "./lease.js";
import {
  boardState, changed, jobLabel, log, openedBoard, openedOrClosedHere, probe, refreshInfo, S,
  sectionOf, select, setSection, start, subscribe, timed, UI_NOTE,
} from "./store.js";
import { applyTheme, initTheme } from "./theme.js";
import { CheckChip, Chip, Icon, LinkLine, Reason, Seg, Spinner } from "./ui.js";

export const SECTIONS = [
  { key: "overview", label: "Overview", icon: "gauge", render: OverviewSection },
  { key: "program", label: "Program", icon: "upload", render: ProgramSection },
  { key: "consoles", label: "Consoles", icon: "terminal", render: ConsolesSection, fill: true },
  { key: "debug", label: "Debug", icon: "bug", render: DebugSection },
  { key: "power", label: "Power", icon: "power", render: PowerSection },
  { key: "clocks", label: "Clocks", icon: "clock", render: ClocksSection },
  { key: "sd", label: "SD card", icon: "hard-drive", render: SdSection },
  { key: "update", label: "Update", icon: "rocket", render: UpdateSection },
  { key: "xdc", label: "XDC", icon: "file-code", render: BoardXdcSection },
  { key: "activity", label: "Activity", icon: "history", render: ActivitySection },
];

// --- the rail ------------------------------------------------------------------------------

function BoardItem({ bid }) {
  const row = S.boards[bid] || {};
  const cand = row.candidate || {};
  const b = S.board[bid];
  const ident = (b && b.info && b.info.identity) || cand.identity || null;
  const named = (b && b.info && b.info.candidate) || cand;     // N1: the latest name
  const design = designText(ident);
  const shell = ident && ident.shell_id;
  const mine = !!row.open;
  const held = !mine && row.holder;
  let dot = "unk";
  let dotTitle = "not checked";
  if (mine && b && b.info) {
    const h = healthOf(b.info);
    dot = h.level;
    dotTitle = `${h.text}: ${h.detail}`;
  } else if (cand.evidence) {
    dot = "ok";
    dotTitle = `found: ${cand.evidence}`;
  }
  const kinds = [...new Set((cand.links || []).map((l) => l.kind))];
  return html`<li>
    <button type="button" class="board-item" aria-current=${S.selected === bid ? "true" : "false"}
      data-board=${bid} onClick=${() => select(bid)}>
      <div class="board-row1">
        <span class=${`dot ${dot}`} title=${dotTitle}></span>
        <span class="board-name" data-testid="rail-name"
          title=${named.name ? `${bid} · ${nameSourceText(named)}` : bid}>${boardName(named, bid)}</span>
        ${(b && b.job) || (row.job && !(b && b.readOnOpen)) ? html`<span class="i-muted" title="a job is running on this board"><${Spinner} /></span>` : null}
        ${mine ? html`<${Chip} level="accent" icon="user" cls="lock-chip">Yours<//>`
          : held ? html`<${Chip} level="warn" icon="lock" cls="lock-chip" title=${`held by ${holderText(row.holder)}`}>
              ${row.holder.user || "held"}<//>` : null}
      </div>
      <div class="board-row2">
        <span class="pack">${(cand.pack || String(bid).split("@")[0] || "").toUpperCase()}</span>
        ${design ? html` · ${design}` : html` · <span class="muted">design not read</span>`}
        ${shell ? html` · <span class="mono">${shell}</span>` : null}
      </div>
      <div class="board-row3">
        ${kinds.map((k) => html`<span key=${k} title=${linkName(k)}><${Icon} name=${LINK_ICONS[k] || "link"} cls="sm" /></span>`)}
        <span class="links-text">${kinds.map(linkName).join(" · ")}</span>
      </div>
    </button>
  </li>`;
}

// A board by address, optionally through a hub's SSH tunnel (L1: POST /probe {via:
// "ssh:HOST"}). The candidate it finds carries that route, so opening it needs no via.
function AddByAddress({ onDone }) {
  const [value, setValue] = useState("");
  const [hub, setHub] = useState("");
  const submit = (e) => {
    e.preventDefault();
    const host = value.trim();
    if (!host) return;
    const h = hub.trim();
    probe([host], h ? (h.startsWith("ssh:") ? h : `ssh:${h}`) : "");
    setValue("");
    onDone();
  };
  return html`<form class="rail-add" onSubmit=${submit}>
    <input class="input mono" placeholder="192.168.10.101[:6900]" aria-label="Board address"
      value=${value} onInput=${(e) => setValue(e.target.value)} autofocus />
    <button type="submit" class="btn sm">Add</button>
    <input class="input mono via" placeholder="through a hub: ssh host (optional)" aria-label="Through a hub (ssh host)"
      data-testid="add-via" value=${hub} onInput=${(e) => setHub(e.target.value)} />
  </form>`;
}

function Rail() {
  const [adding, setAdding] = useState(false);
  const conn = S.connection;
  const daemon = conn === "ok" ? (S.eventsUp ? "ok" : "warn") : conn === "unknown" ? "unk" : "err";
  const daemonText = conn === "ok"
    ? `harness-manager-daemon ${S.version || ""}${S.eventsUp ? "" : " · events reconnecting"}`
    : conn === "auth" ? "session expired" : conn === "down" ? "harness-manager-daemon not answering" : "connecting...";
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
        <button type="button" class="btn ghost sm icon-only" title="Scan for boards"
          aria-label="Scan for boards" data-action="rescan"
          aria-busy=${S.scan.running ? "true" : undefined} onClick=${() => { if (!S.scan.running) probe(); }}>
          ${S.scan.running ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`}</button>
      </span>
    </div>
    ${adding ? html`<${AddByAddress} onDone=${() => setAdding(false)} />` : null}
    ${S.scan.line ? html`<div class=${`rail-status ${S.scan.level === "err" ? "err" : ""}`}
      data-testid="scan-line">${S.scan.line}</div>` : null}
    <ul class="board-list">
      ${S.order.map((bid) => html`<${BoardItem} key=${bid} bid=${bid} />`)}
    </ul>
    <div class="rail-foot">
      <${Seg} label="Theme" value=${S.theme} onChange=${(v) => { applyTheme(v); changed(); }}
        options=${[
          { value: "system", label: "Auto", icon: "monitor", title: "Follow the system theme" },
          { value: "light", label: "Light", icon: "sun" },
          { value: "dark", label: "Dark", icon: "moon" },
        ]} />
      <div class="daemon-line" data-testid="daemon-line">
        <span class=${`dot ${daemon}`}></span><span class="grow"
          title=${S.daemon ? `${S.daemon.service || "harness-manager-daemon"} ${S.daemon.version || ""}, pid ${S.daemon.pid || "?"}` : ""}>${daemonText}</span>
        <button type="button" class="btn ghost sm" data-action="help" onClick=${openHelp}
          title="The command-line help, tab by tab"><${Icon} name="book-open" /> Help</button>
      </div>
    </div>
  </aside>`;
}

// --- the header ------------------------------------------------------------------------------

function Fact({ label, children, testid = "" }) {
  return html`<div class="fact" data-testid=${testid || undefined}>
    <span class="fact-label">${label}</span><span class="fact-value">${children}</span></div>`;
}

function BoardHeader({ bid }) {
  const row = S.boards[bid] || {};
  const b = boardState(bid);
  const info = b.info;
  const cand = (info && info.candidate) || row.candidate || {};
  const ident = (info && info.identity) || cand.identity || {};
  const failed = b.infoError && b.infoError.errName !== "ABSENT";
  const health = failed ? { level: "err", text: "Not answering", detail: b.infoError.message } : healthOf(info);
  const close = async () => {
    const r = await timed(`close ${bid}`, () => call("closeBoard", { bid }));
    log(r.error ? "error" : "info", "session", r.error ? `${r.line}  ${r.error.message}` : r.line, bid);
    if (!r.error) {
      openedOrClosedHere(bid, false);
      S.boards[bid].holder = null;
      closeBoardConsoles(bid);
      delete S.board[bid];
    }
    changed();
  };
  return html`<header class="board-header" data-testid="board-header">
    <div class="header-row1">
      <div class="header-titles">
        <h1 class="header-title" data-testid="header-name"
          title=${nameSourceText(cand) || undefined}>${cand.name || liveTitle(cand, ident, bid)}</h1>
        ${cand.name ? html`<div class="header-sub" data-testid="header-sub">${liveTitle(cand, ident, bid)}</div>` : null}
        <div class="header-id">${bid}</div>
      </div>
      <div class="header-actions">
        ${b.job ? html`<${Chip} level="accent" testid="job-chip" title=${`harness-manager-daemon job ${b.job.id}: the board's other actions wait for it`}>
          <${Spinner} />${jobLabel(b.job.kind)} running · ${Math.floor((Date.now() - b.job.at) / 1000)} s<//>` : null}
        ${row.open ? html`<button type="button" class="btn ghost sm icon-only" data-action="refresh-board"
            aria-label="Read the board again" title="Read the board again" onClick=${() => refreshInfo(bid)}
            aria-busy=${b.infoLoading ? "true" : undefined}>${b.infoLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`}</button>` : null}
        ${row.open ? html`<${Chip} level="accent" icon="user" testid="lock-chip"
            title=${row.holder ? `locked to ${holderText(row.holder)}` : "locked to this daemon"}>Yours<//>
          <button type="button" class="btn sm" onClick=${close}
            title="Release the board's lock so other tools can use it">Close board</button>`
        : null}
      </div>
    </div>
    <div class="facts">
      <${Fact} label="Shell" testid="fact-shell"><span class="mono">${ident.shell_id || "unknown"}</span><//>
      <${Fact} label="Design" testid="fact-design">${ident.rm_name || "unknown"}
        ${ident.rm_id ? html` <span class="mono">${ident.rm_id}</span>` : null}<//>
      <${Fact} label="Harness" testid="fact-harness">${ident.harness_version || "unknown"}
        ${ident.harness_version || ident.harness_impl
          ? html`<span class="secondary">${` · ${ident.harness_impl || "impl unknown"}`}</span>` : null}<//>
      <${Fact} label="Build"><${CheckChip} check=${ident.build_check} testid="build-chip" /><//>
      <${Fact} label="Health"><${Chip} level=${health.level} testid="health-chip" title=${health.detail}
        icon=${health.level === "ok" ? "activity" : health.level === "err" ? "circle-x" : "circle-help"}>
        ${health.text}<//><//>
      <${HubFact} bid=${bid} />
    </div>
    <nav class="sections" role="tablist" aria-label="Board sections">
      ${SECTIONS.map((s) => {
        const badge = s.key === "sd" && b.pending ? html`<span class="badge" aria-label="needs attention">!</span>` : null;
        return html`<button type="button" role="tab" key=${s.key} class="section-tab"
          data-section=${s.key} aria-selected=${sectionOf(bid) === s.key ? "true" : "false"}
          onClick=${() => setSection(bid, s.key)}><${Icon} name=${s.icon} cls="sm" />${s.label}${badge}</button>`;
      })}
    </nav>
  </header>`;
}

// --- a board that is not open here: a preview and the Open button ----------------------------

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
  return html`<div class="section-body"><div class="preview stack">
    <section class="card" aria-label="Board">
      <div class="card-head"><h2 class="card-title" data-testid="preview-name"><${Icon} name="server" />${cand.name
        ? `${cand.name} · ${cand.label || boardTitle(cand, bid)}` : cand.label || boardTitle(cand, bid)}</h2></div>
      <p class="card-sub">${packTitle}${cand.evidence ? ` · found: ${cand.evidence}` : ""}</p>
      <div class="card-body">
        <dl class="kv">
          <dt>Board id</dt><dd class="mono">${bid}</dd>
          <dt>Links</dt><dd>${(cand.links || []).map((l) => html`<${LinkLine} key=${l.kind + l.address} link=${l} />`)}</dd>
          <dt>Shell</dt><dd class="mono">${ident ? ident.shell_id || "unknown" : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Design</dt><dd>${ident ? html`${ident.rm_name || "unknown"} <span class="mono sub">${ident.rm_id}</span>` : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Harness</dt><dd>${ident ? ident.harness_version || "unknown" : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Build check</dt><dd>${ident ? html`<${CheckChip} check=${ident.build_check} testid="preview-build" />` : html`<span class="muted">read when opened</span>`}</dd>
          <dt>Lock</dt><dd>${held ? html`<${Chip} level="warn" icon="lock">held by ${holderText(held)}${held.since ? `, ${holderAge(held)}` : ""}<//>`
            : html`<${Chip} icon="lock-open">free<//>`}</dd>
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
      <button type="button" class="btn sm danger" onClick=${() => setSection(bid, "sd")}>Go to recovery</button>
    </div>`);
  }
  return out;
}

// --- help: the CLI's own help text (GET /help/tabs), so the two never disagree -------------------

const help = { open: false, tabs: null, current: 0, line: "", error: null };

async function openHelp() {
  help.open = true;
  changed();
  if (help.tabs) return;
  const r = await timed("help --tabs", () => call("helpTabs"));
  help.line = r.line;
  help.error = r.error;
  if (!r.error) help.tabs = r.data.data.tabs || [];
  changed();
}

function HelpModal() {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") { help.open = false; changed(); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (!help.open) return null;
  const tab = help.tabs && help.tabs[help.current];
  const close = () => { help.open = false; changed(); };
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) close(); }}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Help" data-testid="help">
      <div class="modal-head"><${Icon} name="book-open" /><h2 class="card-title">Help</h2>
        <span class="muted small">the same text as <code>harness-manager help --tabs</code></span>
        <span class="grow"></span>
        <button type="button" class="btn ghost sm icon-only" aria-label="Close help" onClick=${close} autofocus>
          <${Icon} name="x" /></button></div>
      ${help.error ? html`<div class="card-body"><${Reason} level="err" text=${`${help.line}: ${help.error.message}`} /></div>`
        : !help.tabs ? html`<div class="card-body muted"><${Spinner} /> Reading the help...</div>`
        : html`<div class="modal-body">
          <nav class="modal-nav" aria-label="Help topics">${help.tabs.map((t, i) => html`<button type="button" key=${t.name}
            aria-current=${i === help.current ? "true" : "false"} onClick=${() => { help.current = i; changed(); }}>${t.name}</button>`)}</nav>
          <pre class="modal-text">${tab ? tab.text : ""}</pre>
        </div>`}
    </div>
  </div>`;
}

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
    return html`<main class="workspace"><${Banners} bid=${null} /><${LeaseBanners} bid=${null} />
      <${BoardPreview} key=${bid} bid=${bid} /></main>`;
  }
  const section = SECTIONS.find((s) => s.key === sectionOf(bid)) || SECTIONS[0];
  const Section = section.render;
  return html`<main class="workspace" data-board=${bid}>
    <${Banners} bid=${bid} />
    <${LeaseBanners} bid=${bid} />
    <${BoardHeader} bid=${bid} />
    <div class=${`section-body ${section.fill ? "fill" : ""}`} role="tabpanel"
      data-testid=${`section-${section.key}`} aria-label=${section.label}>
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
  return html`<div class="app"><${Rail} /><${Workspace} /><${HelpModal} /></div>`;
}

// A read-only snapshot for the browser tests' failure reports and for the devtools console.
window.__harness_managerState = () => JSON.parse(JSON.stringify({
  connection: S.connection, eventsUp: S.eventsUp, selected: S.selected, boards: S.boards,
  jobs: Object.fromEntries(Object.entries(S.board).map(([bid, b]) => [bid, b.job])),
  log: S.log.slice(-80),
}));

initTheme();
initToken();
render(html`<${App} />`, document.getElementById("app"));
if (!hasToken()) S.connection = "auth";
start();
