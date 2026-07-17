from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def test_openshell_bootstrap_is_docker_engine_only():
    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    assert 'compute_drivers = ["docker"]' in script
    assert "[openshell.drivers.docker]" in script
    assert '["$OSH_DRIVER"]' not in script
    assert "[openshell.drivers.podman]" not in script
    assert "podman.socket" not in script
    assert "podman --version" not in script
    assert "OSH_DRIVER is no longer supported" in script
    assert "replacing podman-docker compatibility shim" in script
    assert "Podman compatibility shim" in script
    assert (
        "mirroring $OSH_IMAGE_TAG into OpenShell's runtime-visible image store"
        in script
    )
    assert "podman load" in script
    assert (
        "runtime image smoke: Bash >=5.2 plus gh/codex/codegraph visible through OpenShell"
        in script
    )
    assert "run_live_confinement_probe" in script
    assert "live-confinement-probe.sh" in script
    assert '--upload "$probe:/sandbox"' in script
    assert "CONFINEMENT_PROBE_OK" in (
        ROOT / "deploy" / "openshell" / "live-confinement-probe.sh"
    ).read_text(encoding="utf-8")
    assert "MAC_OPENSHELL_GC=1" in script
    assert "MAC_OPENSHELL_STALE_AFTER_SECONDS=86400" in script
    assert "same-version local" in script
    assert script.count("import mac.agent_command") >= 2
    assert script.count("-- /bin/bash -c") >= 2
    assert "-- /bin/bash /sandbox/live-confinement-probe.sh" in script
    assert "installing reviewed openshell CLI" in script
    assert "installing reviewed openshell-gateway" in script
    assert 'uv tool install --force "openshell==$OPENSHELL_VERSION"' not in script
    assert "install_openshell_cli_static" in script
    assert "openshell-x86_64-unknown-linux-musl.tar.gz" in script
    assert "openshell-aarch64-unknown-linux-musl.tar.gz" in script
    assert "using the static musl CLI" not in script
    assert "unsupported unreviewed OPENSHELL_VERSION" in script
    assert "verify_sha256" in script
    assert "openshell-aarch64-apple-darwin.tar.gz" in script
    assert "OSH_CLI_DARWIN_ARM64_SHA256" in script
    assert "OSH_CLI_LINUX_AMD64_SHA256" in script
    assert "OSH_CLI_LINUX_ARM64_SHA256" in script
    assert "OSH_GATEWAY_LINUX_AMD64_SHA256" in script
    assert "OSH_GATEWAY_LINUX_ARM64_SHA256" in script
    assert "--retry 5 --retry-all-errors" in script
    assert "--connect-timeout 15 --max-time 120" in script
    assert "systemctl --user show-environment" in script
    assert "[program:openshell-gateway]" in script
    assert "sudo supervisorctl restart openshell-gateway" in script
    assert "run-gateway.sh" in script
    assert (
        "unset KUBERNETES_SERVICE_HOST KUBERNETES_SERVICE_PORT KUBERNETES_PORT"
        in script
    )
    assert "[program:mac-openshell-firewall]" in script
    assert "chain=MAC_OPENSH_GW" in script
    assert '"$ipt" -A "$chain" -i lo -j RETURN' in script
    assert '"$ipt" -A "$chain" -i "$bridge_iface" -j RETURN' in script
    assert '"$ipt" -A "$chain" -i docker0 -j RETURN' not in script
    assert '"$ipt" -A "$chain" -i \'br+\' -j RETURN' not in script
    assert 'bridge_iface=__OPENSH_BRIDGE_IFACE__' in script
    assert 'network_name="openshell-docker"' in script
    assert 'network create --driver bridge' in script
    assert '[[ "$network_id" =~ ^[0-9a-f]{64}$ ]]' in script
    assert 'bridge_iface="br-${network_id:0:12}"' in script
    assert '"$ipt" -A "$chain" -j DROP' in script
    assert "left mesh interfaces" in script
    assert "sudo systemctl show-environment" in script
    assert "manager=$gateway_manager state=$gateway_state" in script
    assert (
        'OSH_SUPERVISOR_IMAGE="ghcr.io/nvidia/openshell/supervisor@sha256:'
        in script
    )
    assert script.count('supervisor_image = "$OSH_SUPERVISOR_IMAGE"') == 2
    assert "openshell/supervisor:latest" not in script
    assert "MAC_OPENSHELL_UPLOAD_CODEX_AUTH:-0" in script
    assert "rotating OAuth state is not durable in throwaway sandboxes" in script
    create_arg_lines = [
        line
        for line in script.splitlines()
        if 'echo "MAC_OPENSHELL_CREATE_ARGS=' in line
    ]
    assert create_arg_lines
    assert all("--env" not in line and " -- " not in line for line in create_arg_lines)


def test_openshell_image_docs_do_not_advertise_podman_builds():
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")

    assert "docker build" in containerfile
    assert "podman build" not in containerfile
    assert "Docker Engine/Moby" in containerfile


def test_openshell_image_declares_and_verifies_modern_bash_runtime():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")
    contract_path = ROOT / "deploy" / "verify-bash-contract.sh"
    contract = contract_path.read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends bash " in containerfile
    assert (
        "COPY deploy/verify-bash-contract.sh "
        "/usr/local/bin/mac-verify-bash-contract" in containerfile
    )
    assert containerfile.count("/usr/local/bin/mac-verify-bash-contract") >= 2
    assert "node-${asset_arch}.tar.xz" in containerfile
    assert 'test "$(node --version)" = "v${NODE_VERSION}"' in containerfile
    assert "#!/bin/bash" in contract
    assert "BASH_VERSINFO[0]" in contract
    assert "minimum_major=5" in contract
    assert "minimum_minor=2" in contract
    assert "declare -A mac_bash_contract" in contract
    assert "mapfile -t mac_bash_lines" in contract
    assert "MAC_BASH_CONTRACT_OK" in contract
    assert bootstrap.count("-- /bin/bash -c") >= 2
    assert bootstrap.count("/usr/local/bin/mac-verify-bash-contract") >= 2
    assert "set -euo pipefail" in bootstrap


def test_openshell_image_installs_codegraph_baseline():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    builder = (ROOT / "deploy" / "openshell" / "build-runtime-image.sh").read_text(
        encoding="utf-8"
    )
    preparer = (
        ROOT / "deploy" / "openshell" / "prepare-runtime-image-assets.sh"
    ).read_text(encoding="utf-8")
    reviewed_assets = (ROOT / "deploy" / "reviewed-tool-assets.sh").read_text(
        encoding="utf-8"
    )
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")

    assert 'CODEGRAPH_VERSION="${CODEGRAPH_VERSION:-v1.1.6}"' in bootstrap
    assert "prefetching pinned runtime-image assets on the host" in builder
    assert 'REVIEWED_TOOL_ASSETS="$ROOT/deploy/reviewed-tool-assets.sh"' in preparer
    assert "mac_reviewed_asset_spec codegraph Linux x86_64" in preparer
    assert "mac_reviewed_asset_spec codegraph Linux aarch64" in preparer
    assert "codegraph-linux-x64.tar.gz" in reviewed_assets
    assert "codegraph-linux-arm64.tar.gz" in reviewed_assets
    assert 'ARG CODEGRAPH_VERSION="v1.1.6"' in containerfile
    assert "FROM docker.io/library/python@sha256:" in containerfile
    assert "FROM ghcr.io/astral-sh/uv@sha256:" in containerfile
    assert "docker.io/library/python:3.12" not in containerfile
    assert 'ARG NODE_VERSION="22.23.1"' in containerfile
    assert 'ARG PNPM_VERSION="11.13.1"' in containerfile
    assert 'ARG CODEX_VERSION="0.140.0"' in containerfile
    assert '"pnpm@${PNPM_VERSION}"' in containerfile
    assert "npm install -g pnpm" not in containerfile
    assert (
        "COPY .mac-openshell-build-assets /tmp/mac-openshell-build-assets"
        in containerfile
    )
    assert "codegraph-${codegraph_arch}.tgz" in containerfile
    assert (
        'CG_HOME="/usr/local/lib/codegraph/versions/${CODEGRAPH_VERSION}"'
        in containerfile
    )
    assert 'ln -sfn "$CG_HOME" /usr/local/lib/codegraph/current' in containerfile
    assert 'ARG GH_VERSION="2.95.0"' in containerfile
    assert "https://github.com/cli/cli/releases/download/v${GH_VERSION}/" in preparer
    assert "gh_${GH_VERSION}_linux_amd64.tar.gz" in preparer
    assert "gh_${GH_VERSION}_linux_arm64.tar.gz" in preparer
    assert "https://cli.github.com/packages" not in containerfile
    assert "github.com" not in containerfile
    assert "raw.githubusercontent.com" not in containerfile
    assert (
        "chown -R root:root /usr/local/lib/codegraph /usr/local/bin/codegraph"
        in containerfile
    )
    assert "chmod -R a+rX /usr/local/lib/codegraph" in containerfile
    assert "chmod 0755 /usr/local/bin/codegraph" in containerfile
    assert "codegraph install --yes" in containerfile


def test_openshell_supervisor_is_version_matched_and_gateway_is_fail_closed():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    assert (
        "ghcr.io/nvidia/openshell/supervisor@sha256:"
        "80ed9cda5bf672fefdb9dcd4604b40a8b09c0891b6eb9d03e10227c7e3dfb49d"
        in bootstrap
    )
    assert '"$OSH_DOCKER_BIN" run --rm "$OSH_SUPERVISOR_IMAGE" --version' in bootstrap
    assert '"openshell-sandbox $OPENSHELL_VERSION"' in bootstrap
    firewall = bootstrap.index("# --- 7. firewall :17670")
    gateway = bootstrap.index("# --- 8. gateway service + register")
    assert firewall < gateway
    assert "stop_gateway_fail_closed" in bootstrap
    assert "refusing to run the unauthenticated OpenShell gateway without its firewall" in bootstrap
    assert "systemctl restart mac-openshell-firewall.service" in bootstrap
    assert "supervisorctl start mac-openshell-firewall >/dev/null 2>&1 || true" not in bootstrap
    assert (
        'OPENSHELL_LOCAL_GATEWAY_ENDPOINT="http://127.0.0.1:17670"'
        in bootstrap
    )
    assert (
        'OPENSHELL_GATEWAY_ENDPOINT="$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" '
        '"$cli" "$@"' in bootstrap
    )
    sandbox_operations = [
        line
        for line in bootstrap.splitlines()
        if " sandbox create" in line or " sandbox delete" in line
    ]
    assert sandbox_operations
    assert all("openshell_local_gateway" in line for line in sandbox_operations)
    assert 'gateway add --name openshell "$OPENSHELL_LOCAL_GATEWAY_ENDPOINT"' in bootstrap
    assert 'gateway select openshell' in bootstrap
    assert 'gateway list --output json' in bootstrap
    assert '--label mac.owner=mac --label mac.kind=openshell-gateway' in bootstrap
    assert 'matches[0].get("endpoint") != endpoint' in bootstrap
    assert (
        "gateway add http://127.0.0.1:17670 >/dev/null 2>&1 || true"
        not in bootstrap
    )
    assert "gateway select openshell >/dev/null 2>&1 || true" not in bootstrap
    stop_existing = bootstrap.index("stop_gateway_fail_closed\nverify_supervisor_image")
    install_cli = bootstrap.index("# --- 1. openshell CLI")
    assert stop_existing < install_cli
    assert bootstrap.count("clear_repo_update_dispatch_blocker") == 3
    assert "MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE" in bootstrap
    assert 'Path(key).unlink()' in bootstrap


def test_complete_openshell_bootstrap_clears_default_and_configured_dispatch_holds(
    tmp_path,
):
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("clear_repo_update_dispatch_blocker(){")
    end = bootstrap.index('\n}\n\nresolve_deployed_source_revision(){', start) + len("\n}\n")
    helper = bootstrap[start:end]

    mac_home = tmp_path / "mac home"
    override = mac_home / "state" / "custom dispatch hold.json"
    default = mac_home / "repo-update-dispatch-blocked.json"
    override.parent.mkdir(parents=True)
    (mac_home / "venv" / "bin").mkdir(parents=True)
    (mac_home / "venv" / "bin" / "python").symlink_to(sys.executable)
    override.write_text("override\n", encoding="utf-8")
    default.write_text("default\n", encoding="utf-8")
    env_file = mac_home / "mac.env"
    env_file.write_text(
        'MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE="state/custom dispatch hold.json"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["/bin/bash", "-c", helper + "\nclear_repo_update_dispatch_blocker\n"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MAC_HOME": str(mac_home),
            "ENVF": str(env_file),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not default.exists()
    assert not override.exists()


def test_runtime_source_revision_uses_archive_marker_and_rejects_disagreement(
    tmp_path,
):
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("resolve_deployed_source_revision(){")
    end = bootstrap.index('\n}\nexport PATH=', start) + len("\n}\n")
    helper = bootstrap[start:end]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "if [ -n \"${FAKE_GIT_REVISION:-}\" ]; then\n"
        "  printf '%s\\n' \"$FAKE_GIT_REVISION\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    mac_home = tmp_path / "mac-home"
    mac_home.mkdir()
    marker = mac_home / "deployed-source-revision"
    marker.write_text("a" * 40 + "\n", encoding="utf-8")
    base_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MAC_SRC": str(tmp_path / "archive-source"),
        "DEPLOYED_SOURCE_REVISION_FILE": str(marker),
    }

    archive = subprocess.run(
        ["/bin/bash", "-c", helper + "\nresolve_deployed_source_revision\n"],
        check=False,
        capture_output=True,
        text=True,
        env=base_env,
    )
    assert archive.returncode == 0, archive.stderr
    assert archive.stdout.strip() == "a" * 40

    mismatch = subprocess.run(
        ["/bin/bash", "-c", helper + "\nresolve_deployed_source_revision\n"],
        check=False,
        capture_output=True,
        text=True,
        env={**base_env, "FAKE_GIT_REVISION": "b" * 40},
    )
    assert mismatch.returncode != 0
    assert "does not match durable source revision marker" in mismatch.stderr


def test_linux_gateway_firewall_resolves_only_the_owned_docker_bridge(tmp_path):
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("ensure_openshell_docker_bridge() {")
    end = bootstrap.index("\n}\n\nstop_gateway_fail_closed", start) + len("\n}\n")
    helper = bootstrap[start:end]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "network-created"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"$1\" = network ] && [ \"$2\" = inspect ]; then\n"
        "  if [ \"${3:-}\" = --format ]; then\n"
        "    case \"$4\" in\n"
        "      *Driver*) printf '%s\\n' bridge ;;\n"
        "      *Id*) printf '%064d\\n' 0 | tr 0 c ;;\n"
        "      *) exit 2 ;;\n"
        "    esac\n"
        "  else\n"
        "    test -f \"$FAKE_DOCKER_STATE\"\n"
        "  fi\n"
        "elif [ \"$1\" = network ] && [ \"$2\" = create ]; then\n"
        "  : > \"$FAKE_DOCKER_STATE\"\n"
        "else\n"
        "  exit 2\n"
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    ip = fake_bin / "ip"
    ip.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" > \"$FAKE_IP_LOG\"\n"
        "test \"$1\" = link\n"
        "test \"$2\" = show\n"
        "test \"$3\" = br-cccccccccccc\n",
        encoding="utf-8",
    )
    ip.chmod(0o755)
    ip_log = tmp_path / "ip.log"

    completed = subprocess.run(
        ["/bin/bash", "-c", helper + "\nensure_openshell_docker_bridge\n"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "OSH_DOCKER_BIN": str(docker),
            "FAKE_DOCKER_STATE": str(state),
            "FAKE_IP_LOG": str(ip_log),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "br-cccccccccccc"
    assert state.exists()
    assert ip_log.read_text(encoding="utf-8").strip() == (
        "link show br-cccccccccccc"
    )


def test_runtime_publication_verifier_requires_anonymous_digest_readback():
    verifier = (ROOT / "scripts" / "verify-runtime-publication.py").read_text(
        encoding="utf-8"
    )
    assert "mac-openshell-runtime@sha256:" in verifier
    assert 'anonymous_env["DOCKER_CONFIG"] = config' in verifier
    assert '"pull", args.image_ref' in verifier
    assert "org.opencontainers.image.revision" in verifier
    assert "mac.openshell_runtime.publication_verification.v1" in verifier


def test_openshell_image_assets_are_prefetched_and_always_cleaned_up():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "deploy" / "openshell" / "build-runtime-image.sh").read_text(
        encoding="utf-8"
    )
    preparer = (
        ROOT / "deploy" / "openshell" / "prepare-runtime-image-assets.sh"
    ).read_text(encoding="utf-8")

    assert 'IMAGE_ASSET_DIR="$MAC_SRC/.mac-openshell-build-assets"' in script
    assert 'BUILD_LOCK_DIR="$MAC_SRC/.mac-openshell-build.lock"' in script
    assert "acquire_build_lock" in script
    assert 'kill -0 "$owner_pid"' in script
    assert "timed out waiting for OpenShell image-build lock" in script
    assert ".mac-openshell-build.lock" in (ROOT / ".dockerignore").read_text(
        encoding="utf-8"
    )
    assert "prepare-runtime-image-assets.sh" in script
    assert "node-amd64.tar.xz" in preparer
    assert "node-arm64.tar.xz" in preparer
    assert "gh-amd64.tgz" in preparer
    assert "gh-arm64.tgz" in preparer
    assert "raw.githubusercontent.com/technomancy/leiningen/${LEIN_COMMIT}/bin/lein" in preparer
    assert "SHA-256 mismatch" in preparer
    assert "trap cleanup EXIT" in script
    assert '--build-arg "GH_VERSION=$GH_VERSION"' in script
    assert '--build-arg "CODEGRAPH_VERSION=$CODEGRAPH_VERSION"' in script
    assert '--build-arg "NODE_VERSION=$NODE_VERSION"' in script
    assert '--build-arg "PNPM_VERSION=$PNPM_VERSION"' in script
    assert '--build-arg "CODEX_VERSION=$CODEX_VERSION"' in script
    assert '--build-arg "TARGETARCH=$TARGETARCH"' in script
    assert "x86_64|amd64) TARGETARCH=amd64" in script
    assert "aarch64|arm64) TARGETARCH=arm64" in script
    assert 'image_source_sha_file="$OSH_DIR/image-source-sha"' in bootstrap
    assert 'mv -f "$marker_tmp" "$MAC_IMAGE_SOURCE_SHA_FILE"' in script
    assert '/bin/bash "$builder"' in bootstrap


def test_openshell_image_builder_serializes_shared_checkout_builds(tmp_path):
    """Concurrent agents on one host must not delete each other's assets."""
    source = tmp_path / "mac"
    deploy = source / "deploy" / "openshell"
    deploy.mkdir(parents=True)
    (deploy / "mac-hermes.Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    preparer = deploy / "prepare-runtime-image-assets.sh"
    preparer.write_text(
        """#!/bin/bash
set -eu
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then out="$2"; shift 2; else shift; fi
done
mkdir -p "$out"
printf '%s\n' "$PPID" > "$out/owner"
""",
        encoding="utf-8",
    )
    preparer.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/bin/bash
set -eu
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift 2; else shift; fi
done
[ -n "$out" ]
printf '%s\n' "$PPID" > "$out"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    ready = tmp_path / "ready"
    ready.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/bash
set -eu
asset="$MAC_SRC/.mac-openshell-build-assets/owner"
owner="$(cat "$asset")"
[ "$owner" = "$PPID" ]
touch "$MAC_TEST_READY_DIR/$PPID"
sleep 0.25
[ "$(cat "$asset")" = "$owner" ]
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "MAC_SRC": str(source),
        "OSH_DOCKER_BIN": str(fake_docker),
        "MAC_TEST_READY_DIR": str(ready),
        "MAC_OPENSHELL_BUILD_LOCK_POLL_SECONDS": "0.02",
        "MAC_OPENSHELL_BUILD_LOCK_WAIT_SECONDS": "10",
    }
    builder = ROOT / "deploy" / "openshell" / "build-runtime-image.sh"
    first = subprocess.Popen(
        ["/bin/bash", str(builder)], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not any(ready.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(ready.iterdir()), first.communicate(timeout=1)

    second = subprocess.Popen(
        ["/bin/bash", str(builder)], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    first_output = first.communicate(timeout=10)
    second_output = second.communicate(timeout=10)
    assert first.returncode == 0, first_output
    assert second.returncode == 0, second_output
    assert not (source / ".mac-openshell-build-assets").exists()
    assert not (source / ".mac-openshell-build.lock").exists()


def test_openshell_image_installs_dev_extra_for_contract_tests():
    """The task sandbox must carry the [dev] extra (pytest, coverage, …) so the
    repository contract test — scripts/run-contract-tests.sh, which collects the
    full suite — can actually RUN in-sandbox. Without it, in-sandbox
    verification of a repo-coupled code task fails to execute and the substance
    gate can never pass, so no autonomous code change lands through OpenShell."""
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")
    assert "uv sync --frozen --no-editable --extra dev" in containerfile
    assert "COPY pyproject.toml uv.lock README.md /tmp/mac-src/" in containerfile
    assert "COPY src /tmp/mac-src/src" in containerfile
    assert "/tmp/mac-src[dev]" not in containerfile
