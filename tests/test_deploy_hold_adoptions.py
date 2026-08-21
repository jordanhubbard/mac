import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types

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
    frozen = run_helper("snapshot", source, snapshot, "--source-commit", COMMIT)
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
    wrong_commit = run_helper("snapshot", source, snapshot, "--source-commit", "2" * 40)
    assert wrong_commit.returncode == 2
    assert "does not match deploy commit" in wrong_commit.stderr

    assert (
        run_helper("snapshot", source, snapshot, "--source-commit", COMMIT).returncode
        == 0
    )
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
    assert 'HOLD_ADOPTIONS_SOURCE="$2"' in parser
    assert "REQUIRE_RELEASE_ALL_SELECTED=1" in parser
    assert parser.index('if [ -n "$HOLD_ADOPTIONS_SOURCE" ]') < parser.index(
        "REQUIRE_RELEASE_ALL_SELECTED=1",
        parser.index('if [ -n "$HOLD_ADOPTIONS_SOURCE" ]'),
    )

    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    typed = deploy.split("run_typed_cohort() {", 1)[1].split(
        "\n}\n\nmain()", 1
    )[0]
    preflight = typed.index("preflight_cohort_hold_adoptions")
    restore_arm = typed.index(
        'run_bounded_node_phase "$selected_specs_file" phase1-prepare'
    )
    prerequisite = typed.index(
        'run_bounded_node_phase "$selected_specs_file" prerequisites'
    )
    hub_open = typed.index("build_and_open_hub_epoch")
    assert preflight < restore_arm < prerequisite < hub_open
    phase1_worker = deploy.split("typed_phase1_prepare_worker() {", 1)[1].split(
        "\n}\n\nstart_control_master_worker", 1
    )[0]
    prerequisite_worker = deploy.split("typed_prerequisite_worker() {", 1)[1].split(
        "\n}\n\ntyped_staging_worker", 1
    )[0]
    assert "prepare_remote_phase1_restore_contract" in phase1_worker
    assert "prepare_remote_prerequisite_bundle" in prerequisite_worker
    assert "run_typed_cohort" in main
    assert '"$hold_adoption_plan"' in main

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


def test_successor_hold_is_frozen_and_proved_before_any_cohort_mutation():
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "MAC_DEPLOY_SUCCESSOR_HOLD_REASON" in deploy
    assert "--successor-hold-reason" in deploy
    assert (
        "readonly HOLD_ADOPTIONS_FILE REQUIRE_RELEASE_ALL_SELECTED SUCCESSOR_HOLD_REASON"
        in deploy
    )

    parser = deploy.split('while [ "$#" -gt 0 ]; do', 1)[1].split(
        'if ! PYTHON_BIN="$(resolve_python_bin)"', 1
    )[0]
    assert 'SUCCESSOR_HOLD_REASON_RAW="$2"' in parser
    assert 'SUCCESSOR_HOLD_REASON_RAW="${1#--successor-hold-reason=}"' in parser
    normalization = deploy.split('if ! PYTHON_BIN="$(resolve_python_bin)"', 1)[1].split(
        "DEPLOY_CONTROLLER_NONCE=", 1
    )[0]
    assert "raw_reason = sys.argv[1]" in normalization
    assert "if not reason:" in normalization
    assert 'len(reason.encode("utf-8")) > 512' in normalization
    assert "not character.isprintable()" in normalization
    assert "REQUIRE_RELEASE_ALL_SELECTED=1" in normalization

    availability = deploy.split("hub_dispatch_hold_transition_available() {", 1)[
        1
    ].split("preflight_cohort_hold_adoptions() {", 1)[0]
    assert 'paths.get("/agents/dispatch-hold/transition-batch")' in availability
    assert 'operation.get("post")' in availability

    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    typed = deploy.split("run_typed_cohort() {", 1)[1].split(
        "\n}\n\nmain()", 1
    )[0]
    hub_open = deploy.split("build_and_open_hub_epoch() {", 1)[1].split(
        "\n}\n\nprove_and_commit_hub_epoch", 1
    )[0]
    assert typed.index("preflight_cohort_hold_adoptions") < typed.index(
        "build_and_open_hub_epoch"
    )
    assert '"$SUCCESSOR_HOLD_REASON"' in hub_open
    assert '"$REQUIRE_RELEASE_ALL_SELECTED"' in hub_open
    assert "run_typed_cohort" in main


@pytest.mark.parametrize(
    (
        "successor_reason",
        "endpoint",
        "schema",
        "response_flag",
        "reported_hold",
        "expected_error",
    ),
    [
        (
            "synchronized pipeline activation refreeze",
            "/agents/dispatch-hold/transition-batch",
            "mac.fleet_release_receipt.v2",
            True,
            True,
            None,
        ),
        (
            "",
            "/agents/dispatch-hold/release-batch",
            "mac.fleet_release_receipt.v1",
            True,
            False,
            None,
        ),
        (
            "synchronized pipeline activation refreeze",
            "/agents/dispatch-hold/transition-batch",
            "mac.fleet_release_receipt.v2",
            "true",
            True,
            "hub rejected the atomic fleet successor-hold epoch",
        ),
        (
            "synchronized pipeline activation refreeze",
            "/agents/dispatch-hold/transition-batch",
            "mac.fleet_release_receipt.v2",
            True,
            "true",
            "transition receipt lacks the exact successor hold",
        ),
        (
            "",
            "/agents/dispatch-hold/release-batch",
            "mac.fleet_release_receipt.v1",
            1,
            False,
            "hub rejected the atomic fleet release epoch",
        ),
        (
            "",
            "/agents/dispatch-hold/release-batch",
            "mac.fleet_release_receipt.v1",
            True,
            0,
            "release receipt still reports a selected hold",
        ),
    ],
)
def test_embedded_epoch_commit_atomically_transitions_or_releases(
    tmp_path,
    monkeypatch,
    capsys,
    successor_reason,
    endpoint,
    schema,
    response_flag,
    reported_hold,
    expected_error,
):
    deploy = DEPLOY.read_text(encoding="utf-8")
    function_start = deploy.index("commit_fleet_release_epoch() {")
    marker = "\"$HOME/.mac/venv/bin/python\" - <<'PY'\n"
    python_start = deploy.index(marker, function_start) + len(marker)
    embedded = deploy[
        python_start : deploy.index("\nPY\nREMOTE_RELEASE_EPOCH", python_start)
    ]

    generation = "generation-1"
    deployment_id = "1" * 40 + ":worker-1:20260718T000000Z:controller"
    baseline = "2026-07-18T00:00:00+00:00"
    deployment_reason = "mac fleet deployment test"
    digest = "a" * 64
    media_readiness = {
        "schema": "mac.media_runtime_readiness_manifest.v1",
        "status": "proved",
        "manager": "systemd",
        "resources": [],
        "sha256": digest,
        "source_contract_sha256": digest,
    }
    quiescence = {
        "schema": "mac.daemon_resource_quiescence_attestation.v1",
        "agent": "worker-1",
        "receipt_sha256": digest,
        "phase1_receipt_sha256": digest,
        "phase1_daemon_receipt_sha256": digest,
        "phase1_function_block_sha256": digest,
        "phase1_supervisor": {"manager": "systemd", "resources": []},
        "media_runtime_readiness": media_readiness,
        "media_runtime_readiness_sha256": digest,
        "media_runtime_source_contract_sha256": digest,
        "media_runtime_stable_observations": 2,
        "generation": deployment_id,
        "revision": "1" * 40,
        "gateway_implementation": "none",
        "gateway_readiness_sha256": digest,
        "gateway_supervisor": "systemd",
        "gateway_identities": {},
        "required_phases": ["pre_source", "post_install"],
        "container_runtimes": [],
        "stable_absence_observations": 2,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    evidence = [
        {
            "agent_id": AGENT,
            "quiescence": quiescence,
            "commit_quiescence": quiescence,
        }
    ]
    evidence_digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    epoch_id = "1" * 40 + ":20260718T000000Z:nonce:" + evidence_digest
    row = {
        "id": AGENT,
        "status": "idle",
        "health_status": "healthy",
        "current_task_id": None,
        "dispatch_hold": True,
        "dispatch_hold_reason": deployment_reason,
        "last_seen_at": "2026-07-18T00:00:01+00:00",
        "resources": {"deployment_generation": generation},
    }
    plan = {
        "schema": "mac.fleet_release_epoch.v1",
        "epoch_id": epoch_id,
        "source_commit": "1" * 40,
        "require_release_all_selected": True,
        "successor_hold_reason": successor_reason or None,
        "agents": [
            {
                "schema": "mac.deploy_release_ready.v1",
                "agent": "worker-1",
                "agent_id": AGENT,
                "generation": generation,
                "deployment_id": deployment_id,
                "baseline_seen": baseline,
                "hold_reason": deployment_reason,
                "owns_hold": True,
                "principal_id": "",
                "require_authenticated": False,
                "require_report_executor": False,
                "quiescence": quiescence,
                "commit_quiescence": quiescence,
            }
        ],
    }
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def urlopen(request, timeout=0):
        del timeout
        path = request.full_url.removeprefix("http://mock-hub")
        calls.append((request.get_method(), path))
        if request.get_method() == "GET" and path == "/agents/" + AGENT:
            return Response(dict(row))
        if request.get_method() == "POST" and path == endpoint:
            body = json.loads(request.data)
            assert body["epoch_id"] == epoch_id
            assert body["holds"][0]["agent_id"] == AGENT
            if successor_reason:
                assert body["successor_reason"] == successor_reason
                row["dispatch_hold"] = reported_hold
                row["dispatch_hold_reason"] = successor_reason
                return Response(
                    {
                        "transitioned": response_flag,
                        "epoch_id": epoch_id,
                        "successor_reason": successor_reason,
                        "agents": [dict(row)],
                    }
                )
            assert "successor_reason" not in body
            row["dispatch_hold"] = reported_hold
            row["dispatch_hold_reason"] = None
            return Response(
                {
                    "released": response_flag,
                    "epoch_id": epoch_id,
                    "agents": [dict(row)],
                }
            )
        raise AssertionError("unexpected epoch request: %s %s" % calls[-1])

    models = types.ModuleType("mac.models")
    models.agent_has_read_only_report_repository_executor = lambda _resources: True
    models.valid_read_only_report_repository_executor_attestation = lambda _value: True
    monkeypatch.setitem(sys.modules, "mac.models", models)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAC_HUB_URL", "http://mock-hub")
    monkeypatch.setenv("MAC_DEPLOY_GATE_ADMIN_TOKEN", "redacted-test-token")
    monkeypatch.setenv("MAC_DEPLOY_RELEASE_TS", "20260718T000000Z")
    monkeypatch.setenv(
        "MAC_DEPLOY_RELEASE_PLAN_B64",
        base64.b64encode(json.dumps(plan).encode("utf-8")).decode("ascii"),
    )

    if expected_error:
        with pytest.raises(RuntimeError, match=expected_error):
            exec(
                compile(embedded, "<embedded-fleet-epoch>", "exec"),
                {"__name__": "__main__"},
            )
        assert not (
            tmp_path / ".mac" / "logs" / "fleet-release-epoch-20260718T000000Z.json"
        ).exists()
        return

    exec(compile(embedded, "<embedded-fleet-epoch>", "exec"), {"__name__": "__main__"})

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema"] == schema
    assert receipt["deployment_holds_released"] == 1
    assert receipt["operator_holds_preserved"] == 0
    assert row["dispatch_hold"] is reported_hold
    assert ("POST", endpoint) in calls
    assert not any(method == "POST" and path != endpoint for method, path in calls)
    if successor_reason:
        assert receipt["outcome"] == "successor_hold"
        assert receipt["successor_hold_reason"] == successor_reason
        assert receipt["successor_holds_installed"] == 1
        assert row["dispatch_hold_reason"] == successor_reason
    else:
        assert "outcome" not in receipt
        assert "successor_holds_installed" not in receipt
        assert row["dispatch_hold_reason"] is None
    durable = tmp_path / ".mac" / "logs" / "fleet-release-epoch-20260718T000000Z.json"
    assert json.loads(durable.read_text(encoding="utf-8")) == receipt
    assert os.stat(durable).st_mode & 0o777 == 0o600


def test_remote_ssh_heredocs_keep_stdin_open():
    deploy = DEPLOY.read_text(encoding="utf-8")
    lines = deploy.splitlines()
    sites = []
    marker = re.compile(r"<<'(REMOTE(?:_[A-Z_]+)?|HUBSCRIPT)'")
    for index, line in enumerate(lines):
        match = marker.search(line)
        if match is None:
            continue
        context = "\n".join(lines[max(0, index - 4) : index + 1])
        ssh_start = context.rfind("ssh ")
        assert ssh_start >= 0, match.group(1)
        invocation = context[ssh_start:]
        assert not re.search(r"(?:^|\s)-n(?:\s|$)", invocation), match.group(1)
        sites.append(match.group(1))

    assert sites == [
        "REMOTE",
        "REMOTE_RESTORE",
        "REMOTE_HOLD_PREFLIGHT",
        "REMOTE_HUB_GATE",
        "REMOTE_FIRST_HUB_PREREQUISITES",
        "REMOTE_LEGACY_PREREQUISITES",
        "REMOTE",
        "REMOTE_RELEASE",
        "HUBSCRIPT",
        "REMOTE_ATTESTATION_RECOVERY",
        "REMOTE_ATTESTATION_SECOND_PROOF",
        "REMOTE_REPORT_EXECUTOR_APPROVAL",
        "REMOTE_RELEASE_STATUS",
        "REMOTE_RELEASE_EPOCH",
        "REMOTE_TYPED_BARRIER_RELEASE",
    ]


def test_legacy_hub_gate_does_not_import_post_upgrade_model_helpers(
    monkeypatch, capsys
):
    deploy = DEPLOY.read_text(encoding="utf-8")
    gate_start = deploy.index("hub_agent_restart_gate() {")
    python_marker = "\"$HOME/.mac/venv/bin/python\" - <<'PY'\n"
    python_start = deploy.index(python_marker, gate_start) + len(python_marker)
    gate = deploy[python_start : deploy.index("\nPY\nREMOTE_HUB_GATE", python_start)]

    row = {
        "id": AGENT,
        "dispatch_hold": True,
        "dispatch_hold_reason": "synchronized cut-over hold",
        "deleted_at": None,
        "last_seen_at": "2026-07-18T00:00:00+00:00",
        "status": "idle",
        "health_status": "healthy",
        "current_task_id": None,
        "resources": {},
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith(
            "/tasks?state=claimed"
        ) or request.full_url.endswith("/tasks?state=running"):
            return Response([])
        if request.full_url.endswith("/agents/" + AGENT):
            if request.get_method() == "PUT":
                row.update(json.loads(request.data))
            return Response(dict(row))
        raise AssertionError("unexpected legacy bootstrap request: " + request.full_url)

    environment = {
        "MAC_DEPLOY_GATE_PHASE": "legacy-bootstrap",
        "MAC_DEPLOY_GATE_AGENT_ID": AGENT,
        "MAC_DEPLOY_GATE_HOLD_REASON": "deployment hold",
        "MAC_DEPLOY_GATE_PRIOR_HOLD_REASON": "synchronized cut-over hold",
        "MAC_DEPLOY_GATE_PRIOR_OWNED": "0",
        "MAC_DEPLOY_GATE_ALLOW_MISSING": "0",
        "MAC_DEPLOY_GATE_REQUIRE_AUTHENTICATED": "0",
        "MAC_DEPLOY_GATE_REQUIRE_OWNED": "0",
        "MAC_DEPLOY_GATE_REQUIRE_REPORT_EXECUTOR": "0",
        "MAC_DEPLOY_GATE_TIMEOUT": "1",
        "MAC_HUB_URL": "http://legacy-hub",
        "MAC_DEPLOY_GATE_ADMIN_TOKEN": "redacted-test-token",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setitem(sys.modules, "mac.models", types.ModuleType("mac.models"))

    with pytest.raises(SystemExit) as stopped:
        exec(
            compile(gate, "<embedded-legacy-hub-gate>", "exec"),
            {"__name__": "__main__"},
        )

    assert stopped.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "baseline_seen": "2026-07-18T00:00:00+00:00",
        "exists": True,
        "owns_hold": False,
    }
    assert row["dispatch_hold"] is True
    assert row["status"] == "draining"


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
    python_marker = "\"$HOME/.mac/venv/bin/python\" - <<'PY'\n"
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
