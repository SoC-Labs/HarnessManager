"""SET-WIRE, end to end in real ``python -m harness_manager.daemon`` processes (loopback,
ephemeral ports, a virtual board), each check with its negative twin:

- a ``--state-dir`` service reads ITS OWN ``boards.toml``, never the one in
  ``$HARNESS_MANAGER_STATE_DIR`` (SETTINGS.md §12.6: "a --demo/--state-dir service still
  reads the real boards.toml");
- ``daemon start`` passes ``--log-level`` and ``--pack-overrides`` on to the service
  (SETTINGS.md §12.7), and without them the service's settings decide.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from harness_manager.daemon import control
from harness_manager.daemon.state import daemon_log_path
from tests.fakes.t13_daemon import kill, pack_overrides, spawn, wait_info
from tests.fakes.virtual_board import VirtualMps3


def user_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def client(info) -> httpx.Client:
    return httpx.Client(base_url=info.base_url, trust_env=False, timeout=30.0,
                        headers={"Authorization": f"Bearer {info.token}"})


def boards_toml(root: Path, endpoint: str, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "boards.toml").write_text(f'[boards.b]\nmatch = ["{endpoint}"]\nname = "{name}"\n')


def opened_name(tmp_path: Path, service: Path, *, own: str | None) -> str:
    """Open the virtual board through a service on ``service``; the name it shows."""
    with VirtualMps3(tmp_path / "vb") as vb:
        boards_toml(user_dir(), vb.shell_endpoint, "USER-REAL")
        if own is not None:
            boards_toml(service, vb.shell_endpoint, own)
        proc = spawn(service, "--pack-overrides", json.dumps(pack_overrides(vb)))
        try:
            info = wait_info(service, proc.pid)
            with client(info) as c:
                r = c.post("/api/v1/boards", json={"target": vb.shell_endpoint})
                assert r.status_code == 200, r.text
                return r.json()["info"]["candidate"]["name"]
        finally:
            kill(proc)


def test_a_state_dir_service_names_the_board_from_its_own_boards_toml(tmp_path):
    assert opened_name(tmp_path, tmp_path / "svc", own="SERVICE-OWN") == "SERVICE-OWN"


def test_negative_twin_the_users_boards_toml_never_reaches_it(tmp_path):
    # Before SET-WIRE this was "USER-REAL": the service read $HARNESS_MANAGER_STATE_DIR's file.
    assert opened_name(tmp_path, tmp_path / "svc", own=None) != "USER-REAL"


@pytest.fixture
def started(tmp_path: Path):
    """``control.start`` (what ``harness-manager daemon start`` calls), always stopped."""
    dirs: list[Path] = []

    def start(name: str, **kw):
        sdir = tmp_path / name
        dirs.append(sdir)
        return sdir, control.start(sdir, **kw)

    yield start
    for sdir in dirs:
        try:
            control.stop(sdir, force=True, timeout=15)
        except Exception:  # noqa: BLE001 - cleanup: a failed stop is the test's own failure
            pass


def pace(info) -> dict:
    with client(info) as c:
        r = c.get("/api/v1/settings", params={"key": "mps3.console.pace_ms"})
        assert r.status_code == 200, r.text
        return r.json()["rows"][0]


def test_daemon_start_hands_the_log_level_and_pack_overrides_to_the_service(started):
    sdir, info = started("a", log_level="debug",
                         pack_overrides={"mps3": {"console_pace_s": 0.0}})
    row = pace(info)
    assert (row["value"], row["source"]) == (0, "pack")       # the overrides reached the pack
    log = daemon_log_path(sdir).read_text()
    assert "--log-level debug" in log and " DEBUG " in log


def test_negative_twin_without_the_flags_the_services_settings_decide(started, tmp_path):
    sdir = tmp_path / "b"
    sdir.mkdir()
    (sdir / "settings.toml").write_text('[advanced]\nlog_level = "warning"\n')
    sdir, info = started("b")
    assert pace(info)["value"] == 20                          # the pack's own default
    log = daemon_log_path(sdir).read_text()
    assert "--log-level" not in log and " DEBUG " not in log and " INFO " not in log
