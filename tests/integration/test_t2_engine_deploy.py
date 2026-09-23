"""T2 through T1's real Engine and ContentStore (main 8bd9e46): the S2 shape, and deploy-from-store.

The engine builds ``DeployService(engine)`` lazily; this proves that wiring,
events on ``engine.bus``, and overlays found through ``engine.store``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_manager.core.events import EventBus
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.deploy import DeployService
from harness_manager_mps3.overlays import OVERLAY_DIRS_ENV, import_overlay
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t2_overlays import (
    FIELDED_USERCODE,
    PARTIAL,
    make_overlay,
    point_pushes_at,
    ref_named,
    use_overlay_dirs,
)
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture
def seen() -> list:
    return []


def engine_for(vb: VirtualMps3, tmp_path: Path, seen: list) -> Engine:
    bus = EventBus()
    bus.subscribe("deploy.*", seen.append)
    return Engine(EngineConfig(state_dir=tmp_path / "state"),
                  packs={"mps3": Mps3Pack(console_ports=vb.console_ports)}, bus=bus)


def open_board(eng: Engine, vb: VirtualMps3, monkeypatch):
    point_pushes_at(monkeypatch, vb)
    cand = eng.candidate_for(vb.shell_endpoint)
    eng.open(cand, note="t2 deploy")
    return cand.board_id, eng.session(cand.board_id)


def test_engine_deploys_an_overlay_and_identity_changes(vboard, tmp_path, monkeypatch, seen):
    use_overlay_dirs(monkeypatch, make_overlay(tmp_path / "ov", "synth").parent)
    eng = engine_for(vboard, tmp_path, seen)
    assert isinstance(eng.deploy, DeployService)
    board_id, session = open_board(eng, vboard, monkeypatch)
    try:
        synth = ref_named(eng.deploy.overlays(session), "synth")
        result = eng.deploy.deploy(session, synth)
        assert result.verified and result.transport == "tcp+windowed"
        assert session.identity().rm_id == "0x01007a57"
        assert seen[0].topic == "deploy.started" and seen[-1].topic == "deploy.done"
        assert {e.board_id for e in seen} == {board_id}
    finally:
        eng.close_all()


def test_overlay_imported_into_the_engine_store_is_deployable(vboard, tmp_path, monkeypatch, seen):
    monkeypatch.delenv(OVERLAY_DIRS_ENV, raising=False)       # the store is the ONLY source
    eng = engine_for(vboard, tmp_path, seen)
    sha = import_overlay(eng.store, make_overlay(tmp_path / "src", "synth",
                                                 static_usercode=FIELDED_USERCODE))
    assert eng.store.verify(sha)
    board_id, session = open_board(eng, vboard, monkeypatch)
    try:
        (synth,) = eng.deploy.overlays(session)
        assert synth.source == f"store:{sha}"
        assert eng.deploy.deploy(session, synth).verified
        assert session.identity().rm_id == "0x01007a57"
        assert [e.info.len_words * 4 for e in vboard.shell.accepted_pushes] == [128, 384]
    finally:
        eng.close_all()


def test_a_bare_partial_stored_as_overlay_is_rejected_not_deployed(vboard, tmp_path, monkeypatch,
                                                                   seen):
    """T1's store docstring shows a PARTIAL put as kind="overlay". Such a record has no
    manifest and no clearing, so it must never become a deployable overlay."""
    monkeypatch.delenv(OVERLAY_DIRS_ENV, raising=False)
    eng = engine_for(vboard, tmp_path, seen)
    bin_path = tmp_path / "nanosoc.bin"
    bin_path.write_bytes(PARTIAL)
    eng.store.put_file(bin_path, kind="overlay",
                       meta={"name": "nanosoc", "rm_id": "0x01000001", "static_id": "0x3f1a560f"})
    board_id, session = open_board(eng, vboard, monkeypatch)
    try:
        assert eng.deploy.overlays(session) == []
        assert "lacks meta" in next(iter(session.deploy.catalogue.rejects.values()))
        assert vboard.shell.push_events == []
    finally:
        eng.close_all()
