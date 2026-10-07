# mac-hermes sandbox image — the runtime image OpenShell runs the Hermes agent
# inside (`openshell sandbox create --from localhost/mac-hermes:net`).
#
# Multi-arch: the pinned Python image resolves to the host architecture, so the SAME
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

FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv

FROM docker.io/library/python:3.14.7-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

ENV DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/mac-venv \
    UV_PYTHON_DOWNLOADS=never

# iproute2: OpenShell's network-isolation proxy requires `ip` ("trusted ip
#   helper not found" otherwise). git/curl/gh: task work + git push egress.
# opencode: MAC's coding CLI for confined coding tasks. It MUST resolve by
#   basename through the image-owned PATH (the advertisement/probe contract):
#   the build below gates it with `command -v opencode` plus a pinned
#   `--version` so a missing install, a dangling symlink, or a non-PATH binary
#   fails the build closed instead of shipping an image the in-sandbox probe
#   later rejects as agent_binary_missing.
# opencode: installed from npm, but two details are load-bearing. Its platform
#   binary arrives through an optionalDependency placed by the package's
#   postinstall, and npm >=11 blocks install scripts unless the package is
#   named explicitly -- a silently script-less install leaves a package
#   directory with no runnable binary. Its bin entry is "bin/opencode.exe" on
#   every platform (upstream naming, not a Windows artefact) and npm does not
#   create the PATH symlink for it, so the build links it by hand and then
#   gates on `command -v opencode`.
# claude: Claude Code, MAC's second coding CLI (MAC_CODING_AGENT=claude). Same
#   npm shape as opencode: a native "bin/claude.exe" from a per-platform
#   optionalDependency, an install script that must be allowed by name, no
#   PATH symlink from npm, and a pinned `--version` gate. It reaches the hub's
#   /v1/messages front door with the task's inference token and nothing else.
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
# cmake/ninja-build: isaacsim7-poc@feat/ros-sim's contract requires them. This
#   is the first entry here that was NOT transcribed from an incident: the
#   contract said so and `mac admin sandbox-image bom` read it. Everything above this line
#   is the same fact, learned the expensive way.  Do not hand-edit this list --
#   run `mac admin sandbox-image bom --containerfile` and let the contracts say what belongs.
# libssl-dev: OpenSSL headers + libcrypto. nanolang's src/sign.c #includes
#   <openssl/evp.h>/<sha.h>/<err.h> and the build links -lcrypto; without it
#   `make build` fails and a coding agent will destructively stub sign.c just to
#   compile. A real build dependency belongs in the base image.
# libffi-dev (+ pkg-config): nanolang's src/interpreter_ffi.c #includes <ffi.h>
#   and the build resolves it with `pkg-config --cflags/--libs libffi`. No image
#   ever shipped it, so `make build` died with "ffi.h: No such file or
#   directory" and nanolang could not bootstrap in the sandbox at all. Debian
#   puts ffi.h under the multiarch include dir, so the build gate below
#   compiles against it instead of testing a fixed /usr/include path.
# valgrind, libsdl2{,-image,-mixer,-ttf}-dev, libncurses-dev, libreadline-dev,
#   libevent-dev, libuv1-dev, libbullet-dev, libglfw3-dev, libglew-dev,
#   freeglut3-dev, libutf8proc-dev, libsqlite3-dev, libcurl4-openssl-dev, gforth: the rest of nanolang's own Linux CI
#   package list. nanolang's repository gate runs `make build` and then
#   `make test-quick`, and test-quick is not header-free: test-mixer-callbacks
#   requires `pkg-config --exists SDL2_mixer`, test-glut-init needs GLUT,
#   test-pt2-audio needs SDL audio, and test-forth-gforth-diff diffs against a
#   real gforth. Judging SDL "optional" (#921) left the gate failing in the
#   sandbox with "I require SDL2_mixer development headers for this integration
#   gate." Every name exists on bookworm for amd64 and arm64.
# PyYAML for the image's python3: nanolang's schema-generation step runs
#   `python3` with `import yaml`. The executor runs commands in a login shell,
#   and /etc/profile resets PATH, dropping /opt/mac-venv/bin, so that `python3`
#   is the base image's /usr/local/bin/python3 -- not the venv (which has
#   PyYAML) and not Debian's /usr/bin/python3 (which the apt packages above pull
#   in as a dependency, but which sits later on PATH). Debian's python3-yaml
#   would therefore be invisible to it. Install the same pinned, hash-locked
#   PyYAML wheel uv.lock uses into /usr/local/bin/python3's site-packages.
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
#
# The `.profile` install is not cosmetic. A login shell sources ~/.profile
# before the task's first command, and the skel-provided file is not readable
# by the uid the executor runs as, so every command in the sandbox began with
#     /bin/bash: /home/sandbox/.profile: Permission denied
# after which repository harvest failed and took the task with it. On the
# isaacsim7-poc tree that single fault accounted for 14 of 23 failures -- each
# one misclassified as a WORK failure and retired with retry budget remaining.
# deploy/openclaw/OpenClaw.Containerfile already installs an empty, readable
# .profile for exactly this reason; the image that task sandboxes actually run
# in did not.
ARG GH_VERSION="2.95.0"
ARG NODE_VERSION="22.23.1"
ARG PNPM_VERSION="11.13.1"
ARG OPENCODE_VERSION="1.18.18"
ARG CLAUDE_CODE_VERSION="2.1.292"
ARG BUILDX_VERSION="0.30.1"
ARG RUST_VERSION="1.95.0"
ARG TARGETARCH
COPY .mac-openshell-build-assets /tmp/mac-openshell-build-assets
COPY deploy/verify-bash-contract.sh /usr/local/bin/mac-verify-bash-contract
COPY deploy/verify-rust-contract.sh /usr/local/bin/mac-verify-rust-contract
COPY --from=uv /uv /usr/local/bin/uv
RUN printf '%s\n' 'deb http://deb.debian.org/debian bookworm-backports main' > /etc/apt/sources.list.d/mac-bookworm-backports.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl tar xz-utils \
    && chmod 0755 /usr/local/bin/mac-verify-bash-contract \
    && /usr/local/bin/mac-verify-bash-contract \
    && apt-get install -y --no-install-recommends iproute2 iptables git procps make cmake ninja-build build-essential libssl-dev libffi-dev pkg-config openjdk-17-jre-headless clang llvm lld \
    && apt-get install -y --no-install-recommends valgrind libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev libncurses-dev libreadline-dev libevent-dev libuv1-dev libbullet-dev libglfw3-dev libglew-dev freeglut3-dev libutf8proc-dev libsqlite3-dev libcurl4-openssl-dev gforth \
    && python3 -c "import re,subprocess; v=tuple(map(int,re.search(r'[0-9]+(?:\.[0-9]+)+',subprocess.check_output(['git','version'],text=True)).group().split('.')[:2])); assert v >= (2,38), v" \
    && apt-get install -y --no-install-recommends postgresql postgresql-client \
    && apt-get install -y --no-install-recommends -t bookworm-backports qemu-system-misc \
    && command -v ps >/dev/null \
    && pkg-config --exists libffi \
    && echo '#include <ffi.h>' | cc $(pkg-config --cflags libffi) -fsyntax-only -x c - \
    && pkg-config --exists SDL2_mixer SDL2_image SDL2_ttf sdl2 glfw3 glew libuv libevent sqlite3 libcurl \
    && echo '#include <GL/freeglut.h>' | cc -fsyntax-only -x c - \
    && command -v valgrind >/dev/null \
    && gforth --version \
    && printf '%s\n' 'pyyaml==6.0.3 --hash=sha256:c458b6d084f9b935061bc36216e8a69a7e293a2f1e68bf956dcd9e6cbcd143f5 --hash=sha256:501a031947e3a9025ed4405a168e6ef5ae3126c59f90ce0cd6f2bfc477be31b7' > /tmp/mac-system-pyyaml.txt \
    && PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 /usr/local/bin/python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/mac-system-pyyaml.txt \
    && rm -f /tmp/mac-system-pyyaml.txt \
    && test "$(bash -lc 'command -v python3')" = /usr/local/bin/python3 \
    && bash -lc "python3 -c 'import yaml'" \
    && command -v cmake >/dev/null \
    && command -v ninja >/dev/null \
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
         amd64) asset_arch=amd64; gh_arch=amd64; rust_target=x86_64-unknown-linux-gnu ;; \
         arm64) asset_arch=arm64; gh_arch=arm64; rust_target=aarch64-unknown-linux-gnu ;; \
         *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 2 ;; \
       esac \
    && tar -xJf "/tmp/mac-openshell-build-assets/rust-${asset_arch}.tar.xz" -C /tmp \
    && "/tmp/rust-${RUST_VERSION}-${rust_target}/install.sh" --prefix=/usr/local --disable-ldconfig \
         --components="rustc,rust-std-${rust_target},cargo,rustfmt-preview" \
    && rm -rf "/tmp/rust-${RUST_VERSION}-${rust_target}" \
    && chmod 0755 /usr/local/bin/mac-verify-rust-contract \
    && /usr/local/bin/mac-verify-rust-contract "$RUST_VERSION" \
    && install -d -m0755 /usr/local/lib/docker/cli-plugins /usr/local/libexec/docker/cli-plugins \
    && install -m0755 "/tmp/mac-openshell-build-assets/buildx-${asset_arch}" /usr/local/lib/docker/cli-plugins/docker-buildx \
    && ln -s /usr/local/lib/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx \
    && /usr/local/lib/docker/cli-plugins/docker-buildx version | grep -F "v${BUILDX_VERSION}" \
    && tar -xJf "/tmp/mac-openshell-build-assets/node-${asset_arch}.tar.xz" -C /usr/local --strip-components=1 \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && tar -xzf "/tmp/mac-openshell-build-assets/gh-${asset_arch}.tgz" -C /tmp \
    && install -m755 "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh \
    && rm -rf "/tmp/gh_${GH_VERSION}_linux_${gh_arch}" \
    && npm install -g "pnpm@${PNPM_VERSION}" \
    && npm install -g --allow-scripts=opencode-ai "opencode-ai@${OPENCODE_VERSION}" \
    && ln -sfn /usr/local/lib/node_modules/opencode-ai/bin/opencode.exe /usr/local/bin/opencode \
    && command -v opencode \
    && opencode --version | grep -F "${OPENCODE_VERSION}" \
    && npm install -g --allow-scripts=@anthropic-ai/claude-code "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && ln -sfn /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && command -v claude \
    && DISABLE_AUTOUPDATER=1 claude --version | grep -F "${CLAUDE_CODE_VERSION}" \
    && test "$(pnpm --version)" = "$PNPM_VERSION" \
    && install -m755 /tmp/mac-openshell-build-assets/lein /usr/local/bin/lein \
    && groupadd -r sandbox && useradd -r -g sandbox -m -d /home/sandbox sandbox \
    && install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.profile \
    && chmod 0755 /home/sandbox \
    && rm -rf /tmp/mac-openshell-build-assets \
    && rm -rf /var/lib/apt/lists/*

# pnpm/npm: tune for a constrained L7 egress proxy. A large monorepo install
# (1000+ deps) opens many concurrent TLS connections to the registry; OpenShell's
# deny-by-default egress proxy resets them at high concurrency (UND_ERR_SOCKET /
# ERR_PNPM_META_FETCH_FAIL), and pnpm's release-age supply-chain pass amplifies it
# by fetching metadata for every entry. Cap network concurrency, raise
# retries/timeouts, and disable the release-age check. A world-readable global
# config + env vars so the non-root `sandbox` user (HOME=/tmp) honors it too.
#
# pnpm 11 reads NEITHER /etc/npmrc nor npm_config_* for its own settings -- only
# pnpm_config_* (or its YAML config) -- so every limit below was silently off
# for pnpm until 2026-10-03, when an Aviation gate failed exactly as described
# above (UND_ERR_SOCKET on 1230 supply-chain metadata fetches). The npm_config_*
# forms stay for npm itself. pm_on_fail=ignore: the image ships one pnpm; a repo
# whose packageManager pins another must not make pnpm download a second one.
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
# OpenShell does NOT pass image ENV to sandbox processes (verified 2026-10-03:
# zero pnpm_config_*/npm_config_* inside a sandbox, login shell or not, and the
# sandbox HOME is not writable for a per-user pnpm config.yaml). The ENV below
# therefore only reaches `docker run`. What reaches the sandbox is a file on
# PATH: replace the pnpm/pnpx symlinks with wrappers that default the same
# pnpm_config_* values (a caller's own value still wins) and exec the real
# entry point. The runtime smoke proves this under `env -i`.
RUN for tool in pnpm pnpx; do \
      rm -f "/usr/local/bin/$tool" \
      && printf '%s\n' \
        '#!/bin/sh' \
        ': "${pnpm_config_network_concurrency:=2}" "${pnpm_config_child_concurrency:=2}"' \
        ': "${pnpm_config_fetch_retries:=6}" "${pnpm_config_fetch_retry_mintimeout:=20000}"' \
        ': "${pnpm_config_fetch_retry_maxtimeout:=120000}" "${pnpm_config_fetch_timeout:=300000}"' \
        ': "${pnpm_config_minimum_release_age:=0}" "${pnpm_config_pm_on_fail:=ignore}"' \
        'export pnpm_config_network_concurrency pnpm_config_child_concurrency pnpm_config_fetch_retries' \
        'export pnpm_config_fetch_retry_mintimeout pnpm_config_fetch_retry_maxtimeout pnpm_config_fetch_timeout' \
        'export pnpm_config_minimum_release_age pnpm_config_pm_on_fail' \
        "exec /usr/local/lib/node_modules/pnpm/bin/$tool.mjs \"\$@\"" \
        > "/usr/local/bin/$tool" \
      && chmod 0755 "/usr/local/bin/$tool" || exit 1; \
    done \
    && test "$(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp pnpm config get network-concurrency)" = 2 \
    && test "$(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp pnpm config get minimum-release-age)" = 0
ENV NPM_CONFIG_GLOBALCONFIG=/etc/npmrc \
    npm_config_network_concurrency=2 \
    npm_config_fetch_retries=6 \
    npm_config_fetch_retry_mintimeout=20000 \
    npm_config_fetch_retry_maxtimeout=120000 \
    npm_config_fetch_timeout=300000 \
    npm_config_minimum_release_age=0 \
    pnpm_config_network_concurrency=2 \
    pnpm_config_child_concurrency=2 \
    pnpm_config_fetch_retries=6 \
    pnpm_config_fetch_retry_mintimeout=20000 \
    pnpm_config_fetch_retry_maxtimeout=120000 \
    pnpm_config_fetch_timeout=300000 \
    pnpm_config_minimum_release_age=0 \
    pnpm_config_pm_on_fail=ignore

# Install the mac runtime into the in-image venv. The vendored Hermes lives at
# mac/_hermes/hermes_cli, which `import hermes_cli` only finds if mac/_hermes is
# on sys.path — so drop a .pth that adds it (the executor runs
# `python -m hermes_cli.main chat`).
COPY .python-version pyproject.toml uv.lock README.md /tmp/mac-src/
COPY src /tmp/mac-src/src
# Install the [dev] extra (pytest, coverage, psycopg) so the task
# sandbox can RUN the repository contract test — scripts/run-contract-tests.sh
# collects the full suite, which imports those at collection time. Without it,
# in-sandbox verification of a repo-coupled code task fails to execute
# (ModuleNotFoundError) and the substance gate can never pass, so no autonomous
# code change can land through OpenShell.
# Coding-agent shell tools may install their own PATH while retaining
# /usr/local/bin. Keep the image-owned mac entry point available there instead
# of relying only on the image ENV's /opt/mac-venv/bin prefix.
RUN test "$(python3 --version)" = "Python $(cat /tmp/mac-src/.python-version)" \
    && uv sync --frozen --no-editable --extra dev --project /tmp/mac-src \
    && /opt/mac-venv/bin/python -c "import mac; print('IMPORT_OK')" \
    && ln -sfn /opt/mac-venv/bin/mac /usr/local/bin/mac \
    && command -v mac \
    && mac --version \
    && rm -rf /tmp/mac-src

# The executor uploads the task workspace to /sandbox and the upload (ssh+tar)
# runs as the `sandbox` user. The Docker driver creates /sandbox root-owned, so
# make it sandbox-writable or the upload fails ("tar: Cannot mkdir: Permission
# denied").
RUN mkdir -p /sandbox && chown sandbox:sandbox /sandbox

ENV VIRTUAL_ENV=/opt/mac-venv PATH="/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin"
WORKDIR /sandbox
CMD ["python3"]
