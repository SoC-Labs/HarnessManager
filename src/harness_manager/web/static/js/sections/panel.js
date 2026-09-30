// The board's front panel (lane P3, docs/design/CLCD_ALIGNMENT.md §5; UI v2 round 3, lane
// UI2-OVERVIEW): the Overview's hero card, the ONLY front panel in the app (the Workbench's rail
// no longer has one, the Board tile and the Details fold are gone). Its head: Live / Text and
// Identify (5-30 s). Live is the board's own picture (LM4, display.js, fitted to the card); Text
// is the panel's rows as text (the mirror below). The foot: what the picture is (PANEL-TRUTH's
// one headline), owner · page · touch, the last tap, and what this image does not report. Who is
// connected (presence) is the Overview's "Watching" line.
//
// david's decisions of 2026-09-24 still hold: P6 "held" (violet) means someone else has it (the
// DUT owns the panel); P2 a tap on the panel's lease-request banner notifies the holder (lease.js
// shows "tapped on the panel"); it never releases; P3 these are harness features ('panel',
// 'presence', 'locate'). On an image without 'panel' the mirror is rebuilt from what Harness
// Manager read, and says so, and Identify is disabled with the reason. PANEL-TRUTH (2026-09-28):
// every word is about what THIS IMAGE reports, by capability and feature; the harness type is
// named only from its own version.impl, never guessed from a missing feature.
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
import { ActionRow, Chip, Icon, QuietNote, Reason, ResultBlock, Spinner } from "../ui.js";
import { LiveDisplay } from "../display.js";        // LM4: the Live display, over the text mirror
import { displayLive, liveRefusal } from "../display.js";   // PANEL-TRUTH: the headline's "Live"
import { GLYPHS, ROLE_COLOURS, ROLES } from "../panel_codes.js";   // PANEL-V017: generated
import { settingValue } from "../prefs.js";

const STATE_CACHE_MS = 1000;        // the daemon reuses a GET /panel answer this long
const FRAME_CACHE_MS = 3000;        // ... and a GET /panel/frame answer this long
const POLL_MS = 30000;              // a slow re-read while the page shows the panel
const RETRIES = 2;                  // stale answers re-asked after an event, at most
export const IDENTIFY_SECONDS = [5, 10, 20, 30];
// FIX-PACK-4: the Front panel card's Identify time defaults to the setting panel.identify_s
// (prefs.js; 5 s unless changed, as the sidebar's and the tile's one-click buttons).
function identifyDefault() {
  const s = Number(settingValue("panel.identify_s"));
  return Number.isInteger(s) && s >= 1 && s <= 30 ? s : 5;
}
const TAPS_SHOWN = 5;
export const REBUILT_TEXT = "rebuilt from what Harness Manager read, not read from the panel";
// PANEL-TRUTH: a fact the rebuilt text does not have (core/panel.py UNKNOWN), and its legend.
export const UNKNOWN_MARK = "\u2014";
export const NOT_REPORTED = "not reported by this image";

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
      seconds: 0,            // 0: the setting's (identifyDefault); a pick here wins
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
    : { text: "page not reported", level: "unk", title: "this harness image does not report which page the panel shows (harness feature 'panel')" };
}

// PANEL-TRUTH: the harness type in words, from its own version.impl only ("" when it did not
// say). Never from a missing feature.
export function implWords(support) {
  const impl = (support && support.impl) || "";
  return { linux: "Linux harness", "bare-metal": "bare-metal harness" }[impl] || "";
}

// "this Linux harness image" when the harness said what it is, else "this harness image".
function thisImage(support) {
  const w = implWords(support);
  return `this ${w || "harness"} image`;
}

// What the image does not report, by capability, with the feature that would report it:
// [{key, what, feature}] (the card's one "Not reported by this image" line).
export function notReported(f) {
  const p = f.body && f.body.panel;
  if (!p) return [];
  const support = f.body.support || {};
  const out = [];
  if (!p.page) out.push({ key: "page", what: "page", feature: "harness feature 'panel'" });
  const t = p.touch || {};
  if (t.ok !== true && t.ok !== false) out.push({ key: "touch", what: "touch health", feature: "the harness's stats (touch_ok)" });
  if (p.source === "rebuilt") {
    // a rebuilt state lists no one (the list comes with 'panel'; 'presence' announces us)
    out.push({ key: "sessions", what: "who is connected",
      feature: support.presence ? "harness feature 'presence'" : "harness feature 'panel'" });
    out.push({ key: "taps", what: "recent taps", feature: "harness feature 'panel'" });
  }
  return out;
}

// The Board tile's parts: [{text, level, icon, title, key}]. PANEL-TRUTH: a page the image
// does not report is left out (the Front panel card lists what is not reported).
export function lineParts(f) {
  if (f.unsupported) return [{ key: "none", text: "this harness-manager-daemon has no front-panel routes", level: "unk", icon: "circle-slash" }];
  if (f.error && !f.body) return [{ key: "err", text: `${f.error.errName}: ${f.error.message}`, level: "err", icon: "circle-x" }];
  // FIX-PACK-1: held back before the first read: say so (the Board tile has Read now).
  if (!f.body && f.quiet) return [{ key: "quiet", text: "not read", level: "unk", icon: "circle-pause", title: quietWords(f.quiet) }];
  if (!f.body) return [{ key: "loading", text: "reading...", level: "muted" }];
  const p = f.body.panel;
  if (!p) return [{ key: "none", text: `not available: ${f.body.reason || "this board has no front panel Harness Manager can reach"}`, level: "unk", icon: "circle-slash" }];
  const page = p.page ? [{ key: "page", ...pagePart(p) }] : [];
  return [...page, { key: "owner", ...ownerPart(p) }, { key: "touch", ...touchPart(p.touch) }];
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
    title=${`${p.note || REBUILT_TEXT}: ${thisImage(f.body.support)} does not send its panel's text (harness feature 'panel')`}>rebuilt</span>`;
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
      ? `the panel blinks until ${clock(Number(r.until))}` : "the blink is stopped" }],
    onDone: (ok, r) => {
      if (ok) f.until = seconds ? Number(r && r.until) || 0 : 0;
    },
  };
}

export function IdentifyControl({ bid, testid = "identify", head = false }) {
  const f = front(bid);
  const p = panelState(bid, "identify");
  const why = identifyWhy(bid);
  const blinking = f.until > Date.now() / 1000;
  const seconds = f.seconds || identifyDefault();
  const choices = [...new Set([...IDENTIFY_SECONDS, seconds])].sort((a, b) => a - b);
  const spec = identifySpec(bid, blinking ? 0 : seconds);
  // A failure, or a click the gate stopped ("Nothing was run."): the answer stays in view.
  const failed = p.lines.length > 0 && !p.running && (p.lines[0].level === "err" || !!p.lines[0].notRun);
  return html`<div class="identify" data-testid=${testid} data-blinking=${blinking ? "yes" : "no"}>
    <${ActionRow} bid=${bid} panel="identify" spec=${spec} icon=${blinking ? "square" : "scan-search"}
      compact=${true} gate=${{ guard: () => why }} showReason=${!head}>
      ${blinking ? html`<span class="identify-until" data-testid="identify-until"><${Icon} name="timer" cls="sm" />blinking until ${clock(f.until)}</span>`
        : html`<select class="select identify-seconds" aria-label="How long the panel blinks"
          data-testid="identify-seconds" disabled=${!!why}
          onChange=${(e) => { f.seconds = Number(e.target.value) || 0; changed(); }}>
          ${choices.map((s) => html`<option key=${s} value=${String(s)} selected=${s === seconds}>${s} s</option>`)}
        </select>`}
    <//>
    ${failed && !head ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="identify-result" />` : null}
  </div>`;
}

// The Identify answer the head's control does not show (a failure, or a click the gate
// stopped): under the picture, once.
function IdentifyResult({ bid }) {
  const p = panelState(bid, "identify");
  const failed = p.lines.length > 0 && !p.running && (p.lines[0].level === "err" || !!p.lines[0].notRun);
  return failed ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="identify-result" />` : null;
}

// --- the Front panel (round 3: the Overview's hero, the only one in the app) ----------------------

// PANEL-V017: a cell's role CODE is String.fromCharCode(97 + i) for ROLES[i], design/tokens.json's
// panel roles in order (net-protocol v0.17: a text ... i ok ... q banner-err ... u banner-held);
// ROLES, their glass colours and the status glyphs come from js/panel_codes.js, GENERATED from
// the tokens and the panel's glyph table with core/panel_codes.py (tools/gen_panel_codes.py):
// one vocabulary. A code the tokens do not define is drawn as text.
export function roleOf(code) {
  const i = code ? code.charCodeAt(0) - 97 : -1;
  return i >= 0 && i < ROLES.length ? ROLES[i] : "text";
}

// Runs of cells with one role; a status glyph (0x80-0x86) is a run of its own.
export function runs(row, roles) {
  const out = [];
  let cur = null;
  for (let i = 0; i < row.length; i += 1) {
    const role = roleOf(roles[i]);
    const glyph = GLYPHS[row.charCodeAt(i)] ? row.charCodeAt(i) : 0;
    if (glyph || !cur || cur.role !== role || cur.glyph) {
      cur = { role, glyph, text: "" };
      out.push(cur);
    }
    cur.text += row[i];
  }
  return out;
}

// A glyph as the panel draws it: its 8x16 bitmap (bit 7 the leftmost pixel), in the run's colour.
function glyphPath(rows) {
  let d = "";
  rows.forEach((bits, y) => {
    for (let x = 0; x < 8; x += 1) if (bits & (0x80 >> x)) d += `M${x} ${y}h1v1h-1z`;
  });
  return d;
}
const GLYPH_PATHS = Object.fromEntries(Object.entries(GLYPHS).map(([code, g]) => [code, glyphPath(g.rows)]));

// How a run looks. The today theme (and a rebuilt frame, and a harness that did not say): every
// role but the banners is plain text, the banners today's white on red (pm-inv). The aligned
// theme: the role's own colours, as the glass shows them.
function runProps(run, aligned) {
  if (aligned) {
    const [fg, bg] = ROLE_COLOURS[run.role] || ROLE_COLOURS.text;
    return { style: `color: ${fg}; background: ${bg}` };
  }
  return { cls: run.role.startsWith("banner-") ? "pm-inv" : undefined };
}

function Run({ run, aligned }) {
  const p = runProps(run, aligned);
  if (run.glyph) {
    const g = GLYPHS[run.glyph];
    return html`<span class=${`pm-glyph ${p.cls || ""}`} style=${p.style} data-role=${run.role}
      data-glyph=${g.name} role="img" aria-label=${g.name}><svg viewBox="0 0 8 16" preserveAspectRatio="none"
      aria-hidden="true"><path d=${GLYPH_PATHS[run.glyph]} /></svg></span>`;
  }
  return html`<span class=${p.cls} style=${p.style} data-role=${run.role}>${run.text}</span>`;
}

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
  const aligned = fr.theme === "aligned";
  // PANEL-TRUTH: a fact Harness Manager did not read is an em dash, and the legend says why.
  const unknown = rebuilt && rows.some((row) => String(row).includes(UNKNOWN_MARK));
  return html`<div class=${`panel-mirror ${rebuilt ? "rebuilt" : ""} ${aligned ? "aligned" : ""}`} data-testid="panel-mirror"
      data-source=${fr.source || ""} data-theme=${fr.theme || ""} role="group"
      style=${aligned ? `background: ${ROLE_COLOURS.text[1]}` : undefined}
      aria-label=${`The front panel's text, ${rows.length} rows${rebuilt ? `, ${REBUILT_TEXT}` : ""}`}>
    ${rows.map((row, r) => {
      const text = String(row).padEnd(40).slice(0, 40);
      return html`<div class="pm-row" key=${r} data-row=${r}>${runs(text, roles.slice(r * 40, r * 40 + 40))
        .map((run, i) => html`<${Run} key=${i} run=${run} aligned=${aligned} />`)}</div>`;
    })}
  </div>
  ${unknown ? html`<p class="muted small pm-legend" data-testid="panel-mirror-legend"><span class="mono">${UNKNOWN_MARK}</span> ${NOT_REPORTED}</p>` : null}`;
}

export const ROLE_WORDS = { holder: "holds the lease", owner: "has it open", watch: "watching" };

const TAP_WORDS = { request: "the lease request", identify: "Identify (found it)", nav: "next page" };

// The last tap on the glass, in one line (the newest first; the rest on hover).
function Taps({ f }) {
  if (!f.taps.length) return null;
  const t = f.taps[0];
  const all = f.taps.slice(0, TAPS_SHOWN).map((x) => `tapped ${TAP_WORDS[x.on] || x.on || "the glass"}${x.at ? ` at ${clock(x.at)}` : ""}`);
  return html`<span class="pl" data-testid="panel-taps" data-tap=${t.seq} data-on=${t.on} title=${all.join("\n")}>
    <span class="pl-sep"> · </span>last tap: ${TAP_WORDS[t.on] || t.on || "the glass"}${t.at ? ` ${clock(t.at).slice(0, 5)}` : ""}</span>`;
}

// PANEL-TRUTH: the card's one headline: what the card shows now, in plain words.
//   live      the Live display has the board's own picture;
//   read      the text was read from the panel (an image with 'panel');
//   rebuilt   Harness Manager rebuilt the text from what it read (an image without it);
//   none      nothing to show, and why.
export function headline(f, live) {
  const p = f.body && f.body.panel;
  if (live) return { state: "live", level: "ok", chip: "Live", text: "the board's own picture, as its panel shows it now" };
  if (!p) return null;
  if (p.source === "rebuilt") {
    return { state: "rebuilt", level: "unk", chip: "Rebuilt",
      text: `Rebuilt from what Harness Manager read, not read from the panel: ${thisImage(f.body.support)} does not send its panel's text.` };
  }
  return { state: "read", level: "", chip: "Read", text: "" };
}

// One line for everything the image does not report; the features behind a disclosure.
function NotReported({ f }) {
  const items = notReported(f);
  if (!items.length) return null;
  const support = f.body.support || {};
  const impl = implWords(support);
  return html`<details class="panel-missing mt-8" data-testid="panel-not-reported"
      data-missing=${items.map((i) => i.key).join(" ")}>
    <summary class="muted small" title=${`Not reported by this image: ${items.map((i) => i.what).join(", ")}`}><${Icon} name="chevron-right" cls="sm chev" />Not reported by this image: ${items.map((i) => i.what).join(", ")}</summary>
    <ul class="pm-list muted small" data-testid="panel-not-reported-details">
      ${items.map((i) => html`<li key=${i.key} data-missing=${i.key}>${i.what}: needs ${i.feature}</li>`)}
      <li key="impl" data-testid="panel-impl">${impl ? `the harness says it is the ${impl}` : "the harness did not say which harness it is"}${support.impl ? ` (version.impl "${support.impl}")` : ""}</li>
    </ul>
  </details>`;
}

// Live is the daemon's to give (LM4: harness feature 'lcd_mirror', the lease holder only): the
// card offers it until the daemon says this board can never show it (422 UNAVAILABLE: bare
// metal, no lcd_mirror; display.js liveRefusal), then Text only, with the daemon's own reason.
export function liveWhy(bid) {
  const r = liveRefusal(bid);
  return r ? `Live: ${r.reason || "this board has no live display"}` : "";
}

const VIEW_KEY = "harness_manager.panel.view";

function storedView() {
  try { return window.localStorage.getItem(VIEW_KEY) || ""; } catch (e) { return ""; }
}

// "live" (the board's own picture) or "text" (the panel's rows as text): the page's choice,
// Live by default where the board can give it.
export function panelView(bid) {
  if (liveWhy(bid)) return "text";
  const v = front(bid).view || storedView();
  return v === "text" ? "text" : "live";
}

function setView(bid, view) {
  front(bid).view = view;
  try { window.localStorage.setItem(VIEW_KEY, view); } catch (e) { /* this page only */ }
  changed();
}

// The note under the picture: what it is (PANEL-TRUTH's one headline), owner · page · touch,
// the last tap, and what this image does not report.
function PanelFoot({ bid, f, view }) {
  const p = f.body.panel;
  const h = headline(f, view === "live" && displayLive(bid));
  const now = Date.now() / 1000;
  const note = h.state === "live" ? "the board's own picture (lcd_mirror); view only"
    : h.state === "rebuilt" ? h.text
      : `Read from the panel${p.observed_at ? `, ${ageText(p.observed_at, now)}` : ""}: the rows as text you can select and copy.`;
  return html`<div class="ov-panel-foot">
    <div class="panel-headline" data-testid="panel-headline" data-state=${h.state}>
      <${Chip} level=${h.level} testid="panel-headline-chip">${h.chip}<//>
      <span class="small secondary ov-ell" data-testid=${h.state === "rebuilt" ? "panel-rebuilt" : h.state === "read" ? "panel-read-age" : "panel-live-note"}
        title=${note}>${note}</span></div>
    <div class="ov-panel-parts"><span class="panel-line small" data-testid="panel-line"><${Parts} parts=${lineParts(f)} /><${Taps} f=${f} /></span>
      <${NotReported} f=${f} /></div>
  </div>`;
}

export function FrontPanelCard({ bid }) {
  const f = front(bid);
  useEffect(() => {
    const g = front(bid);
    g.cards += 1;
    if (!g.body && !g.loading) loadPanel(bid);
    loadFrame(bid);
    const timer = setInterval(() => {
      const b = boardState(bid);
      if (document.visibilityState === "visible" && !b.job) { loadPanel(bid); loadFrame(bid); }
    }, POLL_MS);
    return () => { g.cards -= 1; clearInterval(timer); };
  }, [bid]);
  const why = liveWhy(bid);
  const view = panelView(bid);
  const idWhy = identifyWhy(bid);
  const tools = html`<div class="seg ov-seg" role="group" aria-label="Front panel view" data-testid="panel-view" data-view=${view}>
      <button type="button" aria-pressed=${view === "live" ? "true" : "false"} disabled=${!!why} data-action="panel-view-live"
        title=${why || "Live: the board's own picture (lcd_mirror); view only, the harness owns the panel"}
        onClick=${() => setView(bid, "live")}><${Icon} name="radio" />Live</button>
      <button type="button" aria-pressed=${view === "text" ? "true" : "false"} data-action="panel-view-text"
        title="Text: the panel's rows as text, from the harness; select and copy it"
        onClick=${() => setView(bid, "text")}><${Icon} name="list" />Text</button></div>
    <span class="spacer"></span>
    <${IdentifyControl} bid=${bid} testid="panel-identify" head=${true} />`;
  const p = f.body && f.body.panel;
  // What shows when there is no live picture: the panel's text, or why there is none. The Live
  // display keeps ONE place in the tree whatever the panel's state (a remount is a new socket).
  let fallback;
  if (f.unsupported || !f.body || !p) {
    const parts = lineParts(f);
    if (parts[0].key === "quiet") {
      // FIX-PACK-1: background reads are off (or paused): never a spinner that waits for ever.
      fallback = html`<${QuietNote} testid="panel-quiet" action="panel-read-now" busy=${f.loading && f.explicit}
        text=${quietWords(f.quiet)} onRead=${() => readPanelNow(bid)} />`;
    } else if (parts[0].key === "loading") {
      fallback = html`<${Reason} level="unk" testid="panel-unavailable" text="Reading the panel..." />`;
    } else {
      fallback = html`<div class="panel-headline" data-testid="panel-headline" data-state="none">
        <${Chip} level=${parts[0].level === "err" ? "err" : "unk"} testid="panel-headline-chip">Not available<//>
        <${Reason} level=${parts[0].level === "err" ? "err" : "unk"} icon=${parts[0].icon || ""}
          testid="panel-unavailable" text=${parts[0].text} /></div>`;
    }
  } else {
    fallback = html`<${Mirror} f=${f} />`;
  }
  const body = html`<div class="ov-panel-pic" data-view=${view}>${view === "live"
      ? html`<${LiveDisplay} bid=${bid} fit=${true}>${fallback}<//>` : fallback}</div>
    ${p && !f.unsupported ? html`<${PanelFoot} bid=${bid} f=${f} view=${view} />` : null}`;
  return html`<section class="card ov-card ov-panel" aria-label="Front panel" data-testid="panel-card"
      data-view=${view} data-owner=${p ? p.owner || "" : ""}>
    <div class="card-head"><h2 class="card-title"><${Icon} name="monitor" />Front panel</h2>${tools}</div>
    <div class="card-body">
      ${body}
      ${idWhy && f.body ? html`<${Reason} icon="circle-slash" testid="reason-identify" text=${idWhy} />` : null}
      <${IdentifyResult} bid=${bid} />
    </div>
  </section>`;
}
