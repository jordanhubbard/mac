"""Fail-closed staging gate for an immutable source release."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from mac import gitops, mac_paths
from mac.models import JsonDict, ValidationError


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


@dataclass(frozen=True)
class StagedSourceRelease:
    repository_name: str
    canonical_remote_url: str
    branch: str
    commit_sha: str
    canonical_ref: str
    tree_digest: str
    stage_path: str
    evidence: JsonDict
    evidence_digest: str


Runner = Callable[[Sequence[str], Path, int], CommandResult]
RequiredChecks = Callable[[str, str], Optional[Tuple[str, ...]]]
CheckVerdicts = Callable[[str, str, Tuple[str, ...]], Dict[str, object]]


class SourceReleaseGate:
    """Freeze, verify, test, and stage one exact source generation."""

    def __init__(
        self,
        repository_path: Path,
        *,
        runner: Optional[Runner] = None,
        required_checks: Optional[RequiredChecks] = None,
        check_verdicts: Optional[CheckVerdicts] = None,
        stage_root: Optional[Path] = None,
    ) -> None:
        self.repository_path = Path(repository_path).resolve()
        self.runner = runner or self._run
        self.required_checks = required_checks or gitops.required_status_check_contexts
        self.check_verdicts = check_verdicts or gitops.required_check_verdicts
        self.stage_root = (
            Path(stage_root).resolve()
            if stage_root is not None
            else mac_paths.mac_home() / "upgrades" / "staging"
        )

    def stage_approved_current(
        self,
        *,
        transaction_id: str,
        branch: str = "main",
        remote: str = "origin",
        explicit_required_checks: Optional[Sequence[str]] = None,
        bootstrap_timeout_seconds: int = 1800,
        test_timeout_seconds: int = 5400,
    ) -> StagedSourceRelease:
        if not transaction_id or "/" in transaction_id or transaction_id in {".", ".."}:
            raise ValidationError("safe transaction_id is required")
        if not branch or branch.startswith("-"):
            raise ValidationError("safe release branch is required")
        repository = self.repository_path
        if not (repository / ".git").exists() and not self._git_file(repository):
            raise ValidationError("source release repository is not a git checkout")

        self._require_ok(["git", "fetch", "--quiet", remote, branch], repository, 300, "fetch")
        remote_ref = "refs/remotes/%s/%s" % (remote, branch)
        target_sha = self._stdout(
            ["git", "rev-parse", "--verify", "%s^{commit}" % remote_ref],
            repository,
            30,
            "resolve approved current",
        )
        if len(target_sha) != 40:
            raise ValidationError("resolved source revision is not a full commit SHA")
        canonical_remote = self._stdout(
            ["git", "remote", "get-url", remote], repository, 30, "resolve canonical remote"
        )
        repository_name = Path(canonical_remote.rstrip("/").removesuffix(".git")).name
        tree_material = self._stdout(
            ["git", "ls-tree", "-r", "--full-tree", target_sha],
            repository,
            120,
            "compute source tree digest",
            preserve_whitespace=True,
        )
        tree_digest = "sha256:" + hashlib.sha256(tree_material.encode("utf-8")).hexdigest()
        deployment_material = self._stdout(
            [
                "git",
                "ls-tree",
                "-r",
                "--full-tree",
                target_sha,
                "--",
                "deploy",
                "scripts",
                "src/mac",
                "Makefile",
                "pyproject.toml",
                "uv.lock",
            ],
            repository,
            120,
            "compute deployment inputs digest",
            preserve_whitespace=True,
        )
        deployment_inputs_digest = (
            "sha256:" + hashlib.sha256(deployment_material.encode("utf-8")).hexdigest()
        )

        contexts = tuple(
            str(item).strip() for item in (explicit_required_checks or ()) if str(item).strip()
        )
        if not contexts:
            discovered = self.required_checks(canonical_remote, branch)
            if not discovered:
                raise ValidationError("required CI checks are unknown or empty")
            contexts = tuple(discovered)
        verdicts = dict(self.check_verdicts(canonical_remote, target_sha, contexts))
        if (
            verdicts.get("known") is not True
            or verdicts.get("failed")
            or verdicts.get("pending")
            or sorted(verdicts.get("passed") or []) != sorted(contexts)
        ):
            raise ValidationError("required CI checks are not green for %s" % target_sha)

        self.stage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.stage_root.chmod(0o700)
        except OSError:
            pass
        stage_path = self.stage_root / transaction_id
        if stage_path.exists():
            raise ValidationError("source stage already exists for transaction")
        self._require_ok(
            ["git", "worktree", "add", "--detach", str(stage_path), target_sha],
            repository,
            300,
            "create immutable source stage",
        )
        try:
            stage_path.chmod(0o700)
        except OSError:
            pass

        bootstrap = self.runner(
            ["python3", "scripts/bootstrap-project.py"],
            stage_path,
            max(1, int(bootstrap_timeout_seconds)),
        )
        if bootstrap.returncode != 0:
            raise ValidationError("staged source bootstrap failed")
        tests = self.runner(
            ["scripts/run-contract-tests.sh"],
            stage_path,
            max(1, int(test_timeout_seconds)),
        )
        if tests.returncode != 0:
            raise ValidationError("staged source contract tests failed")

        self._require_ok(["git", "fetch", "--quiet", remote, branch], repository, 300, "refetch")
        final_sha = self._stdout(
            ["git", "rev-parse", "--verify", "%s^{commit}" % remote_ref],
            repository,
            30,
            "recheck approved current",
        )
        if final_sha != target_sha:
            raise ValidationError(
                "release branch moved during staging; restart against the new immutable SHA"
            )
        staged_sha = self._stdout(
            ["git", "rev-parse", "HEAD"], stage_path, 30, "verify staged source"
        )
        dirty = self._stdout(
            ["git", "status", "--porcelain"], stage_path, 30, "verify staged source cleanliness"
        )
        if staged_sha != target_sha or dirty:
            raise ValidationError("staged source no longer matches the approved commit")

        evidence: JsonDict = {
            "schema": "mac.source_release_gate.v1",
            "transaction_id": transaction_id,
            "repository_name": repository_name,
            "canonical_remote_url": canonical_remote,
            "branch": branch,
            "commit_sha": target_sha,
            "tree_digest": tree_digest,
            "ci": {
                "known": True,
                "contexts": list(contexts),
                "passed": list(verdicts.get("passed") or []),
                "pending": [],
                "failed": [],
                "check_conclusions": {context: "success" for context in contexts},
                "commit_url": self._commit_url(canonical_remote, target_sha),
            },
            "deployment_inputs_digest": deployment_inputs_digest,
            "bootstrap": self._command_evidence(bootstrap),
            "local_contract_tests": {
                **self._command_evidence(tests),
                "status": "passed",
            },
            "stage_path": str(stage_path),
        }
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        evidence_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return StagedSourceRelease(
            repository_name=repository_name,
            canonical_remote_url=canonical_remote,
            branch=branch,
            commit_sha=target_sha,
            canonical_ref=target_sha,
            tree_digest=tree_digest,
            stage_path=str(stage_path),
            evidence=evidence,
            evidence_digest=evidence_digest,
        )

    def stage_registered_release(
        self,
        *,
        transaction_id: str,
        canonical_remote_url: str,
        commit_sha: str,
        tree_digest: str,
        ci_evidence: Mapping[str, Any],
        remote: str = "origin",
        bootstrap_timeout_seconds: int = 1800,
        test_timeout_seconds: int = 5400,
    ) -> StagedSourceRelease:
        """Re-stage and re-test an already registered immutable release."""
        if not transaction_id or "/" in transaction_id or transaction_id in {".", ".."}:
            raise ValidationError("safe transaction_id is required")
        repository = self.repository_path
        configured_remote = self._stdout(
            ["git", "remote", "get-url", remote],
            repository,
            30,
            "resolve canonical remote",
        )
        if configured_remote != canonical_remote_url:
            raise ValidationError("registered release remote does not match deployment checkout")
        contexts = tuple(str(item) for item in ci_evidence.get("contexts") or () if str(item))
        if not contexts:
            raise ValidationError("registered release has no required CI contexts")
        verdicts = dict(self.check_verdicts(configured_remote, commit_sha, contexts))
        if (
            verdicts.get("known") is not True
            or verdicts.get("failed")
            or verdicts.get("pending")
            or sorted(verdicts.get("passed") or []) != sorted(contexts)
        ):
            raise ValidationError("registered release CI is no longer green")
        self._require_ok(
            ["git", "fetch", "--quiet", remote, commit_sha],
            repository,
            300,
            "fetch registered release",
        )
        resolved = self._stdout(
            ["git", "rev-parse", "--verify", "%s^{commit}" % commit_sha],
            repository,
            30,
            "resolve registered release",
        )
        if resolved != commit_sha:
            raise ValidationError("registered release commit could not be resolved exactly")
        tree_material = self._stdout(
            ["git", "ls-tree", "-r", "--full-tree", commit_sha],
            repository,
            120,
            "compute source tree digest",
            preserve_whitespace=True,
        )
        actual_tree_digest = "sha256:" + hashlib.sha256(tree_material.encode("utf-8")).hexdigest()
        if actual_tree_digest != tree_digest:
            raise ValidationError("registered release tree digest mismatch")
        deployment_material = self._stdout(
            [
                "git",
                "ls-tree",
                "-r",
                "--full-tree",
                commit_sha,
                "--",
                "deploy",
                "scripts",
                "src/mac",
                "Makefile",
                "pyproject.toml",
                "uv.lock",
            ],
            repository,
            120,
            "compute deployment inputs digest",
            preserve_whitespace=True,
        )
        self.stage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage_path = self.stage_root / transaction_id
        if stage_path.exists():
            raise ValidationError("source stage already exists for transaction")
        self._require_ok(
            ["git", "worktree", "add", "--detach", str(stage_path), commit_sha],
            repository,
            300,
            "create immutable source stage",
        )
        bootstrap = self.runner(
            ["python3", "scripts/bootstrap-project.py"],
            stage_path,
            max(1, int(bootstrap_timeout_seconds)),
        )
        if bootstrap.returncode != 0:
            raise ValidationError("staged source bootstrap failed")
        tests = self.runner(
            ["scripts/run-contract-tests.sh"],
            stage_path,
            max(1, int(test_timeout_seconds)),
        )
        if tests.returncode != 0:
            raise ValidationError("staged source contract tests failed")
        staged_sha = self._stdout(
            ["git", "rev-parse", "HEAD"], stage_path, 30, "verify staged source"
        )
        dirty = self._stdout(
            ["git", "status", "--porcelain"], stage_path, 30, "verify staged source cleanliness"
        )
        if staged_sha != commit_sha or dirty:
            raise ValidationError("staged source no longer matches the registered release")
        evidence: JsonDict = {
            "schema": "mac.source_release_gate.v1",
            "transaction_id": transaction_id,
            "repository_name": Path(configured_remote.rstrip("/").removesuffix(".git")).name,
            "canonical_remote_url": configured_remote,
            "commit_sha": commit_sha,
            "tree_digest": actual_tree_digest,
            "ci": {
                "known": True,
                "contexts": list(contexts),
                "passed": list(verdicts.get("passed") or []),
                "pending": [],
                "failed": [],
                "check_conclusions": {context: "success" for context in contexts},
                "commit_url": self._commit_url(configured_remote, commit_sha),
            },
            "deployment_inputs_digest": (
                "sha256:" + hashlib.sha256(deployment_material.encode("utf-8")).hexdigest()
            ),
            "bootstrap": self._command_evidence(bootstrap),
            "local_contract_tests": {
                **self._command_evidence(tests),
                "status": "passed",
            },
            "stage_path": str(stage_path),
        }
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return StagedSourceRelease(
            repository_name=str(evidence["repository_name"]),
            canonical_remote_url=configured_remote,
            branch="",
            commit_sha=commit_sha,
            canonical_ref=commit_sha,
            tree_digest=actual_tree_digest,
            stage_path=str(stage_path),
            evidence=evidence,
            evidence_digest="sha256:" + hashlib.sha256(encoded).hexdigest(),
        )

    def discard_stage(self, transaction_id: str) -> None:
        stage_path = self.stage_root / transaction_id
        if not stage_path.exists():
            return
        result = self.runner(
            ["git", "worktree", "remove", "--force", str(stage_path)],
            self.repository_path,
            300,
        )
        if result.returncode != 0:
            raise RuntimeError("could not discard source stage")
        if stage_path.exists():
            shutil.rmtree(stage_path)

    def _stdout(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout: int,
        operation: str,
        *,
        preserve_whitespace: bool = False,
    ) -> str:
        result = self.runner(argv, cwd, timeout)
        if result.returncode != 0:
            raise ValidationError("%s failed" % operation)
        return result.stdout if preserve_whitespace else result.stdout.strip()

    def _require_ok(
        self, argv: Sequence[str], cwd: Path, timeout: int, operation: str
    ) -> CommandResult:
        result = self.runner(argv, cwd, timeout)
        if result.returncode != 0:
            raise ValidationError("%s failed" % operation)
        return result

    @staticmethod
    def _command_evidence(result: CommandResult) -> JsonDict:
        output = (result.stdout + "\n" + result.stderr).encode("utf-8", errors="replace")
        return {
            "returncode": result.returncode,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "output_digest": "sha256:" + hashlib.sha256(output).hexdigest(),
            "output_bytes": len(output),
        }

    @staticmethod
    def _git_file(path: Path) -> bool:
        marker = path / ".git"
        return marker.is_file() and marker.read_text(errors="replace").startswith("gitdir:")

    @staticmethod
    def _commit_url(remote: str, commit_sha: str) -> str:
        value = remote.rstrip("/")
        if value.startswith("git@github.com:"):
            value = "https://github.com/" + value.removeprefix("git@github.com:")
        if value.endswith(".git"):
            value = value[:-4]
        return "%s/commit/%s" % (value, commit_sha) if value.startswith("https://") else ""

    @staticmethod
    def _run(argv: Sequence[str], cwd: Path, timeout: int) -> CommandResult:
        started = time.monotonic()
        env = os.environ.copy()
        env.setdefault("CI", "1")
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            return CommandResult(
                124,
                str(exc.stdout or ""),
                str(exc.stderr or "") + "\ncommand timed out",
                elapsed,
            )
        return CommandResult(
            int(completed.returncode),
            completed.stdout[-1_000_000:],
            completed.stderr[-1_000_000:],
            time.monotonic() - started,
        )
