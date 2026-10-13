"""L5b (HM 1.2): the MCC through the harness (net-protocol v0.20 ``mcc`` verb, ``reset
mcc_warm``) and the ONE route-selection policy. In-process only: a stub board models the
contract's replies on a fake clock (no sockets, no waiting). Each check has a negative twin.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pyverify import mint4 as M4

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Link, LinkKind
from harness_manager.daemon import mcc_route
from harness_manager_mps3 import harness_mcc as HM

SID = "0xAAAA0001"


class Board:
    """A mint-4 harness as the contract describes it, on a fake clock."""

    def __init__(self, features=("mccif", "mcc_local"), *, scc=True, loopback=True,
                 plan="ok") -> None:
        self.features = tuple(features)
        self.scc, self.loopback = scc, loopback
        self.plan = plan
        self.t = 0.0
        self.sent: list[dict[str, Any]] = []
        self.shell_id = SID
        self.up_since = -500.0
        self.token = ""
        self.reboot_at: float | None = None
        self.warm_at: float | None = None
        self.locked = False

    # -- the clock --
    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    # -- the board's state --
    def down(self) -> bool:
        if self.reboot_at is not None and self.plan in ("ok", "never_back", "other_id"):
            if self.t >= self.reboot_at + 4:
                if self.plan == "never_back" or self.t < self.reboot_at + 90:
                    return True
                self.up_since = self.reboot_at + 90
                if self.plan == "other_id":
                    self.shell_id = "0xBBBB0002"
                self.reboot_at = None
                self.token_state = "done"
        if self.warm_at is not None:
            if self.t >= self.warm_at + 1.5:
                if self.t < self.warm_at + 12:
                    return True
                self.up_since = self.warm_at + 12
                self.warm_at = None
        return False

    token_state = ""

    def last_reboot(self):
        if not self.token:
            return None
        state = self.token_state or ("failed" if self.plan == "failed" else
                                     "sent" if self.plan == "pending" else "pending")
        return {"token": self.token, "state": state}

    # -- the wire --
    def request(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(dict(msg))
        if self.down():
            raise UnreachableError("down")
        op, act = msg.get("op"), msg.get("act")
        if op == "reset" and msg.get("target") == "mcc_warm":
            if "mccif" not in self.features:
                return {"ok": False, "err": "reset target 'mcc_warm' not supported",
                        "code": "not_supported"}
            if self.locked:
                return {"ok": False, "err": "reset locked: board claimed", "code": "locked"}
            self.warm_at = self.t
            return {"ok": True, "op": "reset", "target": "mcc_warm", "in_ms": 1500}
        assert op == "mcc", msg
        if act == "status":
            return {"ok": True, "op": "mcc", "act": "status", "route": "none",
                    "scc": {"brev": 2} if self.scc else None,
                    "loopback": ({"tty": "/dev/ttyUSB0", "prompt": True, "busy": False,
                                  "last_reboot": self.last_reboot()} if self.loopback else None)}
        if self.locked and act in ("osc_set", "reboot"):
            return {"ok": False, "err": f"mcc {act} locked: board claimed", "code": "locked"}
        if act == "reboot":
            if not self.loopback:
                return {"ok": False, "err": "mcc reboot: no loopback route", "code": "no_route"}
            self.token, self.reboot_at = msg["token"], self.t
            return {"ok": True, "op": "mcc", "act": "reboot", "route": "loopback",
                    "accepted": True, "token": msg["token"], "eta_min_s": 100, "eta_max_s": 180}
        if act == "osc_set":
            return {"ok": True, "op": "mcc", "act": "osc_set", "route": "scc", "dev": msg["dev"],
                    "hz_set": msg["hz"], "measured_hz": msg["hz"]}
        if act == "osc":
            return {"ok": True, "op": "mcc", "act": "osc", "route": "scc", "dev": msg["dev"],
                    "hz": 50_000_000, "ms": 3}
        return {"ok": True, "op": "mcc", "act": act, "route": "scc"}

    # -- what HarnessMcc is handed --
    def session(self, claim=None) -> Any:
        client = SimpleNamespace(
            _request=self.request,
            reset_mcc_warm=lambda: self.request({"op": "reset", "target": "mcc_warm"}),
            stats=lambda: self._stats())
        shell = SimpleNamespace(call_raw=lambda fn: fn(client, None), call=lambda fn: fn(client),
                                host="127.0.0.1", port=0, gate_key="")
        ident = SimpleNamespace(features=self.features, shell_id=SID)
        return SimpleNamespace(shell=shell, claim=claim, identity=lambda: ident,
                               candidate=SimpleNamespace(links=(), identity=ident,
                                                         board_id="mps3@stub"))

    def _stats(self):
        if self.down():
            raise UnreachableError("down")
        return SimpleNamespace(ok=True, up_ms=int((self.t - self.up_since) * 1000))

    def probe(self):
        return None if self.down() else self.shell_id

    def mcc(self, **kw) -> HM.HarnessMcc:
        sess = self.session()
        hm = HM.HarnessMcc(sess, clock=self.clock, sleep=self.sleep, poll_s=3.0,
                           probe=self.probe)
        sess.harness_mcc = hm
        hm.note_features(self.features)
        return hm


def acts_sent(b: Board) -> list[str]:
    return [m.get("act") or m.get("target") for m in b.sent]


# --- reading the status: v0.20 and the older v0.18 shape -----------------------------------------


def test_status_v020_routes_are_the_non_null_objects():
    b = Board(scc=True, loopback=False)
    st = b.mcc().status(refresh=True)
    assert st.routes == ("scc",) and st.serves("osc") and not st.serves("reboot")
    both = Board().mcc().status(refresh=True)
    assert set(both.routes) == {"scc", "loopback"} and both.serves("reboot")
    none = Board(scc=False, loopback=False).mcc().status(refresh=True)
    assert none.routes == () and none.route == "none" and not none.serves("osc")


def test_status_v018_old_image_flat_route_is_still_read():
    st = HM.parse_status({"ok": True, "op": "mcc", "act": "status", "route": "loopback",
                          "scc": False, "loopback": True})
    assert st.routes == ("loopback",) and st.serves("reboot")
    assert HM.parse_status({"ok": True, "route": "none"}).routes == ()


# --- ONE selection policy: the route matrix -------------------------------------------------------


@pytest.mark.parametrize(("serves", "usb", "hub", "want"), [
    (True, True, True, "harness"), (True, False, False, "harness"),
    (False, True, True, "usb"), (False, False, True, "hub"), (False, False, False, "none"),
])
def test_prefer_order_harness_then_usb_then_hub(serves, usb, hub, want):
    assert mcc_route.prefer("reboot", harness_serves=serves, has_usb=usb, has_hub=hub) == want


class Fallback:
    """The Debug USB / hub controller the harness route wraps."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.observer = None

    def reboot(self, progress=None, wait_s=None):
        self.calls.append("reboot")
        return {"summary": "usb reboot"}

    def oscillators(self):
        self.calls.append("osc")
        return []

    def temperatures(self):
        self.calls.append("temp")
        return []

    def command(self, line, *, arm=False):
        self.calls.append(f"command {line}")
        return "ok"


def routed(features, *, scc, loopback, usb=True):
    b = Board(features, scc=scc, loopback=loopback)
    hm = b.mcc()
    sess = hm._session
    fb = Fallback() if usb else None
    return b, HM.RoutedController(sess, fb), fb


def test_matrix_mccif_only_reboot_falls_to_usb_but_osc_goes_through_the_harness():
    b, ctl, fb = routed(("mccif",), scc=True, loopback=False)
    ctl.reboot()
    assert fb.calls == ["reboot"]                    # scc cannot restart the board
    ctl.oscillators()
    assert fb.calls == ["reboot"] and "osc" in acts_sent(b)


def test_matrix_mcc_local_only_reboot_goes_through_the_harness_with_no_cable():
    b, ctl, _ = routed(("mcc_local",), scc=False, loopback=True, usb=False)
    ev = ctl.reboot()
    assert ev["via"] == "mcc" and "reboot" in acts_sent(b)


def test_matrix_both_features_prefer_the_harness_over_usb():
    b, ctl, fb = routed(("mccif", "mcc_local"), scc=True, loopback=True)
    ctl.reboot()
    assert fb.calls == [] and "reboot" in acts_sent(b)


def test_matrix_neither_feature_the_usb_controller_is_used_and_the_board_never_asked():
    b = Board(())
    sess = b.session()
    fb = Fallback()
    ctl = HM.RoutedController(sess, fb)
    ctl.reboot()
    assert fb.calls == ["reboot"] and b.sent == []
    with pytest.raises(UnavailableError):            # ... and with no USB either: nowhere
        HM.RoutedController(sess, None).reboot()
    assert b.sent == []


def test_route_none_reboot_uses_usb_if_there_is_one():
    b, ctl, fb = routed(("mccif", "mcc_local"), scc=False, loopback=False)
    ctl.reboot()
    assert fb.calls == ["reboot"]


def test_make_controller_adapter_leaves_an_old_board_alone_and_wraps_a_new_one():
    from harness_manager_mps3 import mcc

    usb = Link(LinkKind.USB_SERIAL, "/dev/ttyUSB-nonexistent", "FT4232H MCC if00")

    def sess(features):
        ident = SimpleNamespace(features=features)
        return SimpleNamespace(candidate=SimpleNamespace(links=(usb,), identity=ident), shell=None)

    old = mcc.make_controller_adapter(sess(("stats",)))
    assert isinstance(old, mcc.Mps3Controller)
    new = mcc.make_controller_adapter(sess(("stats", "mccif")))
    assert isinstance(new, HM.RoutedController) and isinstance(new._fallback, mcc.Mps3Controller)


def test_of_session_prefers_the_harness_over_a_usb_cable_when_advertised():
    eth = Link(LinkKind.ETHERNET, "10.0.0.5:6900", "shell")
    usb = Link(LinkKind.USB_SERIAL, "/dev/ttyUSB0", "FT4232H MCC if00")
    s = SimpleNamespace(candidate=SimpleNamespace(links=(eth, usb)), controller=object())
    ident = SimpleNamespace(features=("mccif",))
    assert mcc_route.of_session(s, ident)[0] == "self"
    # twin: no feature, the cable is the route; and a harness with route none falls back to it
    assert mcc_route.of_session(s, SimpleNamespace(features=()))[0] == "pc"
    s.harness_mcc = SimpleNamespace(_last=(SimpleNamespace(route="none"), "", 0.0))
    assert mcc_route.of_session(s, ident)[0] == "pc"
    assert mcc_route.from_links((eth, usb), ("mcc_local",))[0] == "self"
    assert mcc_route.from_links((eth, usb), ())[0] == "pc"


# --- the two-phase reboot -----------------------------------------------------------------------


def test_reboot_succeeds_only_when_the_board_went_away_came_back_and_the_mcc_says_done():
    b = Board()
    phases: list[str] = []
    ev = b.mcc().reboot(lambda p, d, t: phases.append(p))
    assert phases == ["sent", "down", "up"]
    assert ev["last_reboot"] == {"token": b.token, "state": "done"}
    assert ev["shell_id_before"] == ev["shell_id_after"] == SID
    assert 8 <= len(b.token) <= 32 and int(b.token, 16) >= 0          # the caller's own token
    assert ev["down_after_s"] >= 4 and ev["up_after_s"] >= 90
    sent = [m for m in b.sent if m.get("act") == "reboot"]
    assert len(sent) == 1 and sent[0]["token"] == b.token


def test_reboot_tokens_are_random_per_request():
    tokens = set()
    for _ in range(3):
        b = Board()
        b.mcc().reboot()
        tokens.add(b.token)
    assert len(tokens) == 3


def test_reboot_failed_means_the_mcc_never_acknowledged_and_the_board_never_went_down():
    b = Board(plan="failed")
    with pytest.raises(ActionFailedError) as exc:
        b.mcc().reboot()
    assert "never acknowledged" in exc.value.message and "not restarted" in exc.value.message


def test_reboot_down_but_never_came_back_is_its_own_message():
    b = Board(plan="never_back")
    with pytest.raises(ActionFailedError) as exc:
        b.mcc().reboot(wait_s=200)
    assert "did not come back within 200 s" in exc.value.message
    assert "never acknowledged" not in exc.value.message


def test_reboot_never_went_down_and_never_confirmed_times_out_without_success():
    b = Board(plan="pending")
    with pytest.raises(ActionFailedError) as exc:
        b.mcc().reboot(wait_s=150)
    assert "did not restart within 150 s" in exc.value.message


def test_reboot_a_different_shell_id_is_not_a_clean_restart():
    b = Board(plan="other_id")
    with pytest.raises(ActionFailedError) as exc:
        b.mcc().reboot()
    assert "different harness" in exc.value.message


def test_reboot_needs_the_loopback_route_and_sends_nothing_without_it():
    b = Board(scc=True, loopback=False)
    with pytest.raises(UnavailableError):
        b.mcc().reboot()
    assert "reboot" not in acts_sent(b)


def test_reboot_when_the_harness_is_not_answering_sends_nothing():
    b = Board()
    hm = b.mcc()
    hm._probe_fn = lambda: None
    with pytest.raises(UnreachableError):
        hm.reboot()
    assert "reboot" not in acts_sent(b)


def test_a_claim_locked_reboot_without_ssh_says_so_and_does_not_pretend():
    b = Board()
    b.locked = True
    with pytest.raises(HarnessError) as exc:
        b.mcc().reboot()
    assert "SSH" in exc.value.message or "claim" in exc.value.message.lower()


# --- the deny list: a denied act is never sent ----------------------------------------------------


@pytest.mark.parametrize("act", sorted(M4.MCC_DENY_ACTS))
def test_a_denied_act_is_refused_before_the_wire(act):
    b = Board()
    with pytest.raises(RefusedError):
        b.mcc().act(act)
    assert b.sent == []


@pytest.mark.parametrize("dev", sorted(M4.MCC_OSC_PROTECTED))
def test_a_protected_oscillator_is_never_set(dev):
    b = Board()
    with pytest.raises(RefusedError):
        b.mcc().set_osc(dev, 40_000_000)
    assert b.sent == []


def test_probe_func_8_is_denied_and_the_allowed_ones_are_not():
    b = Board()
    hm = b.mcc()
    with pytest.raises(RefusedError):
        hm.act("probe", func=8)
    assert b.sent == []
    for func in sorted(M4.MCC_PROBE_FUNCS):
        hm.act("probe", func=func, dev=0)
    assert len(b.sent) == len(M4.MCC_PROBE_FUNCS)


def test_allowed_oscillator_is_set_armed_and_a_bad_value_is_a_usage_error():
    b = Board()
    hm = b.mcc()
    r = hm.set_osc(2, 40_000_000)
    assert r["hz_set"] == 40_000_000 and b.sent[-1]["arm"] is True
    with pytest.raises(UsageError):
        hm.set_osc(2, 5)
    with pytest.raises(UsageError):
        hm.set_osc(9, 40_000_000)


def test_the_policy_and_facts_are_the_vendored_tables_not_a_copy():
    facts = Board().mcc().facts()
    pol = facts["policy"]
    assert set(pol["deny_acts"]) == set(M4.MCC_DENY_ACTS)
    assert facts["osc_devices"] == sorted(M4.MCC_OSC_ALLOWED) == [2, 4, 5, 7]
    assert set(pol["osc_protected"]) == {str(d) for d in M4.MCC_OSC_PROTECTED}
    assert set(pol["probe_funcs"]) == set(M4.MCC_PROBE_FUNCS)
    assert not set(facts["acts"]) & set(M4.MCC_DENY_ACTS)


def test_facts_say_in_plain_words_why_there_is_no_power_cycle():
    f = Board(scc=True, loopback=False).mcc().facts()
    assert not f["reboot"]["ok"] and "USB loop cable" in f["reboot"]["reason"]
    assert f["warm_reset"]["ok"]
    g = Board(("mcc_local",), scc=False, loopback=True).mcc().facts()
    assert g["reboot"]["ok"] and not g["warm_reset"]["ok"]
    assert Board(()).mcc().facts() is None                      # an old board: nothing offered


# --- warm reset ----------------------------------------------------------------------------------


def test_warm_reset_is_confirmed_by_up_ms_starting_over():
    b = Board()
    phases: list[str] = []
    ev = b.mcc().warm_reset(lambda p, d, t: phases.append(p))
    assert phases == ["sent", "down", "up"]
    assert ev["up_ms_after"] < ev["up_ms_before"] and ev["in_ms"] == 1500
    assert [m for m in b.sent if m.get("op") == "reset"] == [
        {"op": "reset", "target": "mcc_warm"}]


def test_warm_reset_with_no_restart_fails_and_ddr_is_never_sent():
    b = Board()
    b.warm_at = None
    hm = b.mcc()
    real = b.request

    def no_op(msg):                                  # accepted, but nothing happens
        if msg.get("target") == "mcc_warm":
            b.sent.append(dict(msg))
            return {"ok": True, "op": "reset", "target": "mcc_warm", "in_ms": 1500}
        return real(msg)

    hm._session.shell.call_raw = lambda fn: fn(SimpleNamespace(
        _request=no_op, reset_mcc_warm=lambda: no_op({"op": "reset", "target": "mcc_warm"}),
        stats=b._stats), None)
    with pytest.raises(ActionFailedError) as exc:
        hm.warm_reset(wait_s=30)
    assert "no sign that the board restarted" in exc.value.message
    assert all(m.get("target") != "ddr" for m in b.sent)


def test_warm_reset_needs_mccif_and_an_old_board_is_untouched():
    b = Board(("mcc_local",), scc=False)
    with pytest.raises(UnavailableError):
        b.mcc().warm_reset()
    assert b.sent == []


# --- the daemon routes (stubbed daemon: no engine, no sockets) -----------------------------------


@pytest.fixture
def api(monkeypatch):
    import contextlib
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi import APIRouter, FastAPI
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient

    from harness_manager.daemon import power_api
    from harness_manager.daemon.app import RouteContext
    from harness_manager.services import design_check, reset_guard

    monkeypatch.setattr(reset_guard, "guarded", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(design_check, "after_cold_boot", lambda *a, **k: None)

    jobs: list[dict[str, Any]] = []

    class Jobs:
        def submit(self, kind, bid, fn):
            seen: list[str] = []
            try:
                result = fn(lambda p, d, t: seen.append(p))
                jobs.append({"kind": kind, "result": result, "phases": seen, "error": None})
            except HarnessError as exc:
                jobs.append({"kind": kind, "result": None, "phases": seen, "error": exc})
            return SimpleNamespace(id=f"j{len(jobs)}")

    holder = SimpleNamespace(board=None)
    daemon = SimpleNamespace(
        gates=SimpleNamespace(op=lambda bid: contextlib.nullcontext()), jobs=Jobs(),
        bus=SimpleNamespace(publish=lambda e: None), engine=None, leases=None)
    router = APIRouter()
    ctx = RouteContext(daemon=daemon, api=router, wsr=None, board=lambda bid: holder.board,
                       require=lambda *a: None,
                       accepted=lambda job: JSONResponse({"ok": True, "job": job.id}, 202))
    power_api.register(ctx)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=True)
    return client, holder, jobs


def with_board(holder, board: Board):
    sess = board.session()
    sess.hub = None
    sess.power = None
    sess.harness_mcc = board.mcc()
    sess.harness_mcc._session = sess
    holder.board = sess
    return sess


def test_api_power_read_carries_the_mcc_facts_and_null_on_an_old_board(api):
    client, holder, _ = api
    with_board(holder, Board())
    for path in ("power", "mcc"):
        mcc = client.get(f"/boards/mps3@stub/{path}").json()["mcc"]
        assert mcc["reboot"]["ok"] is True and mcc["osc_devices"] == [2, 4, 5, 7]
        assert mcc["warm_reset"]["ok"] is True
    with_board(holder, Board(()))
    assert client.get("/boards/mps3@stub/power").json()["mcc"] is None
    assert client.get("/boards/mps3@stub/mcc").json()["mcc"] is None


def test_api_power_cycle_via_mcc_runs_the_two_phase_job(api):
    client, holder, jobs = api
    b = Board()
    with_board(holder, b)
    r = client.post("/boards/mps3@stub/power/cycle", json={"via": "mcc"})
    assert r.status_code == 202
    (job,) = jobs
    assert job["kind"] == "power_cycle" and job["error"] is None
    assert job["phases"] == ["sent", "down", "up"]
    assert job["result"]["last_reboot"]["state"] == "done"


def test_api_power_cycle_via_mcc_failure_is_the_jobs_error_in_words(api):
    client, holder, jobs = api
    with_board(holder, Board(plan="failed"))
    client.post("/boards/mps3@stub/power/cycle", json={"via": "mcc"})
    assert "never acknowledged" in jobs[0]["error"].message


def test_api_warm_reset_job_and_refusals(api):
    client, holder, jobs = api
    with_board(holder, Board())
    assert client.post("/boards/mps3@stub/power/cycle", json={"via": "mcc_warm"}).status_code == 202
    assert jobs[0]["kind"] == "reboot" and jobs[0]["result"]["via"] == "mcc_warm"
    # twin: an image with mcc_local only has no warm reset; nothing is started
    b = Board(("mcc_local",), scc=False)
    with_board(holder, b)
    with pytest.raises(UnavailableError):
        client.post("/boards/mps3@stub/power/cycle", json={"via": "mcc_warm"})
    assert len(jobs) == 1 and b.sent[-1].get("act") == "status"
    with pytest.raises(UsageError):
        client.post("/boards/mps3@stub/power/cycle", json={"via": "sideways"})


def test_api_osc_set_is_armed_and_limited_to_the_allowed_devices(api):
    client, holder, _ = api
    b = Board()
    with_board(holder, b)
    ok = client.post("/boards/mps3@stub/mcc/osc", json={"dev": 2, "hz": 40_000_000, "arm": True})
    assert ok.json()["hz_set"] == 40_000_000
    n = len(b.sent)
    with pytest.raises(RefusedError):
        client.post("/boards/mps3@stub/mcc/osc", json={"dev": 1, "hz": 40_000_000, "arm": True})
    with pytest.raises(UsageError):
        client.post("/boards/mps3@stub/mcc/osc", json={"dev": 2, "hz": 40_000_000})
    assert len(b.sent) == n                                     # neither was sent


# --- the Recover page (static: no browser on this host) ------------------------------------------


def _power_js() -> str:
    from pathlib import Path

    import harness_manager

    return (Path(harness_manager.__file__).parent / "web/static/js/sections/power.js").read_text()


def test_power_js_shows_evidence_in_words_not_json():
    js = _power_js()
    assert "JSON.stringify" not in js
    assert "evidenceWords(ev.up_evidence)" in js


def test_power_js_has_both_new_rungs_with_the_required_words():
    js = _power_js()
    assert "Power-cycle the board (through its MCC)" in js
    assert "Warm reset (like pressing the board's reset button)" in js
    assert "waiting for the board to come back (about 2-3 minutes)" in js
    assert '{ via: "mcc" }' in js and '{ via: "mcc_warm" }' in js
    # both are armed and have their own arm text; the ladder keeps the old rungs for old boards
    assert js.count("mcc_reboot:") == 1 and js.count("warm_reset:") == 1
    assert 'k: "reboot", key: "reboot"' in js and "if (!mcc) return numbered(steps)" in js
