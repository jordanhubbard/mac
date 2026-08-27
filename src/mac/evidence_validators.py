"""Validation of task evidence and verification manifests.

Defines the dataclasses and checks that normalize and validate submitted
evidence, including verification-manifest anchors, remote-ref resolution, and
git-remote consistency rules enforced before publication.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac.canonical_reconcile import reconcile_evidence_problems
from mac.fleet_learning import (
    AUTH_FAILURE_CLASSES,
    classify_repository_access_failure,
    resolve_git_remote_access,
)
from mac.gitops import redact_git_remote_auth_in_text
from mac.models import EVIDENCE_KINDS, JsonDict, ValidationError, ensure_json_object


GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
WORKTREE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def normalize_manifest_tests(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Normalize ``verification.tests`` so it is always a list of result objects.

    The canonical shape is a list (mac-wjy3).  Some executors historically
    emitted a single result *dict* instead of a one-element list, which caused
    ``RepoChangeValidator`` to treat the dict as tests:null/missing and reject
    otherwise-passing evidence at submit-for-review time.

    Rules:
    - ``tests`` already a list → returned unchanged (pass-through).
    - ``tests`` is a dict (single result object) → wrapped in a list.
    - ``tests`` is None / absent / any other type → left as-is so the
      fail-closed "missing tests" path still triggers when no tests ran.

    The function returns a *new* mapping with the normalised ``tests`` value;
    it never mutates ``raw``.
    """
    tests = raw.get("tests")
    if isinstance(tests, dict):
        # Single structured result — wrap it so the list check passes.
        normalised: Dict[str, Any] = dict(raw)
        normalised["tests"] = [tests]
        return normalised
    return raw


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
    description otherwise.

    Repository authentication is resolved through the same environment-backed
    path as worker Git operations.  Authentication, authorization, and network
    failures remain best-effort: they prove only that this control-plane
    process could not inspect the ref, not that the executor's ref is absent.
    A successful lookup with no matching ref still rejects phantom evidence.
    """
    if not remote_url or not remote_ref:
        return None
    access = resolve_git_remote_access(remote_url)
    try:
        completed = subprocess.run(
            ["git", "ls-remote", access.remote, remote_ref],
            capture_output=True,
            text=True,
            timeout=_REMOTE_REF_VERIFY_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None  # best-effort: we can't reach git or network
    if completed.returncode != 0:
        stderr = redact_git_remote_auth_in_text((completed.stderr or "").strip())
        failure_class = classify_repository_access_failure(stderr)
        if failure_class in {*AUTH_FAILURE_CLASSES, "network"}:
            return None
        return "repo.remote_ref %s does not resolve on %s (git ls-remote returncode=%d: %s)" % (
            remote_ref,
            access.display,
            completed.returncode,
            stderr[:200],
        )
    if not (completed.stdout or "").strip():
        return (
            "repo.remote_ref %s did not match any ref on %s "
            "(git ls-remote returned no output)" % (remote_ref, access.display)
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
            pushed=repo.get("pushed") is True or str(repo.get("pushed") or "").lower() == "true",
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
        data = ensure_json_object(normalize_manifest_tests(raw))
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
    # mac-wjy3: a task whose contract requires tests must record a tests list.
    require_tests: bool = False
    # Prepared canonical HEAD the worker attached. Empty means this run did
    # not snapshot a worktree (unit tests of bare manifests stay fail-open).
    expected_reconcile_head_sha: str = ""


class EvidenceValidator:
    evidence_type = ""

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        raise NotImplementedError

    def require_clean_repo_anchor(self, manifest: VerificationManifest) -> List[str]:
        repo = manifest.repo
        if repo is None:
            return ["repo evidence requires verification.repo object"]
        problems: List[str] = []
        if not GIT_SHA_RE.match(repo.head_sha):
            problems.append("repo.head_sha must be a git SHA")
        if repo.dirty not in {False, "false", "False", 0, "0"}:
            problems.append("repo evidence must declare dirty=false")
        return problems

    def require_pushed_repo_anchor(self, manifest: VerificationManifest) -> List[str]:
        problems = self.require_clean_repo_anchor(manifest)
        repo = manifest.repo
        if repo is None:
            return problems
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
        # mac-wjy3: when the task's contract requires tests, the manifest must
        # record a tests list — tests:null/missing (no invocation) is rejected.
        # This is gated by ``require_tests`` so config/remediation repo changes
        # that legitimately run no tests are unaffected.
        if context.require_tests and not isinstance(manifest.raw.get("tests"), list):
            problems.append(
                "this task's contract requires tests, but verification.tests is "
                "null/missing — run the repository test command and record results"
            )
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
        # A genuine no-change result must identify the clean commit that was
        # inspected, but requiring a newly pushed ref contradicts the evidence
        # type and turned correct investigations into deterministic retries.
        problems = self.require_clean_repo_anchor(manifest)
        if not str(
            manifest.raw.get("reason") or manifest.raw.get("no_change_reason") or ""
        ).strip():
            problems.append("no_change evidence requires a reason")
        if self.passed_checks(manifest, context) < 1:
            problems.append("no_change evidence requires at least one passing check")
        return problems


def rejected_verdict_feedback_problems(raw: Mapping[str, Any]) -> List[str]:
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict != "rejected":
        return []
    has_feedback = bool(str(raw.get("feedback") or "").strip())
    has_summary = bool(str(raw.get("summary") or "").strip())
    findings = raw.get("findings")
    has_findings = isinstance(findings, list) and bool(findings)
    if has_feedback or has_summary or has_findings:
        return []
    return ["rejected review_verdict requires feedback, findings, or summary"]


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
        problems.extend(rejected_verdict_feedback_problems(manifest.raw))
        if verdict == "approved":
            if self.passed_checks(manifest, context) < 1:
                problems.append(
                    "review_verdict evidence requires at least one independent passing check"
                )
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
        problems = operator_result_validation_problems(manifest.raw)
        if problems:
            return problems
        return operator_result_live_host_review_problems(
            manifest.raw, self.passed_checks(manifest, context)
        )


class InvestigationValidator(EvidenceValidator):
    evidence_type = "investigation"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        return operator_result_validation_problems(manifest.raw)


class PlanDecomposedValidator(EvidenceValidator):
    evidence_type = "plan_decomposed"

    def validate(
        self,
        manifest: VerificationManifest,
        context: EvidenceValidationContext,
    ) -> List[str]:
        problems: List[str] = []
        children = _manifest_list(manifest.raw.get("children"))
        if not children:
            problems.append("plan_decomposed evidence requires a non-empty children list")
        else:
            for index, child in enumerate(children, start=1):
                if not isinstance(child, dict) or not str(child.get("title") or "").strip():
                    problems.append("plan_decomposed child %d requires a title" % index)
        if not str(manifest.raw.get("ordering_rationale") or "").strip():
            problems.append("plan_decomposed evidence requires ordering_rationale")
        if not str(manifest.raw.get("coverage_claim") or "").strip():
            problems.append("plan_decomposed evidence requires coverage_claim")
        return problems


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
        InvestigationValidator(),
        PlanDecomposedValidator(),
    )
}


# The canonical evidence-kind registry (``mac.models.EVIDENCE_KINDS``) is the one
# vocabulary every validation path consults. Every ``evidence_type`` the validator
# registry advertises must therefore also be an addable kind through the public
# CLI/API — otherwise ``ControlPlane.add_evidence`` would reject a request these
# validators accept, the exact contradiction this module exists to prevent. Assert
# the subset relationship at import time so a validator added here without a
# matching canonical kind fails fast instead of drifting apart in production.
_UNREGISTERED_VALIDATOR_KINDS = set(VALIDATORS) - EVIDENCE_KINDS
assert not _UNREGISTERED_VALIDATOR_KINDS, (
    "evidence validators advertise kinds missing from the canonical "
    "mac.models.EVIDENCE_KINDS registry: %s" % ", ".join(sorted(_UNREGISTERED_VALIDATOR_KINDS))
)


def registered_evidence_types() -> List[str]:
    return sorted(VALIDATORS)


def validate_evidence_type(
    evidence_type: str,
    manifest: Any,
    *,
    passed_check_count: Callable[[JsonDict], int],
    allow_empty_repo_change: bool = False,
    repo_coupled: bool = False,
    require_tests: bool = False,
    expected_reconcile_head_sha: str = "",
) -> List[str]:
    typed = VerificationManifest.parse(manifest)
    validator = VALIDATORS.get(str(evidence_type or "").strip().lower())
    if validator is None:
        return ["unsupported verification.evidence_type: %s" % evidence_type]
    problems = validator.validate(
        typed,
        EvidenceValidationContext(
            passed_check_count=passed_check_count,
            allow_empty_repo_change=allow_empty_repo_change,
            repo_coupled=repo_coupled,
            require_tests=require_tests,
            expected_reconcile_head_sha=expected_reconcile_head_sha,
        ),
    )
    problems.extend(
        reconcile_evidence_problems(
            typed.raw,
            str(evidence_type or "").strip().lower(),
            expected_reconcile_head_sha,
        )
    )
    return problems


def _manifest_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


# mem-11 / autonomy-loop fix: the executor's fallback evidence writer turns the
# agent's raw chat output (or its own no-output placeholder) into an
# operator_result. The verified jam was a task whose deliverable was literally
# "hello hello hello". These markers + the distinct-token floor below let the
# validator reject degenerate, non-substantive operator_result text without
# over-rejecting genuine short planning summaries (which carry several distinct
# words, or structured findings/artifacts).
_OPERATOR_RESULT_PLACEHOLDERS = frozenset(
    {
        "hermes executor completed without textual output",
        "hermes executor completed",
    }
)
_OPERATOR_RESULT_MIN_DISTINCT_TOKENS = 3


def _operator_result_is_substantive(text: str) -> bool:
    """True when ``text`` reads like a real deliverable rather than degenerate
    chatter ('hello hello hello') or the executor's own no-output placeholder."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return False
    if cleaned.lower().rstrip(". ") in _OPERATOR_RESULT_PLACEHOLDERS:
        return False
    # Distinct, non-trivial word tokens. 'hello hello hello' collapses to one
    # distinct token; a real summary carries several.
    tokens = {t for t in re.findall(r"[a-z0-9]+", cleaned.lower()) if len(t) > 1}
    return len(tokens) >= _OPERATOR_RESULT_MIN_DISTINCT_TOKENS


def _operator_result_sources(raw: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Canonical nested report plus the legacy flat envelope, as one logical
    result. Executors write the report under ``operator_result``; older
    producers wrote its fields directly on the envelope."""
    nested = raw.get("operator_result")
    sources: List[Mapping[str, Any]] = []
    if isinstance(nested, Mapping):
        sources.append(nested)
    sources.append(raw)
    return sources


def operator_result_validation_problems(raw: Mapping[str, Any]) -> List[str]:
    """Validate both canonical nested and legacy flat operator results.

    Executors write the typed verification envelope at the top level and the
    actual report under ``operator_result``.  Older producers wrote the report
    fields directly on the envelope.  Treat both locations as one logical
    result so worker preflight and server validation cannot disagree.
    """
    sources = _operator_result_sources(raw)

    if any(
        _manifest_list(source.get("artifacts")) or _manifest_list(source.get("findings"))
        for source in sources
    ):
        return []

    combined = " ".join(
        part
        for source in sources
        for part in (
            str(source.get("summary") or "").strip(),
            str(source.get("result") or "").strip(),
        )
        if part
    ).strip()
    if not combined:
        return ["operator_result evidence requires summary, result, findings, or artifacts"]
    if not _operator_result_is_substantive(combined):
        return [
            "operator_result evidence is not substantive (degenerate or placeholder "
            "text); provide a real summary/result describing the completed work, or "
            "structured findings/artifacts"
        ]
    return []


# mem-11 follow-up: a live-host operator_result (an ops/deployment task run
# against a real host, not a repo change) is accepted on its report alone by
# the substance gate above. That lets a live-host rollout claim success with
# free text and no independently reviewable anchor. When the manifest declares
# it acted on a live host, require the same class of anchor the deployment path
# demands: at least one passing check plus a verifiable artifact/target/host
# identifier (or artifact digest) a reviewer can check. Non-live operator_result
# (planning/answer/report work) keeps the substance-only gate.
_OPERATOR_RESULT_LIVE_HOST_KEYS = ("live_host", "live-host")
_OPERATOR_RESULT_HOST_ANCHOR_KEYS = (
    "host",
    "hosts",
    "target",
    "targets",
    "service",
    "services",
    "artifact",
    "artifacts",
    "artifact_digest",
    "image_digest",
)


def _operator_result_field(sources: List[Mapping[str, Any]], key: str) -> Any:
    for source in sources:
        if key in source:
            return source.get(key)
    return None


def _operator_result_is_live_host(sources: List[Mapping[str, Any]]) -> bool:
    for key in _OPERATOR_RESULT_LIVE_HOST_KEYS:
        value = _operator_result_field(sources, key)
        if value is True or str(value or "").strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def _operator_result_has_reviewable_anchor(sources: List[Mapping[str, Any]]) -> bool:
    for key in _OPERATOR_RESULT_HOST_ANCHOR_KEYS:
        value = _operator_result_field(sources, key)
        if isinstance(value, (list, tuple)):
            if any(str(item or "").strip() for item in value):
                return True
        elif str(value or "").strip():
            return True
    return False


def operator_result_live_host_review_problems(
    raw: Mapping[str, Any], passed_checks: int
) -> List[str]:
    """Require a live-host operator_result to carry independently reviewable
    anchors (a passing check plus a verifiable artifact/target/host identifier),
    not a substantive summary alone. Non-live results are unaffected."""
    sources = _operator_result_sources(raw)
    if not _operator_result_is_live_host(sources):
        return []
    problems: List[str] = []
    if passed_checks < 1:
        problems.append(
            "live-host operator_result evidence requires at least one passing "
            "check verifying the work on the host"
        )
    if not _operator_result_has_reviewable_anchor(sources):
        problems.append(
            "live-host operator_result evidence requires an independently "
            "reviewable anchor (artifact/target/host identifier or artifact "
            "digest), not a summary alone"
        )
    return problems
