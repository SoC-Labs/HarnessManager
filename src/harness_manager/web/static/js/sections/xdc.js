// Board & XDC (T10): constraint kits from the board pack's pin model.
//
// Pick a kit (the RM kit for the shell's partition, or the full-board export), pick a
// built-in design or paste one, preview the files next to every check, download a zip.
// Nothing here touches the board: the daemon builds the kit from the pin model and reads
// only the static the board runs (docs/API.md "XDC export").

import { ApiError, call, callBlob, toApiError } from "../api.js";
import { html, useEffect, useState } from "../lib.js";
import { Card, Chip, Icon, Reason, Seg, Spinner } from "../ui.js";

// A daemon from before the XDC routes answers GET /boards/{bid}/xdc from its greedy
// /boards/{bid} route: "<board>/xdc is not open" (404 ABSENT). Say what that means.
function explain(err) {
  const e = toApiError(err);
  if (e.status === 404 && /\/xdc\b/.test(e.message || "")) {
    return new ApiError({ name: "UNAVAILABLE", message: "this harness-manager-daemon has no XDC routes yet",
      hint: "update Harness Manager (the XDC export arrived with team T10)" }, 404);
  }
  return e;
}

async function xdcCall(name, params, body) {
  try {
    return (await call(name, params, body)).data;
  } catch (e) { throw explain(e); }
}

// --- state kept per board across re-renders ------------------------------------------------

const S = {};
function st(bid) {
  if (!S[bid]) {
    S[bid] = { cat: null, catError: null, kit: "rm-kit", design: "", custom: "", useCustom: false,
      busy: "", result: null, error: null, tab: "", downloaded: "" };
  }
  return S[bid];
}

const KIT_OPTIONS = [
  { value: "rm-kit", label: "RM kit", icon: "layers", title: "for a module that loads into the shell's partition" },
  { value: "board", label: "Full board", icon: "cpu", title: "for a whole-FPGA design (replaces the harness)" },
];

function levelOf(sev) { return sev === "error" ? "err" : "unk"; }

function Checks({ findings }) {
  if (!findings || !findings.length) {
    return html`<${Reason} level="ok" text="Every check passed." testid="xdc-checks-ok" />`;
  }
  const errors = findings.filter((f) => f.severity === "error");
  return html`<div data-testid="xdc-checks">
    <p class="sub-head">${errors.length ? `${errors.length} check${errors.length === 1 ? "" : "s"} failed` : "Every check passed"}
      ${findings.length - errors.length ? `, ${findings.length - errors.length} note${findings.length - errors.length === 1 ? "" : "s"}` : ""}</p>
    <ul class="caps-missing">${findings.map((f, i) => html`<li key=${i} data-check=${f.code} data-severity=${f.severity}>
      <div class="cap-title"><${Icon} name=${f.severity === "error" ? "circle-x" : "info"} cls=${`sm ${f.severity === "error" ? "i-err" : "i-muted"}`} />
        <${Chip} level=${levelOf(f.severity)}>${f.code}<//> <span class="mono">${f.subject}</span></div>
      <${Reason} level=${f.severity === "error" ? "err" : ""} text=${f.reason + (f.hint ? ` (${f.hint})` : "")} />
      ${f.src ? html`<p class="muted mono">${f.src}</p>` : null}
    </li>`)}</ul>
  </div>`;
}

function ModelCard({ cat, board }) {
  const m = cat.model || {};
  const s = m.status || {};
  const shell = (m.shells || {})[m.default_shell] || {};
  const t = shell.totals || {};
  return html`<${Card} title="Pin model" icon="file-code" testid="xdc-model"
      sub=${`${(m.board || {}).title || ""}, ${(m.board || {}).part || ""}`}>
    <dl class="kv">
      <dt>Model</dt><dd><div class="line">${s.derived ? html`<${Chip} level="warn" testid="xdc-derived">derived<//>` : html`<${Chip} level="ok">Lane C<//>`}
        <span>${s.label || ""}</span></div>
        <div class="sub mono">${s.generator || ""} from ${s.platform_ref || "?"} @ ${(s.platform_commit || "").slice(0, 10)}</div></dd>
      <dt>Shell</dt><dd><div class="line"><span class="mono">${m.default_shell}</span>${shell.fielded ? html`<${Chip} level="ok">fielded<//>` : null}</div>
        <div class="sub">${t.ports} ports, ${t.bits} bits${t.decoupler_intfs ? `, ${t.decoupler_intfs} decoupler interfaces` : ""}</div></dd>
      <dt>This board</dt><dd data-testid="xdc-board-static">${board && board.static_id
        ? html`<div class="line"><span class="mono">${board.static_id}</span>
            ${board.matches ? html`<${Chip} level="ok">matches the model<//>` : html`<${Chip} level="err">different static<//>`}</div>
            ${board.reason ? html`<div class="sub">${board.reason}</div>` : null}`
        : html`<span class="muted">${(board && board.reason) || "unknown"}</span>`}</dd>
    </dl>
  <//>`;
}

export function BoardXdcSection({ bid }) {
  const [, force] = useState(0);
  const redraw = () => force((n) => n + 1);
  const x = st(bid);

  useEffect(() => {
    if (x.cat || x.catError) return;
    xdcCall("boardXdc", { bid }).then((cat) => { x.cat = cat; redraw(); })
      .catch((e) => { x.catError = explain(e); redraw(); });
  }, [bid]);

  if (x.catError) {
    return html`<${Card} title="Board & XDC" icon="file-code" testid="xdc-card">
      <${Reason} level="err" testid="xdc-unavailable" text=${`${x.catError.errName}: ${x.catError.message}${x.catError.hint ? ` (${x.catError.hint})` : ""}`} />
    <//>`;
  }
  if (!x.cat) return html`<${Card} title="Board & XDC" icon="file-code" testid="xdc-card"><p class="muted"><${Spinner} /> Reading the pin model...</p><//>`;

  const designs = (x.cat.designs || []).filter((d) => d.kit === x.kit);
  const chosen = x.design && designs.some((d) => d.name === x.design) ? x.design
    : ((x.cat.default_design || {})[x.kit] || (designs[0] || {}).name || "");

  function designArg() {
    if (!x.useCustom) return chosen;
    try { return JSON.parse(x.custom); } catch (e) { throw new ApiError({ name: "USAGE", message: `the design is not JSON: ${e.message}` }, 400); }
  }

  async function preview() {
    x.busy = "preview"; x.error = null; x.downloaded = ""; redraw();
    try {
      x.result = await xdcCall("boardXdcExport", { bid }, { kit: x.kit, design: designArg(), preview: true });
      const names = Object.keys(x.result.files || {}).sort();
      // open on the constraint file itself: the OOC XDC, or the pins file
      x.tab = names.find((n) => /_(ooc|pins)\.xdc$/.test(n)) || names[0] || "";
    } catch (e) { x.error = toApiError(e); x.result = null; }
    x.busy = ""; redraw();
  }

  async function download() {
    x.busy = "zip"; x.error = null; redraw();
    try {
      const blob = await callBlob("boardXdcExport", { bid }, { kit: x.kit, design: designArg(), format: "zip" });
      const name = `${x.useCustom ? "design" : chosen}_${x.kit}.zip`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      x.downloaded = name;
    } catch (e) {
      x.error = toApiError(e);
      const checks = x.error.data && x.error.data.checks;
      if (checks) x.result = Object.assign({}, x.result || {}, { checks, passed: false });
    }
    x.busy = ""; redraw();
  }

  const r = x.result;
  const files = r && r.files ? Object.keys(r.files).sort() : [];
  const errors = r && r.checks ? r.checks.filter((f) => f.severity === "error").length : 0;
  return html`<div class="grid split" data-testid="xdc-card">
    <div class="actions">
      <${ModelCard} cat=${x.cat} board=${x.cat.board} />
      <${Card} title="Export" icon="download" testid="xdc-export"
          sub=${x.kit === "rm-kit" ? "OOC XDC by boundary group, connectivity sheet, pblock facts, wrapper skeleton."
            : "Pins, IO standards by bank, and clocks, for a whole-FPGA design."}>
        <div class="actions">
          <${Seg} label="Kit" value=${x.kit} options=${KIT_OPTIONS}
            onChange=${(v) => { x.kit = v; x.design = ""; x.result = null; x.error = null; redraw(); }} />
          <label class="line">
            <input type="checkbox" checked=${x.useCustom} data-testid="xdc-use-custom"
              onChange=${(e) => { x.useCustom = e.target.checked; redraw(); }} /> Paste my own design (JSON)
          </label>
          ${x.useCustom
            ? html`<textarea class="input mono" rows="8" data-testid="xdc-custom" aria-label="Design JSON"
                placeholder='{"kind": "rm", "name": "my_rm", "use": {"clkrst": {}, "uart": {}}}'
                value=${x.custom} onInput=${(e) => { x.custom = e.target.value; }}></textarea>`
            : html`<select class="select" aria-label="Design" data-testid="xdc-design" value=${chosen}
                onChange=${(e) => { x.design = e.target.value; x.result = null; redraw(); }}>
                ${designs.map((d) => html`<option key=${d.name} value=${d.name}>${d.name}: ${d.title || ""}</option>`)}
              </select>`}
          <div class="line">
            <button type="button" class="btn primary sm" data-testid="xdc-preview" aria-busy=${x.busy === "preview" ? "true" : undefined}
              disabled=${!!x.busy} onClick=${preview}>${x.busy === "preview" ? html`<${Spinner} />` : html`<${Icon} name="file-code" />`} Preview</button>
            <button type="button" class="btn sm" data-testid="xdc-download" aria-busy=${x.busy === "zip" ? "true" : undefined}
              disabled=${!!x.busy} onClick=${download}>${x.busy === "zip" ? html`<${Spinner} />` : html`<${Icon} name="download" />`} Download zip</button>
          </div>
          ${x.downloaded ? html`<${Reason} level="ok" text=${`Saved ${x.downloaded}.`} testid="xdc-downloaded" />` : null}
          ${x.error ? html`<${Reason} level="err" testid="xdc-error" text=${`${x.error.errName}: ${x.error.message}${x.error.hint ? ` (${x.error.hint})` : ""}`} />` : null}
          ${r && r.checks ? html`<${Checks} findings=${r.checks} />` : null}
        </div>
      <//>
    </div>
    <${Card} title="Files" icon="file-code" testid="xdc-files"
        sub=${r ? (errors ? "Preview only: the export is refused until the failed checks are fixed." : "These are the files the zip holds, with manifest.json.") : "Preview a kit to see its files."}>
      ${files.length ? html`<div class="actions">
          <div role="group" aria-label="File" data-testid="xdc-file-tabs" style="display: flex; flex-wrap: wrap; gap: 4px">
            ${files.map((f) => html`<button type="button" key=${f} class="btn ghost sm mono" data-file=${f}
              aria-pressed=${x.tab === f ? "true" : "false"} style=${x.tab === f ? "background: var(--hover); color: var(--text)" : ""}
              onClick=${() => { x.tab = f; redraw(); }}>${f}</button>`)}
          </div>
          <pre class="result" style="max-height: 520px; white-space: pre; overflow: auto" data-testid="xdc-file-body"
            data-file=${x.tab}>${(r.files || {})[x.tab] || ""}</pre>
        </div>`
        : html`<p class="muted">${r ? "No files." : "Nothing previewed yet."}</p>`}
    <//>
  </div>`;
}
