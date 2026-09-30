// Update: the board's Harness versions (H9, sections/harness.js: every signed release with a
// verdict), then T7's check of the channel's current release: read the plan, approve it (a
// re-key needs the typed phrase), follow the install job to its outcome, with the rollback at
// hand. Routes: POST /update/check, /boards/{bid}/update/harness|rollback (lane L4).

import { panelState, runJob } from "../actions.js";
import { clock } from "../format.js";
import { html, useState } from "../lib.js";
import { changed } from "../store.js";
import { week } from "../week.js";
import { ActionRow, ArmBox, Card, Chip, Icon, Reason, ResultBlock } from "../ui.js";
import { offer, openSettings, U } from "../selfupdate.js";            // UPDATE-UI
import { HarnessVersionsCard } from "./harness.js";                    // UPDATE-UI (H9)

function checkLines(rep) {
  if (!rep || typeof rep !== "object") return [{ kind: "out", text: "checked" }];
  const out = [{ kind: "ok", text: `channel ${rep.channel} #${rep.serial}: harness ${rep.harness_current || "?"}, app ${rep.app_current || "none published"}` }];
  if (rep.plan) {
    const p = rep.plan;
    out.push({ kind: p.blockers && p.blockers.length ? "warnline" : "out",
      text: p.up_to_date ? "this board is up to date" : `plan: ${p.mode} to ${p.version}${p.rekey ? " (RE-KEY)" : ""}, ${(p.blockers || []).length} blocker(s)` });
  }
  return out;
}

// An outcome (update.done): the result, what the board said, and the checks that are not
// plainly ok. UNCHECKED stays UNCHECKED: it never reads as a pass.
function outcomeLines(o) {
  if (!o || typeof o !== "object") return [{ kind: "out", text: "done" }];
  const good = o.result === "installed" || o.result === "restored";
  const out = [{ kind: good ? "ok" : "warnline", text: `${o.result}: ${o.detail || ""}` }];
  const checks = o.checks || [];
  if (checks.length) {
    const count = (c) => checks.filter((x) => x.check === c).length;
    const parts = [`${count("ok")} ok`];
    for (const c of ["unchecked", "failed", "mismatch"]) if (count(c)) parts.push(`${count(c)} ${c}`);
    const other = checks.length - checks.filter((x) => ["ok", "unchecked", "failed", "mismatch"].includes(x.check)).length;
    if (other) parts.push(`${other} other`);
    out.push({ kind: "out", text: `checks: ${parts.join(", ")}` });
    for (const c of checks.filter((x) => x.check !== "ok")) {
      out.push({ kind: c.check === "unchecked" ? "hint" : "warnline", text: `${c.name}: ${c.check.toUpperCase()} (${c.detail})` });
    }
  }
  if (o.evidence && o.evidence.summary) out.push({ kind: "out", text: o.evidence.summary });
  if (o.restore_hint) out.push({ kind: "hint", text: o.restore_hint });
  return out;
}

// update.progress phases ("download:base-sd", "backup:backup", "sd:install", "reboot:down")
// -> the plan step they belong to.
const PHASE_STEP = { download: "download", verify: "verify", "store-overlays": "store-overlays",
  backup: "backup-sd", "backup-sd": "backup-sd", sd: "install-sd", "install-sd": "install-sd",
  restore: "install-sd", reboot: "reboot", confirm: "confirm-identity", "confirm-identity": "confirm-identity" };

export function stepOf(phase) {
  const head = String(phase || "").split(":")[0];
  return PHASE_STEP[phase] || PHASE_STEP[head] || head;
}

function PlanSteps({ plan, phase }) {
  const idx = plan.steps.findIndex((s) => s.action === stepOf(phase));
  return html`<ol class="plan-steps" data-testid="update-steps">${plan.steps.map((s, i) => html`
    <li key=${i} class=${idx < 0 ? "" : i < idx ? "done" : i === idx ? "active" : ""}>
      <span class="mono">${s.action}</span><span class="secondary">${s.detail}</span></li>`)}</ol>`;
}

// What to do if the installed harness misbehaves: the daemon's hint, else the backup it took.
function rollbackHint(o) {
  if (!o) return "";
  if (o.restore_hint) return o.restore_hint;
  const path = o.backup && o.backup.path;
  return path ? `Roll back (below) restores the backup ${path} and reboots the board.` : "";
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
    (d) => ctx.progress(`${d.phase}${d.total > 1 ? `: ${Math.floor((d.done * 100) / d.total)}%` : ""}`, stepOf(d.phase)),
    "update_harness"),
    render: outcomeLines,
    renderError: (e) => {
      const d = e.data || {};
      if (d.outcome) return outcomeLines(d.outcome);
      if (d.plan && d.plan.fingerprint !== plan.fingerprint) {
        return [{ kind: "hint", text: "the card above now shows the new plan: read it, then approve again" }];
      }
      return [];
    },
    onDone: (ok, v) => {
      const d = (!ok && v && v.data) || {};
      if (ok && v) {
        w.outcome = v;
        if (v.result === "installed") plan.applied = true;     // the plan is spent: check again for the next
      } else if (d.outcome) {
        w.outcome = d.outcome;
      } else if (d.plan && w.update && d.plan.fingerprint !== plan.fingerprint) {
        // The board or the channel changed since the check: this is the plan that would run.
        w.update = { ...w.update, plan: d.plan };
        w.planChanged = true;
      }
      setTyped("");
      changed();
    },
  };
  const rollback = {
    key: "update_rollback", label: "Roll back", busyLabel: "Rolling back...", budgetS: 900,
    command: "update rollback",
    run: (ctx) => runJob("updateRollback", { bid }, {}, (d) => ctx.progress(d.phase, stepOf(d.phase)), "update_rollback"),
    render: outcomeLines,
    renderError: (e) => (e.data && e.data.outcome ? outcomeLines(e.data.outcome) : []),
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
      ${w.planChanged ? html`<${Reason} level="warn" testid="plan-changed"
        text="The plan changed since you checked (the board or the channel moved). This is the new plan: read it, then approve again. Nothing was installed." />` : null}
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
      ${rollbackHint(w.outcome) ? html`<${Reason} text=${`If it misbehaves: ${rollbackHint(w.outcome)}`} testid="rollback-hint" />` : null}
      <div class="mt-8">
        <${ActionRow} bid=${bid} panel="update" spec=${rollback} icon="undo-2" compact=${true}
            gate=${{ arm: "update_rollback" }}>
          <${ArmBox} bid=${bid} armKey="update_rollback" compact=${true} testid="arm-rollback"
            text="Arm: I understand this restores the backup the last install took, and reboots the board." />
        <//>
      </div>
    </div>
  <//>`;
}

// The app's own update is not about this board (docs/design/HM_SELF_UPDATE.md §9: "move it
// out of the per-board Update page"). It lives in Settings and the banner at the top of every
// page (UPDATE-UI, selfupdate.js); this card only points there.
function AppCard() {
  const st = U.status;
  const o = offer();
  const text = U.unavailable ? `App updates are unavailable here: ${U.unavailable}.`
    : !st ? "Reading the app's update state..."
    : st.dev_install ? `You run Harness Manager ${st.running}, a developer install: updates are off (${st.dev_install}).`
    : o && o.kind === "staged" ? `You run Harness Manager ${st.running}; ${o.version} is ready: restart to update (the banner at the top).`
    : o ? `You run Harness Manager ${st.running}; ${o.version} is available (the banner at the top).`
    : `You run Harness Manager ${st.running}; no newer version is offered.`;
  return html`<${Card} title="The app" icon="download" testid="update-app"
      sub="Harness Manager's own update is not about this board: it lives in Settings, and a banner at the top of every page offers it.">
    <div class="row">
      <span data-testid="update-app-text">${text}</span>
      <button type="button" class="btn sm" data-action="open-settings" onClick=${() => openSettings("updates")}>
        <${Icon} name="sliders-horizontal" /> Settings</button>
    </div>
  <//>`;
}

function ReleasesTable({ rep }) {
  const rows = (rep.releases && rep.releases.harness) || [];
  if (!rows.length) return null;
  return html`<table class="table" data-testid="releases"><thead><tr>
      <th>Harness</th><th>Status</th><th>Static id</th><th>Impl</th><th></th></tr></thead>
    <tbody>${rows.map((r) => html`<tr key=${r.version} data-release=${r.version}>
      <td class="mono">${r.version}</td><td>${r.current ? html`<b>${r.status}</b>` : r.status}</td>
      <td class="mono">${r.static_id}</td><td>${r.impl}</td>
      <td>${r.rekey ? html`<${Chip} level="warn">re-key<//>` : null}</td></tr>`)}</tbody></table>`;
}

export function UpdateSection({ bid }) {
  const w = week(bid);
  const p = panelState(bid, "update_check");
  const check = {
    key: "update_check", label: "Check for updates", busyLabel: "Checking...", budgetS: 120,
    command: "update check",
    run: (ctx) => runJob("updateCheck", {}, { board_id: bid }, (d) => ctx.progress(d.phase, d.phase), "update_check"),
    render: checkLines,
    onDone: (ok, rep) => {
      if (ok) {
        w.update = rep; w.checkedAt = Date.now() / 1000; w.planChanged = false; w.unavailable = "";
      } else if (rep && rep.errName === "UNAVAILABLE") {
        w.unavailable = rep.reason || rep.message;      // DemoEngine: "this engine has no update service"
      }
      changed();
    },
  };
  const rep = w.update;
  const signer = rep ? String(rep.signed_by || "?") : "";
  return html`<div class="stack">
    <${HarnessVersionsCard} bid=${bid} />
    <${Card} title="The channel's current release" icon="refresh-cw" testid="update-card"
        sub="Releases come from a signed channel (minisign, pinned keys). Checking reads only: nothing is installed until you approve a plan.">
      <div class="actions">
        <${ActionRow} bid=${bid} panel="update_check" spec=${check} variant="primary" icon="scan-search" gate=${{}} />
        <${ResultBlock} lines=${p.lines} panel=${p} testid="update-check-result" />
        ${w.unavailable ? html`<${Reason} icon="circle-slash" testid="update-unavailable"
          text=${`Updates are unavailable here: ${w.unavailable}.`} />` : null}
        ${rep ? html`<p class="muted small">checked ${clock(w.checkedAt)} · signed by <span class="mono">${signer.length > 16 ? `${signer.slice(0, 16)}...` : signer}</span> (${rep.key_role || "?"})
          ${(rep.warnings || []).length ? html` · ${rep.warnings.join("; ")}` : null}</p>` : null}
        ${rep ? html`<${ReleasesTable} rep=${rep} />` : null}
      </div>
    <//>
    ${rep && rep.plan ? html`<${PlanCard} bid=${bid} rep=${rep} />` : null}
    <${AppCard} />
  </div>`;
}

