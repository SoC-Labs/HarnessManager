"""FIX-PACK-8, integration: programming nanosoc_multicore needs the typed word, MULTICORE.

nanosoc_multicore's CPU1 boot ROM writes one 0x00 byte at DUT flash 0x20000 onward on every boot
until Linux v2.1 (release notes known issue 15), which damages a MicroPython image in the DUT
flash. Harness Manager cannot see what the DUT flash holds, so it warns on EVERY program of the
design and asks for the word (david: "warn + typed OK").

- the CLI over the T5 fake engine: a run with no terminal refuses (15) even with ``--yes``;
  ``--allow-dut-flash-write`` gives the consent for a script; at a terminal the word, typed,
  is the consent (and the answer to the y/N question); a word typed wrong refuses;
- the daemon's API over the showcase demo (the Linux board lists the design for its shell):
  ``allow_dut_flash_write`` in the deploy body, else 409 REFUSED with
  ``error.data.dut_flash_write`` and no job; the overlay list carries the declaration;
- the remote client forwards the consent; the HIL plans never program the design without it.

Each check has its negative twin (nanosoc, which declares nothing).
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from harness_manager.core.errors import ExitCode
from harness_manager.core.pack import DutFlashWrite, OverlayRef
from harness_manager_mps3.constants import KNOWN_DESIGNS, WRITES_DUT_FLASH
from tests.fakes.t5_fake_engine import SHELL_ID
from tests.integration import test_demo_showcase as _showcase

api = _showcase.showcase

WHY = ("nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte at 0x20000 onward) "
       "on every boot, until Linux v2.1: it damages a MicroPython image there (nanosoc_upy).")
TEXT = WHY + " Type MULTICORE to program it anyway."
MULTICORE = OverlayRef("nanosoc_multicore", "0x01000003", SHELL_ID, size_bytes=4096,
                       ip_class="arm-aaa", writes_dut_flash=DutFlashWrite(WHY, "MULTICORE"))


class Tty(io.StringIO):
    """A terminal's stdin: what the user types, and ``isatty()`` true."""

    def isatty(self) -> bool:
        return True


class Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


# --- the CLI ------------------------------------------------------------------------------------------


@pytest.fixture
def cli():
    from harness_manager.cli.engine import set_engine_factory
    from tests.fakes.t5_fake_engine import FakeEngine

    eng = FakeEngine()
    eng.st.overlays.append(MULTICORE)
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


def run_cli(capsys, monkeypatch, *argv: str, stdin: Any = None) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    monkeypatch.setattr("sys.stdin", stdin if stdin is not None else Pipe(""))
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_no_terminal_refuses_15_even_with_yes_and_programs_nothing(cli, capsys, monkeypatch):
    rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1", "nanosoc_multicore",
                            "--yes")
    assert rc == ExitCode.REFUSED == 15
    assert f"harness-manager: {TEXT} — nothing was programmed: run it in a terminal and type " \
           "the word, or pass --allow-dut-flash-write (--yes never implies it)" in err
    assert cli.st.deploy_dut_flash == [] and "deploy.deploy" not in cli.st.calls
    rc, out, _err = run_cli(capsys, monkeypatch, "--json", "program", "127.0.0.1",
                            "nanosoc_multicore", "--yes")
    e = json.loads(out)["error"]
    assert rc == 15 and e["name"] == "REFUSED" and e["message"] == TEXT
    assert e["data"]["dut_flash_write"] == {"design": "nanosoc_multicore", "why": WHY,
                                            "word": "MULTICORE"}
    assert cli.st.deploy_dut_flash == []


def test_cli_the_flag_gives_the_consent_for_a_script_with_the_warning(cli, capsys, monkeypatch):
    rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1", "nanosoc_multicore",
                            "--yes", "--allow-dut-flash-write")
    assert rc == 0, err
    assert f"WARNING: {WHY} Programming it anyway (--allow-dut-flash-write)." in err
    assert cli.st.deploy_dut_flash == [True]


def test_cli_at_a_terminal_the_typed_word_is_the_consent(cli, capsys, monkeypatch):
    # no --yes: the word answers the y/N question too (one prompt, not two)
    rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1", "nanosoc_multicore",
                            stdin=Tty("MULTICORE\n"))
    assert rc == 0, err
    assert f"WARNING: {TEXT}\n> " in err and "[y/N]" not in err
    assert cli.st.deploy_dut_flash == [True]
    # --yes does not imply it: the word is still asked for at a terminal
    rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1", "nanosoc_multicore",
                            "--yes", stdin=Tty("  MULTICORE  \n"))
    assert rc == 0 and f"WARNING: {TEXT}" in err
    assert cli.st.deploy_dut_flash == [True, True]


def test_twin_cli_a_word_typed_wrong_refuses_15(cli, capsys, monkeypatch):
    for typed in ("y\n", "multicore\n", ""):
        rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1",
                                "nanosoc_multicore", "--yes", stdin=Tty(typed))
        assert rc == 15, typed
        assert "not confirmed: nanosoc_multicore was not programmed (its boot code writes the " \
               "DUT's flash) — type exactly: MULTICORE (or pass --allow-dut-flash-write; " \
               "--yes never implies it)" in err
    assert cli.st.deploy_dut_flash == []


def test_twin_cli_a_design_that_declares_nothing_asks_nothing(cli, capsys, monkeypatch):
    rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1", "nanosoc", "--yes")
    assert rc == 0 and "WARNING" not in err and cli.st.deploy_dut_flash == [False]
    # and the flag on such a design changes nothing (the keyword is not even sent)
    rc, _out, err = run_cli(capsys, monkeypatch, "program", "127.0.0.1", "nanosoc", "--yes",
                            "--allow-dut-flash-write")
    assert rc == 0 and "WARNING" not in err and cli.st.deploy_dut_flash == [False, False]


def test_cli_overlays_marks_the_design(cli, capsys, monkeypatch):
    rc, out, err = run_cli(capsys, monkeypatch, "overlays", "127.0.0.1")
    assert rc == 0, err
    line = next(ln for ln in out.splitlines() if "nanosoc_multicore" in ln)
    assert "writes the DUT's flash" in line
    other = next(ln for ln in out.splitlines() if " nanosoc " in ln)
    assert "DUT" not in other


# --- the API --------------------------------------------------------------------------------------------


def test_api_without_the_consent_is_409_refused_with_the_data_and_no_job(api):
    from harness_manager.demo_showcase import BOARD_LINUX

    api.open(BOARD_LINUX)
    B = api.b(BOARD_LINUX)
    listed = {o["name"]: o for o in api.get(f"{B}/overlays")["loadable"]}
    assert listed["nanosoc_multicore"]["writes_dut_flash"] == {"why": WHY, "word": "MULTICORE"}
    assert listed["nanosoc_upy"]["writes_dut_flash"] is None
    err = api.get(f"{B}/deploy", method="POST", status=409,
                  json={"overlay": "nanosoc_multicore"})["error"]
    assert err["code"] == 15 and err["name"] == "REFUSED" and err["message"] == TEXT
    assert err["data"]["dut_flash_write"] == {"design": "nanosoc_multicore", "why": WHY,
                                              "word": "MULTICORE"}
    assert err["data"]["overlay"]["name"] == "nanosoc_multicore"
    assert api.engine.called("deploy.deploy") == []
    err = api.get(f"{B}/deploy", method="POST", status=409,     # false is no consent either
                  json={"overlay": "nanosoc_multicore", "allow_dut_flash_write": False})["error"]
    assert err["name"] == "REFUSED"
    bad = api.get(f"{B}/deploy", method="POST", status=400,
                  json={"overlay": "nanosoc_multicore", "allow_dut_flash_write": "yes"})
    assert "allow_dut_flash_write must be true or false" in bad["error"]["message"]
    assert api.engine.called("deploy.deploy") == []


def test_twin_api_with_the_consent_it_programs(api):
    from harness_manager.demo_showcase import BOARD_LINUX

    api.open(BOARD_LINUX)
    job = api.job(f"{api.b(BOARD_LINUX)}/deploy",
                  {"overlay": "nanosoc_multicore", "allow_dut_flash_write": True})
    assert job["result"]["verified"] is True and job["result"]["rm_id"] == "0x01000003"
    assert api.engine.called("deploy.allow_dut_flash_write") == [(BOARD_LINUX,
                                                                  "nanosoc_multicore")]


def test_twin_api_another_design_needs_no_consent(api):
    from harness_manager.demo_showcase import BOARD_LINUX

    api.open(BOARD_LINUX)
    job = api.job(f"{api.b(BOARD_LINUX)}/deploy", {"overlay": "nanosoc_upy"})
    assert job["result"]["verified"] is True
    assert api.engine.called("deploy.allow_dut_flash_write") == []


# --- the remote client and the HIL plans -----------------------------------------------------------------


def test_the_remote_client_sends_the_consent_only_when_given():
    from harness_manager.client.remote import RemoteDeploy

    bodies: list[dict] = []

    class Engine:
        def run_job(self, path, body, progress=None):
            bodies.append(body)
            return {"rm_id": "0x01000003", "verified": True, "seconds": 1.0}

    d = RemoteDeploy(Engine())  # type: ignore[arg-type]
    d._deploy("b", MULTICORE, allow_dut_flash_write=True)
    d._deploy("b", MULTICORE)
    assert bodies[0]["allow_dut_flash_write"] is True
    assert "allow_dut_flash_write" not in bodies[1]


def test_no_hil_plan_programs_a_design_that_writes_the_dut_flash_without_the_flag():
    from harness_manager.checks import plans as P

    writers = {KNOWN_DESIGNS[d] for d in WRITES_DUT_FLASH}
    programs = [c.argv for name in P.PLANS for c in P.build(name).checks()
                if c.argv[:1] == ("program",)]
    assert programs, "the plans program something (the scan would be empty otherwise)"
    for argv in programs:
        assert argv[2] not in writers or "--allow-dut-flash-write" in argv, argv
    # twin: today none programs one at all (HIL-AUTO: the multicore check is manual, OCD8)
    assert not [a for a in programs if a[2] in writers]
