"""Behavioral contract for auto-provisioning the hub's PostgreSQL database.

mac.store accepts only postgres:// / postgresql:// DSNs -- there is no
SQLite fallback -- but nothing in the deploy pipeline ever provisioned one.
A from-scratch --first-hub-bootstrap crashed at "creating/updating mac
environment file" because MAC_DATABASE_URL was never set and the
deploy_env.py default MAC_DB (a SQLite path) is dead code the store layer
rejects. This mirrors the existing Qdrant/Firecrawl auto-install pattern:
default to auto-provisioning a local Postgres on the control-plane node,
never overriding an operator-configured DSN.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"
INSTALL_SCRIPT = ROOT / "deploy" / "install-postgres-service.sh"
SYSTEMD_UNIT = ROOT / "deploy" / "systemd" / "mac-postgres.service"


def _text() -> str:
    return NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")


def _function(name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        _text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"function {name} not found"
    return match.group(0)


def _run_postgres_install_enabled(
    *,
    postgres_url_configured: str = "",
    postgres_install: str = "auto",
    agent: str = "rocky",
    shared_services_manager_agent: str = "rocky",
) -> subprocess.CompletedProcess[str]:
    snippet = "\n".join(
        [
            "log() { printf '%s\\n' \"$*\" >&2; }",
            f'POSTGRES_URL_CONFIGURED={postgres_url_configured!r}',
            f'POSTGRES_INSTALL={postgres_install!r}',
            f'AGENT={agent!r}',
            f'SHARED_SERVICES_MANAGER_AGENT={shared_services_manager_agent!r}',
            _function("control_plane_enabled"),
            _function("postgres_install_enabled"),
            "postgres_install_enabled",
        ]
    )
    return subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, check=False
    )


def test_operator_configured_dsn_always_wins_over_auto_install() -> None:
    result = _run_postgres_install_enabled(
        postgres_url_configured="postgresql://mac:secret@10.0.0.5:5432/mac",
        postgres_install="1",
    )
    assert result.returncode != 0, (
        "an explicit MAC_DEPLOY_DATABASE_URL must never be overridden by "
        "auto-install, even when MAC_DEPLOY_POSTGRES_INSTALL=1"
    )


def test_auto_mode_installs_only_on_the_control_plane_node() -> None:
    hub = _run_postgres_install_enabled(agent="rocky", shared_services_manager_agent="rocky")
    worker = _run_postgres_install_enabled(agent="natasha", shared_services_manager_agent="rocky")

    assert hub.returncode == 0, hub.stderr
    assert worker.returncode != 0


def test_explicit_off_is_honored_even_with_no_dsn_configured() -> None:
    result = _run_postgres_install_enabled(postgres_install="0")
    assert result.returncode != 0


def test_unsupported_install_value_fails_closed() -> None:
    result = _run_postgres_install_enabled(postgres_install="sometimes")
    assert result.returncode != 0
    assert "unsupported MAC_DEPLOY_POSTGRES_INSTALL value" in result.stderr


def test_database_provisioning_precedes_the_mac_env_write() -> None:
    text = _text()
    assert text.index("install_or_validate_control_plane_database") < text.index(
        'log "creating/updating mac environment file"'
    ), (
        "MAC_DATABASE_URL must exist before `mac.deploy_env write-mac-env` "
        "runs, or the freshly written mac.env has no working DSN"
    )


def test_non_control_plane_nodes_never_require_a_local_database() -> None:
    function = _function("install_or_validate_control_plane_database")
    assert function.index("control_plane_enabled || return 0") < function.index(
        "postgres_install_enabled"
    )


def test_install_script_forwards_the_dsn_back_to_the_caller() -> None:
    function = _function("install_or_validate_control_plane_database")
    assert "POSTGRES_DSN_OUT_FILE" in function
    assert 'export MAC_DEPLOY_DATABASE_URL' in function
    assert (
        'die "install-postgres-service.sh did not report a database DSN"'
        in function
    )


def test_install_script_exists_and_is_valid_bash() -> None:
    assert INSTALL_SCRIPT.exists()
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_install_script_reuses_an_existing_password_instead_of_rotating_it() -> None:
    # Postgres bakes the creating user's password into the data volume on
    # first init -- regenerating it on every redeploy would lock the deploy
    # out of its own already-provisioned database.
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert 'get_env_key "$ENV_DEST" POSTGRES_PASSWORD' in text
    assert 'get_env_key "${MAC_HOME}/mac.env" MAC_CONTROL_PLANE_DB_PASSWORD' in text


def test_install_script_refuses_to_bind_all_interfaces() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "refusing unsafe all-interface bind address" in text


def test_systemd_unit_template_exists() -> None:
    assert SYSTEMD_UNIT.exists()
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "POSTGRES_BIND_ADDR=127.0.0.1" in text


def test_native_package_fallback_uses_noninteractive_apt() -> None:
    # Found live on a sandboxed GKE pod (no /dev/net/tun, no NET_ADMIN):
    # podman reports a working `info` but cannot start a container's network
    # namespace there, so this is the only real fallback -- and postgresql
    # pulls in tzdata, whose postinst prompts for a timezone via debconf.
    # Without DEBIAN_FRONTEND=noninteractive, apt-get hangs forever on that
    # prompt (no TTY to answer it) instead of failing loudly.
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql" in text


def test_get_env_key_does_not_die_on_a_first_run_with_no_password_yet() -> None:
    # `var="$(get_env_key ...)"` is a bare command-substitution assignment;
    # under `set -euo pipefail`, grep's exit 1 on "no match" (the normal
    # case before any password has ever been written) propagates through
    # the pipeline and kills the whole script -- found live when the very
    # first run on a fresh node exited silently right after this call.
    script = "\n".join(
        [
            "set -euo pipefail",
            _extract_function(INSTALL_SCRIPT, "get_env_key"),
            "value=\"$(get_env_key /nonexistent/file SOME_KEY)\"",
            "printf 'ok:[%s]\\n' \"$value\"",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok:[]"

    empty_file_script = "\n".join(
        [
            "set -euo pipefail",
            _extract_function(INSTALL_SCRIPT, "get_env_key"),
            "tmp=$(mktemp)",
            ": > \"$tmp\"",
            "value=\"$(get_env_key \"$tmp\" SOME_KEY)\"",
            "printf 'ok:[%s]\\n' \"$value\"",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", empty_file_script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok:[]"


def _extract_function(path: Path, name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        path.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"function {name} not found in {path}"
    return match.group(0)
