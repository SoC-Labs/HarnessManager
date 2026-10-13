"""W0-A: the virtual boards for the v2.1 Linux harness and mint 4 (net-protocol v0.20)."""

from __future__ import annotations

import pytest
from pyverify.client import ShellClient

from tests.fakes.virtual_board import (
    LINUX_HARNESSD,
    LINUX_HARNESSD_V1,
    LINUX_MINT4,
    VirtualMps3,
    linux_mint4_profile,
)

MINT4 = ("hostio", "cosim", "sysmon", "mccif", "mcc_local")


def _usd(vb):
    vb.shell.usd_insert("da")
    with ShellClient(vb.shell.host, vb.shell.control_port) as c:
        return c.usd(), c.version()


def test_linux_v21_has_the_current_features_and_a_real_static(tmp_path):
    assert LINUX_HARNESSD.harness_version == "2.1.0"
    assert LINUX_HARNESSD.static_id == 0x44EE76D5 and LINUX_HARNESSD.static_id_final
    assert {"usd", "slot", "panel", "identity"} <= set(LINUX_HARNESSD.features)
    assert not set(MINT4) & set(LINUX_HARNESSD.features)
    assert LINUX_HARNESSD_V1.harness_version == "1.0.0"


def test_mint4_board_reports_the_mint4_names_and_32_mib_slots(tmp_path):
    with VirtualMps3(tmp_path, LINUX_MINT4) as vb:
        usd, ver = _usd(vb)
    assert set(MINT4) <= set(ver.features)
    assert (usd.slot_mb, usd.store_ver) == (32, 2)


def test_old_card_reports_8_mib_slots_store_ver_1(tmp_path):
    with VirtualMps3(tmp_path, linux_mint4_profile(usd_slot_mib=8)) as vb:
        usd, _ = _usd(vb)
    assert (usd.slot_mb, usd.store_ver) == (8, 1)


def test_old_shell_reports_neither(tmp_path):
    with VirtualMps3(tmp_path, linux_mint4_profile(usd_report_store_ver=False)) as vb:
        usd, _ = _usd(vb)
    assert usd.slot_mb is None and usd.store_ver is None


def test_v21_board_without_mint4_has_no_store_ver(tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        usd, ver = _usd(vb)
    assert usd.slot_mb is None and usd.store_ver is None
    assert not set(MINT4) & set(ver.features)


@pytest.mark.parametrize("bad", [16, 0])
def test_slot_mib_other_than_8_or_32_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        VirtualMps3(tmp_path, linux_mint4_profile(usd_slot_mib=bad)).__enter__()
