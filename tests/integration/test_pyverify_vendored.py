"""PYVERIFY-VENDOR: Harness Manager against the vendored pyverify (platform master 3f7cea2).

pyverify's FakeShell is the platform's executable spec of the harness. From 3f7cea2 it models
what HM so far had only its own doubles for: the board identity (net-protocol v0.16
``identity``/``identity_set``, ``locate``), identify's ``label`` and ``ssh.key_sha256``, and
the slot verb's ``code``/``claimed``/``confirmed``; and ``ShellClient`` gained
``identity()``/``identity_set()``/``locate()``. These tests hold HM to it:

- the requests HM builds for identity, identity_set and locate are, byte for byte, the ones
  pyverify's ``ShellClient`` sends (HM keeps building them, so an older editable pyverify
  still works);
- HM's identity service and Identify, end to end on the vendored model (a plain
  ``FakeShell(profile="linux")``): the read, a change, the refusals, the blink;
- ``tests/fakes/idn_board.py`` (HM's own model, used by the BOARD-ID tests) has the vendored
  model's rules and refusal texts;
- the claim: HM's fingerprint of its own key is the board's ``ssh.key_sha256``;
- the feature names and slot codes HM gates on are ones the vendored pyverify defines.

Every behaviour has its negative twin. The boards are real sockets on 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import struct

import pytest
from pyverify import client as pv_client
from pyverify.client import ShellClient
from pyverify.testing import fakeshell as pvfs
from pyverify.testing.fakeshell import FakeShell

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager_mps3 import net_identity as NI
from harness_manager_mps3 import slot_words as W
from harness_manager_mps3.claim import Mps3Claim, fingerprint, parse_key_line
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes import idn_board
from tests.fakes.lxslots_board import LINUX_SID, board_session

#: A well-formed ed25519 public key line (a made-up 32-byte key).
KEY_BLOB = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
KEY_LINE = "ssh-ed25519 " + base64.b64encode(KEY_BLOB).decode() + " hm@test"


class _Capture:
    """A pyverify ``Transport`` that keeps each request line and answers ``ok``."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def send_line(self, payload: bytes) -> None:
        self.lines.append(payload)

    def recv_line(self) -> bytes:
        return b'{"ok":true}\n'

    def close(self) -> None:
        pass


def pyverify_sends(call) -> bytes:
    """The request line ``call(client)`` makes pyverify's ShellClient send."""
    cap = _Capture()
    call(ShellClient("127.0.0.1", transport=cap))
    assert len(cap.lines) == 1
    return cap.lines[0]


@pytest.fixture
def sent(monkeypatch):
    """Every request line any ShellClient in this process sends (HM's included)."""
    lines: list[bytes] = []
    real = pv_client.ShellClient._send_op

    def spy(self, op):
        lines.append(json.dumps(op, separators=(",", ":")).encode("ascii"))
        return real(self, op)

    monkeypatch.setattr(pv_client.ShellClient, "_send_op", spy)
    return lines


@pytest.fixture
def linux(monkeypatch):
    """A plain vendored ``FakeShell(profile="linux")`` (no HM subclass) and HM's session."""
    made = []

    def make(**kw):
        kw.setdefault("profile", "linux")
        kw.setdefault("static_id", LINUX_SID)
        kw.setdefault("harness_version", "1.0.0")
        fake = FakeShell.ephemeral(**kw)
        fake.start()
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        session = board_session(fake)
        made.append((fake, session))
        return fake, session

    yield make
    for fake, session in made:
        session.close()
        fake.stop()


def ops(lines: list[bytes], op: str) -> list[bytes]:
    return [ln for ln in lines if json.loads(ln)["op"] == op]


# --- 1. HM's requests are pyverify's --------------------------------------------------------------


def test_hms_identity_requests_are_the_ones_pyverify_sends(linux, sent):
    fake, session = linux()
    session.net_identity.read(refresh=True)
    session.net_identity.set_identity({"label": "MPS3-02", "ip": "192.168.11.101/24"})
    session.net_identity.set_identity({"clear": True})
    assert ops(sent, "identity") == [pyverify_sends(lambda c: c.identity())]
    assert ops(sent, "identity_set") == [
        pyverify_sends(lambda c: c.identity_set(label="MPS3-02", ip="192.168.11.101/24")),
        pyverify_sends(lambda c: c.identity_set(clear=True))]


def test_hms_locate_requests_are_the_ones_pyverify_sends(linux, sent):
    fake, session = linux()
    session.panel.locate(5, "david@lab via Harness Manager")
    session.panel.locate(0, "david@lab via Harness Manager")        # stop: no `who`
    assert ops(sent, "locate") == [
        pyverify_sends(lambda c: c.locate(5, "david@lab via Harness Manager")),
        pyverify_sends(lambda c: c.locate(0))]
    assert fake.locates == [(5, "david@lab via Harness Manager"), (0, "")]


# --- 2. HM on the vendored identity model, end to end ---------------------------------------------


def test_hm_reads_and_changes_the_identity_the_vendored_model_keeps(linux):
    s0 = {"label": "MPS3-01", "ip": "192.168.10.101/24", "mac": None}   # board 1's bake
    fake, session = linux(identity={"stage0": s0,
                                    "running": pvfs._IdentityModel.resolve(None, s0)})
    got = session.net_identity.read(refresh=True)
    assert (got["label"], got["ip"], got["hostname"]) == ("MPS3-01", "192.168.10.101/24",
                                                          "mps3-01")
    assert got["source"] == {"label": "stage0", "hostname": "label", "ip": "stage0",
                             "mac": "default"}
    assert got["via"] == "identity" and got["persist"] is True
    out = session.net_identity.set_identity({"label": "MPS3-02"})
    assert out["persisted"] and out["applies"] == "reboot" and out["setter"] == "harnessd"
    assert out["pending"] == {"label": "MPS3-02", "hostname": "mps3-02"}
    assert fake.identity.override == {"label": "MPS3-02"}


def test_twin_the_vendored_models_refusals_are_hms_errors(linux):
    fake, session = linux(identity={"persist": False})               # a netboot: no /persist
    with pytest.raises(RefusedError) as exc:
        session.net_identity.set_identity({"label": "MPS3-02"})
    assert NI.NETBOOT_WHY in exc.value.message and fake.identity.sets == []
    fake.identity.persist = True
    with pytest.raises(UsageError, match="invalid label: not"):
        session.net_identity.set_identity({"label": "mps3 two"})
    assert fake.identity.sets == [] and fake.identity.override is None


def test_twin_bare_metal_is_never_asked_for_an_identity_change(linux, sent):
    fake, session = linux(profile="bare-metal")
    assert "identity" not in fake.features
    with pytest.raises(UnavailableError):
        session.net_identity.set_identity({"label": "MPS3-02"})
    assert ops(sent, "identity_set") == []


# --- 3. HM's own identity model is the vendored one -----------------------------------------------

SAMPLES = {
    "label": ["MPS3", "MPS3-02", "", "mps3", "A" * 19, "A" * 20, "MPS3_02"],
    "hostname": ["mps3-02", "", "a.b.c", "-bad", "x" * 63, "x" * 64, "bad_host"],
    "ip": ["192.168.11.101", "192.168.11.101/24", "10.0.0.0/8", "10.255.255.255/8",
           "127.0.0.1", "224.0.0.1", "192.168.1.1/31", "1.2.3", "300.1.1.1", ""],
    "mac": ["0200000002fe", "02:00:00:00:02:FE", "02-00-00-00-02-fe", "010000000000",
            "000000000000", "02:00-00:00:02:fe", "zz0000000000"],
    "nokey": ["x"],
}


def test_hms_identity_model_has_the_vendored_rules_and_texts():
    for field, values in SAMPLES.items():
        for value in values:
            assert idn_board.check(field, value) == pvfs.identity_check(field, value), \
                (field, value)
    assert idn_board.LOCKED_ERR == pvfs.IDENTITY_LOCKED_ERR
    assert idn_board.NO_PERSIST_ERR == pvfs.IDENTITY_NO_PERSIST_ERR
    assert idn_board.NOT_SUPPORTED_ERR == pvfs.IDENTITY_NOT_SUPPORTED_ERR
    assert idn_board.DEFAULT == pvfs.IDENTITY_DEFAULTS


# --- 4. the claim's first key --------------------------------------------------------------------


def test_hms_fingerprint_of_its_key_is_the_boards_key_sha256(linux):
    from pyverify.identify import identify

    fake, session = linux(ssh_claimed=True)
    fake.authorized_keys = (KEY_LINE + "\n").encode()
    reply = identify(fake.host, fake.identify_port, timeout=2.0, retries=1)
    mine = fingerprint(parse_key_line(KEY_LINE)[1])
    assert reply.key_sha256 == mine
    obs = Mps3Claim._from_raw(reply.raw, "identify", 0.0)
    assert obs.key_fp == mine and obs.claimed is True


def test_twin_an_unclaimed_board_publishes_no_first_key(linux):
    from pyverify.identify import identify

    fake, session = linux()
    reply = identify(fake.host, fake.identify_port, timeout=2.0, retries=1)
    assert reply.key_sha256 == ""
    assert Mps3Claim._from_raw(reply.raw, "identify", 0.0).key_fp == ""


# --- 5. the names HM gates on ---------------------------------------------------------------------

#: Feature names HM tests for that the platform has shipped (HM also tests names it proposed
#: and the platform has not: panel, presence, sysmon, mcc, uart_baud, ...).
SHIPPED = ("slot", "xvc_lock", "usd", "identity", "locate", "lcd_mirror", "windowed", "stats",
           "reboot", "xvc_dbgbr", "clcd_kvm")
#: ... and the ones the Linux harness reports (the vendored linux profile).
ON_LINUX = ("slot", "xvc_lock", "usd", "identity", "locate", "lcd_mirror", "stats", "reboot")


def test_the_feature_names_hm_gates_on_are_the_vendored_ones():
    from harness_manager_mps3 import display, os_slots, xvc

    assert os_slots.SLOT_FEATURE == "slot" and NI.FEATURE == "identity"
    assert display.LCD_MIRROR_FEATURE == "lcd_mirror" and xvc.LOCK_FEATURE == "xvc_lock"
    known = set(pvfs.VERSION_FEATURES) | set(pvfs.ENGINE_FEATURES)
    assert set(SHIPPED) <= known, set(SHIPPED) - known
    assert set(ON_LINUX) <= set(pvfs.PROFILES["linux"]["features"])
    assert "windowed" not in pvfs.PROFILES["linux"]["features"]       # twin: never on Linux


def test_every_slot_code_the_vendored_harness_sends_is_one_hm_lists():
    shipped_verb = {code for _, code in pvfs.SLOT_VERB_CODES}
    shipped_job = {code for _, code in pvfs.SLOT_JOB_CODES}
    assert shipped_verb <= set(W.VERB_CODES), shipped_verb - set(W.VERB_CODES)
    assert shipped_job <= set(W.JOB_CODES) | set(W.VERB_CODES), \
        shipped_job - set(W.JOB_CODES) - set(W.VERB_CODES)
    # and the ones HM acts on mean what the board means
    assert pvfs.slot_code("slot locked: board claimed (use ssh)") == "locked"
    assert W.is_claim_lock("slot locked: board claimed (use ssh)", "locked")
    assert pvfs.slot_code("no slot record: static_id unknown", pvfs.SLOT_JOB_CODES) == "no_record"
