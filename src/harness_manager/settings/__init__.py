"""Harness Manager's settings: one schema, one resolver, the files and the secret store.

Lane SET-CORE (``docs/design/SETTINGS.md``; david's decisions S1-S4, 2026-09-25). Nothing
in the app reads settings through here yet: SET-WIRE switches the ``os.environ`` readers.

- ``schema``: ``Setting`` rows and ``Schema`` (a pack adds its rows: SET-PACK);
- ``rows``: the core's rows (Appendix A, minus the pack's);
- ``policy``: the admin policy's ``[lock]``, ``[default]``, ``[hubs.*]`` and U6's keys;
- ``files``: ``settings.toml`` and ``boards.toml`` (tomlkit, 0600, the migration);
- ``secrets``: the OS keyring, the 0600 file fallback and the index;
- ``resolve``: the precedence, and ``set``/``unset``.

    >>> r = Resolver.load()                       # the real files, env and policy
    >>> r.resolve("tools.openocd").view()         # value, source, shadowed, locked, ...
"""

from .files import SettingsFiles, config_dir
from .policy import MachinePolicy, load_machine_policy, parse_machine_policy
from .resolve import Resolved, Resolver, core_schema
from .schema import Schema, Setting, canonical_key, coerce, join_key, split_key
from .secrets import (
    FileBackend,
    KeyringBackend,
    SecretStatus,
    SecretStore,
    SecretValue,
    resolve_secret,
)

__all__ = [
    "FileBackend", "KeyringBackend", "MachinePolicy", "Resolved", "Resolver", "Schema",
    "SecretStatus", "SecretStore", "SecretValue", "Setting", "SettingsFiles", "canonical_key",
    "coerce", "config_dir", "core_schema", "join_key", "load_machine_policy",
    "parse_machine_policy", "resolve_secret", "split_key",
]
