"""T8: hub mode wired into the pack and the daemon (CCRs T8-1, T8-2, T8-4, T8-5).

These tests exercise the CCR hooks in files T8 does not own (``harness_manager_mps3/hub.py``,
``tunnel.py``, ``daemon/hub_api.py``, ``services/lease.py``). Until the lead applies the
CCRs they SKIP, naming the missing hook; once applied they run. Every check has a twin.
Only 127.0.0.1 is reached (the fake hub and a virtual board).
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from pathlib import Path

import pytest

from harness_manager.core.errors import UnreachableError, UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.transports.hub_rest import RestHubClient
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import tunnel as tunmod
from tests.fakes.t8_hub_rest import DEFAULT_BOARDS, FakeFpgahub, client_for, wait_until

HUB_CCR = hasattr(hubmod.HubConfig, "transport")
TUNNEL_CCR = hasattr(tunmod, "VIA_HUB")
needs_hub = pytest.mark.skipif(not HUB_CCR, reason="CCR T8-1 (hub.py: HubConfig.rest) not applied")
needs_tunnel = pytest.mark.skipif(not (HUB_CCR and TUNNEL_CCR),
                                  reason="CCR T8-5 (tunnel.py: via = \"hub\") not applied")


def _daemon_ccr() -> bool:
    from harness_manager.services.lease import LeaseService

    return hasattr(LeaseService, "on_hub_event")


needs_daemon = pytest.mark.skipif(not (HUB_CCR and _daemon_ccr()),
                                  reason="CCRs T8-2/T8-4 (hub_api.py, lease.py) not applied")


@pytest.fixture(autouse=True)
def _no_fpgahub_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)


@pytest.fixture
def hub():
    with FakeFpgahub() as h:
        yield h
    hubmod.SHARES.close_all()


def _state() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def _token_file(hub: FakeFpgahub, owner: str = "alice", role: str = "write") -> Path:
    path = _state() / f"{owner}.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hub.add_token(owner, role) + "\n")
    path.chmod(0o600)
    return path


def _boards_toml(text: str) -> None:
    _state().mkdir(parents=True, exist_ok=True)
    (_state() / "boards.toml").write_text(text)


def _cand(addr: str = "192.168.10.101:6900", via: str = "") -> Candidate:
    return Candidate(pack="mps3", board_id=f"mps3@{addr}",
                     links=(Link(LinkKind.ETHERNET, addr, "shell control channel", via=via),),
                     label="MPS3", evidence="test")


# --- T8-1: the transport comes from the hub table --------------------------------------------


@needs_hub
def test_url_builds_the_rest_client_host_the_ssh_one_and_both_prefer_rest(hub):
    tf = _token_file(hub)
    for table, want in [
        (f'{{ url = "{hub.url}", token_file = "{tf.as_posix()}" }}', "rest"),
        ('{ host = "mapstone-dev" }', "ssh"),
        (f'{{ url = "{hub.url}", host = "mapstone-dev", token_file = "{tf.as_posix()}" }}', "rest"),
    ]:
        _boards_toml(f'[boards.lab]\nmatch = ["192.168.10.101"]\nhub = {table}\n')
        adapter = hubmod.adapter_for(_cand())
        assert adapter.config.transport == want and adapter.transport == want
        assert isinstance(adapter.client, RestHubClient) == (want == "rest")
    assert adapter.client.principal() == "alice@mapstone-dev"
    assert adapter.client.config.ssh_host == "mapstone-dev"


@needs_hub
def test_a_hub_table_with_neither_url_nor_host_or_a_bad_key_is_refused(hub):
    with pytest.raises(UsageError):
        hubmod.parse_hub_table({"target": "mps3_01_pl"})
    with pytest.raises(UsageError):
        hubmod.parse_hub_table({"url": hub.url, "tokenfile": "x"})


@needs_hub
def test_a_rest_only_share_is_reached_directly_on_the_hub(hub):
    tf = _token_file(hub)
    _boards_toml(f'[boards.lab]\nmatch = ["192.168.10.101"]\n'
                 f'hub = {{ url = "{hub.url}", token_file = "{tf.as_posix()}", start_shares = true, '
                 f'shares = {{ mcc = "/dev/mps3_01_pl/tty_00" }} }}\n')
    adapter = hubmod.adapter_for(_cand())
    port = hubmod.open_hub_share(hubmod.share_url_for(adapter.config, "mcc").split("://", 1)[1])
    try:
        share = next(iter(hub.shares.values()))
        wait_until(lambda: len(share.clients) == 1)
        route = hubmod.SHARES.status(adapter.host, adapter.target)
        assert route["/dev/mps3_01_pl/tty_00"]["state"] == "up"          # no SSH tunnel
    finally:
        port.close()


@needs_hub
def test_a_share_with_an_ssh_host_still_goes_through_the_tunnel(hub, monkeypatch):
    tf = _token_file(hub)
    _boards_toml(f'[boards.lab]\nmatch = ["192.168.10.101"]\n'
                 f'hub = {{ url = "{hub.url}", host = "mapstone-dev", token_file = "{tf.as_posix()}", '
                 f'start_shares = true, shares = {{ mcc = "/dev/mps3_01_pl/tty_00" }} }}\n')
    adapter = hubmod.adapter_for(_cand())
    started = []

    class NoTunnel(tunmod.SshTunnel):
        def start(self):
            started.append(self.host)
            raise UnreachableError("tunnel not available in this test")

    monkeypatch.setattr(tunmod, "SshTunnel", NoTunnel)
    with pytest.raises(UnreachableError):
        hubmod.open_hub_share(hubmod.share_url_for(adapter.config, "mcc").split("://", 1)[1])
    assert started == ["mapstone-dev"]                              # hub.host, over ssh


# --- T8-5: via = "hub" ------------------------------------------------------------------------


def _gate(on: bool) -> dict:
    boards = {k: {r: dict(v) for r, v in m.items()} for k, m in DEFAULT_BOARDS.items()}
    boards["mps3_01"]["pl"]["gate_ethernet"] = on
    return boards


@needs_tunnel
def test_via_hub_with_direct_always_talks_to_the_board_address():
    with FakeFpgahub(boards=_gate(True)) as hub:
        tf = _token_file(hub)
        _boards_toml(f'[boards.lab]\nmatch = ["192.168.10.101"]\nvia = "hub"\n'
                     f'hub = {{ url = "{hub.url}", token_file = "{tf.as_posix()}", direct = "always" }}\n')
        cand = tunmod.with_via(_cand(), "hub")
        assert tunmod.candidate_via(cand) == "hub"
        reach = tunmod.open_reach(cand, {"push": 6910, "rbb": 6921})
        assert reach.host == "192.168.10.101" and reach.ports["control"] == 6900
        assert reach.status()["mode"] == "direct"


@needs_tunnel
def test_via_hub_with_no_direct_path_and_no_ssh_host_says_why():
    with FakeFpgahub(boards=_gate(False)) as hub:
        tf = _token_file(hub)
        _boards_toml(f'[boards.lab]\nmatch = ["192.168.10.101"]\nvia = "hub"\n'
                     f'hub = {{ url = "{hub.url}", token_file = "{tf.as_posix()}" }}\n')
        with pytest.raises(UnreachableError) as ei:
            tunmod.open_reach(tunmod.with_via(_cand(), "hub"), {"push": 6910})
        assert "route" in ei.value.message or "gate" in ei.value.message


# --- T8-2/T8-4: the daemon streams the hub's events and settles a revoke at once --------------


@needs_daemon
def test_an_open_rest_board_streams_and_a_revoke_is_lost_without_waiting_for_a_heartbeat(
        tmp_path, hub):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from harness_manager.daemon.app import create_app
    from tests.fakes.t13_daemon import TOKEN, engine_for, headers
    from tests.fakes.virtual_board import VirtualMps3

    with VirtualMps3(tmp_path) as vb:
        addr = f"127.0.0.1:{vb.shell.control_port}"
        tf = _token_file(hub, "bob")
        _boards_toml(f'[boards.lab]\nmatch = ["{addr}"]\n'
                     f'hub = {{ url = "{hub.url}", token_file = "{tf.as_posix()}" }}\n')
        eng = engine_for(vb)
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as api:
            d = api.app.state.daemon
            states: list[dict] = []
            d.bus.subscribe("lease.state", lambda ev: states.append(ev.data))
            r = api.post("/api/v1/boards", json={"target": addr}, headers=headers())
            assert r.status_code == 200, r.text
            bid = r.json()["board_id"]
            wait_until(lambda: bid in d.hub_streams and d.hub_streams[bid].state == "up")
            session = eng.session(bid)
            d.leases.acquire(session.hub, board_id=bid, ttl_s=600,
                             holder=session.hub.client.principal())
            alice = client_for(hub, hub.add_token("alice"))
            stop = threading.Event()
            threading.Thread(target=lambda: _quiet(alice, stop), daemon=True).start()
            wait_until(lambda: alice.lease_status().queue)
            t0 = time.monotonic()
            client_for(hub, hub.add_token("david", "admin")).lease_revoke("demo")
            wait_until(lambda: states and states[-1]["state"] == "lost", timeout=3)
            assert time.monotonic() - t0 < 2.0          # the stream, not a 20-minute heartbeat
            stop.set()
            api.delete(f"/api/v1/boards/{bid}", headers=headers())
            wait_until(lambda: bid not in d.hub_streams)
        eng.close_all()


def _quiet(client, stop):
    def sleep(s):
        if stop.wait(s):
            raise RuntimeError("stopped")

    try:
        client.lease_acquire("q", ttl=600, sleep=sleep)
    except Exception:  # noqa: BLE001
        pass


@needs_daemon
def test_an_ssh_board_starts_no_event_stream(tmp_path, monkeypatch):
    """Twin: the SSH lab rig (lane L1) opens as before, with no hub stream."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from harness_manager.core.services import EngineConfig
    from harness_manager.daemon.app import create_app
    from harness_manager.engine import Engine
    from tests.fakes.l1_rig import BOARD_IP, lab
    from tests.fakes.t13_daemon import TOKEN, headers
    from tests.fakes.virtual_board import VirtualMps3

    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=_state()):
        eng = Engine(EngineConfig(state_dir=_state()))
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as api:
            r = api.post("/api/v1/boards", json={"target": BOARD_IP}, headers=headers())
            assert r.status_code == 200, r.text
            assert api.app.state.daemon.hub_streams == {}
        eng.close_all()
