"""The mint-4 virtual board serves the v0.20 verbs through pyverify's own models; the v2.1 and
v1 boards still decline them."""

from __future__ import annotations

import json
import socket

import pytest

from tests.fakes.virtual_board import LINUX_HARNESSD, LINUX_HARNESSD_V1, LINUX_MINT4, VirtualMps3

NEW_VERBS = [{"op": "hostio", "act": "get"}, {"op": "sensors", "act": "get"},
             {"op": "cosim", "act": "cycles"}, {"op": "mcc", "act": "status"}]


def _ask(vb, req):
    with socket.create_connection(("127.0.0.1", vb.shell.control_port), timeout=5) as c:
        c.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


@pytest.mark.parametrize("req", NEW_VERBS, ids=lambda r: r["op"])
def test_mint4_board_answers_the_new_verbs(tmp_path, req):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        reply = _ask(vb, req)
        assert reply.get("ok") is True, reply


def test_mint4_stats_has_sysmon_before_os_up_ms(tmp_path):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        reply = _ask(vb, {"op": "stats"})
        keys = list(reply)
        assert "sysmon" in reply
        assert keys.index("lcd_mirror") < keys.index("sysmon") < keys.index("os_up_ms")


@pytest.mark.parametrize("profile", [LINUX_HARNESSD, LINUX_HARNESSD_V1], ids=["v21", "v1"])
@pytest.mark.parametrize("req", NEW_VERBS, ids=lambda r: r["op"])
def test_older_boards_say_not_supported(tmp_path, profile, req):
    with VirtualMps3(tmp_path, profile) as vb:
        reply = _ask(vb, req)
        assert reply.get("ok") is False, reply
        assert "not supported" in str(reply.get("err", "")).lower() or "not_supported" in str(reply), reply
        if profile is LINUX_HARNESSD:
            assert "sysmon" not in _ask(vb, {"op": "stats"})
