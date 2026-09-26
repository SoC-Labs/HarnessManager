"""Harness versions over the daemon API (lane HARNESS-CAT, H8; docs/API.md "Harness versions").

| Route | Does |
|---|---|
| ``GET /harness/catalog?board_id=&channel=`` | the catalogue the last refresh built for that board (``""``: none), with verdicts; 409 REFUSED "refresh first" when there is none |
| ``POST /harness/catalog/refresh`` ``{board_id?, channel?, all?, source?}`` | 202 job ``harness_refresh``: fetch + verify the channels, one plan per release; ``harness.catalog`` |
| ``GET /harness/releases/{version}?board_id=`` | one release from the cached catalogue; with ``board_id`` its plan and ``fingerprint`` (reads the board's identity) |
| ``POST /harness/releases/{version}/fetch`` ``{channel?, source?, kit?}`` | 202 job ``harness_fetch`` (engine-wide): download + verify into the cache |
| ``POST /boards/{bid}/harness/install`` ``{fingerprint, version?, rekey_phrase?, channel?, source?, overlays_only?, via?, board_phrase?, auto_revert?}`` | 202 job ``harness_install`` |
| ``PUT /boards/{bid}/harness/pin`` ``{version}`` · ``DELETE /boards/{bid}/harness/pin`` | the board's pin; ``harness.pinned`` |
| ``GET /boards/{bid}/harness/history?limit=`` | ``{board_id, history, pinned, rollback}`` |
| ``POST /boards/{bid}/harness/rollback`` ``{to?, fingerprint?, rekey_phrase?, channel?, source?}`` or ``{backup_path, wait_s?}`` | 202 job ``harness_rollback`` |

The rules are ``update_api``'s (``cmd_update``'s), plus the lease:

- **The plan the user saw is the plan that runs.** ``install`` recomputes the plan for the
  requested ``version`` (from the channel and source the fingerprint came from: a
  ``GET /harness/releases/{v}`` with ``board_id``, else the board's last refresh) and
  refuses with 409 REFUSED, before any job, when the fingerprint differs.
- **The lease** (HARNESS-DIST §2): a plan that writes or reboots a board behind a hub is
  refused with 409 HELD, naming the holder, unless this client holds the lease. A board
  with no hub has no lease. The executor checks it again inside the job.
- **A re-key needs the typed phrase** ``rekey_phrase == "REKEY <static_id>"``; blockers
  refuse (409 REFUSED with ``error.data.plan``).
- **"Installed" only when the board says so**: ``written-not-running`` fails the job (6).
- **Rollback** re-installs the release the last install replaced (``to: "previous"``) or a
  named one, through the same plan, fingerprint and consent: without the fingerprint (or
  with another) it answers 409 REFUSED with ``error.data.plan`` to confirm. With
  ``backup_path`` it restores that SD backup (T7's rollback).

Events: ``harness.catalog``, ``harness.installing``, ``harness.installed``,
``harness.pinned`` (docs/CONTRACTS.md), plus the ``update.*`` the executor publishes;
``update.progress`` for this board drives ``job.progress``.
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

from .app import JsonBody, RouteContext, _abs_path, _bool, _number, _obj

ENGINE = ""
RESULT_WRITTEN = "written-not-running"
RESULT_RESTORED = "restored"
#: HUB-SD (U10): a remote install that left the board dark fails its job too, loudly.
RESULT_DARK = ("dark", "auto-reverted", "auto-revert-failed")
VIAS = ("hub", "usb")
CHANNELS = ("stable", "beta", "dev")
REMEMBER = 64


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise UsageError(f"{key} must be a non-empty string, not {value!r}")
    return value


def plan_json(plan: Any) -> dict[str, Any]:
    return {**plan.summary(), "fingerprint": plan.fingerprint()}


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    mu = threading.Lock()
    state: dict[str, Any] = {"catalog": None}
    listings: dict[str, Any] = {}                    # board id ("" = none) -> Listing
    sources: dict[str, str | None] = {}              # board id -> its last refresh's source
    Where = tuple[str | None, str | None, str | None]       # channel, source, version
    planned: OrderedDict[tuple[str, str], Where] = OrderedDict()

    # -- plumbing ------------------------------------------------------------------------------

    def catalog() -> Any:
        """The catalogue on the engine's update service, or 422 with why it is missing."""
        svc = getattr(d.engine, "update", None)
        if svc is None:
            raise UnavailableError("update", "this engine has no update service")
        reason = getattr(svc, "reason", None)
        if reason is not None:
            raise UnavailableError("update", str(reason))
        # One lease cache and one view of "mine" with hub_api (as xvc_api does).
        if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
            svc.leases = d.leases
        with mu:
            cat = state["catalog"]
            if cat is None or cat.update is not svc:
                from harness_manager.services.harness_catalog import HarnessCatalog

                cat = state["catalog"] = HarnessCatalog(svc)
            return cat

    def remember(bid: str, fingerprint: str, where: Where) -> None:
        with mu:
            planned[(bid, fingerprint)] = where
            planned.move_to_end((bid, fingerprint))
            while len(planned) > REMEMBER:
                planned.popitem(last=False)

    def recall(bid: str, fingerprint: str) -> Where:
        with mu:
            return planned.get((bid, fingerprint)) or (None, sources.get(bid), None)

    def cached(bid: str) -> Any:
        with mu:
            listing = listings.get(bid)
        if listing is None:
            raise RefusedError(f"no harness catalogue is cached for {bid or 'the service'}",
                               hint="refresh it first: POST /api/v1/harness/catalog/refresh")
        return listing

    def forwarding(bid: str, progress: Callable[[str, int, int], None]) -> Callable[[], None]:
        def on_progress(ev: Event) -> None:
            if ev.board_id == bid:
                progress(str(ev.data.get("phase", "")), int(ev.data.get("bytes", 0) or 0),
                         int(ev.data.get("total", 0) or 0))

        return d.bus.subscribe("update.progress", on_progress)

    def engine_wide(kind: str, fn: Callable[[Callable[[str, int, int], None]], Any]) -> Any:
        other = d.gates.busy(ENGINE)
        if other is not None:
            err = HeldError(f"{other.describe()} is running in the service",
                            holder=f"harness-manager-daemon {other.describe()}",
                            hint=f"wait for it to finish (GET /api/v1/jobs/{other.id})")
            err.data = {"job": other.id, "kind": other.kind, "board_id": ENGINE}  # type: ignore[attr-defined]
            raise err
        return ctx.accepted(d.jobs.submit(kind, ENGINE, fn))

    def channels_of(b: dict[str, Any]) -> list[str]:
        if _bool(b, "all", False):
            return list(CHANNELS)
        ch = _opt_str(b, "channel")
        return [ch] if ch else ["stable"]

    def consent(b: dict[str, Any]) -> str:
        phrase = b.get("rekey_phrase", "")
        if phrase is None:
            return ""
        if not isinstance(phrase, str):
            raise UsageError(f"rekey_phrase must be a string, not {phrase!r}")
        return phrase

    def door_args(b: dict[str, Any]) -> tuple[str | None, str, bool | None]:
        """HUB-SD: ``via`` (hub | usb), the typed ``board_phrase``, ``auto_revert``."""
        via = _opt_str(b, "via")
        if via is not None and via not in VIAS:
            raise UsageError(f"via must be one of {', '.join(VIAS)}, not {via!r}")
        bp = b.get("board_phrase", "")
        if bp is not None and not isinstance(bp, str):
            raise UsageError(f"board_phrase must be a string, not {bp!r}")
        ar = b.get("auto_revert")
        if ar is not None and not isinstance(ar, bool):
            raise UsageError(f"auto_revert must be true or false, not {ar!r}")
        return via, bp or "", ar

    def approve_or_refuse(bid: str, s: Any, cat: Any, plan: Any, fingerprint: str | None,
                          phrase: str, board_phrase: str = "",
                          auto_revert: bool | None = None) -> Any:
        """The daemon's consent rules, before any job (see the module doc)."""
        summary = plan_json(plan)
        cat.check_lease(s, plan)                     # 409 HELD, naming the holder
        if fingerprint is None or summary["fingerprint"] != fingerprint:
            what = ("confirm this plan: send its fingerprint" if fingerprint is None else
                    f"the plan for {bid} changed since it was shown; nothing was installed")
            raise with_data(RefusedError(what, hint="review error.data.plan, then post again "
                                                    "with its fingerprint"), plan=summary)
        if plan.blockers:
            raise with_data(RefusedError(f"cannot install on {bid}: {'; '.join(plan.blockers)}",
                                         hint="fix the blockers, then refresh"), plan=summary)
        if plan.up_to_date:
            raise with_data(RefusedError(f"{bid} already runs harness {plan.version}",
                                         hint="nothing to do"), plan=summary)
        try:
            return plan.approve(consent=phrase, by="harness-manager-daemon",
                                board_phrase=board_phrase, auto_revert=auto_revert)
        except RefusedError as exc:             # a re-key without the exact typed phrase
            raise with_data(exc, plan=summary) from None

    def install_job(kind: str, bid: str, s: Any, cat: Any, plan: Any, approval: Any,
                    verified: Any) -> Any:
        def run(progress: Callable[[str, int, int], None]) -> Any:
            unsubscribe = forwarding(bid, progress)
            try:
                out = cat.install(s, plan, approval, verified, by="harness-manager-daemon")
            finally:
                unsubscribe()
            data = out.as_dict()
            if out.result in RESULT_DARK:
                raise with_data(ActionFailedError(
                    out.detail, hint=out.restore_hint.replace("TARGET", bid) or
                    "check the board"), outcome=data)
            if out.result == RESULT_WRITTEN:
                backup = (out.backup or {}).get("path", "")
                raise with_data(ActionFailedError(
                    out.detail, hint=f"roll {bid} back (POST .../boards/ID/harness/rollback "
                                     f"{{backup_path}}) to restore "
                                     f"{'the backup ' + backup if backup else 'its SD'}"),
                    outcome=data)
            return data

        return ctx.accepted(d.jobs.submit(kind, bid, run))

    # -- the catalogue ---------------------------------------------------------------------------

    @api.get("/harness/catalog")
    def harness_catalog(board_id: str = "", channel: str = "") -> Any:
        catalog()                                     # 422 when updates are unavailable
        listing = cached(board_id)
        data = listing.as_dict()
        if channel:
            if channel not in [c["channel"] for c in data["channels"]]:
                raise RefusedError(f"the cached catalogue for {board_id or 'the service'} has no "
                                   f"{channel!r} channel",
                                   hint="refresh it with that channel first")
            data["releases"] = [r for r in data["releases"] if channel in r["channels"]]
        return {"ok": True, **data}

    @api.post("/harness/catalog/refresh")
    def harness_refresh(body: JsonBody = None) -> Any:
        b = _obj(body)
        board_id = _opt_str(b, "board_id") or ""
        source = _opt_str(b, "source")
        chans = channels_of(b)
        cat = catalog()
        session = ctx.board(board_id) if board_id else None

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("channels", 0, len(chans))
            listing = cat.list(session, channels=chans, source=source)
            with mu:
                listings[board_id] = listing
                sources[board_id] = source
            return listing.as_dict()

        if session is not None:
            return ctx.accepted(d.jobs.submit("harness_refresh", board_id, run))
        return engine_wide("harness_refresh", run)

    @api.get("/harness/releases/{version}")
    def harness_show(version: str, board_id: str = "") -> Any:
        cat = catalog()
        listing = cached(board_id)
        if not board_id:
            pack = listing.catalog.removesuffix("-harness") or "mps3"
            return {"ok": True, **cat.show(version, None, verified=listing.verified, pack=pack)}
        s = ctx.board(board_id)
        with d.gates.op(board_id):
            out = cat.show(version, s, verified=listing.verified)
        if out.get("plan"):
            remember(board_id, out["plan"]["fingerprint"],
                     (out["channel"], sources.get(board_id), version))
        return {"ok": True, **out}

    @api.post("/harness/releases/{version}/fetch")
    def harness_fetch(version: str, body: JsonBody = None) -> Any:
        b = _obj(body)
        channel, source = _opt_str(b, "channel"), _opt_str(b, "source")
        kit = _bool(b, "kit", False)
        cat = catalog()

        def run(progress: Callable[[str, int, int], None]) -> Any:
            def both(phase: str, done: int, total: int) -> None:
                progress(phase, done, total)
                d.bus.publish(Event("update.progress", ENGINE,
                                    {"phase": phase, "bytes": done, "total": total}))

            return cat.fetch(version, channel=channel, source=source, kit=kit, progress=both)

        return engine_wide("harness_fetch", run)

    # -- a board's harness -----------------------------------------------------------------------

    @api.post("/boards/{bid:path}/harness/install")
    def harness_install(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        fingerprint = _opt_str(b, "fingerprint")
        if fingerprint is None:
            raise UsageError("the request needs 'fingerprint' (from GET /harness/releases/"
                             "{version}?board_id=)")
        phrase = consent(b)
        via, board_phrase, auto_revert = door_args(b)
        overlays_only = _bool(b, "overlays_only", False)
        where = recall(bid, fingerprint)
        channel = _opt_str(b, "channel") or where[0]
        source = _opt_str(b, "source") or where[1]
        version = _opt_str(b, "version") or where[2]
        cat = catalog()
        with d.gates.op(bid):
            verified = (cat.locate(version, channel=channel, source=source,
                                   pack=s.candidate.pack) if version else None)
            plan, verified = cat.plan(s, version, channel=channel, source=source,
                                      overlays_only=overlays_only, verified=verified, via=via)
            approval = approve_or_refuse(bid, s, cat, plan, fingerprint, phrase, board_phrase,
                                         auto_revert)
        return install_job("harness_install", bid, s, cat, plan, approval, verified)

    @api.put("/boards/{bid:path}/harness/pin")
    def harness_pin(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        version = _opt_str(b, "version")
        if version is None:
            raise UsageError("the request needs 'version'")
        cat = catalog()
        with mu:
            listing = listings.get(bid) or listings.get(ENGINE)
        out = cat.pin(bid, version, pack=s.candidate.pack, by="harness-manager-daemon",
                      verified=listing.verified if listing is not None else None)
        return {"ok": True, **out}

    @api.delete("/boards/{bid:path}/harness/pin")
    def harness_unpin(bid: str) -> Any:
        ctx.board(bid)
        return {"ok": True, **catalog().unpin(bid, by="harness-manager-daemon")}

    @api.get("/boards/{bid:path}/harness/history")
    def harness_history(bid: str, limit: int | None = None) -> Any:
        ctx.board(bid)
        if limit is not None and limit < 1:
            raise UsageError("limit must be at least 1")
        cat = catalog()
        pin = cat.update.pins().get(bid)
        with mu:
            listing = listings.get(bid)
        return {"ok": True, "board_id": bid, "history": cat.history(bid, limit),
                "pinned": pin["version"] if pin else "",
                "rollback": listing.rollback if listing is not None else None}

    @api.post("/boards/{bid:path}/harness/rollback")
    def harness_rollback(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        cat = catalog()
        if b.get("backup_path") is not None:
            if b.get("to") is not None:
                raise UsageError("give either to or backup_path, not both")
            backup = _abs_path(b["backup_path"], "backup_path")
            if not backup.is_file():
                raise AbsentError(f"no backup at {backup}", hint="give a backup zip that exists")
            wait_s = _number(b, "wait_s") if b.get("wait_s") is not None else None
            if wait_s is not None and wait_s <= 0:
                raise UsageError("wait_s must be positive")
            via_b = _opt_str(b, "via")
            with d.gates.op(bid):
                if via_b != "hub":
                    ctx.require(s, "storage", C.STORAGE_INSTALL)  # 422: needs the Debug USB
                ctx.require(s, "controller", C.REBOOT_BOARD)
                cat.update.check_lease(s, "roll the harness back")

            def run(progress: Callable[[str, int, int], None]) -> Any:
                unsubscribe = forwarding(bid, progress)
                try:
                    out = cat.update.rollback_harness(s, backup_path=backup, wait_s=wait_s,
                                                      **({"via": via_b} if via_b else {}))
                finally:
                    unsubscribe()
                data = {**out.as_dict(), "how": "restore"}
                if out.result != RESULT_RESTORED:
                    raise with_data(ActionFailedError(
                        out.detail, hint="the SD is restored; check the board, and power-cycle "
                                         "it if it is dark"), outcome=data)
                return data

            return ctx.accepted(d.jobs.submit("harness_rollback", bid, run))
        to = _opt_str(b, "to") or "previous"
        fingerprint = _opt_str(b, "fingerprint")
        phrase = consent(b)
        via, board_phrase, auto_revert = door_args(b)
        source = _opt_str(b, "source") or sources.get(bid)
        channel = _opt_str(b, "channel")
        with d.gates.op(bid):
            listing = cat.list(s, channels=[channel] if channel else CHANNELS, source=source)
            target = cat.rollback_target(bid, to, listing)
            name = listing.channel_of(target)
            if not name:
                raise AbsentError(f"no channel lists harness {target}",
                                  hint="restore an SD backup instead ({backup_path})")
            plan, verified = cat.plan(s, target, verified=listing.verified[name], via=via)
            approval = approve_or_refuse(bid, s, cat, plan, fingerprint, phrase, board_phrase,
                                         auto_revert)
        return install_job("harness_rollback", bid, s, cat, plan, approval, verified)
