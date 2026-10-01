// Bring a new board up from this PC (lane BRINGUP-USB; docs/API.md "Bring-up over the Debug USB").
//
// Two parts, both registered here so no integrator file changes:
//
//   OverUsb        the Add dialog's third way (add.js): Scan lists the MPS3 Debug USBs this PC
//                  sees (POST /bringup/scan: the daemon's USB probe), each with its MCC port, its
//                  V2M-MPS3 drive and what it holds, what the MCC answers, and whether a harness
//                  answers on Ethernet; "Add and bring up" opens it as a USB-only board
//                  (mps3@usb:...) and opens the wizard.
//   the wizard     openModal("bringup", {bid}): a dialog, not a Board page, because a new board
//                  has no harness yet (most Board pages need one) and the Board tab's pages are
//                  the integrator's (route.js). Its state lives on the board (boardState(bid).
//                  bringup), so closing it never loses a step: the jobs run in the service, and
//                  "Bring up…" on Board > Versions reopens it where it was.
//
// The wizard composes what exists: the SD backup (sd.js backupSpec), the bundle write
// (POST .../bringup/install, the storage install job), the MCC reboot (power.js rebootSpec),
// the restore (POST .../storage/restore), a signed release's install (harness/install), and
// SD-FLASH's card-reader routes (POST /cardwriter/write; disabled with the reason when its
// setting is off or the routes are not in this build). Every write is armed, needs the backup
// first, and runs one at a time per board (the service's board gate).

import { panelState, runAction } from "./actions.js";
import { bringupCall, bringupMissing, call, toApiError, waitJob } from "./api.js";
import { boardName, bytesText, capState } from "./format.js";
import { html, useEffect, useState } from "./lib.js";
import { closeModal, ModalShell, openModal, registerModal } from "./modal.js";
import { boardState, changed, loadBoards, log, navigate, probe, S, select, setJob, timed, toast } from "./store.js";
import { ActionRow, ArmBox, Chip, Icon, Reason, ResultBlock, Seg, Spinner } from "./ui.js";
import { week } from "./week.js";
import { openBoardHere } from "./sidebar.js";
import { backupSpec, SdRecovery } from "./sections/sd.js";
import { ARM_TEXT, REBOOT_GATE, rebootSpec } from "./sections/power.js";

// --- the stylesheet: this lane's, linked once (index.html is the integrator's: CCR BRINGUP-2) ---
(function linkSheet() {
  if (typeof document === "undefined" || document.querySelector('link[href$="css/bringup.css"]')) return;
  const l = document.createElement("link");
  l.rel = "stylesheet";
  l.href = "./css/bringup.css";
  document.head.appendChild(l);
}());

export const DEFAULT_HOST = "192.168.10.101";
export const USB_WARNING = "A USB write can take 5 minutes: do not unplug, power off or start a second write.";
export const NETWORK_OS_REASON = "comes with Linux v2.1 (HARNESS-DIST L3)";
export const READER_MISSING = "this build has no card-reader writer yet (lane SD-FLASH): use the Debug USB";
const pct = (d) => (d && d.total ? Math.floor((d.done * 100) / d.total) : 0);
const short = (sha) => (sha ? `${String(sha).slice(0, 16)}…` : "?");

// --- shared state: the service's switches, the card reader's devices ---------------------------

const G = { status: null, statusError: null, loading: false, reader: null, readerLoading: false };

export async function loadStatus() {
  if (G.loading) return;
  G.loading = true;
  changed();
  const r = await timed("bringup status", () => bringupCall("bringupStatus"));
  G.loading = false;
  if (r.error) G.statusError = r.error;
  else { G.status = r.data.data; G.statusError = null; }
  changed();
  if (G.status) loadReader();
}

// The card reader's state: {enabled, reason, devices}. Off (the setting), missing (404: SD-FLASH
// is not in this build) and refused all read as disabled, with the reason, never an error.
export async function loadReader() {
  const sw = G.status && G.status.sd_flash;
  if (sw && !sw.enabled) {
    G.reader = { enabled: false, reason: `SD card in this PC's card reader is ${sw.reason}`, devices: [] };
    changed();
    return;
  }
  G.readerLoading = true;
  changed();
  const r = await timed("cardwriter devices", () => bringupCall("cardwriterDevices"));
  G.readerLoading = false;
  if (r.error) {
    G.reader = { enabled: false, devices: [],
      reason: bringupMissing(r.error) ? READER_MISSING : `${r.error.errName}: ${r.error.message}` };
  } else {
    const d = r.data.data || {};
    G.reader = { enabled: d.enabled !== false, reason: d.reason || "", devices: d.devices || [] };
    if (G.reader.enabled && !G.reader.devices.length) G.reader.reason = "no card reader with a card in it: put the card in this PC's reader, then Read again";
  }
  changed();
}

// --- per-board state ---------------------------------------------------------------------------------

export function bs(bid) {
  const b = boardState(bid);
  if (!b.bringup) {
    b.bringup = {
      source: "bundle", path: "", check: null, checkError: null, checking: false,
      relSource: "", rel: null, relError: null, relLoading: false, pick: "", plan: null, planError: null,
      typed: "", method: "usb", device: "", typedDevice: "",
      written: null,          // {how: "usb" | "reader" | "release", at, ...}
      rebooted: null,         // the reboot's evidence (or the install's, for a release)
      replaced: false,        // the reader door: "the card is back in, the board is on"
      witness: null, witnessError: null, host: "",
      os: "", osImage: "", osDevice: "", osTyped: "", osDone: null,
      next: null,
    };
  }
  return b.bringup;
}

export function openBringup(bid) {
  if (!bid) return;
  openModal("bringup", { bid });
}

// A board reached over its Debug USB only (no Ethernet link): what the wizard is for.
export function usbOnly(bid) {
  const row = S.boards[bid] || {};
  const links = ((row.candidate || {}).links) || [];
  return links.some((l) => l.kind === "usb_serial" || l.kind === "usb_msd") && !links.some((l) => l.kind === "ethernet");
}

function linux(w) {
  if (w.source === "bundle") return !!(w.check && w.check.impl === "linux");
  const row = w.rel && (w.rel.releases || []).find((r) => r.version === w.pick);
  return !!(row && row.impl === "linux");
}

function hostOf(w) {
  return (w.host || "").trim() || (G.status && G.status.default_host) || DEFAULT_HOST;
}

function backupOf(bid) {
  const wk = week(bid);
  return (wk.lastBackup && wk.lastBackup.path) || "";
}

// One write at a time per board: the service's board gate covers the Debug USB jobs; a card in
// this PC's reader is not the board's job, so the wizard holds its own writes back too.
const WRITES = { bu_write: "the write", bu_reader: "the card write", bu_os: "the card write",
  bu_restore: "the restore", sd_backup: "the backup", reboot: "the reboot" };
export function otherWrite(bid, self) {
  for (const [panel, what] of Object.entries(WRITES)) {
    if (panel !== self && panelState(bid, panel).running) return `waiting for ${what} to finish (one write at a time)`;
  }
  return "";
}

// A job of a BRINGUP_ENDPOINTS route: the board is busy at once, then its result.
async function bringupJob(name, bid, body, onProgress, kind) {
  const { data } = await bringupCall(name, { bid }, body);
  if (!data.job) return data;
  setJob(bid, data.job, kind);
  return waitJob(data.job, { onProgress });
}

// --- the Add dialog's third way: Over USB --------------------------------------------------------

const U = { scan: null, error: null, running: false, at: 0, adding: "" };

export async function scanUsb() {
  U.running = true;
  U.error = null;
  changed();
  const r = await timed("bringup scan --ask-mcc", () => bringupCall("bringupScan", {}, { ask_mcc: true }));
  U.running = false;
  U.at = Date.now();
  if (r.error) U.error = r.error;
  else U.scan = r.data.data;
  log(r.error ? "error" : "info", "bringup", r.error ? `${r.line}  ${r.error.errName}: ${r.error.message}`
    : `${r.line}: ${(U.scan.boards || []).length} Debug USB(s)`);
  if (!r.error) await loadBoards();
  changed();
}

const MCC_WORDS = {
  answers: ["ok", "circle-check", "answers"],
  silent: ["warn", "triangle-alert", "no prompt"],
  held: ["unk", "lock", "not asked"],
  error: ["err", "circle-x", "error"],
  none: ["unk", "circle-help", "no MCC"],
  "not-asked": ["unk", "circle-help", "not asked"],
};

function MccLine({ row }) {
  if (!row.mcc) return html`<span class="muted">none: no MCC serial port with this drive</span>`;
  const a = row.mcc_answer || {};
  const [level, icon, word] = MCC_WORDS[a.state] || MCC_WORDS["not-asked"];
  const facts = [a.firmware ? `firmware ${a.firmware}` : "", a.board || ""].filter(Boolean).join(" · ");
  return html`<span class="line"><span class="mono">${row.mcc.port}</span>
    <${Chip} level=${level} icon=${icon} testid="usb-mcc-answer" title=${a.hint || ""}>${word}<//></span>
    <div class="sub">${a.text || ""}${facts ? ` · ${facts}` : ""}</div>`;
}

function DriveLine({ row }) {
  if (!row.volume) return html`<span class="muted">none: no V2M-MPS3 drive with this port</span>`;
  const d = row.drive || {};
  const rev = (d.revisions || []).join(", ");
  const loads = d.fpga_file ? `loads ${d.fpga_file}` : d.readable === false ? d.why : "no board file found";
  return html`<span class="mono">${row.volume.path}</span>
    <div class="sub">${[rev, loads, d.mb_bios ? `MB BIOS ${d.mb_bios} (never written)` : "", d.journal ? "an interrupted install's journal is on it" : ""].filter(Boolean).join(" · ")}</div>`;
}

function EthLine({ eth }) {
  if (!eth) return html`<span class="muted">not paired (more than one board on USB)</span>`;
  const level = eth.state === "running" ? "ok" : eth.state === "rescue" ? "warn" : "";
  return html`<span class="line"><${Chip} level=${level} icon=${eth.state === "none" ? "circle-dashed" : "ethernet-port"}
      testid="usb-eth">${eth.state === "running" ? "harness answers" : eth.state === "rescue" ? "stage0 rescue" : "nothing answers"}<//></span>
    <div class="sub">${eth.text}</div>`;
}

async function addAndBringUp(row) {
  const bid = row.board_id;
  U.adding = bid;
  changed();
  if (!S.boards[bid]) S.boards[bid] = { board_id: bid, candidate: row.candidate, open: false };
  if (!(S.boards[bid] || {}).candidate) S.boards[bid] = { ...S.boards[bid], candidate: row.candidate };
  if (!S.order.includes(bid)) S.order.push(bid);
  const opened = row.open ? { error: null } : await openBoardHere(bid);
  U.adding = "";
  if (opened && opened.error && opened.error.errName !== "ALREADY") {
    U.error = opened.error;
    changed();
    return;
  }
  closeModal();
  select(bid);
  toast(`Added ${boardName(row.candidate, bid)} over USB`, { icon: "usb" });
  openBringup(bid);
}

function UsbRow({ row }) {
  const busy = U.adding === row.board_id;
  const why = row.holder ? `held by ${row.holder}: close it there first` : "";
  return html`<li class="bu-usb" data-testid="usb-board" data-board=${row.board_id}>
    <div class="bu-usb-head"><${Icon} name="usb" /><b>MPS3 Debug USB</b>
      <span class="mono small muted">${row.board_id}</span><span class="grow"></span>
      ${row.open ? html`<${Chip} level="accent" icon="check">open here<//>` : null}</div>
    <dl class="kv">
      <dt>MCC</dt><dd data-testid="usb-mcc"><${MccLine} row=${row} /></dd>
      <dt>Drive</dt><dd data-testid="usb-drive"><${DriveLine} row=${row} /></dd>
      <dt>Ethernet</dt><dd><${EthLine} eth=${row.ethernet} /></dd>
    </dl>
    ${(row.problems || []).map((p) => html`<${Reason} key=${p} level="warn" testid="usb-problem" text=${p} />`)}
    <div class="row bu-usb-foot">
      ${why ? html`<${Reason} icon="lock" text=${why} />` : null}
      <span class="grow"></span>
      <button type="button" class="btn primary sm" data-action="usb-add" disabled=${!!why || busy}
        aria-busy=${busy ? "true" : undefined} onClick=${() => addAndBringUp(row)}>
        ${busy ? html`<${Spinner} />` : html`<${Icon} name="plus" />`} ${row.open ? "Bring it up" : "Add and bring up"}</button>
    </div>
  </li>`;
}

// Nothing is scanned until Scan is clicked: a scan types one "?" at each MCC it finds.
export function OverUsb() {
  const s = U.scan;
  const rows = (s && s.boards) || [];
  return html`<div class="stack bu-over-usb" data-testid="add-over-usb">
    <p class="small secondary">A new board plugged into this PC: its Debug USB (the board controller, MCC, and the configuration SD as the drive <span class="mono">V2M-MPS3</span>). Nothing is written until you arm a step in the bring-up.</p>
    <div class="row">
      <button type="button" class="btn sm" data-action="usb-scan" disabled=${U.running}
        aria-busy=${U.running ? "true" : undefined} onClick=${scanUsb}>
        ${U.running ? html`<${Spinner} />` : html`<${Icon} name="scan-search" />`} Scan</button>
      <span class="small muted" data-testid="usb-scan-note">${U.running ? "Listing the Debug USBs and asking each MCC…" : "Lists the Debug USBs, asks each MCC for its prompt, and looks for a harness at 192.168.10.101."}</span>
    </div>
    ${U.error ? html`<${Reason} level="err" testid="usb-error" text=${`${U.error.errName}: ${U.error.message}${U.error.hint ? ` (${U.error.hint})` : ""}`} />` : null}
    ${s && !rows.length ? html`<div class="bu-empty" data-testid="usb-none">
      <${Reason} level="warn" text=${(s.empty && s.empty.text) || "No MPS3 Debug USB found on this PC."} />
      <p class="small secondary">Check:</p>
      <ul class="small bu-checks">${((s.empty && s.empty.check) || []).map((c) => html`<li key=${c}>${c}</li>`)}</ul>
      ${s.ethernet && s.ethernet.state !== "none" ? html`<${Reason} testid="usb-none-eth" text=${`${s.ethernet.text}: add it By address.`} />` : null}
    </div>` : null}
    ${(s && s.notes || []).map((n) => html`<${Reason} key=${n} testid="usb-note" text=${n} />`)}
    ${rows.length ? html`<ul class="bu-usb-list" data-testid="usb-list">${rows.map((row) => html`<${UsbRow} key=${row.board_id} row=${row} />`)}</ul>` : null}
  </div>`;
}

// For tests and the devtools console.
window.__harness_managerBringup = () => JSON.parse(JSON.stringify({ U, G }));

// --- the wizard: one step ---------------------------------------------------------------------------

function Step({ n, title, state = "", children, testid = "" }) {
  return html`<li class=${`flow-step bu-step ${state}`} data-testid=${testid || undefined} data-state=${state || "todo"}>
    <div class="flow-n">${state === "done" ? html`<${Icon} name="check" cls="sm" />` : n}</div>
    <div class="flow-body"><h3 class="flow-title">${title}</h3>${children}</div>
  </li>`;
}

// --- 1. the source ---------------------------------------------------------------------------------

async function checkBundle(bid) {
  const w = bs(bid);
  const path = w.path.trim();
  if (!path) return;
  w.checking = true;
  w.check = null;
  w.checkError = null;
  changed();
  const r = await timed(`bringup bundle ${path}`, () => bringupCall("bringupBundle", {}, { path }));
  w.checking = false;
  if (r.error) {
    w.checkError = r.error;
    w.check = (r.error.data && r.error.data.check) || null;
  } else {
    w.check = r.data.data.check;
  }
  log(r.error ? "error" : "info", "bringup", r.error ? `${r.line}  ${r.error.errName}: ${r.error.message}` : `${r.line}: ${w.check.count} files`, bid);
  changed();
}

function BundleFacts({ chk }) {
  const bit = chk.base_bit;
  return html`<div class="bu-check" data-testid="bundle-check" data-refused=${chk.refused ? "yes" : "no"}>
    <dl class="kv">
      <dt>Bundle</dt><dd><span class="mono">${chk.path}</span>
        <div class="sub">${chk.kind === "zip" ? "a zip, unpacked by the service" : "a folder"} · ${chk.layout === "release-bundle" ? "a release bundle (its sd/)" : "a config-SD tree"}${chk.impl ? ` · ${chk.impl === "linux" ? "Linux" : "bare-metal"} harness${chk.version ? ` ${chk.version}` : ""}` : ""}</div></dd>
      <dt>Base .bit</dt><dd data-testid="bundle-bit">${bit ? html`<span class="mono">${bit.path}</span>
        <div class="sub">${bytesText(bit.size)} · sha256 <span class="mono" title=${bit.sha256}>${short(bit.sha256)}</span>${bit.part ? ` · ${bit.part}` : ""}${bit.userid ? ` · USERID ${bit.userid}` : ""}</div>`
        : html`<span class="muted">none</span>`}</dd>
      <dt>Writes</dt><dd data-testid="bundle-files">${chk.count} file${chk.count === 1 ? "" : "s"}, ${bytesText(chk.total_bytes)}, onto the V2M-MPS3 drive
        <ul class="bu-files mono small">${(chk.files || []).map((f) => html`<li key=${f.path}>${f.path} <span class="muted">${bytesText(f.size)}</span></li>`)}</ul></dd>
      ${chk.os_image ? html`<dt>Slot image</dt><dd><span class="mono">linux_slot.img</span><div class="sub">${bytesText(chk.os_image.size)} · an OS slot image (slot A/B, over Ethernet once Linux runs): never written to a card, and not the whole-card image step 5 asks for</div></dd>` : null}
      ${chk.overlays ? html`<dt>Overlays</dt><dd data-testid="bundle-overlays">${chk.overlays.count} open overlay${chk.overlays.count === 1 ? "" : "s"} (${chk.overlays.names.join(", ")})<div class="sub">after the write, <span class="mono">${chk.overlays.path}</span> joins mps3.overlay_dirs, so Program and Restore find them</div></dd>` : null}
    </dl>
    ${(chk.problems || []).map((p) => html`<${Reason} key=${p} level="err" testid="bundle-problem" text=${p} />`)}
    ${(chk.warnings || []).map((p) => html`<${Reason} key=${p} level="warn" text=${p} />`)}
    ${(chk.ignored || []).length ? html`<${Reason} text=${`ignored (desktop files): ${chk.ignored.join(", ")}`} />` : null}
    ${chk.refused ? html`<${Reason} level="err" testid="bundle-refused" text="Refused: fix the bundle. Nothing was written." />` : null}
  </div>`;
}

function BundleSource({ bid, w }) {
  const examples = (G.status && G.status.examples) || [];
  return html`<div class="stack gap-12">
    <div class="field"><label for=${`bu-path-${bid}`}>Folder or zip</label>
      <input id=${`bu-path-${bid}`} class="input mono grow" data-testid="bundle-path"
        placeholder="/home/me/mps3-harness-1.1.0  or  …/mps3-harness-1.1.0.zip" value=${w.path}
        onInput=${(e) => { w.path = e.target.value; changed(); }}
        onKeyDown=${(e) => { if (e.key === "Enter") checkBundle(bid); }} />
      <button type="button" class="btn sm" data-action="bundle-check" disabled=${!w.path.trim() || w.checking}
        aria-busy=${w.checking ? "true" : undefined} onClick=${() => checkBundle(bid)}>
        ${w.checking ? html`<${Spinner} />` : html`<${Icon} name="list-checks" />`} Check</button></div>
    <p class="small muted">A path on the machine running harness-manager-daemon: the config-SD tree (config.txt and MB/), a release bundle (its sd/), or a zip of either. Never an .ebf.</p>
    ${examples.length ? html`<div class="bu-examples small" data-testid="bundle-examples"><span class="muted">In this demo:</span>
      ${examples.map((x) => html`<button type="button" key=${x.path} class="link-btn" title=${x.path}
        onClick=${() => { w.path = x.path; changed(); checkBundle(bid); }}>${x.what}</button>`)}</div>` : null}
    ${w.checkError && !w.check ? html`<${Reason} level="err" testid="bundle-error" text=${`${w.checkError.errName}: ${w.checkError.message}${w.checkError.hint ? ` (${w.checkError.hint})` : ""}`} />` : null}
    ${w.check ? html`<${BundleFacts} chk=${w.check} />` : null}
  </div>`;
}

async function readReleases(bid) {
  const w = bs(bid);
  w.relLoading = true;
  w.relError = null;
  w.pick = "";
  w.plan = null;
  changed();
  const src = w.relSource.trim();
  try {
    const { data } = await call("harnessRefresh", {}, { board_id: bid, ...(src ? { source: src } : {}),
      ...(w.relAll ? { all: true } : {}) });
    setJob(bid, data.job, "harness_refresh");
    w.rel = await waitJob(data.job, {});
  } catch (e) {
    w.relError = toApiError(e);
    w.rel = null;
  }
  w.relLoading = false;
  log(w.relError ? "error" : "info", "bringup", w.relError ? `harness list ${bid}${src ? ` --source ${src}` : ""}: ${w.relError.errName}: ${w.relError.message}`
    : `harness list ${bid}: ${((w.rel && w.rel.releases) || []).length} release(s)`, bid);
  changed();
}

async function pickRelease(bid, version) {
  const w = bs(bid);
  w.pick = version;
  w.plan = null;
  w.planError = null;
  w.typed = "";
  changed();
  const r = await timed(`harness show ${version}`, () => call("harnessRelease", { version }, undefined, { board_id: bid }));
  if (w.pick !== version) return;
  if (r.error) w.planError = r.error;
  else w.plan = r.data.data;
  changed();
}

function ReleaseSource({ bid, w }) {
  const sign = (G.status && G.status.signing) || null;
  const rows = (w.rel && w.rel.releases) || [];
  const plan = w.plan && w.plan.plan;
  return html`<div class="stack gap-12">
    ${sign && sign.refused ? html`<${Reason} level="warn" testid="release-refused" icon="shield-check"
      text=${`Releases are refused here until signing keys exist (docs/KEYS.md): ${sign.name ? `${sign.name}: ` : ""}${sign.reason}${sign.hint ? ` (${sign.hint})` : ""}`} />` : null}
    <div class="field"><label for=${`bu-src-${bid}`}>Source</label>
      <input id=${`bu-src-${bid}`} class="input mono grow" data-testid="release-source"
        placeholder="the catalogue (default), github:OWNER/REPO, a URL or a mirror folder" value=${w.relSource}
        onInput=${(e) => { w.relSource = e.target.value; changed(); }} />
      <button type="button" class="btn sm" data-action="release-read" disabled=${w.relLoading}
        aria-busy=${w.relLoading ? "true" : undefined} onClick=${() => readReleases(bid)}>
        ${w.relLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Read the list</button>
      <label class="check-inline" title="Also list the beta and dev channels"><input type="checkbox" data-testid="release-all"
        checked=${!!w.relAll} onChange=${(e) => { w.relAll = e.target.checked; changed(); }} />beta and dev</label></div>
    ${w.relError ? html`<${Reason} level="err" testid="release-error" text=${`${w.relError.errName}: ${w.relError.message}${w.relError.hint ? ` (${w.relError.hint})` : ""}`} />` : null}
    ${w.rel && !rows.length ? html`<${Reason} level="warn" text="The channel lists no harness release." />` : null}
    ${rows.length ? html`<ul class="bu-rels" data-testid="release-rows">${rows.map((r) => html`<li key=${r.version}>
      <label class="check-inline"><input type="radio" name=${`bu-rel-${bid}`} checked=${w.pick === r.version}
        onChange=${() => pickRelease(bid, r.version)} />
        <b class="mono">${r.version}</b> <span class="small muted">${r.impl || "?"} · ${r.static_id || ""} · ${r.verdict || "no verdict"}</span></label></li>`)}</ul>` : null}
    ${w.planError ? html`<${Reason} level="err" text=${`${w.planError.errName}: ${w.planError.message}`} />` : null}
    ${plan ? html`<div class="bu-plan" data-testid="release-plan">
      <ul class="small">${(plan.steps || []).map((s, i) => html`<li key=${i}>${s.detail || s.what || s.step || JSON.stringify(s)}</li>`)}</ul>
      ${(plan.blockers || []).map((b) => html`<${Reason} key=${b} level="err" testid="release-blocker" text=${`The planner refuses this release here: ${b}`} />`)}
      ${(plan.blockers || []).length && linux(w) ? html`<${Reason} testid="release-linux-usb"
        text="A Linux release cannot be installed over the Debug USB today: its OS image needs the running harness (HARNESS-DIST L3). Write its configuration SD from its bundle (a folder or zip), then the user microSD with a whole-card image in step 5." />` : null}
      ${plan.consent_phrase ? html`<div class="field"><label>Type <code>${plan.consent_phrase}</code></label>
        <input class="input mono grow" data-testid="release-phrase" value=${w.typed}
          onInput=${(e) => { w.typed = e.target.value; changed(); }} /></div>` : null}
    </div>` : null}
  </div>`;
}

function sourceWhy(w) {
  if (w.source === "bundle") return w.check && w.check.refused ? "the bundle is refused (step 1)" : "choose and check the source first (step 1)";
  const plan = w.plan && w.plan.plan;
  if (plan && (plan.blockers || []).length) return `the planner refuses this release here: ${plan.blockers[0]}`;
  if (plan && plan.consent_phrase && w.typed.trim() !== plan.consent_phrase) return `type ${plan.consent_phrase} in step 1 to confirm the re-key`;
  return "choose and check the source first (step 1)";
}

function sourceReady(w) {
  if (w.source === "bundle") return !!(w.check && !w.check.refused);
  const plan = w.plan && w.plan.plan;
  return !!(plan && !(plan.blockers || []).length && (!plan.consent_phrase || w.typed.trim() === plan.consent_phrase));
}

function SourceStep({ bid, w, done }) {
  return html`<${Step} n="1" title="Source: what to put on the board" state=${done ? "done" : ""} testid="bu-step-source">
    <${Seg} label="Source" value=${w.source} onChange=${(v) => { w.source = v; changed(); }}
      options=${[{ value: "bundle", label: "A bundle folder or zip on this PC", icon: "folder-input" },
        { value: "release", label: "A signed harness release", icon: "shield-check" }]} />
    <div class="mt-8">${w.source === "bundle" ? html`<${BundleSource} bid=${bid} w=${w} />` : html`<${ReleaseSource} bid=${bid} w=${w} />`}</div>
  <//>`;
}

// --- 2. the backup -------------------------------------------------------------------------------------

function BackupStep({ bid, w, ready }) {
  const p = panelState(bid, "sd_backup");
  const path = backupOf(bid);
  const spec = backupSpec(bid);
  return html`<${Step} n="2" title="Back up the configuration SD" state=${path ? "done" : ""} testid="bu-step-backup">
    <p class="small secondary">Mandatory: a zip of the whole card with a sha256 manifest, kept in this service's state. It is your way back.</p>
    <${ActionRow} bid=${bid} panel="sd_backup" spec=${spec} icon="download"
      gate=${{ capability: "storage_backup", adapter: "storage", holder: "Back up the SD",
        guard: () => otherWrite(bid, "sd_backup") || (ready ? "" : sourceWhy(w)) }} />
    ${path ? html`<p class="small" data-testid="bu-backup-path">Backup <span class="mono">${path}</span></p>` : null}
    <${ResultBlock} lines=${p.lines} panel=${p} testid="bu-backup-result" />
  <//>`;
}

// --- 3. the write -------------------------------------------------------------------------------------

function readerChoice(w, kind) {
  const r = G.reader;
  if (!r) return { ok: false, why: G.readerLoading ? "reading the card readers…" : "not read yet" };
  if (!r.enabled) return { ok: false, why: r.reason || "off" };
  if (kind === "files" && w.source !== "bundle") return { ok: false, why: "a signed release goes through the Debug USB (its own install); for the card reader, use a bundle folder or zip" };
  if (!r.devices.length) return { ok: false, why: r.reason || "no card reader with a card in it" };
  return { ok: true, why: "" };
}

function confirmFor(dev) { return dev ? `WRITE ${dev.model} ${dev.size_bytes}` : ""; }

function DevicePicker({ w, field, typedField, kind }) {
  const r = G.reader || { devices: [] };
  const dev = r.devices.find((d) => d.id === w[field]) || null;
  return html`<div class="stack gap-8 bu-reader">
    <div class="field"><label>Card reader</label>
      <select class="select grow" data-testid=${`reader-device-${kind}`} value=${w[field]}
        onChange=${(e) => { w[field] = e.target.value; w[typedField] = ""; changed(); }}>
        <option value="">Choose the card…</option>
        ${r.devices.map((d) => html`<option key=${d.id} value=${d.id} disabled=${d.writable === false}>${d.model} · ${bytesText(d.size_bytes)} · ${d.path}${d.writable === false ? ` (${d.why_not || "not writable"})` : ""}</option>`)}
      </select>
      <button type="button" class="btn ghost sm" onClick=${loadReader} title="Read the card readers again"><${Icon} name="refresh-cw" /></button></div>
    ${dev ? html`<div class="field"><label>Type <code>${confirmFor(dev)}</code></label>
      <input class="input mono grow" data-testid=${`reader-confirm-${kind}`} value=${w[typedField]}
        onInput=${(e) => { w[typedField] = e.target.value; changed(); }} /></div>` : null}
  </div>`;
}

async function readerWrite(bid, ctx, kind, deviceId, source, confirm) {
  const { data } = await bringupCall("cardwriterWrite", {}, { device_id: deviceId, kind, source, confirm });
  if (!data.job) return data;
  return waitJob(data.job, { onProgress: (d) => ctx.progress(`${d.phase || "write"}: ${pct(d)}%`, d.phase) });
}

function readerLines(res, what) {
  if (res && res.needs_privilege) {
    return [{ kind: "warnline", text: "This PC's user cannot write the card: run the command below yourself (Harness Manager never escalates)." },
      { kind: "out", text: res.privileged_command || "" },
      ...(res.verify ? [{ kind: "hint", text: `then verify: ${res.verify}` }] : [])];
  }
  return [{ kind: "ok", text: `${what} written${res && res.verified ? " and verified" : ""}${res && res.sha256 ? ` (sha256 ${short(res.sha256)})` : ""}` }];
}

function overlayLines(o) {
  if (!o) return [];
  if (o.error) return [{ kind: "warnline", text: `the bundle's overlays were not added to mps3.overlay_dirs: ${o.error}` }];
  if (o.shadowed) return [{ kind: "warnline", text: `${o.path} is in settings.toml's mps3.overlay_dirs, but $HARNESS_MANAGER_MPS3_OVERLAY_DIRS in the service's environment hides it` }];
  return [{ kind: "out", text: `${o.count} overlay(s) ${o.added ? "added to" : "already in"} mps3.overlay_dirs: ${o.path} (Program and Restore find them)` }];
}

function WriteStep({ bid, w, ready }) {
  const backup = backupOf(bid);
  const st = G.status || {};
  const reader = readerChoice(w, "files");
  const usb = w.method === "usb";
  const pu = panelState(bid, "bu_write");
  const pr = panelState(bid, "bu_reader");
  const guard = () => {
    const busy = otherWrite(bid, usb ? "bu_write" : "bu_reader");
    if (busy) return busy;
    if (!ready) return sourceWhy(w);
    if (!backup) return "back up the SD first (step 2)";
    return "";
  };
  const release = w.source === "release";
  const usbSpec = release ? {
    key: "bu_write", label: "Install over the Debug USB", busyLabel: "Installing...", budgetS: 900,
    command: `harness install ${bid} ${w.pick} --door usb`,
    run: async (ctx) => {
      const plan = (w.plan && w.plan.plan) || {};
      const { data } = await call("harnessInstall", { bid }, {
        fingerprint: plan.fingerprint, version: w.pick, via: "usb",
        ...(w.relSource.trim() ? { source: w.relSource.trim() } : {}),
        ...(plan.consent_phrase ? { rekey_phrase: w.typed.trim() } : {}),
      });
      setJob(bid, data.job, "harness_install");
      try {
        return await waitJob(data.job, { onProgress: (d) => ctx.progress(`${d.phase || "install"}: ${pct(d)}%`, d.phase) });
      } catch (e) {
        // RELEASE-PIPE: over the Debug USB alone nothing can confirm the harness, so a written
        // and rebooted board ends written-not-running BY DESIGN: written; now witness (step 4).
        const out = e && e.data && e.data.outcome;
        if (out && out.result === "written-not-running" && usbOnly(bid)) return { ...out, usb_only: true };
        throw e;
      }
    },
    render: (out) => (out && out.usb_only
      ? [{ kind: "ok", text: `harness ${w.pick} written to the configuration SD, and the board rebooted` },
        { kind: "out", text: "Over the Debug USB alone nothing can confirm the harness: step 4 waits for it on Ethernet." }]
      : [{ kind: "ok", text: (out && out.detail) || "installed" }]),
    onDone: (ok, out) => {
      if (ok) {
        w.written = { how: "release", at: Date.now(), version: w.pick };
        w.rebooted = (out && out.evidence) || { summary: "rebooted by the install" };
        w.witness = null; w.witnessError = null;
      }
      changed();
    },
  } : {
    key: "bu_write", label: "Write over the Debug USB", busyLabel: "Writing...", budgetS: 600,
    command: `sd - install ${w.check ? w.check.path : "?"} --backup ${backup || "?"}`,
    run: (ctx) => bringupJob("bringupInstall", bid, { bundle: w.check.path, backup_path: backup },
      (d) => ctx.progress(`${d.phase || "install"}: ${pct(d)}%`, d.phase), "sd_install"),
    render: (res) => [{ kind: "ok", text: `wrote ${(res.files || []).length} file(s) to the configuration SD and read them back; the board runs them after a reboot` },
      ...overlayLines(res && res.overlays)],
    onDone: (ok) => {
      if (ok) { w.written = { how: "usb", at: Date.now() }; w.rebooted = null; w.witness = null; w.witnessError = null; }
      changed();
    },
  };
  const dev = (G.reader && G.reader.devices || []).find((d) => d.id === w.device) || null;
  const readerSpec = {
    key: "bu_reader", label: "Write the card in this PC's reader", busyLabel: "Writing...", budgetS: 600,
    command: `cardwriter write ${w.device || "?"} --kind files ${w.check ? w.check.sd_root : "?"}`,
    run: (ctx) => readerWrite(bid, ctx, "files", w.device, w.check.sd_root, w.typedDevice.trim()),
    render: (res) => readerLines(res, "the configuration SD files"),
    onDone: (ok, res) => {
      if (ok && !(res && res.needs_privilege)) { w.written = { how: "reader", at: Date.now() }; w.replaced = false; w.witness = null; w.witnessError = null; }
      changed();
    },
  };
  const readerGuard = () => guard() || (!reader.ok ? reader.why : !dev ? "choose the card" : w.typedDevice.trim() !== confirmFor(dev) ? `type ${confirmFor(dev)} to confirm` : "");
  return html`<${Step} n="3" title="Write the configuration SD" state=${w.written ? "done" : ""} testid="bu-step-write">
    <${Seg} label="Write method" value=${w.method} onChange=${(v) => { w.method = v; if (v === "reader" && !G.reader) loadReader(); changed(); }}
      options=${[{ value: "usb", label: "Over the Debug USB (the V2M-MPS3 drive)", icon: "usb" },
        { value: "reader", label: "SD card in this PC's card reader", icon: "memory-stick", title: reader.ok ? "" : reader.why }]} />
    ${usb && !reader.ok ? html`<p class="small muted mt-8" data-testid="bu-reader-off-note"><${Icon} name="circle-slash" cls="sm" /> SD card in this PC's card reader: ${reader.why}.</p>` : null}
    ${usb ? html`<div class="stack gap-8 mt-8">
      <div class="outcome warn bu-warn" data-testid="bu-usb-warning"><${Icon} name="triangle-alert" /><span><b>${st.usb_write_warning || USB_WARNING}</b></span></div>
      <${ArmBox} bid=${bid} armKey="bu_write" testid="arm-bu-write"
        text=${release ? "Arm: I understand this installs the release onto the board's configuration SD over the Debug USB and reboots the board (journaled; the backup restores it)."
          : "Arm: I understand this writes the board's configuration SD over the Debug USB (journaled; the backup restores it)."} />
      <${ActionRow} bid=${bid} panel="bu_write" spec=${usbSpec} variant="primary" icon="upload"
        gate=${{ capability: "storage_install", adapter: "storage", arm: "bu_write", guard, holder: "Write the configuration SD" }} />
      <${ResultBlock} lines=${pu.lines} panel=${pu} testid="bu-write-result" />
    </div>` : html`<div class="stack gap-8 mt-8">
      ${!reader.ok ? html`<${Reason} icon="circle-slash" testid="bu-reader-disabled" text=${`SD card in this PC's card reader: ${reader.why}.`} />` : null}
      <p class="small secondary">Take the configuration SD out of the board (power it off first) and put it in this PC's card reader. Only a card reader the service lists is offered; never this PC's own disk, never the board's V2M-MPS3 drive.</p>
      ${reader.ok ? html`<${DevicePicker} w=${w} field="device" typedField="typedDevice" kind="files" />` : null}
      <${ArmBox} bid=${bid} armKey="bu_reader" testid="arm-bu-reader"
        text="Arm: I understand this writes the configuration SD files onto the card in this PC's reader (the backup restores the board's card)." />
      <${ActionRow} bid=${bid} panel="bu_reader" spec=${readerSpec} variant="primary" icon="memory-stick"
        gate=${{ arm: "bu_reader", guard: readerGuard, holder: "Write the configuration SD" }} />
      <${ResultBlock} lines=${pr.lines} panel=${pr} testid="bu-reader-result" />
    </div>`}
  <//>`;
}

// --- 4. reboot and witness -------------------------------------------------------------------------

function witnessSpec(bid, w) {
  const host = hostOf(w);
  const waits = (G.status && G.status.witness_s) || {};
  const wait = linux(w) ? (waits.linux || 300) : (waits["bare-metal"] || 180);
  return {
    key: "bu_witness", label: `Wait for the harness at ${host}`, busyLabel: "Waiting...", budgetS: wait + 30,
    command: `bringup witness ${host} --wait ${wait}`,
    run: (ctx) => bringupJob("bringupWitness", bid, { host, wait_s: wait },
      (d) => ctx.progress(`${d.phase === "waiting" ? "nothing yet" : d.phase}: ${d.done} s of ${d.total} s`, "witness"), "bringup_witness"),
    render: (res) => [{ kind: "ok", text: res.text || `answered at ${host}` },
      { kind: "out", text: `after ${res.took_s} s · ${res.evidence || ""}` }],
    onDone: (ok, val) => {
      if (ok) { w.witness = val; w.witnessError = null; } else { w.witness = null; w.witnessError = val; }
      changed();
    },
  };
}

function restoreSpec(bid) {
  const backup = backupOf(bid);
  return {
    key: "bu_restore", label: "Restore the backup", busyLabel: "Restoring...", budgetS: 900,
    command: `sd - restore ${backup || "?"}`,
    run: (ctx) => call("sdRestore", { bid }, { backup_path: backup }).then(({ data }) => {
      setJob(bid, data.job, "sd_restore");
      return waitJob(data.job, { onProgress: (d) => ctx.progress(`${d.phase || "restore"}: ${pct(d)}%`, d.phase) });
    }),
    render: () => [{ kind: "ok", text: `restored the configuration SD from ${backup}; reboot the board to run what it held` }],
    onDone: (ok) => { if (ok) { const w = bs(bid); w.written = null; w.rebooted = null; } changed(); },
  };
}

function RebootStep({ bid, w }) {
  const host = hostOf(w);
  const pr = panelState(bid, "reboot");
  const pw = panelState(bid, "bu_witness");
  const px = panelState(bid, "bu_restore");
  const st = G.status || {};
  const reader = w.written && w.written.how === "reader";
  const release = w.written && w.written.how === "release";
  const spec = { ...rebootSpec(bid), onDone: (ok, ev) => { if (ok) { w.rebooted = ev || {}; w.witness = null; w.witnessError = null; } changed(); } };
  const canWitness = !!(w.rebooted || (reader && w.replaced));
  const timeout = w.witnessError && w.witnessError.data && w.witnessError.data.timeout;
  const done = !!w.witness;
  return html`<${Step} n="4" title=${reader ? "Put the card back, then witness the harness" : "Reboot through the MCC, then witness the harness"}
      state=${done ? "done" : ""} testid="bu-step-reboot">
    ${reader ? html`<div class="stack gap-8">
      <div class="outcome bu-replace" data-testid="bu-put-back"><${Icon} name="info" /><span>No MCC reboot with the card reader: <b>put the card back in the board's configuration SD slot and power the board on.</b></span></div>
      <label class="check-inline"><input type="checkbox" data-testid="bu-replaced" checked=${w.replaced} onChange=${(e) => { w.replaced = e.target.checked; changed(); }} />
        The card is back in the board and it is powered on</label>
    </div>` : release ? html`<p class="small secondary" data-testid="bu-release-rebooted">The release's install rebooted the board through the MCC.</p>` : html`<div class="stack gap-8">
      <p class="small secondary">The board loads the new configuration only after the MCC reloads it from the SD.</p>
      <${ArmBox} bid=${bid} armKey="reboot" text=${ARM_TEXT.reboot} />
      <${ActionRow} bid=${bid} panel="reboot" spec=${spec} variant="danger" icon="power"
        gate=${{ ...REBOOT_GATE, guard: () => otherWrite(bid, "reboot") || (w.written ? "" : "write the SD first (step 3)") }} />
      <${ResultBlock} lines=${pr.lines} panel=${pr} testid="bu-reboot-result" />
    </div>`}
    <div class="stack gap-8 mt-8 bu-witness">
      <div class="field"><label for=${`bu-host-${bid}`}>It answers at</label>
        <input id=${`bu-host-${bid}`} class="input mono" data-testid="bu-host" value=${w.host} placeholder=${(st.default_host || DEFAULT_HOST)}
          onInput=${(e) => { w.host = e.target.value; changed(); }} /></div>
      <p class="small muted" data-testid="bu-pc-hint"><${Icon} name="ethernet-port" cls="sm" /> ${st.pc_hint || "this PC needs an Ethernet port on the board's network (192.168.10.0/24)"}.</p>
      <${ActionRow} bid=${bid} panel="bu_witness" spec=${witnessSpec(bid, w)} icon="ethernet-port"
        gate=${{ guard: () => (canWitness ? "" : reader ? "put the card back and power the board on first" : "reboot the board first") }} />
      <${ResultBlock} lines=${pw.lines} panel=${pw} testid="bu-witness-result" />
      ${timeout ? html`<div class="stack gap-8" data-testid="bu-timeout">
        <${Reason} level="err" text=${`Nothing answered at ${w.witnessError.data.host || host} within ${Math.round(w.witnessError.data.waited_s || 0)} s. Check the cable and this PC's address, wait again, or restore the backup.`} />
        <${ArmBox} bid=${bid} armKey="bu_restore" testid="arm-bu-restore" text="Arm: I understand this rewrites the configuration SD from the backup taken in step 2." />
        <${ActionRow} bid=${bid} panel="bu_restore" spec=${restoreSpec(bid)} icon="undo-2"
          gate=${{ capability: "storage_install", adapter: "storage", arm: "bu_restore", holder: "Restore the SD",
            guard: () => otherWrite(bid, "bu_restore") || (backupOf(bid) ? "" : "no backup to restore: take one in step 2") }} />
        <${ResultBlock} lines=${px.lines} panel=${px} testid="bu-restore-result" />
      </div>` : null}
    </div>
  <//>`;
}

// --- 5. the Linux OS image ------------------------------------------------------------------------

function osNeeded(w) {
  if (w.witness && w.witness.state === "rescue") return true;
  return linux(w) && !(w.witness && w.witness.state === "running" && w.witness.impl === "linux");
}

export const CARD_HINT = "a whole-card image (.img built with stage0_mkcard.py card --card-img)";

// "" when `path` may be offered as the whole-card image, else why not.
export function cardImageWhy(path) {
  const p = String(path || "").trim();
  if (!p) return "give the whole-card image's path";
  if (!/^(\/|[A-Za-z]:[\\/])/.test(p)) return "an absolute path on the machine running harness-manager-daemon";
  const name = p.split(/[\\/]/).pop().toLowerCase();
  if (name === "linux_slot.img") return "linux_slot.img is a SLOT image: written at byte 0 the card never boots. Give the whole-card image (stage0_mkcard.py card --card-img)";
  if (!name.endsWith(".img")) return "a whole-card image is an .img file";
  return "";
}

function OsStep({ bid, w }) {
  const st = G.status || {};
  const choice = readerChoice(w, "card");
  const po = panelState(bid, "bu_os");
  const dev = (G.reader && G.reader.devices || []).find((d) => d.id === w.osDevice) || null;
  const example = (st.card_image && st.card_image.example) || "";
  const imageWhy = cardImageWhy(w.osImage);
  const spec = {
    key: "bu_os", label: "Write the user microSD", busyLabel: "Writing...", budgetS: 1800,
    command: `cardwriter write ${w.osDevice || "?"} --kind card ${w.osImage || "?"}`,
    run: (ctx) => readerWrite(bid, ctx, "card", w.osDevice, w.osImage.trim(), w.osTyped.trim()),
    render: (res) => readerLines(res, "the whole-card image"),
    onDone: (ok, res) => { if (ok && !(res && res.needs_privilege)) w.osDone = { how: "reader", at: Date.now() }; changed(); },
  };
  const rescue = w.witness && w.witness.state === "rescue";
  const opts = [
    { value: "reader", label: "Write the board's user microSD with a whole-card image, in this PC's card reader", why: choice.ok ? "" : choice.why },
    { value: "network", label: "Over the network from rescue", why: (st.rescue_network && st.rescue_network.reason) || NETWORK_OS_REASON,
      note: (st.rescue_network && st.rescue_network.note) || "stage0 rescue boots an image from RAM; it does not write the card" },
    { value: "skip", label: "The card is already prepared: skip", why: "" },
  ];
  return html`<${Step} n="5" title="Linux OS image: the user microSD" state=${w.osDone ? "done" : ""} testid="bu-step-os">
    <p class="small secondary">${rescue ? "The board answers in stage0 RESCUE: its user microSD has no bootable OS slot yet." : "A Linux harness boots its OS from the user microSD (slot A/B). A card with no bootable slot leaves the board in stage0 RESCUE."}</p>
    <ul class="bu-os-opts" data-testid="bu-os-options">${opts.map((o) => html`<li key=${o.value} data-option=${o.value} data-disabled=${o.why ? "yes" : "no"}>
      <label class="check-inline"><input type="radio" name=${`bu-os-${bid}`} disabled=${!!o.why} checked=${w.os === o.value}
        onChange=${() => { w.os = o.value; if (o.value === "skip") w.osDone = { how: "skip", at: Date.now() }; changed(); }} />${o.label}</label>
      ${o.why ? html`<div class="sub bu-why" data-testid=${`bu-os-why-${o.value}`}>Disabled: ${o.why}${o.note ? html`<br /><span class="muted">${o.note}</span>` : null}</div>` : null}
    </li>`)}</ul>
    ${w.os === "reader" && choice.ok ? html`<div class="stack gap-8">
      <div class="field"><label for=${`bu-card-${bid}`}>Whole-card image</label>
        <input id=${`bu-card-${bid}`} class="input mono grow" data-testid="bu-card-image" placeholder=${CARD_HINT}
          value=${w.osImage || ""} onInput=${(e) => { w.osImage = e.target.value; changed(); }} /></div>
      <p class="small muted">${CARD_HINT[0].toUpperCase()}${CARD_HINT.slice(1)}: the MBR, the boot-select and both slots. Never a bundle's linux_slot.img.${example ? html` <button type="button" class="link-btn" onClick=${() => { w.osImage = example; changed(); }}>Use the demo's card image</button>` : null}</p>
      ${w.osImage && imageWhy ? html`<${Reason} level="err" testid="bu-card-why" text=${imageWhy} />` : null}
      <${DevicePicker} w=${w} field="osDevice" typedField="osTyped" kind="card" />
      <${ArmBox} bid=${bid} armKey="bu_os" testid="arm-bu-os" text="Arm: I understand this writes the whole card in this PC's reader with the image (everything on it is replaced)." />
      <${ActionRow} bid=${bid} panel="bu_os" spec=${spec} variant="primary" icon="memory-stick"
        gate=${{ arm: "bu_os", guard: () => otherWrite(bid, "bu_os") || imageWhy || (!dev ? "choose the card" : w.osTyped.trim() !== confirmFor(dev) ? `type ${confirmFor(dev)} to confirm` : "") }} />
      <${ResultBlock} lines=${po.lines} panel=${po} testid="bu-os-result" />
      ${w.osDone ? html`<p class="small" data-testid="bu-os-back">Put the card in the board's user microSD slot, power-cycle the board, then witness it again (step 4).</p>` : null}
    </div>` : null}
  <//>`;
}

// --- 6. next: the Access page ------------------------------------------------------------------------

async function openOnEthernet(bid, w) {
  const target = (w.witness && w.witness.board_id) || "";
  const host = hostOf(w);
  closeModal();
  await probe([host]);
  const id = target && S.boards[target] ? target : Object.keys(S.boards).find((k) => k.includes(`@${host}:`)) || target;
  if (!id) { toast(`Nothing to open at ${host}: add it By address`, { icon: "triangle-alert", level: "err" }); return; }
  const r = await openBoardHere(id);
  if (!r || !r.error || r.error.errName === "ALREADY") navigate(id, "board/access");
}

function NextStep({ bid, w }) {
  const ok = !!(w.witness && w.witness.state === "running");
  const rescue = !!(w.witness && w.witness.state === "rescue");
  const why = ok ? "" : rescue ? "the board is in stage0 RESCUE: do step 5, power-cycle it, then wait for the harness again (step 4)"
    : "once the harness answers (step 4)";
  return html`<${Step} n=${osNeeded(w) ? "6" : "5"} title="Next: claim it and set its identity" state="" testid="bu-step-next">
    <p class="small secondary">On Board > Access: claim its SSH (Linux), and give the board its own label, IP and MAC (every new board starts as MPS3, 192.168.10.101).</p>
    <div class="row"><button type="button" class="btn primary sm" data-action="bu-next" disabled=${!ok}
      title=${why} onClick=${() => openOnEthernet(bid, w)}>
      <${Icon} name="ethernet-port" /> Open it on Ethernet and go to Access</button>
      ${!ok ? html`<span class="small muted" data-testid="bu-next-why">${why}</span>` : null}</div>
  <//>`;
}

// --- the dialog ---------------------------------------------------------------------------------------

function BringupDialog({ bid }) {
  const w = bs(bid);
  const b = boardState(bid);
  useEffect(() => { loadStatus(); }, [bid]);
  const ready = sourceReady(w);
  const st = G.status;
  const row = S.boards[bid] || {};
  const name = boardName(row.candidate, bid);
  const inst = capState(b.info, "storage_install");
  const os = osNeeded(w);
  return html`<${ModalShell} title="Bring up this board" icon="rocket" cls="wide bringup" testid="bringup"
      note=${name}
      foot=${html`<span class="small muted grow">Closing keeps every step: Board > Versions, Bring up…, reopens it.</span>
        <button type="button" class="btn" data-action="bu-close" onClick=${closeModal}>Close</button>`}>
    ${G.statusError ? html`<${Reason} level="err" text=${`${G.statusError.errName}: ${G.statusError.message}`} />` : null}
    <${SdRecovery} bid=${bid} />
    ${inst && !inst.available ? html`<${Reason} icon="circle-slash" testid="bu-no-usb" text=${`The configuration SD is out of reach over USB here: ${inst.reason}. The card-reader door still works.`} />` : null}
    ${!st && !G.statusError ? html`<${Reason} icon="loader-circle" text="Reading the service's switches…" />` : null}
    <ol class="flow bu-flow">
      <${SourceStep} bid=${bid} w=${w} done=${ready} />
      <${BackupStep} bid=${bid} w=${w} ready=${ready} />
      <${WriteStep} bid=${bid} w=${w} ready=${ready} />
      <${RebootStep} bid=${bid} w=${w} />
      ${os ? html`<${OsStep} bid=${bid} w=${w} />` : null}
      <${NextStep} bid=${bid} w=${w} />
    </ol>
  <//>`;
}

registerModal("bringup", BringupDialog);

// The board's own button (Board > Versions' configuration SD card, sd.js): reopens the wizard.
export function BringupButton({ bid, compact = true }) {
  const w = boardState(bid).bringup;
  const started = !!(w && (w.check || w.written));
  return html`<button type="button" class=${`btn ${compact ? "sm" : ""} ${usbOnly(bid) ? "primary" : ""}`} data-action="bringup-open"
    title="Back up, write and reboot this board from this PC, then witness its harness" onClick=${() => openBringup(bid)}>
    <${Icon} name="rocket" /> ${started ? "Bring up… (continue)" : "Bring up…"}</button>`;
}

// For tests: act as if no scan ran yet (a new page).
export function resetScan() { U.scan = null; U.error = null; U.running = false; changed(); }

// keep a reference to the action runner so a test can drive a spec without a click
export const _runAction = runAction;
