// One settings row (lane SET-UI; docs/design/SETTINGS.md §7 "Row behaviour"): the control its
// type picks, the source chip, the notes (locked, overridden, capped, problems, what a change
// needs) and Reset. SecretField is the row of a secret: set / replace / clear, never a value.

import { html, useEffect, useRef, useState } from "../lib.js";
import { Chip, Icon, Reason, Spinner } from "../ui.js";
import {
  boardsFor, clearSecret, reopenBoards, resetSetting, restartPending, saveSetting, specOf, splitKey, SS,
  storeSecret,
} from "./state.js";
import { changed } from "../store.js";

// --- text: labels, values -----------------------------------------------------------------------

// "OpenOCD (empty: openocd on PATH)" -> ["OpenOCD", "empty: openocd on PATH"]
export function docParts(doc) {
  const text = String(doc || "");
  const i = text.indexOf(" (");
  // "(s)", "(px)": a unit stays in the label; a longer aside becomes the hint under it
  const one = text.split("(").length === 2;          // "SSH (a lab account) or REST (a token)" stays whole
  if (i > 0 && one && text.endsWith(")") && text.length - i > 7) return [text.slice(0, i), text.slice(i + 2, -1)];
  return [text, ""];
}

const UNITS = [["d", 86400], ["h", 3600], ["m", 60]];
export function durationText(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v ?? "");
  if (n === 0) return "0";
  for (const [u, s] of UNITS) if (n % s === 0) return `${n / s}${u}`;
  return `${n}s`;
}

const SIZES = [["TiB", 1024 ** 4], ["GiB", 1024 ** 3], ["MiB", 1024 ** 2], ["KiB", 1024],
  ["T", 1000 ** 4], ["G", 1000 ** 3], ["M", 1000 ** 2], ["k", 1000]];
export function sizeText(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return String(v ?? "");
  for (const [u, s] of SIZES) if (n % s === 0) return `${n / s}${u}`;
  return String(n);
}

// A row's value as its text box shows it.
export function valueText(row) {
  const v = row.value;
  if (v === null || v === undefined) return "";
  if (row.type === "duration") return durationText(v);
  if (row.type === "size") return sizeText(v);
  if (row.type === "list") return (v || []).join(", ");
  if (row.type === "int" && /i2c_address$/.test(row.key)) return `0x${Number(v).toString(16)}`;
  return String(v);
}

function defaultText(row) {
  const d = row.default;
  if (d === null || d === undefined || d === "" || (Array.isArray(d) && !d.length)) return "";
  return valueText({ ...row, value: d });
}

// --- the source chip ----------------------------------------------------------------------------

export function SourceChip({ row }) {
  const where = row.where || "";
  switch (row.source) {
    case "user":
      return html`<${Chip} level="accent" testid="source-chip" cls="src" title=${`yours, in ${where || "settings.toml"}`}>
        <span data-source="user">yours</span><//>`;
    case "lock":
      return html`<${Chip} level="held" icon="lock" testid="source-chip" cls="src"
        title=${`${"set by your administrator"} in ${where}`}><span data-source="lock">admin</span><//>`;
    case "machine":
      return html`<${Chip} testid="source-chip" cls="src" title=${`this machine's default, from ${where}`}>
        <span data-source="machine">lab default</span><//>`;
    case "env":
      return html`<${Chip} level="warn" testid="source-chip" cls="src mono"
        title=${`overridden by ${where} in the service's environment`}><span data-source="env">from ${where}</span><//>`;
    case "pack":
      return html`<${Chip} testid="source-chip" cls="src plain" title=${`the default of ${where}`}>
        <span data-source="pack">from the pack</span><//>`;
    default:
      return html`<${Chip} testid="source-chip" cls="src plain" title="the built-in default">
        <span data-source="default">default</span><//>`;
  }
}

// --- controls -----------------------------------------------------------------------------------

function inputId(key) { return `set-${key.replace(/[^A-Za-z0-9_-]+/g, "_")}`; }

// A text box that saves on change (blur or Enter), as UPDATE-UI's controls save on click.
function TextControl({ row, disabled, onSave, placeholder = "", mono = false, type = "text", min, max, step }) {
  const shown = valueText(row);
  const [draft, setDraft] = useState(shown);
  const ref = useRef(null);
  useEffect(() => {
    if (document.activeElement !== ref.current) setDraft(shown);
  }, [shown]);
  const commit = () => {
    if (draft === shown) return;
    onSave(draft);
  };
  return html`<input ref=${ref} id=${inputId(row.key)} class=${`input ${mono ? "mono" : ""} grow-input`}
    type=${type} value=${draft} placeholder=${placeholder} disabled=${disabled}
    min=${min ?? undefined} max=${max ?? undefined} step=${step}
    data-testid="setting-input" spellcheck="false" autocomplete="off"
    onInput=${(e) => setDraft(e.target.value)} onChange=${commit}
    onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } else if (e.key === "Escape" && draft !== shown) { e.stopPropagation(); setDraft(shown); } }} />`;
}

function SegControl({ row, options, disabled, onSave }) {
  const value = row.value;
  return html`<div class="seg" role="group" aria-label=${row.key} data-testid="setting-seg">${options.map((o) => html`<button
      type="button" key=${String(o.value)} data-value=${String(o.value)} aria-pressed=${value === o.value ? "true" : "false"}
      disabled=${disabled} title=${o.title || o.label}
      onClick=${() => { if (value !== o.value) onSave(o.value); }}>${o.label}</button>`)}</div>`;
}

function SelectControl({ row, options, disabled, onSave }) {
  const value = row.value === null || row.value === undefined ? "" : String(row.value);
  return html`<select class="select grow-input" id=${inputId(row.key)} disabled=${disabled} data-testid="setting-input"
    value=${value} onChange=${(e) => onSave(e.target.value)}>
    ${options.map((o) => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}</select>`;
}

// The schema's bounds (inclusive, null: no limit; min_exclusive: "more than"), said as the
// service's checks say them. "" when n is in range (or not a number: the service says why).
export function outOfBounds(n, lo, hi, above = false) {
  if (!Number.isFinite(n)) return "";
  const has = (v) => v !== null && v !== undefined;
  if (has(lo) && (above ? n <= lo : n < lo)) {
    if (above) return `must be more than ${lo}`;
    return has(hi) ? `must be ${lo}..${hi}` : `must be ${lo} or more`;
  }
  if (has(hi) && n > hi) return has(lo) ? `must be ${lo}..${hi}` : `must be ${hi} or less`;
  return "";
}

function choiceLabel(c) {
  return c === "" ? "(none)" : String(c);
}

export function Control({ row, disabled, onSave, hubs = [] }) {
  const spec = specOf(row.key) || {};
  const [, hint] = docParts(row.doc);
  const type = row.type;
  if (type === "bool") {
    const opts = [{ value: true, label: "On" }, { value: false, label: "Off" }];
    return html`<${SegControl} row=${row} options=${opts} disabled=${disabled} onSave=${onSave} />`;
  }
  if (type === "enum") {
    const choices = row.choices || spec.choices || [];
    const opts = choices.map((c) => ({ value: c, label: choiceLabel(c) }));
    return choices.length <= 4 && choices.every((c) => String(c).length <= 10)
      ? html`<${SegControl} row=${row} options=${opts} disabled=${disabled} onSave=${onSave} />`
      : html`<${SelectControl} row=${row} options=${opts} disabled=${disabled} onSave=${onSave} />`;
  }
  if (type === "ref") {
    const names = [...new Set([...(hubs || []), ...(row.value ? [row.value] : [])])];
    const opts = [{ value: "", label: "(no hub)" }, ...names.map((n) => ({ value: n, label: n }))];
    return html`<${SelectControl} row=${row} options=${opts} disabled=${disabled} onSave=${onSave} />`;
  }
  if (type === "int" || type === "float") {
    const [lo, hi] = spec.bounds || [null, null];
    const above = !!spec.min_exclusive;
    const hex = /i2c_address$/.test(row.key);
    return html`<${TextControl} row=${row} disabled=${disabled} type=${hex ? "text" : "number"}
      min=${lo} max=${hi} step=${type === "float" ? "any" : undefined}
      placeholder=${defaultText(row) || hint}
      onSave=${(text) => {
        if (text.trim() === "") { onSave(null); return; }
        const n = Number(hex ? parseInt(text, 16) : text);
        const bad = outOfBounds(n, lo, hi, above);
        if (bad) { onSave(undefined, `${row.key} ${bad}`); return; }
        onSave(text.trim());                // "0x40" too: the service parses it as the CLI does
      }} />`;
  }
  const mono = type === "path" || type === "url" || type === "list" || type === "duration" || type === "size";
  const placeholder = defaultText(row) || (hint.startsWith("empty") ? hint : "");
  return html`<${TextControl} row=${row} disabled=${disabled} mono=${mono} placeholder=${placeholder}
    onSave=${(text) => onSave(text.trim() === "" ? null : text.trim())} />`;
}

// --- notes under a row ----------------------------------------------------------------------------

// What a change needs, said once it was made here: "reopen" offers Reopen board (the boards
// open in this page that it is about), "restart" points at the restart banner.
function ApplyNote({ row }) {
  if (row.apply === "reopen" && SS.reopen[row.key]) {
    const bids = boardsFor(row.key);
    return html`<div class="srow-note apply" data-testid="apply-note" data-apply="reopen">
      <${Icon} name="refresh-cw" cls="sm" /><span>Changed: it applies the next time the board opens.</span>
      ${bids.length ? html`<button type="button" class="btn sm" data-action="reopen-board"
        onClick=${() => reopenBoards(bids, [row.key])}>Reopen ${bids.length === 1 ? "board" : `${bids.length} boards`}</button>` : null}</div>`;
  }
  if (row.apply === "restart" && restartPending().includes(row.key)) {
    return html`<div class="srow-note apply" data-testid="apply-note" data-apply="restart">
      <${Icon} name="rotate-ccw" cls="sm" /><span>Changed: it applies after the service restarts (see below).</span></div>`;
  }
  return null;
}

// A small mark beside the key for a setting that does not apply at once.
function ApplyMark({ row }) {
  if (row.readonly) return null;
  if (row.apply === "reopen") return html`<span class="apply-mark" title="A change applies the next time a board opens">on reopen</span>`;
  if (row.apply === "restart") return html`<span class="apply-mark" title="A change applies after the service restarts">on restart</span>`;
  return null;
}

export function RowNotes({ row, policyPath, quietLock = false, note = null }) {
  const out = [];
  if (row.locked && quietLock) {
    // a machine hub's card names the policy once, above its rows
  } else if (row.locked) {
    out.push(html`<div class="srow-note held" key="lock" data-testid="locked-note"><${Icon} name="lock" cls="sm" />
      <span>Set by your administrator in <code>${row.where || policyPath}</code>: it cannot be changed here.</span></div>`);
  } else if (row.source === "env") {
    out.push(row.shadowed
      ? html`<div class="srow-note warn" key="env" data-testid="shadow-note"><${Icon} name="triangle-alert" cls="sm" />
          <span>Your value is hidden by <code>${row.shadowed}</code>: overridden by ${row.shadowed} in the service's environment.</span></div>`
      : html`<div class="srow-note warn" key="env" data-testid="env-note"><${Icon} name="info" cls="sm" />
          <span>Overridden by <code>${row.where}</code> in the service's environment.</span></div>`);
  }
  if (row.capped) out.push(html`<${Reason} key="cap" level="warn" testid="capped-note" text=${row.capped} />`);
  for (const p of row.problems || []) out.push(html`<${Reason} key=${p} level="warn" testid="row-problem" text=${p} />`);
  const err = SS.rowError[row.key];
  if (err) {
    out.push(html`<${Reason} key="err" level="err" testid="row-error"
      text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}`} />`);
  }
  if (!row.locked && !row.readonly && row.source !== "env") out.push(html`<${ApplyNote} key="apply" row=${row} />`);
  if (note) out.push(html`<div key="extra">${note}</div>`);
  return out.length ? html`<div class="srow-notes">${out}</div>` : null;
}

// --- a row ------------------------------------------------------------------------------------------

export function RowLabel({ row, label = "" }) {
  const [main, hint] = docParts(row.doc);
  return html`<div class="srow-label">
    <label for=${inputId(row.key)}>${label || main}</label>
    ${hint ? html`<span class="srow-hint">${hint}</span>` : null}
    <span class="srow-keyline"><code class="srow-key" title="harness-manager config get KEY">${row.key}</code><${ApplyMark} row=${row} /></span>
  </div>`;
}

function localError(row, message) {
  SS.rowError[row.key] = { errName: "USAGE", message, hint: "nothing was written" };
  changed();
}

export function SettingRow({ row, label = "", hubs = [], extra = null, note = null, policyPath = "", quietLock = false }) {
  if (row.secret) return html`<${SecretField} row=${row} label=${label} policyPath=${policyPath} />`;
  if (row.dev) return html`<${DevRow} row=${row} label=${label} />`;
  const busy = !!SS.busy[row.key];
  const disabled = row.locked || row.readonly || row.source === "env" || busy;
  const canReset = (row.source === "user" || !!row.shadowed) && !row.locked && !row.readonly;
  const onSave = (value, bad) => {
    if (bad) { localError(row, bad); return; }
    if (value === null) {
      // An emptied box: yours goes (back to the next layer); over a lab or pack default,
      // "" (or []) is stored as yours, so "empty: search for it" can beat the default.
      if (row.source === "user") resetSetting(row.key);
      else if ((row.source === "machine" || row.source === "pack") && ["str", "path", "url", "ref", "list"].includes(row.type)) {
        saveSetting(row.key, row.type === "list" ? [] : "");
      }
      return;
    }
    saveSetting(row.key, value);
  };
  return html`<div class="srow" data-testid="setting-row" data-key=${row.key} data-source=${row.source}
      data-locked=${row.locked ? "true" : "false"}>
    <${RowLabel} row=${row} label=${label} />
    <div class="srow-ctl">
      ${row.readonly
        ? html`<span class="mono srow-ro" data-testid="setting-value">${valueText(row) || "—"}</span>`
        : html`<${Control} row=${row} disabled=${disabled} onSave=${onSave} hubs=${hubs} />`}
      ${extra}
    </div>
    <div class="srow-meta">
      ${busy ? html`<${Spinner} />` : null}
      <${SourceChip} row=${row} />
      ${canReset ? html`<button type="button" class="btn ghost sm icon-only" data-action="setting-reset"
        aria-label=${`Reset ${row.key}`} title=${row.shadowed ? "Remove your hidden value" : "Reset: back to the default"}
        disabled=${busy} onClick=${() => resetSetting(row.key)}><${Icon} name="rotate-ccw" /></button>` : html`<span class="reset-gap"></span>`}
    </div>
    <${RowNotes} row=${row} policyPath=${policyPath} quietLock=${quietLock} note=${note} />
  </div>`;
}

// --- a developer seam: shown, never set (SET-WIRE: `config set` refuses it) ----------------------

// SettingsSection's DevGroup marks these ({...row, dev: true}). The value is what the service
// runs with; the note names how it is set: its variable, else the pack's --pack-overrides.
export function DevRow({ row, label = "" }) {
  const how = row.env
    ? html`set <code data-testid="dev-var">$${row.env}</code> in the service's environment`
    : html`the pack's own value, set with the service's <code data-testid="dev-var">--pack-overrides</code>`;
  return html`<div class="srow dev" data-testid="setting-row" data-key=${row.key} data-source=${row.source}
      data-locked="false" data-dev="true">
    <${RowLabel} row=${row} label=${label} />
    <div class="srow-ctl"><span class="mono srow-ro" data-testid="setting-value">${valueText(row) || "—"}</span></div>
    <div class="srow-meta"><${SourceChip} row=${row} /><span class="reset-gap"></span></div>
    <div class="srow-notes">
      <div class="srow-note" data-testid="dev-note"><${Icon} name="file-code" cls="sm" />
        <span>A developer seam, not a setting: ${how}. It cannot be set here or with <code>config set</code>.</span></div>
      ${(row.problems || []).map((p) => html`<${Reason} key=${p} level="warn" testid="row-problem" text=${p} />`)}
    </div>
  </div>`;
}

// --- a secret -----------------------------------------------------------------------------------

const BACKENDS = {
  "secret-service": "in your login keyring (Secret Service)",
  kwallet: "in KWallet",
  "macos-keychain": "in the macOS Keychain",
  "windows-credential": "in the Windows Credential Manager",
  file: "in a private file (0600)",
  inline: "written inline in boards.toml",
  "fpgahub-login": "from your fpgahub login",
  gh: "from gh auth token",
};

export function secretWhere(st) {
  if (!st || !st.set) return "";
  if (st.backend === "env") return `from ${st.where} in the service's environment`;
  // a file: reference in your settings names its path; the store's own file says "a private file"
  if (st.backend === "file" && /[\/\\]/.test(st.where || "") && !/[\/\\]secrets[\/\\]/.test(st.where)) return `read from ${st.where}`;
  return BACKENDS[st.backend] || `in ${st.where || st.backend}`;
}

export function SecretField({ row, label = "", policyPath = "" }) {
  const st = row.secret || { set: !!(row.value && row.value.set) };
  const busy = !!SS.busy[row.key];
  const editing = SS.secretEdit === row.key;
  const ref = useRef(null);
  useEffect(() => { if (editing && ref.current) ref.current.focus(); }, [editing]);
  const save = () => {
    const el = ref.current;
    const value = el ? el.value : "";
    if (el) el.value = "";
    if (!value) { SS.secretEdit = ""; changed(); return; }
    storeSecret(row.key, value);
  };
  const cancel = () => { if (ref.current) ref.current.value = ""; SS.secretEdit = ""; changed(); };
  const stored = st.set && st.backend !== "env" && st.backend !== "inline";
  const unreachable = st.set && st.reachable === false;
  const status = unreachable
    ? `stored ${secretWhere(st) || `in ${st.where}`}, which this service cannot reach`
    : st.set ? `set, ${secretWhere(st)}` : "not set";
  return html`<div class="srow secret" data-testid="setting-row" data-key=${row.key} data-source=${row.source}
      data-locked="false">
    <${RowLabel} row=${row} label=${label} />
    <div class="srow-ctl">
      <div class="secret-field" data-testid="secret-field" data-set=${st.set ? "true" : "false"}
          data-backend=${st.backend || ""} data-reachable=${st.reachable === false ? "false" : "true"}>
        ${editing
          ? html`<input ref=${ref} type="password" class="input mono grow-input" data-testid="secret-input"
              autocomplete="off" spellcheck="false" aria-label=${`New value for ${row.key}`}
              onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); save(); } else if (e.key === "Escape") { e.stopPropagation(); cancel(); } }} />
            <button type="button" class="btn sm primary" data-action="secret-save" onClick=${save}>Save</button>
            <button type="button" class="btn sm ghost" data-action="secret-cancel" onClick=${cancel}>Cancel</button>`
          : html`<span class=${`secret-dots ${st.set ? "" : "none"}`} aria-hidden="true">${st.set ? "●●●●●●" : "—"}</span>
            <span class="secret-status" data-testid="secret-status">${status}</span>`}
      </div>
    </div>
    <div class="srow-meta">
      ${busy ? html`<${Spinner} />` : null}
      ${!editing ? html`<button type="button" class="btn sm" data-action=${st.set ? "secret-replace" : "secret-set"}
        disabled=${busy} onClick=${() => { SS.secretEdit = row.key; changed(); }}>${st.set ? "Replace" : "Set"}</button>` : null}
      ${!editing && stored ? html`<button type="button" class="btn sm ghost" data-action="secret-clear"
        disabled=${busy} onClick=${() => clearSecret(row.key)}>Remove</button>` : null}
    </div>
    <div class="srow-notes">
      ${unreachable ? html`<div class="srow-note warn" data-testid="secret-unreachable"><${Icon} name="triangle-alert" cls="sm" />
        <span>Stored, unreachable here (exit 7): ${st.why || "this service has no session bus"}. Setting it again from here moves it to a private file.</span></div>` : null}
      ${st.backend === "env" && row.shadowed ? html`<div class="srow-note warn" data-testid="shadow-note"><${Icon} name="triangle-alert" cls="sm" />
        <span>Your stored value is hidden by <code>${row.shadowed}</code> in the service's environment.</span></div>` : null}
      ${st.backend === "file" && st.why && !unreachable ? html`<div class="srow-note" data-testid="secret-why"><${Icon} name="info" cls="sm" /><span>${st.why}</span></div>` : null}
      ${(row.problems || []).map((p) => html`<${Reason} key=${p} level="warn" testid="row-problem" text=${p} />`)}
      ${SS.rowError[row.key] ? html`<${Reason} level="err" testid="row-error"
        text=${`${SS.rowError[row.key].errName}: ${SS.rowError[row.key].message}`} />` : null}
    </div>
  </div>`;
}

// --- groups of rows -----------------------------------------------------------------------------

// Rows with a "more" toggle for the advanced ones. id: the group's key in SS.more.
// extras: key -> {ctl, note}: a button beside the control (Detect) and a line under the row.
export function RowGroup({ id, rows, title = "", sub = "", hubs = [], policyPath = "", extras = {}, labels = {},
  quietLock = false }) {
  if (!rows.length) return null;
  const main = rows.filter((r) => !r.advanced);
  const adv = rows.filter((r) => r.advanced);
  const open = !!SS.more[id];
  const one = (r) => html`<${SettingRow} key=${r.key} row=${r} hubs=${hubs} policyPath=${policyPath}
    extra=${(extras[r.key] || {}).ctl || null} note=${(extras[r.key] || {}).note || null}
    label=${labels[r.key] || ""} quietLock=${quietLock} />`;
  return html`<div class="sgroup" data-testid="setting-group" data-group=${id}>
    ${title ? html`<div class="sgroup-head"><h3>${title}</h3>${sub ? html`<span class="muted small">${sub}</span>` : null}</div>` : null}
    ${main.map(one)}
    ${adv.length ? html`<button type="button" class="btn ghost sm more-btn" data-action="show-more"
        aria-expanded=${open ? "true" : "false"} onClick=${() => { SS.more[id] = !open; changed(); }}>
        <${Icon} name="chevron-right" cls=${`sm chev ${open ? "open" : ""}`} />${open ? "Fewer" : `${adv.length} more`}</button>` : null}
    ${open ? adv.map(one) : null}
  </div>`;
}

export function tail(key, n = 1) {
  return splitKey(key).slice(-n).join(".");
}
