#!/usr/bin/env bash
# Pin Harness Manager's dependencies: write constraints.txt, the tested versions that
# scripts/install.sh installs (pip and uv --constraint). One file for every OS and
# every Python from 3.10 on: the pins that differ carry environment markers.
#
#   scripts/lock_deps.sh             keep the current pins, add what is new
#   scripts/lock_deps.sh --upgrade   move every pin to the newest release
#
# Needs uv (https://docs.astral.sh/uv/). pyverify is left out: it is installed from
# vendor/ by path. The extras a user can ask for (serial, app, ina260) are pinned too;
# dev and webtest are not (CI tests against the newest).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null 2>&1 || {
    echo "lock_deps: needs uv (https://docs.astral.sh/uv/)" >&2
    exit 2
}
upgrade=()
case "${1:-}" in
    "") ;;
    --upgrade) upgrade=(--upgrade) ;;
    *) echo "lock_deps: unknown option $1" >&2; exit 2 ;;
esac

cd "$here"
uv pip compile pyproject.toml --quiet --universal --python-version 3.10 \
    --extra serial --extra app --extra ina260 \
    --find-links vendor --no-emit-package mps3-pyverify \
    --custom-compile-command "scripts/lock_deps.sh" \
    ${upgrade[@]+"${upgrade[@]}"} -o constraints.txt
echo "wrote constraints.txt ($(grep -c '==' constraints.txt) pins)"
