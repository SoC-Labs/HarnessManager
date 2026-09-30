"""Lane LM4 in the browser: the Live display canvas (docs/design/LCD_MIRROR.md §7.4-§7.6).

david's decisions: D3 only the lease holder sees the live picture (a refusal shows today's text
mirror with the reason; 409 names the holder); D4 no touch pass-through (the picture is view
only). Over the T14 mock (the REAL display routes over a FakeLcdMirror per demo board,
``tests/fakes/lm3_mock_display.py``), whose knobs script each state; the demo daemon's own
Live display (``app --demo``) is ``test_lm4_display_demo.py``. Each check has a negative twin.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from urllib.parse import quote

import pytest

from harness_manager.core import display_wire as w
from harness_manager.core.display import (
    APPROX_TEXT,
    BLIND_TEXT,
    FMT_TEXT,
    HELD_TEXT,
    VIOL_TEXT,
    rgb888,
)
from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

#: The T14 mock only: its DisplaySim scripts each state (week_plan(sim=True) = mock only).
pytestmark = [pytest.mark.browser, pytest.mark.week_plan("display_api", sim=True)]
T = 10_000
APP = {"width": 1440, "height": 900}
BOARD = BOARD_FIELDED
HOLDER = "alice@lab-pc-07"


# --- helpers ------------------------------------------------------------------------------------


def sim(daemon):
    return daemon.app.state.display


def by_id(page, testid):
    return page.locator(f'[data-testid="{testid}"]')


def open_board(page, bid=BOARD):
    page.locator(f'.board-item[data-board="{bid}"]').click()
    page.wait_for_selector(f'main[data-board="{bid}"], [data-action="open"]', timeout=T)
    if page.locator(f'main[data-board="{bid}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    page.wait_for_selector(f'main[data-board="{bid}"] [data-testid="fact-shell"]'
                           ':not(:has-text("unknown"))', timeout=T)


def show_display(page):
    """Details open, the Live display scrolled on screen (it opens only then)."""
    if page.locator('[data-action="details"][aria-expanded="false"]').count():
        page.locator('[data-action="details"]').click()
    page.wait_for_selector('[data-testid="live-display"]', timeout=T)
    by_id(page, "live-display").scroll_into_view_if_needed()


def is_live(page, timeout=T):
    expect(by_id(page, "live-display")).to_have_attribute("data-live", "yes", timeout=timeout)
    expect(by_id(page, "live-state")).to_have_text("live", timeout=timeout)


def live_page(page_factory, scheme="light", bid=BOARD, **size):
    page = page_factory(scheme, **{**APP, **size})
    open_board(page, bid)
    show_display(page)
    return page


def get(daemon, path):
    req = urllib.request.Request(f"{daemon.url}/api/v1{path}",
                                 headers={"Authorization": f"Bearer {daemon.token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def raw_picture(daemon, bid=BOARD):
    status, headers, body = get(daemon, f"/boards/{quote(bid, safe='')}/display.png?format=raw")
    assert status == 200, body[:300]
    return int(headers["x-display-seq"]), body


def viewers(daemon, bid=BOARD):
    status, _h, body = get(daemon, f"/boards/{quote(bid, safe='')}/display")
    assert status == 200, body[:300]
    return json.loads(body)["viewers"]


CANVAS_565 = """() => {
  const c = document.querySelector('[data-testid="live-canvas"]');
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  const out = new Uint8Array(c.width * c.height * 2);
  let bad = 0;
  for (let i = 0, j = 0; i < d.length; i += 4, j += 2) {
    const v = ((d[i] >> 3) << 11) | ((d[i + 1] >> 2) << 5) | (d[i + 2] >> 3);
    out[j] = v & 255; out[j + 1] = v >> 8;
    const r5 = v >> 11, g6 = (v >> 5) & 63, b5 = v & 31;
    if (d[i] !== ((r5 << 3) | (r5 >> 2)) || d[i + 1] !== ((g6 << 2) | (g6 >> 4))
        || d[i + 2] !== ((b5 << 3) | (b5 >> 2)) || d[i + 3] !== 255) bad += 1;
  }
  let s = '';
  for (let k = 0; k < out.length; k += 8192) s += String.fromCharCode.apply(null, out.subarray(k, k + 8192));
  const root = document.querySelector('[data-testid="live-display"]');
  return { w: c.width, h: c.height, b64: btoa(s), notReplicated: bad, seq: root.dataset.seq };
}"""


def canvas_pixels(page):
    """The canvas's backing store as RGB565 LE (every pixel checked to be the bit-replicated
    RGB888 of its RGB565, so the round trip loses nothing), and the seq it last drew."""
    got = page.evaluate(CANVAS_565)
    assert (got["w"], got["h"]) == (w.W, w.H)
    assert got["notReplicated"] == 0
    return (int(got["seq"]) if got["seq"] else None), base64.b64decode(got["b64"])


def synced(page, daemon, bid=BOARD, timeout=10.0):
    """(canvas, raw) once the canvas has drawn the picture the daemon presents."""
    deadline = time.monotonic() + timeout
    while True:
        seq, raw = raw_picture(daemon, bid)
        cseq, canvas = canvas_pixels(page)
        if cseq == seq or time.monotonic() > deadline:
            return canvas, raw, seq, cseq
        time.sleep(0.1)


def wait_until(fn, timeout=8.0, page=None):
    """``fn()`` true within ``timeout``. With ``page``, the wait pumps Playwright's events (the
    sync API delivers a socket's close and its sent frames only inside its own calls)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        if page is not None:
            page.wait_for_timeout(50)
        else:
            time.sleep(0.05)
    return fn()


def display_sockets(page):
    socks = []
    page.on("websocket", lambda ws: socks.append(ws) if "/display/ws" in ws.url else None)
    return socks


def record_frames(ws, out):
    ws.on("framesent", lambda payload: out.append(payload))


# --- the picture ---------------------------------------------------------------------------------


def test_the_canvas_is_the_panel_byte_for_byte(page_factory, daemon):
    sim(daemon).freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    canvas, raw, seq, cseq = synced(page, daemon)
    assert cseq == seq and len(raw) == w.FRAME_BYTES
    assert canvas == raw                                  # byte for byte
    # the backing store is exactly 320x240 whatever the CSS size
    assert page.eval_on_selector('[data-testid="live-canvas"]', "c => [c.width, c.height]") == [320, 240]
    # the negative twin of the comparison: one changed pixel is seen
    assert canvas != raw[:1000] + bytes([raw[1000] ^ 1]) + raw[1001:]
    assert not page.errors, page.errors


def test_negative_twin_a_frozen_mirror_that_stops_answering_shows_stale(page_factory, daemon):
    sim(daemon).freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-stale", "no")
    expect(by_id(page, "live-stale")).to_have_count(0)
    sim(daemon).stall(BOARD)                              # frozen, and PINGs go unanswered
    expect(root).to_have_attribute("data-stale", "yes", timeout=8000)
    expect(by_id(page, "live-stale")).to_contain_text(re.compile(r"stale · no answer for \d+ s"))
    assert page.eval_on_selector('[data-testid="live-frame"]',
                                 "e => getComputedStyle(e).outlineStyle") == "dashed"
    expect(by_id(page, "live-state")).to_have_text("stale")
    # the canvas still holds the last picture (stale is an overlay, never a blank)
    _seq, canvas = canvas_pixels(page)
    assert canvas == raw_picture(daemon)[1]
    sim(daemon).stall(BOARD, False)
    expect(root).to_have_attribute("data-stale", "no", timeout=20_000)


def test_hatching_is_exactly_on_the_tiles_that_are_not_valid(page_factory, daemon):
    s = sim(daemon)
    s.freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-hatched", "0")      # the twin: all VALID, no hatch
    expect(page.locator(".ld-hatch")).to_have_count(0)
    m = s.mirror(BOARD)
    m.handover(w.OWNER_DUT)                               # CLCD_RST: VALID all 0
    expect(root).to_have_attribute("data-hatched", "300", timeout=T)
    expect(page.locator(".ld-hatch")).to_have_count(300)
    painted = {0, 1, 19, 20, 150, 299}
    with m.edit() as p:
        for t in painted:
            p.fill_tile(t, 0xF800)
    expect(root).to_have_attribute("data-hatched", str(300 - len(painted)), timeout=T)
    tiles = {int(x) for x in page.eval_on_selector_all(".ld-hatch", "els => els.map(e => e.dataset.tile)")}
    assert tiles == set(range(300)) - painted
    # a hatch sits on its tile: tile 21 (row 1, column 1) is 16 CSS px in at 1x
    box = page.locator('.ld-hatch[data-tile="21"]').bounding_box()
    frame = by_id(page, "live-frame").bounding_box()
    assert (round(box["x"] - frame["x"]), round(box["y"] - frame["y"])) == (16, 16)
    assert (round(box["width"]), round(box["height"])) == (16, 16)
    # the painted tiles are on the canvas, pixel for pixel (the hatch is over it, never in it)
    canvas, raw, seq, cseq = synced(page, daemon)
    assert canvas == raw
    expect(page.locator('[data-badge="held"]')).to_contain_text(HELD_TEXT)


def test_blind_greys_the_picture_and_says_why_and_hw_does_not(page_factory, daemon):
    s = sim(daemon)
    s.freeze(BOARD)
    s.mirror(BOARD).mode = "sw"                           # the interim software tap
    page = live_page(page_factory)
    is_live(page)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-grey", "no")     # the harness owns it: not blind
    expect(by_id(page, "live-mode")).to_have_text("software tap")
    canvas_filter = "e => getComputedStyle(e).filter"
    assert page.eval_on_selector('[data-testid="live-canvas"]', canvas_filter) == "none"
    m = s.mirror(BOARD)
    m.handover(w.OWNER_DUT)
    with m.edit() as p:                                   # the DUT paints: VALID again
        p.valid = set(range(w.NTILES))
    expect(root).to_have_attribute("data-grey", "yes", timeout=T)
    expect(by_id(page, "live-grey")).to_have_text(BLIND_TEXT)
    assert "grayscale(1)" in page.eval_on_selector('[data-testid="live-canvas"]', canvas_filter)
    expect(page.locator('[data-badge="held"]')).to_have_count(0)


def test_negative_twin_a_dut_owned_panel_in_hw_mode_is_held_not_grey(page_factory, daemon):
    s = sim(daemon)
    s.freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    m = s.mirror(BOARD)
    m.handover(w.OWNER_DUT)
    with m.edit() as p:
        p.valid = set(range(w.NTILES))
    expect(page.locator('[data-badge="held"]')).to_contain_text(HELD_TEXT, timeout=T)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-grey", "no")
    expect(by_id(page, "live-grey")).to_have_count(0)
    assert page.eval_on_selector('[data-testid="live-canvas"]', "e => getComputedStyle(e).filter") == "none"


def test_dim_states_show_once_they_have_held_1s_and_a_blip_never_does(page_factory, daemon):
    s = sim(daemon)
    s.freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    page.evaluate("""() => {
      const el = document.querySelector('[data-testid="live-display"]');
      window.__dimSeen = false;
      new MutationObserver(() => { if (el.dataset.dim === 'yes') window.__dimSeen = true; })
        .observe(el, { attributes: true });
    }""")
    m = s.mirror(BOARD)
    with m.edit() as p:                                   # a blip: clcd_demo's re-init
        p.csr &= ~w.ST_DISPLAY_ON
    time.sleep(0.25)
    with m.edit() as p:
        p.csr |= w.ST_DISPLAY_ON
    time.sleep(2.0)
    assert page.evaluate("window.__dimSeen") is False     # the twin: never dimmed
    expect(by_id(page, "live-display")).to_have_attribute("data-dim", "no")
    t0 = time.monotonic()
    with m.edit() as p:                                   # the backlight goes off, and stays off
        p.csr &= ~w.ST_BL
    expect(by_id(page, "live-display")).to_have_attribute("data-dim", "yes", timeout=T)
    assert time.monotonic() - t0 >= 0.9                   # not before it has held 1 s
    expect(page.locator('[data-badge="backlight_off"]')).to_have_text("backlight off")
    assert "brightness" in page.eval_on_selector('[data-testid="live-canvas"]', "e => getComputedStyle(e).filter")
    with m.edit() as p:
        p.csr |= w.ST_BL | w.ST_STANDBY
    expect(page.locator('[data-badge="standby"]')).to_have_text("panel in standby", timeout=T)
    expect(page.locator('[data-badge="backlight_off"]')).to_have_count(0)


def test_the_warning_badges_say_the_glass_may_differ(page_factory, daemon):
    s = sim(daemon)
    s.freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    expect(by_id(page, "live-badges")).to_have_count(0)   # the twin: the calibrated anchor
    with s.mirror(BOARD).edit() as p:
        p.csr |= w.ST_VIOL | w.ST_APPROX
        p.csr &= ~w.ST_FMT_OK
        p.regs[0x36] = 0x08                                # R36 off the anchor
    expect(page.locator('[data-badge="viol"]')).to_have_text(VIOL_TEXT, timeout=T)
    expect(page.locator('[data-badge="approx"]')).to_have_text(APPROX_TEXT)
    expect(page.locator('[data-badge="fmt"]')).to_have_text(FMT_TEXT)
    expect(page.locator('[data-badge="inexact"]')).to_contain_text("R36=0x08")
    expect(by_id(page, "live-mode")).to_have_text("exact")  # a hw board, even so
    expect(by_id(page, "live-display")).to_have_attribute("data-grey", "no")


# --- the fallback: refused, never an error page ----------------------------------------------------


def text_mirror_shown(page):
    expect(by_id(page, "panel-mirror")).to_be_visible(timeout=T)
    expect(by_id(page, "live-canvas")).to_have_count(0)
    expect(by_id(page, "panel-card")).to_be_visible()
    expect(page.locator('[data-testid="panel-owner"]')).to_be_visible()


def test_a_409_shows_the_text_mirror_and_names_the_holder(page_factory, daemon):
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder=HOLDER)
    page = live_page(page_factory)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-refused", "HELD", timeout=T)
    text_mirror_shown(page)
    expect(by_id(page, "live-reason")).to_contain_text(
        f"the live display is for the lease holder only: {HOLDER} holds")
    assert "held" in by_id(page, "live-reason").get_attribute("class")
    # the twin: the lease becomes mine, and the lease event brings the picture
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")
    from harness_manager.core.events import Event

    daemon.engine.bus.publish(Event("lease.state", BOARD, {"target": "mps3_01_pl", "state": "held",
                                                           "holder": "me"}))
    is_live(page)
    expect(by_id(page, "panel-mirror")).to_have_count(0)
    assert not page.errors, page.errors


def test_a_board_that_can_never_show_it_is_422_even_behind_someone_elses_lease(page_factory,
                                                                               daemon):
    """The routes' order (SMALL-4): the gate (422) before the lease (409)."""
    why = "needs the Linux harness with lcd_mirror (this board runs the bare-metal harness)"
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder=HOLDER)
    sim(daemon).gate(BOARD, why)
    page = live_page(page_factory)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-refused", "UNAVAILABLE", timeout=T)
    text_mirror_shown(page)
    reason = by_id(page, "live-reason")
    expect(reason).to_have_text(f"Live display: {why}")
    expect(reason).not_to_contain_text(HOLDER)               # no "(the lease is held by ...)"
    assert "held" not in reason.get_attribute("class")
    # the twin: the gate lifted, the same lease is 409 naming the holder
    sim(daemon).allow(BOARD)
    page.locator('[data-action="live-retry"]').click()
    expect(root).to_have_attribute("data-refused", "HELD", timeout=T)
    expect(reason).to_contain_text(f"the live display is for the lease holder only: {HOLDER} holds")
    assert "held" in reason.get_attribute("class")
    assert not page.errors, page.errors


HIDE = """() => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  document.dispatchEvent(new Event('visibilitychange'));
}"""
SHOW = HIDE.replace("'hidden'", "'visible'")


def test_a_refused_view_asks_again_when_it_comes_back_on_screen_and_not_before(page_factory, daemon):
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder=HOLDER)
    page = live_page(page_factory)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-refused", "HELD", timeout=T)
    page.evaluate(HIDE)
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")  # no event says so
    page.wait_for_timeout(2500)
    # the twin: a refused view never polls: hidden, or with no event, it asks nothing
    expect(root).to_have_attribute("data-refused", "HELD")
    assert viewers(daemon) == 0
    page.evaluate(SHOW)                                   # back on screen: asked again, once
    is_live(page)
    expect(by_id(page, "panel-mirror")).to_have_count(0)


def test_a_422_shows_the_text_mirror_and_the_reason(page_factory, daemon):
    sim(daemon).no_display(BOARD)                         # a pack with no live display for it
    page = live_page(page_factory)
    root = by_id(page, "live-display")
    expect(root).to_have_attribute("data-refused", "UNAVAILABLE", timeout=T)
    text_mirror_shown(page)
    expect(by_id(page, "live-reason")).to_contain_text("has no live display for this board")
    # the adapter says why not (the bare-metal harness): its reason, word for word
    why = "needs the Linux harness with lcd_mirror (this board runs the bare-metal harness)"
    sim(daemon).allow(BOARD)
    sim(daemon).refuse(BOARD, why)
    page.locator('[data-action="live-retry"]').click()
    expect(by_id(page, "live-reason")).to_have_text(f"Live display: {why}", timeout=T)
    text_mirror_shown(page)
    # the twin: allowed, Try again brings the picture
    sim(daemon).allow(BOARD)
    page.locator('[data-action="live-retry"]').click()
    is_live(page)
    expect(by_id(page, "live-reason")).to_have_count(0)
    expect(by_id(page, "panel-mirror")).to_have_count(0)
    assert not page.errors, page.errors


# --- the socket is open only while the Live display is on screen ---------------------------------


def test_the_socket_closes_when_the_display_is_hidden_and_the_tab_backgrounded(page_factory, daemon):
    page = page_factory("light", **APP)
    socks = display_sockets(page)
    open_board(page)
    assert socks == []                                    # Details closed: no socket at all
    show_display(page)
    is_live(page)
    assert len(socks) == 1 and not socks[0].is_closed()
    page.wait_for_timeout(3000)                           # the twin: it stays open while visible
    assert not socks[0].is_closed() and wait_until(lambda: viewers(daemon) == 1, page=page)
    page.locator('[data-action="details"]').click()      # hidden: Details collapsed
    assert wait_until(socks[0].is_closed, page=page)
    assert wait_until(lambda: viewers(daemon) == 0, page=page)
    show_display(page)                                    # shown again: a new socket
    is_live(page)
    assert len(socks) == 2 and not socks[1].is_closed()
    page.evaluate("""() => {                              // the tab goes to the background
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    assert wait_until(socks[1].is_closed, page=page)
    expect(by_id(page, "live-display")).to_have_attribute("data-socket", "closed")
    assert wait_until(lambda: viewers(daemon) == 0, page=page)
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    is_live(page)
    assert len(socks) == 3
    nav.tab(page, "workbench")                           # another tab: unmounted
    assert wait_until(socks[2].is_closed, page=page)
    assert wait_until(lambda: viewers(daemon) == 0, page=page)


def test_negative_twin_scrolled_away_it_closes_and_on_screen_it_opens(page_factory, daemon):
    page = page_factory("light", width=1440, height=380)
    socks = display_sockets(page)
    open_board(page)
    show_display(page)
    is_live(page)
    assert len(socks) == 1
    page.evaluate("""() => {                              // back to the top: the card is below it
      let el = document.querySelector('[data-testid="live-display"]');
      for (; el; el = el.parentElement) if (el.scrollHeight > el.clientHeight + 4) el.scrollTop = 0;
      window.scrollTo(0, 0);
    }""")
    assert page.evaluate("""() => {
      const r = document.querySelector('[data-testid="live-display"]').getBoundingClientRect();
      return r.top >= innerHeight || r.bottom <= 0;
    }""")
    assert wait_until(socks[0].is_closed, page=page)                 # scrolled away: closed
    assert wait_until(lambda: viewers(daemon) == 0, page=page)
    by_id(page, "live-display").scroll_into_view_if_needed()
    is_live(page)                                         # on screen again: a new socket
    assert len(socks) == 2 and not socks[1].is_closed()


# --- scale: whole device pixels --------------------------------------------------------------------


BLOCKS = """async ([b64, k, dpr]) => {
  // Every panel pixel must be a k x k block of identical device pixels in the screenshot, equal
  // to the canvas's own pixel. Blink snaps a replaced element's box to whole device pixels;
  // the snapped origin is searched within 2 device pixels of the layout box.
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const bmp = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
  const oc = new OffscreenCanvas(bmp.width, bmp.height);
  const g = oc.getContext('2d');
  g.drawImage(bmp, 0, 0);
  const shot = g.getImageData(0, 0, bmp.width, bmp.height).data;
  const c = document.querySelector('[data-testid="live-canvas"]');
  const src = c.getContext('2d').getImageData(0, 0, 320, 240).data;
  const r = c.getBoundingClientRect();
  const x0 = r.left * dpr, y0 = r.top * dpr;
  const count = (ox, oy, limit) => {
    let bad = 0;
    for (let y = 0; y < 240; y += 1) {
      for (let x = 0; x < 320; x += 1) {
        const s = (y * 320 + x) * 4;
        for (let dy = 0; dy < k; dy += 1) {
          for (let dx = 0; dx < k; dx += 1) {
            const q = ((oy + y * k + dy) * bmp.width + (ox + x * k + dx)) * 4;
            if (shot[q] !== src[s] || shot[q + 1] !== src[s + 1] || shot[q + 2] !== src[s + 2]) {
              bad += 1;
              if (bad > limit) return bad;
            }
          }
        }
      }
    }
    return bad;
  };
  let best = null;
  for (let oy = Math.floor(y0) - 2; oy <= Math.ceil(y0) + 2; oy += 1) {
    for (let ox = Math.floor(x0) - 2; ox <= Math.ceil(x0) + 2; ox += 1) {
      const bad = count(ox, oy, best ? best.bad : 1e9);
      if (!best || bad < best.bad) best = { ox, oy, bad };
    }
  }
  return { bad: best.bad, ox: best.ox, oy: best.oy, x0, y0, checked: 320 * 240 * k * k,
    cssW: r.width, cssH: r.height, shotW: bmp.width };
}"""


@pytest.mark.parametrize("dpr", [1, 2])
def test_1x_and_2x_are_whole_device_pixels_per_panel_pixel(browser, daemon, dpr):
    sim(daemon).freeze(BOARD)
    ctx = browser.new_context(viewport={"width": 1920, "height": 1200}, device_scale_factor=dpr,
                              reduced_motion="reduce")
    try:
        page = ctx.new_page()
        page.goto(daemon.ui_url)
        open_board(page)
        show_display(page)
        is_live(page)
        synced(page, daemon)
        for zoom in (1, 2):
            page.locator(f'[data-testid="live-display"] .seg button:has-text("{zoom}x")').click()
            k = max(1, round(dpr * zoom))
            root = by_id(page, "live-display")
            expect(root).to_have_attribute("data-k", str(k))
            by_id(page, "live-canvas").evaluate("e => e.scrollIntoView({block: 'center'})")
            page.wait_for_timeout(300)
            shot = base64.b64encode(page.screenshot()).decode()
            got = page.evaluate(BLOCKS, [shot, k, dpr])
            assert (got["cssW"], got["cssH"]) == (320 * k / dpr, 240 * k / dpr)
            assert got["checked"] == 320 * 240 * k * k and got["bad"] == 0, json.dumps(got)
            # the negative twin: read as k+1 blocks, the same shot does not match
            wrong = page.evaluate(BLOCKS, [shot, k + 1, dpr])
            assert wrong["bad"] > 0
        page.locator('[data-testid="live-display"] .seg button:has-text("1x")').click()
    finally:
        ctx.close()


# --- snapshot, pause, view only ------------------------------------------------------------------


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_snapshot_downloads_a_png_at_the_scale_shown(page_factory, daemon):
    sim(daemon).freeze(BOARD)
    page = live_page(page_factory)
    is_live(page)
    with page.expect_download() as dl:
        page.locator('[data-action="live-snapshot"]').click()
    d = dl.value
    assert re.fullmatch(r"[\w.-]+-live-display-\d{8}-\d{6}\.png", d.suggested_filename), d.suggested_filename
    assert png_size(open(d.path(), "rb").read()) == (320, 240)
    page.locator('[data-testid="live-display"] .seg button:has-text("2x")').click()
    with page.expect_download() as dl2:
        page.locator('[data-action="live-snapshot"]').click()
    assert png_size(open(dl2.value.path(), "rb").read()) == (640, 480)
    expect(by_id(page, "live-snapshot-error")).to_have_count(0)
    page.locator('[data-testid="live-display"] .seg button:has-text("1x")').click()
    # the twin: a refused board's snapshot says why (the button is on the picture only)
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder=HOLDER)
    status, _h, body = get(daemon, f"/boards/{quote(BOARD, safe='')}/display.png")
    assert status == 409 and json.loads(body)["error"]["holder"] == HOLDER


def test_pause_asks_rate_0_and_holds_the_picture(page_factory, daemon):
    page = page_factory("light", **APP)
    socks = display_sockets(page)
    open_board(page)
    show_display(page)
    is_live(page)
    sent = []
    record_frames(socks[-1], sent)
    root = by_id(page, "live-display")
    seq0 = int(root.get_attribute("data-seq"))
    assert wait_until(lambda: int(root.get_attribute("data-seq") or 0) > seq0)   # the card counts
    page.locator('[data-action="live-pause"]').click()
    expect(by_id(page, "live-state")).to_have_text("paused")
    assert wait_until(lambda: '{"rate":0}' in sent, page=page)
    time.sleep(0.6)
    held = root.get_attribute("data-seq")
    _s, before = canvas_pixels(page)
    time.sleep(1.5)
    assert root.get_attribute("data-seq") == held          # nothing drawn while paused
    assert canvas_pixels(page)[1] == before
    page.locator('[data-action="live-pause"]').click()     # the twin: resume
    assert wait_until(lambda: '{"rate":20}' in sent, page=page)
    assert wait_until(lambda: root.get_attribute("data-seq") != held)
    expect(by_id(page, "live-state")).to_have_text("live")


def test_clicks_on_the_picture_do_nothing_to_the_board(page_factory, daemon):
    """D4: view only. A click sends nothing (no frame but acks, no request)."""
    sim(daemon).freeze(BOARD)
    page = page_factory("light", **APP)
    socks = display_sockets(page)
    open_board(page)
    show_display(page)
    is_live(page)
    sent = []
    record_frames(socks[-1], sent)
    requests = []
    page.on("request", lambda r: requests.append((r.method, r.url)))
    canvas = by_id(page, "live-canvas")
    assert page.eval_on_selector('[data-testid="live-canvas"]', "e => getComputedStyle(e).cursor") == "default"
    assert by_id(page, "live-frame").get_attribute("title").startswith("View only")
    for pos in ({"x": 10, "y": 10}, {"x": 160, "y": 120}, {"x": 300, "y": 230}):
        canvas.click(position=pos)
    canvas.dblclick(position={"x": 50, "y": 50})
    page.wait_for_timeout(1200)
    assert all(re.fullmatch(r'\{"ack":\d+\}', f) for f in sent), sent
    assert not [r for r in requests if "/display" in r[1] or "/panel" in r[1] or r[0] != "GET"], requests
    # the twin: the recorder does see what the page sends (Pause is {"rate": 0})
    page.locator('[data-action="live-pause"]').click()
    assert wait_until(lambda: '{"rate":0}' in sent, page=page)


# --- the decoder: every encoding, the board's own vectors, and malformed frames -------------------


def rgba_of(rgb565: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(rgb565), 2):
        out += bytes(rgb888(rgb565[i] | rgb565[i + 1] << 8)) + b"\xff"
    return bytes(out)


DECODE = """async ([frames]) => {
  const m = await import('./js/display.js');
  const px = new Uint32Array(320 * 240);
  const out = [];
  for (const b64 of frames) {
    const u8 = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    try {
      const msg = m.parseUpdate(u8);
      m.decodeInto(msg, px);
      out.push({ ok: true, seq: msg.seq, ntiles: msg.ntiles, key: !!(msg.status & m.S_KEY) });
    } catch (e) {
      out.push({ ok: false, name: e.constructor.name, err: String(e.message) });
    }
  }
  const b = new Uint8Array(px.buffer);
  let s = '';
  for (let k = 0; k < b.length; k += 8192) s += String.fromCharCode.apply(null, b.subarray(k, k + 8192));
  return { out, rgba: btoa(s) };
}"""


def test_the_js_decoder_matches_the_wire_on_every_encoding(page_factory, daemon):
    import os

    page = page_factory("light", **APP)
    page.wait_for_selector(".board-item", timeout=T)
    frame = bytearray(os.urandom(w.FRAME_BYTES))
    # tiles that encode as each of the five encodings, and a keyframe of them all
    def solid(t, c):
        w.put_tile_in_frame(frame, t, c.to_bytes(2, "little") * 256)

    solid(0, 0x1234)                                                # FILL
    w.put_tile_in_frame(frame, 1, b"".join((0xF800 if (x + y) % 3 else 0x001F).to_bytes(2, "little")
                                            for y in range(16) for x in range(16)))       # PAL1
    w.put_tile_in_frame(frame, 2, b"".join((0x07E0, 0xF800, 0x001F, 0xFFFF)[(x * y) % 4].to_bytes(2, "little")
                                            for y in range(16) for x in range(16)))       # PAL2
    w.put_tile_in_frame(frame, 3, b"".join((0x1111 if x < 9 else 0x2222 + y).to_bytes(2, "little")
                                            for y in range(16) for x in range(16)))       # RLE16
    recs = w.encode_records(frame, range(w.NTILES))
    encs = {w.TILE_RECORD.unpack_from(r)[1] for r in recs}
    assert encs == {w.E_FILL, w.E_PAL1, w.E_PAL2, w.E_RLE16, w.E_RAW}
    regs = bytes(256)
    key = w.update_msg(1, 0, 0, 0, w.S_KEY_BITS | w.S_SNAP_LAST, 0, w.ALL_VALID, recs, regs=regs)
    got = page.evaluate(DECODE, [[base64.b64encode(key).decode()]])
    assert got["out"] == [{"ok": True, "seq": 1, "ntiles": 300, "key": True}]
    assert base64.b64decode(got["rgba"]) == rgba_of(bytes(frame))
    # malformed frames throw (the stream is then dropped); the twin above did not
    delta = w.update_msg(2, 0, 0, 0, w.S_SNAP_LAST, 0, w.ALL_VALID, [w.record(5, w.E_FILL, b"\x00\xf8")])
    rle_short = w.update_msg(3, 0, 0, 0, 0, 0, w.ALL_VALID, [w.record(6, w.E_RLE16, b"\x80\x00\x00")])
    rle_over = w.update_msg(3, 0, 0, 0, 0, 0, w.ALL_VALID, [w.record(6, w.E_RLE16, b"\xff\x00\x00" * 3)])
    bad = [delta[:-1], delta + b"\x00", w.update_msg(4, 0, 0, 0, 0, 0, w.ALL_VALID, [w.record(7, 9, b"\x00\x00")]),
           w.update_msg(5, 0, 0, 0, 0, 0, w.ALL_VALID, [w.record(8, w.E_FILL, b"\x00")]),
           rle_short, rle_over, b"XX" + delta[2:]]
    got = page.evaluate(DECODE, [[base64.b64encode(m).decode() for m in [delta, *bad]]])
    assert got["out"][0]["ok"] is True
    assert [o["ok"] for o in got["out"][1:]] == [False] * len(bad), got["out"]
    assert {o["name"] for o in got["out"][1:]} == {"WireError"}


def test_the_js_decoder_reads_the_boards_own_wire_vectors(page_factory, daemon):
    import zlib
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "fixtures" / "lcdmirror_wire"
    manifest = json.loads((here / "manifest.json").read_text())
    page = page_factory("light", **APP)
    page.wait_for_selector(".board-item", timeout=T)
    for v in manifest["vectors"]:
        if v["kind"] == "update":
            got = page.evaluate(DECODE, [[base64.b64encode((here / v["file"]).read_bytes()).decode()]])
            assert got["out"][0]["ok"] and got["out"][0]["seq"] == v["seq"], got["out"]
            rgba = base64.b64decode(got["rgba"])
            for idx, _enc, _ln, crc in v["tiles"]:
                x0, y0 = w.tile_origin(idx)
                tile = b"".join(rgba[((y0 + r) * w.W + x0) * 4:((y0 + r) * w.W + x0 + 16) * 4] for r in range(16))
                px = bytearray()
                for i in range(0, len(tile), 4):
                    v565 = ((tile[i] >> 3) << 11) | ((tile[i + 1] >> 2) << 5) | (tile[i + 2] >> 3)
                    px += v565.to_bytes(2, "little")
                assert zlib.crc32(bytes(px)) == crc, v["file"]
        elif v["kind"] == "keyframe":
            parts = [base64.b64encode((here / f).read_bytes()).decode() for f in v["parts"]]
            got = page.evaluate(DECODE, [parts])
            assert [o["ok"] for o in got["out"]] == [True] * len(parts)
            assert got["out"][0]["seq"] == v["seq_first"]
            rgba = base64.b64decode(got["rgba"])
            px = bytearray()
            for i in range(0, len(rgba), 4):
                v565 = ((rgba[i] >> 3) << 11) | ((rgba[i + 1] >> 2) << 5) | (rgba[i + 2] >> 3)
                px += v565.to_bytes(2, "little")
            assert zlib.crc32(bytes(px)) == v["frame_crc"]
            # the twin: one part missing is a different picture
            got2 = page.evaluate(DECODE, [parts[:-1]])
            rgba2 = base64.b64decode(got2["rgba"])
            assert rgba2 != rgba


def test_a_board_with_a_usb_only_link_still_opens_its_card(page_factory, daemon):
    """Another demo board: the mock gives it a live display too (the Live display is per board)."""
    sim(daemon).freeze(BOARD_USB)
    page = live_page(page_factory, bid=BOARD_USB)
    is_live(page)
    canvas, raw, seq, cseq = synced(page, daemon, BOARD_USB)
    assert canvas == raw


# --- performance: a keyframe's decode + putImageData, well under a frame at 12 fps ----------------

FRAME_12FPS_MS = 1000 / 12
BENCH = """async ([vectors, iters]) => {
  const m = await import('./js/display.js');
  const canvas = document.createElement('canvas');
  canvas.width = 320; canvas.height = 240;
  const ctx = canvas.getContext('2d');
  const px = new Uint32Array(320 * 240);
  const img = new ImageData(new Uint8ClampedArray(px.buffer), 320, 240);
  const stats = (a) => {
    const s = [...a].sort((x, y) => x - y);
    const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
    return { median: q(0.5), p95: q(0.95), max: s[s.length - 1] };
  };
  const out = {};
  for (const [name, b64] of Object.entries(vectors)) {
    const u8 = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const dec = [], put = [], tot = [], flushed = [];
    let first = null;
    for (let i = 0; i < iters; i += 1) {
      const t0 = performance.now();
      const msg = m.parseUpdate(u8);
      m.decodeInto(msg, px);
      const t1 = performance.now();
      ctx.putImageData(img, 0, 0);
      const t2 = performance.now();
      ctx.getImageData(0, 0, 1, 1);              // the put has really landed
      const t3 = performance.now();
      if (first === null) first = { decode: t1 - t0, put: t2 - t1, total: t2 - t0 };
      dec.push(t1 - t0); put.push(t2 - t1); tot.push(t2 - t0); flushed.push(t3 - t0);
    }
    out[name] = { bytes: u8.length, iters, first, decode: stats(dec), put: stats(put),
      total: stats(tot), total_with_readback: stats(flushed) };
  }
  return out;
}"""


def keyframe(frame: bytes) -> bytes:
    """A frame as the daemon's first viewer message: every tile, REGS, all the key bits."""
    return w.update_msg(1, 0, 1, 0, w.S_KEY_BITS | w.S_SNAP_LAST | w.ST_BL | w.ST_DISPLAY_ON,
                        0, w.ALL_VALID, w.encode_records(frame, range(w.NTILES)), regs=bytes(256))


def test_a_keyframe_decodes_and_draws_well_under_a_frame_at_12fps(page_factory, daemon, screenshots):
    import os

    from harness_manager.demo_display import DemoLcdPanel
    from harness_manager.demo_showcase import SWAP_ROWS_LX
    from tests.fakes.lm1_golden import card_picture

    vectors = {
        "noise (300 RAW tiles, the worst case)": keyframe(os.urandom(w.FRAME_BYTES)),
        "clcd_demo card": keyframe(card_picture(1234)),
        "harness status page (the demo's)": keyframe(
            DemoLcdPanel(tuple(r.format(who="you@host") for r in SWAP_ROWS_LX)).render(0)),
    }
    page = page_factory("light", **APP)
    page.wait_for_selector(".board-item", timeout=T)
    got = page.evaluate(BENCH, [{k: base64.b64encode(v).decode() for k, v in vectors.items()}, 60])
    # and the app's own counters for the real first keyframe over the socket
    sim(daemon).freeze(BOARD)
    open_board(page)
    show_display(page)
    is_live(page)
    app = page.evaluate("window.__harness_managerDisplay()")[0]["perf"]
    report = {"browser": page.context.browser.version, "frame_12fps_ms": round(FRAME_12FPS_MS, 1),
              "bench": got, "app_first_keyframe": app}
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    (out / "display-perf.json").write_text(json.dumps(report, indent=1))
    for name, r in got.items():
        assert r["total"]["median"] < FRAME_12FPS_MS / 4, (name, r)       # well under: < 21 ms
        assert r["total"]["p95"] < FRAME_12FPS_MS, (name, r)
    assert app["keyMs"] < FRAME_12FPS_MS, app
