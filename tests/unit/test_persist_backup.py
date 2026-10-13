"""Lane L3: the card contents (/persist) backup, restore and API, with a fake SSH runner.

The fake runner stands in for ``services.persist_backup.run_stream``: it reads the remote
command off the argv and runs the REAL ``tar`` on a temp folder that plays the board's
/persist. No ssh, no board, no key material beyond throw-away bytes.
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core.errors import ActionFailedError, ClaimLockedError, RefusedError
from harness_manager.services import persist_backup as pb
from harness_manager.services.persist_backup import PersistBackupService

BOARD = "mps3@192.168.10.101"


class FakeBoardPersist:
    """The board's /persist as a folder, and a runner that runs tar on it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.fail: str = ""               # "backup" | "restore": the ssh fails

    def __call__(self, argv, *, stdin_path=None, sink=None, on_bytes=None, timeout=0):
        self.calls.append(list(argv))
        assert argv[:2] == ["fake-ssh", "board"]
        cmd = argv[2:]
        assert cmd[:3] == ["tar", "-C", "/persist"], cmd
        flags = cmd[3]
        if flags == "-czf" and self.fail == "backup":
            return 255, "ssh: connect to host: Connection refused\n"
        if flags == "-xzf" and self.fail == "restore":
            return 1, "tar: short read\n"
        if flags in ("-czf", "-cf"):
            out = subprocess.run(["tar", "-C", str(self.root), flags, "-", "."],
                                 capture_output=True, check=True).stdout
            if sink is not None:
                sink.write(out)
            if on_bytes:
                on_bytes(len(out))
            return 0, ""
        if flags == "-xzf":
            subprocess.run(["tar", "-C", str(self.root), "-xzf", "-"],
                           input=Path(stdin_path).read_bytes(), check=True)
            if on_bytes:
                on_bytes(Path(stdin_path).stat().st_size)
            return 0, ""
        raise AssertionError(cmd)


class FakeClaim:
    def __init__(self, route: str = "board-ssh", why: str = "") -> None:
        self.route, self.why = route, why

    def claimable(self) -> str:
        return ""

    def lock_route(self, what: str) -> str:
        if self.route == "locked":
            raise ClaimLockedError(f"{what} needs the claiming key: {self.why}")
        return self.route

    def ssh_argv(self, command=(), *, tty=False) -> list[str]:
        return ["fake-ssh", "board", *command]


def session(claim: Any = None, shell_id: str = "0x72bb0a36") -> Any:
    return SimpleNamespace(
        candidate=SimpleNamespace(board_id=BOARD), claim=claim or FakeClaim(),
        identity=lambda: SimpleNamespace(shell_id=shell_id))


@pytest.fixture
def rig(tmp_path: Path):
    persist = tmp_path / "board_persist"
    (persist / "ssh").mkdir(parents=True)
    (persist / "ssh" / "authorized_keys").write_bytes(b"ssh-ed25519 AAAAthrowaway test\n")
    (persist / "identity").write_bytes(b"label=board-1\n")
    (persist / "touch_cal").write_bytes(b"\x01\x02\x03\x04" * 8)
    board = FakeBoardPersist(persist)
    clock = {"t": 1_800_000_000.0}
    svc = PersistBackupService(root=tmp_path / "backups", runner=board,
                               clock=lambda: clock["t"])
    return SimpleNamespace(svc=svc, board=board, persist=persist, clock=clock, tmp=tmp_path)


def test_backup_writes_archive_and_manifest(rig) -> None:
    seen: list[tuple] = []
    m = rig.svc.backup(session(), progress=lambda *a: seen.append(a))
    folder = rig.svc.board_dir(BOARD)
    arc = folder / m["archive"]
    assert arc.is_file() and arc.stat().st_size == m["size"]
    import hashlib
    assert hashlib.sha256(arc.read_bytes()).hexdigest() == m["sha256"]
    assert m["board_id"] == BOARD and m["shell_id"] == "0x72bb0a36"
    assert m["created_utc"] == "2027-01-15T08:00:00Z"
    assert {f["path"]: f["size"] for f in m["files"]} == {
        "ssh/authorized_keys": 31, "identity": 14, "touch_cal": 32}
    on_disk = json.loads((folder / f"persist-{m['id']}.json").read_text())
    assert on_disk["sha256"] == m["sha256"] and on_disk["file_count"] == 3
    assert (arc.stat().st_mode & 0o077) == 0           # keys: owner only
    assert rig.board.calls[0][2:] == ["tar", "-C", "/persist", "-czf", "-", "."]
    assert seen[0][0] == "backup" and seen[-1][1] == m["size"]
    assert not list(folder.glob("*.part"))


def test_manifest_and_logs_carry_no_file_contents(rig, caplog) -> None:
    with caplog.at_level("DEBUG"):
        rig.svc.backup(session())
    text = (rig.svc.board_dir(BOARD) / next(
        p.name for p in rig.svc.board_dir(BOARD).glob("*.json"))).read_text()
    assert "AAAAthrowaway" not in text and "AAAAthrowaway" not in caplog.text


def test_list_newest_first_and_recent_guard(rig) -> None:
    assert rig.svc.list(BOARD) == [] and rig.svc.has_recent_backup(BOARD) is False
    a = rig.svc.backup(session())
    rig.clock["t"] += 3600
    b = rig.svc.backup(session())
    assert [m["id"] for m in rig.svc.list(BOARD)] == [b["id"], a["id"]]
    assert rig.svc.has_recent_backup(BOARD)
    rig.clock["t"] += 8 * 24 * 3600
    assert not rig.svc.has_recent_backup(BOARD)
    assert rig.svc.has_recent_backup(BOARD, max_age_s=30 * 24 * 3600)
    assert rig.svc.list("other@board") == []           # per board


def test_same_second_backups_get_distinct_ids(rig) -> None:
    a, b = rig.svc.backup(session()), rig.svc.backup(session())
    assert a["id"] != b["id"] and len(rig.svc.list(BOARD)) == 2


def test_round_trip_after_the_card_is_wiped(rig) -> None:
    m = rig.svc.backup(session())
    for p in sorted(rig.persist.rglob("*"), reverse=True):      # the re-make wipes it
        p.unlink() if p.is_file() else p.rmdir()
    assert not any(rig.persist.iterdir())
    out = rig.svc.restore(session(), m["id"])
    assert out["verified"] is True and out["differences"]["missing"] == []
    assert (rig.persist / "identity").read_bytes() == b"label=board-1\n"
    assert (rig.persist / "ssh" / "authorized_keys").read_bytes().startswith(b"ssh-ed25519")
    assert rig.board.calls[-2][-2:] == ["-xzf", "-"]
    assert rig.board.calls[-1][5] == "-cf"                      # re-listed afterwards


def test_restore_reports_a_mismatch_plainly(rig) -> None:
    m = rig.svc.backup(session())
    real = rig.board.__call__

    def lossy(argv, **kw):                                      # the board drops one file and
        rc, err = real(argv, **kw)                              # truncates another after untar
        if "-xzf" in argv:
            (rig.persist / "identity").unlink()
            (rig.persist / "touch_cal").write_bytes(b"x")
        return rc, err

    rig.svc.runner = lossy
    out = rig.svc.restore(session(), m["id"])
    assert out["verified"] is False
    assert out["differences"]["missing"] == ["identity"]
    assert out["differences"]["changed"] == ["touch_cal (backup 32 bytes, board 1 bytes)"]
    assert "does not match" in out["note"] and "identity" in out["note"]


def test_restore_notes_files_made_since_but_still_verifies(rig) -> None:
    m = rig.svc.backup(session())
    (rig.persist / "newer").write_bytes(b"1")
    out = rig.svc.restore(session(), m["id"])
    assert out["verified"] and out["differences"]["extra"] == ["newer"]
    assert "made since" in out["note"]


def test_restore_refuses_a_changed_archive_before_sending(rig) -> None:
    m = rig.svc.backup(session())
    arc = rig.svc.board_dir(BOARD) / m["archive"]
    arc.write_bytes(arc.read_bytes() + b"\0")
    n = len(rig.board.calls)
    with pytest.raises(ActionFailedError, match="no longer matches"):
        rig.svc.restore(session(), m["id"])
    assert len(rig.board.calls) == n


def test_failed_ssh_keeps_no_partial_backup(rig) -> None:
    rig.board.fail = "backup"
    with pytest.raises(ActionFailedError, match="Connection refused"):
        rig.svc.backup(session())
    assert rig.svc.list(BOARD) == []
    assert not list(rig.svc.board_dir(BOARD).glob("*"))


def test_failed_restore_says_so(rig) -> None:
    m = rig.svc.backup(session())
    rig.board.fail = "restore"
    with pytest.raises(ActionFailedError, match="short read"):
        rig.svc.restore(session(), m["id"])


def test_unknown_backup_id_is_absent(rig) -> None:
    from harness_manager.core.errors import AbsentError

    with pytest.raises(AbsentError):
        rig.svc.restore(session(), "nope")


@pytest.mark.parametrize("act", ["backup", "restore"])
def test_refused_when_the_board_is_not_claimed(rig, act) -> None:
    m = rig.svc.backup(session())
    n = len(rig.board.calls)
    unclaimed = session(FakeClaim(route=""))
    with pytest.raises(RefusedError, match="has not claimed"):
        (rig.svc.backup(unclaimed) if act == "backup" else rig.svc.restore(unclaimed, m["id"]))
    locked = session(FakeClaim(route="locked", why="claimed by another key"))
    with pytest.raises(ClaimLockedError):
        rig.svc.backup(locked)
    assert len(rig.board.calls) == n                            # nothing was sent


def test_unavailable_without_a_claim_adapter(rig) -> None:
    from harness_manager.core.errors import UnavailableError

    s = SimpleNamespace(candidate=SimpleNamespace(board_id=BOARD), claim=None)
    with pytest.raises(UnavailableError):
        rig.svc.backup(s)


def test_archive_files_reads_headers_only() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("./a/b")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"abc"))
        d = tarfile.TarInfo("./a")
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
    buf.seek(0)
    assert pb.archive_files(buf, "r:gz") == [{"path": "a/b", "size": 3}]


# -- the API -------------------------------------------------------------------------------


class Router:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}

    def get(self, path):
        return lambda fn: self.routes.setdefault(("GET", path), fn) and fn

    def post(self, path):
        return lambda fn: self.routes.setdefault(("POST", path), fn) and fn


def api(rig, claimed: bool = True):
    from harness_manager.daemon import persist_api

    s = session(FakeClaim() if claimed else FakeClaim(route=""))
    jobs: list[Any] = []

    class Jobs:
        def submit(self, kind, bid, fn):
            events: list[tuple] = []
            result = fn(lambda *a, **k: events.append(a))
            jobs.append((kind, bid, result, events))
            return SimpleNamespace(id="j1")

    class Gates:
        from contextlib import contextmanager

        @staticmethod
        @contextmanager
        def op(bid):
            yield

    router = Router()
    d = SimpleNamespace(persist_backup=rig.svc, jobs=Jobs(), gates=Gates(), engine=None,
                        leases=None, bus=None)
    ctx = SimpleNamespace(daemon=d, api=router, board=lambda bid: s,
                          accepted=lambda job: {"accepted": job.id})
    persist_api.register(ctx)
    return router.routes, jobs


def body_of(resp) -> dict:
    return json.loads(resp.body)


def test_api_backup_list_restore_as_jobs(rig, monkeypatch) -> None:
    from harness_manager.services import slots

    monkeypatch.setattr(slots.SlotService, "check_lease", lambda self, s, what: None)
    routes, jobs = api(rig)
    pre = "/boards/{bid:path}/persist/"
    assert routes[("POST", pre + "backup")](BOARD, None) == {"accepted": "j1"}
    kind, _bid, result, events = jobs[-1]
    assert kind == "persist_backup" and result["backup"]["file_count"] == 3
    assert "files" not in result["backup"] and events
    listing = body_of(routes[("GET", pre + "backups")](BOARD))
    assert listing["ok"] and listing["recent"] is True
    bid = listing["backups"][0]["id"]
    assert "files" not in listing["backups"][0]
    from harness_manager.core.errors import UsageError

    with pytest.raises(UsageError):                              # no confirm
        routes[("POST", pre + "restore")](BOARD, {"backup_id": bid})
    assert routes[("POST", pre + "restore")](BOARD, {"confirm": True, "backup_id": bid})
    kind, _b, result, _e = jobs[-1]
    assert kind == "persist_restore" and result["verified"] is True


def test_api_refuses_an_unclaimed_board_before_any_job(rig) -> None:
    routes, jobs = api(rig, claimed=False)
    with pytest.raises(RefusedError):
        routes[("POST", "/boards/{bid:path}/persist/backup")](BOARD, None)
    assert jobs == [] and rig.board.calls == []


# -- the CLI -------------------------------------------------------------------------------


def test_cli_verbs_join_the_card_verb(rig) -> None:
    import argparse
    from contextlib import contextmanager

    from harness_manager.cli import cmd_persist, cmd_slots

    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="cmd")
    verbs = cmd_slots.register(sub)
    cmd_persist.register(sub, verbs)
    emitted: list[Any] = []
    cand = SimpleNamespace(board_id=BOARD)

    def ctx_for(argv: list[str]) -> Any:
        args = top.parse_args(argv)

        class C:
            def __init__(self) -> None:
                self.args, self.err = args, io.StringIO()
                self.engine = SimpleNamespace(persist_service=rig.svc)

            def emit(self, r):
                emitted.append(r)

            def candidate(self):
                return cand

            def confirm(self, q):
                emitted.append(("confirm", q))

            @contextmanager
            def board(self, note=""):
                yield cand, session()

        return args, C()

    args, c = ctx_for(["card", "backup", "192.168.10.101"])
    assert args.fn(c) == 0 and emitted[-1].data["backup"]["file_count"] == 3
    assert "files" not in emitted[-1].data["backup"]
    args, c = ctx_for(["card", "backups", "192.168.10.101"])
    assert args.fn(c) == 0 and len(emitted[-1].data["backups"]) == 1
    args, c = ctx_for(["card", "restore", "192.168.10.101", "--yes"])
    assert args.fn(c) == 0 and emitted[-1].data["verified"] is True
