"""The XVC routes (docs/API.md "Fabric debug over XVC", ``xvc_api.py``) in the T14 mock daemon.

Lane XVC-CORE (X3) builds the real routes; the web card (X4) is built and tested against
this: the same routes, bodies, 202 jobs, refusals and ``xvc.state`` events, over the
mock's ``DemoEngine`` boards, with nothing listening anywhere (the ports are made up and
no hw_server runs). The status objects are the real ``XvcStatus`` and the Tcl the real
``vivado_tcl``, so the mock and the daemon cannot disagree on their shape or wording.

Knobs (``XvcSim``): ``hold_slot(bid, who)`` makes the board's slot held by someone else,
``attach(bid)`` pretends Vivado attached, ``ltx_body`` is the probes file served.
Behind a hub (``WeekPlanSim.behind_hub``), ``open`` is for the lease holder only.
Lane XVC-UI adds ``slot_taken(bid, who)`` (an open session loses the board's slot to
another client: ``held``), ``stage_full(bid)`` (the mint staged a full-design file, X5,
which is then preferred), ``result_delay_s`` (the open job answers late) and the static
(MIG) file: a Linux harness has one, a bare-metal
one says why not (``static_note``, the MPS3 pack's words).
"""

from __future__ import annotations

import threading
import time
import zlib
from typing import Any

from fastapi import Body, FastAPI, Response

from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HeldError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.services.xvc import (
    PARTITION_SCOPE,
    SWAP_NOTE,
    TOPIC,
    UNAUTHENTICATED_WARNING,
    XvcStatus,
    vivado_tcl,
)

API = "/api/v1"
WHICH = ("auto", "rm", "static", "full")
JTAGBB_REASON = ("2542 on this image drives jtag_bb (the Identify path), not the Debug Bridge: "
                 "no ILAs to debug over XVC")          # harness_manager_mps3.xvc.JTAGBB_REASON
NO_MIG_NOTE = "the bare-metal static has no MIG debug hub"  # harness_manager_mps3.xvc.NO_MIG_NOTE


class XvcSim:
    """Per-board simulated XVC sessions."""

    def __init__(self, sim: Any) -> None:
        self.sim = sim                         # l3_week_plan.WeekPlanSim
        self.engine = sim.engine
        self._lock = threading.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.held_by: dict[str, str] = {}
        self.full: set[str] = set()            # boards whose mint staged a full-design .ltx
        self.result_delay_s = 0.0              # the open job answers this long after "ready"
        self.ltx_body = b'{"probes": [{"name": "ila_0", "type": "ila"}]}\n'
        self.engine.bus.subscribe("deploy.started", self._swap_started)
        self.engine.bus.subscribe("deploy.done", self._swap_done)
        self.engine.bus.subscribe("deploy.failed", self._swap_failed)

    # -- knobs -----------------------------------------------------------------------------

    def hold_slot(self, bid: str, who: str = "a hub user's Vivado") -> None:
        self.held_by[bid] = who

    def slot_taken(self, bid: str, who: str = "a hub user's Vivado") -> None:
        """An open session loses the board's slot, and another client has it now."""
        if self._set(bid, state="held", slot="held", attached=None,
                     detail=f"the board's slot is held: accepted, then closed ({who})"):
            self._publish(bid)

    def stage_full(self, bid: str) -> None:
        """The mint staged a full-design probes file for the loaded design (X5)."""
        self.full.add(bid)

    def attach(self, bid: str, command: str = "hw_server -q -p0") -> None:
        with self._lock:
            s = self.sessions.get(bid)
            if s is not None:
                s["attached"] = {"peer": "127.0.0.1:51234", "pid": s["hw_pid"] or 4242,
                                 "command": command, "since": time.time(), "bytes_up": 0,
                                 "bytes_down": 0, "shifts": 0}
        self._publish(bid)

    # -- facts -------------------------------------------------------------------------------

    def _ident(self, bid: str) -> Any:
        return self.sim.identity(bid)

    def reason(self, bid: str) -> str:
        feats = set(getattr(self._ident(bid), "features", ()) or ())
        if "xvc_jtagbb" in feats and "xvc_dbgbr" not in feats:
            return JTAGBB_REASON
        if "xvc_dbgbr" not in feats:
            return "needs harness firmware with 'xvc_dbgbr' (v0.11 or later)"
        return ""

    def facts(self, bid: str) -> dict[str, Any]:
        ident = self._ident(bid)
        linux = getattr(ident, "harness_impl", "") == "linux"
        locked = "xvc_lock" in (getattr(ident, "features", ()) or ())
        reach = "board-ssh" if linux else ("hub-tunnel" if bid in self.sim.hubs else "direct")
        return {"reach": reach, "warnings": [] if (linux and locked) else
                [UNAUTHENTICATED_WARNING]}

    def ltx(self, bid: str) -> dict[str, Any]:
        ident = self._ident(bid)
        name = getattr(ident, "rm_name", "") or ""
        linux = getattr(ident, "harness_impl", "") == "linux"
        static = ({"path": "/tmp/harness-manager-mock/config_rm_greybox_static.ltx",
                   "name": "config_rm_greybox_static.ltx", "crc_ok": None, "source": "mock",
                   "vivado": "2026.1"} if linux else None)
        static_note = "" if linux else NO_MIG_NOTE
        if not name or getattr(ident, "rm_id", "") in ("", "0x00000000"):
            return {"rm": None, "static": static, "full": None, "preferred": None,
                    "note": "the greybox has no ILAs", "static_note": static_note}
        rm = {"path": f"/tmp/harness-manager-mock/{name}.ltx", "name": f"{name}.ltx",
              "crc_ok": True, "source": "mock", "vivado": "2024.1"}
        full = ({"path": f"/tmp/harness-manager-mock/{name}_full.ltx",
                 "name": f"{name}_full.ltx", "crc_ok": None, "source": "mock",
                 "vivado": "2024.1"} if bid in self.full else None)
        return {"rm": rm, "static": static, "full": full, "preferred": "full" if full else "rm",
                "note": "", "static_note": static_note}

    # -- status ------------------------------------------------------------------------------

    def status(self, bid: str) -> XvcStatus:
        ident = self._ident(bid)
        facts = self.facts(bid)
        with self._lock:
            s = dict(self.sessions.get(bid) or {})
        if not s:
            return XvcStatus(state="down", reach=facts["reach"],
                             warnings=tuple(facts["warnings"]), reason=self.reason(bid))
        warnings = list(facts["warnings"])
        if s["mode"] == "byo":
            warnings.append("your own hw_server lingers 20 s after its last client: after a "
                            "swap, close and reopen the target (or wait it out)")
        warnings.append(SWAP_NOTE)
        state = s["state"]
        if state == "ready" and s.get("attached"):
            state = "attached"
        return XvcStatus(
            state=state, open=True, mode=s["mode"], relay_port=s["relay"],
            hw_server_port=s["relay"] + 1 if s["mode"] == "m1" else 0,
            hw_server_pid=s["hw_pid"],
            hw_server="/tools/Xilinx/Vivado/2024.1/bin/hw_server (2024.1)"
            if s["mode"] == "m1" else "",
            url=f"localhost:{s['relay'] + 1}" if s["mode"] == "m1" else f"127.0.0.1:{s['relay']}",
            attached=s.get("attached"), board_slot=s["slot"], reach=facts["reach"],
            ltx=self.ltx(bid), warnings=tuple(warnings), scope=PARTITION_SCOPE,
            rm_id=getattr(ident, "rm_id", ""), rm_name=getattr(ident, "rm_name", ""),
            detail=s.get("detail", ""))

    def _publish(self, bid: str, st: XvcStatus | None = None) -> None:
        st = st or self.status(bid)
        self.engine.bus.publish(Event(TOPIC, bid, st.to_json()))

    # -- swaps -------------------------------------------------------------------------------

    def _set(self, bid: str, **changes: Any) -> bool:
        with self._lock:
            s = self.sessions.get(bid)
            if s is None:
                return False
            s.update(changes)
            return True

    def _swap_started(self, ev: Event) -> None:
        if self._set(ev.board_id, state="swapping", slot="released", attached=None, hw_pid=0,
                     detail="closed for a partition swap; it reopens on the new design"):
            self._publish(ev.board_id)

    def _swap_done(self, ev: Event) -> None:
        with self._lock:
            s = self.sessions.get(ev.board_id)
        if s is None or s["state"] != "swapping":
            return
        if not ev.data.get("verified"):
            self.close(ev.board_id, "XVC not reopened: the swap was not verified by the board; "
                                    "check what is loaded")
            return
        pid = 4000 + zlib.crc32(ev.board_id.encode()) % 900 if s["mode"] == "m1" else 0
        self._set(ev.board_id, state="ready", slot="ours", hw_pid=pid,
                  detail="reopened on the new design: re-run the probes lines of `xvc tcl`")
        self._publish(ev.board_id)

    def _swap_failed(self, ev: Event) -> None:
        with self._lock:
            s = self.sessions.get(ev.board_id)
        if s is not None and s["state"] == "swapping":
            stage = ev.data.get("stage") or "an unknown stage"
            self.close(ev.board_id, f"XVC not reopened: the swap failed at {stage}; check "
                                    "what is loaded")

    # -- actions -----------------------------------------------------------------------------

    def check_open(self, bid: str) -> None:
        if bid in self.sessions:
            raise AlreadyError(f"the XVC session for {bid} is already open",
                               hint="`xvc close` first to restart it")
        hub = self.sim.hubs.get(bid)
        if hub is not None:
            lease = hub.get("lease")
            if not lease:
                raise HeldError(f"XVC is for the lease holder only, and nobody holds "
                                f"{hub['target']}", holder="nobody",
                                hint="take the lease first: `harness-manager lease acquire TARGET`")
            if not lease.get("mine"):
                raise HeldError(f"XVC is for the lease holder only: {lease['holder']} holds "
                                f"{hub['target']}", holder=lease["holder"],
                                hint="ask for the board: `harness-manager lease request TARGET`")
        reason = self.reason(bid)
        if reason:
            raise UnavailableError("debug_fabric", reason)

    def open(self, bid: str, byo: bool) -> XvcStatus:
        who = self.held_by.get(bid)
        if who:
            raise HeldError(f"the board's XVC slot is held by another client ({who})", holder=who,
                            hint="the harness serves one XVC client; close the other one first")
        relay = 23600 + 2 * (zlib.crc32(bid.encode()) % 64)
        pid = 4000 + zlib.crc32(bid.encode()) % 900 if not byo else 0
        with self._lock:
            self.sessions[bid] = {"state": "ready", "mode": "byo" if byo else "m1",
                                  "relay": relay, "hw_pid": pid, "slot": "ours",
                                  "attached": None, "detail": ""}
        st = self.status(bid)
        self._publish(bid, st)
        return st

    def close(self, bid: str, detail: str = "closed") -> XvcStatus:
        with self._lock:
            self.sessions.pop(bid, None)
        st = XvcStatus(state="down", reach=self.facts(bid)["reach"], detail=detail,
                       warnings=tuple(self.facts(bid)["warnings"]))
        self._publish(bid, st)
        return st


def register(app: FastAPI, state: Any, sim: Any, ok: Any, accepted: Any) -> XvcSim:
    """Add the five XVC routes to the mock app. Returns the simulation (its knobs)."""
    xs = XvcSim(sim)

    def body_of(bid: str, st: XvcStatus) -> dict[str, Any]:
        return ok(board_id=bid, **st.to_json())

    @app.get(f"{API}/boards/{{bid:path}}/xvc/tcl")
    def xvc_tcl(bid: str, byo: str | None = None) -> dict[str, Any]:
        state.session(bid)
        if byo not in (None, "", "true", "false", "1", "0", "yes", "no"):
            raise UsageError(f"byo must be true or false, not {byo!r}")
        st = xs.status(bid)
        want_byo = (st.mode == "byo") if byo in (None, "") else byo in ("true", "1", "yes")
        ltx = xs.ltx(bid)
        key = ltx.get("preferred")
        path = (ltx.get(key) or {}).get("path") if key else None
        if st.open:
            hw, xvc = f"localhost:{st.relay_port + 1}", f"127.0.0.1:{st.relay_port}"
        else:
            hw, xvc = "localhost:<H: run `xvc open` first>", "127.0.0.1:<R>"
        return ok(board_id=bid, tcl=vivado_tcl(hw_server_url=hw, xvc_url=xvc, ltx=path,
                                               byo=want_byo),
                  url=xvc if want_byo else hw, ltx=path, which=key,
                  mode="byo" if want_byo else "m1", scope=PARTITION_SCOPE, open=st.open)

    @app.get(f"{API}/boards/{{bid:path}}/xvc/ltx")
    def xvc_ltx(bid: str, which: str = "auto", format: str = "file") -> Any:  # noqa: A002
        state.session(bid)
        if which not in WHICH:
            raise UsageError(f"which must be one of {', '.join(WHICH)}, not {which!r}")
        if format not in ("file", "json"):
            raise UsageError(f"format must be file or json, not {format!r}")
        ltx = xs.ltx(bid)
        key = ltx.get("preferred") if which == "auto" else which
        item = ltx.get(key) if key else None
        if not item:
            raise AbsentError("no probes file (.ltx) for the loaded design",
                              hint="the RM's .ltx comes with its overlay")
        if format == "json":
            return ok(board_id=bid, which=key, **item)
        return Response(xs.ltx_body, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{item["name"]}"',
                                 "X-Ltx-Which": key})

    @app.post(f"{API}/boards/{{bid:path}}/xvc/open")
    def xvc_open(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> Any:  # noqa: B008
        state.session(bid)
        byo = body.get("byo", False)
        if not isinstance(byo, bool):
            raise UsageError(f"byo must be true or false, not {byo!r}")
        state.jobs.gate(bid)
        xs.check_open(bid)

        def work(progress: Any) -> dict[str, Any]:
            progress("starting", 0, 0)
            st = xs.open(bid, byo)
            progress(st.state, 0, 0)
            if xs.result_delay_s:              # the job's answer lands after later events
                time.sleep(xs.result_delay_s)
            return body_of(bid, st)

        return accepted(state.jobs.start(bid, "xvc_open", work))

    @app.post(f"{API}/boards/{{bid:path}}/xvc/close")
    def xvc_close(bid: str) -> dict[str, Any]:
        state.session(bid)
        state.jobs.gate(bid)
        return body_of(bid, xs.close(bid))

    @app.get(f"{API}/boards/{{bid:path}}/xvc")
    def xvc_status(bid: str) -> dict[str, Any]:
        state.session(bid)
        return body_of(bid, xs.status(bid))

    return xs
