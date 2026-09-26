"""HUB-SD units: fpgahub's texts as the door reads them, the delta, the backup. Each has a twin.

The texts are fpgahub v0.3.0's own formats: ``program dispatched: …`` (program.py:497-502),
``POST /targets/T/program: timed out`` (ipc.py:139-140 through cli.py ``_die``), the
``--list`` table (cli.py:1024-1051).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.services.update import hub_door
from harness_manager.services.update.schema import (
    Asset,
    Compat,
    Component,
    HarnessIdentity,
    HarnessRelease,
)
from harness_manager_mps3.hub_sd import (
    NANOSOC_BIT,
    SshUploader,
    multipart,
    parse_journal,
    parse_program_list,
    parse_program_reply,
    read_bit_backup,
    sd_delta,
    sha_matches,
    write_bit_backup,
)

T = "mps3_01_pl"
OK_LINE = (f"program dispatched: board={T} method=sd plugin=sd_install ok=True "
           "part=xcku115-flvb1760-1-c sha256=0123456789ab dur=68.44s")


def test_a_client_timeout_is_read_as_timeout_not_failure():
    r = parse_program_reply(1, f"POST /targets/{T}/program: timed out\n")
    assert r.state == "timeout"


def test_twin_an_http_refusal_and_an_answer_are_not_timeouts():
    assert parse_program_reply(1, f"POST /targets/{T}/program → HTTP 409: "
                                  "{'error': 'part_mismatch'}").state == "refused"
    assert parse_program_reply(1, f"POST /targets/{T}/program → HTTP 400: sd_install: "
                                  "mount failed errno=16").state == "failed"
    ok = parse_program_reply(0, f"ok {T} program method=sd plugin=sd_install\n  bitstream: "
                                "part=x build=- sha256=0123456789abcdef…\n")
    assert ok.state == "ok" and ok.fingerprint == "0123456789abcdef"


def test_the_journal_line_names_the_write_and_its_sha():
    done = parse_journal(f"sd_mount: mounting /dev/sdb1\n{OK_LINE}\n", T)
    assert done.ok and done.sha256 == "0123456789ab" and done.dur_s == 68.44
    assert done.source == "journal"


def test_twin_another_board_or_method_is_not_our_write_and_a_400_is_a_failure():
    other = OK_LINE.replace(T, "pynq_z2_01_pl")
    assert parse_journal(other, T) is None
    assert parse_journal(OK_LINE.replace("method=sd", "method=default"), T) is None
    bad = parse_journal(f'INFO: 127.0.0.1 - "POST /api/v1/targets/{T}/program HTTP/1.1" 400', T)
    assert bad is not None and not bad.ok and "HTTP 400" in bad.detail


def test_the_program_list_says_whether_sd_is_available():
    text = ("┃ Method ┃ Plugin ┃ Confirm? ┃ Available? ┃ Description ┃\n"
            "│ default │ vivado_jtag │ no │ yes │ JTAG │\n"
            "│ sd │ sd_install │ no │ yes │ SD │\n\n"
            "last programmed fingerprint: 0123456789abcdef…\n")
    info = parse_program_list(text)
    assert info.methods["sd"] == (True, "") and info.last_fingerprint == "0123456789abcdef"


def test_twin_an_unavailable_or_missing_sd_method():
    info = parse_program_list("│ sd │ sd_install │ no │ no (plugin 'sd_install' not "
                              "registered on this daemon) │ SD │\n")
    assert info.methods["sd"][0] is False and "not registered" in info.methods["sd"][1]
    assert "sd" not in parse_program_list("│ default │ vivado_jtag │ no │ yes │ x │\n").methods


def test_sha_prefixes_match_only_when_long_enough():
    full = "0123456789abcdef" * 4
    assert sha_matches(full, "0123456789ab") and sha_matches(full, full.upper())
    assert not sha_matches(full, "0123") and not sha_matches(full, "ffff456789ab")


def test_the_delta_is_nanosoc_bit_only(tmp_path):
    old = {"config.txt": "a" * 64, NANOSOC_BIT: "b" * 64}
    new = {"CONFIG.TXT": "a" * 64, NANOSOC_BIT.lower(): "c" * 64}   # FAT: case-blind
    assert sd_delta(new, old) == [NANOSOC_BIT.lower()]


def test_twin_a_changed_board_txt_is_in_the_delta(tmp_path):
    f = tmp_path / "board.txt"
    f.write_bytes(b"APPFILE: Other\\other.txt\n")
    old = {"MB/HBI0309C/board.txt": "0" * 64, NANOSOC_BIT: "b" * 64}
    new = {"MB/HBI0309C/board.txt": f, NANOSOC_BIT: "c" * 64}
    assert set(sd_delta(new, old)) == {"MB/HBI0309C/board.txt", NANOSOC_BIT}


def test_the_backup_round_trips(tmp_path):
    bit = tmp_path / "old.bit"
    bit.write_bytes(b"the previous bitstream")
    rec = write_bit_backup(tmp_path / "b", bit, version="1.0.0", label="hub:x", now=1e9)
    out, sha, manifest = read_bit_backup(rec, tmp_path / "x")
    assert out.read_bytes() == bit.read_bytes() and manifest["version"] == "1.0.0"
    assert Path(rec.path + ".sha256").is_file()


def test_twin_a_tampered_backup_is_refused(tmp_path):
    bit = tmp_path / "old.bit"
    bit.write_bytes(b"the previous bitstream")
    rec = write_bit_backup(tmp_path / "b", bit, version="1.0.0", label="hub:x", now=1e9)
    with zipfile.ZipFile(rec.path, "a") as zf:
        zf.writestr("extra", b"x")
    with pytest.raises(RefusedError):
        read_bit_backup(rec, tmp_path / "x")


def test_the_multipart_body_carries_the_fields_and_the_file():
    body, ctype = multipart({"kind": "bitstream"}, "file", "x.bit", b"\x00BIT")
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and b'name="kind"\r\n\r\nbitstream' in body
    assert b'filename="x.bit"' in body and b"\x00BIT\r\n" in body


def test_the_uploader_quotes_the_path_and_never_prompts():
    argv = SshUploader("hub.example", jump="gw.example").argv(".cache/hm/a b.bit")
    assert argv[:1] == ["ssh"] and "BatchMode=yes" in argv and argv[argv.index("-J") + 1] == "gw.example"
    assert "'.cache/hm/a b.bit.part'" in argv[-1] and argv[-2] == "hub.example"


def test_twin_the_uploader_refuses_an_option_for_a_host():
    with pytest.raises(UsageError):
        SshUploader("-oProxyCommand=evil")


# --- the planner's half ---------------------------------------------------------------------------


def _rel(version: str, files: dict[str, str]) -> HarnessRelease:
    sd = Component("sd-HBI0309C", "mcc_sd", "sd",
                   Asset(f"{version}.zip", "u", "a" * 64, 1), files=files)
    return HarnessRelease(version, "current", HarnessIdentity("0x3f1a560f"), Compat(), (sd,))


def _plan():
    return SimpleNamespace(base=True, blockers=[], warnings=[], via="", hub={},
                           board_phrase="", auto_revert=False)


DOOR = {"available": True, "target": T, "hub": "hub", "holder": "me", "mine": True,
        "queue": [], "only_paths": [NANOSOC_BIT]}


def test_a_signed_delta_outside_nanosoc_bit_blocks_the_hub_door():
    old = _rel("1.0.0", {"config.txt": "1" * 64, NANOSOC_BIT: "2" * 64})
    new = _rel("1.1.0", {"config.txt": "9" * 64, NANOSOC_BIT: "3" * 64})
    plan = _plan()
    board = SimpleNamespace(hub_sd=DOOR, has_storage=False, has_controller=True)
    hub_door.apply(plan, new, None, board, via=None, running=old, have_token=False)
    assert plan.via == "hub" and any("config.txt" in b for b in plan.blockers)


def test_twin_a_signed_delta_of_nanosoc_bit_alone_goes_through():
    old = _rel("1.0.0", {"config.txt": "1" * 64, NANOSOC_BIT: "2" * 64})
    new = _rel("1.1.0", {"config.txt": "1" * 64, NANOSOC_BIT: "3" * 64})
    plan = _plan()
    board = SimpleNamespace(hub_sd=DOOR, has_storage=False, has_controller=True)
    hub_door.apply(plan, new, None, board, via=None, running=old, have_token=False)
    assert plan.via == "hub" and not plan.blockers and plan.auto_revert
    local = _plan()
    hub_door.apply(local, new, None, SimpleNamespace(hub_sd=DOOR, has_storage=True,
                                                    has_controller=True),
                   via=None, running=old, have_token=False)
    assert local.via == ""                     # a Debug USB here wins, unless via="hub"
