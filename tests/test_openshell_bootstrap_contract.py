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
    assert (
        "mirroring $OSH_IMAGE_TAG into OpenShell's runtime-visible image store"
        in script
    )
    assert "podman load" in script
    assert "runtime image smoke: gh/codex/codegraph visible through OpenShell" in script
    assert "run_live_confinement_probe" in script
    assert "live-confinement-probe.sh" in script
    assert "CONFINEMENT_PROBE_OK" in (
        ROOT / "deploy" / "openshell" / "live-confinement-probe.sh"
    ).read_text(encoding="utf-8")
    assert "MAC_OPENSHELL_GC=1" in script
    assert "MAC_OPENSHELL_STALE_AFTER_SECONDS=86400" in script
    assert 'current_gateway_version" != "$OPENSHELL_VERSION"' in script
    assert script.count("import mac.agent_command") >= 2
    assert "-- bash -c" in script
    assert 'current_openshell_version" != "$OPENSHELL_VERSION"' in script
    assert 'current_gateway_version" != "$OPENSHELL_VERSION"' in script
    assert 'uv tool install --force "openshell==$OPENSHELL_VERSION"' in script
    assert "install_openshell_cli_static" in script
    assert "openshell-x86_64-unknown-linux-musl.tar.gz" in script
    assert "openshell-aarch64-unknown-linux-musl.tar.gz" in script
    assert "Python wheel is incompatible with this host" in script
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
    assert "sudo systemctl show-environment" in script
    assert "manager=$gateway_manager state=$gateway_state" in script
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


def test_openshell_image_installs_codegraph_baseline():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")

    assert 'CODEGRAPH_VERSION="${CODEGRAPH_VERSION:-v1.1.6}"' in bootstrap
    assert "prefetching pinned runtime-image assets on the host" in bootstrap
    assert "codegraph-linux-${codegraph_arch}.tar.gz" in bootstrap
    assert 'ARG CODEGRAPH_VERSION="v1.1.6"' in containerfile
    assert (
        "COPY .mac-openshell-build-assets /tmp/mac-openshell-build-assets"
        in containerfile
    )
    assert "tar -xzf /tmp/mac-openshell-build-assets/codegraph.tgz" in containerfile
    assert (
        'CG_HOME="/usr/local/lib/codegraph/versions/${CODEGRAPH_VERSION}"'
        in containerfile
    )
    assert 'ln -sfn "$CG_HOME" /usr/local/lib/codegraph/current' in containerfile
    assert 'ARG GH_VERSION="2.95.0"' in containerfile
    assert "https://github.com/cli/cli/releases/download/v${GH_VERSION}/" in bootstrap
    assert "gh_${GH_VERSION}_linux_${gh_arch}.tar.gz" in bootstrap
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


def test_openshell_image_assets_are_prefetched_and_always_cleaned_up():
    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    assert 'IMAGE_ASSET_DIR="$MAC_SRC/.mac-openshell-build-assets"' in script
    assert "prepare_image_build_assets" in script
    assert "nodesource_setup.sh" in script
    assert "gh.tgz" in script
    assert "raw.githubusercontent.com/technomancy/leiningen" in script
    assert "cdn.jsdelivr.net/gh/technomancy/leiningen@stable/bin/lein" in script
    assert "codegraph.tgz" in script
    assert "trap cleanup_image_build_assets EXIT" in script
    assert '--build-arg "GH_VERSION=$GH_VERSION"' in script
    assert '--build-arg "CODEGRAPH_VERSION=$CODEGRAPH_VERSION"' in script


def test_openshell_image_installs_dev_extra_for_contract_tests():
    """The task sandbox must carry the [dev] extra (pytest, coverage, …) so the
    repository contract test — scripts/run-contract-tests.sh, which collects the
    full suite — can actually RUN in-sandbox. Without it, in-sandbox
    verification of a repo-coupled code task fails to execute and the substance
    gate can never pass, so no autonomous code change lands through OpenShell."""
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")
    assert '/opt/mac-venv/bin/pip install --no-cache-dir "/tmp/mac-src[dev]"' in containerfile
