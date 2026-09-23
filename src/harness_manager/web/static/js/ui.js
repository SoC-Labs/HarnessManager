// Shared components: icons, chips, cards, reasons, result boxes, action buttons, arm boxes.

import { ICONS } from "../vendor/lucide/icons.js";
import {
  gateReason, interlock, isArmed, overdueText, panelState, runAction, setArmed,
} from "./actions.js";
import {
  checkIcon, checkLabel, checkLevel, elapsedSince, LINK_ICONS, linkName, secs, VIA_NAMES,
} from "./format.js";
import { html, useState } from "./lib.js";

export function Icon({ name, cls = "", label = "" }) {
  const inner = ICONS[name] || ICONS["circle-help"];
  const a11y = label ? { role: "img", "aria-label": label } : { "aria-hidden": "true" };
  const missing = ICONS[name] ? undefined : name;
  return html`<svg class=${`icon ${cls}`} data-missing-icon=${missing} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false" ...${a11y}
    dangerouslySetInnerHTML=${{ __html: inner }}></svg>`;
}

export function Chip({ level = "", icon = "", children, title = "", cls = "", testid = "" }) {
  return html`<span class=${`chip ${level} ${cls}`} title=${title || undefined}
    data-testid=${testid || undefined} data-level=${level || undefined}>
    ${icon ? html`<${Icon} name=${icon} />` : null}${children}</span>`;
}

// The three-state check chip. UNCHECKED gets its own neutral-warning look.
export function CheckChip({ check, prefix = "", testid = "" }) {
  const level = checkLevel(check);
  const title = check === "unchecked" || !check
    ? "Could not compare: this is NOT a pass" : "";
  return html`<${Chip} level=${level} icon=${checkIcon(check)} title=${title} testid=${testid}>
    ${prefix}${checkLabel(check)}<//>`;
}

export function Card({ title, icon = "", sub = "", actions = null, children, cls = "", testid = "",
  bodyCls = "" }) {
  return html`<section class=${`card ${cls}`} data-testid=${testid || undefined}
      aria-label=${typeof title === "string" ? title : undefined}>
    <div class="card-head">
      <h2 class="card-title">${icon ? html`<${Icon} name=${icon} />` : null}${title}</h2>
      <span class="spacer"></span>
      ${actions}
    </div>
    ${sub ? html`<p class="card-sub">${sub}</p>` : null}
    <div class=${`card-body ${bodyCls}`}>${children}</div>
  </section>`;
}

export function Reason({ text, level = "", icon = "", testid = "" }) {
  if (!text) return null;
  const name = icon || (level === "err" ? "circle-x" : level === "warn" ? "triangle-alert"
    : level === "ok" ? "circle-check" : level === "unk" ? "circle-help" : "info");
  return html`<p class=${`reason ${level}`} data-testid=${testid || undefined}>
    <${Icon} name=${name} /><span>${text}</span></p>`;
}

export function Spinner() { return html`<${Icon} name="loader-circle" cls="spin" />`; }

// The "$ command  (rc N, T s)" box. lines: [{kind, ...}] from actions.js.
export function ResultBlock({ lines, placeholder = "", panel = null, testid = "" }) {
  const running = panel && panel.running;
  if (!lines || !lines.length) {
    if (!placeholder) return null;
    return html`<div class="result empty" data-testid=${testid || undefined}>${placeholder}</div>`;
  }
  return html`<div class="result" role="log" aria-live="polite" data-testid=${testid || undefined}>
    ${lines.map((l, i) => html`<div key=${i}>${renderLine(l, panel)}</div>`)}
    ${running && panel.overdue ? html`<div class="warnline">${overdueText(panel)}</div>` : null}
  </div>`;
}

function renderLine(l, panel) {
  if (l.kind === "rc") {
    if (l.notRun) return html`<span class="rc warn">$ ${l.command}  (<b>not run</b>)</span>`;
    if (l.running) {
      const t = panel && panel.startedAt ? secs(elapsedSince(panel.startedAt)) : "0.0";
      return html`<span class="rc">$ ${l.command}  (running, ${t} s)</span>`;
    }
    const rc = l.rc === null || l.rc === undefined ? "no answer" : `rc ${l.rc}`;
    return html`<span class=${`rc ${l.level || ""}`}>$ ${l.command}  (<b>${rc}</b>, ${secs(l.secs)} s)</span>`;
  }
  if (l.kind === "err") return html`<span><span class="errname">${l.name}</span>  ${l.text}</span>`;
  if (l.kind === "hint") return html`<span class="hint">${l.text}</span>`;
  if (l.kind === "progress") return html`<span class="progress-line">${l.text}</span>`;
  if (l.kind === "ok") return html`<span class="ok">${l.text}</span>`;
  if (l.kind === "warnline") return html`<span class="warnline">${l.text}</span>`;
  return html`<span>${l.text}</span>`;
}

// One button of a panel, plus the reason it cannot run (visible text, never a silent grey).
export function ActionRow({ bid, panel, spec, gate = {}, variant = "", icon = "",
  showReason = true, quietArm = false, compact = false, children = null }) {
  const p = panelState(bid, panel);
  const why = gateReason(bid, panel, spec.key, gate);
  const running = p.running === spec.key;
  const blocked = !!why && !running;
  const onClick = () => {
    if (running) return;
    if (why) {
      interlock(bid, panel, spec.command, why);
      return;
    }
    runAction(bid, panel, { ...spec, arm: gate.arm });
  };
  const label = running
    ? html`<${Spinner} /><span>${p.busyLabel}</span><span class="elapsed">${Math.floor(elapsedSince(p.startedAt))} s</span>`
    : html`${icon ? html`<${Icon} name=${icon} />` : null}<span>${spec.label}</span>`;
  let reason = running ? (p.overdue ? "past its time budget; the page stays usable" : "")
    : why === "running" ? "" : why;
  if (quietArm && reason.startsWith("not armed")) reason = "";   // a sibling already says it
  return html`<div class=${`action-row ${compact ? "compact" : ""}`}>
    <div class="action-main">${children}
    <button type="button" class=${`btn ${variant} ${compact ? "sm" : ""}`} data-action=${spec.key}
      aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined}
      title=${blocked ? why : undefined} onClick=${onClick}>${label}</button></div>
    ${showReason && reason ? html`<${Reason} text=${reason}
      icon=${reason.startsWith("Cannot:") ? "circle-slash" : reason.startsWith("not armed") ? "lock" : "info"}
      testid=${`reason-${spec.key}`} />` : null}
  </div>`;
}

export function ArmBox({ bid, armKey, text, testid = "", compact = false }) {
  const on = isArmed(bid, armKey);
  if (compact) {
    // The tiles' arm: the same state as the section's arm box, the full text as its title.
    return html`<label class=${`arm-inline ${on ? "armed" : ""}`} title=${text} data-testid=${testid || undefined}>
      <input type="checkbox" checked=${on} aria-label=${text}
        onChange=${(e) => setArmed(bid, armKey, e.target.checked)} />
      <${Icon} name=${on ? "lock-open" : "lock"} cls="sm" /><span>Arm</span></label>`;
  }
  return html`<label class=${`arm ${on ? "armed" : ""}`} data-testid=${testid || undefined}>
    <input type="checkbox" checked=${on} onChange=${(e) => setArmed(bid, armKey, e.target.checked)} />
    <${Icon} name=${on ? "lock-open" : "lock"} />
    <span>${text}</span>
  </label>`;
}

export function CopyButton({ text, label = "Copy" }) {
  const [done, setDone] = useState(false);
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1200);
    } catch (e) {
      setDone(false);
    }
  };
  return html`<button type="button" class="btn ghost sm icon-only" title=${`${label}: ${text}`}
    aria-label=${`${label} ${text}`} onClick=${onClick}>
    <${Icon} name=${done ? "check" : "copy"} /></button>`;
}

export function LinkLine({ link }) {
  const via = link.via ? VIA_NAMES[link.via] || `via ${link.via}` : "";
  return html`<div class="link" data-link=${link.kind}>
    <${Icon} name=${LINK_ICONS[link.kind] || "link"} cls="sm" />
    <div><span>${linkName(link.kind)}</span>${" "}<span class="mono secondary">${link.address}</span>
      ${via ? html`${" "}<span class="tag" title="reached through a TCP-only tunnel">${via}</span>` : null}
      ${link.detail ? html`<div class="sub">${link.detail}</div>` : null}</div>
  </div>`;
}


export function Seg({ value, options, onChange, label }) {
  return html`<div class="seg" role="group" aria-label=${label}>
    ${options.map((o) => html`<button type="button" key=${o.value}
      aria-pressed=${value === o.value ? "true" : "false"} title=${o.title || o.label}
      onClick=${() => onChange(o.value)}>${o.icon ? html`<${Icon} name=${o.icon} cls="sm" />` : null}
      ${o.label}</button>`)}
  </div>`;
}

