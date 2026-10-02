"""BRINGUP-USB's page contract, statically: the routes js/api.js's BRINGUP_ENDPOINTS calls are
the ones docs/API.md gives bringup_api and the real daemon serves (as test_t14_static does for
ENDPOINTS), the card-reader routes are SD-FLASH's contract, and the wizard writes a card only
with the contract's kinds. Each check has a negative twin fed a bad input."""

from __future__ import annotations

import re

from tests.fakes.t14_api_contract import (
    STATIC,
    api_md_sections,
    daemon_routes,
    parse_ui_endpoints,
)

API_JS = STATIC / "js" / "api.js"
BRINGUP_JS = STATIC / "js" / "bringup.js"
#: lane SD-FLASH's routes (the contract in the BRING-UP brief); served by cardwriter_api.py
CARDWRITER = {("GET", "/cardwriter/devices"), ("POST", "/cardwriter/write"),
              ("POST", "/cardwriter/check")}


def bringup_endpoints(js: str) -> dict[str, tuple[str, str]]:
    """BRINGUP_ENDPOINTS's names (v1.1: a list), each resolved through ENDPOINTS; a name
    ENDPOINTS lacks resolves to ("?", "?"), which nothing documents or serves."""
    block = js.split("export const BRINGUP_ENDPOINTS", 1)[1].split("]);", 1)[0]
    table = parse_ui_endpoints(js)
    return {n: table.get(n, ("?", "?")) for n in re.findall(r'"(\w+)"', block)}


def problems(called: dict[str, tuple[str, str]], documented: set, served: set) -> list[str]:
    out = []
    for name, ep in called.items():
        if ep in CARDWRITER:
            continue
        if ep not in documented:
            out.append(f"{name}: {ep} is not in docs/API.md's bringup_api section")
        if ep not in served:
            out.append(f"{name}: {ep} is not served by harness-manager-daemon")
    return out


def test_every_bringup_route_the_page_calls_is_documented_and_served():
    called = bringup_endpoints(API_JS.read_text(encoding="utf-8"))
    assert len(called) == 10
    assert problems(called, api_md_sections()["bringup_api"], daemon_routes()) == []
    assert {ep for ep in called.values() if ep in CARDWRITER} == CARDWRITER


def test_twin_a_route_nobody_documents_or_serves_is_caught():
    js = ('export const ENDPOINTS = Object.freeze({\n'
          '  sneaky: ["POST", "/bringup/format"],\n});\n'
          'export const BRINGUP_ENDPOINTS = Object.freeze(["sneaky"]);')
    got = problems(bringup_endpoints(js), api_md_sections()["bringup_api"], daemon_routes())
    assert len(got) == 2 and all("sneaky" in g for g in got)


def write_kinds(js: str) -> set[str]:
    """The kinds the page writes: ``readerWrite``'s (a whole-card image through
    ``/cardwriter/write``), and ``files`` for the bring-up's own card-reader route (a bundle:
    its checks and the typed unsigned phrase first, then the card writer's ``files``)."""
    kinds = set(re.findall(r'readerWrite\([^)]*?"(\w+)"', js))
    if re.search(r'readerJob\(ctx, "bringupCardReader"', js):
        kinds.add("files")
    return kinds


def test_the_wizard_writes_only_the_contract_kinds_files_and_card():
    assert write_kinds(BRINGUP_JS.read_text(encoding="utf-8")) == {"files", "card"}


def test_twin_the_old_image_kind_would_be_caught():
    assert write_kinds('readerWrite(bid, ctx, "image", dev, src, ok)') == {"image"}


def test_a_bundle_in_the_reader_goes_through_the_bringup_route_never_straight_to_the_writer():
    """The unsigned phrase is checked by the service: a bundle's files never go to
    /cardwriter/write directly (that route takes no unsigned phrase)."""
    js = BRINGUP_JS.read_text(encoding="utf-8")
    assert not re.search(r'readerWrite\([^)]*?"files"', js)
    assert re.search(r'confirm_unsigned: w\.unsignedTyped\.trim\(\)', js)


def test_twin_a_files_write_to_the_writer_would_be_caught():
    assert re.search(r'readerWrite\([^)]*?"files"', 'readerWrite(bid, ctx, "files", d, s, c)')
