// Help, by the app's pages (UI v2 round 3, plan M8, lane SHELL-2). The left list is the app:
// Start here, the five tabs, Leases, Settings; each page says what it is for, and names the
// command-line topics that do the same (the CLI's own text, GET /help/tabs, so the two never
// disagree). The CLI's 19 topics are listed whole under "Command line".
//
// It opens on the page the selected board shows. app.js registers its 0.1.0 dialog (the CLI's
// topics only) at load; registerHelp() runs after it (sidebar.js startSidebar) and replaces it
// under the same kind, so the rail's Help button opens this one.

import { call } from "../api.js";
import { html, useEffect, useState } from "../lib.js";
import { ModalShell, registerModal } from "../modal.js";
import { changed, S, sectionOf, timed } from "../store.js";
import { Icon, Reason, Spinner } from "../ui.js";

// [key, title, what it is for, the CLI topics that do the same]
export const HELP_PAGES = [
  ["start", "Start here",
    "Pick a board in the sidebar. A board behind a hub shows who holds its lease: Open and take the lease when it is free, "
    + "or Request board when someone else holds it (requesting does not open or lock the board; the holder is asked). "
    + "A board on your own network opens straight away. Close board gives it back: its default, when the lease is yours "
    + "and a design is loaded, restores the baseline, releases the lease and closes.",
    ["Quick start", "Overview"]],
  ["overview", "Overview",
    "One screen per board: what needs a look (each with its fix), the front panel as the board shows it, the board's "
    + "identity and readings, the design, the lease and who waits for it, the consoles and debug, and recent activity.",
    ["System", "Front panel"]],
  ["workbench", "Workbench",
    "The bring-up loop on one page. Pick a design: its preflight runs under it; arm, then Program. The download bar shows "
    + "guard, swap, push (bytes, rate, time left) and verify. The console below shows the boot; Reset DUT is in its toolbar. "
    + "The right rail opens a debug session (the gdb line) and the logic analysers (ILA over XVC). Program, Reset DUT and "
    + "debug are for the lease holder only.",
    ["Program", "Consoles", "Debug", "Reset"]],
  ["build", "Build",
    "From your RTL to a design on the Workbench, one step at a time: Setup (target, tools, kit), Design (your RTL, "
    + "generics, the RM kit and your constraints), Build (you run Vivado; Harness Manager watches the folder), Check, and "
    + "Add. Building needs no lease; only programming does.",
    ["Build a DUT"]],
  ["board", "Board",
    "Pages in a side list, each on one screen: Recover (from Reset DUT up to a power-cycle, what each keeps), Versions "
    + "(the harness and OS releases, the OS slots, the configuration SD), Connections (where the Ethernet and the Debug "
    + "USB go), Readings, Access (SSH claim, identity, who may drive) and About.",
    ["Reset", "Board controller", "Linux harness", "Updates"]],
  ["checks", "Checks",
    "Unattended hardware-in-the-loop runs, on boards behind a hub: pick a plan and until when, keep the lease, start. "
    + "The announcement to the lab is written for you.",
    ["Lab"]],
  ["lease", "Leases",
    "One lease per hub board, taken from the hub. The header holds the control: acquire, release, request, cancel; "
    + "\"N waiting\" opens the hub's queue (who, their place, how long they want it, since when, their message; "
    + "automation waits apart, behind every person). Releasing hands the board to the next person, as the hub does.",
    ["Hubs and leases"]],
  ["settings", "Settings",
    "Settings opens on General. Hubs and Boards are what boards.toml and settings.toml hold; the theme is in the "
    + "sidebar's foot. A row nothing reads is not offered.",
    ["Settings", "App and service"]],
];

const H = { tabs: null, line: "", error: null, loading: false };

async function loadTabs() {
  if (H.tabs || H.loading) return;
  H.loading = true;
  const r = await timed("help --tabs", () => call("helpTabs"));
  H.loading = false;
  H.line = r.line;
  H.error = r.error;
  if (!r.error) H.tabs = r.data.data.tabs || [];
  changed();
}

// The page to open on: the selected board's tab, else Start here.
function pageFor() {
  const bid = S.selected;
  const row = bid && S.boards[bid];
  if (!row || !row.open) return "start";
  const tab = sectionOf(bid);
  return HELP_PAGES.some((p) => p[0] === tab) ? tab : "start";
}

function HelpDialog({ page = "" }) {
  const [cur, setCur] = useState(() => page || pageFor());
  useEffect(() => { loadTabs(); }, []);
  const cli = cur.startsWith("cli:") ? cur.slice(4) : "";
  const p = HELP_PAGES.find((x) => x[0] === cur);
  const tab = cli && H.tabs ? H.tabs.find((t) => t.name === cli) : null;
  const has = (name) => !!(H.tabs && H.tabs.some((t) => t.name === name));
  return html`<${ModalShell} title="Help" icon="book-open" testid="help" cls="wide help" bodyCls="help-body"
      note="organised by the app's pages; the command line's topics under Command line">
    <nav class="modal-nav help-nav" aria-label="Help topics" data-testid="help-nav">
      ${HELP_PAGES.map(([k, t]) => html`<button type="button" key=${k} data-help=${k}
        aria-current=${cur === k ? "true" : "false"} onClick=${() => setCur(k)}>${t}</button>`)}
      <div class="nav-group">Command line</div>
      ${H.tabs ? H.tabs.map((t) => html`<button type="button" key=${`cli:${t.name}`} data-help=${`cli:${t.name}`}
        aria-current=${cur === `cli:${t.name}` ? "true" : "false"} onClick=${() => setCur(`cli:${t.name}`)}>${t.name}</button>`)
        : H.loading ? html`<div class="muted small nav-group"><${Spinner} /> reading…</div>` : null}
    </nav>
    <div class="help-pane" data-testid="help-pane" data-help=${cur}>
      ${p ? html`<h3>${p[1]}</h3><p data-testid="help-text">${p[2]}</p>
          ${p[3].length ? html`<p class="small secondary">The same on the command line:${" "}
            ${p[3].map((name, i) => html`${i ? ", " : ""}${has(name) ? html`<button type="button" class="linkish"
              data-help-cli=${name} onClick=${() => setCur(`cli:${name}`)}>${name}</button>` : html`<span>${name}</span>`}`)}
            ${" "}(<code>harness-manager help</code>).</p>` : null}`
        : cli ? (H.error ? html`<${Reason} level="err" text=${`${H.line}: ${H.error.message}`} />`
          : tab ? html`<h3><${Icon} name="terminal" /> ${tab.name}</h3><pre class="modal-text">${tab.text}</pre>`
            : html`<p class="muted"><${Spinner} /> Reading the help...</p>`) : null}
    </div>
  <//>`;
}

export function registerHelp() {
  registerModal("help", HelpDialog);
}
