"""``harness-manager panel`` and ``harness-manager identify``: the board's front panel (lane P1).

Verbs::

    harness-manager panel show   TARGET            what the panel shows, who is connected, Identify
    harness-manager panel mirror TARGET            the panel's 15 x 40 text grid
    harness-manager identify     TARGET [--seconds N]   blink the board so you can find it

``identify`` is a new top-level verb: no verb had the name (``probe`` finds boards over UDP
identify, and the capability "Identify the harness" is ``identify``; this one's capability is
``locate``, docs/design/CLCD_ALIGNMENT.md §5). ``--seconds 0`` stops a blink. The board blinks
its user LEDs and its panel's backlight, with an IDENTIFY banner while the harness owns the
panel (docs/design/BOARD_LOCATE.md). 5 s by default. Through harness-manager-daemon a start
goes at most once every 10 s per board (exit 8, ALREADY, says when the next may go); no hub
lease is needed.

On a bare-metal harness (v0.11) ``panel show`` gives the KVM owner only and says the state
is rebuilt; ``panel mirror`` is rebuilt from what Harness Manager read (``source: rebuilt``);
``identify`` fails with exit 12 and the reason (it needs the Linux harness's ``locate``).

Both ways the verbs go through ``session.panel`` (CCR PANEL-5), like every other adapter.
Over a running harness-manager-daemon that is the daemon's proxy (``client.remote``: its
``/panel`` routes, docs/API.md "Front panel"), so the answer carries the daemon's presence
too; in-process it is the board pack's adapter and the board is read once (a CLI run is not
a session worth announcing: it sends no ``hello``).

Exit codes: 0 done; 4 the board is busy (a job, or another client on its control port);
7 the board did not answer; 8 identified less than 10 s ago (try again when it says);
12 this board cannot do it (the reason says why).
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from harness_manager.core import capabilities as C
from harness_manager.core.errors import ExitCode

from .context import SERIAL_HELP, VIA_HELP, VIA_METAVAR, Ctx
from .output import Result, jsonable

TARGET_HELP = "shell address host[:port], or - for a USB-only board (with --serial/--volume)"
DEFAULT_SECONDS = 5


def _parents() -> list[argparse.ArgumentParser]:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    usb = argparse.ArgumentParser(add_help=False)
    usb.add_argument("--serial", action="append", metavar="URL", default=argparse.SUPPRESS,
                     help=SERIAL_HELP)
    usb.add_argument("--volume", action="append", metavar="PATH", default=argparse.SUPPRESS,
                     help="add the configuration SD volume (the mounted V2M-MPS3 drive)")
    usb.add_argument("--via", metavar=VIA_METAVAR, default=argparse.SUPPRESS, help=VIA_HELP)
    return [fmt, usb]


def register(subparsers: Any) -> dict[str, argparse.ArgumentParser]:
    """Add ``panel`` and ``identify``. Returns them by name (for ``help VERB``)."""
    from .output import TSV_COLUMNS

    fmt, usb = _parents()

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(TSV_COLUMNS[layout])}"

    vp = subparsers.add_parser("panel", help="the board's front panel: what it shows, its mirror",
                               description="The board's front panel (LCD): what it shows, who "
                                           "is connected, and its text grid.", parents=[fmt])
    sub = vp.add_subparsers(dest="panel_cmd", required=True, metavar="ACTION")
    ap = sub.add_parser("show", help="page, owner, banner, card, sessions, taps, Identify",
                        parents=[fmt, usb], epilog=epilog("panel show"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap = sub.add_parser("mirror", help="the panel's text grid (read, or rebuilt on bare metal)",
                        parents=[fmt, usb], epilog=epilog("panel mirror"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    vp.set_defaults(fn=cmd_panel)

    ip = subparsers.add_parser("identify", help="blink the board's LEDs and panel so you can find it",
                               description="Identify: blink the board's user LEDs and panel "
                                           "backlight, with an IDENTIFY banner on the panel, so "
                                           "you can tell which board it is (Linux harness). No "
                                           "hub lease is needed; at most once every 10 s per "
                                           "board.",
                               parents=[fmt, usb], epilog=epilog("identify"))
    ip.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ip.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, metavar="N",
                    help=f"how long to blink, 1 to 30 (default {DEFAULT_SECONDS}); 0 stops")
    ip.set_defaults(fn=cmd_identify)
    return {"panel": vp, "identify": ip}


# --- reading through session.panel (the pack's adapter, or the daemon's proxy) ---------------


def _why(ctx: Ctx, session: Any) -> Any:
    def why(capability: str) -> str:
        from harness_manager.core.errors import UnavailableError

        try:
            ctx.require(session, "panel", capability)
        except UnavailableError as exc:
            return exc.reason
        return ""
    return why


def read_state(ctx: Ctx, session: Any) -> dict[str, Any]:
    from harness_manager.services.presence import NOT_BEATING, read_panel

    body = jsonable(read_panel(session, reason_for=_why(ctx, session)))
    # A daemon session's adapter knows the daemon's presence; in-process nothing beats.
    beat = getattr(getattr(session, "panel", None), "presence", None)
    body["presence"] = beat() if callable(beat) else {"active": False, "reason": NOT_BEATING}
    return body


def read_frame(ctx: Ctx, session: Any) -> dict[str, Any]:
    from harness_manager.services.presence import require_panel

    panel = require_panel(session, C.FRONT_PANEL, _why(ctx, session)(C.FRONT_PANEL))
    return jsonable(panel.frame())


def do_identify(ctx: Ctx, session: Any, seconds: int) -> dict[str, Any]:
    from harness_manager.services.presence import (
        check_seconds,
        default_who,
        identify,
        require_panel,
    )

    check_seconds(seconds)
    require_panel(session, C.LOCATE, _why(ctx, session)(C.LOCATE))
    return identify(session, seconds, default_who())


# --- verbs -------------------------------------------------------------------------------------


def cmd_panel(ctx: Ctx) -> int:
    action = ctx.args.panel_cmd
    with ctx.board() as (cand, session):
        if action == "show":
            body = read_state(ctx, session)
            ctx.emit(Result("panel show", {"board_id": cand.board_id, **body},
                            rows=[_show_row(cand.board_id, body)], human=_show_human(body)))
        else:
            frame = read_frame(ctx, session)
            rows = list(frame.get("rows") or ())
            roles = str(frame.get("roles") or "")
            cols = len(rows[0]) if rows else 0
            ctx.emit(Result("panel mirror", {"board_id": cand.board_id, **frame},
                            rows=[[cand.board_id, i, r, roles[i * cols:(i + 1) * cols] or "",
                                   frame.get("source", "")] for i, r in enumerate(rows)],
                            human=_mirror_human(frame)))
    return ExitCode.OK


def cmd_identify(ctx: Ctx) -> int:
    seconds = ctx.args.seconds
    with ctx.board() as (cand, session):
        out = do_identify(ctx, session, seconds)
    until = out.get("until")
    human = (f"identify   {cand.board_id} blinks for {seconds}s (until "
             f"{time.strftime('%H:%M:%S', time.localtime(until))})" if seconds and until
             else f"identify   stopped on {cand.board_id}")
    ctx.emit(Result("identify", {"board_id": cand.board_id, **out},
                    rows=[[cand.board_id, seconds, until]], human=[human]))
    return ExitCode.OK


# --- rendering ---------------------------------------------------------------------------------


def _touch_text(touch: dict[str, Any]) -> str:
    if touch.get("ok") is False:
        return str(touch.get("reason") or "touch unavailable")
    if touch.get("ok") is True:
        return "ok"
    if touch.get("present") is False:
        return "not present"
    return "unknown"


def _sessions_text(panel: dict[str, Any]) -> str:
    rows = panel.get("sessions") or []
    if rows:
        return ", ".join(f"{s.get('who')} ({s.get('role')}, {int(s.get('age_s') or 0)}s)"
                         + (" [this HM]" if s.get("mine") else "") for s in rows)
    return str(panel.get("count") or 0) if panel.get("source") == "panel" else "not known"


def _headline(panel: dict[str, Any]) -> str:
    parts = []
    if panel.get("page"):
        parts.append(f"{panel['page']} page")
    owner = panel.get("owner")
    if owner:
        parts.append("DUT owns the panel" if owner == "dut" else f"{owner} owns it")
    if panel.get("pending"):
        parts.append("handover pending")
    return " · ".join(parts) or "state not known"


def _show_row(board_id: str, body: dict[str, Any]) -> list[Any]:
    panel = body.get("panel") or {}
    ident = body.get("identify") or {}
    return [board_id, panel.get("source", ""), panel.get("page", ""), panel.get("owner", ""),
            panel.get("banner", ""), panel.get("card", ""), len(panel.get("sessions") or ())
            or panel.get("count", 0), panel.get("seq", 0), _touch_text(panel.get("touch") or {}),
            bool(ident.get("available")), body.get("reason") or ident.get("reason") or ""]


def _show_human(body: dict[str, Any]) -> list[str]:
    panel = body.get("panel")
    ident = body.get("identify") or {}
    identify_line = ("identify   available" if ident.get("available")
                     else f"identify   unavailable: {ident.get('reason') or 'unknown'}")
    if panel is None:
        return [f"panel      unavailable: {body.get('reason') or 'unknown'}", identify_line]
    out = [f"panel      {_headline(panel)}"]
    if panel.get("source") == "rebuilt":
        out.append(f"source     {panel.get('note') or 'rebuilt'}")
    out.append(f"banner     {panel.get('banner') or '-'}")
    if panel.get("card"):
        out.append(f"card       {panel['card']}")
    out.append(f"touch      {_touch_text(panel.get('touch') or {})}")
    out.append(f"sessions   {_sessions_text(panel)}")
    for ev in panel.get("events") or []:
        out.append(f"tap        #{ev.get('seq')} on {ev.get('on') or '?'}, "
                   f"{(ev.get('ms_ago') or 0) / 1000:.1f}s ago")
    out.append(identify_line)
    presence = body.get("presence") or {}
    if presence.get("active"):
        out.append(f"presence   announced as {presence.get('sid')} "
                   f"(every {int(presence.get('interval_s') or 0)}s)")
    else:
        out.append(f"presence   not announced: {presence.get('reason') or 'no hello yet'}")
    return out


def _mirror_human(frame: dict[str, Any]) -> list[str]:
    rows = list(frame.get("rows") or ())
    width = len(rows[0]) if rows else 40
    out = ["+" + "-" * width + "+"] + [f"|{r}|" for r in rows] + ["+" + "-" * width + "+"]
    if frame.get("source") == "rebuilt":
        out.append(f"({frame.get('note') or 'rebuilt'})")
    return out
