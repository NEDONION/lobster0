# Lobster0 Phase 6 Sandbox command image。
#
# 这个镜像只承载 DockerSandbox.build_argv 固定的 hardened 运行形态：
# `--user 65532:65532 --read-only --network none --cap-drop ALL`、
# `--tmpfs /tmp:rw,noexec,nosuid,size=64m` 与 `--workdir /workspace`。
# 镜像本身不得有 ENTRYPOINT，模型提供的 exact argv 只能被内核直接执行。
#
# 基础镜像固定 docker.io/library/python:3.12-slim（3.12.13-slim-trixie）index digest。
#
#   docker build -f deploy/sandbox.Dockerfile -t ghcr.io/nedonion/lobster0-sandbox:0.7.0 .

FROM python@sha256:4fad23465a06cc5149a541fbec6f87e234a64dc0550f6bfdd2d290d8f03240df AS sandbox

LABEL org.opencontainers.image.title="Lobster0 Sandbox" \
      org.opencontainers.image.description="Non-root read-only command image for Lobster0 Phase 6 Sandbox" \
      org.opencontainers.image.version="0.7.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/NEDONION/lobster0" \
      org.opencontainers.image.base.name="docker.io/library/python" \
      org.opencontainers.image.base.digest="sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

ENV LANG=C.UTF-8 \
    HOME=/tmp \
    PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1

# 固定 UID/GID 65532、read-only rootfs 下可用的 /tmp 与 /workspace 挂载点，
# 并把 resolver、包数据库与全部构建缓存移出 final layer。
RUN set -eu; \
    printf '%s\n' 'nonroot:x:65532:' >> /etc/group; \
    printf '%s\n' 'nonroot:x:65532:65532:nonroot:/tmp:/usr/sbin/nologin' >> /etc/passwd; \
    mkdir -p /workspace; \
    chown 65532:65532 /workspace; \
    chmod 0755 /workspace; \
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
        /root/.cache /tmp/* /tmp/.[!.]*; \
    find / -xdev -name '__pycache__' -type d -prune -exec rm -rf '{}' +; \
    chmod 1777 /tmp; \
    for resolver in pip pip3 apt apt-get dpkg uv npm pnpm yarn; do \
        if command -v "$resolver" >/dev/null 2>&1; then \
            printf 'package resolver survived in final layer: %s\n' "$resolver" >&2; \
            exit 1; \
        fi; \
    done; \
    if python3 -c \
        "import importlib.util as u, sys; sys.exit(0 if u.find_spec('pip') or u.find_spec('ensurepip') else 1)"; \
    then printf 'python resolver survived in final layer\n' >&2; exit 1; fi

WORKDIR /workspace
USER 65532:65532
ENTRYPOINT []
CMD ["/usr/local/bin/python3.12", "--version"]
