#!/usr/bin/env bash
# Install Harness Manager for this user (Linux and macOS).
#
#   scripts/install.sh [options]
#
# It makes a private venv, installs Harness Manager and pyverify into it, puts
# one command, `harness-manager`, on your PATH, and (on Linux) adds Harness
# Manager to your desktop's application menu. Re-running it upgrades in place
# and keeps your choices. Nothing needs root.
#
# The command runs the version the app's self-update selected, else the one
# installed here. Re-running the installer makes the version it installs the one
# that runs, unless a self-updated version is newer.
#
# Options:
#   --from SRC      what to install. A checkout (default: the one holding this
#                   script), a wheel file, or a git URL (cloned, depth 1).
#   --ref REF       the branch or tag to clone, with a git URL.
#   --with-app      also install the 'app' extra: pywebview, for a native window
#                   (macOS, Windows). On Linux the app uses Chrome or Chromium.
#   --with-serial   also install the 'serial' extra: pyserial, for the Debug USB
#                   serial ports (the MCC and the FPGA UARTs).
#   --python PY     the Python to build the venv with (3.10 or newer).
#   --no-uv         use venv and pip even when uv is on PATH.
#   --offline DIR   install only from DIR, a wheelhouse made by
#                   scripts/make_wheelhouse.sh; never contact the package index.
#   --latest        the newest dependency versions, not the tested ones pinned
#                   in constraints.txt (it rebuilds the venv).
#   --no-desktop    do not add Harness Manager to the application menu (Linux).
#   --desktop       add it again after an earlier --no-desktop.
#   --no-path       do not edit your shell startup files when the command's directory
#                   is not on PATH (the default adds ONE marked block to them).
#   --path          add it again after an earlier --no-path.
#   --force         replace an existing harness-manager command that this
#                   script did not write.
#   --uninstall     stop the service, remove the venv, the self-updated versions,
#                   the command and the menu entry. Your settings and backups in
#                   ~/.config/harness-manager stay.
#   -h, --help      print this help.
#
# Environment:
#   HARNESS_MANAGER_HOME     install root (default ~/.local/share/harness-manager)
#   HARNESS_MANAGER_BIN_DIR  where the command goes (default ~/.local/bin)
#   HARNESS_MANAGER_STATE_DIR  the state dir (default ~/.config/harness-manager). An
#                            older install kept its self-updated versions in it.
#   HTTPS_PROXY              a proxy for the package index (pip and uv use it)
set -euo pipefail

MARKER="harness-manager launcher, written by scripts/install.sh"
DESKTOP_MARKER="X-HarnessManager-Installer=scripts/install.sh"
MIN_PY="3.10"
# The newest first. `python3` last: on RHEL 8 it is 3.6, on Rocky 9 it is 3.9.
PY_CANDIDATES="python3.14 python3.13 python3.12 python3.11 python3.10 python3 python"

say() { printf '%s\n' "$*"; }
note() { printf 'install.sh: %s\n' "$*" >&2; }
die() { printf 'install.sh: error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
checkout="$(cd "$script_dir/.." && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
home_dir="${HARNESS_MANAGER_HOME:-$data_home/harness-manager}"
venv="$home_dir/venv"
record="$home_dir/install.conf"
# The app's self-update keeps its versions and its pointer in the install root
# (harness_manager._launch). An older install kept them in <state dir>/update/app.
install_json="$home_dir/install.json"
lock_dir="$home_dir/.install.lock"
bin_dir="${HARNESS_MANAGER_BIN_DIR:-$HOME/.local/bin}"
launcher="$bin_dir/harness-manager"
state_dir="${HARNESS_MANAGER_STATE_DIR:-$HOME/.config/harness-manager}"
legacy_app="$state_dir/update/app"
apps_dir="$data_home/applications"
desktop_file="$apps_dir/harness-manager.desktop"
icon_file="$data_home/icons/hicolor/scalable/apps/harness-manager.svg"
os="$(uname -s)"

src="" ref="" python="" use_uv=1 force=0 uninstall=0 offline="" latest=0 desktop="" editpath=""
extras=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from) [[ $# -ge 2 ]] || die "--from needs a value"; src="$2"; shift 2 ;;
        --from=*) src="${1#*=}"; shift ;;
        --ref) [[ $# -ge 2 ]] || die "--ref needs a value"; ref="$2"; shift 2 ;;
        --ref=*) ref="${1#*=}"; shift ;;
        --python) [[ $# -ge 2 ]] || die "--python needs a value"; python="$2"; shift 2 ;;
        --python=*) python="${1#*=}"; shift ;;
        --offline) [[ $# -ge 2 ]] || die "--offline needs a directory"; offline="$2"; shift 2 ;;
        --offline=*) offline="${1#*=}"; shift ;;
        --with-app) extras="$extras app"; shift ;;
        --with-serial) extras="$extras serial"; shift ;;
        --no-uv) use_uv=0; shift ;;
        --latest) latest=1; shift ;;
        --no-desktop) desktop=0; shift ;;
        --desktop) desktop=1; shift ;;
        --no-path) editpath=0; shift ;;
        --path) editpath=1; shift ;;
        --force) force=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

# sudo would install into root's home (or leave root-owned files in yours).
if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
    die "run this without sudo. It installs for your user, into your home directory, \
and needs no root."
fi

# -- the lock: one install at a time, per install root --------------------------------
work=""
locked=0
cleanup() {
    [[ -n "$work" ]] && rm -rf "$work"
    rm -f "$launcher.tmp" "$desktop_file.tmp" 2>/dev/null || true
    if [[ $locked -eq 1 ]]; then rm -rf "$lock_dir"; fi
    rmdir "$home_dir" 2>/dev/null || true   # only when a failed first install left it empty
}
trap cleanup EXIT
trap 'note "interrupted; run it again to finish (it resumes safely)"; exit 130' INT
trap 'note "stopped; run it again to finish (it resumes safely)"; exit 143' TERM HUP

# A directory we can create and write, or a clear reason why not.
need_dir() {  # DIR WHAT HOW-TO-MOVE-IT
    if ! mkdir -p "$1" 2>/dev/null || [[ ! -w "$1" ]]; then
        die "cannot write to $1 ($2). Check its owner and permissions, or $3."
    fi
}

take_lock() {
    local pid tries=0
    while ! mkdir "$lock_dir" 2>/dev/null; do
        pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            die "another Harness Manager install (pid $pid) is running for $home_dir. \
Wait for it to finish, then run this again. If none is running, remove $lock_dir."
        fi
        tries=$((tries + 1))
        if [[ -z "$pid" && $tries -le 3 ]]; then sleep 1; continue; fi  # it may be starting
        [[ $tries -le 6 ]] || die "could not take the install lock $lock_dir; remove it and run this again"
        rm -rf "$lock_dir"   # stale: its install was killed, or the machine restarted
    done
    locked=1
    printf '%s\n' "$$" >"$lock_dir/pid"
}

need_dir "$home_dir" "the install root" "set HARNESS_MANAGER_HOME to a directory you own"
take_lock

# -- remembered choices (extras, the menu entry) -----------------------------------------
if [[ -f "$record" ]]; then
    while IFS='=' read -r key value; do
        case "$key" in
            extras) extras="$value $extras" ;;
            desktop) [[ -n "$desktop" ]] || desktop="$value" ;;
            path) [[ -n "$editpath" ]] || editpath="$value" ;;
        esac
    done <"$record"
fi
[[ -n "$desktop" ]] || desktop=1
[[ -n "$editpath" ]] || editpath=1
# Deduplicated and in a fixed order: the set of extras, kept across re-runs.
wanted=""
for e in app ina260 serial; do
    case " $extras " in *" $e "*) wanted="$wanted $e" ;; esac
done
extras="${wanted# }"

# The launcher is ours if it carries the marker, or is a symlink into our venv.
ours() {
    if [[ -L "$1" ]]; then
        case "$(readlink "$1")" in "$venv"/*) return 0 ;; esac
        return 1
    fi
    [[ -f "$1" ]] && grep -q "$MARKER" "$1" 2>/dev/null
}

# True when the service for state dir $1 is gone: no daemon.json, no such process, or
# a zombie. An installed version before 0.1.0's fix waited on a zombie (in a container
# whose PID 1 never reaps, a stopped service stays one) and reported "did not stop".
service_gone() {
    local json="$1/daemon.json" pid st
    [[ -f "$json" ]] || return 0
    pid="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$json" | head -n 1)"
    [[ -n "$pid" ]] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    if [[ -r "/proc/$pid/stat" ]]; then
        st="$(sed 's/.*) //' "/proc/$pid/stat" | cut -d ' ' -f 1)"
        [[ "$st" == Z || "$st" == X ]] && return 0
    fi
    return 1
}

# Stop the Harness Manager service (and the demo one) before the venv changes under it.
# A running job refuses the stop, and then this script stops too.
stop_daemons() {
    local hm="$venv/bin/harness-manager" rc err flag sdir
    [[ -x "$hm" ]] || return 0
    for flag in "" "--demo"; do
        rc=0
        sdir="$state_dir${flag:+/demo}"
        # shellcheck disable=SC2086 # $flag is empty or one word
        err="$("$hm" daemon stop $flag 2>&1 >/dev/null)" || rc=$?
        case "$rc" in
            0|8) ;;   # stopped, or it was not running (8 = ALREADY)
            *) if service_gone "$sdir"; then
                   note "the Harness Manager service${flag:+ (demo)} had already exited; carrying on"
                   continue
               fi
               printf '%s\n' "$err" >&2
               die "the Harness Manager service did not stop (\`harness-manager daemon stop \
$flag\` exited $rc). If a job is running, wait for it; or stop it with --force. Then run \
this again." ;;
        esac
    done
}

remove_desktop_entry() {
    if [[ -f "$desktop_file" ]] && grep -q "$DESKTOP_MARKER" "$desktop_file" 2>/dev/null; then
        rm -f "$desktop_file"
        say "removed  $desktop_file"
        if [[ -f "$icon_file" ]]; then rm -f "$icon_file"; fi
    fi
}

PATH_BEGIN="# >>> harness-manager PATH (written by scripts/install.sh) >>>"
PATH_END="# <<< harness-manager PATH <<<"

# The startup files to edit for a shell: new terminals, then login shells (ssh, `bash -l`).
# bash's login file is the first that exists of ~/.bash_profile, ~/.bash_login, ~/.profile
# (a new ~/.bash_profile would hide an existing ~/.profile), else ~/.bash_profile.
# macOS Terminal starts login shells: bash gets the login file only. fish, tcsh and csh
# are not edited (path_advice tells them what to do).
path_files() {  # SHELL-NAME OS HOME
    local sh="$1" os_="$2" home="$3" f login=.bash_profile
    case "$sh" in
        zsh) echo .zshrc; echo .zprofile ;;
        bash)
            for f in .bash_profile .bash_login .profile; do
                if [[ -f "$home/$f" ]]; then login="$f"; break; fi
            done
            if [[ "$os_" != Darwin ]]; then echo .bashrc; fi
            echo "$login" ;;
    esac
}

# Remove our marked block from file $1 (no-op when it has none).
strip_path_block() {
    local f="$1" tmp
    [[ -f "$f" ]] && grep -qF "$PATH_BEGIN" "$f" 2>/dev/null || return 0
    tmp="$f.hm-tmp"
    awk -v b="$PATH_BEGIN" -v e="$PATH_END" '
        $0 == b { skip = 1; next }
        skip && $0 == e { skip = 0; next }
        !skip { print }' "$f" >"$tmp" && cat "$tmp" >"$f"
    rm -f "$tmp"
}

# Append the one marked block to file $1, once: a re-install finds it and adds nothing.
# $2 is the PATH line. Prints what it did.
add_path_block() {
    local f="$1" line="$2"
    if [[ -f "$f" ]] && grep -qxF "$line" "$f" && grep -qF "$PATH_BEGIN" "$f"; then
        say "path     $f already has the Harness Manager PATH block"
        return 0
    fi
    strip_path_block "$f"   # an older block, for another directory
    if ! { if [[ -s "$f" && -n "$(tail -c 1 "$f")" ]]; then printf '\n' >>"$f"; fi
           printf '%s\n%s\n%s\n' "$PATH_BEGIN" "$line" "$PATH_END" >>"$f"; } 2>/dev/null; then
        note "could not write $f"
        return 1
    fi
    say "path     added the PATH block to $f"
}

remove_path_blocks() {
    local f
    for f in .bashrc .bash_profile .bash_login .profile .zshrc .zprofile; do
        if [[ -f "$HOME/$f" ]] && grep -qF "$PATH_BEGIN" "$HOME/$f" 2>/dev/null; then
            strip_path_block "$HOME/$f"
            say "removed  the PATH block from $HOME/$f"
        fi
    done
}

if [[ $uninstall -eq 1 ]]; then
    stop_daemons
    if [[ -e "$launcher" || -L "$launcher" ]]; then
        if ours "$launcher"; then rm -f "$launcher"; say "removed  $launcher"
        else note "left $launcher alone: this script did not write it"; fi
    fi
    remove_desktop_entry
    remove_path_blocks
    if [[ -d "$venv" ]]; then rm -rf "$venv"; say "removed  $venv"; fi
    # The self-updated versions are venvs, not settings: they go too, from the install
    # root and from where an older install kept them.
    for dir in "$home_dir" "$legacy_app"; do
        if [[ -d "$dir/versions" ]]; then rm -rf "$dir/versions"; say "removed  $dir/versions"; fi
        rm -rf "$dir/wheels" "$dir/reqs"
        rm -f "$dir/current.json"
    done
    rmdir "$legacy_app" 2>/dev/null || true
    rm -f "$record" "$install_json"
    rm -rf "$lock_dir"; locked=0
    rmdir "$home_dir" 2>/dev/null || true
    say "kept     $state_dir (settings, SD backups, the content store)."
    say "         Delete it yourself if you want it gone."
    say "Harness Manager is uninstalled."
    exit 0
fi

need_dir "$bin_dir" "where the command goes" "set HARNESS_MANAGER_BIN_DIR to a directory you own"
work="$(mktemp -d 2>/dev/null)" || die "cannot make a temporary directory in ${TMPDIR:-/tmp}; \
set TMPDIR to a directory you can write"

# -- what to install -------------------------------------------------------------------
is_git_url() {
    case "$1" in
        http://*|https://*|ssh://*|git://*|file://*|git@*:*) return 0 ;;
        *.git) [[ ! -d "$1" ]] ;;
        *) return 1 ;;
    esac
}

find_links=()
add_links() { [[ -d "$1" ]] && find_links+=(--find-links "$1"); return 0; }

if [[ -n "$offline" ]]; then
    [[ -d "$offline" ]] || die "--offline $offline is not a directory (make one with \
scripts/make_wheelhouse.sh on a machine with network)"
    offline="$(cd "$offline" && pwd)"
    add_links "$offline"
    if [[ -z "$src" ]]; then
        for whl in "$offline"/harness_manager-*.whl; do [[ -f "$whl" ]] && src="$whl"; done
    fi
fi
if [[ -z "$src" ]]; then
    src="$checkout"
fi
constraints_dir=""
if is_git_url "$src"; then
    [[ -z "$offline" ]] || die "--offline cannot clone $src; give a checkout or a wheel"
    command -v git >/dev/null || die "git is needed to install from $src"
    say "cloning  $src${ref:+ ($ref)}"
    git clone --quiet --depth 1 ${ref:+--branch "$ref"} "$src" "$work/src" \
        || die "could not clone $src (for the private repo, check your GitHub SSH key)"
    pkg="$work/src"
    add_links "$pkg/vendor"
    constraints_dir="$pkg"
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
    constraints_dir="$pkg"
elif [[ -f "$src" && "$src" == *.whl ]]; then
    pkg="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"
    add_links "$(dirname "$pkg")"
    add_links "$checkout/vendor"
    # A release directory (make dist) carries its constraints.txt next to the wheel.
    constraints_dir="$(dirname "$pkg")"
    [[ -f "$constraints_dir/constraints.txt" ]] || constraints_dir="$checkout"
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

# The tested dependency versions (constraints.txt, made by scripts/lock_deps.sh).
constraint_args=()
if [[ $latest -eq 0 ]]; then
    for c in "${offline:+$offline/constraints.txt}" "$constraints_dir/constraints.txt"; do
        if [[ -n "$c" && -f "$c" ]]; then constraint_args=(--constraint "$c"); break; fi
    done
fi

spec="$pkg"
if [[ -n "$extras" ]]; then
    spec="${pkg}[$(printf '%s' "$extras" | tr ' ' ',')]"
fi

# -- the venv --------------------------------------------------------------------------
py_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}
py_version() {
    "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo "?"
}

# What to install for a newer Python on this system, from /etc/os-release.
python_hint() {
    local osr="${HARNESS_MANAGER_OS_RELEASE:-/etc/os-release}" id="" like=""
    if [[ "$os" == Darwin ]]; then
        say "  macOS: brew install python@3.12, or the installer from python.org"
    elif [[ -r "$osr" ]]; then
        id="$(sed -n 's/^ID=//p' "$osr" | tr -d '"' | head -n 1)"
        like="$(sed -n 's/^ID_LIKE=//p' "$osr" | tr -d '"' | head -n 1)"
        case " $id $like " in
            *" fedora "*)
                if [[ "$id" == fedora ]]; then say "  Fedora: sudo dnf install python3"
                else say "  RHEL, Rocky, Alma 8 or 9: sudo dnf install python3.12 expat"; fi ;;
            *" rhel "*|*" centos "*) say "  RHEL, Rocky, Alma 8 or 9: sudo dnf install python3.12 expat" ;;
            *" debian "*|*" ubuntu "*)
                say "  Debian 12, Ubuntu 22.04 or newer: sudo apt install python3 python3-venv"
                say "  (older releases have no Python $MIN_PY package: use uv, below)" ;;
            *" suse "*|*" opensuse "*) say "  openSUSE, SLES: sudo zypper install python312" ;;
            *" arch "*) say "  Arch: sudo pacman -S python" ;;
        esac
    fi
    say "  or, with no root: curl -LsSf https://astral.sh/uv/install.sh | sh"
    say "  then open a new terminal and run this installer again (uv downloads a Python)."
}

# Why this Python's venv has no pip: venv hides ensurepip's own error, so make a venv
# without pip and run ensurepip in it. Prints the first error line, or nothing.
venv_cause() {
    rm -rf "$work/probe"
    "$1" -m venv --without-pip "$work/probe" >/dev/null 2>&1 || return 0
    if "$work/probe/bin/python" -m ensurepip --default-pip >"$work/ensurepip.out" 2>&1; then
        return 0
    fi
    { grep -E '^[A-Za-z.]*(Error|Exception): ' "$work/ensurepip.out" \
        | grep -v 'CalledProcessError' | head -n 1 | cut -c 1-300; } || true
}

# What gives this Python its venv and pip on this system, from /etc/os-release.
venv_hint() {  # PYTHON [CAUSE]
    local osr="${HARNESS_MANAGER_OS_RELEASE:-/etc/os-release}" id="" like="" xy
    xy="$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    [[ -n "$xy" ]] || xy=3
    if [[ -r "$osr" ]]; then
        id="$(sed -n 's/^ID=//p' "$osr" | tr -d '"' | head -n 1)"
        like="$(sed -n 's/^ID_LIKE=//p' "$osr" | tr -d '"' | head -n 1)"
    fi
    # RHEL 8's python3.12 needs a newer libexpat than an un-updated system has (pip then
    # fails to import pyexpat: "undefined symbol: XML_SetBillionLaughs..."), and its
    # package does not ask for one.
    case "${2:-}" in
        *pyexpat*|*XML_*)
            case " $id $like " in
                *" debian "*|*" ubuntu "*) printf 'Update expat: sudo apt install --only-upgrade libexpat1' ;;
                *" fedora "*|*" rhel "*|*" centos "*) printf 'Update expat: sudo dnf upgrade expat' ;;
                *) printf "Update the system's expat library (libexpat)" ;;
            esac
            printf ', then run this again.'
            return 0 ;;
    esac
    case " $id $like " in
        *" debian "*|*" ubuntu "*) printf 'Install it: sudo apt install python%s-venv' "$xy" ;;
        *" fedora "*|*" rhel "*|*" centos "*)
            # Fedora's own Python, and a RHEL python3, are python3-pip; RHEL's
            # side-by-side ones are python3.X-pip.
            if [[ "$id" == fedora || "$xy" == 3 || "$(basename "$1")" == python3 ]]; then
                printf 'Install it: sudo dnf install python3-pip'
            else printf 'Install it: sudo dnf install python%s-pip' "$xy"; fi ;;
        *" suse "*|*" opensuse "*) printf 'Install it: sudo zypper install python%s-pip' "${xy/./}" ;;
        *) printf 'On Debian or Ubuntu: sudo apt install python%s-venv. On RHEL, Rocky, Alma or Fedora: sudo dnf install python%s-pip' "$xy" "$xy" ;;
    esac
    printf ', then run this again.'
}

if [[ $use_uv -eq 1 ]] && ! command -v uv >/dev/null 2>&1; then use_uv=0; fi
uv_flags=()
if [[ -n "$offline" ]]; then uv_flags=(--offline); fi

if [[ $latest -eq 1 && -d "$venv" ]]; then
    # An upgrade in place keeps each dependency that still satisfies: rebuild instead.
    stop_daemons
    say "latest   rebuilding $venv with the newest dependency versions"
    rm -rf "$venv"
fi
if [[ -x "$venv/bin/python" ]] && py_ok "$venv/bin/python"; then
    stop_daemons
    say "upgrade  $venv"
else
    rm -rf "$venv"
    # The newest local Python >= 3.10 (or --python).
    base="" too_old=""
    if [[ -n "$python" ]]; then
        command -v "$python" >/dev/null 2>&1 || die "--python $python: no such command"
        py_ok "$python" || die "$python is Python $(py_version "$python"); Harness Manager \
needs $MIN_PY or newer"
        base="$python"
    else
        for cand in $PY_CANDIDATES; do
            command -v "$cand" >/dev/null 2>&1 || continue
            if py_ok "$cand"; then base="$cand"; break; fi
            too_old="$too_old $cand ($(py_version "$cand")),"
        done
    fi
    if [[ $use_uv -eq 1 ]]; then
        # With no local Python >= 3.10, uv downloads one.
        uv venv --quiet ${uv_flags[@]+"${uv_flags[@]}"} --python "${base:-3.12}" "$venv" \
            || die "uv could not make a venv with Python >= $MIN_PY"
    else
        if [[ -z "$base" ]]; then
            {
                if [[ -n "$too_old" ]]; then
                    say "install.sh: error: Harness Manager needs Python $MIN_PY or newer. Found only${too_old%,}."
                else
                    say "install.sh: error: Harness Manager needs Python $MIN_PY or newer, and none was found."
                fi
                say "Install one, then run this again:"
                python_hint
                say "If you have one elsewhere, name it: $0 --python /path/to/python3.12"
            } >&2
            exit 1
        fi
        "$base" -m venv "$venv" 2>"$work/venv.err" || {
            cat "$work/venv.err" >&2
            rm -rf "$venv"
            cause="$(venv_cause "$base" || true)"
            if [[ -n "$cause" ]]; then note "the cause: $cause"; fi
            die "$base could not make a venv with pip in it. $(venv_hint "$base" "$cause")"
        }
    fi
    say "venv     $venv ($(py_version "$venv/bin/python"))"
fi

# -- install ---------------------------------------------------------------------------
shown="$what"
if [[ -n "$extras" ]]; then shown="$what [$extras]"; fi
say "install  $shown"
if [[ ${#constraint_args[@]} -gt 0 ]]; then
    say "pins     the tested dependency versions (constraints.txt; --latest for the newest)"
fi
index_args=()
if [[ -n "$offline" ]]; then index_args=(--no-index); fi

install_failed() {
    {
        say "install.sh: error: the install did not finish (the tool's message is above)."
        if [[ -n "$offline" ]]; then
            say "  --offline: $offline lacks a wheel this needs. Make the wheelhouse with the"
            say "  same Python version as this machine: scripts/make_wheelhouse.sh --python $(py_version "$venv/bin/python")"
        else
            say "  If it could not reach the package index (PyPI):"
            say "  - check the network; behind a proxy: export HTTPS_PROXY=http://PROXY:PORT"
            say "  - with no network: build a wheelhouse elsewhere, then --offline DIR (docs/INSTALL.md)"
            if [[ ${#constraint_args[@]} -gt 0 ]]; then
                say "  If a pinned version has no wheel for this Python, try --latest."
            fi
        fi
        say "  Running this again is safe: it resumes."
    } >&2
    exit 1
}

if [[ $use_uv -eq 1 ]]; then
    uvpip() {
        uv pip install --quiet ${uv_flags[@]+"${uv_flags[@]}"} --python "$venv/bin/python" "$@"
    }
    # pyverify keeps its version number across commits: always reinstall the vendored one.
    uvpip --reinstall-package mps3-pyverify --no-deps "$pyverify_wheel" || install_failed
    # --upgrade-package, not --upgrade: an upgrade of everything could swap the vendored
    # pyverify for a same-named package from the index.
    uvpip ${index_args[@]+"${index_args[@]}"} --upgrade-package harness-manager \
        --reinstall-package harness-manager ${find_links[@]+"${find_links[@]}"} \
        ${constraint_args[@]+"${constraint_args[@]}"} "$spec" || install_failed
else
    "$venv/bin/python" -m pip --version >/dev/null 2>&1 \
        || "$venv/bin/python" -m ensurepip --upgrade >/dev/null
    pip() { "$venv/bin/python" -m pip --disable-pip-version-check "$@"; }
    if [[ -z "$offline" ]]; then
        pip install --quiet --upgrade pip 2>/dev/null \
            || note "could not upgrade pip; carrying on with $(pip --version)"
    fi
    pip install --quiet --force-reinstall --no-deps "$pyverify_wheel" || install_failed
    # pip's default upgrade strategy (only-if-needed) keeps the pyverify just installed.
    pip install --quiet --upgrade ${index_args[@]+"${index_args[@]}"} ${find_links[@]+"${find_links[@]}"} \
        ${constraint_args[@]+"${constraint_args[@]}"} "$spec" || install_failed
    if [[ "$pkg" == *.whl ]]; then
        # pip leaves a wheel of the same version alone; the file may still be newer.
        pip install --quiet --force-reinstall --no-deps "$pkg" || install_failed
    fi
fi
version="$("$venv/bin/harness-manager" version)" || die "the installed harness-manager does not run"
printf '# Written by scripts/install.sh: the choices a re-run keeps.\nextras=%s\ndesktop=%s\npath=%s\n' \
    "$extras" "$desktop" "$editpath" >"$record"

# -- uv in the venv: the app's self-update builds each new version with it ------------------
if [[ ! -x "$venv/bin/uv" ]]; then
    if [[ $use_uv -eq 1 ]]; then
        uvpip ${index_args[@]+"${index_args[@]}"} ${find_links[@]+"${find_links[@]}"} "uv>=0.4" \
            >/dev/null 2>&1 || true
    else
        pip install --quiet ${index_args[@]+"${index_args[@]}"} ${find_links[@]+"${find_links[@]}"} \
            "uv>=0.4" >/dev/null 2>&1 || true
    fi
fi
venv_uv=""
if [[ -x "$venv/bin/uv" ]]; then
    venv_uv="$venv/bin/uv"
else
    note "no uv in the venv${offline:+ (the wheelhouse has no uv wheel)}, so the app cannot"
    note "update itself until you run this again with network. Everything else works."
fi

# -- the install record and the self-update pointer ---------------------------------------
# install.json says what was installed, and where. The pointer learns this venv, so that
# rollback can return to it. A re-run wins over an older self-updated version. An older
# install's self-updated versions move into the install root.
if "$venv/bin/python" -c 'import harness_manager._launch' >/dev/null 2>&1; then
    "$venv/bin/python" -m harness_manager._launch --installer-hook --root "$home_dir" \
        --venv "$venv" --version "$version" --extras "$extras" --uv "$venv_uv" \
        --legacy "$legacy_app" --installer install.sh \
        || note "could not record the install in $install_json; the command runs $version"
else
    rm -f "$install_json"
    note "harness-manager $version has no self-update launcher: the command runs it directly"
fi

# -- the command on PATH ---------------------------------------------------------------
if [[ -e "$launcher" || -L "$launcher" ]] && ! ours "$launcher"; then
    if [[ $force -eq 1 ]]; then
        note "replacing $launcher (--force)"
    else
        die "$launcher exists and this script did not write it. Move it away, or re-run \
with --force. Until then, run $venv/bin/harness-manager"
    fi
fi
cat >"$launcher.tmp" <<EOF
#!/bin/sh
# $MARKER.
# Re-run the installer to upgrade; \`install.sh --uninstall\` removes it.
# harness-manager-launch runs the version the app's self-update selected
# (<install root>/current.json), else the installed venv. HARNESS_MANAGER_USE_INSTALLED=1
# runs the installed venv whatever the pointer says.
venv=$(printf '%q' "$venv")
if [ -x "\$venv/bin/harness-manager-launch" ]; then
    exec "\$venv/bin/harness-manager-launch" "\$@"
fi
exec "\$venv/bin/harness-manager" "\$@"
EOF
chmod 755 "$launcher.tmp"
mv -f "$launcher.tmp" "$launcher"
say "command  $launcher"

# -- the application menu (Linux desktops) ----------------------------------------------
# A .desktop file in ~/.local/share/applications (the XDG menu). It is written on a
# server with no display too: a remote desktop (ThinLinc, VNC) shows it later.
desktop_entry() {
    local template="$checkout/packaging/linux/harness-manager.desktop"
    local icon="$checkout/packaging/linux/harness-manager.svg"
    case "$launcher$icon_file" in
        *[\"\`\$\\%]*|*$'\n'*)
            note "no menu entry: the command's path has characters a .desktop file cannot quote"
            return 0 ;;
    esac
    if [[ ! -f "$template" || ! -f "$icon" ]]; then
        note "no menu entry: $checkout/packaging/linux is missing"
        return 0
    fi
    if [[ -f "$desktop_file" ]] && ! grep -q "$DESKTOP_MARKER" "$desktop_file" 2>/dev/null; then
        note "left $desktop_file alone: this script did not write it"
        return 0
    fi
    if ! mkdir -p "$apps_dir" "$(dirname "$icon_file")" 2>/dev/null; then
        note "no menu entry: cannot write to $apps_dir"
        return 0
    fi
    cp "$icon" "$icon_file"
    local text
    text="$(cat "$template")"
    text="${text//@LAUNCHER@/"$launcher"}"
    text="${text//@ICON@/"$icon_file"}"
    printf '%s\n' "$text" >"$desktop_file.tmp"
    mv -f "$desktop_file.tmp" "$desktop_file"
    say "menu     $desktop_file"
}
if [[ "$os" == Linux ]]; then
    if [[ "$desktop" == 1 ]]; then desktop_entry; else remove_desktop_entry; fi
fi

# -- what next ---------------------------------------------------------------------------
# How to put the command's directory on PATH, for the user's shell. A login shell (ssh,
# `bash -l`) reads only the first of ~/.bash_profile, ~/.bash_login, ~/.profile, and not
# ~/.bashrc, unless that file sources it (a clean RHEL account got "command not found").
# tcsh reads ~/.tcshrc, else ~/.cshrc (csh: ~/.cshrc), every shell, and neither reads
# ~/.profile. docs/INSTALL.md "When harness-manager is not on PATH" says the same.
path_advice() {  # SHELL-NAME SHOWN-DIR BIN-DIR OS HOME
    local sh="$1" dir="$2" abs="$3" os_="$4" home="$5" f login=.bash_profile rc=.cshrc
    case "$sh" in
        fish) say "Run this once:  fish_add_path $abs" ;;
        zsh) say "Add this line to ~/.zshrc (new terminals) and to ~/.zprofile (login shells"
             say "and ssh), then open a new terminal:"
             say "    export PATH=\"$dir:\$PATH\"" ;;
        bash)
            for f in .bash_profile .bash_login .profile; do
                if [[ -f "$home/$f" ]]; then login="$f"; break; fi
            done
            if [[ "$os_" == Darwin ]]; then
                say "Add this line to ~/$login (Terminal and ssh start login shells, which"
                say "read it), then open a new terminal:"
            else
                say "Add this line to ~/.bashrc (new terminals) and to ~/$login (login shells"
                say "and ssh read ~/$login, not ~/.bashrc), then open a new terminal:"
            fi
            say "    export PATH=\"$dir:\$PATH\"" ;;
        tcsh|csh)
            if [[ "$sh" == tcsh && -f "$home/.tcshrc" ]]; then rc=.tcshrc; fi
            say "Add this line to ~/$rc, then open a new terminal:"
            say "    set path = ( $dir \$path )" ;;
        *) say "Add this line to ~/.profile, then log in again:"
           say "    export PATH=\"$dir:\$PATH\"" ;;
    esac
}

say ""
say "Harness Manager $version is installed."
hm="harness-manager"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        hm="$launcher"
        shown_dir="$bin_dir"
        case "$bin_dir" in "$HOME"/*) shown_dir="\$HOME/${bin_dir#"$HOME"/}" ;; esac
        say ""
        say "$bin_dir is not on your PATH yet."
        sh_name="$(basename "${SHELL:-sh}")"
        pfiles="$(path_files "$sh_name" "$os" "$HOME")"
        pline="export PATH=\"$shown_dir:\$PATH\""
        case "$bin_dir" in *[\"\`\\]*|*$'\n'*) pfiles="" ;; esac   # not quotable in a shell file
        if [[ "$editpath" == 1 && -n "$pfiles" ]]; then
            say "Adding it, in one marked block, to your shell startup files (--no-path skips this):"
            added=0
            while IFS= read -r pf; do
                if add_path_block "$HOME/$pf" "$pline"; then added=1; fi
            done <<<"$pfiles"
            if [[ $added -eq 1 ]]; then
                say "    $pline"
                say "New terminals have it; this one does not. Open a new terminal, or run that line."
            else
                path_advice "$sh_name" "$shown_dir" "$bin_dir" "$os" "$HOME"
            fi
        else
            if [[ "$editpath" != 1 ]]; then say "(--no-path: your shell files are left alone.)"; fi
            path_advice "$sh_name" "$shown_dir" "$bin_dir" "$os" "$HOME"
        fi
        say "Until then, run it by its full path, as below."
        ;;
esac
if [[ "$os" == Linux && " $extras " == *" app "* ]] \
    && ! "$venv/bin/python" -c 'import gi' >/dev/null 2>&1 \
    && ! "$venv/bin/python" -c 'import qtpy' >/dev/null 2>&1; then
    say ""
    say "--with-app: pywebview on Linux also needs GTK or Qt bindings, which a venv does not"
    say "have, so the app opens a Chrome or Chromium app window instead (the usual way on Linux)."
fi
if [[ "$os" == Linux ]]; then
    browser="$("$venv/bin/python" -c 'from harness_manager.web.window import find_app_browser as f; print(f() or "")' 2>/dev/null || true)"
    if [[ -z "$browser" ]]; then
        say ""
        say "No Chrome, Chromium, Edge or Brave found: \`harness-manager app\` will open a"
        say "browser tab instead of its own window. Install one of them for the app window."
    fi
fi
say ""
say "Next:"
say "  $hm app --demo      the app with demo boards, no hardware needed"
if [[ "$os" == Linux && -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    say "                      (no display here: it prints the URL and the ssh -L command)"
fi
say "  $hm info 192.168.10.101    a real board on your network"
if [[ "$os" == Darwin ]]; then
    say ""
    say "Dock: run \`$hm app\`, right-click the app's Dock icon, Options, Keep in Dock."
    say "(There is no .app bundle.)"
fi
say "Guide: $checkout/docs/USER_GUIDE.md"
