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
# sandbox user/group: OpenShell refuses any image lacking a `sandbox` user.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables git gh make nodejs npm openjdk-17-jre-headless \
    && npm install -g @openai/codex@0.140.0 \
    && npm install -g pnpm \
    && curl -fsSL https://raw.githubusercontent.com/technomancy/leiningen/stable/bin/lein -o /usr/local/bin/lein \
    && chmod +x /usr/local/bin/lein \
    && curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh \
    && if ! command -v codegraph >/dev/null 2>&1; then \
        for candidate in /root/.codegraph/bin/codegraph /root/.local/bin/codegraph /root/.cargo/bin/codegraph /root/bin/codegraph; do \
          if [ -x "$candidate" ]; then ln -sf "$candidate" /usr/local/bin/codegraph; break; fi; \
        done; \
      fi \
    && if [ -x /root/.local/bin/codegraph ]; then rm -f /usr/local/bin/codegraph && cp -L /root/.local/bin/codegraph /usr/local/bin/codegraph; fi \
    && chmod 0755 /usr/local/bin/codegraph \
    && codegraph install --yes \
    && groupadd -r sandbox && useradd -r -g sandbox -m -d /home/sandbox sandbox \
    && rm -rf /var/lib/apt/lists/*

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
