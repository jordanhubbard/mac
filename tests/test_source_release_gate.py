from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

import mac.source_release_gate as gate_module
from mac.models import ValidationError
from mac.source_release_gate import CommandResult, SourceReleaseGate


SHA = "a" * 40
MOVED_SHA = "b" * 40


class FakeRunner:
    def __init__(self, *, moved: bool = False, test_returncode: int = 0) -> None:
        self.calls = []
        self.moved = moved
        self.test_returncode = test_returncode
        self.remote_resolves = 0

    def __call__(self, argv, cwd: Path, timeout: int) -> CommandResult:
        args = tuple(argv)
        self.calls.append((args, Path(cwd), timeout))
        stdout = ""
        returncode = 0
        if args[:3] == ("git", "rev-parse", "--verify"):
            self.remote_resolves += 1
            stdout = MOVED_SHA if self.moved and self.remote_resolves > 1 else SHA
        elif args == ("git", "remote", "get-url", "origin"):
            stdout = "https://github.com/example/mac.git\n"
        elif args[:3] == ("git", "ls-tree", "-r"):
            stdout = "100644 blob deadbeef\tREADME.md\n"
        elif args[:4] == ("git", "worktree", "add", "--detach"):
            stage = Path(args[4])
            (stage / ".venv/bin").mkdir(parents=True)
            (stage / ".venv/bin/python").write_text("")
        elif args == ("scripts/run-contract-tests.sh",):
            returncode = self.test_returncode
        elif args == ("git", "rev-parse", "HEAD"):
            stdout = SHA
        elif args == ("git", "status", "--porcelain"):
            stdout = ""
        return CommandResult(returncode, stdout, "", 0.01)


def _gate(tmp_path: Path, runner: FakeRunner) -> SourceReleaseGate:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: /tmp/fake\n")
    return SourceReleaseGate(
        repository,
        runner=runner,
        required_checks=lambda _remote, _branch: ("contract",),
        check_verdicts=lambda _remote, _sha, checks: {
            "known": True,
            "passed": list(checks),
            "pending": [],
            "failed": [],
        },
        stage_root=tmp_path / "staging",
    )


def test_stage_freezes_sha_checks_ci_runs_contracts_and_records_digests(tmp_path: Path):
    runner = FakeRunner()

    staged = _gate(tmp_path, runner).stage_approved_current(
        transaction_id="upgrade-1",
        branch="main",
    )

    assert staged.commit_sha == SHA
    assert staged.tree_digest.startswith("sha256:")
    assert staged.evidence["ci"]["passed"] == ["contract"]
    assert staged.evidence["local_contract_tests"]["status"] == "passed"
    assert staged.evidence_digest.startswith("sha256:")
    assert ("scripts/run-contract-tests.sh",) in [call[0] for call in runner.calls]


def test_stage_rejects_non_green_ci_before_creating_generation(tmp_path: Path):
    runner = FakeRunner()
    gate = _gate(tmp_path, runner)
    gate.check_verdicts = lambda _remote, _sha, _checks: {
        "known": True,
        "passed": [],
        "pending": [],
        "failed": ["contract"],
    }

    with pytest.raises(ValidationError, match="not green"):
        gate.stage_approved_current(transaction_id="upgrade-2")

    assert not any(call[0][:3] == ("git", "worktree", "add") for call in runner.calls)


def test_stage_rejects_failed_local_contract_tests(tmp_path: Path):
    runner = FakeRunner(test_returncode=1)

    with pytest.raises(ValidationError, match="contract tests failed"):
        _gate(tmp_path, runner).stage_approved_current(transaction_id="upgrade-3")


def test_stage_rejects_branch_that_moves_during_proof(tmp_path: Path):
    runner = FakeRunner(moved=True)

    with pytest.raises(ValidationError, match="branch moved"):
        _gate(tmp_path, runner).stage_approved_current(transaction_id="upgrade-4")


def test_registered_release_is_reverified_retested_and_can_be_discarded(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    gate = _gate(tmp_path, runner)
    tree_material = "100644 blob deadbeef\tREADME.md\n"
    tree_digest = "sha256:" + hashlib.sha256(tree_material.encode("utf-8")).hexdigest()

    staged = gate.stage_registered_release(
        transaction_id="registered-1",
        canonical_remote_url="https://github.com/example/mac.git",
        commit_sha=SHA,
        tree_digest=tree_digest,
        ci_evidence={"contexts": ["contract"]},
    )

    assert staged.repository_name == "mac"
    assert staged.branch == ""
    assert staged.commit_sha == SHA
    assert staged.tree_digest == tree_digest
    assert staged.evidence["ci"]["passed"] == ["contract"]
    assert staged.evidence["local_contract_tests"]["status"] == "passed"
    assert staged.evidence["deployment_inputs_digest"].startswith("sha256:")
    assert staged.evidence_digest.startswith("sha256:")
    assert Path(staged.stage_path).is_dir()

    gate.discard_stage("registered-1")
    assert not Path(staged.stage_path).exists()
    gate.discard_stage("registered-1")


def test_default_runner_bounds_output_and_converts_timeout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        gate_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["synthetic"], 0, "x" * 1_000_010, "diagnostic"
        ),
    )

    completed = SourceReleaseGate._run(["synthetic"], tmp_path, 1)

    assert completed.returncode == 0
    assert len(completed.stdout) == 1_000_000
    assert completed.stderr == "diagnostic"

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["synthetic"],
            1,
            output="partial",
            stderr="slow",
        )

    monkeypatch.setattr(gate_module.subprocess, "run", timeout)
    expired = SourceReleaseGate._run(["synthetic"], tmp_path, 0)

    assert expired.returncode == 124
    assert expired.stdout == "partial"
    assert "slow" in expired.stderr
    assert "command timed out" in expired.stderr
