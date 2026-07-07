# Stock OpenClaw runtime for MAC's OpenShell-confined chat gateway.
#
# This is deliberately based on the official, release-tagged OpenClaw image by
# immutable multi-arch manifest digest.  MAC adds only the non-root `sandbox`
# identity and durable path layout required by OpenShell; it does not install
# or invoke NemoClaw.
ARG OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:2026.6.11@sha256:3814fb1f62f9cfc5944de088c5817c68c88b5d721feebe36420b666a90a61ce7"
FROM ${OPENCLAW_IMAGE}

ARG OPENCLAW_SLACK_PLUGIN_VERSION="2026.6.11"
ARG MAC_OPENCLAW_IMAGE_REVISION="4"

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system sandbox \
    && useradd --system --gid sandbox --create-home --home-dir /home/sandbox sandbox \
    && install -d -m 0700 -o sandbox -g sandbox \
         /home/sandbox/.config/mac-openclaw \
         /home/sandbox/.openclaw-data \
         /home/sandbox/workspace \
    && install -d -m 0755 -o sandbox -g sandbox /sandbox \
    && printf '%s\n' "${MAC_OPENCLAW_IMAGE_REVISION}" \
         > /etc/mac-openclaw-image-revision

ENV HOME=/home/sandbox \
    OPENCLAW_CONFIG_PATH=/home/sandbox/.config/mac-openclaw/openclaw.json \
    OPENCLAW_STATE_DIR=/home/sandbox/.openclaw-data \
    NODE_ENV=production

WORKDIR /home/sandbox
USER sandbox

# Slack is an official external channel plugin in this OpenClaw release.  Pin
# it to the core version and bake its registry/code into the image; runtime
# config still has to explicitly enable it. Telegram is bundled with core and
# is enabled through the same runtime plugin policy.
RUN /usr/local/bin/openclaw plugins install --pin \
      "npm:@openclaw/slack@${OPENCLAW_SLACK_PLUGIN_VERSION}"

# OpenShell supplies the foreground command.  Keep the official image's tini
# entrypoint so signal forwarding and zombie reaping remain correct.
CMD ["/usr/local/bin/openclaw", "gateway", "run"]
