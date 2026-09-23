"""Updates over the daemon API: check, install the harness, roll back, update the app (lane L4).

docs/API.md "Week-plan additions -> Power and update" (frozen):

| Route | Job |
|---|---|
| ``POST /update/check`` ``{board_id?, source?, channel?}`` | ``update_check`` (read-only) |
| ``POST /boards/{bid}/update/harness`` ``{fingerprint, rekey_phrase?}`` | ``update_harness`` |
| ``POST /boards/{bid}/update/rollback`` | ``update_rollback`` |
| ``POST /update/app`` ``{version?}`` | ``update_app`` |
| ``POST /update/app/rollback`` | ``update_app_rollback`` |

Everything goes through the engine's ``update`` service (T7's ``UpdateService``), read
with ``getattr(engine, "update", None)``. A stub with a ``reason`` (docs/CONTRACTS.md)
means updates are unavailable in this build: every route answers 422 UNAVAILABLE
with that reason, before any job.

Consent is code, the same rules as the CLI (``cli/cmd_update.py``):

- **The plan the user saw is the plan that runs.** ``update_check`` returns the
  board's plan with its ``fingerprint``. ``update/harness`` recomputes the plan (from
  the channel and source that check used) and refuses with 409 REFUSED, before any
  job, when the fingerprint differs: the board or the channel changed since.
- **A re-key needs the typed phrase.** ``rekey_phrase`` must equal the plan's
  ``consent_phrase`` (``REKEY <static_id>``). Nothing implies it: there is no "yes"
  that re-keys a board.
- **Blockers refuse** (409 REFUSED with ``error.data.plan``); nothing is written.
- **"Installed" only when the board says so.** A ``written-not-running`` outcome
  FAILS the job (exit 6, as the CLI), with the outcome in ``error.data.outcome`` and
  the restore in the hint; a rollback that is not confirmed fails the same way.

The app routes (``update_app``, ``update_app_rollback``) are engine-wide jobs (board id
``""``). They are refused with 409 HELD naming the job while ANY job runs. T7's own
switch rail still applies inside the job: the new version is staged, but the
launcher is switched only when no board session is held (this service holds the
boards it has open), so the job fails HELD with "close those sessions first"; the
staged version stays for the next try.

Events: the service publishes ``update.*`` on the engine bus and the events socket
forwards them as they are. For board jobs, ``update.progress`` also drives
``job.progress`` ``{phase, done, total}``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from harness_manager.cli.output import with_data
from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event

from .app import JsonBody, RouteContext, _abs_path, _number, _obj, _str

#: The board id of an engine-wide job (a check with no board, the app updates).
ENGINE = ""
#: Outcome results (docs/CONTRACTS.md ``update.done``) that fail the job.
RESULT_WRITTEN = "written-not-running"
RESULT_RESTORED = "restored"
#: How many checked plans the daemon remembers: (board, fingerprint) -> (channel, source).
REMEMBER = 64


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise UsageError(f"{key} must be a non-empty string, not {value!r}")
    return value


def plan_json(plan: Any) -> dict[str, Any]:
    """``Plan.summary()`` plus the ``fingerprint`` an approval is bound to."""
    return {**plan.summary(), "fingerprint": plan.fingerprint()}


def releases_json(channel: Any) -> dict[str, list[dict[str, Any]]]:
    """The channel's releases, newest as listed, with the one it calls current marked."""
    return {
        "harness": [{"version": r.version, "status": r.status, "static_id": r.identity.static_id,
                     "harness": r.identity.harness, "impl": r.identity.impl, "rekey": r.rekey,
                     "released_at": r.released_at, "notes_url": r.notes_url,
                     "current": r.version == channel.harness_current}
                    for r in channel.harness],
        "app": [{"version": r.version, "status": r.status, "released_at": r.released_at,
                 "notes_url": r.notes_url, "current": r.version == channel.app_current}
                for r in channel.app],
    }


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    mu = threading.Lock()
    # Where each checked plan came from, so update/harness recomputes it from the SAME
    # channel and source: by (board, fingerprint), and each board's last check (for a
    # fingerprint the daemon never issued: it is then compared with that board's channel).
    checked: OrderedDict[tuple[str, str], tuple[str | None, str | None]] = OrderedDict()
    last_check: dict[str, tuple[str | None, str | None]] = {}

    # -- plumbing ------------------------------------------------------------------------------

    def service() -> Any:
        """The engine's update service, or 422 with the reason it is missing."""
        svc = getattr(d.engine, "update", None)
        if svc is None:
            raise UnavailableError("update", "this engine has no update service")
        reason = getattr(svc, "reason", None)
        if reason is not None:
            raise UnavailableError("update", str(reason))
        return svc

    def remember(board_id: str, fingerprint: str, channel: str | None,
                 source: str | None) -> None:
        with mu:
            checked[(board_id, fingerprint)] = (channel, source)
            checked.move_to_end((board_id, fingerprint))
            while len(checked) > REMEMBER:
                checked.popitem(last=False)
            last_check[board_id] = (channel, source)

    def recall(board_id: str, fingerprint: str) -> tuple[str | None, str | None]:
        """The channel and source to plan from: that check's, else the board's last check's,
        else the defaults (``$HARNESS_MANAGER_UPDATE_CHANNEL``/``_SOURCE``, then GitHub)."""
        with mu:
            return checked.get((board_id, fingerprint)) or last_check.get(board_id) or (None, None)

    def refuse_while_jobs(what: str) -> None:
        running = d.jobs.running()
        if not running:
            return
        job = running[0]
        where = f"on {job.board_id}" if job.board_id else "in the service"
        err = HeldError(f"cannot {what} while {job.describe()} runs {where}",
                        holder=f"harness-manager-daemon {job.describe()}",
                        hint=f"wait for it to finish (GET /api/v1/jobs/{job.id}), then try again")
        err.data = {"job": job.id, "kind": job.kind, "board_id": job.board_id,  # type: ignore[attr-defined]
                    "jobs": [{"job": j.id, "kind": j.kind, "board_id": j.board_id}
                             for j in running]}
        raise err

    def submit_engine_wide(kind: str, fn: Callable[[Callable[[str, int, int], None]], Any]) -> Any:
        other = d.gates.busy(ENGINE)
        if other is not None:
            err = HeldError(f"{other.describe()} is running in the service",
                            holder=f"harness-manager-daemon {other.describe()}",
                            hint=f"wait for it to finish (GET /api/v1/jobs/{other.id})")
            err.data = {"job": other.id, "kind": other.kind, "board_id": ENGINE}  # type: ignore[attr-defined]
            raise err
        return ctx.accepted(d.jobs.submit(kind, ENGINE, fn))

    def forwarding(bid: str, progress: Callable[[str, int, int], None]) -> Callable[[], None]:
        """``update.progress`` for this board -> the job's progress. Returns the unsubscribe."""
        def on_progress(ev: Event) -> None:
            if ev.board_id == bid:
                progress(str(ev.data.get("phase", "")), int(ev.data.get("bytes", 0) or 0),
                         int(ev.data.get("total", 0) or 0))

        return d.bus.subscribe("update.progress", on_progress)

    # -- check (read-only) -----------------------------------------------------------------------

    @api.post("/update/check")
    def update_check(body: JsonBody = None) -> Any:
        b = _obj(body)
        board_id = _opt_str(b, "board_id")
        channel, source = _opt_str(b, "channel"), _opt_str(b, "source")
        svc = service()
        session = ctx.board(board_id) if board_id else None

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("check", 0, 0)
            report = svc.check(channel=channel, source=source, session=session)
            plan = report.get("plan")
            need_fingerprint = session is not None and not (plan and "fingerprint" in plan)
            if need_fingerprint or "releases" not in report:
                # CCR L4-1: until check() returns them, plan once more for the fingerprint
                # (and the release list), from the same channel and source.
                progress("plan", 0, 0)
                if need_fingerprint:
                    p, verified = svc.plan_harness(session, channel=channel, source=source)
                    report["plan"] = plan = plan_json(p)
                else:
                    verified = svc.fetch_channel(channel, source)
                report.setdefault("releases", releases_json(verified.channel))
            if session is not None and plan is not None:
                remember(board_id or "", plan["fingerprint"], channel, source)
            return report

        if session is not None:
            return ctx.accepted(d.jobs.submit("update_check", board_id or "", run))
        return submit_engine_wide("update_check", run)

    # -- the harness ------------------------------------------------------------------------------

    @api.post("/boards/{bid:path}/update/harness")
    def update_harness(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        fingerprint = _str(b, "fingerprint")
        phrase = b.get("rekey_phrase", "")
        if phrase is None:
            phrase = ""
        if not isinstance(phrase, str):
            raise UsageError(f"rekey_phrase must be a string, not {phrase!r}")
        svc = service()
        remembered = recall(bid, fingerprint)
        channel = _opt_str(b, "channel") or remembered[0]
        source = _opt_str(b, "source") or remembered[1]
        with d.gates.op(bid):
            plan, verified = svc.plan_harness(s, channel=channel, source=source)
        summary = plan_json(plan)
        if summary["fingerprint"] != fingerprint:
            raise with_data(RefusedError(
                f"the update plan for {bid} changed since it was checked; nothing was installed",
                hint="check again (POST /api/v1/update/check) and confirm the new plan"),
                plan=summary)
        if plan.blockers:
            raise with_data(RefusedError(f"cannot update {bid}: {'; '.join(plan.blockers)}",
                                         hint="fix the blockers, then check again"), plan=summary)
        try:
            approval = plan.approve(consent=phrase, by="harness-manager-daemon")
        except RefusedError as exc:          # a re-key without the exact typed phrase
            raise with_data(exc, plan=summary) from None

        def run(progress: Callable[[str, int, int], None]) -> Any:
            unsubscribe = forwarding(bid, progress)
            try:
                out = svc.install_harness(s, plan, approval, verified)
            finally:
                unsubscribe()
            data = out.as_dict()
            if out.result == RESULT_WRITTEN:
                backup = (out.backup or {}).get("path", "")
                raise with_data(ActionFailedError(
                    out.detail, hint=f"roll {bid} back (POST .../boards/ID/update/rollback) to "
                                     f"restore {'the backup ' + backup if backup else 'its SD'}"),
                    outcome=data)
            return data

        return ctx.accepted(d.jobs.submit("update_harness", bid, run))

    @api.post("/boards/{bid:path}/update/rollback")
    def update_rollback(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        backup = _abs_path(b["backup_path"], "backup_path") if b.get("backup_path") else None
        if backup is not None and not backup.is_file():
            raise AbsentError(f"no backup at {backup}",
                              hint="give a backup zip that exists, or none for the last one")
        wait_s = _number(b, "wait_s") if b.get("wait_s") is not None else None
        if wait_s is not None and wait_s <= 0:
            raise UsageError("wait_s must be positive")
        svc = service()
        with d.gates.op(bid):
            ctx.require(s, "storage", C.STORAGE_INSTALL)       # 422: needs the Debug USB
            ctx.require(s, "controller", C.REBOOT_BOARD)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            unsubscribe = forwarding(bid, progress)
            try:
                out = svc.rollback_harness(s, backup_path=backup, wait_s=wait_s)
            finally:
                unsubscribe()
            data = out.as_dict()
            if out.result != RESULT_RESTORED:
                raise with_data(ActionFailedError(
                    out.detail, hint="the SD is restored; check the board (GET .../boards/ID), "
                                     "and power-cycle it if it is dark"), outcome=data)
            return data

        return ctx.accepted(d.jobs.submit("update_rollback", bid, run))

    # -- the app ------------------------------------------------------------------------------------

    @api.post("/update/app")
    def update_app(body: JsonBody = None) -> Any:
        b = _obj(body)
        version = _opt_str(b, "version")
        channel, source = _opt_str(b, "channel"), _opt_str(b, "source")
        svc = service()
        refuse_while_jobs("update the app")

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("stage", 0, 0)
            return svc.update_app(channel=channel, source=source, version=version)

        return submit_engine_wide("update_app", run)

    @api.post("/update/app/rollback")
    def update_app_rollback(body: JsonBody = None) -> Any:
        _obj(body)
        svc = service()
        refuse_while_jobs("roll the app back")

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("switch", 0, 0)
            state = svc.app().rollback()
            return {"target": "app", "result": "switched", "state": state}

        return submit_engine_wide("update_app_rollback", run)
