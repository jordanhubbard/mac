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
    assert 'current_openshell_version" != "$OPENSHELL_VERSION"' in script
    assert 'current_gateway_version" != "$OPENSHELL_VERSION"' in script
    assert 'uv tool install --force "openshell==$OPENSHELL_VERSION"' in script


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
        "CODEGRAPH_INSTALL_DIR=/opt/codegraph CODEGRAPH_BIN_DIR=/usr/local/bin "
        "curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh"
    ) in containerfile
    assert "chmod -R a+rX /opt/codegraph" in containerfile
    assert "codegraph install --yes" in containerfile
