# Stock OpenClaw runtime for MAC's OpenShell-confined chat gateway.
#
# This is deliberately based on the official, release-tagged OpenClaw image by
# immutable multi-arch manifest digest.  MAC adds only the non-root `sandbox`
# identity and durable path layout required by OpenShell; it does not install
# or invoke NemoClaw.
ARG OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:2026.6.11@sha256:3814fb1f62f9cfc5944de088c5817c68c88b5d721feebe36420b666a90a61ce7"
FROM ${OPENCLAW_IMAGE}

USER root
RUN groupadd --system sandbox \
    && useradd --system --gid sandbox --create-home --home-dir /home/sandbox sandbox \
    && install -d -m 0700 -o sandbox -g sandbox \
         /home/sandbox/.config/mac-openclaw \
         /home/sandbox/.openclaw-data \
         /home/sandbox/workspace \
    && install -d -m 0755 -o sandbox -g sandbox /sandbox

ENV HOME=/home/sandbox \
    OPENCLAW_CONFIG_PATH=/home/sandbox/.config/mac-openclaw/openclaw.json \
    OPENCLAW_STATE_DIR=/home/sandbox/.openclaw-data \
    NODE_ENV=production

WORKDIR /home/sandbox
USER sandbox

# OpenShell supplies the foreground command.  Keep the official image's tini
# entrypoint so signal forwarding and zombie reaping remain correct.
CMD ["/usr/local/bin/openclaw", "gateway", "run"]
