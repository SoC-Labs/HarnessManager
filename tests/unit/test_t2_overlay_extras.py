"""The overlay store keeps the optional ``.ltx`` and build receipt (XVC CCR X-2, KIT-GUIDE
CCR KG-3). Each check has a negative twin.

The overlay shape mirrors the ILA mint's real ``nanosoc_ila/manifest.json`` (``ltx`` +
``ltx_crc32``) and KIT-GUIDE's pack step (``build_receipt`` + ``<rm>_build.json``).
"""

from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

import pytest
from pyverify.overlay import OverlayValidationError

from harness_manager.cli.output import jsonable
from harness_manager.client.codec import from_json
from harness_manager.core.errors import RefusedError
from harness_manager.core.model import Check
from harness_manager.core.pack import OverlayRef
from harness_manager.services.store import ContentStore
from harness_manager_mps3 import deploy as deploy_mod
from harness_manager_mps3.overlays import (
    OVERLAY_DIRS_ENV,
    OverlayCatalogue,
    import_overlay,
)
from tests.fakes.t2_overlays import FIELDED_STATIC_ID, FakeStore, make_overlay

LTX = json.dumps({"probes": [{"name": "u_ila_0/probe0", "width": 32}]}).encode() * 20
RECEIPT = json.dumps({"schema": "harness-manager-rm-build", "state": "passed",
                      "rm_name": "synth", "gates": []}).encode()
BASE_META = {"rm_name", "rm_id", "static_id", "clearing_sha256", "partial_sha256"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _no_ambient_overlay_dirs(monkeypatch):
    monkeypatch.delenv(OVERLAY_DIRS_ENV, raising=False)


def with_extras(d: Path, *, ltx: bytes | None = LTX, ltx_crc32: str | None = "auto",
                ltx_name: str | None = None, receipt: bytes | None = RECEIPT,
                receipt_name: str | None = None, named: bool = True) -> Path:
    """Add an ``.ltx`` and/or a build receipt to an overlay dir ``make_overlay`` wrote."""
    mf = d / "manifest.json"
    m = json.loads(mf.read_text())
    if ltx is not None:
        name = ltx_name or f"{m['rm_name']}.ltx"
        (d / name).write_bytes(ltx)
        m["ltx"] = name
        if ltx_crc32 == "auto":
            m["ltx_crc32"] = f"0x{zlib.crc32(ltx) & 0xFFFFFFFF:08x}"
        elif ltx_crc32 is not None:
            m["ltx_crc32"] = ltx_crc32
    if receipt is not None:
        name = receipt_name or f"{m['rm_name']}_build.json"
        (d / name).write_bytes(receipt)
        if named:
            m["build_receipt"] = name
    mf.write_text(json.dumps(m, indent=2) + "\n")
    return d


def stored_entry(store):
    (entry,) = OverlayCatalogue(use_env=False, store=store).entries()
    return entry


# --- import: stored, listed, verifiable -----------------------------------------------------


def test_the_ltx_and_the_receipt_are_stored_by_sha_and_listed(tmp_path):
    store = ContentStore(tmp_path / "store")
    msha = import_overlay(store, with_extras(make_overlay(tmp_path / "src", "synth")))
    ((_, meta),) = store.find("overlay")
    assert meta["ltx_sha256"] == sha(LTX) and meta["receipt_sha256"] == sha(RECEIPT)
    roles = {m["role"]: s for s, m in store.find("overlay_payload")}
    assert roles["ltx"] == sha(LTX) and roles["receipt"] == sha(RECEIPT)
    assert store.verify(meta["ltx_sha256"]) and store.verify(meta["receipt_sha256"])
    entry = stored_entry(store)
    assert entry.ref.source == f"store:{msha}"
    assert entry.ref.ltx_sha256 == sha(LTX) and entry.ref.receipt_sha256 == sha(RECEIPT)
    ltx = entry.overlay.ltx_path()                     # the probes file, served from the store
    assert ltx is not None and sha(ltx.read_bytes()) == sha(LTX)
    assert sha(entry.overlay.optional_path("receipt").read_bytes()) == sha(RECEIPT)
    entry.overlay.validate(expected_static_id=FIELDED_STATIC_ID)


def test_twin_an_overlay_without_them_is_stored_and_listed_exactly_as_before(tmp_path):
    store = ContentStore(tmp_path / "store")
    import_overlay(store, make_overlay(tmp_path / "src", "synth"))
    ((_, meta),) = store.find("overlay")
    assert set(meta) == BASE_META
    assert sorted(m["role"] for _, m in store.find("overlay_payload")) == ["clearing", "partial"]
    entry = stored_entry(store)
    assert entry.ref.ltx_sha256 == "" and entry.ref.receipt_sha256 == ""
    assert entry.overlay.ltx_path() is None and entry.overlay.optional_path("receipt") is None
    entry.overlay.validate(expected_static_id=FIELDED_STATIC_ID)


def test_a_receipt_is_found_by_its_conventional_name_when_the_manifest_names_none(tmp_path):
    store = FakeStore(tmp_path / "store")
    d = with_extras(make_overlay(tmp_path / "src", "synth"), ltx=None, named=False)
    import_overlay(store, d)
    assert stored_entry(store).ref.receipt_sha256 == sha(RECEIPT)
    store2 = FakeStore(tmp_path / "store2")
    d2 = with_extras(make_overlay(tmp_path / "src2", "synth"), ltx=None, named=False,
                     receipt_name="receipt.json")
    import_overlay(store2, d2)
    assert stored_entry(store2).ref.receipt_sha256 == sha(RECEIPT)
    # twin: some other JSON beside the pair is not a receipt
    store3 = FakeStore(tmp_path / "store3")
    d3 = with_extras(make_overlay(tmp_path / "src3", "synth"), ltx=None, named=False,
                     receipt_name="notes.json")
    import_overlay(store3, d3)
    assert stored_entry(store3).ref.receipt_sha256 == ""


def test_an_ltx_without_a_crc_in_the_manifest_is_still_stored(tmp_path):
    store = FakeStore(tmp_path / "store")
    import_overlay(store, with_extras(make_overlay(tmp_path / "src", "synth"), ltx_crc32=None,
                                      receipt=None))
    ref = stored_entry(store).ref
    assert ref.ltx_sha256 == sha(LTX) and ref.receipt_sha256 == ""


# --- import: refused, and the store untouched -----------------------------------------------


def test_a_stale_ltx_that_fails_the_manifests_crc_is_refused(tmp_path):
    store = FakeStore(tmp_path / "store")
    d = with_extras(make_overlay(tmp_path / "src", "synth"), ltx_crc32="0x00000000")
    with pytest.raises(RefusedError, match="ltx crc32 mismatch"):
        import_overlay(store, d)
    assert store.index == {}
    # twin: the right crc (upper-case hex, as a receipt may write it) imports
    with_extras(d, ltx_crc32=f"0x{zlib.crc32(LTX) & 0xFFFFFFFF:08X}", receipt=None)
    import_overlay(store, d)
    assert stored_entry(store).ref.ltx_sha256 == sha(LTX)


def test_a_named_receipt_that_is_missing_is_refused(tmp_path):
    store = FakeStore(tmp_path / "store")
    d = with_extras(make_overlay(tmp_path / "src", "synth"), receipt=None)
    m = json.loads((d / "manifest.json").read_text())
    (d / "manifest.json").write_text(json.dumps({**m, "build_receipt": "synth_build.json"}))
    with pytest.raises(RefusedError, match="build_receipt referenced but missing"):
        import_overlay(store, d)
    assert store.index == {}
    (d / "synth_build.json").write_bytes(RECEIPT)          # twin: present, it imports
    import_overlay(store, d)
    assert stored_entry(store).ref.receipt_sha256 == sha(RECEIPT)


@pytest.mark.parametrize("field", ["ltx", "build_receipt"])
def test_an_optional_file_outside_the_overlay_dir_is_refused(tmp_path, field):
    store = FakeStore(tmp_path / "store")
    d = make_overlay(tmp_path / "src", "synth")
    (tmp_path / "src" / "outside.json").write_bytes(RECEIPT)   # exists, so pyverify passes
    m = json.loads((d / "manifest.json").read_text())
    (d / "manifest.json").write_text(json.dumps({**m, field: "../outside.json"}))
    with pytest.raises(RefusedError, match="not a file name beside the manifest"):
        import_overlay(store, d)
    assert store.index == {}
    (d / "outside.json").write_bytes(RECEIPT)                 # twin: beside the manifest
    (d / "manifest.json").write_text(json.dumps({**m, field: "outside.json"}))
    import_overlay(store, d)


# --- tampering: caught where the payload CRCs are ---------------------------------------------


@pytest.mark.parametrize("role", ["ltx", "receipt"])
def test_a_tampered_stored_file_fails_validate_and_the_deploy_files_check(tmp_path, role):
    store = ContentStore(tmp_path / "store")
    import_overlay(store, with_extras(make_overlay(tmp_path / "src", "synth")))
    entry = stored_entry(store)
    assert deploy_mod._check_files(entry).check is Check.OK     # twin first: intact
    blob = entry.overlay.optional_path(role)
    blob.chmod(0o644)
    blob.write_bytes(blob.read_bytes() + b"tampered")
    assert not store.verify(entry.overlay.optional_sha256[role])
    with pytest.raises(OverlayValidationError, match=f"stored {role} .* fails its sha256"):
        entry.overlay.validate()
    item = deploy_mod._check_files(entry)
    assert item.check is Check.MISMATCH and role in item.detail
    # re-importing repairs the store copy (ContentStore replaces a blob that fails verify)
    import_overlay(store, with_extras(make_overlay(tmp_path / "src2", "synth")))
    stored_entry(store).overlay.validate()


def test_a_lost_stored_ltx_is_reported_not_crashed_on(tmp_path):
    store = ContentStore(tmp_path / "store")
    import_overlay(store, with_extras(make_overlay(tmp_path / "src", "synth"), receipt=None))
    entry = stored_entry(store)
    entry.overlay.ltx_path().unlink()
    entry = stored_entry(store)                                  # a fresh catalogue load
    assert entry.overlay.ltx_path() is None and entry.ref.ltx_sha256 == sha(LTX)
    with pytest.raises(OverlayValidationError, match="stored ltx .* missing"):
        entry.overlay.validate()


# --- listing: directories, the API wire, the CLI ------------------------------------------------


def test_a_directory_overlay_lists_the_shas_of_its_optional_files(tmp_path):
    with_extras(make_overlay(tmp_path / "ov", "synth"))
    make_overlay(tmp_path / "ov", "plain", rm_id=0x0100_7A58)
    refs = {r.name: r for r in OverlayCatalogue([tmp_path / "ov"], use_env=False).refs()}
    assert refs["synth"].ltx_sha256 == sha(LTX) and refs["synth"].receipt_sha256 == sha(RECEIPT)
    assert refs["plain"].ltx_sha256 == "" and refs["plain"].receipt_sha256 == ""   # twin


def test_the_api_wire_carries_the_shas_and_an_older_daemon_still_decodes():
    ref = OverlayRef(name="synth", rm_id="0x01007a57", static_id="0x3f1a560f",
                     source="store:ab", ltx_sha256=sha(LTX), receipt_sha256=sha(RECEIPT))
    body = json.loads(json.dumps(jsonable(ref)))
    assert body["ltx_sha256"] == sha(LTX) and body["receipt_sha256"] == sha(RECEIPT)
    assert from_json(OverlayRef, body) == ref
    old = {k: v for k, v in body.items() if k not in ("ltx_sha256", "receipt_sha256")}
    assert from_json(OverlayRef, old).ltx_sha256 == ""           # twin: a daemon without them
