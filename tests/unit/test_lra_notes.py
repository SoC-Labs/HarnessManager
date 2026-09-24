"""LR-A: the request/answer note files on the hub. Each check has a twin.

Two stores run the same checks: the fake hub's Python model (tests/fakes/lr_hub.py), and the
REAL ``NOTE_SCRIPT`` under this machine's POSIX shells (sh, and ksh/dash when present) in a
temporary directory. ``ShRunner`` runs nothing but that script: an fpgahub argv fails the test.
Nothing here reaches a hub, runs ssh, or runs fpgahub.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pyverify.lease import RunResult, SshHubRunner

from harness_manager.core.errors import ExitCode, UnavailableError, UnreachableError, UsageError
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3.hub import AnswerNote, RequestNote
from tests.fakes.lr_hub import TARGET, LrFakeHub

HOST = "mapstone-dev.ecs.soton.ac.uk"
POSIX = os.name == "posix" and shutil.which("sh") is not None
SHELLS = [sh for sh in ("sh", "ksh", "dash") if shutil.which(sh)] if POSIX else []
ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    def refuse(host, group):
        raise AssertionError(f"a test tried to reach the real hub {host}")

    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", refuse)


class ShRunner:
    """The note script, for real, with one local shell. Refuses any other command."""

    def __init__(self, shell: str = "sh") -> None:
        self.shell = shell
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None) -> RunResult:
        argv = list(argv)
        assert argv[:2] == ["sh", "-c"] and argv[2] == hubmod.NOTE_SCRIPT, argv[:2]
        self.calls.append(argv)
        proc = subprocess.run([self.shell, "-c", *argv[2:]], capture_output=True, text=True,
                              timeout=30, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        return RunResult(proc.returncode, proc.stdout, proc.stderr)


def now(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat(timespec="seconds")


def request(rid: str = "r1", message: str = "may I have mps3-01?", **kw) -> RequestNote:
    base = {"id": rid, "by": "bob@mapstone-dev", "user": "bob", "host": "srv03335",
            "message": message, "created_at": now(), "deadline_at": now(120)}
    return RequestNote(**{**base, **kw})


def answer(rid: str = "r1", ans: str = "keep", minutes: int = 15, message: str = "nearly done") -> AnswerNote:
    return AnswerNote(rid, ans, minutes, message, now())


class Store:
    """One note store under test: a client, plus how to reach the files behind it."""

    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        if kind == "fake":
            self.hub = LrFakeHub()
            self.client = hubmod.HubClient(HOST, TARGET, runner=self.hub)
            self.root = hubmod.NOTE_ROOT
        else:
            self.hub = None
            self.runner = ShRunner(kind)
            self.client = hubmod.HubClient(HOST, TARGET, runner=self.runner, group=None)
            self.root = str(tmp_path / "harness-manager-lease")
            self.client.note_root = self.root
        self.dir = f"{self.root}/{TARGET}"

    def files(self) -> dict[str, str]:
        if self.hub is not None:
            return self.hub.notes()
        d = Path(self.dir)
        return {p.name: p.read_text() for p in d.iterdir()} if d.is_dir() else {}

    def plant(self, name: str, body: str, *, age_s: float = 0.0) -> None:
        if self.hub is not None:
            self.hub.plant_note(name, body, age_s=age_s)
            return
        d = Path(self.dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)
        t = time.time() - age_s
        os.utime(d / name, (t, t))

    def age(self, seconds: float) -> None:
        if self.hub is not None:
            self.hub.age_notes(seconds)
            return
        for p in Path(self.dir).iterdir():
            st = p.stat()
            os.utime(p, (st.st_atime - seconds, st.st_mtime - seconds))

    def lock(self, how: str) -> None:
        """Make the note directory unwritable / unreadable / a symlink."""
        if self.hub is not None:
            setattr(self.hub, f"notes_{how}", True)
            return
        d = Path(self.dir)
        d.mkdir(parents=True, exist_ok=True)
        if how == "unwritable":
            d.chmod(0o550)
        elif how == "unreadable":
            d.chmod(0o330)
        elif how == "symlink":
            shutil.rmtree(d)
            (Path(self.root) / "elsewhere").mkdir()
            d.symlink_to(Path(self.root) / "elsewhere")

    def unlock(self) -> None:
        if self.hub is None and Path(self.dir).is_dir() and not Path(self.dir).is_symlink():
            Path(self.dir).chmod(0o770)


STORES = ["fake", *SHELLS]


@pytest.fixture(params=STORES)
def store(request, tmp_path):
    st = Store(request.param, tmp_path)
    yield st
    st.unlock()


# -- the round trip -------------------------------------------------------------------------------


def test_request_answer_round_trip(store):
    note = request()
    store.client.put_request(note)
    assert store.client.list_requests() == [note]
    assert store.client.get_answer("r1") is None
    ans = answer()
    store.client.put_answer(ans)
    assert store.client.get_answer("r1") == ans
    store.client.delete_request("r1")
    assert store.client.list_requests() == []
    assert set(store.files()) == {"ans-r1.json"}                  # the answer is left to pruning


def test_negative_twin_a_missing_note_directory_is_simply_empty(store):
    assert store.client.list_requests() == []
    assert store.client.get_answer("r1") is None
    store.client.delete_request("r1")                              # nothing to delete: fine
    assert store.files() == {}


def test_requests_list_oldest_first_and_only_requests(store):
    store.client.put_request(request("late", created_at=now(5)))
    store.client.put_request(request("early", created_at=now(-5)))
    store.client.put_answer(answer("early"))
    assert [n.id for n in store.client.list_requests()] == ["early", "late"]


def test_writes_are_atomic_files_with_group_access(store):
    store.client.put_request(request())
    names = set(store.files())
    assert names == {"req-r1.json"}                                # no temp file left behind
    if store.hub is None:
        mode = Path(store.dir).stat().st_mode
        assert mode & 0o7777 == 0o2770 and Path(store.root).stat().st_mode & 0o7777 == 0o2770
        assert Path(store.dir, "req-r1.json").stat().st_mode & 0o777 == 0o660


def test_rewriting_a_note_replaces_it(store):
    store.client.put_answer(answer(minutes=5))
    store.client.put_answer(answer(minutes=30))
    assert store.client.get_answer("r1").minutes == 30


# -- the 4 KiB cap --------------------------------------------------------------------------------


def _message_filling(total: int) -> str:
    base = len(hubmod.encode_request(request(message="")))
    return "m" * (total - base)


def test_a_note_of_exactly_4096_bytes_is_fine(store):
    note = request(message=_message_filling(4096))
    assert len(hubmod.encode_request(note)) == 4096
    store.client.put_request(note)
    assert store.client.list_requests() == [note]


def test_negative_twin_4097_bytes_is_refused_before_it_is_sent(store):
    note = request(message=_message_filling(4097))
    with pytest.raises(UsageError):
        store.client.put_request(note)
    assert store.files() == {}


def test_the_hub_side_cap_holds_even_if_the_client_check_is_bypassed(store):
    with pytest.raises(UsageError) as exc:
        store.client._notes("put", "req-big.json", "x" * 4097, what="put")
    assert "4096" in str(exc.value)
    store.plant("req-big.json", json.dumps({**asdict(request("big")), "message": "y" * 5000}))
    store.client.put_request(request("ok"))
    assert [n.id for n in store.client.list_requests()] == ["ok"]           # the big one is dropped


# -- ids, targets, values ----------------------------------------------------------------------------


@pytest.mark.parametrize("rid", ["", ".", "..", "a/b", "../../etc/passwd", "a b", "a;touch x",
                                 "$(id)", "x" * 65, "é"])
def test_ids_that_are_not_allowed_never_reach_the_hub(store, rid):
    calls = len(store.hub.calls) if store.hub else len(store.runner.calls)
    for op in (lambda: store.client.put_request(request(rid)),
               lambda: store.client.put_answer(answer(rid)),
               lambda: store.client.get_answer(rid),
               lambda: store.client.delete_request(rid)):
        with pytest.raises(UsageError):
            op()
    assert (len(store.hub.calls) if store.hub else len(store.runner.calls)) == calls


def test_negative_twin_ids_at_the_limit_are_fine(store):
    rid = "A-z_0." + "x" * 58
    assert len(rid) == 64
    store.client.put_request(request(rid))
    assert [n.id for n in store.client.list_requests()] == [rid]


@pytest.mark.parametrize("target", ["..", ".", "a/b", "x;y"])
def test_a_target_that_would_leave_the_note_root_is_refused(tmp_path, target):
    client = hubmod.HubClient(HOST, target, runner=LrFakeHub())
    with pytest.raises(UsageError):
        client.list_requests()


@pytest.mark.parametrize("field,value", [("by", "bob\nfake line"), ("user", "a\x1bb"),
                                         ("host", "h\x00"), ("message", "hi\x1b[2Jthere"),
                                         ("created_at", "soon"), ("deadline_at", "")])
def test_request_values_are_checked(store, field, value):
    with pytest.raises(UsageError):
        store.client.put_request(request(**{field: value}))


def test_negative_twin_a_multi_line_unicode_message_is_fine(store):
    note = request(message="line one\n\tline two: café ✓")
    store.client.put_request(note)
    assert store.client.list_requests()[0].message == note.message


@pytest.mark.parametrize("ans,minutes", [("maybe", 5), ("keep", -1), ("keep", 1441), ("keep", True),
                                         ("release", "5")])
def test_answers_are_checked(store, ans, minutes):
    with pytest.raises(UsageError):
        store.client.put_answer(AnswerNote("r1", ans, minutes, "", now()))


# -- injection ------------------------------------------------------------------------------------------


HOSTILE = ["$(touch {p})", "`touch {p}`", "'; touch {p}; echo '", "\"; touch {p}; echo \"",
           "a\nb; touch {p}", "%s%n%x {p}", "\\'$(touch {p})\\'", "${{IFS}}touch${{IFS}}{p}"]


@pytest.mark.skipif(not POSIX, reason="needs a POSIX shell")
@pytest.mark.parametrize("pattern", HOSTILE)
def test_a_hostile_message_is_stored_verbatim_and_never_runs(tmp_path, pattern):
    pwned = tmp_path / "PWNED"
    store = Store("sh", tmp_path)
    note = request(message=pattern.format(p=pwned))
    store.client.put_request(note)
    store.client.put_answer(answer(message=pattern.format(p=pwned)))
    assert store.client.list_requests() == [note]
    assert store.client.get_answer("r1").message == pattern.format(p=pwned)
    assert not pwned.exists()


@pytest.mark.skipif(not POSIX, reason="needs a POSIX shell")
@pytest.mark.parametrize("group", ["fpga", None])
def test_negative_twin_the_ssh_command_survives_both_hub_shells(tmp_path, group):
    """What ssh hands the hub, run as the hub runs it: the login shell, then ``sg fpga -c``
    (a stub that is ``sh -c``). The hostile message still lands as one value, and runs nothing."""
    pwned = tmp_path / "PWNED"
    root = tmp_path / "notes"
    body = hubmod.encode_request(request(message=f"'\"$(touch {pwned})`touch {pwned}`;touch {pwned}"))
    argv = ["sh", "-c", hubmod.NOTE_SCRIPT, "hm-lease", "put", str(root), TARGET, "", "req-r1.json", body]
    ssh = SshHubRunner(HOST, group=group).build(argv)
    assert ssh[0] == "ssh" and ssh[-2] == HOST
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sg").write_text('#!/bin/sh\n[ "$2" = "-c" ] || exit 2\nexec /bin/sh -c "$3"\n')
    (bin_dir / "sg").chmod(0o755)
    proc = subprocess.run(["/bin/sh", "-c", ssh[-1]], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "LC_ALL": "C"})
    assert proc.returncode == 0, proc.stderr
    assert (root / TARGET / "req-r1.json").read_text() == body and not pwned.exists()
    words = shlex.split(ssh[-1])
    inner = shlex.split(words[3]) if group else words
    assert inner[-len(argv):] == argv


# -- missing, unwritable, unreadable, not ours ----------------------------------------------------------


needs_perms = pytest.mark.skipif(ROOT_USER, reason="root ignores directory permissions")


@needs_perms
def test_an_unwritable_note_directory_says_how_to_fix_it(store):
    store.client.put_request(request("before"))
    store.lock("unwritable")
    with pytest.raises(UnavailableError) as exc:
        store.client.put_request(request())
    assert exc.value.code == ExitCode.UNAVAILABLE
    assert "not writable" in str(exc.value) and "chmod 2770" in exc.value.hint
    with pytest.raises(UnavailableError):
        store.client.delete_request("before")
    assert [n.id for n in store.client.list_requests()] == ["before"]      # reading still works


@needs_perms
def test_negative_twin_an_unreadable_note_directory_is_not_read_as_empty(store):
    store.client.put_request(request())
    store.lock("unreadable")
    with pytest.raises(UnavailableError) as exc:
        store.client.list_requests()
    assert "not readable" in str(exc.value)
    with pytest.raises(UnavailableError):
        store.client.get_answer("r1")


def test_a_symlinked_note_directory_is_refused(store):
    store.lock("symlink")
    for op in (lambda: store.client.put_request(request()), store.client.list_requests,
               lambda: store.client.get_answer("r1"), lambda: store.client.delete_request("r1")):
        with pytest.raises(UnavailableError) as exc:
            op()
        assert "symbolic link" in str(exc.value) and "not Harness Manager's" in exc.value.hint


def test_a_failed_ssh_is_a_transport_error_not_a_note_error():
    hub = LrFakeHub()
    hub.fail_with = "bob@hub: Permission denied (publickey)."
    with pytest.raises(UnreachableError) as exc:
        hubmod.HubClient(HOST, TARGET, runner=hub).list_requests()
    assert "BatchMode" in exc.value.hint


# -- pruning and planted junk ------------------------------------------------------------------------------


def test_notes_older_than_an_hour_are_pruned_by_whoever_lists(store):
    store.client.put_request(request("old"))
    store.client.put_answer(answer("old"))
    store.age(3700)
    store.client.put_request(request("new"))                  # put prunes too
    assert [n.id for n in store.client.list_requests()] == ["new"]
    assert set(store.files()) == {"req-new.json"}


def test_negative_twin_notes_younger_than_an_hour_stay(store):
    store.client.put_request(request("recent"))
    store.age(3000)
    assert [n.id for n in store.client.list_requests()] == ["recent"]


def test_junk_in_the_directory_is_ignored_not_fatal(store):
    good = request("good")
    store.client.put_request(good)
    store.plant("req-junk.json", "not json at all")
    store.plant("req-evil.json", json.dumps({**asdict(request("other")), "message": "x"}))
    store.plant("req-ctl.json", json.dumps({**asdict(request("ctl")), "message": "a\u001bb"}))
    store.plant("req-bin.json", "\x00\x01{\x7f")
    store.plant("notes.txt", "hello")
    store.plant("ans-r1.json", json.dumps({"id": "r1", "answer": "explode", "minutes": 5,
                                           "message": "", "at": now()}))
    assert store.client.list_requests() == [good]
    assert store.client.get_answer("r1") is None


# -- the fake is the script -----------------------------------------------------------------------------------


@pytest.mark.skipif(not SHELLS, reason="needs a POSIX shell")
def test_the_fakes_note_model_answers_exactly_as_the_script(tmp_path):
    """Same ops, same root, byte-identical replies from LrFakeHub and the real script."""
    root = str(tmp_path / "notes")
    real = ShRunner(SHELLS[0])
    fake = LrFakeHub()

    def op(*args: str) -> tuple[RunResult, RunResult]:
        argv = ["sh", "-c", hubmod.NOTE_SCRIPT, "hm-lease", *args]
        return real(argv), fake(argv)

    script = [("list", root, TARGET, "", "req-"), ("get", root, TARGET, "", "ans-r1.json"),
              ("put", root, TARGET, "", "req-r1.json", '{"id":"r1"}'),
              ("put", root, TARGET, "", "ans-r1.json", '{"a":"caf\\u00e9"}'),
              ("put", root, TARGET, "", "req-r2.json", "x" * 4097),
              ("put", root, TARGET, "", "req-../x.json", "{}"), ("put", root, TARGET, "", "other.json", "{}"),
              ("list", root, TARGET, "", "req-"), ("list", root, TARGET, "", "ans-"),
              ("list", root, TARGET, "", "bad-"), ("get", root, TARGET, "", "ans-r1.json"),
              ("del", root, TARGET, "", "req-r1.json"), ("del", root, TARGET, "", "req-r1.json"),
              ("list", root, TARGET, "", "req-"), ("frob", root, TARGET, "", "req-r1.json")]
    for args in script:
        r, f = op(*args)
        assert (r.returncode, r.stdout, r.stderr) == (f.returncode, f.stdout, f.stderr), args
