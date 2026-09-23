"""The week-plan API additions (docs/API.md "Week-plan additions", frozen), simulated for
the T14 mock daemon (lane L3 UI).

Lanes L1 (hub: tunnel and lease), L2 (consoles: PTY and baud) and L4 (power and update)
build the real routes in parallel. Until they land, the UI is built and tested against
this: the same routes, bodies, jobs and events, over ``DemoEngine`` boards. Nothing here
touches a real PTY, hub, plug or update channel.

``WeekPlanSim`` holds the simulated state; its knobs (``behind_hub``, ``set_power``,
``attach_screen``, ``pty_unavailable``, ``update_outcome`` ...) set up a scenario for a
test or a screenshot. ``register(app, state, sim)`` adds the routes to the mock app.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import re
import threading
import time
from dataclasses import replace
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from harness_manager.cli.output import reading_json
from harness_manager.core.errors import (
    AbsentError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.core.model import Reading

API = "/api/v1"

#: Routes this module serves, by the daemon extension module that owns them (docs/API.md).
EXTENSION_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "consoles_api": (
        ("POST", "/boards/{bid}/consoles/{name}/pty"),
        ("GET", "/boards/{bid}/consoles/{name}/pty"),
        ("DELETE", "/boards/{bid}/consoles/{name}/pty"),
        ("GET", "/boards/{bid}/consoles/{name}/baud"),
        ("POST", "/boards/{bid}/consoles/{name}/baud"),
    ),
    "hub_api": (
        ("GET", "/boards/{bid}/tunnel"),
        ("GET", "/boards/{bid}/lease"),
        ("POST", "/boards/{bid}/lease"),
        ("DELETE", "/boards/{bid}/lease"),
    ),
    "power_api": (
        ("GET", "/boards/{bid}/power"),
        ("POST", "/boards/{bid}/power/cycle"),
    ),
    "update_api": (
        ("POST", "/update/check"),
        ("POST", "/boards/{bid}/update/harness"),
        ("POST", "/boards/{bid}/update/rollback"),
        ("POST", "/update/app"),
        ("POST", "/update/app/rollback"),
    ),
}

SERIAL_CONSOLES = ("mcc", "shell")          # DemoEngine's Debug-USB consoles
SERIAL_CHOICES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
UART_BAUD_CHOICES = [9600, 19200, 38400, 57600, 76800, 115200, 230400]
DESIGN_BAUD = 76800                          # rp_nanosoc_wrapper.sv UART_BAUD
DUT_CLOCK_PRESETS = (25.0, 50.0, 100.0)
RELEASE_STATIC_ID = "0x72bb0a36"            # the ILA mint's shell (FIELDED_ILA_V011)
RELEASE_VERSION = "1.1.0"


def _slug(board_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", board_id)


class WeekPlanSim:
    """Simulated state for the frozen week-plan routes, over the mock's engine."""

    def __init__(self, state: Any) -> None:
        self.state = state                   # t14_mock_api.MockDaemonApp
        self.engine = state.engine
        self._lock = threading.Lock()
        self.ptys: dict[tuple[str, str], dict[str, Any]] = {}
        self.bauds: dict[tuple[str, str], int] = {}
        self.pty_unavailable = ""            # non-empty: POST .../pty is 422 (as on Windows)
        self._pts = 3
        self.hubs: dict[str, dict[str, Any]] = {}
        self._lease_freed: dict[str, threading.Event] = {}
        self.power: dict[str, dict[str, Any]] = {}
        self.clocks: dict[str, float] = {}
        self.update_outcome = "installed"    # or "written-not-running"
        self.plans: dict[str, dict[str, Any]] = {}
        self.previous: dict[str, dict[str, Any]] = {}
        self.app_version = "0.0.1"
        self.app_current = "0.1.0"

    # -- helpers -------------------------------------------------------------------------

    def publish(self, topic: str, board_id: str, data: dict[str, Any]) -> None:
        self.engine.bus.publish(Event(topic, board_id, data))

    def identity(self, bid: str) -> Any:
        return self.state.session(bid).identity()

    def info(self, bid: str) -> Any:
        return self.engine.info(bid)

    def console_names(self, bid: str) -> list[str]:
        return list(self.engine.consoles.names(self.state.session(bid)))

    def require_console(self, bid: str, name: str) -> None:
        if name not in self.console_names(bid):
            raise AbsentError(f"{bid} has no console named {name!r}",
                              hint="GET .../consoles lists them")

    # -- knobs (tests and screenshots) --------------------------------------------------

    def behind_hub(self, bid: str, *, host: str = "mapstone-dev", target: str = "mps3_01_pl",
                   tunnel: str = "up", lease: str = "mine", expires_in_s: float = 1800,
                   holder: str = "", detail: str = "") -> None:
        """Put a board behind a hub. ``lease``: mine | other | none."""
        me = f"{getpass.getuser()}@harness-manager"
        record = None
        if lease == "mine":
            record = {"target": target, "holder": me, "expires_at": time.time() + expires_in_s,
                      "mine": True}
        elif lease == "other":
            record = {"target": target, "holder": holder or "alice@lab-pc-07",
                      "expires_at": time.time() + expires_in_s, "mine": False}
        with self._lock:
            self.hubs[bid] = {"host": host, "target": target, "tunnel": tunnel,
                              "detail": detail, "lease": record}

    def release_other(self, bid: str) -> None:
        """The other holder releases: a queued lease job then gets it."""
        with self._lock:
            hub = self.hubs.get(bid)
            if hub and hub["lease"] and not hub["lease"]["mine"]:
                hub["lease"] = None
            ev = self._lease_freed.get(bid)
        if ev is not None:
            ev.set()

    def set_power(self, bid: str, *, device: str = "Shelly Plus Plug S (192.168.10.50)",
                  cycle_reason: str = "", watts: float | None = 23.4,
                  volts: float | None = 12.1) -> None:
        with self._lock:
            self.power[bid] = {"device": device, "cycle_reason": cycle_reason, "watts": watts,
                               "volts": volts}

    def attach_screen(self, bid: str, name: str, clients: int = 1) -> None:
        """``screen <path>`` attached to a console's PTY (``console.pty`` fires)."""
        with self._lock:
            pty = self.ptys.get((bid, name))
            if pty is None:
                raise AbsentError(f"{name} has no PTY yet")
            pty["clients"] = clients
        self.publish("console.pty", bid, {"name": name, "path": pty["path"], "clients": clients})

    # -- consoles: PTY and baud ---------------------------------------------------------

    def console_kind(self, bid: str, name: str) -> str:
        return "serial" if name in SERIAL_CONSOLES else "ethernet"

    def baud_view(self, bid: str, name: str) -> dict[str, Any]:
        kind = self.console_kind(bid, name)
        key = (bid, name)
        if kind == "serial":
            baud = self.bauds.get(key, 115200)
            if bid in self.hubs:
                return {"baud": baud, "settable": False, "choices": [], "source": "serial",
                        "reason": "set by the hub share (fpgahub owns the serial port)"}
            return {"baud": baud, "settable": True, "choices": list(SERIAL_CHOICES),
                    "source": "serial"}
        if name == "swo":
            return {"baud": None, "settable": False, "choices": [], "source": "unknown",
                    "reason": "SWO is trace output, not a UART: it has no baud rate"}
        ident = self.identity(bid)
        design = ident.rm_name or ident.rm_id or "loaded"
        if "uart_baud" in (ident.features or ()):
            return {"baud": self.bauds.get(key, DESIGN_BAUD), "settable": True,
                    "choices": list(UART_BAUD_CHOICES), "source": "harness"}
        return {"baud": self.bauds.get(key, DESIGN_BAUD), "settable": False, "choices": [],
                "source": "design",
                "reason": f"fixed by the {design} design (needs harness 'uart_baud')"}

    def consoles_view(self, bid: str) -> list[dict[str, Any]]:
        out = []
        for name in self.console_names(bid):
            view = self.baud_view(bid, name)
            pty = self.ptys.get((bid, name))
            out.append({"name": name, "kind": self.console_kind(bid, name),
                        "baud": view["baud"], "settable": view["settable"],
                        "pty": pty["path"] if pty else None})
        return out

    def open_pty(self, bid: str, name: str) -> dict[str, Any]:
        self.require_console(bid, name)
        if self.pty_unavailable:
            raise UnavailableError(f"console {name} pty", self.pty_unavailable)
        created = False
        with self._lock:
            pty = self.ptys.get((bid, name))
            if pty is None:
                self._pts += 1
                path = f"/tmp/harness-manager-{getpass.getuser()}/{_slug(bid)}/{name}"
                pty = {"path": path, "device": f"/dev/pts/{self._pts}",
                       "command": f"screen {path}", "clients": 0}
                self.ptys[(bid, name)] = pty
                created = True
        if created:
            self.publish("console.pty", bid, {"name": name, "path": pty["path"], "clients": 0})
        return {k: pty[k] for k in ("path", "device", "command")}

    # -- update ----------------------------------------------------------------------------

    def plan_for(self, bid: str) -> dict[str, Any]:
        ident = self.identity(bid)
        info = self.info(bid)
        links = {lk.kind.value for lk in info.candidate.links}
        running = {"shell_id": ident.shell_id, "harness": ident.harness_version,
                   "impl": ident.harness_impl or ""}
        up_to_date = ident.harness_version == RELEASE_VERSION
        rekey = bool(ident.shell_id) and ident.shell_id.lower() != RELEASE_STATIC_ID
        blockers, warnings = [], []
        if "usb_msd" not in links:
            blockers.append("installing the harness base writes the config SD: it needs the "
                            "Debug USB cable (the V2M-MPS3 volume)")
        if "usb_serial" not in links:
            blockers.append("the new base runs only after a board REBOOT: it needs the Debug "
                            "USB cable (the MCC console)")
        unusable = []
        if rekey:
            unusable = [f"overlays keyed to {ident.shell_id}: greybox, nanosoc, nanosoc_upy, "
                        "nanosoc_iice, led"]
            warnings.append(f"RE-KEY: shell {ident.shell_id} -> {RELEASE_STATIC_ID}. Everything "
                            f"keyed to {ident.shell_id} stops loading; the release brings "
                            "overlays keyed to the new shell")
        steps = [] if up_to_date else [
            {"action": "download", "detail": "3 component(s), sha256-checked: base-sd, "
                                             "overlays, firmware", "component": ""},
            {"action": "verify", "detail": "domain checks: part, static_id, usercode, CRC, "
                                           "SD rules", "component": ""},
            {"action": "store-overlays", "detail": f"overlays keyed to {RELEASE_STATIC_ID} "
                                                   "into the local store (no SD write)",
             "component": "overlays"},
            {"action": "backup-sd", "detail": "back up the whole config SD (mandatory gate)",
             "component": "base-sd"},
            {"action": "install-sd", "detail": "write the new base to the config SD, "
                                               "journaled, read back; never an .ebf",
             "component": "base-sd"},
            {"action": "reboot", "detail": "reboot the board and witness it go down and come "
                                           "back (the pack's budget for this harness)",
             "component": ""},
            {"action": "confirm-identity", "detail": f"the board must report shell "
                                                     f"{RELEASE_STATIC_ID}, harness "
                                                     f"{RELEASE_VERSION}", "component": ""},
        ]
        plan = {
            "board_id": bid, "channel": "stable", "serial": 14, "version": RELEASE_VERSION,
            "running": running, "running_release": ident.harness_version or "",
            "mode": "none" if up_to_date else "full", "up_to_date": up_to_date,
            "rekey": rekey and not up_to_date,
            "consent_phrase": f"REKEY {RELEASE_STATIC_ID}" if rekey and not up_to_date else "",
            "unusable": unusable if not up_to_date else [], "steps": steps,
            "warnings": warnings if not up_to_date else [],
            "blockers": blockers if not up_to_date else [],
            "components": [] if up_to_date else ["base-sd", "overlays", "firmware"],
            "skipped": {}, "base": not up_to_date, "os_slot": False,
        }
        plan["fingerprint"] = hashlib.sha256(json.dumps(
            {k: plan[k] for k in ("board_id", "serial", "version", "mode", "running", "rekey")},
            sort_keys=True).encode()).hexdigest()
        return plan

    def check(self, bid: str | None) -> dict[str, Any]:
        report: dict[str, Any] = {
            "channel": "stable", "serial": 14, "issued_at": time.time() - 3 * 86400,
            "expires_at": time.time() + 27 * 86400, "signed_by": "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3",
            "key_role": "release", "source": "github:SoC-Labs/HarnessManager-releases",
            "warnings": [], "harness_current": RELEASE_VERSION, "app_current": self.app_current,
            "app_running": self.app_version,
            "app_update": self.app_current if self.app_current != self.app_version else "",
        }
        if bid:
            plan = self.plan_for(bid)
            self.plans[bid] = plan
            report["plan"] = plan
        report["available"] = bool(report["app_update"]) or bool(
            report.get("plan") and not report["plan"]["up_to_date"]
            and not report["plan"]["blockers"])
        if report["available"]:
            self.publish("update.available", bid or "", {
                "channel": "stable", "serial": 14, "harness": RELEASE_VERSION,
                "app": report["app_update"]})
        return report


def register(app: FastAPI, state: Any, sim: WeekPlanSim, ok: Any, accepted: Any) -> None:
    """Add the week-plan routes to the mock app (``ok``/``accepted`` are the mock's helpers)."""
    jobs = state.jobs

    # -- consoles_api --------------------------------------------------------------------

    @app.post(f"{API}/boards/{{bid}}/consoles/{{name}}/pty")
    def pty_open(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        return ok(**sim.open_pty(bid, name))

    @app.get(f"{API}/boards/{{bid}}/consoles/{{name}}/pty")
    def pty_get(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        sim.require_console(bid, name)
        pty = sim.ptys.get((bid, name))
        return ok(pty=dict(pty) if pty else None)

    @app.delete(f"{API}/boards/{{bid}}/consoles/{{name}}/pty")
    def pty_close(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        with sim._lock:
            pty = sim.ptys.pop((bid, name), None)
        if pty is not None:
            sim.publish("console.pty", bid, {"name": name, "path": pty["path"], "clients": 0,
                                             "closed": True})
        return ok()

    @app.get(f"{API}/boards/{{bid}}/consoles/{{name}}/baud")
    def baud_get(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        sim.require_console(bid, name)
        view = sim.baud_view(bid, name)
        return ok(**{k: v for k, v in view.items() if v is not None or k == "baud"})

    @app.post(f"{API}/boards/{{bid}}/consoles/{{name}}/baud")
    def baud_set(bid: str, name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        jobs.gate(bid)
        state.session(bid)
        sim.require_console(bid, name)
        view = sim.baud_view(bid, name)
        if not view["settable"]:
            raise UnavailableError(f"console {name} baud", view.get("reason") or "not settable")
        try:
            baud = int(body.get("baud"))
        except (TypeError, ValueError):
            raise UsageError("baud must be an integer", hint="e.g. {\"baud\": 115200}") from None
        if baud not in view["choices"]:
            raise UsageError(f"{baud} is not a rate this console offers",
                             hint="choices: " + ", ".join(str(c) for c in view["choices"]))
        sim.bauds[(bid, name)] = baud
        sim.publish("console.state", bid, {"name": name, "state": "up", "baud": baud})
        return ok(baud=baud, source=view["source"])

    # -- hub_api ---------------------------------------------------------------------------

    @app.get(f"{API}/boards/{{bid}}/tunnel")
    def tunnel(bid: str) -> dict[str, Any]:
        state.session(bid)
        hub = sim.hubs.get(bid)
        if hub is None:
            return ok(tunnel=None)
        return ok(tunnel={"via": "ssh", "host": hub["host"], "state": hub["tunnel"],
                          "ports": {"6900": 46900, "6910": 46910, "6921": 46921, "6930": 46930},
                          "detail": hub["detail"] or (
                              f"ssh -L to {hub['host']}" if hub["tunnel"] == "up"
                              else "the ssh process exited: check `ssh " + hub["host"] + "`")})

    @app.get(f"{API}/boards/{{bid}}/lease")
    def lease_get(bid: str) -> dict[str, Any]:
        state.session(bid)
        hub = sim.hubs.get(bid)
        if hub is None:
            return ok(lease=None, hub=None)
        return ok(lease=dict(hub["lease"]) if hub["lease"] else None, hub=hub["host"])

    @app.post(f"{API}/boards/{{bid}}/lease", status_code=202)
    def lease_take(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.session(bid)
        hub = sim.hubs.get(bid)
        if hub is None:
            raise UnavailableError("lease", "this board is not behind a hub")
        ttl = float(body.get("ttl_s") or 1800)
        me = f"{getpass.getuser()}@harness-manager"

        def work(progress: Any) -> dict[str, Any]:
            if hub["lease"] and not hub["lease"]["mine"]:
                sim.publish("lease.state", bid, {"target": hub["target"], "state": "queued",
                                                 "holder": hub["lease"]["holder"],
                                                 "expires_at": hub["lease"]["expires_at"]})
                freed = sim._lease_freed.setdefault(bid, threading.Event())
                if not freed.wait(20):
                    raise HeldError(f"{hub['target']} is leased to {hub['lease']['holder']}",
                                    holder=hub["lease"]["holder"],
                                    hint="the queue timed out; try again later")
            hub["lease"] = {"target": hub["target"], "holder": me,
                            "expires_at": time.time() + ttl, "mine": True}
            sim.publish("lease.state", bid, {"target": hub["target"], "state": "held",
                                             "holder": me, "expires_at": hub["lease"]["expires_at"]})
            return {"lease": dict(hub["lease"])}

        return accepted(jobs.start(bid, "lease", work))

    @app.delete(f"{API}/boards/{{bid}}/lease")
    def lease_release(bid: str) -> dict[str, Any]:
        state.session(bid)
        hub = sim.hubs.get(bid)
        if hub is None or not hub["lease"] or not hub["lease"]["mine"]:
            raise AbsentError("this client holds no lease on the board",
                              hint="GET .../lease shows who holds it")
        target = hub["lease"]["target"]
        hub["lease"] = None
        sim.publish("lease.state", bid, {"target": target, "state": "released", "holder": "",
                                         "expires_at": None})
        return ok()

    # -- power_api -------------------------------------------------------------------------

    @app.get(f"{API}/boards/{{bid}}/power")
    def power(bid: str) -> dict[str, Any]:
        jobs.gate(bid)
        info = sim.info(bid)
        p = sim.power.get(bid)
        if p is None:
            reason = info.unavailable.get("power_cycle") or "needs a networked power plug"
            return ok(readings=[reading_json(Reading.unavailable(
                "board_power", "W", "no power device in boards.toml for this board"))],
                cycle_reason=reason, device=None)
        now = time.time()
        src = p["device"]
        readings = [
            Reading("board_power", p["watts"], "W", source=src, observed_at=now)
            if p["watts"] is not None else
            Reading.unavailable("board_power", "W", "the device reported no power", source=src),
            Reading("supply_voltage", p["volts"], "V", source=src, observed_at=now)
            if p["volts"] is not None else
            Reading.unavailable("supply_voltage", "V", "the device reports no voltage", source=src),
        ]
        return ok(readings=[reading_json(r, now) for r in readings],
                  cycle_reason=p["cycle_reason"], device=src)

    @app.post(f"{API}/boards/{{bid}}/power/cycle", status_code=202)
    def power_cycle(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        jobs.gate(bid)
        p = sim.power.get(bid)
        reason = (p or {}).get("cycle_reason", "") if p else (
            sim.info(bid).unavailable.get("power_cycle") or "needs a networked power plug")
        if reason:
            raise UnavailableError("power_cycle", reason)
        off_s = float(body.get("off_s") or 5.0)

        def work(progress: Any) -> dict[str, Any]:
            t0 = time.monotonic()
            for i, phase in enumerate(("off", "on", "up"), start=1):
                time.sleep(0.25)
                progress(phase, i, 3)
                sim.publish("power.cycle", bid, {"phase": phase, "off_s": off_s,
                                                 "device": p["device"]})
            return {"device": p["device"], "off_s": off_s,
                    "summary": f"power-cycled through {p['device']}: off {off_s:.0f} s, "
                               f"the shell answered {time.monotonic() - t0:.1f} s after power on",
                    "up_after_s": round(time.monotonic() - t0, 1)}

        return accepted(jobs.start(bid, "power_cycle", work))

    # -- update_api ------------------------------------------------------------------------

    @app.post(f"{API}/update/check", status_code=202)
    def update_check(body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        bid = body.get("board_id") or ""
        if bid:
            state.session(bid)

        def work(progress: Any) -> dict[str, Any]:
            progress("fetch", 1, 2)
            time.sleep(0.2)
            progress("verify", 2, 2)
            return sim.check(bid or None)

        return accepted(jobs.start(bid, "update_check", work))

    @app.post(f"{API}/boards/{{bid}}/update/harness", status_code=202)
    def update_harness(bid: str, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        jobs.gate(bid)
        state.session(bid)
        plan = sim.plan_for(bid)
        if body.get("fingerprint") != plan["fingerprint"]:
            raise RefusedError("the plan changed since it was approved (or was never checked)",
                               hint="check again, then approve the new plan")
        if plan["blockers"]:
            raise RefusedError(f"this update cannot run: {'; '.join(plan['blockers'])}",
                               hint="fix the blockers first")
        if plan["up_to_date"]:
            raise RefusedError(f"harness {RELEASE_VERSION} is already running",
                               hint="nothing to install")
        if plan["rekey"] and str(body.get("rekey_phrase") or "").strip() != plan["consent_phrase"]:
            raise RefusedError(f"this update RE-KEYS the board (shell {plan['running']['shell_id']}"
                               f" -> {RELEASE_STATIC_ID})",
                               hint=f"to consent, type exactly: {plan['consent_phrase']}")
        ident = sim.identity(bid)

        def work(progress: Any) -> dict[str, Any]:
            sim.publish("update.started", bid, {"version": RELEASE_VERSION, "mode": plan["mode"],
                                                "rekey": plan["rekey"]})
            total = 12 * 1024 * 1024
            for phase, done in (("download", total // 3), ("download", total), ("verify", total),
                                ("backup-sd", total), ("install-sd", total), ("reboot", total),
                                ("confirm-identity", total)):
                time.sleep(0.2)
                progress(phase, done, total)
                sim.publish("update.progress", bid, {"phase": phase, "bytes": done,
                                                     "total": total})
            backup = f"/home/{getpass.getuser()}/.config/harness-manager/backups/sd-{int(time.time())}.zip"
            sim.previous[bid] = {"shell_id": ident.shell_id, "harness_version": ident.harness_version}
            hint = f"`harness-manager update rollback TARGET` restores the backup {backup}"
            if sim.update_outcome == "installed":
                sim.engine._set_identity(bid, shell_id=RELEASE_STATIC_ID,
                                         harness_version=RELEASE_VERSION)
                result, detail = "installed", (f"the board reports shell {RELEASE_STATIC_ID}, "
                                               f"harness {RELEASE_VERSION}")
            else:
                result, detail = "written-not-running", (
                    "the SD holds the new base, but after the reboot the board still reports "
                    f"shell {ident.shell_id}: it is not running it")
            sim.publish("update.done", bid, {"version": RELEASE_VERSION, "result": result,
                                             "detail": detail})
            return {"board_id": bid, "version": RELEASE_VERSION, "result": result,
                    "detail": detail, "ok": result == "installed", "checks": [],
                    "identity_after": {"shell_id": RELEASE_STATIC_ID if result == "installed"
                                       else ident.shell_id,
                                       "harness_version": RELEASE_VERSION if result == "installed"
                                       else ident.harness_version},
                    "evidence": {"summary": "REBOOT witnessed: down after 1.0 s, up after 6.2 s"},
                    "backup": {"path": backup}, "restore_hint": hint,
                    "stored": ["overlays keyed to " + RELEASE_STATIC_ID]}

        return accepted(jobs.start(bid, "update_harness", work))

    @app.post(f"{API}/boards/{{bid}}/update/rollback", status_code=202)
    def update_rollback(bid: str) -> JSONResponse:
        jobs.gate(bid)
        state.session(bid)
        prev = sim.previous.get(bid)

        def work(progress: Any) -> dict[str, Any]:
            sim.publish("update.started", bid, {"version": "rollback", "mode": "restore",
                                                "rekey": False})
            for i, phase in enumerate(("restore-sd", "reboot", "confirm-identity"), start=1):
                time.sleep(0.2)
                progress(phase, i, 3)
            if prev:
                sim.engine._set_identity(bid, **prev)
            detail = ("restored the backup; the board reports shell "
                      f"{sim.identity(bid).shell_id}")
            sim.publish("update.done", bid, {"version": "rollback", "result": "restored",
                                             "detail": detail})
            return {"board_id": bid, "version": "rollback", "result": "restored",
                    "detail": detail, "ok": True}

        return accepted(jobs.start(bid, "update_rollback", work))

    def app_job(kind: str, version: str) -> JSONResponse:
        for job in jobs.running():
            raise HeldError(f"{job.describe()} is running on {job.board_id}",
                            holder=f"harness-manager-daemon {job.describe()}",
                            hint="update the app when no board job runs")

        def work(progress: Any) -> dict[str, Any]:
            time.sleep(0.3)
            progress("switch", 1, 1)
            before = sim.app_version
            sim.app_version = version
            return {"version": version, "previous": before, "switched": True, "locked": True,
                    "restart": "restart harness-manager to run it"}

        return accepted(jobs.start("", kind, work))

    @app.post(f"{API}/update/app", status_code=202)
    def update_app(body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        return app_job("update_app", str(body.get("version") or sim.app_current))

    @app.post(f"{API}/update/app/rollback", status_code=202)
    def update_app_rollback() -> JSONResponse:
        return app_job("update_app_rollback", "0.0.1")


# -- the clocks adapter the demo boards lack (GET/POST /clocks are core routes) ------------


class SimClocks:
    """DUT clock presets over the shell (the MPS3 `clock` verb), for boards whose DemoEngine
    session has no clock adapter."""

    def __init__(self, sim: WeekPlanSim, bid: str) -> None:
        self.sim, self.bid = sim, bid

    def clocks(self) -> list[Reading]:
        # As harness_manager_mps3.clock: the shell cannot read the clock back, only set it.
        mhz = self.sim.clocks.get(self.bid)
        if mhz is None:
            return [Reading.unavailable("dut", "MHz", "the shell cannot read the DUT clock "
                                        "back; set it to know it", source="shell set_clk")]
        return [Reading("dut", mhz, "MHz", source="shell set_clk (last set value)")]

    def set_clock(self, name: str, mhz: float) -> Reading:
        if name != "dut":
            raise UsageError(f"no clock named {name!r}", hint="clocks: dut")
        if mhz not in DUT_CLOCK_PRESETS:
            raise UsageError(f"{mhz:g} MHz is not a preset",
                             hint="presets: " + ", ".join(f"{p:g}" for p in DUT_CLOCK_PRESETS))
        self.sim.clocks[self.bid] = mhz
        board = self.sim.engine._board(self.bid)
        board.readings = [replace(r, value=mhz) if r.name == "dut_clk" else r
                          for r in board.readings]
        return self.clocks()[0]

