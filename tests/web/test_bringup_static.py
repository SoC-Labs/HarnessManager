"""BRINGUP-USB's page contract, statically: the routes js/api.js's BRINGUP_ENDPOINTS calls are
the ones docs/API.md gives bringup_api and the real daemon serves (as test_t14_static does for
ENDPOINTS), the card-reader routes are SD-FLASH's contract, and the wizard writes a card only
with the contract's kinds. Each check has a negative twin fed a bad input."""

from __future__ import annotations

import re

from tests.fakes.t14_api_contract import STATIC, api_md_sections, daemon_routes, normalise

API_JS = STATIC / "js" / "api.js"
BRINGUP_JS = STATIC / "js" / "bringup.js"
_ENTRY = re.compile(r'^\s*(\w+):\s*\[\s*"([A-Z]+)"\s*,\s*"([^"]+)"\s*\]', re.M)
#: lane SD-FLASH's routes (the contract in the BRING-UP brief); served by cardwriter_api.py
CARDWRITER = {("GET", "/cardwriter/devices"), ("POST", "/cardwriter/write")}


def bringup_endpoints(js: str) -> dict[str, tuple[str, str]]:
    block = js.split("export const BRINGUP_ENDPOINTS", 1)[1].split("});", 1)[0]
    return {n: (m, normalise(p)) for n, m, p in _ENTRY.findall(block)}


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
    assert len(called) == 7
    assert problems(called, api_md_sections()["bringup_api"], daemon_routes()) == []
    assert {ep for ep in called.values() if ep in CARDWRITER} == CARDWRITER


def test_twin_a_route_nobody_documents_or_serves_is_caught():
    js = ('export const BRINGUP_ENDPOINTS = Object.freeze({\n'
          '  sneaky: ["POST", "/bringup/format"],\n});')
    got = problems(bringup_endpoints(js), api_md_sections()["bringup_api"], daemon_routes())
    assert len(got) == 2 and all("sneaky" in g for g in got)


def write_kinds(js: str) -> set[str]:
    return set(re.findall(r'readerWrite\([^)]*?"(\w+)"', js))


def test_the_wizard_writes_only_the_contract_kinds_files_and_card():
    assert write_kinds(BRINGUP_JS.read_text(encoding="utf-8")) == {"files", "card"}


def test_twin_the_old_image_kind_would_be_caught():
    assert write_kinds('readerWrite(bid, ctx, "image", dev, src, ok)') == {"image"}
