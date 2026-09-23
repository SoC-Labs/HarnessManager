"""Team T13: what crosses the wire. JSON <-> core objects, errors, statuses, topics, log redaction.

Every check has a negative twin.
"""

from __future__ import annotations

import json
import logging

import pytest

from socharness.cli.output import error_json, jsonable, reading_json
from socharness.client.codec import error_from_json, from_json
from socharness.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    ExitCode,
    HarnessError,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
    PortBoundError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from socharness.core.model import (
    BoardIdentity,
    BoardInfo,
    Candidate,
    Check,
    Health,
    Link,
    LinkKind,
    Reading,
)
from socharness.core.pack import BackupRecord, DeployResult, OverlayRef, PreflightItem
from socharness.core.services import DebugStatus
from socharness.core.session import LockOwner
from socharness.daemon.server import RedactToken
from socharness.daemon.wire import (
    HTTP_STATUS,
    encode_event,
    http_status,
    owner_json,
    parse_topics,
    topic_matches,
    wants,
)


def wire(obj):
    """What the daemon sends: jsonable, through real JSON text."""
    return json.loads(json.dumps(jsonable(obj)))


IDENTITY = BoardIdentity(board_type="mps3", shell_id="0x3f1a560f", rm_id="0x01000001",
                         rm_name="nanosoc", harness_version="1.0.0", firmware_sha="cb31b0f2",
                         firmware_dirty=True, features=("clcd", "windowed"),
                         build_check=Check.MISMATCH, harness_impl="bare-metal", proto="0.9")
CANDIDATE = Candidate(pack="mps3", board_id="mps3@usb:/dev/ttyUSB10",
                      links=(Link(LinkKind.ETHERNET, "192.168.10.101:6900", "shell"),
                             Link(LinkKind.USB_SERIAL, "serial:///dev/ttyUSB10", "MCC")),
                      label="MPS3", evidence="answered ping", identity=IDENTITY)
INFO = BoardInfo(candidate=CANDIDATE, identity=IDENTITY,
                 health=Health(reachable=True, control_channel="idle", counters={"rx": 3},
                               notes=("a", "b")),
                 capabilities=frozenset({"deploy_partial", "reset_dut"}),
                 unavailable={"reboot_board": "needs the Debug USB cable"})

OBJECTS = [
    INFO,
    CANDIDATE,
    Candidate(pack="mps3", board_id="x", links=()),                      # identity None
    Reading(name="mcc_temp", value=35.5, unit="degC", source="mcc-console", observed_at=12.5),
    Reading.unavailable("board_power", "W", "needs a smart plug", source="none"),
    OverlayRef(name="synth", rm_id="0x01007a57", static_id="0x3f1a560f", source="store:ab",
               size_bytes=512, ip_class="open"),
    PreflightItem(name="shell_id matches", check=Check.UNCHECKED, detail="d", identity=True),
    DeployResult(rm_id="0x01007a57", verified=True, seconds=3.25, transport="tcp+windowed"),
    BackupRecord(path="/tmp/b.zip", sha256="ab" * 32, created_at=1.0, files=3,
                 volume_label="V2M-MPS3"),
    DebugStatus(state="up", gdb_port=3333, telnet_port=4444, tcl_port=6666,
                config=("target/a.cfg", "b.cfg"), pid=42, detail="ok"),
    LockOwner(user="u", host="h", pid=7, since=1.5, note="socharnessd: web"),
]


@pytest.mark.parametrize("obj", OBJECTS, ids=lambda o: type(o).__name__)
def test_every_core_object_survives_the_wire_unchanged(obj):
    assert from_json(type(obj), wire(obj)) == obj


def test_extra_keys_the_api_adds_are_ignored():
    r = Reading(name="t", value=1.0, unit="C", source="s", observed_at=3.0)
    assert from_json(Reading, wire(reading_json(r))) == r            # + available, age_s
    owner = LockOwner(user="u", host="h", pid=7, since=1.5)
    assert from_json(LockOwner, owner_json(owner)) == owner          # + text


@pytest.mark.parametrize("cls, data", [
    (PreflightItem, {"name": "x", "check": "maybe"}),                # not a Check
    (Candidate, {"pack": "mps3", "board_id": "b", "links": "not-a-list"}),
    (Reading, {"name": "t", "value": "hot", "unit": "C", "source": "s"}),
    (DeployResult, {"rm_id": "0x1", "verified": "yes", "seconds": 1.0}),
    (BoardInfo, ["not", "an", "object"]),
])
def test_negative_twin_malformed_objects_are_usage_errors(cls, data):
    with pytest.raises(UsageError):
        from_json(cls, data)


ERRORS = [
    UsageError("bad", hint="h"),
    AbsentError("gone"),
    HeldError("in use", holder="u on h (pid 1)", hint="wait"),
    PortBoundError("port"),
    ActionFailedError("failed", hint="retry"),
    UnreachableError("no route"),
    AlreadyError("already"),
    UnavailableError("reboot_board", "needs the Debug USB cable"),
    NothingOnTargetError("no dap"),
    IncompatibleError("wrong shell", hint="use another"),
    RefusedError("refused"),
    HarnessError("internal error: boom"),
]


@pytest.mark.parametrize("exc", ERRORS, ids=lambda e: type(e).__name__)
def test_errors_rebuild_with_the_same_code_message_and_json(exc):
    exc.data = {"overlay": {"name": "x"}}
    rebuilt = error_from_json(wire(error_json(exc))["error"])
    assert type(rebuilt) is type(exc) and rebuilt.code == exc.code
    assert error_json(rebuilt) == wire(error_json(exc))


@pytest.mark.parametrize("err, code", [
    ({"code": 99, "message": "from the future"}, ExitCode.FAILED),
    ({"code": "garbage"}, ExitCode.FAILED),
    ({}, ExitCode.FAILED),
])
def test_negative_twin_unknown_error_codes_become_failed(err, code):
    exc = error_from_json(err)
    assert type(exc) is HarnessError and exc.code == code and exc.message


def test_http_status_follows_the_api_table():
    table = {ExitCode.USAGE: 400, ExitCode.ABSENT: 404, ExitCode.HELD: 409,
             ExitCode.ALREADY: 409, ExitCode.UNREACHABLE: 502, ExitCode.UNAVAILABLE: 422,
             ExitCode.NOTHING_ON_TARGET: 422, ExitCode.INCOMPATIBLE: 409,
             ExitCode.REFUSED: 409}
    assert HTTP_STATUS == table
    for exc in ERRORS:
        assert http_status(exc) == table.get(exc.code, 500)


@pytest.mark.parametrize("exc", [HarnessError("x"), PortBoundError("p"), ActionFailedError("a")])
def test_negative_twin_anything_else_is_500(exc):
    assert http_status(exc) == 500


def test_topic_filters_follow_the_bus_rule():
    assert parse_topics("board.*, deploy.* ,") == ("board.*", "deploy.*")
    assert parse_topics(None) == ("*",) and parse_topics("") == ("*",)
    assert topic_matches("deploy.*", "deploy.progress") and topic_matches("*", "x.y")
    assert topic_matches("job.done", "job.done")
    assert wants(("board.*", "job.*"), "job.failed")


def test_negative_twin_topic_filters_do_not_overmatch():
    assert not topic_matches("deploy.*", "deployx.progress")
    assert not topic_matches("job.done", "job.failed")
    assert not wants(("board.*",), "console.line")


def test_an_event_frame_has_the_api_shape():
    from socharness.core.events import Event

    frame = json.loads(encode_event(Event("deploy.done", "b", {"result": DeployResult(
        rm_id="0x1", verified=True, seconds=1.0)}, at=5.0)))
    assert set(frame) == {"topic", "board_id", "data", "at"}
    assert frame["data"]["result"]["verified"] is True and frame["at"] == 5.0


def test_the_log_filter_masks_tokens():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                               '%s - "WebSocket %s" [accepted]',
                               ("127.0.0.1:5", "/api/v1/events?token=SECRET&topics=job.*"), None)
    assert RedactToken().filter(record)
    assert "SECRET" not in record.getMessage() and "token=***&topics=job.*" in record.getMessage()


def test_negative_twin_the_log_filter_leaves_other_lines_alone():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "started %s",
                               ("ok",), None)
    assert RedactToken().filter(record) and record.getMessage() == "started ok"
    assert record.args == ("ok",)
