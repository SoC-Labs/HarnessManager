"""Is a recorded host name this machine? (HM 1.2, MAC-HOST.)

Lock files, ``daemon.json`` and job files record ``socket.gethostname()`` and later ask "was
that written here?". macOS answers ``DM-MacBookPro`` on one network and ``DM-MacBookPro.local``
on another (16 Oct: a 1.1 daemon's own lock read as another machine's, so ``daemon stop`` said
"not here" and the stale lock was never removed). A name matches when its short name (before
the first dot, case-insensitive) is this machine's: ``X``, ``X.local`` and ``X.example.org``
are one host. Never ``getfqdn()``: a DNS lookup that can hang on a laptop off its network.
"""

from __future__ import annotations

import socket


def short_host(name: str | None) -> str:
    """``X`` for ``X``, ``X.local``, ``X.example.org.`` (case-folded); "" for none."""
    return str(name or "").strip().rstrip(".").split(".", 1)[0].casefold()


def same_host(a: str | None, b: str | None) -> bool:
    """Do two recorded names name one machine? Two empty names do not."""
    sa = short_host(a)
    return bool(sa) and sa == short_host(b)


def is_this_host(name: str | None) -> bool:
    """Was ``name`` recorded on this machine (``socket.gethostname()``, any suffix)?"""
    return same_host(name, socket.gethostname())
