"""L1-CARD: ``harness-manager program TARGET RM --keep-on-card``.

Over T5's scripted engine (every output format, no sockets), then the round trip: the CLI
through a live harness-manager-daemon to a virtual Linux board with the D13 store, where
the card write is pyverify's real ``commit`` into FakeShell. Each has a negative twin.
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import replace

import pytest

from harness_manager.cli.engine import ENV_NO_DAEMON, set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.core.pack import CARD_NO_CARD, CARD_NO_STORE, CardOutcome, CardStatus
from harness_manager_mps3 import shell as sh
from tests.fakes.t2_overlays import make_overlay, use_overlay_dirs
from tests.fakes.t5_fake_engine import FakeEngine
from tests.fakes.t13_daemon import LiveDaemon, engine_for, run_cli
from tests.fakes.virtual_board import LINUX_HARNESSD, VirtualMps3

T = "127.0.0.1"
READY = CardStatus(store=True, present=True, state="empty", text="empty")
LINUX_USD = replace(LINUX_HARNESSD, name="linux-harnessd-usd",
                    features=(*LINUX_HARNESSD.features, "usd"))


@pytest.fixture
def fake():
    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


@pytest.fixture(autouse=True)
def _stdin_eof(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# -- over the scripted engine -----------------------------------------------------------------


def test_keep_on_card_is_passed_and_reported_in_every_format(fake, capsys):
    fake.st.card = READY
    rc, out, err = run(capsys, "--json", "program", T, "nanosoc", "--yes", "--keep-on-card")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["result"]["card"] == {"kept": True, "slot": "A", "why": ""}
    assert "card: empty; the design will be kept on it" in err
    rc, out, _ = run(capsys, "program", T, "nanosoc", "--yes", "--keep-on-card")
    assert "kept on the card (slot A): the board boots into nanosoc next time" in out
    rc, out, _ = run(capsys, "--tsv", "program", T, "nanosoc", "--yes", "--keep-on-card")
    row = out.rstrip("\n").split("\n")[-1].split("\t")
    assert len(row) == len(TSV_COLUMNS["program"]) and row[-1] == "kept:A"
    assert fake.st.deploy_keeps == [True, True, True]


def test_negative_twin_without_the_flag_nothing_asks_to_keep(fake, capsys):
    fake.st.card = READY                          # it could keep: it is not asked to
    rc, out, _ = run(capsys, "--json", "program", T, "nanosoc", "--yes")
    assert rc == ExitCode.OK and json.loads(out)["result"]["card"] is None
    rc, out, _ = run(capsys, "program", T, "nanosoc", "--yes")
    assert "card" not in out
    rc, out, _ = run(capsys, "--tsv", "program", T, "nanosoc", "--yes")
    row = out.rstrip("\n").split("\n")[-1].split("\t")
    assert len(row) == len(TSV_COLUMNS["program"]) and row[-1] == "-"      # TSV: empty is -
    assert fake.st.deploy_keeps == [False, False, False]
    assert "deploy.card_status" not in fake.calls


@pytest.mark.parametrize("card, reason", [
    (CardStatus(store=False, reason=CARD_NO_STORE), CARD_NO_STORE),
    (CardStatus(store=True, present=False, state="none", reason=CARD_NO_CARD), CARD_NO_CARD),
])
def test_keep_on_card_refuses_with_exit_12_and_writes_nothing(fake, capsys, card, reason):
    fake.st.card = card
    rc, out, err = run(capsys, "--json", "program", T, "nanosoc", "--yes", "--keep-on-card")
    assert rc == ExitCode.UNAVAILABLE
    error = json.loads(out)["error"]
    assert error["message"] == f"keep_on_card is unavailable: {reason}"
    assert error["data"]["card"]["reason"] == reason
    assert "deploy.deploy" not in fake.calls
    rc, _, err = run(capsys, "program", T, "nanosoc", "--yes", "--keep-on-card")
    assert rc == ExitCode.UNAVAILABLE and reason in err


def test_a_card_that_was_not_kept_says_why_and_still_exits_0(fake, capsys):
    fake.st.card = READY
    fake.st.card_outcome = CardOutcome(kept=False, why="no card in the user microSD slot")
    rc, out, _ = run(capsys, "program", T, "nanosoc", "--yes", "--keep-on-card")
    assert rc == ExitCode.OK
    assert "not kept on the card: no card in the user microSD slot" in out


def test_the_prompt_says_the_design_will_be_kept(fake, capsys, monkeypatch):
    fake.st.card = READY
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    rc, _, err = run(capsys, "program", T, "nanosoc", "--keep-on-card")
    assert rc == ExitCode.REFUSED and "and keep it on the card?" in err
    assert "deploy.deploy" not in fake.calls


# -- the round trip: CLI -> harness-manager-daemon -> MPS3 pack -> the board's card -----------


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


@pytest.fixture
def linux_board(tmp_path, monkeypatch):
    from harness_manager.cli import engine as cli_engine

    monkeypatch.delenv(cli_engine.ENV_ENGINE, raising=False)
    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    make_overlay(tmp_path / "ov", "synth", static_id=LINUX_USD.static_id)
    use_overlay_dirs(monkeypatch, tmp_path / "ov")
    with VirtualMps3(tmp_path, LINUX_USD) as vb:
        vb.shell.usd_insert("da")
        eng = engine_for(vb)
        try:
            with LiveDaemon(eng):
                yield vb
        finally:
            eng.close_all()
            set_engine_factory(previous)


def test_the_flag_round_trips_through_the_daemon_to_the_card(linux_board, capsys):
    vb = linux_board
    rc, out, err = run_cli(capsys, "--json", "program", vb.shell_endpoint, "synth", "--yes",
                           "--keep-on-card")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["result"]["card"] == {"kept": True, "slot": "A", "why": ""}
    assert "deploy: card" in err                       # the card write's own progress
    assert vb.shell.commits == [("synth", "A")]


def test_negative_twin_no_flag_round_trips_to_no_card_write(linux_board, capsys):
    vb = linux_board
    rc, out, err = run_cli(capsys, "--json", "program", vb.shell_endpoint, "synth", "--yes")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["result"]["card"] is None
    assert vb.shell.commits == [] and vb.shell.current_rm_id != 0
