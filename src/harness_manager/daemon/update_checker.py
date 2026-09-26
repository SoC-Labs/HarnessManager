"""The daemon's periodic app-update checker (lane OTA-D; docs/design/HM_SELF_UPDATE.md §5.1).

- **When:** the first check ``FIRST_CHECK_S`` (60 s) after the daemon starts, then every
  ``check_interval`` (the admin policy's, default 6 h) with ±10 % jitter. A failed check
  backs off exponentially from 5 minutes up to 24 h; a success returns to the interval.
- **Offline tolerance:** an unreachable or missing source is logged once per outage and
  recorded in ``last_check.json`` (``error_kind: offline``); nothing is published. A bad
  signature or a serial rollback is logged as a warning every time (``error_kind: refused``).
- **What it reads:** the ``hm-app`` catalogue (OTA-C), never the harness catalogues (lane H6).
  The offer is OTA-C's ``appstage.offer_app``: a version marked bad is never offered.
- **What it publishes:** ``update.available {channel, serial, app, notes, staged, harness,
  source: "checker"}``, only when the offer CHANGES (a new version, or it became staged),
  never on every check. OTA-C's ``stage_app`` adds ``update.app.staged`` when it stages.
- **Policy (U3, U6):** the effective mode is the stricter of the admin policy and the user's
  ``auto`` setting (``selfupdate.effective``). ``stage`` (the default): the offer is staged
  in the background by OTA-C's ``appstage.stage_app`` inside an ``update_stage`` job
  (engine-wide, so the gates and a drain apply; a busy service defers it to a retry in 10
  minutes). ``notify``: published only. ``off``, and every developer install: the checker
  does nothing at all (no fetch, no event).
- **It never applies anything.** Applying is ``POST /update/app/apply``, on a click.

``HARNESS_MANAGER_UPDATE_FIRST_CHECK_S`` moves the first check (tests, the spike).

**A settings change applies at once** (lane SET-WIRE; SETTINGS.md §8): once started, the
checker follows ``settings.changed``, and a change to a key it reads (``WAKE_KEYS``: the
channel, the mode, the interval, the source, the mirrors, the token) checks now under the
new settings instead of at the next tick, at most once per ``WAKE_MIN_S``.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    RefusedError,
    UnreachableError,
)
from harness_manager.core.events import Event
from harness_manager.services.update import selfupdate as su

log = logging.getLogger(__name__)

FIRST_CHECK_ENV = "HARNESS_MANAGER_UPDATE_FIRST_CHECK_S"
FIRST_CHECK_S = 60.0
JITTER = 0.10
MIN_BACKOFF_S = 300.0
MAX_BACKOFF_S = 24 * 3600.0
DEFERRED_RETRY_S = 600.0
STAGE_KIND = "update_stage"
#: The settings a check reads: a change to one checks again at once.
WAKE_KEYS = frozenset({"updates.channel", "updates.auto", "updates.check_interval",
                       "updates.source", "updates.mirrors", "updates.github_token"})
#: The least time between two checks a settings change starts (s).
WAKE_MIN_S = 5.0


def first_check_s() -> float:
    raw = os.environ.get(FIRST_CHECK_ENV, "").strip()
    try:
        return max(0.0, float(raw)) if raw else FIRST_CHECK_S
    except ValueError:
        return FIRST_CHECK_S


def error_kind(exc: HarnessError) -> str:
    """``offline`` (retry quietly), ``refused`` (a trust failure: loud) or ``failed``."""
    if isinstance(exc, (UnreachableError, AbsentError)):
        return "offline"
    if isinstance(exc, RefusedError):
        return "refused"
    if isinstance(exc, ActionFailedError) and any(w in exc.message.lower() for w in (
            "timed out", "connection", "unreachable", "resolve", "network")):
        return "offline"
    return "failed"


class UpdateChecker:
    """``tick()`` is one check; ``start()`` runs it on a timer in a daemon thread."""

    def __init__(self, service: Callable[[], Any], bus: Any, state_dir: Path, *,
                 submit: Callable[[str, Callable[[Any], Any]], Any] | None = None,
                 first_delay_s: float | None = None, jitter: float = JITTER,
                 rng: Callable[[], float] = random.random, now: Callable[[], float] = time.time,
                 min_backoff_s: float = MIN_BACKOFF_S, max_backoff_s: float = MAX_BACKOFF_S) -> None:
        self._service = service
        self.bus = bus
        self.state_dir = Path(state_dir)
        self.submit = submit
        self.first_delay_s = first_check_s() if first_delay_s is None else first_delay_s
        self.jitter = jitter
        self.rng = rng
        self.now = now
        self.min_backoff_s = min_backoff_s
        self.max_backoff_s = max_backoff_s
        self.backoff_s = 0.0
        self.next_in_s: float | None = None
        self._offline_logged = False
        self._stop = threading.Event()
        self._kick = threading.Event()       # stop, or a settings change: look again now
        self._thread: threading.Thread | None = None
        self._unsub: Callable[[], None] | None = None
        self._mu = threading.RLock()        # a job may run its stage on this thread

    # -- the timer --

    def start(self) -> UpdateChecker:
        if self._thread is None:
            if self.bus is not None and hasattr(self.bus, "subscribe"):
                self._unsub = self.bus.subscribe("settings.changed", self._on_settings)
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="harness-manager-daemon-update-check")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _on_settings(self, event: Any) -> None:
        keys = set((getattr(event, "data", None) or {}).get("keys") or ())
        if keys & WAKE_KEYS:
            self._kick.set()

    def _loop(self) -> None:
        delay = self.first_delay_s
        last = 0.0
        while True:
            self._kick.wait(delay)
            if self._stop.is_set():
                return
            if self._kick.is_set():              # a settings change: not too often
                self._kick.clear()
                wait = WAKE_MIN_S - (time.monotonic() - last)
                if wait > 0 and self._stop.wait(wait):
                    return
            last = time.monotonic()
            rec = self.tick()
            delay = self.delay_after(rec)
            self.next_in_s = delay

    def delay_after(self, rec: dict[str, Any]) -> float:
        """Seconds to the next check after ``rec`` (the interval, a backoff, or a retry)."""
        if rec.get("error"):
            self.backoff_s = min(self.max_backoff_s, max(self.min_backoff_s, self.backoff_s * 2))
            return self.backoff_s
        self.backoff_s = 0.0
        interval = float(rec.get("interval_s") or su.effective(None, su.Settings())["check_interval_s"])
        if interval <= 0:                          # the policy said "never check"
            return 24 * 3600.0
        base = interval * (1 + self.jitter * (2 * self.rng() - 1))
        if rec.get("stage_deferred"):
            return min(base, DEFERRED_RETRY_S)
        return base

    # -- one check --

    def tick(self) -> dict[str, Any]:
        """One check. Never raises: the outcome is the returned (and recorded) record."""
        with self._mu:
            try:
                return self._tick()
            except Exception as exc:  # noqa: BLE001 - the timer must go on
                log.exception("the update check failed")
                return {"at": self.now(), "error": f"internal error: {exc}", "error_kind": "failed"}

    def _tick(self) -> dict[str, Any]:
        svc = self._service()
        if svc is None or getattr(svc, "reason", None) is not None:
            return {"skipped": "no update service", "interval_s": 0}
        app = svc.app()
        eff = su.effective(svc.policy, su.load_settings(self.state_dir), blocked=app.dev_install)
        if eff["auto"] == "off":
            return {"skipped": eff["why"] or "self-update is off", "mode": "off",
                    "interval_s": eff["check_interval_s"]}
        if eff["check_interval_s"] <= 0:
            return {"skipped": "the policy's check_interval is 0 (never)", "interval_s": 0}
        last = su.read_json(su.last_check_path(self.state_dir)) or {}
        rec: dict[str, Any] = {"at": self.now(), "mode": eff["auto"],
                               "interval_s": eff["check_interval_s"], "error": "",
                               "announced": last.get("announced")}
        from harness_manager.services.update.appstage import offer_app
        from harness_manager.services.update.schema import CATALOG_APP

        try:
            verified = svc.fetch_channel(eff["channel"] or None, None, catalog=CATALOG_APP)
        except HarnessError as exc:
            kind = error_kind(exc)
            rec.update(error=exc.message, error_kind=kind)
            if kind == "offline":
                if not self._offline_logged:
                    log.info("update check: the source is unreachable (%s); retrying quietly",
                             exc.message)
                    self._offline_logged = True
            else:
                log.warning("update check refused the channel: %s", exc.message)
            su.write_json(su.last_check_path(self.state_dir), rec)
            return rec
        self._offline_logged = False
        ch = verified.channel
        verdict = offer_app(ch, svc.app_version, svc.state, app=app)
        offer = verdict.release
        rec.update(channel=ch.channel, serial=ch.serial, source=verified.url,
                   catalog=CATALOG_APP, available=offer.version if offer else "")
        if verdict.skipped_bad:
            rec["skipped_bad"] = {"version": verdict.skipped_bad, "why": verdict.why}
        if offer is None:
            su.write_json(su.last_check_path(self.state_dir), rec)
            return rec
        staged = self._staged(app, offer.version)
        if eff["auto"] == "stage" and not staged:
            staged = self._stage(svc, verified, offer, ch, rec)
        rec["staged"] = staged
        self._announce(ch, offer, staged, rec)
        su.write_json(su.last_check_path(self.state_dir), rec)
        return rec

    @staticmethod
    def _staged(app: Any, version: str) -> bool:
        from harness_manager.services.update.app import STATE_STAGED

        info = app.state()["versions"].get(version) or {}
        return info.get("state") == STATE_STAGED and \
            app.layout.python(version, windows=app.windows).exists()

    def _announce(self, ch: Any, offer: Any, staged: bool, rec: dict[str, Any]) -> None:
        key = [offer.version, bool(staged)]
        if rec.get("announced") == key:
            return
        notes = getattr(offer, "notes", "") or (getattr(offer, "extra", None) or {}).get("notes", "")
        if self.bus is not None:
            self.bus.publish(Event("update.available", "", {
                "channel": ch.channel, "serial": ch.serial, "harness": ch.harness_current,
                "app": offer.version, "notes": notes, "notes_url": offer.notes_url,
                "staged": bool(staged), "source": "checker"}))
        rec["announced"] = key

    def _stage(self, svc: Any, verified: Any, offer: Any, ch: Any, rec: dict[str, Any]) -> bool:
        """Stage ``offer`` with OTA-C's ``stage_app`` (never switches). True when it is staged
        by the time this returns (inline); a job reports it later."""
        from harness_manager.services.update.appstage import stage_app

        def stage(progress: Any = None) -> dict[str, Any]:
            # progress: (what, bytes, total), the job's own shape
            return stage_app(svc, verified=verified, version=offer.version, auto=True,
                             progress=progress)

        def run(progress: Any) -> Any:
            if callable(progress):
                progress("stage", 0, 0)
            out = stage(progress)
            if out.get("staged"):
                # announced from the job thread: the tick has already returned
                with self._mu:
                    last = su.read_json(su.last_check_path(self.state_dir)) or {}
                    after = {**last, "staged": True}
                    self._announce(ch, offer, True, after)
                    su.write_json(su.last_check_path(self.state_dir), after)
            return out

        if self.submit is None:
            try:
                return bool(stage().get("staged"))
            except HarnessError as exc:
                rec["stage_error"] = exc.message
                log.warning("staging harness-manager %s failed: %s", offer.version, exc.message)
                return False
        try:
            job = self.submit(STAGE_KIND, run)
        except HarnessError as exc:
            rec["stage_deferred"] = exc.message
            log.info("staging harness-manager %s deferred: %s", offer.version, exc.message)
            return False
        rec["stage_job"] = getattr(job, "id", "")
        return False
