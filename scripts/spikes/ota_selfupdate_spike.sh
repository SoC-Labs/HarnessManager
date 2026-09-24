#!/usr/bin/env bash
# Spike (lane OTA): a real end-to-end app self-update on this machine, in isolation.
#
#   scripts/spikes/ota_selfupdate_spike.sh [OTA_DIR]      (default /tmp/ota-spike)
#
# What it proves (docs/design/HM_SELF_UPDATE.md, "Spike"):
#   1. three wheels (0.1.0, 0.1.1, 0.1.2 with a daemon that cannot start), all pinning a
#      THROWAWAY minisign key, and a signed channel served from 127.0.0.1;
#   2. the REAL installer (scripts/install.sh) installs 0.1.0 into $OTA, and its daemon runs;
#   3. a release without a hashed lock cannot be staged (T7's "no lock: a warning" is false);
#   4. with a lock, 0.1.1 stages beside the running version, and a held board lock
#      refuses the switch (HELD, exit 4);
#   5. a running daemon job refuses POST /update/app (409 HELD); after it, the job switches;
#   6. the daemon keeps running 0.1.0 until restarted; the launcher then starts 0.1.1;
#   7. 0.1.2 is staged + switched, its daemon fails, and the restart prototype
#      (ota_restart.py) rolls the pointer back and restarts 0.1.1 on the same port;
#   8. `update rollback --app` and a cleared pointer both return to 0.1.0.
#
# Lane OTA-L re-ran it on its layout: the pointer, the versions and install.json live in the
# install root ($HARNESS_MANAGER_HOME), the installer puts uv in its venv, and rollback right
# after the first switch returns to the installer's 0.1.0 (it was exit 15). Its run:
# docs/design/evidence/ota_l_spike_2026-09-24.log (ports 29415-29417, outside the ephemeral
# range, so a parallel test suite cannot take them).
#
# Needs: python3.11 (for the tools venv), network to PyPI (uv, the dependencies).
# Isolation: HOME, XDG dirs, the state dir, the PTY dir and every port are the spike's own
# (scripts/spikes/ota_env.sh). It stops what it started; `rm -rf $OTA` removes the rest.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W="$(cd "$here/../.." && pwd)"
export OTA="${1:-/tmp/ota-spike}"
[[ "$OTA" == /tmp/ota-* || "$OTA" == /tmp/otal-* ]] || { echo "OTA_DIR must be /tmp/ota-* or /tmp/otal-*" >&2; exit 2; }
# shellcheck source=ota_env.sh
. "$here/ota_env.sh"
mkdir -p "$OTA/logs" "$OTA/www"
N="nice -n 10"
step() { printf '\n=== %s\n' "$*"; }
pids=()
cleanup() {
    "$HARNESS_MANAGER_BIN_DIR/harness-manager" daemon stop >/dev/null 2>&1 || true
    for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT
ptr() { "$OTA/tools/bin/python" -c "import json;d=json.load(open('$HARNESS_MANAGER_HOME/current.json'));print('pointer   current', d['current'] or '(installer venv)', ' previous', d['previous'] or '(installer venv)')"; }
hl() { curl -s "http://127.0.0.1:$OTA_DAEMON_PORT/health"; echo; }

step "tools: uv + cryptography in a throwaway venv"
if [[ ! -x "$OTA/tools/bin/uv" ]]; then
    $N python3.11 -m venv "$OTA/tools"
    $N "$OTA/tools/bin/pip" install -q --disable-pip-version-check "uv==0.8.0" cryptography
fi
"$OTA/tools/bin/uv" --version
# the release tool uses the spike's own uv; the installed Harness Manager uses its venv's (M5)
REL="$N env PYTHONPATH=$W/src HARNESS_MANAGER_UV=$OTA_UV $OTA/tools/bin/python $here/ota_release.py"

step "release: a throwaway key, three pinned source copies, three wheels, one hashed lock"
[[ -f "$OTA/keys/release.seed" ]] || $REL keygen "$OTA/keys"
for v in 1 2 3; do
    rm -rf "$OTA/src-v$v" "$OTA/dist-v$v"; mkdir -p "$OTA/src-v$v"
    git -C "$W" archive HEAD | tar -C "$OTA/src-v$v" -xf -
    $REL pin "$OTA/src-v$v" "$OTA/keys/release.pub" >/dev/null
done
$REL bump "$OTA/src-v2" 0.1.1 >/dev/null
$REL bump "$OTA/src-v3" 0.1.2 >/dev/null
sed -i 's/^def main(argv: list\[str\] | None = None) -> int:$/&\n    raise SystemExit("OTA SPIKE: v0.1.2 daemon is deliberately broken")/' \
    "$OTA/src-v3/src/harness_manager/daemon/server.py"
for v in 1 2 3; do
    $N "$OTA/tools/bin/uv" build --quiet --wheel --out-dir "$OTA/dist-v$v" "$OTA/src-v$v"
    cp "$OTA/src-v$v/constraints.txt" "$OTA/dist-v$v/"
done
$REL lock "$OTA/src-v2" "$OTA/lock.txt" "http://127.0.0.1:$OTA_CHANNEL_PORT/assets"
wh1="$OTA/dist-v1/harness_manager-0.1.0-py3-none-any.whl"
wh2="$OTA/dist-v2/harness_manager-0.1.1-py3-none-any.whl"
wh3="$OTA/dist-v3/harness_manager-0.1.2-py3-none-any.whl"
pyv="$W/vendor/mps3_pyverify-0.1.0-py3-none-any.whl"

step "serve the channel on 127.0.0.1:$OTA_CHANNEL_PORT (serial 1: 0.1.1 WITHOUT a lock)"
$REL publish "$OTA/www" stable 1 "$OTA/keys" --app 0.1.1 "$wh2" - --app 0.1.0 "$wh1" -
$N "$OTA/tools/bin/python" -m http.server --bind 127.0.0.1 --directory "$OTA/www" \
    "$OTA_CHANNEL_PORT" >"$OTA/logs/channel_http.log" 2>&1 & pids+=($!)
$N "$OTA/tools/bin/python" "$here/ota_slow_http.py" "$OTA_SLOW_PORT" 25 \
    >"$OTA/logs/slow_http.log" 2>&1 & pids+=($!)
sleep 2

step "install 0.1.0 with the real installer (no uv on PATH here: it uses venv + pip)"
$N bash "$W/scripts/install.sh" --from "$wh1" --with-serial 2>&1 | grep -E "^(venv|install|command|menu|Harness|install.sh: no uv)"
cd "$OTA"
echo "uv in the venv (M5): $(ls "$HARNESS_MANAGER_HOME/venv/bin/uv" 2>&1)"
"$OTA/tools/bin/python" -c "import json;d=json.load(open('$HARNESS_MANAGER_HOME/install.json'));print('install.json', {k: d[k] for k in ('version', 'extras', 'uv')})"
ptr
$N harness-manager daemon start --port "$OTA_DAEMON_PORT" | head -1
hl
$N harness-manager update check | grep -E "^(channel|app)"

step "hold a board lock from a live process (a stand-in for a running CLI deploy)"
"$HARNESS_MANAGER_HOME/venv/bin/python" "$here/ota_hold_lock.py" ota-board-1 600 \
    >"$OTA/logs/holdlock.log" 2>&1 & hold=$!; pids+=("$hold")
sleep 3; cat "$OTA/logs/holdlock.log"

step "update app, serial 1 (no lock file): expect a failed stage"
set +e; $N harness-manager update app --yes 2>&1 | tail -1; echo "rc=${PIPESTATUS[0]}"; set -e

step "serial 2 adds the hashed lock: stage works, the held lock refuses the switch"
$REL publish "$OTA/www" stable 2 "$OTA/keys" --asset "$pyv" --app 0.1.1 "$wh2" "$OTA/lock.txt" \
    --app 0.1.0 "$wh1" "$OTA/lock.txt" >/dev/null
set +e; $N harness-manager update app --yes 2>&1 | tail -1; echo "rc=${PIPESTATUS[0]}"; set -e
ptr
"$OTA_UV" pip list --python "$HARNESS_MANAGER_HOME/versions/0.1.1/bin/python" \
    2>/dev/null | grep -E "^(harness-manager|mps3-pyverify|pyserial|pywebview) " || true
kill "$hold"; sleep 1

step "the daemon path: a 25 s job runs; POST /update/app is refused; after it, it switches"
TOK="$("$OTA/tools/bin/python" -c "import json;print(json.load(open('$HARNESS_MANAGER_STATE_DIR/daemon.json'))['token'])")"
API="http://127.0.0.1:$OTA_DAEMON_PORT/api/v1"; H="Authorization: Bearer $TOK"; CT='Content-Type: application/json'
curl -s -X POST -H "$H" -H "$CT" -d "{\"source\":\"http://127.0.0.1:$OTA_SLOW_PORT/{channel}/channel.json\"}" "$API/update/check"; echo
sleep 2
curl -s -w ' HTTP %{http_code}\n' -X POST -H "$H" -H "$CT" -d '{}' "$API/update/app" | cut -c 1-160
sleep 25
J="$(curl -s -X POST -H "$H" -H "$CT" -d '{}' "$API/update/app" | "$OTA/tools/bin/python" -c "import json,sys;print(json.load(sys.stdin)['job'])")"
for _ in $(seq 60); do
    R="$(curl -s -H "$H" "$API/jobs/$J")"
    case "$R" in *'"state":"running"'*|*'"state":"queued"'*) sleep 1 ;; *) break ;; esac
done
echo "$R" | "$OTA/tools/bin/python" -c "import json,sys;d=json.load(sys.stdin);print('job', d['state'], 'switched', (d.get('result') or {}).get('switched'))"
ptr
echo -n "daemon still runs: "; hl
echo -n "the command now runs: "; harness-manager version

step "restart by hand through the launcher"
$N harness-manager daemon stop | head -1
$N harness-manager daemon start --port "$OTA_DAEMON_PORT" | head -1
hl
set +e; $N harness-manager update rollback --app --yes 2>&1 | tail -1; echo "rollback right after the first switch: rc=${PIPESTATUS[0]}"; set -e
ptr; echo "the command now runs: $(harness-manager version)"
set +e; $N harness-manager update rollback --app --yes 2>&1 | tail -1; echo "and forward again: rc=${PIPESTATUS[0]}"; set -e
ptr; echo "the command now runs: $(harness-manager version)"

step "serial 3: 0.1.2 (its daemon cannot start); switch; the restart prototype rolls back"
$REL publish "$OTA/www" stable 3 "$OTA/keys" --app 0.1.2 "$wh3" "$OTA/lock.txt" \
    --app 0.1.1 "$wh2" "$OTA/lock.txt" --app 0.1.0 "$wh1" "$OTA/lock.txt" >/dev/null
$N harness-manager update app --yes | tail -1
ptr
RESTART="$N $HARNESS_MANAGER_HOME/venv/bin/python $here/ota_restart.py $HARNESS_MANAGER_BIN_DIR/harness-manager $HARNESS_MANAGER_STATE_DIR 15"
set +e; $RESTART | cut -c 1-200; set -e
ptr; hl
$N harness-manager update check | grep "^app"

step "back to 0.1.0: from signed history, then rollback, then a cleared pointer"
$N harness-manager update app --version 0.1.0 --yes | tail -1
$RESTART | tail -1; hl
$N harness-manager update rollback --app --yes | tail -1
$RESTART | tail -1; hl
"$OTA/tools/bin/python" - <<EOF
import json; p = "$HARNESS_MANAGER_HOME/current.json"
d = json.load(open(p)); d["previous"], d["current"] = d["current"], ""; json.dump(d, open(p, "w"))
EOF
ptr; $RESTART | tail -1; hl

step "done: stopping the spike's daemon and servers"
