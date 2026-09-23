"""The lab hub (fpgahub) for the MPS3: leases, TTY shares, and the ``hub://`` serial scheme (lane L1).

Configured per board in boards.toml (``power/config.py`` keeps unknown tables
raw for packs)::

    [boards.lab]
    match = ["192.168.10.101"]
    via = "ssh:mapstone-dev.ecs.soton.ac.uk"
    hub = { host = "mapstone-dev.ecs.soton.ac.uk", target = "mps3_01_pl",
            shares = { mcc = "/dev/mps3_01_pl/tty_00" } }

``hub`` keys: ``host`` (the hub to ssh into; ``"local"`` when the app runs ON
the hub), ``target`` (the fpgahub board name leases and shares use:
``mps3_01_pl``, never the chassis ``mps3_01``; pyverify.lease "THE NAME
AUTHORITY"), ``shares`` (name -> TTY path; ``mcc`` is the board controller,
``fpga_uart0..3`` the FPGA UART lanes), ``baud`` (the rate the shares run at,
default 115200), ``start_shares`` (default false: use a share that is already
running, never start one), ``group`` (the hub socket's group, default ``fpga``).

Every hub command runs through pyverify's lease dialect: ``LeaseClient`` for the
lease verbs and its ``SshHubRunner`` (``ssh HUB 'sg fpga -c "fpgahub …"'``) for
the share verbs. There is deliberately no way to run ``fpgahub share stop``: it
stops EVERY share on the board, including other people's consoles (B0 runbook).

The ``hub://`` scheme. A configured share becomes a ``USB_SERIAL`` link whose
address is ``hub://HOST/TARGET/dev/mps3_01_pl/tty_00`` with ``via="hub"``, so the
MCC adapter (``mcc.make_controller_adapter``) and the FPGA-lane consoles
(``usb.serial_console_endpoints``, which keys on ``if0N`` in the detail) drive it
unchanged. Opening the URL resolves it when it is used: ``fpgahub share list``
finds the share's TCP port, an SSH forward reaches it (the share listens on the
hub, ``0.0.0.0:<port>``), and the result is a ``tcp_serial.TcpSerialPort``.
Resolving late means a share david starts after the board was opened still
works, and one that is missing fails with the exact command to start it.

First writer wins on a share (``tty_share.TtyShareBroker``): only the first
connected client's bytes reach the TTY. Before connecting, the opener reads the
share's client count; if another client is already attached, the port it
returns is read-only and a write raises, instead of a paced MCC command being
dropped without a trace.
"""

from __future__ import annotations

import contextlib
import logging
import re
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.transport import SerialPort, register_serial_scheme
from harness_manager.transports import tcp_serial

from . import tunnel as _tunnel

log = logging.getLogger(__name__)

HUB_SCHEME = "hub"
VIA_HUB = "hub"
LOCAL_HOSTS = ("local", "localhost")
DEFAULT_TARGET = "mps3_01_pl"
DEFAULT_SHARE_BAUD = tcp_serial.DEFAULT_SHARE_BAUD
DEFAULT_GROUP = "fpga"
HUB_TIMEOUT_S = 60.0

#: tty_0N is FT4232H interface 0N (fpgahub's udev naming): 00 = MCC, 01..03 = lanes.
_TTY_IF_RE = re.compile(r"tty_0([0-3])$")
_SHARE_LINE = re.compile(r"share\s+(?P<tty>/\S+)\s+\S+\s+(?P<host>\[[^\]]+\]|[^\s:]+):(?P<port>\d+)")


# --- configuration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HubConfig:
    host: str
    target: str = DEFAULT_TARGET
    shares: dict[str, str] = field(default_factory=dict)     # name -> tty path
    baud: int = DEFAULT_SHARE_BAUD
    start_shares: bool = False
    group: str | None = DEFAULT_GROUP

    @property
    def local(self) -> bool:
        return self.host in LOCAL_HOSTS


def parse_hub_table(table: Any, *, where: str = "hub") -> HubConfig:
    """Validate one boards.toml ``hub`` table. ``UsageError`` names the bad key."""
    if not isinstance(table, dict):
        raise UsageError(f"{where} must be a table: {{ host = ..., target = ... }}")
    unknown = set(table) - {"host", "target", "shares", "baud", "start_shares", "group"}
    if unknown:
        raise UsageError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    host = table.get("host")
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise UsageError(f"{where}.host must be the hub's host name (or \"local\")")
    target = table.get("target", DEFAULT_TARGET)
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]+", target):
        raise UsageError(f"{where}.target must be an fpgahub board name, e.g. mps3_01_pl")
    shares = table.get("shares", {})
    if not isinstance(shares, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.startswith("/dev/")
            for k, v in shares.items()):
        raise UsageError(f"{where}.shares must map names to /dev/... TTY paths")
    baud = table.get("baud", DEFAULT_SHARE_BAUD)
    if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
        raise UsageError(f"{where}.baud must be a positive integer")
    start = table.get("start_shares", False)
    if not isinstance(start, bool):
        raise UsageError(f"{where}.start_shares must be true or false")
    group = table.get("group", DEFAULT_GROUP)
    if group is not None and not isinstance(group, str):
        raise UsageError(f"{where}.group must be a string (empty: no sg wrapper)")
    return HubConfig(host=host, target=target, shares=dict(shares), baud=baud,
                     start_shares=start, group=group or None)


def board_tables(candidate: Candidate) -> dict[str, Any]:
    """This board's raw boards.toml tables (``via``, ``hub``, ...); ``{}`` when none."""
    from harness_manager.power.config import load_boards

    board = load_boards().for_board(candidate.board_id, candidate.links)
    return dict(board.tables) if board is not None else {}


def hub_config_for(candidate: Candidate) -> HubConfig | None:
    """The board's hub, from boards.toml; else from a ``hub://`` link it carries; else None."""
    tables = board_tables(candidate)
    if "hub" in tables:
        return parse_hub_table(tables["hub"], where=f"boards.toml hub for {candidate.board_id}")
    for lk in candidate.links:
        if lk.address.startswith(f"{HUB_SCHEME}://"):
            ref = ShareRef.parse(lk.address.split("://", 1)[1])
            return HubConfig(host=ref.host, target=ref.target)
    return None


def via_for(candidate: Candidate) -> str:
    """boards.toml ``via`` for this board (``"ssh:HOST"``), validated; ``""`` when unset."""
    via = board_tables(candidate).get("via", "")
    if not isinstance(via, str):
        raise UsageError(f"boards.toml via for {candidate.board_id} must be a string: \"ssh:HOST\"")
    _tunnel.parse_via(via)
    return via


# --- share references (the hub:// URL) ---------------------------------------------------------


@dataclass(frozen=True)
class ShareRef:
    host: str
    target: str
    tty: str                   # /dev/mps3_01_pl/tty_00

    @classmethod
    def parse(cls, address: str) -> ShareRef:
        """``HOST/TARGET/dev/...`` (the part after ``hub://``) -> ``ShareRef``."""
        host, _, rest = address.partition("/")
        target, _, tty = rest.partition("/")
        if not host or not target or not tty.startswith("dev/"):
            raise UsageError(f"hub share address {address!r} is not HOST/TARGET/dev/...",
                             hint="e.g. hub://mapstone-dev.ecs.soton.ac.uk/mps3_01_pl/dev/mps3_01_pl/tty_00")
        return cls(host, target, "/" + tty)

    @property
    def url(self) -> str:
        return f"{HUB_SCHEME}://{self.host}/{self.target}{self.tty}"

    @property
    def interface(self) -> int | None:
        m = _TTY_IF_RE.search(self.tty)
        return int(m.group(1)) if m else None


def share_links(cfg: HubConfig) -> list[Link]:
    """One ``USB_SERIAL`` link per configured share, ``via="hub"``; the MCC first.

    The detail of a lane share carries ``if0N`` so ``usb.serial_console_endpoints``
    names it ``fpga_uartN``; the MCC's never does, so ``is_lane_link`` stays False.
    """
    links: list[tuple[int, Link]] = []
    for name, tty in cfg.shares.items():
        ref = ShareRef(cfg.host, cfg.target, tty)
        n = ref.interface
        if name == "mcc" or n == 0:
            detail = f"MCC console over the hub share {tty} on {cfg.host}"
            order = 0
        elif n is not None:
            detail = f"FPGA UART lane {n} (FT4232H if0{n}) over the hub share {tty} on {cfg.host}"
            order = n
        else:
            detail = f"{name} over the hub share {tty} on {cfg.host}"
            order = 9
        tcp_serial.mark_share(ref.url, cfg.baud)
        links.append((order, Link(LinkKind.USB_SERIAL, ref.url, detail, via=VIA_HUB)))
    return [lk for _, lk in sorted(links, key=lambda p: p[0])]


def route_candidate(candidate: Candidate, via: str = "") -> Candidate:
    """The pack hook: apply ``via`` (explicit, else boards.toml) and the hub's share links."""
    if not via and not _tunnel.candidate_via(candidate):
        via = via_for(candidate)
    out = _tunnel.with_via(candidate, via) if via else candidate
    cfg = hub_config_for(out)
    if cfg is not None and cfg.shares:
        from harness_manager.power.config import with_links

        out = with_links(out, share_links(cfg))
    return out


# --- the hub client ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareInfo:
    tty: str
    host: str
    port: int
    writer: str = ""
    readers: int = 0
    running: bool = True

    @property
    def remote_host(self) -> str:
        """Where the HUB connects to reach the share: a wildcard listen is the hub itself."""
        return "127.0.0.1" if self.host in ("0.0.0.0", "::", "[::]", "") else self.host.strip("[]")


def parse_share_list(text: str) -> list[ShareInfo]:
    """``fpgahub share list`` (a rich table: tty, host:port, writer, readers, running)."""
    out: list[ShareInfo] = []
    for line in text.splitlines():
        if "/dev/" not in line:
            continue
        cells = [c.strip() for c in re.split(r"[│┃|]", line) if c.strip()]
        if len(cells) < 2 or not cells[0].startswith("/dev/"):
            continue
        host, sep, port = cells[1].rpartition(":")
        if not sep or not port.isdigit():
            continue
        writer = cells[2] if len(cells) > 2 and cells[2] != "-" else ""
        readers = int(cells[3]) if len(cells) > 3 and cells[3].isdigit() else 0
        running = (cells[4].lower() == "yes") if len(cells) > 4 else True
        out.append(ShareInfo(cells[0], host, int(port), writer, readers, running))
    return out


def parse_share_start(text: str) -> list[ShareInfo]:
    """``fpgahub share start``: one ``share <tty> → <host>:<port>`` line per TTY."""
    return [ShareInfo(m.group("tty"), m.group("host"), int(m.group("port")))
            for m in _SHARE_LINE.finditer(text)]


class _Recorder:
    """Wraps a hub runner: keeps the last reply (pyverify's LeaseClient drops the expiry)
    and refuses ``share stop`` outright, whoever asks."""

    def __init__(self, runner: Callable[..., Any]) -> None:
        self._runner = runner
        self.last: Any = None

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> Any:
        words = list(argv)
        if "share" in words and "stop" in words[words.index("share"):]:
            raise RefusedError("fpgahub share stop stops EVERY share on the board; "
                               "Harness Manager never runs it",
                               hint="close only your own client; david stops shares at close-out")
        self.last = self._runner(words, timeout=timeout)
        return self.last


def default_runner_factory(host: str, group: str | None) -> Callable[..., Any]:
    from pyverify.lease import LocalHubRunner, SshHubRunner

    return LocalHubRunner() if host in LOCAL_HOSTS else SshHubRunner(host, group=group)


#: What the pack uses to reach a hub; tests replace it (tests/fakes/l1_fake_hub.py).
DEFAULT_RUNNER_FACTORY: Callable[[str, str | None], Callable[..., Any]] = default_runner_factory

_EXPIRES = re.compile(r"expires[= ]([^\s),]+)")
_EXPIRES_SHOW = re.compile(r"expires ([^)]+)\)")


@dataclass(frozen=True)
class LeaseView:
    """``fpgahub lease show``: who holds the target, until when (hub time, ISO-8601)."""

    target: str
    held: bool
    holder: str = ""
    user: str = ""
    expires_at: str = ""
    raw: str = ""


class LeaseLostError(HeldError):
    """A heartbeat found the lease gone: ``state`` is ``expired`` (nobody holds it any more)
    or ``lost`` (someone else holds it now, or the token/holder no longer match)."""

    def __init__(self, message: str, *, state: str, hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.state = state


def classify_hub_error(what: str, host: str, target: str, text: str) -> HarnessError:
    """A failed hub command as a ``HarnessError`` with the right exit code and next step."""
    low = text.lower()
    if "no current lease" in low:
        return LeaseLostError(f"{what}: the lease on {target} has expired ({text})", state="expired",
                              hint="acquire it again before touching the board")
    if "does not match" in low:
        return LeaseLostError(f"{what}: the lease on {target} is no longer ours ({text})",
                              state="lost", hint="see who holds it: harness-manager lease show")
    if "permission denied (publickey" in low or "host key verification" in low:
        return UnreachableError(f"{what}: ssh to {host} was refused ({text})",
                                hint=f"check `ssh {host} true` works without a prompt (BatchMode)")
    if "permission denied" in low or "errno 13" in low:
        return UnreachableError(f"{what} on {host}: {text}",
                                hint="your account on the hub needs the 'fpga' group")
    if any(k in low for k in ("could not resolve", "connection refused", "timed out",
                              "no route to host", "cannot run")):
        return UnreachableError(f"{what}: cannot reach the hub {host} ({text})",
                                hint=f"check `ssh {host} true` works, on campus or the VPN")
    if "no such board" in low or "404" in low:
        return AbsentError(f"{what}: the hub {host} has no board {target!r} ({text})",
                           hint="set hub.target in boards.toml (mps3_01_pl on the lab hub)")
    if "409" in low or "held" in low:
        return HeldError(f"{what} on {host}: {text}")
    return UnreachableError(f"{what} on {host} failed: {text}")


class HubClient:
    """fpgahub on one hub for one target: lease verbs (pyverify ``LeaseClient``) and shares."""

    def __init__(self, host: str, target: str = DEFAULT_TARGET, *, group: str | None = DEFAULT_GROUP,
                 runner: Callable[..., Any] | None = None, timeout_s: float = HUB_TIMEOUT_S) -> None:
        from pyverify.lease import LeaseClient

        self.host = host
        self.target = target
        self.timeout_s = timeout_s
        self._run = _Recorder(runner or DEFAULT_RUNNER_FACTORY(host, group))
        self.leases = LeaseClient(self._run, target=target, timeout=timeout_s)

    # -- plumbing -------------------------------------------------------------------------------

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        """Run one hub verb; map pyverify/ssh failures onto exit codes."""
        from pyverify.lease import LeaseError

        try:
            return fn()
        except HarnessError:
            raise
        except TimeoutError as exc:
            raise UnreachableError(f"the hub {self.host} did not answer {what} in time ({exc})",
                                   hint=f"check `ssh {self.host} true` works") from exc
        except LeaseError as exc:
            raise classify_hub_error(what, self.host, self.target, str(exc)) from exc

    def _text(self) -> str:
        last = self._run.last
        return (getattr(last, "stdout", "") or "") + (getattr(last, "stderr", "") or "")

    # -- leases ---------------------------------------------------------------------------------

    def lease_show(self) -> LeaseView:
        st = self._call("lease show", self.leases.status)
        m = _EXPIRES_SHOW.search(st.raw)
        return LeaseView(target=self.target, held=bool(st.held), holder=st.holder or "",
                         user=st.user or "", expires_at=m.group(1).strip() if m else "", raw=st.raw)

    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = 20.0,
                      timeout_s: float = 3600.0, sleep: Callable[[float], None] | None = None,
                      log_fn: Callable[[str], None] | None = None) -> tuple[Any, str]:
        """Acquire, polling while queued (pyverify). Returns ``(Lease, expires_at)``."""
        kwargs: dict[str, Any] = {"ttl": ttl, "poll_s": poll_s, "timeout_s": timeout_s,
                                  "log": log_fn}
        if sleep is not None:
            kwargs["sleep"] = sleep
        lease = self._call("lease acquire", lambda: self.leases.acquire(holder, **kwargs))
        m = _EXPIRES.search(self._text())
        return lease, (m.group(1) if m else "")

    def lease_heartbeat(self, token: str, holder: str) -> str:
        """Extend the lease (the acquire's TTL again). Returns the new ``expires_at``."""
        self._call("lease heartbeat", lambda: self.leases.heartbeat(token, holder))
        m = _EXPIRES.search(self._text())
        return m.group(1) if m else ""

    def lease_release(self, token: str, holder: str) -> None:
        self._call("lease release", lambda: self.leases.release(token, holder))

    def lease_cancel(self, holder: str) -> bool:
        return bool(self._call("lease cancel", lambda: self.leases.cancel(holder)))

    # -- shares (never stop) -------------------------------------------------------------------

    def _share_cmd(self, argv: list[str], what: str) -> str:
        from pyverify.lease import LeaseError

        def run() -> str:
            res = self._run(argv, timeout=self.timeout_s)
            if res.returncode != 0:
                raise LeaseError(f"{what} failed: {res.text.strip() or '(no output)'}")
            return res.text

        return self._call(what, run)

    def share_list(self) -> list[ShareInfo]:
        return parse_share_list(self._share_cmd(["fpgahub", "share", "list", self.target],
                                                "share list"))

    def share_for(self, tty: str) -> ShareInfo | None:
        return next((s for s in self.share_list() if s.tty == tty), None)

    def share_start(self, tty: str, baud: int = DEFAULT_SHARE_BAUD) -> ShareInfo:
        """Start (or get: fpgahub returns an existing share as it is) the share for ``tty``."""
        text = self._share_cmd(["fpgahub", "share", "start", self.target, tty, "--baud", str(baud)],
                               "share start")
        found = [s for s in parse_share_start(text) if s.tty == tty]
        if not found:
            raise UnreachableError(f"share start on {self.host} printed no share for {tty}: "
                                   f"{text.strip()[:200]}")
        return found[0]


def start_share_hint(host: str, target: str, tty: str, baud: int = DEFAULT_SHARE_BAUD) -> str:
    return (f"start it: ssh {host} 'sg fpga -c \"fpgahub share start {target} {tty} --baud {baud}\"'"
            f" (or set start_shares = true in the board's hub table)")


# --- the hub:// opener ---------------------------------------------------------------------------


@dataclass
class _ShareRoute:
    port: int                             # the share's port on the hub
    local_port: int
    tunnel: Any = None                    # SshTunnel, or None on the hub itself


class _ShareRoutes:
    """One SSH forward per share, made the first time the share is opened, kept while
    the board is open (``close_for``), and remade when the share moved port."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._build = threading.Lock()      # one forward per share, even with two openers at once
        self._routes: dict[ShareRef, _ShareRoute] = {}
        self._relays: dict[ShareRef, ShareRelay] = {}
        self._starts: dict[tuple[str, str], bool] = {}     # (host, target) -> start_shares
        self._bauds: dict[tuple[str, str], int] = {}
        self._groups: dict[tuple[str, str], str | None] = {}

    def configure(self, cfg: HubConfig) -> None:
        with self._mu:
            self._starts[(cfg.host, cfg.target)] = cfg.start_shares
            self._bauds[(cfg.host, cfg.target)] = cfg.baud
            self._groups[(cfg.host, cfg.target)] = cfg.group

    def client(self, ref: ShareRef) -> HubClient:
        with self._mu:
            group = self._groups.get((ref.host, ref.target), DEFAULT_GROUP)
        return HubClient(ref.host, ref.target, group=group)

    def route(self, ref: ShareRef, info: ShareInfo) -> _ShareRoute:
        with self._build:
            with self._mu:
                old = self._routes.get(ref)
            if old is not None and old.port == info.port and (
                    old.tunnel is None or old.tunnel.alive() or old.tunnel.state == "starting"):
                return old
            if old is not None and old.tunnel is not None:
                old.tunnel.close()
            if ref.host in LOCAL_HOSTS:
                new = _ShareRoute(info.port, info.port)
            else:
                t = _tunnel.SshTunnel(ref.host,
                                      [_tunnel.Forward("share", info.remote_host, info.port)],
                                      label=f"hub share {ref.tty} on {ref.host}")
                t.start()
                new = _ShareRoute(info.port, t.local_port("share"), t)
            with self._mu:
                self._routes[ref] = new
            return new

    def relay(self, ref: ShareRef) -> ShareRelay:
        with self._mu:
            relay = self._relays.get(ref)
            if relay is None:
                relay = self._relays[ref] = ShareRelay(ref)
            return relay

    def status(self, host: str, target: str) -> dict[str, Any]:
        with self._mu:
            return {ref.tty: {"remote_port": r.port, "local": r.local_port,
                              "state": r.tunnel.state if r.tunnel is not None else "up"}
                    for ref, r in self._routes.items() if (ref.host, ref.target) == (host, target)}

    def close_for(self, host: str, target: str) -> None:
        with self._mu:
            gone = [(ref, r) for ref, r in self._routes.items()
                    if (ref.host, ref.target) == (host, target)]
            for ref, _ in gone:
                del self._routes[ref]
            relays = [(ref, r) for ref, r in self._relays.items()
                      if (ref.host, ref.target) == (host, target)]
            for ref, _ in relays:
                del self._relays[ref]
        for _, relay in relays:
            relay.close()
        for _, r in gone:
            if r.tunnel is not None:
                r.tunnel.close()

    def close_all(self) -> None:
        with self._mu:
            keys = {(ref.host, ref.target) for ref in [*self._routes, *self._relays]}
        for host, target in keys:
            self.close_for(host, target)

    def start_allowed(self, ref: ShareRef) -> bool:
        with self._mu:
            return self._starts.get((ref.host, ref.target), False)

    def baud_for(self, ref: ShareRef) -> int:
        with self._mu:
            return self._bauds.get((ref.host, ref.target), DEFAULT_SHARE_BAUD)


SHARES = _ShareRoutes()


def resolve_share(ref: ShareRef) -> tuple[ShareInfo, _ShareRoute]:
    """Find the share (``share list``; ``share start`` only when allowed) and forward to it."""
    client = SHARES.client(ref)
    info = client.share_for(ref.tty)
    if info is None:
        if not SHARES.start_allowed(ref):
            raise AbsentError(f"no fpgahub share for {ref.tty} on {ref.host} (target {ref.target})",
                              hint=start_share_hint(ref.host, ref.target, ref.tty,
                                                    SHARES.baud_for(ref)))
        info = client.share_start(ref.tty, SHARES.baud_for(ref))
    return info, SHARES.route(ref, info)


def open_hub_share(address: str, baud: int = DEFAULT_SHARE_BAUD) -> SerialPort:
    """The ``hub://`` opener: find the share, forward to it, connect (module docstring)."""
    ref = ShareRef.parse(address)
    share_baud = SHARES.baud_for(ref)
    if baud and baud != share_baud:
        raise UnavailableError(tcp_serial.BAUD_CAPABILITY, tcp_serial.share_baud_reason(share_baud))
    info, route = resolve_share(ref)
    read_only = info.readers > 0
    why = (f"another client ({info.writer or 'unknown'}) holds the hub share's write slot; "
           "fpgahub drops every other client's writes") if read_only else ""
    port = tcp_serial.TcpSerialPort("127.0.0.1", route.local_port, baud=share_baud,
                                    read_only=read_only, read_only_reason=why,
                                    label=f"hub share {ref.tty} on {ref.host}")
    tcp_serial.mark_share(ref.url, share_baud)
    return port


register_serial_scheme(HUB_SCHEME, open_hub_share)


# --- hub shares as consoles --------------------------------------------------------------------


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


class ShareRelay:
    """A local ``tcp://`` endpoint for a hub share, resolved each time a client connects.

    The console broker dials ``tcp://`` console endpoints itself (raw TCP), and
    lane L2 reads a ``tcp://`` console that is not a shell console (uart0, uart1,
    swo) as a hub share whose rate is the URL's ``?baud=`` (``uart.serial_row``).
    So a shared FPGA lane is offered as ``tcp://127.0.0.1:<relay>?baud=<rate>``;
    on each connection the relay finds the share and its forward (``resolve_share``)
    and pipes the bytes. A share started after the board was opened still works; a
    missing one closes the connection, with the reason in ``last_error`` and the log.
    """

    def __init__(self, ref: ShareRef) -> None:
        self.ref = ref
        self.last_error = ""
        self._stop = threading.Event()
        self._mu = threading.Lock()
        self._conns: list[socket.socket] = []
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", _tunnel.free_local_port()))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, name=f"share-relay-{ref.tty}", daemon=True).start()

    def url(self, baud: int) -> str:
        return f"{tcp_serial.TCP_SCHEME}://127.0.0.1:{self.port}?baud={baud}"

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            _info, route = resolve_share(self.ref)
            upstream = socket.create_connection(("127.0.0.1", route.local_port), timeout=10)
            upstream.settimeout(None)
        except (HarnessError, OSError) as exc:
            self.last_error = str(exc)                 # the message and the next step
            log.warning("hub share %s on %s: %s", self.ref.tty, self.ref.host, self.last_error)
            client.close()
            return
        self.last_error = ""
        with self._mu:
            if self._stop.is_set():
                client.close()
                upstream.close()
                return
            self._conns += [client, upstream]
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        _pipe(upstream, client)

    def close(self) -> None:
        self._stop.set()
        # shutdown first: close() alone leaves a socket that another thread is blocked in
        # accept() on still listening (Linux keeps it alive for the syscall).
        with contextlib.suppress(OSError):
            self._srv.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._srv.close()
        with self._mu:
            conns, self._conns = self._conns, []
        for c in conns:
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            c.close()


def relay_share_consoles(endpoints: dict[str, str],
                         candidate: Candidate | None = None) -> dict[str, str]:
    """The pack hook: console endpoints with each ``hub://`` share as a ``tcp://`` relay.

    ``candidate`` gives the hub table (the share's rate, start_shares, group) before the
    session's hub adapter exists.
    """
    if candidate is not None and any(u.startswith(f"{HUB_SCHEME}://") for u in endpoints.values()):
        with contextlib.suppress(UsageError):
            cfg = hub_config_for(candidate)
            if cfg is not None:
                SHARES.configure(cfg)
    out: dict[str, str] = {}
    for name, url in endpoints.items():
        if url.startswith(f"{HUB_SCHEME}://"):
            ref = ShareRef.parse(url.split("://", 1)[1])
            baud = SHARES.baud_for(ref)
            url = SHARES.relay(ref).url(baud)
            tcp_serial.mark_share(url, baud)
        out[name] = url
    return out


# --- the session adapter -------------------------------------------------------------------------


class Mps3Hub:
    """``session.hub``: the board's hub, its target, and a client for both (lane L1)."""

    def __init__(self, cfg: HubConfig, client: HubClient | None = None) -> None:
        self.config = cfg
        self.host = cfg.host
        self.target = cfg.target
        self.client = client or HubClient(cfg.host, cfg.target, group=cfg.group)
        SHARES.configure(cfg)

    def share_status(self) -> dict[str, Any]:
        return SHARES.status(self.host, self.target)

    def close(self) -> None:
        SHARES.close_for(self.host, self.target)


def adapter_for(candidate: Candidate) -> Mps3Hub | None:
    """The hub adapter for a candidate, without opening the board (the CLI's lease verbs)."""
    try:
        cfg = hub_config_for(candidate)
    except UsageError:
        log.warning("the hub table for %s is invalid; no hub for it", candidate.board_id)
        return None
    return Mps3Hub(cfg) if cfg is not None else None


def make_hub_adapter(session: Any) -> Mps3Hub | None:
    """The pack hook. None when the board has no hub (boards.toml ``hub`` or a ``hub://`` link)."""
    cand = getattr(session, "candidate", None)
    return adapter_for(cand) if cand is not None else None


def share_url_for(cfg: HubConfig, name: str) -> str:
    tty = cfg.shares.get(name)
    if tty is None:
        raise AbsentError(f"no share named {name!r} in the hub table",
                          hint=f"configured: {', '.join(sorted(cfg.shares)) or 'none'}")
    return ShareRef(cfg.host, cfg.target, tty).url

