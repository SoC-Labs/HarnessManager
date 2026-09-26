"""Settings over the daemon API: read, change, unset, secrets, test (lane SET-API).

docs/API.md "Settings" is the contract; ``docs/design/SETTINGS.md`` §8 the design. Every
route is behind the Bearer token, like the rest of ``/api/v1``. The work is
``harness_manager.settings.ops``, which the CLI also runs when no service is up, so the
two give the same JSON.

| Route | |
|---|---|
| ``GET /settings`` ``?section=&key=&all=1`` | ``{schema_version, files, policy, problems, sections, instances, rows}`` |
| ``GET /settings/schema`` | the rows without values, every pack's too |
| ``PUT /settings`` ``{KEY: value, …}`` | all or nothing: ``{rows, keys, apply, applies}`` |
| ``DELETE /settings/{key}`` | back to the next layer: ``{rows, keys, apply, applies, changed}`` |
| ``PUT /settings/secrets/{key}`` ``{value}`` · ``DELETE …`` | ``{key, secret, rows, …}``: the status, never the value |
| ``POST /settings/test`` ``{section, name?, table?}`` | the section's test report, or ``testable: false``; a slow tester is a 202 job ``settings_test`` |

**Events.** ``settings.changed {keys, apply, applies, source: "api"}`` after every change
that wrote something (``PUT /update/settings`` sends it too). It never carries a value.
The service's readers follow it (lane SET-WIRE): ``settings.runtime.watch`` drops their
cache, so a ``live`` row applies at its next use; the update checker and the update
service's token follow it too. ``reopen`` rows apply at the next board open, ``restart``
rows after a restart, as the reply's ``apply`` says.

The service keeps one ``SettingsContext`` (``d.settings``): its own state dir, its own
environment (the one the GUI's values come from), the OS's policy file, and its engine's
packs (``Engine.settings_resolver``; ``settings.packs.for_engine`` for the demo engine). A
test points ``d.settings.policy_path`` (or ``env``) somewhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Query

from harness_manager.core.errors import UsageError
from harness_manager.core.events import Event
from harness_manager.settings import ops, runtime, testers

from .app import _JSON, JsonBody, RouteContext, _obj, ok

#: The board id of the engine-wide test job.
ENGINE = ""
#: A PUT /settings/secrets body holds only this.
SECRET_FIELDS = frozenset({"value"})
_TRUE = ("1", "true", "yes", "on")


def _flag(value: str) -> bool:
    return value.strip().lower() in _TRUE


def settings_context(d: Any) -> ops.SettingsContext:
    """The service's context, made once (``d.settings``)."""
    existing = getattr(d, "settings", None)
    if isinstance(existing, ops.SettingsContext):
        return existing
    sctx = ops.SettingsContext(state_dir=d.state_dir, engine=d.engine)
    d.settings = sctx
    return sctx


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    sctx = settings_context(d)

    # SET-WIRE: the readers' cache follows every change (this route's, /update/settings').
    runtime.watch(d.bus)

    def changed(result: dict[str, Any]) -> None:
        if result.get("changed", True):
            d.bus.publish(Event("settings.changed", "", ops.changed_event(result)))

    # -- reading ------------------------------------------------------------------------------

    @api.get("/settings")
    def get_settings(section: str = "", key: str = "", all_: str = Query("", alias="all"),
                     dev: str = "") -> Any:
        return _JSON(ok(**ops.listing(sctx, section=section or None, key=key or None,
                                      include_dev=_flag(all_) or _flag(dev))))

    @api.get("/settings/schema")
    def get_schema() -> Any:
        return _JSON(ok(**ops.schema(sctx)))

    # -- secrets (before /settings/{key:path}, which would take "secrets/...") ---------------

    @api.put("/settings/secrets/{key:path}")
    def put_secret(key: str, body: JsonBody = None) -> Any:
        b = _obj(body)
        extra = sorted(set(b) - SECRET_FIELDS)
        if extra:
            raise UsageError(f"unknown fields: {', '.join(extra)}", hint='send {"value": "..."}')
        if "value" not in b:
            raise UsageError('the request needs "value"', hint='send {"value": "..."}')
        result = ops.set_secret(sctx, key, b["value"])
        changed(result)
        return _JSON(ok(**result))

    @api.delete("/settings/secrets/{key:path}")
    def delete_secret(key: str) -> Any:
        result = ops.delete_secret(sctx, key)
        changed(result)
        return _JSON(ok(**result))

    # -- testing ---------------------------------------------------------------------------------

    @api.post("/settings/test")
    def post_test(body: JsonBody = None) -> Any:
        b = _obj(body)
        section = b.get("section", b.get("kind"))
        if not isinstance(section, str) or not section:
            raise UsageError('say which section to test: {"section": "hubs", "name": "lab"}')
        name = b.get("name") or ""
        if not isinstance(name, str):
            raise UsageError("name must be a string")
        table = b.get("table")
        if table is not None and not isinstance(table, dict):
            raise UsageError("table must be an object (the settings to test before saving them)")
        sid, tester = ops.find_tester(sctx, section)
        if tester is None:
            return _JSON(ok(**testers.not_testable(sid, name)))
        if not tester.job:
            return _JSON(ok(**ops.test(sctx, sid, name, table)))
        testers.check(tester, name, table)            # a missing name: 400 before the job

        def run(progress: Callable[[str, int, int], None]) -> Any:
            return ops.test(sctx, sid, name, table, progress=progress)

        return ctx.accepted(d.jobs.submit("settings_test", ENGINE, run))

    # -- changing --------------------------------------------------------------------------------

    @api.put("/settings")
    def put_settings(body: JsonBody = None) -> Any:
        result = ops.set_values(sctx, body)
        changed(result)
        return _JSON(ok(**result))

    @api.delete("/settings/{key:path}")
    def delete_setting(key: str) -> Any:
        result = ops.unset(sctx, key)
        changed(result)
        return _JSON(ok(**result))
