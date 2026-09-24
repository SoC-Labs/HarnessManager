"""HARNESS-CAT (H7): ``harness-manager harness …`` end to end.

The real CLI main, the real Engine and MPS3 pack, a VirtualMps3 on the ILA static (release
1.1.0) over Ethernet + USB, and the HARNESS-DIST spike's signed multi-version catalogue on
127.0.0.1 (``tests/fakes/hcat_catalog.py``, a throwaway key). Also pins the ``harness``
TSV layouts (the T5 golden exempts them) and that ``update`` still works as T7's alias.
Every behaviour has a negative twin.
"""

from __future__ import annotations

import io
import json

import pytest

from harness_manager.cli import main as climain
from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.update import UpdateService
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.hcat_catalog import BoardRig, CatalogWorld


class Hub:
    host = "mapstone-dev.ecs.soton.ac.uk"
    target = "mps3_01_pl"

    def close(self) -> None:
        pass


class Leases:
    def __init__(self, lease: dict | None) -> None:
        self.lease = lease

    def view(self, hub) -> dict:
        return {"lease": self.lease, "hub": hub.host}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    world = CatalogWorld(tmp_path / "world")
    world.publish()
    rig = BoardRig(tmp_path, world, monkeypatch)
    knobs: dict = {"hub": None, "leases": None}

    def factory(_args):
        eng = Engine(EngineConfig(state_dir=rig.state_dir),
                     packs={"mps3": Mps3Pack(console_ports=rig.vb.console_ports)})
        svc = UpdateService(eng, trust=world.trust(), token="", app_version="0.1.0",
                            leases=knobs["leases"])
        eng._services["update"] = svc
        if knobs["hub"] is not None:
            opened = eng.open

            def open_behind_hub(cand, **kw):
                s = opened(cand, **kw)
                s.hub = knobs["hub"]
                return s

            eng.open = open_behind_hub
        return eng

    previous = set_engine_factory(factory)
    with world.serve() as srv:
        yield {"world": world, "rig": rig, "srv": srv, "knobs": knobs,
               "board": rig.cli_args(srv.source())}
    set_engine_factory(previous)
    rig.close()


def run(capsys, monkeypatch, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = climain.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def tsv_ok(out: str, layout: str) -> list[list[str]]:
    rows = [line.split("\t") for line in out.rstrip("\n").split("\n")]
    assert rows and all(len(r) == len(TSV_COLUMNS[layout]) for r in rows), out
    return rows


def at(c, *positionals: str) -> list[str]:
    """TARGET, then any positionals (an optional VERSION must come before the options:
    argparse), then the board's links and the channel source."""
    board = c["board"]
    return [board[0], *positionals, *board[1:]]


def links(c) -> list[str]:
    """TARGET and the board's links, without the channel source (unpin, history)."""
    board = c["board"]
    return board[:board.index("--source")]


def running_sha(c) -> str:
    return c["rig"].bound["booted"][-1]["sha"] if c["rig"].bound["booted"] else "d68dd0ed"


# --- list and show -----------------------------------------------------------------------------


def test_harness_list_shows_verdicts_and_marks_in_every_format(cli, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "list", *cli["board"], "--all")
    assert rc == ExitCode.OK, err
    obj = json.loads(out)
    assert obj["ok"] and obj["catalog"] == "mps3-harness" and obj["offer"] == "1.1.1"
    verdicts = {r["version"]: (r["verdict"], r["marks"]) for r in obj["releases"]}
    assert verdicts == {"2.0.0": ("needs-door", ["current"]),
                        "1.1.1": ("fits", ["current", "offered"]),
                        "1.1.0": ("fits", ["running"]),
                        "1.0.0": ("re-key", [])}
    assert "the 'dev' channel was not read" in " ".join(obj["warnings"])   # no dev: skipped
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "harness", "list", *cli["board"])
    rows = tsv_ok(out, "harness list")
    assert [r[0] for r in rows] == ["1.1.1", "1.1.0", "1.0.0"] and rows[2][6] == "re-key"
    rc, out, _ = run(capsys, monkeypatch, "harness", "list", *cli["board"])
    assert rc == 0 and "runs 1.1.0" in out and "REKEY 0x3f1a560f" in out


def test_negative_twin_list_without_a_channel_source_fails_and_prints_no_rows(cli, capsys,
                                                                             monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "--tsv", "harness", "list")
    assert rc != ExitCode.OK and out == "" and err.startswith("harness-manager: ")


def test_harness_show_gives_the_parts_the_changes_and_the_plan(cli, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "show", "1.0.0", *cli["board"])
    assert rc == ExitCode.OK, err
    obj = json.loads(out)
    assert obj["channel"] == "stable" and obj["plan"]["rekey"] is True
    assert len(obj["plan"]["fingerprint"]) == 64
    assert {c["name"] for c in obj["component_list"]} == {"sd-HBI0309C", "overlays-open",
                                                          "overlays-aaa"}
    aaa = next(c for c in obj["component_list"] if c["name"] == "overlays-aaa")
    assert aaa["private"] and aaa["skipped"] == "needs a GitHub token"
    assert obj["changes"]["static"]["changes"] is True
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "harness", "show", "1.0.0", "--source",
                     cli["srv"].source())
    tsv_ok(out, "harness show")
    # the twin: a version no channel lists
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "show", "9.9.9", "--source",
                       cli["srv"].source())
    assert rc == ExitCode.ABSENT and json.loads(out)["error"]["name"] == "ABSENT"


# --- install: consent, lease, the offer ---------------------------------------------------------


def test_a_rekey_install_needs_the_typed_phrase(cli, capsys, monkeypatch):
    vb = cli["rig"].vb
    before = vb.sd.snapshot()
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "install",
                       *at(cli, "1.0.0"), "--yes")
    assert rc == ExitCode.REFUSED and "RE-KEYS" in json.loads(out)["error"]["message"]
    assert vb.sd.snapshot() == before and vb.reboots == 0
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "install",
                       *at(cli, "1.0.0"), "--consent", "REKEY 0x3f1a560f")
    assert rc == ExitCode.OK, err
    obj = json.loads(out)
    assert obj["result"] == "installed" and obj["version"] == "1.0.0"
    assert cli["rig"].bound["booted"][-1]["static_id"].lower() == "0x3f1a560f"


def test_install_with_no_version_installs_the_offer_then_says_nothing_to_do(cli, capsys,
                                                                            monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "--tsv", "harness", "install", *cli["board"],
                       "--yes")
    assert rc == ExitCode.OK, err
    (row,) = tsv_ok(out, "harness install")
    assert row[1:4] == ["1.1.1", "installed", "1.1.0"] and running_sha(cli) == "0e12a0b0"
    rc, out, err = run(capsys, monkeypatch, "harness", "install", *cli["board"], "--yes")
    assert rc == ExitCode.ALREADY and "already runs harness 1.1.1" in err


@pytest.mark.parametrize("lease", [None, {"holder": "alice@lab-pc-07", "mine": False}])
def test_an_install_without_the_lease_exits_held_and_touches_nothing(cli, capsys, monkeypatch,
                                                                    lease):
    cli["knobs"].update(hub=Hub(), leases=Leases(lease))
    vb = cli["rig"].vb
    before = vb.sd.snapshot()
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "install",
                       *at(cli, "1.1.1"), "--yes")
    assert rc == ExitCode.HELD, err
    error = json.loads(out)["error"]
    assert error["holder"] == (lease or {}).get("holder", "nobody")
    assert "lease holder only" in error["message"]
    assert vb.sd.snapshot() == before and vb.reboots == 0


def test_negative_twin_the_lease_holder_installs(cli, capsys, monkeypatch):
    cli["knobs"].update(hub=Hub(), leases=Leases({"holder": "me@here", "mine": True}))
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "install",
                       *at(cli, "1.1.1"), "--yes")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["result"] == "installed"


# --- pin, history, rollback ----------------------------------------------------------------------


def test_pin_history_and_rollback(cli, capsys, monkeypatch):
    board = cli["board"]
    rc, out, err = run(capsys, monkeypatch, "--tsv", "harness", "pin", *at(cli, "1.1.0"))
    assert rc == ExitCode.OK, err
    assert tsv_ok(out, "harness pin")[0][1:] == ["1.1.0", "-"]
    rc, out, _ = run(capsys, monkeypatch, "--json", "harness", "list", *board)
    assert json.loads(out)["offer"] == "1.1.0"
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "harness", "unpin", *links(cli))
    assert tsv_ok(out, "harness unpin")[0][1:] == ["-", "1.1.0"]
    # install 1.1.1, then roll back to what it replaced
    assert run(capsys, monkeypatch, "harness", "install", *at(cli, "1.1.1"), "--yes")[0] == 0
    rc, out, err = run(capsys, monkeypatch, "--tsv", "harness", "rollback", *board, "--yes")
    assert rc == ExitCode.OK, err
    (row,) = tsv_ok(out, "harness rollback")
    assert row[1:4] == ["1.1.0", "installed", "re-install"] and running_sha(cli) == "d68dd0ed"
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "harness", "history", *links(cli))
    hist = tsv_ok(out, "harness history")
    assert [(h[3], h[5]) for h in hist] == [("1.1.0", "1.1.1"), ("1.1.1", "1.1.0")]


def test_negative_twin_rollback_to_another_static_still_needs_the_phrase(cli, capsys, monkeypatch):
    vb = cli["rig"].vb
    before = vb.sd.snapshot()
    rc, out, err = run(capsys, monkeypatch, "harness", "rollback", *cli["board"], "--to", "1.0.0",
                       "--yes")
    assert rc == ExitCode.REFUSED and "REKEY 0x3f1a560f" in err
    assert vb.sd.snapshot() == before
    rc, out, err = run(capsys, monkeypatch, "harness", "pin", *at(cli, "9.9.9"))
    assert rc == ExitCode.ABSENT                             # a pin to nothing listed


# --- fetch and mirror ------------------------------------------------------------------------------


def test_fetch_downloads_and_verifies_into_the_cache_then_finds_it_there(cli, capsys, monkeypatch):
    src = cli["srv"].source()
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "fetch", "1.1.1", "--source", src)
    assert rc == ExitCode.OK, err
    obj = json.loads(out)
    results = {c["name"]: c["result"] for c in obj["components"]}
    assert results == {"sd-HBI0309C": "fetched", "overlays-open": "fetched",
                       "overlays-aaa": "skipped"}
    rc, out, _ = run(capsys, monkeypatch, "--tsv", "harness", "fetch", "1.1.1", "--source", src)
    rows = tsv_ok(out, "harness fetch")
    assert {r[1]: r[4] for r in rows}["sd-HBI0309C"] == "cached"
    rc, out, _ = run(capsys, monkeypatch, "--json", "harness", "list", "--source", src)
    assert next(r for r in json.loads(out)["releases"] if r["version"] == "1.1.1")["cached"]
    # the twin: a version no channel lists
    assert run(capsys, monkeypatch, "harness", "fetch", "9.9.9", "--source", src)[0] == \
        ExitCode.ABSENT


def test_harness_mirror_leaves_arm_ip_out_by_default(cli, capsys, monkeypatch, tmp_path):
    world = cli["world"]
    stable = json.loads((world.www / "channel" / "stable" / "channel.json").read_text())
    aaa = {c["sha256"] for rel in stable["harness"]["releases"] for c in rel["components"]
           if c["name"] == "overlays-aaa"}
    assert len(aaa) == 2                  # 1.1.0 and 1.1.1 share one static: the same bytes
    dest = tmp_path / "mirror"
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "mirror", str(dest),
                       "--source", world.file_source())
    assert rc == ExitCode.OK, err
    (ch,) = json.loads(out)["channels"]
    assert set(ch["skipped"]) == {"overlays-aaa"} and "include_private" in ch["skipped"]["overlays-aaa"]
    blobs = {p.name for p in (dest / "blobs").iterdir()}
    assert blobs and not blobs & aaa
    # the mirror is a source: the catalogue lists from it with the origin gone
    rc, out, err = run(capsys, monkeypatch, "--json", "harness", "list", "--source", str(dest))
    assert rc == 0, err
    assert [r["version"] for r in json.loads(out)["releases"]] == ["1.1.1", "1.1.0", "1.0.0"]
    # the twin: --include-private copies them
    dest2 = tmp_path / "mirror-aaa"
    rc, out, err = run(capsys, monkeypatch, "--tsv", "harness", "mirror", str(dest2), "--source",
                       world.file_source(), "--include-private")
    assert rc == ExitCode.OK, err
    tsv_ok(out, "harness mirror")
    assert aaa <= {p.name for p in (dest2 / "blobs").iterdir()}


# --- the update alias --------------------------------------------------------------------------------


def test_the_update_alias_still_checks_and_installs(cli, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "check", *cli["board"])
    assert rc == ExitCode.OK, err
    plan = json.loads(out)["plan"]
    assert (plan["version"], plan["running_release"], plan["mode"]) == ("1.1.1", "1.1.0", "full")
    rc, out, err = run(capsys, monkeypatch, "--tsv", "update", "harness", *cli["board"], "--yes")
    assert rc == ExitCode.OK, err
    assert len(out.rstrip("\n").split("\t")) == len(TSV_COLUMNS["update harness"])
    assert running_sha(cli) == "0e12a0b0"
    # and the harness history saw it
    rc, out, _ = run(capsys, monkeypatch, "--json", "harness", "history", *links(cli))
    assert [h["version"] for h in json.loads(out)["history"]] == ["1.1.1"]


def test_negative_twin_the_update_alias_honours_a_pin(cli, capsys, monkeypatch):
    assert run(capsys, monkeypatch, "harness", "pin", *at(cli, "1.1.0"))[0] == 0
    rc, out, err = run(capsys, monkeypatch, "--json", "update", "check", *cli["board"])
    assert rc == ExitCode.OK, err
    assert json.loads(out)["plan"]["version"] == "1.1.0"
