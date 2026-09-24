#!/usr/bin/env bash
# Spike re-run (lane OTA-D): the OTA spike's five breaks, against the daemon's apply step.
#
#   scripts/spikes/ota_apply_spike.sh [OTA_DIR]      (default /tmp/ota-dspike)
#
# The same isolation, throwaway key, real wheels, real lock, real installer and real uv as
# ota_selfupdate_spike.sh (ota_env.sh, ota_release.py), built from `git archive HEAD`. Then,
# instead of the restart prototype (ota_restart.py), the product path:
# `harness-manager update app --apply` through the running daemon (stage in the daemon, drain,
# resume file, detached helper, same port and token, /health, automatic rollback).
#
# The five breaks of docs/design/HM_SELF_UPDATE.md §1/§3, each checked and reported:
#   1. no hashed lock: a release cannot be staged                       (OTA-R/OTA-C)
#   2. nothing restarts the daemon after a switch                        (OTA-D)
#   3. rollback cannot return to the installer's version                 (OTA-L)
#   4. a version that failed is offered again                            (OTA-D + OTA-C)
#   5. the token changes on a restart: an open app window gets 401      (OTA-D)
#
# 0.1.2's daemon exits at start (its --self-test passes), so the helper's rollback runs.
# Needs: python3.11, network to PyPI (uv, the dependencies). Ports 29425-29427 (outside the
# ephemeral range). It stops what it started; `rm -rf $OTA` removes the rest.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W="$(cd "$here/../.." && pwd)"
export OTA="${1:-/tmp/ota-dspike}"
[[ "$OTA" == /tmp/ota-* ]] || { echo "OTA_DIR must be /tmp/ota-*" >&2; exit 2; }
export OTA_CHANNEL_PORT=29425 OTA_SLOW_PORT=29426 OTA_DAEMON_PORT=29427
# shellcheck source=ota_env.sh
. "$here/ota_env.sh"
mkdir -p "$OTA/logs" "$OTA/www"
N="nice -n 10"
step() { printf '\n=== %s\n' "$*"; }
declare -A BREAK
pids=()
cleanup() {
    "$HARNESS_MANAGER_BIN_DIR/harness-manager" daemon stop --force >/dev/null 2>&1 || true
    for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT
PY="$OTA/tools/bin/python"
ptr() { "$PY" -c "import json;d=json.load(open('$HARNESS_MANAGER_HOME/current.json'));print('pointer   current', d['current'] or '(installer venv)', ' previous', d['previous'] or '(installer venv)')"; }
hl() { curl -s "http://127.0.0.1:$OTA_DAEMON_PORT/health"; echo; }
code_with() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" "http://127.0.0.1:$OTA_DAEMON_PORT/api/v1/boards"; }

step "tools: uv + cryptography in a throwaway venv"
if [[ ! -x "$OTA/tools/bin/uv" ]]; then
    $N python3.11 -m venv "$OTA/tools"
    $N "$OTA/tools/bin/pip" install -q --disable-pip-version-check "uv==0.8.0" cryptography
fi
"$OTA/tools/bin/uv" --version
REL="$N env PYTHONPATH=$W/src HARNESS_MANAGER_UV=$OTA_UV $PY $here/ota_release.py"

step "release: a throwaway key, three pinned copies of HEAD, three wheels, one hashed lock"
[[ -f "$OTA/keys/release.seed" ]] || $REL keygen "$OTA/keys"
for v in 1 2 3; do
    rm -rf "$OTA/src-v$v" "$OTA/dist-v$v"; mkdir -p "$OTA/src-v$v"
    git -C "$W" archive HEAD | tar -C "$OTA/src-v$v" -xf -
    $REL pin "$OTA/src-v$v" "$OTA/keys/release.pub" >/dev/null
done
$REL bump "$OTA/src-v2" 0.1.1 >/dev/null
$REL bump "$OTA/src-v3" 0.1.2 >/dev/null
# 0.1.2's daemon exits at start; its --self-test still passes, so the apply restarts onto it
"$PY" - "$OTA/src-v3/src/harness_manager/daemon/server.py" <<'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
anchor = "    state_dir = Path(state_dir)\n    if resume is not None:\n"
assert anchor in s
open(p, "w").write(s.replace(anchor, "    raise SystemExit('OTA SPIKE: v0.1.2 daemon is deliberately broken')\n" + anchor, 1))
EOF
for v in 1 2 3; do
    $N "$OTA/tools/bin/uv" build --quiet --wheel --out-dir "$OTA/dist-v$v" "$OTA/src-v$v"
done
$REL lock "$OTA/src-v2" "$OTA/lock.txt" "http://127.0.0.1:$OTA_CHANNEL_PORT/assets"
wh1="$OTA/dist-v1/harness_manager-0.1.0-py3-none-any.whl"
wh2="$OTA/dist-v2/harness_manager-0.1.1-py3-none-any.whl"
wh3="$OTA/dist-v3/harness_manager-0.1.2-py3-none-any.whl"
pyvs=("$W"/vendor/mps3_pyverify-*.whl); pyv="${pyvs[0]}"

step "serve the channel on 127.0.0.1:$OTA_CHANNEL_PORT (serial 1: 0.1.1 WITHOUT a lock)"
$REL publish "$OTA/www" stable 1 "$OTA/keys" --app 0.1.1 "$wh2" - --app 0.1.0 "$wh1" -
$N "$PY" -m http.server --bind 127.0.0.1 --directory "$OTA/www" "$OTA_CHANNEL_PORT" \
    >"$OTA/logs/channel_http.log" 2>&1 & pids+=($!)
sleep 2

step "install 0.1.0 with the real installer; start its daemon on port $OTA_DAEMON_PORT"
$N bash "$W/scripts/install.sh" --from "$wh1" --with-serial 2>&1 | grep -E "^(venv|install|command|Harness)"
cd "$OTA"
ptr
$N harness-manager daemon start --port "$OTA_DAEMON_PORT" | head -1
hl
T0="$("$PY" -c "import json;print(json.load(open('$HARNESS_MANAGER_STATE_DIR/daemon.json'))['token'])")"

step "BREAK 1: a release without a hashed lock cannot be staged"
set +e; out="$($N harness-manager update app --stage-only --yes 2>&1)"; rc=$?; set -e
echo "$out" | tail -1 | cut -c 1-240; echo "rc=$rc"
if [[ $rc -ne 0 ]]; then BREAK[1]="REMAINS (rc=$rc: a release still needs a hashed lock; OTA-R's make release ships one)"; else BREAK[1]="fixed"; fi

step "serial 2 adds the hashed lock"
$REL publish "$OTA/www" stable 2 "$OTA/keys" --asset "$pyv" --app 0.1.1 "$wh2" "$OTA/lock.txt" \
    --app 0.1.0 "$wh1" "$OTA/lock.txt" >/dev/null

step "BREAK 2 + 5: update app --apply restarts the running daemon, same port, same token"
set +e; $N harness-manager update app --apply --yes 2>&1 | tail -4; rc=${PIPESTATUS[0]}; set -e
echo "rc=$rc"
ptr; echo -n "health: "; hl
v="$(curl -s "http://127.0.0.1:$OTA_DAEMON_PORT/health" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('version'))")"
c="$(code_with "$T0")"; echo "the OLD token on /api/v1/boards: HTTP $c"
if [[ $rc -eq 0 && "$v" == "0.1.1" ]]; then BREAK[2]="fixed (the daemon restarted onto 0.1.1 by itself, port $OTA_DAEMON_PORT)"; else BREAK[2]="REMAINS (rc=$rc, /health $v)"; fi
if [[ "$c" == "200" ]]; then BREAK[5]="fixed (the old token still answers 200 after the restart)"; else BREAK[5]="REMAINS (HTTP $c)"; fi
"$PY" -c "import json;print('last_apply', json.load(open('$HARNESS_MANAGER_STATE_DIR/update/last_apply.json')))"

step "BREAK 3: rollback right after the first switch reaches the installer's 0.1.0"
set +e; $N harness-manager update rollback --app --yes 2>&1 | tail -1; rc=${PIPESTATUS[0]}; set -e
ptr
if [[ $rc -eq 0 ]]; then BREAK[3]="fixed (rc=0; OTA-L M2)"; else BREAK[3]="REMAINS (rc=$rc)"; fi
set +e; $N harness-manager update rollback --app --yes 2>&1 | tail -1; set -e
ptr

step "BREAK 4: serial 3 offers 0.1.2 (its daemon cannot start); apply; the helper rolls back"
$REL publish "$OTA/www" stable 3 "$OTA/keys" --asset "$pyv" --app 0.1.2 "$wh3" "$OTA/lock.txt" \
    --app 0.1.1 "$wh2" "$OTA/lock.txt" --app 0.1.0 "$wh1" "$OTA/lock.txt" >/dev/null
set +e; $N harness-manager update app --apply --yes 2>&1 | tail -3 | cut -c 1-300; rc=${PIPESTATUS[0]}; set -e
echo "rc=$rc (6: rolled back)"
ptr; echo -n "health: "; hl
echo "the OLD token after the rollback: HTTP $(code_with "$T0")"
"$PY" -c "import json;print('last_apply', json.load(open('$HARNESS_MANAGER_STATE_DIR/update/last_apply.json')))"
"$PY" -c "import json;print('bad', json.load(open('$HARNESS_MANAGER_STATE_DIR/update/bad_versions.json')))"
$N harness-manager update check | grep -E "^(app|warning)" || true
offered="$($N harness-manager update check --json | "$PY" -c "import json,sys;d=json.load(sys.stdin);print(d['app_update'] or '-', (d.get('app_skipped') or {}).get('version','-'))")"
echo "check: app_update / app_skipped = $offered"
if [[ "$offered" == "- 0.1.2" ]]; then BREAK[4]="fixed (0.1.2 rolled back, marked bad, not offered: app_skipped)"; else BREAK[4]="REMAINS ($offered)"; fi
$N harness-manager update status | sed 's/^/status    /'

step "summary: the five breaks"
for i in 1 2 3 4 5; do echo "break $i: ${BREAK[$i]:-not reached}"; done
step "done: stopping the spike's daemon and server"
