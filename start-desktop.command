#!/bin/zsh
set -euo pipefail

readonly REPOSITORY_ROOT="${0:A:h}"
cd "$REPOSITORY_ROOT"

command -v uv >/dev/null 2>&1 || exit 2
command -v node >/dev/null 2>&1 || exit 2
command -v corepack >/dev/null 2>&1 || exit 2
node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=19)?0:1)'

if [[ ! -x "$REPOSITORY_ROOT/.venv/bin/python" \
  || ! -x "$REPOSITORY_ROOT/.venv/bin/miniclaw" ]]; then
  uv sync --extra dev
fi
if [[ ! -d "$REPOSITORY_ROOT/tui/node_modules" ]]; then
  corepack pnpm --dir tui install --frozen-lockfile
fi
if [[ ! -d "$REPOSITORY_ROOT/desktop/node_modules" ]]; then
  corepack pnpm --dir desktop install --frozen-lockfile
fi

readonly STATE_HOME="$(
  "$REPOSITORY_ROOT/.venv/bin/python" \
    -c 'from miniclaw.paths import resolve_home; print(resolve_home())'
)"
export MINICLAW_HOME="$STATE_HOME"
export MINICLAW_PYTHON="$REPOSITORY_ROOT/.venv/bin/python"

if [[ -f "$STATE_HOME/config.toml" ]]; then
  "$REPOSITORY_ROOT/.venv/bin/miniclaw" init --home "$STATE_HOME"
else
  "$REPOSITORY_ROOT/.venv/bin/miniclaw" setup --home "$STATE_HOME"
fi
if [[ -z "${MINICLAW_ENV_FILE:-}" && -f "$STATE_HOME/secrets.env" ]]; then
  export MINICLAW_ENV_FILE="$STATE_HOME/secrets.env"
fi
corepack pnpm --dir tui build
corepack pnpm --dir desktop dev
