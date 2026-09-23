// Reset & Power: SD recovery first (when an install was interrupted), then DUT reset,
// shell restart, board reboot, and the configuration SD backup. Every one is armed.

import { panelState, runJob } from "../actions.js";
import { call } from "../api.js";
import { capState, journalText } from "../format.js";
import { html } from "../lib.js";
import { boardState, changed, loadPending, scheduleRefresh } from "../store.js";
import { ActionRow, ArmBox, Card, Reason, ResultBlock } from "../ui.js";

// The engine picks the reboot wait by harness implementation (bare-metal ~120 s, Linux
// 180 s: constants.reboot_wait_s), so the UI sends none and budgets for the longer one.
const REBOOT_BUDGET_S = 210;

function SdRecoveryCard({ bid }) {
  const b = boardState(bid);
  const j = b.pending;
  const backup = (j.backup && j.backup.path) || "";
  const p = panelState(bid, "sd_restore");
  const spec = {
    key: "sd_restore", label: "Restore the SD", busyLabel: "Restoring...", budgetS: 900,
    command: `sd restore ${backup || "?"}`,
    run: (ctx) => runJob("sdRestore", { bid }, { backup_path: backup },
      (d) => ctx.progress(`${d.phase || "restore"}: ${d.total ? Math.floor((d.done * 100) / d.total) : 0}%`, d.phase),
      "sd_restore"),
    render: () => [{ kind: "ok", text: `restored the SD from ${backup}` }],
    onDone: () => loadPending(bid),
  };
  return html`<${Card} title="Interrupted SD install: restore it first" icon="hard-drive" cls="danger"
      testid="sd-recovery">
    <div class="actions">
      <dl class="kv">
        <dt>Journal</dt><dd data-testid="sd-journal">${journalText(j)}</dd>
        <dt>Backup</dt><dd class="mono">${backup || html`<span class="muted">the journal names no backup</span>`}</dd>
      </dl>
      <${Reason} level="err" text="An SD install stopped part-way. Restore puts back the backup taken before it. Nothing else on this board should be trusted until then." />
      <${ArmBox} bid=${bid} armKey="sd_restore" testid="arm-sd"
        text="Arm: I understand this rewrites the configuration SD from the backup taken before the install." />
      <${ActionRow} bid=${bid} panel="sd_restore" spec=${spec} variant="primary" icon="undo-2"
        gate=${{ capability: "storage_install", adapter: "storage", arm: "sd_restore",
          guard: () => (backup ? "" : "the journal names no backup; restore by hand with harness-manager sd restore") }} />
      <${ResultBlock} lines=${p.lines} panel=${p} testid="sd-result" />
    </div>
  <//>`;
}

function resetTargets(bid) {
  const s = boardState(bid).session;
  return s && Array.isArray(s.reset_targets) ? s.reset_targets : null;   // null: not read
}

function DutResetCard({ bid }) {
  const p = panelState(bid, "reset_dut");
  const b = boardState(bid);
  // "shell" has its own card, gated on its own capability.
  const targets = (resetTargets(bid) || ["dut"]).filter((t) => t !== "shell");
  const target = targets.includes(b.resetTarget) ? b.resetTarget : targets[0] || "dut";
  const spec = {
    key: "reset_dut", label: "Reset DUT", busyLabel: "Resetting...", budgetS: 20,
    command: `reset ${target}`,
    run: async () => { await call("reset", { bid }, { target }); return `reset ${target}: done`; },
    render: (t) => [{ kind: "ok", text: t }],
  };
  return html`<${Card} title="DUT reset" icon="rotate-ccw" testid="reset-dut"
      sub="Pulses the DUT reset through the harness. The consoles stay up.">
    <div class="actions">
      ${targets.length > 1 ? html`<div class="field"><label for=${`rt-${bid}`}>Target</label>
        <select class="select" id=${`rt-${bid}`} value=${target}
          onChange=${(e) => { b.resetTarget = e.target.value; changed(); }}>
          ${targets.map((t) => html`<option key=${t} value=${t}>${t}</option>`)}</select></div>` : null}
      <${ArmBox} bid=${bid} armKey="reset_dut" text="Arm: I understand this resets the DUT CPU (the harness keeps running)." />
      <${ActionRow} bid=${bid} panel="reset_dut" spec=${spec} icon="rotate-ccw"
        gate=${{ capability: "reset_dut", adapter: "resets", arm: "reset_dut" }} />
      <${ResultBlock} lines=${p.lines} panel=${p} />
    </div>
  <//>`;
}

function ShellRestartCard({ bid }) {
  const p = panelState(bid, "reset_shell");
  const spec = {
    key: "reset_shell", label: "Restart shell", busyLabel: "Restarting...", budgetS: 60, command: "reset shell",
    run: async () => { await call("reset", { bid }, { target: "shell" }); return "reset shell: done"; },
    render: (t) => [{ kind: "ok", text: t }],
    onDone: () => scheduleRefresh(bid, 500),
  };
  return html`<${Card} title="Restart the shell" icon="refresh-cw" testid="reset-shell"
      sub="Restarts the harness firmware without reloading the FPGA.">
    <div class="actions">
      <${ArmBox} bid=${bid} armKey="reset_shell" text="Arm: I understand every console and the debug session drop while the shell restarts." />
      <${ActionRow} bid=${bid} panel="reset_shell" spec=${spec} icon="refresh-cw"
        gate=${{ capability: "reset_shell", adapter: "resets", arm: "reset_shell",
          guard: () => {
            const t = resetTargets(bid);
            return t && !t.includes("shell") ? "Cannot: this board's reset adapter offers no 'shell' target" : "";
          } }} />
      <${ResultBlock} lines=${p.lines} panel=${p} />
    </div>
  <//>`;
}

function RebootCard({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "reboot");
  const spec = {
    key: "reboot", label: "Reboot board", busyLabel: "Rebooting...", budgetS: REBOOT_BUDGET_S,
    command: "mcc reboot",
    run: (ctx) => runJob("reboot", { bid }, {},
      (d) => ctx.progress(`reboot: ${d.phase} (${d.done}/${d.total})`, d.phase), "reboot"),
    render: rebootLines,
    onDone: () => scheduleRefresh(bid, 200),
  };
  const phases = ["sent", "down", "up"];
  return html`<${Card} title="Board reboot" icon="power" testid="reboot-card"
      sub="Reboots through the board controller and proves it: the board went down, then came back. The board then runs what is on its SD card.">
    <div class="actions">
      <${ArmBox} bid=${bid} armKey="reboot" testid="arm-reboot"
        text="Arm: I understand this restarts the whole board; every session, console and loaded overlay is lost." />
      <${ActionRow} bid=${bid} panel="reboot" spec=${spec} variant="danger" icon="power"
        gate=${{ capability: "reboot_board", adapter: "controller", arm: "reboot" }} />
      ${b.reboot.phases.length ? html`<div class="steps">${phases.map((ph) => html`<div key=${ph}
        class=${`step ${b.reboot.phases.includes(ph) ? "done" : ""}`}><div class="bar"></div><span>${ph}</span></div>`)}</div>` : null}
      <${ResultBlock} lines=${p.lines} panel=${p} testid="reboot-result" />
    </div>
  <//>`;
}

// The reboot job's result is the controller's evidence: {summary, down_after_s, up_after_s,
// down_evidence[], up_evidence, shell_id_before, shell_id_after, fpga_configured}.
export function rebootLines(ev) {
  if (!ev || typeof ev !== "object") return [{ kind: "ok", text: "the board went down and came back" }];
  const out = [{ kind: "ok", text: ev.summary || "the board went down and came back" }];
  const t = (s) => (Number.isFinite(Number(s)) ? `${Number(s).toFixed(1)} s` : "?");
  if (ev.down_after_s !== undefined || ev.up_after_s !== undefined) {
    out.push({ kind: "out", text: `down after ${t(ev.down_after_s)}, back after ${t(ev.up_after_s)}` });
  }
  if (ev.shell_id_before || ev.shell_id_after) {
    const same = ev.shell_id_before === ev.shell_id_after;
    out.push({ kind: same ? "out" : "warnline",
      text: `shell ${ev.shell_id_before || "?"} -> ${ev.shell_id_after || "?"}${same ? " (unchanged)" : " (CHANGED)"}` });
  }
  if (ev.fpga_configured !== undefined) {
    out.push({ kind: ev.fpga_configured ? "out" : "warnline",
      text: `FPGA configured: ${ev.fpga_configured ? "yes" : "NO"}` });
  }
  for (const line of [].concat(ev.down_evidence || [])) out.push({ kind: "hint", text: `down: ${line}` });
  if (ev.up_evidence) out.push({ kind: "hint", text: `up: ${typeof ev.up_evidence === "object" ? JSON.stringify(ev.up_evidence) : ev.up_evidence}` });
  return out;
}

// A cold power cycle through a networked outlet (T9). harness-manager-daemon has no endpoint for it
// yet, so this says what the board can do and why not, and offers no button.
function PowerCycleCard({ bid }) {
  const st = capState(boardState(bid).info, "power_cycle");
  return html`<${Card} title="Cold power cycle" icon="plug-zap" testid="power-cycle"
      sub="Switches the board's supply off and on through a networked outlet listed in boards.toml.">
    ${!st ? html`<${Reason} text="waiting for the board's capability view" />`
      : !st.available ? html`<${Reason} icon="circle-slash" text=${`Cannot: ${st.reason}`} testid="power-cycle-reason" />`
      : html`<${Reason} text="This board's outlet can cycle it. harness-manager-daemon has no power-cycle endpoint yet, so it is not offered here: use the command line." testid="power-cycle-reason" />`}
  <//>`;
}

function SdBackupCard({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "sd_backup");
  const spec = {
    key: "sd_backup", label: "Back up the SD", busyLabel: "Backing up...", budgetS: 600,
    command: "sd backup",
    run: (ctx) => runJob("sdBackup", { bid }, {},
      (d) => ctx.progress(`${d.phase || "backup"}: ${d.total ? Math.floor((d.done * 100) / d.total) : 0}%`, d.phase),
      "sd_backup"),
    render: (rec) => (rec && typeof rec === "object"
      ? [{ kind: "ok", text: `backup ${rec.path}` },
        { kind: "out", text: `${rec.files} files from ${rec.volume_label}, sha256 ${String(rec.sha256 || "").slice(0, 16)}...` }]
      : [{ kind: "out", text: "done" }]),
    onDone: (ok, rec) => { if (ok) b.backup = rec; },
  };
  return html`<${Card} title="Configuration SD" icon="hard-drive" testid="sd-backup"
      sub="A backup is a zip with a sha256 manifest. Installing onto the SD always takes one first.">
    <div class="actions">
      <${ActionRow} bid=${bid} panel="sd_backup" spec=${spec} icon="download"
        gate=${{ capability: "storage_backup", adapter: "storage" }} />
      <${ResultBlock} lines=${p.lines} panel=${p} />
      <${Reason} text="Installing a harness onto the SD is a CLI step in this build: harness-manager sd install." />
    </div>
  <//>`;
}

export function PowerSection({ bid }) {
  const b = boardState(bid);
  return html`<div class="stack">
    ${b.pending ? html`<${SdRecoveryCard} bid=${bid} />` : null}
    <div class="grid two">
      <${DutResetCard} bid=${bid} />
      <${ShellRestartCard} bid=${bid} />
    </div>
    <div class="grid two">
      <${RebootCard} bid=${bid} />
      <${SdBackupCard} bid=${bid} />
    </div>
    <div class="grid two">
      <${PowerCycleCard} bid=${bid} />
    </div>
  </div>`;
}

