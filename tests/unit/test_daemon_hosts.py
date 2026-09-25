"""SET-API: the service's Host allow-list (``daemon/hosts.py``; SETTINGS.md §12.8).

Each rule has a negative twin: the name that must be refused next to the one that passes.
"""

from __future__ import annotations

import socket

import pytest

from harness_manager.daemon import hosts
from harness_manager.daemon.server import host_allow_list


@pytest.mark.parametrize("header, name", [
    ("127.0.0.1:8080", "127.0.0.1"), ("LOCALHOST", "localhost"), ("[::1]:8080", "::1"),
    ("[::1]", "::1"), ("hub.example.:443", "hub.example"), ("", ""), ("  Hub.Example ", "hub.example"),
])
def test_host_of_strips_the_port_and_the_case(header, name):
    assert hosts.host_of(header) == name


def test_loopback_passes_on_any_port():
    allowed = hosts.allowed_hosts("127.0.0.1")
    for header in ("127.0.0.1:1", "localhost:65535", "[::1]:80", "127.9.9.9", "localhost"):
        assert hosts.host_allowed(header, allowed), header


def test_negative_twin_a_foreign_or_look_alike_name_is_refused():
    allowed = hosts.allowed_hosts("127.0.0.1")
    for header in ("evil.example", "localhost.evil.example", "127.0.0.1.nip.io", "",
                   "0.0.0.0", "[::]", "10.0.0.5:8080"):
        assert not hosts.host_allowed(header, allowed), header


def test_a_loopback_listen_adds_no_machine_names():
    allowed = hosts.allowed_hosts("127.0.0.1")
    assert allowed == hosts.LOOPBACK_NAMES


def test_negative_twin_off_loopback_the_listen_address_and_this_machines_names_pass():
    allowed = hosts.allowed_hosts("192.0.2.10")
    assert "192.0.2.10" in allowed and socket.gethostname().lower() in allowed
    wild = hosts.allowed_hosts("0.0.0.0")
    assert "0.0.0.0" not in wild and socket.gethostname().lower() in wild


def test_off_loopback_an_ip_address_passes_but_a_foreign_name_still_does_not():
    wild = hosts.allowed_hosts("0.0.0.0")
    for header in ("152.78.1.2:8080", "[2001:db8::1]:80", "10.0.0.5"):
        assert hosts.host_allowed(header, wild), header
    for header in ("evil.example", "1.2.3.4.nip.io", hosts.ANY_IP, ""):
        assert not hosts.host_allowed(header, wild), header
    assert not hosts.host_allowed("152.78.1.2", hosts.allowed_hosts("127.0.0.1"))   # the twin


def test_the_configured_names_are_added():
    allowed = hosts.allowed_hosts("127.0.0.1", ["HM.Lab.Example.", " ", ""])
    assert "hm.lab.example" in allowed and "" not in allowed
    assert hosts.host_allowed("hm.lab.example:8443", allowed)
    assert not hosts.host_allowed("hm.lab.example.evil", allowed)


def test_the_service_reads_advanced_allowed_hosts_from_its_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("harness_manager.services.update.policy.policy_path",
                        lambda *a, **k: tmp_path / "no-policy.toml")
    (tmp_path / "settings.toml").write_text('[advanced]\nallowed_hosts = ["hm.lab.example"]\n')
    assert "hm.lab.example" in host_allow_list(tmp_path, "127.0.0.1")


def test_negative_twin_a_broken_settings_file_leaves_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr("harness_manager.services.update.policy.policy_path",
                        lambda *a, **k: tmp_path / "no-policy.toml")
    (tmp_path / "settings.toml").write_text('[advanced\nallowed_hosts = ["hm.lab.example"]\n')
    assert host_allow_list(tmp_path, "127.0.0.1") == hosts.LOOPBACK_NAMES
    (tmp_path / "settings.toml").write_text('[advanced]\nallowed_hosts = ["bad name"]\n')
    assert host_allow_list(tmp_path, "127.0.0.1") == hosts.LOOPBACK_NAMES


def test_the_refusal_names_the_way_out_and_never_more_than_the_name():
    err = hosts.refusal("x" * 500 + ":80")
    assert err.code == 15 and "advanced.allowed_hosts" in err.hint and len(err.message) < 200
