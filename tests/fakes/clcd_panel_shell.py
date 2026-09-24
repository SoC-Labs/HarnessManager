"""``PanelFakeShell``: the Linux harness's front-panel verbs, as docs/design/CLCD_ALIGNMENT.md
§2.2, §2.5 and §5.3 define them, on top of ``HarnessFakeShell`` (lane P2).

The Linux harness side (R1-R3) is not built yet, so this fake IS the wire until it is:

- ``hello`` (feature ``presence``): the request line is at most 256 B (``MPS3_NET_LINE_MAX``);
  the board keeps at most 4 sessions (the oldest is dropped), lists each for its ``ttl``
  after its last hello, ordered holder > owner > watch, most recent first; the reply is
  ``{ok, op, sessions: N, panel: {page, owner, pending, banner, card, seq}, events}``.
- ``panel`` (feature ``panel``): the state with the session list and ``touch``; with
  ``frame`` ``"a"``/``"b"`` the rows 0-7 / 8-14 and their role codes, with ``true`` all 15.
- ``locate`` (feature ``locate``): ``{s: 1-30, who}`` blinks, ``{s: 0}`` stops;
  ``{ok, until_ms}``.
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
PANEL_FEATURES = ("presence", "panel", "locate")
_RANK = {"holder": 0, "owner": 1, "watch": 2}

#: The Linux harness with the front-panel verbs (R1-R3), and the bare-metal v0.11 without.
LINUX_PANEL = replace(LINUX_HARNESSD, name=LINUX_HARNESSD.name.replace("linux", "linux-panel"),
                      features=LINUX_HARNESSD.features + PANEL_FEATURES)
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
                 touch_recoveries: int | None = None, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self.board_clock = board_clock
        self.page = page
        self.banner = banner
        self.card = card
        self.touch_present = touch_present
        self.touch_cal = touch_cal
        self.set_touch_health(touch_ok, touch_bus_lost, touch_recoveries)
        self.rows = list(LINUX_STATUS_ROWS)
        self.roles = "t" * (ROWS * COLS)
        self.board_sessions: dict[str, BoardSession] = {}
        self.ring: list[tuple[int, str, str, float]] = []      # (seq, k, on, at)
        self.seq = 0
        self.locate_until = 0.0
        self.locates: list[dict[str, Any]] = []
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

    def tap(self, on: str, *, k: str = "tap", ago_s: float = 0.0) -> int:
        """A touch on the glass: one event in the ring. Returns its ``seq``."""
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

    def live_sessions(self) -> list[BoardSession]:
        now = self.board_clock()
        with self._panel_mu:
            alive = [s for s in self.board_sessions.values() if now - s.seen <= s.ttl]
        return sorted(alive, key=lambda s: (_RANK.get(s.role, 3), -s.seen))

    # -- the verbs ----------------------------------------------------------------------------

    def handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(dict(request))
        op = request.get("op")
        if op in ("hello", "panel", "locate") and not self.busy and not self.hung:
            if op not in self.panel_verbs:
                return {"ok": False, "err": f"unknown op {op!r}"}
            line = len(json.dumps(request, separators=(",", ":")).encode()) + 1
            if line > LINE_MAX:
                return {"ok": False, "err": f"line too long ({line} B > {LINE_MAX})"}
            return getattr(self, f"_op_{op}")(request)
        return super().handle_control(request)

    def _events(self) -> list[dict[str, Any]]:
        now = self.board_clock()
        with self._panel_mu:
            return [{"seq": seq, "k": k, "on": on, "ms_ago": int(max(0.0, now - at) * 1000)}
                    for seq, k, on, at in self.ring]

    def _panel(self) -> dict[str, Any]:
        return {"page": self.page, "owner": self.display_owner,
                "pending": self.display_owner != self.display_target,
                "banner": self.banner, "card": self.card, "seq": self.seq}

    def _op_hello(self, request: dict[str, Any]) -> dict[str, Any]:
        self.hellos.append(dict(request))
        sid, who, role = request.get("sid"), request.get("who"), request.get("role", "watch")
        if not (isinstance(sid, str) and 1 <= len(sid) <= 8 and isinstance(who, str)
                and role in _RANK):
            return {"ok": False, "err": "bad args"}
        ttl = request.get("ttl", 90)
        ttl = ttl if isinstance(ttl, int) and not isinstance(ttl, bool) else 90
        with self._panel_mu:
            self.board_sessions[sid] = BoardSession(
                sid, who, role, self.board_clock(), max(30, min(300, ttl)),
                str(request.get("app", "")), request.get("lease"), request.get("job"))
            if len(self.board_sessions) > MAX_SESSIONS:
                oldest = min(self.board_sessions.values(), key=lambda s: s.seen)
                del self.board_sessions[oldest.sid]
        return {"ok": True, "op": "hello", "sessions": len(self.live_sessions()),
                "panel": self._panel(), "events": self._events()}

    def _op_panel(self, request: dict[str, Any]) -> dict[str, Any]:
        now = self.board_clock()
        reply: dict[str, Any] = {"ok": True, **self._panel(),
                                 "touch": {"present": self.touch_present, "cal": self.touch_cal},
                                 "sessions": [{"sid": s.sid, "who": s.who, "role": s.role,
                                               "age_s": int(now - s.seen)}
                                              for s in self.live_sessions()],
                                 "events": self._events()}
        part = request.get("frame")
        if part in (True, "a", "b"):
            r0, r1 = {True: (0, ROWS), "a": (0, 8), "b": (8, ROWS)}[part]
            reply["rows"] = self.rows[r0:r1]
            reply["roles"] = self.roles[r0 * COLS:r1 * COLS]
        return reply

    def _op_locate(self, request: dict[str, Any]) -> dict[str, Any]:
        s = request.get("s")
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 30:
            return {"ok": False, "err": "bad args"}
        self.locates.append(dict(request))
        self.locate_until = self.board_clock() + s
        return {"ok": True, "until_ms": s * 1000}


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
            control_port=0, tftp_port=0, raw_tcp_port=0, uart0_port=0, uart1_port=0,
            swo_port=0, **shell_kwargs)
