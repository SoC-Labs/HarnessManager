#!/usr/bin/env bash
# Pin the pyverify wheel Harness Manager ships with: rewrites pyverify.pin and the pyproject.toml
# dependency in one go, so they cannot disagree.
#
#   scripts/pin_pyverify.sh <version> <sha256>            e.g. 2.2.1 <64 hex>
#   scripts/pin_pyverify.sh --check                        only check they agree
#   scripts/pin_pyverify.sh --wheel                        print the path of the verified pinned
#                                                          wheel (vendor/, else downloaded into
#                                                          .pyverify-cache/); used by the Makefile
#
# <version> is the platform release version (the tag is v<version>); <sha256> is the one in
# that release's SHA256SUMS. If vendor/ still holds a wheel, it is removed when it is not the
# pinned one (the pin wins; vendor/ is a dev and CI fallback only while the release is
# unpublished).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { printf 'pin_pyverify: %s\n' "$*" >&2; exit 2; }
pin="$here/pyverify.pin" pyproject="$here/pyproject.toml"
base="https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/releases/download"

check() {
    local v
    v="$(grep -E '^version=' "$pin" | tail -n 1 | cut -d= -f2)"
    grep -q "\"mps3-pyverify==$v\"" "$pyproject" \
        || die "pyproject.toml does not depend on mps3-pyverify==$v (pyverify.pin says $v)"
    echo "pyverify pin: $v consistent"
}

if [[ "${1:-}" == "--check" ]]; then check; exit 0; fi
if [[ "${1:-}" == "--wheel" ]]; then
    pyverify_wheel="" pyverify_from=""
    # shellcheck disable=SC1091
    . "$here/scripts/pyverify_pin.sh"
    pyverify_resolve "$pin" "${HM_PYVERIFY_CACHE:-$here/.pyverify-cache}" "" "$here/vendor"
    printf '%s\n' "$pyverify_wheel"
    exit 0
fi
[[ $# -eq 2 ]] || die "usage: pin_pyverify.sh <version> <sha256> | --check | --wheel"
version="$1" sha="$2"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version '$version' is not X.Y.Z"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "sha256 must be 64 lowercase hex digits"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
grep -E '^#' "$pin" | grep -v 'PLACEHOLDER' > "$tmp" || true
{
    cat "$tmp"
    echo "version=$version"
    echo "sha256=$sha"
    echo "url=$base/v$version/mps3_pyverify-$version-py3-none-any.whl"
} > "$pin"

sed -i -E "s/\"mps3-pyverify[^\"]*\"/\"mps3-pyverify==$version\"/" "$pyproject"

for whl in "$here"/vendor/mps3_pyverify-*.whl; do
    [[ -f "$whl" ]] || continue
    got="$(sha256sum "$whl" | awk '{print $1}')"
    [[ "$got" == "$sha" ]] || echo "pin_pyverify: vendor/$(basename "$whl") is not the pinned wheel; it will be ignored (git rm it when v$version is published)" >&2
done
check
echo "pinned mps3-pyverify $version ($sha)"
