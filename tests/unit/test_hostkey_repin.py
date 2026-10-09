# ruff: noqa: F811
"""HOSTKEY: a changed SSH host key is refused with ONE clear message on every SSH path, the
re-pin is an explicit act that pins exactly the key the user approved, and the one opt-in
exception (a netboot board, ``boards.<b>.ssh.netboot_new_key``) is off unless set.

Same rig as test_linux_claim (pyverify FakeShell + a fake board ssh); identify carries a
``boot_id`` here (``boot()``). Every check has a twin. Nothing reaches a real host.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
import pyverify.testing.fakeshell as fakeshell

from harness_manager.core.errors import AlreadyError, HeldError, RefusedError, UsageError
from harness_manager.services.claim import ClaimService
from harness_manager_mps3 import claim as CL
from harness_manager_mps3 import tunnel as tunmod
from harness_manager_mps3.openocd import Mps3OnBoard
from tests.fakes.lc_fake_board_ssh import make_key_line
from tests.integration.test_lm2_display_mps3 import (
    rig_factory as rig_factory_lm2,  # noqa: E402,F401
)
from tests.unit.test_linux_claim import (  # noqa: F401  (rig_factory is a fixture)
    BOARD_KEY,
    OTHER_BOARD_KEY,
    Leases,
    boards_toml,
    claim_it,
    rig_factory,
)

THIRD_KEY = make_key_line("a-third-key")
FP_OLD = CL.fingerprint(BOARD_KEY.split()[1])
FP_NEW = CL.fingerprint(OTHER_BOARD_KEY.split()[1])
FP_THIRD = CL.fingerprint(THIRD_KEY.split()[1])


@pytest.fixture(autouse=True)
def boot_ids(monkeypatch):
    """identify carries ``boot_id`` from ``shell.boot_id`` (pyverify's FakeShell has none)."""
    real = fakeshell._identify_reply

    def reply(self: Any, nonce: str) -> dict[str, Any]:
        r = real(self, nonce)
        bid = getattr(self, "boot_id", "")
        if bid and "ssh" in r:
            r["boot_id"] = bid
        return r

    monkeypatch.setattr(fakeshell, "_identify_reply", reply)


def boot(rig: Any, boot_id: str) -> None:
    rig.shell.boot_id = boot_id


def reimage(rig: Any, key: str = OTHER_BOARD_KEY, *, offered: str | None = None) -> None:
    """The board now has ``key``: identify says so, and ssh offers ``offered`` (default: it)."""
    rig.shell.ssh_host_key_sha256 = CL.fingerprint(key.split()[1])
    rig.ssh.host_key_line = offered or key


def pinned_line(rig: Any) -> str:
    text = boards_toml(rig.state.parent).read_text()
    return next(ln for ln in text.splitlines() if ln.strip().startswith("host_key"))


def claimed_rig(rig_factory: Any, **kw: Any) -> Any:
    rig = rig_factory(**kw)
    boot(rig, "boot-A")
    claim_it(rig)
    return rig


# --- 1. one refusal, on every ssh path --------------------------------------------------------------


def check_words(err: Exception, *, new: str = FP_NEW) -> None:
    text = str(err)
    assert FP_OLD in text and "pinned on 20" in text                  # old key and its pin date
    assert new in text                                                # the key the board shows now
    assert "boot boot-B" in text                                      # its boot id
    assert f"board repin TARGET --fingerprint {new}" in text          # the one fix
    assert "status 255" not in text and "Host key verification failed" not in text


def test_board_ssh_refuses_a_changed_key_with_the_one_message(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig)
    with pytest.raises(CL.HostKeyChangedError) as exc:
        rig.claim.ssh_argv(["true"])
    check_words(exc.value)
    assert "Refusing to connect" in str(exc.value) and "uptime" not in str(exc.value)


def test_twin_board_ssh_with_the_pinned_key_is_not_refused(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")                                  # rebooted, same key (a card with /persist)
    assert rig.claim.ssh_argv(["true"])[-1] == "true"


def test_the_debug_launcher_refuses_a_changed_key_with_the_same_words(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig)
    rig.claim.observe(refresh=True)                      # identify shows it; ssh would too
    route = Mps3OnBoard(rig.session)
    with pytest.raises(CL.HostKeyChangedError) as exc:
        route.run("status")
    check_words(exc.value)
    assert rig.ssh.calls == [rig.ssh.calls[0]]           # only the claim's capture, no new ssh


def test_the_debug_launcher_maps_sshs_own_255_when_identify_has_not_shown_it(rig_factory):
    rig = claimed_rig(rig_factory)
    rig.session.claim.observe(refresh=True)              # cached: identify still shows the pin
    rig.ssh.host_key_line = OTHER_BOARD_KEY              # but ssh is offered another key
    route = Mps3OnBoard(rig.session)
    with pytest.raises(CL.HostKeyChangedError) as exc:
        route.run("status")
    text = str(exc.value)
    assert FP_OLD in text and "pinned on 20" in text and "status 255" not in text
    assert "board repin TARGET" in text and "--fingerprint" not in text   # key unknown to identify


def test_twin_the_debug_launcher_with_the_pinned_key_runs(rig_factory):
    rig = claimed_rig(rig_factory)
    res = Mps3OnBoard(rig.session).run("status")
    assert res.returncode == 0


class _DeadSsh:
    """An ssh that exits 255 at once with ``stderr`` (a tunnel launcher's process)."""

    pid = 4242

    def __init__(self, stderr: str) -> None:
        self.stderr_tail = stderr
        self.open_failures: list = []

    def poll(self) -> int:
        return 255

    def terminate(self) -> None: ...
    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int:
        return 255


def test_the_lcd_mirror_forward_refuses_a_changed_key_with_the_same_words(rig_factory, monkeypatch):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig)
    rig.claim.observe(refresh=True)
    monkeypatch.setattr(tunmod, "DEFAULT_LAUNCHER", lambda argv: _DeadSsh(
        "Host key verification failed.\r\n"))
    monkeypatch.setattr(tunmod, "DEFAULT_SSH_G", lambda argv: "")
    # identify shows it: refused before ssh is even started
    with pytest.raises(CL.HostKeyChangedError) as exc:
        rig.claim.open_forward({"lcd_mirror": 6940}, label="lcd_mirror")
    check_words(exc.value)


def test_the_lcd_mirror_forward_maps_sshs_refusal_and_the_tunnel_says_it_plainly(
        rig_factory, monkeypatch):
    rig = claimed_rig(rig_factory)
    rig.claim.observe(refresh=True)                      # identify: still the pin
    monkeypatch.setattr(tunmod, "DEFAULT_LAUNCHER", lambda argv: _DeadSsh(
        "Host key verification failed.\r\n"))
    monkeypatch.setattr(tunmod, "DEFAULT_SSH_G", lambda argv: "")
    seen: list[str] = []
    real = tunmod.SshTunnel._set

    def spy(self: Any, state: str, detail: str) -> None:
        real(self, state, detail)
        seen.append(self.detail)

    monkeypatch.setattr(tunmod.SshTunnel, "_set", spy)
    with pytest.raises(CL.HostKeyChangedError) as exc:
        rig.claim.open_forward({"lcd_mirror": 6940}, label="lcd_mirror")
    assert FP_OLD in str(exc.value) and "status 255" not in str(exc.value)
    down = [d for d in seen if "SSH HOST KEY CHANGED" in d]
    assert down and all("status 255" not in d for d in down)     # the log/state detail too


def test_twin_the_tunnel_words_leave_other_failures_alone(rig_factory):
    rig = claimed_rig(rig_factory)
    assert rig.claim.tunnel_words("ssh exited with status 255 (Connection refused)") == ""
    assert "pinned on 20" in rig.claim.tunnel_words(
        "ssh exited with status 255 (Host key verification failed.)")


def test_the_status_carries_the_refusal_and_both_fingerprints(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig)
    hk = rig.claim.claim_status(refresh=True)["host_key"]
    assert hk["match"] is False and hk["pinned"] == FP_OLD and hk["reported"] == FP_NEW
    assert hk["pinned_at"].endswith("Z") and hk["boot_id"] == "boot-B"
    assert hk["netboot_new_key"] is False
    assert f"--fingerprint {FP_NEW}" in hk["refusal"]["hint"]


# --- 2. the re-pin pins exactly the approved key ---------------------------------------------------------


def test_repin_pins_exactly_the_approved_fingerprint(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig)
    before = pinned_line(rig)
    st = ClaimService(None, leases=Leases()).repin(rig.session, confirm=True, fingerprint=FP_NEW)
    assert st["action"] == "repinned" and st["host_key"]["match"] is True
    assert st["host_key"]["pinned"] == FP_NEW and pinned_line(rig) != before
    assert OTHER_BOARD_KEY in pinned_line(rig)
    rec = rig.claim._records.get(rig.claim.board_id)
    assert rec["host_key_fp"] == FP_NEW and rec["how"] == "repin" and rec["pin_boot_id"] == "boot-B"
    assert FP_OLD in [e["fp"] for e in rec["host_keys_seen"]]       # the old pin is remembered
    assert rig.claim.ssh_argv(["true"])                              # and ssh works again


def test_repin_refuses_a_key_other_than_the_one_approved(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig)
    before = pinned_line(rig)
    with pytest.raises(CL.HostKeyChangedError, match="not the one you approved"):
        rig.claim.repin(FP_THIRD)                       # the user approved X, the board shows Y
    assert pinned_line(rig) == before


def test_repin_refuses_when_ssh_presents_another_key_than_the_approved_one(rig_factory):
    rig = claimed_rig(rig_factory)
    boot(rig, "boot-B")
    reimage(rig, OTHER_BOARD_KEY, offered=THIRD_KEY)    # identify says NEW, ssh offers THIRD
    before = pinned_line(rig)
    with pytest.raises(CL.HostKeyChangedError, match="NOT pinned"):
        rig.claim.repin(FP_NEW)
    assert pinned_line(rig) == before


def test_repin_refuses_when_the_key_changes_again_before_it_pins(rig_factory):
    rig = claimed_rig(rig_factory)
    reimage(rig)
    rig.claim.observe(refresh=True)
    reimage(rig, THIRD_KEY)                             # the board changed AGAIN after approval
    before = pinned_line(rig)
    with pytest.raises(CL.HostKeyChangedError, match="not the one you approved"):
        rig.claim.repin(FP_NEW)
    assert pinned_line(rig) == before


def test_repin_of_the_key_already_pinned_is_already(rig_factory):
    rig = claimed_rig(rig_factory)
    with pytest.raises(AlreadyError):
        rig.claim.repin(FP_OLD)


def test_repin_needs_a_fingerprint_a_confirmation_and_the_lease(rig_factory):
    rig = claimed_rig(rig_factory)
    reimage(rig)
    svc = ClaimService(None, leases=Leases())
    before = pinned_line(rig)
    with pytest.raises(RefusedError, match="confirmation"):
        svc.repin(rig.session, confirm=False, fingerprint=FP_NEW)
    with pytest.raises(RefusedError, match="exact fingerprint"):
        svc.repin(rig.session, confirm=True, fingerprint="")
    with pytest.raises(UsageError, match="not a key fingerprint"):
        rig.claim.repin("whatever")
    rig.session.hub = object()
    with pytest.raises(HeldError, match="lease holder only"):
        ClaimService(None, leases=Leases(mine=False, holder="bob")).repin(
            rig.session, confirm=True, fingerprint=FP_NEW)
    assert pinned_line(rig) == before
    assert svc.repin(rig.session, confirm=True, fingerprint=FP_NEW)["action"] == "repinned"


# --- 3. the netboot opt-in ----------------------------------------------------------------------------------


def netboot_rig(rig_factory: Any, *, on: bool, new_boot: str = "boot-B") -> Any:
    rig = claimed_rig(rig_factory)
    if on:
        CL.write_ssh_settings(rig.session.candidate, {"netboot_new_key": True})
    boot(rig, new_boot)
    reimage(rig)
    return rig


def test_netboot_off_never_accepts_even_with_a_new_boot_id_and_a_matching_key(rig_factory):
    rig = netboot_rig(rig_factory, on=False)
    before = pinned_line(rig)
    assert rig.claim.config()["netboot_new_key"] == ""
    with pytest.raises(CL.HostKeyChangedError):
        rig.claim.ssh_argv(["true"])
    assert pinned_line(rig) == before


def test_netboot_on_with_a_new_boot_id_and_a_matching_key_is_accepted_and_logged(
        rig_factory, caplog):
    rig = netboot_rig(rig_factory, on=True)
    events: list[tuple[str, str, dict]] = []
    CL.EVENT_SINK = lambda topic, bid, data: events.append((topic, bid, data))
    try:
        with caplog.at_level(logging.WARNING, logger=CL.log.name):
            argv = rig.claim.ssh_argv(["true"])
    finally:
        CL.EVENT_SINK = None
    assert argv[-1] == "true" and OTHER_BOARD_KEY in pinned_line(rig)
    assert any("accepted by itself" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    (topic, bid, data), = events
    assert topic == "board.hostkey" and bid == rig.claim.board_id
    assert data["old"] == FP_OLD and data["new"] == FP_NEW and data["boot_id"] == "boot-B"
    assert data["previous_boot_id"] == "boot-A" and FP_NEW in data["text"]
    # and it is written down: the next reboot's key is judged against THIS boot
    assert rig.claim._records.get(rig.claim.board_id)["pin_boot_id"] == "boot-B"
    assert rig.claim._records.get(rig.claim.board_id)["how"] == "netboot-auto"


def test_netboot_on_with_the_same_boot_id_is_refused(rig_factory):
    rig = netboot_rig(rig_factory, on=True, new_boot="boot-A")
    before = pinned_line(rig)
    with pytest.raises(CL.HostKeyChangedError, match="has not rebooted since the key was pinned"):
        rig.claim.ssh_argv(["true"])
    assert pinned_line(rig) == before


def test_netboot_on_refuses_when_ssh_presents_another_key_than_identify(rig_factory):
    rig = netboot_rig(rig_factory, on=True)
    rig.ssh.host_key_line = THIRD_KEY                   # something else answers ssh
    before = pinned_line(rig)
    with pytest.raises(CL.HostKeyChangedError, match="not checked"):
        rig.claim.ssh_argv(["true"])
    assert pinned_line(rig) == before


def test_netboot_on_refuses_without_a_boot_id_or_without_a_recorded_one(rig_factory):
    rig = netboot_rig(rig_factory, on=True, new_boot="")
    with pytest.raises(CL.HostKeyChangedError, match="publishes no boot id"):
        rig.claim.ssh_argv(["true"])
    boot(rig, "boot-B")
    rig.claim._records.update(rig.claim.board_id, pin_boot_id=None)
    with pytest.raises(CL.HostKeyChangedError, match="no boot id was recorded"):
        rig.claim.ssh_argv(["true"])


def test_the_netboot_setting_is_off_by_default_and_checked(rig_factory):
    rig = claimed_rig(rig_factory)
    assert rig.claim.netboot_enabled() is False
    CL.write_ssh_settings(rig.session.candidate, {"netboot_new_key": True})
    assert rig.claim.netboot_enabled() is True
    from harness_manager_mps3.settings import _rows

    row = next(r for r in _rows() if r.key == "boards.*.ssh.netboot_new_key")
    assert row.default is False and "new boot id" in row.doc


def test_concurrent_netboot_connects_pin_once(rig_factory):
    rig = netboot_rig(rig_factory, on=True)
    events: list[Any] = []
    CL.EVENT_SINK = lambda *a: events.append(a)
    errors: list[Exception] = []

    def go() -> None:
        try:
            rig.claim.ssh_argv(["true"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        CL.EVENT_SINK = None
    assert not errors and len(events) == 1


# --- 4. the live display says it in the same words ----------------------------------------------------------


def test_the_live_display_reason_is_the_one_refusal(rig_factory_lm2: Any) -> None:
    other = "SHA256:" + "A" * 43
    rig = rig_factory_lm2(reported_key=other)
    got = rig.adapter.display_reason()
    assert got.startswith("THE BOARD'S SSH HOST KEY CHANGED: pinned ")
    assert other in got and f"board repin TARGET --fingerprint {other}" not in got   # message only
    assert "SSH to this board is refused" in got
    assert rig_factory_lm2().adapter.display_reason() == ""             # twin: key matches


# --- WIZARD-FIT: "Replace the pinned key" = claim --replace-host-key, pinning exactly the key shown ------------


def reprovisioned(rig: Any) -> None:
    rig.shell.ssh_claimed, rig.shell.authorized_keys = False, None        # a new card
    reimage(rig)


def test_replace_pins_exactly_the_key_that_was_shown(rig_factory):
    rig = claimed_rig(rig_factory)
    reprovisioned(rig)
    st = claim_it(rig, replace_host_key=True, expect_host_key=FP_NEW)
    assert st["state"] == "mine" and st["host_key"]["pinned"] == FP_NEW


def test_twin_replace_refuses_when_the_board_shows_another_key_than_the_shown_one(rig_factory):
    rig = claimed_rig(rig_factory)
    reprovisioned(rig)
    with pytest.raises(CL.HostKeyChangedError, match="you approved " + FP_THIRD):
        claim_it(rig, replace_host_key=True, expect_host_key=FP_THIRD)
    assert rig.shell.authorized_keys is None                  # nothing sent
    assert rig.claim.claim_status()["host_key"]["pinned"] == FP_OLD


def test_twin_without_replace_a_changed_key_is_still_refused(rig_factory):
    rig = claimed_rig(rig_factory)
    reprovisioned(rig)
    with pytest.raises(CL.HostKeyChangedError):
        claim_it(rig, expect_host_key=FP_NEW)
    assert rig.shell.authorized_keys is None
