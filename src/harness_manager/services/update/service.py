"""``UpdateService``: the one object the CLI, GUI and daemon use for updates.

Constructed as ``UpdateService(engine)`` (the engine's lazy-service shape; see
the contract change request), or with explicit parts in tests. It owns:

- the trust store (pinned keys + accepted rotation) and the channel client;
- the downloader (cache in ``state_dir/update/cache``; the GitHub token from
  ``$HARNESS_MANAGER_GITHUB_TOKEN``, never logged);
- the planner, the harness installer/rollback, and the app self-updater.

``check`` only reads (and publishes ``update.available``). Every install needs
an approved plan: nothing here installs on its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness_manager import __version__
from harness_manager.core.errors import AlreadyError, HarnessError, RefusedError
from harness_manager.core.events import Event, EventBus

from .app import AppUpdater
from .bundle import OverlayHandler, PackOverlayHandler
from .channel import ChannelClient, VerifiedChannel
from .download import Downloader, token_from_env
from .executor import HarnessInstaller, UpdateOutcome, default_os_slots
from .os_slots import OsSlotAdapter
from .planner import Approval, BoardView, Plan, make_plan
from .policy import Policy, load_policy
from .state import StoredComponents, UpdateState
from .trust import TrustStore, load_trust
from .version import compare

log = logging.getLogger(__name__)


def _state_dir(engine: Any, state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    sd = getattr(engine, "state_dir", None)
    if sd is not None:
        return Path(sd)
    from harness_manager.engine import resolve_state_dir

    return resolve_state_dir(getattr(engine, "config", None))


def releases_summary(ch: Any) -> dict[str, list[dict[str, Any]]]:
    """The channel's releases as listed, the one it calls current marked (daemon + UI)."""
    return {
        "harness": [{"version": r.version, "status": r.status, "static_id": r.identity.static_id,
                     "harness": r.identity.harness, "impl": r.identity.impl, "rekey": r.rekey,
                     "released_at": r.released_at, "notes_url": r.notes_url,
                     "current": r.version == ch.harness_current} for r in ch.harness],
        "app": [{"version": r.version, "status": r.status, "released_at": r.released_at,
                 "notes_url": r.notes_url, "current": r.version == ch.app_current}
                for r in ch.app],
    }


class UpdateService:
    def __init__(self, engine: Any = None, *, state_dir: Path | None = None,
                 trust: TrustStore | None = None, token: str | None = None,
                 bus: EventBus | None = None, store: Any = None, app_version: str = __version__,
                 overlay_handler: OverlayHandler | None = None,
                 os_slots_for: Callable[[Any], OsSlotAdapter | None] = default_os_slots,
                 downloader: Downloader | None = None, app_updater: AppUpdater | None = None,
                 policy: Policy | None = None, now: Callable[[], float] = time.time) -> None:
        self.engine = engine
        self.state_dir = _state_dir(engine, state_dir)
        self.state = UpdateState.under(self.state_dir)
        self.trust = trust if trust is not None else load_trust(self.state)
        self.downloader = downloader or Downloader(
            self.state.cache, token=token if token is not None else token_from_env())
        self.channels = ChannelClient(self.state, self.downloader, self.trust, now=now)
        self.bus = bus if bus is not None else getattr(engine, "bus", None)
        self._store = store
        self.app_version = app_version
        self._overlay_handler = overlay_handler
        self.os_slots_for = os_slots_for
        self._app = app_updater
        # the administrator's policy file (U6), read-only; a user's settings cannot loosen it
        self.policy = policy if policy is not None else load_policy()
        self.now = now

    # -- parts --

    @property
    def store(self) -> Any:
        if self._store is None and self.engine is not None:
            self._store = self.engine.store
        return self._store

    def overlay_handler(self, pack: str) -> OverlayHandler:
        return self._overlay_handler or PackOverlayHandler(pack)

    def app(self) -> AppUpdater:
        if self._app is None:
            # lane OTA-L: the install root of the running copy (M4), its uv and extras
            self._app = AppUpdater.for_install(self.state_dir, policy_off=self.policy.off_reason(),
                                               running_version=self.app_version, now=self.now)
        return self._app

    def installer(self, pack: str) -> HarnessInstaller:
        return HarnessInstaller(state=self.state, downloader=self.downloader, store=self.store,
                                bus=self.bus, overlay_handler=self.overlay_handler(pack),
                                os_slots_for=self.os_slots_for, now=self.now)

    # -- channel --

    def fetch_channel(self, channel: str | None = None, source: str | None = None) -> VerifiedChannel:
        return self.channels.fetch(self.policy.channel_for(channel), source)

    # -- board --

    def board_view(self, session: Any) -> BoardView:
        cand = session.candidate
        try:
            ident = session.identity()
            known = bool(ident.shell_id)
        except HarnessError:
            ident, known = None, False
        storage = getattr(session, "storage", None)
        controller = getattr(session, "controller", None)
        slots = self.os_slots_for(session)
        revs: tuple[str, ...] = ()
        if storage is not None:
            try:
                mb = Path(storage.locate()) / "MB"
                revs = tuple(sorted(p.name for p in mb.iterdir()
                                    if p.is_dir() and p.name.upper().startswith("HBI")))
            except (HarnessError, OSError):
                revs = ()
        os_sha = ""
        if slots is not None:
            try:
                os_sha = slots.status().active_info.image_sha256
            except HarnessError:
                os_sha = ""
        witness = getattr(controller, "last_reboot", None)
        boot = getattr(witness, "boot", None)
        return BoardView(board_id=cand.board_id, pack=cand.pack, identity=ident,
                         identity_known=known, has_storage=storage is not None,
                         has_controller=controller is not None, has_os_slots=slots is not None,
                         os_active_sha=os_sha, sd_revisions=revs,
                         mcc_firmware=getattr(boot, "firmware", "") or "")

    def plan_harness(self, session: Any, *, verified: VerifiedChannel | None = None,
                     channel: str | None = None, source: str | None = None,
                     version: str | None = None, overlays_only: bool = False) -> tuple[Plan, VerifiedChannel]:
        verified = verified or self.fetch_channel(channel, source)
        stored = []
        if self.store is not None:
            try:
                stored = [meta for _sha, meta in self.store.find("overlay")]
            except (HarnessError, OSError):
                stored = []
        plan = make_plan(verified.channel, self.board_view(session), app_version=self.app_version,
                         version=version, overlays_only=overlays_only, stored_overlays=stored,
                         have_token=bool(self.downloader.token),
                         channel_warnings=verified.warnings,
                         stored_components=StoredComponents(self.state).all())
        return plan, verified

    def install_harness(self, session: Any, plan: Plan, approval: Approval | None,
                        verified: VerifiedChannel) -> UpdateOutcome:
        return self.installer(session.candidate.pack).run(session, plan, approval, verified)

    def rollback_harness(self, session: Any, *, backup_path: Path | None = None,
                         wait_s: float | None = None) -> UpdateOutcome:
        return self.installer(session.candidate.pack).rollback(session, backup_path=backup_path,
                                                               wait_s=wait_s)

    # -- check (read-only) --

    def check(self, *, channel: str | None = None, source: str | None = None,
              session: Any = None) -> dict[str, Any]:
        verified = self.fetch_channel(channel, source)
        ch = verified.channel
        report: dict[str, Any] = {
            "channel": ch.channel, "serial": ch.serial, "issued_at": ch.issued_at,
            "expires_at": ch.expires_at, "signed_by": verified.key_id,
            "key_role": verified.key_role, "source": verified.url,
            "warnings": list(verified.warnings),
            "harness_current": ch.harness_current, "app_current": ch.app_current,
            "app_running": self.app_version, "app_update": "",
            "releases": releases_summary(ch),
        }
        offer = self.app().offer(ch.app, ch.app_current) if ch.app_current else None
        blocked = self.app().blocked()
        if offer is not None and blocked:
            report["warnings"].append(f"harness-manager {offer.version} is available, but "
                                      f"self-update is off here: {blocked}")
        elif offer is not None:
            report["app_update"] = offer.version
        if self.policy.path:
            report["policy"] = self.policy.as_dict()
            report["warnings"] += [f"policy {self.policy.path}: {p}" for p in self.policy.problems]
        board_id = ""
        if session is not None:
            plan, _ = self.plan_harness(session, verified=verified)
            report["plan"] = plan.summary()
            board_id = session.candidate.board_id
        available = bool(report["app_update"]) or bool(
            report.get("plan") and not report["plan"]["up_to_date"] and not report["plan"]["blockers"])
        report["available"] = available
        if available and self.bus is not None:
            self.bus.publish(Event("update.available", board_id, {
                "channel": ch.channel, "serial": ch.serial, "harness": ch.harness_current,
                "app": report["app_update"],
            }))
        return report

    # -- the app --

    def update_app(self, *, verified: VerifiedChannel | None = None, channel: str | None = None,
                   source: str | None = None, version: str | None = None,
                   switch: bool = True) -> dict[str, Any]:
        """Download + verify + stage the app release, then switch (unless busy)."""
        verified = verified or self.fetch_channel(channel, source)
        ch = verified.channel
        rel = ch.app_release(version)
        if rel is None:
            raise RefusedError(f"the {ch.channel!r} channel has no app release "
                               f"{version or '(no current release)'}")
        if rel.status == "withdrawn":
            raise RefusedError(f"harness-manager {rel.version} is withdrawn by its publisher")
        if version is None and compare(rel.version, self.app_version) <= 0:
            raise AlreadyError(f"harness-manager {self.app_version} is current on the "
                               f"{ch.channel!r} channel")
        self.app().guard(f"update the app to {rel.version}")     # before any download
        wheel = self.downloader.fetch(rel.wheel, base_url=verified.url)
        lock = self.downloader.fetch(rel.lock, base_url=verified.url) if rel.lock else None
        staged = self.app().stage(rel, wheel, lock)
        out: dict[str, Any] = {"version": rel.version, "staged": staged, "switched": False,
                               "locked": lock is not None}
        if not lock:
            out["warning"] = ("the release has no hashed lock file: dependencies came from the "
                              "package index unpinned")
        if staged.get("extras_missing"):
            out["warning"] = "; ".join(w for w in (out.get("warning"), (
                f"the release's lock does not cover the extras "
                f"{', '.join(staged['extras_missing'])} you installed, so {rel.version} is "
                "without them")) if w)
        if switch:
            out["pointer"] = self.app().switch(rel.version)
            out["switched"] = True
        return out
