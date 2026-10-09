"""The board's network identity in the daemon API (lane BOARD-ID), loaded through
``app.EXTENSIONS``.

docs/API.md "Board identity" (bearer auth and the error envelope as everywhere):

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/identity?refresh=`` | ``{board_id, identity}``: ``BoardInfo.net_identity`` read now (the board: one control-port read, or identify; the hub record once per session). ``refresh=true`` asks the hub again, and for its other targets |
| ``GET /boards/{bid}/identity/proposal?label=&mac=&ip=`` | lane IDENTITY: ``{board_id, proposal}``, what "Name this board" shows: the name upper-cased and checked (1-16 of A-Z, 0-9, -), ``mac`` (``random`` (default for an image-default MAC), ``keep`` or a value), ``ip`` (``auto`` from the pack's pool, ``keep`` or a value), the changes, the phrase, the same-/24 line, the hub guard and the notes. Nothing is set or reserved |
| ``POST /boards/{bid}/identity`` ``{confirm, from_hub?, label?, ip?, mac?, hostname?, unset?, clear?, wait_s?, hub_fixed?, other_subnet?, confirm_subnet?, dry_run?}`` | 202 job ``identity``; the result is ``{board_id, action, changes, set, reboot, verified, identity, notes, moved, address}``. ``dry_run: true`` (no ``confirm``): 200 ``{board_id, preflight: {want, plan, notes, identity, address}}``, the same checks and choices, nothing sent |

Rules:

- **Never automatic.** ``confirm`` is the typed phrase (``identity.fix.phrase``: the new
  label, or ``IDENTITY <board_id>``); without it 409 REFUSED before anything is sent, and a
  phrase that does not match the plan fails the job with REFUSED, nothing sent.
- **Refusals before the job:** 409 HELD while another job runs; 409 HELD naming the holder
  when the board is behind a hub and the lease is not this client's; 422 UNAVAILABLE on bare
  metal or an image without the identity verbs; 409 REFUSED on a netbooted board (its
  identity is the stage0 bake) or a claim that is not this Harness Manager's; 400 USAGE for
  a value the board would refuse. In the job: 409 HELD while the card is written or read
  back (the reset guard).
- **V7-ALIGN (net-protocol v0.16 as shipped).** The board's order: the claim (``locked``),
  then no card (``no_persist``), then a bad value (``invalid``): a 400 for a bad value comes
  only when the board could take a change at all. ``unset`` (a list of field names) drops
  those keys from the board's own setting (the wire's ``""``); a field sent as ``""`` is
  still "not given", as before.
- **The reboot is the harness's own ``reboot`` verb** (warm), never an MCC REBOOT.

Events: ``board.net_identity`` ``{status, reported, hub}`` when what the board reports, or
its verdict, changes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.services import board_identity as BI

from .app import _JSON, JsonBody, RouteContext, _bool, _obj, ok
from .xvc_api import _flag


def _unset(b: dict[str, Any]) -> list[str]:
    """``unset``: the fields to drop from the board's override (V7-ALIGN)."""
    value = b.get("unset")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        raise UsageError("unset must be a list of field names", hint="label, hostname, ip, mac")
    bad = [k for k in value if k not in BI.FIELDS]
    if bad:
        raise UsageError(f"unset: {bad[0]!r} is not an identity field",
                         hint="label, hostname, ip, mac")
    return list(dict.fromkeys(value))


def _opt_str(b: dict[str, Any], key: str) -> str | None:
    value = b.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise UsageError(f"{key} must be a string")
    return value


# --- ui2 api-hub (G10): identity clashes across boards, with no contact -------------------------

#: The fields a clash is found on, in the order people fix them.
CLASH_FIELDS = ("mac", "ip", "label")


def _norm(field_name: str, value: Any) -> str:
    if field_name == "mac":
        return BI.norm_mac(value)
    if field_name == "ip":
        return BI.ip_addr(value)
    return str(value or "").strip().upper()


def clash_groups(records: Mapping[str, Mapping[str, Any]], *, now: float | None = None,
                 max_age_s: float = BI.SEEN_MAX_AGE_S) -> list[dict[str, Any]]:
    """``GET /identity/clashes``: every MAC, IP or label two or more boards share, from what
    Harness Manager has seen (``<state>/identity/seen.json``: each board's last reported
    identity, and the other hub targets its last hub read listed). No board or hub is asked.

    The rules are the board identity check's own (``BI.compare``): the image-default label
    (``MPS3``, source ``default``) is "not set", never a clash; a hub record whose MAC looks
    like the hub's own adapter (``mac_suspect``) is not compared; the same board under two ids
    (the same hub target, or the same address) is one board. Records older than
    ``SEEN_MAX_AGE_S`` (14 days) are left out. Returns ``[{field, value, boards: [{board_id,
    name, kind: "board" | "hub", target, at}]}]``, MACs first, then IPs, then labels."""
    now = time.time() if now is None else now
    entries: list[dict[str, Any]] = []
    hub_seen: set[str] = set()
    for bid, rec in sorted(records.items()):
        if not isinstance(rec, Mapping) or now - float(rec.get("at") or 0) > max_age_s:
            continue
        entries.append({"board_id": bid, "name": str(rec.get("name") or ""), "kind": "board",
                        "target": str(rec.get("target") or ""),
                        "address": str(rec.get("address") or ""),
                        "at": BI._iso(float(rec.get("at") or 0)), "rec": rec,
                        "default_label": BI.label_is_default(rec.get("label"),
                                                             rec.get("label_source"))})
    board_targets = {e["target"] for e in entries if e["target"]}
    for e in list(entries):
        for o in e["rec"].get("hub_others") or []:
            target = str((o or {}).get("target") or "") if isinstance(o, Mapping) else ""
            if not target or target in hub_seen or target in board_targets:
                continue                         # a board we saw ourselves speaks for itself
            hub_seen.add(target)
            entries.append({"board_id": "", "name": target, "kind": "hub", "target": target,
                            "address": "", "at": e["at"], "rec": o, "default_label": False})
    out: list[dict[str, Any]] = []
    for f in CLASH_FIELDS:
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            if f == "label" and e["default_label"]:
                continue
            if f == "mac" and e["kind"] == "hub" and e["rec"].get("mac_suspect"):
                continue
            value = _norm(f, e["rec"].get(f))
            if value:
                groups.setdefault(value, []).append(e)
        for value, members in groups.items():
            boards = _distinct(members)
            if len(boards) >= 2:
                out.append({"field": f, "value": value,
                            "boards": [{k: b[k] for k in ("board_id", "name", "kind", "target",
                                                          "at")} for b in boards]})
    return out


def _distinct(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per physical board: the same hub target or the same address is one board."""
    kept: list[dict[str, Any]] = []
    for m in members:
        if any((m["target"] and m["target"] == k["target"])
               or (m["address"] and m["address"] == k["address"]) for k in kept):
            continue
        kept.append(m)
    return kept


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    # --- ui2 api-hub (G10) ---
    @api.get("/identity/clashes")
    def identity_clashes() -> Any:
        """Clashes across every board seen (no contact): ``{clashes, boards_seen, checked_at}``."""
        svc = getattr(d.engine, "board_identity", None)
        from harness_manager.services._unavailable import is_unavailable

        seen = svc.seen if svc is not None and not is_unavailable(svc) else \
            BI.SeenIdentities(Path(d.state_dir) / "identity")
        records = seen.all()
        return _JSON(ok(clashes=clash_groups(records), boards_seen=len(records),
                        checked_at=BI._iso()))
    # --- end ui2 api-hub ---

    def service() -> Any:
        svc = getattr(d.engine, "board_identity", None)
        from harness_manager.services._unavailable import is_unavailable

        if svc is None or is_unavailable(svc):
            raise UnavailableError(BI.CAPABILITY, "this engine has no board identity service")
        if getattr(svc, "leases", "absent") is None and getattr(d, "leases", None) is not None:
            svc.leases = d.leases                           # hub_api's: one view of "mine"
        return svc

    @api.get("/boards/{bid:path}/identity")
    def identity_status(bid: str, refresh: str | None = None) -> Any:
        s = ctx.board(bid)
        st = service().status(s, refresh=_flag(refresh, "refresh"))
        return _JSON(ok(board_id=bid, identity=st))

    # --- lane IDENTITY: "Name this board" (david 2 Oct) ---
    @api.get("/boards/{bid:path}/identity/proposal")
    def identity_proposal(bid: str, label: str | None = None, mac: str | None = None,
                          ip: str | None = None) -> Any:
        """What the dialog shows: the name checked, a random MAC, an IP from the pool, the
        rules and the guards. Nothing is set, written or reserved."""
        s = ctx.board(bid)
        return _JSON(ok(board_id=bid, proposal=service().propose(s, label=label, mac=mac,
                                                                 ip=ip)))
    # --- end lane IDENTITY ---

    @api.post("/boards/{bid:path}/identity")
    def identity_fix(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        confirm = b.get("confirm")
        dry_run = _bool(b, "dry_run", False)                # lane IDENTITY: the CLI's question
        if not dry_run and (not isinstance(confirm, str) or not confirm.strip()):
            raise RefusedError("changing a board's identity needs the typed phrase: nothing was "
                               "changed", hint='send {"confirm": "<identity.fix.phrase>"}')
        want = {k: v for k in BI.FIELDS if (v := _opt_str(b, k)) is not None}
        for k in _unset(b):
            if k in want:
                raise UsageError(f"{k} is both set and unset", hint="give one")
            want[k] = BI.DROP                               # the wire's "": drop that key
        from_hub = _bool(b, "from_hub", False)
        clear = _bool(b, "clear", False)
        hub_fixed = _opt_str(b, "hub_fixed") or ""          # lane IDENTITY: names the hub
        other_subnet = _bool(b, "other_subnet", False)      # lane IDENTITY: leave this /24
        confirm_subnet = _bool(b, "confirm_subnet", False)  # a board outside the pool's /24
        if clear and (want or from_hub):
            raise UsageError("clear goes alone", hint="clear first, then set what you want")
        if not (want or from_hub or clear):
            raise UsageError("nothing to change", hint="send from_hub, or label/ip/mac/hostname, "
                                                      "or clear")
        wait = b.get("wait_s")
        if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float))
                                 or wait <= 0):
            raise UsageError("wait_s must be a positive number of seconds")
        svc = service()
        invalid: UsageError | None = None
        try:
            # lane IDENTITY: mac "random" and ip "auto" are chosen in the job; a value of the
            # person's own meets the pack's rules too (not the image's MAC range, a /24)
            BI.validate_want({k: v for k, v in want.items()
                              if str(v).strip().lower() not in ("random", "auto")})
            svc.check_values(s, want)                       # 400 before the job ...
        except UsageError as exc:
            invalid = exc                                   # ... after the board's refusals
        with d.gates.op(bid):                               # 409 HELD while a job runs
            st = svc.status(s, cheap=True)
            if st is None:
                raise UnavailableError(BI.CAPABILITY, BI.NO_ADAPTER)
            ref = (st.get("fix") or {}).get("refusal")
            if ref:                                         # bare metal, the claim, netboot
                raise BI.refusal_error(ref["name"], ref["message"], ref.get("hint") or "")
            if invalid is not None:                         # the board's order: invalid last
                raise invalid
            if not clear:                                   # the hub and subnet guards: 409
                svc.precheck(s, st, want, from_hub=from_hub, hub_fixed=hub_fixed,
                             other_subnet=other_subnet, confirm_subnet=confirm_subnet)
            svc.check_lease(s)                              # 409 HELD naming the holder
            if dry_run:                                     # nothing sent: what would be set
                return _JSON(ok(board_id=bid, preflight=svc.preflight(
                    s, want=want or None, from_hub=from_hub, clear=clear, hub_fixed=hub_fixed,
                    other_subnet=other_subnet, confirm_subnet=confirm_subnet)))

        def run(progress: Callable[[str, int, int], None]) -> Any:
            return svc.fix(s, confirm=confirm, want=want or None, from_hub=from_hub,
                           clear=clear, wait_s=float(wait) if wait is not None else None,
                           progress=lambda text: progress(text, 0, 0), hub_fixed=hub_fixed,
                           other_subnet=other_subnet, confirm_subnet=confirm_subnet)

        return ctx.accepted(d.jobs.submit("identity", bid, run))
