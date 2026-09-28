"""``PanelFakeShell``: the Linux harness's front-panel verbs on top of ``HarnessFakeShell``
(lane P2), as the Linux harness SHIPPED them (PANEL-V017: net-protocol v0.17, platform
feat/panel-aligned f0f5d6f ``panel_linux.c``/``presence_core.c``, 33ec49d ``clcd.c``):

- **the line layer**: a request line over 256 characters (``MPS3_NET_LINE_MAX``, the newline
  not counted) is refused whole, whatever its verb: ``{"ok":false,"err":"bad json"}``.
- ``hello`` (feature ``presence``): the board keeps at most 4 sessions (the oldest is
  dropped), lists each for its ``ttl`` after its last hello, ordered holder > owner > watch,
  most recent first; the reply is ``{ok, op, sessions: N, panel: {page, owner, pending,
  banner, card, seq}, events}``. A refusal is ``code`` ``invalid`` with the board's words
  (``invalid sid: 1-8 printable characters``, ...); ``sid`` is clipped to 8, not refused.
  The lease request a hello relays (``lease.req`` with ``rl`` left) is drawn as the banner
  (rows 10-12, role ``banner-held``) at the panel's NEXT refresh: ``banner_lag_s`` (0.25 s,
  clcd's reformat) after the hello, so the hello's own reply does not show it yet.
- ``panel`` (feature ``panel``): the state (with ``op``) with the session list and
  ``touch``; ``frame`` ``"a"``/``"b"`` gives ONLY ``{ok, op, frame, theme, rows, roles}``
  (rows 0-7 / 8-14, one role code per cell, ``chr(ord("a") + i)`` in design/tokens.json
  order); ``frame:true`` or any other value is refused (``code`` ``invalid``: a whole frame
  does not fit one reply). ``page`` is claim-locked first (``ssh_claimed``: ``code``
  ``locked``), then judged (``invalid``), then refused while the DUT owns the panel
  (``held``), else set.
- ``version.features`` lists ``locate``, ``presence``, ``panel`` in the board's order.
- ``locate`` (feature ``locate``): ``{s: 1-30, who}`` blinks, ``{s: 0}`` stops;
  ``{ok, op, until_ms}``. V7-ALIGN: as SHIPPED (platform 18622e5, locate_linux.c): ``s`` 0-30,
  ``who`` at most 32 printable ASCII, else ``{ok: false, err: "invalid s: ..."|"invalid who:
  ...", code: "invalid"}``; ``until_ms`` RELATIVE (``s * 1000``); the reply carries ``op``
  (``locate_reply_op=False``: a draft's reply without it); ``locate_decline="not_supported"``
  is a build without the panel (``locate not supported``). LOCATE: what the Linux lead
  confirmed for rc2_v7/v7n (docs/design/BOARD_LOCATE.md §2): the backlight blinks at 2 Hz
  (``blinking``); the "IDENTIFY: <who>" banner shows only while the harness owns the panel
  (``banner_text``);
  a tap on the glass stops it (``tap()``); a harnessd restart restores the backlight
  (``restart_harnessd()``). No rate limit, no LEDs, nothing in the ring.
  ``LINUX_LOCATE`` is that image: ``locate`` without ``presence``/``panel`` (R1/R2 are not
  in it), so ``hello``/``panel`` answer ``unknown op``.
- the tap ring: 8 events, ``seq`` rising, never acknowledged (``tap(on)`` makes one).

A profile without the features answers those ops ``unknown op`` (the v0.11 bare-metal
shell), and every request is recorded (``requests``) so a test can prove none was sent.
The board-side model is written here independently of the host code, so a host bug is not
mirrored. Touch health: ``touch_ok``/``touch_bus_lost``/``touch_recoveries`` join the
``stats`` reply when set (the Linux lead's additive keys, both engines).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .t12_harness_shell import HarnessFakeShell
from .virtual_board import (
    FIELDED_ILA_V011,
    LINUX_HARNESSD,
    FirmwareProfile,
    VirtualMps3,
    _needs_harness_fake,
)

LINE_MAX = 256
MAX_SESSIONS = 4
RING = 8
ROWS, COLS = 15, 40
#: The board's order (net-protocol v0.17: "after locate, in that order").
PANEL_FEATURES = ("locate", "presence", "panel")
_RANK = {"holder": 0, "owner": 1, "watch": 2}
#: clcd's reformat period: what a hello asks the panel to draw shows this much later.
REFRESH_S = 0.25
#: Role codes, written here from the board's clcd_palette.h enum order (a = text, q =
#: banner-err, u = banner-held), independently of the host's generated table.
CODE_TEXT, CODE_BANNER_HELD = "a", "u"
_BAD_JSON = {"ok": False, "err": "bad json"}

#: The Linux harness with the front-panel verbs (R1-R3), and the bare-metal v0.11 without.
LINUX_PANEL = replace(LINUX_HARNESSD, name=LINUX_HARNESSD.name.replace("linux", "linux-panel"),
                      features=LINUX_HARNESSD.features + PANEL_FEATURES)
#: LOCATE: the rc2_v7/v7n images: ``locate`` (R3) only; ``hello``/``panel`` (R1/R2) are not in it.
LINUX_LOCATE = replace(LINUX_HARNESSD, name=LINUX_HARNESSD.name.replace("linux", "linux-locate"),
                       features=LINUX_HARNESSD.features + ("locate",))
V011_BARE_METAL = FIELDED_ILA_V011

#: A Linux status page (docs/design/clcd/source/preview_lx_feat-linux-harness.txt, healthy).
LINUX_STATUS_ROWS = (
    "MPS3-01             nanoSoC harness     ",
    "-" * COLS,
    "DUT : nanosoc              v1.0         ",
    "SWAP: LOADED VERIFIED     #001  last OK ",
    "SID : 0x14E1A2D8  USD : nanosoc [A]     ",
    "NET : 192.168.10.101  UP 100/FD         ",
    "UP  : 000:00:01:48                      ",
    "DUT : RST-REL  CLK-ALIVE   MMCM-LOCK    ",
    "ICAP: 1835072 B  rxdrop 0 txerr 0       ",
    "CFG : 1x Cortex-M0  no ETH  1x UART     ",
    " " * COLS,
    "hm     david@srv03335  +1 watching      ",
    "SYS : linux  ssh claimed SHA256:AbCdEf  ",
    "-" * COLS,
    "MAC 02:00:00:4D:50:53             hb \\  ",
)


@dataclass
class BoardSession:
    sid: str
    who: str
    role: str
    seen: float
    ttl: int
    app: str = ""
    lease: dict | None = None
    job: dict | None = None


class PanelFakeShell(HarnessFakeShell):
    def __init__(self, host: str = "127.0.0.1", *, board_clock: Any = time.monotonic,
                 page: str = "status", banner: str = "", card: str = "nanosoc [A]",
                 touch_present: bool = True, touch_cal: bool = True,
                 touch_ok: bool | None = None, touch_bus_lost: int | None = None,
                 touch_recoveries: int | None = None, locate_reply_op: bool = True,
                 locate_decline: str = "", theme: str = "today",
                 banner_lag_s: float = REFRESH_S, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self.board_clock = board_clock
        self.theme = theme
        self.banner_lag_s = banner_lag_s
        self.request: tuple[str, str, float, float] | None = None   # (req, by, arrived, until)
        self.locate_reply_op = locate_reply_op
        self.locate_decline = locate_decline
        self.page = page
        self.banner = banner
        self.card = card
        self.touch_present = touch_present
        self.touch_cal = touch_cal
        self.set_touch_health(touch_ok, touch_bus_lost, touch_recoveries)
        self.rows = list(LINUX_STATUS_ROWS)
        self.roles = CODE_TEXT * (ROWS * COLS)
        self.board_sessions: dict[str, BoardSession] = {}
        self.ring: list[tuple[int, str, str, float]] = []      # (seq, k, on, at)
        self.seq = 0
        self.locate_until = 0.0
        self.locates: list[dict[str, Any]] = []
        self.locate_who = ""
        self.locate_tap_stops = 0                               # blinks a tap on the glass ended
        self.hellos: list[dict[str, Any]] = []                  # every hello, as received
        self.requests: list[dict[str, Any]] = []                # every request, as received
        self._panel_mu = threading.Lock()

    # -- knobs --------------------------------------------------------------------------------

    @property
    def panel_verbs(self) -> set[str]:
        return {"hello" if "presence" in self.features else "",
                "panel" if "panel" in self.features else "",
                "locate" if "locate" in self.features else ""} - {""}

    def set_touch_health(self, ok: bool | None, bus_lost: int | None = None,
                         recoveries: int | None = None) -> None:
        for key, value in (("touch_ok", ok), ("touch_bus_lost", bus_lost),
                           ("touch_recoveries", recoveries)):
            if value is None:
                self.stats_extra.pop(key, None)
            else:
                self.stats_extra[key] = value

    @property
    def blinking(self) -> bool:
        """The backlight blinks at 2 Hz (a ``locate`` is running)."""
        return self.locate_until > self.board_clock()

    @property
    def banner_text(self) -> str:
        """The IDENTIFY banner, shown only while the harness owns the panel ("" otherwise:
        with the DUT owning it only the backlight blinks)."""
        if not self.blinking or self.display_owner != "harness":
            return ""
        return f"IDENTIFY: {self.locate_who}"[:COLS]

    def restart_harnessd(self) -> None:
        """harnessd restarted: the blink ends and the backlight is on again."""
        self.locate_until = 0.0

    def tap(self, on: str, *, k: str = "tap", ago_s: float = 0.0) -> int:
        """A touch on the glass: one event in the ring. Returns its ``seq``. A running
        ``locate`` stops (the Linux lead's R3: a tap on the panel stops it)."""
        if self.blinking:
            self.locate_until = self.board_clock()
            self.locate_tap_stops += 1
        with self._panel_mu:
            self.seq += 1
            self.ring.append((self.seq, k, on, self.board_clock() - ago_s))
            del self.ring[:-RING]
            return self.seq

    def restart_ring(self) -> None:
        """harnessd restarted: the ring and ``seq`` start again from nothing."""
        with self._panel_mu:
            self.ring.clear()
            self.seq = 0

    def request_banner(self) -> tuple[str, str, str]:
        """The lease-request banner's three lines as the glass shows them NOW ("" when none):
        a relayed request is drawn from the refresh after its hello, until its ``rl`` ends."""
        if self.request is None:
            return ("", "", "")
        req, by, arrived, until = self.request
        now = self.board_clock()
        if now < arrived + self.banner_lag_s or now >= until:
            return ("", "", "")
        return (f"{req} wants this board", f"held by {by or 'nobody'}",
                f"tap: tell {by or 'the holder'} you are here")

    def drawn_banner(self) -> str:
        """The state's ``banner``: a fault banner (``banner``) outranks the request."""
        return self.banner or self.request_banner()[0]

    def frame_cells(self) -> tuple[list[str], str]:
        """The committed grid and its role codes, the request banner drawn over rows 10-12."""
        rows, roles = list(self.rows), self.roles
        lines = self.request_banner()
        if lines[0] and not self.banner:
            for i, text in enumerate(lines):
                rows[10 + i] = text.center(COLS)[:COLS]
            roles = roles[:10 * COLS] + CODE_BANNER_HELD * (3 * COLS) + roles[13 * COLS:]
        return rows, roles

    def live_sessions(self) -> list[BoardSession]:
        now = self.board_clock()
        with self._panel_mu:
            alive = [s for s in self.board_sessions.values() if now - s.seen <= s.ttl]
        return sorted(alive, key=lambda s: (_RANK.get(s.role, 3), -s.seen))

    # -- the verbs ----------------------------------------------------------------------------

    def handle_control(self, request: dict[str, Any],
                       peer: str | None = None) -> dict[str, Any]:
        self.requests.append(dict(request))
        op = request.get("op")
        if not self.busy and not self.hung and \
                len(json.dumps(request, separators=(",", ":")).encode()) > LINE_MAX:
            return dict(_BAD_JSON)             # the line layer: refused whole, any verb
        if op in ("hello", "panel", "locate") and not self.busy and not self.hung:
            if op not in self.panel_verbs:
                return {"ok": False, "err": f"unknown op {op!r}"}
            return getattr(self, f"_op_{op}")(request)
        return super().handle_control(request, peer)

    def _events(self) -> list[dict[str, Any]]:
        now = self.board_clock()
        with self._panel_mu:
            return [{"seq": seq, "k": k, "on": on, "ms_ago": int(max(0.0, now - at) * 1000)}
                    for seq, k, on, at in self.ring]

    def _panel(self) -> dict[str, Any]:
        return {"page": self.page, "owner": self.display_owner,
                "pending": self.display_owner != self.display_target,
                "banner": self.drawn_banner(), "card": self.card, "seq": self.seq}

    @staticmethod
    def _hello_refusal(request: dict[str, Any]) -> str:
        """presence_core.c ``pres_parse_hello``'s checks, in its order ("" = accepted)."""
        def is_int(v: Any) -> bool:
            return isinstance(v, int) and not isinstance(v, bool)

        v = request.get("v", 1)
        if not is_int(v) or v < 1:
            return "invalid v: an integer >= 1"
        sid = request.get("sid")
        if not isinstance(sid, str) or not sid[:8]:
            return "invalid sid: 1-8 printable characters"
        if not isinstance(request.get("who"), str):
            return "invalid who: a string (user@host)"
        for key in ("app", "name"):
            if key in request and not isinstance(request[key], str):
                return f"invalid {key}: a string"
        if "role" in request and request["role"] not in _RANK:
            return "invalid role: holder, owner or watch"
        if "ttl" in request and not is_int(request["ttl"]):
            return "invalid ttl: an integer (30-300 s)"
        lease = request.get("lease")
        if lease is not None:
            if not isinstance(lease, dict):
                return "invalid lease: a flat object"
            for key in ("by", "req"):
                if key in lease and not isinstance(lease[key], str):
                    return f"invalid lease.{key}: a string"
            if any(key in lease and not is_int(lease[key]) for key in ("left", "q", "rl")):
                return "invalid lease: left, q and rl are integers"
        job = request.get("job")
        if job is not None:
            if not isinstance(job, dict):
                return "invalid job: a flat object"
            if ("k" in job and not isinstance(job["k"], str)) or ("p" in job and not is_int(job["p"])):
                return "invalid job: {k: string, p: integer}"
        return ""

    def _op_hello(self, request: dict[str, Any]) -> dict[str, Any]:
        self.hellos.append(dict(request))
        why = self._hello_refusal(request)
        if why:
            return {"ok": False, "err": why, "code": "invalid"}
        sid, who, role = str(request["sid"])[:8], request["who"], request.get("role", "watch")
        ttl = request.get("ttl", 90)
        now = self.board_clock()
        with self._panel_mu:
            self.board_sessions[sid] = BoardSession(
                sid, who[:20], role, now, max(30, min(300, ttl)),
                str(request.get("app", "")), request.get("lease"), request.get("job"))
            if len(self.board_sessions) > MAX_SESSIONS:
                oldest = min(self.board_sessions.values(), key=lambda s: s.seen)
                del self.board_sessions[oldest.sid]
        lease = request.get("lease") or {}
        if lease.get("req") and lease.get("rl"):
            if self.request is None or self.request[0] != lease["req"]:
                self.request = (str(lease["req"]), str(lease.get("by") or ""), now,
                                now + min(600, int(lease["rl"])))
        elif isinstance(request.get("lease"), dict):
            self.request = None              # this session's lease names no open request
        # The reply is rendered from the COMMITTED panel: what this hello asks to draw is not
        # in it until the next refresh (banner_lag_s).
        return {"ok": True, "op": "hello", "sessions": len(self.live_sessions()),
                "panel": self._panel(), "events": self._events()}

    def _op_panel(self, request: dict[str, Any]) -> dict[str, Any]:
        has_frame = "frame" in request and request["frame"] is not False
        if "page" in request:
            if self.ssh_claimed:
                return {"ok": False, "err": "panel locked: board claimed (use ssh)",
                        "code": "locked"}
            if has_frame:
                return {"ok": False, "err": "invalid request: page takes no frame",
                        "code": "invalid"}
            if request["page"] not in ("status", "apps"):
                return {"ok": False, "err": "invalid page: status or apps", "code": "invalid"}
            if self.display_owner == "dut" or self.display_owner != self.display_target:
                return {"ok": False, "err": "dut owns the panel", "code": "held"}
            self.page = request["page"]
            return {"ok": True, "op": "panel", "page": self.page}
        if has_frame:
            part = request["frame"]
            if part not in ("a", "b"):
                return {"ok": False, "code": "invalid",
                        "err": 'invalid frame: "a" (rows 0-7) then "b" (rows 8-14)'}
            r0, r1 = (0, 8) if part == "a" else (8, ROWS)
            rows, roles = self.frame_cells()
            return {"ok": True, "op": "panel", "frame": part, "theme": self.theme,
                    "rows": rows[r0:r1], "roles": roles[r0 * COLS:r1 * COLS]}
        now = self.board_clock()
        return {"ok": True, "op": "panel", **self._panel(),
                "touch": {"present": self.touch_present, "cal": self.touch_cal},
                "sessions": [{"sid": s.sid, "who": s.who, "role": s.role,
                              "age_s": int(now - s.seen)} for s in self.live_sessions()],
                "events": self._events()}

    def _op_locate(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.locate_decline == "not_supported":
            return {"ok": False, "err": "locate not supported", "code": "not_supported"}
        s, who = request.get("s"), request.get("who", "")
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 30:
            return {"ok": False, "err": "invalid s: 0..30 (seconds; 0 stops)", "code": "invalid"}
        if not isinstance(who, str) or len(who) > 32:
            return {"ok": False, "err": "invalid who: a string of <= 32 characters",
                    "code": "invalid"}
        if any(not " " <= ch <= "~" for ch in who):
            return {"ok": False, "err": "invalid who: printable ASCII only", "code": "invalid"}
        self.locates.append(dict(request))
        self.locate_until = self.board_clock() + s
        if s:
            self.locate_who = who
        if not self.locate_reply_op:
            return {"ok": True, "until_ms": s * 1000}
        return {"ok": True, "op": "locate", "until_ms": s * 1000}


class PanelVirtualMps3(VirtualMps3):
    """``VirtualMps3`` whose shell is a ``PanelFakeShell`` (``LINUX_PANEL`` or v0.11)."""

    def __init__(self, tmp_path: Path, profile: FirmwareProfile = LINUX_PANEL, *,
                 board_clock: Any = time.monotonic, **shell_kwargs: Any) -> None:
        super().__init__(tmp_path, profile)
        assert _needs_harness_fake(profile), "the panel fake extends the harness fake"
        self.shell = PanelFakeShell(
            "127.0.0.1", board_clock=board_clock, features=profile.features, impl=profile.impl,
            lmb_kb=profile.lmb_kb, v011_verbs=profile.v011_verbs, identify=False,
            omit_diag_keys=profile.omit_diag_keys, version_extra=profile.version_extra,
            reboot_outage_s=profile.reboot_outage_s, static_id=profile.static_id,
            boot_rm_id=0, reset_targets=profile.reset_targets,
            harness_version=profile.harness_version, harness_sha=profile.harness_sha,
            harness_usr_access=profile.usr_access, harness_ver32=profile.harness_ver32,
            **self.board_ports.shell_ports, **shell_kwargs)   # held for the board's life (FLAKE-2)
