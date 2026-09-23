#!/usr/bin/env bash
# Build the pyverify wheel (distribution mps3-pyverify) from one commit of the
# platform repo, and put it in vendor/ with its provenance. The same commit's
# MPS3 OpenOCD configs (host/openocd) go to vendor/openocd/.
#
#   scripts/vendor_pyverify.sh [PLATFORM_REPO [COMMIT]]
#
#   PLATFORM_REPO  default: ../mps3-nanosoc-platform (next to this checkout)
#   COMMIT         default: that repo's HEAD
#
# pyverify is not on PyPI. Harness Manager depends on it, so the installer and
# CI install it from vendor/ (pip --find-links vendor).
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
rm -rf "$here/vendor/openocd"
cp -R "$work/host/openocd" "$here/vendor/openocd"
openocd_rows=""
for f in "$here"/vendor/openocd/*; do
    openocd_rows+="| \`openocd/$(basename "$f")\` | \`$(sha256 "$f")\` |"$'\n'
done
pyver="$("$py" -c 'import platform; print(platform.python_version())')"
pipver="$("$py" -m pip --version | awk '{print $2}')"

cat > "$here/vendor/README.md" <<EOF
# Vendored from the platform repo

pyverify is the only codec for the MPS3 shell protocol. It lives in the platform
repo (mps3-nanosoc-platform, \`host/pyverify\`) and is not on PyPI. Harness Manager
needs it at run time, so its wheel is kept here. The installer and CI install it
by path first, then Harness Manager with \`pip --find-links vendor\`.

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

\`openocd/\` is \`host/openocd\` from the same platform commit (last changed at
\`$openocd_commit\`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there. Anywhere else, set
\`HARNESS_MANAGER_MPS3_OPENOCD_DIR\` to this directory (CI does).

| File | sha256 |
|---|---|
EOF
printf '%s' "$openocd_rows" >> "$here/vendor/README.md"

echo "vendored $name"
echo "  sha256 $sha"
echo "  from   $commit ($subject)"
echo "  and    vendor/openocd/"
