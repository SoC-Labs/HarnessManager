"""Lane SET-UI-MERGE, end to end in real ``python -m harness_manager.daemon`` processes (loopback,
ephemeral ports, no board): the Settings API of a ``--demo`` service (``app --demo``), with a
plain ``--state-dir`` service as its negative twin.

The demo's settings are its own: what the dialog writes lands in the demo's state dir, never in
the user's (``$HARNESS_MANAGER_STATE_DIR`` here); no hub is reached (Test connection and "Add
this board" are refused before anything runs); ``service.demo`` tells the dialog; and the
demo pack's MPS3 rows are there, the OS-slot card timing included.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from harness_manager.daemon import control


def user_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def client(info) -> httpx.Client:
    return httpx.Client(base_url=info.base_url, trust_env=False, timeout=30.0,
                        headers={"Authorization": f"Bearer {info.token}"})


@pytest.fixture
def started(tmp_path: Path):
    """``control.start`` (what ``harness-manager daemon start [--demo]`` calls), always stopped."""
    dirs: list[Path] = []

    def start(name: str, **kw):
        sdir = tmp_path / name
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "settings.toml").write_text('[hubs.lab]\ntransport = "ssh"\n')    # no host
        dirs.append(sdir)
        return sdir, control.start(sdir, **kw)

    yield start
    for sdir in dirs:
        try:
            control.stop(sdir, force=True, timeout=15)
        except Exception:  # noqa: BLE001 - cleanup: a failed stop is the test's own failure
            pass


def user_files() -> list[str]:
    root = user_dir()
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def test_a_demo_service_keeps_its_settings_to_itself_and_reaches_no_hub(started):
    before = user_files()
    sdir, info = started("demo", demo=True)
    with client(info) as c:
        listing = c.get("/api/v1/settings").json()
        assert listing["service"] == {"demo": True}
        assert listing["files"]["config_dir"] == str(sdir)
        keys = {r["key"] for r in listing["rows"]}
        assert {"mps3.slot.job_timeout_s", "mps3.slot.card_write_bps", "mps3.console.pace_ms"} <= keys
        # a change and a secret land in the demo's own dir
        assert c.put("/api/v1/settings", json={"mps3.slot.job_timeout_s": 1200}).status_code == 200
        r = c.put("/api/v1/settings/secrets/updates.github_token", json={"value": "ghp_demo"})
        assert r.status_code == 200 and r.json()["secret"]["backend"] == "file", r.text
        assert "job_timeout_s = 1200" in (sdir / "settings.toml").read_text()
        assert (sdir / "secrets" / "index.json").is_file()
        # no real hub: refused before a job, with the reason
        r = c.post("/api/v1/settings/test", json={"section": "hubs", "name": "lab"})
        assert r.status_code >= 400 and "the demo reaches no real hub" in r.text, r.text
        r = c.post("/api/v1/hubs/lab/boards", json={"target": "mps3_01_pl"})
        assert r.status_code >= 400 and "the demo reaches no real hub" in r.text, r.text
        assert c.get("/api/v1/jobs").json()["jobs"] == []
    assert user_files() == before                          # the user's own dir: untouched


def test_negative_twin_a_state_dir_service_is_not_the_demo_and_tests_its_hub(started):
    _sdir, info = started("plain")
    with client(info) as c:
        assert c.get("/api/v1/settings").json()["service"] == {"demo": False}
        r = c.post("/api/v1/settings/test", json={"section": "hubs", "name": "lab"})
        assert r.status_code == 202, r.text                  # the tester runs (a job)
        assert [j["kind"] for j in c.get("/api/v1/jobs").json()["jobs"]] == ["settings_test"]
