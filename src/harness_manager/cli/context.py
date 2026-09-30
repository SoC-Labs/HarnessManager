"""Shared plumbing for the verbs: TARGET resolution, sessions, adapters, holding, prompts.

TARGET forms (the help text documents the same list):

- ``host[:port]`` / ``[v6addr]:port``: the shell's control address (MPS3 default port 6900);
- ``-``: no Ethernet link, a USB-only board; give ``--serial URL`` and/or ``--volume PATH``.

``--serial`` / ``--volume`` add USB links to any TARGET. A bare device path
(``/dev/ttyUSB0``, ``COM7``) is turned into a ``serial://`` URL.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from harness_manager.core.capabilities import negotiate
from harness_manager.core.errors import (
    AlreadyError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.pack import BoardSession
from harness_manager.core.session import LockOwner, SessionLock, default_lock_dir

from .output import Result, emit, progress_line

DEFAULT_PACK = "mps3"
USB_ONLY = "-"
#: Marks a lock held by a FOREGROUND CLI verb (attach, console, debug up). ``detach``
#: only ever signals a holder carrying this tag, and only when it is the caller's own.
HOLD_TAG = "[cli-hold]"

#: ``--via`` on every verb that opens a board: the metavar and help, in one place so the
#: verbs cannot disagree (``tunnel.parse_via`` takes ``ssh:HOST`` or ``hub``).
VIA_METAVAR = "ssh:HOST|hub"
VIA_HELP = ("reach the shell through an SSH tunnel on HOST (ssh:HOST), or through the hub the "
            "board's boards.toml hub table names (hub); without --via, the board's boards.toml "
            "via does the same")
#: ``--serial`` where it ADDS a link to a TARGET.
SERIAL_HELP = ("add the board controller's USB serial link (serial:///dev/ttyUSB0, COM7, "
               "/dev/ttyUSB0)")


def serial_url(value: str) -> str:
    return value if "://" in value else f"serial://{value}"


def me_user() -> str:
    # The same spelling SessionLock.acquire records.
    return os.environ.get("USER") or os.environ.get("USERNAME") or "user"


def is_mine(owner: LockOwner | None) -> bool:
    return owner is not None and owner.host == socket.gethostname() and owner.user == me_user()


def is_my_hold(owner: LockOwner | None) -> bool:
    return is_mine(owner) and HOLD_TAG in (owner.note if owner else "")


def hold_note(what: str) -> str:
    return f"{HOLD_TAG} {what}"


def lock_dir_for(engine: Any) -> Path:
    """Where the engine keeps its session locks (``state_dir/locks``)."""
    explicit = getattr(engine, "lock_dir", None)
    if isinstance(explicit, Path):
        return explicit
    config = getattr(engine, "config", None)
    state_dir = getattr(config, "state_dir", None)
    if state_dir is not None:
        return Path(state_dir) / "locks"
    return default_lock_dir()


@dataclass
class Ctx:
    args: argparse.Namespace
    engine: Any
    fmt: str                            # "json" | "tsv" | "human"
    err: TextIO | None = None           # stderr override (tests)

    # -- helpers -----------------------------------------------------------------------

    @property
    def pack(self) -> str:
        return getattr(self.args, "pack", None) or DEFAULT_PACK

    @property
    def verb(self) -> str:
        return getattr(self.args, "cmd", "") or ""

    def emit(self, result: Result) -> None:
        emit(self.fmt, result)

    def note(self, text: str) -> None:
        """A human remark on stderr (never on stdout, which carries the result)."""
        progress_line(self.err, text)

    def lock(self, board_id: str) -> SessionLock:
        return SessionLock(board_id, lock_dir=lock_dir_for(self.engine))

    def owner(self, board_id: str) -> LockOwner | None:
        """Who holds the board (any process), without opening it."""
        lookup = getattr(self.engine, "lock_owner", None)
        if callable(lookup):
            return lookup(board_id)
        return self.lock(board_id).owner()

    # -- target resolution -------------------------------------------------------------

    def usb_links(self) -> tuple[Link, ...]:
        serial = [serial_url(s) for s in (getattr(self.args, "serial", None) or ())]
        volume = list(getattr(self.args, "volume", None) or ())
        return tuple([Link(LinkKind.USB_SERIAL, s, "given with --serial") for s in serial]
                     + [Link(LinkKind.USB_MSD, v, "given with --volume") for v in volume])

    def candidate(self) -> Candidate:
        target = self.args.target
        extra = self.usb_links()
        if target == USB_ONLY:
            if not extra:
                raise UsageError("TARGET '-' means a USB-only board, so it needs a USB link",
                                 hint="add --serial URL and/or --volume PATH")
            first = extra[0].address
            return Candidate(pack=self.pack, board_id=f"{self.pack}@usb:{first}", links=extra,
                             label=f"{self.pack} over USB ({first})", evidence="given explicitly")
        try:
            via = getattr(self.args, "via", "") or ""
            cand = (self.engine.candidate_for(target, self.pack, via=via) if via
                    else self.engine.candidate_for(target, self.pack))
        except ValueError as exc:
            raise UsageError(f"target {target!r} is not host[:port] ({exc})",
                             hint="e.g. 192.168.10.101 or 192.168.10.101:6900, or '-' for USB") \
                from exc
        if extra:
            cand = dataclasses.replace(cand, links=tuple(cand.links) + extra)
        return cand

    # -- sessions ----------------------------------------------------------------------

    @contextmanager
    def board(self, note: str = "") -> Iterator[tuple[Candidate, BoardSession]]:
        """Open the board under its session lock for the length of the block."""
        cand = self.candidate()
        try:
            session = self.engine.open(cand, note=note or f"cli {self.verb}")
        except HeldError as exc:
            raise self._explain_held(cand, exc) from exc
        try:
            yield cand, session
        finally:
            self.engine.close(cand.board_id)

    def _explain_held(self, cand: Candidate, exc: HeldError) -> HarnessError:
        try:
            owner = self.owner(cand.board_id)
        except (OSError, HarnessError):
            owner = None
        if not is_my_hold(owner):
            return exc
        assert owner is not None
        detach = f"`harness-manager detach {self.args.target}`"
        if self.verb == "attach" and "attach" in owner.note:
            return AlreadyError(f"{cand.board_id} is already attached by you (pid {owner.pid})",
                                hint=f"{detach} releases it")
        what = owner.note.replace(HOLD_TAG, "").strip() or "a foreground verb"
        stop = f"stop it with Ctrl-C there, or run {detach}"
        if what.startswith("console ") and "--export" not in what:
            # FIX-PACK-5: an interactive console sends Ctrl-C to the board; Ctrl-] ends it,
            # and Ctrl-] r, r resets the DUT from inside it (one process owns a board).
            stop = (f"end it there with Ctrl-] (Ctrl-C if it is --read-only), or run {detach}")
            if self.verb == "reset":
                stop = ("reset from that console instead: Ctrl-] then r, r (an interactive "
                        "console, not --read-only); or " + stop
                        + ". With the service running (`harness-manager daemon start` before "
                          "the console), both terminals share its session and `reset` works")
        return HeldError(exc.message, holder=exc.holder,
                         hint=f"your own `harness-manager {what}` (pid {owner.pid}) holds it; "
                              f"{stop}")

    def require(self, session: BoardSession, attr: str, capability: str) -> Any:
        """The session adapter ``attr``, or ``UnavailableError`` with the engine's reason."""
        adapter = getattr(session, attr, None)
        if adapter is not None:
            return adapter
        cand = session.candidate
        try:
            reason = self.engine.info(cand.board_id).unavailable.get(capability, "")
        except HarnessError:
            # The board did not answer, so its firmware features are unknown; the
            # link-based part of the capability view still holds.
            reason = ""
            pack = self.engine.packs().get(cand.pack)
            if pack is not None:
                _, missing = negotiate(pack.capability_specs(),
                                       [lk.kind for lk in cand.links], ())
                reason = missing.get(capability, "")
                if "firmware" in reason:
                    reason += " (the board did not answer, so its firmware is unknown)"
        if not reason:
            reason = (f"the {session.candidate.pack} pack in this build has no {attr} "
                      "adapter yet")
        raise UnavailableError(capability, reason)

    # -- consent -----------------------------------------------------------------------

    def confirm(self, question: str) -> None:
        """Ask on stderr, read one line from stdin. Anything but y/yes refuses (exit 15)."""
        if getattr(self.args, "yes", False):
            return
        stream = self.err or sys.stderr
        stream.write(f"{question} [y/N] ")
        stream.flush()
        try:
            answer = sys.stdin.readline()
        except (OSError, ValueError, EOFError):
            answer = ""
        if not answer.endswith("\n"):         # EOF: end the prompt line ourselves
            stream.write("\n")
            stream.flush()
        if answer.strip().lower() not in ("y", "yes"):
            raise RefusedError("not confirmed", hint="re-run with --yes to skip the prompt")

    # -- events as progress ------------------------------------------------------------

    @contextmanager
    def bus_progress(self, board_id: str, prefix: str) -> Iterator[None]:
        """Print ``<prefix>.*`` events for this board on stderr while the block runs."""
        bus = getattr(self.engine, "bus", None)
        if bus is None:
            yield
            return

        def _on(ev: Event) -> None:
            if ev.board_id and ev.board_id != board_id:
                return
            self.note(describe_event(ev))

        unsubscribe = bus.subscribe(f"{prefix}.*", _on)
        try:
            yield
        finally:
            unsubscribe()


def describe_event(ev: Event) -> str:
    d = ev.data
    kind = ev.topic.split(".", 1)[-1]
    head = ev.topic.split(".", 1)[0]
    if kind == "progress":
        total = d.get("total") or 0
        done = d.get("bytes", d.get("done", 0)) or 0
        pct = f" ({100 * done // total}%)" if total else ""
        return f"{head}: {d.get('phase', 'progress')} {done}/{total or '?'}{pct}"
    if kind == "failed":
        return f"{head}: failed: {d.get('reason', 'no reason given')}"
    if kind == "started":
        what = d.get("overlay") or d.get("rm") or ""
        return f"{head}: started {what} {d.get('rm_id', '')}".rstrip()
    if d:
        fields = " ".join(f"{k}={v:.3g}" if isinstance(v, float) else f"{k}={v}"
                          for k, v in sorted(d.items()))
        return f"{head}: {kind} {fields}"
    return f"{head}: {kind}"


# --- holding the foreground ----------------------------------------------------------------


class Stopper:
    """Turns SIGINT/SIGTERM (and SIGBREAK on Windows) into a flag while installed.

    Signal handlers can only be installed from the main thread; elsewhere the
    stopper still honours its deadline, and Ctrl-C arrives as KeyboardInterrupt.
    """

    def __init__(self, seconds: float | None) -> None:
        self.deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)
        self.event = threading.Event()
        self.reason = ""
        self._old: dict[int, Any] = {}

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self.reason = f"signal {signum}"
        self.event.set()

    def __enter__(self) -> Stopper:
        if threading.current_thread() is threading.main_thread():
            sigs = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGBREAK"):
                sigs.append(signal.SIGBREAK)
            for sig in sigs:
                try:
                    self._old[sig] = signal.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    continue
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old)
        self._old.clear()

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    @property
    def done(self) -> bool:
        if self.event.is_set():
            return True
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.reason = self.reason or "time limit"
            return True
        return False

    def wait(self) -> str:
        while not self.done:
            left = self.remaining()
            self.event.wait(0.5 if left is None else min(0.5, left))
        return self.reason


def hold(seconds: float | None) -> str:
    """Block until a stop signal or until ``seconds`` elapse (``None``: no limit)."""
    if seconds is not None and seconds <= 0:
        return "no hold requested"
    with Stopper(seconds) as stopper:
        return stopper.wait()
