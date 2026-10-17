# shellcheck shell=bash
# Sourced by install.sh and make_wheelhouse.sh (and copied into a wheelhouse next to install.sh).
# Finds the ONE pyverify wheel Harness Manager is pinned to (pyverify.pin: version, sha256,
# url) and never lets an unverified wheel through. The caller defines die().
#
#   pyverify_pin_get FILE KEY                  value of KEY in the pin file
#   pyverify_sha256 FILE                       sha256 of a file
#   pyverify_resolve PIN WORKDIR OFFLINE VENDOR_DIR...
#       sets pyverify_wheel (a path) and pyverify_from (what it came from). Order:
#         (a) OFFLINE dir (the wheelhouse), a wheel whose sha256 equals the pin;
#         (b) each VENDOR_DIR, same test (dev and CI, until the release is published);
#         (c) download the pin's url into WORKDIR, unless OFFLINE is set, and check the sha256.
#       Anything else is refused. HM_PYVERIFY_URL replaces the pin's url (a mirror; the sha256
#       is still checked).

pyverify_pin_get() {   # bash builtins only: the installer may run with a bare PATH
    local line value=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$2="* ]]; then value="${line#*=}"; fi
    done < "$1"
    printf '%s' "${value%$'\r'}"
}

pyverify_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" | awk '{print $NF}'
    else die "no sha256sum, shasum or openssl to check the pyverify wheel with"
    fi
}

# the first wheel in DIR whose sha256 is $2, or nothing
pyverify_find_in() {
    local dir="$1" want="$2" whl
    [[ -d "$dir" ]] || return 0
    for whl in "$dir"/mps3_pyverify-*.whl; do
        if [[ -f "$whl" && "$(pyverify_sha256 "$whl")" == "$want" ]]; then
            printf '%s' "$whl"; return 0
        fi
    done
    return 0
}

pyverify_download() {   # URL DEST
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 --connect-timeout 20 --max-time 300 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        python3 - "$1" "$2" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=300) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())
PY
    fi
}

# shellcheck disable=SC2034  # pyverify_wheel and pyverify_from are read by the caller
pyverify_resolve() {
    local pin="$1" work="$2" offline="$3"; shift 3
    local version want url found dir dest got
    [[ -f "$pin" ]] || die "no pyverify.pin (the pyverify version and sha256 this release is pinned to) at $pin"
    version="$(pyverify_pin_get "$pin" version)"
    want="$(pyverify_pin_get "$pin" sha256)"
    url="${HM_PYVERIFY_URL:-$(pyverify_pin_get "$pin" url)}"
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "$pin has no valid sha256 line"
    [[ -n "$version" && -n "$url" ]] || die "$pin needs version= and url= lines"
    pyverify_wheel="" pyverify_from=""
    if [[ -n "$offline" ]]; then
        found="$(pyverify_find_in "$offline" "$want")"
        if [[ -n "$found" ]]; then pyverify_wheel="$found" pyverify_from="the wheelhouse $offline"; return 0; fi
    fi
    for dir in "$@"; do
        found="$(pyverify_find_in "$dir" "$want")"
        if [[ -n "$found" ]]; then pyverify_wheel="$found" pyverify_from="$dir"; return 0; fi
    done
    if [[ -n "$offline" ]]; then
        die "no mps3_pyverify wheel with sha256 $want (version $version) in $offline. \
Make the wheelhouse again with scripts/make_wheelhouse.sh (it downloads and verifies the pinned wheel)."
    fi
    dest="$work/${url##*/}"
    case "$dest" in *.whl) ;; *) dest="$work/mps3_pyverify-$version-py3-none-any.whl" ;; esac
    mkdir -p "$work"
    pyverify_download "$url" "$dest" 2>/dev/null \
        || { rm -f "$dest"; die "could not download the pinned pyverify wheel from $url. \
Nothing was installed. If the release is not published yet, or you have no network, use a wheelhouse \
(--offline DIR) or a checkout whose vendor/ holds the wheel; expected sha256 $want"; }
    got="$(pyverify_sha256 "$dest")"
    if [[ "$got" != "$want" ]]; then
        rm -f "$dest"
        die "the pyverify wheel downloaded from $url has sha256 $got, but pyverify.pin says $want. \
It was NOT installed. Do not work around this: ask the release owner."
    fi
    pyverify_wheel="$dest" pyverify_from="$url"
}
