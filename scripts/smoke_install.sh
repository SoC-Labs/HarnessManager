#!/usr/bin/env bash
# End-to-end check of scripts/install.sh in a throwaway HOME (Linux and macOS).
#
#   scripts/smoke_install.sh [install.sh options...]
#
# Install, run the command from PATH, check the desktop menu entry (Linux),
# start the demo UI and fetch its page (it must carry the
# Content-Security-Policy header), re-run the installer (an upgrade in place),
# then uninstall. Your real HOME is not touched, except that the pip and uv
# download caches are shared with it to save time.
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
# Consoles as terminal devices would live in /tmp/harness-manager-$USER: keep them here.
export HARNESS_MANAGER_PTY_DIR="$fake_home/pty"
export PATH="$fake_home/.local/bin:$PATH"
started=$SECONDS

hm="$fake_home/.local/bin/harness-manager"
log="$fake_home/.config/harness-manager/demo/daemon.log"
menu="$fake_home/.local/share/applications/harness-manager.desktop"
icon="$fake_home/.local/share/icons/hicolor/scalable/apps/harness-manager.svg"
# curl straight to the loopback UI, even behind a proxy (http_proxy set, no no_proxy)
fetch() { curl --noproxy '*' -sS "$@"; }

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
echo "installed in $((SECONDS - started)) s"

step "harness-manager --version"
got="$(harness-manager --version)" || fail "harness-manager --version exited $?"
[[ "$got" == "Harness Manager $want" ]] || fail "--version says '$got', pyproject says '$want'"
echo "$got"

step "harness-manager version"
[[ "$(command -v harness-manager)" == "$hm" ]] || fail "harness-manager on PATH is $(command -v harness-manager)"
got="$(harness-manager version)" || fail "harness-manager version exited $?"
[[ "$got" == "$want" ]] || fail "version is '$got', pyproject says '$want'"
echo "$got"

step "harness-manager packs"
harness-manager packs | grep -q mps3 || fail "the MPS3 board pack is not listed"

step "the install record, uv, and the launcher following the self-update pointer"
root="$fake_home/.local/share/harness-manager"
grep -q '"version": "'"$want"'"' "$root/install.json" || fail "no install record in $root/install.json"
[[ -x "$root/venv/bin/uv" ]] || fail "no uv in the venv: the app could not update itself"
grep -q '"installer"' "$root/current.json" || fail "the pointer does not know the installed venv"
# A stand-in for a self-updated venv's python: the launcher runs `python -c <boot> ARGS`.
mkdir -p "$root/versions/9.9.9/bin"
printf '#!/bin/sh\nshift 2\necho self-updated "$@"\n' >"$root/versions/9.9.9/bin/python"
chmod 755 "$root/versions/9.9.9/bin/python"
cp "$root/current.json" "$fake_home/current.json.saved"
printf '{"current": "9.9.9", "previous": ""}\n' >"$root/current.json"
[[ "$(harness-manager version)" == "self-updated version" ]] || fail "the launcher ignored current.json"
[[ "$(HARNESS_MANAGER_USE_INSTALLED=1 harness-manager version)" == "$want" ]] \
    || fail "HARNESS_MANAGER_USE_INSTALLED=1 did not run the installed venv"
cp "$fake_home/current.json.saved" "$root/current.json"
[[ "$(harness-manager version)" == "$want" ]] || fail "an empty pointer did not fall back to the venv"
rm -rf "$root/versions"
echo "ok"

if [[ "$(uname -s)" == Linux ]]; then
    step "the desktop menu entry"
    [[ -f "$menu" ]] || fail "$menu is missing"
    grep -qxF "Exec=\"$hm\" app" "$menu" || fail "$menu does not run $hm app: $(cat "$menu")"
    grep -qxF "Icon=$icon" "$menu" || fail "$menu has the wrong icon"
    [[ -f "$icon" ]] || fail "$icon is missing"
    if command -v desktop-file-validate >/dev/null 2>&1; then
        desktop-file-validate "$menu" || fail "desktop-file-validate $menu"
    fi
    echo "ok"
fi

step "harness-manager ui --demo --no-browser"
out="$(harness-manager --json ui --demo --no-browser)" || fail "ui exited $?"
url="$(printf '%s' "$out" | sed -n 's/.*"url": *"\([^"]*\)".*/\1/p')"
[[ "$url" == http://127.0.0.1:* ]] || fail "no URL in: $out"
base="${url%%#*}"
echo "$base"

step "the page and its headers"
headers="$(fetch -D - -o "$fake_home/index.html" "$base")" || fail "GET $base failed"
printf '%s\n' "$headers" | grep -qi '^HTTP/[0-9.]* 200' || fail "GET $base: $headers"
csp="$(printf '%s\n' "$headers" | grep -i '^content-security-policy:' || true)"
[[ "$csp" == *"script-src 'self'"* ]] || fail "no CSP with script-src 'self': $headers"
echo "${csp%$'\r'}"
grep -qi '<html' "$fake_home/index.html" || fail "the page is not HTML"
health="$(fetch "${base%/}/api/v1/health")" || fail "GET /api/v1/health failed"
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
[[ ! -e "$menu" && ! -e "$icon" ]] || fail "the menu entry or its icon is still there"
[[ ! -e "$fake_home/.local/share/harness-manager" ]] || fail "the install root is still there"

echo "SMOKE PASS ($((SECONDS - started)) s)"
