# mac-hermes sandbox image — the runtime image OpenShell runs the Hermes agent
# inside (`openshell sandbox create --from localhost/mac-hermes:net`).
#
# Multi-arch: python:3.12-slim resolves to the host architecture, so the SAME
# Containerfile builds natively on x86_64 (rocky, bullwinkle) and aarch64
# (natasha / GB10). Build from the mac source tree as context:
#
#   docker build  -t localhost/mac-hermes:net -f deploy/openshell/mac-hermes.Containerfile <mac-src>
#   podman build  -t localhost/mac-hermes:net -f deploy/openshell/mac-hermes.Containerfile <mac-src>
#
# (Use the driver the gateway is configured with — docker for hosts whose podman
# is too old for the system CDI spec version, podman otherwise.)
#
# Hard-won requirements baked in (each line below is load-bearing — see the
# comments): a `sandbox` user/group, `iproute2` (the egress proxy's `ip`), the
# hermes_cli path hook, and a sandbox-writable /sandbox for the docker driver.

FROM docker.io/library/python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# iproute2: OpenShell's network-isolation proxy requires `ip` ("trusted ip
#   helper not found" otherwise). git/curl: task work + git push egress.
# sandbox user/group: OpenShell refuses any image lacking a `sandbox` user.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables git curl ca-certificates \
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
# runs as the `sandbox` user. The docker driver creates /sandbox root-owned, so
# make it sandbox-writable or the upload fails ("tar: Cannot mkdir: Permission
# denied"). Harmless for the podman driver.
RUN mkdir -p /sandbox && chown sandbox:sandbox /sandbox

ENV VIRTUAL_ENV=/opt/mac-venv PATH="/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin"
WORKDIR /sandbox
CMD ["python3"]
