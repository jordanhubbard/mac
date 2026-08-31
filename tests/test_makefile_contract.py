from __future__ import annotations

import runpy
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_make_help_exposes_conventional_lifecycle() -> None:
    result = subprocess.run(
        ["make", "help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "make install" in result.stdout
    assert "make build" in result.stdout
    assert "make clean" in result.stdout
    assert "make distclean" in result.stdout
    assert "make run-gui" in result.stdout
    # run-gui must advertise the UI the hub actually serves (ADR 0025). It used
    # to say "canonical Fleet IDE" for a bundle no hub has ever mounted.
    assert "the hub serves at /ui" in result.stdout
    assert "canonical Fleet IDE" not in result.stdout
    assert "Python 3.11+, git, gh, and npm" in result.stdout
    assert "Build and test targets also require uv." in result.stdout


def test_makefile_defaults_to_help_and_keeps_fleet_setup_distinct() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert ".DEFAULT_GOAL := help" in makefile
    assert "install: install-cli install-gui" in makefile
    assert "build: build-cli build-gui" in makefile
    assert "clean: clean-cli clean-gui" in makefile
    assert "setup: require-python" in makefile
    assert "Configure a fleet and deploy it (not a local CLI install)." in makefile
    assert "rm -rf dist\n" not in makefile
    assert "updating the existing MAC-managed pre-push hook" in makefile


def test_make_dry_run_builds_both_supported_surfaces() -> None:
    result = subprocess.run(
        ["make", "-n", "build"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "uv build --wheel" in result.stdout
    assert "npm run build" in result.stdout


def test_package_cli_verifies_the_current_console_script_contract() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    scripts = makefile.split("CONSOLE_SCRIPTS = ", 1)[1].splitlines()[0].split()

    assert "mac-hermes-gateway" not in scripts
    assert {
        "mac",
        "mac-agent",
        "mac-evidence",
        "mac-git-askpass",
        "mac-openshell-collector",
        "mac-openshell-supervisor",
        "mac-pg-backup",
        "mac-router",
        "mac-schema-migrate",
    } <= set(scripts)


def test_gui_launcher_selects_auth_without_printing_the_token() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    launcher = (ROOT / "src" / "mac" / "ide_launcher.py").read_text(encoding="utf-8")
    vite_config = (ROOT / "ide" / "vite.config.ts").read_text(encoding="utf-8")
    app = (ROOT / "ide" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "IDE_FLEET ?=" in makefile
    assert "IDE_AUTH ?= auto" in makefile
    assert "IDE_HANDOFF_FILE ?=" in makefile
    assert "IDE_OPEN ?= 0" in makefile
    assert "IDE_PROFILE ?=" in makefile
    # run-gui belongs to the served console; the IDE launcher is reached by
    # ide-run/ide-dev, which is what these launcher assertions cover.
    assert "run-gui: ide-run" not in makefile
    assert "ide-run ide-dev:" in makefile
    assert 'if [ -f "$$HOME/.mac/.env" ]' in makefile
    assert '"$(PYTHON)" -m mac.ide_launcher' in makefile
    assert "IDE auth token: %s" not in makefile
    assert "ensure_session(selected_profile)" in launcher
    assert "load_profile(selected_profile, include_token=True)" in launcher
    assert "load_handoff_connection" in launcher
    assert 'child["MAC_IDE_PROXY_TOKEN"] = connection.token' in launcher
    assert 'child.pop("VITE_MAC_TOKEN"' not in launcher
    assert '"VITE_MAC_TOKEN",' in launcher
    assert "MAC_IDE_PROXY_TOKEN" in vite_config
    assert 'proxyRequest.setHeader("Authorization"' in vite_config
    assert "hasManagedAuth" in app
    assert "authLabel" in app
    assert "http://localhost:8789/ui?t=${hub_token}" not in deploy
    assert "write_ide_handoff_file" in deploy
    assert "mac.ide_handoff.v1" in deploy
    assert "IDE_OPEN=1 make ide-run" in deploy
    # The deploy banner sends operators to the UI the hub is running.
    assert "http://127.0.0.1:8789/ui" in deploy


def test_bootstrap_honors_make_venv_override(monkeypatch) -> None:
    monkeypatch.setenv("MAC_VENV", "custom-venv")

    namespace = runpy.run_path(str(ROOT / "scripts" / "bootstrap-project.py"))

    assert namespace["VENV"] == ROOT / "custom-venv"


def test_sanity_test_depends_on_impact_map_regeneration() -> None:
    """Consumers of the committed map must list the producer as a prerequisite.

    The analog is test-schema-migrations: postgres-schema. Without this, a
    stale interned node id is discovered by an always_run guard after the
    selector has already handed pytest a usage-error id, or not at all
    until the nightly portfolio job happens to rebuild the artifact.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "sanity-test: impact-map" in makefile
    assert "test-schema-migrations: postgres-schema" in makefile
    assert "MAC_TEST_REBUILD_MAP=1" in makefile
    assert "IMPACT_MAP_ARGS ?= --check" in makefile

    sanity = subprocess.run(
        ["make", "-n", "sanity-test"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert sanity.returncode == 0, sanity.stderr
    assert "build-test-impact-map.py" in sanity.stdout
    assert "run-sanity-tests.sh" in sanity.stdout

    portfolio = subprocess.run(
        ["make", "-n", "test-portfolio"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert portfolio.returncode == 0, portfolio.stderr
    assert "MAC_TEST_REBUILD_MAP=1" in portfolio.stdout
