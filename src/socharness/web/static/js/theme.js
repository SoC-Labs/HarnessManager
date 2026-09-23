// Light, dark, or follow the system. The choice is a per-browser convenience only.

import { S } from "./store.js";

const KEY = "socharness.theme";
const listeners = new Set();

export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() { for (const fn of listeners) fn(effectiveTheme()); }

export function effectiveTheme() {
  const set = document.documentElement.getAttribute("data-theme");
  if (set === "light" || set === "dark") return set;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(choice) {
  S.theme = choice;
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  try {
    if (choice === "system") window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, choice);
  } catch (e) { /* not remembered */ }
  notify();
}

export function initTheme() {
  const set = document.documentElement.getAttribute("data-theme");
  S.theme = set === "light" || set === "dark" ? set : "system";
  if (window.matchMedia) {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => { if (S.theme === "system") notify(); };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
  }
}

// A CSS custom property's current value (for the terminal, which is not styled by CSS).
export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
