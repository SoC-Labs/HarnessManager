"""LR-B: GET /boards/{bid}/lease carries what the hub connection can do (CCR-1), through the
real daemon over L1's lab rig (a fake fpgahub over a fake ssh; the rig replaces the hub runner factory, so nothing leaves
the process)."""

from __future__ import annotations

from harness_manager_mps3 import hub as hubmod
from tests.fakes.t13_daemon import bid_path
from tests.integration.test_l1_hub_api import H, client, open_lab, rig  # noqa: F401 - fixtures

KEYS = ("notes_supported", "notes_reason", "can_revoke", "revoke_reason")


def test_get_lease_passes_the_capabilities_through(client, monkeypatch):  # noqa: F811
    bid = open_lab(client)
    got = client.get(f"{bid_path(bid)}/lease", headers=H).json()
    assert got["ok"] and {k: got[k] for k in KEYS} == {
        "notes_supported": True, "notes_reason": "", "can_revoke": True, "revoke_reason": ""}
    # twin: a client that cannot carry notes or revoke says so, before any request
    monkeypatch.setattr(hubmod.HubClient, "notes_supported", False, raising=False)
    monkeypatch.setattr(hubmod.HubClient, "notes_reason", "no note store here", raising=False)
    monkeypatch.setattr(hubmod.HubClient, "can_revoke",
                        lambda self: (False, "needs an admin token"), raising=False)
    got = client.get(f"{bid_path(bid)}/lease", headers=H).json()
    assert {k: got[k] for k in KEYS} == {
        "notes_supported": False, "notes_reason": "no note store here",
        "can_revoke": False, "revoke_reason": "needs an admin token"}
    assert got["request"] is None
