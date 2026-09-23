"""T9: boards.toml. Secrets never leak; each check has a negative twin."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from socharness.core.model import Candidate, Link, LinkKind
from socharness.power.config import (
    BOARDS_FILE,
    ConfigError,
    Secret,
    default_path,
    load_boards,
    parse_power,
    power_link,
    safe_url,
    with_links,
)

PW = "s3cr3t-Pa55"
BOARD = "mps3@192.168.10.101:6900"


def write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / BOARDS_FILE
    path.write_text(text)
    path.chmod(mode)
    return path


def test_missing_file_is_empty(tmp_path: Path):
    cfg = load_boards(tmp_path / "nope.toml")
    assert cfg.boards == () and cfg.for_board(BOARD) is None


def test_default_path_follows_the_state_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SOCHARNESS_STATE_DIR", str(tmp_path / "st"))
    assert default_path() == tmp_path / "st" / BOARDS_FILE


def test_a_shelly_table_with_an_env_password(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHELLY_PW", PW)
    path = write(tmp_path, f"""
[boards."{BOARD}"]
match = ["192.168.10.101"]
[boards."{BOARD}".power]
kind = "shelly_gen2"
url = "http://192.168.1.50"
auth = {{ password_env = "SHELLY_PW" }}
[boards."{BOARD}".sysmon]
backend = "xsdb"
""")
    b = load_boards(path).for_board(BOARD)
    assert b is not None and b.power is not None and b.power_error == ""
    p = b.power
    assert (p.kind, p.url, p.outlet, p.timeout_s, p.cycle) == ("shelly_gen2", "http://192.168.1.50",
                                                              0, 3.0, True)
    assert p.auth is not None and p.auth.user == "admin" and p.auth.password.reveal() == PW
    assert dict(b.tables) == {"sysmon": {"backend": "xsdb"}}
    # The secret never prints.
    for text in (repr(b), str(b), repr(p), p.label, p.address, str(p.auth.password)):
        assert PW not in text
    assert repr(Secret(PW)) == "Secret(***)"


def test_board_matching_exact_key_then_alias(tmp_path: Path):
    path = write(tmp_path, """
[boards.lab1]
match = ["192.168.10.101"]
power = { kind = "netio", url = "http://pdu", outlet = 2 }
[boards."mps3@usb:/dev/ttyUSB3"]
power = { kind = "tasmota", url = "http://plug" }
""")
    cfg = load_boards(path)
    eth = (Link(LinkKind.ETHERNET, "192.168.10.101:6900"),)
    assert cfg.for_board(BOARD, eth).key == "lab1"                    # via the link's host
    assert cfg.for_board("mps3@usb:/dev/ttyUSB3").power.kind == "tasmota"   # exact key
    # Negative twins: another board matches nothing.
    assert cfg.for_board("mps3@10.0.0.9:6900", (Link(LinkKind.ETHERNET, "10.0.0.9:6900"),)) is None


def test_a_bad_power_table_is_an_error_on_that_board_only(tmp_path: Path):
    path = write(tmp_path, """
[boards.a]
power = { kind = "toaster", url = "http://x" }
[boards.b]
power = { kind = "netio", url = "http://pdu" }
""")
    cfg = load_boards(path)
    a, b = cfg.for_board("a"), cfg.for_board("b")
    assert a.power is None and "kind must be one of" in a.power_error and a.has_power
    assert b.power is not None and b.power_error == ""


@pytest.mark.parametrize("table,expect", [
    ({"kind": "shelly_gen2"}, "url is required"),
    ({"kind": "shelly_gen2", "url": "ftp://x"}, "must be http"),
    ({"kind": "shelly_gen2", "url": f"http://admin:{PW}@plug"}, "must not hold credentials"),
    ({"kind": "tasmota", "url": "http://p?user=a"}, "query string"),
    ({"kind": "tasmota", "url": "http://p", "outlet": 0}, "counts from 1"),
    ({"kind": "netio", "url": "http://p", "timeout_s": 0}, "timeout_s"),
    ({"kind": "netio", "url": "http://p", "colour": "red"}, "unknown keys: colour"),
    ({"kind": "netio", "url": "http://p", "auth": {"user": "a"}}, "exactly one of"),
    ({"kind": "netio", "url": "http://p", "auth": {"password": PW, "password_env": "X"}},
     "exactly one of"),
    ({"kind": "netio", "url": "http://p", "auth": {"password_env": "T9_UNSET_VAR"}}, "is not set"),
    ({"kind": "ina260_mcp2221", "i2c_address": 0x10}, "INA260 address"),
])
def test_power_table_errors_are_exact_and_secret_free(table, expect, monkeypatch):
    monkeypatch.delenv("T9_UNSET_VAR", raising=False)
    with pytest.raises(ConfigError) as err:
        parse_power(table)
    assert expect in str(err.value)
    assert PW not in str(err.value)


def test_password_file(tmp_path: Path):
    pw = tmp_path / "pw"
    pw.write_text(PW + "\n")
    p = parse_power({"kind": "netio", "url": "http://p", "auth": {"password_file": str(pw)}})
    assert p.auth.password.reveal() == PW
    with pytest.raises(ConfigError, match="cannot read"):
        parse_power({"kind": "netio", "url": "http://p",
                     "auth": {"password_file": str(tmp_path / "missing")}})


def test_ina260_needs_no_url():
    p = parse_power({"kind": "ina260_mcp2221", "i2c_address": 0x41, "device": 1})
    assert p.url == "" and p.cycle is False and p.address == "mcp2221://1/0x41"


def test_invalid_toml_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_boards(write(tmp_path, "[boards\nx = "))


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_world_readable_inline_password_warns_without_the_password(tmp_path: Path, caplog):
    text = f'[boards.a]\npower = {{ kind = "netio", url = "http://p", auth = {{ password = "{PW}" }} }}\n'
    with caplog.at_level(logging.WARNING, logger="socharness.power.config"):
        load_boards(write(tmp_path, text, mode=0o644))
    assert "chmod 600" in caplog.text and PW not in caplog.text
    caplog.clear()
    # Negative twin: a private file does not warn.
    with caplog.at_level(logging.WARNING, logger="socharness.power.config"):
        load_boards(write(tmp_path, text, mode=0o600))
    assert caplog.text == ""


def test_power_link_is_secret_free_and_links_merge(tmp_path: Path):
    path = write(tmp_path, f"""
[boards."{BOARD}".power]
kind = "shelly_gen2"
url = "http://192.168.1.50:8080/"
auth = {{ password = "{PW}" }}
""")
    b = load_boards(path).for_board(BOARD)
    link = power_link(b)
    assert link == Link(LinkKind.SMART_POWER, "http://192.168.1.50:8080",
                        f"shelly_gen2 outlet 0 ({BOARDS_FILE})")
    assert PW not in repr(link)
    assert power_link(None) is None
    cand = Candidate("mps3", BOARD, (Link(LinkKind.ETHERNET, "192.168.10.101:6900"),))
    merged = with_links(cand, [link])
    assert [lk.kind for lk in merged.links] == [LinkKind.ETHERNET, LinkKind.SMART_POWER]
    assert with_links(merged, [link]) is merged          # no duplicate
    assert with_links(cand, []) is cand


def test_safe_url_strips_credentials_and_query():
    assert safe_url(f"http://admin:{PW}@h:81/x/?password={PW}") == "http://h:81/x"
    assert safe_url("nonsense") == "<unparseable url>"
