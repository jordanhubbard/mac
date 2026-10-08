"""Behavioral contract for deploy/install-postgres-service.sh and its unit.

mac.store accepts only postgres:// / postgresql:// DSNs. The installer
provisions a local Postgres for the hub and never rotates an existing password
or binds all interfaces.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "deploy" / "install-postgres-service.sh"
SYSTEMD_UNIT = ROOT / "deploy" / "systemd" / "mac-postgres.service"


def test_install_script_exists_and_is_valid_bash() -> None:
    assert INSTALL_SCRIPT.exists()
    result = subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True)
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
    assert text.count("@POSTGRES_CONTAINER_RUNTIME@") == 3


def test_systemd_unit_uses_the_selected_container_runtime(tmp_path: Path) -> None:
    function = _extract_function(INSTALL_SCRIPT, "render_systemd_unit")
    for runtime in ("/usr/bin/docker", "/usr/bin/podman"):
        rendered = tmp_path / (Path(runtime).name + ".service")
        result = subprocess.run(
            ["bash", "-c", function + '\nrender_systemd_unit "$OUTPUT"'],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "UNIT_TEMPLATE": str(SYSTEMD_UNIT),
                "ENV_DEST": "/etc/ovswarm/postgres.env",
                "CONTAINER_CMD_ABS": runtime,
                "OUTPUT": str(rendered),
            },
        )
        assert result.returncode == 0, result.stderr
        text = rendered.read_text(encoding="utf-8")
        assert "@POSTGRES_CONTAINER_RUNTIME@" not in text
        assert text.count(runtime) == 3
        assert "EnvironmentFile=-/etc/ovswarm/postgres.env" in text
        other_runtime = "/usr/bin/podman" if runtime.endswith("docker") else "/usr/bin/docker"
        assert other_runtime not in text


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
            'value="$(get_env_key /nonexistent/file SOME_KEY)"',
            "printf 'ok:[%s]\\n' \"$value\"",
        ]
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok:[]"

    empty_file_script = "\n".join(
        [
            "set -euo pipefail",
            _extract_function(INSTALL_SCRIPT, "get_env_key"),
            "tmp=$(mktemp)",
            ': > "$tmp"',
            'value="$(get_env_key "$tmp" SOME_KEY)"',
            "printf 'ok:[%s]\\n' \"$value\"",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", empty_file_script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok:[]"


def test_get_env_key_reads_an_existing_protected_env_through_privilege_boundary(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "postgres.env"
    env_file.write_text("POSTGRES_PASSWORD=existing-volume-authority\n", encoding="utf-8")
    env_file.chmod(0)
    script = "\n".join(
        [
            "set -euo pipefail",
            # Model passwordless sudo without requiring elevated privileges in
            # the test process. The fixture is owner-unreadable, so reaching
            # this function proves get_env_key chose its protected-file path.
            'maybe_sudo() { chmod 600 "$PROTECTED_ENV"; "$@"; }',
            _extract_function(INSTALL_SCRIPT, "get_env_key"),
            'value="$(get_env_key "$PROTECTED_ENV" POSTGRES_PASSWORD)"',
            'printf "value:[%s]\\n" "$value"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PROTECTED_ENV": str(env_file)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "value:[existing-volume-authority]"
    assert result.stderr == ""


def test_get_env_key_fails_closed_when_protected_env_cannot_be_read(tmp_path: Path) -> None:
    env_file = tmp_path / "postgres.env"
    env_file.write_text("POSTGRES_PASSWORD=must-not-be-replaced\n", encoding="utf-8")
    env_file.chmod(0)
    script = "\n".join(
        [
            "set -euo pipefail",
            "maybe_sudo() { return 1; }",
            _extract_function(INSTALL_SCRIPT, "get_env_key"),
            'get_env_key "$PROTECTED_ENV" POSTGRES_PASSWORD',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PROTECTED_ENV": str(env_file)},
    )
    assert result.returncode != 0
    assert "could not read existing protected environment file" in result.stderr
    assert "must-not-be-replaced" not in result.stdout + result.stderr


def _extract_function(path: Path, name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        path.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"function {name} not found in {path}"
    return match.group(0)
