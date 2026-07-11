# Stock OpenClaw runtime for MAC's OpenShell-confined chat gateway.
#
# This is deliberately based on the official, release-tagged OpenClaw image by
# immutable multi-arch manifest digest.  MAC adds the non-root `sandbox`
# identity, durable path layout, and confinement/runtime prerequisites required
# by OpenShell (including the Bash >=5.2 contract); it does not install or
# invoke NemoClaw.
# Build from the MAC repository root so the shared Bash runtime contract can be
# copied into every OpenShell image:
#   docker build -f deploy/openclaw/OpenClaw.Containerfile .
ARG OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:2026.6.11@sha256:3814fb1f62f9cfc5944de088c5817c68c88b5d721feebe36420b666a90a61ce7"
FROM ${OPENCLAW_IMAGE}

ARG OPENCLAW_SLACK_PLUGIN_VERSION="2026.6.11"
ARG MAC_OPENCLAW_IMAGE_REVISION="uncomputed"

USER root
COPY deploy/verify-bash-contract.sh /usr/local/bin/mac-verify-bash-contract
COPY deploy/openclaw/apply-cron-plan.mjs /opt/mac-openclaw/apply-cron-plan.mjs
COPY deploy/openclaw/curiosity-sidecar.py /usr/local/bin/curiosity
COPY deploy/openclaw/plugins/mac-continuity /opt/mac-openclaw/plugins/mac-continuity
# Keep the agent's real home durable. OpenShell command wrappers receive
# explicit no-profile flags and a readable profile, so profile lookup must not
# force session state into a temporary directory.
# bash iproute2: OpenShell runtime prerequisites (Bash >=5.2 contract, netns).
# python3/pip/venv, build-essential, git, curl: a real dev toolchain so the
# chat agent can install packages (pip --user / venv) and compile native/GPU
# code in its sandbox. OpenShell forbids container root, so apt is unavailable
# at runtime — the toolchain must be baked in; runtime installs go to the
# writable /home/sandbox via pip. CUDA userspace comes from GPU-accelerated
# pip wheels (cupy/torch/numba) plus the --gpu-passed driver, so no bulky CUDA
# toolkit is baked in here.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
         bash iproute2 python3 python3-pip python3-venv build-essential git curl ca-certificates \
    && rm -f /usr/lib/python3*/EXTERNALLY-MANAGED \
    && chmod 0755 /usr/local/bin/mac-verify-bash-contract /usr/local/bin/curiosity \
    && /usr/local/bin/mac-verify-bash-contract \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system sandbox \
    && useradd --system --gid sandbox --create-home --home-dir /home/sandbox sandbox \
    && chmod 0755 /home/sandbox \
    && install -d -m 0700 -o sandbox -g sandbox \
         /home/sandbox/.config/mac-openclaw \
         /home/sandbox/.openclaw-data \
         /home/sandbox/workspace \
    && install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.profile \
    && install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.bashrc \
    && chmod 0755 /opt/mac-openclaw/apply-cron-plan.mjs \
    && chmod -R a+rX /opt/mac-openclaw/plugins \
    && install -d -m 0700 -o sandbox -g sandbox \
         /sandbox /sandbox/state /sandbox/workspace \
    && ln -s /sandbox/state /home/sandbox/.openclaw \
    && printf '%s\n' "${MAC_OPENCLAW_IMAGE_REVISION}" \
         > /etc/mac-openclaw-image-revision

ENV HOME=/home/sandbox \
    OPENCLAW_CONFIG_PATH=/home/sandbox/.config/mac-openclaw/openclaw.json \
    OPENCLAW_STATE_DIR=/sandbox/state \
    NODE_ENV=production \
    # Debian marks its system python "externally managed" (PEP 668) and blocks
    # `pip install` without a venv. This IS a throwaway dev sandbox, so let the
    # agent pip install --user into its writable home directly; venvs also work.
    PIP_BREAK_SYSTEM_PACKAGES=1

WORKDIR /home/sandbox
USER sandbox

# Slack is an official external channel plugin in this OpenClaw release.  Pin
# it to the core version and bake its registry/code into the image; runtime
# config still has to explicitly enable it. Telegram is bundled with core and
# is enabled through the same runtime plugin policy.
RUN /bin/bash -c '/usr/local/bin/openclaw plugins install --pin "npm:@openclaw/slack@${OPENCLAW_SLACK_PLUGIN_VERSION}"'

# OpenShell supplies the foreground command.  Keep the official image's tini
# entrypoint so signal forwarding and zombie reaping remain correct.
CMD ["/usr/local/bin/openclaw", "gateway", "run"]
