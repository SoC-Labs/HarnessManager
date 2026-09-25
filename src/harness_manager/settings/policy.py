"""The admin policy, as the settings resolver sees it (lane SET-CORE; david S3, 2026-09-25).

``/etc/harness-manager/policy.toml`` (``services.update.policy.policy_path`` for the other
OSes) is root-owned and read-only to Harness Manager. It gains three tables::

    self_update = "notify"            # U6, unchanged: a ceiling on updates.auto
    channel = "stable"                # U6, unchanged: a lock on updates.channel
    check_interval = "12h"            # U6, unchanged: a lock on updates.check_interval

    [lock]                            # fixed for every user: not even an env var moves them
    tools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"
    "boards.*.power.cycle" = false    # a pattern locks it for every board

    [default]                         # this machine's starting point: a user may change it
    updates.mirrors = ["/lab/mirror"]

    [hubs.lab]                        # a machine hub: locked, but each user brings a token
    transport = "ssh"
    host = "mapstone-dev.ecs.soton.ac.uk"

**The three U6 keys keep their meaning, from one parser.** ``services.update.policy`` (U6)
stays the source for them: this module takes its view (fail closed, the 5-minute floor, the
``[lock]`` spellings) and maps it, so the updater and the Settings menu cannot disagree.

**Only admin rows can be locked or pre-set** (``Setting.lockable``). A secret never goes in
this file (it is readable by every user): a ``token`` in a ``[hubs.*]`` table is dropped
with a problem. Unknown keys are problems, never fatal.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import UsageError

from .schema import Schema, join_key, split_key

#: The U6 top-level keys, and the settings they became.
LEGACY = {"channel": "updates.channel", "check_interval": "updates.check_interval",
          "self_update": "updates.auto"}


def load_toml(text: str) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    return tomllib.loads(text)


def flatten(table: Mapping[str, Any], prefix: tuple[str, ...] = (), *,
            split_root: bool = True) -> dict[str, Any]:
    """Nested tables -> ``{canonical key: value}``. Lists stay values.

    At the root, a key that is itself a dotted setting key (``"updates.channel" = …``) is
    split, so an admin may write either form. Deeper keys are never split: a board key such
    as ``"192.168.10.101"`` is one part.
    """
    out: dict[str, Any] = {}
    for k, v in table.items():
        parts: tuple[str, ...] = (k,)
        if split_root and not prefix and "." in k:
            try:
                parts = split_key(k)
            except UsageError:
                parts = (k,)
        path = prefix + parts
        if isinstance(v, Mapping):
            out.update(flatten(v, path, split_root=False))
        else:
            out[join_key(path)] = v
    return out


@dataclass(frozen=True)
class MachinePolicy:
    path: str = ""                                          # "": no policy file
    lock: Mapping[str, Any] = field(default_factory=dict)   # key (or pattern) -> raw value
    default: Mapping[str, Any] = field(default_factory=dict)
    cap: Mapping[str, Any] = field(default_factory=dict)    # ceiling rows: key -> highest
    hubs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)   # machine hubs
    problems: tuple[str, ...] = ()
    update: Any = None                                      # the U6 ``Policy`` (its callers')

    def lookup(self, table: Mapping[str, Any], key: str, pattern: str) -> tuple[bool, Any, str]:
        """``(found, value, the key it was found under)``: the exact key, then the pattern."""
        if key in table:
            return True, table[key], key
        if pattern != key and pattern in table:
            return True, table[pattern], pattern
        return False, None, ""

    def check(self, schema: Schema) -> list[str]:
        """Problems with the keys it names: unknown to the schema, or not lockable."""
        out = []
        for name, table in (("lock", self.lock), ("default", self.default)):
            for key in table:
                spec = schema.find(key) if _parses(key) else None
                if spec is None:
                    out.append(f"{self.path}: [{name}] {key} is not a setting; ignored")
                elif not spec.lockable and not (spec.ceiling and name == "lock"):
                    out.append(f"{self.path}: [{name}] {key} is not the administrator's "
                               f"to set (a {'secret' if spec.secret else spec.owner} "
                               "setting); ignored")
        return out


def _parses(key: str) -> bool:
    try:
        split_key(key)
    except UsageError:
        return False
    return True


def parse_machine_policy(text: str, path: str = "") -> MachinePolicy:
    """The policy file's text -> what the resolver needs. Never raises."""
    from harness_manager.services.update.policy import parse_policy as parse_u6

    u6 = parse_u6(text, path)
    try:
        data = load_toml(text)
    except Exception:  # noqa: BLE001 - tomllib's or tomli's decode error: U6 said why
        # Fail closed, as U6 does: self-update off. Nothing else is pre-set or locked:
        # which settings the admin meant to fix cannot be known.
        return MachinePolicy(path, cap={"updates.auto": "off"}, problems=u6.problems,
                             update=u6)
    problems = list(u6.problems)
    lock = _table(data, "lock", path, problems)
    default = _table(data, "default", path, problems)
    cap: dict[str, Any] = {}

    # The U6 settings: U6's view, so both agree. A value U6 rejected stays in the lock
    # (the resolver then locks the setting at its default, and says why: fail closed).
    raw_lock = dict(lock)
    for key in LEGACY.values():
        lock.pop(key, None)
    raw_channel = data.get("channel", raw_lock.get("updates.channel"))
    if u6.channel:
        lock["updates.channel"] = u6.channel
    elif raw_channel not in (None, ""):
        lock["updates.channel"] = raw_channel
    raw_interval = data.get("check_interval", raw_lock.get("updates.check_interval"))
    if u6.check_interval_s is not None:
        lock["updates.check_interval"] = u6.check_interval_s
    elif raw_interval is not None:
        lock["updates.check_interval"] = raw_interval
    if u6.self_update != "stage":
        cap["updates.auto"] = u6.self_update       # a ceiling: the admin only tightens

    hubs: dict[str, dict[str, Any]] = {}
    raw_hubs = data.get("hubs", {})
    if not isinstance(raw_hubs, dict):
        problems.append(f"{path}: [hubs] must be a table of hub tables; ignored")
        raw_hubs = {}
    for name, table in raw_hubs.items():
        if not isinstance(table, dict):
            problems.append(f"{path}: hubs.{name} must be a table; ignored")
            continue
        clean = {}
        for k, v in table.items():
            if k in ("token", "token_file"):
                problems.append(f"{path}: hubs.{name}.{k}: a hub's credential never goes in "
                                "the policy (every user can read it); each user sets their "
                                f"own (`harness-manager config set-secret hubs.{name}.token`)")
                continue
            clean[k] = v
            lock[join_key(("hubs", name, k))] = v
        hubs[name] = clean
    return MachinePolicy(path, lock=lock, default=default, cap=cap, hubs=hubs,
                         problems=tuple(problems), update=u6)


def _table(data: Mapping[str, Any], name: str, path: str, problems: list[str]) -> dict[str, Any]:
    raw = data.get(name, {})
    if not isinstance(raw, dict):
        problems.append(f"{path}: [{name}] must be a table; ignored")
        return {}
    return flatten(raw)


def load_machine_policy(path: Path | None = None) -> MachinePolicy:
    """The policy in force on this machine. No file: no policy. Never raises."""
    from harness_manager.services.update.policy import policy_path

    path = Path(path) if path is not None else policy_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MachinePolicy()
    except OSError as exc:
        why = f"it cannot be read: {exc.strerror or exc}"
        from harness_manager.services.update.policy import Policy

        return MachinePolicy(str(path), cap={"updates.auto": "off"},
                             problems=(why,),
                             update=Policy(path=str(path), self_update="off", problems=(why,),
                                           why_off=why))
    return parse_machine_policy(text, str(path))
