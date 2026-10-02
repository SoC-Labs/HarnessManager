"""A fake package index for the cross-platform wheelhouse (lane WINDOWS): a folder of
tiny wheels, each with a METADATA naming its dependencies, tagged for Windows, macOS or any
platform. ``pip download --no-index --find-links`` reads it: the network is never used."""

from __future__ import annotations

import zipfile
from pathlib import Path


def wheel(dest: Path, name: str, version: str, *, tag: str = "py3-none-any",
          requires: tuple[str, ...] = ()) -> Path:
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    meta += [f"Requires-Dist: {r}" for r in requires]
    path = dest / f"{dist}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{dist}/__init__.py", "")
        z.writestr(f"{info}/METADATA", "\n".join(meta) + "\n")
        z.writestr(f"{info}/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n")
        z.writestr(f"{info}/RECORD", "")
    return path


HM_REQUIRES = (
    "mps3-pyverify>=0.1.0",
    "fakedep>=1",
    'fakewinonly; sys_platform == "win32"',
    'fakemaconly; sys_platform == "darwin"',
    'fakeold; python_version < "3.11"',
    'fakeserial>=1; extra == "serial"',
    'fakedev; extra == "dev"',
)

CONSTRAINTS = """\
# a universal lock, as scripts/lock_deps.sh writes it
fakedep==1.0
    # via harness-manager
fakechild==1.0
fakegrand==1.0
fakewinonly==1.0 ; sys_platform == 'win32'
fakemaconly==1.0 ; sys_platform == 'darwin'
fakeold==1.0 ; python_full_version < '3.11'
fakeserial==1.0
"""


def index(root: Path) -> tuple[Path, Path, Path]:
    """``(index dir, the Harness Manager wheel, constraints.txt)``."""
    idx = root / "index"
    idx.mkdir(parents=True)
    hm_dir = root / "dist"
    hm_dir.mkdir()
    hm = wheel(hm_dir, "harness-manager", "9.0.0", requires=HM_REQUIRES)
    wheel(idx, "fakedep", "1.0", requires=("fakechild[x]>=1",))
    wheel(idx, "fakedep", "2.0")
    wheel(idx, "fakechild", "1.0", requires=('fakegrand; extra == "x"',))
    wheel(idx, "fakegrand", "1.0")
    wheel(idx, "fakewinonly", "1.0", tag="cp312-cp312-win_amd64")
    wheel(idx, "fakemaconly", "1.0", tag="py3-none-macosx_11_0_arm64")
    wheel(idx, "fakeold", "1.0")
    wheel(idx, "fakeserial", "1.0")
    wheel(idx, "fakedev", "1.0")
    wheel(idx, "uv", "0.5.0", tag="py3-none-win_amd64")
    wheel(idx, "uv", "0.5.0", tag="py3-none-macosx_11_0_arm64")
    cons = root / "constraints.txt"
    cons.write_text(CONSTRAINTS, encoding="utf-8")
    return idx, hm, cons
