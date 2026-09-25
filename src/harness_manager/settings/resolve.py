"""The resolver: one value per setting, and where it came from (lane SET-CORE).

**Precedence**, highest first (``docs/design/SETTINGS.md`` §4.2):

1. ``lock``: the admin policy's ``[lock]`` (and its machine hubs, and U6's ``channel`` and
   ``check_interval``). Not even an environment variable moves it: a user could otherwise
   step around the admin (U6's rule).
2. ``env``: the developer variable (``$HARNESS_MANAGER_OPENOCD``, …). Tests and CI need an
   override that never edits a person's files; git, pip and uv put the environment above
   their files too. When it hides the user's own value, ``shadowed`` names it, so the menu
   says "overridden by $X in the service's environment".
3. ``user``: the user's files (``settings.toml``; ``boards.toml``, where a board's own table
   comes before ``[boards.defaults]``).
4. ``machine``: the policy's ``[default]``: this machine's starting point.
5. ``pack``: the board pack's default for its own rows (SET-PACK).
6. ``default``: the schema's.

A row with ``env_rank = "under-user"`` puts the variable below the user's file (3 before 2):
``updates.channel`` does, because the updater has always let the user's choice win over
``$HARNESS_MANAGER_UPDATE_CHANNEL``.

After that, a policy **cap** can lower a ceiling row (``updates.auto``: U6's
``self_update``); ``capped`` says so. A ``[lock]`` on a ceiling row is a cap too: the admin
only tightens self-update.

**Failure handling.** A value that fails validation in any layer is skipped, and the row's
``problems`` say why: a typo in a variable cannot take the menu down. A **lock** that fails
validation fails closed: the setting stays locked at its default, and the problem is shown.

**Secrets** resolve to their status only (``{set, backend, where, reachable, why}``); the
value never leaves ``secrets.SecretStore.get`` / ``resolve_secret``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness_manager.core.errors import RefusedError, UsageError

from .files import BOARD_DEFAULTS, SettingsFiles, UserLayer
from .policy import MachinePolicy, load_machine_policy
from .rows import CORE_ROWS
from .schema import Schema, Setting, canonical_key, coerce, join_key, split_key
from .secrets import SecretStore, parse_ref

SOURCES = ("lock", "env", "user", "machine", "pack", "default")


def core_schema() -> Schema:
    """The core's rows. A pack's join with ``Schema.extend(rows, pack=name)`` (SET-PACK)."""
    return Schema(CORE_ROWS)


@dataclass(frozen=True)
class Resolved:
    key: str
    value: Any
    source: str                       # lock | env | user | machine | pack | default
    where: str = ""                   # the file, "$VAR", "the board pack"
    locked: bool = False
    shadowed: str = ""                # the user's own value is hidden by this ("$VAR")
    capped: str = ""                  # the policy lowered the value: why
    apply: str = "live"
    problems: tuple[str, ...] = ()
    secret: Mapping[str, Any] | None = None   # a secret's status (its value is never here)
    spec: Setting | None = field(default=None, repr=False, compare=False)

    def view(self) -> dict[str, Any]:
        """The API's row. A secret's ``value`` is only whether it is set."""
        out = {"key": self.key, "value": self.value, "source": self.source,
               "where": self.where, "locked": self.locked, "shadowed": self.shadowed,
               "capped": self.capped, "apply": self.apply, "problems": list(self.problems)}
        if self.secret is not None:
            out["secret"] = dict(self.secret)
        if self.spec is not None:
            out.update({"type": self.spec.type, "section": self.spec.section,
                        "scope": self.spec.scope, "doc": self.spec.doc,
                        "default": None if self.spec.secret else self.spec.default,
                        "choices": list(self.spec.choices), "readonly": self.spec.readonly,
                        "owner": self.spec.owner, "advanced": self.spec.advanced})
        return out


class Resolver:
    """Resolve, set and unset settings over the layers. Construct it with ``load`` for the
    real files, or with explicit layers (tests, the API's dry runs)."""

    def __init__(self, schema: Schema | None = None, *, policy: MachinePolicy | None = None,
                 env: Mapping[str, str] | None = None,
                 user: Mapping[str, Any] | UserLayer | None = None,
                 pack_defaults: Mapping[str, Any] | None = None,
                 files: SettingsFiles | None = None,
                 secrets: SecretStore | None = None) -> None:
        self.schema = schema or core_schema()
        self.policy = policy or MachinePolicy()
        self.env: Mapping[str, str] = os.environ if env is None else env
        if isinstance(user, UserLayer):
            self.layer = user
        else:
            self.layer = UserLayer(values={canonical_key(k): v for k, v in (user or {}).items()})
            self.layer.origin = {k: "settings.toml" for k in self.layer.values}
            self.layer.hubs = sorted({split_key(k)[1] for k in self.layer.values
                                      if split_key(k)[0] == "hubs" and len(split_key(k)) > 2})
            self.layer.boards = sorted({split_key(k)[1] for k in self.layer.values
                                        if split_key(k)[0] == "boards"
                                        and len(split_key(k)) > 2} - {BOARD_DEFAULTS})
        self.pack_defaults = {canonical_key(k): v for k, v in (pack_defaults or {}).items()}
        self.files = files
        self.secrets = secrets
        self.problems: list[str] = [*self.policy.problems, *self.layer.problems,
                                    *self.policy.check(self.schema), *self._unknown()]

    def _unknown(self) -> list[str]:
        """Keys in settings.toml that no row declares (a typo, or a newer version's). They
        are kept in the file and ignored. boards.toml is not checked: its pack tables pass
        through untouched until the pack declares them (SET-PACK)."""
        out = []
        for key, origin in self.layer.origin.items():
            if origin != "settings.toml":
                continue
            spec = self.schema.find(key)
            if spec is None or (spec.collection and split_key(key)[1] == "*"):
                out.append(f"settings.toml: {key} is not a setting; ignored")
        return out

    @classmethod
    def load(cls, state_dir: Path | str | None = None, *, schema: Schema | None = None,
             env: Mapping[str, str] | None = None, policy_path: Path | None = None,
             pack_defaults: Mapping[str, Any] | None = None,
             secrets: SecretStore | None = None) -> Resolver:
        """The real layers: the policy file, the environment, the user's files in the config
        dir (``files.config_dir``), and the secret store beside them."""
        files = SettingsFiles(state_dir if state_dir is not None else
                              _config_dir_from(env))
        return cls(schema, policy=load_machine_policy(policy_path), env=env,
                   user=files.read(), pack_defaults=pack_defaults, files=files,
                   secrets=secrets if secrets is not None else SecretStore(files.root, env=env))

    # --- resolving ---

    def resolve(self, key: str) -> Resolved:
        spec = self.schema.spec(key)
        key = canonical_key(key)
        if spec.secret:
            return self._resolve_secret(spec, key)
        problems: list[str] = []
        pol = self.policy
        cap = pol.cap.get(key, pol.cap.get(spec.key))
        found, raw, _ = pol.lookup(pol.lock, key, spec.key)
        if found and spec.ceiling:
            cap = _lowest(spec, cap, raw, problems, pol.path)
        elif found and spec.lockable:
            try:
                value = coerce(spec, raw)
            except UsageError as exc:
                # Fail closed: the admin meant to fix this setting, so it stays fixed, at
                # the default, and the menu says why, rather than falling open to the user.
                return Resolved(key, spec.default, "lock", pol.path, locked=True,
                                apply=spec.apply, spec=spec,
                                problems=(f"the policy's value is not valid ({exc.message}); "
                                          "the default is used, locked",))
            return Resolved(key, value, "lock", pol.path, locked=True, apply=spec.apply,
                            spec=spec)

        user_has = key in self.layer.values
        env_layer = []
        if spec.env and self.env.get(spec.env, "").strip():
            env_layer = [("env", f"${spec.env}", self.env[spec.env], True)]
        user_layer = []
        if user_has:
            user_layer.append(("user", self.layer.origin.get(key, "settings.toml"),
                               self.layer.values[key], False))
        parts = split_key(key)
        if spec.collection == "boards" and parts[1] != BOARD_DEFAULTS:
            dkey = join_key(("boards", BOARD_DEFAULTS, *parts[2:]))
            if dkey in self.layer.values:
                user_layer.append(("user", f"{self.layer.origin.get(dkey, 'boards.toml')} "
                                           f"[boards.{BOARD_DEFAULTS}]",
                                   self.layer.values[dkey], False))
        layers = (env_layer + user_layer) if spec.env_rank == "over-user" \
            else (user_layer + env_layer)
        found, raw, _ = pol.lookup(pol.default, key, spec.key)
        if found and spec.lockable:
            layers.append(("machine", pol.path, raw, False))
        found, raw, _ = pol.lookup(self.pack_defaults, key, spec.key)
        if found:
            layers.append(("pack", f"the {spec.pack or 'board'} pack", raw, False))

        for source, where, raw, from_env in layers:
            try:
                value = coerce(spec, raw, from_env=from_env)
            except UsageError as exc:
                problems.append(f"{where}: {exc.message}; ignored")
                continue
            shadowed = where if source == "env" and user_has else ""
            return self._capped(Resolved(key, value, source, where, shadowed=shadowed,
                                         apply=spec.apply, problems=tuple(problems),
                                         spec=spec), spec, cap)
        default = list(spec.default) if isinstance(spec.default, list) else spec.default
        return self._capped(Resolved(key, default, "default", apply=spec.apply,
                                     problems=tuple(problems), spec=spec), spec, cap)

    def _capped(self, r: Resolved, spec: Setting, cap: Any) -> Resolved:
        if cap is None or not spec.ceiling or cap not in spec.choices \
                or r.value not in spec.choices:
            return r
        if spec.choices.index(r.value) > spec.choices.index(cap):
            return replace(r, value=cap, capped=f"the administrator's policy {self.policy.path} "
                                                f"allows at most {cap!r}")
        return r

    def _resolve_secret(self, spec: Setting, key: str) -> Resolved:
        problems: list[str] = []
        found, _, _ = self.policy.lookup(self.policy.lock, key, spec.key)
        if found:
            problems.append(f"{self.policy.path}: a secret cannot be locked by the policy; "
                            "ignored")
        ref = self.layer.values.get(key)
        ref_kind = "store"
        origin = self.layer.origin.get(key, "settings.toml")
        inline = False
        if key in self.layer.values:
            try:
                ref_kind, _ = parse_ref(ref)
            except UsageError as exc:
                if origin == "boards.toml":
                    # T9's inline password: power/config.py still uses it (and warns when
                    # the file is readable). Shown as set, with the way to move it.
                    inline = True
                    problems.append(f"boards.toml holds this secret inline; move it to the "
                                    f"secret store: `harness-manager config set-secret {key}`")
                else:
                    problems.append(f"{origin}: {exc.message}")
        if spec.env and self.env.get(spec.env, "").strip():
            status = {"set": True, "backend": "env", "where": f"${spec.env}",
                      "reachable": True, "why": ""}
            return Resolved(key, {"set": True}, "env", f"${spec.env}", apply=spec.apply,
                            problems=tuple(problems), secret=status, spec=spec,
                            shadowed=f"${spec.env}" if self._stored(key) else "")
        if inline:
            status = {"set": True, "backend": "inline", "where": "boards.toml",
                      "reachable": True, "why": "written in boards.toml"}
            return Resolved(key, {"set": True}, "user", "boards.toml", apply=spec.apply,
                            problems=tuple(problems), secret=status, spec=spec)
        if ref_kind != "store":
            status = {"set": True, "backend": ref_kind, "where": str(ref), "reachable": True,
                      "why": "a reference in your settings (checked when it is used)"}
            return Resolved(key, {"set": True}, "user", self.layer.origin.get(key, ""),
                            apply=spec.apply, problems=tuple(problems), secret=status,
                            spec=spec)
        if self.secrets is not None:
            st = self.secrets.status(key)
            if st.set:
                return Resolved(key, {"set": True}, "user", st.where, apply=spec.apply,
                                problems=tuple(problems), secret=st.view(), spec=spec)
        status = {"set": False, "backend": "", "where": "", "reachable": True, "why": ""}
        return Resolved(key, {"set": False}, "default", apply=spec.apply,
                        problems=tuple(problems), secret=status, spec=spec)

    def _stored(self, key: str) -> bool:
        return bool(self.secrets is not None and self.secrets.status(key).set)

    # --- changing ---

    def check_settable(self, key: str, value: Any, *, text: bool = False) -> tuple[str, Any]:
        """``(canonical key, the value to store)``, or ``UsageError`` (2) / ``RefusedError``
        (15, locked). ``text``: the value came from a command line (parse it as env does)."""
        spec = self.schema.spec(key)
        key = canonical_key(key)
        if spec.secret:
            raise UsageError(f"{key} is a secret: `harness-manager config set-secret {key}`")
        if spec.readonly:
            raise UsageError(f"{key} is not set here: {spec.doc}")
        found, _, where = self.policy.lookup(self.policy.lock, key, spec.key)
        if found and spec.lockable and not spec.ceiling:     # a ceiling is a cap, not a lock
            raise RefusedError(f"{key} is set by the administrator's policy "
                               f"{self.policy.path}" + (f" ([lock] {where})"
                                                         if where != key else ""),
                               hint="ask your administrator to change it")
        parts = split_key(key)
        if parts[0] == "boards" and parts[1] == BOARD_DEFAULTS and spec.collection != "boards":
            raise UsageError(f"{key}: boards.{BOARD_DEFAULTS} holds board settings only")
        return key, coerce(spec, value, from_env=text)

    def set(self, key: str, value: Any, *, text: bool = False) -> Resolved:
        """A user's change: validated, refused when locked, stored even when an env var
        shadows it (the result says so), and kept above a cap (which still applies)."""
        return self.set_many({key: value}, text=text)[0]

    def set_many(self, changes: Mapping[str, Any], *, text: bool = False) -> list[Resolved]:
        """All or nothing: every value is checked before any file is written."""
        checked = dict(self.check_settable(k, v, text=text) for k, v in changes.items())
        self._write(checked)
        return [self.resolve(k) for k in checked]

    def unset(self, key: str) -> Resolved:
        spec = self.schema.spec(key)
        key = canonical_key(key)
        if spec.secret:
            raise UsageError(f"{key} is a secret: `harness-manager config clear-secret {key}`")
        if key in self.layer.values:
            self._write({key: None})
        return self.resolve(key)

    def _write(self, changes: Mapping[str, Any]) -> None:
        if self.files is not None:
            self.files.write(changes)
        for k, v in changes.items():
            if v is None:
                self.layer.values.pop(k, None)
                self.layer.origin.pop(k, None)
            else:
                self.layer.values[k] = v
                self.layer.origin[k] = (self.files.file_for(k).name if self.files is not None
                                        else "settings.toml")
                self.layer.migrated = True
                parts = split_key(k)
                if len(parts) > 2 and parts[0] == "hubs" and parts[1] not in self.layer.hubs:
                    self.layer.hubs = sorted([*self.layer.hubs, parts[1]])
                if len(parts) > 2 and parts[0] == "boards" and parts[1] != BOARD_DEFAULTS \
                        and parts[1] not in self.layer.boards:
                    self.layer.boards = sorted([*self.layer.boards, parts[1]])

    # --- listing ---

    def instances(self) -> dict[str, list[str]]:
        """The hubs (the user's and the machine's) and the boards the files name."""
        return {"hubs": sorted(set(self.layer.hubs) | set(self.policy.hubs)),
                "boards": list(self.layer.boards)}

    def listing(self, *, section: str | None = None, include_dev: bool = False,
                instances: Mapping[str, list[str]] | None = None) -> list[Resolved]:
        """Every row, ``*`` expanded per hub and board, the developer seams only on request."""
        inst = self.instances() if instances is None else instances
        out = []
        for s in self.schema.rows:
            if (not s.ui and not include_dev) or (section and s.section != section):
                continue
            if s.collection:
                for name in inst.get(s.collection, []):
                    parts = list(s.parts)
                    parts[1] = name
                    out.append(self.resolve(join_key(parts)))
            else:
                out.append(self.resolve(s.key))
        return out


def _lowest(spec: Setting, a: Any, b: Any, problems: list[str], path: str) -> Any:
    """The stricter of two ceilings (a bad one is a problem, and off: fail closed)."""
    if b not in spec.choices:
        problems.append(f"{path}: {b!r} is not one of {', '.join(spec.choices)}; "
                        f"{spec.choices[0]!r} is used")
        b = spec.choices[0]
    if a is None or a not in spec.choices:
        return b
    return a if spec.choices.index(a) <= spec.choices.index(b) else b


def _config_dir_from(env: Mapping[str, str] | None) -> Path:
    from .files import config_dir

    return config_dir(None, env)
