"""FIX-PACK-6 item 3: never trust a reported rm_id after a cold boot.

Silicon, H1 r5-r8 (board 1, Linux v2.0.0, 1 Oct): after an MCC REBOOT with nanosoc kept on the
card, harnessd reported ``rm_id 0x01000001`` (nanosoc) while the greybox was resident; ``debug
detect`` found nothing on the JTAG chain (all zeroes). Linux fixes the cause in 1e50499; Harness
Manager now makes one non-intrusive IDCODE read after ``mcc reboot`` and ``power cycle`` on a
Linux board whose reported design has a debug port, and says verified / UNVERIFIED / not
cross-checked, in the result, the JSON, the ``design.check`` event (the app's Activity row) and
the service's ``GET /boards/{bid}``. It never fails the reboot and never changes the board.

Three levels, each with its negative twins:

- the CLI over the T5 fake engine (``mcc reboot``, ``power cycle``: the lines and the JSON);
- the real MPS3 pack and ``DebugService`` over a Linux FakeShell, with stub_openocd and the fake
  JTAG server (``FakeJtagServer``) standing in for 6921: the IDCODE read itself;
- the service: the reboot job's result, the event, ``GET /boards/{bid}`` and a deploy clearing it.
"""

from __future__ import annotations

import io
import json
import sys
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode, NothingOnTargetError, UnavailableError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import BoardIdentity
from harness_manager.services import design_check
from harness_manager.services.debug import DebugService
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.lxslots_board import slot_board
from tests.fakes.t4_debug_rig import use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.fakes.t5_fake_engine import FakeEngine, FakeState

NANOSOC = 0x01000001
H1_WORDS = ("the board reports nanosoc (0x01000001) but no debug port answers: greybox is "
            "probably resident (known issue, Linux v2.0.0)")


# --- 1. the CLI ---------------------------------------------------------------------------------


class FakeDap:
    """The pack's debug adapter: nanosoc has a DAP, greybox has none (as the MPS3 pack)."""

    def __init__(self, st: FakeState) -> None:
        self.st = st

    def openocd_config(self) -> tuple[str, ...]:
        if self.st.identity.rm_id in ("0x00000000", ""):
            raise NothingOnTargetError("the loaded design (greybox) has no debug port")
        return ("nanosoc_mps3_jtag.cfg",)


def linux_nanosoc(**over: Any) -> BoardIdentity:
    kw = dict(board_type="mps3", shell_id="0x44ee76d5", rm_id="0x01000001", rm_name="nanosoc",
              harness_version="1.0.0", harness_impl="linux", features=("usd", "slot"))
    kw.update(over)
    return BoardIdentity(**kw)


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeEngine]:
    st = FakeState(identity=linux_nanosoc(), unavailable={})
    st.capabilities = frozenset(st.capabilities | {"reboot_board", "power_cycle"})
    eng = FakeEngine(st)
    opened = eng.open

    def open_with_dap(cand, *, note: str = ""):
        s = opened(cand, note=note)
        s.debug = FakeDap(st)
        return s

    eng.open = open_with_dap                       # type: ignore[method-assign]
    checks: list[Event] = []
    eng.bus.subscribe(design_check.TOPIC, checks.append)
    eng.checks = checks                            # type: ignore[attr-defined]
    previous = set_engine_factory(lambda args: eng)
    try:
        yield eng
    finally:
        set_engine_factory(previous)


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        rc = main(list(argv))
    finally:
        sys.stdin = old
    out, err = capsys.readouterr()
    return rc, out, err


def test_mcc_reboot_says_unverified_when_no_debug_port_answers_as_in_h1(fake, capsys):
    fake.st.idcode = ""                            # FakeDebug: nothing on the chain
    rc, out, err = cli(capsys, "mcc", "192.168.10.101", "reboot", "--yes")
    assert rc == ExitCode.OK, err                  # never fails the reboot
    assert f"design     UNVERIFIED: {H1_WORDS}" in out
    rc, out, _ = cli(capsys, "--json", "mcc", "192.168.10.101", "reboot", "--yes")
    doc = json.loads(out)
    assert doc["design_check"]["state"] == "unverified"
    assert doc["design_check"]["rm_id"] == "0x01000001" and "evidence" in doc
    assert "design_check" not in (doc["evidence"] or {})
    assert [e.data["state"] for e in fake.checks] == ["unverified", "unverified"]
    # it changed nothing on the board: one IDCODE read per reboot, no deploy, no reset
    assert fake.calls.count("debug.detect") == 2
    assert not [c for c in fake.calls if c.startswith(("deploy.", "resets.", "debug.up"))]


def test_twin_the_dap_answering_says_verified(fake, capsys):
    rc, out, _ = cli(capsys, "mcc", "192.168.10.101", "reboot", "--yes")
    assert rc == ExitCode.OK
    assert ("design     verified: the board reports nanosoc (0x01000001) and its debug port "
            "answers (IDCODE 0x6ba00477)") in out


def test_twin_no_openocd_is_said_and_never_fails_the_reboot(fake, capsys):
    fake.st.raises["debug.detect"] = UnavailableError(
        "debug_dut", "OpenOCD not found — install it or set HARNESS_MANAGER_OPENOCD")
    rc, out, _ = cli(capsys, "--json", "mcc", "192.168.10.101", "reboot", "--yes")
    assert rc == ExitCode.OK
    check = json.loads(out)["design_check"]
    assert check["state"] == "skipped" and "no OpenOCD here" in check["text"]
    rc, out, _ = cli(capsys, "mcc", "192.168.10.101", "reboot", "--yes")
    assert "not cross-checked: not cross-checked" not in out
    assert "design     not cross-checked: no OpenOCD here (OpenOCD not found" in out
    assert out.rstrip().endswith("the board reports nanosoc (0x01000001)")


def test_twin_greybox_or_bare_metal_reads_nothing(fake, capsys):
    fake.st.identity = linux_nanosoc(rm_id="0x00000000", rm_name="greybox")
    rc, out, _ = cli(capsys, "--json", "mcc", "192.168.10.101", "reboot", "--yes")
    assert rc == ExitCode.OK and json.loads(out)["design_check"] is None
    fake.st.identity = linux_nanosoc(harness_impl="bare-metal")
    rc, out, _ = cli(capsys, "mcc", "192.168.10.101", "reboot", "--yes")
    assert rc == ExitCode.OK and "design " not in out
    assert "debug.detect" not in fake.calls and fake.checks == []


def test_a_power_cycle_checks_too_and_says_so_while_the_harness_boots():
    """``after_cold_boot`` after a power cycle: the harness answers -> the same check; it does
    not answer yet (a Linux cold boot is ~3 min) -> skipped, naming the follow-up, but only for
    a board the engine last saw running Linux."""
    from harness_manager.core.errors import UnreachableError

    st = FakeState(identity=linux_nanosoc())
    st.idcode = ""
    eng = FakeEngine(st)
    s = SimpleNamespace(candidate=SimpleNamespace(board_id="mps3@b:6900"),
                        identity=lambda: st.identity, debug=FakeDap(st))
    rec = design_check.after_cold_boot(eng, s, after=design_check.AFTER_POWER_CYCLE)
    assert rec["state"] == "unverified" and rec["after"] == "power cycle"

    def down() -> BoardIdentity:
        raise UnreachableError("the shell did not answer")

    s.identity = down
    eng.last_identity = lambda bid: linux_nanosoc()          # type: ignore[attr-defined]
    rec = design_check.after_cold_boot(eng, s, after=design_check.AFTER_POWER_CYCLE)
    assert rec["state"] == "skipped" and "debug detect TARGET" in rec["text"]
    # Twins: an MCC reboot whose harness does not answer, or a board not last seen on Linux
    assert design_check.after_cold_boot(eng, s, after=design_check.AFTER_MCC_REBOOT) is None
    eng.last_identity = lambda bid: linux_nanosoc(harness_impl="bare-metal")  # type: ignore[attr-defined]
    assert design_check.after_cold_boot(eng, s, after=design_check.AFTER_POWER_CYCLE) is None


# --- 2. the real pack, DebugService and stub OpenOCD over a Linux FakeShell -------------------------


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return use_stub(monkeypatch, tmp_path)


@pytest.fixture
def jtag() -> Iterator[FakeJtagServer]:
    with FakeJtagServer() as srv:
        yield srv


def linux_session(jtag: FakeJtagServer, rm_id: int):
    fake = slot_board(boot_rm_id=rm_id)
    pack = Mps3Pack(console_ports=fake.console_ports, rbb_port=jtag.port,
                    push_port=fake.raw_tcp_port, tftp_port=fake.tftp_port)
    return fake, pack.open(pack.candidate_for_host(f"{fake.host}:{fake.control_port}"))


def test_the_real_read_says_unverified_when_the_chain_is_all_zeroes(rig, jtag, monkeypatch):
    monkeypatch.setenv("STUB_OPENOCD_IDCODE", "none")          # greybox resident: all zeroes
    fake, session = linux_session(jtag, NANOSOC)
    bus, seen = EventBus(), []
    bus.subscribe("design.check", seen.append)
    svc = DebugService(bus)
    try:
        rec = design_check.after_cold_boot(SimpleNamespace(debug=svc), session, bus=bus)
        assert rec["state"] == "unverified" and rec["text"] == H1_WORDS, rec
        assert "all zeroes" in rec["reason"]
        assert seen and seen[0].data["state"] == "unverified"
        assert len(rig.runs()) == 1 and "scan_chain" in rig.runs()[0]   # one read, no session
        assert fake.current_rm_id == NANOSOC                             # nothing changed
    finally:
        svc.close()
        fake.stop()


def test_twin_the_real_read_says_verified_when_the_dap_answers(rig, jtag):
    fake, session = linux_session(jtag, NANOSOC)
    svc = DebugService(None)
    try:
        rec = design_check.check_design(SimpleNamespace(debug=svc), session)
        assert rec["state"] == "verified" and rec["idcode"] == "0x6ba00477", rec
        assert jtag.accepted == 1                                        # it dialled 6921
    finally:
        svc.close()
        fake.stop()


def test_twin_greybox_reported_runs_no_openocd(rig, jtag):
    fake, session = linux_session(jtag, 0)
    svc = DebugService(None)
    try:
        assert design_check.check_design(SimpleNamespace(debug=svc), session) is None
        assert rig.runs() == [] and jtag.accepted == 0
    finally:
        svc.close()
        fake.stop()


def test_twin_no_openocd_on_this_host_is_skipped_and_said(rig, jtag, monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_MANAGER_OPENOCD", str(tmp_path / "no-such-openocd"))
    fake, session = linux_session(jtag, NANOSOC)
    svc = DebugService(None)
    try:
        rec = design_check.check_design(SimpleNamespace(debug=svc), session)
        assert rec["state"] == "skipped" and "no OpenOCD here" in rec["text"], rec
        assert jtag.accepted == 0
    finally:
        svc.close()
        fake.stop()


# --- 3. the service ---------------------------------------------------------------------------------


class Rebooter:
    """The controller adapter's reboot, as the MPS3 MCC answers it (the board is a FakeShell)."""

    def reboot(self, progress=None, wait_s=None) -> dict:
        for phase in ("sent", "down", "up"):
            if progress:
                progress(phase, 0, 0)
        return {"fpga_file": "MB/HBI0309C/Nanosoc/nanosoc.bit", "phases": ["sent", "down", "up"]}


def test_the_reboot_job_carries_the_check_the_event_and_the_board_read(rig, jtag, monkeypatch):
    from fastapi.testclient import TestClient

    from harness_manager.core.services import EngineConfig
    from harness_manager.daemon.app import create_app
    from harness_manager.engine import Engine
    from tests.fakes.t13_daemon import TOKEN, bid_path, headers, state_dir

    monkeypatch.setenv("STUB_OPENOCD_IDCODE", "none")
    fake = slot_board(boot_rm_id=NANOSOC)
    eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
        "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
        "tftp_port": fake.tftp_port, "rbb_port": jtag.port}}))
    seen: list[Event] = []
    eng.bus.subscribe("design.check", seen.append)
    H = headers()
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/boards", json={"target": f"{fake.host}:{fake.control_port}"},
                       headers=H)
            assert r.status_code == 200, r.text
            bid = r.json()["board_id"]
            assert c.get(bid_path(bid), headers=H).json()["design_check"] is None
            eng.session(bid).controller = Rebooter()
            r = c.post(bid_path(bid) + "/controller/reboot", json={}, headers=H)
            assert r.status_code == 202, r.text
            job = wait(c, r.json()["job"], H)
            assert job["state"] == "done", job                 # never fails the reboot
            check = job["result"]["design_check"]
            assert check["state"] == "unverified" and check["text"] == H1_WORDS
            assert job["result"]["fpga_file"].endswith("nanosoc.bit")
            assert [e.data["state"] for e in seen] == ["unverified"]
            got = c.get(bid_path(bid), headers=H).json()["design_check"]
            assert got["state"] == "unverified" and got["rm_id"] == "0x01000001"
            # Twin: a deploy proves its own design, and the service forgets the check
            eng.bus.publish(Event("deploy.done", bid, {"verified": True,
                                                       "rm_id": "0x01000001"}))
            assert c.get(bid_path(bid), headers=H).json()["design_check"] is None
            # Twin: the DAP answers -> verified, and the board read says so
            monkeypatch.delenv("STUB_OPENOCD_IDCODE")
            r = c.post(bid_path(bid) + "/controller/reboot", json={}, headers=H)
            job = wait(c, r.json()["job"], H)
            assert job["result"]["design_check"]["state"] == "verified"
            assert c.get(bid_path(bid), headers=H).json()["design_check"]["state"] == "verified"
    finally:
        eng.close_all()
        fake.stop()


def wait(c, job: str, H: dict, timeout: float = 30.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = c.get(f"/api/v1/jobs/{job}", headers=H).json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job} still running")
