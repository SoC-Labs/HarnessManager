"""BRINGUP-USB's routes (docs/API.md "Bring-up over the Debug USB", ``bringup_api.py``) in the
T14 mock.

The mock serves them so the page's contract holds over it too, with the service's own code
where it touches nothing: ``GET /bringup`` (the switches), ``POST /bringup/scan`` (the real
scan over the mock's engine: the classic demo has no board on USB only, so it says what to
check) and ``POST /bringup/bundle`` (the real check, into a temporary directory of the
mock's own). A write or a witness is the real service's (tests/web/test_bringup_browser.py
drives them over the real daemon): here they are 422 UNAVAILABLE, saying so, and nothing is
ever written.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI
from starlette.routing import Mount

from harness_manager.cli.output import with_data
from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.services import bringup

API = "/api/v1"
JsonBody = Annotated[Any, Body()]
WHY = "the T14 mock writes no SD and waits for no board (the real service does)"


def register(app: FastAPI, state: Any, ok: Any) -> None:
    work = Path(tempfile.mkdtemp(prefix="hm-bringup-mock-"))

    @app.get(f"{API}/bringup")
    def status() -> dict[str, Any]:
        out = bringup.status(state.engine, None)
        out["sd_flash"]["routes"] = False
        return ok(**out)

    @app.post(f"{API}/bringup/scan")
    def scan(body: JsonBody = None) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        out = bringup.scan(state.engine, host=str(b.get("host") or bringup.DEFAULT_HOST),
                           ask=bool(b.get("ask_mcc", False)), timeout_s=0.2)
        state.remember(out.pop("candidates"))
        return ok(**out)

    @app.post(f"{API}/bringup/bundle")
    def bundle(body: JsonBody = None) -> dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        if not isinstance(b.get("path"), str):
            raise UsageError("the request needs 'path': the bundle's folder or .zip")
        chk = bringup.check_bundle(b["path"], work)
        if chk.refused:
            raise with_data(RefusedError(f"refusing {chk.path}: {chk.problems[0]}",
                                         hint="nothing was written"), check=chk.as_dict())
        return ok(check=chk.as_dict())

    @app.post(f"{API}/boards/{{bid:path}}/bringup/install")
    def install(bid: str, body: JsonBody = None) -> dict[str, Any]:
        state.session(bid)
        raise UnavailableError("bring-up install", WHY)

    @app.get(f"{API}/boards/{{bid:path}}/bringup/proposal")
    def identity(bid: str, ip: str | None = None) -> dict[str, Any]:
        s = state.session(bid)
        host = ((ip or "").strip() or bringup.DEFAULT_HOST).split("/", 1)[0]
        from harness_manager.services import identity_assign as IA

        # lane IDENTITY: the MPS3 rules, the pool never asked (no datagram leaves a test)
        policy = IA.IdentityPolicy(pack="mps3", reserved_mac_prefixes=("02:00:00",),
                                   ip_pool="192.168.10.110-199",
                                   reserved_ips=("192.168.10.101",))
        proposal = bringup.propose_identity(
            bringup.mcc_serial(getattr(s, "candidate", None), list_ports=list), policy=policy)
        return ok(board_id=bid, proposal=proposal,
                  command=bringup.identity_command(host, proposal))

    @app.post(f"{API}/bringup/card-reader")
    def card_reader(body: JsonBody = None) -> dict[str, Any]:
        raise UnavailableError("card reader", WHY)

    @app.post(f"{API}/boards/{{bid:path}}/bringup/witness")
    def witness(bid: str, body: JsonBody = None) -> dict[str, Any]:
        state.session(bid)
        raise UnavailableError("bring-up witness", WHY)


def attach(app: FastAPI, ok: Any) -> FastAPI:
    """Register on a built mock app, keeping its UI mount (``/``) last so it shadows nothing."""
    register(app, app.state.daemon, ok)
    routes = app.router.routes
    for mount in [r for r in routes if isinstance(r, Mount)]:
        routes.remove(mount)
        routes.append(mount)
    return app
