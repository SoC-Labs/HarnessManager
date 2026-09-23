"""T7: ``socharness update …`` end to end: the real CLI main, the real Engine and MPS3 pack,
the virtual board over Ethernet + USB, and the fake channel on 127.0.0.1.

``cli/main.py`` registers ``update`` (lead, after the T7 merge). The engine factory
gives the engine an ``update`` service that trusts the test keys (the build pins none yet).
"""

from __future__ import annotations

import io
import json

import pytest

from socharness.cli import cmd_update
from socharness.cli import main as climain
from socharness.cli.engine import set_engine_factory
from socharness.cli.output import TSV_COLUMNS
from socharness.core.errors import ExitCode
from socharness.core.services import EngineConfig
from socharness.engine import Engine
from socharness.services.update import UpdateService
from socharness.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from socharness_board_mps3 import mcc as mccmod
from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_board import FakeUv, bind_identity_to_sd
from tests.fakes.t7_bundles import NEW_STATIC, NEW_USERCODE, Release
from tests.fakes.virtual_board import VirtualMps3

KEYS = TestKeys()


@pytest.fixture
def vb(tmp_path):
    with VirtualMps3(tmp_path / "board", usb=True) as board:
        yield board


@pytest.fixture
def cli(vb, tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    vb.mcc.clock = clock
    vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
    state = tmp_path / "state"
    uv = FakeUv()

    def factory(_args):
        eng = Engine(EngineConfig(state_dir=state),
                     packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        app = AppUpdater(AppLayout(state / "update" / "app"), LocalBusyProbe(state), uv="/opt/uv",
                         runner=uv, python_version="3.11", running_version="0.1.0")
        # engine.update is a lazy service; seed it with one that trusts the test keys.
        eng._services["update"] = UpdateService(eng, trust=KEYS.trust(), token="",
                                                app_version="0.1.0", app_updater=app)
        return eng

    previous = set_engine_factory(factory)
    with FakeChannelServer(tmp_path / "www") as srv:
        yield {"srv": srv, "builder": ChannelBuilder(srv.root, KEYS), "vb": vb, "clock": clock,
               "uv": uv, "art": tmp_path / "art"}
    set_engine_factory(previous)


def run(capsys, monkeypatch, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = climain.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def board_args(c) -> list[str]:
    vb = c["vb"]
    return [vb.shell_endpoint, "--serial", vb.mcc_url, "--volume", str(vb.sd.root),
            "--source", c["srv"].source()]


def publish(c, *releases: Release, serial: int = 1, **kw) -> None:
    for i, r in enumerate(releases):
        r.add_to(c["builder"], c["art"], current=(i == len(releases) - 1), **kw)
    c["builder"].publish(serial=serial)


def test_update_check_without_a_board(cli, capsys, monkeypatch):
    publish(cli, Release.fielded(), Release("1.1.0"))
    rc, out, _ = run(capsys, monkeypatch, "--json", "update", "check", "--source",
                     cli["srv"].source())
    obj = json.loads(out)
    assert rc == ExitCode.OK and obj["ok"] and obj["harness_current"] == "1.1.0"
    assert obj["serial"] == 1 and obj["signed_by"] == KEYS.release.public.id_hex


def test_update_check_with_a_board_shows_the_plan_and_touches_nothing(cli, capsys, monkeypatch):
    publish(cli, Release.fielded(), Release("1.1.0"))
    before = cli["vb"].sd.snapshot()
    rc, out, _ = run(capsys, monkeypatch, "--json", "update", "check", *board_args(cli))
    plan = json.loads(out)["plan"]
    assert rc == ExitCode.OK and plan["mode"] == "full" and plan["running_release"] == "1.0.0"
    assert cli["vb"].sd.snapshot() == before and cli["vb"].reboots == 0


def test_update_check_tsv_has_the_documented_columns(cli, capsys, monkeypatch):
    publish(cli, Release("1.1.0"))
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "update", "check", "--source",
                     cli["srv"].source())
    assert rc == 0 and len(out.rstrip("\n").split("\t")) == len(cmd_update.UPDATE_TSV["update check"])


def _tsv_cols(out: str, layout: str) -> None:
    rows = out.rstrip("\n").split("\n")
    assert rows and all(len(r.split("\t")) == len(TSV_COLUMNS[layout]) for r in rows), out


# The T5 golden test exempts the update layouts; these pin them (lead, after the T7 merge).
def test_update_harness_and_app_tsv_have_the_documented_columns(cli, capsys, monkeypatch):
    bind_identity_to_sd(cli["vb"])
    publish(cli, Release.fielded(), Release("1.1.0"))
    rc, out, err = run(capsys, monkeypatch, "--tsv", "update", "harness", *board_args(cli), "--yes")
    assert rc == ExitCode.OK, err
    _tsv_cols(out, "update harness")
    cli["builder"].add_app("0.2.0", AssetFile("socharness-0.2.0-py3-none-any.whl", b"PK-wheel"))
    cli["builder"].publish(serial=2)
    rc, out, err = run(capsys, monkeypatch, "--tsv", "update", "app", "--yes", "--source",
                       cli["srv"].source())
    assert rc == ExitCode.OK, err
    _tsv_cols(out, "update app")


def test_update_rollback_tsv_has_the_documented_columns(cli, capsys, monkeypatch):
    bind_identity_to_sd(cli["vb"], stale=True)          # written, not running: then roll back
    publish(cli, Release.fielded(), Release("1.1.0"))
    vb = cli["vb"]
    rc, _, _ = run(capsys, monkeypatch, "update", "harness", *board_args(cli), "--yes")
    assert rc == ExitCode.ACTION_FAILED
    rc, out, err = run(capsys, monkeypatch, "--tsv", "update", "rollback", vb.shell_endpoint,
                       "--serial", vb.mcc_url, "--volume", str(vb.sd.root), "--yes")
    assert rc == ExitCode.OK, err
    _tsv_cols(out, "update rollback")


def test_a_tampered_channel_exits_15(cli, capsys, monkeypatch):
    publish(cli, Release("1.1.0"))
    path = cli["srv"].root / "channel" / "stable" / "channel.json"
    path.write_bytes(path.read_bytes().replace(b"1.1.0", b"1.1.1"))
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "check", "--source",
                       cli["srv"].source())
    assert rc == ExitCode.REFUSED and "signature" in err
    assert json.loads(out)["error"]["name"] == "REFUSED"


def test_update_harness_installs_and_confirms(cli, capsys, monkeypatch):
    bind_identity_to_sd(cli["vb"])
    publish(cli, Release.fielded(), Release("1.1.0"))
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "harness", *board_args(cli),
                       "--yes")
    obj = json.loads(out)
    assert rc == ExitCode.OK, err
    assert obj["result"] == "installed" and obj["identity_after"]["harness"] == "1.1.0"
    assert "backup-sd" in err and "update reboot" in err               # plan + progress on stderr
    rc, out, _ = run(capsys, monkeypatch, "update", "harness", *board_args(cli), "--yes")
    assert rc == ExitCode.ALREADY


def test_update_harness_asks_first_and_no_means_no(cli, capsys, monkeypatch):
    publish(cli, Release.fielded(), Release("1.1.0"))
    before = cli["vb"].sd.snapshot()
    rc, _, err = run(capsys, monkeypatch, "update", "harness", *board_args(cli), stdin="n\n")
    assert rc == ExitCode.REFUSED and "not confirmed" in err
    assert cli["vb"].sd.snapshot() == before and cli["vb"].reboots == 0


def test_yes_alone_never_rekeys(cli, capsys, monkeypatch):
    bind_identity_to_sd(cli["vb"])
    publish(cli, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE), rekey=True)
    before = cli["vb"].sd.snapshot()
    rc, _, err = run(capsys, monkeypatch, "update", "harness", *board_args(cli), "--yes")
    assert rc == ExitCode.REFUSED and f"REKEY {NEW_STATIC}" in err
    assert cli["vb"].sd.snapshot() == before and cli["vb"].reboots == 0
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "harness", *board_args(cli),
                       "--consent", f"REKEY {NEW_STATIC}")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["identity_after"]["shell_id"] == NEW_STATIC


def test_typed_consent_at_the_prompt(cli, capsys, monkeypatch):
    bind_identity_to_sd(cli["vb"])
    publish(cli, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE), rekey=True)
    rc, _, err = run(capsys, monkeypatch, "update", "harness", *board_args(cli),
                     stdin="yes\n")
    assert rc == ExitCode.REFUSED and cli["vb"].reboots == 0
    rc, _, err = run(capsys, monkeypatch, "update", "harness", *board_args(cli),
                     stdin=f"REKEY {NEW_STATIC}\n")
    assert rc == ExitCode.OK, err


def test_written_not_running_exits_6_with_the_restore_command(cli, capsys, monkeypatch):
    bind_identity_to_sd(cli["vb"], stale=True)
    publish(cli, Release.fielded(), Release("1.1.0"))
    before = cli["vb"].sd.snapshot()
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "harness", *board_args(cli),
                       "--yes")
    obj = json.loads(out)
    assert rc == ExitCode.ACTION_FAILED and "written, not running" in err
    endpoint = cli["vb"].shell_endpoint
    assert f"socharness update rollback {endpoint}" in obj["error"]["hint"]
    assert obj["error"]["data"]["outcome"]["result"] == "written-not-running"
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "rollback", endpoint, "--serial",
                       cli["vb"].mcc_url, "--volume", str(cli["vb"].sd.root), "--yes")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["result"] == "restored" and cli["vb"].sd.snapshot() == before


def test_update_app_stages_and_switches(cli, capsys, monkeypatch):
    cli["builder"].add_app("0.2.0", AssetFile("socharness-0.2.0-py3-none-any.whl", b"PK-wheel"))
    cli["builder"].publish(serial=1)
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "app", "--yes", "--source",
                       cli["srv"].source())
    obj = json.loads(out)
    assert rc == ExitCode.OK, err
    assert obj["switched"] and obj["pointer"]["current"] == "0.2.0" and "warning" in obj
    rc, _, err = run(capsys, monkeypatch, "update", "rollback", "--app", "--yes")
    assert rc == ExitCode.REFUSED and "no previous" in err


def test_update_rollback_needs_a_target_or_app(cli, capsys, monkeypatch):
    rc, _, err = run(capsys, monkeypatch, "update", "rollback")
    assert rc == ExitCode.USAGE
