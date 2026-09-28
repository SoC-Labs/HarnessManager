"""The service's own environment: the tool variables it started with (FIX-PACK-2 item 6).

Why this exists. The service is started once, and every CLI verb and the app then run in it
(``RemoteEngine``). On POSIX ``control._spawn`` hands it the WHOLE environment of the process
that ran ``daemon start``/``app``/``ui`` (``daemon_python`` returns ``env=None``), and
``ensure_running`` reuses a running service whatever that was: the first starter wins. A
developer variable such as ``HARNESS_MANAGER_OPENOCD`` outranks the user's ``settings.toml``
(``settings/resolve.py``), so one exported in an old terminal (``docs/HIL_B0.md`` step 0.3
said to, 23-27 Sep) rules the service for its whole life, even after the user's own shell
has dropped it. An app update's restart hands the old service's environment on too
(``update_apply``: the helper and the new service get ``os.environ`` of the old one).

So the service keeps what it started with (``capture``, in ``server.run_daemon``, logged
once to daemon.log), and says which of those variables override a setting and which hide
the user's own value (``describe``): ``GET /api/v1/daemon/env`` (``env_api.py``),
``harness-manager daemon status``, Settings > Advanced, and a one-line warning in the app
when a TOOL variable (a row of the Tools section) hides the user's setting.

Which variables: every ``HARNESS_MANAGER_*``, plus any other variable a settings row names
(``XILINX_VIVADO``, ``FPGAHUB_CLIENT_CONFIG``, ``FPGAHUB_TOKEN``). A secret's value is never
shown: a secret row's variable, or a name with TOKEN, SECRET, PASSWORD or CREDENTIAL in it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

PREFIX = "HARNESS_MANAGER_"
HIDDEN = "(set; a secret, not shown)"
TOOLS_SECTION = "Tools"
_SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _rows() -> tuple[Any, ...]:
    """Every settings row this process knows (the core's and each loaded pack's)."""
    from harness_manager.settings import runtime

    try:
        return tuple(runtime.schema().rows)
    except Exception:  # noqa: BLE001 - the rows are never worth failing a status
        log.exception("reading the settings rows failed")
        return ()


def _row_envs() -> dict[str, Any]:
    return {r.env: r for r in _rows() if getattr(r, "env", "")}


def is_secret(name: str, rows: Mapping[str, Any] | None = None) -> bool:
    rows = _row_envs() if rows is None else rows
    row = rows.get(name)
    return bool(getattr(row, "secret", False)) or any(w in name.upper() for w in _SECRET_WORDS)


def capture(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables that matter, as the process has them now (values as they are: keep this
    in memory; ``shown`` masks them for any output)."""
    env = os.environ if environ is None else environ
    names = set(_row_envs())
    return {k: str(v) for k, v in sorted(env.items()) if k.startswith(PREFIX) or k in names}


def shown(snapshot: Mapping[str, str]) -> dict[str, str]:
    """The snapshot for output: every secret's value replaced by ``HIDDEN``."""
    rows = _row_envs()
    return {k: (HIDDEN if is_secret(k, rows) else v) for k, v in snapshot.items()}


def describe(snapshot: Mapping[str, str]) -> dict[str, Any]:
    """``{env, overrides, warning}`` for the service that started with ``snapshot``.

    ``overrides``: one per variable a settings row names, ``{var, value, key, section, tool,
    in_effect, hides_yours}``: ``in_effect`` the setting takes its value from the variable
    (not the user's file, a lock, or a bad value skipped); ``hides_yours`` the user has a
    value of their own in settings.toml that the variable hides. ``warning``: one line when
    a Tools variable hides the user's setting, else ``""``."""
    from harness_manager.settings import runtime

    rows = _row_envs()
    env = dict(snapshot)
    out: list[dict[str, Any]] = []
    for var, value in env.items():
        row = rows.get(var)
        if row is None or getattr(row, "collection", ""):
            continue
        item = {"var": var, "value": HIDDEN if is_secret(var, rows) else value, "key": row.key,
                "section": row.section, "tool": row.section == TOOLS_SECTION,
                "in_effect": False, "hides_yours": False}
        try:
            r = runtime.resolved(row.key, env=env, strict=False)
        except Exception as exc:  # noqa: BLE001 - one row is never worth failing the list
            item["problem"] = str(getattr(exc, "message", exc))
        else:
            item["in_effect"] = r.source == "env" and r.where == f"${var}"
            item["hides_yours"] = bool(item["in_effect"] and r.shadowed)
        out.append(item)
    return {"env": shown(env), "overrides": out, "warning": warning(out)}


def warning(overrides: list[dict[str, Any]]) -> str:
    """One line: the Tools variables in the service's environment that hide the user's own
    setting, and the way out. ``""`` when there are none."""
    hit = [o for o in overrides if o.get("tool") and o.get("hides_yours")]
    if not hit:
        return ""
    names = ", ".join(f"${o['var']}" for o in hit)
    keys = ", ".join(o["key"] for o in hit)
    verb = "overrides" if len(hit) == 1 else "override"
    return (f"{names} in the service's environment {verb} your {keys}: restart the service "
            "from a shell without it (`harness-manager daemon stop`, then `harness-manager "
            "app`)")


def log_at_start(snapshot: Mapping[str, str]) -> None:
    """One daemon.log line: what the service started with (secrets masked), so a later
    "which OpenOCD did it use" has an answer."""
    if not snapshot:
        log.info("service environment: no HARNESS_MANAGER_* or tool variables")
        return
    log.info("service environment: %s",
             ", ".join(f"{k}={v}" for k, v in shown(snapshot).items()))

