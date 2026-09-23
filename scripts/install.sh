#!/usr/bin/env bash
# Install Harness Manager for this user (Linux and macOS).
#
#   scripts/install.sh [options]
#
# It makes a private venv, installs Harness Manager and pyverify into it, and
# puts one command, `harness-manager`, on your PATH. Re-running it upgrades in
# place. Nothing needs root.
#
# Options:
#   --from SRC      what to install. A checkout (default: the one holding this
#                   script), a wheel file, or a git URL (cloned, depth 1).
#   --ref REF       the branch or tag to clone, with a git URL.
#   --with-app      also install the 'app' extra: pywebview, for a native window.
#   --with-serial   also install the 'serial' extra: pyserial, for the Debug USB
#                   serial ports (the MCC and the FPGA UARTs).
#   --python PY     the Python to build the venv with (3.10 or newer).
#   --no-uv         use venv and pip even when uv is on PATH.
#   --force         replace an existing harness-manager command that this
#                   script did not write.
#   --uninstall     stop the service, remove the venv and the command. Your
#                   settings and backups in ~/.config/harness-manager stay.
#   -h, --help      print this help.
#
# Environment:
#   HARNESS_MANAGER_HOME     install root (default ~/.local/share/harness-manager)
#   HARNESS_MANAGER_BIN_DIR  where the command goes (default ~/.local/bin)
set -euo pipefail

MARKER="harness-manager launcher, written by scripts/install.sh"
MIN_PY="3.10"

say() { printf '%s\n' "$*"; }
note() { printf 'install.sh: %s\n' "$*" >&2; }
die() { printf 'install.sh: error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
checkout="$(cd "$script_dir/.." && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
home_dir="${HARNESS_MANAGER_HOME:-$data_home/harness-manager}"
venv="$home_dir/venv"
bin_dir="${HARNESS_MANAGER_BIN_DIR:-$HOME/.local/bin}"
launcher="$bin_dir/harness-manager"
state_dir="${HARNESS_MANAGER_STATE_DIR:-$HOME/.config/harness-manager}"

src="" ref="" python="" use_uv=1 force=0 uninstall=0
extras=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from) [[ $# -ge 2 ]] || die "--from needs a value"; src="$2"; shift 2 ;;
        --from=*) src="${1#*=}"; shift ;;
        --ref) [[ $# -ge 2 ]] || die "--ref needs a value"; ref="$2"; shift 2 ;;
        --ref=*) ref="${1#*=}"; shift ;;
        --python) [[ $# -ge 2 ]] || die "--python needs a value"; python="$2"; shift 2 ;;
        --python=*) python="${1#*=}"; shift ;;
        --with-app) extras+=(app); shift ;;
        --with-serial) extras+=(serial); shift ;;
        --no-uv) use_uv=0; shift ;;
        --force) force=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

# The launcher is ours if it carries the marker, or is a symlink into our venv.
ours() {
    if [[ -L "$1" ]]; then
        case "$(readlink "$1")" in "$venv"/*) return 0 ;; esac
        return 1
    fi
    [[ -f "$1" ]] && grep -q "$MARKER" "$1" 2>/dev/null
}

# Stop the Harness Manager service (and the demo one) before the venv changes under it.
# A running job refuses the stop, and then this script stops too.
stop_daemons() {
    local hm="$venv/bin/harness-manager" rc err flag
    [[ -x "$hm" ]] || return 0
    for flag in "" "--demo"; do
        rc=0
        # shellcheck disable=SC2086 # $flag is empty or one word
        err="$("$hm" daemon stop $flag 2>&1 >/dev/null)" || rc=$?
        case "$rc" in
            0|8) ;;   # stopped, or it was not running (8 = ALREADY)
            *) printf '%s\n' "$err" >&2
               die "the Harness Manager service did not stop (\`harness-manager daemon stop \
$flag\` exited $rc). If a job is running, wait for it; or stop it with --force. Then run \
this again." ;;
        esac
    done
}

if [[ $uninstall -eq 1 ]]; then
    stop_daemons
    if [[ -e "$launcher" || -L "$launcher" ]]; then
        if ours "$launcher"; then rm -f "$launcher"; say "removed  $launcher"
        else note "left $launcher alone: this script did not write it"; fi
    fi
    if [[ -d "$venv" ]]; then rm -rf "$venv"; say "removed  $venv"; fi
    rmdir "$home_dir" 2>/dev/null || true
    say "kept     $state_dir (settings, SD backups, the content store)."
    say "         Delete it yourself if you want it gone."
    say "Harness Manager is uninstalled."
    exit 0
fi

# -- what to install -------------------------------------------------------------------
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

is_git_url() {
    case "$1" in
        http://*|https://*|ssh://*|git://*|file://*|git@*:*) return 0 ;;
        *.git) [[ ! -d "$1" ]] ;;
        *) return 1 ;;
    esac
}

find_links=()
add_links() { [[ -d "$1" ]] && find_links+=(--find-links "$1"); return 0; }

if [[ -z "$src" ]]; then
    src="$checkout"
fi
if is_git_url "$src"; then
    command -v git >/dev/null || die "git is needed to install from $src"
    say "cloning  $src${ref:+ ($ref)}"
    git clone --quiet --depth 1 ${ref:+--branch "$ref"} "$src" "$work/src" \
        || die "could not clone $src (for the private repo, check your GitHub SSH key)"
    pkg="$work/src"
    add_links "$pkg/vendor"
    what="$src${ref:+@$ref}"
elif [[ -d "$src" ]]; then
    what="$(cd "$src" && pwd)"
    grep -q '^name = "harness-manager"' "$what/pyproject.toml" 2>/dev/null \
        || die "$what is not a Harness Manager checkout (no pyproject.toml naming harness-manager)"
    # Build from a clean copy: pip would otherwise write build/ and *.egg-info into the
    # checkout, and a stale build/lib can carry deleted files into the wheel.
    mkdir "$work/src"
    tar -C "$what" --exclude=.git --exclude=.venv --exclude=build --exclude=dist \
        --exclude='*.egg-info' --exclude=__pycache__ --exclude=.pytest_cache \
        --exclude=.ruff_cache --exclude=screenshots -cf - . | tar -C "$work/src" -xf -
    pkg="$work/src"
    add_links "$pkg/vendor"
elif [[ -f "$src" && "$src" == *.whl ]]; then
    pkg="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"
    add_links "$(dirname "$pkg")"
    add_links "$checkout/vendor"
    what="$pkg"
else
    die "--from $src is not a checkout, a wheel file or a git URL"
fi

pyverify_wheel=""
for ((i = 1; i < ${#find_links[@]}; i += 2)); do
    for whl in "${find_links[$i]}"/mps3_pyverify-*.whl; do
        [[ -f "$whl" ]] && pyverify_wheel="$whl"
    done
    [[ -n "$pyverify_wheel" ]] && break
done
[[ -n "$pyverify_wheel" ]] || die "no pyverify wheel (vendor/mps3_pyverify-*.whl) next to $what"

spec="$pkg"
if [[ ${#extras[@]} -gt 0 ]]; then
    spec="${pkg}[$(IFS=,; echo "${extras[*]}")]"
fi

# -- the venv --------------------------------------------------------------------------
py_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

if [[ $use_uv -eq 1 ]] && ! command -v uv >/dev/null 2>&1; then use_uv=0; fi

if [[ -x "$venv/bin/python" ]] && py_ok "$venv/bin/python"; then
    stop_daemons
    say "upgrade  $venv"
else
    rm -rf "$venv"
    mkdir -p "$home_dir"
    if [[ $use_uv -eq 1 ]]; then
        # uv finds a Python >= 3.10, or downloads one.
        uv venv --quiet --python "${python:->=$MIN_PY}" "$venv" \
            || die "uv could not make a venv with Python >= $MIN_PY"
    else
        base=""
        if [[ -n "$python" ]]; then
            py_ok "$python" || die "$python is not Python $MIN_PY or newer"
            base="$python"
        else
            for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
                if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then base="$cand"; break; fi
            done
        fi
        [[ -n "$base" ]] || die "Harness Manager needs Python $MIN_PY or newer, and none was \
found. Install one (python.org, your package manager, or uv: https://docs.astral.sh/uv/), \
or name it with --python."
        "$base" -m venv "$venv" 2>"$work/venv.err" || {
            cat "$work/venv.err" >&2
            die "$base could not make a venv. On Debian or Ubuntu: \
sudo apt install python3-venv (or python3.X-venv for your version)."
        }
    fi
    say "venv     $venv ($("$venv/bin/python" -c 'import platform; print(platform.python_version())'))"
fi

# -- install ---------------------------------------------------------------------------
say "install  $what${extras[*]:+ [${extras[*]}]}"
if [[ $use_uv -eq 1 ]]; then
    uvpip() { uv pip install --quiet --python "$venv/bin/python" "$@"; }
    # pyverify keeps its version number across commits: always reinstall the vendored one.
    uvpip --reinstall-package mps3-pyverify --no-deps "$pyverify_wheel"
    # --upgrade-package, not --upgrade: an upgrade of everything could swap the vendored
    # pyverify for a same-named package from the index.
    uvpip --upgrade-package harness-manager --reinstall-package harness-manager \
        "${find_links[@]}" "$spec"
else
    "$venv/bin/python" -m pip --version >/dev/null 2>&1 \
        || "$venv/bin/python" -m ensurepip --upgrade >/dev/null
    pip() { "$venv/bin/python" -m pip --disable-pip-version-check "$@"; }
    pip install --quiet --upgrade pip || note "could not upgrade pip; carrying on with $(pip --version)"
    pip install --quiet --force-reinstall --no-deps "$pyverify_wheel"
    # pip's default upgrade strategy (only-if-needed) keeps the pyverify just installed.
    pip install --quiet --upgrade "${find_links[@]}" "$spec"
    if [[ "$pkg" == *.whl ]]; then
        # pip leaves a wheel of the same version alone; the file may still be newer.
        pip install --quiet --force-reinstall --no-deps "$pkg"
    fi
fi
version="$("$venv/bin/harness-manager" version)" || die "the installed harness-manager does not run"

# -- the command on PATH ---------------------------------------------------------------
mkdir -p "$bin_dir"
if [[ -e "$launcher" || -L "$launcher" ]] && ! ours "$launcher"; then
    if [[ $force -eq 1 ]]; then
        note "replacing $launcher (--force)"
    else
        die "$launcher exists and this script did not write it. Move it away, or re-run \
with --force. Until then, run $venv/bin/harness-manager"
    fi
fi
q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
cat >"$launcher.tmp" <<EOF
#!/bin/sh
# $MARKER.
# Re-run the installer to upgrade; \`install.sh --uninstall\` removes it.
# Runs the version the app's self-update selected (<state dir>/update/app/current.json)
# when there is one, else the installed venv.
venv=$(q "$venv")
state="\${HARNESS_MANAGER_STATE_DIR:-\$HOME/.config/harness-manager}"
pointer="\$state/update/app/current.json"
if [ -f "\$pointer" ]; then
    v=\$(sed -n 's/.*"current"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "\$pointer" | head -n 1)
    if [ -n "\$v" ] && [ -x "\$state/update/app/versions/\$v/bin/harness-manager" ]; then
        exec "\$state/update/app/versions/\$v/bin/harness-manager" "\$@"
    fi
fi
exec "\$venv/bin/harness-manager" "\$@"
EOF
chmod 755 "$launcher.tmp"
mv -f "$launcher.tmp" "$launcher"

pointer="$state_dir/update/app/current.json"
if [[ -f "$pointer" ]] && grep -q '"current"[[:space:]]*:[[:space:]]*"[^"]' "$pointer"; then
    note "the app's self-update selected a version in $pointer; the command runs that one"
    note "until you roll it back. The version just installed is $version."
fi

say "command  $launcher"
say ""
say "Harness Manager $version is installed."
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        say ""
        say "$bin_dir is not on your PATH. Add this line to your shell's startup file"
        say "(~/.bashrc, ~/.zshrc), then open a new terminal:"
        say "    export PATH=\"$bin_dir:\$PATH\""
        ;;
esac
say ""
say "Next:"
say "  harness-manager app --demo      the app with demo boards, no hardware needed"
say "  harness-manager ui --demo       the same in a browser tab (over ssh it prints the URL)"
say "  harness-manager info 192.168.10.101    a real board on your network"
say "Guide: docs/USER_GUIDE.md in the Harness Manager repo"
