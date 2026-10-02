"""The MPS3 board pack: probe, open, and adapter wiring.

This file is LEAD-OWNED. Teams do not edit it. Instead each team implements a
factory in its own module, and this file wires it in if it exists:

| Hook (module:function)                         | Team | Returns                            |
|------------------------------------------------|------|------------------------------------|
| ``.deploy:make_deploy_adapter(session)``       | T2   | ``DeployAdapter``                  |
| ``.mcc:make_controller_adapter(session)``      | T3   | ``ControllerAdapter`` (needs a USB serial link) |
| ``.sd:make_storage_adapter(session)``          | T3   | ``StorageAdapter`` (needs a USB MSD link) |
| ``.usb:probe_usb(hints)``                      | T3   | ``list[Candidate]`` with USB links  |
| ``.usb:serial_console_endpoints(candidate)``   | T3   | extra console endpoints (FPGA UARTs) |
| ``.openocd:make_debug_adapter(session)``       | T4   | ``DebugAdapter`` (replaces the scaffold one) |
| ``.telemetry:make_telemetry_adapter(session)`` | T9   | ``TelemetryAdapter``               |
| ``.telemetry:make_power_adapter(session)``     | T9   | ``PowerAdapter`` (boards.toml power table) |
| ``.telemetry:with_config_links(candidate)``    | T9   | the candidate plus its boards.toml links |
| ``.clock:make_clock_adapter(session)``         | lead | ``ClockAdapter`` (DUT MMCM presets) |
| ``.identify:probe_identify(hints, found)``     | T12  | ``list[Candidate]`` found by UDP 6899 identify |
| ``.uart:console_baud_info(endpoints, shell)``  | L2   | each console's rate (``ConsoleAdapter.console_baud_info``) |
| ``.uart:console_set_baud(endpoints, shell, name, baud)`` | L2 | the ``uart_baud`` verb (``ConsoleAdapter.console_set_baud``) |
| ``.hub:route_candidate(candidate, via)``       | L1   | the candidate routed ``via`` (boards.toml ``via``/``hub``) |
| ``.tunnel:open_reach(candidate, ports)``       | L1   | ``Reach``: the SSH tunnel's local ports, or None (direct) |
| ``.tunnel:probe_reach(spec, via, ...)``        | L1   | a short tunnel for a probe (control port only) |
| ``.tunnel:probe_route(candidate)``            | SIDEBAR-UX | the probe's ``(via, jump)``: ``via="hub"`` tunnels to the hub's host |
| ``.hub:make_hub_adapter(session)``             | L1   | ``Mps3Hub`` (leases, shares) when the board has a hub |
| ``.hub:relay_share_consoles(endpoints, cand)`` | L1   | the endpoints, each ``hub://`` share as a ``tcp://`` relay |
| ``.naming:name_candidate(candidate)``          | N1   | the candidate with its display name (boards.toml, harness, hub table) |
| ``.naming:session_board_name(session, ident)`` | N1   | ``(name, source)``: the hub's confirmed name for an open board |
| ``.panel:make_panel_adapter(session)``         | P2   | ``session.panel``: the front panel (``core.panel.PanelAdapter``) |
| ``.xvc:make_xvc_adapter(session)``             | XVC  | ``XvcAdapter``: the harness's XVC, partition-scoped (CCR X-8) |
| ``.hub_sd:make_hub_sd_adapter(session)``       | HUB-SD | ``session.hub_sd``: the config SD through the hub (H10) |
| ``.sd_ab:make_ab_storage_adapter(session)``    | HUB-SD | ``session.ab_storage``: the config SD A/B by pointer (U8) |
| ``.settings:mps3_rows(**pack kwargs)``         | SET-PACK | ``Mps3Pack.settings()``: the ``mps3.*`` rows and boards.toml tables |
| ``.claim:make_claim_adapter(session)``         | LINUX-CLAIM | ``session.claim``: the Linux harness's SSH claim and board-SSH reach |
| ``.os_slots:make_os_slot_adapter(session)``    | LINUX-SLOTS | ``session.os_slots``: the Linux harness's A/B OS slots (CCR T7-2) |
| ``.card:make_card_adapter(session)``           | LINUX-SLOTS | ``session.card``: the user microSD, D13 overlay store (CCR LS-1) |
| ``.display:make_display_adapter(session)``     | LM2  | ``session.display``: the live LCD mirror (``Mps3Pack.display_adapter`` is the pack hook) |
| ``.net_identity:make_identity_adapter(session)`` | BOARD-ID | ``session.net_identity``: the board's label/IP/MAC, its hub record, the fix seam |

A factory may return ``None`` when the session lacks the links it needs. The
capability view then explains why.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from pyverify import rm_id as rmid

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import CapabilitySpec
from harness_manager.core.errors import (
    HarnessError,
    HeldError,
    NothingOnTargetError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity, Candidate, Health, Link, LinkKind
from harness_manager.core.pack import BoardPack, BoardSession, ProbeHints

from . import ctlgate
from .capabilities import HARNESS_STATES, SPECS
from .constants import (
    CONSOLE_PORTS,
    CONTROL_PORT,
    DAP_DESIGN_CONFIGS,
    DEFAULT_SHELL_HOST,
    DUT_CONSOLE_PACE_S,
    JTAG_RBB_PORT,
    PACED_CONSOLES,
    PUSH_PORT,
    XVC_PORT,
)
from .shell import Mps3Shell, parse_endpoint

#: How long a "held" answer through an SSH tunnel waits for ssh's "open failed" line,
#: which tells a board that refused the hub from one another client holds (Q2).
OPEN_FAILURE_GRACE_S = 0.5


def _hook(module: str, attr: str) -> Callable[..., Any] | None:
    """Load a team's factory if its module exists yet; ``None`` otherwise."""
    try:
        mod = importlib.import_module(f"{__package__}.{module}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith(module):
            return None
        raise
    return getattr(mod, attr, None)


def _with_config_links(candidate: Candidate) -> Candidate:
    """Add the links boards.toml gives this board (a power meter, a SYSMON JTAG cable)."""
    add = _hook("telemetry", "with_config_links")   # T9
    return add(candidate) if add is not None else candidate


def _named(candidate: Candidate) -> Candidate:
    """Give the candidate its display name (N1: boards.toml, its harness, its hub table)."""
    name = _hook("naming", "name_candidate")   # N1
    return name(candidate) if name is not None else candidate


def _route(candidate: Candidate, via: str = "") -> Candidate:
    """Route the candidate ``via`` an SSH hub and add its hub shares (L1; boards.toml)."""
    route = _hook("hub", "route_candidate")   # L1
    return route(candidate, via) if route is not None else candidate


class Mps3Consoles:
    def __init__(self, endpoints: dict[str, str], pace_s: float = DUT_CONSOLE_PACE_S,
                 shell: Mps3Shell | None = None) -> None:
        self._endpoints = endpoints
        self._pace_s = pace_s
        self._shell = shell

    def console_endpoints(self) -> dict[str, str]:
        return dict(self._endpoints)

    def console_write_pace_s(self) -> dict[str, float]:
        """The DUT UARTs drop unpaced input (see constants.DUT_CONSOLE_PACE_S)."""
        return {n: self._pace_s for n in PACED_CONSOLES if n in self._endpoints}

    def console_baud_info(self) -> dict[str, dict]:
        """Each console's rate and whether it can change (L2: ``.uart``)."""
        from .uart import console_baud_info

        return console_baud_info(self._endpoints, self._shell)

    def console_set_baud(self, name: str, baud: int) -> dict:
        """The harness verb ``uart_baud`` (L2: ``.uart``); UnavailableError with the reason."""
        from .uart import console_set_baud

        return console_set_baud(self._endpoints, self._shell, name, baud)


class Mps3Resets:
    def __init__(self, shell: Mps3Shell) -> None:
        self._shell = shell

    def reset_targets(self) -> Sequence[str]:
        return ("dut",)

    def reset(self, target: str) -> None:
        if target != "dut":
            raise UsageError(f"reset target {target!r} is not supported", hint="targets: dut")
        self._shell.reset_dut()


class Mps3Debug:
    """Scaffold debug adapter. Team T4 replaces it via ``.openocd:make_debug_adapter``."""

    def __init__(self, session: Mps3Session, rbb_port: int) -> None:
        self._session = session
        self._rbb_port = rbb_port

    def openocd_probe_args(self) -> tuple[str, ...]:
        # Overrides MUST precede any -f (harness handover B4).
        return (
            f"set RBB_HOST {self._session.shell.host}",
            f"set RBB_PORT {self._rbb_port}",
        )

    def openocd_search_paths(self) -> tuple[Path, ...]:
        # SET-WIRE: the setting (its variable, then the settings), not the import-time copy
        # (SETTINGS.md §12.9), so the search path and config_dir() never disagree.
        from .openocd import config_dir

        cfg = config_dir()
        return (cfg,) if cfg else ()

    def openocd_config(self) -> tuple[str, ...]:
        ident = self._session.identity()
        design = rmid.design_id(ident.rm_id) if ident.rm_id else None
        cfg = DAP_DESIGN_CONFIGS.get(design) if design is not None else None
        if cfg is None:
            raise NothingOnTargetError(
                f"the loaded design ({ident.rm_name or ident.rm_id or 'none'}) has no debug port",
                hint="load nanosoc, nanosoc_upy or nanosoc_iice first",
            )
        return cfg


class Mps3Session(BoardSession):
    def __init__(self, candidate: Candidate, shell: Mps3Shell | None,
                 console_ports: dict[str, int], rbb_port: int, *,
                 push_port: int | None = None, tftp_port: int | None = None,
                 console_pace_s: float = DUT_CONSOLE_PACE_S, reach: Any = None) -> None:
        self.candidate = candidate
        self.shell = shell
        # L1: how this session reaches the board (an SSH tunnel's local ports), or None.
        self.reach = reach
        # Read by the deploy adapter (T2). None means "use the default or env override".
        self.push_port = push_port
        self.tftp_port = tftp_port
        # Read by the debug adapter (T4): the board's remote_bitbang JTAG port.
        self.rbb_port = rbb_port
        endpoints: dict[str, str] = {}
        if shell is not None:
            endpoints.update({n: f"tcp://{shell.host}:{p}" for n, p in console_ports.items()})
            from .shell import make_reset_adapter  # T12: targets come from the harness
            self.resets = make_reset_adapter(self) or Mps3Resets(shell)
            self.debug = Mps3Debug(self, rbb_port)
        extra = _hook("usb", "serial_console_endpoints")
        if extra is not None:
            endpoints.update(extra(candidate))
        relay = _hook("hub", "relay_share_consoles")    # L1: hub shares as tcp:// consoles
        if relay is not None:
            endpoints = relay(endpoints, candidate)
        self.consoles = Mps3Consoles(endpoints, console_pace_s, shell) if endpoints else None

        for attr, module, factory in (
            ("deploy", "deploy", "make_deploy_adapter"),
            ("controller", "mcc", "make_controller_adapter"),
            ("storage", "sd", "make_storage_adapter"),
            ("debug", "openocd", "make_debug_adapter"),
            ("telemetry", "telemetry", "make_telemetry_adapter"),
            ("power", "telemetry", "make_power_adapter"),
            ("clocks", "clock", "make_clock_adapter"),
            ("hub", "hub", "make_hub_adapter"),              # L1: leases and shares
            ("panel", "panel", "make_panel_adapter"),        # P2: the front panel (CLCD)
            ("xvc", "xvc", "make_xvc_adapter"),              # CCR X-8: fabric debug (XVC)
            ("hub_sd", "hub_sd", "make_hub_sd_adapter"),     # HUB-SD: SD writes via the hub
            ("ab_storage", "sd_ab", "make_ab_storage_adapter"),  # HUB-SD U8: SD A/B view
            ("claim", "claim", "make_claim_adapter"),        # LINUX-CLAIM: SSH claim + reach
            ("os_slots", "os_slots", "make_os_slot_adapter"),  # CCR T7-2: Linux OS slots A/B
            ("card", "card", "make_card_adapter"),           # CCR LS-1: user microSD (D13)
            ("display", "display", "make_display_adapter"),  # LM2: the live LCD mirror
            ("net_identity", "net_identity", "make_identity_adapter"),  # BOARD-ID: label/IP/MAC
            ("harness_mcc", "harness_mcc", "make_harness_mcc_adapter"),  # FIX-PACK-2: v0.18 MCC
        ):
            make = _hook(module, factory)
            if make is not None:
                adapter = make(self)
                if adapter is not None or attr != "debug":
                    setattr(self, attr, adapter)

    def link(self, kind: LinkKind) -> Link | None:
        return next((lk for lk in self.candidate.links if lk.kind == kind), None)

    def set_observer(self, observer: Any) -> None:
        """CCR QUIET-1: the control port's calls (``Mps3Shell.observer``) and the MCC's reads
        (the controller's ``observer``: a second reader of its console is another client)
        report to ``observer(channel, exc)``. Applies to the adapters the session has now."""
        from harness_manager.core.pack import CONTACT_CONTROL, CONTACT_MCC

        def on(channel: str) -> Any:
            return None if observer is None else (lambda exc: observer(channel, exc))

        if self.shell is not None:
            self.shell.observer = on(CONTACT_CONTROL)
        controller = getattr(self, "controller", None)
        if controller is not None and hasattr(controller, "observer"):
            controller.observer = on(CONTACT_MCC)

    def capability_reasons(self, available: frozenset[str],
                           identity: BoardIdentity | None = None) -> dict[str, str]:
        """FIX-PACK-2 (the engine's ``session.capability_reasons`` seam): capabilities the
        links and features allow that this board cannot use now, with why. It only narrows.

        - ``discover_network``: a recent identify answer from the board is the proof
          (``identify.DiscoverWitness``); through a hub it is "not through a hub".
        - ``reboot_board``/``clock_board`` offered only by the harness's MCC route (v0.18
          ``mccif``/``mcc_local``): the route ``mcc status`` reports must serve the act, and
          Harness Manager must drive it (``harness_mcc``: reboot and osc are PENDING v0.18).
        """
        out: dict[str, str] = {}
        hmcc = getattr(self, "harness_mcc", None)
        if hmcc is not None:
            out.update(hmcc.capability_reasons(
                available, tuple(getattr(identity, "features", ()) or ()),
                [lk.kind for lk in self.candidate.links]))
        if C.DISCOVER_NETWORK in available:
            witness = getattr(self, "_discover", None)
            if witness is None:
                from .identify import DiscoverWitness

                witness = self._discover = DiscoverWitness()
            why = witness.reason(self)
            if why:
                out[C.DISCOVER_NETWORK] = why
        return out

    def board_name(self, identity: BoardIdentity | None = None) -> tuple[str, str]:
        """``(name, source)`` for the open board beyond its candidate: the hub's name (N1)."""
        name = _hook("naming", "session_board_name")
        return name(self, identity) if name is not None else ("", "")

    def identity(self) -> BoardIdentity:
        if self.shell is None:
            return BoardIdentity(board_type="mps3")
        from .shell import ShellRescueError, SwapSettlingError
        started = time.monotonic()
        try:
            return self.shell.identity()
        except ShellRescueError as exc:      # stage0 rescue: report what stage0 said (T12-2)
            return exc.identity
        except SwapSettlingError:            # our failed push: the hub's refusal is the swap
            raise
        except HeldError as exc:
            raise self._refused_through_tunnel(started) or exc from None

    def _refused_through_tunnel(self, started: float) -> HarnessError | None:
        """L1: through ssh -L a port that refuses the HUB is accepted locally, then closed,
        which the codec reads as "held by another client". ssh logs the refusal; use it."""
        tunnel = getattr(self.reach, "tunnel", None)
        # ssh's line may trail the close we saw: wait for it briefly (Q2). Only this
        # failure path waits, and only through a tunnel.
        refused = tunnel.open_failures_since(started, wait_s=OPEN_FAILURE_GRACE_S) \
            if tunnel is not None else []
        if not refused:
            return None
        return UnreachableError(
            f"the hub {tunnel.host} could not reach the shell ({refused[-1]})",
            hint="the board is off, rebooting, or its harness is not listening; "
                 "check it from the hub, or power-cycle it")

    def health(self) -> Health:
        if self.shell is None:
            return Health(reachable=False, control_channel="offline",
                          notes=("no Ethernet link to the shell",))
        started = time.monotonic()
        health = self.shell.health()
        tunnel = getattr(self.reach, "tunnel", None)
        refused = tunnel.open_failures_since(
            started, wait_s=OPEN_FAILURE_GRACE_S if health.control_channel == "busy" else 0.0) \
            if tunnel is not None else []
        # Within the window after our own failed push, the hub being refused IS the
        # harness finishing that swap: it stays busy, with the shell's hint.
        settling = self.shell.settling_remaining_s() > 0.0
        if refused and health.control_channel == "busy" and not settling:
            # Through ssh -L, "accepted then closed" is what the hub being refused looks
            # like too; ssh said so, so this is not another client holding the port.
            busy = HARNESS_STATES["harness.busy"]
            health = Health(reachable=False, control_channel="offline", counters=health.counters,
                            notes=(f"the hub {tunnel.host} could not reach the shell "
                                   f"({refused[-1]}); nothing answers on the control channel",
                                   *(n for n in health.notes if n != busy)))
        if tunnel is not None and tunnel.state != "up":
            # Through a tunnel, "no answer" may be the tunnel, not the board: say which.
            note = f"the SSH tunnel to {tunnel.host} is {tunnel.state}: {tunnel.detail}"
            health = Health(reachable=health.reachable, control_channel=health.control_channel,
                            counters=health.counters, notes=(note, *health.notes))
        return health

    def close(self) -> None:
        """Close the live display's forward (LM2: never left open, FINDINGS_TRIAGE #20), the
        claim forward (CLAIMED-LOCK: debug, XVC, slots and card on a claimed board), the
        hub's share forwards, then the board's SSH tunnel (L1). Idempotent."""
        for part in ("display", "claim"):
            adapter = getattr(self, part, None)
            if adapter is not None and hasattr(adapter, "close"):
                adapter.close()               # never raises
        hub = getattr(self, "hub", None)
        try:
            if hub is not None:
                hub.close()
        finally:                          # a failing share close must not leave the ssh up
            if self.reach is not None:
                self.reach.close()


class Mps3Pack(BoardPack):
    name = "mps3"
    title = "Arm MPS3 (V2M-MPS3, HBI0309C)"

    def __init__(self, *, console_ports: dict[str, int] | None = None,
                 rbb_port: int = JTAG_RBB_PORT, push_port: int | None = None,
                 tftp_port: int | None = None,
                 console_pace_s: float = DUT_CONSOLE_PACE_S) -> None:
        # Port overrides exist so tests can point the pack at a FakeShell on
        # ephemeral ports. Real boards use the defaults. ``console_pace_s`` is the
        # DUT UART input pace (0 for a DUT whose UART has a receive FIFO).
        self._console_ports = dict(console_ports or CONSOLE_PORTS)
        self._rbb_port = rbb_port
        self._push_port = push_port
        self._tftp_port = tftp_port
        self._console_pace_s = console_pace_s

    def capability_specs(self) -> Iterable[CapabilitySpec]:
        return SPECS

    def settings(self) -> Iterable[Any]:
        """The MPS3's settings rows (CCR SET-PACK-2: ``.settings``), with this instance's
        values as the defaults (the resolver's pack layer). Nothing reads through them yet."""
        rows = _hook("settings", "mps3_rows")
        return rows(console_pace_s=self._console_pace_s, rbb_port=self._rbb_port,
                    push_port=self._push_port, tftp_port=self._tftp_port) if rows else ()

    def identity_policy(self) -> Any:
        """Lane IDENTITY: the MAC and IP rules for naming an MPS3 board (random MACs 02:...,
        never 02:00:00:*; the pool mps3.identity.ip_pool; never 192.168.10.101)."""
        policy = _hook("net_identity", "identity_policy")
        return policy() if policy else None

    def candidate_for_host(self, spec: str, via: str = "") -> Candidate:
        """``via`` ("ssh:HOST") reaches the shell through an SSH tunnel (L1); boards.toml
        ``via`` does the same when it is not given."""
        host, port = parse_endpoint(spec, CONTROL_PORT)
        addr = f"{host}:{port}"
        return _named(_route(_with_config_links(Candidate(
            pack=self.name,
            board_id=f"mps3@{addr}",
            links=(Link(LinkKind.ETHERNET, addr, "shell control channel"),),
            label=f"MPS3 at {addr}",
            evidence="given explicitly",
        )), via))

    def reap_orphans(self) -> list[str]:
        """Stop what a killed owner left running: its SSH tunnels (Q2). Optional pack hook;
        harness-manager-daemon calls it when it starts. Returns what it stopped."""
        reap = _hook("tunnel", "reap_orphans")
        return [f"ssh tunnel pid {pid}" for pid in reap()] if reap is not None else []

    def display_adapter(self, session: BoardSession) -> Any:
        """The pack hook ``display_adapter`` (lane LM2; the daemon's display API calls it): the
        session's live display adapter (``.display:display_adapter``), made once per session,
        or None (no Ethernet shell)."""
        hook = _hook("display", "display_adapter")
        return hook(session) if hook is not None else getattr(session, "display", None)

    def hub_for(self, candidate: Candidate) -> Any:
        """The board's hub adapter (leases, shares) without opening it; None without a hub (L1)."""
        adapter_for = _hook("hub", "adapter_for")
        return adapter_for(candidate) if adapter_for is not None else None

    def _remote_ports(self) -> dict[str, int]:
        """The board ports a session uses, by name (what an SSH tunnel forwards; L1)."""
        return {"push": self._push_port or PUSH_PORT, "rbb": self._rbb_port,
                **self._console_ports, "xvc": XVC_PORT}

    def _identity(self, host: str, port: int, via: str, timeout_s: float,
                  jump: str = "") -> BoardIdentity:
        if not via:
            return Mps3Shell(host, port, timeout=timeout_s).identity()
        probe_reach = _hook("tunnel", "probe_reach")   # L1
        if probe_reach is None:
            raise UsageError("this build cannot reach a board through an SSH tunnel")
        with probe_reach(f"{host}:{port}", via, timeout_s=timeout_s, jump=jump) as (lhost, lport):
            # SERIAL-6900: the probe's tunnel reaches the same single-client port
            shell = Mps3Shell(lhost, lport, timeout=timeout_s,
                              gate_key=ctlgate.key_for(host, port))
            shell.lagging_close = True
            return shell.identity()

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        found: list[Candidate] = []
        if hints.scan_network or hints.hosts:
            hosts = hints.hosts or (DEFAULT_SHELL_HOST,)
            via = getattr(hints, "via", "")          # L1 (ProbeHints.via, CCR L1-1)
            for spec in hosts:
                cand = self.candidate_for_host(spec, via)
                host, port = parse_endpoint(spec, CONTROL_PORT)
                # SIDEBAR-UX: a via="hub" candidate (boards.toml) is probed through its
                # hub's SSH tunnel, the route its open falls back to (tunnel.probe_route).
                route = _hook("tunnel", "probe_route")
                try:
                    tvia, jump = route(cand) if route else ("", "")
                    ident = self._identity(host, port, tvia, hints.timeout_s, jump)
                except HarnessError:
                    continue
                found.append(Candidate(
                    pack=cand.pack, board_id=cand.board_id, links=cand.links,
                    label=f"MPS3 {ident.rm_name or ident.rm_id} on shell {ident.shell_id}",
                    evidence="answered ping", identity=ident,
                ))
        identify = _hook("identify", "probe_identify")   # T12: UDP 6899 identify
        if identify is not None and hints.scan_network:
            seen = {c.board_id for c in found}
            found.extend(c for c in identify(hints, list(found)) if c.board_id not in seen)
        usb = _hook("usb", "probe_usb")
        if usb is not None and (hints.scan_usb or hints.serial_ports or hints.volumes):
            usb_found = usb(hints, list(found))
            # A USB board paired with an Ethernet shell replaces that shell's candidate.
            ids = {c.board_id for c in usb_found}
            found = [c for c in found if c.board_id not in ids] + list(usb_found)
        return [_named(_with_config_links(c)) for c in found]

    def open(self, candidate: Candidate) -> Mps3Session:
        eth = next((lk for lk in candidate.links if lk.kind == LinkKind.ETHERNET), None)
        if eth is None and not any(lk.kind in (LinkKind.USB_SERIAL, LinkKind.USB_MSD)
                                   for lk in candidate.links):
            raise UsageError("this candidate has no link the MPS3 pack can use")
        # L1: a via="ssh" candidate is reached through an SSH tunnel; the session then
        # talks to the tunnel's local ports, and closing the session closes the tunnel.
        open_reach = _hook("tunnel", "open_reach")
        reach = open_reach(candidate, self._remote_ports()) if open_reach and eth else None
        console_ports, rbb_port = self._console_ports, self._rbb_port
        push_port, tftp_port = self._push_port, self._tftp_port
        shell = None
        # SERIAL-6900: every shell of this board shares the gate of its own control address
        gate_key = ctlgate.key_of_address(eth.address, CONTROL_PORT) if eth is not None else ""
        if reach is not None:
            shell = Mps3Shell(reach.host, reach.ports["control"], gate_key=gate_key)
            shell.lagging_close = True             # through ssh -L (ctlgate)
            console_ports = {n: reach.ports[n] for n in self._console_ports}
            rbb_port, push_port, tftp_port = reach.ports["rbb"], reach.ports["push"], None
        elif eth is not None:
            host, port = parse_endpoint(eth.address, CONTROL_PORT)
            shell = Mps3Shell(host, port, gate_key=gate_key)
        try:
            # mps3.console.pace_ms (reopen): the settings, else this pack's own pace
            from .settings import configured_s

            pace_s = configured_s("mps3.console.pace_ms", self._console_pace_s)
            return Mps3Session(candidate, shell, console_ports, rbb_port,
                               push_port=push_port, tftp_port=tftp_port,
                               console_pace_s=pace_s, reach=reach)
        except BaseException:
            if reach is not None:
                reach.close()
            raise
