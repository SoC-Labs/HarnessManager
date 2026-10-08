"""Compare what a board runs with what a channel offers, and say what an update would do.

The planner reads; it never writes. Its ``Plan`` lists:

- the **steps** in order (download, verify, store overlays, write the OS slot,
  back up the config SD, install it, reboot with a witness, confirm the
  identity the board reports);
- **warnings** (an expired channel, a downgrade, a private component skipped
  for want of a token, a board that cannot be read over Ethernet);
- **blockers**: reasons it cannot run at all (the app is too old, the release
  is withdrawn, the config SD is not reachable, the board is another model);
- for a **re-key** (the release's ``static_id`` differs from the board's):
  what becomes unusable, and the exact phrase the user must type to consent.

A plan is approved with ``Plan.approve(consent=...)``. The executor refuses to
run anything without an approval that matches this plan, so an update always
starts with a user's action (CLI ``--yes``/prompt, GUI button), never by itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import RefusedError
from harness_manager.core.model import BoardIdentity

from . import hub_door
from .schema import (
    KIND_OS_SLOT,
    KIND_OVERLAYS,
    KIND_SD,
    STATUS_WITHDRAWN,
    TARGET_HOST_STORE,
    TARGET_MCC_SD,
    Channel,
    Component,
    HarnessIdentity,
    HarnessRelease,
    os_header_crc,
    os_provisioned_static,
)
from .version import at_least, compare_safe, same_version

#: UI2 G6: where a Linux board's OS boots from (``BoardView.os_boot``, ``Plan.os_boot``).
OS_BOOT_CARD = "card"
OS_BOOT_NETBOOT = "netboot"
#: UI2 G6: the blocker of an OS slot image on a netbooted board (Board > Versions says it).
NETBOOT_BLOCKER = ("the OS is the hub's TFTP image: this board has no user microSD holding an "
                   "OS slot, so stage0 boots the image the hub serves at every cold boot, and "
                   "this release's OS slot image cannot be written here; ask the hub's admin "
                   "to serve this release's image, or give the board a card and install again")

#: The words an OS-slot adapter's ``slots_reason`` uses for "no card" (the MPS3 pack's "no
#: user microSD card in the slot", the demo's "no card in the USER microSD slot"): a running
#: Linux harness with no card booted over the network.
_NO_CARD = ("no user microsd card", "no card in the user microsd")


def os_boot_of(running: str) -> str:
    """Where a Linux board's OS came from, by the slot it reports running: ``card`` for A/B,
    ``netboot`` for rescue/none (stage0 took the hub's image), ``""`` otherwise."""
    if running in ("A", "B"):
        return OS_BOOT_CARD
    return OS_BOOT_NETBOOT if running in ("rescue", "none") else ""


def netboot_of(adapter: object) -> tuple[str, str]:
    """(``os_boot``, the adapter's ``slots_reason``) for a Linux board whose OS slots cannot be
    used: ``netboot`` when the reason is that no card is in the slot. Never raises."""
    reason_of = getattr(adapter, "slots_reason", None)
    if not callable(reason_of):
        return "", ""
    try:
        reason = str(reason_of() or "")
    except Exception as exc:  # noqa: BLE001 - a reason that fails is no reason
        reason = str(getattr(exc, "message", exc))
    low = reason.lower()
    return (OS_BOOT_NETBOOT if any(k in low for k in _NO_CARD) else ""), reason


MODE_FULL = "full"            # base and/or OS, plus overlays
MODE_OVERLAYS = "overlays"    # host store only: no SD write, no reboot
MODE_NONE = "none"            # nothing to do

#: The witness budget when the RELEASE boots Linux (TEAM_PLAN §4a said 180 s; FIX-PACK-2
#: item 7: 300 s, the MPS3 pack's ``REBOOT_WAIT_S_LINUX``, since stage0's DDR settle put a
#: cold MCC boot at ~190 s). Otherwise None: the pack picks it from the running harness
#: (``constants.reboot_wait_s``, T12-6).
LINUX_REBOOT_WAIT_S = 300.0


@dataclass(frozen=True)
class PlanStep:
    action: str               # download | verify | store-overlays | write-os-slot | backup-sd |
                              # install-sd | reboot | confirm-identity | confirm-os-slot
    detail: str
    component: str = ""


@dataclass
class Plan:
    board_id: str
    channel: str
    serial: int
    release: HarnessRelease | None
    running: dict[str, Any]
    running_release: str                  # the matching channel version, or "" (unrecorded)
    mode: str
    rekey: bool = False
    consent_phrase: str = ""
    unusable: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)   # what to download
    skipped: dict[str, str] = field(default_factory=dict)
    base: bool = False                    # the config SD will be written
    os_slot: bool = False                 # the OS slot will be written
    #: Why a door this board does not offer here is needed (LINUX-SLOTS: an OS image for
    #: another static cannot go through the Ethernet door). Also listed in ``blockers``.
    needs_door: list[str] = field(default_factory=list)
    reboot_wait_s: float | None = None    # None: the pack's own budget for the running harness
    # HUB-SD (H10, U9/U10): the door a base goes through ("" = the local Debug USB, "hub" =
    # the board pack's hub SD door), what it is, the typed phrase that names the board, its
    # lease holder and queue, and whether auto-revert is armed by default (``hub_door``).
    via: str = ""
    hub: dict[str, Any] = field(default_factory=dict)
    board_phrase: str = ""
    auto_revert: bool = False
    os_pending: str = ""                  # a committed, unbooted slot rolled back first (rule 1)
    #: LINUX-ANSWERS (S5): the default slot stage0 fell back FROM (it did not come up
    #: healthy; the default stays on it until a rollback). "" = no fallback.
    os_fell_back: str = ""
    #: UI2 G6 (additive): where the board's OS boots from: "card" (its user microSD's OS
    #: slots), "netboot" (no card holds one: stage0 boots the image the hub serves over TFTP
    #: at every cold boot), "" (not known: bare metal, or the board did not say).
    os_boot: str = ""

    @property
    def version(self) -> str:
        return self.release.version if self.release else ""

    @property
    def up_to_date(self) -> bool:
        return self.mode == MODE_NONE

    def fingerprint(self) -> str:
        """What an approval is bound to: the release, the board and the actions."""
        body = json.dumps({
            "board": self.board_id, "channel": self.channel, "serial": self.serial,
            "version": self.version, "mode": self.mode, "components": sorted(self.components),
            "running": self.running, "rekey": self.rekey, "base": self.base, "os": self.os_slot,
            # HUB-SD: only when set, so a local plan's fingerprint is what it always was
            **({"via": self.via, "board_phrase": self.board_phrase} if self.via else {}),
        }, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    def approve(self, *, consent: str = "", by: str = "user", board_phrase: str = "",
                auto_revert: bool | None = None, allow_mcc_update: bool = False) -> Approval:
        """The user's go-ahead. A re-key needs ``consent`` equal to ``consent_phrase``; a
        remote door (HUB-SD) needs ``board_phrase`` equal to the plan's (it names the board,
        the lease holder and the queue). ``auto_revert``: None takes the plan's default
        (armed on the hub door, U10), False disarms it, True needs a backup to revert to."""
        if self.blockers:
            raise RefusedError(f"this update cannot run: {'; '.join(self.blockers)}",
                               hint="fix the blockers first; `harness-manager update check` lists them")
        if self.rekey and consent.strip() != self.consent_phrase:
            raise RefusedError(
                f"this update RE-KEYS the board (shell {self.running.get('shell_id') or '?'} -> "
                f"{self.release.identity.static_id if self.release else '?'}); "
                f"{len(self.unusable)} item(s) become unusable",
                hint=f"to consent, type exactly: {self.consent_phrase}")
        if self.board_phrase and board_phrase.strip() != self.board_phrase:
            raise RefusedError(
                f"this install goes through the hub and interrupts whoever uses the board: "
                f"{(self.hub or {}).get('consent_text') or self.board_phrase}",
                hint=f"to consent, type exactly: {self.board_phrase}")
        armed = self.auto_revert if auto_revert is None else bool(auto_revert)
        if armed and not self.auto_revert and auto_revert:
            raise RefusedError("auto-revert needs a backup to revert to, and this plan has none",
                               hint="install without auto-revert, or through a door that keeps "
                                    "a backup")
        return Approval(fingerprint=self.fingerprint(), consent=consent.strip(), by=by,
                        board_phrase=board_phrase.strip(), auto_revert=armed,
                        allow_mcc_update=bool(allow_mcc_update))

    def summary(self) -> dict[str, Any]:
        return {
            "board_id": self.board_id, "channel": self.channel, "serial": self.serial,
            "version": self.version, "running": self.running,
            "running_release": self.running_release, "mode": self.mode,
            "up_to_date": self.up_to_date, "rekey": self.rekey,
            "consent_phrase": self.consent_phrase, "unusable": list(self.unusable),
            "steps": [{"action": s.action, "detail": s.detail, "component": s.component}
                      for s in self.steps],
            "warnings": list(self.warnings), "blockers": list(self.blockers),
            "components": list(self.components), "skipped": dict(self.skipped),
            "base": self.base, "os_slot": self.os_slot,
            "via": self.via, "hub": dict(self.hub), "board_phrase": self.board_phrase,
            "auto_revert": self.auto_revert,
            "needs_door": list(self.needs_door),
            "os_fell_back": self.os_fell_back or None,
            "os_boot": self.os_boot,
            "fingerprint": self.fingerprint(),
        }


def mcc_version(text: str) -> str:
    """An MCC firmware version as compared: trimmed, one leading ``v``/``V`` dropped (the
    board reports ``v1.3.2``, a release lists ``1.3.2``), case-blind (FIX-PACK-7)."""
    t = str(text or "").strip()
    return (t[1:] if t[:1] in ("v", "V") else t).lower()


def mcc_fw_tested(running: str, tested: Any) -> bool:
    """Is the board's MCC firmware one the release lists as tested (``mcc_version`` on both
    sides)?"""
    want = mcc_version(running)
    return any(mcc_version(t) == want for t in tested)


@dataclass(frozen=True)
class Approval:
    fingerprint: str
    consent: str = ""
    by: str = "user"
    board_phrase: str = ""                # HUB-SD: the typed phrase of a remote door
    auto_revert: bool = False             # HUB-SD (U10): armed at approval
    #: FIX-PACK-7 (G8): write a bundle MBBIOS line that would make the MCC update itself
    #: (the card has no line and has the .ebf it names); refused without it (15)
    allow_mcc_update: bool = False


@dataclass(frozen=True)
class BoardView:
    """What the planner knows about the board and its links."""

    board_id: str
    pack: str
    identity: BoardIdentity | None        # None: no board (a channel-only check)
    identity_known: bool = True           # False: the shell did not answer
    has_storage: bool = False
    has_controller: bool = False
    has_os_slots: bool = False
    os_active_sha: str = ""               # the running OS slot image, when the adapter says
    os_active_crc: str = ""               # the running slot's S0LB table CRC (the board's hdr_crc)
    os_pending: str = ""                  # a slot committed and not booted yet (rule 1)
    # LINUX-ANSWERS (S5): a fallback: the default slot that failed to boot, the slot running
    # instead, and the failed slot's hdr_crc ("" = none; ``slot_health.fell_back``)
    os_fell_back: str = ""
    os_running: str = ""
    os_fell_back_crc: str = ""
    sd_revisions: tuple[str, ...] = ()    # MB/HBI0309<rev> dirs seen on the config SD
    # FIX-PACK-9: the revision the board's MCC reads (``HBI0309C``; "" unknown) and how it
    # is known ("LOG.TXT on its config SD", "the MCC boot log", "the config SD's only
    # revision folder"): the pack's ``storage.board_revision``
    board_rev: str = ""
    board_rev_from: str = ""
    mcc_firmware: str = ""
    # HUB-SD: the pack's hub SD door (``hub_door``), with the lease holder/mine/queue;
    # {} when the board is not behind a hub.
    hub_sd: dict[str, Any] = field(default_factory=dict)
    # UI2 G6: where the OS boots from ("card", "netboot", "": ``Plan.os_boot``), and why the
    # board offers no OS slot door when it offers none (its adapter's ``slots_reason``).
    os_boot: str = ""
    os_slots_reason: str = ""


#: FIX-PACK-9 (david, 2 Oct): the board revisions a release may serve but nobody has run it
#: on, and what the planner warns ("Rev B: boots, untested"; Rev C is the supported one).
UNTESTED_REVS = {"HBI0309B": "Rev B: boots, untested"}


def _sd_has_rev(comp: Component | None, rev: str) -> bool | None:
    """Does the config-SD part's signed file list carry ``MB/<rev>/board.txt``? None when it
    lists no files (the bundle check measures it after the download)."""
    if comp is None or not comp.files:
        return None
    want = f"mb/{rev.lower()}/board.txt"
    return any(k.replace("\\", "/").strip("/").lower() == want for k in comp.files)


def revision_check(rel: HarnessRelease, board: BoardView) -> tuple[str, str]:
    """``(blocker, warning)`` ("" each: none) for the board's revision against the release.

    The MCC reads only ``MB/<its revision>/``: a release whose config SD lacks that folder
    leaves the board unprogrammed ("File not found \\MB\\HBI0309B\\board.txt"). With the
    revision known (``board.board_rev``) it must be one of ``compat.board_revs`` and in the
    SD part's file list (when it has one); unknown, the config SD's revision folders must
    meet ``compat.board_revs`` (the rule before FIX-PACK-9). A known untested revision
    (``UNTESTED_REVS``) is a warning."""
    revs = list(rel.compat.board_revs)
    supported = {r.upper() for r in revs}
    rev = board.board_rev.upper()
    who = f"this board is {rev}" + (f" ({board.board_rev_from})" if board.board_rev_from else "")
    blocker = ""
    if rev and supported:
        if rev not in supported:
            blocker = (f"{who}, and harness {rel.version} carries "
                       f"{', '.join(f'MB/{r}' for r in revs)} only: the MCC reads only "
                       f"MB/{rev}/, so the board would stay unprogrammed")
        elif _sd_has_rev(next((c for c in rel.by_target(TARGET_MCC_SD) if c.kind == KIND_SD),
                              None), rev) is False:
            blocker = (f"{who}, and harness {rel.version}'s config SD has no MB/{rev}/board.txt: "
                       f"the MCC reads only MB/{rev}/, so the board would stay unprogrammed")
    elif board.sd_revisions and supported and \
            not supported & {r.upper() for r in board.sd_revisions}:
        blocker = (f"the config SD is for {', '.join(board.sd_revisions)}; harness "
                   f"{rel.version} supports {', '.join(revs)}")
    warning = ""
    if not blocker and rev in UNTESTED_REVS:
        warning = (f"{UNTESTED_REVS[rev]}. {who[0].upper()}{who[1:]}; harness {rel.version} is "
                   "supported on Rev C")
    return blocker, warning


def _same_u32(a: str, b: str) -> bool:
    try:
        return int(a, 16) == int(b, 16)
    except (TypeError, ValueError):
        return a.lower() == b.lower()


def same_sd_part(a: HarnessRelease, b: HarnessRelease) -> bool:
    """Do two releases carry byte-identical configuration SD parts (by signed sha256)?"""
    sa = sorted(c.asset.sha256 for c in a.by_target(TARGET_MCC_SD))
    sb = sorted(c.asset.sha256 for c in b.by_target(TARGET_MCC_SD))
    return bool(sa) and sa == sb


def os_image_running(comp: Component, board: BoardView) -> bool:
    """Does the board already run this OS image? By the sha256 this host recorded pushing
    under the running slot, or by the image's declared S0LB table CRC (the board's
    ``hdr_crc``: what the board itself knows the image by)."""
    if board.os_active_sha and board.os_active_sha.lower() == comp.asset.sha256:
        return True
    want = os_header_crc(comp)
    return bool(want and board.os_active_crc and _same_u32(want, board.os_active_crc))


def running_summary(ident: BoardIdentity | None) -> dict[str, Any]:
    if ident is None:
        return {}
    return {"shell_id": ident.shell_id.lower(), "harness": ident.harness_version,
            "firmware_sha": ident.firmware_sha, "usercode": ident.usercode.lower(),
            "impl": ident.harness_impl, "features": sorted(ident.features),
            "ver32": ident.ver32.lower()}


#: The shortest sha prefix that names a build (git's own short-sha floor).
FW_SHA_MIN_HEX = 7


def fw_sha_match(want: str, have: str) -> bool | None:
    """Do a release's ``fw_sha`` and the board's firmware sha name the same build?

    None when either side has none (nothing to decide with). Compared on their
    common prefix, because the board reports 8 hex digits and a release may record
    the full sha; a pair shorter than ``FW_SHA_MIN_HEX`` must be equal.
    """
    w, h = (want or "").strip().lower(), (have or "").strip().lower()
    if not w or not h:
        return None
    n = min(len(w), len(h))
    return w == h if n < FW_SHA_MIN_HEX else w[:n] == h[:n]


def ver32_match(want: str, have: str) -> bool | None:
    """Do a release's ``ver32`` and the board's name the same HARNESS_VER32?

    None when either side has none (0 counts as none: "not stamped"). Compared as u32.
    """
    try:
        w = int(want, 16) if want else 0
        h = int(have, 16) if have else 0
    except ValueError:
        return None
    if not w or not h:
        return None
    return w == h


def identity_rank(want: HarnessIdentity, ident: BoardIdentity) -> int | None:
    """How well what the board reports fits a release's wire identity (HARNESS-DIST §3.2).

    None: it does not fit. Otherwise a rank, higher = more specific:
    2 for a matching ``fw_sha``, plus 1 for a matching ``ver32``, plus 1 for a matching
    ``harness`` string.

    - ``static_id`` must match; ``usercode`` and ``impl`` must match when both sides
      have them;
    - ``fw_sha`` is DECISIVE when both sides have one: every firmware since v0.8 says
      ``harness=1.0.0``, and a release may carry its tag there, so a differing
      version string never overrules a matching sha, and a matching one never
      rescues a differing sha;
    - ``ver32`` (H1) is decisive the same way when both sides have one: it is what the
      firmware packs from its VERSION, so once VERSION is stamped with the release tag
      (HARNESS-DIST R4) two bakes of one firmware sha differ only there. A differing
      ver32 never fits, even with a matching sha;
    - with neither a sha nor a ver32 to compare (an older record, or a board that does
      not say), the ``harness`` string must match when both sides have one.
    """
    if not ident.shell_id or not _same_u32(want.static_id, ident.shell_id):
        return None
    if want.usercode and ident.usercode and not _same_u32(want.usercode, ident.usercode):
        return None
    if want.impl and ident.harness_impl and want.impl != ident.harness_impl:
        return None
    sha = fw_sha_match(want.fw_sha, ident.firmware_sha)
    if sha is False:
        return None
    v32 = ver32_match(want.ver32, ident.ver32)
    if v32 is False:
        return None
    harness = same_version(want.harness, ident.harness_version) \
        if want.harness and ident.harness_version else None
    if sha is None and v32 is None and harness is False:
        return None
    return (2 if sha else 0) + (1 if v32 else 0) + (1 if harness else 0)


def match_release(channel: Channel, ident: BoardIdentity,
                  board: BoardView | None = None) -> HarnessRelease | None:
    """The channel release this board runs, by its wire identity (``identity_rank``).

    The most specific fit wins. When two releases fit equally well (the same firmware
    in two releases, or a board that reports too little to tell them apart) the answer
    is None, "unrecorded": naming one of them would be a guess. With no identity fit, a
    Linux board is matched by the OS image its running slot holds (``running_by_os_image``).
    """
    ranked = [(rank, rel) for rel in channel.harness
              if (rank := identity_rank(rel.identity, ident)) is not None]
    if not ranked:
        return running_by_os_image(channel.harness, ident, board)
    best = max(rank for rank, _ in ranked)
    top = [rel for rank, rel in ranked if rank == best]
    return top[0] if len(top) == 1 else None


def running_by_os_image(releases: Iterable[HarnessRelease], ident: BoardIdentity | None,
                        board: BoardView | None) -> HarnessRelease | None:
    """The one release whose OS image the board's running slot holds (by the sha256 this
    host pushed, or the S0LB table CRC the board reports), on the board's own static.
    v2.0.0 was published with a firmware sha the board does not report (harnessd's hash,
    not the image commit), so its identity never fits; the image itself does. None when
    no release, or more than one, carries that image."""
    if board is None or ident is None or not ident.shell_id or \
            not (board.os_active_crc or board.os_active_sha):
        return None
    hits = [rel for rel in releases
            if _same_u32(rel.identity.static_id, ident.shell_id)
            and (os := next((c for c in rel.components if c.kind == KIND_OS_SLOT), None))
            is not None and os_image_running(os, board)]
    return hits[0] if len({r.version for r in hits}) == 1 else None


def base_differs(rel: HarnessRelease, ident: BoardIdentity) -> bool:
    """Would installing ``rel`` change the base the board runs? A differing ver32 does;
    then the firmware sha decides when both sides have one; then a matching ver32 says
    no; otherwise the ``harness`` string (``identity_rank``)."""
    i = rel.identity
    if not ident.shell_id or not _same_u32(i.static_id, ident.shell_id):
        return True
    if i.usercode and ident.usercode and not _same_u32(i.usercode, ident.usercode):
        return True
    if i.impl and ident.harness_impl and i.impl != ident.harness_impl:
        return True
    v32 = ver32_match(i.ver32, ident.ver32)
    if v32 is False:
        return True
    sha = fw_sha_match(i.fw_sha, ident.firmware_sha)
    if sha is not None:
        return not sha
    if v32:
        return False
    return bool(i.harness) and (not ident.harness_version or
                                not same_version(i.harness, ident.harness_version))


def pinned_release(channel: Channel, pinned: str) -> tuple[HarnessRelease | None, str]:
    """What a board pinned to ``pinned`` is OFFERED on ``channel`` (HARNESS-CAT, §4.2):
    ``(release, why)``. The channel's current release when it is not past the pin; else
    the pinned release; None (with the reason) when the channel lists only releases past
    the pin, so nothing is offered."""
    current = channel.harness_release(None)
    if current is not None and (compare_safe(current.version, pinned) or 0) <= 0:
        return current, ""
    pin = channel.harness_release(pinned)
    if pin is not None:
        return pin, (f"the board is pinned to harness {pin.version}: the channel's current "
                     f"{current.version if current else '?'} is past the pin, so it is not offered")
    return None, (f"the board is pinned to harness {pinned}, which the {channel.channel!r} channel "
                  "does not list, and its releases are past the pin: nothing is offered "
                  "(unpin it, or name a version)")


def make_plan(channel: Channel, board: BoardView, *, app_version: str,
              version: str | None = None, overlays_only: bool = False,
              stored_overlays: Iterable[dict[str, str]] = (),
              have_token: bool = False, channel_warnings: Iterable[str] = (),
              stored_components: Iterable[str] = (), pinned: str = "",
              via: str | None = None) -> Plan:
    """The plan for one board (see the module docstring). Never raises for a board state.

    ``pinned`` (HARNESS-CAT): the board's pin. With no ``version``, the plan offers the
    pinned release instead of a newer current one, and a board already past its pin has
    nothing to do (a pin never proposes a rollback; naming the version does).

    ``via`` (HUB-SD): ``"hub"`` sends the base through the pack's hub SD door, ``"usb"``
    through the local Debug USB; None picks the hub door only for a board with no local
    Debug USB whose pack offers it (``hub_door.apply``).
    """
    ident = board.identity or BoardIdentity(board_type=board.pack)
    pin_note = ""
    if version is None and pinned:
        rel, pin_note = pinned_release(channel, pinned)
    else:
        rel = channel.harness_release(version)
    plan = Plan(board_id=board.board_id, channel=channel.channel, serial=channel.serial,
                release=rel, running=running_summary(board.identity),
                running_release="", mode=MODE_NONE)
    plan.warnings.extend(channel_warnings)
    if pin_note and rel is not None:
        plan.warnings.append(pin_note)
    running = match_release(channel, ident, board) if board.identity_known else None
    plan.running_release = running.version if running else ""
    if running is not None and running.status == STATUS_WITHDRAWN:
        plan.warnings.append(f"the board runs harness {running.version}, which the publisher "
                             "has WITHDRAWN: update it")
    if rel is None:
        plan.blockers.append(pin_note or f"the {channel.channel!r} channel has no harness "
                                          f"release {version or '(no current release)'}")
        return plan
    if rel.status == STATUS_WITHDRAWN:
        plan.blockers.append(f"harness {rel.version} is withdrawn by its publisher")
    if channel.board.pack and board.pack and channel.board.pack != board.pack:
        plan.blockers.append(f"the channel is for {channel.board.pack} boards, this is {board.pack}")
    if rel.compat.min_app and not at_least(app_version, rel.compat.min_app):
        plan.blockers.append(f"harness {rel.version} needs harness-manager >= {rel.compat.min_app} "
                             f"(this is {app_version}); run `harness-manager update app` first")
    rev_blocker, rev_warning = revision_check(rel, board)        # FIX-PACK-9
    if rev_blocker:
        plan.blockers.append(rev_blocker)
    if rev_warning:
        plan.warnings.append(rev_warning)
    if board.mcc_firmware and rel.compat.mcc_fw_tested and \
            not mcc_fw_tested(board.mcc_firmware, rel.compat.mcc_fw_tested):
        plan.warnings.append(f"MCC firmware {board.mcc_firmware} was not tested with harness "
                             f"{rel.version} (tested: {', '.join(rel.compat.mcc_fw_tested)})")
    if not board.identity_known:
        plan.warnings.append("the shell did not answer over Ethernet: the running harness is "
                             "unknown, and an install cannot be CONFIRMED without that link")

    # -- what differs --
    rekey = bool(ident.shell_id) and not _same_u32(rel.identity.static_id, ident.shell_id)
    if rekey and not rel.rekey:
        plan.warnings.append(f"the channel does not mark harness {rel.version} as a re-key, but "
                             "its static_id differs from the board's: treating it as one")
    base_needed = base_differs(rel, ident) if board.identity_known else True
    if running is not None and running.version == rel.version and not rekey:
        base_needed = False             # it runs this very release (matched by its OS image)
    elif base_needed and running is not None and not rekey and same_sd_part(rel, running):
        # An OS-only patch (v2.0.0 -> v2.0.1): the configuration SD part is byte-identical,
        # so it is not rewritten. A board whose card carries its own bake keeps it.
        base_needed = False
        plan.warnings.append(f"the configuration SD part of {rel.version} is the same as the "
                             f"running {running.version}'s: it is not written")
    downgrade, newer = False, ident.harness_version
    if board.identity_known and _same_u32(rel.identity.static_id, ident.shell_id or "0x0"):
        if running is not None:
            # The release versions order the catalogue: the wire string may say 1.0.0 for all.
            downgrade, newer = (compare_safe(rel.version, running.version) or 0) < 0, running.version
        elif ident.harness_version and rel.identity.harness:
            downgrade = (compare_safe(rel.identity.harness, ident.harness_version) or 0) < 0
    if downgrade and version is None and not (running is not None and
                                              running.status == STATUS_WITHDRAWN):
        offered = "pinned release" if pin_note else "channel's current"
        plan.warnings.append(f"the board runs harness {newer}, newer than the "
                             f"{offered} {rel.version}; nothing to do")
        base_needed = False
    elif downgrade:
        plan.warnings.append(f"this is a ROLLBACK from harness {newer} to "
                             f"{rel.version} (from the signed release history)")
    os_comp = next((c for c in rel.components if c.kind == KIND_OS_SLOT), None)
    os_needed = os_comp is not None and not os_image_running(os_comp, board)
    if overlays_only:
        base_needed = os_needed = False
        if not board.identity_known or not ident.shell_id:
            plan.blockers.append("an overlay-only update needs the running shell's static_id "
                                 "(the shell did not answer over Ethernet)")
        elif rekey:
            plan.blockers.append(
                f"harness {rel.version}'s overlays are keyed to shell {rel.identity.static_id}; the "
                f"board runs {ident.shell_id}: install the harness first")
            rekey = False

    # Host-store parts already imported (by their signed sha256) need not come again.
    done = {sha.lower() for sha in stored_components}
    host = [c for c in rel.components if c.target == TARGET_HOST_STORE
            and c.asset.sha256 not in done]
    wanted: list[Component] = list(host)
    if base_needed:
        wanted += rel.by_target(TARGET_MCC_SD)
        if not rel.by_target(TARGET_MCC_SD):
            base_needed = False
    if os_needed and os_comp is not None:
        wanted.append(os_comp)
    for c in wanted:
        if c.needs_token and not have_token:
            plan.skipped[c.name] = (f"private ({c.ip_class}); needs a GitHub token with access to "
                                    f"{c.asset.repo or 'the private release repo'}")
            continue
        plan.components.append(c.name)
    for name, why in plan.skipped.items():
        plan.warnings.append(f"{name} will be skipped: {why}")

    plan.base = base_needed
    plan.os_slot = os_needed and os_comp is not None
    plan.os_pending = board.os_pending if plan.os_slot else ""
    plan.os_fell_back = board.os_fell_back
    if board.os_fell_back:
        # LINUX-ANSWERS (S5): stage0 went back to the other slot and the default stayed on
        # the bad one; rollback is the fix (an OS write here does it first).
        run = board.os_running or "the other slot"
        plan.warnings.append(
            f"slot {board.os_fell_back} failed to boot; {run} is running; roll back to make "
            f"{run} the default (`harness-manager slot rollback TARGET`)"
            + (": this update does that first" if plan.os_slot else ""))
        if plan.os_slot and os_comp is not None and board.os_fell_back_crc and \
                _same_u32(os_header_crc(os_comp) or "", board.os_fell_back_crc):
            plan.warnings.append(f"slot {board.os_fell_back} holds THIS release's OS image "
                                 f"(hdr_crc {board.os_fell_back_crc}) and it did not boot: "
                                 "pushing it again will most likely fail the same way")
    plan.rekey = rekey and (plan.base or plan.os_slot)
    if plan.base and not (board.identity_known and ident.shell_id):
        # The running static_id is unknown, so this install MAY re-key the board: never let
        # it happen without the same typed consent a known re-key needs.
        plan.rekey = True
        plan.warnings.append("the running shell's static_id is unknown (no Ethernet answer): "
                             "installing may re-key the board, so it needs typed consent")
    # The harness that comes BACK is the release's: a bare-metal -> Linux install needs the
    # Linux budget even though the pack would pick bare-metal from the running identity.
    plan.reboot_wait_s = LINUX_REBOOT_WAIT_S if rel.identity.impl == "linux" else None

    hub_door.apply(plan, rel, channel, board, via=via, running=running,
                   have_token=have_token)
    if plan.base and board.has_storage and not board.sd_revisions and \
            plan.via != hub_door.VIA_HUB and rel.compat.board_revs:
        # FIX-PACK-9 (the Linux lead): a card with no revision folder is written anyway
        plan.warnings.append(
            "the config SD has no revision folder (MB/HBI*): is it this board's configuration "
            f"SD? harness {rel.version} writes "
            f"{', '.join(f'MB/{r}' for r in rel.compat.board_revs)}")
    if plan.via == hub_door.VIA_HUB and plan.base and not board.has_controller:
        plan.blockers.append("the new base runs only after a board REBOOT: it needs the MCC "
                             "reached on the hub (an SSH login to the hub; a REST-only hub "
                             "cannot reach tty_00)")
    if plan.base and not board.has_storage and plan.via != hub_door.VIA_HUB:
        plan.blockers.append("installing the harness base writes the config SD: it needs the "
                             "MPS3 Debug USB (the V2M-MPS3 volume)")
    if plan.base and not board.has_controller and plan.via != hub_door.VIA_HUB:
        plan.blockers.append("the new base runs only after a board REBOOT: it needs the Debug USB "
                             "MCC console")
    plan.os_boot = board.os_boot
    if plan.os_slot and not board.has_os_slots:
        if board.os_boot == OS_BOOT_NETBOOT:
            plan.blockers.append(NETBOOT_BLOCKER)
        else:
            plan.blockers.append("this release carries an OS slot image, but this harness "
                                 "offers no OS slot update (a Linux harness with the slot "
                                 "verbs is needed)"
                                 + (f": {board.os_slots_reason}" if board.os_slots_reason
                                    else ""))
    if plan.os_slot and os_comp is not None and board.identity_known and ident.shell_id:
        # LINUX-SLOTS: the Ethernet door carries an OS image only for the RUNNING static
        # (the board refuses a push provisioned for any other fabric). A Linux release on
        # another static needs its base through the config SD first: the Debug USB or the
        # hub, then the image through stage0 rescue (HARNESS-DIST L3, not built yet).
        prov = os_provisioned_static(os_comp, rel)
        if not _same_u32(prov, ident.shell_id):
            why = (f"needs Debug USB or hub: the OS image is provisioned for static {prov}, "
                   f"the board runs {ident.shell_id.lower()}; the Ethernet door carries only "
                   "an image for the running static (the base goes through the config SD "
                   "first, then the image through stage0 rescue, which is not built yet)")
            plan.needs_door.append(why)
            plan.blockers.append(why)

    if plan.base or plan.os_slot:
        plan.mode = MODE_FULL
    elif any(c.name in plan.components for c in host):
        plan.mode = MODE_OVERLAYS          # host store only: overlays, openocd, DUT firmware
    else:
        plan.mode = MODE_NONE

    if plan.rekey:
        new = rel.identity.static_id
        old = ident.shell_id or "the running shell (unknown)"
        plan.consent_phrase = f"REKEY {new}"
        plan.unusable = _unusable(old, stored_overlays)
        plan.warnings.append(f"RE-KEY: shell {old} -> {new}. Everything keyed to {old} "
                             "stops loading; see 'unusable'")
    _steps(plan, rel)
    return plan


def _unusable(old_static: str, stored: Iterable[dict[str, str]]) -> list[str]:
    out = []
    for meta in stored:
        if _same_u32(meta.get("static_id", ""), old_static):
            out.append(f"overlay {meta.get('rm_name', '?')} ({meta.get('rm_id', '?')}) in the "
                       f"local store, keyed to {old_static}")
    out.append(f"any overlay you built yourself against static {old_static}")
    out.append("the default overlay committed on the board (QSPI/µSD), if any: re-commit one "
               "built for the new shell")
    return out


def _steps(plan: Plan, rel: HarnessRelease) -> None:
    if plan.mode == MODE_NONE:
        return
    s = plan.steps
    s.append(PlanStep("download", f"{len(plan.components)} component(s), sha256-checked: "
                                  f"{', '.join(plan.components)}"))
    s.append(PlanStep("verify", "domain checks: part, static_id, usercode, CRC, SD rules"))
    for c in rel.components:
        if c.target == TARGET_HOST_STORE and c.name in plan.components:
            what = (f"overlays keyed to {rel.identity.static_id}" if c.kind == KIND_OVERLAYS
                    else f"{c.kind} files")
            s.append(PlanStep("store-overlays", f"{what} into the local store (no SD write)",
                              c.name))
    if plan.os_slot and plan.os_fell_back:
        s.append(PlanStep("rollback-os-slot", f"slot {plan.os_fell_back} failed to boot and "
                                              "stage0 went back to the other slot: roll back "
                                              "first, so the running slot is the default again "
                                              "and a slot is free"))
    elif plan.os_slot and plan.os_pending:
        s.append(PlanStep("rollback-os-slot", f"slot {plan.os_pending} is committed but not "
                                              "booted: roll it back first, so a slot is free "
                                              "(rule 1)"))
    if plan.os_slot:
        s.append(PlanStep("write-os-slot", "push the image into the free OS slot, read it back, "
                                           "commit it as the default (stage0 goes back to the "
                                           "old slot if it never comes up healthy)"))
    if plan.base and plan.via == hub_door.VIA_HUB:
        s.extend(PlanStep(*step) for step in hub_door.steps(plan, rel))
    elif plan.base:
        sd = rel.by_target(TARGET_MCC_SD)[0]
        s.append(PlanStep("backup-sd", "back up the whole config SD (mandatory gate)", sd.name))
        s.append(PlanStep("install-sd", "write the new base to the config SD, journaled, "
                                        "read back; never an .ebf", sd.name))
    if plan.base or plan.os_slot:
        budget = (f"up to {plan.reboot_wait_s:.0f} s" if plan.reboot_wait_s
                  else "the pack's budget for this harness")
        how = ("on the hub, pyverify's `sd field --already-written` (the journal witness, the "
               "only reader on tty_00, an intact Cmd>) then a paced REBOOT (100 ms a "
               "character), witnessed: " if plan.via == hub_door.VIA_HUB else "")
        s.append(PlanStep("reboot", f"{how}reboot the board and witness it go down and come back "
                                    f"({budget})"))
        want = rel.identity
        what = f"harness {want.harness or rel.version}"
        if want.fw_sha:
            what = f"firmware {want.fw_sha[:8]} ({what})"
        s.append(PlanStep("confirm-identity", f"the board must report shell "
                                              f"{want.static_id}, {what}"))
        if plan.base and plan.via == hub_door.VIA_HUB and plan.auto_revert:
            s.append(PlanStep(*hub_door.revert_step(plan)))
    if plan.os_slot:
        s.append(PlanStep("confirm-os-slot", "the board runs the new slot as the default, "
                                             "booted by stage0, and harnessd confirms a "
                                             "healthy boot (when the harness reports it; a "
                                             "boot alone is not a confirm)"))
