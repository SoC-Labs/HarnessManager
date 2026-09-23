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

A factory may return ``None`` when the session lacks the links it needs. The
capability view then explains why.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from pyverify import rm_id as rmid

from harness_manager.core.capabilities import CapabilitySpec
from harness_manager.core.errors import HarnessError, NothingOnTargetError, UsageError
from harness_manager.core.model import BoardIdentity, Candidate, Health, Link, LinkKind
from harness_manager.core.pack import BoardPack, BoardSession, ProbeHints

from .capabilities import SPECS
from .constants import (
    CONSOLE_PORTS,
    CONTROL_PORT,
    DAP_DESIGN_CONFIGS,
    DEFAULT_SHELL_HOST,
    DUT_CONSOLE_PACE_S,
    JTAG_RBB_PORT,
    OPENOCD_CFG_DIR,
    PACED_CONSOLES,
)
from .shell import Mps3Shell, parse_endpoint


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
        return (OPENOCD_CFG_DIR,) if OPENOCD_CFG_DIR else ()

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
                 console_pace_s: float = DUT_CONSOLE_PACE_S) -> None:
        self.candidate = candidate
        self.shell = shell
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
        self.consoles = Mps3Consoles(endpoints, console_pace_s, shell) if endpoints else None

        for attr, module, factory in (
            ("deploy", "deploy", "make_deploy_adapter"),
            ("controller", "mcc", "make_controller_adapter"),
            ("storage", "sd", "make_storage_adapter"),
            ("debug", "openocd", "make_debug_adapter"),
            ("telemetry", "telemetry", "make_telemetry_adapter"),
            ("power", "telemetry", "make_power_adapter"),
            ("clocks", "clock", "make_clock_adapter"),
        ):
            make = _hook(module, factory)
            if make is not None:
                adapter = make(self)
                if adapter is not None or attr != "debug":
                    setattr(self, attr, adapter)

    def link(self, kind: LinkKind) -> Link | None:
        return next((lk for lk in self.candidate.links if lk.kind == kind), None)

    def identity(self) -> BoardIdentity:
        if self.shell is None:
            return BoardIdentity(board_type="mps3")
        from .shell import ShellRescueError
        try:
            return self.shell.identity()
        except ShellRescueError as exc:      # stage0 rescue: report what stage0 said (T12-2)
            return exc.identity

    def health(self) -> Health:
        if self.shell is None:
            return Health(reachable=False, control_channel="offline",
                          notes=("no Ethernet link to the shell",))
        return self.shell.health()


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

    def candidate_for_host(self, spec: str) -> Candidate:
        host, port = parse_endpoint(spec, CONTROL_PORT)
        addr = f"{host}:{port}"
        return _with_config_links(Candidate(
            pack=self.name,
            board_id=f"mps3@{addr}",
            links=(Link(LinkKind.ETHERNET, addr, "shell control channel"),),
            label=f"MPS3 at {addr}",
            evidence="given explicitly",
        ))

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        found: list[Candidate] = []
        if hints.scan_network or hints.hosts:
            hosts = hints.hosts or (DEFAULT_SHELL_HOST,)
            for spec in hosts:
                cand = self.candidate_for_host(spec)
                host, port = parse_endpoint(spec, CONTROL_PORT)
                try:
                    ident = Mps3Shell(host, port, timeout=hints.timeout_s).identity()
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
        return [_with_config_links(c) for c in found]

    def open(self, candidate: Candidate) -> Mps3Session:
        eth = next((lk for lk in candidate.links if lk.kind == LinkKind.ETHERNET), None)
        shell = None
        if eth is not None:
            host, port = parse_endpoint(eth.address, CONTROL_PORT)
            shell = Mps3Shell(host, port)
        elif not any(lk.kind in (LinkKind.USB_SERIAL, LinkKind.USB_MSD) for lk in candidate.links):
            raise UsageError("this candidate has no link the MPS3 pack can use")
        return Mps3Session(candidate, shell, self._console_ports, self._rbb_port,
                           push_port=self._push_port, tftp_port=self._tftp_port,
                           console_pace_s=self._console_pace_s)
