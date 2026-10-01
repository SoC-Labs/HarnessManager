#!/usr/bin/env bash
# shellcheck disable=SC2153  # REPO, CATALOG, ...: from the sourced publish.env
# Upload a BUILT harness release to GitHub Releases (lane RELEASE-PIPE).
#
#   scripts/publish_harness_release.sh [--dry-run | --publish] --repo OWNER/REPO [options] RELEASE_DIR
#
# RELEASE_DIR is the tree `make harness-release` (python -m tools.release harness-release)
# wrote: GitHub's own URL layout, <dir>/<OWNER>/<REPO>/releases/download/<tag>/<file>.
# This script uploads what is in it; it never builds, re-signs or changes a file.
#
# --dry-run is the DEFAULT. It runs the checks, then PRINTS every gh command and runs
# none: no gh call, no network. A real upload needs --publish AND, at the terminal, the
# typed phrase "PUBLISH <catalog> <version> TO <OWNER/REPO>".
#
# The order of a real upload (each step stops the script if it fails):
#   1. checks, with the client's own code (python -m tools.release publish-check
#      --for-publish): the tree was built for OWNER/REPO; the channel is signed by a key
#      PINNED in this checkout's trust.PINNED_KEYS (a TEST key never goes out); the client
#      verifies the channel and every asset; every asset is on disk as signed;
#   2. the live channel, read with `gh release download` (read-only): ours must have a
#      higher serial and keep every live release (else rebuild with --base / --live-base);
#   3. the typed confirmation;
#   4. the per-version release (`gh release create <tag> ... <assets>`), then the Arm-IP
#      assets in the AAA repo when the release has any, then the rolling channel release
#      (`gh release create` once, then `gh release upload --clobber` of channel.json + its
#      .minisig): the channel goes last, so no client sees a release before its assets.
#
# Options:
#   --repo OWNER/REPO      the repo hosting the channel and the assets (required)
#   --aaa-repo OWNER/REPO  the private repo for the Arm-IP assets (default: the one the
#                          tree was built with, SoC-Labs/mps3-harness-aaa)
#   --catalog NAME         default mps3-harness
#   --channel NAME         default beta
#   --version V            default: the channel's current release
#   --public-key FILE      dry run: verify with this key when it is not pinned
#   --gh PATH              the gh binary (default: gh on PATH)
#   --dry-run              print the plan (default)
#   --publish              upload, after the checks and the typed confirmation
#   -h, --help             this text
#
# Environment: HM_PYTHON, the Python that runs the checks (default: this checkout's
# .venv/bin/python, else python3). gh uses its own login (`gh auth login`).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT="$(cd "$HERE/.." && pwd)"

usage() {
  awk 'NR > 1 && /^# shellcheck/ { next } NR > 1 && /^#/ { sub(/^# ?/, ""); print; next }
       NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

die() { echo "publish_harness_release: $*" >&2; exit 2; }

# The typed confirmation: one line on stdin must be exactly the phrase.
confirm_typed() {
  local phrase="$1" line=""
  printf 'To upload, type exactly:\n  %s\n> ' "$phrase" >&2
  IFS= read -r line || return 1
  [ "$line" = "$phrase" ]
}

# One gh command: printed always (shell-quoted, so it can be pasted), run only when
# publishing.
gh_step() {
  local q=""
  printf -v q '%q ' "$@"
  echo "  ${q% }"
  if [ "$MODE" = publish ]; then
    "$@"
  fi
}

read_list() {   # <file> -> the array LIST (one path per line)
  LIST=()
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] && LIST+=("$line")
  done < "$1"
}

main() {
  MODE=dry-run
  local repo="" aaa_repo="" catalog="mps3-harness" channel="beta" version="" pubkey=""
  local gh="gh" dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo)       repo="${2:?--repo needs OWNER/REPO}"; shift 2;;
      --aaa-repo)   aaa_repo="${2:?--aaa-repo needs OWNER/REPO}"; shift 2;;
      --catalog)    catalog="${2:?}"; shift 2;;
      --channel)    channel="${2:?}"; shift 2;;
      --version)    version="${2:?}"; shift 2;;
      --public-key) pubkey="${2:?}"; shift 2;;
      --gh)         gh="${2:?}"; shift 2;;
      --dry-run)    MODE=dry-run; shift;;
      --publish)    MODE=publish; shift;;
      -h|--help)    usage; exit 0;;
      -*)           die "unknown option $1 (see --help)";;
      *)            [ -z "$dir" ] || die "one RELEASE_DIR only"; dir="$1"; shift;;
    esac
  done
  [ -n "$repo" ] || die "--repo OWNER/REPO is required"
  [ -n "$dir" ] || die "give the RELEASE_DIR (the --out of make harness-release)"
  [ -d "$dir" ] || die "no release tree at $dir"
  case "$repo" in */*) ;; *) die "--repo must be OWNER/REPO";; esac

  if [ "$MODE" = publish ] && ! { [ -t 0 ] && [ -t 1 ]; }; then
    die "--publish needs an interactive terminal (the typed confirmation); nothing was uploaded"
  fi

  local py="${HM_PYTHON:-}"
  if [ -z "$py" ]; then
    if [ -x "$CHECKOUT/.venv/bin/python" ]; then py="$CHECKOUT/.venv/bin/python"; else py=python3; fi
  fi

  WORK="$(mktemp -d "${TMPDIR:-/tmp}/hm-publish.XXXXXX")"
  trap 'rm -rf "${WORK:-}"' EXIT
  # The plan inputs (file lists, notes) stay beside the tree, so a printed command can be
  # pasted after the dry run.
  local plan="$dir/plans/$catalog-$channel/publish"

  local check=(--root "$dir" --repo "$repo" --catalog "$catalog" --channel "$channel"
               --env-out "$plan")
  [ -n "$aaa_repo" ] && check+=(--aaa-repo "$aaa_repo")
  [ -n "$version" ] && check+=(--version "$version")
  [ -n "$pubkey" ] && check+=(--public-key "$pubkey")
  [ "$MODE" = publish ] && check+=(--for-publish)

  echo "== 1. checks (the client's own code) =="
  (cd "$CHECKOUT" && "$py" -m tools.release publish-check "${check[@]}") \
    || die "the checks refused this release; nothing was uploaded"
  # shellcheck source=/dev/null
  . "$plan/publish.env"

  echo
  echo "== 2. the live channel =="
  if [ "$MODE" = publish ]; then
    if "$gh" release view "$CHANNEL_TAG" --repo "$REPO" --json tagName >/dev/null 2>&1; then
      "$gh" release download "$CHANNEL_TAG" --repo "$REPO" --pattern channel.json \
        --dir "$WORK/live" --clobber || die "cannot read the live channel; nothing was uploaded"
      (cd "$CHECKOUT" && "$py" -m tools.release publish-check "${check[@]}" \
          --live "$WORK/live/channel.json" >/dev/null) \
        || die "the live channel refuses this one (see above); nothing was uploaded"
      echo "  live $CHANNEL_TAG: ours (serial $SERIAL) follows it and keeps every release"
    else
      echo "  no live $CHANNEL_TAG release yet: this is the first channel document"
    fi
  else
    echo "  (dry run) a real upload reads it first, read-only:"
    printf '  %q release download %q --repo %q --pattern channel.json --dir LIVE\n' \
      "$gh" "$CHANNEL_TAG" "$REPO"
    echo "  and refuses unless ours has a higher serial and keeps every live release"
  fi

  read_list "$ASSETS_FILE"; local assets=("${LIST[@]}")
  read_list "$AAA_ASSETS_FILE"; local aaa_assets=("${LIST[@]+"${LIST[@]}"}")
  read_list "$CHANNEL_FILES_FILE"; local channel_files=("${LIST[@]}")

  echo
  if [ "$MODE" = publish ]; then
    confirm_typed "PUBLISH $CATALOG $VERSION TO $REPO" \
      || die "the typed confirmation did not match; nothing was uploaded"
    echo "== 3. uploading =="
  else
    echo "== 3. the gh commands a real upload runs (DRY RUN: none is run) =="
    [ "$TEST" = 1 ] && echo "  (a TEST release: --publish refuses it at step 1)"
    [ "$PINNED" = 1 ] || echo "  (key $KEY_ID is not pinned: --publish refuses it at step 1)"
  fi
  gh_step "$gh" release create "$TAG" --repo "$REPO" --title "$TITLE" \
    --notes-file "$NOTES_FILE" --prerelease "${assets[@]}"
  if [ "${#aaa_assets[@]}" -gt 0 ]; then
    gh_step "$gh" release create "$TAG" --repo "$AAA_REPO" --title "$TITLE (Arm IP)" \
      --notes-file "$NOTES_FILE" --prerelease "${aaa_assets[@]}"
  fi
  if [ "$MODE" = publish ] && "$gh" release view "$CHANNEL_TAG" --repo "$REPO" \
      --json tagName >/dev/null 2>&1; then
    echo "  (the rolling release $CHANNEL_TAG exists)"
  else
    [ "$MODE" = publish ] || echo "  # only when $CHANNEL_TAG does not exist yet:"
    gh_step "$gh" release create "$CHANNEL_TAG" --repo "$REPO" --prerelease \
      --title "$CATALOG $CHANNEL channel" \
      --notes "Rolling channel release: channel.json + its signature. Managed by tools/release; do not edit by hand."
  fi
  gh_step "$gh" release upload "$CHANNEL_TAG" --repo "$REPO" --clobber "${channel_files[@]}"

  echo
  if [ "$MODE" = publish ]; then
    echo "PUBLISHED $CATALOG $VERSION to $REPO ($CHANNEL serial $SERIAL)."
  else
    echo "DRY RUN: nothing was uploaded. To upload: re-run with --publish at a terminal."
  fi
  echo "Verify from any machine with a token for $REPO:"
  echo "  harness-manager harness list --source github:$REPO --channel $CHANNEL"
}

# Run main only when executed; sourcing the file (the tests) just defines the functions.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
