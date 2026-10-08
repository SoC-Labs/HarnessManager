// The SSH claim (lane LINUX-CLAIM; docs/API.md "SSH claim of a Linux harness"), on Board >
// Access. Only a Linux harness has one: info.claim is absent on bare metal, so nothing shows.
// "Claim with my key…" asks once more before it posts {confirm: true}: a claim gives your key
// root on the board, and the board refuses every later one.

import { gateReason, interlock, isArmed, runJob, setArmed } from "../actions.js";
import { toApiError } from "../api.js";
import { html, useState } from "../lib.js";
import { boardState, refreshInfo } from "../store.js";
import { ArmBox, Card, Chip, Icon, Reason, Spinner } from "../ui.js";
import { openModal } from "../modal.js";
import { clearNamePrefill, hasNamePrefill, namePrefillArgs } from "../name_prefill.js";

// --- Board › Access (UI v2, lane UI2-BOARD): the SSH claim as its own card -----------------------


export function claimOf(b) { return (b && b.info && b.info.claim) || null; }

// The board's SSH link ("root@192.168.10.104"), as its candidate names it.
function sshAddress(b) {
  const links = ((b.info && b.info.candidate) || {}).links || [];
  const l = links.find((x) => x.kind === "ssh");
  return l ? l.address : "";
}

// Only a Linux harness has a claim (info.claim is absent on bare metal): nothing shows then.
export function ClaimCard({ bid }) {
  const b = boardState(bid);
  const c = claimOf(b);
  const [armed, setClaimArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  // The fingerprint on screen when "Re-pin…" was pressed: that exact key is what gets pinned.
  const [approved, setApproved] = useState("");
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
  const ssh = sshAddress(b);
  return html`<${Card} title="SSH claim" icon="lock" testid="access-claim"
      sub="Who may log in to the board's Linux over SSH. Once claimed, slot and card changes are accepted only through it.">
    <div class="stack gap-8" data-claim=${c.state}>
      <dl class="kv">
        <dt>State</dt><dd>${c.state === "mine" ? html`<${Chip} level="ok" icon="lock">Claimed by you<//>`
          : c.state === "other" ? html`<${Chip} level="held" icon="lock">Claimed by another key<//>`
          : c.state === "unclaimed" ? html`<${Chip} level="warn" icon="lock-open">Not claimed<//>`
          : html`<${Chip} level="unk" icon="circle-help">Unknown<//>`}
          ${!c.live && c.checked_at ? html` <span class="muted small">last check ${c.checked_at}</span>` : null}</dd>
        ${who.key_fp ? html`<dt>Key</dt><dd class="mono small">${who.key_fp}</dd>` : null}
        ${who.by ? html`<dt>Claimed by</dt><dd class="small">${who.by}${who.at ? ` · ${String(who.at).slice(0, 16).replace("T", " ")}` : ""}</dd>` : null}
        ${ssh ? html`<dt>Log in</dt><dd class="mono small">${ssh}</dd>` : null}
      </dl>
      ${changedKey ? html`<${Reason} level=${seenBefore ? "warn" : "err"} testid="claim-hostkey"
        text=${hk.refusal && hk.refusal.message ? `${hk.refusal.message}.`
          : seenBefore ? `Host key changed back to one seen on ${seenBefore} (${hk.reported}); on the Linux harness this is usually /persist (the user microSD) mounting or not. SSH is refused until you re-pin it: ${c.state === "unclaimed" ? "board claim" : "board claim --adopt"}, if you trust it.`
          : `Host key changed: pinned ${hk.pinned}, the board reports ${hk.reported}. SSH is refused.`} />` : null}
      ${changedKey ? html`<div class="stack gap-8" data-testid="claim-hostkey-box" data-repin=${canRepin ? "yes" : "no"}>
        <dl class="kv" data-testid="claim-hostkey-keys">
          <dt>Pinned key</dt><dd class="mono small" data-testid="hostkey-old">${hk.pinned}${hk.pinned_at ? ` · pinned ${String(hk.pinned_at).slice(0, 16).replace("T", " ")}` : ""}</dd>
          <dt>Key the board shows now</dt><dd class="mono small" data-testid="hostkey-new">${hk.reported || "not published"}</dd>
          ${hk.boot_id ? html`<dt>Board boot</dt><dd class="mono small" data-testid="hostkey-boot">${hk.boot_id}${hk.up_s !== null && hk.up_s !== undefined ? ` · up ${Math.round(hk.up_s)} s` : ""}</dd>` : null}
        </dl>
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
      ${c.state === "mine" && hasNamePrefill(bid) ? html`<div class="bt-foot" data-testid="claim-then-name">
        <${Reason} level="ok" text="Claimed. Next, name this board: the wizard's proposal is ready." />
        <button type="button" class="btn sm primary" data-action="access-name-after-claim"
          onClick=${() => { const args = namePrefillArgs(bid); clearNamePrefill(bid); openModal("name-board", { bid, ...args }); }}>
          <${Icon} name="tag" /> Name this board…</button></div>` : null}
      ${(c.notes || []).map((n) => html`<${Reason} key=${n} text=${n} />`)}
      ${c.state === "unclaimed" ? html`<div class="bt-foot">
        ${armed ? html`<${Reason} text="Your SSH key gets root on this board; the board then refuses every other key's claim and takes slot changes only over that key's SSH." />
            <button type="button" class="btn sm primary" data-action="access-claim-confirm" disabled=${busy} onClick=${claim}
              aria-disabled=${why ? "true" : undefined}>${busy ? html`<${Spinner} />` : null} Claim with my key</button>
            <button type="button" class="btn ghost sm" disabled=${busy} onClick=${() => setClaimArmed(false)}>Cancel</button>`
          : html`<button type="button" class="btn sm" data-action="access-claim" aria-disabled=${why ? "true" : undefined}
              title=${why || "Claim the board's SSH with your key"}
              onClick=${() => { if (why) { setErr({ errName: "REFUSED", message: `${why}. Nothing was run.` }); interlock(bid, "claim", "board claim", why); return; } setClaimArmed(true); }}>
              <${Icon} name="lock" /> Claim with my key…</button>`}
        ${why ? html`<span class="small muted" data-testid="reason-access-claim">${why}</span>` : null}</div>` : null}
      ${err ? html`<${Reason} level="err" testid="access-claim-error"
        text=${`${err.errName}: ${err.message}${err.hint ? ` (${err.hint})` : ""}${err.holder ? ` (holder: ${err.holder})` : ""}`} />` : null}
    </div><//>`;
}
