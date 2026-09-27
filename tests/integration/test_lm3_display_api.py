"""Lane LM3: the Live display routes (``daemon/display_api.py``) on the real daemon.

A real uvicorn (the product's ``uvicorn_config``), real WebSocket and HTTP clients, a fake
board pack whose ``display_adapter`` hook (lane LM2's shape) hands out an adapter over
LM1's ``FakeLcdMirror``. docs/design/LCD_MIRROR.md §7.2-§7.4; docs/API.md "Live display".
Every check has a negative twin.
"""

from __future__ import annotations

import json
import struct
import time
import zlib
from typing import Any

import httpx
import pytest
from websockets.exceptions import InvalidStatus

from harness_manager.core import display_wire as w
from harness_manager.core.display import HATCH_RGB565, png_rgb565, rgb888
from harness_manager.core.errors import ExitCode, HeldError
from harness_manager.daemon.app import create_app
from harness_manager.daemon.display_api import BOARD_CLOSED, close_reason
from harness_manager.daemon.server import bind_socket, uvicorn_config
from harness_manager.services.display import DisplayTimings
from tests.fakes import lm1_golden as G
from tests.fakes.lm1_fake_lcd_mirror import CardAnimator, FakeLcdMirror, FakePanel
from tests.fakes.lm3_display_rig import (
    BOARD,
    FakeClock,
    FakeDisplayAdapter,
    FakeLeases,
    Tab,
    bid_path,
    connect,
    display_rig,
)
from tests.fakes.t13_daemon import TOKEN, headers, kill, spawn, wait_for, wait_info

HELD_REASON = "needs the lease: alice@hub-02 holds mps3_02 (D3: only the lease holder sees it)"
BARE_REASON = "needs the Linux harness with lcd_mirror (this board runs the bare-metal v0.11)"


def http(rig: Any, token: str | None = TOKEN) -> httpx.Client:
    h = headers(token) if token is not None else {}
    return httpx.Client(base_url=rig.daemon.base_url, headers=h, trust_env=False, timeout=30.0)


def card_board(**kw: Any) -> tuple[FakeLcdMirror, CardAnimator]:
    anim = CardAnimator(period_s=1 / 12)
    return FakeLcdMirror(FakePanel(), animate=anim, rate_default=30, **kw), anim


def settle(board: FakeLcdMirror, tab: Tab, what: str = "exact") -> None:
    """The board has sent all it will, and the tab holds its picture, bit for bit."""
    def done(t: Tab) -> bool:
        frame, valid = board.picture()
        return board.idle() and t.vm.matches(frame, valid)

    tab.pump_until(done, what=what)


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """(w, h, RGB888 rows) of the stdlib PNG ``png_rgb565`` writes (8-bit RGB, filter 0)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(data):
        ln, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            size = struct.unpack(">II", body[:8])
            assert body[8:10] == b"\x08\x02"
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    assert size is not None
    pw, ph = size
    raw = zlib.decompress(idat)
    stride = 1 + pw * 3
    assert len(raw) == stride * ph and all(raw[i * stride] == 0 for i in range(ph))
    return pw, ph, b"".join(raw[i * stride + 1:(i + 1) * stride] for i in range(ph))


# --- the picture over the WebSocket ------------------------------------------------------------


def test_a_tab_draws_the_board_pixel_exact_and_keeps_up_with_changes():
    model, _grid, regs = G.harness_pictures()["boot"]
    with FakeLcdMirror(FakePanel(model, regs=regs), mode="sw") as board, \
            display_rig(FakeDisplayAdapter(board)) as rig, Tab(rig.daemon.display_ws()) as tab:
        settle(board, tab, "the boot screen")
        first = tab.statuses[0]
        assert {"state", "hello", "reason", "badges", "mode", "owner"} <= set(first)
        assert tab.vm.keys == 1 and w.parse_update(tab.binaries[0][w.HEADER_SIZE:]).key_first
        assert rig.status()["hello"]["static_id"] == board.static_id
        assert tab.state == "live" and tab.statuses[-1]["mode"] == "sw"
        model2, _grid2, regs2 = G.harness_pictures()["link_down"]
        with board.edit() as p:
            p.set_frame(model2)
            p.regs[:] = regs2
        settle(board, tab, "the link-down screen")
        assert bytes(tab.vm.frame) == model2
        # the twin: a tab that stopped drawing is NOT exact once the board moves on
        with Tab(rig.daemon.display_ws(), ack=False) as frozen:
            frozen.pump_until(lambda t: t.vm.messages == 1, what="the keyframe")
            with board.edit() as p:
                p.set_frame(model)
            settle(board, tab, "back to boot")
            frozen.pump(0.3)
            assert frozen.vm.messages == 1 and bytes(frozen.vm.frame) == model2 != model


def test_two_tabs_each_get_only_their_unacked_latest_tiles():
    board, anim = card_board()
    with board, display_rig(FakeDisplayAdapter(board)) as rig, \
            Tab(rig.daemon.display_ws()) as fast, Tab(rig.daemon.display_ws(), ack=False) as slow:
        slow.pump_until(lambda t: t.vm.messages == 1, what="the slow tab's keyframe")
        held = bytes(slow.vm.frame)
        start = anim.counter
        fast.pump_until(lambda t: t.vm.messages >= 8 and anim.counter >= start + 8,
                        what="the fast tab drawing the counter")
        slow.pump(0.3)
        assert slow.vm.messages == 1, "a tab that never acks is sent nothing more"
        assert rig.status()["viewers"] == 2
        anim.frozen = True
        settle(board, fast, "the fast tab on the frozen card")
        frame, _valid = board.picture()
        want = {t for t in range(w.NTILES) if w.tile_of_frame(held, t) != w.tile_of_frame(frame, t)}
        assert want, "the card changed while the slow tab held its keyframe"
        slow.send({"ack": slow.vm.seq})                       # it drew; one more message
        slow.pump_until(lambda t: t.vm.messages == 2, what="the slow tab's catch-up")
        slow.pump(0.3)
        catch_up = w.parse_update(slow.binaries[1][w.HEADER_SIZE:])
        assert slow.vm.messages == 2 and not catch_up.key
        assert {r.idx for r in catch_up.tiles} == want       # the latest, only what it lacks
        assert bytes(slow.vm.frame) == frame                  # one step to exact, never stale
        assert fast.vm.messages > slow.vm.messages
        assert board.stats["connects"] == 1                  # one upstream for both tabs


def test_bad_client_frames_get_an_error_frame_and_rate_reaches_the_board():
    board, _anim = card_board()
    with board, display_rig(FakeDisplayAdapter(board)) as rig, \
            Tab(rig.daemon.display_ws(rate="7")) as tab:
        tab.pump_until(lambda t: t.vm.messages >= 1, what="a picture")
        assert wait_for(lambda: board.stats["rate_asked"] == 7, what="RATE 7 on the board")
        def errors(t: Tab) -> list[dict[str, Any]]:
            return [s["error"] for s in t.statuses if "error" in s]

        for n, bad in enumerate(("not json", "[1]", {"rate": 999}, {"ack": "x"}, {"ack": True})):
            tab.send(bad)
            tab.pump_until(lambda t, n=n: len(errors(t)) == n + 1, what=f"the error for {bad!r}")
            assert errors(tab)[-1]["code"] == ExitCode.USAGE
        tab.send({"rate": 12})
        assert wait_for(lambda: board.stats["rate_asked"] == 12, what="RATE 12 on the board")
        tab.pump(0.2)
        assert tab.closed is None                              # the socket stays up


# --- the still: PNG and raw ------------------------------------------------------------------


def test_the_png_equals_the_picture_and_raw_is_the_panels_bytes():
    frame = G.card_picture(0x2A5)
    panel = FakePanel(frame, valid=set(range(w.NTILES)) - {0, 299})
    with FakeLcdMirror(panel) as board, display_rig(FakeDisplayAdapter(board)) as rig, \
            http(rig) as c:
        raw = c.get(f"{bid_path()}/display.png", params={"format": "raw"})
        assert raw.status_code == 200, raw.text
        assert raw.headers["content-type"] == "application/octet-stream"
        assert len(raw.content) == 153600 == w.FRAME_BYTES
        assert raw.headers["x-display-width"] == "320" and raw.headers["x-display-height"] == "240"
        assert raw.headers["x-display-format"] == "rgb565le"
        assert raw.headers["x-display-hatched"] == "2"
        held = bytearray(frame)                                 # the two invalid tiles: unknown
        for t in (0, 299):
            w.put_tile_in_frame(held, t, w.tile_of_frame(raw.content, t))
        assert raw.content == bytes(held)
        png = c.get(f"{bid_path()}/display.png")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        assert png.content == rig.svc.picture(BOARD).png() == png_rgb565(raw.content)
        pw, ph, rgb = decode_png(png.content)
        assert (pw, ph) == (320, 240)
        for x, y in ((0, 0), (17, 3), (160, 120), (319, 239)):
            v = struct.unpack_from("<H", raw.content, (y * 320 + x) * 2)[0]
            assert rgb[(y * 320 + x) * 3:(y * 320 + x) * 3 + 3] == bytes(rgb888(v))
        big = c.get(f"{bid_path()}/display.png", params={"scale": "2", "hatch": "1"})
        pw, ph, rgb2 = decode_png(big.content)
        assert (pw, ph) == (640, 480) and big.headers["x-display-scale"] == "2"
        grey = bytes(rgb888(HATCH_RGB565))
        assert rgb2[0:3] == grey                               # tile 0 hatched at (0, 0)
        assert rgb2[(0 * 640 + 2 * 20) * 3:(0 * 640 + 2 * 20) * 3 + 3] == \
            bytes(rgb888(struct.unpack_from("<H", raw.content, 20 * 2)[0]))   # tile 1: not
        # the twins: a bad ask is 400 USAGE, before any board is touched
        for q in ({"scale": "5"}, {"scale": "0"}, {"scale": "x"}, {"hatch": "maybe"},
                  {"format": "bmp"}, {"format": "raw", "scale": "2"},
                  {"format": "raw", "hatch": "1"}):
            r = c.get(f"{bid_path()}/display.png", params=q)
            assert r.status_code == 400 and r.json()["error"]["code"] == ExitCode.USAGE, q


def test_the_png_of_a_board_that_serves_nothing_says_why_within_the_wait(monkeypatch):
    from harness_manager.daemon import display_api

    monkeypatch.setattr(display_api, "PICTURE_WAIT_S", 0.5)
    with display_rig(FakeDisplayAdapter(None)) as rig, http(rig) as c:
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 422, r.text
        assert "no lcd_mirror service (test)" in r.json()["error"]["message"]


# --- refusals: D3 and the feature gate ------------------------------------------------------------


@pytest.mark.parametrize("reason", [HELD_REASON, BARE_REASON])
def test_a_refusal_is_typed_and_nothing_attaches(reason):
    board, _anim = card_board()
    adapter = FakeDisplayAdapter(board, reason=reason)
    with board, display_rig(adapter) as rig, http(rig) as c:
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["code"] == ExitCode.UNAVAILABLE and err["capability"] == "display_mirror"
        assert err["reason"] == reason
        tab = Tab(rig.daemon.display_ws())
        tab.pump(1.0)
        assert tab.closed == (4000 + ExitCode.UNAVAILABLE, close_reason(reason))
        assert tab.statuses == [{"state": "refused", "reason": reason, "error": err}]
        st = c.get(f"{bid_path()}/display").json()
        assert st["ok"] and st["available"] is False and st["unavailable"] == reason
        assert st["state"] == "down" and st["viewers"] == 0
        assert {"hello", "flags", "regs", "seq", "rtt_ms", "rate", "fps", "counters"} <= set(st)
        assert adapter.connects == 0 and board.stats["connects"] == 0 and rig.svc.boards() == []
        # the twin: the same board once the adapter says yes
        adapter.reason = ""
        assert c.get(f"{bid_path()}/display").json()["available"] is True
        with Tab(rig.daemon.display_ws()) as ok_tab:
            ok_tab.pump_until(lambda t: t.vm.messages >= 1, what="a picture once allowed")
        assert c.get(f"{bid_path()}/display.png").status_code == 200
        assert board.stats["connects"] >= 1


def behind_a_hub(rig: Any, leases: FakeLeases, target: str = "mps3_02_pl") -> None:
    from types import SimpleNamespace

    rig.engine.session(BOARD).hub = SimpleNamespace(target=target)
    rig.daemon.daemon.leases = leases                         # hub_api's lease service


def test_someone_elses_lease_is_409_held_naming_the_holder_and_nothing_attaches():
    board, _anim = card_board()
    adapter = FakeDisplayAdapter(board, reason=HELD_REASON)
    with board, display_rig(adapter) as rig, http(rig) as c:
        leases = FakeLeases("alice@hub-02", mine=False)
        behind_a_hub(rig, leases)
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["name"] == "HELD" and err["holder"] == "alice@hub-02"
        assert err["message"] == HELD_REASON and err["data"] == {"capability": "display_mirror"}
        tab = Tab(rig.daemon.display_ws())
        tab.pump(1.0)
        assert tab.closed == (4000 + ExitCode.HELD, close_reason(HELD_REASON))
        assert tab.statuses[0]["state"] == "refused" and tab.statuses[0]["error"] == err
        st = c.get(f"{bid_path()}/display").json()
        assert st["available"] is False and st["unavailable"] == HELD_REASON
        assert adapter.leases_used and all(u is leases for u in adapter.leases_used)
        assert adapter.leases_before_reason is True           # shared before it was asked
        assert adapter.connects == 0 and board.stats["connects"] == 0 and rig.svc.boards() == []
        # nobody holds it: HELD too, holder "nobody"
        leases.held = False
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 409 and r.json()["error"]["holder"] == "nobody"
        # the twins: the lease is ours, and the adapter's other reason is UNAVAILABLE ...
        leases.held, leases.mine = True, True
        adapter.reason = BARE_REASON
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 422 and r.json()["error"]["reason"] == BARE_REASON
        # ... and with none, it opens
        adapter.reason = ""
        assert c.get(f"{bid_path()}/display.png").status_code == 200
        assert board.stats["connects"] == 1


def test_a_board_that_can_never_show_it_is_422_even_when_someone_else_holds_the_lease():
    """The order (SMALL-4): can't ever (the gate) 422, then the lease 409, then the rest 422."""
    board, _anim = card_board()
    adapter = FakeDisplayAdapter(board, reason=HELD_REASON, gate=BARE_REASON)
    with board, display_rig(adapter) as rig, http(rig) as c:
        leases = FakeLeases("alice@hub-02", mine=False)
        behind_a_hub(rig, leases)
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["name"] == "UNAVAILABLE" and err["capability"] == "display_mirror"
        assert err["reason"] == BARE_REASON and "holder" not in err
        assert "alice" not in err["message"]
        tab = Tab(rig.daemon.display_ws())
        tab.pump(1.0)
        assert tab.closed == (4000 + ExitCode.UNAVAILABLE, close_reason(BARE_REASON))
        assert tab.statuses == [{"state": "refused", "reason": BARE_REASON, "error": err}]
        st = c.get(f"{bid_path()}/display").json()
        assert st["available"] is False and st["unavailable"] == BARE_REASON
        assert leases.views == 0                              # the lease was never asked
        assert adapter.connects == 0 and board.stats["connects"] == 0 and rig.svc.boards() == []
        # the twins: the gate lifted, alice's lease is 409 naming her ...
        adapter.gate = ""
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 409 and r.json()["error"]["holder"] == "alice@hub-02"
        # ... the lease ours, the adapter's claim reason is 422 ...
        leases.mine = True
        adapter.reason = "needs a claimed board"
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 422 and r.json()["error"]["reason"] == "needs a claimed board"
        # ... and with none, it opens
        adapter.reason = ""
        assert c.get(f"{bid_path()}/display.png").status_code == 200


def test_a_lease_lost_between_the_check_and_the_connect_ends_typed():
    board, _anim = card_board()
    adapter = FakeDisplayAdapter(board)
    adapter.connect_error = HeldError(HELD_REASON, holder="bob@lab")   # the MPS3 adapter's
    with board, display_rig(adapter) as rig, http(rig) as c:
        tab = Tab(rig.daemon.display_ws())
        tab.pump(2.0)
        assert tab.closed == (4000 + ExitCode.HELD, close_reason(HELD_REASON))
        assert tab.statuses[-1]["state"] == "down" and tab.statuses[-1]["reason"] == HELD_REASON
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 409 and r.json()["error"]["holder"] == "bob@lab"
        assert board.stats["connects"] == 0
        # the twin: an upstream closed with no error of the source's own ends with 1000
        adapter.connect_error = None
        tab = Tab(rig.daemon.display_ws())
        tab.pump_until(lambda t: t.vm.messages >= 1, what="a picture")
        rig.svc.close(BOARD, "closed: the lease was released")
        tab.pump(1.0)
        assert tab.closed == (1000, "closed: the lease was released")


def test_no_adapter_is_refused_too_and_a_closed_board_is_absent():
    with display_rig(None) as rig, http(rig) as c:
        r = c.get(f"{bid_path()}/display.png")
        assert r.status_code == 422 and "has no live display" in r.json()["error"]["reason"]
        tab = Tab(rig.daemon.display_ws())
        tab.pump(1.0)
        assert tab.closed and tab.closed[0] == 4012 and "has no live display" in tab.closed[1]
        assert rig.pack.hook_calls >= 2 and rig.svc.boards() == []
        st = c.get(f"{bid_path()}/display").json()
        assert st["available"] is False and "has no live display" in st["unavailable"]
        other = "fake@not-open"
        assert c.get(f"{bid_path(other)}/display").status_code == 404
        assert c.get(f"{bid_path(other)}/display.png").status_code == 404
        tab = Tab(rig.daemon.display_ws(other))
        tab.pump(1.0)
        assert tab.closed and tab.closed[0] == 4000 + ExitCode.ABSENT


def test_no_token_is_401_everywhere():
    board, _anim = card_board()
    with board, display_rig(FakeDisplayAdapter(board)) as rig:
        for token in (None, "wrong-token"):
            with http(rig, token) as c:
                for path in ("/display", "/display.png", "/display.png?format=raw"):
                    r = c.get(f"{bid_path()}{path}")
                    assert r.status_code == 401 and r.json()["error"]["name"] == "REFUSED", path
            url = rig.daemon.display_ws(token=token or "")
            with pytest.raises(InvalidStatus) as denied:
                connect(url)
            assert denied.value.response.status_code == 401
        assert board.stats["connects"] == 0 and rig.svc.boards() == []
        # the twin: the token opens all three
        with http(rig) as c:
            assert c.get(f"{bid_path()}/display").status_code == 200
            assert c.get(f"{bid_path()}/display.png").status_code == 200
        with Tab(rig.daemon.display_ws()) as tab:
            tab.pump_until(lambda t: t.vm.messages >= 1, what="a picture with the token")


# --- display.state on the events bus ------------------------------------------------------------


def events(ws: Any, timeout: float = 0.3) -> list[dict[str, Any]]:
    out = []
    while True:
        try:
            out.append(json.loads(ws.recv(timeout=timeout)))
        except TimeoutError:
            return out


def test_display_state_events_fire_on_state_changes_and_the_end_closes_the_tabs():
    frame = G.card_picture(7)
    calm = DisplayTimings(tick_s=0.02, ping_s=0.1, stale_s=30.0, dead_s=60.0)  # no load flaps
    with FakeLcdMirror(FakePanel(frame), mode="hw") as board, \
            display_rig(FakeDisplayAdapter(board), timings=calm) as rig, \
            connect(rig.daemon.events_ws()) as ev, http(rig) as c:
        time.sleep(0.3)                                  # the events socket is subscribed
        tab = Tab(rig.daemon.display_ws())
        tab.pump_until(lambda t: t.state == "live", what="live")
        seen = events(ev, 0.5)
        assert all(e["topic"] == "display.state" and e["board_id"] == BOARD for e in seen)
        states = [e["data"]["state"] for e in seen]
        assert states[:3] == ["connecting", "syncing", "live"], states
        assert set(seen[-1]["data"]) == {"state", "mode", "owner", "badges", "reason"}
        assert seen[-1]["data"]["owner"] == "harness" and seen[-1]["data"]["mode"] == "hw"
        # the twin: a second tab changes nothing the event carries: no event
        with Tab(rig.daemon.display_ws()) as second:
            second.pump_until(lambda t: t.vm.messages >= 1, what="the second tab's picture")
            assert events(ev, 0.5) == []
        # a KVM handover to the DUT: owner and badge change, one event says so
        board.handover(w.OWNER_DUT)
        with board.edit() as p:
            p.set_frame(G.card_picture(8), valid=set(range(w.NTILES)))
        got = wait_for(lambda: [e for e in events(ev, 0.1)
                                if e["data"]["owner"] == "dut"], what="the handover event")
        assert [b["key"] for b in got[-1]["data"]["badges"]] == ["held"]
        # closing the board ends the upstream: the event, then each tab's last word
        assert c.delete(bid_path()).json()["closed"] is True
        end = wait_for(lambda: [e for e in events(ev, 0.1) if e["data"]["state"] == "down"],
                       what="down")
        assert end[-1]["data"]["reason"] == BOARD_CLOSED
        tab.pump(1.0)
        assert tab.statuses[-1]["state"] == "down"
        assert sum(s["state"] == "down" for s in tab.statuses) == 1      # said once
        assert tab.closed == (1000, BOARD_CLOSED)
        assert wait_for(lambda: board.clients == 0, what="the board's connection closed")
        assert rig.svc.boards() == []


# --- permessage-deflate is off -----------------------------------------------------------------


def extensions_offered_back(url: str) -> str:
    with connect(url, compression="deflate") as ws:             # every browser offers it
        return ws.response.headers.get("Sec-WebSocket-Extensions") or ""


def test_the_daemon_process_never_negotiates_permessage_deflate(tmp_path):
    sdir = tmp_path / "svc"
    proc = spawn(sdir)
    try:
        info = wait_info(sdir, proc.pid)
        url = f"ws://127.0.0.1:{info.port}/api/v1/events?token={info.token}"
        assert extensions_offered_back(url) == ""
    finally:
        kill(proc)


def test_enabling_deflate_is_detected_by_the_same_check():
    import threading

    import uvicorn

    from harness_manager.demo import DemoEngine

    assert uvicorn_config(object()).ws_per_message_deflate is False
    engine = DemoEngine(speed=0)
    app = create_app(engine, token=TOKEN, static_dir=None)
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn_config(app, log_level="warning", ws_per_message_deflate=True))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, what="uvicorn")
        url = f"ws://127.0.0.1:{sock.getsockname()[1]}/api/v1/events?token={TOKEN}"
        assert "permessage-deflate" in extensions_offered_back(url)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        engine.close_all()


# --- the grace: the last viewer leaves, the upstream closes 30 s later ---------------------------


def test_the_upstream_closes_30_s_after_the_last_viewer_leaves_and_a_return_reuses_it():
    clock = FakeClock()
    timings = DisplayTimings(tick_s=0.02, stale_s=1e6, dead_s=1e6, hello_timeout_s=1e6,
                             key_retry_s=1e6)
    assert timings.grace_s == 30.0
    board, _anim = card_board()
    adapter = FakeDisplayAdapter(board)
    with board, display_rig(adapter, timings=timings, clock=clock) as rig:
        def viewers() -> int:
            return rig.status()["viewers"]

        with Tab(rig.daemon.display_ws()) as tab:
            tab.pump_until(lambda t: t.vm.messages >= 1, what="a picture")
        wait_for(lambda: viewers() == 0, what="the tab gone")
        clock.advance(20.0)
        time.sleep(0.3)
        assert board.clients == 1 and rig.status()["state"] == "live"
        with Tab(rig.daemon.display_ws()) as back:                  # a return within the grace
            back.pump_until(lambda t: t.vm.messages >= 1, what="a picture again")
        assert board.stats["connects"] == 1 and adapter.connects == 1   # the same upstream
        wait_for(lambda: viewers() == 0, what="the second tab gone")
        clock.advance(29.0)                                        # 49 s after the first left
        time.sleep(0.3)
        assert board.clients == 1 and rig.status()["state"] == "live", "the grace restarted"
        assert adapter.released == 0
        clock.advance(1.5)
        wait_for(lambda: board.clients == 0, what="the upstream closed")
        st = rig.status()
        assert st["state"] == "down" and st["reason"] == "closed: no viewer for 30 s"
        assert adapter.released == 1 and rig.svc.boards() == []
