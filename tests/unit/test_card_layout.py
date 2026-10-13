"""Lane L1 CARD-MBR: ``services/card_layout.py`` and the card writer reading the card's MBR."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services import card_layout as cl
from harness_manager.services import cardwriter as cw
from tests.fakes.cardwriter_fakes import slot_image

MIB = 2048
TOTAL = 1024 * MIB          # a 1 GiB card, in sectors


def _mbr(entries):
    sec = bytearray(512)
    for i, (typ, lba, n) in enumerate(entries):
        struct.pack_into("<B3sB3sII", sec, 446 + 16 * i, 0, b"\xfe\xff\xff", typ, b"\xfe\xff\xff",
                         lba, n)
    sec[510], sec[511] = 0x55, 0xAA
    return bytes(sec)


def test_layout_matches_stage0_mkcard_tables():
    assert cl.layout(32) == (65536, 67584, 198656, 329728)
    assert cl.layout(96) == (196608, 198656, 329728, 460800)
    assert cl.layout() == cl.layout(96)
    with pytest.raises(cl.CardLayoutError):
        cl.layout(64)


@pytest.mark.parametrize(("mib", "version", "a", "b", "p", "n"),
                         [(32, 1, 67584, 198656, 329728, 65536),
                          (96, 2, 198656, 329728, 460800, 196608)])
def test_build_mbr_round_trips(mib, version, a, b, p, n):
    lay = cl.parse_mbr(cl.build_mbr(mib, TOTAL))
    assert lay.version == version and lay.name == f"v{version}"
    assert (lay.slot_a.start, lay.slot_b.start, lay.persist.start) == (a, b, p)
    assert lay.persist.end == TOTAL
    assert (lay.store.start, lay.store.length) == (2048, n)
    assert lay.slot_a.length == lay.slot_b.length == 131072 and lay.slot_a.mib == 64
    assert lay.as_dict()["layout"] == f"v{version}"


def test_default_new_card_is_v2():
    assert cl.parse_mbr(cl.build_mbr()).version == 2


def test_entries_not_disk_order_decide():
    # v2 table in the contract order has slot A (198656) AFTER the store on disk; a table with
    # the entries swapped means A and B swap with them: the entry order is the truth.
    n_store, a, b, p = cl.layout(96)
    lay = cl.parse_mbr(_mbr([(0x7F, b, 131072), (0x7F, a, 131072), (0x83, p, 1000),
                             (0xDA, 2048, n_store)]))
    assert lay.slot_a.start == b and lay.slot_b.start == a
    assert lay.version == 0                    # not where either layout puts A and B


def test_store_in_entry_one_is_missing_slot():
    n_store, a, b, p = cl.layout(32)
    with pytest.raises(cl.CardLayoutError, match="slot A is missing"):
        cl.parse_mbr(_mbr([(0xDA, 2048, n_store), (0x7F, a, 131072), (0x7F, b, 131072)]))


def test_missing_store_is_none_and_required_on_demand():
    _n, a, b, p = cl.layout(96)
    lay = cl.parse_mbr(_mbr([(0x7F, a, 131072), (0x7F, b, 131072), (0x83, p, 1000)]))
    assert lay.store is None
    with pytest.raises(cl.CardLayoutError, match="design store"):
        lay.require_store()


def test_errors_are_plain():
    with pytest.raises(cl.CardLayoutError, match="no harness partitions.*signature"):
        cl.parse_mbr(b"\0" * 512)
    with pytest.raises(cl.CardLayoutError, match="slot B is missing"):
        cl.parse_mbr(_mbr([(0x7F, 8, 100)]))
    with pytest.raises(cl.CardLayoutError, match="slot A and slot B overlap"):
        cl.parse_mbr(_mbr([(0x7F, 8, 100), (0x7F, 50, 100)]))
    _n, a, b, _p = cl.layout(96)
    with pytest.raises(cl.CardLayoutError, match="the store overlap"):      # v1 store over v2 A
        cl.parse_mbr(_mbr([(0x7F, a, 131072), (0x7F, b, 131072), (0xDA, 2048, 300000)]))


def _card(path: Path, store_mib: int) -> Path:
    """A sparse whole-card image in the given layout with a bootable slot A and B."""
    _n, a, b, _p = cl.layout(store_mib)
    img = slot_image()
    with path.open("wb") as f:
        f.truncate(600 * 1024 * 1024)
        f.write(cl.build_mbr(store_mib, 600 * MIB))
        for lba in (a, b):
            f.seek(lba * 512)
            f.write(img)
    return path


@pytest.mark.parametrize(("mib", "version", "a", "b"), [(32, "v1", 67584, 198656),
                                                        (96, "v2", 198656, 329728)])
def test_cardwriter_uses_the_cards_own_slots(tmp_path, mib, version, a, b):
    image = cw.inspect_card(_card(tmp_path / f"c{mib}.img", mib))
    assert image.slots == ("A", "B")
    assert image.layout.name == version
    j = image.layout_json()
    assert j["layout"] == version and j["slot_a"]["start"] == a and j["slot_b"]["start"] == b
    assert j["slot_mib"] == 64 and j["store"]["start"] == 2048
    assert version in image.describe()
    assert not hasattr(cw, "LBA_A") and not hasattr(cw, "LBA_B")


def test_cardwriter_slot_read_follows_a_v2_table_not_v1(tmp_path):
    # S0LB at the v1 slot A place only: a v2 card whose slot A is empty must NOT pass.
    path = tmp_path / "wrong.img"
    with path.open("wb") as f:
        f.truncate(600 * 1024 * 1024)
        f.write(cl.build_mbr(96, 600 * MIB))
        f.seek(67584 * 512)
        f.write(slot_image())
    with pytest.raises(RefusedError, match="no bootable stage0 slot"):
        cw.inspect_card(path)


def test_card_without_harness_partitions_is_refused_plainly(tmp_path):
    path = tmp_path / "plain.img"
    path.write_bytes(_mbr([(0x0C, 8192, 100000)]) + b"\0" * 4096)
    with pytest.raises(RefusedError) as e:
        cw.inspect_card(path)
    assert "no harness partitions" in (getattr(e.value, "hint", "") or str(e.value))


def test_overlapping_table_is_refused(tmp_path):
    path = tmp_path / "ov.img"
    path.write_bytes(_mbr([(0x7F, 8, 100), (0x7F, 50, 100)]) + b"\0" * 4096)
    with pytest.raises(RefusedError, match="overlap"):
        cw.inspect_card(path)
