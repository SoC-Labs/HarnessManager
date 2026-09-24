# shellcheck shell=bash
# Spike (lane OTA): the isolated environment. Source it: `. scripts/spikes/ota_env.sh`.
# Everything lives under $OTA (default /tmp/ota-spike): HOME, XDG dirs, the state dir, the
# PTY dir, the install root and the command. Nothing touches the real user's install.
OTA="${OTA:-/tmp/ota-spike}"
OTA_CHANNEL_PORT="${OTA_CHANNEL_PORT:-47815}"   # the signed channel (python -m http.server)
OTA_SLOW_PORT="${OTA_SLOW_PORT:-47816}"         # a channel that answers after 25 s (a long job)
OTA_DAEMON_PORT="${OTA_DAEMON_PORT:-47817}"     # the spike's harness-manager-daemon
export HOME="$OTA/home"
export XDG_DATA_HOME="$HOME/.local/share" XDG_CONFIG_HOME="$HOME/.config" XDG_CACHE_HOME="$HOME/.cache"
export HARNESS_MANAGER_HOME="$XDG_DATA_HOME/harness-manager"
export HARNESS_MANAGER_BIN_DIR="$HOME/.local/bin"
export HARNESS_MANAGER_STATE_DIR="$HOME/.config/harness-manager"
export HARNESS_MANAGER_PTY_DIR="$OTA/pty"
export HARNESS_MANAGER_UPDATE_SOURCE="http://127.0.0.1:$OTA_CHANNEL_PORT/channel/{channel}/channel.json"
export HARNESS_MANAGER_UPDATE_CHANNEL=stable
# The self-updater needs uv; this machine has none on PATH (the installer fell back to pip).
export HARNESS_MANAGER_UV="$OTA/tools/bin/uv"
export UV_CACHE_DIR="$OTA/uv-cache"
export PATH="$HARNESS_MANAGER_BIN_DIR:/usr/bin:/bin"
unset HARNESS_MANAGER_GITHUB_TOKEN
mkdir -p "$HOME" "$HARNESS_MANAGER_PTY_DIR"
