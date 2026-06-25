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

FROM docker.io/library/python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# iproute2: OpenShell's network-isolation proxy requires `ip` ("trusted ip
#   helper not found" otherwise). git/curl/gh: task work + git push egress.
# codex: repository-editing agent for confined coding tasks. codegraph: local
# codebase indexing and inspection baseline for agent work.
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
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh \
    && bash /tmp/nodesource_setup.sh \
    && rm -f /tmp/nodesource_setup.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables git gh make build-essential libssl-dev nodejs openjdk-17-jre-headless \
    && npm install -g @openai/codex@0.140.0 \
    && npm install -g pnpm \
    && curl -fsSL https://raw.githubusercontent.com/technomancy/leiningen/stable/bin/lein -o /usr/local/bin/lein \
    && chmod +x /usr/local/bin/lein \
    && curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh -o /tmp/codegraph-install.sh \
    && CODEGRAPH_INSTALL_DIR=/usr/local/lib/codegraph CODEGRAPH_BIN_DIR=/usr/local/bin sh /tmp/codegraph-install.sh \
    && rm -f /tmp/codegraph-install.sh \
    && CG_BIN="$(readlink -f /usr/local/bin/codegraph)" \
    && CG_HOME="$(dirname "$(dirname "$CG_BIN")")" \
    && printf '#!/bin/sh\nexec "%s/node" --liftoff-only "%s/lib/dist/bin/codegraph.js" "$@"\n' "$CG_HOME" "$CG_HOME" > /usr/local/bin/codegraph \
    && chown -R root:root /usr/local/lib/codegraph /usr/local/bin/codegraph \
    && chmod -R a+rX /usr/local/lib/codegraph \
    && chmod 0755 /usr/local/bin/codegraph \
    && codegraph install --yes \
    && groupadd -r sandbox && useradd -r -g sandbox -m -d /home/sandbox sandbox \
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

RUN python3 -m venv /opt/mac-venv && /opt/mac-venv/bin/pip install --no-cache-dir --upgrade pip

# Install the mac runtime into the in-image venv. The vendored Hermes lives at
# mac/_hermes/hermes_cli, which `import hermes_cli` only finds if mac/_hermes is
# on sys.path — so drop a .pth that adds it (the executor runs
# `python -m hermes_cli.main chat`).
COPY . /tmp/mac-src
RUN /opt/mac-venv/bin/pip install --no-cache-dir /tmp/mac-src \
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
