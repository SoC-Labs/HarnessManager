"""Capability negotiation.

A capability is available only when every link and every harness feature it
needs is present. The app never guesses: it asks ``negotiate`` and, for each
missing capability, shows ``reason`` ("needs the Debug USB cable",
"needs harness firmware with 'stats'").

Board packs declare their capabilities with ``CapabilitySpec``. The names
below are the shared vocabulary; packs may add their own (prefix with the
pack name, e.g. ``mps3.display_flip``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .model import LinkKind

# Shared capability names (append-only).
IDENTIFY = "identify"
HEALTH = "health"
DEPLOY_PARTIAL = "deploy_partial"
CONSOLE_DUT = "console_dut"
CONSOLE_SHELL = "console_shell"
CONSOLE_CONTROLLER = "console_controller"
DEBUG_DUT = "debug_dut"
DEBUG_FABRIC = "debug_fabric"
RESET_DUT = "reset_dut"
RESET_SHELL = "reset_shell"
REBOOT_BOARD = "reboot_board"
CLOCK_DUT = "clock_dut"
CLOCK_BOARD = "clock_board"
TELEMETRY_TEMP = "telemetry_temp"
TELEMETRY_POWER = "telemetry_power"
STORAGE_BACKUP = "storage_backup"
STORAGE_INSTALL = "storage_install"
DISCOVER_NETWORK = "discover_network"
POWER_CYCLE = "power_cycle"


@dataclass(frozen=True)
class Route:
    """One way to provide a capability: every link AND every harness feature listed."""

    links: frozenset[LinkKind]
    features: frozenset[str] = frozenset()

    def describe(self) -> str:
        parts = [k.value for k in sorted(self.links, key=lambda k: k.value)]
        if self.features:
            parts.append("harness firmware with " + ", ".join(f"'{f}'" for f in sorted(self.features)))
        return " + ".join(parts) if parts else "nothing"


def via(*links: LinkKind, features: Iterable[str] = ()) -> Route:
    return Route(frozenset(links), frozenset(features))


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    title: str                                  # what the UI calls it
    routes: tuple[Route, ...] = ()              # alternatives; the first satisfied one wins
    needs_hint: str = ""                        # human hint when no route is satisfied


def satisfied_route(spec: CapabilitySpec, links: frozenset[LinkKind],
                    features: frozenset[str]) -> Route | None:
    if not spec.routes:
        return Route(frozenset())
    for route in spec.routes:
        if route.links <= links and route.features <= features:
            return route
    return None


def negotiate(
    specs: Iterable[CapabilitySpec],
    links: Iterable[LinkKind],
    features: Iterable[str],
) -> tuple[frozenset[str], dict[str, str]]:
    """Return (available capability names, {unavailable name: reason})."""
    link_set = frozenset(links)
    feature_set = frozenset(features)
    available: set[str] = set()
    unavailable: dict[str, str] = {}
    for spec in specs:
        if satisfied_route(spec, link_set, feature_set) is not None:
            available.add(spec.name)
        else:
            unavailable[spec.name] = spec.needs_hint or _describe(spec, link_set)
    return frozenset(available), unavailable


def _describe(spec: CapabilitySpec, links: frozenset[LinkKind]) -> str:
    # Prefer explaining the route whose links are already present (only features missing).
    for route in spec.routes:
        if route.links <= links and route.features:
            return "needs harness firmware with " + ", ".join(
                f"'{f}'" for f in sorted(route.features))
    return "needs " + " or ".join(r.describe() for r in spec.routes)
