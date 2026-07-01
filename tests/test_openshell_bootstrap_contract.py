from __future__ import annotations

from pathlib import Path


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
    assert "mirroring $OSH_IMAGE_TAG into OpenShell's runtime-visible image store" in script
    assert "podman load" in script
    assert "runtime image smoke: gh/codex/codegraph visible through OpenShell" in script
    assert script.count('import mac.agent_command') >= 2
    assert "-- bash -c" in script
    assert 'current_openshell_version" != "$OPENSHELL_VERSION"' in script
    assert 'current_gateway_version" != "$OPENSHELL_VERSION"' in script
    assert 'uv tool install --force "openshell==$OPENSHELL_VERSION"' in script
    assert 'MAC_OPENSHELL_UPLOAD_CODEX_AUTH:-0' in script
    assert "rotating OAuth state is not durable in throwaway sandboxes" in script
    create_arg_lines = [
        line for line in script.splitlines() if 'echo "MAC_OPENSHELL_CREATE_ARGS=' in line
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


def test_openshell_image_installs_codegraph_baseline():
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")

    assert (
        "curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh "
        "-o /tmp/codegraph-install.sh"
    ) in containerfile
    assert (
        "CODEGRAPH_INSTALL_DIR=/usr/local/lib/codegraph CODEGRAPH_BIN_DIR=/usr/local/bin "
        "sh /tmp/codegraph-install.sh"
    ) in containerfile
    assert "rm -f /tmp/codegraph-install.sh" in containerfile
    assert 'CG_BIN="$(readlink -f /usr/local/bin/codegraph)"' in containerfile
    assert 'CG_HOME="$(dirname "$(dirname "$CG_BIN")")"' in containerfile
    assert "chown -R root:root /usr/local/lib/codegraph /usr/local/bin/codegraph" in containerfile
    assert "chmod -R a+rX /usr/local/lib/codegraph" in containerfile
    assert "chmod 0755 /usr/local/bin/codegraph" in containerfile
    assert "codegraph install --yes" in containerfile
