"""Lane L4: ``harness-manager power show|cycle TARGET`` end to end.

The real CLI ``main()`` with ``cmd_power.register`` wired the way the lead will wire it,
the real Engine and MPS3 pack over ``VirtualMps3``, and T9's ``FakeShelly`` configured in
``boards.toml``; the plug and its driver share a fake clock, so a cycle costs no wall
time. Loopback only. Every check has a negative twin.
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from harness_manager.cli import cmd_power
from harness_manager.cli import main as climain
from harness_manager.cli.context import Ctx
from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.output import READING_COLUMNS, TSV_COLUMNS
from harness_manager.core.errors import ExitCode, UnavailableError
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.power import ina260
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.l4_service import share_clock, write_boards
from tests.fakes.t9_plugs import FakeClock, FakeShelly
from tests.fakes.t13_daemon import state_dir
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture(autouse=True)
def power_verb(monkeypatch) -> Iterator[None]:
    """``main()`` with ``power`` registered; the shared TSV table restored afterwards."""
    saved = dict(TSV_COLUMNS)
    original = climain.make_parser

    def make_parser() -> argparse.ArgumentParser:
        parser = original()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if "power" not in sub.choices:
            cmd_power.register(sub)
        return parser

    monkeypatch.setattr(climain, "make_parser", make_parser)
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    yield
    TSV_COLUMNS.clear()
    TSV_COLUMNS.update(saved)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def plug(clock) -> Iterator[FakeShelly]:
    with FakeShelly(clock=clock) as dev:
        yield dev


@pytest.fixture
def engine_factory(vboard: VirtualMps3, clock) -> Iterator[None]:
    class ClockedEngine(Engine):
        def open(self, candidate, note=""):          # the pack builds the driver at open
            session = super().open(candidate, note=note)
            if getattr(session, "power", None) is not None and \
                    hasattr(session.power.driver, "_clock"):
                share_clock(session, clock)
            return session

    previous = set_engine_factory(lambda _args: ClockedEngine(
        EngineConfig(state_dir=state_dir()),
        packs={"mps3": Mps3Pack(console_ports=vboard.console_ports)}))
    yield
    set_engine_factory(previous)


def run(capsys, monkeypatch, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = climain.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def with_plug(vb: VirtualMps3, plug: FakeShelly, extra: str = "") -> None:
    write_boards(f'[boards."mps3@{vb.shell_endpoint}".power]\nkind = "shelly_gen2"\n'
                 f'url = "{plug.url}"\n{extra}')


# --- show -----------------------------------------------------------------------------------------


def test_power_show_reads_the_plug(capsys, monkeypatch, vboard, plug, engine_factory):
    with_plug(vboard, plug)
    rc, out, _ = run(capsys, monkeypatch, "--json", "power", "show", vboard.shell_endpoint)
    obj = json.loads(out)
    assert rc == ExitCode.OK and obj["ok"] is True
    assert [r["value"] for r in obj["readings"]] == [11.4, 239.1, 0.071]
    assert obj["device"] == f"shelly_gen2 {plug.url} outlet 0" and obj["cycle_reason"] == ""
    rc, out, _ = run(capsys, monkeypatch, "power", "show", vboard.shell_endpoint)
    assert rc == 0 and "board_power      11.4 W" in out and "power cycle      yes" in out


def test_negative_twin_power_show_without_a_meter_is_unavailable_never_zero(
        capsys, monkeypatch, vboard, engine_factory):
    rc, out, _ = run(capsys, monkeypatch, "--json", "power", "show", vboard.shell_endpoint)
    obj = json.loads(out)
    assert rc == ExitCode.OK
    assert all(r["value"] is None and "no power sensor" in r["reason"] for r in obj["readings"])
    assert obj["device"] is None and "networked power plug" in obj["cycle_reason"]


def test_power_show_tsv_has_the_documented_columns(capsys, monkeypatch, vboard, plug,
                                                   engine_factory):
    with_plug(vboard, plug)
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "power", "show", vboard.shell_endpoint)
    rows = out.rstrip("\n").split("\n")
    assert rc == 0 and len(rows) == 3
    assert all(len(r.split("\t")) == len(READING_COLUMNS) for r in rows)
    assert TSV_COLUMNS["power show"] == READING_COLUMNS


# --- cycle ------------------------------------------------------------------------------------------


def test_power_cycle_switches_off_and_back_on(capsys, monkeypatch, vboard, plug, engine_factory):
    with_plug(vboard, plug)
    rc, out, err = run(capsys, monkeypatch, "--json", "power", "cycle", vboard.shell_endpoint,
                       "--off", "5", "--yes")
    obj = json.loads(out)
    assert rc == ExitCode.OK, err
    assert (obj["confirmed_off"], obj["confirmed_on"], obj["off_s"]) == (True, True, 5.0)
    assert obj["phases"] == ["off", "on"] and obj["board_id"] == f"mps3@{vboard.shell_endpoint}"
    assert plug.sets == [{"id": "0", "on": "false", "toggle_after": "5"}] and plug.output is True
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "power", "cycle", vboard.shell_endpoint, "--yes")
    assert rc == 0 and len(out.rstrip("\n").split("\t")) == len(TSV_COLUMNS["power cycle"])


def test_negative_twin_power_cycle_asks_first_and_no_means_no(capsys, monkeypatch, vboard, plug,
                                                              engine_factory):
    with_plug(vboard, plug)
    rc, _, err = run(capsys, monkeypatch, "power", "cycle", vboard.shell_endpoint, stdin="n\n")
    assert rc == ExitCode.REFUSED and "cut the power" in err
    assert plug.sets == []


@pytest.mark.parametrize("off", ["1", "301"])
def test_power_cycle_off_time_out_of_range_is_usage(capsys, monkeypatch, vboard, plug,
                                                    engine_factory, off):
    with_plug(vboard, plug)
    rc, _, err = run(capsys, monkeypatch, "power", "cycle", vboard.shell_endpoint, "--off", off,
                     "--yes")
    assert rc == ExitCode.USAGE and "out of range" in err and plug.sets == []


def test_power_cycle_on_a_meter_only_device_is_unavailable(capsys, monkeypatch, vboard,
                                                           engine_factory):
    class NoBridge:
        def __init__(self, device: int = 0) -> None:
            raise UnavailableError("ina260_mcp2221", "no MCP2221A here (test stub)")

    monkeypatch.setattr(ina260, "Mcp2221Bus", NoBridge)
    write_boards('[boards.lab]\nmatch = ["127.0.0.1"]\n'
                 'power = { kind = "ina260_mcp2221", i2c_address = 0x41, device = 1 }\n')
    rc, out, _ = run(capsys, monkeypatch, "--json", "power", "cycle", vboard.shell_endpoint,
                     "--yes")
    err = json.loads(out)["error"]
    assert rc == ExitCode.UNAVAILABLE and err["capability"] == "power_cycle"
    assert "only measures" in err["reason"]


def test_over_a_daemon_without_the_power_proxy_it_says_so(monkeypatch):
    from harness_manager.client import remote

    fake_remote = object.__new__(remote.RemoteEngine)          # no connection: never called
    ctx = Ctx(argparse.Namespace(), fake_remote, "json")
    with pytest.raises(UnavailableError, match="HARNESS_MANAGER_NO_DAEMON=1"):
        cmd_power._power(ctx, SimpleNamespace(power=None), "power_cycle")
    # Negative twin: a session WITH a power adapter is used as it is, remote or not.
    meter = SimpleNamespace(label="m")
    assert cmd_power._power(ctx, SimpleNamespace(power=meter), "power_cycle") is meter
