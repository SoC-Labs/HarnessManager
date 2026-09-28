"""Fakes for lane HIL-AUTO's runner (``harness_manager.checks``; ``tools/hil`` is its command line).

- ``InProcessHm``: Harness Manager's REAL CLI (``harness_manager.cli.main.main``) run in this
  process, stdout/stderr captured, stdin empty (a prompt fails, as under the runner's
  /dev/null). Behind it sit the virtual boards and the fake hub a test wires up, exactly as
  the L1 and CLAIMED-LOCK tests do. It records every argv.
- ``ScriptedHm``: a counting fake of the CLI, for the safety rules: each verb answers the
  JSON the real one prints, from a small board state (the partition, the lease); a test
  queues other answers (``queue``) or flips a knob. ``calls`` is every argv it got.
- ``HubProbe``: a hub runner that answers ``board claim-status``'s hub-side identify probe
  from the fake board's own identify port (the probe runs pyverify on the hub; here the test
  process plays the hub), and hands everything else to the wrapped runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from collections.abc import Callable
from typing import Any

from harness_manager.checks.run import Outcome

#: The verbs that change something (the board, its cards, the SD, the hub, the lease, the
#: claim). ``--writes none`` must never send one; ``--writes safe`` only program/restore.
WRITE_VERBS = {"program", "restore", "reset", "clock", "lab", "sd", "power", "attach",
               "slot push", "slot commit", "slot rollback", "card clear", "card commit",
               "mcc reboot", "mcc cmd", "mcc temp", "share start", "lease acquire",
               "lease release", "lease force", "lease request", "lease respond", "lease leave",
               "board claim", "harness install", "harness pin", "harness unpin",
               "harness rollback", "update", "identify", "xvc open", "debug up"}


def verb_of(argv: list[str]) -> str:
    """``["--json", "mcc", B, "temp"]`` -> ``"mcc temp"``; ``["info", B]`` -> ``"info"``."""
    words = [a for a in argv if a not in ("--json", "--tsv")]
    if not words:
        return ""
    head = words[0]
    if head == "mcc" and len(words) >= 3:
        return f"mcc {words[2]}"
    if head in ("lease", "share", "slot", "card", "board", "panel", "xvc", "debug", "harness",
                "power") and len(words) >= 2:
        return f"{head} {words[1]}"
    return head


def is_write(argv: list[str]) -> bool:
    v = verb_of(argv)
    return v in WRITE_VERBS or v.split(" ")[0] in WRITE_VERBS or "--keep-on-card" in argv \
        or "--force" in argv


class InProcessHm:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> Outcome:
        from harness_manager.cli.main import main

        self.calls.append(list(argv))
        out, err = io.StringIO(), io.StringIO()
        t0 = time.monotonic()
        stdin = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        finally:
            sys.stdin = stdin
        return Outcome(int(rc), out.getvalue(), err.getvalue(), time.monotonic() - t0)


class HubProbe:
    """Answers the claim probe (``claim.hub_argv("identify", ...)``) from ``identify_port``."""

    def __init__(self, inner: Any, identify_port: int) -> None:
        self.inner = inner
        self.identify_port = identify_port
        self.probes = 0

    def __call__(self, argv: Any, timeout: float | None = None) -> Any:
        argv = list(argv)
        if argv[:2] == ["sh", "-c"] and len(argv) >= 8 and argv[4] == "identify":
            from pyverify.identify import identify
            from pyverify.lease import RunResult

            self.probes += 1
            r = identify("127.0.0.1", self.identify_port, timeout=2.0, retries=1)
            return RunResult(0, json.dumps({"ok": True, "reply": r.raw}) + "\n", "")
        return self.inner(argv, timeout=timeout)


# --- the scripted CLI -------------------------------------------------------------------------------

GREYBOX = "0x00000000"
RMS = {"greybox": GREYBOX, "nanosoc": "0x01000001", "nanosoc_ila": "0x0100000a"}
Answer = Callable[["ScriptedHm", list[str]], tuple[int, dict[str, Any] | str]]


def _err(code: int, name: str, message: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return code, {"ok": False, "error": {"code": code, "name": name, "message": message,
                                         "hint": "", **extra}}


def held(message: str) -> Answer:
    return lambda _hm, _argv: _err(4, "HELD", message)


def unreachable(message: str = "no route to the board") -> Answer:
    return lambda _hm, _argv: _err(7, "UNREACHABLE", message)


def refused(message: str) -> Answer:
    return lambda _hm, _argv: _err(15, "REFUSED", message)


class ScriptedHm:
    """A board and a hub, as the CLI's JSON shows them. ``impl`` ``linux`` or ``bare-metal``;
    ``card`` False: no user microSD at all (board 2: ``slot status`` answers ``card: false``)."""

    def __init__(self, *, static: str = "0x44ee76d5", impl: str = "linux",
                 netboot: bool = True, claim: str = "other", card: bool = True) -> None:
        self.static, self.impl, self.netboot, self.claim = static, impl, netboot, claim
        self.card = card
        self.rm = GREYBOX
        self.calls: list[list[str]] = []
        self.lease_here = True
        self.lease_holder = "david-hm"
        self.lease_expires = "2026-09-29T09:00:00+00:00"
        self.queue: dict[str, list[Answer]] = {}
        #: after a swap the board reports this static (an unexpected identity)
        self.static_after_swap: str | None = None
        self.panel_source = "rebuilt"

    def add(self, verb: str, *answers: Answer) -> None:
        self.queue.setdefault(verb, []).extend(answers)

    def verbs(self) -> list[str]:
        return [verb_of(a) for a in self.calls]

    def __call__(self, argv: list[str], timeout: float) -> Outcome:
        self.calls.append(list(argv))
        verb = verb_of(argv)
        queued = self.queue.get(verb)
        if queued:
            rc, body = queued.pop(0)(self, argv)
        else:
            handler = getattr(self, "v_" + verb.replace(" ", "_").replace("-", "_"), None)
            rc, body = handler(argv) if handler else _err(2, "USAGE", f"no verb {verb!r}")
        if isinstance(body, str):
            return Outcome(rc, body, "", 0.01)
        err = "" if rc == 0 else "harness-manager: " + body["error"]["message"] + "\n"
        return Outcome(rc, json.dumps(body, sort_keys=True) + "\n", err, 0.01)

    # -- the verbs --

    def v_version(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "version": "0.0.0-test", "engine": "scripted"}

    def v_lease_show(self, _a: list[str]) -> tuple[int, Any]:
        lease = None if self.lease_holder is None else {
            "holder": self.lease_holder, "here": self.lease_here, "mine": True,
            "holder_kind": "hm" if self.lease_here else "unknown",
            "expires_at": self.lease_expires, "target": "mps3_01_pl", "user": "david"}
        return 0, {"ok": True, "lease": lease, "notes_supported": True, "can_revoke": True,
                   "queue": [], "request": None, "incoming": [], "taken": None}

    def v_share_list(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "shares": []}

    def v_info(self, _a: list[str]) -> tuple[int, Any]:
        static = self.static
        if self.static_after_swap and self.rm != GREYBOX:
            static = self.static_after_swap
        features = ["usd", "stats"] if self.impl == "linux" else ["clcd", "windowed"]
        ident = {"shell_id": static, "harness_impl": self.impl, "rm_id": self.rm,
                 "harness_version": "1.0.0", "features": features, "ver32": "",
                 "usercode": ""}
        body: dict[str, Any] = {
            "ok": True, "identity": ident, "health": {"reachable": True},
            "capabilities": ["console_dut", "console_controller", "telemetry_temp"]}
        if self.impl == "linux":
            body["claim"] = {"state": "unknown"}
        return 0, body

    def v_panel_show(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "panel": {"source": self.panel_source, "owner": "harness",
                                         "touch": {"ok": True, "bus_lost": 0}}}

    def v_xvc_status(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "state": "down", "reach": "hub-tunnel",
                   "scope": "... It is never whole-device JTAG: ...",
                   "warnings": ["XVC on this harness is unauthenticated: ..."]}

    def v_board_claim_status(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "claim": {"state": self.claim, "host_key": {"match": None}}}

    def v_board_ssh(self, _a: list[str]) -> tuple[int, Any]:
        return 0, ("backing=tmpfs storage=ok\n" if self.netboot or not self.card
                   else "backing=card dev=/dev/mmcblk0p3 storage=ok\n")

    def v_slot_status(self, _a: list[str]) -> tuple[int, Any]:
        if not self.card:
            # harnessd: card:false, ok; HM's slot service: UNAVAILABLE (os_slots.slots_reason)
            reason = "no user microSD card in the slot (the OS slots live on it)"
            return _err(12, "UNAVAILABLE", f"OS slot update is unavailable: {reason}",
                        capability="OS slot update", reason=reason)
        st = "empty" if self.netboot else "valid"
        return 0, {"ok": True, "card": True, "running": "A", "default": "A",
                   "job": {"busy": False}, "slots": {"A": {"state": st}, "B": {"state": st}}}

    def v_card_status(self, _a: list[str]) -> tuple[int, Any]:
        if not self.card:
            return 0, {"ok": True, "present": False, "state": "no card", "line": ""}
        return 0, {"ok": True, "present": True, "state": "valid",
                   "line": "none: the greybox loads at power-on"}

    def v_overlays(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "compatible": [{"name": n, "rm_id": r} for n, r in RMS.items()],
                   "incompatible": []}

    def v_mcc_temp(self, _a: list[str]) -> tuple[int, Any]:
        return 0, {"ok": True, "readings": [{"name": "mcc_temp", "available": True,
                                             "source": "mcc-console (hub)", "value": 35.5}]}

    def v_program(self, a: list[str]) -> tuple[int, Any]:
        words = [w for w in a if w != "--json"]
        self.rm = RMS[words[2]]
        return 0, {"ok": True, "result": {"verified": True, "rm_id": self.rm,
                                          "transport": "tcp", "card": None, "seconds": 1.0}}

    def v_restore(self, _a: list[str]) -> tuple[int, Any]:
        self.rm = GREYBOX
        return 0, {"ok": True, "result": {"verified": True, "rm_id": GREYBOX,
                                          "transport": "tcp", "card": None}}

    def v_identify(self, _a: list[str]) -> tuple[int, Any]:
        return _err(12, "UNAVAILABLE", "locate is unavailable: needs harness feature 'locate' "
                                       "(Linux harness)")

    def v_debug_detect(self, _a: list[str]) -> tuple[int, Any]:
        return _err(13, "NOTHING_ON_TARGET", "the loaded design (greybox) has no debug port")

    def v_harness_list(self, _a: list[str]) -> tuple[int, Any]:
        return _err(7, "UNREACHABLE", "channel.json not found")
