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
    assert "canonical Fleet IDE" in result.stdout
    assert "Python 3.11+, git, gh, npm, and CodeGraph" in result.stdout
    assert "Build and test targets also require uv." in result.stdout


def test_makefile_defaults_to_help_and_keeps_fleet_setup_distinct() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert ".DEFAULT_GOAL := help" in makefile
    # install-codegraph leads: an install that omits CodeGraph is one that
    # fails later at `litai init`, far from the cause.
    assert "install: install-codegraph install-cli install-gui" in makefile
    assert "build: build-cli build-gui" in makefile
    assert "clean: clean-cli clean-gui" in makefile
    assert "setup: require-python codegraph-sync" in makefile
    assert "Configure a fleet and deploy it (not a local CLI install)." in makefile
    assert "rm -rf dist\n" not in makefile
    assert "updating the existing MAC-managed pre-push hook" in makefile


def test_source_consuming_make_targets_refresh_codegraph() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    sync_script = (ROOT / "scripts" / "sync-codegraph.sh").read_text(encoding="utf-8")
    pre_push = (ROOT / "scripts" / "pre-push").read_text(encoding="utf-8")

    assert "codegraph-sync:" in makefile
    assert 'MAC_CODEGRAPH_BIN="$(CODEGRAPH)" scripts/sync-codegraph.sh' in makefile
    for target in (
        "install-cli",
        "install-gui",
        "build-cli",
        "build-gui",
        "test",
        "ide-run ide-dev",
        "setup",
        "deploy",
    ):
        line = next(line for line in makefile.splitlines() if line.startswith(target + ":"))
        assert "codegraph-sync" in line

    assert '"$CODEGRAPH_BIN" sync --quiet .' in sync_script
    assert '"$CODEGRAPH_BIN" init .' in sync_script
    assert "scripts/sync-codegraph.sh" in pre_push


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
    assert "scripts/sync-codegraph.sh" in result.stdout


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
    assert "run-gui: ide-run" in makefile
    assert 'if [ -f "$$HOME/.mac/.env" ]' in makefile
    assert '"$(PYTHON)" -m mac.ide_launcher' in makefile
    assert "IDE auth token: %s" not in makefile
    assert "ensure_session(selected_profile)" in launcher
    assert 'load_profile(selected_profile, include_token=True)' in launcher
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
    assert "IDE_OPEN=1 make run-gui" in deploy


def test_bootstrap_honors_make_venv_override(monkeypatch) -> None:
    monkeypatch.setenv("MAC_VENV", "custom-venv")

    namespace = runpy.run_path(str(ROOT / "scripts" / "bootstrap-project.py"))

    assert namespace["VENV"] == ROOT / "custom-venv"
