"""A fake ``ssh`` EXECUTABLE (lane L1), for testing the tunnel's real ``subprocess`` path.

A test writes a tiny ``ssh`` shim on PATH that runs this module. It answers
``ssh … -G HOST`` from ``$L1_FAKE_SSH_G`` (config evaluation), and otherwise
serves the ``-L`` forwards with ``FakeSsh`` against the routes in
``$L1_FAKE_SSH_ROUTES`` (JSON ``{"host:port": "127.0.0.1:port"}``), writing to
stderr what OpenSSH writes (a refused channel, a taken port), until SIGTERM.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading

from tests.fakes.l1_fake_ssh import FakeSsh


def main(argv: list[str]) -> int:
    if "-G" in argv:
        sys.stdout.write(os.environ.get("L1_FAKE_SSH_G", ""))
        return 0
    routes: dict[tuple[str, int], tuple[str, int]] = {}
    for remote, local in json.loads(os.environ.get("L1_FAKE_SSH_ROUTES", "{}")).items():
        rh, rp = remote.rsplit(":", 1)
        lh, lp = local.rsplit(":", 1)
        routes[(rh, int(rp))] = (lh, int(lp))
    fake = FakeSsh(routes, fail=os.environ.get("L1_FAKE_SSH_FAIL", ""))
    proc = fake(["ssh", *argv])
    if proc.returncode is not None:
        sys.stderr.write(proc.stderr_tail)
        return proc.returncode
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    written = 0
    while not stop.wait(0.02):
        tail = proc.stderr_tail
        if len(tail) > written:                         # stream new lines as ssh would
            sys.stderr.write(tail[written:])
            sys.stderr.flush()
            written = len(tail)
    proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
