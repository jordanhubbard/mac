from __future__ import annotations

import copy
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
