"""Lead-owned checks for the contract changes applied at the Wave 2 merges (T14 CCRs).

- T14-3: while the harness is down (rescue), capabilities that need its Ethernet
  services are unavailable with the harness state as the reason.
- T14-4: ``/session`` says whether each engine service works, or why not.
- T14-5: a HELD caused by a daemon job names the job in ``error.data``.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from socharness.core import capabilities as C
from socharness.core.pack import ProbeHints
from socharness.core.services import EngineConfig
from socharness.daemon.jobs import busy_error
from socharness.engine import Engine
from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import LINUX_HARNESSD, VirtualMps3

with warnings.catch_warnings():
    warnings.simplefilter("ignore")   # starlette: httpx with its TestClient is deprecated
    from fastapi.testclient import TestClient

from socharness.daemon.app import create_app  # noqa: E402


def _rescue_engine(vb: VirtualMps3, tmp_path: Path) -> Engine:
    return Engine(EngineConfig(state_dir=tmp_path / "state"),
                  packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})


def test_a_rescue_board_loses_the_capabilities_that_need_the_harness(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        vb.enter_rescue("slot A and B failed CRC")
        eng = _rescue_engine(vb, tmp_path)
        try:
            (cand,) = eng.probe(ProbeHints(hosts=(vb.shell_endpoint,), scan_usb=False,
                                           timeout_s=0.5))
            eng.open(cand)
            info = eng.info(cand.board_id)
        finally:
            eng.close_all()
    assert info.health.control_channel == "rescue"
    for cap in (C.DEPLOY_PARTIAL, C.CONSOLE_DUT, C.DEBUG_DUT, C.RESET_DUT):
        assert cap not in info.capabilities
        assert info.unavailable[cap] == "the harness is rescue (see Health)"


def test_negative_twin_a_running_board_keeps_them(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        eng = _rescue_engine(vb, tmp_path)
        try:
            cand = eng.candidate_for(vb.shell_endpoint)
            eng.open(cand)
            info = eng.info(cand.board_id)
        finally:
            eng.close_all()
    assert info.health.control_channel == "idle"
    assert {C.DEPLOY_PARTIAL, C.CONSOLE_DUT, C.DEBUG_DUT, C.RESET_DUT} <= info.capabilities


def test_a_busy_board_names_its_job_in_error_data():
    job = SimpleNamespace(id="j7", kind="deploy", describe=lambda: "deploy job j7")
    err = busy_error("mps3@x", job)
    assert err.data == {"job": "j7", "kind": "deploy", "board_id": "mps3@x"}
    assert err.holder == "socharnessd deploy job j7"


@pytest.fixture
def vboard(tmp_path):
    with VirtualMps3(tmp_path) as vb:
        yield vb


def test_session_view_reports_service_availability(vboard):
    eng = engine_for(vboard)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as client:
            r = client.post("/api/v1/boards", json={"target": vboard.shell_endpoint},
                            headers=headers())
            bid = r.json()["board_id"]
            body = client.get(bid_path(bid) + "/session", headers=headers()).json()
    finally:
        eng.close_all()
    assert body["services"] == {"deploy": None, "consoles": None, "debug": None,
                                "telemetry": None}
    assert body["job"] is None and body["job_kind"] is None


# -- `socharness ui --demo` (lead, at the Qt retirement) -------------------------------------


def test_ui_demo_serves_scripted_boards_from_its_own_state_dir(capsys, monkeypatch):
    import json

    import httpx

    from socharness.cli.engine import ENV_NO_DAEMON, set_engine_factory
    from socharness.daemon.state import read_info
    from tests.fakes.t13_daemon import run_cli, state_dir, stop_state_dir

    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    demo_dir = state_dir() / "demo"
    try:
        rc, out, err = run_cli(capsys, "--json", "ui", "--demo", "--no-browser")
        assert rc == 0, err
        url = json.loads(out)["url"]
        token = url.split("#token=", 1)[1]
        with httpx.Client(trust_env=False, timeout=10,
                          headers={"Authorization": f"Bearer {token}"}) as c:
            base = url.split("#")[0].rstrip("/")
            found = c.post(base + "/api/v1/probe", json={}).json()["candidates"]
            index = c.get(base + "/")
        assert len(found) == 3 and all(cand["pack"] == "mps3" for cand in found)
        assert index.status_code == 200 and "script-src 'self'" in index.headers[
            "content-security-policy"]
        # Negative twin: the real daemon's state dir holds no daemon; the demo never shares it.
        assert read_info(demo_dir) is not None and read_info(state_dir()) is None
        rc, _, err = run_cli(capsys, "daemon", "stop", "--demo")
        assert rc == 0, err
        assert read_info(demo_dir) is None
    finally:
        stop_state_dir(demo_dir)
        set_engine_factory(previous)


# -- `socharness app` (lead) ------------------------------------------------------------------


def _app_cli(capsys, monkeypatch, *argv: str):
    from socharness.cli.engine import ENV_NO_DAEMON, set_engine_factory
    from tests.fakes.t13_daemon import run_cli

    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    try:
        return run_cli(capsys, *argv)
    finally:
        set_engine_factory(previous)


def test_app_opens_the_ui_in_an_application_window(capsys, monkeypatch):
    import json

    from socharness.web import window
    from tests.fakes.t13_daemon import state_dir, stop_state_dir

    opened: list[tuple[str, Path, bool]] = []
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(window, "open_window", lambda url, prof, native=True: opened.append(
        (url, prof, native)) or window.Launched("app-mode", "/usr/bin/google-chrome", 1))
    demo_dir = state_dir() / "demo"
    try:
        rc, out, err = _app_cli(capsys, monkeypatch, "--json", "app", "--demo", "--no-native")
        assert rc == 0, err
        shown = json.loads(out)
        assert shown["window"] == "app-mode" and shown["started"] is True
        ((url, prof, native),) = opened
        assert url == shown["url"] and "#token=" in url
        assert prof == demo_dir / "app-window" and native is False
        assert "opened an app window (google-chrome)" in err
    finally:
        stop_state_dir(demo_dir)


def test_negative_twin_app_with_no_display_prints_the_url_and_opens_nothing(capsys, monkeypatch):
    import json

    from socharness.web import window
    from tests.fakes.t13_daemon import state_dir, stop_state_dir

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(window, "open_window", lambda *a, **k: pytest.fail("opened a window"))
    demo_dir = state_dir() / "demo"
    try:
        rc, out, err = _app_cli(capsys, monkeypatch, "--json", "app", "--demo")
        assert rc == 0, err
        assert json.loads(out)["window"] == "none" and "ssh -L" in err
    finally:
        stop_state_dir(demo_dir)
