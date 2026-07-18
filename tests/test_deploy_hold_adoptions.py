import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "deploy-hold-adoptions.py"
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
COMMIT = "1" * 40
HUB = "rocky"
FLEET = "mac"
AGENT = "agent_11111111111111111111111111111111"


def authority(**overrides):
    payload = {
        "schema": "mac.dispatch_hold_adoptions.v1",
        "fleet": FLEET,
        "hub_agent": HUB,
        "source_commit": COMMIT,
        "adoptions": [{"agent": AGENT, "reason": "synchronized cut-over hold"}],
    }
    payload.update(overrides)
    return payload


def write_authority(path: Path, payload=None, *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload or authority()), encoding="utf-8")
    path.chmod(mode)
    return path


def run_helper(*arguments: str):
    return subprocess.run(
        [sys.executable, str(HELPER), *map(str, arguments)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_owner_only_authority_is_frozen_and_scoped_to_exact_cohort(tmp_path):
    source = write_authority(tmp_path / "authority.json")
    snapshot = tmp_path / "snapshot.json"
    frozen = run_helper(
        "snapshot", source, snapshot, "--source-commit", COMMIT
    )
    assert frozen.returncode == 0, frozen.stderr
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload == authority()

    selected = run_helper(
        "validate-selected",
        snapshot,
        "--fleet",
        FLEET,
        "--hub-agent",
        HUB,
        "--agent",
        AGENT,
    )
    assert selected.returncode == 0, selected.stderr
    reason = run_helper("reason", snapshot, AGENT)
    assert reason.returncode == 0
    assert reason.stdout.strip() == "synchronized cut-over hold"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_authority_rejects_group_or_other_permissions(tmp_path, mode):
    source = write_authority(tmp_path / "authority.json", mode=mode)
    result = run_helper(
        "snapshot", source, tmp_path / "snapshot.json", "--source-commit", COMMIT
    )
    assert result.returncode == 2
    assert "group or other permission" in result.stderr


def test_authority_rejects_symlink_and_oversize_input(tmp_path):
    target = write_authority(tmp_path / "target.json")
    link = tmp_path / "authority-link.json"
    link.symlink_to(target)
    linked = run_helper(
        "snapshot", link, tmp_path / "linked.json", "--source-commit", COMMIT
    )
    assert linked.returncode == 2
    assert not (tmp_path / "linked.json").exists()

    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)
    oversized.chmod(0o600)
    too_large = run_helper(
        "snapshot", oversized, tmp_path / "large.json", "--source-commit", COMMIT
    )
    assert too_large.returncode == 2
    assert "size must be between" in too_large.stderr


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            '{"schema":"mac.dispatch_hold_adoptions.v1",'
            '"schema":"mac.dispatch_hold_adoptions.v1",'
            '"fleet":"mac","hub_agent":"rocky","source_commit":"%s",'
            '"adoptions":[]}' % COMMIT,
            "duplicate JSON key",
        ),
        (json.dumps({**authority(), "unexpected": True}), "keys must be exactly"),
        (
            json.dumps(authority(adoptions=[{"agent": AGENT, "reason": " padded "}])),
            "trimmed string",
        ),
        (
            json.dumps(
                authority(
                    adoptions=[
                        {"agent": AGENT, "reason": "one"},
                        {"agent": AGENT, "reason": "two"},
                    ]
                )
            ),
            "duplicate adoption agent",
        ),
    ],
)
def test_authority_rejects_ambiguous_or_non_exact_json(tmp_path, raw, message):
    source = tmp_path / "authority.json"
    source.write_text(raw, encoding="utf-8")
    source.chmod(0o600)
    result = run_helper(
        "snapshot", source, tmp_path / "snapshot.json", "--source-commit", COMMIT
    )
    assert result.returncode == 2
    assert message in result.stderr


def test_authority_rejects_commit_hub_fleet_and_selected_agent_drift(tmp_path):
    source = write_authority(tmp_path / "authority.json")
    snapshot = tmp_path / "snapshot.json"
    wrong_commit = run_helper(
        "snapshot", source, snapshot, "--source-commit", "2" * 40
    )
    assert wrong_commit.returncode == 2
    assert "does not match deploy commit" in wrong_commit.stderr

    assert run_helper(
        "snapshot", source, snapshot, "--source-commit", COMMIT
    ).returncode == 0
    wrong_hub = run_helper(
        "validate-selected",
        snapshot,
        "--fleet",
        FLEET,
        "--hub-agent",
        "bullwinkle",
        "--agent",
        AGENT,
    )
    assert wrong_hub.returncode == 2
    assert "does not match selected hub" in wrong_hub.stderr
    wrong_fleet = run_helper(
        "validate-selected",
        snapshot,
        "--fleet",
        "another",
        "--hub-agent",
        HUB,
        "--agent",
        AGENT,
    )
    assert wrong_fleet.returncode == 2
    assert "does not match selected fleet" in wrong_fleet.stderr
    unselected = run_helper(
        "validate-selected",
        snapshot,
        "--fleet",
        FLEET,
        "--hub-agent",
        HUB,
        "--agent",
        "agent_22222222222222222222222222222222",
    )
    assert unselected.returncode == 2
    assert "unselected agents" in unselected.stderr


def test_deploy_contract_has_explicit_adoption_and_exact_release_mode():
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "MAC_DEPLOY_HOLD_ADOPTIONS_FILE" in deploy
    assert "MAC_DEPLOY_REQUIRE_RELEASE_ALL_SELECTED" in deploy
    assert "--hold-adoptions" in deploy
    assert "--require-release-all-selected" in deploy
    assert "preflight_cohort_hold_adoptions" in deploy
    assert "MAC_DEPLOY_GATE_ADOPT_REASON" in deploy
    assert "adopted_from_reason" in deploy
    assert "require_release_all_selected" in deploy

    parser = deploy.split('while [ "$#" -gt 0 ]; do', 1)[1].split(
        'if ! PYTHON_BIN="$(resolve_python_bin)"', 1
    )[0]
    assert "HOLD_ADOPTIONS_SOURCE=\"$2\"" in parser
    assert "REQUIRE_RELEASE_ALL_SELECTED=1" in parser
    assert parser.index('if [ -n "$HOLD_ADOPTIONS_SOURCE" ]') < parser.index(
        'REQUIRE_RELEASE_ALL_SELECTED=1',
        parser.index('if [ -n "$HOLD_ADOPTIONS_SOURCE" ]'),
    )

    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    preflight = main.index("preflight_cohort_hold_adoptions")
    phase_one = main.index("phase 1/3 holding and draining")
    prepare = main.index("prepare_remote_mac_agent_deployment", phase_one)
    assert preflight < phase_one < prepare
    assert 'hold_adoption_reason_for_agent "$hold_adoption_plan" "$agent_id"' in main

    gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    adoption = gate.split("if authorized_prior_reason:", 1)[1].split(
        'elif bool(row.get("dispatch_hold")):', 1
    )[0]
    assert "expected_hold=True" in adoption
    assert "expected_reason=authorized_prior_reason" in adoption
    assert "if not changed:" in adoption
    assert "expected_hold=False" not in adoption
    exact_prepare = gate.split('if phase == "prepare":', 1)[1].split(
        'elif phase == "verify":', 1
    )[0]
    assert "MAC_DEPLOY_GATE_REQUIRE_OWNED" in gate
    assert exact_prepare.index("if require_owned_after_prepare and not owns_hold:") < (
        exact_prepare.index("post_drain()")
    )

    release = deploy.split("commit_fleet_release_epoch() {", 1)[1].split(
        "enforce_bound_worker_credentials() {", 1
    )[0]
    assert "sorted(agent_ids) != selected_ids" in release
    assert 'response.get("epoch_id") != epoch_id' in release
    assert "hub returned the wrong fleet release epoch id" in release
    assert "returned_ids != requested_ids" in release
    assert "selected agent remained held after exact fleet release" in release
    assert 'receipt["operator_holds_preserved"] == 0' in release


@pytest.mark.parametrize(
    ("prior_owned", "prior_hold_reason"),
    [
        ("0", ""),
        ("1", "new operator hold after preflight"),
    ],
)
def test_post_preflight_operator_hold_race_fails_before_drain(
    monkeypatch, prior_owned, prior_hold_reason
):
    deploy = DEPLOY.read_text(encoding="utf-8")
    gate_start = deploy.index("hub_agent_restart_gate() {")
    python_marker = '"$HOME/.mac/venv/bin/python" - <<\'PY\'\n'
    python_start = deploy.index(python_marker, gate_start) + len(python_marker)
    gate = deploy[python_start : deploy.index("\nPY\nREMOTE_HUB_GATE", python_start)]

    raced_reason = "new operator hold after preflight"
    row = {
        "id": AGENT,
        "dispatch_hold": True,
        "dispatch_hold_reason": raced_reason,
        "deleted_at": None,
        "last_seen_at": "2026-07-18T00:00:00+00:00",
        "status": "idle",
        "health_status": "healthy",
        "current_task_id": None,
        "resources": {},
    }
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(row).encode("utf-8")

    def urlopen(request, timeout=0):
        calls.append((request.get_method(), request.full_url, request.data))
        if request.get_method() == "GET" and request.full_url.endswith(
            "/agents/" + AGENT
        ):
            return Response()
        raise AssertionError("hold-race rejection attempted a mutation")

    environment = {
        "MAC_DEPLOY_GATE_PHASE": "prepare",
        "MAC_DEPLOY_GATE_AGENT_ID": AGENT,
        "MAC_DEPLOY_GATE_GENERATION": "",
        "MAC_DEPLOY_GATE_BASELINE": "",
        "MAC_DEPLOY_GATE_HOLD_REASON": "deployment-owned hold",
        "MAC_DEPLOY_GATE_PRIOR_HOLD_REASON": prior_hold_reason,
        "MAC_DEPLOY_GATE_PRIOR_OWNED": prior_owned,
        "MAC_DEPLOY_GATE_ALLOW_MISSING": "1",
        "MAC_DEPLOY_GATE_REQUIRE_AUTHENTICATED": "0",
        "MAC_DEPLOY_GATE_EXPECTED_PRINCIPAL_ID": "",
        "MAC_DEPLOY_GATE_ADOPT_REASON": "",
        "MAC_DEPLOY_GATE_REQUIRE_OWNED": "1",
        "MAC_DEPLOY_GATE_TIMEOUT": "1",
        "MAC_HUB_URL": "http://mock-hub",
        "MAC_DEPLOY_GATE_ADMIN_TOKEN": "redacted-test-token",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    with pytest.raises(
        RuntimeError,
        match="exact full-cohort release lost deployment hold ownership before drain",
    ):
        exec(compile(gate, "<embedded-hub-gate>", "exec"), {"__name__": "__main__"})

    assert calls == [("GET", "http://mock-hub/agents/" + AGENT, None)]
    assert row["status"] == "idle"
    assert row["health_status"] == "healthy"
