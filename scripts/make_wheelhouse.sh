#!/usr/bin/env bash
# Make a wheelhouse: every wheel Harness Manager needs, for an install with no
# network (scripts/install.sh --offline DIR, or install.ps1 -Offline DIR on Windows).
#
#   scripts/make_wheelhouse.sh [--python PY] [--with-serial] [--with-app] DIR
#   scripts/make_wheelhouse.sh --platform win_amd64 --python-version 3.12 \
#       [--with-serial] [--with-app] DIR
#
# Without --platform, run it on a machine with network, with the SAME Python
# version (3.11, 3.12...), operating system and CPU as the machine you will
# install on: some wheels (pydantic-core, cffi) are built for one Python version.
#
# With --platform (a laptop of ANOTHER kind: win_amd64, macos_arm64 or
# macos_x86_64) and --python-version (that laptop's Python, as 3.12), it
# downloads the wheels for that laptop instead (pip download --platform
# --python-version --only-binary=:all:, pinned by constraints.txt; the markers
# are evaluated as on the laptop: scripts/wheelhouse_cross.py). Harness Manager
# and pyverify are pure Python (py3-none-any): the same wheel everywhere.
#
# Options for --platform:
#   --hm-wheel FILE      use this Harness Manager wheel (make dist) instead of
#                        building one from this checkout
#   --find-links DIR     also look for wheels in DIR
#   --index-url URL      a package index other than PyPI (a mirror)
#   --no-index           only --find-links, never an index (tests use this)
#
# DIR gets the Harness Manager wheel, the vendored pyverify wheel, the pinned
# dependencies (constraints.txt), constraints.txt itself, install.sh and
# install.ps1, and SHA256SUMS. Copy DIR across, then:
#
#   scripts/install.sh --offline DIR                                (Linux, macOS)
#   powershell -ExecutionPolicy Bypass -File DIR\install.ps1 -Offline DIR   (Windows)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="" out="" extras="" platform="" pyver="" hm_wheel="" no_index=0 index_url=""
links=()
die() { printf 'make_wheelhouse: %s\n' "$*" >&2; exit 2; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python) [[ $# -ge 2 ]] || die "--python needs a value"; py="$2"; shift 2 ;;
        --with-serial) extras="$extras,serial"; shift ;;
        --with-app) extras="$extras,app"; shift ;;
        --platform) [[ $# -ge 2 ]] || die "--platform needs a value"; platform="$2"; shift 2 ;;
        --python-version) [[ $# -ge 2 ]] || die "--python-version needs a value"; pyver="$2"; shift 2 ;;
        --hm-wheel) [[ $# -ge 2 ]] || die "--hm-wheel needs a file"; hm_wheel="$2"; shift 2 ;;
        --find-links) [[ $# -ge 2 ]] || die "--find-links needs a directory"; links+=(--find-links "$2"); shift 2 ;;
        --index-url) [[ $# -ge 2 ]] || die "--index-url needs a URL"; index_url="$2"; shift 2 ;;
        --no-index) no_index=1; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [[ -z "$out" ]] || die "one DIR only"; out="$1"; shift ;;
    esac
done
[[ -n "$out" ]] || die "name the output directory (see --help)"
if [[ -n "$platform" && -z "$pyver" ]]; then
    die "--platform $platform needs --python-version (the laptop's Python, as 3.12)"
fi
if [[ -z "$platform" ]]; then
    [[ -z "$pyver" ]] || die "--python-version goes with --platform (else use --python PY)"
    [[ -z "$hm_wheel" && $no_index -eq 0 && -z "$index_url" && ${#links[@]} -eq 0 ]] \
        || die "--hm-wheel, --find-links, --index-url and --no-index go with --platform"
fi
if [[ -n "$hm_wheel" ]]; then
    [[ -f "$hm_wheel" && "$hm_wheel" == *.whl ]] || die "--hm-wheel $hm_wheel is not a wheel file"
    hm_wheel="$(cd "$(dirname "$hm_wheel")" && pwd)/$(basename "$hm_wheel")"
fi
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
mkdir -p "$out"
out="$(cd "$out" && pwd)"
"$py" -m venv "$work/venv"
if [[ $no_index -eq 0 ]]; then
    "$work/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip \
        || printf 'make_wheelhouse: could not upgrade pip; carrying on\n' >&2
fi

# A clean copy, as install.sh does: no build/ or *.egg-info in the checkout.
clean_copy() {
    mkdir "$work/src"
    tar -C "$here" --exclude=.git --exclude=.venv --exclude=build --exclude=dist \
        --exclude='*.egg-info' --exclude=__pycache__ --exclude=.pytest_cache \
        --exclude=.ruff_cache --exclude=screenshots -cf - . | tar -C "$work/src" -xf -
}

if [[ -n "$platform" ]]; then
    # -- another laptop's wheels ---------------------------------------------------------
    if [[ -z "$hm_wheel" ]]; then
        clean_copy
        mkdir "$work/hm"
        "$work/venv/bin/python" -m pip wheel --quiet --disable-pip-version-check --no-deps \
            -w "$work/hm" "$work/src"
        for whl in "$work"/hm/harness_manager-*.whl; do hm_wheel="$whl"; done
        [[ -n "$hm_wheel" ]] || die "could not build the Harness Manager wheel"
    fi
    cross=(--target "$platform" --python-version "$pyver" --hm-wheel "$hm_wheel"
           --constraints "$here/constraints.txt" --extras "${extras#,}" --out "$out"
           --find-links "$here/vendor" ${links[@]+"${links[@]}"})
    if [[ $no_index -eq 1 ]]; then cross+=(--no-index); fi
    if [[ -n "$index_url" ]]; then cross+=(--index-url "$index_url"); fi
    "$work/venv/bin/python" "$here/scripts/wheelhouse_cross.py" "${cross[@]}"
    cp "$hm_wheel" "$out/"
else
    # -- this machine's wheels -----------------------------------------------------------
    clean_copy
    spec="$work/src"
    if [[ -n "$extras" ]]; then spec="$work/src[${extras#,}]"; fi
    # uv too: install.sh puts it in the venv, and the app's self-update builds new versions with it.
    "$work/venv/bin/python" -m pip wheel --quiet --disable-pip-version-check \
        --find-links "$here/vendor" --constraint "$here/constraints.txt" -w "$out" "$spec" "uv>=0.4"
fi
cp "$here"/vendor/mps3_pyverify-*.whl "$here/constraints.txt" "$here/scripts/install.sh" \
    "$here/scripts/install.ps1" "$out/"
(cd "$out" && "$work/venv/bin/python" -c "import hashlib, pathlib; print(''.join(
    f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
    for p in sorted(pathlib.Path('.').iterdir()) if p.is_file() and p.name != 'SHA256SUMS'), end='')" > SHA256SUMS)
n=0
for _ in "$out"/*.whl; do n=$((n + 1)); done
if [[ -n "$platform" ]]; then
    echo "wheelhouse $out ($n wheels, $platform, Python $pyver)"
    case "$platform" in
        win*) echo "install with: powershell -ExecutionPolicy Bypass -File $(basename "$out")\\install.ps1 -Offline $(basename "$out")" ;;
        *) echo "install with: scripts/install.sh --offline $out (Python $pyver on the laptop)" ;;
    esac
else
    echo "wheelhouse $out ($n wheels, Python $("$py" -c 'import platform; print(platform.python_version())'))"
    echo "install with: scripts/install.sh --offline $out"
fi
