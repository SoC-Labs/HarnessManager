#!/usr/bin/env bash
# End-to-end check of scripts/install.sh in a throwaway HOME (Linux and macOS).
#
#   scripts/smoke_install.sh [install.sh options...]
#
# Install, run the command from PATH, start the demo UI and fetch its page (it
# must carry the Content-Security-Policy header), re-run the installer (an
# upgrade in place), then uninstall. Your real HOME is not touched, except
# that the pip and uv download caches are shared with it to save time.
#
# Used by tests/integration/test_l5_install.py and by CI. Exits non-zero on the
# first failed check.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
real_home="$HOME"
cache_home="${XDG_CACHE_HOME:-$real_home/.cache}"

fake_home="$(mktemp -d)"
export HOME="$fake_home"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$cache_home/pip}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$cache_home/uv}"
unset HARNESS_MANAGER_STATE_DIR HARNESS_MANAGER_HOME HARNESS_MANAGER_BIN_DIR XDG_DATA_HOME \
      XDG_CONFIG_HOME VIRTUAL_ENV
export PATH="$fake_home/.local/bin:$PATH"

hm="$fake_home/.local/bin/harness-manager"
log="$fake_home/.config/harness-manager/demo/daemon.log"

step() { printf '== %s\n' "$*"; }
fail() {
    printf 'SMOKE FAIL: %s\n' "$*" >&2
    if [[ -f "$log" ]]; then printf -- '-- %s (tail)\n' "$log" >&2; tail -n 30 "$log" >&2; fi
    exit 1
}
cleanup() {
    if [[ -x "$hm" ]]; then "$hm" daemon stop --demo >/dev/null 2>&1 || true; fi
    rm -rf "$fake_home"
}
trap cleanup EXIT

want="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/pyproject.toml" | head -n 1)"
[[ -n "$want" ]] || fail "no version in pyproject.toml"

step "install ($*)"
"$here/scripts/install.sh" "$@" || fail "install.sh exited $?"
[[ -x "$hm" ]] || fail "$hm is missing"

step "harness-manager version"
[[ "$(command -v harness-manager)" == "$hm" ]] || fail "harness-manager on PATH is $(command -v harness-manager)"
got="$(harness-manager version)" || fail "harness-manager version exited $?"
[[ "$got" == "$want" ]] || fail "version is '$got', pyproject says '$want'"
echo "$got"

step "harness-manager packs"
harness-manager packs | grep -q mps3 || fail "the MPS3 board pack is not listed"

step "the launcher follows the app self-update pointer"
upd="$fake_home/.config/harness-manager/update/app"
mkdir -p "$upd/versions/9.9.9/bin"
printf '#!/bin/sh\necho self-updated "$@"\n' >"$upd/versions/9.9.9/bin/harness-manager"
chmod 755 "$upd/versions/9.9.9/bin/harness-manager"
printf '{\n  "current": "9.9.9",\n  "previous": "%s"\n}\n' "$want" >"$upd/current.json"
[[ "$(harness-manager version)" == "self-updated version" ]] || fail "the launcher ignored current.json"
printf '{"current": "", "previous": ""}\n' >"$upd/current.json"
[[ "$(harness-manager version)" == "$want" ]] || fail "an empty pointer did not fall back to the venv"
rm -rf "$upd"
echo "ok"

step "harness-manager ui --demo --no-browser"
out="$(harness-manager --json ui --demo --no-browser)" || fail "ui exited $?"
url="$(printf '%s' "$out" | sed -n 's/.*"url": *"\([^"]*\)".*/\1/p')"
[[ "$url" == http://127.0.0.1:* ]] || fail "no URL in: $out"
base="${url%%#*}"
echo "$base"

step "the page and its headers"
headers="$(curl -sS -D - -o "$fake_home/index.html" "$base")" || fail "GET $base failed"
printf '%s\n' "$headers" | grep -qi '^HTTP/[0-9.]* 200' || fail "GET $base: $headers"
csp="$(printf '%s\n' "$headers" | grep -i '^content-security-policy:' || true)"
[[ "$csp" == *"script-src 'self'"* ]] || fail "no CSP with script-src 'self': $headers"
echo "${csp%$'\r'}"
grep -qi '<html' "$fake_home/index.html" || fail "the page is not HTML"
health="$(curl -sS "${base%/}/api/v1/health")" || fail "GET /api/v1/health failed"
[[ "$health" == *"\"version\":\"$want\""* || "$health" == *"\"version\": \"$want\""* ]] \
    || fail "health says: $health"
echo "$health"

step "re-run the installer (upgrade in place)"
"$here/scripts/install.sh" "$@" || fail "the second install.sh exited $?"
[[ "$(harness-manager version)" == "$want" ]] || fail "version changed after the re-run"
if harness-manager --json daemon status --demo | grep -q '"state": *"running"'; then
    fail "the demo service still runs after the upgrade (install.sh should stop it)"
fi

step "uninstall"
"$here/scripts/install.sh" --uninstall || fail "install.sh --uninstall exited $?"
[[ ! -e "$hm" ]] || fail "$hm is still there"
[[ ! -d "$fake_home/.local/share/harness-manager/venv" ]] || fail "the venv is still there"
[[ -d "$fake_home/.config/harness-manager" ]] || fail "the state dir was removed (it must stay)"

echo "SMOKE PASS"
