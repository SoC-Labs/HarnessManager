"""What Windows PowerShell answers to ``netcheck.network_ps`` on a lab laptop (lane WINDOWS),
as FIXTURES: never PowerShell. Shapes as Windows PowerShell 5.1 gives them
(``ConvertTo-Json`` of ``[pscustomobject]``s; ``Category`` as a name or the CIM number)."""

from __future__ import annotations

import json
from typing import Any


def ip(alias: str, index: int, addr: str, prefix: int = 24) -> dict:
    return {"Alias": alias, "Index": index, "Ip": addr, "Prefix": prefix}


def adapter(alias: str, index: int, status: str = "Up", desc: str = "") -> dict:
    return {"Alias": alias, "Index": index, "Status": status, "Description": desc}


def profile(alias: str, index: int, category: Any) -> dict:
    return {"Alias": alias, "Index": index, "Category": category, "Name": "Unidentified network"}


def laptop(*, board_ip: str | None = None, category: Any = "Public", apipa: int = 1,
           blocks: list[dict] | None = None) -> dict:
    """Wi-Fi on the campus network (DomainAuthenticated); 'Ethernet 2', a USB adapter cabled to
    the board: ``board_ip`` given = it has 192.168.10.1/24 with ``category``; else it has
    only a 169.254 address (``apipa`` adapters like it)."""
    ips = [ip("Wi-Fi", 12, "10.9.1.23", 22), ip("Loopback Pseudo-Interface 1", 1, "127.0.0.1", 8)]
    adapters = [adapter("Wi-Fi", 12, desc="Intel(R) Wi-Fi 6 AX201 160MHz"),
                adapter("Ethernet", 7, status="Disconnected", desc="Intel(R) Ethernet I219-LM")]
    profiles = [profile("Wi-Fi", 12, "DomainAuthenticated")]
    for n in range(apipa if board_ip is None else 1):
        alias = "Ethernet 2" if n == 0 else f"Ethernet {n + 2}"
        adapters.append(adapter(alias, 20 + n, desc="Realtek USB GbE Family Controller"))
        if board_ip is None:
            ips.append(ip(alias, 20 + n, f"169.254.{12 + n}.3", 16))
        else:
            ips.append(ip(alias, 20 + n, "192.168.10.1"))
            profiles.append(profile(alias, 20 + n, category))
    return {"ips": ips, "profiles": profiles, "adapters": adapters, "blocks": blocks or []}


def answer(doc: dict) -> bytes:
    return b"\xef\xbb\xbf" + json.dumps(doc).encode()
