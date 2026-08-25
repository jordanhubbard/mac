from __future__ import annotations

from pathlib import Path

import pytest

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
