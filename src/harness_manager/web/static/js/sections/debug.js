// Debug: Detect (IDCODE only), open and close the OpenOCD session, its ports and config;
// below it the partition's fabric debug over XVC (sections/xvc.js, lane XVC-UI).

import { panelState, runJob } from "../actions.js";
import { call, unwrapDebug } from "../api.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, loadDebug } from "../store.js";
import { ActionRow, Card, Chip, CopyButton, Reason, ResultBlock } from "../ui.js";
import { XvcCard } from "./xvc.js";

const LEVEL = { up: "ok", starting: "", down: "", failed: "err" };

// DEBUG-ONBOARD: where OpenOCD runs (the status's `where`; an older service has none: this PC).
export const WHERE_TEXT = {
  board: "OpenOCD runs on the board (gdb reaches it through the board's SSH)",
  host: "OpenOCD runs on this PC (on the board's JTAG port, 6921)",
};

// Each core's [name, local gdb port]: `gdb_ports`/`cores` (DEBUG-ONBOARD), else core 0's port.
export function gdbPorts(st) {
  if (!st) return [];
  const ports = (st.gdb_ports && st.gdb_ports.length) ? st.gdb_ports : (st.gdb_port ? [st.gdb_port] : []);
  return ports.map((port, i) => [(st.cores && st.cores[i]) || `cpu${i}`, port]);
}

export function gdbCmdFor(port) {
  return `arm-none-eabi-gdb -ex "set remotetimeout 60" -ex "target extended-remote 127.0.0.1:${port}"`;
}

function statusLines(st) {
  if (!st || typeof st !== "object") return [{ kind: "out", text: String(st) }];
  const out = [{ kind: st.state === "up" ? "ok" : "out", text: `state   ${st.state}` }];
  if (st.state === "up" || st.where === "board") {
    out.push({ kind: "out", text: `where   ${WHERE_TEXT[st.where || "host"] || st.where}` });
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

// The debug actions, shared by this section and the Overview's Debug tile (one panel).
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
      changed();
    },
  };
  const up = {
    key: "up", label: "Open session", busyLabel: "Opening...", budgetS: 60, command: "debug up",
    run: (ctx) => runJob("debugUp", { bid }, undefined, (d) => ctx.progress(`${d.phase || "starting"}`), "debug_up"),
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

export function DebugSection({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "debug");
  useEffect(() => { loadDebug(bid); }, [bid]);
  const st = b.debug || { state: "unknown" };
  const live = debugLive(bid);
  const { detect, up, down } = debugSpecs(bid);
  const gdbCmd = st.gdb_port ? `arm-none-eabi-gdb -ex "set remotetimeout 60" -ex "target extended-remote 127.0.0.1:${st.gdb_port}"` : "";
  const cores = live ? gdbPorts(st) : [];
  const onBoard = st.where === "board";
  return html`<div class="stack"><div class="grid split">
    <${Card} title="DUT debug (OpenOCD)" icon="bug" testid="debug-card"
        sub="Detect reads the TAP IDCODE only: no reset, no halt, no register written. Open starts OpenOCD with the config for the loaded design.">
      <div class="actions">
        <div class="field"><label>Session</label>
          <${Chip} level=${LEVEL[st.state] ?? "unk"} testid="debug-state">${st.state}<//>
          ${st.detail ? html`<span class="secondary small">${st.detail}</span>` : null}</div>
        <${ActionRow} bid=${bid} panel="debug" spec=${detect} icon="scan-search"
          gate=${{ capability: "debug_dut", holder: "Debug" }} />
        <${ActionRow} bid=${bid} panel="debug" spec=${up} variant="primary" icon="play"
          gate=${{ capability: "debug_dut", guard: () => (live ? "the session is already up" : ""), holder: "Debug" }} />
        <${ActionRow} bid=${bid} panel="debug" spec=${down} icon="square"
          gate=${{ guard: () => (live ? "" : "the session is down") }} />
        <${ResultBlock} lines=${p.lines} panel=${p} testid="debug-result"
          placeholder="gdb, telnet and tcl listen on 127.0.0.1 only." />
      </div>
    <//>
    <${Card} title="Connection" icon="cable" testid="debug-ports">
      <dl class="ports">
        <dt>IDCODE</dt><dd>${b.idcode ? html`<${Chip} level=${b.idcodeOk ? "ok" : "err"} cls="mono" testid="idcode">${b.idcode}<//>`
          : html`<span class="muted">not detected yet</span>`}</dd>
        <dt>OpenOCD</dt><dd data-testid="debug-where" data-where=${st.where || "host"}>${onBoard
          ? html`<${Chip} level="ok">on the board<//> <span class="secondary small">gdb reaches it through the board's SSH; no OpenOCD needed here</span>`
          : html`<span>on this PC</span> <span class="secondary small">${live ? "on the board's JTAG port (6921)" : "a claimed Linux board with mps3-debug runs its own (debug.on_board)"}</span>`}</dd>
        ${cores.length > 1 ? null : ["gdb"].map((k) => {
          const port = st[`${k}_port`];
          return html`<dt key=${`k${k}`}>${k}</dt><dd key=${`v${k}`} data-port=${k}>${live && port
            ? html`<span class="copy-row"><code>127.0.0.1:${port}</code><${CopyButton} text=${`127.0.0.1:${port}`} /></span>`
            : html`<span class="muted">-</span>`}</dd>`;
        })}
        ${cores.length > 1 ? cores.map(([core, port]) => html`<dt key=${`kg${core}`}>gdb ${core}</dt><dd key=${`vg${core}`} data-port=${`gdb-${core}`}>
            <span class="copy-row"><code>127.0.0.1:${port}</code><${CopyButton} text=${`127.0.0.1:${port}`} /></span></dd>`) : null}
        ${["telnet", "tcl"].map((k) => {
          const port = st[`${k}_port`];
          return html`<dt key=${`k${k}`}>${k}</dt><dd key=${`v${k}`} data-port=${k}>${live && port
            ? html`<span class="copy-row"><code>127.0.0.1:${port}</code><${CopyButton} text=${`127.0.0.1:${port}`} /></span>`
            : html`<span class="muted">${onBoard ? "on the board only" : "-"}</span>`}</dd>`;
        })}
        ${cores.length > 1
          ? cores.map(([core, port]) => html`<dt key=${`ka${core}`}>Attach ${core}</dt><dd key=${`va${core}`} data-testid=${`debug-attach-${core}`}>
              <span class="copy-row"><code>${gdbCmdFor(port)}</code><${CopyButton} text=${gdbCmdFor(port)} /></span></dd>`)
          : html`<dt>Attach</dt><dd>${gdbCmd ? html`<span class="copy-row"><code>${gdbCmd}</code><${CopyButton} text=${gdbCmd} /></span>`
          : html`<span class="muted">open a session first</span>`}</dd>`}
        <dt>Config</dt><dd class="mono small">${st.config && st.config.length ? st.config.join(" ") : html`<span class="muted">-</span>`}</dd>
        <dt>Process</dt><dd class="mono small">${st.pid ? `pid ${st.pid}` : html`<span class="muted">-</span>`}</dd>
      </dl>
      <div class="mt-14"><${Reason} text="The session closes by itself before a partition swap (closed for the swap), and reopens after a verified one." /></div>
    <//>
  </div>
  <${XvcCard} bid=${bid} /></div>`;
}
