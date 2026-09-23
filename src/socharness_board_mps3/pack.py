"""The MPS3 board pack: probe, open, and adapters.

Scaffold status (Wave 0):
- probe: explicit hosts plus the default shell address; USB scan is Team T3.
- session: identity, health, DUT reset, console endpoints, and the debug
  config choice by rm_id.
- deploy, clocks and telemetry adapters are Teams T2/T3/T4.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pyverify import rm_id as rmid

from socharness.core.capabilities import CapabilitySpec
from socharness.core.errors import HarnessError, NothingOnTargetError, UsageError
from socharness.core.model import BoardIdentity, Candidate, Health, Link, LinkKind
from socharness.core.pack import BoardPack, BoardSession, ProbeHints

from .capabilities import SPECS
from .constants import (
    CONSOLE_PORTS,
    CONTROL_PORT,
    DAP_DESIGN_CONFIGS,
    DEFAULT_SHELL_HOST,
    JTAG_RBB_PORT,
)
from .shell import Mps3Shell, parse_endpoint


class Mps3Consoles:
    def __init__(self, host: str, ports: dict[str, int]) -> None:
        self._host = host
        self._ports = ports

    def console_endpoints(self) -> dict[str, str]:
        return {name: f"tcp://{self._host}:{port}" for name, port in self._ports.items()}


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
    def __init__(self, session: Mps3Session, rbb_port: int) -> None:
        self._session = session
        self._rbb_port = rbb_port

    def openocd_probe_args(self) -> tuple[str, ...]:
        # Overrides MUST precede any -f (harness handover B4).
        return (
            f"set RBB_HOST {self._session.shell.host}",
            f"set RBB_PORT {self._rbb_port}",
        )

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
    def __init__(self, candidate: Candidate, shell: Mps3Shell, console_ports: dict[str, int],
                 rbb_port: int) -> None:
        self.candidate = candidate
        self.shell = shell
        self.consoles = Mps3Consoles(shell.host, console_ports)
        self.resets = Mps3Resets(shell)
        self.debug = Mps3Debug(self, rbb_port)

    def identity(self) -> BoardIdentity:
        return self.shell.identity()

    def health(self) -> Health:
        return self.shell.health()


class Mps3Pack(BoardPack):
    name = "mps3"
    title = "Arm MPS3 (V2M-MPS3, HBI0309C)"

    def __init__(self, *, console_ports: dict[str, int] | None = None,
                 rbb_port: int = JTAG_RBB_PORT) -> None:
        # Port overrides exist so tests can point the pack at a FakeShell on
        # ephemeral ports. Real boards use the defaults.
        self._console_ports = dict(console_ports or CONSOLE_PORTS)
        self._rbb_port = rbb_port

    def capability_specs(self) -> Iterable[CapabilitySpec]:
        return SPECS

    def candidate_for_host(self, spec: str) -> Candidate:
        host, port = parse_endpoint(spec, CONTROL_PORT)
        addr = f"{host}:{port}"
        return Candidate(
            pack=self.name,
            board_id=f"mps3@{addr}",
            links=(Link(LinkKind.ETHERNET, addr, "shell control channel"),),
            label=f"MPS3 at {addr}",
            evidence="given explicitly",
        )

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        hosts = hints.hosts or ((DEFAULT_SHELL_HOST,) if hints.scan_network else ())
        found: list[Candidate] = []
        for spec in hosts:
            cand = self.candidate_for_host(spec)
            host, port = parse_endpoint(spec, CONTROL_PORT)
            try:
                ident = Mps3Shell(host, port, timeout=hints.timeout_s).identity()
            except HarnessError:
                continue
            found.append(
                Candidate(
                    pack=cand.pack,
                    board_id=cand.board_id,
                    links=cand.links,
                    label=f"MPS3 {ident.rm_name or ident.rm_id} on shell {ident.shell_id}",
                    evidence="answered ping",
                )
            )
        return found

    def open(self, candidate: Candidate) -> Mps3Session:
        eth = next((lk for lk in candidate.links if lk.kind == LinkKind.ETHERNET), None)
        if eth is None:
            raise UsageError("this scaffold can only open MPS3 boards over Ethernet",
                             hint="USB-only sessions arrive with Team T3")
        host, port = parse_endpoint(eth.address, CONTROL_PORT)
        return Mps3Session(candidate, Mps3Shell(host, port), self._console_ports, self._rbb_port)
