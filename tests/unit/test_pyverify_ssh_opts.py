"""PYVERIFY-VENDOR: the hub ssh options Harness Manager inherits from the vendored pyverify.

HM's hub calls start from pyverify's argv (``pyverify.lease.SshHubRunner.build`` ->
``["ssh", *SSH_OPTS, hub, remote]``). The service's ssh multiplexing (``hub.with_mux``, lane
LEASE-FRESH) replaces the Control* options with its own and keeps every other one, and it
relies on ``ClearAllForwardings=yes`` staying there: david's ``~/.ssh/config`` has
LocalForwards for the hub (18081 at least), which a long-lived master would otherwise bind.

At platform 3f7cea2 (as at 3bfda65: lease.py, linux.py and slot.py are unchanged)
``SSH_OPTS`` is ``-o ControlPath=none -o BatchMode=yes -o ClearAllForwardings=yes``: no
ControlMaster/ControlPersist. A re-vendor that drops ``ClearAllForwardings`` or brings a
master of pyverify's own fails here, before it reaches a hub.
"""

from __future__ import annotations

from pyverify import lease as pv_lease


def options(argv) -> list[str]:
    """The ``-o`` values of an ssh argv (or an option tuple), in order."""
    argv = list(argv)
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-o"]


def control(opts: list[str]) -> list[str]:
    return [o for o in opts if o.lower().startswith("control")]


def test_pyverifys_hub_ssh_options_keep_clear_all_forwardings():
    opts = options(pv_lease.SSH_OPTS)
    assert "ClearAllForwardings=yes" in opts
    assert "BatchMode=yes" in opts
    # the only Control* option is the one with_mux replaces: pyverify runs no master itself
    assert control(opts) == ["ControlPath=none"]


def test_the_argv_harness_manager_starts_from_carries_them_before_the_hub():
    argv = pv_lease.SshHubRunner("hub.example").build(["target", "show", "mps3_01_pl"])
    assert argv[0] == "ssh" and argv[-2] == "hub.example"
    assert argv[1:1 + len(pv_lease.SSH_OPTS)] == list(pv_lease.SSH_OPTS)
    assert "ClearAllForwardings=yes" in options(argv[:-2])


def test_twin_the_checks_see_a_dropped_option_and_a_master():
    dropped = ("-o", "ControlPath=none", "-o", "BatchMode=yes")
    assert "ClearAllForwardings=yes" not in options(dropped)
    mastered = (*pv_lease.SSH_OPTS, "-o", "ControlMaster=auto", "-o", "ControlPersist=600")
    assert control(options(mastered)) != ["ControlPath=none"]
