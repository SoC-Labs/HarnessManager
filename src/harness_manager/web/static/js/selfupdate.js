// The app's own update (lane OTA-U; docs/design/HM_SELF_UPDATE.md §5 and §9 "UI", docs/API.md
// "App self-update"): the banner at the top of every page, the apply confirm, the restart
// overlay, and the Settings dialog's Updates card.
//
// david's decisions as the page shows them:
// - U3: notify, stage automatically, apply when the user clicks. The banner says "ready:
//   Restart to update" once a version is staged; under a `notify` policy it says
//   "available" and offers the download. The page never applies anything by itself.
// - U4: the console PTYs keep their paths across the restart; the confirm says to re-run
//   `screen <path>`, and the daemon writes the same notice into each PTY.
// - U6: an administrator's policy file can restrict the settings: its controls are disabled
//   and say "set by your admin policy".
// - A developer install never self-updates: no banner, and Settings says why.
//
// The restart keeps the port and the token, so this page reconnects by itself and reloads
// once GET /health reports another version. A version whose apply failed its health check is
// rolled back by the daemon's helper, marked bad and never offered again: the page says so.

import { call, toApiError, waitJob } from "./api.js";
import { ageText, clock } from "./format.js";
import { html, useLayoutEffect, useRef } from "./lib.js";
import { changed, log, onBoardEvent, onEventsReconnected, timed } from "./store.js";
import { Card, Chip, Icon, Reason, Spinner } from "./ui.js";
// SET-UI: the dialog's sections (js/settings/*); the dialog stays this one (SETTINGS.md §7)
import { RestartNote, SettingsNav, SettingsSectionBody } from "./settings/sections.js";
import { loadSettings, SECTIONS, setSection, SS, whenOpen } from "./settings/state.js";

const DISMISS_KEY = "harness_manager.update.dismissed";
const NOTES_KEY = "harness_manager.update.notes";
const POLL_MS = 1000;                  // /health while the service restarts
const STATUS_EVERY_MS = 60000;         // GET /update/app now and then (the checker runs 6-hourly)
export const ADMIN = "set by your admin policy";
export const CHANNELS = ["stable", "beta", "dev"];
export const MODES = [
  { value: "off", label: "Off", title: "Never check for app updates" },
  { value: "notify", label: "Notify", title: "Say when an update exists; download it when you click" },
  { value: "stage", label: "Stage", title: "Download and prepare updates in the background; apply when you click (the default)" },
];
const MODE_TEXT = {
  off: "Off: Harness Manager never checks for a new version.",
  notify: "Notify: the banner says when a new version exists; it downloads when you click.",
  stage: "Stage: new versions download and are prepared in the background; the switch happens only when you click Restart to update.",
};
const MODE_RANK = { off: 0, notify: 1, stage: 2 };
const PHASES = [
  { key: "checking", label: "Checking" },
  { key: "draining", label: "Finishing running jobs" },
  { key: "restarting", label: "Restarting" },
];

function readJson(store, key) {
  try { return JSON.parse(window[store].getItem(key) || "{}") || {}; } catch (e) { return {}; }
}

function writeJson(store, key, value) {
  try { window[store].setItem(key, JSON.stringify(value)); } catch (e) { /* not remembered */ }
}

export const U = {
  status: null,          // GET /update/app
  error: null,           // the last read's ApiError (not "unavailable")
  unavailable: "",       // no update service in this daemon: why
  loadedVersion: "",     // the /health version this page was loaded from
  notes: readJson("sessionStorage", NOTES_KEY),     // version -> the signed notes seen in events
  dismissed: readJson("localStorage", DISMISS_KEY), // "staged:V" | "available:V" | "apply:ID" -> 1
  confirm: null,         // {version, busy: [...]} from a 409 SOFT_BUSY
  applyError: null,      // why the last apply click was refused
  applying: null,        // {id, phase, from, to, waiting_on, eta_s, reason}
  overlay: null,         // {to, from, id, started, deadline, down, late}
  outcome: null,         // an update.applying failed/cancelled seen live: {phase, to, reason}
  stage: { running: false, error: null, line: "" },
  settingsOpen: false,
  settingsBusy: "",      // "channel" | "auto" while a PUT runs
  settingsError: null,
  notesOpen: false,
};

// --- reads ---------------------------------------------------------------------------------------

export async function loadAppUpdate() {
  const r = await timed("update status", () => call("appUpdate"));
  if (r.error) {
    const e = r.error;
    if (e.errName === "UNAVAILABLE" || (e.status === 404 && /no such endpoint/.test(e.message))) {
      U.unavailable = e.reason || e.message;
      U.status = null;
      U.error = null;
    } else if (!e.transport) {
      U.error = e;
    }
  } else {
    U.status = r.data.data;
    U.unavailable = "";
    U.error = null;
    const apply = U.status.apply || {};
    if (apply.state && apply.state !== "idle" && !U.applying) {
      U.applying = { id: apply.id, phase: apply.state, from: apply.from, to: apply.to,
        waiting_on: apply.waiting_on || [] };
    }
  }
  changed();
  return r;
}

function remember(version, notes) {
  if (!version || !notes) return;
  U.notes[version] = notes;
  writeJson("sessionStorage", NOTES_KEY, U.notes);
}

export function dismiss(key) {
  U.dismissed[key] = 1;
  writeJson("localStorage", DISMISS_KEY, U.dismissed);
  changed();
}

// What the banner offers: a staged version (Restart to update), else one that is only
// available (download it). Nothing for a developer install or when the admin turned it off.
export function offer(st = U.status) {
  if (!st || st.dev_install) return null;
  const policy = st.policy || {};
  const bad = st.bad || {};
  const staged = (st.staged || []).find((v) => !bad[v]);
  if (staged && policy.self_update !== "off") return { kind: "staged", version: staged };
  const eff = st.effective || {};
  const avail = st.available;
  if (avail && eff.auto !== "off" && avail !== st.running && !bad[avail]
      && !(st.staged || []).includes(avail)) {
    return { kind: "available", version: avail, auto: eff.auto };
  }
  return null;
}

// How the last apply ended, from last_apply.json (it outlives the restart that wrote it).
export function lastOutcome(st = U.status) {
  const la = st && st.last_apply;
  if (!la || !la.id || U.dismissed[`apply:${la.id}`]) return null;
  if (la.result === "rolled-back" || la.result === "not-switched" || la.result === "down") return la;
  if (la.result === "refused") return la;
  // "applied": say it once, while it is recent and this page runs what it applied
  const at = Number(la.at || 0);
  if (la.result === "applied" && la.to === st.running && (!at || Date.now() / 1000 - at < 600)) return la;
  return null;
}

// --- the apply ------------------------------------------------------------------------------------

export async function startApply(version, confirm = false) {
  U.applyError = null;
  const body = { version, ...(confirm ? { confirm: true } : {}) };
  const r = await timed(`update app --apply ${version}${confirm ? " --yes" : ""}`, () => call("appApply", {}, body));
  if (r.error) {
    const d = r.error.data || {};
    if (r.error.errName === "REFUSED" && d.reason === "SOFT_BUSY" && !confirm) {
      U.confirm = { version, busy: d.soft_busy || [] };
    } else {
      U.confirm = null;
      U.applyError = r.error;
    }
    log(d.reason === "SOFT_BUSY" ? "warning" : "error", "update", `${r.line}  ${r.error.errName}: ${r.error.message}`);
    changed();
    return r;
  }
  U.confirm = null;
  U.outcome = null;
  const a = r.data.data.apply || {};
  U.applying = { id: a.id, phase: a.state || "checking", from: a.from, to: a.to || version,
    waiting_on: a.waiting_on || [] };
  log("info", "update", `${r.line}: applying harness-manager ${U.applying.to}`);
  changed();
  return r;
}

export async function cancelApply() {
  const r = await timed("update app --cancel", () => call("appCancel", {}, {}));
  if (r.error) U.applyError = r.error;
  else U.applying = null;
  changed();
  loadAppUpdate();
}

// POST /update/app {stage_only: true}: download and prepare the version (never switches).
export async function stageNow(version) {
  if (U.stage.running) return;
  U.stage = { running: true, error: null, line: "downloading..." };
  changed();
  try {
    const { data } = await call("updateApp", {}, { version, stage_only: true });
    const out = data.job ? await waitJob(data.job, {
      onProgress: (p) => { U.stage.line = `${p.phase || "stage"}${p.total > 1 ? `: ${Math.floor((p.done * 100) / p.total)}%` : ""}`; changed(); },
    }) : data;
    U.stage = { running: false, error: null, line: out && out.staged === false ? (out.why || "not staged") : "" };
    if (out && out.notes) remember(version, out.notes);
  } catch (e) {
    U.stage = { running: false, error: toApiError(e), line: "" };
  }
  changed();
  loadAppUpdate();
}

// --- the restart: overlay, countdown, reconnect, reload --------------------------------------------

let pollTimer = 0;

function startOverlay(ev) {
  const eta = Number(ev.eta_s || 30);
  U.overlay = { id: ev.id || "", to: ev.to || "", from: ev.from || "", started: Date.now(),
    deadline: Date.now() + eta * 1000, down: false, late: false };
  clearInterval(pollTimer);
  pollTimer = setInterval(pollHealth, POLL_MS);
  changed();
}

function stopOverlay() {
  clearInterval(pollTimer);
  pollTimer = 0;
  U.overlay = null;
  changed();
}

function reloadPage() {
  clearInterval(pollTimer);
  window.location.reload();
}

let polling = false;
async function pollHealth() {
  const o = U.overlay;
  if (!o || polling) return;
  polling = true;
  try {
    o.late = Date.now() > o.deadline;
    let version = "";
    try {
      const { data } = await call("health");
      version = data.version || "";
    } catch (e) {
      o.down = true;             // the old daemon has gone: the new one is on its way
    }
    if (version && U.loadedVersion && version !== U.loadedVersion) {
      reloadPage();
      return;
    }
    if (version) {
      // The same version answers: still the old daemon (before it exits), or the old one again
      // after a rollback. last_apply.json with this apply's id settles it.
      const r = await timed("update status", () => call("appUpdate"));
      const st = r.error ? null : r.data.data;
      const la = st && st.last_apply;
      if (st) U.status = st;
      if (la && o.id && la.id === o.id && la.result !== "applied") {
        U.applying = null;
        stopOverlay();
        return;
      }
    }
    changed();
  } finally {
    polling = false;
  }
}

// --- events -----------------------------------------------------------------------------------------

let statusTimer = 0;
function scheduleStatus(ms = 150) {
  clearTimeout(statusTimer);
  statusTimer = setTimeout(loadAppUpdate, ms);
}

onBoardEvent((ev) => {
  const d = ev.data || {};
  if (ev.topic === "update.available" && !ev.board_id) {
    remember(d.app, d.notes);
    scheduleStatus();
  } else if (ev.topic === "update.app.staged") {
    remember(d.version, d.notes);
    scheduleStatus();
  } else if (ev.topic === "update.applying") {
    const phase = d.phase || "";
    if (phase === "cancelled" || phase === "failed") {
      U.applying = null;
      U.outcome = { phase, to: d.to || "", reason: d.reason || "" };
      stopOverlay();
      scheduleStatus();
    } else {
      U.applying = { ...(U.applying || {}), id: d.id, phase, from: d.from, to: d.to,
        waiting_on: d.waiting_on || (U.applying && U.applying.waiting_on) || [], eta_s: d.eta_s };
      if (phase === "restarting" && !U.overlay) startOverlay(d);
    }
    changed();
  } else if (ev.topic === "update.applied") {
    // The new daemon says so; the /health poll reloads the page onto it.
    if (!U.overlay) startOverlay({ ...d, eta_s: 10 });
    pollHealth();
  } else if (ev.topic === "update.rolled_back") {
    U.applying = null;
    stopOverlay();
    scheduleStatus(0);
  }
});

// The event socket came back: a daemon restarted onto another version reloads this page.
onEventsReconnected(async () => {
  const r = await timed("health", () => call("health"));
  const version = r.error ? "" : r.data.data.version || "";
  if (!U.loadedVersion) U.loadedVersion = version;
  else if (version && version !== U.loadedVersion) reloadPage();
  loadAppUpdate();
});

// Called once at start-up: the version this page came from, then the status.
export async function startSelfUpdate() {
  const h = await timed("health", () => call("health"));
  if (!h.error && !U.loadedVersion) U.loadedVersion = h.data.data.version || "";
  loadAppUpdate();
  setInterval(() => {
    if (document.visibilityState === "visible" && !U.overlay) loadAppUpdate();
  }, STATUS_EVERY_MS);
  // the overlay's countdown
  setInterval(() => { if (U.overlay) changed(); }, 500);
}

// --- the banners ------------------------------------------------------------------------------------

function NotesToggle({ version }) {
  const notes = U.notes[version];
  if (!notes) return null;
  return html`<button type="button" class="btn ghost sm" data-action="app-notes"
    aria-expanded=${U.notesOpen ? "true" : "false"} onClick=${() => { U.notesOpen = !U.notesOpen; changed(); }}>
    <${Icon} name="chevron-right" cls=${`sm chev ${U.notesOpen ? "open" : ""}`} />What's new</button>`;
}

function Notes({ version }) {
  const notes = U.notes[version];
  if (!notes || !U.notesOpen) return null;
  return html`<pre class="update-notes" data-testid="app-notes">${notes}</pre>`;
}

function ApplySteps({ a }) {
  const idx = PHASES.findIndex((p) => p.key === a.phase);
  const waiting = (a.waiting_on || []).map((j) => `${j.kind} ${j.board_id ? `on ${j.board_id}` : "in the service"}`);
  return html`<div class="grow">
    <strong>Updating Harness Manager to ${a.to}.</strong>
    <ol class="apply-steps" data-testid="apply-steps">${PHASES.map((p, i) => html`<li key=${p.key}
      class=${i < idx ? "done" : i === idx ? "active" : ""} data-phase=${p.key}>${i === idx ? html`<${Spinner} />` : null}${p.label}</li>`)}</ol>
    ${a.phase === "draining" && waiting.length ? html`<div class="secondary small" data-testid="apply-waiting">Waiting for ${waiting.join(", ")}. New jobs wait until the restart.</div>` : null}
  </div>`;
}

function outcomeBanner(la, running) {
  const key = `apply:${la.id}`;
  const close = html`<button type="button" class="btn ghost sm icon-only" aria-label="Dismiss" title="Dismiss"
    data-action="dismiss-outcome" onClick=${() => dismiss(key)}><${Icon} name="x" /></button>`;
  if (la.result === "applied") {
    return html`<div class="banner ok" role="status" key="applied" data-testid="app-updated">
      <${Icon} name="circle-check" /><div class="grow"><strong>Updated to Harness Manager ${la.to}.</strong>${" "}
      ${la.from ? `It replaced ${la.from}. ` : ""}Consoles kept their PTY paths: re-run <code>screen</code> on them if one was attached.</div>${close}</div>`;
  }
  const what = la.phase === "self-test" ? "failed its self-test"
    : la.result === "not-switched" ? "could not be switched to" : "failed its health check";
  const back = la.result === "refused" ? `still on ${la.from || running}` : `back on ${la.from || running}`;
  return html`<div class="banner err" role="alert" key="rolled-back" data-testid="app-rolled-back">
    <${Icon} name="circle-x" /><div class="grow"><strong>Harness Manager ${la.to} ${what}; ${back}.${la.result === "rolled-back" || la.result === "refused" ? " It won't be offered again." : ""}</strong>
    ${la.reason ? html`<div class="secondary small mt-8" data-testid="app-rolled-back-why">${la.reason}</div>` : null}</div>${close}</div>`;
}

// The app-wide banners, top of every page: how the last apply ended, an apply in progress, and
// the offer (staged: Restart to update; available: download it).
export function AppUpdateBanners() {
  const st = U.status;
  // SET-UI: settings changes that wait for a service restart (the restart banner)
  const out = [html`<${RestartNote} key="settings-restart" testid="restart-banner" cls="banner" />`];
  if (!st) return out;
  const la = lastOutcome(st);
  if (la && !U.applying) out.push(outcomeBanner(la, st.running));
  if (U.outcome && !U.applying) {
    const o = U.outcome;
    out.push(html`<div class=${`banner ${o.phase === "failed" ? "err" : "warn"}`} role="status" key="apply-ended"
      data-testid="app-apply-ended"><${Icon} name=${o.phase === "failed" ? "circle-x" : "info"} />
      <div class="grow"><strong>${o.phase === "failed" ? `The update to ${o.to} did not go ahead` : `The update to ${o.to} was cancelled`}.</strong>
      ${" "}${o.reason ? `${o.reason}. ` : ""}Nothing was restarted.</div>
      <button type="button" class="btn ghost sm icon-only" aria-label="Dismiss" onClick=${() => { U.outcome = null; changed(); }}><${Icon} name="x" /></button></div>`);
  }
  if (U.applying && !U.overlay) {
    out.push(html`<div class="banner info" role="status" key="applying" data-testid="app-applying">
      <${Icon} name="refresh-cw" /><${ApplySteps} a=${U.applying} />
      ${U.applying.phase !== "restarting" ? html`<button type="button" class="btn sm" data-action="app-cancel"
        onClick=${cancelApply}>Cancel</button>` : null}</div>`);
    return out;
  }
  const o = offer(st);
  if (!o || U.dismissed[`${o.kind}:${o.version}`]) return out;
  const later = html`<button type="button" class="btn ghost sm" data-action="app-later"
    title="Hide this until the next version" onClick=${() => dismiss(`${o.kind}:${o.version}`)}>Later</button>`;
  if (o.kind === "staged") {
    out.push(html`<div class="banner info" role="status" key="staged" data-testid="app-update-banner" data-kind="staged">
      <${Icon} name="rocket" /><div class="grow">
        <strong>Harness Manager ${o.version} is ready: Restart to update.</strong>${" "}
        <span class="secondary">You run ${st.running}. The restart takes a few seconds; the page reconnects by itself.</span>
        <${Notes} version=${o.version} />
        ${U.applyError ? html`<div class="mt-8"><${Reason} level="err" testid="app-apply-error"
          text=${`${U.applyError.errName}: ${U.applyError.message}${U.applyError.hint ? ` (${U.applyError.hint})` : ""}`} /></div>` : null}
      </div>
      <${NotesToggle} version=${o.version} />${later}
      <button type="button" class="btn primary sm" data-action="app-apply" onClick=${() => startApply(o.version)}>
        <${Icon} name="refresh-cw" /> Restart to update</button></div>`);
  } else {
    const auto = o.auto === "stage";
    out.push(html`<div class="banner info" role="status" key="available" data-testid="app-update-banner" data-kind="available">
      <${Icon} name="download" /><div class="grow">
        <strong>Harness Manager ${o.version} is available.</strong>${" "}
        <span class="secondary">${auto ? "It downloads in the background; you restart when you choose."
          : "Download it now; you restart when it is ready."}</span>
        <${Notes} version=${o.version} />
        ${U.stage.running ? html`<div class="secondary small mt-8" data-testid="app-stage-line"><${Spinner} /> ${U.stage.line}</div>` : null}
        ${U.stage.error ? html`<div class="mt-8"><${Reason} level="err" testid="app-stage-error"
          text=${`${U.stage.error.errName}: ${U.stage.error.message}`} /></div>` : null}
        ${!U.stage.running && U.stage.line ? html`<div class="mt-8"><${Reason} level="warn" text=${U.stage.line} /></div>` : null}
      </div>
      <${NotesToggle} version=${o.version} />${later}
      <button type="button" class="btn primary sm" data-action="app-stage" aria-busy=${U.stage.running ? "true" : undefined}
        onClick=${() => stageNow(o.version)}>${U.stage.running ? html`<${Spinner} />` : html`<${Icon} name="download" />`} Download</button></div>`);
  }
  return out;
}

// --- dialogs ---------------------------------------------------------------------------------------

function useEscape(ref, onEscape) {
  useLayoutEffect(() => {
    const first = ref.current && ref.current.querySelector("[data-autofocus]");
    if (first) first.focus();
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); onEscape(); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

const BUSY_ICONS = { gdb: "bug", xvc: "layers", screen: "terminal" };

function ApplyConfirm() {
  const ref = useRef(null);
  const close = () => { U.confirm = null; changed(); };
  useEscape(ref, close);
  const c = U.confirm;
  const screens = c.busy.filter((b) => b.kind === "screen");
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) close(); }}>
    <div class="modal small" role="alertdialog" aria-modal="true" aria-labelledby="apply-title" ref=${ref}
      data-testid="apply-confirm">
      <div class="modal-head"><${Icon} name="refresh-cw" /><h2 class="card-title" id="apply-title">Restart to update to ${c.version}?</h2></div>
      <div class="modal-pad">
        <p>The restart ends ${c.busy.length === 1 ? "this session" : `these ${c.busy.length} sessions`}:</p>
        <ul class="busy-list" data-testid="apply-busy">${c.busy.map((b, i) => html`<li key=${i} data-kind=${b.kind}>
          <${Icon} name=${BUSY_ICONS[b.kind] || "info"} /><span>${b.detail}</span></li>`)}</ul>
        ${screens.length ? html`<p class="secondary small">Consoles keep their PTY paths (U4): re-run
          ${screens.map((b, i) => html`${i ? ", " : " "}<code key=${b.path}>screen ${b.path}</code>`)} after the restart.</p>` : null}
        <p class="secondary small">Running jobs finish first; hub leases are kept. GDB and Vivado reconnect to the same ports.</p>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-action="app-apply-cancel" onClick=${close}>Not now</button>
        <button type="button" class="btn primary" data-action="app-apply-confirm" data-autofocus
          onClick=${() => startApply(c.version, true)}><${Icon} name="refresh-cw" /> Restart anyway</button>
      </div>
    </div>
  </div>`;
}

function RestartOverlay() {
  const o = U.overlay;
  const left = Math.max(0, Math.ceil((o.deadline - Date.now()) / 1000));
  return html`<div class="modal-back overlay" data-testid="restart-overlay" role="alertdialog" aria-modal="true"
      aria-labelledby="restart-title">
    <div class="modal small restart">
      <div class="modal-pad center">
        <div class="restart-spin"><${Spinner} /></div>
        <h2 class="card-title" id="restart-title">Restarting Harness Manager${o.to ? ` to ${o.to}` : ""}</h2>
        <p class="secondary" data-testid="restart-state">${o.down ? "Reconnecting…" : "Reconnecting… the service is stopping"}</p>
        <p class="restart-count num" data-testid="restart-countdown">${o.late
          ? "Taking longer than expected; still trying."
          : html`${left} s`}</p>
        <p class="secondary small">Same address, same session. The page reloads by itself when the new version answers;
          if it does not come up, the old one is restored.</p>
        ${o.late ? html`<p><button type="button" class="btn sm" data-action="restart-reload"
          onClick=${reloadPage}><${Icon} name="refresh-cw" /> Reload now</button></p>` : null}
      </div>
    </div>
  </div>`;
}

// --- Settings: the Updates card -------------------------------------------------------------------

async function putSettings(field, value) {
  U.settingsBusy = field;
  U.settingsError = null;
  changed();
  const r = await timed(`update settings --${field} ${value || '""'}`, () => call("updateSettingsSet", {}, { [field]: value }));
  U.settingsBusy = "";
  if (r.error) {
    U.settingsError = r.error;
  } else if (U.status) {
    const d = r.data.data;
    U.status = { ...U.status, settings: d.settings, effective: d.effective, policy: d.policy };
  }
  changed();
  loadAppUpdate();
}

function Choice({ label, testid, value, options, locked, onPick, busy }) {
  return html`<div class="field setting" data-testid=${testid}>
    <span class="field-label">${label}</span>
    <div class="seg" role="group" aria-label=${label}>${options.map((o) => {
      const off = locked(o.value);
      return html`<button type="button" key=${o.value} data-value=${o.value}
        aria-pressed=${value === o.value ? "true" : "false"} disabled=${!!off || !!busy}
        title=${off || o.title || o.label} onClick=${() => { if (value !== o.value) onPick(o.value); }}>${o.label}</button>`;
    })}</div>
  </div>`;
}

function nextCheck(st) {
  const eff = st.effective || {};
  if (st.dev_install) return "never: this is a developer install";
  if (eff.auto === "off") return `never: ${eff.why || "self-update is off"}`;
  if (!eff.check_interval_s) return "never: the policy's check_interval is 0";
  const lc = st.last_check;
  if (!lc || !lc.at) return "about a minute after the service starts, then every " + hours(eff.check_interval_s);
  if (lc.error) return `after a back-off (the last check failed), then every ${hours(eff.check_interval_s)}`;
  return `about ${clock(Number(lc.at) + Number(lc.interval_s || eff.check_interval_s))}, then every ${hours(eff.check_interval_s)}`;
}

function hours(s) {
  const n = Number(s || 0);
  return n >= 3600 ? `${+(n / 3600).toFixed(1)} h` : `${Math.round(n / 60)} min`;
}

function LastCheck({ st }) {
  const lc = st.last_check;
  if (!lc || !lc.at) return html`<span class="muted">not checked yet</span>`;
  const when = `${clock(lc.at)} (${ageText(lc.at)})`;
  if (lc.skipped) return html`${when}: <span class="muted">skipped: ${lc.skipped}</span>`;
  if (lc.error) {
    return html`${when}: <span class=${lc.error_kind === "refused" ? "i-err" : "muted"}>${lc.error_kind === "offline"
      ? `the source was unreachable (${lc.error})` : lc.error}</span>`;
  }
  return html`${when}: ${lc.channel ? html`<span class="mono">${lc.channel}</span>${lc.serial ? ` #${lc.serial}` : ""}, ` : ""}${lc.available
    ? html`<strong>${lc.available}</strong> offered${lc.staged ? " (downloaded)" : ""}` : "up to date"}${lc.skipped_bad
    ? html`<div class="sub">skipped ${lc.skipped_bad.version}: marked bad</div>` : null}`;
}

export function UpdatesCard() {
  const st = U.status;
  if (!st) {
    return html`<${Card} title="Updates" icon="rocket" testid="update-settings">
      ${U.unavailable ? html`<${Reason} icon="circle-slash" testid="update-settings-unavailable"
        text=${`App updates are unavailable here: ${U.unavailable}.`} />`
      : U.error ? html`<${Reason} level="err" text=${`${U.error.errName}: ${U.error.message}`} />`
      : html`<p class="muted"><${Spinner} /> Reading...</p>`}<//>`;
  }
  const policy = st.policy || {};
  const eff = st.effective || {};
  const settings = st.settings || {};
  const dev = st.dev_install;
  const pinned = policy.channel || "";
  const channel = pinned || settings.channel || eff.channel || "stable";
  const channels = CHANNELS.includes(channel) ? CHANNELS : [...CHANNELS, channel];
  const adminMax = policy.self_update || "stage";
  const lockChannel = (v) => (dev ? "a developer install never self-updates"
    : pinned && v !== pinned ? `${ADMIN} (${pinned} only)` : "");
  const lockMode = (v) => (dev ? "a developer install never self-updates"
    : MODE_RANK[v] > MODE_RANK[adminMax] ? `${ADMIN} (at most ${adminMax})` : "");
  const bad = Object.entries(st.bad || {});
  const ptr = st.pointer || {};
  const la = st.last_apply;
  return html`<${Card} title="Updates" icon="rocket" testid="update-settings"
      sub="Harness Manager checks its signed release channel, downloads a new version beside the one that runs, and switches only when you click Restart to update.">
    <div class="stack gap-12">
      <dl class="kv">
        <dt>Running</dt><dd><span class="mono" data-testid="settings-running">${st.running}</span>
          ${ptr.current ? html`<span class="sub"> · from versions/${ptr.current}</span>` : ptr.installer && ptr.installer.version ? html`<span class="sub"> · the installer's copy</span>` : null}</dd>
        ${(st.staged || []).length ? html`<dt>Ready</dt><dd data-testid="settings-staged">${st.staged.join(", ")}: restart to update (the banner above)</dd>` : null}
      </dl>
      ${dev ? html`<${Reason} level="unk" icon="circle-slash" testid="dev-install"
        text=${`Developer install: updates are off (${dev}). Update it with git.`} />` : null}
      ${policy.path ? html`<div class="policy-note" data-testid="policy-note"><${Icon} name="shield-check" />
        <div><strong>Your administrator's policy</strong> <code>${policy.path}</code> sets${" "}
          self-update <b>${policy.self_update}</b>${pinned ? html`, channel <b>${pinned}</b>` : ""}, a check every ${hours(policy.check_interval_s)}.
          ${" "}Controls it limits are disabled.
          ${(policy.problems || []).map((p) => html`<div class="sub" key=${p}>${p}</div>`)}</div></div>` : null}
      <${Choice} label="Channel" testid="set-channel" value=${channel} busy=${U.settingsBusy}
        options=${channels.map((c) => ({ value: c, label: c, title: c === "stable" ? "Releases david approved" : c === "beta" ? "Release candidates" : "Every build" }))}
        locked=${lockChannel} onPick=${(v) => putSettings("channel", v)} />
      <${Choice} label="Updates" testid="set-auto" value=${eff.auto || settings.auto || "stage"} busy=${U.settingsBusy}
        options=${MODES} locked=${lockMode} onPick=${(v) => putSettings("auto", v)} />
      <p class="secondary small" data-testid="mode-text">${MODE_TEXT[eff.auto || "stage"]}</p>
      ${eff.why && !dev ? html`<${Reason} level=${policy.self_update === "off" ? "warn" : ""} testid="effective-why" text=${`${eff.why}.`} />` : null}
      ${pinned || MODE_RANK[adminMax] < 2 ? html`<p class="secondary small" data-testid="policy-locked">Disabled choices are ${ADMIN}.</p>` : null}
      ${U.settingsError ? html`<${Reason} level="err" testid="settings-error"
        text=${`${U.settingsError.errName}: ${U.settingsError.message}${U.settingsError.hint ? ` (${U.settingsError.hint})` : ""}`} />` : null}
      <dl class="kv">
        <dt>Last check</dt><dd data-testid="last-check"><${LastCheck} st=${st} /></dd>
        <dt>Next check</dt><dd data-testid="next-check">${nextCheck(st)}</dd>
        ${la && la.id ? html`<dt>Last update</dt><dd data-testid="last-apply">${la.from} → ${la.to}: <b>${la.result}</b>${la.reason ? html`<div class="sub">${la.reason}</div>` : null}</dd>` : null}
      </dl>
      <div>
        <div class="field-label mb-6">Bad versions</div>
        ${bad.length ? html`<table class="table compact" data-testid="bad-versions"><thead><tr>
            <th>Version</th><th>Failed at</th><th>Why</th></tr></thead>
          <tbody>${bad.map(([v, b]) => html`<tr key=${v} data-version=${v}><td class="mono">${v}</td>
            <td>${b.phase || "?"}${b.at ? html`<div class="sub">${clock(b.at)}</div>` : null}</td>
            <td class="small-text">${b.reason || "its apply failed"}</td></tr>`)}</tbody></table>
          <p class="secondary small mt-8">A bad version is never offered again; a newer release is.</p>`
        : html`<p class="muted small" data-testid="bad-versions-none">None: no update has failed on this machine.</p>`}
      </div>
    </div>
  <//>`;
}

// SET-UI: openSettings("hubs") deep-links to a section; a click handler's event (or nothing)
// opens the section last used in this tab, the Updates section the first time (where the
// gear, its badge and the board's "Open settings" link have always led).
export function openSettings(section) {
  if (typeof section === "string" && SECTIONS.some((s) => s.id === section)) setSection(section);
  U.settingsOpen = true;
  U.settingsError = null;
  changed();
  loadAppUpdate();
  loadSettings();
}
whenOpen(() => U.settingsOpen);

function SettingsModal() {
  const ref = useRef(null);
  const close = () => { U.settingsOpen = false; changed(); };
  useEscape(ref, close);
  const current = SECTIONS.find((s) => s.id === SS.section);
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) close(); }}>
    <div class="modal settings" role="dialog" aria-modal="true" aria-labelledby="settings-title" ref=${ref}
      data-testid="settings">
      <div class="modal-head"><${Icon} name="sliders-horizontal" /><h2 class="card-title" id="settings-title">Settings</h2>
        <span class="muted small">this machine's Harness Manager</span><span class="grow"></span>
        ${SS.loading ? html`<${Spinner} />` : null}
        <button type="button" class="btn ghost sm icon-only" aria-label="Close settings" data-action="settings-close"
          data-autofocus onClick=${close}><${Icon} name="x" /></button></div>
      <div class="modal-body settings-body">
        <${SettingsNav} />
        <div class="modal-scroll settings-pane" data-testid="settings-pane" data-settings-section=${SS.section}>
          ${SS.section !== "updates" ? html`<h3 class="pane-title">${current ? current.label : SS.section}</h3>` : null}
          <${SettingsSectionBody} updatesCard=${html`<${UpdatesCard} />`} />
        </div>
      </div>
      <${RestartNote} cls="foot" />
    </div>
  </div>`;
}

export function SettingsButton() {
  const o = offer();
  const title = o ? `Settings: Harness Manager ${o.version} is ${o.kind === "staged" ? "ready" : "available"}` : "Settings: hubs, boards, tools, updates";
  return html`<button type="button" class="btn ghost sm icon-only settings-btn" data-action="settings" onClick=${openSettings}
    aria-label="Settings" title=${title}><${Icon} name="sliders-horizontal" />${o ? html`<span class="badge-dot" data-testid="settings-badge"></span>` : null}</button>`;
}

// The dialogs and the overlay, rendered once at the top of the app.
export function AppUpdateLayer() {
  return html`${U.settingsOpen ? html`<${SettingsModal} />` : null}
    ${U.confirm ? html`<${ApplyConfirm} />` : null}
    ${U.overlay ? html`<${RestartOverlay} />` : null}`;
}

// A compact status chip for the rail (Settings) and the board's Update page.
export function AppVersionChip() {
  const st = U.status;
  if (!st) return null;
  return html`<${Chip} icon="rocket" testid="app-version-chip">Harness Manager ${st.running}<//>`;
}
