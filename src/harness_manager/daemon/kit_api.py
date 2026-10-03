"""DUT build kits and the build guide over the daemon API (KIT-CORE), loaded through
``app.EXTENSIONS``. docs/API.md "DUT build kits and the build guide" is the contract.

| Method and path | Returns |
|---|---|
| ``GET /kits`` | ``{kits: [summary], sources}`` |
| ``GET /kits/{static_id}`` | ``{kit: summary, manifest: kit.json, checks}`` (the blobs re-hashed); 404 when not cached |
| ``POST /kits/fetch`` ``{static_id?, board_id?, source?}`` | 202 job ``kit_fetch``; ``kit.progress`` events; the result is ``{kit, source, checks}`` |
| ``POST /kits/import`` ``{path}`` | ``{kit, already, checks}`` |
| ``POST /kits/{static_id}/export`` ``{out_dir}`` | ``{out_dir, written}`` |
| ``GET /kits/{static_id}/zip`` | ``application/zip``: ``<sid>/kit.json`` and every kit file |
| ``GET /boards/{bid}/kit`` | ``{static_id, cached, kit, profile, checks, sources, vivado}`` for the board's live static |
| ``GET /guide?pack=&static_id=&design=&build_dir=`` | the guide: ``{steps: [{id, n, title, state, detail, reason, actions, checks}], next, ...}`` |
| ``GET /boards/{bid}/guide?design=&build_dir=`` | the same, with the board's live static and identity |
| ``POST /guide/script`` ``{static_id, design, pack?, out_dir?, kit_dir?, jobs?, stop_after?, format?}`` | the build directory's files (``format: "zip"``: a zip with the kit) |
| ``POST /kits/check`` ``{path, clearing?, static_id?, board_id?}`` | ``{passed, checks, facts}`` (200 either way: a query) |
| ``POST /kits/pack`` ``{path, out_dir?, import?}`` | ``{overlay_dir, imported}``; 409 REFUSED with ``error.data.checks`` |
| ``POST /overlays/import`` ``{path, board_id?, static_id?, check_only?}`` | UI2 G5: ``{kind, path, name, rm_id, static_id, passed, checks, groups, overlay_dir, imported}``; 409 INCOMPATIBLE/REFUSED with ``error.data`` = the same |
| ``POST /overlays/import-folder`` ``{path, board_id?, static_id?, check_only?}`` | QUICKWINS G3: every design under a folder: ``{path, check_only, results: [{name, path, state: imported or skipped or refused or ready, reason, rm_id, static_id}], counts}``; 404 ABSENT when it holds none |
| ``POST /overlays/upload?name=&board_id=&static_id=&check_only=`` (body: the zip) | UI2 G5: the same as ``/overlays/import``, plus ``upload: {name, bytes}``; 413 over 256 MB |
| ``POST /kits/design/scan`` ``{path, name?, top?, static_id?, board_id?, rm_id?, out?}`` | UI2 G8 (e): ``{path, kind, name, top, tops, sources, include_dirs, defines, packages, generics, use, ports, rm_id, rm_id_proposed, left_out, warnings, design, written}`` |

UI2 G8 (additive): the guide adds ``running``, ``pblock`` and ``utilisation``; ``POST /kits/check``
adds ``facts.utilisation`` and ``facts.pblock``; ``POST /guide/script`` adds ``run`` ("Run it your
way": batch, the Vivado GUI, your open Vivado).

Paths (``path``, ``out_dir``, ``build_dir``, a ``design`` file) are ABSOLUTE paths on the
daemon's host: the build runs there (david K6: Vivado on the user's machine, driven by
HM), and the daemon listens on loopback. A ``design`` may also be a built-in name or an
inline design object. The kits live in the engine's content store.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from harness_manager.cli.output import with_data
from harness_manager.core.errors import UsageError
from harness_manager.core.events import Event
from harness_manager.core.pack import KitCheck, kit_refusal
from harness_manager.services.kit import (
    KitService,
    build,
    floorplan,
    guide,
    overlay_import,
    rtl_scan,
    script,
    vivado,
)
from harness_manager.services.kit.schema import hex32, parse_u32

from .app import _JSON, JsonBody, RouteContext, _abs_path, _obj, ok

ENGINE = ""
STAGES = ("preflight", "synth", "link", "impl", "verify", "bitstream")


def kit_service(engine: Any) -> KitService:
    """The kit service over the engine's content store (a real ``ContentStore``), else the
    state dir's (the demo engine keeps its store in memory)."""
    from harness_manager.engine import resolve_state_dir
    from harness_manager.services.store import ContentStore

    state = Path(getattr(engine, "state_dir", None) or resolve_state_dir())
    try:
        store = engine.store
    except Exception:  # noqa: BLE001 - an engine without a store: use the state dir's
        store = None
    if not isinstance(store, ContentStore):
        store = ContentStore(state / "store")
    from harness_manager.services.update.kits import kit_channel_source

    # OTA-C: the signed channel's rm-kit, through the update service (token, mirrors). An
    # engine may bring its own (the demo's reads its local catalogue, never the network).
    channel = getattr(engine, "kit_channel", None) or kit_channel_source(state)
    return KitService(store, state / "kits", channel=channel)


def static_arg(value: Any, key: str = "static_id") -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(f"{key} must be a string such as 0x72BB0A36")
    try:
        return hex32(parse_u32(value))
    except ValueError:
        raise UsageError(f"{key} {value!r} is not a 32-bit hex id") from None


def design_arg(value: Any) -> str | dict[str, Any] | None:
    if value is None or isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        raise UsageError("design must be a built-in design's name, an inline design object, "
                         "or an absolute path to a design .json")
    if "/" in value or "\\" in value or value.endswith(".json"):
        return str(_abs_path(value, "design"))
    return value


def checks_json(checks: list[KitCheck]) -> list[dict[str, Any]]:
    return [c.__dict__ for c in checks]


def refuse(checks: list[KitCheck], what: str) -> None:
    exc = kit_refusal(checks, what)
    if exc is not None:
        raise with_data(exc, checks=checks_json(checks))


def board_identity(ctx: RouteContext, bid: str) -> Any:
    """What the board said when probed, else one identity read under the board gate."""
    s = ctx.board(bid)
    ident = getattr(getattr(s, "candidate", None), "identity", None)
    if ident is not None and getattr(ident, "shell_id", ""):
        return ident
    with ctx.daemon.gates.op(bid):            # 409 HELD while a job runs on the board
        return s.identity()


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    kits = kit_service(d.engine)

    def pack_of(bid: str) -> str:
        return str(getattr(getattr(ctx.board(bid), "candidate", None), "pack", "") or "mps3")

    # -- the cache ---------------------------------------------------------------------------

    @api.get("/kits")
    def kits_list() -> Any:
        return _JSON(ok(kits=[k.summary() for k in kits.list()], sources=kits.sources()))

    @api.get("/kits/{static_id}")
    def kits_one(static_id: str) -> Any:
        kit = kits.require(static_arg(static_id))
        return _JSON(ok(kit=kit.summary(), manifest=kit.manifest.to_json(),
                        checks=checks_json(kits.verify_cached(kit))))

    @api.post("/kits/fetch")
    def kits_fetch(body: JsonBody = None) -> Any:
        b = _obj(body)
        source = b.get("source")
        if source is not None and not (isinstance(source, str) and source):
            raise UsageError("source must be cache, channel, hub or an absolute path")
        if source and source not in ("cache", "channel", "hub"):
            source = str(_abs_path(source, "source"))
        ident = None
        if b.get("board_id"):
            ident = board_identity(ctx, str(b["board_id"]))
        sid = b.get("static_id")
        if sid is None and ident is None:
            raise UsageError("the request needs static_id or board_id")
        sid = static_arg(sid) if sid is not None else hex32(parse_u32(ident.shell_id))

        def run(progress: Any) -> Any:
            def prog(phase: str, done: int, total: int) -> None:
                progress(phase, done, total)
                d.bus.publish(Event("kit.progress", str(b.get("board_id") or ""),
                                    {"static_id": sid, "phase": phase, "bytes": done,
                                     "total": total}))

            prog("fetch", 0, 0)
            kit, src = kits.fetch(sid, source=source, progress=prog)
            checks = kits.verify_cached(kit) + kits.check_against_board(kit.manifest, ident)
            refuse(checks, f"the kit for {sid}")
            d.bus.publish(Event("kit.stored", "", {"static_id": sid, "source": src,
                                                  "kit_id": kit.manifest.kit_id}))
            return {"kit": kit.summary(), "source": src, "checks": checks_json(checks)}

        return ctx.accepted(d.jobs.submit("kit_fetch", ENGINE, run))

    @api.post("/kits/import")
    def kits_import(body: JsonBody = None) -> Any:
        b = _obj(body)
        path = _abs_path(b.get("path"), "path")
        res = kits.import_(path, source=f"path:{path}")
        d.bus.publish(Event("kit.stored", "", {"static_id": res.kit.static_id,
                                              "source": res.kit.source,
                                              "kit_id": res.kit.manifest.kit_id}))
        return _JSON(ok(kit=res.kit.summary(), already=res.already,
                        checks=checks_json(res.checks)))

    @api.post("/kits/{static_id}/export")
    def kits_export(static_id: str, body: JsonBody = None) -> Any:
        b = _obj(body)
        out = _abs_path(b.get("out_dir"), "out_dir")
        written = kits.export(kits.require(static_arg(static_id)), out)
        return _JSON(ok(out_dir=str(out), written=[str(p) for p in written]))

    @api.get("/kits/{static_id}/zip")
    def kits_zip(static_id: str) -> Any:
        kit = kits.require(static_arg(static_id))
        kits.work_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="kit-", suffix=".zip", dir=kits.work_dir)
        with contextlib.suppress(OSError):
            os.close(fd)
        path = kits.zip_to(kit, Path(name))
        sid = hex32(kit.manifest.static_u32)
        return FileResponse(path, media_type="application/zip",
                            filename=f"mps3-kit-{sid}.zip" if kit.manifest.board_type == "mps3"
                            else f"kit-{sid}.zip",
                            background=BackgroundTask(lambda: path.unlink(missing_ok=True)))

    @api.get("/boards/{bid:path}/kit")
    def board_kit(bid: str) -> Any:
        ident = board_identity(ctx, bid)
        pack = pack_of(bid)
        sid = hex32(parse_u32(ident.shell_id)) if ident.shell_id else ""
        kit = kits.get(sid) if sid else None
        profile = kits.profile(pack, sid) if sid else None
        rel = profile.vivado if profile else ""
        found = vivado.discover(want=rel)                  # KIT-RC2: the kit's release first
        checks = [vivado.check_release(found, rel, profile.vivado_build if profile else 0)]
        pc = vivado.check_path(found, rel)
        checks += [pc] if pc is not None else []
        if kit is not None:
            checks += kits.check_against_board(kit.manifest, ident, pack)
        return _JSON(ok(board_id=bid, static_id=sid or None, cached=kit is not None,
                        kit=kit.summary() if kit else None,
                        profile=profile.__dict__ if profile else None,
                        checks=checks_json(checks), sources=kits.sources(),
                        vivado=found.to_json()))

    # -- the guide -----------------------------------------------------------------------------

    def run_guide(pack: str, static_id: str | None, design: Any, build_dir: str | None,
                  ident: Any = None, bid: str = "") -> Any:
        g = guide.guide(kits, pack=pack, static_id=static_id, identity=ident, board_id=bid,
                        design=design_arg(design),
                        build_dir=_abs_path(build_dir, "build_dir") if build_dir else None,
                        store=kits.store)
        return _JSON(ok(**g.to_json()))

    @api.get("/guide")
    def guide_get(pack: str = "mps3", static_id: str | None = None, design: str | None = None,
                  build_dir: str | None = None) -> Any:
        return run_guide(pack, static_arg(static_id) if static_id else None, design, build_dir)

    @api.get("/boards/{bid:path}/guide")
    def board_guide(bid: str, design: str | None = None, build_dir: str | None = None) -> Any:
        ident = board_identity(ctx, bid)
        return run_guide(pack_of(bid), None, design, build_dir, ident, bid)

    @api.post("/guide/script")
    def guide_script(body: JsonBody = None) -> Any:
        b = _obj(body)
        fmt = b.get("format", "json")
        if fmt not in ("json", "zip"):
            raise UsageError("format must be json or zip")
        stop = b.get("stop_after", "bitstream")
        if stop not in STAGES:
            raise UsageError(f"stop_after must be one of {', '.join(STAGES)}")
        jobs = b.get("jobs", 2)
        if isinstance(jobs, bool) or not isinstance(jobs, int) or not 1 <= jobs <= 64:
            raise UsageError("jobs must be an integer 1-64")
        design = design_arg(b.get("design"))
        if design is None:
            raise UsageError("the request needs design")
        out = _abs_path(b["out_dir"], "out_dir") if b.get("out_dir") else None
        kit_dir = _abs_path(b["kit_dir"], "kit_dir") if b.get("kit_dir") else None
        s = script.make_script(kits, pack=str(b.get("pack") or "mps3"),
                               static_id=static_arg(b.get("static_id")), design=design,
                               out_dir=out, kit_dir=kit_dir, jobs=jobs, stop_after=stop,
                               store=kits.store)
        if fmt == "zip":
            return Response(_script_zip(kits, s), media_type="application/zip",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{s.design}_build.zip"'})
        return _JSON(ok(**s.to_json()))

    # -- after the build -----------------------------------------------------------------------

    @api.post("/kits/check")
    def kits_check(body: JsonBody = None) -> Any:
        b = _obj(body)
        path = _abs_path(b.get("path"), "path")
        ident = board_identity(ctx, str(b["board_id"])) if b.get("board_id") else None
        checks, facts, sid = check_path(kits, path, clearing=b.get("clearing"),
                                        static_id=b.get("static_id"), ident=ident)
        passed = kit_refusal(checks, "") is None
        add_floorplan(facts, sid)
        return _JSON(ok(passed=passed, static_id=sid, checks=checks_json(checks), facts=facts))

    @api.post("/kits/pack")
    def kits_pack(body: JsonBody = None) -> Any:
        b = _obj(body)
        path = _abs_path(b.get("path"), "path")
        do_import = b.get("import", False)
        if not isinstance(do_import, bool):
            raise UsageError("import must be true or false")
        r = build.load(path)
        checks, _facts, _sid = check_path(kits, path)
        refuse(checks, f"the build {r.rm_name}")
        base = r.path.parent.parent if r.path.parent.name == "out" else r.path.parent
        out_root = _abs_path(b["out_dir"], "out_dir") if b.get("out_dir") else base / "overlay"
        adapter = kits.adapter_for(r.kit_id.split("/", 1)[0] if "/" in r.kit_id else "mps3")
        od = adapter.pack_receipt(r, out_root)
        imported = adapter.import_overlay(kits.store, od) if do_import else None
        return _JSON(ok(overlay_dir=str(od), manifest=str(od / "manifest.json"),
                        imported=imported, checks=checks_json(checks)))

    # -- UI2-API-BUILD G5: import a design from a path (the Import dialog) ----------------------

    @api.post("/overlays/import")
    def overlays_import(body: JsonBody = None) -> Any:
        b = _obj(body)
        path = _abs_path(b.get("path"), "path")
        check_only = b.get("check_only", False)
        if not isinstance(check_only, bool):
            raise UsageError("check_only must be true or false")
        sid = static_arg(b["static_id"]) if b.get("static_id") else ""
        bid = str(b["board_id"]) if b.get("board_id") else ""
        ident = board_identity(ctx, bid) if bid else None
        pack = pack_of(bid) if bid else "mps3"
        if check_only:
            res = overlay_import.check_design(kits, path, identity=ident, static_id=sid,
                                              pack=pack, store=kits.store)
        else:
            res = overlay_import.import_design(kits, path, identity=ident, static_id=sid,
                                               pack=pack, store=kits.store)
            d.bus.publish(Event("kit.imported", bid, {
                "name": res.name, "rm_id": res.rm_id, "static_id": res.static_id,
                "kind": res.kind, "sha256": (res.imported or {}).get("sha256", "")}))
        return _JSON(ok(board_id=bid or None, **res.to_json()))

    @api.post("/overlays/import-folder")
    def overlays_import_folder(body: JsonBody = None) -> Any:
        """QUICKWINS G3: every design under a folder (a release's ``overlays/``), each imported,
        skipped (another static) or refused with its reason. One bad design never stops the rest."""
        b = _obj(body)
        path = _abs_path(b.get("path"), "path")
        check_only = b.get("check_only", False)
        if not isinstance(check_only, bool):
            raise UsageError("check_only must be true or false")
        sid = static_arg(b["static_id"]) if b.get("static_id") else ""
        bid = str(b["board_id"]) if b.get("board_id") else ""
        ident = board_identity(ctx, bid) if bid else None
        pack = pack_of(bid) if bid else "mps3"
        out = overlay_import.import_folder(kits, path, identity=ident, static_id=sid, pack=pack,
                                           store=kits.store, check_only=check_only)
        for r in out["results"]:
            if r["state"] == overlay_import.STATE_IMPORTED:
                d.bus.publish(Event("kit.imported", bid, {
                    "name": r["name"], "rm_id": r["rm_id"], "static_id": r["static_id"],
                    "kind": "overlay", "sha256": (r.get("imported") or {}).get("sha256", "")}))
        return _JSON(ok(board_id=bid or None, **out))

    @api.post("/overlays/upload")
    async def overlays_upload(request: Request, name: str = "design.zip",
                              board_id: str | None = None, static_id: str | None = None,
                              check_only: str | None = None) -> Any:
        """The Import dialog's "Choose a zip": the browser sends the file's bytes (a browser
        never reveals a path). Streamed to the kit work dir (413 above the cap), then as
        ``/overlays/import`` (``overlay_import.import_zip``)."""
        from starlette.concurrency import run_in_threadpool

        from .app import _query_flag

        only = _query_flag(check_only, "check_only")
        sid = static_arg(static_id) if static_id else ""
        cap = overlay_import.MAX_UPLOAD_BYTES
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            return too_large(int(declared), cap)
        kits.work_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".upload-", suffix=".zip", dir=kits.work_dir)
        size = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap:
                        return too_large(size, cap)
                    fh.write(chunk)
            if size == 0:
                raise UsageError("the upload is empty", hint="send the zip's bytes as the body "
                                                             "(Content-Type: application/zip)")

            def work() -> Any:
                ident = board_identity(ctx, board_id) if board_id else None
                pack = pack_of(board_id) if board_id else "mps3"
                return overlay_import.import_zip(kits, Path(tmp), name=name, identity=ident,
                                                 static_id=sid, pack=pack, store=kits.store,
                                                 check_only=only)

            res = await run_in_threadpool(work)
        finally:
            Path(tmp).unlink(missing_ok=True)
        if not only:
            d.bus.publish(Event("kit.imported", board_id or "", {
                "name": res.name, "rm_id": res.rm_id, "static_id": res.static_id,
                "kind": res.kind, "sha256": (res.imported or {}).get("sha256", "")}))
        return _JSON(ok(board_id=board_id or None, upload={"name": name, "bytes": size},
                        **res.to_json()))

    # -- UI2-API-BUILD G8 (e): My RTL, a folder or a .f list into a design -----------------------

    @api.post("/kits/design/scan")
    def design_scan(body: JsonBody = None) -> Any:
        b = _obj(body)
        path = _abs_path(b.get("path"), "path")
        for key in ("name", "top", "rm_id"):
            if b.get(key) is not None and not (isinstance(b[key], str) and b[key]):
                raise UsageError(f"{key} must be a non-empty string")
        bid = str(b["board_id"]) if b.get("board_id") else ""
        sid = static_arg(b["static_id"]) if b.get("static_id") else ""
        if bid and not sid:
            ident = board_identity(ctx, bid)
            sid = hex32(parse_u32(ident.shell_id)) if ident.shell_id else ""
        pack = pack_of(bid) if bid else "mps3"
        out = _abs_path(b["out"], "out") if b.get("out") else None
        adapter = kits.adapter_for(pack)
        found = rtl_scan.scan(path, name=b.get("name") or "", top=b.get("top") or "",
                              pack=pack, static_id=sid, rm_id=b.get("rm_id") or "",
                              adapter=adapter, store=kits.store)
        if out is not None:
            rtl_scan.write_design(found, out)
        return _JSON(ok(static_id=sid or None, **found.to_json()))


def too_large(size: int, cap: int) -> Any:
    """413 with the error envelope: the upload is over the cap (nothing was kept)."""
    from .wire import error_body

    return _JSON(error_body(UsageError(
        f"the upload is {size} bytes; the cap is {cap // (1024 * 1024)} MB",
        hint="give the folder's path instead (Import > A path on this machine)")),
        status_code=413)


def add_floorplan(facts: dict[str, Any], static_id: str, pack: str = "mps3") -> None:
    """UI2 G8 (b) (c): the pblock's facts and the receipt's ``<name>_util.rpt`` against it,
    into ``kit check``'s facts (additive; None when there is none)."""
    pblock = floorplan.pblock_facts(pack, static_id) if static_id else None
    facts["pblock"] = pblock
    receipt = facts.get("receipt") or {}
    util = None
    path = receipt.get("path") if isinstance(receipt, dict) else None
    name = receipt.get("rm_name") if isinstance(receipt, dict) else None
    if path and name and build.plain_name(str(name)):
        rpt = Path(path).parent / f"{name}_util.rpt"
        util = floorplan.utilisation(rpt, pblock) if rpt.is_file() else None
    facts["utilisation"] = util


def check_path(kits: KitService, path: Path, *, clearing: Any = None, static_id: Any = None,
               ident: Any = None) -> tuple[list[KitCheck], dict[str, Any], str]:
    """``kit check`` for the API (and the T14 mock): ``build.check_any`` with API arguments."""
    clr = _abs_path(clearing, "clearing") if clearing else None
    sid = static_arg(static_id) if static_id else ""
    checks, facts, sid, _r = build.check_any(kits, path, clearing=clr, static_id=sid,
                                             identity=ident)
    return checks, facts, sid


def _script_zip(kits: KitService, s: script.BuildScript) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in sorted(s.files.items()):
            zf.writestr(f"{s.design}/{name}", text)
        kit = kits.require(s.static_id)
        zf.writestr(f"{s.design}/{script.KIT_SUBDIR}/kit.json", kit.manifest.dumps())
        for f in kit.manifest.files:
            zf.write(kit.blobs[f.path], f"{s.design}/{script.KIT_SUBDIR}/{f.path}",
                     compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()

