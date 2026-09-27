"""The lab hub (fpgahub) for the MPS3: leases, TTY shares, and the ``hub://`` serial scheme (lane L1).

Configured per board in boards.toml (``power/config.py`` keeps unknown tables
raw for packs)::

    [boards.lab]
    match = ["192.168.10.101"]
    via = "ssh:mapstone-dev.ecs.soton.ac.uk"
    hub = { host = "mapstone-dev.ecs.soton.ac.uk", target = "mps3_01_pl",
            shares = { fpga_uart2 = "/dev/mps3_01_pl/tty_02" } }

``hub`` keys: ``host`` (the hub to ssh into; ``"local"`` when the app runs ON
the hub), ``url`` (fpgahub's REST API instead of ssh, with ``token_file``,
``ca_file``, ... : ``harness_manager.transports.hub_rest``; with both, REST wins
and ``host`` is only the SSH fallback of the data plane), ``target`` (the fpgahub board name leases and shares use:
``mps3_01_pl``, never the chassis ``mps3_01``; pyverify.lease "THE NAME
AUTHORITY"), ``shares`` (name -> TTY path: the FPGA UART lanes ``fpga_uart0..3``;
``mcc``/tty_00 only names the MCC's path, never shared), ``baud`` (the rate the shares run at,
default 115200), ``start_shares`` (default false: use a share that is already
running, never start one), ``group`` (the hub socket's group, default ``fpga``),
``board`` (the physical board that owns the target, for ``fpgahub board lease
revoke``; unset, fpgahub's ``board list --json`` says: ``mps3_01`` for ``mps3_01_pl``).

A **named hub** (CCR SET-HUB-1, lane SET-HUBS): ``hub = { use = "lab", target = …,
shares = … }`` takes the hub's own keys from ``[hubs.lab]`` in ``settings.toml`` (or the
admin policy's machine hub), plus ``jump`` (an SSH jump host on the way to the hub),
``holder`` and the lease times (``harness_manager.settings.hubs``). Beside ``use`` the
board keeps only ``target``, ``board``, ``shares``, ``baud``, ``start_shares``; a hub key
there is refused, naming it. An inline table (above) keeps working exactly as before.

Every hub command runs through pyverify's lease dialect: ``LeaseClient`` for the
lease verbs and its ``SshHubRunner`` (``ssh HUB 'sg fpga -c "fpgahub …"'``) for
the share verbs. There is deliberately no way to run ``fpgahub share stop``: it
stops EVERY share on the board, including other people's consoles (B0 runbook).

**Never a share on tty_00** (MCC-FIX, the Linux lead and the lead, 2026-09-26). The paced
MCC REBOOT works only with exactly one reader on ``tty_00``, a share is a reader that cannot
be stopped on its own, and while one exists the platform's tools refuse to REBOOT. So a
``shares.mcc`` entry (or any ``…/tty_00``) makes no link, ``resolve_share`` and
``HubClient.share_start`` refuse it, and the MCC of a hub board is ``hub_mcc``'s: pyverify's
tools, run ON the hub. Shares stay for the FPGA UART lanes (``tty_01..03``).

The ``hub://`` scheme. A configured lane share becomes a ``USB_SERIAL`` link whose
address is ``hub://HOST/TARGET/dev/mps3_01_pl/tty_02`` with ``via="hub"``, so the
FPGA-lane consoles (``usb.serial_console_endpoints``, which keys on ``if0N`` in the
detail) drive it unchanged. Opening the URL resolves it when it is used: ``fpgahub share list``
finds the share's TCP port, an SSH forward reaches it (the share listens on the
hub, ``0.0.0.0:<port>``), and the result is a ``tcp_serial.TcpSerialPort``.
Resolving late means a share david starts after the board was opened still
works, and one that is missing fails with the exact command to start it.

First writer wins on a share (``tty_share.TtyShareBroker``): only the first
connected client's bytes reach the TTY. Before connecting, the opener reads the
share's client count; if another client is already attached, the port it
returns is read-only and a write raises, instead of a paced MCC command being
dropped without a trace. The count lags our own closes: the hub drops a client
only when it reads the EOF, a few ms (a round trip through the forward) after
we close. So while every counted client could be one of our own connections
closed in the last few seconds, the opener re-lists (``SLOT_POLL_S``, for up to
``SLOT_WAIT_S``) until the hub has let go, rather than refuse itself the slot.
Anyone else attached still makes the port read-only at once. A shared console
(``ShareRelay``) settles the same way before it connects, so the first keys typed
after a quick reconnect reach the TTY instead of being dropped by the hub.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
#: How long one of our own share connections may still be counted after we closed it
#: (the hub reads the EOF through the ssh forward), how long the opener waits for the
#: hub to let go of the write slot, and how often it re-lists while it waits.
OWN_LINGER_S = 3.0
SLOT_WAIT_S = 2.0
SLOT_POLL_S = 0.1
#: How long a relayed console may keep sending after the share ended its side.
RELAY_DRAIN_S = 5.0

#: tty_0N is FT4232H interface 0N (fpgahub's udev naming): 00 = MCC, 01..03 = lanes. The one
#: rule for it, on normalised paths, is ``transports.tcp_serial`` (REVIEW-W5 10).
_SHARE_LINE = re.compile(r"share\s+(?P<tty>/\S+)\s+\S+\s+(?P<host>\[[^\]]+\]|[^\s:]+):(?P<port>\d+)")


# --- configuration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HubConfig:
    host: str = ""                     # the SSH hub; with ``rest`` alone, the URL's host name
    target: str = DEFAULT_TARGET
    shares: dict[str, str] = field(default_factory=dict)     # name -> tty path
    baud: int = DEFAULT_SHARE_BAUD
    start_shares: bool = False
    group: str | None = DEFAULT_GROUP
    board: str = ""                    # the physical board (fpgahub chassis); "" = ask the hub
    rest: Any = None                   # hub_rest.RestHubConfig when the table has ``url`` (T8)
    # CCR SET-HUB-1: a named hub's (``hub.use``); an inline table leaves them unset.
    name: str = ""                     # the hub's name ("" = an inline table)
    jump: str = ""                     # ssh -J on the way to the hub
    holder: str = ""                   # the lease holder asked for ("" = default_holder())
    lease_ttl: int = 0                 # the lease times asked for (0 = the service's default)
    request_ttl: int = 0
    queue_timeout: int = 0
    stage_dir: str = ""                # hubs.<name>.stage_dir ("" = hub_sd.STAGE_DIR; MCC-FIX)

    @property
    def local(self) -> bool:
        return self.host in LOCAL_HOSTS and self.rest is None

    @property
    def transport(self) -> str:
        """``"rest"`` (fpgahub's API with a token) or ``"ssh"`` (``sg fpga -c fpgahub``)."""
        return "rest" if self.rest is not None else "ssh"


def parse_hub_table(table: Any, *, where: str = "hub") -> HubConfig:
    """Validate one boards.toml ``hub`` table. ``UsageError`` names the bad key."""
    from harness_manager.transports import hub_rest

    if not isinstance(table, dict):
        raise UsageError(f"{where} must be a table: {{ host = ..., target = ... }}")
    unknown = set(table) - {"host", "target", "shares", "baud", "start_shares", "group", "board"} \
        - hub_rest.REST_KEYS
    if unknown:
        raise UsageError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    rest = hub_rest.parse_rest_table(table, where=where)
    host = table.get("host") or (rest.host if rest is not None else None)
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise UsageError(f"{where}.host must be the hub's host name (or \"local\"), "
                         f"or {where}.url its REST API (https://HUB:7246)")
    target = table.get("target", DEFAULT_TARGET)
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]+", target):
        raise UsageError(f"{where}.target must be an fpgahub board name, e.g. mps3_01_pl")
    shares = table.get("shares", {})
    if not isinstance(shares, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.startswith("/dev/")
            for k, v in shares.items()):
        raise UsageError(f"{where}.shares must map names to /dev/... TTY paths")
    # One spelling for every check and every hub call (REVIEW-W5 10): ``…/tty_00/`` is tty_00.
    shares = {k: tcp_serial.norm_tty(v) for k, v in shares.items()}
    baud = table.get("baud", DEFAULT_SHARE_BAUD)
    if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
        raise UsageError(f"{where}.baud must be a positive integer")
    start = table.get("start_shares", False)
    if not isinstance(start, bool):
        raise UsageError(f"{where}.start_shares must be true or false")
    group = table.get("group", DEFAULT_GROUP)
    if group is not None and not isinstance(group, str):
        raise UsageError(f"{where}.group must be a string (empty: no sg wrapper)")
    board = table.get("board", "")
    if not isinstance(board, str) or (board and not valid_name(board)):
        raise UsageError(f"{where}.board must be an fpgahub board id, e.g. mps3_01")
    return HubConfig(host=host, target=target, shares=dict(shares), baud=baud,
                     start_shares=start, group=group or None, board=board,
                     rest=hub_rest.with_target(rest, target) if rest is not None else None)


def parse_board_hub(table: Any, *, where: str = "hub", root: Any = None) -> HubConfig:
    """A boards.toml ``hub`` table: inline (``parse_hub_table``, unchanged), or naming a hub
    with ``use`` (CCR SET-HUB-1): the named hub's keys, the board's own, and the hub's jump,
    holder and lease times. ``root``: the directory of that boards.toml (its settings.toml
    and secret store are beside it). ``UsageError`` names the problem."""
    if not (isinstance(table, dict) and "use" in table):
        return parse_hub_table(table, where=where)
    from dataclasses import replace

    from harness_manager.settings import hubs as named

    merged, hub = named.board_hub_table(table, where=where, root=root)
    cfg = parse_hub_table(merged, where=where)
    rest = cfg.rest
    if rest is not None:
        rest = replace(rest, hub_name=hub.name, settings_root=str(root) if root else "")
    return replace(cfg, rest=rest, name=hub.name, jump=hub.jump if hub.transport == "ssh" else "",
                   holder=hub.holder, lease_ttl=hub.lease_ttl, request_ttl=hub.request_ttl,
                   queue_timeout=hub.queue_timeout,
                   stage_dir=hub.stage_dir if hub.transport == "ssh" else "")


def _board_config(candidate: Candidate) -> Any:
    from harness_manager.power.config import load_boards

    return load_boards().for_board(candidate.board_id, candidate.links)


def board_tables(candidate: Candidate) -> dict[str, Any]:
    """This board's raw boards.toml tables (``via``, ``hub``, ...); ``{}`` when none."""
    board = _board_config(candidate)
    return dict(board.tables) if board is not None else {}


def hub_config_for(candidate: Candidate) -> HubConfig | None:
    """The board's hub, from boards.toml; else from a ``hub://`` link it carries; else None."""
    board = _board_config(candidate)
    tables = dict(board.tables) if board is not None else {}
    if "hub" in tables:
        root = board.path.parent if getattr(board, "path", None) is not None else None
        return parse_board_hub(tables["hub"], where=f"boards.toml hub for {candidate.board_id}",
                               root=root)
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
        return tcp_serial.tty_interface(self.tty)


def is_mcc_share(name: str, tty: str) -> bool:
    """The MCC console: the ``mcc`` share name, or FT4232H interface 00 in any spelling
    (``…/tty_00``, ``…/tty_00/``, its by-id alias: ``tcp_serial.mcc_tty_reason``)."""
    return name == "mcc" or tcp_serial.is_mcc_tty(tty)


def refuse_mcc_share(tty: str, host: str = "") -> RefusedError:
    why = tcp_serial.mcc_tty_reason(tty)
    return RefusedError(
        f"Harness Manager never starts or uses an fpgahub share on the MCC console {tty}"
        + (f" on {host}" if host else "") + (f" ({why})" if why and "by-id" in why else "")
        + ": the paced REBOOT needs exactly one reader on "
        "tty_00, and a share is one that cannot be stopped on its own",
        hint="the MCC of a hub board runs on the hub through pyverify (`harness-manager mcc "
             "TARGET temp|reboot`); shares are for the FPGA UART lanes tty_01..03")


def share_links(cfg: HubConfig) -> list[Link]:
    """One ``USB_SERIAL`` link per configured lane share, ``via="hub"``; never the MCC's.

    The detail of a lane share carries ``if0N`` so ``usb.serial_console_endpoints``
    names it ``fpga_uartN``. A ``mcc`` / ``tty_00`` entry makes no link (MCC-FIX).
    """
    links: list[tuple[int, Link]] = []
    for name, tty in cfg.shares.items():
        ref = ShareRef(cfg.host, cfg.target, tty)
        n = ref.interface
        if is_mcc_share(name, tty):
            continue             # MCC-FIX: never a tty_00 share; hub_mcc runs the MCC on the hub
        if n is not None:
            detail = f"FPGA UART lane {n} (FT4232H if0{n}) over the hub share {tty} on {cfg.host}"
            order = n
        else:
            detail = f"{name} over the hub share {tty} on {cfg.host}"
            order = 9
        tcp_serial.mark_share(ref.url, cfg.baud)
        links.append((order, Link(LinkKind.USB_SERIAL, ref.url, detail, via=VIA_HUB)))
    return [lk for _, lk in sorted(links, key=lambda p: p[0])]


MCC_ON_HUB_SCHEME = "hub-mcc"


def mcc_link(cfg: HubConfig) -> Link | None:
    """The board controller ON the hub (MCC-FIX): a ``HUB`` link, never a share. It carries
    the capabilities the MCC gives (reboot, oscillators, temperature, the controller console)
    when the hub has an SSH login to run pyverify's tools with; a REST-only hub has none."""
    from .hub_mcc import mcc_tty_for

    rest = cfg.rest
    host = (getattr(rest, "ssh_host", "") or "") if rest is not None else cfg.host
    if not host:
        return None
    tty = mcc_tty_for(cfg)
    return Link(LinkKind.HUB, f"{MCC_ON_HUB_SCHEME}://{host}/{cfg.target}{tty}",
                f"the MCC console {tty}, reached ON the hub {host} (pyverify's tools run "
                "there; never an fpgahub share)", via=VIA_HUB)


def route_candidate(candidate: Candidate, via: str = "") -> Candidate:
    """The pack hook: apply ``via`` (explicit, else boards.toml), the MCC on the hub and the
    hub's lane share links."""
    if not via and not _tunnel.candidate_via(candidate):
        via = via_for(candidate)
    out = _tunnel.with_via(candidate, via) if via else candidate
    cfg = hub_config_for(out)
    if cfg is not None:
        from harness_manager.power.config import with_links

        mcc = mcc_link(cfg)
        extra = ([mcc] if mcc is not None else []) + (share_links(cfg) if cfg.shares else [])
        if extra:
            out = with_links(out, extra)
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


def default_runner_factory(host: str, group: str | None, jump: str = "") -> Callable[..., Any]:
    from pyverify.lease import LocalHubRunner, SshHubRunner

    if host in LOCAL_HOSTS:
        return LocalHubRunner()
    return JumpSshHubRunner(host, group=group, jump=jump) if jump else \
        SshHubRunner(host, group=group)


_JUMP_RUNNER: Any = None


def _jump_runner_class() -> Any:
    global _JUMP_RUNNER
    if _JUMP_RUNNER is not None:
        return _JUMP_RUNNER
    from pyverify.lease import SshHubRunner

    class _JumpSshHubRunner(SshHubRunner):
        """pyverify's ``SshHubRunner`` with ``-J JUMP`` (a named hub's ``jump``, CCR
        SET-HUB-4) and a connect timeout: the quoting stays pyverify's own."""

        def __init__(self, hub: str, group: str | None = DEFAULT_GROUP, jump: str = "") -> None:
            super().__init__(hub, group=group)
            if jump.startswith("-") or any(c.isspace() for c in jump):
                raise UsageError(f"bad SSH jump host {jump!r}")
            self.jump = jump

        def build(self, argv: Sequence[str]) -> list[str]:
            cmd = super().build(argv)
            return [*cmd[:-2], "-o", "ConnectTimeout=15", "-J", self.jump, *cmd[-2:]]

    _JUMP_RUNNER = _JumpSshHubRunner
    return _JUMP_RUNNER


def JumpSshHubRunner(host: str, *, group: str | None, jump: str) -> Any:  # noqa: N802
    return _jump_runner_class()(host, group=group, jump=jump)


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
    if "admin required" in low:
        return RefusedError(f"{what} on {host}: the hub needs an admin for this ({text})",
                            hint="the hub's unix socket makes you admin: check `fpgahub whoami` on "
                                 "the hub says role admin (a stored API token can lower it)")
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


# --- lease requests: formats, notes (lane LR-A; docs/LEASE_REQUESTS.md "Hub client") ------------
#
# Every fpgahub format below is 0.3.0's (tag v0.3.0, 22aa362: what the lab hub runs), read from
# its source and never guessed. The hub runs each command with pyverify's render settings
# (COLUMNS=400 NO_COLOR=1 TERM=dumb), so rich neither wraps nor colours; ANSI escapes are
# stripped anyway, and a reply that does not have the expected shape raises instead of being
# read as "free" or "nobody waiting".
#
#   fpgahub whoami --json                  cli.py whoami: json.dumps(GET /whoami) (api/v1.py
#                                          whoami; ``holder`` = Principal.holder, "name@host")
#   fpgahub lease show TARGET              cli.py lease_show, NO --json: "held by H (user U,
#                                          expires E)" or "not leased", then a rich Table titled
#                                          "Queue" (Pos, Holder, User) when anyone waits
#   fpgahub board list --json              cli.py chassis_list: json.dumps(GET /groups):
#                                          {"groups": [{"board", "size", "is_paired",
#                                          "members": [{"name", "role"}]}]}
#   fpgahub board lease show BOARD --json  cli.py chassis_lease_show: json.dumps(GET
#                                          /boards/{board}/lease): {"board", "state", "members":
#                                          [{"board", "current": {holder, user, expires_at,
#                                          tier} | null}], "queue", ...}
#   fpgahub board lease revoke BOARD --reason R --yes
#                                          cli.py chassis_lease_revoke: "revoked A, B (by
#                                          unix:alice)" or "no lease to revoke" (api/v1.py
#                                          _do_revoke; admin only: 403 "admin required")
#   fpgahub target lease-history TARGET --limit N --json
#                                          cli.py board_lease_history: console.print_json of
#                                          {"board", "events": [LeaseEventRecord]}; a record
#                                          keeps ONLY ts, event, board, holder, user, position,
#                                          ttl_s, expires_at, source, error (api/schemas.py)
#
# What lease-history can NOT say (0.3.0): who revoked a lease, and why. ``lease.admin_revoked``
# is emitted with ``chassis=`` and no ``board=`` (api/v1.py _do_revoke), and the tail buffer is
# filtered on ``board`` (lease_journal.LeaseEventSink.recent), so it never appears; the
# ``lease.revoked`` that does appear (daemon.py _on_revoke) has its ``reason`` stripped by
# LeaseEventRecord. So ``lease_revoke`` also leaves a revoke note (``rev-<id>.json``) in the note
# directory, written BEFORE the revoke so the victim finds it the moment it notices, and
# ``lease_history`` merges those notes in as ``lease.admin_revoked`` entries.

NOTE_ROOT = "/tmp/harness-manager-lease"
NOTE_MAX_BYTES = 4096
NOTE_MAX_AGE_S = 3600
HISTORY_MAX = 500                    # fpgahub's tail buffer (lease_journal.DEFAULT_TAIL_SIZE)
REASON_MAX = 512
ANSWERS = ("release", "keep")
MAX_KEEP_MINUTES = 24 * 60
ADMIN_REVOKED = "lease.admin_revoked"
#: How far apart fpgahub's ``lease.revoked`` and our revoke note may be and still be one revoke.
REVOKE_MATCH_S = 300.0

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
#: C0 and C1 controls, DEL; a message may still carry newlines and tabs.
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANY_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TABLE_BAR = "│┃|"
_TABLE_SPLIT = re.compile(r"[│┃|]")
_BOX_ONLY = re.compile(r"^[\s\-+=━─┳╇┩┡┏┓└┘┴┬├┤┼╋╈╂╀╁╃╄╅╆╉╊┗┛┣┫┠┨┯┷┿]*$")
_HELD_LINE = re.compile(r"^held by (?P<holder>.+?) \(user (?P<user>.*?), expires (?P<expires>[^)]*)\)$")
_REVOKED_LINE = re.compile(r"^revoked (?P<boards>.+?) \(by (?P<by>[^()]*)\)$")


def valid_name(value: Any) -> bool:
    """An id, target or board safe as a file name and as one argv word: ``[A-Za-z0-9_.-]``,
    1 to 64 characters, and not ``.`` or ``..`` (a target is a directory on the hub)."""
    return isinstance(value, str) and bool(_NAME_RE.fullmatch(value)) and value.strip(".") != ""


def _check_name(value: Any, what: str) -> str:
    if not valid_name(value):
        raise UsageError(f"{what} must be 1 to 64 of [A-Za-z0-9_.-] (not . or ..), not {value!r}")
    return value


def _clean(text: str) -> str:
    return _ANSI.sub("", text or "").replace("\r", "")


def _snippet(text: str) -> str:
    text = _clean(text).strip()
    return repr(text[:200] + ("…" if len(text) > 200 else "")) if text else "(nothing)"


_ISO_TS = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<time>\d{2}:\d{2}(?::\d{2})?)"
                     r"(?:[.,](?P<frac>\d+))?(?P<tz>[Zz]|[+-]\d{2}(?::?\d{2})?)?")


def iso_for_fromisoformat(value: str) -> str | None:
    """``value`` in the one shape every ``datetime.fromisoformat`` (3.10 included) reads:
    ``YYYY-MM-DDTHH:MM:SS[.ffffff][+HH:MM]``. Python 3.10 refuses a ``Z``, a fraction
    that is not 3 or 6 digits (``12:03:59.9``) and an offset without a colon; 3.11+ takes
    all three, so without this a time read fine on one Python and was None on another."""
    m = _ISO_TS.fullmatch(value.strip())
    if m is None:
        return None
    clock = m["time"] if m["time"].count(":") == 2 else m["time"] + ":00"
    frac = f".{(m['frac'] + '000000')[:6]}" if m["frac"] else ""
    tz = m["tz"] or ""
    if tz in ("Z", "z"):
        tz = "+00:00"
    elif tz:
        digits = tz[1:].replace(":", "")
        tz = f"{tz[0]}{digits[:2]}:{(digits[2:] or '00')}"
    return f"{m['date']}T{clock}{frac}{tz}"


def parse_ts(value: Any) -> datetime | None:
    """An ISO 8601 time (``Z`` or an offset; none = UTC), or None. The same on 3.10+."""
    if not isinstance(value, str):
        return None
    text = iso_for_fromisoformat(value)
    if text is None:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ts_key(value: Any) -> datetime:
    return parse_ts(value) or _EPOCH


def _json_from(text: str, what: str) -> Any:
    """The JSON document a ``--json`` verb printed (rich's ``print_json`` included)."""
    clean = _clean(text)
    starts = [i for i in (clean.find("{"), clean.find("[")) if i >= 0]
    if not starts:
        raise UnreachableError(f"{what} printed no JSON: {_snippet(text)}")
    try:
        data, _end = json.JSONDecoder().raw_decode(clean, min(starts))
    except ValueError as exc:
        raise UnreachableError(f"{what} printed JSON that does not parse ({exc}): {_snippet(text)}") from exc
    return data


# -- the dataclasses of the frozen interface ---------------------------------------------------


@dataclass(frozen=True)
class QueueEntry:
    position: int
    holder: str        # principal, "david@mapstone-dev"
    user: str


@dataclass(frozen=True)
class LeaseStatus:
    """``fpgahub lease show``: the holder (if any) and the interactive queue, head first."""

    held: bool
    holder: str = ""
    user: str = ""
    expires_at: str = ""
    queue: tuple[QueueEntry, ...] = ()

    @property
    def head(self) -> QueueEntry | None:
        return self.queue[0] if self.queue else None

    def position_of(self, holder: str) -> int | None:
        return next((q.position for q in self.queue if q.holder == holder), None)


@dataclass(frozen=True)
class RequestNote:
    id: str
    by: str
    user: str
    host: str
    message: str
    created_at: str
    deadline_at: str


@dataclass(frozen=True)
class AnswerNote:
    id: str
    answer: str        # "release" | "keep"
    minutes: int
    message: str
    at: str


# -- fpgahub's outputs ---------------------------------------------------------------------------


def parse_whoami(text: str) -> dict[str, Any]:
    """``fpgahub whoami --json``: the principal; ``holder`` is what a lease is recorded under."""
    data = _json_from(text, "fpgahub whoami --json")
    holder = data.get("holder") if isinstance(data, dict) else None
    if not isinstance(holder, str) or not holder or len(holder) > 256 or any(
            c.isspace() for c in holder) or _ANY_CONTROL.search(holder):
        raise UnreachableError(f"fpgahub whoami printed no usable holder: {_snippet(text)}")
    return {k: v for k, v in data.items() if isinstance(k, str)}


def parse_lease_show(text: str) -> LeaseStatus:
    """``fpgahub lease show TARGET`` (0.3.0 cli.py ``lease_show``, which has no ``--json``).

    Line one is ``held by H (user U, expires E)`` or ``not leased``; a rich table titled
    ``Queue`` (``Pos``, ``Holder``, ``User``) follows when anyone waits, drawn with box
    characters, or ``|``/``+``/``-`` when rich falls back to ASCII. A reply without either
    first line, or with a queue row it cannot read (or that rich truncated with ``…``),
    raises: it is never read as "free" or "nobody waiting".
    """
    held: bool | None = None
    holder = user = expires = ""
    queue: list[QueueEntry] = []
    for raw in _clean(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if held is None:
            m = _HELD_LINE.match(line)
            if m:
                held, holder, user, expires = True, m["holder"], m["user"], m["expires"].strip()
                continue
            if line == "not leased":
                held = False
                continue
        if line[0] not in _TABLE_BAR or _BOX_ONLY.match(line):
            continue                                    # the title, a border, a warning
        cells = [c.strip() for c in _TABLE_SPLIT.split(line)]
        cells = [c for c in cells if c]
        if not cells or _BOX_ONLY.match("".join(cells)) or cells[0] == "Pos":
            continue
        if len(cells) != 3 or not cells[0].isdigit():
            raise UnreachableError(f"fpgahub lease show printed a queue row this client cannot "
                                   f"read: {line!r}")
        if any("…" in c for c in cells):
            raise UnreachableError(f"fpgahub lease show truncated a queue row: {line!r}",
                                   hint="the hub's terminal is too narrow; COLUMNS is set by pyverify")
        queue.append(QueueEntry(int(cells[0]), cells[1], cells[2]))
    if held is None:
        raise UnreachableError(f"fpgahub lease show printed neither 'held by …' nor 'not leased': "
                               f"{_snippet(text)}")
    return LeaseStatus(held, holder, user, expires, tuple(sorted(queue, key=lambda q: q.position)))


def parse_groups(text: str) -> list[tuple[str, list[str]]]:
    """``fpgahub board list --json``: ``[(board, [member target, ...]), ...]``."""
    data = _json_from(text, "fpgahub board list --json")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise UnreachableError(f"fpgahub board list --json has no 'groups' list: {_snippet(text)}")
    out: list[tuple[str, list[str]]] = []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("board"), str):
            continue
        members = [m["name"] for m in g.get("members") or []
                   if isinstance(m, dict) and isinstance(m.get("name"), str)]
        out.append((g["board"], members))
    return out


def parse_board_lease(text: str) -> dict[str, dict[str, Any] | None]:
    """``fpgahub board lease show BOARD --json``: ``{member: current lease or None}``."""
    data = _json_from(text, "fpgahub board lease show --json")
    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(members, list):
        raise UnreachableError(f"fpgahub board lease show --json has no 'members' list: "
                               f"{_snippet(text)}")
    out: dict[str, dict[str, Any] | None] = {}
    for m in members:
        if not isinstance(m, dict) or not isinstance(m.get("board"), str):
            raise UnreachableError(f"fpgahub board lease show --json has a member without a "
                                   f"board: {m!r}")
        cur = m.get("current")
        if cur is not None and not (isinstance(cur, dict) and isinstance(cur.get("holder"), str)):
            raise UnreachableError(f"fpgahub board lease show --json: member {m['board']} has a "
                                   f"lease without a holder: {cur!r}")
        out[m["board"]] = cur
    return out


def parse_revoke(text: str) -> dict[str, Any]:
    """``fpgahub board lease revoke``: ``{"revoked": [target, ...], "by": "unix:alice"}``."""
    for raw in _clean(text).splitlines():
        line = raw.strip()
        m = _REVOKED_LINE.match(line)
        if m:
            return {"revoked": [b.strip() for b in m["boards"].split(",") if b.strip()],
                    "by": m["by"].strip()}
        if line == "no lease to revoke":
            return {"revoked": [], "by": ""}
    raise UnreachableError(f"fpgahub board lease revoke printed neither 'revoked …' nor "
                           f"'no lease to revoke': {_snippet(text)}")


def parse_lease_history(text: str) -> list[dict[str, Any]]:
    """``fpgahub target lease-history TARGET --json``: the events, oldest first."""
    data = _json_from(text, "fpgahub target lease-history --json")
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise UnreachableError(f"fpgahub target lease-history --json has no 'events' list: "
                               f"{_snippet(text)}")
    return [dict(e) for e in events
            if isinstance(e, dict) and isinstance(e.get("ts"), str) and isinstance(e.get("event"), str)]


def merge_history(history: Sequence[dict[str, Any]],
                  notes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """fpgahub's lease-history with our revoke notes placed among it by time.

    fpgahub's events keep fpgahub's order, whatever their timestamps (a coarse clock gives
    ties). Each note goes after every event not later than it: a tie puts the history
    first, and notes among themselves are ordered by time, then id. Deterministic, so the
    same inputs give the same list on every platform.
    """
    queue = sorted(notes, key=lambda n: (_ts_key(n.get("ts")), str(n.get("id") or "")))
    out: list[dict[str, Any]] = []
    i = 0
    for event in history:
        at = parse_ts(event.get("ts"))
        while at is not None and i < len(queue) and _ts_key(queue[i].get("ts")) < at:
            out.append(queue[i])
            i += 1
        out.append(event)
    out.extend(queue[i:])
    return out


def taken_from_history(events: Sequence[dict[str, Any]], holder: str) -> dict[str, str] | None:
    """Who took ``holder``'s lease: ``{by, reason, at}``, or None (it expired, or it is still ours).

    ``events`` is :meth:`HubClient.lease_history` (oldest first). Newest first, the first event
    that ends ``holder``'s tenure decides:

    - a Harness Manager revoke note (``lease.admin_revoked`` whose ``prior_holder`` is
      ``holder``): ``by`` is the principal that forced it, ``reason`` what it gave;
    - fpgahub's own ``lease.revoked`` for ``holder`` (0.3.0 keeps no reason or actor in
      lease-history): a revoke note within 5 minutes supplies ``by`` and ``reason``; without
      one, ``by`` is whoever was promoted next and ``reason`` is ``""``;
    - ``lease.expired``, or ``holder``'s own ``lease.acquired``/``lease.promoted``: None.
    """
    evs = [e for e in events if isinstance(e, dict)]
    notes = [e for e in evs if e.get("event") == ADMIN_REVOKED and e.get("prior_holder") == holder]
    for i in range(len(evs) - 1, -1, -1):
        e = evs[i]
        kind = e.get("event")
        if kind == ADMIN_REVOKED and e.get("prior_holder") == holder:
            return {"by": str(e.get("by") or ""), "reason": str(e.get("reason") or ""),
                    "at": str(e.get("ts") or "")}
        if e.get("holder") != holder:
            continue
        if kind == "lease.revoked":
            at = parse_ts(e.get("ts"))
            note = next((n for n in reversed(notes)
                         if at is not None and (t := parse_ts(n.get("ts"))) is not None
                         and abs((t - at).total_seconds()) <= REVOKE_MATCH_S), None)
            if note is not None:
                return {"by": str(note.get("by") or ""), "reason": str(note.get("reason") or ""),
                        "at": str(e.get("ts") or "")}
            nxt = next((str(x.get("holder") or "") for x in evs[i + 1:]
                        if x.get("event") in ("lease.promoted", "lease.acquired")
                        and x.get("holder") != holder), "")
            return {"by": nxt, "reason": "", "at": str(e.get("ts") or "")}
        if kind in ("lease.expired", "lease.acquired", "lease.promoted"):
            return None
    return None


# -- the note files --------------------------------------------------------------------------------


def check_reason(reason: Any) -> str:
    """A revoke reason: 1 to 512 characters, no control characters (it lands in fpgahub's audit
    log and a query string)."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX \
            or _ANY_CONTROL.search(reason):
        raise UsageError(f"a revoke reason is 1 to {REASON_MAX} characters on one line, "
                         f"without control characters (got {reason!r:.80})")
    return reason


def _note_text(value: Any, what: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or (_BAD_CONTROL if multiline else _ANY_CONTROL).search(value):
        raise UsageError(f"{what} must be text without control characters, not {value!r:.80}")
    return value


def _note_time(value: Any, what: str) -> str:
    if parse_ts(value) is None:
        raise UsageError(f"{what} must be an ISO 8601 time, e.g. 2026-09-24T12:00:00+00:00, "
                         f"not {value!r:.80}")
    return value


def _encode_note(obj: dict[str, Any]) -> str:
    """Compact ASCII JSON (so bytes == characters, and a message cannot carry raw control
    characters or non-ASCII to the hub), at most :data:`NOTE_MAX_BYTES`."""
    body = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(body) > NOTE_MAX_BYTES:
        raise UsageError(f"a lease note is at most {NOTE_MAX_BYTES} bytes; this one is {len(body)}",
                         hint="shorten the message")
    return body


def encode_request(note: RequestNote) -> str:
    if not isinstance(note, RequestNote):
        raise UsageError(f"put_request takes a RequestNote, not {type(note).__name__}")
    _check_name(note.id, "a request id")
    for key in ("by", "user", "host"):
        _note_text(getattr(note, key), f"the request's {key}")
    _note_text(note.message, "the request's message", multiline=True)
    _note_time(note.created_at, "created_at")
    _note_time(note.deadline_at, "deadline_at")
    return _encode_note(asdict(note))


def _check_answer(answer: Any, minutes: Any) -> None:
    if answer not in ANSWERS:
        raise UsageError(f"an answer is 'release' or 'keep', not {answer!r:.40}")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= MAX_KEEP_MINUTES:
        raise UsageError(f"minutes must be a whole number 0..{MAX_KEEP_MINUTES}, not {minutes!r:.40}")


def encode_answer(note: AnswerNote) -> str:
    if not isinstance(note, AnswerNote):
        raise UsageError(f"put_answer takes an AnswerNote, not {type(note).__name__}")
    _check_name(note.id, "a request id")
    _check_answer(note.answer, note.minutes)
    _note_text(note.message, "the answer's message", multiline=True)
    _note_time(note.at, "the answer's time")
    return _encode_note(asdict(note))


def decode_request(data: Any, name: str) -> RequestNote | None:
    """A request note read back from the hub; None (and a warning) when it is not one."""
    try:
        if not isinstance(data, dict):
            raise UsageError("not a JSON object")
        note = RequestNote(**{k: data[k] for k in RequestNote.__dataclass_fields__})
        if name != f"req-{note.id}.json":
            raise UsageError(f"its id {note.id!r} does not match the file name")
        encode_request(note)
    except (KeyError, TypeError, UsageError) as exc:
        log.warning("lease note %s ignored: %s", name, exc)
        return None
    return note


def decode_answer(data: Any, name: str) -> AnswerNote | None:
    try:
        if not isinstance(data, dict):
            raise UsageError("not a JSON object")
        note = AnswerNote(**{k: data[k] for k in AnswerNote.__dataclass_fields__})
        if name != f"ans-{note.id}.json":
            raise UsageError(f"its id {note.id!r} does not match the file name")
        encode_answer(note)
    except (KeyError, TypeError, UsageError) as exc:
        log.warning("lease note %s ignored: %s", name, exc)
        return None
    return note


def decode_revoke(data: Any, name: str) -> dict[str, Any] | None:
    """A revoke note as a lease-history entry (``event`` ``lease.admin_revoked``)."""
    keys = ("ts", "by", "reason", "prior_holder", "board")
    if not isinstance(data, dict) or data.get("event") != ADMIN_REVOKED or not all(
            isinstance(data.get(k), str) for k in keys) or parse_ts(data["ts"]) is None \
            or name != f"rev-{data.get('id')}.json":
        log.warning("lease note %s ignored: not a revoke note", name)
        return None
    return {k: v for k, v in data.items() if isinstance(v, (str, int, list)) and not isinstance(v, bool)}


def note_lines(text: str, prefix: str) -> list[tuple[str, Any]]:
    """The ``list`` op's output: one ``NAME<TAB>JSON`` line per note; oversized, unnamed or
    unparsable lines are dropped (and logged)."""
    out: list[tuple[str, Any]] = []
    for line in (text or "").splitlines():
        name, sep, body = line.partition("\t")
        if not sep:
            continue
        if not (name.startswith(prefix) and name.endswith(".json")
                and valid_name(name[len(prefix):-len(".json")])):
            log.warning("lease note %r ignored: not a %s note name", name[:80], prefix)
            continue
        if len(body) > NOTE_MAX_BYTES:
            log.warning("lease note %s ignored: over %d bytes", name, NOTE_MAX_BYTES)
            continue
        try:
            out.append((name, json.loads(body)))
        except ValueError:
            log.warning("lease note %s ignored: not JSON", name)
    return out


#: The note store's one shell script, run on the hub as ``sh -c SCRIPT hm-lease OP ROOT TARGET
#: GROUP ARG...`` through the same runner (and so the same ``sg fpga``) as every fpgahub verb.
#: Values only ever arrive as positional parameters, each quoted by the runner (shlex.quote,
#: once per shell), and are only ever expanded inside double quotes: a message cannot reach a
#: shell as code. Ops: ``put NAME BODY`` (atomic: mktemp in the directory, then mv), ``list
#: PREFIX`` (``NAME<TAB>BODY`` lines), ``get NAME``, ``del NAME``. ``put`` and ``list`` prune
#: notes older than an hour. Exit codes: 64 usage, 65 too big, 66 not writable, 67 not
#: readable, 68 not a plain directory (a symlink, say); messages start with ``hm-lease:``.
NOTE_SCRIPT = r"""set -u
op=$1 root=$2 target=$3 grp=$4
shift 4
dir=$root/$target
umask 007
fail() { printf 'hm-lease: %s\n' "$2" >&2; exit "$1"; }
checkname() {
  case $1 in req-?*.json|ans-?*.json|rev-?*.json) ;; *) fail 64 "not a note name: $1" ;; esac
  case $1 in *[!A-Za-z0-9_.-]*) fail 64 "not a note name: $1" ;; esac
}
plain() {
  for d in "$root" "$dir"; do
    if [ -L "$d" ]; then fail 68 "$d is a symbolic link; refusing to use it"; fi
    if [ -e "$d" ] && [ ! -d "$d" ]; then fail 68 "$d is not a directory"; fi
  done
}
prune() {
  [ -d "$dir" ] || return 0
  find "$dir" -maxdepth 1 -type f \( -name 'req-*.json' -o -name 'ans-*.json' \
    -o -name 'rev-*.json' -o -name '.tmp.*' \) -mmin +60 -exec rm -f {} + 2>/dev/null
  return 0
}
ensure() {
  plain
  for d in "$root" "$dir"; do
    if [ ! -d "$d" ]; then
      mkdir "$d" 2>/dev/null || [ -d "$d" ] || fail 66 "cannot create $d"
      if [ -n "$grp" ]; then chgrp "$grp" "$d" 2>/dev/null; fi
      chmod 2770 "$d" 2>/dev/null
    fi
  done
  if [ ! -w "$dir" ] || [ ! -x "$dir" ]; then fail 66 "$dir is not writable"; fi
}
readable() {
  plain
  [ -d "$dir" ] || exit 0
  if [ ! -r "$dir" ] || [ ! -x "$dir" ]; then fail 67 "$dir is not readable"; fi
}
emit() {
  [ -L "$1" ] && return 0
  head -c 4097 "$1" 2>/dev/null | LC_ALL=C tr -cd '\040-\176'
}
case $op in
put)
  checkname "$1"
  n=$(printf '%s' "$2" | wc -c | tr -d ' ')
  [ "$n" -le 4096 ] || fail 65 "a note is at most 4096 bytes; this one is $n"
  ensure
  prune
  tmp=$(mktemp "$dir/.tmp.XXXXXX" 2>/dev/null) || fail 66 "cannot create a file in $dir"
  if printf '%s' "$2" > "$tmp" && chmod 660 "$tmp" && mv -f "$tmp" "$dir/$1"; then exit 0; fi
  rm -f "$tmp"
  fail 66 "cannot write $dir/$1"
  ;;
list)
  case $1 in req-|ans-|rev-) ;; *) fail 64 "not a note kind: $1" ;; esac
  readable
  prune
  for f in "$dir/$1"*.json; do
    [ -f "$f" ] || continue
    b=${f##*/}
    case $b in *[!A-Za-z0-9_.-]*) continue ;; esac
    printf '%s\t' "$b"
    emit "$f"
    printf '\n'
  done
  ;;
get)
  checkname "$1"
  readable
  [ -f "$dir/$1" ] || exit 0
  emit "$dir/$1"
  ;;
del)
  checkname "$1"
  plain
  if [ ! -e "$dir/$1" ] && [ ! -L "$dir/$1" ]; then exit 0; fi
  rm -f "$dir/$1" 2>/dev/null || fail 66 "cannot delete $dir/$1"
  ;;
*) fail 64 "unknown op: $op" ;;
esac
"""

NOTE_OPS = ("put", "list", "get", "del")
_NOTE_EXIT = {64: "usage", 65: "usage", 66: "unwritable", 67: "unreadable", 68: "unsafe"}


def note_error(res: Any, what: str, host: str, target: str, note_dir: str) -> HarnessError:
    """A failed note op as a ``HarnessError``: ours (``hm-lease:``) or the transport's."""
    text = _clean((getattr(res, "stderr", "") or "") + (getattr(res, "stdout", "") or "")).strip()
    m = re.search(r"hm-lease: (.*)", text)
    kind = _NOTE_EXIT.get(getattr(res, "returncode", 1)) if m else None
    if kind == "usage":
        return UsageError(f"{what}: {m.group(1)}")          # type: ignore[union-attr]
    if kind is not None:
        exc = UnavailableError("lease requests", f"{what}: {m.group(1)} on {host}")  # type: ignore[union-attr]
        exc.hint = (f"remove {note_dir} on {host}: it is not Harness Manager's" if kind == "unsafe"
                    else f"on {host}, its owner runs: chgrp fpga {note_dir} && chmod 2770 {note_dir} "
                         f"(or removes it; the next note recreates it)")
        return exc
    return classify_hub_error(what, host, target, text or f"exit status {getattr(res, 'returncode', '?')}")


class HubClient:
    """fpgahub on one hub for one target: lease verbs (pyverify ``LeaseClient``) and shares."""

    def __init__(self, host: str, target: str = DEFAULT_TARGET, *, group: str | None = DEFAULT_GROUP,
                 runner: Callable[..., Any] | None = None, timeout_s: float = HUB_TIMEOUT_S,
                 board: str = "", jump: str = "") -> None:
        from pyverify.lease import LeaseClient

        self.host = host
        self.target = target
        self.group = group
        self.board = board                   # hub.board in boards.toml; "" = ask fpgahub
        self.jump = jump                     # a named hub's ssh -J (SET-HUB-4); "" = none
        self.timeout_s = timeout_s
        self.note_root = NOTE_ROOT           # tests point it at a temporary directory
        self._whoami: dict[str, Any] | None = None
        self._board_id: str | None = None
        if runner is None:
            # The factory is a test seam taking (host, group); a jump is passed only when set.
            runner = DEFAULT_RUNNER_FACTORY(host, group, jump=jump) if jump else \
                DEFAULT_RUNNER_FACTORY(host, group)
        self._run = _Recorder(runner)
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
        """Start (or get: fpgahub returns an existing share as it is) the share for ``tty``.
        Never ``tty_00``: that refuses before the hub is asked (MCC-FIX)."""
        if is_mcc_share("", tty):
            raise refuse_mcc_share(tty, self.host)
        text = self._share_cmd(["fpgahub", "share", "start", self.target, tty, "--baud", str(baud)],
                               "share start")
        found = [s for s in parse_share_start(text) if s.tty == tty]
        if not found:
            raise UnreachableError(f"share start on {self.host} printed no share for {tty}: "
                                   f"{text.strip()[:200]}")
        return found[0]


    # -- lease requests (lane LR-A; docs/LEASE_REQUESTS.md "Hub client") -----------------------

    def _hub_out(self, argv: list[str], what: str) -> str:
        """One fpgahub verb's stdout, or its failure as a ``HarnessError``."""
        from pyverify.lease import LeaseError

        def run() -> str:
            res = self._run(argv, timeout=self.timeout_s)
            if res.returncode != 0:
                raise LeaseError(f"{what} failed: {res.text.strip() or '(no output)'}")
            return res.stdout or ""

        return self._call(what, run)

    def whoami(self) -> dict[str, Any]:
        """``fpgahub whoami --json`` (asked once per client): ``holder``, ``audit_id``, ``role``…"""
        if self._whoami is None:
            self._whoami = parse_whoami(self._hub_out(["fpgahub", "whoami", "--json"], "whoami"))
        return dict(self._whoami)

    def principal(self) -> str:
        """What fpgahub records this client's leases and queue entries under (``name@host``).

        fpgahub 0.3.0 ignores ``--holder``: over the hub's unix socket it is the unix user at
        the hub's hostname, e.g. ``david@mapstone-dev``, whatever the caller asked for.
        """
        return str(self.whoami()["holder"])

    def lease_status(self) -> LeaseStatus:
        """``fpgahub lease show TARGET``: the holder and the interactive queue (one ssh call)."""
        return parse_lease_show(self._hub_out(["fpgahub", "lease", "show", self.target],
                                              "lease show"))

    def board_id(self) -> str:
        """The physical board that owns the target: ``hub.board`` in boards.toml, else the
        group ``fpgahub board list --json`` puts the target in (asked once per client)."""
        if self.board:
            return self.board
        if self._board_id is None:
            groups = parse_groups(self._hub_out(["fpgahub", "board", "list", "--json"], "board list"))
            owners = [board for board, members in groups if self.target in members]
            if not owners:
                raise AbsentError(f"no board on the hub {self.host} has the target {self.target}",
                                  hint="set hub.board in boards.toml (mps3_01 for mps3_01_pl)")
            if not valid_name(owners[0]):
                raise UnreachableError(f"fpgahub board list named the board {owners[0]!r}, "
                                       "which is not a board id this client will pass on")
            self._board_id = owners[0]
        return self._board_id

    def lease_revoke(self, reason: str) -> dict[str, Any]:
        """Force-release the target: ``fpgahub board lease revoke BOARD --reason R --yes``.

        This KICKS whoever holds the board; fpgahub then promotes the head of the queue. The
        board's members are read first (``board lease show --json``), and the revoke is
        refused when it would also kick a lease on another member held by someone else, or
        when the lease is this client's own. Before revoking, a revoke note is left for the
        victim (:func:`taken_from_history`); it is withdrawn if nothing was revoked.

        Returns ``{"revoked": [target, ...], "by": fpgahub's actor ("unix:alice"), "board",
        "prior_holder", "reason", "principal"}``; ``revoked`` is empty when nobody held it.
        """
        reason = check_reason(reason)
        board = self.board_id()
        me = self.principal()
        members = parse_board_lease(self._hub_out(
            ["fpgahub", "board", "lease", "show", board, "--json"], "board lease show"))
        if self.target not in members:
            raise AbsentError(f"fpgahub's board {board} has no member {self.target}",
                              hint="set hub.board in boards.toml to the board that owns it")
        current = members[self.target]
        base = {"board": board, "reason": reason, "principal": me}
        if current is None:
            return {"revoked": [], "by": "", "prior_holder": "", **base}
        prior = str(current["holder"])
        if prior == me:
            raise RefusedError(f"{self.target} is held by this client's own principal ({me})",
                               hint="release it instead: harness-manager lease release")
        others = sorted(f"{m} (held by {c['holder']})" for m, c in members.items()
                        if m != self.target and c is not None and c.get("holder") != prior)
        if others:
            raise RefusedError(f"force-releasing board {board} would also kick {', '.join(others)}",
                               hint="ask them to release first; fpgahub revokes whole boards")
        note_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        name = f"rev-{note_id}.json"
        note = {"id": note_id, "ts": datetime.now(timezone.utc).isoformat(), "event": ADMIN_REVOKED,
                "board": self.target,
                "chassis": board, "by": me, "actor": str(self.whoami().get("audit_id") or ""),
                "reason": reason, "prior_holder": prior, "prior_user": str(current.get("user") or ""),
                "source": "harness-manager"}
        noted = False
        try:
            self._notes("put", name, _encode_note(note), what="leave the revoke note")
            noted = True
        except HarnessError as exc:
            log.warning("revoking %s without a revoke note (the victim will not see the reason): %s",
                        self.target, exc)
        try:
            text = self._hub_out(["fpgahub", "board", "lease", "revoke", board, "--reason", reason,
                                  "--yes"], "board lease revoke")
        except HarnessError:
            if noted:
                with contextlib.suppress(HarnessError):
                    self._notes("del", name, what="withdraw the revoke note")
            raise
        out = parse_revoke(text)          # a reply we cannot read keeps the note: it may have run
        if not out["revoked"] and noted:
            with contextlib.suppress(HarnessError):
                self._notes("del", name, what="withdraw the revoke note")
        return {**out, "prior_holder": prior, **base}

    def lease_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """The target's recent lease events, oldest first: fpgahub's ``target lease-history
        --json`` plus this hub's revoke notes (as ``lease.admin_revoked`` entries), the last
        ``limit`` (1..500) of them. Unreadable notes only cost the notes."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= HISTORY_MAX:
            raise UsageError(f"limit must be a whole number 1..{HISTORY_MAX}, not {limit!r}")
        events = parse_lease_history(self._hub_out(
            ["fpgahub", "target", "lease-history", self.target, "--limit", str(limit), "--json"],
            "target lease-history"))
        notes: list[dict[str, Any]] = []
        try:
            out = self._notes("list", "rev-", what="list revoke notes")
            notes = [e for n, d in note_lines(out, "rev-") if (e := decode_revoke(d, n))]
        except HarnessError as exc:
            log.warning("lease history for %s without revoke notes: %s", self.target, exc)
        return merge_history(events, notes)[-limit:]

    # -- the note files: /tmp/harness-manager-lease/<target>/ on the hub -----------------------

    @property
    def note_dir(self) -> str:
        return f"{self.note_root}/{self.target}"

    def _notes(self, op: str, *args: str, what: str) -> str:
        """Run one note op (:data:`NOTE_SCRIPT`) on the hub; its stdout."""
        if op not in NOTE_OPS:
            raise UsageError(f"unknown note op {op!r}")
        _check_name(self.target, "the hub target")
        if not self.note_root.startswith("/") or any(c.isspace() for c in self.note_root):
            raise UsageError(f"the note root must be an absolute path, not {self.note_root!r}")
        argv = ["sh", "-c", NOTE_SCRIPT, "hm-lease", op, self.note_root, self.target,
                self.group or "", *args]

        def run() -> str:
            res = self._run(argv, timeout=self.timeout_s)
            if res.returncode != 0:
                raise note_error(res, what, self.host, self.target, self.note_dir)
            return res.stdout or ""

        return self._call(what, run)

    def put_request(self, note: RequestNote) -> None:
        """Leave ``req-<id>.json`` for the holder's session (atomic, at most 4 KiB)."""
        body = encode_request(note)
        self._notes("put", f"req-{note.id}.json", body, what="leave the request note")

    def list_requests(self) -> list[RequestNote]:
        """Every request note for the target, oldest first; prunes notes older than an hour.
        A missing directory is no requests; an unreadable one raises."""
        out = self._notes("list", "req-", what="list request notes")
        notes = [n for name, data in note_lines(out, "req-") if (n := decode_request(data, name))]
        return sorted(notes, key=lambda n: (_ts_key(n.created_at), n.id))

    def delete_request(self, request_id: str) -> None:
        """Withdraw ``req-<id>.json`` (a missing one is fine; its answer is left to pruning)."""
        _check_name(request_id, "a request id")
        self._notes("del", f"req-{request_id}.json", what="withdraw the request note")

    def put_answer(self, note: AnswerNote) -> None:
        """Leave ``ans-<id>.json`` (``id`` is the request's) for the requester's session."""
        body = encode_answer(note)
        self._notes("put", f"ans-{note.id}.json", body, what="leave the answer note")

    def get_answer(self, request_id: str) -> AnswerNote | None:
        """The answer to a request, or None (none yet, or one that is not a valid answer)."""
        _check_name(request_id, "a request id")
        name = f"ans-{request_id}.json"
        out = self._notes("get", name, what="read the answer note").strip()
        if not out:
            return None
        if len(out) > NOTE_MAX_BYTES:
            log.warning("lease note %s ignored: over %d bytes", name, NOTE_MAX_BYTES)
            return None
        try:
            data = json.loads(out)
        except ValueError:
            log.warning("lease note %s ignored: not JSON", name)
            return None
        return decode_answer(data, name)


def start_share_hint(host: str, target: str, tty: str, baud: int = DEFAULT_SHARE_BAUD) -> str:
    return (f"start it: ssh {host} 'sg fpga -c \"fpgahub share start {target} {tty} --baud {baud}\"'"
            f" (or set start_shares = true in the board's hub table)")


# --- the hub:// opener ---------------------------------------------------------------------------


@dataclass
class _ShareRoute:
    port: int                             # the share's port on the hub
    local_port: int
    tunnel: Any = None                    # SshTunnel, or None on the hub itself
    host: str = "127.0.0.1"               # where to connect: the hub itself when REST-only


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
        self._closed_at: dict[ShareRef, list[float]] = {}   # our connections' close times
        self._configs: dict[tuple[str, str], HubConfig] = {}

    def note_closed(self, ref: ShareRef) -> None:
        """One of our connections to this share has just closed (``_SharePort.close``)."""
        now = time.monotonic()
        with self._mu:
            recent = [t for t in self._closed_at.get(ref, []) if now - t < OWN_LINGER_S]
            self._closed_at[ref] = [*recent, now]

    def lingering(self, ref: ShareRef) -> int:
        """How many of the share's clients could still be our own closed connections."""
        now = time.monotonic()
        with self._mu:
            recent = [t for t in self._closed_at.get(ref, []) if now - t < OWN_LINGER_S]
            if recent:
                self._closed_at[ref] = recent
            else:
                self._closed_at.pop(ref, None)
            return len(recent)

    def configure(self, cfg: HubConfig) -> None:
        with self._mu:
            self._starts[(cfg.host, cfg.target)] = cfg.start_shares
            self._bauds[(cfg.host, cfg.target)] = cfg.baud
            self._groups[(cfg.host, cfg.target)] = cfg.group
            self._configs[(cfg.host, cfg.target)] = cfg

    def client(self, ref: ShareRef) -> Any:
        with self._mu:
            group = self._groups.get((ref.host, ref.target), DEFAULT_GROUP)
            cfg = self._configs.get((ref.host, ref.target))
        if cfg is not None and cfg.rest is not None:
            from harness_manager.transports import hub_rest

            return hub_rest.client_for(cfg)
        return HubClient(ref.host, ref.target, group=group, jump=cfg.jump if cfg else "")

    def route(self, ref: ShareRef, info: ShareInfo) -> _ShareRoute:
        with self._build:
            with self._mu:
                old = self._routes.get(ref)
            if old is not None and old.port == info.port and (
                    old.tunnel is None or old.tunnel.alive() or old.tunnel.state == "starting"):
                return old
            if old is not None and old.tunnel is not None:
                old.tunnel.close()
            with self._mu:
                cfg = self._configs.get((ref.host, ref.target))
            rest = cfg.rest if cfg is not None else None
            if ref.host in LOCAL_HOSTS and rest is None:
                new = _ShareRoute(info.port, info.port)
            elif rest is not None and not rest.ssh_host:
                # REST only (no SSH account): the share listens on the hub's 0.0.0.0 (T8).
                new = _ShareRoute(info.port, info.port, host=rest.host)
            else:
                t = _tunnel.SshTunnel(ref.host,
                                      [_tunnel.Forward("share", info.remote_host, info.port)],
                                      label=f"hub share {ref.tty} on {ref.host}",
                                      jump=cfg.jump if cfg is not None else "")
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
    """Find the share (``share list``; ``share start`` only when allowed) and forward to it.
    Never the MCC's ``tty_00``: refused before the hub is asked (MCC-FIX)."""
    if is_mcc_share("", ref.tty):
        raise refuse_mcc_share(ref.tty, ref.host)
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
    info, route = settle_write_slot(ref, *resolve_share(ref))
    read_only = info.readers > 0
    why = (f"another client ({info.writer or 'unknown'}) holds the hub share's write slot; "
           "fpgahub drops every other client's writes") if read_only else ""
    port = _SharePort(ref, route.host, route.local_port, baud=share_baud,
                      read_only=read_only, read_only_reason=why,
                      label=f"hub share {ref.tty} on {ref.host}")
    tcp_serial.mark_share(ref.url, share_baud)
    return port


def settle_write_slot(ref: ShareRef, info: ShareInfo, route: _ShareRoute,
                      *, sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic) -> tuple[ShareInfo, _ShareRoute]:
    """Wait for our own just-closed connections to leave the share (module docstring).

    Re-lists only while EVERY client the hub counts could be one of ours, closed in the
    last ``OWN_LINGER_S``; a client that cannot be ours ends the wait at once, and so does
    ``SLOT_WAIT_S``. Returns the last listing (and route: the share may have moved).
    """
    deadline = clock() + SLOT_WAIT_S
    while 0 < info.readers <= SHARES.lingering(ref) and clock() < deadline:
        sleep(SLOT_POLL_S)
        info, route = resolve_share(ref)
    return info, route


class _SharePort(tcp_serial.TcpSerialPort):
    """A hub share's port that tells ``SHARES`` when it closes, so the next opener knows
    the hub may still be counting it for a moment (``settle_write_slot``)."""

    def __init__(self, ref: ShareRef, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ref = ref

    def close(self) -> None:
        was_open = self.is_open
        super().close()
        if was_open:
            SHARES.note_closed(self.ref)


register_serial_scheme(HUB_SCHEME, open_hub_share)


# --- hub shares as consoles --------------------------------------------------------------------


def _pipe(src: socket.socket, dst: socket.socket,
          on_src_end: Callable[[], None] | None = None) -> None:
    """Copy ``src`` to ``dst`` until ``src`` ends; then end ``dst``'s sending side.
    ``on_src_end`` runs when ``src`` ended (EOF or an error), BEFORE ``dst`` is shut:
    so of two pipes, the one whose source ended first always calls back first."""
    try:
        while True:
            try:
                data = src.recv(65536)
            except OSError:
                data = b""
            if not data:
                if on_src_end is not None:
                    on_src_end()
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
            # Our last connection may still hold the share's write slot (module docstring):
            # wait for the hub to let go, or the first keys typed are dropped. Keys typed
            # meanwhile wait in the client socket; nothing is lost.
            _info, route = settle_write_slot(self.ref, *resolve_share(self.ref))
            upstream = socket.create_connection((route.host, route.local_port), timeout=10)
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

        first_end: list[str] = []
        end_mu = threading.Lock()

        def ended(side: str) -> None:
            # The console left first: we close our side, and the hub counts it until it
            # reads the EOF; record that, so a quick reconnect settles. The share ending
            # first means the hub has already let go: nothing to record.
            with end_mu:
                if first_end:
                    return
                first_end.append(side)
            if side == "console":
                SHARES.note_closed(self.ref)

        def from_client() -> None:
            _pipe(client, upstream, on_src_end=lambda: ended("console"))
            with contextlib.suppress(OSError):
                upstream.shutdown(socket.SHUT_RDWR)       # the console left: end both ways

        back = threading.Thread(target=from_client, name=f"share-relay-in-{self.ref.tty}",
                                daemon=True)
        back.start()
        try:
            _pipe(upstream, client, on_src_end=lambda: ended("share"))
            back.join(timeout=RELAY_DRAIN_S)
        finally:
            # Both sockets of this connection, now: they used to stay open until the
            # board closed, two fds per console (re)connection (Q2 finding).
            with self._mu:
                self._conns = [c for c in self._conns if c is not client and c is not upstream]
            for c in (client, upstream):
                with contextlib.suppress(OSError):
                    c.shutdown(socket.SHUT_RDWR)
                c.close()

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
        from harness_manager.transports import hub_rest

        self.config = cfg
        self.host = cfg.host
        self.target = cfg.target
        # T8: url -> fpgahub's REST API (hub_rest.RestHubClient), else ssh (HubClient).
        self.client = client or hub_rest.client_for(
            cfg, ssh_factory=lambda: HubClient(cfg.host, cfg.target, group=cfg.group,
                                               board=cfg.board, jump=cfg.jump))
        self.transport = getattr(self.client, "transport", "ssh")
        SHARES.configure(cfg)

    def share_status(self) -> dict[str, Any]:
        return SHARES.status(self.host, self.target)

    def refuse_share(self, name: str, tty: str) -> None:
        """``RefusedError`` for the MCC's tty_00 (MCC-FIX), before any client is asked."""
        if is_mcc_share(name, tty):
            raise refuse_mcc_share(tty, self.host)

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

