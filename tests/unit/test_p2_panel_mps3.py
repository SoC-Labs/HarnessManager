"""Lane P2: the MPS3 front panel (``harness_manager_mps3.panel``) over real sockets.

Two FakeShell profiles (tests/fakes/clcd_panel_shell.py): ``LINUX_PANEL`` answers the
design's ``hello``/``panel``/``locate`` wire (R1-R3 are not built yet, so the fake follows
docs/design/CLCD_ALIGNMENT.md exactly); ``V011_BARE_METAL`` is the fielded v0.11 shell,
which lacks them and must stay untouched (decision P3). Every check has a negative twin.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.errors import UnavailableError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.panel import COLS, ROWS, Hello, HelloLease
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.panel import Mps3Panel, rebuilt_frame
from tests.fakes.clcd_panel_shell import LINUX_PANEL, V011_BARE_METAL, PanelVirtualMps3
from tests.fakes.virtual_board import FIELDED_3F1A560F

PANEL_OPS = {"hello", "panel", "locate"}


class Board:
    def __init__(self, vb: PanelVirtualMps3, tmp_path: Path) -> None:
        self.vb = vb
        self.engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                             packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        self.session = self.engine.open(vb.candidate(), note="p2")
        self.bid = self.session.candidate.board_id

    @property
    def panel(self) -> Mps3Panel:
        return self.session.panel

    def ops(self) -> list[str]:
        return [r.get("op") for r in self.vb.shell.requests]


@pytest.fixture
def linux(tmp_path: Path) -> Iterator[Board]:
    with PanelVirtualMps3(tmp_path / "lx", LINUX_PANEL) as vb:
        b = Board(vb, tmp_path / "lx")
        yield b
        b.engine.close_all()


@pytest.fixture
def bare(tmp_path: Path) -> Iterator[Board]:
    with PanelVirtualMps3(tmp_path / "bm", V011_BARE_METAL) as vb:
        b = Board(vb, tmp_path / "bm")
        yield b
        b.engine.close_all()


HELLO = Hello(sid="a1b2c3d4", who="david@srv03335", app="hm/0.1.0", name="mps3-01",
              role="holder", lease=HelloLease(by="david@mapstone-dev", left=4332, q=1))


# --- capabilities by feature bit ----------------------------------------------------------------


def test_the_linux_harness_offers_panel_presence_and_locate(linux):
    info = linux.engine.info(linux.bid)
    assert {C.FRONT_PANEL, C.LOCATE, C.PRESENCE} <= info.capabilities
    assert linux.panel.support().source == "panel"


def test_bare_metal_v011_offers_the_panel_owner_only_with_the_reasons(bare):
    info = bare.engine.info(bare.bid)
    assert C.FRONT_PANEL in info.capabilities
    assert info.unavailable[C.LOCATE] == "needs harness feature 'locate' (Linux harness)"
    assert info.unavailable[C.PRESENCE] == "needs harness feature 'presence' (Linux harness)"
    sup = bare.panel.support()
    assert sup.source == "rebuilt" and "Linux harness" in sup.locate


def test_a_usb_only_board_has_no_panel_adapter(tmp_path):
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        eng = Engine(EngineConfig(state_dir=tmp_path / "s"), packs={"mps3": Mps3Pack()})
        cand = Candidate(pack="mps3", board_id="mps3@usb", links=(
            Link(LinkKind.USB_MSD, str(vb.sd.root), "V2M-MPS3 (fake)"),))
        session = eng.open(cand)
        try:
            assert getattr(session, "panel", None) is None
            assert C.FRONT_PANEL in eng.info("mps3@usb").unavailable
        finally:
            eng.close_all()


# --- the Linux harness: the design's wire ---------------------------------------------------------


def test_a_panel_read_gives_the_state_sessions_in_order_and_touch(linux):
    shell = linux.vb.shell
    shell.handle_control({"op": "hello", "v": 1, "sid": "w1", "who": "bob@srv03340",
                          "app": "hm", "role": "watch", "ttl": 90})
    shell.handle_control({"op": "hello", "v": 1, "sid": "h1", "who": "david@srv03335",
                          "app": "hm", "role": "holder", "ttl": 90})
    shell.tap("request")
    state = linux.panel.state()
    assert (state.page, state.owner, state.card, state.source) == ("status", "harness",
                                                                   "nanosoc [A]", "panel")
    assert [s.role for s in state.sessions] == ["holder", "watch"]
    assert [(e.seq, e.on) for e in state.events] == [(1, "request")]
    assert state.touch.present is True and state.touch.ok is None, "no stats key: unknown"


def test_touch_health_comes_from_stats_when_the_harness_sends_it(linux):
    linux.vb.shell.set_touch_health(False, bus_lost=3, recoveries=1)
    touch = linux.panel.state().touch
    assert touch.ok is False and touch.reason == "touch unavailable (bus lost 3x, 1 recovery)"
    linux.vb.shell.set_touch_health(True)
    assert linux.panel.state().touch.ok is True


def test_a_hello_reply_updates_the_model(linux):
    linux.vb.shell.tap("identify")
    state = linux.panel.hello(HELLO)
    assert state.count == 1 and state.owner == "harness" and state.seq == 1
    assert [e.on for e in state.events] == ["identify"]
    got = linux.vb.shell.hellos[-1]
    assert got["lease"] == {"by": "david", "left": 4332, "q": 1} and got["name"] == "mps3-01"
    linux.vb.shell.display_owner = linux.vb.shell.display_target = "dut"
    assert linux.panel.hello(HELLO).owner == "dut"


def test_the_board_keeps_four_sessions_and_the_host_reads_them(linux):
    for i in range(6):
        linux.panel.hello(Hello(sid=f"s{i}", who=f"u{i}@h", app="hm"))
    assert sorted(linux.vb.shell.board_sessions) == ["s2", "s3", "s4", "s5"]
    assert linux.panel.hello(HELLO).count == 4


def test_a_frame_comes_in_two_halves_and_is_the_panels(linux):
    frame = linux.panel.frame()
    assert frame.source == "panel" and len(frame.rows) == ROWS
    assert all(len(r) == COLS for r in frame.rows) and len(frame.roles) == ROWS * COLS
    assert frame.rows[4].startswith("SID : 0x14E1A2D8  USD : nanosoc [A]")
    parts = [r.get("frame") for r in linux.vb.shell.requests if r.get("op") == "panel"]
    assert parts == ["a", "b"]


def test_identify_blinks_the_board_and_zero_stops_it(linux):
    until = linux.panel.locate(10, "david@srv03335")
    assert until > 0 and linux.vb.shell.locates[-1] == {"op": "locate", "s": 10,
                                                        "who": "david@srv03335"}
    linux.panel.locate(0, "david@srv03335")
    assert linux.vb.shell.locates[-1] == {"op": "locate", "s": 0}


def test_a_harness_that_drops_a_verb_it_announced_is_read_again_and_refused(linux):
    linux.panel.hello(HELLO)
    linux.vb.shell.features = tuple(f for f in linux.vb.shell.features if f != "presence")
    with pytest.raises(UnavailableError, match="declined the hello"):
        linux.panel.hello(HELLO)                    # the cached features still said yes
    with pytest.raises(UnavailableError, match="Linux harness"):
        linux.panel.hello(HELLO)                    # read again: the bit is gone
    assert linux.panel.support().presence and "hello" in linux.ops()


def test_the_harness_refuses_a_line_over_256_bytes_and_accepts_a_capped_one(linux):
    too_long = {"op": "hello", "v": 1, "sid": "a1", "who": "w" * 300, "app": "hm",
                "role": "watch", "ttl": 90}
    assert linux.vb.shell.handle_control(too_long)["err"].startswith("line too long")
    assert linux.panel.hello(Hello(sid="a1", who="w" * 300, app="hm")).count >= 1


# --- riding a connection HM makes anyway ---------------------------------------------------------


def test_an_offered_hello_rides_the_next_connection_before_its_own_request(linux):
    got = []
    linux.panel.offer(HELLO, got.append)
    before = len(linux.vb.shell.requests)
    linux.session.identity()                          # an ordinary read (ping + version)
    ops = linux.ops()[before:]
    assert ops == ["hello", "ping", "version"] and len(got) == 1 and got[0].count == 1
    assert linux.panel.rides == 1 and linux.panel.withdraw() is False


def test_without_an_offer_connections_carry_no_hello(linux):
    before = len(linux.vb.shell.requests)
    linux.session.identity()
    linux.session.health()
    assert "hello" not in linux.ops()[before:] and linux.panel.rides == 0


def test_a_busy_connection_keeps_the_hello_offered_for_the_next_one(linux):
    from harness_manager.core.errors import HeldError

    got = []
    linux.panel.offer(HELLO, got.append)
    linux.vb.set_busy(True)                          # another client holds 6900 (EBUSY)
    with pytest.raises(HeldError):
        linux.session.identity()
    assert got == [] and linux.panel.rides == 0
    linux.vb.set_busy(False)
    linux.session.identity()
    assert len(got) == 1 and linux.panel.rides == 1, "re-armed, then carried"


def test_a_withdrawn_offer_does_not_ride(linux):
    linux.panel.offer(HELLO, lambda _s: None)
    assert linux.panel.withdraw() is True
    before = len(linux.vb.shell.requests)
    linux.session.identity()
    assert "hello" not in linux.ops()[before:]


# --- bare metal v0.11: untouched, with a fallback --------------------------------------------------


def test_bare_metal_state_is_the_kvm_owner_rebuilt(bare):
    state = bare.panel.state()
    assert state.source == "rebuilt" and state.owner == "harness" and state.page == ""
    assert "rebuilt from what Harness Manager read" in state.note
    bare.vb.shell.display_owner = bare.vb.shell.display_target = "dut"
    assert bare.panel.state().owner == "dut"


def test_bare_metal_mirror_is_rebuilt_from_what_hm_read(bare):
    frame = bare.panel.frame()
    assert frame.source == "rebuilt" and len(frame.rows) == ROWS
    assert frame.rows[4].startswith("SID : 0x72BB0A36")
    assert frame.roles == "t" * (ROWS * COLS)
    bare.vb.shell.display_owner = bare.vb.shell.display_target = "dut"
    dut = bare.panel.frame()
    assert dut.rows[6].strip() == "DUT HAS THE DISPLAY" and dut.roles[6 * COLS] == "i"


def test_bare_metal_refuses_hello_and_identify_with_the_reason_and_sends_neither(bare):
    with pytest.raises(UnavailableError) as hello:
        bare.panel.hello(HELLO)
    assert hello.value.capability == C.PRESENCE and "Linux harness" in hello.value.reason
    with pytest.raises(UnavailableError) as loc:
        bare.panel.locate(10, "david@srv03335")
    assert loc.value.capability == C.LOCATE and "Linux harness" in loc.value.reason
    with pytest.raises(UnavailableError):
        bare.panel.offer(HELLO, lambda _s: None)
    bare.panel.state()
    bare.panel.frame()
    assert not PANEL_OPS & set(bare.ops()), "bare metal is never sent a panel verb"


def test_the_linux_twin_does_send_the_panel_verbs(linux):
    linux.panel.hello(HELLO)
    linux.panel.state()
    linux.panel.locate(1, "d@h")
    assert PANEL_OPS <= set(linux.ops())


def test_a_v010_shell_reads_the_owner_and_is_not_asked_for_stats(tmp_path):
    with PanelVirtualMps3(tmp_path, V011_BARE_METAL) as vb:
        vb.shell.features = FIELDED_3F1A560F.features       # v0.10: the five, no 'stats'
        b = Board(vb, tmp_path)
        try:
            state = b.panel.state()
            assert state.owner == "harness" and state.touch.ok is None
            assert "stats" not in b.ops() and "display" in b.ops()
        finally:
            b.engine.close_all()


def test_the_v011_twin_is_asked_for_stats(bare):
    bare.vb.shell.set_touch_health(False, bus_lost=2)
    assert bare.panel.state().touch.reason == "touch unavailable (bus lost 2x)"
    assert "stats" in bare.ops()


# --- the rebuilt frame on its own ---------------------------------------------------------------


def test_the_rebuilt_frame_never_invents_what_hm_did_not_read():
    frame = rebuilt_frame(name="mps3-01", identity=None, host="192.168.10.101", owner="harness",
                          wall=1.0)
    text = "\n".join(frame.rows)
    assert "MPS3-01" in frame.rows[0] and "NET : 192.168.10.101" in text
    assert "SWAP: ?" in text and "UP  : ?" in text, "unknown facts are '?', never a guess"
    assert all(len(r) == COLS for r in frame.rows) and len(frame.rows) == ROWS


def test_the_wire_is_json_lines_the_fake_can_read():
    # The fake re-serialises what it received compactly to measure it: the host's line is
    # compact ASCII JSON, so the two lengths agree.
    from harness_manager.core.panel import encode_hello, hello_message

    line = encode_hello(HELLO)
    assert len(json.dumps(hello_message(HELLO), separators=(",", ":")).encode()) + 1 == len(line)
