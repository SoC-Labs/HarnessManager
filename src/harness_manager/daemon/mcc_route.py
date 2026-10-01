"""The board's Debug USB route (lane UI2-API-HUB, UI v2 gap G2): where its MCC is reached.

The Debug USB cable carries the board controller (the MCC console, tty_00), the config microSD
and the FPGA UART lanes (tty_01..03, the harness console on tty_02). Where that cable goes
decides what Harness Manager can do with them, and the UI v2 header, sidebar and Board >
Connections say it in one word:

- ``hub``: the cable goes to a lab hub. The MCC is reached ON the hub (a ``hub-mcc://`` link:
  pyverify's tools run there; never an fpgahub share on tty_00, MCC-FIX);
- ``pc``: the cable is on this machine (a USB serial link to the MCC console);
- ``self``: the cable loops back into the board's own USB host, or the harness reaches the MCC
  in fabric: the harness reports ``mccif``/``mcc_local`` (net-protocol v0.18);
- ``none``: no route to the MCC (Ethernet only);
- ``unknown``: a board that is not open and whose links do not say (only in ``GET /boards``).

``mcc_route_reason`` says it in a sentence. Nothing here contacts the board or the hub: an open
board is judged from its session (its controller adapter and links, and the harness's MCC
route as last read), any other from its candidate's links.

A board pack may answer for itself with an optional ``session.mcc_route() -> (route, reason)``
(duck-typed, like ``capability_reasons``); otherwise the rules above read the links:
an FPGA-lane link names its FT4232H interface ``if01``..``if03`` (``harness_manager_mps3.usb``),
so any other USB serial link that is not a ``hub://`` share is the MCC's own.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from harness_manager.core.errors import HarnessError
from harness_manager.core.model import LinkKind

log = logging.getLogger(__name__)

HUB, PC, SELF, NONE, UNKNOWN = "hub", "pc", "self", "none", "unknown"
ROUTES = (HUB, PC, SELF, NONE, UNKNOWN)
#: ``hub-mcc://HOST/TARGET/dev/.../tty_00``: the MCC reached on the hub (MCC-FIX).
HUB_MCC_SCHEME = "hub-mcc://"
_LANE = re.compile(r"\bif0[1-3]\b")
#: The harness's own MCC route features (net-protocol v0.18, ``harness_manager_mps3.harness_mcc``).
HARNESS_MCC_FEATURES = ("mccif", "mcc_local", "mcc")


def _hub_of(address: str) -> str:
    rest = address[len(HUB_MCC_SCHEME):]
    return rest.split("/", 1)[0]


def _mcc_links(links: Any) -> tuple[str, str]:
    """``(hub host or "", MCC serial address or "")`` from a board's links."""
    hub, serial = "", ""
    for lk in links or ():
        addr = str(getattr(lk, "address", "") or "")
        kind = getattr(lk, "kind", None)
        if addr.startswith(HUB_MCC_SCHEME) and not hub:
            hub = _hub_of(addr)
        elif kind == LinkKind.USB_SERIAL and not addr.startswith("hub://") and not serial \
                and not _LANE.search(str(getattr(lk, "detail", "") or "")):
            serial = addr
    return hub, serial


def from_links(links: Any, features: Any = ()) -> tuple[str, str]:
    """``(route, reason)`` for a board that is not open: from its candidate's links (and the
    features its probe read), never a contact. ``unknown`` when they do not say."""
    hub, serial = _mcc_links(links)
    if hub:
        return HUB, (f"the Debug USB goes to the hub {hub}: the MCC is reached there "
                     "(its reads and REBOOT run on the hub)")
    if serial:
        return PC, f"the Debug USB is on this machine: the MCC console is {serial}"
    feats = [f for f in HARNESS_MCC_FEATURES if f in tuple(features or ())]
    if feats:
        return SELF, (f"the harness reaches the MCC itself (feature {feats[0]!r}); its route "
                      "is read when the board is open")
    return UNKNOWN, "not known until the board is opened"


def _harness_route(session: Any) -> str:
    """The route the harness's ``mcc status`` last reported (no board contact), else ""."""
    hm = getattr(session, "harness_mcc", None)
    last = getattr(hm, "_last", None) if hm is not None else None
    status = last[0] if isinstance(last, tuple) and last else None
    return str(getattr(status, "route", "") or "")


def of_session(session: Any, identity: Any = None) -> tuple[str, str]:
    """``(route, reason)`` for an open board (``GET /boards/{bid}/session`` and ``info``)."""
    hook = getattr(session, "mcc_route", None)
    if callable(hook):
        try:
            route, reason = hook()
            if route in ROUTES:
                return str(route), str(reason or "")
        except HarnessError as exc:
            log.debug("mcc_route of %s: %s", getattr(session, "candidate", None), exc)
    links = getattr(getattr(session, "candidate", None), "links", ())
    hub, serial = _mcc_links(links)
    try:
        controller = getattr(session, "controller", None)
    except HarnessError:
        controller = None
    if hub:
        return HUB, (f"the Debug USB goes to the hub {hub}: the MCC is reached there (its reads "
                     "and REBOOT run on the hub, one reader on tty_00)")
    if controller is not None and serial:
        return PC, f"the Debug USB is on this machine: the MCC console is {serial}"
    feats = tuple(getattr(identity, "features", ()) or ())
    have = [f for f in HARNESS_MCC_FEATURES if f in feats]
    if have:
        route = _harness_route(session)
        if route == "none":
            return NONE, ("the harness reports no route to the MCC (`mcc status`: none: no "
                          "in-fabric SCC in this static, and no USB loopback cable)")
        how = {"loopback": "the Debug USB loops back into the board's own USB host",
               "scc": "the harness reaches the MCC in fabric (the SCC)"}.get(
            route, f"the harness reaches the MCC itself (feature {have[0]!r})")
        return SELF, how + ("" if route else "; its route is read when needed")
    if controller is not None:
        return PC, "the board controller is reached from this machine"
    return NONE, "no Debug USB here or on a hub: Ethernet only (no MCC console, config SD or REBOOT)"
