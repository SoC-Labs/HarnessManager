"""CORE-CCR: the front panel as a first-class adapter (CCR PANEL-5): ``BoardSession.panel``,
``"panel"`` in the daemon's session adapters, the ``RemoteSession`` proxy, and the CLI going
through ``session.panel`` both ways. The shell's preamble hook (CCR PANEL-3) is tested in
``test_coreccr_preamble.py``.

Real sockets throughout: the Engine and MPS3 pack over ``PanelVirtualMps3`` (the Linux
harness with ``hello``/``panel``/``locate``, and the fielded v0.11 bare metal without), and,
for the proxy, a real harness-manager-daemon (``LiveDaemon``) read by a ``RemoteEngine``.
Every check has a negative twin.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.client import remote as remote_mod
from harness_manager.client.remote import RemoteEngine
from harness_manager.core import capabilities as C
from harness_manager.core.errors import UnavailableError
from harness_manager.core.model import BoardIdentity, Candidate, Health
from harness_manager.core.pack import BoardPack, BoardSession, ProbeHints
from harness_manager.core.panel import (
    Hello,
    HelloLease,
    PanelAdapter,
    PanelFrame,
    PanelState,
    PanelSupport,
)
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.presence import NOT_BEATING
from harness_manager_mps3.capabilities import NEEDS_LOCATE
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.clcd_panel_shell import LINUX_PANEL, V011_BARE_METAL, PanelVirtualMps3
from tests.fakes.t13_daemon import TOKEN, LiveDaemon, bid_path, engine_for, state_dir

HELLO = Hello(sid="c0ffee01", who="core@ccr", app="hm/0.1.0", name="mps3-01", role="holder",
              lease=HelloLease(by="core@ccr", left=600, q=0))
PANEL_READS = {"panel", "display", "stats"}


def ops(vb: PanelVirtualMps3, since: int = 0) -> list[str]:
    return [r.get("op") for r in vb.shell.requests[since:]]


# --- a pack without a panel -------------------------------------------------------------------


class _NoPanelSession(BoardSession):
    def __init__(self, candidate: Candidate) -> None:
        self.candidate = candidate

    def identity(self) -> BoardIdentity:
        return BoardIdentity(board_type="nopanel")

    def health(self) -> Health:
        return Health(reachable=True, control_channel="idle")


class _NoPanelPack(BoardPack):
    name = "nopanel"
    title = "A board without a front panel"

    def capability_specs(self):
        return ()

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        return []

    def open(self, candidate: Candidate) -> BoardSession:
        return _NoPanelSession(candidate)


NOPANEL = Candidate(pack="nopanel", board_id="nopanel@1", links=())


# --- rigs ---------------------------------------------------------------------------------------


class Local:
    """The in-process Engine and MPS3 pack over a panel board."""

    def __init__(self, vb: PanelVirtualMps3, tmp_path: Path) -> None:
        self.vb = vb
        self.engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                             packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        self.session = self.engine.open(vb.candidate(), note="core-ccr")


@contextmanager
def local(tmp_path: Path, profile) -> Iterator[Local]:
    with PanelVirtualMps3(tmp_path, profile) as vb:
        rig = Local(vb, tmp_path)
        try:
            yield rig
        finally:
            rig.engine.close_all()


@contextmanager
def over_daemon(engine: Any, candidate: Candidate) -> Iterator[tuple[LiveDaemon, BoardSession]]:
    """A real daemon for ``engine``, the board opened through a ``RemoteEngine``. The beat is
    stopped so the only requests on the wire are the ones a test makes."""
    with LiveDaemon(engine) as d:
        presence = getattr(d.app.state.daemon, "presence", None)
        if presence is not None:
            presence._stop.set()
        remote = RemoteEngine(d.base_url, TOKEN, state_dir=state_dir())
        session = remote.open(candidate)
        try:
            yield d, session
        finally:
            remote.close(candidate.board_id)
    engine.close_all()


# --- PANEL-5: session.panel is a core adapter ---------------------------------------------------


def test_session_panel_is_a_core_adapter_on_mps3(tmp_path):
    assert "panel" in vars(BoardSession) and BoardSession.panel is None
    with local(tmp_path, LINUX_PANEL) as rig:
        assert isinstance(rig.session.panel, PanelAdapter)
        assert rig.session.panel.support().source == "panel"
    with PanelVirtualMps3(tmp_path / "d", LINUX_PANEL) as vb:
        with over_daemon(engine_for(vb), vb.candidate()) as (d, rs):
            view = d.client().get(f"/api/v1/boards/{rs.candidate.board_id}/session").json()
            assert view["adapters"]["panel"] is True
            assert isinstance(rs.panel, remote_mod._Panel)


def test_twin_a_pack_without_a_panel_has_none_locally_and_over_the_daemon(tmp_path):
    engine = Engine(EngineConfig(state_dir=state_dir()), packs={"nopanel": _NoPanelPack()})
    session = engine.open(NOPANEL)
    try:
        assert session.panel is None, "the attribute exists (no getattr default) and is None"
    finally:
        engine.close_all()
    engine = Engine(EngineConfig(state_dir=state_dir()), packs={"nopanel": _NoPanelPack()})
    with over_daemon(engine, NOPANEL) as (d, rs):
        view = d.client().get("/api/v1/boards/nopanel%401/session").json()
        assert view["adapters"]["panel"] is False
        assert rs.panel is None


# --- PANEL-5: the remote proxy -----------------------------------------------------------------


def test_the_remote_proxy_round_trips_state_frame_and_identify(tmp_path):
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        engine = engine_for(vb)
        with over_daemon(engine, vb.candidate()) as (d, rs):
            bid = rs.candidate.board_id
            real = engine.session(bid).panel
            assert rs.panel.support() == real.support()
            vb.shell.tap("request")
            state = rs.panel.state()
            assert isinstance(state, PanelState) and state.source == "panel"
            assert state.page == "status" and [e.on for e in state.events] == ["request"]
            frame = rs.panel.frame()
            assert isinstance(frame, PanelFrame) and frame.rows == real.frame().rows
            until = rs.panel.locate(5, "core@ccr")
            assert vb.shell.locates[-1]["s"] == 5 and until > time.time()
            assert rs.panel.identify_until() == pytest.approx(until, abs=1.0)
            assert rs.panel.presence()["sid"] == d.app.state.daemon.presence.presence(bid)["sid"]


def test_twin_the_proxy_never_sends_a_hello_the_daemon_owns_presence(tmp_path):
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        with over_daemon(engine_for(vb), vb.candidate()) as (_d, rs):
            with pytest.raises(UnavailableError) as exc:
                rs.panel.hello(HELLO)
            assert exc.value.capability == C.PRESENCE and "daemon" in exc.value.reason
            assert not hasattr(rs.panel, "offer") and vb.shell.hellos == []


def test_support_alone_never_reads_the_panel(tmp_path):
    with PanelVirtualMps3(tmp_path, V011_BARE_METAL) as vb:
        with over_daemon(engine_for(vb), vb.candidate()) as (_d, rs):
            before = len(vb.shell.requests)
            support = rs.panel.support()
            assert isinstance(support, PanelSupport) and support.source == "rebuilt"
            assert support.impl == "bare-metal"          # PANEL-TRUTH: through the daemon too
            assert not PANEL_READS & set(ops(vb, before)), "?state=0 leaves the panel unread"
            with pytest.raises(UnavailableError) as exc:        # bare metal: no Identify
                rs.panel.locate(3, "core@ccr")
            assert exc.value.reason == NEEDS_LOCATE and vb.shell.locates == []


def test_twin_state_does_read_the_panel(tmp_path):
    with PanelVirtualMps3(tmp_path, V011_BARE_METAL) as vb:
        with over_daemon(engine_for(vb), vb.candidate()) as (_d, rs):
            before = len(vb.shell.requests)
            state = rs.panel.state()
            assert state.source == "rebuilt" and state.owner == "harness"
            assert "display" in ops(vb, before)


# --- PANEL-5: the CLI goes through session.panel ----------------------------------------------


class _Spy:
    def __init__(self, monkeypatch) -> None:
        self.calls: list[str] = []
        for name in ("support", "state", "frame", "locate", "presence"):
            original = getattr(remote_mod._Panel, name)

            def wrapped(self_, *a, _n=name, _o=original, **kw):
                self.calls.append(_n)
                return _o(self_, *a, **kw)

            monkeypatch.setattr(remote_mod._Panel, name, wrapped)


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    rc = main(list(argv))
    out, _err = capsys.readouterr()
    return rc, (json.loads(out) if out.strip() else {})


def test_the_cli_show_and_identify_go_through_the_proxy(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
    spy = _Spy(monkeypatch)
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        engine = engine_for(vb)
        with LiveDaemon(engine) as d:
            d.app.state.daemon.presence._stop.set()
            with d.client() as http:        # the UI has it open: the CLI shares the session
                opened = http.post("/api/v1/boards", json={"target": vb.shell_endpoint,
                                                           "note": "ui"})
                assert opened.status_code == 200, opened.text
                bid = opened.json()["board_id"]
                rc, body = _cli(capsys, "--json", "panel", "show", vb.shell_endpoint)
                assert rc == 0 and {"support", "state", "presence"} <= set(spy.calls)
                api = http.get(f"{bid_path(bid)}/panel").json()
            assert body["panel"]["source"] == "panel" and body["support"] == api["support"]
            assert body["presence"]["sid"] == api["presence"]["sid"] != ""
            assert body["presence"]["reason"] != NOT_BEATING
            rc, body = _cli(capsys, "--json", "identify", vb.shell_endpoint, "--seconds", "4")
            assert rc == 0 and body["seconds"] == 4 and "locate" in spy.calls
            assert vb.shell.locates[-1]["s"] == 4
        engine.close_all()


def test_twin_in_process_the_cli_uses_the_packs_adapter(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    spy = _Spy(monkeypatch)
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        previous = set_engine_factory(lambda _args: engine_for(vb))
        try:
            rc, body = _cli(capsys, "--json", "panel", "show", vb.shell_endpoint)
            assert rc == 0 and body["panel"]["source"] == "panel"
            assert body["presence"] == {"active": False, "reason": NOT_BEATING}
            rc, body = _cli(capsys, "--json", "identify", vb.shell_endpoint, "--seconds", "2")
            assert rc == 0 and vb.shell.locates[-1]["s"] == 2
        finally:
            set_engine_factory(previous)
    assert spy.calls == [], "no daemon, no proxy"
