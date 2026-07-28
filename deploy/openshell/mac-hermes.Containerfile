# mac-hermes sandbox image — the runtime image OpenShell runs the Hermes agent
# inside (`openshell sandbox create --from localhost/mac-hermes:net`).
#
# Multi-arch: python:3.12-slim resolves to the host architecture, so the SAME
# Containerfile builds natively on x86_64 (rocky, bullwinkle) and aarch64
# (natasha / GB10). Build from the mac source tree as context:
#
#   docker build  -t localhost/mac-hermes:net -f deploy/openshell/mac-hermes.Containerfile <mac-src>
#
# MAC/OpenShell standardizes on Docker Engine/Moby as the only production
# container runtime. Do not build this image with Podman: OpenShell's gateway,
# image store, GPU/CDI behavior, and nested-container path must all use the same
# Docker driver.
#
# Hard-won requirements baked in (each line below is load-bearing — see the
# comments): a `sandbox` user/group, `iproute2` (the egress proxy's `ip`), the
# hermes_cli path hook, and a sandbox-writable /sandbox for the Docker driver.

FROM ghcr.io/astral-sh/uv@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv

FROM docker.io/library/python@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

ENV DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/mac-venv \
    UV_PYTHON_DOWNLOADS=never

# iproute2: OpenShell's network-isolation proxy requires `ip` ("trusted ip
#   helper not found" otherwise). git/curl/gh: task work + git push egress.
# codex/claude/cursor-agent: the three reviewed coding-agent CLIs for confined
#   coding tasks. All three MUST resolve by basename through the image-owned
#   PATH (the reconciled advertisement/probe contract): the build below gates
#   each with `command -v <basename>` plus a pinned `--version` so a missing
#   install, a dangling symlink, or a non-PATH binary fails the build closed
#   instead of shipping an image the in-sandbox probe later rejects as
#   agent_binary_missing. codegraph: local codebase indexing and inspection
#   baseline for agent work.
# bash >=5.2: the explicit task-runtime shell contract.  Do not rely on the
# base image carrying Bash transitively; executor and verification commands
# invoke /bin/bash and deployment fails if its version/features are unsuitable.
# procps: repository contracts inspect child/process lifecycle with `ps`.
#   Debian-slim does not ship it; without this baseline tool otherwise-valid
#   contract tests fail in the sandbox with FileNotFoundError before assertions
#   can run.
# make/node/npm/java/pnpm/lein: common repository contracts. The executor can
# still provision missing tools into a task-local .mac-toolchain, but the base
# image should cover ordinary polyglot repos without mutating the host fleet.
# build-essential: a C/C++ toolchain (cc/gcc/g++) for repos that compile native
#   code (e.g. nanolang's 3-stage `make build`); Debian-slim ships none.
# libssl-dev: OpenSSL headers + libcrypto. nanolang's src/sign.c #includes
#   <openssl/evp.h>/<sha.h>/<err.h> and the build links -lcrypto; without it
#   `make build` fails and a coding agent will destructively stub sign.c just to
#   compile. A real build dependency belongs in the base image.
# clang/llvm/lld/qemu-system-misc: the current production executor cannot yet
#   materialize ADR 0009 root-level overlay images.  Until that lane exists,
#   the synchronized cut-over must carry the complete, architecture-neutral
#   RISC-V validation floor used by c26: clang, llvm-objcopy, ld.lld, and
#   qemu-system-riscv64.  The build-time probe below proves the toolchain is
#   functional on both published image architectures instead of merely present.
#   Bookworm's QEMU 7.2 lacks virtio-sound-device, so QEMU alone comes from the
#   official bookworm-backports suite; the device probe makes that version floor
#   an executable contract instead of a floating-package assumption.
# nodejs from NodeSource (v22 LTS), NOT Debian's nodejs (v18): current pnpm
#   refuses Node < v22.13 ("This version of pnpm requires at least Node.js
#   v22.13"), which silently breaks every `pnpm install` repo bootstrap.
# sandbox user/group: OpenShell refuses any image lacking a `sandbox` user.
ARG GH_VERSION="2.95.0"
ARG CODEGRAPH_VERSION="v1.1.6"
ARG NODE_VERSION="22.23.1"
ARG PNPM_VERSION="11.13.1"
ARG CODEX_VERSION="0.140.0"
ARG CLAUDE_VERSION="2.1.220"
ARG CURSOR_VERSION="2026.07.23-e383d2b"
ARG BUILDX_VERSION="0.30.1"
ARG TARGETARCH
COPY .mac-openshell-build-assets /tmp/mac-openshell-build-assets
COPY deploy/verify-bash-contract.sh /usr/local/bin/mac-verify-bash-contract
COPY --from=uv /uv /usr/local/bin/uv
RUN printf '%s\n' 'deb http://deb.debian.org/debian bookworm-backports main' > /etc/apt/sources.list.d/mac-bookworm-backports.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl tar xz-utils \
    && chmod 0755 /usr/local/bin/mac-verify-bash-contract \
    && /usr/local/bin/mac-verify-bash-contract \
    && apt-get install -y --no-install-recommends iproute2 iptables git procps make build-essential libssl-dev openjdk-17-jre-headless clang llvm lld \
    && apt-get install -y --no-install-recommends -t bookworm-backports qemu-system-misc \
    && command -v ps >/dev/null \
    && command -v clang >/dev/null \
    && command -v llvm-objcopy >/dev/null \
    && command -v ld.lld >/dev/null \
    && command -v qemu-system-riscv64 >/dev/null \
    && printf '%s\n' 'void _start(void) { for (;;) {} }' > /tmp/mac-riscv-probe.c \
    && clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64 -mcmodel=medany -ffreestanding -fuse-ld=lld -nostdlib -nostartfiles -Wl,-e,_start /tmp/mac-riscv-probe.c -o /tmp/mac-riscv-probe.elf \
    && llvm-objcopy -O binary /tmp/mac-riscv-probe.elf /tmp/mac-riscv-probe.bin \
    && test -s /tmp/mac-riscv-probe.bin \
    && qemu-system-riscv64 --version \
    && qemu-system-riscv64 -M virt -device help > /tmp/mac-qemu-devices 2>&1 \
    && for device in virtio-gpu-device virtio-keyboard-device virtio-mouse-device virtio-sound-device virtio-blk-device virtio-net-device; do grep -F "$device" /tmp/mac-qemu-devices >/dev/null || exit 1; done \
    && rm -f /tmp/mac-riscv-probe.c /tmp/mac-riscv-probe.elf /tmp/mac-riscv-probe.bin /tmp/mac-qemu-devices \
    && (cd /tmp/mac-openshell-build-assets && sha256sum -c SHA256SUMS) \
    && case "$TARGETARCH" in \
         amd64) asset_arch=amd64; gh_arch=amd64; codegraph_arch=amd64 ;; \
         arm64) asset_arch=arm64; gh_arch=arm64; codegraph_arch=arm64 ;; \
         *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 2 ;; \
       esac \
    && install -d -m0755 /usr/local/lib/docker/cli-plugins /usr/local/libexec/docker/cli-plugins \
    && install -m0755 "/tmp/mac-openshell-build-assets/buildx-${asset_arch}" /usr/local/lib/docker/cli-plugins/docker-buildx \
    && ln -s /usr/local/lib/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx \
    && /usr/local/lib/docker/cli-plugins/docker-buildx version | grep -F "v${BUILDX_VERSION}" \
    && tar -xJf "/tmp/mac-openshell-build-assets/node-${asset_arch}.tar.xz" -C /usr/local --strip-components=1 \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && tar -xzf "/tmp/mac-openshell-build-assets/gh-${asset_arch}.tgz" -C /tmp \
    && install -m755 "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh \
    && rm -rf "/tmp/gh_${GH_VERSION}_linux_${gh_arch}" \
    && npm install -g "@openai/codex@${CODEX_VERSION}" "pnpm@${PNPM_VERSION}" \
    && CLAUDE_HOME="/usr/local/lib/claude-code/versions/${CLAUDE_VERSION}" \
    && install -d -m0755 "$CLAUDE_HOME" \
    && tar -xzf "/tmp/mac-openshell-build-assets/claude-${asset_arch}.tgz" -C "$CLAUDE_HOME" --strip-components=1 \
    && ln -sfn "$CLAUDE_HOME/claude" /usr/local/bin/claude \
    && CURSOR_HOME="/usr/local/lib/cursor-agent/versions/${CURSOR_VERSION}" \
    && install -d -m0755 "$CURSOR_HOME" \
    && tar -xzf "/tmp/mac-openshell-build-assets/cursor-${asset_arch}.tgz" -C "$CURSOR_HOME" --strip-components=1 \
    && ln -sfn "$CURSOR_HOME/cursor-agent" /usr/local/bin/cursor-agent \
    && ln -sfn "$CURSOR_HOME/cursor-agent" /usr/local/bin/agent \
    && chown -R root:root /usr/local/lib/claude-code /usr/local/lib/cursor-agent \
    && chmod -R a+rX /usr/local/lib/claude-code /usr/local/lib/cursor-agent \
    && command -v codex \
    && command -v claude \
    && command -v cursor-agent \
    && command -v agent \
    && codex --version | grep -F "${CODEX_VERSION}" \
    && claude --version | grep -F "${CLAUDE_VERSION}" \
    && cursor-agent --version | grep -F "${CURSOR_VERSION}" \
    && test "$(pnpm --version)" = "$PNPM_VERSION" \
    && install -m755 /tmp/mac-openshell-build-assets/lein /usr/local/bin/lein \
    && CG_HOME="/usr/local/lib/codegraph/versions/${CODEGRAPH_VERSION}" \
    && mkdir -p "$CG_HOME" \
    && tar -xzf "/tmp/mac-openshell-build-assets/codegraph-${codegraph_arch}.tgz" -C "$CG_HOME" --strip-components=1 \
    && ln -sfn "$CG_HOME" /usr/local/lib/codegraph/current \
    && printf '#!/bin/sh\nexec "%s/node" --liftoff-only "%s/lib/dist/bin/codegraph.js" "$@"\n' "$CG_HOME" "$CG_HOME" > /usr/local/bin/codegraph \
    && chown -R root:root /usr/local/lib/codegraph /usr/local/bin/codegraph \
    && chmod -R a+rX /usr/local/lib/codegraph \
    && chmod 0755 /usr/local/bin/codegraph \
    && codegraph install --yes \
    && groupadd -r sandbox && useradd -r -g sandbox -m -d /home/sandbox sandbox \
    && rm -rf /tmp/mac-openshell-build-assets \
    && rm -rf /var/lib/apt/lists/*

# pnpm/npm: tune for a constrained L7 egress proxy. A large monorepo install
# (1000+ deps) opens many concurrent TLS connections to the registry; OpenShell's
# deny-by-default egress proxy resets them at high concurrency (UND_ERR_SOCKET /
# ERR_PNPM_META_FETCH_FAIL), and pnpm's release-age supply-chain pass amplifies it
# by fetching metadata for every entry. Cap network concurrency, raise
# retries/timeouts, and disable the release-age check. A world-readable global
# config + env vars so the non-root `sandbox` user (HOME=/tmp) honors it too.
RUN printf '%s\n' \
      'network-concurrency=2' \
      'child-concurrency=2' \
      'fetch-retries=6' \
      'fetch-retry-mintimeout=20000' \
      'fetch-retry-maxtimeout=120000' \
      'fetch-timeout=300000' \
      'minimum-release-age=0' \
      > /etc/npmrc \
    && chmod 0644 /etc/npmrc
ENV NPM_CONFIG_GLOBALCONFIG=/etc/npmrc \
    npm_config_network_concurrency=2 \
    npm_config_fetch_retries=6 \
    npm_config_fetch_retry_mintimeout=20000 \
    npm_config_fetch_retry_maxtimeout=120000 \
    npm_config_fetch_timeout=300000 \
    npm_config_minimum_release_age=0

# Install the mac runtime into the in-image venv. The vendored Hermes lives at
# mac/_hermes/hermes_cli, which `import hermes_cli` only finds if mac/_hermes is
# on sys.path — so drop a .pth that adds it (the executor runs
# `python -m hermes_cli.main chat`).
COPY pyproject.toml uv.lock README.md /tmp/mac-src/
COPY src /tmp/mac-src/src
# Install the [dev] extra (pytest, coverage, psycopg, kubernetes) so the task
# sandbox can RUN the repository contract test — scripts/run-contract-tests.sh
# collects the full suite, which imports those at collection time. Without it,
# in-sandbox verification of a repo-coupled code task fails to execute
# (ModuleNotFoundError) and the substance gate can never pass, so no autonomous
# code change can land through OpenShell.
RUN uv sync --frozen --no-editable --extra dev --project /tmp/mac-src \
    && HP="$(/opt/mac-venv/bin/python -c 'import mac,os;print(os.path.join(os.path.dirname(mac.__file__),"_hermes"))')" \
    && SP="$(/opt/mac-venv/bin/python -c 'import site;print(site.getsitepackages()[0])')" \
    && printf '%s\n' "$HP" > "$SP/zz_hermes_vendor.pth" \
    && /opt/mac-venv/bin/python -c "import hermes_cli, mac; print('IMPORT_OK')" \
    && rm -rf /tmp/mac-src

# The executor uploads the task workspace to /sandbox and the upload (ssh+tar)
# runs as the `sandbox` user. The Docker driver creates /sandbox root-owned, so
# make it sandbox-writable or the upload fails ("tar: Cannot mkdir: Permission
# denied").
RUN mkdir -p /sandbox && chown sandbox:sandbox /sandbox

ENV VIRTUAL_ENV=/opt/mac-venv PATH="/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin"
WORKDIR /sandbox
CMD ["python3"]
