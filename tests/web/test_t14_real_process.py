"""The web UI against the real daemon PROCESS: ``python -m harness_manager.daemon`` (Team T13),
the real Engine and MPS3 pack, and a VirtualMps3 on loopback.

This is the install path david's users take: the daemon finds the UI in the
``harness_manager.web`` package data by itself, writes ``daemon.json`` with a fresh token,
and the page, opened at ``/#token=...``, adds the board by address and opens it.

Loopback only. The page's automatic first scan (``POST /probe`` with no hosts would
try the MPS3's fixed address and broadcast identify) is answered by the test with an
empty list; the probe the page makes for the added address goes to the daemon.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from harness_manager.daemon.state import read_info
from tests.fakes.virtual_board import FIELDED_3F1A560F, VirtualMps3

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000


@pytest.fixture
def daemon_process(tmp_path):
    procs = []

    def start(vb: VirtualMps3):
        sdir = tmp_path / "harness-manager-daemon"
        overrides = {"mps3": {"console_ports": vb.console_ports,
                              "push_port": vb.shell.raw_tcp_port, "tftp_port": vb.shell.tftp_port}}
        env = dict(os.environ, HARNESS_MANAGER_STATE_DIR=str(sdir))
        proc = subprocess.Popen(
            [sys.executable, "-m", "harness_manager.daemon", "--state-dir", str(sdir), "--port", "0",
             "--log-level", "warning", "--pack-overrides", json.dumps(overrides)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        procs.append(proc)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = read_info(sdir)
            if info is not None and info.pid == proc.pid:
                return info
            if proc.poll() is not None:
                raise AssertionError(f"harness-manager-daemon exited: {proc.stdout.read().decode()[-2000:]}")
            time.sleep(0.05)
        raise AssertionError("harness-manager-daemon did not write daemon.json")

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def only_explicit_probes(route, request):
    body = request.post_data or ""
    if '"hosts"' in body:
        route.continue_()
    else:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "candidates": []}))


def test_the_daemon_process_serves_the_ui_and_opens_a_board_added_by_address(
        browser, daemon_process, tmp_path, screenshots):
    with VirtualMps3(tmp_path / "vb", FIELDED_3F1A560F) as vb:
        info = daemon_process(vb)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
        try:
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/api/v1/probe", only_explicit_probes)
            page.goto(f"{info.base_url}/#token={info.token}")
            expect(page.locator('[data-testid="daemon-line"]')).to_contain_text(
                f"harness-manager-daemon {info.version}", timeout=T)
            assert "token" not in page.url
            page.locator('button[aria-label="Add a board by address"]').click()
            page.locator('input[aria-label="Board address"]').fill(vb.shell_endpoint)
            page.locator('.rail-add button[type="submit"]').click()
            board_id = f"mps3@{vb.shell_endpoint}"
            page.locator(f'.board-item[data-board="{board_id}"]').click(timeout=T)
            page.locator('[data-action="open"]').click()
            expect(page.locator('[data-testid="fact-shell"]')).to_contain_text("0x3F1A560F", timeout=T)
            # The fielded mint cannot read USR_ACCESS: UNCHECKED, shown as its own warning.
            expect(page.locator('[data-testid="build-chip"]')).to_have_attribute("data-level", "unk")
            expect(page.locator('[data-testid="fact-harness"]')).to_contain_text("bare-metal")
            page.screenshot(path=str(screenshots / "process-overview-light.png"))
            page.locator('.board-header button:has-text("Close board")').click()
            expect(page.locator('[data-action="open"]')).to_be_visible(timeout=T)
            assert errors == []
        finally:
            ctx.close()
