"""OpenOCD on the board (lane DEBUG-ONBOARD): ``debug up|down|status|detect`` through a
launcher the board runs, with gdb reaching it over the board's SSH.

The Linux harness runs OpenOCD itself (david, 2026-09-30): its image has OpenOCD and a
launcher, ``mps3-debug up|down|status|version [--rm auto|NAME] [--json]``, run over the
claim's SSH. The launcher picks the config for the loaded design and binds gdb (core 0 on
3333, core 1 on 3334), telnet (4444) and Tcl (6666) to the board's 127.0.0.1. Harness Manager
forwards the gdb ports only, over the session's one claim forward. This replaces this PC's
OpenOCD over remote_bitbang (6921) on a claimed Linux board; everything else keeps that path.

The contract (``mps3-debug/1``; the Linux lead confirmed it 2026-10-01). One JSON object on
stdout; unknown keys are ignored::

    {"schema":"mps3-debug/1","state":"up|down|starting|failed","already":false,
     "rm_id":"0x01000001","design":"nanosoc","cfg":["interface/<adapter>.cfg","target/nanosoc.cfg"],
     "pid":1234,"started_at":"2026-10-01T09:12:03Z","bind":"127.0.0.1",
     "openocd":{"version":"0.12.0+dev…","adapter":"remote_bitbang|mps3_jtagbb"},
     "tap":{"idcode":"0x6ba00477"},
     "cores":[{"name":"cpu0","gdb_port":3333},{"name":"cpu1","gdb_port":3334}],
     "telnet_port":4444,"tcl_port":6666,"busy":null,"error":null,"log_tail":[]}

- failure: ``"state":"failed","error":{"code":"no_dap|busy|no_cfg|openocd_exit|no_openocd",
  "message":…,"hint":…}``; ``busy`` = ``{"by":…,"peer":…}``; ``log_tail``: up to 20 lines;
- exit codes: 0 ok, 4 busy (6921 held by another client), 6 failed, 12 no OpenOCD in the
  image, 13 no debug port in this design, 14 no config for this rm_id; 127 (or no JSON at
  all): no launcher;
- ``--rm auto`` reads the rm_id with the harness's UDP identify on the board; no answer or an
  unknown rm_id is 14 ``no_cfg`` ("pass --rm NAME"). Harness Manager passes ``--rm NAME``
  whenever it knows the loaded design, and ``auto`` only when it does not;
- a swap: the launcher's watchdog sees the rm_id change (or the target drop), stops, and
  reports ``down``/``failed`` with ``error.code`` ``openocd_exit`` and "swap" in the message.
  Harness Manager still stops its own session before any program (``deploy.started``) and
  shows a watchdog stop as "closed for the swap", never as a failure;
- the launcher stops an idle OpenOCD after 2 h.

**Via hostio (HM 1.2.1; the launcher's ``up --via hostio``).** The same route, with the DUT's
hostio4 link instead of JTAG: memory reads and writes and image loads only (no halt, step,
registers or breakpoints). ``up`` takes ``via="jtag"`` (default) or ``"hostio"``; the reply's
appended keys are ``via``, ``capabilities`` and ``gdb_init`` (a reply without them is a JTAG
session with the full capability set). A client sends ``gdb_init`` (hostio:
``set remote p-packet off`` and ``set remote P-packet off``) before ``target extended-remote``.
The refusal codes ``hostio_mode``, ``rp_held``, ``no_hostio``, ``no_adp``,
``hostio_unavailable`` (and ``busy`` for the other via, the control port 6900 or the ADP port
6933) are mapped to plain words (``launcher_error``). Before anything is sent, HM itself
refuses a hostio ``up`` for a board it has not claimed or whose pins are not in hostio4 mode
(``OnBoardRoute.hostio_gate``). The launcher reads the pins' mode from harnessd's single-client
6900: HM holds no 6900 connection of its own (every call opens and closes), and holds its
own control gate while ``up --via hostio`` runs.

Which path (the setting ``debug.on_board``, L6; ``$HARNESS_MANAGER_DEBUG_ON_BOARD``):

- ``auto`` (default): the board's OpenOCD when the pack has an on-board route, the board is a
  claimed Linux board this Harness Manager can enter, and the launcher answers (asked once
  per session: ``mps3-debug status --json``, cached); else this PC's OpenOCD, unchanged;
- ``true``: the board's OpenOCD, or a refusal that says why: 12 no launcher (or a board
  without one, bare metal), 15 not claimed or a claim this Harness Manager cannot enter;
- ``false``: this PC's OpenOCD only.

Board-agnostic: the pack supplies the route through its debug adapter's optional
``onboard()`` (``OnBoardRoute``, below). The MPS3's is ``harness_manager_mps3.openocd``
``Mps3OnBoard``; another pack (KR260, PYNQ) can supply its own launcher the same way.

**6921 while the board's OpenOCD holds JTAG.** Until harnessd v2.1 the board's JTAG server
refuses this PC's OpenOCD generically (no named "busy" line). So when this PC's OpenOCD is
turned away (held or unreachable) on a board with the launcher, the launcher is asked; if
its session is up, that is exit 4 with ``BUSY_HINT``.

**DEBUG-DOWN-FIRST** (FIX-PACK-7, agreed with the Linux lead 2026-10-01). Before EVERY
deploy (``DeployService.deploy``/``restore_baseline``: the CLI's ``program``/``restore``, the
app's Program/Restore, Build's Add then Program) to a board whose pack supplies an on-board
route whose plan is READY, ``<launcher> down --json`` runs over the claim's SSH,
synchronously, BEFORE ``deploy.started`` (so before the guard and the swap), whoever started
the board's OpenOCD and whatever ``debug.on_board`` says (``down_first``):

- exit 0 and ``state: down`` (``already`` or not): go on. HM's own session, if any, is closed
  for the swap and reopens after a verified swap (``services/debug.py``), with no second down;
- exit 127 (no launcher on the board) or 12 (no OpenOCD in the image): go on, unchanged (no
  OpenOCD on the board can be running);
- anything else (the SSH did not answer or timed out, another exit, no ``mps3-debug/1``
  answer): ``RefusedError`` (15) naming ``<launcher> down`` and why, hint ``DOWN_FIRST_HINT``,
  before anything touches the board. ``force`` (``--force``, ``force: true``) goes on with a
  warning; so does a board whose launcher lists ``LOCK_CAPABILITY`` in ``version --json``'s
  ``capabilities`` (harnessd v2.1's lock keeps OpenOCD off JTAG during a swap): keyed on that
  one capability, never on a version string. A board without the field has no lock.

Bare metal, an unclaimed board, another key's claim and a board with no route are not asked.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HarnessError,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
    RefusedError,
    UnavailableError,
    UnreachableError,
)
from harness_manager.core.services import DebugStatus

log = logging.getLogger(__name__)

CAPABILITY = "debug_dut"
#: HM 1.2.1: the two ways a session reaches the DUT.
VIA_JTAG, VIA_HOSTIO = "jtag", "hostio"
VIAS = (VIA_JTAG, VIA_HOSTIO)
JTAG_CAPABILITIES = ("mem_read", "mem_write", "load", "halt", "step", "breakpoints",
                     "registers", "reset")
HOSTIO_CAPABILITIES = ("mem_read", "mem_write", "load")
#: What a gdb client sends first on a hostio session (the contract's ``gdb_init``): without
#: them gdb asks for register 25 and the link drops.
HOSTIO_GDB_INIT = ("set remote p-packet off", "set remote P-packet off")
#: The capabilities a hostio session lacks, and why (shown beside every disabled control).
JTAG_ONLY = ("halt", "step", "registers", "breakpoints")
HOSTIO_LIMITS = ("hostio debug reads and writes memory and loads images; halt, step, "
                 "registers and breakpoints need JTAG")
HOSTIO_VERIFY_NOTE = ("hostio cannot verify an image in place (no verify_image): read it back "
                      "with `dump_image` and compare, or check words with `x/4wx ADDRESS`")
SCHEMA = "mps3-debug/1"
#: The setting's key and its developer variable (the row: ``settings/rows.py`` L6).
SETTING = "debug.on_board"
ON_BOARD_ENV = "HARNESS_MANAGER_DEBUG_ON_BOARD"
MODES = ("auto", "true", "false")

# The launcher's exit codes (mps3-debug/1).
RC_OK, RC_BUSY, RC_FAILED = 0, 4, 6
RC_NO_OPENOCD, RC_NO_DAP, RC_NO_CFG, RC_NO_LAUNCHER = 12, 13, 14, 127

#: Seconds each launcher call may take over SSH (through the hub, ~1-3 s for a status).
STATUS_TIMEOUT_S = 30.0
UP_TIMEOUT_S = 90.0
DOWN_TIMEOUT_S = 30.0
#: A reply this fresh answers the status that follows the detect in the same call.
FRESH_S = 2.0
#: "starting" from ``up``: how long, and how often, the launcher is asked again.
START_POLL_S = 1.0

READY, NONE, REFUSED = "ready", "none", "refused"      # OnBoardRoute.plan()'s answers

BUSY_HINT = ("OpenOCD is running on the board (on-board session): use it (`debug status`), "
             "or stop it (`harness-manager debug down`)")
NO_LAUNCHER_HINT = ("the board's Linux image needs OpenOCD and mps3-debug (v7 or later); "
                    "meanwhile set debug.on_board to auto or false to use this PC's OpenOCD")
SWAP_DETAIL = "closed for the swap"
SWAP_REASON = f"{SWAP_DETAIL} (OpenOCD on the board stops before a partition swap)"
ONBOARD_DETAIL = "OpenOCD runs on the board; gdb reaches it through the board's SSH"

#: DEBUG-DOWN-FIRST: harnessd v2.1's JTAG lock, as the launcher's ``version --json`` lists it
#: in ``capabilities`` (the Linux lead, 2026-10-01). With it, a failed down warns and goes on.
LOCK_CAPABILITY = "harnessd-lock"
DOWN_FIRST_HINT = ("retry, or add --force to swap anyway (on Linux v2.0.0 OpenOCD on the "
                   "board may still drive JTAG during the reconfiguration)")
#: ``error.data`` key of the refusal: the app offers "Program anyway" on it.
DOWN_FIRST_DATA = "debug_down"


class OnBoardRoute(Protocol):
    """What a board pack supplies for OpenOCD that runs ON the board: its debug adapter's
    optional ``onboard()`` returns one (or None: no such route for this session).

    - ``launcher``: the board-side command, for the words (``"mps3-debug"``);
    - ``plan()``: ``("ready", None)`` when the launcher can be run (a claimed board this
      Harness Manager can enter), ``("none", err)`` when this board has no such route (bare
      metal), ``("refused", err)`` when it has one this Harness Manager may not use (not
      claimed, another key's claim). ``err`` is what ``debug.on_board = true`` raises;
    - ``run(verb, *, rm="", timeout)``: run ``<launcher> VERB [--rm RM] --json`` on the board;
      returns an object with ``returncode``, ``stdout``, ``stderr``. Raises only for the
      transport (``UnreachableError``, a changed host key);
    - ``design_name()``: the loaded design's name for ``--rm``, "" when not known (``auto``);
    - ``hold()``: open (or join) the forward of the board's gdb ports; returns ``{board port:
      local port}``, local ends on 127.0.0.1 only. ``release()`` lets it go (idempotent);
    - ``describe()``: one line for the status.

    Optional (HM 1.2.1): ``run(..., via="hostio")`` adds ``--via hostio`` to an ``up``, and
    ``hostio_gate()`` returns the refusal (a ``HarnessError``) when a hostio session cannot be
    asked for yet (the DUT's pins are not in hostio4 mode), else None.
    """

    launcher: str

    def plan(self) -> tuple[str, HarnessError | None]: ...
    def run(self, verb: str, *, rm: str = "", timeout: float = STATUS_TIMEOUT_S) -> Any: ...
    def hostio_gate(self) -> HarnessError | None: ...
    def design_name(self) -> str: ...
    def hold(self) -> dict[int, int]: ...
    def release(self) -> None: ...
    def describe(self) -> str: ...


def route_of(session: Any) -> Any:
    """The session's on-board route (the debug adapter's ``onboard()``), or None."""
    hook = getattr(getattr(session, "debug", None), "onboard", None)
    if not callable(hook):
        return None
    try:
        return hook()
    except HarnessError:
        return None


# --- the reply ------------------------------------------------------------------------------


def _int(v: Any) -> int:
    try:
        return int(v) if not isinstance(v, bool) else 0
    except (TypeError, ValueError):
        return 0


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


@dataclass(frozen=True)
class LauncherReply:
    """One ``mps3-debug/1`` answer, read tolerantly (``parse_reply``)."""

    state: str
    already: bool = False
    rm_id: str = ""
    design: str = ""
    cfg: tuple[str, ...] = ()
    pid: int = 0
    started_at: str = ""
    openocd_version: str = ""
    adapter: str = ""
    idcode: str = ""
    cores: tuple[tuple[str, int], ...] = ()       # (name, the board's gdb port)
    telnet_port: int = 0
    tcl_port: int = 0
    busy: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    log_tail: tuple[str, ...] = ()
    #: HM 1.2.1: appended keys; absent = a JTAG session with the full capability set
    via: str = VIA_JTAG
    capabilities: tuple[str, ...] = JTAG_CAPABILITIES
    gdb_init: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def code(self) -> str:
        return _str((self.error or {}).get("code"))

    @property
    def message(self) -> str:
        return _str((self.error or {}).get("message"))

    @property
    def hint(self) -> str:
        return _str((self.error or {}).get("hint"))

    def swap_stopped(self) -> bool:
        """The launcher's watchdog stopped OpenOCD for a swap (rm_id changed, target gone)."""
        return (self.state in ("down", "failed") and self.code == "openocd_exit"
                and "swap" in self.message.lower())

    def what(self) -> str:
        return self.design or self.rm_id or "the loaded design"


def _ours(obj: dict[str, Any]) -> bool:
    schema = obj.get("schema")
    return isinstance(schema, str) and (schema == SCHEMA or schema.startswith(SCHEMA + "."))


def _reply(obj: dict[str, Any]) -> LauncherReply | None:
    if not _ours(obj):
        return None
    state = obj.get("state")
    if not isinstance(state, str) or not state:
        return None
    ocd = obj.get("openocd") if isinstance(obj.get("openocd"), dict) else {}
    tap = obj.get("tap") if isinstance(obj.get("tap"), dict) else {}
    cores = []
    for i, c in enumerate(obj.get("cores") if isinstance(obj.get("cores"), list) else ()):
        if isinstance(c, dict) and _int(c.get("gdb_port")) > 0:
            cores.append((_str(c.get("name")) or f"cpu{i}", _int(c.get("gdb_port"))))
    cfg = obj.get("cfg") if isinstance(obj.get("cfg"), list) else []
    tail = obj.get("log_tail") if isinstance(obj.get("log_tail"), list) else []
    busy = obj.get("busy") if isinstance(obj.get("busy"), dict) else None
    error = obj.get("error") if isinstance(obj.get("error"), dict) else None
    via = _str(obj.get("via")).strip().lower()
    via = via if via in VIAS else VIA_JTAG
    caps = obj.get("capabilities")
    capabilities = (tuple(c for c in caps if isinstance(c, str)) if isinstance(caps, list)
                    else HOSTIO_CAPABILITIES if via == VIA_HOSTIO else JTAG_CAPABILITIES)
    init = obj.get("gdb_init")
    gdb_init = (tuple(c for c in init if isinstance(c, str)) if isinstance(init, list)
                else HOSTIO_GDB_INIT if via == VIA_HOSTIO else ())
    return LauncherReply(
        state=state, already=obj.get("already") is True, rm_id=_str(obj.get("rm_id")),
        design=_str(obj.get("design")), cfg=tuple(str(c) for c in cfg),
        pid=_int(obj.get("pid")), started_at=_str(obj.get("started_at")),
        openocd_version=_str(ocd.get("version")), adapter=_str(ocd.get("adapter")),
        idcode=_str(tap.get("idcode")).lower(), cores=tuple(cores),
        telnet_port=_int(obj.get("telnet_port")), tcl_port=_int(obj.get("tcl_port")),
        busy=busy, error=error, log_tail=tuple(str(t) for t in tail), via=via,
        capabilities=capabilities, gdb_init=gdb_init, raw=obj)


def _objects(text: str | None) -> list[dict[str, Any]]:
    """The ``mps3-debug/1`` objects in the launcher's stdout: the whole text first (one
    object, maybe pretty-printed), then each line from the last, so a login banner or a
    warning line never hides one."""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    for chunk in [text] + [ln.strip() for ln in reversed(text.splitlines()) if ln.strip()]:
        if not chunk.startswith("{"):
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and _ours(obj):
            out.append(obj)
    return out


def parse_reply(text: str | None) -> LauncherReply | None:
    """The ``mps3-debug/1`` state object in the launcher's stdout, or None when there is none
    (no launcher, or not its answer)."""
    for obj in _objects(text):
        got = _reply(obj)
        if got is not None:
            return got
    return None


def parse_capabilities(text: str | None) -> tuple[str, ...] | None:
    """``version --json``'s ``capabilities`` (DEBUG-DOWN-FIRST): None when the text holds no
    ``mps3-debug/1`` object; ``()`` when the field is absent or not a list (no capability)."""
    objs = _objects(text)
    if not objs:
        return None
    for obj in objs:
        if "capabilities" in obj:
            caps = obj["capabilities"]
            return tuple(c for c in caps if isinstance(c, str)) if isinstance(caps, list) else ()
    return ()


def parse_design_hostio(text: str | None, name: str) -> bool | None:
    """``version --json``'s ``designs[]`` entry named ``name``: its ``hostio`` flag. None: no
    such entry, or no flag (an older launcher): unknown, and the board decides at ``up``."""
    for obj in _objects(text):
        for d in obj.get("designs") if isinstance(obj.get("designs"), list) else ():
            if isinstance(d, dict) and d.get("name") == name and isinstance(d.get("hostio"), bool):
                return d["hostio"]
    return None


def _tail(res: Any, n: int = 3) -> str:
    lines = [ln.strip() for ln in ((getattr(res, "stderr", "") or "") + "\n"
                                  + (getattr(res, "stdout", "") or "")).splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) or "no output"


def busy_words(busy: dict[str, Any] | None) -> str:
    """``{"by": "openocd (pid 812)", "peer": "192.168.10.1"}`` -> "openocd (pid 812) from
    192.168.10.1"."""
    by = _str((busy or {}).get("by"))
    peer = _str((busy or {}).get("peer"))
    if by and peer:
        return f"{by} from {peer}"
    return by or (f"a client from {peer}" if peer else "another JTAG client")


#: HM 1.2.1: the plain words for the hostio path's refusals (``hostio_error``). Verbatim, so
#: the guide can quote them.
HOSTIO_MODE_WORDS = "the DUT's pins are in console mode, so hostio debug is not available"
HOSTIO_MODE_HINT = ("press \"Switch to hostio4\" (Consoles), then load the design again so "
                    "the change takes effect")
HOSTIO_PENDING_WORDS = ("the DUT's pins will switch to hostio4 at the next design load, so "
                        "hostio debug is not available yet")
HOSTIO_PENDING_HINT = "load the design again, then open the hostio session"
HOSTIO_RP_HELD_WORDS = ("the DUT is held in reset (or cut off from the board), so its hostio "
                        "link is not running")
HOSTIO_RP_HELD_HINT = "load the design (a swap releases the DUT), then open the hostio session"
HOSTIO_NO_ADP_WORDS = "nothing answers on the DUT's hostio debug link"
HOSTIO_NO_ADP_HINT = ("check that the design is loaded and the DUT's pins are in hostio4 mode "
                      "(Consoles), then try again")
HOSTIO_UNAVAILABLE_WORDS = "this board's software has no hostio bridge"
HOSTIO_UNAVAILABLE_HINT = "hostio debug needs the mint-4 board software; use JTAG instead"
HOSTIO_NO_CFG_WORDS = "this board's Linux image predates hostio debug"
HOSTIO_NO_CFG_HINT = "use JTAG, or update the board's Linux image"
HOSTIO_NO_DESIGN_HINT = ("load a nanoSoC design (with the pins in hostio4 mode first), then "
                         "open the hostio session")
BUSY_OTHER_VIA_HINT = "close the session (`harness-manager debug down TARGET`), then open it again"
BUSY_CONTROL_WORDS = ("another client holds the board's control connection (port 6900), which "
                      "hostio debug needs to check the DUT")
BUSY_CONTROL_HINT = ("close the other client (another Harness Manager or pyverify), then try "
                     "again")
BUSY_ADP_HINT = "close the other hostio client, then try again"
VIA_WORDS = {VIA_JTAG: "JTAG", VIA_HOSTIO: "hostio"}


def _busy_by(reply: LauncherReply) -> str:
    return _str((reply.busy or {}).get("by"))


def hostio_error(returncode: int, reply: LauncherReply, via: str) -> HarnessError | None:
    """The hostio path's refusals as plain words and the right error class; None: not one of
    them (``launcher_error`` goes on). Never shows a raw code."""
    code, by = reply.code, _busy_by(reply)
    said = reply.message
    if by.startswith("mps3-debug via "):
        other = VIA_WORDS.get(by.rsplit(" ", 1)[-1], by)
        return HeldError(f"a {other} debug session is already up on the board; the board "
                         "runs one debug session at a time", holder=by,
                         hint=BUSY_OTHER_VIA_HINT)
    if by == "6900 control":
        return HeldError(BUSY_CONTROL_WORDS, holder=by, hint=BUSY_CONTROL_HINT)
    if by == "6933 hostio ADP":
        peer = _str((reply.busy or {}).get("peer"))
        return HeldError("another client holds the DUT's hostio debug link"
                         + (f" ({peer})" if peer else ""), holder=by, hint=BUSY_ADP_HINT)
    if code == "hostio_mode":
        pending = "pending" in said.lower()
        return RefusedError(HOSTIO_PENDING_WORDS if pending else HOSTIO_MODE_WORDS,
                            hint=HOSTIO_PENDING_HINT if pending else HOSTIO_MODE_HINT)
    if code == "rp_held":
        return RefusedError(HOSTIO_RP_HELD_WORDS, hint=HOSTIO_RP_HELD_HINT)
    if code == "no_adp":
        cls = RefusedError if returncode == 15 else ActionFailedError
        return cls(HOSTIO_NO_ADP_WORDS, hint=HOSTIO_NO_ADP_HINT)
    if code == "hostio_unavailable":
        return UnavailableError(CAPABILITY, HOSTIO_UNAVAILABLE_WORDS,
                                hint=HOSTIO_UNAVAILABLE_HINT)
    if code == "no_hostio":
        return NothingOnTargetError(f"the loaded design ({reply.what()}) has no hostio debug "
                                    "link", hint="use JTAG, or load a nanoSoC-family design")
    if via == VIA_HOSTIO and code == "no_dap":
        loaded_none = "no design is loaded" in said.lower() or reply.design in ("", "greybox")
        return NothingOnTargetError(
            "no design is loaded, so there is nothing to debug over hostio" if loaded_none else
            f"the loaded design ({reply.what()}) has no debug port",
            hint=HOSTIO_NO_DESIGN_HINT if loaded_none else "load a nanoSoC design first")
    if via == VIA_HOSTIO and code == "no_cfg":
        return IncompatibleError(HOSTIO_NO_CFG_WORDS, hint=HOSTIO_NO_CFG_HINT)
    return None


def launcher_error(verb: str, returncode: int, reply: LauncherReply, *,
                   rm: str = "", via: str = VIA_JTAG) -> HarnessError:
    """A launcher's refusal as the Harness Manager error (and exit code) it means."""
    code = reply.code
    said = reply.message or f"`mps3-debug {verb}` exited {returncode}"
    plain = hostio_error(returncode, reply, via)
    if plain is not None:
        return plain
    if returncode == RC_BUSY or code == "busy":
        who = busy_words(reply.busy)
        return HeldError(f"the board's JTAG is held by {who}: {said}", holder=who,
                         hint=reply.hint or "the board serves one JTAG client at a time; "
                                            "stop the other one, then retry")
    if returncode == RC_NO_OPENOCD or code == "no_openocd":
        return UnavailableError(CAPABILITY, f"the board's image has no OpenOCD ({said})",
                                hint=reply.hint or NO_LAUNCHER_HINT)
    if returncode == RC_NO_DAP or code == "no_dap":
        return NothingOnTargetError(f"the loaded design ({reply.what()}) has no debug port: "
                                    f"{said}",
                                    hint=reply.hint or "load nanosoc, nanosoc_upy, nanosoc_iice "
                                                       "or nanosoc_multicore first")
    if returncode == RC_NO_CFG or code == "no_cfg":
        asked = f"--rm {rm}" if rm else "--rm auto"
        return IncompatibleError(
            f"the board's OpenOCD has no config for this design ({asked}): {said}",
            hint=(reply.hint or "pass --rm NAME") + ". Harness Manager passes the loaded "
                 "design's name when it knows it, and auto only when it does not")
    tail = [t for t in reply.log_tail if t.strip()]
    log_words = f" [log: {' | '.join(tail[-5:])}]" if tail else ""
    return ActionFailedError(f"OpenOCD on the board failed to {verb}: {said}{log_words}",
                             hint=reply.hint or "`harness-manager debug status TARGET` shows "
                                                "the board's last state")


# --- DEBUG-DOWN-FIRST: the board's OpenOCD stops before every swap ------------------------------


@dataclass(frozen=True)
class DownFirst:
    """What asking the board's OpenOCD down before a swap found (``down_first``).

    ``asked``: the board had a READY route, so the launcher was asked. ``ok``: it answered
    down (``already``: it was not running), or it is not installed (``no_launcher``, exit 127),
    or the image has no OpenOCD (``no_openocd``, exit 12), or nothing was asked. Not ``ok``: ``why`` says what failed; the swap went on only
    ``forced`` (``--force``) or because the board has harnessd's ``lock``."""

    asked: bool = False
    ok: bool = True
    launcher: str = ""
    why: str = ""
    already: bool = False
    no_launcher: bool = False
    no_openocd: bool = False
    forced: bool = False
    lock: bool = False

    @property
    def word(self) -> str:
        """One word for the log: "" when not asked."""
        if not self.asked:
            return ""
        if not self.ok:
            return "forced" if self.forced else "lock" if self.lock else "failed"
        if self.no_launcher or self.no_openocd:
            return "no launcher" if self.no_launcher else "no OpenOCD in the image"
        return "already down" if self.already else "down"

    @property
    def warning(self) -> str:
        """What the swap that went on despite a failed down warns (``deploy.warning``); ""
        when the down worked (or the failure refused the swap)."""
        if self.ok or not (self.forced or self.lock):
            return ""
        if self.lock:
            return (f"`{self.launcher} down` failed ({self.why}); going on: the board's "
                    f"harnessd lock ({LOCK_CAPABILITY}) keeps OpenOCD off JTAG during the swap")
        return (f"swapping anyway (forced) although `{self.launcher} down` failed ({self.why}): "
                "on Linux v2.0.0 OpenOCD on the board may still drive JTAG during the "
                "reconfiguration")

    def refusal(self) -> RefusedError:
        """The refusal (exit 15) of a swap whose down failed: before anything touched the
        board. ``error.data.debug_down`` lets the app offer "Program anyway"."""
        err = RefusedError(
            f"`{self.launcher} down` failed before the swap ({self.why}): OpenOCD on the board "
            "may still drive JTAG, so nothing was programmed", hint=DOWN_FIRST_HINT)
        err.data = {DOWN_FIRST_DATA: {"launcher": self.launcher,  # type: ignore[attr-defined]
                                      "reason": self.why, "force": True}}
        return err


def ready_route(session: Any) -> Any:
    """The session's on-board route when its plan is READY (a claimed Linux board this Harness
    Manager can enter), else None: bare metal, not claimed, another key's claim, no route.
    Never raises."""
    rt = route_of(session)
    if rt is None:
        return None
    try:
        state, _err = rt.plan()
    except HarnessError as exc:
        log.info("debug on-board: no plan for the down before the swap: %s", exc)
        return None
    return rt if state == READY else None


def ask_down(rt: Any) -> DownFirst:
    """``<launcher> down --json`` on the board, read strictly (DEBUG-DOWN-FIRST): ok only for
    exit 0 with ``state: down``, exit 127 (no launcher) or exit 12 (no OpenOCD in the image:
    none can be running). Never raises."""
    launcher = getattr(rt, "launcher", "") or "the launcher"
    try:
        res = rt.run("down", timeout=DOWN_TIMEOUT_S)
    except HarnessError as exc:
        return DownFirst(asked=True, ok=False, launcher=launcher,
                         why=f"the board's SSH failed: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - whatever stops the ask refuses the swap
        log.exception("debug on-board: `%s down` could not be run", launcher)
        return DownFirst(asked=True, ok=False, launcher=launcher,
                         why=f"it could not be run: {exc}")
    rc = res.returncode
    if rc == RC_NO_LAUNCHER:
        return DownFirst(asked=True, ok=True, launcher=launcher, no_launcher=True)
    if rc == RC_NO_OPENOCD:
        return DownFirst(asked=True, ok=True, launcher=launcher, no_openocd=True)
    reply = parse_reply(getattr(res, "stdout", ""))
    said = ""
    if reply is not None:
        said = ": ".join(w for w in (reply.code, reply.message) if w)
    if reply is None:
        why = f"no {SCHEMA} answer, exit {rc}: {_tail(res)}"
    elif rc != RC_OK:
        why = f"exit {rc}" + (f", {said}" if said else "")
    elif reply.state != "down":
        why = f"it answered state {reply.state!r}" + (f", {said}" if said else "")
    else:
        return DownFirst(asked=True, ok=True, launcher=launcher, already=reply.already)
    return DownFirst(asked=True, ok=False, launcher=launcher, why=why)


def has_lock(rt: Any) -> bool:
    """Does the board's launcher list ``LOCK_CAPABILITY`` (``version --json``'s
    ``capabilities``, exit 0)? An absent field, another answer or no answer: no lock."""
    try:
        res = rt.run("version", timeout=STATUS_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - no answer is no lock: the refusal stands
        log.info("debug on-board: `version --json` did not answer: %s", exc)
        return False
    if res.returncode != RC_OK:
        return False
    return LOCK_CAPABILITY in (parse_capabilities(getattr(res, "stdout", "")) or ())


def settle(got: DownFirst, *, force: bool, lock: Any) -> DownFirst:
    """Go on, or raise the refusal (15): a failed down goes on only ``force``d, or when
    ``lock()`` says the board has harnessd's lock."""
    if got.ok:
        return got
    if force:
        return replace(got, forced=True)
    if lock():
        return replace(got, lock=True)
    raise got.refusal()


def down_first(session: Any, *, force: bool = False) -> DownFirst:
    """DEBUG-DOWN-FIRST for a session with no ``DebugService`` session of its own: ask the
    board's OpenOCD down when its route is READY; raises the refusal (15)."""
    rt = ready_route(session)
    if rt is None:
        return DownFirst()
    got = settle(ask_down(rt), force=force, lock=lambda: has_lock(rt))
    log.info("debug on-board: down before the swap: %s", got.word)
    return got


def mode(state_dir: Any = None) -> str:
    """``debug.on_board``: ``$HARNESS_MANAGER_DEBUG_ON_BOARD``, then the settings; ``auto``."""
    from harness_manager.settings import runtime

    got = runtime.value(SETTING, state_dir=state_dir)
    return got if got in MODES else "auto"


# --- the service's on-board half --------------------------------------------------------------


@dataclass
class _Detected:
    present: bool
    reply: LauncherReply | None = None
    why: str = ""
    at: float = 0.0


@dataclass
class _Live:
    session: Any
    route: Any
    reply: LauncherReply
    status: DebugStatus


HOSTIO_NO_SUPPORT_WORDS = "this board has no hostio support"
HOSTIO_NO_SUPPORT_HINT = "hostio debug needs the mint-4 board software; use JTAG instead"
HOSTIO_NEEDS_BOARD = "hostio debug runs OpenOCD on the board, over the board's SSH"


def _hostio_needs_board(err: HarnessError) -> HarnessError:
    """A refusal (not claimed, another key's claim) with the hostio words in front; any other
    error as it is."""
    if type(err) is RefusedError:
        return RefusedError(f"{HOSTIO_NEEDS_BOARD}: {err.message}", hint=err.hint)
    return err


class OnBoard:
    """``DebugService``'s on-board half: the launcher's sessions this service opened, and the
    per-session detection. ``svc`` is the ``services.debug.DebugService`` (its state dir, bus,
    lease view, the 6921 drain record)."""

    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self._mu = threading.RLock()
        self._live: dict[str, _Live] = {}
        #: Per session (its route object): did the launcher answer? Weak: a closed session
        #: takes its answer with it.
        self._known: weakref.WeakKeyDictionary[Any, _Detected] = weakref.WeakKeyDictionary()
        #: The route last seen with a launcher, per board (``before_swap`` without a record).
        self._seen: dict[str, weakref.ref[Any]] = {}
        #: Why the board's OpenOCD is down, when this service knows (the swap), until next up.
        self._closed_for: dict[str, str] = {}
        #: The via of the session a swap closed, for the reopen after a verified swap.
        self._reopen_via: dict[str, str] = {}

    def reopen_via(self, board_id: str) -> str:
        """The via the session closed for a swap had (once); jtag when none."""
        with self._mu:
            return self._reopen_via.pop(board_id, VIA_JTAG)

    # -- the choice -----------------------------------------------------------------------

    def mode(self) -> str:
        return mode(self.svc.state_dir)

    def live(self, board_id: str) -> _Live | None:
        with self._mu:
            return self._live.get(board_id)

    def route(self, session: Any, via: str = VIA_JTAG) -> Any:
        """The route to use for ``session``, or None for this PC's OpenOCD (today's path).
        ``debug.on_board = true`` raises what stops the board's route instead of None. Asks
        the launcher at most once per session (``detect``).

        ``via="hostio"`` has no path on this PC: it raises what stops the board's route, as
        ``true`` does, and refuses before the launcher is asked when the board is not claimed
        by this Harness Manager or the DUT's pins are not in hostio4 mode."""
        want = self.mode()
        hostio = via == VIA_HOSTIO
        if hostio and want == "false":
            raise RefusedError("hostio debug runs OpenOCD on the board, and the setting "
                               "debug.on_board is false",
                               hint="set debug.on_board to auto, then open the hostio session")
        if want == "false":
            return None
        if hostio:
            want = "true"
        rt = route_of(session)
        if rt is None:
            if want == "true":
                raise UnavailableError(CAPABILITY, "this board has no on-board OpenOCD (its "
                                                   "board pack has no launcher route)",
                                       hint="set debug.on_board to auto to use this PC's "
                                            "OpenOCD")
            return None
        state, err = rt.plan()
        if state != READY:
            if want == "true":
                err = err or UnavailableError(CAPABILITY, "this board has no on-board OpenOCD")
                if hostio:
                    err = _hostio_needs_board(err)
                raise err
            return None
        if hostio:
            gate = getattr(rt, "hostio_gate", None)
            refusal = gate() if callable(gate) else UnavailableError(
                CAPABILITY, HOSTIO_NO_SUPPORT_WORDS, hint=HOSTIO_NO_SUPPORT_HINT)
            if refusal is not None:
                raise refusal
        try:
            det = self.detect(rt, session)
        except HarnessError as exc:
            # auto: the board's SSH did not answer (not remembered). This PC's path goes over
            # the same SSH on a claimed board, so it meets the same fault and reports it
            # as it always has.
            if want == "true":
                raise
            log.info("debug on-board: the launcher could not be asked: %s", exc)
            return None
        if not det.present:
            if want == "true":
                raise UnavailableError(CAPABILITY, f"no OpenOCD launcher on the board: {det.why}",
                                       hint=NO_LAUNCHER_HINT)
            return None
        if hostio:
            name, flag = self.hostio_design(rt)
            if flag is False:
                raise NothingOnTargetError(f"{name} has no hostio",
                                           hint="use JTAG, or load a nanoSoC-family design")
        return rt

    def hostio_design(self, rt: Any) -> tuple[str, bool | None]:
        """``(design name, hostio flag)`` of the loaded design from the launcher's ``version``
        (HM 1.2.1): False = the board says it has no hostio link; None = unknown (no name, no
        flag): allowed, and the board refuses with ``no_hostio`` if it must."""
        name = rt.design_name()
        if not name:
            return "", None
        res = rt.run("version", timeout=STATUS_TIMEOUT_S)
        if res.returncode != RC_OK:
            return name, None
        return name, parse_design_hostio(getattr(res, "stdout", ""), name)

    def detect(self, rt: Any, session: Any = None) -> _Detected:
        """Does the board have the launcher? ``<launcher> status --json``, once per session;
        a transport failure raises and is not remembered."""
        with self._mu:
            got = self._known.get(rt)
        if got is not None:
            return got
        res = rt.run("status", timeout=STATUS_TIMEOUT_S)
        reply = parse_reply(res.stdout)
        launcher = getattr(rt, "launcher", "the launcher")
        if res.returncode == RC_NO_LAUNCHER:
            det = _Detected(False, why=f"{launcher} is not installed (exit 127: {_tail(res, 1)})")
        elif reply is None:
            det = _Detected(False, why=f"`{launcher} status --json` gave no {SCHEMA} answer "
                                       f"(exit {res.returncode}: {_tail(res)})")
        else:
            det = _Detected(True, reply=reply, at=time.monotonic())
        with self._mu:
            self._known[rt] = det
            if det.present and session is not None:
                self._seen[session.candidate.board_id] = weakref.ref(rt)
        log.info("debug on-board: %s", "launcher answered" if det.present else det.why)
        return det

    def forget(self, rt: Any) -> None:
        with self._mu:
            self._known.pop(rt, None)

    def _changing(self, rt: Any) -> None:
        """An up or a down is about to change the board's state: a detect's answer no longer
        answers a status (``_ask``)."""
        with self._mu:
            det = self._known.get(rt)
            if det is not None:
                det.at = 0.0

    def _ask(self, rt: Any) -> LauncherReply:
        """The launcher's state now (a detect's answer when it is this fresh)."""
        with self._mu:
            det = self._known.get(rt)
        if det is not None and det.reply is not None and time.monotonic() - det.at < FRESH_S:
            reply, det.at = det.reply, 0.0                 # use it once
            return reply
        res = rt.run("status", timeout=STATUS_TIMEOUT_S)
        reply = parse_reply(res.stdout)
        if reply is None:
            self.forget(rt)
            raise ActionFailedError(f"`{getattr(rt, 'launcher', 'the launcher')} status --json` "
                                    f"gave no {SCHEMA} answer (exit {res.returncode}: "
                                    f"{_tail(res)})")
        return reply

    # -- the lease --------------------------------------------------------------------------

    def check_lease(self, session: Any) -> None:
        """A board behind a hub: its lease must be held here (FIX-PACK-4's one lease rule),
        as for XVC; a board with no hub has no lease (the session lock is the gate)."""
        hub = getattr(session, "hub", None)
        if hub is None:
            return
        from harness_manager.services.lease import elsewhere_text, held_here, not_fresh

        what = "OpenOCD on the board"
        target = getattr(hub, "target", "") or "the board"
        leases = self.svc.lease_service()
        forget = getattr(leases, "forget", None)
        if callable(forget):
            forget(hub)
        ask_again = (f"{what} is for the lease holder only; retry when the hub answers "
                     "(`harness-manager lease show TARGET`)")
        try:
            view = leases.view(hub)
        except HarnessError as exc:
            raise HeldError(f"cannot confirm you hold the lease on {target}: {exc.message}",
                            holder="unknown (the hub did not answer)", hint=ask_again) from exc
        stale = not_fresh(view)
        if stale:
            raise HeldError(f"cannot confirm you hold the lease on {target}: {stale}",
                            holder="unknown (the hub did not answer)", hint=ask_again)
        lease = (view or {}).get("lease")
        if not lease:
            raise HeldError(f"{what} is for the lease holder only, and nobody holds {target}",
                            holder="nobody",
                            hint="take the lease first: `harness-manager lease acquire TARGET`")
        if not held_here(lease):
            who = lease.get("holder") or "someone else"
            raise HeldError(f"{what} is for the lease holder only: "
                            f"{elsewhere_text(lease, target)}", holder=who,
                            hint="run it from the session that holds the lease, or release it "
                                 "there first" if lease.get("mine") else
                                 "ask for the board: `harness-manager lease request TARGET`")

    # -- up ---------------------------------------------------------------------------------

    def up(self, session: Any, rt: Any, via: str = VIA_JTAG) -> DebugStatus:
        board_id = session.candidate.board_id
        live = self.live(board_id)
        if live is not None:
            other = (f" via {VIA_WORDS.get(live.status.via, live.status.via)}"
                     if live.status.via != via else "")
            raise AlreadyError(f"the debug session for {board_id} is already up (OpenOCD on "
                               f"the board{other})",
                               hint=f"gdb on 127.0.0.1:{live.status.gdb_port}; 'down' first to "
                                    "restart it" + (" or to change the via" if other else ""))
        self.check_lease(session)
        rm = rt.design_name() or "auto"
        word = " --via hostio" if via == VIA_HOSTIO else ""
        self.svc._publish(board_id, "starting", detail=f"{rt.describe()}: up{word} --rm {rm}",
                          where="board")
        reply = self._launch(rt, rm, via)
        return self._attach(session, rt, reply)

    def _launch(self, rt: Any, rm: str, via: str = VIA_JTAG) -> LauncherReply:
        self._changing(rt)
        # a JTAG up is asked exactly as before (an older board knows no --via)
        res = (rt.run("up", rm=rm, timeout=UP_TIMEOUT_S, via=VIA_HOSTIO) if via == VIA_HOSTIO
               else rt.run("up", rm=rm, timeout=UP_TIMEOUT_S))
        reply = parse_reply(res.stdout)
        if reply is None:
            self.forget(rt)
            raise ActionFailedError(
                f"`{getattr(rt, 'launcher', 'the launcher')} up` on the board gave no {SCHEMA} "
                f"answer (exit {res.returncode}: {_tail(res)})",
                hint="the board's launcher and this Harness Manager disagree; `harness-manager "
                     "board ssh TARGET` then `mps3-debug status --json` shows what it says")
        if res.returncode != RC_OK or reply.state not in ("up", "starting"):
            raise launcher_error("start", res.returncode, reply, rm=rm, via=via)
        deadline = time.monotonic() + self.svc.start_timeout
        while reply.state == "starting":
            if time.monotonic() > deadline:
                raise ActionFailedError(f"OpenOCD on the board did not start serving within "
                                        f"{self.svc.start_timeout:.0f}s",
                                        hint="`harness-manager debug status TARGET` shows the "
                                             "board's last state")
            time.sleep(START_POLL_S)
            got = rt.run("status", timeout=STATUS_TIMEOUT_S)
            reply = parse_reply(got.stdout) or reply
            if reply.state not in ("up", "starting"):
                raise launcher_error("start", got.returncode or RC_FAILED, reply, rm=rm,
                                     via=via)
        return reply

    def _attach(self, session: Any, rt: Any, reply: LauncherReply) -> DebugStatus:
        """Forward the board's gdb ports and record the session as this service's."""
        board_id = session.candidate.board_id
        try:
            ports = rt.hold()
        except BaseException:
            if not reply.already:            # ours, and unreachable from here: stop it
                self._quiet_down(rt)
            raise
        names, local = [], []
        for name, board_port in reply.cores:
            got = ports.get(board_port)
            if got:
                names.append(name)
                local.append(int(got))
            else:
                log.warning("debug on-board: %s's gdb port %d is not forwarded", name, board_port)
        if not local:
            rt.release()
            if not reply.already:
                self._quiet_down(rt)
            served = ", ".join(f"{n}:{p}" for n, p in reply.cores) or "none"
            raise ActionFailedError(f"OpenOCD on the board serves gdb on ports this Harness "
                                    f"Manager does not forward ({served})",
                                    hint=f"the forwarded ones are {sorted(ports)}")
        with self._mu:
            self._closed_for.pop(board_id, None)
        detail = ONBOARD_DETAIL + (f" (pid {reply.pid})" if reply.pid else "")
        if reply.already:
            detail += "; it was already running there: attached to it"
        status = DebugStatus(state="up", gdb_port=local[0], config=reply.cfg, pid=reply.pid,
                             detail=detail, gdb_ports=tuple(local), cores=tuple(names),
                             where="board", via=reply.via, capabilities=reply.capabilities,
                             gdb_init=reply.gdb_init)
        with self._mu:
            self._live[board_id] = _Live(session, rt, reply, status)
        self.svc._publish(board_id, "up", ports={"gdb": local[0]}, pid=reply.pid, detail=detail,
                          config=reply.cfg, where="board", gdb_ports=list(local), cores=names,
                          via=reply.via, capabilities=list(reply.capabilities),
                          gdb_init=list(reply.gdb_init))
        return status

    def _quiet_down(self, rt: Any) -> None:
        self._changing(rt)
        try:
            rt.run("down", timeout=DOWN_TIMEOUT_S)
        except HarnessError as exc:
            log.warning("debug on-board: could not stop the board's OpenOCD: %s", exc)

    # -- down -------------------------------------------------------------------------------

    def down(self, session: Any, *, reason: str = "", explicit: bool = True) -> DebugStatus | None:
        """Stop the board's OpenOCD. None: nothing of this route here (this PC's path only).

        With a session this service opened: always. Without one, only when ``explicit`` (a
        user's ``debug down``) and the launcher is detected: a board session someone else
        started is stopped only when asked, never by closing the board."""
        board_id = session.candidate.board_id
        with self._mu:
            live = self._live.pop(board_id, None)
        rt = live.route if live is not None else None
        if rt is None and explicit:
            try:
                rt = self.route(session)
            except HarnessError as exc:
                log.info("debug on-board: down: %s", exc)
                rt = None
            if rt is not None:
                try:
                    if self._ask(rt).state == "down":
                        return DebugStatus(state="down", where="board",
                                           detail=reason or "OpenOCD on the board is not running")
                except HarnessError:
                    pass                      # ask it down anyway
        if rt is None:
            return None
        self._changing(rt)
        try:
            res = rt.run("down", timeout=DOWN_TIMEOUT_S)
        finally:
            if live is not None:
                rt.release()
        reply = parse_reply(res.stdout)
        pid = live.reply.pid if live is not None else (reply.pid if reply else 0)
        self.svc._mark_ended(board_id, pid=pid, why="on-board OpenOCD stopped")
        detail = reason or "stopped on the board"
        if res.returncode != RC_OK and reply is not None and reply.state not in ("down",):
            err = launcher_error("stop", res.returncode, reply)
            self.svc._publish(board_id, "failed", detail=str(err), where="board")
            raise err
        if reply is None and res.returncode != RC_OK:
            err = ActionFailedError(f"`{getattr(rt, 'launcher', 'the launcher')} down` failed "
                                    f"(exit {res.returncode}: {_tail(res)})")
            self.svc._publish(board_id, "failed", detail=str(err), where="board")
            raise err
        self.svc._publish(board_id, "down", pid=pid, detail=detail, where="board")
        return DebugStatus(state="down", pid=pid, detail=detail, where="board")

    # -- status -----------------------------------------------------------------------------

    def status(self, session: Any) -> DebugStatus | None:
        """The board's session as the launcher says now; None: this PC's path (today's)."""
        board_id = session.candidate.board_id
        live = self.live(board_id)
        if live is not None:
            try:
                reply = self._ask(live.route)
            except HarnessError as exc:
                return replace(live.status, detail=f"{live.status.detail} (last known; the "
                                                   f"board did not answer: {exc.message})")
            return self._reconcile(board_id, live, reply)
        try:
            rt = self.route(session)
        except HarnessError as exc:
            if self.mode() == "true":
                return DebugStatus(state="down", where="board", detail=f"not available: {exc}")
            log.info("debug on-board: status: %s", exc)
            return None
        if rt is None:
            return None
        try:
            reply = self._ask(rt)
        except HarnessError as exc:
            return DebugStatus(state="down", where="board",
                               detail=f"the board's launcher did not answer: {exc.message}")
        return self._foreign(board_id, reply)

    def _reconcile(self, board_id: str, live: _Live, reply: LauncherReply) -> DebugStatus:
        if reply.state == "up":
            return live.status
        if reply.state == "starting":
            return replace(live.status, state="starting")
        with self._mu:
            if self._live.get(board_id) is live:
                self._live.pop(board_id, None)
        live.route.release()
        self.svc._mark_ended(board_id, pid=live.reply.pid, why="on-board OpenOCD ended")
        if reply.swap_stopped():
            with self._mu:
                self._closed_for[board_id] = SWAP_REASON
            self.svc._publish(board_id, "down", detail=SWAP_REASON, where="board")
            return DebugStatus(state="down", where="board", detail=SWAP_REASON)
        if reply.state == "failed":
            err = launcher_error("keep running", RC_FAILED, reply)
            self.svc._publish(board_id, "failed", detail=err.message, where="board")
            return DebugStatus(state="failed", where="board", detail=err.message)
        why = f": {reply.message}" if reply.message else " (its 2 h idle stop, or by hand)"
        detail = f"OpenOCD on the board is no longer running{why}"
        self.svc._publish(board_id, "down", detail=detail, where="board")
        return DebugStatus(state="down", where="board", detail=detail)

    def _foreign(self, board_id: str, reply: LauncherReply) -> DebugStatus:
        """The board's state when this service holds no session there."""
        names = tuple(n for n, _p in reply.cores)
        with self._mu:
            closed_for = self._closed_for.get(board_id, "")
        if reply.swap_stopped() or (reply.state == "down" and closed_for):
            return DebugStatus(state="down", where="board", detail=closed_for or SWAP_REASON)
        if reply.state in ("up", "starting"):
            return DebugStatus(state=reply.state, config=reply.cfg, pid=reply.pid, cores=names,
                               where="board", via=reply.via, capabilities=reply.capabilities,
                               gdb_init=reply.gdb_init,
                               detail=f"OpenOCD runs on the board (pid {reply.pid}, "
                                      f"{reply.what()}, via {VIA_WORDS.get(reply.via, reply.via)}), "
                                      "not opened here: `debug up"
                                      + (" --via hostio" if reply.via == VIA_HOSTIO else "")
                                      + "` forwards its gdb ports to this PC")
        if reply.state == "failed":
            return DebugStatus(state="failed", where="board",
                               detail=launcher_error("keep running", RC_FAILED, reply).message)
        return DebugStatus(state="down", where="board", detail="OpenOCD on the board is not "
                                                               "running")

    # -- detect (the IDCODE) -----------------------------------------------------------------

    def idcode(self, session: Any) -> str | None:
        """The TAP IDCODE from the board's OpenOCD; None: this PC's path. With no session up
        on the board, one is started for the read and stopped again (the core is never halted:
        the launcher's configs examine it only on a gdb attach)."""
        board_id = session.candidate.board_id
        live = self.live(board_id)
        rt = live.route if live is not None else self.route(session)
        if rt is None:
            return None
        reply = self._ask(rt)
        if reply.state in ("up", "starting"):
            if reply.idcode:
                return reply.idcode
            if reply.state == "starting":
                raise ActionFailedError("OpenOCD on the board is still starting",
                                        hint="retry in a few seconds")
            raise NothingOnTargetError("the board's OpenOCD reports no TAP on the chain",
                                       hint="the loaded design may have no debug port")
        if live is not None:
            self._reconcile(board_id, live, reply)       # ours ended on the board: say so
        reply = self._launch(rt, rt.design_name() or "auto")
        try:
            idcode = reply.idcode
        finally:
            if not reply.already:                        # a one-shot: stop what it started
                self._quiet_down(rt)
                self.svc._mark_ended(board_id, pid=reply.pid, why="on-board detect")
        if not idcode:
            raise NothingOnTargetError(f"the board's OpenOCD found no TAP ({reply.what()})",
                                       hint="the loaded design may have no debug port")
        return idcode

    # -- this PC's OpenOCD turned away ------------------------------------------------------

    def held_by_board(self, session: Any, exc: BaseException) -> HarnessError | None:
        """When this PC's OpenOCD was turned away (held, or unreachable) by a board whose own
        OpenOCD holds JTAG: exit 4 with ``BUSY_HINT``. None: not that (the error stands)."""
        if not isinstance(exc, (HeldError, UnreachableError)):
            return None
        rt = route_of(session)
        if rt is None:
            return None
        try:
            if rt.plan()[0] != READY:
                return None
            det = self.detect(rt, session)
            if not det.present:
                return None
            reply = self._ask(rt)
        except HarnessError as err:
            log.info("debug on-board: could not ask the board after a refusal: %s", err)
            return None
        if reply.state not in ("up", "starting"):
            return None
        who = f"OpenOCD on the board (pid {reply.pid}, {reply.what()})"
        return HeldError(f"the board's JTAG is held by the on-board OpenOCD session "
                         f"({reply.what()}, pid {reply.pid}), so it turned this PC's OpenOCD "
                         f"away: {getattr(exc, 'message', exc)}", holder=who, hint=BUSY_HINT)

    # -- swaps and closing ------------------------------------------------------------------

    def down_first(self, session: Any, *, force: bool = False) -> tuple[DownFirst, Any]:
        """DEBUG-DOWN-FIRST (``DeployService``, before ``deploy.started``): ask the board's
        OpenOCD down whoever started it. Returns what it found, and the session of this
        service's to reopen after a verified swap (None: none). Raises the refusal (15)
        before anything changes: a session of ours stays open then."""
        board_id = session.candidate.board_id
        live = self.live(board_id)
        rt = live.route if live is not None else ready_route(session)
        if rt is None:
            return DownFirst(), None
        self._changing(rt)
        got = settle(ask_down(rt), force=force, lock=lambda: has_lock(rt))
        log.info("debug on-board: down before the swap: %s", got.word)
        if live is None:
            return got, None
        # ours: closed for the swap (one down, asked above), reopened after a verified one
        with self._mu:
            if self._live.get(board_id) is live:
                self._live.pop(board_id, None)
            self._closed_for[board_id] = SWAP_REASON
            self._reopen_via[board_id] = live.status.via
        live.route.release()
        self.svc._mark_ended(board_id, pid=live.reply.pid, why="on-board OpenOCD stopped")
        detail = SWAP_REASON if got.ok else \
            f"{SWAP_REASON}; the board did not confirm the stop ({got.why})"
        self.svc._publish(board_id, "down", pid=live.reply.pid, detail=detail, where="board")
        return got, live.session

    def before_swap(self, board_id: str) -> Any:
        """``deploy.started`` with no DEBUG-DOWN-FIRST before it (``down_first`` asked the
        board already for every ``DeployService`` deploy): stop the board's OpenOCD first.
        Returns the session to reopen (one this service had open), else None. A launcher known
        on this board is asked down even with no session of ours (the launcher's watchdog is
        the backstop); being down already, it answers down."""
        live = self.live(board_id)
        if live is not None:
            with self._mu:
                self._reopen_via[board_id] = live.status.via
            try:
                self.down(live.session, reason=SWAP_REASON, explicit=False)
            except HarnessError as exc:
                log.warning("debug on-board: stopping for the swap: %s (the board's watchdog "
                            "stops it)", exc)
            with self._mu:
                self._closed_for[board_id] = SWAP_REASON
            return live.session
        with self._mu:
            ref = self._seen.get(board_id)
        rt = ref() if ref is not None else None
        if rt is not None:
            self._quiet_down(rt)
        return None

    def boards(self) -> list[str]:
        with self._mu:
            return list(self._live)

    def close_board(self, board_id: str, reason: str) -> None:
        live = self.live(board_id)
        if live is None:
            return
        try:
            self.down(live.session, reason=reason, explicit=False)
        except HarnessError as exc:
            log.warning("debug on-board: closing %s: %s", board_id, exc)


__all__ = ["BUSY_HINT", "HOSTIO_CAPABILITIES", "HOSTIO_GDB_INIT", "HOSTIO_LIMITS",
           "HOSTIO_VERIFY_NOTE", "JTAG_CAPABILITIES", "JTAG_ONLY", "VIAS", "VIA_HOSTIO",
           "VIA_JTAG", "hostio_error", "DOWN_FIRST_DATA", "DOWN_FIRST_HINT", "DownFirst", "LOCK_CAPABILITY",
           "LauncherReply", "MODES", "ON_BOARD_ENV", "OnBoard", "OnBoardRoute", "SCHEMA",
           "SETTING", "SWAP_DETAIL", "ask_down", "busy_words", "down_first", "has_lock",
           "launcher_error", "mode", "parse_design_hostio", "parse_capabilities", "parse_reply", "ready_route",
           "route_of", "settle"]
