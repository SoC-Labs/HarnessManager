# ruff: noqa: F811  (pytest fixtures imported from the LINUX-CLAIM tests)
"""ADOPT-GUI through the real Engine and daemon routes: adopt, and keys list/check/add/remove."""

from __future__ import annotations

import json

from harness_manager_mps3 import claim as CL
from tests.fakes.lc_fake_board_ssh import make_key_line
from tests.fakes.t13_daemon import headers
from tests.integration.test_linux_claim_cli import (  # noqa: F401  (fixtures and helpers)
    client,
    linux,
    wait,
)
from tests.unit.test_adopt_keys import THEIRS, THEIRS_FP, KeysSsh

H = headers()


def claimed_board(c, B, linux, monkeypatch):
    shell, ssh, public = linux
    monkeypatch.setattr(CL, "DEFAULT_RUN", KeysSsh(ssh))
    r = c.post(f"{B}/claim", json={"confirm": True, "key": str(public)}, headers=H)
    assert wait(c, r.json()["job"])["state"] == "done"
    return shell, public


def test_api_adopt_a_board_claimed_with_this_pcs_key(client, linux):
    c, B = client
    shell, _ssh, public = linux
    shell.ssh_claimed, shell.authorized_keys = True, public.read_bytes()
    assert c.get(f"{B}/claim?refresh=true", headers=H).json()["claim"]["state"] == "other"
    r = c.post(f"{B}/claim", json={"adopt": True, "key": str(public)}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"      # no confirm
    r = c.post(f"{B}/claim", json={"confirm": True, "adopt": True, "key": str(public)}, headers=H)
    done = wait(c, r.json()["job"])
    assert done["state"] == "done" and done["result"]["claim"]["action"] == "adopted"
    assert c.get(f"{B}/claim?refresh=true", headers=H).json()["claim"]["state"] == "mine"


def test_api_adopt_with_the_wrong_key_is_refused_in_plain_words(client, linux):
    c, B = client
    shell, _ssh, public = linux
    shell.ssh_claimed, shell.authorized_keys = True, f"{THEIRS} x\n".encode()
    r = c.post(f"{B}/claim", json={"confirm": True, "adopt": True, "key": str(public)}, headers=H)
    done = wait(c, r.json()["job"])
    assert done["state"] != "done" and "refused your key" in json.dumps(done)
    assert c.get(f"{B}/claim?refresh=true", headers=H).json()["claim"]["state"] == "other"


def test_api_keys_check_add_list_remove(client, linux, monkeypatch):
    c, B = client
    shell, public = claimed_board(c, B, linux, monkeypatch)
    own = CL.fingerprint(public.read_text().split()[1])
    chk = c.post(f"{B}/claim/keys/check", json={"key": THEIRS, "label": "Sam"}, headers=H)
    assert chk.status_code == 200 and chk.json()["key"]["fingerprint"] == THEIRS_FP
    assert THEIRS.split()[1] not in chk.text
    add = {"confirm": True, "key": THEIRS, "label": "Sam", "expect_fingerprint": THEIRS_FP}
    r = c.post(f"{B}/claim/keys", json={**add, "confirm": False}, headers=H)
    assert r.status_code == 409 and THEIRS.split()[1].encode() not in shell.authorized_keys
    done = wait(c, c.post(f"{B}/claim/keys", json=add, headers=H).json()["job"])
    assert done["state"] == "done" and done["result"]["key"]["action"] == "added"
    again = wait(c, c.post(f"{B}/claim/keys", json=add, headers=H).json()["job"])
    assert again["result"]["key"]["action"] == "already_there"
    listed = c.get(f"{B}/claim/keys", headers=H).json()["keys"]
    assert {k["fingerprint"] for k in listed} == {own, THEIRS_FP}
    assert THEIRS.split()[1] not in json.dumps(listed)
    rm = wait(c, c.post(f"{B}/claim/keys/remove", json={"confirm": True, "fingerprint": THEIRS_FP},
                        headers=H).json()["job"])
    assert rm["state"] == "done" and rm["result"]["key"]["action"] == "removed"


def test_api_refused_key_actions_send_nothing(client, linux, monkeypatch):
    c, B = client
    shell, public = claimed_board(c, B, linux, monkeypatch)
    own = CL.fingerprint(public.read_text().split()[1])
    before = shell.authorized_keys
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETBODY\n-----END OPENSSH PRIVATE KEY-----\n"
    for path, body in ((f"{B}/claim/keys/check", {"key": pem, "label": "x"}),
                       (f"{B}/claim/keys", {"confirm": True, "key": pem, "label": "x",
                                            "expect_fingerprint": THEIRS_FP})):
        r = c.post(path, json=body, headers=H)
        assert r.status_code == 400 and "PRIVATE key" in r.text and "SECRETBODY" not in r.text
    r = c.post(f"{B}/claim/keys", json={"confirm": True, "key": THEIRS, "label": "x",
                                         "expect_fingerprint": CL.fingerprint(
                                             make_key_line("other").split()[1])}, headers=H)
    assert wait(c, r.json()["job"])["state"] != "done"
    r = c.post(f"{B}/claim/keys/remove", json={"confirm": True, "fingerprint": own}, headers=H)
    done = wait(c, r.json()["job"])
    assert done["state"] != "done" and "lock you out" in json.dumps(done)
    assert shell.authorized_keys == before
