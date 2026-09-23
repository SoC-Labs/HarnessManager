// Clocks and Board & XDC: placeholders that still say what this board can do today.

import { capState, capTitle } from "../format.js";
import { html } from "../lib.js";
import { boardState } from "../store.js";
import { Card, Chip, Icon, Reason } from "../ui.js";

function CapabilityLines({ bid, caps }) {
  const b = boardState(bid);
  return html`<ul class="caps-missing">${caps.map((c) => {
    const st = capState(b.info, c);
    return html`<li key=${c} data-capability=${c}>
      <div class="cap-title">${st && st.available ? html`<${Icon} name="circle-check" cls="sm i-ok" />`
        : html`<${Icon} name="circle-slash" cls="sm i-muted" />`}${capTitle(c)}
        ${st && st.available ? html`<${Chip} level="ok">available on this board<//>` : null}</div>
      ${st && !st.available ? html`<${Reason} text=${st.reason} />` : null}
      ${!st ? html`<${Reason} text="waiting for the board's capability view" />` : null}
    </li>`;
  })}</ul>`;
}

function Placeholder({ bid, title, icon, text, caps, testid }) {
  return html`<div class="grid split">
    <${Card} title=${title} icon=${icon} testid=${testid}>
      <div class="placeholder-art"><${Icon} name=${icon} cls="lg" /></div>
      <p class="secondary">${text}</p>
      ${caps.length ? html`<div class="mt-14"><p class="sub-head">On this board</p>
        <${CapabilityLines} bid=${bid} caps=${caps} /></div>` : null}
    <//>
  </div>`;
}

export function ClocksSection({ bid }) {
  return html`<${Placeholder} bid=${bid} title="Clocks" icon="clock" testid="clocks-card"
    text="The clock controls are not in this build. DUT clock presets (25, 50 and 100 MHz) arrive with the clock verb; board oscillators with the board-controller driver. The current DUT clock and oscillator readings are on the Overview, each with its source."
    caps=${["clock_dut", "clock_board"]} />`;
}

export function BoardXdcSection({ bid }) {
  return html`<${Placeholder} bid=${bid} title="Board & XDC" icon="file-code" testid="xdc-card"
    text="XDC export is not in this build. It arrives in Wave 2 (team T10): the RM kit (an out-of-context XDC by boundary group, a connectivity sheet, pblock facts) and the full-board three-file export."
    caps=${[]} />`;
}
