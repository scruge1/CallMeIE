#!/usr/bin/env bash
# d5-build-runner.sh — container entrypoint for d5_build_runner.py.
# Mirrors docops-rescue-daemon entrypoint pattern (INFRA.md §14.5).
#
# When running INSIDE the Coolify container, env comes from the Coolify
# env panel (joined to the `coolify` network for DATABASE_URL access).
# When running OUTSIDE for local dev, source the routes vault.
set -euo pipefail

# Routes-vault style env load — only used when running outside Coolify.
if [[ -f "${HOME:-/root}/.claude/routes/.env" && -z "${DATABASE_URL:-}" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${HOME:-/root}/.claude/routes/.env"
  set +a
fi

: "${DATABASE_URL:?DATABASE_URL required}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY required}"

cd "$(dirname "$0")"
exec python3 d5_build_runner.py
