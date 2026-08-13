#!/usr/bin/env bash
set -euo pipefail

# Run on the VPS from /opt/mls-screen.  The git checkout is the source of truth;
# this manifest detects online edits before the next canonical deployment.
ROOT="${1:-/opt/mls-screen}"
MANIFEST="$ROOT/deploy/source.manifest.sha256"
mkdir -p "$ROOT/deploy"
cd "$ROOT"

case "${2:-verify}" in
  snapshot)
    {
      find . -maxdepth 1 -type f \
        \( -name '*.py' -o -name 'index.html' -o -name 'DEPLOYMENT.md' \) -print0
      find deploy -maxdepth 1 -type f \
        ! -name 'source.manifest.sha256' -print0
    } | sort -z | xargs -0 sha256sum > "$MANIFEST"
    printf 'wrote %s\\n' "$MANIFEST"
    ;;
  verify)
    test -s "$MANIFEST"
    sha256sum --check "$MANIFEST"
    ;;
  *)
    printf 'usage: %s [root] {snapshot|verify}\\n' "$0" >&2
    exit 2
    ;;
esac
