"""The Linux harness's SSH claim and its SSH data plane (lane LINUX-CLAIM).

What the board does (platform ``docs/contracts/net-protocol.md`` v0.14, "Identify",
"TOFU first-key claim", "Slot images / The lock"; ``HARNESSD_CONTRACT.md`` §9.6-§9.8a):

- A Linux board ships **unclaimed**: key-only SSH, no key. The first TFTP WRQ named exactly
  ``authorized_keys`` (no MPS3 header, <= 16 KiB) **claims** it. Every later claim gets TFTP
  ERROR 2. ``mps3-unclaim`` on the serial console undoes it.
- UDP 6899 ``identify`` says ``ssh: {claimed, host_key_sha256}``. It does not say WHICH key
  claimed the board (the CLCD's engine row shows a prefix of it; the wire does not).
- Once claimed (S12, david 2026-09-24) the slot mutations are refused from any peer that is
  not the board itself: ``slot locked: board claimed (use ssh)``. The owner comes in over SSH,
  which is the authentication: ``ssh -J HUB root@BOARD -L 127.0.0.1:p:127.0.0.1:6900``.

What Harness Manager adds here:

- **The claim state** (``claim_status``): identify's ``ssh`` block, compared with what THIS
  Harness Manager recorded when it claimed (``<state>/ssh/claims.json``: who, which key, when)
  and the host key it pinned (boards.toml ``boards.<b>.ssh.host_key``). ``mine``, ``other``,
  ``unclaimed`` or ``unknown``; ``claimed = {by, key_fp, at}`` or None. On the LAN identify is
  one UDP round trip. Through a hub UDP cannot ride the SSH tunnel, so the probe runs ON the
  hub: pyverify's own ``identify`` module, shipped in the ssh command (``_hub_script``), run
  by the hub's newest python3 (its system python may be 3.6). ``info`` never does that hub
  round trip (it shows the last check, from ``claims.json``); ``board claim-status`` and the
  claim itself do.
- **The claim** (``claim``): never automatic. The TFTP ``authorized_keys`` put is pyverify's
  ``tftp_put`` (in process on the LAN, shipped to the hub like identify otherwise). Then the
  host key is captured over the real SSH path (``StrictHostKeyChecking=accept-new`` into a
  scratch known_hosts) and pinned only when its fingerprint equals identify's
  ``host_key_sha256``. ``adopt=True`` does the second half alone, for a board already claimed
  with your key elsewhere (pyverify ``claim``, the B1 runbook).
- **The data plane** (``ssh_argv``, ``open_forward``): ``ssh [-J HUB] -l root BOARD`` with the
  pinned key in a Harness-Manager-owned known_hosts file, ``StrictHostKeyChecking=yes``, key-only
  auth and ``boards.<b>.ssh.key`` when set. A changed host key is refused LOUDLY
  (``HostKeyChangedError``), before a connection when identify already shows it, and from
  ssh's own refusal otherwise. Never a ControlMaster (FINDINGS_TRIAGE #20: a lingering forward
  to the board's loopback would pass the claim lock for anyone on this host).
- **The lock's refusal** (``refusal_error``): ``slot locked: board claimed (use ssh)`` becomes
  ``ClaimLockedError``; the fabric identity lock (``identity lock: <reason>``, a different
  thing: the card image and the FPGA's static disagree) becomes an ``IncompatibleError``.

Bare metal has none of this: ``claim_status`` is None (no ``claim`` key in ``info``) and the
claim is ``UnavailableError``.

Test seams: ``DEFAULT_RUN`` (a one-shot ssh: ``(argv, timeout) -> RunResult``), the hub's runner
(``hub.DEFAULT_RUNNER_FACTORY``) and the tunnel's launcher (``tunnel.DEFAULT_LAUNCHER``).
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import hashlib
import inspect
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HarnessError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import LinkKind

from .constants import IMPL_LINUX

log = logging.getLogger(__name__)

CAPABILITY = "ssh_claim"
DEFAULT_USER = "root"
#: pyverify ``claim``'s default key (B1 runbook P2). Only read when ``--key`` and
#: ``boards.<b>.ssh.key`` are both unset.
DEFAULT_CLAIM_KEY = "~/.ssh/id_ed25519.pub"
#: The TOFU filename and its cap (net-protocol "TOFU first-key claim").
TOFU_FILENAME = "authorized_keys"
TOFU_MAX_BYTES = 16 * 1024
#: The lock's refusal, verbatim (net-protocol "The lock"; pyverify.slot.SLOT_LOCKED_ERR).
SLOT_LOCKED_ERR = "slot locked: board claimed (use ssh)"
#: The fabric identity lock's prefix (net-protocol "Identity lock").
IDENTITY_LOCK_PREFIX = "identity lock:"

STATE_MINE = "mine"
STATE_OTHER = "other"
STATE_UNCLAIMED = "unclaimed"
STATE_UNKNOWN = "unknown"

#: How long a LAN identify answer is reused (``info`` asks on every read).
LAN_TTL_S = 30.0
IDENTIFY_TIMEOUT_S = 1.0
HUB_TIMEOUT_S = 30.0
SSH_TIMEOUT_S = 30.0
#: After a fresh claim harnessd runs mps3-keys-sync (<= 2 s) before dropbear knows the key.
KEYS_SYNC_RETRIES_S = (0.5, 1.0, 2.0, 3.0)

#: One-shot ssh options (no forwards of ours, so the config's are cleared).
ONE_SHOT_OPTIONS: tuple[str, ...] = (
    "-o", "ControlPath=none", "-o", "ControlMaster=no", "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15", "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=no",
)
#: Key-only, never a prompt (DL5: the root password exists only on the serial console).
KEY_ONLY: tuple[str, ...] = (
    "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
)

_KEY_TYPES = ("ssh-", "ecdsa-", "sk-")
_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_FP = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,64}$")


# --- errors ------------------------------------------------------------------------------------


class HostKeyChangedError(RefusedError):
    """The board's SSH host key is not the one pinned: refused, loudly, never re-trusted."""


class ClaimLockedError(RefusedError):
    """The harness refused an operation because the board is claimed (S12)."""


# --- fingerprints and key lines ------------------------------------------------------------------


def fingerprint(blob_b64: str) -> str:
    """OpenSSH's ``SHA256:`` fingerprint of a base64 key blob (``ssh-keygen -lf``)."""
    try:
        raw = base64.b64decode(blob_b64.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise UsageError(f"not a base64 SSH key blob: {blob_b64[:24]!r}...") from exc
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def parse_key_line(line: str) -> tuple[str, str]:
    """``"ssh-ed25519 AAAA... comment"`` -> ``("ssh-ed25519", "AAAA...")``. ``UsageError`` if not."""
    parts = line.split()
    if len(parts) < 2 or not parts[0].startswith(_KEY_TYPES) or not _B64.match(parts[1]):
        raise UsageError(f"not an OpenSSH public key line: {line[:40]!r}")
    return parts[0], parts[1]


def pin_fingerprint(pin: str) -> str:
    """The fingerprint a pin stands for: the pin itself (``SHA256:...``) or its key's."""
    pin = (pin or "").strip()
    if not pin:
        return ""
    if pin.startswith("SHA256:"):
        return pin
    return fingerprint(parse_key_line(pin)[1])


def check_pin(value: Any) -> str:
    """A settings check for ``boards.*.ssh.host_key``: a key line, a fingerprint, or empty."""
    if not value:
        return ""
    if isinstance(value, str) and _FP.match(value.strip()):
        return ""
    try:
        parse_key_line(str(value))
    except UsageError:
        return "must be an SSH public key line (ssh-ed25519 AAAA...) or a SHA256: fingerprint"
    return ""


def read_claim_key(path: Path) -> tuple[bytes, list[str]]:
    """The public key file a claim sends, checked as pyverify ``claim`` checks it
    (``pyverify/cli.py`` ``_cmd_claim``): OpenSSH public key lines, <= 16 KiB."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read the claim key {path}: {exc.strerror or exc}",
                         hint="give the PUBLIC key with --key (e.g. ~/.ssh/id_ed25519.pub)") from exc
    lines = [ln for ln in data.decode("ascii", "replace").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines or not all(ln.split()[0].startswith(_KEY_TYPES) for ln in lines):
        raise UsageError(f"{path} does not look like an OpenSSH PUBLIC key file",
                         hint="the .pub file, never the private key")
    if len(data) > TOFU_MAX_BYTES:
        raise UsageError(f"{path} is {len(data)} bytes; a claim is capped at 16 KiB")
    for ln in lines:
        parse_key_line(ln)
    return data, lines


# --- where things live ---------------------------------------------------------------------------


def _state_dir() -> Path:
    from harness_manager.engine import resolve_state_dir  # the one state-dir rule

    return resolve_state_dir()


def ssh_dir() -> Path:
    path = _state_dir() / "ssh"
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def _slug(board_id: str) -> str:
    return hashlib.sha256(board_id.encode("utf-8")).hexdigest()[:12]


def host_key_alias(board_id: str) -> str:
    """The name the board's key is filed under (never its address: a bare-metal image, another
    card or another board may answer on the same 192.168.10.101)."""
    return f"harness-manager-{_slug(board_id)}"


def known_hosts_path(board_id: str) -> Path:
    return ssh_dir() / f"known_hosts.{_slug(board_id)}"


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ClaimRecords:
    """``<state>/ssh/claims.json``: what this Harness Manager claimed (who, which key, when)
    and the last claim state it saw on each board (so ``info`` through a hub has one)."""

    FILE = "claims.json"
    _mu = threading.Lock()

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory

    @property
    def path(self) -> Path:
        return (self._dir or ssh_dir()) / self.FILE

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, board_id: str) -> dict[str, Any]:
        rec = self._read().get(board_id)
        return rec if isinstance(rec, dict) else {}

    def update(self, board_id: str, **fields: Any) -> dict[str, Any]:
        with self._mu:
            data = self._read()
            rec = data.get(board_id) if isinstance(data.get(board_id), dict) else {}
            for k, v in fields.items():
                if v is None:
                    rec.pop(k, None)
                else:
                    rec[k] = v
            data[board_id] = rec
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True) + "\n")
            return rec


# --- boards.toml: boards.<b>.ssh ------------------------------------------------------------------

SSH_KEYS = ("user", "key", "host_key")


def board_key(candidate: Any) -> str:
    """The board's table in boards.toml: its own (``[boards.lab]``), else its board_id."""
    from .hub import _board_config

    try:
        cfg = _board_config(candidate)
    except HarnessError:
        cfg = None
    return cfg.key if cfg is not None else candidate.board_id


def ssh_config(candidate: Any) -> dict[str, str]:
    """boards.toml ``ssh`` for this board: ``{user, key, host_key}`` (missing: "")."""
    from .hub import board_tables

    try:
        raw = board_tables(candidate).get("ssh", {})
    except HarnessError:
        raw = {}
    where = f"boards.toml ssh for {candidate.board_id}"
    if not isinstance(raw, dict):
        raise UsageError(f"{where} must be a table: {{ user = \"root\", key = \"~/.ssh/id_ed25519\" }}")
    unknown = set(raw) - set(SSH_KEYS)
    if unknown:
        raise UsageError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    out = {k: "" for k in SSH_KEYS}
    for k in SSH_KEYS:
        v = raw.get(k, "")
        if not isinstance(v, str):
            raise UsageError(f"{where}.{k} must be a string")
        out[k] = v.strip()
    if out["user"] and (out["user"].startswith("-") or any(c.isspace() for c in out["user"])):
        raise UsageError(f"{where}.user must be a user name")
    problem = check_pin(out["host_key"])
    if problem:
        raise UsageError(f"{where}.host_key {problem}",
                         hint="`harness-manager board claim TARGET --adopt` pins it for you")
    return out


def write_ssh_settings(candidate: Any, changes: Mapping[str, str | None]) -> str:
    """Write ``boards.<b>.ssh.<k>`` through the settings writer (tomlkit, comments kept,
    atomic, locked). Returns the table key used."""
    from harness_manager.settings.files import SettingsFiles
    from harness_manager.settings.schema import join_key

    key = board_key(candidate)
    SettingsFiles(_state_dir()).write(
        {join_key(("boards", key, "ssh", k)): v for k, v in changes.items()})
    return key


# --- the hub-side probe: pyverify's own modules, run on the hub ------------------------------------

#: The hub's system python may be 3.6; pyverify needs 3.8+. The newest one found runs the
#: script (``$0``); the arguments follow.
_PY_PICK = ('for p in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; '
            'do command -v "$p" >/dev/null 2>&1 && exec "$p" -c "$0" "$@"; done; '
            'echo "harness-manager: no python3 on the hub" >&2; exit 127')

_DRIVER = r'''
import base64, json, sys, types, zlib
if sys.version_info < (3, 8):
    print(json.dumps({"ok": False, "err": "the hub's python is %d.%d; pyverify needs 3.8+"
                      % sys.version_info[:2]}))
    sys.exit(0)
def _load(name, src):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(src, "<pyverify %s>" % name, "exec"), mod.__dict__)
    return mod
_mods = json.loads(zlib.decompress(base64.b64decode(_BLOB)).decode("utf-8"))
ident = _load("hm_pyverify_identify", _mods["identify"])
pusher = _load("hm_pyverify_pusher", _mods["pusher"])
op, host, port, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
try:
    if op == "identify":
        r = ident.identify(host, port, timeout=timeout, retries=1)
        print(json.dumps({"ok": True, "reply": r.raw}))
    elif op == "claim":
        data = base64.b64decode(sys.argv[5])
        pusher.tftp_put(data, host, port, filename="authorized_keys", timeout_s=timeout)
        print(json.dumps({"ok": True}))
    else:
        print(json.dumps({"ok": False, "err": "unknown op %s" % op}))
except Exception as exc:
    print(json.dumps({"ok": False, "err": str(exc) or type(exc).__name__,
                      "kind": type(exc).__name__}))
'''

_SCRIPT: str | None = None


def _hub_script() -> str:
    """pyverify's ``identify`` and ``pusher`` (both stdlib-only single modules), compressed,
    plus the driver: one ``python -c`` argument, ~20 KB, well under a shell's 128 KiB."""
    global _SCRIPT
    if _SCRIPT is None:
        import pyverify.identify as pv_identify
        import pyverify.pusher as pv_pusher

        mods = {"identify": inspect.getsource(pv_identify), "pusher": inspect.getsource(pv_pusher)}
        blob = base64.b64encode(zlib.compress(json.dumps(mods).encode("utf-8"), 9)).decode("ascii")
        _SCRIPT = f"_BLOB = {blob!r}\n{_DRIVER}"
    return _SCRIPT


def hub_argv(op: str, host: str, port: int, timeout: float, *extra: str) -> list[str]:
    """What the hub runner runs: ``sh -c <pick a python> <script> OP HOST PORT TIMEOUT ...``."""
    return ["sh", "-c", _PY_PICK, _hub_script(), op, host, str(port), f"{timeout:g}", *extra]


def _hub_call(hub: str, jump: str, argv: Sequence[str], timeout: float) -> dict[str, Any]:
    from . import hub as _hub

    runner = _hub.DEFAULT_RUNNER_FACTORY(hub, None, jump=jump) if jump else \
        _hub.DEFAULT_RUNNER_FACTORY(hub, None)
    try:
        res = runner(list(argv), timeout=timeout)
    except TimeoutError as exc:
        raise UnreachableError(f"the hub {hub} did not answer within {timeout:.0f}s: {exc}") from exc
    except OSError as exc:
        raise UnreachableError(f"cannot run ssh to the hub {hub}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - pyverify's LeaseError ("cannot run ssh")
        raise UnreachableError(f"cannot reach the hub {hub}: {exc}") from exc
    out = (getattr(res, "stdout", "") or "").strip().splitlines()
    for line in reversed(out):
        with contextlib.suppress(ValueError):
            obj = json.loads(line)
            if isinstance(obj, dict) and "ok" in obj:
                return obj
    why = (getattr(res, "stderr", "") or "").strip().splitlines()
    raise UnreachableError(
        f"the hub {hub} could not run the probe (status {getattr(res, 'returncode', '?')}"
        f"{': ' + why[-1] if why else ''})",
        hint=f"check `ssh {hub} true` works without a prompt, and that the hub has python3.8+")


# --- the one-shot ssh -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def run_ssh(argv: Sequence[str], timeout: float) -> RunResult:
    """Run one ssh command; stdin is closed (BatchMode: nothing may prompt)."""
    try:
        proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UnreachableError(f"ssh did not finish within {timeout:.0f}s") from exc
    except OSError as exc:
        raise UnreachableError(f"cannot run {argv[0]}: {exc}", hint="install the OpenSSH client") from exc
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


#: The one-shot ssh the claim uses (tests replace it).
DEFAULT_RUN: Callable[[Sequence[str], float], RunResult] = run_ssh


def _stderr_line(text: str) -> str:
    from .tunnel import _explain

    return _explain(text) or (text.strip().splitlines() or [""])[-1]


def _host_key_failed(text: str) -> bool:
    low = text.lower()
    return ("host key verification failed" in low or "remote host identification has changed" in low
            or "no ed25519 host key is known" in low or "host key for" in low and "has changed" in low)


# --- the adapter ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What identify said about the board's SSH (``claimed`` None: it did not say)."""

    claimed: bool | None
    host_key: str
    source: str
    at: float
    error: str = ""


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _me() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry (a container)
        user = os.environ.get("USER", "") or "unknown"
    return f"{user}@{socket.gethostname()}"


class Mps3Claim:
    """``session.claim``: the Linux harness's SSH claim and SSH reach for one MPS3 session."""

    def __init__(self, session: Any, *, records: ClaimRecords | None = None) -> None:
        self._session = session
        self._records = records or ClaimRecords()
        self._mu = threading.Lock()
        self._lan: Observation | None = None

    # -- facts --------------------------------------------------------------------------------

    @property
    def candidate(self) -> Any:
        return self._session.candidate

    @property
    def board_id(self) -> str:
        return self.candidate.board_id

    def _impl(self, identity: Any = None) -> str:
        if identity is None:
            identity = getattr(self.candidate, "identity", None)
            shell = getattr(self._session, "shell", None)
            if shell is not None:
                with contextlib.suppress(HarnessError):
                    identity = self._session.identity()
        return str(getattr(identity, "harness_impl", "") or "")

    def config(self) -> dict[str, str]:
        return ssh_config(self.candidate)

    def user(self) -> str:
        return self.config()["user"] or DEFAULT_USER

    def route(self) -> tuple[str, str]:
        """``(hub, hub_jump)``: the hub the board sits behind ("" = the board is on this LAN)
        and a jump host on the way to that hub (a named hub's ``jump``)."""
        from .tunnel import _hub_jump
        from .xvc import _jump_host

        hub = _jump_host(self.candidate)
        return hub, (_hub_jump(self.candidate, hub) if hub else "")

    def proxy_jump(self) -> str:
        """The ``-J`` value to reach the board: ``HUB``, ``JUMP,HUB`` or "" (on the LAN)."""
        hub, jump = self.route()
        return ",".join(h for h in (jump, hub) if h)

    def board_host(self) -> str:
        """The board's own address, as the hub (or this LAN) reaches it."""
        from .shell import parse_endpoint

        reach = getattr(self._session, "reach", None)
        if getattr(reach, "remote_host", ""):
            return reach.remote_host
        eth = next((lk for lk in self.candidate.links if lk.kind == LinkKind.ETHERNET), None)
        if eth is None:
            return ""
        return parse_endpoint(eth.address, 0)[0]

    def _tftp_port(self) -> int:
        from .deploy import TFTP_PORT_ENV, _env_port

        hub, _ = self.route()
        if not hub and getattr(self._session, "tftp_port", None):
            return int(self._session.tftp_port)
        from pyverify.pusher import TFTP_PORT

        return _env_port(TFTP_PORT_ENV) or TFTP_PORT

    # -- observing --------------------------------------------------------------------------

    def observe(self, *, refresh: bool = False, hub_ok: bool = False) -> Observation:
        """identify's ``ssh`` block. On the LAN: asked (cached ``LAN_TTL_S``). Through a hub:
        asked on the hub only when ``hub_ok``; otherwise the last check on record."""
        from . import identify as _identify

        hub, jump = self.route()
        now = time.time()
        if not hub:
            with self._mu:
                cached = self._lan
            if cached is not None and not refresh and now - cached.at < LAN_TTL_S:
                return cached
            host = self.board_host()
            try:
                # info asks once (<= 1 s, cached LAN_TTL_S); a refresh or a claim asks twice
                reply = _identify.identify(host, timeout=IDENTIFY_TIMEOUT_S,
                                           retries=1 if refresh else 0)
                obs = self._from_raw(reply.raw, f"identify {host}", now)
            except (UnreachableError, UsageError) as exc:
                obs = Observation(None, "", f"identify {host}", now, exc.message)
            with self._mu:
                self._lan = obs
            self._remember(obs)
            return obs
        if not hub_ok:
            seen = self._records.get(self.board_id).get("observed") or {}
            if isinstance(seen, dict) and seen.get("at"):
                claimed = seen.get("claimed")
                return Observation(claimed if isinstance(claimed, bool) else None,
                                   str(seen.get("host_key") or ""),
                                   f"{seen.get('source') or 'identify via ' + hub} (last check)",
                                   float(seen["at"]))
            return Observation(None, "", f"identify via {hub}", now,
                               "not checked through the hub yet (identify is UDP: it does not "
                               "ride the SSH tunnel); `harness-manager board claim-status` asks "
                               "the hub")
        port = _identify.identify_port()
        host = self.board_host()
        try:
            out = _hub_call(hub, jump, hub_argv("identify", host, port, 2.0), HUB_TIMEOUT_S)
        except UnreachableError as exc:
            return Observation(None, "", f"identify via {hub}", now, exc.message)
        if not out.get("ok"):
            return Observation(None, "", f"identify via {hub}", now,
                               f"the hub asked, the board did not answer identify: {out.get('err')}")
        obs = self._from_raw(out.get("reply") or {}, f"identify via {hub}", now)
        self._remember(obs)
        return obs

    @staticmethod
    def _from_raw(raw: Mapping[str, Any], source: str, at: float) -> Observation:
        ssh = raw.get("ssh") if isinstance(raw.get("ssh"), dict) else {}
        claimed = ssh.get("claimed") if isinstance(ssh.get("claimed"), bool) else None
        key = ssh.get("host_key_sha256") if isinstance(ssh.get("host_key_sha256"), str) else ""
        err = "" if ssh else "the board's identify has no ssh block (bare metal, or an older image)"
        return Observation(claimed, key, source, at, err)

    def _remember(self, obs: Observation) -> None:
        if obs.claimed is None:
            return
        seen = self._records.get(self.board_id).get("observed") or {}
        if not obs.source.endswith("(last check)") and isinstance(seen, dict) and \
                (seen.get("claimed"), seen.get("host_key"), seen.get("source")) == \
                (obs.claimed, obs.host_key, obs.source) and obs.at - float(seen.get("at") or 0) < 600:
            return                           # nothing new: no write on every LAN info
        with contextlib.suppress(OSError):
            self._records.update(self.board_id, observed={
                "claimed": obs.claimed, "host_key": obs.host_key, "source": obs.source,
                "at": obs.at})

    # -- the state --------------------------------------------------------------------------

    def claim_status(self, identity: Any = None, *, refresh: bool = False,
                     hub_ok: bool = False) -> dict[str, Any] | None:
        """The board model's ``claim``; None on bare metal (no SSH, nothing to claim)."""
        if self._impl(identity) != IMPL_LINUX:
            return None
        return self.compose(self.observe(refresh=refresh, hub_ok=hub_ok))

    def compose(self, obs: Observation) -> dict[str, Any]:
        try:
            cfg = self.config()
            config_error = ""
        except UsageError as exc:
            cfg, config_error = {k: "" for k in SSH_KEYS}, exc.message
        pin = cfg["host_key"]
        pinned = pin_fingerprint(pin) if pin else ""
        rec = self._records.get(self.board_id)
        reported = obs.host_key
        match = (pinned == reported) if (pinned and reported) else None
        mine_on_record = bool(pinned) and rec.get("host_key_fp") == pinned
        notes: list[str] = []
        if obs.claimed is None:
            state = STATE_MINE if mine_on_record else STATE_UNKNOWN
            notes.append(f"not checked now: {obs.error}" if obs.error else "not checked now")
        elif obs.claimed is False:
            state = STATE_UNCLAIMED
            if pinned:
                notes.append(
                    f"this Harness Manager pinned {pinned} before; the board is unclaimed now "
                    "(mps3-unclaim, a new card, or a claim that lived on tmpfs with no card)")
        elif mine_on_record and match is not False:
            state = STATE_MINE
        else:
            state = STATE_OTHER
        if match is False:
            notes.append(f"HOST KEY CHANGED: pinned {pinned}, the board reports {reported}. "
                         "SSH to this board is refused until you re-claim it (only if it was "
                         "re-provisioned: `harness-manager board claim TARGET "
                         "--replace-host-key`)")
        if state == STATE_OTHER and match is not False:
            notes.append("claimed by a key this Harness Manager did not claim with; the board "
                         "does not publish which. If it is yours (pyverify claim, another "
                         "machine): `harness-manager board claim TARGET --adopt`")
        if config_error:
            notes.append(config_error)
        claimed: dict[str, Any] | None = None
        if state == STATE_MINE:
            claimed = {"by": rec.get("by") or "you", "key_fp": rec.get("key_fp") or None,
                       "at": rec.get("at") or None, "mine": True}
        elif state == STATE_OTHER:
            claimed = {"by": "another key", "key_fp": None, "at": None, "mine": False}
        hub, _ = self.route()
        return {
            "state": state,
            "claimed": claimed,
            "host_key": {"reported": reported or None, "pinned": pinned or None, "match": match},
            "route": f"hub {hub}" if hub else "lan",
            "user": cfg["user"] or DEFAULT_USER,
            "source": obs.source,
            "checked_at": _iso(obs.at),
            "live": obs.claimed is not None and "(last check)" not in obs.source,
            "notes": notes,
        }

    # -- the claim --------------------------------------------------------------------------

    def _claim_key_path(self, key: str | None) -> Path:
        if key:
            return Path(os.path.expanduser(key))
        private = self.config()["key"]
        if private:
            return Path(os.path.expanduser(private) + ".pub")
        return Path(os.path.expanduser(DEFAULT_CLAIM_KEY))

    def claimable(self) -> str:
        """"" when this board has an SSH to claim; else why not (bare metal)."""
        impl = self._impl()
        if impl == IMPL_LINUX:
            return ""
        return (f"the {impl or 'bare-metal'} harness has no SSH to claim (claims are for the "
                "Linux harness)")

    def claim(self, *, key: str | None = None, adopt: bool = False,
              replace_host_key: bool = False,
              progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """The TOFU claim (or ``adopt``: pin a board already claimed with your key). The caller
        (``services.claim.ClaimService``) has checked the lease and the confirmation."""
        say = progress or (lambda _t: None)
        identity = None
        with contextlib.suppress(HarnessError):
            identity = self._session.identity()
        impl = self._impl(identity)
        if impl != IMPL_LINUX:
            raise UnavailableError(CAPABILITY, f"the {impl or 'bare-metal'} harness has no SSH "
                                               "to claim (claims are for the Linux harness)")
        say("identify: is the board claimed, and what is its host key?")
        obs = self.observe(refresh=True, hub_ok=True)
        if obs.claimed is None:
            raise UnreachableError(f"cannot read the board's claim state: {obs.error or 'no answer'}",
                                   hint="identify (UDP 6899) must answer: from the hub, or on "
                                        "the board's LAN")
        if not obs.host_key:
            raise RefusedError("the board publishes no SSH host key fingerprint, so its key "
                               "cannot be checked before it is pinned; nothing was claimed",
                               hint="the image's identify must carry ssh.host_key_sha256")
        cfg = self.config()
        pinned = pin_fingerprint(cfg["host_key"]) if cfg["host_key"] else ""
        if pinned and pinned != obs.host_key and not replace_host_key:
            raise HostKeyChangedError(
                f"THE BOARD'S SSH HOST KEY CHANGED: pinned {pinned}, the board now reports "
                f"{obs.host_key}. Nothing was claimed or pinned",
                hint="if the board was re-provisioned (a new card or image), claim it again "
                     "with --replace-host-key; otherwise something else answers for it")
        rec = self._records.get(self.board_id)
        key_path = self._claim_key_path(key)
        if obs.claimed and not adopt:
            mine = bool(pinned) and pinned == obs.host_key and rec.get("host_key_fp") == pinned
            raise AlreadyError(
                f"{self.board_id} is already claimed "
                f"{'by you (' + str(rec.get('key_fp') or rec.get('by')) + ')' if mine else 'by another key'}; "
                "a claim is never repeated or taken over",
                hint="nothing to do" if mine else
                "if the claim is your key (pyverify claim, another machine), pin it with "
                "--adopt; if the board was re-provisioned, unclaim it on the serial console "
                "(mps3-unclaim) first")
        if not obs.claimed and adopt:
            raise UsageError(f"{self.board_id} is unclaimed: there is no claim to adopt",
                             hint="claim it: `harness-manager board claim TARGET`")
        key_fp = ""
        if not obs.claimed:
            data, lines = read_claim_key(key_path)
            key_fp = fingerprint(parse_key_line(lines[0])[1])
            say(f"claim: TFTP {TOFU_FILENAME} ({len(lines)} key(s), {key_fp}) -> "
                f"{self.board_host()}{' via ' + self.route()[0] if self.route()[0] else ''}")
            self._tofu_put(data)
        elif key_path.is_file():
            with contextlib.suppress(UsageError):
                key_fp = fingerprint(parse_key_line(read_claim_key(key_path)[1][0])[1])
        say("host key: connecting over SSH to capture it")
        line = self._capture_host_key(obs.host_key, fresh=not obs.claimed,
                                      identity_file=self._identity_for(key, key_path))
        changes: dict[str, str | None] = {"host_key": line}
        private = self._private_for(key_path)
        if key and private:
            changes["key"] = str(private)
        table = write_ssh_settings(self.candidate, changes)
        self._write_known_hosts(line)
        self._records.update(self.board_id, by=_me(), key_fp=key_fp or None, at=_iso(time.time()),
                             host_key_fp=obs.host_key, how="adopt" if adopt else "claim")
        say(f"pinned {obs.host_key} in boards.toml boards.{table}.ssh.host_key")
        now = Observation(True, obs.host_key, obs.source, time.time())
        with self._mu:
            if self._lan is not None:
                self._lan = now              # the board is claimed now: no stale "unclaimed"
        self._remember(now)
        status = self.compose(now)
        status.update(action="adopted" if adopt else "claimed", table=table)
        return status

    def _tofu_put(self, data: bytes) -> None:
        hub, jump = self.route()
        host, port = self.board_host(), self._tftp_port()
        if not hub:
            from pyverify.pusher import PushError, tftp_put

            try:
                tftp_put(data, host, port, filename=TOFU_FILENAME, timeout_s=3.0)
            except PushError as exc:
                raise self._tofu_error(str(exc)) from exc
            except OSError as exc:
                raise UnreachableError(f"the claim's TFTP put to {host}:{port} failed: {exc}") from exc
            return
        out = _hub_call(hub, jump, hub_argv("claim", host, port, 3.0,
                                            base64.b64encode(data).decode("ascii")), HUB_TIMEOUT_S)
        if not out.get("ok"):
            raise self._tofu_error(str(out.get("err") or "no reason given"))

    def _tofu_error(self, text: str) -> HarnessError:
        low = text.lower()
        if "error 2" in low or "access violation" in low or "already claimed" in low:
            return AlreadyError(f"the board refused the claim (TFTP error 2): it was claimed "
                                f"meanwhile, by someone else ({text})",
                                hint="never retried: a claim is never taken over")
        if "error 3" in low:
            return UsageError(f"the board refused the key file as too large ({text})")
        return UnreachableError(f"the claim's TFTP put failed: {text}",
                                hint="nothing was claimed unless `board claim-status` says so")

    @staticmethod
    def _private_for(pub: Path) -> Path | None:
        if pub.suffix == ".pub":
            private = pub.with_suffix("")
            if private.is_file():
                return private
        return None

    def _identity_for(self, key: str | None, key_path: Path) -> str:
        if key:
            private = self._private_for(key_path)
            return str(private) if private else ""
        return self.config()["key"]

    # -- SSH --------------------------------------------------------------------------------

    def _base_options(self, identity_file: str = "") -> list[str]:
        opts = list(KEY_ONLY)
        ident = identity_file or self.config()["key"]
        if ident:
            opts += ["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(ident)]
        return opts

    def _write_known_hosts(self, line: str) -> Path:
        ktype, blob = parse_key_line(line)
        path = known_hosts_path(self.board_id)
        _atomic_write(path, f"{host_key_alias(self.board_id)} {ktype} {blob}\n")
        return path

    def pinned_options(self) -> list[str]:
        """The options that make ssh trust exactly the pinned key (and our key only)."""
        pin = self.config()["host_key"]
        if not pin:
            raise UsageError(f"{self.board_id}'s SSH host key is not pinned",
                             hint="claim the board (`harness-manager board claim TARGET`), or "
                                  "pin a claim you made elsewhere with --adopt")
        if pin.startswith("SHA256:"):
            raise UsageError(f"{self.board_id}'s pin is a fingerprint only ({pin}); ssh needs the key",
                             hint="`harness-manager board claim TARGET --adopt` fetches the key "
                                  "and checks it against that fingerprint")
        kh = self._write_known_hosts(pin)
        return [*self._base_options(),
                "-o", f"HostKeyAlias={host_key_alias(self.board_id)}",
                "-o", f"UserKnownHostsFile={kh}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "StrictHostKeyChecking=yes", "-o", "CheckHostIP=no",
                "-o", "UpdateHostKeys=no"]

    def check_host_key(self) -> None:
        """Refuse loudly when identify (as last seen) shows a key other than the pinned one."""
        pin = self.config()["host_key"]
        if not pin:
            return
        pinned = pin_fingerprint(pin)
        obs = self.observe()
        if obs.host_key and obs.host_key != pinned:
            raise HostKeyChangedError(
                f"THE BOARD'S SSH HOST KEY CHANGED: pinned {pinned}, the board reports "
                f"{obs.host_key}. Refusing to connect",
                hint="if the board was re-provisioned (a new card or image), re-claim it: "
                     "`harness-manager board claim TARGET --replace-host-key`")

    def ssh_argv(self, command: Sequence[str] = (), *, tty: bool = False) -> list[str]:
        """``ssh [-J HUB] -l USER BOARD [CMD]`` with the pinned key; nothing is run."""
        self.check_host_key()
        host = self.board_host()
        if not host:
            raise UsageError(f"{self.board_id} has no board address for SSH")
        argv = ["ssh", "-o", "ControlPath=none", "-o", "ControlMaster=no",
                "-o", "ConnectTimeout=15", *self.pinned_options()]
        if tty:
            argv.append("-t")
        jump = self.proxy_jump()
        if jump:
            argv += ["-J", jump]
        argv += ["-l", self.user(), host]
        return argv + list(command)

    def _capture_host_key(self, expected_fp: str, *, fresh: bool, identity_file: str) -> str:
        """Connect once with ``accept-new`` into a scratch known_hosts and return the key line
        ssh saw, only when its fingerprint is ``expected_fp`` (identify's)."""
        host = self.board_host()
        alias = host_key_alias(self.board_id)
        with tempfile.TemporaryDirectory(prefix="hm-hostkey-", dir=ssh_dir()) as scratch:
            kh = Path(scratch) / "known_hosts"
            argv = ["ssh", *ONE_SHOT_OPTIONS, *self._base_options(identity_file),
                    "-o", f"HostKeyAlias={alias}", "-o", f"UserKnownHostsFile={kh}",
                    "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "StrictHostKeyChecking=accept-new", "-o", "CheckHostIP=no",
                    "-o", "UpdateHostKeys=no"]
            jump = self.proxy_jump()
            if jump:
                argv += ["-J", jump]
            argv += ["-l", self.user(), host, "true"]
            waits = list(KEYS_SYNC_RETRIES_S) if fresh else []
            while True:
                res = DEFAULT_RUN(argv, SSH_TIMEOUT_S)
                if res.returncode == 0 or "permission denied" not in res.stderr.lower() or not waits:
                    break
                time.sleep(waits.pop(0))       # mps3-keys-sync has not run yet
            seen = self._key_lines(kh)
            got = [(line, fingerprint(parse_key_line(line)[1])) for line in seen]
            for _line, fp in got:
                if fp != expected_fp:
                    raise HostKeyChangedError(
                        f"the SSH host key the board offered ({fp}) is not the one its identify "
                        f"published ({expected_fp}): NOT pinned. Something between you and the "
                        "board may be answering for it",
                        hint="check from the hub: `ssh-keyscan 192.168.10.101`, and the key on "
                             "the board's CLCD or serial console")
            if res.returncode != 0:
                why = _stderr_line(res.stderr) or f"ssh exited with status {res.returncode}"
                if "permission denied" in res.stderr.lower():
                    raise RefusedError(
                        f"the board's SSH refused your key ({why})"
                        + (": the claim was accepted, but the key does not log in" if fresh else
                           ": the board is claimed by another key"),
                        hint="check the key (boards.<b>.ssh.key, --key) is the one that claimed it")
                raise UnreachableError(f"SSH to {host} failed: {why}",
                                       hint="the claim state is unchanged unless it said "
                                            "'accepted'; `board claim-status` reads it again")
            if not got:
                raise ActionFailedError("ssh logged in but recorded no host key; nothing pinned")
            return got[0][0]

    @staticmethod
    def _key_lines(kh: Path) -> list[str]:
        try:
            text = kh.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        for raw in text.splitlines():
            parts = raw.split()
            if len(parts) >= 3 and not raw.startswith("#"):
                with contextlib.suppress(UsageError):
                    parse_key_line(" ".join(parts[1:3]))
                    out.append(" ".join(parts[1:3]))
        return out

    def open_forward(self, forwards: Mapping[str, int], *, label: str = "",
                     restart: bool = True) -> Any:
        """A started ``tunnel.SshTunnel`` to the board's LOOPBACK ports (``{name: board port}``),
        through the hub, with the pinned key. XVC after its lock, the slot verbs (6900/6910
        from the board itself, S12) and the on-board GDB server's ports use it."""
        from . import tunnel as _tunnel

        self.check_host_key()
        host = self.board_host()
        fws = [_tunnel.Forward(name, "127.0.0.1", port) for name, port in forwards.items()]
        tunnel = _tunnel.SshTunnel(host, fws, jump=self.proxy_jump(), user=self.user(),
                                   options=self.pinned_options(), restart=restart,
                                   label=label or f"{self.board_id} board-ssh {','.join(forwards)}")
        try:
            tunnel.start()
        except UnreachableError as exc:
            raise self.map_ssh_failure(exc) from exc
        return tunnel

    def map_ssh_failure(self, exc: UnreachableError) -> HarnessError:
        """ssh's own host-key refusal, as the loud error; anything else unchanged."""
        if _host_key_failed(exc.message):
            pinned = pin_fingerprint(self.config()["host_key"])
            return HostKeyChangedError(
                f"THE BOARD'S SSH HOST KEY IS NOT THE PINNED ONE ({pinned}): ssh refused to "
                f"connect ({exc.message})",
                hint="if the board was re-provisioned, re-claim it: `harness-manager board "
                     "claim TARGET --replace-host-key`")
        return exc

    # -- the lock's refusal -----------------------------------------------------------------

    def refusal_error(self, err: str, what: str = "this operation") -> HarnessError | None:
        return refusal_error(err, what, status=self._status_quiet())

    def _status_quiet(self) -> dict[str, Any] | None:
        try:
            return self.compose(self.observe())
        except HarnessError:
            return None


def refusal_error(err: str, what: str = "this operation", *,
                  status: Mapping[str, Any] | None = None) -> HarnessError | None:
    """Map a harness refusal line to the error Harness Manager shows; None if it is not one.

    - ``slot locked: board claimed (use ssh)`` (S12, the claim lock) -> ``ClaimLockedError``;
    - ``identity lock: <reason>`` (the fabric identity lock) -> ``IncompatibleError``.
    """
    text = (err or "").strip()
    low = text.lower()
    if low.startswith("slot locked") or "board claimed" in low:
        state = (status or {}).get("state")
        claimed = (status or {}).get("claimed") or {}
        fp = claimed.get("key_fp") if claimed.get("mine") else None
        if claimed.get("mine"):
            who = fp or "your key"
        elif state == "other":
            who = "another key (the board does not publish which)"
        else:
            who = "an SSH key (the board does not publish which; `board claim-status` reads it)"
        hint = ("Harness Manager sends it from the board itself over SSH when the host key is "
                "pinned; run it again, or check `harness-manager board claim-status TARGET`"
                if claimed.get("mine") else
                "only the claiming key's owner can do this now, over SSH. If the board was "
                "re-provisioned, unclaim it on the serial console (mps3-unclaim), then "
                "`harness-manager board claim TARGET`")
        exc = ClaimLockedError(
            f"this board is claimed by {who}; {what} needs the claiming key (use `board claim` "
            f"only if the board was re-provisioned) [harness: {text}]", hint=hint)
        return exc
    if low.startswith(IDENTITY_LOCK_PREFIX):
        reason = text[len(IDENTITY_LOCK_PREFIX):].strip()
        return IncompatibleError(
            f"the harness refused {what}: its fabric identity is locked ({reason}); the card's "
            "image and the FPGA's static disagree",
            hint="nothing was changed; `info` shows the skew. Put the image that belongs to "
                 "this static on the card, or load the static the image was built for")
    return None


def make_claim_adapter(session: Any) -> Mps3Claim | None:
    """The pack hook: every session with an Ethernet shell (bare metal answers None status)."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3Claim(session)
