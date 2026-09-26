// The Hubs section (lane SET-UI; docs/design/SETTINGS.md §6): your hubs and the machine's, Add a
// hub (SSH or REST), Test connection with its steps and hints, the targets it offers with
// "Add this board", "Make this a hub" for an inline hub table, and Remove. Nothing here takes,
// joins or releases a lease: Test connection is three reads over REST, one ssh round trip.

import { clock } from "../format.js";
import { html } from "../lib.js";
import { changed } from "../store.js";
import { Chip, Icon, Reason, Spinner } from "../ui.js";
import { RowGroup, SecretField } from "./rows.js";
import {
  addBoardFromHub, addHub, adoptInline, joinKey, removeHub, splitKey, SS, testHub,
} from "./state.js";

export const STEPS = [
  { step: "config", label: "Config" },
  { step: "reach", label: "Reach" },
  { step: "auth", label: "Auth" },
  { step: "group", label: "Group" },
  { step: "targets", label: "Targets" },
  { step: "target", label: "Target" },
];
const SSH_FIELDS = ["transport", "host", "group", "jump", "holder", "lease_ttl", "request_ttl", "queue_timeout"];
const REST_FIELDS = ["transport", "url", "ca_file", "lease_ttl", "cert_file", "key_file", "insecure", "events",
  "direct", "timeout_s", "request_ttl", "queue_timeout", "host"];
const ADVANCED = new Set(["jump", "holder", "request_ttl", "queue_timeout", "cert_file", "key_file", "insecure",
  "events", "direct", "timeout_s", "host"]);
const LABELS = { transport: "Transport", host: "Host", group: "Group", jump: "Jump host", holder: "Lease holder",
  lease_ttl: "Lease time", request_ttl: "Request lease time", queue_timeout: "Queue wait", url: "URL",
  ca_file: "CA file", cert_file: "Client certificate", key_file: "Client key", insecure: "Skip TLS checks",
  events: "Event stream", direct: "Data plane", timeout_s: "Call timeout", token: "Token" };

function errText(e) {
  return e ? `${e.errName}: ${e.message}${e.hint ? ` (${e.hint})` : ""}` : "";
}

// --- Test connection: the steps ------------------------------------------------------------------

export function TestSteps({ test, transport }) {
  if (!test) return null;
  if (test.error) return html`<${Reason} level="err" testid="test-error" text=${errText(test.error)} />`;
  const res = test.result;
  const ran = res ? res.steps || [] : [];
  const byStep = Object.fromEntries(ran.map((s) => [s.step, s]));
  let steps = STEPS;
  if (res) {
    const failedAt = ran.findIndex((s) => !s.ok);
    steps = failedAt >= 0 || !res.passed ? STEPS.filter((s) => byStep[s.step] || (transport !== "rest" || s.step !== "group"))
      : STEPS.filter((s) => byStep[s.step]);
  } else if (transport === "rest") {
    steps = STEPS.filter((s) => s.step !== "group");
  }
  const runningIdx = test.running ? Math.max(0, STEPS.findIndex((s) => s.step === (test.phase || "config"))) : -1;
  const failed = ran.find((s) => !s.ok);
  return html`<div class="test-box" data-testid="test-result" data-passed=${res ? (res.passed ? "true" : "false") : "running"}>
    <ol class="test-steps" data-testid="test-steps">${steps.map((s) => {
      const got = byStep[s.step];
      const idx = STEPS.indexOf(s);
      const state = got ? (got.ok ? "ok" : "fail") : test.running && idx === runningIdx ? "running"
        : test.running && idx < runningIdx ? "ok" : "pending";
      const icon = state === "ok" ? "circle-check" : state === "fail" ? "circle-x" : state === "running" ? "" : "circle-dashed";
      return html`<li key=${s.step} data-step=${s.step} data-state=${state} class=${state}
          title=${got ? got.detail : state === "pending" ? "not run" : ""}>
        ${state === "running" ? html`<${Spinner} />` : html`<${Icon} name=${icon} cls="sm" />`}${s.label}</li>`;
    })}</ol>
    ${failed ? html`<div class="test-fail" data-testid="test-failure" data-step=${failed.step}>
        <div><strong>${failed.step}:</strong> ${failed.detail}</div>
        ${failed.hint ? html`<div class="hint" data-testid="step-hint">${failed.hint}</div>` : null}
        ${failed.step === "auth" && transport === "rest" ? html`<div class="hint">Here: Set or Replace the token above, then test again.</div>` : null}</div>`
      : res && res.passed ? html`<div class="test-ok small" data-testid="test-passed">${ran.map((s) => html`<div key=${s.step}>
          <span class="step-name">${s.step}</span> ${s.detail}</div>`)}</div>` : null}
    ${test.running ? html`<div class="secondary small"><${Spinner} /> Testing${test.phase ? `: ${test.phase}` : ""}… (reads only: no lease is taken)</div>` : null}
  </div>`;
}

function TestChip({ test }) {
  if (!test || test.running) return test && test.running ? html`<${Chip} level="accent" testid="hub-test-chip"><${Spinner} />testing<//>` : null;
  if (test.error) return html`<${Chip} level="err" icon="circle-x" testid="hub-test-chip">error<//>`;
  const r = test.result || {};
  if (r.passed) return html`<${Chip} level="ok" icon="circle-check" testid="hub-test-chip" title=${`passed at ${clock(test.at)}`}>${clock(test.at)}<//>`;
  return html`<${Chip} level="err" icon="circle-x" testid="hub-test-chip">${r.failed || "failed"}<//>`;
}

// --- the targets a hub offers, with "Add this board" -----------------------------------------------

function Targets({ hub, test }) {
  const res = test && test.result;
  if (!res || !res.passed || !(res.targets || []).length) return null;
  const used = {};
  for (const [board, t] of Object.entries(hub.targets_used || {})) (used[t] = used[t] || []).push(board);
  const boardRows = new Set((SS.listing && SS.listing.instances.boards) || []);
  return html`<div class="hub-targets" data-testid="hub-targets">
    <div class="field-label mb-6">What ${hub.name} offers</div>
    <table class="table compact"><thead><tr><th>Target</th><th>Board</th><th>Role</th><th></th></tr></thead>
      <tbody>${res.targets.map((t) => {
        const slot = `target:${hub.name}:${t.target}`;
        const act = SS.hubAction[slot] || {};
        const by = used[t.target] || [];
        const taken = boardRows.has(t.target) && !by.length;
        return html`<tr key=${t.target} data-target=${t.target}>
          <td class="mono">${t.target}</td><td class="mono">${t.board}</td><td>${t.role || ""}</td>
          <td class="right">${by.length ? html`<span class="muted small" data-testid="target-used">used by ${by.join(", ")}</span>`
            : act.done ? html`<span class="small" data-testid="target-added">added as <code>boards.${act.done.board}</code></span>`
            : taken ? html`<span class="muted small">boards.${t.target} exists</span>`
            : html`<button type="button" class="btn sm" data-action="hub-add-board" aria-busy=${act.busy ? "true" : undefined}
                disabled=${act.busy} onClick=${() => addBoardFromHub(hub.name, t.target)}>
                ${act.busy ? html`<${Spinner} />` : html`<${Icon} name="plus" />`} Add this board</button>`}
            ${act.error ? html`<${Reason} level="err" text=${errText(act.error)} />` : null}
            ${act.done && (act.done.notes || []).length ? act.done.notes.map((n) => html`<div class="sub" key=${n}>${n}</div>`) : null}</td>
        </tr>`;
      })}</tbody></table>
  </div>`;
}

// --- one hub ------------------------------------------------------------------------------------

function hubRows(name) {
  const out = {};
  for (const key of SS.order) {
    const p = splitKey(key);
    if (p[0] === "hubs" && p[1] === name && p.length === 3) out[p[2]] = SS.rows[key];
  }
  return out;
}

export function HubCard({ hub, policyPath }) {
  const rows = hubRows(hub.name);
  const rest = hub.transport === "rest";
  const fields = rest ? REST_FIELDS : SSH_FIELDS;
  // The rows a hub of this transport uses; the rarely changed ones behind "more" unless set.
  const list = fields.filter((f) => rows[f]).map((f) => {
    const r = rows[f];
    const tucked = ADVANCED.has(f) && !(r.source === "user" && r.value !== "" && r.value !== null);
    return tucked && !r.advanced ? { ...r, advanced: true } : r;
  });
  const labels = Object.fromEntries(list.map((r) => [r.key, LABELS[splitKey(r.key)[2]] || ""]));
  const test = SS.tests[`hubs:${hub.name}`];
  const act = SS.hubAction[`hub:${hub.name}`] || {};
  const token = rows.token;
  const where = rest ? hub.url : hub.host;
  const confirm = SS.hubAction[`confirm:${hub.name}`];
  return html`<section class="card hub-card" data-testid="hub-card" data-hub=${hub.name} data-transport=${hub.transport}
      data-machine=${hub.machine ? "true" : "false"} aria-label=${`Hub ${hub.name}`}>
    <div class="card-head">
      <h3 class="card-title"><${Icon} name="server" />${hub.name}</h3>
      <${Chip} cls="mono">${rest ? "REST" : "SSH"}<//>
      <span class="mono secondary small ellipsis" title=${where}>${where || "not set"}</span>
      ${hub.machine ? html`<${Chip} level="held" icon="lock" testid="hub-machine" title=${`set by your administrator in ${hub.policy}`}>admin<//>` : null}
      <span class="spacer"></span>
      <${TestChip} test=${test} />
      ${!hub.machine ? html`<button type="button" class="btn ghost sm" data-action="hub-remove"
        onClick=${() => { SS.hubAction[`confirm:${hub.name}`] = true; changed(); }}><${Icon} name="trash-2" /> Remove</button>` : null}
    </div>
    <div class="card-body stack gap-12">
      ${hub.machine ? html`<div class="policy-note" data-testid="hub-policy-note"><${Icon} name="shield-check" />
        <div><strong>${hub.name}</strong> is ${"set by your administrator"} in <code>${hub.policy}</code>: its settings are locked.
          ${rest ? " The token is yours to set." : " You log in with your own SSH key."}</div></div>` : null}
      ${confirm ? html`<div class="banner warn inline-banner" data-testid="hub-remove-confirm"><${Icon} name="triangle-alert" />
          <div class="grow">Remove the hub <b>${hub.name}</b>${rest && token && token.secret && token.secret.set ? " and its stored token" : ""}?
            ${hub.boards.length ? html` <b>${hub.boards.join(", ")}</b> use${hub.boards.length === 1 ? "s" : ""} it and will not open until pointed elsewhere.` : ""}</div>
          <button type="button" class="btn sm" onClick=${() => { delete SS.hubAction[`confirm:${hub.name}`]; changed(); }}>Keep it</button>
          <button type="button" class="btn sm danger" data-action="hub-remove-confirm" disabled=${act.busy}
            onClick=${async () => { await removeHub(hub.name, hub.boards.length > 0); delete SS.hubAction[`confirm:${hub.name}`]; changed(); }}>
            ${act.busy ? html`<${Spinner} />` : null} Remove</button></div>` : null}
      ${act.error ? html`<${Reason} level="err" testid="hub-action-error" text=${errText(act.error)} />` : null}
      ${(hub.problems || []).map((p) => html`<${Reason} key=${p} level="warn" testid="hub-problem" text=${p} />`)}
      <div class="sgroup">
        ${rest && token ? html`<${SecretField} row=${token} label="Token" policyPath=${policyPath} />` : null}
      </div>
      <${RowGroup} id=${`hub:${hub.name}`} rows=${list} policyPath=${policyPath} labels=${labels} quietLock=${hub.machine} />
      <div class="hub-boards small" data-testid="hub-boards">
        <span class="field-label">Boards</span>${" "}
        ${hub.boards.length ? hub.boards.map((b, i) => html`${i ? ", " : ""}<span key=${b} class="mono">${b}</span>
          <span class="muted"> (${(hub.targets_used || {})[b] || "?"})</span>`)
          : html`<span class="muted">No board uses this hub yet: Test connection, then Add this board.</span>`}
      </div>
      <div class="row">
        <button type="button" class="btn" data-action="hub-test" aria-busy=${test && test.running ? "true" : undefined}
          disabled=${!!(test && test.running)} onClick=${() => testHub(hub.name)}>
          ${test && test.running ? html`<${Spinner} />` : html`<${Icon} name="plug-zap" />`} Test connection</button>
        <span class="muted small">${rest ? "Three reads: /health, /whoami, /groups." : "One ssh round trip."} No lease is taken.</span>
      </div>
      <${TestSteps} test=${test} transport=${hub.transport} />
      <${Targets} hub=${hub} test=${test} />
    </div>
  </section>`;
}

// --- Add a hub ------------------------------------------------------------------------------------

function Field({ label, field, form, placeholder = "", mono = true, hint = "" }) {
  return html`<label class="form-field"><span class="field-label">${label}</span>
    <input class=${`input ${mono ? "mono" : ""}`} data-field=${field} value=${form[field]} placeholder=${placeholder}
      spellcheck="false" autocomplete="off" onInput=${(e) => { form[field] = e.target.value; changed(); }} />
    ${hint ? html`<span class="muted small">${hint}</span>` : null}</label>`;
}

function suggestName(form) {
  const src = form.transport === "rest" ? (form.url.replace(/^https?:\/\//, "").split(/[:/]/)[0] || "") : form.host;
  const label = src.split(".")[0].replace(/[^A-Za-z0-9_-]/g, "-").toLowerCase();
  return label || "";
}

function formTable(form) {
  return form.transport === "rest"
    ? { transport: "rest", url: form.url.trim() }
    : { transport: "ssh", host: form.host.trim(), group: form.group.trim(), ...(form.jump.trim() ? { jump: form.jump.trim() } : {}) };
}

export function AddHubForm() {
  const form = SS.hubForm;
  const rest = form.transport === "rest";
  const name = form.name.trim() || suggestName(form);      // empty: the host's first label
  const ready = /^[A-Za-z0-9_-]{1,64}$/.test(name) && (rest ? form.url.trim() : form.host.trim());
  const test = SS.tests["hubs:"];
  return html`<section class="card hub-form" data-testid="hub-add-form" aria-label="Add a hub">
    <div class="card-head"><h3 class="card-title"><${Icon} name="plus" />Add a hub</h3></div>
    <div class="card-body stack gap-12">
      <div class="seg" role="group" aria-label="Transport">
        ${[["ssh", "SSH: a lab account"], ["rest", "REST: a token"]].map(([v, l]) => html`<button type="button" key=${v}
          data-value=${v} aria-pressed=${form.transport === v ? "true" : "false"}
          onClick=${() => { form.transport = v; changed(); }}>${l}</button>`)}</div>
      <div class="form-grid">
        ${rest ? html`<${Field} label="URL" field="url" form=${form} placeholder="the hub's https address, port 7246"
            hint="The hub's web port (7246) takes a token." />`
          : html`<${Field} label="Host" field="host" form=${form} placeholder="mapstone-dev.ecs.soton.ac.uk"
              hint='"local" when Harness Manager runs on the hub.' />
            <${Field} label="Group" field="group" form=${form} placeholder="fpga" hint="The fpgahub socket's group (sg)." />
            <${Field} label="Jump host" field="jump" form=${form} placeholder="(none)" hint="Optional: ssh -J." />`}
        <${Field} label="Name" field="name" form=${form} placeholder=${suggestName(form) || "lab"}
          hint="Boards refer to the hub by this name." />
      </div>
      ${rest ? html`<p class="secondary small">After adding it, set its token (an fpgahub admin makes one with <code>fpgahub token create</code>).</p>` : null}
      ${form.error ? html`<${Reason} level="err" testid="hub-add-error" text=${errText(form.error)} />` : null}
      <div class="row">
        <button type="button" class="btn primary" data-action="hub-add-save" disabled=${!ready || form.busy}
          onClick=${() => addHub({ ...form, name })}>${form.busy ? html`<${Spinner} />` : null} Add hub</button>
        <button type="button" class="btn" data-action="hub-add-test" disabled=${!ready || (test && test.running)}
          onClick=${() => testHub(name, formTable(form))}>Test before adding</button>
        <button type="button" class="btn ghost" data-action="hub-add-cancel"
          onClick=${() => { SS.hubForm = null; delete SS.tests["hubs:"]; changed(); }}>Cancel</button>
      </div>
      <${TestSteps} test=${test} transport=${form.transport} />
    </div>
  </section>`;
}

function openHubForm() {
  SS.hubForm = { name: "", transport: "ssh", host: "", url: "", group: "fpga", jump: "", busy: false, error: null };
  changed();
}

// --- inline hub tables: "Make this a hub" -----------------------------------------------------------

function InlineHub({ item }) {
  const act = SS.hubAction[`adopt:${item.board}`] || {};
  return html`<div class="inline-hub" data-testid="inline-hub" data-board=${item.board}>
    <${Icon} name="info" />
    <div class="grow">
      <div><code>boards.${item.board}</code> has its hub written inline (<span class="mono">${item.host || item.url}</span>).
        Make it a named hub, <b>${item.name}</b>, so other boards can use it and it can be tested here.</div>
      ${act.done ? html`<div class="small" data-testid="adopt-done">Done: <code>boards.${act.done.board}</code> now uses the hub${" "}
        <b>${act.done.hub}</b>. The old file is kept as <code>${act.done.backup}</code>.</div>` : null}
      ${act.error ? html`<${Reason} level="err" text=${errText(act.error)} />` : null}
    </div>
    <button type="button" class="btn sm" data-action="hub-adopt" disabled=${act.busy}
      onClick=${() => adoptInline(item.board)}>${act.busy ? html`<${Spinner} />` : null} Make this a hub</button>
  </div>`;
}

// --- the section ------------------------------------------------------------------------------------

export function HubsSection({ policyPath }) {
  if (SS.hubsUnavailable) {
    return html`<${Reason} level="unk" icon="circle-slash" testid="hubs-unavailable" text=${`Hubs cannot be managed here: ${SS.hubsUnavailable}.`} />`;
  }
  const data = SS.hubs || { hubs: [], inline: [] };
  const hubs = data.hubs || [];
  return html`<div class="stack gap-12" data-testid="hubs-section">
    <div class="section-intro">
      <p class="secondary">A hub is an fpgahub server that leases lab boards. Boards name their hub in${" "}
        <code>boards.toml</code> (<code>hub = { use = "NAME", target = … }</code>).</p>
      ${!SS.hubForm ? html`<button type="button" class="btn sm" data-action="hub-add-open" onClick=${openHubForm}>
        <${Icon} name="plus" /> Add a hub</button>` : null}
    </div>
    ${SS.hubForm ? html`<${AddHubForm} />` : null}
    ${!hubs.length && !SS.hubForm ? html`<div class="empty-note" data-testid="hubs-empty"><${Icon} name="server" />
      <div><strong>No hub.</strong> Harness Manager talks to boards on your desk or your network directly.
        Add a hub when your boards live in a lab.</div></div>` : null}
    ${hubs.map((h) => html`<${HubCard} key=${h.name} hub=${h} policyPath=${policyPath} />`)}
    ${(data.inline || []).map((it) => html`<${InlineHub} key=${it.board} item=${it} />`)}
  </div>`;
}

export function hubKey(name, field) { return joinKey(["hubs", name, field]); }
