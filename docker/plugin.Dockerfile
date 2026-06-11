# mac Hermes plugin — packaged as a minimal payload image.
#
# The image just holds /plugin/. hermes-agent's install-mac-plugin init
# container copies its contents into /opt/data/plugins/mac/, then exits.
# The plugin is loaded by the main hermes-agent process at startup.

FROM busybox:1.38.0

WORKDIR /plugin

COPY plugin/plugin.yaml      ./plugin.yaml
COPY plugin/__init__.py      ./__init__.py
COPY plugin/client.py        ./client.py
COPY plugin/manifest.py      ./manifest.py
COPY plugin/schemas.py       ./schemas.py

RUN test -f /plugin/plugin.yaml \
 && test -f /plugin/__init__.py \
 && test -f /plugin/client.py \
 && test -f /plugin/manifest.py \
 && test -f /plugin/schemas.py

CMD ["sh", "-c", "find /plugin -maxdepth 2 -type f | sort"]
