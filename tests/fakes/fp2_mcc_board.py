"""FIX-PACK-2 item 2: a harness that answers net-protocol v0.18's ``mcc`` verb (``act: status``).

HM's own model of the v0.18 shape the lead gave on 2026-09-28 (no platform commit yet): ONE verb
``{"op": "mcc", "act": ACT}``, replies carry ``"route": "scc" | "loopback" | "none"``; the image
lists ``mccif`` (in-fabric SCC) and/or ``mcc_local`` (the USB loopback) in ``version.features``.
Only ``status`` is modelled; any other act is refused, so a test proves HM never sends one.
``mcc_verb=False`` is an image that lists the feature but answers ``unknown op``.
"""

from __future__ import annotations

from typing import Any

from pyverify.testing.fakeshell import FakeShell

from tests.fakes.virtual_board import PRODUCT_V011_FEATURES


class MccBoard(FakeShell):
    def __init__(self, *args: Any, mcc_route: str = "scc", mcc_verb: bool = True,
                 **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.mcc_route = mcc_route
        self.mcc_verb = mcc_verb
        self.mcc_requests: list[dict[str, Any]] = []

    def handle_control(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        if request.get("op") != "mcc":
            return super().handle_control(request, peer=peer)
        with self._lock:
            self.mcc_requests.append(dict(request))
        if not self.mcc_verb:
            return {"ok": False, "err": "unknown op 'mcc'"}
        act = request.get("act")
        if act == "status":
            return {"ok": True, "op": "mcc", "act": "status", "route": self.mcc_route,
                    "scc": self.mcc_route == "scc", "loopback": self.mcc_route == "loopback"}
        return {"ok": False, "op": "mcc", "act": act, "route": self.mcc_route,
                "err": f"the FIX-PACK-2 fake models only act status (got {act!r})"}


def mcc_board(extra: tuple[str, ...] = ("mccif",), **kw: Any) -> MccBoard:
    """A started bare-metal v0.11 board plus ``extra`` features, on ephemeral ports. The
    vendored FakeShell validates its features against the names it knows (v0.8-v0.13), so
    the v0.18 names are set after init, as its docstring says a test does."""
    kw.setdefault("features", PRODUCT_V011_FEATURES)
    fake = MccBoard.ephemeral(**kw)
    fake.features = tuple(fake.features) + tuple(extra)
    fake.start()
    return fake
