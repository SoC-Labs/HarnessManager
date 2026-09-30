"""CLCD-HM spike (docs/design/CLCD_ALIGNMENT.md): the shared tokens, the panel's RGB565
mapping, and the proposed presence model (hello, session table, the panel's rows).

Board-free. ``tools/clcd_mock.py`` is a script, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("clcd_mock", REPO / "tools" / "clcd_mock.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clcd_mock"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()
NOW = 1_000_000.0


# --- one source of truth -----------------------------------------------------------------


def test_tokens_equal_the_web_ui_css_today():
    """design/tokens.json's values ARE the web UI's: the stylesheet it generates (tokens.css,
    which index.html loads before app.css) sets exactly them, in all three theme blocks.
    tools/gen_tokens.py --check (make check, tests/unit/test_p4_tokens.py) keeps app.css
    from defining colours of its own."""
    tokens = M.load_tokens()
    css = M.app_css_values(M.TOKENS_CSS.read_text())
    for block, theme in (("light", "light"), ("media-dark", "dark"), ("dark", "dark")):
        want = M.css_block(tokens, theme)
        if theme == "light":
            want.update({f"--{n}": v for n, v in tokens["static"].items()})
        assert css[block] == want, block


def test_the_held_family_is_in_the_web_ui_css():
    """'held' (decision P6: someone else has it) is the one new colour family; app.css
    itself defines no token (they are all in tokens.css)."""
    tokens = M.load_tokens()
    css = M.app_css_values(M.TOKENS_CSS.read_text())
    held = ("held", "held-soft", "held-border")
    for block, theme in (("light", "light"), ("media-dark", "dark"), ("dark", "dark")):
        assert {f"--{n}": tokens["color"][n][theme] for n in held}.items() <= css[block].items()
    assert tokens["color"]["held"] == {"light": "#6b3fc4", "dark": "#bfa9f9"}     # UI v2 round 3
    # app.css uses the family (var(--held), lane P3) but defines none of it
    assert not re.search(r"--held[\w-]*\s*:", M.APP_CSS.read_text())
    assert "var(--held)" in M.APP_CSS.read_text()


def test_every_panel_role_resolves_and_every_grammar_state_has_a_colour():
    tokens = M.load_tokens()
    pal = M.panel_palette(tokens)
    for spec in tokens["grammar"].values():
        assert spec["color"] in tokens["color"]
    for state in ("ok", "warn", "err", "busy", "unk", "held"):
        assert state in pal
    assert pal["text"][1] == 0x0000, "the panel background is true black (bg_override)"


# --- RGB565 ------------------------------------------------------------------------------


@pytest.mark.parametrize(("hexs", "word"), [("#ffffff", 0xFFFF), ("#000000", 0x0000),
                                            ("#ff0000", 0xF800), ("#00ff00", 0x07E0),
                                            ("#0000ff", 0x001F)])
def test_rgb565_matches_the_firmware_and_clcd_demo_constants(hexs, word):
    # clcd.c:65-67 (WHITE/BLACK/RED) and clcd_demo_gen.sv's bars (GRN 07E0, BLU 001F)
    assert M.rgb565(hexs) == word
    assert M.rgb565_hex(word) == hexs


def test_an_rb_swap_turns_the_fault_red_blue():
    """What a wrong PANEL_CTRL BGR bit would show: the reason the tokens say 'RGB'."""
    assert M.rgb565_hex(0xF800, bgr=True) == "#0000ff"
    assert M.rgb565_hex(0xFFFF, bgr=True) == "#ffffff"


# --- the renderer's inputs ---------------------------------------------------------------


def test_font_is_the_firmware_table():
    font = json.loads(M.FONT_PATH.read_text())
    assert len(font["glyphs"]) == 95 and all(len(g) == 16 for g in font["glyphs"])
    a = font["glyphs"][ord("A") - 0x20]
    assert a[1] == 0b00010000 and a[9] == 0b01111100   # apex and crossbar of the fixed 8x16 'A'


def test_today_frames_come_from_the_real_renderer_output():
    frames = M.preview_scenarios(
        (M.DESIGN / "source" / "preview_v011_feat-rm-ila-mint.txt").read_text())
    healthy = frames["healthy"].text()
    assert healthy[0].startswith("MPS3-01") and "nanoSoC harness" in healthy[0]
    assert healthy[5].startswith("NET : 192.168.10.101")
    down = frames["link-down"]
    assert [r for r in range(M.ROWS) if down.roles[r][0] == "inv"] == [10, 11, 12]
    assert frames["apps-nanosoc"].roles[14][0] == "inv", "today's hint bar uses the fault style"


def test_aligned_frames_fit_the_grid_and_draw_to_320x240():
    s = M.PanelSessions()
    s.hello(M.hello_line(sid="a1", who="david@srv03335", app="hm/0.1.0", role="holder",
                         lease={"by": "david@mapstone-dev", "left": 4320, "q": 1}), NOW)
    f = M.aligned_status(sessions=s, now=NOW)
    assert all(len(row) == M.COLS for row in f.text())
    assert "david 1h12m, 1 waiting" in f.text()[0]
    lines = M.rasterise(f, M.load_font(), M.panel_palette(M.load_tokens()))
    assert len(lines) == 240 and len(lines[0]) == 320 * 3


def test_build_writes_the_mockup(tmp_path):
    out = M.build(tmp_path)
    assert (tmp_path / "mockup.html").is_file()
    png = (tmp_path / "aligned_status.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == 320
    assert "aligned_request" in out and "today_status" in out


# --- presence ----------------------------------------------------------------------------


def test_the_worst_case_hello_fits_the_harness_line_buffer():
    line = M.hello_line(sid="f" * 32, who="x" * 80, app="harness-manager/10.20.30", name="n" * 64,
                        role="holder",
                        lease={"by": "y" * 80, "left": 10**9, "q": 12345, "req": "z" * 80,
                               "rl": 10**6},
                        job={"k": "program-partition", "p": 1000}, ttl=100000)
    assert len(line) <= M.LINE_MAX, len(line)
    msg = json.loads(line)
    assert len(msg["who"]) == M.WHO_MAX and len(msg["lease"]["req"]) == M.USER_MAX
    assert msg["lease"]["q"] == 99 and msg["job"]["p"] == 100 and msg["ttl"] == 300
    assert msg["lease"]["left"] == 86_400 and msg["lease"]["rl"] == 600


def test_hello_is_ascii_and_sends_only_the_user_part_of_a_principal():
    msg = json.loads(M.hello_line(sid="a1", who="d\u00e9vid@h\x1b[2J", app="hm",
                                  lease={"by": "david@mapstone-dev", "left": 1, "q": 0}))
    assert msg["who"] == "d?vid@h?[2J", "non-ASCII and control characters never reach the panel"
    assert msg["lease"]["by"] == "david"


def test_a_hello_over_the_limit_is_refused_host_side(monkeypatch):
    monkeypatch.setattr(M, "LINE_MAX", 100)
    with pytest.raises(ValueError, match="at most 100"):
        M.hello_line(sid="a", who="d" * 24, app="hm", name="n" * 16,
                     lease={"by": "david", "left": 1, "q": 1})


def test_the_lease_holder_is_shown_first_and_others_are_counted():
    s = M.PanelSessions()
    s.hello(M.hello_line(sid="w1", who="bob@srv03340", app="hm"), NOW - 1)
    s.hello(M.hello_line(sid="h1", who="david@srv03335", app="hm", role="holder"), NOW - 20)
    row = "".join(t for _c, t, _r in M.hm_row(s, NOW))
    assert "david@srv03335" in row and "+1 watching" in row


def test_a_session_disappears_after_its_ttl_and_the_panel_says_when():
    s = M.PanelSessions()
    s.hello(M.hello_line(sid="h1", who="david@srv03335", app="hm", ttl=90), NOW)
    assert s.live(NOW + 90)
    assert not s.live(NOW + 91)
    row = [t for _c, t, _r in M.hm_row(s, NOW + 300)]
    assert row[-1].startswith("david@srv03335") and "left 5m ago" in row[-1]


def test_the_table_keeps_at_most_four_sessions():
    s = M.PanelSessions()
    for i in range(6):
        s.hello(M.hello_line(sid=f"s{i}", who=f"u{i}@h", app="hm"), NOW + i)
    assert sorted(s.by_sid) == ["s2", "s3", "s4", "s5"]


def test_the_lease_badge_is_the_hosts_word_and_turns_warn_near_expiry():
    s = M.PanelSessions()
    s.hello(M.hello_line(sid="h1", who="david@srv03335", app="hm", role="holder",
                         lease={"by": "david@mapstone-dev", "left": 300, "q": 0}), NOW - 60)
    text, role = M.lease_badge(s, NOW, hub=True)
    assert "david 4m" in text and "waiting" not in text and role == "title-warn"
    assert M.lease_badge(s, NOW, hub=False) is None, "standalone: no hub, no lease badge"


def test_lease_times_are_relative_and_count_down_on_the_board_clock():
    """No wall clock is trusted: 'left' is seconds at send time, aged by the board's
    own monotonic time since that hello arrived."""
    s = M.PanelSessions()
    s.hello(M.hello_line(sid="w1", who="bob@srv03340", app="hm",
                         lease={"by": "david", "left": 900, "q": 2}), NOW - 30)
    s.hello(M.hello_line(sid="h1", who="david@srv03335", app="hm", role="holder",
                         lease={"by": "david", "left": 1000, "q": 1}), NOW - 50)
    lease = s.lease(NOW)
    assert lease["left"] == 950 and lease["q"] == 1, "the holder's own report wins"


def test_behind_a_hub_with_nobody_leasing_the_panel_says_not_leased():
    s = M.PanelSessions()
    s.hello(M.hello_line(sid="w1", who="bob@srv03340", app="hm"), NOW)
    assert M.lease_badge(s, NOW, hub=True) == (M.G["warn"] + " not leased", "title-warn")


def test_the_hm_row_fits_forty_columns_at_worst():
    s = M.PanelSessions()
    for i in range(4):
        s.hello(M.hello_line(sid=f"s{i}", who="w" * 40, app="hm"), NOW)
    end = max(c + len(t) for c, t, _r in M.hm_row(s, NOW))
    assert end <= M.COLS
