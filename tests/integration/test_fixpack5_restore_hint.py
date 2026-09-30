"""FIX-PACK-5 item 3: ``restore`` with no greybox says exactly what to do.

The clean-run step 10 (board 2, 2026-09-30 14:31): a home that had imported the kit and
packed ``minimal`` ran ``restore`` and got "no baseline overlay (greybox) is known for the
running shell". The kit zip (mps3_rc2_0x44EE76D5_kit.zip, read 2026-09-30) holds the static,
its boundary and the XDC, and no overlay; ``kit pack`` refuses rm_id 0. So ``kit import``
cannot register a greybox, and the refusal now names the shell, where HM looked, what it has,
and the two ways to give it the mint's overlay folder. Each check has a twin that follows the
hint (through the real CLI, Engine and MPS3 pack, on the virtual board).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness_manager.cli.engine import ENV_NO_DAEMON, set_engine_factory
from harness_manager.cli.main import main
from harness_manager.core.errors import AbsentError, ExitCode
from harness_manager.services.deploy import DeployService
from harness_manager_mps3.overlays import OVERLAY_DIRS_ENV, import_overlay
from tests.fakes.t2_overlays import (
    FIELDED_USERCODE,
    GREYBOX_RM_ID,
    OTHER_STATIC_ID,
    make_overlay,
)
from tests.fakes.t13_daemon import engine_for
from tests.fakes.virtual_board import VirtualMps3

NANOSOC_RM_ID = 0x01000001
SHELL = "0x3f1a560f"                     # the virtual board's fielded shell


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A virtual board running nanosoc; each verb gets its own in-process Engine (a terminal)."""
    monkeypatch.delenv(OVERLAY_DIRS_ENV, raising=False)       # a clean home: no directories
    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    with VirtualMps3(tmp_path / "b", boot_rm_id=NANOSOC_RM_ID) as vb:
        previous = set_engine_factory(lambda _args: engine_for(vb))
        try:
            yield vb
        finally:
            set_engine_factory(previous)


def import_minimal(vb, tmp_path) -> None:
    """What `kit pack ~/builds/minimal --import` leaves: one RM in the content store."""
    eng = engine_for(vb)
    try:
        import_overlay(eng.store, make_overlay(tmp_path / "packed", "minimal",
                                               static_usercode=FIELDED_USERCODE))
    finally:
        eng.close_all()


def greybox_dir(tmp_path, static_id=None):
    root = tmp_path / "mint_overlays"
    kw = {} if static_id is None else {"static_id": static_id}
    make_overlay(root, "greybox", rm_id=GREYBOX_RM_ID, static_usercode=FIELDED_USERCODE, **kw)
    return root


# -- the refusal ---------------------------------------------------------------------------------


def test_a_clean_home_with_only_an_imported_rm_is_told_what_to_do(board, tmp_path, capsys):
    import_minimal(board, tmp_path)
    rc, out, err = run(capsys, "--json", "restore", board.shell_endpoint)
    e = json.loads(out)["error"]
    assert rc == ExitCode.ABSENT
    assert e["message"] == (f"no baseline overlay (greybox) for shell {SHELL}: no overlay "
                            "directories are set, and the imported overlays for it (minimal) "
                            "include none")
    for said in ("greybox/manifest.json", "restore TARGET --overlay-dir DIR",
                 "harness-manager config set mps3.overlay_dirs DIR",
                 "`kit import` adds no greybox", "rm_id 0 is refused"):
        assert said in e["hint"], said
    assert board.shell.push_events == []                  # refused before anything moved


def test_twin_following_the_hint_once_with_overlay_dir_restores(board, tmp_path, capsys):
    import_minimal(board, tmp_path)
    rc, out, err = run(capsys, "restore", board.shell_endpoint,
                       "--overlay-dir", str(greybox_dir(tmp_path)))
    assert rc == ExitCode.OK, err
    assert "to the baseline (0x00000000)" in out and "verified" in out
    assert board.shell.swaps[-1]["rm"] == "greybox"


def test_twin_following_the_hint_for_good_with_config_set_restores(board, tmp_path, capsys):
    root = greybox_dir(tmp_path)
    rc, _, err = run(capsys, "config", "set", "mps3.overlay_dirs", str(root))
    assert rc == ExitCode.OK, err
    rc, out, err = run(capsys, "restore", board.shell_endpoint)
    assert rc == ExitCode.OK, err
    assert board.shell.swaps[-1]["rm"] == "greybox"


def test_a_greybox_for_another_shell_is_named_and_where_hm_looked(board, tmp_path, capsys):
    root = greybox_dir(tmp_path, static_id=OTHER_STATIC_ID)
    rc, out, _ = run(capsys, "--json", "restore", board.shell_endpoint, "--overlay-dir", str(root))
    msg = json.loads(out)["error"]["message"]
    assert rc == ExitCode.ABSENT
    assert f"none in the overlay directories ({root})" in msg and "none is imported for it" in msg
    assert "the greybox HM has is for shell 0xdeadbeef" in msg
    assert board.shell.push_events == []


def test_a_pack_without_its_own_words_keeps_the_generic_refusal():
    adapter = SimpleNamespace(baseline=lambda: None)
    session = SimpleNamespace(deploy=adapter, candidate=SimpleNamespace(board_id="x@y"))
    with pytest.raises(AbsentError) as exc:
        DeployService().restore_baseline(session)
    assert exc.value.message == "no baseline overlay (greybox) is known for the running shell"


def test_negative_twin_a_failing_explanation_still_refuses_plainly():
    def broken():
        raise RuntimeError("catalogue exploded")

    adapter = SimpleNamespace(baseline=lambda: None, baseline_missing=broken)
    session = SimpleNamespace(deploy=adapter, candidate=SimpleNamespace(board_id="x@y"))
    with pytest.raises(AbsentError) as exc:
        DeployService().restore_baseline(session)
    assert "greybox" in exc.value.message and exc.value.hint
