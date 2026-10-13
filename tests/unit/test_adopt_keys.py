# ruff: noqa: F811  (pytest fixtures imported from the LINUX-CLAIM tests)
"""ADOPT-GUI: adopting a board from the app, and adding or removing a key on a claimed board.

Built on the LINUX-CLAIM rig (FakeShell + a fake board SSH). ``KeysSsh`` adds what the board's
shell does with the key scripts: it reads the ``BLOB``/``LINE`` assignments and keeps the file in
``shell.authorized_keys``. Every refusal has a twin that works, and a refused action must have
sent nothing to the board (no script reached the fake ssh).
"""

from __future__ import annotations

import shlex
from typing import Any

import pytest

from harness_manager.core.errors import HeldError, RefusedError, UsageError
from harness_manager.services.claim import ClaimService
from harness_manager_mps3 import claim as CL
from tests.fakes.lc_fake_board_ssh import FakeBoardSsh, make_key_line
from tests.unit.test_linux_claim import (  # noqa: F401  (fixtures and helpers)
    Leases,
    claim_it,
    rig_factory,
)

THEIRS = make_key_line("sams-laptop")
THEIRS_FP = CL.fingerprint(THEIRS.split()[1])


class KeysSsh(FakeBoardSsh):
    """The fake ssh plus the board's reaction to the ``# hm-keys`` scripts."""

    def __init__(self, inner: FakeBoardSsh) -> None:
        super().__init__(inner.shell, inner.host_key_line, mitm_key=inner.mitm_key)
        self.scripts: list[str] = []

    def __call__(self, argv, timeout):
        res = super().__call__(argv, timeout)
        body = argv[-1]
        if res.returncode != 0 or not body.lstrip().startswith(tuple(f"{k}=" for k in ("BLOB", "LINE"))) \
                and "# hm-keys" not in body:
            return res
        self.scripts.append(body)
        env: dict[str, str] = {}
        for ln in body.splitlines():
            if ln.startswith(("BLOB=", "LINE=")):
                k, _, v = ln.partition("=")
                env[k] = shlex.split(v)[0]
        text = (self.shell.authorized_keys or b"").decode()
        lines = [x for x in text.splitlines() if x.strip()]
        out = CL.RunResult
        if "# hm-keys list" in body:
            return out(0, text, "")
        if "# hm-keys add" in body:
            if any(env["BLOB"] in x.split() for x in lines):
                return out(0, "there /persist/ssh/authorized_keys\nthere /root/.ssh/authorized_keys\n", "")
            self.shell.authorized_keys = (text.rstrip("\n") + "\n" + env["LINE"] + "\n").encode()
            return out(0, "added /persist/ssh/authorized_keys\nadded /root/.ssh/authorized_keys\n", "")
        if "# hm-keys remove" in body:
            keep = [x for x in lines if env["BLOB"] not in x.split()]
            if not keep:
                return out(4, "last\n", "")
            self.shell.authorized_keys = ("\n".join(keep) + "\n").encode()
            return out(0, "removed /persist/ssh/authorized_keys\n", "")
        return res


def mine_rig(rig_factory, monkeypatch):
    """A board this Harness Manager claimed, with a second key already accepted."""
    rig = rig_factory()
    claim_it(rig)
    ssh = KeysSsh(rig.ssh)
    monkeypatch.setattr(CL, "DEFAULT_RUN", ssh)
    return rig, ssh, ClaimService(None, leases=Leases())


def own_fp(rig) -> str:
    return CL.fingerprint(rig.public.read_text().split()[1])


# --- adopt ----------------------------------------------------------------------------------------


def test_adopt_a_board_claimed_with_this_pcs_key_makes_it_mine(rig_factory):
    rig = rig_factory(claimed=True)
    rig.shell.authorized_keys = rig.public.read_bytes()
    svc = ClaimService(None, leases=Leases())
    assert rig.claim.claim_status()["state"] == "other"
    st = svc.claim(rig.session, confirm=True, key=str(rig.public), adopt=True)
    assert st["action"] == "adopted" and st["state"] == "mine"


def test_adopt_is_refused_without_confirmation_and_by_a_non_holder(rig_factory):
    rig = rig_factory(claimed=True)
    rig.shell.authorized_keys = rig.public.read_bytes()
    with pytest.raises(RefusedError, match="confirmation"):
        ClaimService(None, leases=Leases()).claim(rig.session, confirm=False, adopt=True)
    rig.session.hub = object()
    with pytest.raises(HeldError, match="lease holder only"):
        ClaimService(None, leases=Leases(mine=False, holder="bob")).claim(
            rig.session, confirm=True, key=str(rig.public), adopt=True)
    assert rig.ssh.calls == []


def test_adopt_with_a_key_the_board_does_not_hold_says_so_in_plain_words(rig_factory):
    rig = rig_factory(claimed=True)
    rig.shell.authorized_keys = f"{THEIRS} x\n".encode()
    with pytest.raises(RefusedError, match="refused your key"):
        ClaimService(None, leases=Leases()).claim(rig.session, confirm=True,
                                                  key=str(rig.public), adopt=True)


# --- checking a pasted key ----------------------------------------------------------------------------


def test_a_valid_public_key_is_checked_and_shown_by_fingerprint():
    info = CL.parse_public_key(f"{THEIRS} sam@laptop", "Sam's laptop")
    assert info["fingerprint"] == THEIRS_FP and info["label"] == "Sam's laptop"
    assert info["line"] == f"{THEIRS} Sam's laptop"
    # the comment is the label when none is given
    assert CL.parse_public_key(f"{THEIRS} sam@laptop")["label"] == "sam@laptop"


@pytest.mark.parametrize("text,words", [
    ("", "paste the public key"),
    ("ssh-ed25519", "not valid"),
    ("ssh-ed25519 !!!notbase64", "not valid"),
    ("ssh-dss AAAAB3NzaC1kc3M= x", "not an OpenSSH public key"),
    (f'command="rm -rf /" {THEIRS} x', "not an OpenSSH public key"),
    (f"{THEIRS} a\n{THEIRS} b", "ONE public key"),
    (f"ssh-rsa {THEIRS.split()[1]} x", "does not match"),
])
def test_invalid_keys_are_refused_with_a_reason(text, words):
    with pytest.raises(UsageError, match=words):
        CL.parse_public_key(text, "x")


def test_a_key_without_a_label_or_comment_is_refused():
    with pytest.raises(UsageError, match="label"):
        CL.parse_public_key(THEIRS)


def test_a_private_key_is_refused_outright_and_never_echoed():
    secret = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ-SECRET-BODY"
    pem = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{secret}\n-----END OPENSSH PRIVATE KEY-----\n"
    with pytest.raises(UsageError) as exc:
        CL.parse_public_key(pem, "x")
    assert "PRIVATE key" in exc.value.message
    assert secret not in exc.value.message and secret not in str(exc.value.hint or "")


# --- adding a key ------------------------------------------------------------------------------------


def test_add_key_appends_it_and_is_idempotent(rig_factory, monkeypatch):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    out = svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam's laptop",
                       expect_fingerprint=THEIRS_FP)
    assert out["action"] == "added" and out["fingerprint"] == THEIRS_FP
    assert {k["fingerprint"] for k in out["keys"]} == {own_fp(rig), THEIRS_FP}
    assert b"Sam's laptop" in rig.shell.authorized_keys
    again = svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam's laptop",
                         expect_fingerprint=THEIRS_FP)
    assert again["action"] == "already_there"
    assert rig.shell.authorized_keys.count(THEIRS.split()[1].encode()) == 1


def test_listing_shows_fingerprints_and_labels_and_marks_this_pcs_key(rig_factory, monkeypatch):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam", expect_fingerprint=THEIRS_FP)
    keys = svc.keys_list(rig.session)
    by = {k["fingerprint"]: k for k in keys}
    assert by[own_fp(rig)]["mine"] is True and by[THEIRS_FP]["mine"] is False
    assert by[THEIRS_FP]["label"] == "Sam"
    assert all(set(k) == {"fingerprint", "type", "label", "mine"} for k in keys)   # no key text


@pytest.mark.parametrize("kw,exc,words", [
    ({"confirm": False, "key": THEIRS, "label": "x", "expect_fingerprint": THEIRS_FP},
     RefusedError, "confirmation"),
    ({"confirm": True, "key": THEIRS, "label": "x", "expect_fingerprint": ""},
     RefusedError, "fingerprint you confirmed"),
    ({"confirm": True, "key": THEIRS, "label": "x", "expect_fingerprint": CL.fingerprint(
        make_key_line("other").split()[1])}, RefusedError, "not the one you confirmed"),
    ({"confirm": True, "key": "-----BEGIN OPENSSH PRIVATE KEY-----\nzz", "label": "x",
      "expect_fingerprint": THEIRS_FP}, UsageError, "PRIVATE key"),
    ({"confirm": True, "key": "ssh-ed25519 AAAA", "label": "x", "expect_fingerprint": "SHA256:x"},
     UsageError, "not valid|does not match"),
])
def test_a_refused_add_sends_nothing_to_the_board(rig_factory, monkeypatch, kw, exc, words):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    before = rig.shell.authorized_keys
    with pytest.raises(exc, match=words):
        svc.keys_add(rig.session, **kw)
    assert ssh.scripts == [] and rig.shell.authorized_keys == before


def test_add_is_for_the_lease_holder_only_and_sends_nothing(rig_factory, monkeypatch):
    rig, ssh, _ = mine_rig(rig_factory, monkeypatch)
    rig.session.hub = object()
    with pytest.raises(HeldError, match="lease holder only"):
        ClaimService(None, leases=Leases(mine=False, holder="bob")).keys_add(
            rig.session, confirm=True, key=THEIRS, label="x", expect_fingerprint=THEIRS_FP)
    assert ssh.scripts == []


def test_a_board_this_pc_did_not_claim_gets_no_keys_added(rig_factory, monkeypatch):
    rig = rig_factory(claimed=True)                  # claimed by someone else, not adopted
    ssh = KeysSsh(rig.ssh)
    monkeypatch.setattr(CL, "DEFAULT_RUN", ssh)
    with pytest.raises(RefusedError, match="claiming key"):
        ClaimService(None, leases=Leases()).keys_add(
            rig.session, confirm=True, key=THEIRS, label="x", expect_fingerprint=THEIRS_FP)
    assert ssh.calls == []


# --- removing a key ----------------------------------------------------------------------------------


def test_remove_a_key_by_fingerprint(rig_factory, monkeypatch):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam", expect_fingerprint=THEIRS_FP)
    out = svc.keys_remove(rig.session, confirm=True, fingerprint=THEIRS_FP)
    assert out["action"] == "removed" and [k["fingerprint"] for k in out["keys"]] == [own_fp(rig)]
    assert THEIRS.split()[1].encode() not in rig.shell.authorized_keys


def test_remove_refuses_the_key_this_pc_uses_and_sends_nothing(rig_factory, monkeypatch):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam", expect_fingerprint=THEIRS_FP)
    sent = len(ssh.scripts)
    before = rig.shell.authorized_keys
    with pytest.raises(RefusedError, match="lock you out"):
        svc.keys_remove(rig.session, confirm=True, fingerprint=own_fp(rig))
    assert len(ssh.scripts) == sent and rig.shell.authorized_keys == before


@pytest.mark.parametrize("fp,words", [
    ("not-a-fingerprint", "SHA256"),
    (CL.fingerprint(make_key_line("nobody").split()[1]), "does not have a key"),
])
def test_remove_refuses_a_bad_or_unknown_fingerprint(rig_factory, monkeypatch, fp, words):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    before = rig.shell.authorized_keys
    with pytest.raises((RefusedError, UsageError), match=words):
        svc.keys_remove(rig.session, confirm=True, fingerprint=fp)
    assert rig.shell.authorized_keys == before


def test_remove_needs_confirmation(rig_factory, monkeypatch):
    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    with pytest.raises(RefusedError, match="confirmation"):
        svc.keys_remove(rig.session, confirm=False, fingerprint=THEIRS_FP)
    assert ssh.scripts == []


# --- nothing but fingerprints comes back ----------------------------------------------------------------


def test_results_carry_fingerprints_never_key_text(rig_factory, monkeypatch):
    import json

    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    blob = THEIRS.split()[1]
    seen: list[Any] = [svc.keys_preview(rig.session, THEIRS, "Sam")]
    seen.append(svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam",
                             expect_fingerprint=THEIRS_FP))
    seen.append(svc.keys_list(rig.session))
    seen.append(svc.keys_remove(rig.session, confirm=True, fingerprint=THEIRS_FP))
    text = json.dumps(seen)
    assert blob not in text and rig.public.read_text().split()[1] not in text
    assert "PRIVATE" not in text and THEIRS_FP in text


# --- the CLI: board keys list|add|remove -----------------------------------------------------------------


class StubCtx:
    """What ``cmd_keys`` uses of a Ctx: args, engine, board(), confirm(), note(), emit()."""

    def __init__(self, rig, svc, args, answer="y") -> None:
        self.args, self.answer, self.asked, self.emitted = args, answer, [], []
        self.engine = type("E", (), {"board_claim": svc})()
        self._rig = rig

    def board(self, note=""):
        import contextlib

        @contextlib.contextmanager
        def cm():
            yield self._rig.session.candidate, self._rig.session
        return cm()

    def confirm(self, question):
        self.asked.append(question)
        if getattr(self.args, "yes", False):
            return
        if self.answer != "y":
            raise RefusedError("not confirmed")

    def note(self, text):
        pass

    def emit(self, result):
        self.emitted.append(result)


def cli_args(*argv):
    import argparse

    from harness_manager.cli.cmd_board import add_keys_parser

    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="board_cmd")
    add_keys_parser(sub, [])
    return top.parse_args(["keys", *argv])


def test_cli_keys_add_asks_with_the_fingerprint_then_adds_and_lists(rig_factory, monkeypatch):
    from harness_manager.cli.cmd_board import cmd_keys

    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    ctx = StubCtx(rig, svc, cli_args("add", "--key", THEIRS, "--label", "Sam"))
    assert cmd_keys(ctx) == 0
    assert THEIRS_FP in ctx.asked[0] and THEIRS.split()[1] not in ctx.asked[0]
    res = ctx.emitted[0]
    assert res.rows[0][1] == THEIRS_FP and res.rows[0][-1] == "added"
    assert THEIRS.split()[1] not in "\n".join(res.human)


def test_cli_keys_add_declined_or_private_sends_nothing(rig_factory, monkeypatch, tmp_path):
    from harness_manager.cli.cmd_board import cmd_keys

    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    with pytest.raises(RefusedError, match="not confirmed"):
        cmd_keys(StubCtx(rig, svc, cli_args("add", "--key", THEIRS, "--label", "Sam"), "n"))
    pem = tmp_path / "id"
    pem.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----\n")
    with pytest.raises(UsageError, match="PRIVATE key"):
        cmd_keys(StubCtx(rig, svc, cli_args("add", "--key-file", str(pem), "--yes")))
    assert ssh.scripts == []


def test_cli_keys_remove_refuses_own_key_and_removes_another(rig_factory, monkeypatch):
    from harness_manager.cli.cmd_board import cmd_keys

    rig, ssh, svc = mine_rig(rig_factory, monkeypatch)
    svc.keys_add(rig.session, confirm=True, key=THEIRS, label="Sam", expect_fingerprint=THEIRS_FP)
    with pytest.raises(RefusedError, match="lock you out"):
        cmd_keys(StubCtx(rig, svc, cli_args("remove", "--fingerprint", own_fp(rig), "--yes")))
    ctx = StubCtx(rig, svc, cli_args("remove", "--fingerprint", THEIRS_FP, "--yes"))
    assert cmd_keys(ctx) == 0 and ctx.emitted[0].rows[0][-1] == "removed"
    lst = StubCtx(rig, svc, cli_args("list"))
    cmd_keys(lst)
    assert [r[1] for r in lst.emitted[0].rows] == [own_fp(rig)]
