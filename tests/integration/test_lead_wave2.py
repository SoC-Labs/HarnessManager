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
        assert info.unavailable[cap].startswith("the harness is rescue"), info.unavailable[cap]


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
