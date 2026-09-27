"""Run harness-manager-daemon: ``python -m harness_manager.daemon [--state-dir D] [--port N] [--listen ADDR]``.

Start-up order, so a client never sees a half-started daemon:

1. take the single-instance lock (``HeldError``, exit 4, if a live daemon has it);
2. bind the socket (``PortBoundError``, exit 5, if the port is taken);
3. build the engine and the app;
4. write ``daemon.json`` (mode 0600) with the bound port and a fresh token;
5. serve until SIGINT/SIGTERM or ``POST /api/v1/daemon/shutdown``.

On the way out it closes every board (releasing their locks, stopping OpenOCD
and the consoles), removes ``daemon.json`` and releases the instance lock.

The log goes to stderr, which ``harness-manager daemon start`` points at
``<state_dir>/daemon.log``. WebSocket URLs carry ``?token=``, so every log
line passes a filter that masks it.

Lane OTA-D (app self-update, additive):

- ``--resume FILE`` starts the daemon that follows an app update (or its rollback): the
  port, listen address and TOKEN come from the resume file, so an open app window and every
  client keep working, and once the server answers it opens the same boards and their
  consoles' PTYs at the same paths (``update_apply.resume_after_start``). The file (0600)
  is deleted once read; the token never travels in argv.
- ``--self-test`` imports the app, the engine and the board packs and builds the routes
  without binding anything or writing the state dir; exit 0 and one JSON line when they all
  load. The apply step runs it on the new version before it drains.
- The periodic update checker (``update_checker.py``) starts once the server answers.

Lane SET-WIRE (settings):

- The service is the service for its ``--state-dir``: ``settings.files.use_config_dir`` makes
  every reader of the state dir (``boards.toml``, ``settings.toml``, the tunnel dir, the
  statics, the leases) use it, so a ``--state-dir`` or ``--demo`` service never reads the
  user's own files. It is put back when the service stops.
- ``--port``, ``--listen`` and ``--log-level``, when not given, come from the settings
  ``advanced.port``, ``advanced.listen`` and ``advanced.log_level`` (``restart`` rows: read
  as the service starts). A flag wins, except over the administrator's ``[lock]``, which
  refuses a different value (15).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from harness_manager import __version__
from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    PortBoundError,
    RefusedError,
    UsageError,
)

from . import logfile
from .state import (
    DaemonInstance,
    daemon_log_path,
    default_state_dir,
    is_loopback,
    new_info,
    remove_info,
    write_info,
)

log = logging.getLogger("harness_manager.daemon")

_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")


class RedactToken(logging.Filter):
    """Mask ``token=...`` in a log record (uvicorn logs WebSocket paths with their query)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is not ours to fix
            return True
        if "token=" in message:
            record.msg = _TOKEN_RE.sub(r"\1***", message)
            record.args = ()
        return True


class QuietWebSocketChatter(logging.Filter):
    """Drop uvicorn's bare "connection open"/"connection closed" INFO lines.

    They were 40 % of daemon.log in the Q2 soak (one pair per WebSocket, and the UI
    reconnects), and say nothing the "WebSocket <path> [accepted]" line before them
    does not. Warnings and errors from the same logger still pass.
    """

    _CHATTER = frozenset({"connection open", "connection closed"})

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO or record.name != "uvicorn.error":
            return True
        try:
            return record.getMessage() not in self._CHATTER
        except Exception:  # noqa: BLE001 - a malformed record is not ours to fix
            return True


def install_redaction() -> None:
    flt = RedactToken()
    quiet = QuietWebSocketChatter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "harness_manager"):
        logger = logging.getLogger(name)
        logger.addFilter(flt)
        for handler in logger.handlers:
            handler.addFilter(flt)
            handler.addFilter(quiet)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def bind_socket(listen: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in listen else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # SO_REUSEADDR on Windows lets another process steal a bound port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((listen, port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) or \
                getattr(exc, "winerror", None) == 10048:
            raise PortBoundError(f"port {port} on {listen} is already in use",
                                 hint="pick another --port, or 0 for any free port") from exc
        raise UsageError(f"cannot listen on {listen}:{port}: {exc}",
                         hint="check --listen; the default is 127.0.0.1") from exc
    sock.set_inheritable(False)
    return sock


def uvicorn_config(app: Any, *, log_level: str = "info", **overrides: Any) -> Any:
    """The daemon's ``uvicorn.Config`` (``run_daemon`` serves with exactly this).

    - ``log_config=None``: uvicorn's own lines go through the root handler, so they carry
      the same timestamps as ours (its default formatter has none) and the token filter.
    - ``ws_per_message_deflate=False`` (lane LM3, docs/design/LCD_MIRROR.md §7.3): every
      browser offers permessage-deflate and uvicorn accepts it by default. On loopback it
      buys nothing and costs about 9x the daemon's CPU per Live display message (6.3 ms
      against 0.7 ms on the noise pattern); the display's tiles are already encoded.
    """
    import uvicorn

    options: dict[str, Any] = {"log_level": log_level, "access_log": False, "lifespan": "on",
                               "timeout_graceful_shutdown": 5, "log_config": None,
                               "ws_per_message_deflate": False}
    options.update(overrides)
    return uvicorn.Config(app, **options)


def _server_class() -> type:
    import uvicorn

    class Server(uvicorn.Server):
        """uvicorn's server, but a SIGINT/SIGTERM ends ``run()`` normally.

        uvicorn records the signal and re-raises it once it has shut down, which
        kills the process before ``run_daemon`` can remove ``daemon.json`` and
        release the instance lock. The graceful shutdown is the same.
        """

        def handle_exit(self, sig: int, frame: Any) -> None:
            if self.should_exit and sig == signal.SIGINT:
                self.force_exit = True
            else:
                self.should_exit = True

    return Server


def reap_debris(engine: Any) -> list[str]:
    """What a killed daemon (``kill -9``, a crash) left running or lying around. Q2.

    Called once the single-instance lock is ours, so nothing of a LIVE daemon for this
    state dir is touched; every step only takes what a dead process owned:

    - each pack's ``reap_orphans()`` (optional hook): the MPS3 pack's SSH tunnels;
    - the debug service's orphaned OpenOCDs (``DebugService.reap_orphans``);
    - PTY links whose process is gone (``pty.sweep_stale``): their ``/dev/pts/N`` may
      belong to another terminal by now, so ``screen <path>`` would attach to it.
    """
    done: list[str] = []
    try:
        packs = engine.packs()
    except Exception:  # noqa: BLE001 - start-up clean-up must never stop the daemon
        log.exception("clean-up: the packs did not load")
        packs = {}
    for name, pack in sorted(packs.items()):
        hook = getattr(pack, "reap_orphans", None)
        if callable(hook):
            try:
                done += [f"{name}: {what}" for what in hook() or ()]
            except Exception:  # noqa: BLE001
                log.exception("clean-up: pack %s failed to reap its orphans", name)
    debug = getattr(engine, "debug", None)
    if debug is not None and getattr(debug, "reason", None) is None \
            and callable(getattr(debug, "reap_orphans", None)):
        try:
            done += [f"OpenOCD of {board}" for board in debug.reap_orphans()]
        except Exception:  # noqa: BLE001
            log.exception("clean-up: reaping orphaned OpenOCDs failed")
    try:
        from harness_manager.services import pty

        if pty.supported():
            done += [f"PTY link {p}" for p in pty.sweep_stale(pty.runtime_dir())]
    except Exception:  # noqa: BLE001
        log.exception("clean-up: sweeping stale PTY links failed")
    for what in done:
        log.warning("clean-up after a daemon that did not stop cleanly: %s", what)
    return done


def _hold_signals() -> None:
    """While the boards close, a SIGINT/SIGTERM must not kill the process half way.

    uvicorn restores the default handlers when ``run()`` returns, so a second Ctrl-C,
    or ``daemon stop``'s SIGTERM fallback, landing during ``close_all`` used to end the
    process there: its ssh tunnels and OpenOCDs orphaned, daemon.json and the lock
    left behind. Now the signal is logged and the clean-up finishes (Q2, 2026-09-24).
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def still_closing(sig: int, _frame: Any) -> None:
        log.warning("signal %d while closing the boards: finishing the clean-up first", sig)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, still_closing)


def _stop_on_hangup(server: Any) -> None:
    """SIGHUP (the terminal of a foreground daemon closed) stops it cleanly, like SIGTERM.

    uvicorn handles only SIGINT and SIGTERM; the default SIGHUP action killed the
    process with no clean-up at all.
    """
    sig = getattr(signal, "SIGHUP", None)
    if sig is None or threading.current_thread() is not threading.main_thread():
        return

    def hangup(_sig: int, _frame: Any) -> None:
        log.info("SIGHUP: stopping")
        server.should_exit = True

    with contextlib.suppress(OSError, ValueError):
        signal.signal(sig, hangup)


def _state_error(what: str, path: Path, exc: OSError) -> HarnessError:
    """An OSError on the state dir as a message with the next step (never a traceback)."""
    if exc.errno == errno.ENOSPC:
        why = "the disk is full"
        hint = f"free space on the disk holding {path}"
    elif exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        why = "it is not writable"
        hint = ("use a state directory you can write: --state-dir DIR or "
                "HARNESS_MANAGER_STATE_DIR")
    else:
        why = exc.strerror or str(exc)
        hint = "check the state directory (--state-dir, HARNESS_MANAGER_STATE_DIR)"
    return ActionFailedError(f"harness-manager-daemon cannot {what} {path}: {why}", hint=hint)


def _after_start(server: Any, d: Any, resume: dict[str, Any] | None) -> None:
    """Once the server answers: the resumed boards and PTYs, then the update checker."""
    deadline = time.monotonic() + 60.0
    while not getattr(server, "started", False):
        if getattr(server, "should_exit", False) or time.monotonic() > deadline:
            return
        time.sleep(0.05)
    checker = getattr(d, "update_checker", None)
    if checker is not None:
        try:
            checker.start()
        except Exception:  # noqa: BLE001 - the checker is never worth a daemon
            log.exception("the update checker did not start")
    if resume is not None:
        from .update_apply import resume_after_start

        resume_after_start(d, resume)


def host_allow_list(state_dir: Path, listen: str) -> frozenset[str]:
    """The ``Host`` names this service answers to (lane SET-API, ``hosts.py``): loopback, the
    ``--listen`` address, and ``advanced.allowed_hosts`` from the settings. A setting that
    cannot be read adds nothing (the loopback names still work), and says why in the log."""
    from .hosts import allowed_hosts

    extra: list[str] = []
    try:
        from harness_manager.settings import Resolver

        got = Resolver.load(state_dir).resolve("advanced.allowed_hosts")
        extra = [str(h) for h in got.value or []]
        for problem in got.problems:
            log.warning("advanced.allowed_hosts: %s", problem)
    except Exception as exc:  # noqa: BLE001 - a bad settings file must never stop the service
        log.warning("advanced.allowed_hosts could not be read (%s); only loopback and %s are "
                    "answered", exc, listen)
    names = allowed_hosts(listen, extra)
    log.info("answering to the host names: %s", ", ".join(sorted(names)))
    return names


def self_test() -> int:
    """``--self-test``: everything a start needs loads (app, engine, packs, routes, server)."""
    import tempfile

    import uvicorn  # noqa: F401 - the server must import too

    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine

    from .app import create_app

    with tempfile.TemporaryDirectory(prefix="hm-self-test-") as tmp:
        engine = Engine(EngineConfig(state_dir=Path(tmp)))
        try:
            packs = sorted(engine.packs())
            app = create_app(engine, token=new_token(), state_dir=Path(tmp))
            routes = len(app.routes)
            app.state.daemon.close()
        finally:
            engine.close_all()
    sys.stdout.write(json.dumps({"ok": True, "version": __version__, "packs": packs,
                                 "routes": routes}) + "\n")
    return 0


#: A start flag -> its setting (``restart`` rows, lane SET-WIRE).
START_SETTINGS = {"port": "advanced.port", "listen": "advanced.listen",
                  "log_level": "advanced.log_level"}
START_DEFAULTS = {"port": 0, "listen": "127.0.0.1", "log_level": "info"}


def start_setting(name: str, given: Any, state_dir: Path) -> Any:
    """``--port``/``--listen``/``--log-level``: the flag when given, else the setting in
    ``state_dir`` (``advanced.*``), else the built-in default. A flag that differs from the
    administrator's ``[lock]`` is refused (15); a settings file that cannot be read never
    stops the service (the default is used, and the log says why)."""
    key = START_SETTINGS[name]
    try:
        from harness_manager.settings import runtime

        r = runtime.resolved(key, state_dir=state_dir)
    except Exception as exc:  # noqa: BLE001 - a bad settings file must never stop the service
        log.warning("%s could not be read (%s); %s", key, exc,
                    "the flag is used" if given is not None else "the default is used")
        return START_DEFAULTS[name] if given is None else given
    if given is None:
        return r.value
    if r.locked and given != r.value:
        flag = "--" + name.replace("_", "-")
        raise RefusedError(f"{flag} {given} is not allowed: the administrator's policy "
                           f"{r.where} sets {key} to {r.value!r}",
                           hint=f"leave {flag} out, or ask your administrator")
    return given


def run_daemon(state_dir: Path, *, port: int | None = None, listen: str | None = None,
               pack_overrides: dict[str, dict] | None = None, log_level: str | None = None,
               demo: bool = False, resume: dict[str, Any] | None = None) -> int:
    """Serve until stopped. Returns 0; raises ``HarnessError`` if it cannot start.

    ``port``, ``listen``, ``log_level``: None reads the settings (``start_setting``).
    ``resume`` (lane OTA-D): a resume file's content. Its port, listen address, token, pack
    overrides and demo flag win over the arguments."""
    import uvicorn

    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager.settings import runtime
    from harness_manager.settings.files import use_config_dir

    from .app import create_app

    state_dir = Path(state_dir)
    if resume is not None:
        port, listen = int(resume["port"]), str(resume.get("listen") or listen or "") or None
        pack_overrides = resume.get("pack_overrides") or pack_overrides
        demo = bool(resume.get("demo", demo))
    port = start_setting("port", port, state_dir)
    listen = start_setting("listen", listen, state_dir)
    log_level = start_setting("log_level", log_level, state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _state_error("create its state directory", state_dir, exc) from None
    instance = DaemonInstance(state_dir)
    try:
        instance.acquire()
    except OSError as exc:
        raise _state_error("write its lock file in", state_dir, exc) from None
    engine: Any = None
    wrote = False
    # SET-WIRE (SETTINGS.md §12.6): this process is the service for state_dir, so every
    # reader of the state dir (boards.toml, settings.toml, tunnels, statics, leases) uses
    # it, not $HARNESS_MANAGER_STATE_DIR's or ~/.config's; restart rows keep these files.
    outer_dir = use_config_dir(state_dir)
    runtime.refresh()
    runtime.prime(state_dir)
    try:
        sock = bind_socket(listen, port)
        if not is_loopback(listen):
            log.warning("harness-manager-daemon listens on %s, which is not loopback: anyone who can "
                        "reach it AND has the token controls your boards", listen)
        token = str(resume["token"]) if resume is not None else new_token()
        if demo:        # scripted boards, no hardware: `harness-manager ui --demo`
            from harness_manager.demo import DemoEngine

            # The showcase (one board of each harness, every feature), over the demo's own
            # state dir: its catalogue, kits, pins and history never touch the real one's.
            engine = DemoEngine(console_chatter=True, showcase=True, state_dir=state_dir)
        else:
            engine = Engine(EngineConfig(state_dir=state_dir,
                                         pack_overrides=pack_overrides or {}))
        engine.packs()               # bad pack settings fail here, before anyone connects
        if not demo:
            reap_debris(engine)
        holder: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = holder.get("server")
            if server is not None:
                server.should_exit = True

        app = create_app(engine, token=token, state_dir=state_dir, shutdown=request_shutdown,
                         allowed_hosts=host_allow_list(state_dir, listen))
        daemon = app.state.daemon
        if demo and getattr(daemon, "settings", None) is not None:
            # The demo's Settings: its own files only, no OS keyring, no real hub
            # (ops.SettingsContext.demo; lane SET-UI-MERGE).
            daemon.settings.demo = True
        # What a restart for an app update hands the next daemon (lane OTA-D).
        daemon.runtime = {"port": sock.getsockname()[1], "listen": listen,
                          "log_level": log_level, "pack_overrides": pack_overrides or {},
                          "demo": demo}
        daemon.resumed = resume
        server = _server_class()(uvicorn_config(app, log_level=log_level))
        holder["server"] = server
        install_redaction()
        info = new_info(port=sock.getsockname()[1], token=token, version=__version__,
                        listen=listen)
        try:
            write_info(state_dir, info)
        except OSError as exc:
            raise _state_error("write daemon.json in", state_dir, exc) from None
        wrote = True
        log.info("harness-manager-daemon %s (pid %d) serving %s for %s%s", __version__, info.pid,
                 info.base_url, state_dir,
                 f" (resumed after {resume.get('reason', 'a restart')}: "
                 f"{len(resume.get('boards') or [])} board(s))" if resume is not None else "")
        _stop_on_hangup(server)
        threading.Thread(target=_after_start, args=(server, daemon, resume), daemon=True,
                         name="harness-manager-daemon-after-start").start()
        # Install lane Q3 (from Q2's soak): a size cap on daemon.log while it runs.
        rotator = logfile.LogRotator(daemon_log_path(state_dir)).start()
        try:
            server.run(sockets=[sock])
        finally:
            rotator.stop()
            checker = getattr(daemon, "update_checker", None)
            if checker is not None:
                checker.stop()
        log.info("harness-manager-daemon stopped")
        return 0
    finally:
        _hold_signals()
        if engine is not None:
            t0 = time.monotonic()
            try:
                engine.close_all()
            except Exception:  # noqa: BLE001 - shutting down must finish
                log.exception("closing the boards failed")
            log.info("boards closed in %.1f s", time.monotonic() - t0)
        if wrote:
            remove_info(state_dir, os.getpid())
        instance.release()
        use_config_dir(outer_dir)
        runtime.refresh()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness-manager-daemon",
                                description="SoC Labs Harness Manager local engine service")
    p.add_argument("--state-dir", default=None, metavar="DIR",
                   help="state directory (default: $HARNESS_MANAGER_STATE_DIR or ~/.config/harness-manager)")
    p.add_argument("--port", type=int, default=None, metavar="N",
                   help="TCP port (default: the setting advanced.port; 0: any free port)")
    p.add_argument("--listen", default=None, metavar="ADDR",
                   help="address to bind (default: the setting advanced.listen, 127.0.0.1; "
                        "anything off loopback prints a warning)")
    p.add_argument("--log-level", default=None,
                   choices=("critical", "error", "warning", "info", "debug"),
                   help="default: the setting advanced.log_level (info)")
    p.add_argument("--demo", action="store_true",
                   help="serve scripted demo boards (no hardware); use its own --state-dir")
    p.add_argument("--resume", default=None, metavar="FILE",
                   help="restart from a resume file (after an app update): the same port and "
                        "token, the same boards and PTY paths")
    p.add_argument("--self-test", action="store_true",
                   help="check that everything a start needs loads, then exit (binds nothing)")
    # Development and test seam: per-pack constructor kwargs, as EngineConfig.pack_overrides.
    p.add_argument("--pack-overrides", default=None, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    from harness_manager.cli.output import error_line

    args = _parser().parse_args(argv)
    state_dir = Path(args.state_dir) if args.state_dir else default_state_dir()
    try:
        level = "info" if args.self_test else start_setting("log_level", args.log_level,
                                                            state_dir)
    except HarnessError as exc:
        sys.stderr.write(error_line(exc).replace("harness-manager:", "harness-manager-daemon:", 1) + "\n")
        return int(exc.code)
    logging.basicConfig(level=getattr(logging, str(level).upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.self_test:
        try:
            return self_test()
        except HarnessError as exc:
            sys.stderr.write(error_line(exc) + "\n")
            return int(exc.code)
    try:
        overrides = None
        if args.pack_overrides:
            try:
                overrides = json.loads(args.pack_overrides)
            except ValueError as exc:
                raise UsageError(f"--pack-overrides is not JSON: {exc}") from exc
            if not isinstance(overrides, dict):
                raise UsageError("--pack-overrides must be a JSON object")
        resume = None
        if args.resume:
            from .update_apply import read_resume

            resume = read_resume(Path(args.resume))
            Path(args.resume).unlink(missing_ok=True)       # it holds the token: read once
        return run_daemon(state_dir, port=args.port, listen=args.listen,
                          pack_overrides=overrides, log_level=level, demo=args.demo,
                          resume=resume)
    except HarnessError as exc:
        sys.stderr.write(error_line(exc).replace("harness-manager:", "harness-manager-daemon:", 1) + "\n")
        return int(exc.code)
