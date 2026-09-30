// UI v2 routes (docs/planning/UI_V2_PLAN.md §3.3): five tabs, the Board and Build sub-pages,
// and the old tab keys, which still work everywhere (sessionStorage, setSection, the tests).
//
//   #/<board>/<tab>[/<sub>][?console=<name>&activity=err|all]
//
// The board id is URL-encoded (ids hold "@" and ":"). `#token=...` is read and stripped first
// (api.js initToken), then the route. Pure: nothing here reads the store, so any module can
// import it.

export const TABS = [
  { key: "overview", label: "Overview", icon: "gauge" },
  { key: "workbench", label: "Workbench", icon: "wrench" },
  { key: "build", label: "Build", icon: "file-cog" },
  { key: "board", label: "Board", icon: "circuit-board" },
  // HIL-GUI: the unattended runbooks; they take a hub lease, so hub boards only (plan C3)
  { key: "checks", label: "Checks", icon: "list-checks", hubOnly: true },
];

export const TAB_KEYS = TABS.map((t) => t.key);

// The sub-pages a tab has, in order; the first is where the tab opens. Build's are the
// stepper's steps (Phase 2); Board's are its six pages.
export const SUBS = {
  build: ["setup", "design", "build", "check", "add"],
  board: ["recover", "versions", "connections", "readings", "access", "about"],
};

// The twelve keys of 0.1.0 (the tab strip, sessionStorage "harness_manager.sections",
// setSection calls, the tests): where each one lands now.
export const OLD_KEYS = {
  overview: { tab: "overview" },
  xdc: { tab: "build", sub: "design" },           // the RM kit; full-board XDC under More exports
  build: { tab: "build" },
  program: { tab: "workbench", part: "program" },
  consoles: { tab: "workbench", part: "consoles", console: true },
  debug: { tab: "workbench", part: "debug" },
  power: { tab: "board", sub: "recover" },
  clocks: { tab: "board", sub: "readings" },
  sd: { tab: "board", sub: "versions" },
  update: { tab: "board", sub: "versions" },
  checks: { tab: "checks" },                       // overview on a board with no hub
  activity: { tab: "overview", activity: "board" },  // + the Activity drawer, this board
};

// A route as the store keeps it: {tab, sub, console, activity, part}. `text` is a key ("sd"),
// a route ("board/versions", "workbench?console=uart0"), or a route object. Unknown tabs land
// on the Overview; an unknown sub-page on the tab's first.
export function resolveRoute(text) {
  if (text && typeof text === "object") {
    return resolveRoute(`${text.tab || ""}${text.sub ? `/${text.sub}` : ""}${queryOf(text)}`);
  }
  const s = String(text || "").replace(/^#?\/?/, "");
  const [path, query = ""] = s.split("?");
  const parts = path.split("/").filter(Boolean);
  const q = new URLSearchParams(query);
  const key = parts[0] || "overview";
  const old = !TAB_KEYS.includes(key) ? OLD_KEYS[key] : null;
  const tab = old ? old.tab : TAB_KEYS.includes(key) ? key : "overview";
  const want = parts[1] || (old && old.sub) || "";
  const sub = SUBS[tab] && SUBS[tab].includes(want) ? want : "";
  const out = { tab, sub };
  const con = q.get("console");
  if (con) out.console = con;
  const activity = q.get("activity") || (old && old.activity) || "";
  if (activity) out.activity = activity === "err" ? "err" : "all";
  if (old && old.part) out.part = old.part;
  if (old && old.console) out.consoleKey = true;       // the board's selected console
  if (old) out.from = key;
  return out;
}

function queryOf(r) {
  const q = new URLSearchParams();
  if (r.console) q.set("console", r.console);
  if (r.activity) q.set("activity", r.activity === "err" ? "err" : "all");
  const t = q.toString();
  return t ? `?${t}` : "";
}

// "#/mps3%40192.168.10.101%3A6900/board/versions"
export function routeHash(bid, r = {}) {
  if (!bid) return "";
  const tab = r.tab || "overview";
  return `#/${encodeURIComponent(bid)}/${tab}${r.sub ? `/${r.sub}` : ""}${queryOf(r)}`;
}

// The route in a location hash: {bid, route} or null (no route there: "", "#token=...").
export function parseHash(hash) {
  const h = String(hash || "").replace(/^#/, "");
  if (!h.startsWith("/")) return null;
  const [path, query = ""] = h.slice(1).split("?");
  const parts = path.split("/");
  let bid = "";
  try { bid = decodeURIComponent(parts[0] || ""); } catch (e) { return null; }
  if (!bid) return null;
  return { bid, route: resolveRoute(`${parts.slice(1).join("/")}${query ? `?${query}` : ""}`) };
}

// The tabs a board shows: Checks only on a board behind a hub.
export function tabsFor(hub) {
  return TABS.filter((t) => !t.hubOnly || hub);
}

// The tab an Activity row belongs to, from its source (the event topic or the page's own
// label), for the drawer's "Show in ..." link. "" when no one tab owns it.
const SOURCE_ROUTES = [
  [/^(deploy|restore|program|overlays|preflight)/, "workbench"],
  [/^(console|debug|xvc)/, "workbench"],
  [/^(kit|build|guide|xdc)/, "build"],
  [/^(checks)/, "checks"],
  [/^(power|controller|reboot|reset|clock|osc)/, "board/recover"],
  [/^(update|harness|storage|sd|card|slot)/, "board/versions"],
  [/^(claim|identity|board\.net_identity)/, "board/access"],
  [/^(tunnel)/, "board/connections"],
  [/^(telemetry)/, "board/readings"],
  [/^(lease|panel|display|session|board)/, "overview"],
];

export function routeForSource(source) {
  const s = String(source || "");
  for (const [re, route] of SOURCE_ROUTES) if (re.test(s)) return route;
  return "";
}

export function tabLabel(key) {
  const t = TABS.find((x) => x.key === key);
  return t ? t.label : key;
}

// --- the tab registry: a Phase 2 lane replaces its tab's body from its own module ------------
//
// registerTab("build", BuildTab, {fill: true}) in sections/build.js swaps the Build tab's body
// without touching app.js. app.js registers today's bodies as fallbacks, which never replace a
// lane's (module order does not matter).

const registry = new Map();

export function registerTab(key, render, { fill = false, fallback = false } = {}) {
  if (!TAB_KEYS.includes(key)) throw new Error(`registerTab: no tab "${key}"`);
  if (fallback && registry.has(key)) return;
  registry.set(key, { render, fill });
}

export function tabBody(key) {
  return registry.get(key) || null;
}
