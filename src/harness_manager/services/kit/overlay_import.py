"""Import a design from a path on this machine (lane UI2-API-BUILD, gap G5).

docs/API.md "Import a design, and the build's progress, floorplan and utilisation"
(``POST /overlays/import``) and the Import dialog (UI_V2_PLAN.md M6). Two kinds of path:

- **a packed overlay folder** (``manifest.json`` + the partial and clearing it names, as
  ``kit pack`` writes it, or a lab's fielded overlay): the pack checks it
  (``KitAdapter.check_overlay``), then it goes into the content store as
  ``kit pack --import`` puts one (``KitAdapter.import_overlay``);
- **a build receipt** (``out/<name>_build.json``) or a build directory holding one: the
  checks ``kit check`` runs (``build.check_any``), then ``kit pack --import``: the overlay
  folder is written from the receipt beside the build (``<build>/overlay/<name>``) and
  imported.

Both are held to the same five groups the dialog shows (``GROUPS``): the files, the same
shell as the board (an identity check: a mismatch refuses with 14 INCOMPATIBLE), the rm_id
(the user range 0x8000-0xFFFF and a clash with the catalogue are warnings, never a refusal:
a rebuilt platform design keeps its id), the lengths and CRC-32, and the pair (the
clearing's frames inside the partial's, the harness's clearing arena, the frame box when the
static's kit is cached). Any ``mismatch`` refuses and nothing is imported.

A zip is the upload's (``import_zip``, ``POST /overlays/upload``): the browser sends the
file's bytes (it never reveals a path); they are extracted safely (``update.bundle.
safe_extract``: no absolute path, no ``..``, no symlink, a size and entry cap) into the kit
work dir, the folder or receipt inside is found (at the top, one folder down, or the first
``manifest.json``/receipt within three levels), checked and imported the same way, and the
extraction is removed whatever happens. A path to a zip is refused (400) with that hint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, HarnessError, UsageError
from harness_manager.core.pack import KitCheck, kit_refusal

from . import build
from .schema import hex32, parse_u32, same_id

KIND_OVERLAY = "overlay"
KIND_RECEIPT = "receipt"

#: The dialog's five rows: (id, title), in order.
GROUPS = (("files", "Files"), ("shell", "Same shell"), ("rm_id", "rm_id in the user range"),
          ("crc", "CRC and sizes"), ("pair", "Clearing pairs with the partial"))
#: A check's group by its name; anything else (the pair's stream checks) is ``pair``.
GROUP_OF = {"manifest": "files", "build": "files", "static_id": "files", "timing": "files",
            "board_static": "shell", "expected_static": "shell",
            "rm_id": "rm_id", "rm_id_range": "rm_id", "rm_id_clash": "rm_id",
            "crc": "crc", "partial": "crc", "clearing": "crc", "ltx": "crc"}
_RANK = {"ok": 0, "warning": 1, "unchecked": 2, "mismatch": 3}


def kind_of(path: Path) -> tuple[str, Path]:
    """(kind, the folder or receipt to read). ``AbsentError`` for nothing importable there,
    ``UsageError`` for a path to a zip (it goes through ``import_zip``)."""
    p = Path(path)
    if p.is_dir():
        if (p / "manifest.json").is_file():
            return KIND_OVERLAY, p
        if build.find_receipts(p):
            return KIND_RECEIPT, p
        raise AbsentError(f"{p} holds no manifest.json and no build receipt",
                          hint="give a kit-built overlay folder (manifest.json + the pair), "
                               "a build receipt (out/<name>_build.json) or its build directory")
    if p.is_file():
        if p.name == "manifest.json":
            return KIND_OVERLAY, p.parent
        if p.suffix.lower() == ".json":
            return KIND_RECEIPT, p
        if p.suffix.lower() == ".zip":
            raise UsageError(f"{p.name} is a zip: give its folder, or upload the zip",
                             hint="unzip it and give the folder, or send the zip itself to "
                                  "POST /overlays/upload (the Import dialog's Choose a zip)")
        raise UsageError(f"{p.name} is neither an overlay folder nor a build receipt",
                         hint="give manifest.json's folder, or out/<name>_build.json")
    raise AbsentError(f"no such file or folder: {p}", hint="give an absolute path on this machine")


def groups(checks: list[KitCheck]) -> list[dict[str, Any]]:
    """The five rows: each with the worst state of its checks (``unchecked`` when none ran)
    and the check names in it."""
    rows = []
    for gid, title in GROUPS:
        mine = [c for c in checks if GROUP_OF.get(c.name.split(":", 1)[0], "pair") == gid]
        worst = max(mine, key=lambda c: _RANK.get(c.state, 2), default=None)
        state = worst.state if worst is not None else "unchecked"
        bad = [c for c in mine if c.state == state] if worst is not None else []
        rows.append({"id": gid, "title": title, "state": state,
                     "detail": bad[0].detail if bad and state != "ok" else
                     (mine[0].detail if len(mine) == 1 else
                      f"{len(mine)} checks passed" if mine else "not checked"),
                     "checks": [c.name for c in mine]})
    return rows


@dataclass
class ImportResult:
    kind: str
    path: Path
    name: str = ""
    rm_id: str = ""
    static_id: str = ""
    checks: list[KitCheck] = field(default_factory=list)
    overlay_dir: Path | None = None
    imported: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return kit_refusal(self.checks, "") is None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": str(self.path), "name": self.name,
                "rm_id": self.rm_id, "static_id": self.static_id, "passed": self.passed,
                "checks": [c.__dict__ for c in self.checks], "groups": groups(self.checks),
                "overlay_dir": str(self.overlay_dir) if self.overlay_dir else None,
                "imported": self.imported}


def _shell_checks(static_id: str, identity: Any, expect: str) -> list[KitCheck]:
    """The design's static against the board's (``board_static``) and the one asked for
    (``expected_static``); both identity checks (a mismatch is exit 14)."""
    out: list[KitCheck] = []
    live = str(getattr(identity, "shell_id", "") or "") if identity is not None else ""
    if live:
        same = bool(static_id) and same_id(live, static_id)
        out.append(KitCheck("board_static", "ok" if same else "mismatch",
                            f"built for {static_id or 'no static'}; the board runs {live}"
                            + ("" if same else ": a partial for another static can destroy the "
                                               "FPGA's configuration"), identity=True))
    if expect:
        try:
            want = hex32(parse_u32(expect))
        except (TypeError, ValueError):
            raise UsageError(f"static_id {expect!r} is not a 32-bit hex id") from None
        same = bool(static_id) and same_id(want, static_id)
        out.append(KitCheck("expected_static", "ok" if same else "mismatch",
                            f"built for {static_id or 'no static'}; you asked for {want}",
                            identity=True))
    if not out:
        out.append(KitCheck("board_static", "unchecked",
                            "no board named: the shell was not compared (import it for a board)",
                            identity=True))
    return out


def check_design(kits: Any, path: Path, *, identity: Any = None, static_id: str = "",
                 pack: str = "mps3", store: Any = None) -> ImportResult:
    """Every check of an import, with nothing written (``import_design`` runs this first)."""
    kind, where = kind_of(path)
    adapter = kits.adapter_for(pack)
    if kind == KIND_OVERLAY:
        def kit_for(sid: str) -> Any:
            kit = kits.get(hex32(parse_u32(sid))) if sid else None
            return kit.manifest if kit is not None else None

        checks, facts = adapter.check_overlay(where, kit_for=kit_for)
        sid = str(facts.get("static_id") or "")
        res = ImportResult(KIND_OVERLAY, where, str(facts.get("name") or ""),
                           str(facts.get("rm_id") or ""), sid, list(checks))
    else:
        checks, _facts, sid, r = build.check_any(kits, where, identity=identity, pack=pack)
        name = r.rm_name if r is not None else ""
        rm = r.get("rm_id") if r is not None else ""
        res = ImportResult(KIND_RECEIPT, r.path if r is not None else where, name,
                           hex32(parse_u32(rm)) if rm else "", sid, list(checks))
    if not any(c.state == "mismatch" and c.name in ("manifest", "build") for c in res.checks):
        have = {c.name for c in res.checks}
        res.checks += [c for c in _shell_checks(res.static_id, identity, static_id)
                       if c.name not in have]
        if res.rm_id:
            taken = adapter.taken_designs(store)
            res.checks += [c for c in adapter.rm_id_checks(res.rm_id, res.name, taken)
                           if c.name not in {x.name for x in res.checks}]
    return res


def import_design(kits: Any, path: Path, *, identity: Any = None, static_id: str = "",
                  pack: str = "mps3", store: Any = None, out_dir: Path | None = None
                  ) -> ImportResult:
    """``check_design``, then into the store when every check passes. A refusal raises
    (``kit_refusal``: 14 INCOMPATIBLE for the shell, else 15 REFUSED) with the result's JSON
    as ``error.data`` (``result``), and nothing is written."""
    res = check_design(kits, path, identity=identity, static_id=static_id, pack=pack,
                       store=store)
    exc = kit_refusal(res.checks, f"importing {res.name or Path(path).name}")
    if exc is not None:
        exc.data = res.to_json()  # type: ignore[attr-defined]
        raise exc
    adapter = kits.adapter_for(pack)
    store = store if store is not None else kits.store
    if res.kind == KIND_OVERLAY:
        res.overlay_dir = res.path
    else:
        r = build.load(res.path)
        base = r.path.parent.parent if r.path.parent.name == "out" else r.path.parent
        res.overlay_dir = adapter.pack_receipt(r, out_dir or base / "overlay")
    res.imported = adapter.import_overlay(store, res.overlay_dir)
    return res


#: The largest upload taken (the plan's cap; 413 above it) and how deep a zip's design may sit.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
_ZIP_DEPTH = 3


def zip_root(root: Path) -> Path:
    """Where the importable folder or receipt of an extracted zip is: ``root`` itself, its
    one folder, else the shallowest ``manifest.json`` (then ``*_build.json``) within
    ``_ZIP_DEPTH`` levels. ``AbsentError`` when there is none."""
    for cand in (root, *([d for d in root.iterdir() if d.is_dir()]
                         if len([x for x in root.iterdir() if not x.name.startswith(".")]) == 1
                         else [])):
        try:
            kind_of(cand)
            return cand
        except (AbsentError, UsageError):
            continue
    for pattern in ("manifest.json", build.RECEIPT_GLOB):
        found = sorted((p for p in root.rglob(pattern)
                        if len(p.relative_to(root).parts) <= _ZIP_DEPTH + 1),
                       key=lambda p: (len(p.parts), str(p)))
        if found:
            return found[0].parent if pattern == "manifest.json" else found[0]
    raise AbsentError("the zip holds no manifest.json and no build receipt",
                      hint="zip the kit-built overlay folder (manifest.json + the pair), or "
                           "the build directory with its out/<name>_build.json")


def import_zip(kits: Any, archive: Path, *, name: str = "", identity: Any = None,
               static_id: str = "", pack: str = "mps3", store: Any = None,
               check_only: bool = False) -> ImportResult:
    """An uploaded zip, checked (and with ``check_only`` False, imported) as ``import_design``
    does a path; the extraction is removed afterwards, always. The result's ``path`` is the
    upload's ``name`` and its ``overlay_dir`` None (the overlay is in the store)."""
    import shutil
    import uuid

    from harness_manager.services.update.bundle import safe_extract

    work = Path(kits.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / f".upload-{uuid.uuid4().hex}"
    try:
        safe_extract(Path(archive), tmp)
        root = zip_root(tmp)
        if check_only:
            res = check_design(kits, root, identity=identity, static_id=static_id, pack=pack,
                               store=store)
        else:
            try:
                res = import_design(kits, root, identity=identity, static_id=static_id,
                                    pack=pack, store=store)
            except HarnessError as exc:
                data = getattr(exc, "data", None)
                if isinstance(data, dict):
                    data.update(path=name or Path(archive).name, overlay_dir=None)
                raise
        res.path = Path(name or Path(archive).name)
        res.overlay_dir = None
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
