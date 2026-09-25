"""Team T9: a virtual MPS3 whose harness reports ``sysmon`` / ``touch_temp`` (a FUTURE profile).

pyverify's FakeShell does not know these features yet (its VERSION_FEATURES would
refuse them) and has no ``stats`` verb. Until the harness agent adds them (T9 CCR 3),
``T9Shell`` extends it for T9's tests only, answering in the wire shape T9 proposes:

- ``telemetry``: FakeShell's exact failure line plus ``"touch_temp_c": <float|null>``;
- ``stats``: ``{"ok":true, ..., "sysmon": {<raw 16-bit DRP codes>}}``.

``VirtualMps3`` itself is lead-owned and untouched: ``t9_virtual_board`` swaps its
shell for a ``T9Shell`` before starting it.

The installed pyverify may predate the v0.11 ``stats()`` codec; ``install_v011_stats``
gives it a stand-in with the same request and ``raw`` carrier, for tests only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyverify.client import ShellClient
from pyverify.testing.fakeshell import FakeShell

from tests.fakes.t9_sysmon import GOOD_REGS
from tests.fakes.virtual_board import FIELDED_3F1A560F, VirtualMps3

STATS_SYSMON = {
    "temp": GOOD_REGS[0x00], "vccint": GOOD_REGS[0x01], "vccaux": GOOD_REGS[0x02],
    "vccbram": GOOD_REGS[0x06],
    "temp_max": GOOD_REGS[0x20], "vccint_max": GOOD_REGS[0x21], "vccaux_max": GOOD_REGS[0x22],
    "vccbram_max": GOOD_REGS[0x23],
    "temp_min": GOOD_REGS[0x24], "vccint_min": GOOD_REGS[0x25], "vccaux_min": GOOD_REGS[0x26],
    "vccbram_min": GOOD_REGS[0x27],
    "flag": GOOD_REGS[0x3F],
}
_MISSING = object()


class T9Shell(FakeShell):
    touch_temp_c: Any = 23.9            # _MISSING: key absent; None: null on the wire
    sysmon: Any = STATS_SYSMON          # _MISSING: key absent
    stats_supported = True
    ops: list[str]

    def handle_control(self, request: dict[str, Any],
                       peer: str | None = None) -> dict[str, Any]:
        self.ops.append(str(request.get("op")))
        op = request.get("op")
        if op == "telemetry":
            reply = super().handle_control(request, peer)
            if self.touch_temp_c is not _MISSING:
                reply["touch_temp_c"] = self.touch_temp_c
            return reply
        if op == "stats":
            if not self.stats_supported:
                return {"ok": False, "err": "unknown op 'stats'"}
            reply = {"ok": True, "up_ms": 123456, "sid": "0x3f1a560f", "rm": "0x00000000",
                     "rm_ok": True, "swap": "idle", "swap_ok": True, "swap_n": 0}
            if self.sysmon is not _MISSING:
                reply["sysmon"] = self.sysmon
            return reply
        return super().handle_control(request, peer)


MISSING = _MISSING


@dataclass(frozen=True)
class _StatsResponse:
    ok: bool
    err: str = ""
    raw: dict = field(default_factory=dict)


def _v011_stats(self: ShellClient) -> _StatsResponse:
    """Stand-in for pyverify's v0.11 ``ShellClient.stats()`` (platform d68dd0e): same
    request, same ``raw`` carrier for keys the codec does not model."""
    resp = self._request({"op": "stats"})
    return _StatsResponse(ok=bool(resp["ok"]), err=str(resp.get("err", "")), raw=dict(resp))


def install_v011_stats(monkeypatch: Any) -> bool:
    """Give the installed pyverify a ``stats()`` if it lacks one; True if it was added."""
    if callable(getattr(ShellClient, "stats", None)):
        return False                     # the real v0.11 codec is installed: use it
    monkeypatch.setattr(ShellClient, "stats", _v011_stats, raising=False)
    return True


def t9_virtual_board(tmp_path: Path, *, features: tuple[str, ...] = ("sysmon", "touch_temp"),
                     usb: bool = False) -> VirtualMps3:
    """A VirtualMps3 (fielded profile) whose shell also reports ``features``."""
    vb = VirtualMps3(tmp_path, usb=usb)
    p = FIELDED_3F1A560F
    shell = T9Shell.ephemeral(
        static_id=p.static_id, boot_rm_id=0, reset_targets=p.reset_targets,
        harness_version=p.harness_version, harness_sha=p.harness_sha,
        harness_usr_access=p.usr_access, features=p.features,
        **vb.board_ports.shell_ports)                        # held for the board's life (FLAKE-2)
    shell.features = tuple(p.features) + tuple(features)   # bypasses FakeShell's known-feature check
    shell.ops = []
    vb.shell = shell
    return vb
