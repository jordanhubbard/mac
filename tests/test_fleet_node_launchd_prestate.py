from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"
REVISION = "a" * 40
GENERATION = "generation-rocky-001"
LABELS = (
    "com.mac.hermes-gateway",
    "com.mac.openclaw-gateway",
    "com.mac.nemoclaw-gateway",
    "com.mac.agent",
)


def _capture_function() -> str:
    source = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    start = source.index("capture_darwin_launchd_prestate() {")
    end = source.index("\n}\n\nsystem_launchd_job_is_loaded() {", start) + 2
    return source[start:end]


def _function(name: str, next_name: str) -> str:
    source = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    return (
        f"{name}() {{"
        + source.split(f"{name}() {{", 1)[1].split(
            f"\n}}\n\n{next_name}() {{", 1
        )[0]
        + "\n}"
    )


def _valid_receipt() -> dict[str, object]:
    uid = os.getuid()
    resources = []
    for label in LABELS:
        resources.extend(
            [
                {
                    "name": label,
                    "target": f"gui/{uid}/{label}",
                    "prior_state": "absent",
                    "state": "absent",
                },
                {
                    "name": label,
                    "target": f"system/{label}",
                    "prior_state": "absent",
                    "state": "absent",
                },
            ]
        )
    return {
        "schema": "mac.phase1_cohort_quiescence.v1",
        "agent": "rocky",
        "fleet": "mac",
        "os_kind": "darwin",
        "revision": REVISION,
        "generation": GENERATION,
        "supervisor": {"manager": "launchd", "resources": resources},
    }


def _resource(payload: dict[str, object], label: str, domain: str) -> dict[str, object]:
    resources = payload["supervisor"]["resources"]  # type: ignore[index]
    target_prefix = f"gui/{os.getuid()}/" if domain == "gui" else "system/"
    return next(
        item
        for item in resources  # type: ignore[union-attr]
        if item["name"] == label and item["target"] == target_prefix + label
    )


def _run_capture(tmp_path: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    mac_home = tmp_path / "mac-home"
    home = tmp_path / "home"
    (mac_home / "backups").mkdir(parents=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    receipt = mac_home / f"phase1-cohort-quiescence-{GENERATION}.json"
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    receipt.chmod(0o600)
    command = f"""set -euo pipefail
PY=${{TEST_PY:?}}
MAC_HOME=${{TEST_MAC_HOME:?}}
HOME=${{TEST_HOME:?}}
SUPERVISOR_KIND=launchd
AGENT=rocky
FLEET_NAME=mac
DEPLOY_REV={REVISION}
DEPLOY_GENERATION={GENERATION}
DEPLOY_TS=20260719T000000Z
ROLLBACK_INTENT="$MAC_HOME/logs/rollback-intent.json"
MAC_LAUNCHD_LABEL=com.mac.control-plane
DARWIN_SYSTEM_SUPERVISOR_LABEL=com.mac.supervisor
HERMES_LAUNCHD_LABEL={LABELS[0]}
OPENCLAW_LAUNCHD_LABEL={LABELS[1]}
NEMOCLAW_LAUNCHD_LABEL={LABELS[2]}
MAC_AGENT_LAUNCHD_LABEL={LABELS[3]}
DARWIN_SYSTEM_LAUNCHD_ACTIVE=0
DARWIN_GUI_LAUNCHD_ACTIVE=0
DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=0
DARWIN_HERMES_LAUNCHD_ACTIVE=0
DARWIN_OPENCLAW_LAUNCHD_ACTIVE=0
DARWIN_NEMOCLAW_LAUNCHD_ACTIVE=0
DARWIN_AGENT_LAUNCHD_ACTIVE=0
log() {{ printf '%s\n' "$*" >&2; }}
sudo() {{
  if [ "${{1:-}}" = -n ]; then shift; fi
  case "${{1:-}}" in
    true) return 0 ;;
    test) return 1 ;;
    *) return 64 ;;
  esac
}}
system_launchd_job_is_loaded() {{ return 1; }}
gui_launchd_job_is_loaded() {{ return 1; }}
write_rollback_script() {{ :; }}
{_capture_function()}
capture_darwin_launchd_prestate
printf 'states=%s,%s,%s,%s\n' \
  "$DARWIN_HERMES_LAUNCHD_ACTIVE" \
  "$DARWIN_OPENCLAW_LAUNCHD_ACTIVE" \
  "$DARWIN_NEMOCLAW_LAUNCHD_ACTIVE" \
  "$DARWIN_AGENT_LAUNCHD_ACTIVE"
"""
    return subprocess.run(
        ["/bin/bash", "-c", command],
        env={
            **os.environ,
            "TEST_PY": sys.executable,
            "TEST_MAC_HOME": str(mac_home),
            "TEST_HOME": str(home),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_launchd_prestate_consumer_recovers_exact_prior_activity(tmp_path: Path) -> None:
    payload = _valid_receipt()
    _resource(payload, LABELS[0], "gui")["prior_state"] = "active"
    _resource(payload, LABELS[3], "gui")["prior_state"] = "active"

    result = _run_capture(tmp_path, payload)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "states=1,0,0,1\n"


@pytest.mark.parametrize("same_content", [True, False])
def test_armed_phase2_reuses_only_an_exact_private_rollback_snapshot(
    tmp_path: Path, same_content: bool
) -> None:
    source = tmp_path / "service.plist"
    backup = tmp_path / "service.backup.plist"
    intent = tmp_path / "rollback-intent.json"
    source.write_text("prior service\n", encoding="utf-8")
    backup.write_text(
        "prior service\n" if same_content else "different service\n",
        encoding="utf-8",
    )
    backup.chmod(0o600)
    intent.write_text("{}\n", encoding="utf-8")
    intent.chmod(0o600)
    verify = _function(
        "verify_armed_rollback_file_snapshot", "snapshot_rollback_file"
    )
    snapshot = _function("snapshot_rollback_file", "track_auxiliary_rollback_artifact")
    command = f"""set -euo pipefail
PY=${{TEST_PY:?}}
ROLLBACK_INTENT=${{TEST_INTENT:?}}
mac_launchd_run_python_bounded() {{
  local mode="$1" timeout="$2" program="$3"
  shift 3
  : "$mode" "$timeout"
  "$PY" -c "$program" "$@"
}}
mac_launchd_snapshot_file() {{
  echo "unexpected fresh snapshot" >&2
  return 91
}}
die() {{ echo "$*" >&2; return 1; }}
log() {{ printf '%s\n' "$*" >&2; }}
{verify}
{snapshot}
snapshot_rollback_file "$1" "$2" user
"""
    result = subprocess.run(
        ["/bin/bash", "-c", command, "fixture", str(source), str(backup)],
        env={
            **os.environ,
            "TEST_PY": sys.executable,
            "TEST_INTENT": str(intent),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    if same_content:
        assert result.returncode == 0, result.stderr
        assert "reusing exact rollback snapshot" in result.stderr
    else:
        assert result.returncode != 0
        assert "differs from the current source" in result.stderr


def test_apply_phase_loads_prior_state_from_the_sealed_rollback_intent(
    tmp_path: Path,
) -> None:
    mac_home = tmp_path / "mac-home"
    logs = mac_home / "logs"
    logs.mkdir(parents=True)
    deploy_ts = "20260721T195306Z"
    agent = "rocky"
    src = mac_home / "src" / "mac"
    venv = mac_home / "venv"
    hermes = mac_home / "hermes-agent"
    src_backup = mac_home / "backups" / f"mac-src.{agent}.{deploy_ts}"
    venv_backup = mac_home / "backups" / f"venv.{agent}.{deploy_ts}"
    hermes_backup = mac_home / "backups" / f"hermes-agent.{agent}.{deploy_ts}"
    bin_backup = mac_home / "backups" / f"bin.{agent}.{deploy_ts}"
    openclaw_backup = mac_home / "backups" / f"openclaw.{agent}.{deploy_ts}"
    intent_path = logs / f"rollback-{deploy_ts}-intent.json"
    rollback_script = logs / f"rollback-{deploy_ts}.sh"
    rollback_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rollback_script.chmod(0o700)
    completion_receipt = logs / f"rollback-{deploy_ts}-completion.json"
    supervisor_helper = logs / "rollback-supervisor.py"
    lifecycle_helper = logs / "launchd-lifecycle.py"
    node_identity_sha256 = "1" * 64
    bundle_sha256 = "2" * 64
    expectations_sha256 = "3" * 64
    supervisor_helper_sha256 = "4" * 64
    lifecycle_helper_sha256 = "5" * 64
    intent = {
        "schema": "mac.fleet_node_rollback_intent.v1",
        "status": "armed",
        "agent": agent,
        "fleet": "mac",
        "os_kind": "darwin",
        "generation": GENERATION,
        "revision": REVISION,
        "prior_generation": "sealed-prior-generation",
        "prior_revision": "b" * 40,
        "rollback_capable": True,
        "prior_topology": {
            "supervisor": "launchd",
            "active_gateway": "hermes",
            "agent_prior_state": "active",
        },
        "artifacts": {
            "source": {"path": str(src), "backup": str(src_backup)},
            "venv": {"path": str(venv), "backup": str(venv_backup)},
            "hermes": {"path": str(hermes), "backup": str(hermes_backup)},
            "bin_backup": str(bin_backup),
            "openclaw_backup": str(openclaw_backup),
            "openclaw_existed": True,
        },
        "prerequisites": {
            "schema": "mac.fleet_prerequisite_rollback_binding.v1",
            "node_identity_sha256": node_identity_sha256,
            "bundle_sha256": bundle_sha256,
            "expectations_sha256": expectations_sha256,
        },
        "contracts": {
            "supervisor_helper": {
                "path": str(supervisor_helper),
                "sha256": supervisor_helper_sha256,
            },
            "lifecycle_helper": {
                "path": str(lifecycle_helper),
                "sha256": lifecycle_helper_sha256,
            },
        },
        "rollback": {
            "path": str(rollback_script),
            "sha256": hashlib.sha256(rollback_script.read_bytes()).hexdigest(),
            "completion_receipt": str(completion_receipt),
        },
    }
    intent_path.write_text(json.dumps(intent) + "\n", encoding="utf-8")
    intent_path.chmod(0o600)
    validator = _function(
        "verify_existing_phase2_sealed_state", "arm_phase2_rollback"
    )
    command = f"""set -euo pipefail
PY=${{TEST_PY:?}}
MAC_HOME=${{TEST_MAC_HOME:?}}
ROLLBACK_INTENT=${{TEST_INTENT:?}}
AGENT={agent}
FLEET_NAME=mac
OS_KIND=darwin
DEPLOY_GENERATION={GENERATION}
DEPLOY_REV={REVISION}
SUPERVISOR_KIND=launchd
DEPLOY_TS={deploy_ts}
SRC_DIR=recaptured-wrong-source
SRC_BACKUP=recaptured-wrong-source-backup
VENV=recaptured-wrong-venv
VENV_BACKUP=recaptured-wrong-venv-backup
HERMES_DIR=recaptured-wrong-hermes
BIN_BACKUP=recaptured-wrong-bin-backup
{validator}
verify_existing_phase2_sealed_state
"""
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        env={
            **os.environ,
            "TEST_PY": sys.executable,
            "TEST_MAC_HOME": str(mac_home),
            "TEST_INTENT": str(intent_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashlib.sha256(intent_path.read_bytes()).hexdigest()

    verifier = _function(
        "verify_phase2_rollback_intent", "write_phase2_rollback_intent"
    )
    verify_command = f"""set -euo pipefail
PY=${{TEST_PY:?}}
MAC_HOME=${{TEST_MAC_HOME:?}}
ROLLBACK_INTENT=${{TEST_INTENT:?}}
ROLLBACK_SCRIPT={rollback_script}
ROLLBACK_COMPLETION_RECEIPT={completion_receipt}
AGENT={agent}
FLEET_NAME=mac
OS_KIND=darwin
DEPLOY_GENERATION={GENERATION}
DEPLOY_REV={REVISION}
DEPLOY_TS={deploy_ts}
ROLLBACK_PRIOR_GENERATION=recaptured-wrong-generation
ROLLBACK_PRIOR_REVISION={'c' * 40}
SUPERVISOR_KIND=launchd
ROLLBACK_ACTIVE_GATEWAY=none
ROLLBACK_AGENT_PRIOR_STATE=inactive
NODE_IDENTITY_SHA256={node_identity_sha256}
PREREQUISITE_BUNDLE_SHA256={bundle_sha256}
PREREQUISITE_EXPECTATIONS_SHA256={expectations_sha256}
SRC_DIR={src}
SRC_BACKUP={src_backup}
VENV={venv}
VENV_BACKUP={venv_backup}
HERMES_DIR={hermes}
HERMES_BACKUP=
BIN_BACKUP={bin_backup}
OPENCLAW_HOME_BACKUP=
OPENCLAW_HOME_EXISTED=0
ROLLBACK_SUPERVISOR_HELPER={supervisor_helper}
ROLLBACK_SUPERVISOR_HELPER_SHA256={supervisor_helper_sha256}
ROLLBACK_LAUNCHD_LIFECYCLE={lifecycle_helper}
ROLLBACK_LAUNCHD_LIFECYCLE_SHA256={lifecycle_helper_sha256}
{validator}
{verifier}
verify_existing_phase2_sealed_state >/dev/null
verify_phase2_rollback_intent sealed-replay
"""
    verified = subprocess.run(
        ["/bin/bash", "-c", verify_command],
        env={
            **os.environ,
            "TEST_PY": sys.executable,
            "TEST_MAC_HOME": str(mac_home),
            "TEST_INTENT": str(intent_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.strip() == hashlib.sha256(
        intent_path.read_bytes()
    ).hexdigest()

    tampered_intent = dict(intent)
    tampered_intent["artifacts"] = dict(intent["artifacts"])
    tampered_intent["artifacts"]["bin_backup"] = str(tmp_path / "wrong-backup")
    intent_path.write_text(json.dumps(tampered_intent) + "\n", encoding="utf-8")
    intent_path.chmod(0o600)
    stale = subprocess.run(
        ["/bin/bash", "-c", verify_command],
        env={
            **os.environ,
            "TEST_PY": sys.executable,
            "TEST_MAC_HOME": str(mac_home),
            "TEST_INTENT": str(intent_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert stale.returncode != 0
    assert "differs at: artifact_bin_backup" in stale.stderr
    assert str(tmp_path / "wrong-backup") not in stale.stderr


@pytest.mark.parametrize(
    "mutation",
    ["missing-prior-state", "malformed-prior-state", "nonquiescent-final-state"],
)
def test_launchd_prestate_consumer_rejects_malformed_transition(
    tmp_path: Path, mutation: str
) -> None:
    payload = _valid_receipt()
    item = _resource(payload, LABELS[0], "gui")
    if mutation == "missing-prior-state":
        item.pop("prior_state")
    elif mutation == "malformed-prior-state":
        item["prior_state"] = ["active"]
    else:
        item["state"] = "active"

    result = _run_capture(tmp_path, payload)

    assert result.returncode != 0
    assert "phase-1 launchd prestate contains an invalid transition" in result.stderr


@pytest.mark.parametrize("label", LABELS)
def test_launchd_prestate_consumer_rejects_system_domain_prior_activity(
    tmp_path: Path, label: str
) -> None:
    payload = _valid_receipt()
    _resource(payload, label, "system")["prior_state"] = "active"

    result = _run_capture(tmp_path, payload)

    assert result.returncode != 0
    assert "phase-1 found a system-domain gateway or worker" in result.stderr


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (LABELS[0], LABELS[1]),
        (LABELS[0], LABELS[2]),
        (LABELS[1], LABELS[2]),
    ],
)
def test_launchd_prestate_consumer_rejects_multiple_prior_active_gateways(
    tmp_path: Path, first: str, second: str
) -> None:
    payload = _valid_receipt()
    _resource(payload, first, "gui")["prior_state"] = "active"
    _resource(payload, second, "gui")["prior_state"] = "active"

    result = _run_capture(tmp_path, payload)

    assert result.returncode != 0
    assert "phase-1 found multiple active launchd gateway owners" in result.stderr


@pytest.mark.parametrize(
    "field",
    ["schema", "agent", "fleet", "revision", "generation"],
)
def test_launchd_prestate_consumer_requires_exact_release_identity(
    tmp_path: Path, field: str
) -> None:
    payload = _valid_receipt()
    payload[field] = str(payload[field]) + "-impostor"

    result = _run_capture(tmp_path, payload)

    assert result.returncode != 0
    assert "phase-1 launchd prestate receipt belongs to another release" in result.stderr


@pytest.mark.parametrize("mutation", ["name", "target", "missing", "duplicate"])
def test_launchd_prestate_consumer_requires_exact_resource_identity(
    tmp_path: Path, mutation: str
) -> None:
    payload = _valid_receipt()
    resources = payload["supervisor"]["resources"]  # type: ignore[index]
    if mutation == "name":
        resources[0]["name"] = "com.mac.impostor"  # type: ignore[index]
    elif mutation == "target":
        resources[0]["target"] = "gui/999999/com.mac.hermes-gateway"  # type: ignore[index]
    elif mutation == "missing":
        resources.pop()  # type: ignore[union-attr]
    else:
        resources[-1] = copy.deepcopy(resources[0])  # type: ignore[index]

    result = _run_capture(tmp_path, payload)

    assert result.returncode != 0
    assert any(
        message in result.stderr
        for message in (
            "phase-1 launchd prestate contains an unexpected identity",
            "phase-1 launchd prestate contains a duplicate identity",
            "phase-1 launchd prestate is incomplete",
        )
    )
