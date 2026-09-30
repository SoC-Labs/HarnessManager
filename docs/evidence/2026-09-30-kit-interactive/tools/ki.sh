#!/bin/bash
# ki: run one command in a fresh login shell of a throwaway account (KIT-NIGHT's kn.sh /
# KIT-NANOSOC's kns.sh with this lane's paths), with PRIVATE Harness Manager state and the
# hm-kit-interactive worktree venv first on PATH. Logs every command and its rc to $KI/ki.log.
KI=/tmpdir/claude-74755/kit-interactive
WT=/home/dam1n19/SoCLabs/hm-kit-interactive
printf '\n=== %s $ %s\n' "$(date +%T)" "$*" >> "$KI/ki.log"
t0=$(date +%s)
env -i HOME="$KI/home" USER="$USER" LOGNAME="$LOGNAME" TERM=dumb SHELL=/bin/bash \
  bash -lc "export HARNESS_MANAGER_STATE_DIR=$KI/home/.config/harness-manager HARNESS_MANAGER_PTY_DIR=$KI/pty \
XDG_CONFIG_HOME=$KI/xdg/config XDG_DATA_HOME=$KI/xdg/data XDG_CACHE_HOME=$KI/xdg/cache TMPDIR=$KI/tmp; \
export PATH=$WT/.venv/bin:\$PATH; cd ~ && $*" 2>&1 | tee -a "$KI/ki.log"
rc=${PIPESTATUS[0]}
printf '=== rc=%s took=%ss\n' "$rc" "$(( $(date +%s) - t0 ))" | tee -a "$KI/ki.log"
exit $rc
