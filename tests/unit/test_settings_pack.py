"""SET-PACK: board packs declare their own settings; the MPS3 pack's rows. Each probe has its
negative twin, so it bites.

docs/design/SETTINGS.md §3.2 (packs declare rows), §4.2 (the pack layer), Appendix A (the MPS3
and Boards rows). ``BoardPack.settings()`` is CCR SET-PACK-1; the MPS3 rows SET-PACK-2.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from itertools import combinations
from pathlib import Path

import pytest

from harness_manager.core.capabilities import CapabilitySpec
from harness_manager.core.errors import UsageError
from harness_manager.core.model import Candidate
from harness_manager.core.pack import BoardPack, BoardSession, ProbeHints
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.settings import (
    MachinePolicy,
    Resolver,
    Schema,
    Setting,
    coerce,
    core_schema,
    with_packs,
)
from harness_manager.settings.packs import pack_rows
from harness_manager_mps3 import settings as mps3_settings
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t1_fakes import FakePack

SRC = Path(__file__).resolve().parents[2] / "src"
PACK_SRC = SRC / "harness_manager_mps3"
PACK_ENV = "HARNESS_MANAGER_MPS3_"
NO_KEYRING = {"HARNESS_MANAGER_KEYRING": "off"}     # never the user's OS keyring


def mps3_rows(**kw) -> tuple[Setting, ...]:
    return tuple(Mps3Pack(**kw).settings())


def rows_by_key(**kw) -> dict[str, Setting]:
    return {s.key: s for s in mps3_rows(**kw)}


# --- a second pack, declared here and nowhere in src ----------------------------------------------


class Kr260Pack(BoardPack):
    """A KR260-like pack: its own rows, its own board table, no UI or core code."""

    name = "kr260"
    title = "AMD Kria KR260 (test)"

    def __init__(self, extra: Iterable[Setting] = (), *, broken: bool = False) -> None:
        self._extra = tuple(extra)
        self._broken = broken

    def capability_specs(self) -> Iterable[CapabilitySpec]:
        return ()

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        return []

    def open(self, candidate: Candidate) -> BoardSession:
        raise NotImplementedError

    def settings(self) -> Iterable[Setting]:
        if self._broken:
            raise RuntimeError("this pack's settings() is broken")
        return (
            Setting("kr260.console.pace_ms", "int", 5, "Consoles",
                    "Delay between characters typed into the PS UART", scope="pack",
                    pack="kr260", apply="reopen"),
            Setting("kr260.jtag.cable", "str", "digilent", "Debug", "The JTAG cable",
                    scope="pack", pack="kr260"),
            Setting("boards.*.bootmode.default", "enum", "sd", "Boards",
                    "Where the KR260 boots from", scope="board", pack="kr260",
                    choices=("sd", "qspi", "jtag")),
            *self._extra,
        )


# --- SET-PACK-1: the protocol ------------------------------------------------------------------------


def test_a_pack_declares_no_settings_unless_it_says_so():
    fake = FakePack()
    assert tuple(fake.settings()) == () and pack_rows(fake) == ()
    schema, layer = with_packs(core_schema(), [fake])
    assert schema.rows == core_schema().rows and layer == {}
    assert pack_rows(object()) == ()                 # not a BoardPack at all: still no rows


def test_negative_twin_the_mps3_pack_says_so():
    rows = pack_rows(Mps3Pack())
    # +4: SLOT-TIMING; +1: IDENTITY's mps3.identity.ip_pool
    assert len(rows) == 39 and {s.pack for s in rows} == {"mps3"}
    schema, layer = with_packs(core_schema(), [Mps3Pack()])
    assert len(schema.rows) == len(core_schema().rows) + 39
    assert layer["mps3.console.pace_ms"] == 20


# --- the MPS3 rows: the prefix rule ------------------------------------------------------------------


def test_mps3_rows_sit_under_mps3_or_in_a_board_table():
    rows = mps3_rows()
    for s in rows:
        assert s.pack == "mps3", s.key
        assert s.key.startswith("mps3.") or s.collection == "boards", s.key
    schema = Schema(core_schema().rows).extend(rows, pack="mps3")
    assert schema.spec("boards.lab.hub.target").pack == "mps3"
    assert schema.spec('boards."mps3@192.168.10.101:6900".hub.shares.mcc').key \
        == "boards.*.hub.shares.*"
    assert schema.spec("mps3.identify.port").env == f"{PACK_ENV}IDENTIFY_PORT"


def _pack_row(key: str, pack: str = "mps3") -> Setting:
    return Setting(key, "str", "", "Boards", "doc", pack=pack)


@pytest.mark.parametrize("bad, why", [
    (_pack_row("tools.mine"), "under 'mps3.'"),                 # outside the prefix
    (_pack_row("hubs.*.mine"), "under 'mps3.'"),                # a hub row is the core's
    (_pack_row("kr260.console.pace_ms"), "under 'mps3.'"),      # another pack's prefix
    (_pack_row("boards.*.name"), "declared twice"),              # a core key
    (_pack_row("boards.*.power.kind"), "declared twice"),        # the core's power table
    (_pack_row("mps3.console.pace_ms"), "declared twice"),       # its own key, twice
    (_pack_row("boards.*.hub.shares.mcc"), "declared twice"),    # inside its own pattern
    (_pack_row("mps3.x", pack="kr260"), "declared by pack"),     # says another pack
])
def test_negative_twin_a_wrong_prefix_or_a_duplicate_is_refused(bad, why):
    schema = Schema(core_schema().rows)
    with pytest.raises(ValueError, match=re.escape(why)):
        schema.extend((*mps3_rows(), bad), pack="mps3")
    assert schema.rows == core_schema().rows                    # all or nothing


def test_every_mps3_default_passes_its_own_row():
    for s in mps3_rows():
        if s.default is not None:
            assert coerce(s, s.default) == s.default, s.key


def test_the_rows_carry_type_scope_secret_and_apply():
    r = rows_by_key()
    assert not any(s.secret for s in r.values())                 # the MPS3 has no secret
    assert r["mps3.openocd_cfg_dir"].lockable and r["mps3.openocd_cfg_dir"].scope == "machine"
    assert r["mps3.console.pace_ms"].apply == "reopen" and r["mps3.console.pace_ms"].ui
    assert r["mps3.rbb_port"].apply == "restart" and not r["mps3.rbb_port"].ui   # a dev seam
    assert r["mps3.overlay_dirs"].env_split == "pathsep"
    assert {s.scope for k, s in r.items() if k.startswith("boards.")} == {"board"}
    for s in r.values():
        assert s.doc and s.section in ("Tools", "Harness + kits", "Consoles", "Debug",
                                       "Advanced", "Boards"), s.key


def test_negative_twin_bad_values_are_refused_by_the_rows():
    r = rows_by_key()
    for key, bad in (("mps3.console.pace_ms", 900), ("mps3.mcc.pace_ms", 10),
                     ("mps3.identify.port", 70000), ("boards.*.hub.target", "mps3 01"),
                     ("boards.*.hub.shares.*", "/tmp/tty"), ("boards.*.xvc.reach", "ssh"),
                     ("boards.*.xvc.user", "-oProxyCommand=x"), ("boards.*.hub.baud", 0),
                     ("mps3.tunnelled", "maybe"), ("boards.*.sysmon.backend", "vivado")):
        with pytest.raises(UsageError):
            coerce(r[key], bad)
    assert coerce(r["mps3.tunnelled"], " ON ", from_env=True) == "ON"
    assert coerce(r["mps3.identify.broadcast"], "127.0.0.1:1,127.0.0.2", from_env=True) \
        == ["127.0.0.1:1", "127.0.0.2"]


# --- the rows match what the pack validates (one list) ----------------------------------------------


def _fields(table: str) -> set[str]:
    return {s.parts[3] for s in mps3_rows() if s.parts[:3] == ("boards", "*", table)}


HUB_SAMPLES = {"target": "kr260_01_ps", "board": "mps3_02", "shares": {"mcc": "/dev/x/tty_00"},
               "baud": 57600, "start_shares": True}


def test_the_hub_rows_are_the_hub_tables_per_board_keys():
    from harness_manager_mps3 import hub

    assert _fields("hub") == set(HUB_SAMPLES)
    for key, value in HUB_SAMPLES.items():                       # each one the parser takes
        cfg = hub.parse_hub_table({"host": "hub", key: value})
        assert getattr(cfg, key) == value
    r, dflt = rows_by_key(), hub.HubConfig()
    for key in ("target", "baud", "start_shares", "board"):      # and the same defaults
        assert r[f"boards.*.hub.{key}"].default == getattr(dflt, key), key


def test_negative_twin_a_key_the_hub_parser_refuses_is_not_a_row():
    from harness_manager_mps3 import hub

    with pytest.raises(UsageError, match="unknown keys: sharez"):
        hub.parse_hub_table({"host": "hub", "sharez": {}})
    assert "sharez" not in _fields("hub")


def test_the_xvc_rows_are_the_xvc_tables_keys(monkeypatch):
    from harness_manager_mps3 import hub, xvc

    cand = Candidate(pack="mps3", board_id="mps3@b:6900", links=())
    assert _fields("xvc") == {"reach", "user", "host"}
    assert rows_by_key()["boards.*.xvc.reach"].choices == xvc.REACH_CHOICES
    table = {"reach": "board-ssh", "user": "linaro", "host": "b"}
    monkeypatch.setattr(hub, "board_tables", lambda c: {"xvc": table})
    assert xvc.xvc_config(cand) == table
    monkeypatch.setattr(hub, "board_tables", lambda c: {"xvc": {**table, "port": 1}})
    with pytest.raises(UsageError, match="unknown keys: port"):          # the twin
        xvc.xvc_config(cand)


SYSMON_SAMPLES = {
    "xsdb": {"xsdb": "/opt/xsdb", "hw_server": "tcp:h:3121", "device": "xcku*",
             "timeout_s": 5.0, "min_interval_s": 20.0},
    "openocd": {"openocd": "/usr/bin/openocd", "adapter": ["adapter driver ftdi"],
                "speed_khz": 500, "search": ["/s"], "timeout_s": 5.0, "min_interval_s": 20.0},
}


def test_the_sysmon_rows_are_both_backends_keys():
    from harness_manager_mps3 import sysmon

    declared = _fields("sysmon")
    assert declared == {"backend"} | set(SYSMON_SAMPLES["xsdb"]) | set(SYSMON_SAMPLES["openocd"])
    for backend, table in SYSMON_SAMPLES.items():
        sysmon.make_sysmon_reader({"backend": backend, **table})
    reader, r = sysmon.make_sysmon_reader({}), rows_by_key()     # an empty table: defaults
    assert r["boards.*.sysmon.backend"].default == "xsdb"
    assert isinstance(reader, sysmon.XsdbSysmon)
    assert (r["boards.*.sysmon.hw_server"].default, r["boards.*.sysmon.device"].default) \
        == (reader.hw_server, reader.device)
    with pytest.raises(UsageError, match="unknown keys"):              # the twin
        sysmon.make_sysmon_reader({"backend": "xsdb", "speed": 1})


def test_the_estimates_row_is_the_estimates_tables_key(tmp_path):
    from harness_manager.power.config import BoardConfig
    from harness_manager_mps3 import telemetry

    assert _fields("estimates") == {"vivado_reports"}
    ok = BoardConfig("lab", tables={"estimates": {"vivado_reports": str(tmp_path)}})
    assert telemetry._estimates_from(ok) == (tmp_path, "")
    bad = BoardConfig("lab", tables={"estimates": {"reports": str(tmp_path)}})
    assert telemetry._estimates_from(bad)[0] is None                  # the twin


def test_the_defaults_are_the_readers_constants():
    from pyverify.pusher import TFTP_PORT

    from harness_manager_mps3 import constants as c
    from harness_manager_mps3 import mcc, telemetry

    r = rows_by_key()
    assert r["mps3.console.pace_ms"].default == round(c.DUT_CONSOLE_PACE_S * 1000)
    assert r["mps3.mcc.pace_ms"].default == round(mcc.DEFAULT_TIMING.pace_s * 1000)
    assert r["mps3.mcc.share_pace_ms"].default == round(mcc.SHARE_PACE_S * 1000)
    assert (r["mps3.rbb_port"].default, r["mps3.xvc_port"].default) \
        == (c.JTAG_RBB_PORT, c.XVC_PORT)
    assert (r["mps3.identify.port"].default, r["mps3.push_port"].default,
            r["mps3.tftp_port"].default) == (c.IDENTIFY_PORT, c.PUSH_PORT, TFTP_PORT)
    assert r["boards.*.sysmon.min_interval_s"].default == telemetry.SYSMON_MIN_INTERVAL_S
    # SLOT-TIMING: the OS-slot card timing rows
    from harness_manager_mps3 import os_slots

    assert (r["mps3.slot.job_timeout_s"].default, r["mps3.slot.push_timeout_s"].default) \
        == (os_slots.JOB_TIMEOUT_S, os_slots.STALL_S) == (1800.0, 900.0)
    assert (r["mps3.slot.card_write_bps"].default, r["mps3.slot.card_read_bps"].default) \
        == (os_slots.CARD_WRITE_BPS, os_slots.CARD_READ_BPS) == (70_000, 14_000)


def test_negative_twin_the_instance_values_are_the_pack_defaults():
    r = rows_by_key(console_pace_s=0.0, rbb_port=7000, push_port=7001, tftp_port=7002)
    assert (r["mps3.console.pace_ms"].default, r["mps3.rbb_port"].default,
            r["mps3.push_port"].default, r["mps3.tftp_port"].default) == (0, 7000, 7001, 7002)


# --- each row cites where it is read today ------------------------------------------------------------


def _citation_problems(read_at: dict[str, str], rows: Iterable[Setting]) -> list[str]:
    out = []
    for s in rows:
        at = read_at.get(s.key)
        if not at:
            out.append(f"{s.key}: no citation")
            continue
        path, _, line = at.rpartition(":")
        f = PACK_SRC / path
        if not f.is_file() or not line.isdigit():
            out.append(f"{s.key}: {at} is not a file:line")
            continue
        lines = f.read_text().splitlines()
        if not 1 <= int(line) <= len(lines):
            out.append(f"{s.key}: {at} is past the end of {path}")
    return out


def test_every_row_cites_where_it_is_read_today():
    rows = mps3_rows()
    assert not _citation_problems(mps3_settings.READ_AT, rows)
    # An env row's reader names its variable, or the constant that holds it, nearby.
    for s in rows:
        if not s.env:
            continue
        path, _, line = mps3_settings.READ_AT[s.key].rpartition(":")
        text = (PACK_SRC / path).read_text()
        const = re.search(rf"^(\w+) = \"{s.env}\"", text, re.M)
        near = "\n".join(text.splitlines()[max(0, int(line) - 3):int(line) + 2])
        assert s.env in near or (const and const.group(1) in near), s.key


def test_negative_twin_a_bad_citation_is_caught():
    rows = mps3_rows()[:2]
    assert _citation_problems({}, rows) == [f"{rows[0].key}: no citation",
                                            f"{rows[1].key}: no citation"]
    bad = {rows[0].key: "nowhere.py:1", rows[1].key: "pack.py:99999"}
    assert len(_citation_problems(bad, rows)) == 2


# --- precedence: the pack layer sits in its place -----------------------------------------------------

POL = "/etc/harness-manager/policy.toml"
KEY = "mps3.openocd_cfg_dir"                  # admin-lockable, with a variable
ENV = f"{PACK_ENV}OPENOCD_DIR"
VALUES = {"lock": "/lock", "env": "/env", "user": "/user", "machine": "/machine"}
ORDER = ("lock", "env", "user", "machine", "pack")


def resolver(layers, *, pack=True) -> Resolver:
    return Resolver(
        policy=MachinePolicy(POL, lock={KEY: VALUES["lock"]} if "lock" in layers else {},
                             default={KEY: VALUES["machine"]} if "machine" in layers else {}),
        env={ENV: VALUES["env"], **NO_KEYRING} if "env" in layers else dict(NO_KEYRING),
        user={KEY: VALUES["user"]} if "user" in layers else {},
        packs=[Mps3Pack()] if pack else ())


@pytest.mark.parametrize("hi, lo", list(combinations(ORDER, 2)))
def test_the_pack_layer_sits_below_user_machine_env_and_lock(hi, lo):
    got = resolver((hi, lo)).resolve(KEY)
    assert got.source == hi
    if hi != "pack":
        assert got.value == VALUES[hi]


def test_the_pack_layer_alone_says_which_pack():
    got = resolver(()).resolve(KEY)
    assert (got.source, got.where, got.value) == ("pack", "the mps3 pack", "")


def test_negative_twin_without_the_pack_there_is_no_row_and_no_pack_layer():
    with pytest.raises(UsageError, match="no such setting"):
        resolver((), pack=False).resolve(KEY)
    # The rows alone, with no pack layer: the built-in default, not "the pack".
    schema = Schema(core_schema().rows).extend(mps3_rows(), pack="mps3")
    got = Resolver(schema, env=dict(NO_KEYRING), user={}).resolve("mps3.console.pace_ms")
    assert (got.source, got.value) == ("default", 20)


def test_an_instance_value_is_the_pack_layer_and_the_user_still_wins():
    pack = Mps3Pack(console_pace_s=0.005)
    got = Resolver(env=dict(NO_KEYRING), user={}, packs=[pack]).resolve("mps3.console.pace_ms")
    assert (got.source, got.value) == ("pack", 5)
    got = Resolver(env=dict(NO_KEYRING), user={"mps3.console.pace_ms": 30},
                   packs=[pack]).resolve("mps3.console.pace_ms")
    assert (got.source, got.value) == ("user", 30)


def test_a_board_row_resolves_board_then_boards_defaults_then_pack():
    key = "boards.lab.hub.baud"
    both = {"boards.lab.hub.baud": 9600, "boards.defaults.hub.baud": 57600}
    for user, want in ((both, (9600, "user", "settings.toml")),
                       ({"boards.defaults.hub.baud": 57600},
                        (57600, "user", "settings.toml [boards.defaults]")),
                       ({}, (115200, "pack", "the mps3 pack"))):
        got = Resolver(env=dict(NO_KEYRING), user=user, packs=[Mps3Pack()]).resolve(key)
        assert (got.value, got.source, got.where) == want


def test_negative_twin_a_bad_user_value_falls_to_the_pack_layer_with_a_problem():
    got = Resolver(env=dict(NO_KEYRING), user={"boards.lab.hub.baud": "fast"},
                   packs=[Mps3Pack()]).resolve("boards.lab.hub.baud")
    assert (got.value, got.source) == (115200, "pack") and "ignored" in got.problems[0]


def test_an_env_seam_is_above_the_pack_layer_and_a_bad_one_is_skipped():
    env = {f"{PACK_ENV}IDENTIFY_PORT": "16899", **NO_KEYRING}
    got = Resolver(env=env, user={}, packs=[Mps3Pack()]).resolve("mps3.identify.port")
    assert (got.value, got.source) == (16899, "env")
    env[f"{PACK_ENV}IDENTIFY_PORT"] = "port"
    got = Resolver(env=env, user={}, packs=[Mps3Pack()]).resolve("mps3.identify.port")
    assert (got.value, got.source) == (6899, "pack") and got.problems


def test_the_admin_may_lock_a_pack_row_it_owns_but_not_a_users():
    pol = MachinePolicy(POL, lock={"mps3.openocd_cfg_dir": "/lab/cfg",
                                   "mps3.console.pace_ms": 0})
    r = Resolver(policy=pol, env=dict(NO_KEYRING), user={}, packs=[Mps3Pack()])
    assert r.resolve("mps3.openocd_cfg_dir").locked
    assert not r.resolve("mps3.console.pace_ms").locked           # the twin: a user row
    assert any("mps3.console.pace_ms is not the administrator's" in p for p in r.problems)


# --- files: a board table written through the settings is one the pack still takes -------------------


BOARDS_TOML = {
    # the shape in docs/HUB_MODE.md and david's file: the hub as an inline table
    "inline": '[boards.lab]\nmatch = ["192.168.10.101"]  # the lab board\n'
              'hub = { host = "hub", target = "mps3_01_pl" }\n',
    "table": '[boards.lab]\nmatch = ["192.168.10.101"]  # the lab board\n\n'
             '[boards.lab.hub]\nhost = "hub"\ntarget = "mps3_01_pl"\n',
}


@pytest.mark.parametrize("shape", sorted(BOARDS_TOML))
def test_a_share_set_through_the_settings_is_listed_and_still_valid(tmp_path, shape):
    """A new ``shares`` table inside an inline ``hub`` must be inline too (tomlkit refuses a
    table there): the write the menu makes is one the pack's parser still takes."""
    from harness_manager_mps3 import hub

    (tmp_path / "boards.toml").write_text(BOARDS_TOML[shape])
    r = Resolver.load(tmp_path, env=dict(NO_KEYRING), policy_path=tmp_path / "none.toml",
                      packs=[Mps3Pack()])
    assert [x.key for x in r.listing(section="Boards")
            if ".shares." in x.key] == []                          # none yet, and no template
    got = r.set("boards.lab.hub.shares.mcc", "/dev/mps3_01_pl/tty_00")
    assert (got.source, got.value) == ("user", "/dev/mps3_01_pl/tty_00")
    text = (tmp_path / "boards.toml").read_text()
    assert "# the lab board" in text
    import tomllib

    table = tomllib.loads(text)["boards"]["lab"]["hub"]
    assert hub.parse_hub_table(table).shares == {"mcc": "/dev/mps3_01_pl/tty_00"}
    r = Resolver.load(tmp_path, env=dict(NO_KEYRING), policy_path=tmp_path / "none.toml",
                      packs=[Mps3Pack()])
    listed = [x.key for x in r.listing(section="Boards") if ".shares." in x.key]
    assert listed == ["boards.lab.hub.shares.mcc"]


def test_negative_twin_a_share_the_pack_would_refuse_is_refused_first(tmp_path):
    r = Resolver.load(tmp_path, env=dict(NO_KEYRING), policy_path=tmp_path / "none.toml",
                      packs=[Mps3Pack()])
    with pytest.raises(UsageError, match="/dev/"):
        r.set("boards.lab.hub.shares.mcc", "/tmp/not-a-tty")
    assert not (tmp_path / "boards.toml").exists()


# --- the coverage: every MPS3 variable is a pack row --------------------------------------------------


def _pack_env_names() -> set[str]:
    """``HARNESS_MANAGER_MPS3_*`` named anywhere in src, outside the modules that declare them."""
    names: set[str] = set()
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC)
        if "settings" in rel.parts[:2] or rel.name == "settings.py":
            continue
        names |= set(re.findall(rf"{PACK_ENV}[A-Z0-9_]*[A-Z0-9]\b", p.read_text()))
    return names


def _coverage(rows: Iterable[Setting]) -> tuple[list[str], list[str]]:
    """``(read but not declared, declared but not read)``."""
    declared = {s.env for s in rows if s.env.startswith(PACK_ENV)}
    read = _pack_env_names()
    return sorted(read - declared), sorted(declared - read)


def test_every_mps3_variable_is_a_pack_row_and_every_row_variable_is_read():
    missing, ghosts = _coverage(mps3_rows())
    assert not missing, f"not declared in harness_manager_mps3/settings.py: {missing}"
    assert not ghosts, f"pack rows name variables nothing reads: {ghosts}"
    assert len(_pack_env_names()) == 13          # +4: SLOT-TIMING; +1: IDENTITY (IP_POOL)
    core = {s.env for s in core_schema().rows}
    assert not {n for n in core if n.startswith(PACK_ENV)}         # none is the core's


@pytest.mark.parametrize("drop", ["mps3.xvc_port", "mps3.overlay_dirs", "mps3.tunnelled"])
def test_negative_twin_the_coverage_fails_when_a_row_is_removed(drop):
    rows = [s for s in mps3_rows() if s.key != drop]
    missing, _ = _coverage(rows)
    assert missing == [rows_by_key()[drop].env]


def test_negative_twin_a_row_naming_a_variable_nothing_reads_is_a_ghost():
    ghost = Setting("mps3.nothing", "str", "", "Advanced", "d", pack="mps3", owner="dev",
                    env=f"{PACK_ENV}NOTHING")
    assert _coverage([*mps3_rows(), ghost])[1] == [f"{PACK_ENV}NOTHING"]


# --- a second pack: rows appear, and no core change is needed -----------------------------------------


def test_a_second_pack_adds_rows_through_the_engine(tmp_path):
    engine = Engine(EngineConfig(state_dir=tmp_path), packs={"mps3": Mps3Pack(),
                                                             "kr260": Kr260Pack()})
    schema = engine.settings_schema()
    assert schema.spec("kr260.console.pace_ms").view()["pack"] == "kr260"
    assert schema.spec("mps3.console.pace_ms").pack == "mps3"
    (tmp_path / "boards.toml").write_text('[boards.kria]\nbootmode = { default = "jtag" }\n')
    r = engine.settings_resolver(env=dict(NO_KEYRING), policy_path=tmp_path / "none.toml")
    got = r.resolve("kr260.console.pace_ms")
    assert (got.value, got.source, got.where) == (5, "pack", "the kr260 pack")
    got = r.resolve("boards.kria.bootmode.default")
    assert (got.value, got.source, got.where) == ("jtag", "user", "boards.toml")
    boards = {x.key: x.value for x in r.listing(section="Boards")}
    assert boards["boards.kria.bootmode.default"] == "jtag"
    assert boards["boards.kria.hub.baud"] == 115200                 # the MPS3 pack's, too
    assert "Debug" in schema.sections() and schema.spec("kr260.jtag.cable").section == "Debug"


def test_the_second_pack_needs_no_core_code():
    """No code in the core, the settings package or the web UI names the fake pack, its
    keys or its board table (prose may mention a KR260; code may not)."""
    code = re.compile(r"""['"]kr260['"]|kr260\.|bootmode""")
    for p in (SRC / "harness_manager").rglob("*"):
        if p.is_file() and p.suffix in (".py", ".js", ".html", ".css"):
            assert not code.search(p.read_text(errors="replace")), p
    assert code.search(Path(__file__).read_text())                  # the twin: it bites here


def test_negative_twin_a_second_pack_that_takes_the_mps3_table_is_left_out(tmp_path, caplog):
    grabby = Kr260Pack([Setting("boards.*.xvc.reach", "str", "", "Boards", "d", pack="kr260")])
    with pytest.raises(ValueError, match="declared twice"):
        with_packs(core_schema(), [Mps3Pack(), grabby])
    engine = Engine(EngineConfig(state_dir=tmp_path), packs={"mps3": Mps3Pack(),
                                                             "kr260": grabby})
    with caplog.at_level(logging.ERROR, logger="harness_manager.settings.packs"):
        schema = engine.settings_schema()
    assert schema.find("kr260.console.pace_ms") is None             # all of it, left out
    assert schema.spec("boards.*.xvc.reach").pack == "mps3"         # the MPS3's stay
    assert "'kr260'" in caplog.text and "declared twice" in caplog.text


def test_negative_twin_a_pack_whose_settings_raise_is_left_out(tmp_path, caplog):
    engine = Engine(EngineConfig(state_dir=tmp_path),
                    packs={"mps3": Mps3Pack(), "kr260": Kr260Pack(broken=True)})
    with caplog.at_level(logging.ERROR, logger="harness_manager.settings.packs"):
        schema = engine.settings_schema()
    assert schema.find("kr260.jtag.cable") is None and schema.find("mps3.rbb_port")
    assert "broken" in caplog.text
    with pytest.raises(RuntimeError):
        with_packs(core_schema(), [Kr260Pack(broken=True)])


def test_the_engine_builds_the_pack_layer_from_pack_overrides(tmp_path):
    """``--pack-overrides`` builds the pack with other kwargs; its values are the pack layer."""
    engine = Engine(EngineConfig(state_dir=tmp_path,
                                 pack_overrides={"mps3": {"console_pace_s": 0.0}}))
    r = engine.settings_resolver(env=dict(NO_KEYRING), policy_path=tmp_path / "none.toml")
    got = r.resolve("mps3.console.pace_ms")
    assert (got.value, got.source) == (0, "pack")
    plain = Engine(EngineConfig(state_dir=tmp_path)).settings_resolver(
        env=dict(NO_KEYRING), policy_path=tmp_path / "none.toml")
    assert plain.resolve("mps3.console.pace_ms").value == 20        # the twin
