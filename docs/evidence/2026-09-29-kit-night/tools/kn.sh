#!/bin/bash
# kn: run one command in a fresh login shell of a throwaway account (like the runbook's `cr`),
# with PRIVATE Harness Manager state and the hm-kit-night worktree venv first on PATH. Logs it.
KN=/tmpdir/claude-74755/kit-night
WT=/home/dam1n19/SoCLabs/hm-kit-night
printf '\n=== %s $ %s\n' "$(date +%T)" "$*" >> "$KN/kitnight.log"
t0=$(date +%s)
env -i HOME="$KN/home" USER="$USER" LOGNAME="$LOGNAME" TERM=dumb SHELL=/bin/bash \
  bash -lc "export HARNESS_MANAGER_STATE_DIR=$KN/home/.config/harness-manager HARNESS_MANAGER_PTY_DIR=$KN/pty \
XDG_CONFIG_HOME=$KN/xdg/config XDG_DATA_HOME=$KN/xdg/data XDG_CACHE_HOME=$KN/xdg/cache TMPDIR=$KN/tmp; \
export PATH=$WT/.venv/bin:\$PATH; cd ~ && $*" 2>&1 | tee -a "$KN/kitnight.log"
rc=${PIPESTATUS[0]}
printf '=== rc=%s took=%ss\n' "$rc" "$(( $(date +%s) - t0 ))" | tee -a "$KN/kitnight.log"
exit $rc
