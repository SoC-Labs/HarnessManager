"""UI v2 drop B (lane UI2-API-HUB): the hub reads behind G3 and G11, on a scripted SSH runner and
fpgahub's REST fake (``tests/fakes/t8_hub_rest.py``). Nothing here reaches a real hub.

- G3: every target's lease in ONE read (``fpgahub status --json`` over SSH, ``GET /status`` over
  REST), ``LeaseService.hub_overview`` reusing it and seeding ``last_known``;
- G11: the waiters' tier and the background queue (REST's ``GET /targets/{t}/lease``; SSH's
  ``board lease show --json``), and ``want_s`` in a request note.

Each check has a negative twin.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyverify.lease import RunResult

from harness_manager.core.errors import UnavailableError, UnreachableError, UsageError
from harness_manager.services.lease import BACKGROUND_TTL_S, LeaseService
from harness_manager.transports import hub_rest
from harness_manager_mps3 import hub as hubmod
from tests.fakes import t8_hub_rest as fakes

GOLDEN = json.loads((Path(fakes.__file__).with_name("t8_fpgahub_v030_golden.json")).read_text())
STATUS = next(s for s in GOLDEN["steps"] if s["name"] == "status")["response"]
HUB = "mapstone-dev.ecs.soton.ac.uk"
TARGET = "mps3_01_pl"


class Runner:
    """A hub that answers the fpgahub verbs these reads use, and records every argv."""

    def __init__(self, **answers: str) -> None:
        self.answers = answers
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float | None = None) -> RunResult:
        self.argv.append(list(argv))
        words = " ".join(w for w in argv[1:] if not w.startswith("-"))
        for verb, out in self.answers.items():
            if words.startswith(verb.replace("_", " ")):
                return RunResult(0, out, "")
        return RunResult(2, "", f"no such command {words}")


def rich_json(data: Any) -> str:
    """What ``console.print_json`` prints (indented, maybe with ANSI colour)."""
    return "\x1b[1m" + json.dumps(data, indent=2) + "\x1b[0m\n"


def client(runner: Runner) -> hubmod.HubClient:
    return hubmod.HubClient(HUB, TARGET, runner=runner, board="mps3_01")


# --- G3 over SSH: fpgahub status --json -----------------------------------------------------------------


def test_ssh_overview_reads_every_target_in_one_call():
    r = Runner(status=rich_json(STATUS))
    rows = client(r).lease_overview()
    assert r.argv == [["fpgahub", "status", "--json"]]
    by = {row["target"]: row for row in rows}
    assert set(by) == {"mps3_01_pl", "kr260_01_ps", "kr260_01_pl"}
    assert by["mps3_01_pl"]["state"] == "held" and by["mps3_01_pl"]["queue_length"] == 1
    assert by["mps3_01_pl"]["holder"] == "alice@mapstone-dev" and by["mps3_01_pl"]["in_use"]
    assert by["kr260_01_pl"] == {"target": "kr260_01_pl", "state": "none", "holder": "",
                                 "user": "", "expires_at": "", "queue_length": 0,
                                 "in_use": False}
    assert hubmod.hub_read_only(["fpgahub", "status", "--json"])   # safe to try twice


@pytest.mark.parametrize("bad", [
    {"targets": []},                                             # not the /status shape
    {"boards": [{"name": "t1", "lease_state": "reserved"}]},     # a state we do not know
    {"boards": [{"lease_state": "none"}]},                       # a row with no name
    {"boards": [{"name": "t1", "lease_queue_length": -1}]},
])
def test_twin_an_overview_it_cannot_read_is_never_free(bad):
    with pytest.raises(UnreachableError):
        client(Runner(status=rich_json(bad))).lease_overview()


def test_twin_the_queued_state_names_the_head_waiter_not_a_holder():
    row = hub_rest.parse_overview({"boards": [{"name": "t1", "lease_state": "queued",
                                               "lease_holder": "bob@h", "lease_queue_length": 1}]},
                                  "x")[0]
    assert row["state"] == "queued" and row["holder"] == "bob@h"
    svc_row = LeaseService(Path("/nonexistent"))._overview_row(
        SimpleNamespace(host=HUB, target="t1", client=None), row, "me@h", 0.0, 0.0, ["b1"])
    assert svc_row["state"] == "free" and svc_row["waiting"] and svc_row["next"] == "bob@h"
    assert svc_row["holder"] == "" and not svc_row["mine"]


# --- G3 over REST: GET /status ---------------------------------------------------------------------------


def test_rest_overview_is_get_status():
    with fakes.FakeFpgahub() as hub:
        alice = hub.add_token("alice")
        hub_rest.RestHubClient(fakes.rest_config(hub), credential=hub_rest.Credential(
            alice, "test")).lease_acquire("x", ttl=600)
        c = fakes.client_for(hub, hub.add_token("carol", "read"))
        rows = {r["target"]: r for r in c.lease_overview()}
        assert rows["mps3_01_pl"]["state"] == "held"
        assert rows["mps3_01_pl"]["holder"] == hub.holder("alice")
        assert rows["kr260_01_ps"]["state"] == "none"
        assert any(q["path"] == "/api/v1/status" for q in hub.requests)
        assert hub_rest.ROUTES["status"] == ("GET", "/api/v1/status")


def test_twin_rest_overview_without_a_token_is_refused():
    from harness_manager.core.errors import HarnessError

    with fakes.FakeFpgahub() as hub:
        c = fakes.client_for(hub, None)
        with pytest.raises(HarnessError):
            c.lease_overview()


# --- G3 in the lease service: one read per hub, reused, seeding last_known --------------------------------


class OverviewClient:
    transport = "ssh"

    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows, self.calls = rows, 0

    def lease_overview(self) -> list[dict[str, Any]]:
        self.calls += 1
        if isinstance(self.rows, Exception):
            raise self.rows
        return [dict(r) for r in self.rows]

    def principal(self) -> str:
        return "me@mapstone-dev"


ROWS = [{"target": "mps3_01_pl", "state": "held", "holder": "alice@lab", "user": "alice",
         "expires_at": "2026-10-01T12:00:00+00:00", "queue_length": 2, "in_use": True},
        {"target": "mps3_02_pl", "state": "none", "holder": "", "user": "", "expires_at": "",
         "queue_length": 0, "in_use": False}]


def test_one_overview_read_serves_every_board_on_the_hub(tmp_path):
    svc, c = LeaseService(tmp_path), OverviewClient(ROWS)
    hub = SimpleNamespace(host=HUB, target="mps3_01_pl", client=c)
    boards = {"mps3_01_pl": ["mps3@10.0.0.1:6900"], "mps3_02_pl": ["mps3@10.0.0.2:6900"]}
    out = svc.hub_overview("lab", hub, boards=boards)
    assert c.calls == 1 and out["cached"] is False and out["hub"] == "lab"
    again = svc.hub_overview("lab", SimpleNamespace(host=HUB, target="mps3_02_pl", client=c),
                             boards=boards)
    assert c.calls == 1 and again["cached"] is True             # another target: the same read
    svc.hub_overview("lab", hub, boards=boards, refresh=True)
    assert c.calls == 2
    k1, k2 = svc.last_known("mps3@10.0.0.1:6900"), svc.last_known("mps3@10.0.0.2:6900")
    assert k1["state"] == "held" and k1["holder"] == "alice@lab" and k1["source"] == "overview"
    assert k1["queue_length"] == 2 and not k1["here"]
    assert k2["state"] == "free"


def test_twin_a_failed_overview_raises_and_seeds_nothing(tmp_path):
    svc = LeaseService(tmp_path)
    hub = SimpleNamespace(host=HUB, target="mps3_01_pl",
                          client=OverviewClient(UnreachableError("ssh: connect refused")))
    with pytest.raises(UnreachableError):
        svc.hub_overview("lab", hub, boards={"mps3_01_pl": ["b1"]})
    assert svc.last_known("b1") is None
    with pytest.raises(UnavailableError):                        # a client with no overview
        svc.hub_overview("lab", SimpleNamespace(host=HUB, target="t", client=object()))


def test_our_own_newer_state_wins_over_an_older_overview(tmp_path):
    svc, c = LeaseService(tmp_path), OverviewClient(ROWS)
    hub = SimpleNamespace(host=HUB, target="mps3_02_pl", client=c)
    svc.hub_overview("lab", hub, boards={"mps3_02_pl": ["b2"]})
    svc._board_for(hub, "b2")
    svc._know(hub, "acquire", "me@mapstone-dev", "2026-10-01T13:00:00+00:00")
    assert svc.last_known("b2")["source"] == "acquire" and svc.last_known("b2")["state"] == "held"
    row = {t["target"]: t for t in svc.hub_overview("lab", hub)["targets"]}["mps3_02_pl"]
    assert row["source"] == "acquire" and row["state"] == "held"


# --- G11: the background queue and the waiters' tier ---------------------------------------------------------


def board_lease(background: list[dict[str, Any]], member: str = TARGET) -> dict[str, Any]:
    return {"board": "mps3_01", "state": "held",
            "members": [{"board": member, "current": {"holder": "alice@h", "user": "alice",
                                                       "expires_at": "2026-10-01T12:00:00Z",
                                                       "tier": "interactive"}}],
            "queue": [{"board": "mps3_01", "holder": "bob@h", "user": "bob", "position": 1,
                       "tier": "interactive"}],
            "background_queue": background, "current_tier": "interactive"}


def test_ssh_lease_queues_reads_both_tiers():
    bg = [{"board": "mps3_01", "holder": "ci@h", "user": "ci", "position": 1,
           "tier": "background"}]
    r = Runner(board_lease_show=rich_json(board_lease(bg)))
    st = client(r).lease_queues()
    assert r.argv == [["fpgahub", "board", "lease", "show", "mps3_01", "--json"]]
    assert st.held and st.holder == "alice@h" and st.tier == "interactive"
    assert st.background_known and [(q.holder, q.tier) for q in st.background_queue] == [
        ("ci@h", "background")]
    assert [(q.holder, q.tier) for q in st.queue] == [("bob@h", "interactive")]


def test_twin_ssh_lease_queues_of_a_board_without_the_target_is_unreachable():
    r = Runner(board_lease_show=rich_json(board_lease([], member="mps3_01_ps")))
    with pytest.raises(UnreachableError):
        client(r).lease_queues()


def test_rest_lease_status_keeps_tier_and_the_background_queue():
    with fakes.FakeFpgahub() as hub:
        a = fakes.client_for(hub, hub.add_token("alice"))
        a.lease_acquire("x", ttl=600)
        st = fakes.client_for(hub, hub.add_token("carol", "read")).lease_status()
        assert st.held and st.tier == "interactive" and st.background_known
        assert st.background_queue == ()
    # twin: a waiter it cannot read is never dropped quietly
    with pytest.raises(UnreachableError):
        hub_rest.queue_entries([{"position": 1}], "interactive")


class SshLikeClient:
    """``lease_status`` without the background queue (SSH's ``lease show``), and ``lease_queues``."""

    def __init__(self, queues: Any) -> None:
        self.queues, self.queue_calls = queues, 0

    def lease_status(self) -> hubmod.LeaseStatus:
        return hubmod.LeaseStatus(True, "alice@h", "alice", "", (hubmod.QueueEntry(1, "bob@h", "bob"),))

    lease_show = lease_status

    def lease_queues(self) -> hubmod.LeaseStatus:
        self.queue_calls += 1
        if isinstance(self.queues, Exception):
            raise self.queues
        return self.queues


def test_the_view_reads_the_ssh_background_queue_only_when_asked_and_reuses_it(tmp_path):
    bg = hubmod.LeaseStatus(True, "alice@h", queue=(), background_known=True,
                            background_queue=(hubmod.QueueEntry(1, "ci@h", "ci", "background"),))
    c = SshLikeClient(bg)
    svc = LeaseService(tmp_path)
    hub = SimpleNamespace(host=HUB, target=TARGET, client=c)
    plain = svc.view(hub)
    assert c.queue_calls == 0 and plain["background_known"] is False    # not asked: no call
    v = svc.view(hub, background=True)
    assert c.queue_calls == 1 and v["background_known"] is True
    assert [(w["holder"], w["tier"]) for w in v["background_queue"]] == [("ci@h", "background")]
    assert v["queue"][0]["tier"] == "interactive"
    svc.forget(hub)
    svc.view(hub, background=True)
    assert c.queue_calls == 2                                   # a forget reads again ...
    svc.view(hub, background=True)
    assert c.queue_calls == 2 and BACKGROUND_TTL_S >= 60        # ... then reused


def test_twin_a_failed_background_read_is_unknown_never_empty(tmp_path):
    svc = LeaseService(tmp_path)
    hub = SimpleNamespace(host=HUB, target=TARGET,
                          client=SshLikeClient(UnreachableError("board lease show failed")))
    v = svc.view(hub, background=True)
    assert v["background_known"] is False and "board lease show failed" in v["background_reason"]
    assert v["lease"]["holder"] == "alice@h"                     # the rest of the view stands


# --- G11: how long, in the request note ------------------------------------------------------------------------


def note(**kw: Any) -> hubmod.RequestNote:
    base = {"id": "r1", "by": "bob@h", "user": "bob", "host": "h", "message": "hi",
            "created_at": "2026-10-01T10:00:00+00:00", "deadline_at": "2026-10-01T10:02:00+00:00"}
    return hubmod.RequestNote(**{**base, **kw})


def test_want_s_is_carried_only_when_set_and_optional_when_read():
    assert "want_s" not in json.loads(hubmod.encode_request(note()))   # the frozen bytes
    body = json.loads(hubmod.encode_request(note(want_s=1800)))
    assert body["want_s"] == 1800
    assert hubmod.decode_request(body, "req-r1.json").want_s == 1800
    old = json.loads(hubmod.encode_request(note()))
    assert hubmod.decode_request(old, "req-r1.json").want_s == 0         # an older note
    with pytest.raises(UsageError):                                       # twin: out of range
        hubmod.encode_request(note(want_s=86401))
    from harness_manager.services.lease import check_want

    assert check_want(None) == 0 and check_want(3600) == 3600
    for bad in (59, 86401, True, "3600", 1.5):
        with pytest.raises(UsageError):
            check_want(bad)
