"""The MPS3 shell over Ethernet, as seen through pyverify.

This module turns pyverify replies into board-agnostic models and pyverify or
socket failures into ``HarnessError``s with exit codes. The control port
accepts ONE client, so every call opens, asks and closes. Never hold 6900 open
(docs: harness handover §5; fpgahub's poller does the same).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TypeVar

from pyverify import rm_id as rmid
from pyverify.client import ShellClient, ShellProtocolError

from socharness.core.errors import ActionFailedError, HeldError, UnreachableError
from socharness.core.model import BoardIdentity, Check, Health

from .constants import KNOWN_DESIGNS

T = TypeVar("T")


def _resolve_rm_name(rm_id: str) -> str:
    """Design name from the overlay manifests (T2), falling back to KNOWN_DESIGNS."""
    try:
        from .overlays import resolve_rm_name
    except ImportError:
        return KNOWN_DESIGNS.get(rmid.design_id(rm_id), "")
    return resolve_rm_name(rm_id)


def parse_endpoint(spec: str, default_port: int) -> tuple[str, int]:
    """'host' | 'host:port' -> (host, port). IPv6 literals need brackets."""
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, int(port)
    return spec, default_port


class Mps3Shell:
    def __init__(self, host: str, port: int, *, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------------

    def call(self, fn: Callable[[ShellClient], T]) -> T:
        """Open 6900, run ``fn``, close. Maps failures to exit-coded errors."""
        try:
            with ShellClient(self.host, self.port, timeout=self.timeout) as client:
                return fn(client)
        except ConnectionRefusedError as exc:
            raise UnreachableError(
                f"shell at {self.host}:{self.port} refused the connection",
                hint="is the board powered and the harness loaded?",
            ) from exc
        except TimeoutError as exc:
            raise UnreachableError(
                f"shell at {self.host}:{self.port} did not answer within {self.timeout}s",
                hint="check the Ethernet link and the board's IP",
            ) from exc
        except ConnectionError as exc:
            # pyverify raises ConnectionError("... closed by peer") on EOF. Accept-then-EOF
            # is how the fielded shell turns away a second client (one client at a time);
            # lwIP may instead reset the connection (RST) when it aborts the extra client.
            if isinstance(exc, ConnectionResetError):
                raise HeldError(
                    f"shell at {self.host}:{self.port} reset the connection",
                    hint="another client probably holds the control port, or the board is restarting",
                ) from exc
            if "closed by peer" in str(exc):
                raise HeldError(
                    f"shell at {self.host}:{self.port} closed the connection",
                    hint="another client probably holds the control port (one client at a time)",
                ) from exc
            raise UnreachableError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        except ShellProtocolError as exc:
            # accept-then-EOF is how the shell turns away a second client today.
            if "EOF" in str(exc) or "closed" in str(exc).lower():
                raise HeldError(
                    f"shell at {self.host}:{self.port} closed the connection",
                    hint="another client probably holds the control port (one client at a time)",
                ) from exc
            raise ActionFailedError(f"shell protocol error: {exc}") from exc

    # -- identity and health ----------------------------------------------------

    def identity(self) -> BoardIdentity:
        def ask(c: ShellClient):
            return c.ping(), c.version()

        ping, ver = self.call(ask)
        if not ping.ok:
            raise ActionFailedError("shell answered ping with ok:false")
        verdict = ver.skew_verdict if ver.ok else "unchecked"
        return BoardIdentity(
            board_type="mps3",
            shell_id=ping.shell_id,
            rm_id=ping.rm_id,
            rm_name=_resolve_rm_name(ping.rm_id) if ping.rm_id else "",
            harness_version=ver.harness if ver.ok else "",
            firmware_sha=ver.sha if ver.ok else "",
            firmware_dirty=bool(ver.dirty) if ver.ok else False,
            features=tuple(ver.features) if ver.ok else (),
            build_check={"ok": Check.OK, "SKEW": Check.MISMATCH}.get(verdict, Check.UNCHECKED),
        )

    def health(self) -> Health:
        try:
            diag = self.call(lambda c: c.diag())
        except UnreachableError:
            return Health(reachable=False, control_channel="offline")
        except HeldError:
            return Health(reachable=True, control_channel="busy")
        if not diag.ok:
            # A harness without `diag` (e.g. the July Linux v0.7 daemons) is a legitimate
            # older harness, not a fault: reachable, idle, just no counters.
            return Health(reachable=True, control_channel="idle",
                          notes=("this harness does not report diagnostic counters (no `diag` verb)",))
        counters = {
            k: v
            for k, v in dataclasses.asdict(diag).items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        notes = []
        if counters.get("svc_skipped"):
            notes.append("superloop skipped services (control channel may be starved)")
        return Health(reachable=True, control_channel="idle", counters=counters, notes=tuple(notes))

    # -- simple verbs -----------------------------------------------------------

    def reset_dut(self) -> None:
        resp = self.call(lambda c: c.reset("dut"))
        if not resp.ok:
            raise ActionFailedError("shell refused reset target 'dut'")
