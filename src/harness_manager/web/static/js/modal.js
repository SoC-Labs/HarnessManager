// The page's dialogs and toasts (UI v2 shell): one layer app.js renders, and the extension
// points a lane uses so it never edits app.js.
//
//   registerModal("import", ImportDialog)      // at the lane module's top level
//   openModal("import", {bid, path})           // from a button; the component gets
//                                              //   {...props, close}
//   closeModal()
//
// One dialog at a time: opening another replaces it. Escape and a click on the backdrop close
// it, unless the dialog says it is busy (`setModalBusy(true)`: an action it started runs).
// The dialogs of 0.1.0 (lease.js, the Settings, the app update) keep their own layers.

import { html, useEffect } from "./lib.js";
import { changed, S } from "./store.js";
import { Icon } from "./ui.js";

const kinds = new Map();
const M = { open: null, busy: false, trigger: null };   // open: {kind, props, id}

export function registerModal(kind, component) {
  kinds.set(kind, component);
}

export function openModal(kind, props = {}) {
  if (!kinds.has(kind)) throw new Error(`openModal: no dialog "${kind}" (registerModal it first)`);
  M.trigger = document.activeElement;
  M.open = { kind, props, id: (M.open ? M.open.id : 0) + 1 };
  M.busy = false;
  changed();
}

export function closeModal() {
  if (!M.open) return;
  M.open = null;
  M.busy = false;
  const t = M.trigger;
  M.trigger = null;
  changed();
  if (t && t.isConnected && t.focus) requestAnimationFrame(() => t.focus());
}

export function modalOpen(kind = "") {
  return !!M.open && (!kind || M.open.kind === kind);
}

export function setModalBusy(on) {
  M.busy = !!on;
  changed();
}

export function ModalLayer() {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && M.open && !M.busy) closeModal(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => {
    if (!M.open) return;
    const el = document.querySelector('[data-testid="modal-layer"] [data-autofocus]');
    if (el && el.focus) el.focus();
  }, [M.open && M.open.id]);
  if (!M.open) return null;
  const Component = kinds.get(M.open.kind);
  return html`<div class="modal-back" data-testid="modal-layer" data-modal=${M.open.kind}
      onMouseDown=${(e) => { if (e.target === e.currentTarget && !M.busy) closeModal(); }}>
    <${Component} key=${M.open.id} ...${M.open.props} close=${closeModal} />
  </div>`;
}

// A dialog's frame: the head (icon, title, a note, close), the body, the foot (its buttons).
// `bodyCls` "modal-body" is the two-column body (a nav and a pane); the default pads.
export function ModalShell({ title, icon = "info", note = null, children, foot = null, cls = "",
  bodyCls = "modal-pad", testid = "" }) {
  return html`<div class=${`modal ${cls}`} role="dialog" aria-modal="true" aria-label=${title}
      data-testid=${testid || undefined}>
    <div class="modal-head"><${Icon} name=${icon} /><h2 class="card-title">${title}</h2>
      ${note ? html`<span class="muted small">${note}</span>` : null}
      <span class="grow"></span>
      <button type="button" class="btn ghost sm icon-only" aria-label=${`Close ${title.toLowerCase()}`}
        disabled=${M.busy} data-autofocus onClick=${closeModal}><${Icon} name="x" /></button></div>
    <div class=${bodyCls}>${children}</div>
    ${foot ? html`<div class="modal-foot">${foot}</div>` : null}
  </div>`;
}

// store.js toast(): one note at a time, at the bottom of the page.
export function ToastLayer() {
  const t = S.ui.toast;
  if (!t) return null;
  return html`<div class=${`toast ${t.level || ""}`} role="status" data-testid="toast" key=${t.id}>
    <${Icon} name=${t.icon || "circle-check"} /><span>${t.text}</span></div>`;
}
