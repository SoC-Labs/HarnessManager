"""UDP 6899 ``identify``: find MPS3 harnesses, and learn what a board is when its
control port cannot say (held by another client, parked by a swap, or absent
because stage0 is in rescue).

The wire, as agreed with the Linux harness owner (plan §10, 2026-09-23)::

    request  {"op":"identify","v":1,"nonce":"<8-32 hex>"}         unicast or broadcast
    reply    one datagram (<= 1200 B) to the SENDER's addr:port:
             {"ok":true,"op":"identify","v":1,"nonce","board":"mps3","mac","ip","dhcp",
              "shell_id","rm_id","harness","proto","impl"?,"unit"?,"up_ms","os_up_ms"?,
              "mode":"run"|"rescue","reason"?,"ssh"?:{"claimed","host_key_sha256"},
              "ports":{"ctrl":6900,"push":6910,"tftp":69,"jtag":6921,"xvc":2542,
                       "uart0":6930,"uart1":6931,"swo":6932}}

Facts this module relies on:

- identify is a firmware SERVICE MODULE, so both engines answer it once it is
  registered (Linux now; bare-metal from v0.12). ``impl`` absent = bare-metal.
- It is independent of 6900: it answers while another client holds 6900.
- stage0 RESCUE answers it with ``"mode":"rescue"`` (+ ``reason``) and has NO 6900.
- A hung ``mps3-harnessd`` is silent here too (same service loop).
- UDP cannot cross an SSH or hub tunnel: identify works on the board's own
  network only. Over a tunnel, discovery is by explicit address (6900 ping).
- Two boards cannot share one L2 segment until D6 (DNA) gives each a unique
  MAC; ``unit`` appears once D6 exists, and is the stable board key.

PYVERIFY REQUEST (one-codec rule): pyverify's HOST lane is adding a unicast
``pyverify.identify.identify()``. It has no broadcast ``discover()``, and its
unicast socket is unconnected (a closed loopback port costs the full timeout).
harness-manager keeps this client until pyverify ships both, then delegates to it.

Test seams (environment, read on every call):

- ``HARNESS_MANAGER_MPS3_IDENTIFY_PORT``: the UDP port to probe (default 6899), so a
  test can reach a fake responder on an ephemeral port;
- ``HARNESS_MANAGER_MPS3_IDENTIFY_BROADCAST``: comma-separated ``addr[:port]``
  discovery targets. It REPLACES the default list (``255.255.255.255`` plus the
  fixed ``192.168.10.101``), so a test can "broadcast" to loopback responders
  and nothing else.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import UnreachableError, UsageError
from harness_manager.core.model import BoardIdentity, Candidate, Check, Link, LinkKind
from harness_manager.core.pack import ProbeHints

from .capabilities import HARNESS_STATES
from .constants import CONTROL_PORT, DEFAULT_SHELL_HOST, IDENTIFY_PORT, IMPL_BARE_METAL

IDENTIFY_PORT_ENV = "HARNESS_MANAGER_MPS3_IDENTIFY_PORT"
IDENTIFY_BROADCAST_ENV = "HARNESS_MANAGER_MPS3_IDENTIFY_BROADCAST"

#: The reply limit the harness promises (plan §10). A bigger datagram is not ours.
MAX_REPLY_BYTES = 1200

MODE_RUN = "run"
MODE_RESCUE = "rescue"

_HEX = frozenset("0123456789abcdef")


# --- configuration -------------------------------------------------------------------


def identify_port() -> int:
    """The UDP port to probe: ``$HARNESS_MANAGER_MPS3_IDENTIFY_PORT`` or 6899."""
    raw = os.environ.get(IDENTIFY_PORT_ENV, "").strip()
    if not raw:
        return IDENTIFY_PORT
    try:
        port = int(raw, 10)
    except ValueError as exc:
        raise UsageError(f"${IDENTIFY_PORT_ENV}={raw!r} is not a port number") from exc
    if not 0 < port < 65536:
        raise UsageError(f"${IDENTIFY_PORT_ENV}={port} is out of range")
    return port


def broadcast_targets() -> list[tuple[str, int]]:
    """Where a discovery probe goes: ``$HARNESS_MANAGER_MPS3_IDENTIFY_BROADCAST`` or the
    limited broadcast address, each on the identify port unless one is given."""
    port = identify_port()
    raw = os.environ.get(IDENTIFY_BROADCAST_ENV, "").strip()
    if not raw:
        return [("255.255.255.255", port)]
    out: list[tuple[str, int]] = []
    for item in (s.strip() for s in raw.split(",")):
        if not item:
            continue
        host, _, p = item.partition(":")
        out.append((host, int(p) if p else port))
    return out


def is_loopback(host: str) -> bool:
    """True for a loopback address or ``localhost``: a test fake or a tunnel end."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --- the wire ------------------------------------------------------------------------


def new_nonce() -> str:
    """16 lowercase hex characters (the request accepts 8-32)."""
    return secrets.token_hex(8)


def encode_request(nonce: str) -> bytes:
    if not 8 <= len(nonce) <= 32 or not set(nonce.lower()) <= _HEX:
        raise UsageError(f"identify nonce {nonce!r} must be 8-32 hex characters")
    return json.dumps({"op": "identify", "v": 1, "nonce": nonce},
                      separators=(",", ":")).encode("ascii")


def _hex_id(value: Any) -> str:
    """``"0x3F1A560F"`` / ``1058690575`` -> ``"0x3f1a560f"``; anything else -> ``""``."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"0x{value & 0xFFFFFFFF:08x}"
    if isinstance(value, str) and value:
        try:
            return f"0x{int(value, 0) & 0xFFFFFFFF:08x}"
        except ValueError:
            return value
    return ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class IdentifyReply:
    """One board's answer. ``raw`` is the whole reply; the properties read it tolerantly."""

    raw: dict[str, Any]
    source: tuple[str, int]              # who answered (addr, port): the routable address
    probe_port: int = IDENTIFY_PORT
    received_at: float = field(default_factory=time.time, compare=False)

    @property
    def ok(self) -> bool:
        return self.raw.get("ok") is True

    @property
    def mode(self) -> str:
        mode = self.raw.get("mode")
        return mode if isinstance(mode, str) and mode else MODE_RUN

    @property
    def is_rescue(self) -> bool:
        return self.mode == MODE_RESCUE

    @property
    def impl(self) -> str:
        """``"linux"``, else ``"bare-metal"`` (the key is additive); ``""`` in rescue,
        where stage0, not a harness, is answering."""
        if self.is_rescue:
            return ""
        impl = self.raw.get("impl")
        return impl if isinstance(impl, str) and impl else IMPL_BARE_METAL

    @property
    def address(self) -> str:
        return self.source[0]

    @property
    def ip(self) -> str:
        ip = self.raw.get("ip")
        return ip if isinstance(ip, str) else ""

    @property
    def unit(self) -> str:
        unit = self.raw.get("unit")
        return unit if isinstance(unit, str) else ""

    @property
    def name(self) -> str:
        """The board's own name (the proposed ``name`` key, N1); ``""`` in rescue or absent."""
        from harness_manager.naming import clean_name

        return "" if self.is_rescue else clean_name(self.raw.get("name"))

    @property
    def shell_id(self) -> str:
        return _hex_id(self.raw.get("shell_id"))

    @property
    def rm_id(self) -> str:
        return _hex_id(self.raw.get("rm_id"))

    @property
    def harness(self) -> str:
        h = self.raw.get("harness")
        return h if isinstance(h, str) and not self.is_rescue else ""

    @property
    def proto(self) -> str:
        p = self.raw.get("proto")
        return str(p) if isinstance(p, (str, int, float)) and not isinstance(p, bool) else ""

    @property
    def reason(self) -> str:
        r = self.raw.get("reason")
        return r if isinstance(r, str) else ""

    @property
    def up_ms(self) -> int | None:
        return _int(self.raw.get("up_ms"))

    @property
    def os_up_ms(self) -> int | None:
        """The OS's uptime (Linux only; absent on bare-metal). ``up_ms`` is the harness
        PROCESS's, so ``up_ms << os_up_ms`` means harnessd respawned."""
        return _int(self.raw.get("os_up_ms"))

    @property
    def dhcp(self) -> bool | None:
        d = self.raw.get("dhcp")
        return d if isinstance(d, bool) else None

    @property
    def ssh(self) -> dict[str, Any]:
        s = self.raw.get("ssh")
        return s if isinstance(s, dict) else {}

    @property
    def ports(self) -> dict[str, int]:
        p = self.raw.get("ports")
        if not isinstance(p, dict):
            return {}
        return {k: v for k, v in p.items() if _int(v) is not None}

    @property
    def control_port(self) -> int:
        return self.ports.get("ctrl", CONTROL_PORT)

    @property
    def control_endpoint(self) -> str:
        return f"{self.address}:{self.control_port}"

    @property
    def board_id(self) -> str:
        """Stable: the D6 ``unit`` when the board reports one, else ``mps3@<ip>:<ctrl>``."""
        return f"mps3@{self.unit}" if self.unit else f"mps3@{self.control_endpoint}"

    # -- conversions into the core model -----------------------------------------------

    def identity(self) -> BoardIdentity:
        from .shell import resolve_rm_name

        rm = self.rm_id
        return BoardIdentity(
            board_type="mps3",
            shell_id=self.shell_id,
            rm_id=rm,
            rm_name=resolve_rm_name(rm) if rm and not self.is_rescue else "",
            harness_version=self.harness,
            build_check=Check.UNCHECKED,     # identify carries no firmware/fabric verdict
            unit_id=self.unit,
            harness_impl=self.impl,
            proto=self.proto,
            name=self.name,
        )

    def describe(self) -> str:
        """One line of evidence: what answered, and how."""
        if self.is_rescue:
            text = (f"answered identify on UDP {self.probe_port} in RESCUE mode: "
                    f"{HARNESS_STATES['harness.rescue']}")
            return text + (f" (reason: {self.reason})" if self.reason else "")
        bits = [self.impl]
        if self.up_ms is not None:
            bits.append(f"harness up {self.up_ms / 1000:.0f} s")
        if self.dhcp:
            bits.append("DHCP")
        if self.ssh:
            bits.append("SSH claimed" if self.ssh.get("claimed") else "SSH unclaimed")
        if self.ip and self.ip != self.address:
            bits.append(f"reports ip {self.ip}")
        return f"answered identify on UDP {self.probe_port} ({', '.join(bits)})"

    def candidate(self) -> Candidate:
        if self.is_rescue:
            link = Link(LinkKind.ETHERNET, self.control_endpoint,
                        "stage0 rescue: TFTP and identify only, no control channel")
            label = f"MPS3 in RESCUE at {self.address}"
        else:
            link = Link(LinkKind.ETHERNET, self.control_endpoint,
                        "shell control channel (found by identify)")
            ident = self.identity()
            label = (f"MPS3 {ident.rm_name or ident.rm_id} on shell {ident.shell_id}"
                     f" ({self.impl})")
        return Candidate(pack="mps3", board_id=self.board_id, links=(link,), label=label,
                         evidence=self.describe(), identity=self.identity())


def parse_reply(data: bytes, nonce: str, source: tuple[str, int],
                probe_port: int = IDENTIFY_PORT) -> IdentifyReply | None:
    """The reply as an ``IdentifyReply``, or None when it does not answer THIS request
    (malformed, oversized, another op, or someone else's nonce)."""
    if len(data) > MAX_REPLY_BYTES:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("op") != "identify":
        return None
    if str(obj.get("nonce", "")).lower() != nonce.lower():
        return None
    return IdentifyReply(raw=obj, source=(source[0], source[1]), probe_port=probe_port)


# --- the client ----------------------------------------------------------------------


def identify(host: str, port: int | None = None, *, timeout: float = 1.0,
             retries: int = 1, nonce: str | None = None) -> IdentifyReply:
    """Unicast one identify probe (re-sent ``retries`` times: it is UDP).

    Raises ``UnreachableError`` when nothing answers. A loopback target (a test
    fake, or a tunnel end, where no source-address rewrite can happen) uses a
    connected socket, so a closed port fails at once instead of timing out.
    """
    port = identify_port() if port is None else port
    nonce = nonce or new_nonce()
    request = encode_request(nonce)
    loopback = is_loopback(host)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        raise UnreachableError(f"cannot open a UDP socket for identify: {exc}") from exc
    with sock:
        sock.settimeout(timeout)
        try:
            if loopback:
                sock.connect((host, port))
            for _ in range(max(1, retries + 1)):
                if loopback:
                    sock.send(request)
                else:
                    sock.sendto(request, (host, port))
                deadline = time.monotonic() + timeout
                while (left := deadline - time.monotonic()) > 0:
                    sock.settimeout(left)
                    try:
                        data, src = sock.recvfrom(4096)
                    except TimeoutError:
                        break
                    reply = parse_reply(data, nonce, src, port)
                    if reply is not None:
                        return reply
        except ConnectionRefusedError as exc:
            raise UnreachableError(f"nothing answers identify at {host}:{port} "
                                   "(port closed)") from exc
        except OSError as exc:
            raise UnreachableError(f"identify {host}:{port} failed: {exc}") from exc
    raise UnreachableError(f"nothing answered identify at {host}:{port} within "
                           f"{timeout:.1f}s x{max(1, retries + 1)}",
                           hint="identify is UDP: it does not cross SSH or hub tunnels")


def discover(targets: Sequence[tuple[str, int]] | None = None, *, timeout: float = 2.0,
             nonce: str | None = None) -> list[IdentifyReply]:
    """Send one probe to every target (broadcast addresses included) and collect every
    matching reply that arrives within ``timeout``. One reply per board: the first
    from each (unit, or answering address)."""
    targets = list(targets) if targets is not None else broadcast_targets()
    nonce = nonce or new_nonce()
    request = encode_request(nonce)
    replies: dict[str, IdentifyReply] = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    with sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        for host, port in targets:
            try:
                sock.sendto(request, (host, port))
            except OSError:
                continue            # no route to that network: try the others
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, src = sock.recvfrom(4096)
            except TimeoutError:
                break
            except (ConnectionResetError, ConnectionRefusedError):
                continue            # Windows reports an earlier ICMP port-unreachable here
            except OSError:
                break
            probe_port = next((p for h, p in targets if h == src[0]), identify_port())
            reply = parse_reply(data, nonce, src, probe_port)
            if reply is None:
                continue
            key = reply.unit or reply.address + f":{reply.control_port}"
            replies.setdefault(key, reply)
    return list(replies.values())


# --- the pack hook -------------------------------------------------------------------


def probe_identify(hints: ProbeHints, found: Sequence[Candidate]) -> list[Candidate]:
    """``pack.py``'s T12 hook: boards found by UDP identify, as Candidates.

    - With explicit ``hints.hosts``: unicast to each host that did NOT already
      answer the 6900 ping (a board in rescue, or with its harness down, is
      found this way); nothing is broadcast.
    - Without: broadcast (``broadcast_targets()``) plus the fixed ``.101``, which
      stays a permanent secondary address under DHCP and is stage0 rescue's address.

    A board already in ``found`` (same control endpoint or board_id) is skipped.
    Rescue boards are included; their ``evidence`` says so.
    """
    from .shell import parse_endpoint

    port = identify_port()
    known_addrs = {lk.address for c in found for lk in c.links if lk.kind == LinkKind.ETHERNET}
    known_ids = {c.board_id for c in found}
    timeout = max(0.1, hints.timeout_s)
    replies: list[IdentifyReply] = []
    if hints.hosts:
        for spec in hints.hosts:
            try:
                host, ctrl = parse_endpoint(spec, CONTROL_PORT)
            except ValueError:
                continue
            if f"{host}:{ctrl}" in known_addrs:
                continue
            try:
                replies.append(identify(host, port, timeout=min(timeout, 2.0), retries=1))
            except (UnreachableError, UsageError):
                continue
    else:
        targets = broadcast_targets()
        if not os.environ.get(IDENTIFY_BROADCAST_ENV, "").strip():
            # The default target list: the limited broadcast plus the fixed .101. An
            # explicit $HARNESS_MANAGER_MPS3_IDENTIFY_BROADCAST replaces both.
            targets.append((DEFAULT_SHELL_HOST, port))
        replies = discover(targets, timeout=timeout)

    out: list[Candidate] = []
    for reply in replies:
        if not reply.ok:
            continue
        cand = reply.candidate()
        addr = cand.links[0].address
        if cand.board_id in known_ids or addr in known_addrs:
            continue
        known_ids.add(cand.board_id)
        known_addrs.add(addr)
        out.append(cand)
    return out
