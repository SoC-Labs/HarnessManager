"""FIX-PACK-2 item 4: `xvc status` states the XVC capability from the board's CURRENT identity.

On the lab board, `xvc status` said "cannot: needs harness firmware with 'xvc_dbgbr' (v0.11 or
later)" while the board reported ``xvc_dbgbr``. The source: the MPS3 XVC adapter judged from
the identity it was built with (the probe's; one found by UDP identify, or while another
client held 6900, carries NO features), and nothing but the XVC service ever told it better,
which ``status`` skipped whenever the probe carried any identity. Now the engine's ``info``
notes every identity it reads on the adapter (as it does the display's and the panel's), an
identity without features never wipes the features known, a probe identity without features
is not "known", and a failed read falls back to the engine's last read before the probe's.

Board-free: a ``VirtualMps3`` through the real Engine and the real daemon app. Each check has
a negative twin.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.core import capabilities as C
from harness_manager.core.errors import HeldError
from harness_manager.core.model import BoardIdentity
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3 import xvc as MX
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.l4_service import H
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for
from tests.fakes.virtual_board import FIELDED_3F1A560F, FIELDED_ILA_V011, VirtualMps3

#: What a board found by UDP identify (or while 6900 was held) carries: no features.
BARE = BoardIdentity(board_type="mps3", shell_id="0x1", harness_impl="bare-metal")


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def engine(vb: VirtualMps3) -> Engine:
    return Engine(EngineConfig(state_dir=state_dir()),
                  packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})


def identify_candidate(vb: VirtualMps3):
    return replace(vb.candidate(), identity=BARE)


# --- the engine: status after info --------------------------------------------------------------


def test_status_after_info_says_what_the_board_reports_not_the_probe(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        eng = engine(vb)
        try:
            cand = identify_candidate(vb)
            session = eng.open(cand)
            info = eng.info(cand.board_id)
            assert C.DEBUG_FABRIC in info.capabilities
            st = eng.xvc.status(session)                   # no refresh: the daemon's first look
            assert st.reason == "" and st.state == "down"
            assert "xvc_dbgbr" in eng.last_identity(cand.board_id).features
        finally:
            eng.close_all()


def test_negative_twin_an_image_without_xvc_dbgbr_still_says_so(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_3F1A560F) as vb:
        eng = engine(vb)
        try:
            cand = identify_candidate(vb)
            session = eng.open(cand)
            assert C.DEBUG_FABRIC not in eng.info(cand.board_id).capabilities
            assert eng.xvc.status(session).reason == MX.NO_DBGBR_REASON
        finally:
            eng.close_all()


# --- the adapter: an identity without features never wipes what is known -------------------------


def test_a_featureless_identity_keeps_the_features_known_and_takes_the_rest():
    session = type("S", (), {})()
    session.candidate = type("Cand", (), {"identity": None, "links": (), "board_id": "b"})()
    x = MX.Mps3Xvc(session)
    full = BoardIdentity(board_type="mps3", rm_id="0x0100000a", harness_impl="bare-metal",
                         features=("xvc_dbgbr", "stats"))
    x.xvc_note_identity(full)
    assert MX.DBGBR_FEATURE in x._features()
    x.xvc_note_identity(replace(BARE, rm_id="0x01000002"))    # a held board's identify answer
    assert MX.DBGBR_FEATURE in x._features()                   # not known is not never
    assert x._ident.rm_id == "0x01000002"                      # the rest is taken


def test_negative_twin_a_real_image_without_xvc_dbgbr_does_replace_the_features():
    session = type("S", (), {})()
    session.candidate = type("Cand", (), {"identity": None, "links": (), "board_id": "b"})()
    x = MX.Mps3Xvc(session)
    x.xvc_note_identity(BoardIdentity(board_type="mps3", features=("xvc_dbgbr",)))
    x.xvc_note_identity(BoardIdentity(board_type="mps3", features=("clcd", "stats")))
    assert MX.DBGBR_FEATURE not in x._features()


# --- the service: what "known" means, and a read that fails ---------------------------------------


def test_a_probe_identity_without_features_is_not_known_one_with_features_is(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        eng = engine(vb)
        try:
            bare = eng.open(identify_candidate(vb))
            assert eng.xvc.identity_known(bare) is False           # the first look reads it
            eng.info(bare.candidate.board_id)
            assert eng.xvc.identity_known(bare) is True            # twin: info read it
        finally:
            eng.close_all()


def test_a_held_board_falls_back_to_the_engines_last_read_not_the_probe(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        eng = engine(vb)
        try:
            cand = identify_candidate(vb)
            session = eng.open(cand)
            eng.info(cand.board_id)

            def held():
                raise HeldError("the control channel is held by another client")

            monkeypatch.setattr(session, "identity", held)
            assert eng.xvc.status(session, refresh=True).reason == ""
        finally:
            eng.close_all()


def test_negative_twin_with_no_read_at_all_a_held_board_uses_the_probe(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        eng = engine(vb)
        try:
            session = eng.open(identify_candidate(vb))

            def held():
                raise HeldError("the control channel is held by another client")

            monkeypatch.setattr(session, "identity", held)
            # nothing ever read this board's features: the probe's (none) is all there is
            assert eng.xvc.status(session, refresh=True).reason == MX.NO_DBGBR_REASON
        finally:
            eng.close_all()


# --- the API (what the CLI's daemon path reads) ----------------------------------------------------


@pytest.fixture
def api(tmp_path):
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011) as vb:
        eng = engine_for(vb)
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
                yield c, eng, vb
        finally:
            eng.close_all()


def open_identify_found(client: TestClient, vb: VirtualMps3) -> str:
    """POST /boards with the candidate a UDP identify probe gives (an identity, no features)."""
    import dataclasses
    import json

    cand = json.loads(json.dumps(dataclasses.asdict(identify_candidate(vb)), default=str))
    for link in cand["links"]:
        link["kind"] = link["kind"].split(".")[-1].lower() if "." in link["kind"] \
            else link["kind"]
    r = client.post("/api/v1/boards", json={"candidate": cand, "note": "fix-pack-2"},
                    headers=H)
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def test_the_api_states_xvc_from_the_current_identity(api):
    client, eng, vb = api
    bid = open_identify_found(client, vb)
    info = client.get(bid_path(bid), headers=H).json()
    assert C.DEBUG_FABRIC in info["capabilities"]
    st = client.get(f"{bid_path(bid)}/xvc", headers=H).json()
    assert st["ok"] and st["state"] == "down" and st["reason"] == ""


def test_negative_twin_the_api_before_any_info_reads_the_identity_itself(api):
    client, eng, vb = api
    bid = open_identify_found(client, vb)
    st = client.get(f"{bid_path(bid)}/xvc", headers=H).json()     # no info first
    assert st["ok"] and st["reason"] == ""                         # read, not the probe's
