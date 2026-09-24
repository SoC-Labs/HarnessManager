"""The data plane in hub mode: reach the board network through the hub without SSH (team T8).

The lab MPS3's shell (192.168.10.101: 6900 control, 6910 push, 6921 JTAG, 6930-6932
consoles, 2542 XVC) sits on a point-to-point link that only the hub
(``mps3_01_pl``, 192.168.10.1/24) is on. Today every client reaches it through
``ssh -N -L`` (``harness_manager_mps3.tunnel``). An external user has an fpgahub
token and no SSH account, so the only SSH-free path is **routing through the hub**:

1. **A host route on the client**: ``192.168.10.0/24 via <hub's campus address>``
   (or a VPN/WireGuard route that ends on the hub). 192.168.10.x is RFC 1918: it is
   never routed across the internet, so an off-campus user needs a tunnel to the hub
   network first.
2. **The hub forwards** between its campus NIC and the board NIC. fpgahub renders
   the board NIC's networkd file with ``IPForward=no`` (fpgahub ``network.py``
   ``render_networkd``), so this is OFF unless the hub admin turns it on.
3. **The board answers back**: its replies to the client's address must go to the
   hub. The shell has no gateway on that link (a point-to-point /24), so the hub
   has to masquerade (SNAT to 192.168.10.1) or the board needs a default route.
4. **The ethernet gate admits us** (fpgahub ``lease_gates.EthernetGate``,
   ``nftables.py``): with ``access.gate_ethernet = true`` the hub's forward chain
   drops everything on the board NIC except the current lease holder's source IP,
   which is the address the hub saw on the lease **acquire** connection. So the
   lease must be acquired over REST from this machine (an acquire over SSH/the unix
   socket records no source IP), and everyone behind one NAT address shares the
   gate. With the gate off, the chain does not exist and whatever routing the hub
   has decides. Traffic through an SSH tunnel starts ON the hub (OUTPUT, not
   FORWARD), so the gate never applies to it.

The lab hub's config as fpgahub records it (``tests/fixtures/live_shape_config.toml``
in fpgahub 0.3.0) has ``gate_ethernet = false`` for mps3_01_pl, and fpgahub's own
networkd file says ``IPForward=no``: today the direct path does not exist, and the
SSH tunnel is the only data plane. Nothing here can prove the hub side without the
real hub (docs/HUB_MODE.md lists the read-only checks).

``via = "hub"`` therefore **plans** before it connects (``plan_reach``) and uses
the direct route only when every check a client can make passes, in this order:
a specific (non-default) local route to the board, the hub's gate on for the
target, the lease held by this credential, and an ICMP echo from the board through
that route (``ping``: it never opens one of the board's single-client TCP ports).
Otherwise it falls back to the SSH tunnel when the hub table has ``host``, and
fails with the first missing piece when it does not. ``direct = "always"`` skips
the checks (the user asserts the path); ``"never"`` always tunnels.
"""

from __future__ import annotations

import ipaddress
import logging
import platform
import socket
import struct
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import HarnessError, UnreachableError

log = logging.getLogger(__name__)

VIA_HUB = "hub"
PROC_ROUTE = "/proc/net/route"


# --- the local routing table ----------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    network: str               # "192.168.10.0/24"
    gateway: str               # "10.1.2.3", or "" for on-link
    iface: str

    @property
    def prefix_len(self) -> int:
        return ipaddress.ip_network(self.network).prefixlen


def _le_ip(hex_text: str) -> str:
    return socket.inet_ntoa(struct.pack("<I", int(hex_text, 16)))


def parse_proc_route(text: str) -> list[Route]:
    """``/proc/net/route`` (little-endian hex) -> routes that are up (flag RTF_UP)."""
    out: list[Route] = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        iface, dest, gw, flags, mask = cols[0], cols[1], cols[2], cols[3], cols[7]
        try:
            if not int(flags, 16) & 0x1:
                continue
            net = ipaddress.ip_network(f"{_le_ip(dest)}/{_le_ip(mask)}", strict=False)
            gateway = _le_ip(gw)
        except (ValueError, OSError, struct.error):
            continue
        out.append(Route(str(net), "" if gateway == "0.0.0.0" else gateway, iface))
    return out


def best_route(ip: str, routes: list[Route]) -> Route | None:
    """The longest-prefix route for ``ip``, ignoring the default route (prefix 0)."""
    addr = ipaddress.ip_address(ip)
    hits = [r for r in routes if r.prefix_len > 0 and addr in ipaddress.ip_network(r.network)]
    return max(hits, key=lambda r: r.prefix_len) if hits else None


def local_route(ip: str) -> Route | None:
    """This machine's specific route to ``ip`` (Linux ``/proc``); None when none or unknown."""
    try:
        with open(PROC_ROUTE, encoding="ascii") as fh:
            return best_route(ip, parse_proc_route(fh.read()))
    except OSError:
        return None


def ping(ip: str, timeout_s: float = 1.0) -> bool:
    """One ICMP echo through the OS ``ping`` (no board TCP port is touched)."""
    system = platform.system()
    if system == "Windows":
        argv = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), ip]
    elif system == "Darwin":
        argv = ["ping", "-c", "1", "-t", str(max(1, int(timeout_s))), ip]
    else:
        argv = ["ping", "-n", "-c", "1", "-W", str(max(1, int(timeout_s))), ip]
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout_s + 3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- the plan ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReachPlan:
    mode: str                                  # "direct" | "ssh" | "none"
    reason: str
    board_ip: str = ""
    route: Route | None = None
    checks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "reason": self.reason, "board_ip": self.board_ip,
                "route": None if self.route is None else {
                    "network": self.route.network, "gateway": self.route.gateway,
                    "iface": self.route.iface},
                "checks": dict(self.checks)}


def _fallback(reason: str, board_ip: str, ssh_host: str, route: Route | None,
              checks: dict[str, Any]) -> ReachPlan:
    return ReachPlan("ssh" if ssh_host else "none", reason, board_ip, route, checks)


def plan_reach(board_ip: str, *, client: Any = None, direct: str = "auto", ssh_host: str = "",
               route_fn: Callable[[str], Route | None] = local_route,
               probe_fn: Callable[[str], bool] = ping) -> ReachPlan:
    """Decide how to reach ``board_ip``: direct routing through the hub, or the SSH tunnel."""
    checks: dict[str, Any] = {}
    if direct == "never":
        return _fallback("hub.direct is \"never\"", board_ip, ssh_host, None, checks)
    route = route_fn(board_ip)
    checks["route"] = route.network if route else None
    if direct == "always":
        return ReachPlan("direct", "hub.direct is \"always\" (the path is asserted, not checked)",
                         board_ip, route, checks)
    if route is None:
        return _fallback(f"no route to {board_ip} on this machine other than the default one "
                         f"(add one via the hub, e.g. `ip route add "
                         f"{ipaddress.ip_network(board_ip + '/24', strict=False)} via <hub>`)",
                         board_ip, ssh_host, None, checks)
    if client is None:
        return _fallback("no REST hub client to ask about the gate and the lease",
                         board_ip, ssh_host, route, checks)
    try:
        access = (client.target_info() or {}).get("access") or {}
        checks["gate_ethernet"] = bool(access.get("gate_ethernet"))
        if not checks["gate_ethernet"]:
            return _fallback(f"the hub's ethernet gate is off for {client.target}, so the hub "
                             f"does not route lease holders to the board network",
                             board_ip, ssh_host, route, checks)
        me = client.principal()
        status = client.lease_status()
        checks["lease_holder"] = status.holder
        if not status.held or status.holder != me:
            return _fallback(f"the ethernet gate admits only the lease holder's address, and "
                             f"{me or 'this credential'} does not hold {client.target}",
                             board_ip, ssh_host, route, checks)
    except HarnessError as exc:
        checks["error"] = exc.message
        return _fallback(f"cannot ask the hub about the gate: {exc.message}",
                         board_ip, ssh_host, route, checks)
    alive = probe_fn(board_ip)
    checks["probe"] = alive
    if not alive:
        return _fallback(f"{board_ip} does not answer through {route.network} "
                         f"(the hub may not forward: fpgahub sets IPForward=no)",
                         board_ip, ssh_host, route, checks)
    return ReachPlan("direct", f"routed through the hub via {route.gateway or route.iface}; "
                               f"the ethernet gate admits this lease holder",
                     board_ip, route, checks)


# --- the reach -----------------------------------------------------------------------------------------


@dataclass
class DirectReach:
    """How a session reaches its board when the hub routes it: the board's own address.

    Duck-types ``harness_manager_mps3.tunnel.Reach`` (``host``, ``ports``, ``tunnel``,
    ``status``, ``close``) so the pack's ``open`` uses it unchanged.
    """

    host: str
    ports: dict[str, int]
    plan: ReachPlan
    hub: str = ""
    via: str = VIA_HUB
    tunnel: Any = None
    remote_host: str = ""
    closers: list[Callable[[], None]] = field(default_factory=list)

    def port(self, name: str, default: int | None = None) -> int | None:
        return self.ports.get(name, default)

    def status(self) -> dict[str, Any]:
        return {"via": VIA_HUB, "host": self.hub, "state": "up", "mode": "direct",
                "ports": {str(p): p for p in self.ports.values()},
                "detail": self.plan.reason, "plan": self.plan.as_dict()}

    def close(self) -> None:
        for fn in reversed(self.closers):
            try:
                fn()
            except Exception:  # noqa: BLE001 - closing must always finish
                log.exception("closing part of the board's route failed")
        self.closers.clear()


def split_endpoint(address: str, default_port: int) -> tuple[str, int]:
    host, sep, port = address.rpartition(":")
    if sep and port.isdigit() and host:
        return host.strip("[]"), int(port)
    return address.strip("[]"), default_port


def open_hub_reach(board_address: str, remote_ports: Mapping[str, int], *, client: Any = None,
                   direct: str = "auto", ssh_host: str = "",
                   ssh_fallback: Callable[[ReachPlan], Any] | None = None,
                   control_port: int = 6900,
                   route_fn: Callable[[str], Route | None] = local_route,
                   probe_fn: Callable[[str], bool] = ping) -> Any:
    """``via = "hub"``: a ``DirectReach`` when the plan says direct, else ``ssh_fallback(plan)``.

    ``remote_ports`` are the board ports by name (the pack's ``_remote_ports``); the
    control port comes from ``board_address``. Raises ``UnreachableError`` with the
    plan's reason when there is neither a direct path nor an SSH host.
    """
    board_ip, port = split_endpoint(board_address, control_port)
    plan = plan_reach(board_ip, client=client, direct=direct, ssh_host=ssh_host,
                      route_fn=route_fn, probe_fn=probe_fn)
    log.info("hub reach for %s: %s (%s)", board_ip, plan.mode, plan.reason)
    if plan.mode == "direct":
        ports = {**{k: v for k, v in remote_ports.items() if v}, "control": port}
        return DirectReach(board_ip, ports, plan, hub=getattr(client, "host", "") or ssh_host,
                           remote_host=board_ip)
    if plan.mode == "ssh" and ssh_fallback is not None:
        reach = ssh_fallback(plan)
        try:
            reach.hub_plan = plan               # the API's tunnel status can say why
        except AttributeError:
            pass
        return reach
    raise UnreachableError(f"cannot reach {board_ip} through the hub: {plan.reason}",
                           hint="set hub.host for the SSH tunnel fallback, or see "
                                "docs/HUB_MODE.md (the data plane)")
