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
    assert "Podman compatibility shim" in script


def test_openshell_image_docs_do_not_advertise_podman_builds():
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")

    assert "docker build" in containerfile
    assert "podman build" not in containerfile
    assert "Docker Engine/Moby" in containerfile
