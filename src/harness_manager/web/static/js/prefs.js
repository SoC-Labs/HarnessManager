// The Settings rows the page itself reads (FIX-PACK-4; docs/USER_GUIDE.md §11 "What takes
// effect"). They were stored and shown, and read by nothing:
//   panel.identify_s      the Front panel card's Identify time (the sidebar's icon stays 5 s)
//   consoles.line_ending  what Enter sends on a console's send line (the select's default)
//   debug.hw_server_mode  the XVC card's "Bring your own hw_server" box (its default)
// One GET /settings?key= each at start, and again for the keys a settings.changed names (a
// change in the dialog, another tab or the CLI). Until an answer comes, or with a service that
// does not answer it, the built-in default stands. Nothing here reaches a board.

import { call } from "./api.js";
import { changed, onBoardEvent, onEventsReconnected } from "./store.js";

export const SETTING_DEFAULTS = {
  "panel.identify_s": 5,
  "consoles.line_ending": "crlf",
  "debug.hw_server_mode": "own",
};

const values = {};

export function settingValue(key) {
  return key in values ? values[key] : SETTING_DEFAULTS[key];
}

async function readSetting(key) {
  try {
    const { data } = await call("settings", {}, undefined, { key });
    const row = ((data && data.rows) || []).find((r) => r && r.key === key);
    if (row && row.value !== undefined && row.value !== null && values[key] !== row.value) {
      values[key] = row.value;
      changed();
    }
  } catch (e) { /* an older service, or no answer: the default stands */ }
}

export function loadSettingValues(keys = Object.keys(SETTING_DEFAULTS)) {
  return Promise.all(keys.map(readSetting));
}

onBoardEvent((ev) => {
  if (ev.topic !== "settings.changed") return;
  const keys = ((ev.data || {}).keys || []).filter((k) => k in SETTING_DEFAULTS);
  if (keys.length) loadSettingValues(keys);
});
onEventsReconnected(() => loadSettingValues());
