"""ContentStore (Team T1). Each check has a negative twin."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from socharness.core.errors import AbsentError, ExitCode, UsageError
from socharness.core.services import ContentStore as ContentStoreProtocol
from socharness.services import store as store_mod
from socharness.services.store import ContentStore

OVL = {"name": "nanosoc", "rm_id": "0x01000001", "static_id": "0x3f1a560f"}


@pytest.fixture
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "store")


def blobs(store: ContentStore) -> list[Path]:
    return sorted(p for p in (store.root / "blobs").rglob("*") if p.is_file())


def records(store: ContentStore) -> list[Path]:
    return sorted((store.root / "index").glob("*/*.json"))


def test_satisfies_the_frozen_protocol(store: ContentStore):
    assert isinstance(store, ContentStoreProtocol)


# -- put / get / verify -------------------------------------------------------------------

def test_put_bytes_returns_sha256_and_content_round_trips(store: ContentStore):
    data = b"partial bitstream bytes"
    sha = store.put_bytes(data, kind="overlay", meta=OVL)
    assert sha == hashlib.sha256(data).hexdigest()
    assert store.path(sha).read_bytes() == data
    assert store.path(sha) == store.root / "blobs" / sha[:2] / sha
    assert store.verify(sha)


def test_path_of_unknown_blob_is_absent(store: ContentStore):
    # Negative twin: a digest that was never stored.
    with pytest.raises(AbsentError) as exc:
        store.path("0" * 64)
    assert exc.value.code == ExitCode.ABSENT


def test_corrupted_blob_is_detected(store: ContentStore):
    sha = store.put_bytes(b"good content", kind="overlay", meta=OVL)
    store.path(sha).write_bytes(b"g00d content")          # same length, one byte flipped
    assert not store.verify(sha)


def test_truncated_or_deleted_blob_fails_verify(store: ContentStore):
    sha = store.put_bytes(b"0123456789", kind="overlay", meta=OVL)
    store.path(sha).write_bytes(b"01234")
    assert not store.verify(sha)
    store.path(sha).unlink()
    assert not store.verify(sha)
    with pytest.raises(AbsentError):
        store.path(sha)


def test_adding_good_content_again_heals_a_corrupted_blob(store: ContentStore):
    data = b"heal me"
    sha = store.put_bytes(data, kind="overlay", meta=OVL)
    store.path(sha).write_bytes(b"damaged")
    assert store.put_bytes(data, kind="overlay", meta=OVL) == sha
    assert store.verify(sha) and store.path(sha).read_bytes() == data


@pytest.mark.parametrize("bad", ["../../etc/passwd", "ABC", "g" * 64, "A" * 64, ""])
def test_malformed_digest_is_a_usage_error(store: ContentStore, bad: str):
    # Negative twin of path()/verify(): nothing outside blobs/ can be named.
    with pytest.raises(UsageError):
        store.path(bad)
    with pytest.raises(UsageError):
        store.verify(bad)


def test_put_file_streams_in_chunks(store: ContentStore, tmp_path: Path, monkeypatch):
    src = tmp_path / "big.bin"
    data = os.urandom(1000) + b"tail"
    src.write_bytes(data)
    monkeypatch.setattr(store_mod, "CHUNK", 7)             # force many chunks
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read the whole file"))
    sha = store.put_file(src, kind="bundle", meta={"harness_version": "1.0.0"})
    monkeypatch.undo()
    assert sha == hashlib.sha256(data).hexdigest()
    assert store.path(sha).read_bytes() == data and store.verify(sha)


def test_put_file_missing_source_is_absent(store: ContentStore, tmp_path: Path):
    with pytest.raises(AbsentError):
        store.put_file(tmp_path / "nope.bit", kind="overlay", meta=OVL)
    assert blobs(store) == [] and records(store) == []


def test_put_file_and_put_bytes_agree(store: ContentStore, tmp_path: Path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"same bytes")
    assert store.put_file(src, kind="overlay", meta=OVL) == store.put_bytes(
        b"same bytes", kind="overlay", meta=OVL)
    assert len(blobs(store)) == 1


# -- metadata validation ----------------------------------------------------------------

@pytest.mark.parametrize("kind, meta", [
    ("", {"a": "b"}),
    ("overlay", {"size": 123}),
    ("overlay", {1: "x"}),
    ("overlay", ["not", "a", "dict"]),
])
def test_bad_kind_or_meta_is_refused_before_anything_is_written(store, kind, meta):
    with pytest.raises(UsageError):
        store.put_bytes(b"x", kind=kind, meta=meta)
    assert blobs(store) == [] and records(store) == []


# -- dedup and find ---------------------------------------------------------------------

def test_identical_content_is_stored_once(store: ContentStore):
    a = store.put_bytes(b"dup", kind="overlay", meta=OVL)
    b = store.put_bytes(b"dup", kind="overlay", meta=dict(OVL))
    assert a == b
    assert len(blobs(store)) == 1 and len(records(store)) == 1
    assert store.find("overlay") == [(a, OVL)]


def test_different_content_is_stored_separately(store: ContentStore):
    # Negative twin of dedup.
    a = store.put_bytes(b"one", kind="overlay", meta=OVL)
    b = store.put_bytes(b"two", kind="overlay", meta=OVL)
    assert a != b and len(blobs(store)) == 2


def test_same_content_new_meta_adds_a_record_and_keeps_the_old(store: ContentStore):
    sha = store.put_bytes(b"shared", kind="overlay", meta=OVL)
    other = dict(OVL, source="fielded/0x3F1A560F")
    assert store.put_bytes(b"shared", kind="overlay", meta=other) == sha
    assert len(blobs(store)) == 1 and len(records(store)) == 2
    metas = [m for _, m in store.find("overlay", name="nanosoc")]
    assert OVL in metas and other in metas


def test_find_requires_every_meta_key_to_match(store: ContentStore):
    keyed = store.put_bytes(b"A", kind="overlay", meta=OVL)
    store.put_bytes(b"B", kind="overlay", meta=dict(OVL, static_id="0xcd74b6ae", name="led"))
    store.put_bytes(b"C", kind="bundle", meta={"static_id": "0x3f1a560f"})
    assert store.find("overlay", static_id="0x3f1a560f") == [(keyed, OVL)]
    assert store.find("overlay", static_id="0x3f1a560f", name="nanosoc") == [(keyed, OVL)]
    # Negative twins: one key wrong, an unknown key, the wrong kind.
    assert store.find("overlay", static_id="0x3f1a560f", name="led") == []
    assert store.find("overlay", static_usercode="0x1234") == []
    assert store.find("clearing", static_id="0x3f1a560f") == []
    assert len(store.find("overlay")) == 2


def test_find_skips_records_whose_blob_is_gone(store: ContentStore):
    sha = store.put_bytes(b"vanishing", kind="overlay", meta=OVL)
    store.path(sha).unlink()
    assert store.find("overlay") == []


def test_find_skips_a_torn_index_record(store: ContentStore):
    sha = store.put_bytes(b"x", kind="overlay", meta=OVL)
    (store.root / "index" / sha / "junk.json").write_text("{not json")
    assert store.find("overlay") == [(sha, OVL)]


def test_store_survives_reopening(tmp_path: Path):
    sha = ContentStore(tmp_path / "s").put_bytes(b"persist", kind="overlay", meta=OVL)
    again = ContentStore(tmp_path / "s")
    assert again.find("overlay", rm_id="0x01000001") == [(sha, OVL)] and again.verify(sha)


# -- temp files -------------------------------------------------------------------------

def test_no_partial_files_are_left_behind(store: ContentStore, tmp_path: Path):
    store.put_bytes(b"a", kind="k", meta={})
    src = tmp_path / "f"
    src.write_bytes(b"b")
    store.put_file(src, kind="k", meta={})
    assert list((store.root / "tmp").iterdir()) == []


def test_old_partial_files_are_swept_fresh_ones_kept(tmp_path: Path):
    tmpdir = tmp_path / "s" / "tmp"
    tmpdir.mkdir(parents=True)
    old, fresh = tmpdir / "old.part", tmpdir / "fresh.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    past = time.time() - store_mod.TMP_MAX_AGE_S - 60
    os.utime(old, (past, past))
    ContentStore(tmp_path / "s")
    assert not old.exists() and fresh.exists()


def test_failed_write_removes_its_partial_file(store: ContentStore, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        store.put_bytes(b"x", kind="k", meta={})
    assert list((store.root / "tmp").iterdir()) == [] and blobs(store) == []


# -- concurrency --------------------------------------------------------------------------

def test_concurrent_puts_from_threads_are_safe(store: ContentStore):
    payloads = [f"blob-{i % 5}".encode() * 1000 for i in range(40)]
    results: list[str] = []
    errors: list[BaseException] = []
    gate = threading.Barrier(len(payloads))

    def worker(data: bytes) -> None:
        try:
            gate.wait()
            results.append(store.put_bytes(data, kind="overlay", meta={"n": data[:6].decode()}))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    expected = {hashlib.sha256(p).hexdigest() for p in payloads}
    assert set(results) == expected and len(blobs(store)) == 5
    assert all(store.verify(sha) for sha in expected)
    assert len(records(store)) == 5 and len(store.find("overlay")) == 5
    assert list((store.root / "tmp").iterdir()) == []


def test_concurrent_puts_from_two_processes_are_safe(store: ContentStore):
    script = textwrap.dedent("""
        import sys
        from pathlib import Path
        from socharness.services.store import ContentStore
        s = ContentStore(Path(sys.argv[1]))
        for i in range(60):
            s.put_bytes((b"shared-%d" % (i % 6)) * 2000, kind="overlay",
                        meta={"n": str(i % 6)})
        print("done")
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, str(store.root)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    outs = [p.communicate(timeout=50) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], [o[1] for o in outs]
    found = store.find("overlay")
    assert len(found) == 6 and len(blobs(store)) == 6
    assert all(store.verify(sha) for sha, _ in found)
    assert list((store.root / "tmp").iterdir()) == []
