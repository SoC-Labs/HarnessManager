"""PANEL-TRUTH: the Live display's lease check survives one hub hiccup (david, board 1).

2026-09-28, Linux harness rc2_v6 on ``mps3_01_pl``: david held the lease (Harness Manager's
service heartbeated it; the CLI said "yours"), and the Live display said "cannot confirm you
hold the lease on mps3_01_pl: lease show on mapstone-dev.ecs.soton.ac.uk failed: lease show
failed: kex_exchange_identification: read: Connection reset by peer". The hub's sshd had
throttled ONE ssh (MaxStartups), and the display asked the hub fresh on every open.

Now (``harness_manager_mps3.display``, module docstring): the lease service's own view while
it is under 60 s old; else the hub; a transient hub failure stands on the last time the hub
said this process holds it (``LeaseService.held_here``: a view that said ``here``, a
heartbeat it took) while that is under 5 minutes old; else "checking your lease with the
hub..." as ``connecting``, the hub's words only as the ``detail``, and a retry with
back-off. D3 holds throughout: nothing opens for anyone but the holder.

The REAL ``LeaseService`` and ``HubClient`` over the L1 fake fpgahub, whose ssh can be made
to fail the way the hub's sshd did (``FlakyRunner``); the REAL adapter, forward and
compositor from LM2's rig. Each check has its negative twin.
"""

# ruff: noqa: F811 - the tests take the fixture imported from LM2's lane test below
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyverify.lease import RunResult

from harness_manager.core.display import DisplayUnavailable
from harness_manager.core.errors import HeldError, UnreachableError
from harness_manager.services.lease import LeaseService
from harness_manager_mps3 import display as D
from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_fake_hub import FakeHub
from tests.fakes.lm1_fake_lcd_mirror import FakeLcdMirror, ViewerModel
from tests.integration.test_lm2_display_mps3 import (  # noqa: F401 - rig_factory is a fixture
    BID,
    HUB_HOST,
    TARGET,
    Clock,
    harness_board,
    rig_factory,
    shows,
    wait_for,
)

KEX = "kex_exchange_identification: read: Connection reset by peer\r\n"
HOLDER = "hm-test"


class FlakyRunner:
    """The hub's ssh as the fake fpgahub answers it, until ``fail`` is set: then every
    command fails the way the hub's sshd turned one away (exit 255, ssh's words)."""

    def __init__(self, fake: FakeHub) -> None:
        self.fake = fake
        self.fail = ""
        self.failed = 0

    def __call__(self, argv: Any, timeout: float | None = None) -> RunResult:
        if self.fail:
            self.failed += 1
            return RunResult(255, "", self.fail)
        return self.fake(argv, timeout)


@pytest.fixture
def world(tmp_path: Path) -> Any:
    fake = FakeHub(TARGET)
    runner = FlakyRunner(fake)
    clock = Clock()
    hub = SimpleNamespace(host=HUB_HOST, target=TARGET,
                          client=hubmod.HubClient(HUB_HOST, TARGET, runner=runner))
    svc = LeaseService(tmp_path / "state", clock=clock, tick_s=3600.0)
    yield SimpleNamespace(fake=fake, runner=runner, clock=clock, hub=hub, svc=svc)
    svc.close()
    fake.close()


def holder_rig(rig_factory: Any, world: Any) -> Any:
    """LM2's rig, behind the hub, with the daemon's LeaseService; the lease acquired by it."""
    rig = rig_factory(hub=True, leases=world.svc)
    rig.session.hub = world.hub
    world.svc.acquire(world.hub, board_id=BID, ttl_s=600, holder=HOLDER, heartbeat=False)
    assert world.svc.view(world.hub)["lease"]["here"] is True
    rig.adapter.lease_backoff_s = (0.2, 0.3)
    return rig


# --- 1. the service's view, and the fallback on one hub hiccup ------------------------------------


def test_a_recent_view_answers_without_asking_the_hub(rig_factory: Any, world: Any) -> None:
    rig = holder_rig(rig_factory, world)
    calls = len(world.fake.calls)
    world.clock.advance(D.LEASE_VIEW_MAX_AGE_S - 5)
    assert rig.adapter.display_reason() == ""
    assert len(world.fake.calls) == calls                 # the view was 55 s old: no ssh at all
    # the twin: past 60 s the hub is asked again
    world.clock.advance(10)
    assert rig.adapter.display_reason() == ""
    assert len(world.fake.calls) > calls


def test_a_cached_here_and_a_failing_hub_read_still_open(rig_factory: Any, world: Any) -> None:
    """The case david met: the heartbeat's lease, one ssh reset, and the display opens."""
    rig = holder_rig(rig_factory, world)
    world.clock.advance(D.LEASE_VIEW_MAX_AGE_S + 1)        # the view is old: the hub is asked
    world.runner.fail = KEX
    with FakeLcdMirror() as board:
        rig.serve(board)
        assert rig.adapter.display_reason() == ""
        sock = rig.adapter.display_connect()
        try:
            assert sock.recv(2) == b"LM"                      # the mirror answered
        finally:
            sock.close()
    assert world.runner.failed >= 1                          # the hub was asked, and failed
    assert len(rig.ssh.launches) == 1


def test_twin_the_hub_says_someone_else_holds_it_refused_by_name(rig_factory: Any,
                                                                  world: Any) -> None:
    rig = holder_rig(rig_factory, world)
    world.fake.steal("alice@lab")
    world.svc.forget(world.hub)                               # the hub's event: it changed
    reason = rig.adapter.display_reason()
    assert "lease holder only" in reason and f"holds {TARGET}" in reason
    with pytest.raises(HeldError) as exc:
        rig.adapter.display_connect()
    assert exc.value.holder == "alice@lab" and rig.ssh.launches == []
    # and a hub hiccup after that answer does not bring "yours" back
    world.clock.advance(D.LEASE_VIEW_MAX_AGE_S + 1)
    world.runner.fail = KEX
    with pytest.raises(DisplayUnavailable) as unk:
        rig.adapter.display_connect()
    assert unk.value.reason == D.CHECKING and rig.ssh.launches == []


def test_twin_a_here_older_than_five_minutes_is_not_trusted(rig_factory: Any,
                                                           world: Any) -> None:
    rig = holder_rig(rig_factory, world)
    world.clock.advance(D.LEASE_GOOD_MAX_AGE_S + 1)
    world.runner.fail = KEX
    with pytest.raises(DisplayUnavailable) as exc:
        rig.adapter.display_connect()
    assert exc.value.reason == D.CHECKING and exc.value.state == "connecting"
    assert "kex_exchange_identification" in exc.value.detail      # the hub's words: detail only
    assert "kex" not in exc.value.reason and rig.ssh.launches == []
    assert exc.value.retry_s == 0.2                               # the back-off's first wait
    with pytest.raises(DisplayUnavailable) as again:
        rig.adapter.display_connect()
    assert again.value.retry_s == 0.3                             # ... then the next


def test_twin_a_hub_that_refuses_us_is_not_a_hiccup(rig_factory: Any, world: Any) -> None:
    """Only a failure that passes by itself stands on the last "yours": a refused key does not."""
    rig = holder_rig(rig_factory, world)
    world.clock.advance(D.LEASE_VIEW_MAX_AGE_S + 1)
    world.runner.fail = "dam1n19@hub: Permission denied (publickey).\n"
    with pytest.raises(DisplayUnavailable) as exc:
        rig.adapter.display_connect()
    assert exc.value.reason.startswith(f"could not confirm your lease on {TARGET} with the hub")
    assert "Permission denied" in exc.value.detail and rig.ssh.launches == []
    assert exc.value.retry_s == D.LEASE_RETRY_S


# --- 2. through the compositor: "checking", then it opens on the retry ---------------------------


def test_a_transient_failure_with_no_good_view_shows_checking_then_opens(rig_factory: Any,
                                                                        world: Any) -> None:
    rig = holder_rig(rig_factory, world)
    world.clock.advance(D.LEASE_GOOD_MAX_AGE_S + 1)           # nothing recent to stand on
    world.runner.fail = KEX
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        st = wait_for(lambda: (s := rig.status())["reason"] == D.CHECKING and s,
                      what="checking your lease")
        assert st["state"] == "connecting"                        # under way, not down
        assert "kex_exchange_identification" in st["detail"]
        assert rig.ssh.launches == [] and board.stats["connects"] == 0   # D3: nothing opened
        world.runner.fail = ""                                    # the hub answers again
        wait_for(lambda: shows(vm, v, grid), what="the picture on the retry")
        assert rig.status()["state"] == "live" and rig.status()["detail"] == ""
        v.close()


def test_twin_a_hub_that_never_answers_never_opens(rig_factory: Any, world: Any) -> None:
    rig = holder_rig(rig_factory, world)
    world.clock.advance(D.LEASE_GOOD_MAX_AGE_S + 1)
    world.runner.fail = KEX
    with FakeLcdMirror() as board:
        rig.serve(board)
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: world.runner.failed >= 3, what="three checks")
        assert rig.status()["reason"] == D.CHECKING and rig.ssh.launches == []
        assert board.stats["connects"] == 0
        v.close()


# --- 3. the lease service's side: what it confirms, and what drops it -----------------------------


def test_held_here_follows_the_heartbeat_and_drops_with_the_lease(world: Any) -> None:
    svc, hub = world.svc, world.hub
    svc.acquire(hub, board_id=BID, ttl_s=600, holder=HOLDER, heartbeat=False)
    assert svc.held_here(hub, max_age_s=1.0) is not None       # the acquire: the hub said so
    world.clock.advance(D.LEASE_GOOD_MAX_AGE_S + 1)
    assert svc.held_here(hub, max_age_s=D.LEASE_GOOD_MAX_AGE_S) is None
    svc.track(BID, hub)
    svc.beat_due(force=True)                                   # the hub took our heartbeat
    age, lease = svc.held_here(hub, max_age_s=1.0)
    assert age == 0 and lease["here"] is True
    svc.forget(hub)                                            # a hub event keeps it
    assert svc.held_here(hub, max_age_s=1.0) is not None
    # the twin: our lease ends here: nothing to stand on
    svc.release(hub, board_id=BID)
    assert svc.held_here(hub, max_age_s=1.0) is None


def test_twin_a_heartbeat_that_failed_confirms_nothing(world: Any) -> None:
    svc, hub = world.svc, world.hub
    svc.acquire(hub, board_id=BID, ttl_s=600, holder=HOLDER, heartbeat=False)
    world.clock.advance(D.LEASE_GOOD_MAX_AGE_S + 1)
    svc.track(BID, hub)
    world.runner.fail = KEX
    svc.beat_due(force=True)
    assert svc.held_here(hub, max_age_s=D.LEASE_GOOD_MAX_AGE_S) is None


@pytest.mark.parametrize("text, hiccup", [
    ("lease show on hub failed: lease show failed: kex_exchange_identification: read: "
     "Connection reset by peer", True),
    ("the hub hub did not answer lease show in time (timed out)", True),
    ("lease show: cannot reach the hub hub (ssh: connect to host hub port 22: Connection "
     "timed out)", True),
    ("lease show: ssh to hub was refused (Permission denied (publickey).)", False),
    ("lease show on hub failed: HTTP 500 internal error", False),
])
def test_hub_hiccup_is_a_failure_that_passes_by_itself(text: str, hiccup: bool) -> None:
    assert D.hub_hiccup(UnreachableError(text)) is hiccup
    assert D.hub_hiccup(TimeoutError("slow")) is True
    assert D.hub_hiccup(HeldError("held")) is False
