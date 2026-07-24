"""Report/answer (non-code) deliverable tasks.

A task declared ``metadata.deliverable == "report"`` is satisfied by a
substantive ``operator_result`` — no repo diff, no pushed branch — so
investigation / triage / answer tasks (and system-smoke tasks) run without
faking a code change, while the code-substance gate is untouched for real
code tasks.
"""
from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from mac import task_executor as te
from mac import worker as wk
from mac.models import (
    REPORT_REPOSITORY_ACCESS_SCHEMA,
    metadata_declares_read_only_report_repository,
    metadata_declares_report_deliverable,
    normalize_deliverable_kind,
)
from mac.hermes_adapter import MacApiClient
from mac.repository_access_env import (
    fence_read_only_repository_environment,
    read_only_repository_content_digest,
)
from mac.worker import MacWorker, WorkerExecution


@pytest.mark.parametrize(
    "value,expected",
    [
        ("report", "report"),
        ("answer", "report"),
        ("Investigation", "report"),
        ("TRIAGE", "report"),
        ("code", ""),
        ("", ""),
        (None, ""),
        ("weird", "weird"),
    ],
)
def test_normalize_deliverable_kind(value, expected):
    assert normalize_deliverable_kind(value) == expected


def test_predicate_reads_metadata():
    assert metadata_declares_report_deliverable({"deliverable": "report"})
    assert metadata_declares_report_deliverable({"deliverable": "answer"})
    assert not metadata_declares_report_deliverable({"deliverable": "code"})
    assert not metadata_declares_report_deliverable({})
    assert not metadata_declares_report_deliverable(None)


def _read_only_access():
    return {
        "schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
        "mode": "read_only",
    }


def test_read_only_repository_opt_in_requires_exact_report_schema_and_mode():
    assert metadata_declares_read_only_report_repository(
        {"deliverable": "analysis", "report_repository_access": _read_only_access()}
    )
    assert not metadata_declares_read_only_report_repository(
        {"deliverable": "code", "report_repository_access": _read_only_access()}
    )
    assert not metadata_declares_read_only_report_repository(
        {
            "deliverable": "report",
            "report_repository_access": {"schema": "wrong", "mode": "read_only"},
        }
    )
    assert not metadata_declares_read_only_report_repository(
        {
            "deliverable": "report",
            "report_repository_access": {
                "schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
                "mode": "write",
            },
        }
    )


def _repo_task(deliverable=None):
    md = {
        "origin": {
            "repository_path": "/repo",
            "repository_contract": {"schema": "mac.repo.v1", "test": {"command": "make test"}},
        },
        "execution_contract": {"type": "repository"},
    }
    if deliverable:
        md["deliverable"] = deliverable
    return {"id": "task_x", "metadata": md}


def test_report_declaration_flips_all_repo_coupling_checks():
    code_task = _repo_task()
    report_task = _repo_task(deliverable="report")

    # executor repo-coupling
    assert te.task_is_repo_coupled(code_task) is True
    assert te.task_is_repo_coupled(report_task) is False

    # worker worktree/finalizer trigger
    assert wk._repository_task_origin(code_task) is not None
    assert wk._repository_task_origin(report_task) is None


def test_read_only_report_gets_repository_origin_but_stays_operator_result():
    task = _repo_task(deliverable="report")
    task["metadata"]["report_repository_access"] = _read_only_access()
    task["metadata"]["execution_contract"]["evidence_type"] = "repo_change"

    assert wk._repository_task_origin(task) is not None
    assert te.task_is_repo_coupled(task) is False
    assert te.task_evidence_type(task) == "operator_result"


def test_report_task_uses_operator_result_fallback(tmp_path):
    """The executor fallback writes operator_result for a report task even
    though it carries a repository contract (would be repo_change otherwise)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    task = _repo_task(deliverable="report")
    result = type("R", (), {"returncode": 0, "stdout": "Investigated: config is correct.\n", "stderr": ""})()
    te.write_fallback_evidence_manifest(ws, task, result, None)
    import json

    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["status"] == "complete"


def test_code_task_still_blocks_operator_result_fallback(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    task = _repo_task()  # code
    result = type("R", (), {"returncode": 0, "stdout": "hi", "stderr": ""})()
    te.write_fallback_evidence_manifest(ws, task, result, None)
    # Repo-coupled code task: fallback refuses to fabricate operator_result.
    assert not (ws / "mac-evidence.json").exists()


def test_services_repo_coupled_and_operator_result_enforcement_honor_report():
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    report = cp.create_task("report task", metadata=_repo_task(deliverable="report")["metadata"])
    code = cp.create_task("code task", metadata=_repo_task()["metadata"])

    assert cp._task_is_repo_coupled(report) is False
    assert cp._task_is_repo_coupled(code) is True
    # operator_result enforcement: raises for the code task, silent for report.
    md_op = {"verification": {"evidence_type": "operator_result"}}
    cp._enforce_repo_coupled_evidence_type(report, md_op)  # no raise
    with pytest.raises(Exception, match="operator_result"):
        cp._enforce_repo_coupled_evidence_type(code, md_op)


def _register_repo(cp, tmp_path):
    repo_path = tmp_path / "proj-src"
    contract_dir = repo_path / ".mac"
    contract_dir.mkdir(parents=True)
    (contract_dir / "project.yaml").write_text(
        (
            "schema: mac.repository_contract.v1\n"
            "project: proj\n"
            "platforms:\n"
            "  - darwin\n"
            "  - linux\n"
            "  - wsl2\n"
            "toolchain:\n"
            "  required_commands:\n"
            "    - python3\n"
            "bootstrap:\n"
            "  command: python3 scripts/bootstrap-project.py\n"
            "  creates:\n"
            "    - .venv/bin/python\n"
            "test:\n"
            "  command: make test\n"
            "evidence:\n"
            "  required:\n"
            "    - tests\n"
        ),
        encoding="utf-8",
    )
    return cp.register_project_repository(
        "proj",
        str(repo_path),
        project="proj",
    )


def test_project_report_keeps_operator_result_contract(tmp_path):
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    _register_repo(cp, tmp_path)

    contract = cp._normalize_task_execution_contract(
        {"deliverable": "report"}, "proj", []
    )["execution_contract"]

    assert contract["type"] == "operator_directive"
    assert contract["evidence_type"] == "operator_result"
    assert contract["repository_required"] is False
    # Repository context preserved for reproducibility, but no repo_change coupling.
    assert "repository_contract" not in contract
    ctx = contract["repository_context"]
    assert ctx["repository_name"] == "proj"
    assert ctx["repository_contract_project"] == "proj"


def test_project_code_task_still_repo_change(tmp_path):
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    _register_repo(cp, tmp_path)

    for md in ({}, {"execution_contract": {"type": "repository"}}):
        contract = cp._normalize_task_execution_contract(md, "proj", [])[
            "execution_contract"
        ]
        assert contract["type"] == "repository"
        assert contract["evidence_type"] == "repo_change"


def test_project_report_not_upgraded_via_metadata_execution_contract(tmp_path):
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    _register_repo(cp, tmp_path)

    contract = cp._normalize_task_execution_contract(
        {
            "deliverable": "investigation",
            "evidence_type": "investigation",
            "execution_contract": {"type": "repository", "evidence_type": "repo_change"},
        },
        "proj",
        [],
    )["execution_contract"]

    # A report cannot be silently upgraded to repo_change through metadata.
    assert contract["type"] == "operator_directive"
    assert contract["repository_required"] is False
    assert "repository_contract" not in contract
    # Explicit report evidence type is honored; leaked repo_change is ignored.
    assert contract["evidence_type"] == "investigation"


def test_project_report_no_missing_contract_error(tmp_path):
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    # No registered repository; a report must not raise the missing-contract error.
    contract = cp._normalize_task_execution_contract(
        {
            "deliverable": "report",
            "execution_contract": {"type": "repository"},
            "origin": {"repository_url": "https://example.invalid/repo.git"},
        },
        "proj",
        [],
    )["execution_contract"]
    assert contract["type"] == "operator_directive"
    assert contract["evidence_type"] == "operator_result"


# ---------------------------------------------------------------------------
# Alias coverage: every report alias stays operator_result with AND without a
# registered project, and code tasks stay repo_change (mac task_e2abc48b).
# ---------------------------------------------------------------------------

# The full set of report deliverable aliases the normalizer must exempt from
# the repo_change path.  Mirrors ``_REPORT_DELIVERABLE_ALIASES`` in mac.models.
REPORT_ALIASES = ("report", "analysis", "investigation", "answer", "question", "triage")


@pytest.mark.parametrize("alias", REPORT_ALIASES)
def test_project_report_alias_stays_operator_result_with_registered_project(alias, tmp_path):
    """Each report alias created against a registered project keeps an
    operator_result contract while preserving repository CONTEXT."""
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    repo = _register_repo(cp, tmp_path)

    normalized = cp._normalize_task_execution_contract({"deliverable": alias}, "proj", [])
    contract = normalized["execution_contract"]

    # Non-repository operator contract, never repo_change.
    assert contract["type"] == "operator_directive"
    assert contract["evidence_type"] == "operator_result"
    assert contract["repository_required"] is False
    assert contract.get("evidence_type") != "repo_change"
    assert "repository_contract" not in contract

    # Repository context preserved for reviewer reproducibility.
    ctx = contract["repository_context"]
    assert ctx["repository_id"] == repo.id
    assert ctx["repository_name"] == repo.name
    assert ctx["repository_path"] == repo.path
    assert ctx["repository_contract_project"] == "proj"
    assert ctx["repository_contract_schema"] == "mac.repository_contract.v1"

    # The normalized origin / acc_metadata also carry the registered identity so
    # a reviewer can reproduce the inspection without re-resolving the project.
    origin = normalized["origin"]
    assert origin["repository_id"] == repo.id
    assert origin["repository_name"] == repo.name
    assert origin["repository_path"] == repo.path
    acc = normalized["acc_metadata"]
    assert acc["repository_contract_project"] == "proj"
    assert acc["repository_contract_schema"] == "mac.repository_contract.v1"


@pytest.mark.parametrize("alias", REPORT_ALIASES)
def test_project_report_alias_stays_operator_result_without_registered_project(alias):
    """Each report alias created WITHOUT a registered project still normalizes
    to a non-repository operator_result contract (no missing-contract error)."""
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()

    normalized = cp._normalize_task_execution_contract({"deliverable": alias}, "proj", [])
    contract = normalized["execution_contract"]

    assert contract["type"] == "operator_directive"
    assert contract["evidence_type"] == "operator_result"
    assert contract["repository_required"] is False
    assert "repository_contract" not in contract
    # No registered project -> no repository context to preserve.
    assert "repository_context" not in contract


@pytest.mark.parametrize("alias", REPORT_ALIASES)
def test_project_report_alias_not_upgraded_via_metadata_execution_contract(alias, tmp_path):
    """A report alias cannot be smuggled onto the repo_change path via an
    explicit metadata.execution_contract.type=repository."""
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    _register_repo(cp, tmp_path)

    contract = cp._normalize_task_execution_contract(
        {
            "deliverable": alias,
            "execution_contract": {"type": "repository", "evidence_type": "repo_change"},
        },
        "proj",
        [],
    )["execution_contract"]

    assert contract["type"] == "operator_directive"
    assert contract["repository_required"] is False
    assert contract["evidence_type"] == "operator_result"
    assert "repository_contract" not in contract


def test_project_code_task_with_registered_project_stays_repo_change(tmp_path):
    """A default (code) task against a registered project remains a repository /
    repo_change task carrying the registered contract."""
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    repo = _register_repo(cp, tmp_path)

    normalized = cp._normalize_task_execution_contract({}, "proj", [])
    contract = normalized["execution_contract"]

    assert contract["type"] == "repository"
    assert contract["evidence_type"] == "repo_change"
    assert contract["repository_id"] == repo.id
    assert isinstance(contract["repository_contract"], dict)
    assert contract["repository_contract"]["project"] == "proj"


def test_code_task_for_repository_url_without_registered_contract_still_raises():
    """A code task for a project advertising repository_url but no registered
    repository contract keeps raising the existing ValidationError."""
    from mac.services import ControlPlane
    from mac.models import ValidationError

    cp = ControlPlane.in_memory()
    cp.create_project(
        "widget",
        metadata={"repository_url": "https://github.com/o/widget.git"},
        dispatch_paused=False,
    )

    with pytest.raises(ValidationError, match="no registered repository contract"):
        cp.create_task("Fix widget bug", project="widget", required_capabilities=["python"])


def _git(cwd, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _registered_repository(tmp_path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(source))
    _git(source, "config", "user.email", "mac-tests@example.invalid")
    _git(source, "config", "user.name", "MAC tests")
    (source / "README.md").write_text("current repository\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    return source.resolve(), remote.resolve()


def _worker(tmp_path):
    return MacWorker(
        MacApiClient("http://mac.test", transport=lambda *_args, **_kwargs: {}),
        "agent-report",
        tmp_path / "workspaces",
        lambda *_args: WorkerExecution(0, "unused"),
        attestation_key="test-key",
    )


def _directory_bytes(path):
    snapshot = {}
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_symlink():
            snapshot[relative] = ("symlink", candidate.readlink().as_posix())
        elif candidate.is_file():
            snapshot[relative] = ("file", candidate.read_bytes())
        elif candidate.is_dir():
            snapshot[relative] = ("directory", b"")
    return snapshot


def test_dirty_registered_checkout_is_ignored_for_read_only_canonical_clone(tmp_path):
    source, remote = _registered_repository(tmp_path)
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "inspection-target",
        "canonical_remote_url": str(remote),
        "default_branch": "main",
        "test": {"command": "make check"},
    }
    # Model normal parallel-worker churn: the registered checkout is dirty and
    # carries a private ref/object which the report lane must never inspect,
    # fetch into, clean, or otherwise mutate.
    (source / "README.md").write_text("unrelated worker mutation\n", encoding="utf-8")
    _git(source, "update-ref", "refs/mac/private/other-worker", "HEAD")
    task = {
        "id": "task_read_only_report",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "origin": {
                "type": "direct_task",
                "repository_path": str(source),
                "default_branch": "main",
                "repository_contract": {"schema": "stale.contract.v0"},
            },
            "execution_contract": {
                "type": "repository",
                "repository_contract": contract,
                "evidence_type": "repo_change",
            },
        },
    }
    source_remote_before = _git(source, "remote", "get-url", "origin")
    source_status_before = _git(source, "status", "--porcelain")
    source_refs_before = _git(
        source, "for-each-ref", "--format=%(refname) %(objectname)"
    )
    source_git_bytes_before = _directory_bytes(source / ".git")
    worker = _worker(tmp_path)

    task_dir = worker._prepare_task_workspace(task, {"id": "lease-report"})

    runtime = task["metadata"]["runtime"]
    worktree = runtime["repository_worktree"]
    assert runtime["checkout_policy"] == "task_owned_read_only_clone"
    assert runtime["repository_access_mode"] == "read_only"
    assert runtime["repository_contract"] == contract
    assert _git(worktree, "branch", "--show-current") == ""
    assert _git(worktree, "remote") == ""
    assert _git(worktree, "for-each-ref") == ""
    assert _git(worktree, "rev-parse", "HEAD") == runtime["repository_base_sha"]
    assert (Path(worktree) / "README.md").read_text(encoding="utf-8") == "current repository\n"
    assert _git(source, "remote", "get-url", "origin") == source_remote_before
    assert _git(source, "status", "--porcelain") == source_status_before
    assert (
        _git(source, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )
    assert _directory_bytes(source / ".git") == source_git_bytes_before
    assert json.loads((task_dir / "task.json").read_text())["task"]["metadata"][
        "runtime"
    ]["repository_contract"] == contract


def test_ordinary_report_still_prepares_no_repository(tmp_path):
    source, remote = _registered_repository(tmp_path)
    task = _repo_task(deliverable="report")
    task["metadata"]["origin"].update(
        {
            "repository_path": str(source),
            "repository_url": str(remote),
        }
    )
    worker = _worker(tmp_path)

    task_dir = worker._prepare_task_workspace(task, {"id": "lease-ordinary"})

    assert not (task_dir / "repository-worktree.json").exists()
    assert "runtime" not in task["metadata"]


def test_dirty_read_only_report_never_invokes_commit_push_or_finalizer(
    tmp_path, monkeypatch
):
    repo = tmp_path / "inspection"
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "MAC tests")
    (repo / "state.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    refs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    refs_digest = hashlib.sha256(refs.encode("utf-8")).hexdigest()
    content_digest = read_only_repository_content_digest(repo)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    task = _repo_task(deliverable="report")
    task["metadata"]["report_repository_access"] = _read_only_access()
    task["metadata"]["runtime"] = {
        "repository_worktree": str(repo),
        "repository_base_sha": base_sha,
        "repository_base_tree": base_tree,
        "repository_refs_digest": refs_digest,
        "repository_content_digest": content_digest,
        "repository_access_mode": "read_only",
        "repository_access_schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
    }
    (task_dir / "task.json").write_text(json.dumps({"task": task}), encoding="utf-8")
    (task_dir / "repository-worktree.json").write_text(
        json.dumps(task["metadata"]["runtime"]), encoding="utf-8"
    )
    (repo / "state.txt").write_text("mutated\n", encoding="utf-8")
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_finalize_missing_repository_evidence_manifest",
        lambda *_args, **_kwargs: pytest.fail("read-only report reached finalizer"),
    )
    monkeypatch.setattr(
        worker,
        "_commit_dirty_repository_worktree",
        lambda *_args, **_kwargs: pytest.fail("read-only report reached commit path"),
    )
    monkeypatch.setattr(
        wk,
        "guarded_push",
        lambda *_args, **_kwargs: pytest.fail("read-only report reached push path"),
    )

    assert worker._write_missing_repository_evidence_manifest(
        task["id"],
        task_dir,
        WorkerExecution(0, "analysis", stdout="Substantive repository findings."),
    )
    manifest = json.loads((task_dir / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["status"] == "invalid"
    assert "mutated" in " ".join(manifest["problems"])
    evidence = {"metadata": {"verification": manifest}}
    problems = worker._execution_submission_problems(task_dir, evidence)
    assert any("mutated" in problem for problem in problems)


def test_read_only_repository_environment_scrubs_credentials_and_git_injection():
    environment = {
        "GH_TOKEN": "secret",
        "GITHUB_TOKEN": "secret",
        "MAC_TASK_GIT_TOKEN": "secret",
        "GITLAB_TOKEN": "secret",
        "GITEA_TOKEN": "secret",
        "GITEA_USER": "user",
        "GIT_ASKPASS": "/tmp/askpass",
        "SSH_ASKPASS": "/tmp/ssh-askpass",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!publish-anywhere",
        "GIT_CONFIG_GLOBAL": "/tmp/host-gitconfig",
        "GIT_CONFIG_SYSTEM": "/tmp/system-gitconfig",
        "OPENAI_API_KEY": "model-credential-must-remain",
    }

    fence_read_only_repository_environment(environment)

    assert environment["OPENAI_API_KEY"] == "model-credential-must-remain"
    assert not any(
        name in environment
        for name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "MAC_TASK_GIT_TOKEN",
            "GITLAB_TOKEN",
            "GITEA_TOKEN",
            "GITEA_USER",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "SSH_AUTH_SOCK",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        )
    )
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert "IdentityFile=/dev/null" in environment["GIT_SSH_COMMAND"]


def test_read_only_content_digest_hashes_directory_symlink_target(tmp_path):
    root = tmp_path / "repository"
    first_target = root / "existing-a"
    second_target = root / "existing-b"
    first_target.mkdir(parents=True)
    second_target.mkdir()
    link = root / "selected"
    link.symlink_to(first_target.name, target_is_directory=True)

    first_digest = read_only_repository_content_digest(root)
    link.unlink()
    link.symlink_to(second_target.name, target_is_directory=True)
    second_digest = read_only_repository_content_digest(root)

    assert first_digest != second_digest


@pytest.mark.parametrize(
    ("current_contract", "error"),
    [
        (
            {"default_branch": "main", "test": {"command": "make check"}},
            "has no canonical_remote_url",
        ),
        (
            {"canonical_remote_url": "REMOTE", "test": {"command": "make check"}},
            "has no canonical branch",
        ),
    ],
)
def test_read_only_report_never_falls_back_from_incomplete_current_contract(
    tmp_path, monkeypatch, current_contract, error
):
    source, remote = _registered_repository(tmp_path)
    current_contract = dict(current_contract)
    if current_contract.get("canonical_remote_url") == "REMOTE":
        current_contract["canonical_remote_url"] = str(remote)
    stale_contract = {
        "schema": "mac.repository_contract.v1",
        "canonical_remote_url": str(remote),
        "default_branch": "main",
        "test": {"command": "make check"},
    }
    task = {
        "id": "task_incomplete_current_contract",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "origin": {
                "type": "direct_task",
                "repository_path": str(source),
                "repository_url": str(remote),
                "default_branch": "main",
                "repository_contract": stale_contract,
            },
            "repository_contract": stale_contract,
            "execution_contract": {
                "type": "repository",
                "repository_contract": current_contract,
            },
        },
    }
    monkeypatch.setenv("MAC_TASK_REPO_URL", str(remote))
    monkeypatch.setenv("MAC_TASK_REPO_DEFAULT_BRANCH", "main")

    with pytest.raises(RuntimeError, match=error):
        _worker(tmp_path)._prepare_task_workspace(task, {"id": "lease-stale"})


def test_read_only_report_test_command_never_falls_back_to_stale_contract():
    stale_contract = {"test": {"command": "printf STALE_TEST_EXECUTED"}}
    task = {
        "id": "task_missing_current_test",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "origin": {"repository_contract": stale_contract},
            "repository_contract": stale_contract,
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                    "canonical_remote_url": "https://example.invalid/repo.git",
                    "default_branch": "main",
                },
            },
        },
    }

    assert te._repository_contract_test_command(task) == ""
    assert wk._repository_contract_test_command(task) == ""


def test_read_only_report_missing_current_test_replaces_spoofed_agent_tests(
    tmp_path,
):
    stale_contract = {"test": {"command": "printf STALE_TEST_EXECUTED"}}
    task = {
        "id": "task_missing_current_test",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "origin": {"repository_contract": stale_contract},
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                    "canonical_remote_url": "https://example.invalid/repo.git",
                    "default_branch": "main",
                },
            },
        },
    }
    spoofed = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "tests": [{"command": "true", "status": "pass", "returncode": 0}],
        "checks": [{"command": "true", "status": "pass", "returncode": 0}],
    }

    attached, problems = wk._attach_trusted_read_only_report_test(
        spoofed, tmp_path, task
    )

    assert attached["tests"] == []
    assert attached["checks"] == []
    assert problems == [
        "read-only repository report current contract lacks test.command"
    ]


def test_executor_clean_read_only_report_skips_git_finalizer(
    tmp_path, monkeypatch
):
    repo = tmp_path / "inspection"
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "MAC tests")
    (repo / "state.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    refs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    refs_digest = hashlib.sha256(refs.encode("utf-8")).hexdigest()
    content_digest = read_only_repository_content_digest(repo)
    task = _repo_task(deliverable="report")
    task["metadata"]["report_repository_access"] = _read_only_access()
    task["metadata"]["runtime"] = {
        "repository_worktree": str(repo),
        "repository_base_sha": base_sha,
        "repository_base_tree": base_tree,
        "repository_refs_digest": refs_digest,
        "repository_content_digest": content_digest,
        "repository_access_mode": "read_only",
        "repository_access_schema": REPORT_REPOSITORY_ACCESS_SCHEMA,
    }
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}), encoding="utf-8")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", base_sha)
    monkeypatch.setenv("MAC_TASK_REPO_BASE_TREE", base_tree)
    monkeypatch.setenv("MAC_TASK_REPO_REFS_DIGEST", refs_digest)
    monkeypatch.setenv("MAC_TASK_REPO_CONTENT_DIGEST", content_digest)
    monkeypatch.setattr(te, "recall_prior_attempt_lessons", lambda _task: [])
    monkeypatch.setattr(te, "recall_deployment_lessons", lambda _task: [])
    monkeypatch.setattr(te, "maybe_preflight_scope_estimate", lambda _task: None)
    monkeypatch.setattr(te, "is_planning_phase", lambda _task: False)
    agent_result = subprocess.CompletedProcess(
        ["agent"], 0, "Substantive inspection findings.\n", ""
    )
    agent_result.mac_read_only_git_control_digest = (
        te._read_only_git_control_digest(repo)
    )
    monkeypatch.setattr(
        te, "_invoke_agent", lambda *_args, **_kwargs: agent_result
    )
    monkeypatch.setattr(
        te,
        "finalize_with_new_file_recovery",
        lambda *_args: pytest.fail("read-only report reached git finalizer"),
    )
    monkeypatch.setattr(te, "maybe_auto_decompose", lambda *_args: False)
    monkeypatch.setattr(te, "emit_telemetry", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(te, "record_deployment_learning", lambda *_args: None)
    monkeypatch.setattr(te, "record_curated_lessons", lambda *_args: None)

    rc = te._run_executor(
        runner=lambda *_args, **_kwargs: None,
        task=task,
        task_file=task_file,
        task_workspace=tmp_path,
        task_id=task["id"],
        review_context=None,
        is_review=False,
    )

    assert rc == 0
    manifest = json.loads((tmp_path / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["status"] == "complete"


def test_read_only_verification_failure_overwrites_complete_model_manifest(
    tmp_path, monkeypatch
):
    task = _repo_task(deliverable="report")
    task["metadata"]["report_repository_access"] = _read_only_access()
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}), encoding="utf-8")

    def failed_verification(*_args, **_kwargs):
        (tmp_path / "mac-evidence.json").write_text(
            json.dumps(
                {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "operator_result",
                    "summary": "model claims everything passed",
                    "result": "untrusted",
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.CompletedProcess(
            ["agent"], 67, "model output", "verification failed"
        )
        result.mac_read_only_verification_failure = True
        return result

    monkeypatch.setattr(te, "recall_prior_attempt_lessons", lambda _task: [])
    monkeypatch.setattr(te, "recall_deployment_lessons", lambda _task: [])
    monkeypatch.setattr(te, "maybe_preflight_scope_estimate", lambda _task: None)
    monkeypatch.setattr(te, "is_planning_phase", lambda _task: False)
    monkeypatch.setattr(te, "_invoke_agent", failed_verification)
    monkeypatch.setattr(
        te, "_read_only_report_repository_violation", lambda *_args: ""
    )
    monkeypatch.setattr(
        te,
        "maybe_auto_decompose",
        lambda *_args: pytest.fail(
            "authoritative read-only verification failure reached auto-decompose"
        ),
    )
    monkeypatch.setattr(te, "emit_telemetry", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(te, "record_deployment_learning", lambda *_args: None)
    monkeypatch.setattr(te, "record_curated_lessons", lambda *_args: None)

    rc = te._run_executor(
        runner=lambda *_args, **_kwargs: None,
        task=task,
        task_file=task_file,
        task_workspace=tmp_path,
        task_id=task["id"],
        review_context=None,
        is_review=False,
    )

    manifest = json.loads((tmp_path / "mac-evidence.json").read_text())
    assert rc == 67
    assert manifest["status"] == "invalid"
    assert "contract verification failed" in manifest["summary"].lower()
    assert "model claims everything passed" not in json.dumps(manifest)


def test_read_only_report_replaces_spoofed_test_with_host_verification(
    tmp_path,
):
    source, remote = _registered_repository(tmp_path)
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "inspection-target",
        "canonical_remote_url": str(remote),
        "default_branch": "main",
        "test": {"command": "make smoke"},
    }
    task = {
        "id": "task_trusted_report_test",
        "title": "inspect repository",
        "project": "inspection-target",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "origin": {"repository_path": str(source)},
            "execution_contract": {
                "type": "repository",
                "repository_contract": contract,
            },
        },
    }
    worker = _worker(tmp_path)
    task_dir = worker._prepare_task_workspace(task, {"id": "lease-trusted-test"})
    spoof = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "substantive analysis",
        "result": "findings",
        "tests": [
            {
                "name": "spoofed model test",
                "command": "true",
                "returncode": 0,
                "status": "pass",
                "stdout": "model supplied",
            }
        ],
    }
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(spoof), encoding="utf-8"
    )
    (task_dir / "mac-sandbox-verification.json").write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "command": "make smoke",
                "returncode": 0,
                "status": "pass",
                "stdout": "trusted smoke passed",
                "stderr": "",
                "worktree": task["metadata"]["runtime"]["repository_worktree"],
            }
        ),
        encoding="utf-8",
    )

    execution = WorkerExecution(
        0,
        "analysis complete",
        stdout="substantive analysis",
        metadata={"verification": spoof},
    )
    assert worker._write_missing_repository_evidence_manifest(
        task["id"], task_dir, execution
    )
    metadata = worker._execution_metadata(task_dir, execution)
    manifest = metadata["verification"]

    assert manifest["status"] == "complete"
    assert [item["command"] for item in manifest["tests"]] == ["make smoke"]
    assert manifest["tests"][0]["stdout"] == "trusted smoke passed"
    assert manifest["tests"][0]["execution_environment"] == "openshell_sandbox"
    assert manifest["checks"] == manifest["tests"]
    assert "spoofed model test" not in json.dumps(manifest)
    assert worker._execution_submission_problems(
        task_dir, {"metadata": metadata}
    ) == []

    (task_dir / "mac-sandbox-verification.json").unlink()
    missing_metadata = worker._execution_metadata(task_dir, execution)
    assert missing_metadata["verification"]["status"] == "invalid"
    assert any(
        "lacks trusted OpenShell" in problem
        for problem in worker._execution_submission_problems(
            task_dir, {"metadata": missing_metadata}
        )
    )


def test_read_only_report_rejects_verification_for_different_contract_command(
    tmp_path,
):
    task = {
        "id": "task_mismatched_report_test",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                    "canonical_remote_url": "https://example.invalid/repo.git",
                    "default_branch": "main",
                    "test": {"command": "make smoke"},
                },
            },
        },
    }
    (tmp_path / "mac-sandbox-verification.json").write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "command": "make test",
                "returncode": 0,
                "stdout": "different command passed",
                "stderr": "",
            }
        ),
        encoding="utf-8",
    )

    item, problems = wk._trusted_read_only_report_test_item(tmp_path, task)

    assert item is not None
    assert item["status"] == "fail"
    assert item["returncode"] == 1
    assert item["command"] == "make smoke"
    assert "does not match the repository contract" in item["stderr"]
    assert problems == ["read-only repository report contract test did not pass"]


def test_reviewer_gets_second_exact_base_credential_free_clone(tmp_path):
    source, remote = _registered_repository(tmp_path)
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "inspection-target",
        "canonical_remote_url": str(remote),
        "default_branch": "main",
        "test": {"command": "make smoke"},
    }
    task = {
        "id": "task_reviewed_report",
        "title": "inspect repository",
        "project": "inspection-target",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": _read_only_access(),
            "origin": {"repository_path": str(source)},
            "execution_contract": {
                "type": "repository",
                "repository_contract": contract,
            },
        },
    }
    worker = _worker(tmp_path)
    executor_dir = worker._prepare_task_workspace(
        task, {"id": "lease-executor-clone"}
    )
    executor_context = task["metadata"]["runtime"]
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "analysis complete",
        "result": "findings",
        "repository_access": wk._read_only_repository_access_evidence(
            executor_context
        ),
    }
    task_detail = {
        "task": task,
        "evidence": [
            {
                "id": "evidence_executor",
                "metadata": {"verification": executor_manifest},
            }
        ],
    }

    review_dir = worker._prepare_review_workspace(
        task["id"],
        "review_exact_base",
        "evidence_executor",
        task_detail,
        {"id": "message_review"},
    )
    review_task = json.loads((review_dir / "task.json").read_text())["task"]
    review_context = review_task["metadata"]["runtime"]
    review_repo = Path(review_context["repository_worktree"])

    assert review_repo != Path(executor_context["repository_worktree"])
    assert review_context["checkout_policy"] == "review_read_only_clone"
    assert _git(review_repo, "rev-parse", "HEAD") == executor_context[
        "repository_base_sha"
    ]
    assert _git(review_repo, "for-each-ref") == ""
    assert _git(review_repo, "remote") == ""
    assert read_only_repository_content_digest(review_repo) == executor_context[
        "repository_content_digest"
    ]
    assert review_task["metadata"]["origin"].get("repository_path") is None
    assert str(executor_dir) not in json.dumps(review_task)


def test_durable_evidence_harvest_rejects_symlinked_manifest(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("must not be harvested\n", encoding="utf-8")
    (task_dir / "mac-evidence.json").symlink_to(outside)
    result = task_dir / "worker-result.json"
    result.write_text('{"returncode": 0}\n', encoding="utf-8")

    artifacts = wk._durable_evidence_artifacts(task_dir, result)

    assert [artifact["name"] for artifact in artifacts] == ["worker-result.json"]
    assert "must not be harvested" not in json.dumps(artifacts)
