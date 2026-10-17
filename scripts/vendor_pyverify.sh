#!/usr/bin/env bash
# Build the pyverify wheel (distribution mps3-pyverify) from one commit of the
# platform repo, and put it in vendor/ with its provenance. The same commit's
# MPS3 OpenOCD configs (host/openocd) go to src/harness_manager_mps3/openocd_cfg/ (in the wheel).
#
#   scripts/vendor_pyverify.sh [PLATFORM_REPO [COMMIT]]
#
#   PLATFORM_REPO  default: ../mps3-nanosoc-platform (next to this checkout)
#   COMMIT         default: that repo's HEAD
#
# DEV AND CI FALLBACK ONLY. Harness Manager pins the platform's PUBLISHED wheel (pyverify.pin,
# scripts/pin_pyverify.sh); vendor/ is used only while that wheel's sha256 is the pin's, and goes
# away once the release is published. This script re-pins to the wheel it just built, so that
# invariant holds; do not ship such a pin.
#
# Both come from `git archive COMMIT`, not from the working tree. Uncommitted
# edits in the platform repo never reach vendor/, and nothing is written into
# the platform repo.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="${1:-$here/../mps3-nanosoc-platform}"
ref="${2:-HEAD}"
py="${PYTHON:-}"
if [[ -z "$py" ]]; then
    if [[ -x "$here/.venv/bin/python" ]]; then py="$here/.venv/bin/python"; else py=python3; fi
fi

if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "vendor_pyverify: $repo is not a git checkout of the platform repo" >&2
    exit 2
fi
commit="$(git -C "$repo" rev-parse --verify "$ref^{commit}")"
subject="$(git -C "$repo" log -1 --format=%s "$commit")"
epoch="$(git -C "$repo" log -1 --format=%ct "$commit")"
date_iso="$(git -C "$repo" log -1 --format=%cI "$commit")"
remote="$(git -C "$repo" config --get remote.origin.url || echo unknown)"
tree_commit="$(git -C "$repo" log -1 --format=%H "$commit" -- host/pyverify)"
openocd_commit="$(git -C "$repo" log -1 --format=%H "$commit" -- host/openocd)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git -C "$repo" archive "$commit" host/pyverify host/openocd | tar -x -C "$work"

# SOURCE_DATE_EPOCH pins the timestamps inside the wheel to the commit's time.
SOURCE_DATE_EPOCH="$epoch" "$py" -m pip wheel -q --no-deps -w "$work/out" "$work/host/pyverify"
whl="$(ls "$work"/out/mps3_pyverify-*.whl)"
name="$(basename "$whl")"

sha256() {
    "$py" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
}

mkdir -p "$here/vendor"
rm -f "$here"/vendor/mps3_pyverify-*.whl
cp "$whl" "$here/vendor/$name"
sha="$(sha256 "$here/vendor/$name")"
cfg="$here/src/harness_manager_mps3/openocd_cfg"   # shipped in the wheel
rm -rf "$cfg"
cp -R "$work/host/openocd" "$cfg"
openocd_rows=""
for f in "$cfg"/*; do
    openocd_rows+="| \`openocd/$(basename "$f")\` | \`$(sha256 "$f")\` |"$'\n'
done
pyver="$("$py" -c 'import platform; print(platform.python_version())')"
pipver="$("$py" -m pip --version | awk '{print $2}')"

cat > "$here/vendor/README.md" <<EOF
# Vendored from the platform repo (a fallback that goes away)

**This wheel is a dev and CI fallback only, and it goes away once the platform release is
published.** Harness Manager pins the platform's PUBLISHED wheel by exact version and sha256
(\`pyverify.pin\`, set with \`scripts/pin_pyverify.sh <version> <sha256>\`). The installers and
\`make_wheelhouse.sh\` use a wheel from this directory only when its sha256 equals the pin's;
otherwise they download the pinned URL and verify it. When v<version> is published and pinned:
\`git rm vendor/mps3_pyverify-*.whl\` (keep the OpenOCD table below, it describes
\`src/harness_manager_mps3/openocd_cfg/\`).

pyverify is the only codec for the MPS3 shell protocol. It lives in the platform
repo (mps3-nanosoc-platform, \`host/pyverify\`) and is not on PyPI.

Do not edit anything here by hand. Rebuild it with \`scripts/vendor_pyverify.sh\` (or
\`make vendor-pyverify\`), which rewrites this file.

| Field | Value |
|---|---|
| Wheel | \`$name\` |
| sha256 | \`$sha\` |
| Built from | \`$remote\` |
| Platform commit | \`$commit\` ($date_iso) |
| Commit subject | $subject |
| Last pyverify change at or before it | \`$tree_commit\` |
| Source | \`git archive <commit> host/pyverify\` (committed files only) |
| Built with | \`pip wheel --no-deps\`, pip $pipver, Python $pyver, SOURCE_DATE_EPOCH=$epoch |

Check it: \`sha256sum vendor/$name\` must print the sha256 above.
\`tests/unit/test_l5_release.py\` checks this in \`make check\`.

## The MPS3 OpenOCD configs (openocd/)

\`openocd/\` (shipped in the wheel as \`src/harness_manager_mps3/openocd_cfg/\`) is \`host/openocd\` from the same platform commit (last changed at
\`$openocd_commit\`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there; anywhere else it uses the packaged copy.
\`HARNESS_MANAGER_MPS3_OPENOCD_DIR\` still overrides both.

| File | sha256 |
|---|---|
EOF
printf '%s' "$openocd_rows" >> "$here/vendor/README.md"

ver="${name#mps3_pyverify-}"; ver="${ver%%-*}"
"$here/scripts/pin_pyverify.sh" "$ver" "$sha" >/dev/null
echo "vendored $name"
echo "  sha256 $sha"
echo "  from   $commit ($subject)"
echo "  and    src/harness_manager_mps3/openocd_cfg/"
