"""FIX-PACK-2 item 5: what the MCC's ``CFG R TEMP 0`` measures, said on every reading.

The sensor is the SLR0 die's temperature diode, read through U53 (MPS3 schematic p4); it is
not the ambient. Twin: a reading the MCC did not give carries why, never the sensor caveat.
"""

from __future__ import annotations

from harness_manager_mps3 import hub_mcc, mcc
from tests.unit.test_t3_mcc import SilentPort, make

WANT = "the SLR0 die diode via U53 (MPS3 schematic p4); not ambient"


def test_the_temperature_names_the_slr0_die_diode_via_u53_not_ambient():
    assert mcc.TEMP_CAVEAT == WANT
    assert hub_mcc.TEMP_CAVEAT is mcc.TEMP_CAVEAT        # the hub's reading says the same
    ctl, *_ = make()
    (temp,) = ctl.temperatures()
    assert temp.available and temp.value == 35.5
    assert temp.reason == WANT
    assert "IOFPGA" not in temp.reason and "unverified" not in temp.reason


def test_negative_twin_a_missing_temperature_carries_its_failure_not_the_caveat():
    ctl, *_ = make(SilentPort())
    (temp,) = ctl.temperatures()
    assert not temp.available and temp.value is None
    assert "no MCC prompt" in temp.reason and "U53" not in temp.reason
