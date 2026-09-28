"""Lane LOCATE on the real harness-manager-daemon: ``POST /boards/{bid}/identify``.

The app wraps the real Engine and MPS3 pack over ``PanelVirtualMps3``: ``LINUX_LOCATE`` is the
Linux harness's rc2_v7/v7n image as the Linux lead confirmed it (``locate`` only, no
``hello``/``panel``: docs/design/BOARD_LOCATE.md §2), and the fielded v0.11 has none.
Each behaviour has its negative twin.
"""

from __future__ import annotations

import time

from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3.capabilities import NEEDS_LOCATE
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.clcd_panel_shell import LINUX_LOCATE, LINUX_PANEL, V011_BARE_METAL
from tests.fakes.l4_service import H
from tests.fakes.t13_daemon import bid_path
from tests.integration.test_p1_panel_api import rig


def known(r, client) -> str:
    """The board is known to the daemon (a probe found it) but not open here."""
    got = client.post("/api/v1/probe", json={"hosts": [r.vb.shell_endpoint], "scan_usb": False,
                                             "scan_network": False, "timeout_s": 5}, headers=H)
    assert got.status_code == 200, got.text
    (cand,) = got.json()["candidates"]
    assert "locate" in cand["identity"]["features"] or r.vb.shell.panel_verbs == set()
    assert cand["board_id"] not in r.engine.open_boards()
    return cand["board_id"]


def post(client, bid, seconds=None):
    body = {} if seconds is None else {"seconds": seconds}
    return client.post(f"{bid_path(bid)}/identify", json=body, headers=H)


# --- the rate limit -----------------------------------------------------------------------------


def test_one_start_per_board_every_10_s_is_409_already_with_the_wait(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = r.open(client)
        first = post(client, bid, 5).json()
        assert first["ok"] and first["seconds"] == 5 and first["next_at"] > time.time() + 9
        assert 4900 <= first["until_ms"] <= 5000, "the board's own until_ms, for the countdown"
        again = post(client, bid, 5)
        err = again.json()["error"]
        assert again.status_code == 409 and err["name"] == "ALREADY"
        assert 0 < err["data"]["retry_after_s"] <= 10 and "try again in" in err["hint"]
        assert [q["s"] for q in r.vb.shell.locates] == [5], "the second never reached the board"


def test_twin_a_stop_goes_at_once_and_a_start_goes_again_after_the_window(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = r.open(client)
        r.presence.limiter.every_s = 0.3
        assert post(client, bid, 5).json()["ok"]
        stop = post(client, bid, 0).json()
        assert stop["ok"] and stop["until_ms"] == 0, "a stop is never limited"
        assert r.vb.shell.locates[-1] == {"op": "locate", "s": 0} and not r.vb.shell.blinking
        time.sleep(0.4)
        assert post(client, bid, 5).json()["ok"]
        assert [q["s"] for q in r.vb.shell.locates] == [5, 0, 5]


def test_v7_the_daemon_answers_the_boards_relative_until_ms(tmp_path, monkeypatch):
    """V7-ALIGN: until_ms is RELATIVE on the wire and in the daemon's answer: 3000 from the
    board is about 3000 here (and ``until`` is about now + 3)."""
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = r.open(client)
        monkeypatch.setattr(r.vb.shell, "_op_locate",
                            lambda req: {"ok": True, "op": "locate", "until_ms": 3000})
        body = post(client, bid, 5).json()
        assert body["ok"] and 2900 <= body["until_ms"] <= 3000
        assert 2.5 < body["until"] - time.time() <= 3.5


def test_v7_twin_an_absolute_until_ms_from_a_board_is_not_passed_on(tmp_path, monkeypatch):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = r.open(client)
        epoch_ms = int(time.time() * 1000) + 5000
        monkeypatch.setattr(r.vb.shell, "_op_locate",
                            lambda req: {"ok": True, "op": "locate", "until_ms": epoch_ms})
        body = post(client, bid, 5).json()
        assert body["ok"] and 4900 <= body["until_ms"] <= 5000, "the asked 5 s, not years"


def test_the_board_hears_exactly_op_s_who_via_harness_manager(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = r.open(client)
        assert post(client, bid).json()["ok"]
        (sent,) = r.vb.shell.locates
        assert set(sent) == {"op", "s", "who"} and sent["s"] == 5
        assert sent["who"].endswith((" via Harness Manager", " via HM")) and len(sent["who"]) <= 30
        assert r.vb.shell.banner_text == f"IDENTIFY: {sent['who']}"
        assert not {"hello", "panel"} & {q.get("op") for q in r.vb.shell.requests}, \
            "rc2_v7 has no hello/panel: none is ever sent"


def test_twin_the_dut_owning_the_panel_still_blinks_the_backlight(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = r.open(client)
        r.vb.shell.display_owner = r.vb.shell.display_target = "dut"
        assert post(client, bid).json()["ok"]
        assert r.vb.shell.blinking and r.vb.shell.banner_text == ""


# --- a board that is not open here ------------------------------------------------------------


def test_a_known_board_not_open_here_is_opened_for_the_one_identify(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = known(r, client)
        got = post(client, bid).json()
        assert got["ok"] and got["seconds"] == 5 and got["opened_for_identify"] is True
        assert r.vb.shell.locates[-1]["s"] == 5
        assert r.engine.open_boards() == [], "closed again"
        assert r.presence.tracked() == [], "never tracked: no hello rides on it"
        assert r.vb.shell.hellos == []
        opened = client.post("/api/v1/boards", json={"target": r.vb.shell_endpoint}, headers=H)
        assert opened.status_code == 200, "its lock was released"


def test_twin_a_board_another_engine_holds_is_409_held_and_nothing_is_sent(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        bid = known(r, client)
        other = Engine(EngineConfig(state_dir=r.engine.config.state_dir),
                       packs={"mps3": Mps3Pack(console_ports=r.vb.console_ports)})
        other.open(r.vb.candidate(), note="someone else")
        try:
            resp = post(client, bid, 5)
            assert resp.status_code == 409 and resp.json()["error"]["name"] == "HELD"
            assert r.vb.shell.locates == []
            assert r.presence.limiter.wait_s(bid) == 0.0, "a refused start costs no wait"
        finally:
            other.close_all()


def test_twin_a_board_this_daemon_never_saw_is_404(tmp_path):
    with rig(tmp_path, LINUX_LOCATE) as (r, client):
        resp = post(client, "mps3@192.0.2.9:6900", 5)
        assert resp.status_code == 404 and resp.json()["error"]["name"] == "ABSENT"
        assert r.vb.shell.locates == []


def test_bare_metal_not_open_here_is_422_with_the_reason_and_sends_nothing(tmp_path):
    with rig(tmp_path, V011_BARE_METAL) as (r, client):
        bid = known(r, client)
        resp = post(client, bid, 5)
        err = resp.json()["error"]
        assert resp.status_code == 422 and err["reason"] == NEEDS_LOCATE
        assert "locate" not in {q.get("op") for q in r.vb.shell.requests}
        assert r.engine.open_boards() == []


# --- never background ----------------------------------------------------------------------------


def test_no_background_contact_ever_sends_a_locate(tmp_path):
    """The presence beat runs (a page views the board; a harness with R1/R2, so there IS a
    beat) and the panel is read in the background: no locate."""
    with rig(tmp_path, LINUX_PANEL, beat=True) as (r, client):
        bid = r.open(client)
        deadline = time.monotonic() + 15
        while not r.vb.shell.hellos and time.monotonic() < deadline:
            time.sleep(0.05)
        client.get(f"{bid_path(bid)}/panel", headers={**H, "X-HM-Background": "1"})
        time.sleep(1.5)
        assert r.vb.shell.hellos, "the beat ran"
        assert r.vb.shell.locates == [] and "locate" not in {
            q.get("op") for q in r.vb.shell.requests}


def test_twin_the_explicit_request_is_the_one_locate(tmp_path):
    with rig(tmp_path, LINUX_LOCATE, beat=True) as (r, client):
        bid = r.open(client)
        assert post(client, bid).json()["ok"]
        time.sleep(1.5)
        assert [q["s"] for q in r.vb.shell.locates] == [5]


# --- the CLI -------------------------------------------------------------------------------------


def _run(capsys, *argv: str):
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_identify_blinks_5_s_by_default(tmp_path, capsys, monkeypatch):
    import json

    from harness_manager.cli.engine import set_engine_factory
    from tests.fakes.clcd_panel_shell import PanelVirtualMps3
    from tests.fakes.t13_daemon import engine_for

    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    with PanelVirtualMps3(tmp_path, LINUX_LOCATE) as vb:
        previous = set_engine_factory(lambda _args: engine_for(vb))
        try:
            rc, out, _ = _run(capsys, "--json", "identify", vb.shell_endpoint)
            body = json.loads(out)
            assert rc == 0 and body["seconds"] == 5 and body["until_ms"] > 4000
            assert vb.shell.locates[-1] == {"op": "locate", "s": 5, "who": vb.shell.locate_who}
            assert vb.shell.locate_who.endswith((" via Harness Manager", " via HM"))
            rc, out, _ = _run(capsys, "help", "identify")
            assert rc == 0 and "IDENTIFY banner" in out and "default 5" in out
        finally:
            set_engine_factory(previous)


def test_twin_cli_a_second_identify_through_the_daemon_within_10_s_is_exit_8(tmp_path, capsys,
                                                                            monkeypatch):
    from harness_manager.core.errors import ExitCode
    from tests.fakes.clcd_panel_shell import PanelVirtualMps3
    from tests.fakes.t13_daemon import LiveDaemon, engine_for

    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
    with PanelVirtualMps3(tmp_path, LINUX_LOCATE) as vb:
        eng = engine_for(vb)
        with LiveDaemon(eng):
            rc, _out, _ = _run(capsys, "identify", vb.shell_endpoint)
            assert rc == 0
            rc, out, err = _run(capsys, "identify", vb.shell_endpoint)
            assert rc == ExitCode.ALREADY and out == "" and "try again in" in err
            assert [q["s"] for q in vb.shell.locates] == [5]
        eng.close_all()
