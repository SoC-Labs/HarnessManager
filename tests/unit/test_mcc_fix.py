"""MCC-FIX: the reboot budget, what the MCC loaded, the hub door's stage dir. Each has a twin.

Board-free: ``FakeMcc`` on a ``FakeClock`` plays the MCC, the CLI runs over T5's scripted
fake engine, and the settings live in the test's own state dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ActionFailedError, ExitCode, UsageError
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.settings import hubs as H
from harness_manager.settings.rows import CORE_ROWS
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import hub_sd
from harness_manager_mps3.constants import REBOOT_WAIT_S_BARE_METAL, REBOOT_WAIT_S_LINUX
from harness_manager_mps3.mcc import BootRecord, Mps3Controller, RebootWitness, boot_fields
from tests.fakes.fake_mcc import BOOT_BANNER, FakeMcc
from tests.fakes.t3_clock import FakeClock, RecordingPort
from tests.fakes.t5_fake_engine import FakeEngine

T = "127.0.0.1"
LOADED = "MB/HBI0309C/Nanosoc/nanosoc.bit"


def controller(mcc: FakeMcc, clock: FakeClock, impl: str | None) -> Mps3Controller:
    port = RecordingPort(mcc, clock)
    ctl = Mps3Controller("fake://mcc-fix", clock=clock, sleep=clock.sleep,
                         opener=lambda url, baud: port)
    if impl is not None:
        ctl.identity_fn = lambda: BoardIdentity(board_type="mps3", harness_impl=impl)
    return ctl


def slow_mcc(clock: FakeClock, boot_s: float, **kw) -> FakeMcc:
    return FakeMcc(clock=clock, down_s=1.0, boot_s=boot_s, autoboot_window_s=3.0, **kw)


# --- 1. the reboot budget: the harness's own, unless --wait says otherwise ------------------


def test_a_linux_board_reboot_waits_up_to_180_s_by_default():
    assert REBOOT_WAIT_S_LINUX == 180.0
    clock = FakeClock()
    ctl = controller(slow_mcc(clock, boot_s=150.0), clock, "linux")
    out = ctl.reboot()                                  # no wait_s: the pack's budget
    # up after ~145 s: past the bare-metal 120 s, inside the Linux 180 s
    assert 120 < out["up_after_s"] < 180 and ctl.last_reboot.boot.fpga_configured


def test_twin_bare_metal_keeps_its_120_s_budget():
    assert REBOOT_WAIT_S_BARE_METAL == 120.0
    clock = FakeClock()
    ctl = controller(slow_mcc(clock, boot_s=150.0), clock, "bare-metal")
    with pytest.raises(ActionFailedError, match="within 120s"):
        ctl.reboot()


def test_twin_an_explicit_wait_wins_over_the_linux_budget():
    clock = FakeClock()
    ctl = controller(slow_mcc(clock, boot_s=150.0), clock, "linux")
    with pytest.raises(ActionFailedError, match="within 60s"):
        ctl.reboot(wait_s=60)


@pytest.fixture
def fake() -> FakeEngine:
    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


@pytest.fixture(autouse=True)
def _stdin_eof(monkeypatch):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def spy_reboot(fake: FakeEngine, result: dict | None) -> list:
    """The fake controller's reboot, recording the wait it was given."""
    seen: list = []
    ctl = fake.st.adapters["controller"]

    def reboot(progress=None, wait_s=None):
        seen.append(wait_s)
        for phase in ("sent", "down", "up"):
            if progress:
                progress(phase, 0, 0)
        return result

    ctl.reboot = reboot
    return seen


def test_the_cli_leaves_the_wait_to_the_board_when_wait_is_not_given(fake, capsys):
    seen = spy_reboot(fake, None)
    assert main(["--json", "mcc", T, "reboot", "--yes"]) == ExitCode.OK
    assert seen == [None]                   # the pack picks 180 s Linux / 120 s bare metal


def test_twin_the_cli_passes_an_explicit_wait_and_refuses_a_bad_one(fake, capsys):
    seen = spy_reboot(fake, None)
    assert main(["--json", "mcc", T, "reboot", "--yes", "--wait", "42"]) == ExitCode.OK
    assert seen == [42.0]
    capsys.readouterr()
    assert main(["mcc", T, "reboot", "--yes", "--wait", "0"]) == ExitCode.USAGE
    assert seen == [42.0]                   # a refused wait never reaches the board


# --- 2. which file the MCC loaded -------------------------------------------------------------


def test_the_witness_carries_the_file_the_mcc_loaded():
    clock = FakeClock()
    ctl = controller(slow_mcc(clock, boot_s=25.0), clock, None)
    out = ctl.reboot(wait_s=120)
    assert out["fpga_file"] == LOADED
    assert out["board_file"] == "MB/HBI0309C/Nanosoc/nanosoc.txt"
    assert (out["mcc_firmware"], out["hbi_build"], out["bootloader"]) == \
        ("v1.3.2", "HBI0309 build 567", "v1.0.0")
    assert json.loads(json.dumps(out)) == out           # plain JSON, the API's result


def test_the_sd_ab_image_name_is_what_the_witness_reports():
    clock = FakeClock()
    banner = tuple(line.replace("nanosoc.bit", "nanosoca.bit") for line in BOOT_BANNER)
    ctl = controller(slow_mcc(clock, boot_s=25.0, boot_banner=banner), clock, None)
    assert ctl.reboot(wait_s=120)["fpga_file"] == "MB/HBI0309C/Nanosoc/nanosoca.bit"


def test_twin_a_banner_with_no_file_line_gives_empty_fields_not_a_crash():
    clock = FakeClock()
    banner = tuple(line for line in BOOT_BANNER
                   if not line.startswith(("Configuring FPGA from file", "Reading Board File",
                                           "ARM V2M-MPS3 Firmware")))
    ctl = controller(slow_mcc(clock, boot_s=25.0, boot_banner=banner), clock, None)
    out = ctl.reboot(wait_s=120)
    assert out["fpga_configured"] and out["fpga_file"] == "" and out["board_file"] == ""
    assert out["mcc_firmware"] == ""
    # and a witness with no boot record at all (the shell came back first)
    bare = RebootWitness(sent_at=0, down_after_s=1, up_after_s=2, down_evidence=("x",),
                         up_evidence="y").as_dict()
    assert bare["fpga_file"] == "" and bare["fpga_configured"] is None
    assert boot_fields(BootRecord()) == {k: "" for k in boot_fields(None)}


def test_the_cli_shows_the_loaded_file_in_every_format(fake, capsys):
    spy_reboot(fake, {"fpga_file": LOADED, "mcc_firmware": "v1.3.2", "summary": "ok"})
    assert main(["--json", "mcc", T, "reboot", "--yes"]) == ExitCode.OK
    data = json.loads(capsys.readouterr().out)
    assert data["fpga_file"] == LOADED and data["mcc_firmware"] == "v1.3.2"
    assert data["evidence"]["summary"] == "ok"
    assert main(["mcc", T, "reboot", "--yes"]) == ExitCode.OK
    assert f"MCC loaded {LOADED}" in capsys.readouterr().out
    assert main(["--tsv", "mcc", T, "reboot", "--yes"]) == ExitCode.OK
    row = capsys.readouterr().out.strip().split("\t")
    assert TSV_COLUMNS["mcc reboot"][-1] == "FPGA_FILE" and row[-1] == LOADED
    assert len(row) == len(TSV_COLUMNS["mcc reboot"])
    assert TSV_COLUMNS["mcc reboot"][:3] == ("BOARD_ID", "RESULT", "PHASES")   # append-only


def test_twin_a_controller_that_returns_nothing_gives_an_empty_field(fake, capsys):
    spy_reboot(fake, None)
    assert main(["--json", "mcc", T, "reboot", "--yes"]) == ExitCode.OK
    data = json.loads(capsys.readouterr().out)
    assert data["fpga_file"] == "" and data["evidence"] == {}
    assert main(["mcc", T, "reboot", "--yes"]) == ExitCode.OK
    assert "MCC loaded" not in capsys.readouterr().out


# --- 3. the hub door's stage dir comes from the settings --------------------------------------

HOST = "hub.invalid"
ADDR = "192.168.10.101"
USE_LAB = (f'[boards.lab]\nmatch = ["{ADDR}"]\nvia = "hub"\n'
           'hub = { use = "lab", target = "mps3_01_pl" }\n')


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "POLICY_PATH", tmp_path / "no-policy.toml")
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def lab_candidate() -> Candidate:
    return Candidate(pack="mps3", board_id=f"mps3@{ADDR}:6900",
                     links=(Link(LinkKind.ETHERNET, f"{ADDR}:6900", "shell control channel"),),
                     label="MPS3", evidence="test")


def ssh_backend(cfg) -> hub_sd.SshSdBackend:
    client = SimpleNamespace(target=cfg.target, host=cfg.host, transport="ssh")
    return hub_sd.backend_for(SimpleNamespace(client=client, config=cfg))


def test_the_stage_dir_comes_from_the_hubs_settings(hermetic):
    (hermetic / "settings.toml").write_text(
        f'[hubs.lab]\nhost = "{HOST}"\nstage_dir = "/srv/fpga/hm-stage"\n')
    (hermetic / "boards.toml").write_text(USE_LAB)
    cfg = hubmod.hub_config_for(lab_candidate())
    assert cfg.stage_dir == "/srv/fpga/hm-stage"
    be = ssh_backend(cfg)
    assert be.stage_dir == "/srv/fpga/hm-stage"
    assert hub_sd.SshUploader(HOST).argv(f"{be.stage_dir}/abc.bit")[-1].startswith(
        "mkdir -p /srv/fpga/hm-stage && ")


def test_twin_unset_the_stage_dir_is_todays_path(hermetic):
    (hermetic / "settings.toml").write_text(f'[hubs.lab]\nhost = "{HOST}"\n')
    (hermetic / "boards.toml").write_text(USE_LAB)
    cfg = hubmod.hub_config_for(lab_candidate())
    assert ssh_backend(cfg).stage_dir == hub_sd.STAGE_DIR == ".cache/harness-manager/hub-sd"
    assert H.DEFAULT_STAGE_DIR == hub_sd.STAGE_DIR
    # an inline hub table (no name) and a hub with no config keep it too
    assert ssh_backend(hubmod.HubConfig(host=HOST)).stage_dir == hub_sd.STAGE_DIR
    assert hub_sd.stage_dir_for(SimpleNamespace()) == hub_sd.STAGE_DIR


def test_the_stage_dir_row_is_declared_and_refuses_a_bad_value():
    row = next(s for s in CORE_ROWS if s.key == "hubs.*.stage_dir")
    assert row.default == hub_sd.STAGE_DIR and row.scope == "hub"
    assert "stage_dir" in H.HUB_FIELDS and "stage_dir" in H.HUB_KEYS
    assert row.check("/srv/fpga/hm-stage") == "" and row.check(hub_sd.STAGE_DIR) == ""
    for bad in ("", "a dir", "~/x", "-rf", "/srv/../etc"):
        assert row.check(bad), bad


def test_twin_a_board_table_that_sets_the_stage_dir_beside_use_is_refused(hermetic):
    (hermetic / "settings.toml").write_text(f'[hubs.lab]\nhost = "{HOST}"\n')
    (hermetic / "boards.toml").write_text(
        f'[boards.lab]\nmatch = ["{ADDR}"]\nvia = "hub"\n'
        'hub = { use = "lab", target = "mps3_01_pl", stage_dir = "/srv/x" }\n')
    with pytest.raises(UsageError, match="stage_dir"):
        hubmod.parse_board_hub({"use": "lab", "target": "mps3_01_pl", "stage_dir": "/srv/x"},
                               root=hermetic)
