// The Activity drawer (UI v2): 0.1.0's Activity tab, opened from the rail's foot, over the
// page. store.js openActivity(bid, level) opens it; Escape or the backdrop closes it.

import { html, useEffect } from "./lib.js";
import { ActivitySection } from "./sections/activity.js";
import { closeActivity, S } from "./store.js";
import { Icon } from "./ui.js";

export function ActivityDrawer() {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && S.ui.drawer) closeActivity(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (!S.ui.drawer) return null;
  return html`<div class="scrim" onClick=${closeActivity}></div>
    <aside class="drawer" role="dialog" aria-label="Activity" data-testid="activity-drawer">
      <div class="drawer-head"><${Icon} name="history" /><b>Activity</b><span class="grow"></span>
        <button type="button" class="btn ghost sm icon-only" aria-label="Close Activity"
          onClick=${closeActivity}><${Icon} name="x" /></button></div>
      <div class="drawer-body"><${ActivitySection} bid=${S.selected} /></div>
    </aside>`;
}
