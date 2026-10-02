"""Lane WINDOWS: this PC's network check (services/netcheck.py) on Windows fixtures: an
address on the board's /24, the Public profile, a firewall that drops identify, a Python
block rule. HM never runs a command that changes Windows: only Get-* is asked. Each
behaviour has its negative twin."""

from __future__ import annotations

from harness_manager.core import winps
from harness_manager.services import netcheck as NC
from tests.fakes import win_net

RULE = ('New-NetFirewallRule -DisplayName "Harness Manager board UDP" -Direction Inbound '
        "-Protocol UDP -RemoteAddress 192.168.10.0/24 -Action Allow")


def run_with(doc, seen=None):
    def run(argv):
        if seen is not None:
            seen.append(list(argv))
        return 0, win_net.answer(doc), b""
    return run


def check(doc, host="192.168.10.101", **kw):
    return NC.check(host, platform="win32", run=run_with(doc), exes=["C:\\py\\python.exe"], **kw)


def codes(found):
    return [p["code"] for p in found["problems"]]


def test_no_address_on_the_boards_network_names_the_169_254_adapter_and_the_fix():
    found = check(win_net.laptop())
    (p,) = found["problems"]
    assert p["code"] == "no_address" and p["adapter"] == "Ethernet 2"
    assert p["admin"] == ['Set-NetIPInterface -InterfaceAlias "Ethernet 2" -Dhcp Disabled',
                          'New-NetIPAddress -InterfaceAlias "Ethernet 2" -IPAddress '
                          "192.168.10.1 -PrefixLength 24"]
    assert "This PC has no address on 192.168.10.0/24, the board's network (192.168.10.101)" \
        in p["text"]
    assert "'Ethernet 2' (Realtek USB GbE Family Controller)" in p["text"]
    assert "Subnet mask 255.255.255.0, Gateway empty" in p["gui"]
    assert found["ok"] is False and found["network"] == "192.168.10.0/24"


def test_twin_two_169_254_adapters_are_not_guessed():
    found = check(win_net.laptop(apipa=2))
    (p,) = found["problems"]
    assert p["adapter"] == "" and "<the Ethernet adapter cabled to the board>" in p["admin"][0]
    assert "Which adapter is cabled to the board? Up now:" in p["text"]
    assert "'Ethernet 2'" in p["text"] and "'Ethernet 3'" in p["text"]
    assert "'Ethernet' (" not in p["text"]                      # disconnected: not offered
    assert "Wi-Fi" not in p["text"]                             # a radio is never the cable


def test_a_public_board_network_says_make_it_private_or_allow_the_udp():
    found = check(win_net.laptop(board_ip="192.168.10.1", category="Public"))
    (p,) = found["problems"]
    assert p["code"] == "public_profile" and found["adapter"] == "Ethernet 2"
    assert p["admin"] == ['Set-NetConnectionProfile -InterfaceAlias "Ethernet 2" '
                          "-NetworkCategory Private"]
    assert p["alternative"] == [RULE]
    assert "Windows Firewall drops the board's UDP replies there" in p["text"]


def test_twin_the_cim_number_0_is_public_too_and_1_private_is_fine():
    pub = check(win_net.laptop(board_ip="192.168.10.1", category=0))
    assert codes(pub) == ["public_profile"]
    ok = check(win_net.laptop(board_ip="192.168.10.1", category=1))
    assert ok["ok"] is True and ok["problems"] == [] and ok["category"] == "Private"


def test_identify_silent_while_tcp_answers_on_a_private_network_is_the_firewall_rule():
    found = check(win_net.laptop(board_ip="192.168.10.1", category="Private"),
                  identify_ok=False, tcp_ok=True)
    (p,) = found["problems"]
    assert p["code"] == "udp_blocked" and p["admin"] == [RULE]
    assert "The board answers TCP at 192.168.10.101 but not UDP identify" in p["text"]


def test_twin_identify_silent_and_tcp_silent_too_is_not_called_a_firewall():
    found = check(win_net.laptop(board_ip="192.168.10.1", category="Private"),
                  identify_ok=False, tcp_ok=False)
    assert found["problems"] == [] and found["ok"] is True


def test_a_python_block_rule_is_named_with_its_off_switch():
    blocks = [{"Name": "TCP Query User{6D7F1A52-0E7B-4C1E-9A3D-1B2C3D4E5F60}C:\\py\\python.exe",
               "DisplayName": "python.exe", "Profile": "Public",
               "Program": "C:\\py\\python.exe"}]
    found = check(win_net.laptop(board_ip="192.168.10.1", category="Private", blocks=blocks))
    (p,) = found["problems"]
    assert p["code"] == "python_blocked"
    assert p["admin"] == ["Disable-NetFirewallRule -Name 'TCP Query User{6D7F1A52-0E7B-4C1E-"
                          "9A3D-1B2C3D4E5F60}C:\\py\\python.exe'"]
    assert "wins over any allow rule" in p["text"]


def test_the_question_is_read_only_and_names_this_python(monkeypatch):
    seen: list[list[str]] = []
    NC.check("192.168.10.101", platform="win32", run=run_with(win_net.laptop(), seen),
             exes=["C:\\Users\\Ann O'Neil\\hm\\venv\\Scripts\\python.exe"])
    (argv,) = seen
    script = winps.script_of(argv)
    for verb in ("Get-NetIPAddress", "Get-NetConnectionProfile", "Get-NetAdapter",
                 "Get-NetFirewallApplicationFilter", "ConvertTo-Json"):
        assert verb in script
    for never in ("Set-Net", "New-Net", "Disable-Net", "Enable-Net", "Remove-Net"):
        assert never not in script, never
    assert "'C:\\Users\\Ann O''Neil\\hm\\venv\\Scripts\\python.exe'" in script


def test_twin_off_windows_there_is_no_check_and_nothing_runs():
    def boom(argv):
        raise AssertionError("ran PowerShell off Windows")
    assert NC.check("192.168.10.101", platform="linux", run=boom) is None
    assert NC.check("192.168.10.101", platform="darwin", run=boom) is None


def test_powershell_failing_is_a_problem_with_what_to_check_by_hand():
    found = NC.check("192.168.10.101", platform="win32", exes=[],
                     run=lambda argv: (1, b"", b"Get-NetAdapter : access denied\r\n"))
    (p,) = found["problems"]
    assert p["code"] == "unknown" and "access denied" in p["text"]
    assert "a Private network profile (Get-NetConnectionProfile)" in p["text"]


def test_the_cli_lines_give_each_problem_then_its_admin_powershell():
    found = check(win_net.laptop(board_ip="192.168.10.1", category="Public"))
    out = NC.lines(found)
    assert out[0] == "this PC's network (192.168.10.101, Windows):"
    assert out[1].startswith("  The board's network is Public: Windows treats 'Ethernet 2'")
    assert out[2] == ("    in PowerShell as Administrator (Start, type PowerShell, right-click "
                      "Windows PowerShell, Run as administrator, Yes; paste each line):")
    assert out[3] == ('      Set-NetConnectionProfile -InterfaceAlias "Ethernet 2" '
                      "-NetworkCategory Private")
    assert out[4:6] == ["    or instead:", f"      {RULE}"]
    assert out[-1] == "  Harness Manager never runs these itself."
    assert NC.lines(None) == [] and NC.lines({"problems": []}) == []


def test_windows_ping_counts_only_a_reply_with_a_ttl():
    from harness_manager_mps3.shell import echo_answered

    reply = b"Reply from 192.168.10.101: bytes=32 time<1ms TTL=64\r\n"
    unreachable = b"Reply from 192.168.10.1: Destination host unreachable.\r\n"
    assert echo_answered(0, reply, "win32") is True
    assert echo_answered(0, unreachable, "win32") is False         # exit 0 all the same
    assert echo_answered(1, b"Request timed out.\r\n", "win32") is False


def test_twin_ping_elsewhere_trusts_its_exit_code():
    from harness_manager_mps3.shell import echo_answered

    assert echo_answered(0, b"", "linux") is True and echo_answered(1, b"", "darwin") is False


def test_hub_reach_ping_on_windows_needs_the_ttl_too(monkeypatch):
    import subprocess

    from harness_manager.transports import hub_reach

    monkeypatch.setattr(hub_reach.platform, "system", lambda: "Windows")
    out = {"v": b"Reply from 10.0.0.1: Destination host unreachable.\r\n"}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, out["v"], b""))
    assert hub_reach.ping("10.0.0.9") is False
    out["v"] = b"Reply from 10.0.0.9: bytes=32 time=3ms TTL=63\r\n"
    assert hub_reach.ping("10.0.0.9") is True
