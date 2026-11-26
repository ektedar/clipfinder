#!/usr/bin/env bash
# TruffleHog secret scan for git hooks (run by pre-commit; see .pre-commit-config.yaml).
#   pre-commit stage: scans the staged changes
#   pre-push stage:   scans every commit about to be pushed
# Exits non-zero (blocking the commit/push) if it finds anything that looks like a secret.
# A false positive can be silenced by adding a `trufflehog:ignore` comment on that line.
set -euo pipefail

if ! command -v trufflehog >/dev/null 2>&1; then
  echo "trufflehog is not installed. Install it with: brew install trufflehog" >&2
  exit 1
fi

args=(--results="${TRUFFLEHOG_RESULTS:-verified,unknown,unverified}" --fail --no-update)

if [[ -n "${PRE_COMMIT_TO_REF:-}" ]]; then
  # pre-push: pre-commit exports the range being pushed
  from="${PRE_COMMIT_FROM_REF:-}"
  if [[ -n "$from" && ! "$from" =~ ^0+$ ]] && git cat-file -e "$from" 2>/dev/null; then
    exec trufflehog git file://. --since-commit "$from" --branch "$PRE_COMMIT_TO_REF" "${args[@]}"
  fi
  # new branch with no known base: scan everything reachable from what's being pushed
  exec trufflehog git file://. --branch "$PRE_COMMIT_TO_REF" "${args[@]}"
fi

# pre-commit: TruffleHog scans the staged changes when given --since-commit HEAD
exec trufflehog git file://. --since-commit HEAD "${args[@]}"
