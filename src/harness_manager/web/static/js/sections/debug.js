// The Workbench rail's Debug card (UI v2, docs/design/ui-v2/prototype-b-round3.html "DebugCard";
// plan §1.3 W14): Open session (OpenOCD for the loaded design) -> the gdb line to copy, one per
// core; Detect reads the TAP IDCODE only (no reset, no halt); Close session.
//
// DebugStatus's DEBUG-ONBOARD fields (additive; tolerated when absent, as the API says: an older
// reply reads as `gdb_ports: [gdb_port]`, `cores: ["cpu0"]`, `where: "host"`):
// - `gdb_ports`: each core's LOCAL gdb port (127.0.0.1), core order; `cores`: their names;
// - `where`: "board" (OpenOCD runs on the harness; gdb reaches it through the claim's SSH
//   forward; telnet and Tcl stay on the board) or "host" (this PC's OpenOCD, on 6921).
//
// Lease gating (R3): Open session and Detect go through gateReason with `holder` (actions.js).
// The Logic analysers card (sections/xvc.js) sits under it in the rail.

import { gateReason, interlock, panelState, runAction, runJob } from "../actions.js";
import { ENDPOINTS, call, toApiError, unwrapDebug } from "../api.js";
import { clock } from "../format.js";
import { html, useEffect, useState } from "../lib.js";
import { boardState, changed, loadDebug } from "../store.js";
import { CopyButton, Icon, ResultBlock, Spinner } from "../ui.js";
import { holderOnly } from "../week.js";

const PANEL = "debug";
const LEVEL = { up: "ok", starting: "accent", down: "", failed: "err", unknown: "unk" };

// DEBUG-ONBOARD: where OpenOCD runs (the status's `where`; an older service has none: this PC).
export const WHERE_TEXT = {
  board: "OpenOCD runs on the board (gdb reaches it through the board's SSH)",
  host: "OpenOCD runs on this PC (on the board's JTAG port, 6921)",
};

// HM 1.2.1 (preview): "Debug via": JTAG, or the DUT's hostio pins (memory and load only). The
// status carries `via` ("jtag" when absent), `capabilities` (the full JTAG set when absent) and
// `gdb_init` (the commands gdb gets before `target extended-remote`; hostio needs the two
// p-packet ones). Halt, step, registers and breakpoints are JTAG-only: on hostio they show
// disabled, with HOSTIO_LIMITS beside them.
export const VIA_LABEL = { jtag: "JTAG", hostio: "hostio (memory only, preview)" };
export const HOSTIO_LIMITS = "hostio debug reads and writes memory and loads images; halt, step, registers and breakpoints need JTAG";
export const HOSTIO_MODE_GATE = "the DUT's pins are in console mode: press \"Switch to hostio4\" (Consoles), then load the design again";
export const HOSTIO_PENDING_GATE = "hostio4 takes effect at the next design load: load the design again";
export const HOSTIO_NO_SUPPORT_GATE = "this board has no hostio support";
export const JTAG_CAPS = ["mem_read", "mem_write", "load", "halt", "step", "breakpoints", "registers", "reset"];
export const CAP_CONTROLS = [["halt", "Halt"], ["step", "Step"], ["registers", "Registers"], ["breakpoints", "Breakpoints"]];

export function capsOf(st) {
  return Array.isArray(st && st.capabilities) ? st.capabilities : JTAG_CAPS;
}

// Why "hostio" cannot be chosen now ("" when it can), from the hostio view (GET .../hostio).
export function hostioGate(view, failed, design) {
  if (failed) return HOSTIO_NO_SUPPORT_GATE;
  // the board's launcher says the running design has no hostio link (absent/null = unknown: allowed)
  if (design && design.hostio === false) return `${design.design} has no hostio`;
  if (!view) return "";
  if (view.mode === "hostio4") return "";
  return view.pending === "hostio4" ? HOSTIO_PENDING_GATE : HOSTIO_MODE_GATE;
}

// Each core's [name, local gdb port]: `gdb_ports`/`cores` (DEBUG-ONBOARD), else core 0's port.
export function gdbPorts(st) {
  if (!st) return [];
  const ports = (Array.isArray(st.gdb_ports) && st.gdb_ports.length) ? st.gdb_ports : (st.gdb_port ? [st.gdb_port] : []);
  return ports.filter((p) => p).map((port, i) => [(Array.isArray(st.cores) && st.cores[i]) || `cpu${i}`, port]);
}

// The gdb line `debug up` prints (services/debug.py gdb_command): pastes into sh, cmd and PowerShell.
export function gdbCmdFor(port) {
  return `arm-none-eabi-gdb -ex "set remotetimeout 60" -ex "target extended-remote 127.0.0.1:${port}"`;
}

// HM 1.2.1: the same line with the session's `gdb_init` commands (hostio: the two p-packet
// ones) before the connect, as services/debug.py gdb_command(gdb_init=...) builds it.
export function gdbCmdWith(port, st) {
  const init = ((st && Array.isArray(st.gdb_init)) ? st.gdb_init : []).map((c) => `-ex "${c}" `).join("");
  return gdbCmdFor(port).replace('-ex "target extended', `${init}-ex "target extended`);
}

function statusLines(st) {
  if (!st || typeof st !== "object") return [{ kind: "out", text: String(st) }];
  const out = [{ kind: st.state === "up" ? "ok" : "out", text: `state   ${st.state}` }];
  if (st.state === "up" || st.where === "board") {
    out.push({ kind: "out", text: `where   ${WHERE_TEXT[st.where || "host"] || st.where}` });
  }
  if (st.state === "up" || st.state === "starting") {
    out.push({ kind: "out", text: `via     ${VIA_LABEL[st.via || "jtag"] || st.via}` });
  }
  if (st.state === "up") {
    const cores = gdbPorts(st);
    for (const [core, port] of cores) {
      out.push({ kind: "out", text: `gdb     127.0.0.1:${port}${cores.length > 1 ? `  ${core}` : ""}` });
    }
    if (st.telnet_port) out.push({ kind: "out", text: `telnet  127.0.0.1:${st.telnet_port}` });
    if (st.tcl_port) out.push({ kind: "out", text: `tcl     127.0.0.1:${st.tcl_port}` });
  }
  if (st.config && st.config.length) out.push({ kind: "out", text: `config  ${st.config.join(" ")}` });
  if (st.pid) out.push({ kind: "out", text: `pid     ${st.pid}` });
  if (st.detail) out.push({ kind: "out", text: `detail  ${st.detail}` });
  return out;
}

// The debug actions, shared by this card and the Overview's Debug tile (one panel).
export function debugSpecs(bid) {
  const b = boardState(bid);
  const keep = (ok, value) => {
    if (ok && value && typeof value === "object" && value.state) {
      b.debug = unwrapDebug(value);
      b.debugAt = performance.now();
      changed();
    } else {
      loadDebug(bid);
    }
  };
  const detect = {
    key: "detect", label: "Detect", busyLabel: "Detecting...", budgetS: 45, command: "debug detect",
    run: async () => (await call("debugDetect", { bid })).data.idcode,
    render: (idcode) => [{ kind: "ok", text: `TAP IDCODE ${idcode}` },
      { kind: "hint", text: "read without reset or halt: nothing on the target changed" }],
    onDone: (ok, value) => {
      b.idcode = ok ? String(value) : `${value.errName}`;
      b.idcodeOk = ok;
      b.idcodeAt = Date.now() / 1000;
      changed();
    },
  };
  const up = {
    key: "up", label: "Open session", busyLabel: "Opening...", budgetS: 60,
    command: b.debugVia === "hostio" ? "debug up --via hostio" : "debug up",
    // JTAG sends no body (unchanged); hostio sends {via: "hostio"}
    run: (ctx) => runJob("debugUp", { bid }, b.debugVia === "hostio" ? { via: "hostio" } : undefined,
      (d) => ctx.progress(`${d.phase || "starting"}`), "debug_up"),
    render: statusLines, onDone: keep,
  };
  const down = {
    key: "down", label: "Close session", busyLabel: "Closing...", budgetS: 30, command: "debug down",
    run: async () => unwrapDebug((await call("debugDown", { bid })).data),
    render: statusLines, onDone: keep,
  };
  return { detect, up, down };
}

export function debugLive(bid) {
  const st = boardState(bid).debug || { state: "unknown" };
  return st.state === "up" || st.state === "starting";
}

// The gate each debug action uses (FIX-PACK-4: `holder`, the lease holder only on a hub board).
export function debugGates(bid) {
  const live = debugLive(bid);
  return {
    detect: { capability: "debug_dut", holder: "Debug" },
    up: { capability: "debug_dut", guard: () => (live ? "the session is already up" : ""), holder: "Debug" },
    down: { guard: () => (live ? "" : "the session is down") },
  };
}

// A rail button: disabled with the gate's reason in its title (the card shows one reason line).
function RailButton({ bid, spec, gate, variant = "", icon, title = "" }) {
  const p = panelState(bid, PANEL);
  const why = gateReason(bid, PANEL, spec.key, gate);
  const running = p.running === spec.key;
  const blocked = !!why && !running;
  // FIX-PACK-4: primary unless the lease is what blocks it
  const notYours = blocked && !!gate.holder && !!holderOnly(bid, gate.holder);
  const look = notYours && variant.includes("primary") ? variant.replace("primary", "").trim() : variant;
  const onClick = () => {
    if (running) return;
    if (why) { interlock(bid, PANEL, spec.command, why); return; }
    runAction(bid, PANEL, spec);
  };
  return html`<button type="button" class=${`btn sm ${look}`} data-action=${spec.key}
      aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined}
      title=${blocked ? why : title || undefined} onClick=${onClick}>
    ${running ? html`<${Spinner} />` : html`<${Icon} name=${icon} />`}${running ? spec.busyLabel : spec.label}</button>`;
}

// The reasons, one visible line each (the lease, a running job, a missing capability); a key
// whose reason repeats one already shown, or only says the session's state, is there for
// screen readers (and the tests) but not drawn twice.
function Reasons({ bid, keys, gates }) {
  const shown = new Set();
  return keys.map((k) => {
    const why = gateReason(bid, PANEL, k, gates[k]);
    if (!why || why === "running") return null;
    const quiet = why.startsWith("the session is") || shown.has(why);
    shown.add(why);
    if (quiet) return html`<span key=${k} class="sr-only" data-testid=${`reason-${k}`}>${why}</span>`;
    const held = /lease holder only/.test(why);
    return html`<p key=${k} class=${`reason ${held ? "held" : ""}`} data-testid=${`reason-${k}`}>
      <${Icon} name=${held ? "lock" : why.startsWith("Cannot:") ? "circle-slash" : "info"} /><span>${why}</span></p>`;
  });
}

// "$ debug up  (rc 0, 0.8 s)": the head of a command's lines.
function rcText(l) {
  if (!l || l.kind !== "rc") return "command";
  if (l.notRun) return `$ ${l.command}  (not run)`;
  if (l.running) return `$ ${l.command}  (running)`;
  const rc = l.rc === null || l.rc === undefined ? "no answer" : `rc ${l.rc}`;
  return `$ ${l.command}  (${rc}, ${Number.isFinite(l.secs) ? l.secs.toFixed(1) : "?"} s)`;
}

// A rail card's command lines, folded under their first line so both cards fit the rail at
// 1440x900; a failure or a refusal opens them (the reason is what to read next).
export function CommandLines({ lines, panel, testid }) {
  const shown = (lines || []).filter((l) => l.kind !== "progress");
  if (!shown.length) return null;
  const head = shown[0];
  const bad = !!head.notRun || head.level === "err" || shown.some((l) => l.kind === "err");
  return html`<details class=${`rail-cmd ${bad ? "bad" : ""}`} open=${bad || (panel && !!panel.running)}>
    <summary title="The command this card ran, and what it said"><${Icon} name="chevron-right" cls="sm chev" /><span class="mono">${rcText(head)}</span></summary>
    <${ResultBlock} lines=${shown} panel=${panel} testid=${testid} /></details>`;
}

// The gdb line as the rail shows it: the part that matters (its port, "127.0.0.1:3343") on
// screen, the whole command (what Copy copies: gdb -ex "target extended-remote ...") for the
// title, screen readers and the tests.
function GdbLine({ port, st }) {
  const cmd = gdbCmdWith(port, st);
  return html`<span class="copy-row"><code title=${cmd}>127.0.0.1:${port}</code>
    <span class="sr-only">${cmd}</span><${CopyButton} text=${cmd} label="Copy the gdb command" /></span>`;
}

export function DebugCard({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, PANEL);
  useEffect(() => { loadDebug(bid); }, [bid]);
  // HM 1.2.1 (preview): the DUT pins' mode decides whether "hostio" can be chosen
  const [hview, setHview] = useState(null);
  const [hfail, setHfail] = useState(false);
  useEffect(() => {
    if (!ENDPOINTS.hostioGet) { setHfail(true); return; }
    call("hostioGet", { bid }).then(({ data }) => { setHview(data); setHfail(false); })
      .catch((e) => { toApiError(e); setHfail(true); });
  }, [bid]);
  const st = b.debug || { state: "unknown" };
  const live = debugLive(bid);
  const { detect, up, down } = debugSpecs(bid);
  const gates = debugGates(bid);
  const state = p.running === "up" && st.state !== "up" ? "starting" : st.state;
  const cores = st.state === "up" ? gdbPorts(st) : [];
  const onBoard = st.where === "board";
  const idcode = b.idcode ? html`<span class=${`mono ${b.idcodeOk ? "" : "i-err"}`} data-testid="idcode">${b.idcode}</span>
      ${b.idcodeOk && !live && b.idcodeAt ? html` <span class="sub">detected ${clock(b.idcodeAt).slice(0, 5)}, no halt</span>` : null}` : null;
  const port = (k) => {
    const v = st[`${k}_port`];
    return live && v ? html`<span class="mono">${k} ${v}</span>`
      : html`<span class="muted">${k} ${onBoard ? "on the board only" : "-"}</span>`;
  };
  const whereLine = st.where || live
    ? html`<dt>OpenOCD</dt><dd data-testid="debug-where" data-where=${st.where || "host"} title=${WHERE_TEXT[st.where || "host"]}>
        ${onBoard ? html`<span class="chip ok">on the board</span>` : html`<span>on this PC</span>`}</dd>` : null;
  const chip = html`<span class=${`chip ${LEVEL[state] ?? "unk"}`} data-testid="debug-state" title=${st.detail || ""}>
    ${state === "starting" ? html`<${Spinner} />` : state === "up" ? html`<${Icon} name="circle-check" />` : null}${state === "starting" ? "opening" : state}</span>`;
  let body;
  if (st.state === "up") {
    body = html`<dl class="kv" data-testid="debug-ports">
      ${idcode ? html`<dt>IDCODE</dt><dd>${idcode}</dd>` : null}
      ${cores.length > 1 ? cores.map(([core, gp]) => html`<dt key=${`k${core}`}>gdb ${core}</dt>
          <dd key=${`v${core}`} data-port=${`gdb-${core}`} data-testid=${`debug-attach-${core}`}><${GdbLine} port=${gp} st=${st} /></dd>`)
        : cores.length ? html`<dt>gdb</dt><dd data-port="gdb" data-testid="debug-attach"><${GdbLine} port=${cores[0][1]} st=${st} /></dd>`
        : html`<dt>gdb</dt><dd class="muted small" data-port="gdb">${st.detail || "not forwarded here: Close and Open again"}</dd>`}
      <dt>Ports</dt><dd class="small"><span data-port="telnet">${port("telnet")}</span> · <span data-port="tcl">${port("tcl")}</span></dd>
      ${whereLine}
    </dl>`;
  } else {
    body = html`<dl class="kv" data-testid="debug-ports">
      ${idcode ? html`<dt>IDCODE</dt><dd>${idcode}</dd>` : null}
      ${whereLine}
    </dl>
    <div class="small secondary" data-testid="debug-note">${st.detail && st.state !== "unknown" ? st.detail
      : "Open session starts OpenOCD for the loaded design and gives you the gdb line."}</div>`;
  }
  const lines = p.lines.filter((l) => l.kind !== "progress");
  const via = live ? (st.via || "jtag") : (b.debugVia || "jtag");
  const caps = capsOf(st);
  const why = hostioGate(hview, hfail, st.hostio_design);
  const pick = (v) => { if (v === "hostio" && why) return; b.debugVia = v; changed(); };
  const chooser = !live ? html`<div class="seg" role="radiogroup" aria-label="Debug via" data-testid="debug-via">
      ${["jtag", "hostio"].map((v) => {
        const off = v === "hostio" && !!why;
        return html`<button type="button" key=${v} class=${`btn sm ${via === v ? "primary" : "ghost"}`} role="radio"
          aria-checked=${via === v ? "true" : "false"} aria-disabled=${off ? "true" : undefined} data-via=${v}
          title=${off ? why : undefined} onClick=${() => pick(v)}>${VIA_LABEL[v]}</button>`;
      })}</div>
    ${why ? html`<p class="small muted" data-testid="debug-via-reason">hostio: ${why}</p>` : null}`
    : html`<div class="small" data-testid="debug-via-live">Via <span class="chip plain" data-via=${via}>${VIA_LABEL[via] || via}</span></div>`;
  const capRow = (live || via === "hostio") ? html`<div class="row small" data-testid="debug-caps" data-via=${via}>
      ${CAP_CONTROLS.map(([k, label]) => {
        const on = (live ? caps : (via === "hostio" ? [] : JTAG_CAPS)).includes(k);
        return html`<span key=${k} class=${`chip ${on ? "ok" : "plain"}`} data-cap=${k} data-enabled=${on ? "true" : "false"}
          aria-disabled=${on ? undefined : "true"} title=${on ? undefined : HOSTIO_LIMITS}>${label}</span>`;
      })}
      ${(live ? via === "hostio" : via === "hostio") ? html`<span class="small muted" data-testid="debug-caps-reason">${HOSTIO_LIMITS}</span>` : null}
    </div>` : null;
  return html`<section class="card wb-debug" data-testid="debug-card" aria-label="Debug">
    <div class="card-head"><h2 class="card-title"><${Icon} name="bug" />Debug</h2><span class="spacer"></span>${chip}</div>
    <div class="card-body"><div class="stack tight">
      ${chooser}
      ${body}
      ${capRow}
      <div class="row">
        ${live ? html`<${RailButton} bid=${bid} spec=${down} gate=${gates.down} icon="square" />`
          : html`<${RailButton} bid=${bid} spec=${up} gate=${gates.up} variant="primary" icon="play"
              title="Start OpenOCD for the loaded design; gdb connects to it" />`}
        <${RailButton} bid=${bid} spec=${detect} gate=${gates.detect} variant="ghost" icon="scan-search"
          title="Reads the TAP IDCODE only: no reset, no halt" />
        <span class="small muted">A swap closes it.</span>
      </div>
      <${Reasons} bid=${bid} keys=${["up", "down", "detect"]} gates=${gates} />
      <${CommandLines} lines=${lines} panel=${p} testid="debug-result" />
    </div></div>
  </section>`;
}
