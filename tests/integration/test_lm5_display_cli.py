"""Lane LM5: ``harness-manager display TARGET snapshot|status|show`` (``cli/cmd_display.py``).

Both ways the CLI reaches a board: in-process (the engine factory hands the verb an Engine
whose fake pack has LM2's ``display_adapter`` hook) and through a real harness-manager-daemon
(LM3's rig: the product's routes under uvicorn, discovered from ``daemon.json``). The board
is LM1's ``FakeLcdMirror`` on 127.0.0.1:0; the pictures are LM1's goldens. Every check has a
negative twin.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.cli import cmd_display as CD
from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.client.display import PictureModel, close_error
from harness_manager.client.remote import RemoteEngine
from harness_manager.core import display_wire as w
from harness_manager.core.display import HATCH_RGB565, png_rgb565, rgb888
from harness_manager.core.errors import (
    ExitCode,
    HeldError,
    UnavailableError,
    UnreachableError,
)
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.state import DaemonInfo, daemon_json_path, write_info
from harness_manager.engine import Engine
from tests.fakes import lm1_golden as G
from tests.fakes.lm1_fake_lcd_mirror import CardAnimator, FakeLcdMirror, FakePanel
from tests.fakes.lm3_display_rig import (
    BOARD,
    DisplayPack,
    FakeDisplayAdapter,
    FakeLeases,
    display_rig,
)
from tests.fakes.t13_daemon import state_dir, wait_for
from tests.integration.test_lm3_display_api import decode_png

TARGET = "lcd-01"                       # the fake pack's candidate: fake@lcd-01 (= BOARD)
BARE = "needs the Linux harness with lcd_mirror (this board runs the bare-metal harness)"
HELD = "the live display is for the lease holder only: alice@hub-02 holds mps3_02_pl"
CARD = G.card_picture(0x2A5)
STATUS_KEYS = {"board_id", "available", "unavailable", "state", "reason", "mode", "hello",
               "owner", "flags", "regs", "badges", "presented", "hatched", "seq", "t_ms",
               "frames", "resets", "rtt_ms", "rate", "rate_asked", "fps", "bytes_per_s",
               "viewers", "counters"}

assert f"fake@{TARGET}" == BOARD


# --- the two ways to a board ------------------------------------------------------------------------


@dataclass
class HubPack(DisplayPack):
    """A fake display pack whose sessions sit behind a hub (``session.hub``) when ``hub``."""

    hub: Any = None

    def open(self, cand: Any) -> Any:
        session = super().open(cand)
        session.hub = self.hub
        return session


@dataclass
class Where:
    via: str
    pack: DisplayPack
    board: FakeLcdMirror | None
    rig: Any = None

    @property
    def adapter(self) -> FakeDisplayAdapter:
        return self.pack.adapter

    def behind_hub(self, leases: FakeLeases, monkeypatch: Any) -> None:
        """The board is behind a hub whose lease view is ``leases`` (D3)."""
        hub = SimpleNamespace(target="mps3_02_pl")
        if self.rig is not None:
            self.rig.engine.session(BOARD).hub = hub
            self.rig.daemon.daemon.leases = leases           # hub_api's lease service
        else:
            self.pack.hub = hub
            monkeypatch.setattr(CD, "_lease_service",
                                lambda _e, s: leases if getattr(s, "hub", None) else None)


@contextmanager
def in_process(board: FakeLcdMirror | None) -> Iterator[Where]:
    pack = HubPack(adapter=FakeDisplayAdapter(board))
    previous = set_engine_factory(
        lambda _args: Engine(EngineConfig(state_dir=state_dir()), packs={"fake": pack}))
    try:
        yield Where("in-process", pack, board)
    finally:
        set_engine_factory(previous)


@contextmanager
def through_daemon(board: FakeLcdMirror | None, monkeypatch: Any) -> Iterator[Where]:
    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
    monkeypatch.delenv("HARNESS_MANAGER_CLI_ENGINE", raising=False)
    with display_rig(FakeDisplayAdapter(board)) as rig:
        write_info(state_dir(), DaemonInfo(pid=os.getpid(), port=rig.daemon.port,
                                           token=rig.daemon.token, started_at=time.time(),
                                           version="test", hostname=""))
        # The daemon's engine has the fake pack; the CLI resolves TARGET with the same one.
        monkeypatch.setattr(RemoteEngine, "_local_packs", lambda _self: {"fake": rig.pack})
        try:
            yield Where("daemon", rig.pack, board, rig)
        finally:
            daemon_json_path(state_dir()).unlink(missing_ok=True)


@pytest.fixture(params=["in-process", "daemon"])
def via(request: Any) -> str:
    return request.param


@contextmanager
def reach(via: str, board: FakeLcdMirror | None, monkeypatch: Any) -> Iterator[Where]:
    if via == "daemon":
        with through_daemon(board, monkeypatch) as where:
            yield where
    else:
        with in_process(board) as where:
            yield where


def cli(capsys: Any, *argv: str) -> tuple[int, str, str]:
    rc = main(["--pack", "fake", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


def still_board(frame: bytes = CARD, **kw: Any) -> FakeLcdMirror:
    return FakeLcdMirror(FakePanel(frame, **kw))


def expected_rgb(frame: bytes, scale: int = 1) -> bytes:
    """What a PNG of ``frame`` decodes to: RGB888 rows, ``scale`` x ``scale`` per pixel."""
    vals = struct.unpack(f"<{w.W * w.H}H", frame)
    rows = []
    for y in range(w.H):
        line = b"".join(bytes(rgb888(v)) * scale for v in vals[y * w.W:(y + 1) * w.W])
        rows.append(line * scale)
    return b"".join(rows)


# --- snapshot -----------------------------------------------------------------------------------------


def test_the_snapshot_png_is_the_golden_picture(via, monkeypatch, tmp_path, capsys):
    with still_board() as board, reach(via, board, monkeypatch) as where:
        out_png = tmp_path / "shot.png"
        rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot", "-o", str(out_png))
        assert rc == 0, err
        body = json.loads(out)
        data = out_png.read_bytes()
        pw, ph, rgb = decode_png(data)                        # stdlib zlib + struct
        assert (pw, ph) == (320, 240) and rgb == expected_rgb(CARD)
        assert data == png_rgb565(CARD)
        assert body["path"] == str(out_png.resolve()) and body["format"] == "png"
        assert (body["width"], body["height"], body["bytes"]) == (320, 240, len(data))
        assert body["state"] == "live" and body["mode"] == "hw" and body["owner"] == "harness"
        assert body["exact"] is True and body["hatched"] == 0 and body["badges"] == []
        assert board.stats["connects"] >= 1
        if where.rig is not None:                             # it really went through the daemon
            assert where.rig.svc.boards() == [BOARD] and where.pack.hook_calls >= 1
        # the human form: the path, then the status line
        rc, out, _ = cli(capsys, "display", TARGET, "snapshot", "-o", str(tmp_path))
        path, line = out.splitlines()
        assert rc == 0 and Path(path).parent == tmp_path.resolve()
        assert Path(path).name.startswith("display-fake_lcd-01-") and path.endswith(".png")
        assert decode_png(Path(path).read_bytes())[2] == rgb
        assert "live  mode hw  owner harness  exact  badges -" in line
        # the twin: a different picture does not decode to the golden, and a snapshot taken
        # after the board changed is the new picture, never a cached one
        other = G.card_picture(0x2A6)
        assert expected_rgb(other) != rgb
        with board.edit() as p:
            p.set_frame(other)

        def fresh() -> bool:
            rc2, _o, _e = cli(capsys, "display", TARGET, "snapshot", "-o", str(out_png))
            return rc2 == 0 and decode_png(out_png.read_bytes())[2] == expected_rgb(other)

        wait_for(fresh, timeout=20, what="a snapshot of the changed board")


def test_raw_is_the_panels_153600_bytes(via, monkeypatch, tmp_path, capsys):
    with still_board() as board, reach(via, board, monkeypatch):
        dest = tmp_path / "shot.rgb565"
        rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot", "--raw", "-o",
                           str(dest))
        assert rc == 0, err
        body = json.loads(out)
        assert dest.stat().st_size == 153600 == w.FRAME_BYTES
        assert dest.read_bytes() == CARD
        assert body["format"] == "rgb565le" and (body["width"], body["height"]) == (320, 240)
        assert body["scale"] == 1 and body["bytes"] == 153600
        rc, out, _ = cli(capsys, "--tsv", "display", TARGET, "snapshot", "--raw", "-o", str(dest))
        row = out.rstrip("\n").split("\t")
        assert rc == 0 and len(row) == len(TSV_COLUMNS["display snapshot"]) == 13
        assert row[:6] == [BOARD, str(dest.resolve()), "rgb565le", "320", "240", "153600"]
        assert row[6:10] == ["live", "hw", "harness", "true"]
        # the twins: --raw with --scale or --hatch is a usage error before the board is asked
        connects = board.stats["connects"]
        for extra in (("--scale", "2"), ("--hatch",)):
            gone = tmp_path / "never.rgb565"
            rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot", "--raw",
                               *extra, "-o", str(gone))
            assert rc == ExitCode.USAGE and "--raw" in json.loads(out)["error"]["message"]
            assert not gone.exists()
        assert board.stats["connects"] == connects


def test_scale_2_is_640x480_and_hatch_greys_the_unknown_tiles(via, monkeypatch, tmp_path,
                                                              capsys):
    valid = set(range(w.NTILES)) - {0}
    with still_board(valid=valid) as board, reach(via, board, monkeypatch):
        big = tmp_path / "big.png"
        rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot", "--scale", "2",
                           "--hatch", "-o", str(big))
        assert rc == 0, err
        body = json.loads(out)
        pw, ph, rgb = decode_png(big.read_bytes())
        assert (pw, ph) == (640, 480) and (body["width"], body["height"]) == (640, 480)
        assert body["hatched"] == 1 and body["exact"] is True
        assert rgb[0:3] == bytes(rgb888(HATCH_RGB565))           # tile 0: hatched grey
        x, y = 100, 50                                           # a bar, known: scaled 2x
        px = struct.unpack_from("<H", CARD, (y * 320 + x) * 2)[0]
        for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            o = ((2 * y + dy) * 640 + 2 * x + dx) * 3
            assert rgb[o:o + 3] == bytes(rgb888(px))
        rc, out, _ = cli(capsys, "display", TARGET, "snapshot", "-o", str(big))
        assert rc == 0 and "exact, 1 tile unknown" in out
        # the twins: scale 1 is 320x240, and no --hatch leaves tile 0 as the mirror holds it
        pw, ph, rgb1 = decode_png(big.read_bytes())
        assert (pw, ph) == (320, 240)
        raw = tmp_path / "raw"
        assert cli(capsys, "display", TARGET, "snapshot", "--raw", "-o", str(raw))[0] == 0
        assert rgb1[0:3] == bytes(rgb888(struct.unpack_from("<H", raw.read_bytes(), 0)[0]))
        rc, _out, _err = cli(capsys, "display", TARGET, "snapshot", "--scale", "5")
        assert rc == ExitCode.USAGE


def test_a_bad_out_directory_fails_before_the_board(via, monkeypatch, tmp_path, capsys):
    with still_board() as board, reach(via, board, monkeypatch):
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "snapshot", "-o",
                         str(tmp_path / "no" / "such" / "x.png"))
        assert rc == ExitCode.USAGE and "no directory" in json.loads(out)["error"]["message"]
        assert board.stats["connects"] == 0
        # twin: an existing directory takes the default name
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "snapshot", "-o", str(tmp_path))
        assert rc == 0 and Path(json.loads(out)["path"]).parent == tmp_path.resolve()


# --- refusals ---------------------------------------------------------------------------------------


def test_unavailable_is_exit_12_with_the_reason_and_nothing_connects(via, monkeypatch,
                                                                     tmp_path, capsys):
    with still_board() as board, reach(via, board, monkeypatch) as where:
        where.adapter.reason = BARE
        shot = tmp_path / "x.png"
        for argv in (("snapshot", "-o", str(shot)), ("show", "--for", "0.2")):
            rc, out, err = cli(capsys, "--json", "display", TARGET, *argv)
            assert rc == ExitCode.UNAVAILABLE, (argv, err)
            e = json.loads(out)["error"]
            assert e["name"] == "UNAVAILABLE" and e["capability"] == "display_mirror"
            assert e["reason"] == BARE
            assert err.strip() == f"harness-manager: display_mirror is unavailable — {BARE}"
        assert not shot.exists() and board.stats["connects"] == 0
        # status is a report, not a failure: exit 0, available false, the reason
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "status")
        st = json.loads(out)
        assert rc == 0 and st["available"] is False and st["unavailable"] == BARE
        rc, out, _ = cli(capsys, "display", TARGET, "status")
        assert f"available  no: {BARE}" in out
        # the twin: the same board once the adapter says yes
        where.adapter.reason = ""
        assert cli(capsys, "display", TARGET, "snapshot", "-o", str(shot))[0] == 0
        assert shot.exists() and board.stats["connects"] >= 1


def test_held_is_exit_4_and_names_the_holder(via, monkeypatch, tmp_path, capsys):
    with still_board() as board, reach(via, board, monkeypatch) as where:
        leases = FakeLeases("alice@hub-02", mine=False)
        where.behind_hub(leases, monkeypatch)
        where.adapter.reason = HELD
        rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot", "-o",
                           str(tmp_path / "x.png"))
        assert rc == ExitCode.HELD, err
        e = json.loads(out)["error"]
        assert e["name"] == "HELD" and e["holder"] == "alice@hub-02"
        assert "alice@hub-02" in err and "lease" in err
        rc, _out, err = cli(capsys, "display", TARGET, "show", "--for", "0.2")
        assert rc == ExitCode.HELD and "alice@hub-02" in err
        # a reason that does not name the holder: the CLI names it
        where.adapter.reason = "the live display is for the lease holder only"
        rc, out, err = cli(capsys, "--json", "display", TARGET, "snapshot")
        assert rc == ExitCode.HELD
        assert "(the lease holder: alice@hub-02)" in json.loads(out)["error"]["message"]
        assert board.stats["connects"] == 0
        # the twin: the lease is ours, and the board opens
        leases.mine = True
        where.adapter.reason = ""
        rc, _out, err = cli(capsys, "display", TARGET, "snapshot", "-o", str(tmp_path / "y.png"))
        assert rc == 0, err
        assert board.stats["connects"] >= 1


def test_a_lease_lost_after_the_check_ends_the_view_typed(via, monkeypatch, capsys):
    with still_board() as board, reach(via, board, monkeypatch) as where:
        where.adapter.connect_error = HeldError(HELD, holder="alice@hub-02")
        rc, _out, err = cli(capsys, "display", TARGET, "show", "--for", "3")
        assert rc == ExitCode.HELD and "alice@hub-02" in err
        # the twin: the connect works again, and the view runs its time
        where.adapter.connect_error = None
        if where.rig is not None:
            where.rig.svc.close(BOARD, "test: forget the ended upstream")
        rc, out, err = cli(capsys, "display", TARGET, "show", "--for", "0.4", "--rate", "5")
        assert rc == 0, err


def test_no_live_display_on_the_board_is_exit_12(via, monkeypatch, capsys):
    """The pack's hook returns no adapter: the route's own words."""
    with reach(via, None, monkeypatch) as where:
        where.pack.adapter = None
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "snapshot")
        assert rc == ExitCode.UNAVAILABLE
        assert "has no live display" in json.loads(out)["error"]["reason"]


# --- status ------------------------------------------------------------------------------------------


def test_status_json_is_the_whole_dict_and_tsv_its_row(via, monkeypatch, tmp_path, capsys):
    with still_board() as board, reach(via, board, monkeypatch) as where:
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "status")
        st = json.loads(out)
        assert rc == 0 and STATUS_KEYS <= set(st) and st["ok"] is True
        assert st["available"] is True and st["unavailable"] == "" and st["board_id"] == BOARD
        rc, out, _ = cli(capsys, "--tsv", "display", TARGET, "status")
        row = out.rstrip("\n").split("\t")
        assert rc == 0 and len(row) == len(TSV_COLUMNS["display status"]) == 12
        assert row[:2] == [BOARD, "true"]
        if where.rig is not None:
            # through the daemon the upstream a snapshot opened is still live (its grace)
            assert cli(capsys, "display", TARGET, "snapshot", "-o", str(tmp_path))[0] == 0
            rc, out, _ = cli(capsys, "--json", "display", TARGET, "status")
            st = json.loads(out)
            assert st["state"] == "live" and st["presented"] is True and st["mode"] == "hw"
            assert st["flags"]["exact"] is True and st["hello"]["static_id"] == board.static_id
        else:
            # in-process nothing is viewing it: down, and the human form says what opens it
            assert st["state"] == "down" and st["presented"] is False
            rc, out, _ = cli(capsys, "display", TARGET, "status")
            assert "`snapshot` or `show` opens it" in out


# --- show ---------------------------------------------------------------------------------------------


def test_show_without_a_terminal_prints_one_status_line_per_refresh(via, monkeypatch, capsys):
    board = FakeLcdMirror(FakePanel(), animate=CardAnimator(period_s=1 / 12), rate_default=30)
    with board, reach(via, board, monkeypatch):
        t0 = time.monotonic()
        rc, out, err = cli(capsys, "display", TARGET, "show", "--for", "1.2", "--rate", "5")
        took = time.monotonic() - t0
        lines = out.splitlines()
        assert rc == 0, err
        assert len(lines) == math.ceil(1.2 * 5) == 6
        assert "\x1b[" not in out and CD.HALF not in out
        assert all(BOARD in ln for ln in lines) and "live" in lines[-1]
        assert "exact" in lines[-1] and "owner harness" in lines[-1]
        assert 1.1 <= took < 10
        # the view keeps drawing (it acks what it drew, so the next UPDATE comes): the
        # counter's seq moves on from the first picture to the last
        seqs = [int(ln.rsplit(" seq ", 1)[1].split()[0]) for ln in lines if " seq -" not in ln]
        assert len(seqs) >= 3 and seqs[-1] > seqs[0], lines
        # --tsv: one row per refresh, the layout's columns
        rc, out, _ = cli(capsys, "--tsv", "display", TARGET, "show", "--for", "0.4", "--rate",
                         "5")
        rows = [r.split("\t") for r in out.splitlines()]
        assert rc == 0 and len(rows) == 2
        assert all(len(r) == len(TSV_COLUMNS["display show"]) for r in rows)
        # --json: one object at the end
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "show", "--for", "0.6", "--rate",
                         "5")
        body = json.loads(out)
        assert rc == 0 and body["refreshes"] == 3 and body["picture"] is False
        assert body["updates"] >= 2 and body["state"] == "live" and body["exact"] is True
        assert {"board_id", "rate", "seq", "status", "owner", "mode", "badges"} <= set(body)
        # the twins: --json with no end is refused up front; a bad rate is a usage error
        connects = board.stats["connects"]
        rc, out, _ = cli(capsys, "--json", "display", TARGET, "show")
        assert rc == ExitCode.USAGE and "--for" in json.loads(out)["error"]["message"]
        for bad in ("0", "31", "x"):
            assert cli(capsys, "display", TARGET, "show", "--rate", bad)[0] == ExitCode.USAGE
        assert cli(capsys, "display", TARGET, "show", "--for", "-1")[0] == ExitCode.USAGE
        assert board.stats["connects"] == connects


class FakeTty(io.TextIOWrapper):
    """A terminal on stdout: ``isatty()`` is True; what is written is kept."""

    def __init__(self) -> None:
        self.raw_bytes = io.BytesIO()
        super().__init__(self.raw_bytes, encoding="utf-8", write_through=True)

    def isatty(self) -> bool:
        return True

    def text(self) -> str:
        self.flush()
        return self.raw_bytes.getvalue().decode("utf-8")


def on_tty(tty: FakeTty, *argv: str) -> int:
    real = sys.stdout
    sys.stdout = tty
    try:
        return main(["--pack", "fake", *argv])
    finally:
        sys.stdout = real


def test_show_on_a_truecolor_terminal_draws_half_blocks_in_place(via, monkeypatch, capsys):
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    with still_board() as board, reach(via, board, monkeypatch):
        tty = FakeTty()
        rc = on_tty(tty, "display", TARGET, "show", "--for", "0.8", "--rate", "5")
        capsys.readouterr()
        text = tty.text()
        assert rc == 0
        assert text.startswith(CD.ALT_SCREEN_ON) and CD.ALT_SCREEN_OFF in text
        k = CD.fit_scale(80, 24)
        assert k == 6
        rows = CD.halfblock(CARD, frozenset(range(w.NTILES)), k)
        assert len(rows) == 20 and all(r.count(CD.HALF) == 53 for r in rows)
        for i, row in enumerate(rows, 1):                     # the golden, drawn in place
            assert f"\x1b[{i};1H{row}\x1b[K" in text
        yellow = "38;2;255;255;0;48;2;255;255;0m"               # bar 1 (0xFFE0), both halves
        assert yellow in rows[5]
        assert "live  mode hw  owner harness  exact" in text
        # after the screen: the last status line, for the scrollback
        tail = text.split(CD.ALT_SCREEN_OFF, 1)[1]
        assert tail.startswith(f"{BOARD}  live") and "refreshes)" in tail
        # the twin: the same terminal without truecolor gets status lines, no escapes
        monkeypatch.delenv("COLORTERM", raising=False)
        tty2 = FakeTty()
        rc = on_tty(tty2, "display", TARGET, "show", "--for", "0.4", "--rate", "5")
        capsys.readouterr()
        assert rc == 0 and "\x1b[" not in tty2.text() and len(tty2.text().splitlines()) == 2


def test_truecolor_needs_a_terminal_that_says_so():
    tty = FakeTty()
    assert CD.truecolor(tty, {"COLORTERM": "truecolor"})
    assert CD.truecolor(tty, {"COLORTERM": "24bit"}) and CD.truecolor(tty, {"WT_SESSION": "x"})
    # twins: no claim, NO_COLOR, a dumb terminal, not a terminal
    assert not CD.truecolor(tty, {})
    assert not CD.truecolor(tty, {"COLORTERM": "truecolor", "NO_COLOR": "1"})
    assert not CD.truecolor(tty, {"COLORTERM": "truecolor", "TERM": "dumb"})
    assert not CD.truecolor(io.StringIO(), {"COLORTERM": "truecolor"})


def test_halfblock_greys_unknown_tiles_and_fits_the_terminal():
    rows = CD.halfblock(CARD, frozenset(range(1, w.NTILES)), 4)       # tile 0 unknown
    grey = rgb888(HATCH_RGB565)
    assert rows[0].startswith(f"\x1b[38;2;{grey[0]};{grey[1]};{grey[2]};")
    full = CD.halfblock(CARD, frozenset(range(w.NTILES)), 4)
    assert not full[0].startswith(f"\x1b[38;2;{grey[0]};{grey[1]};{grey[2]};")   # twin
    assert CD.fit_scale(400, 200) == 1 and CD.fit_scale(160, 61) == 2
    assert CD.fit_scale(80, 24) == 6 and CD.fit_scale(1, 1) == 320


class FakeKeys:
    """The console's key reader, scripted: ``keys`` arrives after ``after_s``."""

    def __init__(self, keys: bytes, after_s: float) -> None:
        self.keys, self.at = keys, time.monotonic() + after_s
        self.entered = self.exited = False

    def __enter__(self) -> FakeKeys:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def read(self, timeout: float) -> bytes:
        if time.monotonic() >= self.at and self.keys:
            out, self.keys = self.keys, b""
            return out
        time.sleep(min(timeout, 0.05))
        return b""


@pytest.mark.parametrize("key", [b"\x1d", b"q\x03"])
def test_ctrl_bracket_ends_the_view_as_in_console(key, monkeypatch, capsys):
    monkeypatch.delenv("COLORTERM", raising=False)
    with still_board() as board, reach("in-process", board, monkeypatch):
        keys = FakeKeys(key, 0.6)
        monkeypatch.setattr(CD, "_stdin_tty", lambda: True)
        monkeypatch.setattr(CD, "_stdout_tty", lambda: True)
        monkeypatch.setattr(CD, "_key_reader", lambda: keys)
        t0 = time.monotonic()
        # --for 8 is only the safety net: the key must end it long before
        rc, out, err = cli(capsys, "display", TARGET, "show", "--rate", "5", "--for", "8")
        took = time.monotonic() - t0
        assert rc == 0 and "Ctrl-] exits" in err
        assert keys.entered and keys.exited and 0.5 <= took < 5
        assert out.count("\r\n") >= 2                                # raw mode: CR LF
        # the twin: keys that never come; --for ends it
        keys = FakeKeys(b"", 0)
        monkeypatch.setattr(CD, "_key_reader", lambda: keys)
        rc, out, _ = cli(capsys, "display", TARGET, "show", "--for", "0.4", "--rate", "5")
        assert rc == 0 and out.count("\r\n") == 2 and keys.exited


# --- the client's pieces ------------------------------------------------------------------------------


def test_a_display_socket_close_maps_to_its_error():
    held = close_error(4000 + ExitCode.HELD, HELD)
    assert isinstance(held, HeldError) and held.code == ExitCode.HELD and held.message == HELD
    gone = close_error(4000 + ExitCode.UNAVAILABLE, BARE)
    assert isinstance(gone, UnavailableError) and gone.reason == BARE
    ended = close_error(1000, "closed: the lease was released")
    assert isinstance(ended, UnavailableError) and ended.reason == "closed: the lease was released"
    # the twin: an abnormal close is a transport failure, not a refusal
    assert isinstance(close_error(1006, ""), UnreachableError)


def test_the_picture_model_draws_what_the_compositor_sends():
    from harness_manager.services.display import DisplayService
    from tests.fakes.lm3_display_rig import FAST

    with still_board(G.card_picture(7)) as board:
        svc = DisplayService(timings=FAST)
        try:
            v = svc.attach(BOARD, FakeDisplayAdapter(board), ack=True)
            m = PictureModel()

            def drawn() -> bool:
                msg = v.next_message()
                if msg is not None:
                    v.ack(m.apply(msg))
                return m.presented and bytes(m.frame) == G.card_picture(7)

            wait_for(drawn, what="the keyframe drawn")
            assert m.valid == frozenset(range(w.NTILES)) and not m.hatched
            assert m.flags.exact and m.owner == w.OWNER_HARNESS
            # the twin: a message that is not one whole UPDATE is refused
            with pytest.raises(w.WireError):
                m.apply(b"LM\x01\x00\x00\x00\x00\x00")
        finally:
            svc.shutdown()


# --- help --------------------------------------------------------------------------------------------


def test_help_names_the_three_actions_and_ctrl_bracket(capsys):
    rc, out, _ = cli(capsys, "help", "display")
    flat = " ".join(out.split())
    assert rc == 0 and all(a in flat for a in ("snapshot", "status", "show"))
    assert "lab TARGET display" in flat                          # not the panel-owner verb
    rc, out, _ = cli(capsys, "help", "--tabs", "Front panel")
    assert "display TARGET show [--rate HZ] [--for SECONDS]" in out and "Ctrl-] exits" in out
    # twin: the lab tab's display is still the owner flip
    rc, out, _ = cli(capsys, "help", "--tabs", "Lab")
    assert "lab TARGET display harness|dut|toggle|query" in out
