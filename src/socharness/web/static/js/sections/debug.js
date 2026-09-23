// Debug: Detect (IDCODE only), open and close the OpenOCD session, its ports and config.

import { panelState, runJob } from "../actions.js";
import { call, unwrapDebug } from "../api.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, loadDebug } from "../store.js";
import { ActionRow, Card, Chip, CopyButton, Reason, ResultBlock } from "../ui.js";

const LEVEL = { up: "ok", starting: "", down: "", failed: "err" };

function statusLines(st) {
  if (!st || typeof st !== "object") return [{ kind: "out", text: String(st) }];
  const out = [{ kind: st.state === "up" ? "ok" : "out", text: `state   ${st.state}` }];
  if (st.state === "up") {
    out.push({ kind: "out", text: `gdb     127.0.0.1:${st.gdb_port}` });
    out.push({ kind: "out", text: `telnet  127.0.0.1:${st.telnet_port}` });
    out.push({ kind: "out", text: `tcl     127.0.0.1:${st.tcl_port}` });
  }
  if (st.config && st.config.length) out.push({ kind: "out", text: `config  ${st.config.join(" ")}` });
  if (st.pid) out.push({ kind: "out", text: `pid     ${st.pid}` });
  if (st.detail) out.push({ kind: "out", text: `detail  ${st.detail}` });
  return out;
}

export function DebugSection({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "debug");
  useEffect(() => { loadDebug(bid); }, [bid]);
  const st = b.debug || { state: "unknown" };
  const live = st.state === "up" || st.state === "starting";
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
  const gdbCmd = st.gdb_port ? `arm-none-eabi-gdb -ex 'target extended-remote :${st.gdb_port}'` : "";
  return html`<div class="grid split">
    <${Card} title="DUT debug (OpenOCD)" icon="bug" testid="debug-card"
        sub="Detect reads the TAP IDCODE only: no reset, no halt, no register written. Open starts OpenOCD with the config for the loaded design.">
      <div class="actions">
        <div class="field"><label>Session</label>
          <${Chip} level=${LEVEL[st.state] ?? "unk"} testid="debug-state">${st.state}<//>
          ${st.detail ? html`<span class="secondary small">${st.detail}</span>` : null}</div>
        <${ActionRow} bid=${bid} panel="debug" spec=${detect} icon="scan-search"
          gate=${{ capability: "debug_dut" }} />
        <${ActionRow} bid=${bid} panel="debug" spec=${up} variant="primary" icon="play"
          gate=${{ capability: "debug_dut", guard: () => (live ? "the session is already up" : "") }} />
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
        ${["gdb", "telnet", "tcl"].map((k) => {
          const port = st[`${k}_port`];
          return html`<dt key=${`k${k}`}>${k}</dt><dd key=${`v${k}`} data-port=${k}>${live && port
            ? html`<span class="copy-row"><code>127.0.0.1:${port}</code><${CopyButton} text=${`127.0.0.1:${port}`} /></span>`
            : html`<span class="muted">-</span>`}</dd>`;
        })}
        <dt>Attach</dt><dd>${gdbCmd ? html`<span class="copy-row"><code>${gdbCmd}</code><${CopyButton} text=${gdbCmd} /></span>`
          : html`<span class="muted">open a session first</span>`}</dd>
        <dt>Config</dt><dd class="mono small">${st.config && st.config.length ? st.config.join(" ") : html`<span class="muted">-</span>`}</dd>
        <dt>Process</dt><dd class="mono small">${st.pid ? `pid ${st.pid}` : html`<span class="muted">-</span>`}</dd>
      </dl>
      <div class="mt-14"><${Reason} text="The session closes by itself before a partition swap, and reopens only when you ask." /></div>
    <//>
  </div>`;
}
