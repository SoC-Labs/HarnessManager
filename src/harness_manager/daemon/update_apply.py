"""Apply a staged app update with a seamless restart, and roll back automatically (lane OTA-D).

docs/design/HM_SELF_UPDATE.md §5.3-5.4, with david's decisions of 2026-09-24: U3 (notify,
auto-stage, apply on a click) and U4 (the same PTY paths and a re-attach notice; no fd
handover). Two halves live here.

**The daemon's half** (``Applier``, driven by ``POST /update/app/apply``):

1. **Check.** The version is staged, not marked bad, and not what runs; no OTHER process holds
   a board or has a harness update open (the launcher switch would be refused); the new
   version passes ``python -m harness_manager.daemon --self-test``. Soft-busy sessions (GDB on
   OpenOCD, XVC, ``screen`` on a PTY) refuse it with 409 ``SOFT_BUSY`` unless ``confirm``.
2. **Drain.** New jobs get 409 HELD ``error.data.reason == "DRAINING"``; the running ones
   finish (no timeout unless ``drain_timeout_s``). ``POST /update/app/cancel`` ends it.
3. **Resume file** (``selfupdate.resume_path``, 0600): port, listen, token, the open boards
   with their notes, their consoles' PTY paths, the hub lease each one heartbeats (the lease
   token stays in ``<state>/leases/``: leases are NOT released), and the pack overrides.
4. **Notices:** one line into each PTY, "re-attach with ``screen <path>`` in a few seconds".
5. **Helper, then exit.** A detached helper (``main`` below) runs with THIS process's
   interpreter, the old and known-good one; the daemon then exits cleanly (boards closed:
   locks released, OpenOCD stopped).

**The helper's half** (``python -m harness_manager.daemon.update_apply``):

1. waits for the old daemon's pid to go;
2. switches the pointer (``current.json``), retrying while another process holds a board;
3. starts the NEW daemon with ``--resume`` (same port, same token);
4. requires ``/health`` to report the new version within ``health_s`` (30) and to stay up,
   same pid, for ``stable_s`` (10), and the old token to still be accepted;
5. otherwise: stops it, puts the pointer back, marks the version bad (OTA-C's catalogue
   store and the pointer, ``AppUpdater.mark_bad``: never offered again), and starts the OLD
   daemon with the same resume;
6. writes ``last_apply.json`` ``{id, from, to, result: applied|rolled-back|down|not-switched,
   phase, reason, seconds}``.

The daemon that comes up from a resume reopens the boards and the PTYs at the same paths
(``resume_after_start``), then publishes ``update.applied`` or ``update.rolled_back`` once the
helper has written the outcome.

Events (docs/CONTRACTS.md): ``update.applying {id, phase: checking|draining|restarting|
cancelled|failed, from, to, waiting_on, eta_s, reason}``, ``update.applied {id, from, to,
seconds}``, ``update.rolled_back {id, from, to, phase, reason}``.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness_manager import __version__
from harness_manager.core.errors import (
    AlreadyError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.services.update import selfupdate as su

log = logging.getLogger(__name__)

HEALTH_S = 30.0
STABLE_S = 10.0
OLD_PID_WAIT_S = 60.0
SWITCH_WAIT_S = 60.0
SELF_TEST_S = 60.0
#: How long a restarting daemon lets ``screen`` read the notice before the line goes.
NOTICE_GRACE_S = 1.0
#: How long a resumed daemon waits for the helper's verdict before it stops looking.
VERDICT_WAIT_S = 180.0
RESUME_SCHEMA = 1
DAEMON_NOTE_PREFIX = "harness-manager-daemon:"

IDLE, CHECKING, DRAINING, RESTARTING = "idle", "checking", "draining", "restarting"


# --- errors the routes raise ------------------------------------------------------------------


def draining_error(plan: dict[str, Any]) -> HeldError:
    err = HeldError(f"harness-manager-daemon is restarting to apply harness-manager "
                    f"{plan['to']}; it takes no new jobs",
                    holder="harness-manager-daemon (applying an update)",
                    hint="wait for the restart (a few seconds once the running jobs finish), "
                         "or cancel it: POST /api/v1/update/app/cancel")
    err.data = {"reason": "DRAINING", "version": plan["to"],  # type: ignore[attr-defined]
                "from": plan["from"], "apply": plan["id"]}
    return err


def soft_busy_error(version: str, busy: list[dict[str, Any]]) -> RefusedError:
    what = "; ".join(b["detail"] for b in busy)
    err = RefusedError(f"applying harness-manager {version} restarts the service, which ends: "
                       f"{what}", hint='confirm with {"confirm": true} (the CLI asks), or close '
                                       "them first")
    err.data = {"reason": "SOFT_BUSY", "soft_busy": busy,  # type: ignore[attr-defined]
                "version": version}
    return err


# --- what the daemon holds -----------------------------------------------------------------------


def _pty_sources(d: Any) -> list[Any]:
    out = []
    broker = getattr(d.engine, "consoles", None)
    if broker is not None and getattr(broker, "reason", None) is None and \
            callable(getattr(broker, "open_ptys", None)):
        out.append(broker)
    fallback = getattr(d, "fallback_ptys", None)
    if fallback is not None:
        out.append(fallback)
    return out


def open_ptys(d: Any) -> list[dict[str, Any]]:
    """Every PTY the daemon has open: ``{board_id, name, path, device, clients, command}``."""
    rows: list[dict[str, Any]] = []
    for src in _pty_sources(d):
        try:
            rows += src.open_ptys()
        except Exception:  # noqa: BLE001 - a listing never stops an apply
            log.exception("listing the PTYs failed")
    return rows


def soft_busy(d: Any) -> list[dict[str, Any]]:
    """Interactive sessions a restart ends: GDB on OpenOCD, XVC, ``screen`` on a PTY.

    ``d.soft_busy_probes`` (a list of callables returning rows) lets another lane add more.
    """
    out: list[dict[str, Any]] = []
    engine = d.engine
    for bid in list(engine.open_boards()):
        try:
            session = engine.session(bid)
        except HarnessError:
            continue
        debug = getattr(engine, "debug", None)
        if debug is not None and getattr(debug, "reason", None) is None:
            try:
                st = debug.status(session)
            except Exception:  # noqa: BLE001
                st = None
            if st is not None and getattr(st, "state", "") in ("up", "starting"):
                port = getattr(st, "gdb_port", 0)
                out.append({"kind": "gdb", "board_id": bid,
                            "detail": f"OpenOCD for GDB on {bid}" + (f" (port {port})" if port
                                                                     else "")})
        xvc = getattr(engine, "xvc", None)                  # lane XVC-CORE, when it is there
        if xvc is not None and getattr(xvc, "reason", None) is None and \
                callable(getattr(xvc, "status", None)):
            try:
                st = xvc.status(session)
            except Exception:  # noqa: BLE001
                st = None
            if st is not None and getattr(st, "open", False):
                att = getattr(st, "attached", None) or {}
                who = f", {att.get('command') or 'a client'} attached" if att else ""
                out.append({"kind": "xvc", "board_id": bid,
                            "detail": f"the XVC session on {bid}{who}"})
    for p in open_ptys(d):
        clients = p.get("clients")
        if clients:
            out.append({"kind": "screen", "board_id": p["board_id"], "name": p["name"],
                        "path": p["path"], "clients": clients,
                        "detail": f"{clients} terminal(s) on {p['path']} (re-attach after the "
                                  "restart)"})
    for probe in getattr(d, "soft_busy_probes", ()) or ():
        try:
            out += list(probe())
        except Exception:  # noqa: BLE001
            log.exception("a soft-busy probe failed")
    return out


def snapshot(d: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """The resume file's content: what the next daemon needs to look like this one."""
    from harness_manager.cli.output import jsonable
    from harness_manager.services import pty as _pty

    runtime = getattr(d, "runtime", None) or {}
    ptys = open_ptys(d)
    leases = getattr(d, "leases", None)
    boards = []
    for bid in list(d.engine.open_boards()):
        try:
            session = d.engine.session(bid)
        except HarnessError:
            continue
        lock_owner = getattr(d.engine, "lock_owner", None)
        owner = lock_owner(bid) if callable(lock_owner) else None
        note = (getattr(owner, "note", "") if owner is not None else "") or ""
        if note.startswith(DAEMON_NOTE_PREFIX):
            note = note[len(DAEMON_NOTE_PREFIX):].strip()
        row: dict[str, Any] = {
            "board_id": bid, "candidate": jsonable(session.candidate), "note": note,
            "consoles": [{"name": p["name"], "path": p["path"]} for p in ptys
                         if p["board_id"] == bid],
        }
        hub = getattr(session, "hub", None)
        if hub is not None:
            host, target = str(getattr(hub, "host", "")), str(getattr(hub, "target", ""))
            stored = None
            store = getattr(leases, "store", None)
            with contextlib.suppress(Exception):
                stored = store.get(host, target) if store is not None else None
            row["lease"] = {"hub": host, "target": target, "stored": stored is not None}
        boards.append(row)
    return {
        "schema": RESUME_SCHEMA, "id": plan["id"], "reason": "update",
        "created_at": time.time(), "pid": os.getpid(),
        "from": plan["from"], "to": plan["to"], "from_pointer": plan["from_pointer"],
        "state_dir": str(d.state_dir), "port": runtime.get("port"),
        "listen": runtime.get("listen", "127.0.0.1"), "token": d.token,
        "log_level": runtime.get("log_level", "info"),
        "pack_overrides": runtime.get("pack_overrides") or {},
        "demo": bool(runtime.get("demo")), "pty_dir": str(_pty.runtime_dir()),
        "boards": boards,
    }


def notice_text(version: str) -> Callable[[dict[str, Any]], str]:
    def text(view: dict[str, Any]) -> str:
        cmd = view.get("command") or f"screen {view.get('path', '')}"
        return (f"\r\n[Harness Manager is restarting for an update to {version}; "
                f"re-attach with `{cmd}` in a few seconds]\r\n")
    return text


def announce(d: Any, version: str) -> list[str]:
    """The re-attach notice into every PTY. The paths written to."""
    done: list[str] = []
    for src in _pty_sources(d):
        fn = getattr(src, "announce_ptys", None)
        if callable(fn):
            try:
                done += fn(notice_text(version))
            except Exception:  # noqa: BLE001
                log.exception("writing the restart notice failed")
    return done


def helper_argv(*, state_dir: Path, root: Path, plan: dict[str, Any]) -> tuple[list[str],
                                                                                 dict[str, str]]:
    """The helper's command line and environment: THIS interpreter (the known-good one)."""
    from .control import daemon_python

    python, env = daemon_python()
    argv = [python, "-m", "harness_manager.daemon.update_apply",
            "--state-dir", str(state_dir), "--root", str(root), "--id", plan["id"],
            "--from", plan["from"], "--from-pointer", plan["from_pointer"], "--to", plan["to"],
            "--old-pid", str(os.getpid()), "--health-s", str(plan["health_s"]),
            "--stable-s", str(plan["stable_s"])]
    return argv, dict(env if env is not None else os.environ)


def spawn_detached(argv: list[str], env: dict[str, str], log_path: Path,
                   cwd: Path | None = None) -> subprocess.Popen:
    """Start ``argv`` detached (a new session / process group), output to ``log_path``."""
    from .control import CREATE_NEW_PROCESS_GROUP, DETACHED_PROCESS

    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT,
                              "close_fds": True, "env": env,
                              "cwd": str(cwd or log_path.parent)}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as fh:
        fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(argv[:3])} …\n".encode())
        fh.flush()
        kwargs["stdout"] = fh
        return subprocess.Popen(argv, **kwargs)


def self_test(python: Path, *, timeout: float = SELF_TEST_S) -> str:
    """``python -m harness_manager.daemon --self-test``: "" when it passed, else why not."""
    try:
        res = subprocess.run([str(python), "-m", "harness_manager.daemon", "--self-test"],
                             capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"its self-test did not run: {exc}"
    if res.returncode == 0:
        return ""
    tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1][:300]
    return f"its self-test failed (exit {res.returncode}): {tail or 'no output'}"


def pointer_name(app: Any, prefix: str | None = None) -> str | None:
    """The pointer's name for the venv this process runs from: ``""`` (the installer's),
    ``"0.2.0"``, or None when it is neither (it cannot be switched back to)."""
    from harness_manager import _launch

    here = Path(prefix or sys.prefix)
    st = app.state()
    installer = str((st.get("installer") or {}).get("venv") or "")
    if installer and _launch.same_path(installer, here):
        return ""
    if here.parent.name == "versions" and _launch.same_path(here.parent.parent, app.layout.root):
        for v in st["versions"]:
            if _launch.same_path(app.layout.venv(v), here):
                return v
    return None


# --- the daemon's half -----------------------------------------------------------------------


class Applier:
    """One apply at a time: check -> drain -> resume file -> notices -> helper -> exit."""

    def __init__(self, d: Any, service: Callable[[], Any], *,
                 spawn: Callable[..., Any] = spawn_detached,
                 self_test: Callable[[Path], str] = self_test,
                 prefix: str | None = None, grace_s: float = NOTICE_GRACE_S) -> None:
        self.d = d
        self._service = service
        self.spawn = spawn
        self.self_test = self_test
        self.prefix = prefix
        self.grace_s = grace_s
        self._mu = threading.Lock()
        self.state = IDLE
        self.plan: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None     # the last apply that did not restart
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.helper: Any = None

    # -- view --

    def status(self) -> dict[str, Any]:
        with self._mu:
            plan = dict(self.plan) if self.plan else None
            state = self.state
        out: dict[str, Any] = {"state": state}
        if plan is not None:
            out.update({k: plan[k] for k in ("id", "from", "to", "started_at", "confirmed",
                                              "drain_timeout_s", "health_s", "stable_s")})
            out["waiting_on"] = [{"job": j.id, "kind": j.kind, "board_id": j.board_id}
                                 for j in self.d.jobs.running()]
        if self.last is not None:
            out["last"] = dict(self.last)
        return out

    def _publish(self, phase: str, **data: Any) -> None:
        plan = self.plan or {}
        self.d.bus.publish(Event("update.applying", "", {
            "id": plan.get("id", ""), "phase": phase, "from": plan.get("from", ""),
            "to": plan.get("to", ""), **data}))

    # -- start / cancel --

    def start(self, version: str | None, *, confirm: bool = False,
              drain_timeout_s: float | None = None, health_s: float = HEALTH_S,
              stable_s: float = STABLE_S) -> dict[str, Any]:
        with self._mu:
            if self.state != IDLE and self.plan is not None:
                err = HeldError(f"an update to {self.plan['to']} is already being applied "
                                f"({self.state})", holder="harness-manager-daemon",
                                hint="wait for it, or POST /api/v1/update/app/cancel")
                err.data = {"reason": "APPLYING", "apply": self.plan["id"]}  # type: ignore[attr-defined]
                raise err
        for value, name, low, high in ((health_s, "health_s", 5, 600),
                                       (stable_s, "stable_s", 1, 300)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                    not low <= value <= high:
                raise UsageError(f"{name} must be {low}-{high} s, not {value!r}")
        if drain_timeout_s is not None and (isinstance(drain_timeout_s, bool) or not isinstance(
                drain_timeout_s, (int, float)) or drain_timeout_s <= 0):
            raise UsageError(f"drain_timeout_s must be a positive number, not {drain_timeout_s!r}")
        runtime = getattr(self.d, "runtime", None)
        if not runtime or not runtime.get("port"):
            raise UnavailableError("update_apply", "this server was not started by "
                                   "`harness-manager daemon`, so it cannot restart itself")
        svc = self._service()
        app = svc.app()
        app.guard("apply an app update")
        target = version or self._newest_staged(app)
        if not target:
            raise RefusedError("no staged version newer than the one that runs "
                               f"({__version__})",
                               hint="stage one first: POST /api/v1/update/app "
                                    '{"stage_only": true}, or `harness-manager update app '
                                    "--apply`")
        from harness_manager.services.update.appstage import bad_reason

        mark = bad_reason(svc.state, target, app=app) if hasattr(svc, "state") else app.bad(target)
        if mark is not None:
            raise RefusedError(f"harness-manager {target} is marked bad: "
                               f"{mark.get('reason') or 'its apply failed'}",
                               hint="a newer release will be offered when there is one")
        from harness_manager.services.update.app import STATE_STAGED
        from harness_manager.services.update.version import parse_version

        info = app.state()["versions"].get(target) or {}
        py = app.layout.python(target, windows=app.windows)
        if info.get("state") != STATE_STAGED or not py.exists():
            raise RefusedError(f"harness-manager {target} is not staged",
                               hint='stage it first: POST /api/v1/update/app {"version": '
                                    f'"{target}", "stage_only": true}}')
        if parse_version(target) == parse_version(__version__):
            raise AlreadyError(f"harness-manager {target} is what runs")
        from_pointer = pointer_name(app, self.prefix)
        if from_pointer is None:
            raise RefusedError(f"this daemon runs from {self.prefix or sys.prefix}, which the "
                               "pointer cannot switch back to",
                               hint="start the daemon through the installed `harness-manager`")
        others = [r for r in app.busy.reasons() if not r.startswith("this process holds")]
        if others:
            raise HeldError(f"cannot apply harness-manager {target} now: {'; '.join(others)}",
                            hint="finish or close those sessions first; the new version stays "
                                 "staged")
        busy = soft_busy(self.d)
        if busy and not confirm:
            raise soft_busy_error(target, busy)
        plan = {"id": su.new_id(), "from": __version__, "from_pointer": from_pointer,
                "to": target, "started_at": time.time(), "confirmed": bool(busy),
                "soft_busy": busy, "drain_timeout_s": drain_timeout_s,
                "health_s": float(health_s), "stable_s": float(stable_s),
                "root": str(app.layout.root), "python": str(py)}
        with self._mu:
            if self.state != IDLE:
                raise HeldError("an update is already being applied", holder="harness-manager-daemon")
            self.plan = plan
            self.state = CHECKING
            self._cancel.clear()
        self.d.jobs.drain(lambda: draining_error(plan))
        self._publish(CHECKING)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="harness-manager-daemon-apply")
        self._thread.start()
        return self.status()

    @staticmethod
    def _newest_staged(app: Any) -> str:
        from harness_manager.services.update.app import STATE_STAGED
        from harness_manager.services.update.version import is_version, parse_version

        best = ""
        for v, info in app.state()["versions"].items():
            if info.get("state") != STATE_STAGED or not is_version(v) or app.bad(v):
                continue
            if parse_version(v) <= parse_version(__version__):
                continue
            if not best or parse_version(v) > parse_version(best):
                best = v
        return best

    def cancel(self) -> dict[str, Any]:
        with self._mu:
            if self.state == IDLE:
                raise AlreadyError("no update is being applied", hint="nothing to cancel")
            if self.state == RESTARTING:
                err = HeldError("too late to cancel: harness-manager-daemon is restarting",
                                holder="harness-manager-daemon",
                                hint="the helper rolls back by itself if the new version "
                                     "does not come up")
                err.data = {"reason": "RESTARTING"}  # type: ignore[attr-defined]
                raise err
            self._cancel.set()
            self._end("cancelled", "cancelled by a user")
        return self.status()

    def _end(self, phase: str, reason: str) -> None:
        """Back to idle (under ``_mu``): jobs accepted again, the outcome kept for status."""
        plan = self.plan or {}
        self.d.jobs.undrain()
        self.last = {"id": plan.get("id", ""), "to": plan.get("to", ""), "result": phase,
                     "reason": reason, "at": time.time()}
        self.state = IDLE
        self._publish(phase, reason=reason)
        self.plan = None

    # -- the thread --

    def _run(self) -> None:
        plan = self.plan
        assert plan is not None
        try:
            why = self.self_test(Path(plan["python"]))
            if why:
                self._refuse_bad(plan, why)
                return
            with self._mu:
                if self._cancel.is_set() or self.state != CHECKING:
                    return
                self.state = DRAINING
            self._drain(plan)
        except Exception as exc:  # noqa: BLE001 - a bug here must not leave the daemon draining
            log.exception("applying harness-manager %s failed", plan.get("to"))
            with self._mu:
                if self.state in (CHECKING, DRAINING):
                    self._end("failed", f"internal error: {type(exc).__name__}: {exc}")

    def _refuse_bad(self, plan: dict[str, Any], why: str) -> None:
        reason = f"harness-manager {plan['to']}: {why}"
        log.warning("not applying: %s", reason)
        with contextlib.suppress(Exception):
            from harness_manager.services.update.appstage import mark_bad

            svc = self._service()
            mark_bad(svc.state, plan["to"], why, phase="self-test")        # OTA-C's store
            svc.app().mark_bad(plan["to"], why, phase="self-test")         # and the pointer
        su.write_json(su.last_apply_path(self.d.state_dir), {
            "id": plan["id"], "from": plan["from"], "to": plan["to"], "result": "refused",
            "phase": "self-test", "reason": why, "at": time.time(), "seconds": 0.0})
        with self._mu:
            if self.state == CHECKING:
                self._end("failed", reason)

    def _drain(self, plan: dict[str, Any]) -> None:
        deadline = (time.monotonic() + plan["drain_timeout_s"]) if plan["drain_timeout_s"] \
            else None
        waiting: list[str] | None = None
        while not self._cancel.is_set():
            running = self.d.jobs.running()
            if not running:
                break
            names = sorted(f"{j.kind} on {j.board_id or 'the service'}" for j in running)
            if names != waiting:
                waiting = names
                self._publish(DRAINING, waiting_on=[{"job": j.id, "kind": j.kind,
                                                     "board_id": j.board_id} for j in running])
            if deadline is not None and time.monotonic() > deadline:
                with self._mu:
                    if self.state == DRAINING:
                        self._end("failed", f"the running jobs did not finish within "
                                            f"{plan['drain_timeout_s']:g} s: {', '.join(names)}")
                return
            self.d.jobs.wait_idle(timeout=0.5, stop=self._cancel)
        with self._mu:
            if self._cancel.is_set() or self.state != DRAINING:
                return
            self.state = RESTARTING
        self._restart(plan)

    def _restart(self, plan: dict[str, Any]) -> None:
        sd = Path(self.d.state_dir)
        resume = snapshot(self.d, plan)
        su.write_json(su.resume_path(sd), resume, private=True)
        written = announce(self.d, plan["to"])
        argv, env = helper_argv(state_dir=sd, root=Path(plan["root"]), plan=plan)
        self._publish(RESTARTING, eta_s=plan["health_s"], boards=len(resume["boards"]),
                      notices=len(written))
        log.info("applying harness-manager %s: %d board(s), %d PTY notice(s); the helper "
                 "restarts the service", plan["to"], len(resume["boards"]), len(written))
        try:
            self.helper = self.spawn(argv, env, su.apply_log_path(sd), sd)
        except OSError as exc:
            su.resume_path(sd).unlink(missing_ok=True)
            with self._mu:
                self._end("failed", f"the apply helper did not start: {exc}")
            return
        if written:
            time.sleep(self.grace_s)          # let screen show the notice before the line goes
        shutdown = getattr(self.d, "shutdown", None)
        if callable(shutdown):
            shutdown()


# --- a daemon started with --resume ----------------------------------------------------------


def read_resume(path: Path) -> dict[str, Any]:
    data = su.read_json(path)
    if data is None:
        raise UsageError(f"--resume {path}: not a resume file (missing or not JSON)")
    if data.get("schema") != RESUME_SCHEMA or not data.get("token") or not data.get("port"):
        raise UsageError(f"--resume {path}: not a resume file this version reads "
                         f"(schema {data.get('schema')!r})")
    return data


def reopen_boards(d: Any, resume: dict[str, Any]) -> dict[str, Any]:
    """Open the boards and their PTYs again, at the same paths. ``{opened, ptys, failed}``."""
    from harness_manager.client.codec import from_json
    from harness_manager.core.model import Candidate

    report: dict[str, Any] = {"opened": [], "ptys": [], "failed": []}
    for row in resume.get("boards") or []:
        bid = row.get("board_id", "")
        try:
            cand = from_json(Candidate, row["candidate"])
            note = row.get("note") or "resumed after an update"
            d.engine.open(cand, note=f"{DAEMON_NOTE_PREFIX} {note}".strip())
            d.remember([cand])
            report["opened"].append(bid)
        except Exception as exc:  # noqa: BLE001 - one board never stops the others
            log.warning("resume: %s did not open again: %s", bid, exc)
            report["failed"].append({"board_id": bid, "what": "open", "reason": str(exc)})
            continue
        session = d.engine.session(bid)
        for con in row.get("consoles") or []:
            try:
                view = open_pty(d, session, con["name"])
            except Exception as exc:  # noqa: BLE001
                log.warning("resume: the PTY of %s/%s did not open again: %s", bid,
                            con.get("name"), exc)
                report["failed"].append({"board_id": bid, "what": f"pty {con.get('name')}",
                                         "reason": str(exc)})
                continue
            if view.get("path") != con.get("path"):
                log.warning("resume: the PTY of %s/%s moved from %s to %s", bid, con["name"],
                            con.get("path"), view.get("path"))
            report["ptys"].append({"board_id": bid, "name": con["name"],
                                   "path": view.get("path"), "was": con.get("path")})
    return report


def open_pty(d: Any, session: Any, name: str) -> dict[str, Any]:
    broker = d.engine.consoles
    fn = getattr(broker, "pty", None)
    if getattr(broker, "reason", None) is None and callable(fn):
        return fn(session, name)
    fallback = getattr(d, "fallback_ptys", None)
    if fallback is None:
        raise UnavailableError("console_pty", "this service has no PTYs")
    return fallback.pty(session, name)


def await_verdict(d: Any, resume: dict[str, Any], *, wait_s: float = VERDICT_WAIT_S,
                  stop: threading.Event | None = None, poll_s: float = 0.25) -> dict | None:
    """Publish ``update.applied`` or ``update.rolled_back`` once the helper wrote the outcome
    of THIS resume (``last_apply.json`` with the same id). Returns it, or None."""
    rid = resume.get("id", "")
    path = su.last_apply_path(d.state_dir)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and not (stop is not None and stop.is_set()):
        rec = su.read_json(path)
        if rec is not None and rec.get("id") == rid and rec.get("result") in (
                "applied", "rolled-back", "not-switched"):
            if rec["result"] == "applied":
                d.bus.publish(Event("update.applied", "", {
                    "id": rid, "from": rec.get("from", ""), "to": rec.get("to", ""),
                    "seconds": rec.get("seconds")}))
            else:
                d.bus.publish(Event("update.rolled_back", "", {
                    "id": rid, "from": rec.get("from", ""), "to": rec.get("to", ""),
                    "phase": rec.get("phase", ""), "reason": rec.get("reason", "")}))
            return rec
        time.sleep(poll_s)
    return None


def resume_after_start(d: Any, resume: dict[str, Any]) -> None:
    """In a thread once the server answers: boards and PTYs back, then the verdict event."""
    try:
        d.resume_report = reopen_boards(d, resume)
        log.info("resumed: %d board(s), %d PTY(s) reopened, %d failure(s)",
                 len(d.resume_report["opened"]), len(d.resume_report["ptys"]),
                 len(d.resume_report["failed"]))
    except Exception:  # noqa: BLE001
        log.exception("resume: reopening the boards failed")
    if resume.get("id"):
        await_verdict(d, resume)


# --- the helper's half ----------------------------------------------------------------------------


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, text: str) -> None:
        """One line into apply.log (the helper's own stdout and stderr go there too, so the
        line is written once, by this)."""
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} apply: {text}"
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()


def _health(host: str, port: int, *, token: str = "", path: str = "/api/v1/health",
            timeout: float = 1.5) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read() or b"{}")
        except ValueError:
            body = {}
        return resp.status, body if isinstance(body, dict) else {}
    except (OSError, http.client.HTTPException):
        return 0, {}
    finally:
        conn.close()


def wait_healthy(host: str, port: int, token: str, want: str, child: Any, *,
                 health_s: float, stable_s: float, sleep: Callable[[float], None] = time.sleep,
                 log_tail: Callable[[], str] = lambda: "") -> tuple[bool, str, str]:
    """(ok, why, phase): ``/health`` reports ``want`` within ``health_s``, stays up with the
    same pid for ``stable_s``, and the resumed token is accepted."""
    t0 = time.monotonic()
    first: dict[str, Any] | None = None
    seen = ""
    while time.monotonic() - t0 < health_s:
        code = child.poll() if child is not None else None
        if code is not None:
            tail = log_tail()
            return False, (f"the daemon exited with code {code} while starting"
                           + (f": {tail}" if tail else "")), "start"
        status, body = _health(host, port)
        if status == 200 and body.get("ok"):
            seen = str(body.get("version", ""))
            if seen == want:
                first = body
                break
        sleep(0.25)
    if first is None:
        return False, (f"/health did not report {want} within {health_s:g} s"
                       + (f" (it reported {seen})" if seen else "")), "health"
    pid = first.get("pid")
    t1 = time.monotonic()
    while time.monotonic() - t1 < stable_s:
        code = child.poll() if child is not None else None
        if code is not None:
            tail = log_tail()
            return False, (f"the daemon exited with code {code} "
                           f"{time.monotonic() - t1:.1f} s after it first answered"
                           + (f": {tail}" if tail else "")), "stable"
        status, body = _health(host, port)
        if status != 200 or body.get("pid") != pid or body.get("version") != want:
            return False, (f"the daemon stopped answering /health as {want} (pid {pid}) "
                           f"{time.monotonic() - t1:.1f} s after it first answered"), "stable"
        sleep(0.5)
    status, _ = _health(host, port, token=token, path="/api/v1/jobs", timeout=5.0)
    if status != 200:
        return False, f"the resumed token was refused (HTTP {status or 'no answer'})", "token"
    return True, f"/health reports {want} (pid {pid}) and stayed up {stable_s:g} s", ""


def python_for(app: Any, pointer: str) -> Path:
    """The Python of a pointer name: ``""`` is the installer's venv."""
    if not pointer:
        py = app.installer_python(app.state())
        if py is None:
            raise RefusedError("the installer's venv is not registered in the pointer")
        return py
    return app.layout.python(pointer, windows=app.windows)


def run_helper(a: argparse.Namespace, *, spawn: Callable[..., Any] = spawn_detached,
               sleep: Callable[[float], None] = time.sleep) -> int:
    from harness_manager.core.session import pid_alive
    from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe

    from .state import connect_host, daemon_log_path

    sd = Path(a.state_dir)
    say = _Log(su.apply_log_path(sd))
    t0 = time.monotonic()
    su.write_json(su.apply_record_path(sd), {
        "pid": os.getpid(), "id": a.id, "from": a.from_version, "to": a.to,
        "python_version": a.from_version, "started_at": time.time(), "phase": "waiting"})

    def verdict(result: str, phase: str = "", reason: str = "", **extra: Any) -> None:
        su.write_json(su.last_apply_path(sd), {
            "id": a.id, "from": a.from_version, "to": a.to, "result": result, "phase": phase,
            "reason": reason, "at": time.time(), "seconds": round(time.monotonic() - t0, 1),
            **extra})
        say(f"result {result}" + (f" ({phase}: {reason})" if reason else ""))

    mark = {"offset": 0}

    def log_mark() -> None:
        """What a daemon started from now on writes to daemon.log begins here."""
        try:
            mark["offset"] = daemon_log_path(sd).stat().st_size
        except OSError:
            mark["offset"] = 0

    def tail() -> str:
        """The last lines the daemon the helper started wrote (its own error, not the old
        daemon's shutdown)."""
        try:
            with open(daemon_log_path(sd), "rb") as fh:
                fh.seek(mark["offset"])
                lines = fh.read().decode(errors="replace").splitlines()
        except OSError:
            return ""
        lines = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("--- ")]
        return " | ".join(lines[-2:])[-300:]

    try:
        resume = read_resume(Path(a.resume))
    except HarnessError as exc:
        verdict("not-started", "resume", exc.message)
        return 2
    host, port, token = connect_host(resume.get("listen", "")), int(resume["port"]), resume["token"]
    env = dict(os.environ)
    if resume.get("pty_dir"):
        env["HARNESS_MANAGER_PTY_DIR"] = resume["pty_dir"]
    say(f"{a.from_version} -> {a.to} on port {port}; waiting for pid {a.old_pid} to exit")
    deadline = time.monotonic() + OLD_PID_WAIT_S
    while pid_alive(a.old_pid) and time.monotonic() < deadline:
        sleep(0.1)
    if pid_alive(a.old_pid):
        verdict("not-started", "stop", f"the old daemon (pid {a.old_pid}) did not exit within "
                                       f"{OLD_PID_WAIT_S:g} s")
        return 4
    up = AppUpdater(AppLayout(Path(a.root)), LocalBusyProbe(sd), windows=os.name == "nt",
                    running_version=a.from_version, state_dir=sd)
    before = up.state()

    def restart(pointer: str, want: str, why: str) -> bool:
        """Start the daemon of ``pointer`` with the resume file again; True when healthy."""
        su.write_json(su.resume_path(sd), {**resume, "reason": why}, private=True)
        py = Path(sys.executable) if pointer == a.from_pointer else python_for(up, pointer)
        say(f"starting {want} again ({py})")
        log_mark()
        child = spawn([str(py), "-m", "harness_manager.daemon", "--state-dir", str(sd),
                       "--resume", str(su.resume_path(sd))], env, daemon_log_path(sd), sd)
        ok, how, _ = wait_healthy(host, port, token, want, child, health_s=a.health_s,
                                  stable_s=min(a.stable_s, 3.0), sleep=sleep, log_tail=tail)
        say(f"{want}: {'up' if ok else 'DOWN'}: {how}")
        return ok

    # 2. the pointer
    switched = False
    if before["current"] != a.to:
        until = time.monotonic() + SWITCH_WAIT_S
        while True:
            try:
                up.switch(a.to)
                switched = True
                break
            except HeldError as exc:
                if time.monotonic() > until:
                    ok = restart(a.from_pointer, a.from_version, "not-switched")
                    verdict("not-switched" if ok else "down", "switch", exc.message)
                    return 4
                sleep(0.5)
            except HarnessError as exc:
                ok = restart(a.from_pointer, a.from_version, "not-switched")
                verdict("not-switched" if ok else "down", "switch", exc.message)
                return 15
    say(f"pointer: current {a.to or '(installed)'}")
    # 3-4. the new daemon
    try:
        py = python_for(up, a.to)
    except HarnessError as exc:
        py, why = None, exc.message
    child = None
    if py is not None:
        su.write_json(su.apply_record_path(sd), {
            "pid": os.getpid(), "id": a.id, "from": a.from_version, "to": a.to,
            "python_version": a.from_version, "started_at": time.time(), "phase": "starting"})
        log_mark()
        child = spawn([str(py), "-m", "harness_manager.daemon", "--state-dir", str(sd),
                       "--resume", a.resume], env, daemon_log_path(sd), sd)
        ok, why, phase = wait_healthy(host, port, token, a.to, child, health_s=a.health_s,
                                      stable_s=a.stable_s, sleep=sleep, log_tail=tail)
        if ok:
            say(why)
            verdict("applied", pid=child.pid)
            su.apply_record_path(sd).unlink(missing_ok=True)
            return 0
    else:
        phase = "start"
    # 5. roll back
    say(f"{a.to} is not healthy ({phase}): {why}; rolling back")
    if child is not None and child.poll() is None:
        _stop(sd, child, say)
    if switched:
        st = up.state()
        st["current"], st["previous"] = before["current"], before["previous"]
        st["switched_at"] = time.time()
        up._save(st)
    up.mark_bad(a.to, why, phase=phase or "health")
    say(f"marked {a.to} bad; pointer back to {before['current'] or '(installed)'}")
    ok = restart(a.from_pointer, a.from_version, "rollback")
    verdict("rolled-back" if ok else "down", phase or "health", why)
    su.apply_record_path(sd).unlink(missing_ok=True)
    return 6


def _stop(sd: Path, child: Any, say: Callable[[str], None]) -> None:
    """Stop the new daemon the helper started (its own child)."""
    from . import control

    try:
        control.stop(sd, force=True, timeout=10.0)
    except HarnessError as exc:
        say(f"stopping it through its API: {exc.message}")
    if child.poll() is None:
        with contextlib.suppress(OSError):
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m harness_manager.daemon.update_apply",
                                description="The app-update apply helper (started by the daemon)")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--root", required=True, help="the install root (current.json)")
    p.add_argument("--resume", default=None, help="the resume file (default: the state dir's)")
    p.add_argument("--id", required=True)
    p.add_argument("--from", dest="from_version", required=True)
    p.add_argument("--from-pointer", default="")
    p.add_argument("--to", required=True)
    p.add_argument("--old-pid", type=int, required=True)
    p.add_argument("--health-s", type=float, default=HEALTH_S)
    p.add_argument("--stable-s", type=float, default=STABLE_S)
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.resume is None:
        a.resume = str(su.resume_path(Path(a.state_dir)))
    try:
        return run_helper(a)
    except Exception as exc:  # noqa: BLE001 - the verdict file must say what happened
        sd = Path(a.state_dir)
        su.write_json(su.last_apply_path(sd), {
            "id": a.id, "from": a.from_version, "to": a.to, "result": "down",
            "phase": "helper", "reason": f"internal error: {type(exc).__name__}: {exc}",
            "at": time.time()})
        raise


if __name__ == "__main__":
    sys.exit(main())
