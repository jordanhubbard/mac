"""Regression coverage for durable, secret-safe bounded-phase failure evidence.

`run_bounded_node_phase` writes per-agent phase logs under `TMPDIR_LOCAL`, and
the `cleanup_local_deployment` EXIT trap removes that directory wholesale after
fix-forward recovery. That previously made a failed phase opaque: the reported
log path no longer existed. These tests prove that each failed bounded phase now
persists a sanitized, owner-only (0600) JSON artifact outside `TMPDIR_LOCAL`,
bound to the source commit, cohort epoch, agent id, phase, exit status, and a
SHA-256 digest, and that the artifact survives the cleanup that wipes the secret
staging tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"

GIT_REV = "a" * 40
DEPLOY_TS = "20260724T010203Z"
COHORT_EPOCH = f"{GIT_REV}:{DEPLOY_TS}:0123456789abcdef0123456789abcdef"


def _extract(name: str, next_marker: str) -> str:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    body = source[start:]
    end = body.index(next_marker)
    return body[:end].rstrip()


def _writer_source() -> str:
    return _extract(
        "persist_bounded_phase_failure_evidence",
        "\nrun_bounded_node_phase() {",
    )


def _bounded_phase_source() -> str:
    # The writer sits immediately before run_bounded_node_phase; grab both so
    # the reporting loop can call the writer.
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index("persist_bounded_phase_failure_evidence() {")
    body = source[start:]
    end = body.index("\npreflight_probe_helper_source")
    return body[:end].rstrip()


def _emitter_source() -> str:
    return _extract(
        "emit_bounded_phase_failure_evidence_recovery_summary",
        "\nrecover_active_cohort_transaction_v2() {",
    )


def _stable_id_stub() -> str:
    # Deterministic, dependency-free stand-in for stable_worker_agent_id.
    return (
        "stable_worker_agent_id() {\n"
        '  printf '"'"'agent_%s\\n'"'"' '
        '"$(printf '"'"'%s'"'"' "$1" | tr '"'"'A-Z'"'"' '"'"'a-z'"'"' '
        '| tr -c '"'"'a-z0-9_.-'"'"' '"'"'_'"'"')"\n'
        "}\n"
    )


def _preamble(evidence_dir: Path, tmpdir_local: Path) -> str:
    return "\n".join(
        [
            "set -u",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            f"GIT_REV={GIT_REV}",
            f"TS={DEPLOY_TS}",
            f"COHORT_EPOCH_ID={shlex.quote(COHORT_EPOCH)}",
            f"TMPDIR_LOCAL={shlex.quote(str(tmpdir_local))}",
            "NODE_PARALLELISM=2",
            "BOUNDED_PHASE_FAILURE_EVIDENCE_DIR="
            + shlex.quote(str(evidence_dir)),
            'BOUNDED_PHASE_FAILURE_EVIDENCE_PATHS=""',
            _stable_id_stub(),
        ]
    )


def test_failed_bounded_phase_persists_sanitized_owner_only_artifact(tmp_path):
    evidence_dir = tmp_path / "durable-logs"
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()
    log = tmpdir_local / "phase-phase2-arm-agent_worker4.log"
    log.write_text(
        "\n".join(
            [
                "starting phase2-arm",
                "MAC_DEPLOY_HUB_TOKEN=supersecrettoken123456",
                "Authorization: Bearer aaa.bbb.cccdddeee",
                "provider key=secret:hub-bearer-material",
                "review_key_b64=" + "Q" * 96,
                "harmless progress line",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snippet = (
        _preamble(evidence_dir, tmpdir_local)
        + "\n"
        + _writer_source()
        + "\n"
        + "persist_bounded_phase_failure_evidence "
        + "phase2-arm worker4 agent_worker4 7 "
        + shlex.quote(str(log))
        + "\n"
    )
    result = subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    artifact = Path(result.stdout.strip())
    assert artifact.is_file()
    assert artifact.parent == evidence_dir
    # The durable artifact must be owner-only (mode 0600).
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["schema"] == "mac.fleet_bounded_phase_failure_evidence.v1"
    assert payload["phase"] == "phase2-arm"
    assert payload["agent"] == "worker4"
    assert payload["stable_id"] == "agent_worker4"
    assert payload["exit_status"] == 7
    assert payload["source_commit"] == GIT_REV
    assert payload["cohort_epoch"] == COHORT_EPOCH
    assert payload["deploy_ts"] == DEPLOY_TS
    assert payload["log_present"] is True

    sanitized = payload["sanitized_log"]
    # No node secret payload, hub bearer material, or credential blob survives.
    for secret in (
        "supersecrettoken123456",
        "aaa.bbb.cccdddeee",
        "hub-bearer-material",
        "Q" * 96,
    ):
        assert secret not in sanitized
        assert secret not in artifact.read_text(encoding="utf-8")
    assert "<redacted>" in sanitized
    # The digest binds the sanitized body.
    assert payload["sanitized_sha256"] == hashlib.sha256(
        sanitized.encode("utf-8")
    ).hexdigest()


def test_cleanup_removes_secret_staging_but_artifact_survives(tmp_path):
    evidence_dir = tmp_path / "durable-logs"
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()
    # Secret staging material that the EXIT trap wipes with TMPDIR_LOCAL.
    secret_stage = tmpdir_local / "hub-authority.json"
    secret_stage.write_text("MAC_DEPLOY_HUB_TOKEN=topsecret999\n", encoding="utf-8")
    log = tmpdir_local / "phase-phase2-arm-agent_worker4.log"
    log.write_text("MAC_DEPLOY_HUB_TOKEN=topsecret999\nboom\n", encoding="utf-8")

    snippet = (
        _preamble(evidence_dir, tmpdir_local)
        + "\n"
        + _writer_source()
        + "\n"
        + 'artifact="$(persist_bounded_phase_failure_evidence '
        + "phase2-arm worker4 agent_worker4 7 "
        + shlex.quote(str(log))
        + ')"\n'
        # Emulate the cleanup EXIT trap wiping the entire temp directory.
        + 'rm -rf "$TMPDIR_LOCAL"\n'
        + 'printf '"'"'%s\\n'"'"' "$artifact"\n'
    )
    result = subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    artifact = Path(result.stdout.strip())

    # Secret staging is gone with TMPDIR_LOCAL.
    assert not secret_stage.exists()
    assert not tmpdir_local.exists()
    # The sanitized 0600 artifact survives cleanup.
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert "topsecret999" not in artifact.read_text(encoding="utf-8")
    assert payload["exit_status"] == 7


def test_reporting_loop_surfaces_durable_path_in_controller_output(tmp_path):
    evidence_dir = tmp_path / "durable-logs"
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()
    specs = tmp_path / "specs"
    specs.write_text("worker4|\n", encoding="utf-8")

    worker = tmp_path / "fake-worker.sh"
    worker.write_text(
        "#!/usr/bin/env bash\n"
        'echo "MAC_DEPLOY_HUB_TOKEN=leakme123456"\n'
        'echo "phase2-arm failed hard"\n'
        "exit 7\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)

    snippet = (
        _preamble(evidence_dir, tmpdir_local)
        + "\n"
        + "BOUNDED_NODE_PHASE_AGGREGATE_FAILURES=0\n"
        + _bounded_phase_source()
        + "\n"
        + "set +e\n"
        + "run_bounded_node_phase "
        + shlex.quote(str(specs))
        + " phase2-arm "
        + shlex.quote(str(worker))
        + "\n"
        + "rc=$?\n"
        + "set -e\n"
        + 'printf '"'"'RC=%s\\n'"'"' "$rc"\n'
        + 'printf '"'"'PATHS=%b'"'"' "$BOUNDED_PHASE_FAILURE_EVIDENCE_PATHS"\n'
    )
    result = subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True, check=False
    )
    assert "RC=1" in result.stdout, result.stderr + result.stdout
    # Controller stderr names the durable artifact for the failed phase.
    assert "failure_evidence=" in result.stderr
    assert "failure_evidence=unavailable" not in result.stderr
    # The collected path is surfaced for recovery evidence.
    paths = [
        line for line in result.stdout.splitlines() if line.startswith("PATHS=")
    ]
    assert paths, result.stdout
    reported = paths[0][len("PATHS="):]
    artifact = Path(reported.strip())
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert "leakme123456" not in artifact.read_text(encoding="utf-8")


def test_recovery_summary_emits_collected_failure_evidence_paths(tmp_path):
    evidence_dir = tmp_path / "durable-logs"
    tmpdir_local = tmp_path / "tmpdir-local"
    tmpdir_local.mkdir()
    fake_artifact = evidence_dir / "phase-failure-x.json"
    evidence_dir.mkdir()
    fake_artifact.write_text("{}\n", encoding="utf-8")

    snippet = "\n".join(
        [
            "set -u",
            "BOUNDED_PHASE_FAILURE_EVIDENCE_PATHS="
            + shlex.quote(str(fake_artifact) + "\\n"),
            _emitter_source(),
            "emit_bounded_phase_failure_evidence_recovery_summary",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "recovery references durable bounded-phase failure evidence" in result.stdout
    assert f"failure_evidence={fake_artifact}" in result.stdout


def test_cleanup_trap_never_removes_the_durable_evidence_directory():
    source = DEPLOY.read_text(encoding="utf-8")
    cleanup = source.split("cleanup_local_deployment() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    # The EXIT trap wipes SSH control + TMPDIR_LOCAL only, never the durable
    # failure-evidence directory.
    assert 'rm -rf "$SSH_CONTROL_DIR" "$TMPDIR_LOCAL"' in cleanup
    assert "BOUNDED_PHASE_FAILURE_EVIDENCE_DIR" not in cleanup
    # The durable directory is defined outside TMPDIR_LOCAL.
    assert (
        'BOUNDED_PHASE_FAILURE_EVIDENCE_DIR="${MAC_FLEET_PHASE_FAILURE_EVIDENCE_DIR:-$HOME/.mac/logs}"'
        in source
    )
