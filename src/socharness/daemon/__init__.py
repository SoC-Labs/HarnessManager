"""``socharnessd``: the per-user local engine service (docs/API.md).

One process per user owns the engine, so the CLI, the web UI and long-lived
sessions (debug, consoles) share one engine and one board session. A board's
lock belongs to one process and each board port accepts one client; this
process is that one.

Modules:

- ``state``: ``<state_dir>/daemon.json`` and the single-instance lock (stdlib only);
- ``control``: start (detached), stop and status, for the CLI verbs (stdlib only);
- ``server``: binds, writes ``daemon.json`` and runs uvicorn (``python -m socharness.daemon``);
- ``app``: the FastAPI application (every endpoint in docs/API.md);
- ``jobs``: long operations on worker threads, and the per-board operation gate;
- ``outbox``: bounded drop-oldest queues between engine threads and WebSockets;
- ``lab``: the lab verbs, run through the CLI's own verb code.

Importing this package imports nothing heavy: ``state`` and ``control`` stay
usable by the CLI without FastAPI or uvicorn being imported.
"""
