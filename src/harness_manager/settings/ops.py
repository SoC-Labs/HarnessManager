"""What the settings API and ``harness-manager config`` do, in one place (lane SET-API).

The service's ``/settings`` routes (``daemon/settings_api.py``) and the CLI when no service
runs (``cli/cmd_config.py``) both call these functions, so ``config --json`` prints the
API's own shapes. Design: ``docs/design/SETTINGS.md`` §8; the routes are in docs/API.md
"Settings".

- ``listing``: every row (``*`` expanded per hub and board), a section, or one key;
- ``schema``: the rows without values, the pack's rows included;
- ``set_values``: ``{KEY: value, …}``, all or nothing; the reply says what each change
  needs (``apply``: ``live``, ``reopen`` or ``restart``);
- ``unset``, ``set_secret``, ``delete_secret``, ``paths``, ``test``.

**Values.** A JSON string is parsed as a command line gives it (``"90m"``, ``"true"``,
``"0x40"``, ``"a,b"``): the CLI sends what was typed, the menu sends what it has. Any other
JSON value must already have the setting's type (``5`` for an int, ``true`` for a bool).

**Secrets** never come back: the replies carry ``{set, backend, where, reachable, why}``.
A secret's value reaches ``SecretStore.set`` and nothing else here.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import UsageError

from . import testers
from .files import LEGACY_UPDATES, SCHEMA_VERSION, SettingsFiles, config_dir
from .resolve import Resolved, Resolver, core_schema
from .schema import Schema, canonical_key, split_key
from .secrets import SecretStore
from .testers import section_id

#: What a change needs, weakest first. A reply's ``apply`` is the strongest of its keys'.
APPLY_ORDER = ("live", "reopen", "restart")


@dataclass
class SettingsContext:
    """Where the settings are, for one process: the service keeps one (``d.settings``), and
    each CLI call makes one. Every read builds a fresh ``Resolver`` over the files, so a
    hand edit is seen at once; the secret store (its keyring probe) and the schema are kept.

    ``env``/``policy_path``/``keyrings`` default to the process's own: ``os.environ``, the
    OS's policy file, the OS keyrings (``$HARNESS_MANAGER_KEYRING=off``: none).
    """

    state_dir: Path | None = None
    env: Mapping[str, str] | None = None
    policy_path: Path | None = None
    keyrings: Sequence[Any] | None = None
    packs: Callable[[], Mapping[str, Any]] | None = None
    _store: SecretStore | None = field(default=None, init=False, repr=False)
    _schema: Schema | None = field(default=None, init=False, repr=False)
    _schema_problems: list[str] = field(default_factory=list, init=False, repr=False)

    def environ(self) -> Mapping[str, str]:
        return os.environ if self.env is None else self.env

    @property
    def config_dir(self) -> Path:
        return config_dir(self.state_dir, self.environ())

    def policy_file(self) -> Path:
        if self.policy_path is not None:
            return Path(self.policy_path)
        from harness_manager.services.update.policy import policy_path

        return policy_path()

    def store(self) -> SecretStore:
        root = self.config_dir
        if self._store is None or self._store.root != root:
            self._store = SecretStore(root, keyrings=self.keyrings, env=self.environ())
        return self._store

    def schema(self) -> Schema:
        """The core's rows, plus each board pack's (``BoardPack.settings()``, lane SET-PACK,
        when a pack has it). A pack whose rows are refused is a problem, not a failure."""
        if self._schema is not None:
            return self._schema
        schema = core_schema()
        problems: list[str] = []
        try:
            packs = dict(self.packs()) if self.packs is not None else {}
        except Exception as exc:  # noqa: BLE001 - the core rows still work without the packs
            packs = {}
            problems.append(f"the board packs did not load ({type(exc).__name__}: {exc}); "
                            "their settings are not shown")
        for name, pack in sorted(packs.items()):
            declare = getattr(pack, "settings", None)
            if not callable(declare):
                continue
            try:
                schema.extend(list(declare() or ()), pack=name)
            except Exception as exc:  # noqa: BLE001 - one pack's bad row must not hide the rest
                problems.append(f"the {name} pack's settings are not used: {exc}")
        self._schema, self._schema_problems = schema, problems
        return schema

    def resolver(self) -> Resolver:
        r = Resolver.load(self.config_dir, schema=self.schema(), env=self.environ(),
                          policy_path=self.policy_file(), secrets=self.store())
        r.problems.extend(self._schema_problems)
        return r


# --- helpers ------------------------------------------------------------------------------------


def strongest(applies: Sequence[str]) -> str:
    return max(applies, key=APPLY_ORDER.index, default="live")


def apply_summary(rows: Sequence[Resolved]) -> dict[str, Any]:
    """``{keys, apply, applies: {live: [...], reopen: [...], restart: [...]}}``."""
    applies: dict[str, list[str]] = {a: [] for a in APPLY_ORDER}
    for r in rows:
        applies.setdefault(r.apply, []).append(r.key)
    return {"keys": [r.key for r in rows], "apply": strongest([r.apply for r in rows]),
            "applies": applies}


def row(r: Resolved) -> dict[str, Any]:
    out = r.view()
    if r.spec is not None:
        out["section_id"] = section_id(r.spec.section)
    return out


def sections(schema: Schema) -> list[dict[str, str]]:
    return [{"id": section_id(n), "name": n} for n in schema.sections()]


def find_section(schema: Schema, text: str) -> str:
    """A section by its id or name (any case), or a unique start of its id."""
    names = schema.sections()
    want = section_id(text)
    exact = [n for n in names if section_id(n) == want]
    if exact:
        return exact[0]
    start = [n for n in names if want and section_id(n).startswith(want)]
    if len(start) == 1:
        return start[0]
    raise UsageError(f"no settings section {text!r}",
                     hint="sections: " + ", ".join(section_id(n) for n in names))


def concrete(key: str) -> str:
    """The canonical key; a ``*`` part (a declared pattern) is refused: name one."""
    if not isinstance(key, str):
        raise UsageError("a setting's key is text, such as tools.openocd")
    parts = split_key(key)
    if "*" in parts:
        kind = {"hubs": "hub", "boards": "board"}.get(parts[0], "instance")
        raise UsageError(f"{key}: name one {kind} in place of the *",
                         hint=f"e.g. {parts[0]}.NAME.{'.'.join(parts[2:])}"
                         if len(parts) > 2 else "")
    return canonical_key(key)


def _backend_view(store: SecretStore) -> dict[str, str]:
    """Where a new secret would go, and why not the keyring (it may probe it, once)."""
    backend, why = store.where_new()
    return {"backend": backend.name, "where": backend.label, "why": why}


def files_view(ctx: SettingsContext, r: Resolver) -> dict[str, Any]:
    files = r.files if r.files is not None else SettingsFiles(ctx.config_dir)
    return {"config_dir": str(files.root), "settings": str(files.settings_path),
            "boards": str(files.boards_path), "policy": str(ctx.policy_file()),
            "secrets": str(ctx.store().dir), "secrets_backend": _backend_view(ctx.store())}


def policy_view(ctx: SettingsContext, r: Resolver) -> dict[str, Any]:
    path = ctx.policy_file()
    return {"path": str(path), "exists": path.is_file(), "problems": list(r.policy.problems)}


# --- reading ------------------------------------------------------------------------------------


def listing(ctx: SettingsContext, *, section: str | None = None, key: str | None = None,
            include_dev: bool = False) -> dict[str, Any]:
    """``GET /settings``: the rows, with where the files are and what is wrong with them."""
    r = ctx.resolver()
    if key:
        rows = [r.resolve(concrete(key))]
    else:
        rows = r.listing(section=find_section(r.schema, section) if section else None,
                         include_dev=include_dev)
    return {"schema_version": SCHEMA_VERSION, "files": files_view(ctx, r),
            "policy": policy_view(ctx, r), "problems": list(dict.fromkeys(r.problems)),
            "sections": sections(r.schema), "instances": r.instances(),
            "rows": [row(x) for x in rows]}


def schema(ctx: SettingsContext) -> dict[str, Any]:
    """``GET /settings/schema``: every declared row, without a value (dev rows too: ``ui``
    says which the menu shows)."""
    s = ctx.schema()
    return {"schema_version": SCHEMA_VERSION, "sections": sections(s),
            "rows": [{**x.view(), "section_id": section_id(x.section)} for x in s.rows],
            "problems": list(ctx._schema_problems)}


# --- changing -----------------------------------------------------------------------------------


def set_values(ctx: SettingsContext, changes: Any) -> dict[str, Any]:
    """``PUT /settings``: all or nothing. Every key and value is checked, and every file it
    touches is parsed, before anything is written. 400 names a bad key or value; 409 names
    the policy file that locks a key."""
    if not isinstance(changes, Mapping) or not changes:
        raise UsageError('send the settings to change as a JSON object: {"KEY": value, ...}')
    r = ctx.resolver()
    try:
        keys = [concrete(k) for k in changes]
        if len(set(keys)) != len(keys):
            raise UsageError("a setting is named twice (two spellings of one key)")
        values = dict(zip(keys, changes.values(), strict=True))
        checked = [r.check_settable(k, v, text=True)[0] for k, v in values.items()]
        if r.files is not None:
            for path in {r.files.file_for(k) for k in checked}:
                SettingsFiles._parse_for_write(path)  # a file that does not parse: refused now
    except UsageError as exc:
        if not exc.hint:
            exc.hint = ("nothing was written; `harness-manager config get KEY` "
                        "(GET /settings?key=KEY) says what a setting takes")
        raise
    rows = r.set_many(values, text=True)
    return {"rows": [row(x) for x in rows], **apply_summary(rows)}


def unset(ctx: SettingsContext, key: str) -> dict[str, Any]:
    """``DELETE /settings/{key}``: back to the next layer. ``changed``: the user's files held
    it (else nothing was written)."""
    r = ctx.resolver()
    key = concrete(key)
    r.schema.spec(key)
    changed = key in r.layer.values
    if changed and r.files is not None:
        SettingsFiles._parse_for_write(r.files.file_for(key))
    got = r.unset(key)
    return {"rows": [row(got)], "changed": changed, **apply_summary([got])}


def _secret_key(r: Resolver, key: str) -> str:
    key = concrete(key)
    if not r.schema.spec(key).secret:
        raise UsageError(f"{key} is not a secret", hint=f"`harness-manager config set {key} VALUE`")
    return key


def set_secret(ctx: SettingsContext, key: str, value: Any) -> dict[str, Any]:
    """``PUT /settings/secrets/{key}``: into the store (the keyring, else a 0600 file). The
    reply is the status, never the value (and no error repeats it)."""
    r = ctx.resolver()
    key = _secret_key(r, key)
    if not isinstance(value, str):
        raise UsageError('send the secret as {"value": "..."} (a string)')
    status = ctx.store().set(key, value)
    got = r.resolve(key)
    return {"key": key, "secret": status.view(), "rows": [row(got)], "changed": True,
            **apply_summary([got])}


def delete_secret(ctx: SettingsContext, key: str) -> dict[str, Any]:
    """``DELETE /settings/secrets/{key}``: from the store (and a stray file copy)."""
    r = ctx.resolver()
    key = _secret_key(r, key)
    store = ctx.store()
    was = store.status(key).set
    status = store.delete(key)
    got = r.resolve(key)
    return {"key": key, "secret": status.view(), "rows": [row(got)], "changed": was,
            **apply_summary([got])}


def changed_event(result: Mapping[str, Any], source: str = "api") -> dict[str, Any]:
    """``settings.changed``'s data: keys and what they need, never a value."""
    return {"keys": list(result["keys"]), "apply": result["apply"],
            "applies": {k: list(v) for k, v in result["applies"].items()}, "source": source}


# --- where things are ---------------------------------------------------------------------------


def paths(ctx: SettingsContext) -> dict[str, Any]:
    """``config path``: every file the settings use, whether it is there, and where a new
    secret would go (the keyring, or why not)."""
    files = SettingsFiles(ctx.config_dir)
    store = ctx.store()
    rows = [("config_dir", files.root), ("settings", files.settings_path),
            ("boards", files.boards_path), ("policy", ctx.policy_file()),
            ("secrets", store.dir), ("secrets_index", store.index_path),
            ("legacy_updates", files.root.joinpath(*LEGACY_UPDATES))]
    return {"config_dir": str(files.root),
            "files": [{"what": w, "path": str(p), "exists": Path(p).exists()} for w, p in rows],
            "secrets_backend": _backend_view(store), "secrets_stored": len(store.names())}


# --- testing ------------------------------------------------------------------------------------


def find_tester(ctx: SettingsContext, section: str) -> tuple[str, testers.Tester | None]:
    sid = section_id(find_section(ctx.schema(), section))
    return sid, testers.tester_for(sid)


def test(ctx: SettingsContext, section: str, name: str = "", table: Mapping[str, Any] | None = None,
         progress: testers.Progress | None = None) -> dict[str, Any]:
    """Run the section's tester (``testers``), or say it is not testable yet."""
    sid, t = find_tester(ctx, section)
    if t is None:
        return testers.not_testable(sid, name)
    req = testers.TestRequest(sid, name, table, ctx.resolver(),
                              progress or (lambda _s, _d, _t: None))
    return testers.run(t, req)
