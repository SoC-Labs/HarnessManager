"""T8: the REST hub client against the fake fpgahub 0.3.0. Every check has a negative twin.

Only 127.0.0.1 is reached (``tests/fakes/t8_hub_rest.FakeFpgahub``); nothing here can
touch the real hub.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.transports import hub_rest
from harness_manager.transports.hub_rest import (
    Credential,
    RestHubClient,
    parse_rest_table,
    resolve_credential,
    transport_for,
)
from tests.fakes.t8_hub_rest import FakeFpgahub, client_for, wait_until

TARGET = "mps3_01_pl"


@pytest.fixture(autouse=True)
def _no_fpgahub_login(tmp_path, monkeypatch):
    """Never read the developer's real fpgahub login store or token."""
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-fpgahub-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)


@pytest.fixture
def hub():
    with FakeFpgahub() as h:
        yield h


class _Stop(Exception):
    pass


@pytest.fixture
def bg():
    """Queue a client in the background; every one is cancelled and joined at teardown."""
    stops: list[tuple[threading.Event, threading.Thread]] = []

    def queue(client, **kw):
        stop = threading.Event()

        def sleep(s):
            if stop.wait(s):
                raise _Stop()

        def run():
            try:
                client.lease_acquire("q", ttl=600, sleep=sleep, **kw)
            except Exception:  # noqa: BLE001 - stopped, cancelled or the hub closed
                pass

        t = threading.Thread(target=run, daemon=True)
        t.start()
        stops.append((stop, t))
        return stop

    yield queue
    for stop, t in stops:
        stop.set()
        t.join(5)


@pytest.fixture
def people(hub):
    toks = {"alice": hub.add_token("alice"), "bob": hub.add_token("bob"),
            "carol": hub.add_token("carol", "read"), "david": hub.add_token("david", "admin")}
    return toks, {n: client_for(hub, t) for n, t in toks.items()}


# --- configuration and the transport choice --------------------------------------------------


def test_url_means_rest_host_means_ssh_and_both_prefer_rest():
    assert transport_for({"url": "https://hub:7246"}) == "rest"
    assert transport_for({"host": "mapstone-dev"}) == "ssh"
    assert transport_for({"url": "https://hub:7246", "host": "mapstone-dev"}) == "rest"


def test_a_table_with_neither_chooses_no_transport():
    assert transport_for({}) == ""
    assert transport_for({"target": "mps3_01_pl"}) == ""
    assert parse_rest_table({"host": "mapstone-dev"}) is None


def test_the_rest_table_parses_and_keeps_host_as_the_ssh_fallback():
    cfg = parse_rest_table({"url": "https://mapstone-dev.ecs.soton.ac.uk:7246/api/v1/",
                            "host": "mapstone-dev", "target": "mps3_01_pl",
                            "token_file": "~/t", "direct": "never", "board": "mps3_01"})
    assert cfg.url == "https://mapstone-dev.ecs.soton.ac.uk:7246"
    assert (cfg.host, cfg.port, cfg.ssh_host, cfg.direct, cfg.board) == (
        "mapstone-dev.ecs.soton.ac.uk", 7246, "mapstone-dev", "never", "mps3_01")


@pytest.mark.parametrize("table, word", [
    ({"url": "http://mapstone-dev:7246"}, "plain http"),
    ({"url": "https://u:p@hub:7246"}, "credentials"),
    ({"url": "https://hub:7246/api/v2"}, "path"),
    ({"url": "https://hub:7246?token=x"}, "query"),
    ({"url": "ftp://hub"}, "http(s)"),
    ({"url": "https://hub", "insecure": "yes"}, "insecure"),
    ({"url": "https://hub", "direct": "sometimes"}, "direct"),
    ({"url": "https://hub", "cert_file": "c.pem"}, "go together"),
    ({"url": "https://hub", "target": "mps3 01"}, "target"),
    ({"url": "https://hub", "timeout_s": 0}, "timeout_s"),
])
def test_a_bad_rest_table_is_a_usage_error_naming_the_key(table, word):
    with pytest.raises(UsageError) as ei:
        parse_rest_table(table)
    assert word in ei.value.message


def test_plain_http_is_allowed_to_loopback_only():
    assert parse_rest_table({"url": "http://127.0.0.1:9"}).url == "http://127.0.0.1:9"
    with pytest.raises(UsageError):
        parse_rest_table({"url": "http://10.0.0.1:9"})


def test_client_for_builds_rest_for_a_config_with_rest_and_ssh_otherwise():
    class Cfg:
        rest = parse_rest_table({"url": "https://hub:7246"})

    class SshOnly:
        rest = None

    assert isinstance(hub_rest.client_for(Cfg()), RestHubClient)
    assert hub_rest.client_for(SshOnly(), ssh_factory=lambda: "ssh-client") == "ssh-client"
    with pytest.raises(UsageError):
        hub_rest.client_for(SshOnly())


# --- the credential ---------------------------------------------------------------------------


def test_the_token_file_wins(tmp_path, monkeypatch):
    tf = tmp_path / "hub.token"
    tf.write_text("sekrit-token\n")
    tf.chmod(0o600)
    monkeypatch.setenv("FPGAHUB_TOKEN", "from-env")
    cred = resolve_credential(parse_rest_table({"url": "https://hub:7246",
                                                "token_file": str(tf)}))
    assert cred.header() == {"Authorization": "Bearer sekrit-token"}
    assert "sekrit" not in repr(cred) and "sekrit" not in str(cred)


def test_a_missing_or_multi_line_token_file_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError):
        resolve_credential(parse_rest_table({"url": "https://hub",
                                             "token_file": str(tmp_path / "none")}))
    bad = tmp_path / "bad"
    bad.write_text("two tokens\n")
    with pytest.raises(UsageError):
        resolve_credential(parse_rest_table({"url": "https://hub", "token_file": str(bad)}))


def test_the_fpgahub_login_store_is_used_for_the_hub_it_names(tmp_path):
    store = tmp_path / "config.toml"
    store.write_text('[client]\naddr = "hub.example:7246"\ntoken = "login-token"\n')
    cfg = parse_rest_table({"url": "https://hub.example:7246"})
    cred = resolve_credential(cfg, env={}, store=store)
    assert cred.present and cred.header()["Authorization"] == "Bearer login-token"
    assert "fpgahub login" in cred.source


def test_the_login_store_token_never_goes_to_another_hub(tmp_path):
    store = tmp_path / "config.toml"
    store.write_text('[client]\naddr = "hub.example"\ntoken = "login-token"\n')
    # no port in the store means fpgahub's 7245; this config is 7246
    cred = resolve_credential(parse_rest_table({"url": "https://hub.example:7246"}), env={},
                              store=store)
    assert not cred.present
    cred = resolve_credential(parse_rest_table({"url": "https://other:7245"}), env={},
                              store=store)
    assert not cred.present


def test_fpgahub_token_env_applies_when_no_addr_or_the_same_addr():
    cfg = parse_rest_table({"url": "https://hub.example:7246"})
    assert resolve_credential(cfg, env={"FPGAHUB_TOKEN": "t"}, store=Path("/nonexistent")).present
    assert resolve_credential(cfg, env={"FPGAHUB_TOKEN": "t", "FPGAHUB_ADDR": "hub.example:7246"},
                              store=Path("/nonexistent")).present


def test_fpgahub_token_env_for_another_hub_is_not_sent():
    cfg = parse_rest_table({"url": "https://hub.example:7246"})
    cred = resolve_credential(cfg, env={"FPGAHUB_TOKEN": "t", "FPGAHUB_ADDR": "elsewhere:7246"},
                              store=Path("/nonexistent"))
    assert not cred.present


# --- identity -----------------------------------------------------------------------------------


def test_principal_and_board_id_come_from_the_hub(people):
    _, c = people
    assert c["alice"].principal() == "alice@mapstone-dev"
    assert c["alice"].board_id() == "mps3_01"
    assert c["david"].can_revoke() == (True, "")


def test_a_write_token_cannot_revoke_and_says_why(people):
    _, c = people
    ok, why = c["alice"].can_revoke()
    assert not ok and "admin" in why and "write" in why


def test_board_id_from_config_skips_the_hub(hub, people):
    toks, _ = people
    c = client_for(hub, toks["alice"], board="mps3_99")
    before = len(hub.requests)
    assert c.board_id() == "mps3_99"
    assert len(hub.requests) == before


# --- auth failures ------------------------------------------------------------------------------


def test_no_token_is_401_unreachable_with_the_login_hint(hub):
    c = client_for(hub, None)
    with pytest.raises(UnreachableError) as ei:
        c.lease_show()
    assert ei.value.code == ExitCode.UNREACHABLE and "401" in ei.value.message
    assert "token_file" in ei.value.hint and "fpgahub login" in ei.value.hint


def test_a_valid_token_is_not_401(people):
    _, c = people
    assert c["carol"].lease_show().held is False


def test_a_bad_token_is_401_and_the_token_is_never_in_the_error(hub):
    c = client_for(hub, "not-a-real-token-xyz")
    with pytest.raises(UnreachableError) as ei:
        c.whoami()
    assert "not-a-real-token-xyz" not in str(ei.value)


def test_a_read_token_cannot_acquire_403_refused_naming_both_roles(people):
    _, c = people
    with pytest.raises(RefusedError) as ei:
        c["carol"].lease_acquire("x", ttl=600)
    assert "write" in ei.value.message and "read" in ei.value.message
    assert ei.value.code == ExitCode.REFUSED


def test_a_write_token_can_acquire(people):
    _, c = people
    lease, expires = c["alice"].lease_acquire("x", ttl=600)
    assert lease.holder == "alice@mapstone-dev" and lease.target == TARGET and expires


def test_a_write_token_revoke_is_403_refused_admin_required(people):
    _, c = people
    c["bob"].lease_acquire("x", ttl=600)
    with pytest.raises(RefusedError) as ei:
        c["alice"].lease_revoke("force")
    assert "admin" in ei.value.message
    assert c["alice"].lease_status().holder == "bob@mapstone-dev"      # nothing was kicked


def test_an_admin_token_revoke_kicks_and_promotes_the_head(people):
    _, c = people
    c["bob"].lease_acquire("x", ttl=600)
    t = threading.Thread(target=c["alice"].lease_acquire, args=("y",), kwargs={"ttl": 600})
    t.start()
    wait_until(lambda: c["alice"].lease_status().queue)
    out = c["david"].lease_revoke("force-released by alice via Harness Manager")
    assert out == {"revoked": [TARGET], "by": "token:david", "board": "mps3_01"}
    t.join(5)
    assert c["alice"].lease_status().holder == "alice@mapstone-dev"


# --- every lease verb ------------------------------------------------------------------------------


def test_lease_show_reports_the_holder_and_expiry(people):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    view = c["bob"].lease_show()
    assert (view.held, view.holder, view.user) == (True, "alice@mapstone-dev", "alice")
    assert view.expires_at.endswith("Z") and "held by alice@mapstone-dev" in view.raw


def test_lease_show_on_a_free_board_is_not_held(people):
    _, c = people
    view = c["bob"].lease_show()
    assert not view.held and view.raw == "not leased" and view.holder == ""


def test_acquire_twice_as_the_same_principal_keeps_the_token(people):
    _, c = people
    a, _ = c["alice"].lease_acquire("x", ttl=600)
    b, _ = c["alice"].lease_acquire("other-holder-name", ttl=600)
    assert a.token == b.token


def test_acquire_waits_in_the_queue_then_is_granted_a_new_token(people):
    _, c = people
    first, _ = c["alice"].lease_acquire("x", ttl=600)
    got, logs = {}, []
    t = threading.Thread(target=lambda: got.update(r=c["bob"].lease_acquire(
        "y", ttl=600, log_fn=logs.append)))
    t.start()
    wait_until(lambda: any("queued at position 1" in m for m in logs))
    assert "r" not in got
    c["alice"].lease_release(first.token, "x")
    t.join(5)
    lease, _ = got["r"]
    assert lease.holder == "bob@mapstone-dev" and lease.token != first.token


def test_a_cancelled_acquire_stops_at_the_next_slice(people):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    cancel = threading.Event()

    class Cancelled(Exception):
        pass

    def sleep(s):
        if cancel.wait(s):
            raise Cancelled()

    err = {}

    def run():
        try:
            c["bob"].lease_acquire("y", ttl=600, sleep=sleep)
        except Cancelled:
            err["cancelled"] = True

    t = threading.Thread(target=run)
    t.start()
    wait_until(lambda: c["alice"].lease_status().queue)
    t0 = time.monotonic()
    cancel.set()
    t.join(5)
    assert err == {"cancelled": True} and time.monotonic() - t0 < 2.0
    assert c["bob"].lease_cancel() is True          # the entry is the caller's to remove


def test_acquire_gives_up_at_its_timeout_and_removes_its_queue_entry(people):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    with pytest.raises(HeldError) as ei:
        c["bob"].lease_acquire("y", ttl=600, timeout_s=0.5)
    assert ei.value.holder == "alice@mapstone-dev" and "removed" in ei.value.message
    assert c["alice"].lease_status().queue == ()


def test_heartbeat_extends_and_returns_the_new_expiry(people):
    _, c = people
    lease, first = c["alice"].lease_acquire("x", ttl=600)
    time.sleep(0.01)
    assert c["alice"].lease_heartbeat(lease.token, "x") > first


def test_heartbeat_after_expiry_is_lease_lost_expired(people, hub):
    _, c = people
    lease, _ = c["alice"].lease_acquire("x", ttl=600)
    hub.expire(TARGET)
    with pytest.raises(HeldError) as ei:
        c["alice"].lease_heartbeat(lease.token, "x")
    assert ei.value.state == "expired"


def test_heartbeat_after_a_revoke_is_lease_lost_lost(people):
    _, c = people
    lease, _ = c["alice"].lease_acquire("x", ttl=600)
    c["david"].lease_revoke("kick")
    c["bob"].lease_acquire("y", ttl=600)
    with pytest.raises(HeldError) as ei:
        c["alice"].lease_heartbeat(lease.token, "x")
    assert ei.value.state == "lost"


def test_release_frees_the_board(people):
    _, c = people
    lease, _ = c["alice"].lease_acquire("x", ttl=600)
    c["alice"].lease_release(lease.token, "x")
    assert not c["bob"].lease_show().held


def test_release_of_nothing_is_lease_lost_not_silence(people):
    _, c = people
    lease, _ = c["alice"].lease_acquire("x", ttl=600)
    c["alice"].lease_release(lease.token, "x")
    with pytest.raises(HeldError) as ei:
        c["alice"].lease_release(lease.token, "x")
    assert ei.value.state == "expired"


def test_release_by_someone_else_is_refused_and_the_board_stays_held(people):
    _, c = people
    lease, _ = c["alice"].lease_acquire("x", ttl=600)
    with pytest.raises(HeldError):
        c["bob"].lease_release(lease.token, "x")
    assert c["bob"].lease_show().holder == "alice@mapstone-dev"


def test_lease_cancel_removes_only_our_queue_entry(people, bg):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    for who in ("bob", "david"):
        bg(c[who])
        wait_until(lambda w=who: any(q.holder.startswith(w) for q in
                                     c["alice"].lease_status().queue))
    wait_until(lambda: len(c["alice"].lease_status().queue) == 2)
    assert c["bob"].lease_cancel() is True
    assert [q.holder for q in c["alice"].lease_status().queue] == ["david@mapstone-dev"]


def test_lease_cancel_with_nothing_queued_is_false(people):
    _, c = people
    assert c["bob"].lease_cancel() is False


def test_lease_cancel_on_a_multi_target_board_goes_to_the_board_queue(hub, people, bg):
    toks, _ = people
    a = client_for(hub, toks["alice"], target="kr260_01_pl")
    b = client_for(hub, toks["bob"], target="kr260_01_pl")
    a.lease_acquire("x", ttl=600)
    bg(b)
    wait_until(lambda: a.lease_status().queue)
    assert b.lease_cancel() is True
    assert ("DELETE", "/api/v1/boards/kr260_01/queue") in [
        (r["method"], r["path"]) for r in hub.requests]
    assert b.board_id() == "kr260_01"


def test_lease_cancel_does_not_follow_a_409_that_is_not_board_required(hub, people):
    _, c = people
    hub.fail_next(409)
    with pytest.raises(HeldError):
        c["bob"].lease_cancel()


def test_lease_status_lists_the_queue_in_order(people, bg):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    bg(c["bob"])
    st = wait_until(lambda: (s := c["carol"].lease_status()).queue and s)
    assert st.held and st.holder == "alice@mapstone-dev"
    assert [(q.position, q.holder, q.user) for q in st.queue] == [(1, "bob@mapstone-dev", "bob")]


def test_lease_status_of_a_free_board_is_empty(people):
    _, c = people
    st = c["carol"].lease_status()
    assert (st.held, st.holder, st.queue) == (False, "", ())


def test_lease_history_merges_by_and_reason_the_rest_route_drops(people, hub):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    c["david"].lease_revoke("taken for the demo")
    plain = c["alice"].lease_history()
    revoked = [r for r in plain if r["event"] == "lease.revoked"]
    assert revoked and "by" not in revoked[0]                        # the hub's own history
    assert not any(r["event"] == "lease.admin_revoked" for r in plain)
    for ev in hub.events:                                            # what /events delivered
        if c["alice"].relevant(ev):
            c["alice"].observe(ev)
    merged = c["alice"].lease_history()
    admin_rows = [r for r in merged if r["event"] == "lease.admin_revoked"]
    assert admin_rows and admin_rows[0]["by"] == "token:david"
    assert "taken for the demo" in admin_rows[0]["reason"]
    assert admin_rows[0]["holder"] == "alice@mapstone-dev"
    assert "taken for the demo" in next(r for r in merged if r["event"] == "lease.revoked")["reason"]


def test_lease_history_without_the_stream_has_no_by(people):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    c["david"].lease_revoke("r")
    assert all(not r.get("by") for r in c["alice"].lease_history())


# --- shares ------------------------------------------------------------------------------------------


def test_share_start_then_list_and_find(people):
    _, c = people
    info = c["alice"].share_start("/dev/mps3_01_pl/tty_02", 115200)
    assert info.tty == "/dev/mps3_01_pl/tty_02" and info.port > 0 and info.host == "0.0.0.0"
    assert c["bob"].share_for("/dev/mps3_01_pl/tty_02").port == info.port
    assert c["bob"].share_for("/dev/mps3_01_pl/tty_03") is None


def test_negative_twin_a_share_on_the_mcc_tty_00_is_refused_before_the_hub(people, hub):
    from harness_manager.core.errors import RefusedError

    _, c = people
    with pytest.raises(RefusedError, match="never starts or uses an fpgahub share"):
        c["alice"].share_start("/dev/mps3_01_pl/tty_00", 115200)
    assert c["bob"].share_for("/dev/mps3_01_pl/tty_00") is None      # nothing was started


def test_share_start_on_a_board_someone_else_holds_is_held(people):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    with pytest.raises(HeldError) as ei:
        c["bob"].share_start("/dev/mps3_01_pl/tty_02")
    assert ei.value.holder == "alice@mapstone-dev"


def test_share_stop_is_refused_and_never_reaches_the_hub(people, hub):
    _, c = people
    with pytest.raises(RefusedError):
        c["alice"].share_stop()
    assert hub.share_stops == []


# --- request notes: the degraded mode ------------------------------------------------------------


def test_request_notes_are_degraded_waiters_become_messageless_requests(people, bg):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    bg(c["bob"])
    wait_until(lambda: c["alice"].lease_status().queue)
    notes = c["alice"].list_requests()
    assert [(n.id, n.by, n.user, n.host, n.message) for n in notes] == [
        ("q-bob_mapstone-dev", "bob@mapstone-dev", "bob", "mapstone-dev", "")]
    assert notes[0].deadline_at > notes[0].created_at
    assert c["alice"].notes_supported is False and "no note store" in c["alice"].notes_reason


def test_a_keep_answer_cannot_travel_over_rest_and_says_why(people):
    _, c = people
    note = hub_rest._AnswerNote("q-bob", "keep", 15, "5 more minutes", "2026-09-24T12:00:00Z")
    with pytest.raises(UnavailableError) as ei:
        c["alice"].put_answer(note)
    assert "keep" in ei.value.reason
    assert c["bob"].get_answer("q-bob") is None


def test_our_own_request_is_kept_locally_and_can_be_withdrawn(people):
    _, c = people
    note = hub_rest._RequestNote("r1", "bob@mapstone-dev", "bob", "mapstone-dev", "please",
                                 "2026-09-24T12:00:00+00:00", "2026-09-24T12:02:00+00:00")
    c["bob"].put_request(note)
    assert c["bob"].list_requests() == [note]
    c["bob"].delete_request("r1")
    assert c["bob"].list_requests() == []


def test_a_request_id_outside_the_safe_charset_is_refused(people):
    _, c = people
    note = hub_rest._RequestNote("../x", "b", "b", "h", "", "", "")
    with pytest.raises(UsageError):
        c["bob"].put_request(note)


# --- retries and timeouts -------------------------------------------------------------------------


def test_a_read_is_retried_after_a_5xx_and_a_dropped_connection(people, hub):
    _, c = people
    hub.fail_next(503, "drop")
    assert c["alice"].lease_show().held is False


def test_a_read_that_keeps_failing_is_unreachable(people, hub):
    _, c = people
    hub.fail_next(503, 503, 503)
    with pytest.raises(UnreachableError):
        c["alice"].lease_show()


def test_a_revoke_is_never_resent_after_the_request_may_have_left(people, hub):
    _, c = people
    c["alice"].lease_acquire("x", ttl=600)
    c["david"].board_id()
    hub.fail_next("drop")
    with pytest.raises(UnreachableError):
        c["david"].lease_revoke("kick")
    revokes = [r for r in hub.requests if r["path"].endswith("/lease/revoke")]
    assert len(revokes) == 1
    assert c["alice"].lease_status().holder == "alice@mapstone-dev"


def test_a_release_whose_first_answer_was_lost_is_done_on_the_retry(people, hub):
    _, c = people
    lease, _ = c["alice"].lease_acquire("x", ttl=600)
    orig = c["alice"]._http.request
    calls = {"n": 0}

    def flaky(method, path, **kw):
        calls["n"] += 1
        resp = orig(method, path, **kw)
        if method == "DELETE" and calls["n"] == 1:
            raise hub_rest._Sent(OSError("reset after send"))      # released, answer lost
        return resp

    c["alice"]._http.request = flaky
    c["alice"].lease_release(lease.token, "x")                      # no "nothing to release"
    assert not c["bob"].lease_show().held


def test_an_unreachable_hub_says_where_and_what_to_check():
    cfg = hub_rest.RestHubConfig(url="http://127.0.0.1:9", target=TARGET, timeout_s=1)
    c = RestHubClient(cfg, credential=Credential("t", "test"), retry_backoff_s=())
    with pytest.raises(UnreachableError) as ei:
        c.lease_show()
    assert "127.0.0.1:9" in ei.value.message and "campus" in ei.value.hint


def test_an_unknown_target_is_absent_with_the_target_hint(hub, people):
    toks, _ = people
    c = client_for(hub, toks["alice"], target="mps3_99_pl")
    with pytest.raises(AbsentError) as ei:
        c.lease_show()
    assert "hub.target" in ei.value.hint


# --- the token stays secret ---------------------------------------------------------------------


def test_the_token_is_never_in_a_url_a_log_line_or_a_repr(hub, caplog):
    tok = hub.add_token("alice")
    c = client_for(hub, tok)
    with caplog.at_level(logging.DEBUG):
        lease, _ = c.lease_acquire("x", ttl=600)
        c.lease_heartbeat(lease.token, "x")
        c.lease_history()
        c.lease_release(lease.token, "x")
    assert all(tok not in r["path"] + r["query"] for r in hub.requests)
    assert all(r["authorized"] for r in hub.requests)
    assert tok not in caplog.text and tok not in repr(c)


def test_an_anonymous_client_sends_no_authorization_header(hub):
    c = client_for(hub, None)
    with pytest.raises(UnreachableError):
        c.lease_show()
    assert hub.requests and not any(r["authorized"] for r in hub.requests)
