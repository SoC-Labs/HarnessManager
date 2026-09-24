// Overview: what needs attention (only when something is wrong), four action tiles
// (Design, Consoles, Debug, Board), and the Details, collapsed.
//
// The header above already shows the board, shell, design, harness and health, so nothing
// here repeats them: the tiles carry what a user does next.

import { panelState } from "../actions.js";
import { capState, valueText } from "../format.js";
import { existingSession } from "../consoles.js";
import { html, useEffect } from "../lib.js";
import {
  boardState, changed, loadConsoles, loadOverlays, loadTelemetry, refreshInfo, S, setSection,
} from "../store.js";
import { consoleRows, durationText, leaseLeft, openPty, week } from "../week.js";
import { ScreenCommand } from "./consoles.js";
import { CapabilitiesCard, HealthCard, IdentityCard, TelemetryCard } from "./details.js";
import { debugLive, debugSpecs } from "./debug.js";
import { ARM_TEXT, REBOOT_GATE, RESET_DUT_GATE, rebootSpec, resetDutSpec } from "./power.js";
import {
  ActionRow, ArmBox, Card, Chip, CopyButton, Icon, Reason, ResultBlock, Spinner,
} from "../ui.js";
import { leaseSpecs } from "../hub.js";
import { openRequestForm, requestActive } from "../lease.js";

// --- needs attention ----------------------------------------------------------------------

const HARNESS_TITLES = {
  busy: "The harness is busy", wedged: "The harness is wedged", offline: "The harness is offline",
  rescue: "The board is in stage0 rescue", unknown: "The harness state is unknown",
};

// One line per problem, each with its fix. Nothing when all is well.
export function attentionItems(bid) {
  const b = boardState(bid);
  const w = week(bid);
  const out = [];
  const info = b.info;
  const ident = (info && info.identity) || {};
  const go = (section, label) => ({ label, run: () => setSection(bid, section) });
  if (b.pending) {
    out.push({ key: "sd", level: "err", title: "An SD install was interrupted.",
      text: "The configuration SD is half-written; nothing else on this board should be trusted.",
      fix: go("sd", "Restore it first") });
  }
  if (b.infoError && b.infoError.errName !== "ABSENT") {
    out.push({ key: "read", level: "err", title: "The last read of the board failed.",
      text: `${b.infoError.message}${b.infoError.hint ? ` Fix: ${b.infoError.hint}` : ""}`,
      fix: { label: "Read again", run: () => refreshInfo(bid) } });
  }
  const h = info && info.health;
  if (h && !(b.infoError && b.infoError.errName !== "ABSENT")) {
    const cc = h.control_channel || "unknown";
    if (!h.reachable) {
      out.push({ key: "harness", level: "err", title: "The harness does not answer.",
        text: (h.notes || [])[0] || "Check the board's power, the Ethernet cable and its address.",
        fix: go("power", "Power") });
    } else if (cc !== "idle") {
      out.push({ key: "harness", level: cc === "busy" || cc === "rescue" ? "warn" : "err",
        title: `${HARNESS_TITLES[cc] || `The harness is ${cc}`}.`,
        text: (h.notes || []).join(" ") || "It does not report idle.",
        fix: cc === "busy" ? null : go("power", "Power") });
    }
  }
  if (info && ident.build_check === "unchecked") {
    out.push({ key: "build", level: "unk", title: "Build check unchecked.",
      text: "The harness cannot compare its firmware with the fabric (this mint's USR_ACCESS is unreadable), so a mismatch would not show. Fix: a harness that can, from the Update page.",
      fix: go("update", "Update") });
  } else if (info && ident.build_check === "mismatch") {
    out.push({ key: "build", level: "err", title: "Build check MISMATCH.",
      text: "The harness firmware was built for other fabric. Fix: reinstall the harness that matches this shell.",
      fix: go("update", "Update") });
  }
  const hub = w.hub;
  if (hub) {
    const t = hub.tunnel;
    if (t && t.state !== "up") {
      out.push({ key: "tunnel", level: t.state === "starting" ? "warn" : "err",
        title: `The SSH tunnel to ${t.host || hub.host} is ${t.state}.`,
        text: `${sentence(t.detail)} Fix: check that \`ssh ${t.host || hub.host}\` works from this machine (key, VPN).`.trim() });
    }
    const lease = hub.lease;
    const left = leaseLeft(lease);
    const specs = leaseSpecs(bid);
    if (!lease) {
      out.push({ key: "lease", level: "warn", title: `Not leased on ${hub.host}.`,
        text: "Another hub user can take this board at any time. Fix: acquire the lease.",
        action: specs.acquire });
    } else if (!lease.mine) {
      const asked = requestActive(bid);
      out.push({ key: "lease", level: "err", title: `Leased to ${lease.holder || "someone else"} on ${hub.host}.`,
        text: `This client must not drive the board until the lease is yours${left !== null ? ` (theirs ends in ${durationText(left)})` : ""}. ${asked
          ? "You have asked for it: the request is above."
          : "Fix: request it. The holder is asked; with no answer in 2 minutes you may force-release it."}`,
        fix: asked ? null : { label: "Request board", action: "lease_request_open", run: (e) => openRequestForm(bid, e && e.currentTarget) } });
    } else if (left !== null && left < 300) {
      out.push({ key: "lease", level: "warn", title: `Your lease ends in ${durationText(left)}.`,
        text: "The daemon renews it while the board is open; if this stays, the hub is not answering. Fix: renew it.",
        action: specs.acquire });
    }
  }
  return out;
}

// A daemon detail as a sentence: "…Connection refused" -> "…Connection refused."
function sentence(text) {
  const s = String(text || "").trim();
  return !s || /[.!?)]$/.test(s) ? s : `${s}.`;
}

const ATTENTION_ICONS = { err: "circle-x", unk: "circle-help", warn: "triangle-alert" };

function AttentionStrip({ bid }) {
  const items = attentionItems(bid);
  if (!items.length) return null;
  const pl = panelState(bid, "lease");
  return html`<section class="attention" aria-label="Needs attention" data-testid="attention">
    <h2 class="attention-title"><${Icon} name="triangle-alert" />Needs attention</h2>
    <ul>${items.map((it) => html`<li key=${it.key} class=${`att ${it.level}`} data-attention=${it.key}>
      <${Icon} name=${ATTENTION_ICONS[it.level] || "triangle-alert"} />
      <span class="att-text"><strong>${it.title}</strong>${" "}${it.text}</span>
      ${it.fix ? html`<button type="button" class="btn sm" data-action=${it.fix.action || undefined} onClick=${it.fix.run}>${it.fix.label}</button>` : null}
      ${it.action ? html`<${ActionRow} bid=${bid} panel="lease" spec=${it.action} compact=${true} gate=${{}} />` : null}
    </li>`)}</ul>
    ${pl.lines && pl.lines.length ? html`<${ResultBlock} lines=${pl.lines} panel=${pl} testid="lease-result" />` : null}
  </section>`;
}

// --- the tiles -----------------------------------------------------------------------------

function Tile({ title, icon, action = null, children, testid }) {
  return html`<section class="tile" aria-label=${title} data-testid=${testid}>
    <div class="tile-head"><h2 class="tile-title"><${Icon} name=${icon} />${title}</h2>
      <span class="spacer"></span>${action}</div>
    <div class="tile-body">${children}</div>
  </section>`;
}

function DesignTile({ bid }) {
  const b = boardState(bid);
  useEffect(() => { if (!b.overlays && !b.overlaysLoading) loadOverlays(bid); }, [bid]);
  const ident = (b.info && b.info.identity) || {};
  const d = b.deploy;
  const o = b.overlays;
  const loadable = o ? o.loadable.length : null;
  const program = html`<button type="button" class="btn primary sm" data-action="go-program"
    onClick=${() => setSection(bid, "program")}><${Icon} name="upload" /> Program...</button>`;
  return html`<${Tile} title="Design" icon="layers" action=${program} testid="tile-design">
    <div class="big-value" data-testid="tile-design-name">${ident.rm_name || "unknown design"}</div>
    <div class="mono secondary">${ident.rm_id ? `rm_id ${ident.rm_id}` : "rm_id not reported"}</div>
    ${loadable !== null ? html`<p class="secondary small mt-8">${loadable} design${loadable === 1 ? "" : "s"} load on this shell.</p>` : null}
    ${d.state === "running" ? html`<p class="small mt-8"><${Spinner} /> Programming ${d.overlay}: ${d.phase}</p>`
      : d.state === "done" ? html`<${Reason} level=${d.verified ? "ok" : "warn"} text=${`Last program: ${d.overlay || d.rm_id}, ${d.verified ? "verified by the board" : "written, not verified"}.`} />`
      : d.state === "failed" ? html`<${Reason} level="err" text=${`Last program failed: ${d.reason}`} />` : null}
  <//>`;
}

const DOT = { up: "ok", connecting: "unk", down: "warn", closed: "unk" };

function ConsolesTile({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  useEffect(() => { if (!b.consoles && !b.consolesError) loadConsoles(bid); }, [bid]);
  const caps = ["console_dut", "console_shell", "console_controller"].map((c) => capState(b.info, c));
  const none = caps.every((s) => s && !s.available);
  const open = (name) => { b.consoleSelected = name; setSection(bid, "consoles"); };
  const meta = Object.fromEntries((w.consoles || []).map((c) => [c.name, c]));
  const attach = async (name) => {
    try { await openPty(bid, name); } catch (e) { w.ptyError[name] = e; changed(); }
  };
  const openAll = html`<button type="button" class="btn sm" onClick=${() => setSection(bid, "consoles")}>
    <${Icon} name="terminal" /> Consoles</button>`;
  return html`<${Tile} title="Consoles" icon="terminal" action=${openAll} testid="tile-consoles">
    ${none ? html`<${Reason} icon="circle-slash" text=${`Cannot: ${caps[0].reason}`} />` : null}
    ${b.consolesError ? html`<${Reason} level="err" text=${`${b.consolesError.errName}: ${b.consolesError.message}`} />` : null}
    ${!none && !b.consoles && !b.consolesError ? html`<p class="muted"><${Spinner} /> Reading...</p>` : null}
    <ul class="tile-consoles">${consoleRows(bid, b.consoles).map(({ name, aka }) => {
      const s = existingSession(bid, name);
      const state = (s && s.state) || w.consoleState[name] || (meta[name] && meta[name].state) || "";
      const pty = w.pty[name];
      const baud = (w.baud[name] && w.baud[name].baud) ?? (meta[name] && meta[name].baud);
      return html`<li key=${name} data-console=${name}>
        <span class=${`dot ${DOT[state] || "unk"}`} title=${state || "not open in this page"}></span>
        <span class="mono cname" title=${aka.length ? `also called ${aka.join(", ")}` : undefined}>${name}</span>
        <span class="muted small num baud">${baud ? `${baud}` : ""}</span>
        <span class="screen">${pty && pty.path
          ? html`<${ScreenCommand} pty=${pty} compact=${true} />`
          : w.ptyUnsupported ? null
          : w.ptyError[name] ? html`<span class="muted small" title=${w.ptyError[name].message}>no screen here</span>`
          : html`<button type="button" class="btn ghost sm" data-action=${`attach-${name}`}
              onClick=${() => attach(name)} title="Create this console's PTY, then attach any terminal with screen">
              <${Icon} name="terminal" cls="sm" /> Attach with screen</button>`}</span>
        <button type="button" class="btn sm" data-action=${`open-${name}`} onClick=${() => open(name)}>Open</button>
      </li>`;
    })}</ul>
  <//>`;
}

function DebugTile({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "debug");
  const st = b.debug || { state: "unknown" };
  const live = debugLive(bid);
  const { up, down } = debugSpecs(bid);
  const start = { ...up, label: "Start", busyLabel: "Starting..." };
  const stop = { ...down, label: "Stop", busyLabel: "Stopping..." };
  const level = { up: "ok", starting: "", down: "", failed: "err" }[st.state] ?? "unk";
  return html`<${Tile} title="Debug" icon="bug" testid="tile-debug">
    <div class="row"><${Chip} level=${level} testid="tile-debug-state">${st.state}<//>
      ${b.idcode ? html`<span class="secondary small">IDCODE <span class="mono">${b.idcode}</span></span>` : null}</div>
    <div class="tile-kv mt-8">
      <span class="k">gdb</span>
      <span class="v">${live && st.gdb_port ? html`<span class="copy-row"><code>127.0.0.1:${st.gdb_port}</code>
        <${CopyButton} text=${`127.0.0.1:${st.gdb_port}`} /></span>` : html`<span class="muted">start a session to get a port</span>`}</span>
    </div>
    <div class="mt-8">${live
      ? html`<${ActionRow} bid=${bid} panel="debug" spec=${stop} icon="square" compact=${true} gate=${{}} />`
      : html`<${ActionRow} bid=${bid} panel="debug" spec=${start} variant="primary" icon="play" compact=${true}
          gate=${{ capability: "debug_dut" }} />`}</div>
    ${p.lines && p.lines.length && p.running ? html`<p class="muted small mt-8">${p.lines[p.lines.length - 1].text || ""}</p>` : null}
  <//>`;
}

function pickTemperature(readings) {
  const temps = (readings || []).filter((r) => r.unit === "degC");
  return temps.find((r) => r.value !== null && r.value !== undefined) || temps[0] || null;
}

function ReadingValue({ r, empty }) {
  if (!r) return html`<span class="muted">${empty}</span>`;
  const ok = r.value !== null && r.value !== undefined;
  return ok
    ? html`<span class="num">${valueText(r)}</span> <span class="muted small mono">${r.source || ""}</span>`
    : html`<span class="muted" title=${r.reason}>unavailable</span> <span class="muted small">${r.reason}</span>`;
}

function BoardTile({ bid }) {
  const b = boardState(bid);
  const readings = b.telemetry;
  const temp = pickTemperature(readings);
  const clk = (readings || []).find((r) => r.name === "dut_clk");
  const pr = panelState(bid, "reset_dut");
  const pb = panelState(bid, "reboot");
  const last = [pr, pb].filter((p) => p.lines && p.lines.length).sort((x, y) => y.startedAt - x.startedAt)[0];
  return html`<${Tile} title="Board" icon="circuit-board" testid="tile-board">
    <div class="tile-kv">
      <span class="k">Temperature</span><span class="v" data-testid="tile-temp"><${ReadingValue} r=${temp} empty=${readings ? "no sensor" : "reading..."} /></span>
      <span class="k">DUT clock</span><span class="v" data-testid="tile-clock"><${ReadingValue} r=${clk} empty=${readings ? "not reported" : "reading..."} /></span>
    </div>
    <div class="tile-actions">
      <${ActionRow} bid=${bid} panel="reset_dut" spec=${resetDutSpec(bid)} icon="rotate-ccw" compact=${true}
        gate=${RESET_DUT_GATE}><${ArmBox} bid=${bid} armKey="reset_dut" compact=${true} text=${ARM_TEXT.reset_dut} /><//>
      <${ActionRow} bid=${bid} panel="reboot" spec=${{ ...rebootSpec(bid), label: "Reboot" }} variant="danger" icon="power"
        compact=${true} gate=${REBOOT_GATE}><${ArmBox} bid=${bid} armKey="reboot" compact=${true} testid="tile-arm-reboot" text=${ARM_TEXT.reboot} /><//>
    </div>
    ${last ? html`<div class="mt-8"><${ResultBlock} lines=${last.lines} panel=${last} testid="tile-board-result" /></div>` : null}
  <//>`;
}

// --- details --------------------------------------------------------------------------------

function detailsOpen() {
  try { return window.sessionStorage.getItem("harness-manager.details") === "open"; } catch (e) { return false; }
}

function Details({ bid }) {
  const b = boardState(bid);
  if (S.detailsOpen === undefined) S.detailsOpen = detailsOpen();
  const toggle = () => {
    S.detailsOpen = !S.detailsOpen;
    try { window.sessionStorage.setItem("harness-manager.details", S.detailsOpen ? "open" : "closed"); } catch (e) { /* ok */ }
    changed();
  };
  return html`<section class="details" data-testid="details">
    <button type="button" class="details-toggle" aria-expanded=${S.detailsOpen ? "true" : "false"}
      data-action="details" onClick=${toggle}>
      <${Icon} name="chevron-right" cls=${`sm chev ${S.detailsOpen ? "open" : ""}`} />Details
      <span class="muted small">identity, health counters, telemetry, capabilities</span></button>
    ${S.detailsOpen ? html`<div class="grid split mt-14">
      <div class="stack">
        <${IdentityCard} info=${b.info} />
        <${TelemetryCard} bid=${bid} />
      </div>
      <div class="stack">
        <${HealthCard} bid=${bid} info=${b.info} />
        <${CapabilitiesCard} info=${b.info} />
      </div>
    </div>` : null}
  </section>`;
}

// SYSMON over JTAG takes ~3 s a read (docs/CONTRACTS.md), so telemetry is polled in the
// background, never awaited by anything else, and the last good values stay on screen.
const TELEMETRY_POLL_MS = 15000;
// Over a hub share an MCC read takes about 2 s (oscillators about 6 s): ask half as often.
const HUB_TELEMETRY_POLL_MS = 30000;

export function OverviewSection({ bid }) {
  const b = boardState(bid);
  useEffect(() => {
    const timer = setInterval(() => {
      const now = boardState(bid);
      const every = week(bid).hub ? HUB_TELEMETRY_POLL_MS : TELEMETRY_POLL_MS;
      if (document.visibilityState === "visible" && now.info && !now.job && !now.telemetryLoading
          && Date.now() - (now.telemetryAt || 0) >= every - 1000) {
        loadTelemetry(bid);
      }
    }, TELEMETRY_POLL_MS);
    return () => clearInterval(timer);
  }, [bid]);
  if (!b.info) {
    return html`<div class="card" data-testid="info-error"><div class="card-body">
      ${b.infoError ? html`<${Reason} level="err" text=${`${b.infoLine}: ${b.infoError.errName}: ${b.infoError.message}`} />
          ${b.infoError.hint ? html`<div class="mt-8"><${Reason} text=${b.infoError.hint} /></div>` : null}
          <p class="mt-14"><button type="button" class="btn sm" onClick=${() => refreshInfo(bid)}>
            <${Icon} name="refresh-cw" /> Read again</button></p>`
        : html`<p class="muted"><${Spinner} /> Reading the board...</p>`}</div></div>`;
  }
  return html`<div class="stack overview">
    <${AttentionStrip} bid=${bid} />
    <div class="tiles" data-testid="tiles">
      <${DesignTile} bid=${bid} />
      <${ConsolesTile} bid=${bid} />
      <${DebugTile} bid=${bid} />
      <${BoardTile} bid=${bid} />
    </div>
    <${Details} bid=${bid} />
  </div>`;
}

