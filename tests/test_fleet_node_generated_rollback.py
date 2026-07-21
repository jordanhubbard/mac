from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"


FAKE_SUPERVISOR_HELPER = r'''from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def value(name: str) -> str:
    positions = [index for index, item in enumerate(sys.argv) if item == name]
    if len(positions) != 1 or positions[0] + 1 >= len(sys.argv):
        raise SystemExit("expected exactly one " + name)
    return sys.argv[positions[0] + 1]


action = sys.argv[1]
if action not in {"quiesce", "restore"}:
    raise SystemExit("unexpected action")
supervisor = value("--supervisor")
mode = value("--control-plane-mode")
if supervisor != "supervisord":
    raise SystemExit("rollback selected more than the captured supervisor")

source_marker = Path(os.environ["ROLLBACK_TEST_SRC"]) / "generation"
venv_marker = Path(os.environ["ROLLBACK_TEST_VENV"]) / "generation"
source_generation = source_marker.read_text() if source_marker.is_file() else "absent"
venv_generation = venv_marker.read_text() if venv_marker.is_file() else "absent"
config_generation = Path(os.environ["ROLLBACK_TEST_CONF"]).read_text()
bin_generation = (Path(os.environ["ROLLBACK_TEST_BIN"]) / "generation").read_text()
revision_generation = Path(os.environ["ROLLBACK_TEST_REVISION"]).read_text()
openclaw_home = Path(os.environ["ROLLBACK_TEST_OPENCLAW_HOME"])
expected = "current" if action == "quiesce" else "restored"
expected_source = (
    os.environ.get("ROLLBACK_TEST_QUIESCE_SOURCE_STATE", expected)
    if action == "quiesce"
    else expected
)
expected_venv = (
    os.environ.get("ROLLBACK_TEST_QUIESCE_VENV_STATE", expected)
    if action == "quiesce"
    else expected
)
expected_config = (
    os.environ.get("ROLLBACK_TEST_QUIESCE_CONFIG_STATE", expected)
    if action == "quiesce"
    else expected
)
expected_bin = (
    os.environ.get("ROLLBACK_TEST_QUIESCE_BIN_STATE", expected)
    if action == "quiesce"
    else expected
)
expected_revision = (
    os.environ.get(
        "ROLLBACK_TEST_QUIESCE_REVISION",
        os.environ["ROLLBACK_TEST_CURRENT_REVISION"],
    )
    if action == "quiesce"
    else os.environ["ROLLBACK_TEST_PRIOR_REVISION"]
)
if (
    source_generation != expected_source
    or venv_generation != expected_venv
    or config_generation != expected_config
    or bin_generation != expected_bin
    or revision_generation.strip() != expected_revision
):
    raise SystemExit("supervisor transition occurred on a mixed artifact generation")
if (
    action == "quiesce"
    and os.environ.get("ROLLBACK_TEST_QUIESCE_OPENCLAW", "present") == "present"
    and not openclaw_home.is_dir()
):
    raise SystemExit("current OpenClaw runtime state disappeared before quiescence")
if action == "restore":
    if openclaw_home.exists():
        raise SystemExit("prior-absent OpenClaw runtime state was not removed")
    if value("--active-gateway") != "hermes":
        raise SystemExit("rollback did not use the recorded prior gateway owner")
    if value("--agent-prior-state") != "active":
        raise SystemExit("rollback did not use the recorded prior worker state")
if action == "restore" and os.environ.get("ROLLBACK_TEST_FAIL_RESTORE") == "1":
    raise SystemExit("injected supervisor restore failure")

control_marker = Path(os.environ["ROLLBACK_TEST_CONTROL_MARKER"])
hermes_marker = Path(os.environ["ROLLBACK_TEST_HERMES_MARKER"])
agent_marker = Path(os.environ["ROLLBACK_TEST_AGENT_MARKER"])
if action == "quiesce":
    control_marker.unlink(missing_ok=True)
    hermes_marker.unlink(missing_ok=True)
    agent_marker.unlink(missing_ok=True)
else:
    hermes_marker.write_text("active")
    agent_marker.write_text("active")
    if mode == "inactive":
        control_marker.unlink(missing_ok=True)
    else:
        control_marker.write_text("active")

log_path = Path(os.environ["ROLLBACK_TEST_LOG"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "action": action,
        "supervisor": supervisor,
        "mode": mode,
        "source": source_generation,
        "venv": venv_generation,
        "config": config_generation,
    }, sort_keys=True) + "\n")

receipt = Path(value("--receipt"))
receipt.write_text(json.dumps({
    "schema": "mac.fleet_node_rollback_supervisor.v1",
    "action": action,
    "status": "passed",
    "prior_topology": (
        {
            "active_gateway": value("--active-gateway"),
            "agent_state": value("--agent-prior-state"),
        }
        if action == "restore"
        else None
    ),
}) + "\n")
receipt.chmod(stat.S_IRUSR | stat.S_IWUSR)
'''


FAKE_ATOMIC_ARTIFACT_HELPER = r'''#!/usr/bin/env bash

mac_launchd_artifact_timeout() {
  printf '%s\n' 10
}

mac_launchd_run_python_bounded() {
  local mode="$1" timeout="$2" program="$3"
  shift 3
  : "$mode" "$timeout"
  command python3 -c "$program" "$@"
}

mac_run_bounded() {
  local timeout="$1"
  shift
  : "$timeout"
  "$@"
}

mac_launchd_snapshot_file() {
  local source="$1" backup="$2" mode="${3:-user}"
  : "$mode"
  if [ -f "$source" ]; then
    command cp -f "$source" "$backup"
    printf '%s\n' 1
  else
    printf '%s\n' 0
  fi
}

mac_launchd_atomic_restore() {
  local backup="$1" destination="$2" mode="${3:-user}" stage
  : "$mode"
  stage="${destination}.test-stage.$$"
  command cp -f "$backup" "$stage"
  mv -f "$stage" "$destination"
}

mac_launchd_atomic_replace() {
  local staged="$1" destination="$2" mode="${3:-user}"
  : "$mode" "${4:-}" "${5:-}" "${6:-}"
  command cp -f "$staged" "$destination"
  rm -f "$staged"
}

mac_launchd_remove_file_and_fsync() {
  local destination="$1" mode="${2:-user}"
  : "$mode"
  if [ "${ROLLBACK_TEST_FAIL_CLEANUP:-}" = file ] \
      && [[ "$destination" == *rollback-current-file.* ]]; then
    return 74
  fi
  rm -f "$destination"
}

mac_launchd_fsync_directory() {
  : "$1" "${2:-user}"
}
'''


def _rollback_function() -> str:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    body = source.split("write_rollback_script() {", 1)[1].split(
        "\n}\n\nverify_phase2_rollback_intent() {", 1
    )[0]
    return "write_rollback_script() {" + body + "\n}\n"


def _finalize_function() -> str:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    body = source.split("write_phase2_finalize_receipt() {", 1)[1].split(
        "\n}\n\nsnapshot_rollback_file() {", 1
    )[0]
    return "write_phase2_finalize_receipt() {" + body + "\n}\n"


def _write_generation(path: Path, generation: str, *, python: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "generation").write_text(generation, encoding="utf-8")
    if python:
        bin_dir = path / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "python").symlink_to(sys.executable)


def _shell_assignments(values: Mapping[str, str]) -> str:
    return "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())


def _generate_rollback(tmp_path: Path, *, control_active: bool) -> tuple[Path, dict[str, Path]]:
    successor_generation = "successor-generation-rocky-001"
    prior_generation = "prior-generation-rocky-000"
    successor_revision = "c" * 40
    prior_revision = "b" * 40
    mac_home = tmp_path / "mac-home"
    log_dir = mac_home / "logs"
    backup_root = mac_home / "backups"
    log_dir.mkdir(parents=True)
    backup_root.mkdir(parents=True)

    source = tmp_path / "source"
    source_backup = backup_root / "source.old"
    venv = tmp_path / "venv"
    venv_backup = backup_root / "venv.old"
    _write_generation(source, "current")
    _write_generation(source_backup, "restored")
    _write_generation(venv, "current", python=True)
    _write_generation(venv_backup, "restored", python=True)

    bin_dir = mac_home / "bin"
    bin_backup = backup_root / "bin.old"
    _write_generation(bin_dir, "current")
    _write_generation(bin_backup, "restored")
    revision = mac_home / "deployed-source-revision"
    revision_backup = backup_root / "revision.old"
    revision.write_text(successor_revision + "\n", encoding="utf-8")
    revision.chmod(0o600)
    revision_backup.write_text(prior_revision + "\n", encoding="utf-8")
    revision_backup.chmod(0o600)
    env_file = mac_home / "mac.env"
    env_backup = backup_root / "mac.env.old"
    env_file.write_text(
        f"MAC_WORKER_DEPLOY_GENERATION={successor_generation}\n", encoding="utf-8"
    )
    env_file.chmod(0o600)
    env_backup.write_text(
        f"MAC_WORKER_DEPLOY_GENERATION={prior_generation}\n", encoding="utf-8"
    )
    env_backup.chmod(0o600)
    openclaw_home = mac_home / "openclaw"
    _write_generation(openclaw_home, "current")
    (openclaw_home / "managed").mkdir()
    (openclaw_home / "managed" / "sandbox-name").write_text(
        "successor-sandbox\n", encoding="utf-8"
    )
    (openclaw_home / "sandbox-live").touch()
    installer = source / "deploy" / "openclaw" / "install-openclaw-gateway.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "[ \"${1:-}\" = withdraw ]\n"
        "[ \"$(<\"$MAC_SRC/generation\")\" = current ]\n"
        "[ \"$(<\"$MAC_HOME/openclaw/generation\")\" = current ]\n"
        "[ -f \"$MAC_HOME/openclaw/sandbox-live\" ]\n"
        "[ \"${ROLLBACK_TEST_FAIL_WITHDRAW:-0}\" != 1 ]\n"
        "rm -f \"$MAC_HOME/openclaw/sandbox-live\"\n"
        ": > \"$ROLLBACK_TEST_WITHDRAW_MARKER\"\n",
        encoding="utf-8",
    )
    installer.chmod(0o700)

    config_dir = tmp_path / "supervisord"
    config_dir.mkdir()
    config = config_dir / "mac.conf"
    config_backup = backup_root / "mac.conf.old"
    config.write_text("current", encoding="utf-8")
    config_backup.write_text("restored", encoding="utf-8")

    helper = log_dir / "rollback-supervisor.py"
    helper.write_text(FAKE_SUPERVISOR_HELPER, encoding="utf-8")
    helper.chmod(0o600)
    lifecycle = log_dir / "launchd-lifecycle.sh"
    lifecycle.write_text(FAKE_ATOMIC_ARTIFACT_HELPER, encoding="utf-8")
    lifecycle.chmod(0o600)

    rollback = log_dir / "rollback-test.sh"
    latest = log_dir / "rollback-latest.sh"
    rollback_intent = log_dir / "rollback-intent.json"
    manifest_post = log_dir / "deploy-manifest-post.json"
    completion_receipt = log_dir / "rollback-completion.json"
    values = {
        "MAC_HOME": str(mac_home),
        "MAC_PORT": "65534",
        "SRC_DIR": str(source),
        "VENV": str(venv),
        "HERMES_DIR": str(tmp_path / "hermes"),
        "OS_KIND": "linux",
        "SUPERVISOR_KIND": "supervisord",
        "FLEET_NAME": "mac",
        "DEPLOY_GENERATION": successor_generation,
        "DEPLOY_REV": successor_revision,
        "SRC_BACKUP": str(source_backup),
        "VENV_BACKUP": str(venv_backup),
        "HERMES_BACKUP": "",
        "BIN_BACKUP": str(bin_backup),
        "OPENCLAW_HOME_BACKUP": "",
        "OPENCLAW_HOME_EXISTED": "0",
        "MAC_UNIT_BACKUP": str(config_backup),
        "HERMES_UNIT_BACKUP": "",
        "MAC_AGENT_UNIT_BACKUP": "",
        "MAC_UNIT_MUTATED": "1",
        "HERMES_UNIT_MUTATED": "0",
        "MAC_AGENT_UNIT_MUTATED": "0",
        "MAC_PLIST_BACKUP": "",
        "MAC_PLIST_MUTATED": "0",
        "DARWIN_SYSTEM_PLIST_BACKUP": "",
        "DARWIN_SYSTEM_PLIST_MUTATED": "0",
        "DARWIN_SYSTEM_LAUNCHD_ACTIVE": "0",
        "DARWIN_GUI_LAUNCHD_ACTIVE": "0",
        "DARWIN_SYSTEM_SUPERVISOR_LABEL": "com.mac.supervisor",
        "DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP": "",
        "DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED": "0",
        "DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE": "0",
        "HERMES_PLIST_BACKUP": "",
        "HERMES_PLIST_MUTATED": "0",
        "MAC_AGENT_PLIST_BACKUP": "",
        "MAC_AGENT_PLIST_MUTATED": "0",
        "ROLLBACK_SUPERVISOR_HELPER": str(helper),
        "ROLLBACK_SUPERVISOR_HELPER_SHA256": hashlib.sha256(
            helper.read_bytes()
        ).hexdigest(),
        "ROLLBACK_LAUNCHD_LIFECYCLE": str(lifecycle),
        "ROLLBACK_LAUNCHD_LIFECYCLE_SHA256": hashlib.sha256(
            lifecycle.read_bytes()
        ).hexdigest(),
        "MAC_SERVICE_NAME": "mac.service",
        "HERMES_SERVICE_NAME": "mac-hermes-gateway.service",
        "OPENCLAW_SERVICE_NAME": "mac-openclaw-gateway.service",
        "NEMOCLAW_SERVICE_NAME": "mac-nemoclaw-gateway.service",
        "MAC_AGENT_SERVICE_NAME": "mac-agent.service",
        "MAC_GEN_SERVICE_NAME": "mac-gen-server.service",
        "MAC_GEN_AUDIO_SERVICE_NAME": "mac-gen-audio-server.service",
        "MAC_GEN_VIDEO_SERVICE_NAME": "mac-gen-video-server.service",
        "MAC_LAUNCHD_LABEL": "com.mac.control-plane",
        "HERMES_LAUNCHD_LABEL": "com.mac.hermes-gateway",
        "OPENCLAW_LAUNCHD_LABEL": "com.mac.openclaw-gateway",
        "NEMOCLAW_LAUNCHD_LABEL": "com.mac.nemoclaw-gateway",
        "MAC_AGENT_LAUNCHD_LABEL": "com.mac.agent",
        "MAC_SUPERVISORD_PROG": "mac",
        "HERMES_SUPERVISORD_PROG": "mac-hermes-gateway",
        "OPENCLAW_SUPERVISORD_PROG": "mac-openclaw-gateway",
        "NEMOCLAW_SUPERVISORD_PROG": "mac-nemoclaw-gateway",
        "AGENT_SUPERVISORD_PROG": "mac-agent",
        "MAC_SUPERVISORD_CONF_NAME": config.name,
        "LOG_DIR": str(log_dir),
        "ROLLBACK_SCRIPT": str(rollback),
        "ROLLBACK_LATEST": str(latest),
        "DEPLOY_TS": "20260719T120000Z",
        "ROLLBACK_ACTIVE_GATEWAY": "hermes",
        "ROLLBACK_AGENT_PRIOR_STATE": "active",
        "ROLLBACK_PRIOR_GENERATION": prior_generation,
        "ROLLBACK_PRIOR_REVISION": prior_revision,
        "ROLLBACK_INTENT": str(rollback_intent),
        "ROLLBACK_COMPLETION_RECEIPT": str(completion_receipt),
    }
    generator = tmp_path / "generate-rollback.sh"
    generator.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + _shell_assignments(values)
        + "\nROLLBACK_AUX_ARTIFACT_COUNT=2\n"
        + f"ROLLBACK_AUX_ARTIFACT_PATHS=({shlex.quote(str(revision))})\n"
        + f"ROLLBACK_AUX_ARTIFACT_BACKUPS=({shlex.quote(str(revision_backup))})\n"
        + "ROLLBACK_AUX_ARTIFACT_EXISTED=(1)\n"
        + "ROLLBACK_AUX_ARTIFACT_MODES=(user)\n"
        + f"ROLLBACK_AUX_ARTIFACT_PATHS[1]={shlex.quote(str(env_file))}\n"
        + f"ROLLBACK_AUX_ARTIFACT_BACKUPS[1]={shlex.quote(str(env_backup))}\n"
        + "ROLLBACK_AUX_ARTIFACT_EXISTED[1]=1\n"
        + "ROLLBACK_AUX_ARTIFACT_MODES[1]=user\n"
        + f". {shlex.quote(str(lifecycle))}\n"
        + "\ncontrol_plane_enabled() { "
        + ("return 0" if control_active else "return 1")
        + "; }\n"
        + f"supervisord_conf_dir() {{ printf '%s\\n' {shlex.quote(str(config_dir))}; }}\n"
        + _rollback_function()
        + "write_rollback_script\n",
        encoding="utf-8",
    )
    generated = subprocess.run(
        ["/bin/bash", str(generator)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    assert rollback.exists()
    rollback_intent.write_text(
        json.dumps(
            {
                "schema": "mac.fleet_node_rollback_intent.v1",
                "status": "armed",
                "generation": successor_generation,
                "revision": successor_revision,
                "prior_generation": prior_generation,
                "prior_revision": prior_revision,
                "rollback_capable": True,
                "prior_topology": {
                    "supervisor": "supervisord",
                    "active_gateway": "hermes",
                    "agent_prior_state": "active",
                },
                "rollback": {
                    "path": str(rollback),
                    "sha256": hashlib.sha256(rollback.read_bytes()).hexdigest(),
                    "completion_receipt": str(completion_receipt),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rollback_intent.chmod(0o600)
    return rollback, {
        "source": source,
        "source_backup": source_backup,
        "venv": venv,
        "venv_backup": venv_backup,
        "bin": bin_dir,
        "bin_backup": bin_backup,
        "revision": revision,
        "revision_backup": revision_backup,
        "env": env_file,
        "env_backup": env_backup,
        "openclaw_home": openclaw_home,
        "config": config,
        "config_backup": config_backup,
        "log": tmp_path / "supervisor-events.jsonl",
        "control": tmp_path / "control-active",
        "hermes": tmp_path / "hermes-active",
        "agent": tmp_path / "agent-active",
        "withdraw": tmp_path / "openclaw-withdrawn",
        "supervisor_helper": helper,
        "lifecycle_helper": lifecycle,
        "manifest_post": manifest_post,
        "intent": rollback_intent,
        "completion_receipt": completion_receipt,
        "successor_revision": Path(successor_revision),
        "prior_revision": Path(prior_revision),
        "successor_generation": Path(successor_generation),
        "prior_generation": Path(prior_generation),
    }


def _rollback_env(paths: Mapping[str, Path]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        ROLLBACK_TEST_SRC=str(paths["source"]),
        ROLLBACK_TEST_VENV=str(paths["venv"]),
        ROLLBACK_TEST_BIN=str(paths["bin"]),
        ROLLBACK_TEST_REVISION=str(paths["revision"]),
        ROLLBACK_TEST_CURRENT_REVISION=str(paths["successor_revision"]),
        ROLLBACK_TEST_PRIOR_REVISION=str(paths["prior_revision"]),
        ROLLBACK_TEST_OPENCLAW_HOME=str(paths["openclaw_home"]),
        ROLLBACK_TEST_CONF=str(paths["config"]),
        ROLLBACK_TEST_LOG=str(paths["log"]),
        ROLLBACK_TEST_CONTROL_MARKER=str(paths["control"]),
        ROLLBACK_TEST_HERMES_MARKER=str(paths["hermes"]),
        ROLLBACK_TEST_AGENT_MARKER=str(paths["agent"]),
        ROLLBACK_TEST_WITHDRAW_MARKER=str(paths["withdraw"]),
    )
    return env


def _events(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _make_prior_interruption_state(
    paths: Mapping[str, Path], *, source_already_moved: bool
) -> None:
    prior_revision = str(paths["prior_revision"])
    prior_generation = str(paths["prior_generation"])
    paths["revision"].write_text(prior_revision + "\n", encoding="utf-8")
    paths["env"].write_text(
        f"MAC_WORKER_DEPLOY_GENERATION={prior_generation}\n", encoding="utf-8"
    )
    paths["bin"].joinpath("generation").write_text("restored", encoding="utf-8")
    paths["config"].write_text("restored", encoding="utf-8")
    subprocess.run(["/bin/rm", "-rf", str(paths["openclaw_home"])], check=True)
    subprocess.run(["/bin/rm", "-rf", str(paths["venv_backup"])], check=True)
    paths["venv"].joinpath("generation").write_text("restored", encoding="utf-8")
    if source_already_moved:
        subprocess.run(["/bin/rm", "-rf", str(paths["source"])], check=True)
    else:
        subprocess.run(["/bin/rm", "-rf", str(paths["source_backup"])], check=True)
        paths["source"].joinpath("generation").write_text("restored", encoding="utf-8")


def _finalize_case(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    generation = "generation-rocky-finalize-001"
    revision = "d" * 40
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    intent = log_dir / "intent.json"
    post = log_dir / "post.json"
    receipt = log_dir / "finalize.json"
    rollback_path = log_dir / "rollback.sh"
    rollback_sha = "e" * 64
    intent.write_text(
        json.dumps(
            {
                "schema": "mac.fleet_node_rollback_intent.v1",
                "status": "armed",
                "generation": generation,
                "revision": revision,
                "rollback": {"path": str(rollback_path), "sha256": rollback_sha},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    intent.chmod(0o600)
    intent_sha = hashlib.sha256(intent.read_bytes()).hexdigest()
    post.write_text(
        json.dumps(
            {
                "stage": "post",
                "deploy": {"generation": generation, "mac_git_rev": revision},
                "rollback": {
                    "status": "armed",
                    "authority": "pre_mutation_intent",
                    "path": str(rollback_path),
                    "sha256": rollback_sha,
                    "intent": {"path": str(intent), "sha256": intent_sha},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    post.chmod(0o600)
    harness = tmp_path / "finalize.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + _shell_assignments(
            {
                "FINALIZE_RECEIPT": str(receipt),
                "MANIFEST_POST": str(post),
                "ROLLBACK_INTENT": str(intent),
                "AGENT": "rocky",
                "FLEET_NAME": "mac",
                "DEPLOY_GENERATION": generation,
                "DEPLOY_REV": revision,
                "PY": sys.executable,
            }
        )
        + "\n"
        + _finalize_function()
        + "write_phase2_finalize_receipt\n",
        encoding="utf-8",
    )
    return harness, {"intent": intent, "post": post, "receipt": receipt}


def test_generated_rollback_has_one_bounded_supervisor_protocol_and_ordered_barriers(
    tmp_path: Path,
) -> None:
    rollback, _paths = _generate_rollback(tmp_path, control_active=True)
    script = rollback.read_text(encoding="utf-8")

    syntax = subprocess.run(
        ["/bin/bash", "-n", str(rollback)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    preflight = script.index(
        'SRC_ROLLBACK_STATE="$(rollback_directory_state "$SRC_BACKUP" "$SRC_DIR")"'
    )
    quiesce = script.index(
        'python "$ROLLBACK_SUPERVISOR_HELPER" '
        '"$ROLLBACK_SUPERVISOR_HELPER_SHA256" quiesce'
    )
    sandbox_withdraw = script.index(
        'mac_run_bounded 360 "$current_openclaw_installer" withdraw'
    )
    source_restore = script.index(
        'restore_dir_or_keep_prior "$SRC_BACKUP" "$SRC_DIR" "$SRC_ROLLBACK_STATE"'
    )
    service_restore = script.index('restore_file_or_remove "$MAC_UNIT_BACKUP"')
    supervisor_restore = script.index(
        'python "$ROLLBACK_SUPERVISOR_HELPER" '
        '"$ROLLBACK_SUPERVISOR_HELPER_SHA256" restore'
    )
    assert (
        preflight
        < quiesce
        < sandbox_withdraw
        < source_restore
        < service_restore
        < supervisor_restore
    )

    assert script.count('--supervisor "$rollback_supervisor"') == 2
    assert 'rollback_supervisor="${SUPERVISOR_KIND:-$OS_KIND}"' in script
    assert '*) echo "rollback failed: unsupported supervisor"' in script
    assert re.search(
        r"(?m)^\s*(?:sudo\s+-n\s+)?(?:systemctl|supervisorctl|launchctl)\b",
        script,
    ) is None
    assert "SECONDS" not in script
    # Fixed-count reverse journal walks are local artifact compensation, not
    # unbounded supervisor polling.
    assert "while :" not in script
    assert re.search(r"(?m)^\s*until\b", script) is None
    assert re.search(r"(?m)^\s*sleep\b", script) is None
    assert re.search(r"(?m)^\s*sleep\b", script) is None

    assert 'command cp -a "$backup" "$stage"' in script
    assert 'mv -f "$dest" "$current_backup"' in script
    assert 'if ! mv -f "$stage" "$dest"; then' in script
    assert 'mv -f "$current_backup" "$dest"' in script
    assert 'mac_launchd_atomic_restore "$backup" "$destination" "$mode"' in script
    assert "ROLLBACK_INTENT=" in script
    assert "MANIFEST_POST" not in script
    assert "post_manifest_sha256" not in script


def test_generated_rollback_recovers_after_sigkill_at_intent_boundary_without_post_manifest(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    assert paths["intent"].is_file()
    assert not paths["manifest_post"].exists()
    _make_prior_interruption_state(paths, source_already_moved=False)

    crashed = subprocess.run(
        ["/bin/bash", "-c", "kill -KILL $$"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert crashed.returncode < 0

    env = _rollback_env(paths)
    env.update(
        ROLLBACK_TEST_QUIESCE_SOURCE_STATE="restored",
        ROLLBACK_TEST_QUIESCE_VENV_STATE="restored",
        ROLLBACK_TEST_QUIESCE_CONFIG_STATE="restored",
        ROLLBACK_TEST_QUIESCE_BIN_STATE="restored",
        ROLLBACK_TEST_QUIESCE_REVISION=str(paths["prior_revision"]),
        ROLLBACK_TEST_QUIESCE_OPENCLAW="absent",
    )
    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not paths["withdraw"].exists()
    assert paths["source"].joinpath("generation").read_text() == "restored"
    assert paths["venv"].joinpath("generation").read_text() == "restored"
    receipt = json.loads(paths["completion_receipt"].read_text(encoding="utf-8"))
    assert receipt["intent_sha256"] == hashlib.sha256(
        paths["intent"].read_bytes()
    ).hexdigest()


def test_generated_rollback_recovers_after_sigkill_between_source_and_venv_rename(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=False)
    _make_prior_interruption_state(paths, source_already_moved=True)
    assert paths["source_backup"].is_dir()
    assert not paths["source"].exists()
    assert paths["venv"].is_dir()
    assert not paths["venv_backup"].exists()

    env = _rollback_env(paths)
    env.update(
        ROLLBACK_TEST_QUIESCE_SOURCE_STATE="absent",
        ROLLBACK_TEST_QUIESCE_VENV_STATE="restored",
        ROLLBACK_TEST_QUIESCE_CONFIG_STATE="restored",
        ROLLBACK_TEST_QUIESCE_BIN_STATE="restored",
        ROLLBACK_TEST_QUIESCE_REVISION=str(paths["prior_revision"]),
        ROLLBACK_TEST_QUIESCE_OPENCLAW="absent",
    )
    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert paths["source"].joinpath("generation").read_text() == "restored"
    assert paths["venv"].joinpath("generation").read_text() == "restored"


def test_generated_rollback_rejects_tampered_intent_before_quiescence(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    intent = json.loads(paths["intent"].read_text(encoding="utf-8"))
    intent["generation"] = "unrelated-generation"
    paths["intent"].write_text(json.dumps(intent) + "\n", encoding="utf-8")

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode != 0
    assert "pre-mutation intent contract is invalid" in result.stderr
    assert not paths["log"].exists()
    assert paths["source"].joinpath("generation").read_text() == "current"


def test_generated_rollback_rejects_tampered_executable_bound_by_intent(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    with rollback.open("a", encoding="utf-8") as stream:
        stream.write("# hostile append\n")
    rollback.chmod(0o700)

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode != 0
    assert "pre-mutation intent contract is invalid" in result.stderr
    assert not paths["log"].exists()


def test_finalize_is_idempotent_and_binds_post_evidence_to_pre_mutation_intent(
    tmp_path: Path,
) -> None:
    harness, paths = _finalize_case(tmp_path)
    first = subprocess.run(
        ["/bin/bash", str(harness)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    receipt_raw = paths["receipt"].read_bytes()
    receipt = json.loads(receipt_raw)
    assert receipt["schema"] == "mac.fleet_node_finalize.v1"
    assert receipt["status"] == "finalized"
    assert receipt["post_manifest"]["sha256"] == hashlib.sha256(
        paths["post"].read_bytes()
    ).hexdigest()
    assert receipt["rollback_intent"]["sha256"] == hashlib.sha256(
        paths["intent"].read_bytes()
    ).hexdigest()

    replay = subprocess.run(
        ["/bin/bash", str(harness)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout.encode() == receipt_raw


def test_finalize_rejects_tampered_post_manifest_and_preserves_receipt(
    tmp_path: Path,
) -> None:
    harness, paths = _finalize_case(tmp_path)
    first = subprocess.run(
        ["/bin/bash", str(harness)], capture_output=True, text=True, check=False
    )
    assert first.returncode == 0, first.stderr
    receipt_raw = paths["receipt"].read_bytes()
    post = json.loads(paths["post"].read_text(encoding="utf-8"))
    post["deploy"]["generation"] = "wrong-generation"
    paths["post"].write_text(json.dumps(post) + "\n", encoding="utf-8")

    tampered = subprocess.run(
        ["/bin/bash", str(harness)], capture_output=True, text=True, check=False
    )
    assert tampered.returncode != 0
    assert "does not finalize the armed generation" in tampered.stderr
    assert paths["receipt"].read_bytes() == receipt_raw


def test_installer_snapshots_complete_entrypoint_and_generation_metadata_before_bootstrap_mutation() -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    arm_call = source.index("\narm_phase2_rollback\n")
    pre_manifest = source.index('\nwrite_deploy_manifest "pre" "$MANIFEST_PRE"\n')
    artifact_backup = source.index("\nbackup_existing_artifacts\n", arm_call)
    source_replace = source.index("\nlog \"installing mac source\"\n")
    assert arm_call < pre_manifest < artifact_backup < source_replace

    capture = source.split("capture_auxiliary_rollback_artifacts() {", 1)[1].split(
        "\n}\n\nwrite_rollback_script() {", 1
    )[0]
    assert "snapshot_bin_directory_for_rollback" in capture
    assert 'track_auxiliary_rollback_artifact "$ENV_FILE" user' in capture
    assert 'track_auxiliary_rollback_artifact "$MAC_HOME/fleets.yaml" user' in capture
    assert '"$MAC_HOME/deployed-source-revision" user' in capture
    assert '"$MAC_HOME/deploy-start-barrier" user' in capture
    assert '"$OPENCLAW_SERVICE_NAME"' in capture
    assert '"$NEMOCLAW_SERVICE_NAME"' in capture
    assert '"$MAC_AGENT_SERVICE_NAME"' in capture
    assert '"$OPENCLAW_LAUNCHD_LABEL"' in capture
    assert '"$NEMOCLAW_LAUNCHD_LABEL"' in capture
    assert '"$MAC_AGENT_LAUNCHD_LABEL"' in capture
    assert '"$(supervisord_conf_dir)/$MAC_SUPERVISORD_CONF_NAME" system' in capture


def test_installer_arms_rollback_only_after_mutable_snapshots_and_durable_regeneration() -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    arm_body = source.split("arm_phase2_rollback() {", 1)[1].split(
        "\n}\n\nbackup_existing_artifacts() {", 1
    )[0]
    mutable_snapshot = arm_body.index("capture_mutable_runtime_state_for_rollback")
    auxiliary_snapshot = arm_body.index("capture_auxiliary_rollback_artifacts")
    rollback_publish = arm_body.index("write_rollback_script")
    intent_publish = arm_body.index("write_phase2_rollback_intent")
    rollback_arm = arm_body.rindex("DEPLOY_ROLLBACK_ARMED=1")
    assert (
        mutable_snapshot
        < auxiliary_snapshot
        < rollback_publish
        < intent_publish
        < rollback_arm
    )
    sealed_load = arm_body.index("load_existing_phase2_rollback_state")
    existing_verify = arm_body.index("verify_phase2_rollback_intent", sealed_load)
    assert sealed_load < existing_verify

    main_arm = source.index("\narm_phase2_rollback\n")
    pre_manifest = source.index('\nwrite_deploy_manifest "pre" "$MANIFEST_PRE"\n')
    typed_arm_exit = source.index('if [ "$NODE_ACTION" = arm-phase2 ]; then', main_arm)
    artifact_move = source.index("\nbackup_existing_artifacts\n", typed_arm_exit)
    assert main_arm < pre_manifest < typed_arm_exit < artifact_move

    backup_body = source.split("backup_existing_artifacts() {", 1)[1].split(
        "\n}\n\ncapture_darwin_launchd_prestate() {", 1
    )[0]
    assert backup_body.index('mac_launchd_fsync_directory "$MAC_HOME/backups" user') < (
        backup_body.index("verify_phase2_rollback_intent")
    )
    assert "write_rollback_script" not in backup_body
    rollback_body = source.split("write_rollback_script() {", 1)[1].split(
        "\n}\n\nverify_phase2_rollback_intent() {", 1
    )[0]
    rollback_publish = rollback_body.rindex(
        'mac_launchd_atomic_replace \\\n    "$rollback_stage" "$ROLLBACK_SCRIPT"'
    )
    rollback_alias_publish = rollback_body.rindex(
        'mac_launchd_atomic_restore "$ROLLBACK_SCRIPT" "$ROLLBACK_LATEST"'
    )
    assert rollback_publish < rollback_alias_publish


def test_typed_synchronized_apply_consumes_receipts_and_skips_legacy_quiescence() -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    assert 'NODE_ACTION="${1:-${MAC_DEPLOY_NODE_ACTION:-legacy-one-shot}}"' in source
    main = source[source.index('\nlog "deploy log: $DEPLOY_LOG"\n') :]
    typed_gate = main.index(
        'if [ "$NODE_ACTION" = arm-phase2 ] || [ "$NODE_ACTION" = apply-phase2 ]'
    )
    receipt = main.index("validate_typed_prerequisite_bundle", typed_gate)
    rollback_arm = main.index("\narm_phase2_rollback\n", receipt)
    typed_quiescence = main.index(
        "typed phase 2 is consuming the journal-bound phase-1 quiescence proof",
        rollback_arm,
    )
    artifact_move = main.index("\nbackup_existing_artifacts\n", typed_quiescence)
    assert typed_gate < receipt < rollback_arm < typed_quiescence < artifact_move
    legacy = main[typed_gate:rollback_arm]
    assert legacy.index("else") < legacy.index("ensure_dns_resolution")
    optional = main.index('if [ "$NODE_ACTION" = legacy-one-shot ]; then', artifact_move)
    assert "install_gpu_gen_server" in main[optional:]


def test_injected_mutable_snapshot_failure_leaves_prior_generation_roots_untouched(
    tmp_path: Path,
) -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    function = source.split("capture_mutable_runtime_state_for_rollback() {", 1)[1].split(
        "\n}\n\ncapture_auxiliary_rollback_artifacts() {", 1
    )[0]
    mac_home = tmp_path / "mac-home"
    src = mac_home / "src" / "mac"
    venv = mac_home / "venv"
    openclaw = mac_home / "openclaw"
    for root, marker in ((src, "source"), (venv, "venv"), (openclaw, "openclaw")):
        root.mkdir(parents=True)
        (root / "generation").write_text(marker, encoding="utf-8")
    harness = tmp_path / "snapshot-failure.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + _shell_assignments(
            {
                "MAC_HOME": str(mac_home),
                "SRC_DIR": str(src),
                "VENV": str(venv),
                "AGENT": "worker",
                "DEPLOY_TS": "20260719T120000Z",
                "ROLLBACK_ACTIVE_GATEWAY": "openclaw",
                "OPENCLAW_HOME_EXISTED": "0",
                "OPENCLAW_HOME_BACKUP": "",
                "MAC_DEPLOY_TEST_INJECT_OPENCLAW_SNAPSHOT_FAILURE": "1",
            }
        )
        + "\ndie() { printf '%s\\n' \"$*\" >&2; exit 73; }\n"
        + "snapshot_rollback_directory() { exit 99; }\n"
        + "capture_mutable_runtime_state_for_rollback() {"
        + function
        + "\n}\n"
        + "capture_mutable_runtime_state_for_rollback\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", str(harness)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 73
    assert "injected OpenClaw rollback snapshot failure" in result.stderr
    assert (src / "generation").read_text(encoding="utf-8") == "source"
    assert (venv / "generation").read_text(encoding="utf-8") == "venv"
    assert (openclaw / "generation").read_text(encoding="utf-8") == "openclaw"
    assert not (mac_home / "backups").exists()


def test_generated_rollback_preflight_fails_before_quiesce_or_mutation(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    missing = paths["venv_backup"]
    subprocess.run(["/bin/rm", "-rf", str(missing)], check=True)

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "neither a durable backup nor the untouched prior directory" in result.stderr
    assert not paths["log"].exists()
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"
    assert paths["config"].read_text() == "current"


def test_generated_rollback_rejects_mutated_supervisor_contract_before_quiescence(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    with paths["supervisor_helper"].open("a", encoding="utf-8") as stream:
        stream.write("\n# tampered after rollback publication\n")

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "rollback contract hash or identity changed" in result.stderr
    assert not paths["log"].exists()
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"
    assert (paths["openclaw_home"] / "generation").read_text() == "current"
    assert paths["config"].read_text() == "current"


def test_generated_rollback_rejects_mutated_lifecycle_contract_before_artifact_swap(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    with paths["lifecycle_helper"].open("a", encoding="utf-8") as stream:
        stream.write("\n# tampered after rollback publication\n")

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "rollback contract hash or identity changed" in result.stderr
    assert [event["action"] for event in _events(paths["log"])] == ["quiesce"]
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"
    assert (paths["openclaw_home"] / "generation").read_text() == "current"
    assert (paths["openclaw_home"] / "sandbox-live").exists()
    assert paths["config"].read_text() == "current"


@pytest.mark.parametrize("malformed_hash", ["a" * 63, "b" * 65])
def test_generated_rollback_rejects_short_or_long_embedded_contract_hashes(
    tmp_path: Path,
    malformed_hash: str,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    script = rollback.read_text(encoding="utf-8")
    script, substitutions = re.subn(
        r"(?m)^ROLLBACK_SUPERVISOR_HELPER_SHA256='[0-9a-f]{64}'$",
        f"ROLLBACK_SUPERVISOR_HELPER_SHA256='{malformed_hash}'",
        script,
    )
    assert substitutions == 1
    rollback.write_text(script, encoding="utf-8")
    rollback.chmod(0o700)

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "retained contract hash is malformed" in result.stderr
    assert not paths["log"].exists()
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"


def test_generated_rollback_requires_successor_sandbox_withdrawal_before_artifact_swap(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    env = _rollback_env(paths)
    env["ROLLBACK_TEST_FAIL_WITHDRAW"] = "1"

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"
    assert (paths["bin"] / "generation").read_text() == "current"
    assert (paths["openclaw_home"] / "sandbox-live").exists()
    assert not paths["withdraw"].exists()
    assert [event["action"] for event in _events(paths["log"])] == ["quiesce"]


@pytest.mark.parametrize("control_active", [False, True])
def test_generated_rollback_restores_artifacts_before_exact_prior_topology(
    tmp_path: Path,
    control_active: bool,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=control_active)

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = _events(paths["log"])
    expected_mode = "active" if control_active else "inactive"
    assert events == [
        {
            "action": "quiesce",
            "config": "current",
            "mode": expected_mode,
            "source": "current",
            "supervisor": "supervisord",
            "venv": "current",
        },
        {
            "action": "restore",
            "config": "restored",
            "mode": expected_mode,
            "source": "restored",
            "supervisor": "supervisord",
            "venv": "restored",
        },
    ]
    assert (paths["source"] / "generation").read_text() == "restored"
    assert (paths["venv"] / "generation").read_text() == "restored"
    assert (paths["bin"] / "generation").read_text() == "restored"
    assert paths["revision"].read_text().strip() == str(paths["prior_revision"])
    assert not paths["openclaw_home"].exists()
    assert paths["withdraw"].exists()
    assert paths["config"].read_text() == "restored"
    assert paths["hermes"].read_text() == "active"
    assert paths["agent"].read_text() == "active"
    assert paths["control"].exists() is control_active
    completion = json.loads(paths["completion_receipt"].read_text(encoding="utf-8"))
    assert completion["schema"] == "mac.fleet_node_rollback.v1"
    assert completion["status"] == "restored"
    assert completion["generation"] == "successor-generation-rocky-001"
    assert completion["revision"] == str(paths["successor_revision"])
    assert completion["prior_generation"] == "prior-generation-rocky-000"
    assert completion["prior_revision"] == str(paths["prior_revision"])
    assert len(completion["intent_sha256"]) == 64
    assert completion["intent_sha256"] == hashlib.sha256(
        paths["intent"].read_bytes()
    ).hexdigest()
    assert len(completion["prior_topology_proof"]["sha256"]) == 64
    assert not list((tmp_path / "mac-home" / "backups").glob("rollback-current.*"))
    assert not list(
        (tmp_path / "mac-home" / "backups").glob("rollback-current-file.*")
    )


def test_completed_generated_rollback_replay_returns_same_receipt_without_mutation(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    first = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    receipt = paths["completion_receipt"].read_bytes()
    events = paths["log"].read_bytes()

    second = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert second.stdout.encode() == receipt
    assert paths["completion_receipt"].read_bytes() == receipt
    assert paths["log"].read_bytes() == events
    assert (paths["source"] / "generation").read_text() == "restored"


def test_generated_rollback_refuses_an_unrelated_current_generation_before_quiescence(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    paths["env"].write_text(
        "MAC_WORKER_DEPLOY_GENERATION=unrelated-successor-generation\n",
        encoding="utf-8",
    )
    paths["env"].chmod(0o600)

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=_rollback_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "current node generation is outside this rollback contract" in result.stderr
    assert not paths["log"].exists()
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"


def test_generated_rollback_cleanup_residue_does_not_invalidate_a_restored_generation(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    env = _rollback_env(paths)
    env["ROLLBACK_TEST_FAIL_CLEANUP"] = "file"

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "stale cleanup snapshots remain" in result.stderr
    assert (paths["source"] / "generation").read_text() == "restored"
    assert (paths["venv"] / "generation").read_text() == "restored"
    assert (paths["bin"] / "generation").read_text() == "restored"
    assert paths["revision"].read_text().strip() == str(paths["prior_revision"])
    assert not paths["openclaw_home"].exists()
    assert paths["config"].read_text() == "restored"
    assert list(
        (tmp_path / "mac-home" / "backups").glob("rollback-current-file.*")
    )


def test_generated_rollback_compensates_bin_metadata_and_service_files_when_restore_fails(
    tmp_path: Path,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    env = _rollback_env(paths)
    env["ROLLBACK_TEST_FAIL_RESTORE"] = "1"

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "injected supervisor restore failure" in result.stderr
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"
    assert (paths["bin"] / "generation").read_text() == "current"
    assert paths["revision"].read_text().strip() == str(paths["successor_revision"])
    assert (paths["openclaw_home"] / "generation").read_text() == "current"
    assert paths["config"].read_text() == "current"
    assert [event["action"] for event in _events(paths["log"])] == ["quiesce"]


@pytest.mark.parametrize("failure_key", ["source", "venv"])
def test_generated_rollback_compensates_a_failed_directory_swap_and_prior_swaps(
    tmp_path: Path,
    failure_key: str,
) -> None:
    rollback, paths = _generate_rollback(tmp_path, control_active=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "args=(\"$@\")\n"
        "source_index=0\n"
        "[ \"${args[0]:-}\" != -f ] || source_index=1\n"
        "source=${args[$source_index]}\n"
        "destination=${args[$((source_index + 1))]}\n"
        "if [[ \"$source\" == *'.rollback-stage.'* ]] "
        "&& [ \"$destination\" = \"$ROLLBACK_TEST_FAIL_DEST\" ] "
        "&& [ ! -e \"$ROLLBACK_TEST_FAIL_MARKER\" ]; then\n"
        "  : > \"$ROLLBACK_TEST_FAIL_MARKER\"\n"
        "  exit 73\n"
        "fi\n"
        "exec /bin/mv \"$@\"\n",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    env = _rollback_env(paths)
    env.update(
        PATH=str(fake_bin) + os.pathsep + env.get("PATH", ""),
        ROLLBACK_TEST_FAIL_DEST=str(paths[failure_key]),
        ROLLBACK_TEST_FAIL_MARKER=str(tmp_path / "injected-swap-failure"),
    )

    result = subprocess.run(
        ["/bin/bash", str(rollback)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert (tmp_path / "injected-swap-failure").exists()
    assert (paths["source"] / "generation").read_text() == "current"
    assert (paths["venv"] / "generation").read_text() == "current"
    assert not list(tmp_path.glob("source.rollback-stage.*"))
    assert not list(tmp_path.glob("venv.rollback-stage.*"))
    assert [event["action"] for event in _events(paths["log"])] == ["quiesce"]
