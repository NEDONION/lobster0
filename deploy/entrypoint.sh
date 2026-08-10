#!/bin/sh
# Lobster0 容器稳定入口：以 exact argv 执行受管 CLI，绝不重解释调用方参数。
set -eu

LOBSTER0_BIN=/opt/lobster0/venv/bin/lobster0

if [ ! -x "$LOBSTER0_BIN" ]; then
    printf '%s\n' "lobster0 runtime is missing or not executable" >&2
    exit 78
fi

if [ ! -d "${LOBSTER0_HOME:-/data}" ]; then
    printf '%s\n' "lobster0 state home is missing" >&2
    exit 78
fi

exec "$LOBSTER0_BIN" "$@"
