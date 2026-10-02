"""An UNSIGNED harness write needs the typed ``INSTALL UNSIGNED <first 8 hex of its sha256>``
(david 2 Oct: D3a, then "same rule" for every door).

Every write of something Harness Manager cannot verify the origin of goes through here, wizard
or CLI, config SD or whole card: a bundle folder or zip (the bring-up's source, ``flash write
--kind files``) and a whole-card image (the Linux user microSD, ``flash write --kind card``).
Signed releases (the catalogue, a mirror) never do, and nothing here touches their trust.

The sha256 names what is written, so a person can check it on their own machine:

- a ``.zip``: the zip file's own (``sha256sum BUNDLE.zip``);
- any other FILE (a whole-card image): the file's own (``sha256sum CARD.img``);
- a FOLDER: the sha256 of its MANIFEST: one ``sha256sum`` line ``<sha256>  <path>`` per regular
  file under it (found without following a symbolic link, as ``find -type f``; the path
  relative to the folder, ``/``-separated; a name with a backslash or a newline escaped as GNU
  ``sha256sum`` escapes it), sorted by the path's bytes (``LC_ALL=C sort``), which
  ``MANIFEST_RECIPE`` prints too. A folder with a symbolic link is refused by its callers
  (the sha256 would not cover what the link points to).

``require`` takes the phrase for the thing AS IT IS NOW (any spacing, the hex in either case)
or refuses (15, ``error.data.unsigned``). ``--yes`` never implies it.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, RefusedError

WORDS = "INSTALL UNSIGNED"
BANNER = ("Unsigned: Harness Manager cannot check where this came from; only install a bundle "
          "you built or got from SoC Labs directly.")
ZIP_RECIPE = "sha256sum BUNDLE.zip"
FILE_RECIPE = "sha256sum CARD.img"
MANIFEST_RECIPE = ("cd FOLDER && find . -type f -printf '%P\\0' | LC_ALL=C sort -z | "
                   "xargs -0 sha256sum | sha256sum")
#: What the refusal calls each kind of thing.
NOUNS = {"zip": "bundle", "manifest": "bundle", "file": "card image"}


def phrase(sha256: str) -> str:
    """``INSTALL UNSIGNED <first 8 hex of sha256>``."""
    return f"{WORDS} {sha256[:8].lower()}"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sum_line(sha256: str, rel: bytes) -> bytes:
    """One line as GNU ``sha256sum`` prints it: ``<hex>  <name>``; a name with a backslash or
    a newline is escaped and the line starts with a backslash (coreutils 8.x)."""
    if b"\\" in rel or b"\n" in rel:
        return b"\\" + sha256.encode() + b"  " + \
            rel.replace(b"\\", b"\\\\").replace(b"\n", b"\\n") + b"\n"
    return sha256.encode() + b"  " + rel + b"\n"


def folder_manifest(root: Path) -> tuple[bytes, dict[str, str], list[str]]:
    """A FOLDER's manifest (module docstring). Returns (the manifest, each file's sha256 by its
    path, the symbolic links found)."""
    entries: list[tuple[bytes, str, Path]] = []
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = here / name
            rel = path.relative_to(root).as_posix()
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            if stat.S_ISLNK(mode):
                links.append(rel)
            elif stat.S_ISREG(mode):
                entries.append((os.fsencode(rel), rel, path))
    entries.sort(key=lambda e: e[0])
    sums = {rel: file_sha256(path) for _raw, rel, path in entries}
    manifest = b"".join(_sum_line(sums[rel], raw) for raw, rel, _p in entries)
    return manifest, sums, sorted(links)


@dataclass(frozen=True)
class Unsigned:
    """What an unsigned write names: ``path`` (as given), its ``sha256``, ``of`` (``zip``,
    ``manifest`` or ``file``), the files a manifest lists, and a folder's symbolic links."""

    path: str
    sha256: str
    of: str
    files: int | None = None
    links: tuple[str, ...] = field(default_factory=tuple)

    @property
    def phrase(self) -> str:
        return phrase(self.sha256)

    @property
    def noun(self) -> str:
        return NOUNS.get(self.of, "bundle")

    @property
    def how(self) -> str:
        return {"zip": ZIP_RECIPE, "file": FILE_RECIPE}.get(self.of, MANIFEST_RECIPE)

    def as_dict(self) -> dict[str, Any]:
        return {"phrase": self.phrase, "sha256": self.sha256, "of": self.of,
                "files": self.files if self.of == "manifest" else None, "how": self.how,
                "banner": BANNER, "path": self.path}


def of_path(path: Path | str) -> Unsigned:
    """The sha256 of what ``path`` is (a folder's manifest, a zip's or a file's own)."""
    p = Path(path)
    if p.is_dir():
        manifest, sums, links = folder_manifest(p)
        return Unsigned(str(p), hashlib.sha256(manifest).hexdigest(), "manifest", len(sums),
                        tuple(links))
    if not p.is_file():
        raise AbsentError(f"nothing at {p}", hint="give the folder, the .zip or the image file")
    return Unsigned(str(p), file_sha256(p), "zip" if p.suffix.lower() == ".zip" else "file")


def links_refusal(info: Unsigned) -> RefusedError | None:
    """A folder with a symbolic link: its sha256 would not cover what the link points to."""
    if not info.links:
        return None
    more = f" and {len(info.links) - 5} more" if len(info.links) > 5 else ""
    return RefusedError(f"{', '.join(info.links[:5])}{more}: a symbolic link; a bundle carries "
                        "only regular files (its sha256 covers the files, not what a link "
                        "points to)", hint="copy the files in, or zip the folder; nothing was "
                                           "written")


def matches(want: str, typed: Any) -> bool:
    """The typed phrase as the doors compare it: any spacing, the hex in either case."""
    got = " ".join(typed.split()) if isinstance(typed, str) else ""
    return bool(want) and (got == want or (got[:-8] == want[:-8]
                                           and got[-8:].lower() == want[-8:]))


def require(info: Unsigned, typed: Any) -> None:
    """The typed ``INSTALL UNSIGNED <sha8>`` for ``info``, else REFUSED (15) with
    ``error.data.unsigned``; nothing is written."""
    from harness_manager.cli.output import with_data

    want = info.phrase
    if matches(want, typed):
        return
    got = " ".join(typed.split()) if isinstance(typed, str) else ""
    if got.upper().startswith(WORDS):
        msg = (f"not confirmed: {got!r} does not name this {info.noun}: its sha256 starts "
               f"{info.sha256[:8]}; type exactly {want!r}")
    else:
        msg = f"not confirmed: this {info.noun} is unsigned; type exactly {want!r} to install it"
    raise with_data(RefusedError(msg, hint=f"Harness Manager cannot check where an unsigned "
                                           f"{info.noun} came from: only install one you built "
                                           "or got from SoC Labs directly. The phrase names the "
                                           "first 8 hex of its sha256 (error.data.unsigned); "
                                           "nothing was written"),
                    unsigned=info.as_dict())


def still_same(info: Unsigned) -> None:
    """Just before the write: ``info.path`` still has the sha256 its phrase named."""
    now = of_path(info.path)
    if now.sha256 != info.sha256:
        from harness_manager.cli.output import with_data

        raise with_data(RefusedError(
            f"{info.path} changed since its phrase was typed: its sha256 now starts "
            f"{now.sha256[:8]}, not {info.sha256[:8]}",
            hint="check it again and type its new phrase; nothing was written"),
            unsigned=now.as_dict())
