from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"


def _function(source: str, name: str, next_name: str) -> str:
    return (
        f"{name}() {{"
        + source.split(f"{name}() {{", 1)[1].split(f"\n}}\n\n{next_name}", 1)[0]
        + "\n}"
    )


def test_parallel_typed_barriers_keep_wal_parent_owned_and_ordered() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    typed = source.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain() {", 1)[0]

    ordered = (
        "cohort_journal_mutate phase1-prepare-start",
        'run_bounded_node_phase "$selected_specs_file" phase1-prepare',
        "cohort_journal_mutate phase1-armed",
        'run_bounded_node_phase "$selected_specs_file" prerequisites',
        'run_bounded_node_phase "$selected_specs_file" stage-bundle',
        "build_and_open_hub_epoch",
        "cohort_journal_mutate quiesce-start",
        'typed_quiesce_worker "$spec"',
        "cohort_journal_mutate quiesced",
        'run_bounded_node_phase "$selected_specs_file" phase2-arm',
        "cohort_journal_mutate phase2-armed",
        "cohort_journal_mutate phase2-start",
        'run_bounded_node_phase "$selected_specs_file" phase2-apply',
        "cohort_journal_mutate prepared",
        "prove_and_commit_hub_epoch",
        "cohort_journal_mutate finalize-start",
        'run_bounded_node_phase "$selected_specs_file" finalize-node',
        "cohort_journal_mutate finalized-node",
    )
    positions = [typed.index(value) for value in ordered]
    assert positions == sorted(positions)
    quiescence = typed.split(
        'echo "==> fleet: quiescing the exact cohort under hub epoch ownership"', 1
    )[1].split(
        'echo "==> fleet: installing immutable finalizers and arming phase-2 rollback"',
        1,
    )[0]
    assert quiescence.count("while IFS= read -r spec; do") == 1
    assert 'run_bounded_node_phase "$selected_specs_file" quiesce' not in quiescence

    workers = source.split("typed_phase1_prepare_worker() {", 1)[1].split(
        "\nrun_typed_cohort() {", 1
    )[0]
    assert "cohort_journal_mutate" not in workers
    bounded = source.split("run_bounded_node_phase() {", 1)[1].split(
        "\n}\n\npreflight_probe_helper_source", 1
    )[0]
    assert 'while [ "$active" -gt 0 ]' in bounded
    assert "phase_status_files" in bounded
    assert '[ "$failed" -ne 0 ] && [ "$aggregate_failures" != 1 ]' in bounded
    assert 'while [ "$index" -lt "$total" ]' in bounded


def test_cohort_journal_mutation_failure_preserves_revision() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    mutate = _function(
        source,
        "cohort_journal_mutate",
        "write_cohort_transaction_input() {",
    )
    snippet = f"""set -u
PYTHON_BIN={shlex.quote(sys.executable)}
COHORT_JOURNAL_REVISION=7
cohort_journal() {{ printf '%s\n' '{{"error":"rejected"}}' >&2; return 17; }}
cohort_journal_revision() {{ "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["journal"]["revision"])'; }}
{mutate}
set +e
if cohort_journal_mutate phase epoch 7 operation owner >/dev/null; then result=0; else result=$?; fi
printf '%s|%s\n' "$result" "$COHORT_JOURNAL_REVISION"
"""
    result = subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1|7"


def test_bounded_scheduler_stops_new_work_after_first_failure(tmp_path: Path) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    bounded = _function(
        source,
        "run_bounded_node_phase",
        "preflight_probe_helper_source() {",
    )
    specs = tmp_path / "specs"
    specs.write_text("a|x\nb|x\nc|x\nd|x\n", encoding="utf-8")
    started = tmp_path / "started"
    output = tmp_path / "output"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
NODE_PARALLELISM=2
STARTED={shlex.quote(str(started))}
stable_worker_agent_id() {{ printf 'agent_%s\n' "$1"; }}
worker() {{
  agent="${{1%%|*}}"
  printf '%s\n' "$agent" >> "$STARTED"
  case "$agent" in
    a) sleep 0.20; return 0 ;;
    b) sleep 0.02; return 7 ;;
    *) return 0 ;;
  esac
}}
{bounded}
set +e
run_bounded_node_phase "$1" fixture worker > {shlex.quote(str(output))} 2>&1
result=$?
set -e
printf '%s\n' "$result"
"""
    result = subprocess.run(
        ["bash", "-c", snippet, "scheduler", str(specs)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    assert set(started.read_text(encoding="utf-8").splitlines()) == {"a", "b"}
    rendered = output.read_text(encoding="utf-8")
    assert rendered.index("a: fixture passed") < rendered.index("b: fixture failed")


def test_bounded_scheduler_reaps_child_killed_before_status(tmp_path: Path) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    bounded = _function(
        source,
        "run_bounded_node_phase",
        "preflight_probe_helper_source() {",
    )
    specs = tmp_path / "specs"
    specs.write_text("killed|x\nnever|x\n", encoding="utf-8")
    started = tmp_path / "started"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
NODE_PARALLELISM=1
STARTED={shlex.quote(str(started))}
stable_worker_agent_id() {{ printf 'agent_%s\n' "$1"; }}
worker() {{
  agent="${{1%%|*}}"
  printf '%s\n' "$agent" >> "$STARTED"
  kill -KILL "$BASHPID"
}}
{bounded}
set +e
run_bounded_node_phase "$1" killed worker >/dev/null 2>&1
result=$?
set -e
printf '%s\n' "$result"
"""
    result = subprocess.run(
        ["bash", "-c", snippet, "scheduler", str(specs)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    assert started.read_text(encoding="utf-8").splitlines() == ["killed"]


def test_bounded_scheduler_can_aggregate_every_preflight_failure(
    tmp_path: Path,
) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    bounded = _function(
        source,
        "run_bounded_node_phase",
        "preflight_probe_helper_source() {",
    )
    specs = tmp_path / "specs"
    specs.write_text("a|x\nb|x\nc|x\n", encoding="utf-8")
    started = tmp_path / "started"
    output = tmp_path / "output"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
NODE_PARALLELISM=2
STARTED={shlex.quote(str(started))}
BOUNDED_NODE_PHASE_AGGREGATE_FAILURES=1
stable_worker_agent_id() {{ printf 'agent_%s\n' "$1"; }}
worker() {{
  agent="${{1%%|*}}"
  printf '%s\n' "$agent" >> "$STARTED"
  case "$agent" in a|c) return 7 ;; *) return 0 ;; esac
}}
{bounded}
set +e
run_bounded_node_phase "$1" preflight worker > {shlex.quote(str(output))} 2>&1
result=$?
set -e
printf '%s\n' "$result"
"""
    result = subprocess.run(
        ["bash", "-c", snippet, "scheduler", str(specs)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    assert set(started.read_text(encoding="utf-8").splitlines()) == {"a", "b", "c"}
    rendered = output.read_text(encoding="utf-8")
    assert "a: preflight failed" in rendered
    assert "b: preflight passed" in rendered
    assert "c: preflight failed" in rendered


def test_prerequisite_worker_propagates_remote_builder_failure(tmp_path: Path) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    prepare = _function(
        source,
        "prepare_remote_prerequisite_bundle",
        "collect_phase2_arm_evidence() {",
    )
    snippet = f"""set -u
DEPLOY_CONTROLLER_NONCE=fixture
PREREQUISITE_RECEIPT_HELPER=/tmp/fleet-prerequisite-receipts.py
MAC_REVIEWED_CODEGRAPH_VERSION=fixture
PYTHON_BIN=/bin/false
deployment_id_for_agent() {{ printf 'deployment-fixture\n'; }}
stable_worker_agent_id() {{ printf 'agent_%s\n' "$1"; }}
node_route_identity_sha256() {{ printf '%064d\n' 0; }}
node_prerequisite_bundle_file() {{ printf '%s/bundle.json\n' {shlex.quote(str(tmp_path))}; }}
node_prerequisite_expectations_file() {{ printf '%s/expectations.json\n' {shlex.quote(str(tmp_path))}; }}
fenced_remote_upload() {{ return 0; }}
ssh_target_args() {{ printf 'fixture-target\\0'; }}
remote_deployment_fenced_exec() {{ printf 'fixture-command\n'; }}
shell_quote() {{ printf '%s' "$1"; }}
ssh() {{ return 23; }}
{prepare}
set +e
prepare_remote_prerequisite_bundle 'node|unused|linux'
result=$?
set -e
printf '%s\n' "$result"
"""
    result = subprocess.run(
        ["bash", "-c", snippet],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    assert "remote prerequisite proof failed" in result.stderr
    assert "eight exact prerequisite participants proved" not in result.stderr


def test_typed_arm_and_apply_reuse_one_digest_verified_stage() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    stage = source.split("stage_remote_deployment_bundle() {", 1)[1].split(
        "\n}\n\nassert_prerequisite_remaining_budget", 1
    )[0]
    deploy_host = source.split("deploy_host() {", 1)[1].split(
        "\n}\n\nrestart_remote_mac_agent_under_epoch", 1
    )[0]
    verifier = source.split("staged_bundle_verifier_source() {", 1)[1].split(
        "\n}\n\nstage_remote_file_once_exact", 1
    )[0]

    for name in (
        "release.tar.gz",
        "fleets.yaml",
        "fleet-node-install.sh",
        "reviewed-tool-assets.sh",
        "launchd-lifecycle.sh",
        "rollback-supervisor.py",
        "prerequisite-receipts.py",
        "prerequisite-bundle.json",
        "prerequisite-expectations.json",
    ):
        assert name in stage
        assert name in verifier
    assert "stage_remote_file_once_exact" in stage
    assert "except FileExistsError: pass" in stage
    assert "os.fchmod(directory_fd,0o700)" in stage
    assert "could not normalize stage directory mode" in stage
    assert '"$ssh_target" "$command" || return 1' in stage
    assert '"$source" "$remote_root/$destination" || return 1' in stage
    assert '"$manifest" "$remote_root/manifest.json" || return 1' in stage
    assert '"$ssh_target" "$command" > "$receipt" || return 1' in stage
    assert "remote staged deployment receipt is invalid" in stage
    assert "<<'PY' || return 1" in stage
    assert "existing staged item differs from controller digest" in source
    assert 'if [ "$typed_staged_bundle" = 0 ]' in deploy_host
    assert "reusing digest-bound staged deployment bundle" in deploy_host
    assert r'python3 -c \"\$_mac_verify\"' in deploy_host
    assert deploy_host.index(r'python3 -c \"\$_mac_verify\"') < deploy_host.index(
        r'bash \"\$_mac_script\" \"\$_mac_action\"'
    )
    assert "O_NOFOLLOW" in verifier
    assert "manifest_digest != expected_manifest_digest" in verifier
    assert "receipt.get(\"items\") != proved" in verifier


def test_stage_cleanup_is_terminal_for_finalize_and_abort_recovery() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    finalize = source.split("finalize_remote_deployment_release() {", 1)[1].split(
        "\n}\n\nrefresh_release_ready_quiescence", 1
    )[0]
    recovery = source.split("recover_cohort_node() {", 1)[1].split(
        "\n}\n\nrecover_active_cohort_transaction", 1
    )[0]
    assert finalize.index("cleanup_remote_staged_deployment_bundle") < finalize.index(
        "release_remote_deployment_lock"
    )
    assert recovery.index("cleanup_remote_staged_deployment_bundle") < recovery.index(
        "release_remote_deployment_lock"
    )
    typed = source.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain() {", 1)[0]
    assert "cleanup_remote_staged_deployment_bundle" not in typed


def test_stage_cleanup_normalizes_only_owner_private_inherited_setgid() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    cleanup = source.split("staged_bundle_cleanup_source() {", 1)[1].split(
        "\n}\n\ncleanup_remote_staged_deployment_bundle", 1
    )[0]
    cleanup = cleanup.split("cat <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

    legacy = Path(tempfile.mkdtemp(prefix="mac-deploy-stage-legacy-", dir="/tmp"))
    legacy.chmod(0o2700)
    nested = legacy / "nested"
    nested.mkdir()
    (nested / "payload").write_text("staged", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", cleanup, str(legacy)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not legacy.exists()

    unsafe = Path(tempfile.mkdtemp(prefix="mac-deploy-stage-unsafe-", dir="/tmp"))
    unsafe.chmod(0o755)
    result = subprocess.run(
        [sys.executable, "-c", cleanup, str(unsafe)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unsafe staged bundle cleanup root" in result.stderr
    assert unsafe.exists()
    unsafe.rmdir()


def test_finalizer_install_is_destination_atomic_and_phase_workers_fail_fast(
    tmp_path: Path,
) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    installer = source.split("node_finalizer_installer_source() {", 1)[1].split(
        "\n}\n\ninstall_remote_node_finalizer", 1
    )[0]
    installer = installer.split("cat <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

    destination = tmp_path / "finalizers"
    destination.mkdir(mode=0o700)
    staged = tmp_path / "staged-finalizer.py"
    payload = b"print('finalize')\n"
    staged.write_bytes(payload)
    target = destination / "generation.py"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            installer,
            str(staged),
            str(target),
            hashlib.sha256(payload).hexdigest(),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not staged.exists()
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o7777 == 0o700
    assert "dir=str(target.parent)" in installer
    assert "os.replace(temporary, target)" in installer
    assert "os.replace(source, target)" not in installer

    arm = source.split("typed_phase2_arm_worker() {", 1)[1].split(
        "\n}\n\ntyped_phase2_apply_worker", 1
    )[0]
    apply = source.split("typed_phase2_apply_worker() {", 1)[1].split(
        "\n}\n\ntyped_finalize_worker", 1
    )[0]
    finalize = source.split("typed_finalize_worker() {", 1)[1].split(
        "\n}\n\nrun_typed_cohort", 1
    )[0]
    assert "install_remote_node_finalizer \"$agent\" || return 1" in arm
    assert arm.count("|| return 1") >= 3
    assert apply.count("|| return 1") >= 4
    assert finalize.count("|| return 1") >= 4


def test_node_installer_remains_compatible_with_macos_bash_32() -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    assert "mapfile" not in source
    result = subprocess.run(
        ["/bin/bash", "-n", str(NODE_INSTALL)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_prerequisite_budget_rejects_insufficient_remaining_lifetime(tmp_path: Path) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    budget = _function(
        source,
        "assert_prerequisite_remaining_budget",
        "reconcile_bound_worker_attestation_key() (",
    )
    fresh = tmp_path / "fresh.json"
    stale = tmp_path / "stale.json"
    fresh.write_text(
        json.dumps(
            {
                "schema": "mac.fleet_prerequisite_bundle.v1",
                "agent_id": "natasha",
                "created_at_epoch": time.time() - 100,
            }
        ),
        encoding="utf-8",
    )
    stale.write_text(
        json.dumps(
            {
                "schema": "mac.fleet_prerequisite_bundle.v1",
                "agent_id": "natasha",
                "created_at_epoch": time.time() - 3500,
            }
        ),
        encoding="utf-8",
    )
    snippet = f"""set -u
PYTHON_BIN=python3
BUNDLE=$1
node_prerequisite_bundle_file() {{ printf '%s\n' "$BUNDLE"; }}
{budget}
assert_prerequisite_remaining_budget natasha 120
"""
    ok = subprocess.run(
        ["bash", "-c", snippet, "budget", str(fresh)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    expired = subprocess.run(
        ["bash", "-c", snippet, "budget", str(stale)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert expired.returncode != 0
    assert "below the 120s phase budget" in expired.stderr


def test_preflight_receipt_is_read_only_canonical_and_rejects_aliases() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    main = source.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    preflight = source.split("run_preflight_qualification() {", 1)[1].split(
        "\n}\n\nstaged_bundle_remote_root_for_deployment", 1
    )[0]
    receipt = source.split("write_preflight_qualification_receipt() {", 1)[1].split(
        "\n}\n\nrun_preflight_qualification", 1
    )[0]
    unique = source.split("assert_unique_selected_endpoint_identities() {", 1)[1].split(
        "\n}\n\nnode_route_identity_file", 1
    )[0]

    assert '"schema": "mac.fleet_preflight_qualification.v1"' in receipt
    assert '"status": "passed"' in receipt
    assert '"read_only": True' in receipt
    assert '"authorizes_deployment": False' in receipt
    assert "canonical = json.dumps(payload, sort_keys=True" in receipt
    assert '"endpoint_identity_sha256": endpoint_digest' in receipt
    assert '"probe_evidence_sha256": probe_digest' in receipt
    assert "reviewed_openshell_cli" in source
    assert "reviewed_codegraph_runtime" in source
    assert "assert_unique_selected_endpoint_identities" in preflight
    assert "BOUNDED_NODE_PHASE_AGGREGATE_FAILURES=1" in preflight
    assert "selected aliases resolve to one physical endpoint" in unique
    preflight_branch = main.rsplit('if [ "$PREFLIGHT_ONLY" = 1 ]; then', 1)[1].split(
        "\n  fi", 1
    )[0]
    assert "initialize_cohort_transaction" not in preflight_branch
    assert "cohort_journal_mutate" not in preflight_branch
    assert "run_preflight_qualification" in preflight_branch
