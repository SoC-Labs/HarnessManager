"""Team T1 test doubles: a scriptable board pack/session and telemetry sources.

Only the T1 tests use these. The virtual MPS3 (tests/fakes/virtual_board.py)
covers the real pack; these cover the paths a real board cannot be made to
take on demand (a pack that fails to probe or open, a sensor that raises).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from socharness.core import capabilities as C
from socharness.core.capabilities import CapabilitySpec, via
from socharness.core.model import BoardIdentity, Candidate, Health, Link, LinkKind, Reading
from socharness.core.pack import BoardPack, BoardSession, ProbeHints

SPECS = (
    CapabilitySpec(C.IDENTIFY, "Identify", (via(LinkKind.ETHERNET),)),
    CapabilitySpec(C.REBOOT_BOARD, "Reboot", (via(LinkKind.USB_SERIAL),),
                   needs_hint="needs the Debug USB cable"),
    CapabilitySpec(C.RESET_SHELL, "Restart", (via(LinkKind.ETHERNET, features=("reboot",)),)),
)


def candidate(board_id: str = "fake@1", *kinds: LinkKind, pack: str = "fake") -> Candidate:
    kinds = kinds or (LinkKind.ETHERNET,)
    return Candidate(pack=pack, board_id=board_id,
                     links=tuple(Link(k, f"{k.value}:{board_id}") for k in kinds),
                     label=f"fake board {board_id}", evidence="fake")


class FakeTelemetry:
    def __init__(self, readings: Sequence[Reading] = (), error: Exception | None = None) -> None:
        self._readings = list(readings)
        self._error = error

    def readings(self) -> Sequence[Reading]:
        if self._error is not None:
            raise self._error
        return list(self._readings)


class FakeController:
    """Only the telemetry half of a ControllerAdapter."""

    def __init__(self, temps: Sequence[Reading] = (), oscs: Sequence[Reading] = (), *,
                 temp_error: Exception | None = None,
                 osc_error: Exception | None = None) -> None:
        self._temps, self._oscs = list(temps), list(oscs)
        self._temp_error, self._osc_error = temp_error, osc_error

    def temperatures(self) -> Sequence[Reading]:
        if self._temp_error is not None:
            raise self._temp_error
        return list(self._temps)

    def oscillators(self) -> Sequence[Reading]:
        if self._osc_error is not None:
            raise self._osc_error
        return list(self._oscs)


class TempOnlyController:
    """A controller that reports temperatures and has no oscillators method at all."""

    def __init__(self, temps: Sequence[Reading]) -> None:
        self._temps = list(temps)

    def temperatures(self) -> Sequence[Reading]:
        return list(self._temps)


class FakeSession(BoardSession):
    def __init__(self, cand: Candidate, identity: BoardIdentity | None = None, *,
                 telemetry: object | None = None, controller: object | None = None) -> None:
        self.candidate = cand
        self._identity = identity or BoardIdentity(board_type="fake", shell_id="0x1")
        self.telemetry = telemetry            # type: ignore[assignment]
        self.controller = controller          # type: ignore[assignment]
        self.closed = 0

    def set_identity(self, identity: BoardIdentity) -> None:
        self._identity = identity

    def identity(self) -> BoardIdentity:
        return self._identity

    def health(self) -> Health:
        return Health(reachable=True, control_channel="idle")

    def close(self) -> None:
        self.closed += 1


@dataclass
class FakePack(BoardPack):
    """A pack whose probe results and open behaviour are set by the test."""

    name: str = "fake"
    title: str = "Fake board"
    found: list[Candidate] = field(default_factory=list)
    probe_error: Exception | None = None
    open_error: Exception | None = None
    identity: BoardIdentity | None = None
    opened: list[FakeSession] = field(default_factory=list)
    probes: int = 0

    def capability_specs(self) -> Iterable[CapabilitySpec]:
        return SPECS

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        self.probes += 1
        if self.probe_error is not None:
            raise self.probe_error
        return list(self.found)

    def open(self, cand: Candidate) -> FakeSession:
        if self.open_error is not None:
            raise self.open_error
        session = FakeSession(cand, self.identity)
        self.opened.append(session)
        return session

    def candidate_for_host(self, spec: str) -> Candidate:
        return candidate(f"{self.name}@{spec}", pack=self.name)


def reading(name: str, value: float | None, source: str, *, age_s: float = 0.0,
            now: float | None = None, unit: str = "degC", reason: str = "") -> Reading:
    at = (now if now is not None else time.time()) - age_s
    return Reading(name=name, value=value, unit=unit, source=source, observed_at=at, reason=reason)
