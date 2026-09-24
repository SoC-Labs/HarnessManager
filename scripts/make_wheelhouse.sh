#!/usr/bin/env bash
# Make a wheelhouse: every wheel Harness Manager needs, for an install with no
# network (scripts/install.sh --offline DIR).
#
#   scripts/make_wheelhouse.sh [--python PY] [--with-serial] [--with-app] DIR
#
# Run it on a machine with network, with the SAME Python version (3.11, 3.12...),
# operating system and CPU as the machine you will install on: some wheels
# (pydantic-core, cffi) are built for one Python version. DIR gets the Harness
# Manager wheel, the vendored pyverify wheel, the pinned dependencies
# (constraints.txt), constraints.txt itself and SHA256SUMS. Copy DIR across, then:
#
#   scripts/install.sh --offline DIR
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="" out="" extras=""
die() { printf 'make_wheelhouse: %s\n' "$*" >&2; exit 2; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python) [[ $# -ge 2 ]] || die "--python needs a value"; py="$2"; shift 2 ;;
        --with-serial) extras="$extras,serial"; shift ;;
        --with-app) extras="$extras,app"; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [[ -z "$out" ]] || die "one DIR only"; out="$1"; shift ;;
    esac
done
[[ -n "$out" ]] || die "name the output directory (see --help)"
if [[ -z "$py" ]]; then
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cand" >/dev/null 2>&1 \
            && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            py="$cand"; break
        fi
    done
fi
[[ -n "$py" ]] || die "no Python 3.10 or newer found; name one with --python"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# A clean copy, as install.sh does: no build/ or *.egg-info in the checkout.
mkdir "$work/src"
tar -C "$here" --exclude=.git --exclude=.venv --exclude=build --exclude=dist \
    --exclude='*.egg-info' --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.ruff_cache --exclude=screenshots -cf - . | tar -C "$work/src" -xf -

mkdir -p "$out"
out="$(cd "$out" && pwd)"
"$py" -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
spec="$work/src"
if [[ -n "$extras" ]]; then spec="$work/src[${extras#,}]"; fi
"$work/venv/bin/python" -m pip wheel --quiet --disable-pip-version-check \
    --find-links "$here/vendor" --constraint "$here/constraints.txt" -w "$out" "$spec"
cp "$here"/vendor/mps3_pyverify-*.whl "$here/constraints.txt" "$out/"
(cd "$out" && "$work/venv/bin/python" -c "import hashlib, pathlib; print(''.join(
    f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
    for p in sorted(pathlib.Path('.').iterdir()) if p.name != 'SHA256SUMS'), end='')" > SHA256SUMS)
n=0
for _ in "$out"/*.whl; do n=$((n + 1)); done
echo "wheelhouse $out ($n wheels, Python $("$py" -c 'import platform; print(platform.python_version())'))"
echo "install with: scripts/install.sh --offline $out"
