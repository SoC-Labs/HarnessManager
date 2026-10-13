#!/usr/bin/env bash
# Build "Harness Manager.app".
#
#   packaging/macos/make_app.sh APP_DIR LAUNCHER ICNS [VERSION]
#
# APP_DIR    where the bundle goes, e.g. "$HOME/Applications/Harness Manager.app"
# LAUNCHER   the installed `harness-manager` command the bundle runs
# ICNS       the .icns file (packaging/icon/build/harness-manager.icns)
#
# Idempotent: it replaces a bundle this script made and refuses to touch anything else.
# Remove the bundle with:  packaging/macos/make_app.sh --remove APP_DIR
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
marker="Contents/Resources/.harness-manager-installer"

if [[ "${1:-}" == "--remove" ]]; then
    app="${2:?usage: make_app.sh --remove APP_DIR}"
    if [[ -f "$app/$marker" ]]; then rm -rf "$app"; echo "removed  $app"; fi
    exit 0
fi

app="${1:?usage: make_app.sh APP_DIR LAUNCHER ICNS [VERSION]}"
hm="${2:?launcher path missing}"
icns="${3:?icns path missing}"
version="${4:-1.2.0}"

case "$hm$app" in
    *[\"\`\$\\]*|*$'\n'*) echo "make_app.sh: the path has characters a shell script cannot quote" >&2; exit 1 ;;
esac
[[ -f "$icns" ]] || { echo "make_app.sh: no icon at $icns" >&2; exit 1; }
if [[ -e "$app" && ! -f "$app/$marker" ]]; then
    echo "make_app.sh: left $app alone: this script did not write it" >&2
    exit 2
fi

tmp="$app.tmp.$$"
rm -rf "$tmp"
mkdir -p "$tmp/Contents/MacOS" "$tmp/Contents/Resources"
sed "s|@VERSION@|$version|g" "$here/Info.plist.in" >"$tmp/Contents/Info.plist"
sed "s|@HM@|$hm|g" "$here/launcher.in" >"$tmp/Contents/MacOS/harness-manager-app"
chmod 755 "$tmp/Contents/MacOS/harness-manager-app"
cp "$icns" "$tmp/Contents/Resources/harness-manager.icns"
printf 'Written by scripts/install.sh; removed by --uninstall.\n' >"$tmp/$marker"
rm -rf "$app"
mv "$tmp" "$app"
echo "app      $app"
