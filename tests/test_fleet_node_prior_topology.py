"""Behavioral contract for phase-one prior worker-topology recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"
REVISION = "a" * 40
GENERATION = "generation-test-001"

SYSTEMD_NAMES = [
    "mac-hermes-gateway.service",
    "mac-openclaw-gateway.service",
    "mac-nemoclaw-gateway.service",
    "mac-agent.service",
]
LAUNCHD_NAMES = [
    "com.mac.hermes-gateway",
    "com.mac.openclaw-gateway",
    "com.mac.nemoclaw-gateway",
    "com.mac.agent",
]
SUPERVISORD_NAMES = [
    "mac-hermes-gateway",
    "mac-openclaw-gateway",
    "mac-nemoclaw-gateway",
    "mac-agent",
]


def _function_source() -> str:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    body = source.split("capture_phase1_prior_worker_topology() {", 1)[1].split(
        "\n}\n\nsystem_launchd_job_is_loaded() {", 1
    )[0]
    return "capture_phase1_prior_worker_topology() {" + body + "\n}\n"


def _resource(name: str, prior: str, final: str | None = None) -> dict[str, str]:
    if final is None:
        final = "absent" if prior == "absent" else "inactive"
    return {"name": name, "prior_state": prior, "state": final}


def _systemd_supervisor(*, gateway: str = "hermes", agent: str = "active") -> dict[str, Any]:
    owners = {"hermes": 0, "openclaw": 1, "nemoclaw": 2}
    prior = ["inactive", "inactive", "inactive", agent]
    if gateway != "none":
        prior[owners[gateway]] = "active"
    return {
        "manager": "systemd",
        "resources": [_resource(name, state) for name, state in zip(SYSTEMD_NAMES, prior)],
    }


def _launchd_supervisor(*, system_active: bool = False) -> dict[str, Any]:
    uid = os.getuid()
    resources = []
    for index, name in enumerate(LAUNCHD_NAMES):
        resources.extend(
            [
                {
                    "name": name,
                    "target": f"gui/{uid}/{name}",
                    "prior_state": "active" if index in {1, 3} else "absent",
                    "state": "absent",
                },
                {
                    "name": name,
                    "target": f"system/{name}",
                    "prior_state": ("active" if system_active and index == 1 else "absent"),
                    "state": "absent",
                },
            ]
        )
    return {"manager": "launchd", "resources": resources}


def _supervisord_resources(
    *, gateway: str = "openclaw", agent: str = "STOPPED"
) -> list[dict[str, str]]:
    owners = {"hermes": 0, "openclaw": 1, "nemoclaw": 2}
    prior = ["STOPPED", "STOPPED", "STOPPED", agent]
    if gateway != "none":
        prior[owners[gateway]] = "RUNNING"
    return [_resource(name, state, "STOPPED") for name, state in zip(SUPERVISORD_NAMES, prior)]


def _supervisord_supervisor(
    *, include_user: bool = True, user_active: bool = False
) -> dict[str, Any]:
    managers: list[dict[str, Any]] = [
        {
            "scope": "system",
            "manager_identity_sha256": "1" * 64,
            "resources": _supervisord_resources(),
        }
    ]
    if include_user:
        managers.append(
            {
                "scope": "user",
                "manager_identity_sha256": "2" * 64,
                "resources": _supervisord_resources(
                    gateway="openclaw" if user_active else "none",
                    agent="STOPPED",
                ),
            }
        )
    return {"manager": "supervisord", "managers": managers}


def _run(
    tmp_path: Path,
    manager: str,
    supervisor: dict[str, Any],
    *,
    receipt_mode: int = 0o600,
    generation: str = GENERATION,
    write_receipt: bool = True,
    require_phase1_quiescence: str = "1",
) -> subprocess.CompletedProcess[str]:
    mac_home = tmp_path / "mac-home"
    mac_home.mkdir(parents=True)
    if write_receipt:
        receipt = mac_home / f"phase1-cohort-quiescence-{GENERATION}.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema": "mac.phase1_cohort_quiescence.v1",
                    "agent": "rocky",
                    "fleet": "mac",
                    "revision": REVISION,
                    "generation": generation,
                    "supervisor": supervisor,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        receipt.chmod(receipt_mode)

    values = {
        "PY": sys.executable,
        "MAC_HOME": str(mac_home),
        "AGENT": "rocky",
        "FLEET_NAME": "mac",
        "DEPLOY_REV": REVISION,
        "DEPLOY_GENERATION": GENERATION,
        "SUPERVISOR_KIND": manager,
        "MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE": require_phase1_quiescence,
        "HERMES_SERVICE_NAME": SYSTEMD_NAMES[0],
        "OPENCLAW_SERVICE_NAME": SYSTEMD_NAMES[1],
        "NEMOCLAW_SERVICE_NAME": SYSTEMD_NAMES[2],
        "MAC_AGENT_SERVICE_NAME": SYSTEMD_NAMES[3],
        "HERMES_LAUNCHD_LABEL": LAUNCHD_NAMES[0],
        "OPENCLAW_LAUNCHD_LABEL": LAUNCHD_NAMES[1],
        "NEMOCLAW_LAUNCHD_LABEL": LAUNCHD_NAMES[2],
        "MAC_AGENT_LAUNCHD_LABEL": LAUNCHD_NAMES[3],
        "HERMES_SUPERVISORD_PROG": SUPERVISORD_NAMES[0],
        "OPENCLAW_SUPERVISORD_PROG": SUPERVISORD_NAMES[1],
        "NEMOCLAW_SUPERVISORD_PROG": SUPERVISORD_NAMES[2],
        "AGENT_SUPERVISORD_PROG": SUPERVISORD_NAMES[3],
    }
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + "ROLLBACK_ACTIVE_GATEWAY=''\nROLLBACK_AGENT_PRIOR_STATE=''\n"
        + "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())
        + "\nwrite_rollback_script() { :; }\n"
        + "die() { printf '%s\\n' \"$*\" >&2; return 1; }\n"
        + "truthy() {\n"
        + '  case "${1:-}" in\n'
        + "    1|true|TRUE|yes|YES|on|ON) return 0 ;;\n"
        + "    *) return 1 ;;\n"
        + "  esac\n"
        + "}\n"
        + _function_source()
        + "capture_phase1_prior_worker_topology\n"
        + "printf '%s %s\\n' \"$ROLLBACK_ACTIVE_GATEWAY\" "
        '"$ROLLBACK_AGENT_PRIOR_STATE"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        ["/bin/bash", str(harness)],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("gateway", "agent"),
    [
        ("hermes", "active"),
        ("openclaw", "inactive"),
        ("none", "inactive"),
    ],
)
def test_systemd_receipt_selects_exact_prior_owner_and_agent_state(
    tmp_path: Path, gateway: str, agent: str
) -> None:
    result = _run(
        tmp_path,
        "systemd",
        _systemd_supervisor(gateway=gateway, agent=agent),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{gateway} {agent}"


def test_multiple_prior_gateway_owners_fail_closed(tmp_path: Path) -> None:
    supervisor = _systemd_supervisor()
    supervisor["resources"][1]["prior_state"] = "active"

    result = _run(tmp_path, "systemd", supervisor)

    assert result.returncode != 0
    assert "multiple active gateways" in result.stderr


def test_prior_nemo_gateway_without_runtime_checkpoint_fails_closed(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "systemd",
        _systemd_supervisor(gateway="nemoclaw", agent="absent"),
    )

    assert result.returncode != 0
    assert "prior Nemo gateway lacks a durable runtime checkpoint" in result.stderr


def test_launchd_uses_gui_prior_state_and_rejects_system_worker(
    tmp_path: Path,
) -> None:
    success = _run(tmp_path / "success", "launchd", _launchd_supervisor())
    failure = _run(
        tmp_path / "failure",
        "launchd",
        _launchd_supervisor(system_active=True),
    )

    assert success.returncode == 0, success.stderr
    assert success.stdout.strip() == "openclaw active"
    assert failure.returncode != 0
    assert "unsupported system launchd worker topology" in failure.stderr


def test_supervisord_uses_only_canonical_system_manager_prior_state(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "supervisord", _supervisord_supervisor())

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "openclaw inactive"


@pytest.mark.parametrize("failure", ["missing", "duplicate", "active-user"])
def test_supervisord_invalid_manager_topology_fails_closed(tmp_path: Path, failure: str) -> None:
    supervisor = _supervisord_supervisor(user_active=failure == "active-user")
    if failure == "missing":
        supervisor["managers"] = [supervisor["managers"][1]]
    elif failure == "duplicate":
        duplicate = dict(supervisor["managers"][0])
        duplicate["manager_identity_sha256"] = "3" * 64
        supervisor["managers"].append(duplicate)

    result = _run(tmp_path, "supervisord", supervisor)

    assert result.returncode != 0
    if failure == "active-user":
        assert "active unsupported user supervisord topology" in result.stderr
    else:
        assert "lacks one system manager" in result.stderr


@pytest.mark.parametrize(
    ("mode", "generation"), [(0o644, GENERATION), (0o600, GENERATION + "-old")]
)
def test_untrusted_or_stale_receipt_cannot_select_rollback_topology(
    tmp_path: Path, mode: int, generation: str
) -> None:
    result = _run(
        tmp_path,
        "systemd",
        _systemd_supervisor(),
        receipt_mode=mode,
        generation=generation,
    )

    assert result.returncode != 0


def test_from_scratch_first_hub_install_skips_without_a_receipt(tmp_path: Path) -> None:
    # This is the exact scenario that crashed --first-hub-bootstrap: no
    # phase1-cohort-quiescence receipt exists at all (a from-scratch node
    # never ran a phase-1 drain), and the deploy correctly declared
    # MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE=0. The function must return
    # success without ever trying to read the (nonexistent) receipt file,
    # setting the rollback-topology globals to their semantically correct
    # values for "there was never a prior generation" (not raw empty
    # strings, which fail the rollback intent's own enum validation).
    result = _run(
        tmp_path,
        "systemd",
        _systemd_supervisor(),
        write_receipt=False,
        require_phase1_quiescence="0",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "none absent"


def test_upgrade_still_requires_the_receipt_even_if_absent(tmp_path: Path) -> None:
    # The inverse: MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE=1 (a real upgrade)
    # but no receipt was written -- a genuinely broken prior state. Must
    # still fail, not be silently treated as "nothing to restore".
    result = _run(
        tmp_path,
        "systemd",
        _systemd_supervisor(),
        write_receipt=False,
        require_phase1_quiescence="1",
    )

    assert result.returncode != 0
