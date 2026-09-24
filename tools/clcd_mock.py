"""CLCD-HM spike: the MPS3 front panel (CLCD) beside Harness Manager, in one design language.

Board-free and stdlib-only. It renders 320x240 panel frames pixel for pixel with the
firmware's own 8x16 font, from two sources:

- **today**: the REAL renderer's text grid. ``firmware/clcd/tools/clcd_preview.c``
  (platform repo) runs ``clcd.c reformat()`` on the host; its output is saved under
  ``docs/design/clcd/source/``. Colours are the firmware's only three: white text,
  black background, red on an inverted row (clcd.c:65-67, :911-913).
- **proposed**: frames built here from a board state, with colour roles resolved
  from ``docs/design/clcd/tokens.json`` (the proposed single source of truth) and
  quantised to RGB565 exactly as the panel stores them.

It also holds the proposed presence model (``hello`` line, session table, the
panel's ``hm`` row), so the mock-up's session row comes from code a test checks.

Usage::

    python3 tools/clcd_mock.py                 # writes docs/design/clcd/*.png + mockup.html
    python3 tools/clcd_mock.py --shot          # also screenshots mockup.html (needs Playwright
                                               # and a system Chrome; HM's .venv has both)

Nothing here talks to a board, a hub or the network.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs" / "design" / "clcd"
TOKENS_PATH = DESIGN / "tokens.json"
FONT_PATH = DESIGN / "source" / "font8x16.json"
APP_CSS = ROOT / "src" / "harness_manager" / "web" / "static" / "css" / "app.css"
ICONS_JS = ROOT / "src" / "harness_manager" / "web" / "static" / "vendor" / "lucide" / "icons.js"

COLS, ROWS = 40, 15
CELL_W, CELL_H = 8, 16
PANEL_W, PANEL_H = COLS * CELL_W, ROWS * CELL_H          # 320 x 240

# --- font --------------------------------------------------------------------------------


def parse_font_header(text: str) -> list[list[int]]:
    """``font8x16.h`` -> 95 glyphs x 16 scanlines (bit 7 = leftmost pixel)."""
    start = text.index("font8x16[CLCD_FONT_COUNT][16] = {")
    body = re.sub(r"/\*.*?\*/", "", text[start:], flags=re.S)
    vals = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{2})\b", body)]
    if len(vals) != 95 * 16:
        raise ValueError(f"expected 1520 scanlines, got {len(vals)}")
    return [vals[i * 16:(i + 1) * 16] for i in range(95)]


def _art(rows: dict[int, str]) -> list[int]:
    out = [0] * 16
    for r, s in rows.items():
        out[r] = int(s.replace(".", "0").replace("#", "1"), 2)
    return out


#: PROPOSED status glyphs for a Linux renderer's extended font (0x80-0x86), each an
#: 8x16 stand-in for the lucide icon the web UI uses for the same state.
GLYPHS: dict[str, tuple[str, list[int]]] = {
    "ok": ("\x80", _art({4: "......#.", 5: ".....##.", 6: ".....#..", 7: "#...##..",
                         8: "##.##...", 9: ".###....", 10: "..#....."})),
    "err": ("\x81", _art({4: "##...##.", 5: "###.###.", 6: ".#####..", 7: "..###...",
                          8: ".#####..", 9: "###.###.", 10: "##...##."})),
    "warn": ("\x82", _art({2: "...#....", 3: "..###...", 4: "..#.#...", 5: ".##.##..",
                           6: ".#.#.#..", 7: "##.#.##.", 8: "#..#..#.", 9: "#.....#.",
                           10: "#..#..#.", 11: "#######."})),
    "held": ("\x83", _art({2: "..###...", 3: ".#...#..", 4: ".#...#..", 5: ".#...#..",
                           6: "#######.", 7: "#######.", 8: "###.###.", 9: "###.###.",
                           10: "#######.", 11: "#######."})),
    "user": ("\x84", _art({2: "..###...", 3: ".#####..", 4: ".#####..", 5: ".#####..",
                           6: "..###...", 8: ".#####..", 9: "#######.", 10: "#######.",
                           11: "#######."})),
    "unk": ("\x85", _art({2: "..###...", 3: ".#...#..", 4: ".....#..", 5: "....#...",
                          6: "...#....", 7: "...#....", 9: "...#....", 10: "...#...."})),
    "dot": ("\x86", _art({5: "..###...", 6: ".#####..", 7: ".#####..", 8: ".#####..",
                          9: "..###..."})),
}
G = {name: ch for name, (ch, _rows) in GLYPHS.items()}


def load_font(path: Path = FONT_PATH) -> dict[str, list[int]]:
    data = json.loads(path.read_text())
    font = {chr(0x20 + i): g for i, g in enumerate(data["glyphs"])}
    font.update({ch: rows for ch, rows in GLYPHS.values()})
    return font


# --- tokens and RGB565 -------------------------------------------------------------------


def load_tokens(path: Path = TOKENS_PATH) -> dict:
    return json.loads(path.read_text())


def _rgb(hexs: str) -> tuple[int, int, int]:
    h = hexs.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb565(hexs: str) -> int:
    """#rrggbb -> the panel's 16-bit word (rounded, not truncated)."""
    r, g, b = _rgb(hexs)
    return (round(r * 31 / 255) << 11) | (round(g * 63 / 255) << 5) | round(b * 31 / 255)


def rgb565_hex(v: int, *, bgr: bool = False) -> str:
    """The colour a 16-bit word shows (5/6-bit fields expanded by bit replication).
    ``bgr=True`` shows what an R<->B channel swap would put on the glass."""
    r5, g6, b5 = (v >> 11) & 31, (v >> 5) & 63, v & 31
    if bgr:
        r5, b5 = b5, r5
    r, g, b = (r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)
    return f"#{r:02x}{g:02x}{b:02x}"


def resolve(tokens: dict, ref: str, theme: str = "dark") -> str:
    """A role colour reference -> #rrggbb. ``"#ffffff"`` literal; ``"err"`` the theme's
    token; ``"err@light"`` a named theme; ``"bg"`` honours the panel's ``bg_override``."""
    if ref.startswith("#"):
        return ref
    name, _, want = ref.partition("@")
    if name == "bg" and not want and tokens["panel"].get("bg_override"):
        return tokens["panel"]["bg_override"]
    return tokens["color"][name][want or theme]


def panel_palette(tokens: dict) -> dict[str, tuple[int, int]]:
    """role -> (fg565, bg565), in the panel's theme."""
    theme = tokens["panel"]["theme"]
    return {role: (rgb565(resolve(tokens, spec["fg"], theme)), rgb565(resolve(tokens, spec["bg"], theme)))
            for role, spec in tokens["panel"]["roles"].items()}


#: Today's firmware palette: white on black, white on red for an inverted row.
TODAY = {"text": (0xFFFF, 0x0000), "inv": (0xFFFF, 0xF800)}


def css_block(tokens: dict, theme: str) -> dict[str, str]:
    """The ``--name: value`` pairs the web UI's CSS would be generated from."""
    return {f"--{name}": vals[theme] for name, vals in tokens["color"].items()}


def app_css_values(css: str) -> dict[str, dict[str, str]]:
    """The colour custom properties of app.css: light (``:root``) and dark
    (``:root[data-theme="dark"]``)."""
    def block(selector: str) -> dict[str, str]:
        i = css.index(selector)
        body = css[css.index("{", i) + 1:css.index("}", i)]
        return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", body))
    return {"light": block(":root {"), "dark": block(':root[data-theme="dark"] {')}


# --- frames and pixels -------------------------------------------------------------------


@dataclass
class Frame:
    chars: list[list[str]] = field(default_factory=lambda: [[" "] * COLS for _ in range(ROWS)])
    roles: list[list[str]] = field(default_factory=lambda: [["text"] * COLS for _ in range(ROWS)])

    def put(self, row: int, col: int, text: str, role: str = "text") -> Frame:
        for i, ch in enumerate(text):
            if 0 <= col + i < COLS:
                self.chars[row][col + i] = ch
                self.roles[row][col + i] = role
        return self

    def right(self, row: int, text: str, role: str = "text", pad: int = 0) -> Frame:
        return self.put(row, COLS - pad - len(text), text, role)

    def fill(self, row: int, role: str, c0: int = 0, c1: int = COLS) -> Frame:
        for c in range(c0, c1):
            self.roles[row][c] = role
        return self

    def text(self) -> list[str]:
        return ["".join(r) for r in self.chars]


def frame_from_preview(block: list[str]) -> Frame:
    """One scenario of ``clcd_preview``'s human output -> a Frame in today's palette."""
    f = Frame()
    for line in block:
        m = re.match(r"^(INV|   )(\d\d) \|(.{40})\|", line)
        if m:
            r = int(m.group(2))
            f.put(r, 0, m.group(3), "inv" if m.group(1) == "INV" else "text")
    return f


def preview_scenarios(text: str) -> dict[str, Frame]:
    out = {}
    for chunk in re.split(r"\n== ", "\n" + text):
        if chunk.strip() and not chunk.lstrip().startswith("#"):
            name = chunk.split()[0]
            out[name] = frame_from_preview(chunk.splitlines()[1:])
    return out


def rasterise(frame: Frame, font: dict[str, list[int]], palette: dict[str, tuple[int, int]],
              *, bgr: bool = False) -> list[bytes]:
    """Frame -> 240 scanlines of RGB888, via RGB565 (what the panel really holds)."""
    cache: dict[int, bytes] = {}

    def px(v: int) -> bytes:
        if v not in cache:
            cache[v] = bytes.fromhex(rgb565_hex(v, bgr=bgr)[1:])
        return cache[v]

    lines = []
    for r in range(ROWS):
        rows = [bytearray() for _ in range(CELL_H)]
        for c in range(COLS):
            glyph = font.get(frame.chars[r][c], font[" "])
            fg, bg = palette[frame.roles[r][c]]
            pfg, pbg = px(fg), px(bg)
            for y in range(CELL_H):
                bits = glyph[y]
                for x in range(CELL_W):
                    rows[y] += pfg if bits & (0x80 >> x) else pbg
        lines.extend(bytes(b) for b in rows)
    return lines


def write_png(path: Path, lines: list[bytes], scale: int = 1) -> None:
    width = len(lines[0]) // 3
    raw = bytearray()
    for line in lines:
        wide = b"".join(line[i:i + 3] * scale for i in range(0, len(line), 3)) if scale > 1 else line
        for _ in range(scale):
            raw += b"\x00" + wide
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width * scale, len(lines) * scale, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


# --- presence (PROPOSED wire + the panel's session table) -------------------------------

#: The harness's request line limit (platform firmware/common/net_if.h:177, MPS3_NET_LINE_MAX).
LINE_MAX = 256
WHO_MAX = 20       # user@host, clipped by the host (the panel shows 16)
USER_MAX = 12      # a lease holder or requester: the user part only (the panel shows no host)
NAME_MAX = 16
APP_MAX = 12
JOB_MAX = 8
#: Numeric caps. Times are RELATIVE seconds at send time: the harness has no wall clock
#: it can trust (bare-metal has none; the Linux image may lack NTP), so it counts
#: them down from its own monotonic clock from the moment the hello arrived.
LIMITS = {"left": 86_400, "q": 99, "rl": 600}
TTL_S = 90         # a session is shown for this long after its last hello
MAX_SESSIONS = 4   # the board keeps at most this many; the oldest is dropped


def _ascii(value: str, limit: int) -> str:
    """Printable ASCII only (the panel font's range, and one byte per character, so the
    line budget holds), clipped to ``limit``."""
    return "".join(ch if " " <= ch <= "~" else "?" for ch in str(value))[:limit]


def hello_line(*, sid: str, who: str, app: str, name: str = "", role: str = "watch",
               lease: dict | None = None, job: dict | None = None, ttl: int = TTL_S) -> bytes:
    """The proposed 6900 ``hello`` request, as sent (compact JSON + newline).

    ``role``: ``holder`` (this session holds the hub lease), ``owner`` (standalone:
    this session's daemon holds the board), ``watch`` (anything else).
    ``lease``: ``{"by": holder, "left": s, "q": queue_len, "req": requester, "rl": s}``,
    what this host last read from the hub (the board cannot see the hub). ``left``
    and ``rl`` (the open request's time to answer) are seconds from now; only the
    user part of a principal is sent.
    ``job``: ``{"k": kind, "p": percent}`` while a Harness Manager job runs.
    """
    msg: dict = {"op": "hello", "v": 1, "sid": _ascii(sid, 8), "who": _ascii(who, WHO_MAX),
                 "app": _ascii(app, APP_MAX)}
    if name:
        msg["name"] = _ascii(name, NAME_MAX)
    msg["role"] = role
    if lease:
        msg["lease"] = {k: (_ascii(v.split("@")[0], USER_MAX) if isinstance(v, str)
                            else max(0, min(LIMITS[k], int(v))))
                        for k, v in lease.items() if k in ("by", "left", "q", "req", "rl")}
    if job:
        msg["job"] = {"k": _ascii(str(job.get("k", "")), JOB_MAX), "p": max(0, min(100, int(job.get("p", 0))))}
    msg["ttl"] = max(30, min(300, int(ttl)))
    line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
    if len(line) > LINE_MAX:
        raise ValueError(f"hello is {len(line)} B; the harness reads at most {LINE_MAX}")
    return line


@dataclass
class Session:
    sid: str
    who: str
    role: str
    seen: float
    ttl: int
    lease: dict | None = None
    job: dict | None = None


class PanelSessions:
    """What the harness would keep: the last hello per session, at most MAX_SESSIONS."""

    def __init__(self) -> None:
        self.by_sid: dict[str, Session] = {}

    def hello(self, line: bytes, now: float) -> dict:
        msg = json.loads(line)
        s = Session(msg["sid"], msg["who"], msg.get("role", "watch"), now, int(msg.get("ttl", TTL_S)),
                    msg.get("lease"), msg.get("job"))
        self.by_sid[s.sid] = s
        if len(self.by_sid) > MAX_SESSIONS:
            oldest = min(self.by_sid.values(), key=lambda x: x.seen)
            del self.by_sid[oldest.sid]
        return {"ok": True, "op": "hello", "sessions": len(self.live(now))}

    def live(self, now: float) -> list[Session]:
        rank = {"holder": 0, "owner": 1, "watch": 2}
        alive = [s for s in self.by_sid.values() if now - s.seen <= s.ttl]
        return sorted(alive, key=lambda s: (rank.get(s.role, 3), -s.seen))

    def lease(self, now: float) -> dict | None:
        """The freshest lease any live session reported (the holder's wins a tie)."""
        live = [s for s in self.live(now) if s.lease]
        if not live:
            return None
        src = min(live, key=lambda s: (s.role != "holder", now - s.seen))
        out = dict(src.lease)
        for k in ("left", "rl"):
            if k in out:
                out[k] = max(0, out[k] - (now - src.seen))
        return out


def _dur(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m"


def hm_row(sessions: PanelSessions, now: float) -> list[tuple[int, str, str]]:
    """The panel's row 11, ``[(col, text, role), ...]``: which Harness Managers are
    connected. The first is the lease holder when one is, else the latest."""
    live = sessions.live(now)
    out: list[tuple[int, str, str]] = [(0, "hm", "label")]
    if not live:
        gone = list(sessions.by_sid.values())
        if gone:
            last = max(gone, key=lambda s: s.seen)
            out.append((7, f"{last.who[:16]}  left {_dur(now - last.seen)} ago", "label"))
        else:
            out.append((7, "none connected", "label"))
        return out
    first = live[0]
    out.append((7, G["user"] + first.who[:16], "value"))
    if len(live) > 1:
        out.append((7 + 1 + len(first.who[:16]) + 2, f"+{len(live) - 1} watching", "label"))
    return out


def lease_badge(sessions: PanelSessions, now: float, *, hub: bool) -> tuple[str, str] | None:
    """The title bar's right side: the hub lease as the sessions reported it, or None
    when the board is not behind a hub (or nobody has said)."""
    if not hub:
        return None
    lease = sessions.lease(now)
    if lease:
        left = lease.get("left", 0)
        q = lease.get("q") or 0
        text = f"{G['held']} {lease.get('by', '?').split('@')[0][:10]} {_dur(left)}"
        text += f", {q} waiting" if q else ""
        return text, ("title-warn" if left < 300 else "title-held")
    if sessions.live(now):
        return G["warn"] + " not leased", "title-warn"
    return None


# --- scenes ------------------------------------------------------------------------------


def aligned_status(*, sessions: PanelSessions | None, now: float, hub: bool = True,
                   banner: tuple[str, list[str]] | None = None, prog: tuple[str, int] | None = None,
                   dut_ip: str = "") -> Frame:
    """The PROPOSED status page: today's rows and facts, in the shared vocabulary,
    with colour roles and the ``hm`` row (11)."""
    f = Frame()
    f.fill(0, "title").put(0, 1, "mps3-01", "title")
    badge = lease_badge(sessions, now, hub=hub) if sessions is not None else None
    if badge:
        f.right(0, badge[0], badge[1], pad=1)
    f.put(1, 0, "-" * COLS, "rule")
    if prog:
        f.put(2, 0, "design", "label").put(2, 7, "partition decoupled", "unk")
    else:
        f.put(2, 0, "design", "label").put(2, 7, "nanosoc", "value").put(2, 15, "v1.0", "label")
        f.right(2, G["ok"] + "verified", "ok", pad=1)
    if prog:
        what, pct = prog
        f.put(3, 0, "prog", "label").put(3, 7, what, "busy")
        f.right(3, f"{pct:3d}%", "busy", pad=1)
    else:
        f.put(3, 0, "prog", "label").put(3, 7, "#001 loaded", "value").right(3, "last ok", "ok", pad=1)
    f.put(4, 0, "shell", "label").put(4, 7, "0x72BB0A36", "value")
    f.put(4, 18, "card", "label").put(4, 23, "nanosoc [A]", "value")
    f.put(5, 0, "net", "label").put(5, 7, "192.168.10.101", "value").right(5, "up 100/FD", "ok", pad=1)
    f.put(6, 0, "up", "label").put(6, 7, "001:23:45:07", "value")
    f.put(7, 0, "dut", "label").put(7, 7, "rst-rel", "ok").put(7, 16, "clk-alive", "ok").put(7, 27, "mmcm-lock", "ok")
    f.put(8, 0, "icap", "label").put(8, 7, "1835072 B", "value").put(8, 18, "rxdrop 0", "label").put(8, 28, "txerr 0", "label")
    f.put(9, 0, "cfg", "label").put(9, 7, "1x Cortex-M0  no ETH  1x UART", "value")
    if dut_ip:
        f.put(10, 0, "dut ip", "label").put(10, 7, dut_ip, "value")
    if sessions is not None:
        for col, text, role in hm_row(sessions, now):
            f.put(11, col, text, role)
    f.put(12, 0, "sys", "label").put(12, 7, "linux ssh claimed SHA256:AbCdEfGh", "value")
    f.put(13, 0, "-" * COLS, "rule")
    f.fill(14, "chrome").put(14, 1, "mac 02:00:00:4D:50:53", "chrome").right(14, "hb /", "chrome", pad=1)
    if prog:
        _, pct = prog
        f.put(10, 0, " " * COLS, "track")
        f.fill(10, "bar", 0, round(COLS * pct / 100))
    if banner:
        role, texts = banner
        for r in (10, 11, 12):
            f.put(r, 0, " " * COLS, role)
        for i, t in enumerate(texts[:3]):
            f.put(10 + i, (COLS - len(t)) // 2, t, role)
    return f


def aligned_apps() -> Frame:
    f = Frame()
    f.fill(0, "title").put(0, 1, "apps & ports", "title").right(0, "nanosoc", "chrome", pad=1)
    f.put(1, 0, "-" * COLS, "rule")
    rows = [("ctrl", "nc 192.168.10.101 6900"), ("tftp", "tftp 192.168.10.101 69"),
            ("push", "nc 192.168.10.101 6910"), ("xvc", "xvc 192.168.10.101:2542"),
            ("jtag", "ocd rbb 192.168.10.101:6921"), ("uart0", "nc 192.168.10.101 6930"),
            ("swo", "nc 192.168.10.101 6932"), ("ssh", "ssh root@192.168.10.101")]
    for i, (k, v) in enumerate(rows):
        f.put(2 + i, 0, k, "label").put(2 + i, 7, v, "value")
    f.put(13, 0, "-" * COLS, "rule")
    f.fill(14, "chrome").put(14, 1, "PB1 tap: next page   hold: give DUT", "chrome")
    return f


def aligned_dut_owns() -> Frame:
    f = Frame()
    f.fill(0, "title").put(0, 1, "mps3-01", "title").right(0, "linux harness", "chrome", pad=1)
    for r in (6, 7, 8):
        f.put(r, 0, " " * COLS, "banner-held")
    f.put(6, 10, G["held"] + " DUT HAS THE PANEL", "banner-held")
    f.put(8, 9, "press PB1 to take it back", "banner-held")
    f.fill(14, "chrome").put(14, 1, "the DUT draws next; this is the last", "chrome")
    return f


# --- the page ----------------------------------------------------------------------------


def lucide(names: list[str]) -> dict[str, str]:
    out = {}
    for line in ICONS_JS.read_text().splitlines():
        m = re.match(r'^\s*"([a-z0-9-]+)":\s*(".*")\s*,?\s*$', line)
        if m and m.group(1) in names:
            out[m.group(1)] = json.loads(m.group(2))
    return out


def svg(icons: dict[str, str], name: str) -> str:
    return (f'<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icons[name]}</svg>')


def build(out: Path = DESIGN, *, now: float = 1_000_000.0) -> dict[str, Path]:
    tokens, font = load_tokens(), load_font()
    pal = panel_palette(tokens)
    written: dict[str, Path] = {}

    def png(name: str, frame: Frame, palette: dict, scale: int = 1, bgr: bool = False) -> str:
        p = out / f"{name}.png"
        write_png(p, rasterise(frame, font, palette, bgr=bgr), scale)
        written[name] = p
        return p.name

    today = preview_scenarios((DESIGN / "source" / "preview_v011_feat-rm-ila-mint.txt").read_text())
    s = PanelSessions()
    s.hello(hello_line(sid="a1b2c3d4", who="david@srv03335", app="hm/0.1.0", name="mps3-01", role="holder",
                       lease={"by": "david@mapstone-dev", "left": 4332, "q": 1}), now - 12)
    s.hello(hello_line(sid="0badf00d", who="bob@srv03340", app="hm/0.1.0", name="mps3-01", role="watch",
                       lease={"by": "david@mapstone-dev", "left": 4324, "q": 1}), now - 4)

    frames = {
        "today_status": (today["healthy"], TODAY),
        "today_apps": (today["apps-nanosoc"], TODAY),
        "today_link_down": (today["link-down"], TODAY),
        "today_dut": (today["dut-banner"], TODAY),
        "aligned_status": (aligned_status(sessions=s, now=now), pal),
        "aligned_identify": (aligned_status(sessions=s, now=now, banner=("banner-busy", [
            G["user"] + " IDENTIFY: david@srv03335", "backlight blinks 2 Hz for 10 s",
            "tap here to say you found it"])), pal),
        "aligned_request": (aligned_status(sessions=s, now=now, banner=("banner-held", [
            G["held"] + " bob@srv03340 wants this board", "held by david  1:43 to answer",
            "tap: tell david you are here"])), pal),
        "aligned_program": (aligned_status(sessions=s, now=now, prog=("pushing nanosoc", 42)), pal),
        "aligned_link_down": (aligned_status(sessions=s, now=now, banner=("banner-err", [
            "", G["err"] + " NETWORK LINK DOWN", ""])), pal),
        "aligned_apps": (aligned_apps(), pal),
        "aligned_dut": (aligned_dut_owns(), pal),
    }
    names = {k: png(k, fr, p) for k, (fr, p) in frames.items()}
    names["today_status@2x"] = png("today_status@2x", today["healthy"], TODAY, 2)
    names["aligned_status@2x"] = png("aligned_status@2x", frames["aligned_status"][0], pal, 2)
    names["aligned_status_if_bgr"] = png("aligned_status_if_bgr", frames["aligned_status"][0], pal, 1, bgr=True)

    icons = lucide(["circle-check", "circle-x", "triangle-alert", "lock", "user", "circle-help",
                    "monitor", "scan-search", "arrow-right-left", "loader-circle", "layers"])
    theme = tokens["panel"]["theme"]
    swatches = []
    for role in ("text", "label", "rule", "chrome", "ok", "warn", "err", "busy", "unk", "held",
                 "banner-err", "banner-held", "banner-busy"):
        spec = tokens["panel"]["roles"][role]
        fg, bg = resolve(tokens, spec["fg"], theme), resolve(tokens, spec["bg"], theme)
        f5, b5 = rgb565(fg), rgb565(bg)
        swatches.append(
            f'<tr><td class="mono">{role}</td>'
            f'<td><span class="sw" style="background:{bg};color:{fg}">Aa</span> <span class="mono small">{fg} / {bg}</span></td>'
            f'<td><span class="sw" style="background:{rgb565_hex(b5)};color:{rgb565_hex(f5)}">Aa</span> '
            f'<span class="mono small">0x{f5:04X} / 0x{b5:04X}</span></td>'
            f'<td><span class="sw" style="background:{rgb565_hex(b5, bgr=True)};color:{rgb565_hex(f5, bgr=True)}">Aa</span></td></tr>')
    glyph_rows = []
    for gname, lname in (("ok", "circle-check"), ("err", "circle-x"), ("warn", "triangle-alert"),
                         ("held", "lock"), ("user", "user"), ("unk", "circle-help")):
        fr = Frame()
        role = {"user": "busy"}.get(gname, gname)
        fr.put(0, 0, G[gname], role)
        glyph_rows.append((gname, lname, fr, role))
    glyph_imgs = []
    for gname, lname, fr, _role in glyph_rows:
        cell = rasterise(fr, font, pal)[:16]
        cell = [line[:24] for line in cell]
        p = out / f"glyph_{gname}.png"
        write_png(p, cell, 4)
        glyph_imgs.append(f'<div class="glyph"><img src="{p.name}" width="32" height="64" alt="{gname}">'
                          f'<span class="mono small">{gname}</span><span class="lu">{svg(icons, lname)}</span>'
                          f'<span class="muted small">{lname}</span></div>')

    def panel(name: str, cap: str, scale: int = 1) -> str:
        return (f'<figure class="panel"><img src="{names[name]}" width="{PANEL_W * scale}" '
                f'height="{PANEL_H * scale}" alt="{html.escape(cap)}"><figcaption>{cap}</figcaption></figure>')

    card = f"""
<section class="card" aria-label="Front panel">
  <div class="card-head"><h2 class="card-title">{svg(icons, "monitor")}Front panel</h2><span class="spacer"></span>
    <span class="chip ok">{svg(icons, "circle-check")}harness owns it</span></div>
  <div class="card-body">
    <img class="mirror" src="{names['aligned_status']}" width="320" height="240" alt="panel mirror">
    <div class="tile-kv mt-8">
      <span class="k">Page</span><span class="v">status <span class="muted small">(read from the panel 3 s ago)</span></span>
      <span class="k">Sessions</span><span class="v"><span class="chip accent">{svg(icons, "user")}you</span>
        <span class="chip plain">bob@srv03340 watching</span></span>
      <span class="k">Lease</span><span class="v"><span class="chip held">{svg(icons, "lock")}yours 1h12m, 1 waiting</span></span>
      <span class="k">Card</span><span class="v mono">nanosoc [A]</span>
    </div>
    <div class="row mt-14">
      <button class="btn sm">{svg(icons, "scan-search")} Identify</button>
      <button class="btn sm">{svg(icons, "arrow-right-left")} Hand to DUT</button>
      <button class="btn sm">{svg(icons, "layers")} Show apps page</button>
    </div>
  </div>
</section>"""
    card_bare = f"""
<section class="card" aria-label="Front panel (bare-metal)">
  <div class="card-head"><h2 class="card-title">{svg(icons, "monitor")}Front panel</h2><span class="spacer"></span>
    <span class="chip ok">{svg(icons, "circle-check")}harness owns it</span></div>
  <div class="card-body">
    <p class="secondary small">Bare-metal v0.11 reports only the owner (<code>display query</code>). The mirror is
      rebuilt by Harness Manager from what it read, not read from the panel.</p>
    <div class="row mt-8"><span class="chip unk">{svg(icons, "circle-help")}presence: needs the Linux harness</span></div>
    <div class="row mt-14"><button class="btn sm">{svg(icons, "arrow-right-left")} Hand to DUT</button>
      <button class="btn sm" disabled title="needs harness feature 'locate' (Linux)">{svg(icons, "scan-search")} Identify</button></div>
  </div>
</section>"""

    page = f"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<title>CLCD alignment mock-up</title>
<link rel="stylesheet" href="../../../src/harness_manager/web/static/css/fonts.css">
<link rel="stylesheet" href="../../../src/harness_manager/web/static/css/app.css">
<style>
  :root[data-theme="dark"] {{ --held: {tokens['color']['held']['dark']}; --held-soft: {tokens['color']['held-soft']['dark']};
    --held-border: {tokens['color']['held-border']['dark']}; }}
  body {{ padding: 24px 28px; min-width: 1400px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }} h2.sec {{ font-size: 15px; margin: 28px 0 10px; color: var(--text-2); }}
  .cols {{ display: grid; grid-template-columns: 660px 660px 380px; gap: 22px; align-items: start; }}
  .panel {{ margin: 0; }} .panel img, img.mirror {{ image-rendering: pixelated; display: block; border-radius: 4px;
    outline: 1px solid var(--border-strong); }}
  .panel figcaption {{ font-size: 12px; color: var(--text-3); margin-top: 6px; max-width: 640px; }}
  .strip {{ display: grid; grid-template-columns: repeat(4, 330px); gap: 18px; }}
  .chip.held {{ background: var(--held-soft); border-color: var(--held-border); color: var(--held); }}
  table.tok {{ border-collapse: collapse; font-size: 12.5px; }} table.tok td, table.tok th {{ padding: 4px 10px;
    border-bottom: 1px solid var(--border); text-align: left; }}
  .sw {{ display: inline-block; width: 38px; text-align: center; font-family: var(--font-mono); border-radius: 3px; }}
  .glyphs {{ display: flex; gap: 22px; }} .glyph {{ display: flex; flex-direction: column; align-items: center; gap: 4px; }}
  .glyph img {{ image-rendering: pixelated; }} .lu .icon {{ width: 20px; height: 20px; }}
  .note {{ color: var(--text-2); font-size: 13px; max-width: 1100px; }}
</style></head>
<body>
<h1>MPS3 front panel and Harness Manager: one design language (spike)</h1>
<p class="note">Lane CLCD-HM, 2026-09-24. Every panel image is 320x240 pixels drawn with the firmware's own 8x16 font and
quantised to RGB565. "Today" is the real renderer's output (clcd.c via clcd_preview, feat/rm-ila-mint, v0.11).
"Proposed" is built from docs/design/clcd/tokens.json. Proposed rows target the Linux harness only (DL4).</p>

<h2 class="sec">1. The status page, and the card that mirrors it</h2>
<div class="cols">
  {panel("today_status@2x", "TODAY, bare-metal v0.11: white on black, red only for a fault. No idea who is connected.", 2)}
  {panel("aligned_status@2x", "PROPOSED, Linux harness: HM's words (design, prog, shell, card), muted labels like HM's tile-kv, "
         "status colours from the web UI, and row 11 = who is connected + the hub lease (the board can't see the hub; HM tells it).", 2)}
  <div class="stack">{card}{card_bare}</div>
</div>

<h2 class="sec">2. The same states, both places</h2>
<div class="strip">
  {panel("aligned_identify", "Identify: HM asks, the backlight blinks (works even when the DUT owns the panel), a tap answers.")}
  {panel("aligned_request", "Lease request: held colour, countdown from the request note. A tap notifies; it never releases.")}
  {panel("aligned_program", "Programming: busy colour; the percentage is the board's own ICAP byte count.")}
  {panel("aligned_link_down", "Fault banner: same rows (10-12) as today, error red from the web UI's light theme.")}
  {panel("today_apps", "TODAY apps page: its hint bar is drawn in the fault red.")}
  {panel("aligned_apps", "PROPOSED apps page: the hint bar is neutral chrome, like HM's header.")}
  {panel("today_dut", "TODAY: the last frame before the DUT takes the panel.")}
  {panel("aligned_dut", "PROPOSED: the same, in the held colour ('someone else has it').")}
</div>

<h2 class="sec">3. Tokens: web value, what the panel shows, and what an R/B swap would show</h2>
<table class="tok"><tr><th>panel role</th><th>token value fg / bg (dark theme, black panel bg)</th><th>panel RGB565 fg / bg</th><th>if R/B swapped</th></tr>
{''.join(swatches)}</table>

<h2 class="sec">4. Status glyphs for an extended panel font (8x16), beside the web UI's lucide icons</h2>
<div class="glyphs">{''.join(glyph_imgs)}</div>
</body></html>
"""
    (out / "mockup.html").write_text(page)
    written["mockup.html"] = out / "mockup.html"
    return written


def screenshot(out: Path = DESIGN) -> Path:
    from playwright.sync_api import sync_playwright

    chrome = next((p for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                               "/usr/bin/chromium-browser", "/usr/bin/chromium") if Path(p).exists()), None)
    shot = out / "mockup.png"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome, headless=True)
        page = browser.new_page(viewport={"width": 1760, "height": 1000}, device_scale_factor=1)
        page.goto((out / "mockup.html").as_uri())
        page.wait_for_timeout(300)
        page.screenshot(path=str(shot), full_page=True)
        browser.close()
    return shot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shot", action="store_true", help="also screenshot mockup.html with headless Chrome")
    a = ap.parse_args(argv)
    for path in build().values():
        print(f"wrote {path.relative_to(ROOT)}")
    if a.shot:
        print(f"wrote {screenshot().relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
