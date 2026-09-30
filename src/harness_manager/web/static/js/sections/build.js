// Build (UI v2, lane UI2-BUILD; KIT-UI before it, david K9): from "I have RTL" to "it is on
// the Workbench", for the static this board runs. docs/planning/UI_V2_PLAN.md §1.4, the frozen
// round-3 design (docs/design/ui-v2/prototype-b-round3.html, BuildTab).
//
// A five-step bar (Setup · Design · Build · Check · Add) over ONE focused step panel. The
// states come from GET /boards/{bid}/guide (KIT-CORE's guide engine: target, tools, kit,
// wrapper, build, check) and the page's own record of what the user chose:
//
// - Setup = the guide's target + tools + kit. No Vivado on this machine is a warning, never a
//   block: the build directory can be written, or downloaded as a zip, and run elsewhere.
// - Design = an example (a built-in: the guide checks it), My RTL (POST /kits/design/scan: an
//   RTL folder or .f list, then the proposal as an inline design) or a pasted .json. An inline
//   design is checked by a preview of POST /guide/script (the guide is a GET and cannot see
//   it). Generics, the rm_xdc path and the partition's pblock facts (G8) live here too.
// - Build = POST /guide/script writes the build directory; "Run it your way" gives its batch,
//   GUI and open-session commands (G8 `run`). Harness Manager never runs Vivado: it watches the
//   directory (the guide's `running`: stage and elapsed time) until the receipt appears.
// - Check = the guide's check step and the receipt: four groups, the one failed gate with its
//   fix, and the utilisation of pblock_rp_dut (G8 `utilisation`).
// - Add = POST /kits/pack {import: true}; the Workbench opens with the design picked.
//
// Building needs no lease; only programming does (the note under the bar says who holds it).
// Paths are on harness-manager-daemon's host (docs/API.md). The Import dialog is ../import.js.

import { ApiError, call, callBlob, routeMissing, toApiError, waitJob } from "../api.js";
import { panelState } from "../actions.js";
import { boardName, bytesText, clock, hexId } from "../format.js";
import { noteBuilt, pickOnWorkbench } from "../import.js";
import { html, useEffect, useRef, useState } from "../lib.js";
import { registerTab } from "../route.js";
import { boardState, changed, navigate, S as APP, toast, writeRoute } from "../store.js";
import { ActionRow, Chip, CopyButton, Icon, Reason, ResultBlock, Seg, Spinner, useReveal } from "../ui.js";
import { leaseWho } from "../week.js";
import { BoardXdcSection } from "./xdc.js";

export const STAGES = ["preflight", "synth", "link", "impl", "verify", "bitstream"];
export const STEPS = [
  { k: "setup", l: "Setup" }, { k: "design", l: "Design" }, { k: "build", l: "Build" },
  { k: "check", l: "Check" }, { k: "add", l: "Add" },
];
const STATE_WORD = { done: "done", current: "now", running: "running", failed: "failed",
  blocked: "waits for an earlier step", warn: "done, with a warning" };
const HOW = { env: "$HARNESS_MANAGER_VIVADO", setting: "in the settings (tools.vivado)",
  path: "on PATH", xilinx_vivado: "$XILINX_VIVADO", "install root": "a standard install root" };
const WAYS = [
  { value: "batch", label: "Batch", icon: "terminal", title: "vivado -mode batch: Harness Manager follows every stage in build_rm.log" },
  { value: "gui", label: "Vivado GUI", icon: "monitor", title: "vivado -mode gui: the same script and log, in the GUI" },
  { value: "session", label: "Your open Vivado", icon: "keyboard", title: "paste into a Vivado you already have open: Harness Manager sees only the receipt" },
];
// the build_rm.tcl gates a change to the design fixes (Check's "Fix it in Design")
const DESIGN_GATES = new Set(["rm_id_format", "rm_id_nonzero", "source_present", "sources_given",
  "generic_file_present", "rm_xdc_present", "no_black_boxes", "boundary_bits", "rm_id_match",
  "rm_timing", "ooc_clocks", "drc_hdpr_link"]);
const SAMPLE = `{
 "kind": "rm", "name": "my_soc",
 "use": {"clkrst": {}, "uart": {}, "status": {}},
 "build": {
  "top": "rp_my_soc_wrapper",
  "sources": ["/home/you/rtl/my_soc/my_pkg.sv", "/home/you/rtl/my_soc/rp_my_soc_wrapper.sv"],
  "generics": {"IMEM_MEM_FPGA_IMG": {"path": "/home/you/rtl/my_soc/fw/hello_image.hex"}},
  "rm_xdc": "/home/you/rtl/my_soc/my_soc_rm.xdc"
 }
}`;

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

// "rm_nanosoc: 7,903 LUT (18.5%), 16.5 BRAM tiles (11.5%)" (the pin model's reference_use)
// -> {name: "rm_nanosoc", LUT: 7903, BRAM: 16.5}. Only what the text says; nothing is guessed.
export function referenceUse(text) {
  const t = String(text || "");
  const out = { name: (t.split(":")[0] || "").trim() };
  for (const m of t.matchAll(/([\d][\d,]*(?:\.\d+)?)\s+(LUT|FF|BRAM|DSP)\b/g)) {
    out[m[2]] = Number(m[1].replace(/,/g, ""));
  }
  return out;
}

const num = (n) => (Number.isInteger(n) ? n.toLocaleString("en-GB") : String(n));
const pctText = (p) => (p === 0 ? "0 %" : p < 0.1 ? "<0.1 %" : `${p.toFixed(1)} %`);
const mmss = (s) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`
  : `${Math.floor(s / 60)} min ${String(Math.floor(s % 60)).padStart(2, "0")} s`);
const absPath = (p) => /^\//.test(String(p || "").trim());

// --- per-board state, kept for this tab of the browser (sessionStorage) -------------------

const KEEP = ["src", "ex", "rtl", "rtlName", "scan", "gens", "rmXdc", "paste", "preview", "chosen",
  "buildDir", "jobs", "way", "stopAfter", "written", "run", "packed"];
const STORE_KEY = "harness_manager.build";
const K = {};

function saved(bid) {
  try {
    const all = JSON.parse(window.sessionStorage.getItem(STORE_KEY) || "{}");
    return (all && all[bid]) || {};
  } catch (e) { return {}; }
}

function persist(bid) {
  const x = K[bid];
  if (!x) return;
  try {
    const all = JSON.parse(window.sessionStorage.getItem(STORE_KEY) || "{}") || {};
    all[bid] = Object.fromEntries(KEEP.map((k) => [k, x[k]]));
    window.sessionStorage.setItem(STORE_KEY, JSON.stringify(all));
  } catch (e) { /* not remembered: the tab still works */ }
}

function st(bid) {
  if (!K[bid]) {
    K[bid] = {
      guide: null, guideError: null, guideLoading: false, guideAt: 0, guideSeq: 0,
      kit: null, kitError: null, cat: null, catError: null, loaded: false,
      src: "example", ex: "", rtl: "", rtlName: "", scan: null, scanError: null, scanBusy: false,
      gens: [], rmXdc: "", paste: "", preview: null, previewError: null, previewBusy: false,
      saveBusy: false, savedJson: "", saveError: null,
      chosen: false, buildDir: "", jobs: "4", way: "batch", stopAfter: false,
      script: null, scriptError: null, scriptBusy: "", written: "", run: null, zipSaved: "",
      packed: null, packError: null, packBusy: false,
      source: "auto", sourcePath: "", showFetch: false,
      verify: null, verifyError: null, verifyBusy: false, zipBusy: false, kitZipSaved: "",
      view: null, seen: null,
      ...saved(bid),
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

function errText(e) { return `${e.errName}: ${e.message}${e.hint ? ` (${e.hint})` : ""}`; }

function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

async function copyText(text, what = "Copied") {
  try { await navigator.clipboard.writeText(text); toast(what, { icon: "copy" }); } catch (e) {
    toast("The browser refused the clipboard: select the text instead", { icon: "circle-x", level: "err" });
  }
}

// --- the design ------------------------------------------------------------------------------

// The design as the guide reads it: a built-in's name. An inline design (My RTL, Paste) has
// no file the guide could open, so its checks come from the script's preview instead.
function guideDesign(x) { return x.src === "example" ? x.ex || "" : ""; }

function parsedPaste(x) {
  try { return { obj: JSON.parse(x.paste) }; } catch (e) { return { error: e.message }; }
}

// The generics of My RTL's editor as build.generics: a file is {"path": FILE}; a whole number
// stays a number.
function genericsObject(gens) {
  const out = {};
  for (const g of gens || []) {
    const k = String(g.k || "").trim();
    if (!k) continue;
    const v = String(g.v || "").trim();
    out[k] = g.path ? { path: v } : /^-?\d+$/.test(v) ? Number(v) : v;
  }
  return out;
}

// The design object an inline design sends (My RTL: the scan's proposal plus the page's
// generics and rm_xdc; Paste: the JSON), or null.
function inlineDesign(x) {
  if (x.src === "rtl") {
    if (!x.scan || !x.scan.design) return null;
    const d = JSON.parse(JSON.stringify(x.scan.design));
    d.build = d.build || {};
    const gens = genericsObject(x.gens);
    if (Object.keys(gens).length) d.build.generics = gens; else delete d.build.generics;
    if (x.rmXdc.trim()) d.build.rm_xdc = x.rmXdc.trim(); else delete d.build.rm_xdc;
    return d;
  }
  if (x.src === "paste") return parsedPaste(x).obj || null;
  return null;
}

function designArg(x) {
  if (x.src === "example") {
    if (!x.ex) throw new ApiError({ name: "USAGE", message: "pick a design first" }, 400);
    return x.ex;
  }
  const d = inlineDesign(x);
  if (!d) {
    const p = x.src === "paste" ? parsedPaste(x) : null;
    throw new ApiError({ name: "USAGE", message: p && p.error ? `the pasted design is not JSON: ${p.error}`
      : "scan your RTL first (Read it)" }, 400);
  }
  return d;
}

// A build directory under the home of a path this page has seen (the RTL folder, a pasted
// source, the kit's folder): "/home/you/builds/<name>". "" when no such path is known.
function suggestDir(x) {
  const p = x.src === "paste" ? parsedPaste(x).obj : null;
  const seen = [x.src === "rtl" ? x.rtl : "", ((p && p.build && p.build.sources) || [])[0] || "", x.sourcePath];
  for (const s of seen) {
    const m = String(s || "").trim().match(/^(\/home\/[^/]+|\/Users\/[^/]+)\//);
    if (m) return `${m[1]}/builds/${designName(x) || "my_rm"}`;
  }
  return "";
}

function designName(x) {
  if (x.src === "example") return x.ex || "";
  if (x.src === "rtl") return (x.scan && x.scan.name) || x.rtlName.trim() || "";
  const p = parsedPaste(x);
  return p.obj && p.obj.name ? String(p.obj.name) : "";
}

// A change to the design starts the build again (the prototype's bdReset).
function resetDesign(x) {
  Object.assign(x, { chosen: false, preview: null, previewError: null, written: "", run: null,
    script: null, scriptError: null, packed: null, packError: null, zipSaved: "" });
}

function troubleFix(g, name, checks) {
  const t = (g && g.troubleshooting) || { gates: [], checks: {} };
  const card = cardFor(name, t.checks);
  return (t.checks || {})[card] || ((t.gates || []).find((x) => x.gate === card) || {}).fix || "";
}

// What refuses the design now: {gate, title, text, fix} or null; `todo` is what is missing.
function designProblem(x) {
  const g = x.guide;
  if (x.src === "paste") {
    if (!x.paste.trim()) return { todo: "Paste your design .json: kind, name, use, and its build (the format: USER_GUIDE §7.2)." };
    const p = parsedPaste(x);
    if (p.error) return { err: { gate: "json", title: "the pasted design is not JSON", text: p.error,
      fix: "Paste the design .json exactly as it is in the file." } };
  }
  if (x.src === "rtl") {
    if (x.scanError) {
      return { err: { gate: x.scanError.errName, title: x.scanError.message, text: x.scanError.hint || "",
        fix: "Give the folder that holds your RTL, or its .f file list (an absolute path on harness-manager-daemon's host)." } };
    }
    if (!x.rtl.trim()) return { todo: "Give the folder that holds your RTL, or its .f file list." };
    if (!x.scan) return { todo: "Read it: Harness Manager lists the sources in compile order and finds the top." };
  }
  if (x.src === "example" && !x.ex) return { todo: "Pick an example." };
  if (x.previewError) {
    const bad = ((x.previewError.data && x.previewError.data.checks) || []).find((c) => c.state === "mismatch");
    return { err: bad ? { gate: bad.name, title: `${bad.name}: ${bad.detail}`, text: "", fix: troubleFix(g, bad.name) }
      : { gate: x.previewError.errName, title: x.previewError.message, text: x.previewError.hint || "", fix: "" } };
  }
  if (x.src === "example" && g) {
    const w = (g.steps || []).find((s) => s.id === "wrapper");
    if (w && w.state === "failed") {
      const bad = (w.checks || []).find((c) => c.state === "mismatch");
      return { err: { gate: bad ? bad.name : "wrapper", title: bad ? `${bad.name}: ${bad.detail}` : w.detail,
        text: w.reason || "", fix: bad ? troubleFix(g, bad.name) : "" } };
    }
  }
  return {};
}

// The checks of the design (the guide's wrapper step, or the script's preview).
function designChecks(x) {
  if (x.src === "example") {
    const w = x.guide && (x.guide.steps || []).find((s) => s.id === "wrapper");
    return (w && w.checks) || [];
  }
  return (x.preview && x.preview.checks) || ((x.previewError && x.previewError.data && x.previewError.data.checks) || []);
}

function rmIdOf(x) {
  if (x.src === "example") {
    const r = x.guide && x.guide.rm_id;
    return r && r.rm_id ? { rm_id: r.rm_id, proposed: !!r.proposed } : null;
  }
  if (x.preview && x.preview.rm_id) return { rm_id: x.preview.rm_id, proposed: !!x.preview.rm_id_proposed };
  if (x.src === "rtl" && x.scan) return { rm_id: x.scan.rm_id, proposed: !!x.scan.rm_id_proposed };
  const p = x.src === "paste" ? parsedPaste(x).obj : null;
  return p && p.rm_id ? { rm_id: String(p.rm_id), proposed: false } : null;
}

// --- reads -------------------------------------------------------------------------------------

export async function loadGuide(bid) {
  const x = st(bid);
  const seq = (x.guideSeq || 0) + 1;       // a slower, older answer never overwrites a newer one
  x.guideSeq = seq;
  x.guideLoading = true;
  changed();
  try {
    // the directory being watched: one the page wrote, or was told is written ("watch it")
    const dir = x.chosen && absPath(x.written) ? x.written.trim() : "";
    const q = { design: guideDesign(x), build_dir: dir };
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
    noteChecked(bid, x);
  } catch (e) {
    if (seq !== x.guideSeq) return;
    x.guideError = explain(e);
  }
  x.guideLoading = false;
  changed();
}

// A passed, checked build: the Import dialog's "From Build" lists it until it is added.
function noteChecked(bid, x) {
  const g = x.guide;
  const r = g && g.receipt;
  const c = g && (g.steps || []).find((s) => s.id === "check");
  if (!r || r.state !== "passed" || !c || c.state === "failed" || c.state === "blocked") return;
  noteBuilt(bid, { name: r.rm_name, rm_id: r.rm_id, static_id: r.static_id, path: r.path,
    gates: (r.gates || []).length, when: r.built || "", added: c.state === "done" });
}

async function loadCatalogue(bid) {
  const x = st(bid);
  try {
    x.cat = (await call("boardXdc", { bid })).data;
    const rm = (x.cat.designs || []).filter((d) => d.kit === "rm-kit");
    if (!x.ex) x.ex = (x.cat.default_design || {})["rm-kit"] || (rm[0] || {}).name || "";
  } catch (e) {
    x.catError = toApiError(e);
  }
  changed();
}

// --- the flow: five steps, their states, the current one and the one on view ------------------

function stepOf(g, id) { return g && (g.steps || []).find((s) => s.id === id); }

export function flow(bid) {
  const x = st(bid);
  const g = x.guide;
  const target = stepOf(g, "target");
  const tools = stepOf(g, "tools");
  const kitStep = stepOf(g, "kit");
  const checkStep = stepOf(g, "check");
  const r = x.chosen && g ? g.receipt : null;
  const running = x.chosen && g && g.running && g.running.fresh ? g.running : null;
  const prob = designProblem(x);
  const s = {};
  const sub = {};
  // Setup: the target and the kit gate everything; the tools only warn
  if (!g) { s.setup = "current"; sub.setup = "reading…"; }
  else if ([target, kitStep, tools].some((v) => v && v.state === "failed")) {
    s.setup = "failed";
    sub.setup = target && target.state === "failed" ? "target" : kitStep && kitStep.state === "failed" ? "kit" : "Vivado does not start";
  } else if (!target || target.state !== "done" || !kitStep || kitStep.state !== "done") {
    s.setup = "current"; sub.setup = "fetch the kit";
  } else if (!tools || tools.state !== "done") {
    s.setup = "warn"; sub.setup = toolsShort(g);
  } else { s.setup = "done"; sub.setup = "target · tools · kit"; }
  const setupOk = s.setup === "done" || s.setup === "warn";
  // Design
  sub.design = designName(x) || "pick one";
  if (!setupOk) s.design = "blocked";
  else if (prob.err) s.design = "failed";
  else if (x.chosen) s.design = "done";
  else s.design = "current";
  // Build
  if (s.design !== "done") { s.build = "blocked"; sub.build = "Vivado · 30-60 min"; }
  else if (running) {
    s.build = "running";
    sub.build = x.way === "session" ? "your Vivado" : `${running.stage} · ${Math.floor(elapsed(x, running) / 60)} min`;
  } else if (r && (r.state === "passed" || r.state === "failed")) {
    s.build = "done"; sub.build = r.state === "passed" ? "passed" : `failed at ${r.stage}`;
  } else if (r && r.state === "stopped") { s.build = "current"; sub.build = "stopped after link"; }
  else { s.build = "current"; sub.build = x.written ? "run Vivado" : "write the folder"; }
  // Check
  const fi = failInfo(x);
  if (!r || running || r.state === "stopped") { s.check = "blocked"; sub.check = "the receipt"; }
  else if (fi) { s.check = "failed"; sub.check = fi.gate; }
  else { s.check = "done"; sub.check = "passed"; }
  // Add
  const imported = !!(checkStep && checkStep.state === "done") || !!(x.packed && r && x.packed.path === r.path);
  if (s.check !== "done") { s.add = "blocked"; sub.add = "to the Workbench"; }
  else if (imported) { s.add = "done"; sub.add = "on the Workbench"; }
  else { s.add = "current"; sub.add = "to the Workbench"; }
  const steps = STEPS.map((v, i) => ({ ...v, n: i + 1, state: s[v.k], sub: sub[v.k] }));
  const cur = steps.find((v) => v.state !== "done" && v.state !== "warn") || null;
  const view = x.view && steps.some((v) => v.k === x.view) ? x.view : cur ? cur.k : "add";
  return { x, g, r, running, fi, prob, steps, cur, view, imported };
}

function toolsShort(g) {
  const v = (g && g.vivado) || {};
  const need = (g && g.profile && g.profile.vivado) || "";
  if (!v.found) return "no Vivado here";
  if (need && v.version && majorMinor(v.version) !== majorMinor(need)) return `Vivado ${v.version}: another release`;
  return "tools: a warning";
}

// Seconds since the running build began: the guide's elapsed_s plus the time since that read.
function elapsed(x, running) {
  const base = Number(running.elapsed_s) || 0;
  return Math.max(0, base + (Date.now() - (x.guideAt || Date.now())) / 1000);
}

// The one thing that failed at Check, its fix and what fixes it; null when nothing did.
function failInfo(x) {
  const g = x.guide;
  const r = x.chosen && g ? g.receipt : null;
  if (!r) return null;
  const t = (g && g.troubleshooting) || { gates: [], checks: {} };
  if (r.state === "failed") {
    const bad = (r.gates || []).find((v) => v.verdict === "FAIL");
    const gate = bad ? bad.gate : "tcl_error";
    return { gate, stage: r.stage, detail: bad ? bad.detail : `the build failed at ${r.stage}`,
      fix: ((t.gates || []).find((v) => v.gate === gate) || {}).fix || "", exit: "",
      design: DESIGN_GATES.has(gate), build: true };
  }
  if (r.state !== "passed") return null;
  const c = stepOf(g, "check");
  const bad = c && (c.checks || []).find((v) => v.state === "mismatch");
  if (!bad) return null;
  return { gate: bad.name, stage: "kit check", detail: bad.detail, fix: troubleFix(g, bad.name),
    exit: bad.identity ? "exit 14 INCOMPATIBLE" : "exit 15 REFUSED", design: false, build: false,
    rewrite: bad.name === "board_static" };
}

// Every card that is failing now: {name, card, detail, level, from} ("Every build gate and its fix").
export function failures(x) {
  const g = x.guide;
  if (!g) return [];
  const t = g.troubleshooting || { gates: [], checks: {} };
  const out = [];
  const add = (f) => { if (!out.some((o) => o.card === f.card)) out.push(f); };
  const r = x.chosen ? g.receipt : null;
  if (r && r.state === "failed") {
    const failed = (r.gates || []).filter((v) => v.verdict === "FAIL");
    for (const v of failed) add({ name: v.gate, card: v.gate, detail: v.detail, level: "err", from: "the build" });
    if (!failed.length) add({ name: "tcl_error", card: "tcl_error", detail: `the build failed at ${r.stage}`, level: "err", from: "the build" });
  }
  const v = g.vivado || {};
  const need = (g.profile || {}).vivado;
  if (v.found && v.version && need && majorMinor(v.version) !== majorMinor(need)) {
    add({ name: "vivado_version", card: "vivado_version", level: "warn", from: "Setup",
      detail: `Vivado ${v.version} found; the kit needs ${need}: build_rm.tcl will refuse to start` });
  }
  const checkFails = (checks, from) => {
    for (const c of checks || []) {
      if (c.state !== "mismatch") continue;
      add({ name: c.name, card: cardFor(c.name, t.checks), detail: c.detail, level: "err", from });
    }
  };
  for (const step of g.steps || []) {
    if (step.id === "tools" || (step.id === "check" && !r)) continue;
    if (step.id === "wrapper" && x.src !== "example") continue;
    checkFails(step.checks, step.id === "check" ? "Check" : step.id === "wrapper" ? "Design" : "Setup");
  }
  if (x.previewError && x.previewError.data) checkFails(x.previewError.data.checks, "Design");
  if (x.scriptError && x.scriptError.data) checkFails(x.scriptError.data.checks, "Build");
  if (x.packError && x.packError.data) checkFails(x.packError.data.checks, "Add");
  return out;
}

// --- actions -----------------------------------------------------------------------------------

async function scanRtl(bid, { out = "" } = {}) {
  const x = st(bid);
  const path = x.rtl.trim();
  if (!path) return;
  if (out) { x.saveBusy = true; x.saveError = null; } else { x.scanBusy = true; x.scanError = null; }
  changed();
  try {
    const body = { path, board_id: bid };
    if (x.rtlName.trim()) body.name = x.rtlName.trim();
    if (out) body.out = out;
    const d = (await call("designScan", {}, body)).data;
    if (out) x.savedJson = d.written || "";
    else {
      x.scan = d;
      x.gens = Object.entries(d.generics || {}).map(([k, v]) => (v && typeof v === "object"
        ? { k, path: true, v: String(v.path || "") } : { k, path: false, v: String(v) }));
    }
  } catch (e) {
    if (out) x.saveError = toApiError(e);
    else { x.scan = null; x.scanError = toApiError(e); }
  }
  x.scanBusy = false; x.saveBusy = false;
  persist(bid);
  changed();
}

// Continue to Build: a built-in is the guide's (already checked); an inline design is checked
// by the script's preview (nothing written).
async function chooseDesign(bid) {
  const x = st(bid);
  const g = x.guide;
  if (x.src === "example") {
    x.chosen = true; x.view = null;
    persist(bid);
    followCurrent(bid);
    loadGuide(bid);
    return;
  }
  x.previewBusy = true; x.previewError = null; changed();
  try {
    const d = (await call("guideScript", {}, { static_id: g.static_id, design: designArg(x) })).data;
    x.preview = { rm_id: d.rm_id, rm_id_proposed: d.rm_id_proposed, checks: d.checks, design: d.design };
    x.chosen = true; x.view = null;
  } catch (e) {
    x.preview = null;
    x.previewError = toApiError(e);
  }
  x.previewBusy = false;
  persist(bid);
  followCurrent(bid);
  changed();
  if (x.chosen) loadGuide(bid);
}

async function writeDir(bid, { stopAfter = st(bid).stopAfter } = {}) {
  const x = st(bid);
  const g = x.guide;
  x.scriptBusy = "write"; x.scriptError = null; changed();
  try {
    const body = { static_id: g.static_id, design: designArg(x), out_dir: x.buildDir.trim(),
      jobs: Number(x.jobs) || 4 };
    if (stopAfter) body.stop_after = "link";
    const d = (await call("guideScript", {}, body)).data;
    x.script = d;
    x.written = d.out_dir || x.buildDir.trim();
    x.run = d.run || null;
    x.stopAfter = !!stopAfter;
  } catch (e) {
    x.scriptError = toApiError(e);
    if (x.run) x.stopAfter = x.run.stop_after === "link";     // the directory keeps what it had
  }
  x.scriptBusy = "";
  persist(bid);
  changed();
  loadGuide(bid);
}

async function downloadZip(bid) {
  const x = st(bid);
  const g = x.guide;
  x.scriptBusy = "zip"; x.scriptError = null; changed();
  try {
    const name = `${designName(x) || "design"}_build.zip`;
    const body = { static_id: g.static_id, design: designArg(x), format: "zip", jobs: Number(x.jobs) || 4 };
    if (x.stopAfter) body.stop_after = "link";
    saveBlob(await callBlob("guideScript", {}, body), name);
    x.zipSaved = name;
  } catch (e) { x.scriptError = toApiError(e); }
  x.scriptBusy = ""; changed();
}

async function addToWorkbench(bid) {
  const x = st(bid);
  const r = x.guide && x.guide.receipt;
  if (!r) return;
  x.packBusy = true; x.packError = null; changed();
  try {
    const d = (await call("kitPack", {}, { path: r.path, import: true })).data;
    const imp = d.imported || {};
    x.packed = { path: r.path, name: imp.name || r.rm_name, rm_id: imp.rm_id || r.rm_id,
      shadowed_by: imp.shadowed_by || "", overlay_dir: d.overlay_dir || "" };
    persist(bid);
    noteBuilt(bid, { name: x.packed.name, rm_id: x.packed.rm_id, static_id: r.static_id, path: r.path, added: true });
    if (imp.shadowed_by) {
      toast(`${x.packed.name} is in the store, but the Workbench lists ${imp.shadowed_by} instead (the same name)`,
        { icon: "triangle-alert", level: "err", ms: 6000 });
    } else {
      toast(`${x.packed.name} ${x.packed.rm_id} is on the Workbench, picked: tick Arm, then Program`);
    }
    x.packBusy = false;
    loadGuide(bid);
    pickOnWorkbench(bid, x.packed.name);
    return;
  } catch (e) {
    x.packError = toApiError(e);
  }
  x.packBusy = false;
  changed();
}

// --- the route: #/<board>/build/<step> ---------------------------------------------------------

// Look at a step: the current one follows the flow (no sub-page in the address), another one
// is kept in the address (a link to it lands there).
function look(bid, k, cur) {
  const x = st(bid);
  if (cur && cur.k === k) { x.view = null; followCurrent(bid); changed(); return; }
  x.view = k;
  x.seen = k;
  navigate(bid, `build/${k}`);
}

function followCurrent(bid) {
  const x = st(bid);
  x.seen = "";
  const subs = { ...(APP.subs[bid] || {}) };
  if (!subs.build) return;
  delete subs.build;
  APP.subs[bid] = subs;
  try { window.sessionStorage.setItem("harness_manager.subs", JSON.stringify(APP.subs)); } catch (e) { /* ok */ }
  writeRoute();
}

// --- small pieces ------------------------------------------------------------------------------

function Btn({ cls = "", icon = "", children, onClick, dis = false, busy = false, title = "", testid = "", action = "" }) {
  return html`<button type="button" class=${`btn ${cls}`} disabled=${dis || busy} title=${title || undefined}
      aria-busy=${busy ? "true" : undefined} data-testid=${testid || undefined} data-action=${action || undefined}
      onClick=${onClick}>${busy ? html`<${Spinner} />` : icon ? html`<${Icon} name=${icon} />` : null}${children}</button>`;
}

function Cmd({ text, testid = "", label = "Copy" }) {
  return html`<div class="bd-cmd" data-testid=${testid || undefined}><code title=${text}>${text}</code>
    <button type="button" class="btn primary sm" onClick=${() => copyText(text, "Command copied")}>
      <${Icon} name="copy" />${label}</button></div>`;
}

function CopyLine({ text, testid = "" }) {
  return html`<div class="copy-row" data-testid=${testid || undefined}><code title=${text}>${text}</code><${CopyButton} text=${text} /></div>`;
}

function Then({ children }) {
  return html`<div class="bd-then"><${Icon} name="chevron-right" /><span><b>Then:</b> ${children}</span></div>`;
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

function PathsHint() {
  const host = window.location.hostname;
  return isLoopbackHost(host)
    ? html`<${Reason} testid="build-paths-hint" icon="info"
        text="Paths are on this machine: harness-manager-daemon runs here, and so does Vivado (Harness Manager gives the command; it does not run it)." />`
    : html`<${Reason} level="warn" testid="build-paths-hint" icon="triangle-alert"
        text=${`This page comes from harness-manager-daemon on ${host}: every path is on that machine, not this one. To build on this machine, download the build directory as a zip.`} />`;
}

// --- the head: the bar, where you are, ready to build, the lease ------------------------------

function Stepper({ bid, f }) {
  return html`<ol class="bd-stepper" aria-label="Build steps" data-testid="bd-stepper">
    ${f.steps.map((s, i) => {
      const lit = i > 0 && (f.steps[i - 1].state === "done" || f.steps[i - 1].state === "warn");
      const ic = s.state === "done" ? html`<${Icon} name="check" />` : s.state === "failed" ? html`<${Icon} name="x" />`
        : s.state === "running" ? html`<${Icon} name="loader-circle" cls="spin" />`
        : s.state === "warn" ? html`<${Icon} name="triangle-alert" />` : html`<span>${s.n}</span>`;
      return html`<li key=${s.k} class=${`bd-node ${s.state}${lit ? " lit" : ""}${f.view === s.k ? " viewing" : ""}`}
          data-step=${s.k} data-state=${s.state} data-testid=${`bd-node-${s.k}`}>
        <button type="button" aria-current=${f.view === s.k ? "step" : undefined} title=${`${s.n} ${s.l}: ${STATE_WORD[s.state]}`}
          onClick=${() => look(bid, s.k, f.cur)}>
          <span class="bd-dot">${ic}</span><span class="bd-lbl">${s.l}</span><span class="bd-sub">${s.sub}</span></button></li>`;
    })}</ol>`;
}

function nowNext(f) {
  const { x, r, running, fi, prob, cur, g } = f;
  switch (cur.k) {
    case "setup": {
      const t = stepOf(g, "target");
      if (!g) return ["reading the guide", "Design"];
      if (t && t.state === "failed") return ["the board's static has no kit here", "nothing can be built for it yet"];
      const tl = stepOf(g, "tools");
      if (tl && tl.state === "failed") return ["Vivado does not start", "fix it (Setup), then Design"];
      return ["fetch the kit for this static", "Design: pick what to build"];
    }
    case "design": return prob.err ? [`refused: ${prob.err.title}`, "fix the design, then Build"]
      : ["pick what to build", "Build: Harness Manager writes the script, you run Vivado (30-60 min)"];
    case "build":
      if (running) return [x.way === "session" ? `running in your Vivado · ${Math.floor(elapsed(x, running) / 60)} min`
        : `Vivado running · ${running.stage} · ${Math.floor(elapsed(x, running) / 60)} min`, "Check, by itself when the receipt appears"];
      if (r && r.state === "stopped") return ["stopped after link: floorplan in Vivado", "write the pblock to your rm_xdc, then build again"];
      if (!x.written) return ["write the build directory", "you run one Vivado command (30-60 min)"];
      return [x.stopAfter ? "run Vivado to link, to floorplan" : "run the Vivado command (30-60 min)",
        x.stopAfter ? "draw nested pblocks in the linked design" : "Check, by itself when the receipt appears"];
    case "check": return fi ? [`refused: ${fi.gate}`, "fix it, then build again"] : ["reading the receipt", "Add to the Workbench"];
    case "add": return [`put ${r ? r.rm_name : "it"} on the Workbench`, "Arm → Program on the Workbench"];
    default: return ["", ""];
  }
}

function Where({ f }) {
  if (!f.cur) {
    const name = (f.x.packed && f.x.packed.name) || (f.r && f.r.rm_name) || "";
    return html`<div class="bd-where" data-testid="build-next"><${Chip} level="ok" icon="circle-check">Done<//>
      <span><b>${name}</b> is on the Workbench</span><span class="bd-next"><${Icon} name="chevron-right" />Next: Arm → Program there</span></div>`;
  }
  const [now, next] = nowNext(f);
  return html`<div class="bd-where" data-testid="build-next"><span class="nowrap"><b>Step ${f.cur.n} of ${f.steps.length}</b> · ${f.cur.l}</span>
    <span class="secondary">${now}</span><span class="bd-next"><${Icon} name="chevron-right" />Next: ${next}</span></div>`;
}

function Ready({ bid, f }) {
  const { g, x } = f;
  if (!g) return null;
  const open = f.view === "setup";
  const t = stepOf(g, "target");
  const k = stepOf(g, "kit");
  const tl = stepOf(g, "tools");
  const v = g.vivado || {};
  const need = (g.profile && g.profile.vivado) || "";
  let level = "ok";
  let text;
  if (t && t.state === "failed") { level = "err"; text = html`<b>No kit for this board's static.</b> ${t.detail}`; }
  else if (!k || k.state !== "done") { level = "warn"; text = html`<b>The kit for ${hexId(g.static_id)} is not here yet.</b> Fetch it in Setup: every build starts from the static's checkpoint.`; }
  else if (tl && tl.state === "failed") { level = "err"; text = html`<b>Vivado ${v.version || ""} does not start.</b> ${(v.launch && v.launch.detail) || tl.detail}`; }
  else if (!v.found) { level = "warn"; text = html`<b>No Vivado on this machine</b> (${v.reason || "not found"}). Write the build directory anyway, or download it as a zip, and run Vivado ${need} where it is installed.`; }
  else if (need && majorMinor(v.version) !== majorMinor(need)) { level = "warn"; text = html`<b>Vivado ${v.version} is not this kit's ${need}.</b> build_rm.tcl refuses another release: install ${need}.`; }
  else text = html`Ready to build for <b class="mono">${hexId(g.static_id)}</b> with Vivado ${v.version || need} · kit <span class="mono">${g.kit_id}</span> ok <span class="muted">· licence checked at synthesis</span>`;
  return html`<div class=${`bd-ready ${level}`} data-testid="build-ready" data-level=${level}>
    <${Icon} name=${level === "ok" ? "circle-check" : level === "err" ? "circle-x" : "triangle-alert"} />
    <span class="grow">${text}</span>
    <button type="button" class="link small" data-testid="build-ready-details"
      onClick=${() => (open ? look(bid, f.cur ? f.cur.k : "add", f.cur) : look(bid, "setup", f.cur))}>${open ? "Hide setup" : level === "ok" ? "Details" : "Fix it"}</button>
  </div>`;
}

// Building needs no lease; programming does. Who holds it, on a board behind a hub.
function LeaseNote({ bid }) {
  const who = leaseWho(bid);
  const name = boardName(null, bid);
  let text = "";
  let level = "";
  if (who.state === "other" || who.state === "elsewhere") {
    level = "held";
    text = `Building doesn't need the lease; only programming does. ${who.holder} holds ${name}${who.state === "elsewhere" ? " in another session" : ""}: build now, and request it when the build is near done.`;
  } else if (who.state === "free") text = "Building doesn't need the lease; programming does. Nobody holds it now.";
  else if (who.state === "unknown") { level = "warn"; text = "Building doesn't need the lease; programming does. The hub lease could not be read."; }
  if (!text) return null;
  return html`<p class=${`reason ${level}`} data-testid="build-lease-note" data-lease=${who.state}>
    <${Icon} name=${level === "held" ? "lock" : "info"} /><span>${text}</span></p>`;
}

// --- the panels --------------------------------------------------------------------------------

const PANEL_CHIP = { done: ["ok", "circle-check", "Done"], current: ["accent", "chevron-right", "Now"],
  running: ["accent", "loader-circle", "Running"], failed: ["err", "circle-x", "Failed"],
  blocked: ["", "circle-dashed", "Waits"], warn: ["warn", "triangle-alert", "Warning"] };

function Panel({ f, k, title, children }) {
  const s = f.steps.find((v) => v.k === k);
  const chip = PANEL_CHIP[s.state];
  return html`<section class=${`card bd-panel ${s.state}`} aria-label=${`${s.n} ${s.l}`} data-testid="bd-panel"
      data-step=${k} data-state=${s.state}>
    <div class="bd-panel-head"><span class="bd-n">${s.n}</span><h2 class="bd-panel-title" data-testid="bd-panel-title">${title}</h2><span class="spacer"></span>
      <${Chip} level=${chip[0]} icon=${chip[1]} cls=${s.state === "running" ? "busy" : ""} testid="bd-panel-state">${chip[2]}<//></div>
    <div class="bd-panel-body">${children}</div></section>`;
}

// 1 Setup: target, tools and kit (the guide's first three steps)
function progressText(d) {
  const phase = d.phase || "fetch";
  if (d.total > 0) return `${phase}: ${bytesText(d.done)} of ${bytesText(d.total)}`;
  if (d.done > 0) return `${phase}: ${bytesText(d.done)}`;
  return `${phase}: started`;
}

function StepChip({ state, testid = "" }) {
  const look = { done: ["ok", "circle-check", "done"], next: ["accent", "chevron-right", "next"],
    blocked: ["", "circle-dashed", "waits"], failed: ["err", "circle-x", "failed"],
    unchecked: ["unk", "circle-help", "unchecked"] }[state] || ["", "circle-dashed", state || "?"];
  return html`<${Chip} level=${look[0]} icon=${look[1]} testid=${testid}>${look[2]}<//>`;
}

function SetupPanel({ bid, f }) {
  const { g, x } = f;
  if (!g) return html`<${Panel} f=${f} k="setup" title="Setup: target, tools and kit">
    ${x.guideError ? html`<${Reason} level="err" testid="build-error" text=${errText(x.guideError)} />`
      : html`<p class="muted"><${Spinner} /> Reading the guide...</p>`}<//>`;
  const t = stepOf(g, "target") || {};
  const tl = stepOf(g, "tools") || {};
  const ks = stepOf(g, "kit") || {};
  const v = g.vivado || {};
  const need = (g.profile && g.profile.vivado) || "";
  const needBuild = (g.profile && g.profile.vivado_build) || 0;
  const have = v.found ? v.version || "" : "";
  const mismatch = !!(need && have && majorMinor(have) !== majorMinor(need));
  const buildDiff = !!(need && have && !mismatch && needBuild && v.build && v.build !== needBuild);
  const licence = String(tl.detail || "").split(/; licence: /)[1] || "";
  const launch = v.launch;
  const kb = x.kit || {};
  const kit = kb.kit;
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
      x.kitZipSaved = name;
    } catch (e) { x.verifyError = toApiError(e); }
    x.zipBusy = false; changed();
  };
  const vbad = x.verify ? (x.verify.checks || []).filter((c) => c.state === "mismatch") : [];
  const showFetch = sid && (!kit || x.showFetch || ks.state === "failed" || p.running || p.lines.length);
  return html`<${Panel} f=${f} k="setup" title="Setup: target, tools and kit">
    <p class="bd-lead">Harness Manager checks these each time you open Build. They rarely change: a harness install can change the shell, and a new Vivado the tools.</p>
    <dl class="kv bd-kv">
      <dt>Target</dt><dd data-testid="step-target" data-state=${t.state}><div class="line"><${StepChip} state=${t.state} testid="state-target" />
          <span data-testid="step-target-detail">${t.detail}</span></div>
        ${t.reason ? html`<div class="sub">${t.reason}</div>` : null}</dd>
      <dt>Tools</dt><dd data-testid="step-tools" data-state=${tl.state}>
        <div class="line"><${StepChip} state=${tl.state} testid="state-tools" />
          <span data-testid="vivado-found">${v.found
            ? html`<span class="mono">Vivado ${have || "?"}</span> ${need ? (mismatch ? html`<${Chip} level="warn" icon="triangle-alert" testid="vivado-chip">different release<//>`
              : html`<${Chip} level=${buildDiff ? "warn" : "ok"} icon=${buildDiff ? "triangle-alert" : "circle-check"} testid="vivado-chip">${buildDiff ? "other build" : "matches"}<//>`) : null}
              <span class="sub">found ${HOW[v.how] || v.how || "?"}${v.error ? `; ${v.error}` : ""}</span>`
            : html`<span class="muted">no Vivado: ${v.reason || "not found"}</span>`}</span></div>
        ${v.found ? html`<div class="sub mono">${v.path}</div><div class="sub">The command Harness Manager prints uses this path, so the vivado on PATH doesn't matter.</div>` : null}
        <div class="sub" data-testid="vivado-needed">${need ? html`needs <span class="mono">Vivado ${need}</span>${needBuild ? ` (build ${needBuild})` : ""}: the kit's release` : "the release is known once the kit is here"}</div>
        ${(v.others || []).length ? html`<div class="sub">also installed: ${v.others.map((o) => html`<span key=${o.path} class="mono">${o.version} ${o.path} </span>`)}</div>` : null}
        ${launch ? html`<div class="line" data-testid="vivado-launch"><${Chip} testid="vivado-launch-chip"
            level=${launch.state === "ok" ? "ok" : launch.state === "failed" ? "err" : "unk"}
            icon=${launch.state === "ok" ? "circle-check" : launch.state === "failed" ? "circle-x" : "circle-help"}>${
            launch.state === "ok" ? "starts" : launch.state === "failed" ? "does not start" : "unchecked"}<//>
          <span class="sub">${launch.detail}</span></div>` : null}
        ${tl.reason ? html`<${Reason} level=${tl.state === "failed" ? "err" : "warn"} testid="step-tools-reason" text=${tl.reason} />` : null}
        ${mismatch ? html`<${Reason} level="warn" testid="vivado-mismatch"
          text=${`Vivado ${have} is not this kit's ${need}: a checkpoint opens only in the release that wrote it, and build_rm.tcl refuses to start in another. Harness Manager only warns; install ${need}, or point $HARNESS_MANAGER_VIVADO at it.`} />` : null}</dd>
      <dt>Licence</dt><dd data-testid="licence"><div class="line"><${Chip} level="unk" icon="circle-help">unchecked<//>until synthesis</div>
        <div class="sub">${String(licence || "").replace(/^unchecked\s*\((.*)\)$/, "$1") || "only synthesis can tell"}</div></dd>
      <dt>Kit</dt><dd data-testid="step-kit" data-state=${ks.state}>
        <div class="line"><${StepChip} state=${ks.state} testid="state-kit" />${kit ? html`<span class="mono" data-testid="build-kit-id">${kit.kit_id}</span>
          <span class="sub">${bytesText(kit.size)}, ${kit.files} files, from <span class="mono">${kit.source}</span></span>` : html`<span>${ks.detail}</span>`}</div>
        ${kit ? html`<div class="sub" data-testid="kit-summary">${ks.detail}</div>
          <div class="line"><span class="sub">access</span><${Chip} level=${kit.access === "public" ? "ok" : "warn"} testid="kit-access">${kit.access || "?"}<//>
            <span class="sub" data-testid="kit-licence">${kit.licence_note || "the kit gives no licence note"}</span></div>` : null}
        ${ks.reason ? html`<${Reason} testid="step-kit-reason" icon="circle-dashed" text=${ks.reason} />` : null}
        <${CheckList} checks=${ks.checks} testid="kit-checks" />
        ${showFetch ? html`<div class="field wrap bd-field"><label>Source</label>
          <${Seg} label="Kit source" value=${x.source} onChange=${(val) => { x.source = val; changed(); }} options=${[
            { value: "auto", label: "Automatic", title: "the cache, then the release channel, then the hub" },
            { value: "path", label: "A folder or zip", title: "a kit directory, a kit zip, or a fielded/<static>/ directory on harness-manager-daemon's host" },
          ]} />
          ${x.source === "path" ? html`<input class="input mono grow" aria-label="Kit source path" data-testid="kit-source-path"
            placeholder="/path/to/fielded/0x72BB0A36 (absolute, on harness-manager-daemon's host)" value=${x.sourcePath}
            onInput=${(e) => { x.sourcePath = e.target.value; changed(); }} />` : null}</div>
          ${x.source === "auto" && (kb.sources || []).length ? html`<ul class="source-list" data-testid="kit-sources">${kb.sources.map((s) => html`
            <li key=${s.name} data-source=${s.name} data-available=${s.available ? "true" : "false"}>
              <${Icon} name=${s.available ? "circle-check" : "circle-minus"} cls=${`sm ${s.available ? "i-ok" : "i-muted"}`} />
              <span class="mono">${s.name}</span>${s.reason ? html`<span class="sub">${s.reason}</span>` : null}</li>`)}</ul>` : null}
          <${ActionRow} bid=${bid} panel="kit_fetch" spec=${fetchSpec} variant=${kit ? "" : "primary"} icon="download" />
          <${ResultBlock} lines=${p.lines} panel=${p} testid="kit-fetch-result" />` : null}
        ${kit ? html`<div class="bd-acts">
          ${!showFetch ? html`<${Btn} cls="sm ghost" icon="refresh-cw" testid="kit-fetch-again" onClick=${() => { x.showFetch = true; changed(); }}>Fetch again…<//>` : null}
          <${Btn} cls="sm" icon="shield-check" testid="kit-verify" busy=${x.verifyBusy} onClick=${verify}>Verify<//>
          <${Btn} cls="sm ghost" icon="download" testid="kit-zip" busy=${x.zipBusy} onClick=${zip}>Download kit zip<//></div>
          ${x.verify ? html`<${Reason} level=${vbad.length ? "err" : "ok"} testid="kit-verified"
            text=${vbad.length ? `Verify: ${vbad.map((c) => `${c.name}: ${c.detail}`).join("; ")}` : `Verified: ${(x.verify.checks || []).map((c) => c.detail).join("; ")}`} />` : null}
          ${x.kitZipSaved ? html`<${Reason} level="ok" text=${`Saved ${x.kitZipSaved}.`} testid="kit-zip-saved" />` : null}` : null}
        ${x.verifyError ? html`<${Reason} level="err" text=${errText(x.verifyError)} />` : null}
        ${x.kitError ? html`<${Reason} level="warn" testid="kit-error" text=${`The kit's details could not be read: ${errText(x.kitError)}`} />` : null}</dd>
    </dl>
    <${PathsHint} />
    <div class="bd-acts"><${Btn} cls="ghost" icon="refresh-cw" testid="build-refresh" busy=${x.guideLoading}
      onClick=${() => loadGuide(bid)}>Check again<//></div>
  <//>`;
}

// 2 Design: what to build, its generics, its constraints and the partition's floorplan
function Generics({ bid, x, locked, edit }) {
  if (x.src === "example") return html`<span class="small muted">none: a built-in names no build.generics (My RTL or Paste can)</span>`;
  if (x.src === "paste") {
    const p = parsedPaste(x).obj;
    const gens = Object.entries(((p && p.build) || {}).generics || {});
    return gens.length ? html`<div class="bd-gens" data-testid="bd-generics">${gens.map(([k, v]) => html`<div key=${k} class="bd-gen-ro">
        <span class="mono">${k}</span><span class="muted">=</span><span class="mono">${v && typeof v === "object" ? `{"path": "${v.path}"}` : String(v)}</span></div>`)}
        <div class="small muted">From the pasted design's build.generics: edit the JSON to change them. A missing file stops the build at preflight (generic_file_present).</div></div>`
      : html`<span class="small muted">none in the design (build.generics)</span>`;
  }
  if (!x.scan) return html`<span class="small muted">read your RTL first: the top's parameters that name a file beside it are proposed</span>`;
  return html`<div class="bd-gens" data-testid="bd-generics">
    ${x.gens.map((g, i) => html`<div class="bd-gen" key=${i}>
      <input class="input sm mono" aria-label="Generic name" placeholder="NAME" value=${g.k} disabled=${locked} onInput=${(e) => edit(() => (g.k = e.target.value))} />
      <select class="select sm" aria-label="File or value" disabled=${locked} onChange=${(e) => edit(() => (g.path = e.target.value === "path"))}>
        <option value="path" selected=${g.path}>file</option><option value="value" selected=${!g.path}>value</option></select>
      <input class="input sm mono" aria-label="Value" placeholder=${g.path ? "/home/you/rtl/my_soc/fw/image.hex" : "1"} value=${g.v} disabled=${locked} onInput=${(e) => edit(() => (g.v = e.target.value))} />
      <button type="button" class="btn ghost icon-only sm" title="Remove" aria-label=${`Remove ${g.k || "generic"}`} disabled=${locked} onClick=${() => edit(() => x.gens.splice(i, 1))}><${Icon} name="x" /></button></div>`)}
    <div class="row"><button type="button" class="btn sm" disabled=${locked} onClick=${() => edit(() => x.gens.push({ k: "", path: false, v: "" }))}>
      <${Icon} name="plus" />Add a generic</button></div>
    <div class="small muted">Top-level parameters (build.generics), one -generic each. A file, such as the image a SoC's IMEM loads with $readmemh, is written absolute; a missing one stops the build at preflight.</div>
  </div>`;
}

function Meter({ label, used, cap, refv, muted = false, level = "" }) {
  if (!cap) {
    return html`<div class="bd-meter-row" data-meter=${label}><span class="bd-meter-l">${label}</span>
      <div class="bd-meter none"></div><span class="bd-meter-n num">${used === null || used === undefined ? "" : html`<b>${num(used)}</b> / `}not in the pin model</span></div>`;
  }
  const p = used === null || used === undefined ? 0 : (100 * used) / cap;
  const rp = refv === null || refv === undefined ? null : (100 * refv) / cap;
  const hot = level === "warn" || level === "high" || p > 70;
  return html`<div class="bd-meter-row" data-meter=${label} title=${`${label}: ${used === null || used === undefined ? "?" : num(used)} of ${num(cap)} in the pblock${rp === null ? "" : ` · reference ${num(refv)}`}`}>
    <span class="bd-meter-l">${label}</span>
    <div class=${`bd-meter${muted ? " muted" : ""}${hot ? " hot" : ""}`}><div style=${`width:${Math.min(100, Math.max(p, used ? 0.6 : 0))}%`}></div>
      ${rp === null ? null : html`<i style=${`left:${Math.min(100, rp)}%`}></i>`}</div>
    <span class="bd-meter-n num">${used === null || used === undefined ? html`<span class="muted">?</span>` : html`<b>${num(used)}</b>`} / ${num(cap)}${used === null || used === undefined ? "" : ` · ${pctText(p)}`}</span></div>`;
}

const METERS = [["LUT", "LUT"], ["FF", "FF"], ["BRAM", "BRAM"], ["DSP", "DSP"]];

function askLab(g) {
  const pb = g.pblock || {};
  copyText(`Could the lab consider a larger (or moved) ${pb.name || "pblock_rp_dut"} for ${hexId(g.static_id)}? My RM needs more room than ${pb.slice_range || "the partition"}`
    + ` (LUT ${num((pb.capacity || {}).LUT || 0)} · BRAM ${num((pb.capacity || {}).BRAM || 0)}). I understand it means a new static (a mint) that re-keys every overlay.`, "Request copied: send it to the lab");
}

function Pblock({ g }) {
  const pb = g.pblock;
  if (!pb) return html`<div class="bd-pb" data-testid="bd-pblock"><${Reason} level="unk"
    text=${`The pin model does not describe the partition of ${hexId(g.static_id)}: no pblock facts to show.`} /></div>`;
  const cap = pb.capacity || {};
  const ref = referenceUse(pb.reference_use);
  const u = g.utilisation;
  const row = (key) => (u && (u.rows || []).find((r) => r.key === key)) || null;
  return html`<div class="bd-pb" data-testid="bd-pblock">
    <div class="bd-pb-h"><span class="mono"><b>${pb.name}</b></span><${Chip} cls="bd-mini" icon="lock"
      title=${`${pb.fixed_why || ""} (${(pb.sources || {}).name || "the pin model"})`}>fixed by the static<//></div>
    <dl class="bd-pb-kv">
      <dt>Where</dt><dd>${pb.slr} · clock regions ${(pb.clock_regions || []).join(" ")}</dd>
      <dt>Range</dt><dd class="mono">${pb.slice_range}</dd>
      <dt>Sites</dt><dd>${(pb.site_types || []).join(", ")} · <b>${pb.io_sites} IO</b>: an RM owns no pad</dd>
      <dt>Rules</dt><dd>snapping ${pb.snapping_mode}${pb.exclude_placement_contain_routing ? " · EXCLUDE_PLACEMENT · CONTAIN_ROUTING" : ""}${pb.rules ? html`<br /><span class="muted">${pb.rules}</span>` : null}</dd>
    </dl>
    <div class="bd-meters" aria-label=${`Capacity of ${pb.name}`}>
      ${METERS.map(([k, l]) => {
        const r = row(k);
        return html`<${Meter} key=${k} label=${l} used=${r ? r.used : ref[k] === undefined ? null : ref[k]}
          cap=${r && r.available ? r.available : cap[k]} refv=${r && ref[k] !== undefined ? ref[k] : null} muted=${!r} level=${r ? r.level : ""} />`;
      })}
      <div class="bd-meter-cap">${u ? html`Yours, from <span class="mono">${(u.path || "").split("/").pop()}</span>${ref.name ? `; the tick is ${ref.name}, the reference` : ""}`
        : html`Filled: ${pb.reference_use || "no reference build"}. Yours shows after Check.`}</div>
    </div>
    <div class="bd-pb-foot"><span>Bigger, or moved? A new static (a mint), which re-keys every overlay: not a user action.</span>
      <button type="button" class="link small" onClick=${() => askLab(g)}>Ask the lab…</button></div>
  </div>`;
}

function Constraints({ bid, f, locked, edit }) {
  const { x, g } = f;
  const p = x.src === "paste" ? parsedPaste(x).obj : null;
  const build = x.src === "paste" ? ((p && p.build) || {}) : x.src === "rtl" ? { rm_xdc: x.rmXdc.trim() } : {};
  const hook = build.synth_hook ? String(build.synth_hook) : "";
  const xdc = build.rm_xdc ? String(build.rm_xdc) : "";
  const pbName = (g.pblock && g.pblock.name) || "pblock_rp_dut";
  const box = (t, who, sub, title, cls = "") => html`<div class=${`bd-ff ${cls}`} title=${title}><span class="bd-ff-t"><span class="mono">${t}</span>
    <span class="bd-ff-who">${who}</span></span><span class="bd-ff-s">${sub}</span></div>`;
  let body;
  if (x.src === "example") {
    body = html`<div class="small secondary">None: ${x.ex} is a built-in, and a built-in carries no rm_xdc. Your own design can:
      <button type="button" class="link" disabled=${locked} onClick=${() => edit(() => { x.src = "rtl"; })}>My RTL</button> or a pasted .json.</div>`;
  } else if (x.src === "rtl") {
    body = html`<div class="bd-xpath"><input class="input sm mono grow" aria-label="rm_xdc path" data-testid="bd-rm-xdc"
        placeholder="optional: /home/you/rtl/my_soc/my_soc_rm.xdc" value=${x.rmXdc} disabled=${locked}
        onInput=${(e) => edit(() => (x.rmXdc = e.target.value))} />
      ${xdc ? html`<button type="button" class="btn ghost icon-only sm" title="No rm_xdc (it is optional)" aria-label="Clear the rm_xdc" disabled=${locked}
        onClick=${() => edit(() => (x.rmXdc = ""))}><${Icon} name="x" /></button>` : null}</div>
      ${xdc && !absPath(xdc) ? html`<${Reason} level="warn" text="Give it as an absolute path on harness-manager-daemon's host." />` : null}`;
  } else {
    body = html`<div class="bd-xpath">${xdc ? html`<span class="mono small">"rm_xdc": "${xdc}"</span><span class="small muted">from the pasted design's build</span>`
      : html`<span class="small muted">none in the design: add <span class="mono">"rm_xdc": "/path/to/${designName(x) || "my_rm"}_rm.xdc"</span> to its build</span>`}</div>`;
  }
  return html`<section class="bd-cf" aria-label="Constraints and floorplan" data-testid="bd-constraints">
    <div class="bd-cf-top"><span class="bd-sect">Constraints and floorplan</span><span class="small muted">what you may add, and the partition your RM must fit</span></div>
    <div class="bd-ffs" aria-label="Where each file is read">
      ${box("build.synth_hook", hook ? "yours" : "yours · none", "Tcl before synthesis: read_ip, create_ip, set_property",
        "Optional. A Tcl file sourced inside the synthesis project after the sources: Xilinx IP (read_ip, create_ip) and synthesis settings (set_property). Not constraints.", hook ? "on" : "")}
      ${box("RM_OOC_XDC", "Harness Manager writes it", "synthesis: boundary clocks and ports, HD.CLK_SRC for dut_clk",
        "Harness Manager writes xdc/<name>_ooc.xdc from the pin model: the partition's clocks and ports for out-of-context synthesis. Don't repeat it in your rm_xdc.", "hm")}
      ${box("build.rm_xdc", xdc ? "yours" : "yours · none", "right after link: read_xdc -cell u_rp_dut, before opt, place and route",
        "Optional. build_rm.tcl reads it with read_xdc -cell u_rp_dut once your RM is linked into the static, so opt, place and route see it. The preflight gate rm_xdc_present stops the build when the file is missing.", xdc ? "on" : "")}
      ${box(pbName, "fixed by the static", "opt, place and route stay inside it",
        "EXCLUDE_PLACEMENT and CONTAIN_ROUTING: nothing of the static is placed inside, and your RM's routing stays inside.", "fixed")}
    </div>
    <div class="bd-cf-grid">
      <div class="bd-cf-col">
        <div class="bd-cf-t">Your constraints <span class="mono muted">build.rm_xdc</span><${Chip} cls="bd-mini">optional<//></div>
        ${body}
        ${xdc ? html`<div class="small muted">Harness Manager does not read the file's text: build_rm.tcl checks it is there (rm_xdc_present) and Vivado reads it after link.</div>` : null}
        <div class="bd-may">
          <div class="bd-may-c ok"><div class="bd-may-t"><${Icon} name="check" />May</div><ul>
            <li title="set_false_path, set_max_delay, set_min_delay, set_multicycle_path, set_clock_groups, create_generated_clock, set_case_analysis, set_disable_timing, set_bus_skew, set_max_skew, group_path">timing inside the RM: false and multicycle paths, max delay, clock groups</li>
            <li>placement inside the partition: <span class="mono">create_pblock</span>, <span class="mono">resize_pblock</span>, <span class="mono">add_cells_to_pblock</span> (nested pblocks: untested)</li>
            <li>cell properties: <span class="mono">set_property</span> ASYNC_REG, DONT_TOUCH…</li></ul></div>
          <div class="bd-may-c no"><div class="bd-may-t"><${Icon} name="x" />May not</div><ul>
            <li>pins or pads (PACKAGE_PIN, IOSTANDARD): the RM sees only the ${(g.profile || {}).boundary_ports || 47}-port / ${(g.profile || {}).boundary_bits || 148}-bit boundary</li>
            <li>clocks: no BUFG or MMCM (HDPR-18); dut_clk and the others come from the static</li>
            <li>change <span class="mono">${pbName}</span>, or use Tcl control flow (if, foreach, puts)</li></ul></div>
        </div>
        <div class="bd-cf-draw"><${Icon} name="layers" /><span>Rather draw a nested pblock?${" "}<button type="button" class="link" data-testid="bd-to-floorplan" disabled=${locked || x.src === "example"}
            onClick=${() => { x.way = x.way === "session" ? "session" : "gui"; x.stopAfter = true; persist(bid);
              toast("Build: Vivado GUI, stop after link: the linked design stays open to floorplan", { icon: "layers" }); changed(); }}>Stop after link to floorplan</button>${" "}in Vivado, then write it to your rm_xdc. <span class="muted">Being proven now: one test build.</span></span></div>
      </div>
      <${Pblock} g=${g} />
    </div>
  </section>`;
}

function RmId({ rm, checks }) {
  if (!rm) return html`<span class="muted small">known once the design is read</span>`;
  const clash = (checks || []).find((c) => c.name === "rm_id_clash" && c.state !== "ok");
  const range = (checks || []).find((c) => c.name === "rm_id_range" && c.state !== "ok");
  const free = (checks || []).find((c) => c.name === "rm_id_clash" && c.state === "ok");
  return html`<div data-testid="wrapper-rm-id">
    <div class="line"><span class="mono">rm_id ${rm.rm_id}</span>
      ${rm.proposed ? html`<${Chip} level="accent" testid="rm-id-proposed">proposed by HM<//>` : html`<${Chip}>from the design<//>`}
      ${free && !clash ? html`<span class="sub">${free.detail}</span>` : null}</div>
    ${rm.proposed ? html`<div class="sub">User designs take design ids 0x8000-0xFFFF; the same name proposes the same id on every machine.</div>` : null}
    ${clash ? html`<${Reason} level="warn" testid="rm-id-clash" text=${clash.detail} />` : null}
    ${range ? html`<${Reason} level="warn" text=${range.detail} />` : null}
  </div>`;
}

function Fix({ e, testid = "design-refused" }) {
  return html`<div class="bd-fail" role="alert" data-testid=${testid} data-gate=${e.gate}>
    <div class="bd-fail-h"><${Icon} name="circle-x" /><span>Refused: ${e.title}</span></div>
    ${e.text ? html`<div class="small secondary">${e.text}</div>` : null}
    ${e.fix ? html`<div class="small"><b>Fix:</b> ${e.fix}</div>` : null}</div>`;
}

function Sources({ x }) {
  if (x.src === "example") {
    const w = x.guide && stepOf(x.guide, "wrapper");
    return html`<span class="small">${(w && w.detail) || "the kit's wrapper skeleton"}</span>`;
  }
  if (x.src === "paste") {
    const p = parsedPaste(x).obj;
    const b = (p && p.build) || {};
    const n = (b.sources || []).length;
    return html`<span class="small">${n} source file${n === 1 ? "" : "s"}${b.top ? html`, top <span class="mono">${b.top}</span>` : ""}</span>`;
  }
  const s = x.scan;
  if (!s) return html`<span class="small muted">not read yet</span>`;
  return html`<div class="stack-sm" data-testid="bd-scan">
    <span class="small">${s.sources.length} HDL file${s.sources.length === 1 ? "" : "s"} in compile order${(s.packages || []).length ? ` (${s.packages.length} package${s.packages.length === 1 ? "" : "s"} first)` : ""};
      top <span class="mono">${s.top}</span>${(s.tops || []).length > 1 ? html` <span class="muted">(of ${s.tops.join(", ")})</span>` : null}</span>
    <details class="bd-more"><summary>The ${s.sources.length} files, in compile order</summary><div><ol class="bd-files mono small">
      ${s.sources.map((p) => html`<li key=${p}>${p}</li>`)}</ol>
      ${(s.include_dirs || []).length ? html`<div class="small">+incdir+ ${s.include_dirs.map((p) => html`<span class="mono" key=${p}>${p} </span>`)}</div>` : null}
      ${(s.defines || []).length ? html`<div class="small">+define+ <span class="mono">${s.defines.join(" ")}</span></div>` : null}
      ${(s.left_out || []).length ? html`<div class="small muted">left out (test benches): ${s.left_out.map((p) => p.split("/").pop()).join(", ")}</div>` : null}</div></details>
    ${(s.ports || []).length ? html`<span class="small secondary">ports: ${s.ports.map((p) => `${p.name}${p.group ? ` (${p.group})` : " (not a boundary port)"}`).join(", ")}</span>` : null}
    ${(s.warnings || []).map((w, i) => html`<${Reason} key=${i} level="warn" text=${w} />`)}
  </div>`;
}

function DesignPanel({ bid, f }) {
  const { x, g, prob } = f;
  if (!g) return null;
  if (f.steps[1].state === "blocked") return html`<${Panel} f=${f} k="design" title="Your design">
    <p class="bd-lead">Waits for Setup: this board's static needs its kit (the static's checkpoint and the partition's facts) before anything can be built for it.</p>
    <div class="bd-acts"><${Btn} icon="chevron-right" onClick=${() => look(bid, "setup", f.cur)}>Go to Setup<//></div><//>`;
  const locked = !!f.running;
  const edit = (fn) => { fn(); resetDesign(x); x.previewError = null; persist(bid); changed(); };
  const rm = (x.cat ? (x.cat.designs || []) : []).filter((d) => d.kit === "rm-kit");
  const checks = designChecks(x);
  const warns = checks.filter((c) => c.state === "warning" && !["vivado", "rm_id_proposed", "rm_id_clash"].includes(c.name));
  const name = designName(x);
  let src;
  if (x.src === "example") {
    src = rm.length ? html`<div class="bd-choices" role="radiogroup" aria-label="Examples" data-testid="build-design">
      ${rm.map((d) => html`<label key=${d.name} class=${`choice${x.ex === d.name ? " on" : ""}`} data-design=${d.name}>
        <input type="radio" name=${`bd-ex-${bid}`} checked=${x.ex === d.name} disabled=${locked}
          onChange=${() => edit(() => { x.ex = d.name; loadGuide(bid); })} />
        <span><b class="mono">${d.name}</b><span class="sub">${d.title || ""}</span></span></label>`)}</div>`
      : html`<p class="muted">${x.catError ? `The design catalogue could not be read: ${errText(x.catError)}` : html`<${Spinner} /> Reading the designs...`}</p>`;
  } else if (x.src === "rtl") {
    src = html`<div class="stack-sm">
      <div class="field bd-field"><label for=${`bd-rtl-${bid}`}>RTL folder or .f list</label>
        <input id=${`bd-rtl-${bid}`} class="input mono grow" data-testid="bd-rtl-path" placeholder="/home/you/rtl/my_soc  or  /home/you/rtl/my_soc/files.f"
          value=${x.rtl} disabled=${locked} onInput=${(e) => edit(() => { x.rtl = e.target.value; x.scan = null; x.scanError = null; x.savedJson = ""; })}
          onKeyDown=${(e) => { if (e.key === "Enter") scanRtl(bid); }} />
        <${Btn} cls="sm" icon="scan-search" testid="bd-rtl-read" busy=${x.scanBusy} dis=${locked || !x.rtl.trim()} onClick=${() => scanRtl(bid)}>Read it<//></div>
      <div class="field bd-field"><label for=${`bd-name-${bid}`}>Name</label>
        <input id=${`bd-name-${bid}`} class="input mono" data-testid="bd-rtl-name" style="width:220px;max-width:100%" placeholder=${(x.scan && x.scan.name) || "from the folder"}
          value=${x.rtlName} disabled=${locked} onInput=${(e) => edit(() => { x.rtlName = e.target.value; })} />
        <span class="small muted">the design's name on the Workbench</span></div>
      ${x.scan && !prob.err ? html`<${Reason} level="ok" testid="bd-rtl-found" text=${html`Found ${x.scan.sources.length} HDL files; top <span class="mono">${x.scan.top}</span>. Check the compile order below.`} />` : null}
      ${x.scan ? html`<div class="row small">
        <${Btn} cls="sm ghost" icon="file-code" testid="bd-rtl-save" busy=${x.saveBusy} onClick=${() => {
          const p = x.rtl.trim().replace(/\/+$/, "");
          scanRtl(bid, { out: /\.f$/i.test(p) ? p.split("/").slice(0, -1).join("/") : p });
        }}>Save ${x.scan.name}.json beside your RTL<//>
        ${x.savedJson ? html`<span class="muted" data-testid="bd-rtl-saved">written: <span class="mono">${x.savedJson}</span> (for kit script --design)</span>` : null}
        ${x.saveError ? html`<span class="err-text">${errText(x.saveError)}</span>` : null}</div>` : null}
    </div>`;
  } else {
    src = html`<div class="stack-sm"><textarea class="input mono bd-json" rows="11" aria-label="Design JSON" data-testid="build-design-json"
        placeholder=${SAMPLE} disabled=${locked} value=${x.paste} onInput=${(e) => edit(() => (x.paste = e.target.value))}></textarea>
      <div class="small muted">A pasted design lives only in this page; give paths absolute (on harness-manager-daemon's host). The format: USER_GUIDE §7.2.</div></div>`;
  }
  return html`<${Panel} f=${f} k="design" title="Your design">
    <p class="bd-lead">Pick what to build: an example, the folder with your RTL, or a design .json. Everything about the design is here, in one place.</p>
    ${prob.err ? html`<${Fix} e=${prob.err} />` : null}
    <div class="seg bd-seg" role="group" aria-label="Design source" data-testid="bd-design-source">${[["example", "Example", "list"], ["rtl", "My RTL", "file-code"], ["paste", "Paste .json", "copy"]].map(([k, l, ic]) =>
      html`<button type="button" key=${k} aria-pressed=${x.src === k ? "true" : "false"} data-src=${k} disabled=${locked}
        onClick=${() => edit(() => { x.src = k; if (k === "example") loadGuide(bid); })}><${Icon} name=${ic} cls="sm" />${l}</button>`)}</div>
    ${src}
    <dl class="kv bd-kv">
      ${x.src === "rtl" ? null : html`<dt>Name</dt><dd class="mono">${name || html`<span class="muted">none</span>`}</dd>`}
      <dt>rm_id</dt><dd><${RmId} rm=${rmIdOf(x)} checks=${checks} /></dd>
      <dt>Sources</dt><dd><${Sources} x=${x} /></dd>
      <dt>Generics</dt><dd><${Generics} bid=${bid} x=${x} locked=${locked} edit=${edit} /></dd>
    </dl>
    <${Constraints} bid=${bid} f=${f} locked=${locked} edit=${edit} />
    ${warns.map((w) => html`<${Reason} key=${w.name} level="warn" testid=${`design-warn-${w.name}`} text=${`${w.name}: ${w.detail}`} />`)}
    ${locked ? html`<${Reason} text=${`Vivado is building in ${x.buildDir}: the design stays as it is until that build ends.`} />` : null}
    <div class="bd-acts">
      ${x.chosen ? html`<${Chip} level="ok" icon="circle-check">Chosen<//><span class="small muted">Changing anything here starts the build again.</span>`
        : html`<${Btn} cls="primary" icon="chevron-right" testid="bd-continue" busy=${x.previewBusy} dis=${!!prob.err || !!prob.todo || !name}
            title=${prob.todo || (prob.err ? `Refused: ${prob.err.title}` : "")} onClick=${() => chooseDesign(bid)}>Continue to Build<//>`}
      <${Btn} icon="download" testid="bd-rm-kit" onClick=${() => revealXdc(bid)}>Download the RM kit (wrapper + XDC)<//>
    </div>
    ${prob.todo && !x.chosen ? html`<${Reason} testid="design-todo" text=${prob.todo} />` : null}
    <${Then}>Build writes build_rm.tcl for ${name || "your design"}; you run Vivado yourself (30-60 min).<//>
  <//>`;
}

// the foot's XDC fold, opened and scrolled to (Design's "Download the RM kit")
function revealXdc(bid) {
  APP.ui.reveal = { bid, part: "xdc", at: Date.now() };
  changed();
}

// 3 Build: write the directory, run Vivado your way, watch it
function Way({ bid, x }) {
  const run = x.run;
  const way = x.way || "batch";
  const it = run ? run[way] : null;
  const text = it ? it.text : way === "batch" && x.script && (x.script.command || []).length ? x.script.command.join(" ") : "";
  const note = {
    batch: "Harness Manager follows every stage in build_rm.log.",
    gui: "You watch it run in the Vivado GUI; the same log, so Harness Manager still follows every stage.",
    session: "In your Vivado's Tcl console, with no project open. Harness Manager sees only the verdict: the stages appear when the receipt does.",
  }[way];
  return html`<div class="bd-way" data-testid="bd-way" data-way=${way}>
    <div class="bd-way-top"><span class="bd-way-l">Run it your way</span>
      <${Seg} label="How to run Vivado" value=${way} options=${WAYS} onChange=${(v) => { x.way = v; persist(bid); changed(); }} />
      ${way !== "batch" || x.stopAfter ? html`<${Chip} level="warn" cls="bd-mini" title="build_rm.tcl reads KEY=value from argv over the defaults Harness Manager wrote, and resolves paths against its own folder, so it can be sourced. A test build proves the GUI, your open Vivado and the stop-after-link loop">being proven now: one test build<//>` : null}</div>
    ${text ? html`<${Cmd} text=${text} testid="script-command" />`
      : html`<${Reason} level="unk" testid="bd-way-missing" text="This harness-manager-daemon gives no command for this way: update Harness Manager, or use Batch." />`}
    <div class="bd-way-note">${note}${it && it.watch ? html` <span class="muted">Watches: ${it.watch}.</span>` : null}</div>
    <label class=${`bd-stop${x.stopAfter ? " on" : ""}`} title="STOP_AFTER=link: the script returns after link, before close_project, so the static with your RM linked in stays open. The receipt says stopped.">
      <input type="checkbox" data-testid="bd-stop-after" checked=${!!x.stopAfter} disabled=${!!x.scriptBusy}
        onChange=${(e) => { const on = e.target.checked; if (on && x.way === "batch") x.way = "gui"; x.stopAfter = on; writeDir(bid, { stopAfter: on }); }} />
      <span><b>Stop after link to floorplan</b> <span class="mono small">STOP_AFTER=link</span>: the linked design stays open; draw nested pblocks inside the partition, write them to your rm_xdc, build again. Harness Manager writes the build directory again with it.</span></label>
  </div>`;
}

function Clock({ x, running }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t); }, []);
  return html`<div class="bd-clock num" data-testid="bd-clock">${mmss(elapsed(x, running))}</div>`;
}

function Stages({ at, failed = false, testid = "build-stages" }) {
  return html`<div class="bd-stages" aria-label="Build stages" data-testid=${testid}>${STAGES.map((s, i) => {
    const c = i < at ? "done" : i === at ? (failed ? "failed" : "now") : "";
    return html`<div key=${s} class=${`bd-stage ${c}`} data-stage=${s} data-stage-state=${c === "now" ? "running" : c || "pending"}><div class="bar"></div><span>${s}</span></div>`;
  })}</div>`;
}

function Expect() {
  return html`<div class="bd-expect"><div class="bd-expect-t">What to expect</div><ul>
    <li><${Icon} name="circle-check" /><span><b>The verdict is the receipt</b> (<span class="mono">${"out/<name>_build.json"}</span>), or the last line of build_rm.log that <i>starts</i> with <span class="mono">HM_RM_BUILD_</span>: <code>grep -E '^HM_RM_BUILD_' build_rm.log | tail -1</code>. The log also echoes the script, so HM_RM_BUILD_FAILED is in its text even after a pass: Harness Manager never reads echoed text.</span></li>
    <li><${Icon} name="info" /><span><b>Vivado exits 0 even when a gate fails</b>: its exit code says nothing.</span></li>
    <li><${Icon} name="triangle-alert" /><span><b>About 18-21 CRITICAL WARNINGs are expected</b>, not failures: from Harness Manager's out-of-context XDC on the tied-off ports, and from the static's debug_bridge.</span></li>
  </ul></div>`;
}

function BuildPanel({ bid, f }) {
  const { x, g, r, running } = f;
  const st3 = f.steps[2].state;
  const name = designName(x);
  if (st3 === "blocked") return html`<${Panel} f=${f} k="build" title="Build">
    <p class="bd-lead">Waits for Design: pick what to build first. Then Harness Manager writes build_rm.tcl and gives you the Vivado command.</p>
    <div class="bd-acts"><${Btn} icon="chevron-right" onClick=${() => look(bid, "design", f.cur)}>Go to Design<//></div><//>`;
  const dirOk = absPath(x.buildDir);
  const refused = x.scriptError && x.scriptError.data && x.scriptError.data.checks;
  const errs = html`${x.scriptError ? html`<${Reason} level="err" testid="script-error" text=${`${x.scriptError.status === 409 ? "Refused" : "Failed"}: ${errText(x.scriptError)}`} />` : null}
    ${refused ? html`<${CheckList} checks=${refused} testid="script-refused" skip=${(c) => c.state !== "mismatch"} />` : null}`;
  if (!x.written && !running) {
    return html`<${Panel} f=${f} k="build" title="Write the build directory">
      <p class="bd-lead">Harness Manager writes <span class="mono">build_rm.tcl</span>, the kit and the XDC kit for <b>${name}</b> into one folder. Nothing runs yet.</p>
      <div class="field bd-field"><label for=${`bd-dir-${bid}`}>Build directory</label>
        <input id=${`bd-dir-${bid}`} class="input mono grow" data-testid="build-dir" value=${x.buildDir}
          placeholder=${`/home/you/builds/${name || "my_rm"} (absolute, on harness-manager-daemon's host)`}
          onInput=${(e) => { x.buildDir = e.target.value; persist(bid); changed(); }} /></div>
      <div class="field bd-field"><label for=${`bd-jobs-${bid}`}>Threads</label>
        <select id=${`bd-jobs-${bid}`} class="select" value=${x.jobs} onChange=${(e) => { x.jobs = e.target.value; persist(bid); changed(); }}>
          ${["2", "4", "8"].map((n) => html`<option key=${n} value=${n}>${n}</option>`)}</select>
        <span class="small muted">4-8 GB of RAM either way</span></div>
      ${!x.buildDir.trim() && suggestDir(x) ? html`<div class="small muted">Under your home:
        <button type="button" class="link small mono" data-testid="build-dir-suggest"
          onClick=${() => { x.buildDir = suggestDir(x); persist(bid); changed(); }}>${suggestDir(x)}</button></div>` : null}
      <${PathsHint} />
      ${x.buildDir.trim() && !dirOk ? html`<${Reason} level="warn" testid="build-dir-hint" text="Give the build directory as an absolute path (harness-manager-daemon's working directory is not yours)." />` : null}
      ${errs}
      ${x.zipSaved ? html`<${Reason} level="ok" testid="script-zip-saved" text=${`Saved ${x.zipSaved}: unzip it, then run build_rm.tcl in its folder (README.txt says how).`} />` : null}
      <div class="bd-acts">
        <${Btn} cls="primary" icon="file-code" testid="script-write" busy=${x.scriptBusy === "write"} dis=${!dirOk || !!x.scriptBusy}
          title=${dirOk ? "" : "give the build directory first (an absolute path)"} onClick=${() => writeDir(bid)}>Write the build directory<//>
        <${Btn} cls="ghost" icon="download" testid="script-zip" busy=${x.scriptBusy === "zip"} dis=${!!x.scriptBusy} onClick=${() => downloadZip(bid)}>Download as a zip<//>
        <button type="button" class="link small" data-testid="build-watch" disabled=${!dirOk}
          onClick=${() => { x.written = x.buildDir.trim(); persist(bid); loadGuide(bid); }}>It is written already: watch it</button>
      </div>
      <${Then}>you run one Vivado command (30-60 min); Harness Manager watches the folder.<//>
    <//>`;
  }
  if (r && r.state === "stopped" && !running) return html`<${Panel} f=${f} k="build" title=${`Stopped after link: floorplan ${r.rm_name || name} in Vivado`}>
    <div class="bd-verdict" data-testid="bd-verdict">HM_RM_BUILD_STOPPED after=${r.stage} · receipt state stopped</div>
    <p class="bd-lead">The static with ${r.rm_name || name} linked in is open in ${x.way === "session" ? "your Vivado" : "the Vivado GUI"} (STOP_AFTER=link returns before close_project). <${Chip} level="warn" cls="bd-mini">being proven now: one test build<//></p>
    <ol class="bd-howto">
      <li><b>Draw the pblock inside ${(g.pblock && g.pblock.name) || "pblock_rp_dut"}</b> in the Device view, and assign your cells to it. <span class="sub">It must sit inside ${(g.pblock && g.pblock.slice_range) || "the partition"}; your cells, not the static's.</span></li>
      <li><b>Put it in your rm_xdc</b>: copy the create_pblock / resize_pblock / add_cells_to_pblock lines the Tcl console echoes, or write them to a file:
        <${Cmd} text=${`write_xdc -cell u_rp_dut -exclude_timing -force ${x.buildDir.trim()}/floorplan.xdc`} />
        <span class="sub">rm_xdc is read with -cell u_rp_dut, so cell names start inside the RM: drop a u_rp_dut/ prefix.</span></li>
      <li><b>Build again, all stages.</b> <span class="sub">Untick "Stop after link" below: Check then reports the utilisation of the partition.</span></li>
    </ol>
    <${Way} bid=${bid} x=${x} />
    ${errs}
  <//>`;
  if (running || (!r && x.written)) {
    const fresh = running;
    const guideRun = !running && g && g.running;       // a log with no verdict, not written for 30 min
    return html`<${Panel} f=${f} k="build" title=${running ? `Vivado is building ${name}` : x.stopAfter ? "Run Vivado to link" : "Run Vivado: 30-60 min"}>
      ${running ? html`<div class="bd-wait" data-testid="bd-running" data-stage=${running.stage}>
          <div><${Clock} x=${x} running=${running} /><div class="small muted">${running.started_at ? `since ${clock(running.started_at).slice(0, 5)} · ` : ""}typical 30-60 min</div></div>
          ${x.way === "session" ? html`<div class="stack-sm"><div class="small"><b>Running in your Vivado</b>: stages appear when the receipt does.</div></div>`
            : html`<div class="stack-sm"><${Stages} at=${Math.max(0, (running.stage_index || 1) - 1)} testid="bd-run-stages" />
            <div class="small secondary">build_rm.log: last <span class="mono">HM_STAGE ${running.stage}</span>${running.stage_elapsed_s !== null && running.stage_elapsed_s !== undefined ? ` for ${mmss(running.stage_elapsed_s)}` : ""}, written ${clock(running.log_mtime)}${x.stopAfter ? " · stops after link" : ""}</div></div>`}
        </div>
        <${Reason} level="warn" text=${`Don't start a second Vivado in ${x.buildDir.trim()}: it would overwrite out/. Harness Manager offers no command while this one runs.`} />`
        : html`<div class="bd-note"><${Icon} name="info" /><span><b>Harness Manager doesn't run Vivado.</b> You run it, your way. Harness Manager watches <span class="mono">${x.written}</span> and moves on by itself when the receipt appears; you can close this page meanwhile.</span></div>
        <${Way} bid=${bid} x=${x} />
        <ol class="bd-howto">
          <li><b>Leave it running.</b> <span class="sub">A small RM takes about 30 minutes on a quiet machine and up to an hour on a loaded one; a nanosoc-sized RM about 50. 4-8 GB of RAM.</span></li>
          <li><b>Come back to Check.</b> <span class="sub">Harness Manager reads the receipt, checks it and says what's next.</span></li>
        </ol>
        <div class="bd-watch" data-testid="bd-watch"><span class="dot accent bd-blink"></span><span>Watching <span class="mono">${x.written}</span></span>
          ${guideRun ? html`<span class="warn-text">· build_rm.log stops at ${guideRun.stage} (${clock(guideRun.log_mtime)}) with no verdict: that run died</span>`
            : html`<span>· ${x.way === "session" ? "no receipt yet (your open Vivado writes no build_rm.log)" : "no build_rm.log yet"}</span>`}</div>`}
      <${Expect} />
      ${errs}
      ${running ? html`<details class="bd-more"><summary>The command it runs</summary><div><${Way} bid=${bid} x=${x} /></div></details>` : null}
      <div class="bd-acts"><span class="grow"></span><${Btn} cls="ghost sm" icon="file-code" testid="build-change-dir"
        onClick=${() => { x.written = ""; x.run = null; persist(bid); loadGuide(bid); }}>${running ? "Watch another folder" : "Change the folder"}<//></div>
      <${Then}>when the receipt appears, Harness Manager checks it and shows the verdict in Check.<//>
    <//>`;
  }
  const ok = r.state === "passed";
  return html`<${Panel} f=${f} k="build" title=${`Vivado finished ${r.rm_name || name}`}>
    <div class=${`bd-verdict ${ok ? "ok" : "err"}`} data-testid="bd-verdict" title="The receipt's verdict (the last line of build_rm.log that starts with HM_RM_BUILD_)">${ok
      ? `HM_RM_BUILD_COMPLETE rm=${r.rm_name} rm_id=${r.rm_id} static_id=${r.static_id}` : `HM_RM_BUILD_FAILED gate=${f.fi ? f.fi.gate : r.stage}`}</div>
    <p class="bd-lead">${ok ? "The receipt says passed." : `The receipt says failed at ${r.stage}.`} The CRITICAL WARNINGs in build_rm.log are expected, not failures. Check has the verdict in full.</p>
    <${Stages} at=${ok ? 6 : Math.max(0, STAGES.indexOf(r.stage))} failed=${!ok} />
    <div class="small muted">Receipt <span class="mono">${r.path}</span>${r.built ? ` · built ${r.built}` : ""}</div>
    ${ok ? null : html`<${Way} bid=${bid} x=${x} />`}
    <div class="bd-acts"><${Btn} cls="primary" icon="chevron-right" testid="bd-see-check" onClick=${() => look(bid, "check", f.cur)}>See the check<//>
      <${Btn} cls="ghost sm" icon="file-code" testid="build-change-dir" onClick=${() => { x.written = ""; x.run = null; x.buildDir = ""; persist(bid); loadGuide(bid); }}>Another build directory<//></div>
  <//>`;
}

// 4 Check: four groups, the one failed gate, and the pblock's utilisation
const GROUPS = [
  { id: "identity", l: "Identity", stage: 2, checks: (n) => ["static_id", "rm_id", "board_static", "rm_id_clash"].includes(n),
    gates: ["static_id", "static_dcp_present", "rm_id_match", "boundary_bits", "rp_pins_link"] },
  { id: "timing", l: "Timing", stage: 3, checks: (n) => n === "timing", gates: ["rm_timing", "ooc_clocks", "drc_routed"] },
  { id: "pr_verify", l: "pr_verify", stage: 4, checks: (n) => n === "pr_verify", gates: ["pr_verify"] },
  { id: "files", l: "Files", stage: 5, checks: (n) => /^(partial|clearing|bit_bin_pair|static_binding|ltx|pair|build)\b/.test(n),
    gates: ["ltx_written", "clearing_fits"] },
];

export function checkGroups(receipt, checks) {
  const r = receipt;
  const at = r.state === "passed" ? 6 : STAGES.indexOf(r.stage);
  return GROUPS.map((gr) => {
    const gates = (r.gates || []).filter((v) => gr.gates.includes(v.gate));
    const rows = r.state === "passed" ? (checks || []).filter((c) => gr.checks(c.name)) : [];
    const fail = gates.find((v) => v.verdict === "FAIL") || null;
    const bad = rows.find((c) => c.state === "mismatch") || null;
    if (fail || bad) {
      return { ...gr, st: "err", exit: bad && bad.identity ? "exit 14" : "", d: fail ? `${fail.gate}: ${fail.detail}` : `${bad.name}: ${bad.detail}` };
    }
    if (r.state !== "passed" && gr.id === "files") return { ...gr, st: "skip", d: "not written: the build did not finish" };
    if (r.state !== "passed" && at <= gr.stage && !gates.length) return { ...gr, st: "skip", d: `not reached: the build stopped at ${r.stage}` };
    const main = rows.find((c) => c.state === "ok" && ["static_id", "timing", "partial", "board_static"].includes(c.name)) || rows[0];
    const unk = rows.filter((c) => c.state === "unchecked").length;
    const gd = gates.map((v) => `${v.gate} ${v.verdict}${v.detail ? `: ${v.detail}` : ""}`).join("; ");
    let d = main ? main.detail : gd || "no gate of this group in the receipt";
    if (gr.id === "identity" && rows.length) d = rows.filter((c) => c.state === "ok").map((c) => c.detail).join("; ");
    if (gr.id === "files" && rows.length) {
      const pc = rows.filter((c) => c.name === "partial" || c.name === "clearing").map((c) => c.detail);
      d = `${pc.join("; ") || `${rows.length} checks`}${rows.length > pc.length ? `; ${rows.length - pc.length} more checks of the pair` : ""}`;
    }
    if (unk) d += `; ${unk} unchecked (not a pass, does not block)`;
    return { ...gr, st: "ok", d };
  });
}

function Group({ gr }) {
  const ic = { ok: "circle-check", err: "circle-x", skip: "circle-dashed" }[gr.st];
  return html`<div class=${`bd-group ${gr.st}`} data-group=${gr.id} data-state=${gr.st}><div class="bd-group-h"><${Icon} name=${ic} /><span>${gr.l}</span>
    ${gr.exit ? html`<${Chip} level="err" cls="bd-mini">${gr.exit}<//>` : null}</div><div class="bd-group-d">${gr.d}</div></div>`;
}

function Utilisation({ g }) {
  const u = g.utilisation;
  if (!u) return null;
  const ref = referenceUse(g.pblock && g.pblock.reference_use);
  const rows = (u.rows || []).filter((r) => ["LUT", "FF", "BRAM", "DSP"].includes(r.key));
  return html`<div class="bd-sect">Utilisation in <span class="bd-nc">${u.pblock || "the pblock"}</span></div>
    <div class="bd-util" data-testid="bd-util" data-worst=${(u.worst || {}).level || ""}>
      <div class="bd-meters">${rows.map((r) => html`<${Meter} key=${r.key} label=${r.key} used=${r.used} cap=${r.available} refv=${ref[r.key] === undefined ? null : ref[r.key]} level=${r.level} />`)}</div>
      <div class="small secondary bd-util-n">
        <div>From <span class="mono">${(u.path || "").split("/").pop()}</span> (report_utilization -pblocks ${u.pblock}), ${String(u.design_state || "").toLowerCase() || "after route"}.${ref.name ? ` The tick is ${ref.name}, the reference build.` : ""}</div>
        ${u.worst ? html`<div>Fullest: <b>${u.worst.key}</b> at ${u.worst.util_pct} %${u.worst.level === "high" ? " (over 90 %)" : u.worst.level === "warn" ? " (over 70 %)" : ""}.</div>` : null}
        <div class="muted">Fixed by the static: an RM that outgrows it needs a new static (a mint). <button type="button" class="link small" onClick=${() => askLab(g)}>Ask the lab…</button></div></div>
    </div>`;
}

function CheckPanel({ bid, f }) {
  const { x, g, r, fi } = f;
  if (!r || f.running || r.state === "stopped") return html`<${Panel} f=${f} k="check" title="Check">
    <p class="bd-lead">Waits for Build. When Vivado finishes, Harness Manager reads the receipt and checks it here, board-free: identity, timing, pr_verify and the files.</p><//>`;
  const c = stepOf(g, "check");
  const groups = checkGroups(r, c && c.checks);
  const ok = r.state === "passed";
  const passedN = ((c && c.checks) || []).filter((v) => v.state === "ok").length;
  const unk = ((c && c.checks) || []).filter((v) => v.state === "unchecked");
  return html`<${Panel} f=${f} k="check" title=${fi ? "Check: refused" : "Check: passed"}>
    ${fi ? html`<div class="bd-fail" role="alert" data-testid="check-refused" data-gate=${fi.gate}>
        <div class="bd-fail-h"><${Icon} name="circle-x" /><span>Refused: <span class="mono">${fi.gate}</span></span>${fi.exit ? html`<span class="exit-code">${fi.exit}</span>` : null}</div>
        <div class="small secondary">One ${fi.build ? "gate" : "check"} failed at ${fi.stage}: ${fi.detail}</div>
        ${fi.fix ? html`<div class="small" data-testid="check-fix"><b>Fix:</b> ${fi.fix}</div>` : null}
        <div class="bd-acts">
          ${fi.design ? html`<${Btn} cls="primary" icon="file-code" testid="check-fix-design" onClick=${() => look(bid, "design", f.cur)}>Fix it in Design<//>`
            : fi.rewrite ? html`<${Btn} cls="primary" icon="refresh-cw" testid="check-fix-rewrite" onClick=${() => { x.written = ""; x.run = null; persist(bid); look(bid, "build", f.cur); }}>Write it again for ${hexId(g.static_id)}<//>`
            : html`<${Btn} cls="primary" icon="refresh-cw" testid="check-fix-build" onClick=${() => look(bid, "build", f.cur)}>Build again<//>`}
          <${Btn} cls="ghost sm" icon="copy" onClick=${() => copyText(`harness-manager kit guide --why ${fi.gate}`, "Command copied")}>kit guide --why ${fi.gate}<//></div>
      </div>`
      : html`<div class="outcome ok" data-testid="check-passed"><${Icon} name="circle-check" /><span><b>Passed:</b> ${r.rm_name} <span class="mono">${r.rm_id}</span> for <span class="mono">${r.static_id}</span> · ${passedN} checks ok${unk.length ? `, ${unk.length} unchecked` : ""}</span></div>`}
    <div class="bd-sect">Stages</div>
    <${Stages} at=${ok ? 6 : Math.max(0, STAGES.indexOf(r.stage))} failed=${!ok} testid="check-stages" />
    <div class="bd-sect">Checks</div>
    <div class="bd-groups" data-testid="check-groups">${groups.map((gr) => html`<${Group} key=${gr.id} gr=${gr} />`)}</div>
    ${ok ? html`<${Utilisation} g=${g} />` : null}
    ${ok ? html`<details class="bd-more"><summary>Every check (${(c && c.checks || []).length})</summary><div><${CheckList} checks=${c && c.checks} testid="check-list" /></div></details>` : null}
    ${fi ? null : html`${unk.length ? html`<${Reason} text=${`${unk.length} unchecked: ${unk.map((v) => v.name).join(", ")}. Unchecked is not a pass, and it doesn't block.`} />` : null}
      <div class="bd-acts"><${Btn} cls="primary" icon="chevron-right" testid="check-to-add" onClick=${() => look(bid, "add", f.cur)}>Go to Add<//></div>`}
    <div class="small muted">Receipt <span class="mono">${r.path}</span> · the same as <span class="mono">harness-manager kit check ${x.written || x.buildDir} ${bid}</span></div>
  <//>`;
}

// 5 Add: to the Workbench, landing there with the design picked
function AddLease({ bid, name }) {
  const who = leaseWho(bid);
  const board = boardName(null, bid);
  let level = "";
  let text;
  if (who.state === "none") { level = "ok"; text = `No lease needed: ${board} is not behind a hub.`; }
  else if (who.state === "here") { level = "ok"; text = "Programming needs the lease: yours."; }
  else if (who.state === "other" || who.state === "elsewhere") {
    level = "held";
    text = `Programming needs the lease: ${who.holder} holds ${board}${who.state === "elsewhere" ? " in another session" : ""}. Adding doesn't: add ${name} now, then request the board (header).`;
  } else if (who.state === "free") text = "Programming needs the lease: nobody holds it. Acquire it in the header when you get there.";
  else text = "Programming needs the lease; adding doesn't.";
  return html`<p class=${`reason ${level}`} data-testid="add-lease" data-lease=${who.state}><${Icon} name=${level === "held" ? "lock" : level === "ok" ? "circle-check" : "info"} /><span>${text}</span></p>`;
}

function AddPanel({ bid, f }) {
  const { x, r } = f;
  const s5 = f.steps[4].state;
  if (s5 === "blocked") return html`<${Panel} f=${f} k="add" title="Add to the Workbench">
    <p class="bd-lead">Waits for a passed check. Adding makes the build an overlay (kit pack --import) and opens the Workbench with it picked.</p><//>`;
  const name = (x.packed && x.packed.path === r.path && x.packed.name) || r.rm_name;
  const kind = ((boardState(bid).info || {}).identity || {}).harness_impl || "";
  if (f.imported) return html`<${Panel} f=${f} k="add" title=${`${name} is on the Workbench`}>
    <div class="outcome ok" data-testid="pack-done"><${Icon} name="circle-check" /><span><b>${name}</b> <span class="mono">${(x.packed && x.packed.rm_id) || r.rm_id}</span> is in this machine's overlay store: the Workbench lists it for every board on ${r.static_id}. Pick it there, tick Arm, then Program.</span></div>
    ${x.packed && x.packed.shadowed_by ? html`<${Reason} level="warn" testid="pack-shadowed" text=${`The Workbench lists ${x.packed.shadowed_by} instead: the same name, rm_id and static, and the first one found wins. Rename your design, or load this one with --overlay-dir ${x.packed.overlay_dir}.`} />` : null}
    <${AddLease} bid=${bid} name=${name} />
    <div class="bd-acts"><${Btn} cls="primary" icon="wrench" testid="open-workbench" onClick=${() => pickOnWorkbench(bid, name)}>Open the Workbench<//>
      <${Btn} cls="ghost" icon="plus" testid="build-another" onClick=${() => { resetDesign(x); x.buildDir = ""; x.view = null; persist(bid); followCurrent(bid); loadGuide(bid); }}>Build another design<//></div>
  <//>`;
  return html`<${Panel} f=${f} k="add" title=${`Add ${name} to the Workbench`}>
    <div class="bd-recap"><${Icon} name="circle-check" /><span>Check passed: <b>${name}</b> <span class="mono">${r.rm_id}</span> for <span class="mono">${r.static_id}</span></span></div>
    <p class="bd-lead">Adding writes the overlay (a manifest, the partial and its clearing) from the receipt and puts it in this machine's overlay store, as <span class="mono">kit pack --import</span> does. The Workbench opens with ${name} picked: tick Arm, then Program${kind === "linux" ? " (35-80 s on Linux)" : kind ? " (3-7 s on bare metal)" : ""}.</p>
    ${x.packError ? html`<${Reason} level="err" testid="pack-error" text=${`${x.packError.status === 409 ? "Refused" : "Failed"}: ${errText(x.packError)}`} />` : null}
    <div class="bd-acts"><${Btn} cls="primary" icon="upload" testid="kit-pack" action="kit_pack" busy=${x.packBusy} onClick=${() => addToWorkbench(bid)}>Add to the Workbench and program…<//></div>
    <${AddLease} bid=${bid} name=${name} />
    <${Then}>on the Workbench, Arm → Program; the console shows it boot.<//>
  <//>`;
}

// --- the foot: the command line, every gate and its fix, the XDC exports -------------------------

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

function XdcFold({ bid }) {
  const ref = useRef(null);
  const revealed = useReveal(bid, "xdc", ref);
  const [open, setOpen] = useState(revealed);
  useEffect(() => { if (revealed) setOpen(true); }, [revealed, APP.ui.reveal && APP.ui.reveal.at]);
  return html`<section class="card bd-fold-card" data-testid="xdc-fold" ref=${ref}>
    <button type="button" class="bd-fold-btn" aria-expanded=${open ? "true" : "false"} data-action="xdc-fold"
      onClick=${() => setOpen(!open)}><${Icon} name="chevron-right" cls=${`sm chev ${open ? "open" : ""}`} />More exports: the RM kit (wrapper + XDC) and the full-board XDC</button>
    ${open ? html`<div class="bd-fold-body"><${BoardXdcSection} bid=${bid} /></div>` : null}
  </section>`;
}

function Foot({ bid, f }) {
  const { x, g } = f;
  if (!g) return null;
  const t = g.troubleshooting || { gates: [], checks: {} };
  const fails = failures(x);
  const failing = new Set(fails.map((v) => v.card));
  const fixOf = (card) => (t.checks || {})[card] || ((t.gates || []).find((gg) => gg.gate === card) || {}).fix || "";
  const name = designName(x) || "my_rm";
  const des = x.src === "example" ? name : x.savedJson || `${name}.json`;
  const dir = x.written || x.buildDir.trim() || `/home/you/builds/${name}`;
  const cmds = [`harness-manager kit script ${bid} --design ${des} --out ${dir}`,
    `harness-manager kit build ${dir}${x.stopAfter ? " --stop-after link" : ""}`,
    `harness-manager kit check ${dir} ${bid}`, `harness-manager kit pack ${dir} --import`];
  return html`<div class="bd-foot">
    <details class="card bd-fold" data-testid="build-cli"><summary><${Icon} name="chevron-right" />The same on the command line</summary>
      <div class="bd-fold-body">${cmds.map((c) => html`<${CopyLine} key=${c} text=${c} />`)}
        <div class="small muted">kit build prints the Vivado command; it never runs Vivado. <span class="mono">harness-manager kit guide ${bid} --design ${des}</span> shows these steps' states.</div>
        <div class="bd-cli-t">Your constraints</div>
        <div class="small secondary">An rm_xdc lives in the design .json's build, as a path relative to the design file; kit script writes it into build_rm.tcl as RM_XDC. A built-in carries none: use a design .json.</div>
        <${CopyLine} text=${`grep -E 'RM_XDC|STOP_AFTER' ${dir}/build_rm.tcl`} /></div></details>
    <details class="card bd-fold" data-testid="trouble-card"><summary><${Icon} name="chevron-right" />Every build gate and its fix (${(t.gates || []).length})${fails.length ? html` <${Chip} level="err" cls="bd-mini">${fails.length} failing<//>` : null}</summary>
      <div data-testid="trouble-list">
        ${fails.map((v) => html`<${TroubleItem} key=${`f-${v.card}`} ...${v} fix=${fixOf(v.card)} failing=${true} />`)}
        ${(t.gates || []).filter((gg) => !failing.has(gg.gate)).map((gg) => html`<${TroubleItem} key=${gg.gate}
          card=${gg.gate} name=${gg.gate} fix=${gg.fix} failing=${false} />`)}
      </div></details>
    <${XdcFold} bid=${bid} />
  </div>`;
}

// --- the tab -----------------------------------------------------------------------------------

export function BuildSection({ bid }) {
  const x = st(bid);
  // a route (#/<board>/build/<step>, or navigate() from another tab) picks the step on view
  const chosenSub = (APP.subs[bid] || {}).build || "";
  if (chosenSub !== (x.seen || "")) { x.view = chosenSub || null; x.seen = chosenSub; }
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
  const f = flow(bid);
  // While Vivado may be running (the directory is written, no verdict yet), read the guide
  // again every 10 s: the stage, the elapsed time, and the receipt when it appears.
  const watching = x.chosen && !!x.written && (!f.r || !!f.running);
  useEffect(() => {
    if (!watching) return undefined;
    const t = setInterval(() => { if (document.visibilityState !== "hidden") loadGuide(bid); }, 10000);
    return () => clearInterval(t);
  }, [bid, watching]);
  const top = useRef(null);
  useEffect(() => {
    const el = top.current && top.current.closest(".section-body");
    if (el && !(APP.ui.reveal && APP.ui.reveal.bid === bid)) el.scrollTop = 0;
  }, [f.view]);
  const Panels = { setup: SetupPanel, design: DesignPanel, build: BuildPanel, check: CheckPanel, add: AddPanel };
  const P = Panels[f.view];
  const vs = f.steps.find((s) => s.k === f.view);
  const row = APP.boards[bid] || {};
  const cand = (boardState(bid).info && boardState(bid).info.candidate) || row.candidate || {};
  return html`<div class="bd" data-testid="build-section" ref=${top}>
    <section class="card bd-head" aria-label="Build progress" data-testid="build-head">
      <div class="bd-top"><span class="bd-title"><${Icon} name="file-cog" />Build a design for ${boardName(cand, bid)}</span>
        <span class="small muted bd-tagline">from your RTL to a design on the Workbench</span>
        <span class="grow"></span>
        ${f.g ? html`<span class="small muted" data-testid="build-static">static <span class="mono">${hexId(f.g.static_id) || "unknown"}</span></span>` : null}
        <button type="button" class="btn ghost sm icon-only" title="Read the guide again" aria-label="Read the guide again"
          data-testid="build-reload" aria-busy=${x.guideLoading ? "true" : undefined} onClick=${() => loadGuide(bid)}>
          ${x.guideLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`}</button></div>
      <${Stepper} bid=${bid} f=${f} />
      <${Where} f=${f} />
      <${Ready} bid=${bid} f=${f} />
      <${LeaseNote} bid=${bid} />
      ${x.guideError && f.g ? html`<${Reason} level="err" testid="build-error" text=${errText(x.guideError)} />` : null}
    </section>
    ${f.cur && f.view !== f.cur.k ? html`<div class="bd-viewing" data-testid="bd-viewing"><${Icon} name="eye" /><span>You are looking at <b>${vs.l}</b>; the current step is <b>${f.cur.l}</b>.</span>
      <${Btn} cls="sm" icon="chevron-right" testid="bd-back" onClick=${() => look(bid, f.cur.k, f.cur)}>Back to ${f.cur.l}<//></div>` : null}
    <${P} bid=${bid} f=${f} />
    <${Foot} bid=${bid} f=${f} />
  </div>`;
}

registerTab("build", BuildSection);
