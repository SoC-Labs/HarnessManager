"""The harness versions routes (docs/API.md "Harness versions", ``harness_api.py``) in the
T14 mock daemon.

Like ``kit_mock``, the mock runs the REAL routes (``daemon/harness_api.register``) and the
REAL catalogue (``services/harness_catalog``: verdicts, marks, what changes, pins, history,
rollback candidates), so the mock and the daemon cannot disagree on a shape or a refusal.
Only the update service under them is simulated (``SimUpdate``): a fixed, parsed (unsigned)
catalogue, the DemoEngine board's identity, and installs that only change which release
the demo board "runs". Nothing is downloaded, written to a board or signed.

The catalogue: ``stable`` 1.0.0 @0x3f1a560f (fw cb31b0f2: what the demo boards run),
1.1.0 and 1.1.1 @0x72bb0a36; ``beta`` adds 2.0.0 (Linux). Knobs: the week-plan sim's
``behind_hub(bid, lease=...)`` (an install needs the lease), ``SimUpdate.outcome``
(``written-not-running`` fails the job), ``SimUpdate.withdrawn`` (releases the publisher
withdrew: incompatible). State (pins, history) lives in a private temporary directory,
removed with the app.
"""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, FastAPI

from harness_manager.core.errors import AbsentError, RefusedError
from harness_manager.core.events import Event
from harness_manager.core.model import BoardIdentity
from harness_manager.daemon import harness_api
from harness_manager.daemon.app import RouteContext
from harness_manager.services.update.channel import VerifiedChannel
from harness_manager.services.update.executor import UpdateOutcome, _record_fields
from harness_manager.services.update.lease_gate import lease_state, require_lease
from harness_manager.services.update.planner import BoardView, make_plan
from harness_manager.services.update.schema import harness_catalog, parse_channel
from harness_manager.services.update.state import InstallRecords, Pins, UpdateState

API = "/api/v1"
V08 = ["clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"]
V011 = V08 + ["dut_egress", "jtag_server", "xvc_dbgbr", "stats", "log", "reboot", "touch_cal"]


def _h(*parts: str) -> str:
    return hashlib.sha256("/".join(parts).encode()).hexdigest()


def _sd(version: str, size: int) -> dict[str, Any]:
    return {"name": "sd-HBI0309C", "target": "mcc-sd", "kind": "sd", "size": size,
            "url": f"../../assets/{version}/mps3-harness-{version}-sd-HBI0309C.zip",
            "sha256": _h("sd", version),
            "files": {"MB/HBI0309C/Nanosoc/nanosoc.bit": _h("bit", version)}}


def _ovl(version: str, static: str, aaa: bool = False) -> dict[str, Any]:
    name = "overlays-aaa" if aaa else "overlays-open"
    comp = {"name": name, "target": "host-store", "kind": "overlays", "size": 2_400_000,
            "url": f"../../assets/{version}/mps3-harness-{version}-{name}.zip",
            "sha256": _h(name, static),
            "ip_class": "arm-aaa" if aaa else "open"}
    if aaa:
        comp.update(access="github-token", repo="SoC-Labs/mps3-harness-aaa")
    return comp


def _release(version: str, static: str, usercode: str, fw: str, impl: str, feats: list[str],
             proto: str, vivado: str, notes: str, *, os_image: bool = False) -> dict[str, Any]:
    comps = [_sd(version, 12_433_250), _ovl(version, static), _ovl(version, static, aaa=True)]
    if os_image:
        comps.append({"name": "os-slot", "target": "user-usd", "kind": "os-slot", "format": "raw",
                      "size": 24_117_248, "url": f"../../assets/{version}/linux_slot.img",
                      "sha256": _h("os", version)})
    return {"version": version, "status": "current", "rekey": False, "vivado": vivado,
            "notes": notes, "notes_url": f"https://example.invalid/harness/{version}",
            "released_at": "2026-09-24T12:00:00Z",
            "identity": {"static_id": static, "usercode": usercode, "harness": "1.0.0",
                         "impl": impl, "proto": proto, "features": feats, "fw_sha": fw,
                         "ver32": "0x01000000"},
            "compat": {"min_app": "0.0.1", "board_revs": ["HBI0309C"],
                       "mcc_fw_tested": ["1.3.2"], "net_protocol": proto},
            "components": comps}


RELEASES = {
    "2.0.0": _release("2.0.0", "0x4c1a0003", "0x3c0ffee3", "5eed0000", "linux", V011[1:],
                      "0.14", "2026.1", "The MicroBlaze V Linux harness.", os_image=True),
    "1.1.1": _release("1.1.1", "0x72bb0a36", "0xc8551081", "0e12a0b0", "bare-metal", V011,
                      "0.12", "2024.1", "Firmware re-bake: the console flush fix."),
    "1.1.0": _release("1.1.0", "0x72bb0a36", "0xc8551081", "d68dd0ed", "bare-metal", V011,
                      "0.11", "2024.1", "RM ILAs over XVC; the ILA static."),
    "1.0.0": _release("1.0.0", "0x3f1a560f", "0xd46fcdcb", "cb31b0f2", "bare-metal", V08,
                      "0.10", "2024.1", "The fielded static until 09-24."),
}


def _doc(channel: str, serial: int, withdrawn: frozenset[str] = frozenset()) -> dict[str, Any]:
    versions = ["1.1.1", "1.1.0", "1.0.0"] if channel == "stable" else \
        ["2.0.0", "1.1.1", "1.1.0", "1.0.0"]
    current = versions[0]
    rels = []
    for v in versions:
        r = dict(RELEASES[v])
        r["status"] = "withdrawn" if v in withdrawn else "current" if v == current else "superseded"
        rels.append(r)
    for i, r in enumerate(rels[:-1]):
        r["rekey"] = r["identity"]["static_id"] != rels[i + 1]["identity"]["static_id"]
    return {"schema": "harness-manager-channel", "schema_version": 1, "channel": channel,
            "serial": serial, "issued_at": "2026-09-24T12:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z", "signing_key_id": "4E3C9A1F0B7D2E68",
            "board": {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]},
            "harness": {"current": current, "releases": rels}}


class _Downloader:
    """No network: a fetch leaves a sparse file of the asset's size in the cache."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache

    @staticmethod
    def has_token() -> bool:
        return False

    def fetch(self, asset: Any, *, base_url: str = "", progress: Any = None) -> Path:
        blob = self.cache / "blobs" / asset.sha256
        blob.parent.mkdir(parents=True, exist_ok=True)
        with open(blob, "wb") as fh:
            fh.truncate(asset.size)
        if progress is not None:
            progress(f"download:{asset.name}", asset.size, asset.size)
        return blob


class SimUpdate:
    """What ``HarnessCatalog`` and ``harness_api`` use of ``UpdateService``, simulated."""

    reason = None

    def __init__(self, sim: Any, engine: Any) -> None:
        self.sim = sim                      # l3_week_plan.WeekPlanSim (its hubs and leases)
        self.engine = engine
        self._tmp = tempfile.TemporaryDirectory(prefix="hcat-mock-")
        self.state = UpdateState.under(Path(self._tmp.name))
        self.downloader = _Downloader(self.state.cache)
        self.app_version = "0.1.0"
        self.store = None
        self.bus = engine.bus
        self.leases = "simulated"           # harness_api keeps its hands off
        self.serial = 7
        self.outcome = "installed"
        self.running: dict[str, str] = {}   # board -> the release it runs after an install
        self.withdrawn: set[str] = set()    # UPDATE-UI: releases the publisher withdrew

    # -- channel --

    def fetch_channel(self, channel: str | None = None, source: str | None = None, *,
                      catalog: str | None = None) -> VerifiedChannel:
        name = channel or "stable"
        if name not in ("stable", "beta"):
            raise AbsentError(f"no {name!r} channel in the mock catalogue")
        return VerifiedChannel(channel=parse_channel(_doc(name, self.serial,
                                                          frozenset(self.withdrawn))),
                               url=f"https://example.invalid/channel/{name}/channel.json",
                               sha256="0" * 64, key_id="4E3C9A1F0B7D2E68",
                               key_role="harness-release", trusted_comment="mock",
                               catalog=catalog or harness_catalog("mps3"))

    def pins(self) -> Pins:
        return Pins(self.state)

    # -- the board --

    def identity(self, session: Any) -> BoardIdentity:
        bid = session.candidate.board_id
        version = self.running.get(bid)
        if version is None:
            return session.identity()
        i = RELEASES[version]["identity"]
        return BoardIdentity(board_type="mps3", shell_id=i["static_id"], harness_version="1.0.0",
                             firmware_sha=i["fw_sha"], features=tuple(i["features"]),
                             harness_impl=i["impl"], proto=i["proto"], ver32=i["ver32"])

    def board_view(self, session: Any) -> BoardView:
        return BoardView(board_id=session.candidate.board_id, pack=session.candidate.pack,
                         identity=self.identity(session), identity_known=True,
                         has_storage=session.storage is not None,
                         has_controller=session.controller is not None,
                         sd_revisions=("HBI0309C",))

    def _hub(self, session: Any) -> tuple[Any, Any]:
        hub = self.sim.hubs.get(session.candidate.board_id)
        if hub is None:
            return SimpleNamespace(hub=None), None
        return (SimpleNamespace(hub=SimpleNamespace(target=hub["target"], host=hub["host"])),
                SimpleNamespace(view=lambda _h: {"lease": hub.get("lease")}))

    def lease_state(self, session: Any) -> dict[str, Any]:
        return lease_state(*self._hub(session))

    def check_lease(self, session: Any, what: str = "install a harness") -> dict[str, Any]:
        s, leases = self._hub(session)
        return require_lease(s, leases, what)

    # -- plans and installs --

    def plan_harness(self, session: Any, *, verified: VerifiedChannel | None = None,
                     channel: str | None = None, source: str | None = None,
                     version: str | None = None, overlays_only: bool = False,
                     catalog: str | None = None, pinned: str | None = None) -> Any:
        verified = verified or self.fetch_channel(channel, source, catalog=catalog)
        if pinned is None:
            pin = self.pins().get(session.candidate.board_id)
            pinned = str(pin["version"]) if pin else ""
        plan = make_plan(verified.channel, self.board_view(session), app_version=self.app_version,
                         version=version, overlays_only=overlays_only, pinned=pinned)
        return plan, verified

    def install_harness(self, session: Any, plan: Any, approval: Any,
                        verified: VerifiedChannel) -> UpdateOutcome:
        bid = session.candidate.board_id
        if approval is None or approval.fingerprint != plan.fingerprint():
            raise RefusedError("this update was not approved (or the plan changed since)")
        if plan.base or plan.os_slot:
            self.check_lease(session, f"install harness {plan.version}")
        self.bus.publish(Event("update.started", bid, {"version": plan.version,
                                                       "mode": plan.mode, "rekey": plan.rekey}))
        for phase in ("download", "sd:write", "reboot:down", "reboot:up"):
            self.bus.publish(Event("update.progress", bid, {"phase": phase, "bytes": 1,
                                                            "total": 1}))
        result = self.outcome if plan.base or plan.os_slot else "stored"
        if result == "installed":
            self.running[bid] = plan.version
        detail = (f"harness {plan.version} is running (mock)" if result == "installed" else
                  f"written, not running (mock): the board does not report {plan.version}")
        backup = {"path": f"/tmp/hcat-mock/backups/{plan.version}.zip", "sha256": "0" * 64} \
            if plan.base else None
        InstallRecords(self.state).put(bid, {"version": plan.version, "result": result,
                                             "backup": backup, "detail": detail,
                                             **_record_fields(plan)})
        self.bus.publish(Event("update.done", bid, {"version": plan.version, "result": result,
                                                    "detail": detail}))
        return UpdateOutcome(bid, plan.version, result, detail, backup=backup,
                             restore_hint="`harness-manager update rollback TARGET` restores "
                                          "the backup" if result != "installed" else "")

    def rollback_harness(self, session: Any, *, backup_path: Path | None = None,
                         wait_s: float | None = None) -> UpdateOutcome:
        bid = session.candidate.board_id
        self.check_lease(session, "roll the harness back")
        last = InstallRecords(self.state).get(bid) or {}
        back = str(last.get("from_version") or "")
        if back in RELEASES:
            self.running[bid] = back
        else:
            self.running.pop(bid, None)
        InstallRecords(self.state).put(bid, {"version": back, "result": "restored",
                                             "kind": "restore",
                                             "from_version": last.get("version", "")})
        return UpdateOutcome(bid, "rollback", "restored", f"restored {backup_path} (mock)",
                             backup={"path": str(backup_path), "sha256": "0" * 64})


def register(app: FastAPI, state: Any, sim: Any, accepted: Any) -> SimUpdate:
    """Add the harness versions routes to the mock app. Returns the simulated update service."""
    upd = SimUpdate(sim, state.engine)

    @contextmanager
    def op(bid: str):
        state.jobs.gate(bid)                  # 409 HELD while a job runs on the board
        yield

    def busy(bid: str) -> Any:
        running = state.jobs.running(bid)
        return running[0] if running else None

    daemon = SimpleNamespace(
        engine=SimpleNamespace(update=upd), bus=state.jobs.bus,
        gates=SimpleNamespace(op=op, busy=busy),
        jobs=SimpleNamespace(submit=lambda kind, board_id, fn: state.jobs.start(board_id, kind,
                                                                                fn)))
    router = APIRouter(prefix=API)
    harness_api.register(RouteContext(daemon=daemon, api=router, wsr=None, board=state.session,
                                      require=_require, accepted=accepted))
    app.include_router(router)
    return upd


def _require(session: Any, attr: str, capability: str) -> Any:
    from harness_manager.core.errors import UnavailableError

    adapter = getattr(session, attr, None)
    if adapter is None:
        raise UnavailableError(capability, "needs the MPS3 Debug USB")
    return adapter
