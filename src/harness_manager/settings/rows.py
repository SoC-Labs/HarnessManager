"""The core's settings rows: ``docs/design/SETTINGS.md`` Appendix A, minus the pack's.

Each row cites where the value is read (``path:line``). SET-WIRE switched those readers to
the resolver (``settings/runtime.py``: ``runtime.value(key)``), keeping the variable as the
env layer; the ``dev`` rows stay variables only (developer and test seams, SETTINGS.md §2),
and ``updates.channel``'s variable is also read alone where no user is asked
(``channel.default_channel``: harness catalogues).

**Declared by the board pack instead (SET-PACK, ``BoardPack.settings()``,
``harness_manager_mps3/settings.py``):** the MPS3 rows (``mps3.*``: T2 openocd_cfg_dir, K7
overlay_dirs, C1/C2 pacing, D4 rbb_port, D5 and X4-X8 the ``MPS3_*`` variables) and the
pack-validated board tables ``boards.*.hub.{target,board,shares.*,baud,start_shares}``,
``boards.*.xvc.*``, ``boards.*.sysmon.*``, ``boards.*.estimates.*``. ``boards.*.power.*``
stays here: its parser is the core's (``power/config.py``), for every pack.
``test_settings_schema`` and ``test_settings_pack`` hold that split: every
``HARNESS_MANAGER_*`` variable is a core row, except ``HARNESS_MANAGER_MPS3_*``, which are
all the MPS3 pack's.

**Not declared (nothing can set them today):** G5 (derived from ``BROWSER``/``DISPLAY``),
U7 install record, U12 apply budgets, K8 trust keys, B22 console baud (runtime only),
B23 harness pin (``pins.json``), C5 console history, P3 presence beat, V5 log rotation,
V7 client timeout, D6 service timeouts, X9/X10 flags. The Settings UI shows the read-only
ones from their own files (SET-API).
"""

from __future__ import annotations

import re
from typing import Any

from .schema import Setting

_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,253}$")


def _port_or_zero(v: Any) -> str:
    return "" if v == 0 or (isinstance(v, int) and 1024 <= v <= 65535) \
        else "must be 0 (automatic) or a TCP port 1024..65535"


def _channel(v: Any) -> str:
    return "" if _CHANNEL_RE.match(v) else "must be a channel name such as stable, beta or dev"


def _source(v: Any) -> str:
    if not v:
        return "must not be empty"
    if re.match(r"^[a-z][a-z0-9+.-]*:", v) and not v.startswith(
            ("https:", "http:", "github:", "file:")):
        return "must be https:..., github:OWNER/REPO, file:... or a directory"
    return ""


def _host(v: Any) -> str:
    return "" if v == "" or (_HOST_RE.match(v) and not v.startswith("-")) \
        else "must be a host name (no spaces, not starting with '-')"


def _hosts(v: Any) -> str:
    bad = [h for h in v if not h or _host(h)]
    return f"has names that are not host names: {bad}" if bad else ""


def _via(v: Any) -> str:
    if v in ("", "direct", "hub"):
        return ""
    if v.startswith("ssh:") and _host(v[4:]) == "" and v[4:]:
        return ""
    return 'must be "direct", "hub" or "ssh:HOST"'


def _name(v: Any) -> str:
    return "" if v == "" or (len(v) <= 64 and v.isprintable() and v.strip() == v) \
        else "must be 1-64 printable characters"


def _between(lo: float, hi: float, unit: str = "") -> Any:
    def check(v: Any) -> str:
        return "" if lo <= v <= hi else f"must be {lo:g}..{hi:g}{unit}"
    return check


def _positive(v: Any) -> str:
    return "" if v > 0 else "must be more than 0"


def _window(v: Any) -> str:
    return "" if re.match(r"^\d{3,5}x\d{3,5}$", v) else "must be WIDTHxHEIGHT, e.g. 1440x900"


def _stage_dir(v: Any) -> str:
    if not v or any(c.isspace() for c in v) or v.startswith(("-", "~")) \
            or ".." in v.split("/"):
        return ("must be a directory on the hub: relative to the hub user's home, or absolute; "
                "no spaces, no '~', no '..'")
    return ""


def _kit_sources(v: Any) -> str:
    bad = [s for s in v if s not in ("cache", "channel", "hub")]
    return f"has unknown sources {bad} (cache, channel, hub)" if bad else ""


def _i2c(v: Any) -> str:
    return "" if 0x08 <= v <= 0x77 else "must be a 7-bit I2C address, 0x08..0x77"


GENERAL = (
    # G1 web/static/js/theme.js:5,28-31 (stays in the browser: read before first paint)
    Setting("general.theme", "enum", "auto", "General", "Light, dark, or follow the system",
            choices=("auto", "light", "dark")),
    # G2 web/window.py:39,68-78 (named_app_browser; SET-WIRE)
    Setting("general.app_browser", "str", "", "General",
            "The browser the app window uses (empty: pywebview, else Chrome/Edge/Chromium)",
            scope="machine", env="HARNESS_MANAGER_APP_BROWSER"),
    # G3 web/window.py:41
    Setting("general.window_size", "str", "1440x900", "General", "The app window's size",
            check=_window),
    # G4 web/window.py:39,90-93
    Setting("general.app_keep_dbus", "bool", False, "General",
            "Keep the desktop bus for the app window", scope="machine", owner="dev",
            env="HARNESS_MANAGER_APP_KEEP_DBUS"),
)

HUBS = (
    # H1 transports/hub_rest.py:351-357 (a url means REST today)
    Setting("hubs.*.transport", "enum", "ssh", "Hubs", "SSH (a lab account) or REST (a token)",
            scope="hub", owner="admin", apply="reopen", choices=("ssh", "rest")),
    # H2 harness_manager_mps3/hub.py:111,141-144
    Setting("hubs.*.host", "str", "", "Hubs",
            "The hub's host name (SSH; \"local\" when Harness Manager runs on the hub)",
            scope="hub", owner="admin", apply="reopen", check=_host),
    # H3 hub.py:90,116,159
    Setting("hubs.*.group", "str", "fpga", "Hubs",
            "The group of the hub's fpgahub socket (sg; empty: none)", scope="hub",
            owner="admin", apply="reopen"),
    # H4 new (tunnel.py:36-45 honours ~/.ssh/config only)
    Setting("hubs.*.jump", "str", "", "Hubs", "An SSH jump host on the way to the hub",
            scope="hub", owner="admin", apply="reopen", check=_host),
    # H5 hub_rest.py:242,277-302
    Setting("hubs.*.url", "url", "", "Hubs", "The hub's API, https://HUB:7246",
            scope="hub", owner="admin", apply="reopen"),
    # H6 hub_rest.py:429-449 (token_file, $FPGAHUB_TOKEN with $FPGAHUB_ADDR, fpgahub login)
    Setting("hubs.*.token", "str", None, "Hubs", "The fpgahub Bearer token", scope="hub",
            secret=True, apply="reopen", env="FPGAHUB_TOKEN"),
    # H7-H11 hub_rest.py:245-250,319-328,340-342
    Setting("hubs.*.ca_file", "path", "", "Hubs",
            "The hub's CA (empty: the system's store)", scope="hub", owner="admin",
            apply="reopen"),
    Setting("hubs.*.cert_file", "path", "", "Hubs", "The mTLS client certificate (port 7245)",
            scope="hub", apply="reopen", advanced=True),
    Setting("hubs.*.key_file", "path", "", "Hubs", "The mTLS client key (port 7245)",
            scope="hub", apply="reopen", advanced=True),
    Setting("hubs.*.insecure", "bool", False, "Hubs", "Skip TLS verification (unsafe)",
            scope="hub", owner="admin", apply="reopen", advanced=True),
    Setting("hubs.*.events", "bool", True, "Hubs", "Follow the hub's event stream",
            scope="hub", owner="admin", apply="reopen", advanced=True),
    Setting("hubs.*.direct", "enum", "auto", "Hubs",
            "The data plane: route to the board, or through the tunnel", scope="hub",
            owner="admin", apply="reopen", advanced=True, choices=("auto", "never", "always")),
    # H12 hub_rest.py:105,252,332-334
    Setting("hubs.*.timeout_s", "duration", 30, "Hubs", "The REST call timeout", scope="hub",
            owner="admin", advanced=True, check=_positive),
    # H13 cli/cmd_hub.py:291,828; services/lease.py:186-193
    Setting("hubs.*.holder", "str", "", "Hubs",
            "The lease holder (SSH; empty: harness-manager-<user>@<host>)", scope="hub"),
    # H14-H16 services/lease.py:126-127,132; cli/cmd_hub.py:288,293
    Setting("hubs.*.lease_ttl", "duration", 3600, "Hubs", "The lease time asked for",
            scope="hub", owner="admin", check=_positive),
    Setting("hubs.*.request_ttl", "duration", 7200, "Hubs",
            "The lease time asked for with a request", scope="hub", owner="admin",
            advanced=True, check=_positive),
    Setting("hubs.*.queue_timeout", "duration", 3600, "Hubs",
            "How long an acquire waits in the queue", scope="hub", advanced=True),
    # H19 (MCC-FIX) harness_manager_mps3/hub_sd.py STAGE_DIR, backend_for: where the hub SD
    # door stages a .bit for fpgahubd to read (SSH hubs). fpgahubd with ProtectHome=yes cannot
    # read /home: then a group-fpga directory under /var/lib or /srv, made by the hub's admin.
    Setting("hubs.*.stage_dir", "str", ".cache/harness-manager/hub-sd", "Hubs",
            "Where the hub SD door stages a .bit on the hub (relative: the hub user's home; "
            "outside /home when fpgahubd has ProtectHome=yes)", scope="hub", owner="admin",
            advanced=True, check=_stage_dir),
)

BOARDS = (
    # B1-B2 power/config.py:189-193,201,207-222; naming.py:11-18
    Setting("boards.*.match", "list", [], "Boards",
            "Other board ids or addresses for this board", scope="board"),
    Setting("boards.*.name", "str", "", "Boards", "The board's display name", scope="board",
            check=_name),
    # B3 harness_manager_mps3/hub.py:190-196; cli/main.py:67,124 ("hub" is new)
    Setting("boards.*.via", "str", "", "Boards",
            'How to reach the board: "direct", "ssh:HOST" or "hub"', scope="board",
            apply="reopen", check=_via),
    # B4 new: the board's hub, by name (SET-HUB-1 resolves it)
    Setting("boards.*.hub.use", "ref", "", "Boards", "Which hub leases this board",
            scope="board", apply="reopen"),
    # B10-B16 power/config.py:225-302
    Setting("boards.*.power.kind", "enum", "", "Boards", "The power meter or switch",
            scope="board",
            choices=("", "shelly_gen2", "tasmota", "netio", "ina260_mcp2221")),
    Setting("boards.*.power.url", "url", "", "Boards",
            "The plug's URL (credentials in it are refused)", scope="board"),
    Setting("boards.*.power.outlet", "int", None, "Boards",
            "The outlet (empty: 0 for Shelly, else 1)", scope="board"),
    Setting("boards.*.power.auth.user", "str", "admin", "Boards", "The plug's user",
            scope="board"),
    Setting("boards.*.power.auth.password", "str", None, "Boards", "The plug's password",
            scope="board", secret=True),
    Setting("boards.*.power.timeout_s", "float", 3.0, "Boards", "The plug's timeout (s)",
            scope="board", check=_positive),
    Setting("boards.*.power.cycle", "bool", None, "Boards",
            "Allow a power cycle (empty: yes, except an INA260 meter)", scope="board"),
    Setting("boards.*.power.i2c_address", "int", 0x40, "Boards", "The INA260's I2C address",
            scope="board", advanced=True, check=_i2c),
    Setting("boards.*.power.device", "int", 0, "Boards", "Which MCP2221A", scope="board",
            advanced=True, check=_between(0, 15)),
)

# SET-HUBS: an INLINE hub table's own keys (harness_manager_mps3/hub.py:136-165 for SSH,
# transports/hub_rest.py REST_KEYS for REST), declared so a boards.toml key nobody reads can
# be flagged. They keep working as before; `harness-manager hub adopt BOARD` ("Make this a
# hub") moves them to [hubs.<name>], and a board table with `use` refuses them. The table's
# per-board keys (target, board, shares, baud, start_shares) are the board pack's rows.
_INLINE = "an inline hub table's; `hub adopt` moves it to [hubs.<name>]"
INLINE_HUB = (
    Setting("boards.*.hub.host", "str", "", "Boards", f"The hub's SSH host ({_INLINE})",
            scope="board", apply="reopen", advanced=True, check=_host),
    Setting("boards.*.hub.url", "url", "", "Boards", f"The hub's REST API ({_INLINE})",
            scope="board", apply="reopen", advanced=True),
    Setting("boards.*.hub.group", "str", "fpga", "Boards",
            f"The fpgahub socket's group ({_INLINE})", scope="board", apply="reopen",
            advanced=True),
    Setting("boards.*.hub.token_file", "path", "", "Boards",
            f"A file holding the hub token ({_INLINE}; read before $FPGAHUB_TOKEN)",
            scope="board", apply="reopen", advanced=True),
    Setting("boards.*.hub.ca_file", "path", "", "Boards", f"The hub's CA ({_INLINE})",
            scope="board", apply="reopen", advanced=True),
    Setting("boards.*.hub.cert_file", "path", "", "Boards",
            f"The mTLS client certificate ({_INLINE})", scope="board", apply="reopen",
            advanced=True),
    Setting("boards.*.hub.key_file", "path", "", "Boards", f"The mTLS client key ({_INLINE})",
            scope="board", apply="reopen", advanced=True),
    Setting("boards.*.hub.insecure", "bool", False, "Boards",
            f"Skip TLS hostname verification ({_INLINE})", scope="board", apply="reopen",
            advanced=True),
    Setting("boards.*.hub.events", "bool", True, "Boards",
            f"Follow the hub's event stream ({_INLINE})", scope="board", apply="reopen",
            advanced=True),
    Setting("boards.*.hub.direct", "enum", "auto", "Boards",
            f"The data plane's route ({_INLINE})", scope="board", apply="reopen",
            advanced=True, choices=("auto", "never", "always")),
    Setting("boards.*.hub.timeout_s", "float", 30.0, "Boards",
            f"The REST call timeout, s ({_INLINE})", scope="board", advanced=True,
            check=_positive),
)

TOOLS = (
    # T1 services/debug.py:126,153-171 (find_openocd; SET-WIRE)
    Setting("tools.openocd", "path", "", "Tools", "OpenOCD (empty: openocd on PATH)",
            scope="machine", owner="admin", env="HARNESS_MANAGER_OPENOCD"),
    # T3 services/kit/vivado.py:40,137-161 (discover; SET-WIRE)
    Setting("tools.vivado", "path", "", "Tools",
            "Vivado (empty: found under /tools, /opt, /apps; off: never)", scope="machine",
            owner="admin", env="HARNESS_MANAGER_VIVADO"),
    # T4 services/xvc.py:120,760-790 (find_hw_server; SET-WIRE)
    Setting("tools.hw_server", "path", "", "Tools",
            "hw_server (empty: Vivado's, then $XILINX_VIVADO's)", scope="machine",
            owner="admin", env="HARNESS_MANAGER_HW_SERVER"),
    # T5 services/update/app.py:83,227-231,267,310 (_uv_setting; SET-WIRE)
    Setting("tools.uv", "path", "", "Tools", "uv, for the app's self-update (empty: on PATH)",
            scope="machine", owner="admin", env="HARNESS_MANAGER_UV", advanced=True),
    # T6 services/update/github.py:148,154-180
    Setting("tools.gh", "path", "", "Tools", "The GitHub CLI, found on PATH (a token fallback)",
            readonly=True),
    # T7 services/xvc.py:779
    Setting("tools.xilinx_vivado", "path", "", "Tools", "Xilinx's own Vivado root",
            scope="machine", owner="dev", env="XILINX_VIVADO"),
    # H18 transports/hub_rest.py:388-394
    Setting("tools.fpgahub_login_store", "path", "", "Tools",
            "Where `fpgahub login` keeps its login (empty: ~/.config/fpgahub/config.toml)",
            owner="dev", env="FPGAHUB_CLIENT_CONFIG"),
)

UPDATES = (
    # U1 selfupdate.py:116; channel.py:54,105; policy.py:63. Today the user's choice beats
    # the variable (selfupdate.effective: policy, then settings.json, then the env, then
    # stable), so this row keeps that order: env_rank "under-user".
    Setting("updates.channel", "str", "stable", "Updates", "Which release channel",
            owner="admin", env="HARNESS_MANAGER_UPDATE_CHANNEL", env_rank="under-user",
            check=_channel),
    # U2 selfupdate.py:117,158-174; policy.py:62 (the admin only tightens: a ceiling)
    Setting("updates.auto", "enum", "stage", "Updates",
            "Off, notify, or stage in the background and apply on a click", owner="admin",
            choices=("off", "notify", "stage"), ceiling=True),
    # U3 policy.py:16,142-151
    Setting("updates.check_interval", "duration", 6 * 3600, "Updates",
            "How often the service checks (0: never)", scope="machine", owner="admin",
            check=lambda v: "" if v == 0 or v >= 300 else "must be 0 (never) or at least 5m"),
    # U4 channel.py:50,80,104-109 (_source_setting; SET-WIRE); cli/cmd_update.py:81
    Setting("updates.source", "str", "github:SoC-Labs/HarnessManager", "Updates",
            "Where releases come from (github:OWNER/REPO, a URL or a directory)",
            owner="admin", env="HARNESS_MANAGER_UPDATE_SOURCE", check=_source),
    # U5 download.py:77,116-122,388 (mirrors_from_env, at each download; SET-WIRE)
    Setting("updates.mirrors", "list", [], "Updates", "Mirrors tried by sha256 first",
            scope="machine", owner="admin", env="HARNESS_MANAGER_UPDATE_MIRRORS"),
    # U6 github.py:48,171-184 (resolve_token: env, the store, gh; SET-WIRE); download.py:108
    Setting("updates.github_token", "str", None, "Updates",
            "A GitHub token for the private (Arm-IP) parts", secret=True,
            env="HARNESS_MANAGER_GITHUB_TOKEN"),
    # U8-U11 developer seams
    Setting("updates.github_api", "url", "https://api.github.com", "Updates",
            "The GitHub API host", scope="machine", owner="dev",
            env="HARNESS_MANAGER_GITHUB_API"),
    Setting("updates.first_check_s", "float", 60.0, "Updates",
            "The delay before the service's first check", scope="machine", owner="dev",
            env="HARNESS_MANAGER_UPDATE_FIRST_CHECK_S", apply="restart"),
    Setting("updates.no_self_update", "bool", False, "Updates",
            "The launcher ignores the self-update pointer", scope="machine", owner="dev",
            env="HARNESS_MANAGER_NO_SELF_UPDATE"),
    Setting("updates.use_installed", "bool", False, "Updates",
            "Run the installer's version", scope="machine", owner="dev",
            env="HARNESS_MANAGER_USE_INSTALLED"),
    # U8 (HUB-SD) services/update/service.py sd_ab_setting: the config SD A/B by pointer.
    # Off until the 10-minute board check proves the MCC loads another 8.3 F0FILE name.
    Setting("updates.sd_ab", "bool", False, "Updates",
            "Install the config SD A/B by pointer (off until its board check)",
            scope="machine", owner="admin", advanced=True),
)

KITS = (
    # K1-K2 cli/cmd_harness.py:69,73; daemon/harness_api.py:108
    Setting("harness.channels", "list", ["stable"], "Harness + kits",
            "The channels the harness versions list shows"),
    Setting("harness.source", "str", "", "Harness + kits",
            "The harness catalogue source (empty: the update source)", owner="admin"),
    # K3-K4 planned (HARNESS_DISTRIBUTION.md:288-292; DUT_BUILD_KIT_STORAGE.md:222)
    Setting("harness.cache_max", "size", 2_000_000_000, "Harness + kits",
            "The harness download cache's cap", scope="machine", owner="admin"),
    Setting("kits.cache_max", "size", 0, "Harness + kits",
            "The kit cache's cap (0: no cap)", scope="machine", owner="admin"),
    # K5-K6 services/kit/service.py:80,83-86,250-260 (KitService.hub; SET-WIRE); cli/cmd_kit.py:94
    Setting("kits.sources", "list", ["cache", "channel", "hub"], "Harness + kits",
            "Where a kit is looked for, in order", check=_kit_sources),
    Setting("kits.hub_dir", "path", "", "Harness + kits", "The hub's mint archive",
            scope="machine", owner="admin", env="HARNESS_MANAGER_KIT_HUB_DIR"),
    # K7 is the pack's (mps3.overlay_dirs): each pack has its own variable
    # (cli/cmd_program.py:23-25, HARNESS_MANAGER_<PACK>_OVERLAY_DIRS).
    # K9 cli/cmd_kit.py:121
    Setting("kits.jobs", "int", 2, "Harness + kits", "Vivado threads for a kit build",
            advanced=True, check=_between(1, 64)),
)

DEBUG = (
    # D1 services/debug.py:127,872-880 (_pinned_base; SET-WIRE)
    Setting("debug.port_base", "int", 0, "Debug",
            "Pin the gdb/telnet/tcl ports (0: a slot per board from 23300)", scope="machine",
            owner="admin", env="HARNESS_MANAGER_DEBUG_PORT_BASE", apply="restart",
            advanced=True, check=_port_or_zero),
    # D2 services/xvc.py:121,1221-1230 (_pinned_base; SET-WIRE)
    Setting("debug.xvc_port_base", "int", 0, "Debug",
            "Pin the XVC relay and hw_server ports (0: a slot per board from 23600)",
            scope="machine", owner="admin", env="HARNESS_MANAGER_XVC_PORT_BASE",
            apply="restart", advanced=True, check=_port_or_zero),
    # D3 cli/cmd_xvc.py:97,109; daemon/xvc_api.py:82,107
    Setting("debug.hw_server_mode", "enum", "own", "Debug",
            "Run Harness Manager's own hw_server, or bring your own", choices=("own", "byo")),
)

CONSOLES = (
    # C3-C4 web/static/js/sections/consoles.js:14,44-53
    Setting("consoles.line_ending", "enum", "crlf", "Consoles", "What Enter sends",
            choices=("crlf", "cr", "lf")),
    Setting("consoles.scrollback", "int", 5000, "Consoles", "Lines kept in a console",
            check=_between(100, 1_000_000)),
    Setting("consoles.font_size", "int", 13, "Consoles", "The console font size (px)",
            check=_between(8, 32)),
    # C6 services/pty.py:107,186-189
    Setting("consoles.pty_dir", "path", "", "Consoles",
            "Where the PTY links are (empty: /tmp/harness-manager-$USER)", owner="dev",
            env="HARNESS_MANAGER_PTY_DIR", apply="restart"),
)

PANEL = (
    # P1 services/presence.py:89-90; cli/cmd_panel.py:40; sections/panel.js:29
    Setting("panel.identify_s", "int", 10, "General", "How long Identify blinks (s)",
            check=_between(1, 30)),
    # P2 services/presence.py:98-108,214
    Setting("panel.presence_who", "str", "", "General",
            "The name a board's panel shows for you (empty: user@host)", owner="admin",
            check=_name),
)

ADVANCED = (
    # V1 settings/files.py:55,67-86 (config_dir, the one rule: SET-WIRE); engine.py:80
    Setting("advanced.state_dir", "path", "~/.config/harness-manager", "Advanced",
            "Where settings and state live (set $HARNESS_MANAGER_STATE_DIR to move it)",
            env="HARNESS_MANAGER_STATE_DIR", apply="restart", readonly=True),
    # V2-V4 daemon/server.py:340-366,391-393,495-503,521 (start_setting; SET-WIRE); cli/cmd_daemon.py:69-83
    Setting("advanced.port", "int", 0, "Advanced", "The service's TCP port (0: any free one)",
            scope="machine", apply="restart", check=_port_or_zero),
    Setting("advanced.listen", "str", "127.0.0.1", "Advanced",
            "The service's bind address (off loopback, it warns)", scope="machine",
            owner="admin", apply="restart", check=_host),
    # SET-API daemon/hosts.py, server.py host_allow_list (SETTINGS.md §12.8)
    Setting("advanced.allowed_hosts", "list", [], "Advanced",
            "Other host names the service answers to (loopback and its listen address "
            "always work)", scope="machine", owner="admin", apply="restart",
            advanced=True, check=_hosts),
    Setting("advanced.log_level", "enum", "info", "Advanced", "Log verbosity",
            scope="machine", apply="restart",
            choices=("critical", "error", "warning", "info", "debug")),
    # V6 scripts/install.sh:39-41; _launch.py:9
    Setting("advanced.home", "path", "~/.local/share/harness-manager", "Advanced",
            "Where the installer put Harness Manager", scope="machine", owner="installer",
            env="HARNESS_MANAGER_HOME", readonly=True),
    # X1-X3 cli/engine.py:40-41,77-78,109,124; cli/main.py:426
    Setting("dev.no_daemon", "bool", False, "Advanced", "The CLI never uses the service",
            owner="dev", env="HARNESS_MANAGER_NO_DAEMON"),
    Setting("dev.cli_engine", "str", "", "Advanced", "An engine factory, package.module:name",
            owner="dev", env="HARNESS_MANAGER_CLI_ENGINE"),
    Setting("dev.debug", "bool", False, "Advanced", "Tracebacks on internal errors",
            owner="dev", env="HARNESS_MANAGER_DEBUG"),
    # SET-CORE: the secret store's backend (tests and headless CI force the file)
    Setting("dev.keyring", "enum", "auto", "Advanced",
            "The secret store: the OS keyring when reachable (auto), or never (off)",
            owner="dev", env="HARNESS_MANAGER_KEYRING", choices=("auto", "off")),
)

CORE_ROWS: tuple[Setting, ...] = (GENERAL + HUBS + BOARDS + INLINE_HUB + TOOLS + UPDATES + KITS
                                  + DEBUG + CONSOLES + PANEL + ADVANCED)

#: Variables that name output markers or install paths, not settings.
NOT_SETTINGS_ENV = frozenset({"HARNESS_MANAGER_SYSMON", "HARNESS_MANAGER_SYSMON_ERR"})
