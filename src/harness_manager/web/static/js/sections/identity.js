// The board's identity, on Board > Access (lane BOARD-ID; docs/API.md "Board identity",
// docs/design/BOARD_IDENTITY.md). What the board says it is (label, IP, MAC), a warning when
// it clashes with another board or is not what its hub entry says, and ONE dialog to change it
// (lane IDENTITY, HM v0.1.1): "Name this board" (openModal("name-board", {bid, prefill?, hub?,
// impl?})). It folds the old "Fix identity" (the hub entry: a mode of the same dialog) and the
// bring-up wizard's "Set this board's identity" (its last step opens this dialog, pre-filled).
// Every change is a 202 job that sets it, restarts the harness WARM (its reboot verb, never an
// MCC REBOOT) and reads it back, after the typed phrase (the new name).
// A board with no Ethernet harness has no info.net_identity, so nothing shows.

import { runJob } from "../actions.js";
import { call, identityCall, toApiError } from "../api.js";
import { closeBoardConsoles } from "../consoles.js";
import { html, useEffect, useRef, useState } from "../lib.js";
import { boardState, changed, navigate, openedOrClosedHere, probe, refreshInfo, S, toast } from "../store.js";
import { openBoardHere } from "../sidebar.js";
import { holderOnly } from "../week.js";
import { Card, Icon, Reason, Seg, Spinner } from "../ui.js";
import { closeModal, ModalShell, openModal, registerModal } from "../modal.js";

// --- the stylesheet: this lane's, linked once (index.html is the integrator's: CCR IDENTITY-2) ---
(function linkSheet() {
  if (typeof document === "undefined" || document.querySelector('link[href$="css/identity.css"]')) return;
  const l = document.createElement("link");
  l.rel = "stylesheet";
  l.href = "./css/identity.css";
  document.head.appendChild(l);
}());

export function identityOf(b) {
  return (b && b.netIdentity) || (b && b.info && b.info.net_identity) || null;
}

export async function loadIdentity(bid, { refresh = false } = {}) {
  const b = boardState(bid);
  if (b.netIdentityLoading) return;
  b.netIdentityLoading = true;
  changed();
  try {
    const { data } = await call("netIdentity", { bid }, undefined, refresh ? { refresh: "true" } : null);
    b.netIdentity = data.identity || null;
    b.netIdentityError = null;
  } catch (e) {
    b.netIdentityError = toApiError(e);
  } finally {
    b.netIdentityLoading = false;
    changed();
  }
}

// The worst findings first, in one sentence each.
function problemText(st) {
  const bad = (st.findings || []).filter((f) => f.level === "err" || f.level === "warn");
  if (!bad.length) return "";
  const head = st.status === "clash" ? "Identity clash: " : st.status === "unset" ? "Identity not set: " : "Not its hub entry: ";
  const text = bad.map((f) => f.text).join("; ");
  // V7-ALIGN: the finding already says "identity not set (default label, ...)": not twice.
  const lead = head.slice(0, -2).toLowerCase();
  if (text.toLowerCase().startsWith(lead)) return text.charAt(0).toUpperCase() + text.slice(1) + ".";
  return head + text + ".";
}

// --- "Name this board" (lane IDENTITY, david 2 Oct) ------------------------------------------------
//
// A name is 1-16 of A-Z, 0-9 and - (upper-cased: the aligned panel shows 16). The MAC is random
// (a locally administered one, never the image's 02:00:00:*), kept, or your own; the IP comes
// from the pool (the next free address), is kept, or is your own. The service decides every
// value and guard (GET .../identity/proposal, asked again on each choice): this page only
// shows them, counts the name and checks it as you type.

export const NAME_MAX = 16;
export const NO_IDENTITY_STORE = "the bare-metal harness has no identity store: its label, IP and MAC are compiled into the firmware; the name is for the Linux harness (net-protocol v0.16 identity_set)";

export function nameProblem(text) {
  const t = String(text || "").trim().toUpperCase();
  if (!t) return "it is empty";
  const bad = [...new Set([...t].filter((c) => !/[A-Z0-9-]/.test(c)))].sort();
  if (bad.length) return `it has ${bad.map((c) => (c === " " ? "a space" : `'${c}'`)).join(", ")} (only A-Z, 0-9 and - are allowed)`;
  if (t.length > NAME_MAX) return `it is ${t.length} characters (at most ${NAME_MAX}: the panel shows ${NAME_MAX})`;
  return "";
}

// "" when the board can take a change; else why not (the board's own words, and "predates").
export function identityRefusal(st, impl) {
  if (impl && impl !== "linux") return `This board cannot take a name: ${NO_IDENTITY_STORE}.`;
  if (!st) return "This board's harness does not report its identity: it predates net-protocol v0.16 (identity_set), so Harness Manager cannot set it. Update its harness, then name it on Board > Access.";
  const ref = st.fix && st.fix.refusal;
  if (!ref) return "";
  const r = st.reported || {};
  if (r.impl && r.impl !== "linux") return `This board cannot take a name: ${ref.message}.`;
  if (/no identity verbs/.test(ref.message || "")) return `This board's harness predates identity_set: ${ref.message}.`;
  return `Not now: ${ref.message}${ref.hint ? ` (${ref.hint})` : ""}.`;
}

const addr = (ip) => String(ip || "").split("/")[0];

function Changes({ changes }) {
  return html`<ul class="small nb-changes" data-testid="identity-changes">
    ${changes.map((c) => html`<li key=${c.field} data-field=${c.field}><span class="mono">${c.field}</span>
      ${" "}<span class="mono">${c.from || "-"}</span> → <b class="mono">${c.to}</b></li>`)}
  </ul>`;
}

// After a board moved to its new address: find it there, open it, and close the old session.
async function closeOld(oldBid) {
  try {
    await call("closeBoard", { bid: oldBid });
    openedOrClosedHere(oldBid, false);
    closeBoardConsoles(oldBid);
    delete S.board[oldBid];
  } catch (e) { /* the old session stays listed: closing it is the user's */ }
  changed();
}

async function openMoved(oldBid, moved) {
  closeModal();
  // a board keyed by its unit (not its address) keeps its id: its session at the old address
  // goes first, then it is opened again at the new one
  if (oldBid && moved.to === oldBid) await closeOld(oldBid);
  await probe([moved.host]);
  const id = S.boards[moved.to] ? moved.to
    : Object.keys(S.boards).find((k) => k.includes(`@${moved.host}:`)) || moved.to;
  const r = await openBoardHere(id);
  if (r && r.error && r.error.errName !== "ALREADY") {
    toast(`Not open at ${moved.host} yet: add it By address`, { icon: "triangle-alert", level: "err" });
    return;
  }
  navigate(id, "board/access");
  if (oldBid && oldBid !== id) await closeOld(oldBid);
}

function NameBoard({ bid, prefill = null, hub = false, impl = "", close }) {
  const b = boardState(bid);
  const asks = !impl || impl === "linux";             // bare metal: nothing to ask, its refusal is known
  const [read, setRead] = useState(!asks);
  const [mode, setMode] = useState(hub ? "hub" : "name");
  const [name, setName] = useState(prefill && prefill.label ? String(prefill.label).toUpperCase() : "");
  const [macMode, setMacMode] = useState(prefill && prefill.mac ? "random" : "");
  const [macRandom, setMacRandom] = useState((prefill && prefill.mac) || "");
  const [macCustom, setMacCustom] = useState("");
  const [ipMode, setIpMode] = useState(prefill && prefill.ip ? "auto" : "");
  const [ipAuto, setIpAuto] = useState((prefill && prefill.ip) || "");
  const [ipCustom, setIpCustom] = useState("");
  const [p, setP] = useState(null);                 // the last proposal
  const [pErr, setPErr] = useState(null);
  const [asking, setAsking] = useState(false);
  const [typed, setTyped] = useState("");
  const [hubTyped, setHubTyped] = useState("");
  const [otherSubnet, setOtherSubnet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [done, setDone] = useState(null);
  const seq = useRef(0);
  const timer = useRef(null);

  // Ask the service for the proposal with these choices (the latest answer wins). The choices
  // are read when the question is asked (a debounced one sees the latest), `o` overrides those
  // just set in the same click.
  const live = useRef({});
  live.current = { name, macMode, macRandom, macCustom, ipMode, ipAuto, ipCustom };
  const ask = async (o = {}) => {
    const v = { ...live.current, ...o };
    const mm = v.macMode;
    const im = v.ipMode;
    const q = { label: v.name };
    if (mm === "random") q.mac = o.regen ? "random" : v.macRandom || "random";
    else if (mm === "keep") q.mac = "keep";
    else if (mm === "custom") q.mac = v.macCustom || "keep";
    if (im === "auto") q.ip = v.ipAuto || "auto";
    else if (im === "keep") q.ip = "keep";
    else if (im === "custom") q.ip = v.ipCustom || "keep";
    const n = ++seq.current;
    setAsking(true);
    try {
      const { data } = await identityCall("identityProposal", { bid }, q);
      if (n !== seq.current) return;
      const got = data.proposal;
      setP(got); setPErr(null);
      if (!mm) setMacMode(got.mac_how === "keep" ? "keep" : "random");
      if (!im) setIpMode(got.ip_how === "keep" ? "keep" : "auto");
      if ((!mm && got.mac_how !== "keep") || (mm === "random" && (o.regen || !v.macRandom))) setMacRandom(got.mac || "");
      if ((!im && got.ip_how !== "keep") || (im === "auto" && !v.ipAuto)) setIpAuto(got.ip || "");
      // a board named already keeps its name in the field; the image's own name is no name
      const curLabel = got.current && got.current.label ? String(got.current.label) : "";
      if (!v.name && o.name === undefined && curLabel && !mm && !prefill && curLabel !== "MPS3") setName(curLabel.toUpperCase());
    } catch (e) {
      if (n === seq.current) setPErr(toApiError(e));
    } finally {
      if (n === seq.current) setAsking(false);
    }
  };
  const askSoon = (o) => { clearTimeout(timer.current); timer.current = setTimeout(() => ask(o), 250); };

  useEffect(() => {
    if (!asks) return undefined;
    Promise.resolve(loadIdentity(bid, { refresh: true })).then(() => { setRead(true); ask(); });
    return () => clearTimeout(timer.current);
  }, [bid]);

  const st = identityOf(b);
  const loading = asks && (!!b.netIdentityLoading || !read);
  const refusal = b.netIdentityError && !st ? "" : identityRefusal(st, impl);
  const leaseWhy = holderOnly(bid, "Naming the board");
  const hubRec = (st && st.hub) || (p && p.hub && p.hub.record) || null;
  const fix = (st && st.fix) || {};
  const why = nameProblem(name);
  const errors = (p && p.errors) || {};
  const ipNow = p && p.address ? p.address.ip : "";
  const guard = !!(p && p.hub && p.hub.guard);
  const hubName = (p && p.hub && p.hub.name) || "";
  const outside = !!(p && p.address && p.address.subnet && p.address.subnet.same === false);
  const changes = mode === "hub" ? (fix.changes || []) : ((p && p.changes) || []);
  const phrase = mode === "hub" ? fix.phrase : (p ? p.phrase : "");
  const blocked = mode === "hub" ? !changes.length
    : (!!why || !p || asking || !changes.length || !!errors.mac || !!errors.ip
      || (guard && hubTyped.trim() !== hubName) || (outside && !otherSubnet));

  const apply = async () => {
    setBusy(true); setErr(null);
    let body;
    if (mode === "hub") body = { confirm: typed.trim(), from_hub: true };
    else {
      body = { confirm: typed.trim(), label: name.trim().toUpperCase() };
      const cur = (p && p.current) || {};
      if (p && p.mac && macMode !== "keep" && p.mac !== cur.mac) body.mac = p.mac;
      if (p && p.ip && ipMode !== "keep" && addr(p.ip) !== addr(cur.ip)) body.ip = p.ip;
      if (guard) body.hub_fixed = hubTyped.trim();
      if (outside) body.other_subnet = true;
    }
    try {
      const out = await runJob("netIdentityFix", { bid }, body, null, "identity");
      setDone(out || {});
      toast(`Named${body.label ? `: ${body.label}` : ""}`, { icon: "tag" });
      if (!(out && out.moved)) { loadIdentity(bid); refreshInfo(bid); }
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };

  const macOpts = [{ value: "random", label: "Random", title: "A random locally administered MAC (02:...)" },
    { value: "keep", label: "Keep", title: "The MAC the board has now" },
    { value: "custom", label: "Custom", title: "Your own: unicast, not 02:00:00:*" }];
  const ipOpts = [{ value: "auto", label: "Auto", title: `A free address of the pool, searched from the MAC${p && p.pool && p.pool.range ? ` (${p.pool.range})` : ""}` },
    { value: "keep", label: "Keep", title: "The IP the board has now" },
    { value: "custom", label: "Custom", title: "Your own: an IPv4 address of a /24" }];
  const cur = (p && p.current) || (st && st.reported) || {};
  const foot = html`<span class="grow"></span><button type="button" class="btn" data-action="nb-close"
    disabled=${busy} onClick=${close}>${done ? "Close" : "Cancel"}</button>`;

  return html`<${ModalShell} title="Name this board" icon="tag" cls="name-board" testid="name-board-modal"
      note=${(S.boards[bid] && S.boards[bid].candidate && S.boards[bid].candidate.name) || bid} foot=${foot}>
    <div class="stack gap-8 nb" data-testid="identity-dialog" data-mode=${mode}>
      <p class="small secondary">Give this board its own name, MAC and IP. They are set on the board; then the harness restarts (its reboot verb, warm: the FPGA is not reloaded, never an MCC REBOOT) and the identity is read back. It needs your lease and your claim, and waits for no card job.</p>
      ${loading ? html`<${Reason} icon="loader-circle" text="Reading what the board says it is…" />` : null}
      ${b.netIdentityError && !st ? html`<${Reason} level="err" testid="nb-read-error" text=${`Harness Manager cannot read this board's identity: ${b.netIdentityError.errName}: ${b.netIdentityError.message}`} />` : null}
      ${!loading && refusal ? html`<${Reason} level="warn" testid="identity-refusal" text=${refusal} />` : null}
      ${!loading && !refusal && st && !done ? html`<div class="stack gap-8">
        ${hubRec ? html`<${Seg} label="What to set" value=${mode} onChange=${(v) => { setMode(v); setTyped(""); }}
          options=${[{ value: "name", label: "Name it", icon: "tag" }, { value: "hub", label: `Match its hub entry (${hubRec.target || "hub"})`, icon: "server" }]} />` : null}
        ${mode === "hub" ? html`<div class="stack gap-8" data-testid="nb-hub-entry">
          <p class="small">Make this board match its hub entry <span class="mono">${hubRec ? hubRec.target : ""}</span>${hubRec && hubRec.label ? ` (${hubRec.label})` : ""}:</p>
          ${changes.length ? html`<${Changes} changes=${changes} />` : html`<${Reason} level="ok" testid="nb-hub-same" text="The board already matches its hub entry: nothing to set." />`}
          ${(fix.notes || []).map((n) => html`<${Reason} text=${n} />`)}
        </div>` : html`<div class="stack gap-8">
          <div class="nb-row"><label for=${`nb-name-${bid}`}>Name</label>
            <div class="nb-col">
              <div class="nb-inline"><input id=${`nb-name-${bid}`} class="input mono grow" data-testid="nb-name" autocomplete="off" spellcheck="false"
                maxlength="24" value=${name} placeholder="LAB-07"
                onInput=${(e) => { const v = e.target.value.toUpperCase(); setName(v); askSoon({ name: v }); }} />
                <span class=${`nb-count ${name.trim().length > NAME_MAX ? "over" : ""}`} data-testid="nb-name-count">${name.trim().length}/${NAME_MAX}</span></div>
              ${why && name.trim() ? html`<p class="small nb-why" data-testid="nb-name-why">${why[0].toUpperCase()}${why.slice(1)}. A name is 1-16 characters of A-Z, 0-9 and -.</p>`
                : !name.trim() ? html`<p class="small muted nb-why" data-testid="nb-name-rule">1-16 characters of A-Z, 0-9 and - (lower case is upper-cased).</p>`
                : html`<p class="small muted nb-why">On the panel's top row, and the board's host name: <span class="mono">${name.trim().toLowerCase()}</span>.</p>`}
            </div></div>
          <div class="nb-row"><label>MAC</label>
            <div class="nb-col">
              <div data-testid="nb-mac-mode"><${Seg} label="MAC" value=${macMode} options=${macOpts}
                onChange=${(v) => { setMacMode(v); ask({ macMode: v }); }} /></div>
              ${macMode === "random" ? html`<div class="nb-inline"><span class="mono" data-testid="nb-mac">${(p && p.mac) || macRandom || "…"}</span>
                <button type="button" class="btn ghost sm" data-action="nb-mac-regenerate" disabled=${asking}
                  onClick=${() => ask({ regen: true })}><${Icon} name="refresh-cw" cls="sm" /> Regenerate</button></div>` : null}
              ${macMode === "keep" ? html`<span class="mono" data-testid="nb-mac">${cur.mac || "-"}</span>` : null}
              ${macMode === "custom" ? html`<input class="input mono" data-testid="nb-mac-custom" autocomplete="off" spellcheck="false"
                placeholder="02:xx:xx:xx:xx:xx" value=${macCustom}
                onInput=${(e) => { setMacCustom(e.target.value); askSoon({ macCustom: e.target.value }); }} />` : null}
              ${errors.mac ? html`<p class="small nb-why" data-testid="nb-mac-why">${errors.mac}</p>` : null}
            </div></div>
          <div class="nb-row"><label>IP</label>
            <div class="nb-col">
              <div data-testid="nb-ip-mode"><${Seg} label="IP" value=${ipMode} options=${ipOpts}
                onChange=${(v) => { setIpMode(v); ask({ ipMode: v }); }} /></div>
              ${ipMode === "auto" && p && p.pool && p.pool.range ? html`<p class="small muted nb-why">A free address of ${p.pool.range}, searched from the MAC (its last byte picks the first one tried): not one given to another board here, and not one that answers now.</p>` : null}
              ${ipMode === "custom" ? html`<input class="input mono" data-testid="nb-ip-custom" autocomplete="off" spellcheck="false"
                placeholder="192.168.10.117" value=${ipCustom}
                onInput=${(e) => { setIpCustom(e.target.value); askSoon({ ipCustom: e.target.value }); }} />` : null}
              ${errors.ip ? html`<p class="small nb-why" data-testid="nb-ip-why">${errors.ip}</p>` : null}
            </div></div>
          ${ipNow && !errors.ip ? html`<div class="nb-ip" data-testid="nb-ip">
            <span class="nb-ip-label">${p && p.address.moving ? "After the restart the board answers at" : "The board's IP"}</span>
            <span class="nb-ip-value mono" data-testid="nb-ip-value">${ipNow}</span>
            <span class="small" data-testid="nb-ip-note">${p.address.same_net.charAt(0).toUpperCase()}${p.address.same_net.slice(1)}.</span>
          </div>` : null}
          ${outside ? html`<div class="stack gap-8" data-testid="nb-subnet">
            <${Reason} level="warn" text=${`${ipNow} is not on this PC's network (this PC is ${p.address.subnet.local} in ${p.address.subnet.network}): after the restart this PC cannot reach the board until it has an address in that /24 (e.g. ${p.address.pc_example}).`} />
            <label class="check-inline small"><input type="checkbox" data-testid="nb-other-subnet" checked=${otherSubnet}
              onChange=${(e) => setOtherSubnet(e.target.checked)} /> I will move this PC to that network: set it anyway</label></div>` : null}
          ${p && p.hub ? html`<div class="stack gap-8 nb-hub" data-testid="nb-hub">
            <p class="small">This board is behind the hub <b class="mono">${p.hub.name}</b> (${p.hub.target}), whose DHCP (dnsmasq) knows it by its MAC.</p>
            ${guard ? html`<${Reason} level="warn" testid="nb-hub-warning" text=${`Changing its ${p.changes.some((c) => c.field === "mac") ? "MAC" : "IP"} before the hub's record is fixed loses the board's address: fix ${p.hub.target}'s record on ${p.hub.name} first (fpgahub).${Object.keys(p.hub.known_bad || {}).length ? ` ${Object.entries(p.hub.known_bad).map(([t, w]) => `${t}'s hub record is known to be wrong today (${w})`).join("; ")}.` : ""}`} />
              <div class="field"><label for=${`nb-hub-${bid}`}>Type the hub's name, <code>${p.hub.name}</code>, to confirm its record is fixed</label>
                <input id=${`nb-hub-${bid}`} class="input mono grow" data-testid="nb-hub-fixed" autocomplete="off" value=${hubTyped}
                  onInput=${(e) => setHubTyped(e.target.value)} /></div>` : null}
          </div>` : null}
          ${(p && p.notes ? p.notes : []).filter((n) => n !== p.rescue_note).map((n) => html`<${Reason} level="warn" testid="nb-arp" text=${`${n.charAt(0).toUpperCase()}${n.slice(1)}.`} />`)}
          ${p && p.rescue_note ? html`<${Reason} icon="info" testid="nb-rescue" text=${`${p.rescue_note.charAt(0).toUpperCase()}${p.rescue_note.slice(1)}.`} />` : null}
          ${changes.length ? html`<${Changes} changes=${changes} />`
            : p && !why ? html`<${Reason} level="ok" testid="nb-same" text="The board already has this name, MAC and IP: nothing to set." />` : null}
        </div>`}
        ${pErr ? html`<${Reason} level="err" testid="nb-proposal-error" text=${`${pErr.errName}: ${pErr.message}`} />` : null}
        ${leaseWhy ? html`<${Reason} level="held" icon="lock" testid="nb-lease" text=${leaseWhy} />` : null}
        ${changes.length ? html`<div class="field"><label for=${`nb-phrase-${bid}`}>Type <code data-testid="nb-phrase-want">${phrase}</code> to confirm</label>
          <input id=${`nb-phrase-${bid}`} class="input mono grow" data-testid="identity-phrase" autocomplete="off" value=${typed}
            onInput=${(e) => setTyped(e.target.value)} /></div>
        <div class="row"><button type="button" class="btn primary sm" data-action="identity-fix-confirm"
          disabled=${busy || blocked || typed.trim() !== phrase || !!leaseWhy} onClick=${apply}>
          ${busy ? html`<${Spinner} />` : html`<${Icon} name="tag" />`} Set and restart</button>
          ${busy ? html`<span class="small muted">Setting, restarting the harness (warm), reading it back…</span>` : null}</div>` : null}
      </div>` : null}
      ${done ? html`<div class="stack gap-8" data-testid="nb-done">
        <${Reason} level="ok" text=${`Set${done.verified === false ? "" : " and read back"}: ${(done.changes || []).map((c) => `${c.field} ${c.to}`).join(", ")}.`} />
        ${done.address && done.address.ip ? html`<div class="nb-ip" data-testid="nb-ip">
          <span class="nb-ip-label">The board answers at</span>
          <span class="nb-ip-value mono" data-testid="nb-ip-value">${done.address.ip}</span>
          <span class="small">${done.address.same_net.charAt(0).toUpperCase()}${done.address.same_net.slice(1)}.</span></div>` : null}
        ${done.moved ? html`<div class="stack gap-8" data-testid="nb-moved">
          <${Reason} text=${`The board moved: it was found at ${done.moved.host} by its SSH host key, and its claim and settings now belong to ${done.moved.to}.`} />
          <div><button type="button" class="btn primary sm" data-action="nb-open-moved"
            onClick=${() => openMoved(bid, done.moved)}><${Icon} name="ethernet-port" /> Open it at ${done.moved.host}</button></div></div>` : null}
        ${(done.notes || []).map((n) => html`<${Reason} text=${`${n.charAt(0).toUpperCase()}${n.slice(1)}.`} />`)}
      </div>` : null}
      ${err ? html`<${Reason} level="err" testid="identity-fix-error" text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}`} />` : null}
    </div>
  <//>`;
}

registerModal("name-board", NameBoard);

// --- Board › Access (UI v2, lane UI2-BOARD): the identity card -------------------------------------
//
// What the board says it is (label, IP, MAC: net_identity when the harness reports one), the
// clashes G10 found across every board this Harness Manager has seen, "Name this board…" and,
// when the hub entry differs, "Fix identity…" (the same dialog, on its hub-entry mode). Not the
// same as Identify (the blink).

function ethAddress(b) {
  const links = ((b.info && b.info.candidate) || {}).links || [];
  const l = links.find((x) => x.kind === "ethernet");
  return l ? String(l.address).split(":")[0] : "";
}

export function IdentityCard({ bid, clashes = [], goTo = null }) {
  const b = boardState(bid);
  const st = identityOf(b);
  const has = !!(b.info && b.info.net_identity);
  useEffect(() => {
    if (has && !b.netIdentity && !b.netIdentityLoading && !b.netIdentityError) loadIdentity(bid);
  }, [bid, has]);
  const r = (st && st.reported) || {};
  const bare = !!(b.info && (b.info.identity || {}).harness_impl && b.info.identity.harness_impl !== "linux");
  const nothing = bare ? "not reported by the bare-metal harness" : "not reported";
  const problems = st ? problemText(st) : "";
  const changes = (st && st.fix && st.fix.changes) || [];
  const level = st && st.level === "err" ? "err" : st && st.level === "warn" ? "warn" : "";
  return html`<${Card} title="Identity" icon="tag" testid="access-identity"
      sub="Who this board says it is: its label, address and MAC. Not the same as Identify (the blink).">
    <div class="stack gap-8" data-status=${(st && st.status) || "none"}>
      <dl class="kv">
        <dt>Label</dt><dd>${r.label || html`<span class="muted">${nothing}</span>`}${r.source === "default" ? html` <span class="sub">image default</span>` : null}</dd>
        <dt>IP</dt><dd class="mono">${r.ip || ethAddress(b) || html`<span class="muted">${nothing}</span>`}</dd>
        <dt>MAC</dt><dd class="mono">${r.mac || html`<span class="muted" style="font-family:var(--font-sans)">${nothing}</span>`}</dd>
        ${st && st.hub && st.hub.target ? html`<dt>Hub entry</dt><dd class="small"><span class="mono">${st.hub.target}</span>${st.hub.label ? ` · ${st.hub.label}` : ""}${st.hub.board_ip ? ` · ${st.hub.board_ip}` : ""}</dd>` : null}
        ${st && !st.live && st.checked_at ? html`<dt>Checked</dt><dd class="small muted">${st.checked_at} (the last check)</dd>` : null}
      </dl>
      ${clashes.map((c) => {
        const others = (c.boards || []).filter((x) => x.board_id !== bid);
        const name = others.map((x) => x.name || x.target || x.board_id).join(", ");
        const go = others.find((x) => x.board_id && x.kind === "board");
        return html`<div class="stack gap-8" key=${`${c.field}:${c.value}`} data-testid="access-clash" data-field=${c.field}>
          <${Reason} level="err" text=${`Identity clash with ${name}: the same ${c.field === "mac" ? "MAC" : c.field === "ip" ? "IP" : "label"} ${c.value}.`} />
          ${!changes.length && go && goTo ? html`<div><button type="button" class="btn sm" data-action="access-go-clash"
            onClick=${() => goTo(go.board_id)}>Go to ${go.name || go.board_id}</button></div>` : null}</div>`;
      })}
      ${problems ? html`<${Reason} level=${level} testid="access-identity-warning" text=${problems} />` : null}
      ${has && !bare ? html`<div class="row gap-8">
        ${changes.length ? html`<button type="button" class="btn sm primary" data-action="access-identity-fix"
          onClick=${() => openModal("name-board", { bid, hub: true })}><${Icon} name="server" /> Fix identity…</button>` : null}
        <button type="button" class=${`btn sm ${changes.length ? "" : "primary"}`} data-action="access-identity-name"
          onClick=${() => openModal("name-board", { bid })}><${Icon} name="tag" /> Name this board…</button></div>` : null}
      ${b.netIdentityError ? html`<${Reason} level="err" text=${`${b.netIdentityError.errName}: ${b.netIdentityError.message}`} />` : null}
      ${!has && b.info && !bare ? html`<p class="small muted">This harness image does not report its identity (net-protocol v0.16 adds it).</p>` : null}
    </div><//>`;
}
