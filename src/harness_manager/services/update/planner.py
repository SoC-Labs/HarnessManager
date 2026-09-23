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

from .schema import (
    KIND_OS_SLOT,
    KIND_OVERLAYS,
    STATUS_WITHDRAWN,
    TARGET_HOST_STORE,
    TARGET_MCC_SD,
    Channel,
    Component,
    HarnessRelease,
)
from .version import at_least, compare_safe, same_version

MODE_FULL = "full"            # base and/or OS, plus overlays
MODE_OVERLAYS = "overlays"    # host store only: no SD write, no reboot
MODE_NONE = "none"            # nothing to do

#: The witness budget when the RELEASE boots Linux (TEAM_PLAN §4a: 180 s). Otherwise None:
#: the pack picks it from the running harness (``constants.reboot_wait_s``, T12-6).
LINUX_REBOOT_WAIT_S = 180.0


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
    reboot_wait_s: float | None = None    # None: the pack's own budget for the running harness

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
        }, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    def approve(self, *, consent: str = "", by: str = "user") -> Approval:
        """The user's go-ahead. A re-key needs ``consent`` equal to ``consent_phrase``."""
        if self.blockers:
            raise RefusedError(f"this update cannot run: {'; '.join(self.blockers)}",
                               hint="fix the blockers first; `harness-manager update check` lists them")
        if self.rekey and consent.strip() != self.consent_phrase:
            raise RefusedError(
                f"this update RE-KEYS the board (shell {self.running.get('shell_id') or '?'} -> "
                f"{self.release.identity.static_id if self.release else '?'}); "
                f"{len(self.unusable)} item(s) become unusable",
                hint=f"to consent, type exactly: {self.consent_phrase}")
        return Approval(fingerprint=self.fingerprint(), consent=consent.strip(), by=by)

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
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class Approval:
    fingerprint: str
    consent: str = ""
    by: str = "user"


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
    sd_revisions: tuple[str, ...] = ()    # MB/HBI0309<rev> dirs seen on the config SD
    mcc_firmware: str = ""


def _same_u32(a: str, b: str) -> bool:
    try:
        return int(a, 16) == int(b, 16)
    except (TypeError, ValueError):
        return a.lower() == b.lower()


def running_summary(ident: BoardIdentity | None) -> dict[str, Any]:
    if ident is None:
        return {}
    return {"shell_id": ident.shell_id.lower(), "harness": ident.harness_version,
            "firmware_sha": ident.firmware_sha, "usercode": ident.usercode.lower(),
            "impl": ident.harness_impl, "features": sorted(ident.features)}


def match_release(channel: Channel, ident: BoardIdentity) -> HarnessRelease | None:
    """The channel release this board runs, by (static_id, harness version[, usercode])."""
    for rel in channel.harness:
        if not ident.shell_id or not _same_u32(rel.identity.static_id, ident.shell_id):
            continue
        if rel.identity.harness and ident.harness_version and \
                not same_version(rel.identity.harness, ident.harness_version):
            continue
        if rel.identity.usercode and ident.usercode and \
                not _same_u32(rel.identity.usercode, ident.usercode):
            continue
        return rel
    return None


def base_differs(rel: HarnessRelease, ident: BoardIdentity) -> bool:
    i = rel.identity
    if not ident.shell_id or not _same_u32(i.static_id, ident.shell_id):
        return True
    if i.harness and (not ident.harness_version or
                      not same_version(i.harness, ident.harness_version)):
        return True
    if i.usercode and ident.usercode and not _same_u32(i.usercode, ident.usercode):
        return True
    return bool(i.fw_sha and ident.firmware_sha and not
                ident.firmware_sha.lower().startswith(i.fw_sha.lower()[:8]))


def make_plan(channel: Channel, board: BoardView, *, app_version: str,
              version: str | None = None, overlays_only: bool = False,
              stored_overlays: Iterable[dict[str, str]] = (),
              have_token: bool = False, channel_warnings: Iterable[str] = (),
              stored_components: Iterable[str] = ()) -> Plan:
    """The plan for one board (see the module docstring). Never raises for a board state."""
    ident = board.identity or BoardIdentity(board_type=board.pack)
    rel = channel.harness_release(version)
    plan = Plan(board_id=board.board_id, channel=channel.channel, serial=channel.serial,
                release=rel, running=running_summary(board.identity),
                running_release="", mode=MODE_NONE)
    plan.warnings.extend(channel_warnings)
    running = match_release(channel, ident) if board.identity_known else None
    plan.running_release = running.version if running else ""
    if running is not None and running.status == STATUS_WITHDRAWN:
        plan.warnings.append(f"the board runs harness {running.version}, which the publisher "
                             "has WITHDRAWN: update it")
    if rel is None:
        plan.blockers.append(f"the {channel.channel!r} channel has no harness release "
                             f"{version or '(no current release)'}")
        return plan
    if rel.status == STATUS_WITHDRAWN:
        plan.blockers.append(f"harness {rel.version} is withdrawn by its publisher")
    if channel.board.pack and board.pack and channel.board.pack != board.pack:
        plan.blockers.append(f"the channel is for {channel.board.pack} boards, this is {board.pack}")
    if rel.compat.min_app and not at_least(app_version, rel.compat.min_app):
        plan.blockers.append(f"harness {rel.version} needs harness-manager >= {rel.compat.min_app} "
                             f"(this is {app_version}); run `harness-manager update app` first")
    if board.sd_revisions and rel.compat.board_revs:
        supported = {r.upper() for r in rel.compat.board_revs}
        if not supported & {r.upper() for r in board.sd_revisions}:
            plan.blockers.append(f"the config SD is for {', '.join(board.sd_revisions)}; harness "
                                 f"{rel.version} supports {', '.join(rel.compat.board_revs)}")
    if board.mcc_firmware and rel.compat.mcc_fw_tested and \
            board.mcc_firmware not in rel.compat.mcc_fw_tested:
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
    downgrade = False
    if board.identity_known and ident.harness_version and rel.identity.harness and \
            _same_u32(rel.identity.static_id, ident.shell_id or "0x0"):
        downgrade = (compare_safe(rel.identity.harness, ident.harness_version) or 0) < 0
    if downgrade and version is None:
        plan.warnings.append(f"the board runs harness {ident.harness_version}, newer than the "
                             f"channel's current {rel.version}; nothing to do")
        base_needed = False
    elif downgrade:
        plan.warnings.append(f"this is a ROLLBACK from harness {ident.harness_version} to "
                             f"{rel.version} (from the signed release history)")
    os_comp = next((c for c in rel.components if c.kind == KIND_OS_SLOT), None)
    os_needed = os_comp is not None and (
        not board.os_active_sha or board.os_active_sha.lower() != os_comp.asset.sha256)
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

    if plan.base and not board.has_storage:
        plan.blockers.append("installing the harness base writes the config SD: it needs the "
                             "MPS3 Debug USB (the V2M-MPS3 volume)")
    if plan.base and not board.has_controller:
        plan.blockers.append("the new base runs only after a board REBOOT: it needs the Debug USB "
                             "MCC console")
    if plan.os_slot and not board.has_os_slots:
        plan.blockers.append("this release carries an OS slot image, but this harness offers no "
                             "OS slot update (a Linux harness with the slot verbs is needed)")

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
    if plan.os_slot:
        s.append(PlanStep("write-os-slot", "write the INACTIVE OS slot, arm try-once "
                                           "(stage0 rolls back if it does not confirm)"))
    if plan.base:
        sd = rel.by_target(TARGET_MCC_SD)[0]
        s.append(PlanStep("backup-sd", "back up the whole config SD (mandatory gate)", sd.name))
        s.append(PlanStep("install-sd", "write the new base to the config SD, journaled, "
                                        "read back; never an .ebf", sd.name))
    if plan.base or plan.os_slot:
        budget = (f"up to {plan.reboot_wait_s:.0f} s" if plan.reboot_wait_s
                  else "the pack's budget for this harness")
        s.append(PlanStep("reboot", "reboot the board and witness it go down and come back "
                                    f"({budget})"))
        s.append(PlanStep("confirm-identity", f"the board must report shell "
                                              f"{rel.identity.static_id}, harness "
                                              f"{rel.identity.harness or rel.version}"))
    if plan.os_slot:
        s.append(PlanStep("confirm-os-slot", "confirm the new slot so stage0 keeps it"))
