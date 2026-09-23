// Activity: every event from the daemon plus this page's own action results.

import { clock } from "../format.js";
import { html, useState } from "../lib.js";
import { changed, S } from "../store.js";
import { Icon, Seg } from "../ui.js";

const FILTERS = {
  all: ["info", "ok", "warning", "error"],
  problems: ["warning", "error"],
  errors: ["error"],
};
const ICON = { info: ["info", "i-muted"], ok: ["circle-check", "i-ok"], warning: ["triangle-alert", "i-warn"],
  error: ["circle-x", "i-err"] };

export function ActivitySection({ bid }) {
  const [filter, setFilter] = useState("all");
  const [scope, setScope] = useState("board");
  const shown = S.log.filter((e) => FILTERS[filter].includes(e.level)
    && (scope === "all" || !e.board || e.board === bid)).slice(-500).reverse();
  return html`<div class="card" data-testid="activity">
    <div class="card-body">
      <div class="log-tools">
        <${Seg} label="Show" value=${filter} onChange=${setFilter} options=${[
          { value: "all", label: "Everything" }, { value: "problems", label: "Warnings and errors" },
          { value: "errors", label: "Errors" }]} />
        <${Seg} label="Boards" value=${scope} onChange=${setScope} options=${[
          { value: "board", label: "This board" }, { value: "all", label: "All boards" }]} />
        <span class="grow"></span>
        <span class="muted small">${shown.length} of ${S.log.length} entries, newest first</span>
        <button type="button" class="btn sm ghost" onClick=${() => { S.log.length = 0; changed(); }}>
          <${Icon} name="trash-2" /> Clear</button>
      </div>
      ${shown.length ? html`<table class="table log" data-testid="activity-table">
        <colgroup><col style="width:84px" /><col style="width:34px" /><col style="width:150px" /><col /></colgroup>
        <thead><tr><th>Time</th><th><span class="sr-only">Level</span></th><th>Source</th><th>Message</th></tr></thead>
        <tbody>${shown.map((e) => html`<tr key=${e.id} data-level=${e.level}>
          <td class="t">${clock(e.at)}</td>
          <td class="lvl">${e.level === "info" ? html`<span class="sr-only">info</span>`
            : html`<${Icon} name=${ICON[e.level][0]} cls=${ICON[e.level][1]} label=${e.level} />`}</td>
          <td class="src">${e.source}${scope === "all" && e.board ? html`<div class="muted">${e.board}</div>` : null}</td>
          <td class="msg">${e.text}</td>
        </tr>`)}</tbody></table>`
        : html`<p class="muted">Nothing yet. Events from the daemon and the results of your actions land here.</p>`}
    </div>
  </div>`;
}
