// SD card: the configuration SD's recovery (first, when an install was interrupted), then
// the install flow: 1 back up, 2 install files, 3 reboot and witness it, 4 restore if needed.
// The storage routes are the existing ones; every write is armed and needs a backup first.

import { panelState, runJob } from "../actions.js";
import { capState, journalText } from "../format.js";
import { html, useState } from "../lib.js";
import { boardState, changed, loadPending } from "../store.js";
import { week } from "../week.js";
import { ARM_TEXT, REBOOT_GATE, rebootSpec } from "./power.js";
import { ActionRow, ArmBox, Card, Icon, Reason, ResultBlock } from "../ui.js";

const pct = (d) => (d.total ? Math.floor((d.done * 100) / d.total) : 0);

function SdRecoveryCard({ bid }) {
  const b = boardState(bid);
  const j = b.pending;
  const backup = (j.backup && j.backup.path) || "";
  const p = panelState(bid, "sd_restore");
  const spec = {
    key: "sd_restore", label: "Restore the SD", busyLabel: "Restoring...", budgetS: 900,
    command: `sd restore ${backup || "?"}`,
    run: (ctx) => runJob("sdRestore", { bid }, { backup_path: backup },
      (d) => ctx.progress(`${d.phase || "restore"}: ${pct(d)}%`, d.phase), "sd_restore"),
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
        gate=${{ capability: "storage_install", adapter: "storage", arm: "sd_restore", holder: "Restore the SD",
          guard: () => (backup ? "" : "the journal names no backup; restore by hand with harness-manager sd TARGET restore ZIP") }} />
      <${ResultBlock} lines=${p.lines} panel=${p} testid="sd-result" />
    </div>
  <//>`;
}

// Back up the whole configuration SD (a zip with a sha256 manifest); this page keeps the
// record, so an install and the card's "Last backup" name it.
export function backupSpec(bid) {
  const b = boardState(bid);
  const w = week(bid);
  return {
    key: "sd_backup", label: "Back up the SD", busyLabel: "Backing up...", budgetS: 600,
    command: "sd backup",
    run: (ctx) => runJob("sdBackup", { bid }, {}, (d) => ctx.progress(`${d.phase || "backup"}: ${pct(d)}%`, d.phase),
      "sd_backup"),
    render: (rec) => (rec && typeof rec === "object"
      ? [{ kind: "ok", text: `backup ${rec.path}` },
        { kind: "out", text: `${rec.files} files from ${rec.volume_label}, sha256 ${String(rec.sha256 || "").slice(0, 16)}...` }]
      : [{ kind: "out", text: "done" }]),
    onDone: (ok, rec) => { if (ok && rec) { w.lastBackup = rec; b.backup = rec; changed(); } },
  };
}

function Step({ n, title, done = false, children, testid = "" }) {
  return html`<li class=${`flow-step ${done ? "done" : ""}`} data-testid=${testid || undefined}>
    <div class="flow-n">${done ? html`<${Icon} name="check" cls="sm" />` : n}</div>
    <div class="flow-body"><h3 class="flow-title">${title}</h3>${children}</div>
  </li>`;
}

function InstallFlow({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  const [files, setFiles] = useState([{ dest: "", src: "" }]);
  const [backupPath, setBackupPath] = useState("");
  const pb = panelState(bid, "sd_backup");
  const pi = panelState(bid, "sd_install");
  const pr = panelState(bid, "reboot");
  const px = panelState(bid, "sd_restore_manual");
  const backup = backupPath || (w.lastBackup && w.lastBackup.path) || "";
  const given = files.filter((f) => f.dest.trim() && f.src.trim());
  const backup0 = backupSpec(bid);
  const installSpec = {
    key: "sd_install", label: "Install onto the SD", busyLabel: "Installing...", budgetS: 900,
    command: `sd install ${given.map((f) => f.dest.trim()).join(" ") || "?"}`,
    run: (ctx) => runJob("sdInstall", { bid }, {
      files: Object.fromEntries(given.map((f) => [f.dest.trim(), f.src.trim()])), backup_path: backup,
    }, (d) => ctx.progress(`${d.phase || "install"}: ${pct(d)}%`, d.phase), "sd_install"),
    render: (res) => [{ kind: "ok", text: `installed ${given.length} file(s); the board runs them after a reboot` },
      ...(res && res.backup ? [{ kind: "hint", text: `backup ${res.backup.path || backup}` }] : [])],
    onDone: () => loadPending(bid),
  };
  const restoreSpec = {
    key: "sd_restore_manual", label: "Restore this backup", busyLabel: "Restoring...", budgetS: 900,
    command: `sd restore ${backup || "?"}`,
    run: (ctx) => runJob("sdRestore", { bid }, { backup_path: backup },
      (d) => ctx.progress(`${d.phase || "restore"}: ${pct(d)}%`, d.phase), "sd_restore"),
    render: () => [{ kind: "ok", text: `restored the SD from ${backup}` }],
    onDone: () => loadPending(bid),
  };
  // Over a hub there is no USB_MSD link: both storage capabilities say why, once, here.
  const bk = capState(boardState(bid).info, "storage_backup");
  const inst = capState(boardState(bid).info, "storage_install");
  const sdOut = bk && inst && !bk.available && !inst.available ? bk.reason : "";
  const installGuard = () => {
    if (!given.length) return "add at least one file: its path on the SD and the local file to write";
    if (given.some((f) => f.dest.trim().toLowerCase().endsWith(".ebf"))) {
      return "an .ebf (board-controller firmware) is never written to the SD";
    }
    if (!backup) return "take a backup first (step 1), or give the path of one";
    return "";
  };
  const setRow = (i, key, value) => { const next = files.slice(); next[i] = { ...next[i], [key]: value }; setFiles(next); };
  return html`<${Card} title="Install onto the configuration SD" icon="hard-drive" testid="sd-flow"
      sub="The board loads its base (bitstream, firmware, config) from this SD at power-on. Paths are on the machine running harness-manager-daemon.">
    ${sdOut ? html`<${Reason} icon="circle-slash" testid="sd-unavailable"
      text=${`The configuration SD is out of reach from here: ${sdOut}`} />` : null}
    <ol class="flow">
      <${Step} n="1" title="Back up the SD" done=${!!w.lastBackup} testid="sd-step-backup">
        <p class="secondary small">A zip with a sha256 manifest of the whole card. An install needs one.</p>
        <${ActionRow} bid=${bid} panel="sd_backup" spec=${backup0} icon="download"
          gate=${{ capability: "storage_backup", adapter: "storage", holder: "Back up the SD" }} />
        <${ResultBlock} lines=${pb.lines} panel=${pb} testid="sd-backup-result" />
      <//>
      <${Step} n="2" title="Install files" testid="sd-step-install">
        <div class="file-rows">
          ${files.map((f, i) => html`<div class="file-row" key=${i}>
            <input class="input mono" placeholder="SD path, e.g. MB/HBI0309C/AN536/images.txt" aria-label=${`SD path ${i + 1}`}
              value=${f.dest} onInput=${(e) => setRow(i, "dest", e.target.value)} data-testid=${`sd-dest-${i}`} />
            <${Icon} name="arrow-right-left" cls="sm i-muted" />
            <input class="input mono" placeholder="local file, e.g. /home/me/build/images.txt" aria-label=${`Local file ${i + 1}`}
              value=${f.src} onInput=${(e) => setRow(i, "src", e.target.value)} data-testid=${`sd-src-${i}`} />
            <button type="button" class="btn ghost sm icon-only" aria-label="Remove this file"
              onClick=${() => setFiles(files.length > 1 ? files.filter((_, j) => j !== i) : [{ dest: "", src: "" }])}>
              <${Icon} name="x" /></button>
          </div>`)}
          <button type="button" class="btn ghost sm" onClick=${() => setFiles([...files, { dest: "", src: "" }])}>
            <${Icon} name="plus" /> Add a file</button>
        </div>
        <div class="field mt-8"><label for=${`bk-${bid}`}>Backup</label>
          <input class="input mono grow" id=${`bk-${bid}`} placeholder="the backup to restore if it goes wrong"
            value=${backup} onInput=${(e) => setBackupPath(e.target.value)} data-testid="sd-backup-path" /></div>
        <${ArmBox} bid=${bid} armKey="sd_install" testid="arm-sd-install"
          text="Arm: I understand this writes the configuration SD (journaled; the backup restores it)." />
        <${ActionRow} bid=${bid} panel="sd_install" spec=${installSpec} variant="primary" icon="upload"
          gate=${{ capability: "storage_install", adapter: "storage", arm: "sd_install", guard: installGuard, holder: "Install onto the SD" }} />
        <${ResultBlock} lines=${pi.lines} panel=${pi} testid="sd-install-result" />
      <//>
      <${Step} n="3" title="Reboot and witness it" testid="sd-step-reboot">
        <p class="secondary small">The new base runs only after the board reloads from the SD.</p>
        <${ArmBox} bid=${bid} armKey="reboot" text=${ARM_TEXT.reboot} />
        <${ActionRow} bid=${bid} panel="reboot" spec=${rebootSpec(bid)} variant="danger" icon="power" gate=${REBOOT_GATE} />
        <${ResultBlock} lines=${pr.lines} panel=${pr} testid="sd-reboot-result" />
      <//>
      <${Step} n="4" title="Restore the backup, if it went wrong" testid="sd-step-restore">
        <${ArmBox} bid=${bid} armKey="sd_restore_manual"
          text="Arm: I understand this rewrites the configuration SD from the backup." />
        <${ActionRow} bid=${bid} panel="sd_restore_manual" spec=${restoreSpec} icon="undo-2"
          gate=${{ capability: "storage_install", adapter: "storage", arm: "sd_restore_manual", holder: "Restore the SD",
            guard: () => (backup ? "" : "no backup yet: take one in step 1, or give its path in step 2") }} />
        <${ResultBlock} lines=${px.lines} panel=${px} testid="sd-restore-result" />
      <//>
    </ol>
  <//>`;
}

// Board › Versions (UI v2, lane UI2-BOARD): the interrupted-install recovery comes first.
export function SdRecovery({ bid }) {
  return boardState(bid).pending ? html`<${SdRecoveryCard} bid=${bid} />` : null;
}

// Board › Versions' left card on a bare-metal board: the configuration SD (the route it is
// reached by, what it holds, this page's last backup, Back up now), the Roll back at its
// foot, and the by-hand install flow in a fold.
export function ConfigSdCard({ bid, route, routeWhy = "", foot = null, after = null, note = null }) {
  const b = boardState(bid);
  const w = week(bid);
  const id = (b.info && b.info.identity) || {};
  const pb = panelState(bid, "sd_backup");
  const bk = capState(b.info, "storage_backup");
  const out = bk && !bk.available ? bk.reason : "";
  const last = w.lastBackup;
  return html`<${Card} title="Configuration SD" icon="hard-drive" cls="os-here" testid="config-sd"
      sub="The MCC's SD card. A harness install writes it (after a backup), then reboots.">
    <dl class="kv">
      <dt>Route</dt><dd class="small" title=${routeWhy}>${route}</dd>
      <dt>Holds</dt><dd class="small">harness ${id.harness_version || "?"} · shell <span class="mono">${id.shell_id || "?"}</span></dd>
      <dt>Last backup</dt><dd class="small" data-testid="config-sd-backup">${last
        ? html`<span class="mono">${last.path}</span>${last.files ? ` · ${last.files} files` : ""}`
        : html`<span class="muted">none taken from this page</span>`}</dd>
    </dl>
    ${out ? html`<div class="mt-8"><${Reason} icon="circle-slash" testid="config-sd-reason" text=${`Configuration SD tools: ${out}.`} /></div>` : null}
    <div class="mt-8"><${ActionRow} bid=${bid} panel="sd_backup" spec=${backupSpec(bid)} icon="download" compact=${true}
      gate=${{ capability: "storage_backup", adapter: "storage", holder: "Back up the SD" }} /></div>
    ${pb.lines && pb.lines.length ? html`<${ResultBlock} lines=${pb.lines} panel=${pb} testid="config-sd-backup-result" />` : null}
    ${foot ? html`<div class="bt-foot">${foot}</div>` : null}
    ${after}
    <details class="os-more" data-testid="sd-more"><summary data-action="sd-more">Install files onto the SD by hand…</summary>
      <${InstallFlow} bid=${bid} /></details>
    ${note}
  <//>`;
}
