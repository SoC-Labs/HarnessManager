"""fpgahub hubs as named settings (lane SET-HUBS; ``docs/design/SETTINGS.md`` §6; david S1-S3).

A hub is a ``[hubs.<name>]`` table: the user's, in ``settings.toml``, or the admin's, in the
policy file (a **machine hub**: every key the admin wrote is locked, but each user brings
their own token, S3). A board names its hub in ``boards.toml``::

    [boards.lab]
    match = ["192.168.10.101"]
    via = "hub"
    hub = { use = "lab", target = "mps3_01_pl", shares = { mcc = "/dev/mps3_01_pl/tty_00" } }

A board with no hub stays the zero-config default: nothing here runs, reads or probes.

**Which keys go where** (§4.1). The board keeps what is about the board: ``target``,
``board``, ``shares``, ``baud``, ``start_shares`` (``BOARD_KEYS``). The hub keeps what is
about the hub: ``transport``, ``host``, ``url``, ``group``, ``jump``, the token, ``ca_file``,
``cert_file``, ``key_file``, ``insecure``, ``events``, ``direct``, ``timeout_s``, ``holder``,
``lease_ttl``, ``request_ttl``, ``queue_timeout`` (``HUB_KEYS``). A board table with ``use``
and a hub key is refused, naming the key: a silent override is how a lab ends up with two
definitions of one hub.

**An inline ``hub = { host = …, target = … }`` keeps working exactly as before** (L1, T8),
token file first included. ``adopt_inline_hub`` ("Make this a hub…") moves it: the hub keys
become ``[hubs.<name>]`` (a ``token_file`` becomes ``token = "file:PATH"``), the board keeps
its own keys plus ``use``, ``via = "ssh:<the same host>"`` becomes ``via = "hub"``, and
``boards.toml`` is copied to ``boards.toml.bak-<date>`` first. Run again, it does nothing.

**A named REST hub's token** (``hub_credential``), highest first: ``$FPGAHUB_TOKEN`` (only
when ``$FPGAHUB_ADDR`` is unset or names this hub), then the user's reference
(``hubs.<name>.token``: the secret store by default, ``file:PATH`` read strictly, so a file
others can read is refused, or ``env:VAR``), then the fpgahub login store for this hub.
A token stored in a keyring this process cannot reach is an error (7), never quietly
replaced by another source.

**Test connection, discovery:** ``hubtest.py``. Nothing in either takes a lease.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, RefusedError, UsageError

from .resolve import Resolved, Resolver
from .schema import join_key, split_key
from .secrets import parse_ref, read_private_file, resolve_secret

#: The per-hub keys a ``[hubs.<name>]`` table holds (the token is a secret: ``token``).
HUB_FIELDS = ("transport", "host", "group", "jump", "holder", "url", "ca_file", "cert_file",
              "key_file", "insecure", "events", "direct", "timeout_s", "lease_ttl",
              "request_ttl", "queue_timeout")
#: Every key that belongs to the hub, not the board (a board table with ``use`` refuses them).
HUB_KEYS = frozenset({*HUB_FIELDS, "token", "token_file"})
#: What a board's ``hub`` table keeps when it names a hub.
BOARD_KEYS = ("use", "target", "board", "shares", "baud", "start_shares")
#: The keys of an inline ``hub`` table that move to ``[hubs.<name>]`` (T8's and L1's).
INLINE_HUB_KEYS = ("host", "url", "group", "token_file", "ca_file", "cert_file", "key_file",
                   "insecure", "events", "direct", "timeout_s")
TRANSPORTS = ("ssh", "rest")
TOKEN_ENV = "FPGAHUB_TOKEN"
ADDR_ENV = "FPGAHUB_ADDR"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TARGET = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

#: The admin policy the hubs are read with; tests point it at a file of their own (the real
#: one, /etc/harness-manager/policy.toml, has no environment override on purpose: U6).
POLICY_PATH: Path | None = None


def check_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise UsageError(f"{name!r} is not a hub name (1-64 of A-Z a-z 0-9 _ -)",
                         hint="e.g. lab, or the hub's host name's first label")
    return name


def load_resolver(root: Path | str | None = None,
                  env: Mapping[str, str] | None = None) -> Resolver:
    """The settings the hubs are resolved from: ``settings.toml`` and ``boards.toml`` in
    ``root`` (default: the config dir), the environment and the admin policy."""
    return Resolver.load(root, env=env, policy_path=POLICY_PATH)


# --- the hub -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hub:
    """One named hub, resolved: every key's value, and where it came from (``rows``)."""

    name: str
    transport: str = "ssh"
    host: str = ""
    group: str = "fpga"
    jump: str = ""
    holder: str = ""
    url: str = ""
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    insecure: bool = False
    events: bool = True
    direct: str = "auto"
    timeout_s: int = 30
    lease_ttl: int = 3600
    request_ttl: int = 7200
    queue_timeout: int = 3600
    token_ref: str = "store"          # store | file:PATH | env:VAR | fpgahub-login
    machine: bool = False             # defined by the admin policy
    policy: str = ""                  # that policy file
    rows: Mapping[str, Resolved] = field(default_factory=dict, repr=False, compare=False)
    token: Mapping[str, Any] = field(default_factory=dict, compare=False)   # status only
    problems: tuple[str, ...] = ()

    @property
    def local(self) -> bool:
        return self.transport == "ssh" and self.host in ("local", "localhost")

    @property
    def locked(self) -> list[str]:
        """The keys the admin policy fixes (a machine hub's)."""
        return [k for k, r in self.rows.items() if r.locked]

    def table(self) -> dict[str, Any]:
        """The inline ``hub`` table this hub stands for (``hub.parse_hub_table`` reads it;
        the credential comes from the hub's name, never from a ``token_file`` here)."""
        if self.transport == "rest":
            out: dict[str, Any] = {"url": self.url, "insecure": self.insecure,
                                   "events": self.events, "direct": self.direct,
                                   "timeout_s": self.timeout_s}
            for k in ("ca_file", "cert_file", "key_file"):
                if getattr(self, k):
                    out[k] = getattr(self, k)
            if self.host:
                out["host"] = self.host          # the data plane's SSH fallback (T8)
            return out
        return {"host": self.host, "group": self.group}

    def config_problems(self) -> list[str]:
        """What stops this hub from being used, each naming its key."""
        out = []
        where = f"hubs.{self.name}"
        if self.transport == "ssh" and not self.host:
            out.append(f"{where}.host is not set (the hub to ssh into, or \"local\")")
        if self.transport == "rest":
            if not self.url:
                out.append(f"{where}.url is not set (https://HUB:7246)")
            else:
                from harness_manager.transports import hub_rest

                try:
                    hub_rest._normalise_url(self.url, where)
                except UsageError as exc:
                    out.append(exc.message)
            if bool(self.cert_file) != bool(self.key_file):
                out.append(f"{where}: cert_file and key_file go together (an mTLS pair)")
        if self.jump and self.transport != "ssh":
            out.append(f"{where}.jump is for an SSH hub only")
        return out

    def view(self) -> dict[str, Any]:
        """The API's and ``hub list --json``'s shape. Never a token: its status only."""
        out: dict[str, Any] = {"name": self.name, "transport": self.transport,
                               "machine": self.machine, "policy": self.policy,
                               "locked": self.locked, "token": dict(self.token),
                               "token_ref": _safe_ref(self.token_ref),
                               "problems": [*self.problems, *self.config_problems()]}
        for f in HUB_FIELDS[1:]:
            out[f] = getattr(self, f)
        out["sources"] = {k: {"source": r.source, "where": r.where, "locked": r.locked,
                              "shadowed": r.shadowed} for k, r in self.rows.items()}
        return out


def _safe_ref(ref: str) -> str:
    try:
        kind, arg = parse_ref(ref)
    except UsageError:
        return "(not a reference: ignored)"
    return kind if kind in ("store", "fpgahub-login", "gh") else f"{kind}:{arg}"


def hub_names(resolver: Resolver) -> list[str]:
    return list(resolver.instances()["hubs"])


def resolve_hub(name: str, resolver: Resolver) -> Hub:
    """The hub ``name`` (the user's or the machine's). ``UsageError`` when there is none."""
    check_name(name)
    if name not in hub_names(resolver):
        broken = [p for p in resolver.layer.problems if "settings.toml" in p]
        raise UsageError(f"no hub named {name!r}" + (f" ({broken[0]})" if broken else ""),
                         hint="`harness-manager hub list` shows the hubs; `harness-manager "
                              "hub add NAME --ssh HOST` (or --url URL) adds one")
    rows: dict[str, Resolved] = {}
    problems: list[str] = []
    for f in HUB_FIELDS:
        r = resolver.resolve(join_key(("hubs", name, f)))
        rows[f] = r
        problems.extend(r.problems)
    token = resolver.resolve(join_key(("hubs", name, "token")))
    problems.extend(token.problems)
    v = {f: r.value for f, r in rows.items()}
    transport = v["transport"]
    if rows["transport"].source == "default" and v["url"]:
        transport = "rest"                       # H1: a url means REST, as T8 has it
    raw_ref = resolver.layer.values.get(join_key(("hubs", name, "token")))
    try:
        kind, arg = parse_ref(raw_ref)
        ref = "store" if kind == "store" else (f"{kind}:{arg}" if arg else kind)
    except UsageError:
        ref = "store"
    machine = name in resolver.policy.hubs
    return Hub(name=name, transport=transport, host=v["host"], group=v["group"],
               jump=v["jump"], holder=v["holder"], url=v["url"], ca_file=v["ca_file"],
               cert_file=v["cert_file"], key_file=v["key_file"], insecure=v["insecure"],
               events=v["events"], direct=v["direct"], timeout_s=int(v["timeout_s"]),
               lease_ttl=int(v["lease_ttl"]), request_ttl=int(v["request_ttl"]),
               queue_timeout=int(v["queue_timeout"]), token_ref=ref, machine=machine,
               policy=resolver.policy.path if machine else "", rows=rows,
               token=dict(token.secret or {}), problems=tuple(dict.fromkeys(problems)))


def list_hubs(resolver: Resolver) -> list[Hub]:
    return [resolve_hub(n, resolver) for n in hub_names(resolver)]


def boards_using(resolver: Resolver, name: str) -> list[str]:
    """The ``boards.toml`` board keys whose ``hub.use`` is ``name``."""
    out = []
    for key, value in resolver.layer.values.items():
        parts = split_key(key)
        if len(parts) == 4 and parts[0] == "boards" and parts[2:] == ("hub", "use") \
                and value == name:
            out.append(parts[1])
    return sorted(out)


def inline_hub_boards(resolver: Resolver) -> list[str]:
    """Boards with an inline hub table (no ``use``): each is a "Make this a hub…" candidate."""
    found: dict[str, set[str]] = {}
    for key in resolver.layer.values:
        parts = split_key(key)
        if len(parts) >= 4 and parts[0] == "boards" and parts[2] == "hub":
            found.setdefault(parts[1], set()).add(parts[3])
    return sorted(b for b, keys in found.items()
                  if "use" not in keys and keys & {"host", "url"})


# --- a board's hub (CCR SET-HUB-1: harness_manager_mps3/hub.py calls this) ---------------------


def board_hub_table(table: Mapping[str, Any], *, where: str = "hub",
                    root: Path | str | None = None, env: Mapping[str, str] | None = None,
                    resolver: Resolver | None = None) -> tuple[dict[str, Any], Hub]:
    """A board's ``hub = { use = NAME, … }`` as the inline table it stands for, and the hub.

    ``UsageError`` names the problem: a hub key beside ``use``, an unknown key, no such
    hub, or a hub that cannot be used (no host, a bad url…).
    """
    name = table.get("use")
    if not isinstance(name, str) or not _NAME.match(name):
        raise UsageError(f"{where}.use must name a hub ([hubs.<name>] in settings.toml)")
    beside = sorted(k for k in table if k in HUB_KEYS)
    if beside:
        raise UsageError(f"{where} uses the hub {name!r} and also sets "
                         f"{', '.join(beside)}: {'those belong' if len(beside) > 1 else 'that belongs'} "
                         f"to the hub, in [hubs.{name}] (settings.toml)",
                         hint="remove them from the board's hub table, or drop `use` to keep "
                              "an inline hub")
    unknown = sorted(set(table) - set(BOARD_KEYS))
    if unknown:
        raise UsageError(f"{where} has unknown keys: {', '.join(unknown)}")
    r = resolver or load_resolver(root, env)
    hub = resolve_hub(name, r)
    bad = hub.config_problems()
    if bad:
        raise UsageError(f"{where} uses the hub {name!r}, which cannot be used: {bad[0]}",
                         hint=f"`harness-manager hub test {name}` checks it")
    merged = {**hub.table(), **{k: v for k, v in table.items() if k != "use"}}
    return merged, hub


# --- a named REST hub's token (CCR SET-HUB-2: hub_rest.resolve_credential calls this) ----------


def hub_credential(name: str, host: str, port: int, *, root: Path | str | None = None,
                   env: Mapping[str, str] | None = None, login_store: Path | None = None,
                   resolver: Resolver | None = None) -> Any:
    """The ``hub_rest.Credential`` for the named hub (module docstring for the order)."""
    from harness_manager.transports import hub_rest

    r = resolver or load_resolver(root, env)
    env = r.env
    key = join_key(("hubs", name, "token"))

    def env_ok(e: Mapping[str, str]) -> bool:
        addr = e.get(ADDR_ENV, "")
        return not addr or hub_rest._addr_key(addr) == (host.lower(), port)

    saved: dict[str, Any] = {}

    def login() -> str | None:
        path = login_store or hub_rest.fpgahub_login_store()
        data = hub_rest._read_login_store(path)
        addr = data.get("addr")
        if isinstance(addr, str) and hub_rest._addr_key(addr) == (host.lower(), port):
            saved.update(data, path=str(path))
            tok = data.get("token")
            return tok if isinstance(tok, str) and tok else None
        return None

    if r.secrets is None:                        # pragma: no cover - load() always has one
        from .secrets import SecretStore

        r.secrets = SecretStore(r.files.root if r.files else Path("."), env=env)
    got = resolve_secret(key, store=r.secrets, env=env, env_var=TOKEN_ENV, env_ok=env_ok,
                         ref=r.layer.values.get(key), legacy=[("fpgahub-login", login)])
    if got is None:
        return hub_rest.Credential(None, "none")
    if got.source == "fpgahub-login":
        tls = saved.get("tls_dir") if isinstance(saved.get("tls_dir"), str) else ""
        return hub_rest.Credential(got.value, f"fpgahub login ({saved.get('path', '')})",
                                   tls_dir=tls, insecure=saved.get("insecure") is True)
    source = got.source if got.source.startswith(("$", "the file")) else \
        f"{key} in {got.source}"
    return hub_rest.Credential(got.value, source)


# --- writing: add, token, remove ---------------------------------------------------------------


def _require_user_hub(name: str, resolver: Resolver, what: str) -> None:
    if name in resolver.policy.hubs:
        raise RefusedError(f"{name} is a machine hub, set by your administrator in "
                           f"{resolver.policy.path}; {what} is theirs to change",
                           hint=f"only its token is yours: `harness-manager hub token {name} "
                                "--stdin`")


def add_hub(name: str, values: Mapping[str, Any], resolver: Resolver, *,
            update: bool = False, text: bool = True) -> Hub:
    """Write ``[hubs.<name>]`` (all or nothing). ``values`` are hub keys (``HUB_FIELDS``);
    ``text``: they came from a command line (``"1h"``, ``"true"``). ``update``: change an
    existing hub's keys instead of refusing."""
    check_name(name)
    _require_user_hub(name, resolver, "its definition")
    exists = name in hub_names(resolver)
    if exists and not update:
        raise UsageError(f"a hub named {name!r} already exists",
                         hint=f"`harness-manager hub add {name} --update …` changes it; "
                              f"`harness-manager hub remove {name}` removes it")
    if not exists and update:
        raise AbsentError(f"no hub named {name!r} to update",
                          hint=f"`harness-manager hub add {name}` without --update adds it")
    bad = sorted(set(values) - set(HUB_FIELDS))
    if bad:
        raise UsageError(f"not hub keys: {', '.join(bad)}",
                         hint=f"a hub has {', '.join(HUB_FIELDS)} (and a token: "
                              f"`harness-manager hub token {name}`)")
    changes = {join_key(("hubs", name, k)): v for k, v in values.items()}
    if not exists:
        transport = values.get("transport") or ("rest" if values.get("url") else "ssh")
        changes.setdefault(join_key(("hubs", name, "transport")), transport)
    checked = dict(resolver.check_settable(k, v, text=text) for k, v in changes.items())
    # Check the result before anything is written: a hub that cannot be used is refused.
    trial = Resolver(resolver.schema, policy=resolver.policy, env=resolver.env,
                     user={**resolver.layer.values, **checked}, secrets=resolver.secrets)
    bad_cfg = resolve_hub(name, trial).config_problems()
    if bad_cfg:
        raise UsageError(bad_cfg[0], hint="nothing was written")
    resolver._write(checked)
    return resolve_hub(name, resolver)


def set_hub_token(name: str, resolver: Resolver, *, value: str | None = None,
                  ref: str | None = None, clear: bool = False) -> dict[str, Any]:
    """Set (``value``: into the secret store), point at (``ref``: ``file:PATH``, ``env:VAR``,
    ``fpgahub-login``) or clear the user's own token for a hub, machine hubs included (S3).
    Returns the token's status (never the token)."""
    check_name(name)
    if name not in hub_names(resolver):
        raise AbsentError(f"no hub named {name!r}", hint="`harness-manager hub list`")
    if sum(x is not None and x is not False for x in (value, ref, clear or None)) != 1:
        raise UsageError("give exactly one of a token, a reference, or clear")
    if not clear and resolve_hub(name, resolver).transport != "rest":
        raise UsageError(f"{name} is an SSH hub: it logs in with your SSH key, not a token",
                         hint="a token is for a REST hub (url = https://HUB:7246)")
    key = join_key(("hubs", name, "token"))
    store = resolver.secrets
    if store is None:                            # pragma: no cover - load() always has one
        raise UsageError("no secret store")
    had_ref = key in resolver.layer.values
    if value is not None:
        store.set(key, value)
        if had_ref:
            _write_ref(resolver, key, None)
    elif ref is not None:
        kind, arg = parse_ref(ref)
        if kind == "file":
            read_private_file(Path(arg).expanduser(), key)   # refused now, not at first use
        if kind in ("gh",):
            raise UsageError("gh is the GitHub token's source, not a hub's")
        _write_ref(resolver, key, None if kind == "store" else ref)
    else:
        store.delete(key)
        if had_ref:
            _write_ref(resolver, key, None)
    return dict(resolver.resolve(key).secret or {})


def _write_ref(resolver: Resolver, key: str, ref: str | None) -> None:
    """A secret row's reference goes in settings.toml (the resolver's ``set`` refuses secret
    rows on purpose: a value there would be a pasted secret)."""
    if resolver.files is not None:
        resolver.files.write({key: ref})
    if ref is None:
        resolver.layer.values.pop(key, None)
        resolver.layer.origin.pop(key, None)
    else:
        resolver.layer.values[key] = ref
        resolver.layer.origin[key] = "settings.toml"


def remove_hub(name: str, resolver: Resolver, *, force: bool = False) -> dict[str, Any]:
    """Remove ``[hubs.<name>]`` and its stored token. Refused for a machine hub, and while a
    board uses it (``force``: remove anyway; those boards then fail to open, naming it)."""
    check_name(name)
    _require_user_hub(name, resolver, "removing it")
    if name not in hub_names(resolver):
        raise AbsentError(f"no hub named {name!r}", hint="`harness-manager hub list`")
    users = boards_using(resolver, name)
    if users and not force:
        raise UsageError(f"the hub {name!r} is used by {', '.join(users)} in boards.toml",
                         hint="point those boards elsewhere first, or add --force")
    prefix = ("hubs", name)
    keys = [k for k in resolver.layer.values if split_key(k)[:2] == prefix
            and resolver.layer.origin.get(k) == "settings.toml"]
    if keys:
        resolver._write(dict.fromkeys(keys))
    token = join_key(("hubs", name, "token"))
    if resolver.secrets is not None and resolver.secrets.status(token).set:
        resolver.secrets.delete(token)
    resolver.layer.hubs = [h for h in resolver.layer.hubs if h != name]
    return {"removed": name, "boards": users}


# --- discovery: a board for a target the hub offers ---------------------------------------------


def _inline(value: Mapping[str, Any]) -> Any:
    """A mapping as a tomlkit inline table, written as a person would (``{ a = 1, b = 2 }``,
    nested tables inline too). A value that is already a tomlkit item keeps its own text."""
    import tomlkit

    def text(v: Any) -> str:
        if hasattr(v, "as_string") and not isinstance(v, str):
            return v.as_string().strip()
        if isinstance(v, Mapping):
            return _inline_text(v)
        return tomlkit.item(v).as_string()

    def _inline_text(m: Mapping[str, Any]) -> str:
        from .schema import quote_part

        return "{ " + ", ".join(f"{quote_part(k)} = {text(v)}" for k, v in m.items()) + " }" \
            if m else "{}"

    return tomlkit.parse(f"x = {_inline_text(value)}\n")["x"]


def add_board_for_target(hub_name: str, target: str, resolver: Resolver, *,
                         board_key: str | None = None, name: str | None = None,
                         match: Sequence[str] | None = None,
                         details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write a ``boards.toml`` entry for ``target`` on the hub (the design's "Add" button):
    ``hub = { use, target, shares = { mcc = "/dev/<target>/tty_00" } }``, ``via = "hub"``,
    ``name`` (the hub's description) and ``match`` (the target's ``board_ip``) from
    ``details`` (``hubtest.target_details``) unless given. Refused when the key exists or
    another board already has this hub and target."""
    hub = resolve_hub(hub_name, resolver)
    if not isinstance(target, str) or not _TARGET.match(target):
        raise UsageError(f"{target!r} is not an fpgahub target name, e.g. mps3_01_pl")
    key = board_key or target
    if not _NAME.match(key):
        raise UsageError(f"{key!r} is not a board key here (A-Z a-z 0-9 _ -)",
                         hint="give one with --board KEY")
    if key in resolver.layer.boards:
        raise UsageError(f"boards.toml already has a board {key!r}",
                         hint="give another key with --board KEY")
    for other in boards_using(resolver, hub.name):
        if resolver.layer.values.get(join_key(("boards", other, "hub", "target"))) == target:
            raise UsageError(f"boards.toml's {other!r} already uses {target} on {hub.name}")
    details = dict(details or {})
    net = details.get("network") if isinstance(details.get("network"), Mapping) else {}
    ip = net.get("board_ip") if isinstance(net.get("board_ip"), str) else ""
    label = name if name is not None else str(details.get("description") or "")
    addrs = list(match) if match is not None else ([ip] if ip else [])
    changes: dict[str, Any] = {}
    if addrs:
        changes[join_key(("boards", key, "match"))] = addrs
    if label:
        changes[join_key(("boards", key, "name"))] = label[:64].strip()
    changes[join_key(("boards", key, "via"))] = "hub"
    checked = dict(resolver.check_settable(k, v) for k, v in changes.items())
    hub_table = {"use": hub.name, "target": target,
                 "shares": {"mcc": f"/dev/{target}/tty_00"}}
    checked[join_key(("boards", key, "hub"))] = _inline(hub_table)
    if resolver.files is None:                   # pragma: no cover - tests use load()
        raise UsageError("no settings files to write")
    resolver.files.write(checked)
    notes = [] if addrs else [f"the hub did not say {target}'s address: add it with "
                              f"`match = [\"<ip>\"]` under [boards.{key}] in boards.toml"]
    return {"board": key, "hub": hub.name, "target": target, "match": addrs, "name": label,
            "path": str(resolver.files.boards_path), "notes": notes}


# --- migration: "Make this a hub…" --------------------------------------------------------------


def default_hub_name(host: str) -> str:
    """``mapstone-dev.ecs.soton.ac.uk`` -> ``mapstone-dev``; an IP address keeps its digits."""
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        label = re.sub(r"[.:]+", "-", h)
    except ValueError:
        label = h.split(".", 1)[0]
    label = re.sub(r"[^A-Za-z0-9_-]", "-", label).strip("-").lower()
    return label[:64] or "hub"


def _read_boards(resolver: Resolver) -> tuple[dict[str, Any], Any]:
    """``boards.toml`` as data (tomllib) and as a document (tomlkit, for its layout)."""
    import tomlkit

    from .policy import load_toml

    if resolver.files is None:                   # pragma: no cover - tests use load()
        raise UsageError("no settings files")
    path = resolver.files.boards_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AbsentError(f"there is no {path}") from None
    try:
        return load_toml(text), tomlkit.parse(text)
    except Exception as exc:  # noqa: BLE001 - tomllib's or tomlkit's parse error
        raise UsageError(f"{path} is not valid TOML ({exc}); it is not changed") from None


def adopt_inline_hub(board_key: str, resolver: Resolver, *,
                     as_name: str | None = None) -> dict[str, Any]:
    """Move a board's inline hub table into ``[hubs.<name>]`` plus ``hub.use`` (idempotent).

    Returns ``{board, hub, created, reused, via, changed, backup, notes}``; ``changed`` is
    False when the board already names a hub (nothing is written)."""
    data, doc = _read_boards(resolver)
    boards = data.get("boards") if isinstance(data.get("boards"), dict) else {}
    board = boards.get(board_key)
    if not isinstance(board, dict):
        raise AbsentError(f"boards.toml has no board {board_key!r}",
                          hint=f"boards: {', '.join(sorted(boards)) or 'none'}")
    table = board.get("hub")
    if not isinstance(table, dict):
        raise UsageError(f"boards.{board_key} has no hub table to make into a hub")
    if "use" in table:
        return {"board": board_key, "hub": table["use"], "created": False, "reused": True,
                "via": board.get("via", ""), "changed": False, "backup": "",
                "notes": [f"boards.{board_key} already uses the hub {table['use']!r}"]}
    unknown = sorted(set(table) - set(INLINE_HUB_KEYS) - set(BOARD_KEYS))
    if unknown:
        raise UsageError(f"boards.{board_key}.hub has unknown keys: {', '.join(unknown)}; "
                         "it is not changed")
    url = table.get("url") or ""
    host = table.get("host") or ""
    if not url and not host:
        raise UsageError(f"boards.{board_key}.hub has neither host nor url")
    from harness_manager.transports import hub_rest

    if url:
        hub_rest._normalise_url(url, f"boards.{board_key}.hub")   # refused as it would be
        from urllib.parse import urlsplit

        name_from = urlsplit(url).hostname or ""
    else:
        name_from = host
    name = check_name(as_name) if as_name else default_hub_name(name_from)
    definition: dict[str, Any] = {"transport": "rest" if url else "ssh"}
    for k in INLINE_HUB_KEYS:
        if k in table and k != "token_file":
            definition[k] = table[k]
    notes: list[str] = []
    ref = ""
    if table.get("token_file"):
        ref = f"file:{table['token_file']}"
        try:
            read_private_file(Path(str(table["token_file"])).expanduser(), "token_file")
        except FileNotFoundError:
            notes.append(f"the token file {table['token_file']} does not exist yet")
        except UsageError as exc:
            notes.append(f"{exc.message}: a named hub refuses it until it is fixed "
                         "(chmod 600, or set the token again)")
    # The hub: reuse one with the same definition, refuse one that differs.
    created, reused = False, False
    if name in hub_names(resolver):
        have = resolve_hub(name, resolver)
        trial = Resolver(resolver.schema, policy=resolver.policy, env={},
                         user={join_key(("hubs", name, k)): v for k, v in definition.items()})
        want = resolve_hub(name, trial)
        if have.table() != want.table() or (ref and have.token_ref != ref):
            raise UsageError(f"a hub named {name!r} already exists with a different "
                             "definition; boards.toml is not changed",
                             hint=f"name the new one: `harness-manager hub adopt {board_key} "
                                  "--as NAME`")
        extra = [f"{f} = {getattr(have, f)!r}" for f in ("jump", "holder", "lease_ttl",
                                                         "request_ttl", "queue_timeout")
                 if have.rows[f].source != "default"]
        if extra:
            notes.append(f"the hub {name} also sets {', '.join(extra)}; boards.{board_key} "
                         "now uses them too")
        reused = True
    else:
        _require_user_hub(name, resolver, "its definition")
        changes = {join_key(("hubs", name, k)): v for k, v in definition.items()}
        checked = dict(resolver.check_settable(k, v) for k, v in changes.items())
        resolver._write(checked)
        if ref:
            _write_ref(resolver, join_key(("hubs", name, "token")), ref)
        created = True
    # The board: its own keys plus use, comments and layout kept; via when it is this hub.
    edits: dict[str, Any] = {}
    node = doc["boards"][board_key]["hub"]
    import tomlkit.items as tki

    base = ("boards", board_key, "hub")
    if isinstance(node, tki.InlineTable):
        kept = {"use": name, **{k: node[k] for k in node if k in BOARD_KEYS}}
        edits[join_key(base)] = _inline(kept)
    else:
        edits[join_key((*base, "use"))] = name        # first: the table is never left empty
        for k in table:
            if k not in BOARD_KEYS:
                edits[join_key((*base, k))] = None
    via = board.get("via", "")
    via_host = host or definition.get("host", "")
    if isinstance(via, str) and via_host and via == f"ssh:{via_host}":
        edits[join_key(("boards", board_key, "via"))] = "hub"
        via = "hub"
    assert resolver.files is not None
    backup = resolver.files.boards_path.with_name(
        f"boards.toml.bak-{_today()}")
    resolver.files.write(edits)
    return {"board": board_key, "hub": name, "created": created, "reused": reused,
            "via": via, "changed": True, "backup": str(backup), "notes": notes}


def _today() -> str:
    import datetime as _dt

    return f"{_dt.date.today():%Y%m%d}"


__all__ = [
    "BOARD_KEYS", "HUB_FIELDS", "HUB_KEYS", "INLINE_HUB_KEYS", "Hub", "add_board_for_target",
    "add_hub", "adopt_inline_hub", "board_hub_table", "boards_using", "check_name",
    "default_hub_name", "hub_credential", "hub_names", "inline_hub_boards", "list_hubs",
    "load_resolver", "remove_hub", "resolve_hub", "set_hub_token",
]
