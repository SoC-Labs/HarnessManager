"""Harness versions: a board pack's harness catalogue, per board (HARNESS-CAT; HARNESS-DIST H6).

Built on the update core (``services/update``: the signed channel, the planner, the
executor), never beside it. For the selected board it answers:

- **list**: every release on the chosen channels (``stable``; ``beta`` and ``dev`` when
  asked), newest first, once each. Each row is ONE ``make_plan(version=v)`` (spike P3)
  turned into a **verdict** with its reason:

  ==============  ============================================================================
  ``fits``        installs as it is (firmware-only, overlays only, an older release on the
                  same static, or nothing to do: the board runs it)
  ``re-key``      a different static: the typed ``REKEY <static_id>`` consent, and every
                  overlay, DUT RM and kit keyed to the running static stops matching
  ``needs-door``  "needs Debug USB or hub": the config SD needs the MPS3 Debug USB on this
                  machine (the hub door is H10), or the OS slot needs a Linux harness
  ``incompatible`` withdrawn, another board pack or revision, a newer Harness Manager, or
                  any other blocker the planner names
  ==============  ============================================================================

  plus the marks ``running`` (the board reports the release's wire identity, fw_sha
  first: ``planner.match_release``), ``installed`` (HM's last install on this board names
  it), ``pinned``, ``current`` and ``offered`` (what an install with no version gives),
  the signed release notes, the size and whether it is in the cache, and **what changes**
  (``what_changes``): the static (a re-key changes every overlay and the kit), the
  firmware sha, the implementation, features, the protocol, the config-SD files and an OS
  image;
- **show**: one release in full (identity, components, notes, what changes, the plan with
  its ``fingerprint``);
- **fetch**: download and verify a release's parts into the cache now (offline later);
- **install**: the planner and executor as ``update harness`` runs them (an approved plan,
  the typed re-key consent), refused unless this client holds the board's hub lease
  (``update.lease_gate``); events ``harness.installing`` and ``harness.installed``;
- **pin / unpin**: a per-board pin in the state dir (``update.state.Pins``): the planner
  never offers a release past it;
- **history**: the last ``HISTORY_KEEP`` installs of the board (``InstallRecords``), and
  the **rollback candidates**: the release each install replaced (``from_version``), then
  the channel's release before the running one;
- **mirror**: ``update.mirror.write_mirror`` per channel, Arm IP left out by default.

Events (docs/CONTRACTS.md): ``harness.catalog``, ``harness.installing``,
``harness.installed``, ``harness.pinned``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    RefusedError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.core.model import BoardIdentity

from .update.channel import VerifiedChannel
from .update.executor import UpdateOutcome, doors_of
from .update.mirror import MirrorReport, write_mirror
from .update.planner import (
    Approval,
    BoardView,
    Plan,
    fw_sha_match,
    identity_rank,
    make_plan,
    pinned_release,
)
from .update.schema import (
    KIND_OS_SLOT,
    KIND_OVERLAYS,
    KIND_RM_KIT,
    KIND_SD,
    STATUS_WITHDRAWN,
    TARGET_MCC_SD,
    HarnessRelease,
    harness_catalog,
)
from .update.state import HISTORY_KEEP, InstallRecords, StoredComponents
from .update.version import at_least, compare, is_version, parse_version

CHANNELS = ("stable", "beta", "dev")

VERDICT_FITS = "fits"
VERDICT_REKEY = "re-key"
VERDICT_DOOR = "needs-door"
VERDICT_INCOMPATIBLE = "incompatible"
VERDICT_TEXT = {VERDICT_FITS: "fits", VERDICT_REKEY: "re-key",
                VERDICT_DOOR: "needs Debug USB or hub", VERDICT_INCOMPATIBLE: "incompatible"}

#: Install records that mean "the board was given this release" (the ``installed`` mark).
INSTALLED_RESULTS = ("installed", "restored")


def _same_u32(a: str, b: str) -> bool:
    try:
        return int(a, 16) == int(b, 16)
    except (TypeError, ValueError):
        return (a or "").lower() == (b or "").lower()


def _key(version: str) -> Any:
    return parse_version(version)


def release_size(rel: HarnessRelease, *, kits: bool = False) -> int:
    return sum(c.asset.size for c in rel.components if kits or c.kind != KIND_RM_KIT)


# --- what changes -----------------------------------------------------------------------


def _sd_files(rel: HarnessRelease | None) -> dict[str, str] | None:
    """The config-SD component's file list (path -> sha256), when the release states it."""
    if rel is None:
        return None
    sd = next((c for c in rel.by_target(TARGET_MCC_SD) if c.kind == KIND_SD), None)
    return dict(sd.files) if sd is not None and sd.files else None


def what_changes(rel: HarnessRelease, ident: BoardIdentity | None,
                 running: HarnessRelease | None = None,
                 plan: Plan | None = None) -> dict[str, Any]:
    """What installing ``rel`` changes on a board that reports ``ident`` (HARNESS-DIST §5 show).

    ``running``: the channel release the board runs, when known (for the config-SD file
    list and the kit's Vivado release); ``plan``: that release's plan, for the overlays in
    the local store that stop loading. ``summary`` is the lines a person reads.
    """
    ri = rel.identity
    comps_ovl = [{"name": c.name, "ip_class": c.ip_class, "private": c.needs_token}
                 for c in rel.components if c.kind == KIND_OVERLAYS]
    os_image = any(c.kind == KIND_OS_SLOT for c in rel.components)
    if ident is None or not ident.shell_id:
        return {"known": False, "static": {"from": "", "to": ri.static_id, "changes": None},
                "overlays": {"components": comps_ovl, "rekeyed": None}, "os_image": os_image,
                "summary": ["the running harness is unknown (no board, or it did not answer): "
                            "what changes cannot be said"]}
    old = ident.shell_id.lower()
    rekey = not _same_u32(ri.static_id, old)
    fw = fw_sha_match(ri.fw_sha, ident.firmware_sha)
    impl_changes = bool(ri.impl and ident.harness_impl and ri.impl != ident.harness_impl)
    added = sorted(set(ri.features) - set(ident.features)) if ri.features else []
    removed = sorted(set(ident.features) - set(ri.features)) if ri.features else []
    have_sd, want_sd = _sd_files(running), _sd_files(rel)
    sd: dict[str, Any] = {"known": have_sd is not None and want_sd is not None,
                          "changed": [], "added": [], "removed": []}
    if have_sd is not None and want_sd is not None:
        sd["changed"] = sorted(p for p in want_sd if p in have_sd and have_sd[p] != want_sd[p])
        sd["added"] = sorted(p for p in want_sd if p not in have_sd)
        sd["removed"] = sorted(p for p in have_sd if p not in want_sd)
    stops = list(plan.unusable) if plan is not None and plan.rekey else []
    kit = {"from_static": old, "to_static": ri.static_id.lower(), "changes": rekey,
           "vivado_from": running.vivado if running is not None else "",
           "vivado_to": rel.vivado, "in_release": bool(rel.kits())}
    out: dict[str, Any] = {
        "known": True,
        "static": {"from": old, "to": ri.static_id.lower(), "changes": rekey},
        "firmware": {"from": ident.firmware_sha, "to": ri.fw_sha,
                     "changes": None if fw is None else not fw},
        "impl": {"from": ident.harness_impl, "to": ri.impl, "changes": impl_changes},
        "proto": {"from": ident.proto, "to": ri.proto},
        "features": {"added": added, "removed": removed},
        "overlays": {"components": comps_ovl, "rekeyed": rekey, "keyed_to": ri.static_id.lower(),
                     "stops_loading": stops},
        "kit": kit,
        "sd_files": sd,
        "os_image": os_image,
    }
    lines: list[str] = []
    if rekey:
        lines.append(f"re-key: static {old} -> {ri.static_id.lower()}: every overlay changes, "
                     f"and every overlay or DUT RM keyed to {old} stops loading")
        vivado = (f"; Vivado {kit['vivado_from'] or '?'} -> {rel.vivado}"
                  if rel.vivado and kit["vivado_from"] != rel.vivado else "")
        lines.append(f"DUT kit: kits for {old} stop matching; this release needs the kit for "
                     f"{ri.static_id.lower()}{vivado}")
    if impl_changes:
        lines.append(f"harness implementation {ident.harness_impl} -> {ri.impl}")
    if fw is False:
        lines.append(f"firmware {ident.firmware_sha[:8] or '?'} -> {ri.fw_sha[:8]}")
    if added or removed:
        lines.append("features " + ", ".join([*(f"+{f}" for f in added),
                                              *(f"-{f}" for f in removed)]))
    if ri.proto and ident.proto and ri.proto != ident.proto:
        lines.append(f"net-protocol {ident.proto} -> {ri.proto}")
    if sd["known"] and (sd["changed"] or sd["added"] or sd["removed"]):
        lines.append("config SD: " + ", ".join([*(f"{p} changes" for p in sd["changed"]),
                                               *(f"+{p}" for p in sd["added"]),
                                               *(f"-{p}" for p in sd["removed"])]))
    if os_image:
        lines.append("an OS slot image: written to the inactive slot, try-once")
    if not lines:
        lines.append("nothing on the board changes: it reports this release's identity"
                     if fw is not False and not rekey else "the base changes")
    out["summary"] = lines
    return out


# --- verdicts -----------------------------------------------------------------------------


def verdict_of(plan: Plan, rel: HarnessRelease, board: BoardView, *, app_version: str,
               board_pack: str = "") -> dict[str, Any]:
    """The row's compatibility with the board: ``{verdict, verdict_text, why, needs}``."""
    needs: list[str] = []
    bad: list[str] = []
    if rel.status == STATUS_WITHDRAWN:
        bad.append(f"harness {rel.version} is withdrawn by its publisher")
    if board_pack and board.pack and board_pack != board.pack:
        bad.append(f"the release is for {board_pack} boards, this is {board.pack}")
    if rel.compat.min_app and not at_least(app_version, rel.compat.min_app):
        bad.append(f"needs harness-manager >= {rel.compat.min_app} (this is {app_version})")
        needs.append("newer-app")
    if board.sd_revisions and rel.compat.board_revs and not (
            {r.upper() for r in rel.compat.board_revs} & {r.upper() for r in board.sd_revisions}):
        bad.append(f"supports {', '.join(rel.compat.board_revs)}; the config SD is for "
                   f"{', '.join(board.sd_revisions)}")
    door: list[str] = []
    if plan.base and not (board.has_storage and board.has_controller):
        door.append("the base is on the config SD: it needs the MPS3 Debug USB on this machine "
                    "(or the hub door, not built yet)")
        needs.append("debug-usb")
    if plan.os_slot and not board.has_os_slots:
        door.append("it carries an OS slot image: it needs a Linux harness with the slot verbs "
                    "and a card")
        needs.append("linux-slot")
    if plan.rekey:
        needs.append("consent")
    if bad:
        verdict, why = VERDICT_INCOMPATIBLE, bad[0]
    elif door:
        verdict, why = VERDICT_DOOR, door[0]
    elif plan.blockers:
        verdict, why = VERDICT_INCOMPATIBLE, plan.blockers[0]
    elif plan.rekey:
        old = plan.running.get("shell_id") or "the running static"
        verdict = VERDICT_REKEY
        why = (f"another static ({old} -> {rel.identity.static_id.lower()}): everything "
               f"keyed to {old} stops loading; type {plan.consent_phrase!r} to consent")
    else:
        verdict, why = VERDICT_FITS, _fits_why(plan)
    return {"verdict": verdict, "verdict_text": VERDICT_TEXT[verdict], "why": why,
            "needs": needs}


def _fits_why(plan: Plan) -> str:
    if plan.mode == "none":
        return "nothing to do: the board runs it"
    if plan.mode == "overlays":
        return "overlays only: into the local store; the board is not touched"
    back = any("ROLLBACK" in w for w in plan.warnings)
    what = []
    if plan.base:
        what.append("the config SD is rewritten and the board rebooted")
    if plan.os_slot:
        what.append("the OS image goes to the inactive slot, try-once")
    return ("rollback on the same static: " if back else "same static: ") + "; ".join(what)


# --- the catalogue ------------------------------------------------------------------------


@dataclass
class Listing:
    """``HarnessCatalog.list`` (and what the daemon caches): JSON via ``as_dict``."""

    catalog: str
    board_id: str
    channels: list[dict[str, Any]]
    releases: list[dict[str, Any]]
    board: dict[str, Any] | None = None
    offer: str = ""
    rollback: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    at: float = 0.0
    verified: dict[str, VerifiedChannel] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"catalog": self.catalog, "board_id": self.board_id, "channels": self.channels,
                "board": self.board, "offer": self.offer, "releases": self.releases,
                "rollback": self.rollback, "warnings": self.warnings, "at": self.at}

    def channel_of(self, version: str) -> str:
        """The first (most stable) channel listing ``version``, or ""."""
        want = _key(version)
        for name, v in self.verified.items():
            if any(_key(r.version) == want for r in v.channel.harness):
                return name
        return ""


class HarnessCatalog:
    """The harness catalogue of a board pack, on an ``UpdateService`` (see the module doc)."""

    def __init__(self, update: Any, *, keep: int = HISTORY_KEEP,
                 now: Callable[[], float] = time.time) -> None:
        self.update = update
        self.records = InstallRecords(update.state, keep=keep)
        self.now = now

    # -- plumbing --

    @property
    def bus(self) -> Any:
        return getattr(self.update, "bus", None)

    def _emit(self, topic: str, board_id: str, **data: Any) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))

    @staticmethod
    def catalog_id(pack: str) -> str:
        return harness_catalog(pack)

    def channels(self, channels: Sequence[str] | None, *, source: str | None = None,
                 pack: str = "mps3") -> tuple[dict[str, VerifiedChannel], list[str]]:
        """Fetch and verify each channel of the pack's catalogue, in order. With ONE channel
        asked for, its error is raised; with several, one that cannot be read is skipped with
        a warning (``dev`` rarely exists) unless none can be."""
        names = list(dict.fromkeys(channels or ("stable",)))
        cat = self.catalog_id(pack)
        out: dict[str, VerifiedChannel] = {}
        warnings: list[str] = []
        first: HarnessError | None = None
        for name in names:
            try:
                out[name] = self.update.fetch_channel(name, source, catalog=cat)
            except HarnessError as exc:
                if len(names) == 1:
                    raise
                first = first or exc
                warnings.append(f"the {name!r} channel was not read: {exc.message}")
        if not out:
            assert first is not None
            raise first
        return out, warnings

    def _stored_overlays(self) -> list[dict[str, str]]:
        store = getattr(self.update, "store", None)
        if store is None:
            return []
        try:
            return [meta for _sha, meta in store.find("overlay")]
        except (HarnessError, OSError):
            return []

    def _plan(self, v: VerifiedChannel, board: BoardView, version: str,
              stored: list[dict[str, str]], stored_components: set[str]) -> Plan:
        return make_plan(v.channel, board, app_version=self.update.app_version, version=version,
                         stored_overlays=stored, have_token=self.update.downloader.has_token(),
                         channel_warnings=v.warnings, stored_components=stored_components)

    def _cached(self, rel: HarnessRelease) -> bool:
        """Every part an install would fetch is in the cache (by name and size: a listing
        never re-hashes 12 MB blobs; the install still does)."""
        blobs = Path(self.update.downloader.cache) / "blobs"
        wanted = [c for c in rel.components if c.kind != KIND_RM_KIT
                  and not (c.needs_token and not self.update.downloader.has_token())]
        return bool(wanted) and all(
            (blobs / c.asset.sha256).is_file()
            and (blobs / c.asset.sha256).stat().st_size == c.asset.size for c in wanted)

    # -- list / show --

    @staticmethod
    def _merged(verified: dict[str, VerifiedChannel]
                ) -> list[tuple[HarnessRelease, list[str], VerifiedChannel]]:
        """Each release once, newest first, from the first (most stable) channel listing it."""
        seen: dict[Any, tuple[HarnessRelease, list[str], VerifiedChannel]] = {}
        for name, v in verified.items():
            for rel in v.channel.harness:
                k = _key(rel.version)
                if k in seen:
                    seen[k][1].append(name)
                else:
                    seen[k] = (rel, [name], v)
        return sorted(seen.values(), key=lambda t: _key(t[0].version), reverse=True)

    @staticmethod
    def running_release(releases: Iterable[HarnessRelease],
                        ident: BoardIdentity | None) -> HarnessRelease | None:
        """``planner.match_release`` over releases from several channels."""
        if ident is None or not ident.shell_id:
            return None
        ranked = [(rank, rel) for rel in releases
                  if (rank := identity_rank(rel.identity, ident)) is not None]
        if not ranked:
            return None
        best = max(r for r, _ in ranked)
        top = [rel for r, rel in ranked if r == best]
        return top[0] if len(top) == 1 else None

    def board_state(self, session: Any) -> tuple[BoardView, dict[str, Any]]:
        view = self.update.board_view(session)
        return view, self.update.lease_state(session)

    def list(self, session: Any = None, *, channels: Sequence[str] | None = None,
             source: str | None = None, pack: str | None = None,
             verified: dict[str, VerifiedChannel] | None = None, emit: bool = True) -> Listing:
        """The catalogue with verdicts for ``session``'s board (None: a channel-only list).
        ``emit``: publish ``harness.catalog`` (``show`` builds its row without it)."""
        pack = pack or (session.candidate.pack if session is not None else "mps3")
        warnings: list[str] = []
        if verified is None:
            verified, warnings = self.channels(channels, source=source, pack=pack)
        board_id = session.candidate.board_id if session is not None else ""
        merged = self._merged(verified)
        cat = self.catalog_id(pack)
        pins = self.update.pins()
        pin = pins.get(board_id, cat) if board_id else None
        primary = next(iter(verified.values()))
        if pin:
            off, why = pinned_release(primary.channel, str(pin["version"]))
            if why:
                warnings.append(why)
        else:
            off = primary.channel.harness_release(None)
        offer = off.version if off is not None else ""
        currents = {v.channel.harness_current for v in verified.values()}

        board: dict[str, Any] | None = None
        view: BoardView | None = None
        running: HarnessRelease | None = None
        last = self.records.get(board_id) if board_id else None
        if session is not None:
            view, lease = self.board_state(session)
            running = self.running_release([r for r, _, _ in merged], view.identity) \
                if view.identity_known else None
            board = {"board_id": board_id, "pack": pack, "identity_known": view.identity_known,
                     "running": self._running_summary(view.identity),
                     "running_release": running.version if running else "",
                     "installed": self._last_summary(last), "pinned": pin["version"] if pin else "",
                     "lease": lease, "doors": self._doors(view)}
            if lease["required"] and not lease["mine"]:
                warnings.append(f"installs on {board_id} need its hub lease: {lease['reason']}")
        stored = self._stored_overlays()
        stored_components = StoredComponents(self.update.state).all()
        rows = []
        for rel, chans, v in merged:
            row = self._row(rel, chans, v, view=view, running=running, last=last, pin=pin,
                            offer=offer, currents=currents, stored=stored,
                            stored_components=stored_components)
            if board is not None and board["lease"]["required"] and not board["lease"]["mine"] \
                    and row.get("touches_board"):
                row["needs"].append("hub-lease")
            rows.append(row)
        listing = Listing(catalog=cat, board_id=board_id,
                          channels=[self._channel_summary(n, v) for n, v in verified.items()],
                          releases=rows, board=board, offer=offer,
                          rollback=self.rollback_candidates(
                              board_id, [r for r, _, _ in merged],
                              running.version if running else "") if board_id else [],
                          warnings=warnings, at=self.now(), verified=dict(verified))
        if emit:
            self._emit("harness.catalog", board_id, catalog=cat,
                       channels=[c["channel"] for c in listing.channels],
                       serials={c["channel"]: c["serial"] for c in listing.channels},
                       releases=len(rows), running=board["running_release"] if board else "",
                       offer=offer)
        return listing

    def _row(self, rel: HarnessRelease, chans: list[str], v: VerifiedChannel, *,
             view: BoardView | None, running: HarnessRelease | None,
             last: dict[str, Any] | None, pin: dict[str, Any] | None, offer: str,
             currents: set[str], stored: list[dict[str, str]],
             stored_components: set[str]) -> dict[str, Any]:
        ri = rel.identity
        marks = []
        is_running = running is not None and _key(running.version) == _key(rel.version)
        installed = bool(last) and last.get("kind") != "overlays" and \
            last.get("result") in INSTALLED_RESULTS and is_version(str(last.get("version", ""))) \
            and _key(str(last["version"])) == _key(rel.version)
        written = bool(last) and last.get("result") == "written-not-running" and \
            is_version(str(last.get("version", ""))) and _key(str(last["version"])) == _key(rel.version)
        pinned = bool(pin) and is_version(str(pin["version"])) and \
            _key(str(pin["version"])) == _key(rel.version)
        for flag, name in ((is_running, "running"), (installed, "installed"), (written, "written"),
                           (pinned, "pinned"), (rel.version in currents, "current"),
                           (bool(offer) and _key(offer) == _key(rel.version), "offered")):
            if flag:
                marks.append(name)
        if pin and not pinned and is_version(str(pin["version"])) and \
                compare(rel.version, str(pin["version"])) > 0:
            marks.append("past-pin")
        row: dict[str, Any] = {
            "version": rel.version, "channels": chans, "status": rel.status,
            "released_at": rel.released_at, "static_id": ri.static_id.lower(),
            "usercode": ri.usercode.lower(), "impl": ri.impl, "fw_sha": ri.fw_sha,
            "harness": ri.harness, "ver32": ri.ver32, "proto": ri.proto, "vivado": rel.vivado,
            "rekey_in_channel": rel.rekey, "notes": rel.notes, "notes_url": rel.notes_url,
            "size": release_size(rel), "cached": self._cached(rel),
            "components": len(rel.components), "marks": marks,
            "running": is_running, "installed": installed, "pinned": pinned,
            "verdict": "", "verdict_text": "", "why": "no board: name one for verdicts",
            "needs": [], "reasons": [], "warnings": [], "mode": "", "rekey": False,
            "consent_phrase": "", "doors": [], "touches_board": False, "channel": chans[0],
            "fingerprint": "",
        }
        if view is None:
            row["changes"] = what_changes(rel, None)
            return row
        plan = self._plan(v, view, rel.version, stored, stored_components)
        row.update(verdict_of(plan, rel, view, app_version=self.update.app_version,
                              board_pack=v.channel.board.pack))
        row.update({"reasons": list(plan.blockers), "warnings": list(plan.warnings),
                    "mode": plan.mode, "rekey": plan.rekey,
                    "consent_phrase": plan.consent_phrase, "doors": doors_of(plan),
                    "touches_board": plan.base or plan.os_slot,
                    "fingerprint": plan.fingerprint()})
        row["changes"] = what_changes(rel, view.identity if view.identity_known else None,
                                      running, plan)
        return row

    @staticmethod
    def _running_summary(ident: BoardIdentity | None) -> dict[str, Any]:
        if ident is None:
            return {}
        return {"shell_id": ident.shell_id.lower(), "harness": ident.harness_version,
                "firmware_sha": ident.firmware_sha, "impl": ident.harness_impl,
                "usercode": ident.usercode.lower(), "ver32": ident.ver32, "proto": ident.proto}

    @staticmethod
    def _last_summary(last: dict[str, Any] | None) -> dict[str, Any] | None:
        if not last:
            return None
        return {k: last.get(k) for k in ("version", "result", "kind", "from_version",
                                         "recorded_at", "static_id")}

    @staticmethod
    def _doors(view: BoardView) -> dict[str, bool]:
        """The install doors this board has here (HARNESS-DIST §3.1)."""
        return {"mcc_sd": view.has_storage and view.has_controller,
                "ethernet": view.has_os_slots, "host-store": True}

    @staticmethod
    def _channel_summary(name: str, v: VerifiedChannel) -> dict[str, Any]:
        ch = v.channel
        return {"channel": name, "serial": ch.serial, "current": ch.harness_current,
                "issued_at": ch.issued_at, "expires_at": ch.expires_at, "signed_by": v.key_id,
                "key_role": v.key_role, "source": v.url, "warnings": list(v.warnings),
                "catalog": v.catalog}

    def find(self, version: str, verified: dict[str, VerifiedChannel]
             ) -> tuple[HarnessRelease, str, VerifiedChannel]:
        """The release and the (most stable) channel that lists it; ``AbsentError`` if none."""
        if not is_version(version):
            raise UsageError(f"{version!r} is not a release version", hint="e.g. 1.1.0")
        for name, v in verified.items():
            rel = v.channel.harness_release(version)
            if rel is not None:
                return rel, name, v
        raise AbsentError(f"no harness release {version} on the "
                          f"{', '.join(repr(n) for n in verified)} channel(s)",
                          hint="`harness-manager harness list --all` lists every channel")

    def show(self, version: str, session: Any = None, *, channels: Sequence[str] | None = None,
             source: str | None = None, pack: str | None = None,
             verified: dict[str, VerifiedChannel] | None = None) -> dict[str, Any]:
        """One release in full, with the plan for ``session``'s board (and its fingerprint)."""
        pack = pack or (session.candidate.pack if session is not None else "mps3")
        if verified is None:
            verified, _ = self.channels(CHANNELS if channels is None else channels,
                                        source=source, pack=pack)
        rel, name, v = self.find(version, verified)
        listing = self.list(session, pack=pack, verified={name: v}, emit=False)
        row = next(r for r in listing.releases if _key(r["version"]) == _key(rel.version))
        tok = self.update.downloader.has_token()
        blobs = Path(self.update.downloader.cache) / "blobs"
        comps = [{"name": c.name, "target": c.target, "kind": c.kind, "size": c.asset.size,
                  "sha256": c.asset.sha256, "ip_class": c.ip_class, "access": c.asset.access,
                  "private": c.needs_token, "repo": c.asset.repo,
                  "cached": (blobs / c.asset.sha256).is_file(),
                  "skipped": ("needs a GitHub token" if c.needs_token and not tok else
                              "fetched on demand by the kit service" if c.kind == KIND_RM_KIT
                              else ""),
                  "files": sorted(c.files)} for c in rel.components]
        out = {**row, "channel": name, "identity": {
                   "static_id": rel.identity.static_id, "usercode": rel.identity.usercode,
                   "harness": rel.identity.harness, "impl": rel.identity.impl,
                   "proto": rel.identity.proto, "features": list(rel.identity.features),
                   "fw_sha": rel.identity.fw_sha, "ver32": rel.identity.ver32,
                   "usr_access": rel.identity.usr_access},
               "compat": {"min_app": rel.compat.min_app,
                          "board_revs": list(rel.compat.board_revs),
                          "mcc_fw_tested": list(rel.compat.mcc_fw_tested),
                          "net_protocol": rel.compat.net_protocol,
                          "replaces_static_ids": list(rel.compat.replaces_static_ids)},
               "component_list": comps, "board": listing.board, "plan": None}
        if session is not None:
            plan, _ = self.plan(session, rel.version, verified=v)
            out["plan"] = {**plan.summary(), "fingerprint": plan.fingerprint()}
        return out

    # -- install --

    def plan(self, session: Any, version: str | None = None, *, channel: str | None = None,
             source: str | None = None, overlays_only: bool = False,
             verified: VerifiedChannel | None = None) -> tuple[Plan, VerifiedChannel]:
        """The install plan (``UpdateService.plan_harness``): ``version`` None is what the
        board is offered (its pin, else the channel's current release)."""
        return self.update.plan_harness(session, verified=verified, channel=channel,
                                        source=source, version=version,
                                        overlays_only=overlays_only,
                                        catalog=self.catalog_id(session.candidate.pack))

    def locate(self, version: str, *, channel: str | None = None, source: str | None = None,
               pack: str = "mps3") -> VerifiedChannel:
        """The verified channel to install ``version`` from: ``channel`` when named, else
        the first of stable, beta, dev that lists it."""
        if channel:
            return self.update.fetch_channel(channel, source, catalog=self.catalog_id(pack))
        verified, _ = self.channels(CHANNELS, source=source, pack=pack)
        return self.find(version, verified)[2]

    def check_lease(self, session: Any, plan: Plan) -> dict[str, Any]:
        """``HeldError`` unless this client may install ``plan`` (it writes or reboots the
        board only when it has a base or OS part: an overlay-only plan needs no lease)."""
        if plan.base or plan.os_slot:
            return self.update.check_lease(session, f"install harness {plan.version}")
        return self.update.lease_state(session)

    def install(self, session: Any, plan: Plan, approval: Approval | None,
                verified: VerifiedChannel, *, by: str = "user") -> UpdateOutcome:
        """Run an approved plan (the executor re-checks the approval, consent and lease)."""
        board_id = session.candidate.board_id
        self.check_lease(session, plan)
        self._emit("harness.installing", board_id, version=plan.version,
                   **{"from": plan.running_release}, mode=plan.mode, rekey=plan.rekey,
                   doors=doors_of(plan), channel=plan.channel, by=by)
        out = self.update.install_harness(session, plan, approval, verified)
        self._emit("harness.installed", board_id, version=out.version, result=out.result,
                   **{"from": plan.running_release}, ok=out.ok, detail=out.detail)
        return out

    # -- pins --

    def pin(self, board_id: str, version: str, *, pack: str = "mps3", by: str = "user",
            verified: dict[str, VerifiedChannel] | None = None) -> dict[str, Any]:
        if not is_version(version):
            raise UsageError(f"{version!r} is not a release version", hint="e.g. 1.1.0")
        if verified:
            rel, _, _ = self.find(version, verified)
            if rel.status == STATUS_WITHDRAWN:
                raise RefusedError(f"harness {version} is withdrawn by its publisher; a board "
                                   "is never pinned to it", hint="pin another release")
        before = self.update.pins().set(board_id, version, catalog=self.catalog_id(pack), by=by)
        prev = str((before or {}).get("version", ""))
        self._emit("harness.pinned", board_id, version=version, previous=prev, by=by)
        return {"board_id": board_id, "pinned": version, "previous": prev}

    def unpin(self, board_id: str, *, by: str = "user") -> dict[str, Any]:
        before = self.update.pins().clear(board_id)
        prev = str((before or {}).get("version", ""))
        if before is not None:
            self._emit("harness.pinned", board_id, version="", previous=prev, by=by)
        return {"board_id": board_id, "pinned": "", "previous": prev}

    # -- history and rollback --

    def history(self, board_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self.records.history(board_id, limit)

    def rollback_candidates(self, board_id: str, releases: Sequence[HarnessRelease],
                            running: str) -> list[dict[str, Any]]:
        """Where "roll back" can go, best first: each release an install replaced
        (``from_version``, newest install first), then the channel's release before the
        running one (same implementation). Each says whether the channel still lists it."""
        by_key = {_key(r.version): r for r in releases}
        out: list[dict[str, Any]] = []
        seen: set[Any] = set()
        if running and is_version(running):
            seen.add(_key(running))

        def add(version: str, source: str, why: str) -> None:
            if not version or not is_version(version) or _key(version) in seen:
                return
            seen.add(_key(version))
            rel = by_key.get(_key(version))
            listed = rel is not None
            ok = listed and rel.status != STATUS_WITHDRAWN
            out.append({"version": version, "source": source, "why": why, "listed": listed,
                        "status": rel.status if rel else "",
                        "static_id": rel.identity.static_id.lower() if rel else "",
                        "installable": ok,
                        "reason": "" if ok else (
                            f"harness {version} is withdrawn by its publisher" if listed else
                            f"the channel no longer lists {version}: it cannot be re-installed "
                            "by version (restore the SD backup instead)")})

        for h in self.records.history(board_id):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(h.get("recorded_at", 0))))
            # an install's from_version ran before it; a restore's is the release it undid
            add(str(h.get("from_version") or ""), "history",
                f"ran before harness {h.get('version') or '?'} "
                f"({h.get('kind') or 'install'}, {when})")
        cur = by_key.get(_key(running)) if running and is_version(running) else None
        older = [r for r in releases if cur is not None
                 and compare(r.version, cur.version) < 0
                 and (not cur.identity.impl or not r.identity.impl
                      or r.identity.impl == cur.identity.impl)
                 and r.status != STATUS_WITHDRAWN]
        if older:
            prev_rel = max(older, key=lambda r: _key(r.version))
            add(prev_rel.version, "channel", f"the release before {running} in the channel")
        return out

    def rollback_target(self, board_id: str, to: str, listing: Listing) -> str:
        """The version "roll back" installs: ``to`` = ``previous`` or a release version.

        ``previous`` is the release the last recorded install replaced (the first history
        candidate); only a board with no usable history falls back to the channel's release
        before the running one. A previous release the channel no longer lists (or has
        withdrawn) is refused, never silently swapped for another."""
        if to and to != "previous":
            if not is_version(to):
                raise UsageError(f"--to takes 'previous' or a release version, not {to!r}")
            return to
        cands = listing.rollback
        first = next((c for c in cands if c["source"] == "history"), None) or \
            next(iter(cands), None)
        if first is not None and first["installable"]:
            return str(first["version"])
        why = (f"{first['version']}: {first['reason']}" if first is not None else
               "no install is recorded for this board, and the channel has no older release")
        raise RefusedError(f"nothing to roll {board_id} back to ({why})",
                           hint="name a version (--to VERSION), or restore an SD backup "
                                "(--backup ZIP)")

    # -- fetch and mirror --

    def fetch(self, version: str, *, channel: str | None = None, source: str | None = None,
              pack: str = "mps3", kit: bool = False,
              progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
        """Download and verify ``version``'s parts into the cache (sha256 each). Private parts
        need a token and are skipped without one; the kit only with ``kit``."""
        v = self.locate(version, channel=channel, source=source, pack=pack)
        rel = v.channel.harness_release(version)
        assert rel is not None
        dl = self.update.downloader
        rows = []
        for c in rel.components:
            row = {"name": c.name, "target": c.target, "kind": c.kind, "size": c.asset.size,
                   "sha256": c.asset.sha256, "result": "", "why": "", "path": ""}
            if c.kind == KIND_RM_KIT and not kit:
                row.update(result="skipped", why="the DUT kit (pass --kit)")
            elif c.needs_token and not dl.has_token():
                row.update(result="skipped", why=f"private ({c.ip_class}); needs a GitHub token")
            else:
                blob = Path(dl.cache) / "blobs" / c.asset.sha256
                had = blob.is_file() and blob.stat().st_size == c.asset.size
                path = dl.fetch(c.asset, base_url=v.url, progress=progress)
                row.update(result="cached" if had else "fetched", path=str(path))
            rows.append(row)
        return {"version": rel.version, "channel": v.channel.channel, "components": rows,
                "fetched": sum(r["result"] == "fetched" for r in rows),
                "cached": sum(r["result"] == "cached" for r in rows),
                "skipped": {r["name"]: r["why"] for r in rows if r["result"] == "skipped"}}

    def mirror(self, root: Path, *, channels: Sequence[str] | None = None,
               source: str | None = None, pack: str = "mps3",
               versions: Iterable[str] | None = None, include_private: bool = False,
               progress: Callable[[str, int, int], None] | None = None) -> list[MirrorReport]:
        """``harness mirror DIR``: each channel's signed files plus ``blobs/<sha256>``. The
        Arm-IP (``access: github-token``) parts only with ``include_private``."""
        verified, _ = self.channels(channels, source=source, pack=pack)
        vers = list(versions) if versions is not None else None
        for v in vers or ():
            if not is_version(v):
                raise UsageError(f"{v!r} is not a release version")
        return [write_mirror(v, self.update.downloader, Path(root), versions=vers,
                             include_private=include_private, progress=progress)
                for v in verified.values()]


__all__ = ["CHANNELS", "HarnessCatalog", "Listing", "VERDICT_DOOR", "VERDICT_FITS",
           "VERDICT_INCOMPATIBLE", "VERDICT_REKEY", "verdict_of", "what_changes"]
