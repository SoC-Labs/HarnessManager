"""The Linux harness's SSH claim in the daemon API (lane LINUX-CLAIM), loaded through
``app.EXTENSIONS``.

docs/API.md "SSH claim (Linux harness)" (bearer auth and the error envelope as everywhere):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/claim?refresh=&hub=`` | ``{board_id, claim}``: ``claim`` is ``BoardInfo.claim`` (null on bare metal). ``refresh=true`` asks the board now, through the hub when there is one. ``hub=true`` (the app's Access card, on load): on a hub board, asks the hub, reusing an answer of the last minute |
| ``POST /boards/{bid}/claim`` ``{confirm: true, key?, adopt?, replace_host_key?, expect_host_key?}`` (``expect_host_key``: with ``replace_host_key``, the exact fingerprint shown to the user; the claim pins it or nothing) | 202 job ``claim``; the result is ``{board_id, claim}`` with ``claim.action`` ``claimed`` or ``adopted`` |
| ``POST /boards/{bid}/repin`` ``{confirm: true, fingerprint}`` | 202 job ``repin``; pins exactly ``fingerprint`` (the new key the user was shown) when identify and ssh both show it; the result is ``{board_id, claim}`` with ``claim.action`` ``repinned``. The lease holder only |
| ``GET /boards/{bid}/claim/keys`` | ``{board_id, keys: [{fingerprint, type, label, mine}]}``: the keys the board accepts, read over the claim SSH (claimed by this Harness Manager only; 409 otherwise). Fingerprints only |
| ``POST /boards/{bid}/claim/keys/check`` ``{key, label?}`` | ``{board_id, key: {type, fingerprint, label}}``: a pasted PUBLIC key checked, nothing sent. A bad or private key is 400 (usage) and is never echoed or kept |
| ``POST /boards/{bid}/claim/keys`` ``{confirm: true, key, label?, expect_fingerprint}`` | 202 job ``claim_key``; result ``{board_id, key: {action: added or already_there, fingerprint, label, keys}}``. ``expect_fingerprint`` is the one ``keys/check`` showed; a different key is refused before anything is sent. The lease holder only |
| ``POST /boards/{bid}/claim/keys/remove`` ``{confirm: true, fingerprint}`` | 202 job ``claim_key``; result ``{board_id, key: {action: removed, fingerprint, keys}}``. Refuses the key this PC uses, an unknown key and the last key. The lease holder only |
| ``GET /boards/{bid}/ssh?command=`` | ``{board_id, argv}``: the pinned ``ssh [-J HUB] -l root BOARD`` (nothing is run) |

Adopt is ``POST /boards/{bid}/claim`` with ``{confirm: true, adopt: true}`` (the Access card's
"Adopt this board": the same checks as ``board claim --adopt``).

Rules:

- **Never automatic, always confirmed.** ``POST`` without ``"confirm": true`` is 409 REFUSED
  before anything reaches the board.
- **The lease holder only**, on a board behind a hub (hub_api's lease service, shared): 409
  HELD naming the holder, before the job starts.
- ``POST`` is a board job (409 HELD while another job runs). ``GET`` never takes the board's
  control port: identify is UDP, and the hub round trip happens only with ``refresh`` or
  ``hub`` (an ssh to the hub that runs identify there: never the lease, never tty_00).

Events: ``board.claim`` ``{state, claimed, host_key, route, action?}``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import RefusedError, UnavailableError

from .app import _JSON, JsonBody, RouteContext, _bool, _obj, ok
from .xvc_api import _flag

CAPABILITY = "ssh_claim"


def _opt_str(b: dict[str, Any], key: str) -> str | None:
    value = b.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        from harness_manager.core.errors import UsageError

        raise UsageError(f"{key} must be a string")
    return value


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    def service() -> Any:
        svc = getattr(d.engine, "board_claim", None)
        if svc is None:                                     # an engine without one (the demo)
            raise UnavailableError(CAPABILITY, "this engine has no SSH claim service")
        if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
            svc.leases = d.leases                           # hub_api's: one view of "mine"
        return svc

    @api.get("/boards/{bid:path}/claim")
    def claim_status(bid: str, refresh: str | None = None, hub: str | None = None) -> Any:
        s = ctx.board(bid)
        svc = service()
        st = svc.refresh(s) if _flag(refresh, "refresh") else \
            svc.check_hub(s) if _flag(hub, "hub") else svc.status(s)
        return _JSON(ok(board_id=bid, claim=st))

    @api.post("/boards/{bid:path}/claim")
    def claim(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        if not _bool(b, "confirm", False):
            raise RefusedError("a claim needs a confirmation: it gives your key root on the "
                               "board and locks its slots to that key",
                               hint="send {\"confirm\": true}")
        key = _opt_str(b, "key")
        adopt = _bool(b, "adopt", False)
        replace = _bool(b, "replace_host_key", False)
        expect = _opt_str(b, "expect_host_key")
        if expect and not replace:
            raise RefusedError("expect_host_key goes with replace_host_key: it names the exact "
                               "key the replacement pins")
        svc = service()
        with d.gates.op(bid):                               # 409 HELD while a job runs
            svc.check_claimable(s)                          # 422 on a board with no claim
            svc.check_lease(s)                              # 409 HELD naming the holder

        def run(progress: Callable[[str, int, int], None]) -> Any:
            st = svc.claim(s, confirm=True, key=key, adopt=adopt, replace_host_key=replace,
                           expect_host_key=expect, progress=lambda text: progress(text, 0, 0))
            return {"board_id": bid, "claim": st}

        return ctx.accepted(d.jobs.submit("claim", bid, run))

    @api.post("/boards/{bid:path}/repin")
    def repin(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        if not _bool(b, "confirm", False):
            raise RefusedError("a re-pin needs a confirmation: Harness Manager will trust the "
                               "new SSH host key for this board",
                               hint="send {\"confirm\": true, \"fingerprint\": \"SHA256:...\"}")
        fingerprint = _opt_str(b, "fingerprint")
        if not fingerprint:
            raise RefusedError("a re-pin names the exact fingerprint you approved",
                               hint="send {\"fingerprint\": \"SHA256:...\"}")
        svc = service()
        with d.gates.op(bid):
            svc.check_claimable(s)
            svc.check_lease(s, what="re-pinning")

        def run(progress: Callable[[str, int, int], None]) -> Any:
            st = svc.repin(s, confirm=True, fingerprint=fingerprint,
                           progress=lambda text: progress(text, 0, 0))
            return {"board_id": bid, "claim": st}

        return ctx.accepted(d.jobs.submit("repin", bid, run))

    # keys routes first: "{bid:path}/claim" is greedy, so the longer paths are registered before
    @api.get("/boards/{bid:path}/claim/keys")
    def keys_list(bid: str) -> Any:
        s = ctx.board(bid)
        return _JSON(ok(board_id=bid, keys=service().keys_list(s)))

    @api.post("/boards/{bid:path}/claim/keys/check")
    def keys_check(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        info = service().keys_preview(s, _opt_str(b, "key") or "", _opt_str(b, "label") or "")
        return _JSON(ok(board_id=bid, key=info))

    @api.post("/boards/{bid:path}/claim/keys/remove")
    def keys_remove(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        if not _bool(b, "confirm", False):
            raise RefusedError("removing a key takes its owner's access away: it needs a "
                               "confirmation", hint="send {\"confirm\": true, \"fingerprint\": "
                                                    "\"SHA256:...\"}")
        fingerprint = _opt_str(b, "fingerprint")
        if not fingerprint:
            raise RefusedError("name the key to remove by its fingerprint",
                               hint="send {\"fingerprint\": \"SHA256:...\"}")
        svc = service()
        with d.gates.op(bid):
            svc.check_claimable(s)
            svc.check_lease(s, what="removing a key")

        def run(progress: Callable[[str, int, int], None]) -> Any:
            out = svc.keys_remove(s, confirm=True, fingerprint=fingerprint,
                                  progress=lambda text: progress(text, 0, 0))
            return {"board_id": bid, "key": out}

        return ctx.accepted(d.jobs.submit("claim_key", bid, run))

    @api.post("/boards/{bid:path}/claim/keys")
    def keys_add(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        if not _bool(b, "confirm", False):
            raise RefusedError("adding a key gives its owner root on the board: it needs a "
                               "confirmation", hint="send {\"confirm\": true, \"key\": \"ssh-...\", "
                                                    "\"expect_fingerprint\": \"SHA256:...\"}")
        key = _opt_str(b, "key") or ""
        label = _opt_str(b, "label") or ""
        expect = _opt_str(b, "expect_fingerprint") or ""
        svc = service()
        with d.gates.op(bid):
            svc.check_claimable(s)
            svc.keys_preview(s, key, label)               # a bad or private key stops here
            svc.check_lease(s, what="adding a key")

        def run(progress: Callable[[str, int, int], None]) -> Any:
            out = svc.keys_add(s, confirm=True, key=key, label=label, expect_fingerprint=expect,
                               progress=lambda text: progress(text, 0, 0))
            return {"board_id": bid, "key": out}

        return ctx.accepted(d.jobs.submit("claim_key", bid, run))

    @api.get("/boards/{bid:path}/ssh")
    def ssh_argv(bid: str, command: str | None = None) -> Any:
        import shlex

        s = ctx.board(bid)
        argv = service().ssh_argv(s, shlex.split(command) if command else ())
        return _JSON(ok(board_id=bid, argv=argv))
