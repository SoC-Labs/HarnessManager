"""MBBIOS is never changed by Harness Manager (FIX-PACK-7; the Linux lead's request, platform
item G8).

``MB/HBI0309C/board.txt`` names the board controller's BIOS image: ``MBBIOS: mbb_v141.ebf``.
An MCC that finds the ``.ebf`` that line names on its SD updates ITSELF to it at the next
boot. A bundle whose board.txt names ``mbb_v141.ebf`` could so move a third-party MCC that
has that file from 1.3.2 to 1.4.1, and this tooling is proven on 1.3.2 only. So every path
that writes the config SD's board.txt goes through ``keep_mbbios`` first:

- **the card has an MBBIOS line**: the card's line replaces the bundle's (or is put into a
  bundle board.txt that has none), verbatim: "MBBIOS kept: <value>";
- **the card has no MBBIOS line (or no board.txt)**, and the bundle's line names an ``.ebf``
  that is NOT on the card: the bundle's line goes in unchanged, since the MCC has nothing to
  update to: "MBBIOS: <value> from the bundle (the card has no <file>, so the MCC will not
  update)";
- **the card has no MBBIOS line, and the ``.ebf`` IS on the card**: refused (exit 15), "this
  card would make the MCC update itself to <file>: remove <file> from the card, or add
  --allow-mcc-update". ``allow_mcc_update`` writes it with a warning.

A board.txt with the line REMOVED is never written: whether MCC 1.3.2 runs without it is
unverified (the Linux lead). The ``.ebf`` itself is never written (``sd.py``).

Callers: ``sd.Mps3Storage.install`` (``sd install``, the API's storage install, the update's
Debug USB door), ``sd_ab.AbStorage.install`` and ``hub_sd.HubSdDoor.install`` (they never
write board.txt, so a board.txt that differs from the card's only by MBBIOS no longer
blocks them), and SD-FLASH's card-reader ``files`` kind. The note goes to the progress
callback (phase ``mbbios``, ``detail.text``) and to the adapter's ``install_notes``.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from harness_manager.core.errors import RefusedError

MB_DIR = "MB/HBI0309C"
BOARD_TXT = f"{MB_DIR}/board.txt"
#: The key, case-blind as the MCC's own parser; the value is the first word after the colon.
_LINE = re.compile(r"^[ \t]*MBBIOS[ \t]*:[ \t]*([^\s;]+)[^\r\n]*", re.I | re.M)
_SECTION = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][^\r\n]*", re.M)
ALLOW_FLAG = "--allow-mcc-update"
#: The refusal's ``error.data`` key (the app and the API read it).
DATA_KEY = "mcc_update"

KEPT, FROM_BUNDLE, ALLOWED, NO_LINE = "kept", "bundle", "allowed", "none"


@dataclass(frozen=True)
class MbbiosDecision:
    """What ``decide`` did to a board.txt: ``action`` (``KEPT``, ``FROM_BUNDLE``, ``ALLOWED``,
    ``NO_LINE``: neither side has one), the value written, the text to show (``note``), and
    the board.txt to write."""

    action: str
    value: str
    note: str
    content: bytes


def mbbios_line(text: str) -> tuple[str, str] | None:
    """``(the whole line, its value)`` of the first MBBIOS line, else None."""
    m = _LINE.search(text)
    return (m.group(0), m.group(1)) if m else None


def _norm(rel: str) -> str:
    return rel.replace("\\", "/").strip("/").lower()


def _ebf_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _on_card(value: str, card_files: Iterable[str]) -> bool:
    """Is the ``.ebf`` an MBBIOS value names on the card? Case-blind (FAT), anywhere on it:
    the MCC looks in ``MB/HBI0309C``, and a copy elsewhere is refused too, to be safe."""
    want = _ebf_name(value).lower()
    return any(_norm(f).rsplit("/", 1)[-1] == want for f in card_files)


def _insert(bundle: str, line: str) -> str:
    """Put the card's MBBIOS line into a bundle board.txt that has none: after its
    ``[MCCS]`` header when it has one (where the MCC's own file keeps it), else before its
    first section header, else at the end."""
    nl = "\r\n" if "\r\n" in bundle else "\n"
    for m in _SECTION.finditer(bundle):
        if m.group(1).strip().upper() == "MCCS":
            return bundle[:m.end()] + nl + line + bundle[m.end():]
    first = _SECTION.search(bundle)
    if first is not None:
        return bundle[:first.start()] + line + nl + bundle[first.start():]
    return bundle + ("" if not bundle or bundle.endswith(("\n", "\r")) else nl) + line + nl


def decide(bundle_board_txt: bytes, *, card_board_txt: bytes | None,
           card_files: Iterable[str], allow_mcc_update: bool = False) -> MbbiosDecision:
    """The board.txt to write for ``bundle_board_txt`` on a card whose board.txt is
    ``card_board_txt`` (None: none) and whose files are ``card_files`` (SD-relative paths).
    Raises the refusal (15) for a card that would make the MCC update itself."""
    bundle = bundle_board_txt.decode("latin-1")
    card = card_board_txt.decode("latin-1") if card_board_txt is not None else ""
    ours = mbbios_line(bundle)
    theirs = mbbios_line(card) if card_board_txt is not None else None
    if theirs is not None:
        line, value = theirs
        text = bundle.replace(ours[0], line, 1) if ours is not None else _insert(bundle, line)
        return MbbiosDecision(KEPT, value, f"MBBIOS kept: {value}", text.encode("latin-1"))
    if ours is None:
        return MbbiosDecision(NO_LINE, "", "", bundle_board_txt)
    value = ours[1]
    ebf = _ebf_name(value)
    if not _on_card(value, card_files):
        return MbbiosDecision(FROM_BUNDLE, value,
                              f"MBBIOS: {value} from the bundle (the card has no {ebf}, so the "
                              "MCC will not update)", bundle_board_txt)
    if not allow_mcc_update:
        err = RefusedError(
            f"this card would make the MCC update itself to {ebf}: remove {ebf} from the card, "
            f"or add {ALLOW_FLAG}",
            hint=f"the card's board.txt has no MBBIOS line, the bundle's names {value}, and "
                 f"{ebf} is on the card: the MCC would flash it at its next boot (Harness "
                 "Manager is proven on MCC 1.3.2 only); nothing was written")
        err.data = {DATA_KEY: {"file": ebf, "value": value}}  # type: ignore[attr-defined]
        raise err
    return MbbiosDecision(ALLOWED, value,
                          f"MBBIOS: {value} from the bundle, allowed by {ALLOW_FLAG}: the card "
                          f"has {ebf}, so the MCC may update itself to it at its next boot",
                          bundle_board_txt)


def keep_mbbios(files: Mapping[str, Path], *, card_board_txt: bytes | None,
                card_files: Iterable[str], workdir: Path,
                allow_mcc_update: bool = False) -> tuple[dict[str, Path], MbbiosDecision | None]:
    """THE substitution, for every writer of the config SD: ``files`` (SD path -> local
    file) with its board.txt (if any, FAT case-blind) replaced by ``decide``'s, written into
    ``workdir`` (which must outlive the write). Returns the files to write and the decision
    (None: no board.txt among ``files``). Raises the refusal (15)."""
    out = dict(files)
    key = next((k for k in files if _norm(k) == _norm(BOARD_TXT)), None)
    if key is None:
        return out, None
    got = decide(Path(files[key]).read_bytes(), card_board_txt=card_board_txt,
                 card_files=card_files, allow_mcc_update=allow_mcc_update)
    if got.content != Path(files[key]).read_bytes():
        dest = Path(tempfile.mkdtemp(prefix="mbbios-", dir=workdir)) / "board.txt"
        dest.write_bytes(got.content)
        out[key] = dest
    return out, got


__all__ = ["ALLOW_FLAG", "ALLOWED", "BOARD_TXT", "DATA_KEY", "FROM_BUNDLE", "KEPT",
           "MbbiosDecision", "NO_LINE", "decide", "keep_mbbios", "mbbios_line"]
