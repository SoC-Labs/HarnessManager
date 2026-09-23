// Small pure helpers: levels, titles, numbers, time. No DOM, no state.

// Capability titles. The board pack owns them (CapabilitySpec.title), but API v1 does not
// serve them yet (contract change request T14-1), so the shared vocabulary and the MPS3
// pack's own names are mirrored here. An unknown name is shown as it is spelled.
export const CAPABILITY_TITLES = {
  identify: "Identify the harness",
  health: "Health counters",
  deploy_partial: "Program a partition",
  console_dut: "DUT consoles (UART0/UART1/SWO)",
  console_shell: "Shell console",
  console_controller: "Board controller (MCC) console",
  debug_dut: "Debug the DUT CPU (OpenOCD)",
  debug_fabric: "Debug the fabric",
  reset_dut: "Reset the DUT",
  reset_shell: "Restart the shell",
  reboot_board: "Reboot the board (reload from SD)",
  clock_dut: "Set the DUT clock",
  clock_board: "Board oscillators",
  telemetry_temp: "Temperatures",
  telemetry_power: "Board power",
  storage_backup: "Back up the configuration SD",
  storage_install: "Install a harness onto the SD",
  discover_network: "Find boards on the network",
  power_cycle: "Power-cycle the board (cold)",
  "mps3.display_flip": "Hand the CLCD panel to the DUT",
  "mps3.dut_egress": "Read frames the DUT transmitted",
};

export const CAPABILITY_ORDER = Object.keys(CAPABILITY_TITLES);

export function capTitle(name) { return CAPABILITY_TITLES[name] || name; }

export function capState(info, name) {
  if (!info) return null;
  const caps = info.capabilities || [];
  if (caps.includes(name)) return { available: true, reason: "" };
  const why = (info.unavailable || {})[name];
  return { available: false, reason: why || "not offered by this board pack" };
}

// Three states, three looks. UNCHECKED is its own neutral warning, never "ok".
export function checkLevel(check) {
  if (check === "ok") return "ok";
  if (check === "mismatch") return "err";
  return "unk";
}

export function checkLabel(check) {
  if (check === "ok") return "OK";
  if (check === "mismatch") return "Mismatch";
  return "Unchecked";
}

export function checkIcon(check) {
  if (check === "ok") return "circle-check";
  if (check === "mismatch") return "circle-x";
  return "circle-help";
}

export function healthOf(info) {
  if (!info || !info.health) return { level: "unk", text: "Unknown", detail: "not read yet" };
  const h = info.health;
  if (!h.reachable) return { level: "err", text: "Unreachable", detail: "the harness did not answer" };
  const cc = h.control_channel || "unknown";
  const map = {
    idle: ["ok", "Healthy"],
    busy: ["warn", "Busy"],
    wedged: ["err", "Wedged"],
    offline: ["err", "Offline"],
    rescue: ["warn", "Rescue"],
  };
  const [level, text] = map[cc] || ["unk", "Unknown"];
  return { level, text, detail: `control channel ${cc}` };
}

export const LINK_NAMES = {
  ethernet: "Ethernet",
  usb_serial: "USB serial",
  usb_msd: "USB storage",
  usb_debug: "USB debug",
  jtag: "JTAG",
  hub: "Hub",
  smart_power: "Smart plug",
  ssh: "SSH",
};

export const LINK_ICONS = {
  ethernet: "ethernet-port",
  usb_serial: "usb",
  usb_msd: "hard-drive",
  usb_debug: "bug",
  jtag: "cable",
  hub: "server",
  smart_power: "plug-zap",
  ssh: "terminal",
};

export function linkName(kind) { return LINK_NAMES[kind] || kind; }

// Link.via: "" direct, "ssh" (an SSH port-forward), "hub" (an fpgahub tunnel). TCP only.
export const VIA_NAMES = { ssh: "via SSH tunnel", hub: "via hub tunnel" };


export function hostOf(boardId) {
  const at = String(boardId || "").indexOf("@");
  return at >= 0 ? boardId.slice(at + 1) : String(boardId || "");
}

export function boardTitle(cand, boardId) {
  const pack = (cand && cand.pack) || String(boardId || "").split("@")[0] || "board";
  return `${pack.toUpperCase()} ${hostOf(boardId || (cand && cand.board_id))}`;
}

export function designText(ident) {
  if (!ident) return "";
  return ident.rm_name || ident.rm_id || "";
}

export function secs(t) {
  if (!Number.isFinite(t)) return "?";
  return t < 10 ? t.toFixed(1) : String(Math.round(t));
}

export function elapsedSince(start, now = Date.now()) {
  return Math.max(0, (now - start) / 1000);
}

export function kib(n) {
  if (!n) return "?";
  return `${Math.round(n / 1024).toLocaleString("en-GB")} KiB`;
}

export function bytesText(n) {
  if (!Number.isFinite(n)) return "?";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MiB`;
  if (n >= 1024) return `${Math.round(n / 1024).toLocaleString("en-GB")} KiB`;
  return `${n} B`;
}

export function ageText(observedAt, now = Date.now() / 1000) {
  const age = Math.max(0, now - observedAt);
  if (age < 2) return "just now";
  if (age < 120) return `${Math.round(age)} s ago`;
  if (age < 7200) return `${Math.round(age / 60)} min ago`;
  return `${(age / 3600).toFixed(1)} h ago`;
}

export function clock(at) {
  const d = at ? new Date(at * 1000) : new Date();
  return d.toTimeString().slice(0, 8);
}

const UNITS = { degC: "°C", V: "V", W: "W", MHz: "MHz", A: "A", mV: "mV" };

export function valueText(r) {
  if (r.value === null || r.value === undefined) return "";
  const v = Number(r.value);
  const text = Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
  const unit = UNITS[r.unit] || r.unit || "";
  return unit ? `${text} ${unit}` : text;
}

export function holderText(h) {
  if (!h) return "";
  const who = `${h.user || "?"} on ${h.host || "?"}`;
  const pid = h.pid ? ` (pid ${h.pid})` : "";
  const note = h.note ? `: ${h.note}` : "";
  return `${who}${pid}${note}`;
}

export function holderAge(h, now = Date.now() / 1000) {
  if (!h || !h.since) return "";
  const s = Math.max(0, now - h.since);
  if (s < 120) return `${Math.round(s)} s`;
  if (s < 7200) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

export function journalText(j) {
  if (!j) return "";
  const who = j.pid ? ` by pid ${j.pid} on ${j.host || "?"}` : "";
  let text = `${j.op || "?"} ${j.state || "?"}${who}`;
  if (j.current) text += `; was writing ${j.current}`;
  if (j.error) text += `; ${j.error}`;
  return text;
}

