"""The MPS3's ``XvcAdapter``: where the harness's XVC server is, and the probes files (lane XVC-CORE, X2).

``pack.py`` wires ``make_xvc_adapter(session)`` (CCR X-8); ``harness_manager.services.xvc``
drives it (docs/design/XVC_DEBUG.md). What it knows about the MPS3:

**What XVC reaches.** The firmware's ``xvc_server`` (TCP 2542) drives the Debug Bridge
(``debug_bridge_0``, feature ``xvc_dbgbr``): the reconfigurable partition's debug hub
and ILAs (and, on the Linux harness, the static MIG calibration hub behind the same
bridge). Never whole-device JTAG. An image built with ``XVC_TARGET=jtagbb``
(``xvc_jtagbb``) drives ``jtag_bb`` instead: no ILAs, so XVC is refused there with the
reason.

**Reach** (``xvc_facts()["reach"]``), from boards.toml ``xvc`` (default ``"auto"``)::

    [boards.lab]
    xvc = { reach = "auto", user = "root" }     # auto | hub | board-ssh | direct

- ``board-ssh`` (D-X1; ``auto`` on the Linux harness): ``ssh -J HUB root@BOARD -L
  127.0.0.1:<p>:127.0.0.1:2542`` through ``tunnel.SshTunnel`` (``jump``/``user``, CCR X-1).
  The user's key on the board is the authentication, and the hub's port-22 lease gate
  the lease. With the harness's XVC lock (feature ``xvc_lock``, a HARNESSD request for
  after cutover) 2542 answers only the board itself; without it the reach still works
  and the adapter warns exactly as on bare-metal.
- ``hub`` (``auto`` on bare-metal, behind a hub): the board's existing hub tunnel,
  which already forwards 2542 (``tunnel.py``). Unauthenticated: the X6 warning.
- ``direct``: the shell's own address (a board on your desk). Unauthenticated too.

**Probes files** (``xvc_probes``): the RM's ``.ltx`` from the overlay catalogue
(``ltx_for``: ``ltx_path()``, which serves the store's copy since lane FIXES, CCR X-2), the
static's (Linux: the MIG view) and a full-design one from ``statics.StaticStore`` or the
manifest's ``full_ltx`` key. The service prefers the full-design file (X5).

Test seams: ``XVC_PORT_ENV`` (the board's XVC port for a DIRECT board; the lab's is
2542), and ``tunnel.DEFAULT_LAUNCHER``/``DEFAULT_SSH_G`` for the board-SSH tunnel.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pyverify import rm_id as rmid

from harness_manager.core.errors import HarnessError, UnreachableError, UsageError
from harness_manager.core.model import LinkKind
from harness_manager.services.xvc import PARTITION_SCOPE, UNAUTHENTICATED_WARNING

from . import tunnel as _tunnel
from .constants import IMPL_LINUX, XVC_PORT
from .statics import StaticStore, canonical_id, crc32_of

log = logging.getLogger(__name__)

XVC_PORT_ENV = "HARNESS_MANAGER_MPS3_XVC_PORT"
DBGBR_FEATURE = "xvc_dbgbr"
JTAGBB_FEATURE = "xvc_jtagbb"
#: The Linux harness's XVC lock (docs/design/XVC_DEBUG.md §7.1): claimed boards answer 2542
#: from loopback peers only. A HARNESSD request for after cutover; the name is HM's proposal.
LOCK_FEATURE = "xvc_lock"
REACH_HUB = "hub-tunnel"
REACH_BOARD_SSH = "board-ssh"
REACH_DIRECT = "direct"
REACH_CHOICES = ("auto", "hub", "board-ssh", "direct")
DEFAULT_BOARD_USER = "root"
TARGET = "debug_bridge_0 (the Debug Bridge, XAPP1251 layout, at 0x44A8)"

JTAGBB_REASON = ("2542 on this image drives jtag_bb (the Identify path), not the Debug Bridge: "
                 "no ILAs to debug over XVC")
NO_DBGBR_REASON = "needs harness firmware with 'xvc_dbgbr' (v0.11 or later)"
NO_LOCK_NOTE = ("this Linux harness does not report the XVC lock ('xvc_lock') yet, so 2542 "
                "still answers anyone on the board network; the board-SSH reach is yours, "
                "the port is not")
PARKED_NOTE = ("after a harness restart the partition stays parked until the next swap: "
               "swap once to bring the RM's ILAs back")
MIG_NOTE = ("the static MIG calibration hub sits behind the same Debug Bridge: load the "
            "static probes file for it (Linux only, while DDR calibrates)")
NO_MIG_NOTE = "the bare-metal static has no MIG debug hub"


# --- the RM's probes file (CCR X-2, landed by lane FIXES in 287b6b2) -----------------------


def ltx_for(entry: Any) -> Path | None:
    """The RM's ``.ltx`` for one overlay catalogue entry (or a pyverify ``Overlay``), or None.

    ``ltx_path()`` answers for both kinds: a directory overlay through pyverify, a
    content-store overlay with the store's copy (``_StoredOverlay.ltx_path``, lane FIXES;
    None for an overlay imported without one). A path that is not a file is None.
    """
    overlay = getattr(entry, "overlay", entry)
    fn = getattr(overlay, "ltx_path", None)
    try:
        path = fn() if callable(fn) else None
    except Exception:  # noqa: BLE001 - a probes file is never worth failing a session
        log.exception("looking up the .ltx of %r failed", overlay)
        return None
    if not path:
        return None
    path = Path(path)
    return path if path.is_file() else None


# --- boards.toml ------------------------------------------------------------------------------


def xvc_config(candidate: Any) -> dict[str, str]:
    """boards.toml ``xvc`` for this board: ``{reach, user, host}`` (a string is the reach)."""
    from .hub import board_tables

    try:
        raw = board_tables(candidate).get("xvc", {})
    except HarnessError:
        raw = {}
    where = f"boards.toml xvc for {candidate.board_id}"
    if isinstance(raw, str):
        raw = {"reach": raw}
    if not isinstance(raw, dict):
        raise UsageError(f"{where} must be a table: {{ reach = \"auto\", user = \"root\" }}")
    unknown = set(raw) - {"reach", "user", "host"}
    if unknown:
        raise UsageError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    reach = raw.get("reach", "auto")
    if reach not in REACH_CHOICES:
        raise UsageError(f"{where}.reach must be one of {', '.join(REACH_CHOICES)}")
    out = {"reach": reach}
    for key in ("user", "host"):
        value = raw.get(key, "")
        if not isinstance(value, str) or any(c.isspace() for c in value) or \
                value.startswith("-"):
            raise UsageError(f"{where}.{key} must be a {key} name")
        if value:
            out[key] = value
    return out


def _ssh_link(candidate: Any) -> tuple[str, str]:
    """``(user, host)`` of the candidate's SSH link (``ssh://root@host:22``), or ``("", "")``."""
    for lk in candidate.links:
        if lk.kind == LinkKind.SSH:
            rest = lk.address.split("://", 1)[-1]
            user, _, hostport = rest.rpartition("@")
            host = hostport.rsplit(":", 1)[0] if hostport.count(":") == 1 else hostport
            return user, host.strip("[]")
    return "", ""


def _jump_host(candidate: Any) -> str:
    """The hub to jump through: the candidate's ``via ssh:HOST``, else its hub table's SSH host."""
    via = _tunnel.candidate_via(candidate)
    if via.startswith(f"{_tunnel.VIA_SSH}:"):
        return via.split(":", 1)[1]
    try:
        from .hub import LOCAL_HOSTS, hub_config_for

        cfg = hub_config_for(candidate)
    except HarnessError:
        return ""
    if cfg is None:
        return ""
    host = (cfg.rest.ssh_host if cfg.rest is not None and cfg.rest.ssh_host else cfg.host) or ""
    return "" if host in LOCAL_HOSTS else host


# --- the adapter ------------------------------------------------------------------------------


class Mps3Xvc:
    """``core.pack.XvcAdapter`` for an MPS3 session (module docstring)."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._ident = getattr(session.candidate, "identity", None)
        self._store: Any = None
        self._mu = threading.Lock()
        self._tunnel: _tunnel.SshTunnel | None = None

    # -- facts ------------------------------------------------------------------------------

    def xvc_note_identity(self, identity: Any) -> None:
        """The service read the board's identity: use it (features, impl, rm, static)."""
        if identity is not None:
            self._ident = identity

    def use_store(self, store: Any) -> None:
        self._store = store

    def _features(self) -> frozenset[str]:
        return frozenset(getattr(self._ident, "features", ()) or ())

    def _impl(self) -> str:
        return str(getattr(self._ident, "harness_impl", "") or "")

    def _config(self) -> dict[str, str]:
        return xvc_config(self._session.candidate)

    def _has_hub_tunnel(self) -> bool:
        reach = getattr(self._session, "reach", None)
        ports = getattr(reach, "ports", None) or {}
        return bool(ports.get("xvc"))

    def reach(self) -> str:
        """``board-ssh`` | ``hub-tunnel`` | ``direct`` for this board now."""
        want = self._config()["reach"]
        if want == "board-ssh" or (want == "auto" and self._impl() == IMPL_LINUX):
            return REACH_BOARD_SSH
        if want in ("auto", "hub") and self._has_hub_tunnel():
            return REACH_HUB
        return REACH_DIRECT

    def xvc_reason(self) -> str:
        try:
            reach = self.reach()
        except UsageError as exc:
            return exc.message
        feats = self._features()
        if JTAGBB_FEATURE in feats and DBGBR_FEATURE not in feats:
            return JTAGBB_REASON
        if DBGBR_FEATURE not in feats:
            return NO_DBGBR_REASON
        if reach == REACH_BOARD_SSH and self._impl() != IMPL_LINUX:
            return ("boards.toml xvc.reach = \"board-ssh\" needs the Linux harness; this board "
                    f"runs the {self._impl() or 'bare-metal'} harness (no SSH)")
        if reach == REACH_BOARD_SSH and not self._board_host():
            return "no board address for the board-SSH reach (set boards.toml xvc.host)"
        return ""

    def _authenticated(self, reach: str) -> bool:
        return reach == REACH_BOARD_SSH and LOCK_FEATURE in self._features()

    def xvc_facts(self) -> dict[str, Any]:
        try:
            reach = self.reach()
        except UsageError as exc:
            return {"scope": PARTITION_SCOPE, "reach": "", "impl": self._impl(),
                    "authenticated": False, "warnings": [UNAUTHENTICATED_WARNING],
                    "target": TARGET, "notes": [exc.message]}
        linux = self._impl() == IMPL_LINUX
        auth = self._authenticated(reach)
        notes: list[str] = []
        if linux and not auth:
            notes.append(NO_LOCK_NOTE if reach == REACH_BOARD_SSH else
                         "the hub tunnel is not authenticated; the board-SSH reach is "
                         "(boards.toml xvc.reach = \"auto\")")
        notes.append(MIG_NOTE if linux else NO_MIG_NOTE)
        if not linux:
            notes.append(PARKED_NOTE)
        facts: dict[str, Any] = {
            "scope": PARTITION_SCOPE, "reach": reach, "impl": self._impl() or "bare-metal",
            "authenticated": auth, "lock": LOCK_FEATURE in self._features(),
            "warnings": [] if auth else [UNAUTHENTICATED_WARNING], "target": TARGET,
            "notes": notes}
        if reach == REACH_BOARD_SSH:
            facts["board_ssh"] = {"host": self._board_host(), "user": self._board_user(),
                                  "jump": _jump_host(self._session.candidate)}
        return facts

    # -- the endpoint -------------------------------------------------------------------------

    def _board_host(self) -> str:
        cfg = self._config()
        if cfg.get("host"):
            return cfg["host"]
        _, host = _ssh_link(self._session.candidate)
        if host:
            return host
        reach = getattr(self._session, "reach", None)
        if getattr(reach, "remote_host", ""):
            return reach.remote_host
        eth = next((lk for lk in self._session.candidate.links
                    if lk.kind == LinkKind.ETHERNET), None)
        if eth is None:
            return ""
        from .shell import parse_endpoint

        return parse_endpoint(eth.address, 0)[0]

    def _board_user(self) -> str:
        cfg = self._config()
        user, _ = _ssh_link(self._session.candidate)
        return cfg.get("user") or user or _ssh_user(self._session) or DEFAULT_BOARD_USER

    def _direct_port(self) -> int:
        env = os.environ.get(XVC_PORT_ENV, "").strip()
        return int(env) if env else XVC_PORT

    def xvc_endpoint(self) -> tuple[str, int]:
        reach = self.reach()
        if reach == REACH_BOARD_SSH:
            return "127.0.0.1", self._board_ssh().local_port("xvc")
        if reach == REACH_HUB:
            r = self._session.reach
            return r.host, int(r.ports["xvc"])
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnreachableError("this session has no Ethernet link to the harness")
        return shell.host, self._direct_port()

    def _board_ssh(self) -> _tunnel.SshTunnel:
        """``ssh -J HUB USER@BOARD -L 127.0.0.1:<p>:127.0.0.1:2542``, started once, supervised."""
        with self._mu:
            if self._tunnel is not None:
                return self._tunnel
            host, user = self._board_host(), self._board_user()
            jump = _jump_host(self._session.candidate)
            # LINUX-CLAIM: a claimed board's pinned host key and claimed key (boards.<b>.ssh),
            # and the hub's own jump; no pin yet: the user's ssh config, as before.
            claim, options = self._claim_reach()
            if claim is not None:
                jump = claim.proxy_jump()
            label = (f"{self._session.candidate.board_id} xvc: ssh "
                     f"{f'-J {jump} ' if jump else ''}{user}@{host}")
            tunnel = _tunnel.SshTunnel(host, [_tunnel.Forward("xvc", "127.0.0.1", XVC_PORT)],
                                       jump=jump, user=user, label=label, options=options)
            try:
                tunnel.start()
            except UnreachableError as exc:
                if claim is not None:
                    mapped = claim.map_ssh_failure(exc)
                    if mapped is not exc:
                        raise mapped from exc
                raise UnreachableError(
                    f"the board-SSH forward for XVC did not come up: {exc.message}",
                    hint=f"check `ssh {f'-J {jump} ' if jump else ''}{user}@{host} true` works "
                         "without a prompt (your key in the board's authorized_keys); or set "
                         "boards.toml xvc.reach = \"hub\" (unauthenticated)") from exc
            self._tunnel = tunnel
            return tunnel

    def _claim_reach(self) -> tuple[Any, list[str]]:
        """``(claim adapter, its pinned ssh options)`` when this board's host key is pinned;
        ``(None, [])`` otherwise. A changed host key raises (``claim.HostKeyChangedError``)."""
        claim = getattr(self._session, "claim", None)
        if claim is None:
            return None, []
        try:
            if not claim.config()["host_key"]:
                return None, []
        except UsageError:
            return None, []
        claim.check_host_key()
        return claim, claim.pinned_options()

    def xvc_open_failures_since(self, t0: float) -> list[str]:
        tunnel = self._tunnel if self._tunnel is not None else getattr(
            getattr(self._session, "reach", None), "tunnel", None)
        if tunnel is None:
            return []
        return list(tunnel.open_failures_since(t0, wait_s=0.5))

    def xvc_release(self) -> None:
        with self._mu:
            tunnel, self._tunnel = self._tunnel, None
        if tunnel is not None:
            tunnel.close()

    def tunnel_status(self) -> dict[str, Any] | None:
        return self._tunnel.status() if self._tunnel is not None else None

    # -- probes files -------------------------------------------------------------------------

    def _catalogue(self) -> Any:
        deploy = getattr(self._session, "deploy", None)
        cat = getattr(deploy, "catalogue", None)
        if cat is None:
            from .overlays import default_catalogue

            cat = default_catalogue()
        if self._store is not None and hasattr(cat, "use_store"):
            cat.use_store(self._store)
        return cat

    def _entry(self, rm_id: str) -> Any:
        if not rm_id:
            return None
        try:
            want = rmid.parse_rm_id(rm_id)
        except (TypeError, ValueError):
            return None
        shell = str(getattr(self._ident, "shell_id", "") or "")
        found = None
        try:
            entries = self._catalogue().entries()
        except Exception:  # noqa: BLE001 - a broken catalogue is only "no probes file"
            log.exception("reading the overlay catalogue failed")
            return None
        for e in entries:
            if e.overlay.manifest.rm_id != want:
                continue
            if shell and _same(e.ref.static_id, shell):
                return e
            found = found or e
        return found

    def _raw_manifest(self, entry: Any) -> dict[str, Any]:
        source = str(getattr(entry.ref, "source", "") or "")
        path: Path | None = None
        if source.startswith("store:") and self._store is not None:
            try:
                path = Path(self._store.path(source.split(":", 1)[1]))
            except Exception:  # noqa: BLE001
                path = None
        elif source:
            path = Path(source)
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def xvc_rm_name(self, rm_id: str) -> str:
        entry = self._entry(rm_id)
        return entry.ref.name if entry is not None else ""

    def xvc_probes(self, rm_id: str) -> dict[str, Any]:
        linux = self._impl() == IMPL_LINUX
        shell = str(getattr(self._ident, "shell_id", "") or "")
        entry = self._entry(rm_id)
        rm_name = entry.ref.name if entry is not None else str(
            getattr(self._ident, "rm_name", "") or "")
        out: dict[str, Any] = {"rm": None, "static": None, "full": None, "vivado": "",
                               "rm_name": rm_name, "note": ""}
        store = StaticStore()
        if entry is not None:
            raw = self._raw_manifest(entry)
            vivado = str(raw.get("vivado") or getattr(entry.overlay.manifest, "vivado", "")
                         or "")
            out["vivado"] = vivado
            path = ltx_for(entry)
            if path is not None:
                crc_ok = None
                if raw.get("ltx_crc32") is not None:
                    try:
                        crc_ok = crc32_of(path) == int(str(raw["ltx_crc32"]), 0)
                    except (OSError, ValueError):
                        crc_ok = False
                out["rm"] = {"path": path, "name": path.name, "crc_ok": crc_ok,
                             "source": entry.ref.source, "vivado": vivado, "rm_id": rm_id}
            elif entry.overlay.manifest.ltx:
                out["note"] = (f"{rm_name}'s manifest names {entry.overlay.manifest.ltx}, "
                               "but the file is not here (re-import the overlay with it)")
            full_key = raw.get("full_ltx")
            if isinstance(full_key, str) and full_key and not entry.ref.source.startswith(
                    "store:"):
                full = Path(entry.ref.source).parent / full_key
                if full.is_file():
                    out["full"] = {"path": full, "name": full.name, "crc_ok": None,
                                   "source": entry.ref.source, "vivado": vivado}
        elif rm_id:
            out["note"] = f"no overlay in the catalogue for rm_id {rm_id}"
        if shell:
            if out["full"] is None and rm_name:
                out["full"] = store.full_ltx(shell, rm_name)
            static = store.static_ltx(shell) if linux else None
            out["static"] = static
            if linux and static is None:
                out["static_note"] = (f"no static probes file for {canonical_id(shell)}; import "
                                      "the mint's config_rm_greybox_static.ltx")
        if not linux:
            out["static_note"] = NO_MIG_NOTE
        return out


def _same(a: object, b: object) -> bool:
    try:
        return rmid.parse_rm_id(a) == rmid.parse_rm_id(b)
    except (TypeError, ValueError):
        return str(a).lower() == str(b).lower()


def _ssh_user(session: Any) -> str:
    """boards.toml ``ssh.user`` (LINUX-CLAIM), the board-SSH user for everything else."""
    claim = getattr(session, "claim", None)
    try:
        return claim.config()["user"] if claim is not None else ""
    except UsageError:
        return ""


def make_xvc_adapter(session: Any) -> Mps3Xvc | None:
    """The pack hook (CCR X-8): an adapter for any session with an Ethernet shell."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3Xvc(session)
