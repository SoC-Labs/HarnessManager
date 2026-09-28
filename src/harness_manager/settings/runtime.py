"""A setting's value where it is used: the helper every reader calls (lane SET-WIRE).

    >>> from harness_manager.settings import runtime
    >>> runtime.value("tools.openocd")            # "" when nothing sets it
    >>> runtime.resolved("tools.openocd").where   # "$HARNESS_MANAGER_OPENOCD", "settings.toml"

The value is the resolver's (``resolve.py``, ``docs/design/SETTINGS.md`` §4.2): the admin's
``[lock]``, the developer's variable, the user's ``settings.toml``, the admin's
``[default]``, the pack's default, the schema's. So a variable that is set means what it
always meant, and ``settings.toml`` (the Settings menu, ``harness-manager config``) speaks
where nothing above it does. Two rows keep an older order, and the resolver holds both:
``updates.channel`` puts the user's file above its variable (``env_rank="under-user"``),
and a lock on ``updates.auto`` is a cap (``ceiling``).

**Which files:** ``files.config_dir(state_dir)``: the reader's own state dir when it knows
it (an engine's), else the service's (``files.use_config_dir``, which ``run_daemon`` sets,
so a ``--state-dir`` or ``--demo`` service reads its own files, never yours), else
``$HARNESS_MANAGER_STATE_DIR``, else ``~/.config/harness-manager``.

**When a change takes effect** (the row's ``apply``):

- ``live``: at the next use. The files are read again whenever their bytes change (a hand
  edit, ``harness-manager config set``, ``PUT /settings``), and the service also drops the
  cache on ``settings.changed`` (``watch``).
- ``reopen``: at the next use too; the readers of these rows run when a board opens.
- ``restart``: the value the process started with. The first read of a config dir keeps a
  snapshot of its files, and ``restart`` rows resolve against that snapshot for the life of
  the process (the service takes it as it starts: ``prime``). The environment is the
  process's own, so a variable still wins.

**A bad value** in a file is skipped with a problem, as the Settings menu shows it, and the
next layer speaks; the problem is logged once. A bad value in the developer's own variable
raises ``UsageError`` naming the variable (``strict``, the default): quietly falling back
to a default (GitHub, an automatic port) would hide a typo that used to fail.

Only app-wide and pack rows are read here. Board rows come from ``boards.toml`` through
``power.config.load_boards`` (``[boards.defaults]`` included), and hubs through
``settings.hubs``. Secrets through ``secret``.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.errors import UsageError

from .files import LEGACY_UPDATES, SETTINGS_FILE, SettingsFiles, UserLayer, config_dir
from .packs import pack_defaults
from .policy import MachinePolicy, load_machine_policy
from .resolve import Resolved, Resolver, core_schema
from .schema import Schema, Setting, canonical_key

log = logging.getLogger(__name__)

#: The admin policy file the readers use (None: the OS's). Tests point it elsewhere.
POLICY_PATH: Path | None = None


@dataclass(frozen=True)
class _Snapshot:
    stamp: tuple[bytes | None, ...]
    layer: UserLayer
    policy: MachinePolicy


_mu = threading.RLock()
_now: dict[tuple[str, str], _Snapshot] = {}         # (config dir, policy file) -> the files now
_boot: dict[tuple[str, str], _Snapshot] = {}        # ... as this process first read them
_resolvers: dict[tuple[tuple[str, str], bool, int], tuple[_Snapshot, Resolver]] = {}
_logged: set[str] = set()
_packs: dict[str, tuple[Setting, ...]] = {}
_schema: list[Any] = [None, {}, 0]                  # [Schema, pack layer, generation]


# --- the schema: the core's rows, and each pack's once it asks ---------------------------------


def add_rows(rows: Sequence[Setting], *, pack: str) -> None:
    """A board pack's rows (``BoardPack.settings()``), so its readers can ask for them. Once
    per pack; a second call is free."""
    if pack in _packs:
        return
    with _mu:
        if pack in _packs:
            return
        _packs[pack] = tuple(rows)
        _schema[0] = None


def schema() -> Schema:
    """Every row this process knows: the core's and each pack's that ``add_rows`` gave
    (FIX-PACK-2: the service lists the variables its rows name)."""
    return _layers()[0]


def _layers() -> tuple[Schema, dict[str, Any], int]:
    with _mu:
        if _schema[0] is None:
            schema = core_schema()
            layer: dict[str, Any] = {}
            for pack, rows in _packs.items():
                schema.extend(rows, pack=pack)
                layer.update(pack_defaults(rows))
            _schema[:] = [schema, layer, _schema[2] + 1]
        return _schema[0], _schema[1], _schema[2]


# --- the files ---------------------------------------------------------------------------------


def _policy_path() -> Path:
    if POLICY_PATH is not None:
        return Path(POLICY_PATH)
    from harness_manager.services.update.policy import policy_path

    return policy_path()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _snapshot(root: Path, policy: Path) -> _Snapshot:
    """The user's settings.toml (and the update settings it folds in) and the policy, parsed
    again only when their bytes change: a stat-sized cost per read, and a hand edit is seen
    at once (a timestamp can miss two quick writes of the same size)."""
    stamp = (_read(root / SETTINGS_FILE), _read(root.joinpath(*LEGACY_UPDATES)), _read(policy))
    key = (str(root), str(policy))
    with _mu:
        snap = _now.get(key)
        if snap is not None and snap.stamp == stamp:
            return snap
    snap = _Snapshot(stamp, SettingsFiles(root).read(boards=False), load_machine_policy(policy))
    with _mu:
        _now[key] = snap
        _boot.setdefault(key, snap)
    return snap


def _resolver(root: Path, *, boot: bool, env: Mapping[str, str] | None) -> Resolver:
    policy = _policy_path()
    key = (str(root), str(policy))
    snap = _snapshot(root, policy)
    if boot:
        with _mu:
            snap = _boot.setdefault(key, snap)
    schema, layer, gen = _layers()
    if env is not None:                              # a caller's own environment: not cached
        return Resolver(schema, policy=snap.policy, env=env, user=snap.layer,
                        pack_defaults=layer)
    rkey = (key, boot, gen)
    with _mu:
        hit = _resolvers.get(rkey)
        if hit is not None and hit[0] is snap:
            return hit[1]
    # ``os.environ`` itself, not a copy: a variable is read when the value is.
    r = Resolver(schema, policy=snap.policy, env=os.environ, user=snap.layer,
                 pack_defaults=layer)
    with _mu:
        _resolvers[rkey] = (snap, r)
    return r


# --- reading -----------------------------------------------------------------------------------


def resolved(key: str, *, state_dir: Path | str | None = None,
             env: Mapping[str, str] | None = None, strict: bool = True) -> Resolved:
    """The row's value and where it came from. ``state_dir``: the reader's own (an engine's);
    ``env``: an environment of the caller's (a test seam of the reader's), else the
    process's. ``UsageError`` for a key no row declares, a board or hub key (read those
    through ``boards.toml`` and ``settings.hubs``), or (``strict``) a bad value in the
    developer's variable."""
    schema, _, _ = _layers()
    spec = schema.spec(key)
    if spec.collection:
        raise UsageError(f"{key}: a {spec.collection} setting is read per "
                         f"{spec.collection[:-1]}, not here")
    boot = spec.apply == "restart"
    r = _resolver(config_dir(state_dir), boot=boot, env=env).resolve(canonical_key(key))
    for problem in r.problems:
        if spec.env and problem.startswith(f"${spec.env}:") and strict \
                and r.source not in ("env", "lock"):
            raise UsageError(problem.removesuffix("; ignored"),
                             hint=f"fix ${spec.env}, or unset it to use {key} from the "
                                  "settings")
        _log_once(f"{key}: {problem}")
    return r


def value(key: str, *, state_dir: Path | str | None = None,
          env: Mapping[str, str] | None = None, strict: bool = True) -> Any:
    """The row's value (``resolved(...).value``): typed as the row says, never ``None`` for
    a row whose default is not."""
    return resolved(key, state_dir=state_dir, env=env, strict=strict).value


def said(r: Resolved, shown: Any = None) -> str:
    """The value and who said it, for a message: ``HARNESS_MANAGER_OPENOCD=/x`` when the
    variable did (the words the messages have always used), else
    ``tools.openocd=/x (settings.toml)``."""
    text = r.value if shown is None else shown
    if r.source == "env":
        return f"{r.where.lstrip('$')}={text}"
    return f"{r.key}={text} ({r.where or r.source})"


def secret(key: str, *, env_var: str = "", legacy: Iterable[tuple[str, Callable[[], Any]]] = (),
           env: Mapping[str, str] | None = None, state_dir: Path | str | None = None) -> Any:
    """The secret the code should send (``secrets.resolve_secret``): the variable, then the
    user's reference in ``settings.toml`` (the secret store by default), then ``legacy``
    sources in order. ``None``: none has it. ``env``: the variable's environment (a
    caller's test seam); the store is always the config dir's, never one ``env`` names."""
    from .secrets import SecretStore, resolve_secret

    root = config_dir(state_dir)
    ref = _snapshot(root, _policy_path()).layer.values.get(canonical_key(key))
    store = SecretStore(root)
    return resolve_secret(key, store=store, env=os.environ if env is None else env,
                          env_var=env_var, ref=ref, legacy=tuple(legacy))


# --- the cache ---------------------------------------------------------------------------------


def refresh() -> None:
    """Forget the files as last read, so the next read takes them again (``settings.changed``
    and ``watch``). The snapshot ``restart`` rows use is kept: those need a restart."""
    with _mu:
        _now.clear()
        _resolvers.clear()


def prime(state_dir: Path | str | None = None) -> None:
    """Take the service's start-up snapshot now (``run_daemon``): ``restart`` rows keep the
    values its files had when it started, whenever they are first read."""
    _snapshot(config_dir(state_dir), _policy_path())


def watch(bus: Any) -> Callable[[], None]:
    """Drop the cache whenever ``settings.changed`` is published on ``bus`` (the service).
    Returns the unsubscribe."""
    def changed(_event: Any) -> None:
        refresh()
    return bus.subscribe("settings.changed", changed)


def reset() -> None:
    """Forget everything, the start-up snapshots and the packs' rows too (tests)."""
    with _mu:
        _now.clear()
        _boot.clear()
        _resolvers.clear()
        _logged.clear()
        _packs.clear()
        _schema[:] = [None, {}, _schema[2] + 1]


def _log_once(message: str) -> None:
    with _mu:
        if message in _logged:
            return
        _logged.add(message)
    log.warning("settings: %s", message)
