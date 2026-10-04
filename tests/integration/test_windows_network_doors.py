"""Lane WINDOWS: where this PC's network check shows (``harness-manager probe`` and ``info``
on failure, ``info``'s identify witness, the wizard's scan and witness), with Windows
pretended (``winps.is_windows``) and PowerShell answered from fixtures (``winps._run``).
No board, no PowerShell, no packet off this machine (the hosts are loopback). Each door has
its negative twin: off Windows, nothing is added."""

from __future__ import annotations

import json

import pytest

from harness_manager.cli.main import main
from harness_manager.core import winps
from harness_manager.core.errors import ActionFailedError, ExitCode, UnreachableError
from harness_manager.services import bringup
from harness_manager_mps3 import identify as IDF
from tests.fakes import win_net


@pytest.fixture
def windows(monkeypatch):
    """Windows, whose board adapter has only a 169.254 address."""
    asked: list[list[str]] = []

    def run(argv, timeout=30.0):  # noqa: ANN001
        asked.append(list(argv))
        return 0, win_net.answer(win_net.laptop()), b""

    monkeypatch.setattr(winps, "is_windows", lambda platform=None: True)
    monkeypatch.setattr(winps, "_run", run)
    return asked


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_probe_with_nothing_answering_on_windows_prints_the_network_fix(windows, capsys):
    rc, out, err = run_cli(capsys, "--json", "probe", "--no-scan", "--host", "127.0.0.1:1",
                           "--timeout", "0.3")
    assert rc == ExitCode.ABSENT
    assert "this PC's network (127.0.0.1, Windows):" in err
    assert "No address on the board's network" in err
    assert 'Set-NetIPInterface -InterfaceAlias "Ethernet 2" -Dhcp Disabled' in err
    doc = json.loads(out)
    net = doc["error"]["data"]["network"]
    assert net["problems"][0]["code"] == "no_address" and len(windows) == 1


def test_twin_probe_off_windows_adds_nothing(monkeypatch, capsys):
    monkeypatch.setattr(winps, "_run", lambda *a, **k: pytest.fail("PowerShell off Windows"))
    rc, out, err = run_cli(capsys, "--json", "probe", "--no-scan", "--host", "127.0.0.1:1",
                           "--timeout", "0.3")
    assert rc == ExitCode.ABSENT and "this PC's network" not in err
    assert "network" not in (json.loads(out)["error"].get("data") or {})


def test_twin_a_usb_only_probe_is_never_checked(windows, capsys):
    # an explicit Debug USB port is listed as given: no Ethernet was looked for
    rc, _, err = run_cli(capsys, "probe", "--no-scan", "--serial", "COM99", "--timeout", "0.3")
    assert rc == ExitCode.OK and "this PC's network" not in err and windows == []


def test_info_on_a_board_that_does_not_answer_prints_the_network_fix(windows, capsys):
    rc, _, err = run_cli(capsys, "info", "127.0.0.1:1")
    assert rc != ExitCode.OK and "this PC's network (127.0.0.1, Windows):" in err


class _Shell:
    host = "192.168.10.101"


class _Session:
    shell = _Shell()
    candidate = None


def test_infos_identify_witness_names_the_windows_fix_when_tcp_works():
    def silent(host, **kw):  # noqa: ANN001
        raise UnreachableError("nothing answered identify at 192.168.10.101:6899")

    def public(host, **kw):  # noqa: ANN001
        assert kw == {"identify_ok": False, "tcp_ok": True}
        from harness_manager.services import netcheck as NC

        adapters, blocks = NC.parse(win_net.laptop(board_ip="192.168.10.1"))
        return NC.diagnose(host, adapters, blocks, **kw)

    why = IDF.DiscoverWitness(probe=silent, netcheck=public).reason(_Session())
    assert why.startswith("the board did not answer identify from here")
    assert ". On this Windows PC: The board's network is Public:" in why
    assert ('As Administrator: Set-NetConnectionProfile -InterfaceAlias "Ethernet 2" '
            "-NetworkCategory Private") in why


def test_twin_the_witness_off_windows_keeps_its_words():
    def silent(host, **kw):  # noqa: ANN001
        raise UnreachableError("nothing answered")

    why = IDF.DiscoverWitness(probe=silent, netcheck=lambda h, **kw: None).reason(_Session())
    assert why.endswith("unless something drops UDP 6899")


class _Engine:
    def __init__(self) -> None:
        self.hints = []

    def probe(self, hints):
        self.hints.append(hints)
        return []

    def open_boards(self):
        return []


def test_the_wizards_witness_timeout_carries_the_network_check(windows):
    clock = iter([0.0, 0.0, 999.0])
    with pytest.raises(ActionFailedError) as exc:
        bringup.witness(_Engine(), "192.168.10.101", wait_s=10, now=lambda: next(clock),
                        sleep=lambda s: None)
    net = exc.value.data["network"]
    assert exc.value.data["timeout"] is True and net["problems"][0]["code"] == "no_address"
    assert net["problems"][0]["admin"][1] == ('New-NetIPAddress -InterfaceAlias "Ethernet 2" '
                                              "-IPAddress 192.168.10.1 -PrefixLength 24")


def test_the_wizards_scan_shows_the_check_when_nothing_answers(windows):
    out = bringup.scan(_Engine())
    assert out["ethernet"]["state"] == "none"
    assert out["ethernet"]["network"]["problems"][0]["code"] == "no_address"


def test_twin_the_wizard_off_windows_adds_no_check(monkeypatch):
    monkeypatch.setattr(winps, "_run", lambda *a, **k: pytest.fail("PowerShell off Windows"))
    out = bringup.scan(_Engine())
    assert "network" not in out["ethernet"]
    clock = iter([0.0, 0.0, 999.0])
    with pytest.raises(ActionFailedError) as exc:
        bringup.witness(_Engine(), "192.168.10.101", wait_s=10, now=lambda: next(clock),
                        sleep=lambda s: None)
    assert "network" not in exc.value.data
