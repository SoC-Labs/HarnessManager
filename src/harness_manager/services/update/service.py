"""``UpdateService``: the one object the CLI, GUI and daemon use for updates.

Constructed as ``UpdateService(engine)`` (the engine's lazy-service shape; see
the contract change request), or with explicit parts in tests. It owns:

- the trust store (pinned keys + accepted rotation) and the channel client;
- the downloader (cache in ``state_dir/update/cache``; the GitHub token from
  ``$HARNESS_MANAGER_GITHUB_TOKEN``, else ``gh auth token`` on first need, never logged);
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

from . import github
from .app import AppUpdater
from .appstage import app_dirs, offer_app, prepare_app_release, refuse_if_bad
from .bundle import OverlayHandler, PackOverlayHandler
from .channel import ChannelClient, VerifiedChannel
from .download import Downloader, token_from_env
from .executor import HarnessInstaller, UpdateOutcome, default_os_slots
from .lease_gate import lease_state, require_lease
from .os_slots import OsSlotAdapter
from .planner import Approval, BoardView, Plan, make_plan
from .policy import Policy, load_policy
from .schema import harness_catalog
from .state import Pins, StoredComponents, UpdateState
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


def sd_ab_setting(resolver: Any = None) -> bool:
    """``updates.sd_ab`` from the settings (U8). Off when unset, unreadable, or not a bool:
    the A/B pointer install stays off until its 10-minute board check proves the MCC loads
    another 8.3 ``F0FILE`` name."""
    try:
        if resolver is None:
            from harness_manager.settings import Resolver

            resolver = Resolver.load()
        value = resolver.resolve("updates.sd_ab").value
    except Exception:  # noqa: BLE001 - a broken settings file never turns an install mode on
        return False
    return value is True


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
                 policy: Policy | None = None, now: Callable[[], float] = time.time,
                 leases: Any = None, sd_ab: bool | Callable[[], bool] | None = None) -> None:
        self.engine = engine
        self.state_dir = _state_dir(engine, state_dir)
        self.state = UpdateState.under(self.state_dir)
        self.trust = trust if trust is not None else load_trust(self.state)
        # An explicit token (even "") is final; otherwise the env var, else `gh auth token`,
        # resolved only when a GitHub host is about to be asked (OTA-C, U1).
        self.downloader = downloader or Downloader(
            self.state.cache, token=token if token is not None else token_from_env(),
            token_provider=None if token is not None else github.resolve_token)
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
        # HARNESS-CAT: the hub lease gates every install (``lease_gate``). The daemon shares
        # its ``LeaseService`` here; otherwise one is made on first need (no bus: the
        # daemon's own service is the one that announces lease changes).
        self.leases = leases
        # HUB-SD (U8): the config SD A/B by pointer, off until the 10-minute board check
        # (``updates.sd_ab``); None reads the setting when an install asks.
        self._sd_ab = sd_ab
        # SET-WIRE: a token stored or cleared through the settings applies to the next
        # download, not after a restart (updates.github_token is a live row).
        if self.bus is not None and hasattr(self.bus, "subscribe"):
            self.bus.subscribe("settings.changed", self._on_settings_changed)

    def _on_settings_changed(self, event: Any) -> None:
        keys = (getattr(event, "data", None) or {}).get("keys") or ()
        if "updates.github_token" in keys and hasattr(self.downloader, "forget_token"):
            self.downloader.forget_token()

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
                                os_slots_for=self.os_slots_for, now=self.now,
                                lease_check=self.check_lease, sd_ab=self.sd_ab_enabled)

    def sd_ab_enabled(self) -> bool:
        """``updates.sd_ab`` (U8): False unless it is set, and False when it cannot be read."""
        if self._sd_ab is not None:
            return bool(self._sd_ab() if callable(self._sd_ab) else self._sd_ab)
        return sd_ab_setting()

    def hub_door_view(self, session: Any) -> dict[str, Any]:
        """The pack's hub SD door (``session.hub_sd.describe()``) plus the lease as it bears
        on an install: ``holder``, ``mine``, ``queue`` (HUB-SD). ``{}`` without a door."""
        door = getattr(session, "hub_sd", None)
        if door is None:
            return {}
        try:
            out = dict(door.describe())
        except Exception as exc:  # noqa: BLE001 - a plan never fails for a door's description
            out = {"available": False, "reason": f"the hub door could not be read: {exc}"}
        st = self.lease_state(session)
        out.update(lease_required=bool(st.get("required")), mine=bool(st.get("mine")),
                   holder=st.get("holder", ""), lease_reason=st.get("reason", ""))
        queue: list[dict[str, Any]] = []
        hub = getattr(session, "hub", None)
        if hub is not None and st.get("required"):
            try:
                view = self.lease_service().view(hub, cached_only=True) or {}
                queue = [{"holder": q.get("holder", ""), "position": q.get("position", 0)}
                         for q in view.get("queue") or [] if isinstance(q, dict)]
            except (HarnessError, TypeError):
                queue = []
        out["queue"] = queue
        return out

    # -- the hub lease (HARNESS-CAT) --

    def lease_service(self) -> Any:
        if self.leases is None:
            from harness_manager.services.lease import LeaseService

            self.leases = LeaseService(self.state_dir)
        return self.leases

    def lease_state(self, session: Any) -> dict[str, Any]:
        """The board's hub lease as it bears on an install (``lease_gate.lease_state``)."""
        if getattr(session, "hub", None) is None:
            return lease_state(session, None)
        return lease_state(session, self.lease_service())

    def check_lease(self, session: Any, what: str = "install a harness") -> dict[str, Any]:
        """``HeldError`` unless an install may go ahead on this board (``lease_gate``)."""
        if getattr(session, "hub", None) is None:
            return lease_state(session, None)
        return require_lease(session, self.lease_service(), what)

    def pins(self) -> Pins:
        return Pins(self.state)

    # -- channel --

    def fetch_channel(self, channel: str | None = None, source: str | None = None, *,
                      catalog: str | None = None) -> VerifiedChannel:
        """``catalog`` (OTA-C): ``hm-app`` or a harness catalogue; picks a ``github:``
        source's rolling release, and the signed document must belong to it."""
        return self.channels.fetch(self.policy.channel_for(channel), source, catalog=catalog)

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
        os_sha = os_crc = os_pending = ""
        if slots is not None:
            try:
                st = slots.status()
                active = st.active_info
                os_sha, os_crc = active.image_sha256, getattr(active, "hdr_crc", "")
                os_pending = getattr(st, "pending_commit", "")
            except HarnessError:
                os_sha = ""
        witness = getattr(controller, "last_reboot", None)
        boot = getattr(witness, "boot", None)
        return BoardView(board_id=cand.board_id, pack=cand.pack, identity=ident,
                         identity_known=known, has_storage=storage is not None,
                         has_controller=controller is not None, has_os_slots=slots is not None,
                         os_active_sha=os_sha, os_active_crc=os_crc, os_pending=os_pending,
                         sd_revisions=revs,
                         mcc_firmware=getattr(boot, "firmware", "") or "",
                         hub_sd=self.hub_door_view(session))

    def plan_harness(self, session: Any, *, verified: VerifiedChannel | None = None,
                     channel: str | None = None, source: str | None = None,
                     version: str | None = None, overlays_only: bool = False,
                     catalog: str | None = None,
                     pinned: str | None = None,
                     via: str | None = None) -> tuple[Plan, VerifiedChannel]:
        """``pinned`` (HARNESS-CAT): None reads the board's pin (``Pins``) for this channel's
        catalogue; "" plans as if it had none. ``via`` (HUB-SD): ``hub``/``usb``/None."""
        verified = verified or self.fetch_channel(channel, source, catalog=catalog)
        if pinned is None:
            pin = self.pins().get(session.candidate.board_id,
                                  verified.catalog or harness_catalog(session.candidate.pack))
            pinned = str(pin["version"]) if pin else ""
        stored = []
        if self.store is not None:
            try:
                stored = [meta for _sha, meta in self.store.find("overlay")]
            except (HarnessError, OSError):
                stored = []
        plan = make_plan(verified.channel, self.board_view(session), app_version=self.app_version,
                         version=version, overlays_only=overlays_only, stored_overlays=stored,
                         have_token=self.downloader.has_token(),
                         channel_warnings=verified.warnings,
                         stored_components=StoredComponents(self.state).all(),
                         pinned=pinned, via=via)
        return plan, verified

    def install_harness(self, session: Any, plan: Plan, approval: Approval | None,
                        verified: VerifiedChannel) -> UpdateOutcome:
        return self.installer(session.candidate.pack).run(session, plan, approval, verified)

    def rollback_harness(self, session: Any, *, backup_path: Path | None = None,
                         wait_s: float | None = None, via: str | None = None) -> UpdateOutcome:
        return self.installer(session.candidate.pack).rollback(session, backup_path=backup_path,
                                                               wait_s=wait_s, via=via)

    # -- check (read-only) --

    def check(self, *, channel: str | None = None, source: str | None = None,
              session: Any = None, catalog: str | None = None) -> dict[str, Any]:
        verified = self.fetch_channel(channel, source, catalog=catalog)
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
        # OTA-C: a version marked bad (it failed its health check here) is never offered
        verdict = offer_app(ch, self.app_version, self.state, app=self.app())
        offer = verdict.release
        if verdict.skipped_bad:
            report["app_skipped"] = {"version": verdict.skipped_bad, "why": verdict.why}
        policy_off = self.policy.off_reason() or self.app().policy_off
        if offer is not None and policy_off:
            # the administrator's "off": no app update is offered at all
            report["warnings"].append(f"harness-manager {offer.version} is available, but "
                                      f"self-update is off here: {policy_off}")
        elif offer is not None:
            report["app_update"] = offer.version
            if self.app().dev_install:
                # a check only reads: it still says what the channel has
                report["warnings"].append(f"this copy cannot update itself to {offer.version}: "
                                          f"{self.app().dev_install}")
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
                   switch: bool = True, catalog: str | None = None) -> dict[str, Any]:
        """Download + verify + stage the app release, then switch (unless busy)."""
        verified = verified or self.fetch_channel(channel, source, catalog=catalog)
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
        refuse_if_bad(self.state, rel.version, app=self.app())
        # wheel, lock and deps, each sha256-checked; deps pinned to the verified local files
        wheels_dir, locks_dir = app_dirs(self.app(), self.state)
        prepared = prepare_app_release(self.downloader, verified, rel, wheels_dir=wheels_dir,
                                       locks_dir=locks_dir,
                                       extras=getattr(self.app(), "extras", ()) or ())
        staged = self.app().stage(rel, prepared.wheel, prepared.lock)
        out: dict[str, Any] = {"version": rel.version, "staged": staged, "switched": False,
                               "locked": rel.lock is not None, "lock": prepared.lock_name}
        if rel.lock is None:
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
