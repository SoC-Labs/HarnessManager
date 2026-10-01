// Add a board (UI v2 round 3, plan M5, lane SHELL-2): ONE dialog, from the rail's + and from
// Settings, in place of three (the rail's inline address form, Settings > Boards' form, Settings
// > Hubs' "Add this board").
//
//   From a hub   the hubs this page knows (Settings > Hubs, and the hub of every listed board);
//                each target it serves with its lease (GET /hubs/{name}/leases, G3: one read per
//                hub, no board contact), and Add (POST /hubs/{name}/boards: a boards.toml table).
//   By address   an address (and a route: a hub, or an ssh host), Test (POST /probe: it reads the
//                harness and the shell, and lists nothing), then Add (the same probe, into the
//                rail). "Save to boards.toml" writes [boards.KEY] too (off unless ticked).
//   Over USB     (lane BRINGUP-USB, bringup.js) a new board plugged into this PC: Scan lists its
//                Debug USB (the MCC port, the V2M-MPS3 drive, what the MCC says, a harness on
//                Ethernet), and "Add and bring up" opens it USB-only and starts the bring-up.
//
// openModal("add", {mode: "hub" | "addr" | "usb"}). Nothing here takes a lease; only Over USB's
// "Add and bring up" opens a board (the one the user picked).

import { call } from "./api.js";
import { boardName, clock, hexId } from "./format.js";
import { html, useEffect, useRef, useState } from "./lib.js";
import { closeModal, ModalShell, openModal, registerModal } from "./modal.js";
import { changed, loadBoards, log, probe, S, select, timed, toast } from "./store.js";
import { Chip, Icon, Reason, Seg, Spinner } from "./ui.js";
import { epochOf } from "./week.js";
import { addBoard, addBoardFromHub, loadSettings, SS, testHub } from "./settings/state.js";
import { TestSteps } from "./settings/hubs.js";
import { configFor, routeText, viaOf } from "./sidebar.js";
import { OverUsb } from "./bringup.js";            // BRINGUP-USB: the third way

// --- the stylesheet ----------------------------------------------------------------------------
// css/shell2.css is this lane's. index.html links it once CCR SHELL2-1 lands (the integrator's
// file); until then, and never twice, this module links it.
(function linkSheet() {
  if (typeof document === "undefined" || document.querySelector('link[href$="css/shell2.css"]')) return;
  const l = document.createElement("link");
  l.rel = "stylesheet";
  l.href = "./css/shell2.css";
  document.head.appendChild(l);
}());

// --- the rail's address form, kept: what the rail's + renders (app.js), now the dialog ------------

// The rail's + (app.js: "Add a board by address") renders this in place of its old inline form:
// it opens the one dialog on By address, and hands the rail its button back.
export function AddDialogOpener({ onDone, mode = "addr" }) {
  useEffect(() => {
    openModal("add", { mode });
    if (onDone) onDone();
  }, []);
  return null;
}

export function openAdd(mode = "") { openModal("add", mode ? { mode } : {}); }

// --- hubs and their targets ------------------------------------------------------------------------

const A = { leases: {} };      // hub name -> {data, error, at, loading}

// The hubs this page knows: Settings > Hubs (their names), then the hub of every listed board
// (GET /boards `hub.name`: a hub a board pack names, not in Settings).
function knownHubs() {
  const out = [];
  const seen = new Set();
  for (const h of (SS.hubs && SS.hubs.hubs) || []) {
    if (h && h.name && !seen.has(h.name)) { seen.add(h.name); out.push({ name: h.name, host: h.host || h.url || "", settings: true }); }
  }
  for (const bid of S.order) {
    const hub = S.boards[bid] && S.boards[bid].hub;
    if (hub && hub.name && !seen.has(hub.name)) { seen.add(hub.name); out.push({ name: hub.name, host: hub.host || "", settings: false }); }
  }
  return out;
}

async function readHubLeases(name, refresh = false) {
  const slot = A.leases[name] || (A.leases[name] = { data: null, error: null, at: 0, loading: false });
  if (slot.loading) return;
  slot.loading = true;
  changed();
  const r = await timed(`hub leases ${name}${refresh ? " --refresh" : ""}`,
    () => call("hubLeases", { name }, undefined, refresh ? { refresh: "1" } : null));
  slot.loading = false;
  slot.at = Date.now() / 1000;
  if (r.error) slot.error = r.error;
  else { slot.data = r.data.data; slot.error = null; }
  changed();
}

function leaseWords(t) {
  if (!t) return "not read";
  if (t.state === "free") return t.waiting ? `Free · ${t.next || "someone"} is next` : "Free";
  const until = epochOf(t.expires_at);
  const who = t.here ? "Yours" : t.mine ? `Held by ${t.holder || "your hub name"} (another session)` : `Held by ${t.holder || "someone else"}`;
  const q = t.queue_length ? ` · ${t.queue_length} waiting` : "";
  return `${who}${until ? ` until ${clock(until).slice(0, 5)}` : ""}${q}`;
}

function FromHub({ m, set }) {
  const hubs = knownHubs();
  const name = m.hub && hubs.some((h) => h.name === m.hub) ? m.hub : (hubs[0] && hubs[0].name) || "";
  const hub = hubs.find((h) => h.name === name) || null;
  useEffect(() => { if (name && !A.leases[name]) readHubLeases(name); }, [name]);
  if (!hubs.length) {
    return html`<div class="stack" data-testid="add-hub-none">
      <${Reason} icon="server" text="No hub is set up yet: add one in Settings > Hubs (its host, and how to reach it), then add its boards here." />
      <div><button type="button" class="btn sm" data-action="add-hub-settings" onClick=${() => goSettings("hubs")}>
        <${Icon} name="plus" /> Add a hub…</button></div></div>`;
  }
  const slot = A.leases[name] || {};
  const test = SS.tests[`hubs:${name}`] || null;
  const d = slot.data;
  const rows = new Map();
  for (const t of (d && d.targets) || []) rows.set(t.target, { target: t.target, board: t.board || "", lease: t, boards: t.boards || [] });
  for (const t of (test && test.result && test.result.targets) || []) {
    if (!rows.has(t.target)) rows.set(t.target, { target: t.target, board: t.board || "", lease: null, boards: [] });
  }
  const readAt = d ? epochOf(d.read_at) : null;
  return html`<div class="stack" data-testid="add-from-hub">
    <div class="field"><label for="add-hub">Hub</label>
      <select id="add-hub" class="select grow" data-testid="add-hub-select" value=${name}
        onChange=${(e) => { if (e.target.value === "__add") { goSettings("hubs"); return; } set({ hub: e.target.value }); }}>
        ${hubs.map((h) => html`<option key=${h.name} value=${h.name}>${h.name}${h.host && h.host !== h.name ? ` (${h.host})` : ""}${h.settings ? "" : " · from a board pack"}</option>`)}
        <option value="__add">Add a hub…</option>
      </select>
      <button type="button" class="btn sm" data-action="add-hub-read" aria-busy=${slot.loading ? "true" : undefined}
        title="Read every target's lease again (one hub read)" onClick=${() => readHubLeases(name, true)}>
        ${slot.loading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Read</button>
      ${hub && hub.settings ? html`<button type="button" class="btn sm" data-action="add-hub-test"
        aria-busy=${test && test.running ? "true" : undefined} disabled=${!!(test && test.running)}
        title="Test connection: reach the hub, sign in, list its targets (no lease is taken)"
        onClick=${() => testHub(name)}><${Icon} name="plug-zap" /> Test</button>` : null}
    </div>
    ${slot.error ? html`<${Reason} level="err" testid="add-hub-error" text=${`${slot.error.errName}: ${slot.error.message}${slot.error.errName === "ABSENT" ? ": no board this service lists is behind it yet; Test lists its targets" : ""}`} />`
      : d ? html`<${Reason} level="ok" testid="add-hub-read"
        text=${`${d.host || name} (${d.transport || "hub"}) · ${(d.targets || []).length} target${(d.targets || []).length === 1 ? "" : "s"} · leases read ${readAt ? clock(readAt).slice(0, 5) : "now"}${d.cached ? " (kept from the last read)" : ""}`} />`
      : slot.loading ? html`<p class="muted small"><${Spinner} /> Reading the hub's leases…</p>` : null}
    ${test ? html`<${TestSteps} test=${test} transport=${(SS.rows[`hubs.${name}.transport`] || {}).value || ""} />` : null}
    ${rows.size ? html`<div class="table-wrap"><table class="table" data-testid="add-hub-targets"><thead><tr>
        <th>Target</th><th>Lease</th><th class="right"></th></tr></thead><tbody>
      ${[...rows.values()].map((r) => html`<tr key=${r.target} data-target=${r.target}>
        <td class="mono">${r.target}${r.board ? html`<div class="sub">${r.board}</div>` : null}</td>
        <td class="small">${leaseWords(r.lease)}</td>
        <td class="right"><${TargetAction} hub=${hub} row=${r} /></td></tr>`)}
      </tbody></table></div>` : null}
  </div>`;
}

function TargetAction({ hub, row }) {
  if (row.boards.length) {
    const bid = row.boards[0];
    return html`<button type="button" class="btn ghost sm" data-action="add-target-select"
      title=${`Already in the list: ${bid}`} onClick=${() => { closeModal(); select(bid); }}>
      in the list as ${boardName((S.boards[bid] || {}).candidate, bid)}</button>`;
  }
  if (!hub || !hub.settings) {
    return html`<span class="muted small" title="Add the hub in Settings > Hubs first: its boards are written under it">
      add the hub in Settings first</span>`;
  }
  const act = SS.hubAction[`target:${hub.name}:${row.target}`] || {};
  if (act.done) return html`<span class="small" data-testid="add-target-added">added as <code>boards.${act.done.board}</code></span>`;
  return html`<button type="button" class="btn sm primary" data-action="add-target" aria-busy=${act.busy ? "true" : undefined}
    disabled=${act.busy} onClick=${async () => {
      const done = await addBoardFromHub(hub.name, row.target);
      if (done) { await loadBoards(); readHubLeases(hub.name, true); toast(`Added ${row.target} from ${hub.name} (saved to boards.toml)`, { icon: "plus" }); }
    }}>${act.busy ? html`<${Spinner} />` : html`<${Icon} name="plus" />`} Add</button>
    ${act.error ? html`<div class="sub i-err">${act.error.errName}: ${act.error.message}</div>` : null}`;
}

function goSettings(section) {
  closeModal();
  import("./selfupdate.js").then((m) => m.openSettings(section));
}

// --- by address ----------------------------------------------------------------------------------------

function viaDefault(conf) {
  const via = (conf && conf.via) || "";
  return via === "hub" || via.startsWith("ssh:") ? via : "";
}

function hostPort(text) {
  let t = String(text || "").trim();
  const at = t.indexOf("@");
  if (at >= 0) t = t.slice(at + 1);
  const m = /^\[?([^\]]*?)\]?(?::(\d+))?$/.exec(t);
  return m ? { host: m[1].toLowerCase(), port: m[2] || "" } : { host: t.toLowerCase(), port: "" };
}

function keyOf(name, host) {
  return String(name || host || "").trim().replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
}

function testWords(c) {
  const id = c.identity || {};
  const bits = [id.harness_impl ? `${id.harness_impl} harness${id.harness_version ? ` ${id.harness_version}` : ""}` : "",
    id.shell_id ? `shell ${hexId(id.shell_id)}` : "",
    id.rm_name || ""].filter(Boolean);
  return `${boardName(c, c.board_id)}${bits.length ? `: ${bits.join(" · ")}` : ""}`;
}

function ByAddress({ m, set }) {
  const ref = useRef(null);
  useEffect(() => {
    // the dialog layer focuses its first [data-autofocus] (the head's close) after this runs
    const t = setTimeout(() => { if (ref.current) ref.current.focus(); }, 30);
    return () => clearTimeout(t);
  }, []);
  const match = configFor(m.host);
  const route = m.typed ? m.via : viaDefault(match && match.conf);
  const hp = hostPort(m.host);
  const target = hp.host ? String(m.host).trim() : "";
  const test = m.test || null;
  const runTest = async () => {
    if (!target) return;
    set({ test: { running: true } });
    const t0 = performance.now();
    const body = { hosts: [target], scan_usb: false, ...(viaOf(route) ? { via: viaOf(route) } : {}) };
    const r = await timed(`probe ${target}${route ? ` --via ${viaOf(route)}` : ""}`, () => call("probe", {}, body));
    const took = (performance.now() - t0) / 1000;
    // only the board at this address (a probe may also report what it found on the way)
    const at = (c) => [c.board_id, ...((c.links || []).map((l) => l.address))]
      .some((a) => hostPort(a).host === hp.host && (!hp.port || !hostPort(a).port || hostPort(a).port === hp.port));
    const cands = r.error ? [] : (r.data.data.candidates || []).filter(at);
    set({ test: { running: false, error: r.error, cands, took, for: `${target}|${route}` } });
  };
  const tested = test && !test.running && test.for === `${target}|${route}`;
  const submit = async (e) => {
    e.preventDefault();
    if (!target) return;
    const via = viaOf(route);
    closeModal();
    probe([target], via);
    if (m.save) {
      const key = keyOf(m.name, hp.host);
      const form = { key, match: target, name: (m.name || "").trim(), busy: false, error: null };
      await loadSettings();
      await addBoard(form);
      if (form.error) log("error", "settings", `boards.toml [boards.${key}] was not written: ${form.error.errName}: ${form.error.message}`);
      else toast(`Saved ${target} to boards.toml as [boards.${key}]`, { icon: "plus" });
    }
  };
  return html`<form class="rail-add stack" data-testid="add-by-address" onSubmit=${submit}>
    <div class="field"><label for="add-host">Address</label>
      <input id="add-host" class="input mono grow" placeholder="192.168.10.101[:6900]" aria-label="Board address"
        ref=${ref} value=${m.host} onInput=${(e) => set({ host: e.target.value })} /></div>
    <div class="field"><label for="add-via">Through</label>
      <input id="add-via" class="input mono grow via" placeholder="a hub: hub, or an ssh host (optional)"
        aria-label="Through a hub: hub, or an ssh host" data-testid="add-via" value=${route}
        onInput=${(e) => set({ via: e.target.value, typed: true })} /></div>
    ${match ? html`<div class="rail-add-note" data-testid="add-route" role="note">
      <${Icon} name="info" cls="sm" /><span>boards.toml <b>${match.conf.key}</b>: ${routeText(match.conf)}${match.conf.target ? ` (${match.conf.target})` : ""}${m.typed && route && route !== viaDefault(match.conf) ? "; the route typed here is used instead" : ""}</span></div>` : null}
    <div class="field"><label for="add-name">Name</label>
      <input id="add-name" class="input grow" placeholder="mps3-04 (optional)" value=${m.name}
        onInput=${(e) => set({ name: e.target.value })} /></div>
    <label class="check-inline"><input type="checkbox" checked=${!!m.save} data-testid="add-save"
      onChange=${(e) => set({ save: e.target.checked })} />Save to boards.toml${m.save && target ? html` as <code>[boards.${keyOf(m.name, hp.host) || "?"}]</code>` : ""}</label>
    <div class="row">
      <button type="button" class="btn sm" data-action="add-test" disabled=${!target || (test && test.running)}
        aria-busy=${test && test.running ? "true" : undefined} onClick=${runTest}>
        ${test && test.running ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Test</button>
      ${tested ? (test.error ? html`<${Reason} level="err" testid="add-test-result" text=${`${test.error.errName}: ${test.error.message}`} />`
        : test.cands.length ? html`<${Reason} level="ok" testid="add-test-result"
            text=${`Answered in ${test.took.toFixed(1)} s: ${test.cands.map(testWords).join("; ")}`} />`
          : html`<${Reason} level="warn" testid="add-test-result" text=${`Nothing answered at ${target}${route ? ` (${viaOf(route)})` : ""}. Check the address and the route.`} />`)
        : html`<span class="small muted">Test reads the harness and the shell; it adds nothing.</span>`}
    </div>
    <div class="modal-foot inline">
      <span class="small muted grow">${m.save ? "Saved to boards.toml" : "For this session: a scan finds it again"}</span>
      <button type="button" class="btn ghost" data-action="add-cancel" onClick=${closeModal}>Cancel</button>
      <button type="submit" class="btn primary" data-action="add-address" disabled=${!target}><${Icon} name="plus" /> Add</button>
    </div>
  </form>`;
}

// --- the dialog -------------------------------------------------------------------------------------

function AddDialog({ mode = "" }) {
  const [m, setM] = useState(() => ({ mode: mode || (knownHubs().length ? "hub" : "addr"), hub: "", host: "",
    via: "", typed: false, name: "", save: false, test: null }));
  const set = (patch) => setM((x) => ({ ...x, ...patch }));
  useEffect(() => { if (!SS.loaded || !SS.hubs) loadSettings(); }, []);
  return html`<${ModalShell} title="Add a board" icon="plus" cls="mid add-dialog" testid="add-board"
      foot=${m.mode === "hub" ? html`<span class="small muted grow">Added boards are written to boards.toml</span>
        <button type="button" class="btn" data-action="add-cancel" onClick=${closeModal}>Close</button>`
        : m.mode === "usb" ? html`<span class="small muted grow">Opened over its Debug USB only: no hub, no lease</span>
        <button type="button" class="btn" data-action="add-cancel" onClick=${closeModal}>Close</button>` : null}>
    <${Seg} label="How to add it" value=${m.mode} onChange=${(v) => set({ mode: v })}
      options=${[{ value: "hub", label: "From a hub", icon: "server" }, { value: "addr", label: "By address", icon: "ethernet-port" },
        { value: "usb", label: "Over USB (a new board plugged into this PC)", icon: "usb" }]} />
    ${m.mode === "hub" ? html`<${FromHub} m=${m} set=${set} />` : m.mode === "usb" ? html`<${OverUsb} />` : html`<${ByAddress} m=${m} set=${set} />`}
  <//>`;
}

registerModal("add", AddDialog);

// For tests and the devtools console.
window.__harness_managerAdd = () => JSON.parse(JSON.stringify({ leases: A.leases, hubs: knownHubs() }));
