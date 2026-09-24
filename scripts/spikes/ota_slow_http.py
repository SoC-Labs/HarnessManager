"""Spike (lane OTA): an HTTP server that answers 404 after DELAY seconds.

    python ota_slow_http.py PORT DELAY

Pointing ``POST /api/v1/update/check`` at it keeps an ``update_check`` job running in
the daemon for DELAY seconds: a board-free stand-in for a long deploy or swap, to show
that ``POST /update/app`` is refused while any job runs.
"""

import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(sys.argv[2])


class Slow(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(DELAY)
        self.send_error(404, "ota spike: slow and empty")

    def log_message(self, fmt, *args):
        sys.stderr.write("slow-http: " + fmt % args + "\n")


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Slow).serve_forever()
