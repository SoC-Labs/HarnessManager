"""KIT-RC2 P9: ``tools/gen_mps3_pins.py`` models more than one fielded shell.

The RC2 record (``fielded/0x44EE76D5/``) is not published yet, so the fixture is a
throwaway clone of the platform repo (``git clone --shared --no-checkout``: the platform
checkout is only read) with one extra commit: the 0x72BB0A36 record relabelled as
0x44EE76D5, on its own branch. Its boundary and every shell source are 0x72BB0A36's, which
is what RC2's are for the boundary (boundary.yaml and partition-pins.md are identical at
platform e543630 and fb1f8c7).

Skips (with the reason) where the platform repo or the Xilinx package file is absent, like
``test_t10_model``'s drift tests. Every test has its negative twin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness_manager.services import xdc
from harness_manager.services.xdc.model import PinModel
from harness_manager_mps3 import pins
from tests.fakes.t10_platform import PLATFORM, REPO

BASE = "0x72BB0A36"
RC2 = "0x44EE76D5"
BASE_REF = "feat/rm-ila-mint"
GEN = REPO / "tools" / "gen_mps3_pins.py"


def _gen(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GEN), *args], capture_output=True, text=True,
                          timeout=180)


def _git(repo: Path, *args: str, env: dict | None = None, data: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          input=data, env=env).stdout.decode().strip()


@pytest.fixture(scope="module")
def plat(tmp_path_factory) -> Path:
    """A clone of the platform at BASE_REF, plus branches holding relabelled records."""
    if shutil.which("git") is None:
        pytest.skip("no git")
    if not (PLATFORM / ".git").exists():
        pytest.skip(f"the platform repo is not at {PLATFORM}")
    pkg = pins.load_model()["sources"]["xilinx_pkg"]["path"]
    if not Path(pkg).is_file():
        pytest.skip("the Xilinx IBIS package file is not on this host")
    root = tmp_path_factory.mktemp("plat") / "platform"
    r = subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", "--single-branch",
                        "-b", BASE_REF, str(PLATFORM), str(root)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"{BASE_REF} is not in the platform repo: {r.stderr.strip()}")
    base = json.loads(_git(root, "show", f"{BASE_REF}:fielded/{BASE}/mint.json"))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_INDEX_FILE": str(root.parent / "index")}

    readme = ("# RC2 fixture\n\n- **`dut_clk` enters the partition from `BUFGCE_X2Y47`.** On "
              "`0x72BB0A36` it was `BUFGCE_X2Y24`.\n")

    def branch(name: str, record: dict, extra: dict[str, str] | None = None,
               with_readme: bool = True) -> None:
        _git(root, "read-tree", BASE_REF, env=env)
        files = {f"fielded/{record['static_id']}/mint.json": json.dumps(record, indent=2),
                 **({f"fielded/{record['static_id']}/README.md": readme} if with_readme else {}),
                 **(extra or {})}
        for path, text in files.items():
            blob = _git(root, "hash-object", "-w", "--stdin", env=env, data=text.encode())
            _git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        tree = _git(root, "write-tree", env=env)
        commit = _git(root, "commit-tree", tree, "-p", BASE_REF, "-m", name, env=env)
        _git(root, "update-ref", f"refs/heads/{name}", commit, env=env)

    rc2 = json.loads(json.dumps(base))
    rc2["static_id"] = RC2
    rc2["static_usercode"]["value"]["usercode"] = "0x12345678"
    rc2["static_canon"]["value"]["flags"] = dict(rc2["static_canon"]["value"]["flags"],
                                                 SHELL_CPU="mbv")
    branch("rc2-fixture", rc2)
    bad = json.loads(json.dumps(rc2))
    for i in bad["static_canon"]["value"]["inputs"]:
        if i["path"] == "fpga/shell/shell_top.sv":
            i["sha256"] = "0" * 64                  # the record says: another shell_top
    branch("rc2-tampered", bad)
    branch("rc2-noreadme", rc2, with_readme=False)
    md = _git(root, "show", f"{BASE_REF}:docs/FIELDED_SHELL.md")
    branch("rc2-cutover", rc2, {"docs/FIELDED_SHELL.md":
                                md.replace(f"| `fielded` | `{BASE}` |", f"| `fielded` | `{RC2}` |")})
    return root


def _model(path: Path) -> dict:
    return json.loads(path.read_text())


def test_two_shells_each_with_its_own_block(plat, tmp_path):
    out = tmp_path / "m.json"
    r = _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
             "--shell", f"{RC2}@rc2-fixture", "--out", str(out))
    assert r.returncode == 0, r.stderr
    doc = _model(out)
    assert list(doc["shells"]) == [BASE, RC2] and doc["default_shell"] == BASE
    base, rc2 = doc["shells"][BASE], doc["shells"][RC2]
    assert rc2["static_id"] == RC2 and rc2["usercode"] == "0x12345678"
    assert rc2["platform"]["ref"] == "rc2-fixture" and rc2["platform"]["commit"] != \
        base["platform"]["commit"]
    assert rc2["rp_boundary"]["totals"] == {"ports": 47, "bits": 148, "decoupler_intfs": 20}
    assert rc2["rp_boundary"]["groups"] == base["rp_boundary"]["groups"]
    assert rc2["owns"] == base["owns"] and rc2["connectivity"] == base["connectivity"]
    # the Linux shell's DDR4 is noted, not modelled (the Arm pinmap does not place it)
    assert any("DDR4" in n and "not modelled" in n for n in rc2["notes"])
    assert "notes" not in base
    # a release candidate: docs/FIELDED_SHELL.md at its ref names 0x72BB0A36, not RC2
    assert base["fielded"] is True and rc2["fielded"] is False
    assert doc["sources"][rc2["fielded_src"].split(":")[0]]["path"] == "docs/FIELDED_SHELL.md"
    # its own pblock fact, from its own record (the default shell keeps X2Y24)
    assert rc2["pblock"]["dut_clk_hd_clk_src"]["value"] == "BUFGCE_X2Y47"
    assert base["pblock"]["dut_clk_hd_clk_src"]["value"] == "BUFGCE_X2Y24"
    assert doc["sources"][rc2["pblock"]["dut_clk_hd_clk_src"]["src"].split(":")[0]]["path"] == \
        f"fielded/{RC2}/README.md"
    # its own record, and every fact it cites is a listed source
    assert rc2["mint_record"].split(":")[0] in doc["sources"]
    assert doc["sources"][rc2["mint_record"].split(":")[0]]["path"] == f"fielded/{RC2}/mint.json"
    # the board part is the default shell's, unchanged by a second shell
    one = tmp_path / "one.json"
    assert _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}", "--out",
                str(one)).returncode == 0
    single = _model(one)
    for key in ("nets", "banks", "package_pins", "clocks", "connectors", "config"):
        assert doc[key] == single[key], key
    # the kit flow reads it: the RM kit for RC2 is RC2's
    model = PinModel(doc, pack="mps3")
    assert len(model.boundary(RC2)) == 47
    d = xdc.from_doc({"kind": "rm", "name": "minimal", "static_id": RC2,
                      "use": {"clkrst": {}, "status": {"tie": ["dut_lockup", "irq_out"]}}},
                     origin="inline")
    k = xdc.rm_kit(model, d)
    assert k.facts["static_id"] == RC2 and not [f for f in k.findings if f.code == "static_id"]


def test_after_the_cutover_the_second_shell_is_fielded(plat, tmp_path):
    out = tmp_path / "m.json"
    r = _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
             "--shell", f"{RC2}@rc2-cutover", "--out", str(out))
    assert r.returncode == 0, r.stderr
    rc2 = _model(out)["shells"][RC2]
    assert rc2["fielded"] is True and "fielded_src" not in rc2


def test_negative_twin_a_per_shell_fact_whose_citation_is_gone_is_refused(plat, tmp_path):
    # the RC2 record with no README (so no X2Y47 sentence): never fall back to X2Y24
    out = tmp_path / "m.json"
    r = _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
             "--shell", f"{RC2}@rc2-noreadme", "--out", str(out))
    assert r.returncode == 2 and not out.exists()
    assert f"fielded/{RC2}/README.md" in r.stderr          # its source is gone: refused


def test_negative_twin_a_record_whose_sources_are_not_at_its_ref_is_refused(plat, tmp_path):
    out = tmp_path / "m.json"
    r = _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
             "--shell", f"{RC2}@rc2-tampered", "--out", str(out))
    assert r.returncode == 2 and not out.exists()
    assert f"differ from what {RC2} was built from" in r.stderr
    assert "fpga/shell/shell_top.sv" in r.stderr


def test_negative_twin_a_shell_with_no_record_or_named_twice(plat, tmp_path):
    out = tmp_path / "m.json"
    r = _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
             "--shell", f"{RC2}@{BASE_REF}", "--out", str(out))    # no RC2 record there
    assert r.returncode == 2 and not out.exists() and f"fielded/{RC2}/mint.json" in r.stderr
    r = _gen("--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
             "--shell", f"{BASE.lower()}@rc2-fixture", "--out", str(out))
    assert r.returncode == 2 and "named twice" in r.stderr
    r = _gen("--platform", str(plat), "--shell", "72BB0A36", "--out", str(out))
    assert r.returncode == 2 and "0x followed by 8 hex digits" in r.stderr


def test_all_finds_every_fielded_record_that_matches_a_ref(plat, tmp_path):
    out = tmp_path / "m.json"
    r = _gen("--platform", str(plat), "--all", "--ref", BASE_REF, "--ref", "rc2-fixture",
             "--out", str(out))
    assert r.returncode == 0, r.stderr
    doc = _model(out)
    assert list(doc["shells"]) == [BASE, RC2]         # SHELLS order first: the default
    # the older records are left out loudly, never modelled from the wrong files
    assert "0x3F1A560F left out" in r.stderr
    # twin: only the tampered branch besides the base: RC2 is left out
    r = _gen("--platform", str(plat), "--all", "--ref", BASE_REF, "--ref", "rc2-tampered",
             "--out", str(out))
    assert r.returncode == 0 and list(_model(out)["shells"]) == [BASE]
    assert f"{RC2} left out" in r.stderr


def test_the_drift_gate_covers_every_shell(plat, tmp_path):
    out = tmp_path / "m.json"
    args = ["--platform", str(plat), "--shell", f"{BASE}@{BASE_REF}",
            "--shell", f"{RC2}@rc2-fixture", "--out", str(out)]
    assert _gen(*args).returncode == 0
    r = _gen(*args, "--check")
    assert r.returncode == 0 and f"{RC2}@rc2-fixture" in r.stdout
    # twin: a hand edit in the SECOND shell's block is caught
    doc = _model(out)
    doc["shells"][RC2]["rp_boundary"]["totals"]["bits"] = 149
    out.write_text(json.dumps(doc, indent=1) + "\n")
    r = _gen(*args, "--check")
    assert r.returncode == 1 and "stale" in r.stderr


def test_the_committed_model_holds_the_default_shells():
    # SHELLS (no --shell) is what --check regenerates (test_t10_model runs that drift gate)
    doc = pins.load_model()
    assert list(doc["shells"]) == [s.partition("@")[0] for s in _shells()]
    for sid, spec in zip(doc["shells"], _shells(), strict=True):
        assert doc["shells"][sid]["platform"]["ref"] == spec.partition("@")[2]


def _shells() -> list[str]:
    sys.path.insert(0, str(REPO / "tools"))
    try:
        import gen_mps3_pins
    finally:
        sys.path.pop(0)
    return list(gen_mps3_pins.SHELLS)
