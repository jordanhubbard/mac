"""Contracts for the deploy assets that outlive the deleted fleet deploy script:
the dotenv helpers, reviewed tool assets, systemd units, the OpenShell
bootstrap, and the executor prompt/runner contract."""

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from mac.deploy_env import parse_env_text, render_env

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_env_render_round_trips_shell_quoted_values():
    values = {
        "PLAIN": "abc_123",
        "WITH_SPACE": "one two",
        "WITH_SINGLE_QUOTE": "one'two",
        "WITH_HASH": "abc#def",
        "EMPTY": "",
    }

    assert parse_env_text(render_env(values)) == values
    assert parse_env_text("export FOO='bar baz'\nQUOTED='one'\"'\"'two'\n") == {
        "FOO": "bar baz",
        "QUOTED": "one'two",
    }


def test_parse_env_text_skips_malformed_quoted_lines():
    # A line with unbalanced shell quoting is corrupt: it must be skipped, not
    # stored as a half-parsed value, and must not poison the good lines around it.
    text = (
        "GOOD=ok\n"
        'BROKEN="unterminated\n'  # unbalanced double quote
        "ALSO_BROKEN=it's mine\n"  # unbalanced single quote
        "NEXT=fine\n"
    )
    parsed = parse_env_text(text)
    assert parsed == {"GOOD": "ok", "NEXT": "fine"}
    assert "BROKEN" not in parsed
    assert "ALSO_BROKEN" not in parsed


def test_parse_env_text_trailing_unquoted_tokens_take_leading_assignment():
    # Documented fallback semantics: with trailing unquoted tokens the leading
    # KEY=val wins and the rest is ignored (render_env never emits this — unsafe
    # values are quoted — so it only arises from a hand-edited file).
    assert parse_env_text("KEY=val extra garbage\n") == {"KEY": "val"}
    assert parse_env_text("export RAW=plainvalue\n") == {"RAW": "plainvalue"}


def test_reviewed_tool_asset_checksum_mismatch_fails_closed(tmp_path):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    payload = tmp_path / "asset.tgz"
    payload.write_bytes(b"not the reviewed release")

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; mac_verify_reviewed_asset "$2" "$3"',
            "bash",
            str(assets),
            str(payload),
            "0" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "SHA-256 mismatch for reviewed asset" in result.stderr


def test_reviewed_download_preserves_proxy_trust_without_deploy_credentials(tmp_path):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    observed = tmp_path / "curl-environment"
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "${SSL_CERT_FILE-}" "${CURL_CA_BUNDLE-}" '
        '"${MAC_SECRET_KEY-unset}" > ' + shlex.quote(str(observed)) + "\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then printf payload > "$2"; exit 0; fi\n'
        "  shift\n"
        "done\nexit 1\n"
    )
    curl.chmod(0o755)
    expected = hashlib.sha256(b"payload").hexdigest()
    target = tmp_path / "download.tgz"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; '
            'mac_reviewed_asset_spec() { printf "asset.tgz %s https://example.invalid/asset.tgz root\\n" "$digest"; }; '
            'digest="$3"; mac_download_reviewed_asset uv "$2"',
            "bash",
            str(assets),
            str(target),
            expected,
        ],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "SSL_CERT_FILE": "/trusted/proxy-ca.pem",
            "CURL_CA_BUNDLE": "/trusted/proxy-ca.pem",
            "MAC_SECRET_KEY": "fixture-secret-must-not-enter-curl",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b"payload"
    assert observed.read_text().splitlines() == [
        "/trusted/proxy-ca.pem",
        "/trusted/proxy-ca.pem",
        "unset",
    ]


@pytest.mark.parametrize(
    ("tool", "os_name", "architecture", "filename"),
    [
        ("uv", "Linux", "x86_64", "uv-x86_64-unknown-linux-gnu.tar.gz"),
        ("uv", "Linux", "aarch64", "uv-aarch64-unknown-linux-gnu.tar.gz"),
        ("uv", "Darwin", "x86_64", "uv-x86_64-apple-darwin.tar.gz"),
        ("uv", "Darwin", "arm64", "uv-aarch64-apple-darwin.tar.gz"),
        (
            "python",
            "Linux",
            "x86_64",
            "cpython-3.14.7+20260901-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        ),
        (
            "python",
            "Linux",
            "aarch64",
            "cpython-3.14.7+20260901-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz",
        ),
    ],
)
def test_reviewed_tool_asset_matrix_covers_fleet_platforms(tool, os_name, architecture, filename):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; mac_reviewed_asset_spec "$2" "$3" "$4"',
            "bash",
            str(assets),
            tool,
            os_name,
            architecture,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    observed_name, digest, url, root = result.stdout.strip().split()
    assert observed_name == filename
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert url.startswith("https://github.com/") and url.endswith(filename.replace("+", "%2B"))
    assert root == ("python" if tool == "python" else filename.removesuffix(".tar.gz"))


@pytest.mark.parametrize(
    ("tool", "os_name", "architecture"),
    [("uv", "Plan9", "x86_64")],
)
def test_reviewed_tool_asset_unsupported_platform_fails_closed(tool, os_name, architecture):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; mac_reviewed_asset_spec "$2" "$3" "$4"',
            "bash",
            str(assets),
            tool,
            os_name,
            architecture,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unsupported reviewed-tool" in result.stderr


def test_fleet_context_systemd_unit_does_not_require_a_local_control_plane():
    unit = (ROOT / "deploy" / "systemd" / "mac-fleet-context.service").read_text(encoding="utf-8")
    unit_section = unit.split("[Service]", 1)[0]

    assert "After=network-online.target" in unit_section
    assert "Wants=network-online.target" in unit_section
    assert not re.search(r"^(?:After|Requires)=.*\bmac\.service\b", unit_section, re.MULTILINE)


def test_openshell_bootstrap_needs_no_macos_docker_path():
    """The macOS non-interactive PATH workaround is gone with the path it served.

    It existed so a launchd/SSH bootstrap could find Docker Desktop. macOS
    nodes are host installs now (ADR 0015) and bootstrap exits before any
    Docker lookup, so prepending Docker.app to PATH would be dead code that
    implies a dependency the platform no longer has.
    """

    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(encoding="utf-8")

    assert "/Applications/Docker.app" not in script
    darwin_entry = script.index('if [ "$(uname -s)" = "Darwin" ]; then')
    assert "exit 0" in script[darwin_entry : script.index("\nfi\n", darwin_entry)]


def test_executor_prompt_includes_repository_runtime_contract():
    script = (ROOT / "src" / "mac" / "executor_prompt.py").read_text(encoding="utf-8")

    assert "def repository_contract_section(task: Dict[str, Any]) -> str:" in script
    assert "Repository runtime contract:" in script
    assert "Use $MAC_TASK_REPO_WORKTREE as the only writable checkout." in script
    # NOT advertised as an alternative: metadata.runtime.repository_worktree is a
    # host-absolute path for the worker's own host-side orchestration, not one
    # that exists inside the sandbox. An agent that read it and tried to access
    # it directly was auto-rejected as "external_directory" -- the same failure
    # mode $MAC_TASK_FILE's deferral fixed for task.json (see
    # repository_contract_section's neighboring comment).
    assert "metadata.runtime.repository_worktree" not in script
    assert "origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only" in script
    assert "Agent ownership ends with tested task-worktree changes" in script
    assert "deterministic host finalizer exclusively owns fetching" in script
    assert "bootstrap.command" in script
    assert "test.command" in script
    assert ".mac-executor-policy.txt" in script
    policy = (ROOT / "src" / "mac" / "executor-policy.txt").read_text(encoding="utf-8")
    assert policy.startswith("mac.executor_policy.v1")
    assert "Write $MAC_TASK_WORKSPACE/mac-evidence.json" in policy


def test_reviewer_prompt_includes_verdict_contract():
    script = (ROOT / "src" / "mac" / "executor_prompt.py").read_text(encoding="utf-8")

    assert "MAC_TASK_REPO_WORKTREE" in script
    assert "local review checkout" in script
    assert "run the repository contract test command" in script
    assert "repo copied from the executor verification repo object" in script
    assert "worktree_digest as sha256" in script
    assert "reviewed_evidence_id=%s" in script


def test_mac_repository_contract_test_command_uses_hermetic_runner():
    contract = yaml.safe_load((ROOT / ".mac" / "project.yaml").read_text(encoding="utf-8"))
    runner = ROOT / "scripts" / "run-contract-tests.sh"

    assert contract["test"]["command"] == "scripts/run-contract-tests.sh"
    assert "gh" in contract["toolchain"]["required_commands"]
    text = runner.read_text(encoding="utf-8")
    assert 'unset "${!MAC_@}"' in text
    # Interpreter is discovered (repo .venv on dev hosts; /opt/mac-venv in the
    # OpenShell task sandbox), not hardcoded to .venv — a hardcoded
    # .venv/bin/python is rc 127 in-sandbox and blocked every code task.
    # Do not require ``exec`` here: focused runs use a temporary HOME that the
    # runner must remove after pytest returns.  The contract is interpreter
    # discovery plus lossless forwarding of the caller's pytest arguments.
    assert '"$PY" -m pytest "$@"' in text
    assert "/opt/mac-venv/bin/python" in text


def test_openshell_bootstrap_installs_the_reviewed_archive_not_a_symlink():
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    assert 'python3 "$helper" install-archive' in bootstrap
    assert 'ln -sf "$cli" "$MAC_HOME/bin/openshell"' not in bootstrap
