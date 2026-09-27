// The Settings dialog's sections (lane SET-UI; docs/design/SETTINGS.md §7): the left-hand list,
// and General, Boards, Tools, Harness & kits, Debug, Consoles and Advanced. Hubs is hubs.js;
// Updates is UPDATE-UI's card (selfupdate.js) plus the rows it does not cover. Every row comes
// from the schema, so a board pack's rows appear in their section with no code here.

import { hostOf } from "../format.js";
import { html } from "../lib.js";
import { changed, S } from "../store.js";
import { applyTheme } from "../theme.js";
import { Chip, CopyButton, Icon, Reason, Seg, Spinner } from "../ui.js";
import { HubsSection } from "./hubs.js";
import { RowGroup, RowLabel } from "./rows.js";
import {
  addBoard, adoptInline, detectTool, dismissRestart, joinKey, resetSetting, restartIsDemo, restartPending, saveSetting,
  sectionOfRow,
  SECTIONS, setDev, setSection, specOf, splitKey, SS,
} from "./state.js";

const TOOL_KEYS = { "tools.openocd": "openocd", "tools.vivado": "vivado", "tools.hw_server": "hw_server", "tools.uv": "uv" };
const UPDATES_CARD_KEYS = new Set(["updates.channel", "updates.auto"]);   // the Updates card has them
const BOARD_TABLES = [
  { id: "board", title: "Board", parts: ["name", "match", "via"] },
  { id: "hub", title: "Hub", parts: ["hub"] },
  { id: "ssh", title: "Linux SSH", parts: ["ssh"] },
  { id: "xvc", title: "XVC", parts: ["xvc"] },
  { id: "power", title: "Power", parts: ["power"] },
  { id: "telemetry", title: "Telemetry", parts: ["sysmon", "estimates"] },
];

function policyPath() {
  return (SS.listing && SS.listing.policy && SS.listing.policy.path) || "";
}

function isInstance(key) {
  const p = splitKey(key);
  return p[0] === "hubs" || p[0] === "boards";
}

// The rows of a section that are not a hub's or a board's: [core rows, {pack: rows}, dev rows].
function sectionRows(id, skip = new Set()) {
  const core = [];
  const packs = {};
  const dev = [];
  for (const key of SS.order) {
    const row = SS.rows[key];
    if (!row || isInstance(key) || skip.has(key) || sectionOfRow(row) !== id) continue;
    const spec = specOf(key) || {};
    if (spec.ui === false) dev.push({ ...row, readonly: true, dev: true, env: row.env || spec.env || "" });
    else if (spec.pack) (packs[spec.pack] = packs[spec.pack] || []).push(row);
    else core.push(row);
  }
  return { core, packs, dev };
}

function problemsIn(id) {
  return SS.order.some((k) => {
    const r = SS.rows[k];
    return r && sectionOfRow(r) === id && (r.problems || []).length;
  });
}

// --- the list on the left ---------------------------------------------------------------------------

export function SettingsNav() {
  const known = new Set(SECTIONS.map((s) => s.id));
  const extra = ((SS.listing && SS.listing.sections) || []).filter((s) => !known.has(s.id))
    .map((s) => ({ id: s.id, label: s.name, icon: "sliders-horizontal" }));
  const counts = {
    hubs: SS.hubs ? (SS.hubs.hubs || []).length : 0,
    boards: SS.listing ? (SS.listing.instances.boards || []).length : 0,
  };
  return html`<nav class="modal-nav settings-nav" aria-label="Settings sections" data-testid="settings-nav">
    ${[...SECTIONS, ...extra].map((s) => html`<button type="button" key=${s.id} data-settings-section=${s.id}
        aria-current=${SS.section === s.id ? "true" : "false"} onClick=${() => setSection(s.id)}>
      <${Icon} name=${s.icon} cls="sm" /><span class="grow">${s.label}</span>
      ${problemsIn(s.id) ? html`<span class="nav-flag" title="a value was skipped: see the row" data-testid="nav-problem">!</span>`
        : counts[s.id] ? html`<span class="nav-count">${counts[s.id]}</span>` : null}</button>`)}
  </nav>`;
}

// --- generic sections ---------------------------------------------------------------------------------

function PackGroups({ packs, id }) {
  return Object.entries(packs).map(([pack, rows]) => html`<${RowGroup} key=${pack} id=${`${id}:${pack}`} rows=${rows}
    title=${`${pack.toUpperCase()} board pack`} sub="declared by the pack" policyPath=${policyPath()} />`);
}

function DevGroup({ rows, id }) {
  if (!SS.dev || !rows.length) return null;
  return html`<${RowGroup} id=${`${id}:dev`} rows=${rows} title="Developer and test seams"
    sub="environment variables only; shown, never set here" policyPath=${policyPath()} />`;
}

// promote: keys shown up front even when the schema tucks them under "more" (the Tools
// section's four Detect rows).
export function GenericSection({ id, skip = new Set(), extras = {}, before = null, after = null, quiet = false,
  promote = new Set() }) {
  const rows = sectionRows(id, skip);
  const core = rows.core.map((r) => (promote.has(r.key) && r.advanced ? { ...r, advanced: false } : r));
  const { packs, dev } = rows;
  const hubs = (SS.listing && SS.listing.instances.hubs) || [];
  return html`<div class="stack gap-12">
    ${before}
    <${RowGroup} id=${id} rows=${core} policyPath=${policyPath()} extras=${extras} hubs=${hubs} />
    <${PackGroups} packs=${packs} id=${id} />
    <${DevGroup} rows=${dev} id=${id} />
    ${!core.length && !Object.keys(packs).length && !before && !quiet ? html`<p class="muted">Nothing to set here.</p>` : null}
    ${after}
  </div>`;
}

// --- General: the theme stays in this browser -----------------------------------------------------

function ThemeRow() {
  const row = SS.rows["general.theme"] || { key: "general.theme", doc: "Light, dark, or follow the system" };
  return html`<div class="srow" data-testid="setting-row" data-key="general.theme" data-source="browser">
    <${RowLabel} row=${row} label="Theme" />
    <div class="srow-ctl"><${Seg} label="Theme" value=${S.theme} onChange=${(v) => { applyTheme(v); changed(); }}
      options=${[{ value: "system", label: "Auto", icon: "monitor" }, { value: "light", label: "Light", icon: "sun" },
        { value: "dark", label: "Dark", icon: "moon" }]} /></div>
    <div class="srow-meta"><${Chip} cls="src plain" testid="source-chip" title="kept in this browser, read before the page draws">
      <span data-source="browser">this browser</span><//><span class="reset-gap"></span></div>
  </div>`;
}

// --- Tools: Detect --------------------------------------------------------------------------------

function ToolResult({ tool, row }) {
  const t = SS.tools[tool];
  if (!t || t.running) return null;
  if (t.error) return html`<div class="tool-result"><${Reason} level="err" testid="tool-result" text=${`${t.error.errName}: ${t.error.message}`} /></div>`;
  const step = ((t.result && t.result.steps) || [])[0] || {};
  const found = ((t.result && t.result.tools) || {})[tool] || {};
  const canUse = step.ok && found.path && found.path !== row.value && !row.locked && row.source !== "env"
    && found.how !== "off" && found.how !== "setting";
  return html`<div class="tool-result" data-testid="tool-result" data-ok=${step.ok ? "true" : "false"} data-tool=${tool}>
    <${Reason} level=${step.ok ? "ok" : "err"} text=${step.detail || ""} />
    ${!step.ok && step.hint ? html`<div class="hint small">${step.hint}</div>` : null}
    ${canUse ? html`<button type="button" class="btn sm" data-action="tool-use"
      onClick=${() => saveSetting(row.key, found.path)}>Use this path</button>` : null}
  </div>`;
}

function toolExtras() {
  const out = {};
  for (const [key, tool] of Object.entries(TOOL_KEYS)) {
    const row = SS.rows[key];
    if (!row) continue;
    const t = SS.tools[tool];
    out[key] = {
      ctl: html`<button type="button" class="btn sm" data-action="tool-detect" data-tool=${tool}
        aria-busy=${t && t.running ? "true" : undefined} disabled=${!!(t && t.running)}
        title="Find it and run its version probe (nothing else)" onClick=${() => detectTool(tool)}>
        ${t && t.running ? html`<${Spinner} />` : html`<${Icon} name="scan-search" />`} Detect</button>`,
      note: html`<${ToolResult} tool=${tool} row=${row} />`,
    };
  }
  return out;
}

// --- Boards ---------------------------------------------------------------------------------------

function boardRows(board) {
  const groups = Object.fromEntries(BOARD_TABLES.map((t) => [t.id, []]));
  for (const key of SS.order) {
    const p = splitKey(key);
    if (p[0] !== "boards" || p[1] !== board || p.length < 3) continue;
    const t = BOARD_TABLES.find((g) => g.parts.includes(p[2])) || BOARD_TABLES[0];
    groups[t.id].push(SS.rows[key]);
  }
  return groups;
}

// MCC-FIX: Harness Manager never uses a share on the MCC's tty_00 (the paced REBOOT needs exactly
// one reader; the MCC of a hub board runs on the hub). A `shares.mcc` entry is never a share: it
// only names the MCC console's path on the hub (hub_mcc.mcc_tty_for), so the dialog shows it as
// that, never as a share row. Remove is offered when it names the default path
// (/dev/<target>/tty_00), where removing it changes nothing. Any other share on tty_00 is
// refused by the pack's check (the row shows why, with Remove).
export function isMccShare(key) {
  const p = splitKey(key);
  return p[2] === "hub" && p[3] === "shares" && p[4] === "mcc";
}

function onTty00(row) {
  return splitKey(row.key)[3] === "shares" && (row.problems || []).some((p) => p.includes("tty_00"));
}

function MccPath({ row, board }) {
  const target = (SS.rows[joinKey(["boards", board, "hub", "target"])] || {}).value || "mps3_01_pl";
  const fallback = `/dev/${target}/tty_00`;
  const busy = !!SS.busy[row.key];
  const err = SS.rowError[row.key];
  return html`<div class="mcc-path" data-testid="mcc-path" data-key=${row.key}>
    <${Icon} name="circle-slash" />
    <div class="grow"><div><code>${row.key}</code> is never a share: it only names the MCC console's path on the
      hub, <code>${row.value}</code>. The MCC of a hub board runs on the hub.</div>
      ${row.value === fallback ? html`<div class="sub">It is the default path: removing it changes nothing.</div>` : null}
      ${err ? html`<${Reason} level="err" text=${`${err.errName}: ${err.message}`} />` : null}</div>
    ${row.value === fallback ? html`<button type="button" class="btn sm" data-action="mcc-path-remove" disabled=${busy}
      title="Remove it from boards.toml" onClick=${() => resetSetting(row.key)}>
      ${busy ? html`<${Spinner} />` : html`<${Icon} name="trash-2" />`} Remove</button>` : null}
  </div>`;
}

function openHere(board) {
  const match = (SS.rows[joinKey(["boards", board, "match"])] || {}).value || [];
  return S.order.filter((bid) => bid === board || match.includes(hostOf(bid)));
}

function BoardCard({ board }) {
  const g = boardRows(board);
  const name = (SS.rows[joinKey(["boards", board, "name"])] || {}).value || "";
  const via = (SS.rows[joinKey(["boards", board, "via"])] || {}).value || "direct";
  const use = (SS.rows[joinKey(["boards", board, "hub", "use"])] || {}).value || "";
  const inline = ((SS.hubs && SS.hubs.inline) || []).find((i) => i.board === board);
  const hubs = (SS.listing && SS.listing.instances.hubs) || [];
  const seen = openHere(board);
  const mccPaths = g.hub.filter((r) => isMccShare(r.key));
  g.hub = g.hub.filter((r) => !isMccShare(r.key));
  const labels = Object.fromEntries(g.hub.filter((r) => splitKey(r.key)[3] === "shares")
    .map((r) => [r.key, `${splitKey(r.key)[4]} share (its /dev path on the hub)`]));
  // a share left on tty_00 in boards.toml is refused (the row says why): offer to remove it
  const extras = Object.fromEntries(g.hub.filter(onTty00).map((r) => [r.key, { note: html`<button type="button"
    class="btn sm" data-action="share-remove" disabled=${!!SS.busy[r.key]} onClick=${() => resetSetting(r.key)}>
    <${Icon} name="trash-2" /> Remove it from boards.toml</button>` }]));
  return html`<section class="card board-card" data-testid="board-card" data-board=${board} aria-label=${`Board ${board}`}>
    <div class="card-head">
      <h3 class="card-title"><${Icon} name="circuit-board" /><span class="mono">${board}</span></h3>
      ${name ? html`<span class="secondary">${name}</span>` : null}
      <span class="spacer"></span>
      ${use ? html`<${Chip} icon="server" title="the hub that leases it">hub ${use}<//>` : html`<${Chip} cls="mono">${via || "direct"}<//>`}
      ${seen.length ? html`<${Chip} level="ok" icon="link" title=${seen.join(", ")}>in the rail<//>` : null}
    </div>
    <div class="card-body stack gap-12">
      ${inline ? html`<div class="inline-hub" data-testid="inline-hub" data-board=${board}><${Icon} name="info" />
        <div class="grow">Its hub is written inline (<span class="mono">${inline.host || inline.url}</span>).</div>
        <button type="button" class="btn sm" data-action="hub-adopt" onClick=${() => adoptInline(board)}>Make this a hub</button></div>` : null}
      ${BOARD_TABLES.map((t) => html`<${RowGroup} key=${t.id} id=${`board:${board}:${t.id}`} rows=${g[t.id]}
        title=${g[t.id].length && t.id !== "board" ? t.title : ""} hubs=${hubs} policyPath=${policyPath()} labels=${labels}
        extras=${extras} />
        ${t.id === "hub" ? mccPaths.map((r) => html`<${MccPath} key=${r.key} row=${r} board=${board} />`) : null}`)}
    </div>
  </section>`;
}

function AddBoardForm() {
  const f = SS.boardForm;
  const ok = /^[A-Za-z0-9_-]{1,64}$/.test(f.key.trim());
  const input = (label, field, ph, hint) => html`<label class="form-field"><span class="field-label">${label}</span>
    <input class="input mono" data-field=${field} value=${f[field]} placeholder=${ph} spellcheck="false"
      onInput=${(e) => { f[field] = e.target.value; changed(); }} />${hint ? html`<span class="muted small">${hint}</span>` : null}</label>`;
  return html`<section class="card" data-testid="board-add-form" aria-label="Add a board">
    <div class="card-head"><h3 class="card-title"><${Icon} name="plus" />Add a board</h3></div>
    <div class="card-body stack gap-12">
      <div class="form-grid">
        ${input("Key", "key", "lab", "Its table in boards.toml: [boards.KEY].")}
        ${input("Address", "match", "192.168.10.101", "The board's IP address or id (match).")}
        ${input("Name", "name", "mps3-01", "Its display name (optional).")}
      </div>
      ${f.error ? html`<${Reason} level="err" text=${`${f.error.errName}: ${f.error.message}`} />` : null}
      <div class="row"><button type="button" class="btn primary" data-action="board-add-save" disabled=${!ok || f.busy}
        onClick=${() => addBoard(f)}>Add board</button>
        <button type="button" class="btn ghost" onClick=${() => { SS.boardForm = null; changed(); }}>Cancel</button></div>
    </div></section>`;
}

function BoardsSection() {
  const boards = (SS.listing && SS.listing.instances.boards) || [];
  const { core, packs } = sectionRows("boards");
  return html`<div class="stack gap-12" data-testid="boards-section">
    <div class="section-intro">
      <p class="secondary">Per board: its name, how to reach it and its hub, from <code>boards.toml</code>${" "}
        (comments are kept when this page writes it). A board pack's own tables show under their heading.</p>
      ${!SS.boardForm ? html`<button type="button" class="btn sm" data-action="board-add-open"
        onClick=${() => { SS.boardForm = { key: "", match: "", name: "", busy: false, error: null }; changed(); }}>
        <${Icon} name="plus" /> Add a board</button>` : null}
    </div>
    ${SS.boardForm ? html`<${AddBoardForm} />` : null}
    ${!boards.length ? html`<div class="empty-note" data-testid="boards-empty"><${Icon} name="circuit-board" />
      <div><strong>No board has settings yet.</strong> Scan finds boards without any; add one here to name it, route it or
        give it a hub, or add it from a hub (Hubs → Test connection → Add this board).</div></div>` : null}
    ${boards.map((b) => html`<${BoardCard} key=${b} board=${b} />`)}
    <${RowGroup} id="boards:core" rows=${core} policyPath=${policyPath()} />
    <${PackGroups} packs=${packs} id="boards" />
  </div>`;
}

// --- Advanced: the files in use, developer seams ---------------------------------------------------

function FilesCard() {
  const f = (SS.listing && SS.listing.files) || {};
  const b = f.secrets_backend || {};
  const items = [["Settings", f.settings], ["Boards", f.boards], ["Admin policy", f.policy], ["Secrets", f.secrets]];
  return html`<div class="sgroup" data-testid="files-in-use">
    <div class="sgroup-head"><h3>Files in use</h3><span class="muted small">the same as <code>harness-manager config path</code></span></div>
    <dl class="kv">${items.map(([k, v]) => html`<dt key=${`t${k}`}>${k}</dt><dd key=${`d${k}`} class="mono small-text">${v || "—"}
        ${v ? html`<${CopyButton} text=${v} />` : null}</dd>`)}
      <dt>New secrets</dt><dd data-testid="secrets-backend">${b.where || b.backend || "?"}${b.why ? html`<div class="sub">${b.why}</div>` : null}</dd>
    </dl>
    <label class="check-inline mt-8"><input type="checkbox" checked=${SS.dev} data-action="show-dev"
      onChange=${(e) => setDev(e.target.checked)} />Show developer settings (environment variables and test seams)</label>
  </div>`;
}

// A --state-dir or --demo service reads and writes its own directory, not the one the
// advanced.state_dir row resolves to (the variable, else the default): say which it uses.
function stateDirExtras() {
  const row = SS.rows["advanced.state_dir"];
  const dir = ((SS.listing && SS.listing.files) || {}).config_dir || "";
  if (!row || !dir) return {};
  const v = String(row.value || "");
  const same = v === dir || (v.startsWith("~/") && dir.endsWith(v.slice(1)));
  const demo = !!(SS.listing.service && SS.listing.service.demo);
  if (same && !demo) return {};
  return { "advanced.state_dir": { note: html`<div class="srow-note" data-testid="service-dir-note"><${Icon} name="info" cls="sm" />
    <span>This service ${demo ? "(the demo) " : ""}reads and writes <code>${dir}</code>, its own directory${demo
      ? ": nothing here changes your own settings" : ""}.</span></div>` } };
}

// --- the restart note ------------------------------------------------------------------------------

export const RESTART_COMMAND = "harness-manager daemon stop && harness-manager ui";
export const DEMO_RESTART_COMMAND = "harness-manager daemon stop --demo && harness-manager app --demo";
// The settings `daemon start` reads when its flag is left out (SET-WIRE): a flag beats them.
const START_FLAGS = { "advanced.port": "--port", "advanced.listen": "--listen", "advanced.log_level": "--log-level" };

// null until the page knows which service it is (never the real service's command for the demo)
export function restartCommand() {
  const demo = restartIsDemo();
  return demo === null ? null : demo ? DEMO_RESTART_COMMAND : RESTART_COMMAND;
}

export function RestartNote({ testid = "settings-restart", cls = "" }) {
  const keys = restartPending();
  if (!keys.length) return null;
  const cmd = restartCommand();
  const flags = keys.map((k) => START_FLAGS[k]).filter(Boolean);
  return html`<div class=${`restart-note ${cls}`} data-testid=${testid} role="status">
    <${Icon} name="rotate-ccw" />
    <div class="grow"><strong>${keys.length === 1 ? "1 change needs" : `${keys.length} changes need`} the service restarted</strong>
      <span class="secondary"> (${keys.join(", ")}).</span>
      ${cmd ? html`<div class="small secondary">Restart it: <code data-testid="restart-command">${cmd}</code> <${CopyButton} text=${cmd} /></div>` : null}
      ${flags.length ? html`<div class="small secondary" data-testid="restart-flags">Start it without${" "}
        ${flags.map((f, i) => html`${i ? " or " : ""}<code key=${f}>${f}</code>`)}: a flag given to${" "}
        <code>daemon start</code> wins over the setting.</div>` : null}</div>
    <button type="button" class="btn ghost sm" data-action="restart-dismiss" title="Hide this until the next such change"
      onClick=${dismissRestart}>Later</button>
  </div>`;
}

// --- one section --------------------------------------------------------------------------------------

export function SettingsSectionBody({ updatesCard = null }) {
  if (!SS.loaded) {
    return SS.error ? html`<${Reason} level="err" testid="settings-load-error"
        text=${`${SS.error.errName}: ${SS.error.message}${SS.error.hint ? ` (${SS.error.hint})` : ""}`} />`
      : html`<p class="muted"><${Spinner} /> Reading the settings...</p>`;
  }
  const id = SS.section;
  const pp = policyPath();
  const problems = (SS.listing.problems || []);
  const top = html`${id === "general" && problems.length ? html`<div class="stack">${problems.map((p) => html`<${Reason}
    key=${p} level="warn" testid="settings-problem" text=${p} />`)}</div>` : null}
    ${SS.listing.policy && SS.listing.policy.exists && id === "general" ? html`<div class="policy-note" data-testid="settings-policy-note">
      <${Icon} name="shield-check" /><div>Your administrator's policy <code>${pp}</code> applies: the rows it locks show${" "}
        <b>admin</b> and cannot be changed here.</div></div>` : null}`;
  switch (id) {
    case "hubs": return html`<${HubsSection} policyPath=${pp} />`;
    case "boards": return html`<${BoardsSection} />`;
    case "updates":
      return html`<div class="stack gap-12">${updatesCard}
        <${GenericSection} id="updates" skip=${UPDATES_CARD_KEYS} quiet=${true} /></div>`;
    case "general":
      return html`<${GenericSection} id="general" skip=${new Set(["general.theme"])}
        before=${html`${top}<div class="sgroup"><${ThemeRow} /></div>`} />`;
    case "tools":
      return html`<${GenericSection} id="tools" extras=${toolExtras()} promote=${new Set(Object.keys(TOOL_KEYS))}
        before=${html`<p class="secondary">Empty means
        Harness Manager looks for the tool itself. Detect finds it and runs only its version probe
        (<code>--version</code>, <code>-version</code>); hw_server is never run.</p>`} />`;
    case "advanced":
      return html`<${GenericSection} id="advanced" extras=${stateDirExtras()} after=${html`<${FilesCard} />`} />`;
    default:
      return html`<${GenericSection} id=${id} />`;
  }
}
