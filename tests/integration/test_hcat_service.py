"""HARNESS-CAT (H6): the harness versions catalogue on the update core.

A signed multi-version catalogue (the HARNESS-DIST spike's fake mints, a throwaway key,
``tests/fakes/hcat_catalog.py``) served on 127.0.0.1, listed for VirtualMps3 boards on the
fielded ILA static running release 1.1.0, through the real MPS3 pack (Debug USB, FakeMcc
on a fake clock). Every behaviour has a negative twin.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import HeldError, RefusedError
from harness_manager.services.harness_catalog import (
    VERDICT_DOOR,
    VERDICT_FITS,
    VERDICT_REKEY,
    HarnessCatalog,
)
from harness_manager.services.update.state import InstallRecords, UpdateState
from tests.fakes.hcat_catalog import NOTES, BoardRig, CatalogWorld


@pytest.fixture
def world(tmp_path):
    w = CatalogWorld(tmp_path / "world")
    w.publish()
    with w.serve() as srv:
        w.srv = srv
        yield w


@pytest.fixture
def rig(tmp_path, world, monkeypatch):
    r = BoardRig(tmp_path, world, monkeypatch)
    yield r
    r.close()


def rows(listing) -> dict[str, dict]:
    return {r["version"]: r for r in listing.as_dict()["releases"]}


def listing_for(rig, world, channels=("stable", "beta")):
    return HarnessCatalog(rig.svc).list(rig.session, channels=channels, source=world.srv.source())


class Hub:
    host = "mapstone-dev.ecs.soton.ac.uk"
    target = "mps3_01_pl"

    def close(self) -> None:
        pass


class Leases:
    """``LeaseService.view`` stand-in: what the hub says about the board's lease."""

    def __init__(self, lease: dict | None) -> None:
        self.lease = lease
        self.views = 0

    def view(self, hub) -> dict:
        self.views += 1
        return {"lease": self.lease, "hub": hub.host}


MINE = {"holder": "david@mapstone-dev", "mine": True}
ALICE = {"holder": "alice@lab-pc-07", "mine": False}


# --- list: verdicts and reasons --------------------------------------------------------------


def test_list_gives_every_release_on_the_channels_a_verdict_with_its_reason(rig, world):
    listing = listing_for(rig, world)
    r = rows(listing)
    assert list(r) == ["2.0.0", "1.1.1", "1.1.0", "1.0.0"]          # newest first, each once
    assert r["1.1.1"]["channels"] == ["stable", "beta"] and r["2.0.0"]["channels"] == ["beta"]
    # same static, a firmware re-bake: fits, the SD is rewritten
    assert (r["1.1.1"]["verdict"], r["1.1.1"]["mode"]) == (VERDICT_FITS, "full")
    assert r["1.1.1"]["why"].startswith("same static")
    assert r["1.1.1"]["changes"]["firmware"] == {"from": "d68dd0ed", "to": "0e12a0b0",
                                                 "changes": True}
    # another static: a re-key, with the typed consent phrase and what stops matching
    old = r["1.0.0"]
    assert old["verdict"] == VERDICT_REKEY and old["consent_phrase"] == "REKEY 0x3f1a560f"
    assert "consent" in old["needs"] and "0x72bb0a36 -> 0x3f1a560f" in old["why"]
    assert old["changes"]["static"] == {"from": "0x72bb0a36", "to": "0x3f1a560f", "changes": True}
    assert old["changes"]["kit"]["changes"] is True
    assert any("every overlay changes" in line for line in old["changes"]["summary"])
    assert any("DUT kit: kits for 0x72bb0a36 stop matching" in line
               for line in old["changes"]["summary"])
    # Linux on a bare-metal harness: its OS slot has no door here
    lnx = r["2.0.0"]
    assert lnx["verdict"] == VERDICT_DOOR and lnx["verdict_text"] == "needs Debug USB or hub"
    assert "linux-slot" in lnx["needs"] and "OS slot" in lnx["why"]
    assert any("slot" in b for b in lnx["reasons"])
    assert lnx["changes"]["kit"]["vivado_to"] == "2026.1"
    # the signed notes, the size, the cache
    assert [r[v]["notes"] for v in r] == [NOTES[v] for v in r]
    assert all(x["size"] > 0 and x["cached"] is False for x in r.values())
    assert [c["channel"] for c in listing.channels] == ["stable", "beta"]


def test_negative_twin_without_a_board_the_list_has_no_verdicts(world):
    from harness_manager.services.update import UpdateService

    svc = UpdateService(state_dir=world.root / "state-nob", trust=world.trust(), token="",
                        app_version="0.1.0", store=None)
    listing = HarnessCatalog(svc).list(None, channels=["stable"], source=world.srv.source())
    r = rows(listing)
    assert list(r) == ["1.1.1", "1.1.0", "1.0.0"] and listing.board is None
    assert {x["verdict"] for x in r.values()} == {""}
    assert all(x["why"] == "no board: name one for verdicts" for x in r.values())
    assert not any("running" in x["marks"] for x in r.values())


def test_negative_twin_without_the_debug_usb_a_base_change_needs_a_door(tmp_path, world,
                                                                        monkeypatch):
    rig = BoardRig(tmp_path, world, monkeypatch, usb=False)
    try:
        r = rows(listing_for(rig, world, channels=("stable",)))
    finally:
        rig.close()
    assert r["1.1.1"]["verdict"] == VERDICT_DOOR and "debug-usb" in r["1.1.1"]["needs"]
    assert "Debug USB" in r["1.1.1"]["why"]
    assert r["1.0.0"]["verdict"] == VERDICT_DOOR            # a door beats the re-key
    assert r["1.1.0"]["verdict"] == VERDICT_FITS             # overlays only: no door needed


def test_a_withdrawn_release_is_incompatible_and_says_why(rig, world):
    world.publish(withdrawn=("1.0.0",))
    r = rows(listing_for(rig, world, channels=("stable",)))
    assert r["1.0.0"]["verdict"] == "incompatible" and "withdrawn" in r["1.0.0"]["why"]
    assert r["1.1.1"]["verdict"] == VERDICT_FITS             # the twin: the others still fit


# --- running and installed marks --------------------------------------------------------------


def test_running_and_installed_marks_follow_the_board_and_the_install(rig, world):
    cat = HarnessCatalog(rig.svc)
    before = rows(listing_for(rig, world))
    assert before["1.1.0"]["marks"] == ["running"] and before["1.1.0"]["running"]
    assert "installed" not in before["1.1.1"]["marks"]
    assert before["1.1.1"]["marks"] == ["current", "offered"]
    plan, verified = cat.plan(rig.session, None, channel="stable", source=world.srv.source())
    assert plan.version == "1.1.1"
    out = cat.install(rig.session, plan, plan.approve(), verified)
    assert out.result == "installed" and rig.session.identity().firmware_sha == "0e12a0b0"
    listing = listing_for(rig, world)
    after = rows(listing)
    assert after["1.1.1"]["marks"] == ["running", "installed", "current", "offered"]
    assert after["1.1.0"]["marks"] == [] and not after["1.1.0"]["running"]
    assert listing.board["running_release"] == "1.1.1"
    assert listing.board["installed"]["version"] == "1.1.1"


def test_negative_twin_a_written_but_not_running_install_is_not_marked_running(
        tmp_path, world, monkeypatch):
    rig = BoardRig(tmp_path, world, monkeypatch, stale=True)   # the board keeps the old image
    try:
        cat = HarnessCatalog(rig.svc)
        plan, verified = cat.plan(rig.session, "1.1.1", channel="stable",
                                  source=world.srv.source())
        out = cat.install(rig.session, plan, plan.approve(), verified)
        assert out.result == "written-not-running"
        r = rows(listing_for(rig, world))
    finally:
        rig.close()
    assert r["1.1.1"]["marks"] == ["written", "current", "offered"]
    assert r["1.1.0"]["marks"] == ["running"]


# --- pins -------------------------------------------------------------------------------------


def test_a_pin_is_per_board_and_nothing_past_it_is_offered(tmp_path, world, monkeypatch):
    a = BoardRig(tmp_path, world, monkeypatch, name="a")
    b = BoardRig(tmp_path, world, monkeypatch, name="b", state_dir=a.state_dir)  # one state dir
    try:
        bid_a, bid_b = a.session.candidate.board_id, b.session.candidate.board_id
        assert bid_a != bid_b
        cat_a = HarnessCatalog(a.svc)
        out = cat_a.pin(bid_a, "1.1.0")
        assert out == {"board_id": bid_a, "pinned": "1.1.0", "previous": ""}
        la, lb = listing_for(a, world, ("stable",)), listing_for(b, world, ("stable",))
        ra, rb = rows(la), rows(lb)
        assert la.offer == "1.1.0" and la.board["pinned"] == "1.1.0"
        assert "pinned" in ra["1.1.0"]["marks"] and "past-pin" in ra["1.1.1"]["marks"]
        assert lb.offer == "1.1.1" and lb.board["pinned"] == ""
        assert not any("pinned" in r["marks"] or "past-pin" in r["marks"] for r in rb.values())
        # the planner honours it (the `update` alias too): board A is offered 1.1.0 only
        plan_a, _ = a.svc.plan_harness(a.session, channel="stable", source=world.srv.source())
        plan_b, _ = b.svc.plan_harness(b.session, channel="stable", source=world.srv.source())
        assert (plan_a.version, plan_b.version) == ("1.1.0", "1.1.1")
        assert any("pinned to harness 1.1.0" in w for w in plan_a.warnings)
        assert a.svc.check(channel="stable", source=world.srv.source(),
                           session=a.session)["plan"]["version"] == "1.1.0"
        # an explicit version is still the user's choice
        assert a.svc.plan_harness(a.session, channel="stable", source=world.srv.source(),
                                  version="1.1.1")[0].version == "1.1.1"
        # the twin: unpinned, A is offered the current release again
        assert cat_a.unpin(bid_a) == {"board_id": bid_a, "pinned": "", "previous": "1.1.0"}
        assert listing_for(a, world, ("stable",)).offer == "1.1.1"
    finally:
        b.close()
        a.close()


def test_a_pin_to_a_release_no_channel_lists_is_refused(rig, world):
    cat = HarnessCatalog(rig.svc)
    verified, _ = cat.channels(["stable"], source=world.srv.source())
    with pytest.raises(Exception, match="no harness release 9.9.9"):
        cat.pin(rig.session.candidate.board_id, "9.9.9", verified=verified)
    assert rig.svc.pins().get(rig.session.candidate.board_id) is None
    assert cat.pin(rig.session.candidate.board_id, "1.0.0", verified=verified)["pinned"] == "1.0.0"


# --- history ----------------------------------------------------------------------------------


def test_the_history_keeps_the_last_n_installs_newest_first(tmp_path):
    rec = InstallRecords(UpdateState.under(tmp_path / "s"), keep=3)
    for i in range(5):
        rec.put("mps3@b", {"version": f"1.0.{i}", "result": "installed"})
    hist = rec.history("mps3@b")
    assert [h["version"] for h in hist] == ["1.0.4", "1.0.3", "1.0.2"]
    assert rec.get("mps3@b")["version"] == "1.0.4"             # T7's last record, unchanged
    assert [h["version"] for h in rec.history("mps3@b", limit=1)] == ["1.0.4"]
    assert rec.history("mps3@other") == []


def test_negative_twin_below_the_limit_nothing_is_dropped_and_a_t7_record_still_shows(tmp_path):
    state = UpdateState.under(tmp_path / "s")
    rec = InstallRecords(state)
    rec.put("mps3@b", {"version": "1.0.0", "result": "installed"})
    rec.put("mps3@b", {"version": "1.1.0", "result": "installed"})
    assert [h["version"] for h in rec.history("mps3@b")] == ["1.1.0", "1.0.0"]
    # a board recorded by T7 (installed.json only, no history file yet)
    from harness_manager.services.update.state import atomic_write_json

    atomic_write_json(state.installed, {"mps3@t7": {"version": "1.0.0", "result": "installed"}})
    assert [h["version"] for h in InstallRecords(state).history("mps3@t7")] == ["1.0.0"]


def test_an_install_records_what_it_replaced_and_how(rig, world):
    cat = HarnessCatalog(rig.svc)
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    cat.install(rig.session, plan, plan.approve(), verified)
    (h,) = cat.history(rig.session.candidate.board_id)
    assert (h["version"], h["result"], h["kind"], h["from_version"]) == \
        ("1.1.1", "installed", "install", "1.1.0")
    assert h["fw_sha"] == "0e12a0b0" and h["static_id"].lower() == "0x72bb0a36"
    assert h["doors"] == ["mcc_sd", "host-store"] and h["backup"]["path"]
    assert (h["channel"], h["mode"], h["rekey"]) == ("stable", "full", False)


# --- rollback ---------------------------------------------------------------------------------


def test_rollback_picks_the_release_the_last_install_replaced(rig, world):
    cat = HarnessCatalog(rig.svc)
    bid = rig.session.candidate.board_id
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    cat.install(rig.session, plan, plan.approve(), verified)
    listing = listing_for(rig, world)
    assert listing.rollback[0]["version"] == "1.1.0" and listing.rollback[0]["source"] == "history"
    assert listing.rollback[0]["installable"]
    target = cat.rollback_target(bid, "previous", listing)
    assert target == "1.1.0"
    plan, verified = cat.plan(rig.session, target,
                              verified=listing.verified[listing.channel_of(target)])
    assert any("ROLLBACK" in w for w in plan.warnings) and not plan.rekey
    out = cat.install(rig.session, plan, plan.approve(), verified)
    assert out.result == "installed" and rig.session.identity().firmware_sha == "d68dd0ed"
    assert [h["version"] for h in cat.history(bid)] == ["1.1.0", "1.1.1"]
    # and now "previous" is 1.1.1 again
    assert cat.rollback_target(bid, "previous", listing_for(rig, world)) == "1.1.1"


def test_negative_twin_rollback_refuses_a_previous_release_the_channel_dropped(rig, world):
    cat = HarnessCatalog(rig.svc)
    bid = rig.session.candidate.board_id
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    cat.install(rig.session, plan, plan.approve(), verified)
    world.publish(stable=("1.0.0", "1.1.1"), beta=False)            # 1.1.0 is gone (P11)
    listing = listing_for(rig, world, ("stable",))
    first = listing.rollback[0]
    assert (first["version"], first["listed"], first["installable"]) == ("1.1.0", False, False)
    assert "no longer lists" in first["reason"]
    with pytest.raises(RefusedError, match="nothing to roll .* back to .*1.1.0"):
        cat.rollback_target(bid, "previous", listing)


def test_with_no_history_rollback_offers_the_channels_release_before_the_running_one(rig, world):
    listing = listing_for(rig, world, ("stable",))
    assert [(c["version"], c["source"]) for c in listing.rollback] == [("1.0.0", "channel")]
    cat = HarnessCatalog(rig.svc)
    assert cat.rollback_target(rig.session.candidate.board_id, "previous", listing) == "1.0.0"
    # the twin: an explicit version is taken as given
    assert cat.rollback_target(rig.session.candidate.board_id, "1.1.1", listing) == "1.1.1"


# --- the lease ----------------------------------------------------------------------------------


def _behind_hub(rig, lease):
    rig.session.hub = Hub()
    rig.svc.leases = Leases(lease)


@pytest.mark.parametrize("lease, holder", [(None, "nobody"), (ALICE, "alice@lab-pc-07")])
def test_an_install_without_the_lease_is_refused_before_the_board_is_touched(rig, world, lease,
                                                                           holder):
    cat = HarnessCatalog(rig.svc)
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    _behind_hub(rig, lease)
    before = rig.vb.sd.snapshot()
    with pytest.raises(HeldError) as exc:
        cat.install(rig.session, plan, plan.approve(), verified)
    assert exc.value.holder == holder and "lease holder only" in exc.value.message
    # the `update harness` path (the executor) refuses too: the rail is in services/update
    with pytest.raises(HeldError):
        rig.svc.install_harness(rig.session, plan, plan.approve(), verified)
    assert rig.vb.sd.snapshot() == before and rig.vb.reboots == 0
    rows_ = rows(listing_for(rig, world, ("stable",)))
    assert "hub-lease" in rows_["1.1.1"]["needs"] and "hub-lease" not in rows_["1.1.0"]["needs"]


def test_negative_twin_the_lease_holder_installs_and_an_overlay_only_plan_needs_no_lease(rig,
                                                                                       world):
    cat = HarnessCatalog(rig.svc)
    _behind_hub(rig, None)
    ovl, verified = cat.plan(rig.session, "1.1.0", channel="stable", source=world.srv.source())
    assert ovl.mode == "overlays"
    assert cat.install(rig.session, ovl, ovl.approve(), verified).result == "stored"
    rig.svc.leases = Leases(MINE)
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    assert cat.install(rig.session, plan, plan.approve(), verified).result == "installed"


# FIX-PACK-4: the one lease rule. `mine` is by principal: another session of your own hub name
# (a soak, a runner, a second Harness Manager) holds it too. Only `here` (this Harness Manager
# has the token) may install; the catalogue's lease says which, and the app's Update line
# reads `here`.
ELSEWHERE = {"holder": "david@mapstone-dev", "mine": True, "here": False}
HERE = {"holder": "david@mapstone-dev", "mine": True, "here": True}


def test_a_lease_your_other_session_holds_does_not_install_here(rig, world):
    cat = HarnessCatalog(rig.svc)
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    _behind_hub(rig, ELSEWHERE)
    before = rig.vb.sd.snapshot()
    st = rig.svc.lease_state(rig.session)
    assert (st["mine"], st["here"]) == (True, False)
    assert st["reason"] == ("david@mapstone-dev holds the lease on mps3_01_pl in another session, "
                            "not this Harness Manager")
    with pytest.raises(HeldError) as exc:
        cat.install(rig.session, plan, plan.approve(), verified)
    assert "lease holder only" in exc.value.message and "another session" in exc.value.message
    assert "session that holds the lease" in exc.value.hint
    assert rig.vb.sd.snapshot() == before and rig.vb.reboots == 0
    listing = listing_for(rig, world, ("stable",))
    assert listing.board["lease"]["here"] is False
    assert "hub-lease" in rows(listing)["1.1.1"]["needs"]
    assert any("another session" in w for w in listing.warnings)


def test_negative_twin_the_lease_held_here_installs(rig, world):
    cat = HarnessCatalog(rig.svc)
    _behind_hub(rig, HERE)
    listing = listing_for(rig, world, ("stable",))
    assert listing.board["lease"]["here"] is True and listing.board["lease"]["reason"] == ""
    assert "hub-lease" not in rows(listing)["1.1.1"]["needs"]
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    assert cat.install(rig.session, plan, plan.approve(), verified).result == "installed"


def test_a_board_with_no_hub_needs_no_lease_and_nothing_asks_a_hub(rig, world, monkeypatch):
    # No hub, no lease: the gate must not build a LeaseService, call a hub or wait, on any
    # path (list, install with its reboot, the rollback's gate).
    import harness_manager.services.lease as lease_mod

    def no_lease_service(*_a, **_k):
        raise AssertionError("a board with no hub made the lease gate build a LeaseService")

    monkeypatch.setattr(lease_mod, "LeaseService", no_lease_service)
    assert rig.svc.lease_state(rig.session) == {"required": False, "mine": False, "here": False,
                                                "holder": "", "target": "", "reason": ""}
    assert rig.svc.check_lease(rig.session)["required"] is False
    listing = listing_for(rig, world, ("stable",))
    assert listing.board["lease"]["required"] is False
    assert not any("hub-lease" in r["needs"] for r in listing.as_dict()["releases"])
    cat = HarnessCatalog(rig.svc)
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    assert cat.install(rig.session, plan, plan.approve(), verified).result == "installed"
    # the rollback's gate is the same call (executor.rollback's first step)
    assert rig.svc.installer("mps3").lease_check(rig.session, "roll the harness back") == \
        {"required": False, "mine": False, "here": False, "holder": "", "target": "", "reason": ""}
    assert rig.svc.leases is None                     # never built, never asked


def test_negative_twin_a_hub_board_asks_the_lease_view_once_per_check(rig, world):
    _behind_hub(rig, MINE)
    leases = rig.svc.leases
    assert rig.svc.check_lease(rig.session)["mine"] is True
    assert leases.views == 1
    cat = HarnessCatalog(rig.svc)
    plan, verified = cat.plan(rig.session, "1.1.1", channel="stable", source=world.srv.source())
    cat.install(rig.session, plan, plan.approve(), verified)
    assert leases.views == 3                          # the catalogue's check + the executor's


# --- events -------------------------------------------------------------------------------------


def test_the_catalogue_publishes_its_events(rig, world):
    seen: list = []
    rig.engine.bus.subscribe("harness.*", lambda ev: seen.append((ev.topic, ev.data)))
    cat = HarnessCatalog(rig.svc)
    listing_for(rig, world, ("stable",))
    bid = rig.session.candidate.board_id
    cat.pin(bid, "1.1.1")
    plan, verified = cat.plan(rig.session, None, channel="stable", source=world.srv.source())
    cat.install(rig.session, plan, plan.approve(), verified)
    cat.unpin(bid)
    topics = [t for t, _ in seen]
    assert topics == ["harness.catalog", "harness.pinned", "harness.installing",
                      "harness.installed", "harness.pinned"]
    data = dict(seen)
    assert data["harness.catalog"]["releases"] == 3 and data["harness.catalog"]["running"] == "1.1.0"
    assert data["harness.installing"]["from"] == "1.1.0"
    assert data["harness.installed"]["result"] == "installed" and data["harness.installed"]["ok"]
    assert seen[1][1]["version"] == "1.1.1" and seen[-1][1] == {"version": "", "previous": "1.1.1",
                                                                "by": "user"}
