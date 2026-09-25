"""SET-HUBS end to end: a board that names its hub opens, leases and reaches its MCC share
through it, over SSH (L1's rig: fake ssh launcher, fake hub runner, a VirtualMps3) and over
REST (T8's fake fpgahub). Each check has its twin: the same board with an inline table.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness_manager.core.errors import UnreachableError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.lease import LeaseService
from harness_manager.settings import hubs as H
from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_rig import BOARD_IP, HUB, LAB_TOML, MCC_TTY, TARGET, lab
from tests.fakes.t8_hub_rest import FakeFpgahub
from tests.fakes.virtual_board import VirtualMps3

NAMED_TOML = f"""\
# the lab board, after "Make this a hub"
[boards.lab]
match = ["{BOARD_IP}"]
via = "hub"
hub = {{ use = "lab", target = "{TARGET}", shares = {{ mcc = "{MCC_TTY}" }} }}
"""
LAB_HUB = f'[hubs.lab]\nhost = "{HUB}"\njump = "bastion.invalid"\nholder = "me@desk"\n' \
          'lease_ttl = "15m"\n'


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "POLICY_PATH", tmp_path / "no-policy.toml")
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def engine():
    eng = Engine(EngineConfig(state_dir=state_dir()))
    yield eng
    eng.close_all()


def _jump_aware(monkeypatch, rig):
    """L1's rig takes (host, group); a named hub with a jump passes jump= too."""
    def factory(host, group, jump=""):
        rig.runners.append((host, group, jump))
        return rig.hub
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", factory)


def test_a_board_naming_an_ssh_hub_opens_through_it_with_the_jump_and_its_lease_settings(
        tmp_path, monkeypatch, engine):
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "settings.toml").write_text(LAB_HUB)
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=NAMED_TOML) as rig:
        _jump_aware(monkeypatch, rig)
        cand = engine.candidate_for(BOARD_IP)
        mcc = next(lk for lk in cand.links if lk.kind == LinkKind.USB_SERIAL)
        assert mcc.via == "hub" and mcc.address == f"hub://{HUB}/{TARGET}{MCC_TTY}"
        session = engine.open(cand, note="set-hubs test")
        assert engine.info(cand.board_id).identity.shell_id == "0x3f1a560f"   # through it
        argv = rig.ssh.launches[0]
        assert argv[argv.index("-J") + 1] == "bastion.invalid" and argv[-1] == HUB
        out = LeaseService(tmp_path / "leases").acquire(session.hub, board_id=cand.board_id,
                                                        heartbeat=False)
        assert out["lease"]["holder"] == "me@desk"
        acq = next(c for c in rig.hub.calls if c[:3] == ["fpgahub", "lease", "acquire"])
        assert acq[acq.index("--ttl") + 1] == "900"
        assert ("mapstone-dev.ecs.soton.ac.uk", "fpga", "bastion.invalid") in rig.runners
        engine.close(cand.board_id)
        assert not rig.ssh.live()


def test_negative_twin_the_same_board_inline_has_no_jump_and_the_default_lease(
        tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=LAB_TOML) as rig:
        cand = engine.candidate_for(BOARD_IP)
        session = engine.open(cand, note="set-hubs twin")
        assert "-J" not in rig.ssh.launches[0]
        LeaseService(tmp_path / "leases").acquire(session.hub, board_id=cand.board_id,
                                                  heartbeat=False)
        acq = next(c for c in rig.hub.calls if c[:3] == ["fpgahub", "lease", "acquire"])
        assert acq[acq.index("--ttl") + 1] == "3600"
        engine.close(cand.board_id)


def _cand() -> Candidate:
    return Candidate(pack="mps3", board_id=f"mps3@{BOARD_IP}:6900",
                     links=(Link(LinkKind.ETHERNET, f"{BOARD_IP}:6900", "shell control channel"),),
                     label="MPS3", evidence="test")


def test_a_board_naming_a_rest_hub_leases_with_the_stored_token_and_the_hubs_ttl(tmp_path):
    with FakeFpgahub() as hub:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "settings.toml").write_text(
            f'[hubs.remote]\nurl = "{hub.url}"\nlease_ttl = "20m"\n')
        (state_dir() / "boards.toml").write_text(
            f'[boards.lab]\nmatch = ["{BOARD_IP}"]\nhub = {{ use = "remote", target = "{TARGET}" }}\n')
        H.set_hub_token("remote", H.load_resolver(), value=hub.add_token("alice", "write"))
        adapter = hubmod.adapter_for(_cand())
        assert adapter.transport == "rest" and adapter.client.principal() == "alice@mapstone-dev"
        out = LeaseService(tmp_path / "leases").acquire(adapter, board_id="b", heartbeat=False)
        assert out["lease"]["holder"] == "alice@mapstone-dev"
        assert hub.leases[TARGET].ttl == 1200
        LeaseService(tmp_path / "leases").release(adapter, board_id="b")


def test_negative_twin_a_rest_hub_without_its_token_is_401_naming_the_hub_token_verb(tmp_path):
    with FakeFpgahub() as hub:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "settings.toml").write_text(f'[hubs.remote]\nurl = "{hub.url}"\n')
        (state_dir() / "boards.toml").write_text(
            f'[boards.lab]\nmatch = ["{BOARD_IP}"]\nhub = {{ use = "remote", target = "{TARGET}" }}\n')
        adapter = hubmod.adapter_for(_cand())
        with pytest.raises(UnreachableError) as ei:
            adapter.client.lease_show()
        assert "401" in ei.value.message and "hub token remote" in ei.value.hint
        assert not hub.leases
