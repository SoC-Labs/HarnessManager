"""The board-free partial validator (``harness_manager_mps3.bitcheck``, KIT-GUIDE KG-B).

Moved from the spike (``tools/spike_kit_guide/test_partial_check.py``). The streams are
synthetic, built from the packet rules only with the shapes measured on the real partials
(tests/fakes/kit_fakes.py). The golden files are those streams: their bytes are pinned by
sha256 in tests/fixtures/kit/bitstreams.sha256 (``*.bit``/``*.bin`` never go in git, so the
tests regenerate them), and each has its verdict pinned below. The load-bearing case is
``test_frame_data_is_skipped``: FDRI payload stuffed with words that LOOK like headers
(the sync word, an AXSS write, IPROG) must be skipped by count, never parsed.

Every refusal has a twin that passes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from harness_manager_mps3 import bitcheck as bc
from tests.fakes import kit_fakes as kf

PINS = Path(__file__).resolve().parents[1] / "fixtures" / "kit" / "bitstreams.sha256"
REF = kf.FRAMES_72BB0A36


@pytest.fixture(scope="module")
def golden(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("bitstreams")
    for name, data in kf.golden_bitstreams().items():
        (d / name).write_bytes(data)
    return d


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def states(v: bc.Verdict) -> dict[str, str]:
    return v.states()


@pytest.fixture
def g(golden: Path):
    return lambda name: golden / name


# --- the goldens -----------------------------------------------------------------------------------


def test_the_goldens_are_the_pinned_bytes():
    pins = dict(reversed(line.split("  ", 1)) for line in PINS.read_text().splitlines()
                if line and not line.startswith("#"))
    made = {n: hashlib.sha256(b).hexdigest() for n, b in kf.golden_bitstreams().items()}
    assert made == pins


def test_golden_good_pair_passes_against_the_fielded_statics_frames(g):
    v, facts = bc.check(g("good_partial.bit"), clearing=g("good_partial_clear.bin"),
                        bin_path=g("good_partial.bin"), ref=REF)
    assert not v.refused, v.items
    s = states(v)
    assert s["partial: frame_box"] == "ok" and s["clearing: frame_box"] == "ok"
    assert s["partial: vocabulary"] == "ok" and s["bit_bin_pair"] == "ok"
    assert s["static_binding"] == "unchecked"          # the honest limit (N7)
    assert facts["partial"]["header"]["tokens"]["Version"] == "2024.1"
    assert "_fars" not in facts["partial"]


@pytest.mark.parametrize("name, check", [
    ("full_image.bit", "partial: partial_flag"),            # N1: a full image as a partial
    ("full_image.bit", "partial: device_global_writes"),    # ... it writes AXSS and IPROG
    ("truncated_partial.bin", "partial: stream"),           # N3: half-copied
    ("wrong_part.bit", "partial: part"),                    # N6: another device's bitstream
    ("other_device_partial.bin", "partial: idcode"),        # another device's IDCODE
    ("other_partition_partial.bin", "partial: frame_box"),  # another partition's frames
])
def test_golden_refusals(g, name: str, check: str):
    v, _ = bc.check(g(name), ref=REF)
    assert v.refused
    assert states(v)[check] == "mismatch", v.items


def test_swapped_pair_is_refused_and_its_twin_passes(g):               # N2
    v, _ = bc.check(g("good_partial_clear.bin"), clearing=g("good_partial.bin"), ref=REF)
    s = states(v)
    assert v.refused
    assert s["partial: role"] == "mismatch" and s["clearing: role"] == "mismatch"
    ok, _ = bc.check(g("good_partial.bin"), clearing=g("good_partial_clear.bin"), ref=REF)
    assert not ok.refused


def test_without_a_reference_the_frame_box_is_unchecked_not_passed(g):
    v, _ = bc.check(g("other_partition_partial.bin"))
    s = states(v)
    assert s["partial: frame_box"] == "unchecked"
    assert not v.refused                                # unchecked never refuses...
    assert bc.check(g("other_partition_partial.bin"), ref=REF)[0].refused   # ...a reference does


def test_frames_of_pair_computes_a_kits_rp_frames(g):
    f = bc.frames_of_pair(g("good_partial.bin"), g("good_partial_clear.bin"))
    assert f["idcode"] == "0x0390D093"
    assert f["partial"]["by_block"]["0"] == {"rows": [0, 1], "columns": [100, 150]}
    assert "GRESTORE" in f["partial"]["cmds"] and "AGHIGH" in f["clearing"]["cmds"]
    # a candidate checked against those frames passes; one outside them does not
    assert not bc.check(g("good_partial.bin"), ref=f)[0].refused
    assert bc.check(g("other_partition_partial.bin"), ref=f)[0].refused


# --- the spike's synthetic cases ---------------------------------------------------------------------


def test_partial_and_clearing_pass(tmp_path):
    part = write(tmp_path, "p.bin", kf.stream())
    clr = write(tmp_path, "c.bin", kf.clearing_stream())
    v, facts = bc.check(part, clearing=clr, ref=bc.frames_of_pair(part, clr))
    assert not v.refused, v.items
    s = states(v)
    assert s["partial: role"] == "ok" and s["clearing: role"] == "ok"
    assert facts["partial"]["frames"]["by_block"]["0"] == {"rows": [0, 1], "columns": [100, 150]}


def test_frame_data_is_skipped(tmp_path):
    poison = [bc.SYNC, (1 << 29) | (2 << 27) | (0x0D << 13) | 1, 0xDEADBEEF,
              *kf.t1(kf.REG["CMD"], [kf.CMD["IPROG"]])] * 20
    part = write(tmp_path, "p.bin", kf.stream(fdri=poison))
    v, facts = bc.check(part)
    s = states(v)
    assert s["partial: stream"] == "ok"
    assert s["partial: device_global_writes"] == "ok", "frame data was parsed as packets"
    assert facts["partial"]["sections"][0]["axss"] == []


def test_other_slr_and_device_global_writes_refused(tmp_path):
    part = write(tmp_path, "p.bin", kf.stream(idcode=kf.SLR1,
                                              extra=kf.t1(kf.REG["AXSS"], [0x01000001])))
    v, _ = bc.check(part)
    s = states(v)
    assert s["partial: idcode"] == "ok"                 # SLR1 is still the KU115...
    assert s["partial: device_global_writes"] == "mismatch"   # ...but AXSS is never a partial's
    # ...and with the kit's frames, SLR1 is not the partition's SLR
    assert states(bc.check(part, ref=REF)[0])["partial: idcode"] == "mismatch"


def test_bit_bin_pair_mismatch_refused(tmp_path):
    payload = kf.stream()
    bit = write(tmp_path, "p.bit", kf.bit_file(payload))
    bad = write(tmp_path, "q.bin", payload[:-4] + b"\x00\x00\x00\x01")
    assert states(bc.check(bit, bin_path=bad)[0])["bit_bin_pair"] == "mismatch"
    good = write(tmp_path, "p.bin", payload)
    assert states(bc.check(bit, bin_path=good)[0])["bit_bin_pair"] == "ok"


def test_clearing_over_arena_refused(tmp_path):
    part = write(tmp_path, "p.bin", kf.stream())
    clr = write(tmp_path, "c.bin", kf.clearing_stream(fars=((0, 0, 110),)))
    assert states(bc.check(part, clearing=clr, clearing_max=64)[0])["clearing_fits"] == "mismatch"
    assert states(bc.check(part, clearing=clr)[0])["clearing_fits"] == "ok"


def test_a_cut_bit_header_is_refused_not_crashed(tmp_path):
    bit = kf.bit_file(kf.stream())
    cut = write(tmp_path, "cut.bit", bit[:40])
    v, _ = bc.check(cut)
    assert v.refused
    assert states(v)["partial: stream"] == "mismatch"
