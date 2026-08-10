# Lobster0 non-root complete Runtime image。
#
# builder stage 只做「校验 + 安装」，final stage 只保留 Runtime 输出，
# 不含任何 package resolver、Docker socket、Secret 或默认 Provider key。
#
# 基础镜像固定 docker.io/library/python:3.12-slim（3.12.13-slim-trixie）的
# multi-platform index digest，绝不按可变 tag 解析。
#
# 发布构建（含 native bundle）：
#   docker build -f deploy/Dockerfile \
#     --build-arg LOBSTER0_REQUIRE_NATIVE_BUNDLES=1 \
#     --build-arg LOBSTER0_WHEEL_SHA256=<64 hex> \
#     -t ghcr.io/nedonion/lobster0:0.7.0 .

FROM python@sha256:3a9d2dd3f18e5c7a9d8de7b3659418a4ab848ccd409fb9e91ef9d7a6a3520ba7 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# 空值表示本地开发构建；发布构建必须显式传入 wheel 摘要与 native bundle 名称。
ARG LOBSTER0_WHEEL_SHA256=""
ARG LOBSTER0_TUI_BUNDLE="lobster0-tui-0.7.0-linux-x86_64.tar.gz"
ARG LOBSTER0_NODE_BUNDLE="lobster0-node-24.18.0-linux-x86_64.tar.gz"
ARG LOBSTER0_REQUIRE_NATIVE_BUNDLES="0"

WORKDIR /build

COPY requirements-all.lock /build/requirements-all.lock
COPY dist/lobster0_agent-0.7.0-py3-none-any.whl /build/lobster0_agent-0.7.0-py3-none-any.whl
COPY deploy/artifacts/ /build/artifacts/

# 先校验 wheel 摘要，再在离线模式下按 hash-locked 依赖构建 Runtime venv。
RUN set -eu; \
    if [ -n "$LOBSTER0_WHEEL_SHA256" ]; then \
        printf '%s  %s\n' "$LOBSTER0_WHEEL_SHA256" \
            /build/lobster0_agent-0.7.0-py3-none-any.whl | sha256sum -c -; \
    fi; \
    python -m venv --copies /opt/lobster0/venv; \
    /opt/lobster0/venv/bin/python -m pip install \
        --require-hashes --no-deps --only-binary :all: \
        --requirement /build/requirements-all.lock; \
    /opt/lobster0/venv/bin/python -m pip install \
        --no-deps --no-index \
        /build/lobster0_agent-0.7.0-py3-none-any.whl; \
    /opt/lobster0/venv/bin/python -m pip uninstall --yes pip setuptools wheel; \
    find /opt/lobster0/venv -name '__pycache__' -type d -prune -exec rm -rf '{}' +

# 解包受管 Node 与 pi-tui bundle；发布构建缺 bundle 时必须失败，不静默降级。
RUN set -eu; \
    mkdir -p /opt/lobster0/current; \
    for pair in "tui:$LOBSTER0_TUI_BUNDLE" "node:$LOBSTER0_NODE_BUNDLE"; do \
        component="${pair%%:*}"; \
        bundle="/build/artifacts/${pair#*:}"; \
        if [ -f "$bundle" ]; then \
            tar -xzf "$bundle" -C /opt/lobster0/current --no-same-owner --no-same-permissions; \
        elif [ "$LOBSTER0_REQUIRE_NATIVE_BUNDLES" = "1" ]; then \
            printf 'missing native bundle for %s: %s\n' "$component" "$bundle" >&2; \
            exit 1; \
        fi; \
    done; \
    printf '%s\n' "$LOBSTER0_REQUIRE_NATIVE_BUNDLES" > /opt/lobster0/current/native-bundles-required


FROM python@sha256:3a9d2dd3f18e5c7a9d8de7b3659418a4ab848ccd409fb9e91ef9d7a6a3520ba7 AS runtime

LABEL org.opencontainers.image.title="Lobster0" \
      org.opencontainers.image.description="Non-root complete Lobster0 runtime" \
      org.opencontainers.image.version="0.7.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/NEDONION/lobster0" \
      org.opencontainers.image.base.name="docker.io/library/python" \
      org.opencontainers.image.base.digest="sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

COPY --from=builder /opt/lobster0 /opt/lobster0
COPY deploy/entrypoint.sh /usr/local/bin/lobster0-entrypoint

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PATH=/opt/lobster0/venv/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/data \
    LOBSTER0_HOME=/data \
    LOBSTER0_PREFIX=/opt/lobster0 \
    LOBSTER0_NODE=/opt/lobster0/current/node/bin/node \
    LOBSTER0_TUI_ENTRY=/opt/lobster0/current/tui/dist/main.js

# 建立 owner-only 状态根与固定非 root 身份，并把 resolver 与包数据库整体移出 final layer。
RUN set -eu; \
    chmod 0755 /usr/local/bin/lobster0-entrypoint; \
    printf '%s\n' 'nonroot:x:65532:' >> /etc/group; \
    printf '%s\n' 'nonroot:x:65532:65532:nonroot:/data:/usr/sbin/nologin' >> /etc/passwd; \
    mkdir -p /data; \
    chown 65532:65532 /data; \
    chmod 700 /data; \
    rm -rf \
        /usr/local/lib/python3.12/ensurepip \
        /usr/local/lib/python3.12/site-packages/pip \
        /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.12/site-packages/setuptools \
        /usr/local/lib/python3.12/site-packages/setuptools-*.dist-info \
        /usr/local/lib/python3.12/site-packages/pkg_resources \
        /usr/local/lib/python3.12/site-packages/wheel \
        /usr/local/lib/python3.12/site-packages/wheel-*.dist-info \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
        /usr/bin/apt /usr/bin/apt-cache /usr/bin/apt-cdrom /usr/bin/apt-config \
        /usr/bin/apt-get /usr/bin/apt-key /usr/bin/apt-mark \
        /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/dpkg-divert /usr/bin/dpkg-query \
        /usr/bin/dpkg-split /usr/bin/dpkg-statoverride /usr/bin/dpkg-trigger \
        /usr/sbin/dpkg-preconfigure /usr/sbin/dpkg-reconfigure \
        /usr/lib/apt /etc/apt \
        /var/lib/apt /var/lib/dpkg /var/cache/apt /var/cache/debconf \
        /root/.cache /tmp/*; \
    for resolver in pip pip3 apt apt-get dpkg uv npm pnpm yarn; do \
        if command -v "$resolver" >/dev/null 2>&1; then \
            printf 'package resolver survived in final layer: %s\n' "$resolver" >&2; \
            exit 1; \
        fi; \
    done; \
    if /opt/lobster0/venv/bin/python -c \
        "import importlib.util as u, sys; sys.exit(0 if u.find_spec('pip') or u.find_spec('ensurepip') else 1)"; \
    then printf 'python resolver survived in final layer\n' >&2; exit 1; fi

# 构建期 smoke：CLI 版本与全部 Channel import 必须在最终文件系统上成立。
RUN set -eu; \
    /opt/lobster0/venv/bin/lobster0 --version; \
    /opt/lobster0/venv/bin/python -c \
        "import lobster0, lobster0.cli, lobster0.channels.feishu, lobster0.channels.telegram, lobster0.channels.discord"

VOLUME ["/data"]
WORKDIR /data
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/lobster0-entrypoint"]
CMD ["--help"]
