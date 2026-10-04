// The Import dialog (UI v2, lane UI2-BUILD; UI_V2_PLAN.md M6, round-3 ImportModal): a design
// someone built, into this machine's overlay store, so the Workbench lists it.
//
//   openModal("import", {bid})              // from the Workbench's "Import a design…"
//   openModal("import", {bid, way: "path"})  // "file" | "build" | "path"
//
// Three ways in:
// - a zip (a packed overlay folder, or a build directory's out/ with its receipt), sent as the
//   request body: POST /overlays/upload (a browser never reveals a file's path);
// - From Build: the builds this page's Build tab checked and has not added yet;
// - a path on harness-manager-daemon's host (a folder, its manifest.json, a receipt or a build
//   directory): POST /overlays/import.
// Every way answers the same five groups (Files, Same shell, rm_id in the user range, CRC and
// sizes, Clearing pairs with the partial) and refuses with the daemon's code: exit 14
// INCOMPATIBLE for another shell, exit 15 REFUSED for the rest. Nothing is written to the board,
// so an import needs no lease. A good one lands on the Workbench, picked.

import { call, callUpload, toApiError } from "./api.js";
import { boardName, bytesText, hexId } from "./format.js";
import { html, useState } from "./lib.js";
import { closeModal, ModalShell, registerModal, setModalBusy } from "./modal.js";
import { boardState, changed, loadOverlays, navigate, runPreflight, S } from "./store.js";
import { Chip, Icon, Reason, Spinner } from "./ui.js";

// --- From Build: what the Build tab checked (build.js noteBuilt) -----------------------------

const BUILT = new Map();          // path -> {bid, name, rm_id, static_id, path, gates, when, added}

export function noteBuilt(bid, entry) {
  if (!entry || !entry.path) return;
  const old = BUILT.get(entry.path) || {};
  BUILT.set(entry.path, { ...old, bid, ...entry, added: !!(entry.added || old.added) });
}

export function builtList() { return [...BUILT.values()]; }

// Land on the Workbench with `name` picked (its preflight runs), as the Build tab's Add does.
export function pickOnWorkbench(bid, name) {
  const b = boardState(bid);
  b.selectedOverlay = name;
  b.overlays = null;                       // the list is read again: the new design is in it
  navigate(bid, "workbench");
  loadOverlays(bid).then(() => runPreflight(bid, name));
}

// --- the five groups ---------------------------------------------------------------------------

function boardStatic(bid) {
  const info = boardState(bid).info || {};
  const id = info.identity || (info.candidate && info.candidate.identity) || {};
  return hexId(id.shell_id || "");
}

const GROUPS = [
  { id: "files", title: "Files", d: () => "manifest.json with the partial and clearing it names, or a passed build receipt and its pair" },
  { id: "shell", title: "Same shell", d: (bid) => `built for the shell ${bid} runs${boardStatic(bid) ? `, ${boardStatic(bid)}` : ""}` },
  { id: "rm_id", title: "rm_id in the user range", d: () => "design id 0x8000-0xFFFF; outside it, or one another design holds, is a warning" },
  { id: "crc", title: "CRC and sizes", d: () => "the partial and clearing match the manifest's (or the receipt's) lengths and CRC-32" },
  { id: "pair", title: "Clearing pairs with the partial", d: () => "the clearing's frames lie inside the partial's; it fits the harness's clearing arena" },
];

const ROW_LOOK = { ok: ["ok", "circle-check"], warning: ["warn", "triangle-alert"], unchecked: ["unk", "circle-help"],
  mismatch: ["err", "circle-x"] };

function Groups({ bid, res, running }) {
  const got = (res && res.groups) || [];
  return html`<ul class="imp-checks" data-testid="import-groups">${GROUPS.map((gr) => {
    const r = got.find((x) => x.id === gr.id);
    const look = r ? ROW_LOOK[r.state] || ["unk", "circle-help"] : running ? ["run", "loader-circle"] : ["", "circle-dashed"];
    return html`<li key=${gr.id} class=${look[0]} data-group=${gr.id} data-state=${r ? r.state : ""}>
      <${Icon} name=${look[1]} cls=${look[0] === "run" ? "spin" : ""} />
      <span class="t">${r ? r.title : gr.title}</span><span class="d">${r ? r.detail : gr.d(bid)}</span></li>`;
  })}</ul>`;
}

// --- the dialog --------------------------------------------------------------------------------

const WAYS = [
  { k: "file", icon: "folder-input", t: "A zip", d: "A packed overlay folder, or a build's out/ with its receipt, as a .zip" },
  { k: "build", icon: "file-cog", t: "From Build", d: "Designs checked in the Build tab, not added yet" },
  { k: "path", icon: "hard-drive", t: "A path on this machine", d: "Where it is; Harness Manager reads it in place" },
  { k: "folder", icon: "layers", t: "A folder of designs", d: "A release's overlays/ folder: every design in it, one click" },
];

// The words of one row of "A folder of designs" (the guide quotes them).
export const FOLDER_STATE = { imported: "imported", skipped: "skipped: built for another static", refused: "refused", ready: "ready" };
export function folderRowText(r) {
  const head = r.state === "skipped" ? "skipped" : FOLDER_STATE[r.state] || r.state;
  return r.state === "skipped" || r.state === "refused" ? `${head}: ${r.reason}` : `${head}${r.reason ? ` (${r.reason})` : ""}`;
}

function exitOf(e) {
  if (!e) return "";
  if (e.errName === "INCOMPATIBLE") return "exit 14 INCOMPATIBLE";
  if (e.errName === "REFUSED") return "exit 15 REFUSED";
  return e.code !== null && e.code !== undefined ? `exit ${e.code} ${e.errName}` : e.errName;
}

function ImportDialog({ bid, way: startWay = "file" }) {
  const [, setN] = useState(0);
  const redraw = () => setN((n) => n + 1);
  const [m] = useState(() => ({ way: startWay, file: null, path: "", pick: null, over: false,
    busy: "", res: null, err: null, done: null, note: "", fpath: "", folder: null }));
  const reset = () => { m.res = null; m.err = null; m.done = null; m.note = ""; m.folder = null; };
  const info = boardState(bid).info || {};
  const board = boardName(info.candidate || (S.boards[bid] || {}).candidate || null, bid);
  const here = boardStatic(bid);
  const built = builtList().filter((x) => !x.added);

  const choose = (file) => {
    reset();
    if (!file) { m.file = null; redraw(); return; }
    if (!/\.zip$/i.test(file.name)) {
      m.file = null;
      m.note = /\.json$/i.test(file.name)
        ? `${file.name} is a receipt: it names its partial and clearing beside it, which a browser cannot send. Give its path instead (A path on this machine), or zip its out/ folder.`
        : `${file.name} is not a .zip: zip the overlay folder (manifest.json + the partial + the clearing), or give its path.`;
    } else m.file = file;
    redraw();
  };

  const runFolder = async (checkOnly) => {
    reset();
    m.busy = checkOnly ? "read" : "import";
    setModalBusy(true);
    redraw();
    try {
      const path = m.fpath.trim();
      if (!path) throw new Error("give the folder's path first");
      const body = { path, board_id: bid };
      if (checkOnly) body.check_only = true;
      m.folder = (await call("overlayImportFolder", {}, body)).data;
      if (!checkOnly && (m.folder.counts.imported || 0) > 0) loadOverlays(bid);
    } catch (e) {
      m.err = toApiError(e);
    }
    m.busy = "";
    setModalBusy(false);
    redraw();
    changed();
  };

  const run = async (checkOnly = false) => {
    if (m.way === "folder") return runFolder(checkOnly);
    reset();
    m.busy = checkOnly ? "read" : "import";
    setModalBusy(true);
    redraw();
    try {
      let d;
      if (m.way === "file") {
        if (!m.file) throw new Error("choose a zip first");
        d = (await callUpload("overlayUpload", {}, m.file, { name: m.file.name, board_id: bid, check_only: checkOnly ? "true" : "" })).data;
      } else {
        const path = m.way === "build" ? (m.pick && m.pick.path) || "" : m.path.trim();
        if (!path) throw new Error(m.way === "build" ? "select a design first" : "give the path first");
        const body = { path, board_id: bid };
        if (checkOnly) body.check_only = true;
        d = (await call("overlayImport", {}, body)).data;
      }
      m.res = d;
      if (!checkOnly && d.imported) {
        m.done = d;
        noteBuilt(bid, { name: d.name, rm_id: d.rm_id, static_id: d.static_id, path: d.path, added: true });
        loadOverlays(bid);
      }
    } catch (e) {
      const err = toApiError(e);
      m.err = err;
      if (err.data && err.data.groups) m.res = err.data;
    }
    m.busy = "";
    setModalBusy(false);
    redraw();
    changed();
  };

  const onDrop = (e) => {
    e.preventDefault();
    m.over = false;
    const dt = e.dataTransfer;
    const f = dt && dt.files && dt.files[0];
    let dir = false;
    try {
      const en = dt.items && dt.items[0] && dt.items[0].webkitGetAsEntry && dt.items[0].webkitGetAsEntry();
      dir = !!(en && en.isDirectory);
    } catch (err) { /* no entries API */ }
    if (dir) {
      reset(); m.file = null;
      m.note = "A folder cannot be sent from the browser: zip it, or give its path (A path on this machine).";
      redraw();
      return;
    }
    choose(f || null);
  };

  let src = null;
  if (m.way === "file") {
    src = html`<div class=${`imp-drop${m.over ? " over" : ""}`} data-testid="import-drop"
        onDragOver=${(e) => { e.preventDefault(); if (!m.over) { m.over = true; redraw(); } }}
        onDragLeave=${() => { m.over = false; redraw(); }} onDrop=${onDrop}>
      <${Icon} name="folder-input" />
      <div>Drop a <code>.zip</code> here: a packed overlay folder (<code>manifest.json</code> + the partial + the clearing), or a build's <code>out/</code> holding its receipt <code>${"<name>_build.json"}</code> and the pair it names.</div>
      <label class="btn sm imp-file"><${Icon} name="upload" />Choose a zip…
        <input type="file" accept=".zip,application/zip" data-testid="import-file" disabled=${!!m.busy}
          onChange=${(e) => choose(e.target.files && e.target.files[0])} /></label></div>`;
  } else if (m.way === "build") {
    src = built.length ? html`<ul class="imp-list" data-testid="import-built">${built.map((x) => {
        const on = m.pick && m.pick.path === x.path;
        const other = here && x.static_id && hexId(x.static_id) !== here;
        return html`<li key=${x.path} class=${on ? "on" : ""} data-name=${x.name}><div>
            <div class="nm">${x.name}<span class="muted mono small">${x.rm_id}</span>
              ${other ? html`<${Chip} level="warn" icon="triangle-alert" cls="bd-mini">built for ${hexId(x.static_id)}<//>` : null}</div>
            <div class="sub">checked in Build${x.bid && x.bid !== bid ? ` (${x.bid})` : ""} · ${x.gates ? `${x.gates} gates` : "passed"} · <span class="mono">${x.path}</span></div></div>
          <button type="button" class=${`btn sm ${on ? "primary" : ""}`} disabled=${!!m.busy}
            onClick=${() => { reset(); m.pick = x; redraw(); }}>${on ? "Selected" : "Select"}</button></li>`;
      })}</ul>
      <div class="small muted">A design lands here when the Build tab's Check passes. Its Add does the same as Import here.</div>`
      : html`<${Reason} testid="import-built-none" text="Nothing checked in the Build tab and not added yet. Build a design there, or give a build directory's path (A path on this machine)." />`;
  } else if (m.way === "folder") {
    src = html`<div class="field" style="flex-wrap:wrap"><label for="imp-fpath">Folder</label>
        <input id="imp-fpath" class="input mono grow" data-testid="import-folder-path" placeholder="/home/you/mps3-harness-1.1.0/overlays"
          value=${m.fpath} disabled=${!!m.busy} onInput=${(e) => { m.fpath = e.target.value; reset(); redraw(); }}
          onKeyDown=${(e) => { if (e.key === "Enter" && m.fpath.trim()) run(true); }} />
        <button type="button" class="btn sm" data-testid="import-folder-read" disabled=${!!m.busy || !m.fpath.trim()} onClick=${() => run(true)}>
          ${m.busy === "read" ? html`<${Spinner} />` : html`<${Icon} name="scan-search" />`}Read it</button></div>
      <div class="small muted">A folder holding one sub-folder per design (a release's <code>overlays/</code>): an absolute path on harness-manager-daemon's host. Each design is checked and imported on its own; one that does not fit never stops the others.</div>`;
  } else {
    src = html`<div class="field" style="flex-wrap:wrap"><label for="imp-path">Path</label>
        <input id="imp-path" class="input mono grow" data-testid="import-path" placeholder="/home/you/builds/blinky_rm/out/blinky_rm_build.json"
          value=${m.path} disabled=${!!m.busy} onInput=${(e) => { m.path = e.target.value; reset(); redraw(); }}
          onKeyDown=${(e) => { if (e.key === "Enter" && m.path.trim()) run(true); }} />
        <button type="button" class="btn sm" data-testid="import-read" disabled=${!!m.busy || !m.path.trim()} onClick=${() => run(true)}>
          ${m.busy === "read" ? html`<${Spinner} />` : html`<${Icon} name="scan-search" />`}Read it</button></div>
      <div class="small muted">An overlay folder (or its manifest.json), a build receipt, or the build directory holding one: an absolute path on harness-manager-daemon's host. A zip: choose it (A zip).</div>`;
  }

  const res = m.res;
  const refused = !!m.err && !!res;
  const failed = !!m.err && !res;
  const cand = res ? html`<div class="imp-picked" data-testid="import-picked">
      <${Icon} name=${res.kind === "receipt" ? "file-code" : "folder-input"} /><b>${res.name}</b><span class="mono muted">${res.rm_id}</span>
      <span class="muted">·</span><span class="secondary">${res.kind === "receipt" ? "build receipt" : "overlay folder"} for <span class="mono">${res.static_id}</span></span>
      ${res.upload ? html`<span class="muted">· ${res.upload.name}, ${bytesText(res.upload.bytes)}</span>` : html`<span class="mono small muted" style="overflow-wrap:anywhere">${res.path}</span>`}</div>`
    : m.way === "file" && m.file ? html`<div class="imp-picked" data-testid="import-picked"><${Icon} name="binary" /><b>${m.file.name}</b>
      <span class="secondary">zip · ${bytesText(m.file.size)}</span></div>` : null;
  const shadow = m.done && m.done.imported && m.done.imported.shadowed_by;
  const ready = m.way === "file" ? !!m.file : m.way === "build" ? !!m.pick : m.way === "folder" ? !!m.fpath.trim() : !!m.path.trim();
  const fol = m.folder;
  const foot = m.done
    ? html`<span class="small muted grow">In the overlay store: the Workbench lists it for every board on ${m.done.static_id}</span>
        <button type="button" class="btn primary" data-testid="import-done" onClick=${() => { closeModal(); pickOnWorkbench(bid, m.done.name); }}>
          <${Icon} name="check" />Pick it on the Workbench</button>`
    : html`<span class="small muted grow">Nothing is written to the board: an import only adds to this machine's overlay store</span>
        <button type="button" class="btn ghost" disabled=${!!m.busy} onClick=${closeModal}>${refused || failed ? "Close" : "Cancel"}</button>
        <button type="button" class="btn primary" data-testid="import-go" disabled=${!!m.busy || !ready} onClick=${() => run(false)}>
          ${m.busy === "import" ? html`<${Spinner} />` : html`<${Icon} name="shield-check" />`}Check and import</button>`;
  return html`<${ModalShell} title=${`Import a design for ${board}`} icon="folder-input" cls="mid imp-modal" foot=${foot} testid="import-dialog">
    <div class="imp-ways" role="group" aria-label="Three ways in">${WAYS.map((w) => html`<button type="button" key=${w.k} class="imp-way"
        aria-pressed=${m.way === w.k ? "true" : "false"} data-testid=${`import-way-${w.k}`} disabled=${!!m.busy}
        onClick=${() => { m.way = w.k; reset(); redraw(); }}><b><${Icon} name=${w.icon} />${w.t}${w.k === "build" && built.length ? ` · ${built.length}` : ""}</b><span>${w.d}</span></button>`)}</div>
    ${m.done ? null : src}
    ${m.note ? html`<${Reason} level="warn" testid="import-note" text=${m.note} />` : null}
    ${m.way === "folder" ? null : cand}
    ${m.way === "folder" ? null : html`<div>
      <div class="strip-label bd-sect">What Harness Manager checks before it accepts</div>
      <${Groups} bid=${bid} res=${res} running=${m.busy === "import" || m.busy === "read"} />
    </div>`}
    ${fol ? html`<div data-testid="import-folder-results">
      <div class="strip-label bd-sect">${fol.check_only ? "Read, nothing imported yet" : "Result"}: ${fol.results.length} design${fol.results.length === 1 ? "" : "s"} in <span class="mono">${fol.path}</span></div>
      <ul class="imp-list" data-testid="import-folder-list">${fol.results.map((r) => html`<li key=${r.path} data-name=${r.name} data-state=${r.state}>
        <div><div class="nm">${r.name}<span class="muted mono small">${r.rm_id}</span>
          <${Chip} level=${r.state === "imported" || r.state === "ready" ? "ok" : r.state === "skipped" ? "warn" : "err"} cls="bd-mini" testid="import-folder-state">${r.state === "skipped" ? "skipped" : r.state}<//></div>
          <div class="sub" data-testid="import-folder-text">${folderRowText(r)}</div></div></li>`)}</ul>
      ${!fol.check_only && fol.counts.imported ? html`<div class="outcome ok" role="status" data-testid="import-folder-ok"><${Icon} name="circle-check" /><span>${fol.counts.imported} imported into the overlay store${fol.counts.skipped ? `, ${fol.counts.skipped} skipped (another static)` : ""}${fol.counts.refused ? `, ${fol.counts.refused} refused` : ""}. The Workbench lists them.</span></div>` : null}
      ${!fol.check_only && !fol.counts.imported ? html`<${Reason} level="warn" testid="import-folder-none" text=${`Nothing was imported${fol.counts.skipped ? `: ${fol.counts.skipped} built for another static` : ""}${fol.counts.refused ? `${fol.counts.skipped ? "," : ":"} ${fol.counts.refused} refused` : ""}.`} />` : null}
    </div>` : null}
    ${m.done ? html`<div class="outcome ok" role="status" data-testid="import-ok"><${Icon} name="circle-check" /><span>Imported <b>${m.done.name}</b> <span class="mono">${m.done.rm_id}</span> into the overlay store${m.done.overlay_dir ? html` (packed in <span class="mono">${m.done.overlay_dir}</span>)` : ""}. The Workbench lists it for every board on ${m.done.static_id}: pick it, tick Arm, then Program.</span></div>` : null}
    ${shadow ? html`<${Reason} level="warn" testid="import-shadowed" text=${`The Workbench lists ${shadow} instead: the same name, rm_id and static, and the first one found wins. Rename your design to see yours.`} />` : null}
    ${res && !m.err && !m.done && res.passed ? html`<${Reason} level="ok" testid="import-checked" text=${`${res.name} passes: Check and import puts it in the overlay store.`} />` : null}
    ${res && !m.err && !m.done && !res.passed ? html`<${Reason} level="err" testid="import-would-refuse" text=${`${res.name} would be refused: ${(res.groups || []).filter((g) => g.state === "mismatch").map((g) => `${g.title}: ${g.detail}`).join("; ")}.`} />` : null}
    ${m.err ? html`<div class="outcome err" role="alert" data-testid="import-refused" data-error=${m.err.errName}><${Icon} name="circle-x" /><span>
        <b>${refused ? "Refused" : "Not imported"}</b> <span class="exit-code" data-testid="import-exit">${exitOf(m.err)}</span>${" "}${m.err.message}${m.err.hint ? html` <span class="muted">(${m.err.hint})</span>` : ""}. Nothing was imported.
        ${m.err.errName === "INCOMPATIBLE" ? html` Rebuild it against this board's kit (the Build tab), or import it on a board that runs ${res ? res.static_id : "its shell"}.` : null}</span></div>` : null}
    ${res && res.kind === "receipt" && m.way !== "file" ? html`<div><div class="small muted">The same from a shell</div>
      <div class="copy-row"><code>harness-manager kit pack ${res.path} --import</code></div></div>` : null}
  <//>`;
}

registerModal("import", ImportDialog);
