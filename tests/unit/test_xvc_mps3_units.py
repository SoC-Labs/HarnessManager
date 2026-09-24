"""Lane XVC-CORE (X2): the MPS3 pieces with no board: the ``-J`` tunnel (CCR X-1), the
per-static probes store, boards.toml ``xvc`` and ``ltx_for``. Each check has a negative twin.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3 import xvc as MX
from harness_manager_mps3.statics import StaticStore, canonical_id
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.fakes.t2_overlays import make_overlay

HUB = "mapstone-dev.ecs.soton.ac.uk"


def argv_of(**kw) -> list[str]:
    ssh = FakeSsh()
    t = T.SshTunnel("192.168.10.101", [T.Forward("xvc", "127.0.0.1", 2542, 40001)],
                    launcher=ssh, ssh_g=ssh.ssh_g, **kw)
    return t.build_argv()


# --- CCR X-1: jump and user ----------------------------------------------------------------------


def test_the_tunnel_can_jump_through_the_hub_as_a_user():
    argv = argv_of(jump=HUB, user="root")
    assert argv[argv.index("-J") + 1] == HUB and argv[argv.index("-l") + 1] == "root"
    assert argv[-1] == "192.168.10.101"
    assert argv[argv.index("-L") + 1] == "127.0.0.1:40001:127.0.0.1:2542"
    assert argv.index("-J") < argv.index("-L")            # options before the forwards
    # the orphan reaper still recognises it: the host last, every -L spec
    specs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-L"]
    assert specs == ["127.0.0.1:40001:127.0.0.1:2542"]


def test_negative_twin_without_jump_or_user_the_argv_is_unchanged_and_bad_values_refused():
    plain = argv_of()
    assert "-J" not in plain and "-l" not in plain
    assert plain == [*plain[:1], *T.SSH_OPTIONS, "-N", "-T", "-L",
                     "127.0.0.1:40001:127.0.0.1:2542", "192.168.10.101"]
    for bad in ({"jump": "-oProxyCommand=sh"}, {"user": "root -v"}, {"jump": "a b"}):
        with pytest.raises(UsageError):
            argv_of(**bad)


def test_status_names_the_jump_only_for_a_jump_tunnel():
    ssh = FakeSsh()
    t = T.SshTunnel("b", [T.Forward("xvc", "127.0.0.1", 2542, 40002)], launcher=ssh,
                    ssh_g=ssh.ssh_g, jump=HUB, user="root")
    assert t.status()["jump"] == HUB and t.status()["user"] == "root"
    t2 = T.SshTunnel("b", [T.Forward("xvc", "127.0.0.1", 2542, 40003)], launcher=ssh,
                     ssh_g=ssh.ssh_g)
    assert "jump" not in t2.status()                       # twin: the L1 shape is untouched


# --- the per-static store ----------------------------------------------------------------------------


def test_the_static_store_keeps_probes_files_per_static(tmp_path):
    store = StaticStore(tmp_path / "statics")
    ltx = tmp_path / "config_rm_greybox_static.ltx"
    ltx.write_text('{"mig": 1}')
    side = tmp_path / "config_rm_greybox_static.ltx.json"
    side.write_text(json.dumps({"static_id": "0x72BB0A36", "vivado": "2026.1",
                                "ltx_crc32": f"0x{zlib.crc32(ltx.read_bytes()):08x}"}))
    store.import_mint_dir(tmp_path, 0x72BB0A36)
    got = store.static_ltx("0x72bb0a36")
    assert got["crc_ok"] is True and got["vivado"] == "2026.1"
    assert got["path"] == tmp_path / "statics" / "0x72bb0a36" / "static.ltx"
    assert store.static_ltx("0x11c30003") is None           # another static: nothing
    assert canonical_id(0x72BB0A36) == canonical_id("0x72BB0A36") == "0x72bb0a36"


def test_negative_twin_a_sidecar_for_another_static_is_refused(tmp_path):
    store = StaticStore(tmp_path / "statics")
    ltx = tmp_path / "x.ltx"
    ltx.write_text("{}")
    side = tmp_path / "x.ltx.json"
    side.write_text(json.dumps({"static_id": "0x3F1A560F"}))
    with pytest.raises(RefusedError):
        store.put_static_ltx(ltx, "0x72BB0A36", side)
    with pytest.raises(RefusedError):
        store.put_full_ltx(ltx, "0x72BB0A36", "nanosoc_ila", side)
    assert store.static_ltx("0x72BB0A36") is None
    with pytest.raises(UsageError):
        store.put_full_ltx(ltx, "0x72BB0A36", "../escape")


# --- boards.toml xvc, and ltx_for ------------------------------------------------------------------------


def cand() -> Candidate:
    return Candidate(pack="mps3", board_id="mps3@192.168.10.101:6900",
                     links=(Link(LinkKind.ETHERNET, "192.168.10.101:6900", "shell"),))


def write_toml(state: Path, text: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "boards.toml").write_text(text)


def test_boards_toml_xvc_sets_the_reach_and_user(tmp_path, monkeypatch):
    state = Path(tmp_path / "state")
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(state))
    assert MX.xvc_config(cand()) == {"reach": "auto"}      # nothing configured
    write_toml(state, '[boards.lab]\nmatch = ["192.168.10.101"]\n'
                      'xvc = { reach = "board-ssh", user = "hm" }\n')
    assert MX.xvc_config(cand()) == {"reach": "board-ssh", "user": "hm"}
    write_toml(state, '[boards.lab]\nmatch = ["192.168.10.101"]\nxvc = "hub"\n')
    assert MX.xvc_config(cand()) == {"reach": "hub"}


@pytest.mark.parametrize("bad", ['xvc = { reach = "wifi" }', 'xvc = { colour = "red" }',
                                 'xvc = { user = "-oProxyCommand" }', "xvc = 3"])
def test_negative_twin_a_bad_xvc_table_is_a_usage_error(tmp_path, monkeypatch, bad):
    state = Path(tmp_path / "state")
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(state))
    write_toml(state, f'[boards.lab]\nmatch = ["192.168.10.101"]\n{bad}\n')
    with pytest.raises(UsageError):
        MX.xvc_config(cand())


def test_ltx_for_reads_the_overlays_own_probes_file(tmp_path):
    from harness_manager_mps3.overlays import load_overlay_dir

    d = make_overlay(tmp_path, "nanosoc_ila", rm_id=0x0100000A)
    (d / "nanosoc_ila.ltx").write_text("{}")
    manifest = json.loads((d / "manifest.json").read_text())
    manifest["ltx"] = "nanosoc_ila.ltx"
    (d / "manifest.json").write_text(json.dumps(manifest))
    overlay, _ = load_overlay_dir(d)
    assert MX.ltx_for(overlay) == d / "nanosoc_ila.ltx"
    (d / "nanosoc_ila.ltx").unlink()
    assert MX.ltx_for(overlay) is None                     # twin: named but missing
    plain, _ = load_overlay_dir(make_overlay(tmp_path, "synth"))
    assert MX.ltx_for(plain) is None                       # and none named at all
