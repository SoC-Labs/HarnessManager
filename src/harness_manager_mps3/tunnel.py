"""Reach a board through an SSH tunnel on the lab hub (lane L1).

srv03335 cannot route to the lab MPS3's shell at 192.168.10.101: only the hub
(mapstone-dev, NIC ``mps3_01_pl`` = 192.168.10.1/24) can. So the app runs ONE
``ssh -N`` to the hub that forwards each of the shell's TCP ports to a free
local port, and the session talks to ``127.0.0.1:<local>`` instead:

| Name | Board port | Used by |
|---|---|---|
| control | 6900 | pyverify ``ShellClient`` (identity, health, swap, reset, clocks) |
| push | 6910 | the windowed/raw TCP bitstream push (deploy) |
| rbb | 6921 | OpenOCD ``remote_bitbang`` (debug) |
| uart0, uart1, swo | 6930, 6931, 6932 | the DUT consoles |
| xvc | 2542 | Xilinx Virtual Cable (nothing uses it yet; forwarded so it can) |

UDP does not cross an SSH port forward: TFTP (69) and identify (6899) are
unreachable through the tunnel. Deploy therefore always pushes over 6910 TCP
(``deploy.is_tunnelled`` reads the Ethernet link's ``via="ssh"``), and network
discovery only works on the hub's own LAN.

How a candidate asks for it. ``via = "ssh:HOST"`` (boards.toml, ``--via``, or the
API's ``via`` field) marks the candidate's Ethernet link ``via="ssh"`` and puts
``via ssh:HOST`` in its ``detail``, so the candidate alone says how to reach the
board, across processes (the CLI builds it, the daemon opens it). The address
stays the board's own (``192.168.10.101:6900``): it is what boards.toml ``match``
and the board id use, and it is what the HUB connects to.

The ssh command. ``ssh -N -T`` with ``ControlPath=none`` (never join the user's
multiplexed master), ``BatchMode=yes`` (a missing key fails at once instead of
prompting), ``ExitOnForwardFailure=yes`` (a local port taken between choosing it
and binding it fails the start, which is retried, instead of leaving a tunnel
with a hole) and ``ServerAliveInterval=15`` (a dead hub is noticed in ~45 s and
the tunnel restarts). Every forward binds 127.0.0.1 only, never port 2542
(another session on srv03335 owns it).

The user's ``LocalForward`` lines. ``~/.ssh/config`` gives mapstone-dev two
(18081, 18082), which its ControlMaster already holds. ``ExitOnForwardFailure``
covers them too, so a plain ``ssh -L … mapstone-dev`` would exit at once; and
``ClearAllForwardings=yes`` cannot help because it also clears the command
line's ``-L`` (ssh_config(5)). So the tunnel asks ``ssh -G HOST`` (it only
evaluates the config; it never connects) whether the config gives HOST any
forwards. When it does, the tunnel runs ``ssh -F <copy>``, where the copy is the
user's config with the forward lines commented out and the system config
included at the end, and checks the copy with ``ssh -G`` before using it.

Restart and close. A supervisor thread restarts ssh (1, 2, 5, 10, 30 s back-off)
on the SAME local ports, so console and debug endpoints stay valid across a
drop. ``close()`` stops it for good; the pack calls it when the board closes.

Test seams: ``launcher`` (``argv -> process``) and ``ssh_g`` (``argv -> text``);
``DEFAULT_LAUNCHER``/``DEFAULT_SSH_G`` are what the pack uses, so tests swap
them for ``tests.fakes.l1_fake_ssh``.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from harness_manager.core.errors import UnreachableError, UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.transports import tcp_serial as _tcp_serial  # noqa: F401 - registers tcp://

from .constants import CONSOLE_PORTS, CONTROL_PORT, JTAG_RBB_PORT, PUSH_PORT, XVC_PORT

log = logging.getLogger(__name__)

VIA_SSH = "ssh"
#: The marker in a link's ``detail`` that names the hub (``via ssh:HOST``).
_VIA_RE = re.compile(r"\bvia (ssh:[A-Za-z0-9._@\-\[\]:]+)")

#: Local ports never to bind: 2542 belongs to another session on srv03335 (XVC).
AVOID_LOCAL_PORTS = frozenset({2542})

SSH_OPTIONS: tuple[str, ...] = (
    "-o", "ControlPath=none",
    "-o", "ControlMaster=no",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=15",
    "-o", "PermitLocalCommand=no",
    "-o", "RequestTTY=no",
)

READY_TIMEOUT_S = 30.0
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
_POLL_S = 0.25
_STDERR_KEEP = 4096

#: The ports a session needs through the tunnel, by name (constants.py for the numbers).
DEFAULT_REMOTE_PORTS: dict[str, int] = {
    "control": CONTROL_PORT, "push": PUSH_PORT, "rbb": JTAG_RBB_PORT,
    **CONSOLE_PORTS, "xvc": XVC_PORT,
}


# --- via: how a candidate names its route ------------------------------------------------


def parse_via(via: str) -> str:
    """``"ssh:HOST"`` -> ``HOST``; ``""`` -> ``""``. Anything else is a ``UsageError``."""
    via = (via or "").strip()
    if not via:
        return ""
    kind, sep, host = via.partition(":")
    if kind != VIA_SSH or not sep or not host or any(c.isspace() for c in host):
        raise UsageError(f"via {via!r} is not ssh:HOST",
                         hint="e.g. --via ssh:mapstone-dev.ecs.soton.ac.uk (the lab hub)")
    return host


def via_host(link: Link) -> str:
    """The SSH host a link is reached through, or ``""`` for a direct link."""
    if link.via != VIA_SSH:
        return ""
    m = _VIA_RE.search(link.detail or "")
    return m.group(1).split(":", 1)[1] if m else ""


def candidate_via(candidate: Candidate) -> str:
    """``"ssh:HOST"`` when the candidate's Ethernet link goes through a hub, else ``""``."""
    for lk in candidate.links:
        if lk.kind == LinkKind.ETHERNET:
            host = via_host(lk)
            if host:
                return f"{VIA_SSH}:{host}"
    return ""


def with_via(candidate: Candidate, via: str) -> Candidate:
    """The candidate with every Ethernet link routed ``via`` (``"ssh:HOST"``); ``""`` is a no-op.

    Board-agnostic: it only rewrites links. The pack's ``open`` does the tunnelling.
    """
    host = parse_via(via)
    if not host:
        return candidate
    links = []
    for lk in candidate.links:
        if lk.kind == LinkKind.ETHERNET:
            base = _VIA_RE.sub("", lk.detail or "").rstrip(" ,") or "shell control channel"
            lk = Link(lk.kind, lk.address, f"{base}, via ssh:{host}", via=VIA_SSH)
        links.append(lk)
    evidence = candidate.evidence
    if f"ssh:{host}" not in evidence:
        evidence = f"{evidence} (through an SSH tunnel on {host})" if evidence else \
            f"through an SSH tunnel on {host}"
    return replace(candidate, links=tuple(links), evidence=evidence)


# --- local ports -------------------------------------------------------------------------


def free_local_port(exclude: Iterable[int] = ()) -> int:
    """A free 127.0.0.1 port the kernel picks, never one in ``AVOID_LOCAL_PORTS``/``exclude``."""
    avoid = set(AVOID_LOCAL_PORTS) | set(exclude)
    for _ in range(64):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port
    raise UnreachableError("no free local port for the SSH tunnel")


def _proc_listening(port: int) -> bool | None:
    """True/False from /proc/net/tcp{,6} (state 0A = LISTEN); None when there is no /proc."""
    found_table = False
    want = f":{port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as fh:
                lines = fh.readlines()[1:]
        except OSError:
            continue
        found_table = True
        for line in lines:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A" and parts[1].endswith(want):
                return True
    return False if found_table else None


def listening(port: int) -> bool:
    """Is something listening on 127.0.0.1:``port``? Never opens a connection through it.

    A connect would open an SSH channel to the board's single-client port, so
    Linux reads /proc; elsewhere a bind that fails with EADDRINUSE means taken.
    """
    seen = _proc_listening(port)
    if seen is not None:
        return seen
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


# --- the user's ssh config -------------------------------------------------------------------

_FORWARD_KEYS = ("localforward", "remoteforward", "dynamicforward", "clearallforwardings")
_FORWARD_LINE = re.compile(r"^\s*(LocalForward|RemoteForward|DynamicForward|ClearAllForwardings)\b",
                           re.IGNORECASE)


def run_ssh_g(argv: Sequence[str]) -> str:
    """``ssh … -G HOST``: print the effective config. It evaluates the config and never connects."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnreachableError(f"cannot run {argv[0]} -G: {exc}",
                               hint="is OpenSSH installed and on PATH?") from exc
    if proc.returncode != 0:
        raise UsageError(f"`{' '.join(argv)}` failed: {(proc.stderr or proc.stdout).strip()}",
                         hint="check ~/.ssh/config")
    return proc.stdout


def config_forwards(ssh_g_output: str) -> list[str]:
    """The forward lines (and a ClearAllForwardings yes) in ``ssh -G`` output."""
    out = []
    for line in ssh_g_output.splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        if key in ("localforward", "remoteforward", "dynamicforward"):
            out.append(line.strip())
        elif key == "clearallforwardings" and value.strip().lower() == "yes":
            out.append(line.strip())
    return out


def filtered_config_text(text: str, *, system_config: Path | None) -> str:
    """The user's config with forward lines commented out, the system config included last."""
    lines = ["# Written by Harness Manager for its SSH tunnel: your ~/.ssh/config with the",
             "# port-forward lines commented out (they would collide with your own sessions).",
             "# Regenerated on every tunnel start; edit ~/.ssh/config, not this file."]
    for line in text.splitlines():
        lines.append(f"# [harness-manager: forward removed] {line}" if _FORWARD_LINE.match(line)
                     else line)
    if system_config is not None and system_config.is_file():
        # -F skips the system-wide config; include it LAST so the user's settings win
        # (ssh takes the first value it reads for most options).
        lines += ["", "Host *", f"    Include {system_config}"]
    return "\n".join(lines) + "\n"


def tunnel_config_dir() -> Path:
    from harness_manager.engine import resolve_state_dir  # the one state-dir rule

    return resolve_state_dir() / "tunnel"


def ssh_base_argv(host: str, *, ssh: str = "ssh",
                  ssh_g: Callable[[Sequence[str]], str] | None = None,
                  user_config: Path | None = None,
                  system_config: Path | None = Path("/etc/ssh/ssh_config"),
                  config_dir: Path | None = None) -> list[str]:
    """``[ssh]`` or ``[ssh, "-F", <filtered copy>]`` (module docstring, "The user's LocalForward lines")."""
    ssh_g = ssh_g or DEFAULT_SSH_G
    forwards = config_forwards(ssh_g([ssh, "-G", host]))
    if not forwards:
        return [ssh]
    user_config = user_config or Path.home() / ".ssh" / "config"
    try:
        text = user_config.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"your ssh config gives {host} port forwards ({'; '.join(forwards)}) "
                         f"and {user_config} cannot be read to leave them out: {exc}") from exc
    body = filtered_config_text(text, system_config=system_config)
    cdir = config_dir or tunnel_config_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(cdir, 0o700)
    path = cdir / f"ssh_config.{hashlib.sha256(host.encode()).hexdigest()[:12]}"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, path)
    left = config_forwards(ssh_g([ssh, "-F", str(path), "-G", host]))
    if left:
        raise UsageError(
            f"your ssh config gives {host} port forwards the tunnel cannot leave out "
            f"({'; '.join(left)}); they come from an Include'd file or the system config",
            hint=f"move them into a Host block that does not match {host}, or into "
                 f"~/.ssh/config itself (the tunnel comments them out there)")
    return [ssh, "-F", str(path)]


# --- processes ------------------------------------------------------------------------------


class TunnelProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Launcher = Callable[[Sequence[str]], TunnelProcess]


class _PopenProcess:
    """``subprocess.Popen`` plus a thread that keeps the last few KB of ssh's stderr."""

    def __init__(self, argv: Sequence[str]) -> None:
        self._proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.pid = self._proc.pid
        self.stderr_tail = ""
        #: ``(monotonic time, line)`` for each "channel N: open failed: ..." ssh logged.
        self.open_failures: deque[tuple[float, str]] = deque(maxlen=32)
        self._t = threading.Thread(target=self._drain, name=f"ssh-stderr-{self.pid}", daemon=True)
        self._t.start()

    def _drain(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace")
            self.stderr_tail = (self.stderr_tail + line)[-_STDERR_KEEP:]
            if "open failed" in line:
                self.open_failures.append((time.monotonic(), line.strip()))

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout)


def popen_launcher(argv: Sequence[str]) -> TunnelProcess:
    if shutil.which(argv[0]) is None and not Path(argv[0]).is_file():
        raise UnreachableError(f"{argv[0]} is not installed or not on PATH",
                               hint="install the OpenSSH client")
    return _PopenProcess(argv)


#: What the pack uses. Tests replace these (tests/fakes/l1_fake_ssh.py).
DEFAULT_LAUNCHER: Launcher = popen_launcher
DEFAULT_SSH_G: Callable[[Sequence[str]], str] = run_ssh_g


def _stderr_of(proc: Any) -> str:
    return (getattr(proc, "stderr_tail", "") or "").strip()


def _explain(stderr: str) -> str:
    """The one line of ssh's stderr worth showing (auth, DNS, a refused forward)."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for key in ("Permission denied", "Could not resolve", "Connection refused", "timed out",
                "Address already in use", "cannot listen", "Host key verification failed",
                "open failed"):
        for ln in reversed(lines):
            if key.lower() in ln.lower():
                return ln
    return lines[-1] if lines else ""


# --- the tunnel ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Forward:
    """One ``-L 127.0.0.1:local:remote_host:remote_port``; ``remote_host`` is resolved BY THE HUB."""

    name: str
    remote_host: str
    remote_port: int
    local_port: int = 0

    def spec(self) -> str:
        rhost = f"[{self.remote_host}]" if ":" in self.remote_host else self.remote_host
        return f"127.0.0.1:{self.local_port}:{rhost}:{self.remote_port}"


class SshTunnel:
    """One supervised ``ssh -N`` carrying a set of forwards to one host."""

    def __init__(self, host: str, forwards: Sequence[Forward], *,
                 launcher: Launcher | None = None, ssh: str = "ssh",
                 ssh_g: Callable[[Sequence[str]], str] | None = None,
                 ready_timeout_s: float = READY_TIMEOUT_S, restart: bool = True,
                 backoff_s: Sequence[float] = BACKOFF_S,
                 on_state: Callable[[dict[str, Any]], None] | None = None,
                 label: str = "", user_config: Path | None = None,
                 system_config: Path | None = Path("/etc/ssh/ssh_config")) -> None:
        if not host:
            raise UsageError("an SSH tunnel needs a host")
        if not forwards:
            raise UsageError("an SSH tunnel needs at least one forward")
        self.host = host
        self.label = label or f"ssh:{host}"
        self._launcher = launcher or DEFAULT_LAUNCHER
        self._ssh = ssh
        self._ssh_g = ssh_g or DEFAULT_SSH_G
        self._user_config = user_config            # None: ~/.ssh/config
        self._system_config = system_config
        self.ready_timeout_s = ready_timeout_s
        self._restart = restart
        self._backoff = tuple(backoff_s) or (1.0,)
        self._watchers: list[Callable[[dict[str, Any]], None]] = [on_state] if on_state else []
        taken: set[int] = set()
        fixed: list[Forward] = []
        for fw in forwards:
            port = fw.local_port or free_local_port(taken)
            if port in AVOID_LOCAL_PORTS:
                raise UsageError(f"local port {port} is reserved (another session uses it)")
            taken.add(port)
            fixed.append(replace(fw, local_port=port))
        self.forwards: tuple[Forward, ...] = tuple(fixed)
        self.state = "down"
        self.detail = ""
        self.restarts = 0
        self.argv: list[str] = []
        self._proc: TunnelProcess | None = None
        self._mu = threading.RLock()
        self._closing = threading.Event()
        self._sup: threading.Thread | None = None

    # -- views ------------------------------------------------------------------------------

    def local_port(self, name: str) -> int:
        for fw in self.forwards:
            if fw.name == name:
                return fw.local_port
        raise KeyError(name)

    def ports(self) -> dict[int, int]:
        """``{remote port: local port}``, the API's ``tunnel.ports``."""
        return {fw.remote_port: fw.local_port for fw in self.forwards}

    def status(self) -> dict[str, Any]:
        with self._mu:
            return {"via": f"{VIA_SSH}:{self.host}", "host": self.host, "state": self.state,
                    "ports": {str(k): v for k, v in self.ports().items()},
                    "forwards": {fw.name: {"remote": f"{fw.remote_host}:{fw.remote_port}",
                                           "local": fw.local_port} for fw in self.forwards},
                    "detail": self.detail, "restarts": self.restarts,
                    "pid": getattr(self._proc, "pid", None)}

    def watch(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Call ``callback(status())`` on every state change (the daemon publishes events)."""
        self._watchers.append(callback)

    def _set(self, state: str, detail: str) -> None:
        with self._mu:
            changed = (state, detail) != (self.state, self.detail)
            self.state, self.detail = state, detail
        if changed:
            log.info("tunnel %s: %s %s", self.label, state, detail)
            snap = self.status()
            for cb in list(self._watchers):
                try:
                    cb(snap)
                except Exception:  # noqa: BLE001 - a watcher must never stop the tunnel
                    log.exception("tunnel state watcher failed")

    # -- lifecycle --------------------------------------------------------------------------

    def build_argv(self) -> list[str]:
        """The ssh command line (it reads the user's config with ``ssh -G``; never connects)."""
        base = ssh_base_argv(self.host, ssh=self._ssh, ssh_g=self._ssh_g,
                             user_config=self._user_config, system_config=self._system_config)
        argv = [*base, *SSH_OPTIONS, "-N", "-T"]
        for fw in self.forwards:
            argv += ["-L", fw.spec()]
        argv.append(self.host)
        return argv

    def start(self) -> SshTunnel:
        """Start ssh and wait until every local forward listens. Raises ``UnreachableError``."""
        self.argv = self.build_argv()
        self._set("starting", f"connecting to {self.host}")
        ok, why = self._launch_and_wait()
        if not ok:
            self._stop_proc()
            self._set("down", why)
            raise UnreachableError(
                f"the SSH tunnel to {self.host} did not come up: {why}",
                hint=f"check `ssh {self.host} true` works without a prompt (BatchMode), "
                     "and that you are on the campus network or VPN")
        self._set("up", f"{len(self.forwards)} ports forwarded through {self.host}")
        if self._restart:
            self._sup = threading.Thread(target=self._supervise, name=f"tunnel-{self.host}",
                                         daemon=True)
            self._sup.start()
        return self

    def _launch_and_wait(self) -> tuple[bool, str]:
        try:
            proc = self._launcher(self.argv)
        except UnreachableError as exc:
            return False, exc.message
        except OSError as exc:
            return False, f"cannot start {self.argv[0]}: {exc}"
        with self._mu:
            self._proc = proc
        deadline = time.monotonic() + self.ready_timeout_s
        while not self._closing.is_set():
            rc = proc.poll()
            if rc is not None:
                why = _explain(_stderr_of(proc)) or "no message"
                return False, f"ssh exited with status {rc} ({why})"
            if all(listening(fw.local_port) for fw in self.forwards):
                return True, ""
            if time.monotonic() >= deadline:
                return False, f"no forward listening after {self.ready_timeout_s:.0f}s " \
                              f"({_explain(_stderr_of(proc)) or 'ssh is still connecting'})"
            self._closing.wait(0.05)
        return False, "closed"

    def _stop_proc(self) -> None:
        with self._mu:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except Exception:  # noqa: BLE001 - TimeoutExpired or a fake's own error
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)

    def _supervise(self) -> None:
        attempt = 0
        last_failure = ""                   # why the last restart failed (its process is gone)
        while not self._closing.is_set():
            with self._mu:
                proc = self._proc
            rc = proc.poll() if proc is not None else None
            if proc is not None and rc is None:
                self._closing.wait(_POLL_S)
                continue
            wait = self._backoff[min(attempt, len(self._backoff) - 1)]
            if proc is not None:
                why = _explain(_stderr_of(proc))
                last_failure = f"ssh exited with status {rc}{f' ({why})' if why else ''}"
            self._set("down", f"{last_failure or 'ssh is not running'}; restarting in {wait:.0f}s")
            if self._closing.wait(wait):
                break
            attempt += 1
            self._stop_proc()
            self._set("starting", f"restart {attempt}: connecting to {self.host}")
            ok, why = self._launch_and_wait()
            if ok:
                with self._mu:
                    self.restarts += 1
                attempt = 0
                self._set("up", f"{len(self.forwards)} ports forwarded through {self.host} "
                                f"(restarted {self.restarts}x)")
            elif not self._closing.is_set():
                self._stop_proc()
                last_failure = why

    def open_failures_since(self, t0: float) -> list[str]:
        """ssh's "channel N: open failed: …" lines logged at or after ``t0`` (monotonic).

        Through ``ssh -L`` a board port that refuses the HUB is still accepted
        locally, then closed: a client sees "accepted, then EOF", which the shell
        codec reads as another client holding the port. These lines tell the two apart.
        """
        with self._mu:
            proc = self._proc
        fails = getattr(proc, "open_failures", ()) or ()
        return [line for at, line in list(fails) if at >= t0]

    def alive(self) -> bool:
        with self._mu:
            proc = self._proc
        return self.state == "up" and proc is not None and proc.poll() is None

    def close(self) -> None:
        """Stop for good (idempotent). The local ports are released with ssh."""
        self._closing.set()
        sup = self._sup
        if sup is not None and sup is not threading.current_thread():
            sup.join(timeout=5.0)
        self._stop_proc()
        # docs/API.md's states are up|down|starting: a closed tunnel is down, and says why.
        self._set("down", "closed with the board")

    def __enter__(self) -> SshTunnel:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- what the pack uses ---------------------------------------------------------------------


@dataclass
class Reach:
    """How an open session reaches its board: local endpoints plus what to close."""

    via: str                                    # "ssh:HOST" or ""
    host: str = "127.0.0.1"                     # where the session connects
    ports: dict[str, int] = field(default_factory=dict)   # name -> local port
    tunnel: SshTunnel | None = None
    remote_host: str = ""
    closers: list[Callable[[], None]] = field(default_factory=list)

    def port(self, name: str, default: int | None = None) -> int | None:
        return self.ports.get(name, default)

    def status(self) -> dict[str, Any] | None:
        return self.tunnel.status() if self.tunnel is not None else None

    def close(self) -> None:
        for fn in reversed(self.closers):
            try:
                fn()
            except Exception:  # noqa: BLE001 - closing must always finish
                log.exception("closing part of the board's route failed")
        self.closers.clear()
        if self.tunnel is not None:
            self.tunnel.close()


def _eth(candidate: Candidate) -> Link | None:
    return next((lk for lk in candidate.links if lk.kind == LinkKind.ETHERNET), None)


def open_reach(candidate: Candidate, remote_ports: Mapping[str, int], *,
               launcher: Launcher | None = None,
               ssh_g: Callable[[Sequence[str]], str] | None = None) -> Reach | None:
    """The pack hook: a started tunnel for a ``via="ssh"`` candidate; None for a direct one.

    ``remote_ports`` are the board ports the session needs, by name (``control``,
    ``push``, ``rbb``, the console names, ``xvc``); the pack passes its own
    configuration, so a test pack's ports are forwarded as they are.
    """
    eth = _eth(candidate)
    if eth is None:
        return None
    hub = via_host(eth)
    if not hub:
        # Direct, or ``via="ssh"`` with no host: a forward someone made by hand (``ssh -L``),
        # whose address is already the local end. Connect to it as it is.
        return None
    from .shell import parse_endpoint

    rhost, rport = parse_endpoint(eth.address, CONTROL_PORT)
    ports = dict(remote_ports)
    ports["control"] = rport
    forwards = [Forward(name, rhost, port) for name, port in ports.items() if port]
    tunnel = SshTunnel(hub, forwards, launcher=launcher, ssh_g=ssh_g,
                       label=f"{candidate.board_id} via ssh:{hub}")
    tunnel.start()
    return Reach(via=f"{VIA_SSH}:{hub}", ports={fw.name: fw.local_port for fw in tunnel.forwards},
                 tunnel=tunnel, remote_host=rhost)


@contextlib.contextmanager
def probe_reach(host_spec: str, via: str, *, timeout_s: float = 2.0,
                launcher: Launcher | None = None,
                ssh_g: Callable[[Sequence[str]], str] | None = None):
    """A short-lived tunnel for a probe: only the control port. Yields ``(host, port)``."""
    from .shell import parse_endpoint

    hub = parse_via(via)
    rhost, rport = parse_endpoint(host_spec, CONTROL_PORT)
    tunnel = SshTunnel(hub, [Forward("control", rhost, rport)], launcher=launcher, ssh_g=ssh_g,
                       restart=False, ready_timeout_s=max(READY_TIMEOUT_S, timeout_s),
                       label=f"probe {rhost}:{rport} via ssh:{hub}")
    tunnel.start()
    try:
        yield "127.0.0.1", tunnel.local_port("control")
    finally:
        tunnel.close()
