// Update: check the signed channel, read the plan, approve it (a re-key needs the typed
// phrase), then follow the install job to its outcome, with the rollback at hand.
// Routes: POST /update/check, /boards/{bid}/update/harness|rollback, /update/app (lane L4).

import { panelState, runJob } from "../actions.js";
import { clock } from "../format.js";
import { html, useState } from "../lib.js";
import { changed } from "../store.js";
import { week } from "../week.js";
import { ActionRow, ArmBox, Card, Chip, Reason, ResultBlock } from "../ui.js";

function checkLines(rep) {
  if (!rep || typeof rep !== "object") return [{ kind: "out", text: "checked" }];
  const out = [{ kind: "ok", text: `channel ${rep.channel} #${rep.serial}: harness ${rep.harness_current || "?"}, app ${rep.app_current || "?"}` }];
  if (rep.plan) {
    const p = rep.plan;
    out.push({ kind: p.blockers && p.blockers.length ? "warnline" : "out",
      text: p.up_to_date ? "this board is up to date" : `plan: ${p.mode} to ${p.version}${p.rekey ? " (RE-KEY)" : ""}, ${(p.blockers || []).length} blocker(s)` });
  }
  return out;
}

function outcomeLines(o) {
  if (!o || typeof o !== "object") return [{ kind: "out", text: "done" }];
  const out = [{ kind: o.result === "installed" || o.result === "restored" ? "ok" : "warnline",
    text: `${o.result}: ${o.detail || ""}` }];
  if (o.restore_hint) out.push({ kind: "hint", text: o.restore_hint });
  return out;
}

function PlanSteps({ plan, phase }) {
  const idx = plan.steps.findIndex((s) => s.action === phase);
  return html`<ol class="plan-steps" data-testid="update-steps">${plan.steps.map((s, i) => html`
    <li key=${i} class=${idx < 0 ? "" : i < idx ? "done" : i === idx ? "active" : ""}>
      <span class="mono">${s.action}</span><span class="secondary">${s.detail}</span></li>`)}</ol>`;
}

function PlanCard({ bid, rep }) {
  const w = week(bid);
  const plan = rep.plan;
  const [typed, setTyped] = useState("");
  const p = panelState(bid, "update");
  const last = w.updateEvents.filter((e) => e.topic === "update.progress").slice(-1)[0];
  const phase = p.running === "update_harness" && last ? last.data.phase : "";
  const install = {
    key: "update_harness", label: `Install harness ${plan.version}`, busyLabel: "Installing...",
    budgetS: 900, command: `update harness ${plan.version}${plan.rekey ? " --rekey" : ""}`,
    run: (ctx) => runJob("updateHarness", { bid }, { fingerprint: plan.fingerprint,
      ...(plan.rekey ? { rekey_phrase: typed.trim() } : {}) },
    (d) => ctx.progress(`${d.phase}${d.total > 1 ? `: ${Math.floor((d.done * 100) / d.total)}%` : ""}`, d.phase),
    "update_harness"),
    render: outcomeLines,
    onDone: (ok, o) => {
      if (ok && o) {
        w.outcome = o;
        if (o.result === "installed") plan.applied = true;     // the plan is spent: check again for the next
        changed();
      }
      setTyped("");
    },
  };
  const rollback = {
    key: "update_rollback", label: "Roll back", busyLabel: "Rolling back...", budgetS: 900,
    command: "update rollback",
    run: (ctx) => runJob("updateRollback", { bid }, undefined, (d) => ctx.progress(d.phase, d.phase), "update_rollback"),
    render: outcomeLines,
  };
  const guard = () => {
    if (plan.up_to_date) return "the board already runs this release";
    if ((plan.blockers || []).length) return `blocked: ${plan.blockers[0]}`;
    if (plan.rekey && typed.trim() !== plan.consent_phrase) return `a re-key: type exactly ${plan.consent_phrase}`;
    return "";
  };
  const running = plan.running || {};
  return html`<${Card} title=${`Harness ${running.harness || "?"} to ${plan.version || "?"}`} icon="rocket" testid="update-plan"
      sub=${`The ${plan.channel} channel, release #${plan.serial}. The plan below was computed for this board; approving binds to it.`}>
    <div class="actions">
      <div class="row">
        ${plan.up_to_date ? html`<${Chip} level="ok" icon="circle-check">up to date<//>` : html`<${Chip} icon="layers">${plan.mode}<//>`}
        ${plan.rekey ? html`<${Chip} level="warn" icon="triangle-alert" testid="rekey-chip">re-key: shell ${running.shell_id || "?"} to a new static id<//>` : null}
        <span class="secondary small">running shell <span class="mono">${running.shell_id || "?"}</span></span>
      </div>
      ${(plan.blockers || []).map((t) => html`<${Reason} level="err" text=${t} key=${t} testid="update-blocker" />`)}
      ${(plan.warnings || []).map((t) => html`<${Reason} level="warn" text=${t} key=${t} />`)}
      ${(plan.unusable || []).length ? html`<${Reason} level="warn" icon="circle-slash" text=${`Becomes unusable: ${plan.unusable.join("; ")}`} />` : null}
      ${plan.steps && plan.steps.length ? html`<${PlanSteps} plan=${plan} phase=${phase} />` : null}
      ${plan.applied ? html`<${Reason} level="ok" testid="plan-applied"
        text=${`Installed. This plan is spent; check again to see what the channel offers this board now.`} />` : null}
      ${plan.up_to_date || plan.applied ? null : html`
        ${plan.rekey ? html`<div class="field rekey"><label for=${`rk-${bid}`}>Consent</label>
          <input class="input mono grow" id=${`rk-${bid}`} placeholder=${`type ${plan.consent_phrase}`} autocomplete="off"
            value=${typed} onInput=${(e) => setTyped(e.target.value)} data-testid="rekey-phrase" /></div>` : null}
        <${ArmBox} bid=${bid} armKey="update" testid="arm-update"
          text="Arm: I understand this writes the board's SD and reboots it; the backup it takes restores it." />
        <${ActionRow} bid=${bid} panel="update" spec=${install} variant="primary" icon="upload"
          gate=${{ arm: "update", guard }} />`}
      <${ResultBlock} lines=${p.lines} panel=${p} testid="update-result" />
      ${w.outcome && w.outcome.restore_hint ? html`<${Reason} text=${`If it misbehaves: ${w.outcome.restore_hint}`} testid="rollback-hint" />` : null}
      <div class="mt-8">
        <${ArmBox} bid=${bid} armKey="update_rollback" compact=${true}
          text="Arm: I understand this restores the backup the last install took, and reboots the board." />
        <${ActionRow} bid=${bid} panel="update" spec=${rollback} icon="undo-2" quietArm=${true}
          gate=${{ arm: "update_rollback" }} />
      </div>
    </div>
  <//>`;
}

// The app's own update is not about this board, but it runs from this page: its panel lives
// with the board's, and it waits for the board's jobs like every other action.
function AppCard({ bid, rep }) {
  const p = panelState(bid, "update_app");
  const spec = {
    key: "update_app", label: `Update the app to ${rep.app_update}`, busyLabel: "Updating...", budgetS: 600,
    command: `update app ${rep.app_update}`,
    run: (ctx) => runJob("updateApp", {}, { version: rep.app_update }, (d) => ctx.progress(d.phase, d.phase), "update_app"),
    render: (r) => [{ kind: "ok", text: `harness-manager ${r && r.version} staged${r && r.switched ? " and switched" : ""}` },
      ...(r && r.restart ? [{ kind: "hint", text: r.restart }] : [])],
  };
  return html`<${Card} title="The app" icon="download" testid="update-app"
      sub=${`You run harness-manager ${rep.app_running || "?"}; the channel's current release is ${rep.app_current || "?"}.`}>
    ${rep.app_update ? html`<div class="actions">
      <${ActionRow} bid=${bid} panel="update_app" spec=${spec} icon="download" gate=${{}} />
      <${ResultBlock} lines=${p.lines} panel=${p} />
    </div>` : html`<${Reason} level="ok" text="The app is current." />`}
  <//>`;
}

export function UpdateSection({ bid }) {
  const w = week(bid);
  const p = panelState(bid, "update_check");
  const check = {
    key: "update_check", label: "Check for updates", busyLabel: "Checking...", budgetS: 120,
    command: "update check",
    run: (ctx) => runJob("updateCheck", {}, { board_id: bid }, (d) => ctx.progress(d.phase, d.phase), "update_check"),
    render: checkLines,
    onDone: (ok, rep) => { if (ok) { w.update = rep; w.checkedAt = Date.now() / 1000; changed(); } },
  };
  const rep = w.update;
  return html`<div class="stack">
    <${Card} title="Updates" icon="refresh-cw" testid="update-card"
        sub="Releases come from a signed channel (minisign, pinned keys). Checking reads only: nothing is installed until you approve a plan.">
      <div class="actions">
        <${ActionRow} bid=${bid} panel="update_check" spec=${check} variant="primary" icon="scan-search" gate=${{}} />
        <${ResultBlock} lines=${p.lines} panel=${p} testid="update-check-result" />
        ${rep ? html`<p class="muted small">checked ${clock(w.checkedAt)} · signed by <span class="mono">${String(rep.signed_by || "?").slice(0, 16)}...</span> (${rep.key_role || "?"})
          ${(rep.warnings || []).length ? html` · ${rep.warnings.join("; ")}` : null}</p>` : null}
      </div>
    <//>
    ${rep && rep.plan ? html`<${PlanCard} bid=${bid} rep=${rep} />` : null}
    ${rep ? html`<${AppCard} bid=${bid} rep=${rep} />` : null}
  </div>`;
}

