// Console sessions: one WebSocket and one xterm.js terminal per (board, console name).
//
// A session lives outside the component tree, so switching sections or boards never
// drops bytes: the terminal keeps its scrollback and is re-attached when shown again.
// Bytes arrive as binary frames; a text frame {"state": ...} is a state change.
//
// UI v2 (Workbench): who may type (G1b). The daemon sends {"input": {role, writable,
// read_only_reason}} when the socket opens read-only and whenever that changes; `input` keeps
// the last one (null: the daemon never said, so the console row's `writable` stands). A
// read-only session drops keystrokes here too, with the reason, so nothing is sent that the
// daemon would drop anyway. `lines` counts the lines received (the switcher's "new" count).

import { socketCloseReason, socketUrl } from "./api.js";
import { changed, log } from "./store.js";
import { onThemeChange, token } from "./theme.js";
import { FitAddon } from "../vendor/xterm/addon-fit.module.js";
import { Terminal } from "../vendor/xterm/xterm.module.js";

const sessions = new Map();
const encoder = new TextEncoder();

function termTheme() {
  return {
    background: token("--term-bg"),
    foreground: token("--term-fg"),
    cursor: token("--term-cursor"),
    cursorAccent: token("--term-bg"),
    selectionBackground: token("--term-selection"),
  };
}

onThemeChange(() => {
  // Let the new custom properties apply first.
  requestAnimationFrame(() => {
    for (const s of sessions.values()) s.term.options.theme = termTheme();
  });
});

export class ConsoleSession {
  constructor(bid, name) {
    this.bid = bid;
    this.name = name;
    this.state = "connecting";
    this.detail = "";
    this.bytesIn = 0;
    this.dropped = 0;          // bytes harness-manager-daemon dropped because this page fell behind
    this.input = null;         // {role, writable, read_only_reason} from the daemon's input frame
    this.lines = 0;            // newlines received since the session started (or was cleared)
    this.ws = null;
    this.opened = false;
    this.closedByUs = false;
    this.term = new Terminal({
      convertEol: true,
      cursorBlink: false,
      fontFamily: '"IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.2,
      scrollback: 5000,
      theme: termTheme(),
      allowProposedApi: false,
    });
    this.fit = new FitAddon();
    this.term.loadAddon(this.fit);
    this.host = document.createElement("div");
    this.host.className = "term-host";
    this.host.dataset.console = name;
    this.term.onData((text) => this.send(encoder.encode(text)));
    this.connect();
  }

  connect() {
    this.closedByUs = false;
    this.input = null;         // a new socket says again when it is read-only
    this.setState("connecting", "");
    let ws;
    try {
      ws = new WebSocket(socketUrl("consoleSocket", { bid: this.bid, name: this.name }));
    } catch (e) {
      this.setState("down", String(e.message || e));
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    let opened = false;
    // Text frames from harness-manager-daemon: {"state","name","detail"} first and on every change,
    // {"dropped", "dropped_frames"} when the page fell behind, {"error": {...}} on a failure.
    ws.onmessage = (msg) => {
      if (typeof msg.data === "string") {
        let f = null;
        try { f = JSON.parse(msg.data); } catch (e) { f = null; }
        if (!f) return;
        if (f.input && typeof f.input === "object") {
          this.input = { role: f.input.role || "", writable: f.input.writable !== false,
            read_only_reason: f.input.read_only_reason || "" };
          changed();
        }
        if (f.state) this.setState(f.state, f.detail || "");
        if (f.dropped) {
          this.dropped += Number(f.dropped) || 0;
          log("warning", "console", `${this.name}: the daemon dropped ${f.dropped} bytes (this page fell behind)`, this.bid);
          changed();
        }
        if (f.error) {
          this.detail = `${f.error.name || "error"}: ${f.error.message || ""}`;
          log("error", "console", `${this.name}: ${this.detail}`, this.bid);
          changed();
        }
        return;
      }
      const bytes = new Uint8Array(msg.data);
      this.bytesIn += bytes.length;
      let nl = 0;
      for (let i = 0; i < bytes.length; i += 1) if (bytes[i] === 10) nl += 1;
      if (nl) {
        this.lines += nl;
        if (this.onLines) this.onLines();
      }
      this.term.write(bytes);
    };
    ws.onopen = () => {
      opened = true;
      if (this.state === "connecting") this.setState("up", "");
    };
    ws.onclose = (ev) => {
      this.ws = null;
      if (this.closedByUs) return;
      const why = socketCloseReason(ev, opened);
      if (this.state === "closed" || (ev.code === 1000 && opened)) {
        this.setState("closed", this.detail || "the console was closed");
        return;
      }
      this.setState("down", why.text || this.detail);
    };
  }

  setState(state, detail) {
    const was = this.state;
    this.state = state;
    this.detail = detail;
    if (was !== state && (state === "down" || state === "closed")) {
      log(state === "up" ? "info" : "warning", "console", `${this.name}: ${state}${detail ? ` (${detail})` : ""}`, this.bid);
    }
    changed();
  }

  // Returns "" when sent, else why not (nothing was sent then).
  send(bytes) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return "the console is not connected";
    if (this.input && !this.input.writable) return this.input.read_only_reason || "this console is read-only here";
    this.ws.send(bytes);
    return "";
  }

  attach(container) {
    if (this.host.parentNode !== container) container.appendChild(this.host);
    if (!this.opened) {
      this.term.open(this.host);
      this.opened = true;
    }
    this.refit();
  }

  refit() {
    if (!this.opened || !this.host.isConnected) return;
    try { this.fit.fit(); } catch (e) { /* not laid out yet */ }
  }

  detach() {
    if (this.host.parentNode) this.host.parentNode.removeChild(this.host);
  }

  clear() { this.term.clear(); }

  // The switcher's "N new lines" (a console not on screen): lines since `seen` was taken.
  unseen() { return Math.max(0, this.lines - (this.seen || 0)); }

  markSeen() { this.seen = this.lines; }

  text() {
    const buf = this.term.buffer.active;
    const lines = [];
    for (let i = 0; i < buf.length; i += 1) {
      const line = buf.getLine(i);
      if (line) lines.push(line.translateToString(true));
    }
    while (lines.length && lines[lines.length - 1] === "") lines.pop();
    return lines.join("\n");
  }

  dispose() {
    this.closedByUs = true;
    if (this.ws) this.ws.close();
    this.detach();
    this.term.dispose();
  }
}

export function consoleSession(bid, name) {
  const key = `${bid}\u0000${name}`;
  let s = sessions.get(key);
  if (!s) {
    s = new ConsoleSession(bid, name);
    sessions.set(key, s);
  }
  return s;
}

export function existingSession(bid, name) { return sessions.get(`${bid}\u0000${name}`) || null; }

export function closeBoardConsoles(bid) {
  for (const [key, s] of sessions) {
    if (s.bid === bid) {
      s.dispose();
      sessions.delete(key);
    }
  }
}

// For the browser tests and for debugging from the devtools console.
window.__harness_managerConsoles = {
  text: (bid, name) => { const s = existingSession(bid, name); return s ? s.text() : null; },
  state: (bid, name) => { const s = existingSession(bid, name); return s ? s.state : null; },
};
