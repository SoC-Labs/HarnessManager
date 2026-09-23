// Consoles: one tab per console, a live terminal, a send line, the baud (set here, like
// ConfPro-SX does for HAPS UARTs), "Attach with screen" (the daemon's PTY for the console,
// shared with this page), Export to TCP, Save.

import { call } from "../api.js";
import { baudWhy, capState, shortScreen } from "../format.js";
import { consoleSession } from "../consoles.js";
import { html, useEffect, useRef, useState } from "../lib.js";
import { boardState, changed, loadConsoles, log, timed } from "../store.js";
import { consoleRows, loadBaud, loadPty, openPty, setBaud, week } from "../week.js";
import { Chip, CopyButton, Icon, Reason, Spinner } from "../ui.js";

const CONSOLE_CAPS = ["console_dut", "console_shell", "console_controller"];
const ENDINGS = { LF: "\n", CR: "\r", CRLF: "\r\n" };
const STATE_LEVEL = { up: "ok", connecting: "", down: "warn", closed: "" };

function Terminal({ session }) {
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
  return html`<div class="term-wrap" ref=${ref} data-testid="terminal"></div>`;
}

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
      <label class="muted small" for=${`baud-${name}`}>Baud</label>
      <select class="select sm" id=${`baud-${name}`} value=${String(v.baud)} onChange=${change}
        aria-label=${`Baud rate of ${name}`} title=${`set on ${SOURCE_TEXT[v.source] || v.source}`}>
        ${(v.choices || []).map((c) => html`<option key=${c} value=${String(c)}>${c}</option>`)}
      </select></span>`;
  }
  const why = baudWhy(v);
  const tip = [v.reason, v.cite ? `(${v.cite})` : "", `source: ${SOURCE_TEXT[v.source] || v.source}`].filter(Boolean).join(" ");
  return html`<span class="baud-ctl" data-testid="baud" data-settable="no" title=${tip}>
    <span class="muted small">Baud</span>
    <span class="num">${v.baud ?? "no rate"}</span>${why ? html`<span class="muted small why">· ${why}</span>` : null}</span>`;
}

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

// "Attach with screen": the daemon's PTY for this console (the page and screen share it).
function ScreenControl({ bid, name, onResult, onExport }) {
  const w = week(bid);
  const pty = w.pty[name];
  const err = w.ptyError[name];
  if (w.ptyUnsupported) return null;
  if (pty && pty.path) return html`<${ScreenCommand} pty=${pty} />`;
  if (err) {
    return html`<span class="screen-row" data-testid="screen-unavailable">
      <${Reason} icon="circle-slash" text=${`No screen here: ${err.reason || err.message}`} />
      <button type="button" class="btn sm primary" onClick=${onExport}><${Icon} name="external-link" /> Export to TCP instead</button></span>`;
  }
  const attach = async () => {
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
  };
  return html`<button type="button" class="btn sm" data-action="attach-screen" onClick=${attach}
    title="Create this console's PTY, then attach any terminal with screen">
    <${Icon} name="terminal" /> Attach with screen</button>`;
}

function ConsolePane({ bid, name }) {
  const session = consoleSession(bid, name);
  const b = boardState(bid);
  const [line, setLine] = useState("");
  const [ending, setEnding] = useState("CRLF");
  const [result, setResult] = useState(null);
  const [exported, setExported] = useState(null);
  const [exporting, setExporting] = useState(false);
  const send = (e) => {
    e.preventDefault();
    const command = `console ${name} send ${JSON.stringify(line)}`;
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
  const doExport = async () => {
    setExporting(true);
    const r = await timed(`console ${name} export`, () => call("consoleExport", { bid, name }, {}));
    setExporting(false);
    if (r.error) {
      setResult({ level: "err", text: `${r.line}\n${r.error.errName}  ${r.error.message}${r.error.hint ? `\nhint: ${r.error.hint}` : ""}` });
      log("error", "console", `${r.line}  ${r.error.message}`, bid);
      return;
    }
    setExported(r.data.data.port);
    setResult({ level: "ok", text: `${r.line}\non 127.0.0.1:${r.data.data.port}` });
    log("info", "console", `${r.line}: 127.0.0.1:${r.data.data.port}`, bid);
  };
  const save = () => {
    const blob = new Blob([session.text() + "\n"], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name}-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  const st = session.state;
  useEffect(() => { loadBaud(bid, name); loadPty(bid, name); }, [bid, name]);
  return html`<section class="card console-card" aria-label=${`Console ${name}`} data-testid=${`console-${name}`}>
    <div class="console-bar">
      <${Chip} level=${STATE_LEVEL[st] ?? "unk"} testid="console-state"
        icon=${st === "up" ? "radio" : st === "connecting" ? "loader-circle" : "unplug"}>${st}<//>
      <span class="grow secondary small" data-testid="console-detail">${session.detail || (st === "up" ? "Type into the terminal, or send a line below." : "")}
        ${session.dropped ? html`${" "}<span class="i-warn" data-testid="console-dropped">${session.dropped} bytes dropped: the page fell behind</span>` : null}</span>
      <button type="button" class="btn sm ghost" onClick=${save} title="Save the scrollback as a text file">
        <${Icon} name="download" /> Save</button>
      <button type="button" class="btn sm ghost" onClick=${() => { session.clear(); changed(); }}
        title="Clear the terminal (the board is not touched)"><${Icon} name="trash-2" /> Clear</button>
      ${st === "down" || st === "closed" ? html`<button type="button" class="btn sm"
        onClick=${() => session.connect()}><${Icon} name="refresh-cw" /> Reconnect</button>` : null}
    </div>
    <div class="console-meta">
      <${BaudControl} bid=${bid} name=${name} onResult=${setResult} />
      <span class="grow"></span>
      <${ScreenControl} bid=${bid} name=${name} onResult=${setResult} onExport=${doExport} />
      ${exported ? html`<span class="copy-row"><code>telnet 127.0.0.1 ${exported}</code>
        <${CopyButton} text=${`telnet 127.0.0.1 ${exported}`} /></span>` : null}
      <button type="button" class="btn sm ghost" data-action="export" onClick=${doExport} aria-busy=${exporting ? "true" : undefined}
        title="Re-export this console on a local TCP port for an external terminal">
        ${exporting ? html`<${Spinner} />` : html`<${Icon} name="external-link" />`} Export to TCP</button>
    </div>
    <${Terminal} session=${session} />
    <form class="console-send" onSubmit=${send}>
      <input class="input mono" placeholder=${`a line for ${name}; Enter sends it`} aria-label=${`Send a line to ${name}`}
        value=${line} onInput=${(e) => setLine(e.target.value)} data-testid="send-line" />
      <select class="select" aria-label="Line ending" value=${ending} onChange=${(e) => setEnding(e.target.value)}>
        ${Object.keys(ENDINGS).map((k) => html`<option key=${k} value=${k}>${k}</option>`)}
      </select>
      <button type="submit" class="btn" data-action="send"><${Icon} name="send" /> Send</button>
    </form>
    ${result ? html`<div class="console-foot"><div class="result" data-testid="console-result">${result.text}</div></div>` : null}
  </section>`;
}

export function ConsolesSection({ bid }) {
  const b = boardState(bid);
  useEffect(() => {
    if (!b.consoles && !b.consolesError) loadConsoles(bid);
  }, [bid]);
  const states = CONSOLE_CAPS.map((c) => capState(b.info, c));
  const none = states.every((s) => s && !s.available);
  if (none) {
    return html`<div class="card"><div class="card-body">
      <${Reason} icon="circle-slash" text=${`Cannot open a console: ${states.map((s) => s.reason).join("; ")}`} /></div></div>`;
  }
  if (b.consolesError) {
    return html`<div class="card"><div class="card-body">
      <${Reason} level="err" text=${`${b.consolesLine}: ${b.consolesError.errName}: ${b.consolesError.message}`} />
      <p class="mt-12"><button type="button" class="btn sm" onClick=${() => { b.consolesError = null; loadConsoles(bid); }}>Try again</button></p>
    </div></div>`;
  }
  if (!b.consoles) return html`<p class="muted"><${Spinner} /> Asking the daemon for this board's consoles...</p>`;
  if (!b.consoles.length) return html`<${Reason} text="The daemon reports no consoles for this board." />`;
  const rows = consoleRows(bid, b.consoles);
  const shown = rows.map((r) => r.name);
  const alias = ((week(bid).consoles || []).find((c) => c.name === b.consoleSelected) || {}).alias_of;
  const current = alias || (shown.includes(b.consoleSelected) ? b.consoleSelected : shown[0]);
  return html`
    <div class="console-tabs" role="tablist" aria-label="Consoles">
      ${rows.map(({ name: n, aka }) => {
        const s = consoleSession(bid, n);
        return html`<button type="button" role="tab" key=${n} class="console-tab" data-console-tab=${n}
          aria-selected=${n === current ? "true" : "false"} title=${aka.length ? `also called ${aka.join(", ")}` : undefined}
          onClick=${() => { b.consoleSelected = n; changed(); }}>
          <span class=${`dot ${s.state === "up" ? "ok" : s.state === "down" ? "warn" : "unk"}`}></span>${n}${aka.length ? html`<span class="muted small"> (${aka.join(", ")})</span>` : null}</button>`;
      })}
    </div>
    <${ConsolePane} key=${current} bid=${bid} name=${current} />`;
}
