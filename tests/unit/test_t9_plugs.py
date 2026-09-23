"""T9: metered-outlet drivers against fake devices on 127.0.0.1. Never a 0 for a missing value;
never a password in a log, a reason or a source. Each check has a negative twin."""

from __future__ import annotations

import logging
import socket

import pytest

from socharness.core.errors import ActionFailedError, UnavailableError, UnreachableError, UsageError
from socharness.power.adapter import PowerAdapter, make_driver, make_power_adapter
from socharness.power.base import ON_GRACE_S
from socharness.power.config import Auth, BoardConfig, PowerConfig, Secret
from socharness.power.http import digest_authorization, parse_challenge
from tests.fakes.t9_plugs import FakeClock, FakeNetio, FakeShelly, FakeTasmota

PW = "pl4g-Secret"
NAMES = ["board_power", "supply_voltage", "supply_current"]


def cfg(kind: str, url: str, *, password: str | None = None, **kw) -> PowerConfig:
    auth = Auth("admin", Secret(password)) if password else None
    outlet = kw.pop("outlet", {"shelly_gen2": 0}.get(kind, 1))
    return PowerConfig(kind=kind, url=url, outlet=outlet, auth=auth, **kw)


def driver(kind: str, url: str, clock: FakeClock | None = None, **kw):
    clock = clock or FakeClock()
    return make_driver(cfg(kind, url, **kw), clock=clock, sleep=clock.sleep)


def assert_all_unavailable(rows, text: str) -> None:
    assert [r.name for r in rows] == NAMES
    for r in rows:
        assert r.value is None and not r.available, r        # never a 0
        assert text in r.reason, r.reason
        assert r.source


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --- HTTP Digest (RFC 7616 §3.9.1 known answers) -----------------------------------------

RFC_CHALLENGE = ('Digest realm="http-auth@example.org", qop="auth, auth-int", '
                 'algorithm=SHA-256, nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", '
                 'opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"')
RFC_CNONCE = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"


@pytest.mark.parametrize("algorithm,response", [
    ("SHA-256", "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"),
    ("MD5", "8ca523f5e9506fed4657c9700eebdbec"),
])
def test_digest_matches_rfc_7616(algorithm, response):
    ch = parse_challenge(RFC_CHALLENGE.replace("SHA-256", algorithm))
    header = digest_authorization(ch, method="GET", uri="/dir/index.html", user="Mufasa",
                                  password="Circle of Life", nc=1, cnonce=RFC_CNONCE)
    assert f'response="{response}"' in header and "qop=auth" in header and "nc=00000001" in header
    # Negative twin: the wrong password gives a different response.
    wrong = digest_authorization(ch, method="GET", uri="/dir/index.html", user="Mufasa",
                                 password="circle of life", nc=1, cnonce=RFC_CNONCE)
    assert response not in wrong


def test_digest_refuses_an_unknown_algorithm():
    with pytest.raises(ValueError, match="unsupported digest algorithm"):
        digest_authorization({"algorithm": "SHA-512-256"}, method="GET", uri="/", user="a",
                             password="b", nc=1, cnonce="c")


# --- Shelly Gen2 ------------------------------------------------------------------------


def test_shelly_reads_watts_volts_amps():
    with FakeShelly() as dev:
        rows = driver("shelly_gen2", dev.url).read()
    assert [(r.name, r.value, r.unit) for r in rows] == [
        ("board_power", 11.4, "W"), ("supply_voltage", 239.1, "V"), ("supply_current", 0.071, "A")]
    assert all(r.source == f"shelly_gen2 {dev.url} outlet 0" for r in rows)
    assert all("AC at the wall outlet" in r.reason for r in rows)


def test_shelly_digest_auth_ok_and_wrong_password():
    with FakeShelly(password=PW) as dev:
        good = driver("shelly_gen2", dev.url, password=PW).read()
        bad = driver("shelly_gen2", dev.url, password="nope").read()
        none = driver("shelly_gen2", dev.url).read()
    assert good[0].value == 11.4
    assert_all_unavailable(bad, "rejected the credentials (HTTP 401)")
    assert_all_unavailable(none, "needs a password (HTTP 401)")


def test_shelly_timeout_is_unavailable_not_zero():
    with FakeShelly() as dev:
        dev.hang = True
        rows = driver("shelly_gen2", dev.url, timeout_s=0.3).read()
    assert_all_unavailable(rows, "did not answer within 0.3 s")


def test_refused_connection_is_unavailable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    rows = driver("shelly_gen2", f"http://127.0.0.1:{port}", timeout_s=1.0).read()
    assert_all_unavailable(rows, "refused the connection")


def test_shelly_missing_field_and_wrong_switch():
    with FakeShelly() as dev:
        dev.voltage = None
        rows = driver("shelly_gen2", dev.url).read()
        wrong = driver("shelly_gen2", dev.url, outlet=3).read()
        dev.garbage = True
        junk = driver("shelly_gen2", dev.url).read()
    assert rows[0].value == 11.4 and rows[1].value is None
    assert "did not report voltage" in rows[1].reason
    assert_all_unavailable(wrong, "HTTP 400")
    assert_all_unavailable(junk, "not JSON")


def test_shelly_outlet_off_says_so():
    with FakeShelly() as dev:
        dev.output = False
        rows = driver("shelly_gen2", dev.url).read()
    assert rows[0].value == 0.0 and "switched OFF" in rows[0].reason   # a measured 0, labelled


def test_shelly_power_cycle_is_timed_by_the_device(clock):
    with FakeShelly(clock=clock, password=PW) as dev:
        d = driver("shelly_gen2", dev.url, clock, password=PW)
        phases = []
        ev = d.power_cycle(5.0, progress=lambda p, n, t: phases.append((p, n, t)))
    assert dev.sets == [{"id": "0", "on": "false", "toggle_after": "5"}]
    assert ev["confirmed_off"] is True and ev["confirmed_on"] is True and ev["was_on"] is True
    assert 5.0 <= ev["seconds"] <= 5.0 + ON_GRACE_S
    assert phases == [("off", 1, 2), ("on", 2, 2)]
    assert dev.output is True


def test_shelly_power_cycle_that_never_switches_fails(clock):
    with FakeShelly(clock=clock) as dev:
        dev.ignore_set = True
        with pytest.raises(ActionFailedError, match="still reports ON"):
            driver("shelly_gen2", dev.url, clock).power_cycle(5.0)


def test_power_cycle_refusals_send_nothing(clock):
    with FakeShelly(clock=clock) as dev:
        with pytest.raises(UsageError, match="out of range"):
            driver("shelly_gen2", dev.url, clock).power_cycle(0.5)
        with pytest.raises(UnavailableError, match="power.cycle = false"):
            driver("shelly_gen2", dev.url, clock, cycle=False).power_cycle(5.0)
        assert dev.requests == [] and dev.sets == []


def test_power_cycle_timeout_is_unreachable(clock):
    with FakeShelly(clock=clock) as dev:
        dev.hang = True
        with pytest.raises(UnreachableError):
            driver("shelly_gen2", dev.url, clock, timeout_s=0.3).power_cycle(5.0)


# --- Tasmota ----------------------------------------------------------------------------


def test_tasmota_reads_status_8():
    with FakeTasmota() as dev:
        rows = driver("tasmota", dev.url).read()
    assert [r.value for r in rows] == [9.0, 238.0, 0.061]
    assert dev.commands == ["Status 8"]
    assert "cmnd=Status%208" in dev.requests[0]["raw_path"]      # %20, never '+'


def test_tasmota_multi_channel_picks_the_outlet():
    with FakeTasmota(relays=2) as dev:
        dev.power = [5.5, 17.25]
        rows = driver("tasmota", dev.url, outlet=2).read()
        out3 = driver("tasmota", dev.url, outlet=3).read()
    assert rows[0].value == 17.25 and rows[1].value == 238.0
    assert out3[0].value is None and "did not report Power" in out3[0].reason


@pytest.mark.parametrize("style", ["warning", "401"])
def test_tasmota_wrong_password_both_styles(style):
    with FakeTasmota(password=PW, auth_style=style) as dev:
        good = driver("tasmota", dev.url, password=PW).read()
        bad = driver("tasmota", dev.url, password="nope").read()
    assert good[0].value == 9.0
    assert_all_unavailable(bad, "rejected the credentials")


def test_tasmota_without_metering_says_so():
    with FakeTasmota(metering=False) as dev:
        rows = driver("tasmota", dev.url).read()
    assert_all_unavailable(rows, "no ENERGY data")


def test_tasmota_power_cycle_uses_backlog_delay(clock):
    with FakeTasmota(clock=clock, relays=2) as dev:
        ev = driver("tasmota", dev.url, clock, outlet=2).power_cycle(5.0)
    assert "Backlog Power2 Off; Delay 50; Power2 On" in dev.commands
    assert ev["confirmed_off"] is True and ev["confirmed_on"] is True
    assert dev.power_on == [True, True]


# --- NETIO -------------------------------------------------------------------------------


def test_netio_reads_its_output():
    with FakeNetio(password=PW) as dev:
        rows = driver("netio", dev.url, password=PW, outlet=2).read()
        bad = driver("netio", dev.url, password="nope", outlet=2).read()
    assert [(r.name, r.value) for r in rows] == [
        ("board_power", 60.0), ("supply_voltage", 231.4), ("supply_current", 0.3)]
    assert_all_unavailable(bad, "rejected the credentials")


def test_netio_missing_output_lists_what_exists():
    with FakeNetio(outputs=4) as dev:
        rows = driver("netio", dev.url, outlet=7).read()
    assert_all_unavailable(rows, "no output ID 7 (outputs: 1, 2, 3, 4)")


def test_netio_power_cycle_short_off(clock):
    with FakeNetio(clock=clock) as dev:
        ev = driver("netio", dev.url, clock, outlet=3).power_cycle(7.5)
    assert dev.posts == [{"Outputs": [{"ID": 3, "Action": 2, "Delay": 7500}]}]
    assert ev["confirmed_on"] is True and dev.outputs[2]["State"] == 1


def test_netio_that_never_comes_back_fails_loudly(clock):
    with FakeNetio(clock=clock) as dev:
        d = driver("netio", dev.url, clock, outlet=1)
        orig = dev._tick
        dev._tick = lambda: None                       # the device loses its timer
        with pytest.raises(ActionFailedError, match="did not report ON again"):
            d.power_cycle(5.0)
        dev._tick = orig


# --- the adapter and the no-secrets rule ----------------------------------------------------


def test_adapter_from_a_board_table(clock):
    with FakeShelly(clock=clock) as dev:
        board = BoardConfig(key="b1", power=cfg("shelly_gen2", dev.url))
        adapter = make_power_adapter(board, clock=clock, sleep=clock.sleep)
        assert isinstance(adapter, PowerAdapter) and adapter.cycle_reason == ""
        assert adapter.read()[0].value == 11.4
        assert adapter.power_cycle(5.0)["board_id"] == "b1"
    assert make_power_adapter(None) is None
    assert make_power_adapter(BoardConfig(key="b2")) is None


def test_adapter_for_a_broken_table_explains_itself():
    adapter = make_power_adapter(BoardConfig(key="b", power_error="boards.toml: kind is bad"))
    assert_all_unavailable(adapter.read(), "kind is bad")
    assert adapter.cycle_reason == "boards.toml: kind is bad"
    with pytest.raises(UsageError, match="kind is bad"):
        adapter.power_cycle(5.0)


def test_adapter_turns_a_driver_crash_into_a_reason():
    class Boom:
        kind, label, cycle_reason = "x", "x meter", ""

        def read(self):
            raise RuntimeError("bus fell over")

    assert_all_unavailable(PowerAdapter(Boom()).read(), "RuntimeError: bus fell over")


def test_no_password_in_any_log_reason_or_source(clock, caplog):
    rows = []
    with caplog.at_level(logging.DEBUG):
        with FakeShelly(password=PW, clock=clock) as s, FakeTasmota(password=PW, clock=clock) as t, \
                FakeNetio(password=PW, clock=clock) as n:
            for kind, url in (("shelly_gen2", s.url), ("tasmota", t.url), ("netio", n.url)):
                for pw in (PW, "wrong-" + PW):
                    d = driver(kind, url, clock, password=pw)
                    rows += d.read()
                    try:
                        d.power_cycle(5.0)
                    except ActionFailedError as exc:
                        rows.append(str(exc))
            tasmota_paths = [r["raw_path"] for r in t.requests]
    assert "GET" in caplog.text                        # the debug log did record the requests
    assert PW not in caplog.text
    for r in rows:
        text = r if isinstance(r, str) else f"{r.source} {r.reason}"
        assert PW not in text
    # Negative twin: the device itself DID receive the password (so the test can see it).
    assert any(PW in p for p in tasmota_paths)
