"""A harness older than net-protocol v0.8 has no `version` (and maybe no `diag`) verb.

The July Linux image (v0.7 daemons), which boots at the B0 window, is exactly
this. Its reply is {"ok":false,"err":"unknown op"}. `harness-manager info` must
treat that as a legitimate older harness, never as an error
(from the MicroBlaze agent, 2026-09-23).
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading

from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode


class _V07Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            op = json.loads(raw).get("op")
            if op == "ping":
                reply = {"ok": True, "shell_id": "0x2b082e1b", "rm_id": "0x00000000"}
            else:
                reply = {"ok": False, "err": "unknown op"}
            self.wfile.write((json.dumps(reply) + "\n").encode())


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def test_info_on_a_harness_without_version_or_diag(capsys):
    srv = _Server(("127.0.0.1", 0), _V07Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = main(["--json", "info", f"127.0.0.1:{srv.server_address[1]}"])
        out = json.loads(capsys.readouterr().out)
        assert rc == ExitCode.OK
        ident = out["identity"]
        assert ident["shell_id"] == "0x2b082e1b"
        assert ident["harness_version"] == "" and ident["features"] == []
        assert ident["build_check"] == "unchecked"          # nothing to compare, not a pass
        assert out["health"]["reachable"] and any("diag" in n for n in out["health"]["notes"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_negative_twin_a_dead_port_is_still_unreachable(capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert main(["info", f"127.0.0.1:{port}"]) == ExitCode.UNREACHABLE
