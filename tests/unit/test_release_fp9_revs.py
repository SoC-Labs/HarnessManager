"""FIX-PACK-9: the release tool emits a config SD for Rev B AND Rev C (david, 2 Oct). Each check
has its twin.

- by default both ``MB/HBI0309B`` and ``MB/HBI0309C`` are stamped from the templates exactly as
  the platform's ``assemble_sd.sh`` does (``sed s/@BOARD@/HBI0309$V/g``: a comment naming the
  token is stamped too), both listed in the SD part's ``files``, ``compat.board_revs`` and
  ``board.revisions`` both; ``--board-rev C`` (``BOARD_REVS=``) still makes a C-only release;
- the revision trees must be the same tree apart from each board.txt's revision token and its
  comments (finding ``REVS``): a B tree that differs is refused;
- a ready ``--sd`` tree is taken as it is: it must serve exactly the ``--board-rev`` asked for,
  and without one, a tree that is not B+C is a warning.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.fakes.test_release_platform import bare_metal_platform, release_args
from tools.release.assemble import revisions, stamp_sd_tree, tree_revisions
from tools.release.cli import main
from tools.release.common import Layout
from tools.release.harness import _board_txt_core, rev_trees_finding, sd_component_rev

CAT = "mps3-harness"
V = "1.2.0"
#: the platform template's shape (fpga/mps3_sd/templates/board.txt): the token in a comment too
BOARD_TPL = (b"BOARD: @BOARD@\r\nTITLE: nanoSoC motherboard configuration file\r\n;\r\n"
             b"; Per-variant MCC motherboard config. assemble_sd.sh stamps the @BOARD@ token\r\n"
             b"; with HBI0309A / HBI0309B / HBI0309C.\r\n\r\n[MCCS]\r\n"
             b"MBBIOS: mbb_v141.ebf           ;MB BIOS image\r\n\r\n[APPLICATION NOTE]\r\n"
             b"APPFILE: Nanosoc\\nanosoc.txt   ;nanoSoC FPGA config\r\n")


def run(argv: list[str]) -> tuple[int, str]:
    lines: list[str] = []
    rc = main(argv, printer=lines.append)
    return rc, "\n".join(lines)


def entry(out: Path) -> dict:
    return json.loads(Layout(out).channel_file(CAT, "beta").read_bytes())


@pytest.fixture
def plat(tmp_path):
    return bare_metal_platform(tmp_path / "plat")


def test_revisions_default_to_b_and_c_and_take_either_spelling():
    assert revisions(None) == revisions("") == ("B", "C")
    assert revisions("HBI0309B,HBI0309C") == revisions("b, c") == ("B", "C")
    assert revisions("C") == ("C",) and revisions("ALL") == ("A", "B", "C")


def test_stamping_matches_assemble_sd_sh_token_for_token(tmp_path, plat):
    tpl = tmp_path / "tpl"
    shutil.copytree(plat.templates, tpl)
    (tpl / "board.txt").write_bytes(BOARD_TPL)
    out = stamp_sd_tree(tpl, plat.bit, tmp_path / "sd", revs=("B", "C"))
    b = (tmp_path / "sd/MB/HBI0309B/board.txt").read_bytes()
    c = (tmp_path / "sd/MB/HBI0309C/board.txt").read_bytes()
    assert b == BOARD_TPL.replace(b"@BOARD@", b"HBI0309B")          # sed .../g: every token
    assert c == BOARD_TPL.replace(b"@BOARD@", b"HBI0309C")
    assert b"stamps the HBI0309B token" in b                          # the comment differs too
    assert [n for n, (x, y) in enumerate(zip(b.splitlines(), c.splitlines(), strict=True))
            if x != y] == [0, 3]                                       # not "only line 1"
    assert _board_txt_core(b, "HBI0309B") == _board_txt_core(c, "HBI0309C")
    assert tree_revisions(out) == ("B", "C")


def test_a_default_release_carries_both_revisions(tmp_path, plat):
    out = tmp_path / "out"
    rc, text = run(release_args(plat, out, V, "--test-key"))
    assert rc == 0, text
    doc = entry(out)
    rel = doc["harness"]["releases"][0]
    sd = next(c for c in rel["components"] if c["target"] == "mcc-sd")
    assert sd["name"] == "sd-HBI0309BC" and sd["url"].endswith("-sd-HBI0309BC.zip")
    assert {"MB/HBI0309B/board.txt", "MB/HBI0309C/board.txt", "MB/HBI0309B/Nanosoc/nanosoc.bit",
            "MB/HBI0309C/Nanosoc/nanosoc.bit"} <= set(sd["files"])
    assert sd["files"]["MB/HBI0309B/Nanosoc/nanosoc.bit"] == \
        sd["files"]["MB/HBI0309C/Nanosoc/nanosoc.bit"]
    assert rel["compat"]["board_revs"] == ["HBI0309B", "HBI0309C"]
    assert doc["board"]["revisions"] == ["HBI0309B", "HBI0309C"]
    assert "ok [REVS] MB/HBI0309B, MB/HBI0309C: the same tree apart from each board.txt's " \
           "BOARD: revision (and comments)" in text


def test_twin_board_rev_c_makes_a_c_only_release(tmp_path, plat):
    out = tmp_path / "out"
    rc, text = run(release_args(plat, out, V, "--test-key", "--board-rev", "HBI0309C"))
    assert rc == 0, text
    doc = entry(out)
    rel = doc["harness"]["releases"][0]
    sd = next(c for c in rel["components"] if c["target"] == "mcc-sd")
    assert sd["name"] == "sd-HBI0309C" and not any("HBI0309B" in f for f in sd["files"])
    assert rel["compat"]["board_revs"] == ["HBI0309C"] and doc["board"]["revisions"] == [
        "HBI0309C"]
    assert "[REVS]" not in text


def _tree(tmp: Path, plat, revs=("B", "C")) -> Path:
    sd = tmp / "ready"
    stamp_sd_tree(plat.templates, plat.bit, sd, revs=revs)
    return sd


def _sd_args(plat, out: Path, sd: Path, *extra: str) -> list[str]:
    args = release_args(plat, out, V, "--test-key", *extra)
    i = args.index("--sd-templates")
    args[i:i + 2] = ["--sd", str(sd)]
    j = args.index("--bit")
    del args[j:j + 2]
    return args


def test_a_ready_tree_with_both_revisions_goes_as_it_is(tmp_path, plat):
    out = tmp_path / "out"
    rc, text = run(_sd_args(plat, out, _tree(tmp_path, plat)))
    assert rc == 0, text
    assert "WARNING: the --sd tree" not in text
    assert entry(out)["harness"]["releases"][0]["compat"]["board_revs"] == ["HBI0309B",
                                                                            "HBI0309C"]


def test_twin_a_ready_c_only_tree_warns_by_default_and_is_refused_when_b_c_is_asked(tmp_path,
                                                                                      plat):
    sd = _tree(tmp_path, plat, revs=("C",))
    rc, text = run(_sd_args(plat, tmp_path / "out1", sd))
    assert rc == 0, text
    assert ("WARNING: the --sd tree serves HBI0309C, not HBI0309B and HBI0309C: a board of "
            "another revision stays unprogrammed (platform v2.0.0 ships B and C)") in text
    rc, text = run(_sd_args(plat, tmp_path / "out2", sd, "--board-rev", "B,C"))
    assert rc != 0
    assert ("the --sd tree serves HBI0309C, not the HBI0309B and HBI0309C --board-rev asks for"
            in text)
    assert not (tmp_path / "out2" / "SoC-Labs").exists()


def test_a_b_tree_that_differs_from_c_is_refused(tmp_path, plat):
    sd = _tree(tmp_path, plat)
    note = sd / "MB/HBI0309B/Nanosoc/nanosoc.txt"
    note.write_bytes(note.read_bytes() + b"OSC2: 99.0\n")
    rc, text = run(_sd_args(plat, tmp_path / "out", sd))
    assert rc == 15, text
    assert "[REVS] MB/HBI0309C/Nanosoc/nanosoc.txt differs from MB/HBI0309B/Nanosoc/nanosoc.txt" \
        in text
    assert not (tmp_path / "out" / "SoC-Labs").exists()


def test_twin_a_board_txt_that_differs_beyond_its_token_is_refused_comments_are_not(tmp_path,
                                                                                    plat):
    sd = _tree(tmp_path, plat)
    b = sd / "MB/HBI0309B/board.txt"
    b.write_bytes(b.read_bytes() + b";a comment only B has, naming HBI0309B\n")
    files = {p.relative_to(sd).as_posix(): p for p in sd.rglob("*") if p.is_file()}
    assert rev_trees_finding(files, ["HBI0309B", "HBI0309C"]).ok
    b.write_bytes(b.read_bytes().replace(b"MBBIOS: mbb_v141.ebf", b"MBBIOS: mbb_v999.ebf"))
    got = rev_trees_finding(files, ["HBI0309B", "HBI0309C"])
    assert not got.ok and got.detail == ("MB/HBI0309C/board.txt differs from "
                                         "MB/HBI0309B/board.txt beyond its BOARD: revision and "
                                         "comments")


def test_the_sd_part_names_its_revisions():
    assert sd_component_rev(["HBI0309C"]) == "HBI0309C"
    assert sd_component_rev(["HBI0309B", "HBI0309C"]) == "HBI0309BC"
    assert sd_component_rev(["HBI0309B", "XYZ"]) == "multi"
