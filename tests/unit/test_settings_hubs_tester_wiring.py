"""The lead's integration of SET-HUBS and SET-API: ``config test hubs NAME`` and
``POST /settings/test`` reach SET-HUBS's Test connection through the ``hubs`` CONVENTION
(``settings/testers.py``), and it never takes a lease. Each check has a negative twin."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.settings import hubs as H
from harness_manager.settings import testers as T
from tests.fakes.t8_hub_rest import FakeFpgahub


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "POLICY_PATH", tmp_path / "no-policy.toml")
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)


def _state() -> Path:
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def rest():
    with FakeFpgahub() as hub:
        (_state() / "settings.toml").write_text(f'[hubs.remote]\nurl = "{hub.url}"\n')
        yield hub, H.load_resolver()


def test_the_hubs_section_has_a_tester_and_it_runs_test_connection(rest):
    hub, r = rest
    tok = hub.add_token("alice", "write")
    H.set_hub_token("remote", r, value=tok)
    t = T.tester_for("hubs")
    assert t is not None and t.job                       # an ssh round trip: run as a job
    out = T.run(t, T.TestRequest(section="hubs", name="remote", resolver=r))
    assert out["passed"] and [s["step"] for s in out["steps"]][:3] == ["config", "reach", "auth"]
    assert tok not in json.dumps(out)
    assert not hub.leases and not hub.emitted("lease.")


def test_negative_twin_a_wrong_token_fails_at_auth_through_the_same_path(rest):
    hub, r = rest
    H.set_hub_token("remote", r, value="not-the-token")
    out = T.run(T.tester_for("hubs"), T.TestRequest(section="hubs", name="remote", resolver=r))
    assert not out["passed"] and out["failed"] == "auth" and out["why"].startswith("auth:")


def test_an_unsaved_table_is_tested_before_saving(rest):
    hub, r = rest
    out = T.run(T.tester_for("hubs"),
                T.TestRequest(section="hubs", name="draft", table={"url": hub.url}, resolver=r))
    assert out["transport"] == "rest" and out["steps"][0]["step"] == "config"
    assert "draft" not in (_state() / "settings.toml").read_text()      # nothing was saved


def test_negative_twin_no_name_and_no_table_is_a_usage_error(rest):
    _hub, r = rest
    with pytest.raises(UsageError):
        H.test_connection(T.TestRequest(section="hubs", resolver=r))
