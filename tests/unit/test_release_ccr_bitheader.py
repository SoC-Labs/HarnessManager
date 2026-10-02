"""CCR-1 (lane RELEASE-PIPE, outside its ownership): the .bit header's UserID as Vivado
2026.1 writes it, ``UserID=FB1F8C76`` with no ``0X``.

Without it every RC2 (mint 3, 0x44EE76D5) bitstream reads as UNSTAMPED: the release tool
refuses to catalogue the v2.0.0 config SD, and the client's bundle check refuses to install
it (``bundle.check_sd_component``: "header USERID none"). The design string below is the
real one from config_rm_greybox_stage0_generic_b1.bit (the public v2.0.0 bake).
"""

from __future__ import annotations

import struct

from harness_manager.services.update.bitheader import MAGIC, build_bit, parse_bit_header

RC2_DESIGN = ("shell_top;UserID=FB1F8C76;COMPRESS=TRUE;Version=2026.1;SW_CRC=71e5cc68;"
              "SW_HDR_SHA256=c58e53387eeb7731f57e54d96e455339b222a8242cf12a12f486cc28323de701")


def _bit(design: str, data: bytes = b"\xff" * 64) -> bytes:
    def f(key: str, text: str) -> bytes:
        raw = text.encode() + b"\0"
        return key.encode() + struct.pack(">H", len(raw)) + raw

    return (struct.pack(">H", len(MAGIC)) + MAGIC + struct.pack(">H", 1) + f("a", design)
            + f("b", "xcku115-flvb1760-1-c") + f("c", "2026/09/30") + f("d", "19:58:55")
            + b"e" + struct.pack(">I", len(data)) + data)


def test_a_vivado_2026_1_userid_without_0x_reads_stamped():
    hdr = parse_bit_header(_bit(RC2_DESIGN))
    assert hdr.userid == "0xfb1f8c76" and hdr.stamped


def test_twin_the_2024_1_spelling_and_an_unstamped_bit_read_as_before():
    assert parse_bit_header(build_bit("d", "xcku115", b"\0" * 8, userid="0xD46FCDCB")
                            ).userid == "0xd46fcdcb"
    unstamped = parse_bit_header(_bit("shell_top;UserID=0XFFFFFFFF;Version=2024.1"))
    assert unstamped.userid == "0xffffffff" and not unstamped.stamped
    assert parse_bit_header(_bit("shell_top;COMPRESS=TRUE;Version=2026.1")).userid == ""
