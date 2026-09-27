"""The MPS3 debug adapter: which OpenOCD configuration debugs the loaded design.

``harness_manager_mps3.pack`` calls ``make_debug_adapter(session)`` (the T4
hook) when it opens a session, and the returned adapter replaces the scaffold
one. ``harness_manager.services.debug`` turns it into an OpenOCD command line.

The debug path on the MPS3, proven on silicon::

    OpenOCD remote_bitbang -> TCP 6921 on the board -> firmware jtag_server
      -> the RP jtag_* pins -> CoreSight SoC-400 SWJ-DP (TAP 0x6ba00477) -> Cortex-M0

It exists only while a design with a DAP is loaded. The table below maps the
loaded ``rm_id`` design number to the OpenOCD target half from
``mps3-nanosoc-platform/host/openocd`` (constants.DAP_DESIGN_CONFIGS):

| design              | target half                                   |
|---------------------|-----------------------------------------------|
| nanosoc, nanosoc_upy| nanosoc_mps3_jtag.cfg + nanosoc_ops.tcl       |
| nanosoc_iice        | nanosoc_iice_chain.cfg (+ nanosoc_ops.tcl)    |
| nanosoc_multicore   | none yet: needs a two-AP config (handover B4) |
| anything else       | none: no debug port (greybox, led, ...)       |

Three rules from the configs themselves:

1. **The probe half is Tcl variables, not commands.** The target-half configs
   read ``RBB_HOST``/``RBB_PORT``/``TRANSPORT_MODE`` with
   ``if {![info exists RBB_HOST]} {...}`` (nanosoc_mps3_jtag.cfg:44-48), so the
   ``set`` overrides must run BEFORE the ``-f``; after it they are silently
   ignored and OpenOCD dials the default 192.168.10.101:6921 (harness
   handover B4).
2. **Deferred examine.** Both configs create the core with ``-defer-examine``
   (AHBSLV=0: PPB reads while the core runs mirror the fetch stream,
   nanosoc_mps3_jtag.cfg:84-91). The core is examined only by
   ``nanosoc_halt_examine``. OpenOCD fires ``gdb-attach`` BEFORE it refuses an
   unexamined target (v0.12.0 src/server/gdb_server.c:1037 vs :1077), so a
   ``gdb-attach`` hook running ``nanosoc_halt_examine`` lets gdb attach. The
   hook must be set before ``init``: ``init_target_events`` installs the
   default ``halt 1000`` only when no handler is set (src/target/startup.tcl:
   202-215), and ``halt`` fails on an unexamined core.
3. **One client.** The board's jtag_server serves one OpenOCD at a time
   (jtag_server.c:185-192); a second is accepted and closed at once.

**The claim lock** (lane CLAIMED-LOCK). A claimed Linux board serves 6921 to the board itself
only; through the hub's tunnel it answers one line (``jtag locked: board claimed (use ssh)``,
code ``locked``) and closes. So ``openocd_config`` asks the session's claim
(``claim.lock_route``) before OpenOCD starts: a board this Harness Manager claimed is dialled
through the session's board-SSH forward (``claim.hold_forward("debug")``: RBB_HOST
127.0.0.1, RBB_PORT its local end of ``-L ...:127.0.0.1:6921``), held while the OpenOCD runs
and let go by ``debug_release`` (the debug service calls it when the OpenOCD ends); a board
claimed by another key, or with no pin here, is ``ClaimLockedError`` with the claim hint
before anything dials. Bare metal and unclaimed boards keep the shell's endpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyverify import rm_id as rmid

from harness_manager.core.errors import NothingOnTargetError, UnavailableError
from harness_manager.core.pack import BoardSession

from .constants import DAP_DESIGN_CONFIGS, JTAG_RBB_PORT, KNOWN_DESIGNS, OPENOCD_CFG_DIR

CFG_DIR_ENV = "HARNESS_MANAGER_MPS3_OPENOCD_DIR"
MULTICORE_DESIGN = 0x0003          # constants.KNOWN_DESIGNS: nanosoc_multicore
DAP_TAP_IDCODE = "0x6ba00477"      # nanosoc_mps3_jtag.cfg: _DAP_TAPID (silicon-verified)

# Every config in DAP_DESIGN_CONFIGS creates the core as nanosoc.cpu0 and
# defines nanosoc_halt_examine (nanosoc_mps3_jtag.cfg:84-104,
# nanosoc_iice_chain.cfg:126-143).
_GDB_ATTACH_HOOKS = (("nanosoc.cpu0", "nanosoc_halt_examine"),)


@dataclass(frozen=True)
class DesignDebug:
    """How to debug one loaded design."""

    design: int
    name: str
    configs: tuple[str, ...]
    gdb_attach: tuple[tuple[str, str], ...] = _GDB_ATTACH_HOOKS   # (target, Tcl proc)


def design_debug(design: int) -> DesignDebug:
    """The debug recipe for design number ``design`` (``rm_id & 0xFFFF``).

    ``NothingOnTargetError`` (exit 13) when the design has no debug port.
    """
    name = KNOWN_DESIGNS.get(design, f"design 0x{design:04x}")
    cfgs = DAP_DESIGN_CONFIGS.get(design)
    if cfgs is not None:
        return DesignDebug(design, name, tuple(cfgs))
    if design == MULTICORE_DESIGN:
        raise NothingOnTargetError(
            "nanosoc_multicore: two-AP config not available yet",
            hint="its OpenOCD config (one AP per core) is harness handover B4; "
                 "debug nanosoc or nanosoc_upy meanwhile",
        )
    raise NothingOnTargetError(
        f"the loaded design ({name}) has no debug port",
        hint="load nanosoc, nanosoc_upy or nanosoc_iice first",
    )


def config_dir() -> Path | None:
    """The setting ``mps3.openocd_cfg_dir``, read at call time: ``CFG_DIR_ENV``, then the
    Settings menu / ``settings.toml`` (lane SET-WIRE); else ``OPENOCD_CFG_DIR``: the
    platform repo's when it is checked out alongside, else the packaged copy."""
    from .settings import value

    configured = value("mps3.openocd_cfg_dir")      # CFG_DIR_ENV first
    if configured:
        return Path(configured)
    return OPENOCD_CFG_DIR


_RBB_PORT_RE = re.compile(r"^set\s+RBB_PORT\s+(\d+)\s*$")


def _rbb_port_of(session: BoardSession) -> int:
    """The board's jtag_server port as the pack was configured.

    ``Mps3Pack(rbb_port=...)`` passes it only to the scaffold adapter, which the
    pack builds before calling this hook. Read it back through the scaffold's
    public ``openocd_probe_args()``; a ``session.rbb_port`` attribute wins if the
    pack ever grows one (contract change request T4-1).
    """
    port = getattr(session, "rbb_port", None)
    if isinstance(port, int) and port > 0:
        return port
    scaffold = getattr(session, "debug", None)
    if scaffold is not None and hasattr(scaffold, "openocd_probe_args"):
        for arg in scaffold.openocd_probe_args():
            m = _RBB_PORT_RE.match(arg)
            if m:
                return int(m.group(1))
    return JTAG_RBB_PORT


class Mps3DebugAdapter:
    """``core.pack.DebugAdapter`` for the MPS3 (replaces the scaffold ``Mps3Debug``).

    Beyond the frozen protocol it offers ``openocd_post_config()`` (the ``-c``
    commands that go after the target half, before ``init``) and
    ``expected_idcode`` (contract change request T4-2).
    """

    expected_idcode = DAP_TAP_IDCODE

    def __init__(self, session: BoardSession, *, host: str, rbb_port: int,
                 cfg_dir: Path | None = None) -> None:
        self._session = session
        self.host = host
        self.rbb_port = rbb_port
        self._cfg_dir = cfg_dir
        self._last: DesignDebug | None = None
        #: CLAIMED-LOCK: the route ``openocd_config`` chose ("" the shell's endpoint,
        #: "board-ssh" the claim forward) and the forward while an OpenOCD holds it.
        self.route = ""
        self._impl = ""
        self._forward: Any = None

    @property
    def cfg_dir(self) -> Path | None:
        return self._cfg_dir if self._cfg_dir is not None else config_dir()

    def design(self) -> DesignDebug:
        """Ask the board what is loaded and return its debug recipe (or raise 13)."""
        ident = self._session.identity()
        self._impl = str(getattr(ident, "harness_impl", "") or "")
        if not ident.rm_id:
            raise NothingOnTargetError("the board reports no loaded design",
                                       hint="load nanosoc, nanosoc_upy or nanosoc_iice first")
        self._last = design_debug(rmid.design_id(ident.rm_id))
        return self._last

    # -- core.pack.DebugAdapter ------------------------------------------------------

    def openocd_probe_args(self) -> tuple[str, ...]:
        # Rule 1: these are consumed by the target half, so they precede every -f.
        # RBB_HOST first: callers and tests key on it. TRANSPORT_MODE is pinned so a
        # future default flip in the config cannot silently change the path.
        host, port = self._endpoint()
        return (
            f"set RBB_HOST {host}",
            f"set RBB_PORT {port}",
            "set TRANSPORT_MODE rbb",
        )

    # -- the claim lock (CLAIMED-LOCK) ---------------------------------------------------

    def _claim_route(self) -> str:
        """The claim's route for 6921 ("" or "board-ssh"); ``ClaimLockedError`` for a board
        this Harness Manager cannot enter, before anything dials."""
        claim = getattr(self._session, "claim", None)
        if claim is None or not callable(getattr(claim, "lock_route", None)):
            return ""
        return claim.lock_route("the debug session (JTAG 6921)", impl=self._impl)

    def _endpoint(self) -> tuple[str, int]:
        """Where OpenOCD dials: the shell's (hub tunnel or LAN), or on a claimed board the
        local end of the session's board-SSH forward (held until ``debug_release``)."""
        if self.route != "board-ssh":
            return self.host, self.rbb_port
        if self._forward is None:
            self._forward = self._session.claim.hold_forward("debug")
        return "127.0.0.1", self._forward.local_port("rbb")

    def debug_release(self) -> None:
        """The OpenOCD ended (``services.debug`` calls it): let the claim forward go."""
        forward, self._forward = self._forward, None
        if forward is not None:
            self._session.claim.release_forward("debug")

    def openocd_search_paths(self) -> tuple[Path, ...]:
        d = self.cfg_dir
        return (d,) if d is not None else ()

    def openocd_config(self) -> tuple[str, ...]:
        recipe = self.design()
        self.route = self._claim_route()          # a locked board is refused here, first
        d = self.cfg_dir
        missing = [c for c in recipe.configs if d is None or not (d / c).is_file()]
        if missing:
            where = str(d) if d is not None else "no directory"
            raise UnavailableError(
                "debug_dut",
                f"the MPS3 OpenOCD configs ({', '.join(missing)}) are not in {where}; "
                f"set {CFG_DIR_ENV} to mps3-nanosoc-platform/host/openocd",
            )
        return recipe.configs

    # -- extensions (CCR T4-2) -------------------------------------------------------------

    def openocd_post_config(self) -> tuple[str, ...]:
        """Rule 2: gdb-attach hooks, after the target half and before ``init``."""
        recipe = self._last or self.design()
        return tuple(f"{target} configure -event gdb-attach {proc}"
                     for target, proc in recipe.gdb_attach)

    def describe(self) -> str:
        if self.route == "board-ssh":
            return "remote_bitbang 127.0.0.1:6921 on the board, through its SSH (claimed)"
        return f"remote_bitbang {self.host}:{self.rbb_port}"


def make_debug_adapter(session: BoardSession) -> Mps3DebugAdapter | None:
    """The pack hook. ``None`` without an Ethernet link: 6921 is on the shell."""
    shell = getattr(session, "shell", None)
    if shell is None:
        return None
    return Mps3DebugAdapter(session, host=shell.host, rbb_port=_rbb_port_of(session))
