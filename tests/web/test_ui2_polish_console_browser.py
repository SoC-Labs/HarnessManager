"""UI2-POLISH item 2 (david): a real terminal in the Workbench console card.

"The console inputs don't seem to accept command string i.e. ctrl-], new lines by themselves
natively like you can do from screen or from the python app and it would be good to show the
cursor in the console." Now (JS/consoles.js, sections/consoles.js):

- the terminal is the keyboard: every key xterm gives goes to the board raw (Ctrl-], Ctrl-C, Esc,
  a bare Enter = the line ending picked in CR/LF/CRLF), a paste in one message;
- the Keys menu sends what a browser keeps for itself (Ctrl-W, Ctrl-T, Ctrl-N: "Ctrl-..." takes any
  letter) and the usual controls;
- the send line is a line mode: line + ending, an empty line the ending alone, ^X and \\xNN expanded;
- the cursor blinks (a block in --term-cursor; an outline unfocused) and is hidden on a read-only
  console, which sends nothing.

Every check reads the bytes that reached the console (the demo engine's ``consoles.writes``, or the
virtual board's echo), never the page's own idea of what it sent. Each has its negative twin. The
REAL daemon over ``DemoEngine(showcase=True)``; the pacing check runs the real Engine and MPS3 pack
over ``VirtualMps3`` (its uart0 is paced 20 ms a byte by the daemon, FakeShell echoes it).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_V011
from tests.web import nav
from tests.web.test_demo_all_browser import showcase  # noqa: F401 - the fixture

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}

# Every console WebSocket message the page sends, as bytes (installed before the app loads).
WS_SPY = """
window.__wsSent = [];
const _send = WebSocket.prototype.send;
WebSocket.prototype.send = function (data) {
  try {
    if (String(this.url).includes('/consoles/')) {
      const u8 = data instanceof ArrayBuffer ? new Uint8Array(data)
        : ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
        : new TextEncoder().encode(String(data));
      window.__wsSent.push(Array.from(u8));
    }
  } catch (e) { /* the spy never breaks a send */ }
  return _send.call(this, data);
};
"""

PASTE = """(text) => {
  const ta = document.querySelector('[data-testid="terminal"] textarea.xterm-helper-textarea');
  const dt = new DataTransfer();
  dt.setData('text/plain', text);
  ta.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
}"""


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def wait_until(fn, timeout: float = 10.0, page: Any = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        if page is not None:
            page.wait_for_timeout(50)
        else:
            time.sleep(0.05)
    return bool(fn())


def spy_page(show: Any, scheme: str = "light") -> Any:
    ctx = show.browser.new_context(viewport=APP, color_scheme=scheme, reduced_motion="reduce")
    show.contexts.append(ctx)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.add_init_script(WS_SPY)
    page.goto(show.daemon.ui_url)
    page.wait_for_selector(".board-item", timeout=T)
    show.pages.append(page)
    return page


def console_page(show: Any, bid: str = BOARD_LINUX, scheme: str = "light") -> Any:
    page = spy_page(show, scheme)
    nav.open_board(page, bid)
    nav.tab(page, "workbench")
    page.wait_for_selector('[data-testid="console-state"]:has-text("up")', timeout=T)
    return page


def written(engine: Any, bid: str, since: int, name: str = "uart0") -> bytes:
    return b"".join(d for b, n, d in engine.consoles.writes[since:] if b == bid and n == name)


def focus_terminal(page: Any) -> None:
    by_id(page, "terminal").click()
    expect(page.locator('[data-testid="terminal"] textarea.xterm-helper-textarea')).to_be_focused(timeout=T)


def ending(page: Any, value: str) -> None:
    by_id(page, "send-ending").select_option(value)


def cursor_classes(page: Any) -> list[str]:
    return page.evaluate("""() => [...document.querySelectorAll('[data-testid="terminal"] .xterm-cursor')]
      .map((e) => e.className)""")


# --- 1. Ctrl-] in the terminal ------------------------------------------------------------------------


def test_ctrl_bracket_in_the_terminal_sends_0x1d_and_esc_stays_off_the_page(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    nav.activity(page)                                   # the drawer is open: Esc would close it
    ta = page.locator('[data-testid="terminal"] textarea.xterm-helper-textarea')
    ta.focus()                                           # (the drawer covers the console's right side)
    expect(ta).to_be_focused()
    n = len(eng.consoles.writes)
    page.keyboard.press("Control+BracketRight")
    page.keyboard.press("Escape")
    page.keyboard.press("Control+c")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("Tab")
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == b"\x1d\x1b\x03\x1b[A\t", page=page), \
        written(eng, BOARD_LINUX, n)
    expect(by_id(page, "activity-drawer")).to_have_count(1)     # Esc went to the board, not the page
    expect(by_id(page, "term-hint")).to_contain_text("Typing goes to the board")
    assert page.errors == []


def test_twin_keys_typed_outside_the_terminal_never_reach_the_board(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    focus_terminal(page)
    page.locator('[data-testid="console-uart0"] .cs-now').click()          # the focus leaves it
    expect(by_id(page, "term-hint")).to_contain_text("Click the console to type")
    n = len(eng.consoles.writes)
    page.keyboard.press("Control+BracketRight")
    page.keyboard.type("ls")
    page.wait_for_timeout(400)
    assert written(eng, BOARD_LINUX, n) == b""


# --- 2. a bare Enter --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("choice", "want"), [("LF", b"\n"), ("CR", b"\r"), ("CRLF", b"\r\n")])
def test_a_bare_enter_sends_the_chosen_line_ending(showcase, choice, want):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    ending(page, choice)
    focus_terminal(page)
    n = len(eng.consoles.writes)
    page.keyboard.press("Enter")
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == want, page=page), written(eng, BOARD_LINUX, n)
    expect(by_id(page, "term-hint")).to_contain_text(f"Enter sends {choice}")


def test_twin_a_typed_line_and_enter_arrive_as_typed_then_one_ending(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    ending(page, "LF")
    focus_terminal(page)
    n = len(eng.consoles.writes)
    page.keyboard.type("print(1)")
    page.keyboard.press("Enter")
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == b"print(1)\n", page=page), written(eng, BOARD_LINUX, n)


# --- 3. the Keys menu -------------------------------------------------------------------------------------


def test_the_keys_menu_sends_ctrl_c_and_any_ctrl_letter(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    n = len(eng.consoles.writes)
    page.locator('[data-action="keys"]').click()
    menu = by_id(page, "keys-menu")
    expect(menu).to_be_visible()
    menu.locator('[data-key="ctrl-c"]').click()
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == b"\x03", page=page)
    expect(menu).to_have_count(0)                        # one click, one key; the menu closes
    expect(page.locator('[data-testid="terminal"] textarea.xterm-helper-textarea')).to_be_focused()
    expect(by_id(page, "console-result")).to_contain_text('key Ctrl-C  (rc 0, 0.0 s)  sent "\\x03"')
    # Ctrl-W: a key the browser keeps for itself (it closes the tab), sent from the menu
    n = len(eng.consoles.writes)
    page.locator('[data-action="keys"]').click()
    by_id(page, "keys-ctrl-letter").fill("w")
    page.locator('[data-key="ctrl-letter"]').click()
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == b"\x17", page=page)
    # Enter alone: the ending
    ending(page, "CR")
    n = len(eng.consoles.writes)
    page.locator('[data-action="keys"]').click()
    by_id(page, "keys-menu").locator('[data-key="enter"]').click()
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == b"\r", page=page)
    assert page.errors == []


def test_twin_the_keys_menu_on_a_read_only_console_sends_nothing(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase, BOARD_LEASED)          # alice@lab-pc-07 holds the hub lease
    expect(by_id(page, "console-readonly")).to_contain_text("alice@lab-pc-07", timeout=T)
    keys = page.locator('[data-action="keys"]')
    expect(keys).to_have_attribute("aria-disabled", "true")
    n = len(eng.consoles.writes)
    keys.click(force=True)                               # aria-disabled: the click says why
    expect(by_id(page, "keys-menu")).to_have_count(0)
    expect(by_id(page, "console-result")).to_contain_text("Nothing was run.")
    page.wait_for_timeout(300)
    assert written(eng, BOARD_LEASED, n) == b""


# --- 4. the send line: ^X and \\xNN, an empty line ----------------------------------------------------------


def test_the_send_line_expands_caret_notation_and_hex_escapes(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    ending(page, "CRLF")
    line = by_id(page, "send-line")
    expect(line).to_have_attribute("placeholder", "a line for uart0 (+ CRLF) · ^C ^] \\x1d: control bytes (\\^ a caret)")
    for typed, want in (("^]", b"\x1d\r\n"), ("^C", b"\x03\r\n"), ("\\x1dq", b"\x1dq\r\n"), ("", b"\r\n")):
        n = len(eng.consoles.writes)
        line.fill(typed)
        line.press("Enter")
        assert wait_until(lambda w=want, k=n: written(eng, BOARD_LINUX, k) == w, page=page), \
            (typed, written(eng, BOARD_LINUX, n))
        expect(by_id(page, "console-result")).to_contain_text("(rc 0, 0.0 s)")
    assert page.errors == []


def test_twin_an_escaped_caret_and_lower_case_stay_as_typed(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    ending(page, "LF")
    line = by_id(page, "send-line")
    for typed, want in (("\\^]", b"^]\n"), ("a^b", b"a^b\n"), ("5^3", b"5^3\n"), ("\\\\x1d", b"\\x1d\n"),
                        ("\\xZZ", b"\\xZZ\n")):
        n = len(eng.consoles.writes)
        line.fill(typed)
        line.press("Enter")
        assert wait_until(lambda w=want, k=n: written(eng, BOARD_LINUX, k) == w, page=page), \
            (typed, written(eng, BOARD_LINUX, n))


# --- 5. a read-only console sends nothing; 6. the cursor ------------------------------------------------


def test_a_read_only_console_sends_nothing_and_shows_no_cursor(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase, BOARD_LEASED)
    expect(by_id(page, "console-readonly")).to_contain_text("alice@lab-pc-07", timeout=T)
    expect(by_id(page, "terminal")).to_have_attribute("data-writable", "no")
    expect(by_id(page, "term-hint")).to_have_count(0)
    n = len(eng.consoles.writes)
    by_id(page, "terminal").click()
    expect(page.locator('[data-testid="terminal"] textarea.xterm-helper-textarea')).not_to_be_focused()
    page.keyboard.type("reboot")
    page.keyboard.press("Enter")
    page.keyboard.press("Control+c")
    page.evaluate(PASTE, "rm -rf /\n")                          # a paste: refused too
    expect(by_id(page, "send-line")).to_be_disabled()
    page.locator('[data-action="send"]').click(force=True)
    expect(by_id(page, "console-result")).to_contain_text("Nothing was run.")
    page.wait_for_timeout(500)
    assert written(eng, BOARD_LEASED, n) == b""
    assert page.evaluate("() => window.__wsSent.length") == 0     # nothing even left the page
    drawn = [c for c in cursor_classes(page) if any(f"xterm-cursor-{k}" in c for k in ("block", "bar", "underline", "outline"))]
    assert drawn == [], cursor_classes(page)                   # no cursor drawn at all
    assert page.errors == []


def test_twin_a_writable_console_shows_a_blinking_block_then_an_outline(showcase):  # noqa: F811
    for scheme in ("light", "dark"):
        page = console_page(showcase, BOARD_V011, scheme)       # no hub: writable
        expect(by_id(page, "terminal")).to_have_attribute("data-writable", "yes")
        focus_terminal(page)
        assert wait_until(lambda p=page: any("xterm-cursor-block" in c for c in cursor_classes(p)), page=page), \
            cursor_classes(page)
        assert any("xterm-cursor-blink" in c for c in cursor_classes(page))
        token = page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--term-cursor').trim()")
        theme = page.evaluate(f"""() => import('./js/consoles.js').then((m) => m.existingSession(
          {BOARD_V011!r}, 'uart0').term.options.theme.cursor)""")
        assert theme == token and token not in ("", "transparent"), (scheme, theme, token)
        ring = page.evaluate("""() => getComputedStyle(document.querySelector('[data-testid="terminal"]')).boxShadow""")
        assert ring not in ("", "none"), ring                       # the focus ring
        page.locator('[data-testid="console-uart0"] .cs-now').click()
        assert wait_until(lambda p=page: any("xterm-cursor-outline" in c for c in cursor_classes(p)), page=page), \
            cursor_classes(page)
        ring = page.evaluate("""() => getComputedStyle(document.querySelector('[data-testid="terminal"]')).boxShadow""")
        assert ring == "none", ring
        assert page.errors == []


def test_picking_a_console_in_the_switcher_gives_it_the_keyboard(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    page.locator('[data-console-tab="uart1"]').click()
    expect(page.locator('[data-testid="terminal"] textarea.xterm-helper-textarea')).to_be_focused(timeout=T)
    n = len(eng.consoles.writes)
    page.keyboard.press("Control+d")
    assert wait_until(lambda: written(eng, BOARD_LINUX, n, "uart1") == b"\x04", page=page)
    assert written(eng, BOARD_LINUX, n, "uart0") == b""


# --- 7. a paste goes whole; the daemon paces it, never the page ----------------------------------------------


def test_a_paste_is_one_message_with_the_ending_for_each_newline(showcase):  # noqa: F811
    eng = showcase.engine
    page = console_page(showcase)
    ending(page, "CRLF")
    focus_terminal(page)
    sent0 = page.evaluate("() => window.__wsSent.length")
    n = len(eng.consoles.writes)
    page.evaluate(PASTE, "print(1+1)\nprint(2)")
    want = b"print(1+1)\r\nprint(2)"
    assert wait_until(lambda: written(eng, BOARD_LINUX, n) == want, page=page), written(eng, BOARD_LINUX, n)
    assert page.evaluate("(k) => window.__wsSent.slice(k)", sent0) == [list(want)]    # ONE message


def test_twin_typed_keys_are_one_message_each(showcase):  # noqa: F811
    page = console_page(showcase)
    focus_terminal(page)
    sent0 = page.evaluate("() => window.__wsSent.length")
    page.keyboard.type("abc")
    assert wait_until(lambda: page.evaluate("(k) => window.__wsSent.slice(k)", sent0) == [[97], [98], [99]], page=page)


@pytest.fixture
def virtual_console(browser, tmp_path, monkeypatch):
    """A page on the real stack (daemon -> Engine -> MPS3 pack -> VirtualMps3), on the
    Workbench console of the board, and the board."""
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager_mps3.pack import Mps3Pack
    from tests.fakes import virtual_board
    from tests.fakes.t14_mock_api import real_daemon
    from tests.fakes.virtual_board import FIELDED_ILA_V011, VirtualMps3

    # this lane's ports: 20000-23000 (10000-19999 is kept free for the lab's services)
    monkeypatch.setattr(virtual_board, "BOARD_PORT_RANGE", (20000, 23000))
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011) as vb:
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                        packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        daemon = real_daemon(engine, token="polish-vb", state_dir=tmp_path / "daemon").start()
        ctx = browser.new_context(viewport=APP, color_scheme="light", reduced_motion="reduce")
        try:
            cand = vb.candidate()
            daemon.seed(cand)
            page = ctx.new_page()
            page.errors = []
            page.on("pageerror", lambda e: page.errors.append(str(e)))
            page.add_init_script(WS_SPY)
            page.goto(daemon.ui_url)
            page.locator(f'.board-item[data-board="{cand.board_id}"]').click()
            page.locator('[data-action="open"]').click()
            page.wait_for_selector('[data-testid="board-header"]', timeout=T)
            nav.tab(page, "workbench")
            page.locator('[data-console-tab="uart0"]').click()       # the switcher may open on SWO
            page.wait_for_selector('[data-testid="console-uart0"] [data-testid="console-state"]:has-text("up")',
                                   timeout=T)
            yield page, engine, cand.board_id
        finally:
            ctx.close()
            daemon.stop()


def _terminal_text(page: Any, bid: str) -> str:
    return page.evaluate("([b]) => window.__harness_managerConsoles.text(b, 'uart0')", [bid]) or ""


def test_a_pasted_line_reaches_the_paced_dut_uart_whole(virtual_console):
    page, engine, bid = virtual_console
    focus_terminal(page)
    sent0 = page.evaluate("() => window.__wsSent.length")
    line = "print('pasted whole 0123456789')"
    page.evaluate(PASTE, line)
    # the page sent it in ONE message; the daemon paces it to the UART a byte at a time (20 ms)
    assert page.evaluate("(k) => window.__wsSent.slice(k)", sent0) == [list(line.encode())]
    assert wait_until(lambda: line in _terminal_text(page, bid), timeout=15, page=page), _terminal_text(page, bid)
    assert engine.consoles._ups[(bid, "uart0")].pace_s == pytest.approx(0.02)      # the daemon paces
    assert page.errors == []


def test_twin_the_echo_of_typed_keys_arrives_in_order(virtual_console):
    page, _engine, bid = virtual_console
    focus_terminal(page)
    page.keyboard.type("abcdef", delay=5)                      # faster than the 20 ms pacing
    assert wait_until(lambda: "abcdef" in _terminal_text(page, bid), timeout=15, page=page), _terminal_text(page, bid)
