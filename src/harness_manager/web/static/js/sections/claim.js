// The SSH claim (lane LINUX-CLAIM; docs/API.md "SSH claim of a Linux harness"), on Board >
// Access. Only a Linux harness has one: info.claim is absent on bare metal, so nothing shows.
// "Claim with my key…" asks once more before it posts {confirm: true}: a claim gives your key
// root on the board, and the board refuses every later one. "Adopt this board" (a board claimed
// by a key this PC holds, with no record here) and "Keys" (who else may log in: add a pasted
// public key, remove one by fingerprint) are the CLI's `board claim --adopt` and `board keys`.

import { gateReason, interlock, isArmed, runJob, setArmed } from "../actions.js";
import { call, toApiError } from "../api.js";
import { html, useEffect, useState } from "../lib.js";
import { boardState, refreshInfo } from "../store.js";
import { ArmBox, Card, Chip, Icon, Reason, Seg, Spinner } from "../ui.js";
import { ModalShell, openModal, registerModal, setModalBusy } from "../modal.js";
import { LEASE_WORDS as W } from "../lease_words.js";
import { leaseBoardName, forceName, nameMatches } from "../lease.js";
import { leaseWho, loadHub, loadLeaseMode, week } from "../week.js";
import { clearNamePrefill, hasNamePrefill, namePrefillArgs } from "../name_prefill.js";

// The daemon stamps times in UTC ("2026-10-13T09:30:00Z"); people read their own clock.
export function localStamp(iso) {
  const d = new Date(String(iso));
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// --- Board › Access (UI v2, lane UI2-BOARD): the SSH claim as its own card -----------------------


export function claimOf(b) { return (b && b.info && b.info.claim) || null; }

// The board's SSH link ("root@192.168.10.104"), as its candidate names it.
function sshAddress(b) {
  const links = ((b.info && b.info.candidate) || {}).links || [];
  const l = links.find((x) => x.kind === "ssh");
  return l ? l.address : "";
}

// A board behind a hub: identify (UDP) does not cross the SSH tunnel, so info only knows the last
// check. The card asks the hub itself when it loads (GET claim?hub=true: identify run on the hub
// over ssh; never the lease, never a board port; the service reuses an answer of the last
// minute), at most once per HUB_AUTO_MS per board, and "Check through the hub" asks now.
const HUB_AUTO_MS = 5 * 60 * 1000;
const hubAsked = new Map();

export function hubRouted(c) { return !!c && String(c.route || "").startsWith("hub "); }

// Only a Linux harness has a claim (info.claim is absent on bare metal): nothing shows then.
// The SSH claim card, then "Who can use this board" (the board's single-user mode).
export function ClaimCard({ bid }) {
  return html`<${SshClaimCard} bid=${bid} /><${SingleUserCard} bid=${bid} />`;
}

function SshClaimCard({ bid }) {
  const b = boardState(bid);
  const c = claimOf(b);
  const [armed, setClaimArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  // The fingerprint on screen when "Re-pin…" was pressed: that exact key is what gets pinned.
  const [approved, setApproved] = useState("");
  // Replace the pinned key (a board claimed before and re-imaged): the fingerprint on screen
  // when it was pressed; the claim pins exactly it or nothing.
  const [replacing, setReplacing] = useState("");
  const [checking, setChecking] = useState(false);
  // Adopt: the Arm box (armKey "adopt") then the button; the same checks as `board claim --adopt`.
  const [adopting, setAdopting] = useState(false);
  const viaHub = hubRouted(c);
  // now: the button (always asks); else the automatic check on load (the service's cache)
  const checkHub = async (now) => {
    hubAsked.set(bid, Date.now());
    setChecking(true); setErr(null);
    try {
      await call("claimStatus", { bid }, undefined, now ? { refresh: "true" } : { hub: "true" });
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setChecking(false);
      refreshInfo(bid);
    }
  };
  useEffect(() => {
    if (!viaHub || c.live || checking) return;
    if (Date.now() - (hubAsked.get(bid) || 0) < HUB_AUTO_MS) return;
    checkHub(false);
  }, [bid, viaHub, c && c.live, b.infoGen]);
  if (!c) return null;
  const who = c.claimed || {};
  const changedKey = c.host_key && c.host_key.match === false;
  const seenBefore = changedKey && c.host_key.seen_before ? String(c.host_key.seen_before).slice(0, 10) : "";
  const hk = c.host_key || {};
  const repinArmed = !!approved;
  const moved = repinArmed && approved !== hk.reported;       // the board's key changed again
  const repinOk = isArmed(bid, "repin") && !moved;
  // The key the board shows must be known to be re-pinned: the button pins exactly that one.
  const canRepin = changedKey && !!hk.reported && c.state !== "unclaimed";
  // An UNCLAIMED board whose key differs from the one pinned before was re-imaged: the claim is
  // refused until the old pin is replaced on purpose.
  const canReplace = changedKey && !!hk.pinned && !!hk.reported && c.state === "unclaimed";
  const replaceMoved = !!replacing && replacing !== hk.reported;
  const replaceOk = isArmed(bid, "replace") && !replaceMoved;
  // R3: a claim needs this board's lease on a hub board (the service says 409 HELD too).
  const why = gateReason(bid, "claim", "claim", { holder: "SSH claim" });
  const claim = async () => {
    if (why) { interlock(bid, "claim", "board claim", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      await runJob("claim", { bid }, { confirm: true }, null, "claim");
      setClaimArmed(false);
      refreshInfo(bid);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  // Re-pin: the Arm box (a check box) and the button post the exact fingerprint shown above.
  const repin = async () => {
    if (why) { interlock(bid, "claim", "board re-pin", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      await runJob("repin", { bid }, { confirm: true, fingerprint: approved }, null, "repin");
      setApproved(""); setArmed(bid, "repin", false);
      refreshInfo(bid);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  // Same as `board claim --replace-host-key`, with the shown fingerprint named: never automatic.
  const replace = async () => {
    if (why) { interlock(bid, "claim", "board claim --replace-host-key", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      await runJob("claim", { bid }, { confirm: true, replace_host_key: true, expect_host_key: replacing }, null, "claim");
      setReplacing(""); setArmed(bid, "replace", false);
      refreshInfo(bid);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  const adopt = async () => {
    if (why) { interlock(bid, "claim", "board adopt", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      await runJob("claim", { bid }, { confirm: true, adopt: true }, null, "claim");
      setAdopting(false); setArmed(bid, "adopt", false);
      refreshInfo(bid);
    } catch (e) {
      setErr(toApiError(e));      // plain words from the daemon: key refused, host key moved, ...
    } finally {
      setBusy(false);
    }
  };
  // A board claimed by a key this PC may hold, with no record here: adopting logs in with this
  // PC's key and pins the board's host key. A host key that changed to one never seen needs a
  // claim with the replace option instead.
  const canAdopt = c.state === "other" && (!changedKey || !!seenBefore);
  const ssh = sshAddress(b);
  return html`<${Card} title="SSH claim" icon="lock" testid="access-claim"
      sub="Who may log in to the board's Linux over SSH. Once claimed, slot and card changes are accepted only through it.">
    <div class="stack gap-8" data-claim=${c.state}>
      <dl class="kv">
        <dt>State</dt><dd>${c.state === "mine" ? html`<${Chip} level="ok" icon="lock">Claimed by you<//>`
          : c.state === "other" ? html`<${Chip} level="held" icon="lock">Claimed by another key<//>`
          : c.state === "unclaimed" ? html`<${Chip} level="warn" icon="lock-open">Not claimed<//>`
          : html`<${Chip} level="unk" icon="circle-help">Unknown<//>`}
          ${!c.live && c.checked_at && !(viaHub && c.state === "unknown") ? html` <span class="muted small">last check ${localStamp(c.checked_at)}</span>` : null}
          ${checking ? html` <span class="muted small" data-testid="claim-hub-checking"><${Spinner} /> asking the hub…</span>` : null}</dd>
        ${who.key_fp ? html`<dt>Key</dt><dd class="mono small">${who.key_fp}</dd>` : null}
        ${who.by ? html`<dt>Claimed by</dt><dd class="small">${who.by}${who.at ? ` · ${localStamp(who.at)}` : ""}</dd>` : null}
        ${ssh ? html`<dt>Log in</dt><dd class="mono small">${ssh}</dd>` : null}
      </dl>
      ${changedKey ? html`<${Reason} level=${seenBefore ? "warn" : "err"} testid="claim-hostkey"
        text=${hk.refusal && hk.refusal.message ? `${hk.refusal.message}.`
          : seenBefore ? `Host key changed back to one seen on ${seenBefore} (${hk.reported}); on the Linux harness this is usually /persist (the user microSD) mounting or not. SSH is refused until you re-pin it: ${c.state === "unclaimed" ? "claim the board again" : "use Adopt this board below"}, if you trust it.`
          : `Host key changed: pinned ${hk.pinned}, the board reports ${hk.reported}. SSH is refused.`} />` : null}
      ${changedKey ? html`<div class="stack gap-8" data-testid="claim-hostkey-box" data-repin=${canRepin ? "yes" : "no"}>
        <dl class="kv" data-testid="claim-hostkey-keys">
          <dt>Pinned key</dt><dd class="mono small" data-testid="hostkey-old">${hk.pinned}${hk.pinned_at ? ` · pinned ${localStamp(hk.pinned_at)}` : ""}</dd>
          <dt>Key the board shows now</dt><dd class="mono small" data-testid="hostkey-new">${hk.reported || "not published"}</dd>
          ${hk.boot_id ? html`<dt>Board boot</dt><dd class="mono small" data-testid="hostkey-boot">${hk.boot_id}${hk.up_s !== null && hk.up_s !== undefined ? ` · up ${Math.round(hk.up_s)} s` : ""}</dd>` : null}
        </dl>
        ${canReplace ? html`<div class="bt-foot" data-testid="claim-replace-box">
          ${replacing ? html`${replaceMoved ? html`<${Reason} level="err" testid="replace-moved"
                text=${`The board's key changed again: you approved ${replacing}, it shows ${hk.reported || "no key"} now. Cancel and read the new one.`} />` : null}
              <${Reason} text="This board was claimed here before and shows a different key now (a new card or image). Only replace the pinned key if you expect that and the new key above is the one on the board's console. Harness Manager then pins exactly that key and claims the board with your key." />
              <${ArmBox} bid=${bid} armKey="replace" testid="arm-replace"
                text="Arm: I checked the new key above is the one on the board, and I want to replace the pinned key." />
              <button type="button" class="btn sm primary" data-action="access-replace-confirm" disabled=${busy || !replaceOk} onClick=${replace}
                aria-disabled=${why ? "true" : undefined}>${busy ? html`<${Spinner} />` : null} Replace the pinned key and claim</button>
              <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => { setReplacing(""); setArmed(bid, "replace", false); }}>Cancel</button>`
            : html`<button type="button" class="btn sm" data-action="access-replace" aria-disabled=${why ? "true" : undefined}
                title=${why || "Pin the key the board shows now, in place of the old one, and claim"}
                onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board claim --replace-host-key", why); return; } setReplacing(hk.reported); }}>
                <${Icon} name="lock" /> Replace the pinned key…</button>`}
          ${why ? html`<span class="small muted">${why}</span>` : null}</div>` : null}
        ${canRepin ? html`<div class="bt-foot">
          ${repinArmed ? html`${moved ? html`<${Reason} level="err" testid="repin-moved"
                text=${`The board's key changed again: you approved ${approved}, it shows ${hk.reported || "no key"} now. Cancel and read the new one.`} />` : null}
              <${Reason} text="Only re-pin if you expect a new key (a new card or image, or a netboot) and the new key is the one on the board's console. Harness Manager then trusts exactly that key for this board's root SSH." />
              <${ArmBox} bid=${bid} armKey="repin" testid="arm-repin"
                text="Arm: I checked the new key above is the one on the board." />
              <button type="button" class="btn sm primary" data-action="access-repin-confirm" disabled=${busy || !repinOk} onClick=${repin}
                aria-disabled=${why ? "true" : undefined}>${busy ? html`<${Spinner} />` : null} Re-pin to the new key</button>
              <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => { setApproved(""); setArmed(bid, "repin", false); }}>Cancel</button>`
            : html`<button type="button" class="btn sm" data-action="access-repin" aria-disabled=${why ? "true" : undefined}
                title=${why || "Pin the key the board shows now"}
                onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board re-pin", why); return; } setApproved(hk.reported); }}>
                <${Icon} name="lock" /> Re-pin…</button>`}
          ${why ? html`<span class="small muted" data-testid="reason-access-repin">${why}</span>` : null}</div>` : null}
      </div>` : null}
      ${c.state === "unclaimed" ? html`<${Reason} level="warn" text="Not claimed: anyone with the image's default key can log in." />` : null}
      ${c.lost ? html`<${Reason} level="warn" text=${c.lost.message} />` : null}
      ${c.state === "mine" && hasNamePrefill(bid) ? html`<div class="bt-foot" data-testid="claim-then-name">
        <${Reason} level="ok" text="Claimed. Next, name this board: the wizard's proposal is ready." />
        <button type="button" class="btn sm primary" data-action="access-name-after-claim"
          onClick=${() => { const args = namePrefillArgs(bid); clearNamePrefill(bid); openModal("name-board", { bid, ...args }); }}>
          <${Icon} name="tag" /> Name this board…</button></div>` : null}
      ${(c.notes || []).filter((n) => !(canAdopt && /board claim TARGET --adopt/.test(n))).map((n) => html`<${Reason} key=${n} text=${n} />`)}
      ${canAdopt ? html`<div class="bt-foot" data-testid="claim-adopt-box">
        <${Reason} text="This board was claimed by a key this Harness Manager has no record of. If that key is on this PC (a claim made from here before, or with pyverify), adopt it: Harness Manager logs in with your key, checks the board's host key against what the board reports, and remembers it. Nothing on the board changes." />
        ${adopting ? html`<${ArmBox} bid=${bid} armKey="adopt" testid="arm-adopt"
              text="Arm: this PC holds the key that claimed this board." />
            <button type="button" class="btn sm primary" data-action="access-adopt-confirm" disabled=${busy || !isArmed(bid, "adopt")} onClick=${adopt}
              aria-disabled=${why ? "true" : undefined}>${busy ? html`<${Spinner} />` : null} Adopt this board</button>
            <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => { setAdopting(false); setArmed(bid, "adopt", false); }}>Cancel</button>`
          : html`<button type="button" class="btn sm" data-action="access-adopt" aria-disabled=${why ? "true" : undefined}
              title=${why || "Log in with this PC's key and remember the board's host key"}
              onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board adopt", why); return; } setAdopting(true); }}>
              <${Icon} name="lock" /> Adopt this board…</button>`}
        ${why ? html`<span class="small muted" data-testid="reason-access-adopt">${why}</span>` : null}</div>` : null}
      ${c.state === "mine" ? html`<${KeysBlock} bid=${bid} why=${why} />` : null}
      ${c.state === "unclaimed" && !(changedKey && !seenBefore) ? html`<div class="bt-foot">
        ${armed ? html`<${Reason} text="Your SSH key gets root on this board; the board then refuses every other key's claim and takes slot changes only over that key's SSH." />
            <button type="button" class="btn sm primary" data-action="access-claim-confirm" disabled=${busy} onClick=${claim}
              aria-disabled=${why ? "true" : undefined}>${busy ? html`<${Spinner} />` : null} Claim with my key</button>
            <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => setClaimArmed(false)}>Cancel</button>`
          : html`<button type="button" class="btn sm" data-action="access-claim" aria-disabled=${why ? "true" : undefined}
              title=${why || "Claim the board's SSH with your key"}
              onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board claim", why); return; } setClaimArmed(true); }}>
              <${Icon} name="lock" /> Claim with my key…</button>`}
        ${why ? html`<span class="small muted" data-testid="reason-access-claim">${why}</span>` : null}</div>` : null}
      ${viaHub && !c.live ? html`<div class="bt-foot" data-testid="claim-hub-check">
        ${c.state === "unknown" && !checking ? html`<${Reason} text="The claim could not be read through the hub. The board's identify does not cross the SSH tunnel, so Harness Manager asks the hub to run it." />` : null}
        <button type="button" class="btn sm" data-action="access-claim-hub-check" disabled=${checking}
          title="Ask the hub now whether the board is claimed (no lease needed; nothing changes on the board)"
          onClick=${() => checkHub(true)}>${checking ? html`<${Spinner} />` : html`<${Icon} name="server" />`} Check through the hub</button></div>` : null}
      ${err ? html`<${Reason} level="err" testid="access-claim-error"
        text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}${err.holder ? ` (holder: ${err.holder})` : ""}`} />` : null}
    </div><//>`;
}


// --- who else may log in: the board's keys (the CLI's `board keys`) -----------------------------
// Fingerprints and labels only. A pasted key is checked on the daemon (a private key is refused
// there, with nothing kept) and its fingerprint shown before "Add this key"; the text is dropped
// from the page as soon as it is sent or cancelled.

function KeysBlock({ bid, why }) {
  const [keys, setKeys] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");
  const [label, setLabel] = useState("");
  const [seen, setSeen] = useState(null);          // {type, fingerprint, label} from the check
  const [removing, setRemoving] = useState("");    // fingerprint on screen when Remove was pressed
  const [note, setNote] = useState("");
  const stop = () => { setAdding(false); setText(""); setLabel(""); setSeen(null); setArmed(bid, "addkey", false); };
  const load = async () => {
    setLoading(true); setErr(null);
    try {
      const { data } = await call("claimKeys", { bid });
      setKeys(data.keys || []);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setLoading(false);
    }
  };
  const check = async () => {
    setBusy(true); setErr(null); setSeen(null); setNote("");
    try {
      const { data } = await call("claimKeyCheck", { bid }, { key: text, label });
      setSeen(data.key);
    } catch (e) {
      setErr(toApiError(e));
      if (/private key/i.test(String(e && e.message))) setText("");     // never keep it on screen
    } finally {
      setBusy(false);
    }
  };
  const add = async () => {
    if (why) { interlock(bid, "claim", "board keys add", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      const res = await runJob("claimKeyAdd", { bid }, { confirm: true, key: text, label, expect_fingerprint: seen.fingerprint }, null, "claim_key");
      const k = (res && res.key) || {};
      setNote(k.action === "already_there" ? `That key is already there (${seen.fingerprint}).` : `Added ${seen.fingerprint}.`);
      setKeys(k.keys || keys);
      stop();
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (why) { interlock(bid, "claim", "board keys remove", why); setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); return; }
    setBusy(true); setErr(null);
    try {
      const res = await runJob("claimKeyRemove", { bid }, { confirm: true, fingerprint: removing }, null, "claim_key");
      setNote(`Removed ${removing}.`);
      setKeys(((res && res.key) || {}).keys || keys);
      setRemoving(""); setArmed(bid, "rmkey", false);
    } catch (e) {
      setErr(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="stack gap-8" data-testid="claim-keys">
    <div class="bt-foot">
      <button type="button" class="btn sm" data-action="access-keys-load" disabled=${loading} onClick=${load}>
        ${loading ? html`<${Spinner} />` : html`<${Icon} name="lock" />`} ${keys ? "Refresh keys" : "Show who can log in"}</button>
      ${!adding ? html`<button type="button" class="btn sm" data-action="access-keys-add" aria-disabled=${why ? "true" : undefined}
          title=${why || "Let another person or PC log in: paste their public key"}
          onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board keys add", why); return; } setAdding(true); }}>
          <${Icon} name="lock-open" /> Add a key…</button>` : null}
      ${why ? html`<span class="small muted">${why}</span>` : null}</div>
    ${keys ? html`<ul class="stack gap-4" data-testid="claim-keys-list">
      ${keys.length === 0 ? html`<li class="muted small">The board lists no keys.</li>` : null}
      ${keys.map((k) => html`<li key=${k.fingerprint} class="small">
        <span class="mono">${k.fingerprint}</span> · ${k.label || "no label"}${k.mine ? " · this PC's key" : ""}
        ${k.mine ? null : html` <button type="button" class="btn ghost sm" data-action="access-key-remove" disabled=${busy}
          onClick=${() => { setRemoving(k.fingerprint); setNote(""); }}>Remove…</button>`}</li>`)}</ul>` : null}
    ${removing ? html`<div class="bt-foot" data-testid="claim-key-remove-box">
      <${Reason} text=${`Remove ${removing}? Whoever uses that key can no longer log in to this board. Your own key is never removed.`} />
      <${ArmBox} bid=${bid} armKey="rmkey" testid="arm-rmkey" text="Arm: remove this key." />
      <button type="button" class="btn sm primary" data-action="access-key-remove-confirm" disabled=${busy || !isArmed(bid, "rmkey")} onClick=${remove}>
        ${busy ? html`<${Spinner} />` : null} Remove this key</button>
      <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => { setRemoving(""); setArmed(bid, "rmkey", false); }}>Cancel</button></div>` : null}
    ${adding ? html`<div class="stack gap-8" data-testid="claim-key-add-box">
      <${Reason} text="Paste the other person's PUBLIC key: one line from their .pub file, starting ssh-ed25519 or ssh-rsa. Never a private key." />
      <textarea class="mono small" rows="3" aria-label="Public key" data-testid="key-text" value=${text} disabled=${busy}
        onInput=${(e) => { setText(e.target.value); setSeen(null); }}></textarea>
      <input type="text" aria-label="Label for this key" placeholder="Label, for example Sam's laptop" data-testid="key-label" value=${label} disabled=${busy}
        onInput=${(e) => { setLabel(e.target.value); setSeen(null); }} />
      ${seen ? html`<${Reason} level="ok" testid="key-fingerprint" text=${`This is ${seen.type}, ${seen.fingerprint}, labelled "${seen.label}". It will get root on the board.`} />
        <${ArmBox} bid=${bid} armKey="addkey" testid="arm-addkey" text="Arm: I checked this fingerprint with the key's owner." />` : null}
      <div class="bt-foot">
        ${!seen ? html`<button type="button" class="btn sm" data-action="access-key-check" disabled=${busy || !text.trim()} onClick=${check}>
            ${busy ? html`<${Spinner} />` : null} Check the key</button>`
          : html`<button type="button" class="btn sm primary" data-action="access-key-add-confirm" disabled=${busy || !isArmed(bid, "addkey")} onClick=${add}>
            ${busy ? html`<${Spinner} />` : null} Add this key</button>`}
        <button type="button" class="btn ghost sm" disabled=${busy} onClick=${stop}>Cancel</button></div></div>` : null}
    ${note ? html`<${Reason} level="ok" testid="claim-keys-note" text=${note} />` : null}
    ${err ? html`<${Reason} level="err" testid="claim-keys-error"
      text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}`} />` : null}
  </div>`;
}


// --- Board > Access: "Who can use this board" (single-user mode; the board's own lease) -------
//
// The board's state, not a setting: every PC sees the same. ON takes the lease in the same step
// (owner only); OFF is for the owner or the holder, and an owner switching off while someone
// else holds it types the board's name first.

export function modeLock(b, m, hubBehind) {
  // The reason the control is off, "" when it can be used.
  const c = claimOf(b);
  if (hubBehind) return W.behindHub;
  if (!m.available) return W.noFeature;
  if (c && c.state === "unclaimed") return W.unclaimed;
  if (!m.single_user && m.local_hold_s > 0) return W.localHold(Math.max(1, Math.ceil(m.local_hold_s / 60)));
  if (!m.can_change) return m.change_reason || (m.single_user ? W.notOwnerOff : W.notOwner);
  return "";
}

function SingleUserCard({ bid }) {
  const b = boardState(bid);
  const c = claimOf(b);
  const w = week(bid);
  useEffect(() => { loadLeaseMode(bid); }, [bid, c && c.state]);
  if (!c || w.leaseModeUnsupported) return null;
  const m = w.leaseMode;
  const hubBehind = hubRouted(c) || !!(w.hub && w.hub.server && w.hub.server.kind === "hub");
  if (!m) {
    return html`<${Card} title=${W.cardTitle} icon="lock" testid="access-single-user" sub=${W.cardSub}>
      ${w.leaseModeError ? html`<${Reason} level="err" text=${w.leaseModeError} />` : html`<${Reason} text=${W.reading} />`}<//>`;
  }
  const on = !!m.single_user;
  const lock = modeLock(b, m, hubBehind);
  const pick = (v) => {
    if (lock || (v === "one") === on) return;
    openModal("lease_mode", { bid, on: v === "one" });
  };
  const state = on ? (m.by ? W.stateOneBy(m.by) : W.stateOne) : W.stateShared;
  return html`<${Card} title=${W.cardTitle} icon="lock" testid="access-single-user" sub=${W.cardSub}>
    <div class="stack gap-8" data-single-user=${on ? "on" : "off"}>
      <div aria-disabled=${lock ? "true" : undefined} title=${lock || undefined}
        style=${lock ? "opacity:.6" : undefined} data-testid="single-user-seg">
        <${Seg} label=${W.segLabel} value=${on ? "one" : "shared"} onChange=${pick}
          options=${[{ value: "shared", label: W.segShared }, { value: "one", label: W.segOne }]} />
      </div>
      <p class="small secondary" data-testid="single-user-state">${state}</p>
      <p class="small secondary" data-testid="single-user-help">${W.help}</p>
      ${on ? html`<${Reason} icon="info" testid="single-user-courtesy" text=${m.label || W.courtesy} />` : null}
      ${lock ? html`<${Reason} level="warn" testid="single-user-why" text=${lock} />` : null}
    </div><//>`;
}

function LeaseModeDialog({ bid, on, close }) {
  const board = leaseBoardName(bid);
  const m = (week(bid).leaseMode) || {};
  const who = leaseWho(bid);
  const other = !on && (who.state === "other" || who.state === "elsewhere");
  const typeName = forceName(bid);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const ready = !other || nameMatches(typed, typeName);
  const go = async () => {
    if (busy || !ready) return;
    setBusy(true); setModalBusy(true); setErr("");
    try {
      const body = { single_user: on, confirm: true };
      if (other) body.confirm_board = typed.trim();
      await runJob("leaseModeSet", { bid }, body, null, "lease_mode");
      await loadLeaseMode(bid);
      loadHub(bid);
      setModalBusy(false);
      close();
    } catch (e) {
      const ae = toApiError(e);
      setErr(W.failed(ae.message || ae.errName));
      setBusy(false); setModalBusy(false);
    }
  };
  const title = on ? W.onTitle(board) : other ? W.offForceTitle(board) : W.offTitle(board);
  const holder = who.holder || "someone else";
  return html`<${ModalShell} title=${title} icon=${other ? "triangle-alert" : "lock"} cls="small" testid="lease-mode-confirm"
      foot=${html`<button type="button" class="btn" data-action="lease_mode_cancel" disabled=${busy} onClick=${close}>${W.cancel}</button>
        <button type="button" class=${`btn ${other ? "danger-solid" : "primary"}`} data-action="lease_mode_confirm"
          disabled=${busy || !ready} aria-busy=${busy ? "true" : undefined} onClick=${go}>
          ${busy ? html`<${Spinner} /> ${W.working}` : on ? W.onButton : other ? W.offForceButton : W.offButton}</button>`}>
    <p data-testid="lease-mode-what">${on ? W.onBody(`${m.ttl_min || 15} min`) : other ? W.offForceBody(holder, board) : W.offBody}</p>
    ${other ? html`<div><label class="field-label" for="lease-mode-name">${W.offForceLabel}</label>
      <input id="lease-mode-name" class="input mono force-name" type="text" autocomplete="off" spellcheck="false"
        data-testid="lease-mode-name" data-autofocus value=${typed}
        onInput=${(e) => setTyped(e.target.value)}
        onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); go(); } }} /></div>` : null}
    ${err ? html`<${Reason} level="err" text=${err} testid="lease-mode-error" />` : null}
  <//>`;
}
registerModal("lease_mode", LeaseModeDialog);
