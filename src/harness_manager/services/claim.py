"""The SSH claim of a board's harness, and its SSH reach (lane LINUX-CLAIM).

Board-agnostic: the pack's ``session.claim`` adapter does the board's side (for the MPS3,
``harness_manager_mps3.claim``: identify, the TOFU ``authorized_keys`` put, the host-key pin,
the ``ssh -J HUB root@BOARD`` reach). This service adds the rules every front end shares:

- **A claim is never automatic.** ``claim`` refuses without ``confirm=True`` (the CLI asks, the
  API needs ``{"confirm": true}``) and nothing else in Harness Manager ever calls it.
- **It needs the lease.** On a board behind a hub the lease holder only (the same check as
  XVC's): claiming someone else's leased board would lock them out of its slots. A board with
  no hub has no lease; the session lock is the gate.
- ``status`` is what ``info`` shows (``BoardInfo.claim``); ``refresh=True`` asks the board now,
  through the hub when there is one (``info`` never does that round trip).
- A change publishes ``board.claim`` ``{board_id, state, ...}`` on the bus.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HarnessError, HeldError, RefusedError, UnavailableError
from harness_manager.core.events import Event, EventBus

CAPABILITY = "ssh_claim"
STATE_DIR_ENV = "HARNESS_MANAGER_STATE_DIR"
NO_ADAPTER = ("this board's pack has no SSH claim (the claim is for the Linux harness; "
              "bare metal has no SSH)")


def _bus_of(engine: Any) -> EventBus | None:
    if isinstance(engine, EventBus):
        return engine
    return getattr(engine, "bus", None)


class ClaimService:
    """``engine.board_claim``. ``leases``: an object with ``view(hub) -> {lease: {mine,
    holder}}`` (the daemon gives its ``LeaseService``); by default one on the state dir."""

    def __init__(self, engine: Any = None, *, leases: Any = None) -> None:
        self.engine = None if isinstance(engine, EventBus) else engine
        self.bus = _bus_of(engine)
        self.leases = leases

    @property
    def state_dir(self) -> Path:
        sd = getattr(self.engine, "state_dir", None)
        if sd is not None:
            return Path(sd)
        env = os.environ.get(STATE_DIR_ENV)
        return Path(env) if env else Path.home() / ".config" / "harness-manager"

    @staticmethod
    def adapter(session: Any) -> Any:
        claim = getattr(session, "claim", None)
        if claim is None:
            raise UnavailableError(CAPABILITY, NO_ADAPTER)
        return claim

    def check_claimable(self, session: Any) -> Any:
        """The adapter, or ``UnavailableError`` saying why this board has nothing to claim."""
        claim = self.adapter(session)
        why = claim.claimable() if hasattr(claim, "claimable") else ""
        if why:
            raise UnavailableError(CAPABILITY, why)
        return claim

    # -- reading ------------------------------------------------------------------------------

    def status(self, session: Any, *, refresh: bool = False) -> dict[str, Any] | None:
        """The claim as ``info`` shows it; None where there is nothing to claim (bare metal)."""
        claim = getattr(session, "claim", None)
        if claim is None:
            return None
        return claim.claim_status(refresh=refresh, hub_ok=refresh)

    # -- the lease ----------------------------------------------------------------------------

    def _lease_service(self) -> Any:
        if self.leases is None:
            from harness_manager.services.lease import LeaseService

            self.leases = LeaseService(self.state_dir, self.bus)
        return self.leases

    def check_lease(self, session: Any) -> str:
        """``""`` for a board with no hub; the holder when the lease is ours; else HeldError."""
        hub = getattr(session, "hub", None)
        if hub is None:
            return ""
        target = getattr(hub, "target", "") or "the board"
        leases = self._lease_service()
        forget = getattr(leases, "forget", None)
        if callable(forget):
            forget(hub)                       # a fresh answer: a lease taken a moment ago counts
        try:
            view = leases.view(hub)
        except HarnessError as exc:
            raise HeldError(f"cannot confirm you hold the lease on {target}: {exc.message}",
                            holder="unknown (the hub did not answer)",
                            hint="claiming is for the lease holder only; retry when the hub "
                                 "answers (`harness-manager lease show TARGET`)") from exc
        lease = (view or {}).get("lease")
        if not lease:
            raise HeldError(f"claiming is for the lease holder only, and nobody holds {target}",
                            holder="nobody",
                            hint="take the lease first: `harness-manager lease acquire TARGET`")
        if not lease.get("mine"):
            who = lease.get("holder") or "someone else"
            raise HeldError(f"claiming is for the lease holder only: {who} holds {target}",
                            holder=who,
                            hint="ask for the board: `harness-manager lease request TARGET`")
        return str(lease.get("holder") or "")

    # -- the claim ----------------------------------------------------------------------------

    def claim(self, session: Any, *, confirm: bool, key: str | None = None, adopt: bool = False,
              replace_host_key: bool = False,
              progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """TOFU-claim the board's SSH with your key and pin its host key (``adopt``: pin a
        claim you made elsewhere). Never automatic: ``confirm`` must be True."""
        claim = self.check_claimable(session)
        if not confirm:
            raise RefusedError("a claim needs a confirmation: it gives your key root on the "
                               "board and locks its slots to that key",
                               hint="CLI: answer the prompt or pass --yes; API: "
                                    "{\"confirm\": true}")
        self.check_lease(session)
        out = claim.claim(key=key, adopt=adopt, replace_host_key=replace_host_key,
                          progress=progress)
        self._publish(session, out)
        return out

    def refresh(self, session: Any) -> dict[str, Any] | None:
        out = self.status(session, refresh=True)
        if out is not None:
            self._publish(session, out)
        return out

    def ssh_argv(self, session: Any, command: Sequence[str] = (), *,
                 tty: bool = False) -> list[str]:
        """The pinned ``ssh [-J HUB] -l USER BOARD`` command line; nothing is run."""
        return self.adapter(session).ssh_argv(command, tty=tty)

    def _publish(self, session: Any, status: dict[str, Any]) -> None:
        if self.bus is None:
            return
        board_id = getattr(getattr(session, "candidate", None), "board_id", "")
        self.bus.publish(Event("board.claim", board_id, {
            k: status.get(k) for k in ("state", "claimed", "host_key", "route", "action")
            if k in status}))
