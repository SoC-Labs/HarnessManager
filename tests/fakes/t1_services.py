"""Team T1: stand-ins for lazily resolved services (deploy/consoles/debug).

The engine constructs a service as ``Cls(engine)``. These classes let the
tests prove that path without depending on Team T2/T4 modules.
"""

from __future__ import annotations

from typing import Any


class RecordingService:
    """Remembers the engine it was built with and what it was asked to release."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.closed_boards: list[str] = []
        self.downed: list[Any] = []

    def close_all(self, board_id: str) -> None:
        self.closed_boards.append(board_id)

    def down(self, session: Any) -> str:
        self.downed.append(session)
        return "down"


class FailingReleaseService(RecordingService):
    """Raises while the engine asks it to let go of a board."""

    def close_all(self, board_id: str) -> None:
        raise RuntimeError("console broker exploded")

    def down(self, session: Any) -> str:
        raise RuntimeError("debug service exploded")


class ExplodingService:
    def __init__(self, engine: Any) -> None:
        raise RuntimeError("constructor exploded")
