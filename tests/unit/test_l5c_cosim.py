"""L5c COSIM: the DUT clock service, CLI and API against the mint-4 fake board
(pyverify's FakeShell, profile ``linux_mint4``; loopback only)."""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from pyverify.testing.mint4_models import CosimModel

from harness_manager.cli import main as cli_main
from harness_manager.cli.main import main
from harness_manager.core.errors import (
    ActionFailedError,
    ClaimLockedError,
    ExitCode,
    UnavailableError,
    UsageError,
)
from harness_manager.daemon import app as daemon_app
from harness_manager.daemon.app import create_app
from harness_manager.services import cosim as CS
from harness_manager_mps3.shell import Mps3Shell
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import LINUX_HARNESSD, LINUX_MINT4, VirtualMps3

H = headers()
LOCAL_BOARD = "127.0.0.3"            # what the fake treats as "the board itself"


def session_for(vb: VirtualMps3, claim=None) -> SimpleNamespace:
    return SimpleNamespace(shell=Mps3Shell("127.0.0.1", vb.shell.control_port), claim=claim)


@pytest.fixture
def mint4(tmp_path: Path):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        yield vb


@pytest.fixture
def old_image(tmp_path: Path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        yield vb


# -- the service ---------------------------------------------------------------------------------


def test_halt_step_run_is_exact(mint4):
    s = session_for(mint4)
    assert CS.offered(s) == (True, "")
    assert CS.cycles(s)["running"] is True
    halted = CS.halt(s)
    assert (halted["mode"], halted["running"], halted["remaining"]) == ("halt", False, 0)
    before = CS.cycles(s)["cycles"]
    stepped = CS.step(s, 10000)
    assert stepped["n"] == 10000 and stepped["mode"] == "halt" and stepped["remaining"] == 0
    assert CS.cycles(s)["cycles"] == before + 10000 == stepped["cycles"]
    running = CS.run(s)
    assert (running["mode"], running["running"]) == ("run", True)
    assert list(running) == ["mode", "running", "cycles", "remaining"]


def test_reply_keys_are_in_the_contract_order(mint4):
    s = session_for(mint4)
    assert list(CS.step(s, 5)) == ["mode", "running", "cycles", "remaining", "n"]


@pytest.mark.parametrize("bad", [0, -1, 100_000_001, 1.5, "10", True, None])
def test_invalid_step_never_reaches_the_board(mint4, bad):
    s = session_for(mint4)
    with pytest.raises(UsageError) as e:
        CS.step(s, bad)
    assert "whole number of cycles from 1 to 100,000,000" in e.value.message
    assert mint4.shell.cosim.steps == []


def test_step_limits_are_accepted(mint4):
    s = session_for(mint4)
    assert CS.step(s, 1)["n"] == 1
    assert CS.step(s, CS.STEP_MAX)["n"] == CS.STEP_MAX
    assert mint4.shell.cosim.steps == [1, CS.STEP_MAX]


def test_locked_text_when_claimed_and_no_ssh(mint4):
    mint4.shell.ssh_claimed = True
    mint4.shell.trusted_peer = LOCAL_BOARD
    s = session_for(mint4)
    with pytest.raises(ClaimLockedError) as e:
        CS.halt(s)
    assert e.value.message == "Claim the board to stop its clock"
    assert CS.cycles(s)["running"] is True          # reading is open


def test_locked_stays_locked_when_the_claim_is_not_ours(mint4):
    mint4.shell.ssh_claimed = True
    mint4.shell.trusted_peer = LOCAL_BOARD

    class NotOurs:
        def lock_route(self, what, **kw):
            if kw.get("claimed"):
                raise ClaimLockedError("raw words from the claim", hint="their hint")
            return ""

    with pytest.raises(ClaimLockedError) as e:
        CS.step(session_for(mint4, NotOurs()), 10)
    assert e.value.message == CS.LOCKED
    assert mint4.shell.cosim.steps == []


def test_no_feature_is_not_offered(old_image):
    s = session_for(old_image)
    assert CS.offered(s) == (False, "This board's image has no clock control")
    for call in (CS.cycles, CS.halt, CS.run, lambda x: CS.step(x, 10)):
        with pytest.raises(UnavailableError) as e:
            call(s)
        assert e.value.message == "This board's image has no clock control"
        assert e.value.code == ExitCode.UNAVAILABLE


@pytest.mark.parametrize("code,cls,text", [
    ("not_supported", UnavailableError, CS.NOT_SUPPORTED),
    ("no_hw", UnavailableError, CS.NO_HW),
    ("locked", ClaimLockedError, CS.LOCKED),
    ("timeout", ActionFailedError, "The step did not finish in 5 seconds; the clock was left as it is"),
])
def test_error_codes_become_sentences(code, cls, text):
    err = CS.refusal({"ok": False, "err": "raw", "code": code})
    assert isinstance(err, cls) and err.message == text


def test_no_hw_when_the_block_is_absent(tmp_path):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        vb.shell.cosim = CosimModel(vb.shell, {"present": False})
        with pytest.raises(UnavailableError) as e:
            CS.cycles(session_for(vb))
        assert e.value.message == CS.NO_HW


def test_swap_style_restore_is_run_again(mint4):
    s = session_for(mint4)
    CS.halt(s)
    mint4.shell.cosim.force_run()
    assert CS.cycles(s)["running"] is True


# -- the CLI -------------------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    from harness_manager.cli import cmd_cosim

    original = cli_main.make_parser

    def make_parser() -> argparse.ArgumentParser:
        parser = original()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if "cosim" not in sub.choices:
            cmd_cosim.register(sub)
        return parser

    monkeypatch.setattr(cli_main, "make_parser", make_parser)


def test_cli_cycles_halt_step_run(cli, mint4, capsys):
    t = mint4.shell_endpoint
    assert main(["cosim", "halt", t]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "clock stopped at" in out and "Serial input and the network receive" in out
    assert main(["--json", "cosim", "cycles", t]) == ExitCode.OK
    base = json.loads(capsys.readouterr().out)
    assert base["ok"] is True and base["act"] == "cycles" and base["running"] is False
    assert main(["--json", "cosim", "step", t, "10000"]) == ExitCode.OK
    stepped = json.loads(capsys.readouterr().out)
    assert stepped["cycles"] == base["cycles"] + 10000 and stepped["n"] == 10000
    assert main(["cosim", "step", t, "10,000"]) == ExitCode.OK
    assert "ran 10,000 cycles and stopped at" in capsys.readouterr().out
    assert main(["--tsv", "cosim", "run", t]) == ExitCode.OK
    row = capsys.readouterr().out.rstrip("\n").split("\t")
    assert row[1:4] == ["run", "run", "true"] and row[6] == "-"


def test_cli_bad_step_is_a_usage_error(cli, mint4, capsys):
    assert main(["cosim", "step", mint4.shell_endpoint, "0"]) == ExitCode.USAGE
    assert main(["cosim", "step", mint4.shell_endpoint, "many"]) == ExitCode.USAGE
    assert mint4.shell.cosim.steps == []


def test_cli_claimed_board_says_claim_it(cli, mint4, capsys):
    mint4.shell.ssh_claimed = True
    mint4.shell.trusted_peer = LOCAL_BOARD
    assert main(["cosim", "halt", mint4.shell_endpoint]) == ExitCode.REFUSED
    assert "Claim the board to stop its clock" in capsys.readouterr().err
    assert main(["cosim", "cycles", mint4.shell_endpoint]) == ExitCode.OK


def test_cli_no_feature_exits_12(cli, old_image, capsys):
    assert main(["cosim", "cycles", old_image.shell_endpoint]) == ExitCode.UNAVAILABLE
    assert "This board's image has no clock control" in capsys.readouterr().err


# -- the API -------------------------------------------------------------------------------------


@pytest.fixture
def api_for(monkeypatch):
    monkeypatch.setattr(daemon_app, "EXTENSIONS", tuple(daemon_app.EXTENSIONS) + ("cosim_api",))

    def make(vb):
        eng = engine_for(vb)
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "t"},
                       headers=H)
            assert r.status_code == 200, r.text
            yield c, bid_path(r.json()["board_id"])
        eng.close_all()

    return make


def test_api_state_and_acts(api_for, mint4):
    for c, base in api_for(mint4):
        st = c.get(f"{base}/cosim", headers=H).json()
        assert st["offered"] is True and st["running"] is True and "Serial input" in st["limitation"]
        assert c.post(f"{base}/cosim", json={"act": "halt"}, headers=H).json()["running"] is False
        r = c.post(f"{base}/cosim", json={"act": "step", "n": 10000}, headers=H).json()
        assert (r["act"], r["n"], r["mode"]) == ("step", 10000, "halt")
        assert c.get(f"{base}/cosim", headers=H).json()["cycles"] == r["cycles"]
        assert c.post(f"{base}/cosim", json={"act": "run"}, headers=H).json()["running"] is True


def test_api_bad_requests_are_400_before_the_board(api_for, mint4):
    for c, base in api_for(mint4):
        for body in ({"act": "step"}, {"act": "step", "n": 0}, {"act": "stop"}, {}):
            r = c.post(f"{base}/cosim", json=body, headers=H)
            assert r.status_code == 400, (body, r.text)
        assert mint4.shell.cosim.steps == []


def test_api_no_feature_is_not_offered(api_for, old_image):
    for c, base in api_for(old_image):
        st = c.get(f"{base}/cosim", headers=H).json()
        assert st["offered"] is False and st["reason"] == "This board's image has no clock control"
        assert "cycles" not in st
        r = c.post(f"{base}/cosim", json={"act": "halt"}, headers=H)
        assert r.status_code == 422
        assert r.json()["error"]["message"] == "This board's image has no clock control"


# -- the card (static) ---------------------------------------------------------------------------


def test_card_source_has_its_parts():
    js = (Path(__file__).parents[2] / "src/harness_manager/web/static/js/sections/cosim.js"
          ).read_text()
    for needle in ('testid="cosim-card"', "export function CosimCard", "export function mountCosim",
                   'includes(FEATURE)', "Claim the board to stop its clock",
                   "Serial input and the network receive", "cosimState", "cosimAct"):
        assert needle in js, needle
