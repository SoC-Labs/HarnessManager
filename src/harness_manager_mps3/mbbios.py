"""MBBIOS is never changed by Harness Manager (FIX-PACK-7; the Linux lead's request, platform
item G8), in ANY revision's folder (FIX-PACK-9).

``MB/HBI0309<rev>/board.txt`` names the board controller's BIOS image: ``MBBIOS: mbb_v141.ebf``.
The MCC reads only the folder of the revision it detects (``MB/HBI0309C`` on a Rev C board,
``MB/HBI0309B`` on a Rev B), so a bundle for several revisions (platform v2.0.0 ships B and C,
identical apart from the ``BOARD:`` line) carries one board.txt per revision, and the rule
below is applied to EACH of them against the SAME revision's board.txt on the card.
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
unverified (the Linux lead). The ``.ebf`` itself is never written (``sd.py``). Only the
first MBBIOS line of a file counts, and a bundle's second one never reaches the card; DOS
line endings are read as the MCC reads them.

Per revision (FIX-PACK-9): a bundle revision whose folder the card lacks takes the "card
has no MBBIOS line" branch, and its ``.ebf`` is looked for on the WHOLE card. One revision
written gives that revision's decision, as before ("MBBIOS kept: mbb_v132.ebf"); several
give one combined decision whose note names each ("MBBIOS kept: HBI0309C mbb_v132.ebf;
MBBIOS: HBI0309B mbb_v141.ebf from the bundle (…)") and whose ``revs`` holds each one. Any
revision that would make the MCC update itself refuses the whole write. A card with
neither an ``HBI0309B`` nor an ``HBI0309C`` folder is written with a warning
(``MbbiosDecision.warnings``). Folders the bundle does not carry (an Arm ``HBI0309A`` tree)
are never touched.

Callers: ``sd.Mps3Storage.install`` (``sd install``, the API's storage install, the update's
Debug USB door), ``sd_ab.AbStorage.install`` and ``hub_sd.HubSdDoor.install`` (they never
write board.txt, so a board.txt that differs from the card's only by MBBIOS no longer
blocks them), and SD-FLASH's card-reader ``files`` kind. The note goes to the progress
callback (phase ``mbbios``, ``detail.text``) and to the adapter's ``install_notes``; each
warning follows it the same way.

The card side is ``card_boards`` (every ``MB/HBI*/board.txt`` on the card, by SD path:
``card_boards_of`` reads them from a mounted card). A caller from before FIX-PACK-9 passes
``card_board_txt`` (the card's ``MB/HBI0309C/board.txt`` only): a bundle revision whose
board.txt is on the card but was not passed is then refused, never guessed.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from harness_manager.core.errors import RefusedError

#: The revision the pre-FIX-PACK-9 callers mean (``card_board_txt``), and every MPS3 lab
#: board's (LOG.TXT "MotherBoard Revision C Variant A").
MB_DIR = "MB/HBI0309C"
BOARD_TXT = f"{MB_DIR}/board.txt"
#: The revisions a config SD serves for the MPS3 (platform v2.0.0: both; Rev A: none).
MPS3_REVS = ("HBI0309B", "HBI0309C")
#: The key, case-blind as the MCC's own parser; the value is the first word after the colon.
_LINE = re.compile(r"^[ \t]*MBBIOS[ \t]*:[ \t]*([^\s;]+)[^\r\n]*", re.I | re.M)
_SECTION = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][^\r\n]*", re.M)
#: ``MB/<revision folder>/board.txt`` (any case, either slash): the folder is the revision.
_BOARD_REL = re.compile(r"^mb/(hbi[0-9a-z]+)/board\.txt$", re.I)
_REV_DIR = re.compile(r"^mb/(hbi[0-9a-z]+)/", re.I)
ALLOW_FLAG = "--allow-mcc-update"
#: The refusal's ``error.data`` key (the app and the API read it).
DATA_KEY = "mcc_update"

KEPT, FROM_BUNDLE, ALLOWED, NO_LINE = "kept", "bundle", "allowed", "none"
#: A combined decision's ``action``: the most telling of its revisions'.
_RANK = {ALLOWED: 3, FROM_BUNDLE: 2, KEPT: 1, NO_LINE: 0}


@dataclass(frozen=True)
class MbbiosDecision:
    """What ``decide`` did to a board.txt: ``action`` (``KEPT``, ``FROM_BUNDLE``, ``ALLOWED``,
    ``NO_LINE``: neither side has one), the value written, the text to show (``note``), and
    the board.txt to write.

    FIX-PACK-9: ``rev`` is the revision folder (``HBI0309C``) and ``path`` the SD path of its
    board.txt. A COMBINED decision (``keep_mbbios`` over several revisions) has one entry per
    revision in ``revs``, a note naming each, the most telling action (allowed > bundle >
    kept > none), ``value`` as ``"HBI0309C mbb_v132.ebf, HBI0309B mbb_v141.ebf"`` and no
    ``content``. ``warnings`` are shown beside the note (a card with no B or C folder)."""

    action: str
    value: str
    note: str
    content: bytes
    rev: str = ""
    path: str = ""
    revs: tuple[MbbiosDecision, ...] = ()
    warnings: tuple[str, ...] = ()

    def per_rev(self) -> tuple[MbbiosDecision, ...]:
        """Each revision's decision (this one alone when it is not combined)."""
        return self.revs or (self,)


def mbbios_line(text: str) -> tuple[str, str] | None:
    """``(the whole line, its value)`` of the first MBBIOS line, else None."""
    m = _LINE.search(text)
    return (m.group(0), m.group(1)) if m else None


def _norm(rel: str) -> str:
    return rel.replace("\\", "/").strip("/").lower()


def board_rev(rel: str) -> str:
    """The revision folder of a ``MB/<rev>/board.txt`` path (upper case: ``HBI0309B``), else
    "" (not a board.txt the MCC reads)."""
    m = _BOARD_REL.match(_norm(rel))
    return m.group(1).upper() if m else ""


def board_txt(rev: str) -> str:
    """``MB/<rev>/board.txt`` for a revision folder name."""
    return f"MB/{rev.upper()}/board.txt"


def rev_dirs(paths: Iterable[str]) -> set[str]:
    """The ``MB/HBI*`` revision folders a list of SD paths has files in (upper case)."""
    return {m.group(1).upper() for p in paths if (m := _REV_DIR.match(_norm(p)))}


def _ebf_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _on_card(value: str, card_files: Iterable[str]) -> bool:
    """Is the ``.ebf`` an MBBIOS value names on the card? Case-blind (FAT), anywhere on it:
    the MCC looks in its revision's ``MB/HBI0309<rev>``, and a copy elsewhere (another
    revision's folder included) is refused too, to be safe."""
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


def _one_line(text: str) -> str:
    """``text`` with only its FIRST MBBIOS line: any later one goes, with its line ending, so
    a bundle that names two never brings a second to the card."""
    first = _LINE.search(text)
    if first is None:
        return text
    head, tail = text[:first.end()], text[first.end():]
    return head + re.sub(r"^[ \t]*MBBIOS[ \t]*:[^\r\n]*(?:\r\n|\n|\r)?", "", tail,
                         flags=re.I | re.M)


def decide(bundle_board_txt: bytes, *, card_board_txt: bytes | None,
           card_files: Iterable[str], allow_mcc_update: bool = False,
           rev: str = "", named: bool = False) -> MbbiosDecision:
    """The board.txt to write for ``bundle_board_txt`` on a card whose board.txt (the SAME
    revision's) is ``card_board_txt`` (None: none) and whose files are ``card_files``
    (SD-relative paths: the whole card). Raises the refusal (15) for a card that would make
    the MCC update itself. ``rev`` (FIX-PACK-9) is the revision folder; ``named`` puts it in
    the note and the refusal (a bundle for several revisions)."""
    card_files = list(card_files)
    bundle = _one_line(bundle_board_txt.decode("latin-1"))
    bundle_board_txt = bundle.encode("latin-1")
    card = card_board_txt.decode("latin-1") if card_board_txt is not None else ""
    ours = mbbios_line(bundle)
    theirs = mbbios_line(card) if card_board_txt is not None else None
    who = f"{rev} " if named and rev else ""
    path = board_txt(rev) if rev else ""
    if theirs is not None:
        line, value = theirs
        text = bundle.replace(ours[0], line, 1) if ours is not None else _insert(bundle, line)
        return MbbiosDecision(KEPT, value, f"MBBIOS kept: {who}{value}", text.encode("latin-1"),
                              rev=rev, path=path)
    if ours is None:
        return MbbiosDecision(NO_LINE, "", "", bundle_board_txt, rev=rev, path=path)
    value = ours[1]
    ebf = _ebf_name(value)
    if not _on_card(value, card_files):
        return MbbiosDecision(FROM_BUNDLE, value,
                              f"MBBIOS: {who}{value} from the bundle (the card has no {ebf}, so "
                              "the MCC will not update)", bundle_board_txt, rev=rev, path=path)
    if not allow_mcc_update:
        where = f" through {path}" if named and path else ""
        err = RefusedError(
            f"this card would make the MCC update itself to {ebf}{where}: remove {ebf} from the "
            f"card, or add {ALLOW_FLAG}",
            hint=f"the card's {path or 'board.txt'} has no MBBIOS line, the bundle's names "
                 f"{value}, and {ebf} is on the card: the MCC would flash it at its next boot "
                 "(Harness Manager is proven on MCC 1.3.2 only); nothing was written")
        # FIX-PACK-9: ``board_txt`` names the revision's file when the caller gave one
        err.data = {DATA_KEY: {"file": ebf, "value": value,  # type: ignore[attr-defined]
                               **({"board_txt": path} if path else {})}}
        raise err
    return MbbiosDecision(ALLOWED, value,
                          f"MBBIOS: {who}{value} from the bundle, allowed by {ALLOW_FLAG}: the "
                          f"card has {ebf}, so the MCC may update itself to it at its next boot",
                          bundle_board_txt, rev=rev, path=path)


def folders_warning(bundle_paths: Iterable[str], card_files: Iterable[str]) -> str:
    """FIX-PACK-9 (the Linux lead, 2 Oct): the text to show when the bundle writes the B or C
    folder onto a card that had NEITHER (is it an MPS3 configuration SD at all?); "" when
    the card had one of them, or the bundle writes neither."""
    ours = sorted((rev_dirs(bundle_paths) & set(MPS3_REVS)), reverse=True)
    if not ours or rev_dirs(card_files) & set(MPS3_REVS):
        return ""
    what = "both were written" if len(ours) > 1 else f"{ours[0]} was written"
    return ("this card had no HBI0309B or HBI0309C folder: is it an MPS3 configuration SD? "
            f"{what}")


def _combined(decisions: list[MbbiosDecision], warnings: tuple[str, ...]) -> MbbiosDecision:
    action = max((d.action for d in decisions), key=lambda a: _RANK.get(a, 0))
    value = ", ".join(f"{d.rev} {d.value}" for d in decisions if d.value)
    note = "; ".join(d.note for d in decisions if d.note)
    return MbbiosDecision(action, value, note, b"", revs=tuple(decisions), warnings=warnings)


def keep_mbbios(files: Mapping[str, Path], *, card_board_txt: bytes | None = None,
                card_files: Iterable[str], workdir: Path, allow_mcc_update: bool = False,
                card_boards: Mapping[str, bytes] | None = None,
                ) -> tuple[dict[str, Path], MbbiosDecision | None]:
    """THE substitution, for every writer of the config SD: ``files`` (SD path -> local
    file) with EACH ``MB/<rev>/board.txt`` (FAT case-blind) replaced by ``decide``'s against
    the card's board.txt of the same revision, written into ``workdir`` (which must outlive
    the write). Returns the files to write and the decision (None: no board.txt among
    ``files``; combined when the bundle has several revisions). Raises the refusal (15).

    ``card_boards`` (FIX-PACK-9): every ``MB/HBI*/board.txt`` on the card, SD path -> bytes
    (``card_boards_of``); a revision missing from it has none. Without it, ``card_board_txt``
    is the card's ``MB/HBI0309C/board.txt`` (the pre-FIX-PACK-9 call), and another
    revision's board.txt that ``card_files`` lists cannot be read: that is refused."""
    out = dict(files)
    card_files = list(card_files)
    keys = sorted(((board_rev(k), k) for k in files if board_rev(k)), reverse=True)
    if not keys:
        return out, None
    if card_boards is not None:
        card = {board_rev(k): v for k, v in card_boards.items() if board_rev(k)}
        unread: set[str] = set()
    else:
        card = {MB_DIR.split("/")[1]: card_board_txt} if card_board_txt is not None else {}
        unread = {board_rev(f) for f in card_files if board_rev(f)} - {MB_DIR.split("/")[1]}
    named = len(keys) > 1 or keys[0][0] != MB_DIR.split("/")[1]
    decisions: list[MbbiosDecision] = []
    for rev, key in keys:
        if rev in unread:
            raise RefusedError(
                f"the card's {board_txt(rev)} was not read, so its MBBIOS line cannot be kept: "
                "nothing was written",
                hint="this writer passes only MB/HBI0309C/board.txt (it predates FIX-PACK-9): "
                     "write the card over the MPS3 Debug USB instead")
        src = Path(files[key]).read_bytes()
        got = decide(src, card_board_txt=card.get(rev), card_files=card_files,
                     allow_mcc_update=allow_mcc_update, rev=rev, named=named)
        if got.content != src:
            dest = Path(tempfile.mkdtemp(prefix="mbbios-", dir=workdir)) / "board.txt"
            dest.write_bytes(got.content)
            out[key] = dest
        decisions.append(got)
    warning = folders_warning(files, card_files)
    warnings = (warning,) if warning else ()
    if len(decisions) == 1:
        one = decisions[0]
        return out, (MbbiosDecision(one.action, one.value, one.note, one.content, rev=one.rev,
                                    path=one.path, warnings=warnings) if warnings else one)
    return out, _combined(decisions, warnings)


def card_boards_of(root: Path | str) -> dict[str, bytes]:
    """Every ``MB/HBI*/board.txt`` on a mounted card at ``root`` (FAT case-blind), by its
    SD path as the card spells it: the ``card_boards`` of ``keep_mbbios``."""
    out: dict[str, bytes] = {}
    mb = next((e for e in _entries(Path(root)) if e.name.lower() == "mb" and e.is_dir()), None)
    if mb is None:
        return out
    for rev in _entries(mb):
        if not (rev.is_dir() and rev.name.lower().startswith("hbi")):
            continue
        board = next((e for e in _entries(rev) if e.name.lower() == "board.txt"), None)
        if board is not None and board.is_file():
            out[f"{mb.name}/{rev.name}/{board.name}"] = board.read_bytes()
    return out


def _entries(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def notes_of(decision: MbbiosDecision | None) -> list[str]:
    """What a writer shows for a decision: its note, then each warning ("WARNING: …")."""
    if decision is None:
        return []
    return ([decision.note] if decision.note else []) + [f"WARNING: {w}"
                                                          for w in decision.warnings]


__all__ = ["ALLOW_FLAG", "ALLOWED", "BOARD_TXT", "DATA_KEY", "FROM_BUNDLE", "KEPT", "MB_DIR",
           "MPS3_REVS", "MbbiosDecision", "NO_LINE", "board_rev", "board_txt",
           "card_boards_of", "decide", "folders_warning", "keep_mbbios", "mbbios_line",
           "notes_of", "rev_dirs"]
