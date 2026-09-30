// The Board tab (UI v2): six sub-pages, each one screen: Recover · Versions · Connections ·
// Readings · Access · About, each with its deep link (#/<board>/board/<page>).
//
// PHASE 1 STUB (lane UI2-SHELL): each page renders today's cards (Power, Update + SD card,
// the links, Telemetry + Health + Clocks, the SSH claim + identity, Identity +
// Capabilities), so the tab ships before the Board lane replaces this file with the
// prototype's pages (the Recover ladder, one Versions list with the OS slots, the
// Connections diagram...). The Front panel is not here: it lives on the Overview (decided).
// Every drive button keeps its lease gate: the cards are today's, unchanged.

import { html } from "../lib.js";
import { boardState, hubBoard, navigate, S, subOf } from "../store.js";
import { routeText } from "../sidebar.js";
import { Card, Icon, LinkLine, Reason } from "../ui.js";
import { usbRoute } from "../format.js";
import { week } from "../week.js";
import { ClaimTileRow } from "./claim.js";
import { ClocksSection } from "./clocks.js";
import { CapabilitiesCard, HealthCard, IdentityCard, TelemetryCard } from "./details.js";
import { IdentityTileRow } from "./identity.js";
import { PowerSection } from "./power.js";
import { SdSection } from "./sd.js";
import { UpdateSection } from "./update.js";

function NotRead({ bid }) {
  const b = boardState(bid);
  return html`<${Reason} text=${b.infoError ? `The board's last read failed: ${b.infoError.message}`
    : "The board has not been read yet; this page fills in when it is."} />`;
}

function Recover({ bid }) {
  return html`<${PowerSection} bid=${bid} />`;
}

function Versions({ bid }) {
  return html`<div class="stack">
    <${UpdateSection} bid=${bid} />
    <h2 class="board-page-head" id="board-sd"><${Icon} name="hard-drive" />Configuration SD card</h2>
    <${SdSection} bid=${bid} />
  </div>`;
}

function Connections({ bid }) {
  const b = boardState(bid);
  const row = S.boards[bid] || {};
  const cand = (b.info && b.info.candidate) || row.candidate || {};
  const hub = week(bid).hub;
  const usb = usbRoute(cand, row);
  const conf = row.configured;
  return html`<div class="stack">
    <${Card} title="Links" icon="cable" testid="board-links"
        sub="How this Harness Manager reaches the board now.">
      <dl class="kv">
        <dt>Board id</dt><dd class="mono">${bid}</dd>
        ${conf ? html`<dt>Route</dt><dd>boards.toml <b>${conf.key}</b>: ${routeText(conf)}</dd>` : null}
        <dt>Links</dt>
        <dd>${(cand.links || []).length ? cand.links.map((l) => html`<${LinkLine} key=${l.kind + l.address} link=${l} />`)
          : html`<span class="muted">none reported</span>`}</dd>
        <dt>Debug USB</dt><dd data-testid="board-usb" data-usb=${usb.to}><${Icon} name=${usb.icon} cls="sm" /> ${usb.fact}
          <div class="sub">${usb.detail}</div></dd>
        ${hub ? html`<dt>Hub</dt><dd><span class="mono">${hub.host}</span>
          ${hub.tunnel ? html`<div class="sub">SSH tunnel ${hub.tunnel.state}${hub.tunnel.detail ? `: ${hub.tunnel.detail}` : ""}</div>` : null}</dd>` : null}
      </dl>
    <//>
  </div>`;
}

function Readings({ bid }) {
  const b = boardState(bid);
  return html`<div class="stack">
    <div class="grid split">
      <${TelemetryCard} bid=${bid} />
      ${b.info ? html`<${HealthCard} bid=${bid} info=${b.info} />` : html`<${NotRead} bid=${bid} />`}
    </div>
    <${ClocksSection} bid=${bid} />
  </div>`;
}

function Access({ bid }) {
  const b = boardState(bid);
  const claim = !!(b.info && b.info.claim);
  const ident = !!(b.info && b.info.net_identity);
  return html`<div class="stack">
    <${Card} title="SSH and identity" icon="lock" testid="board-access"
        sub="Who may log in to the board, and the name, address and MAC it answers with.">
      ${claim || ident ? html`<div class="tile-kv">
          <${ClaimTileRow} bid=${bid} />
          <${IdentityTileRow} bid=${bid} />
        </div>`
        : html`<${Reason} text="This harness has no SSH claim and reports no network identity (a Linux harness has both)." />`}
    <//>
    ${hubBoard(bid) ? html`<${Reason} icon="lock" text="Who may drive this board is the hub lease's: the header's Hub fact." />` : null}
  </div>`;
}

function About({ bid }) {
  const b = boardState(bid);
  if (!b.info) return html`<${NotRead} bid=${bid} />`;
  return html`<div class="grid split">
    <${IdentityCard} info=${b.info} />
    <${CapabilitiesCard} info=${b.info} />
  </div>`;
}

export const BOARD_PAGES = [
  { key: "recover", label: "Recover", icon: "rotate-ccw", render: Recover },
  { key: "versions", label: "Versions", icon: "rocket", render: Versions },
  { key: "connections", label: "Connections", icon: "cable", render: Connections },
  { key: "readings", label: "Readings", icon: "thermometer", render: Readings },
  { key: "access", label: "Access", icon: "lock", render: Access },
  { key: "about", label: "About", icon: "info", render: About },
];

export function BoardSection({ bid }) {
  const b = boardState(bid);
  const at = subOf(bid, "board");
  const page = BOARD_PAGES.find((p) => p.key === at) || BOARD_PAGES[0];
  const Page = page.render;
  return html`<div class="board-pages">
    <nav class="board-nav" aria-label="Board pages">
      ${BOARD_PAGES.map((p) => html`<button type="button" key=${p.key} data-board-page=${p.key}
        aria-current=${p.key === page.key ? "page" : undefined}
        onClick=${() => navigate(bid, `board/${p.key}`)}><${Icon} name=${p.icon} cls="sm" />${p.label}
        ${p.key === "versions" && b.pending ? html`<span class="badge" aria-label="needs attention">!</span>` : null}</button>`)}
    </nav>
    <div class="board-page" data-testid=${`board-page-${page.key}`}><${Page} bid=${bid} /></div>
  </div>`;
}
