"""The week-plan API additions (docs/API.md "Week-plan additions", frozen), simulated for
the T14 mock daemon (lane L3 UI).

Lanes L1 (hub: tunnel and lease), L2 (consoles: PTY and baud) and L4 (power and update)
build the real routes in parallel. Until they land, the UI is built and tested against
this: the same routes, bodies, jobs and events, over ``DemoEngine`` boards. Nothing here
touches a real PTY, hub, plug or update channel.

``WeekPlanSim`` holds the simulated state; its knobs (``behind_hub``, ``set_power``,
``attach_screen``, ``pty_unavailable``, ``update_reason``, ``bump_channel`` ...) set up a
scenario for a test or a screenshot. ``register(app, state, sim)`` adds the routes to the
mock app. The power and update routes follow docs/API.md "As built by L4"."""

from __future__ import annotations

import getpass
import hashlib
import json
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from harness_manager.cli.output import reading_json, with_data
from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.core.model import Reading
from harness_manager.power.base import DEFAULT_OFF_S, check_off_s

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
        ("POST", "/boards/{bid}/lease/request"),
        ("POST", "/boards/{bid}/lease/respond"),
        ("POST", "/boards/{bid}/lease/force"),
        ("DELETE", "/boards/{bid}/lease/queue"),
        ("DELETE", "/boards/{bid}/lease/taken"),
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
    # T10: served in the mock by tests/fakes/t10_mock_xdc.py over the real xdc service.
    "xdc_api": (
        ("GET", "/xdc"),
        ("POST", "/xdc/export"),
        ("GET", "/boards/{bid}/xdc"),
        ("POST", "/boards/{bid}/xdc/export"),
    ),
    # P1: served in the mock by tests/fakes/p1_mock_panel.py (a simulated panel per board).
    "panel_api": (
        ("GET", "/boards/{bid}/panel"),
        ("GET", "/boards/{bid}/panel/frame"),
        ("POST", "/boards/{bid}/identify"),
    ),
}

SERIAL_CONSOLES = ("mcc", "shell")          # DemoEngine's Debug-USB consoles
SERIAL_CHOICES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
#: harness_manager_mps3.uart as built by L2 (the rates on today's fielded shell), copied so
#: the mock does not need pyverify.
HARNESS_CHOICES = [9600, 19200, 38400, 57600, 76800, 115200, 230400, 460800, 921600]
DESIGN_BAUD = 76800                          # rp_nanosoc_wrapper.sv UART_BAUD
DESIGN_CITE = {
    "nanosoc": "fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:54 (UART_BAUD -> uart_axis_shim .BAUD, "
               "382-384)",
    "nanosoc_upy": "fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv:62 (passed to "
                   "rp_nanosoc_wrapper, 164)",
}
GREYBOX_WHY = "no design is loaded (greybox): nothing drives uart0"
UART1_WHY = ("nothing drives uart1: the shell ties its DUT side off until a design widens the "
             "partition contract (fpga/shell/ip/uart_bridge/README.md:72-75)")
SWO_BAUD = 2_000_000
SWO_SOURCE = "firmware/uart_over_eth/uart_over_eth.c:30-35 (UART_OVER_ETH_SWO_DIVISOR 24)"
HUB_SHARE = "a hub share: the share sets the rate (change it on the hub, not here)"
STANDARD_SPEEDS = {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}
DUT_CLOCK_PRESETS = (25.0, 50.0, 100.0)
RELEASE_STATIC_ID = "0x72bb0a36"            # the ILA mint's shell (FIELDED_ILA_V011)
RELEASE_VERSION = "1.1.0"
#: Every meter answers these three rows, in this order (daemon/power_api.py POWER_ROWS).
POWER_ROWS = (("board_power", "W"), ("supply_voltage", "V"), ("supply_current", "A"))
#: The update service's key id and channel source, as the real check reports them.
SIGNED_BY = "4E3C9A1F0B7D2E68"
CHANNEL_SOURCE = ("https://raw.githubusercontent.com/SoC-Labs/mps3-platform-dist/channel/"
                  "stable/channel.json")


def _iso(epoch: float) -> str:
    """fpgahub's lease expiry format: ISO 8601 with the UTC offset."""
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat()


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
        self._lease_cancel: dict[str, threading.Event] = {}
        self.power: dict[str, dict[str, Any]] = {}
        self.clocks: dict[str, float] = {}
        self.update_reason = ""              # non-empty: every update route is 422 (DemoEngine)
        self.update_outcome = "installed"    # or "written-not-running" (the job then fails)
        self.rollback_outcome = "restored"   # or "restored-not-confirmed" (fails the job)
        self.channel_serial = 14             # bump it between check and install: plan changed
        self.plans: dict[str, dict[str, Any]] = {}
        self.previous: dict[str, dict[str, Any]] = {}
        self.app_version = "0.0.1"
        self.app_current = "0.1.0"
        self.requests: Any = None            # t14_lease_requests.LeaseRequestSim (the mock sets it)

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
            record = {"target": target, "holder": me, "user": getpass.getuser(),
                      "expires_at": _iso(time.time() + expires_in_s), "mine": True}
        elif lease == "other":
            record = {"target": target, "holder": holder or "alice@lab-pc-07", "user": "alice",
                      "expires_at": _iso(time.time() + expires_in_s), "mine": False}
        with self._lock:
            self.hubs[bid] = {"host": host, "target": target, "tunnel": tunnel,
                              "detail": detail, "lease": record, "restarts": 0}

    def tunnel_view(self, bid: str) -> dict[str, Any] | None:
        """``GET .../tunnel`` as L1 built it (``Reach.status()`` plus the hub's shares)."""
        hub = self.hubs.get(bid)
        if hub is None:
            return None
        forwards = {"6900": 46900, "6910": 46910, "6921": 46921, "6930": 46930, "6931": 46931,
                    "6932": 46932}
        up = hub["tunnel"] == "up"
        return {"via": "ssh", "host": hub["host"], "state": hub["tunnel"],
                "ports": dict(forwards), "forwards": dict(forwards) if up else {},
                "restarts": hub["restarts"], "pid": 48213 if up else None,
                "shares": {"mcc": "tcp://127.0.0.1:47001", "shell": "tcp://127.0.0.1:47002"},
                "detail": hub["detail"] or (f"ssh -N -L ... {hub['host']}" if up else
                                            f"the ssh process exited: check `ssh {hub['host']}`")}

    def set_tunnel(self, bid: str, state: str, detail: str = "") -> None:
        """The tunnel moves (it dropped, it restarts, it is back): ``tunnel.state`` fires."""
        with self._lock:
            hub = self.hubs[bid]
            if state == "starting":
                hub["restarts"] += 1
            hub["tunnel"], hub["detail"] = state, detail
        self.publish("tunnel.state", bid, self.tunnel_view(bid) or {})

    def release_other(self, bid: str) -> None:
        """The other holder releases: a queued lease job then gets it."""
        with self._lock:
            hub = self.hubs.get(bid)
            if hub and hub["lease"] and not hub["lease"]["mine"]:
                hub["lease"] = None
            ev = self._lease_freed.get(bid)
        if ev is not None:
            ev.set()

    def set_power(self, bid: str, *, device: str = "shelly_gen2 http://192.168.10.50 outlet 0",
                  cycle_reason: str = "", watts: float | None = 11.4,
                  volts: float | None = 239.1, amps: float | None = 0.071,
                  note: str = "AC at the wall outlet: includes the board power supply's own "
                              "losses") -> None:
        """A meter on the board's supply (boards.toml ``[power]``). ``cycle_reason`` non-empty:
        a meter that cannot switch (an INA260), so the page offers no cycle."""
        with self._lock:
            self.power[bid] = {"device": device, "cycle_reason": cycle_reason, "watts": watts,
                               "volts": volts, "amps": amps, "note": note}

    def bump_channel(self) -> None:
        """A new release on the channel: every plan checked before this one is stale."""
        with self._lock:
            self.channel_serial += 1

    def require_update(self) -> None:
        if self.update_reason:
            raise UnavailableError("update", self.update_reason)

    def attach_screen(self, bid: str, name: str, clients: int | None = 1) -> None:
        """A terminal attached to (or left) a console's PTY: ``console.pty`` fires."""
        with self._lock:
            pty = self.ptys.get((bid, name))
            if pty is None:
                raise AbsentError(f"{name} has no PTY yet")
            pty["clients"] = clients
        self.publish("console.pty", bid, {"name": name, "path": pty["path"],
                                          "device": pty["device"], "clients": clients,
                                          "open": True})

    def close_pty(self, bid: str, name: str) -> bool:
        """The PTY goes away (DELETE .../pty; the daemon closing the board)."""
        with self._lock:
            pty = self.ptys.pop((bid, name), None)
        if pty is not None:
            self.publish("console.pty", bid, {"name": name, "path": pty["path"],
                                              "device": pty["device"], "clients": 0,
                                              "open": False})
        return pty is not None

    # -- consoles: PTY and baud (L2 as built: services/console.py, harness_manager_mps3.uart) --

    def console_kind(self, bid: str, name: str) -> str:
        return "serial" if name in SERIAL_CONSOLES else "ethernet"

    @staticmethod
    def _row(kind: str, baud: int | None, source: str, *, settable: bool = False,
             reason: str = "", choices: list[int] | None = None, **extra: Any) -> dict[str, Any]:
        out = {"kind": kind, "baud": baud, "source": source, "settable": settable,
               "reason": "" if settable else reason,
               "choices": list(choices) if choices else ([baud] if baud else [])}
        out.update(extra)
        return out

    def baud_view(self, bid: str, name: str) -> dict[str, Any]:
        kind = self.console_kind(bid, name)
        key = (bid, name)
        if kind == "serial":
            baud = self.bauds.get(key, 115200)
            if bid in self.hubs:
                return self._row("serial", baud, "serial", reason=HUB_SHARE, share=True)
            return self._row("serial", baud, "serial", settable=True, choices=SERIAL_CHOICES)
        if name == "swo":
            return self._row("ethernet", SWO_BAUD, "harness", cite=SWO_SOURCE,
                             reason=f"the harness firmware fixes the SWO deserialiser at "
                                    f"{SWO_BAUD} baud (divisor 24 at the 50 MHz dut_clk; "
                                    f"{SWO_SOURCE})")
        if name == "uart1":
            return self._row("ethernet", None, "design", reason=UART1_WHY)
        ident = self.identity(bid)
        design = ident.rm_name or "unknown"
        if design == "greybox":
            return self._row("ethernet", None, "design", reason=GREYBOX_WHY, design=design,
                             cite="fpga/dfx/rms/rm_greybox/rm_greybox.sv")
        cite = DESIGN_CITE.get(design, "")
        if "uart_baud" in (ident.features or ()):
            mode = "set" if key in self.bauds else "fixed"
            return self._row("ethernet", self.bauds.get(key, DESIGN_BAUD), "harness",
                             settable=True, choices=HARNESS_CHOICES, mode=mode, design=design)
        return self._row("ethernet", DESIGN_BAUD, "design", design=design, cite=cite,
                         reason=f"{design} fixes {name} at {DESIGN_BAUD} baud when it is built "
                                f"({cite}); changing it at run time needs harness firmware "
                                "with 'uart_baud'")

    def consoles_view(self, bid: str) -> list[dict[str, Any]]:
        out = []
        for name in self.console_names(bid):
            view = self.baud_view(bid, name)
            pty = self.ptys.get((bid, name))
            out.append({"name": name, "kind": view["kind"], "baud": view["baud"],
                        "settable": view["settable"], "source": view["source"],
                        "reason": view["reason"], "pty": pty["path"] if pty else None,
                        "state": "up" if pty else "closed"})
        return out

    def pty_view(self, bid: str, name: str) -> dict[str, Any] | None:
        pty = self.ptys.get((bid, name))
        if pty is None:
            return None
        view = self.baud_view(bid, name)
        rate = view["baud"] if view["kind"] == "serial" and view["baud"] in STANDARD_SPEEDS \
            else None
        command = f"screen {pty['path']}" + (f" {rate}" if rate else "")
        return {"name": name, "path": pty["path"], "device": pty["device"],
                "clients": pty["clients"], "command": command}

    def open_pty(self, bid: str, name: str) -> dict[str, Any]:
        self.require_console(bid, name)
        if self.pty_unavailable:
            err = UnavailableError("console_pty", self.pty_unavailable)
            err.hint = ("use the TCP export instead: `harness-manager console TARGET NAME "
                        "--export 0`, then a raw-TCP terminal (PuTTY 'Raw') on 127.0.0.1 and "
                        "the port it prints")
            raise err
        created = False
        with self._lock:
            pty = self.ptys.get((bid, name))
            if pty is None:
                self._pts += 1
                path = f"/tmp/harness-manager-{getpass.getuser()}/{_slug(bid)}/{name}"
                pty = {"path": path, "device": f"/dev/pts/{self._pts}", "clients": 0}
                self.ptys[(bid, name)] = pty
                created = True
        if created:
            self.publish("console.pty", bid, {"name": name, "path": pty["path"],
                                              "device": pty["device"], "clients": 0,
                                              "open": True})
        return self.pty_view(bid, name) or {}

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
            {"action": "download", "detail": "2 component(s), sha256-checked: base-sd, "
                                             "overlays", "component": ""},
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
            "board_id": bid, "channel": "stable", "serial": self.channel_serial,
            "version": RELEASE_VERSION,
            "running": running, "running_release": ident.harness_version or "",
            "mode": "none" if up_to_date else "full", "up_to_date": up_to_date,
            "rekey": rekey and not up_to_date,
            "consent_phrase": f"REKEY {RELEASE_STATIC_ID}" if rekey and not up_to_date else "",
            "unusable": unusable if not up_to_date else [], "steps": steps,
            "warnings": warnings if not up_to_date else [],
            "blockers": blockers if not up_to_date else [],
            "components": [] if up_to_date else ["base-sd", "overlays"],
            "skipped": {}, "base": not up_to_date, "os_slot": False,
        }
        plan["fingerprint"] = hashlib.sha256(json.dumps(
            {k: plan[k] for k in ("board_id", "serial", "version", "mode", "running", "rekey")},
            sort_keys=True).encode()).hexdigest()
        return plan

    def releases(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "harness": [
                {"version": RELEASE_VERSION, "status": "current", "static_id": RELEASE_STATIC_ID,
                 "harness": RELEASE_VERSION, "impl": "bare-metal", "rekey": True,
                 "released_at": "2026-09-23", "notes_url": "", "current": True},
                {"version": "1.0.0", "status": "superseded", "static_id": "0x3f1a560f",
                 "harness": "1.0.0", "impl": "bare-metal", "rekey": False,
                 "released_at": "2026-09-16", "notes_url": "", "current": False},
            ],
            "app": [{"version": self.app_current, "status": "current", "released_at": "2026-09-23",
                     "notes_url": "", "current": True}] if self.app_current else [],
        }

    def check(self, bid: str | None) -> dict[str, Any]:
        report: dict[str, Any] = {
            "channel": "stable", "serial": self.channel_serial,
            "issued_at": "2026-09-23T12:00:00Z", "expires_at": "2026-10-23T12:00:00Z",
            "signed_by": SIGNED_BY, "key_role": "harness-release", "source": CHANNEL_SOURCE,
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
        report["releases"] = self.releases()
        if report["available"]:
            self.publish("update.available", bid or "", {
                "channel": "stable", "serial": self.channel_serial, "harness": RELEASE_VERSION,
                "app": report["app_update"]})
        return report


def register(app: FastAPI, state: Any, sim: WeekPlanSim, ok: Any, accepted: Any) -> None:
    """Add the week-plan routes to the mock app (``ok``/``accepted`` are the mock's helpers)."""
    jobs = state.jobs

    # -- consoles_api --------------------------------------------------------------------

    @app.post(f"{API}/boards/{{bid}}/consoles/{{name}}/pty")
    def pty_open(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        return ok(board_id=bid, **sim.open_pty(bid, name))

    @app.get(f"{API}/boards/{{bid}}/consoles/{{name}}/pty")
    def pty_get(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        sim.require_console(bid, name)
        return ok(board_id=bid, pty=sim.pty_view(bid, name))

    @app.delete(f"{API}/boards/{{bid}}/consoles/{{name}}/pty")
    def pty_close(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        return ok(board_id=bid, name=name, closed=sim.close_pty(bid, name))

    @app.get(f"{API}/boards/{{bid}}/consoles/{{name}}/baud")
    def baud_get(bid: str, name: str) -> dict[str, Any]:
        state.session(bid)
        sim.require_console(bid, name)
        return ok(board_id=bid, name=name, **sim.baud_view(bid, name))

    @app.post(f"{API}/boards/{{bid}}/consoles/{{name}}/baud")
    def baud_set(bid: str, name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        state.session(bid)
        if "baud" not in body:
            raise UsageError("the request needs 'baud'",
                             hint='e.g. {"baud": 115200}; 0 goes back to the console\'s default '
                                  "rate")
        baud = body["baud"]
        if isinstance(baud, bool) or not isinstance(baud, int):
            raise UsageError(f"baud must be a whole number, not {baud!r}")
        jobs.gate(bid)
        sim.require_console(bid, name)
        view = sim.baud_view(bid, name)
        if not view["settable"]:
            raise UnavailableError("console_baud", view["reason"] or "the rate is unknown")
        if baud:
            sim.bauds[(bid, name)] = baud
        else:
            sim.bauds.pop((bid, name), None)
        view = sim.baud_view(bid, name)
        sim.publish("console.state", bid, {"name": name, "state": "up", "baud": view["baud"],
                                           "detail": f"{name} set to {view['baud']} baud"})
        return ok(board_id=bid, name=name, baud=view["baud"], source=view["source"],
                  mode=view.get("mode", ""))

    # -- hub_api ---------------------------------------------------------------------------

    @app.get(f"{API}/boards/{{bid}}/tunnel")
    def tunnel(bid: str) -> dict[str, Any]:
        state.session(bid)
        return ok(tunnel=sim.tunnel_view(bid))

    @app.get(f"{API}/boards/{{bid}}/lease")
    def lease_get(bid: str) -> dict[str, Any]:
        state.session(bid)
        hub = sim.hubs.get(bid)
        # docs/LEASE_REQUESTS.md adds queue, request, incoming and taken.
        more = sim.requests.view(bid) if sim.requests is not None else {}
        if hub is None:
            return ok(lease=None, hub=None, **more)
        lease = dict(hub["lease"]) if hub["lease"] else None
        if lease is not None and sim.requests is not None:
            lease.update(sim.requests.lease_keys(bid))          # D12: holder_kind
        return ok(lease=lease, hub=hub["host"], **more)

    @app.post(f"{API}/boards/{{bid}}/lease", status_code=202)
    def lease_take(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        ttl = body.get("ttl_s", 3600)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 86400:
            raise UsageError(f"ttl_s must be whole seconds from 60 to 86400, not {ttl!r}",
                             hint="the lease lapses after it unless heartbeated; the service "
                                  "heartbeats it while the board is open")
        state.session(bid)
        hub = sim.hubs.get(bid)
        if hub is None:
            raise UnavailableError("lease", f"{bid} is not behind a hub: there is no lease to take")
        me = f"{getpass.getuser()}@harness-manager"
        cancel = threading.Event()

        def work(progress: Any) -> dict[str, Any]:
            progress("acquire", 0, 1)
            if hub["lease"] and hub["lease"]["mine"]:
                return {"lease": dict(hub["lease"]), "already": True}
            if hub["lease"]:
                progress("queued", 1, 0)
                sim.publish("lease.state", bid, {"target": hub["target"], "state": "queued",
                                                 "holder": me, "expires_at": ""})
                freed = sim._lease_freed.setdefault(bid, threading.Event())
                sim._lease_cancel[bid] = cancel
                try:
                    deadline = time.monotonic() + 20
                    while not freed.wait(0.05):
                        if cancel.is_set():
                            raise ActionFailedError(
                                f"the lease request for {hub['target']} was cancelled; its "
                                "queue entry was removed", hint="acquire again when you want the board")
                        if time.monotonic() > deadline:
                            raise HeldError(f"{hub['target']} is leased to {hub['lease']['holder']}",
                                            holder=hub["lease"]["holder"],
                                            hint="the queue timed out; try again later")
                finally:
                    sim._lease_cancel.pop(bid, None)
            hub["lease"] = {"target": hub["target"], "holder": me, "user": getpass.getuser(),
                            "expires_at": _iso(time.time() + ttl), "mine": True}
            progress("held", 1, 1)
            sim.publish("lease.state", bid, {"target": hub["target"], "state": "held",
                                             "holder": me, "expires_at": hub["lease"]["expires_at"]})
            return {"lease": {k: hub["lease"][k] for k in ("target", "holder", "expires_at")}}

        return accepted(jobs.start(bid, "lease", work))

    @app.delete(f"{API}/boards/{{bid}}/lease")
    def lease_release(bid: str) -> dict[str, Any]:
        state.session(bid)
        hub = sim.hubs.get(bid)
        if hub is None:
            raise UnavailableError("lease", f"{bid} is not behind a hub: there is no lease to take")
        pending = sim._lease_cancel.get(bid)
        if pending is not None:
            pending.set()
            return ok(cancelled=True)
        if not hub["lease"] or not hub["lease"]["mine"]:
            who = f"held by {hub['lease']['holder']}" if hub["lease"] else "not leased"
            raise AbsentError(f"this Harness Manager holds no lease on {hub['target']} ({who})",
                              hint="a lease taken outside Harness Manager is released where it "
                                   "was taken (fpgahub lease release --token …)")
        released = {k: hub["lease"][k] for k in ("target", "holder", "expires_at")}
        hub["lease"] = None
        sim.publish("lease.state", bid, {"target": released["target"], "state": "released",
                                         "holder": released["holder"], "expires_at": ""})
        return ok(released={**released, "mine": True})

    # -- power_api (as built by L4: daemon/power_api.py) ----------------------------------

    @app.get(f"{API}/boards/{{bid}}/power")
    def power(bid: str) -> dict[str, Any]:
        state.session(bid)
        jobs.gate(bid)
        info = sim.info(bid)
        p = sim.power.get(bid)
        now = time.time()
        if p is None:
            why = info.unavailable.get("telemetry_power") or "this board has no power meter"
            readings = [Reading.unavailable(n, u, why, source="power-meter") for n, u in POWER_ROWS]
            return ok(board_id=bid, readings=[reading_json(r, now) for r in readings],
                      cycle_reason=info.unavailable.get("power_cycle") or "no power adapter",
                      device=None)
        src = p["device"]
        values = {"board_power": p["watts"], "supply_voltage": p["volts"],
                  "supply_current": p["amps"]}
        readings = [Reading(n, values[n], u, source=src, observed_at=now, reason=p["note"])
                    if values[n] is not None else
                    Reading.unavailable(n, u, "the device does not report it", source=src)
                    for n, u in POWER_ROWS]
        return ok(board_id=bid, readings=[reading_json(r, now) for r in readings],
                  cycle_reason=p["cycle_reason"], device=src)

    @app.post(f"{API}/boards/{{bid}}/power/cycle", status_code=202)
    def power_cycle(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.session(bid)
        off_s = check_off_s(body.get("off_s", DEFAULT_OFF_S))     # 400 before any job
        jobs.gate(bid)
        p = sim.power.get(bid)
        reason = p["cycle_reason"] if p else (
            sim.info(bid).unavailable.get("power_cycle") or "no power adapter")
        if reason:
            raise UnavailableError("power_cycle", reason)

        def work(progress: Any) -> dict[str, Any]:
            t0 = time.monotonic()
            for i, phase in enumerate(("off", "on"), start=1):    # never "up": no witness
                time.sleep(0.25)
                progress(phase, i, 2)
                sim.publish("power.cycle", bid, {"phase": phase, "off_s": off_s,
                                                 "device": p["device"]})
            return {"board_id": bid, "meter": p["device"], "off_s": off_s, "was_on": True,
                    "confirmed_off": True, "confirmed_on": True,
                    "seconds": round(off_s + time.monotonic() - t0, 1)}

        return accepted(jobs.start(bid, "power_cycle", work))

    # -- update_api (as built by L4: daemon/update_api.py) ----------------------------------

    @app.post(f"{API}/update/check", status_code=202)
    def update_check(body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        bid = body.get("board_id") or ""
        sim.require_update()
        if bid:
            state.session(bid)

        def work(progress: Any) -> dict[str, Any]:
            progress("check", 0, 0)
            time.sleep(0.2)
            progress("plan", 0, 0)
            return sim.check(bid or None)

        return accepted(jobs.start(bid, "update_check", work))

    @app.post(f"{API}/boards/{{bid}}/update/harness", status_code=202)
    def update_harness(bid: str, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        state.session(bid)
        fingerprint = body.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise UsageError("fingerprint must be a non-empty string")
        sim.require_update()
        jobs.gate(bid)
        plan = sim.plan_for(bid)
        if fingerprint != plan["fingerprint"]:
            raise with_data(RefusedError(
                f"the update plan for {bid} changed since it was checked; nothing was installed",
                hint="check again (POST /api/v1/update/check) and confirm the new plan"), plan=plan)
        if plan["blockers"]:
            raise with_data(RefusedError(f"cannot update {bid}: {'; '.join(plan['blockers'])}",
                                         hint="fix the blockers, then check again"), plan=plan)
        if plan["up_to_date"]:
            raise with_data(RefusedError(f"harness {RELEASE_VERSION} is already running",
                                         hint="nothing to install"), plan=plan)
        if plan["rekey"] and str(body.get("rekey_phrase") or "") != plan["consent_phrase"]:
            raise with_data(RefusedError(
                f"this update RE-KEYS the board (shell {plan['running']['shell_id']} -> "
                f"{RELEASE_STATIC_ID}); {len(plan['unusable'])} item(s) become unusable",
                hint=f"to consent, type exactly: {plan['consent_phrase']}"), plan=plan)
        ident = sim.identity(bid)

        def work(progress: Any) -> dict[str, Any]:
            sim.publish("update.started", bid, {"version": RELEASE_VERSION, "mode": plan["mode"],
                                                "rekey": plan["rekey"]})
            mib = 1024 * 1024
            for phase, done, total in (("download:base-sd", 4 * mib, 12 * mib),
                                       ("download:base-sd", 12 * mib, 12 * mib),
                                       ("download:overlays", 3 * mib, 3 * mib),
                                       ("store-overlays", 0, 0), ("backup:backup", 0, 0),
                                       ("sd:install", 12 * mib, 12 * mib), ("sd:verify", 0, 0),
                                       ("reboot:sent", 1, 3), ("reboot:down", 2, 3),
                                       ("reboot:up", 3, 3)):
                time.sleep(0.15)
                progress(phase, done, total)
                sim.publish("update.progress", bid, {"phase": phase, "bytes": done,
                                                     "total": total})
            backup = (f"/home/{getpass.getuser()}/.local/state/harness-manager/update/backups/"
                      f"{_slug(bid)}/V2M-MPS3-20260924T060000Z.zip")
            sim.previous[bid] = {"shell_id": ident.shell_id,
                                 "harness_version": ident.harness_version}
            installed = sim.update_outcome == "installed"
            if installed:
                sim.engine._set_identity(bid, shell_id=RELEASE_STATIC_ID,
                                         harness_version=RELEASE_VERSION)
                result = "installed"
                detail = (f"harness {RELEASE_VERSION} is running: the board reports shell "
                          f"{RELEASE_STATIC_ID}, harness {RELEASE_VERSION}")
            else:
                result = "written-not-running"
                detail = ("the SD holds the new base, but after the reboot the board still "
                          f"reports shell {ident.shell_id}, harness {ident.harness_version}")
            after = sim.identity(bid)
            outcome = {
                "board_id": bid, "version": RELEASE_VERSION, "result": result, "detail": detail,
                "ok": installed,
                "checks": [
                    {"name": "shell_id", "check": "ok" if installed else "failed",
                     "detail": f"board reports {after.shell_id}, release is {RELEASE_STATIC_ID}"},
                    {"name": "harness version", "check": "ok" if installed else "failed",
                     "detail": f"board reports {after.harness_version}, release is "
                               f"{RELEASE_VERSION}"},
                    {"name": "usercode", "check": "unchecked",
                     "detail": "board reports nothing (not on the wire yet), release is "
                               "0xd46fcdcb"},
                ],
                "identity_after": {"shell_id": after.shell_id, "harness": after.harness_version,
                                   "impl": after.harness_impl or ""},
                "evidence": {"summary": "REBOOT witnessed: down after 1.0s (the shell stopped "
                                        "answering ping), up after 14.3s (the shell answers "
                                        f"ping again (shell_id {after.shell_id}))"},
                "backup": {"path": backup, "sha256": hashlib.sha256(backup.encode()).hexdigest()},
                "restore_hint": "", "stored": [f"overlays keyed to {RELEASE_STATIC_ID}"],
                "skipped": {}, "os_slot": None,
            }
            sim.publish("update.done", bid, {"version": RELEASE_VERSION, "result": result,
                                             "detail": detail})
            if not installed:
                raise with_data(ActionFailedError(
                    detail, hint=f"roll {bid} back (POST .../boards/ID/update/rollback) to "
                                 f"restore the backup {backup}"), outcome=outcome)
            return outcome

        return accepted(jobs.start(bid, "update_harness", work))

    @app.post(f"{API}/boards/{{bid}}/update/rollback", status_code=202)
    def update_rollback(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.session(bid)
        sim.require_update()
        jobs.gate(bid)
        info = sim.info(bid)
        for cap in ("storage_install", "reboot_board"):
            if cap in info.unavailable:
                raise UnavailableError(cap, info.unavailable[cap])
        prev = sim.previous.get(bid)

        def work(progress: Any) -> dict[str, Any]:
            sim.publish("update.started", bid, {"version": "rollback", "mode": "restore",
                                                "rekey": False})
            for phase, done, total in (("restore:restore", 0, 0), ("reboot:sent", 1, 3),
                                       ("reboot:down", 2, 3), ("reboot:up", 3, 3)):
                time.sleep(0.15)
                progress(phase, done, total)
                sim.publish("update.progress", bid, {"phase": phase, "bytes": done,
                                                     "total": total})
            if prev and sim.rollback_outcome == "restored":
                sim.engine._set_identity(bid, **prev)
            after = sim.identity(bid)
            result = sim.rollback_outcome
            detail = (f"restored the backup; the board reports shell {after.shell_id}, harness "
                      f"{after.harness_version}")
            outcome = {"board_id": bid, "version": "rollback", "result": result,
                       "detail": detail, "ok": result == "restored", "checks": [],
                       "identity_after": {"shell_id": after.shell_id,
                                          "harness": after.harness_version},
                       "restore_hint": "", "stored": [], "skipped": {}, "os_slot": None}
            sim.publish("update.done", bid, {"version": "rollback", "result": result,
                                             "detail": detail})
            if result != "restored":
                raise with_data(ActionFailedError(
                    detail, hint="the SD is restored; check the board (GET .../boards/ID), and "
                                 "power-cycle it if it is dark"), outcome=outcome)
            return outcome

        return accepted(jobs.start(bid, "update_rollback", work))

    def app_job(kind: str, version: str, what: str) -> JSONResponse:
        sim.require_update()
        for job in jobs.running():
            where = f"on {job.board_id}" if job.board_id else "in the service"
            raise HeldError(f"cannot {what} while {job.describe()} runs {where}",
                            holder=f"harness-manager-daemon {job.describe()}",
                            hint=f"wait for it to finish (GET /api/v1/jobs/{job.id}), then try "
                                 "again")

        def work(progress: Any) -> dict[str, Any]:
            progress("stage" if kind == "update_app" else "switch", 0, 0)
            time.sleep(0.2)
            held = [f"this process holds {b}" for b in sim.engine.open_boards()]
            if held:        # T7's switch rail: staged, but no switch while a session is held
                raise HeldError(f"cannot switch to {version} now: {'; '.join(held)}",
                                hint="finish or close those sessions first; the new version "
                                     "stays staged")
            before = sim.app_version
            sim.app_version = version
            return {"target": "app", "result": "switched", "version": version,
                    "previous": before}

        return accepted(jobs.start("", kind, work))

    @app.post(f"{API}/update/app", status_code=202)
    def update_app(body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        return app_job("update_app", str(body.get("version") or sim.app_current),
                       "update the app")

    @app.post(f"{API}/update/app/rollback", status_code=202)
    def update_app_rollback() -> JSONResponse:
        return app_job("update_app_rollback", "0.0.1", "roll the app back")


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

