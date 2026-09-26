"""The MPS3 pack's settings rows (lane SET-PACK; ``docs/design/SETTINGS.md`` §3.2, Appendix A).

``Mps3Pack.settings()`` returns ``mps3_rows(...)``: the pack's own ``mps3.*`` rows and the
``boards.toml`` tables this pack reads (``boards.*.hub``, ``xvc``, ``sysmon``, ``estimates``,
``ssh``).
The Settings menu, ``harness-manager config`` and the API draw them; the core has no MPS3
code for them.

**Nothing reads a value through these rows yet.** The pack still reads its variables from
``os.environ`` and still validates its own ``boards.toml`` tables (``hub.parse_hub_table``,
``xvc.xvc_config``, ``sysmon.make_sysmon_reader``, ``telemetry._estimates_from``). Switching
the readers to the resolver is lane SET-WIRE. Every default here comes from the constant the
reader uses today, so the two cannot drift; ``READ_AT`` names where each value is read
(``harness_manager_mps3/<file>:<line>``, checked by ``tests/unit/test_settings_pack.py``).

**Pack defaults.** A row's default is this pack instance's value: ``Mps3Pack(console_pace_s=0)``
gives ``mps3.console.pace_ms`` 0. Those defaults are the resolver's ``pack`` layer.

**Not declared here:**

- ``boards.*.power.*``: board-agnostic (``harness_manager/power/config.py``), so the core
  declares it (``settings/rows.py`` B10-B16) and every pack gets it;
- the hub's own keys (``host``, ``url``, ``group``, the token, ...): ``hubs.*`` core rows,
  which ``hub.use`` names (SET-HUBS);
- ``boards.*.via``: a core row (B3);
- the ``xvc = "<reach>"`` shorthand is still accepted by the pack, but the resolver sees
  only the table form (``xvc = { reach = ... }``), which is what the menu writes.
"""

from __future__ import annotations

import re
from typing import Any

from pyverify.pusher import TFTP_PORT

from harness_manager.settings.schema import Setting

from . import constants as _c

PACK = "mps3"

#: Where each value is read today, by key: ``file:line`` under ``harness_manager_mps3/``.
#: Filled as the rows are built (``mps3_rows``). SET-WIRE switches these readers.
READ_AT: dict[str, str] = {}

_HUB_TARGET_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_TRUTHY = ("", "auto", "1", "true", "yes", "on", "0", "false", "no", "off")


def _row(key: str, type_: str, default: Any, section: str, doc: str, *, at: str,
         **kw: Any) -> Setting:
    READ_AT[key] = at
    return Setting(key, type_, default, section, doc, pack=PACK, **kw)


# --- checks -------------------------------------------------------------------------------------


def _port(v: Any) -> str:
    return "" if 1 <= v <= 65535 else "must be a port, 1..65535"


def _ms(lo: int, hi: int, why: str = "") -> Any:
    def check(v: Any) -> str:
        return "" if lo <= v <= hi else f"must be {lo}..{hi} ms{why}"
    return check


def _tunnelled(v: Any) -> str:
    return "" if v.strip().lower() in _TRUTHY else "must be auto, on or off (1/0, yes/no)"


def _word(what: str) -> Any:
    def check(v: Any) -> str:
        return "" if not v or (not any(c.isspace() for c in v) and not v.startswith("-")) \
            else f"must be a {what} (no spaces, not starting with '-')"
    return check


def _hub_target(v: Any) -> str:
    return "" if _HUB_TARGET_RE.match(v) else "must be an fpgahub board name, e.g. mps3_01_pl"


def _hub_board(v: Any) -> str:
    from .hub import valid_name

    return "" if v == "" or valid_name(v) else "must be an fpgahub board id, e.g. mps3_01"


def _tty(v: Any) -> str:
    return "" if v.startswith("/dev/") else "must be a /dev/... TTY path on the hub"


def _host_key(v: Any) -> str:
    from .claim import check_pin

    return check_pin(v)


def _positive(v: Any) -> str:
    return "" if v > 0 else "must be more than 0"


def _not_negative(v: Any) -> str:
    return "" if v >= 0 else "must be 0 or more"


# --- the rows -----------------------------------------------------------------------------------


def mps3_rows(*, console_pace_s: float = _c.DUT_CONSOLE_PACE_S,
              rbb_port: int = _c.JTAG_RBB_PORT, push_port: int | None = None,
              tftp_port: int | None = None) -> tuple[Setting, ...]:
    """Every MPS3 row. The arguments are ``Mps3Pack``'s own (``--pack-overrides``)."""
    from .hub import DEFAULT_SHARE_BAUD, DEFAULT_TARGET
    from .mcc import DEFAULT_TIMING, SHARE_PACE_S
    from .sysmon import DEFAULT_DEVICE, DEFAULT_HW_SERVER
    from .telemetry import SYSMON_MIN_INTERVAL_S
    from .xvc import REACH_CHOICES

    return (*_pack_rows(console_pace_s, rbb_port, push_port or _c.PUSH_PORT,
                        tftp_port or TFTP_PORT, DEFAULT_TIMING.pace_s, SHARE_PACE_S),
            *_board_rows(DEFAULT_TARGET, DEFAULT_SHARE_BAUD, REACH_CHOICES,
                         DEFAULT_HW_SERVER, DEFAULT_DEVICE, SYSMON_MIN_INTERVAL_S))


def _pack_rows(console_pace_s: float, rbb_port: int, push_port: int, tftp_port: int,
               mcc_pace_s: float, share_pace_s: float) -> tuple[Setting, ...]:
    return (
        # T2 (also frozen at import, constants.py:113; SETTINGS.md §12.9: SET-WIRE drops it)
        _row("mps3.openocd_cfg_dir", "path", "", "Tools",
             "OpenOCD's MPS3 target configs (empty: the platform checkout beside Harness "
             "Manager, else the packaged copy)", at="openocd.py:102",
             scope="machine", owner="admin", env="HARNESS_MANAGER_MPS3_OPENOCD_DIR",
             advanced=True),
        # K7 (the core's --overlay-dir puts directories first: cli/cmd_program.py:23-25)
        _row("mps3.overlay_dirs", "list", [], "Harness + kits",
             "Extra overlay directories, searched first", at="overlays.py:313",
             env="HARNESS_MANAGER_MPS3_OVERLAY_DIRS", env_split="pathsep"),
        # C1: the pack's kwarg console_pace_s (pack.py:308), paced consoles constants.py:32
        _row("mps3.console.pace_ms", "int", round(console_pace_s * 1000), "Consoles",
             "Delay between characters typed into the DUT's UARTs (0: none; the nanoSoC "
             "UART has no receive FIFO)", at="pack.py:310", scope="pack", apply="reopen",
             check=_ms(0, 500)),
        # C2: MccTiming.pace_s over the Debug USB; SHARE_PACE_S across a hub share
        _row("mps3.mcc.pace_ms", "int", round(mcc_pace_s * 1000), "Consoles",
             "Delay between characters sent to the MCC over the Debug USB",
             at="mcc.py:122", scope="pack", apply="reopen", advanced=True,
             check=_ms(50, 1000, " (the MCC drops faster input)")),
        _row("mps3.mcc.share_pace_ms", "int", round(share_pace_s * 1000), "Consoles",
             "Delay between characters sent to the MCC across a hub share",
             at="mcc.py:139", scope="pack", apply="reopen", advanced=True,
             check=_ms(50, 1000, " (the MCC drops faster input)")),
        # D4: the pack's kwarg rbb_port (pack.py:306), via --pack-overrides only
        _row("mps3.rbb_port", "int", rbb_port, "Debug",
             "The board's remote_bitbang JTAG port", at="pack.py:308", scope="pack",
             owner="dev", apply="restart", check=_port),
        # D5
        _row("mps3.xvc_port", "int", _c.XVC_PORT, "Debug",
             "The XVC port of a board reached directly", at="xvc.py:287", scope="pack",
             owner="dev", env="HARNESS_MANAGER_MPS3_XVC_PORT", check=_port),
        # X4-X5
        _row("mps3.identify.port", "int", _c.IDENTIFY_PORT, "Advanced",
             "The identify probe's UDP port", at="identify.py:78", scope="pack",
             owner="dev", env="HARNESS_MANAGER_MPS3_IDENTIFY_PORT", check=_port),
        _row("mps3.identify.broadcast", "list", [], "Advanced",
             "Discovery targets, addr[:port] (empty: 255.255.255.255 and "
             f"{_c.DEFAULT_SHELL_HOST}; a list replaces both)", at="identify.py:94",
             scope="pack", owner="dev", env="HARNESS_MANAGER_MPS3_IDENTIFY_BROADCAST"),
        # X6-X8
        _row("mps3.push_port", "int", push_port, "Advanced",
             "The bitstream push port (raw or windowed TCP)", at="deploy.py:349",
             scope="pack", owner="dev", env="HARNESS_MANAGER_MPS3_PUSH_PORT", check=_port),
        _row("mps3.tftp_port", "int", tftp_port, "Advanced", "The TFTP push port",
             at="deploy.py:353", scope="pack", owner="dev",
             env="HARNESS_MANAGER_MPS3_TFTP_PORT", check=_port),
        _row("mps3.tunnelled", "str", "", "Advanced",
             "Treat the shell as reached through a TCP-only tunnel (no TFTP): on, off, or "
             "empty for auto", at="deploy.py:208", scope="pack", owner="dev",
             env="HARNESS_MANAGER_MPS3_TUNNELLED", apply="reopen", check=_tunnelled),
    )


def _board_rows(target: str, baud: int, reach: tuple[str, ...], hw_server: str, device: str,
                min_interval_s: float) -> tuple[Setting, ...]:
    return (
        # B5-B9: the per-board keys of the hub table (the per-hub ones are hubs.* rows)
        _row("boards.*.hub.target", "str", target, "Boards",
             "The fpgahub target leases and shares use (mps3_01_pl, never the chassis)",
             at="hub.py:145", scope="board", apply="reopen", check=_hub_target),
        _row("boards.*.hub.board", "str", "", "Boards",
             "The physical board that owns the target, for a revoke (empty: ask the hub)",
             at="hub.py:162", scope="board", apply="reopen", advanced=True,
             check=_hub_board),
        _row("boards.*.hub.shares.*", "str", None, "Boards",
             "A hub TTY share by name (mcc, fpga_uart0..3): its /dev path on the hub",
             at="hub.py:148", scope="board", apply="reopen", check=_tty),
        _row("boards.*.hub.baud", "int", baud, "Boards", "The rate the hub's shares run at",
             at="hub.py:153", scope="board", apply="reopen", advanced=True,
             check=_positive),
        _row("boards.*.hub.start_shares", "bool", False, "Boards",
             "Start a missing share (off: use one that is already running)",
             at="hub.py:156", scope="board", apply="reopen"),
        # B17-B18
        _row("boards.*.xvc.reach", "enum", "auto", "Boards",
             "How XVC reaches the harness: through the hub's tunnel, board SSH or directly",
             at="xvc.py:126", scope="board", apply="reopen", choices=reach),
        _row("boards.*.xvc.user", "str", "", "Boards",
             "The board-SSH user for XVC (empty: the board's SSH link, else root)",
             at="xvc.py:284", scope="board", apply="reopen", check=_word("user name")),
        _row("boards.*.xvc.host", "str", "", "Boards",
             "The board-SSH host for XVC (empty: the board's own address)",
             at="xvc.py:130", scope="board", apply="reopen", advanced=True,
             check=_word("host name")),
        # B19-B20: SYSMON over JTAG (xsdb through hw_server, or an OpenOCD adapter)
        _row("boards.*.sysmon.backend", "enum", "xsdb", "Boards",
             "How the FPGA's SYSMON is read over JTAG", at="sysmon.py:415", scope="board",
             choices=("xsdb", "openocd")),
        _row("boards.*.sysmon.xsdb", "path", "xsdb", "Boards", "xsdb (xsdb backend)",
             at="sysmon.py:421", scope="board", advanced=True),
        _row("boards.*.sysmon.hw_server", "str", hw_server, "Boards",
             "The hw_server xsdb connects to (xsdb backend)", at="sysmon.py:422",
             scope="board"),
        _row("boards.*.sysmon.device", "str", device, "Boards",
             "The JTAG device to read (xsdb backend)", at="sysmon.py:423", scope="board",
             advanced=True),
        _row("boards.*.sysmon.openocd", "path", "openocd", "Boards",
             "OpenOCD (openocd backend)", at="sysmon.py:434", scope="board", advanced=True),
        _row("boards.*.sysmon.adapter", "list", [], "Boards",
             "OpenOCD adapter commands (openocd backend)", at="sysmon.py:429",
             scope="board"),
        _row("boards.*.sysmon.speed_khz", "int", 1000, "Boards",
             "The adapter's JTAG speed, kHz (openocd backend; 0: leave it)",
             at="sysmon.py:435", scope="board", advanced=True, check=_not_negative),
        _row("boards.*.sysmon.search", "list", [], "Boards",
             "OpenOCD search directories (openocd backend)", at="sysmon.py:436",
             scope="board", advanced=True),
        _row("boards.*.sysmon.timeout_s", "float", None, "Boards",
             "How long one read may take (empty: 60 s xsdb, 30 s openocd)",
             at="sysmon.py:416", scope="board", advanced=True, check=_positive),
        _row("boards.*.sysmon.min_interval_s", "float", min_interval_s, "Boards",
             "The least time between SYSMON reads (s)", at="telemetry.py:397",
             scope="board", advanced=True, check=_not_negative),
        # B21
        _row("boards.*.estimates.vivado_reports", "path", "", "Boards",
             "A directory of Vivado power reports, for the power estimate",
             at="telemetry.py:407", scope="board"),
        # A.12 L1-L3 (LINUX-CLAIM): the Linux harness's SSH, claimed with your key
        _row("boards.*.ssh.user", "str", "", "Boards",
             "The Linux harness's SSH login (empty: root)", at="claim.py:321",
             scope="board", apply="reopen", check=_word("user name")),
        _row("boards.*.ssh.key", "path", "", "Boards",
             "The private key Harness Manager's ssh uses for this board (empty: your ssh "
             "default); a claim sends its .pub. The key itself is never read",
             at="claim.py:841", scope="board", apply="reopen"),
        _row("boards.*.ssh.host_key", "str", "", "Boards",
             "The board's pinned SSH host key, written by `board claim` (a changed key is "
             "refused; clear it only for a re-provisioned board)", at="claim.py:323",
             scope="board", apply="reopen", check=_host_key),
    )
