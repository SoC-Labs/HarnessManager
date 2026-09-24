"""Lane XVC-UI: the XVC card over the REAL daemon (``xvc_api.py``), the real Engine and MPS3
pack and the real ``XvcService``, clicks only.

The board is a ``VirtualMps3`` on the ILA image (firmware v0.11, ``xvc_dbgbr``); its XVC
server is the fake one on 127.0.0.1 (``HARNESS_MANAGER_MPS3_XVC_PORT``), and Harness
Manager's hw_server is the fake one (``tests/fakes/fake_hw_server.py``, through a shim where
a Vivado 2024.1 install keeps it). Vivado attaching is a TCP client on hw_server's port, as
in the service's unit tests; a swap is the engine's ``deploy.started``/``deploy.done``.
Loopback only: the page's own first scan never runs (the board is open before the page
loads, and ``POST /probe`` is answered here with no candidates). Each check has its twin.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import pytest

from harness_manager.core.events import Event
from harness_manager.services import xvc as X
from harness_manager_mps3 import xvc as MX
from tests.fakes.t2_overlays import FIELDED_USERCODE, make_overlay, use_overlay_dirs
from tests.fakes.t13_daemon import engine_for
from tests.fakes.t14_mock_api import real_daemon
from tests.fakes.virtual_board import FIELDED_3F1A560F, FIELDED_ILA_V011, VirtualMps3
from tests.fakes.xvc_server import FakeXvcServer
from tests.integration.test_xvc_mps3 import NANOSOC_ILA, with_ltx
from tests.unit.test_xvc_service import (
    hw_shim,  # noqa: F401 - the fake hw_server, a fixture
    wait_for,
)
from tests.web.conftest import dump_failed_pages
from tests.web.test_xvc_card_browser import APP, SCOPE, T, button, by_id, state_is, to_debug

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(os.name != "posix", reason="a /bin/sh shim stands in for hw_server")]
TOKEN = "xvc-ui-real"


@pytest.fixture
def fake(monkeypatch):
    with FakeXvcServer() as srv:
        monkeypatch.setenv(MX.XVC_PORT_ENV, str(srv.port))
        yield srv


@pytest.fixture
def ila_overlay(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "ov"
    ila = make_overlay(root, "nanosoc_ila", rm_id=NANOSOC_ILA,
                       static_id=FIELDED_ILA_V011.static_id, static_usercode=FIELDED_USERCODE)
    ltx = with_ltx(ila, "nanosoc_ila", '{"probes": ["ila_0"]}')
    use_overlay_dirs(monkeypatch, root)
    return ltx


@pytest.fixture
def served(tmp_path, fake, ila_overlay, hw_shim):  # noqa: F811 - the imported fixture
    """The real daemon over a VirtualMps3 whose board is open already: (server, board_id)."""
    def start(identity=FIELDED_ILA_V011, boot=NANOSOC_ILA):
        vb = VirtualMps3(tmp_path / "vb", identity, **({"boot_rm_id": boot} if boot else {}))
        vb.__enter__()
        eng = engine_for(vb)
        server = real_daemon(eng, token=TOKEN, state_dir=tmp_path / "daemon")
        server.__enter__()
        cleanup.append((vb, eng, server))
        with httpx.Client(base_url=server.url, headers={"Authorization": f"Bearer {TOKEN}"},
                          trust_env=False, timeout=30.0) as api:
            r = api.post("/api/v1/boards", json={"target": vb.shell_endpoint, "note": "xvc-ui"})
            assert r.status_code == 200, r.text
            return server, r.json()["board_id"]

    cleanup: list = []
    yield start
    for vb, eng, server in reversed(cleanup):
        server.__exit__(None, None, None)
        eng.close_all()
        vb.__exit__(None, None, None)


@pytest.fixture
def page_on(browser, request):
    contexts, pages = [], []

    def make(server, bid, scheme="light"):
        ctx = browser.new_context(viewport=APP, color_scheme=scheme, reduced_motion="reduce")
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        # Loopback only: a scan would try the MPS3's fixed address and broadcast identify.
        page.route("**/api/v1/probe", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "candidates": []})))
        page.goto(server.ui_url)
        pages.append(page)
        page.locator(f'.board-item[data-board="{bid}"]').click()
        page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
        to_debug(page)
        return page

    yield make
    dump_failed_pages(request, pages)
    for ctx in contexts:
        ctx.close()


def test_real_daemon_open_attach_swap_close(served, page_on, fake):
    server, bid = served()
    page = page_on(server, bid)
    svc = server.engine.xvc
    expect(page.locator('[data-testid="xvc-card"] .card-sub')).to_have_text(SCOPE)
    state_is(page, "down")
    expect(by_id(page, "xvc-unauth")).to_contain_text("unauthenticated")      # bare metal (X6)
    expect(by_id(page, "xvc-ltx")).to_contain_text("nanosoc_ila.ltx", timeout=T)
    button(page, "xvc_open").click()
    state_is(page, "ready", timeout=30_000)
    session = server.engine.session(bid)
    st = svc.status(session)
    assert st.mode == "m1" and st.hw_server_pid > 0
    expect(by_id(page, "xvc-url")).to_contain_text(f"localhost:{st.hw_server_port}")
    expect(by_id(page, "xvc-ltx")).to_contain_text("nanosoc_ila.ltx")
    # Vivado connects to HM's hw_server, which opens the XVC target through the relay.
    with socket.create_connection(("127.0.0.1", st.hw_server_port), timeout=5):
        state_is(page, "attached")
        expect(by_id(page, "xvc-attached")).to_contain_text(f"pid {st.hw_server_pid}")
        expect(by_id(page, "xvc-attached")).to_contain_text("Harness Manager's hw_server")
    # A partition swap: the slot and hw_server drop, then a fresh hw_server re-attaches.
    server.engine.bus.publish(Event("deploy.started", bid, {"overlay": "nanosoc_ila"}))
    state_is(page, "swapping")
    expect(by_id(page, "xvc-swapping")).to_contain_text("Swapping...")
    server.engine.bus.publish(Event("deploy.done", bid, {"verified": True,
                                                         "rm_id": f"0x{NANOSOC_ILA:08X}"}))
    state_is(page, "ready", timeout=30_000)
    expect(by_id(page, "xvc-reattached")).to_contain_text("Re-attached on nanosoc_ila")
    new = svc.status(session)
    assert new.hw_server_pid not in (0, st.hw_server_pid) and new.hw_server_port == st.hw_server_port
    button(page, "xvc_close").click()
    state_is(page, "down")
    wait_for(lambda: not fake.attached, what="the board's slot to free")
    wait_for(lambda: not X._pid_alive(new.hw_server_pid), what="HM's hw_server to exit")
    assert page.errors == []


def test_negative_twin_real_daemon_a_harness_without_xvc_dbgbr_cannot_open(served, page_on,
                                                                           fake):
    server, bid = served(FIELDED_3F1A560F, boot=None)
    page = page_on(server, bid)
    state_is(page, "down")
    expect(by_id(page, "reason-xvc_open")).to_contain_text("Cannot: needs harness firmware")
    expect(by_id(page, "reason-xvc_open")).to_contain_text("xvc_dbgbr")
    button(page, "xvc_open").click(force=True)
    expect(by_id(page, "xvc-result")).to_contain_text("Nothing was run.")
    assert fake.stats.connections == 0
    assert page.errors == []
