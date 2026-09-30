// The Live display: a pixel-exact mirror of the board's 320x240 panel (lane LM4;
// docs/design/LCD_MIRROR.md §7.4-§7.6, docs/API.md "Live display"). david's decisions:
// D3 only the lease holder sees the live picture (the daemon refuses anyone else, 409 HELD
// naming the holder); D4 no touch pass-through: the picture is view only, and a click on
// it does nothing to the board.
//
// - The socket (WS .../display/ws?ack=1) is open only while the Live display is on screen
//   and the tab is visible. It closes when the card is hidden or scrolled away, or the
//   tab is backgrounded: Harness Manager holds no board resource for a page nobody reads.
// - Binary frames are UPDATEs in the board's own layout (core/display_wire.py). They are
//   decoded here into one 320x240 backing store, the dirty tiles are put, and only THEN
//   is {"ack": seq} sent. One message is in flight, so a slow tab only ever gets fewer,
//   fresher pictures.
// - Text frames are the status: state, reason, badges, owner, mode, rate, rtt. The dim
//   badges (backlight off, display off, standby) come from the daemon, which shows a dim
//   state only once it has held for 1 s (LM1's DimDebounce): clcd_demo passes through
//   "display off" three times a second.
// - Overlays are drawn OVER the canvas, never into it: hatching on tiles that are not
//   VALID, grey while the DUT owns the panel and this image cannot see it, the badges,
//   stale and reconnecting. The canvas always holds the panel's own pixels.
// - Refused (409 HELD, 422 UNAVAILABLE, a board that is not open): today's text mirror
//   with a one-line reason (§7.5). Never an error page. The daemon picks which (its
//   display_api.refusal): a board that can never show it (bare metal, no lcd_mirror) is 422
//   even behind someone else's lease, so the line names a holder only for a 409.
// - PANEL-TRUTH: while the daemon checks the lease with the hub (a hub that reset one ssh:
//   status "connecting" with a reason and a detail), the line says so with a spinner, and
//   the raw error (ssh's words) is behind "Details", never the headline. displayLive(bid)
//   tells the Front panel card's headline whether the picture is on screen; a client
//   re-renders the page (store.changed) only when that flips.

import { callBytes, socketCloseReason, socketUrl } from "./api.js";
import { boardName } from "./format.js";
import { html, useEffect, useMemo, useRef, useState } from "./lib.js";
import { changed, onBoardEvent, S } from "./store.js";
import { Chip, Icon, Reason, Seg, Spinner } from "./ui.js";

// --- the wire (core/display_wire.py; LCD_MIRROR.md §6.1) -----------------------------------------

export const W = 320;
export const H = 240;
export const TILE = 16;
export const TILES_X = 20;
export const TILES_Y = 15;
export const NTILES = 300;
const HEADER = 8;                  // 'L' 'M' u8 type, u8 rsvd, u32 len (LE)
const T_UPDATE = 0x02;
const FIXED = 59;                  // u32 seq, t_ms, frames, resets, status; u8 owner; u8 valid[38]
const VALID_BYTES = 38;
const REGS = 256;
export const S_KEY = 1 << 24;
export const S_KEY_FIRST = 1 << 25;
export const S_KEY_LAST = 1 << 26;
export const S_SNAP_LAST = 1 << 27;
export const ENCODINGS = ["FILL", "PAL1", "PAL2", "RLE16", "RAW"];
const PAYLOAD = [2, 36, 72, -1, 512];     // RLE16 varies

export class WireError extends Error {}

let LUT = null;

// RGB565 -> the canvas's RGBA as one u32, by bit replication (tools/gen_tokens.py, the PNG).
export function rgbaLut() {
  if (LUT) return LUT;
  const little = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;
  LUT = new Uint32Array(65536);
  for (let v = 0; v < 65536; v += 1) {
    const r5 = (v >> 11) & 31;
    const g6 = (v >> 5) & 63;
    const b5 = v & 31;
    const r = (r5 << 3) | (r5 >> 2);
    const g = (g6 << 2) | (g6 >> 4);
    const b = (b5 << 3) | (b5 >> 2);
    LUT[v] = little ? ((255 << 24) | (b << 16) | (g << 8) | r) >>> 0
      : ((r << 24) | (g << 16) | (b << 8) | 255) >>> 0;
  }
  return LUT;
}

// One viewer UPDATE (a whole binary frame) -> its fields and a flat record table
// [idx, enc, offset, length] x ntiles. Strict, as core/display_wire.parse_update is.
export function parseUpdate(data) {
  const u8 = data instanceof Uint8Array ? data : new Uint8Array(data);
  const n = u8.length;
  if (n < HEADER) throw new WireError(`a ${n}-byte frame is shorter than its header`);
  if (u8[0] !== 0x4c || u8[1] !== 0x4d) throw new WireError("not an lcd_mirror message");
  if (u8[2] !== T_UPDATE) throw new WireError(`message type 0x${u8[2].toString(16)} is not UPDATE`);
  const dv = new DataView(u8.buffer, u8.byteOffset, n);
  const len = dv.getUint32(4, true);
  if (HEADER + len !== n) throw new WireError(`the header says ${len} B, the frame has ${n - HEADER}`);
  if (len < FIXED) throw new WireError(`UPDATE is ${len} B, shorter than its ${FIXED} B header`);
  const b = HEADER;
  const status = dv.getUint32(b + 16, true);
  let off = b + FIXED;
  let regs = null;
  let mode = null;
  if (status & S_KEY_FIRST) {
    if (off + REGS + 2 > n) throw new WireError("UPDATE key_first is too short for its 256 REGS");
    regs = u8.subarray(off, off + REGS);
    off += REGS;
  } else {
    if (off + 4 + 2 > n) throw new WireError("UPDATE is too short for its mode word");
    mode = dv.getUint32(off, true);
    off += 4;
  }
  const ntiles = dv.getUint16(off, true);
  off += 2;
  if (ntiles > NTILES) throw new WireError(`UPDATE claims ${ntiles} tiles; the panel has ${NTILES}`);
  const recs = new Int32Array(ntiles * 4);
  for (let i = 0; i < ntiles; i += 1) {
    if (off + 5 > n) throw new WireError("UPDATE ends inside a tile record");
    const idx = dv.getUint16(off, true);
    const enc = u8[off + 2];
    const ln = dv.getUint16(off + 3, true);
    const p = off + 5;
    if (p + ln > n) throw new WireError(`tile ${idx}'s payload runs past the UPDATE`);
    if (idx >= NTILES) throw new WireError(`tile index ${idx} is out of range`);
    if (enc > 4) throw new WireError(`tile ${idx} has unknown encoding ${enc}`);
    if (PAYLOAD[enc] >= 0 && ln !== PAYLOAD[enc]) {
      throw new WireError(`${ENCODINGS[enc]} is ${PAYLOAD[enc]} B, not ${ln} (tile ${idx})`);
    }
    recs.set([idx, enc, p, ln], i * 4);
    off = p + ln;
  }
  if (off !== n) throw new WireError(`UPDATE has ${n - off} trailing bytes`);
  return {
    seq: dv.getUint32(b, true), t_ms: dv.getUint32(b + 4, true), frames: dv.getUint32(b + 8, true),
    resets: dv.getUint32(b + 12, true), status, owner: u8[b + 20],
    valid: u8.subarray(b + 21, b + 21 + VALID_BYTES), regs, mode, ntiles, recs, u8, bytes: n,
  };
}

export function isValid(valid, t) { return ((valid[t >> 3] >> (t & 7)) & 1) === 1; }

// One tile record -> its 256 pixels in a row-major 320x240 Uint32Array (RGBA).
function decodeTile(u8, idx, enc, p, ln, px, lut) {
  const x0 = (idx % TILES_X) * TILE;
  const y0 = ((idx / TILES_X) | 0) * TILE;
  const o = y0 * W + x0;
  if (enc === 0) {                                      // FILL: u16
    const c = lut[u8[p] | (u8[p + 1] << 8)];
    for (let r = 0; r < TILE; r += 1) px.fill(c, o + r * W, o + r * W + TILE);
  } else if (enc === 4) {                               // RAW: 256 x u16
    for (let i = 0; i < 256; i += 1) {
      const q = p + 2 * i;
      px[o + (i >> 4) * W + (i & 15)] = lut[u8[q] | (u8[q + 1] << 8)];
    }
  } else if (enc === 1) {                               // PAL1: 2 colours, 16 x u16 rows
    const c0 = lut[u8[p] | (u8[p + 1] << 8)];
    const c1 = lut[u8[p + 2] | (u8[p + 3] << 8)];
    for (let r = 0; r < TILE; r += 1) {
      const bits = u8[p + 4 + 2 * r] | (u8[p + 5 + 2 * r] << 8);
      const row = o + r * W;
      for (let x = 0; x < TILE; x += 1) px[row + x] = (bits >> x) & 1 ? c1 : c0;
    }
  } else if (enc === 2) {                               // PAL2: 4 colours, 16 x u32 rows
    const c = [0, 1, 2, 3].map((k) => lut[u8[p + 2 * k] | (u8[p + 2 * k + 1] << 8)]);
    for (let r = 0; r < TILE; r += 1) {
      const q = p + 8 + 4 * r;
      const bits = (u8[q] | (u8[q + 1] << 8) | (u8[q + 2] << 16) | (u8[q + 3] << 24)) >>> 0;
      const row = o + r * W;
      for (let x = 0; x < TILE; x += 1) px[row + x] = c[(bits >>> (2 * x)) & 3];
    }
  } else {                                              // RLE16: PackBits over u16
    const end = p + ln;
    let i = p;
    let k = 0;
    while (i < end) {
      const t = u8[i];
      i += 1;
      if (t & 0x80) {
        const run = (t & 0x7f) + 1;
        if (i + 2 > end) throw new WireError(`RLE16 run is cut short (tile ${idx})`);
        if (k + run > 256) throw new WireError(`RLE16 runs past the tile's 256 pixels (tile ${idx})`);
        const c = lut[u8[i] | (u8[i + 1] << 8)];
        i += 2;
        for (let j = 0; j < run; j += 1, k += 1) px[o + (k >> 4) * W + (k & 15)] = c;
      } else {
        const lit = t + 1;
        if (i + 2 * lit > end) throw new WireError(`RLE16 literal is cut short (tile ${idx})`);
        if (k + lit > 256) throw new WireError(`RLE16 runs past the tile's 256 pixels (tile ${idx})`);
        for (let j = 0; j < lit; j += 1, k += 1, i += 2) px[o + (k >> 4) * W + (k & 15)] = lut[u8[i] | (u8[i + 1] << 8)];
      }
    }
    if (k !== 256) throw new WireError(`RLE16 gave ${k} pixels, not 256 (tile ${idx})`);
  }
}

// Every record of a parsed UPDATE into ``px``; returns the dirty box in tiles
// {tx0, ty0, tx1, ty1} (inclusive), or null when the message carried none.
export function decodeInto(msg, px, lut = rgbaLut()) {
  const { recs, u8, ntiles } = msg;
  let tx0 = TILES_X;
  let ty0 = TILES_Y;
  let tx1 = -1;
  let ty1 = -1;
  for (let i = 0; i < ntiles; i += 1) {
    const idx = recs[i * 4];
    decodeTile(u8, idx, recs[i * 4 + 1], recs[i * 4 + 2], recs[i * 4 + 3], px, lut);
    const tx = idx % TILES_X;
    const ty = (idx / TILES_X) | 0;
    if (tx < tx0) tx0 = tx;
    if (tx > tx1) tx1 = tx;
    if (ty < ty0) ty0 = ty;
    if (ty > ty1) ty1 = ty;
  }
  return tx1 < 0 ? null : { tx0, ty0, tx1, ty1 };
}

// --- scale: k whole device pixels per panel pixel (§7.4) ----------------------------------------

export function scaleFor(zoom, dpr = window.devicePixelRatio || 1) {
  const k = Math.max(1, Math.round(dpr * zoom));
  return { k, dpr, cssW: (W * k) / dpr, cssH: (H * k) / dpr };
}

const ZOOM_KEY = "harness_manager.display.zoom";
export const LIVE_RATE_HZ = 20;          // services.display.DEFAULT_RATE_HZ: the board clamps it
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
const WINDOW_MS = 2000;                  // fps and kB/s are over the last 2 s
const REFUSED_CODES = new Set([4002, 4003, 4004, 4012]);
const VIEW_ONLY = "View only: clicks on the picture do nothing to the board";

function storedZoom() {
  try { return window.localStorage.getItem(ZOOM_KEY) === "2" ? 2 : 1; } catch (e) { return 1; }
}

// --- one Live display's client: the socket, the backing store, the draws ------------------------

const LIVE = new Set();                  // mounted clients (event retries, the debug hook)

// A board's lease, session, identity or display changed: a refused view asks again.
onBoardEvent((ev) => {
  const t = ev.topic || "";
  if (!(t.startsWith("lease.") || t.startsWith("session.") || t === "board.identity" || t === "display.state")) return;
  for (const c of LIVE) if (c.bid === ev.board_id) c.eventRetry();
});

export class DisplayClient {
  constructor(bid) {
    this.bid = bid;
    this.lut = rgbaLut();
    this.px = new Uint32Array(W * H);
    this.image = null;                   // ImageData over px
    this.canvas = null;
    this.ctx = null;
    this.ws = null;
    this.socket = "closed";              // closed | connecting | open
    this.opened = false;                 // this socket reached onopen
    this.wanted = { mounted: false, visible: document.visibilityState !== "hidden", inView: false };
    this.status = null;                  // the latest status text frame
    this.wasLive = false;                // PANEL-TRUTH: what displayLive() last said
    this.refusal = null;                 // {reason, name, holder, code}: the typed refusal
    this.failure = "";                   // why the socket closed (untyped)
    this.presented = false;              // a picture is in the backing store
    this.have = new Uint8Array(NTILES);  // a record for the tile has been drawn since the keyframe
    this.valid = new Uint8Array(VALID_BYTES);
    this.hatched = [];
    this.hatchSig = "";
    this.dirty = null;
    this.pendingAck = null;
    this.stash = null;                   // a frame that arrived while paused
    this.paused = false;
    this.rafId = 0;
    this.retryN = 0;
    this.retryTimer = 0;
    this.eventTimer = 0;
    this.lastSeq = null;
    this.drawnAt = 0;                    // Date.now() of the last draw
    this.statusAt = 0;
    this.staleAt = 0;
    this.drawn = [];                     // [at, bytes, tiles] for fps / kB/s
    this.sent = { ack: 0, rate: 0, other: 0 };
    this.messages = 0;
    this.keys = 0;
    this.perf = { decodeMs: 0, drawMs: 0, keyMs: 0, keyDecodeMs: 0, keyDrawMs: 0, keyBytes: 0, maxMs: 0 };
    this.zoom = storedZoom();
    this.listeners = new Set();
    this.io = null;
    this.onVisibility = () => this.want({ visible: document.visibilityState !== "hidden" });
    this.onPageHide = () => this.want({ visible: false });
    this.mq = null;
    this.onDpr = () => { this.watchDpr(); this.notify(); };
  }

  // -- lifecycle ------------------------------------------------------------------------------

  mount(root) {
    LIVE.add(this);
    document.addEventListener("visibilitychange", this.onVisibility);
    window.addEventListener("pagehide", this.onPageHide);
    this.watchDpr();
    if (typeof IntersectionObserver === "function" && root) {
      this.io = new IntersectionObserver((entries) => {
        const e = entries[entries.length - 1];
        this.want({ inView: !!e && e.isIntersecting });
      });
      this.io.observe(root);
    } else {
      this.wanted.inView = true;
    }
    this.want({ mounted: true });
  }

  unmount() {
    LIVE.delete(this);
    if (this.wasLive) { this.wasLive = false; changed(); }
    document.removeEventListener("visibilitychange", this.onVisibility);
    window.removeEventListener("pagehide", this.onPageHide);
    if (this.io) this.io.disconnect();
    if (this.mq) this.mq.removeEventListener("change", this.onDpr);
    this.wanted.mounted = false;
    this.close("the Live display was hidden");
    clearTimeout(this.eventTimer);
    this.listeners.clear();
  }

  watchDpr() {
    if (this.mq) this.mq.removeEventListener("change", this.onDpr);
    if (typeof window.matchMedia !== "function") return;
    this.mq = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    this.mq.addEventListener("change", this.onDpr);
  }

  listen(fn) { this.listeners.add(fn); }

  notify() {
    const live = this.presented && !this.refusal;
    if (live !== this.wasLive) { this.wasLive = live; changed(); }   // the card's headline
    for (const fn of this.listeners) fn();
  }

  get isWanted() { return this.wanted.mounted && this.wanted.visible && this.wanted.inView; }

  want(part) {
    const was = this.isWanted;
    Object.assign(this.wanted, part);
    if (!this.isWanted) {
      this.close(this.wanted.visible ? "the Live display was scrolled away" : "the tab was hidden");
    } else if (!was && this.refusal) {
      this.retry();                        // back on screen: a refusal is asked again, once
    } else if (!this.ws && !this.refusal) {
      this.connect();
    }
    this.notify();
  }

  // -- the socket -----------------------------------------------------------------------------

  connect() {
    clearTimeout(this.retryTimer);
    this.retryTimer = 0;
    if (this.ws || !this.isWanted) return;
    let ws;
    try {
      ws = new WebSocket(socketUrl("displaySocket", { bid: this.bid },
        { ack: "1", rate: String(this.paused ? 0 : LIVE_RATE_HZ) }));
    } catch (e) {
      this.failure = String(e && e.message || e);
      this.schedule();
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.socket = "connecting";
    this.opened = false;
    this.lastSeq = null;
    this.pendingAck = null;
    this.stash = null;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.opened = true;
      this.socket = "open";
      this.notify();
    };
    ws.onmessage = (m) => {
      if (this.ws !== ws) return;
      if (typeof m.data === "string") this.onText(m.data);
      else this.onBinary(m.data);
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.socket = "closed";
      this.onClosed(ev, this.opened);
    };
    ws.onerror = () => { /* onclose follows */ };
    this.notify();
  }

  close(why = "") {
    clearTimeout(this.retryTimer);
    this.retryTimer = 0;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
    const ws = this.ws;
    this.ws = null;
    this.socket = "closed";
    this.pendingAck = null;
    if (ws) {
      try { ws.close(1000, why.slice(0, 120)); } catch (e) { /* already closing */ }
    }
  }

  schedule() {
    if (!this.isWanted || this.refusal) return;
    const delay = BACKOFF_MS[Math.min(this.retryN, BACKOFF_MS.length - 1)];
    this.retryN += 1;
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }

  retry() {
    this.refusal = null;
    this.retryN = 0;
    this.failure = "";
    this.close();
    this.connect();
    this.notify();
  }

  // A lease, session or identity event: a refused view asks again (once the burst settles).
  eventRetry() {
    if (!this.refusal || !this.isWanted) return;
    clearTimeout(this.eventTimer);
    this.eventTimer = setTimeout(() => { if (this.refusal && this.isWanted) this.retry(); }, 300);
  }

  onClosed(ev, opened) {
    const why = socketCloseReason(ev, opened);
    if (REFUSED_CODES.has(ev.code)) {
      // The daemon said no, typed (D3: not the lease holder; UNAVAILABLE; not open). The
      // picture goes: only the lease holder may see it. The text mirror stands in.
      const last = this.refusal || {};
      this.refusal = { reason: last.reason || ev.reason || why.text, name: last.name || why.text.split(":")[0],
        holder: last.holder || "", code: ev.code };
      this.presented = false;
      this.have.fill(0);
      this.px.fill(0);                     // nothing of the old picture is kept
    } else {
      this.failure = ev.code === 1000 ? (this.status && this.status.reason) || "the daemon closed the Live display"
        : why.text;
      this.schedule();
    }
    this.notify();
  }

  send(obj) {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(obj));
    if ("ack" in obj) this.sent.ack += 1;
    else if ("rate" in obj) this.sent.rate += 1;
    else this.sent.other += 1;
    return true;
  }

  onText(text) {
    let obj;
    try { obj = JSON.parse(text); } catch (e) { return; }
    if (!obj || typeof obj !== "object") return;
    if (obj.error && !obj.badges) {
      const e = obj.error || {};
      if (obj.state === "refused") {         // the socket's refusal: the close follows
        this.refusal = { reason: obj.reason || e.message || "", name: e.name || "", holder: e.holder || "", code: 0 };
      }
      this.notify();
      return;
    }
    if (obj.state === "stale" && (!this.status || this.status.state !== "stale")) this.staleAt = Date.now();
    this.status = obj;
    this.statusAt = Date.now();
    this.notify();
  }

  onBinary(buf) {
    if (this.paused) {                     // hold it (and its ack): the picture stays put
      this.stash = buf;
      return;
    }
    this.apply(buf);
  }

  apply(buf) {
    const t0 = performance.now();
    let msg;
    try {
      msg = parseUpdate(buf);
      if (msg.status & S_KEY) this.have.fill(0);
      const box = decodeInto(msg, this.px, this.lut);
      if (box) {
        this.dirty = this.dirty ? {
          tx0: Math.min(this.dirty.tx0, box.tx0), ty0: Math.min(this.dirty.ty0, box.ty0),
          tx1: Math.max(this.dirty.tx1, box.tx1), ty1: Math.max(this.dirty.ty1, box.ty1),
        } : box;
      }
    } catch (e) {
      // The stream is untrusted from here: drop the socket; the next one starts with a keyframe.
      this.failure = `the Live display sent a frame this page cannot read: ${e.message}`;
      this.close();
      this.schedule();
      this.notify();
      return;
    }
    const decodeMs = performance.now() - t0;
    for (let i = 0; i < msg.ntiles; i += 1) this.have[msg.recs[i * 4]] = 1;
    this.valid.set(msg.valid);
    this.messages += 1;
    if (msg.status & S_KEY) this.keys += 1;
    this.lastSeq = msg.seq;
    this.pendingAck = msg.seq;
    this.drawn.push([Date.now(), msg.bytes, msg.ntiles]);
    this.perf.decodeMs = decodeMs;
    this.pendingKey = (msg.status & S_KEY) ? { decodeMs, bytes: msg.bytes } : null;
    this.updateHatch();
    const first = !this.presented;
    this.presented = true;
    if (first) this.notify();              // the canvas mounts; attach() draws and acks
    this.scheduleDraw();
  }

  updateHatch() {
    const out = [];
    for (let t = 0; t < NTILES; t += 1) if (!this.have[t] || !isValid(this.valid, t)) out.push(t);
    const sig = out.join(",");
    if (sig !== this.hatchSig) {
      this.hatchSig = sig;
      this.hatched = out;
      this.notify();
    }
  }

  // -- drawing --------------------------------------------------------------------------------

  attach(canvas) {
    if (canvas === this.canvas) return;
    this.canvas = canvas;
    this.ctx = canvas ? canvas.getContext("2d") : null;
    if (this.ctx) {
      if (!this.image) this.image = new ImageData(new Uint8ClampedArray(this.px.buffer), W, H);
      this.dirty = { tx0: 0, ty0: 0, tx1: TILES_X - 1, ty1: TILES_Y - 1 };
      this.scheduleDraw();
    }
  }

  scheduleDraw() {
    if (!this.ctx || this.rafId) return;
    this.rafId = requestAnimationFrame(() => {
      this.rafId = 0;
      this.draw();
    });
  }

  draw() {
    if (!this.ctx) return;
    const t0 = performance.now();
    const d = this.dirty;
    if (d) {
      if (!this.image) this.image = new ImageData(new Uint8ClampedArray(this.px.buffer), W, H);
      this.ctx.putImageData(this.image, 0, 0, d.tx0 * TILE, d.ty0 * TILE,
        (d.tx1 - d.tx0 + 1) * TILE, (d.ty1 - d.ty0 + 1) * TILE);
      this.dirty = null;
    }
    const drawMs = performance.now() - t0;
    this.perf.drawMs = drawMs;
    this.perf.maxMs = Math.max(this.perf.maxMs, this.perf.decodeMs + drawMs);
    if (this.pendingKey) {
      this.perf.keyDecodeMs = this.pendingKey.decodeMs;
      this.perf.keyDrawMs = drawMs;
      this.perf.keyMs = this.pendingKey.decodeMs + drawMs;
      this.perf.keyBytes = this.pendingKey.bytes;
      this.pendingKey = null;
    }
    this.drawnAt = Date.now();
    if (this.pendingAck !== null && this.send({ ack: this.pendingAck })) this.pendingAck = null;
    this.retryN = 0;
    this.failure = "";
  }

  // -- controls -------------------------------------------------------------------------------

  setZoom(z) {
    this.zoom = z === 2 ? 2 : 1;
    try { window.localStorage.setItem(ZOOM_KEY, String(this.zoom)); } catch (e) { /* not kept */ }
    this.notify();
  }

  setPaused(on) {
    this.paused = !!on;
    this.send({ rate: this.paused ? 0 : LIVE_RATE_HZ });
    if (!this.paused && this.stash) {
      const buf = this.stash;
      this.stash = null;
      this.apply(buf);
    }
    this.notify();
  }

  rates() {
    const now = Date.now();
    while (this.drawn.length && now - this.drawn[0][0] > WINDOW_MS) this.drawn.shift();
    let bytes = 0;
    let frames = 0;
    for (const [, b, tiles] of this.drawn) { bytes += b; if (tiles) frames += 1; }
    return { fps: frames / (WINDOW_MS / 1000), bps: bytes / (WINDOW_MS / 1000) };
  }

  debug() {
    return {
      bid: this.bid, socket: this.socket, state: this.status ? this.status.state : "", seq: this.lastSeq,
      messages: this.messages, keys: this.keys, hatched: this.hatched.length, paused: this.paused,
      presented: this.presented, refusal: this.refusal, failure: this.failure, sent: { ...this.sent },
      perf: { ...this.perf }, zoom: this.zoom, wanted: { ...this.wanted },
    };
  }
}

// For the browser tests and the devtools console: each mounted Live display's counters.
window.__harness_managerDisplay = () => [...LIVE].map((c) => c.debug());

// PANEL-TRUTH: a mounted Live display of this board has the board's own picture on screen.
export function displayLive(bid) {
  for (const c of LIVE) if (c.bid === bid && c.presented && !c.refusal) return true;
  return false;
}

// --- the component ----------------------------------------------------------------------------

function agoText(ms) {
  const s = Math.max(0, ms) / 1000;
  if (s < 10) return `${s.toFixed(1)} s ago`;
  if (s < 120) return `${Math.round(s)} s ago`;
  if (s < 7200) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

const STATE_CHIP = {
  live: { level: "ok", icon: "circle-check", text: "live" },
  syncing: { level: "accent", icon: "loader-circle", text: "syncing" },
  connecting: { level: "accent", icon: "loader-circle", text: "connecting" },
  stale: { level: "warn", icon: "triangle-alert", text: "stale" },
  reconnecting: { level: "warn", icon: "loader-circle", text: "reconnecting" },
  refused: { level: "unk", icon: "circle-slash", text: "refused by the board" },
  down: { level: "unk", icon: "circle-slash", text: "down" },
};

const BADGE_ICON = { held: "lock", dim: "moon", warn: "triangle-alert", grey: "circle-slash" };
const BADGE_LEVEL = { held: "held", dim: "unk", warn: "warn", grey: "unk" };
const MODE_TITLE = {
  sw: "The harness's software tap: exact for the harness's own screen, blind while the DUT owns the panel",
  hw: "The static shell's snooper: exact for whoever owns the panel",
};

// What the head's chip says: the socket first (it is what this page has), then the board.
function viewState(c) {
  if (c.paused) return { level: "accent", icon: "circle-pause", text: "paused" };
  if (c.socket === "connecting") return STATE_CHIP.connecting;
  if (c.socket === "closed") return c.isWanted ? STATE_CHIP.reconnecting : { level: "unk", icon: "circle-minus", text: "closed while hidden" };
  const st = c.status && c.status.state;
  return STATE_CHIP[st] || STATE_CHIP.connecting;
}

function Hatches({ tiles }) {
  if (!tiles.length) return null;
  return html`<div class="ld-hatches" aria-hidden="true" data-testid="live-hatches">
    ${tiles.map((t) => html`<div key=${t} class="ld-hatch" data-tile=${t}
      style=${`left:${(t % TILES_X) * 5}%;top:${(Math.floor(t / TILES_X) * 100) / TILES_Y}%`}></div>`)}
  </div>`;
}

function refusalText(r) {
  const why = r.reason || "the daemon refused the Live display";
  const holder = r.holder && !why.includes(r.holder) ? ` (the lease is held by ${r.holder})` : "";
  return `Live display: ${why}${holder}`;
}

// Why there is no picture (yet): the one line over the text mirror.
function fallbackLine(c) {
  if (c.refusal) {
    return { level: c.refusal.name === "HELD" || c.refusal.code === 4004 ? "held" : "unk",
      icon: c.refusal.name === "HELD" || c.refusal.code === 4004 ? "lock" : "circle-slash",
      text: refusalText(c.refusal), kind: "refused" };
  }
  const st = c.status;
  if (st && (st.state === "down" || st.state === "refused") && st.reason) {
    return { level: "unk", icon: "circle-slash", text: `Live display: ${st.reason}`, kind: st.state, detail: st.detail || "" };
  }
  // PANEL-TRUTH: still connecting, and the daemon says what it is doing (checking the lease
  // with the hub): a spinner and its words; the raw error only behind Details.
  if (st && st.state === "connecting" && st.reason && c.socket === "open") {
    return { level: "", icon: "loader-circle", text: `Live display: ${st.reason}`, kind: "checking", detail: st.detail || "" };
  }
  if (st && st.state === "reconnecting" && st.reason && c.socket === "open") {
    return { level: "unk", icon: "circle-help", text: `Live display: reconnecting to the board (${st.reason})`, kind: "reconnecting" };
  }
  if (c.failure && c.socket === "closed") {
    return { level: "unk", icon: "circle-help", text: `Live display: ${c.failure}; trying again`, kind: "failed" };
  }
  if (!c.isWanted) return { level: "", icon: "info", text: "Live display: opens when this card is on screen", kind: "idle" };
  return { level: "", icon: "loader-circle", text: "Live display: connecting...", kind: "connecting" };
}

function fileName(bid) {
  const row = S.boards[bid] || {};
  const name = boardName(row.candidate, bid).replace(/[^A-Za-z0-9._-]+/g, "_");
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${name}-live-display-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.png`;
}

async function snapshot(c, setNote) {
  setNote({ busy: true, text: "" });
  try {
    const blob = await callBytes("displayPng", { bid: c.bid }, { scale: String(c.zoom), hatch: "1" }, "image/png");
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fileName(c.bid);
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    setNote({ busy: false, text: "" });
  } catch (e) {
    setNote({ busy: false, text: `${e.errName || "FAILED"}: ${e.reason || e.message}` });
  }
}

// ``children``: today's text mirror, shown whenever there is no live picture (§7.5).
// ``fit`` (UI v2 round 3, the Overview's Front panel): the picture fills the card's width
// (nearest-neighbour, 4:3) instead of k whole device pixels, and the head folds into one foot
// row (state, mode, freshness, Pause, Snapshot): the card's own head has the title.
export function LiveDisplay({ bid, children = null, fit = false }) {
  const client = useMemo(() => new DisplayClient(bid), [bid]);
  const [, setTick] = useState(0);
  const [note, setNote] = useState({ busy: false, text: "" });
  const rootRef = useRef(null);
  const canvasRef = useMemo(() => (el) => client.attach(el), [client]);
  useEffect(() => {
    client.listen(() => setTick((n) => n + 1));
    client.mount(rootRef.current);
    // The freshness line's clock: only while a picture is on screen.
    const timer = setInterval(() => { if (client.presented && client.isWanted) setTick((n) => n + 1); }, 1000);
    return () => { clearInterval(timer); client.unmount(); };
  }, [client]);
  const c = client;
  const live = c.presented && !c.refusal;
  const { k, dpr, cssW, cssH } = scaleFor(c.zoom);
  const st = c.status || {};
  const badges = Array.isArray(st.badges) ? st.badges : [];
  const grey = live && badges.some((b) => b.level === "grey");
  const dim = live && badges.some((b) => b.level === "dim");
  const stale = live && st.state === "stale" && c.socket === "open";
  const scrim = live && !c.paused && (c.socket !== "open" || st.state === "reconnecting" || st.state === "connecting"
    || (st.state === "down" && !!st.reason));
  const head = viewState(c);
  const fb = live ? null : fallbackLine(c);
  const { fps, bps } = c.rates();
  const now = Date.now();
  const staleFor = stale ? Math.round(((/for (\d+) s/.exec(st.reason || "") || [])[1] | 0) + (now - c.staleAt) / 1000) : 0;
  const fresh = [
    c.drawnAt ? `updated ${agoText(now - c.drawnAt)}` : "waiting for the first picture",
    `${Math.round(fps)} fps`,
    `${bps >= 10240 ? Math.round(bps / 1024) : (bps / 1024).toFixed(1)} kB/s`,
    st.rtt_ms !== null && st.rtt_ms !== undefined ? `RTT ${Math.round(st.rtt_ms)} ms` : "RTT -",
  ];
  const holdsHatch = live && c.hatched.length > 0;
  const slotStyle = fit ? "width:100%" : `width:${cssW}px;height:${cssH}px`;
  const boxStyle = fit ? "width:100%;aspect-ratio:4 / 3" : `width:${cssW}px;height:${cssH}px`;
  const canvasStyle = fit ? "width:100%;height:100%" : `width:${cssW}px;height:${cssH}px`;
  const stateChip = html`<${Chip} level=${head.level} icon=${head.icon} testid="live-state"
    cls=${head.icon === "loader-circle" ? "spin-icon" : ""}>${head.text}<//>`;
  const modeTag = st.mode ? html`<span class="tag" data-testid="live-mode" title=${MODE_TITLE[st.mode] || ""}>${st.mode === "hw" ? "exact" : "software tap"}</span>` : null;
  const retry = !live && fb && (fb.kind === "refused" || fb.kind === "failed") ? html`<button type="button" class="btn ghost sm" data-action="live-retry"
    onClick=${() => c.retry()}><${Icon} name="refresh-cw" />Try again</button>` : null;
  return html`<div class=${`ld${fit ? " fit" : ""}`} ref=${rootRef} data-testid="live-display" data-socket=${c.socket} data-fit=${fit ? "yes" : "no"}
      data-state=${live ? st.state || "" : fb.kind} data-live=${live ? "yes" : "no"} data-seq=${c.lastSeq ?? ""}
      data-zoom=${c.zoom} data-k=${k} data-dpr=${dpr} data-hatched=${live ? c.hatched.length : 0}
      data-grey=${grey ? "yes" : "no"} data-dim=${dim ? "yes" : "no"} data-stale=${stale ? "yes" : "no"}
      data-paused=${c.paused ? "yes" : "no"} data-refused=${c.refusal ? c.refusal.name || String(c.refusal.code) : ""}>
    ${fit ? null : html`<div class="ld-head">
      <span class="ld-title"><${Icon} name="monitor" cls="sm" />Live display</span>
      ${live ? stateChip : null}
      ${live ? modeTag : null}
      <span class="spacer"></span>
      ${live ? html`<${Seg} label="Scale" value=${c.zoom} onChange=${(z) => c.setZoom(z)}
          options=${[{ value: 1, label: "1x", title: `1x: ${Math.max(1, Math.round(dpr))} device pixel(s) per panel pixel` },
            { value: 2, label: "2x", title: `2x: ${Math.max(1, Math.round(dpr * 2))} device pixels per panel pixel` }]} />`
        : retry}
    </div>`}
    ${live ? html`
      <div class="ld-scroll">
        <div class="ld-slot" style=${slotStyle}>
          <div class=${`ld-frame${stale ? " ld-stale" : ""}${grey ? " ld-grey" : ""}${dim ? " ld-dim" : ""}`}
            title=${VIEW_ONLY} data-testid="live-frame" style=${boxStyle}>
            <canvas class="ld-canvas" width=${W} height=${H} ref=${canvasRef} data-testid="live-canvas"
              role="img" aria-label=${`The board's panel, live, ${W} by ${H}. ${VIEW_ONLY}.`}
              style=${canvasStyle}></canvas>
            ${holdsHatch ? html`<${Hatches} tiles=${c.hatched} />` : null}
            ${grey ? html`<div class="ld-note" data-testid="live-grey"><span>${badges.find((b) => b.level === "grey").text}</span></div>` : null}
            ${badges.some((b) => b.level !== "grey") || stale ? html`<div class="ld-badges" data-testid="live-badges">
              ${stale ? html`<${Chip} level="warn" icon="triangle-alert" testid="live-stale">stale · no answer for ${staleFor} s<//>` : null}
              ${badges.filter((b) => b.level !== "grey").map((b) => html`<span key=${b.key} data-badge=${b.key}><${Chip}
                level=${BADGE_LEVEL[b.level] || "warn"} icon=${BADGE_ICON[b.level] || "info"}>${b.text}<//></span>`)}
            </div>` : null}
            ${scrim ? html`<div class="ld-scrim" data-testid="live-scrim"><${Spinner} />${st.state === "down" && c.socket === "open"
              ? `down: ${st.reason}` : `reconnecting${c.failure ? `: ${c.failure}` : st.reason ? `: ${st.reason}` : ""}`}</div>` : null}
          </div>
        </div>
      </div>
      <div class="ld-foot">
        ${fit ? stateChip : null}${fit ? modeTag : null}
        <p class="muted small ld-fresh" data-testid="live-freshness">${fresh.join(" · ")}</p>
        <span class="spacer"></span>
        <button type="button" class="btn ghost sm" data-action="live-pause" aria-pressed=${c.paused ? "true" : "false"}
          title=${c.paused ? "Resume the live picture" : "Pause: the board stops sending (rate 0); the picture stays"}
          onClick=${() => c.setPaused(!c.paused)}><${Icon} name=${c.paused ? "play" : "circle-pause"} />${c.paused ? "Resume" : "Pause"}</button>
        <button type="button" class="btn ghost sm" data-action="live-snapshot" aria-busy=${note.busy ? "true" : undefined}
          title="Save the board's picture now as a PNG (at this scale)" onClick=${() => snapshot(c, setNote)}>
          ${note.busy ? html`<${Spinner} />` : html`<${Icon} name="download" />`}Snapshot</button>
      </div>
      ${note.text ? html`<${Reason} level="err" text=${`Snapshot: ${note.text}`} testid="live-snapshot-error" />` : null}`
      : html`<div class="ld-why">${fb.kind === "checking" ? html`<p class="reason" data-testid="live-reason"><${Spinner} /><span>${fb.text}</span></p>`
        : html`<${Reason} level=${fb.level} icon=${fb.icon} text=${fb.text} testid="live-reason" />`}${fit ? retry : null}</div>
      ${fb.detail ? html`<details class="ld-detail" data-testid="live-detail"><summary class="muted small"><${Icon} name="chevron-right" cls="sm chev" />Details</summary>
        <p class="mono small secondary">${fb.detail}</p></details>` : null}
      ${children}`}
  </div>`;
}
