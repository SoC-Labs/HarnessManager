"""L4 HOSTIO: on a mint-4 board (feature name ``hostio``) 6931 is "hostio1", unpaced, and the
console panel can read and switch the DUT's pin mode. A v2.1 board keeps "uart1" (paced).
6933 (ADP) is claim-gated: never in a forwarded list. Every check has a negative twin."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.core.events import EventBus
from harness_manager.services.console import ConsoleBroker
from harness_manager_mps3 import identify as ident
from harness_manager_mps3 import uart
from harness_manager_mps3.constants import CONSOLE_PORTS, DUT_CONSOLE_PACE_S, HOSTIO_ADP_PORT
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.tunnel import CLAIM_GATED_PORTS, DEFAULT_REMOTE_PORTS, forwardable
from tests.fakes.virtual_board import LINUX_HARNESSD, LINUX_MINT4, VirtualMps3


def _open(vb: VirtualMps3):
    ports = {n: p for n, p in vb.console_ports.items() if n in CONSOLE_PORTS}   # no hostio_adp
    pack = Mps3Pack(console_ports=ports)
    session = pack.open(pack.candidate_for_host(vb.shell_endpoint))
    session.identity()                      # the engine's first poll: the features arrive
    return session


@pytest.fixture
def broker():
    b = ConsoleBroker(EventBus(), backoff=(0.05, 0.2))
    yield b
    b.shutdown()


# -- the label and the pacing follow the feature name ----------------------------------------------


def test_mint4_6931_is_hostio1_and_unpaced(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        s = _open(vb)
        names = broker.names(s)
        assert "hostio1" in names and "uart1" not in names
        pace = s.consoles.console_write_pace_s()
        assert "hostio1" not in pace and pace["uart0"] == DUT_CONSOLE_PACE_S
        assert broker.endpoint(s, "hostio1").endswith(f":{vb.console_ports['uart1']}")
        row = {r["name"]: r for r in broker.consoles(s, live=True)}["hostio1"]
        assert row["carries"] == "hostio1"


def test_v21_6931_stays_uart1_and_paced(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        s = _open(vb)
        names = broker.names(s)
        assert "uart1" in names and "hostio1" not in names
        assert s.consoles.console_write_pace_s()["uart1"] == DUT_CONSOLE_PACE_S
        with pytest.raises(UnavailableError):
            broker.hostio(s)


def test_label_follows_the_feature_name_not_the_version(tmp_path):
    from dataclasses import replace

    # A 2.2.0 image without the name keeps uart1; a 2.1.0 image WITH the name relabels.
    no_name = replace(LINUX_MINT4, features=tuple(f for f in LINUX_MINT4.features if f != "hostio"))
    with VirtualMps3(tmp_path / "a", no_name) as vb:
        assert "uart1" in _open(vb).consoles.console_endpoints()
    named = replace(LINUX_HARNESSD, features=(*LINUX_HARNESSD.features, "hostio"))
    with VirtualMps3(tmp_path / "b", named) as vb:
        assert "hostio1" in _open(vb).consoles.console_endpoints()


def test_the_label_updates_when_the_identity_reads_the_features(tmp_path):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        s = _open(vb)
        s.consoles.note_features(())                 # a session opened before the board said
        assert "uart1" in s.consoles.console_endpoints()
        s.identity()                                 # a poll reads version.features
        assert "hostio1" in s.consoles.console_endpoints()


def test_the_rate_query_also_teaches_the_label_over_its_one_connection(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        s = _open(vb)
        s.consoles.note_features(())
        broker.baud(s, "uart0")                      # the uart_baud query reads version.features
        assert "hostio1" in s.consoles.console_endpoints()


def test_hostio1_has_no_baud_and_is_not_uart1_not_connected(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        row = broker.baud(_open(vb), "hostio1")
        assert row["settable"] is False and "connected" not in row
        assert row["carries"] == "hostio1"


# -- identify: hostio_usrt1 and claim_ports ----------------------------------------------------------


def _reply(**extra):
    base = {"ok": True, "op": "identify", "v": 1, "nonce": "n", "board": "mps3",
            "ip": "10.0.0.2", "shell_id": "0x1", "rm_id": "0x0", "harness": "2.2.0",
            "mode": "run", "ports": {"ctrl": 6900, "uart0": 6930, "uart1": 6931, "swo": 6932}}
    base.update(extra)
    return ident.IdentifyReply(raw=base, source=("10.0.0.2", 6899))


def test_identify_reads_hostio_usrt1_and_keeps_claim_ports_apart():
    r = _reply(ports={"ctrl": 6900, "uart1": 6931, "hostio_usrt1": 6931},
               claim_ports={"hostio_adp": 6933})
    assert r.hostio and r.claim_ports == {"hostio_adp": 6933}
    assert HOSTIO_ADP_PORT not in r.ports.values()
    assert "hostio" in r.identity().features


def test_identify_without_the_key_is_not_hostio():
    r = _reply()
    assert not r.hostio and r.claim_ports == {} and "hostio" not in r.identity().features


# -- 6933 is never forwarded ---------------------------------------------------------------------


def test_the_adp_port_is_never_in_a_forwarded_list():
    assert HOSTIO_ADP_PORT in CLAIM_GATED_PORTS
    assert HOSTIO_ADP_PORT not in DEFAULT_REMOTE_PORTS.values()
    got = forwardable({"control": 6900, "hostio_adp": 6933, "uart1": 6931})
    assert got == {"control": 6900, "uart1": 6931}
    # negative twin: nothing else is dropped
    assert forwardable({"a": 1, "b": 2}) == {"a": 1, "b": 2}


# -- the hostio verb: mode, pending, refusal --------------------------------------------------------


def test_status_shows_mode_pending_held_and_drops(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        s = _open(vb)
        v = broker.hostio(s)
        assert (v["mode"], v["pending"], v["changing"]) == ("console", "console", False)
        assert v["drops"] == {"ch0": 0, "ch1": 0} and v["note"] == ""


def test_set_makes_pending_differ_from_mode_until_the_next_swap(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        s = _open(vb)
        out = broker.hostio_set(s, "hostio4")
        assert out["mode"] == "console" and out["pending"] == "hostio4" and out["changing"]
        assert "next design load" in out["note"]
        assert out["result"]["applies"] == "rp_release" and out["result"]["route"] == "direct"
        vb.shell.hostio.rp_released()                # the swap lands
        after = broker.hostio(s)
        assert (after["mode"], after["changing"]) == ("hostio4", False)


def test_set_is_refused_in_words_while_a_design_runs(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        s = _open(vb)
        vb.shell.current_rm_id = 0x0A1B2C3D
        with pytest.raises(RefusedError) as e:
            broker.hostio_set(s, "hostio4")
        assert e.value.message == ("Load the greybox or hold the DUT first; the change applies "
                                   "at the next design load.")
        assert "0x" not in e.value.message
        assert broker.hostio(s)["pending"] == "console"        # nothing changed
        vb.shell.hostio.rp_held = True                          # negative twin: held is accepted
        assert broker.hostio_set(s, "hostio4")["pending"] == "hostio4"


def test_a_bad_mode_is_a_usage_error(tmp_path, broker):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        with pytest.raises(UsageError):
            broker.hostio_set(_open(vb), "uart")


@pytest.mark.parametrize("code,word", [("not_supported", "no hostio support"),
                                       ("no_hw", "no hostio block"),
                                       ("locked", "claimed"),
                                       ("busy", "busy"), ("timeout", "in time")])
def test_each_refusal_code_has_a_plain_sentence(code, word):
    err = uart.hostio_failure({"ok": False, "err": "x", "code": code})
    assert word in err.message


def test_hostio_view_pending_note():
    v = uart.hostio_view({"mode": "console", "pending": "hostio4", "drops": {"ch1": 3}})
    assert v["changing"] and v["drops"]["ch1"] == 3 and "hostio4" in v["note"]
