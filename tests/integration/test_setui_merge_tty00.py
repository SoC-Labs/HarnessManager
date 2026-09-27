"""Lane SET-UI-MERGE, MCC-FIX over the service's routes: no share on tty_00 through
``PUT /settings`` or ``POST /hubs/adopt`` (the daemon app over the demo engine, whose pack
declares the MPS3 rows, under FastAPI's TestClient; SET-UI's world). ``shares.mcc`` still
names the MCC console's path (``hub_mcc.mcc_tty_for``). Each check has its negative twin.
"""

from __future__ import annotations

from tests.integration.test_setui_hubs_api import w  # noqa: F401 - the fixture

TTY00 = "/dev/mps3_01_pl/tty_00"
REASON = "tty_00 is the MCC console; Harness Manager never shares it; the MCC is reached on the hub"


def test_put_settings_refuses_a_share_on_tty_00_with_the_reason(w):  # noqa: F811
    r = w.call("PUT", "/settings", json={"boards.lab.hub.shares.fpga_uart0": TTY00})
    assert r.status_code == 400 and REASON in r.json()["error"]["message"], r.text
    assert not (w.state / "boards.toml").exists() and w.changed() == []


def test_negative_twin_put_settings_takes_tty_01_and_the_mccs_path_name(w):  # noqa: F811
    r = w.call("PUT", "/settings", json={"boards.lab.hub.shares.fpga_uart1": "/dev/mps3_01_pl/tty_01",
                                         "boards.lab.hub.shares.mcc": TTY00})
    assert r.status_code == 200, r.text
    assert w.file("boards.toml")["boards"]["lab"]["hub"]["shares"] == {
        "fpga_uart1": "/dev/mps3_01_pl/tty_01", "mcc": TTY00}


def test_adopt_refuses_an_inline_hub_with_a_share_on_tty_00(w):  # noqa: F811
    w.state.mkdir(parents=True, exist_ok=True)
    text = ('[boards.lab]\nhub = { host = "hub.invalid", target = "mps3_01_pl", '
            f'shares = {{ fpga_uart0 = "{TTY00}" }} }}\n')
    (w.state / "boards.toml").write_text(text)
    r = w.call("POST", "/hubs/adopt", json={"board": "lab"})
    assert r.status_code == 400 and REASON in r.json()["error"]["message"], r.text
    assert (w.state / "boards.toml").read_text() == text and w.changed() == []


def test_negative_twin_adopt_keeps_a_lane_share_and_the_mccs_path_name(w):  # noqa: F811
    w.state.mkdir(parents=True, exist_ok=True)
    (w.state / "boards.toml").write_text(
        '[boards.lab]\nhub = { host = "hub.invalid", target = "mps3_01_pl", '
        f'shares = {{ mcc = "{TTY00}", fpga_uart1 = "/dev/mps3_01_pl/tty_01" }} }}\n')
    r = w.call("POST", "/hubs/adopt", json={"board": "lab"})
    assert r.status_code == 200 and r.json()["changed"] is True, r.text
    hub = w.file("boards.toml")["boards"]["lab"]["hub"]
    assert hub["use"] == "hub" and hub["shares"] == {"mcc": TTY00, "fpga_uart1": "/dev/mps3_01_pl/tty_01"}


BY_ID_00 = "/dev/serial/by-id/usb-FTDI_Quad_RS232-HS-if00-port0"


def test_put_settings_refuses_the_mccs_by_id_alias_and_a_trailing_slash(w):  # noqa: F811
    """REVIEW-W5 10: one rule, on normalised paths: the by-id alias of FT4232H interface 00
    is the MCC too (with a message), as is ``…/tty_00/``."""
    for tty in (BY_ID_00, TTY00 + "/", "/dev/mps3_01_pl/./tty_00"):
        r = w.call("PUT", "/settings", json={"boards.lab.hub.shares.fpga_uart0": tty})
        assert r.status_code == 400 and REASON in r.json()["error"]["message"], (tty, r.text)
    r = w.call("PUT", "/settings", json={"boards.lab.hub.shares.fpga_uart0": BY_ID_00})
    assert "FT4232H interface 00" in r.json()["error"]["message"]
    assert not (w.state / "boards.toml").exists() and w.changed() == []


def test_negative_twin_another_interfaces_by_id_name_and_a_ttyusb_are_taken(w):  # noqa: F811
    r = w.call("PUT", "/settings", json={
        "boards.lab.hub.shares.fpga_uart1": BY_ID_00.replace("if00", "if01"),
        "boards.lab.hub.shares.fpga_uart2": "/dev/ttyUSB12"})
    assert r.status_code == 200, r.text
