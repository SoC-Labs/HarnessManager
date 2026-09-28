"""FIX-PACK-1 item 4: ``display snapshot`` waits for the SSH forward to come up.

On silicon (2026-09-28) the first ``display TARGET snapshot`` over ``ssh -J hub root@board``
timed out: the 10 s first-picture wait was mostly spent bringing the forward up, and the
second try succeeded. The wait is 30 s now (in-process and the daemon's PNG route; the
client's request 45 s), human mode says what it waits for ("opening the SSH forward..."),
and a wait that runs out still ends in a clear error (exit 12, "try again").

LM5's two ways to a board (in-process and a real daemon) over LM3's fake adapter, whose
``display_connect`` is slowed down as a cold forward is. The clocks are scaled: the forward
takes 1.5 s, the old budget is 1 s and the new one 3 s. Each check has its twin.
"""

from __future__ import annotations

import json
import time
from typing import Any

from harness_manager.cli import cmd_display as CD
from harness_manager.client import display as client_display
from harness_manager.core.errors import ExitCode
from harness_manager.daemon import display_api
from tests.integration.test_lm5_display_cli import (  # noqa: F401 - the fixture
    TARGET,
    cli,
    reach,
    still_board,
    via,
)

FORWARD_S = 1.5                     # a cold ``ssh -J`` forward, scaled down


def cold_forward(where: Any, delay: float = FORWARD_S) -> None:
    """The adapter's first connect waits for the SSH forward to come up."""
    adapter = where.adapter
    real = adapter.display_connect
    state = {"cold": True}

    def connect() -> Any:
        if state["cold"]:
            state["cold"] = False
            time.sleep(delay)
        return real()

    adapter.display_connect = connect


def budget(monkeypatch: Any, seconds: float) -> None:
    monkeypatch.setattr(CD, "PICTURE_WAIT_S", seconds)
    monkeypatch.setattr(display_api, "PICTURE_WAIT_S", seconds)


def test_the_wait_covers_a_cold_forward_30_s_and_the_request_outlasts_it():
    assert CD.PICTURE_WAIT_S == display_api.PICTURE_WAIT_S == 30.0
    assert client_display.STILL_TIMEOUT_S > display_api.PICTURE_WAIT_S


def test_a_cold_forward_longer_than_the_old_wait_still_gives_the_picture_and_says_so(
        via, monkeypatch, tmp_path, capsys):
    budget(monkeypatch, 3.0)                           # the new budget, scaled (30 s)
    with still_board() as board, reach(via, board, monkeypatch) as where:
        cold_forward(where)
        rc, out, err = cli(capsys, "display", TARGET, "snapshot", "-o", str(tmp_path))
        assert rc == 0, err
        assert out.splitlines()[0].endswith(".png")
        assert CD.OPENING.format(wait=3.0) in err, err   # the progress, on stderr only
        assert "opening the SSH forward" not in out


def test_twin_machine_output_gets_no_progress_words(via, monkeypatch, tmp_path, capsys):
    budget(monkeypatch, 3.0)
    with still_board() as board, reach(via, board, monkeypatch) as where:
        cold_forward(where)
        rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot", "-o",
                           str(tmp_path / "a.png"))
        assert rc == 0, err
        assert json.loads(out)["format"] == "png" and "opening the SSH forward" not in err


def test_twin_a_wait_that_runs_out_is_a_clear_error(via, monkeypatch, tmp_path, capsys):
    budget(monkeypatch, 1.0)                           # the OLD budget, scaled (10 s)
    with still_board() as board, reach(via, board, monkeypatch) as where:
        cold_forward(where, delay=2.5)
        shots = tmp_path / "shots"
        shots.mkdir()
        rc, out, err = cli(capsys, "display", TARGET, "snapshot", "-o", str(shots))
        assert rc == ExitCode.UNAVAILABLE, (out, err)
        assert "no picture from the board within 1 s (connecting" in err, err
        assert "try again" in err and not list(shots.iterdir())
