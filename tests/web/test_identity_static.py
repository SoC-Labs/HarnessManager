"""Lane IDENTITY's page contract, statically: the route js/api.js's IDENTITY_ENDPOINTS calls is
the one docs/API.md gives identity_api and the real daemon serves (as test_t14_static does for
ENDPOINTS); there is ONE identity dialog (the old "Fix identity" and the wizard's "Set this
board's identity" are folded into "Name this board"); the dialog's stylesheet resolves. Each
check has a negative twin fed a bad input."""

from __future__ import annotations

import re

from tests.fakes.t14_api_contract import STATIC, api_md_sections, daemon_routes, normalise

API_JS = STATIC / "js" / "api.js"
IDENTITY_JS = STATIC / "js" / "sections" / "identity.js"
_ENTRY = re.compile(r'^\s*(\w+):\s*\[\s*"([A-Z]+)"\s*,\s*"([^"]+)"\s*\]', re.M)


def identity_endpoints(js: str) -> dict[str, tuple[str, str]]:
    block = js.split("export const IDENTITY_ENDPOINTS", 1)[1].split("});", 1)[0]
    return {n: (m, normalise(p)) for n, m, p in _ENTRY.findall(block)}


def problems(called: dict[str, tuple[str, str]], documented: set, served: set) -> list[str]:
    out = []
    for name, ep in called.items():
        if ep not in documented:
            out.append(f"{name}: {ep} is not in docs/API.md's identity_api section")
        if ep not in served:
            out.append(f"{name}: {ep} is not served by harness-manager-daemon")
    return out


def test_every_identity_route_the_page_calls_is_documented_and_served():
    called = identity_endpoints(API_JS.read_text(encoding="utf-8"))
    assert called == {"identityProposal": ("GET", "/boards/{}/identity/proposal")}
    assert problems(called, api_md_sections()["identity_api"], daemon_routes()) == []


def test_twin_a_route_nobody_documents_or_serves_is_caught():
    js = ('export const IDENTITY_ENDPOINTS = Object.freeze({\n'
          '  sneaky: ["POST", "/boards/{bid}/identity/force"],\n});')
    got = problems(identity_endpoints(js), api_md_sections()["identity_api"], daemon_routes())
    assert len(got) == 2 and all("sneaky" in g for g in got)


def dialogs(root=STATIC / "js") -> dict[str, list[str]]:
    """Every registerModal kind, by file."""
    out: dict[str, list[str]] = {}
    for p in root.rglob("*.js"):
        for kind in re.findall(r'registerModal\("([\w-]+)"', p.read_text(encoding="utf-8")):
            out.setdefault(kind, []).append(p.name)
    return out


def test_there_is_one_identity_dialog_and_the_old_two_are_gone():
    kinds = dialogs()
    assert kinds.get("name-board") == ["identity.js"]
    assert "fixid" not in kinds and "bu-identity" not in kinds
    js = "\n".join(p.read_text(encoding="utf-8") for p in (STATIC / "js").rglob("*.js"))
    assert 'openModal("fixid"' not in js and "openModal('bu-identity'" not in js
    assert js.count('openModal("name-board"') >= 3        # Access: name, fix; the wizard


def test_twin_a_second_identity_dialog_would_be_caught(tmp_path):
    (tmp_path / "x.js").write_text('registerModal("bu-identity", X);\n', encoding="utf-8")
    assert "bu-identity" in dialogs(tmp_path)


def test_the_dialogs_stylesheet_ships_and_is_linked_the_lanes_way():
    js = IDENTITY_JS.read_text(encoding="utf-8")
    assert 'l.href = "./css/identity.css"' in js
    assert (STATIC / "css" / "identity.css").is_file()


def test_the_name_rule_is_the_services():
    """The page checks names as the service does (``identity_assign.name_problem``)."""
    from harness_manager.services import identity_assign as IA

    js = IDENTITY_JS.read_text(encoding="utf-8")
    assert f"export const NAME_MAX = {IA.NAME_MAX};" in js
    for words in ("only A-Z, 0-9 and - are allowed", "the panel shows", "it is empty",
                  "a space"):
        assert words in js and any(words in IA.name_problem(v) for v in
                                   ("a b", "x" * 17, "", "a_b")), words
