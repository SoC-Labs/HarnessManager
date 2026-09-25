"""SET-CORE: the settings schema (keys, types, rows), each check with its negative twin.

docs/design/SETTINGS.md §3 and Appendix A; david's decisions S1-S4 (2026-09-25).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.settings import Schema, Setting, canonical_key, coerce, join_key, split_key
from harness_manager.settings.rows import CORE_ROWS, NOT_SETTINGS_ENV
from harness_manager.settings.schema import parse_duration, parse_size

SRC = Path(__file__).resolve().parents[2] / "src"


# --- keys ------------------------------------------------------------------------------------


@pytest.mark.parametrize("key, parts", [
    ("updates.channel", ("updates", "channel")),
    ("hubs.*.url", ("hubs", "*", "url")),
    ('boards."mps3@192.168.10.101:6900".name', ("boards", "mps3@192.168.10.101:6900", "name")),
    ("boards.'lit.eral'.name", ("boards", "lit.eral", "name")),
    ('boards."a\\"b".name', ("boards", 'a"b', "name")),
])
def test_keys_split_as_toml_does_and_join_back(key, parts):
    assert split_key(key) == parts
    assert split_key(join_key(parts)) == parts


def test_negative_twin_a_dotted_board_key_needs_quotes():
    # Unquoted, the board's address is four parts: that is why keys follow TOML's quoting.
    assert len(split_key("boards.192.168.10.101.name")) == 6
    for bad in ("", "a..b", "a.", 'a."open', "a b.c", "boards.mps3@x.name"):
        with pytest.raises(UsageError):
            split_key(bad)


def test_canonical_spelling_quotes_only_what_needs_it():
    assert canonical_key("boards.'lab'.name") == "boards.lab.name"
    assert canonical_key('boards."a:b".name') == 'boards."a:b".name'


# --- types -----------------------------------------------------------------------------------


def _row(type_, **kw):
    return Setting("x.y", type_, kw.pop("default", None), "S", "doc", **kw)


@pytest.mark.parametrize("type_, env_text, want", [
    ("int", "23500", 23500), ("int", "0x40", 0x40), ("float", "2.5", 2.5),
    ("bool", "yes", True), ("bool", "0", False), ("duration", "12h", 43200),
    ("duration", "90m", 5400), ("size", "1GiB", 1 << 30), ("size", "2G", 2_000_000_000),
    ("list", "/a, /b;/c", ["/a", "/b", "/c"]), ("str", "  x  ", "x"),
])
def test_env_text_parses_to_the_type(type_, env_text, want):
    assert coerce(_row(type_), env_text, from_env=True) == want


@pytest.mark.parametrize("type_, file_value", [
    ("int", "23500"), ("int", True), ("bool", "true"), ("str", 3), ("list", "/a"),
    ("duration", "soon"), ("size", "lots"), ("float", "2.5"),
])
def test_negative_twin_a_file_value_must_already_have_the_type(type_, file_value):
    spec = _row(type_)
    if type_ == "list":                        # a bare string in a TOML file is not a list
        assert coerce(spec, file_value) == ["/a"]     # (the env splitter applies to strings)
        return
    with pytest.raises(UsageError):
        coerce(spec, file_value)


def test_enum_url_and_checks_refuse_with_the_reason():
    e = _row("enum", choices=("a", "b"))
    assert coerce(e, "a") == "a"
    with pytest.raises(UsageError, match="one of a, b"):
        coerce(e, "c")
    u = _row("url")
    assert coerce(u, "https://hub:7246") == "https://hub:7246" and coerce(u, "") == ""
    with pytest.raises(UsageError, match="https://"):
        coerce(u, "ftp://hub")
    c = _row("int", check=lambda v: "" if v < 10 else "must be under 10")
    assert coerce(c, 3) == 3
    with pytest.raises(UsageError, match="under 10"):
        coerce(c, 30)
    with pytest.raises(UsageError, match="true or false"):
        coerce(_row("bool"), "maybe", from_env=True)


def test_durations_and_sizes():
    assert parse_duration(0) == 0 and parse_duration("1d") == 86400
    assert parse_size("64k") == 64000 and parse_size(5) == 5
    for bad in (-1, True, "1w"):
        with pytest.raises(ValueError):
            parse_duration(bad)


# --- rows ------------------------------------------------------------------------------------


def test_every_core_default_passes_its_own_row():
    for s in CORE_ROWS:
        if s.default is None or s.secret:
            continue
        assert coerce(s, s.default) == s.default, s.key


def test_negative_twin_a_bad_row_is_refused_at_declaration():
    with pytest.raises(ValueError, match="type"):
        Setting("a.b", "colour", "", "S", "doc")
    with pytest.raises(ValueError, match="choices"):
        Setting("a.b", "enum", "", "S", "doc")
    with pytest.raises(ValueError, match="hubs.\\*.<field>"):
        Setting("hubs.url", "url", "", "S", "doc")
    with pytest.raises(ValueError, match="spell it"):
        Setting("boards.*.'name'", "str", "", "S", "doc")


def test_owner_decides_lockable_and_menu():
    s = {r.key: r for r in CORE_ROWS}
    assert s["tools.openocd"].lockable and s["tools.openocd"].ui
    assert not s["general.theme"].lockable                     # the user's own
    assert not s["updates.github_token"].lockable              # a secret, never
    assert not s["advanced.state_dir"].lockable                # read-only
    assert not s["dev.no_daemon"].ui                           # a developer seam
    assert s["updates.auto"].ceiling and s["updates.channel"].env_rank == "under-user"


def test_core_schema_has_no_duplicates_and_packs_extend_it_under_their_prefix():
    schema = Schema(CORE_ROWS)
    assert schema.spec('hubs.lab.url').key == "hubs.*.url"
    assert schema.spec('boards."a.b".power.auth.user').key == "boards.*.power.auth.user"
    pack = [Setting("mps3.console.pace_ms", "int", 20, "Consoles", "doc", scope="pack",
                    pack="mps3"),
            Setting("boards.*.xvc.reach", "enum", "auto", "Boards", "doc", scope="board",
                    pack="mps3", choices=("auto", "hub"))]
    schema.extend(pack, pack="mps3")
    assert schema.spec("mps3.console.pace_ms").pack == "mps3"


def test_negative_twin_a_pack_cannot_take_a_core_key_or_leave_its_prefix():
    schema = Schema(CORE_ROWS)
    with pytest.raises(ValueError, match="declared twice"):
        schema.extend([Setting("boards.*.name", "str", "", "B", "d", pack="mps3")], pack="mps3")
    with pytest.raises(ValueError, match="under 'mps3.'"):
        schema.extend([Setting("tools.mine", "str", "", "T", "d", pack="mps3")], pack="mps3")
    with pytest.raises(ValueError, match="declared twice"):
        Schema(CORE_ROWS, [Setting("updates.*", "str", "", "U", "d")])      # overlaps
    with pytest.raises(UsageError, match="no such setting"):
        schema.spec("tools.nothing")


#: The MPS3 pack's variables: its own rows (SET-PACK, ``tests/unit/test_settings_pack.py``).
PACK_ENV = "HARNESS_MANAGER_MPS3_"


def _env_names(root: Path) -> set[str]:
    """Variables named in the code, outside the modules that declare them (the settings
    package, and a pack's ``settings.py``)."""
    names: set[str] = set()
    for p in root.rglob("*.py"):
        rel = p.relative_to(SRC)
        if "settings" in rel.parts[:2] or rel.name == "settings.py":
            continue
        names |= set(re.findall(r"HARNESS_MANAGER_[A-Z0-9_]*[A-Z0-9]\b", p.read_text()))
    return names


def _core_names() -> set[str]:
    return {n for n in _env_names(SRC / "harness_manager")
            if not n.startswith(PACK_ENV)} - NOT_SETTINGS_ENV


def test_every_core_variable_is_a_row_and_every_row_variable_is_real():
    """The inventory cannot drift: each ``HARNESS_MANAGER_*`` read in ``src/harness_manager``
    is declared, and each variable a row names is read somewhere (or is the new keyring
    switch). The ``HARNESS_MANAGER_MPS3_*`` ones are the MPS3 pack's rows, never the core's
    (``test_settings_pack`` checks every one is declared there)."""
    core = _core_names()
    declared = {s.env for s in CORE_ROWS if s.env.startswith("HARNESS_MANAGER_")}
    missing = sorted(core - declared)
    assert not missing, f"not declared in settings/rows.py: {missing}"
    everywhere = _env_names(SRC)
    ghosts = sorted(declared - everywhere - {"HARNESS_MANAGER_KEYRING"})   # new: secrets.py
    assert not ghosts, f"rows name variables nothing reads: {ghosts}"
    pack_only = sorted(n for n in everywhere if n.startswith(PACK_ENV))
    assert pack_only and not set(pack_only) & declared, "the MPS3 variables are the pack's rows"


def test_negative_twin_the_coverage_check_bites():
    core = _core_names()
    declared = {s.env for s in CORE_ROWS[1:] if s.env.startswith("HARNESS_MANAGER_")}
    declared.discard("HARNESS_MANAGER_OPENOCD")
    assert "HARNESS_MANAGER_OPENOCD" in core - declared
