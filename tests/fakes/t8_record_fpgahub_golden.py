"""Record fpgahub v0.3.0's REAL REST behaviour as a golden file for the T8 fake hub.

NOT a test (pytest never collects it). It runs fpgahub's own FastAPI app, in process, from
the v0.3.0 source, and drives the lease/share/whoami/groups routes Harness Manager's REST
hub client uses, as three real Bearer-token principals (write, read, admin). It writes
``t8_fpgahub_v030_golden.json`` beside itself:

- ``openapi``: the paths the client and the fake use, and every component schema they
  reference, from the app's own ``app.openapi()``;
- ``steps``: each request (principal, method, path, body) and the real status and body;
- ``events``: every event the daemon emitted, as the SSE ``data:`` payload
  (``Event.to_dict()``: ``{type, ts, data}``).

``tests/unit/test_t8_contract.py`` replays the same steps against the fake
(``t8_hub_rest.FakeFpgahub``) and compares status codes, key sets and value types, so the
fake cannot drift from the hub without a red test.

It touches nothing outside a temporary directory: no network, no nftables (the ethernet gate
is off in the config and ``nftables.reload`` is a no-op), no udev, no pings. Regenerate::

    python3.11 -m venv /tmp/fh && /tmp/fh/bin/pip install fastapi pydantic uvicorn httpx \\
        rich click tomli_w pyudev cryptography itsdangerous python-multipart jinja2 \\
        pyserial websockets numpy six python-pam
    cp -r ~/SoCLabs/fpgahub-v030-readonly/src /tmp/fh-src      # never build in the source
    PYTHONPATH=/tmp/fh-src /tmp/fh/bin/python tests/fakes/t8_record_fpgahub_golden.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

OUT = Path(__file__).with_name("t8_fpgahub_v030_golden.json")

HUB_HOSTNAME = "mapstone-dev"

CONFIG = """
[daemon]
role = "both"
reachability_interval_s = 0
lease_event_log = "@LOG@"

[[peers]]
name = "mapstone-dev"
addr = "mapstone-dev.example.org:7245"

[boards.mps3_01.targets.pl]
server = "mapstone-dev"
hub_path = "1-2.3.4.3"
description = "HBI0309C MPS3 #01 (lab)"
board_type = "mps3"
role = "pl"

[boards.mps3_01.targets.pl.naming]
tty_symlink_dir = "mps3_01_pl"
net_name = "mps3_01_pl"

[boards.mps3_01.targets.pl.network]
host_ip = "192.168.10.1/24"
board_ip = "192.168.10.101"
board_mac = "02:00:5e:00:03:03"
hostname = "mps3-01-pl"
dns_search = "fpga"

[boards.mps3_01.targets.pl.access]
lease_timeout_s = 3600
share_tty = false
share_port_base = 12000
gate_tty = false
gate_ethernet = false

[boards.kr260_01.targets.ps]
server = "mapstone-dev"
hub_path = "1-1.3"
description = "KR260 PS"

[boards.kr260_01.targets.ps.naming]
tty_symlink_dir = "kr260_01_ps"
net_name = "kr260_01_ps"

[boards.kr260_01.targets.ps.network]
host_ip = "192.168.20.1/24"
board_ip = "192.168.20.101"
board_mac = "02:00:5e:00:20:01"
hostname = "kr260-01-ps"

[boards.kr260_01.targets.pl]
server = "mapstone-dev"
hub_path = "1-1.4"
description = "KR260 PL"

[boards.kr260_01.targets.pl.naming]
tty_symlink_dir = "kr260_01_pl"
net_name = "kr260_01_pl"

[boards.kr260_01.targets.pl.network]
host_ip = "192.168.21.1/24"
board_ip = "192.168.21.101"
board_mac = "02:00:5e:00:21:01"
hostname = "kr260-01-pl"
"""

#: The paths (OpenAPI templates) Harness Manager's REST hub client and the fake use.
PATHS = (
    "/api/v1/health",
    "/api/v1/whoami",
    "/api/v1/groups",
    "/api/v1/targets/{name}",
    "/api/v1/targets/{name}/lease",
    "/api/v1/targets/{name}/lease/heartbeat",
    "/api/v1/targets/{name}/lease/wait",
    "/api/v1/targets/{name}/lease/history",
    "/api/v1/targets/{name}/queue",
    "/api/v1/targets/{name}/shares",
    "/api/v1/boards/{name}/lease",
    "/api/v1/boards/{name}/lease/revoke",
    "/api/v1/boards/{name}/lease/history",
    "/api/v1/boards/{name}/queue",
    "/api/v1/events",
)


def _guards() -> None:
    os.environ["FPGAHUB_DISABLE_REACHABILITY"] = "1"
    os.environ["FPGAHUB_DISABLE_USBIP_PROBE"] = "1"
    import pyudev
    from fpgahub import nftables

    def _absent(context, subsystem, sys_name):
        raise pyudev.DeviceNotFoundError(f"no device {sys_name}")

    class _Inert:
        def __init__(self, *a, **k):
            pass

        def filter_by(self, *a, **k):
            pass

        def filter_by_tag(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def send_stop(self):
            pass

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

        def poll(self, timeout=None):
            return None

        def __iter__(self):
            return iter(())

    pyudev.Devices.from_name = staticmethod(_absent)
    pyudev.Monitor.from_netlink = staticmethod(lambda context, source="udev": _Inert())
    pyudev.MonitorObserver = _Inert
    nftables.reload = lambda **kw: None


def _openapi_subset(app) -> dict:
    spec = app.openapi()
    paths = {p: spec["paths"][p] for p in PATHS if p in spec["paths"]}
    missing = [p for p in PATHS if p not in spec["paths"]]
    if missing:
        raise SystemExit(f"fpgahub has no route for {missing}")
    comps = spec.get("components", {}).get("schemas", {})
    keep: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in keep:
                    keep[name] = comps[name]
                    walk(comps[name])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(paths)
    return {"paths": paths, "components": {"schemas": dict(sorted(keep.items()))}}


def main() -> None:
    _guards()
    from fastapi.testclient import TestClient
    from fpgahub import __version__
    from fpgahub.config import Config
    from fpgahub.daemon import DaemonContext, create_app
    from fpgahub.pam_auth import FakeAuthenticator

    tmp = Path(tempfile.mkdtemp(prefix="t8-fpgahub-"))
    cfg_path = tmp / "config.toml"
    cfg_path.write_text(CONFIG.replace("@LOG@", str(tmp / "lease_events.jsonl")))
    ctx = DaemonContext(
        config=Config.load(cfg_path), config_path=cfg_path,
        state_path=tmp / "state.json", lease_state_path=tmp / "leases.json",
        tokens_path=tmp / "tokens.json", authenticator=FakeAuthenticator(users={}),
        session_secret="t8-golden", topology_fn=lambda: [],
        udev_rules_path=tmp / "udev.rules", udev_tul_rules_path=tmp / "udev-tul.rules",
        networkd_dir=tmp / "networkd", dnsmasq_dir=tmp / "dnsmasq",
        resolved_dropin_path=tmp / "resolved.conf", nftables_path=tmp / "fpgahub.nft",
        hostname=HUB_HOSTNAME, user="fpgahub",
    )
    tokens = {
        "alice": ctx.tokens.mint("alice-dev", role="write", owner="alice").token,
        "bob": ctx.tokens.mint("bob-dev", role="write", owner="bob").token,
        "carol": ctx.tokens.mint("carol-ro", role="read", owner="carol").token,
        "david": ctx.tokens.mint("david-admin", role="admin", owner="david").token,
    }
    events: list[dict] = []
    real_emit = ctx.emit

    def emit(event_type: str, **data):
        from fpgahub.events import Event

        events.append(json.loads(json.dumps(Event.now(event_type, **data).to_dict(),
                                            default=str)))
        real_emit(event_type, **data)

    ctx.emit = emit                                            # type: ignore[method-assign]
    app = create_app(ctx)
    steps: list[dict] = []
    held: dict[str, str] = {}

    with TestClient(app, base_url="https://testserver") as client:
        def call(name, who, method, path, body=None, params=None, token=None):
            headers = {}
            if who == "bogus":
                headers["Authorization"] = "Bearer not-a-token"
            elif who:
                headers["Authorization"] = f"Bearer {tokens[who]}"
            kw = {"headers": headers}
            if body is not None:
                kw["json"] = body
            if params is not None:
                kw["params"] = params
            r = client.request(method, "/api/v1" + path, **kw)
            try:
                out = r.json()
            except ValueError:
                out = r.text
            steps.append({"name": name, "who": who, "method": method, "path": path,
                          "params": params, "body": body, "status": r.status_code,
                          "response": out})
            return out

        call("health_anon", None, "GET", "/health")
        call("whoami_anon", None, "GET", "/whoami")
        call("whoami_bad_token", "bogus", "GET", "/whoami")
        call("whoami_write", "alice", "GET", "/whoami")
        call("whoami_admin", "david", "GET", "/whoami")
        call("groups", "alice", "GET", "/groups")
        call("target", "alice", "GET", "/targets/mps3_01_pl")
        call("target_404", "alice", "GET", "/targets/nope/lease")
        call("lease_free", "alice", "GET", "/targets/mps3_01_pl/lease")
        call("acquire_read_403", "carol", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 600, "tier": "interactive"})
        call("acquire_bad_ttl_422", "alice", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 0})
        g = call("acquire_granted", "alice", "POST", "/targets/mps3_01_pl/lease",
                 {"ttl_seconds": 600, "tier": "interactive"})
        held["alice"] = g["token"]
        call("acquire_again_refreshes", "alice", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 600, "tier": "interactive"})
        call("acquire_queued", "bob", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 600, "tier": "interactive"})
        call("acquire_queued_again", "bob", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 600, "tier": "interactive"})
        call("lease_held_queue", "carol", "GET", "/targets/mps3_01_pl/lease")
        call("board_lease", "carol", "GET", "/boards/mps3_01/lease")
        call("heartbeat", "alice", "POST", "/targets/mps3_01_pl/lease/heartbeat",
             {"token": held["alice"]})
        call("heartbeat_not_holder_403", "bob", "POST", "/targets/mps3_01_pl/lease/heartbeat",
             {"token": held["alice"]})
        call("wait_timeout_408", "bob", "GET", "/targets/mps3_01_pl/lease/wait",
             params={"timeout": "0.2"})
        call("revoke_write_403", "bob", "POST", "/boards/mps3_01/lease/revoke",
             params={"reason": "force-released by bob via Harness Manager"})
        call("revoke_admin", "david", "POST", "/boards/mps3_01/lease/revoke",
             params={"reason": "force-released by david@mapstone-dev via Harness Manager"})
        call("lease_after_revoke", "carol", "GET", "/targets/mps3_01_pl/lease")
        w = call("wait_granted", "bob", "GET", "/targets/mps3_01_pl/lease/wait",
                 params={"timeout": "1"})
        held["bob"] = w["token"]
        call("heartbeat_after_revoke_403", "alice", "POST",
             "/targets/mps3_01_pl/lease/heartbeat", {"token": held["alice"]})
        call("history", "carol", "GET", "/targets/mps3_01_pl/lease/history",
             params={"limit": 50})
        call("board_history", "carol", "GET", "/boards/mps3_01/lease/history",
             params={"limit": 50})
        call("release", "bob", "DELETE", "/targets/mps3_01_pl/lease", {"token": held["bob"]})
        call("release_nothing", "bob", "DELETE", "/targets/mps3_01_pl/lease",
             {"token": held["bob"]})
        call("heartbeat_no_lease_403", "bob", "POST", "/targets/mps3_01_pl/lease/heartbeat",
             {"token": held["bob"]})
        call("acquire_again", "alice", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 600, "tier": "interactive"})
        call("queue_again", "bob", "POST", "/targets/mps3_01_pl/lease",
             {"ttl_seconds": 600, "tier": "interactive"})
        call("cancel_queue", "bob", "DELETE", "/targets/mps3_01_pl/queue", {})
        call("cancel_queue_nothing", "bob", "DELETE", "/targets/mps3_01_pl/queue", {})
        call("cancel_multi_target_409", "bob", "DELETE", "/targets/kr260_01_pl/queue", {})
        call("cancel_board_queue", "bob", "DELETE", "/boards/kr260_01/queue", {})
        call("shares_empty", "alice", "GET", "/targets/mps3_01_pl/shares")
        call("share_start_extra_key_422", "alice", "POST", "/targets/mps3_01_pl/shares",
             {"tty_paths": ["/dev/mps3_01_pl/tty_00"], "baud": 115200, "holder": "x"})
        call("share_start_not_holder_409", "bob", "POST", "/targets/mps3_01_pl/shares",
             {"tty_paths": ["/dev/mps3_01_pl/tty_00"], "baud": 115200})

    golden = {
        "fpgahub_version": __version__,
        "source": "~/SoCLabs/fpgahub-v030-readonly (22aa362 build: version 0.3.0)",
        "hub_hostname": HUB_HOSTNAME,
        "sse_framing": {"connected": ":connected\n\n",
                        "event": "event: {type}\ndata: {json of {type, ts, data}}\n\n"},
        "openapi": _openapi_subset(app),
        "steps": steps,
        "events": events,
    }
    OUT.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(steps)} steps, {len(events)} events)", file=sys.stderr)


if __name__ == "__main__":
    main()
