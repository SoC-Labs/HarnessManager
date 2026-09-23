// Consoles: one tab per console, a live terminal, a send line, Export to TCP, Save.

import { call } from "../api.js";
import { capState } from "../format.js";
import { consoleSession } from "../consoles.js";
import { html, useEffect, useRef, useState } from "../lib.js";
import { boardState, changed, loadConsoles, log, timed } from "../store.js";
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
  return html`<section class="card console-card" aria-label=${`Console ${name}`} data-testid=${`console-${name}`}>
    <div class="console-bar">
      <${Chip} level=${STATE_LEVEL[st] ?? "unk"} testid="console-state"
        icon=${st === "up" ? "radio" : st === "connecting" ? "loader-circle" : "unplug"}>${st}<//>
      <span class="grow secondary small" data-testid="console-detail">${session.detail || (st === "up" ? "Type into the terminal, or send a line below." : "")}
        ${session.dropped ? html`${" "}<span class="i-warn" data-testid="console-dropped">${session.dropped} bytes dropped: the page fell behind</span>` : null}</span>
      ${exported ? html`<span class="copy-row"><code>telnet 127.0.0.1 ${exported}</code>
        <${CopyButton} text=${`telnet 127.0.0.1 ${exported}`} /></span>` : null}
      <button type="button" class="btn sm" onClick=${doExport} aria-busy=${exporting ? "true" : undefined}
        title="Re-export this console on a local TCP port for an external terminal">
        ${exporting ? html`<${Spinner} />` : html`<${Icon} name="external-link" />`} Export to TCP</button>
      <button type="button" class="btn sm ghost" onClick=${save} title="Save the scrollback as a text file">
        <${Icon} name="download" /> Save</button>
      <button type="button" class="btn sm ghost" onClick=${() => { session.clear(); changed(); }}
        title="Clear the terminal (the board is not touched)"><${Icon} name="trash-2" /> Clear</button>
      ${st === "down" || st === "closed" ? html`<button type="button" class="btn sm"
        onClick=${() => session.connect()}><${Icon} name="refresh-cw" /> Reconnect</button>` : null}
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
  const current = b.consoleSelected || b.consoles[0];
  return html`
    <div class="console-tabs" role="tablist" aria-label="Consoles">
      ${b.consoles.map((n) => {
        const s = consoleSession(bid, n);
        return html`<button type="button" role="tab" key=${n} class="console-tab" data-console-tab=${n}
          aria-selected=${n === current ? "true" : "false"}
          onClick=${() => { b.consoleSelected = n; changed(); }}>
          <span class=${`dot ${s.state === "up" ? "ok" : s.state === "down" ? "warn" : "unk"}`}></span>${n}</button>`;
      })}
    </div>
    <${ConsolePane} key=${current} bid=${bid} name=${current} />`;
}
