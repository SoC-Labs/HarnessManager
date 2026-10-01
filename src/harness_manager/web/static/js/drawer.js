// The Activity drawer (UI v2; 0.1.0's Activity tab): every event from the daemon and this
// page's own action results, newest first, over the page from the rail's foot.
//
// store.js openActivity(bid, level) opens it (the rail's Activity button, the header's
// last-error chip, 0.1.0's "activity" key); Escape or the backdrop closes it. This board /
// All boards, All / Errors. A row whose source a tab owns links back to it on its board
// (route.js routeForSource). The log is this page's (store.js S.log) until the service
// keeps one (plan gap G9): the foot says so.

import { boardName, clock } from "./format.js";
import { html, useEffect } from "./lib.js";
import { modalOpen } from "./modal.js";
import { routeForSource, tabLabel } from "./route.js";
import { changed, closeActivity, lastProblem, navigate, openActivity, S } from "./store.js";
import { Icon, Seg } from "./ui.js";

const ICON = { info: ["info", "i-muted"], ok: ["circle-check", "i-ok"], warning: ["triangle-alert", "i-warn"],
  error: ["circle-x", "i-err"] };
const SHOWN_MAX = 500;

function nameOf(bid) {
  const row = S.boards[bid] || {};
  const b = S.board[bid];
  return boardName((b && b.info && b.info.candidate) || row.candidate, bid);
}

function RowLink({ e }) {
  const route = routeForSource(e.source);
  const row = e.board && S.boards[e.board];
  if (!route || !row) return null;
  const tab = route.split("/")[0];
  const go = () => { closeActivity(); navigate(e.board, route); };
  const where = row.open ? `Show in ${tabLabel(tab)}` : `Show ${nameOf(e.board)}`;
  return html`<button type="button" class="link" data-action="activity-go" data-route=${route}
    title=${`${nameOf(e.board)} › ${route.replace("/", " › ")}`} onClick=${go}>${where}</button>`;
}

export function ActivityDrawer() {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape" && S.ui.drawer && !modalOpen()) closeActivity();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const d = S.ui.drawer;
  if (!d) return null;
  const bid = S.selected && S.boards[S.selected] ? S.selected : null;
  const scope = bid && d.scope === "board" ? "board" : "all";
  const set = (patch) => { S.ui.drawer = { ...d, ...patch }; changed(); };
  const shown = S.log.filter((e) => (d.level === "all" || e.level === "error")
    && (scope === "all" || !e.board || e.board === bid)).slice(-SHOWN_MAX).reverse();
  return html`<div class="scrim" onClick=${closeActivity}></div>
    <aside class="drawer" role="dialog" aria-label="Activity" data-testid="activity-drawer">
      <div class="drawer-in" data-testid="activity">
        <div class="drawer-head"><${Icon} name="history" /><b>Activity</b><span class="grow"></span>
          ${bid ? html`<${Seg} label="Boards" value=${scope} onChange=${(v) => set({ scope: v })} options=${[
            { value: "board", label: nameOf(bid), title: `${nameOf(bid)} (and the service's own rows)` },
            { value: "all", label: "All boards" }]} />` : null}
          <${Seg} label="Show" value=${d.level} onChange=${(v) => set({ level: v })} options=${[
            { value: "all", label: "All" }, { value: "err", label: "Errors" }]} />
          <button type="button" class="btn ghost sm icon-only" aria-label="Close Activity" data-action="activity-close"
            onClick=${closeActivity}><${Icon} name="x" /></button></div>
        <div class="drawer-tools muted small">
          <span class="grow">${shown.length} of ${S.log.length} entries, newest first</span>
          <button type="button" class="btn sm ghost" data-action="activity-clear"
            onClick=${() => { S.log.length = 0; changed(); }}><${Icon} name="trash-2" /> Clear</button></div>
        <div class="drawer-body">
          ${shown.length ? html`<table class="table log drawer-log" data-testid="activity-table">
            <colgroup><col style="width:72px" /><col style="width:26px" /><col /></colgroup>
            <thead class="sr-only"><tr><th>Time</th><th>Level</th><th>Message</th></tr></thead>
            <tbody>${shown.map((e) => html`<tr key=${e.id} data-level=${e.level} data-source=${e.source}>
              <td class="t">${clock(e.at)}</td>
              <td class="lvl">${e.level === "info" ? html`<span class="sr-only">info</span>`
                : html`<${Icon} name=${ICON[e.level][0]} cls=${ICON[e.level][1]} label=${e.level} />`}</td>
              <td><div class="msg">${e.text}</div>
                <div class="meta"><span class="src">${e.source}</span>
                  ${scope === "all" && e.board ? html`<span class="tag">${nameOf(e.board)}</span>` : null}
                  <${RowLink} e=${e} /></div></td>
            </tr>`)}</tbody></table>`
            : html`<p class="muted drawer-empty">Nothing ${d.level === "err" ? "went wrong" : "happened"}
              ${scope === "all" ? "on any board" : `on ${nameOf(bid)}`} since this page opened.</p>`}
        </div>
        <div class="drawer-foot">Since this page opened: the service's events and this page's own
          actions, a refused click too; one failed job is one row.</div>
      </div>
    </aside>`;
}

// The header's last-error chip: this board's newest error or warning the drawer has not shown;
// a click opens the drawer on it (errors: filtered to errors).
export function LastProblemChip({ bid }) {
  const e = lastProblem(bid);
  if (!e) return null;
  const err = e.level === "error";
  return html`<button type="button" class=${`chip ${err ? "err" : "warn"} chip-btn`} data-testid="last-problem-chip"
    data-level=${e.level} title=${`${e.source}: ${e.text}`}
    onClick=${() => openActivity(bid, err ? "err" : "all")}>
    <${Icon} name=${err ? "circle-x" : "triangle-alert"} />Last ${err ? "error" : "warning"} ${clock(e.at).slice(0, 5)}</button>`;
}
