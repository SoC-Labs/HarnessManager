"""The Settings dialog's Hubs section over the daemon API (lane SET-UI; docs/API.md "Hubs").

``docs/design/SETTINGS.md`` §6 and §8 ("``GET/PUT/DELETE /hubs[/{name}]``: sugar over
``hubs.*`` rows, plus discovery"). The work is SET-HUBS' ``settings/hubs.py`` and
``hubtest.py``, the same functions ``harness-manager hub`` runs; these routes add nothing but
the HTTP shape. Test connection is ``POST /settings/test {section: "hubs", name}`` (SET-API).

| Route | |
|---|---|
| ``GET /hubs`` | ``{hubs: [hub + {boards, targets_used}], inline: [{board, host, url, via}], policy}`` |
| ``PUT /hubs/{name}`` ``{transport?, host?, url?, group?, jump?, ...}`` | add it, or change it: ``{hub, keys, apply}`` |
| ``DELETE /hubs/{name}`` ``?force=1`` | ``{removed, boards}``; refused while a board uses it |
| ``POST /hubs/{name}/boards`` ``{target, board?, name?}`` | 202 job ``hub_add_board``: the target's facts (a read), then a ``boards.toml`` entry |
| ``POST /hubs/adopt`` ``{board, name?}`` | "Make this a hub": ``{board, hub, created, reused, via, changed, backup, notes}`` |

Nothing here takes, joins or releases a lease, and no reply carries a token (only
``token: {set, backend, where, reachable, why}``). Every change publishes
``settings.changed {keys, apply, applies, source: "api"}``, as the settings routes do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import Query

from harness_manager.core.errors import HarnessError, UsageError
from harness_manager.core.events import Event
from harness_manager.settings import hubs, hubtest, ops
from harness_manager.settings.schema import join_key

from .app import _JSON, JsonBody, RouteContext, _obj, ok
from .settings_api import settings_context

ENGINE = ""
_TRUE = ("1", "true", "yes", "on")


def hubs_view(r: Any) -> dict[str, Any]:
    """Every hub (the user's and the machine's) with the boards that use it, and the boards
    whose ``hub`` table is inline (each a "Make this a hub" candidate)."""
    out = []
    for hub in hubs.list_hubs(r):
        v = hub.view()
        boards = hubs.boards_using(r, hub.name)
        v["boards"] = boards
        v["targets_used"] = {
            b: r.layer.values.get(join_key(("boards", b, "hub", "target")), "mps3_01_pl")
            for b in boards}
        out.append(v)
    inline = []
    for board in hubs.inline_hub_boards(r):
        get = r.layer.values.get
        inline.append({"board": board,
                       "host": get(join_key(("boards", board, "hub", "host")), ""),
                       "url": get(join_key(("boards", board, "hub", "url")), ""),
                       "via": get(join_key(("boards", board, "via")), ""),
                       "name": hubs.default_hub_name(
                           get(join_key(("boards", board, "hub", "host")), "")
                           or _url_host(get(join_key(("boards", board, "hub", "url")), "")))})
    return {"hubs": out, "inline": inline,
            "policy": {"path": str(r.policy.path or ""), "hubs": sorted(r.policy.hubs)}}


def _url_host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def changed_keys(before: Mapping[str, Any], after: Mapping[str, Any], schema: Any) -> list[str]:
    """The declared settings a change wrote (added, changed or removed), in key order."""
    keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return [k for k in keys if schema.find(k) is not None]


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    sctx = settings_context(d)

    def announce(before: Mapping[str, Any]) -> dict[str, Any]:
        """Publish what a change wrote, as ``settings.changed`` (never a value)."""
        r = sctx.resolver()
        keys = changed_keys(before, r.layer.values, r.schema)
        applies: dict[str, list[str]] = {a: [] for a in ops.APPLY_ORDER}
        for k in keys:
            applies.setdefault(r.schema.spec(k).apply, []).append(k)
        summary = {"keys": keys, "apply": ops.strongest([a for a in applies if applies[a]]),
                   "applies": applies}
        if keys:
            d.bus.publish(Event("settings.changed", ENGINE, ops.changed_event(summary)))
        return summary

    @api.get("/hubs")
    def get_hubs() -> Any:
        return _JSON(ok(**hubs_view(sctx.resolver())))

    @api.post("/hubs/adopt")
    def adopt(body: JsonBody = None) -> Any:
        b = _obj(body)
        board = b.get("board")
        if not isinstance(board, str) or not board:
            raise UsageError('say which board: {"board": "<its key in boards.toml>"}')
        name = b.get("name") or None
        if name is not None and not isinstance(name, str):
            raise UsageError("name must be a hub name")
        r = sctx.resolver()
        before = dict(r.layer.values)
        out = hubs.adopt_inline_hub(board, r, as_name=name)
        summary = announce(before) if out.get("changed") else {"keys": [], "apply": "live"}
        return _JSON(ok(**out, keys=summary["keys"], apply=summary["apply"]))

    @api.put("/hubs/{name}")
    def put_hub(name: str, body: JsonBody = None) -> Any:
        values = dict(_obj(body))
        if "token" in values:
            raise UsageError("a hub's token is a secret",
                             hint=f"PUT /settings/secrets/hubs.{name}.token {{\"value\": ...}}")
        r = sctx.resolver()
        before = dict(r.layer.values)
        exists = hubs.check_name(name) in hubs.hub_names(r)
        hub = hubs.add_hub(name, values, r, update=exists, text=True)
        summary = announce(before)
        return _JSON(ok(hub=hub.view(), created=not exists, keys=summary["keys"],
                        apply=summary["apply"]))

    @api.delete("/hubs/{name}")
    def delete_hub(name: str, force: str = Query("")) -> Any:
        r = sctx.resolver()
        before = dict(r.layer.values)
        out = hubs.remove_hub(name, r, force=force.strip().lower() in _TRUE)
        summary = announce(before)
        if not summary["keys"]:              # a stored token only: still say the hub changed
            key = join_key(("hubs", name, "token"))
            d.bus.publish(Event("settings.changed", ENGINE, ops.changed_event(
                {"keys": [key], "apply": "reopen",
                 "applies": {"live": [], "reopen": [key], "restart": []}})))
        return _JSON(ok(**out))

    @api.post("/hubs/{name}/boards")
    def add_board(name: str, body: JsonBody = None) -> Any:
        b = _obj(body)
        target = b.get("target")
        if not isinstance(target, str) or not target:
            raise UsageError('say which target: {"target": "mps3_01_pl"}')
        hubs.check_target(target)          # 400 before the job runs `fpgahub target show`
        key = b.get("board") or None
        label = b.get("name")
        for field, v in (("board", key), ("name", label)):
            if v is not None and not isinstance(v, str):
                raise UsageError(f"{field} must be text")
        hubs.resolve_hub(hubs.check_name(name), sctx.resolver())      # 400 before the job
        sctx.refuse_in_demo("Add this board")          # it reads the target on the hub

        def run(progress: Callable[[str, int, int], None]) -> Any:
            progress("details", 0, 2)
            r = sctx.resolver()
            details: dict[str, Any] = {}
            notes: list[str] = []
            try:
                details = hubtest.target_details(name, target, resolver=r)
            except UsageError:
                raise
            except HarnessError as exc:        # a board can be written without the facts
                notes.append(f"the hub did not say {target}'s address and description "
                             f"({exc.message}); the board is written without them")
            progress("write", 1, 2)
            before = dict(r.layer.values)
            out = hubs.add_board_for_target(name, target, r, board_key=key, name=label,
                                            details=details)
            summary = announce(before)
            return {**out, "notes": [*notes, *out.get("notes", [])], "keys": summary["keys"]}

        return ctx.accepted(d.jobs.submit("hub_add_board", ENGINE, run))


__all__ = ["changed_keys", "hubs_view", "register"]
