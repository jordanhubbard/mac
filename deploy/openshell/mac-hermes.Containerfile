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
# codex: repository-editing agent for confined coding tasks. codegraph: local
# codebase indexing and inspection baseline for agent work.
# bash >=5.2: the explicit task-runtime shell contract.  Do not rely on the
# base image carrying Bash transitively; executor and verification commands
# invoke /bin/bash and deployment fails if its version/features are unsuitable.
# make/node/npm/java/pnpm/lein: common repository contracts. The executor can
# still provision missing tools into a task-local .mac-toolchain, but the base
# image should cover ordinary polyglot repos without mutating the host fleet.
# build-essential: a C/C++ toolchain (cc/gcc/g++) for repos that compile native
#   code (e.g. nanolang's 3-stage `make build`); Debian-slim ships none.
# libssl-dev: OpenSSL headers + libcrypto. nanolang's src/sign.c #includes
#   <openssl/evp.h>/<sha.h>/<err.h> and the build links -lcrypto; without it
#   `make build` fails and a coding agent will destructively stub sign.c just to
#   compile. A real build dependency belongs in the base image.
# nodejs from NodeSource (v22 LTS), NOT Debian's nodejs (v18): current pnpm
#   refuses Node < v22.13 ("This version of pnpm requires at least Node.js
#   v22.13"), which silently breaks every `pnpm install` repo bootstrap.
# sandbox user/group: OpenShell refuses any image lacking a `sandbox` user.
ARG GH_VERSION="2.95.0"
ARG CODEGRAPH_VERSION="v1.1.6"
ARG NODE_VERSION="22.23.1"
ARG PNPM_VERSION="11.13.1"
ARG CODEX_VERSION="0.140.0"
ARG TARGETARCH
COPY .mac-openshell-build-assets /tmp/mac-openshell-build-assets
COPY deploy/verify-bash-contract.sh /usr/local/bin/mac-verify-bash-contract
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl tar xz-utils \
    && chmod 0755 /usr/local/bin/mac-verify-bash-contract \
    && /usr/local/bin/mac-verify-bash-contract \
    && apt-get install -y --no-install-recommends iproute2 iptables git make build-essential libssl-dev openjdk-17-jre-headless \
    && (cd /tmp/mac-openshell-build-assets && sha256sum -c SHA256SUMS) \
    && case "$TARGETARCH" in \
         amd64) asset_arch=amd64; gh_arch=amd64; codegraph_arch=amd64 ;; \
         arm64) asset_arch=arm64; gh_arch=arm64; codegraph_arch=arm64 ;; \
         *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 2 ;; \
       esac \
    && tar -xJf "/tmp/mac-openshell-build-assets/node-${asset_arch}.tar.xz" -C /usr/local --strip-components=1 \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && tar -xzf "/tmp/mac-openshell-build-assets/gh-${asset_arch}.tgz" -C /tmp \
    && install -m755 "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh \
    && rm -rf "/tmp/gh_${GH_VERSION}_linux_${gh_arch}" \
    && npm install -g "@openai/codex@${CODEX_VERSION}" "pnpm@${PNPM_VERSION}" \
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
