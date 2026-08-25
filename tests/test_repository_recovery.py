from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac.repository_recovery import (
    RepositoryRecoveryError,
    inspect_finalizer_recovery,
    recover_finalizer_worktree,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _recovery_fixture(
    tmp_path: Path,
    *,
    test_command: str = "true",
    with_sanity_wrapper: bool = False,
) -> tuple[Path, Path, Path, str]:
    """Build a preserved new-file-refusal workspace.

    ``test_command`` is the repository contract gate and ``with_sanity_wrapper``
    commits the repository-owned sanity wrapper into the base commit so wrapper
    evidence has valid provenance.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "tests@example.invalid")
    _git(seed, "config", "user.name", "tests")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    if with_sanity_wrapper:
        wrapper = seed / "scripts" / "run-sanity-tests.sh"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        wrapper.chmod(0o755)
        _git(seed, "add", "scripts/run-sanity-tests.sh")
    _git(seed, "commit", "-m", "base")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")

    worktree = tmp_path / "worktree"
    subprocess.run(["git", "clone", str(remote), str(worktree)], check=True, capture_output=True)
    _git(worktree, "checkout", "main")
    _git(worktree, "config", "user.email", "tests@example.invalid")
    _git(worktree, "config", "user.name", "tests")
    (worktree / "README.md").write_text("recovered\n", encoding="utf-8")
    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = "task_1234567890abcdef1234567890abcdef"
    task = {
        "id": task_id,
        "title": "Preserved task",
        "metadata": {
            "execution_contract": {
                "repository_contract": {
                    "canonical_remote_url": remote.as_uri(),
                    "default_branch": "main",
                    "test": {"command": test_command},
                }
            }
        },
    }
    (workspace / "task.json").write_text(json.dumps({"task": task}), encoding="utf-8")
    (workspace / "repository-worktree.json").write_text(
        json.dumps(
            {
                "repository_worktree": str(worktree),
                "repository_branch": "mac/recovery-test",
                "repository_base_sha": base,
                "repository_canonical_branch": "main",
                "repository_canonical_remote_url": remote.as_uri(),
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "problems": [
            "untracked files present at finalize time — agent must commit ALL new files before declaring done: new_module.py",
            "repository finalizer had local errors; refusing to push",
        ],
        "repo": {
            "head_sha": base,
            "dirty": True,
            "pushed": False,
            "remote_ref": "refs/heads/mac/recovery-test",
            "files_changed": ["README.md"],
        },
        "tests": [{"command": test_command, "returncode": 0, "status": "pass"}],
        "codegraph": {
            "schema": "mac.codegraph_audit.v1",
            "status": "pass",
            "reason": "affected_computed",
            "relevant_files": ["README.md"],
            "commands": [],
        },
    }
    (workspace / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    (workspace / "worker-result.json").write_text(
        json.dumps({"returncode": 0, "summary": "done"}), encoding="utf-8"
    )
    return workspace, worktree, remote, base


def _passing_codegraph(_worktree: Path, files: list[str]):
    return {
        "schema": "mac.codegraph_audit.v1",
        "status": "pass",
        "reason": "affected_computed",
        "relevant_files": files,
        "commands": [
            {"argv": ["codegraph", "sync"], "returncode": 0},
            {"argv": ["codegraph", "affected"], "returncode": 0},
        ],
    }


def test_inspect_finalizer_recovery_is_read_only_and_lists_exact_new_files(tmp_path: Path):
    workspace, worktree, _remote, base = _recovery_fixture(tmp_path)

    plan = inspect_finalizer_recovery(workspace)

    assert plan["eligible"] is True
    assert plan["head_sha"] == base
    assert plan["preserved_test_evidence"]["gate"] == "contract"
    assert plan["preserved_test_evidence"]["accepted"] is True
    assert plan["new_files"] == ["new_module.py"]
    assert plan["changed_files"] == ["README.md", "new_module.py"]
    assert _git(worktree, "rev-parse", "HEAD") == base
    assert "?? new_module.py" in _git(worktree, "status", "--short")


def test_recovery_requires_exact_new_file_approval(tmp_path: Path):
    workspace, _worktree, _remote, _base = _recovery_fixture(tmp_path)

    with pytest.raises(RepositoryRecoveryError, match="must exactly match"):
        inspect_finalizer_recovery(
            workspace,
            approved_new_files=["different.py"],
        )


def test_recovery_rejects_stale_head_and_semantic_failure(tmp_path: Path):
    workspace, worktree, _remote, _base = _recovery_fixture(tmp_path)
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "unexpected head")

    with pytest.raises(RepositoryRecoveryError, match="HEAD no longer matches"):
        inspect_finalizer_recovery(workspace)

    # Restore the original shape and prove non-protocol failures are rejected.
    workspace, _worktree, _remote, _base = _recovery_fixture(tmp_path / "second")
    manifest_path = workspace / "mac-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["problems"].append("repository contract test failed")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RepositoryRecoveryError, match="non-recoverable"):
        inspect_finalizer_recovery(workspace)


def test_execute_revalidates_commits_with_provenance_and_pushes(tmp_path: Path):
    workspace, worktree, remote, base = _recovery_fixture(tmp_path)
    calls = []

    def passing_test(command: str, cwd: Path):
        calls.append((command, cwd))
        return subprocess.CompletedProcess(["bash", "-lc", command], 0, "pass\n", "")

    result = recover_finalizer_worktree(
        workspace,
        approved_new_files=["new_module.py"],
        original_evidence_id="ev_original",
        execute=True,
        test_runner=passing_test,
        codegraph_runner=_passing_codegraph,
    )

    assert result["status"] == "complete"
    assert result["remote_verified"] is True
    assert result["original_evidence_id"] == "ev_original"
    assert calls == [("true", worktree)]
    assert _git(worktree, "status", "--porcelain") == ""
    message = _git(worktree, "show", "-s", "--format=%B", "HEAD")
    assert "MAC-Original-Evidence: ev_original" in message
    assert "MAC-Recovery-Reason: new-file-finalizer-refusal" in message
    remote_line = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/mac/recovery-test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_line == result["recovery_head_sha"]
    assert base != result["recovery_head_sha"]
    assert (workspace / "recovery-evidence.json").exists()


_CONTRACT_COMMAND = "scripts/run-contract-tests.sh"
_SANITY_COMMAND = "scripts/run-sanity-tests.sh"


def _preserve_test_evidence(workspace: Path, command: str, **overrides) -> None:
    """Replace the preserved gate result with one wrapper/gate run.

    A malformed entry and a non-gate command are kept alongside it: recovery
    must judge the gate itself and ignore the rest of a preserved manifest.
    """
    item = {"command": command, "returncode": 0, "status": "pass", "total": 532}
    item.update(overrides)
    manifest_path = workspace / "mac-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tests"] = [
        "not-a-result-object",
        {"command": "python3 scripts/bootstrap-project.py", "returncode": 0},
        item,
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _rewrite_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_recovery_accepts_passing_full_sanity_wrapper_evidence(tmp_path: Path):
    """The wrapper escalated to the whole-repo suite; that IS the contract gate."""
    workspace, _worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
        with_sanity_wrapper=True,
    )
    _preserve_test_evidence(workspace, "%s --base %s" % (_SANITY_COMMAND, base))

    plan = inspect_finalizer_recovery(workspace)

    assert plan["eligible"] is True
    assert plan["preserved_test_evidence"]["gate"] == "sanity_wrapper"
    assert plan["preserved_test_evidence"]["base"] == base
    # Acceptance never narrows what recovery itself reruns after rebasing.
    assert plan["test_command"] == _CONTRACT_COMMAND


def test_recovery_accepts_focused_sanity_wrapper_with_abbreviated_base(tmp_path: Path):
    workspace, _worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
        with_sanity_wrapper=True,
    )
    _preserve_test_evidence(
        workspace,
        "./%s --base=%s --changed-file src/mac/repository_recovery.py"
        % (_SANITY_COMMAND, base[:12]),
        total=17,
        mode="focused",
    )

    plan = inspect_finalizer_recovery(workspace)

    assert plan["eligible"] is True
    assert plan["preserved_test_evidence"]["gate"] == "sanity_wrapper"
    assert plan["preserved_test_evidence"]["base"] == base[:12]


def test_sanity_wrapper_recovery_still_reruns_the_full_contract_gate(tmp_path: Path):
    workspace, worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
        with_sanity_wrapper=True,
    )
    _preserve_test_evidence(workspace, "%s --base %s" % (_SANITY_COMMAND, base))
    calls = []

    def passing_test(command: str, cwd: Path):
        calls.append((command, cwd))
        return subprocess.CompletedProcess(["bash", "-lc", command], 0, "pass\n", "")

    result = recover_finalizer_worktree(
        workspace,
        approved_new_files=["new_module.py"],
        original_evidence_id="ev_sanity",
        execute=True,
        test_runner=passing_test,
        codegraph_runner=_passing_codegraph,
    )

    assert result["status"] == "complete"
    assert calls == [(_CONTRACT_COMMAND, worktree)]
    published = json.loads((workspace / "recovery-evidence.json").read_text(encoding="utf-8"))
    assert published["preserved_test_evidence"]["command"].startswith(_SANITY_COMMAND)


@pytest.mark.parametrize(
    "command_template, overrides, expected",
    [
        # A wrapper run against some other base proves nothing about this task.
        ("{sanity} --base {other}", {}, "does not match the prepared base"),
        ("{sanity} --base {short}", {}, "does not match the prepared base"),
        ("{sanity}", {}, "without a --base"),
        ("{sanity} --base {base}", {"returncode": 1, "status": "fail"}, "did not pass"),
        ("{sanity} --base {base}", {"status": "fail"}, "did not pass"),
        # Arguments outside the wrapper surface change scope unverifiably.
        ("{sanity} --base {base} --tests-only", {}, "unapproved arguments"),
        ("{sanity} --base {base} --changed-file ../outside.py", {}, "unapproved arguments"),
        ("{sanity} --base", {}, "unapproved arguments"),
        ("{sanity} --base {base} --base {other}", {}, "unapproved arguments"),
        # A ref-shaped base is not provably this task's prepared base.
        ("{sanity} --base origin/main", {}, "does not match the prepared base"),
        # Arbitrary, malformed, or unreadable commands are not a repository gate.
        ("pytest -q tests/test_repository_recovery.py", {}, "is not passing"),
        ('{sanity} --base "{base}', {}, "is not passing"),
        ("", {}, "is not passing"),
        ("{contract}", {"returncode": 2, "status": "fail"}, "did not pass"),
        ("{contract}", {"returncode": None}, "did not pass"),
    ],
)
def test_recovery_rejects_unapproved_or_failing_test_evidence(
    tmp_path: Path,
    command_template: str,
    overrides: dict,
    expected: str,
):
    workspace, _worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
        with_sanity_wrapper=True,
    )
    command = command_template.format(
        sanity=_SANITY_COMMAND,
        contract=_CONTRACT_COMMAND,
        base=base,
        short=base[:5],
        other="0" * 40,
    )
    _preserve_test_evidence(workspace, command, **overrides)

    with pytest.raises(RepositoryRecoveryError, match=expected):
        inspect_finalizer_recovery(workspace)


def test_recovery_reads_the_prepared_base_from_task_runtime_metadata(tmp_path: Path):
    workspace, _worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
        with_sanity_wrapper=True,
    )
    _preserve_test_evidence(workspace, "%s --base %s" % (_SANITY_COMMAND, base))
    _rewrite_json(
        workspace / "repository-worktree.json",
        lambda document: document.pop("repository_base_sha"),
    )
    _rewrite_json(
        workspace / "task.json",
        lambda document: document["task"]["metadata"].update(
            {"runtime": {"repository_base_sha": base}}
        ),
    )

    plan = inspect_finalizer_recovery(workspace)

    assert plan["preserved_test_evidence"]["accepted"] is True


def test_recovery_rejects_wrapper_evidence_with_no_prepared_base_on_record(tmp_path: Path):
    workspace, _worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
        with_sanity_wrapper=True,
    )
    _preserve_test_evidence(workspace, "%s --base %s" % (_SANITY_COMMAND, base))
    _rewrite_json(
        workspace / "repository-worktree.json",
        lambda document: document.pop("repository_base_sha"),
    )

    with pytest.raises(RepositoryRecoveryError, match="does not match the prepared base"):
        inspect_finalizer_recovery(workspace)


def test_recovery_rejects_a_sanity_wrapper_the_repository_does_not_own(tmp_path: Path):
    """An uncommitted wrapper in the refused worktree is not an approved gate."""
    workspace, worktree, _remote, base = _recovery_fixture(
        tmp_path,
        test_command=_CONTRACT_COMMAND,
    )
    _preserve_test_evidence(workspace, "%s --base %s" % (_SANITY_COMMAND, base))
    planted = worktree / _SANITY_COMMAND
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    planted.chmod(0o755)

    with pytest.raises(RepositoryRecoveryError, match="not committed"):
        inspect_finalizer_recovery(workspace)


def _stalled_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """A finalizer that harvested verified work but stalled before pushing.

    The worktree already committed the recovered change on the task branch, a
    partial ``mac-evidence.json`` carries the ``finalizer_interrupted`` marker,
    and ``finalizer-progress.json`` is stuck in a non-terminal status.
    """

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "tests@example.invalid")
    _git(seed, "config", "user.name", "tests")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "base")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")

    worktree = tmp_path / "worktree"
    subprocess.run(["git", "clone", str(remote), str(worktree)], check=True, capture_output=True)
    _git(worktree, "checkout", "main")
    _git(worktree, "config", "user.email", "tests@example.invalid")
    _git(worktree, "config", "user.name", "tests")
    (worktree / "README.md").write_text("recovered\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "harvested work")
    head = _git(worktree, "rev-parse", "HEAD")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = "task_1234567890abcdef1234567890abcdef"
    task = {
        "id": task_id,
        "title": "Stalled task",
        "metadata": {
            "execution_contract": {
                "repository_contract": {
                    "canonical_remote_url": remote.as_uri(),
                    "default_branch": "main",
                    "test": {"command": "true"},
                }
            }
        },
    }
    (workspace / "task.json").write_text(json.dumps({"task": task}), encoding="utf-8")
    (workspace / "repository-worktree.json").write_text(
        json.dumps(
            {
                "repository_worktree": str(worktree),
                "repository_branch": "mac/recovery-test",
                "repository_base_sha": base,
                "repository_canonical_branch": "main",
                "repository_canonical_remote_url": remote.as_uri(),
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "fail",
        "partial": True,
        "evidence_type": "repo_change",
        "finalizer_interrupted": {"phase": "guarded_push", "reason": "timeout"},
        "repo": {
            "head_sha": head,
            "base_sha": base,
            "dirty": True,
            "pushed": False,
            "remote_ref": "refs/heads/mac/recovery-test",
            "files_changed": ["README.md"],
        },
    }
    (workspace / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    (workspace / "finalizer-progress.json").write_text(
        json.dumps(
            {
                "schema": "mac.finalizer_progress.v1",
                "task_id": task_id,
                "phase": "guarded_push",
                "status": "timeout",
            }
        ),
        encoding="utf-8",
    )
    return workspace, worktree, remote, base


def test_inspect_stalled_finalizer_is_read_only(tmp_path: Path):
    from mac.repository_recovery import inspect_stalled_finalizer_recovery

    workspace, worktree, _remote, _base = _stalled_fixture(tmp_path)
    head = _git(worktree, "rev-parse", "HEAD")

    plan = inspect_stalled_finalizer_recovery(workspace)

    assert plan["eligible"] is True
    assert plan["progress_status"] == "timeout"
    assert plan["head_sha"] == head
    assert plan["committed_files"] == ["README.md"]
    assert plan["new_files"] == []
    # Inspection must not touch the worktree.
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert _git(worktree, "status", "--porcelain") == ""


def test_stalled_recovery_rejects_completed_progress(tmp_path: Path):
    from mac.repository_recovery import inspect_stalled_finalizer_recovery

    workspace, _worktree, _remote, _base = _stalled_fixture(tmp_path)
    progress_path = workspace / "finalizer-progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["status"] = "pass"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    with pytest.raises(RepositoryRecoveryError, match="reports completion"):
        inspect_stalled_finalizer_recovery(workspace)


def test_stalled_recovery_rejects_stale_head(tmp_path: Path):
    from mac.repository_recovery import inspect_stalled_finalizer_recovery

    workspace, worktree, _remote, _base = _stalled_fixture(tmp_path)
    (worktree / "extra.txt").write_text("drift\n", encoding="utf-8")
    _git(worktree, "add", "extra.txt")
    _git(worktree, "commit", "-m", "unexpected drift")

    with pytest.raises(RepositoryRecoveryError, match="HEAD no longer matches"):
        inspect_stalled_finalizer_recovery(workspace)


def test_stalled_recovery_requires_progress_record(tmp_path: Path):
    from mac.repository_recovery import inspect_stalled_finalizer_recovery

    workspace, _worktree, _remote, _base = _stalled_fixture(tmp_path)
    (workspace / "finalizer-progress.json").unlink()

    with pytest.raises(RepositoryRecoveryError, match="not a stalled-finalizer case"):
        inspect_stalled_finalizer_recovery(workspace)


def test_stalled_recovery_execute_commits_and_pushes(tmp_path: Path):
    from mac.repository_recovery import recover_stalled_finalizer

    workspace, worktree, remote, base = _stalled_fixture(tmp_path)
    calls = []

    def passing_test(command: str, cwd: Path):
        calls.append((command, cwd))
        return subprocess.CompletedProcess(["bash", "-lc", command], 0, "pass\n", "")

    result = recover_stalled_finalizer(
        workspace,
        original_evidence_id="ev_stalled",
        execute=True,
        test_runner=passing_test,
        codegraph_runner=_passing_codegraph,
    )

    assert result["status"] == "complete"
    assert result["remote_verified"] is True
    assert result["original_evidence_id"] == "ev_stalled"
    assert calls == [("true", worktree)]
    message = _git(worktree, "show", "-s", "--format=%B", "HEAD")
    # The harvested commit is preserved; no empty recovery commit is created
    # because the work was already committed before the finalizer stalled.
    assert "MAC-Recovery-Reason: stalled-finalizer" not in message
    remote_line = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/mac/recovery-test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_line == result["recovery_head_sha"]
    assert base != result["recovery_head_sha"]
    assert (workspace / "stalled-recovery-evidence.json").exists()


def test_stalled_recovery_execute_commits_uncommitted_new_file(tmp_path: Path):
    from mac.repository_recovery import recover_stalled_finalizer

    workspace, worktree, remote, _base = _stalled_fixture(tmp_path)
    # A finalizer that stalled with an uncommitted new file still present.
    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    def passing_test(command: str, cwd: Path):
        return subprocess.CompletedProcess(["bash", "-lc", command], 0, "pass\n", "")

    result = recover_stalled_finalizer(
        workspace,
        original_evidence_id="ev_stalled",
        approved_new_files=["new_module.py"],
        execute=True,
        test_runner=passing_test,
        codegraph_runner=_passing_codegraph,
    )

    assert result["status"] == "complete"
    message = _git(worktree, "show", "-s", "--format=%B", "HEAD")
    assert "MAC-Recovery-Reason: stalled-finalizer" in message
    assert "MAC-Original-Evidence: ev_stalled" in message
    assert _git(worktree, "status", "--porcelain") == ""
    remote_line = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/mac/recovery-test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_line == result["recovery_head_sha"]


def test_stalled_recovery_execute_rejects_unapproved_new_file(tmp_path: Path):
    from mac.repository_recovery import recover_stalled_finalizer

    workspace, worktree, _remote, _base = _stalled_fixture(tmp_path)
    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(RepositoryRecoveryError, match="--approve-new-file"):
        recover_stalled_finalizer(
            workspace,
            original_evidence_id="ev_stalled",
            execute=True,
        )
