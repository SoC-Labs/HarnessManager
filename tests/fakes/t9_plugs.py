"""Team T9 test doubles: fake metered plugs/PDUs on 127.0.0.1, and a shared fake clock.

Each fake models the device's documented HTTP API (see socharness/power/plugs.py for
the sources) closely enough to exercise the real drivers over real sockets:

- ``FakeShelly``: Gen2 RPC ``Switch.GetStatus``/``Switch.Set`` (``toggle_after``),
  HTTP Digest SHA-256 when a password is set (verified here independently of the
  client code, per RFC 7616).
- ``FakeTasmota``: ``/cm?cmnd=``: ``Status 8``, ``Power<n>``, ``Backlog ...; Delay ...``;
  the password in the query; a wrong one answers the ``WARNING`` reply or HTTP 401.
- ``FakeNetio``: ``GET``/``POST /netio.json`` with HTTP Basic; action 2 "short off".

Device-side timers run on the injected clock, so a power cycle takes no real time
when the driver sleeps on the same ``FakeClock``. ``hang = True`` makes the device
accept the connection and never answer (until ``close``), for timeout tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


Reply = tuple[int, dict[str, str], bytes]


def _json(obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(obj).encode()


class _FakeDevice:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or FakeClock()
        self.hang = False
        self.garbage = False
        self.requests: list[dict[str, Any]] = []
        self._release = threading.Event()
        self._lock = threading.Lock()
        device = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
                device.requests.append({"method": method, "path": parts.path, "query": query,
                                        "raw_path": self.path, "headers": dict(self.headers),
                                        "body": body})
                if device.hang:
                    device._release.wait(10)
                    return
                if device.garbage:
                    status, headers, data = 200, {"Content-Type": "text/html"}, b"<html>not json</html>"
                else:
                    with device._lock:
                        device._tick()
                        status, headers, data = device.handle(method, parts.path, query,
                                                              dict(self.headers), body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:   # noqa: N802
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _tick(self) -> None:
        """Apply device-side timers that are due (on the injected clock)."""

    def handle(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
               body: bytes) -> Reply:
        raise NotImplementedError

    def start(self) -> _FakeDevice:
        self._thread.start()
        return self

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- Shelly Gen2 --------------------------------------------------------------------------

_DIGEST_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


class FakeShelly(_FakeDevice):
    REALM = "shellyplus1pm-fake0001"
    NONCE = "1700000000"

    def __init__(self, *, password: str | None = None, switches: int = 1, **kw: Any) -> None:
        super().__init__(**kw)
        self.password = password
        self.switches = switches
        self.output = True
        self.apower: float | None = 11.4
        self.voltage: float | None = 239.1
        self.current: float | None = 0.071
        self.toggle_at: float | None = None
        self.ignore_set = False          # a device that answers Switch.Set but never switches
        self.sets: list[dict[str, str]] = []

    def _tick(self) -> None:
        if self.toggle_at is not None and self.clock() >= self.toggle_at:
            self.output = not self.output
            self.toggle_at = None

    def _authorised(self, method: str, raw_uri: str, headers: dict[str, str]) -> bool:
        if not self.password:
            return True
        auth = headers.get("Authorization", "")
        if not auth.startswith("Digest "):
            return False
        p = {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
             for m in _DIGEST_PARAM.finditer(auth[7:])}

        def h(s: str) -> str:
            return hashlib.sha256(s.encode()).hexdigest()

        if p.get("algorithm") != "SHA-256" or p.get("realm") != self.REALM or p.get("uri") != raw_uri:
            return False
        ha1 = h(f"{p.get('username')}:{self.REALM}:{self.password}")
        ha2 = h(f"{method}:{raw_uri}")
        want = h(f"{ha1}:{p.get('nonce')}:{p.get('nc')}:{p.get('cnonce')}:{p.get('qop')}:{ha2}")
        return p.get("username") == "admin" and p.get("response") == want

    def handle(self, method, path, query, headers, body):
        raw_uri = self.requests[-1]["raw_path"]
        if not self._authorised(method, raw_uri, headers):
            challenge = (f'Digest qop="auth", realm="{self.REALM}", nonce="{self.NONCE}", '
                         f"algorithm=SHA-256")
            return _json({"code": 401, "message": "Unauthorized"}, 401,
                         {"WWW-Authenticate": challenge})
        sid = query.get("id", "")
        if not sid.isdigit() or int(sid) >= self.switches:
            return _json({"code": -105, "message": f"Argument 'id', value {sid} not found!"}, 400)
        if path == "/rpc/Switch.GetStatus":
            st: dict[str, Any] = {"id": int(sid), "source": "init", "output": self.output,
                                  "freq": 50.0, "temperature": {"tC": 41.2, "tF": 106.2}}
            for key in ("apower", "voltage", "current"):
                value = getattr(self, key)
                if value is not None:
                    st[key] = value if self.output or key == "voltage" else 0.0
            return _json(st)
        if path == "/rpc/Switch.Set":
            self.sets.append(dict(query))
            was_on = self.output
            if not self.ignore_set:
                self.output = query.get("on") == "true"
                if "toggle_after" in query:
                    self.toggle_at = self.clock() + float(query["toggle_after"])
            return _json({"was_on": was_on})
        return _json({"code": 404, "message": "No handler for " + path}, 404)


# --- Tasmota -----------------------------------------------------------------------------


class FakeTasmota(_FakeDevice):
    def __init__(self, *, password: str | None = None, auth_style: str = "warning",
                 relays: int = 1, metering: bool = True, **kw: Any) -> None:
        super().__init__(**kw)
        self.password = password
        self.auth_style = auth_style      # "warning" (HTTP 200 + WARNING) or "401"
        self.relays = relays
        self.metering = metering
        self.power_on = [True] * relays
        self.power: float | list[float] = 9.0 if relays == 1 else [9.0] * relays
        self.voltage: float | list[float] = 238.0
        self.current: float | list[float] = 0.061
        self.pending: list[tuple[float, int, bool]] = []    # (due, relay, on)
        self.commands: list[str] = []

    def _tick(self) -> None:
        now = self.clock()
        due = [p for p in self.pending if p[0] <= now]
        self.pending = [p for p in self.pending if p[0] > now]
        for _t, relay, on in sorted(due):
            self.power_on[relay - 1] = on

    def _power_reply(self, n: int) -> dict[str, str]:
        state = "ON" if self.power_on[n - 1] else "OFF"
        return {"POWER": state} if self.relays == 1 else {f"POWER{n}": state}

    def _run(self, cmnd: str, at: float) -> Any:
        m = re.fullmatch(r"Power(\d*)(?:\s+(On|Off))?", cmnd, re.I)
        if m:
            n = int(m.group(1) or 1)
            if not 1 <= n <= self.relays:
                return {"Command": "Unknown"}
            if m.group(2):
                on = m.group(2).lower() == "on"
                if at <= self.clock():
                    self.power_on[n - 1] = on
                else:
                    self.pending.append((at, n, on))
            return self._power_reply(n)
        if cmnd == "Status 8":
            sns: dict[str, Any] = {"Time": "2026-09-23T12:00:00"}
            if self.metering:
                sns["ENERGY"] = {"TotalStartTime": "2026-01-01T00:00:00", "Total": 12.3,
                                 "Yesterday": 0.2, "Today": 0.1, "Power": self.power,
                                 "ApparentPower": 10, "ReactivePower": 4, "Factor": 0.9,
                                 "Voltage": self.voltage, "Current": self.current}
            return {"StatusSNS": sns}
        return {"Command": "Unknown"}

    def handle(self, method, path, query, headers, body):
        if path != "/cm":
            return 404, {}, b"not found"
        if self.password and (query.get("user") != "admin" or query.get("password") != self.password):
            if self.auth_style == "401":
                return 401, {}, b""
            return _json({"WARNING": "Need user=<username>&password=<password>"})
        cmnd = unquote(query.get("cmnd", ""))
        self.commands.append(cmnd)
        if cmnd.lower().startswith("backlog "):
            at = self.clock()
            for part in (p.strip() for p in cmnd[8:].split(";")):
                d = re.fullmatch(r"Delay\s+(\d+)", part, re.I)
                if d:
                    at += int(d.group(1)) / 10.0
                elif part:
                    self._run(part, at)
            return _json({"WARNING": "Enable weblog 2 if response expected"})
        return _json(self._run(cmnd, self.clock()))


# --- NETIO -------------------------------------------------------------------------------


class FakeNetio(_FakeDevice):
    def __init__(self, *, password: str | None = None, outputs: int = 4, **kw: Any) -> None:
        super().__init__(**kw)
        self.password = password
        self.voltage = 231.4
        self.outputs = [{"ID": i, "Name": f"output_{i}", "State": 1, "Action": 6, "Delay": 5000,
                         "Current": 150 * i, "PowerFactor": 0.81, "Phase": 0, "Energy": 1000 * i,
                         "ReverseEnergy": 0, "Load": 30 * i} for i in range(1, outputs + 1)]
        self.pending: list[tuple[float, int]] = []    # (due, output ID) to switch back on
        self.posts: list[Any] = []

    def _tick(self) -> None:
        now = self.clock()
        for _due, oid in [p for p in self.pending if p[0] <= now]:
            self._out(oid)["State"] = 1
        self.pending = [p for p in self.pending if p[0] > now]

    def _out(self, oid: int) -> dict[str, Any]:
        return next(o for o in self.outputs if o["ID"] == oid)

    def _status(self) -> Reply:
        return _json({"Agent": {"Model": "PowerPDU 4C", "Version": "3.4.0", "NumOutputs": len(self.outputs)},
                      "GlobalMeasure": {"Voltage": self.voltage, "Frequency": 50.0,
                                        "TotalLoad": sum(o["Load"] for o in self.outputs)},
                      "Outputs": self.outputs})

    def handle(self, method, path, query, headers, body):
        if path != "/netio.json":
            return 404, {}, b""
        if self.password:
            want = "Basic " + base64.b64encode(f"admin:{self.password}".encode()).decode()
            if headers.get("Authorization") != want:
                return 401, {"WWW-Authenticate": 'Basic realm="NETIO"'}, b""
        if method == "POST":
            req = json.loads(body.decode())
            self.posts.append(req)
            for o in req.get("Outputs", []):
                out = self._out(o["ID"])
                if o.get("Action") == 2:           # short off
                    out["State"] = 0
                    self.pending.append((self.clock() + o.get("Delay", out["Delay"]) / 1000.0, o["ID"]))
                elif o.get("Action") in (0, 1):
                    out["State"] = o["Action"]
        return self._status()
