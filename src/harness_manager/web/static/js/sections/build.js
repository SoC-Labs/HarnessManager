// Build (KIT-UI, david K9): from "I have RTL" to "it is in Program", for the static this
// board runs. Between XDC and Program in the section list.
//
// The six step cards come from GET /boards/{bid}/guide: KIT-CORE's guide engine works out
// every state (done, next, blocked, failed, unchecked); this page never guesses one. Each
// card carries the actions its step needs:
//
// - Kit: fetch it (a job, with its progress), verify it, download it; kits are public and
//   the card shows the kit's licence note (K2).
// - Tools: the Vivado found against the release the kit needs. HM warns on a different
//   major.minor; the generated build_rm.tcl refuses one (K4).
// - Wrapper and XDC: the design's XDC checks and its rm_id; HM proposes a user rm_id and
//   warns on a clash (K8). Links to the XDC section.
// - Build: build_rm.tcl generated for the design (files, a zip, or a build directory), and
//   the Vivado command to copy. HM does not run Vivado here: it runs on the user's machine,
//   driven by HM's script and checks (K6).
// - Check and add: the build's receipt checked, then packed into Program.
//
// "When it goes wrong" holds one card per gate of build_rm.tcl and one per check that
// refused; a card whose gate or check failed opens itself. The words come from the guide
// (troubleshooting), the same text as `harness-manager kit guide --why GATE`.
//
// Paths are on harness-manager-daemon's host (docs/API.md). A browser on another machine is
// told so, and offered the zips first.

import { ApiError, call, callBlob, routeMissing, toApiError, waitJob } from "../api.js";
import { panelState } from "../actions.js";
import { bytesText, boardName } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, S as APP, setSection } from "../store.js";
import { ActionRow, Card, Chip, CopyButton, Icon, Reason, ResultBlock, Seg, Spinner } from "../ui.js";

export const STAGES = ["preflight", "synth", "link", "impl", "verify", "bitstream"];

const STATE_LOOK = {
  done: { level: "ok", icon: "circle-check", text: "Done" },
  next: { level: "accent", icon: "chevron-right", text: "Next" },
  blocked: { level: "", icon: "circle-dashed", text: "Blocked" },
  failed: { level: "err", icon: "circle-x", text: "Failed" },
  unchecked: { level: "unk", icon: "circle-help", text: "Unchecked" },
};

const HOW = { env: "$HARNESS_MANAGER_VIVADO", setting: "in the settings (tools.vivado)",
  path: "on PATH", xilinx_vivado: "$XILINX_VIVADO",
  "install root": "a standard install root" };

// --- pure helpers (exported for the browser tests) ------------------------------------------

// Is the page served from this machine? Loopback names only: anything else means the paths
// the page shows are on another machine (harness-manager-daemon's).
export function isLoopbackHost(hostname) {
  const h = String(hostname || "").replace(/^\[|\]$/g, "").toLowerCase();
  return h === "localhost" || h.endsWith(".localhost") || h === "::1" || /^127\./.test(h);
}

export function majorMinor(v) { return String(v || "").split(".").slice(0, 2).join("."); }

// The card a check uses (docs/API.md: "partial: <name>" -> <name>; xdc:<code> -> xdc).
export function cardFor(name, checks) {
  const key = String(name || "").replace(/^(partial|clearing):\s*/, "");
  if (checks && checks[key]) return key;
  if (key.startsWith("xdc:") && checks && checks.xdc) return "xdc";
  return key;
}

export function checkLevel(state) {
  return state === "ok" ? "ok" : state === "mismatch" ? "err" : state === "warning" ? "warn" : "unk";
}

function checkIcon(state) {
  return state === "ok" ? "circle-check" : state === "mismatch" ? "circle-x"
    : state === "warning" ? "triangle-alert" : "circle-help";
}

// Where a receipt ended, stage by stage: done | failed | "" (not reached).
export function stageStates(receipt) {
  if (!receipt) return STAGES.map(() => "");
  const at = STAGES.indexOf(receipt.stage);
  return STAGES.map((s, i) => {
    if (receipt.state === "passed") return "done";
    if (at < 0) return "";
    if (i < at) return "done";
    if (i === at) return receipt.state === "failed" ? "failed" : "done";
    return "";
  });
}

// --- per-board state ---------------------------------------------------------------------------

const K = {};
function st(bid) {
  if (!K[bid]) {
    K[bid] = {
      guide: null, guideError: null, guideLoading: false, guideAt: 0,
      kit: null, kitError: null,
      cat: null, catError: null,
      designMode: "builtin", design: "", designFile: "", designJson: "",
      buildDir: "",
      source: "auto", sourcePath: "",
      verify: null, verifyError: null, verifyBusy: false, zipBusy: false, zipSaved: "",
      script: null, scriptError: null, scriptBusy: "", scriptTab: "", scriptSaved: "",
      receiptPath: "", check: null, checkFor: "", pack: null, packError: null,
      loaded: false,
    };
  }
  return K[bid];
}

// A daemon from before KIT-CORE answers GET /boards/{bid}/guide from its greedy /boards/{bid}
// route ("<board>/guide is not open", 404), and POST /kits/... with "no such endpoint".
function explain(err) {
  const e = toApiError(err);
  if ((e.status === 404 && /\/(guide|kit)\b/.test(e.message || "")) || routeMissing(e)) {
    return new ApiError({ name: "UNAVAILABLE", message: "this harness-manager-daemon has no build-kit routes yet",
      hint: "update Harness Manager (the build kit arrived with lane KIT-CORE)" }, 404);
  }
  return e;
}

function errText(e) {
  return `${e.errName}: ${e.message}${e.hint ? ` (${e.hint})` : ""}`;
}

// The design the guide and the script are given: a built-in name, an absolute path on the
// daemon's host, or (script only) the pasted object. The guide is a GET: a pasted design
// cannot reach it, so its checks come from Generate script instead.
function designForGuide(x) {
  if (x.designMode === "builtin") return x.design || "";
  if (x.designMode === "file") return x.designFile.trim();
  return "";
}

function designForScript(x) {
  if (x.designMode === "paste") {
    try { return JSON.parse(x.designJson); } catch (e) {
      throw new ApiError({ name: "USAGE", message: `the pasted design is not JSON: ${e.message}` }, 400);
    }
  }
  const d = designForGuide(x);
  if (!d) throw new ApiError({ name: "USAGE", message: "pick a design first" }, 400);
  return d;
}

function designName(x) {
  if (x.designMode === "paste") {
    try { return String(JSON.parse(x.designJson).name || "design"); } catch (e) { return "design"; }
  }
  const d = designForGuide(x);
  return d.split(/[\\/]/).pop().replace(/\.json$/, "") || "design";
}

export async function loadGuide(bid) {
  const x = st(bid);
  const seq = (x.guideSeq || 0) + 1;       // a slower, older answer never overwrites a newer one
  x.guideSeq = seq;
  x.guideLoading = true;
  changed();
  try {
    const q = { design: designForGuide(x), build_dir: x.buildDir.trim() };
    const [g, k] = await Promise.all([
      call("boardGuide", { bid }, undefined, q),
      call("boardKit", { bid }).catch((e) => ({ error: e })),
    ]);
    if (seq !== x.guideSeq) return;
    x.guide = g.data;
    x.guideError = null;
    x.kit = k.error ? null : k.data;
    x.kitError = k.error ? explain(k.error) : null;
    x.guideAt = Date.now();
    const r = x.guide.receipt;
    if (r && r.path && (!x.receiptPath || x.receiptAuto)) {
      x.receiptPath = r.path;
      x.receiptAuto = true;
    }
  } catch (e) {
    if (seq !== x.guideSeq) return;
    x.guideError = explain(e);
  }
  x.guideLoading = false;
  changed();
}

async function loadCatalogue(bid) {
  const x = st(bid);
  try {
    x.cat = (await call("boardXdc", { bid })).data;
    const rm = (x.cat.designs || []).filter((d) => d.kit === "rm-kit");
    if (!x.design) x.design = (x.cat.default_design || {})["rm-kit"] || (rm[0] || {}).name || "";
  } catch (e) {
    x.catError = toApiError(e);
  }
  changed();
}

function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// --- small pieces ------------------------------------------------------------------------------

function StateChip({ state, testid }) {
  const look = STATE_LOOK[state] || STATE_LOOK.blocked;
  return html`<${Chip} level=${look.level} icon=${look.icon} testid=${testid} cls="state-chip">${look.text}<//>`;
}

function Cmd({ text, testid = "", wrap = false }) {
  return html`<div class=${`copy-row ${wrap ? "wrap" : ""}`} data-testid=${testid || undefined}>
    <code title=${text}>${text}</code><${CopyButton} text=${text} /></div>`;
}

function Actions({ step }) {
  const acts = (step.actions || []).filter((a) => a.text);
  if (!acts.length) return null;
  return html`<div class="cmd-list" data-testid=${`step-${step.id}-actions`}>
    ${acts.map((a, i) => html`<${Cmd} key=${i} text=${a.text} wrap=${true} />`)}</div>`;
}

function CheckRows({ rows }) {
  return html`<ul class="checks compact">${rows.map((c, i) => html`
    <li key=${`${c.name}-${i}`} data-check=${c.name} data-state=${c.state}>
      <${Icon} name=${checkIcon(c.state)} cls=${`i-${checkLevel(c.state)}`} />
      <div><div class="name mono">${c.name}</div><div class="detail">${c.detail}</div></div>
      <${Chip} level=${checkLevel(c.state)}>${String(c.state || "unchecked").toUpperCase()}<//>
    </li>`)}</ul>`;
}

// What did not pass, as rows; what passed, folded into one line that opens to its rows.
function CheckList({ checks, testid = "", skip = null }) {
  const rows = (checks || []).filter((c) => !(skip && skip(c)));
  if (!rows.length) return null;
  const bad = rows.filter((c) => c.state !== "ok");
  const ok = rows.filter((c) => c.state === "ok");
  return html`<div class="check-block" data-testid=${testid || undefined}>
    ${bad.length ? html`<${CheckRows} rows=${bad} />` : null}
    ${ok.length ? html`<details class="ok-fold"><summary><${Icon} name="circle-check" cls="sm i-ok" />
      <span class="ok-names">${ok.length} passed: <span class="mono">${ok.map((c) => c.name).join(", ")}</span></span></summary>
      <${CheckRows} rows=${ok} /></details>` : null}
  </div>`;
}

function StepCard({ step, children = null, extra = null, title = "" }) {
  return html`<section class="card step-card" data-testid=${`step-${step.id}`} data-state=${step.state}
      aria-label=${`${step.n} ${step.title}`}>
    <div class="card-head">
      <h2 class="card-title"><span class="flow-n">${step.state === "done" ? html`<${Icon} name="check" cls="sm" />` : step.n}</span>
        ${title || step.title}</h2>
      <span class="spacer"></span>
      ${extra}
      <${StateChip} state=${step.state} testid=${`state-${step.id}`} />
    </div>
    <div class="card-body stack-sm">
      ${step.detail ? html`<p class="step-detail" data-testid=${`step-${step.id}-detail`}>${step.detail}</p>` : null}
      ${step.reason ? html`<${Reason} level=${step.state === "failed" ? "err" : ""} text=${step.reason}
        icon=${step.state === "blocked" ? "circle-dashed" : ""} testid=${`step-${step.id}-reason`} />` : null}
      ${children}
    </div>
  </section>`;
}

// --- the header: what is being built, where the paths live -----------------------------------

function PathsHint() {
  const host = window.location.hostname;
  const remote = !isLoopbackHost(host);
  return remote
    ? html`<${Reason} level="warn" testid="build-paths-hint" icon="triangle-alert"
        text=${`This page comes from harness-manager-daemon on ${host}: every path below is on that machine, not this one. To build on this machine, use the Download zip buttons.`} />`
    : html`<${Reason} testid="build-paths-hint" icon="info"
        text="Paths are on this machine: harness-manager-daemon runs here, and so does Vivado (HM gives the command; it does not run it)." />`;
}

function DesignPicker({ bid, x }) {
  const rm = x.cat ? (x.cat.designs || []).filter((d) => d.kit === "rm-kit") : [];
  const apply = () => loadGuide(bid);
  return html`<div class="build-inputs">
    <div class="field wrap"><label>Design</label>
      <${Seg} label="Design source" value=${x.designMode} onChange=${(v) => {
        x.designMode = v; x.script = null; x.scriptError = null; changed(); apply();
      }} options=${[
        { value: "builtin", label: "Built-in", icon: "list", title: "a design from the board pack's pin model" },
        { value: "file", label: "File", icon: "file-code", title: "a design .json on harness-manager-daemon's host (absolute path)" },
        { value: "paste", label: "Paste", icon: "copy", title: "paste a design as JSON (checked when you generate the script)" },
      ]} />
      ${x.designMode === "builtin"
        ? (rm.length
          ? html`<select class="select" aria-label="Design" data-testid="build-design" value=${x.design}
              onChange=${(e) => { x.design = e.target.value; x.script = null; x.scriptError = null; apply(); }}>
              ${rm.map((d) => html`<option key=${d.name} value=${d.name}>${d.name}${d.title ? `: ${d.title}` : ""}</option>`)}
            </select>`
          : html`<input class="input mono grow" aria-label="Design name" data-testid="build-design-name"
              placeholder="minimal" value=${x.design}
              onChange=${(e) => { x.design = e.target.value.trim(); apply(); }} />`)
        : x.designMode === "file"
          ? html`<input class="input mono grow" aria-label="Design file" data-testid="build-design-file"
              placeholder="/home/you/my_rm/my_rm.json" value=${x.designFile}
              onChange=${(e) => { x.designFile = e.target.value; x.script = null; apply(); }} />`
          : null}
    </div>
    ${x.designMode === "paste" ? html`<textarea class="input mono" rows="6" data-testid="build-design-json"
        aria-label="Design JSON" placeholder='{"kind": "rm", "name": "my_rm", "use": {"clkrst": {}, "gpio": {}}}'
        value=${x.designJson} onInput=${(e) => { x.designJson = e.target.value; }}></textarea>
      <p class="muted small">A pasted design lives only in this page: Generate script checks it. Save it as a file to have the guide track it.</p>` : null}
    <div class="field wrap"><label>Build directory</label>
      <input class="input mono grow" aria-label="Build directory" data-testid="build-dir"
        placeholder="optional: /home/you/builds/my_rm (absolute, on harness-manager-daemon's host)" value=${x.buildDir}
        onChange=${(e) => { x.buildDir = e.target.value; apply(); }} />
    </div>
  </div>`;
}

function Head({ bid, x }) {
  const g = x.guide;
  const row = APP.boards[bid] || {};
  const cand = (boardState(bid).info && boardState(bid).info.candidate) || row.candidate || {};
  const nxt = g && g.next && g.steps.find((s) => s.id === g.next.step);
  const refresh = html`<button type="button" class="btn ghost sm" data-testid="build-refresh"
    onClick=${() => loadGuide(bid)} aria-busy=${x.guideLoading ? "true" : undefined}>
    ${x.guideLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Refresh</button>`;
  return html`<${Card} title=${`Build a DUT for ${boardName(cand, bid)}`} icon="file-cog" testid="build-head" actions=${refresh}
      sub="From your RTL to an overlay in Program. Each step's state is what Harness Manager can see from here.">
    <div class="actions">
      ${g ? html`<dl class="kv">
        <dt>Static</dt><dd class="mono" data-testid="build-static">${g.static_id || "unknown"}</dd>
        <dt>Kit</dt><dd class="mono" data-testid="build-kit-id">${g.kit_id || html`<span class="muted">not cached yet</span>`}</dd>
        <dt>Next</dt><dd data-testid="build-next">${nxt ? html`<strong>${nxt.n} ${nxt.title}</strong>` : g.steps.every((s) => s.state === "done")
          ? html`<span class="line"><${Chip} level="ok" icon="circle-check">Every step is done<//>
              <button type="button" class="btn sm" onClick=${() => setSection(bid, "program")}><${Icon} name="upload" /> Program it</button></span>`
          : html`<span class="muted">nothing can be done from here: see the failed step</span>`}</dd>
      </dl>` : null}
      <${DesignPicker} bid=${bid} x=${x} />
      <${PathsHint} />
      ${x.guideError ? html`<${Reason} level="err" testid="build-error" text=${errText(x.guideError)} />` : null}
    </div>
  <//>`;
}

// --- 1 target ------------------------------------------------------------------------------------

function TargetCard({ step }) {
  return html`<${StepCard} step=${step}><${Actions} step=${step} /><//>`;
}

// --- 2 tools (K4; KIT-LIC: does it start, and which device licence) -----------------------------

function ToolsCard({ step, g }) {
  const v = g.vivado || {};
  const need = (g.profile && g.profile.vivado) || "";
  const needBuild = (g.profile && g.profile.vivado_build) || 0;
  const have = v.found ? v.version || "" : "";
  const mismatch = !!(need && have && majorMinor(have) !== majorMinor(need));
  const buildDiff = !!(need && have && !mismatch && needBuild && v.build && v.build !== needBuild);
  const licence = String(step.detail || "").split(/; licence: /)[1] || "";
  const launch = v.launch;                  // KIT-LIC: the kit's Vivado launched once
  const check = (step.checks || [])[0];
  return html`<${StepCard} step=${{ ...step, detail: "" }}>
    <dl class="kv">
      <dt>Needed</dt><dd data-testid="vivado-needed">${need
        ? html`<span class="mono">Vivado ${need}</span>${needBuild ? html` <span class="sub">build ${needBuild}</span>` : null} <span class="sub">(the kit's release)</span>`
        : html`<span class="muted">known once the kit is fetched</span>`}</dd>
      <dt>Found</dt><dd data-testid="vivado-found">${v.found
        ? html`<div class="line"><span class="mono">Vivado ${have || "?"}</span>
            ${need ? (mismatch ? html`<${Chip} level="warn" icon="triangle-alert" testid="vivado-chip">different release<//>`
              : html`<${Chip} level=${buildDiff ? "warn" : "ok"} icon=${buildDiff ? "triangle-alert" : "circle-check"} testid="vivado-chip">${buildDiff ? "other build" : "matches"}<//>`) : null}</div>
          <div class="sub mono">${v.path}</div><div class="sub">found ${HOW[v.how] || v.how || "?"}${v.error ? `; ${v.error}` : ""}</div>`
        : html`<span class="muted">none: ${v.reason || "not found"}</span>`}</dd>
      ${(v.others || []).length ? html`<dt>Also installed</dt><dd>${v.others.map((o) => html`<div key=${o.path} class="sub mono">${o.version} ${o.path}</div>`)}</dd>` : null}
      ${launch ? html`<dt>Starts</dt><dd data-testid="vivado-launch"><div class="line"><${Chip} testid="vivado-launch-chip"
          level=${launch.state === "ok" ? "ok" : launch.state === "failed" ? "err" : "unk"}
          icon=${launch.state === "ok" ? "circle-check" : launch.state === "failed" ? "circle-x" : "circle-help"}>${
          launch.state === "ok" ? "starts" : launch.state === "failed" ? "does not start" : "unchecked"}<//></div>
        <div class="sub">${launch.detail}</div></dd>` : null}
      <dt>Licence</dt><dd data-testid="licence"><div class="line"><${Chip} level="unk" icon="circle-help">unchecked<//></div>
        <div class="sub">${String(licence || "").replace(/^unchecked\s*\((.*)\)$/, "$1") || "only synthesis can tell"}</div></dd>
    </dl>
    ${mismatch ? html`<${Reason} level="warn" testid="vivado-mismatch"
      text=${`Vivado ${have} is not this kit's ${need}: a checkpoint opens only in the release that wrote it, and build_rm.tcl refuses to start in another. Harness Manager only warns; install ${need}, or point $HARNESS_MANAGER_VIVADO at it.`} />` : null}
    ${!mismatch && check && check.state === "warning" && v.found ? html`<${Reason} level="warn" text=${check.detail} />` : null}
    <${Actions} step=${step} />
  <//>`;
}

// --- 3 kit (K2) ----------------------------------------------------------------------------------

function progressText(d) {
  const phase = d.phase || "fetch";
  if (d.total > 0) return `${phase}: ${bytesText(d.done)} of ${bytesText(d.total)}`;
  if (d.done > 0) return `${phase}: ${bytesText(d.done)}`;
  return `${phase}: started`;
}

function KitCard({ bid, step, g, x }) {
  const b = x.kit || {};
  const kit = b.kit;
  const sid = g.static_id;
  const p = panelState(bid, "kit_fetch");
  const fetchSpec = {
    key: "kit_fetch", label: kit ? "Fetch again" : "Fetch the kit", busyLabel: "Fetching...", budgetS: 600,
    command: `kit fetch ${bid}${x.source === "path" && x.sourcePath.trim() ? ` --source ${x.sourcePath.trim()}` : ""}`,
    run: async (ctx) => {
      const body = { board_id: bid };
      if (x.source === "path") {
        if (!x.sourcePath.trim()) throw new ApiError({ name: "USAGE", message: "name the kit's folder or zip first" }, 400);
        body.source = x.sourcePath.trim();
      }
      const { data } = await call("kitFetch", {}, body);
      const seen = new Set();
      const show = (d) => { seen.add(d.phase || "fetch"); ctx.progress(progressText(d), d.phase || "fetch"); };
      try {
        return await waitJob(data.job, { onProgress: show });
      } finally {
        // A fast job can end before its progress events reach the page: its record keeps
        // every phase it went through.
        try {
          const rec = (await call("job", { id: data.job })).data;
          for (const ph of rec.phases || []) {
            if (!seen.has(ph)) { seen.add(ph); ctx.progress(progressText({ phase: ph }), ph); }
          }
        } catch (e) { /* the result still stands */ }
      }
    },
    render: (r) => [
      { kind: "ok", text: `${r.kit.kit_id} is cached (from ${r.source}), ${bytesText(r.kit.size)}` },
      ...(r.checks || []).filter((c) => c.state !== "ok").map((c) => (c.state === "mismatch"
        ? { kind: "err", name: c.name, text: c.detail } : { kind: "hint", text: `${c.name}: ${c.detail}` })),
    ],
    renderError: (e) => ((e.data && e.data.checks) || []).filter((c) => c.state === "mismatch")
      .map((c) => ({ kind: "hint", text: `${c.name}: ${c.detail}` })),
    onDone: () => { x.verify = null; loadGuide(bid); },
  };
  const verify = async () => {
    x.verifyBusy = true; x.verifyError = null; changed();
    try { x.verify = (await call("kitGet", { static_id: sid })).data; } catch (e) { x.verify = null; x.verifyError = toApiError(e); }
    x.verifyBusy = false; changed();
  };
  const zip = async () => {
    x.zipBusy = true; x.verifyError = null; changed();
    try {
      const name = `mps3-kit-${sid}.zip`;
      saveBlob(await callBlob("kitZip", { static_id: sid }), name);
      x.zipSaved = name;
    } catch (e) { x.verifyError = toApiError(e); }
    x.zipBusy = false; changed();
  };
  const vbad = x.verify ? (x.verify.checks || []).filter((c) => c.state === "mismatch") : [];
  return html`<${StepCard} step=${step}>
    ${kit ? html`<dl class="kv" data-testid="kit-summary">
      <dt>Kit</dt><dd><span class="mono">${kit.kit_id}</span> <span class="sub">${bytesText(kit.size)}, ${kit.files} files</span></dd>
      <dt>Vivado</dt><dd class="mono">${(kit.vivado || {}).release || "?"}</dd>
      <dt>From</dt><dd class="mono sub">${kit.source}</dd>
      <dt>Access</dt><dd data-testid="kit-access"><${Chip} level=${kit.access === "public" ? "ok" : "warn"}>${kit.access || "?"}<//></dd>
      <dt>Licence</dt><dd data-testid="kit-licence">${kit.licence_note || html`<span class="muted">the kit gives no licence note</span>`}</dd>
    </dl>` : null}
    <${CheckList} checks=${step.checks} testid="kit-checks" />
    ${sid && (!kit || x.showFetch || step.state === "failed" || p.running || p.lines.length) ? html`<div class="field wrap"><label>Source</label>
      <${Seg} label="Kit source" value=${x.source} onChange=${(v) => { x.source = v; changed(); }} options=${[
        { value: "auto", label: "Automatic", title: "the cache, then the release channel, then the hub" },
        { value: "path", label: "A folder or zip", title: "a kit directory, a kit zip, or a fielded/<static>/ directory on harness-manager-daemon's host" },
      ]} />
      ${x.source === "path" ? html`<input class="input mono grow" aria-label="Kit source path" data-testid="kit-source-path"
        placeholder="/path/to/fielded/0x72BB0A36 (absolute, on harness-manager-daemon's host)" value=${x.sourcePath}
        onInput=${(e) => { x.sourcePath = e.target.value; }} />` : null}
    </div>
    ${x.source === "auto" && (b.sources || []).length ? html`<ul class="source-list" data-testid="kit-sources">${b.sources.map((s) => html`
      <li key=${s.name} data-source=${s.name} data-available=${s.available ? "true" : "false"}>
        <${Icon} name=${s.available ? "circle-check" : "circle-minus"} cls=${`sm ${s.available ? "i-ok" : "i-muted"}`} />
        <span class="mono">${s.name}</span>${s.reason ? html`<span class="sub">${s.reason}</span>` : null}</li>`)}</ul>` : null}
    <${ActionRow} bid=${bid} panel="kit_fetch" spec=${fetchSpec} variant=${kit ? "" : "primary"} icon="download" />
    <${ResultBlock} lines=${p.lines} panel=${p} testid="kit-fetch-result" />` : null}
    ${kit ? html`<div class="line">
      ${!(x.showFetch || step.state === "failed" || p.running || p.lines.length) ? html`<button type="button" class="btn sm"
        data-testid="kit-fetch-again" onClick=${() => { x.showFetch = true; changed(); }}><${Icon} name="download" /> Fetch again...</button>` : null}
      <button type="button" class="btn sm" data-testid="kit-verify" disabled=${x.verifyBusy} onClick=${verify}>
        ${x.verifyBusy ? html`<${Spinner} />` : html`<${Icon} name="shield-check" />`} Verify</button>
      <button type="button" class="btn sm" data-testid="kit-zip" disabled=${x.zipBusy} onClick=${zip}>
        ${x.zipBusy ? html`<${Spinner} />` : html`<${Icon} name="download" />`} Download kit zip</button>
    </div>
    ${x.verify ? html`<${Reason} level=${vbad.length ? "err" : "ok"} testid="kit-verified"
      text=${vbad.length ? `Verify: ${vbad.map((c) => `${c.name}: ${c.detail}`).join("; ")}` : `Verified: ${(x.verify.checks || []).map((c) => c.detail).join("; ")}`} />` : null}
    ${x.zipSaved ? html`<${Reason} level="ok" text=${`Saved ${x.zipSaved}.`} testid="kit-zip-saved" />` : null}` : null}
    ${x.verifyError ? html`<${Reason} level="err" text=${errText(x.verifyError)} />` : null}
    ${x.kitError ? html`<${Reason} level="warn" testid="kit-error" text=${`The kit's details could not be read: ${errText(x.kitError)}`} />` : null}
    ${!kit ? html`<${Actions} step=${step} />` : null}
  <//>`;
}

// --- 4 wrapper and XDC (K8) ----------------------------------------------------------------------

function RmId({ rm, proposed, checks, testid = "rm-id" }) {
  if (!rm) return null;
  const clash = (checks || []).find((c) => c.name === "rm_id_clash" && c.state !== "ok");
  const range = (checks || []).find((c) => c.name === "rm_id_range" && c.state !== "ok");
  const free = (checks || []).find((c) => c.name === "rm_id_clash" && c.state === "ok");
  return html`<div data-testid=${testid}>
    <div class="line"><span class="mono">rm_id ${rm}</span>
      ${proposed ? html`<${Chip} level="accent" testid="rm-id-proposed">proposed by HM<//>` : html`<${Chip}>from the design<//>`}
      ${free && !clash ? html`<span class="sub">${free.detail}</span>` : null}</div>
    ${proposed ? html`<p class="sub">User designs take design ids 0x8000-0xFFFF; the same name proposes the same id on every machine. Add <code>"rm_id": "${rm}"</code> to the design to keep it.</p>` : null}
    ${clash ? html`<${Reason} level="warn" testid="rm-id-clash" text=${clash.detail} />` : null}
    ${range ? html`<${Reason} level="warn" text=${range.detail} />` : null}
  </div>`;
}

function WrapperCard({ bid, step, g, x }) {
  const pasted = x.designMode === "paste";
  const s = x.script;
  const shown = pasted && (s || x.scriptError)
    ? { ...step, state: x.scriptError ? "failed" : "done",
      detail: x.scriptError ? `the pasted design: ${x.scriptError.message}` : `the pasted design ${s.design}: every XDC check passed (checked by Generate script)`,
      reason: "", actions: [] }
    : pasted ? { ...step, detail: "a pasted design is checked when you generate the script (5 Build)", actions: [] } : step;
  const checks = pasted ? ((s && s.checks) || ((x.scriptError && x.scriptError.data && x.scriptError.data.checks) || [])) : step.checks;
  const rm = pasted ? (s ? { rm_id: s.rm_id, proposed: s.rm_id_proposed } : null) : (g.rm_id && g.rm_id.rm_id ? g.rm_id : null);
  const toXdc = html`<button type="button" class="btn ghost sm" data-testid="open-xdc" onClick=${() => setSection(bid, "xdc")}>
    <${Icon} name="file-code" /> XDC<${Icon} name="chevron-right" cls="sm" /></button>`;
  return html`<${StepCard} step=${shown} extra=${toXdc}>
    ${rm ? html`<${RmId} rm=${rm.rm_id} proposed=${rm.proposed} checks=${checks} testid="wrapper-rm-id" />` : null}
    <${CheckList} checks=${checks} testid="wrapper-checks" skip=${(c) => c.name.startsWith("rm_id")} />
    <${Actions} step=${shown} />
  <//>`;
}

// --- 5 build (K6) --------------------------------------------------------------------------------

function StageBar({ receipt }) {
  if (!receipt) return null;
  const states = stageStates(receipt);
  return html`<div data-testid="build-stages" data-state=${receipt.state}>
    <div class="steps">${STAGES.map((s, i) => html`<div key=${s} class=${`step ${states[i]}`} data-stage=${s} data-stage-state=${states[i] || "pending"}>
      <div class="bar"></div><span>${s}</span></div>`)}</div>
    <p class="sub mono">${receipt.path}</p>
  </div>`;
}

function BuildCard({ bid, step, g, x }) {
  const remote = !isLoopbackHost(window.location.hostname);
  const s = x.script;
  const generate = async () => {
    x.scriptBusy = "json"; x.scriptError = null; x.scriptSaved = ""; changed();
    try {
      const body = { static_id: g.static_id, design: designForScript(x) };
      if (x.buildDir.trim()) body.out_dir = x.buildDir.trim();
      x.script = (await call("guideScript", {}, body)).data;
      const names = Object.keys(x.script.files || {}).sort();
      x.scriptTab = names.includes("build_rm.tcl") ? "build_rm.tcl" : names[0] || "";
      if (x.script.out_dir && x.script.receipt) {
        x.receiptPath = `${x.script.out_dir.replace(/[\\/]$/, "")}/${x.script.receipt}`;
        x.receiptAuto = true;
      }
    } catch (e) {
      x.script = null;
      x.scriptError = toApiError(e);
    }
    x.scriptBusy = ""; changed();
    loadGuide(bid);
  };
  const zip = async () => {
    x.scriptBusy = "zip"; x.scriptError = null; changed();
    try {
      const name = `${s ? s.design : designName(x)}_build.zip`;
      saveBlob(await callBlob("guideScript", {}, { static_id: g.static_id, design: designForScript(x), format: "zip" }), name);
      x.scriptSaved = name;
    } catch (e) { x.scriptError = toApiError(e); }
    x.scriptBusy = ""; changed();
  };
  const cmd = s && (s.command || []).length ? (s.out_dir ? s.command.join(" ") : `cd ${s.design} && ${s.command.join(" ")}`) : "";
  const refused = x.scriptError && x.scriptError.data && x.scriptError.data.checks;
  const canGen = !!g.static_id && !!g.kit_id;
  return html`<${StepCard} step=${step}>
    <${StageBar} receipt=${g.receipt} />
    <div class="line">
      <button type="button" class=${`btn sm ${remote ? "" : "primary"}`} data-testid="script-generate" disabled=${!!x.scriptBusy || !canGen}
        title=${canGen ? "" : "fetch the kit first (3 Kit)"} onClick=${generate}>
        ${x.scriptBusy === "json" ? html`<${Spinner} />` : html`<${Icon} name="file-cog" />`} Generate script</button>
      <button type="button" class=${`btn sm ${remote ? "primary" : ""}`} data-testid="script-zip" disabled=${!!x.scriptBusy || !canGen}
        title=${canGen ? "build_rm.tcl, the XDCs, the skeleton and the kit, in one zip" : "fetch the kit first (3 Kit)"} onClick=${zip}>
        ${x.scriptBusy === "zip" ? html`<${Spinner} />` : html`<${Icon} name="download" />`} Download zip</button>
    </div>
    ${!canGen ? html`<${Reason} text="Generate script needs the kit (3 Kit)." icon="circle-dashed" />` : null}
    ${x.scriptSaved ? html`<${Reason} level="ok" testid="script-zip-saved" text=${`Saved ${x.scriptSaved}: unzip it, then run the command below in its ${s ? s.design : designName(x)}/ folder.`} />` : null}
    ${x.scriptError ? html`<${Reason} level="err" testid="script-error" text=${`${x.scriptError.status === 409 ? "Refused" : "Failed"}: ${errText(x.scriptError)}`} />` : null}
    ${refused ? html`<${CheckList} checks=${refused} testid="script-refused" skip=${(c) => c.state !== "mismatch"} />` : null}
    ${s ? html`<div class="stack-sm" data-testid="script-result">
      <${RmId} rm=${s.rm_id} proposed=${s.rm_id_proposed} checks=${s.checks} />
      <p class="sub">${Object.keys(s.files || {}).length} files${s.out_dir ? html`, written to <span class="mono">${s.out_dir}</span>` : ", not written (preview; the zip holds them with the kit)"}. The receipt will be <span class="mono">${s.receipt}</span>.</p>
      <div><div class="sub-head">Run it (Vivado ${(g.profile || {}).vivado || ""}, on the machine that holds the files)</div>
        <${Cmd} text=${cmd} testid="script-command" wrap=${true} /></div>
      <p class="sub">A small RM takes about 30 minutes on a quiet machine and up to an hour when the machine is loaded; nanosoc about 50 minutes; 4-8 GB of RAM. Vivado exits 0 even when a gate fails: the verdict is the receipt. Then Refresh.</p>
    </div>` : null}
    <${Actions} step=${step} />
  <//>`;
}

// --- 6 check and add -----------------------------------------------------------------------------

function CheckCard({ bid, step, g, x }) {
  const path = x.receiptPath.trim();
  const c = x.check;
  const checkSpec = {
    key: "kit_check", label: "Check", busyLabel: "Checking...", budgetS: 60,
    command: `kit check ${path || "?"}`,
    run: async () => {
      if (!path) throw new ApiError({ name: "USAGE", message: "name the receipt (or the build directory) first" }, 400);
      return (await call("kitCheck", {}, { path, board_id: bid })).data;
    },
    render: (d) => {
      const bad = (d.checks || []).filter((k) => k.state === "mismatch");
      const unk = (d.checks || []).filter((k) => k.state === "unchecked").length;
      return [d.passed
        ? { kind: "ok", text: `passed: ${(d.checks || []).filter((k) => k.state === "ok").length} ok${unk ? `, ${unk} unchecked (not a pass, does not block)` : ""}` }
        : { kind: "warnline", text: `refused: ${bad.map((k) => k.name).join(", ")}` }];
    },
    onDone: (ok, value) => { x.check = ok ? value : null; x.checkFor = ok ? path : ""; x.pack = null; x.packError = null; },
  };
  const addGuard = () => {
    if (!c) return "check the build first";
    if (x.checkFor !== path) return "the path changed since the check: check again";
    if (!c.passed) return "the check refused this build: see When it goes wrong";
    return "";
  };
  const packSpec = {
    key: "kit_pack", label: "Add to Program", busyLabel: "Adding...", budgetS: 60,
    command: `kit pack ${path || "?"} --import`,
    run: async () => (await call("kitPack", {}, { path, import: true })).data,
    render: (d) => [
      { kind: "ok", text: d.imported ? `${d.imported.name || "overlay"} rm_id ${d.imported.rm_id} is in `
        + (d.imported.shadowed_by ? "the store" : "Program") : "packed" },
      { kind: "out", text: `overlay ${d.overlay_dir}` },
      ...(d.imported && d.imported.shadowed_by ? [{ kind: "hint", text: `Program lists ${d.imported.shadowed_by} instead: `
        + "the same name, rm_id and static, and the first one found wins"
        + (d.imported.shadow_same_bits ? " (byte-identical bits)" : `; load this one with --overlay-dir ${d.overlay_dir}`) }] : []),
    ],
    renderError: (e) => ((e.data && e.data.checks) || []).filter((k) => k.state === "mismatch")
      .map((k) => ({ kind: "hint", text: `${k.name}: ${k.detail}` })),
    onDone: (ok, value) => {
      x.pack = ok ? value : null;
      x.packError = ok ? null : value;
      if (ok) { const b = boardState(bid); b.overlays = null; }    // Program reads the list again
      loadGuide(bid);
    },
  };
  const pc = panelState(bid, "kit_check");
  const pp = panelState(bid, "kit_pack");
  return html`<${StepCard} step=${step}>
    <div class="field wrap"><label>Receipt</label>
      <input class="input mono grow" aria-label="Build receipt" data-testid="receipt-path"
        placeholder="/home/you/builds/my_rm/out/my_rm_build.json (or the build directory)" value=${x.receiptPath}
        onInput=${(e) => { x.receiptPath = e.target.value; x.receiptAuto = false; changed(); }} />
    </div>
    <${ActionRow} bid=${bid} panel="kit_check" spec=${checkSpec} icon="shield-check" compact=${true} />
    <${ResultBlock} lines=${pc.lines} panel=${pc} testid="check-result" />
    ${c && x.checkFor === path ? html`<${CheckList} checks=${c.checks} testid="check-list" />` : null}
    <${ActionRow} bid=${bid} panel="kit_pack" spec=${packSpec} variant="primary" icon="upload"
      gate=${{ guard: addGuard }} compact=${true} />
    <${ResultBlock} lines=${pp.lines} panel=${pp} testid="pack-result" />
    ${x.pack && x.pack.imported ? html`<div class="line" data-testid="pack-done">
      <button type="button" class="btn sm" data-testid="open-program" onClick=${() => setSection(bid, "program")}>
        <${Icon} name="upload" /> Open Program</button>
      <span class="sub">then Consoles and Debug</span></div>` : null}
    <${Actions} step=${step} />
  <//>`;
}

// --- right column: the script's files, and when it goes wrong ----------------------------------

function FilesCard({ x }) {
  const s = x.script;
  const files = s ? Object.keys(s.files || {}).sort() : [];
  if (!files.length) return null;
  return html`<${Card} title="Script files" icon="file-code" testid="script-files"
      sub=${s.out_dir ? `Written to ${s.out_dir}, with the kit in kit/.` : "The zip holds these, with the kit in kit/."}>
    <div class="actions">
      <div class="file-tabs" role="group" aria-label="File">${files.map((f) => html`
        <button type="button" key=${f} class="btn ghost sm mono" data-file=${f} aria-pressed=${x.scriptTab === f ? "true" : "false"}
          onClick=${() => { x.scriptTab = f; changed(); }}>${f}</button>`)}</div>
      <pre class="result file-body" data-testid="script-file-body" data-file=${x.scriptTab}>${(s.files || {})[x.scriptTab] || ""}</pre>
    </div>
  <//>`;
}

// Every card that is failing now: {name, card, detail, level, from}.
export function failures(x) {
  const g = x.guide;
  if (!g) return [];
  const t = g.troubleshooting || { gates: [], checks: {} };
  const out = [];
  const add = (f) => { if (!out.some((o) => o.card === f.card)) out.push(f); };
  const r = g.receipt;
  if (r && r.state === "failed") {
    const failed = (r.gates || []).filter((v) => v.verdict === "FAIL");
    for (const v of failed) add({ name: v.gate, card: v.gate, detail: v.detail, level: "err", from: "the build" });
    if (!failed.length) add({ name: "tcl_error", card: "tcl_error", detail: `the build failed at ${r.stage}`, level: "err", from: "the build" });
  }
  const v = g.vivado || {};
  const need = (g.profile || {}).vivado;
  if (v.found && v.version && need && majorMinor(v.version) !== majorMinor(need)) {
    add({ name: "vivado_version", card: "vivado_version", level: "warn", from: "2 Tools",
      detail: `Vivado ${v.version} found; the kit needs ${need}: build_rm.tcl will refuse to start` });
  }
  const checkFails = (checks, from) => {
    for (const c of checks || []) {
      if (c.state !== "mismatch") continue;
      add({ name: c.name, card: cardFor(c.name, t.checks), detail: c.detail, level: "err", from });
    }
  };
  for (const step of g.steps || []) if (step.id !== "tools") checkFails(step.checks, `${step.n} ${step.title}`);
  if (x.scriptError && x.scriptError.data) checkFails(x.scriptError.data.checks, "Generate script");
  if (x.check && x.checkFor === x.receiptPath.trim()) checkFails(x.check.checks, "6 Check");
  if (x.packError && x.packError.data) checkFails(x.packError.data.checks, "Add to Program");
  return out;
}

function TroubleItem({ card, name, detail, level, from, fix, failing }) {
  return html`<details class="trouble" data-testid="trouble" data-card=${card}
      data-failing=${failing ? "true" : "false"} data-level=${level || undefined} open=${failing}>
    <summary><${Icon} name="chevron-right" cls="sm chev" />
      <span class="mono">${name}</span><span class="spacer"></span>
      ${failing ? html`<${Chip} level=${level === "warn" ? "warn" : "err"}>${level === "warn" ? "will fail" : "failed"}<//>` : null}</summary>
    <div class="trouble-body">
      ${detail ? html`<p data-testid="trouble-detail"><strong>${from ? `${from}: ` : ""}</strong>${detail}</p>` : null}
      <p data-testid="trouble-fix">${fix ? html`<strong>Fix: </strong>${fix}` : "The detail above says what differs."}</p>
    </div>
  </details>`;
}

function TroubleCard({ x }) {
  const g = x.guide;
  if (!g || !g.troubleshooting) return null;
  const t = g.troubleshooting;
  const fails = failures(x);
  const failing = new Set(fails.map((f) => f.card));
  const fixOf = (card) => (t.checks || {})[card] || ((t.gates || []).find((gg) => gg.gate === card) || {}).fix || "";
  return html`<${Card} title="When it goes wrong" icon="circle-help" testid="trouble-card" bodyCls="flush"
      sub=${fails.length ? `${fails.length} card${fails.length === 1 ? " is" : "s are"} open for what failed; the rest are every gate of build_rm.tcl.`
        : "One card per gate of build_rm.tcl; the one that fails opens itself."}>
    <div data-testid="trouble-list">
      ${fails.map((f) => html`<${TroubleItem} key=${`f-${f.card}`} ...${f} fix=${fixOf(f.card)} failing=${true} />`)}
      ${(t.gates || []).filter((gg) => !failing.has(gg.gate)).map((gg) => html`<${TroubleItem} key=${gg.gate}
        card=${gg.gate} name=${gg.gate} fix=${gg.fix} failing=${false} />`)}
    </div>
  <//>`;
}

// --- the section ---------------------------------------------------------------------------------

export function BuildSection({ bid }) {
  const x = st(bid);
  useEffect(() => {
    // Every visit reads the guide again (a build may have finished meanwhile); the design
    // catalogue once.
    if (!x.loaded) {
      x.loaded = true;
      loadCatalogue(bid).then(() => loadGuide(bid));
    } else {
      loadGuide(bid);
    }
  }, [bid]);
  const g = x.guide;
  const step = (id) => g && (g.steps || []).find((s) => s.id === id);
  return html`<div class="stack" data-testid="build-section">
    <${Head} bid=${bid} x=${x} />
    ${!g ? (x.guideError ? null : html`<p class="muted"><${Spinner} /> Reading the guide...</p>`)
      : html`<div class="grid split build-grid">
        <div class="build-steps" data-testid="build-steps">
          ${step("target") ? html`<${TargetCard} step=${step("target")} g=${g} />` : null}
          ${step("tools") ? html`<${ToolsCard} step=${step("tools")} g=${g} />` : null}
          ${step("kit") ? html`<${KitCard} bid=${bid} step=${step("kit")} g=${g} x=${x} />` : null}
          ${step("wrapper") ? html`<${WrapperCard} bid=${bid} step=${step("wrapper")} g=${g} x=${x} />` : null}
          ${step("build") ? html`<${BuildCard} bid=${bid} step=${step("build")} g=${g} x=${x} />` : null}
          ${step("check") ? html`<${CheckCard} bid=${bid} step=${step("check")} g=${g} x=${x} />` : null}
        </div>
        <div class="stack">
          <${FilesCard} x=${x} />
          <${TroubleCard} x=${x} />
        </div>
      </div>`}
  </div>`;
}
