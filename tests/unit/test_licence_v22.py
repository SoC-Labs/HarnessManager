"""The v2.2 static's IP licence status was measured (report_ip_status, Vivado 2026.1, 18 Oct:
all 125 IP "Included"), so the kit's licence line says it needs none. Twin: a static nobody
measured still says unchecked."""

from harness_manager.services.kit.licence import needs


def test_the_v22_static_s_ip_needs_no_licence():
    assert "the static's IP needs none" in needs("xcku115-flvb1760-1-c", "2026.1", "0xA5856D30")


def test_twin_an_unmeasured_static_is_still_unchecked():
    assert "were not checked" in needs("xcku115-flvb1760-1-c", "2026.1", "0x12345678")
