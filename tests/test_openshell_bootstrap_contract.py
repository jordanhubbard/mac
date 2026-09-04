from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _api_retirement_planner_source() -> str:
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    function = bootstrap.split("retire_managed_sandboxes_via_api() {", 1)[1].split(
        "\n}\n\nretire_managed_sandboxes_via_docker", 1
    )[0]
    return function.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def _api_retirement_function_source() -> str:
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("wait_for_empty_openshell_api_inventory() {")
    end = bootstrap.index("\n\nretire_managed_sandboxes_via_docker()", start)
    return bootstrap[start:end]


def _run_api_retirement_planner(
    tmp_path: Path,
    inventory: list[dict],
    expected_openclaw: str = "mac-openclaw-bullwinkle",
):
    path = tmp_path / "sandbox-inventory.json"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _api_retirement_planner_source(),
            str(path),
            expected_openclaw,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_api_retirement_function(
    tmp_path: Path,
    post_delete_inventories: list[str],
    *,
    hang_list: bool = False,
    retirement_timeout_seconds: int = 1,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    assert post_delete_inventories
    mac_home = tmp_path / "mac-home"
    python_bin = mac_home / "venv" / "bin"
    python_bin.mkdir(parents=True)
    (python_bin / "python").symlink_to(sys.executable)

    initial_inventory = tmp_path / "initial-inventory.json"
    initial_inventory.write_text(
        json.dumps(
            [
                {
                    "name": "mac-task-deadbeef",
                    "phase": "Ready",
                    "labels": {
                        "mac.owner": "mac",
                        "mac.kind": "task",
                        "mac.keep": "false",
                        "mac.pid": "99999999",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    responses = tmp_path / "responses"
    responses.mkdir()
    for index, inventory in enumerate(post_delete_inventories, start=1):
        (responses / str(index)).write_text(inventory, encoding="utf-8")
    operations = tmp_path / "operations.log"
    list_count = tmp_path / "list-count"
    hang_pid = tmp_path / "hang-pid"
    fake_cli = tmp_path / "fake-openshell"
    fake_cli.write_text(
        """#!/bin/bash
set -euo pipefail
[ "${OPENSHELL_GATEWAY_ENDPOINT:-}" = "$EXPECTED_ENDPOINT" ] || exit 97
case "$1:$2" in
  sandbox:delete)
    printf 'delete %s\n' "$3" >> "$OPERATIONS"
    ;;
  sandbox:list)
    count=0
    if [ -f "$LIST_COUNT" ]; then count=$(/bin/cat "$LIST_COUNT"); fi
    count=$((count + 1))
    printf '%s\n' "$count" > "$LIST_COUNT"
    printf 'list\n' >> "$OPERATIONS"
    if [ "$HANG_LIST" = 1 ]; then
      printf '%s\n' "$$" > "$HANG_PID"
      exec /bin/sleep 60
    fi
    response="$RESPONSES/$count"
    if [ ! -f "$response" ]; then response="$LAST_RESPONSE"; fi
    /bin/cat "$response"
    ;;
  *) exit 98 ;;
esac
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    harness = (
        "set -euo pipefail\n"
        "openshell_local_gateway() {\n"
        '  local cli="$1"\n'
        "  shift\n"
        '  OPENSHELL_GATEWAY_ENDPOINT="$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" '
        '"$cli" "$@"\n'
        "}\n"
        "checkpoint_openclaw_with_cli() { return 97; }\n"
        "write_managed_openshell_container_ids() {\n"
        '  printf \'containers %s\\n\' "$1" >> "$OPERATIONS"\n'
        '  : > "$2"\n'
        "}\n"
        'log() { printf \'log %s\\n\' "$*" >> "$OPERATIONS"; }\n'
        + _api_retirement_function_source()
        + '\nretire_managed_sandboxes_via_api "$FAKE_CLI" '
        '"$INITIAL_INVENTORY" "$RETIREMENT_TIMEOUT_SECONDS"\n'
    )
    result = subprocess.run(
        ["/bin/bash", "-c", harness],
        env={
            **os.environ,
            "EXPECTED_ENDPOINT": "http://127.0.0.1:17670",
            "FAKE_CLI": str(fake_cli),
            "HANG_LIST": "1" if hang_list else "0",
            "HANG_PID": str(hang_pid),
            "INITIAL_INVENTORY": str(initial_inventory),
            "LAST_RESPONSE": str(responses / str(len(post_delete_inventories))),
            "LIST_COUNT": str(list_count),
            "MAC_HOME": str(mac_home),
            "OPENSHELL_LOCAL_GATEWAY_ENDPOINT": "http://127.0.0.1:17670",
            "OPERATIONS": str(operations),
            "RETIREMENT_TIMEOUT_SECONDS": str(retirement_timeout_seconds),
            "RESPONSES": str(responses),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    recorded_operations = (
        operations.read_text(encoding="utf-8").splitlines() if operations.exists() else []
    )
    return result, recorded_operations


def _openclaw_promotion_source() -> str:
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("rollback_openclaw_promotion() {")
    end = bootstrap.index("\n\ncheckpoint_openclaw_with_cli()", start)
    return bootstrap[start:end]


def _linux_gateway_ownership_source() -> str:
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("mac_owned_gateway_wrapper() {")
    end = bootstrap.index("\n\nretire_managed_sandboxes_via_api()", start)
    return bootstrap[start:end]


def _gateway_fail_closed_source() -> str:
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("stop_gateway_fail_closed() {")
    end = bootstrap.index("\n\n# A previous deployment", start)
    return bootstrap[start:end]


def test_openshell_bootstrap_is_docker_engine_only():
    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(encoding="utf-8")

    assert 'compute_drivers = ["docker"]' in script
    assert "[openshell.drivers.docker]" in script
    assert '["$OSH_DRIVER"]' not in script
    assert "[openshell.drivers.podman]" not in script
    assert "podman.socket" not in script
    assert "podman --version" not in script
    assert "OSH_DRIVER is no longer supported" in script
    assert "replacing podman-docker compatibility shim" in script
    assert "Podman compatibility shim" in script
    assert "mirroring $OSH_IMAGE_TAG into OpenShell's runtime-visible image store" in script
    assert "podman load" in script
    assert (
        "runtime image smoke: Bash >=5.2 plus gh/codex/claude/cursor-agent/buildx visible through OpenShell"
        in script
    )
    assert (
        script.count("/usr/local/lib/docker/cli-plugins/docker-buildx version | grep -F v0.30.1")
        >= 1
    )
    assert "run_live_confinement_probe" in script
    assert "live-confinement-probe.sh" in script
    assert '--upload "$probe:/sandbox"' in script
    assert "CONFINEMENT_PROBE_OK" in (
        ROOT / "deploy" / "openshell" / "live-confinement-probe.sh"
    ).read_text(encoding="utf-8")
    assert "MAC_OPENSHELL_GC=1" in script
    assert "MAC_OPENSHELL_STALE_AFTER_SECONDS=86400" in script
    # The CLI is reinstalled unconditionally on every bootstrap, so a
    # same-version local replacement cannot survive on version text alone.
    assert "install_openshell_cli_static\n" in script
    # One runtime-image smoke block, not two: the darwin block went away
    # with the macOS Docker path (ADR 0015). Linux is unchanged.
    assert script.count("import mac.agent_command") >= 1
    assert script.count("-- /bin/bash -c") >= 1
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
    # No darwin CLI is resolved here any more: macOS nodes are host
    # installs and never run the OpenShell gateway (ADR 0015).
    assert "apple-darwin" not in script
    assert "OSH_CLI_DARWIN_ARM64_SHA256" not in script
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
    assert "wait_for_local_gateway" in script
    assert "for ((attempt = 1; attempt <= 120; attempt++))" in script
    assert 'openshell_local_gateway "$cli" status' in script
    assert "((attempt == 120)) || sleep 1" in script
    assert "sleep 3" not in script
    assert "unset KUBERNETES_SERVICE_HOST KUBERNETES_SERVICE_PORT KUBERNETES_PORT" in script
    assert "[program:mac-openshell-firewall]" in script
    assert "chain=MAC_OPENSH_GW" in script
    assert '"$ipt" -A "$chain" -i lo -j RETURN' in script
    assert '"$ipt" -A "$chain" -i "$bridge_iface" -j RETURN' in script
    assert '"$ipt" -A "$chain" -i docker0 -j RETURN' not in script
    assert '"$ipt" -A "$chain" -i \'br+\' -j RETURN' not in script
    assert "bridge_iface=__OPENSH_BRIDGE_IFACE__" in script
    assert 'network_name="openshell-docker"' in script
    assert "network create --driver bridge" in script
    assert '[[ "$network_id" =~ ^[0-9a-f]{64}$ ]]' in script
    assert 'bridge_iface="br-${network_id:0:12}"' in script
    assert '"$ipt" -A "$chain" -j DROP' in script
    assert "left mesh interfaces" in script
    assert "sudo systemctl show-environment" in script
    assert "manager=$gateway_manager state=$gateway_state" in script
    assert 'OSH_SUPERVISOR_IMAGE="ghcr.io/nvidia/openshell/supervisor@sha256:' in script
    assert script.count('supervisor_image = "$OSH_SUPERVISOR_IMAGE"') == 1
    assert "openshell/supervisor:latest" not in script
    assert "MAC_OPENSHELL_UPLOAD_CODEX_AUTH:-0" in script
    assert "rotating OAuth state is not durable in throwaway sandboxes" in script
    create_arg_lines = [
        line for line in script.splitlines() if 'echo "MAC_OPENSHELL_CREATE_ARGS=' in line
    ]
    assert create_arg_lines
    assert all("--env" not in line and " -- " not in line for line in create_arg_lines)


def test_bootstrap_pins_the_managed_gateway_endpoint_into_mac_env():
    """Live-found on natasha (2026-09-04): the openshell CLI's own persisted
    "active gateway" selection is local, unrelated state that any other
    process (a NemoClaw pilot, in this case) can silently repoint. Because
    mac-agent's executor never explicitly set OPENSHELL_GATEWAY_ENDPOINT, it
    inherited whatever gateway happened to be selected, and every
    coding-agent sandbox preflight probe (all 5 configured agents) failed
    uniformly with no per-agent credential explanation -- they were all
    quietly hitting the wrong gateway. bootstrap-openshell.sh must pin this
    into mac.env exactly like it already pins its own
    openshell_local_gateway() calls to OPENSHELL_LOCAL_GATEWAY_ENDPOINT, so
    mac-agent's process (which sources mac.env) is immune to that drift."""
    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(encoding="utf-8")
    assert 'OPENSHELL_LOCAL_GATEWAY_ENDPOINT="http://127.0.0.1:17670"' in script
    recipe = script.split("# --- 11. env recipe in mac.env", 1)[1].split(
        "# sanity: mac.env must still source cleanly", 1
    )[0]
    assert 'echo "OPENSHELL_GATEWAY_ENDPOINT=$OPENSHELL_LOCAL_GATEWAY_ENDPOINT"' in recipe
    # The stale-key cleanup sed must also strip a prior run's value so
    # rerunning bootstrap can never leave two conflicting definitions.
    assert "/^OPENSHELL_GATEWAY_ENDPOINT=/d" in script


def test_linux_bootstrap_installs_and_verifies_docker_buildx():
    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(encoding="utf-8")

    assert "ensure_docker_buildx()" in script
    assert "for candidate in docker-buildx docker-buildx-plugin" in script
    assert 'apt-get install -y "$package"' in script
    assert '"$OSH_DOCKER_BIN" buildx version' in script
    assert "ensure_docker_engine\nensure_docker_buildx" in script


def test_openshell_image_docs_do_not_advertise_podman_builds():
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )

    assert "docker build" in containerfile
    assert "podman build" not in containerfile
    assert "Docker Engine/Moby" in containerfile


def test_openshell_image_declares_and_verifies_modern_bash_runtime():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )
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
    # One runtime-image smoke block, not two: the darwin block went away
    # with the macOS Docker path (ADR 0015). Linux is unchanged.
    assert bootstrap.count("-- /bin/bash -c") >= 1
    assert bootstrap.count("/usr/local/bin/mac-verify-bash-contract") >= 1
    assert "set -euo pipefail" in bootstrap


def test_openshell_image_proves_the_riscv_validation_floor() -> None:
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )

    package_install = " ".join(
        line.strip()
        for line in containerfile.splitlines()
        if "apt-get install -y --no-install-recommends" in line
    )
    for package in ("clang", "llvm", "lld", "qemu-system-misc"):
        assert package in package_install.split()
    assert "bookworm-backports main" in containerfile
    assert "-t bookworm-backports qemu-system-misc" in containerfile
    for command in ("clang", "llvm-objcopy", "ld.lld", "qemu-system-riscv64"):
        assert f"command -v {command}" in containerfile
    for probe in (
        "--target=riscv64-unknown-elf",
        "-march=rv64imac",
        "-fuse-ld=lld",
        "llvm-objcopy -O binary",
        "qemu-system-riscv64 -M virt -device help",
    ):
        assert probe in containerfile
    for device in (
        "virtio-gpu-device",
        "virtio-keyboard-device",
        "virtio-mouse-device",
        "virtio-sound-device",
        "virtio-blk-device",
        "virtio-net-device",
    ):
        assert device in containerfile


def test_openshell_image_provides_process_inspection_baseline() -> None:
    """Contract tests may inspect child lifecycle; Debian-slim must not omit ps."""
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )

    package_install = " ".join(
        line.strip()
        for line in containerfile.splitlines()
        if "apt-get install -y --no-install-recommends" in line
    )
    assert "procps" in package_install.split()
    assert "command -v ps >/dev/null" in containerfile


def test_openshell_image_uses_pinned_offline_assets():
    builder = (ROOT / "deploy" / "openshell" / "build-runtime-image.sh").read_text(encoding="utf-8")
    preparer = (ROOT / "deploy" / "openshell" / "prepare-runtime-image-assets.sh").read_text(
        encoding="utf-8"
    )
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )

    assert "prefetching pinned runtime-image assets on the host" in builder
    assert 'REVIEWED_TOOL_ASSETS="$ROOT/deploy/reviewed-tool-assets.sh"' in preparer
    assert "FROM docker.io/library/python@sha256:" in containerfile
    assert "FROM ghcr.io/astral-sh/uv@sha256:" in containerfile
    assert "docker.io/library/python:3.12" not in containerfile
    assert 'ARG NODE_VERSION="22.23.1"' in containerfile
    assert 'ARG PNPM_VERSION="11.13.1"' in containerfile
    assert 'ARG CODEX_VERSION="0.140.0"' in containerfile
    assert 'ARG CLAUDE_VERSION="2.1.220"' in containerfile
    assert 'ARG CURSOR_VERSION="2026.07.23-e383d2b"' in containerfile
    assert '"pnpm@${PNPM_VERSION}"' in containerfile
    assert "claude-${asset_arch}.tgz" in containerfile
    assert "cursor-${asset_arch}.tgz" in containerfile
    assert "npm install -g pnpm" not in containerfile
    assert "COPY .mac-openshell-build-assets /tmp/mac-openshell-build-assets" in containerfile
    assert 'ARG GH_VERSION="2.95.0"' in containerfile
    assert "https://github.com/cli/cli/releases/download/v${GH_VERSION}/" in preparer
    assert "gh_${GH_VERSION}_linux_amd64.tar.gz" in preparer
    assert "gh_${GH_VERSION}_linux_arm64.tar.gz" in preparer
    assert "https://cli.github.com/packages" not in containerfile
    assert "github.com" not in containerfile
    assert "raw.githubusercontent.com" not in containerfile


def test_runtime_image_proves_all_three_coding_clis_resolve_on_path() -> None:
    """The reconciled advertisement/probe contract requires every coding-agent
    CLI (``codex``, ``claude``, ``cursor-agent``) to resolve by basename through
    the image-owned PATH. The Containerfile build MUST gate each with
    ``command -v <basename>`` plus a pinned ``--version`` so a missing install,
    a dangling symlink, or a non-PATH binary fails the build closed instead of
    shipping an image the in-sandbox probe later rejects as
    ``agent_binary_missing`` (the reported cursor-agent-not-on-PATH case)."""
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )

    # Basename PATH-resolution proof for all three CLIs plus the cursor `agent`
    # alias, so build-time catches an unlinked/host-only binary.
    for basename in ("codex", "claude", "cursor-agent", "agent"):
        assert f"command -v {basename}" in containerfile, basename

    # Pinned version proof for all three CLIs (codex previously had none).
    assert 'codex --version | grep -F "${CODEX_VERSION}"' in containerfile
    assert 'claude --version | grep -F "${CLAUDE_VERSION}"' in containerfile
    assert 'cursor-agent --version | grep -F "${CURSOR_VERSION}"' in containerfile


def test_runtime_image_smoke_checks_catch_missing_cursor_agent_on_path() -> None:
    """Every runtime-image smoke check (enforcement-mode on Docker Desktop and
    the Linux gateway smoke) must probe all three coding CLIs with path + version
    + a minimal non-mutating ``--version`` invocation. This is what would catch
    a regression that drops ``cursor-agent`` from the image-owned PATH while
    preserving the fail-closed posture for both static and fungible classes."""
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    smoke_lines = [
        line
        for line in bootstrap.splitlines()
        if "-- /bin/bash -c" in line
        and "mac-verify-bash-contract" in line
        and "command -v cursor-agent" in line
    ]
    # One runtime-image smoke block, not two: the darwin block went away
    # with the macOS Docker path (ADR 0015). Linux is unchanged.
    assert len(smoke_lines) >= 1, smoke_lines
    for line in smoke_lines:
        for basename in ("codex", "claude", "cursor-agent"):
            assert f"command -v {basename}" in line, (basename, line)
        assert "codex --version" in line
        assert "claude --version | grep -F 2.1.220" in line
        assert "cursor-agent --version | grep -F 2026.07.23-e383d2b" in line


def test_openshell_supervisor_is_version_matched_and_gateway_is_fail_closed():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    assert (
        "ghcr.io/nvidia/openshell/supervisor@sha256:"
        "80ed9cda5bf672fefdb9dcd4604b40a8b09c0891b6eb9d03e10227c7e3dfb49d" in bootstrap
    )
    assert '"$OSH_DOCKER_BIN" run --rm "$OSH_SUPERVISOR_IMAGE" --version' in bootstrap
    assert '"openshell-sandbox $OPENSHELL_VERSION"' in bootstrap
    assert '"$OSH_DOCKER_BIN" cp "$container_id:$entrypoint" "$extracted"' in bootstrap
    assert 'install -m700 "$extracted" "$MAC_HOME/bin/openshell-sandbox"' in bootstrap
    assert "if kind == 3:  # PT_INTERP" in bootstrap
    assert "reviewed OpenShell supervisor is not statically linked" in bootstrap
    firewall = bootstrap.index("# --- 7. firewall :17670")
    gateway = bootstrap.index("# --- 8. gateway service + register")
    assert firewall < gateway
    assert "stop_gateway_fail_closed" in bootstrap
    assert "refusing to run the unauthenticated OpenShell gateway without its firewall" in bootstrap
    assert "systemctl restart mac-openshell-firewall.service" in bootstrap
    assert "supervisorctl start mac-openshell-firewall >/dev/null 2>&1 || true" not in bootstrap
    assert 'OPENSHELL_LOCAL_GATEWAY_ENDPOINT="http://127.0.0.1:17670"' in bootstrap
    assert 'OPENSHELL_GATEWAY_ENDPOINT="$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" "$cli" "$@"' in bootstrap
    sandbox_operations = [
        line
        for line in bootstrap.splitlines()
        if " sandbox create" in line or " sandbox delete" in line
    ]
    assert sandbox_operations
    assert all("openshell_local_gateway" in line for line in sandbox_operations)
    assert 'gateway add --name openshell "$OPENSHELL_LOCAL_GATEWAY_ENDPOINT"' in bootstrap
    assert "gateway select openshell" in bootstrap
    assert "gateway list --output json" in bootstrap
    assert "--label mac.owner=mac --label mac.kind=openshell-gateway" in bootstrap
    assert 'matches[0].get("endpoint") != endpoint' in bootstrap
    assert "gateway add http://127.0.0.1:17670 >/dev/null 2>&1 || true" not in bootstrap
    assert "gateway select openshell >/dev/null 2>&1 || true" not in bootstrap
    linux = bootstrap.index("ensure_docker_engine")
    retirement = bootstrap.index("retire_managed_sandboxes_before_upgrade || exit $?", linux)
    stop_existing = bootstrap.index("stop_gateway_fail_closed\nverify_supervisor_image", retirement)
    install_cli = bootstrap.index("# --- 1. openshell CLI")
    assert retirement < stop_existing < install_cli
    # One runtime-image smoke block, not two: the darwin block went away
    # with the macOS Docker path (ADR 0015). Linux is unchanged.
    assert bootstrap.count("clear_repo_update_dispatch_blocker") == 2
    assert "MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE" in bootstrap
    assert "Path(key).unlink()" in bootstrap


def test_macos_bootstrap_is_a_host_install_with_no_container_runtime():
    """macOS nodes take no container path at all (ADR 0015).

    This replaces the Docker-Desktop gateway bootstrap: there is no gateway
    container to own, replace or protect on darwin, because there is no
    container runtime. The exit is a *success* -- a macOS node with no
    OpenShell is correctly provisioned, not broken -- so a deploy does not
    fail on a platform that is no longer expected to have Docker.
    """

    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    assert "bootstrap_darwin" not in bootstrap
    assert "remove_existing_owned_macos_gateway" not in bootstrap
    assert "openshell-gw" in bootstrap  # the Linux gateway is untouched

    darwin_entry = bootstrap.index('if [ "$(uname -s)" = "Darwin" ]; then')
    branch = bootstrap[darwin_entry : bootstrap.index("\nfi\n", darwin_entry)]
    assert "exit 0" in branch
    assert "macos_host" in branch
    assert "Docker" not in branch
    # Nothing Linux-only may run before the darwin exit.
    assert bootstrap.index("install_docker_engine() {") > darwin_entry


def test_linux_gateway_stops_require_exact_mac_manager_ownership(tmp_path):
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    schema_stop = bootstrap.split("stop_existing_gateway_for_schema_recovery() {", 1)[1].split(
        "\n}\n\nretire_managed_sandboxes_via_api", 1
    )[0]
    fail_closed = _gateway_fail_closed_source()
    assert schema_stop.index("require_owned_gateway_manager_definitions") < schema_stop.index(
        "systemctl --user stop openshell-gateway.service"
    )
    assert schema_stop.index("require_owned_gateway_manager_definitions") < schema_stop.index(
        "sudo supervisorctl stop openshell-gateway"
    )
    assert fail_closed.index("mac_owned_systemd_gateway") < fail_closed.index(
        "systemctl --user stop openshell-gateway.service"
    )
    assert fail_closed.index("mac_owned_supervisord_gateway") < fail_closed.index(
        "sudo supervisorctl stop openshell-gateway"
    )
    linux_entry = bootstrap.index('if [ "$(uname -s)" = "Darwin" ]; then')
    initial_preflight = bootstrap.index(
        "require_owned_gateway_manager_definitions || exit $?", linux_entry
    )
    first_linux_mutation = bootstrap.index('mkdir -p "$OSH_DIR" "$BIN"', linux_entry)
    assert initial_preflight < first_linux_mutation
    manager_install = bootstrap.index("# --- 8. gateway service + register")
    replacement_preflight = bootstrap.index(
        "require_owned_gateway_manager_definitions || exit $?", manager_install
    )
    systemd_replacement = bootstrap.index(
        'cat > "$HOME/.config/systemd/user/openshell-gateway.service"',
        manager_install,
    )
    supervisor_replacement = bootstrap.index(
        'sudo tee "$OSH_GATEWAY_SUPERVISOR_CONFIG"', manager_install
    )
    assert replacement_preflight < systemd_replacement
    assert replacement_preflight < supervisor_replacement
    assert "Environment=MAC_OPENSH_GATEWAY_OWNER=mac" in bootstrap
    assert 'MAC_OPENSH_GATEWAY_OWNER="mac"' in bootstrap

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    manager_log = tmp_path / "manager.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$MANAGER_LOG"
case " $* " in
  *" --user cat openshell-gateway.service "*)
    [ "${SYSTEMD_PRESENT:-1}" = 1 ]
    ;;
  *" --property=ExecStart --value "*)
    printf '%s\n' "${SYSTEMD_EXEC_START:-}"
    ;;
  *" --property=Environment --value "*)
    printf '%s\n' "${SYSTEMD_ENVIRONMENT:-}"
    ;;
  *" --property=FragmentPath --value "*)
    printf '%s\n' "${SYSTEMD_FRAGMENT_PATH:-}"
    ;;
  *" --user stop openshell-gateway.service "*) exit 0 ;;
  *" --user is-active --quiet openshell-gateway.service "*) exit 3 ;;
  *) exit 97 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/bin/sh
printf 'sudo %s\n' "$*" >> "$MANAGER_LOG"
case " $* " in
  *" supervisorctl status openshell-gateway "*) printf '%s\n' 'openshell-gateway RUNNING' ;;
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf '%s\\n' Linux\n", encoding="utf-8")
    uname.chmod(0o755)
    pgrep = fake_bin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    pgrep.chmod(0o755)

    home = tmp_path / "home"
    osh_dir = home / ".mac" / "openshell"
    osh_dir.mkdir(parents=True)
    supervisor_config = tmp_path / "openshell-gateway.conf"
    unrelated_exec = (
        "{ path=/opt/acme/openshell-gateway ; "
        "argv[]=/opt/acme/openshell-gateway --config /opt/acme/gateway.toml ; "
        "ignore_errors=no ; }"
    )
    helper = _linux_gateway_ownership_source()
    schema_harness = helper + "\nstop_existing_gateway_for_schema_recovery\n"
    fail_closed_harness = helper + "\n" + fail_closed + "\nstop_gateway_fail_closed\n"
    install_harness = (
        helper
        + "\nrequire_owned_gateway_manager_definitions || exit $?\n"
        + "printf '%s\\n' overwritten > \"$TARGET_DEFINITION\"\n"
    )
    base_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "MAC_HOME": str(home / ".mac"),
        "OSH_DIR": str(osh_dir),
        "BIN": str(home / ".local" / "bin"),
        "OSH_GATEWAY_SUPERVISOR_CONFIG": str(supervisor_config),
        "MANAGER_LOG": str(manager_log),
        "SYSTEMD_EXEC_START": unrelated_exec,
    }

    systemd_definition = tmp_path / "openshell-gateway.service"
    original_systemd_definition = (
        b"[Service]\nExecStart=/opt/acme/openshell-gateway --config /opt/acme/gateway.toml\n"
    )
    systemd_definition.write_bytes(original_systemd_definition)
    manager_log.write_text("", encoding="utf-8")
    blocked_systemd_install = subprocess.run(
        ["/bin/bash", "-c", install_harness],
        env={
            **base_env,
            "SYSTEMD_PRESENT": "1",
            "TARGET_DEFINITION": str(systemd_definition),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked_systemd_install.returncode != 0
    assert "unowned systemd service" in blocked_systemd_install.stderr
    assert systemd_definition.read_bytes() == original_systemd_definition
    assert "systemctl --user stop" not in manager_log.read_text(encoding="utf-8")

    manager_log.write_text("", encoding="utf-8")
    unowned_systemd = subprocess.run(
        ["/bin/bash", "-c", schema_harness],
        env={**base_env, "SYSTEMD_PRESENT": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert unowned_systemd.returncode != 0
    assert "unowned systemd service" in unowned_systemd.stderr
    assert "systemctl --user stop" not in manager_log.read_text(encoding="utf-8")

    unrelated_supervisor_definition = (
        "[program:openshell-gateway]\n"
        "command=/opt/acme/openshell-gateway --config /opt/acme/gateway.toml\n"
        "directory=/opt/acme\n"
    )
    supervisor_config.write_text(unrelated_supervisor_definition, encoding="utf-8")
    manager_log.write_text("", encoding="utf-8")
    blocked_supervisor_install = subprocess.run(
        ["/bin/bash", "-c", install_harness],
        env={
            **base_env,
            "SYSTEMD_PRESENT": "0",
            "TARGET_DEFINITION": str(supervisor_config),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked_supervisor_install.returncode != 0
    assert "unowned supervisord service" in blocked_supervisor_install.stderr
    assert supervisor_config.read_text(encoding="utf-8") == (unrelated_supervisor_definition)
    assert "sudo supervisorctl stop" not in manager_log.read_text(encoding="utf-8")

    manager_log.write_text("", encoding="utf-8")
    unowned_supervisord = subprocess.run(
        ["/bin/bash", "-c", schema_harness],
        env={**base_env, "SYSTEMD_PRESENT": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert unowned_supervisord.returncode != 0
    assert "unowned supervisord service" in unowned_supervisord.stderr
    assert "sudo supervisorctl stop" not in manager_log.read_text(encoding="utf-8")

    manager_log.write_text("", encoding="utf-8")
    cleanup = subprocess.run(
        ["/bin/bash", "-c", fail_closed_harness],
        env={**base_env, "SYSTEMD_PRESENT": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert cleanup.returncode == 0, cleanup.stderr
    calls = manager_log.read_text(encoding="utf-8")
    assert "systemctl --user stop" not in calls
    assert "sudo supervisorctl stop" not in calls
    assert "left unowned systemd service" in cleanup.stderr
    assert "left unowned supervisord service" in cleanup.stderr

    wrapper = osh_dir / "run-gateway.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{base_env["BIN"]}/openshell-gateway" '
        f'--config "{osh_dir}/gateway.toml"\n',
        encoding="utf-8",
    )
    supervisor_config.write_text(
        f"[program:openshell-gateway]\ncommand={wrapper}\ndirectory={osh_dir}\n",
        encoding="utf-8",
    )
    manager_log.write_text("", encoding="utf-8")
    owned = subprocess.run(
        ["/bin/bash", "-c", fail_closed_harness],
        env={
            **base_env,
            "SYSTEMD_PRESENT": "1",
            "SYSTEMD_EXEC_START": (f"{{ path={wrapper} ; argv[]={wrapper} ; ignore_errors=no ; }}"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert owned.returncode == 0, owned.stderr
    calls = manager_log.read_text(encoding="utf-8")
    assert "systemctl --user stop openshell-gateway.service" in calls
    assert "sudo supervisorctl stop openshell-gateway" in calls


def test_api_readable_upgrade_retires_only_ready_owned_dead_pid_sandboxes(
    tmp_path,
):
    dead_pid = "99999999"
    eligible = {
        "name": "mac-task-deadbeef",
        "phase": "Ready",
        "labels": {
            "mac.owner": "mac",
            "mac.kind": "task",
            "mac.keep": "false",
            "mac.pid": dead_pid,
        },
    }
    result = _run_api_retirement_planner(tmp_path, [eligible])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "disposable\tmac-task-deadbeef"

    # API-visible ownership metadata permits cleanup of a Ready orphan even if
    # its supervisor container has not exited yet. A live creator PID remains a
    # hard safety boundary.
    live = {**eligible, "labels": {**eligible["labels"], "mac.pid": str(os.getpid())}}
    result = _run_api_retirement_planner(tmp_path, [live])
    assert result.returncode != 0
    assert "live managed sandbox creator blocks" in result.stderr

    for unsafe in (
        {**eligible, "phase": "Stopped"},
        {**eligible, "labels": {**eligible["labels"], "mac.keep": "true"}},
        {**eligible, "labels": {**eligible["labels"], "mac.owner": "someone-else"}},
        {**eligible, "labels": {**eligible["labels"], "mac.pid": ""}},
        {**eligible, "labels": {**eligible["labels"], "mac.kind": "certifier"}},
        {
            "name": "mac-cert-retained",
            "phase": "Ready",
            "labels": {"mac.owner": "mac", "mac.kind": "certifier"},
        },
    ):
        result = _run_api_retirement_planner(tmp_path, [unsafe])
        assert result.returncode != 0

    openclaw = {
        "name": "mac-openclaw-bullwinkle",
        "phase": "Ready",
        "labels": {"mac.role": "openclaw-gateway"},
    }
    result = _run_api_retirement_planner(tmp_path, [openclaw])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "openclaw\tmac-openclaw-bullwinkle"

    for impersonator in (
        {
            "name": "mac-openclaw-bullwinkle-copy",
            "phase": "Ready",
            "labels": {"mac.role": "openclaw-gateway"},
        },
        {
            "name": "mac-openclaw-bullwinkle",
            "phase": "Ready",
            "labels": {},
        },
    ):
        result = _run_api_retirement_planner(tmp_path, [impersonator])
        assert result.returncode != 0


def test_api_retirement_waits_for_inventory_to_converge(tmp_path):
    result, operations = _run_api_retirement_function(
        tmp_path,
        [json.dumps([{"name": "mac-task-deadbeef"}]), "[]"],
        retirement_timeout_seconds=2,
    )

    assert result.returncode == 0, result.stderr
    assert operations == [
        "delete mac-task-deadbeef",
        "log requested pre-upgrade retirement of managed sandbox mac-task-deadbeef",
        "list",
        "list",
        "log OpenShell API confirmed the pre-upgrade sandbox inventory is empty",
        "containers all",
    ]


def test_api_retirement_rejects_malformed_post_delete_inventory(tmp_path):
    for index, inventory in enumerate(
        ("not-json", '{"unexpected": "object-not-list"}'),
    ):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        result, operations = _run_api_retirement_function(case_dir, [inventory])

        assert result.returncode != 0
        assert "malformed post-retirement OpenShell inventory" in result.stderr
        assert "ERROR: malformed post-retirement OpenShell API inventory" in result.stderr
        assert operations == [
            "delete mac-task-deadbeef",
            "log requested pre-upgrade retirement of managed sandbox mac-task-deadbeef",
            "list",
        ]


@pytest.mark.process_e2e
def test_api_retirement_times_out_while_inventory_remains_nonempty(tmp_path):
    result, operations = _run_api_retirement_function(
        tmp_path,
        [json.dumps([{"name": "mac-task-deadbeef"}])],
    )

    assert result.returncode != 0
    assert (
        "OpenShell API inventory did not become empty before the 1-second "
        "retirement deadline" in result.stderr
    )
    assert "ERROR: timed out waiting for OpenShell API inventory retirement" in (result.stderr)
    assert operations.count("list") >= 2
    assert "containers all" not in operations


@pytest.mark.process_e2e
def test_api_retirement_kills_a_hung_inventory_call_at_the_deadline(tmp_path):
    result, operations = _run_api_retirement_function(
        tmp_path,
        ["[]"],
        hang_list=True,
    )

    assert result.returncode != 0
    assert "inventory call exceeded its bounded wait" in result.stderr
    assert "ERROR: timed out waiting for OpenShell API inventory retirement" in (result.stderr)
    assert operations == [
        "delete mac-task-deadbeef",
        "log requested pre-upgrade retirement of managed sandbox mac-task-deadbeef",
        "list",
    ]
    hung_pid = int((tmp_path / "hang-pid").read_text(encoding="utf-8"))
    try:
        os.kill(hung_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"hung OpenShell inventory process {hung_pid} survived")


def test_openclaw_checkpoint_promotion_rolls_back_an_interrupted_pair(tmp_path):
    mac_home = tmp_path / "mac home"
    openclaw = mac_home / "openclaw"
    recovered = tmp_path / "recovered"
    osh_dir = mac_home / "openshell"
    for root, marker in (
        (openclaw / "workspace", "old-workspace"),
        (openclaw / "state", "old-state"),
        (recovered / "workspace", "new-workspace"),
        (recovered / "state", "new-state"),
    ):
        root.mkdir(parents=True, exist_ok=True)
        (root / "marker.txt").write_text(marker, encoding="utf-8")
    osh_dir.mkdir(parents=True)

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    mock_mv = mock_bin / "mv"
    mock_mv.write_text(
        """#!/bin/bash
last=$((${#@} - 1))
before=$((${#@} - 2))
args=("$@")
src="${args[$before]}"
dst="${args[$last]}"
if [ "${FAIL_PROMOTION_STATE_INSTALL:-0}" = 1 ] \\
    && [[ "$src" == */.upgrade-staging-*/state ]] \\
    && [[ "$dst" == */openclaw/state ]]; then
  exit 91
fi
exec /bin/mv "$@"
""",
        encoding="utf-8",
    )
    mock_mv.chmod(0o755)

    harness = (
        _openclaw_promotion_source()
        + "\nlog() { :; }\n"
        + 'if promote_recovered_openclaw_state "$RECOVERED" '
        + '"mac-openclaw-test" "test"; then exit 90; fi\n'
    )
    env = {
        **os.environ,
        "MAC_HOME": str(mac_home),
        "OSH_DIR": str(osh_dir),
        "RECOVERED": str(recovered),
        "FAIL_PROMOTION_STATE_INSTALL": "1",
        "PATH": str(mock_bin) + ":/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", harness],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (openclaw / "workspace" / "marker.txt").read_text() == "old-workspace"
    assert (openclaw / "state" / "marker.txt").read_text() == "old-state"
    assert (recovered / "workspace" / "marker.txt").read_text() == "new-workspace"
    assert (recovered / "state" / "marker.txt").read_text() == "new-state"


def test_schema_fallback_requires_stopped_exact_managed_containers():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    fallback = bootstrap.split("retire_managed_sandboxes_before_upgrade() {", 1)[1].split(
        "\n}\n\nrun_live_confinement_probe", 1
    )[0]
    first_quiescence = fallback.index("validate_managed_container_quiescence")
    stop_gateway = fallback.index("stop_existing_gateway_for_schema_recovery", first_quiescence)
    second_quiescence = fallback.index(
        "validate_managed_container_quiescence", first_quiescence + 1
    )
    docker_recovery = fallback.index("retire_managed_sandboxes_via_docker", second_quiescence)
    assert first_quiescence < stop_gateway < second_quiescence < docker_recovery

    direct = bootstrap.split("retire_managed_sandboxes_via_docker() {", 1)[1].split(
        "\n}\n\nretire_managed_sandboxes_before_upgrade", 1
    )[0]
    assert "exited|created|dead" in direct
    assert "refusing direct recovery of non-quiescent OpenShell container" in direct
    inventory_writer = bootstrap.split("write_managed_openshell_container_ids() {", 1)[1].split(
        "\n}\n\nvalidate_managed_container_quiescence", 1
    )[0]
    assert "openshell.ai/managed-by=openshell" in inventory_writer
    assert "openshell.ai/sandbox-name" in direct
    assert "^mac-(task|hubverify|codingcap|runtime-smoke|security-probe)-[A-Za-z0-9._-]+$" in direct
    assert 'sandbox_name" = "$expected_openclaw' in direct
    checkpoint = direct.index("checkpoint_openclaw_with_docker")
    exact_remove = direct.index('"$OSH_DOCKER_BIN" rm "$container_id"')
    assert checkpoint < exact_remove
    assert '"$OSH_DOCKER_BIN" rm -f "$container_id"' not in direct

    api = bootstrap.split("retire_managed_sandboxes_via_api() {", 1)[1].split(
        "\n}\n\nretire_managed_sandboxes_via_docker", 1
    )[0]
    api_wait = bootstrap.split("wait_for_empty_openshell_api_inventory() {", 1)[1].split(
        "\n}\n\nretire_managed_sandboxes_via_api", 1
    )[0]
    assert '[cli, "sandbox", "list", "--limit", "1000", "--output", "json"]' in (api_wait)
    assert "time.monotonic()" in api_wait
    assert "start_new_session=True" in api_wait
    assert "os.killpg(process.pid, signal.SIGTERM)" in api_wait
    assert "process.wait(timeout=min(5.0, remaining))" in api_wait
    assert "wait_for_empty_openshell_api_inventory" in api
    assert 'str(item.get("phase") or "").strip().lower() != "ready"' in api
    assert 'labels.get("mac.owner") == "mac"' in api
    assert 'labels.get("mac.keep") or ""' in api
    assert '"certifier"' not in api.split("disposable_patterns =", 1)[1].split("}", 1)[0]
    assert "pid = int(raw_pid)" in api
    assert "os.kill(pid, 0)" in api

    retirement = bootstrap.split("retire_managed_sandboxes_before_upgrade() {", 1)[1].split(
        "\n}\n\nrun_live_confinement_probe", 1
    )[0]
    assert retirement.count("sandbox list --limit 1000 --output json") == 1


def test_complete_openshell_bootstrap_clears_default_and_configured_dispatch_holds(
    tmp_path,
):
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    start = bootstrap.index("clear_repo_update_dispatch_blocker(){")
    end = bootstrap.index("\n}\n\nresolve_deployed_source_revision(){", start) + len("\n}\n")
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
    end = bootstrap.index("\n}\nexport PATH=", start) + len("\n}\n")
    helper = bootstrap[start:end]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ -n "${FAKE_GIT_REVISION:-}" ]; then\n'
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
        'if [ "$1" = network ] && [ "$2" = inspect ]; then\n'
        '  if [ "${3:-}" = --format ]; then\n'
        '    case "$4" in\n'
        "      *Driver*) printf '%s\\n' bridge ;;\n"
        "      *Id*) printf '%064d\\n' 0 | tr 0 c ;;\n"
        "      *) exit 2 ;;\n"
        "    esac\n"
        "  else\n"
        '    test -f "$FAKE_DOCKER_STATE"\n'
        "  fi\n"
        'elif [ "$1" = network ] && [ "$2" = create ]; then\n'
        '  : > "$FAKE_DOCKER_STATE"\n'
        "else\n"
        "  exit 2\n"
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    ip = fake_bin / "ip"
    ip.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" > "$FAKE_IP_LOG"\n'
        'test "$1" = link\n'
        'test "$2" = show\n'
        'test "$3" = br-cccccccccccc\n',
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
    assert ip_log.read_text(encoding="utf-8").strip() == ("link show br-cccccccccccc")


def test_runtime_publication_verifier_requires_anonymous_digest_readback():
    verifier = (ROOT / "scripts" / "verify-runtime-publication.py").read_text(encoding="utf-8")
    assert "mac-openshell-runtime@sha256:" in verifier
    assert 'claude --version | grep -F "2.1.220"' in verifier
    assert 'cursor-agent --version | grep -F "2026.07.23-e383d2b"' in verifier
    assert "command -v codex; command -v claude; command -v cursor-agent;" in verifier
    assert 'anonymous_env["DOCKER_CONFIG"] = config' in verifier
    assert '"pull", args.image_ref' in verifier
    assert "org.opencontainers.image.revision" in verifier
    assert "io.mac.frozen-inputs.sha256" in verifier
    assert "runtime_input_sha256" in verifier
    assert "build_revision" in verifier
    assert "mac.openshell_runtime.publication_verification.v1" in verifier


def test_openshell_image_assets_are_prefetched_and_always_cleaned_up():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "deploy" / "openshell" / "build-runtime-image.sh").read_text(encoding="utf-8")
    preparer = (ROOT / "deploy" / "openshell" / "prepare-runtime-image-assets.sh").read_text(
        encoding="utf-8"
    )

    assert 'IMAGE_ASSET_DIR="$MAC_SRC/.mac-openshell-build-assets"' in script
    assert 'BUILD_LOCK_DIR="$MAC_SRC/.mac-openshell-build.lock"' in script
    assert "acquire_build_lock" in script
    assert 'kill -0 "$owner_pid"' in script
    assert "timed out waiting for OpenShell image-build lock" in script
    assert ".mac-openshell-build.lock" in (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "prepare-runtime-image-assets.sh" in script
    assert "node-amd64.tar.xz" in preparer
    assert "node-arm64.tar.xz" in preparer
    assert "gh-amd64.tgz" in preparer
    assert "gh-arm64.tgz" in preparer
    assert "raw.githubusercontent.com/technomancy/leiningen/${LEIN_COMMIT}/bin/lein" in preparer
    assert "SHA-256 mismatch" in preparer
    assert "trap cleanup EXIT" in script
    assert '--build-arg "GH_VERSION=$GH_VERSION"' in script
    assert '--build-arg "NODE_VERSION=$NODE_VERSION"' in script
    assert '--build-arg "PNPM_VERSION=$PNPM_VERSION"' in script
    assert '--build-arg "CODEX_VERSION=$CODEX_VERSION"' in script
    assert '--build-arg "CLAUDE_VERSION=$CLAUDE_VERSION"' in script
    assert '--build-arg "CURSOR_VERSION=$CURSOR_VERSION"' in script
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
        ["/bin/bash", str(builder)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not any(ready.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(ready.iterdir()), first.communicate(timeout=1)

    second = subprocess.Popen(
        ["/bin/bash", str(builder)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )
    assert "uv sync --frozen --no-editable --extra dev" in containerfile
    assert "COPY pyproject.toml uv.lock README.md /tmp/mac-src/" in containerfile
    assert "COPY src /tmp/mac-src/src" in containerfile
    assert "/tmp/mac-src[dev]" not in containerfile
