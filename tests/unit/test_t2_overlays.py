"""T2 unit tests: the MPS3 overlay catalogue. Each check has a negative twin.

``tests/fixtures/t2/fielded_manifests`` holds three manifests copied verbatim
from mps3-nanosoc-platform ``fpga/dfx/overlay/<rm>/manifest.json`` at 9a4058c
(2026-09-15, the fielded 0x3F1A560F mint). Their ``.bin`` payloads are not
copied (Arm IP, size, gitignored).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_manager.core.errors import AbsentError, RefusedError
from harness_manager.core.model import Check
from harness_manager.core.pack import OverlayRef
from harness_manager_mps3 import overlays as ov_mod
from harness_manager_mps3.overlays import (
    OVERLAY_DIRS_ENV,
    OverlayCatalogue,
    import_overlay,
    resolve_rm_name,
)
from tests.fakes.t2_overlays import (
    FIELDED_STATIC_ID,
    OTHER_STATIC_ID,
    SYNTH2_RM_ID,
    SYNTH_RM_ID,
    FakeStore,
    make_overlay,
    ref_named,
    use_overlay_dirs,
)

FIELDED = Path(__file__).resolve().parents[1] / "fixtures" / "t2" / "fielded_manifests"


@pytest.fixture(autouse=True)
def _no_ambient_overlay_dirs(monkeypatch):
    monkeypatch.delenv(OVERLAY_DIRS_ENV, raising=False)


# -- loading ------------------------------------------------------------------------


def test_root_dir_loads_every_overlay_as_a_ref(tmp_path):
    make_overlay(tmp_path, "synth", static_usercode=0xD46FCDCB, ip_class="open")
    cat = OverlayCatalogue([tmp_path])
    (ref,) = cat.refs()
    assert ref.name == "synth"
    assert ref.rm_id == "0x01007a57" and ref.static_id == "0x3f1a560f"
    assert ref.static_usercode == "0xd46fcdcb"
    assert ref.size_bytes == 128 + 384
    assert ref.ip_class == "open"
    assert ref.source == str(tmp_path / "synth" / "manifest.json")


def test_single_overlay_dir_is_accepted_too(tmp_path):
    d = make_overlay(tmp_path, "synth")
    assert [r.name for r in OverlayCatalogue([d]).refs()] == ["synth"]


def test_ip_class_and_usercode_default_when_the_manifest_lacks_them(tmp_path):
    make_overlay(tmp_path, "synth")
    (ref,) = OverlayCatalogue([tmp_path]).refs()
    assert ref.ip_class == "unknown" and ref.static_usercode == ""


def test_invalid_manifest_is_rejected_with_a_reason_not_loaded(tmp_path):
    make_overlay(tmp_path, "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"schema": 1, "rm_name": "bad"}))
    cat = OverlayCatalogue([tmp_path])
    assert [r.name for r in cat.refs()] == ["good"]
    assert "missing required key" in cat.rejects[str(bad / "manifest.json")]


def test_unparsable_json_is_rejected(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not json")
    cat = OverlayCatalogue([tmp_path])
    assert cat.refs() == [] and "invalid JSON" in next(iter(cat.rejects.values()))


def test_missing_dir_is_empty_not_an_error(tmp_path):
    assert OverlayCatalogue([tmp_path / "nowhere"]).refs() == []


def test_real_fielded_manifests_load(tmp_path):
    cat = OverlayCatalogue([FIELDED])
    names = {r.name: r for r in cat.refs()}
    assert set(names) == {"greybox", "nanosoc", "clcd_demo"}
    assert names["nanosoc"].rm_id == "0x01000001"
    assert names["greybox"].static_id == f"0x{FIELDED_STATIC_ID:08x}"
    assert names["nanosoc"].static_usercode == "0xd46fcdcb"
    assert cat.rejects == {}


# -- search order -------------------------------------------------------------------


def test_env_dirs_are_searched_after_explicit_dirs(tmp_path, monkeypatch):
    first = make_overlay(tmp_path / "a", "synth").parent
    second = make_overlay(tmp_path / "b", "synth").parent        # same key: shadowed
    make_overlay(tmp_path / "b", "other", rm_id=SYNTH2_RM_ID)
    use_overlay_dirs(monkeypatch, second)
    cat = OverlayCatalogue([first])
    refs = cat.refs()
    assert [r.name for r in refs] == ["synth", "other"]
    assert ref_named(refs, "synth").source.startswith(str(first))
    assert [e.origin for e in cat.entries()] == ["dir", "env"]


def test_env_var_is_ignored_when_use_env_is_false(tmp_path, monkeypatch):
    use_overlay_dirs(monkeypatch, make_overlay(tmp_path, "synth").parent)
    assert OverlayCatalogue(use_env=False).refs() == []
    assert [r.name for r in OverlayCatalogue().refs()] == ["synth"]


def test_env_var_splits_on_os_pathsep(tmp_path, monkeypatch):
    a = make_overlay(tmp_path / "a", "one").parent
    b = make_overlay(tmp_path / "b", "two", rm_id=SYNTH2_RM_ID).parent
    monkeypatch.setenv(OVERLAY_DIRS_ENV, f"{a}{os.pathsep}{os.pathsep}{b}")
    assert [r.name for r in OverlayCatalogue().refs()] == ["one", "two"]


def test_same_name_for_another_shell_is_kept_not_shadowed(tmp_path):
    make_overlay(tmp_path / "a", "synth")
    make_overlay(tmp_path / "b", "synth", static_id=OTHER_STATIC_ID)
    refs = OverlayCatalogue([tmp_path / "a", tmp_path / "b"]).refs()
    assert sorted(r.static_id for r in refs) == ["0x3f1a560f", "0xdeadbeef"]


# -- lookups ------------------------------------------------------------------------


def test_entry_for_finds_by_source_then_by_identity(tmp_path):
    make_overlay(tmp_path, "synth")
    cat = OverlayCatalogue([tmp_path])
    ref = cat.refs()[0]
    assert cat.entry_for(ref).ref is ref
    loose = OverlayRef(name="synth", rm_id="0x01007A57", static_id="0x3F1A560F")
    assert cat.entry_for(loose).ref == ref


def test_entry_for_unknown_overlay_is_absent(tmp_path):
    cat = OverlayCatalogue([tmp_path])
    with pytest.raises(AbsentError):
        cat.entry_for(OverlayRef(name="nope", rm_id="0x1", static_id="0x2"))


def test_greybox_for_matches_the_running_shell_only(tmp_path):
    make_overlay(tmp_path / "a", "greybox", rm_id=0)
    make_overlay(tmp_path / "b", "greybox", rm_id=0, static_id=OTHER_STATIC_ID)
    cat = OverlayCatalogue([tmp_path / "a", tmp_path / "b"])
    assert cat.greybox_for("0xdeadbeef").ref.static_id == "0xdeadbeef"
    assert cat.greybox_for("0x3F1A560F").ref.static_id == "0x3f1a560f"
    assert cat.greybox_for("0x12345678") is None


# -- rm_name by design id -----------------------------------------------------------


def test_rm_name_is_keyed_by_design_id_across_versions():
    cat = OverlayCatalogue([FIELDED])
    assert cat.rm_name("0x01000001") == "nanosoc"
    assert cat.rm_name(0x01010001) == "nanosoc"       # v1.1 is still nanosoc (rm_id.py)
    assert cat.rm_name("0x00000000") == "greybox"
    assert cat.rm_name("0x00010007") == "clcd_demo"


def test_rm_name_unknown_design_is_empty():
    cat = OverlayCatalogue([FIELDED])
    assert cat.rm_name("0x0100001e") == ""             # led is not in the fixture
    assert cat.rm_name("garbage") == ""


def test_resolve_rm_name_prefers_manifests_then_known_designs(tmp_path, monkeypatch):
    make_overlay(tmp_path, "renamed_nanosoc", rm_id=0x01000001)
    use_overlay_dirs(monkeypatch, tmp_path)
    assert resolve_rm_name("0x01000001") == "renamed_nanosoc"     # manifest wins
    assert resolve_rm_name("0x0100001e") == "led"                 # KNOWN_DESIGNS fallback
    assert resolve_rm_name("0x0100abcd") == ""                    # nobody knows it
    assert resolve_rm_name("not-an-id") == ""


def test_default_catalogue_follows_the_env_var(tmp_path, monkeypatch):
    use_overlay_dirs(monkeypatch, make_overlay(tmp_path / "a", "one").parent)
    assert ov_mod.default_catalogue().rm_name(SYNTH_RM_ID) == "one"
    use_overlay_dirs(monkeypatch, make_overlay(tmp_path / "b", "two").parent)
    assert ov_mod.default_catalogue().rm_name(SYNTH_RM_ID) == "two"


# -- preflight (c): pairing ---------------------------------------------------------


def test_pair_ok_for_gen_manifest_names(tmp_path):
    make_overlay(tmp_path, "synth")
    (entry,) = OverlayCatalogue([tmp_path]).entries()
    assert entry.pair_check is Check.OK and "0x01007a57" in entry.pair_detail


def test_pair_mismatch_when_clearing_is_not_the_partials(tmp_path):
    make_overlay(tmp_path, "synth", clearing_name="other_clear.bin")
    (entry,) = OverlayCatalogue([tmp_path]).entries()
    assert entry.pair_check is Check.MISMATCH and "synth_clear.bin" in entry.pair_detail


def test_pair_mismatch_when_both_point_at_one_file(tmp_path):
    make_overlay(tmp_path, "synth", clearing_name="synth.bin")
    (entry,) = OverlayCatalogue([tmp_path]).entries()
    assert entry.pair_check is Check.MISMATCH and "same file" in entry.pair_detail


# -- content store ------------------------------------------------------------------


def test_imported_overlay_loads_from_the_store(tmp_path):
    store = FakeStore(tmp_path / "store")
    sha = import_overlay(store, make_overlay(tmp_path / "src", "synth", static_usercode=0xD46FCDCB))
    cat = OverlayCatalogue(use_env=False)
    assert cat.refs() == []                                       # no store attached yet
    cat.use_store(store)
    (entry,) = cat.entries()
    assert entry.origin == "store" and entry.ref.source == f"store:{sha}"
    assert entry.ref.static_usercode == "0xd46fcdcb"
    assert entry.pair_check is Check.OK
    entry.overlay.validate(expected_static_id=FIELDED_STATIC_ID)  # the pusher's view is intact
    assert entry.overlay.partial_path().parent == store.root
    kinds = sorted(k for k, _ in store.index.values())
    assert kinds == ["overlay", "overlay_payload", "overlay_payload"]


def test_store_is_searched_after_dirs(tmp_path):
    store = FakeStore(tmp_path / "store")
    import_overlay(store, make_overlay(tmp_path / "src", "synth"))
    cat = OverlayCatalogue([tmp_path / "src"], store=store)
    (entry,) = cat.entries()                                      # the dir copy shadows it
    assert entry.origin == "dir"


def test_corrupt_overlay_is_refused_at_import(tmp_path):
    store = FakeStore(tmp_path / "store")
    with pytest.raises(RefusedError, match="crc32 mismatch"):
        import_overlay(store, make_overlay(tmp_path / "src", "synth", corrupt_partial=True))
    assert store.index == {}


def test_store_entry_without_payload_meta_is_rejected(tmp_path):
    store = FakeStore(tmp_path / "store")
    manifest = (make_overlay(tmp_path / "src", "synth") / "manifest.json").read_bytes()
    store.put_bytes(manifest, kind="overlay", meta={"rm_name": "synth"})
    cat = OverlayCatalogue(use_env=False, store=store)
    assert cat.refs() == [] and "lacks meta 'clearing_sha256'" in next(iter(cat.rejects.values()))


def test_store_entry_with_a_broken_manifest_is_rejected(tmp_path):
    store = FakeStore(tmp_path / "store")
    store.put_bytes(b"{}", kind="overlay", meta={"clearing_sha256": "a", "partial_sha256": "b"})
    cat = OverlayCatalogue(use_env=False, store=store)
    assert cat.refs() == [] and "missing required key" in next(iter(cat.rejects.values()))


def test_store_pair_mismatch_when_a_payload_was_imported_for_another_rm(tmp_path):
    store = FakeStore(tmp_path / "store")
    sha = import_overlay(store, make_overlay(tmp_path / "src", "synth"))
    _, meta = store.index[sha]
    kind, pmeta = store.index[meta["partial_sha256"]]
    store.index[meta["partial_sha256"]] = (kind, {**pmeta, "rm_id": "0x01000001"})
    (entry,) = OverlayCatalogue(use_env=False, store=store).entries()
    assert entry.pair_check is Check.MISMATCH and "partial" in entry.pair_detail
