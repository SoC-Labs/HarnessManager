"""The Python client of socharnessd (docs/API.md "The Python client").

``RemoteEngine`` implements ``socharness.core.services.Engine`` over the
daemon's HTTP/WebSocket API, so a front-end written against the engine
protocol runs unchanged against the daemon::

    eng = RemoteEngine.discover()          # None when no daemon runs for this state dir
    if eng is not None:
        with eng:
            session = eng.open(eng.candidate_for("192.168.10.101"))
            print(eng.info(session.candidate.board_id).identity.rm_name)
"""

from .codec import error_from_json, from_json
from .remote import RemoteEngine, RemoteSession

__all__ = ["RemoteEngine", "RemoteSession", "error_from_json", "from_json"]
