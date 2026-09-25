"""Where Harness Manager keeps a secret, and how it says so without showing it (SET-CORE; david S2).

A secret (the GitHub token, an fpgahub token, a plug's password) goes to the **OS keyring**
when this process can reach one, else to a **0600 file** in ``<config>/secrets/`` (a 0700
directory). ``secrets/index.json`` records which backend holds each secret, and never a
value, a length or a hash. So a later process that cannot reach the keyring (a service
started over ssh has no session bus) says "stored in your login keyring, which this process
cannot reach" (7 UNREACHABLE) instead of "not set", and never writes a second copy.
Setting the secret again from there moves it to the file, deliberately.

**The keyring** is the ``keyring`` package, but only its four real stores: the Secret
Service (GNOME Keyring, KWallet's bridge) over jeepney, KWallet, the macOS Keychain and the
Windows Credential Manager. Each is built by name, so ``keyrings.alt``'s plaintext and
"encrypted file" backends, a ``keyringrc.cfg`` or ``$PYTHON_KEYRING_BACKEND`` can never
choose where a secret goes. Nothing touches a keyring until a secret is set or read: on
Linux, not even then without ``$DBUS_SESSION_BUS_ADDRESS`` (it is never autolaunched). A
probe that does not answer in 3 s (a locked keyring waiting on its unlock prompt) counts as
unreachable. ``$HARNESS_MANAGER_KEYRING=off`` turns the keyring off (tests, headless CI).

**A file that others can read is refused**, not just warned about: whatever read it may
already hold the secret, so the fix is a new one, and the refusal says so. A file that is
not the user's, or is a symlink, is refused too.

**What leaves the store:** only ``get()`` and ``resolve_secret()``, called by the code that
sends the credential. Everything else (``status``, ``view``, errors, ``repr``) only ever
says ``{set, backend, where, reachable, why}``.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from harness_manager.core.errors import UnreachableError, UsageError

SERVICE = "harness-manager"
KEYRING_ENV = "HARNESS_MANAGER_KEYRING"
PROBE_TIMEOUT_S = 3.0
OP_TIMEOUT_S = 30.0
MAX_SECRET_BYTES = 16384
_NAME_RE = re.compile(r"^[^\s/\\\x00-\x1f\x7f]{1,200}$")
_FILE_SAFE = re.compile(r"[a-z0-9._-]")

#: The keyring backends accepted, by class: (index name, what a person is told).
ACCEPTED = {
    "keyring.backends.SecretService.Keyring":
        ("secret-service", "the Secret Service (your login keyring)"),
    "keyring.backends.libsecret.Keyring":
        ("secret-service", "the Secret Service (your login keyring)"),
    "keyring.backends.kwallet.DBusKeyring": ("kwallet", "KWallet"),
    "keyring.backends.macOS.Keyring": ("macos-keychain", "the macOS Keychain"),
    "keyring.backends.Windows.WinVaultKeyring":
        ("windows-credential", "the Windows Credential Manager"),
}
#: Per platform, the kinds tried in order, and the class that implements each.
_KINDS = {
    "linux": (("secret-service", "keyring.backends.SecretService", "Keyring"),
              ("kwallet", "keyring.backends.kwallet", "DBusKeyring")),
    "darwin": (("macos-keychain", "keyring.backends.macOS", "Keyring"),),
    "win32": (("windows-credential", "keyring.backends.Windows", "WinVaultKeyring"),),
}
_LABELS = {name: label for name, label in ACCEPTED.values()}


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise UsageError(f"{name!r} is not a secret's name")
    return name


def accepted(backend: Any) -> tuple[str, str] | None:
    """``(name, label)`` for one of the four real keyrings, else None (keyrings.alt, null,
    fail, chainer: refused)."""
    cls = type(backend)
    return ACCEPTED.get(f"{cls.__module__}.{cls.__qualname__}")


class Backend(Protocol):
    name: str
    label: str

    def available(self) -> tuple[bool, str]: ...
    def get(self, name: str) -> str | None: ...
    def put(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


def _with_timeout(fn: Callable[[], Any], timeout_s: float) -> tuple[bool, Any]:
    """Run ``fn`` on a daemon thread: ``(True, result)``, ``(False, exception)``, or
    ``(False, TimeoutError)`` when it does not return in time (the thread is left to its
    unlock prompt; it never holds the interpreter open)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["ok"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["err"] = exc

    t = threading.Thread(target=run, name="hm-keyring", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return False, TimeoutError()
    if "err" in box:
        return False, box["err"]
    return True, box.get("ok")


# --- the OS keyring ----------------------------------------------------------------------------


class KeyringBackend:
    """One kind of OS keyring (``secret-service``, ``kwallet``, ``macos-keychain``,
    ``windows-credential``) through the ``keyring`` package.

    ``impl`` is for tests: an object with ``get_password``/``set_password``/
    ``delete_password``, used as-is (the real class is never imported)."""

    def __init__(self, kind: str, *, env: Mapping[str, str] | None = None,
                 impl: Any = None, service: str = SERVICE,
                 probe_timeout_s: float = PROBE_TIMEOUT_S,
                 op_timeout_s: float = OP_TIMEOUT_S) -> None:
        if kind not in _LABELS:
            raise ValueError(f"not a keyring kind: {kind}")
        self.name = kind
        self.label = _LABELS[kind]
        self.env = os.environ if env is None else env
        self.service = service
        self.probe_timeout_s = probe_timeout_s
        self.op_timeout_s = op_timeout_s
        self._impl = impl
        self._avail: tuple[bool, str] | None = None

    def _build(self) -> Any:
        if self._impl is not None:
            return self._impl
        for kind, module, cls in (k for kinds in _KINDS.values() for k in kinds):
            if kind == self.name:
                import importlib

                obj = getattr(importlib.import_module(module), cls)()
                if accepted(obj) is None:          # pragma: no cover - a guard
                    raise RuntimeError(f"{module}.{cls} is not an accepted keyring")
                obj.priority                       # noqa: B018 - raises when not viable
                return obj
        raise RuntimeError(f"no {self.name} keyring on this system")

    def available(self, *, refresh: bool = False) -> tuple[bool, str]:
        if self._avail is not None and not refresh:
            return self._avail
        if self.env.get(KEYRING_ENV, "").strip().lower() == "off":
            self._avail = (False, f"the keyring is turned off (${KEYRING_ENV}=off)")
            return self._avail
        if self.name in ("secret-service", "kwallet") and self._impl is None and \
                not self.env.get("DBUS_SESSION_BUS_ADDRESS"):
            # ssh, cron, a service started outside the desktop: no bus. Never autolaunch one
            # (it would be empty, and forgotten).
            self._avail = (False, "no session bus in this process (an ssh or service "
                                  "session?)")
            return self._avail

        def probe() -> None:
            impl = self._build()
            impl.get_password(f"{self.service}-probe", "probe")
            self._impl = impl

        ok, got = _with_timeout(probe, self.probe_timeout_s)
        if ok:
            self._avail = (True, "")
        elif isinstance(got, TimeoutError):
            self._avail = (False, f"{self.label} did not answer in {self.probe_timeout_s:g} s "
                                  "(locked, and waiting on its unlock prompt?)")
        else:
            self._avail = (False, _why(got))
        return self._avail

    def _op(self, what: str, name: str, fn: Callable[[Any], Any]) -> Any:
        ok, why = self.available()
        if not ok:
            raise UnreachableError(f"{name}: {self.label} cannot be reached here: {why}")
        good, got = _with_timeout(lambda: fn(self._impl), self.op_timeout_s)
        if good:
            return got
        if isinstance(got, TimeoutError):
            raise UnreachableError(f"{name}: {self.label} did not answer in "
                                   f"{self.op_timeout_s:g} s ({what})")
        raise UnreachableError(f"{name}: {self.label} refused to {what} it: {_why(got)}")

    def get(self, name: str) -> str | None:
        return self._op("read", name, lambda k: k.get_password(self.service, name))

    def put(self, name: str, value: str) -> None:
        self._op("store", name, lambda k: k.set_password(self.service, name, value))

    def delete(self, name: str) -> None:
        def rm(k: Any) -> None:
            with contextlib.suppress(Exception):   # already gone: fine
                k.delete_password(self.service, name)
        self._op("delete", name, rm)


def _why(exc: BaseException) -> str:
    """An exception as a reason. The ``keyring`` package's messages never carry the value,
    but it is cut short anyway, and its type is always said."""
    text = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def default_keyrings(env: Mapping[str, str] | None = None,
                     platform: str = sys.platform) -> list[KeyringBackend]:
    """The keyrings this OS might have, in order. Building them touches nothing."""
    env = os.environ if env is None else env
    if env.get(KEYRING_ENV, "").strip().lower() == "off":
        return []
    key = "linux" if platform.startswith(("linux", "freebsd")) else \
        "win32" if platform.startswith("win") else platform
    return [KeyringBackend(kind, env=env) for kind, _, _ in _KINDS.get(key, ())]


# --- the fallback: a 0600 file -----------------------------------------------------------------


def file_name(name: str) -> str:
    """A secret's name as a file name that is the same on every OS and file system:
    ``a-z 0-9 . _ -`` as they are, anything else (upper case too) as ``%XX``."""
    out = []
    for i, ch in enumerate(name):
        if _FILE_SAFE.fullmatch(ch) and not (i == 0 and ch == "."):
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out) + ".secret"


def read_private_file(path: Path, what: str) -> str:
    """One line from a file only its owner can read. ``UsageError`` otherwise (never the
    content)."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UsageError(f"{what}: {path} cannot be read ({exc.strerror or exc}; "
                         "a symlink is refused)") from None
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        if os.name == "posix":
            st = os.fstat(fh.fileno())
            mode = stat.S_IMODE(st.st_mode)
            if mode & 0o077:
                raise UsageError(f"{what}: {path} is readable by others (mode {mode:o}); "
                                 "it is not used",
                                 hint="replace the secret (it may be known): set it again, "
                                      "and it is written 0600")
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise UsageError(f"{what}: {path} belongs to another user; it is not used")
        text = fh.read()
    line = text.rstrip("\r\n")
    if not line or "\n" in line:
        raise UsageError(f"{what}: {path} must hold one secret on one line")
    return line


class FileBackend:
    """``<root>/<name>.secret``, 0600, in a 0700 directory; one secret per file."""

    name = "file"
    label = "a private file"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def available(self, *, refresh: bool = False) -> tuple[bool, str]:
        return True, ""

    def path(self, name: str) -> Path:
        return self.root / file_name(check_name(name))

    def ensure_dir(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.root, 0o700)
        return self.root

    def get(self, name: str) -> str | None:
        try:
            return read_private_file(self.path(name), name)
        except FileNotFoundError:
            return None

    def put(self, name: str, value: str) -> None:
        d = self.ensure_dir()
        tmp = d / f".{uuid.uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path(name))
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def delete(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(name).unlink()


# --- the store ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretStatus:
    name: str
    set: bool
    backend: str = ""                 # "secret-service" | "file" | … | "" (not set)
    where: str = ""                   # for a person: "the Secret Service (your login keyring)"
    reachable: bool = True            # False: stored where this process cannot reach
    why: str = ""

    def view(self) -> dict[str, Any]:
        """The API's shape: never a value, a length or a hash."""
        return {"set": self.set, "backend": self.backend, "where": self.where,
                "reachable": self.reachable, "why": self.why}


class SecretStore:
    """The index plus the backends, under ``<config>/secrets``."""

    def __init__(self, root: Path | str, *, keyrings: Sequence[Backend] | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self.root = Path(root)
        self.dir = self.root / "secrets"
        self.file = FileBackend(self.dir)
        self.keyrings: list[Backend] = list(default_keyrings(env) if keyrings is None
                                            else keyrings)
        self.index_path = self.dir / "index.json"
        self.problems: list[str] = []

    def backends(self) -> dict[str, Backend]:
        out: dict[str, Backend] = {}
        for k in self.keyrings:
            out.setdefault(k.name, k)
        out["file"] = self.file
        return out

    # --- the index: names and where, never values ---

    def _index(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            msg = f"{self.index_path} cannot be read ({type(exc).__name__}); files are still found"
            if msg not in self.problems:
                self.problems.append(msg)
            return {}
        entries = data.get("secrets", {}) if isinstance(data, dict) else {}
        return {k: v for k, v in entries.items()
                if isinstance(v, dict) and isinstance(v.get("backend"), str)}

    def _write_index(self, index: dict[str, dict[str, str]]) -> None:
        self.file.ensure_dir()
        tmp = self.dir / f".index.{uuid.uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "secrets": index}, fh, indent=1, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.index_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def _where(self, name: str) -> str | None:
        entry = self._index().get(name)
        if entry is not None:
            return entry["backend"]
        # Not indexed (an index lost or hand-deleted): a private file still counts.
        return "file" if self.file.path(name).exists() else None

    def names(self) -> list[str]:
        return sorted(self._index())

    # --- the four operations ---

    def where_new(self) -> tuple[Backend, str]:
        """Where a new secret goes: the first reachable keyring, else the file, and why."""
        whys = []
        for k in self.keyrings:
            ok, why = k.available()
            if ok:
                return k, ""
            whys.append(f"{k.label}: {why}")
        return self.file, ("no keyring: " + "; ".join(whys)) if whys else \
            "no keyring on this system"

    def set(self, name: str, value: str) -> SecretStatus:
        check_name(name)
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value \
                or len(value.encode("utf-8")) > MAX_SECRET_BYTES:
            raise UsageError(f"{name}: a secret is one non-empty line "
                             f"(at most {MAX_SECRET_BYTES} bytes)")
        index = self._index()
        old = index.get(name, {}).get("backend") or self._where(name)
        backend, why = self.where_new()
        backend.put(name, value)                   # the new copy first
        if old and old != backend.name and old in self.backends():
            with contextlib.suppress(Exception):   # then the old one, when it can be reached
                self.backends()[old].delete(name)
        index[name] = {"backend": backend.name,
                       "updated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self._write_index(index)
        st = self.status(name)
        return replace(st, why=why) if why else st

    def get(self, name: str) -> str | None:
        """The value, for the code that sends it. ``None``: not set. ``UnreachableError``
        (7): stored where this process cannot reach (never a silent ``None``).
        ``UsageError``: a private file that others can read (refused)."""
        where = self._where(check_name(name))
        if where is None:
            return None
        backend = self.backends().get(where)
        if backend is None:
            raise UnreachableError(f"{name} is stored in {_LABELS.get(where, where)}, which "
                                   "this system does not have",
                                   hint=f"set it again here: `harness-manager config "
                                        f"set-secret {name}`")
        ok, why = backend.available()
        if not ok:
            raise UnreachableError(f"{name} is stored in {backend.label}, which this process "
                                   f"cannot reach: {why}",
                                   hint="start the service from your desktop session, or set "
                                        "the secret again here (it then moves to a private "
                                        "file)")
        return backend.get(name)

    def status(self, name: str) -> SecretStatus:
        where = self._where(check_name(name))
        if where is None:
            return SecretStatus(name, False)
        backend = self.backends().get(where)
        if backend is None:
            return SecretStatus(name, True, where, _LABELS.get(where, where), reachable=False,
                                why="this system has no such keyring")
        ok, why = backend.available()
        if ok and backend is self.file:
            try:
                self.file.get(name)
            except UsageError as exc:
                return SecretStatus(name, True, "file", self.file.label, reachable=False,
                                    why=exc.message)
        return SecretStatus(name, True, backend.name, backend.label, reachable=ok, why=why)

    def delete(self, name: str) -> SecretStatus:
        index = self._index()
        where = (index.pop(check_name(name), None) or {}).get("backend") or self._where(name)
        if where and where in self.backends():
            self.backends()[where].delete(name)
        self.file.delete(name)                     # a stray file copy goes too
        self._write_index(index)
        return self.status(name)


# --- the lookup order for the code that sends a secret -----------------------------------------


@dataclass(frozen=True)
class SecretValue:
    """A secret and where it came from. ``repr`` never shows it."""

    value: str = field(repr=False)
    source: str                       # "$FPGAHUB_TOKEN", "the Secret Service …", "gh auth token"

    def __str__(self) -> str:
        return f"<secret from {self.source}>"


#: A legacy source: a label, and a function returning the value or None.
Legacy = tuple[str, Callable[[], "str | None"]]


def parse_ref(text: Any) -> tuple[str, str]:
    """A secret row's value in ``settings.toml`` is a reference, never the secret:
    ``store`` (the default), ``file:PATH``, ``env:VAR``, or a legacy source by name
    (``gh``, ``fpgahub-login``). ``UsageError`` for anything else (and the text is never
    repeated: it may be a pasted secret)."""
    if text in (None, "", "store"):
        return "store", ""
    if isinstance(text, str):
        if text.startswith("file:") and len(text) > 5:
            return "file", text[5:]
        if text.startswith("env:") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text[4:]):
            return "env", text[4:]
        if text in ("gh", "fpgahub-login"):
            return text, ""
    raise UsageError("a secret's setting holds a reference (store, file:PATH, env:VAR, gh or "
                     "fpgahub-login), not the secret itself; the value is ignored",
                     hint="move the secret with `harness-manager config set-secret KEY`")


def resolve_secret(name: str, *, store: SecretStore, env: Mapping[str, str] | None = None,
                   env_var: str = "", env_ok: Callable[[Mapping[str, str]], bool] | None = None,
                   ref: Any = None, legacy: Sequence[Legacy] = ()) -> SecretValue | None:
    """The secret the code should send, highest first: the env var (when ``env_ok`` agrees:
    ``$FPGAHUB_TOKEN`` only for its ``$FPGAHUB_ADDR``), then the user's reference (the store,
    by default), then each legacy source (``token_file``, ``gh auth token``, the fpgahub
    login) in order. ``None``: none has it.

    A secret stored where this process cannot reach raises ``UnreachableError`` (7): it is
    never quietly replaced by a legacy source."""
    env = os.environ if env is None else env
    if env_var and env.get(env_var, "").strip() and (env_ok is None or env_ok(env)):
        return SecretValue(env[env_var].strip(), f"${env_var}")
    kind, arg = parse_ref(ref)
    if kind == "file":
        return SecretValue(read_private_file(Path(arg).expanduser(), name), f"the file {arg}")
    if kind == "env":
        value = env.get(arg, "").strip()
        return SecretValue(value, f"${arg}") if value else None
    by_label = dict(legacy)
    if kind != "store":
        fn = next((f for label, f in legacy if label == kind or label.startswith(kind)), None)
        value = fn() if fn is not None else None
        return SecretValue(value, kind) if value else None
    value = store.get(name)
    if value is not None:
        return SecretValue(value, store.status(name).where)
    for label, fn in by_label.items():
        value = fn()
        if value:
            return SecretValue(value, label)
    return None


def legacy_gh(gh: str | None = None) -> Legacy:
    """``gh auth token`` (``services.update.github.gh_cli_token``)."""
    def read() -> str | None:
        from harness_manager.services.update.github import gh_cli_token

        return gh_cli_token(gh=gh)
    return ("gh", read)


def legacy_token_file(path: str, *, strict: bool = True) -> Legacy:
    """A ``token_file`` the user already has (boards.toml ``hub.token_file``).

    ``strict`` refuses a file others can read, as the store does. Today's reader
    (``hub_rest._read_token_file``) only warns: SET-HUB-2 chooses when it switches."""
    def read() -> str | None:
        if strict:
            return read_private_file(Path(path).expanduser(), path)
        from harness_manager.transports.hub_rest import _read_token_file

        return _read_token_file(path)
    return (f"token_file {path}", read)


def legacy_fpgahub_login(host: str, port: int) -> Legacy:
    """The token ``fpgahub login`` saved, when it is for this hub."""
    def read() -> str | None:
        from harness_manager.transports import hub_rest

        saved = hub_rest._read_login_store(hub_rest.fpgahub_login_store())
        addr = saved.get("addr")
        if isinstance(addr, str) and hub_rest._addr_key(addr) == (host.lower(), port):
            tok = saved.get("token")
            return tok if isinstance(tok, str) and tok else None
        return None
    return ("fpgahub-login", read)
