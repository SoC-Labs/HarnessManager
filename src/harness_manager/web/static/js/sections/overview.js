// The Overview (UI v2 round 3, lane UI2-OVERVIEW; docs/planning/UI_V2_PLAN.md §1.2): one screen
// at 1440 x 900, no scroll. Needs attention (severity stripes, one fix each), the identity strip
// and the readings row, then Design · Lease (or Access) · Consoles and debug · Recent activity
// beside the Front panel, the largest element and the only front panel in the app
// (sections/panel.js FrontPanelCard).
//
// Every value is read, never made up: each names its source on hover, and one the board or the
// service does not give says "unavailable" / "not reported" and why. What the prototype faked
// (plan §1.8) is left out.
//
// Reads: GET /boards/{bid} (identity, health, answer_ms, uptime_s, stats: api-build G4), the
// session's mcc_route (api-hub G2), GET /telemetry (polled here), GET /readings/history (the
// temperature trend), GET /clocks (only when telemetry has no DUT clock), GET /lease (the week
// read; the background queue from this module's own read until week.js keeps it: CCR),
// GET /identity/clashes (G10), GET /boards/{bid}/checks (the Checks line), the consoles, debug
// and XVC state the Workbench's modules keep, and this page's Activity log.

import { call, routeMissing } from "../api.js";
import { existingSession } from "../consoles.js";
import { bytesText, clock, deployBar, healthOf, hexId, hostOf } from "../format.js";
import { html, useEffect, useState } from "../lib.js";
import { openReleaseConfirm } from "../lease.js";
import {
  boardState, cardJobBusy, changed, hasCardStore, loadCard, loadOverlays, loadTelemetry, navigate,
  onBoardEvent, openActivity, quietWords, refreshInfo, S,
} from "../store.js";
import { Chip, Icon, MiniBar, QuietNote, Reason, ResultBlock, Spinner } from "../ui.js";
import { panelState } from "../actions.js";
import {
  consoleRows, durationText, epochOf, leaseLeft, leaseWhere, leaseWho, loadClocks, onHubLoaded, week,
} from "../week.js";
import { checksOf } from "./checks.js";
import { front, FrontPanelCard, readPanelNow } from "./panel.js";
import { loadXvc, viewState, xvc } from "./xvc.js";
import { mccRoute, USB_WORDS } from "./boardfacts.js";   // UI2-POLISH: one Debug USB vocabulary

// --- small helpers ---------------------------------------------------------------------------------

const nowS = () => Date.now() / 1000;
const hhmm = (at) => (at ? clock(at).slice(0, 5) : "");
const num = (n) => Math.round(Number(n)).toLocaleString("en-GB");
const has = (v) => v !== null && v !== undefined && v !== "";

function upText(s) {
  if (!has(s) || !Number.isFinite(Number(s))) return "";
  const t = Math.max(0, Number(s));
  const d = Math.floor(t / 86400);
  const h = Math.floor(t / 3600) % 24;
  const m = Math.floor(t / 60) % 60;
  const mm = String(m).padStart(2, "0");
  return d ? `${d} d ${h} h ${mm} min` : h ? `${h} h ${mm} min` : `${m} min`;
}

// A want_s as people say it: "30 min", "1 h", "1 h 30 min".
function wantText(s) {
  const t = Number(s) || 0;
  if (t <= 0) return "";
  if (t < 3600) return `${Math.round(t / 60)} min`;
  const h = Math.floor(t / 3600);
  const m = Math.round((t % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

function ago(at) {
  if (!at) return "";
  const s = Math.max(0, nowS() - at);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

function sentence(text) {
  const s = String(text || "").trim();
  return !s || /[.!?)]$/.test(s) ? s : `${s}.`;
}

// Click the header's own Close board (the one dialog: restore, release, close; SHELL-2's).
function closeBoardDialog(bid) {
  const btn = document.querySelector('[data-action="close-board"]');
  if (btn) btn.click();
  else openReleaseConfirm(bid);
}

// The words for a person in the hub's queue: "carol" (and "@lab-pc-11" apart).
function whoParts(holder) {
  const [who, host] = String(holder || "").split("@");
  return { who: who || "someone", host: host || "" };
}

// --- the background queue: week.js keeps background_queue (CCR UI2-OV-1 = SHELL2-2) ---------------

// Read on a week read of the lease that came without one (a daemon without G11: GET /lease is
// cached 10 s by the service, so this is no extra hub contact); week.js's own copy wins.
async function loadBackground(bid) {
  const b = boardState(bid);
  if (b.ovBgLoading) return;
  b.ovBgLoading = true;
  try {
    const { data } = await call("lease", { bid });
    b.ovBg = { queue: Array.isArray(data.background_queue) ? data.background_queue : [],
      known: data.background_known !== false, reason: data.background_reason || "",
      present: "background_queue" in data };
  } catch (e) {
    b.ovBg = null;
  } finally {
    b.ovBgLoading = false;
    changed();
  }
}

onHubLoaded((bid, hub) => {
  if (!hub) return;
  const w = hub || {};
  if (Array.isArray(w.background_queue)) return;    // week.js keeps it (CCR SHELL2-2)
  if (S.boards[bid] && S.boards[bid].open) loadBackground(bid);
});

function backgroundOf(bid) {
  const hub = week(bid).hub;
  if (hub && Array.isArray(hub.background_queue)) {
    return { queue: hub.background_queue, known: hub.background_known !== false, reason: hub.background_reason || "" };
  }
  const b = boardState(bid);
  return b.ovBg && b.ovBg.present ? b.ovBg : null;
}

// The hub's queue as the Lease card lists it: people (the interactive tier, FIFO) and
// automation (the background tier, behind every person).
export function leaseQueue(bid) {
  const hub = week(bid).hub;
  const people = hub && Array.isArray(hub.queue) ? hub.queue.filter((x) => (x.tier || "interactive") !== "background") : [];
  const bg = backgroundOf(bid);
  return { people, bots: bg ? bg.queue : [], botsKnown: bg ? bg.known : null, botsReason: bg ? bg.reason : "" };
}

// Someone else waits for the board this Harness Manager holds: {first, more} or null.
export function waitingForYou(bid) {
  const who = leaseWho(bid);
  if (who.state !== "here") return null;
  const q = leaseQueue(bid).people.filter((x) => !x.mine);
  return q.length ? { first: q[0], more: q.length - 1 } : null;
}

function waitWords(w) {
  const name = whoParts(w.first.holder).who;
  const want = wantText(w.first.want_s);
  return `${name}${w.more ? ` and ${w.more} more` : ""} ${w.more ? "wait" : "waits"} for this board${want ? ` (${want})` : ""}`;
}

// --- G10: clashes across every board this Harness Manager has seen --------------------------------

const clashes = { list: null, at: 0, loading: false, unsupported: false };

async function loadClashes() {
  if (clashes.loading || clashes.unsupported) return;
  clashes.loading = true;
  try {
    const { data } = await call("identityClashes");
    clashes.list = Array.isArray(data.clashes) ? data.clashes : [];
    clashes.at = Date.now();
  } catch (e) {
    if (routeMissing(e)) clashes.unsupported = true;      // G10 not in this daemon: per board only
  } finally {
    clashes.loading = false;
    changed();
  }
}

onBoardEvent((ev) => {
  if (ev.topic === "board.net_identity" || ev.topic === "board.identity") {
    if (clashes.list && Date.now() - clashes.at > 5000) loadClashes();
  }
});

// This board's clashes: [{field, value, others: [{board_id, name, kind, target}]}].
function clashesOf(bid) {
  if (!clashes.list) return [];
  return clashes.list.map((c) => {
    const boards = c.boards || [];
    if (!boards.some((x) => x.board_id === bid)) return null;
    return { field: c.field, value: c.value, others: boards.filter((x) => x.board_id !== bid) };
  }).filter(Boolean);
}

function netIdentity(b) {
  return (b && b.netIdentity) || (b && b.info && b.info.net_identity) || null;
}

// --- needs attention ---------------------------------------------------------------------------------

const HARNESS_TITLES = {
  wedged: "The harness is wedged", offline: "The harness is offline",
  rescue: "The board is in stage0 rescue", unknown: "The harness state is unknown",
};

// One row per problem, each with its one fix; nothing when all is well. The header's Overview
// badge counts them (app.js). Round 3: the SD install is the banner's (Board › Versions), the
// build check the header's Design chip, the lease state the header's lease control; what
// stays here is what this board needs from you now.
export function attentionItems(bid) {
  const b = boardState(bid);
  const out = [];
  const info = b.info;
  const failed = b.infoError && b.infoError.errName !== "ABSENT";
  const go = (route, label) => ({ label, run: () => navigate(bid, route) });
  // an identity clash: the board's own check (BOARD-ID), else the one across boards (G10)
  const ni = netIdentity(b);
  const cl = clashesOf(bid);
  if ((ni && ni.status === "clash") || cl.length) {
    const c = cl[0];
    const other = c && c.others[0];
    const otherName = other ? (other.name || other.board_id || other.target) : "";
    const field = c ? c.field.toUpperCase() : "";
    const findings = ni && (ni.findings || []).filter((f) => f.kind === "clash").map((f) => f.text);
    // The fix belongs on the board that still carries the image default: this one, unless its
    // own identity is set (net-protocol v0.16) and the other board is one this page lists.
    const src = (ni && ni.reported && typeof ni.reported.source === "object" && ni.reported.source) || {};
    const clashField = c ? c.field : "mac";
    const ownSet = !!ni && !!src[clashField] && src[clashField] !== "default" && ni.status === "clash";
    const otherBid = other && other.board_id && S.boards[other.board_id] ? other.board_id : "";
    out.push({ key: "identity", level: "err",
      title: `Identity clash${otherName ? ` with ${otherName}` : ""}.`,
      text: findings && findings.length ? ` ${sentence(findings.join("; "))}`
        : ` This board reports the same ${field} as ${otherName} (${c.value}).`,
      more: ownSet && otherBid ? `${otherName}'s identity was never set: the fix belongs there.`
        : "Two boards with one MAC or IP break the hub's DHCP and the front panel.",
      fix: ownSet && otherBid ? { label: `Go to ${otherName}`, run: () => navigate(otherBid, "overview") }
        : go("board/access", "Fix identity…") });
  }
  if (failed) {
    out.push({ key: "read", level: "warn", title: "Last read failed:",
      text: ` ${sentence(b.infoError.message)}${info && b.infoOkAt ? ` Values below are from ${hhmm(b.infoOkAt)}.` : ""}`,
      more: b.infoError.hint ? sentence(b.infoError.hint) : "",
      fix: { label: "Read again", run: () => refreshInfo(bid) } });
  }
  const h = info && info.health;
  if (h && !failed) {
    const cc = h.control_channel || "unknown";
    if (!h.reachable) {
      out.push({ key: "harness", level: "err", title: "The harness does not answer.",
        text: ` ${sentence((h.notes || [])[0] || "Check the board's power, the Ethernet cable and its address")}`,
        fix: go("board/recover", "Recover…") });
    } else if (cc !== "idle" && cc !== "busy") {
      out.push({ key: "harness", level: cc === "rescue" ? "warn" : "err",
        title: `${HARNESS_TITLES[cc] || `The harness is ${cc}`}.`,
        text: ` ${sentence((h.notes || []).join(" ") || "It does not report idle")}`,
        fix: go("board/recover", "Recover…") });
    }
  }
  const claim = info && info.claim;
  const who = leaseWho(bid);
  const drive = who.state === "none" || who.state === "here";
  if (claim && claim.state === "unclaimed" && drive) {
    out.push({ key: "claim", level: "warn", title: "SSH not claimed.",
      text: " Anyone with the image's default key can log in to this board's Linux. Claim it with your key.",
      fix: go("board/access", "Claim on Board") });
  }
  const hub = week(bid).hub;
  if (hub && hub.tunnel && hub.tunnel.state && hub.tunnel.state !== "up") {
    const t = hub.tunnel;
    out.push({ key: "tunnel", level: t.state === "starting" ? "warn" : "err",
      title: `The SSH tunnel to ${t.host || hub.host} is ${t.state}.`,
      text: ` ${sentence(t.detail)} Fix: check that \`ssh ${t.host || hub.host}\` works from this machine (key, VPN).`.replace(/\s+/g, " "),
      fix: go("board/connections", "Connections") });
  }
  if (who.state === "unknown") {
    out.push({ key: "lease", level: "unk", title: `The lease on ${who.host || "the hub"} could not be read.`,
      text: ` ${sentence(who.error)} It may be held: not known is not free. The page reads it again every 30 s.` });
  }
  const w = waitingForYou(bid);
  if (w) {
    const first = w.first;
    const since = epochOf(first.since);
    out.push({ key: "waiting", level: "warn", title: `${waitWords(w)}:`,
      text: ` release when you're done. The hub hands it to ${whoParts(first.holder).who} at once.`,
      more: `#${first.position} in the hub's queue${since ? ` since ${hhmm(since)}` : ""}${first.message ? `: “${first.message}”` : ""}. Close board… restores the baseline and releases.`,
      fix: { label: "Close board…", run: () => closeBoardDialog(bid) } });
  }
  if (who.state === "here") {
    const left = leaseLeft(who.lease);
    if (left !== null && left <= 600) {
      out.push({ key: "lease", level: "warn", title: `Your lease ends in ${durationText(left)}.`,
        text: ` At ${hhmm(epochOf(who.lease.expires_at))} Program, Reset DUT and debug stop working here. The service renews it while the board is open; if this stays, the hub is not answering.`,
        fix: { label: "Close board…", run: () => closeBoardDialog(bid) } });
    }
  }
  const rank = { err: 0, warn: 1, unk: 2 };
  return out.sort((x, y) => (rank[x.level] ?? 3) - (rank[y.level] ?? 3));
}

const ATT_ICONS = { err: "circle-x", unk: "circle-help", warn: "triangle-alert" };

function Attention({ bid }) {
  const items = attentionItems(bid);
  if (!items.length) return null;
  const worst = items.some((a) => a.level === "err") ? "err" : "warn";
  return html`<section class=${`ov-att ${worst}`} aria-label="Needs attention" data-testid="attention">
    <div class="ov-att-head"><${Icon} name="triangle-alert" />Needs attention<span class="ov-att-n">${items.length}</span></div>
    <ul>${items.map((a) => html`<li key=${a.key} class=${`ov-att-row ${a.level}`} data-attention=${a.key}
        title=${`${a.title}${a.text}${a.more ? ` ${a.more}` : ""}`}>
      <${Icon} name=${ATT_ICONS[a.level] || "triangle-alert"} />
      <span class="ov-att-text"><strong>${a.title}</strong>${a.text}${a.more ? html`<span class="ov-att-more"> ${a.more}</span>` : null}</span>
      ${a.fix ? html`<button type="button" class="btn sm" data-action=${`attention-${a.key}`}
        onClick=${a.fix.run}>${a.fix.label}</button>` : null}
    </li>`)}</ul>
  </section>`;
}

// --- the identity strip -------------------------------------------------------------------------------

// UI2-POLISH: the words are boardfacts.js DEBUG_USB (the header and the Board tab say the same).
const ROUTE_LEVEL = { hub: "ok", pc: "ok", self: "accent", none: "unk", unknown: "unk" };
const MCC_ROUTE = Object.fromEntries(Object.entries(USB_WORDS).map(([to, w]) => [to,
  { level: ROUTE_LEVEL[to], icon: w.icon, text: w.chip }]));

// G2: the Debug USB route is boardfacts.js mccRoute (the service's, else the links), the one the
// header and the Board tab read too.

function harnessWords(b) {
  const id = (b.info && b.info.identity) || {};
  const impl = id.harness_impl === "linux" ? "Linux" : id.harness_impl === "bare-metal" ? "bare-metal" : "";
  const rel = b.harness && b.harness.catalog && b.harness.catalog.board && b.harness.catalog.board.running_release;
  return { impl, version: id.harness_version || "", release: rel || "", proto: id.proto || "",
    sha: id.firmware_sha || "" };
}

function IdentityStrip({ bid }) {
  const b = boardState(bid);
  const info = b.info || {};
  const ni = netIdentity(b);
  const rep = (ni && ni.reported) || {};
  const cl = clashesOf(bid);
  const macClash = cl.find((c) => c.field === "mac");
  const ipClash = cl.find((c) => c.field === "ip");
  const src = rep.source && typeof rep.source === "object" ? rep.source : {};
  const label = rep.label || "";
  // V7-ALIGN: the image default label (source "default"; with no source, the shipped "MPS3")
  const labelDefault = !!label && (src.label ? src.label === "default" : label.toUpperCase() === "MPS3");
  const ip = rep.ip || hostOf(bid).replace(/:\d+$/, "");
  const mac = rep.mac || (macClash ? macClash.value : "");
  const macBad = !!macClash || (ni && ni.status === "clash" && (ni.findings || []).some((f) => f.field === "mac"));
  const hw = harnessWords(b);
  const at = Number(info.readings_at) || 0;
  const since = at ? Math.max(0, nowS() - at) : 0;
  const up = has(info.uptime_s) ? upText(Number(info.uptime_s) + since) : "";
  const osUp = has(info.os_uptime_s) ? upText(Number(info.os_uptime_s) + since) : "";
  const r = mccRoute(bid);
  const U = MCC_ROUTE[r.to] || MCC_ROUTE.unknown;
  const srcWords = Object.entries(src).map(([k, v]) => `${k} from ${v}`).join(", ");
  const idSrc = ni ? `Source: the board's identity${srcWords ? ` (${srcWords})` : ""}${rep.via ? `, via ${rep.via}` : ""}` : "";
  return html`<div class="ov-id" data-testid="ov-identity">
    <span class="ov-id-i" title=${ni ? `${idSrc}. The label is the front panel's row 0.` : "Source: this harness image reports no label (net-protocol v0.16 identity)"}>
      <span class="ov-id-k">Label</span>
      ${label ? html`<span class="board-label" data-testid="ov-label">${label}</span>${labelDefault ? html`<span class="ov-id-note">image default</span>` : null}`
        : html`<span class="muted" data-testid="ov-label">not reported</span>`}</span>
    <span class="ov-id-i" title=${rep.ip ? idSrc : "Source: the board's address (its board id); the harness reports no IP of its own here"}>
      <span class="ov-id-k">IP</span><span class=${`mono ${ipClash ? "ov-id-bad" : ""}`}>${ip}</span>
      <span class="ov-id-k">MAC</span>${mac ? html`<span class=${`mono ${macBad ? "ov-id-bad" : ""}`} data-testid="ov-mac"
          title=${macClash && !rep.mac ? "Source: what this board last reported (seen by this Harness Manager)" : idSrc}>${macBad ? html`<${Icon} name="triangle-alert" cls="sm" />` : null}${mac}</span>`
        : html`<span class="muted" data-testid="ov-mac">not reported</span>`}</span>
    <span class="ov-id-i ov-shrink" title=${`Source: the version reply${hw.sha ? ` · firmware ${hw.sha}` : ""}${hw.proto ? ` · net-protocol ${hw.proto}` : ""}${hw.release ? ` · catalogue release ${hw.release}` : ""}`}>
      <span class="ov-id-k">Harness</span>
      <span class="ov-ell" data-testid="ov-harness">${hw.impl ? `${hw.impl} ` : ""}${hw.release || hw.version || "unknown"}${hw.proto ? html` <span class="secondary">· protocol ${hw.proto}</span>` : null}</span></span>
    <span class="ov-id-i" data-testid="ov-uptime" title=${up ? `Source: ${info.readings_source || "the harness"} at ${hhmm(at)} (the harness has run this long${osUp ? `; the OS: ${osUp}` : ""}), plus the time since`
        : "The harness's uptime is read with its stats; this read had none"}>
      <span class="ov-id-k">Up</span>${up ? html`<span class="num">${up}</span>` : html`<span class="muted">not reported</span>`}</span>
    <span class="ov-id-i"><span class="ov-id-k">Debug USB</span>
      <button type="button" class=${`chip ov-chip-sm ${U.level}`} data-testid="ov-usb" data-usb=${r.to}
        title=${`${r.reason || U.text} · Board › Connections`} onClick=${() => navigate(bid, "board/connections")}>
        <${Icon} name=${U.icon} />${U.text}</button></span>
    <span class="grow"></span>
    <button type="button" class="ov-go" data-action="ov-go-access" title="Identity and the SSH claim on Board › Access"
      onClick=${() => navigate(bid, "board/access")}>Board<${Icon} name="chevron-right" /></button>
  </div>`;
}

// --- the readings row ------------------------------------------------------------------------------------

function Kpi({ label, lvl = "ok", src, value, unit = "", unk = false, sub = [], extra = null, stale = "", testid }) {
  const dot = lvl === "unk" ? "ov-dot-unk" : lvl === "plain" ? "" : lvl;
  const tip = `${label}: ${sub.filter(Boolean).join(" · ")}\nSource: ${src}${stale ? `\nStale: ${stale}` : ""}`;
  return html`<div class=${`ov-kpi ${lvl}${stale ? " stale" : ""}`} title=${tip} data-testid=${testid} data-level=${lvl}>
    <div class="ov-kpi-l"><span class=${`dot ${dot}`}></span>${label}</div>
    <div class="ov-kpi-v">${unk ? html`<span class="ov-unav">${value}</span>`
      : html`<span>${value}${unit ? html`<small>${unit}</small>` : null}</span>`}${extra}</div>
    <div class="ov-kpi-s"><span>${sub[0] || ""}</span></div></div>`;
}

function Spark({ points, hover, onHover }) {
  const W = 64;
  const H = 22;
  const pad = 3;
  const vals = points.map((p) => p[1]);
  const last = vals.length - 1;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = Math.max(0.6, hi - lo);
  const x = (i) => pad + (i * (W - 2 * pad)) / Math.max(1, last);
  const y = (v) => H - pad - ((v - lo) / span) * (H - 2 * pad);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const at = hover === null ? last : hover;
  const move = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const i = Math.max(0, Math.min(last, Math.round(((e.clientX - rect.left) / rect.width) * last)));
    if (i !== hover) onHover(i);
  };
  return html`<svg class="ov-spark" viewBox=${`0 0 ${W} ${H}`} role="img" data-testid="ov-spark" data-points=${vals.length}
    aria-label=${`Temperature, last ${Math.round((points[last][0] - points[0][0]) / 60)} min: ${lo.toFixed(1)} to ${hi.toFixed(1)} °C`}
    onMouseMove=${move} onMouseLeave=${() => onHover(null)}>
    <path class="ln" d=${d} />
    ${hover !== null ? html`<line class="xh" x1=${x(hover)} x2=${x(hover)} y1="0" y2=${H} />` : null}
    <circle class="pt" cx=${x(at)} cy=${y(vals[at])} r=${hover !== null ? 3 : 2.4} /></svg>`;
}

function pickTemperature(readings) {
  const temps = (readings || []).filter((r) => r.unit === "degC");
  return temps.find((r) => has(r.value)) || temps[0] || null;
}

// GET /readings/history for the temperature on screen: its last 30 minutes (G4).
async function loadHistory(bid, name) {
  const b = boardState(bid);
  if (b.ovHistLoading) return;
  b.ovHistLoading = true;
  try {
    const since = Math.floor(nowS() - 1800);
    const { data } = await call("readingsHistory", { bid }, undefined, { name, since: String(since) });
    const s = (data.series || []).find((x) => x.name === name);
    b.ovHist = { name, points: s ? (s.points || []).filter((p) => has(p[1])) : [], source: s ? s.source : "", at: Date.now() };
  } catch (e) {
    b.ovHist = { name, points: [], error: e, at: Date.now() };
  } finally {
    b.ovHistLoading = false;
    changed();
  }
}

function clockReading(bid) {
  const b = boardState(bid);
  const t = (b.telemetry || []).find((r) => r.name === "dut_clk");
  if (t) return { r: t, src: `${t.source || "telemetry"}${t.reason ? ` (${t.reason})` : ""}` };
  const w = week(bid);
  const c = (w.clocks || []).find((r) => /dut/.test(r.name || "")) || (w.clocks || [])[0];
  if (c) return { r: c, src: `GET /clocks: ${c.source || "clock adapter"}` };
  if (w.clocksError) {
    return { r: null, why: w.clocksError.reason || w.clocksError.message, src: "GET /clocks" };
  }
  return { r: null, why: "", src: "GET /clocks" };
}

function counterOf(info, ...names) {
  const c = (info.health && info.health.counters) || {};
  const s = info.stats || {};
  for (const n of names) {
    if (has(s[n])) return Number(s[n]);
    if (has(c[n])) return Number(c[n]);
  }
  return null;
}

function Readings({ bid }) {
  const b = boardState(bid);
  const info = b.info || {};
  const [hover, setHover] = useState(null);
  const failed = b.infoError && b.infoError.errName !== "ABSENT";
  const stale = failed ? `the last read failed; these are from ${hhmm(b.infoOkAt)}` : "";
  const bg = b.background || {};
  const readAge = b.infoLoading ? "reading now"
    : bg.kind === "lease" ? `paused: ${bg.holder || "someone else"} holds it`
      : bg.kind === "off" ? `read ${ago(b.infoOkAt)} · background reads off`
        : b.infoOkAt ? `read ${ago(b.infoOkAt)}` : "not read yet";
  const cells = [];
  // 1. Health
  const hl = healthOf(info.health ? info : null);
  const cc = (info.health && info.health.control_channel) || "unknown";
  if (b.infoLoading && !b.info) {
    cells.push(html`<${Kpi} label="Health" lvl="accent" value="Reading…" sub=${["reading now"]} src="the harness's health (control channel 6900)" testid="ov-kpi-health" />`);
  } else if (failed) {
    cells.push(html`<${Kpi} label="Health" lvl="warn" value="Stale" testid="ov-kpi-health"
      sub=${[`failed: ${b.infoError.errName}`, `last good read ${hhmm(b.infoOkAt)}`]} src=${`the harness's health · ${b.infoError.message}`} />`);
  } else {
    cells.push(html`<${Kpi} label="Health" lvl=${hl.level} value=${hl.text} testid="ov-kpi-health"
      sub=${[readAge, `control channel ${cc}`]} src="the harness's health (control channel 6900: reachable, state, counters)" />`);
  }
  // 2. Temperature (+ its trend, G4)
  const t = pickTemperature(b.telemetry);
  if (!b.telemetry) {
    const q = b.telemetryQuiet;
    cells.push(html`<${Kpi} label="Temperature" lvl="unk" unk value=${q ? "not read" : "reading…"} testid="ov-kpi-temp"
      sub=${[q ? quietWords(q) : "reading the board's telemetry"]} src="GET /telemetry" />`);
  } else if (!t || !has(t.value)) {
    cells.push(html`<${Kpi} label="Temperature" lvl="unk" unk value="unavailable" testid="ov-kpi-temp"
      sub=${[t ? t.reason || "not reported" : "no temperature sensor reported"]} src=${t ? `${t.source || "telemetry"}: ${t.reason || "no value"}` : "GET /telemetry: no degC reading"} />`);
  } else {
    const v = Number(t.value);
    const lvl = v >= 60 ? "err" : v >= 50 ? "warn" : "ok";
    const hist = b.ovHist && b.ovHist.name === t.name ? b.ovHist.points : [];
    const pts = hist.length >= 2 ? hist : [];
    let sub;
    if (pts.length && hover !== null && pts[hover]) sub = [`${hhmm(pts[hover][0])} · ${Number(pts[hover][1]).toFixed(1)} °C`];
    else if (pts.length) {
      const vals = pts.map((p) => Number(p[1]));
      const mins = Math.max(1, Math.round((pts[pts.length - 1][0] - pts[0][0]) / 60));
      sub = [`${mins} min: ${Math.min(...vals).toFixed(1)} to ${Math.max(...vals).toFixed(1)} °C`];
    } else sub = [`${t.name} · no trend yet`];
    cells.push(html`<${Kpi} label="Temperature" lvl=${lvl} value=${v.toFixed(1)} unit="°C" sub=${sub} stale=${stale} testid="ov-kpi-temp"
      extra=${pts.length ? html`<${Spark} points=${pts} hover=${hover} onHover=${setHover} />` : null}
      src=${`${t.name} from ${t.source || "telemetry"}${t.observed_at ? ` at ${hhmm(t.observed_at)}` : ""}; the trend is this service's history (GET /readings/history). Warn at 50 °C, act at 60 °C.`} />`);
  }
  // 3. DUT clock
  const ck = clockReading(bid);
  if (ck.r && has(ck.r.value)) {
    cells.push(html`<${Kpi} label="DUT clock" value=${Number(ck.r.value)} unit=${ck.r.unit || "MHz"} testid="ov-kpi-clock"
      sub=${[`${ck.r.reason || "reported"} · ${ck.r.source || ""}`]} stale=${stale} src=${ck.src} />`);
  } else {
    const why = (ck.r && ck.r.reason) || ck.why || "no clock adapter reports it";
    cells.push(html`<${Kpi} label="DUT clock" lvl="unk" unk value="unavailable" sub=${[why]} src=${ck.src} testid="ov-kpi-clock" />`);
  }
  // 4. Harness loop (bare metal's superloop; Linux has none)
  const maxUs = counterOf(info, "svc_max_us");
  const hw = harnessWords(b);
  const answer = has(info.answer_ms) ? `answers in ${Number(info.answer_ms) < 1000 ? `${info.answer_ms} ms` : `${(info.answer_ms / 1000).toFixed(1)} s`}` : "";
  if (maxUs !== null) {
    const ix = counterOf(info, "svc_max_ix");
    const over = counterOf(info, "svc_overruns");
    const skips = counterOf(info, "svc_skips");
    const bad = maxUs > 2e6;
    cells.push(html`<${Kpi} label="Harness loop" lvl=${bad ? "warn" : "ok"} value=${(maxUs / 1e6).toFixed(2)} unit="s worst" stale=${stale} testid="ov-kpi-loop"
      sub=${[bad ? `service ${ix ?? "?"}: over the 2 s read timeout` : `${over ?? "?"} overruns · ${skips ?? "?"} skips`]}
      src=${`the harness's diag: svc_max_us (the worst single service), svc_max_ix = ${ix ?? "?"}, svc_overruns ${over ?? "?"}, svc_skips ${skips ?? "?"}. Above 2 s the control port misses Harness Manager's 2 s read timeout.${answer ? ` Harness Manager's last read: ${answer}.` : ""}`} />`);
  } else {
    cells.push(html`<${Kpi} label="Harness loop" lvl="unk" unk value="not reported" testid="ov-kpi-loop"
      sub=${[hw.impl === "Linux" ? `harnessd has no superloop${answer ? ` · ${answer}` : ""}` : answer || "no svc_* counters from this harness"]}
      src=${hw.impl === "Linux" ? "The Linux harness (harnessd) is event-driven, so it sends no svc_* counters. The answer time is Harness Manager's own read (answer_ms)."
        : `This harness sends no svc_* counters.${answer ? ` The answer time is Harness Manager's own read (answer_ms).` : ""}`} />`);
  }
  // 5. Network
  const s = info.stats || {};
  const rx = counterOf(info, "rx_frames");
  const tx = counterOf(info, "tx_frames", "tx_frames_sent");
  const drops = counterOf(info, "rxdrop", "rx_drops");
  const errs = counterOf(info, "txerr", "tx_errors");
  const crc = counterOf(info, "crc_errors");
  const bits = [];
  if (rx !== null || tx !== null) bits.push(`rx ${rx !== null ? num(rx) : "?"} · tx ${tx !== null ? num(tx) : "?"}`);
  const badBits = [drops !== null ? `drops ${num(drops)}` : "", errs !== null ? `tx errors ${num(errs)}` : "", crc !== null ? `CRC errors ${num(crc)}` : ""].filter(Boolean).join(" · ");
  const netBad = (drops || 0) + (errs || 0) + (crc || 0) > 0;
  if (has(s.link)) {
    const spd = has(s.spd) ? `${s.spd}/${s.fdx === false ? "HD" : "FD"}` : "link";
    cells.push(html`<${Kpi} label="Network" lvl=${!s.link ? "err" : netBad ? "warn" : "ok"} value=${s.link ? spd : "down"} unit=${s.link ? "up" : ""} stale=${stale} testid="ov-kpi-net"
      sub=${netBad ? [badBits, bits[0]] : [bits[0] || badBits || "no frame counters"]}
      src=${`the harness's stats (link, spd, fdx${drops !== null ? ", rxdrop" : ""}${errs !== null ? ", txerr" : ""}) and its counters, ${hw.impl === "Linux" ? "since the last boot" : "since the shell started"}`} />`);
  } else if (bits.length || badBits) {
    cells.push(html`<${Kpi} label="Network" lvl=${netBad ? "warn" : "plain"} value=${rx !== null ? num(rx) : "?"} unit="rx" stale=${stale} testid="ov-kpi-net"
      sub=${[badBits || bits[0]]} src="the harness's counters (it sends no link state)" />`);
  } else {
    cells.push(html`<${Kpi} label="Network" lvl="unk" unk value="not reported" sub=${["no link state or frame counters"]}
      src="the harness sent no stats (link, spd) and no frame counters" testid="ov-kpi-net" />`);
  }
  // 6. Partition swaps
  const swaps = counterOf(info, "swap_n", "swaps");
  const icap = counterOf(info, "icap", "icap_bytes");
  const dep = b.deploy;
  const running = dep.state === "running";
  if (swaps !== null || icap !== null || running) {
    const last = dep.state === "done" && dep.doneAt ? `last: ${Math.round(dep.seconds || 0)} s at ${hhmm(dep.doneAt)}` : "";
    cells.push(html`<${Kpi} label="Partition swaps" lvl=${running ? "accent" : "plain"} value=${swaps !== null ? swaps : "?"} unit=${running ? "+1 running" : ""} stale=${stale} testid="ov-kpi-swaps"
      sub=${[running ? `swapping to ${dep.overlay || "a design"}…` : last || (icap !== null ? `ICAP ${bytesText(icap)} written` : "since the shell started")]}
      src=${`the harness's stats: swaps${icap !== null ? ` and ICAP bytes (${bytesText(icap)})` : ""} since the shell started; the last swap from this page's Activity`} />`);
  } else {
    cells.push(html`<${Kpi} label="Partition swaps" lvl="unk" unk value="not reported" sub=${["no swap counter from this harness"]}
      src="the harness sent no stats.swap_n and no swaps counter" testid="ov-kpi-swaps" />`);
  }
  return html`<div class="ov-kpis" data-testid="ov-readings">${cells}</div>`;
}

function Summary({ bid }) {
  return html`<section class="card ov-sum" aria-label="This board now: identity and readings" data-testid="ov-summary">
    <${IdentityStrip} bid=${bid} />
    <${Readings} bid=${bid} />
  </section>`;
}

// --- the cards -------------------------------------------------------------------------------------------

function Go({ label, title, run, action }) {
  return html`<button type="button" class="ov-go" data-action=${action} title=${title || `Go to ${label}`}
    onClick=${run}>${label}<${Icon} name="chevron-right" /></button>`;
}

function OvCard({ title, icon, go = null, cls = "", tools = null, testid, children }) {
  return html`<section class=${`card ov-card ${cls}`} aria-label=${title} data-testid=${testid}>
    <div class="card-head"><h2 class="card-title"><${Icon} name=${icon} />${title}</h2>${tools}
      <span class="spacer"></span>${go}</div>
    <div class="card-body">${children}</div></section>`;
}

// LINUX-SLOTS: the user microSD in one line (present / store default / OS slots), from GET
// /boards/{bid}/card (L1-CARD). A harness with no card store ("usd") says so; nothing is read.
export function cardLine(b) {
  const c = b.card;
  if (!hasCardStore(b)) return { text: "no card store on this harness", muted: true, none: true };
  if (!c) {
    if (b.cardError) return { text: "unavailable", muted: true };
    if (b.cardQuiet) return { text: "not read", muted: true, title: quietWords(b.cardQuiet) };
    return { text: "reading...", muted: true };
  }
  if (!c.store) return { text: c.reason || "no card store", muted: true, none: true };
  if (!c.present) return { text: "none (boots as always)", muted: true, none: true };
  const bits = [c.state || "?"];
  if (c.default) bits.push(`default ${c.default.rm_name || c.default.rm_id || "?"} [${c.default.slot || "?"}]`);
  const os = c.os_slots;
  // SLOT-TIMING: a card job that writes or reads back is the line: nothing may reset the board.
  if (cardJobBusy(c) && b.cardText) {
    return { text: b.cardText, muted: false, busy: true,
      title: "a reset now can wedge the card: reboot, power cycle, deploy and restore wait for it" };
  }
  if (os && os.slots) {
    bits.push("OS " + Object.keys(os.slots).sort()
      .map((n) => `${n}:${os.slots[n].state}${n === os.running ? "*" : ""}`).join(" "));
  }
  return { text: bits.join(" · "), muted: false, title: (c.notes || []).join("\n") };
}

// "Boots next": what a power-on loads, from the user microSD's store (the card line above).
function CardRow({ b }) {
  const l = cardLine(b);
  const c = b.card;
  let name = "";
  let sub = "";
  if (l.busy) { name = ""; sub = l.text; }
  else if (c && c.store && c.present && c.default) { name = c.default.rm_name || c.default.rm_id || "?"; sub = `kept on the card [${c.default.slot || "?"}]`; }
  else if (c && c.store && c.present) { name = "greybox"; sub = "nothing kept on the card"; }
  else if (l.none) { name = "the baseline"; sub = l.text; }
  else { sub = l.text; }
  return html`<dt>Boots next</dt><dd data-testid="tile-card" title=${`${l.text}${l.title ? `\n${l.title}` : ""}`}
      data-card=${l.busy ? "busy" : l.none ? "none" : c ? "read" : "reading"}>
    ${name ? html`<span class="mono">${name}</span>` : null}<span class="sub ov-ell">${sub}</span></dd>`;
}

function OsSlots({ bid }) {
  const b = boardState(bid);
  const os = b.card && b.card.os_slots;
  if (!os || !os.slots) return null;
  return html`<dt>OS slots</dt><dd data-testid="ov-os-slots">${Object.keys(os.slots).sort().map((n) => {
    const s = os.slots[n];
    const run = n === os.running;
    return html`<button type="button" key=${n} class=${`chip ov-chip-sm ${run ? "ok" : s.state === "valid" ? "" : "warn"}`}
      data-slot=${n} title=${`Slot ${n}: ${s.state}${s.version ? ` · ${s.version}` : ""}${s.verified ? ` · verified by ${s.verified}` : ""} · Board › Versions`}
      onClick=${() => navigate(bid, "board/versions")}>${run ? html`<${Icon} name="circle-check" />` : null}${n} ${s.version || s.state}${run ? " · booted" : n === os.default ? " · default" : s.state === "valid" ? " · fallback" : ""}</button>`;
  })}</dd>`;
}

function overlayRef(b, name, rmId) {
  const o = b.overlays;
  const all = (o && (o.all || o.loadable)) || [];
  return all.find((x) => rmId && String(x.rm_id).toLowerCase() === String(rmId).toLowerCase())
    || all.find((x) => x.name === name) || null;
}

function DesignCard({ bid }) {
  const b = boardState(bid);
  useEffect(() => { if (!b.overlays && !b.overlaysLoading) loadOverlays(bid); }, [bid]);
  const id = (b.info && b.info.identity) || {};
  const dep = b.deploy;
  const bar = deployBar(dep);
  const ref = overlayRef(b, id.rm_name, id.rm_id);
  const baseline = id.rm_id && Number(id.rm_id) === 0;
  const o = b.overlays;
  const loadable = o ? o.loadable.length : null;
  const kits = o ? o.loadable.filter((x) => x.receipt_sha256).length : 0;
  const verified = dep.state === "done" && dep.verified && dep.rm_id && String(dep.rm_id).toLowerCase() === String(id.rm_id || "").toLowerCase();
  let top;
  if (bar) {
    top = html`<div class="ov-prog" data-testid="ov-design-progress" title=${`Programming ${bar.overlay}: ${bar.line}`}>
      <p class="reason"><${Spinner} /><span>Programming ${bar.overlay} · ${bar.line}</span></p><${MiniBar} bar=${bar} /></div>`;
  } else {
    top = html`<div class="ov-design" title=${`Source: the harness's identity (rm_id read back after the swap)${ref ? ` · ${ref.name}: ${bytesText(ref.size_bytes)} partial, ${ref.ip_class || "?"}` : ""}`}>
      <span class="ov-name" data-testid="tile-design-name">${id.rm_name || "unknown design"}</span>
      <span class="mono muted">${hexId(id.rm_id) || "rm_id not reported"}</span>
      <span class="tags">${ref && ref.ltx_sha256 ? html`<span class="tag ila" title="its build kept the ILA probes file (.ltx)">ILAs</span>` : null}
        ${ref && ref.receipt_sha256 ? html`<span class="tag kit" title="built with Harness Manager's kit (a build receipt)">kit-built</span>` : null}
        ${baseline ? html`<span class="tag">baseline</span>` : null}</span></div>`;
  }
  const loaded = verified
    ? html`<${Chip} level="ok" icon="circle-check" cls="ov-chip-sm" testid="ov-verified"
        title=${`Read back from the board after programming, at ${clock(dep.doneAt)}`}>verified ${hhmm(dep.doneAt)}<//>
      <span class="sub">by you · ${Math.round(dep.seconds || 0)} s</span>`
    : dep.state === "failed" ? html`<${Chip} level="err" icon="circle-x" cls="ov-chip-sm" title=${dep.reason}>failed ${hhmm(dep.doneAt)}<//><span class="sub ov-ell">${dep.reason}</span>`
      : html`<span class="secondary" title="Not programmed from this page: the name and rm_id are what the harness reports">reported by the harness</span>
        ${b.infoOkAt ? html`<span class="sub">read ${hhmm(b.infoOkAt)}</span>` : null}`;
  return html`<${OvCard} title="Design" icon="layers" testid="tile-design"
      go=${html`<${Go} label="Workbench" action="ov-go-workbench" title="Program, reset and debug on the Workbench" run=${() => navigate(bid, "workbench")} />`}>
    ${top}
    <dl class="ov-kv">
      <dt>Loaded</dt><dd data-testid="ov-loaded">${loaded}</dd>
      <${CardRow} b=${b} />
      <${OsSlots} bid=${bid} />
      <dt>Can load</dt><dd title=${`Source: the overlay catalogue for shell ${hexId(id.shell_id) || "?"}`} data-testid="ov-can-load">
        ${loadable === null ? html`<span class="muted">reading…</span>` : html`${loadable} design${loadable === 1 ? "" : "s"}${kits ? html`<span class="sub">· ${kits} kit-built</span>` : null}`}</dd>
    </dl>
  <//>`;
}

// Who is watching: the harness's presence list when the image reports it, else this page and
// the other pages the service says view the board.
function Watching({ bid }) {
  const f = front(bid);
  const b = boardState(bid);
  const p = f.body && f.body.panel;
  const list = (p && p.sessions) || [];
  if (list.length) {
    return html`<span class="ov-watch" data-testid="ov-watching" data-source="presence">${list.map((s, i) => html`<span key=${s.sid}
        title=${`${s.who} · ${s.role || ""}`}>${i ? ", " : ""}<b>${s.mine ? "you" : String(s.who || "").split("@")[0]}</b> <span class="muted">(${s.role || "watching"})</span></span>`)}</span>`;
  }
  const viewers = Number((b.background || {}).viewers || 0);
  const others = Math.max(0, viewers - 1);
  return html`<span class="ov-watch" data-testid="ov-watching" data-source="viewers"
      title="Source: the service's viewers (pages showing this board); this harness image does not list who is connected (presence)">
    <b>you</b> <span class="muted">(this page)</span>${others ? html`, <b>${others}</b> <span class="muted">other page${others === 1 ? "" : "s"}</span>` : null}</span>`;
}

function QueueRow({ x, bot }) {
  const { who, host } = whoParts(x.holder);
  const since = epochOf(x.since);
  const tip = [`${bot ? "background" : "interactive"} #${x.position}: ${x.holder}`,
    bot ? "" : x.want_s ? `wants ${wantText(x.want_s)}` : "how long not given",
    since ? `waiting since ${hhmm(since)}` : "", x.message ? `“${x.message}”` : bot ? "" : "no message"].filter(Boolean).join(" · ");
  return html`<li class=${`ov-q-row${x.mine ? " you" : ""}${bot ? " bot" : ""}`} title=${tip} data-position=${x.position}
      data-tier=${bot ? "background" : "interactive"} data-mine=${x.mine ? "yes" : "no"}>
    <span class="ov-q-pos">#${x.position}</span>
    <span class="ov-q-who">${bot ? html`<b>${who}</b><span class="tag">automation</span>`
      : html`<b>${x.mine ? "you" : who}</b>${host ? html`<span class="muted">@${host}</span>` : null}`}</span>
    <span class="ov-q-meta">${bot ? (since ? `since ${hhmm(since)}` : "") : `${wantText(x.want_s) || "?"}${since ? ` · ${hhmm(since)}` : ""}`}</span></li>`;
}

function Queue({ bid }) {
  const { people, bots, botsKnown, botsReason } = leaseQueue(bid);
  const who = leaseWho(bid);
  const src = "Source: the hub's lease queue (GET /boards/{bid}/lease: queue, background_queue). The next person gets the lease the moment it frees.";
  const mineWaiting = people.find((x) => x.mine);
  return html`<div class="ov-q" data-testid="ov-queue" data-people=${people.length} data-bots=${bots.length}>
    <div class="ov-q-h" title=${src}>Queue<span class="ov-q-n">${people.length ? `${people.length} waiting` : "nobody waiting"}</span>
      ${who.state === "other" && mineWaiting ? html`<span class="ov-q-note">${whoParts(who.holder).who} sees your request</span>` : null}</div>
    ${people.length ? html`<ul>${people.map((x) => html`<${QueueRow} key=${`p${x.position}`} x=${x} bot=${false} />`)}</ul>` : null}
    ${bots.length ? html`<div class="ov-q-h" title="The background tier: automation such as Checks. It gets the board only when no person waits, and a person's request takes the board from it.">
        <${Icon} name="repeat" />Automation<span class="ov-q-n">${bots.length} waiting · after people</span></div>
      <ul>${bots.map((x) => html`<${QueueRow} key=${`b${x.position}`} x=${x} bot=${true} />`)}</ul>`
      : botsKnown === false ? html`<div class="ov-q-h" title=${botsReason}><${Icon} name="repeat" />Automation<span class="ov-q-n">not visible here</span></div>` : null}
  </div>`;
}

function ChecksLine({ bid }) {
  const st = checksOf(bid);
  const run = st && st.run;
  const last = st && (st.last || (st.runs || [])[0]);
  if (!st) return html`<span class="muted">reading…</span>`;
  if (run) {
    const it = run.iteration || 0;
    return html`<span class="chip accent ov-chip-sm" title=${`Running: ${run.id} (${run.plan}, writes ${run.writes})`}>
      <${Icon} name="loader-circle" cls="spin" />${run.state} · ${it}/${run.repeat || "?"}</span>
      <span class="sub">${(run.totals && run.totals.pass) || 0} pass · ${(run.totals && run.totals.fail) || 0} fail</span>`;
  }
  if (last && last.result) {
    const ok = last.result === "PASS";
    const at = epochOf(last.ended_at || last.started_at || last.created_at);
    return html`<span class=${`chip ov-chip-sm ${ok ? "ok" : "err"}`} title=${`Last run ${last.id}: ${last.result}`}>
      <${Icon} name=${ok ? "circle-check" : "circle-x"} />${last.result}</span>${at ? html`<span class="sub">${hhmm(at)}</span>` : null}`;
  }
  return html`<span class="secondary">no runs on this board</span>`;
}

function leaseChip(who) {
  const left = leaseLeft(who.lease);
  const leftText = left !== null ? ` · ${durationText(left)}` : "";
  const where = leaseWhere(who);
  switch (who.state) {
    case "here": return { level: "accent", icon: "user", text: `Yours${leftText}`, title: `Your hub lease: ${where}, held by this Harness Manager` };
    case "free": return { level: "plain", icon: "lock-open", text: "Free", title: `Free: nobody holds ${where}` };
    case "elsewhere": return { level: "held", icon: "lock", text: `Held by you in another session${leftText}`, title: `${who.holder}: ${where}; your hub name, but not this Harness Manager` };
    case "unknown": return { level: "unk", icon: "circle-help", text: "Lease unknown", title: `${where}: ${who.error || "the hub did not answer"}` };
    case "unread": return { level: "unk", icon: "circle-help", text: "Reading the lease…", title: "" };
    default: return { level: "held", icon: "lock", text: `Held by ${whoParts(who.holder).who}${leftText}`, title: `${who.holder} holds ${where}` };
  }
}

// The outcome of this page's last lease action (the header's Acquire, Release, Request): one
// box, while it is fresh (0.1.0 showed it under Needs attention).
const LEASE_RESULT_MS = 120000;

function LeaseResult({ bid }) {
  const p = panelState(bid, "lease");
  if (!p.lines || !p.lines.length) return null;
  if (!p.running && p.startedAt && Date.now() - p.startedAt > LEASE_RESULT_MS) return null;
  return html`<${ResultBlock} lines=${p.lines} panel=${p} testid="lease-result" />`;
}

function LeaseCard({ bid }) {
  const who = leaseWho(bid);
  const b = boardState(bid);
  const row = S.boards[bid] || {};
  if (who.state === "none") {
    const holder = row.holder;
    return html`<${OvCard} title="Access" icon="ethernet-port" testid="ov-access"
        go=${html`<${Go} label="Board" action="ov-go-about" title="About this board" run=${() => navigate(bid, "board/about")} />`}>
      <div class="ov-lease-top"><${Chip} icon="lock-open" cls="ov-chip-sm plain">No hub lease<//>
        <span class="small secondary">a desk board on this network</span></div>
      <dl class="ov-kv">
        <dt>Who drives</dt><dd title="Source: this app's board lock (one session: this page and the CLI share it)" data-testid="ov-who-drives">
          ${holder ? html`whoever has it open: <b>${holder.user || "?"}</b>${holder.host ? html` <span class="muted">on ${holder.host}</span>` : null}` : "whoever has it open"}</dd>
        <dt>Watching</dt><dd><${Watching} bid=${bid} /></dd>
        <dt>Checks</dt><dd class="secondary" title="Leases, queues and unattended runs need a hub">need a hub</dd>
      </dl><//>`;
  }
  const c = leaseChip(who);
  const lease = who.lease;
  const exp = lease ? epochOf(lease.expires_at) : null;
  let until = "";
  if (who.state === "here" && exp) until = `until ${hhmm(exp)}`;
  else if ((who.state === "other" || who.state === "elsewhere") && exp) until = `until ${hhmm(exp)} · ${who.holder}`;
  else if (who.state === "free") until = "nobody holds it: Acquire is in the header";
  const w = waitingForYou(bid);
  const people = leaseQueue(bid).people;
  const firstOther = people.find((x) => x.mine) || people[0];
  let wait = null;
  if (w) {
    const since = epochOf(w.first.since);
    wait = html`<div class="ov-wait" data-testid="ov-wait" title=${`#${w.first.position}${since ? ` since ${hhmm(since)}` : ""}${w.first.message ? `: “${w.first.message}”` : ""}. When you release, the hub hands the lease to ${whoParts(w.first.holder).who} at once.`}>
      <${Icon} name="triangle-alert" /><span><b>${waitWords(w)}:</b> release when you're done.
      <button type="button" class="link" data-action="ov-release" onClick=${(e) => openReleaseConfirm(bid, e.currentTarget)}>Release…</button></span></div>`;
  } else if ((who.state === "other" || who.state === "elsewhere") && firstOther) {
    const nm = whoParts(firstOther.holder).who;
    wait = html`<div class="ov-wait info" data-testid="ov-wait" title="The hub promotes the next person the moment the lease frees">
      <${Icon} name="info" /><span><b>${firstOther.mine ? `You wait, #${firstOther.position}` : `${nm} waits`}${firstOther.want_s ? ` (${wantText(firstOther.want_s)})` : ""}</b>: ${firstOther.mine ? (firstOther.position === 1 ? "you get" : "your turn comes after the others") : "gets"}${firstOther.mine && firstOther.position !== 1 ? "" : ` it when ${whoParts(who.holder).who} releases`}${exp ? ` (the lease runs to ${hhmm(exp)})` : ""}.</span></div>`;
  }
  const stale = who.stale ? `the hub did not answer the last read; last confirmed ${hhmm(epochOf(who.stale.confirmed_at))}` : "";
  return html`<${OvCard} title="Lease" icon="lock" testid="ov-lease"
      go=${html`<${Go} label="Checks" action="ov-go-checks" title="Unattended runs on the Checks tab" run=${() => navigate(bid, "checks")} />`}>
    <div class="ov-lease-top" data-testid="tile-lease" data-lease=${who.state}
        title=${`Source: the hub${who.host ? ` (${who.host})` : ""}: the lease and its queue${stale ? `; ${stale}` : ""}`}>
      <${Chip} level=${c.level} icon=${c.icon} cls="ov-chip-sm" title=${c.title} testid="ov-lease-chip">${c.text}<//>
      ${until ? html`<span class="small secondary ov-ell">${until}</span>` : null}</div>
    ${wait}
    <${LeaseResult} bid=${bid} />
    <${Queue} bid=${bid} />
    <dl class="ov-kv">
      <dt>Watching</dt><dd><${Watching} bid=${bid} /></dd>
      <dt>Checks</dt><dd data-testid="ov-checks" title="Source: this service's checks runs (GET /boards/{bid}/checks)"><${ChecksLine} bid=${bid} /></dd>
    </dl>
  <//>`;
}

// The console's last line (this page's own session: a console the Workbench opened), or why none.
function lastLine(bid, name) {
  const s = existingSession(bid, name);
  if (!s) return { text: "not open in this page", sys: true };
  let text = "";
  try { text = s.text(); } catch (e) { text = ""; }
  const lines = text.split("\n").map((l) => l.trimEnd()).filter((l) => l && !/^\S*[>#$] ?$/.test(l));
  return lines.length ? { text: lines[lines.length - 1], sys: false, state: s.state } : { text: "(nothing yet)", sys: true, state: s.state };
}

const CONSOLE_DOT = { up: "ok", connecting: "unk", down: "warn", closed: "unk" };
const ROLE_NAMES = { dut: "DUT console", shell: "Shell console", "linux-root": "Harness console", mcc: "MCC log", lane: "FPGA UART" };

function LiveCard({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  const x = xvc(bid);
  useEffect(() => { if (!x.st && !x.error) loadXvc(bid); }, [bid]);
  const meta = Object.fromEntries((w.consoles || []).map((c) => [c.name, c]));
  const rows = consoleRows(bid, b.consoles || (w.consoles || []).map((c) => c.name));
  const main = [];
  const also = [];
  for (const r of rows) {
    const m = meta[r.name] || {};
    const role = m.role || (r.name === "shell" ? "shell" : /^uart|^swo/.test(r.name) ? "dut" : "");
    if ((role === "dut" && r.name === "uart0") || role === "shell") main.push({ ...r, role, m });
    else also.push({ ...r, role, m });
  }
  if (!main.length) {
    const first = also.findIndex((r) => r.role === "dut");
    if (first >= 0) main.push(...also.splice(first, 1));
  }
  const d = b.debug || { state: "unknown" };
  const ports = Array.isArray(d.gdb_ports) && d.gdb_ports.length ? d.gdb_ports : d.gdb_port ? [d.gdb_port] : [];
  const cores = Array.isArray(d.cores) ? d.cores : [];
  const where = d.where === "board" ? "OpenOCD runs on the board" : d.where === "host" ? "OpenOCD runs on this PC" : "";
  const dbgTip = d.state === "up" ? `Source: this app's OpenOCD session · ${ports.map((p, i) => `${cores[i] || `core ${i}`}: gdb 127.0.0.1:${p}`).join(" · ")}${where ? ` · ${where}` : ""}`
    : `Source: this app's OpenOCD session: ${d.state}${d.detail ? ` (${d.detail})` : ""}. Open session on the Workbench gives the gdb line.`;
  const vs = viewState(bid);
  const st = x.st || {};
  const ila = st.reason ? { level: "", text: "none here", tip: `not on this board: ${st.reason}` }
    : vs === "reading" ? { level: "", text: "reading…", tip: "" }
      : st.open ? { level: vs === "attached" ? "accent" : "ok",
        text: `${vs === "attached" ? "attached · " : "XVC "}${st.url ? `:${String(st.url).split(":").pop()}` : "open"}`,
        tip: `XVC open at ${st.url || "?"}${vs === "attached" ? " · a client (Vivado) is attached" : ""}` }
        : { level: "", text: vs === "held" ? "held" : "closed", tip: `the shell's XVC server: ${vs}` };
  return html`<${OvCard} title="Consoles and debug" icon="terminal" testid="tile-consoles"
      go=${html`<${Go} label="Workbench" action="ov-go-consoles" title="The console, Reset DUT and Open session are on the Workbench" run=${() => navigate(bid, "workbench")} />`}>
    ${main.length ? html`<ul class="ov-cons">${main.slice(0, 2).map((r) => {
      const l = lastLine(bid, r.name);
      const state = l.state || w.consoleState[r.name] || r.m.state || "";
      return html`<li key=${r.name} data-console=${r.name} title=${`Source: ${r.name} (${r.m.kind || "console"}${state ? ` · ${state}` : ""}) · last line: ${l.text}`}>
        <span class=${`dot ${CONSOLE_DOT[state] || "unk"}`}></span>
        <button type="button" class="ov-con-name" onClick=${() => navigate(bid, `workbench?console=${encodeURIComponent(r.name)}`)}>${ROLE_NAMES[r.role] || r.name}</button>
        <span class=${`ov-last${l.sys ? " sys" : ""}`}>${l.text}</span></li>`;
    })}</ul>` : html`<p class="small muted">${b.consoles ? "No DUT console on this board." : "Reading the consoles…"}</p>`}
    <dl class="ov-kv">
      ${also.length ? html`<dt>Also</dt><dd data-testid="ov-also">${also.map((r) => {
        const ro = r.m.writable === false;
        return html`<button type="button" key=${r.name} class="chip ov-chip-sm" data-console=${r.name}
            title=${`${ROLE_NAMES[r.role] || r.name} (${r.name})${ro ? ` · read only: ${r.m.read_only_reason || ""}` : r.role === "linux-root" ? " · a root shell: only the lease holder types" : ""}`}
            onClick=${() => navigate(bid, `workbench?console=${encodeURIComponent(r.name)}`)}>
          <span class=${`dot ${CONSOLE_DOT[w.consoleState[r.name] || r.m.state] || "unk"}`}></span>${r.role === "linux-root" ? "Harness" : r.name}${ro ? html`<${Icon} name="eye" />` : null}</button>`;
      })}</dd>` : null}
      <dt>Debug</dt><dd data-testid="tile-debug" data-state=${d.state}>
        <span title=${dbgTip}>${d.state === "up" ? html`<span class="chip ok ov-chip-sm" data-testid="tile-debug-state"><${Icon} name="circle-check" />${ports.length ? `gdb :${ports[0]}${ports.length > 1 ? ` +${ports.length - 1}` : ""}` : "up"}</span>`
          : d.state === "starting" ? html`<span class="chip accent ov-chip-sm" data-testid="tile-debug-state"><${Icon} name="loader-circle" cls="spin" />opening</span>`
            : html`<span class="chip ov-chip-sm" data-testid="tile-debug-state">${d.state === "failed" ? "failed" : "down"}</span>`}</span>
        <span class="sub">ILAs</span>
        <span class=${`chip ov-chip-sm ${ila.level}`} data-testid="tile-xvc" data-state=${st.reason ? "unsupported" : vs} title=${ila.tip}>${ila.text}</span></dd>
    </dl>
  <//>`;
}

const LOG_ICONS = { ok: "circle-check", error: "circle-x", warning: "triangle-alert", info: "info" };

// State chatter the cards above already show (the picture, the panel, a console's state) is
// left out here; the Activity drawer has everything.
const CHATTY = /^(display|panel|console)\./;

function RecentCard({ bid }) {
  const rows = S.log.filter((e) => e.board === bid && !CHATTY.test(e.source)).slice(-4).reverse();
  return html`<${OvCard} title="Recent activity" icon="history" testid="ov-recent"
      go=${html`<${Go} label="Activity" action="ov-go-activity" title="Everything on this board, newest first" run=${() => openActivity(bid)} />`}>
    ${rows.length ? html`<ul class="ov-log">${rows.map((e) => html`<li key=${e.id}><button type="button" class="ov-log-row"
        data-level=${e.level} title=${`${clock(e.at)} ${e.source}: ${e.text} · open in Activity`}
        onClick=${() => openActivity(bid, e.level === "error" ? "err" : "all")}>
        <span class="t">${hhmm(e.at)}</span><span class=${`lv-${e.level}`}><${Icon} name=${LOG_ICONS[e.level] || "info"} /></span>
        <span class="m"><span class="src">${e.source}</span> ${e.text}</span></button></li>`)}</ul>`
      : html`<div class="small muted">Nothing on this board since this page opened.</div>`}
  <//>`;
}

// --- the section ------------------------------------------------------------------------------------------

// Telemetry (SYSMON over JTAG, ~3 s a read; an MCC read over a hub share ~2 s, the oscillators ~6
// s) is polled in the background, never awaited by anything else, and the last good values stay.
const TELEMETRY_POLL_MS = 15000;
const HUB_TELEMETRY_POLL_MS = 30000;

// FIX-PACK-1: reads the daemon held back before any answer (background reads off, the lease
// elsewhere, busy): the first such reason, or null.
export function tileQuiet(bid) {
  const b = boardState(bid);
  const f = front(bid);
  return (!b.telemetry && b.telemetryQuiet) || (hasCardStore(b) && !b.card && b.cardQuiet)
    || (!f.body && f.quiet) || null;
}

function readTileNow(bid) {
  const b = boardState(bid);
  if (!b.telemetry && b.telemetryQuiet) loadTelemetry(bid);
  if (hasCardStore(b) && !b.card && b.cardQuiet) loadCard(bid);
  if (!front(bid).body) readPanelNow(bid);
}

function useOverviewReads(bid) {
  const b = boardState(bid);
  useEffect(() => {
    const timer = setInterval(() => {
      const now = boardState(bid);
      const every = week(bid).hub ? HUB_TELEMETRY_POLL_MS : TELEMETRY_POLL_MS;
      if (document.visibilityState === "visible" && now.info && !now.job && !now.telemetryLoading
          && Date.now() - (now.telemetryAt || 0) >= every - 1000) {
        loadTelemetry(bid, { background: true });     // QUIET-POLL: nobody clicked
      }
    }, TELEMETRY_POLL_MS);
    if (!clashes.list || Date.now() - clashes.at > 60000) loadClashes();
    return () => clearInterval(timer);
  }, [bid]);
  // the temperature's trend: read again after each telemetry read (a minute apart at most)
  const t = pickTemperature(b.telemetry);
  const tName = t && has(t.value) ? t.name : "";
  useEffect(() => {
    if (!tName) return;
    const h = b.ovHist;
    if (!h || h.name !== tName || Date.now() - h.at > 55000) loadHistory(bid, tName);
  }, [bid, tName, b.telemetryAt]);
  // the DUT clock from GET /clocks only when telemetry has none (O13: a real board's case)
  const needClock = !!b.telemetry && !(b.telemetry || []).some((r) => r.name === "dut_clk");
  useEffect(() => {
    const w = week(bid);
    if (needClock && !w.clocks && !w.clocksError) loadClocks(bid);
  }, [bid, needClock]);
  // the background queue once, for a hub board (week.js keeps only the people)
  const hub = !!week(bid).hub;
  useEffect(() => { if (hub && !b.ovBg && !b.ovBgLoading) loadBackground(bid); }, [bid, hub]);
}

export function OverviewSection({ bid }) {
  const b = boardState(bid);
  useOverviewReads(bid);
  if (!b.info && b.quiet && !b.infoError) {
    // QUIET-POLL: the daemon held the first read back: nothing was read. Say why, calmly.
    return html`<div class="card" data-testid="info-quiet"><div class="card-body">
      <${Reason} text=${b.quiet.text} />
      <p class="mt-14"><button type="button" class="btn sm" onClick=${() => refreshInfo(bid)}>
        <${Icon} name="refresh-cw" /> Read now</button></p></div></div>`;
  }
  if (!b.info) {
    return html`<div class="card" data-testid="info-error"><div class="card-body">
      ${b.infoError ? html`<${Reason} level="err" text=${`${b.infoLine}: ${b.infoError.errName}: ${b.infoError.message}`} />
          ${b.infoError.hint ? html`<div class="mt-8"><${Reason} text=${b.infoError.hint} /></div>` : null}
          <p class="mt-14"><button type="button" class="btn sm" onClick=${() => refreshInfo(bid)}>
            <${Icon} name="refresh-cw" /> Read again</button></p>`
        : html`<p class="muted"><${Spinner} /> Reading the board...</p>`}</div></div>`;
  }
  const quiet = tileQuiet(bid);
  const reading = b.telemetryLoading || b.cardLoading || front(bid).loading;
  return html`<div class="ov" data-testid="overview">
    <${Attention} bid=${bid} />
    ${quiet ? html`<${QuietNote} testid="tile-board-quiet" action="tile-read-now" text=${quietWords(quiet)}
      busy=${reading} onRead=${() => readTileNow(bid)} />` : null}
    <${Summary} bid=${bid} />
    <div class="ov-grid">
      <${DesignCard} bid=${bid} />
      <${LeaseCard} bid=${bid} />
      <${FrontPanelCard} bid=${bid} />
      <${LiveCard} bid=${bid} />
      <${RecentCard} bid=${bid} />
    </div>
  </div>`;
}
