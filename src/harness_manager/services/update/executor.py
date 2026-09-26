"""Run an approved harness update, and never claim more than the board proves.

The flow (each phase is journaled in ``state_dir/update/journal/<board>.json``):

1. **approval**: the plan's fingerprint and, for a re-key, the typed consent;
2. **nothing half-done is lying around**: our own journal and the SD's
   (``storage.pending()``); an interrupted install is refused with the restore
   that fixes it;
3. **the board is the one planned** (its identity has not changed since);
4. **download + verify** every component (``bundle.prepare_release``): the
   board is still untouched if any check fails;
5. **overlays** into the content store (no SD write, no reboot);
6. **OS slot** (Linux): write the inactive slot, arm try-once;
7. **base** (config SD): ``storage.backup`` (the mandatory gate), then
   ``storage.install`` with that backup, then ``controller.reboot()``, which
   returns its witness evidence (went down, came back);
8. **confirm**: re-read the identity the board reports. Only when it matches
   the release is the result ``installed``. Anything else is
   ``written-not-running``, with the backup to restore.

The same service rolls back (``rollback``): restore the backup, reboot,
confirm the old identity came back.

**Doors** (HUB-SD). A plan with ``via="hub"`` writes the base through the pack's
``session.hub_sd`` (the hub SD door: the same StorageAdapter shape, its backup the
running release's ``.bit`` from the signed cache, which this installer prepares), and
with ``updates.sd_ab`` on (``sd_ab=True``) a local base goes through
``session.ab_storage`` (the config SD A/B by pointer, U8). A remote door that leaves the
board **dark** (no ping, no version ``dark_after_s`` after the REBOOT) is written back
and REBOOTed when the approval armed auto-revert (U10), and says so loudly
(``update.dark``, ``update.auto_revert``, the history); not armed, it only reports.

Events on the engine bus: ``update.started``, ``update.progress`` ``{phase, bytes,
total}``, ``update.done`` ``{version, result}``, ``update.failed`` ``{reason}``.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import BoardIdentity, Check
from harness_manager.core.pack import BackupRecord, PreflightItem
from harness_manager.core.session import pid_alive

from .bundle import OverlayHandler, PreparedRelease, prepare_release
from .channel import VerifiedChannel
from .download import Downloader
from .hub_door import DARK_AFTER_S, VIA_HUB
from .os_slots import OsSlotAdapter
from .planner import (
    MODE_NONE,
    MODE_OVERLAYS,
    Approval,
    Plan,
    fw_sha_match,
    running_summary,
    ver32_match,
)
from .schema import (
    KIND_OVERLAYS,
    TARGET_ETHERNET,
    TARGET_HOST_STORE,
    TARGET_MCC_SD,
    HarnessIdentity,
    HarnessRelease,
)
from .state import InstallRecords, Journal, StoredComponents, UpdateState
from .version import same_version

log = logging.getLogger(__name__)

RESULT_INSTALLED = "installed"
RESULT_WRITTEN = "written-not-running"
RESULT_STORED = "stored"
RESULT_UP_TO_DATE = "up-to-date"
RESULT_RESTORED = "restored"
RESULT_RESTORED_UNCONFIRMED = "restored-not-confirmed"
# HUB-SD (U10): a remote install that left the board dark
RESULT_DARK = "dark"                              # not armed: reported, nothing done
RESULT_REVERTED = "auto-reverted"                 # the previous base is back and answers
RESULT_REVERT_FAILED = "auto-revert-failed"       # written back, but it did not come back
#: The A/B view of the local config SD records itself as this door (U8, ``sd_ab``).
VIA_AB = "ab"

@dataclass
class UpdateOutcome:
    board_id: str
    version: str
    result: str
    detail: str
    checks: list[PreflightItem] = field(default_factory=list)
    identity_after: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    backup: dict[str, Any] | None = None
    restore_hint: str = ""
    stored: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    os_slot: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.result in (RESULT_INSTALLED, RESULT_STORED, RESULT_UP_TO_DATE, RESULT_RESTORED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "board_id": self.board_id, "version": self.version, "result": self.result,
            "detail": self.detail, "ok": self.ok,
            "checks": [{"name": c.name, "check": c.check.value, "detail": c.detail}
                       for c in self.checks],
            "identity_after": self.identity_after, "evidence": self.evidence,
            "backup": self.backup, "restore_hint": self.restore_hint, "stored": self.stored,
            "skipped": self.skipped, "os_slot": self.os_slot,
        }


def _same_u32(a: str, b: str) -> bool:
    try:
        return int(a, 16) == int(b, 16)
    except (TypeError, ValueError):
        return a.lower() == b.lower()


def _item(name: str, ok: bool | None, detail: str, identity: bool = True) -> PreflightItem:
    check = Check.UNCHECKED if ok is None else (Check.OK if ok else Check.MISMATCH)
    return PreflightItem(name, check, detail, identity)


def confirm_identity(rel: HarnessRelease, ident: BoardIdentity | None) -> tuple[bool, list[PreflightItem]]:
    """Does the identity the board reports match the release? (confirmed, items)."""
    return confirm_wire_identity(rel.identity, ident)


def confirm_wire_identity(want: HarnessIdentity,
                          ident: BoardIdentity | None) -> tuple[bool, list[PreflightItem]]:
    """``confirm_identity`` on a bare wire identity (the journal keeps one, not a release).

    Essential: ``shell_id`` plus the ``harness`` string OR the firmware sha OR the
    ``ver32`` (HARNESS-DIST §3.2 rule 3; ver32 is H1). A matching sha or ver32 is
    decisive: the firmware reports ``harness=1.0.0`` whatever the release is tagged, so
    a differing version string is then shown UNCHECKED, never a mismatch. Any other
    MISMATCH (a differing ver32 included) fails the confirm.
    """
    items: list[PreflightItem] = []
    if ident is None or not ident.shell_id:
        items.append(_item("shell_id", None, "the shell did not answer: nothing to confirm with"))
        return False, items
    items.append(_item("shell_id", _same_u32(ident.shell_id, want.static_id),
                       f"board reports {ident.shell_id}, release is {want.static_id}"))
    sha_ok = fw_sha_match(want.fw_sha, ident.firmware_sha)
    v32_ok = ver32_match(want.ver32, ident.ver32)
    if want.harness:
        have = ident.harness_version
        ok = None if not have else same_version(have, want.harness)
        detail = f"board reports {have or 'nothing (no version verb)'}, release is {want.harness}"
        if ok is False and (sha_ok or v32_ok):
            ok = None
            detail += (" (not decisive: the firmware " + ("sha" if sha_ok else "ver32") +
                       " matches, and the firmware does not report the release's version)")
        items.append(_item("harness version", ok, detail))
    if want.fw_sha:
        items.append(_item("firmware sha", sha_ok,
                           f"board reports {ident.firmware_sha or '?'}, release is {want.fw_sha}"))
    if want.ver32:
        items.append(_item("ver32", v32_ok,
                           f"board reports {ident.ver32 or 'nothing'}, release is {want.ver32}"))
    if want.features:
        missing = sorted(set(want.features) - set(ident.features))
        items.append(_item("features", not missing,
                           f"missing {', '.join(missing)}" if missing else "all present"))
    if want.usercode:
        have = ident.usercode
        items.append(_item("usercode", None if not have else _same_u32(have, want.usercode),
                           f"board reports {have or 'nothing (not on the wire yet)'}, "
                           f"release is {want.usercode}"))
    if want.impl:
        have = ident.harness_impl
        items.append(_item("impl", None if not have else have == want.impl,
                           f"board reports {have or '?'}, release is {want.impl}"))
    if ident.build_check == Check.MISMATCH:
        items.append(_item("build check", False, "the firmware and fabric disagree (skew)"))
    checks = {i.name: i.check for i in items}
    version_ok = not (want.harness or want.fw_sha) or Check.OK in (
        checks.get("harness version"), checks.get("firmware sha"), checks.get("ver32"))
    confirmed = checks["shell_id"] == Check.OK and version_ok and \
        not any(i.check == Check.MISMATCH for i in items)
    return confirmed, items


def _wire(want: HarnessIdentity) -> dict[str, str]:
    """What the journal keeps of a release's wire identity, to recognise it after a crash."""
    return {"harness": want.harness, "fw_sha": want.fw_sha, "usercode": want.usercode,
            "impl": want.impl, "ver32": want.ver32}


def journaled_release_runs(j: dict[str, Any], ident: BoardIdentity | None) -> bool:
    """Does the board run the release an interrupted update's journal names?

    By the wire identity the journal recorded (``confirm_wire_identity``: the firmware
    sha is decisive). A journal written before it recorded one has only the release
    version, which is then compared as the harness string, as it always was.
    """
    if ident is None or not ident.shell_id:
        return False
    wire = j.get("identity") if isinstance(j.get("identity"), dict) else {}
    want = HarnessIdentity(
        static_id=str(j.get("static_id") or ident.shell_id),
        harness=str(wire.get("harness") or j.get("version") or ""),
        fw_sha=str(wire.get("fw_sha") or ""), usercode=str(wire.get("usercode") or ""),
        impl=str(wire.get("impl") or ""), ver32=str(wire.get("ver32") or ""))
    if not (want.harness or want.fw_sha or want.ver32):
        return False
    return confirm_wire_identity(want, ident)[0]


def _evidence(result: Any, controller: Any) -> dict[str, Any]:
    """Normalise the reboot evidence (a dict, a summary string, or None) plus the witness."""
    out: dict[str, Any] = {}
    if isinstance(result, dict):
        out.update(result)
    elif result is not None:
        out["summary"] = str(result)
    witness = getattr(controller, "last_reboot", None)
    for key in ("down_after_s", "up_after_s", "down_evidence", "up_evidence",
                "shell_id_before", "shell_id_after"):
        if witness is not None and hasattr(witness, key):
            val = getattr(witness, key)
            out.setdefault(key, list(val) if isinstance(val, tuple) else val)
    return out


def _record_fields(plan: Plan) -> dict[str, Any]:
    """What the install history keeps of a plan (HARNESS-CAT): the release that ran before
    it (``from_version``, by the wire identity; "" when unrecorded), the wire identity it
    installs, the doors, and the channel it came from."""
    rel = plan.release
    return {"kind": "install" if plan.base or plan.os_slot else "overlays",
            "from_version": plan.running_release, "channel": plan.channel,
            "serial": plan.serial, "mode": plan.mode, "rekey": plan.rekey,
            "fw_sha": rel.identity.fw_sha if rel else "",
            "static_id": rel.identity.static_id if rel else "", "doors": doors_of(plan)}


def _door_settled(storage: Any) -> bool:
    """A door with nothing in flight (``pending()`` None); False when it cannot say."""
    try:
        return storage.pending() is None
    except HarnessError:
        return False


def _is_previous(now: dict[str, Any], previous: dict[str, Any]) -> bool:
    """Does the board (``running_summary``) report the identity it ran before (``plan.running``)?
    The rollback's rule: shell, harness, and a sha/ver32 that does not contradict."""
    if not now.get("shell_id"):
        return False
    if not previous:
        return True
    return (_same_u32(now.get("shell_id", ""), previous.get("shell_id", "")) and
            now.get("harness") == previous.get("harness") and
            fw_sha_match(previous.get("firmware_sha", ""), now.get("firmware_sha", "")) is not False
            and ver32_match(previous.get("ver32", ""), now.get("ver32", "")) is not False)


def default_os_slots(session: Any) -> OsSlotAdapter | None:
    """``session.os_slots`` when the pack provides it (see the contract change request)."""
    return getattr(session, "os_slots", None)


def doors_of(plan: Plan) -> list[str]:
    """The doors an install goes through (HARNESS-DIST §3.1; the history records them)."""
    doors = [TARGET_MCC_SD] if plan.base else []
    if plan.os_slot:
        doors.append(TARGET_ETHERNET)
    if plan.release is not None and any(
            c.target == TARGET_HOST_STORE and c.name in plan.components
            for c in plan.release.components):
        doors.append(TARGET_HOST_STORE)
    return doors


class HarnessInstaller:
    """``lease_check(session, what)`` (HARNESS-CAT): raises ``HeldError`` unless this client
    holds the board's hub lease (a board with no hub has no lease: it passes). It runs before
    anything that writes to or reboots the board; None skips it (the unit tests' default)."""

    def __init__(self, *, state: UpdateState, downloader: Downloader, store: Any,
                 bus: EventBus | None = None, overlay_handler: OverlayHandler | None = None,
                 os_slots_for: Callable[[Any], OsSlotAdapter | None] = default_os_slots,
                 now: Callable[[], float] = time.time,
                 lease_check: Callable[[Any, str], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 sd_ab: bool | Callable[[], bool] = False,
                 dark_after_s: float = DARK_AFTER_S, dark_poll_s: float = 5.0) -> None:
        self.state = state
        self.downloader = downloader
        self.store = store
        self.bus = bus
        self.overlay_handler = overlay_handler
        self.os_slots_for = os_slots_for
        self.now = now
        self.lease_check = lease_check
        self.records = InstallRecords(state)
        # HUB-SD: the dark budget (U10) and the A/B flag (U8, ``updates.sd_ab``)
        self.sleep = sleep
        self.sd_ab = sd_ab
        self.dark_after_s = dark_after_s
        self.dark_poll_s = dark_poll_s

    # -- events --

    def _emit(self, topic: str, board_id: str, **data: Any) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))

    def _progress(self, board_id: str, prefix: str = "") -> Callable[[str, int, int], None]:
        def emit(phase: str, done: int, total: int) -> None:
            self._emit("update.progress", board_id, phase=f"{prefix}{phase}", bytes=done,
                       total=total)
        return emit

    # -- doors (HUB-SD) --

    def _sd_ab_on(self) -> bool:
        flag = self.sd_ab
        try:
            return bool(flag() if callable(flag) else flag)
        except Exception:  # noqa: BLE001 - an unreadable flag is the default: off
            return False

    def _door(self, session: Any, via: str = "") -> Any:
        """The storage a base goes through: the hub SD door (``via="hub"``), the A/B view
        of the local SD (``via="ab"``, or a local install with ``updates.sd_ab`` on), else
        T7's ``session.storage``. A door is bound to this installer's state and lease."""
        if via == VIA_HUB:
            door = getattr(session, "hub_sd", None)
        elif via == VIA_AB or (not via and self._sd_ab_on()
                               and getattr(session, "ab_storage", None) is not None):
            door = getattr(session, "ab_storage", None)
        else:
            return getattr(session, "storage", None)
        bind = getattr(door, "bind", None)
        if callable(bind):
            check = self.lease_check
            bind(state_dir=self.state.root, board_id=session.candidate.board_id,
                 lease_check=(lambda what: check(session, what)) if check is not None else None)
        return door

    def _previous_base(self, storage: Any, plan: Plan, verified: VerifiedChannel,
                       part: str) -> None:
        """A door that keeps the running release's base as its backup (the hub door: fpgahub
        cannot back up the SD) gets it here, downloaded and checked like any component."""
        b = (plan.hub or {}).get("backup") or {}
        prev = verified.channel.harness_release(str(b.get("version") or "")) if b else None
        if prev is None:
            raise RefusedError("this door keeps the running release's base as its backup, and "
                               "the channel no longer lists it", hint="plan it again")
        name = str(b.get("component") or "")
        prepared = prepare_release(prev, [name], downloader=self.downloader,
                                   base_url=verified.url, work=self.state.work(prev.version),
                                   part=part or verified.channel.board.part)
        storage.set_previous_base(prev.version, prepared.sd_files)

    def _await_identity(self, session: Any) -> BoardIdentity | None:
        """The identity once ping + version answer, within ``dark_after_s``; None: dark."""
        end = self.now() + self.dark_after_s
        while True:
            ident = self._identity(session)
            if ident is not None and ident.shell_id:
                return ident
            if self.now() >= end:
                return None
            self.sleep(self.dark_poll_s)

    # -- guards --

    @staticmethod
    def _identity(session: Any) -> BoardIdentity | None:
        try:
            return session.identity()
        except HarnessError as exc:
            log.info("identity read failed: %s", exc)
            return None

    def _check_journal(self, session: Any, board_id: str) -> Journal:
        """Deal with a journal a previous update left behind (it clears its own when done).

        - its process is alive: another update is running (``HeldError``);
        - it stopped BEFORE the SD write and the SD has no pending write: nothing on the
          board changed, so the stale journal is dropped;
        - it stopped AFTER the SD write finished: if the board now reports the journaled
          release, that install is recorded as done; otherwise it is refused with the
          two ways out (reboot and check, or roll back);
        - anything else (the SD write itself was interrupted): refused, roll back first.
        """
        journal = Journal(self.state, board_id)
        j = journal.read()
        if not j:
            return journal
        pid, host = int(j.get("pid", -1)), j.get("host", socket.gethostname())
        if host == socket.gethostname() and pid != os.getpid() and pid_alive(pid):
            raise HeldError(f"a harness update of {board_id} is in progress (pid {pid})",
                            hint="wait for it to finish; never start a second one")
        phase = j.get("phase", "")
        version = j.get("version", "")
        backup = (j.get("backup") or {}).get("path", "")
        if j.get("base") is False:
            return self._recover_os_only(session, board_id, journal, j)
        storage = self._door(session, str(j.get("via") or ""))
        pending: dict | None = {"state": "unknown"}
        if storage is not None:
            try:
                pending = storage.pending()
            except HarnessError:
                pending = {"state": "unknown"}
        if phase in ("verified", "os-written", "backing-up", "backed-up") and pending is None:
            log.info("dropping the journal of an update to %s that stopped at %s before any SD "
                     "write", board_id, phase)
            journal.clear()
            self._emit("update.progress", board_id, phase="journal-dropped", bytes=0, total=0)
            return journal
        if phase in ("written", "rebooting", "rebooted") and pending is None:
            ident = self._identity(session)
            if journaled_release_runs(j, ident):
                self.records.put(board_id, {
                    "version": version, "result": RESULT_INSTALLED, "kind": "recovered",
                    "from_version": j.get("from_version", ""),
                    "static_id": j.get("static_id", ""), "backup": j.get("backup"),
                    "previous": j.get("previous"), "identity_after": running_summary(ident),
                    "detail": f"recovered: the board reports harness {version} after an update "
                              f"that stopped at {phase}"})
                journal.clear()
                self._emit("update.done", board_id, version=version, result=RESULT_INSTALLED,
                           detail="recovered from an interrupted update")
                return journal
            raise RefusedError(
                f"the last update wrote harness {version or '?'} to the config SD of {board_id} "
                f"(stopped at {phase!r}) but the board does not report it",
                hint="reboot it (`harness-manager mcc TARGET reboot`) and run this again, or "
                     "`harness-manager update rollback TARGET` to restore "
                     + (f"the backup {backup}" if backup else "the last backup"))
        raise RefusedError(
            f"the last harness update of {board_id} stopped at phase {phase!r}",
            hint="run `harness-manager update rollback TARGET` to restore "
                 + (f"the backup {backup}" if backup else "the last backup (or pass --backup)"))

    def _recover_os_only(self, session: Any, board_id: str, journal: Journal,
                         j: dict[str, Any]) -> Journal:
        """An OS-slot-only update never touched the config SD, and stage0 guards the slots:
        either the board runs the new image (record it) or it kept/returned to the old slot."""
        version = j.get("version", "")
        ident = self._identity(session)
        if version and journaled_release_runs(j, ident):
            self.records.put(board_id, {
                "version": version, "result": RESULT_INSTALLED, "static_id": j.get("static_id", ""),
                "kind": "recovered", "from_version": j.get("from_version", ""),
                "previous": j.get("previous"),
                "identity_after": running_summary(ident),
                "detail": f"recovered: the board reports harness {version} after an OS-slot "
                          f"update that stopped at {j.get('phase')}"})
            self._emit("update.done", board_id, version=version, result=RESULT_INSTALLED,
                       detail="recovered from an interrupted update")
        else:
            log.info("dropping the journal of an OS-slot update to %s that stopped at %s; "
                     "stage0 keeps the old slot", board_id, j.get("phase"))
            self._emit("update.progress", board_id, phase="journal-dropped", bytes=0, total=0)
        journal.clear()
        return journal

    @staticmethod
    def _check_sd_pending(storage: Any) -> None:
        pending = storage.pending()
        if pending:
            backup = (pending.get("backup") or {}).get("path", "?")
            raise RefusedError(
                f"the config SD holds an interrupted {pending.get('op', 'write')} "
                f"(state {pending.get('state', '?')})",
                hint=f"restore it first: `harness-manager update rollback TARGET` (backup {backup})")

    # -- the run --

    def run(self, session: Any, plan: Plan, approval: Approval | None, verified: VerifiedChannel,
            *, part: str = "") -> UpdateOutcome:
        board_id = session.candidate.board_id
        if approval is None or approval.fingerprint != plan.fingerprint():
            raise RefusedError("this update was not approved (or the plan changed since)",
                               hint="plan it again and confirm it")
        if plan.blockers:
            raise RefusedError(f"this update cannot run: {'; '.join(plan.blockers)}")
        if plan.rekey and approval.consent != plan.consent_phrase:
            raise RefusedError("a re-key needs typed consent",
                               hint=f"type exactly: {plan.consent_phrase}")
        if plan.board_phrase and approval.board_phrase != plan.board_phrase:
            raise RefusedError("an install through the hub needs its typed consent",
                               hint=f"type exactly: {plan.board_phrase}")
        rel = plan.release
        assert rel is not None
        if (plan.base or plan.os_slot) and self.lease_check is not None:
            # HARNESS-DIST §2: a board behind a hub is written and rebooted only by the
            # lease holder (HeldError names who holds it). Before anything touches it.
            self.lease_check(session, f"install harness {rel.version}")
        journal = self._check_journal(session, board_id)
        if plan.mode == MODE_NONE:
            return UpdateOutcome(board_id, rel.version, RESULT_UP_TO_DATE,
                                 f"the board already runs harness {rel.version}")
        storage = self._door(session, plan.via) if plan.base else None
        controller = session.controller if plan.base else None
        slots = self.os_slots_for(session) if plan.os_slot else None
        if plan.base:
            if storage is None or controller is None:
                raise UnavailableError("harness install", "needs the Debug USB (config SD + MCC)")
            self._check_sd_pending(storage)
            preflight = getattr(storage, "preflight", None)
            if callable(preflight):
                preflight()          # HUB-SD: lease, tty_00, the hub's sd method; nothing yet
        if plan.os_slot and slots is None:
            raise UnavailableError("OS slot update", "this harness offers no OS slot update")
        if plan.running:
            now_running = running_summary(self._identity(session))
            if now_running.get("shell_id") != plan.running.get("shell_id") or \
                    now_running.get("harness") != plan.running.get("harness") or \
                    fw_sha_match(plan.running.get("firmware_sha", ""),
                                 now_running.get("firmware_sha", "")) is False or \
                    ver32_match(plan.running.get("ver32", ""),
                                now_running.get("ver32", "")) is False:
                raise RefusedError("the board changed since this update was planned "
                                   f"(planned against {plan.running}, now {now_running})",
                                   hint="check again, then confirm the new plan")

        self._emit("update.started", board_id, version=rel.version, mode=plan.mode,
                   rekey=plan.rekey)
        try:
            prepared = prepare_release(
                rel, plan.components, downloader=self.downloader, base_url=verified.url,
                work=self.state.work(rel.version), part=part or verified.channel.board.part,
                overlay_handler=self.overlay_handler, progress=self._progress(board_id))
        except HarnessError as exc:
            self._emit("update.failed", board_id, version=rel.version, reason=str(exc),
                       phase="verify")
            raise
        stored = self._store_overlays(prepared, board_id)
        skipped = {**plan.skipped, **prepared.skipped}
        if plan.mode == MODE_OVERLAYS:
            out = UpdateOutcome(board_id, rel.version, RESULT_STORED,
                                f"{len(stored)} overlay(s) keyed to {rel.identity.static_id} are "
                                "in the local store; the board was not touched",
                                checks=prepared.checks, stored=stored, skipped=skipped)
            self.records.put(board_id, {"version": rel.version, "result": out.result,
                                        "overlays": stored, **_record_fields(plan)})
            self._emit("update.done", board_id, version=rel.version, result=out.result)
            return out

        journal.write(phase="verified", version=rel.version, static_id=rel.identity.static_id,
                      identity=_wire(rel.identity), host=socket.gethostname(), pid=os.getpid(),
                      previous=plan.running, base=plan.base, os_slot=plan.os_slot,
                      from_version=plan.running_release,
                      via=getattr(storage, "via", "") if storage is not None else "")
        try:
            if plan.base and getattr(storage, "wants_previous_base", False):
                self._previous_base(storage, plan, verified, part)
            return self._install(session, plan, prepared, journal, storage, controller, slots,
                                 stored, skipped, approval=approval)
        except HarnessError as exc:
            self._emit("update.failed", board_id, version=rel.version, reason=str(exc),
                       phase=(journal.read() or {}).get("phase", ""))
            raise

    def _store_overlays(self, prepared: PreparedRelease, board_id: str) -> list[str]:
        """Every host-store part into the content store: overlays through the pack's importer
        (the deploy catalogue reads them), anything else (openocd, identity, DUT firmware)
        as one ``harness_<kind>`` blob keyed by version and static_id."""
        stored: list[str] = []
        rel = prepared.release
        for name, part in prepared.parts.items():
            comp = part.component
            if comp.target != TARGET_HOST_STORE:
                continue
            if comp.kind == KIND_OVERLAYS:
                if self.overlay_handler is None:
                    raise UnavailableError("overlay update", "no overlay importer for this pack")
                for rel_path, path in sorted(part.files.items()):
                    if rel_path.rsplit("/", 1)[-1] != "manifest.json":
                        continue
                    self.overlay_handler.import_(self.store, path.parent)
                    stored.append(f"{name}/{path.parent.name}")
                    self._emit("update.progress", board_id, phase="store-overlays",
                               bytes=len(stored), total=0)
            elif self.store is not None:
                self.store.put_file(part.blob, kind=f"harness_{comp.kind}",
                                    meta={"name": name, "version": rel.version,
                                          "static_id": rel.identity.static_id,
                                          "sha256": comp.asset.sha256})
                stored.append(name)
            else:
                continue
            StoredComponents(self.state).add(comp.asset.sha256, name, rel.version)
        return stored

    def _install(self, session: Any, plan: Plan, prepared: PreparedRelease, journal: Journal,
                 storage: Any, controller: Any, slots: OsSlotAdapter | None,
                 stored: list[str], skipped: dict[str, str],
                 approval: Approval | None = None) -> UpdateOutcome:
        board_id = session.candidate.board_id
        rel = prepared.release
        via = getattr(storage, "via", "") if storage is not None else ""
        os_info: dict[str, Any] | None = None
        backup: BackupRecord | None = None
        backup_d: dict[str, Any] | None = None

        # -- OS slot (written first; the reboot below boots it) --
        if plan.os_slot and slots is not None:
            os_info = self._write_os(slots, prepared, board_id)
            journal.write(phase="os-written", os_slot=os_info)

        # -- base on the config SD --
        if plan.base:
            journal.write(phase="backing-up")
            backup = storage.backup(self.state.backups(board_id),
                                    progress=self._progress(board_id, "backup:"))
            backup_d = {"path": backup.path, "sha256": backup.sha256}
            journal.write(phase="backed-up", backup=backup_d)
            journal.write(phase="writing")
            try:
                storage.install(prepared.sd_files, backup=backup,
                                progress=self._progress(board_id, "sd:"))
            except HarnessError as exc:
                # HUB-SD: a door that says nothing is in flight wrote nothing it would restore
                # (a refusal, a failed upload): the journal stays droppable. A door write the
                # hub has not finished keeps "interrupted", and the door's own hint (never
                # "roll back now": that is a second write mid-write).
                settled = bool(via) and _door_settled(storage)
                journal.write(phase="backed-up" if settled else "interrupted", error=str(exc))
                raise ActionFailedError(
                    f"writing the config SD failed: {exc.message}",
                    hint=(exc.hint if via and exc.hint else
                          f"restore the backup: `harness-manager update rollback TARGET` "
                          f"({backup.path})")) from exc
            journal.write(phase="written")

        # -- reboot, witnessed --
        journal.write(phase="rebooting")
        restore_hint = (f"`harness-manager update rollback TARGET` restores the backup {backup.path}"
                        if backup else "")
        try:
            if plan.base:
                raw = controller.reboot(progress=self._progress(board_id, "reboot:"),
                                        wait_s=plan.reboot_wait_s)
                evidence = _evidence(raw, controller)
            else:
                assert slots is not None
                raw = slots.reboot(progress=self._progress(board_id, "reboot:"),
                                   wait_s=plan.reboot_wait_s or 180.0)
                evidence = _evidence(raw, slots)
        except HarnessError as exc:
            if plan.base and plan.via and self._await_identity(session) is None:
                return self._on_dark(session, plan, approval, rel, journal, storage, controller,
                                     backup, backup_d, stored, skipped,
                                     {"reboot_error": str(exc)}, via)
            return self._finish(board_id, rel, RESULT_WRITTEN, journal,
                                f"written, not running: the reboot was not witnessed ({exc.message})",
                                backup_d, restore_hint, stored=stored, skipped=skipped,
                                os_info=os_info, evidence={"reboot_error": str(exc)},
                                plan=plan, via=via)
        journal.write(phase="rebooted", evidence=evidence)

        # -- confirm what the board reports --
        if plan.via:
            self._emit("update.progress", board_id, phase="confirm", bytes=0, total=1)
        ident = self._identity(session)
        if plan.base and plan.via and (ident is None or not ident.shell_id):
            ident = self._await_identity(session)
            if ident is None:
                return self._on_dark(session, plan, approval, rel, journal, storage, controller,
                                     backup, backup_d, stored, skipped, evidence, via)
        confirmed, checks = confirm_identity(rel, ident)
        identity_after = running_summary(ident)
        if plan.os_slot and slots is not None:
            # Confirm the new slot only when the board proved it runs the release; an
            # unconfirmed try-once slot is what lets stage0 fall back at the next boot.
            os_ok, os_info = self._confirm_os(slots, os_info or {}, checks, confirm=confirmed)
            confirmed = confirmed and os_ok
        if confirmed:
            return self._finish(board_id, rel, RESULT_INSTALLED, journal,
                                f"harness {rel.version} is running: the board reports shell "
                                f"{ident.shell_id if ident else '?'}, harness "
                                f"{ident.harness_version if ident else '?'}",
                                backup_d, "", checks=checks, identity_after=identity_after,
                                evidence=evidence, stored=stored, skipped=skipped,
                                os_info=os_info, previous=plan.running, plan=plan, via=via)
        bad = "; ".join(f"{c.name}: {c.detail}" for c in checks if c.check != Check.OK)
        return self._finish(board_id, rel, RESULT_WRITTEN, journal,
                            f"written, not running: after the reboot the board does not report "
                            f"harness {rel.version} ({bad})",
                            backup_d, restore_hint, checks=checks, identity_after=identity_after,
                            evidence=evidence, stored=stored, skipped=skipped, os_info=os_info,
                            previous=plan.running, plan=plan, via=via)

    def _write_os(self, slots: OsSlotAdapter, prepared: PreparedRelease,
                  board_id: str) -> dict[str, Any]:
        part = prepared.os_image
        if part is None:
            raise RefusedError("the plan writes an OS slot but no OS image was prepared")
        before = slots.status()
        target = before.inactive
        sha = part.component.asset.sha256
        written = slots.write_inactive(part.blob, sha256=sha, version=prepared.release.version,
                                       progress=self._progress(board_id, "os:"))
        if written != target:
            raise ActionFailedError(f"the OS image went to slot {written}, expected the inactive "
                                    f"slot {target}", hint="the running slot was not meant to change")
        after = slots.status()
        got = after.slots.get(target)
        if got is None or got.image_sha256.lower() != sha:
            raise ActionFailedError(f"slot {target} does not hold the image just written "
                                    f"(read back {got.image_sha256[:12] if got else 'nothing'})",
                                    hint="nothing booted it; retry the update")
        slots.arm_try_once(target)
        return {"slot": target, "previous_slot": before.active, "sha256": sha}

    def _confirm_os(self, slots: OsSlotAdapter, info: dict[str, Any],
                    checks: list[PreflightItem], *, confirm: bool) -> tuple[bool, dict[str, Any]]:
        st = slots.status()
        target = info.get("slot", "")
        active = st.active_info
        if st.active != target or active.image_sha256.lower() != info.get("sha256"):
            checks.append(_item("os slot", False,
                                f"the board booted slot {st.active}, not {target}: stage0 "
                                "rolled back to the old image"))
            return False, {**info, "active": st.active, "rolled_back": True}
        if not confirm:
            checks.append(_item("os slot", None, f"slot {target} booted but is left "
                                                 "unconfirmed: the board did not prove the release"))
            return False, {**info, "active": st.active, "state": active.state}
        slots.confirm(target)
        st2 = slots.status()
        ok = st2.active == target and st2.active_info.state == "confirmed"
        checks.append(_item("os slot", ok, f"slot {target} is "
                                          f"{st2.active_info.state or 'unconfirmed'}"))
        return ok, {**info, "active": st2.active, "state": st2.active_info.state}

    def _finish(self, board_id: str, rel: HarnessRelease, result: str, journal: Journal,
                detail: str, backup: dict[str, Any] | None, restore_hint: str, *,
                checks: list[PreflightItem] | None = None, identity_after: dict | None = None,
                evidence: dict | None = None, stored: list[str] | None = None,
                skipped: dict[str, str] | None = None, os_info: dict | None = None,
                previous: dict | None = None, plan: Plan | None = None, via: str = "",
                extra: dict[str, Any] | None = None) -> UpdateOutcome:
        out = UpdateOutcome(board_id, rel.version, result, detail, checks=checks or [],
                            identity_after=identity_after or {}, evidence=evidence or {},
                            backup=backup, restore_hint=restore_hint, stored=stored or [],
                            skipped=skipped or {}, os_slot=os_info)
        prior = self.records.get(board_id) or {}
        self.records.put(board_id, {
            "version": rel.version, "result": result, "static_id": rel.identity.static_id,
            "backup": backup, "previous": previous if previous is not None else prior.get("previous"),
            "identity_after": out.identity_after, "detail": detail,
            **(_record_fields(plan) if plan is not None else {}),
            **({"via": via} if via else {}), **(extra or {}),
        })
        journal.clear()
        topic = "update.done"
        self._emit(topic, board_id, version=rel.version, result=result, detail=detail)
        return out

    # -- a dark board after a remote install (HUB-SD, U10) --

    def _on_dark(self, session: Any, plan: Plan, approval: Approval | None, rel: HarnessRelease,
                 journal: Journal, storage: Any, controller: Any, backup: BackupRecord | None,
                 backup_d: dict[str, Any] | None, stored: list[str], skipped: dict[str, str],
                 evidence: dict[str, Any], via: str) -> UpdateOutcome:
        """The board answers neither ping nor version after the REBOOT. Armed: write the
        backup back through the same door, REBOOT, confirm the previous release answers.
        Not armed: say so, loudly, and touch nothing."""
        board_id = session.candidate.board_id
        armed = bool(approval is not None and approval.auto_revert)
        back_to = plan.running_release or (plan.running or {}).get("harness", "") or "the previous base"
        dark = (f"DARK: after the REBOOT {board_id} answered neither ping nor version within "
                f"{self.dark_after_s:.0f} s of harness {rel.version}")
        log.error("%s (auto-revert %s)", dark, "ARMED" if armed else "not armed")
        self._emit("update.dark", board_id, version=rel.version, armed=armed, back_to=back_to,
                   detail=dark)
        target = board_id
        if not armed or backup is None:
            why = ("auto-revert was NOT armed at approval" if not armed
                   else "there is no backup to revert to")
            hint = (f"`harness-manager update rollback {target}` writes {back_to}'s base back "
                    f"({backup.path})" if backup is not None else
                    "recover it by hand (the platform's JTAG recover, or the Debug USB)")
            return self._finish(board_id, rel, RESULT_DARK, journal,
                                f"{dark}; {why}: NOTHING WAS DONE. The config SD holds harness "
                                f"{rel.version}; the board stays dark until it is written back",
                                backup_d, hint, evidence=evidence, stored=stored, skipped=skipped,
                                previous=plan.running, plan=plan, via=via,
                                extra={"dark": True, "auto_revert": {"armed": armed}})
        journal.write(phase="auto-reverting")
        self._emit("update.auto_revert", board_id, phase="start", version=rel.version,
                   back_to=back_to, detail=f"{dark}: writing {back_to} back")
        revert: dict[str, Any] = {"armed": True, "back_to": back_to, "backup": backup_d}
        try:
            storage.restore(backup, progress=self._progress(board_id, "revert:"))
            revert["written"] = True
            raw = controller.reboot(progress=self._progress(board_id, "revert-reboot:"),
                                    wait_s=plan.reboot_wait_s)
            revert["reboot"] = _evidence(raw, controller)
        except HarnessError as exc:
            revert["error"] = str(exc)
        ident = self._identity(session)
        if ident is None or not ident.shell_id:
            ident = self._await_identity(session)
        now = running_summary(ident)
        ok = ident is not None and _is_previous(now, plan.running or {})
        result = RESULT_REVERTED if ok else RESULT_REVERT_FAILED
        if ok:
            detail = (f"{dark}. AUTO-REVERTED: {back_to} was written back through the hub and the "
                      f"board answers again (shell {now.get('shell_id')}, firmware "
                      f"{now.get('firmware_sha') or '?'}); harness {rel.version} is NOT installed")
        else:
            detail = (f"{dark}. AUTO-REVERT FAILED: "
                      + (f"writing {back_to} back failed ({revert['error']})" if "error" in revert
                         else f"{back_to} was written back but the board "
                              + ("does not answer" if ident is None else
                                 f"reports shell {now.get('shell_id')}, firmware "
                                 f"{now.get('firmware_sha') or '?'}"))
                      + "; recover it by hand")
        log.error(detail)
        self._emit("update.auto_revert", board_id, phase="done", version=rel.version,
                   back_to=back_to, result=result, detail=detail)
        return self._finish(board_id, rel, result, journal, detail, backup_d,
                            "" if ok else f"`harness-manager update rollback {target}` writes "
                                          f"{back_to} back again",
                            identity_after=now, evidence={**evidence, "revert": revert},
                            stored=stored, skipped=skipped, previous=plan.running, plan=plan,
                            via=via, extra={"dark": True, "auto_revert": revert})

    # -- rollback --

    def rollback(self, session: Any, *, backup_path: Path | None = None,
                 wait_s: float | None = None, via: str | None = None) -> UpdateOutcome:
        """Restore the config SD from a backup, reboot, and confirm the old identity is back.
        ``via`` (HUB-SD): the door to restore through; None takes the last install's."""
        board_id = session.candidate.board_id
        if via is None:
            via = str((self.records.get(board_id) or {}).get("via") or "")
        storage, controller = self._door(session, via), session.controller
        if storage is None or controller is None:
            raise UnavailableError("harness rollback", "needs the Debug USB (config SD + MCC)")
        if self.lease_check is not None:
            self.lease_check(session, "roll the harness back")
        record = self.records.get(board_id) or {}
        journal = Journal(self.state, board_id)
        chosen = str(backup_path) if backup_path else ""
        if not chosen:
            pending = storage.pending() or {}
            chosen = (pending.get("backup") or {}).get("path", "")
        if not chosen:
            chosen = ((journal.read() or {}).get("backup") or {}).get("path", "")
        if not chosen:
            chosen = (record.get("backup") or {}).get("path", "")
        if not chosen:
            raise RefusedError(f"no backup is recorded for {board_id}",
                               hint="pass --backup ZIP (a backup `harness-manager sd backup` made)")
        backup = storage.load_backup(Path(chosen))
        self._emit("update.started", board_id, version="rollback", mode="restore")
        storage.restore(backup, progress=self._progress(board_id, "restore:"))
        journal.clear()
        try:
            raw = controller.reboot(progress=self._progress(board_id, "reboot:"), wait_s=wait_s)
        except HarnessError as exc:
            out = UpdateOutcome(board_id, "rollback", RESULT_RESTORED_UNCONFIRMED,
                                f"the SD is restored from {backup.path}, but the reboot was not "
                                f"witnessed ({exc.message})",
                                backup={"path": backup.path, "sha256": backup.sha256})
            self._emit("update.done", board_id, version="rollback", result=out.result)
            return out
        evidence = _evidence(raw, controller)
        ident = self._identity(session)
        now = running_summary(ident)
        previous = record.get("previous") or {}
        confirmed = bool(ident and ident.shell_id) and (
            not previous or (_same_u32(now.get("shell_id", ""), previous.get("shell_id", "")) and
                             now.get("harness") == previous.get("harness") and
                             fw_sha_match(previous.get("firmware_sha", ""),
                                          now.get("firmware_sha", "")) is not False and
                             ver32_match(previous.get("ver32", ""),
                                         now.get("ver32", "")) is not False))
        result = RESULT_RESTORED if confirmed else RESULT_RESTORED_UNCONFIRMED
        detail = (f"restored {backup.path}; the board reports shell {now.get('shell_id') or '?'}, "
                  f"harness {now.get('harness') or '?'}")
        if not confirmed and previous:
            detail += (f" (expected shell {previous.get('shell_id')}, harness "
                       f"{previous.get('harness')}, firmware {previous.get('firmware_sha') or '?'})")
        out = UpdateOutcome(board_id, "rollback", result, detail, identity_after=now,
                            evidence=evidence, backup={"path": backup.path, "sha256": backup.sha256})
        # The history names releases (HARNESS-CAT): the backup holds what ran before the
        # last install (its ``from_version``); a second restore restores the same backup.
        undone = record.get("version", "")
        back_to = (undone if record.get("kind") == "restore"
                   else record.get("from_version", "")) or previous.get("harness", "")
        self.records.put(board_id, {"version": back_to, "result": result,
                                    "backup": out.backup, "previous": previous,
                                    "identity_after": now, "detail": detail,
                                    "kind": "restore", "from_version": undone,
                                    "static_id": previous.get("shell_id", ""),
                                    "fw_sha": previous.get("firmware_sha", ""),
                                    "doors": [TARGET_MCC_SD],
                                    **({"via": via} if via else {})})
        self._emit("update.done", board_id, version="rollback", result=result, detail=detail)
        return out
