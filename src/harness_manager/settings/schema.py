"""The settings schema: every setting is declared once, as a ``Setting`` row (lane SET-CORE).

Design: ``docs/design/SETTINGS.md`` §3. A row says what a setting is (its key, type,
default and help), where it lives (``scope``), who may set it (``owner``), whether it is a
secret, what a change needs (``apply``) and the developer variable that overrides it
(``env``). The Settings menu, ``harness-manager config`` and the API are views of these rows.

**Keys** are TOML dotted keys: ``updates.channel``, ``hubs.*.url``,
``boards."mps3@192.168.10.101:6900".name``. A ``*`` part in a declared key stands for one
instance (a hub name, a board key). A part that is not bare (``A-Z a-z 0-9 _ -``) is quoted,
exactly as TOML quotes it, so a board key with dots in it stays one part.

**Owner** (Appendix A's column) decides who may set it:

- ``user``: the user's own choice. The admin policy cannot lock or pre-set it.
- ``admin``: the user's choice too, but the admin policy may lock it (``[lock]``) or
  pre-set it for the machine (``[default]``).
- ``dev``: a developer or test seam. Environment only, never in the menu (``ui`` is False);
  ``config list --all`` shows it.
- ``installer`` / ``build``: read-only facts shown for reference.

**Apply** says what a change needs: ``live`` (read at each use), ``reopen`` (the next board
open picks it up) or ``restart`` (the service must restart: the design's ``daemon``).

This module is stdlib-only, so anything may import it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from harness_manager.core.errors import UsageError

TYPES = ("str", "int", "float", "bool", "enum", "path", "url", "duration", "size", "list",
         "ref")
SCOPES = ("user", "machine", "board", "hub", "pack")
APPLY = ("live", "reopen", "restart")
OWNERS = ("user", "admin", "dev", "installer", "build")
ENV_RANKS = ("over-user", "under-user")
ENV_SPLITS = ("words", "pathsep")

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZES = {"": 1, "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4,
          "ki": 1024, "mi": 1024**2, "gi": 1024**3, "ti": 1024**4}


# --- keys --------------------------------------------------------------------------------------


def split_key(key: str) -> tuple[str, ...]:
    """``'boards."a.b".name'`` -> ``("boards", "a.b", "name")``. ``UsageError`` if malformed.

    TOML's dotted-key syntax: bare parts, or ``"basic"`` (with ``\\"`` and ``\\\\``) or
    ``'literal'`` quoted parts. A bare ``*`` is allowed (a declared key's instance slot).
    """
    if not isinstance(key, str) or not key.strip():
        raise UsageError(f"{key!r} is not a setting key")
    parts: list[str] = []
    i, n = 0, len(key)
    while True:
        while i < n and key[i] in " \t":
            i += 1
        if i >= n:
            raise UsageError(f"{key!r} is not a setting key (it ends with a dot)")
        if key[i] in "\"'":
            quote, j, buf = key[i], i + 1, []
            while j < n and key[j] != quote:
                if quote == '"' and key[j] == "\\" and j + 1 < n and key[j + 1] in "\"\\":
                    j += 1
                buf.append(key[j])
                j += 1
            if j >= n:
                raise UsageError(f"{key!r} is not a setting key (an unclosed quote)")
            parts.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and key[j] not in ". \t":
                j += 1
            part = key[i:j]
            if not (part == "*" or _BARE.match(part)):
                raise UsageError(f"{key!r} is not a setting key ({part!r} needs quotes)")
            parts.append(part)
            i = j
        while i < n and key[i] in " \t":
            i += 1
        if i >= n:
            return tuple(parts)
        if key[i] != ".":
            raise UsageError(f"{key!r} is not a setting key")
        i += 1


def quote_part(part: str) -> str:
    if part == "*" or _BARE.match(part):
        return part
    return '"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"'


def join_key(parts: Sequence[str]) -> str:
    """The canonical spelling: ``("boards", "a.b", "name")`` -> ``'boards."a.b".name'``."""
    return ".".join(quote_part(p) for p in parts)


def canonical_key(key: str) -> str:
    return join_key(split_key(key))


def matches(pattern: Sequence[str], parts: Sequence[str]) -> bool:
    """A declared key's parts against a concrete key's: same length, ``*`` matches one part."""
    return len(pattern) == len(parts) and all(p == "*" or p == q for p, q in zip(pattern, parts, strict=True))


# --- values ------------------------------------------------------------------------------------


def parse_duration(value: Any) -> int:
    """``3600``, ``"90m"``, ``"12h"``, ``"1d"`` -> whole seconds. ``ValueError`` otherwise.

    The same rule as the self-update policy's ``check_interval`` (``policy.parse_interval``),
    kept here so the settings package does not import the update package.
    """
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a time")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", str(value), re.IGNORECASE)
        if not m:
            raise ValueError(f"{value!r} is not a time (say 3600, 90m, 12h or 1d)")
        seconds = float(m.group(1)) * _UNITS[(m.group(2) or "s").lower()]
    if seconds < 0:
        raise ValueError(f"{value!r} is negative")
    return int(seconds)


def parse_size(value: Any) -> int:
    """``1073741824``, ``"1GiB"``, ``"2G"``, ``"500MB"``, ``"64k"`` -> bytes."""
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a size")
    if isinstance(value, int):
        n = value
    else:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]i?)?b?\s*$", str(value), re.IGNORECASE)
        if not m:
            raise ValueError(f"{value!r} is not a size (say 500M, 2G or 1GiB)")
        n = int(float(m.group(1)) * _SIZES[(m.group(2) or "").lower()])
    if n < 0:
        raise ValueError(f"{value!r} is negative")
    return n


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


# --- the row -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    key: str                          # "updates.channel", "hubs.*.url", "mps3.console.pace_ms"
    type: str
    default: Any
    section: str                      # the Settings page section it shows in
    doc: str                          # one line for the menu, `config list` and --help
    scope: str = "user"               # user | machine | board | hub | pack
    secret: bool = False              # the value lives in the secret store, never in a file
    apply: str = "live"               # live | reopen | restart
    owner: str = "user"               # user | admin | dev | installer | build
    env: str = ""                     # the developer override ("" = none)
    env_rank: str = "over-user"       # "under-user": the user's file beats the variable
    env_split: str = "words"          # a list from env: split on spaces , ; | on os.pathsep
    choices: tuple[Any, ...] = ()     # enum: in order (a cap compares by position)
    readonly: bool = False            # shown, never set here (the doc says where it is set)
    ceiling: bool = False             # an admin value is a ceiling, never a floor (a cap)
    advanced: bool = False            # the menu shows it under "Advanced"
    pack: str = ""                    # the board pack that declared it ("" = the core)
    check: Callable[[Any], str] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        problems = []
        if self.type not in TYPES:
            problems.append(f"type {self.type!r}")
        if self.scope not in SCOPES:
            problems.append(f"scope {self.scope!r}")
        if self.apply not in APPLY:
            problems.append(f"apply {self.apply!r}")
        if self.owner not in OWNERS:
            problems.append(f"owner {self.owner!r}")
        if self.env_rank not in ENV_RANKS or self.env_split not in ENV_SPLITS:
            problems.append("env_rank/env_split")
        if self.type == "enum" and not self.choices:
            problems.append("an enum needs choices")
        if self.ceiling and self.type != "enum":
            problems.append("only an enum can have a ceiling")
        try:
            parts = split_key(self.key)
        except UsageError as exc:
            problems.append(exc.message)
        else:
            if canonical_key(self.key) != self.key:
                problems.append(f"spell it {canonical_key(self.key)!r}")
            if parts[0] in ("boards", "hubs") and (len(parts) < 3 or parts[1] != "*"):
                problems.append(f"a {parts[0]} row is keyed {parts[0]}.*.<field>")
        if problems:
            raise ValueError(f"setting {self.key!r}: " + "; ".join(problems))

    @cached_property
    def parts(self) -> tuple[str, ...]:
        return split_key(self.key)          # cached: every lookup and extend compares parts

    @property
    def lockable(self) -> bool:
        """May the admin policy lock or pre-set it? Only ``admin`` rows, never a secret."""
        return self.owner == "admin" and not self.secret and not self.readonly

    @property
    def ui(self) -> bool:
        """Shown in the menu? Developer seams are not (``config list --all`` shows them)."""
        return self.owner != "dev"

    @property
    def collection(self) -> str:
        """``hubs`` / ``boards`` for an instance row, else ``""``."""
        parts = self.parts
        return parts[0] if len(parts) > 1 and parts[1] == "*" else ""

    def view(self) -> dict[str, Any]:
        """The row without a value (``GET /settings/schema``)."""
        return {"key": self.key, "type": self.type, "default": None if self.secret else self.default,
                "section": self.section, "doc": self.doc, "scope": self.scope,
                "secret": self.secret, "apply": self.apply, "owner": self.owner,
                "env": self.env, "env_rank": self.env_rank, "choices": list(self.choices),
                "readonly": self.readonly, "lockable": self.lockable, "ui": self.ui,
                "ceiling": self.ceiling, "advanced": self.advanced, "pack": self.pack,
                **self.bounds_view()}

    def bounds_view(self) -> dict[str, Any]:
        """SET-UI: a number's range, when its check declares one (``check.bounds = (lo, hi)``,
        inclusive, ``None`` an open end; ``check.min_exclusive``: "more than ``lo``"). The menu
        uses it for the input's limits and to refuse a value before sending it; the check
        stays the judge. An int's "more than ``lo``" is ``lo + 1``, inclusive."""
        b = getattr(self.check, "bounds", None) if self.type in ("int", "float") else None
        if not b:
            return {"bounds": None, "min_exclusive": False}
        lo, hi = b
        above = bool(getattr(self.check, "min_exclusive", False)) and lo is not None
        if above and self.type == "int":
            lo, above = int(lo) + 1, False
        return {"bounds": [lo, hi], "min_exclusive": above}


def coerce(spec: Setting, raw: Any, *, from_env: bool = False, concrete: str | None = None) -> Any:
    """A layer's raw value as the setting's type. ``UsageError`` says why not.

    Values from the environment are text, so they are parsed; values from a TOML file
    already have a type, and must have the right one (``"3"`` is not an ``int``).
    ``concrete``: the key being resolved or set (``boards.lab.hub.shares.fpga_uart1``), for
    a check whose rule depends on the name (``check_value``).
    """
    key, v = spec.key, raw
    t = spec.type
    if t in ("str", "path", "url", "ref", "enum"):
        if not isinstance(v, str):
            raise UsageError(f"{key} must be text, not {type(v).__name__}")
        if from_env:
            v = v.strip()
        if t == "enum" and v not in spec.choices:
            raise UsageError(f"{key} must be one of {', '.join(map(str, spec.choices))}, "
                             f"not {v!r}")
        if t == "url" and v and not re.match(r"^https?://[^\s/]+", v):
            raise UsageError(f"{key} must be an http:// or https:// URL, not {v!r}")
    elif t == "int":
        if from_env:
            text = str(raw).strip()
            try:
                v = int(text)
            except ValueError:
                try:
                    v = int(text, 0)              # 0x40
                except ValueError:
                    raise UsageError(f"{key}: {raw!r} is not a whole number") from None
        if not isinstance(v, int) or isinstance(v, bool):
            raise UsageError(f"{key} must be a whole number")
    elif t == "float":
        if from_env:
            try:
                v = float(str(raw).strip())
            except ValueError:
                raise UsageError(f"{key}: {raw!r} is not a number") from None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise UsageError(f"{key} must be a number")
        v = float(v)
    elif t == "bool":
        if from_env:
            low = str(raw).strip().lower()
            if low not in _TRUE + _FALSE:
                raise UsageError(f"{key}: {raw!r} is not true or false (1/0, yes/no, on/off)")
            v = low in _TRUE
        if not isinstance(v, bool):
            raise UsageError(f"{key} must be true or false")
    elif t == "duration":
        try:
            v = parse_duration(raw)
        except ValueError as exc:
            raise UsageError(f"{key}: {exc}") from None
    elif t == "size":
        try:
            v = parse_size(raw)
        except ValueError as exc:
            raise UsageError(f"{key}: {exc}") from None
    elif t == "list":
        if isinstance(raw, str):
            if spec.env_split == "pathsep":
                import os

                v = [p for p in raw.split(os.pathsep) if p.strip()]
            else:
                v = [p for p in re.split(r"[\s,;]+", raw) if p]
        if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
            raise UsageError(f"{key} must be a list of strings")
    why = check_value(spec, v, concrete)
    if why:
        keyed = concrete if concrete and getattr(spec.check, "with_key", False) else key
        raise UsageError(f"{keyed} {why}")
    return v


def check_value(spec: Setting, v: Any, concrete: str | None = None) -> str:
    """The row's check on a typed value ("" when it passes). A check marked ``with_key`` also
    gets the concrete key, for a rule that depends on the name: the MPS3 pack's hub shares,
    where ``mcc`` may name tty_00 (the MCC console's path) and no share may use it (MCC-FIX;
    SET-UI-MERGE)."""
    if not spec.check:
        return ""
    if getattr(spec.check, "with_key", False):
        return spec.check(v, concrete or spec.key)
    return spec.check(v)


# --- the schema --------------------------------------------------------------------------------


class Schema:
    """Every declared row: the core's (``rows.CORE_ROWS``) plus each pack's (``extend``)."""

    def __init__(self, *groups: Iterable[Setting]) -> None:
        self._rows: list[Setting] = []
        for g in groups:
            self.extend(g)

    @property
    def rows(self) -> tuple[Setting, ...]:
        return tuple(self._rows)

    def extend(self, rows: Iterable[Setting], *, pack: str = "") -> Schema:
        """Add rows. A pack's rows (SET-PACK: ``BoardPack.settings()``) must carry its name
        and sit under its prefix (``mps3.``) or in a board table (``boards.*.<table>``)."""
        new = list(rows)
        for i, s in enumerate(new):
            if pack:
                if s.pack != pack:
                    raise ValueError(f"{s.key}: declared by pack {pack!r} but says {s.pack!r}")
                if not (s.key.startswith(f"{pack}.") or s.collection == "boards"):
                    raise ValueError(f"{s.key}: a {pack} row sits under '{pack}.' "
                                     "or 'boards.*.'")
            for old in (*self._rows, *new[:i]):      # SET-PACK: twice in one batch, too
                if s.key == old.key or matches(old.parts, s.parts) or matches(s.parts, old.parts):
                    raise ValueError(f"setting {s.key!r} is declared twice "
                                     f"(also {old.key!r}{' by ' + old.pack if old.pack else ''})")
        self._rows.extend(new)
        return self

    def find(self, key: str) -> Setting | None:
        parts = split_key(key)
        for s in self._rows:
            if s.parts == parts:
                return s
        for s in self._rows:
            if matches(s.parts, parts):
                return s
        return None

    def spec(self, key: str) -> Setting:
        s = self.find(key)
        if s is None:
            raise UsageError(f"no such setting: {key}",
                             hint="`harness-manager config list --all` shows them all")
        return s

    def sections(self) -> list[str]:
        out: list[str] = []
        for s in self._rows:
            if s.section not in out:
                out.append(s.section)
        return out
