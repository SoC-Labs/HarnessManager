"""PANEL-V017: Harness Manager's side of net-protocol v0.17 as the Linux harness SHIPPED it
(platform ``feat/panel-aligned``: 33ec49d the clcd side, f0f5d6f the protocol side).

The Linux lead's P1/P2 run found HM's code and fakes out of step with the board. Each item
here has its test and a negative twin:

1. role codes are ``chr(ord("a") + i)`` over design/tokens.json ``panel.roles`` (one
   vocabulary, GENERATED: ``tools/gen_panel_codes.py``), glyphs 0x80-0x86 from the panel's
   glyph table; rebuilt frames use the same letters;
2. ``frame:true`` is refused (``invalid``) and HM never sends it;
3. a line over 256 characters is ``bad json`` (tests/unit/test_p2_panel_mps3.py);
4. a hello refusal is ``code`` ``invalid`` and HM keeps the feature list;
5. the hello reply's ``panel.banner`` may lag by one 250 ms refresh;
6. row 0 is the board's own label (the design says so);
7. the board does not rate-limit; HM keeps its own pacing;
8. a page set is claim-locked (HM sends none; its refusal mapping is ready);
9. frame halves carry only frame/theme/rows/roles, replies carry ``op``, features in any order.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness_manager.core import panel as P
from harness_manager.core import panel_codes
from harness_manager.core.errors import (
    ClaimLockedError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import EventBus
from harness_manager.core.panel import COLS, ROWS, Hello, HelloLease, PanelFrame
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.presence import (
    FRAME_CACHE_S,
    STATE_CACHE_S,
    PresenceService,
)
from harness_manager_mps3 import panel as MP
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.clcd_panel_shell import LINUX_PANEL, PANEL_FEATURES, PanelVirtualMps3

REPO = Path(__file__).resolve().parents[2]
TOKENS = REPO / "design" / "tokens.json"


def gen():
    """``tools/gen_panel_codes.py``, loaded by path."""
    if "gen_panel_codes_t" not in sys.modules:
        spec = importlib.util.spec_from_file_location("gen_panel_codes_t",
                                                      REPO / "tools" / "gen_panel_codes.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gen_panel_codes_t"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["gen_panel_codes_t"]


# --- the board's own renders (platform feat/panel-aligned, clcd_preview --aligned) -------------
#
# Rows of the board's aligned status page (aligned_grids.txt, "aligned-status") with its
# role codes, in the WIRE form: the preview prints the status glyphs as one-column stand-ins
# ('#' held 0x83, '✓' ok 0x80, '@' user 0x84); here they are the bytes the board sends.

BOARD_ROWS = {
    0: (" mps3-01       \x83 david 1h12m, 1 waiting ",
        "fffffffffffffffggggggggggggggggggggggggf"),
    2: ("design nanosoc v1.0           \x80verified ",
        "bbbbbbacccccccabbbbaaaaaaaaaaaiiiiiiiiia"),
    11: ("hm     \x84david@srv03335  +1 watching     ",
         "bbaaaaacccccccccccccccaabbbbbbbbbbbaaaaa"),
    14: (" mac 02:00:00:4D:50:53             hb / ",
         "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"),
}


# --- 1. one vocabulary, generated from the tokens -----------------------------------------------


def _enum_order() -> list[str]:
    """``design/generated/clcd_palette.h``'s enum clcd_role, in order."""
    text = (REPO / "design" / "generated" / "clcd_palette.h").read_text()
    start = text.index("enum clcd_role {")
    body = text[start:text.index("CLCD_ROLE_COUNT", start)]
    return [m.lower().replace("_", "-") for m in re.findall(r"CLCD_ROLE_([A-Z_]+)", body)]


def test_role_codes_are_the_tokens_order_which_is_the_palette_headers_enum():
    roles = list(json.loads(TOKENS.read_text())["panel"]["roles"])
    assert list(P.ROLE_NAMES) == roles == _enum_order()
    # the letters the Linux lead listed (net-protocol v0.17 "panel")
    assert [P.role_code(n) for n in ("text", "ok", "banner-err", "banner-busy", "banner-held")] \
        == ["a", "i", "q", "t", "u"]
    assert [P.role_name(c) for c in "abcdefghijklmnopqrstu"] == roles


def test_negative_twin_hms_old_letters_mean_something_else_on_the_wire():
    """Before PANEL-V017 HM read ``t`` as text and ``i`` as inverted: on the wire ``i`` is ok
    (green text, never inverted) and ``t`` is the IDENTIFY banner."""
    assert P.role_name("i") == "ok" and not P.is_banner_role(P.role_name("i"))
    assert P.role_name("t") == "banner-busy" and P.is_banner_role("banner-busy")
    assert P.ROLE_TEXT == "a" and P.ROLE_INVERTED == "q"
    assert P.role_name("v") == "" and P.role_name("") == "", "a code the tokens do not define"
    with pytest.raises(ValueError):
        P.role_code("inverted")


def test_the_generated_vocabulary_is_fresh_and_the_web_ui_has_the_same():
    assert gen().check(REPO) == []
    js = (REPO / "src/harness_manager/web/static/js/panel_codes.js").read_text()
    js_roles = re.findall(r'^  "([a-z-]+)",$', js[js.index("export const ROLES"):
                                                   js.index("export const ROLE_COLOURS")], re.M)
    assert tuple(js_roles) == P.ROLE_NAMES
    js_glyphs = {int(code): (name, tuple(int(b) for b in rows.split(", ")))
                 for code, name, rows in re.findall(
                     r'^  (\d+): \{ name: "([a-z]+)", rows: \[([\d, ]+)\]', js, re.M)}
    assert js_glyphs == {code: (g[0], g[1]) for code, g in panel_codes.GLYPHS.items()}
    assert "GENERATED by tools/gen_panel_codes.py" in js


def test_negative_twin_reordering_the_tokens_roles_makes_the_vocabulary_stale(tmp_path):
    for rel in ("design/tokens.json", "design/generated/clcd_palette.h",
                "src/harness_manager/core/panel_codes.py",
                "src/harness_manager/web/static/js/panel_codes.js"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((REPO / rel).read_bytes())
    doc = json.loads(TOKENS.read_text())
    roles = doc["panel"]["roles"]
    swapped = dict(reversed(list(roles.items())))
    doc["panel"]["roles"] = swapped
    (tmp_path / "design/tokens.json").write_text(json.dumps(doc, indent=2))
    problems = gen().check(tmp_path)
    assert len(problems) == 2 and all("is stale" in p for p in problems), problems
    gen().write(tmp_path)
    assert gen().check(tmp_path) == []
    text = (tmp_path / "src/harness_manager/core/panel_codes.py").read_text()
    assert text.index('"banner-held",') < text.index('"text",'), "the codes follow the tokens"


def test_the_glyphs_are_the_panels_table_not_a_copy():
    table = gen()._tool("clcd_mock", gen().ROOT).GLYPHS
    assert {code: (g[0], g[1]) for code, g in panel_codes.GLYPHS.items()} == \
        {ord(ch): (name, tuple(rows)) for name, (ch, rows) in table.items()}
    assert set(P.GLYPH_NAMES.values()) == {"ok", "err", "warn", "held", "user", "unk", "dot"}
    assert min(panel_codes.GLYPHS) == 0x80 and max(panel_codes.GLYPHS) == 0x86


def test_negative_twin_a_bitmap_changed_in_the_glyph_table_makes_the_vocabulary_stale(
        monkeypatch):
    mock = gen()._tool("clcd_mock", gen().ROOT)
    edited = dict(mock.GLYPHS)
    ch, rows = edited["ok"]
    edited["ok"] = (ch, [0xFF] + list(rows[1:]))
    monkeypatch.setattr(mock, "GLYPHS", edited)
    assert any("panel_codes.py is stale" in p for p in gen().check(REPO))


def _board_frame(theme: str = "aligned") -> PanelFrame:
    rows = [" " * COLS] * ROWS
    roles = [P.ROLE_TEXT * COLS] * ROWS
    for r, (text, codes) in BOARD_ROWS.items():
        rows[r], roles[r] = text, codes
    return PanelFrame(rows=tuple(rows), roles="".join(roles), theme=theme)


def test_hm_decodes_the_boards_own_render_by_the_tokens_order():
    frame = _board_frame()
    assert frame.role_at(0, 0) == "title" and frame.role_at(0, 15) == "title-held"
    assert frame.rows[0][15] == "\x83" and P.GLYPH_NAMES[frame.rows[0][15]] == "held"
    assert [frame.role_at(2, c) for c in (0, 7, 30)] == ["label", "value", "ok"]
    assert P.GLYPH_NAMES[frame.rows[2][30]] == "ok" and frame.role_at(14, 3) == "chrome"
    assert frame.role_at(11, 7) == "value" and P.GLYPH_NAMES[frame.rows[11][7]] == "user"
    # a terminal gets one-column stand-ins, never a C1 control character
    text = P.frame_text(frame.rows)
    assert all(len(r) == COLS for r in text)
    assert not any(0x80 <= ord(ch) <= 0x9F for row in text for ch in row)
    assert text[0][15] == "#" and text[11][7] == "@"


def test_negative_twin_without_roles_a_frame_has_no_role_to_decode():
    frame = PanelFrame(rows=_board_frame().rows, roles="")
    assert frame.role_at(2, 30) == ""
    assert P.frame_text(["ok \x80"]) != ("ok \x80",)


# --- over the socket: the Linux harness as shipped -----------------------------------------------


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Board:
    def __init__(self, vb: PanelVirtualMps3, tmp_path: Path) -> None:
        self.vb = vb
        self.engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                             packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        self.session = self.engine.open(vb.candidate(), note="panel-v017")
        self.bid = self.session.candidate.board_id

    @property
    def panel(self) -> MP.Mps3Panel:
        return self.session.panel

    def ops(self, op: str | None = None) -> list[dict]:
        return [r for r in self.vb.shell.requests if op is None or r.get("op") == op]


@pytest.fixture
def board_clock() -> Clock:
    return Clock()


@pytest.fixture
def linux(tmp_path: Path, board_clock: Clock) -> Iterator[Board]:
    with PanelVirtualMps3(tmp_path / "lx", LINUX_PANEL, board_clock=board_clock) as vb:
        b = Board(vb, tmp_path / "lx")
        yield b
        b.engine.close_all()


def test_a_read_frame_decodes_roles_glyphs_and_theme(linux):
    shell = linux.vb.shell
    shell.theme = "aligned"
    board = _board_frame()
    shell.rows, shell.roles = list(board.rows), board.roles
    frame = linux.panel.frame()
    assert frame.theme == "aligned" and frame.source == "panel"
    assert frame.rows[2][30] == "\x80" and frame.role_at(2, 30) == "ok"
    assert frame.rows == board.rows and frame.roles == board.roles


def test_negative_twin_roles_with_a_code_the_tokens_do_not_define_are_dropped_whole(linux):
    shell = linux.vb.shell
    shell.roles = "z" + shell.roles[1:]
    frame = linux.panel.frame()
    assert frame.roles == "" and frame.role_at(0, 1) == "", "drawn as plain text, never guessed"
    assert len(frame.rows) == ROWS


def test_frame_true_is_refused_as_the_board_does_and_hm_never_sends_it(linux):
    """2. The real harness refuses a whole frame (it does not fit one 1280 B reply)."""
    want = {"ok": False, "code": "invalid",
            "err": 'invalid frame: "a" (rows 0-7) then "b" (rows 8-14)'}
    assert linux.vb.shell.handle_control({"op": "panel", "frame": True}) == want
    assert linux.vb.shell.handle_control({"op": "panel", "frame": "c"}) == want
    linux.panel.frame()
    assert [r["frame"] for r in linux.ops("panel") if "frame" in r][-2:] == ["a", "b"]
    assert not any(r.get("frame") is True for r in linux.ops("panel")[2:])


def test_negative_twin_the_halves_and_frame_false_are_answered(linux):
    shell = linux.vb.shell
    a = shell.handle_control({"op": "panel", "frame": "a"})
    b = shell.handle_control({"op": "panel", "frame": "b"})
    assert a["ok"] and len(a["rows"]) == 8 and len(a["roles"]) == 8 * COLS
    assert b["ok"] and len(b["rows"]) == 7 and len(b["roles"]) == 7 * COLS
    state = shell.handle_control({"op": "panel", "frame": False})
    assert state["ok"] and "rows" not in state and state["page"] == "status"


def test_a_frame_half_is_only_frame_theme_rows_roles_and_replies_carry_op(linux):
    """9. The board's half: ``{ok, op, frame, theme, rows, roles}``, no state; HM reads it."""
    half = linux.vb.shell.handle_control({"op": "panel", "frame": "a"})
    assert set(half) == {"ok", "op", "frame", "theme", "rows", "roles"}
    assert half["op"] == "panel" and half["theme"] == "today"
    assert linux.vb.shell.handle_control({"op": "panel"})["op"] == "panel"
    frame = MP.frame_from_halves([half, linux.vb.shell.handle_control(
        {"op": "panel", "frame": "b"})], wall=1.0)
    assert frame.theme == "today" and len(frame.roles) == ROWS * COLS
    assert linux.panel.state().page == "status"          # a state reply with "op" parses


def test_negative_twin_halves_in_two_themes_say_no_theme():
    a = {"ok": True, "op": "panel", "frame": "a", "theme": "today", "rows": [" " * COLS] * 8,
         "roles": "a" * 8 * COLS}
    b = {**a, "frame": "b", "theme": "aligned", "rows": [" " * COLS] * 7, "roles": "a" * 7 * COLS}
    assert MP.frame_from_halves([a, b], wall=1.0).theme == ""
    with pytest.raises(UnavailableError, match="8 rows"):
        MP.frame_from_halves([a], wall=1.0)


def test_features_are_read_in_any_order(linux):
    """9. The board lists ``locate``, ``presence``, ``panel`` (in that order, after the
    others); HM reads a set."""
    assert LINUX_PANEL.features[-3:] == PANEL_FEATURES == ("locate", "presence", "panel")
    before = linux.panel.support()
    linux.vb.shell.features = tuple(reversed(linux.vb.shell.features))
    linux.panel.identity(fresh=True)
    assert linux.panel.support() == before


def test_negative_twin_a_missing_feature_is_noticed_whatever_the_order(linux):
    linux.vb.shell.features = tuple(f for f in reversed(linux.vb.shell.features) if f != "panel")
    linux.panel.identity(fresh=True)
    assert linux.panel.support().source == "rebuilt"


# --- 4. a hello refusal keeps the features ----------------------------------------------------


def test_a_refused_hello_is_reported_with_the_boards_words_and_the_features_are_kept(linux):
    linux.panel.hello(Hello(sid="a1", who="d@h", app="hm"))            # features read
    versions = len(linux.ops("version"))
    with pytest.raises(UsageError, match="invalid sid: 1-8 printable characters"):
        linux.panel.hello(Hello(sid="", who="d@h", app="hm"))
    reply = linux.vb.shell.handle_control({"op": "hello", "sid": "", "who": "d@h"})
    assert reply == {"ok": False, "err": "invalid sid: 1-8 printable characters",
                     "code": "invalid"}
    assert linux.panel.support().presence == ""
    assert linux.panel.hello(Hello(sid="a1", who="d@h", app="hm")).count == 1
    assert len(linux.ops("version")) == versions, "nothing forgotten: no features re-read"


def test_negative_twin_a_missing_verb_is_the_one_refusal_that_reads_the_features_again(linux):
    linux.panel.hello(Hello(sid="a1", who="d@h", app="hm"))
    versions = len(linux.ops("version"))
    linux.vb.shell.features = tuple(f for f in linux.vb.shell.features if f != "presence")
    with pytest.raises(UnavailableError, match="declined the hello"):
        linux.panel.hello(Hello(sid="a1", who="d@h", app="hm"))
    assert linux.panel.support().presence                  # read again: the bit is gone
    assert len(linux.ops("version")) > versions


@pytest.mark.parametrize(("bad", "words"), [
    ({"v": 0}, "invalid v: an integer >= 1"),
    ({"who": 7}, "invalid who: a string (user@host)"),
    ({"role": "boss"}, "invalid role: holder, owner or watch"),
    ({"ttl": "90"}, "invalid ttl: an integer (30-300 s)"),
    ({"lease": [1]}, "invalid lease: a flat object"),
    ({"lease": {"left": "1h"}}, "invalid lease: left, q and rl are integers"),
    ({"job": {"k": 1}}, "invalid job: {k: string, p: integer}"),
])
def test_the_fake_refuses_hellos_in_the_boards_words(linux, bad, words):
    req = {"op": "hello", "v": 1, "sid": "a1", "who": "d@h", **bad}
    assert linux.vb.shell.handle_control(req) == {"ok": False, "err": words, "code": "invalid"}


def test_negative_twin_a_long_sid_is_clipped_not_refused(linux):
    reply = linux.vb.shell.handle_control({"op": "hello", "sid": "0123456789", "who": "d@h"})
    assert reply["ok"] and list(linux.vb.shell.board_sessions) == ["01234567"]


def test_decline_mapping_keeps_the_features_for_every_refusal_but_a_missing_verb():
    def refusal(**reply):
        return MP._declined({"ok": False, **reply}, "presence", "the hello")

    assert isinstance(refusal(err="invalid sid: x", code="invalid"), UsageError)
    assert isinstance(refusal(err="panel locked: board claimed (use ssh)", code="locked"),
                      ClaimLockedError)
    held = refusal(err="dut owns the panel", code="held")
    assert type(held) is RefusedError and "(held)" in held.message
    assert type(refusal(err="hello: the reply does not fit", code="too_large")) is RefusedError
    # the twins: only these two are "the verb is missing" (UnavailableError: forget + re-read)
    assert isinstance(refusal(err="hello not supported", code="not_supported"), UnavailableError)
    assert isinstance(refusal(err="unknown op 'hello'"), UnavailableError)
    assert MP._declined({"ok": True}, "presence", "the hello") is None


class _Panel:
    """A panel adapter whose hello the board refuses (or declines), for the service."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def support(self):
        return P.PanelSupport()

    def hello(self, hello):
        raise self.exc


def _service(panel) -> PresenceService:
    from types import SimpleNamespace

    svc = PresenceService(EventBus(), clock=Clock(), wall=lambda: 1_790_000_000.0,
                          ride_wait_s=0.0, who="d@h", app="hm/0.1.0")
    svc.track("b1", SimpleNamespace(candidate=SimpleNamespace(name="mps3-01"), panel=panel,
                                    hub=None))
    return svc


def test_presence_reports_a_refused_hello_and_keeps_beating():
    svc = _service(_Panel(UsageError("the harness refused the hello: invalid sid: 1-8 "
                                     "printable characters")))
    svc.beat_due()
    got = svc.presence("b1")
    assert got["reason"] == "" and "invalid sid" in got["last_error"]


def test_negative_twin_presence_calls_a_missing_verb_unsupported():
    svc = _service(_Panel(UnavailableError("presence", "the harness declined the hello: "
                                                       "hello not supported")))
    svc.beat_due()
    assert "hello not supported" in svc.presence("b1")["reason"]


# --- 5. the hello reply's banner may lag one refresh ------------------------------------------------


REQ = Hello(sid="h1", who="david@srv03335", app="hm", role="holder",
            lease=HelloLease(by="david@mapstone-dev", left=4332, q=1, req="bob@srv03340", rl=103))


def test_the_hello_reply_may_lag_the_banner_it_asks_for(linux, board_clock):
    reply = linux.panel.hello(REQ)
    assert reply.banner == "", "rendered from the committed panel: not drawn yet"
    board_clock.t += 0.3                                   # the next 250 ms refresh
    assert linux.panel.state().banner == "bob wants this board"
    frame = linux.panel.frame()
    assert frame.rows[10].strip() == "bob wants this board"
    assert {frame.role_at(r, 0) for r in (10, 11, 12)} == {"banner-held"}
    assert linux.panel.hello(REQ).banner == "bob wants this board"


def test_negative_twin_without_the_lag_the_reply_carries_it_at_once(linux):
    linux.vb.shell.banner_lag_s = 0.0
    assert linux.panel.hello(REQ).banner == "bob wants this board"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def test_presence_tolerates_the_lag_the_next_beat_carries_the_banner(linux, board_clock):
    wall = 1_790_000_000.0
    view = {"hub": "mapstone-dev",
            "lease": {"holder": "david@mapstone-dev", "expires_at": _iso(wall + 4332),
                      "mine": True},
            "incoming": [{"id": "r1", "by": "bob@srv03340", "deadline_at": _iso(wall + 103),
                          "answer": None}],
            "request": None, "queue": [{"position": 1}]}
    bus, clock = EventBus(), Clock()
    events: list = []
    bus.subscribe("panel.*", events.append)
    svc = PresenceService(bus, lease_view=lambda _s: view, clock=clock, wall=lambda: wall,
                          ride_wait_s=0.0, who="david@srv03335", app="hm/0.1.0")
    svc.track(linux.bid, linux.session)
    svc.beat_due()
    board_clock.t += 0.3
    clock.t += P.FAST_BEAT_S                                # an open request: the fast beat
    svc.beat_due()
    states = [e.data for e in events if e.topic == "panel.state"]
    assert [s["banner"] for s in states] == ["", "bob wants this board"]
    got = svc.presence(linux.bid)
    assert got["active"] and got["last_error"] == "" and got["sent"] == 2


# --- 7. HM's own pacing (the board has none) -----------------------------------------------------


def test_hm_paces_its_own_reads_because_the_board_does_not(linux):
    src = (REPO / "src/harness_manager/services/presence.py").read_text()
    assert "rate-limited on the board" not in src
    assert (STATE_CACHE_S, FRAME_CACHE_S) == (1.0, 3.0)
    svc = PresenceService(EventBus(), ride_wait_s=0.0)
    svc.track(linux.bid, linux.session)
    before = len(linux.ops("panel"))
    svc.frame(linux.bid, linux.session)
    svc.frame(linux.bid, linux.session)
    assert len(linux.ops("panel")) - before == 2, "one frame (two halves), the second reused"


def test_negative_twin_the_board_answers_every_read_it_is_sent(linux):
    before = len(linux.ops("panel"))
    for _ in range(5):
        assert linux.vb.shell.handle_control({"op": "panel"})["ok"]
    assert len(linux.ops("panel")) - before == 5


# --- 8. a page set is claim-locked -------------------------------------------------------------------


def test_a_page_set_is_claim_locked_first_and_hm_sends_none(linux):
    shell = linux.vb.shell
    linux.panel.state()
    linux.panel.frame()
    linux.panel.hello(Hello(sid="a1", who="d@h", app="hm"))
    assert not any("page" in r for r in linux.ops("panel")), "HM sends no page change"
    shell.ssh_claimed = True
    locked = shell.handle_control({"op": "panel", "page": "nope", "frame": "a"})
    assert locked == {"ok": False, "err": "panel locked: board claimed (use ssh)",
                      "code": "locked"}, "the lock is first, whatever the request holds"
    assert isinstance(MP._declined(locked, "front_panel", "the page change"), ClaimLockedError)


def test_negative_twin_unclaimed_a_page_set_is_judged_then_held_then_set(linux):
    shell = linux.vb.shell
    assert shell.handle_control({"op": "panel", "page": "nope"})["code"] == "invalid"
    assert shell.handle_control({"op": "panel", "page": "apps", "frame": "a"})["code"] == "invalid"
    shell.display_owner = shell.display_target = "dut"
    assert shell.handle_control({"op": "panel", "page": "apps"}) == {
        "ok": False, "err": "dut owns the panel", "code": "held"}
    shell.display_owner = shell.display_target = "harness"
    assert shell.handle_control({"op": "panel", "page": "apps"}) == {
        "ok": True, "op": "panel", "page": "apps"}
    assert linux.panel.state().page == "apps"


# --- 6. row 0 is the board's own label ---------------------------------------------------------------


def _section(doc: str, heading: str) -> str:
    start = doc.index(heading)
    return doc[start:doc.index("\n### ", start + 1)]


def test_the_design_says_row_0_is_the_boards_label_and_hm_is_in_the_session_row():
    doc = (REPO / "docs/design/CLCD_ALIGNMENT.md").read_text()
    names = _section(doc, "### 4.4 The same names for things")
    assert "the board's own label" in names and "`hello.name` is accepted but not drawn" in names
    assert "session row (row 11)" in names
    wire = _section(doc, "### 2.2 The wire")
    assert "Kept, not drawn in v0.17" in wire and "kept, not drawn." in wire


def test_negative_twin_hm_still_sends_its_name_the_board_keeps_it(linux):
    """The name stays on the wire (additive, the board keeps it): only the drawing changed."""
    linux.panel.hello(Hello(sid="a1", who="d@h", app="hm", name="mps3-01"))
    assert linux.vb.shell.hellos[-1]["name"] == "mps3-01"
    doc = (REPO / "docs/design/CLCD_ALIGNMENT.md").read_text()
    assert "`mps3-01`, from `hello.name`" not in doc, "the superseded plan is gone"


def test_the_cli_mirror_prints_stand_ins_and_json_keeps_the_glyphs():
    from harness_manager.cli.cmd_panel import _mirror_human
    from harness_manager.cli.output import jsonable

    frame = _board_frame()
    human = _mirror_human(jsonable(frame))
    assert not any(0x80 <= ord(ch) <= 0x9F for line in human for ch in line)
    assert "|design nanosoc v1.0           *verified |" in human
    assert jsonable(frame)["rows"][2][30] == "\x80", "--json: as the board sent it"
