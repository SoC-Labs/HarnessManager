"""HIL-AUTO end to end: the runner drives Harness Manager's REAL CLI against the virtual boards.

- **bare metal** (``docs/HIL_B0.md``): the L1 lab (``l1_rig.lab``): ``VirtualMps3`` behind the
  fake hub and the SSH tunnel, the MCC on a real pty read ON the "hub", ``stub_openocd`` for R5;
- **Linux, netboot** (``docs/HIL_LINUX.md`` Netboot mode): pyverify's FakeShell as
  ``SlotBoard`` (profile ``linux``, both OS slot headers zeroed, the SSH claim taken by another
  key) behind the same kind of hub (``claimed_lock.HubAndBoardSsh``), its claim probe answered
  from the board's identify port;
- **Linux, card-less** (Card-less mode: board 2): the same ``SlotBoard`` with no user microSD
  at all (``slots card: False``: harnessd answers ``card: false`` and no slots).

In both, the lease is taken through the CLI first (``lease acquire``), so the lease service
says ``here``, as it will for david. Every safety rule has its twin against the real CLI.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness_manager.cli.main import main as hm_main
from tests.fakes.hil_auto import HubProbe, InProcessHm, is_write, verb_of
from tools.hil.run import EXIT_PASS, EXIT_STOP
from tools.hil.run import main as hil_main

BOARD = "192.168.10.101"
LX_STATIC = 0x44EE76D5


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


class NoSleep(list):
    def __init__(self) -> None:
        super().__init__()
        self.now = 1_790_000_000.0

    def __call__(self, seconds: float) -> None:
        self.append(seconds)
        self.now += max(seconds, 0.0)

    def clock(self) -> float:
        return self.now


def hil(ev: Path, plan: str, *extra: str, hm: InProcessHm | None = None) -> tuple[int, InProcessHm]:
    hm = hm or InProcessHm()
    sleep = NoSleep()
    rc = hil_main(["run", "--plan", plan, "--board", BOARD, "--evidence", str(ev), "--gap", "1",
                   *extra], invoker=hm, sleep=sleep, clock=sleep.clock)
    return rc, hm


def acquire(capsys: pytest.CaptureFixture[str]) -> None:
    assert hm_main(["--json", "lease", "acquire", BOARD, "--ttl", "7200",
                    "--holder", "david-hm"]) == 0
    capsys.readouterr()


def summary(ev: Path) -> dict[str, Any]:
    return json.loads((ev / "summary.json").read_text())


def verdicts(ev: Path) -> dict[str, str]:
    return {c["id"]: c["verdict"] for c in summary(ev)["checks"]}


def overlays(root: Path, static: int, *names: str) -> Path:
    from tests.fakes.t2_overlays import make_overlay

    for name in names:
        make_overlay(root, name, rm_id={"greybox": 0, "nanosoc": 0x01000001,
                                        "nanosoc_ila": 0x0100000A}[name], static_id=static)
    return root


# --- the bare-metal lab ----------------------------------------------------------------------------


@pytest.fixture
def bm(tmp_path, monkeypatch) -> Iterator[Any]:
    from tests.fakes.l1_rig import lab
    from tests.fakes.t2_overlays import use_overlay_dirs
    from tests.fakes.t4_debug_rig import use_stub
    from tests.fakes.virtual_board import VirtualMps3

    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    use_stub(monkeypatch, tmp_path)
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        use_overlay_dirs(monkeypatch, overlays(tmp_path / "ov", vb.shell.static_id,
                                               "greybox", "nanosoc"))
        yield rig


def bm_static(rig: Any) -> str:
    return f"0x{rig.vb.shell.static_id:08x}"


def test_the_bare_metal_plan_passes_against_the_virtual_mps3(bm, tmp_path, capsys):
    acquire(capsys)
    rc, hm = hil(tmp_path / "ev", "bare-metal", "--writes", "safe",
                 "--expect-static", bm_static(bm))
    v = verdicts(tmp_path / "ev")
    assert rc == EXIT_PASS, (v, summary(tmp_path / "ev")["first_failure"])
    assert {k for k, x in v.items() if x == "pass"} == {
        "1", "2", "R1", "R4", "R5", "R6", "R7", "R7b", "R10", "R11", "W1", "W1b", "W4", "W4b"}
    assert bm.vb.shell.current_rm_id == 0 and summary(tmp_path / "ev")["end_state"]["greybox"] is True
    assert bm.hub.share_stops == [] and not any("share" in " ".join(r) for r in bm.hub.calls
                                                if "start" in r)
    assert [verb_of(a) for a in hm.calls if is_write(a)] == ["mcc temp", "identify", "program",
                                                               "restore"]


def test_twin_writes_none_on_bare_metal_leaves_the_board_untouched(bm, tmp_path, capsys):
    acquire(capsys)
    pushes = len(bm.vb.shell.accepted_pushes)
    rc, hm = hil(tmp_path / "ev", "bare-metal", "--expect-static", bm_static(bm))
    assert rc == EXIT_PASS and [a for a in hm.calls if is_write(a)] == []
    assert len(bm.vb.shell.accepted_pushes) == pushes and bm.vb.shell.current_rm_id == 0
    assert bm.tool.reader_runs == []                           # tty_00 never touched


def test_no_lease_here_refuses_to_start_against_the_real_cli(bm, tmp_path):
    rc, hm = hil(tmp_path / "ev", "bare-metal", "--writes", "safe",
                 "--expect-static", bm_static(bm))
    assert rc == EXIT_STOP and [verb_of(a) for a in hm.calls] == ["lease show"]
    assert "not leased" in summary(tmp_path / "ev")["refused_start"]


def test_twin_a_lease_someone_else_holds_is_not_here(bm, tmp_path):
    bm.hub.steal("soak-runner")
    rc, hm = hil(tmp_path / "ev", "bare-metal", "--expect-static", bm_static(bm))
    assert rc == EXIT_STOP and len(hm.calls) == 1
    assert "held by soak-runner" in summary(tmp_path / "ev")["refused_start"]


def test_the_runbooks_static_on_another_board_stops_at_r1(bm, tmp_path, capsys):
    acquire(capsys)
    rc, hm = hil(tmp_path / "ev", "bare-metal", "--writes", "safe")   # expects 0x72bb0a36
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["stopped"]["check"] == "R1"
    assert "identity.shell_id" in s["stopped"]["reason"]
    assert "program" not in [verb_of(a) for a in hm.calls]


def test_a_second_reader_on_tty00_skips_the_mcc_read(bm, tmp_path, capsys):
    acquire(capsys)
    bm.tool.others = [[4242, "python3 soak_linux.py tail /dev/mps3_01_pl/tty_00"]]
    rc, hm = hil(tmp_path / "ev", "bare-metal", "--writes", "safe",
                 "--expect-static", bm_static(bm))
    assert rc == EXIT_PASS
    r4 = next(c for c in summary(tmp_path / "ev")["checks"] if c["id"] == "R4")
    assert r4["verdict"] == "skipped" and r4["reason"].startswith("tty_00 busy")
    assert [verb_of(a) for a in hm.calls].count("mcc temp") == 1


# --- the Linux lab, netboot ------------------------------------------------------------------------


@dataclass
class LinuxLab:
    fake: Any
    hub: Any
    tool: Any
    ssh: Any


@contextmanager
def linux_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
              static: int = LX_STATIC, card: bool = True) -> Iterator[LinuxLab]:
    from harness_manager_mps3 import hub as hubmod
    from harness_manager_mps3 import tunnel as T
    from tests.fakes.claimed_lock import TRUSTED, HubAndBoardSsh, board_key_fp, route_board
    from tests.fakes.fake_mcc import FakeMcc
    from tests.fakes.hub_mcc_fakes import HubTool, PtyMcc
    from tests.fakes.l1_fake_hub import FakeHub
    from tests.fakes.l1_rig import write_boards_toml
    from tests.fakes.lxslots_board import slot_board
    from tests.fakes.t2_overlays import use_overlay_dirs

    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    use_overlay_dirs(monkeypatch, overlays(tmp_path / "ov", static,
                                           "greybox", "nanosoc", "nanosoc_ila"))
    zeroed = {"state": "empty", "err": "no S0LB header"}       # Netboot mode: both headers gone
    slots: dict[str, Any] = {"trusted_peer": TRUSTED, "running": "none", "a": zeroed,
                             "b": dict(zeroed)}
    if not card:
        slots = {"trusted_peer": TRUSTED, "running": "none", "card": False}   # board 2
    fake = slot_board(profile="linux", static_id=static, ssh_claimed=True,
                      ssh_host_key_sha256=board_key_fp(), slots=slots)
    ssh = HubAndBoardSsh()
    route_board(ssh, {6900: fake.control_port, 6910: fake.raw_tcp_port})
    monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
    monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
    hub = FakeHub("mps3_01_pl")
    mcc = FakeMcc()
    pty = PtyMcc(mcc)
    hub.add_tty("/dev/mps3_01_pl/tty_00", mcc)
    tool = HubTool(HubProbe(hub, fake.identify_port), mcc=mcc, pty=pty,
                   tty="/dev/mps3_01_pl/tty_00")
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda *_a, **_k: tool)
    from tests.fakes.claimed_lock import HUB
    write_boards_toml(state_dir(), f'[boards.lab]\nmatch = ["{BOARD}"]\nvia = "ssh:{HUB}"\n'
                                   f'hub = {{ host = "{HUB}", target = "mps3_01_pl" }}\n')
    try:
        yield LinuxLab(fake, hub, tool, ssh)
    finally:
        hubmod.SHARES.close_all()
        hub.close()
        ssh.close()
        pty.close()
        fake.stop()


@pytest.fixture
def lx(tmp_path, monkeypatch) -> Iterator[LinuxLab]:
    with linux_lab(tmp_path, monkeypatch) as rig:
        yield rig


def test_the_linux_netboot_plan_passes_against_the_linux_harness_fake(lx, tmp_path, capsys):
    acquire(capsys)
    rc, hm = hil(tmp_path / "ev", "linux-netboot", "--writes", "safe")
    s = summary(tmp_path / "ev")
    v = verdicts(tmp_path / "ev")
    assert rc == EXIT_PASS, (v, s["first_failure"])
    assert {k for k, x in v.items() if x == "pass"} == {
        "0.2", "0.3", "0.4", "A1", "A2", "A3", "B1", "C1", "D1", "D4a", "E1", "E1b", "Z2", "Z2b"}
    assert v["B3"] == "skipped"                                # claimed by another key: no adopt
    assert lx.fake.current_rm_id == 0 and s["end_state"]["greybox"] is True
    assert s["end_state"]["claim"] == {**s["end_state"]["claim"], "start": "other",
                                       "end": "other", "unchanged": True}
    # the swap went over 6910 and nothing touched the card or the slots
    pushes = lx.fake.accepted_pushes                      # two swaps: clearing + partial each
    assert len(pushes) == 4 and {p.transport for p in pushes} == {"tcp"}
    assert not any(verb_of(a) in ("slot push", "slot commit", "card clear", "card commit")
                   for a in hm.calls)
    c1 = json.loads((tmp_path / "ev" / "c1_slot_status.json").read_text())
    assert c1["stdout_json"]["slots"]["A"]["state"] == "empty"


def test_twin_writes_none_on_linux_sends_no_write_and_swaps_nothing(lx, tmp_path, capsys):
    acquire(capsys)
    rc, hm = hil(tmp_path / "ev", "linux-netboot")
    assert rc == EXIT_PASS and [a for a in hm.calls if is_write(a)] == []
    assert lx.fake.accepted_pushes == [] and lx.tool.reader_runs == []
    assert summary(tmp_path / "ev")["end_state"]["restore"].startswith("not needed")


def test_a_card_job_refuses_the_swap_and_the_runner_stops_without_force(lx, tmp_path, capsys):
    acquire(capsys)
    lx.fake.hold_job("writing", act="push", slot="B")      # another host's push, still writing
    rc, hm = hil(tmp_path / "ev", "linux-netboot", "--writes", "safe")
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and s["stopped"]["check"] == "E1", s["stopped"]
    assert "HELD" in s["stopped"]["reason"] and "never forces" in s["stopped"]["reason"]
    assert lx.fake.accepted_pushes == []
    assert not any("--force" in a for a in hm.calls)
    assert [verb_of(a) for a in hm.calls].count("program") == 1


def test_twin_the_full_linux_plan_expects_valid_slots_and_fails_c1_on_a_netbooted_board(
        lx, tmp_path, capsys):
    acquire(capsys)
    rc, _ = hil(tmp_path / "ev", "linux")
    s = summary(tmp_path / "ev")
    assert rc == 1 and s["first_failure"]["id"] == "C1"
    assert "slots.A.state" in s["first_failure"]["reason"]


def test_the_lease_taken_away_mid_run_stops_and_the_board_is_left_alone(lx, tmp_path, capsys):
    acquire(capsys)
    hm = InProcessHm()
    real = hm.__class__.__call__

    def steal_after_program(self, argv, timeout):
        out = real(self, argv, timeout)
        if "program" in argv:
            lx.hub.steal("someone-else")
        return out

    hm.__class__ = type("StealingHm", (InProcessHm,), {"__call__": steal_after_program})
    rc, _ = hil(tmp_path / "ev", "linux-netboot", "--writes", "safe", hm=hm)
    s = summary(tmp_path / "ev")
    assert rc == EXIT_STOP and "lease" in (s["stopped"] or {}).get("reason", "") + \
        s["end_state"]["restore"]
    assert "restore" not in [verb_of(a) for a in hm.calls]
    assert s["end_state"]["greybox"] is False


# --- the Linux lab, card-less (board 2) ------------------------------------------------------------


@pytest.fixture
def nocard(tmp_path, monkeypatch) -> Iterator[LinuxLab]:
    with linux_lab(tmp_path, monkeypatch, card=False) as rig:
        yield rig


def test_the_linux_nocard_plan_passes_against_a_card_less_board(nocard, tmp_path, capsys):
    acquire(capsys)
    rc, hm = hil(tmp_path / "ev", "linux-nocard", "--writes", "safe")
    s = summary(tmp_path / "ev")
    v = verdicts(tmp_path / "ev")
    assert rc == EXIT_PASS, (v, s["first_failure"])
    assert {k for k, x in v.items() if x == "pass"} == {
        "0.2", "0.3", "0.4", "A1", "A2", "A3", "B1", "C1", "D1", "D4a", "E1", "E1b", "Z2", "Z2b"}
    assert {k for k, x in v.items() if x == "skipped"} >= {
        "C2", "D2", "D3", "D4", "D5", "F6", "G1", "G2", "G3", "G4", "Z1", "Z2c"}
    # harnessd said card:false; the real CLI says it as exit 12 with the slot service's reason
    c1 = json.loads((tmp_path / "ev" / "c1_slot_status.json").read_text())
    assert c1["exit"] == 12 and c1["stdout_json"]["error"]["reason"].startswith(
        "no user microSD card in the slot")
    # the swaps and the MCC read, and not one card, slot or reset verb
    assert [verb_of(a) for a in hm.calls if is_write(a)] == ["mcc temp", "program", "restore"]
    assert not {"card status", "slot push", "slot commit", "slot rollback", "card clear",
                "mcc reboot", "mcc cmd", "reset", "power"} & {verb_of(a) for a in hm.calls}
    assert nocard.fake.boots == [] and nocard.fake.current_rm_id == 0
    assert s["end_state"]["greybox"] is True


def test_twin_the_netboot_plan_fails_c1_on_the_same_card_less_board(nocard, tmp_path, capsys):
    acquire(capsys)
    rc, _ = hil(tmp_path / "ev", "linux-netboot")
    s = summary(tmp_path / "ev")
    assert rc == 1 and s["first_failure"]["id"] == "C1"
    assert s["first_failure"]["reason"].startswith("exit 12, expected 0")
    assert "no user microSD card in the slot" in s["first_failure"]["reason"]


def test_twin_the_nocard_plan_fails_c1_on_a_blank_card(lx, tmp_path, capsys):
    acquire(capsys)
    rc, _ = hil(tmp_path / "ev", "linux-nocard")
    s = summary(tmp_path / "ev")
    assert rc == 1 and s["first_failure"]["id"] == "C1"
    assert s["first_failure"]["reason"].startswith("exit 0, expected 12")
