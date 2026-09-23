"""Partition programming: overlays, program, restore (all through ``engine.deploy``)."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    ExitCode,
    HarnessError,
)
from harness_manager.core.model import Check
from harness_manager.core.pack import DeployResult, OverlayRef, PreflightItem

from .context import Ctx
from .output import Result, with_data


def overlay_env_name(pack: str) -> str:
    """The pack's overlay search-path variable (``HARNESS_MANAGER_MPS3_OVERLAY_DIRS``)."""
    return f"HARNESS_MANAGER_{pack.upper()}_OVERLAY_DIRS"


@contextmanager
def overlay_dirs(ctx: Ctx) -> Iterator[None]:
    """``--overlay-dir DIR`` (repeatable): put DIRs first on the pack's overlay search
    path for the length of the verb. The variable is restored afterwards."""
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
    os.environ[name] = os.pathsep.join(dirs + ([old] if old else []))
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def cmd_overlays(ctx: Ctx) -> int:
    with overlay_dirs(ctx), ctx.board() as (cand, session):
        loadable, refused = ctx.engine.deploy.compatible(session)
    rows, human = [], []
    for o in loadable:
        rows.append([cand.board_id, o.name, "compatible", o.rm_id, o.static_id, o.size_bytes,
                     o.ip_class, ""])
        human.append(f"ok         {o.name:<20} {o.rm_id}  ({o.ip_class})")
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


def _check_verified(board_id: str, r: DeployResult, **data: object) -> None:
    if not r.verified:
        raise with_data(ActionFailedError(
            f"{board_id} did not confirm the new design ({r.rm_id or 'no rm_id'})",
            hint="run `harness-manager info TARGET`; treat the board as unknown until it confirms"),
            result=r, **data)


def cmd_program(ctx: Ctx) -> int:
    with overlay_dirs(ctx), ctx.board() as (cand, session):
        deploy = ctx.engine.deploy
        overlay = _find_overlay(list(deploy.overlays(session)), ctx.args.rm)
        items = list(deploy.preflight(session, overlay))
        for line in preflight_lines(items):
            ctx.note(line)
        refused = preflight_refusal(items, overlay.name)
        if refused is not None:                 # refuse BEFORE deploy() is ever called
            raise with_data(refused, overlay=overlay, preflight=items)
        ctx.confirm(f"program {overlay.name} ({overlay.rm_id}) into {cand.board_id}?")
        with ctx.bus_progress(cand.board_id, "deploy"):
            result = deploy.deploy(session, overlay)
    _check_verified(cand.board_id, result, overlay=overlay, preflight=items)
    ctx.emit(Result("program", {
        "board_id": cand.board_id, "overlay": overlay, "preflight": items, "result": result,
    }, rows=[_result_rows(cand.board_id, overlay.name, result)], human=[
        f"programmed {overlay.name} ({result.rm_id}) into {cand.board_id} in "
        f"{result.seconds:.1f}s via {result.transport or '?'}; verified",
    ]))
    return ExitCode.OK


def cmd_restore(ctx: Ctx) -> int:
    with overlay_dirs(ctx), ctx.board() as (cand, session):
        with ctx.bus_progress(cand.board_id, "deploy"):
            result = ctx.engine.deploy.restore_baseline(session)
    _check_verified(cand.board_id, result)
    row = _result_rows(cand.board_id, "", result)
    ctx.emit(Result("restore", {"board_id": cand.board_id, "result": result},
                    rows=[[row[0]] + row[2:]], human=[
        f"restored   {cand.board_id} to the baseline ({result.rm_id}) in {result.seconds:.1f}s; "
        "verified",
    ]))
    return ExitCode.OK
