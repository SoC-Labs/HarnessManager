// Card contents (lane L3 PERSIST BACKUP): the board's /persist (claim keys, identity, sensor
// calibration, kept state) saved to this PC, and put back. Re-making the card wipes it, so the
// re-make action asks canRemake(bid) and stays disabled until a recent backup of this board
// exists. The HM lead mounts PersistCard in Board > Versions and gates the re-make with canRemake.
//
// Routes (daemon/persist_api.py; named in api.js ENDPOINTS by the HM lead): persistBackups
// (GET), persistBackup (POST), persistRestore (POST {confirm, backup_id}).

import { panelState, runAction, runJob, gateReason, interlock } from "../actions.js";
import { call, ENDPOINTS } from "../api.js";
import { bytesText } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed } from "../store.js";
import { ArmBox, Card, Icon, Reason, ResultBlock, Spinner } from "../ui.js";

const present = () => !!(ENDPOINTS && ENDPOINTS.persistBackups);

function stateOf(bid) {
  const b = boardState(bid);
  if (!b.persist) b.persist = { backups: null, recent: false, recentDays: 7, loading: false, error: "", picked: "" };
  return b.persist;
}

export async function loadBackups(bid) {
  const st = stateOf(bid);
  if (!present() || st.loading) return;
  st.loading = true;
  changed();
  try {
    const { data } = await call("persistBackups", { bid });
    st.backups = data.backups || [];
    st.recent = !!data.recent;
    st.recentDays = data.recent_days || 7;
    st.error = "";
    if (!st.backups.some((m) => m.id === st.picked)) st.picked = st.backups.length ? st.backups[0].id : "";
  } catch (e) {
    st.error = (e && e.message) || "could not read the backups";
  } finally {
    st.loading = false;
    changed();
  }
}

// May the card be re-made now? { ok, reason }: ok is false, with a plain reason, until a recent
// backup of THIS board is on this PC. The re-make action disables itself on !ok and shows the reason.
export function canRemake(bid) {
  if (!present()) return { ok: false, reason: "Back up the card contents first: this build cannot save them yet." };
  const st = stateOf(bid);
  if (st.backups === null) return { ok: false, reason: st.error ? `Back up the card contents first (${st.error}).` : "Checking for a backup of this board's card contents..." };
  if (!st.backups.length) return { ok: false, reason: "Back up the card contents first: making the card again wipes its keys and settings." };
  if (!st.recent) return { ok: false, reason: `The last backup is more than ${st.recentDays} days old: back up again before making the card.` };
  return { ok: true, reason: "" };
}

const when = (m) => (m.created_utc || "").replace("T", " ").replace("Z", " UTC");

function backupSpec(bid) {
  const st = stateOf(bid);
  return {
    key: "persist_backup", label: "Back up to this PC", busyLabel: "Backing up...", budgetS: 900,
    command: "card backup",
    run: (ctx) => runJob("persistBackup", { bid }, {}, (d) => {
      ctx.progress(d.done ? `backing up: ${bytesText(d.done)}` : "backing up", d.phase);
    }, "persist_backup"),
    render: (r) => [{ kind: "ok", text: (r && r.note) || "saved" }],
    onDone: (ok) => { if (ok) { st.backups = null; loadBackups(bid); } },
  };
}

function restoreSpec(bid, id) {
  const st = stateOf(bid);
  return {
    key: "persist_restore", label: "Restore", busyLabel: "Restoring...", budgetS: 900,
    command: `card restore --backup ${id}`,
    run: (ctx) => runJob("persistRestore", { bid }, { confirm: true, backup_id: id }, (d) => {
      ctx.progress(d.phase === "restore" && d.total ? `restoring: ${Math.floor((d.done * 100) / d.total)}%` : (d.phase || "restoring"), d.phase);
    }, "persist_restore"),
    render: (r) => {
      const diff = (r && r.differences) || {};
      const lines = [{ kind: r && r.verified ? "ok" : "warnline", text: (r && r.note) || "done" }];
      for (const p of (diff.missing || []).slice(0, 5)) lines.push({ kind: "out", text: `not on the board: ${p}` });
      for (const p of (diff.changed || []).slice(0, 5)) lines.push({ kind: "out", text: `different size: ${p}` });
      return lines;
    },
    onDone: () => { st.backups = null; loadBackups(bid); },
  };
}

function Drive({ bid, panel, spec, gate, icon, variant = "", armText, armTestid }) {
  const p = panelState(bid, panel);
  const why = gateReason(bid, panel, spec.key, gate);
  const running = p.running === spec.key;
  const blocked = !!why && !running;
  return html`<div class="stack gap-8">
    <div class="row">
      ${gate.arm ? html`<${ArmBox} bid=${bid} armKey=${gate.arm} compact=${true} testid=${armTestid} text=${armText} />` : null}
      <button type="button" class=${`btn sm ${blocked ? "" : variant}`} data-action=${spec.key}
        aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined}
        title=${blocked ? why : spec.label}
        onClick=${() => { if (running) return; if (why) { interlock(bid, panel, spec.command, why); return; } runAction(bid, panel, { ...spec, arm: gate.arm }); }}>
        ${running ? html`<${Spinner} /> ${p.busyLabel}` : html`${icon ? html`<${Icon} name=${icon} />` : null}${spec.label}`}</button></div>
    ${why && !running && !why.startsWith("not armed") ? html`<${Reason} text=${why} testid=${`reason-${spec.key}`} />` : null}
    ${p.lines && p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid=${`${spec.key}-result`} />` : null}
  </div>`;
}

// The "Card contents" card. Mount it in Board > Versions for a Linux board.
export function PersistCard({ bid }) {
  const st = stateOf(bid);
  useEffect(() => { if (st.backups === null && !st.loading) loadBackups(bid); }, [bid]);
  if (!present()) return null;
  const list = st.backups || [];
  const gate = canRemake(bid);
  const picked = list.find((m) => m.id === st.picked) || list[0];
  return html`<${Card} title="Card contents" icon="hard-drive" testid="persist-card"
      sub="The board's keys, name and settings kept on its microSD. Making the card again wipes them, so save them to this PC first.">
    <${Drive} bid=${bid} panel="persist_backup" spec=${backupSpec(bid)} icon="download"
      variant=${list.length && st.recent ? "" : "primary"} gate=${{ holder: "Back up the card contents" }} />
    ${st.error ? html`<${Reason} level="warn" text=${st.error} />` : null}
    <div class="stack gap-8 mt-8" data-testid="persist-list">
      ${st.backups === null ? html`<span class="small muted"><${Spinner} /> reading</span>`
        : !list.length ? html`<span class="small muted">No backup of this board on this PC yet.</span>`
        : list.map((m) => html`<label class="row small" key=${m.id} data-testid=${`persist-${m.id}`}>
            <input type="radio" name=${`persist-${bid}`} checked=${picked && picked.id === m.id}
              onChange=${() => { st.picked = m.id; changed(); }} />
            <span>${when(m)}</span><span class="muted">${bytesText(m.size)} · ${m.file_count} file${m.file_count === 1 ? "" : "s"}</span></label>`)}
    </div>
    ${picked ? html`<div class="mt-8"><${Drive} bid=${bid} panel="persist_restore" spec=${restoreSpec(bid, picked.id)} icon="upload"
      gate=${{ arm: "persist_restore", holder: "Restore the card contents" }} armTestid="arm-persist-restore"
      armText=${`Arm: this writes the ${when(picked)} backup into the board's card contents, overwriting files with the same names.`} /></div>` : null}
    ${gate.ok ? html`<${Reason} level="ok" text="A recent backup is on this PC: making the card is allowed." testid="persist-remake-ok" />`
      : html`<${Reason} level="warn" text=${gate.reason} testid="persist-remake-blocked" />`}
  <//>`;
}

// The Versions section calls this to mount the card.
export function mountPersist(bid) { return html`<${PersistCard} bid=${bid} />`; }
