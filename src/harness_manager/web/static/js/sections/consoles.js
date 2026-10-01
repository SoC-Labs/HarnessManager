// The Workbench's console card (UI v2, docs/design/ui-v2/prototype-b-round3.html "ConsoleCard";
// plan §1.3 W8-W13): one card, a switcher over every console the board has, Reset DUT (armed)
// in its toolbar, then the console you are on: its state, who may type, its route and rate,
// Attach (screen, or an export to TCP), Save and Clear, the terminal and a send line.
//
// The switcher lists what GET /consoles lists, named by each row's `role` (G1b):
// - `dut` (uart0, uart1, swo): the DUT consoles;
// - `linux-root` (tty_02 on a Linux harness, a ROOT shell): "Harness console"; only the lease
//   holder types there (the row's `writable` / `read_only_reason`, and the socket's `input`
//   frame, JS/consoles.js);
// - `shell` (the same lane on bare metal): "Shell console";
// - `mcc` (only a scripted board lists one: Harness Manager never opens tty_00 as a console) and
//   `lane` (another FPGA UART lane) as they are named.
// The MCC log (G1a, a transcript of tty_00) is v0.2.1: not here.
//
// Each console keeps its own scrollback (JS/consoles.js keeps one session per console, outside
// the component tree); a count on the switcher shows the lines another console received since
// it was last on screen (only for a console this page has opened: nothing opens a console you
// did not ask for).
//
// Reset DUT goes through gateReason with `holder` (R3): on a hub board only this Harness Manager
// holding the lease resets the DUT. It switches to the DUT console, which shows the boot banner.

import { gateReason, interlock, isArmed, panelState, runAction, setArmed } from "../actions.js";
import { call } from "../api.js";
import { baudWhy, capState, clock, shortScreen } from "../format.js";
import { consoleSession, existingSession } from "../consoles.js";
import { html, useEffect, useRef, useState } from "../lib.js";
import { boardState, changed, loadConsoles, log, onBoardEvent, timed, toast } from "../store.js";
import { consoleRows, loadBaud, loadPty, openPty, setBaud, week } from "../week.js";
import { CopyButton, Icon, Spinner } from "../ui.js";
import { settingValue } from "../prefs.js";

const CONSOLE_CAPS = ["console_dut", "console_shell", "console_controller"];
const ENDINGS = { LF: "\n", CR: "\r", CRLF: "\r\n" };
// FIX-PACK-4: the send line's default is the setting consoles.line_ending (prefs.js).
const ENDING_OF = { lf: "LF", cr: "CR", crlf: "CRLF" };
export function defaultEnding() {
  return ENDING_OF[String(settingValue("consoles.line_ending")).toLowerCase()] || "CRLF";
}
// QUIET-POLL: "paused" waits for the board's lease (someone else holds it): calm, not a fault.
const STATE_LEVEL = { up: "ok", connecting: "accent", down: "warn", closed: "", paused: "held" };
const STATE_DOT = { up: "ok", connecting: "accent", down: "warn", closed: "idle", paused: "held" };

// --- the switcher's rows ------------------------------------------------------------------------

const DUT_NAMES = { uart0: ["DUT console", "DUT"], uart1: ["DUT uart1", "uart1"], swo: ["DUT SWO", "SWO"] };
const ROLE_ORDER = { dut: 0, "linux-root": 1, shell: 2, lane: 3, mcc: 4, "": 5 };

function roleOf(name, meta) {
  if (meta && typeof meta.role === "string" && meta.role) return meta.role;
  if (DUT_NAMES[name]) return "dut";
  if (name === "mcc") return "mcc";
  if (name === "shell" || name === "fpga_uart2") return "shell";
  if (/^fpga_uart/.test(name)) return "lane";
  return "";
}

// [{name, role, label, short, icon, meta, aka}] in switcher order (DUT consoles first).
export function consoleList(bid) {
  const b = boardState(bid);
  const rows = consoleRows(bid, b.consoles || []);
  const out = rows.map(({ name, meta, aka }) => {
    const role = roleOf(name, meta);
    let label = name;
    let short = name;
    let icon = "terminal";
    if (role === "dut") [label, short] = DUT_NAMES[name] || [`DUT ${name}`, name];
    if (role === "dut") icon = "radio";
    if (role === "linux-root") { label = "Harness console"; short = "Harness"; icon = "terminal"; }
    if (role === "shell") { label = "Shell console"; short = "Shell"; icon = "server"; }
    if (role === "mcc") { label = "MCC console"; short = "MCC"; icon = "server"; }
    return { name, role, label, short, icon, meta, aka };
  });
  const order = (r) => ROLE_ORDER[r.role] ?? 5;
  return out.sort((x, y) => order(x) - order(y));
}

// Who may type on this console: the socket's latest `input` frame, else the row's (G1b), else
// yes (a daemon from before G1b: no rule to show).
export function access(bid, c) {
  const s = existingSession(bid, c.name);
  if (s && s.input) return { writable: s.input.writable, reason: s.input.read_only_reason || "" };
  const m = c.meta || {};
  if (m.writable === false) return { writable: false, reason: m.read_only_reason || "read-only here" };
  return { writable: true, reason: "" };
}

function selectedConsole(bid, list) {
  const b = boardState(bid);
  const alias = ((week(bid).consoles || []).find((c) => c.name === b.consoleSelected) || {}).alias_of;
  const want = alias || b.consoleSelected;
  return list.find((c) => c.name === want) || list.find((c) => c.role === "dut") || list[0] || null;
}

// A console not on screen re-renders the switcher (its "new" count) at most twice a second.
let countTimer = 0;
function countChanged() {
  if (countTimer) return;
  countTimer = setTimeout(() => { countTimer = 0; changed(); }, 500);
}

// --- the terminal -----------------------------------------------------------------------------

function Terminal({ session, label }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    session.attach(el);
    const ro = new ResizeObserver(() => session.refit());
    ro.observe(el);
    return () => {
      ro.disconnect();
      session.detach();
    };
  }, [session]);
  return html`<div class="term-wrap" ref=${ref} data-testid="terminal" role="log" aria-label=${label}></div>`;
}

// A dim line into the page's own copy of the terminal (never sent to the board): a marker for
// a reset or a swap, as the prototype's "--- 12:04:05 DUT reset by you ---".
function mark(bid, name, text) {
  const s = existingSession(bid, name);
  if (!s || !s.opened) return;
  s.term.write(`\r\n\x1b[2m--- ${clock()} ${text} ---\x1b[0m\r\n`);
}

// A swap resets the DUT: its consoles say so where it happened.
onBoardEvent((ev) => {
  if (ev.topic !== "deploy.started" || !ev.board_id) return;
  const d = ev.data || {};
  for (const c of consoleList(ev.board_id)) {
    if (c.role === "dut") mark(ev.board_id, c.name, `partition swap: ${d.overlay || d.rm_id || "a design"}`);
  }
});

// --- the rate --------------------------------------------------------------------------------

const SOURCE_TEXT = { serial: "the host serial port", design: "the loaded design", harness: "the harness", unknown: "unknown" };

// The console's rate: a selector when it can change, else the rate and why not.
function BaudControl({ bid, name, onResult }) {
  const w = week(bid);
  const v = w.baud[name];
  if (w.baudUnsupported || !v) return null;
  const change = async (e) => {
    const baud = Number(e.target.value);
    const t0 = performance.now();
    const command = `console ${name} baud ${baud}`;
    try {
      const r = await setBaud(bid, name, baud);
      const took = ((performance.now() - t0) / 1000).toFixed(1);
      onResult({ level: "ok", text: `$ ${command}  (rc 0, ${took} s)\nnow ${r.baud} baud (set by ${SOURCE_TEXT[r.source] || r.source || "?"})` });
      log("info", "console", `${command}: now ${r.baud}`, bid);
    } catch (err) {
      const took = ((performance.now() - t0) / 1000).toFixed(1);
      onResult({ level: "err", text: `$ ${command}  (rc ${err.code ?? "?"}, ${took} s)\n${err.errName}  ${err.message}${err.hint ? `\nhint: ${err.hint}` : ""}` });
      log("error", "console", `${command}: ${err.message}`, bid);
      loadBaud(bid, name);
    }
  };
  if (v.settable) {
    return html`<span class="baud-ctl" data-testid="baud" data-settable="yes">
      <select class="select sm" id=${`baud-${name}`} value=${String(v.baud)} onChange=${change}
        aria-label=${`Baud rate of ${name}`} title=${`the rate, set on ${SOURCE_TEXT[v.source] || v.source}`}>
        ${(v.choices || []).map((c) => html`<option key=${c} value=${String(c)}>${c}</option>`)}
      </select><span class="muted small">baud</span></span>`;
  }
  const why = baudWhy(v);
  const tip = [v.reason, v.cite ? `(${v.cite})` : "", `source: ${SOURCE_TEXT[v.source] || v.source}`].filter(Boolean).join(" ");
  return html`<span class="baud-ctl" data-testid="baud" data-settable="no" title=${tip}>
    ${v.baud ? html`<span class="num">${v.baud}</span> baud` : "no rate"}${why ? html`<span class="why"> · ${why}</span>` : null}</span>`;
}

// --- screen and the TCP export ------------------------------------------------------------------

const EXCLUSIVE = "one terminal at a time: screen holds the PTY exclusively";

// The daemon's screen line, verbatim (copy it whole; the display shortens the path only).
export function ScreenCommand({ pty, compact = false }) {
  const cmd = pty.command;
  if (!cmd) return html`<span class="muted small"><${Spinner} /> reading the screen command...</span>`;
  const clients = pty.clients !== undefined && pty.clients !== null
    ? html`<span class="muted small nowrap" data-testid="screen-clients">${pty.clients} attached</span>` : null;
  return html`<span class="copy-row screen-row" data-testid="screen-command"
      title=${`Run this in any terminal; ${EXCLUSIVE}. screen shares the console with this page (Ctrl-A K ends it).`}>
    <code title=${cmd}>${shortScreen(cmd)}</code><${CopyButton} text=${cmd} />
    ${compact ? (pty.clients ? clients : null) : clients}
    ${compact ? null : html`<span class="muted small" data-testid="screen-exclusive">${EXCLUSIVE}</span>`}</span>`;
}

async function attachPty(bid, name, onResult) {
  const w = week(bid);
  const t0 = performance.now();
  const command = `console ${name} pty`;
  try {
    const r = await openPty(bid, name);
    const took = ((performance.now() - t0) / 1000).toFixed(1);
    onResult({ level: "ok", text: `$ ${command}  (rc 0, ${took} s)\n${r.command}` });
    log("info", "console", `${command}: ${r.path} -> ${r.device}`, bid);
  } catch (e) {
    w.ptyError[name] = e;
    changed();
    const took = ((performance.now() - t0) / 1000).toFixed(1);
    onResult({ level: "err", text: `$ ${command}  (rc ${e.code ?? "?"}, ${took} s)\n${e.errName}  ${e.message}${e.hint ? `\nhint: ${e.hint}` : ""}` });
    log("warning", "console", `${command}: ${e.message}`, bid);
  }
}

function AttachRow({ bid, name, onResult }) {
  const w = week(bid);
  const pty = w.pty[name];
  const err = w.ptyError[name];
  const [exported, setExported] = useState(null);
  const [exporting, setExporting] = useState(false);
  const doExport = async () => {
    setExporting(true);
    const r = await timed(`console ${name} export`, () => call("consoleExport", { bid, name }, {}));
    setExporting(false);
    if (r.error) {
      onResult({ level: "err", text: `${r.line}\n${r.error.errName}  ${r.error.message}${r.error.hint ? `\nhint: ${r.error.hint}` : ""}` });
      log("error", "console", `${r.line}  ${r.error.message}`, bid);
      return;
    }
    setExported(r.data.data.port);
    onResult({ level: "ok", text: `${r.line}\non 127.0.0.1:${r.data.data.port}` });
    log("info", "console", `${r.line}: 127.0.0.1:${r.data.data.port}`, bid);
  };
  let screen;
  if (w.ptyUnsupported) {
    screen = html`<span class="muted small">This harness-manager-daemon has no PTYs for screen: export over TCP.</span>`;
  } else if (pty && pty.path) {
    screen = html`<${ScreenCommand} pty=${pty} />`;
  } else if (err) {
    screen = html`<span class="screen-row" data-testid="screen-unavailable">
      <span class="reason"><${Icon} name="circle-slash" /><span>No screen here: ${err.reason || err.message}</span></span>
      <button type="button" class="btn sm primary" onClick=${doExport}><${Icon} name="external-link" /> Export to TCP instead</button></span>`;
  } else {
    screen = html`<span class="muted small"><${Spinner} /> making this console's PTY...</span>`;
  }
  return html`<div class="attach-row" data-testid="attach-row">
    <span class="secondary nowrap">Attach with screen</span>
    ${screen}
    ${exported ? html`<span class="copy-row"><code>telnet 127.0.0.1 ${exported}</code>
      <${CopyButton} text=${`telnet 127.0.0.1 ${exported}`} /></span>` : null}
    <button type="button" class="btn sm ghost" data-action="export" onClick=${doExport} aria-busy=${exporting ? "true" : undefined}
      title="Re-export this console on a local TCP port for an external terminal">
      ${exporting ? html`<${Spinner} />` : html`<${Icon} name="external-link" />`} Export to TCP</button>
  </div>`;
}

// --- Reset DUT ----------------------------------------------------------------------------------

const RESET_ARM = "reset_dut";
const RESET_PANEL = "reset_dut";
// FIX-PACK-4: `holder`: on a hub board, the lease holder only (actions.js gateReason).
export const RESET_DUT_GATE = { capability: "reset_dut", adapter: "resets", arm: RESET_ARM, holder: "Reset DUT" };

function resetSpec(bid, dut) {
  return {
    key: "reset_dut", label: "Reset DUT", busyLabel: "Resetting...", budgetS: 20,
    command: "reset dut",
    run: async () => { await call("reset", { bid }, { target: "dut" }); return "reset dut: done"; },
    render: (t) => [{ kind: "ok", text: t }],
    onDone: (ok) => {
      if (!ok) return;
      if (dut) mark(bid, dut, "DUT reset from this page");
      toast(`DUT reset on ${bid.split("@").pop()}: the DUT console shows the boot`, { icon: "rotate-ccw" });
    },
  };
}

function ResetGroup({ bid, dut }) {
  const b = boardState(bid);
  const p = panelState(bid, RESET_PANEL);
  const spec = resetSpec(bid, dut);
  const why = gateReason(bid, RESET_PANEL, spec.key, RESET_DUT_GATE);
  const running = p.running === spec.key;
  const armed = isArmed(bid, RESET_ARM);
  // Arm itself is refused for what Arm cannot fix: the lease, a running job, no capability.
  const armWhy = why === "running" ? "" : why.startsWith("not armed") ? "" : why;
  const blocked = !!why && !running;
  const onClick = () => {
    if (running) return;
    if (why) {
      interlock(bid, RESET_PANEL, spec.command, why);
      return;
    }
    if (dut) { b.consoleSelected = dut; changed(); }
    runAction(bid, RESET_PANEL, { ...spec, arm: RESET_ARM });
  };
  return html`<span class="reset-group">
    <label class=${`arm-inline ${armed ? "armed" : ""} ${armWhy ? "dis" : ""}`} data-testid="arm-reset-dut"
        title="Arm, then Reset DUT: resets the DUT only (the harness and the consoles stay)">
      <input type="checkbox" checked=${armed} disabled=${!!armWhy} aria-label="Arm Reset DUT"
        onChange=${(e) => setArmed(bid, RESET_ARM, e.target.checked)} />
      <${Icon} name=${armed ? "lock-open" : "lock"} />Arm</label>
    <button type="button" class="btn sm" data-action="reset_dut" aria-disabled=${blocked ? "true" : undefined}
        aria-busy=${running ? "true" : undefined} onClick=${onClick}
        title=${blocked ? why : "Reset the DUT: switches to the DUT console, which shows the boot"}>
      ${running ? html`<${Spinner} />` : html`<${Icon} name="rotate-ccw" />`}${running ? "Resetting..." : "Reset DUT"}</button>
  </span>`;
}

// --- the card -----------------------------------------------------------------------------------

function ConsoleCard({ bid, list, c }) {
  const b = boardState(bid);
  const session = consoleSession(bid, c.name);
  const [line, setLine] = useState("");
  const [picked, setEnding] = useState(null);         // null: the setting's (prefs.js)
  const [result, setResult] = useState(null);
  const [attach, setAttach] = useState(false);
  const ending = picked || defaultEnding();
  useEffect(() => { loadBaud(bid, c.name); loadPty(bid, c.name); }, [bid, c.name]);
  // The console on screen is seen; the others count what they receive.
  session.markSeen();
  session.onLines = null;
  for (const x of list) {
    const s = existingSession(bid, x.name);
    if (s && x.name !== c.name) s.onLines = countChanged;
  }
  const acc = access(bid, c);
  const st = session.state;
  // Many consoles: the ones not on screen go by their short names, so Reset DUT keeps its row.
  const compact = list.length > 3;
  const w = week(bid);
  const meta = c.meta || {};
  const dut = (list.find((x) => x.role === "dut") || {}).name || "";
  const resetP = panelState(bid, RESET_PANEL);
  const resetWhy = gateReason(bid, RESET_PANEL, "reset_dut", RESET_DUT_GATE);
  const resetHeld = resetWhy && resetWhy !== "running" && !resetWhy.startsWith("not armed");
  const resetFail = resetP.lines.length && !resetP.running && resetP.lines[0].kind === "rc"
    && (resetP.lines[0].notRun || resetP.lines[0].level === "err")
    ? resetP.lines.slice(1).map((l) => (l.name ? `${l.name}: ${l.text}` : l.text)).join("  ") : "";
  const send = (e) => {
    e.preventDefault();
    const command = `console ${c.name} send ${JSON.stringify(line)}`;
    if (!acc.writable) {
      setResult({ level: "warn", text: `$ ${command}  (not run)\nCannot send: ${acc.reason}. Nothing was run.` });
      return;
    }
    if (!line) {
      setResult({ level: "warn", text: `$ ${command}  (not run)\nNothing to send: the line is empty. Nothing was run.` });
      return;
    }
    const why = session.send(new TextEncoder().encode(line + ENDINGS[ending]));
    if (why) {
      setResult({ level: "warn", text: `$ ${command}  (not run)\nCannot send: ${why}. Nothing was run.` });
      log("warning", "console", `${command}: ${why}. Nothing was run.`, bid);
      return;
    }
    setResult({ level: "ok", text: `$ ${command}  (rc 0, 0.0 s)` });
    setLine("");
  };
  const save = () => {
    const blob = new Blob([`${session.text()}\n`], { type: "text/plain" });
    const a = document.createElement("a");
    const file = `${c.name}-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
    a.href = URL.createObjectURL(blob);
    a.download = file;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast(`Saved ${c.label} to ${file}`, { icon: "download" });
  };
  const toggleAttach = () => {
    const open = !attach;
    setAttach(open);
    // Attach shows the screen command: the PTY is made when the row opens (POST .../pty).
    if (open && !w.ptyUnsupported && !(w.pty[c.name] && w.pty[c.name].path) && !w.ptyError[c.name]) {
      attachPty(bid, c.name, setResult);
    }
  };
  const harness = c.role === "linux-root";
  const route = meta.kind === "serial" ? "serial" : meta.kind === "ethernet" ? "over Ethernet" : "";
  const accessChip = !acc.writable
    ? html`<span class="chip held" data-testid="console-access" title=${acc.reason}><${Icon} name="eye" />read-only</span>`
    : harness ? html`<span class="chip accent" data-testid="console-access" title="The Linux console is a root shell: only the lease holder types"><${Icon} name="keyboard" />you can type</span>`
    : null;
  let note = null;
  if (!acc.writable) {
    note = html`<p class="reason held" data-testid="console-readonly"><${Icon} name="eye" /><span>${
      /^read-only/i.test(acc.reason) ? acc.reason.charAt(0).toUpperCase() + acc.reason.slice(1) : `Read-only: ${acc.reason}`}</span></p>`;
  } else if (session.detail && (st === "down" || st === "paused" || st === "closed")) {
    note = html`<p class=${`reason ${st === "paused" ? "held" : "warn"}`}><${Icon} name=${st === "paused" ? "circle-pause" : "unplug"} /><span>${session.detail}</span></p>`;
  }
  const resetNote = c.role === "dut" && resetHeld
    ? html`<p class=${`reason ${/lease holder/.test(resetWhy) ? "held" : ""}`} data-testid="reason-reset_dut"><${Icon} name="lock" /><span>${resetWhy}</span></p>`
    : resetFail ? html`<p class="reason err" data-testid="reset-result"><${Icon} name="circle-x" /><span>Reset DUT: ${resetFail}</span></p>` : null;
  return html`<section class="card console-card" aria-label=${`Console ${c.name}`} data-testid=${`console-${c.name}`}>
    <div class="console-bar cs-bar">
      <div class="cs-seg" role="tablist" aria-label=${`Consoles on ${bid}`}>
        ${list.map((x) => {
          const s = existingSession(bid, x.name);
          const xs = s ? s.state : "";
          const xa = access(bid, x);
          const n = x.name === c.name || !s ? 0 : s.unseen();
          return html`<button type="button" role="tab" key=${x.name} class="cs-btn" data-console-tab=${x.name}
              aria-selected=${x.name === c.name ? "true" : "false"}
              title=${`${x.label} (${x.name}${x.aka.length ? `, also ${x.aka.join(", ")}` : ""})${xs ? `: ${xs}` : ": not opened yet"}${xa.writable ? "" : `, read-only: ${xa.reason}`}`}
              onClick=${() => { b.consoleSelected = x.name; changed(); }}>
            <span class=${`dot ${xs ? STATE_DOT[xs] || "idle" : "off"}`}></span>${compact && x.name !== c.name
              ? html`<span>${x.short}</span>` : html`<span class="cs-long">${x.label}</span><span class="cs-short">${x.short}</span>`}
            ${xa.writable ? null : html`<${Icon} name="eye" />`}
            ${n ? html`<span class="cs-new" title=${`${n} new line${n > 1 ? "s" : ""}`}>${n > 99 ? "99+" : n}</span>` : null}</button>`;
        })}
      </div>
      <span class="grow"></span>
      <${ResetGroup} bid=${bid} dut=${dut} />
    </div>
    <div class="cs-info">
      <span class="cs-now" title=${`${c.label}: ${c.name}`}><${Icon} name=${c.icon} />${c.label}</span>
      <span class=${`chip ${STATE_LEVEL[st] ?? "unk"}`} data-testid="console-state" data-level=${STATE_LEVEL[st] ?? "unk"}
        title=${session.detail || ""}>${st}</span>
      ${accessChip}
      <span class="small muted csub" data-testid="console-detail">
        <span class="mono">${c.name}</span>${route ? ` · ${route}` : ""}${harness ? " · root shell" : ""}
        ${" · "}<${BaudControl} bid=${bid} name=${c.name} onResult=${setResult} />
        ${session.dropped ? html`${" "}<span class="i-warn" data-testid="console-dropped">${session.dropped} bytes dropped: the page fell behind</span>` : null}</span>
      <span class="cs-tools">
        ${st === "down" || st === "closed" ? html`<button type="button" class="btn ghost sm" data-action="reconnect"
          onClick=${() => session.connect()} title="Connect this console again"><${Icon} name="refresh-cw" />Reconnect</button>` : null}
        <button type="button" class="btn ghost icon-only sm" data-action="attach-screen" aria-pressed=${attach ? "true" : "false"}
          onClick=${toggleAttach} title="Attach with screen, or export to TCP"><${Icon} name="external-link" /></button>
        <button type="button" class="btn ghost icon-only sm" data-action="save" onClick=${save}
          title="Save this console's scrollback to a file"><${Icon} name="download" /></button>
        <button type="button" class="btn ghost icon-only sm" data-action="clear"
          onClick=${() => { session.clear(); session.markSeen(); changed(); }}
          title="Clear this console's screen (the others keep theirs; the board is not touched)"><${Icon} name="trash-2" /></button>
      </span>
    </div>
    ${note || resetNote ? html`<div class="console-note">${note}${resetNote}</div>` : null}
    ${attach ? html`<${AttachRow} bid=${bid} name=${c.name} onResult=${setResult} />` : null}
    <${Terminal} key=${c.name} session=${session} label=${c.label} />
    <form class="console-send" onSubmit=${send}>
      <input class="input mono" data-testid="send-line" aria-label=${`Send a line to ${c.name}`} disabled=${!acc.writable}
        placeholder=${!acc.writable ? `read-only: ${acc.reason.replace(/^read-only:\s*/i, "")}`
          : harness ? `a command for the root shell on ${bid.split("@").pop()}; Enter sends it` : `a line for ${c.name}; Enter sends it`}
        value=${acc.writable ? line : ""} onInput=${(e) => setLine(e.target.value)} />
      <select class="select" aria-label="Line ending" data-testid="send-ending" value=${ending} disabled=${!acc.writable}
        title="What Enter sends (the default is Settings > Consoles > What Enter sends)" onChange=${(e) => setEnding(e.target.value)}>
        ${Object.keys(ENDINGS).map((k) => html`<option key=${k} value=${k}>${k}</option>`)}
      </select>
      <button type="submit" class="btn" data-action="send" aria-disabled=${acc.writable ? undefined : "true"}
        title=${acc.writable ? "" : acc.reason}><${Icon} name="send" /> Send</button>
    </form>
    ${result ? html`<div class="console-foot"><div class=${`result ${result.level || ""}`} data-testid="console-result">${result.text}</div></div>` : null}
  </section>`;
}

// The console part of the Workbench: the card, or why there is none.
export function ConsolesSection({ bid }) {
  const b = boardState(bid);
  useEffect(() => {
    if (!b.consoles && !b.consolesError) loadConsoles(bid);
  }, [bid]);
  const states = CONSOLE_CAPS.map((c) => capState(b.info, c));
  const none = states.every((s) => s && !s.available);
  const empty = (body) => html`<section class="card console-card console-empty" data-testid="console-none">
    <div class="card-body">${body}</div></section>`;
  if (none) {
    return empty(html`<p class="reason"><${Icon} name="circle-slash" /><span>Cannot open a console: ${states.map((s) => s.reason).join("; ")}</span></p>`);
  }
  if (b.consolesError) {
    return empty(html`<p class="reason err"><${Icon} name="circle-x" /><span>${b.consolesLine}: ${b.consolesError.errName}: ${b.consolesError.message}</span></p>
      <p class="mt-12"><button type="button" class="btn sm" onClick=${() => { b.consolesError = null; loadConsoles(bid); }}>Try again</button></p>`);
  }
  if (!b.consoles) return empty(html`<p class="muted"><${Spinner} /> Asking the daemon for this board's consoles...</p>`);
  const list = consoleList(bid);
  if (!list.length) return empty(html`<p class="reason"><${Icon} name="info" /><span>The daemon reports no consoles for this board.</span></p>`);
  const c = selectedConsole(bid, list);
  return html`<${ConsoleCard} key=${c.name} bid=${bid} list=${list} c=${c} />`;
}
