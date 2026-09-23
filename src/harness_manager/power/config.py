"""Per-board add-on settings: ``boards.toml``.

The file lives in the state directory (``$HARNESS_MANAGER_STATE_DIR``, else
``~/.config/harness-manager``; the engine's rule, ``harness_manager.engine.resolve_state_dir``).
A missing file is not an error: it means "nothing configured".

Schema (every table is optional)::

    # The table key is the board_id, as `harness-manager probe` prints it.
    [boards."mps3@192.168.10.101:6900"]
    match = ["192.168.10.101"]        # optional: other board ids or link addresses for this board

    [boards."mps3@192.168.10.101:6900".power]
    kind = "shelly_gen2"              # shelly_gen2 | tasmota | netio | ina260_mcp2221
    url = "http://192.168.1.50"       # the plug or PDU (not used by ina260_mcp2221)
    outlet = 0                        # shelly switch id (default 0), tasmota relay (1), netio output ID (1)
    timeout_s = 3.0
    cycle = true                      # false: never power-cycle through this outlet
    auth = { user = "admin", password_env = "SHELLY_PASSWORD" }
    #        or password = "...", or password_file = "~/.config/harness-manager/shelly.pw"
    i2c_address = 0x40                # ina260_mcp2221 only
    device = 0                        # ina260_mcp2221 only: which MCP2221A

    [boards."mps3@192.168.10.101:6900".sysmon]      # read by the board pack (MPS3: sysmon.py)
    [boards."mps3@192.168.10.101:6900".estimates]   # read by the board pack (vivado_reports = "<dir>")

Secrets: a password is held in a ``Secret``, whose ``repr``/``str`` is ``***``.
No message, log line, reading, ``Link`` or ``repr`` built here ever contains
one. Credentials in the URL itself are refused, because a URL is shown to users.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from harness_manager.core.errors import UsageError
from harness_manager.core.model import Candidate, Link, LinkKind

log = logging.getLogger(__name__)

BOARDS_FILE = "boards.toml"
KINDS = ("shelly_gen2", "tasmota", "netio", "ina260_mcp2221")
HTTP_KINDS = ("shelly_gen2", "tasmota", "netio")
DEFAULT_OUTLET = {"shelly_gen2": 0, "tasmota": 1, "netio": 1}
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_USER = "admin"            # Shelly Gen2's fixed digest user; Tasmota and NETIO default


class ConfigError(UsageError):
    """``boards.toml`` is unreadable or a table in it is wrong. Never carries a secret."""


class Secret:
    """A password. Prints as ``***``; ``reveal()`` is the only way to the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class Auth:
    user: str
    password: Secret


@dataclass(frozen=True)
class PowerConfig:
    kind: str
    url: str = ""                 # never holds credentials (refused at load time)
    outlet: int = 0
    auth: Auth | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    cycle: bool = True
    i2c_address: int = 0x40
    device: int = 0

    @property
    def address(self) -> str:
        """Where the meter is, safe to show: the URL without query or credentials."""
        if self.kind == "ina260_mcp2221":
            return f"mcp2221://{self.device}/0x{self.i2c_address:02x}"
        return safe_url(self.url)

    @property
    def label(self) -> str:
        """``shelly_gen2 http://192.168.1.50 outlet 0``: for sources and messages."""
        if self.kind == "ina260_mcp2221":
            return f"ina260 at 0x{self.i2c_address:02x} on MCP2221A #{self.device}"
        return f"{self.kind} {self.address} outlet {self.outlet}"


@dataclass(frozen=True)
class BoardConfig:
    key: str                                   # the table key (normally a board_id)
    match: tuple[str, ...] = ()
    power: PowerConfig | None = None
    power_error: str = ""                      # why the power table cannot be used
    tables: Mapping[str, Any] = field(default_factory=dict)   # sysmon, estimates, ... (for packs)
    path: Path | None = None

    @property
    def has_power(self) -> bool:
        return self.power is not None or bool(self.power_error)


@dataclass(frozen=True)
class BoardsConfig:
    boards: tuple[BoardConfig, ...] = ()
    path: Path | None = None

    def for_board(self, board_id: str, links: Iterable[Link] = ()) -> BoardConfig | None:
        """The table for this board: an exact key first, then the first ``match`` hit."""
        for b in self.boards:
            if b.key == board_id:
                return b
        names = _names_for(board_id, links)
        for b in self.boards:
            if names & set(b.match):
                return b
        return None


# --- loading -------------------------------------------------------------------------


def default_path() -> Path:
    from harness_manager.engine import resolve_state_dir  # the one state-dir rule

    return resolve_state_dir() / BOARDS_FILE


def load_boards(path: Path | None = None) -> BoardsConfig:
    """Read ``boards.toml``. A missing file is empty; a malformed one is ``ConfigError``."""
    path = Path(path) if path is not None else default_path()
    if not path.is_file():
        return BoardsConfig(path=path)
    try:
        import tomllib
    except ModuleNotFoundError:           # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise ConfigError(f"cannot read {path}: Python 3.10 needs the 'tomli' package",
                              hint="pip install tomli, or use Python 3.11+") from exc
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    boards = data.get("boards", {})
    if not isinstance(boards, dict):
        raise ConfigError(f"{path}: [boards] must be a table of board tables")
    out: list[BoardConfig] = []
    for key, table in boards.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: boards.{key!r} must be a table")
        out.append(_board(path, key, table))
    _warn_if_readable(path, data)
    return BoardsConfig(tuple(out), path)


def _board(path: Path, key: str, table: dict[str, Any]) -> BoardConfig:
    match = table.get("match", [])
    if isinstance(match, str):
        match = [match]
    if not isinstance(match, list) or not all(isinstance(m, str) for m in match):
        raise ConfigError(f"{path}: boards.{key!r}.match must be a list of strings")
    power: PowerConfig | None = None
    power_error = ""
    if "power" in table:
        try:
            power = parse_power(table["power"], where=f"boards.{key!r}.power")
        except ConfigError as exc:
            power_error = f"{path.name}: {exc.message}"
    others = {k: v for k, v in table.items() if k not in ("match", "power")}
    return BoardConfig(key=key, match=tuple(match), power=power, power_error=power_error,
                       tables=MappingProxyType(others), path=path)


def parse_power(table: Any, *, where: str = "power") -> PowerConfig:
    """Validate one ``power`` table. ``ConfigError`` messages never contain a secret."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    kind = table.get("kind")
    if kind not in KINDS:
        raise ConfigError(f"{where}.kind must be one of {', '.join(KINDS)} (got {kind!r})")
    unknown = set(table) - {"kind", "url", "outlet", "auth", "timeout_s", "cycle",
                            "i2c_address", "device"}
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    url = table.get("url", "")
    if kind in HTTP_KINDS:
        if not isinstance(url, str) or not url:
            raise ConfigError(f"{where}.url is required for kind {kind!r}")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(f"{where}.url must be http://host[:port] (got {safe_url(url)!r})")
        if parts.username or parts.password:
            raise ConfigError(f"{where}.url must not hold credentials; put them in {where}.auth")
        if parts.query:
            raise ConfigError(f"{where}.url must not have a query string")
    outlet = table.get("outlet", DEFAULT_OUTLET.get(kind, 0))
    if not isinstance(outlet, int) or isinstance(outlet, bool) or outlet < 0:
        raise ConfigError(f"{where}.outlet must be a non-negative integer")
    if kind in ("tasmota", "netio") and outlet < 1:
        raise ConfigError(f"{where}.outlet counts from 1 for kind {kind!r}")
    timeout_s = table.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not 0 < timeout_s <= 60:
        raise ConfigError(f"{where}.timeout_s must be a number of seconds in (0, 60]")
    cycle = table.get("cycle", kind != "ina260_mcp2221")
    if not isinstance(cycle, bool):
        raise ConfigError(f"{where}.cycle must be true or false")
    i2c_address = table.get("i2c_address", 0x40)
    device = table.get("device", 0)
    if kind == "ina260_mcp2221":
        if not isinstance(i2c_address, int) or not 0x40 <= i2c_address <= 0x4F:
            raise ConfigError(f"{where}.i2c_address must be an INA260 address, 0x40..0x4f")
        if not isinstance(device, int) or device < 0:
            raise ConfigError(f"{where}.device must be a non-negative integer")
    auth = _auth(table.get("auth"), where=f"{where}.auth") if "auth" in table else None
    return PowerConfig(kind=kind, url=url if kind in HTTP_KINDS else "", outlet=outlet,
                       auth=auth, timeout_s=float(timeout_s), cycle=cycle,
                       i2c_address=i2c_address, device=device)


def _auth(table: Any, *, where: str) -> Auth:
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table: {{ user = ..., password_env = ... }}")
    unknown = set(table) - {"user", "password", "password_env", "password_file"}
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    user = table.get("user", DEFAULT_USER)
    if not isinstance(user, str) or not user:
        raise ConfigError(f"{where}.user must be a non-empty string")
    given = [k for k in ("password", "password_env", "password_file") if k in table]
    if len(given) != 1:
        raise ConfigError(f"{where} needs exactly one of password, password_env, password_file")
    how = given[0]
    raw = table[how]
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{where}.{how} must be a non-empty string")
    if how == "password":
        return Auth(user, Secret(raw))
    if how == "password_env":
        value = os.environ.get(raw)
        if not value:
            raise ConfigError(f"{where}.password_env: ${raw} is not set")
        return Auth(user, Secret(value))
    pw_path = Path(raw).expanduser()
    try:
        value = pw_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"{where}.password_file: cannot read {pw_path}: "
                          f"{exc.strerror or type(exc).__name__}") from exc
    if not value:
        raise ConfigError(f"{where}.password_file: {pw_path} is empty")
    return Auth(user, Secret(value))


def _warn_if_readable(path: Path, data: dict[str, Any]) -> None:
    """An inline password in a file other users can read deserves a warning (not the password)."""
    if os.name != "posix" or not _has_inline_password(data):
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("%s holds a password and other users can read it (mode %o); "
                    "run chmod 600 on it, or use password_env/password_file",
                    path, stat.S_IMODE(mode))


def _has_inline_password(data: dict[str, Any]) -> bool:
    for table in (data.get("boards") or {}).values():
        power = table.get("power") if isinstance(table, dict) else None
        auth = power.get("auth") if isinstance(power, dict) else None
        if isinstance(auth, dict) and "password" in auth:
            return True
    return False


# --- helpers for packs -------------------------------------------------------------------


def safe_url(url: str) -> str:
    """``scheme://host[:port]/path``: no credentials, query or fragment. Safe to show and log."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme or not host:
        return "<unparseable url>"
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def _names_for(board_id: str, links: Iterable[Link]) -> set[str]:
    names = {board_id}
    if "@" in board_id:
        names.add(board_id.split("@", 1)[1])
    for lk in links:
        names.add(lk.address)
        host, sep, port = lk.address.rpartition(":")
        if sep and port.isdigit() and host:
            names.add(host.strip("[]"))
    return names


def power_link(board: BoardConfig | None) -> Link | None:
    """The ``SMART_POWER`` link a configured meter gives a candidate (address is secret-free)."""
    if board is None or board.power is None:
        return None
    p = board.power
    detail = f"{p.kind} outlet {p.outlet} ({BOARDS_FILE})" if p.kind != "ina260_mcp2221" \
        else f"INA260 on the 12 V input ({BOARDS_FILE})"
    return Link(LinkKind.SMART_POWER, p.address, detail)


def with_links(candidate: Candidate, extra: Iterable[Link]) -> Candidate:
    """``candidate`` plus ``extra`` links it does not already have (same kind and address)."""
    seen = {(lk.kind, lk.address) for lk in candidate.links}
    add = tuple(lk for lk in extra if (lk.kind, lk.address) not in seen)
    if not add:
        return candidate
    return Candidate(pack=candidate.pack, board_id=candidate.board_id,
                     links=candidate.links + add, label=candidate.label,
                     evidence=candidate.evidence, identity=candidate.identity)
