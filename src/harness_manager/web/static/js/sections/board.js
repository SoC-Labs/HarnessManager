// The Board tab (UI v2 round 3, lane UI2-BOARD; docs/design/ui-v2/prototype-b-round3.html
// "B3-BOARD"): six sub-pages in a side list, each one screen at 1440 x 900: Recover ·
// Versions · Connections · Readings · Access · About. Each list item has a one-line status
// and a dot when something needs a look. A link opens the page it names
// (#/<board>/board/<page>); with none, the tab opens what needs a look, else Recover.
//
// Every value comes from the service (the board read, its session, the card and slot
// reads, the lease, the catalogue); a fact the service does not give says "not known" with
// the reason, never a made-up value (UI_V2_PLAN.md §1.8).

import { call } from "../api.js";
import { boardName, capTitle as capTitleOf, clock } from "../format.js";
import { html, useEffect, useLayoutEffect, useRef } from "../lib.js";
import { boardState, changed, navigate, S, select, timed } from "../store.js";
import { durationText, leaseLeft, leaseWho, week } from "../week.js";
import { Card, CheckChip, Chip, Icon, LinkLine, Reason } from "../ui.js";
import {
  featuresOf, hasCap, identityOf, isLinux, mccRoute, osKind, USB_WORDS,
} from "./boardfacts.js";
import { claimOf, ClaimCard } from "./claim.js";
import { ReadingsPage, readingsStatus } from "./clocks.js";
import { IdentityCard, identityOf as netIdentityOf } from "./identity.js";
import { RecoverPage, recoverStatus } from "./power.js";
import { VersionsPage, versionsStatus } from "./update.js";

// --- Connections: how this PC reaches the board, and what each link gives ---------------------

// What the Debug USB gives, and where it goes (G2's route and reason, as the service says it).
function usbFacts(bid) {
  const b = boardState(bid);
  const r = mccRoute(bid);
  const noSd = !hasCap(b, "storage_backup") && !!b.info;
  const gives = [["MCC console", r.to === "hub" || r.to === "pc" || r.to === "self"],
    ["MCC reboot", hasCap(b, "reboot_board")], ["config SD", !noSd && r.to !== "none"],
    ["temperature", hasCap(b, "telemetry_temp")]];
  const words = USB_WORDS[r.to] || USB_WORDS.unknown;
  const lvl = r.to === "none" ? "warn" : r.to === "unknown" ? "unk" : "accent";
  const out = { to: r.to, chip: words.chip, icon: words.icon, lvl, where: r.reason, gives, note: "", fix: "" };
  if (r.to === "hub") out.note = "One reader on tty_00 at a time: Harness Manager goes through the hub, never an fpgahub share.";
  if (r.to === "self") out.note = "No hub or PC needed. It lives while the harness runs, so a wedged board cannot reboot itself.";
  if (r.to === "none") {
    out.gives = gives.map(([n]) => [n, false]);
    out.note = isLinux(b) ? "OS updates and netboot still work: they go over Ethernet."
      : "Harness installs need it: they write the config SD.";
    out.fix = "Connect J8 (Debug USB) to the hub or to this PC.";
  }
  return out;
}

function CxDiagram({ bid, u, hub }) {
  const name = boardName((boardState(bid).info || {}).candidate || (S.boards[bid] || {}).candidate, bid);
  const usbTxt = { hub: "the hub's Debug USB reaches the board's MCC", self: "the board's USB host is looped back into its own MCC",
    pc: "this PC's Debug USB reaches the board's MCC", none: "nothing reaches the board's MCC",
    unknown: "the Debug USB is not known yet" }[u.to] || "the Debug USB is not known yet";
  const claim = `${hub ? "Ethernet through the hub's ssh tunnel" : "Ethernet straight to the board"}; ${usbTxt}.`;
  const hubName = hub ? String(hub.host || "hub").split(".")[0] : "";
  return html`<figure class="cx-fig" data-testid="cx-diagram" data-usb=${u.to} data-hub=${hub ? "yes" : "no"}>
    <svg class="cx-svg" viewBox="0 0 440 150" role="img" aria-label=${`${name}: ${claim}`}>
      <rect class="cx-box" x="2" y="30" width="92" height="106" rx="8" />
      <text class="cx-t" x="48" y="80">This PC</text><text class="cx-s" x="48" y="96">Harness Manager</text>
      ${hub ? html`<rect class="cx-box" x="164" y="30" width="100" height="106" rx="8" />
        <text class="cx-t" x="214" y="80">Hub</text><text class="cx-s" x="214" y="96">${hubName}</text>` : null}
      <rect class="cx-board" x="330" y="3" width="108" height="144" rx="10" />
      <text class="cx-t" x="384" y="19">${name.length > 14 ? `${name.slice(0, 13)}…` : name}</text>
      <rect class="cx-box" x="340" y="28" width="88" height="46" rx="6" />
      <text class="cx-t" x="384" y="48">FPGA</text><text class="cx-s" x="384" y="63">harness · JTAG</text>
      <rect class=${`cx-box ${u.to === "none" || u.to === "unknown" ? "cx-dim" : ""}`} x="340" y="90" width="88" height="46" rx="6" />
      <text class="cx-t" x="384" y="110">MCC</text><text class="cx-s" x="384" y="125">config SD</text>
      ${hub ? html`<line class="cx-eth" x1="94" y1="51" x2="164" y2="51" /><text class="cx-l" x="129" y="44">ssh tunnel</text>
          <line class="cx-eth" x1="264" y1="51" x2="340" y2="51" /><text class="cx-l" x="302" y="44">Ethernet</text>`
        : html`<line class="cx-eth" x1="94" y1="51" x2="340" y2="51" /><text class="cx-l" x="217" y="44">Ethernet, direct</text>`}
      ${u.to === "hub" ? html`<line class="cx-usb" x1="264" y1="113" x2="340" y2="113" /><text class="cx-l cx-lu" x="302" y="106">Debug USB</text>` : null}
      ${u.to === "pc" ? html`<line class="cx-usb" x1="94" y1="113" x2="340" y2="113" /><text class="cx-l cx-lu" x="217" y="106">Debug USB</text>` : null}
      ${u.to === "self" ? html`<polyline class="cx-usb" points="340,64 314,64 314,113 340,113" /><text class="cx-l cx-lu cx-end" x="308" y="86">USB</text><text class="cx-l cx-lu cx-end" x="308" y="99">loop</text>` : null}
      ${u.to === "none" ? html`<line class="cx-none" x1="340" y1="113" x2="300" y2="113" /><path class="cx-x" d="M288 107 l10 10 M298 107 l-10 10" /><text class="cx-l cx-ln" x="293" y="134">no USB</text>` : null}
    </svg>
    <figcaption class="cx-cap">${claim[0].toUpperCase() + claim.slice(1)}</figcaption></figure>`;
}

function Connections({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  const hub = w.hub;
  const u = usbFacts(bid);
  const lx = isLinux(b);
  const c = claimOf(b);
  const tunnel = hub && hub.tunnel;
  const eth = ((b.info && b.info.candidate) || (S.boards[bid] || {}).candidate || {}).links || [];
  const ethLink = eth.find((l) => l.kind === "ethernet");
  const stats = (b.info && b.info.stats) || null;
  const speed = stats && stats.spd ? `${stats.spd} Mb/s${stats.fdx === true ? " full duplex" : stats.fdx === false ? " half duplex" : ""}` : "";
  const jtag = featuresOf(b).includes("jtag_server");
  return html`<${Card} title="Connections" icon="cable" cls="cx" testid="connections-card"
      sub="How this PC reaches the board, and what each link gives.">
    <${CxDiagram} bid=${bid} u=${u} hub=${hub} />
    <div class="cx-cols">
    <ul class="cx-rows" aria-label="The links">
      <li class="cx-row" data-testid="cx-ethernet"><span class="cx-k"><${Icon} name="ethernet-port" />Ethernet</span><div class="cx-v">
        <div class="cx-line">${b.infoError ? html`<${Chip} level="warn" icon="triangle-alert">Last read failed ${b.infoErrorAt ? clock(b.infoErrorAt) : ""}<//>`
          : b.info ? html`<${Chip} level="ok" icon="circle-check">Up<//>` : html`<${Chip} level="unk" icon="circle-help">Not read yet<//>`}
          ${speed ? html`<span class="small secondary">${speed}</span>` : null}
          ${stats && stats.link === false ? html`<span class="small secondary">link down</span>` : null}</div>
        <div class="small secondary"><span class="mono">${ethLink ? ethLink.address : bid.replace(/^[^@]*@/, "")}</span> · ${hub
          ? `through the hub's ssh tunnel (${hub.host}); only the hub reaches the board`
          : "direct, on this network"}</div>
        ${tunnel ? html`<div class="small muted" data-testid="cx-tunnel" title=${tunnel.detail || ""}>ssh tunnel ${tunnel.state}${tunnel.ports ? ` · ${Object.keys(tunnel.ports).length} port${Object.keys(tunnel.ports).length === 1 ? "" : "s"} (${Object.keys(tunnel.ports).join(", ")})` : ""}${tunnel.restarts ? ` · ${tunnel.restarts} restart${tunnel.restarts === 1 ? "" : "s"}` : ""}</div>` : null}</div></li>
      <li class="cx-row" data-testid="cx-usb" data-usb=${u.to}><span class="cx-k"><${Icon} name="usb" />Debug USB</span><div class="cx-v">
        <div class="cx-line"><${Chip} level=${u.lvl} icon=${u.icon}>${u.chip}<//></div>
        <div class="small secondary" data-testid="board-usb" data-usb=${u.to}>${u.where}</div>
        <div class="cx-gives">${u.gives.map(([n, ok]) => html`<span key=${n} class=${`cx-give ${ok ? "" : "no"}`} title=${`${n}: ${ok ? "available" : "not available"}`}
          ><${Icon} name=${ok ? "check" : "x"} />${n}</span>`)}</div>
        ${u.note ? html`<div class="small muted">${u.note}</div>` : null}
        ${u.fix ? html`<${Reason} level="warn" text=${`Fix: ${u.fix}`} />` : null}</div></li>
    </ul>
    <ul class="cx-rows" aria-label="Over the Ethernet">
      <li class="cx-row" data-testid="cx-ssh"><span class="cx-k"><${Icon} name="terminal" />SSH</span><div class="cx-v">
        ${!lx ? html`<div class="cx-line"><${Chip} level="plain">None<//><span class="small secondary">the bare-metal harness has no SSH</span></div>`
          : !c ? html`<div class="cx-line"><${Chip} level="unk" icon="circle-help">Not known<//><span class="small secondary">the board has not reported its claim</span></div>`
          : html`<div class="cx-line">${c.state === "mine" ? html`<${Chip} level="ok" icon="lock">Claimed by you<//>`
              : c.state === "other" ? html`<${Chip} level="held" icon="lock">Claimed by another key<//>`
              : c.state === "unclaimed" ? html`<${Chip} level="warn" icon="lock-open">Not claimed<//>`
              : html`<${Chip} level="unk" icon="circle-help">Unknown<//>`}
              <span class="small secondary mono">${c.user || "root"}@${(ethLink ? ethLink.address : "").split(":")[0] || "board"}${hub ? ` (ssh -J ${String(hub.host).split(".")[0]})` : ""}</span></div>
            <div class="small muted">${c.state === "mine" ? `Key ${(c.claimed && c.claimed.key_fp) || "?"}. Slot and card changes go through this SSH.`
              : c.state === "unclaimed" ? html`Anyone with the image's default key can log in. <button type="button" class="link-btn" onClick=${() => navigate(bid, "board/access")}>Claim it on Access</button>`
              : c.state === "other" ? "Slot and card changes need the claiming key." : ""}</div>`}</div></li>
      <li class="cx-row" data-testid="cx-jtag"><span class="cx-k"><${Icon} name="cpu" />JTAG</span><div class="cx-v">
        ${jtag ? html`<div class="cx-line"><${Chip} level="plain" icon="circle-check">Built in<//><span class="small secondary">the harness's own jtag_server</span></div>
            <div class="small muted">Over the same Ethernet${hub ? " (tunnelled)" : ""}; no JTAG cable to the board.</div>`
          : html`<div class="cx-line"><${Chip} level="unk">Not reported<//><span class="small secondary">this harness does not report jtag_server</span></div>`}</div></li>
    </ul></div>
  <//>`;
}

// --- Access: SSH claim, identity, who may drive ---------------------------------------------------

function WhoDrives({ bid }) {
  const who = leaseWho(bid);
  const row = S.boards[bid] || {};
  const lock = row.holder && row.holder.text;
  const q = (who.hub && who.hub.queue) || [];
  let head;
  let lvl = "";
  if (who.state === "none") {
    head = "No hub lease: this board is not behind a hub. Whoever opens it in this Harness Manager drives it.";
  } else if (who.state === "here") {
    const left = leaseLeft(who.lease);
    head = `You hold the hub lease${left !== null ? ` for ${durationText(left)} more` : ""}: you drive it; others watch.`;
    lvl = "ok";
  } else if (who.state === "free") {
    head = "Nobody holds the hub lease: take it in the header to drive the board.";
  } else if (who.state === "unread") {
    head = "Reading the board's hub lease...";
  } else if (who.state === "unknown") {
    head = `The hub lease could not be read (${who.error}): not known is not free, so nothing drives it from here.`;
    lvl = "warn";
  } else {
    head = `${who.holder} holds the hub lease${who.state === "elsewhere" ? " in another session" : ""}: you watch; they drive.`;
    lvl = "warn";
  }
  return html`<${Card} title="Who may drive this board" icon="shield-check" testid="access-drive"
      sub="Recover, Program, the clock and debug are for the one who drives; everyone else watches.">
    <${Reason} level=${lvl} icon=${lvl === "ok" ? "circle-check" : "lock"} testid="access-drive-rule" text=${head} />
    <dl class="kv mt-8">
      ${who.hub ? html`<dt>Hub</dt><dd><span class="mono small">${who.target || "?"}</span> on ${who.host || "?"}</dd>
        <dt>Waiting</dt><dd data-testid="access-queue">${q.length ? q.map((x) => `#${x.position} ${x.holder}${x.mine ? " (you)" : ""}`).join(" · ") : "nobody"}</dd>` : null}
      <dt>Board lock</dt><dd class="small">${lock || html`<span class="muted">not held</span>`}</dd>
    </dl><//>`;
}

// G10: the clashes across every board this Harness Manager has seen (no board contact). A
// service without the route leaves it out (the board's own net_identity still warns).
async function loadClashes(bid) {
  const b = boardState(bid);
  const r = await timed("identity clashes", () => call("identityClashes"));
  b.clashes = r.error ? null : (r.data.data.clashes || []);
  changed();
}

export function clashesFor(bid) {
  const b = boardState(bid);
  return (b.clashes || []).filter((c) => (c.boards || []).some((x) => x.board_id === bid));
}

function Access({ bid }) {
  const b = boardState(bid);
  useEffect(() => { loadClashes(bid); }, [bid]);
  return html`<div class="bt-pair">
    <div class="bt-stack">
      <${ClaimCard} bid=${bid} />
      <${WhoDrives} bid=${bid} />
    </div>
    <${IdentityCard} bid=${bid} clashes=${clashesFor(bid)}
      goTo=${(other) => { if (S.boards[other]) select(other); }} />
    ${!b.info ? html`<${Reason} text="The board has not been read yet; this page fills in when it is." />` : null}
  </div>`;
}

// --- About: the fixed facts, and what this board cannot do -----------------------------------------

function imageText(bid) {
  const b = boardState(bid);
  const k = osKind(bid);
  if (k === "bm") return "the config SD (bare-metal)";
  if (k === "netboot") return "netbooted: the hub serves the image at every cold boot (no user microSD)";
  if (k === "card") {
    const run = (b.slots && b.slots.slots) || (b.card && b.card.os_slots) || {};
    const s = (run.slots || {})[run.running] || {};
    return `slot ${run.running || "?"} of the user microSD${s.version ? ` (${s.version})` : ""}`;
  }
  return "not known yet";
}

function About({ bid }) {
  const b = boardState(bid);
  if (!b.info) {
    return html`<${Reason} text=${b.infoError ? `The board's last read failed: ${b.infoError.message}`
      : "The board has not been read yet; this page fills in when it is."} />`;
  }
  const id = identityOf(b);
  const row = S.boards[bid] || {};
  const hub = row.hub || null;
  const feats = featuresOf(b);
  const missing = Object.entries(b.info.unavailable || {});
  const links = ((b.info.candidate || {}).links) || [];
  return html`<${Card} title="About this board" icon="info" testid="about-card"
      sub="The fixed facts, and what this board cannot do.">
    <div class="bt-about">
      <dl class="kv" data-testid="identity-card">
        <dt>Board id</dt><dd class="mono small">${bid}</dd>
        <dt>Links</dt><dd>${links.length ? links.map((l) => html`<${LinkLine} key=${l.kind + l.address} link=${l} />`)
          : html`<span class="muted small">none reported</span>`}</dd>
        ${hub ? html`<dt>Hub target</dt><dd class="mono small">${hub.target || "?"} · ${hub.host}</dd>` : null}
        <dt>Shell</dt><dd class="mono small" data-testid="about-shell">${id.shell_id || "unknown"}</dd>
        <dt>Harness</dt><dd class="small" data-testid="about-harness">${id.harness_version || "unknown"}${id.harness_impl ? ` · ${id.harness_impl}` : ""}${id.proto ? ` · protocol ${id.proto}` : ""}
          ${id.firmware_sha ? html` · fw <span class="mono">${id.firmware_sha}</span>` : null}</dd>
        <dt>Image</dt><dd class="small">${imageText(bid)}</dd>
        ${id.usercode ? html`<dt>Usercode</dt><dd class="mono small">${id.usercode}</dd>` : null}
        <dt>Build check</dt><dd class="small"><${CheckChip} check=${id.build_check} testid="about-build" /></dd>
        <dt>Features</dt><dd>${feats.length ? html`<div class="tags" data-testid="about-features">${feats.map((f) => html`<span class="tag" key=${f}>${f}</span>`)}</div>`
          : html`<span class="muted">none reported</span>`}</dd>
      </dl>
      <div data-testid="capabilities-card"><div class="bt-sub-h">Not available here</div>
        ${missing.length ? html`<ul class="bt-caps" data-testid="about-missing">${missing.map(([n, why]) => html`<li key=${n} data-capability=${n}>
          <b>${capTitleOf(n)}</b>: ${why}</li>`)}</ul>`
          : html`<p class="small muted">Everything this board pack offers is available.</p>`}
        <details class="os-more"><summary>What it can do here (${(b.info.capabilities || []).length})</summary>
          <div class="bt-cando">${(b.info.capabilities || []).map((n) => html`<span class="cap" key=${n} data-capability=${n} title=${n}>${capTitleOf(n)}</span>`)}</div></details></div>
    </div><//>`;
}


// --- the side list --------------------------------------------------------------------------------

function connectionsStatus(bid) {
  const r = mccRoute(bid);
  const words = USB_WORDS[r.to] || USB_WORDS.unknown;
  return [words.nav, r.to === "none" ? "warn" : null, `Debug USB: ${r.reason || words.chip}`];
}

function accessStatus(bid) {
  const b = boardState(bid);
  const clash = clashesFor(bid)[0];
  const ni = netIdentityOf(b);
  if (clash) {
    const other = clash.boards.find((x) => x.board_id !== bid) || {};
    return [`${clash.field.toUpperCase()} clash with ${other.name || other.target || "another board"}`, "err",
      `Same ${clash.field} ${clash.value} as ${other.name || other.board_id || "another board"}`];
  }
  if (ni && ni.status === "clash") return ["identity clash", "err", "This board's identity clashes with another board's"];
  if (!b.info) return ["not read yet", null, "The board has not been read yet"];
  if (!isLinux(b)) return ["no SSH (bare-metal)", null, "Bare-metal: no SSH"];
  const c = claimOf(b);
  if (c && c.host_key && c.host_key.match === false) return ["SSH host key changed", "err", "The board's host key is not the pinned one: SSH is refused"];
  if (c && c.state === "mine") return ["SSH claimed", null, `SSH claimed with ${(c.claimed && c.claimed.key_fp) || "your key"}`];
  if (c && c.state === "unclaimed") return ["SSH not claimed", "warn", "Anyone with the image's default key can log in"];
  if (c && c.state === "other") return ["claimed by another key", null, "Claimed by a key this Harness Manager did not claim with"];
  return ["SSH claim not known", null, "The board has not reported its claim"];
}

function aboutStatus(bid) {
  const b = boardState(bid);
  if (!b.info) return ["not read yet", null, "The board has not been read yet"];
  const n = featuresOf(b).length;
  const m = Object.keys(b.info.unavailable || {}).length;
  return [`${n} feature${n === 1 ? "" : "s"} · ${m} not here`, null, "Fixed facts, and what this board cannot do"];
}

export const BOARD_PAGES = [
  { key: "recover", label: "Recover", icon: "power", render: RecoverPage, status: recoverStatus },
  { key: "versions", label: "Versions", icon: "rocket", render: VersionsPage, status: versionsStatus },
  { key: "connections", label: "Connections", icon: "cable", render: Connections, status: connectionsStatus },
  { key: "readings", label: "Readings", icon: "thermometer", render: ReadingsPage, status: readingsStatus },
  { key: "access", label: "Access", icon: "shield-check", render: Access, status: accessStatus },
  { key: "about", label: "About", icon: "info", render: About, status: aboutStatus },
];

const DOT_WORDS = { err: "needs a fix", warn: "needs a look", held: "watch only", accent: "running" };

function statusOf(bid, p) {
  try {
    const [text, dot, title] = p.status(bid);
    return { text, dot, title: `${p.label}: ${title}` };
  } catch (e) {
    return { text: "", dot: null, title: p.label };    // a status line never breaks the tab
  }
}

// With no page chosen: what needs a look (an error first, then a running or broken install),
// else Recover.
function defaultPage(nav) {
  const err = nav.find((n) => n.dot === "err");
  if (err) return err.key;
  const v = nav.find((n) => n.key === "versions");
  if (v && (v.dot === "warn" || v.dot === "accent")) return "versions";
  return "recover";
}

export function BoardSection({ bid }) {
  // G10's clashes feed Access's status line: read once per board shown (no board contact)
  useEffect(() => { if (boardState(bid).clashes === undefined) loadClashes(bid); }, [bid]);
  const nav = BOARD_PAGES.map((p) => ({ ...p, ...statusOf(bid, p) }));
  const chosen = (S.subs[bid] || {}).board;
  const page = BOARD_PAGES.find((p) => p.key === chosen) || BOARD_PAGES.find((p) => p.key === defaultPage(nav));
  const Page = page.render;
  const box = useRef(null);
  // a new page starts at its top (the tab's body scrolls, not the page)
  useLayoutEffect(() => {
    const sb = box.current && box.current.closest(".section-body");
    if (sb) sb.scrollTop = 0;
  }, [page.key, bid]);
  return html`<div class="bt" ref=${box} data-testid="board-tab">
    <nav class="bt-nav" aria-label="Board pages">
      ${nav.map((p) => html`<button type="button" key=${p.key} class="bt-nav-i" data-board-page=${p.key}
          aria-current=${p.key === page.key ? "page" : undefined} title=${p.title}
          onClick=${() => navigate(bid, `board/${p.key}`)}><${Icon} name=${p.icon} />
        <span class="bt-nav-t"><span class="bt-nav-l">${p.label}</span>
          <span class="bt-nav-s" data-testid=${`board-status-${p.key}`}>${p.text}</span></span>
        ${p.dot ? html`<span class=${`bt-dot ${p.dot}`} role="img" aria-label=${DOT_WORDS[p.dot]} data-dot=${p.dot}></span>` : html`<span></span>`}
      </button>`)}
    </nav>
    <div class="bt-page" data-testid=${`board-page-${page.key}`}><${Page} bid=${bid} /></div>
  </div>`;
}

