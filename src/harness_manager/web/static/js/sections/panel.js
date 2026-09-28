// The board's front panel (lane P3; docs/design/CLCD_ALIGNMENT.md §5; david's decisions of
// 2026-09-24):
// - P5: a line and Identify in the Overview's Board tile ("Panel: status page · harness
//   owns it · touch ok"), and the Front panel card, the full mirror, in Details;
// - P6: "held" (violet) means someone else has it: here, the DUT owns the panel;
// - P2: a tap on the panel's lease-request banner notifies the holder (lease.js shows
//   "tapped on the panel"); it never releases;
// - P3: these are the Linux harness's features. On bare metal (v0.11) the mirror is rebuilt
//   from what Harness Manager read, and says so, and Identify is disabled with the reason.
//
// Reads (docs/API.md "Front panel"): GET .../panel (the state; the daemon reuses an answer
// up to 1 s old) and GET .../panel/frame (the 15 x 40 text grid; up to 3 s old). The
// panel.state event carries the state, and the mirror is read again after one. A read older
// than the event that asked for it is the daemon's cache: it is not applied, and is asked
// again once that cache has expired.

import { panelState } from "../actions.js";
import { call, heldByJob, routeMissing } from "../api.js";
import { ageText, clock, hostOf } from "../format.js";
import { html, useEffect } from "../lib.js";
import {
  bgOpts, boardState, changed, heldBack, onBoardEvent, onJobEnded, quietWords, S, timed,
} from "../store.js";
import { ActionRow, Card, Chip, Icon, QuietNote, Reason, ResultBlock, Spinner } from "../ui.js";
import { LiveDisplay } from "../display.js";        // LM4: the Live display, over the text mirror

const STATE_CACHE_MS = 1000;        // the daemon reuses a GET /panel answer this long
const FRAME_CACHE_MS = 3000;        // ... and a GET /panel/frame answer this long
const POLL_MS = 30000;              // a slow re-read while the page shows the panel
const RETRIES = 2;                  // stale answers re-asked after an event, at most
export const IDENTIFY_SECONDS = [5, 10, 20, 30];
const IDENTIFY_DEFAULT_S = 10;
const TAPS_SHOWN = 5;
export const REBUILT_TEXT = "rebuilt from what Harness Manager read, not read from the panel";

// --- per-board state ---------------------------------------------------------------------------

export function front(bid) {
  const b = boardState(bid);
  if (!b.front) {
    b.front = {
      bid,                   // FIX-PACK-1: whose it is (the mirror's Read now)
      body: null,            // GET /panel: {panel, reason, identify, support, presence}
      error: null, line: "", loading: false, again: false, readAt: 0, retries: 0,
      // FIX-PACK-1: the quiet answer of the last held-back read of the state / the mirror
      // (null once one was answered), and a Read now asked while a read was in flight.
      quiet: null, frameQuiet: null, againNow: false, frameAgainNow: false, explicit: false,
      frameExplicit: false,
      unsupported: false,    // a daemon without the front-panel routes
      deferred: false,       // a read waits for the board's job
      eventAt: 0,            // the daemon's time of the last panel.state event
      frame: null,           // GET /panel/frame: {rows, roles, source, observed_at, note}
      frameError: null, frameLine: "", frameLoading: false, frameAgain: false,
      frameAskedAt: 0, frameRetries: 0,
      cards: 0,              // Front panel cards on screen (they want the mirror)
      taps: [],              // [{seq, on, at}], newest first
      until: 0,              // Identify blinks until this (epoch s), 0 when it does not
      seconds: IDENTIFY_DEFAULT_S,
    };
  }
  return b.front;
}

function isStale(observedAt, f) {
  return !!f.eventAt && Number(observedAt || 0) < f.eventAt - 0.05;
}

function mergeRing(f, events) {
  if (!Array.isArray(events)) return;
  f.taps = events.map((e) => ({ seq: e.seq, on: e.on || "", at: e.at || 0 }))
    .sort((a, b) => b.seq - a.seq).slice(0, 8);
}

function addTap(f, d) {
  if (d.seq === undefined || f.taps.some((t) => t.seq === d.seq)) return;
  f.taps = [{ seq: d.seq, on: d.on || "", at: d.at || Date.now() / 1000 }, ...f.taps]
    .sort((a, b) => b.seq - a.seq).slice(0, 8);
}

// --- reads ---------------------------------------------------------------------------------------

// Every panel read is one nobody clicked (the tile's poll, an event, a mount): QUIET-POLL marks
// them background, and one the daemon held back keeps what the page last showed. `explicit`
// (Read now, FIX-PACK-1) is a click: never held back.
export async function loadPanel(bid, { explicit = false } = {}) {
  const f = front(bid);
  if (f.loading) {
    if (explicit) f.againNow = true; else f.again = true;
    return;
  }
  f.loading = true;
  f.explicit = explicit;
  changed();
  const r = await timed("panel show", () => call("panel", { bid }, undefined, null,
    explicit ? {} : bgOpts(bid)));
  f.loading = false;
  f.explicit = false;
  f.line = r.line;
  if (heldBack(bid, r)) {
    f.quiet = boardState(bid).quiet;
    f.again = false;
    if (f.againNow) { f.againNow = false; loadPanel(bid, { explicit: true }); }
    changed();
    return;
  }
  f.quiet = null;
  if (r.error) {
    if (routeMissing(r.error)) f.unsupported = true;
    else if (heldByJob(r.error)) f.deferred = true;       // read again when the job ends
    else f.error = r.error;
  } else {
    const d = r.data.data;
    const p = d.panel;
    if (p && isStale(p.observed_at, f) && f.retries < RETRIES && f.body && f.body.panel) {
      // The daemon's cached answer, older than the event we applied: keep the event's facts.
      f.retries += 1;
      f.body = { ...d, panel: f.body.panel };
      setTimeout(() => loadPanel(bid), STATE_CACHE_MS + 150);
    } else {
      f.retries = 0;
      f.body = d;
      if (p) mergeRing(f, p.events);
    }
    if (d.identify && d.identify.until) f.until = Number(d.identify.until) || 0;
    f.error = null;
    f.deferred = false;
    f.readAt = Date.now();
  }
  if (f.againNow) { f.againNow = false; f.again = false; loadPanel(bid, { explicit: true }); }
  else if (f.again) { f.again = false; loadPanel(bid); }
  changed();
}

export async function loadFrame(bid, { explicit = false } = {}) {
  const f = front(bid);
  if (f.frameLoading) {
    if (explicit) f.frameAgainNow = true; else f.frameAgain = true;
    return;
  }
  f.frameLoading = true;
  f.frameExplicit = explicit;
  f.frameAskedAt = Date.now();
  changed();
  const r = await timed("panel mirror", () => call("panelFrame", { bid }, undefined, null,
    explicit ? {} : bgOpts(bid)));
  f.frameLoading = false;
  f.frameExplicit = false;
  f.frameLine = r.line;
  if (heldBack(bid, r)) {
    f.frameQuiet = boardState(bid).quiet;
    f.frameAgain = false;
    if (f.frameAgainNow) { f.frameAgainNow = false; loadFrame(bid, { explicit: true }); }
    changed();
    return;
  }
  f.frameQuiet = null;
  if (f.frameAgainNow) { f.frameAgainNow = false; f.frameAgain = false; loadFrame(bid, { explicit: true }); }
  if (r.error) {
    if (routeMissing(r.error)) f.unsupported = true;
    else if (heldByJob(r.error)) f.deferred = true;
    else f.frameError = r.error;
  } else {
    const d = r.data.data;
    if (isStale(d.observed_at, f) && f.frameRetries < RETRIES) {
      f.frameRetries += 1;
      if (!f.frame) f.frame = d;
      f.frameAgain = true;               // the daemon's cached grid: ask when it has expired
    } else {
      f.frameRetries = 0;
      f.frame = d;
    }
    f.frameError = null;
  }
  if (f.frameAgain) { f.frameAgain = false; scheduleFrame(bid); }
  changed();
}

const frameTimers = {};

// FIX-PACK-1: Read now: one explicit read of the panel's state, and of its mirror when a Front
// panel card shows it. A click, so never held back.
export function readPanelNow(bid) {
  const f = front(bid);
  loadPanel(bid, { explicit: true });
  if (f.cards > 0) loadFrame(bid, { explicit: true });
}

// The mirror again, no sooner than the daemon's 3 s reuse of the last one this page asked for.
function scheduleFrame(bid) {
  const f = front(bid);
  const wait = Math.max(0, f.frameAskedAt + FRAME_CACHE_MS + 100 - Date.now());
  clearTimeout(frameTimers[bid]);
  frameTimers[bid] = setTimeout(() => loadFrame(bid), wait);
}

// --- events ----------------------------------------------------------------------------------------

onBoardEvent((ev) => {
  const bid = ev.board_id;
  if (!bid || !ev.topic.startsWith("panel.")) return;
  const f = front(bid);
  const d = ev.data || {};
  if (ev.topic === "panel.state") {
    f.eventAt = Math.max(f.eventAt, Number(ev.at) || 0);
    const body = f.body;
    if (!body || !body.panel) {
      loadPanel(bid);
    } else {
      const prev = body.panel;
      const sid = body.presence && body.presence.sid;
      // A hello reply counts the sessions but does not list them: keep the last list.
      const listed = Array.isArray(d.sessions) && d.sessions.length > 0;
      const sessions = listed ? d.sessions.map((s) => ({ ...s, mine: !!s.mine || (!!sid && s.sid === sid) }))
        : prev.sessions || [];
      body.panel = { ...prev, ...d, sessions, events: prev.events || [], observed_at: f.eventAt };
      if (!listed && (d.count || 0) !== sessions.length) setTimeout(() => loadPanel(bid), STATE_CACHE_MS + 150);
    }
    f.frameRetries = 0;
    if (f.cards > 0) scheduleFrame(bid);
  } else if (ev.topic === "panel.tap") {
    addTap(f, d);
  } else if (ev.topic === "panel.locate") {
    f.until = d.state === "on" ? Number(d.until) || 0 : 0;
  }
});

onJobEnded((bid) => {
  const b = S.board[bid];
  const f = b && b.front;
  if (!f || !(f.body || f.deferred)) return;
  loadPanel(bid);
  if (f.cards > 0) scheduleFrame(bid);
});

// Identify's "blinking until" goes when the time has passed.
setInterval(() => {
  const now = Date.now() / 1000;
  let any = false;
  for (const b of Object.values(S.board)) {
    if (b.front && b.front.until && b.front.until <= now) { b.front.until = 0; any = true; }
  }
  if (any) changed();
}, 1000);

// --- words and levels ------------------------------------------------------------------------------

export function ownerPart(p) {
  if (p.pending) return { text: "handing over...", level: "busy", icon: "loader-circle" };
  if (p.owner === "harness") return { text: "harness owns it", level: "ok", icon: "" };
  if (p.owner === "dut") return { text: "DUT owns it", level: "held", icon: "lock" };
  return { text: "owner unknown", level: "unk", icon: "" };
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

export function touchPart(t) {
  const touch = t || {};
  if (touch.ok === true) return { text: "touch ok", level: "ok" };
  if (touch.ok === false) {
    const extra = [];
    if (touch.bus_lost !== null && touch.bus_lost !== undefined) extra.push(`bus lost ${touch.bus_lost}×`);
    if (touch.recoveries !== null && touch.recoveries !== undefined) {
      extra.push(plural(touch.recoveries, "recovery", "recoveries"));
    }
    return { text: `touch unavailable${extra.length ? ` (${extra.join(", ")})` : ""}`, level: "err",
      title: touch.reason || "the harness says the touch controller is not answering" };
  }
  return { text: "touch unknown", level: "unk", title: "the harness does not report the touch controller's health" };
}

function pagePart(p) {
  return p.page ? { text: `${p.page} page`, level: "" }
    : { text: "page not reported", level: "unk", title: p.source === "rebuilt" ? "a bare-metal harness does not say which page it shows" : "" };
}

// The Board tile's parts: [{text, level, icon, title, key}].
export function lineParts(f) {
  if (f.unsupported) return [{ key: "none", text: "this harness-manager-daemon has no front-panel routes", level: "unk", icon: "circle-slash" }];
  if (f.error && !f.body) return [{ key: "err", text: `${f.error.errName}: ${f.error.message}`, level: "err", icon: "circle-x" }];
  // FIX-PACK-1: held back before the first read: say so (the Board tile has Read now).
  if (!f.body && f.quiet) return [{ key: "quiet", text: "not read", level: "unk", icon: "circle-pause", title: quietWords(f.quiet) }];
  if (!f.body) return [{ key: "loading", text: "reading...", level: "muted" }];
  const p = f.body.panel;
  if (!p) return [{ key: "none", text: `not available: ${f.body.reason || "this board has no front panel Harness Manager can reach"}`, level: "unk", icon: "circle-slash" }];
  return [{ key: "page", ...pagePart(p) }, { key: "owner", ...ownerPart(p) }, { key: "touch", ...touchPart(p.touch) }];
}

function Parts({ parts }) {
  return parts.map((pt, i) => html`${i ? html`<span class="pl-sep" key=${`s${pt.key}`}> · </span>` : null}<span key=${pt.key}
    class=${`pl ${pt.level || ""}`} data-part=${pt.key} data-level=${pt.level || undefined} title=${pt.title || undefined}>${pt.icon
      ? html`<${Icon} name=${pt.icon} cls=${pt.icon === "loader-circle" ? "spin" : ""} />` : null}${pt.text}</span>`);
}

function RebuiltTag({ f, testid }) {
  const p = f.body && f.body.panel;
  if (!p || p.source !== "rebuilt") return null;
  return html`<span class="tag rebuilt-tag" data-testid=${testid}
    title=${`${p.note || REBUILT_TEXT}. The live panel needs the Linux harness.`}>rebuilt</span>`;
}

// --- Identify ------------------------------------------------------------------------------------

export function identifyWhy(bid) {
  const f = front(bid);
  if (f.unsupported) return "Cannot: this harness-manager-daemon has no front-panel routes";
  if (!f.body) {
    if (f.error) return `Cannot: ${f.error.message}`;
    return f.quiet ? `Not read yet: ${quietWords(f.quiet)}` : "reading the panel...";
  }
  const id = f.body.identify || {};
  if (!id.available) return `Cannot: ${id.reason || f.body.reason || "this board cannot identify itself"}`;
  return "";
}

function identifySpec(bid, seconds) {
  const f = front(bid);
  return {
    key: seconds ? "identify" : "identify_stop",
    label: seconds ? "Identify" : "Stop blinking",
    busyLabel: seconds ? "Asking the board..." : "Stopping...",
    budgetS: 15,
    command: `identify ${hostOf(bid)} --seconds ${seconds}`,
    run: async () => (await call("identify", { bid }, { seconds })).data,
    render: (r) => [{ kind: "ok", text: seconds
      ? `the panel's backlight blinks until ${clock(Number(r.until))}` : "the blink is stopped" }],
    onDone: (ok, r) => {
      if (ok) f.until = seconds ? Number(r && r.until) || 0 : 0;
    },
  };
}

export function IdentifyControl({ bid, testid = "identify" }) {
  const f = front(bid);
  const p = panelState(bid, "identify");
  const why = identifyWhy(bid);
  const blinking = f.until > Date.now() / 1000;
  const spec = identifySpec(bid, blinking ? 0 : f.seconds);
  // A failure, or a click the gate stopped ("Nothing was run."): the answer stays in view.
  const failed = p.lines.length > 0 && !p.running && (p.lines[0].level === "err" || !!p.lines[0].notRun);
  return html`<div class="identify" data-testid=${testid} data-blinking=${blinking ? "yes" : "no"}>
    <${ActionRow} bid=${bid} panel="identify" spec=${spec} icon=${blinking ? "square" : "scan-search"}
      compact=${true} gate=${{ guard: () => why }}>
      ${blinking ? html`<span class="identify-until" data-testid="identify-until"><${Icon} name="timer" cls="sm" />blinking until ${clock(f.until)}</span>`
        : html`<select class="select identify-seconds" aria-label="How long the panel blinks"
          data-testid="identify-seconds" disabled=${!!why}
          onChange=${(e) => { f.seconds = Number(e.target.value) || IDENTIFY_DEFAULT_S; changed(); }}>
          ${IDENTIFY_SECONDS.map((s) => html`<option key=${s} value=${String(s)} selected=${s === f.seconds}>${s} s</option>`)}
        </select>`}
    <//>
    ${failed ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="identify-result" />` : null}
  </div>`;
}

// --- the Board tile's line -----------------------------------------------------------------------

function usePanelReads(bid) {
  useEffect(() => {
    const f = front(bid);
    if (!f.body && !f.loading) loadPanel(bid);
    const timer = setInterval(() => {
      const b = boardState(bid);
      if (document.visibilityState === "visible" && !b.job) loadPanel(bid);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [bid]);
}

// Two cells of the tile's key/value grid: "Panel" and its line, with Identify under it.
export function PanelTileRow({ bid }) {
  usePanelReads(bid);
  const f = front(bid);
  return html`<span class="k">Panel</span>
    <span class="v" data-testid="tile-panel">
      <span class="panel-line" data-testid="tile-panel-line"><${Parts} parts=${lineParts(f)} />${" "}<${RebuiltTag} f=${f} testid="tile-panel-rebuilt" /></span>
      ${f.body && f.body.panel ? html`<${IdentifyControl} bid=${bid} testid="tile-identify" />` : null}
    </span>`;
}

// --- the Front panel card (Details) ---------------------------------------------------------------

function runs(row, roles) {
  const out = [];
  let cur = null;
  for (let i = 0; i < row.length; i += 1) {
    const r = roles[i] || "t";
    if (!cur || cur.r !== r) { cur = { r, text: "" }; out.push(cur); }
    cur.text += row[i];
  }
  return out;
}

const ROLE_CLASS = { i: "pm-inv" };      // a rebuilt frame: t text, i inverted (today's red)

function Mirror({ f }) {
  const fr = f.frame;
  if (!fr) {
    if (f.frameError) {
      return html`<${Reason} level="err" testid="panel-mirror-error" text=${`${f.frameError.errName}: ${f.frameError.reason || f.frameError.message}`} />`;
    }
    if (f.frameQuiet) {
      return html`<${QuietNote} testid="panel-mirror-quiet" action="panel-mirror-read-now"
        text=${quietWords(f.frameQuiet)} busy=${f.frameLoading && f.frameExplicit}
        onRead=${() => loadFrame(f.bid, { explicit: true })} />`;
    }
    return html`<p class="muted small"><${Spinner} /> Reading the panel's text...</p>`;
  }
  const rows = Array.isArray(fr.rows) ? fr.rows : [];
  const roles = String(fr.roles || "");
  const rebuilt = fr.source === "rebuilt";
  return html`<div class=${`panel-mirror ${rebuilt ? "rebuilt" : ""}`} data-testid="panel-mirror"
      data-source=${fr.source || ""} role="group"
      aria-label=${`The front panel's text, ${rows.length} rows${rebuilt ? `, ${REBUILT_TEXT}` : ""}`}>
    ${rows.map((row, r) => {
      const text = String(row).padEnd(40).slice(0, 40);
      return html`<div class="pm-row" key=${r} data-row=${r}>${runs(text, roles.slice(r * 40, r * 40 + 40))
        .map((run, i) => html`<span key=${i} class=${ROLE_CLASS[run.r] || undefined}>${run.text}</span>`)}</div>`;
    })}
  </div>`;
}

const ROLE_WORDS = { holder: "holds the lease", owner: "has it open", watch: "watching" };

function Sessions({ f }) {
  const p = f.body.panel;
  const support = f.body.support || {};
  if (p.source === "rebuilt") {
    return html`<span class="muted" data-testid="panel-sessions-none">not reported: ${support.presence || "needs the Linux harness"}</span>`;
  }
  const list = p.sessions || [];
  if (!list.length) {
    return html`<span class="muted" data-testid="panel-sessions-none">${p.count ? `${plural(p.count, "session", "sessions")}, not listed yet` : "no Harness Manager connected"}</span>`;
  }
  const now = Date.now() / 1000;
  return html`<ul class="pm-list" data-testid="panel-sessions">${list.map((s) => html`<li key=${s.sid}
      data-session=${s.sid} data-mine=${s.mine ? "yes" : "no"}>
    <${Icon} name="user" cls="sm i-muted" /><span class="mono">${s.who}</span>
    <span class="secondary">${ROLE_WORDS[s.role] || s.role}</span>
    ${s.mine ? html`<${Chip} level="accent" testid="panel-session-mine" title="this Harness Manager">you<//>` : null}
    <span class="muted small">${ageText(now - Number(s.age_s || 0), now)}</span>
  </li>`)}</ul>`;
}

const TAP_WORDS = { request: "the lease request", identify: "Identify (found it)", nav: "next page" };

function Taps({ f }) {
  const p = f.body.panel;
  if (p.source === "rebuilt") return html`<span class="muted" data-testid="panel-taps-none">not reported by a bare-metal harness</span>`;
  if (!f.taps.length) return html`<span class="muted" data-testid="panel-taps-none">none</span>`;
  return html`<ul class="pm-list" data-testid="panel-taps">${f.taps.slice(0, TAPS_SHOWN).map((t) => html`<li key=${t.seq}
      data-tap=${t.seq} data-on=${t.on}>
    <${Icon} name="user" cls="sm i-muted" /><span>tapped ${TAP_WORDS[t.on] || t.on || "the glass"}</span>
    <span class="muted small">${t.at ? clock(t.at) : ""}</span>
  </li>`)}</ul>`;
}

function OwnerChip({ p }) {
  const o = ownerPart(p);
  return html`<${Chip} level=${o.level === "busy" ? "accent" : o.level} testid="panel-owner-chip"
    icon=${o.level === "ok" ? "circle-check" : o.level === "held" ? "lock" : o.level === "busy" ? "" : "circle-help"}>${o.text}<//>`;
}

export function PanelCard({ bid }) {
  const f = front(bid);
  useEffect(() => {
    const g = front(bid);
    g.cards += 1;
    if (!g.body && !g.loading) loadPanel(bid);
    loadFrame(bid);
    const timer = setInterval(() => {
      const b = boardState(bid);
      if (document.visibilityState === "visible" && !b.job) loadFrame(bid);
    }, POLL_MS);
    return () => { g.cards -= 1; clearInterval(timer); };
  }, [bid]);
  const p = f.body && f.body.panel;
  const actions = p ? html`<span class="row"><${RebuiltTag} f=${f} testid="panel-rebuilt-tag" /><${OwnerChip} p=${p} /></span>` : null;
  const now = Date.now() / 1000;
  let body;
  if (f.unsupported || !f.body || !p) {
    const parts = lineParts(f);
    if (parts[0].key === "quiet") {
      // FIX-PACK-1: background reads are off (or paused): never a spinner that waits for ever.
      body = html`<${QuietNote} testid="panel-quiet" action="panel-read-now" busy=${f.loading && f.explicit}
        text=${quietWords(f.quiet)}
        onRead=${() => readPanelNow(bid)} />`;
    } else {
      body = html`<${Reason} level=${parts[0].level === "err" ? "err" : "unk"} icon=${parts[0].icon || ""}
        testid="panel-unavailable" text=${parts[0].key === "loading" ? "Reading the panel..." : parts[0].text} />`;
    }
  } else {
    const touch = touchPart(p.touch);
    body = html`
      ${p.source === "rebuilt"
        ? html`<div class="mb-12"><${Reason} level="unk" icon="circle-help" testid="panel-rebuilt"
            text=${`Rebuilt from what Harness Manager read, not read from the panel. This harness (bare metal) reports only who owns the panel; the live mirror, who is connected and Identify need the Linux harness.`} /></div>`
        : html`<p class="muted small mb-12" data-testid="panel-read-age">Read from the panel${p.observed_at ? `, ${ageText(p.observed_at, now)}` : ""}.</p>`}
      <${LiveDisplay} bid=${bid}><${Mirror} f=${f} /><//>
      <dl class="kv mt-14">
        <dt>Page</dt><dd data-testid="panel-page"><${Parts} parts=${[{ key: "page", ...pagePart(p) }]} /></dd>
        <dt>Owner</dt><dd data-testid="panel-owner"><${Parts} parts=${[{ key: "owner", ...ownerPart(p) }]} /></dd>
        ${p.banner ? html`<dt>Banner</dt><dd class="mono" data-testid="panel-banner">${p.banner}</dd>` : null}
        ${p.card ? html`<dt>Card</dt><dd class="mono" data-testid="panel-card-slot">${p.card}</dd>` : null}
        <dt>Touch</dt><dd data-testid="panel-touch"><${Parts} parts=${[{ key: "touch", ...touch }]} /></dd>
        <dt>Sessions</dt><dd><${Sessions} f=${f} /></dd>
        <dt>Recent taps</dt><dd><${Taps} f=${f} /></dd>
        <dt>Identify</dt><dd><${IdentifyControl} bid=${bid} testid="panel-identify" /></dd>
      </dl>`;
  }
  return html`<${Card} title="Front panel" icon="monitor" actions=${actions} testid="panel-card">${body}<//>`;
}
