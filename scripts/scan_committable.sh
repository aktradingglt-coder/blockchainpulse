#!/usr/bin/env bash
# Scans exactly what git would commit (tracked + untracked-but-not-ignored files) with gitleaks.
# Ignored files (.env, build output, caches) are never scanned or copied.
set -euo pipefail
cd "$(dirname "$0")/.."
gitleaks_bin="${GITLEAKS:-.tools/bin/gitleaks}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
  [[ -f "$f" ]] || continue
  mkdir -p "$work/$(dirname "$f")"
  cp -p "$f" "$work/$f"
done
"$gitleaks_bin" dir --config .gitleaks.toml --redact --no-banner "$work"
