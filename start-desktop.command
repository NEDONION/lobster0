#!/bin/zsh
set -euo pipefail

readonly REPOSITORY_ROOT="${0:A:h}"
cd "$REPOSITORY_ROOT"

fail() {
  local message="$1"
  local exit_code="${2:-2}"
  print -u2 -- "Lobster0 Desktop: $message"
  exit "$exit_code"
}

on_error() {
  local exit_code="$?"
  trap - ZERR
  set +e
  print -u2 -- "Lobster0 Desktop 启动失败（exit $exit_code），请查看上方输出。"
  if [[ -t 0 ]]; then
    read -r "?按回车关闭窗口..." _
  fi
  exit "$exit_code"
}
trap on_error ZERR

[[ "$(uname -s)" == "Darwin" ]] || fail "当前一键入口仅支持 macOS。"
command -v uv >/dev/null 2>&1 || fail "缺少 uv，请先安装 uv。"
command -v node >/dev/null 2>&1 || fail "缺少 Node.js，需要 >=22.19.0。"
command -v corepack >/dev/null 2>&1 \
  || fail "缺少 Corepack，请安装完整 Node.js >=22.19.0。"
node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=19)?0:1)' \
  || fail "Node.js 版本过低，需要 >=22.19.0。"

if [[ ! -x "$REPOSITORY_ROOT/.venv/bin/python" \
  || ! -x "$REPOSITORY_ROOT/.venv/bin/lobster0" ]]; then
  uv sync --extra dev
fi
if [[ ! -d "$REPOSITORY_ROOT/tui/node_modules" ]]; then
  corepack pnpm --dir tui install --frozen-lockfile
fi
corepack pnpm --dir tui build
if [[ ! -d "$REPOSITORY_ROOT/desktop/node_modules" ]]; then
  corepack pnpm --dir desktop install --frozen-lockfile
elif [[ ! -f "$REPOSITORY_ROOT/desktop/node_modules/@lobster0/pi-tui/dist/bridge-client.js" ]]; then
  corepack pnpm --dir desktop install --force --frozen-lockfile
fi
node -e 'require("./desktop/node_modules/electron")'

readonly STATE_HOME="$(
  "$REPOSITORY_ROOT/.venv/bin/python" \
    -c 'from lobster0.paths import resolve_home; print(resolve_home())'
)"
export LOBSTER0_HOME="$STATE_HOME"
export LOBSTER0_PYTHON="$REPOSITORY_ROOT/.venv/bin/python"

if [[ -f "$STATE_HOME/config.toml" ]]; then
  "$REPOSITORY_ROOT/.venv/bin/lobster0" init --home "$STATE_HOME"
else
  "$REPOSITORY_ROOT/.venv/bin/lobster0" setup --home "$STATE_HOME"
fi
if [[ -z "${LOBSTER0_ENV_FILE:-}" && -f "$STATE_HOME/secrets.env" ]]; then
  export LOBSTER0_ENV_FILE="$STATE_HOME/secrets.env"
elif [[ -z "${LOBSTER0_ENV_FILE:-}" && -f "$REPOSITORY_ROOT/.env" ]]; then
  export LOBSTER0_ENV_FILE="$REPOSITORY_ROOT/.env"
fi
corepack pnpm --dir desktop dev
