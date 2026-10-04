"""Can this PC reach a board on the board's own network? The Windows check (lane WINDOWS).

A new MPS3 answers at 192.168.10.101 on a cable straight to this PC. On a Windows laptop two
things go wrong, and HM can see both (read only, through PowerShell):

1. **No address on the board's /24.** The adapter cabled to the board needs a fixed
   address there (no DHCP server on a direct cable: Windows falls back to 169.254.x.x).
2. **A Public network profile.** Windows calls a new adapter's network (an "Unidentified
   network", no gateway) Public, and Windows Firewall then drops what the board sends
   unasked: the UDP identify replies (finding boards, a board in stage0 RESCUE, a board
   that moved), while TCP (6900, SSH) still works because this PC opened it. And a
   "Windows Security Alert" for Python that was cancelled leaves an inbound BLOCK rule for
   python.exe, which wins over any allow rule.

``check(host)`` answers ``{platform, host, network, adapter, category, ok, problems,
notes}``; each problem is ``{code, title, text, admin, alternative?, gui?}``: ``admin`` is
the exact Administrator PowerShell, one command per line, for the user to paste. HM NEVER
runs a command that changes the system (only ``Get-*`` here), and never asks for
Administrator.

Codes: ``no_address`` (no adapter in the board's /24; the fix names the adapter when one is
plainly the board's: Up with only a 169.254 address), ``public_profile`` (the adapter in the
/24 is Public: make it Private, or allow the board's UDP), ``udp_blocked`` (identify got no
reply while TCP to the board works), ``python_blocked`` (an inbound Block rule for this
Python), ``unknown`` (PowerShell could not say).

Off Windows ``check`` returns None: Linux and macOS have neither trap (their tools say
what to do in their own words). ``run`` is the PowerShell seam (tests answer with JSON).
"""

from __future__ import annotations

import ipaddress
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core import winps
from harness_manager.core.errors import HarnessError

CAPABILITY = "network"
#: How a Windows user opens the shell the fixes need (HM never opens it for them).
ADMIN_HOW = ("Start, type PowerShell, right-click Windows PowerShell, Run as administrator, "
             "Yes; paste each line")
#: Adapters never offered as "the one cabled to the board" (radios, virtual switches, VPNs).
NOT_A_CABLE = re.compile(r"wi-?fi|wireless|wlan|802\.11|bluetooth|vethernet|hyper-v|virtual|"
                         r"vpn|tap-windows|wintun|wireguard|loopback", re.IGNORECASE)
RULE_NAME = "Harness Manager board UDP"
#: MSFT_NetConnectionProfile.NetworkCategory as a number (Windows PowerShell 5.1's CIM).
CATEGORIES = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}


def _ps(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _exes() -> list[str]:
    """This Python, and the interpreter a venv's python.exe redirects to (the one whose
    socket Windows Firewall sees)."""
    out = [sys.executable, getattr(sys, "_base_executable", "") or ""]
    return [e for i, e in enumerate(out) if e and e not in out[:i]]


def network_ps(exes: Sequence[str]) -> str:
    """One read-only PowerShell question: IPv4 addresses, connection profiles, adapters, and
    enabled inbound Block rules for these programs."""
    progs = ", ".join(_ps(e) for e in exes) or "''"
    return (
        "$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "ForEach-Object { [pscustomobject]@{ Alias = [string]$_.InterfaceAlias; "
        "Index = [int]$_.InterfaceIndex; Ip = [string]$_.IPAddress; "
        "Prefix = [int]$_.PrefixLength } }); "
        "$profiles = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
        "ForEach-Object { [pscustomobject]@{ Alias = [string]$_.InterfaceAlias; "
        "Index = [int]$_.InterfaceIndex; Category = [string]$_.NetworkCategory; "
        "Name = [string]$_.Name } }); "
        "$adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object { "
        "[pscustomobject]@{ Alias = [string]$_.Name; Index = [int]$_.ifIndex; "
        "Status = [string]$_.Status; Description = [string]$_.InterfaceDescription } }); "
        f"$blocks = @(foreach ($p in @({progs})) {{ Get-NetFirewallApplicationFilter "
        "-Program $p -ErrorAction SilentlyContinue | Get-NetFirewallRule "
        "-ErrorAction SilentlyContinue | Where-Object { ([string]$_.Enabled) -eq 'True' -and "
        "([string]$_.Direction) -eq 'Inbound' -and ([string]$_.Action) -eq 'Block' } | "
        "ForEach-Object { [pscustomobject]@{ Name = [string]$_.Name; "
        "DisplayName = [string]$_.DisplayName; Profile = [string]$_.Profile; Program = $p } } }); "
        "[pscustomobject]@{ ips = $ips; profiles = $profiles; adapters = $adapters; "
        "blocks = $blocks } | ConvertTo-Json -Depth 4 -Compress")


@dataclass
class Adapter:
    alias: str
    index: int = -1
    ipv4: list[tuple[str, int]] = field(default_factory=list)
    status: str = ""
    description: str = ""
    category: str = ""             # Public | Private | DomainAuthenticated | "" (no profile)

    def in_network(self, net: ipaddress.IPv4Network) -> bool:
        return any(_addr(ip) in net for ip, _ in self.ipv4 if _addr(ip) is not None)

    @property
    def apipa_only(self) -> bool:
        return bool(self.ipv4) and all(ip.startswith("169.254.") for ip, _ in self.ipv4)

    @property
    def shown(self) -> str:
        return f"'{self.alias}'" + (f" ({self.description})" if self.description else "")


def _addr(text: str) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        return None


def parse(doc: Any) -> tuple[list[Adapter], list[dict[str, str]]]:
    """``network_ps``'s answer -> the adapters (with their addresses and profile) and the
    block rules."""
    doc = doc if isinstance(doc, dict) else {}
    by: dict[str, Adapter] = {}

    def get(alias: str, index: Any) -> Adapter:
        a = by.get(alias)
        if a is None:
            a = by[alias] = Adapter(alias=alias, index=int(index or -1))
        return a

    for row in winps.as_list(doc.get("adapters")):
        if isinstance(row, dict) and winps.text(row.get("Alias")):
            a = get(winps.text(row["Alias"]), row.get("Index"))
            a.status = winps.text(row.get("Status"))
            a.description = winps.text(row.get("Description"))
    for row in winps.as_list(doc.get("ips")):
        if isinstance(row, dict) and winps.text(row.get("Alias")):
            ip = winps.text(row.get("Ip"))
            if ip and not ip.startswith("127."):
                get(winps.text(row["Alias"]), row.get("Index")).ipv4.append(
                    (ip, int(row.get("Prefix") or 0)))
    for row in winps.as_list(doc.get("profiles")):
        if isinstance(row, dict) and winps.text(row.get("Alias")):
            get(winps.text(row["Alias"]), row.get("Index")).category = \
                winps.enum_name(row.get("Category"), CATEGORIES)
    blocks = [{k: winps.text(r.get(k)) for k in ("Name", "DisplayName", "Profile", "Program")}
              for r in winps.as_list(doc.get("blocks")) if isinstance(r, dict)]
    return [a for a in by.values() if a.alias.lower() != "loopback pseudo-interface 1"], blocks


def pc_address(host: str) -> str:
    """An address for this PC in the board's /24: .1, or .2 when the board is .1."""
    head, _, last = host.rpartition(".")
    return f"{head}.{2 if last == '1' else 1}"


def firewall_rule(net: str) -> str:
    return (f'New-NetFirewallRule -DisplayName "{RULE_NAME}" -Direction Inbound -Protocol UDP '
            f"-RemoteAddress {net} -Action Allow")


def private_profile(alias: str) -> str:
    return f'Set-NetConnectionProfile -InterfaceAlias "{alias}" -NetworkCategory Private'


def _problem(code: str, title: str, text: str, admin: Iterable[str] = (), **more: Any
             ) -> dict[str, Any]:
    return {"code": code, "title": title, "text": text, "admin": list(admin), **more}


def diagnose(host: str, adapters: Sequence[Adapter], blocks: Sequence[dict[str, str]] = (), *,
             identify_ok: bool | None = None, tcp_ok: bool | None = None) -> dict[str, Any]:
    """The findings for ``host`` from what PowerShell said (pure: the tests' entry)."""
    board = _addr(host)
    out: dict[str, Any] = {"platform": "win32", "host": host, "network": "", "adapter": "",
                           "category": "", "ok": True, "problems": [], "notes": [],
                           "admin_how": ADMIN_HOW}
    if board is None:
        out["notes"].append(f"{host} is not an IPv4 address: nothing to check here")
        return out
    net = ipaddress.IPv4Network(f"{board}/24", strict=False)
    out["network"] = str(net)
    problems: list[dict[str, Any]] = out["problems"]
    mine = [a for a in adapters if a.in_network(net)]
    if not mine:
        up = [a for a in adapters if a.status.lower() == "up"
              and not NOT_A_CABLE.search(f"{a.alias} {a.description}")]
        guess = [a for a in up if a.apipa_only]
        alias = guess[0].alias if len(guess) == 1 else "<the Ethernet adapter cabled to the board>"
        pc = pc_address(str(board))
        text = (f"This PC has no address on {net}, the board's network ({board}). Give the "
                f"Ethernet adapter cabled to the board a fixed address there, e.g. {pc} "
                "(netmask 255.255.255.0, no gateway).")
        if len(guess) == 1:
            text += (f" The adapter is {guess[0].shown}: it is up with only a 169.254.x.x "
                     "address (no DHCP on a direct cable).")
        elif up:
            text += (" Which adapter is cabled to the board? Up now: "
                     + "; ".join(a.shown for a in up) + ". Get-NetAdapter lists them all.")
        problems.append(_problem(
            "no_address", "No address on the board's network", text,
            [f'Set-NetIPInterface -InterfaceAlias "{alias}" -Dhcp Disabled',
             f'New-NetIPAddress -InterfaceAlias "{alias}" -IPAddress {pc} -PrefixLength 24'],
            gui=(f"Or in Settings: Network & internet, Ethernet (the board's adapter), IP "
                 f"assignment, Edit, Manual, IPv4 on: IP address {pc}, Subnet mask "
                 "255.255.255.0, Gateway empty, Save"),
            adapter=alias if len(guess) == 1 else ""))
    else:
        a = mine[0]
        out["adapter"], out["category"] = a.alias, a.category
        if a.category == "Public":
            problems.append(_problem(
                "public_profile", "The board's network is Public",
                f"Windows treats {a.shown}, the board's network, as a Public network, and "
                "Windows Firewall drops the board's UDP replies there: finding boards, a board "
                "in stage0 RESCUE, a board that moved. Make the network Private (one line), or "
                "allow UDP from the board's network.",
                [private_profile(a.alias)], alternative=[firewall_rule(str(net))],
                adapter=a.alias))
        elif identify_ok is False and tcp_ok:
            problems.append(_problem(
                "udp_blocked", "The board's UDP replies are dropped",
                f"The board answers TCP at {board} but not UDP identify: a firewall on this PC "
                f"drops its UDP replies ({a.shown} is {a.category or 'without a profile'}). "
                "Allow UDP from the board's network.",
                [firewall_rule(str(net))], adapter=a.alias))
    if blocks:
        names = ", ".join(sorted({b["DisplayName"] or b["Name"] for b in blocks}))
        problems.append(_problem(
            "python_blocked", "Windows Firewall blocks this Python",
            f"An inbound Block rule for Python ({names}: made when a Windows Security Alert "
            "for Python was cancelled) wins over any allow rule, so the board's UDP replies "
            "never reach Harness Manager. Turn it off:",
            [f"Disable-NetFirewallRule -Name {_ps(b['Name'])}" for b in blocks]))
    if identify_ok is False and tcp_ok and not any(
            p["code"] in ("public_profile", "udp_blocked", "python_blocked") for p in problems):
        out["notes"].append("identify got no reply while TCP works; Windows' own firewall "
                            "allows it here, so look at other security software")
    out["ok"] = not problems
    return out


def check(host: str, *, platform: str | None = None, run: winps.Runner | None = None,
          identify_ok: bool | None = None, tcp_ok: bool | None = None,
          exes: Sequence[str] | None = None) -> dict[str, Any] | None:
    """The Windows network check for a board at ``host``; None off Windows. Never raises:
    PowerShell failing is the problem ``unknown`` with its reason."""
    if not winps.is_windows(platform):
        return None
    try:
        doc = winps.run_json(network_ps(exes if exes is not None else _exes()),
                             what="read the network adapters", capability=CAPABILITY, run=run)
    except HarnessError as exc:
        why = (getattr(exc, "reason", "") or exc.message).rstrip(". ")
        return {"platform": "win32", "host": host, "network": "", "adapter": "",
                "category": "", "ok": False, "notes": [], "admin_how": ADMIN_HOW,
                "problems": [_problem("unknown", "This PC's network could not be read",
                                      f"{why}. Check by hand: the board's adapter needs an "
                                      f"address on the board's /24, and a Private network "
                                      "profile (Get-NetConnectionProfile)")]}
    adapters, blocks = parse(doc)
    return diagnose(host, adapters, blocks, identify_ok=identify_ok, tcp_ok=tcp_ok)


def lines(found: dict[str, Any] | None) -> list[str]:
    """The check as the CLI prints it (``probe``, ``info``): each problem, then its
    Administrator PowerShell, one command per line."""
    if not found or not found.get("problems"):
        return []
    out = [f"this PC's network ({found.get('host')}, Windows):"]
    for p in found["problems"]:
        out.append(f"  {p['title']}: {p['text']}")
        if p.get("admin"):
            out.append(f"    in PowerShell as Administrator ({ADMIN_HOW}):")
            out += [f"      {c}" for c in p["admin"]]
        if p.get("alternative"):
            out.append("    or instead:")
            out += [f"      {c}" for c in p["alternative"]]
        if p.get("gui"):
            out.append(f"    {p['gui']}")
    out.append("  Harness Manager never runs these itself.")
    return out


Checker = Callable[..., "dict[str, Any] | None"]
