"""The deploy script must name a stuck cohort epoch before it touches a node.

A transaction stuck in ``preparing`` since 2026-08-11, owned by a controller
that no longer existed, made the fleet undeployable for nine days. It presented
as an agent problem -- ``ERROR: jordanh-worker5: authoritative SSH route
resolved empty`` -- so three remedies were aimed at the agents (deleting them,
pruning the fleet registry, passing an explicit agent list) and none of them
could work, because the deploy replays the cohort pinned in the journal.

These tests drive the deploy script's own functions against a real dead-owner
journal and assert the diagnostic names the epoch, before any SSH is attempted.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
HELPER = ROOT / "deploy" / "fleet-cohort-transaction.py"
SOURCE = DEPLOY.read_text(encoding="utf-8")
SOURCE_COMMIT = "a" * 40
STUCK_EPOCH = f"{SOURCE_COMMIT}:20260811T211348Z:0123456789abcdef"
FRESH_EPOCH = f"{SOURCE_COMMIT}:20260820T000000Z:fedcba9876543210"
COHORT_NAMES = ("natasha", "bullwinkle", "jordanh-worker5")
# Only the first two survive in the frozen fleet registry.
REGISTRY_IDS = ("agent_natasha", "agent_bullwinkle")


def extract(name: str, next_marker: str) -> str:
    start = SOURCE.index(f"{name}() {{")
    body = SOURCE[start:]
    return body[: body.index(next_marker)].rstrip()


def diagnostics_source() -> str:
    # report_stuck_cohort_transactions and reap_cohort_transaction_journals sit
    # together, immediately before the pre-deploy recovery entry point.
    return extract(
        "report_stuck_cohort_transactions",
        "\nrecover_incomplete_cohort_transaction_before_deploy() {",
    )


def route_report_source() -> str:
    # cohort_pinned_epoch_id and report_absent_ssh_route are adjacent.
    return extract("cohort_pinned_epoch_id", "\nssh_control_path_for_agent() {")


def cohort_journal_source() -> str:
    return extract("cohort_journal", "\ncohort_journal_revision() {")


def stable_id_source() -> str:
    return extract(
        "stable_worker_agent_id", "\npersist_bounded_phase_failure_evidence() {"
    )


def run_journal(directory: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(HELPER), "--directory", str(directory), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def journal_file(directory: Path, epoch: str) -> Path:
    import hashlib

    return directory / f"transaction-{hashlib.sha256(epoch.encode()).hexdigest()}.json"


def make_stuck_journal(tmp_path: Path, epoch: str = STUCK_EPOCH) -> tuple[Path, int]:
    """A real journal, pinned to a real cohort, owned by a dead controller."""
    directory = tmp_path / "fleet-cohort-transactions"
    cohort_file = tmp_path / "cohort.json"
    cohort_file.write_text(
        json.dumps(
            [
                {
                    "name": name,
                    "stable_id": f"agent_{name.replace('-', '-')}",
                    "generation": f"generation-{index}",
                    "deployment_id": f"deployment-{index}",
                    "os": "linux",
                    "supervisor": "systemd",
                }
                for index, name in enumerate(COHORT_NAMES)
            ],
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    cohort_file.chmod(0o600)
    run_journal(
        directory,
        "init",
        "--epoch",
        epoch,
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260811T211348Z",
        "--fleet",
        "rocky",
        "--hub-agent",
        "rocky",
        "--cohort-file",
        str(cohort_file),
        "--owner-nonce",
        "dead-controller",
        "--owner-pid",
        str(os.getpid()),
    )
    pid = dead_pid()
    path = journal_file(directory, epoch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["owner"]["pid"] = pid
    payload["owner"]["process_start_sha256"] = "0" * 64
    payload["updated_at"] = "2026-08-11T21:13:48Z"
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return directory, pid


def preamble(
    journal_dir: Path,
    *,
    registry_ids: tuple[str, ...] = REGISTRY_IDS,
    active: int = 0,
    replay_epoch: str = "",
) -> str:
    registry = "\n".join(registry_ids)
    return "\n".join(
        [
            "set -euo pipefail",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            f"COHORT_JOURNAL_HELPER={shlex.quote(str(HELPER))}",
            f"COHORT_JOURNAL_DIR={shlex.quote(str(journal_dir))}",
            f"COHORT_EPOCH_ID={shlex.quote(FRESH_EPOCH)}",
            f"COHORT_JOURNAL_ACTIVE={active}",
            f"COHORT_REPLAY_EPOCH_ID={shlex.quote(replay_epoch)}",
            "FLEET_REGISTRY_SOURCE=/frozen/fleets.yaml",
            "fleet_config_query() {",
            f"  printf '%s' {shlex.quote(registry)}",
            "  [ -n " + shlex.quote(registry) + " ] && printf '\\n'",
            "  return 0",
            "}",
            cohort_journal_source(),
            stable_id_source(),
        ]
    )


def run_snippet(snippet: str, *, path_prefix: Path | None = None):
    environment = dict(os.environ)
    if path_prefix is not None:
        environment["PATH"] = f"{path_prefix}{os.pathsep}{environment['PATH']}"
    return subprocess.run(
        ["bash", "-c", snippet],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


@pytest.fixture
def ssh_tripwire(tmp_path: Path) -> Path:
    """An `ssh` on PATH that records any invocation and fails."""
    binary_dir = tmp_path / "tripwire-bin"
    binary_dir.mkdir()
    marker = binary_dir / "ssh-was-called"
    for name in ("ssh", "scp"):
        stub = binary_dir / name
        stub.write_text(
            f'#!/bin/sh\necho "$@" >> {shlex.quote(str(marker))}\nexit 1\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return binary_dir


def test_stuck_epoch_is_reported_by_epoch_before_any_ssh(
    tmp_path: Path, ssh_tripwire: Path
) -> None:
    directory, pid = make_stuck_journal(tmp_path)
    snippet = (
        preamble(directory)
        + "\n"
        + diagnostics_source()
        + "\nreport_stuck_cohort_transactions\n"
    )
    result = run_snippet(snippet, path_prefix=ssh_tripwire)
    # A cohort pinning a name the registry no longer has cannot be replayed, so
    # the diagnostic returns the refusal verdict instead of proceeding.
    assert result.returncode == 1
    report = result.stderr

    # The diagnostic, not the symptom: the epoch is the subject.
    assert STUCK_EPOCH in report
    assert "is stuck" in report
    assert "state=preparing" in report
    assert f"owner pid {pid}" in report
    assert "2026-08-11T21:13:48Z" in report
    # The pinned cohort is named, so the SSH failure has an explanation.
    for name in COHORT_NAMES:
        assert name in report
    # Nothing reached a node, so blocking on this epoch protects nothing.
    assert "blocking no completed work" in report
    # And the operator is told how to act on the epoch.
    assert f"status --epoch {STUCK_EPOCH}" in report
    assert f"recovery --epoch {STUCK_EPOCH}" in report

    # Nothing in the diagnostic path touched a node.
    assert not (ssh_tripwire / "ssh-was-called").exists()


def test_cohort_members_absent_from_the_registry_are_named(tmp_path: Path) -> None:
    directory, _pid = make_stuck_journal(tmp_path)
    snippet = (
        preamble(directory)
        + "\n"
        + diagnostics_source()
        + "\nreport_stuck_cohort_transactions\n"
    )
    result = run_snippet(snippet)
    assert result.returncode == 1
    report = result.stderr

    assert "NOT an enabled agent in /frozen/fleets.yaml (1)" in report
    assert "jordanh-worker5 (agent_jordanh-worker5)" in report
    # The two surviving members are not accused of being missing.
    absent_line = next(
        line for line in report.splitlines() if "NOT an enabled agent" in line
    )
    assert "natasha" not in absent_line
    assert "bullwinkle" not in absent_line
    # The three failed remedies are named so they are not attempted again.
    assert "deleting those agents" in report
    assert "passing an explicit agent list cannot clear it" in report


def test_a_resolvable_stuck_epoch_is_reported_but_still_recoverable(
    tmp_path: Path,
) -> None:
    """A dead owner alone is a diagnosis, not a refusal.

    Recovery is the normal, correct response to a crashed controller. Only a
    cohort the registry can no longer resolve makes the replay futile, so only
    that case blocks.
    """
    directory, _pid = make_stuck_journal(tmp_path)
    registry = tuple(f"agent_{name}" for name in COHORT_NAMES)
    snippet = (
        preamble(directory, registry_ids=registry)
        + "\n"
        + diagnostics_source()
        + "\nreport_stuck_cohort_transactions\n"
    )
    result = run_snippet(snippet)
    assert result.returncode == 0, result.stderr
    assert STUCK_EPOCH in result.stderr
    assert "is stuck" in result.stderr
    assert "NOT an enabled agent" not in result.stderr


def test_a_live_or_terminal_epoch_produces_no_stuck_report(tmp_path: Path) -> None:
    directory = tmp_path / "fleet-cohort-transactions"
    cohort_file = tmp_path / "cohort.json"
    cohort_file.write_text(
        json.dumps(
            [
                {
                    "name": "natasha",
                    "stable_id": "agent_natasha",
                    "generation": "generation-0",
                    "deployment_id": "deployment-0",
                    "os": "linux",
                    "supervisor": "systemd",
                }
            ]
        ),
        encoding="utf-8",
    )
    cohort_file.chmod(0o600)
    run_journal(
        directory,
        "init",
        "--epoch",
        FRESH_EPOCH,
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260820T000000Z",
        "--fleet",
        "rocky",
        "--hub-agent",
        "rocky",
        "--cohort-file",
        str(cohort_file),
        "--owner-nonce",
        "live-controller",
        "--owner-pid",
        str(os.getpid()),
    )
    snippet = (
        preamble(directory)
        + "\n"
        + diagnostics_source()
        + "\nreport_stuck_cohort_transactions\n"
    )
    result = run_snippet(snippet)
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == ""


def test_empty_ssh_route_names_the_absent_agent_and_the_pinning_epoch(
    tmp_path: Path,
) -> None:
    directory, _pid = make_stuck_journal(tmp_path)
    snippet = (
        preamble(directory, replay_epoch=STUCK_EPOCH)
        + "\n"
        + route_report_source()
        + "\nreport_absent_ssh_route jordanh-worker5\n"
    )
    result = run_snippet(snippet)
    assert result.returncode == 0, result.stderr
    report = result.stderr

    # The old message blamed the agent for an empty route and stopped there.
    assert "no SSH route exists because agent_jordanh-worker5 is not an enabled agent" in report
    assert "/frozen/fleets.yaml" in report
    # The replayed epoch is named, not the fresh one this invocation would use.
    assert STUCK_EPOCH in report
    assert FRESH_EPOCH not in report
    assert "every deploy replays until it reaches a terminal state" in report
    assert f"status --epoch {STUCK_EPOCH}" in report


def test_registered_agent_with_no_route_is_not_blamed_on_the_registry(
    tmp_path: Path,
) -> None:
    directory, _pid = make_stuck_journal(tmp_path)
    snippet = (
        preamble(directory)
        + "\n"
        + route_report_source()
        + "\nreport_absent_ssh_route natasha\n"
    )
    result = run_snippet(snippet)
    assert result.returncode == 0, result.stderr
    report = result.stderr
    assert "authoritative SSH route resolved empty" in report
    assert "IS an enabled agent in /frozen/fleets.yaml" in report
    assert "not an enabled agent" not in report
    # No transaction is active here, so no epoch is asserted.
    assert STUCK_EPOCH not in report


def test_retention_pass_reaps_aged_terminal_journals_but_not_the_stuck_one(
    tmp_path: Path,
) -> None:
    directory, _pid = make_stuck_journal(tmp_path)
    # A finished epoch from a month ago, which nothing used to remove.
    terminal_epoch = f"{SOURCE_COMMIT}:20260701T000000Z:terminal"
    cohort_file = tmp_path / "cohort-terminal.json"
    cohort_file.write_text(
        json.dumps(
            [
                {
                    "name": "natasha",
                    "stable_id": "agent_natasha",
                    "generation": "generation-0",
                    "deployment_id": "deployment-0",
                    "os": "linux",
                    "supervisor": "systemd",
                }
            ]
        ),
        encoding="utf-8",
    )
    cohort_file.chmod(0o600)
    # `init` refuses a second live epoch, so fork the terminal one durably.
    scratch = tmp_path / "scratch-journal"
    run_journal(
        scratch,
        "init",
        "--epoch",
        terminal_epoch,
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260701T000000Z",
        "--fleet",
        "rocky",
        "--hub-agent",
        "rocky",
        "--cohort-file",
        str(cohort_file),
        "--owner-nonce",
        "old-controller",
        "--owner-pid",
        str(os.getpid()),
    )
    run_journal(
        scratch,
        "abort",
        "--epoch",
        terminal_epoch,
        "--expected-revision",
        "0",
        "--operation-id",
        "abort-old",
        "--owner-nonce",
        "old-controller",
    )
    source = journal_file(scratch, terminal_epoch)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["updated_at"] = "2026-07-01T00:00:00Z"
    target = journal_file(directory, terminal_epoch)
    target.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)

    snippet = (
        preamble(directory)
        # Retention knobs reach the helper through the environment.
        + "\nexport MAC_FLEET_COHORT_JOURNAL_RETENTION_DAYS=14"
        + "\nexport MAC_FLEET_COHORT_JOURNAL_RETENTION_KEEP_COUNT=0\n"
        + diagnostics_source()
        + "\nreap_cohort_transaction_journals\n"
    )
    result = run_snippet(snippet)
    assert result.returncode == 0, result.stderr
    assert "reaped 1 terminal cohort journal(s)" in result.stdout
    assert "older than 14 days" in result.stdout
    assert not target.exists()
    # The stuck, non-terminal journal is state, not evidence: it survives.
    assert journal_file(directory, STUCK_EPOCH).exists()


def test_deploy_reports_and_reaps_before_it_replays_a_pinned_cohort() -> None:
    """Order matters: the diagnostic must precede discover/adopt/recover."""
    entry = SOURCE.index("recover_incomplete_cohort_transaction_before_deploy() {")
    body = SOURCE[entry : SOURCE.index("\ncommit_fleet_release_epoch() {", entry)]
    assert body.index("report_stuck_cohort_transactions") < body.index(
        "cohort_journal discover"
    )
    # And the refusal short-circuits before adopt/recover, never after.
    assert body.index("refusing to replay a stuck cohort epoch") < body.index(
        "cohort_journal adopt"
    )
    # main() reconciles first, then applies bounded retention.
    main_body = SOURCE[SOURCE.index("\nmain() {") :]
    assert main_body.index("recover_incomplete_cohort_transaction_before_deploy\n") < (
        main_body.index("reap_cohort_transaction_journals\n")
    )
