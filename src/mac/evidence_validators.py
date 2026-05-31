from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from mac.models import JsonDict, ValidationError, ensure_json_object


GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
WORKTREE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# mem-13: when evidence claims pushed=true with a remote_url + remote_ref,
# the validator runs `git ls-remote <url> <ref>` and refuses the evidence
# if the ref doesn't resolve. This closes the validator gap that let
# bullwinkle's phantom-push evidence get past the repo anchor check.
#
# Set MAC_VALIDATE_REMOTE_REFS=0 to skip the network round-trip (useful
# for offline development; tests run with no remote_url so they don't
# touch the network either way).
_REMOTE_REF_VERIFY_TIMEOUT_SEC = 8


def _remote_ref_verification_enabled() -> bool:
    flag = os.environ.get("MAC_VALIDATE_REMOTE_REFS", "1")
    return flag not in {"", "0", "false", "False"}


def _verify_remote_ref_resolves(remote_url: str, remote_ref: str) -> Optional[str]:
    """Return None if the ref resolves on the remote; a string problem
    description otherwise. Returns None on network failure (best-effort)
    so a flaky CI doesn't reject legitimate evidence."""
    if not remote_url or not remote_ref:
        return None
    try:
        completed = subprocess.run(
            ["git", "ls-remote", remote_url, remote_ref],
            capture_output=True,
            text=True,
            timeout=_REMOTE_REF_VERIFY_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None  # best-effort: we can't reach git or network
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return (
            "repo.remote_ref %s does not resolve on %s (git ls-remote "
            "returncode=%d: %s)"
            % (remote_ref, remote_url, completed.returncode, stderr[:200])
        )
    if not (completed.stdout or "").strip():
        return (
            "repo.remote_ref %s did not match any ref on %s "
            "(git ls-remote returned no output)" % (remote_ref, remote_url)
        )
    return None


@dataclass(frozen=True)
class VerificationRepoAnchor:
    head_sha: str
    dirty: Any
    pushed: bool
    remote_ref: str
    remote_url: str
    pr_url: str
    files_changed: List[Any]

    @classmethod
    def parse(cls, manifest: Mapping[str, Any]) -> Optional["VerificationRepoAnchor"]:
        repo = manifest.get("repo")
        if not isinstance(repo, dict):
            return None
        return cls(
            head_sha=str(repo.get("head_sha") or "").strip(),
            dirty=repo.get("dirty"),
            pushed=repo.get("pushed") is True
            or str(repo.get("pushed") or "").lower() == "true",
            remote_ref=str(repo.get("remote_ref") or "").strip(),
            remote_url=str(repo.get("remote_url") or "").strip(),
            pr_url=str(repo.get("pr_url") or "").strip(),
            files_changed=_manifest_list(repo.get("files_changed")),
        )


@dataclass(frozen=True)
class VerificationManifest:
    raw: JsonDict
    schema: str
    status: str
    evidence_type: str
    repo: Optional[VerificationRepoAnchor]

    @classmethod
    def parse(cls, raw: Any) -> "VerificationManifest":
        if not isinstance(raw, dict):
            raise ValidationError("verification manifest must be an object")
        data = ensure_json_object(raw)
        return cls(
            raw=data,
            schema=str(data.get("schema") or "").strip(),
            status=str(data.get("status") or "").strip().lower(),
            evidence_type=str(data.get("evidence_type") or "").strip().lower(),
            repo=VerificationRepoAnchor.parse(data),
        )


@dataclass(frozen=True)
class EvidenceValidationContext:
    passed_check_count: Callable[[JsonDict], int]
    allow_empty_repo_change: bool = False
    # mem-11: a repo-coupled task (one with a repository_contract / git target)
    # must produce a pushed repo anchor, not a free-text operator_result.
    repo_coupled: bool = False


class EvidenceValidator:
    evidence_type = ""

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        raise NotImplementedError

    def require_pushed_repo_anchor(self, manifest: VerificationManifest) -> List[str]:
        repo = manifest.repo
        if repo is None:
            return ["repo evidence requires verification.repo object"]
        problems: List[str] = []
        if not GIT_SHA_RE.match(repo.head_sha):
            problems.append("repo.head_sha must be a git SHA")
        if repo.dirty not in {False, "false", "False", 0, "0"}:
            problems.append("repo evidence must declare dirty=false")
        if not (repo.pushed and repo.remote_ref) and not repo.pr_url:
            problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
        # mem-13: when the manifest claims pushed=true with a remote_url
        # + remote_ref, ask git itself whether the ref resolves. This
        # catches the failure mode where an executor lies about pushing
        # (the original task_d7c51a0b incident was on a soft validator
        # path that mem-11 closes; this anchor check defends the strict
        # validator path too). Network failures don't reject evidence —
        # they fall back to the existing static checks.
        if (
            repo.pushed
            and repo.remote_url
            and repo.remote_ref
            and _remote_ref_verification_enabled()
        ):
            failure = _verify_remote_ref_resolves(repo.remote_url, repo.remote_ref)
            if failure is not None:
                problems.append(failure)
        return problems

    def passed_checks(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> int:
        return context.passed_check_count(manifest.raw)


class RepoChangeValidator(EvidenceValidator):
    evidence_type = "repo_change"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems = self.require_pushed_repo_anchor(manifest)
        if (
            manifest.repo is not None
            and not manifest.repo.files_changed
            and not context.allow_empty_repo_change
        ):
            problems.append("repo evidence requires changed files")
        if self.passed_checks(manifest, context) < 1:
            problems.append("repo code evidence requires at least one passing test/check")
        return problems


class DocumentationValidator(RepoChangeValidator):
    evidence_type = "documentation"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems = self.require_pushed_repo_anchor(manifest)
        if manifest.repo is not None and not manifest.repo.files_changed:
            problems.append("repo evidence requires changed files")
        return problems


class DeploymentValidator(EvidenceValidator):
    evidence_type = "deployment"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems = self.require_pushed_repo_anchor(manifest)
        if self.passed_checks(manifest, context) < 1:
            problems.append("deployment evidence requires at least one passing check")
        if not (
            _manifest_list(manifest.raw.get("targets"))
            or _manifest_list(manifest.raw.get("services"))
            or _manifest_list(manifest.raw.get("artifacts"))
        ):
            problems.append("deployment evidence requires targets, services, or artifacts")
        return problems


class TestValidator(EvidenceValidator):
    evidence_type = "test"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems = self.require_pushed_repo_anchor(manifest)
        if self.passed_checks(manifest, context) < 1:
            problems.append("test evidence requires at least one passing check or test")
        return problems


class ArtifactValidator(TestValidator):
    evidence_type = "artifact"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems = self.require_pushed_repo_anchor(manifest)
        if self.passed_checks(manifest, context) < 1:
            problems.append("artifact evidence requires at least one passing check or test")
        if not _manifest_list(manifest.raw.get("artifacts")):
            problems.append("artifact evidence requires artifacts")
        return problems


class NoChangeValidator(EvidenceValidator):
    evidence_type = "no_change"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems = self.require_pushed_repo_anchor(manifest)
        if not str(manifest.raw.get("reason") or manifest.raw.get("no_change_reason") or "").strip():
            problems.append("no_change evidence requires a reason")
        if self.passed_checks(manifest, context) < 1:
            problems.append("no_change evidence requires at least one passing check")
        return problems


class ReviewVerdictValidator(EvidenceValidator):
    evidence_type = "review_verdict"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems: List[str] = []
        verdict = str(manifest.raw.get("verdict") or "").strip().lower()
        if verdict not in {"approved", "rejected"}:
            problems.append("review_verdict evidence requires verdict approved or rejected")
        if not str(manifest.raw.get("reviewed_evidence_id") or "").strip():
            problems.append("review_verdict evidence requires reviewed_evidence_id")
        digest = str(manifest.raw.get("worktree_digest") or "").strip()
        if not WORKTREE_DIGEST_RE.match(digest):
            problems.append("review_verdict evidence requires worktree_digest sha256")
        if verdict == "approved":
            if self.passed_checks(manifest, context) < 1:
                problems.append("review_verdict evidence requires at least one independent passing check")
        return problems


class OperatorResultValidator(EvidenceValidator):
    evidence_type = "operator_result"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        # mem-11: reject the permissive operator_result path for repo-coupled
        # tasks. The verified task_d7c51a0b incident was a code task whose
        # executor emitted operator_result with a "hello hello…" summary, no
        # commit, and pushed=false — it passed here and then jammed the review
        # loop. A repo task must anchor on a pushed commit.
        if context.repo_coupled:
            return [
                "operator_result evidence is not accepted for a repo-coupled task; "
                "provide repo_change/test/no_change evidence with a pushed repo anchor "
                "(repo.head_sha, repo.pushed=true, repo.remote_ref)"
            ]
        if str(manifest.raw.get("summary") or manifest.raw.get("result") or "").strip():
            return []
        if _manifest_list(manifest.raw.get("artifacts")) or _manifest_list(manifest.raw.get("findings")):
            return []
        return ["operator_result evidence requires summary, result, findings, or artifacts"]


VALIDATORS: Dict[str, EvidenceValidator] = {
    validator.evidence_type: validator
    for validator in (
        RepoChangeValidator(),
        DocumentationValidator(),
        DeploymentValidator(),
        TestValidator(),
        ArtifactValidator(),
        NoChangeValidator(),
        ReviewVerdictValidator(),
        OperatorResultValidator(),
    )
}


def registered_evidence_types() -> List[str]:
    return sorted(VALIDATORS)


def validate_evidence_type(
    evidence_type: str,
    manifest: Any,
    *,
    passed_check_count: Callable[[JsonDict], int],
    allow_empty_repo_change: bool = False,
    repo_coupled: bool = False,
) -> List[str]:
    typed = VerificationManifest.parse(manifest)
    validator = VALIDATORS.get(str(evidence_type or "").strip().lower())
    if validator is None:
        return ["unsupported verification.evidence_type: %s" % evidence_type]
    return validator.validate(
        typed,
        EvidenceValidationContext(
            passed_check_count=passed_check_count,
            allow_empty_repo_change=allow_empty_repo_change,
            repo_coupled=repo_coupled,
        ),
    )


def _manifest_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []
