"""Run socharnessd: ``python -m socharness.daemon [--state-dir D] [--port N] [--listen ADDR]``.

Start-up order, so a client never sees a half-started daemon:

1. take the single-instance lock (``HeldError``, exit 4, if a live daemon has it);
2. bind the socket (``PortBoundError``, exit 5, if the port is taken);
3. build the engine and the app;
4. write ``daemon.json`` (mode 0600) with the bound port and a fresh token;
5. serve until SIGINT/SIGTERM or ``POST /api/v1/daemon/shutdown``.

On the way out it closes every board (releasing their locks, stopping OpenOCD
and the consoles), removes ``daemon.json`` and releases the instance lock.

The log goes to stderr, which ``socharness daemon start`` points at
``<state_dir>/daemon.log``. WebSocket URLs carry ``?token=``, so every log
line passes a filter that masks it.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from socharness import __version__
from socharness.core.errors import HarnessError, PortBoundError, UsageError

from .state import DaemonInstance, default_state_dir, is_loopback, new_info, remove_info, write_info

log = logging.getLogger("socharness.daemon")

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


def install_redaction() -> None:
    flt = RedactToken()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "socharness"):
        logger = logging.getLogger(name)
        logger.addFilter(flt)
        for handler in logger.handlers:
            handler.addFilter(flt)


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


def run_daemon(state_dir: Path, *, port: int = 0, listen: str = "127.0.0.1",
               pack_overrides: dict[str, dict] | None = None, log_level: str = "info") -> int:
    """Serve until stopped. Returns 0; raises ``HarnessError`` if it cannot start."""
    import uvicorn

    from socharness.core.services import EngineConfig
    from socharness.engine import Engine

    from .app import create_app

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    instance = DaemonInstance(state_dir)
    instance.acquire()
    engine: Any = None
    wrote = False
    try:
        sock = bind_socket(listen, port)
        if not is_loopback(listen):
            log.warning("socharnessd listens on %s, which is not loopback: anyone who can "
                        "reach it AND has the token controls your boards", listen)
        token = new_token()
        engine = Engine(EngineConfig(state_dir=state_dir, pack_overrides=pack_overrides or {}))
        engine.packs()               # bad pack settings fail here, before anyone connects
        holder: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = holder.get("server")
            if server is not None:
                server.should_exit = True

        app = create_app(engine, token=token, state_dir=state_dir, shutdown=request_shutdown)
        config = uvicorn.Config(app, log_level=log_level, access_log=False, lifespan="on",
                                timeout_graceful_shutdown=5)
        server = _server_class()(config)
        holder["server"] = server
        install_redaction()
        info = new_info(port=sock.getsockname()[1], token=token, version=__version__,
                        listen=listen)
        write_info(state_dir, info)
        wrote = True
        log.info("socharnessd %s (pid %d) serving %s for %s", __version__, info.pid,
                 info.base_url, state_dir)
        server.run(sockets=[sock])
        log.info("socharnessd stopped")
        return 0
    finally:
        if engine is not None:
            try:
                engine.close_all()
            except Exception:  # noqa: BLE001 - shutting down must finish
                log.exception("closing the boards failed")
        if wrote:
            remove_info(state_dir, os.getpid())
        instance.release()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socharnessd",
                                description="SoC Labs Harness Manager local engine service")
    p.add_argument("--state-dir", default=None, metavar="DIR",
                   help="state directory (default: $SOCHARNESS_STATE_DIR or ~/.config/socharness)")
    p.add_argument("--port", type=int, default=0, metavar="N", help="TCP port (0: any free port)")
    p.add_argument("--listen", default="127.0.0.1", metavar="ADDR",
                   help="address to bind (default 127.0.0.1; anything else prints a warning)")
    p.add_argument("--log-level", default="info",
                   choices=("critical", "error", "warning", "info", "debug"))
    # Development and test seam: per-pack constructor kwargs, as EngineConfig.pack_overrides.
    p.add_argument("--pack-overrides", default=None, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    from socharness.cli.output import error_line

    args = _parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        overrides = None
        if args.pack_overrides:
            try:
                overrides = json.loads(args.pack_overrides)
            except ValueError as exc:
                raise UsageError(f"--pack-overrides is not JSON: {exc}") from exc
            if not isinstance(overrides, dict):
                raise UsageError("--pack-overrides must be a JSON object")
        state_dir = Path(args.state_dir) if args.state_dir else default_state_dir()
        return run_daemon(state_dir, port=args.port, listen=args.listen,
                          pack_overrides=overrides, log_level=args.log_level)
    except HarnessError as exc:
        sys.stderr.write(error_line(exc).replace("socharness:", "socharnessd:", 1) + "\n")
        return int(exc.code)
