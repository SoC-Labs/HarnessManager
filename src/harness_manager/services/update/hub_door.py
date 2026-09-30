"""The planner's half of the hub door (lane HUB-SD; HARNESS-DIST §6 (b), H10; U9, U10).

A board whose config SD is on the hub (its Debug USB is plugged into the hub, not into
this machine) can still take a new harness base: the board pack's ``hub_sd`` door writes
the one file the hub can write, and the pack's controller REBOOTs the board ON the hub
(never through a share on tty_00: MCC-FIX). The pack describes the door in ``BoardView.hub_sd`` (``available``,
``reason``, ``target``, ``hub``, ``only_paths``, the lease ``holder``/``mine``/``queue``);
this module decides whether a plan goes through it and says what that means:

- **when**: ``via="hub"`` asks for it; with no ``via``, a base update on a board with no
  local Debug USB goes through the hub when the pack offers the door. ``via="usb"`` never.
- **blockers**: the door unavailable (no ``sd`` method, the hub down, no MCC on it); the
  running release unknown (the door's backup is that release's ``.bit`` from the signed
  cache: fpgahub cannot back up the SD); the running release's SD part private and no
  token; a signed SD file list that differs outside ``only_paths``;
- **consent**: a typed phrase that names the board, the lease holder and the queue
  (``board_phrase``), besides the re-key phrase when there is one;
- **auto-revert** (U10): armed by default for this remote door (D6a), off when the user
  says so at approval; it needs the backup, which this door always has.

The core never learns what an MCC or a ``nanosoc.bit`` is: the pack's description names
the paths, the door and the hub.
"""

from __future__ import annotations

from typing import Any

from .schema import KIND_SD, TARGET_MCC_SD, Channel, Component, HarnessRelease

VIA_HUB = "hub"
VIA_LOCAL = "usb"
VIAS = ("", VIA_HUB, VIA_LOCAL)
#: How long the executor waits for ping/version after the REBOOT before it calls the board
#: dark (U10). The reboot witness has already waited its own budget by then.
DARK_AFTER_S = 60.0


def _sd(rel: HarnessRelease | None) -> Component | None:
    if rel is None:
        return None
    return next((c for c in rel.by_target(TARGET_MCC_SD) if c.kind == KIND_SD), None)


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()


def signed_delta(new: Component | None, old: Component | None) -> list[str] | None:
    """The SD paths whose signed sha differs, from the components' ``files`` lists; None
    when either list is missing (the executor measures it after download instead)."""
    if new is None or old is None or not new.files or not old.files:
        return None
    a = {_norm(k): (k, v.lower()) for k, v in new.files.items()}
    b = {_norm(k): (k, v.lower()) for k, v in old.files.items()}
    return sorted((a.get(k) or b[k])[0] for k in set(a) | set(b)
                  if (a.get(k) or ("", ""))[1] != (b.get(k) or ("", ""))[1])


def wants_hub(plan: Any, board: Any, via: str | None) -> bool:
    """Does this plan's base go through the hub door?"""
    if not plan.base or via == VIA_LOCAL:
        return False
    if via == VIA_HUB:
        return True
    door = getattr(board, "hub_sd", None) or {}
    return bool(door) and not (board.has_storage and board.has_controller)


def board_phrase(door: dict[str, Any]) -> str:
    """The typed consent for a remote install: the board, the lease holder, the queue."""
    target = door.get("target") or "the board"
    holder = door.get("holder") or "nobody"
    queued = len(door.get("queue") or [])
    return f"INSTALL {target} HELD BY {holder} {queued} QUEUED"


def consent_text(door: dict[str, Any], version: str) -> str:
    target = door.get("target") or "the board"
    holder = door.get("holder") or "nobody"
    queue = [q.get("holder", "?") if isinstance(q, dict) else str(q)
             for q in door.get("queue") or []]
    waiting = (f"{len(queue)} queued ({', '.join(queue[:3])}{' …' if len(queue) > 3 else ''}) "
               "wait until it is done" if queue else "nobody is queued")
    here = door.get("here") if door.get("here") is not None else door.get("mine")
    mine = " (you)" if here else " (you, in another session)" if door.get("mine") else ""
    return (f"This writes harness {version} to the config SD of {target} through the hub "
            f"{door.get('hub') or '?'} and REBOOTs it: the lease is held by {holder}{mine}; "
            f"{waiting}. Type exactly: {board_phrase(door)}")


def apply(plan: Any, rel: HarnessRelease, channel: Channel, board: Any, *, via: str | None,
          running: HarnessRelease | None, have_token: bool) -> None:
    """Route ``plan``'s base through the hub door when it applies (the module doc)."""
    if via not in (None, *VIAS):
        plan.blockers.append(f"unknown install door {via!r}: give hub or usb")
        return
    if not wants_hub(plan, board, via):
        return
    door = dict(getattr(board, "hub_sd", None) or {})
    plan.via = VIA_HUB
    if not door:
        plan.blockers.append("this board has no hub door: it is not behind a hub (boards.toml "
                             "hub table), so its config SD needs the MPS3 Debug USB here")
        return
    if not door.get("available"):
        plan.blockers.append(f"the hub door is not available: {door.get('reason') or 'unknown'}")
    new_sd = _sd(rel)
    old_sd = _sd(running)
    backup = {}
    if running is None:
        plan.blockers.append(
            "the hub door keeps the running release's nanosoc.bit as its backup (fpgahub cannot "
            "back up the SD), but the board's running release is unrecorded on this channel: "
            "install it with the Debug USB here, or publish the running release first")
    elif old_sd is None:
        plan.blockers.append(f"harness {running.version} (running) has no config-SD part to "
                             "keep as the hub door's backup")
    elif old_sd.needs_token and not have_token:
        plan.blockers.append(f"the backup (harness {running.version}'s SD part) is private: it "
                             "needs a GitHub token with access to it")
    else:
        backup = {"version": running.version, "component": old_sd.name,
                  "sha256": old_sd.asset.sha256}
    only = {_norm(p) for p in door.get("only_paths") or ()}
    delta = signed_delta(new_sd, old_sd)
    if delta is not None and only and any(_norm(p) not in only for p in delta):
        extra = [p for p in delta if _norm(p) not in only]
        plan.blockers.append(
            f"harness {rel.version} changes more of the config SD than the hub can write "
            f"({', '.join(extra[:4])}{' …' if len(extra) > 4 else ''}): it needs the MPS3 "
            "Debug USB here")
    elif delta is None and only:
        plan.warnings.append("the config-SD delta is checked after the download: the hub door "
                             f"writes only {', '.join(sorted(door.get('only_paths') or ()))}, "
                             "and refuses a release that changes anything else")
    lease_required = door.get("lease_required", True)
    # FIX-PACK-4: held HERE (the token is this Harness Manager's), not by principal; a door
    # description from before ``here`` reads ``mine``, as ``services.lease.held_here``.
    held = door.get("here") if door.get("here") is not None else door.get("mine")
    if lease_required and not held:
        plan.warnings.append(f"only the lease holder installs through the hub: "
                             f"{door.get('holder') or 'nobody'} holds {door.get('target') or 'it'}"
                             " (the install refuses anyone else)")
    plan.hub = {k: door.get(k) for k in ("door", "hub", "target", "transport", "holder", "mine",
                                          "here", "queue", "mcc_tty", "only_paths")}
    plan.hub["backup"] = backup
    plan.hub["consent_text"] = consent_text(door, rel.version)
    plan.hub["dark_after_s"] = DARK_AFTER_S
    plan.board_phrase = board_phrase(door)
    plan.auto_revert = bool(backup)          # D6a: armed by default on a remote door


def steps(plan: Any, rel: HarnessRelease) -> list[tuple[str, str, str]]:
    """The hub door's steps (``action, detail, component``), in place of backup + install."""
    sd = rel.by_target(TARGET_MCC_SD)[0]
    hub = plan.hub or {}
    b = hub.get("backup") or {}
    host, target = hub.get("hub") or "the hub", hub.get("target") or "the board"
    out = [
        ("backup-sd", f"keep harness {b.get('version') or '?'}'s nanosoc.bit (signed, from the "
                      "cache) as the backup: fpgahub cannot back up the SD", sd.name),
        ("hub-upload", f"upload nanosoc.bit to {host} and check its sha256 there", sd.name),
        ("hub-write", f"ONE `fpgahub target program {target} --method sd --force`: a client "
                      "timeout here is expected (the hub keeps writing); never retried", sd.name),
        ("hub-verify", "wait for the hub's own record of the write (the completion event or "
                       "journal line naming our sha256); never the client's exit code", sd.name),
    ]
    return out


def revert_step(plan: Any) -> tuple[str, str]:
    hub = plan.hub or {}
    b = hub.get("backup") or {}
    return ("auto-revert", f"if the board answers neither ping nor version "
                           f"{hub.get('dark_after_s', DARK_AFTER_S):.0f} s after the REBOOT: write "
                           f"harness {b.get('version') or '?'}'s nanosoc.bit back through the hub "
                           "and REBOOT (armed at approval; on by default)")
