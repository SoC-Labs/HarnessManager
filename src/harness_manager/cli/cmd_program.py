"""Partition programming: overlays, program, restore (all through ``engine.deploy``)."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    ExitCode,
    HarnessError,
    HeldError,
)
from harness_manager.core.model import Check
from harness_manager.core.pack import (
    DeployResult,
    OverlayRef,
    PreflightItem,
    card_status_of,
    keep_refusal,
)

from .context import Ctx
from .output import Result, with_data


def overlay_env_name(pack: str) -> str:
    """The pack's overlay search-path variable (``HARNESS_MANAGER_MPS3_OVERLAY_DIRS``)."""
    return f"HARNESS_MANAGER_{pack.upper()}_OVERLAY_DIRS"


def _setting_dirs(ctx: Ctx) -> str:
    """The pack's ``<pack>.overlay_dirs`` from the settings when its variable is unset (lane
    SET-WIRE), joined as the variable is; "" when the pack declares none."""
    try:
        r = ctx.engine.settings_resolver().resolve(f"{ctx.pack}.overlay_dirs")
    except Exception:  # noqa: BLE001 - no such row, or a remote engine: only the flag's
        return ""
    return os.pathsep.join(str(d) for d in (r.value or []))


@contextmanager
def overlay_dirs(ctx: Ctx) -> Iterator[None]:
    """``--overlay-dir DIR`` (repeatable): put DIRs first on the pack's overlay search
    path for the length of the verb, ahead of the variable's directories, else those the
    settings name (``mps3.overlay_dirs``). The variable is restored afterwards."""
    dirs = [str(Path(d)) for d in (getattr(ctx.args, "overlay_dir", None) or ())]
    if not dirs:
        yield
        return
    for d in dirs:
        if not Path(d).is_dir():
            raise AbsentError(f"no overlay directory at {d}",
                              hint="give a directory that holds <rm>/manifest.json")
    name = overlay_env_name(ctx.pack)
    old = os.environ.get(name)
    rest = old if old else _setting_dirs(ctx)
    os.environ[name] = os.pathsep.join(dirs + ([rest] if rest else []))
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


SERVICE_NAME = "harness-manager-daemon"

#: FIX-PACK-7 (DEBUG-DOWN-FIRST): ``program``/``restore --force``.
FORCE_HELP = ("swap even when OpenOCD on the board cannot be stopped first (`mps3-debug down` "
              "failed): a warning instead of the refusal (exit 15)")


def _force(ctx: Ctx) -> dict[str, bool]:
    """``{"force": True}`` with ``--force``, else nothing: the keyword only when asked."""
    return {"force": True} if getattr(ctx.args, "force", False) else {}


def _service_holds(exc: HeldError) -> bool:
    """The board's lock is the Harness Manager service's (or a service runs here)."""
    if SERVICE_NAME in f"{exc.holder} {exc.hint} {exc.message}":
        return True
    try:
        from harness_manager.daemon.state import discover

        return discover() is not None
    except Exception:  # noqa: BLE001 - no state dir, or a broken record: say nothing more
        return False


@contextmanager
def board_for(ctx: Ctx) -> Iterator[tuple[Any, Any]]:
    """``overlay_dirs`` then ``ctx.board()``. ``--overlay-dir`` keeps the verb in this process
    (the directory is this process's), so while the service holds the board the open is
    refused by its lock: say how to give the service the directory instead (SERIAL-6900 4b)."""
    with overlay_dirs(ctx), ExitStack() as stack:
        try:
            pair = stack.enter_context(ctx.board())
        except HeldError as exc:
            dirs = [str(Path(d)) for d in (getattr(ctx.args, "overlay_dir", None) or ())]
            if not dirs or "is in use" not in exc.message or not _service_holds(exc):
                raise
            joined = os.pathsep.join(dirs)
            raise HeldError(
                f"{exc.message}: --overlay-dir runs in this process, and the Harness Manager "
                "service holds the board", holder=exc.holder,
                hint=f"give the service the directory instead: `harness-manager config set "
                     f"{ctx.pack}.overlay_dirs {joined}` (read at its next listing), then run "
                     "this again without --overlay-dir; or close the board in the app "
                     "first") from exc
        yield pair


def cmd_overlays(ctx: Ctx) -> int:
    with board_for(ctx) as (cand, session):
        loadable, refused = ctx.engine.deploy.compatible(session)
    rows, human = [], []
    for o in loadable:
        rows.append([cand.board_id, o.name, "compatible", o.rm_id, o.static_id, o.size_bytes,
                     o.ip_class, ""])
        carries = [role for role, sha in (("ltx", o.ltx_sha256), ("receipt", o.receipt_sha256))
                   if sha]
        human.append(f"ok         {o.name:<20} {o.rm_id}  ({', '.join([o.ip_class, *carries])})")
    for name, why in sorted(refused.items()):
        rows.append([cand.board_id, name, "incompatible", "", "", "", "", why])
        human.append(f"cannot     {name:<20} {why}")
    ctx.emit(Result("overlays", {
        "board_id": cand.board_id,
        "compatible": list(loadable),
        "incompatible": [{"name": n, "reason": w} for n, w in sorted(refused.items())],
    }, rows=rows, human=human or ["no overlays known for this board"]))
    return ExitCode.OK


def _find_overlay(refs: Sequence[OverlayRef], spec: str) -> OverlayRef:
    for o in refs:
        if o.name == spec:
            return o
    try:
        wanted = int(spec, 16) if spec.lower().startswith("0x") else None
    except ValueError:
        wanted = None
    if wanted is not None:
        for o in refs:
            try:
                if int(o.rm_id, 16) == wanted:
                    return o
            except ValueError:
                continue
    names = ", ".join(sorted(o.name for o in refs)) or "none"
    raise AbsentError(f"no overlay named {spec!r} for this board",
                      hint=f"known: {names} (`harness-manager overlays TARGET` shows which load)")


def preflight_lines(items: Sequence[PreflightItem]) -> list[str]:
    marks = {Check.OK: "ok      ", Check.MISMATCH: "MISMATCH", Check.UNCHECKED: "unchecked"}
    lines = ["preflight:"]
    for i in items:
        tail = " (not a pass)" if i.check == Check.UNCHECKED else ""
        detail = f"  {i.detail}" if i.detail else ""
        lines.append(f"  {marks.get(i.check, i.check.value):<9} {i.name}{detail}{tail}")
    return lines


def preflight_refusal(items: Sequence[PreflightItem], overlay_name: str) -> HarnessError | None:
    """The core rule (``harness_manager.core.pack.preflight_refusal``), shared with the GUI and service.

    Identity items are marked by the deploy service. When items arrive
    unmarked, the service's ``mark_identity`` is applied if it is installed.
    """
    from harness_manager.core.pack import preflight_refusal as core_rule

    try:
        from harness_manager.services.deploy import mark_identity
    except ImportError:
        return core_rule(items, overlay_name)
    return core_rule(mark_identity(items), overlay_name)


def _result_rows(board_id: str, overlay: str, r: DeployResult) -> list:
    return [board_id, overlay, r.rm_id, r.verified, round(r.seconds, 3), r.transport]


def card_line(r: DeployResult, overlay: str) -> str:
    """The deploy report's card line, for a deploy that was asked to keep its design."""
    if r.card is None:
        return "not kept on the card: the deploy reported nothing about the card"
    if r.card.kept:
        slot = f" (slot {r.card.slot})" if r.card.slot else ""
        return f"kept on the card{slot}: the board boots into {overlay} next time"
    return f"not kept on the card: {r.card.why or 'no reason given'}"


def _card_cell(r: DeployResult, asked: bool) -> str:
    """TSV CARD: "" (printed "-") when not asked, "kept:<slot>" or "not kept: <why>"."""
    if not asked:
        return ""
    if r.card is not None and r.card.kept:
        return f"kept:{r.card.slot}"
    return f"not kept: {r.card.why if r.card else 'not reported'}"


def _check_verified(board_id: str, r: DeployResult, **data: object) -> None:
    if not r.verified:
        raise with_data(ActionFailedError(
            f"{board_id} did not confirm the new design ({r.rm_id or 'no rm_id'})",
            hint="run `harness-manager info TARGET`; treat the board as unknown until it confirms"),
            result=r, **data)


def cmd_program(ctx: Ctx) -> int:
    with board_for(ctx) as (cand, session):
        deploy = ctx.engine.deploy
        overlay = _find_overlay(list(deploy.overlays(session)), ctx.args.rm)
        items = list(deploy.preflight(session, overlay))
        for line in preflight_lines(items):
            ctx.note(line)
        refused = preflight_refusal(items, overlay.name)
        if refused is not None:                 # refuse BEFORE deploy() is ever called
            raise with_data(refused, overlay=overlay, preflight=items)
        keep = bool(getattr(ctx.args, "keep_on_card", False))
        also = ""
        if keep:                                # the card must take it, before anything
            card = card_status_of(deploy, session)
            refused = keep_refusal(card)
            if refused is not None:
                raise with_data(refused, overlay=overlay, card=card)
            ctx.note(f"card: {card.text or card.state or 'present'}; the design will be "
                     "kept on it")
            also = " and keep it on the card"
        ctx.confirm(f"program {overlay.name} ({overlay.rm_id}) into {cand.board_id}{also}?")
        with ctx.bus_progress(cand.board_id, "deploy"):
            # The keywords only when asked: the default never writes the card, never forces.
            result = (deploy.deploy(session, overlay, keep_on_card=True, **_force(ctx)) if keep
                      else deploy.deploy(session, overlay, **_force(ctx)))
    _check_verified(cand.board_id, result, overlay=overlay, preflight=items)
    human = [f"programmed {overlay.name} ({result.rm_id}) into {cand.board_id} in "
             f"{result.seconds:.1f}s via {result.transport or '?'}; verified"]
    if keep:
        human.append(card_line(result, overlay.name))
    ctx.emit(Result("program", {
        "board_id": cand.board_id, "overlay": overlay, "preflight": items, "result": result,
    }, rows=[[*_result_rows(cand.board_id, overlay.name, result), _card_cell(result, keep)]],
        human=human))
    return ExitCode.OK


def cmd_restore(ctx: Ctx) -> int:
    with board_for(ctx) as (cand, session):
        with ctx.bus_progress(cand.board_id, "deploy"):
            result = ctx.engine.deploy.restore_baseline(session, **_force(ctx))
    _check_verified(cand.board_id, result)
    row = _result_rows(cand.board_id, "", result)
    ctx.emit(Result("restore", {"board_id": cand.board_id, "result": result},
                    rows=[[row[0]] + row[2:]], human=[
        f"restored   {cand.board_id} to the baseline ({result.rm_id}) in {result.seconds:.1f}s; "
        "verified",
    ]))
    return ExitCode.OK
